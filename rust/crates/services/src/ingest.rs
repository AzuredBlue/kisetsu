//! Keeping what the feeds publish: store each new article once, and match it to the followed shows.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use jiff::SignedDuration;
use kisetsu_clients::rss::FeedArticle;
use kisetsu_db::schema::{Feed, FeedItem, FeedItemMatch, Show};
use kisetsu_db::storage::{Column, format_timestamp};
use kisetsu_domain::matcher::{Matcher, broad_match, build_matcher, matching_show_ids, signature};
use kisetsu_domain::parsing::{is_batch, parse_release_title};
use kisetsu_domain::schedule::{PREMIERE_SLACK, premiere_out_of_reach};

use crate::context::{Context, MatcherSet, get_state, set_state};
use crate::error::Result;
use crate::facts::facts_for;

/// Articles not matched to any show are kept this long.
const UNMATCHED_KEEP: SignedDuration = SignedDuration::from_hours(30 * 24);
/// Matches of a completed show are kept this long.
const COMPLETED_KEEP: SignedDuration = SignedDuration::from_hours(60 * 24);
/// Items kept per show, newest first.
const PER_SHOW_CAP: i64 = 300;
const SIGNATURE_KEY: &str = "match_signature";
/// Items looked at in one statement, well under SQLite's variable limit.
const CHUNK: usize = 500;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IngestResult {
    pub new: usize,
    pub matched: usize,
    pub show_ids: BTreeSet<i64>,
    /// Shows matched on a feed their searches had left out as not carrying them: they are to be
    /// searched again.
    pub reconsider: BTreeSet<i64>,
}

/// Shows whose releases are looked for: not completed, and already near enough to premiere.
pub fn followed_shows(ctx: &Context<'_>) -> Result<Vec<Show>> {
    let shows = Show::select(ctx.conn, "WHERE completed = 0 ORDER BY id", [])?;
    let facts = facts_for(ctx, &shows)?;
    Ok(shows
        .into_iter()
        .zip(&facts)
        .filter(|(_, facts)| !premiere_out_of_reach(facts, ctx.now))
        .map(|(show, _)| show)
        .collect())
}

/// Each followed show's matcher, and the signature of what it was built from, rebuilt when a
/// show or what it is matched by changed.
pub(crate) fn matchers(ctx: &mut Context<'_>) -> Result<Arc<MatcherSet>> {
    let shows = followed_shows(ctx)?;
    let minimum = ctx.settings.min_resolution;
    let aliases: Vec<Vec<String>> = shows.iter().map(Show::effective_aliases).collect();
    let signatures: BTreeMap<i64, String> = shows
        .iter()
        .zip(&aliases)
        .filter_map(|(show, aliases)| {
            let id = show.id?;
            Some((id, matcher_signature(show, id, aliases, minimum)))
        })
        .collect();
    if let Some(cached) = ctx
        .caches
        .matchers
        .as_ref()
        .filter(|cached| cached.signatures == signatures)
    {
        return Ok(Arc::clone(cached));
    }
    let built = shows
        .iter()
        .zip(&aliases)
        .filter_map(|(show, aliases)| {
            let matcher = build_matcher(aliases, show.learned_name.as_deref(), minimum);
            show.id.map(|id| (id, matcher))
        })
        .collect();
    let set = Arc::new(MatcherSet {
        signatures,
        matchers: built,
    });
    ctx.caches.matchers = Some(Arc::clone(&set));
    Ok(set)
}

/// What a show's matcher is built from. The minimum resolution decides what matches too, so a
/// change to it rematches every show.
fn matcher_signature(show: &Show, id: i64, aliases: &[String], min_resolution: i64) -> String {
    let learned = show.learned_name.as_deref().unwrap_or_default();
    let extra = format!("min_resolution={min_resolution}");
    signature([(id, aliases, learned)], &extra)
}

/// The show's matcher from the cache `matchers` keeps, when it was built from what the show is
/// now.
pub(crate) fn cached_matcher(ctx: &Context<'_>, show: &Show) -> Option<Matcher> {
    let set = ctx.caches.matchers.as_ref()?;
    let id = show.id?;
    let built_from = set.signatures.get(&id)?;
    let now = matcher_signature(
        show,
        id,
        &show.effective_aliases(),
        ctx.settings.min_resolution,
    );
    if *built_from != now {
        return None;
    }
    set.matchers
        .iter()
        .find(|(of, _)| *of == id)
        .map(|(_, matcher)| matcher.clone())
}

/// The `(feed id, guid)` of every stored item, read once.
pub fn known_items<'c>(ctx: &'c mut Context<'_>) -> Result<&'c mut HashSet<(i64, String)>> {
    if ctx.caches.known_items.is_none() {
        let mut statement = ctx.conn.prepare("SELECT feed_id, guid FROM feed_items")?;
        let known = statement
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<std::result::Result<HashSet<_>, _>>()?;
        ctx.caches.known_items = Some(known);
    }
    Ok(ctx.caches.known_items.get_or_insert_with(HashSet::new))
}

/// The feed item an article makes, not yet stored.
pub fn new_item(feed: &Feed, article: &FeedArticle, first_seen_at: jiff::Timestamp) -> FeedItem {
    // Every single release is parsed, whatever its resolution: the minimum is a setting that can change later.
    let parsed = (!is_batch(&article.title)).then(|| parse_release_title(&article.title));
    FeedItem {
        id: None,
        feed_id: feed.id.unwrap_or_default(),
        guid: article.guid.clone(),
        info_hash: article.info_hash.clone(),
        title: article.title.clone(),
        torrent_url: Some(article.torrent_url.clone()),
        published_at: article.published_at,
        first_seen_at,
        episode_raw: parsed.as_ref().and_then(|parsed| parsed.episode),
        version: parsed.as_ref().map_or(1, |parsed| parsed.version.max(1)),
        release_group: parsed
            .as_ref()
            .and_then(|parsed| parsed.release_group.clone()),
        parsed_title: parsed.map(|parsed| parsed.title),
    }
}

/// Stores the articles this feed has not shown before and matches them to the followed shows.
pub fn ingest(
    ctx: &mut Context<'_>,
    feed: &Feed,
    articles: &[FeedArticle],
) -> Result<IngestResult> {
    let mut result = IngestResult::default();
    let feed_id = feed.id.unwrap_or_default();
    let set = matchers(ctx)?;
    let now = ctx.now;
    let mut fresh = Vec::new();
    {
        let known = known_items(ctx)?;
        for article in articles {
            if article.title.is_empty() || !known.insert((feed_id, article.guid.clone())) {
                continue;
            }
            fresh.push(new_item(feed, article, now));
        }
    }
    if fresh.is_empty() {
        return Ok(result);
    }
    for item in &mut fresh {
        item.id = Some(item.insert(ctx.conn)?);
    }
    result.new = fresh.len();
    ctx.emit(&["feed_items"]);
    for item in &fresh {
        let parsed = parse_release_title(&item.title);
        for show_id in matching_show_ids(&set.matchers, &parsed, None) {
            insert_match(ctx, item.id.unwrap_or_default(), show_id)?;
            result.show_ids.insert(show_id);
            result.matched += 1;
            if ctx
                .caches
                .skipped
                .get(&show_id)
                .is_some_and(|feeds| feeds.contains(&feed_id))
            {
                result.reconsider.insert(show_id);
            }
        }
    }
    if result.matched > 0 {
        ctx.emit(&["shows"]);
    }
    Ok(result)
}

fn insert_match(ctx: &Context<'_>, item_id: i64, show_id: i64) -> Result<()> {
    FeedItemMatch { item_id, show_id }.insert(ctx.conn)?;
    Ok(())
}

/// Matches every stored item against the followed shows again; returns the matches made.
///
/// With `show_ids` only those shows' matches are redone, after an alias edit.
pub fn rematch(ctx: &mut Context<'_>, show_ids: Option<&[i64]>) -> Result<usize> {
    let set = matchers(ctx)?;
    match show_ids {
        None => {
            // A completed show is not matched any more, so its Releases tab lives on what it has until `prune` ages that out.
            ctx.conn.execute(
                "DELETE FROM feed_item_matches WHERE show_id NOT IN (SELECT id FROM shows WHERE completed = 1)",
                [],
            )?;
        }
        Some([]) => return Ok(0),
        Some(only) => {
            ctx.conn.execute(
                &format!(
                    "DELETE FROM feed_item_matches WHERE show_id IN ({})",
                    id_list(only)
                ),
                [],
            )?;
        }
    }
    let mut made = 0;
    for (item_id, title) in item_titles(ctx)? {
        let parsed = parse_release_title(&title);
        for show_id in matching_show_ids(&set.matchers, &parsed, show_ids) {
            insert_match(ctx, item_id, show_id)?;
            made += 1;
        }
    }
    if show_ids.is_none() {
        store_signatures(ctx, &set.signatures)?;
    }
    ctx.emit(&["shows"]);
    Ok(made)
}

fn id_list(ids: &[i64]) -> String {
    ids.iter()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ")
}

/// Every stored item's id and title.
fn item_titles(ctx: &Context<'_>) -> Result<Vec<(i64, String)>> {
    let mut statement = ctx
        .conn
        .prepare_cached("SELECT id, title FROM feed_items ORDER BY id")?;
    let titles = statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(titles)
}

/// The signatures as stored: a JSON object by show id.
fn store_signatures(ctx: &Context<'_>, signatures: &BTreeMap<i64, String>) -> Result<()> {
    let body = signatures
        .iter()
        .map(|(id, signature)| format!("\"{id}\": \"{signature}\""))
        .collect::<Vec<_>>()
        .join(", ");
    set_state(ctx.conn, SIGNATURE_KEY, &format!("{{{body}}}"))
}

/// Redoes the matches of the shows that were added, removed, or whose aliases or learned name
/// changed since they were made.
///
/// Only those shows are tried against every stored item; the items they touch are then matched
/// again against every followed show, so a title several shows compete for still goes to the
/// right one. Returns whether anything was redone.
pub fn ensure_matches_current(ctx: &mut Context<'_>) -> Result<bool> {
    let set = matchers(ctx)?;
    let stored = serde_json::from_str::<HashMap<String, Option<String>>>(&get_state(
        ctx.conn,
        SIGNATURE_KEY,
        "",
    )?)
    .ok();
    let Some(stored) = stored else {
        // Nothing to compare with (a new database, or one from before signatures were kept per show).
        rematch(ctx, None)?;
        return Ok(true);
    };
    let stored: HashMap<i64, Option<&str>> = stored
        .iter()
        .filter_map(|(id, value)| Some((id.parse().ok()?, value.as_deref())))
        .collect();
    let changed: BTreeSet<i64> = set
        .signatures
        .iter()
        .filter(|(id, signature)| stored.get(id).copied().flatten() != Some(signature.as_str()))
        .map(|(id, _)| *id)
        .collect();
    let removed: BTreeSet<i64> = stored
        .keys()
        .filter(|id| !set.signatures.contains_key(id))
        .copied()
        .collect();
    if changed.is_empty() && removed.is_empty() {
        return Ok(false);
    }
    rematch_affected(ctx, &set, &changed, &removed)?;
    store_signatures(ctx, &set.signatures)?;
    ctx.emit(&["shows"]);
    Ok(true)
}

/// `rematch` limited to the items the changed or removed shows matched before or match now.
fn rematch_affected(
    ctx: &Context<'_>,
    set: &MatcherSet,
    changed: &BTreeSet<i64>,
    removed: &BTreeSet<i64>,
) -> Result<()> {
    let completed: BTreeSet<i64> = ctx
        .conn
        .prepare_cached("SELECT id FROM shows WHERE completed = 1")?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let touched: Vec<i64> = changed.union(removed).copied().collect();
    let mut affected: BTreeSet<i64> = ctx
        .conn
        .prepare(&format!(
            "SELECT DISTINCT item_id FROM feed_item_matches WHERE show_id IN ({})",
            id_list(&touched)
        ))?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let newcomers: Vec<_> = set
        .matchers
        .iter()
        .filter(|(show_id, _)| changed.contains(show_id))
        .map(|(_, matcher)| matcher)
        .collect();
    let titles: BTreeMap<i64, String> = item_titles(ctx)?.into_iter().collect();
    for (item_id, title) in &titles {
        if !affected.contains(item_id) && !newcomers.is_empty() {
            let parsed = parse_release_title(title);
            if newcomers
                .iter()
                .any(|matcher| broad_match(matcher, &parsed))
            {
                affected.insert(*item_id);
            }
        }
    }
    let gone: Vec<i64> = removed.difference(&completed).copied().collect();
    if !gone.is_empty() {
        // A completed show keeps its matches for its Releases tab, as `rematch` does.
        ctx.conn.execute(
            &format!(
                "DELETE FROM feed_item_matches WHERE show_id IN ({})",
                id_list(&gone)
            ),
            [],
        )?;
    }
    let ordered: Vec<i64> = affected.into_iter().collect();
    for chunk in ordered.chunks(CHUNK) {
        ctx.conn.execute(
            &format!(
                "DELETE FROM feed_item_matches WHERE item_id IN ({}) \
                 AND show_id NOT IN (SELECT id FROM shows WHERE completed = 1)",
                id_list(chunk)
            ),
            [],
        )?;
    }
    for item_id in ordered {
        let Some(title) = titles.get(&item_id) else {
            continue;
        };
        let parsed = parse_release_title(title);
        for show_id in matching_show_ids(&set.matchers, &parsed, None) {
            if !completed.contains(&show_id) {
                insert_match(ctx, item_id, show_id)?;
            }
        }
    }
    Ok(())
}

/// Drops what no longer makes sense to keep; returns how many rows went.
pub fn prune(ctx: &mut Context<'_>) -> Result<usize> {
    let conn = ctx.conn;
    let mut removed = 0;

    // A release published well before its show's first episode aired belongs to an earlier
    // season; nothing can use it. Undated items stay.
    let first_airs: Vec<(i64, jiff::Timestamp)> = conn
        .prepare_cached(
            "SELECT show_id, min(air_at) FROM episodes WHERE air_at IS NOT NULL GROUP BY show_id",
        )?
        .query_map([], |row| {
            Ok((i64::read(row, 0)?, jiff::Timestamp::read(row, 1)?))
        })?
        .collect::<std::result::Result<_, _>>()?;
    for (show_id, first_air) in first_airs {
        let cutoff = first_air
            .saturating_sub(PREMIERE_SLACK)
            .unwrap_or(first_air);
        removed += conn.execute(
            "DELETE FROM feed_item_matches WHERE show_id = ? AND item_id IN \
             (SELECT id FROM feed_items WHERE published_at IS NOT NULL AND published_at < ?)",
            (show_id, format_timestamp(cutoff)),
        )?;
    }

    // A show that has not started airing has no releases of its own.
    let shows = Show::select(conn, "WHERE completed = 0 ORDER BY id", [])?;
    let facts = facts_for(ctx, &shows)?;
    let not_started: Vec<i64> = facts
        .iter()
        .filter(|facts| premiere_out_of_reach(facts, ctx.now))
        .map(|facts| facts.id)
        .collect();
    if !not_started.is_empty() {
        removed += conn.execute(
            &format!(
                "DELETE FROM feed_item_matches WHERE show_id IN ({})",
                id_list(&not_started)
            ),
            [],
        )?;
    }

    let completed_cutoff = ctx.now.saturating_sub(COMPLETED_KEEP).unwrap_or(ctx.now);
    removed += conn.execute(
        "DELETE FROM feed_item_matches WHERE show_id IN (SELECT id FROM shows WHERE completed = 1) \
         AND item_id IN (SELECT id FROM feed_items WHERE coalesce(published_at, first_seen_at) < ?)",
        [format_timestamp(completed_cutoff)],
    )?;

    let counts: Vec<(i64, i64)> = conn
        .prepare_cached("SELECT show_id, count(*) FROM feed_item_matches GROUP BY show_id")?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<std::result::Result<_, _>>()?;
    for show_id in counts
        .into_iter()
        .filter(|(_, count)| *count > PER_SHOW_CAP)
        .map(|(show_id, _)| show_id)
    {
        removed += conn.execute(
            "DELETE FROM feed_item_matches WHERE show_id = ?1 AND item_id NOT IN \
             (SELECT feed_items.id FROM feed_items \
              JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
              WHERE feed_item_matches.show_id = ?1 \
              ORDER BY feed_items.published_at DESC, feed_items.first_seen_at DESC LIMIT ?2)",
            (show_id, PER_SHOW_CAP),
        )?;
    }

    let unmatched_cutoff =
        format_timestamp(ctx.now.saturating_sub(UNMATCHED_KEEP).unwrap_or(ctx.now));
    let gone: Vec<(i64, String)> = conn
        .prepare_cached(
            "SELECT feed_id, guid FROM feed_items \
             WHERE id NOT IN (SELECT item_id FROM feed_item_matches) AND first_seen_at < ?",
        )?
        .query_map([&unmatched_cutoff], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<std::result::Result<_, _>>()?;
    removed += conn.execute(
        "DELETE FROM feed_items \
         WHERE id NOT IN (SELECT item_id FROM feed_item_matches) AND first_seen_at < ?",
        [&unmatched_cutoff],
    )?;
    // A pruned item looks new again when the feed still lists it, which re-adds it and its matches.
    if let Some(known) = ctx.caches.known_items.as_mut() {
        for key in &gone {
            known.remove(key);
        }
    }
    Ok(removed)
}
