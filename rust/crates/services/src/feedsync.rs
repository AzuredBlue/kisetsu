//! Reading the feeds: what a read does to the stored items and the feeds' health.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use kisetsu_db::schema::{Feed, FeedState, Show};
use kisetsu_db::storage::{Column, format_timestamp};
use kisetsu_domain::schedule::{HUNT_WINDOW, HuntPlan, hunt_plan};
use tracing::{info, warn};

use crate::context::Context;
use crate::error::Result;
use crate::facts::facts_for;
use crate::feeds::feed_id;
use crate::ingest;

/// A feed no show needs is still read this often, for v2s, batches and releases ahead of their
/// air time (only the feeds of locked and pinned shows while every followed show has one: see
/// `idle_read`).
const IDLE_FEED_REFRESH: SignedDuration = SignedDuration::from_hours(6);
/// An aired episode still missing keeps its show's feeds read this long; then only the idle read looks.
const MISSING_FOR: SignedDuration = SignedDuration::from_hours(3 * 24);
/// Past the day it is hunted for, a still missing episode's feeds are read this often instead of
/// on every routine read.
const LATE_READ_EVERY: SignedDuration = SignedDuration::from_hours(1);
/// A needed feed is read again this much before its interval has passed since its last read,
/// since a read is stamped a little after the routine read was planned; a feed the hunt read more
/// recently than that is left to it.
const READ_SLACK: SignedDuration = SignedDuration::from_mins(2);
/// How many of a read's titles are looked up before it is called a gap.
const GAP_TITLES_CHECKED: usize = 500;

/// Why a read failed, in words for the user.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FetchFailure {
    pub message: String,
    /// Whether the cause may pass on its own.
    pub transient: bool,
}

impl From<&FeedFetchError> for FetchFailure {
    fn from(error: &FeedFetchError) -> Self {
        Self {
            message: error.to_string(),
            transient: error.is_transient(),
        }
    }
}

/// What reading one feed gave.
pub type FetchResult = std::result::Result<Vec<FeedArticle>, FetchFailure>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedRef {
    pub id: i64,
    pub name: String,
    pub url: String,
}

impl From<&Feed> for FeedRef {
    fn from(feed: &Feed) -> Self {
        Self {
            id: feed_id(feed),
            name: feed.name.clone(),
            url: feed.url.clone(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Outcome {
    pub new: usize,
    pub matched_show_ids: BTreeSet<i64>,
    /// Shows a feed their searches left out turned out to carry (`IngestResult::reconsider`).
    pub reconsider: BTreeSet<i64>,
    /// Feeds whose read had nothing in common with what was stored: items may have scrolled past unseen.
    pub gaps: Vec<i64>,
    pub failed: BTreeMap<i64, String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HuntInfo {
    pub show_ids: Vec<i64>,
    pub feeds: Vec<FeedRef>,
    pub plan: HuntPlan,
    pub interval_minutes: i64,
    /// Hunted shows whose feeds were all read less than an interval ago: the seconds until the
    /// first is due again.
    pub wait_seconds: Option<f64>,
}

/// Whether the show reads one feed: its own, once it has one.
fn has_own_feed(state: FeedState, feed_id: Option<i64>) -> Option<i64> {
    match state {
        FeedState::Locked | FeedState::Pinned | FeedState::Assigned => feed_id,
        FeedState::None | FeedState::Candidate => None,
    }
}

/// The feeds a show with a recently aired, still missing episode reads (its own, or every feed
/// while it has none), and how often: every routine read while that episode is hunted for, then
/// every `LATE_READ_EVERY`.
fn needed(ctx: &Context<'_>, all_feeds: &[Feed]) -> Result<BTreeMap<i64, SignedDuration>> {
    let mut statement = ctx.conn.prepare_cached(
        "SELECT shows.feed_id, shows.feed_state, episodes.air_at FROM shows \
         JOIN episodes ON episodes.show_id = shows.id \
         WHERE shows.completed = 0 AND shows.paused = 0 \
         AND episodes.status IN ('WANTED', 'MISSED') AND episodes.air_at IS NOT NULL \
         AND episodes.air_at <= ?1 AND episodes.air_at >= ?2",
    )?;
    let oldest = ctx.now.saturating_sub(MISSING_FOR).unwrap_or(ctx.now);
    let mut rows = statement.query((format_timestamp(ctx.now), format_timestamp(oldest)))?;
    let routine = SignedDuration::from_mins(ctx.settings.feed_refresh_minutes);
    let late = routine.max(LATE_READ_EVERY);
    let mut needed: BTreeMap<i64, SignedDuration> = BTreeMap::new();
    while let Some(row) = rows.next()? {
        let feed_of_show = Option::<i64>::read(row, 0)?;
        let state = FeedState::read(row, 1)?;
        let air_at = Timestamp::read(row, 2)?;
        let every = if ctx.now.duration_since(air_at) <= HUNT_WINDOW {
            routine
        } else {
            late
        };
        let ids: Vec<i64> = match has_own_feed(state, feed_of_show) {
            Some(id) => vec![id],
            None => all_feeds.iter().map(feed_id).collect(),
        };
        for id in ids {
            let slot = needed.entry(id).or_insert(every);
            *slot = (*slot).min(every);
        }
    }
    Ok(needed)
}

/// The feeds the idle read keeps up: every feed while a followed show may still take its
/// releases from any, else only the feeds the locked and pinned shows read, since nothing
/// another feed carries can be theirs.
fn idle_read(ctx: &Context<'_>, all_feeds: &[Feed]) -> Result<HashSet<i64>> {
    let shows: Vec<Show> = ingest::followed_shows(ctx)?
        .into_iter()
        .filter(|show| !show.paused)
        .collect();
    if shows
        .iter()
        .any(|show| !show.feed_is_fixed() || show.feed_id.is_none())
    {
        return Ok(all_feeds.iter().map(feed_id).collect());
    }
    Ok(shows.iter().filter_map(|show| show.feed_id).collect())
}

/// When the routine read next has a feed to read: after `interval` while a show needs one, else
/// when the first feed has been idle long enough or the next wanted episode airs (`None`:
/// neither).
pub fn next_read_at(ctx: &Context<'_>, interval: SignedDuration) -> Result<Option<Timestamp>> {
    let all_feeds = Feed::select(ctx.conn, "ORDER BY id", [])?;
    if all_feeds.is_empty() {
        return Ok(None);
    }
    if !needed(ctx, &all_feeds)?.is_empty() {
        return Ok(ctx.now.checked_add(interval).ok());
    }
    let idle = idle_read(ctx, &all_feeds)?;
    let mut times: Vec<Timestamp> = all_feeds
        .iter()
        .filter(|feed| feed.last_fetch_at.is_none() || idle.contains(&feed_id(feed)))
        .map(|feed| match feed.last_fetch_at {
            None => ctx.now,
            Some(read) => read.checked_add(IDLE_FEED_REFRESH).unwrap_or(read),
        })
        .collect();
    let next_air = ctx
        .conn
        .prepare_cached(
            "SELECT min(episodes.air_at) FROM episodes JOIN shows ON shows.id = episodes.show_id \
             WHERE shows.completed = 0 AND shows.paused = 0 \
             AND episodes.status IN ('WANTED', 'MISSED') AND episodes.air_at > ?",
        )?
        .query_row([format_timestamp(ctx.now)], |row| {
            Option::<Timestamp>::read(row, 0)
        })?;
    times.extend(next_air);
    Ok(times.into_iter().min().map(|first| first.max(ctx.now)))
}

/// The feeds to read now, in priority order: those a show needs, and the rest once they have
/// been idle long enough.
pub fn plan_fetch(ctx: &Context<'_>, everything: bool) -> Result<Vec<FeedRef>> {
    let all_feeds = Feed::select(ctx.conn, "ORDER BY priority, id", [])?;
    if everything {
        return Ok(all_feeds.iter().map(FeedRef::from).collect());
    }
    let needed = needed(ctx, &all_feeds)?;
    let idle = idle_read(ctx, &all_feeds)?;
    Ok(all_feeds
        .iter()
        .filter(|feed| {
            let id = feed_id(feed);
            let Some(read) = feed.last_fetch_at else {
                return true;
            };
            let since = ctx.now.duration_since(read);
            needed
                .get(&id)
                .is_some_and(|every| since >= *every - READ_SLACK)
                || (idle.contains(&id) && since >= IDLE_FEED_REFRESH)
        })
        .map(FeedRef::from)
        .collect())
}

/// Stores what the reads returned and records each feed's health. A failed result carries the
/// error of the read.
///
/// A feed a show reads that failed transiently is left unread, so the next routine read tries it
/// again.
pub fn apply_fetch(ctx: &mut Context<'_>, results: &[(i64, FetchResult)]) -> Result<Outcome> {
    let mut outcome = Outcome::default();
    let stored_feeds: HashSet<i64> = ingest::known_items(ctx)?
        .iter()
        .map(|(feed, _)| *feed)
        .collect();
    let mut used: Option<HashSet<i64>> = None;
    for (id, result) in results {
        let Some(mut feed) = Feed::find(ctx.conn, "WHERE id = ?", [id])? else {
            continue;
        };
        feed.last_fetch_at = Some(ctx.now);
        let articles = match result {
            Err(failure) => {
                if failure.transient {
                    if used.is_none() {
                        let all_feeds = Feed::select(ctx.conn, "ORDER BY id", [])?;
                        let mut feeds: HashSet<i64> =
                            needed(ctx, &all_feeds)?.into_keys().collect();
                        feeds.extend(idle_read(ctx, &all_feeds)?);
                        used = Some(feeds);
                    }
                    if used.as_ref().is_some_and(|feeds| feeds.contains(id)) {
                        feed.last_fetch_at = None;
                    }
                }
                if feed.failing_since.is_none() {
                    warn!(
                        "Feed '{}' could not be read: {}",
                        feed.name, failure.message
                    );
                }
                feed.last_error = Some(failure.message.clone());
                feed.failing_since = feed.failing_since.or(Some(ctx.now));
                outcome.failed.insert(*id, failure.message.clone());
                feed.update(ctx.conn)?;
                ctx.emit(&["feeds"]);
                continue;
            }
            Ok(articles) => articles,
        };
        if feed.failing_since.is_some() {
            info!("Feed '{}' can be read again.", feed.name);
        }
        if feed.failing_since.is_some() || feed.last_error.as_deref().is_some_and(|e| !e.is_empty())
        {
            ctx.emit(&["feeds"]);
        }
        feed.last_ok_at = Some(ctx.now);
        feed.failing_since = None;
        feed.last_error = None;
        feed.update(ctx.conn)?;
        if !articles.is_empty() && stored_feeds.contains(id) && is_gap(ctx, &feed, articles)? {
            outcome.gaps.push(*id);
        }
        let dated = articles.iter().filter_map(|article| article.published_at);
        if let Some(oldest) = dated.min() {
            ctx.caches.read_windows.insert(*id, (oldest, ctx.now));
        }
        let stored = ingest::ingest(ctx, &feed, articles)?;
        outcome.new += stored.new;
        outcome.matched_show_ids.extend(stored.show_ids);
        outcome.reconsider.extend(stored.reconsider);
    }
    Ok(outcome)
}

/// Whether a read has nothing in common with what the feed has stored: no guid known, and no
/// title either, so items may have scrolled past unseen.
fn is_gap(ctx: &Context<'_>, feed: &Feed, articles: &[FeedArticle]) -> Result<bool> {
    let id = feed_id(feed);
    let known = ctx.caches.known_items.as_ref();
    if articles
        .iter()
        .any(|article| known.is_some_and(|known| known.contains(&(id, article.guid.clone()))))
    {
        return Ok(false);
    }
    Ok(!titles_stored(ctx, feed, articles)?)
}

/// Whether the feed already holds an item titled like one of these: the same releases under new
/// guids, so nothing scrolled past.
fn titles_stored(ctx: &Context<'_>, feed: &Feed, articles: &[FeedArticle]) -> Result<bool> {
    let mut seen = HashSet::new();
    let titles: Vec<&str> = articles
        .iter()
        .map(|article| article.title.as_str())
        .filter(|title| seen.insert(*title))
        .take(GAP_TITLES_CHECKED)
        .collect();
    let marks = vec!["?"; titles.len()].join(", ");
    let sql = format!("SELECT 1 FROM feed_items WHERE feed_id = ? AND title IN ({marks}) LIMIT 1");
    let params = std::iter::once(kisetsu_db::rusqlite::types::Value::Integer(feed_id(feed))).chain(
        titles
            .into_iter()
            .map(|title| kisetsu_db::rusqlite::types::Value::Text(title.to_owned())),
    );
    Ok(ctx
        .conn
        .prepare(&sql)?
        .exists(kisetsu_db::rusqlite::params_from_iter(params))?)
}

/// The shows in their hunting window, the feeds they may read, and when the next show starts
/// hunting.
pub fn plan_hunt(ctx: &Context<'_>) -> Result<HuntInfo> {
    let shows = Show::select(
        ctx.conn,
        "WHERE completed = 0 AND paused = 0 ORDER BY id",
        [],
    )?;
    let facts = facts_for(ctx, &shows)?;
    let plan = hunt_plan(&facts, ctx.now, ctx.tolerance_hours());
    let all_feeds = Feed::select(ctx.conn, "ORDER BY priority, id", [])?;
    let mut wanted: Vec<&Feed> = Vec::new();
    for show_id in &plan.hunting {
        let Some(show) = shows.iter().find(|show| show.id == Some(*show_id)) else {
            continue;
        };
        let chosen: Vec<&Feed> = match has_own_feed(show.feed_state, show.feed_id) {
            Some(own) => all_feeds
                .iter()
                .filter(|feed| feed.id == Some(own))
                .collect(),
            None => all_feeds.iter().collect(),
        };
        for feed in chosen {
            if !wanted.contains(&feed) {
                wanted.push(feed);
            }
        }
    }
    let interval = ctx.settings.hunt_interval_minutes.max(1);
    // A feed another read fetched moments ago (the startup read, say) has nothing new yet.
    let due_at = |feed: &Feed| match feed.last_fetch_at {
        Some(read) => read
            .checked_add(SignedDuration::from_mins(interval))
            .unwrap_or(read),
        None => ctx.now,
    };
    let due: Vec<FeedRef> = wanted
        .iter()
        .filter(|feed| due_at(feed) <= ctx.now)
        .map(|feed| FeedRef::from(*feed))
        .collect();
    let wait_seconds = if due.is_empty() {
        wanted
            .iter()
            .map(|feed| due_at(feed))
            .min()
            .map(|first| first.duration_since(ctx.now).as_secs_f64())
    } else {
        None
    };
    Ok(HuntInfo {
        show_ids: plan.hunting.clone(),
        feeds: due,
        plan,
        interval_minutes: interval,
        wait_seconds,
    })
}
