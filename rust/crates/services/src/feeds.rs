//! The feed list and which feed each show downloads from.
//!
//! This module is the only writer of a show's feed state. A lock is set only from evidence that
//! a real release came from the feed; an assignment is a guess and never becomes the lock.

use std::collections::{HashMap, HashSet};

use jiff::{SignedDuration, Timestamp};
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{
    ACTIVE_DOWNLOAD_STATES, DownloadState, EventKind, Feed, FeedState, OffsetSource, Show,
};
use kisetsu_domain::schedule::premiere_out_of_reach;

use crate::adoption;
use crate::context::Context;
use crate::error::{Error, Result};
use crate::events::{self, Subject};
use crate::facts::facts_for;
use crate::library;

/// A lower-ranked feed that carries a release first waits this long for a higher-ranked one.
pub const CANDIDATE_GRACE: SignedDuration = SignedDuration::from_secs(300);

/// What proved that a show's releases really come from a feed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Evidence {
    /// A release was grabbed from the feed.
    Grab,
    /// A torrent the show already had is named by exactly one feed's items.
    AdoptedUnique,
    /// The files already saved for the show are named by exactly one feed's items.
    SavedFilesUnique,
}

impl Evidence {
    fn label(self) -> &'static str {
        match self {
            Self::Grab => "grab",
            Self::AdoptedUnique => "adopted unique",
            Self::SavedFilesUnique => "saved files unique",
        }
    }
}

/// A feed that carries a release for a show, and when that release was published.
#[derive(Debug, Clone, PartialEq)]
pub struct Carrier {
    pub feed: Feed,
    pub published_at: Option<Timestamp>,
}

/// The id of a feed read from the database.
pub fn feed_id(feed: &Feed) -> i64 {
    feed.id.unwrap_or_default()
}

pub fn feed_by_id(ctx: &Context<'_>, id: Option<i64>) -> Result<Option<Feed>> {
    match id {
        Some(id) => Ok(Feed::find(ctx.conn, "WHERE id = ?", [id])?),
        None => Ok(None),
    }
}

/// The feeds, best first.
pub fn ranked(ctx: &Context<'_>) -> Result<Vec<Feed>> {
    Ok(Feed::select(ctx.conn, "ORDER BY priority, id", [])?)
}

fn reprioritise(ctx: &Context<'_>) -> Result<()> {
    set_priorities(ctx, ranked(ctx)?)
}

/// Gives the feeds priorities 1.. in the order given.
fn set_priorities(ctx: &Context<'_>, order: Vec<Feed>) -> Result<()> {
    for (index, mut feed) in order.into_iter().enumerate() {
        let priority = i64::try_from(index + 1).unwrap_or(i64::MAX);
        if feed.priority != priority {
            feed.priority = priority;
            feed.update(ctx.conn)?;
        }
    }
    Ok(())
}

/// Adds a feed at the lowest priority; a URL that is already listed is returned as it is.
pub fn add_feed(
    ctx: &mut Context<'_>,
    name: &str,
    url: &str,
    qbit_path: Option<&str>,
) -> Result<Feed> {
    if let Some(existing) = Feed::find(ctx.conn, "WHERE url = ?", [url])? {
        return Ok(existing);
    }
    let mut feed = Feed {
        id: None,
        name: name.to_owned(),
        url: url.to_owned(),
        priority: i64::try_from(ranked(ctx)?.len() + 1).unwrap_or(i64::MAX),
        qbit_path: qbit_path.map(str::to_owned),
        last_fetch_at: None,
        last_ok_at: None,
        failing_since: None,
        last_error: None,
    };
    feed.id = Some(feed.insert(ctx.conn)?);
    ctx.emit(&["feeds"]);
    Ok(feed)
}

/// Renames a feed or points it at another URL. Items already stored from the old URL stay.
pub fn edit_feed(
    ctx: &mut Context<'_>,
    feed: &mut Feed,
    name: Option<&str>,
    url: Option<&str>,
) -> Result<()> {
    if let Some(url) = url.filter(|url| *url != feed.url) {
        if let Some(clash) = Feed::find(ctx.conn, "WHERE url = ? AND id != ?", (url, feed.id))? {
            return Err(Error::FeedUrlTaken { name: clash.name });
        }
        feed.url = url.to_owned();
        feed.failing_since = None;
        feed.last_error = None;
        // Not read at its new address yet, so the next pass reads it.
        feed.last_fetch_at = None;
    }
    if let Some(name) = name.filter(|name| *name != feed.name) {
        feed.name = name.to_owned();
    }
    feed.update(ctx.conn)?;
    ctx.emit(&["feeds"]);
    Ok(())
}

/// The last part of a path in qBittorrent's RSS tree.
fn leaf(path: &str) -> &str {
    let after_backslash = path.rsplit('\\').next().unwrap_or(path);
    after_backslash
        .rsplit('/')
        .next()
        .unwrap_or(after_backslash)
}

/// Gives the listed feeds priorities 1..n in that order; feeds not listed follow in their old order.
pub fn reorder_feeds(ctx: &mut Context<'_>, feed_ids: &[i64]) -> Result<()> {
    let mut remaining = ranked(ctx)?;
    let mut order = Vec::with_capacity(remaining.len());
    for wanted in feed_ids {
        if let Some(at) = remaining.iter().position(|feed| feed.id == Some(*wanted)) {
            order.push(remaining.remove(at));
        }
    }
    order.extend(remaining);
    set_priorities(ctx, order)?;
    ctx.emit(&["feeds", "shows"]);
    Ok(())
}

/// Adds the feeds in qBittorrent's RSS tree that Kisetsu does not list yet; returns the new ones.
pub fn import_from_qbit(ctx: &mut Context<'_>) -> Result<Vec<Feed>> {
    let mut known: HashSet<String> = ranked(ctx)?.into_iter().map(|feed| feed.url).collect();
    let mut added = Vec::new();
    for item in ctx.qbit()?.rss_feeds()? {
        if item.url.is_empty() || !known.insert(item.url.clone()) {
            continue;
        }
        added.push(add_feed(
            ctx,
            leaf(&item.path),
            &item.url,
            Some(&item.path),
        )?);
    }
    Ok(added)
}

/// Removes a feed. Shows that were using it go back to discovery; returns how many.
pub fn forget_feed(ctx: &mut Context<'_>, feed: &Feed) -> Result<usize> {
    let shows = Show::select(ctx.conn, "WHERE feed_id = ? ORDER BY id", [feed.id])?;
    for mut show in shows.iter().cloned() {
        clear(&mut show);
        show.update(ctx.conn)?;
    }
    ctx.conn
        .execute("DELETE FROM episode_offsets WHERE feed_id = ?", [feed.id])?;
    if let Some(known) = ctx.caches.known_items.as_mut() {
        known.retain(|(feed_of_item, _)| Some(*feed_of_item) != feed.id);
    }
    ctx.conn
        .execute("DELETE FROM feeds WHERE id = ?", [feed.id])?;
    reprioritise(ctx)?;
    ctx.emit(&["feeds", "shows"]);
    Ok(shows.len())
}

fn clear(show: &mut Show) {
    show.feed_id = None;
    show.feed_state = FeedState::None;
    show.candidate_since = None;
    show.learned_name = None;
    show.learned_group = None;
}

/// Deletes the offsets of the show from the given sources, of one feed or of every feed.
fn drop_offsets(
    ctx: &Context<'_>,
    show: &Show,
    feed_id: Option<i64>,
    sources: &[OffsetSource],
) -> Result<()> {
    let sources = sources
        .iter()
        .map(|source| format!("'{}'", source.name()))
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!("DELETE FROM episode_offsets WHERE show_id = ? AND source IN ({sources})");
    match feed_id {
        Some(feed_id) => ctx
            .conn
            .execute(&format!("{sql} AND feed_id = ?"), (show.id, feed_id))?,
        None => ctx.conn.execute(&sql, [show.id])?,
    };
    Ok(())
}

/// The carriers that may be used, best-ranked feed first.
pub fn carriers_in_order<'a>(
    ctx: &Context<'_>,
    carriers: &'a [Carrier],
    excluded: &HashSet<i64>,
) -> Result<Vec<&'a Carrier>> {
    let rank: HashMap<i64, usize> = ranked(ctx)?
        .iter()
        .enumerate()
        .map(|(index, feed)| (feed_id(feed), index))
        .collect();
    let mut usable: Vec<&Carrier> = carriers
        .iter()
        .filter(|carrier| !excluded.contains(&feed_id(&carrier.feed)))
        .collect();
    usable.sort_by_key(|carrier| {
        rank.get(&feed_id(&carrier.feed))
            .copied()
            .unwrap_or(rank.len())
    });
    Ok(usable)
}

/// Decides which feed a show without a proven one may download from now.
///
/// `carriers` are the feeds that carry a release the show could use. The best-ranked feed is
/// taken at once when it is the top-ranked feed overall; a lower-ranked one is only a candidate
/// until the grace period, counted from when it published the release, has passed without a
/// higher-ranked feed carrying it. Returns the feed to grab from, or `None` while the show has
/// to wait.
pub fn assign(
    ctx: &mut Context<'_>,
    show: &mut Show,
    carriers: &[Carrier],
    excluded: &HashSet<i64>,
) -> Result<Option<Feed>> {
    if show.feed_is_fixed() {
        return feed_by_id(ctx, show.feed_id);
    }
    let usable = carriers_in_order(ctx, carriers, excluded)?;
    let Some(best) = usable.first() else {
        return if show.feed_state == FeedState::Assigned {
            feed_by_id(ctx, show.feed_id)
        } else {
            Ok(None)
        };
    };
    if show.feed_state == FeedState::Assigned
        && usable.iter().any(|carrier| carrier.feed.id == show.feed_id)
    {
        return feed_by_id(ctx, show.feed_id);
    }
    let top = ranked(ctx)?
        .into_iter()
        .find(|feed| !excluded.contains(&feed_id(feed)));
    if top.is_some_and(|top| top.id == best.feed.id) {
        set_assigned(ctx, show, &best.feed)?;
        return Ok(Some(best.feed.clone()));
    }
    if show.feed_state != FeedState::Candidate || show.feed_id != best.feed.id {
        show.feed_id = best.feed.id;
        show.feed_state = FeedState::Candidate;
        show.candidate_since = Some(best.published_at.unwrap_or(ctx.now));
        show.update(ctx.conn)?;
        ctx.emit(&["shows"]);
    }
    let since = show.candidate_since.unwrap_or(ctx.now);
    if since
        .checked_add(CANDIDATE_GRACE)
        .is_ok_and(|due| due > ctx.now)
    {
        return Ok(None);
    }
    set_assigned(ctx, show, &best.feed)?;
    Ok(Some(best.feed.clone()))
}

/// When the earliest pending candidate becomes assignable, for the scheduler's timer.
pub fn next_candidate_due(ctx: &Context<'_>) -> Result<Option<Timestamp>> {
    let candidates = Show::select(
        ctx.conn,
        "WHERE feed_state = ?",
        [FeedState::Candidate.name()],
    )?;
    Ok(candidates
        .iter()
        .filter_map(|show| {
            show.candidate_since
                .unwrap_or(ctx.now)
                .checked_add(CANDIDATE_GRACE)
                .ok()
        })
        .min())
}

/// Makes a feed the show's guess: it downloads from it, but an assignment is not the lock.
pub fn set_assigned(ctx: &mut Context<'_>, show: &mut Show, feed: &Feed) -> Result<()> {
    if show.feed_state == FeedState::Assigned && show.feed_id == feed.id {
        return Ok(());
    }
    show.feed_id = feed.id;
    show.feed_state = FeedState::Assigned;
    show.candidate_since = None;
    show.update(ctx.conn)?;
    ctx.emit(&["shows"]);
    Ok(())
}

/// Makes a feed the show's only source because a real release came from it.
///
/// A show pinned or locked to another feed is not moved (only `pin` with `release_lock` does
/// that); returns whether the lock was set.
pub fn lock(
    ctx: &mut Context<'_>,
    show: &mut Show,
    feed: &Feed,
    evidence: Evidence,
    learned_name: Option<&str>,
    learned_group: Option<&str>,
) -> Result<bool> {
    if show.feed_is_fixed() && show.feed_id != feed.id {
        return Ok(false);
    }
    let mut changed = false;
    if !(show.feed_state == FeedState::Locked && show.feed_id == feed.id) {
        show.feed_id = feed.id;
        show.feed_state = FeedState::Locked;
        show.candidate_since = None;
        changed = true;
    }
    changed |= learn(&mut show.learned_name, learned_name);
    changed |= learn(&mut show.learned_group, learned_group);
    if changed {
        show.update(ctx.conn)?;
        ctx.emit(&["shows"]);
        events::record(
            ctx,
            EventKind::Feed,
            &format!("Locked to '{}' ({}).", feed.name, evidence.label()),
            Subject {
                show_id: show.id,
                feed_id: feed.id,
                ..Subject::default()
            },
        )?;
    }
    Ok(changed)
}

/// The user picks the feed. Moving off a locked feed needs `release_lock`.
pub fn pin(ctx: &mut Context<'_>, show: &mut Show, feed: &Feed, release_lock: bool) -> Result<()> {
    if show.feed_state == FeedState::Locked && show.feed_id != feed.id {
        if !release_lock {
            return Err(Error::FeedLocked {
                title: show.title("english"),
            });
        }
        drop_offsets(
            ctx,
            show,
            show.feed_id,
            &[OffsetSource::Inferred, OffsetSource::Confirmed],
        )?;
        show.learned_name = None;
        show.learned_group = None;
    }
    if !(show.feed_state == FeedState::Locked && show.feed_id == feed.id) {
        show.feed_id = feed.id;
        show.feed_state = FeedState::Pinned;
    }
    show.candidate_since = None;
    show.update(ctx.conn)?;
    ctx.emit(&["shows"]);
    Ok(())
}

/// Back to automatic discovery: clears a pin or a lock and what the lock taught.
pub fn release(ctx: &mut Context<'_>, show: &mut Show) -> Result<()> {
    drop_offsets(
        ctx,
        show,
        None,
        &[OffsetSource::Inferred, OffsetSource::Confirmed],
    )?;
    clear(show);
    show.update(ctx.conn)?;
    ctx.emit(&["shows"]);
    Ok(())
}

/// Drops the lock on a feed once nothing downloaded from it is left (every such grab was
/// canceled).
///
/// Returns whether anything was released. A pin stays, and so do offsets the user set.
pub fn unwind_unproven(
    ctx: &mut Context<'_>,
    show: &mut Show,
    feed_id: Option<i64>,
) -> Result<bool> {
    let (Some(feed_id), Some(show_id)) = (feed_id, show.id) else {
        return Ok(false);
    };
    let states = ACTIVE_DOWNLOAD_STATES
        .iter()
        .chain(&[DownloadState::Done, DownloadState::Superseded])
        .map(|state| format!("'{}'", state.name()))
        .collect::<Vec<_>>()
        .join(", ");
    let holder = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT downloads.id FROM downloads \
             JOIN episodes ON episodes.id = downloads.episode_id \
             WHERE episodes.show_id = ? AND downloads.feed_id = ? AND downloads.state IN ({states}) \
             LIMIT 1"
        ))?
        .exists((show_id, feed_id))?;
    if holder {
        return Ok(false);
    }
    let locked_here = show.feed_state == FeedState::Locked && show.feed_id == Some(feed_id);
    if locked_here && saved_files_prove(ctx, show, feed_id)? {
        // The files on disk still came from this feed: that evidence did not go away with the canceled grab.
        return Ok(false);
    }
    drop_offsets(
        ctx,
        show,
        Some(feed_id),
        &[OffsetSource::Inferred, OffsetSource::Confirmed],
    )?;
    if locked_here {
        clear(show);
        show.update(ctx.conn)?;
        ctx.emit(&["shows"]);
        return Ok(true);
    }
    Ok(false)
}

/// Whether the show's saved files point to this one feed, the evidence a saved-files lock rests on.
fn saved_files_prove(ctx: &Context<'_>, show: &Show, feed_id: i64) -> Result<bool> {
    let names = library::saved_release_names(show, &ctx.settings);
    if names.is_empty() {
        return Ok(false);
    }
    let found = adoption::feed_from_saved_files(ctx, show, &names, &[])?;
    Ok(found.is_some_and(|found| found.feed.id == Some(feed_id)))
}

/// Puts back to discovery the shows matched before they started airing.
///
/// Such a match is an earlier season's release. A show that is locked, pinned or has a download
/// is left alone.
pub fn unwind_unreleased(ctx: &mut Context<'_>) -> Result<Vec<Show>> {
    let candidates = Show::select(
        ctx.conn,
        "WHERE completed = 0 AND feed_state IN ('CANDIDATE', 'ASSIGNED') ORDER BY id",
        [],
    )?;
    if candidates.is_empty() {
        return Ok(Vec::new());
    }
    let facts = facts_for(ctx, &candidates)?;
    let downloaded: HashSet<i64> = ctx
        .conn
        .prepare_cached(
            "SELECT DISTINCT episodes.show_id FROM episodes \
             JOIN downloads ON downloads.episode_id = episodes.id",
        )?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let mut undone = Vec::new();
    for (mut show, facts) in candidates.into_iter().zip(&facts) {
        if downloaded.contains(&facts.id) || !premiere_out_of_reach(facts, ctx.now) {
            continue;
        }
        clear(&mut show);
        show.update(ctx.conn)?;
        events::record(
            ctx,
            EventKind::Info,
            "Put back to Upcoming: it hasn't started airing, so the releases it matched were from an earlier season.",
            Subject {
                show_id: show.id,
                ..Subject::default()
            },
        )?;
        undone.push(show);
    }
    if !undone.is_empty() {
        ctx.emit(&["shows"]);
    }
    Ok(undone)
}

/// Remembers what a feed calls a show's releases; returns whether that changed anything.
fn learn(learned: &mut Option<String>, name: Option<&str>) -> bool {
    match name.filter(|name| !name.is_empty()) {
        Some(name) if learned.as_deref() != Some(name) => {
            *learned = Some(name.to_owned());
            true
        }
        _ => false,
    }
}
