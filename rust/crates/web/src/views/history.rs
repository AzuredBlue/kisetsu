//! The History page.

use std::collections::{HashMap, HashSet};

use jiff::Timestamp;
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{Event, EventKind, Feed, Show};
use kisetsu_services::Result;
use kisetsu_services::context::Context;
use serde_json::{Value, json};

use super::{iso, value_of};

/// The kinds of event the History page lists.
pub const HISTORY_KINDS: [EventKind; 6] = [
    EventKind::Grabbed,
    EventKind::Done,
    EventKind::Replaced,
    EventKind::Failed,
    EventKind::Missed,
    EventKind::Deleted,
];

/// Grabs that aren't queued any more: something else has happened to the episode since, like
/// another release, an adoption or a failure.
fn superseded_grabs(ctx: &Context<'_>) -> Result<HashSet<i64>> {
    let mut newest_at: HashMap<(i64, i64), Timestamp> = HashMap::new();
    for event in Event::select(
        ctx.conn,
        "WHERE kind != 'GRABBED' AND show_id IS NOT NULL AND episode IS NOT NULL",
        [],
    )? {
        if let (Some(show_id), Some(episode)) = (event.show_id, event.episode) {
            let newest = newest_at.entry((show_id, episode)).or_insert(event.at);
            *newest = (*newest).max(event.at);
        }
    }
    let grabs = Event::select(
        ctx.conn,
        "WHERE kind = 'GRABBED' AND show_id IS NOT NULL AND episode IS NOT NULL ORDER BY id DESC",
        [],
    )?;
    let mut latest = HashSet::new();
    let mut superseded = HashSet::new();
    for grab in grabs {
        let (Some(id), Some(show_id), Some(episode)) = (grab.id, grab.show_id, grab.episode) else {
            continue;
        };
        let key = (show_id, episode);
        let settled = newest_at
            .get(&key)
            .is_some_and(|settled| *settled >= grab.at);
        if latest.contains(&key) || settled {
            superseded.insert(id);
        }
        latest.insert(key);
    }
    Ok(superseded)
}

/// The newest `limit` entries: each download once, and a deletion as its own entry.
pub fn history(ctx: &Context<'_>, limit: i64) -> Result<Value> {
    let kinds = HISTORY_KINDS
        .iter()
        .map(|kind| format!("'{}'", kind.name()))
        .collect::<Vec<_>>()
        .join(", ");
    let rows = Event::select(
        ctx.conn,
        &format!("WHERE kind IN ({kinds}) ORDER BY at DESC, id DESC LIMIT ?"),
        [limit.saturating_mul(3)],
    )?;
    let superseded = superseded_grabs(ctx)?;
    let mut seen = HashSet::new();
    let mut events = Vec::new();
    for event in rows {
        // A deletion is its own entry: the download it ends stays listed on its day.
        let key = (
            event.show_id,
            event.episode,
            event.title.clone(),
            event.kind == EventKind::Deleted,
        );
        if !event.id.is_some_and(|id| superseded.contains(&id)) && seen.insert(key) {
            events.push(event);
        }
    }
    events.truncate(usize::try_from(limit).unwrap_or(0));
    let shows: HashMap<i64, Show> = if events.is_empty() {
        HashMap::new()
    } else {
        let ids = events
            .iter()
            .filter_map(|event| event.show_id)
            .map(|id| id.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        Show::select(ctx.conn, &format!("WHERE id IN ({ids})"), [])?
            .into_iter()
            .filter_map(|show| show.id.map(|id| (id, show)))
            .collect()
    };
    let feeds: HashMap<i64, String> = Feed::select(ctx.conn, "", [])?
        .into_iter()
        .filter_map(|feed| feed.id.map(|id| (id, feed.name)))
        .collect();
    let language = &ctx.settings.title_language;
    let rows: Vec<Value> = events
        .iter()
        .map(|event| {
            let show = event.show_id.and_then(|id| shows.get(&id));
            json!({
                "id": event.id,
                "at": iso(Some(event.at)),
                "kind": value_of(event.kind),
                "show_id": event.show_id,
                "show": show.map(|show| show.title(language)),
                "cover": show.and_then(|show| show.cover_image.clone()),
                "cover_color": show.and_then(|show| show.cover_color.clone()),
                "episode": event.episode,
                "feed": event.feed_id.and_then(|id| feeds.get(&id)),
                "title": event.title,
                "message": event.message,
            })
        })
        .collect();
    Ok(Value::Array(rows))
}
