//! Adding torrents for episodes and following them in qBittorrent until they are done.
//!
//! A service here changes the rows it is given and saves each change at once, so what it calls
//! next reads it. A function that needs the show or the episode takes the caller's copy rather
//! than loading a second one: the caller would otherwise write its stale copy over the change.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::qbittorrent::{
    AddTorrent, ContentLayout, Qbit, QbitError, QbitResult, Torrent,
};
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{
    ACTIVE_DOWNLOAD_STATES, Download, DownloadState, EndReason, Episode, EventKind, Feed, FeedItem,
    Show,
};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::parsing::{episode_pair, is_batch, same_release};
use kisetsu_domain::patterns::sanitize_folder_name;
use tracing::{info, warn};

use crate::context::{Context, PendingAdd};
use crate::error::{Error, Result};
use crate::events::{self, Subject};
use crate::feeds::{self, Evidence};
use crate::library::{self, same_dir};
use crate::tags;

/// Marks every torrent Kisetsu added.
pub const MANAGED_TAG: &str = "kisetsu-managed";
/// What a tag put on a torrent while its hash is unknown starts with.
pub const TAG_PREFIX: &str = "kisetsu-op-";

pub const MAX_ATTEMPTS: i64 = 8;
/// A version of an episode that gave up is tried again after these many hours (its 1st, 2nd and
/// 3rd give-up); the next one fails it.
pub const GIVE_UP_RETRY_HOURS: [i64; 3] = [2, 6, 12];
const GAVE_UP_PREFIX: &str = "Gave up after";
/// A replacement that has not finished by then gives up: it is dropped and the previous release
/// kept.
pub const REPLACEMENT_GIVE_UP: SignedDuration = SignedDuration::from_hours(48);
/// An add that qBittorrent never showed is treated as lost after this long.
pub const ADDING_STUCK: SignedDuration = SignedDuration::from_mins(15);
const AMBIGUOUS_RETRY: SignedDuration = SignedDuration::from_mins(15);
const ADD_RETRY: SignedDuration = SignedDuration::from_mins(1);
const ERROR_RETRY: SignedDuration = SignedDuration::from_mins(15);
/// A torrent qBittorrent could not delete is tried again after this long.
const DELETE_RETRY: SignedDuration = SignedDuration::from_mins(5);
/// A download that was only just added or is being verified is watched closely for this long.
pub const FAST_WINDOW: SignedDuration = SignedDuration::from_mins(5);
/// A torrent missing from qBittorrent is forgotten after this many consecutive passes with a
/// list of the same size.
const ABSENT_PASSES: u32 = 2;
/// Several missing torrents with nothing listed means qBittorrent is still loading.
const LOADING_GUESS: usize = 3;
/// With fewer, an empty list may still be qBittorrent loading: nothing is forgotten until it has
/// stayed empty this long.
const EMPTY_LIST_GRACE: SignedDuration = SignedDuration::from_mins(5);
/// An unfinished torrent that has moved no data for this long is called stalled.
pub const STALLED_AFTER: SignedDuration = SignedDuration::from_hours(6);
/// States in which a torrent is not expected to move: stopped by the user, waiting in
/// qBittorrent's queue, or being checked.
const IDLE_PREFIXES: [&str; 5] = ["paused", "stopped", "queued", "checking", "moving"];
/// The reason shown for a removed torrent that is requeued. A second removal is told by
/// `EndReason::Removed`, never by this text.
const REMOVED_REASON: &str =
    "Torrent is no longer present in qBittorrent; queued for a safe retry.";

/// Whether an unfinished, active torrent has not moved for `STALLED_AFTER`.
///
/// A torrent that was never active (still fetching its metadata) has no activity time, so it
/// counts from when it was added.
pub fn is_stalled(torrent: &Torrent, now: Timestamp) -> bool {
    let state = torrent.state.to_lowercase();
    if torrent.finished()
        || torrent.errored()
        || IDLE_PREFIXES.iter().any(|prefix| state.starts_with(prefix))
    {
        return false;
    }
    torrent
        .last_activity
        .or(torrent.added_on)
        .is_some_and(|since| now.duration_since(since) >= STALLED_AFTER)
}

/// What a settle pass found: whether to look again soon, and when the next retry is due.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Settled {
    pub busy: bool,
    pub active: bool,
    pub next_due: Option<Timestamp>,
    pub changed: bool,
}

/// How an add is made beyond the release itself.
#[derive(Debug, Clone, Copy, Default)]
pub struct StartOptions {
    /// Replace the release the episode has even when this one is no newer, for a feed the user
    /// moved the show to.
    pub switch_source: bool,
    /// The torrent holds several episodes.
    pub batch: bool,
    /// The feed's number of this episode when the item holds more than the one it is numbered
    /// by.
    pub source_episode: Option<i64>,
}

/// Whether a download failed for good. A failed download that kept its hash is only a torrent
/// qBittorrent reports as errored.
pub fn is_final_failure(dl: &Download) -> bool {
    dl.state == DownloadState::Failed && dl.info_hash.is_none()
}

/// Whether the download's torrent holds other episodes too: flagged when added, or named like a
/// batch or a double episode.
pub fn is_batch_download(dl: &Download) -> bool {
    dl.batch || is_batch(&dl.title) || episode_pair(&dl.title).is_some()
}

/// `states` as the members of an SQL `IN` list.
pub(crate) fn state_list(states: &[DownloadState]) -> String {
    states
        .iter()
        .map(|state| format!("'{}'", state.name()))
        .collect::<Vec<_>>()
        .join(", ")
}

fn active_states() -> String {
    state_list(&ACTIVE_DOWNLOAD_STATES)
}

fn has_hash(dl: &Download) -> bool {
    dl.info_hash.as_deref().is_some_and(|hash| !hash.is_empty())
}

fn save(ctx: &Context<'_>, dl: &Download) -> Result<()> {
    dl.update(ctx.conn)?;
    Ok(())
}

fn show_of(ctx: &Context<'_>, id: i64) -> Result<Option<Show>> {
    Ok(Show::find(ctx.conn, "WHERE id = ?", [id])?)
}

fn episode_of(ctx: &Context<'_>, id: i64) -> Result<Option<Episode>> {
    Ok(Episode::find(ctx.conn, "WHERE id = ?", [id])?)
}

fn download_of(ctx: &Context<'_>, id: Option<i64>) -> Result<Option<Download>> {
    match id {
        Some(id) => Ok(Download::find(ctx.conn, "WHERE id = ?", [id])?),
        None => Ok(None),
    }
}

/// The torrent qBittorrent showed at the last sync under this hash.
fn torrent_of(ctx: &mut Context<'_>, info_hash: &str) -> Result<Option<Torrent>> {
    Ok(ctx.qbit()?.torrents().get(info_hash).cloned())
}

/// The qBittorrent error in `error`, for a caller that deals with it; any other error goes on.
fn qbit_error(error: Error) -> Result<QbitError> {
    match error {
        Error::Qbit(error) => Ok(error),
        other => Err(other),
    }
}

/// The download an episode currently takes its release from.
pub fn current(ctx: &Context<'_>, episode: &Episode) -> Result<Option<Download>> {
    download_of(ctx, episode.current_download_id)
}

/// The episode's newest download that is still under way.
pub fn active_download(ctx: &Context<'_>, episode: &Episode) -> Result<Option<Download>> {
    Ok(Download::find(
        ctx.conn,
        &format!(
            "WHERE episode_id = ? AND state IN ({}) ORDER BY created_at DESC, id DESC",
            active_states()
        ),
        [episode.id],
    )?)
}

/// Sets the episode's status from the release it currently has, and saves the episode.
pub fn recompute_episode(ctx: &mut Context<'_>, episode: &mut Episode) -> Result<()> {
    match current(ctx, episode)? {
        None => {
            if !matches!(
                episode.status,
                EpisodeStatus::Wanted | EpisodeStatus::Missed | EpisodeStatus::Skipped
            ) {
                episode.status = EpisodeStatus::Wanted;
            }
            episode.attempts = 0;
        }
        Some(cur) => {
            let (status, last_error, retry_after) = match cur.state {
                DownloadState::Adding | DownloadState::Verifying | DownloadState::Downloading => {
                    (EpisodeStatus::Downloading, None, None)
                }
                DownloadState::Done => (EpisodeStatus::Completed, None, None),
                DownloadState::Failed if replacing(ctx, &cur)? => {
                    (EpisodeStatus::Downloading, None, None)
                }
                DownloadState::Failed => (EpisodeStatus::Failed, cur.last_error.clone(), None),
                _ => (
                    EpisodeStatus::Wanted,
                    cur.last_error.clone(),
                    cur.next_retry_at,
                ),
            };
            episode.status = status;
            episode.last_error = last_error;
            episode.retry_after = retry_after;
            episode.attempts = cur.attempts;
        }
    }
    episode.update(ctx.conn)?;
    // Only the episode changed: the views of other shows and of the feeds need not refetch.
    ctx.emit(&["episodes", &format!("show:{}", episode.show_id)]);
    Ok(())
}

/// Whether a replacement of `old` is still under way.
fn replacing(ctx: &Context<'_>, old: &Download) -> Result<bool> {
    Ok(ctx
        .conn
        .prepare_cached(&format!(
            "SELECT 1 FROM downloads WHERE supersedes_id = ? AND state IN ({})",
            active_states()
        ))?
        .exists([old.id])?)
}

/// Versions of this episode that failed for good; they are not offered again.
pub fn failed_versions(ctx: &Context<'_>, episode: &Episode) -> Result<BTreeSet<i64>> {
    let versions = ctx
        .conn
        .prepare_cached(
            "SELECT version FROM downloads WHERE episode_id = ? AND state = 'FAILED' AND info_hash IS NULL",
        )?
        .query_map([episode.id], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(versions)
}

/// The torrent qBittorrent holds for a download: by info hash, else by its operation tag.
pub fn locate(ctx: &mut Context<'_>, dl: &mut Download) -> Result<Option<Torrent>> {
    if let Some(hash) = dl.info_hash.clone().filter(|hash| !hash.is_empty()) {
        if let Some(torrent) = torrent_of(ctx, &hash)? {
            return Ok(Some(torrent));
        }
    }
    let Some(tag) = dl.tag.clone() else {
        return Ok(None);
    };
    let found = ctx.qbit()?.find_by_tag(&tag).cloned();
    if let Some(torrent) = &found {
        dl.info_hash = Some(torrent.hash.clone());
        save(ctx, dl)?;
    }
    Ok(found)
}

/// The one torrent holding this release name, whatever its tag or category.
fn by_name(ctx: &mut Context<'_>, title: &str) -> Result<Option<Torrent>> {
    let mut named = ctx
        .qbit()?
        .torrents()
        .values()
        .filter(|torrent| same_release(&torrent.name, title))
        .cloned();
    Ok(match (named.next(), named.next()) {
        (Some(only), None) => Some(only),
        _ => None,
    })
}

/// Adds the torrent of a feed item for an episode.
///
/// An episode that already has a release gets a replacement: the new torrent is added paused and
/// the old release stays until it has finished. Returns `None` when there is nothing to start.
/// The download returned is as it stood when the call ended.
pub fn start_download(
    ctx: &mut Context<'_>,
    show: &mut Show,
    feed: &Feed,
    episode: &mut Episode,
    item: &FeedItem,
    options: StartOptions,
) -> Result<Option<Download>> {
    if active_download(ctx, episode)?.is_some()
        || item.torrent_url.as_deref().is_none_or(str::is_empty)
    {
        return Ok(None);
    }
    if !ctx.qbit()?.synced() {
        ctx.qbit()?.sync(false)?;
    }
    let mut cur = current(ctx, episode)?;
    let mut supersedes = None;
    let mut old_present = false;
    if let Some(old) = cur.as_mut().filter(|old| {
        matches!(
            old.state,
            DownloadState::Verifying
                | DownloadState::Downloading
                | DownloadState::Done
                | DownloadState::Failed
        )
    }) {
        let old_torrent = match old.info_hash.clone() {
            Some(hash) if !hash.is_empty() => torrent_of(ctx, &hash)?,
            _ => None,
        };
        old_present = old_torrent.is_some();
        if old.state == DownloadState::Done && !has_hash(old) {
            // A finished episode whose torrent Kisetsu lost track of: the new add restores it and
            // keeps it completed meanwhile.
            supersedes = old.id;
        } else if item.version > old.version || options.switch_source {
            if old_present || old.state == DownloadState::Done {
                supersedes = old.id;
            } else if old.info_hash.is_none() && !old.title.is_empty() {
                if let Some(named) = by_name(ctx, &old.title)? {
                    supersedes = old.id;
                    old_present = true;
                    old.info_hash = Some(named.hash);
                    save(ctx, old)?;
                }
            }
        } else if old.state == DownloadState::Failed && old_present {
            // The errored torrent is still in qBittorrent: it goes the way of any replaced
            // release once this one has finished.
            supersedes = old.id;
        }
    }
    let tag = ctx.next_download_tag()?;
    let mut dl = new_download(ctx, feed, episode, item, options.source_episode);
    dl.tag = Some(tag);
    dl.batch = options.batch || episode_pair(&item.title).is_some();
    dl.supersedes_id = supersedes;
    dl.id = Some(dl.insert(ctx.conn)?);
    if supersedes.is_none() {
        if let Some(old) = cur.as_mut().filter(|old| old.state != DownloadState::Done) {
            old.state = DownloadState::Superseded;
            save(ctx, old)?;
        }
        episode.current_download_id = dl.id;
        episode.update(ctx.conn)?;
    }
    attempt_add(
        ctx,
        show,
        episode,
        &mut dl,
        supersedes.is_some() && old_present,
    )?;
    events::record(
        ctx,
        EventKind::Grabbed,
        &format!("Added {}", item.title),
        Subject {
            show_id: show.id,
            episode: Some(episode.number),
            feed_id: feed.id,
            title: &item.title,
        },
    )?;
    info!(
        "'{}' Ep {}: started from {}.",
        show.title(&ctx.settings.title_language),
        episode.number,
        feed.name
    );
    Ok(Some(dl))
}

/// A download of the item, not stored yet.
fn new_download(
    ctx: &Context<'_>,
    feed: &Feed,
    episode: &Episode,
    item: &FeedItem,
    source_episode: Option<i64>,
) -> Download {
    Download {
        id: None,
        episode_id: episode.id.unwrap_or_default(),
        feed_id: feed.id,
        feed_item_guid: Some(item.guid.clone()),
        title: item.title.clone(),
        parsed_title: item.parsed_title.clone(),
        release_group: item.release_group.clone(),
        version: item.version,
        source_episode: source_episode.or(item.episode_raw),
        torrent_url: item.torrent_url.clone(),
        info_hash: item.info_hash.clone(),
        tag: None,
        state: DownloadState::Adding,
        supersedes_id: None,
        adopted: false,
        batch: false,
        attempts: 0,
        next_retry_at: None,
        last_error: None,
        end_reason: None,
        created_at: ctx.now,
        updated_at: ctx.now,
        completed_at: None,
    }
}

/// Tracks another episode of a torrent already added (a batch, a double episode) on its hash,
/// without adding it again.
pub fn follow_download(
    ctx: &mut Context<'_>,
    show: &mut Show,
    feed: &Feed,
    episode: &mut Episode,
    item: &FeedItem,
    info_hash: &str,
    source_episode: Option<i64>,
) -> Result<Download> {
    let mut dl = new_download(ctx, feed, episode, item, source_episode);
    dl.info_hash = Some(info_hash.to_owned());
    dl.batch = true;
    dl.attempts = 1;
    dl.id = Some(dl.insert(ctx.conn)?);
    if let Some(mut old) = current(ctx, episode)?.filter(|old| old.state != DownloadState::Done) {
        old.state = DownloadState::Superseded;
        save(ctx, &old)?;
    }
    episode.current_download_id = dl.id;
    episode.update(ctx.conn)?;
    match torrent_of(ctx, info_hash)? {
        Some(torrent) => exposed(ctx, show, episode, &mut dl, &torrent)?,
        None => recompute_episode(ctx, episode)?,
    }
    events::record(
        ctx,
        EventKind::Grabbed,
        &format!("Added {}", item.title),
        Subject {
            show_id: show.id,
            episode: Some(episode.number),
            feed_id: feed.id,
            title: &item.title,
        },
    )?;
    info!(
        "'{}' Ep {}: started from {}.",
        show.title(&ctx.settings.title_language),
        episode.number,
        feed.name
    );
    Ok(dl)
}

/// Puts the download back in the queue to be added again after `delay`.
fn retry(
    ctx: &mut Context<'_>,
    dl: &mut Download,
    episode: &mut Episode,
    error: &str,
    delay: SignedDuration,
) -> Result<()> {
    dl.state = DownloadState::RetryWait;
    dl.last_error = Some(error.to_owned());
    dl.next_retry_at = Some(ctx.after(delay));
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    recompute_episode(ctx, episode)
}

/// What adding a torrent to qBittorrent came to.
enum Added {
    Accepted,
    /// qBittorrent could not be reached.
    Unavailable(String),
    /// qBittorrent refused, or answered something unexpected.
    Refused(QbitError),
}

fn add_to_qbit(qbit: &mut dyn Qbit, mut add: AddTorrent, title: &str) -> Added {
    let mut put = || -> QbitResult<()> {
        if add.save_path.is_empty() {
            // Blank "Save to": the show still gets its own folder under qBittorrent's default.
            let default = qbit.default_save_path()?;
            if !default.is_empty() {
                add.save_path = Path::new(&default)
                    .join(sanitize_folder_name(title))
                    .to_string_lossy()
                    .into_owned();
            }
        }
        qbit.add_torrent(&add)
    };
    match put() {
        Ok(()) => Added::Accepted,
        Err(QbitError::Unavailable(message)) => Added::Unavailable(message),
        Err(other) => Added::Refused(other),
    }
}

fn attempt_add(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
    paused: bool,
) -> Result<()> {
    if show.feed_is_fixed() && dl.feed_id.is_some() && dl.feed_id != show.feed_id {
        // The show was locked or pinned to another feed while this add waited: it downloads only
        // from that one now.
        let name = feeds::feed_by_id(ctx, show.feed_id)?
            .map_or_else(|| "its feed".to_owned(), |feed| feed.name);
        return cancel(
            ctx,
            dl,
            episode,
            &format!("Not added: the show now downloads only from '{name}'."),
            None,
        );
    }
    let category = show
        .category
        .clone()
        .filter(|category| !category.is_empty())
        .unwrap_or_else(|| ctx.settings.default_category.clone());
    let ratio = show.seed_ratio.unwrap_or(ctx.settings.default_seed_ratio);
    if !category.is_empty() {
        match ctx.qbit()?.ensure_category(&category) {
            Ok(()) => {}
            Err(QbitError::Unavailable(error)) => {
                let message = format!("Could not create the qBittorrent category: {error}");
                return retry(ctx, dl, episode, &message, ADD_RETRY);
            }
            Err(error) => {
                // qBittorrent answered and refused the category, so this counts as a failed add.
                dl.attempts += 1;
                let message = format!("Could not create the qBittorrent category: {error}");
                return retry(ctx, dl, episode, &message, AMBIGUOUS_RETRY);
            }
        }
    }
    dl.attempts += 1;
    // Adding before the add is committed: if the app stops during it, settle tries again once
    // it looks stuck.
    dl.state = DownloadState::Adding;
    dl.next_retry_at = None;
    dl.last_error = None;
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    let language = ctx.settings.title_language.clone();
    let title = show.title(&language);
    let save_path = library::save_path(&ctx.settings, show, &language);
    // Following a batch, or added again after its tag was released, a download has no operation tag.
    let mut labels = vec![MANAGED_TAG.to_owned()];
    labels.extend(dl.tag.clone());
    labels.push(tags::series_tag(ctx, show)?);
    labels.retain(|label| !label.is_empty());
    ctx.checkpoint()?;
    let add = AddTorrent {
        url: dl.torrent_url.clone().unwrap_or_default(),
        save_path,
        category,
        tags: labels.clone(),
        paused,
        ratio_limit: Some(ratio),
        // A batch carries its own top folder; its files go straight into the show's folder instead.
        content_layout: is_batch_download(dl).then_some(ContentLayout::NoSubfolder),
    };
    match add_to_qbit(ctx.qbit()?, add, &title) {
        Added::Accepted => {}
        Added::Unavailable(message) => return retry(ctx, dl, episode, &message, ADD_RETRY),
        Added::Refused(error) => {
            // A refused add and a duplicate look alike: the release may already be in qBittorrent.
            let Some(existing) = existing(ctx, dl)? else {
                return retry(ctx, dl, episode, &error.to_string(), AMBIGUOUS_RETRY);
            };
            if let Err(tag_error) = ctx
                .qbit()?
                .add_tags(std::slice::from_ref(&existing.hash), &labels)
            {
                return retry(ctx, dl, episode, &tag_error.to_string(), AMBIGUOUS_RETRY);
            }
            dl.info_hash = Some(existing.hash);
        }
    }
    // qBittorrent accepted the release, so its feed is proven for this show.
    if let Some(feed) = feeds::feed_by_id(ctx, dl.feed_id)? {
        feeds::lock(
            ctx,
            show,
            &feed,
            Evidence::Grab,
            learned_name(dl).as_deref(),
            dl.release_group.as_deref(),
        )?;
    }
    dl.state = DownloadState::Adding;
    save(ctx, dl)?;
    ctx.checkpoint()?;
    if let Some(pending) = ctx.pending_adds.as_mut() {
        pending.push(PendingAdd {
            download_id: dl.id.unwrap_or_default(),
            episode_id: episode.id.unwrap_or_default(),
        });
        return recompute_episode(ctx, episode);
    }
    let seen = sync_quietly(ctx)?;
    take_up(ctx, show, episode, dl, seen)
}

/// Syncs with qBittorrent; whether it could be read.
fn sync_quietly(ctx: &mut Context<'_>) -> Result<bool> {
    Ok(ctx.qbit()?.sync(false).is_ok())
}

/// Runs `work`, and looks for the torrents it adds with one sync at the end instead of one each.
pub fn batched_adds<'a, T>(
    ctx: &mut Context<'a>,
    work: impl FnOnce(&mut Context<'a>) -> Result<T>,
) -> Result<T> {
    if ctx.pending_adds.is_some() {
        return work(ctx);
    }
    ctx.pending_adds = Some(Vec::new());
    let outcome = work(ctx);
    let pending = ctx.pending_adds.take().unwrap_or_default();
    // After an error they are not looked for now: settle finds them by their tag.
    let value = outcome?;
    if !pending.is_empty() {
        find_added(ctx, &pending)?;
    }
    Ok(value)
}

/// Syncs once and takes up each torrent just added; one qBittorrent does not show yet waits for
/// settle.
fn find_added(ctx: &mut Context<'_>, added: &[PendingAdd]) -> Result<()> {
    let seen = sync_quietly(ctx)?;
    for pending in added {
        let Some(mut dl) = download_of(ctx, Some(pending.download_id))? else {
            continue;
        };
        let Some(mut episode) = episode_of(ctx, pending.episode_id)? else {
            continue;
        };
        let Some(mut show) = show_of(ctx, episode.show_id)? else {
            continue;
        };
        take_up(ctx, &mut show, &mut episode, &mut dl, seen)?;
    }
    Ok(())
}

/// Takes up a torrent just added, once qBittorrent shows it.
fn take_up(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
    seen: bool,
) -> Result<()> {
    if dl.state != DownloadState::Adding {
        return Ok(());
    }
    let torrent = if seen { locate(ctx, dl)? } else { None };
    match torrent {
        Some(torrent) => exposed(ctx, show, episode, dl, &torrent),
        None => {
            dl.last_error = Some("Waiting for qBittorrent to show the new torrent.".to_owned());
            save(ctx, dl)?;
            recompute_episode(ctx, episode)
        }
    }
}

/// The name a show's feed is learned by from this download; a batch's name ("... S01 (01-12)")
/// fits no single release.
fn learned_name(dl: &Download) -> Option<String> {
    if is_batch_download(dl) {
        return None;
    }
    Some(dl.parsed_title.clone().unwrap_or_else(|| dl.title.clone()))
}

/// The torrent qBittorrent already holds for a download whose add was refused.
fn existing(ctx: &mut Context<'_>, dl: &Download) -> Result<Option<Torrent>> {
    if ctx.qbit()?.sync(false).is_err() {
        return Ok(None);
    }
    if let Some(hash) = dl.info_hash.as_deref().filter(|hash| !hash.is_empty()) {
        if let Some(torrent) = torrent_of(ctx, hash)? {
            return Ok(Some(torrent));
        }
    }
    by_name(ctx, &dl.title)
}

/// Drops the temporary tag once the torrent is known by its hash.
fn release_tag(ctx: &mut Context<'_>, dl: &mut Download) -> Result<()> {
    let (Some(tag), Some(hash)) = (dl.tag.clone(), dl.info_hash.clone()) else {
        return Ok(());
    };
    let qbit = ctx.qbit()?;
    let hashes = [hash];
    let tags = [tag];
    if qbit
        .remove_tags(&hashes, &tags)
        .and_then(|()| qbit.delete_tags(&tags))
        .is_ok()
    {
        dl.tag = None;
    }
    Ok(())
}

/// Says once per show when a torrent is not in the folder Kisetsu asked qBittorrent to use.
fn check_folder(ctx: &mut Context<'_>, show: &Show, torrent: &Torrent) -> Result<()> {
    let language = ctx.settings.title_language.clone();
    let wanted = library::save_path(&ctx.settings, show, &language);
    let Some(show_id) = show.id else {
        return Ok(());
    };
    if wanted.is_empty()
        || same_dir(&torrent.save_path, &wanted)
        || !ctx.caches.misplaced.insert(show_id)
    {
        return Ok(());
    }
    let there = if torrent.save_path.is_empty() {
        "its default folder"
    } else {
        &torrent.save_path
    };
    let message = format!(
        "qBittorrent has a torrent of this show in {there}, not in {wanted}. The folder must be one qBittorrent itself can see: with qBittorrent in Docker, use the path inside the container."
    );
    warn!("{}: {message}", show.title(&language));
    events::record(
        ctx,
        EventKind::Info,
        &message,
        Subject {
            show_id: show.id,
            title: &torrent.name,
            ..Subject::default()
        },
    )?;
    Ok(())
}

/// The torrent is in qBittorrent: a replacement is verified first, anything else just downloads.
fn exposed(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
    torrent: &Torrent,
) -> Result<()> {
    dl.info_hash = Some(torrent.hash.clone());
    dl.last_error = None;
    release_tag(ctx, dl)?;
    if !dl.adopted {
        check_folder(ctx, show, torrent)?;
        if dl.feed_id.is_some() && !show.feed_is_fixed() {
            if let Some(feed) = feeds::feed_by_id(ctx, dl.feed_id)? {
                feeds::lock(
                    ctx,
                    show,
                    &feed,
                    Evidence::Grab,
                    learned_name(dl).as_deref(),
                    dl.release_group.as_deref(),
                )?;
            }
        }
    }
    if dl.supersedes_id.is_some() {
        // Added paused so the old release stays until this one is checked and has finished.
        let hashes = [torrent.hash.clone()];
        let qbit = ctx.qbit()?;
        if let Err(error) = qbit.recheck(&hashes).and_then(|()| qbit.resume(&hashes)) {
            return retry(ctx, dl, episode, &error.to_string(), ADD_RETRY);
        }
        dl.state = DownloadState::Verifying;
    } else {
        dl.state = DownloadState::Downloading;
    }
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    recompute_episode(ctx, episode)?;
    if torrent.finished() && dl.supersedes_id.is_none() {
        complete(ctx, dl, episode)?;
    }
    Ok(())
}

fn complete(ctx: &mut Context<'_>, dl: &mut Download, episode: &mut Episode) -> Result<()> {
    dl.state = DownloadState::Done;
    dl.completed_at = dl.completed_at.or(Some(ctx.now));
    dl.last_error = None;
    dl.next_retry_at = None;
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    episode.current_download_id = dl.id;
    recompute_episode(ctx, episode)?;
    let Some(show) = show_of(ctx, episode.show_id)? else {
        return Ok(());
    };
    if dl.supersedes_id.is_none() {
        events::record(
            ctx,
            EventKind::Done,
            "Downloaded",
            Subject {
                show_id: show.id,
                episode: Some(episode.number),
                feed_id: dl.feed_id,
                title: &dl.title,
            },
        )?;
        info!(
            "'{}' Ep {}: downloaded.",
            show.title(&ctx.settings.title_language),
            episode.number
        );
        notify_done(ctx, &show, dl, episode);
    }
    Ok(())
}

fn notify_done(ctx: &mut Context<'_>, show: &Show, dl: &Download, episode: &Episode) {
    let Some(notifier) = ctx.notifier.filter(|_| ctx.settings.desktop_notifications) else {
        return;
    };
    let name = show.title(&ctx.settings.title_language);
    let cover = show.cover_image.as_deref();
    if !dl.batch {
        notifier.notify(
            &format!("{name} - Episode {}", episode.number),
            "Download finished",
            cover,
        );
    } else if ctx
        .caches
        .announced_batches
        .insert(dl.info_hash.clone().unwrap_or_default())
    {
        // One torrent is one notification, however many episodes finish with it.
        notifier.notify(&format!("{name} - Batch"), "Download finished", cover);
    }
}

/// Fails a download for good. Its hash is cleared: nothing is left to follow, and a kept hash
/// would read as an errored torrent.
fn fail(
    ctx: &mut Context<'_>,
    dl: &mut Download,
    episode: &mut Episode,
    error: &str,
    end_reason: Option<EndReason>,
) -> Result<()> {
    dl.state = DownloadState::Failed;
    dl.info_hash = None;
    dl.last_error = Some(error.to_owned());
    dl.end_reason = end_reason;
    dl.next_retry_at = None;
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    recompute_episode(ctx, episode)?;
    events::record(
        ctx,
        EventKind::Failed,
        error,
        Subject {
            show_id: Some(episode.show_id),
            episode: Some(episode.number),
            feed_id: dl.feed_id,
            title: &dl.title,
        },
    )?;
    Ok(())
}

fn cancel(
    ctx: &mut Context<'_>,
    dl: &mut Download,
    episode: &mut Episode,
    reason: &str,
    end_reason: Option<EndReason>,
) -> Result<()> {
    dl.state = DownloadState::Canceled;
    dl.last_error = Some(reason.to_owned());
    dl.end_reason = end_reason;
    dl.next_retry_at = None;
    dl.updated_at = ctx.now;
    save(ctx, dl)?;
    if episode.current_download_id == dl.id {
        episode.current_download_id = None;
    }
    recompute_episode(ctx, episode)?;
    events::record(
        ctx,
        EventKind::Info,
        reason,
        Subject {
            show_id: Some(episode.show_id),
            episode: Some(episode.number),
            feed_id: dl.feed_id,
            title: &dl.title,
        },
    )?;
    Ok(())
}

/// What a version of an episode that gave up does next.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum GiveUp {
    /// The episode is wanted again after this many hours.
    RetryAfter(i64),
    /// Every try is used up.
    Fail,
}

impl GiveUp {
    /// After `earlier` give-ups of the same version.
    fn after(earlier: usize) -> Self {
        match GIVE_UP_RETRY_HOURS.get(earlier) {
            Some(hours) => Self::RetryAfter(*hours),
            None => Self::Fail,
        }
    }
}

fn give_up_step(ctx: &Context<'_>, dl: &Download) -> Result<GiveUp> {
    let earlier: i64 = ctx.conn.prepare_cached(
        "SELECT COUNT(*) FROM downloads WHERE episode_id = ? AND version = ? AND state = 'CANCELED' \
         AND end_reason IN ('ADD_GAVE_UP', 'REPLACEMENT_GAVE_UP')",
    )?
    .query_row((dl.episode_id, dl.version), |row| row.get(0))?;
    Ok(GiveUp::after(
        usize::try_from(earlier).unwrap_or(usize::MAX),
    ))
}

/// A download that used up its tries: wanted again after a while, or failed once the waits are
/// spent. `show` is given for an add, whose feed lock then goes if nothing else proves it.
fn give_up(
    ctx: &mut Context<'_>,
    dl: &mut Download,
    episode: &mut Episode,
    show: Option<&mut Show>,
    end_reason: EndReason,
    error: Option<String>,
) -> Result<()> {
    let error = error.unwrap_or_else(|| {
        format!(
            "{GAVE_UP_PREFIX} {} attempts: {}",
            dl.attempts,
            dl.last_error
                .as_deref()
                .unwrap_or("qBittorrent never showed the torrent.")
        )
    });
    match give_up_step(ctx, dl)? {
        // The hash came from the feed and qBittorrent never had it; followed, it would read as a
        // removed torrent and be tried again.
        GiveUp::Fail => fail(ctx, dl, episode, &error, Some(end_reason))?,
        GiveUp::RetryAfter(hours) => {
            cancel(ctx, dl, episode, &error, Some(end_reason))?;
            episode.retry_after = Some(ctx.after(SignedDuration::from_hours(hours)));
            episode.update(ctx.conn)?;
        }
    }
    if let Some(show) = show.filter(|_| dl.supersedes_id.is_none()) {
        feeds::unwind_unproven(ctx, show, dl.feed_id)?;
    }
    Ok(())
}

/// Deletes a torrent with its files, unless the release named by `keep_files_of` uses the same
/// files.
fn delete_torrent(
    ctx: &mut Context<'_>,
    info_hash: Option<&str>,
    keep_files_of: Option<&str>,
) -> Result<()> {
    let Some(hash) = info_hash.filter(|hash| !hash.is_empty()) else {
        return Ok(());
    };
    let Some(torrent) = torrent_of(ctx, hash)? else {
        return Ok(());
    };
    let protected = match keep_files_of.filter(|kept| *kept != hash) {
        Some(kept) => torrent_of(ctx, kept)?,
        None => None,
    };
    let shares = protected.is_some_and(|kept| {
        !torrent.content_path.is_empty() && torrent.content_path == kept.content_path
    });
    let hashes = [hash.to_owned()];
    let qbit = ctx.qbit()?;
    qbit.pause(&hashes)?;
    qbit.delete(&hashes, !shares)?;
    Ok(())
}

/// Stops a download and deletes the torrent it added, unless `leave_torrent`; a replacement
/// leaves the previous release as it is. Returns whether it was canceled.
pub fn cancel_download(
    ctx: &mut Context<'_>,
    show: &mut Show,
    dl: &mut Download,
    reason: &str,
    leave_torrent: bool,
) -> Result<bool> {
    let Some(mut episode) = episode_of(ctx, dl.episode_id)? else {
        return Ok(false);
    };
    cancel_for(ctx, show, &mut episode, dl, reason, leave_torrent)
}

fn cancel_for(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
    reason: &str,
    leave_torrent: bool,
) -> Result<bool> {
    let old = download_of(ctx, dl.supersedes_id)?;
    if !leave_torrent {
        let kept = old.as_ref().and_then(|old| old.info_hash.as_deref());
        if let Err(error) = delete_torrent(ctx, dl.info_hash.as_deref(), kept) {
            let error = qbit_error(error)?;
            dl.last_error = Some(format!("{reason} Cleanup failed: {error}"));
            save(ctx, dl)?;
            return Ok(false);
        }
    }
    cancel(ctx, dl, episode, reason, None)?;
    feeds::unwind_unproven(ctx, show, dl.feed_id)?;
    Ok(true)
}

/// Drops a stalled download: deletes its torrent (keeping files a previous release shares) and
/// cancels it.
///
/// Unlike `cancel_download` this does not unwind the show's feed: the feed delivered a real
/// release, and the one that replaces this download comes from the same feed. A qBittorrent
/// error leaves everything as it was.
pub fn abandon_stalled(ctx: &mut Context<'_>, dl: &mut Download, reason: &str) -> Result<()> {
    let Some(mut episode) = episode_of(ctx, dl.episode_id)? else {
        return Ok(());
    };
    let old = download_of(ctx, dl.supersedes_id)?;
    let kept = old.as_ref().and_then(|old| old.info_hash.as_deref());
    delete_torrent(ctx, dl.info_hash.as_deref(), kept)?;
    cancel(ctx, dl, &mut episode, reason, None)?;
    if let Some(id) = dl.id {
        ctx.caches.stalled.remove(&id);
    }
    Ok(())
}

/// Cancels the episode's unfinished downloads; with `keep_added` one already in qBittorrent
/// finishes, with `leave_torrents` it just stops being followed. Returns how many were canceled.
pub fn cancel_episode(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    reason: &str,
    keep_added: bool,
    leave_torrents: bool,
) -> Result<usize> {
    let mut canceled = 0;
    let downloads = Download::select(
        ctx.conn,
        &format!(
            "WHERE episode_id = ? AND state IN ({}) ORDER BY id",
            active_states()
        ),
        [episode.id],
    )?;
    for mut dl in downloads {
        if dl.supersedes_id.is_some() && dl.state == DownloadState::Downloading {
            continue;
        }
        if keep_added && locate(ctx, &mut dl)?.is_some() {
            continue;
        }
        if cancel_for(ctx, show, episode, &mut dl, reason, leave_torrents)? {
            canceled += 1;
        }
    }
    Ok(canceled)
}

/// After the user switched feeds: deletes what is still downloading from another feed.
///
/// A finished torrent is never touched, nor is a download whose feed is unknown. Returns the
/// episode numbers that are wanted again.
pub fn cancel_from_other_feeds(
    ctx: &mut Context<'_>,
    show: &mut Show,
    keep_feed_id: Option<i64>,
    reason: &str,
) -> Result<Vec<i64>> {
    // What finished since the last pass must count as finished, as anything else is deleted with its files.
    if let Err(error) = ctx.qbit()?.sync(false) {
        warn!(
            "{}: kept what is downloading from the old feed, as qBittorrent could not be read: {error}",
            show.title(&ctx.settings.title_language)
        );
        return Ok(Vec::new());
    }
    let states =
        state_list(&[ACTIVE_DOWNLOAD_STATES.as_slice(), &[DownloadState::Failed]].concat());
    let downloads = Download::select(
        ctx.conn,
        &format!(
            "WHERE episode_id IN (SELECT id FROM episodes WHERE show_id = ?) AND state IN ({states}) \
             AND feed_id IS NOT NULL AND feed_id IS NOT ? \
             ORDER BY (SELECT number FROM episodes WHERE episodes.id = downloads.episode_id), id"
        ),
        (show.id, keep_feed_id),
    )?;
    let mut reset = Vec::new();
    for mut dl in downloads {
        let Some(mut episode) = episode_of(ctx, dl.episode_id)? else {
            continue;
        };
        let torrent = match dl.info_hash.clone() {
            Some(hash) => torrent_of(ctx, &hash)?,
            None => locate(ctx, &mut dl)?,
        };
        if torrent.is_some_and(|torrent| torrent.finished()) {
            continue;
        }
        if cancel_for(ctx, show, &mut episode, &mut dl, reason, false)? {
            reset.push(episode.number);
        }
    }
    Ok(reset)
}

/// Whether a tracked torrent is really gone: missing on consecutive passes while the list did
/// not grow.
///
/// The episodes of a batch share one torrent, so `judged` makes a pass count it once.
fn missing(
    ctx: &mut Context<'_>,
    info_hash: &str,
    judged: &mut HashMap<String, bool>,
) -> Result<bool> {
    if torrent_of(ctx, info_hash)?.is_some() {
        ctx.caches.absent.remove(info_hash);
        return Ok(false);
    }
    let gone = match judged.get(info_hash) {
        Some(gone) => *gone,
        None => {
            let count = ctx.caches.absent.get(info_hash).copied().unwrap_or(0) + 1;
            ctx.caches.absent.insert(info_hash.to_owned(), count);
            judged.insert(info_hash.to_owned(), count >= ABSENT_PASSES);
            count >= ABSENT_PASSES
        }
    };
    Ok(gone)
}

/// The downloads a settle pass follows: what is under way or errored, and finished ones whose
/// torrent is still kept.
fn tracked_ids(ctx: &Context<'_>) -> Result<Vec<i64>> {
    let states =
        state_list(&[ACTIVE_DOWNLOAD_STATES.as_slice(), &[DownloadState::Failed]].concat());
    // In the order the database finds them, which settles the episodes of one torrent as a pass
    // always has.
    let tracked = Download::select(
        ctx.conn,
        &format!("WHERE state IN ({states}) OR (state = 'DONE' AND info_hash IS NOT NULL)"),
        [],
    )?;
    Ok(tracked.into_iter().filter_map(|dl| dl.id).collect())
}

/// Brings downloads up to date with qBittorrent: exposes, verifies, finishes, replaces, retries
/// and forgets.
///
/// With `observe_only` it only mirrors what qBittorrent shows (exposed, downloading, finished):
/// it adds, deletes and forgets nothing, which is what startup allows before adoption has
/// looked at what exists.
pub fn settle(ctx: &mut Context<'_>, observe_only: bool) -> Result<Settled> {
    let mut result = Settled::default();
    ctx.qbit()?.sync(false)?;
    let listed = ctx.qbit()?.torrents().len();
    if ctx.caches.listed != Some(listed) {
        ctx.caches.absent.clear();
        ctx.caches.listed = Some(listed);
    }
    let tracked = tracked_ids(ctx)?;
    let mut missing_count = 0;
    for id in &tracked {
        if let Some(hash) = download_of(ctx, Some(*id))?.and_then(|dl| dl.info_hash) {
            if torrent_of(ctx, &hash)?.is_none() {
                missing_count += 1;
            }
        }
    }
    if listed > 0 {
        ctx.caches.empty_since = None;
    } else if ctx.caches.empty_since.is_none() {
        ctx.caches.empty_since = Some(ctx.now);
    }
    let empty_for = ctx
        .caches
        .empty_since
        .map_or(SignedDuration::ZERO, |since| ctx.now.duration_since(since));
    let loading = listed == 0
        && missing_count > 0
        && (missing_count >= LOADING_GUESS || empty_for < EMPTY_LIST_GRACE);
    let mut judged = HashMap::new();
    // The shows whose page shows something new: a release that moved on, or one that stalled or
    // got going again.
    let mut touched: HashSet<i64> = HashSet::new();
    for id in &tracked {
        // Each download is read afresh: an earlier one may have changed it (a replacement that
        // finished supersedes the release it replaced).
        let Some(mut dl) = download_of(ctx, Some(*id))? else {
            continue;
        };
        if dl.state == DownloadState::Done {
            if let Some(hash) = dl.info_hash.clone() {
                if torrent_of(ctx, &hash)?.is_some() {
                    // Finished and still in qBittorrent, as most of what is tracked is: nothing
                    // to follow or to load for it.
                    ctx.caches.absent.remove(&hash);
                    continue;
                }
            }
        }
        let Some(mut episode) = episode_of(ctx, dl.episode_id)? else {
            continue;
        };
        let Some(mut show) = show_of(ctx, episode.show_id)? else {
            continue;
        };
        let before = (dl.state, dl.info_hash.clone());
        advance(
            ctx,
            &mut show,
            &mut episode,
            &mut dl,
            &mut judged,
            loading,
            observe_only,
        )?;
        if (dl.state, dl.info_hash.clone()) != before {
            result.changed = true;
            touched.insert(episode.show_id);
        }
        if ACTIVE_DOWNLOAD_STATES.contains(&dl.state) {
            result.active = true;
        }
        let closely_watched = matches!(dl.state, DownloadState::Adding | DownloadState::Verifying)
            && ctx.now.duration_since(dl.updated_at) < FAST_WINDOW;
        let absent = dl
            .info_hash
            .as_ref()
            .is_some_and(|hash| ctx.caches.absent.contains_key(hash));
        if closely_watched || absent {
            result.busy = true;
        }
        ctx.checkpoint()?;
        if let Some(due) = dl
            .next_retry_at
            .filter(|_| ACTIVE_DOWNLOAD_STATES.contains(&dl.state))
        {
            if result.next_due.is_none_or(|next| due < next) {
                result.next_due = Some(due);
            }
        }
    }
    let mut stalled = HashSet::new();
    for id in &tracked {
        let Some(dl) = download_of(ctx, Some(*id))? else {
            continue;
        };
        if !matches!(
            dl.state,
            DownloadState::Verifying | DownloadState::Downloading
        ) {
            continue;
        }
        let Some(hash) = dl.info_hash.as_deref() else {
            continue;
        };
        if torrent_of(ctx, hash)?.is_some_and(|torrent| is_stalled(&torrent, ctx.now)) {
            stalled.insert(*id);
        }
    }
    if stalled != ctx.caches.stalled {
        for id in stalled.symmetric_difference(&ctx.caches.stalled) {
            let shows = ctx
                .conn
                .prepare_cached(
                    "SELECT episodes.show_id FROM episodes JOIN downloads ON downloads.episode_id = episodes.id \
                     WHERE downloads.id = ?",
                )?
                .query_map([id], |row| row.get(0))?
                .collect::<std::result::Result<Vec<i64>, _>>()?;
            touched.extend(shows);
        }
        ctx.caches.stalled = stalled;
        result.changed = true;
    }
    if result.changed {
        let mut topics = vec!["episodes".to_owned(), "history".to_owned()];
        topics.extend(touched.iter().map(|show_id| format!("show:{show_id}")));
        ctx.emit(&topics.iter().map(String::as_str).collect::<Vec<_>>());
    }
    Ok(result)
}

/// What a torrent's state means for a download that is not a replacement.
#[derive(Debug, PartialEq, Eq)]
enum Progress {
    Finished,
    /// qBittorrent reports it errored.
    Errored,
    /// An errored torrent got going again.
    Revived,
    /// Its check is over: it downloads.
    Verified,
    Unchanged,
}

fn progress_of(dl: &Download, torrent: &Torrent) -> Progress {
    if torrent.finished() {
        Progress::Finished
    } else if torrent.errored() {
        Progress::Errored
    } else if dl.state == DownloadState::Failed && !torrent.checking() && torrent.progress > 0.0 {
        Progress::Revived
    } else if dl.state == DownloadState::Verifying && !torrent.checking() {
        Progress::Verified
    } else {
        Progress::Unchanged
    }
}

fn advance(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
    judged: &mut HashMap<String, bool>,
    loading: bool,
    observe_only: bool,
) -> Result<()> {
    if matches!(
        dl.state,
        DownloadState::Superseded | DownloadState::Canceled
    ) || is_final_failure(dl)
    {
        return Ok(());
    }
    let suspended = show.paused || show.completed;
    if matches!(dl.state, DownloadState::Adding | DownloadState::RetryWait) {
        if let Some(torrent) = locate(ctx, dl)? {
            return exposed(ctx, show, episode, dl, &torrent);
        }
        if suspended || observe_only {
            return Ok(());
        }
        if dl.state == DownloadState::Adding {
            let stuck = ctx.now
                >= dl
                    .updated_at
                    .saturating_add(ADDING_STUCK)
                    .unwrap_or(ctx.now);
            if stuck {
                retry(
                    ctx,
                    dl,
                    episode,
                    "qBittorrent has not shown the torrent; trying again.",
                    SignedDuration::ZERO,
                )?;
            }
            return Ok(());
        }
        if dl.next_retry_at.is_some_and(|due| due <= ctx.now) {
            if dl.attempts >= MAX_ATTEMPTS {
                give_up(ctx, dl, episode, Some(show), EndReason::AddGaveUp, None)?;
            } else {
                let paused = adds_paused(ctx, dl)?;
                attempt_add(ctx, show, episode, dl, paused)?;
            }
        }
        return Ok(());
    }

    let Some(hash) = dl.info_hash.clone() else {
        return Ok(());
    };
    let Some(torrent) = torrent_of(ctx, &hash)? else {
        if observe_only || loading || !missing(ctx, &hash, judged)? {
            return Ok(());
        }
        return forget(ctx, show, episode, dl);
    };
    ctx.caches.absent.remove(&hash);

    if dl.state == DownloadState::Done {
        return Ok(());
    }
    if dl.supersedes_id.is_some() {
        // A replacement's finish deletes the release it replaces, so it waits for the full
        // picture too.
        if !observe_only {
            advance_replacement(ctx, episode, dl, &torrent)?;
        }
        return Ok(());
    }
    match progress_of(dl, &torrent) {
        Progress::Finished => complete(ctx, dl, episode)?,
        Progress::Errored => {
            let error = format!("qBittorrent torrent state: {}", torrent.state);
            if dl.state != DownloadState::Failed || dl.last_error.as_deref() != Some(&error) {
                dl.state = DownloadState::Failed;
                dl.last_error = Some(error);
                save(ctx, dl)?;
                recompute_episode(ctx, episode)?;
            }
        }
        Progress::Revived => {
            dl.state = DownloadState::Downloading;
            dl.last_error = None;
            save(ctx, dl)?;
            recompute_episode(ctx, episode)?;
        }
        Progress::Verified => {
            dl.state = DownloadState::Downloading;
            save(ctx, dl)?;
        }
        Progress::Unchanged => {}
    }
    Ok(())
}

/// Whether a replacement of `dl` is added paused: its previous release is still in qBittorrent.
fn adds_paused(ctx: &mut Context<'_>, dl: &Download) -> Result<bool> {
    let Some(old) = download_of(ctx, dl.supersedes_id)? else {
        return Ok(false);
    };
    match old.info_hash {
        Some(hash) => Ok(torrent_of(ctx, &hash)?.is_some()),
        None => Ok(false),
    }
}

/// The torrent of a watched episode is gone. A finished release stays as what the episode had;
/// an episode left with none is skipped.
pub fn end_watched(ctx: &mut Context<'_>, dl: &mut Download, episode: &mut Episode) -> Result<()> {
    dl.info_hash = None;
    dl.end_reason = Some(EndReason::Watched);
    dl.next_retry_at = None;
    dl.updated_at = ctx.now;
    if dl.state != DownloadState::Done {
        dl.state = DownloadState::Canceled;
        if episode.current_download_id == dl.id {
            episode.current_download_id = None;
        }
    }
    save(ctx, dl)?;
    recompute_episode(ctx, episode)?;
    if episode.current_download_id.is_none() && episode.status != EpisodeStatus::Skipped {
        episode.status = EpisodeStatus::Skipped;
        episode.update(ctx.conn)?;
    }
    Ok(())
}

/// A tracked torrent was removed in qBittorrent.
fn forget(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
    dl: &mut Download,
) -> Result<()> {
    let hash = dl.info_hash.clone().unwrap_or_default();
    if dl.end_reason == Some(EndReason::Watched) {
        // Its episode was watched and the torrent was to be deleted anyway: it is never added
        // again.
        ctx.caches.absent.remove(&hash);
        return end_watched(ctx, dl, episode);
    }
    if dl.state == DownloadState::Done {
        dl.info_hash = None;
        save(ctx, dl)?;
        return events::record(
            ctx,
            EventKind::Info,
            "Torrent removed in qBittorrent, no longer managed",
            Subject {
                show_id: show.id,
                episode: Some(episode.number),
                title: &dl.title,
                ..Subject::default()
            },
        )
        .map(drop);
    }
    ctx.caches.absent.remove(&hash);
    if dl.supersedes_id.is_some() {
        return fail(
            ctx,
            dl,
            episode,
            "Replacement torrent is no longer present in qBittorrent; the previous release was kept.",
            None,
        );
    }
    let removed_before: i64 = ctx
        .conn
        .prepare_cached(
            "SELECT COUNT(*) FROM downloads WHERE episode_id = ? AND version = ? \
             AND state = 'CANCELED' AND end_reason = 'REMOVED'",
        )?
        .query_row((dl.episode_id, dl.version), |row| row.get(0))?;
    if removed_before > 0 {
        // Removed again after one retry: someone keeps deleting it, so it is not added a third time.
        fail(
            ctx,
            dl,
            episode,
            "Removed from qBittorrent again after one retry; not adding it again.",
            Some(EndReason::Removed),
        )?;
        feeds::unwind_unproven(ctx, show, dl.feed_id)?;
        return Ok(());
    }
    cancel(ctx, dl, episode, REMOVED_REASON, Some(EndReason::Removed))?;
    episode.retry_after = Some(ctx.after(ERROR_RETRY));
    episode.update(ctx.conn)?;
    feeds::unwind_unproven(ctx, show, dl.feed_id)?;
    events::record(
        ctx,
        EventKind::Info,
        "Requeued after qBittorrent removal",
        Subject {
            show_id: show.id,
            episode: Some(episode.number),
            title: &dl.title,
            ..Subject::default()
        },
    )?;
    Ok(())
}

/// What a replacement's torrent calls for.
#[derive(Debug, PartialEq, Eq)]
enum ReplacementStep {
    /// Not yet: a delay is running.
    Wait,
    /// It did not finish in time: it is dropped and the previous release kept.
    GiveUp,
    Errored,
    /// Still being checked, or not started.
    Hold,
    Downloading,
    /// Finished and checked, whether seeding or stopped: the previous release goes.
    Finish,
}

fn replacement_step(dl: &Download, torrent: &Torrent, now: Timestamp) -> ReplacementStep {
    let finished = torrent.finished();
    if dl.next_retry_at.is_some_and(|due| due > now) && !finished {
        ReplacementStep::Wait
    } else if !finished && now.duration_since(dl.created_at) > REPLACEMENT_GIVE_UP {
        ReplacementStep::GiveUp
    } else if torrent.errored() {
        ReplacementStep::Errored
    } else if torrent.checking() || (torrent.progress <= 0.0 && !finished) {
        ReplacementStep::Hold
    } else if !finished {
        ReplacementStep::Downloading
    } else {
        ReplacementStep::Finish
    }
}

fn advance_replacement(
    ctx: &mut Context<'_>,
    episode: &mut Episode,
    dl: &mut Download,
    torrent: &Torrent,
) -> Result<()> {
    let old = download_of(ctx, dl.supersedes_id)?;
    let old_hash = old.as_ref().and_then(|old| old.info_hash.clone());
    match replacement_step(dl, torrent, ctx.now) {
        ReplacementStep::Wait | ReplacementStep::Hold => {}
        ReplacementStep::GiveUp => {
            if let Err(error) = delete_torrent(ctx, dl.info_hash.as_deref(), old_hash.as_deref()) {
                // Given up now, the torrent would stay in qBittorrent with nothing following it.
                return put_off(ctx, dl, qbit_error(error)?);
            }
            let hours = REPLACEMENT_GIVE_UP.as_hours();
            let message = format!(
                "{GAVE_UP_PREFIX} {hours} hours: the replacement did not finish and was removed; the previous release was kept."
            );
            give_up(
                ctx,
                dl,
                episode,
                None,
                EndReason::ReplacementGaveUp,
                Some(message),
            )?;
        }
        ReplacementStep::Errored => {
            dl.last_error = Some(format!("Replacement torrent state: {}", torrent.state));
            dl.next_retry_at = Some(ctx.after(ERROR_RETRY));
            save(ctx, dl)?;
        }
        ReplacementStep::Downloading => {
            dl.state = DownloadState::Downloading;
            dl.last_error = None;
            dl.next_retry_at = None;
            save(ctx, dl)?;
        }
        ReplacementStep::Finish => {
            if let Some(old_hash) = old_hash
                .as_deref()
                .filter(|old| Some(*old) != dl.info_hash.as_deref())
            {
                if let Err(error) = delete_torrent(ctx, Some(old_hash), dl.info_hash.as_deref()) {
                    return put_off(ctx, dl, qbit_error(error)?);
                }
            }
            if let Some(mut old) = old {
                old.state = DownloadState::Superseded;
                old.updated_at = ctx.now;
                save(ctx, &old)?;
            }
            complete(ctx, dl, episode)?;
            events::record(
                ctx,
                EventKind::Replaced,
                &format!("Replaced by {}", dl.title),
                Subject {
                    show_id: Some(episode.show_id),
                    episode: Some(episode.number),
                    feed_id: dl.feed_id,
                    title: &dl.title,
                },
            )?;
        }
    }
    Ok(())
}

/// A delete qBittorrent refused: the replacement waits and tries again.
fn put_off(ctx: &mut Context<'_>, dl: &mut Download, error: QbitError) -> Result<()> {
    dl.last_error = Some(error.to_string());
    dl.next_retry_at = Some(ctx.after(DELETE_RETRY));
    save(ctx, dl)
}

/// Whether `tag` is one this module puts on a torrent while its hash is unknown.
fn is_operation_tag(tag: &str) -> bool {
    tag.strip_prefix(TAG_PREFIX).is_some_and(|rest| {
        rest.len() == 32
            && rest
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    })
}

/// Deletes operation tags that no pending add is waiting for; returns how many.
pub fn sweep_tags(ctx: &mut Context<'_>) -> Result<usize> {
    let waiting: HashSet<String> = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT tag FROM downloads WHERE tag IS NOT NULL AND state IN ({})",
            active_states()
        ))?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let stale: Vec<String> = ctx
        .qbit()?
        .tags()?
        .into_iter()
        .filter(|tag| is_operation_tag(tag) && !waiting.contains(tag))
        .collect();
    if !stale.is_empty() {
        ctx.qbit()?.delete_tags(&stale)?;
    }
    Ok(stale.len())
}
