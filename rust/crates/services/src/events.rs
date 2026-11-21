//! The History entries.

use kisetsu_db::schema::{Event, EventKind};

use crate::context::Context;
use crate::error::Result;

/// What an entry is about, beyond its message.
#[derive(Debug, Clone, Copy, Default)]
pub struct Subject<'a> {
    pub show_id: Option<i64>,
    pub episode: Option<i64>,
    pub feed_id: Option<i64>,
    pub title: &'a str,
}

/// Adds a History entry. The caller's transaction commits it.
pub fn record(
    ctx: &mut Context<'_>,
    kind: EventKind,
    message: &str,
    subject: Subject<'_>,
) -> Result<Event> {
    let mut event = Event {
        id: None,
        at: ctx.now,
        kind,
        show_id: subject.show_id,
        episode: subject.episode,
        feed_id: subject.feed_id,
        title: subject.title.to_owned(),
        message: message.to_owned(),
    };
    event.id = Some(event.insert(ctx.conn)?);
    ctx.emit(&["history"]);
    Ok(event)
}
