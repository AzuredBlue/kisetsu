//! What the UI shows, built from the database. The UI decides nothing: statuses, labels and roles
//! are settled here.

mod attention;
mod counts;
mod feeds;
mod history;
mod shows;

use std::collections::HashMap;

use jiff::Timestamp;
use kisetsu_db::StoredEnum;
use kisetsu_db::rusqlite::types::Value as SqlValue;
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::status::ShowStatus;
use kisetsu_services::Result;
use kisetsu_services::context::{Context, iso_utc};
use kisetsu_services::shows::all_paused;
use serde_json::Value;

pub use attention::{
    ATTENTION_DISMISSED_KEY, attention, dismissed_attention, rows as attention_rows,
};
pub use counts::counts;
pub use feeds::{feed_items, feeds};
pub use history::{HISTORY_KINDS, history};
pub use shows::{card_for, show_view, shows};

/// An air time or any other moment, as the UI reads it.
pub fn iso(at: Option<Timestamp>) -> Value {
    at.map_or(Value::Null, |at| Value::String(iso_utc(at)))
}

/// The name a stored enum has in the UI's data: its member name in lower case.
pub fn value_of(member: impl StoredEnum) -> String {
    member.name().to_ascii_lowercase()
}

/// The name a show's status has in the UI's data.
fn status_value(status: ShowStatus) -> &'static str {
    match status {
        ShowStatus::Paused => "paused",
        ShowStatus::Completed => "completed",
        ShowStatus::Working => "working",
        ShowStatus::Upcoming => "upcoming",
        ShowStatus::Waiting => "waiting",
        ShowStatus::Testing => "testing",
    }
}

/// A timestamp as a bound parameter, in the form the database keeps.
fn bound(at: Timestamp) -> SqlValue {
    SqlValue::Text(kisetsu_db::storage::format_timestamp(at))
}

/// The number of episodes of each status, by show.
fn episode_counts(ctx: &Context<'_>) -> Result<HashMap<i64, HashMap<EpisodeStatus, i64>>> {
    let mut counts: HashMap<i64, HashMap<EpisodeStatus, i64>> = HashMap::new();
    let mut statement = ctx.conn.prepare_cached(
        "SELECT show_id, status, COUNT(id) FROM episodes GROUP BY show_id, status",
    )?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        if let Some(status) = EpisodeStatus::from_name(&name) {
            counts
                .entry(row.get(0)?)
                .or_default()
                .insert(status, row.get(2)?);
        }
    }
    Ok(counts)
}

/// The number of episodes of each status.
fn episode_counts_total(ctx: &Context<'_>) -> Result<HashMap<EpisodeStatus, i64>> {
    let mut totals: HashMap<EpisodeStatus, i64> = HashMap::new();
    for counts in episode_counts(ctx)?.into_values() {
        for (status, count) in counts {
            *totals.entry(status).or_default() += count;
        }
    }
    Ok(totals)
}
