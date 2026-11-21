//! Taking over torrents that are already in qBittorrent, and locking a show's feed from what it
//! already has.

use std::collections::BTreeSet;

use kisetsu_db::schema::{Feed, FeedItem, Show};
use kisetsu_domain::parsing::{parse_release_title, release_key};

use crate::context::Context;
use crate::error::Result;

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
