//! Explicit user actions on releases: list what the feeds carry, grab one, restore a lost torrent.

use std::collections::{BTreeMap, HashSet};

use kisetsu_db::schema::{Download, DownloadState, Episode, Feed, FeedItem, FeedState, Show};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::matcher::Matcher;
use kisetsu_domain::parsing::{resolution_rank, same_release};
use kisetsu_domain::schedule::RELEASE_HOLDING;

use crate::context::Context;
use crate::downloads::{self, MANAGED_TAG, StartOptions};
use crate::error::{Error, Result};
use crate::facts::facts_for;
use crate::feeds;
use crate::grab::{self, ResolveOptions, Resolved, ShowState};
use crate::tags;

/// Why an explicit action on a release was not carried out. The messages are shown to the user.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    #[error("This show is paused. Resume it before downloading.")]
    Paused,
    #[error("Episode {number} already has a download in progress.")]
    InProgress { number: i64 },
    #[error("Episode {number} came in a batch; a single release won't replace it.")]
    CameInBatch { number: i64 },
    #[error("Episode {number} already has v{held}; an older version won't replace it.")]
    OlderVersion { number: i64, held: i64 },
    #[error("Episode {number} v{held} is already downloaded or queued.")]
    AlreadyHeld { number: i64, held: i64 },
    #[error("That release is not in this show's feed any more, or does not match this show.")]
    NotInFeed,
    #[error("Several releases in the feed match that name; pick one from the Releases tab.")]
    Ambiguous,
    #[error("The matching release in the feed is episode {found}, not episode {expected}.")]
    OtherEpisode { found: i64, expected: i64 },
    /// The download could not be started; the episode says why when it knows.
    #[error("{0}")]
    NotStarted(String),
    #[error("Episode {number} has no stalled download.")]
    NotStalled { number: i64 },
    #[error("No other release of episode {number} is in the feeds this show uses.")]
    NoAlternative { number: i64 },
    #[error("Episode {number} is already managed.")]
    AlreadyManaged { number: i64 },
    #[error("No release is recorded for episode {number}.")]
    NoRecordedRelease { number: i64 },
    #[error("Not in qBittorrent. {0}")]
    NotInQbit(Box<Refusal>),
}

const NOT_STARTED: &str = "Could not start the download for that release.";

/// A stored release the grabber would accept for a show, with the episode it is for.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub feed: Feed,
    pub entry: Resolved,
    pub episode: Episode,
}

/// How a show relates to a feed it is listed under.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Locked,
    Picked,
    Assigned,
    Discovery,
    /// A feed the show may not read; listed for information only.
    Other,
}

impl Role {
    pub fn as_str(self) -> &'static str {
        match self {
            Role::Locked => "locked",
            Role::Picked => "picked",
            Role::Assigned => "assigned",
            Role::Discovery => "discovery",
            Role::Other => "other",
        }
    }
}

/// One release a feed carries, as the Releases tab shows it.
#[derive(Debug, Clone, PartialEq)]
pub struct Match {
    pub title: String,
    pub display_title: String,
    pub feed_id: i64,
    pub feed_name: String,
    pub episode: i64,
    pub version: i64,
    pub episode_status: EpisodeStatus,
    /// Whether this item is the release the episode has: another feed's copy of a finished
    /// episode is not.
    pub this_release: bool,
    pub restore: bool,
    pub replaces: bool,
    pub downloadable: bool,
}

/// An item of a feed that carries the show's name but is not taken, and why.
#[derive(Debug, Clone, PartialEq)]
pub struct Rejected {
    pub title: String,
    pub reason: String,
    pub published_at: Option<jiff::Timestamp>,
}

/// What one feed carries for a show.
#[derive(Debug, Clone, PartialEq)]
pub struct FeedGroup {
    pub feed_id: i64,
    pub feed_name: String,
    pub role: Role,
    /// Whether the show may download from this feed.
    pub usable: bool,
    /// The feed could not be read, so nothing is stored for it.
    pub unavailable: bool,
    pub matches: Vec<Match>,
    /// `None` for a feed whose turned-down items were not asked for.
    pub rejected: Option<Vec<Rejected>>,
}

/// The items turned down while listing, by feed id.
pub type Rejections = BTreeMap<i64, Vec<(FeedItem, String)>>;

/// How many of a feed's newest items are read for the ones that carry the show's name but are
/// not taken.
const UNMATCHED_LIMIT: usize = 300;

fn id_of(id: Option<i64>) -> i64 {
    id.unwrap_or_default()
}

fn has_hash(hash: Option<&str>) -> bool {
    hash.is_some_and(|hash| !hash.is_empty())
}

fn has_url(item: &FeedItem) -> bool {
    item.torrent_url
        .as_deref()
        .is_some_and(|url| !url.is_empty())
}

/// A downloaded episode Kisetsu holds no torrent for, so it cannot manage it.
fn is_restore(ctx: &Context<'_>, episode: &Episode) -> Result<bool> {
    let current = downloads::current(ctx, episode)?;
    Ok(episode.status == EpisodeStatus::Completed
        && current.is_none_or(|current| current.info_hash.is_none()))
}

/// Whether `item` is another copy of a finished, managed episode.
///
/// That is a different feed than the one the episode came from, or, when the feed was not
/// recorded, a release that is not the one already downloaded.
fn other_source(
    ctx: &Context<'_>,
    episode: &Episode,
    feed: &Feed,
    item: &FeedItem,
) -> Result<bool> {
    let Some(current) = downloads::current(ctx, episode)? else {
        return Ok(false);
    };
    if episode.status != EpisodeStatus::Completed || !has_hash(current.info_hash.as_deref()) {
        return Ok(false);
    }
    if item.info_hash.is_some() && item.info_hash == current.info_hash {
        return Ok(false);
    }
    Ok(match current.feed_id {
        Some(feed_id) => Some(feed_id) != feed.id,
        None => !same_release(&current.title, &item.title),
    })
}

/// Why an explicit grab of `version` of `episode` must not go ahead.
///
/// `stalled` is the one active download that is about to be dropped; any other still blocks the
/// grab.
fn refusal(
    ctx: &Context<'_>,
    episode: &Episode,
    version: i64,
    other_source: bool,
    stalled: Option<&Download>,
) -> Result<Option<Refusal>> {
    let number = episode.number;
    let blocked = downloads::active_download(ctx, episode)?
        .is_some_and(|active| stalled.is_none_or(|stalled| stalled.id != active.id));
    if blocked {
        return Ok(Some(Refusal::InProgress { number }));
    }
    if is_restore(ctx, episode)? {
        return Ok(None);
    }
    let current = downloads::current(ctx, episode)?;
    let held = current.as_ref().map_or(1, |current| current.version);
    let holding = RELEASE_HOLDING.contains(&episode.status);
    if holding && current.as_ref().is_some_and(downloads::is_batch_download) {
        return Ok(Some(Refusal::CameInBatch { number }));
    }
    if other_source {
        if episode.status == EpisodeStatus::Completed && version < held {
            return Ok(Some(Refusal::OlderVersion { number, held }));
        }
        return Ok(None);
    }
    if holding && version <= held {
        return Ok(Some(Refusal::AlreadyHeld { number, held }));
    }
    Ok(None)
}

/// The feeds the show may read.
fn own_feeds(ctx: &Context<'_>, show: &Show) -> Result<Vec<Feed>> {
    let all = feeds::ranked(ctx)?;
    Ok(match show.feed_state {
        FeedState::Locked | FeedState::Pinned => all
            .into_iter()
            .filter(|feed| feed.id == show.feed_id)
            .collect(),
        FeedState::Assigned => {
            let assigned: Vec<Feed> = all
                .iter()
                .filter(|feed| feed.id == show.feed_id)
                .cloned()
                .collect();
            if assigned.is_empty() { all } else { assigned }
        }
        FeedState::None | FeedState::Candidate => all,
    })
}

/// What the show's state is, as a grab would see it now.
fn state_of(ctx: &Context<'_>, show: &Show) -> Result<Option<ShowState>> {
    let facts = facts_for(ctx, std::slice::from_ref(show))?;
    match facts.first() {
        Some(facts) => Ok(Some(grab::show_state(ctx, show, facts)?)),
        None => Ok(None),
    }
}

/// Every stored item the grabber would accept for the show, with its episode.
///
/// Reads only the feeds the show may use, so the feed lock is honoured, and never writes.
/// `feed_list` replaces that set, to list what other feeds carry. `rejected` collects, by feed
/// id, the items turned down and why.
pub fn candidates(
    ctx: &mut Context<'_>,
    show: &Show,
    feed_list: Option<&[Feed]>,
    rejected: Option<&mut Rejections>,
) -> Result<Vec<Candidate>> {
    let matcher = grab::show_matcher(ctx, show);
    candidates_with(ctx, show, &matcher, feed_list, rejected)
}

/// `candidates`, read with the show's matcher (`grab::show_matcher`).
fn candidates_with(
    ctx: &mut Context<'_>,
    show: &Show,
    matcher: &Matcher,
    feed_list: Option<&[Feed]>,
    mut rejected: Option<&mut Rejections>,
) -> Result<Vec<Candidate>> {
    let own;
    let chosen: &[Feed] = match feed_list {
        Some(list) => list,
        None => {
            own = own_feeds(ctx, show)?;
            &own
        }
    };
    if chosen.is_empty() {
        return Ok(Vec::new());
    }
    let Some(mut state) = state_of(ctx, show)? else {
        return Ok(Vec::new());
    };
    let mut found = Vec::new();
    for feed in chosen {
        let mut turned_down = Vec::new();
        let (resolved, _) = grab::resolve(
            ctx,
            show,
            &mut state,
            feed,
            ResolveOptions {
                learn: false,
                rejected: Some(&mut turned_down),
                dated: None,
                matcher: Some(matcher),
            },
        )?;
        if let Some(rejected) = rejected.as_deref_mut() {
            rejected
                .entry(id_of(feed.id))
                .or_default()
                .extend(turned_down);
        }
        for entry in resolved {
            if let Some(episode) = state.episode(entry.number).cloned() {
                found.push(Candidate {
                    feed: feed.clone(),
                    entry,
                    episode,
                });
            }
        }
    }
    Ok(found)
}

fn role_of(show: &Show, feed: &Feed) -> Role {
    if show.feed_id != feed.id {
        return Role::Discovery;
    }
    match show.feed_state {
        FeedState::Locked => Role::Locked,
        FeedState::Pinned => Role::Picked,
        FeedState::Assigned => Role::Assigned,
        FeedState::None | FeedState::Candidate => Role::Discovery,
    }
}

fn group_for<'g>(
    groups: &'g mut Vec<FeedGroup>,
    feed: &Feed,
    role: Role,
    usable: bool,
) -> &'g mut FeedGroup {
    let feed_id = id_of(feed.id);
    let at = match groups.iter().position(|group| group.feed_id == feed_id) {
        Some(at) => at,
        None => {
            groups.push(FeedGroup {
                feed_id,
                feed_name: feed.name.clone(),
                role,
                usable,
                unavailable: false,
                matches: Vec::new(),
                rejected: None,
            });
            groups.len() - 1
        }
    };
    &mut groups[at]
}

fn describe(ctx: &Context<'_>, candidate: &Candidate, usable: bool) -> Result<Match> {
    let Candidate {
        feed,
        entry,
        episode,
    } = candidate;
    let item = &entry.item;
    let (replaces, refused) = if usable {
        let replaces = other_source(ctx, episode, feed, item)?;
        let refused = refusal(ctx, episode, entry.version, replaces, None)?.is_some();
        (replaces, refused)
    } else {
        (false, true)
    };
    let current = downloads::current(ctx, episode)?;
    let this_release = current.as_ref().is_some_and(|current| {
        (has_hash(item.info_hash.as_deref()) && current.info_hash == item.info_hash)
            || same_release(&current.title, &item.title)
    });
    let display_title = match &current {
        Some(current) if this_release => current.title.clone(),
        _ => item.title.clone(),
    };
    Ok(Match {
        title: item.title.clone(),
        display_title,
        feed_id: id_of(feed.id),
        feed_name: feed.name.clone(),
        episode: entry.number,
        version: entry.version,
        episode_status: episode.status,
        this_release,
        restore: usable && is_restore(ctx, episode)?,
        replaces: replaces && !refused,
        downloadable: !refused,
    })
}

/// Releases the engine recognises, grouped by feed, for the UI.
///
/// The feeds the show may read come first, each with a role and its releases ready to download.
/// Other feeds that carry releases follow as `Other` groups, for information only: offering a
/// download from them would bypass the feed lock.
pub fn feed_matches(ctx: &mut Context<'_>, show: &Show, limit: usize) -> Result<Vec<FeedGroup>> {
    let all_feeds = feeds::ranked(ctx)?;
    let own = own_feeds(ctx, show)?;
    let own_ids: HashSet<i64> = own.iter().map(|feed| id_of(feed.id)).collect();
    let mut groups: Vec<FeedGroup> = Vec::new();

    let matcher = grab::show_matcher(ctx, show);
    let mut rejected = Rejections::new();
    let own_found = candidates_with(ctx, show, &matcher, Some(&own), Some(&mut rejected))?;
    for candidate in &own_found {
        let group = group_for(
            &mut groups,
            &candidate.feed,
            role_of(show, &candidate.feed),
            true,
        );
        if group.matches.len() < limit {
            group.matches.push(describe(ctx, candidate, true)?);
        }
    }
    // What the show's own feeds carry under its name but it does not take, and why: the answer to
    // "why wasn't it grabbed?".
    for feed in &own {
        let turned_down = rejected.get(&id_of(feed.id)).cloned().unwrap_or_default();
        let mut seen: HashSet<i64> = own_found
            .iter()
            .filter(|candidate| candidate.feed.id == feed.id)
            .filter_map(|candidate| candidate.entry.item.id)
            .collect();
        seen.extend(turned_down.iter().filter_map(|(item, _)| item.id));
        let mut notes = turned_down;
        notes.extend(grab::unmatched_releases(
            ctx,
            &matcher,
            feed,
            &seen,
            UNMATCHED_LIMIT,
        )?);
        if notes.is_empty() {
            continue;
        }
        notes.sort_by(|(first, _), (second, _)| {
            let newest = |item: &FeedItem| item.published_at.unwrap_or(item.first_seen_at);
            newest(second).cmp(&newest(first))
        });
        let group = group_for(&mut groups, feed, role_of(show, feed), true);
        group.rejected = Some(
            notes
                .into_iter()
                .take(limit)
                .map(|(item, reason)| Rejected {
                    title: item.title,
                    reason,
                    published_at: item.published_at,
                })
                .collect(),
        );
    }
    let others: Vec<Feed> = all_feeds
        .into_iter()
        .filter(|feed| !own_ids.contains(&id_of(feed.id)))
        .collect();
    for candidate in candidates_with(ctx, show, &matcher, Some(&others), None)? {
        let group = group_for(&mut groups, &candidate.feed, Role::Other, false);
        if group.matches.len() < limit {
            group.matches.push(describe(ctx, &candidate, false)?);
        }
    }
    // A feed that could not be read has nothing stored, so it would simply be missing here.
    for feed in &own {
        if feed.failing_since.is_some()
            && !groups.iter().any(|group| Some(group.feed_id) == feed.id)
        {
            group_for(&mut groups, feed, role_of(show, feed), true).unavailable = true;
        }
    }
    let (mut ordered, others): (Vec<FeedGroup>, Vec<FeedGroup>) = groups
        .into_iter()
        .partition(|group| own_ids.contains(&group.feed_id));
    ordered.extend(others);
    Ok(ordered)
}

/// Adds one named release through the normal download path.
///
/// This is an explicit user action, so the discovery grace window does not apply: a missed or
/// failed episode that is still in a feed may be fetched again. With `fuzzy` the title may be the
/// release under another name (a torrent's file name rather than the feed title).
pub fn manual_grab(
    ctx: &mut Context<'_>,
    show: &mut Show,
    title: &str,
    fuzzy: bool,
    expect_episode: Option<i64>,
) -> Result<String> {
    if show.paused {
        return Err(Refusal::Paused.into());
    }
    let wanted = title.trim();
    let found = candidates(ctx, show, None, None)?;
    let mut pick = found
        .iter()
        .position(|candidate| candidate.entry.item.title == wanted);
    if pick.is_none() && fuzzy {
        let near: Vec<usize> = found
            .iter()
            .enumerate()
            .filter(|(_, candidate)| same_release(&candidate.entry.item.title, wanted))
            .map(|(at, _)| at)
            .collect();
        match near.as_slice() {
            [] => {}
            [only] => pick = Some(*only),
            _ => return Err(Refusal::Ambiguous.into()),
        }
    }
    let Some(candidate) = pick.and_then(|at| found.get(at)) else {
        return Err(Refusal::NotInFeed.into());
    };
    let Candidate {
        feed,
        entry,
        episode,
    } = candidate;
    if let Some(expected) = expect_episode.filter(|expected| *expected != entry.number) {
        return Err(Refusal::OtherEpisode {
            found: entry.number,
            expected,
        }
        .into());
    }
    let switch_source = other_source(ctx, episode, feed, &entry.item)?;
    if let Some(reason) = refusal(ctx, episode, entry.version, switch_source, None)? {
        return Err(reason.into());
    }
    if let Some(state) = state_of(ctx, show)? {
        grab::record_evidence(ctx, show, &state, feed, entry.raw, entry.number)?;
    }
    let previous = downloads::current(ctx, episode)?;
    let mut episode = episode.clone();
    let started = downloads::start_download(
        ctx,
        show,
        feed,
        &mut episode,
        &entry.item,
        StartOptions {
            switch_source,
            ..StartOptions::default()
        },
    )?;
    let Some(started) = started else {
        return Err(not_started(&episode).into());
    };
    if matches!(show.feed_state, FeedState::None | FeedState::Candidate) {
        feeds::set_assigned(ctx, show, feed)?;
    }
    let action = match (started.supersedes_id, previous) {
        (None, _) => "download",
        (Some(_), Some(previous)) if previous.info_hash.is_none() => "restore",
        (Some(_), _) => "replacement",
    };
    Ok(format!(
        "Manually queued {action} for {} Ep {} v{}: {}",
        show.title(&ctx.settings.title_language),
        entry.number,
        entry.version,
        entry.item.title
    ))
}

fn not_started(episode: &Episode) -> Refusal {
    match episode.last_error.as_deref() {
        Some(reason) if !reason.is_empty() => Refusal::NotStarted(reason.to_owned()),
        _ => Refusal::NotStarted(NOT_STARTED.to_owned()),
    }
}

/// The title of the best release of a failed episode that can be downloaded again by hand, if
/// the feeds still carry one.
pub fn retry_release(
    ctx: &mut Context<'_>,
    show: &Show,
    episode: &Episode,
    found: Option<&[Candidate]>,
) -> Result<Option<String>> {
    let own;
    let list: &[Candidate] = match found {
        Some(list) => list,
        None => {
            own = candidates(ctx, show, None, None)?;
            &own
        }
    };
    let mut best: Option<&Candidate> = None;
    for candidate in list {
        let Candidate { feed, entry, .. } = candidate;
        if candidate.episode.id != episode.id || !has_url(&entry.item) {
            continue;
        }
        let other = other_source(ctx, episode, feed, &entry.item)?;
        if refusal(ctx, episode, entry.version, other, None)?.is_some() {
            continue;
        }
        if best.is_none_or(|best| entry.version > best.entry.version) {
            best = Some(candidate);
        }
    }
    Ok(best.map(|best| best.entry.item.title.clone()))
}

/// The episode's active download, if the last settle pass found its torrent stalled.
pub fn stalled_download(ctx: &Context<'_>, episode: &Episode) -> Result<Option<Download>> {
    Ok(downloads::active_download(ctx, episode)?
        .filter(|active| active.id.is_some_and(|id| ctx.caches.stalled.contains(&id))))
}

/// The other releases of the episode that could take a stalled download's place, best first.
///
/// They come from the feeds the show may use (so its feed lock stands) and never include the
/// stalled release itself. A batch is never offered a swap: its torrent serves other episodes
/// too.
pub fn alternatives(
    ctx: &mut Context<'_>,
    show: &Show,
    episode: &Episode,
    dl: &Download,
    found: Option<&[Candidate]>,
) -> Result<Vec<Candidate>> {
    if downloads::is_batch_download(dl) {
        return Ok(Vec::new());
    }
    let own;
    let list: &[Candidate] = match found {
        Some(list) => list,
        None => {
            own = candidates(ctx, show, None, None)?;
            &own
        }
    };
    let mut options = Vec::new();
    for candidate in list {
        let Candidate { feed, entry, .. } = candidate;
        let item = &entry.item;
        if candidate.episode.id != episode.id || !has_url(item) {
            continue;
        }
        let dead = item.title == dl.title
            || (has_hash(item.info_hash.as_deref()) && item.info_hash == dl.info_hash)
            || (feed.id == dl.feed_id && Some(&item.guid) == dl.feed_item_guid.as_ref());
        if dead {
            continue;
        }
        if dl.supersedes_id.is_some() {
            // A stalled replacement leaves the previous release in place, and a new one must
            // still be able to replace it.
            let other = other_source(ctx, episode, feed, item)?;
            if refusal(ctx, episode, entry.version, other, Some(dl))?.is_some() {
                continue;
            }
        }
        options.push(candidate.clone());
    }
    let preferred = ctx.settings.min_resolution;
    options.sort_by_key(|option| {
        (
            -option.entry.version,
            resolution_rank(&option.entry.item.title, preferred),
        )
    });
    Ok(options)
}

/// Drops the episode's stalled download and adds the best other release of it, in one step.
pub fn try_another(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
) -> Result<String> {
    if show.paused {
        return Err(Refusal::Paused.into());
    }
    let number = episode.number;
    let Some(mut dl) = stalled_download(ctx, episode)? else {
        return Err(Refusal::NotStalled { number }.into());
    };
    let options = alternatives(ctx, show, episode, &dl, None)?;
    let Some(best) = options.first() else {
        return Err(Refusal::NoAlternative { number }.into());
    };
    let switch_source = other_source(ctx, episode, &best.feed, &best.entry.item)?;
    if let Some(state) = state_of(ctx, show)? {
        grab::record_evidence(
            ctx,
            show,
            &state,
            &best.feed,
            best.entry.raw,
            best.entry.number,
        )?;
    }
    let hours = downloads::STALLED_AFTER.as_secs() / 3600;
    downloads::abandon_stalled(
        ctx,
        &mut dl,
        &format!("Stalled for over {hours} hours; replaced by another release."),
    )?;
    // Dropping the download saved its episode, so the caller's copy is out of date.
    let Some(fresh) = Episode::find(ctx.conn, "WHERE id = ?", [episode.id])? else {
        return Err(Refusal::NotStarted(NOT_STARTED.to_owned()).into());
    };
    *episode = fresh;
    let started = downloads::start_download(
        ctx,
        show,
        &best.feed,
        episode,
        &best.entry.item,
        StartOptions {
            switch_source,
            ..StartOptions::default()
        },
    )?;
    if started.is_none() {
        return Err(not_started(episode).into());
    }
    Ok(format!(
        "Dropped the stalled release of {} Ep {} and queued {}",
        show.title(&ctx.settings.title_language),
        best.entry.number,
        best.entry.item.title
    ))
}

/// Brings a finished episode with no tracked torrent under management.
///
/// The torrent is adopted if qBittorrent still holds it, otherwise the release is added again
/// from the feed.
pub fn restore_episode(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
) -> Result<String> {
    let number = episode.number;
    if !is_restore(ctx, episode)? {
        return Err(Refusal::AlreadyManaged { number }.into());
    }
    if downloads::active_download(ctx, episode)?.is_some() {
        return Err(Refusal::InProgress { number }.into());
    }
    let recorded = downloads::current(ctx, episode)?;
    let title = recorded
        .as_ref()
        .map(|current| current.title.trim().to_owned())
        .unwrap_or_default();
    let Some(mut current) = recorded.filter(|_| !title.is_empty()) else {
        return Err(Refusal::NoRecordedRelease { number }.into());
    };
    ctx.qbit()?.sync(false)?;
    let named: Vec<(String, bool)> = ctx
        .qbit()?
        .torrents()
        .values()
        .filter(|torrent| same_release(&torrent.name, &title))
        .map(|torrent| (torrent.hash.clone(), torrent.finished()))
        .collect();
    if let [(hash, finished)] = named.as_slice() {
        let labels = vec![MANAGED_TAG.to_owned(), tags::series_tag(ctx, show)?];
        ctx.qbit()?.add_tags(std::slice::from_ref(hash), &labels)?;
        current.info_hash = Some(hash.clone());
        current.state = if *finished {
            DownloadState::Done
        } else {
            DownloadState::Downloading
        };
        current.update(ctx.conn)?;
        downloads::recompute_episode(ctx, episode)?;
        return Ok(format!(
            "Restored {} Ep {number}: now managed.",
            show.title(&ctx.settings.title_language)
        ));
    }
    match manual_grab(ctx, show, &title, true, Some(number)) {
        Ok(message) => {
            // The grab changed the episode through its own copy.
            if let Some(fresh) = Episode::find(ctx.conn, "WHERE id = ?", [episode.id])? {
                *episode = fresh;
            }
            Ok(message)
        }
        Err(Error::Refused(reason)) => Err(Refusal::NotInQbit(Box::new(reason)).into()),
        Err(other) => Err(other),
    }
}
