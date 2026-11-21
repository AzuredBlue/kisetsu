//! Plain-data views of shows for the pure scheduling and status rules.

use std::collections::{BTreeMap, HashMap};

use jiff::{SignedDuration, Timestamp};
use kisetsu_db::schema::{FeedState, Show};
use kisetsu_db::storage::Column;
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::schedule::{EpisodeFacts, ShowFacts, is_dated, release_delay};

use crate::context::Context;
use crate::error::Result;

/// The facts of each show, in the order of `shows`.
pub fn facts_for(ctx: &Context<'_>, shows: &[Show]) -> Result<Vec<ShowFacts>> {
    let mut episodes: HashMap<i64, Vec<EpisodeFacts>> = HashMap::new();
    if !shows.is_empty() {
        let ids = shows
            .iter()
            .filter_map(|show| show.id)
            .map(|id| id.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        // Only the columns the rules read: loading every episode as a full row costs far more on a big library.
        let sql = format!(
            "SELECT show_id, number, air_at, status, retry_after FROM episodes WHERE show_id IN ({ids})"
        );
        let mut statement = ctx.conn.prepare(&sql)?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let show_id = i64::read(row, 0)?;
            episodes.entry(show_id).or_default().push(EpisodeFacts {
                number: i64::read(row, 1)?,
                air_at: Option::<Timestamp>::read(row, 2)?,
                status: EpisodeStatus::read(row, 3)?,
                retry_after: Option::<Timestamp>::read(row, 4)?,
            });
        }
    }
    let language = &ctx.settings.title_language;
    Ok(shows
        .iter()
        .map(|show| {
            let mut rows = show
                .id
                .and_then(|id| episodes.remove(&id))
                .unwrap_or_default();
            rows.sort_by_key(|episode| episode.number);
            let has_completed = rows
                .iter()
                .any(|episode| episode.status == EpisodeStatus::Completed);
            let locked = show.feed_state == FeedState::Locked;
            ShowFacts {
                id: show.id.unwrap_or_default(),
                name: show.title(language),
                paused: show.paused,
                completed: show.completed,
                in_premiere: !has_completed && !locked,
                working: locked,
                anilist_status: show.anilist_status.clone(),
                next_airing_episode: show.next_airing_episode,
                next_airing_at: show.next_airing_at,
                episodes: rows,
            }
        })
        .collect())
}

/// Each show's learned delay from air time to release (`release_delay`), for the shows that have one.
///
/// Learned from the show's downloads that came from an item of its current feed, adopted ones
/// included: first versions of single episodes with an AniList air time, dated by the feed
/// itself, so when Kisetsu happened to read the feed does not count. With `show_ids` only
/// those shows are looked at.
pub fn release_delays(
    ctx: &Context<'_>,
    show_ids: Option<&[i64]>,
) -> Result<BTreeMap<i64, SignedDuration>> {
    let mut sql = String::from(
        "SELECT episodes.show_id, episodes.id, episodes.air_at, feed_items.published_at \
         FROM episodes \
         JOIN downloads ON downloads.episode_id = episodes.id \
         JOIN shows ON shows.id = episodes.show_id \
         JOIN feed_items ON feed_items.feed_id = downloads.feed_id AND feed_items.guid = downloads.feed_item_guid \
         WHERE downloads.feed_id = shows.feed_id AND downloads.version = 1 AND downloads.batch = 0 \
         AND downloads.supersedes_id IS NULL AND episodes.air_at IS NOT NULL \
         AND feed_items.published_at IS NOT NULL",
    );
    if let Some(ids) = show_ids {
        let listed = ids
            .iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        sql.push_str(&format!(" AND episodes.show_id IN ({listed})"));
    }
    // One release per episode: a retried add is the same release again.
    let mut samples: BTreeMap<i64, BTreeMap<i64, (Timestamp, Timestamp)>> = BTreeMap::new();
    let mut statement = ctx.conn.prepare(&sql)?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let (show_id, episode_id) = (i64::read(row, 0)?, i64::read(row, 1)?);
        let (air_at, published_at) = (Timestamp::read(row, 2)?, Timestamp::read(row, 3)?);
        if !is_dated(Some(published_at)) {
            continue;
        }
        let known = samples.entry(show_id).or_default().entry(episode_id);
        known
            .and_modify(|(_, known_at)| {
                if published_at < *known_at {
                    *known_at = published_at;
                }
            })
            .or_insert((air_at, published_at));
    }
    Ok(samples
        .into_iter()
        .filter_map(|(show_id, pairs)| {
            let pairs: Vec<_> = pairs.into_values().collect();
            release_delay(&pairs).map(|delay| (show_id, delay))
        })
        .collect())
}
