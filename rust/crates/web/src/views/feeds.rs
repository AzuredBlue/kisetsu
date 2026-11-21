//! The Feeds page and the items read from the feeds.

use std::collections::HashMap;

use jiff::SignedDuration;
use kisetsu_db::rusqlite::params_from_iter;
use kisetsu_db::rusqlite::types::Value as SqlValue;
use kisetsu_db::schema::{Download, DownloadState, Feed, FeedItem, Show};
use kisetsu_services::Result;
use kisetsu_services::context::Context;
use serde_json::{Value, json};

use super::{bound, iso};

/// What the Releases list says of an item whose release was taken.
fn download_label(state: DownloadState) -> Option<&'static str> {
    match state {
        DownloadState::Adding | DownloadState::Verifying | DownloadState::RetryWait => {
            Some("Queued")
        }
        DownloadState::Downloading => Some("Downloading"),
        DownloadState::Done => Some("Downloaded"),
        _ => None,
    }
}

fn counts_by_feed(ctx: &Context<'_>, sql: &str) -> Result<HashMap<i64, i64>> {
    let mut statement = ctx.conn.prepare_cached(sql)?;
    let rows = statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
    Ok(rows.collect::<std::result::Result<_, _>>()?)
}

/// The feeds, with how many shows use each and how many items were stored.
pub fn feeds(ctx: &Context<'_>) -> Result<Value> {
    let usage = counts_by_feed(
        ctx,
        "SELECT feed_id, COUNT(id) FROM shows WHERE feed_id IS NOT NULL GROUP BY feed_id",
    )?;
    let locked = counts_by_feed(
        ctx,
        "SELECT feed_id, COUNT(id) FROM shows WHERE feed_state = 'LOCKED' AND feed_id IS NOT NULL GROUP BY feed_id",
    )?;
    let stored = counts_by_feed(
        ctx,
        "SELECT feed_id, COUNT(id) FROM feed_items GROUP BY feed_id",
    )?;
    let rows: Vec<Value> = Feed::select(ctx.conn, "ORDER BY priority, id", [])?
        .into_iter()
        .map(|feed| {
            let id = feed.id.unwrap_or_default();
            json!({
                "id": feed.id,
                "name": feed.name,
                "url": feed.url,
                "priority": feed.priority,
                "qbit_path": feed.qbit_path,
                "shows": usage.get(&id).copied().unwrap_or(0),
                "locked": locked.get(&id).copied().unwrap_or(0),
                "items": stored.get(&id).copied().unwrap_or(0),
                "last_ok_at": iso(feed.last_ok_at),
                "failing_since": iso(feed.failing_since),
                "last_error": feed.last_error,
            })
        })
        .collect();
    Ok(Value::Array(rows))
}

/// The articles read from the feeds, newest first, with the shows they belong to and what became
/// of them.
pub fn feed_items(
    ctx: &Context<'_>,
    feed_id: Option<i64>,
    matched: bool,
    limit: i64,
    offset: i64,
) -> Result<Value> {
    let mut conditions = Vec::new();
    let mut params: Vec<SqlValue> = Vec::new();
    if let Some(feed_id) = feed_id {
        conditions.push("feed_id = ?");
        params.push(feed_id.into());
    }
    if matched {
        conditions.push("id IN (SELECT item_id FROM feed_item_matches)");
    }
    let filter = if conditions.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", conditions.join(" AND "))
    };
    let total: i64 = ctx
        .conn
        .prepare_cached(&format!("SELECT COUNT(*) FROM feed_items {filter}"))?
        .query_row(params_from_iter(&params), |row| row.get(0))?;
    let mut page_params = params.clone();
    page_params.extend([SqlValue::from(limit), SqlValue::from(offset)]);
    let rows = FeedItem::select(
        ctx.conn,
        &format!(
            "{filter} ORDER BY COALESCE(published_at, first_seen_at) DESC, id DESC LIMIT ? OFFSET ?"
        ),
        params_from_iter(page_params),
    )?;
    let mut recent_params: Vec<SqlValue> = vec![bound(
        ctx.now
            .checked_sub(SignedDuration::from_hours(24))
            .unwrap_or(ctx.now),
    )];
    let recent_filter = match feed_id {
        Some(feed_id) => {
            recent_params.push(feed_id.into());
            " AND feed_id = ?"
        }
        None => "",
    };
    let new_recent: i64 = ctx
        .conn
        .prepare_cached(&format!(
            "SELECT COUNT(id) FROM feed_items WHERE first_seen_at >= ?{recent_filter}"
        ))?
        .query_row(params_from_iter(recent_params), |row| row.get(0))?;

    let mut belongs: HashMap<i64, Vec<Value>> = HashMap::new();
    let mut taken: HashMap<(i64, String), &'static str> = HashMap::new();
    if !rows.is_empty() {
        let ids = rows
            .iter()
            .filter_map(|row| row.id.map(|id| id.to_string()))
            .collect::<Vec<_>>()
            .join(", ");
        let language = &ctx.settings.title_language;
        let pairs: Vec<(i64, i64)> = ctx
            .conn
            .prepare_cached(&format!(
                "SELECT item_id, show_id FROM feed_item_matches WHERE item_id IN ({ids})                  ORDER BY item_id, show_id"
            ))?
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<std::result::Result<_, _>>()?;
        let show_ids = pairs
            .iter()
            .map(|(_, show_id)| *show_id)
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .map(|id| id.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        let shows: HashMap<i64, Show> =
            Show::select(ctx.conn, &format!("WHERE id IN ({show_ids})"), [])?
                .into_iter()
                .filter_map(|show| show.id.map(|id| (id, show)))
                .collect();
        for (item_id, show_id) in pairs {
            if let Some(show) = shows.get(&show_id) {
                belongs
                    .entry(item_id)
                    .or_default()
                    .push(json!({"id": show.id, "title": show.title(language)}));
            }
        }
        let guids = rows
            .iter()
            .map(|row| format!("'{}'", row.guid.replace('\'', "''")))
            .collect::<Vec<_>>()
            .join(", ");
        for download in Download::select(
            ctx.conn,
            &format!("WHERE feed_item_guid IN ({guids}) AND feed_id IS NOT NULL ORDER BY id"),
            [],
        )? {
            if let (Some(label), Some(feed_id), Some(guid)) = (
                download_label(download.state),
                download.feed_id,
                download.feed_item_guid,
            ) {
                taken.insert((feed_id, guid), label);
            }
        }
    }
    let names: HashMap<i64, String> = Feed::select(ctx.conn, "", [])?
        .into_iter()
        .filter_map(|feed| feed.id.map(|id| (id, feed.name)))
        .collect();
    let items: Vec<Value> = rows
        .iter()
        .map(|row| {
            json!({
                "id": row.id,
                "feed_id": row.feed_id,
                "feed": names.get(&row.feed_id),
                "title": row.title,
                "published_at": iso(row.published_at),
                "first_seen_at": iso(Some(row.first_seen_at)),
                "shows": row.id.and_then(|id| belongs.remove(&id)).unwrap_or_default(),
                "download": taken.get(&(row.feed_id, row.guid.clone())),
            })
        })
        .collect();
    Ok(json!({"items": items, "total": total, "new_recent": new_recent}))
}
