//! Taking over torrents that are already in qBittorrent, and locking a show's feed from what it
//! already has.

use std::collections::hash_map::Entry;
use std::collections::{BTreeSet, HashMap, HashSet};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::qbittorrent::Torrent;
use kisetsu_db::schema::{
    ACTIVE_DOWNLOAD_STATES, Download, DownloadState, Episode, EpisodeOffset, EventKind, Feed,
    FeedItem, FeedState, Show,
};
use kisetsu_domain::matcher::{Matcher, decide, most_plainly_named};
use kisetsu_domain::parsing::{
    ParsedRelease, names_something_else, parse_release_title, release_key,
};
use kisetsu_domain::schedule::{PUBLISHED_BEFORE_AIR_SLACK, air_horizon, latest_aired};
use tracing::debug;

use crate::context::{Context, Unnamed};
use crate::downloads::{MANAGED_TAG, recompute_episode, state_list};
use crate::error::Result;
use crate::events::{self, Subject};
use crate::facts::facts_for;
use crate::feeds::{self, Evidence};
use crate::{ingest, library, tags};

/// The refused torrents remembered at most; the memory is dropped once it grows past this.
const REFUSED_LIMIT: usize = 5000;
/// Hashes looked up in one statement, well under SQLite's variable limit.
const CHUNK: usize = 500;

/// What adopting did.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AdoptResult {
    pub imported: usize,
    pub refused: usize,
}

/// Why an existing torrent cannot be taken as the episode's release.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    #[error("the episode has not aired yet")]
    NotAired,
    #[error("it was added before the episode aired")]
    AddedBeforeAir,
}

/// Torrents with the Kisetsu tag or in the user's category; torrents with neither are not ours.
pub fn managed_torrents(ctx: &mut Context<'_>) -> Result<Vec<Torrent>> {
    let category = ctx.settings.default_category.clone();
    Ok(ctx
        .qbit()?
        .torrents()
        .values()
        .filter(|torrent| {
            torrent.tags.contains(MANAGED_TAG)
                || (!category.is_empty() && torrent.category == category)
        })
        .cloned()
        .collect())
}

/// Whether qBittorrent added the torrent well before `reference`, so it cannot be a release of
/// what aired then.
fn added_before_air(
    added_on: Option<Timestamp>,
    reference: Option<Timestamp>,
    tolerance_hours: i64,
) -> bool {
    let (Some(added_on), Some(reference)) = (added_on, reference) else {
        return false;
    };
    let slack = PUBLISHED_BEFORE_AIR_SLACK.max(SignedDuration::from_hours(tolerance_hours));
    reference
        .checked_sub(slack)
        .is_ok_and(|limit| added_on < limit)
}

/// Why an existing torrent cannot be taken as the episode's release, or `None` when it can.
///
/// A release name does not tell one season of a show from the next, so adoption goes by dates:
/// an episode that has not aired has no release yet, and a torrent that qBittorrent added well
/// before the episode could have aired is from an earlier run.
pub fn adoption_refusal(
    ctx: &mut Context<'_>,
    show: &Show,
    episode: &Episode,
    torrent: &Torrent,
    newest_aired: Option<i64>,
    first_air: Option<Timestamp>,
) -> Option<Refusal> {
    let reason = if newest_aired.is_none_or(|newest| episode.number > newest) {
        Some(Refusal::NotAired)
    } else if added_before_air(
        torrent.added_on,
        episode.air_at.or(first_air),
        ctx.tolerance_hours(),
    ) {
        Some(Refusal::AddedBeforeAir)
    } else {
        None
    };
    if let Some(reason) = reason {
        if !ctx.caches.refused.contains(&torrent.hash) {
            if ctx.caches.refused.len() > REFUSED_LIMIT {
                ctx.caches.refused.clear();
            }
            ctx.caches.refused.insert(torrent.hash.clone());
            debug!(
                "Not adopting '{}' as Ep {} of '{}': {reason}.",
                torrent.name,
                episode.number,
                show.title("english")
            );
        }
    }
    reason
}

/// The one feed whose stored items carry this release, and its item; a release on several feeds
/// proves none, unless the user pinned one of them.
pub fn carriers(
    ctx: &Context<'_>,
    show: &Show,
    name: &str,
    info_hash: &str,
) -> Result<Option<(Feed, FeedItem)>> {
    let rows = FeedItem::select(
        ctx.conn,
        "JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
         WHERE feed_item_matches.show_id = ? ORDER BY feed_items.id",
        [show.id],
    )?;
    let by_hash: Vec<&FeedItem> = rows
        .iter()
        .filter(|row| !info_hash.is_empty() && row.info_hash.as_deref() == Some(info_hash))
        .collect();
    let found = if by_hash.is_empty() {
        let key = release_key(name);
        let same = |row: &&FeedItem| !key.is_empty() && release_key(&row.title) == key;
        let mut found: Vec<&FeedItem> = rows.iter().filter(same).collect();
        if found.is_empty() && !key.is_empty() {
            // The feed title may extend the torrent's name ("... (Ao no Hako Season 2, ...)").
            found = rows
                .iter()
                .filter(|row| {
                    let title = release_key(&row.title);
                    title.starts_with(&format!("{key} ")) || key.starts_with(&format!("{title} "))
                })
                .collect();
        }
        found
    } else {
        by_hash
    };
    let mut feed_ids: BTreeSet<i64> = found.iter().map(|row| row.feed_id).collect();
    if feed_ids.len() > 1
        && show.feed_is_fixed()
        && show.feed_id.is_some_and(|id| feed_ids.contains(&id))
    {
        feed_ids = show.feed_id.into_iter().collect();
    }
    let mut ids = feed_ids.into_iter();
    let (Some(feed_id), None) = (ids.next(), ids.next()) else {
        return Ok(None);
    };
    let item = found.iter().find(|row| row.feed_id == feed_id);
    let feed = Feed::find(ctx.conn, "WHERE id = ?", [feed_id])?;
    Ok(feed.zip(item.map(|item| (*item).clone())))
}

/// The feed the newest saved file came from, and what its release is called there.
#[derive(Debug, Clone, PartialEq)]
pub struct SavedFilesFeed {
    pub feed: Feed,
    pub release_group: Option<String>,
    pub title: Option<String>,
}

/// The feed the newest saved file came from, if exactly one feed carries it.
///
/// A file several feeds carry proves none of them. When the files came from different feeds, the
/// newest episode is the latest choice.
pub fn feed_from_saved_files(
    ctx: &Context<'_>,
    show: &Show,
    names: &[String],
    excluded: &[i64],
) -> Result<Option<SavedFilesFeed>> {
    let mut best: Option<(i64, SavedFilesFeed)> = None;
    for name in names {
        let Some((feed, _)) = carriers(ctx, show, name, "")? else {
            continue;
        };
        if feed.id.is_some_and(|id| excluded.contains(&id)) {
            continue;
        }
        let parsed = parse_release_title(name);
        let episode = parsed.episode.unwrap_or(0);
        if best.as_ref().is_none_or(|(newest, _)| episode >= *newest) {
            best = Some((
                episode,
                SavedFilesFeed {
                    feed,
                    release_group: parsed.release_group,
                    title: Some(parsed.title),
                },
            ));
        }
    }
    Ok(best.map(|(_, found)| found))
}

fn known_hashes(ctx: &Context<'_>) -> Result<HashSet<String>> {
    // A release a replacement superseded is accounted for: adopting it again would roll the
    // episode back.
    let hashes = ctx
        .conn
        .prepare_cached("SELECT info_hash FROM downloads WHERE info_hash IS NOT NULL")?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(hashes)
}

/// The show a stored feed item with this info-hash was matched to; a hash on items of several
/// shows names none.
fn hash_owners(
    ctx: &Context<'_>,
    hashes: &[&str],
    show_ids: &HashSet<i64>,
) -> Result<HashMap<String, i64>> {
    let mut owners: HashMap<String, BTreeSet<i64>> = HashMap::new();
    for chunk in hashes.chunks(CHUNK) {
        let marks = vec!["?"; chunk.len()].join(", ");
        let mut statement = ctx.conn.prepare(&format!(
            "SELECT feed_items.info_hash, feed_item_matches.show_id FROM feed_items \
             JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
             WHERE feed_items.info_hash IN ({marks})"
        ))?;
        let rows = statement.query_map(
            kisetsu_db::rusqlite::params_from_iter(chunk.iter()),
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
        )?;
        for row in rows {
            let (info_hash, show_id) = row?;
            if show_ids.contains(&show_id) {
                owners.entry(info_hash).or_default().insert(show_id);
            }
        }
    }
    Ok(owners
        .into_iter()
        .filter_map(
            |(hash, ids)| match ids.into_iter().collect::<Vec<_>>()[..] {
                [only] => Some((hash, only)),
                _ => None,
            },
        )
        .collect())
}

/// A followed show's episodes, with how far it has aired.
struct ShowState {
    episodes: Vec<Episode>,
    newest_aired: Option<i64>,
    first_air: Option<Timestamp>,
}

/// Fills gaps with the torrents already in qBittorrent; a tracked release is never overwritten.
pub fn adopt_existing(ctx: &mut Context<'_>) -> Result<AdoptResult> {
    let mut result = AdoptResult::default();
    let torrents = managed_torrents(ctx)?;
    if torrents.is_empty() {
        return Ok(result);
    }
    let mut known = known_hashes(ctx)?;
    let pending: Vec<&Torrent> = torrents
        .iter()
        .filter(|torrent| !known.contains(&torrent.hash) && !torrent.name.is_empty())
        .collect();
    if pending.is_empty() {
        return Ok(result);
    }
    let mut shows: Vec<Show> = ingest::followed_shows(ctx)?
        .into_iter()
        .filter(|show| !show.completed)
        .collect();
    let facts = facts_for(ctx, &shows)?;
    let by_id: HashMap<i64, usize> = shows
        .iter()
        .enumerate()
        .filter_map(|(index, show)| Some((show.id?, index)))
        .collect();
    let set = ingest::matchers(ctx)?;
    let matchers: Vec<(usize, &Matcher)> = set
        .matchers
        .iter()
        .filter_map(|(show_id, matcher)| Some((*by_id.get(show_id)?, matcher)))
        .collect();
    if ctx
        .caches
        .unnamed
        .as_ref()
        .is_none_or(|unnamed| unnamed.built_from != set.signatures)
    {
        ctx.caches.unnamed = Some(Unnamed {
            built_from: set.signatures.clone(),
            torrents: HashSet::new(),
        });
    }
    let ids: HashSet<i64> = by_id.keys().copied().collect();
    let hashes: Vec<&str> = pending
        .iter()
        .map(|torrent| torrent.hash.as_str())
        .collect();
    let carried = hash_owners(ctx, &hashes, &ids)?;
    let tagged_shows = tags::owners(ctx)?;
    let retired_tags: HashSet<String> = tags::retired(ctx)?.into_iter().collect();
    let mut states: HashMap<usize, ShowState> = HashMap::new();
    for torrent in pending {
        let parsed = parse_release_title(&torrent.name);
        let Some(raw) = parsed.episode else {
            continue;
        };
        // A series tag says which show the torrent belongs to; a show that is not followed now
        // leaves it alone.
        let tagged = tags::owner_id(torrent, &tagged_shows);
        let chosen = if let Some(owner) = tagged {
            by_id.get(&owner).copied()
        } else if torrent.tags.iter().any(|tag| retired_tags.contains(tag)) {
            None
        } else {
            let mut chosen = carried
                .get(&torrent.hash)
                .and_then(|owner| by_id.get(owner))
                .copied();
            let attempt = (torrent.hash.clone(), torrent.name.clone());
            let tried = ctx
                .caches
                .unnamed
                .as_ref()
                .is_some_and(|unnamed| unnamed.torrents.contains(&attempt));
            if chosen.is_none() && !tried {
                chosen = named_show(&matchers, &parsed);
                if chosen.is_none() {
                    if let Some(unnamed) = ctx.caches.unnamed.as_mut() {
                        unnamed.torrents.insert(attempt);
                    }
                }
            }
            chosen
        };
        let Some(at) = chosen else {
            continue;
        };
        // A series tag is proof. Without one the name decides, and it may name a movie or
        // another show that holds this one's name.
        if tagged.is_none() {
            let show = &shows[at];
            let mut names = show.effective_aliases();
            names.extend(show.learned_name.clone().filter(|name| !name.is_empty()));
            if names_something_else(&parsed, &names) {
                continue;
            }
        }
        let show_id = shows[at].id.unwrap_or_default();
        let state = match states.entry(at) {
            Entry::Occupied(known) => known.into_mut(),
            Entry::Vacant(vacant) => {
                let Some(facts) = facts.iter().find(|facts| facts.id == show_id) else {
                    continue;
                };
                let episodes =
                    Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY id", [show_id])?;
                let horizon = air_horizon(facts, ctx.now, ctx.tolerance_hours());
                let first_air = episodes.iter().filter_map(|episode| episode.air_at).min();
                vacant.insert(ShowState {
                    episodes,
                    newest_aired: latest_aired(facts, horizon),
                    first_air,
                })
            }
        };
        // A feed that numbers episodes from the series' start (week 52 of a long run) is mapped
        // by its known offset.
        let offset = match carriers(ctx, &shows[at], &torrent.name, &torrent.hash)? {
            Some((feed, _)) => offset_of(ctx, &shows[at], &feed)?,
            None => 0,
        };
        let Some(index) = state
            .episodes
            .iter()
            .position(|episode| episode.number == raw - offset)
        else {
            continue;
        };
        if !is_gap(ctx, &state.episodes[index])? {
            continue;
        }
        let refusal = adoption_refusal(
            ctx,
            &shows[at],
            &state.episodes[index],
            torrent,
            state.newest_aired,
            state.first_air,
        );
        if refusal.is_some() {
            result.refused += 1;
            continue;
        }
        adopt(
            ctx,
            &mut shows[at],
            &mut state.episodes[index],
            torrent,
            &parsed,
            raw,
        )?;
        known.insert(torrent.hash.clone());
        result.imported += 1;
    }
    if result.imported > 0 {
        ctx.emit(&["shows", "history"]);
    }
    Ok(result)
}

/// The show (by position) a torrent's name is for. Several seasons of a series can all match
/// it; it belongs to the one it names.
fn named_show(matchers: &[(usize, &Matcher)], parsed: &ParsedRelease) -> Option<usize> {
    let matched: Vec<(i64, &Matcher)> = matchers
        .iter()
        .filter(|(_, matcher)| decide(matcher, parsed, false).0)
        .filter_map(|(at, matcher)| Some((i64::try_from(*at).ok()?, *matcher)))
        .collect();
    most_plainly_named(matched, parsed)
        .first()
        .and_then(|(at, _)| usize::try_from(*at).ok())
}

fn offset_of(ctx: &Context<'_>, show: &Show, feed: &Feed) -> Result<i64> {
    let row = EpisodeOffset::find(
        ctx.conn,
        "WHERE show_id = ? AND feed_id = ?",
        (show.id, feed.id),
    )?;
    Ok(row.map_or(0, |row| row.offset))
}

/// Whether the episode has no tracked torrent: only gaps are filled.
fn is_gap(ctx: &Context<'_>, episode: &Episode) -> Result<bool> {
    let active = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT 1 FROM downloads WHERE episode_id = ? AND state IN ({}) LIMIT 1",
            state_list(&ACTIVE_DOWNLOAD_STATES)
        ))?
        .exists([episode.id])?;
    if active {
        return Ok(false);
    }
    let current = match episode.current_download_id {
        Some(id) => Download::find(ctx.conn, "WHERE id = ?", [id])?,
        None => None,
    };
    Ok(current.is_none_or(|current| current.info_hash.is_none()))
}

fn adopt(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    torrent: &Torrent,
    parsed: &ParsedRelease,
    raw: i64,
) -> Result<()> {
    let found = carriers(ctx, show, &torrent.name, &torrent.hash)?;
    let (feed, item) = match found {
        Some((feed, item)) => (Some(feed), Some(item)),
        None => (None, None),
    };
    let finished = torrent.finished();
    let mut dl = Download {
        id: None,
        episode_id: episode.id.unwrap_or_default(),
        feed_id: feed.as_ref().and_then(|feed| feed.id),
        feed_item_guid: item.as_ref().map(|item| item.guid.clone()),
        title: torrent.name.clone(),
        parsed_title: match &item {
            Some(item) => item.parsed_title.clone(),
            None => Some(parsed.title.clone()),
        },
        release_group: parsed.release_group.clone(),
        version: parsed.version.max(1),
        source_episode: Some(raw),
        torrent_url: item.as_ref().and_then(|item| item.torrent_url.clone()),
        info_hash: Some(torrent.hash.clone()),
        tag: None,
        state: if finished {
            DownloadState::Done
        } else {
            DownloadState::Downloading
        },
        supersedes_id: None,
        adopted: true,
        batch: false,
        attempts: 1,
        next_retry_at: None,
        last_error: None,
        end_reason: None,
        created_at: ctx.now,
        updated_at: ctx.now,
        completed_at: finished.then_some(ctx.now),
    };
    dl.id = Some(dl.insert(ctx.conn)?);
    if let Some(id) = episode.current_download_id {
        if let Some(mut previous) = Download::find(ctx.conn, "WHERE id = ?", [id])? {
            previous.state = DownloadState::Superseded;
            previous.update(ctx.conn)?;
        }
    }
    episode.current_download_id = dl.id;
    recompute_episode(ctx, episode)?;
    events::record(
        ctx,
        EventKind::Adopted,
        &format!("Found in qBittorrent: {}", torrent.name),
        Subject {
            show_id: show.id,
            episode: Some(episode.number),
            feed_id: dl.feed_id,
            title: &torrent.name,
        },
    )?;
    let tag = tags::series_tag(ctx, show)?;
    if let Err(error) = ctx
        .qbit()?
        .add_tags(std::slice::from_ref(&torrent.hash), &[tag])
    {
        debug!("Could not tag '{}' with its show: {error}", torrent.name);
    }
    // Only a feed that actually carries this release is recorded; an assigned feed is a guess
    // and never the lock.
    if let Some(feed) = feed.filter(|_| show.feed_state != FeedState::Locked) {
        feeds::lock(
            ctx,
            show,
            &feed,
            Evidence::AdoptedUnique,
            item.as_ref().and_then(|item| item.parsed_title.as_deref()),
            item.as_ref().and_then(|item| item.release_group.as_deref()),
        )?;
    }
    Ok(())
}

/// The ids of the feeds this show's downloads failed from for good.
pub fn failed_feed_ids(ctx: &Context<'_>, show: &Show) -> Result<Vec<i64>> {
    let ids = ctx
        .conn
        .prepare_cached(
            "SELECT DISTINCT downloads.feed_id FROM downloads \
             JOIN episodes ON episodes.id = downloads.episode_id \
             WHERE episodes.show_id = ? AND downloads.state = 'FAILED' \
             AND downloads.info_hash IS NULL AND downloads.feed_id IS NOT NULL \
             ORDER BY downloads.feed_id",
        )?
        .query_map([show.id], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(ids)
}

/// The shows still open to a feed, with the names of their saved release files.
fn saved_file_candidates(ctx: &Context<'_>) -> Result<Vec<(Show, Vec<String>)>> {
    let shows = Show::select(
        ctx.conn,
        "WHERE completed = 0 AND paused = 0 AND feed_state IN ('NONE', 'CANDIDATE', 'ASSIGNED') \
         ORDER BY id",
        [],
    )?;
    Ok(shows
        .into_iter()
        .filter_map(|show| {
            let names = library::saved_release_names(&show, &ctx.settings);
            (!names.is_empty()).then_some((show, names))
        })
        .collect())
}

/// Locks a show to the feed its saved files came from, so a library carried over from another
/// machine keeps its source.
pub fn lock_from_saved_files(ctx: &mut Context<'_>) -> Result<Vec<Show>> {
    let mut locked = Vec::new();
    for (mut show, names) in saved_file_candidates(ctx)? {
        let excluded = failed_feed_ids(ctx, &show)?;
        let Some(found) = feed_from_saved_files(ctx, &show, &names, &excluded)? else {
            continue;
        };
        feeds::lock(
            ctx,
            &mut show,
            &found.feed,
            Evidence::SavedFilesUnique,
            found.title.as_deref(),
            found.release_group.as_deref(),
        )?;
        library::release(ctx, show.id.unwrap_or_default());
        locked.push(show);
    }
    Ok(locked)
}

/// Shows with saved files that no feed's items account for yet; they are held until a search
/// has run.
pub fn unmatched_saved_files(ctx: &mut Context<'_>) -> Result<Vec<Show>> {
    let mut unmatched = Vec::new();
    for (show, names) in saved_file_candidates(ctx)? {
        let excluded = failed_feed_ids(ctx, &show)?;
        if feed_from_saved_files(ctx, &show, &names, &excluded)?.is_none() {
            unmatched.push(show);
        } else {
            library::release(ctx, show.id.unwrap_or_default());
        }
    }
    Ok(unmatched)
}

/// Gives adopted downloads with no feed their feed once an item that carries them is stored.
///
/// A torrent adopted before its feed item was known is never revisited by adoption, so its feed
/// would stay unproven. Returns how many downloads got a feed.
pub fn reattribute(ctx: &mut Context<'_>) -> Result<usize> {
    let rows = Download::select(
        ctx.conn,
        "WHERE adopted = 1 AND feed_id IS NULL AND info_hash IS NOT NULL ORDER BY id",
        [],
    )?;
    let mut done = 0;
    for mut dl in rows {
        let Some(episode) = Episode::find(ctx.conn, "WHERE id = ?", [dl.episode_id])? else {
            continue;
        };
        let Some(mut show) = Show::find(ctx.conn, "WHERE id = ?", [episode.show_id])? else {
            continue;
        };
        let hash = dl.info_hash.as_deref().unwrap_or_default();
        let Some((feed, item)) = carriers(ctx, &show, &dl.title, hash)? else {
            continue;
        };
        dl.feed_id = feed.id;
        dl.feed_item_guid = Some(item.guid.clone());
        dl.torrent_url = dl
            .torrent_url
            .take()
            .filter(|url| !url.is_empty())
            .or(item.torrent_url.clone());
        dl.parsed_title = item
            .parsed_title
            .clone()
            .filter(|title| !title.is_empty())
            .or(dl.parsed_title.take());
        dl.update(ctx.conn)?;
        if show.feed_state != FeedState::Locked {
            feeds::lock(
                ctx,
                &mut show,
                &feed,
                Evidence::AdoptedUnique,
                item.parsed_title.as_deref(),
                item.release_group.as_deref(),
            )?;
        }
        done += 1;
    }
    Ok(done)
}
