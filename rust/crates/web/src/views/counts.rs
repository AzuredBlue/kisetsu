//! The totals the header and the nav badges need.

use std::collections::{BTreeMap, HashMap, HashSet};

use jiff::SignedDuration;
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{DownloadState, Feed, Show};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::schedule::ShowFacts;
use kisetsu_services::Result;
use kisetsu_services::context::Context;
use kisetsu_services::facts::facts_for;
use serde_json::{Value, json};

use super::shows::{caught_up, standing};
use super::{all_paused, bound, episode_counts_total, status_value, value_of};

/// An add that qBittorrent still hasn't shown after this long means qBittorrent is not picking
/// up new torrents.
const STUCK_ADD_AFTER: SignedDuration = SignedDuration::from_hours(1);

/// Episodes whose add qBittorrent seems to be ignoring, from the database alone (a read cannot
/// reach qBittorrent).
///
/// Either an add that is still ADDING or RETRY_WAIT after [`STUCK_ADD_AFTER`], or a give-up after
/// repeated attempts whose episode is waiting to be tried again and has not been supplied since.
/// A replacement that gave up after 48 hours is a slow torrent, not a lost add, so it does not
/// count. Paused and completed shows are left out.
pub fn stuck_adds(ctx: &Context<'_>) -> Result<i64> {
    let live = "shows.paused = 0 AND shows.completed = 0";
    let mut stuck: HashSet<i64> = HashSet::new();
    let waiting_since = bound(ctx.now.checked_sub(STUCK_ADD_AFTER).unwrap_or(ctx.now));
    let waiting = format!(
        "SELECT downloads.episode_id FROM downloads \
         JOIN episodes ON episodes.id = downloads.episode_id \
         JOIN shows ON shows.id = episodes.show_id \
         WHERE downloads.state IN ('{}', '{}') AND downloads.created_at < ? \
         AND episodes.status != 'COMPLETED' AND {live}",
        DownloadState::Adding.name(),
        DownloadState::RetryWait.name()
    );
    let gave_up = format!(
        "SELECT downloads.episode_id FROM downloads \
         JOIN episodes ON episodes.id = downloads.episode_id \
         JOIN shows ON shows.id = episodes.show_id \
         WHERE downloads.state = 'CANCELED' AND downloads.end_reason = 'ADD_GAVE_UP' \
         AND episodes.retry_after > ? \
         AND episodes.status NOT IN ('COMPLETED', 'SKIPPED') AND {live}"
    );
    for (sql, at) in [(waiting, waiting_since), (gave_up, bound(ctx.now))] {
        let mut statement = ctx.conn.prepare_cached(&sql)?;
        let rows = statement.query_map([at], |row| row.get::<_, i64>(0))?;
        for row in rows {
            stuck.insert(row?);
        }
    }
    Ok(i64::try_from(stuck.len()).unwrap_or(i64::MAX))
}

/// The header's totals.
pub fn counts(ctx: &mut Context<'_>) -> Result<Value> {
    let all = Show::select(ctx.conn, "ORDER BY id", [])?;
    let facts: HashMap<i64, ShowFacts> = facts_for(ctx, &all)?
        .into_iter()
        .map(|facts| (facts.id, facts))
        .collect();
    let caught = caught_up(ctx, None)?;
    let mut by_status: BTreeMap<&str, i64> = BTreeMap::new();
    for show in &all {
        if let Some(facts) = show.id.and_then(|id| facts.get(&id)) {
            let status = status_value(standing(ctx, show, facts, &caught));
            *by_status.entry(status).or_default() += 1;
        }
    }
    let episodes = episode_counts_total(ctx)?;
    let episodes: BTreeMap<String, i64> = [
        EpisodeStatus::Wanted,
        EpisodeStatus::Downloading,
        EpisodeStatus::Completed,
        EpisodeStatus::Failed,
        EpisodeStatus::Missed,
        EpisodeStatus::Skipped,
    ]
    .into_iter()
    .map(|status| {
        (
            value_of(status),
            episodes.get(&status).copied().unwrap_or(0),
        )
    })
    .collect();
    let feeds = Feed::select(ctx.conn, "ORDER BY id", [])?;
    let failing: Vec<&str> = feeds
        .iter()
        .filter(|feed| feed.failing_since.is_some())
        .map(|feed| feed.name.as_str())
        .collect();
    Ok(json!({
        "shows": all.len(),
        "feeds": feeds.len(),
        "by_status": by_status,
        "episodes": episodes,
        "failing_feeds": failing,
        "all_paused": all_paused(ctx)?,
        "stuck_adds": stuck_adds(ctx)?,
    }))
}
