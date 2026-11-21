//! Episodes the user has watched, going by their AniList progress.
//!
//! With `Settings.delete_watched` on, a watched episode is not downloaded, and its torrent is
//! deleted with its files. Each show keeps the progress (`Show.watched`) and up to which episode
//! it was acted on (`Show.watched_applied`), so an episode is acted on once: one the user asks
//! for again after it was watched is kept.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::qbittorrent::Torrent;
use kisetsu_db::schema::{
    ACTIVE_DOWNLOAD_STATES, Download, DownloadState, EndReason, Episode, EventKind, Show,
};
use kisetsu_domain::enums::EpisodeStatus;
use tracing::info;

use crate::context::Context;
use crate::downloads::{self, state_list};
use crate::error::Result;
use crate::events::{self, Subject};
use crate::paths::{normalized, within};

/// Watched episodes in these states that hold no torrent are skipped.
const SKIPPABLE: [EpisodeStatus; 3] = [
    EpisodeStatus::Wanted,
    EpisodeStatus::Missed,
    EpisodeStatus::Failed,
];
/// A watched episode's finished torrent waits for qBittorrent to stop seeding it (its seed
/// ratio), but no longer than this after it finished: a release few people download may never
/// reach the ratio.
const SEED_MAX: SignedDuration = SignedDuration::from_hours(7 * 24);
const GONE_STATES: [DownloadState; 2] = [DownloadState::Superseded, DownloadState::Canceled];

/// Whether the episode is one to leave alone because the user watched it; never with the
/// setting off.
pub fn is_watched(ctx: &Context<'_>, show: &Show, number: i64) -> bool {
    ctx.settings.delete_watched && show.watched.is_some_and(|watched| number <= watched)
}

fn holds_nothing(ctx: &Context<'_>, episode: &Episode) -> Result<bool> {
    if downloads::active_download(ctx, episode)?.is_some() {
        return Ok(false);
    }
    Ok(downloads::current(ctx, episode)?.is_none_or(|cur| cur.info_hash.is_none()))
}

/// How far the user has watched, when that is past `after` and watched episodes are to be left
/// alone.
fn progress_past(ctx: &Context<'_>, show: &Show, after: i64) -> Option<i64> {
    show.watched
        .filter(|watched| ctx.settings.delete_watched && *watched > after.max(0))
}

/// Skips the watched episodes past `after` that hold no torrent, so nothing downloads or
/// searches for them. Returns how many were skipped. A torrent is left to `apply`, which needs
/// qBittorrent.
pub fn skip_watched(ctx: &mut Context<'_>, show: &Show, after: i64) -> Result<usize> {
    let Some(watched) = progress_past(ctx, show, after) else {
        return Ok(0);
    };
    let mut episodes = Episode::select(
        ctx.conn,
        "WHERE show_id = ? AND number > ? AND number <= ? ORDER BY id",
        (show.id, after, watched),
    )?;
    skip_among(ctx, show, after, episodes.iter_mut())
}

/// `skip_watched` for the episodes the caller holds, which it sees changed.
pub fn skip_among<'a>(
    ctx: &mut Context<'_>,
    show: &Show,
    after: i64,
    episodes: impl IntoIterator<Item = &'a mut Episode>,
) -> Result<usize> {
    let Some(watched) = progress_past(ctx, show, after) else {
        return Ok(0);
    };
    let mut skipped = 0;
    for episode in episodes {
        let in_range = after < episode.number && episode.number <= watched;
        if in_range && SKIPPABLE.contains(&episode.status) && holds_nothing(ctx, episode)? {
            episode.status = EpisodeStatus::Skipped;
            episode.last_error = None;
            episode.retry_after = None;
            episode.update(ctx.conn)?;
            skipped += 1;
        }
    }
    if skipped > 0 {
        ctx.emit(&["episodes", &format!("show:{}", show.id.unwrap_or_default())]);
    }
    Ok(skipped)
}

/// Takes in the progress a sync reported; returns whether the show changed.
///
/// The first progress seen for a show is where it stands, not a watch. With deleting watched
/// episodes off there is nothing to act on, so the progress counts as acted on at once. A lower
/// one (a rewatch, a correction) lowers how far it was acted on too, so the episodes are acted
/// on again when the progress passes them once more.
pub fn note_progress(ctx: &mut Context<'_>, show: &mut Show, progress: i64) -> Result<bool> {
    if show.watched == Some(progress) {
        return Ok(false);
    }
    let Some(applied) = show.watched.and(show.watched_applied) else {
        show.watched = Some(progress);
        show.watched_applied = Some(progress);
        show.update(ctx.conn)?;
        // Turned on before the progress was known: what was watched is not downloaded, and what
        // is in qBittorrent stays.
        skip_watched(ctx, show, 0)?;
        return Ok(true);
    };
    show.watched = Some(progress);
    if progress < applied || !ctx.settings.delete_watched {
        show.watched_applied = Some(progress);
        show.update(ctx.conn)?;
    } else {
        show.update(ctx.conn)?;
        // Before a grab could start them; their torrents wait for `apply`.
        skip_watched(ctx, show, applied)?;
    }
    Ok(true)
}

/// Acts on what the user watched: marks the torrents of newly watched episodes, then deletes
/// those whose time has come.
///
/// Runs after settle has synced with qBittorrent. Returns whether anything changed.
pub fn apply(ctx: &mut Context<'_>) -> Result<bool> {
    if !ctx.settings.delete_watched {
        return Ok(false);
    }
    let mut changed = false;
    let due = Show::select(
        ctx.conn,
        "WHERE watched IS NOT NULL AND watched_applied < watched ORDER BY id",
        [],
    )?;
    for mut show in due {
        if mark(ctx, &show)? {
            show.watched_applied = show.watched;
            show.update(ctx.conn)?;
            changed = true;
        }
    }
    if changed {
        ctx.checkpoint()?;
    }
    Ok(sweep(ctx)? || changed)
}

/// Marks the torrents of the episodes watched since the last pass; false while an add
/// qBittorrent has not shown yet holds it up.
fn mark(ctx: &mut Context<'_>, show: &Show) -> Result<bool> {
    let after = show.watched_applied.unwrap_or(0);
    skip_watched(ctx, show, after)?;
    let episodes = Episode::select(
        ctx.conn,
        "WHERE show_id = ? AND number > ? AND number <= ? ORDER BY id",
        (show.id, after, show.watched),
    )?;
    for mut episode in episodes {
        let active = Download::select(
            ctx.conn,
            &format!(
                "WHERE episode_id = ? AND state IN ({}) ORDER BY id",
                state_list(&ACTIVE_DOWNLOAD_STATES)
            ),
            [episode.id],
        )?;
        for mut dl in active {
            if downloads::locate(ctx, &mut dl)?.is_none() {
                if dl.state == DownloadState::Adding {
                    return Ok(false);
                }
                // A retry still waiting never reached qBittorrent, so there is nothing to delete.
                downloads::end_watched(ctx, &mut dl, &mut episode)?;
                continue;
            }
            dl.end_reason = Some(EndReason::Watched);
            dl.update(ctx.conn)?;
        }
        if let Some(mut cur) = downloads::current(ctx, &episode)? {
            if cur.info_hash.is_some()
                && matches!(cur.state, DownloadState::Done | DownloadState::Failed)
            {
                cur.end_reason = Some(EndReason::Watched);
                cur.update(ctx.conn)?;
            }
        }
    }
    Ok(true)
}

/// Every episode's release in this torrent: what is downloading from it, and what an episode
/// has from it now.
fn holders(ctx: &Context<'_>, info_hash: &str) -> Result<Vec<(Download, Episode)>> {
    let rows = Download::select(
        ctx.conn,
        &format!(
            "WHERE info_hash = ? AND state NOT IN ({}) ORDER BY id",
            state_list(&GONE_STATES)
        ),
        [info_hash],
    )?;
    let mut found = Vec::new();
    for dl in rows {
        let episode = Episode::find(ctx.conn, "WHERE id = ?", [dl.episode_id])?;
        if let Some(episode) = episode.filter(|episode| {
            dl.state != DownloadState::Done || episode.current_download_id == dl.id
        }) {
            found.push((dl, episode));
        }
    }
    Ok(found)
}

fn still_seeding(torrent: &Torrent, held: &[(Download, Episode)], now: Timestamp) -> bool {
    let state = torrent.state.to_lowercase();
    if torrent.errored() || state.starts_with("paused") || state.starts_with("stopped") {
        return false;
    }
    let finished = torrent
        .completion_on
        .or_else(|| held.iter().filter_map(|(dl, _)| dl.completed_at).max());
    finished.is_some_and(|finished| now.duration_since(finished) < SEED_MAX)
}

/// Deletes the torrents every episode of which was watched, once a finished one is done seeding.
fn sweep(ctx: &mut Context<'_>) -> Result<bool> {
    let marked: BTreeSet<String> = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT info_hash FROM downloads WHERE end_reason = 'WATCHED' AND info_hash IS NOT NULL \
             AND state NOT IN ({})",
            state_list(&GONE_STATES)
        ))?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let mut due: Vec<(String, Vec<(Download, Episode)>)> = Vec::new();
    for info_hash in marked {
        let Some(torrent) = ctx.qbit()?.torrents().get(&info_hash).cloned() else {
            // A torrent already gone is settled by `downloads::settle`, as one removed in qBittorrent.
            continue;
        };
        if torrent.checking() {
            continue;
        }
        let held = holders(ctx, &info_hash)?;
        // A batch waits for its last episode to be watched.
        if held.is_empty()
            || held
                .iter()
                .any(|(dl, _)| dl.end_reason != Some(EndReason::Watched))
        {
            continue;
        }
        if torrent.finished() && still_seeding(&torrent, &held, ctx.now) {
            continue;
        }
        due.push((info_hash, held));
    }
    if due.is_empty() {
        return Ok(false);
    }
    let hashes: Vec<String> = due.iter().map(|(hash, _)| hash.clone()).collect();
    delete_torrents(ctx, &hashes)?;
    ctx.qbit()?.sync(false)?;
    let mut ended: Vec<(Download, Episode)> = Vec::new();
    for (info_hash, held) in due {
        if ctx.qbit()?.torrents().contains_key(&info_hash) {
            continue;
        }
        ended.extend(held);
    }
    let gone = !ended.is_empty();
    // By show and episode, the last first as History lists the newest on top; what an episode has
    // now comes before a replacement, so its event names that release.
    ended.sort_by_key(|(dl, episode)| {
        (
            episode.show_id,
            -episode.number,
            episode.current_download_id != dl.id,
        )
    });
    // An episode held by two releases is one row, so that each ending sees what the other did.
    let mut episodes: HashMap<i64, Episode> = HashMap::new();
    let mut told = HashSet::new();
    let mut names: BTreeMap<i64, Vec<i64>> = BTreeMap::new();
    for (mut dl, held_episode) in ended {
        let episode_id = held_episode.id.unwrap_or_default();
        let episode = episodes.entry(episode_id).or_insert(held_episode);
        if told.insert(episode_id) {
            events::record(
                ctx,
                EventKind::Deleted,
                "Watched, so deleted",
                Subject {
                    show_id: Some(episode.show_id),
                    episode: Some(episode.number),
                    feed_id: dl.feed_id,
                    title: &dl.title,
                },
            )?;
            names
                .entry(episode.show_id)
                .or_default()
                .push(episode.number);
        }
        downloads::end_watched(ctx, &mut dl, episode)?;
    }
    for (show_id, mut numbers) in names.clone() {
        let show = Show::find(ctx.conn, "WHERE id = ?", [show_id])?;
        if let Some(show) = show {
            numbers.sort_unstable();
            let listed = numbers
                .iter()
                .map(i64::to_string)
                .collect::<Vec<_>>()
                .join(", ");
            info!(
                "'{}' Ep {listed}: watched, so deleted from qBittorrent with the files.",
                show.title(&ctx.settings.title_language)
            );
        }
    }
    if gone {
        let mut topics = vec!["shows".to_owned(), "history".to_owned()];
        topics.extend(names.keys().map(|show_id| format!("show:{show_id}")));
        ctx.emit(&topics.iter().map(String::as_str).collect::<Vec<_>>());
    }
    Ok(gone)
}

/// Deletes the torrents with their files, except a torrent that has a file in common with one
/// that stays: it keeps them.
///
/// Only torrents whose content paths nest can have files in common (a batch put straight in a
/// show's folder holds that folder, as the singles next to it do), so only theirs are listed and
/// compared.
fn delete_torrents(ctx: &mut Context<'_>, hashes: &[String]) -> Result<()> {
    let torrents = ctx.qbit()?.torrents().clone();
    let going: HashSet<&str> = hashes.iter().map(String::as_str).collect();
    let mut listed: HashMap<String, HashSet<String>> = HashMap::new();
    let mut shared = Vec::new();
    for hash in hashes {
        let Some(torrent) = torrents.get(hash) else {
            continue;
        };
        let mut sharing = false;
        for (other, kept) in &torrents {
            if going.contains(other.as_str()) || !nest(torrent, kept) {
                continue;
            }
            list_files(ctx, &mut listed, &torrents, hash)?;
            list_files(ctx, &mut listed, &torrents, other)?;
            if listed
                .get(hash)
                .zip(listed.get(other))
                .is_some_and(|(ours, theirs)| !ours.is_disjoint(theirs))
            {
                sharing = true;
                break;
            }
        }
        if sharing {
            shared.push(hash.clone());
        }
    }
    ctx.qbit()?.pause(hashes)?;
    let alone: Vec<String> = hashes
        .iter()
        .filter(|hash| !shared.contains(hash))
        .cloned()
        .collect();
    for (chosen, delete_files) in [(alone, true), (shared, false)] {
        if !chosen.is_empty() {
            ctx.qbit()?.delete(&chosen, delete_files)?;
        }
    }
    Ok(())
}

/// Asks qBittorrent for the files of a torrent, once, and keeps them as normalized paths.
fn list_files(
    ctx: &mut Context<'_>,
    listed: &mut HashMap<String, HashSet<String>>,
    torrents: &BTreeMap<String, Torrent>,
    info_hash: &str,
) -> Result<()> {
    if listed.contains_key(info_hash) {
        return Ok(());
    }
    let folder = torrents
        .get(info_hash)
        .map(|torrent| torrent.save_path.clone())
        .unwrap_or_default();
    let names = ctx.qbit()?.files(info_hash)?;
    let files = names
        .iter()
        .map(|name| normalized(&Path::new(&folder).join(name).to_string_lossy()))
        .collect();
    listed.insert(info_hash.to_owned(), files);
    Ok(())
}

/// Whether one torrent's content is the other's or inside it.
fn nest(a: &Torrent, b: &Torrent) -> bool {
    !a.content_path.is_empty()
        && !b.content_path.is_empty()
        && (within(&a.content_path, &b.content_path) || within(&b.content_path, &a.content_path))
}

/// The setting was just turned on or off.
///
/// On, the episodes already watched are not downloaded, and with `backlog` their torrents are
/// deleted as well (by `apply`). Off, nothing is deleted any more, and what was waiting to be is
/// kept.
pub fn switched(ctx: &mut Context<'_>, backlog: bool) -> Result<()> {
    let shows = Show::select(ctx.conn, "WHERE watched IS NOT NULL ORDER BY id", [])?;
    for mut show in shows.iter().cloned() {
        if !ctx.settings.delete_watched {
            show.watched_applied = show.watched;
        } else if backlog {
            show.watched_applied = Some(0);
        } else {
            skip_watched(ctx, &show, 0)?;
        }
        show.update(ctx.conn)?;
    }
    if !ctx.settings.delete_watched {
        let waiting = Download::select(
            ctx.conn,
            "WHERE end_reason = 'WATCHED' AND info_hash IS NOT NULL ORDER BY id",
            [],
        )?;
        for mut dl in waiting {
            dl.end_reason = None;
            dl.update(ctx.conn)?;
        }
    }
    let mut topics = vec!["shows".to_owned(), "episodes".to_owned()];
    topics.extend(
        shows
            .iter()
            .map(|show| format!("show:{}", show.id.unwrap_or_default())),
    );
    ctx.emit(&topics.iter().map(String::as_str).collect::<Vec<_>>());
    Ok(())
}

/// What deleting the episodes of one show that were already watched would delete.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BacklogShow {
    pub name: String,
    pub numbers: Vec<i64>,
    /// The size of their torrents; unknown without qBittorrent.
    pub size: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Backlog {
    pub shows: Vec<BacklogShow>,
    /// How many episodes in all.
    pub episodes: usize,
    pub size: Option<i64>,
}

/// What deleting the episodes already watched would delete: by show, the episodes and the size of
/// their torrents.
///
/// A batch counts only when every episode in it was watched. Without a reading of qBittorrent
/// the size is unknown.
pub fn backlog(ctx: &mut Context<'_>) -> Result<Backlog> {
    let listed: Option<BTreeMap<String, Torrent>> = match ctx.qbit()?.sync(false) {
        Ok(_) => Some(ctx.qbit()?.torrents().clone()),
        Err(_) => None,
    };
    let hashes: BTreeSet<String> = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT downloads.info_hash FROM downloads \
             JOIN episodes ON episodes.id = downloads.episode_id \
             JOIN shows ON shows.id = episodes.show_id \
             WHERE shows.watched IS NOT NULL AND episodes.number <= shows.watched \
             AND downloads.info_hash IS NOT NULL AND downloads.state NOT IN ({})",
            state_list(&GONE_STATES)
        ))?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let mut found: BTreeMap<i64, (String, BTreeSet<i64>, i64)> = BTreeMap::new();
    for info_hash in hashes {
        if listed
            .as_ref()
            .is_some_and(|listed| !listed.contains_key(&info_hash))
        {
            continue;
        }
        let held = holders(ctx, &info_hash)?;
        let mut owners = Vec::new();
        for (_, episode) in &held {
            owners.push(Show::find(ctx.conn, "WHERE id = ?", [episode.show_id])?);
        }
        let all_watched = !held.is_empty()
            && held.iter().zip(&owners).all(|((_, episode), owner)| {
                owner
                    .as_ref()
                    .and_then(|owner| owner.watched)
                    .is_some_and(|watched| episode.number <= watched)
            });
        if !all_watched {
            continue;
        }
        for (index, ((_, episode), owner)) in held.iter().zip(&owners).enumerate() {
            let Some(owner) = owner else {
                continue;
            };
            let row = found
                .entry(owner.id.unwrap_or_default())
                .or_insert_with(|| {
                    (
                        owner.title(&ctx.settings.title_language),
                        BTreeSet::new(),
                        0,
                    )
                });
            row.1.insert(episode.number);
            if index == 0 {
                if let Some(torrent) = listed.as_ref().and_then(|listed| listed.get(&info_hash)) {
                    row.2 += torrent.size;
                }
            }
        }
    }
    let mut shows: Vec<(String, BTreeSet<i64>, i64)> = found.into_values().collect();
    shows.sort_by_key(|(name, _, _)| name.to_lowercase());
    let known = listed.is_some();
    let size = known.then(|| shows.iter().map(|(_, _, size)| size).sum());
    let episodes = shows.iter().map(|(_, numbers, _)| numbers.len()).sum();
    Ok(Backlog {
        shows: shows
            .into_iter()
            .map(|(name, numbers, size)| BacklogShow {
                name,
                numbers: numbers.into_iter().collect(),
                size: known.then_some(size),
            })
            .collect(),
        episodes,
        size,
    })
}
