//! How many requests Kisetsu has sent since it started, by site and kind, to see what costs requests.
//!
//! Every attempt counts, retries included, since each one reaches the site. Kept in memory only.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::sync::{Arc, LazyLock, Mutex};

use jiff::Timestamp;
use serde::Serialize;

use crate::locked;
use crate::url;

/// How a request ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Outcome {
    Ok,
    RateLimited,
    Error,
}

impl Outcome {
    /// The outcome of an HTTP answer with this status.
    pub fn of_status(status: u16) -> Self {
        match status {
            429 => Self::RateLimited,
            400.. => Self::Error,
            _ => Self::Ok,
        }
    }
}

/// The requests of one kind to one host.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Row {
    pub kind: String,
    pub host: String,
    pub requests: u64,
    pub ok: u64,
    pub rate_limited: u64,
    pub error: u64,
}

/// The counts by kind and host, most requests first.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snapshot {
    pub since: Timestamp,
    pub total: u64,
    pub rows: Vec<Row>,
}

/// The request counters, apart from the process-wide ones the free functions use.
#[derive(Debug)]
pub struct Counts {
    since: Timestamp,
    counts: BTreeMap<(String, String, Outcome), u64>,
}

impl Counts {
    pub fn new(since: Timestamp) -> Self {
        Self {
            since,
            counts: BTreeMap::new(),
        }
    }

    pub fn record(&mut self, kind: &str, host: &str, outcome: Outcome) {
        *self
            .counts
            .entry((kind.to_owned(), host.to_owned(), outcome))
            .or_default() += 1;
    }

    pub fn snapshot(&self) -> Snapshot {
        let mut rows: BTreeMap<(&str, &str), Row> = BTreeMap::new();
        for ((kind, host, outcome), &n) in &self.counts {
            let row = rows.entry((kind, host)).or_insert_with(|| Row {
                kind: kind.clone(),
                host: host.clone(),
                requests: 0,
                ok: 0,
                rate_limited: 0,
                error: 0,
            });
            row.requests += n;
            match outcome {
                Outcome::Ok => row.ok += n,
                Outcome::RateLimited => row.rate_limited += n,
                Outcome::Error => row.error += n,
            }
        }
        let mut rows: Vec<Row> = rows.into_values().collect();
        rows.sort_by(|a, b| {
            b.requests
                .cmp(&a.requests)
                .then_with(|| a.kind.cmp(&b.kind))
                .then_with(|| a.host.cmp(&b.host))
        });
        Snapshot {
            since: self.since,
            total: rows.iter().map(|row| row.requests).sum(),
            rows,
        }
    }

    pub fn reset(&mut self, since: Timestamp) {
        self.counts.clear();
        self.since = since;
    }
}

type Listener = Arc<dyn Fn() + Send + Sync>;

static COUNTS: LazyLock<Mutex<Counts>> =
    LazyLock::new(|| Mutex::new(Counts::new(Timestamp::now())));
static LISTENER: Mutex<Option<Listener>> = Mutex::new(None);

thread_local! {
    static CALLER: RefCell<String> = RefCell::new(String::from("?"));
}

/// Counts one request. The listener, when set, runs afterwards on the calling thread.
pub fn record(kind: &str, host: &str, outcome: Outcome) {
    locked(&COUNTS).record(kind, host, outcome);
    let listener = locked(&LISTENER).clone();
    if let Some(listener) = listener {
        listener();
    }
}

/// Sets what runs after each counted request, or clears it. Whatever shows the counts uses it.
pub fn set_listener(listener: Option<Listener>) {
    *locked(&LISTENER) = listener;
}

/// The counts so far.
pub fn snapshot() -> Snapshot {
    locked(&COUNTS).snapshot()
}

/// Starts counting from zero.
pub fn reset() {
    locked(&COUNTS).reset(Timestamp::now());
}

/// The host of a URL, or the whole text when it has none.
pub fn host_of(url: &str) -> String {
    url::host_or_url(url)
}

/// Names the part of the app making requests on this thread, until the guard drops.
///
/// The name (`module.function`) is shown with every request counted meanwhile.
pub fn caller(name: &str) -> CallerGuard {
    CallerGuard {
        previous: CALLER.with(|caller| caller.replace(name.to_owned())),
    }
}

/// Restores the previous caller name when dropped.
#[derive(Debug)]
pub struct CallerGuard {
    previous: String,
}

impl Drop for CallerGuard {
    fn drop(&mut self) {
        CALLER.with(|caller| *caller.borrow_mut() = std::mem::take(&mut self.previous));
    }
}

/// The name set by the innermost live [`caller`] guard on this thread, `?` when none is.
pub fn current_caller() -> String {
    CALLER.with(|caller| caller.borrow().clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_caller_name_nests_and_restores() {
        assert_eq!(current_caller(), "?");
        let outer = caller("grab.run");
        {
            let _inner = caller("shows.sync");
            assert_eq!(current_caller(), "shows.sync");
        }
        assert_eq!(current_caller(), "grab.run");
        drop(outer);
        assert_eq!(current_caller(), "?");
    }
}
