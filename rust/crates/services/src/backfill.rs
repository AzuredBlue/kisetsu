//! Searching the feeds' sites for past episodes that scrolled out of the RSS ("Search older").

use std::collections::{BTreeSet, HashMap, HashSet};
use std::hash::{DefaultHasher, Hash, Hasher};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::sites::{self, Site};
use kisetsu_db::rusqlite::Params;
use kisetsu_db::schema::{Episode, Feed, Show};
use kisetsu_db::storage::format_timestamp;
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::matcher::broad_match;
use kisetsu_domain::parsing::parse_release_title;
use kisetsu_domain::schedule::{AIR_DATE_TOLERANCE, PUBLISHED_BEFORE_AIR_SLACK, WEEK};
use tracing::warn;

use crate::context::Context;
use crate::downloads;
use crate::error::Result;
use crate::facts::facts_for;
use crate::feeds;
use crate::grab;
use crate::ingest;

/// The same search is not repeated for the same missing episodes within this time,
const REPEAT: SignedDuration = SignedDuration::from_hours(6);
/// which doubles, up to this, for each search in a row that found nothing (a show no feed
/// carries).
const REPEAT_MAX: SignedDuration = SignedDuration::from_hours(24);

/// A show with episodes to search for, and what the search is made of.
#[derive(Debug, Clone, PartialEq)]
pub struct Due {
    pub show_id: i64,
    pub name: String,
    pub terms: Vec<String>,
    pub feeds: Vec<Feed>,
    pub missing: Vec<i64>,
    /// What was asked: the terms, the feeds and the episodes.
    pub signature: u64,
    pub first_air: Option<Timestamp>,
    /// What maps a feed's episode numbers onto the show's, as the grab does.
    pub air_times: Vec<(i64, Timestamp)>,
    pub target_count: Option<i64>,
    pub latest_aired: Option<i64>,
    pub tolerance_hours: i64,
    /// The name SubsPlease's schedule gives the show, searched for first on SubsPlease.
    pub subsplease_name: Option<String>,
}

/// Which shows `due` looks at, and how hard.
#[derive(Debug, Clone, Copy, Default)]
pub struct DueOptions {
    /// Only this show, which also covers a paused one, and with `unmanaged` a finished one.
    pub show_id: Option<i64>,
    /// Ignore that the same search ran lately.
    pub force: bool,
    /// Also count finished episodes Kisetsu holds no torrent for.
    pub unmanaged: bool,
    /// A search the user asked for: feeds that seem not to carry the show are asked too.
    pub everywhere: bool,
}

fn hash_of(terms: &[String], feeds: &[Feed], missing: &[i64]) -> u64 {
    let mut hasher = DefaultHasher::new();
    terms.hash(&mut hasher);
    feeds
        .iter()
        .map(|feed| feed.id)
        .collect::<Vec<_>>()
        .hash(&mut hasher);
    missing.hash(&mut hasher);
    hasher.finish()
}

/// The feeds a show's search may ask.
fn searchable_feeds(ctx: &Context<'_>, show: &Show) -> Result<Vec<Feed>> {
    let mut all_feeds = feeds::ranked(ctx)?;
    if show.feed_is_fixed() {
        // A locked show reads one feed; searching others would only fill the store with releases
        // it may not use.
        all_feeds.retain(|feed| feed.id == show.feed_id);
        all_feeds.truncate(1);
    }
    all_feeds.retain(|feed| sites::site_of(&feed.url).is_some());
    Ok(all_feeds)
}

/// The feeds that do not carry the show, so a search of them would find nothing.
///
/// A feed that ever listed the show, or on which a search found it, carries it. Otherwise:
///
/// - A feed lists its newest releases. When its last read reached back past one of the show's
///   aired episodes, far enough that a release of it would be listed even if posted early, and
///   was read late enough that it would have been posted, the feed lacks the show.
/// - A SubsPlease feed lacks an airing show SubsPlease's own schedule leaves out
///   (`note_schedule`).
fn lacking(ctx: &Context<'_>, show: &Show, show_feeds: &[Feed]) -> Result<HashSet<i64>> {
    let windows: HashMap<i64, (Timestamp, Timestamp)> = show_feeds
        .iter()
        .filter_map(|feed| {
            let id = feed.id?;
            Some((id, *ctx.caches.read_windows.get(&id)?))
        })
        .collect();
    let off_schedule = show
        .id
        .is_some_and(|id| ctx.caches.off_schedule.contains(&id));
    if windows.is_empty() && !off_schedule {
        return Ok(HashSet::new());
    }
    let carrying: HashSet<i64> = ctx
        .conn
        .prepare_cached(
            "SELECT DISTINCT feed_items.feed_id FROM feed_items \
             JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
             WHERE feed_item_matches.show_id = ?",
        )?
        .query_map([show.id], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let mut lacking: HashSet<i64> = show_feeds
        .iter()
        .filter(|feed| {
            off_schedule
                && sites::site_of(&feed.url) == Some(Site::SubsPlease)
                && feed.id.is_some_and(|id| !carrying.contains(&id))
        })
        .filter_map(|feed| feed.id)
        .collect();
    let aired: Vec<Timestamp> = if windows.is_empty() {
        Vec::new()
    } else {
        Episode::select(
            ctx.conn,
            "WHERE show_id = ? AND air_at IS NOT NULL AND air_at <= ?",
            (show.id, format_timestamp(ctx.now)),
        )?
        .into_iter()
        .filter_map(|episode| episode.air_at)
        .collect()
    };
    lacking.extend(
        windows
            .iter()
            .filter(|(feed_id, _)| !carrying.contains(feed_id))
            .filter(|(_, (oldest, read_at))| {
                aired.iter().any(|air| {
                    oldest
                        .saturating_add(PUBLISHED_BEFORE_AIR_SLACK)
                        .is_ok_and(|from| from <= *air)
                        && air
                            .saturating_add(AIR_DATE_TOLERANCE)
                            .is_ok_and(|to| to <= *read_at)
                })
            })
            .map(|(feed_id, _)| *feed_id),
    );
    Ok(lacking)
}

fn subsplease_feeds(ctx: &Context<'_>) -> Result<Vec<i64>> {
    Ok(Feed::select(ctx.conn, "ORDER BY id", [])?
        .into_iter()
        .filter(|feed| sites::site_of(&feed.url) == Some(Site::SubsPlease))
        .filter_map(|feed| feed.id)
        .collect())
}

/// The show ids a query selects.
fn show_ids(ctx: &Context<'_>, sql: &str, params: impl Params) -> Result<HashSet<i64>> {
    Ok(ctx
        .conn
        .prepare_cached(sql)?
        .query_map(params, |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?)
}

/// Takes SubsPlease's schedule in: which airing shows it leaves out, so its feeds are not
/// searched for them.
///
/// Only a show airing week by week now (an episode in the last week and one still to come) is
/// judged, as that is all the schedule lists. It is matched as a release named after the
/// schedule's title would be. The schedule is not assumed complete: when it leaves out a show
/// airing now that a SubsPlease feed carries, it is not used at all.
pub fn note_schedule(ctx: &mut Context<'_>, titles: Option<&[String]>) -> Result<()> {
    ctx.caches.off_schedule.clear();
    ctx.caches.schedule_names.clear();
    let feeds_on = subsplease_feeds(ctx)?;
    let Some(titles) = titles.filter(|titles| !titles.is_empty()) else {
        return Ok(());
    };
    if feeds_on.is_empty() {
        return Ok(());
    }
    let week_ago = ctx.now.saturating_sub(WEEK).unwrap_or(ctx.now);
    let mut airing_ids = show_ids(
        ctx,
        "SELECT show_id FROM episodes WHERE air_at > ? AND air_at <= ?",
        (format_timestamp(week_ago), format_timestamp(ctx.now)),
    )?;
    let upcoming = show_ids(
        ctx,
        "SELECT show_id FROM episodes WHERE air_at > ?",
        [format_timestamp(ctx.now)],
    )?;
    airing_ids.retain(|id| upcoming.contains(id));
    let set = ingest::matchers(ctx)?;
    for (show_id, matcher) in &set.matchers {
        let named = titles.iter().find(|title| {
            let release = parse_release_title(&format!("[SubsPlease] {title} - 01 (1080p).mkv"));
            broad_match(matcher, &release)
        });
        if let Some(named) = named {
            ctx.caches.schedule_names.insert(*show_id, named.clone());
        }
    }
    let off: HashSet<i64> = set
        .matchers
        .iter()
        .map(|(show_id, _)| *show_id)
        .filter(|id| airing_ids.contains(id) && !ctx.caches.schedule_names.contains_key(id))
        .collect();
    let feed_list = feeds_on
        .iter()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    let carried = show_ids(
        ctx,
        &format!(
            "SELECT feed_item_matches.show_id FROM feed_item_matches \
             JOIN feed_items ON feed_items.id = feed_item_matches.item_id \
             WHERE feed_items.feed_id IN ({feed_list})"
        ),
        [],
    )?;
    let both: BTreeSet<i64> = off.intersection(&carried).copied().collect();
    if !both.is_empty() {
        let mut names = Vec::new();
        for id in &both {
            if let Some(show) = Show::find(ctx.conn, "WHERE id = ?", [id])? {
                names.push(show.title(&ctx.settings.title_language));
            }
        }
        names.sort();
        warn!(
            "SubsPlease's schedule leaves out {}, which its feed carries, so it is not used to skip searches.",
            names.join(", ")
        );
        return Ok(());
    }
    ctx.caches.off_schedule = off;
    Ok(())
}

/// Shows with an aired episode that is still wanted and that no stored feed item can supply.
///
/// Searching is only for what the feeds have lost, and only on feeds that may carry the show
/// (`lacking`), unless `everywhere` (a search the user asked for) or `unmanaged`, which also
/// counts finished episodes Kisetsu holds no torrent for. `unsearchable`, when given, collects
/// the ids of shows skipped because no feed can be searched or the show has no usable search term.
pub fn due(
    ctx: &mut Context<'_>,
    options: DueOptions,
    mut unsearchable: Option<&mut Vec<i64>>,
) -> Result<Vec<Due>> {
    let shows = match options.show_id {
        Some(id) => Show::select(ctx.conn, "WHERE id = ?", [id])?,
        None => Show::select(
            ctx.conn,
            "WHERE completed = 0 AND paused = 0 ORDER BY id",
            [],
        )?,
    };
    let mut result = Vec::new();
    for show in shows {
        let wanted = outstanding(ctx, &show, options.unmanaged)?;
        if wanted.is_empty() {
            continue;
        }
        let Some(show_id) = show.id else {
            continue;
        };
        // The names that found releases before come first, so the few terms searched include them.
        let custom = show.custom_aliases();
        let aliases = show.aliases();
        let titles = std::iter::once(show.learned_name.as_deref())
            .chain(custom.iter().map(|alias| Some(alias.as_str())))
            .chain([show.title_romaji.as_deref(), show.title_english.as_deref()])
            .chain(aliases.iter().map(|alias| Some(alias.as_str())));
        let terms = sites::search_terms(titles);
        let mut show_feeds = searchable_feeds(ctx, &show)?;
        if terms.is_empty() || show_feeds.is_empty() {
            if let Some(unsearchable) = unsearchable.as_deref_mut() {
                unsearchable.push(show_id);
            }
            continue;
        }
        let covered = grab::covered_numbers(ctx, &show)?;
        let missing: Vec<i64> = wanted
            .into_iter()
            .filter(|number| !covered.contains(number))
            .collect();
        if missing.is_empty() {
            continue;
        }
        if !(options.unmanaged || options.everywhere) {
            // Saved files may come from a feed that has since stopped carrying the show, so they
            // are looked for everywhere.
            let skipped = lacking(ctx, &show, &show_feeds)?;
            show_feeds.retain(|feed| feed.id.is_some_and(|id| !skipped.contains(&id)));
            // A release from one of them later has the show searched again
            // (`ingest::IngestResult::reconsider`).
            if skipped.is_empty() {
                ctx.caches.skipped.remove(&show_id);
            } else {
                ctx.caches.skipped.insert(show_id, skipped);
            }
            if show_feeds.is_empty() {
                continue;
            }
        }
        let signature = hash_of(&terms, &show_feeds, &missing);
        let last = ctx.caches.searched.get(&show_id);
        if !options.force {
            if let Some(last) = last {
                if last.signature == signature
                    && ctx.now.duration_since(last.at) < repeat(last.fruitless)
                {
                    continue;
                }
            } else if show
                .last_backfill_at
                .is_some_and(|at| ctx.now.duration_since(at) < REPEAT)
            {
                // Searched before a restart, which forgot what was searched for: the stored time
                // stands in for it.
                continue;
            }
        }
        let facts = facts_for(ctx, std::slice::from_ref(&show))?;
        let Some(facts) = facts.first() else {
            continue;
        };
        let state = grab::show_state(ctx, &show, facts)?;
        result.push(Due {
            show_id,
            name: show.title(&ctx.settings.title_language),
            terms,
            feeds: show_feeds,
            missing,
            signature,
            first_air: state.first_air,
            air_times: state.air_times,
            target_count: Some(state.target_count),
            latest_aired: state.newest_aired,
            tolerance_hours: ctx.tolerance_hours(),
            subsplease_name: ctx.caches.schedule_names.get(&show_id).cloned(),
        });
    }
    Ok(result)
}

/// How long the same search waits: longer after each one in a row that found nothing.
fn repeat(fruitless: u32) -> SignedDuration {
    let doublings = fruitless.saturating_sub(1).min(16);
    REPEAT
        .checked_mul(1_i32 << doublings)
        .map_or(REPEAT_MAX, |wait| wait.min(REPEAT_MAX))
}

fn outstanding(ctx: &Context<'_>, show: &Show, unmanaged: bool) -> Result<Vec<i64>> {
    let rows = Episode::select(
        ctx.conn,
        "WHERE show_id = ? AND air_at IS NOT NULL AND air_at <= ? ORDER BY number",
        (show.id, format_timestamp(ctx.now)),
    )?;
    let mut numbers = Vec::new();
    for episode in rows {
        match episode.status {
            EpisodeStatus::Wanted | EpisodeStatus::Missed => numbers.push(episode.number),
            EpisodeStatus::Completed if unmanaged => {
                let cur = downloads::current(ctx, &episode)?;
                if cur.is_none_or(|cur| cur.info_hash.is_none()) {
                    numbers.push(episode.number);
                }
            }
            _ => {}
        }
    }
    Ok(numbers)
}

/// Whether the show's aired episodes were searched for, or no search has anything left to look
/// for.
///
/// A show without air dates has not been searched, however empty the search looks.
pub fn search_completed(ctx: &mut Context<'_>, show_id: i64) -> Result<bool> {
    let dated = ctx
        .conn
        .prepare_cached("SELECT 1 FROM episodes WHERE show_id = ? AND air_at IS NOT NULL")?
        .exists([show_id])?;
    if !dated {
        return Ok(false);
    }
    if ctx.caches.searched.contains_key(&show_id) {
        return Ok(true);
    }
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        ..DueOptions::default()
    };
    Ok(due(ctx, options, None)?.is_empty())
}

/// Whether a search of the feeds' sites for these episodes is still to run, so they may yet turn
/// up.
pub fn search_pending(ctx: &mut Context<'_>, show_id: i64, numbers: &[i64]) -> Result<bool> {
    if let Some(last) = ctx.caches.searched.get(&show_id) {
        if numbers.iter().all(|number| last.episodes.contains(number)) {
            return Ok(false);
        }
    }
    let options = DueOptions {
        show_id: Some(show_id),
        ..DueOptions::default()
    };
    Ok(!due(ctx, options, None)?.is_empty())
}

/// The episodes of one show that a search the user asks for would look for (empty: nothing is
/// missing).
pub fn missing_episodes(ctx: &mut Context<'_>, show_id: i64, unmanaged: bool) -> Result<Vec<i64>> {
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        unmanaged,
        everywhere: true,
    };
    Ok(due(ctx, options, None)?
        .into_iter()
        .next()
        .map(|item| item.missing)
        .unwrap_or_default())
}

/// Whether the show has episodes to look for but no feed or search term to look with.
pub fn search_unavailable(ctx: &mut Context<'_>, show_id: i64, unmanaged: bool) -> Result<bool> {
    let mut skipped = Vec::new();
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        unmanaged,
        everywhere: false,
    };
    due(ctx, options, Some(&mut skipped))?;
    Ok(skipped.contains(&show_id))
}
