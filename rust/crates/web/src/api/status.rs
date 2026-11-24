//! The header's status, what needs attention, the logs, the request counts and the history.

use std::collections::BTreeMap;
use std::collections::VecDeque;
use std::time::Duration;

use jiff::Timestamp;
use kisetsu_clients::traffic;
use kisetsu_db::schema::EventKind;
use kisetsu_engine::bus::{Change, RecvError, Subscription};
use kisetsu_services::context::{Context, set_state};
use kisetsu_services::{Error, Result};
use serde_json::{Value, json};

use super::{checked, ok, poke, success};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::ApiResult;
use crate::http::{Body, Response};
use crate::views::{self, ATTENTION_DISMISSED_KEY, HISTORY_KINDS, iso};

/// The most history a page may ask for.
const HISTORY_LIMIT_MAX: i64 = 500;

/// The counts, and the feeds' last read.
fn totals(ctx: &mut Context<'_>) -> Result<Value> {
    let Value::Object(mut body) = views::counts(ctx)? else {
        return Ok(Value::Null);
    };
    let last_read: Option<String> = ctx
        .conn
        .prepare_cached("SELECT MAX(last_fetch_at) FROM feeds")?
        .query_row([], |row| row.get(0))?;
    let last_read = last_read.and_then(|text| kisetsu_db::storage::parse_timestamp(&text));
    body.insert("last_feed_read_at".to_owned(), iso(last_read));
    Ok(Value::Object(body))
}

/// What the engine is doing now.
fn live(call: &Call<'_, '_>) -> Value {
    let runtime = call.runtime();
    let now = runtime.scheduler.now().as_secs_f64();
    let statuses = runtime.scheduler.statuses();
    // The routine read wakes only when it has a feed to read; the hunt's own wake-ups are
    // rechecks, so its next real read counts.
    let mut dues: Vec<f64> = statuses
        .iter()
        .find(|(name, _)| name == "refresh_feeds")
        .and_then(|(_, status)| status.due)
        .map(|due| due.as_secs_f64() - now)
        .into_iter()
        .collect();
    if let Some(at) = runtime.jobs.hunt_read_at() {
        dues.push(at.duration_since(runtime.engine.now()).as_secs_f64());
    }
    let next = dues
        .into_iter()
        .reduce(f64::min)
        .map(|seconds| seconds.trunc().max(0.0) as i64);
    let health = runtime.jobs.health();
    let blocked = runtime
        .remote
        .anilist_blocked_until()
        .and_then(|seconds| Timestamp::from_microsecond((seconds * 1e6) as i64).ok());
    let failing: BTreeMap<&str, &str> = statuses
        .iter()
        .filter_map(|(name, status)| {
            status
                .last_error
                .as_deref()
                .map(|error| (name.as_str(), error))
        })
        .collect();
    let mut running: Vec<&str> = statuses
        .iter()
        .filter(|(_, status)| status.running)
        .map(|(name, _)| name.as_str())
        .collect();
    running.sort_unstable();
    json!({
        "ready": runtime.jobs.is_ready(),
        "sandbox": runtime.sandbox,
        "running": running,
        "next_feed_read_in": next,
        "qbit_error": health.qbit,
        "anilist_error": health.anilist,
        "grab_error": health.grab,
        // Background jobs whose last run raised; each is retried a minute later.
        "jobs_failing": failing,
        "anilist_blocked_until": iso(blocked),
        // Only while the first-run setup waits for the user to confirm its first downloads.
        "setup_progress": runtime.jobs.setup_progress().map(|progress| json!({
            "searched": progress.searched,
            "total": progress.total,
            "done": progress.done,
        })),
        "rev": runtime.engine.bus().rev(),
    })
}

pub(super) fn get(call: &mut Call<'_, '_>) -> ApiResult {
    let Value::Object(mut body) = call.runtime().engine.read(totals)? else {
        return ok(&Value::Null);
    };
    if let Value::Object(live) = live(call) {
        body.extend(live);
    }
    if body.get("qbit_error").is_some_and(|error| !error.is_null()) {
        // Adds can't be judged while qBittorrent is unreachable; that chip already says so.
        body.insert("stuck_adds".to_owned(), json!(0));
    }
    ok(&Value::Object(body))
}

pub(super) fn attention(call: &mut Call<'_, '_>) -> ApiResult {
    ok(&call.runtime().engine.read(views::attention)?)
}

pub(super) fn dismiss_attention(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let keys =
        body.and_then(|body| Reader::new(&mut problems, "body").required_strings(&body, "keys"));
    checked(problems)?;
    let keys = keys.unwrap_or_default();
    call.runtime().engine.call(move |ctx| {
        let current: Vec<String> = views::attention_rows(ctx, true)?
            .into_iter()
            .flat_map(|row| row.keys)
            .collect();
        let closing: Vec<&String> = current.iter().filter(|key| keys.contains(key)).collect();
        if closing.is_empty() {
            return Err(Error::NotFound(
                "That no longer needs attention.".to_owned(),
            ));
        }
        // Keys of rows that are gone are dropped, so the list only ever holds what it still
        // hides.
        let mut kept: Vec<String> = views::dismissed_attention(ctx)?
            .into_iter()
            .filter(|key| current.contains(key))
            .chain(closing.into_iter().cloned())
            .collect();
        kept.sort();
        kept.dedup();
        let encoded = serde_json::to_string(&kept).unwrap_or_else(|_| "[]".to_owned());
        set_state(ctx.conn, ATTENTION_DISMISSED_KEY, &encoded)?;
        ctx.emit(&["shows"]);
        Ok(())
    })?;
    success()
}

pub(super) fn logs(call: &mut Call<'_, '_>) -> ApiResult {
    let pairs = call.query();
    let mut problems = Problems::default();
    let mut reader = Reader::new(&mut problems, "query");
    let limit = reader.query_integer(&pairs, "limit").unwrap_or(100);
    let after = reader.query_integer(&pairs, "after");
    let run = crate::check::query_value(&pairs, "run");
    checked(problems)?;
    ok(&call.runtime().logs.recent(limit, after, run))
}

pub(super) fn requests(_call: &mut Call<'_, '_>) -> ApiResult {
    let snapshot = traffic::snapshot();
    ok(&json!({
        "since": views::iso(Some(snapshot.since)),
        "total": snapshot.total,
        "rows": snapshot.rows,
    }))
}

pub(super) fn reset_requests(_call: &mut Call<'_, '_>) -> ApiResult {
    traffic::reset();
    success()
}

pub(super) fn history(call: &mut Call<'_, '_>) -> ApiResult {
    let pairs = call.query();
    let mut problems = Problems::default();
    let limit = Reader::new(&mut problems, "query")
        .query_integer(&pairs, "limit")
        .unwrap_or(100);
    checked(problems)?;
    let limit = limit.clamp(1, HISTORY_LIMIT_MAX);
    ok(&call
        .runtime()
        .engine
        .read(|ctx| views::history(ctx, limit))?)
}

pub(super) fn clear_history(call: &mut Call<'_, '_>) -> ApiResult {
    call.runtime().engine.call(|ctx| {
        let kinds = HISTORY_KINDS
            .iter()
            .map(|kind: &EventKind| format!("'{}'", kisetsu_db::StoredEnum::name(*kind)))
            .collect::<Vec<_>>()
            .join(", ");
        ctx.conn
            .execute(&format!("DELETE FROM events WHERE kind IN ({kinds})"), [])?;
        ctx.emit(&["history"]);
        Ok(())
    })?;
    success()
}

pub(super) fn refresh(call: &mut Call<'_, '_>) -> ApiResult {
    call.runtime().jobs.request_refresh();
    poke(call, "sync_anilist");
    ok(&json!({"status": "queued"}))
}

/// One message of the stream: the topics that changed and which message this is.
fn message(change: &Change) -> Vec<u8> {
    let topics: Vec<String> = change
        .changed
        .iter()
        .map(|topic| json!(topic).to_string())
        .collect();
    format!(
        "data: {{\"changed\": [{}], \"rev\": {}}}\n\n",
        topics.join(", "),
        change.rev
    )
    .into_bytes()
}

/// The text the stream is made of: a refetch first, then what changes, and a comment whenever
/// it has been quiet a while.
struct Events {
    subscription: Subscription,
    first: VecDeque<Vec<u8>>,
    keepalive: Duration,
}

impl Iterator for Events {
    type Item = Vec<u8>;

    fn next(&mut self) -> Option<Vec<u8>> {
        if let Some(first) = self.first.pop_front() {
            return Some(first);
        }
        match self.subscription.recv_timeout(self.keepalive) {
            Ok(change) => Some(message(&change)),
            Err(RecvError::Timeout) => Some(b": keepalive\n\n".to_vec()),
            Err(RecvError::Ended) => None,
        }
    }
}

pub(super) fn stream(call: &mut Call<'_, '_>) -> ApiResult {
    let bus = call.runtime().engine.bus();
    let subscription = bus.subscribe();
    // A client that connects (or reconnects) refetches everything once, so it never misses a
    // change.
    let everything = Change {
        changed: vec![kisetsu_engine::bus::EVERYTHING.to_owned()],
        rev: bus.rev(),
    };
    let events = Events {
        subscription,
        first: VecDeque::from([b"retry: 3000\n\n".to_vec(), message(&everything)]),
        keepalive: call.app.keepalive(),
    };
    Ok(Response::new(200, Body::Events(Box::new(events)))
        .with("content-type", "text/event-stream; charset=utf-8")
        .with("cache-control", "no-store")
        .with("x-accel-buffering", "no"))
}
