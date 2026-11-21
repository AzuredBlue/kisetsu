//! The recent log lines the Logs page shows.

use std::collections::VecDeque;
use std::fmt::Write as _;
use std::sync::{Arc, Mutex, MutexGuard};

use jiff::Timestamp;
use kisetsu_services::context::iso_utc;
use serde_json::{Value, json};
use sha1_smol::Sha1;
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::Layer;
use tracing_subscriber::layer::Context;

/// How many lines are kept.
const RING_SIZE: usize = 500;
/// What the lines of the ring are named after: the program's own parts.
const OWN_TARGET: &str = "kisetsu";

/// One log line.
#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    pub seq: u64,
    pub at: Timestamp,
    pub level: &'static str,
    pub source: String,
    pub message: String,
}

impl Line {
    fn to_json(&self) -> Value {
        json!({
            "seq": self.seq,
            "at": iso_utc(self.at),
            "level": self.level,
            "source": self.source,
            "message": self.message,
        })
    }
}

struct Ring {
    lines: VecDeque<Line>,
    seq: u64,
}

/// Keeps the last lines logged by the program; the listener is told when a new one arrives.
pub struct LogRing {
    ring: Mutex<Ring>,
    /// Each line is numbered, so the Logs page can ask for only the newer ones; the run tells
    /// this run's numbers from an earlier run's, which started over.
    run: String,
    listener: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl Default for LogRing {
    fn default() -> Self {
        Self::new()
    }
}

impl LogRing {
    pub fn new() -> Self {
        let seed = format!(
            "{}:{}",
            std::process::id(),
            Timestamp::now().as_nanosecond()
        );
        Self {
            ring: Mutex::new(Ring {
                lines: VecDeque::new(),
                seq: 0,
            }),
            run: Sha1::from(seed)
                .digest()
                .to_string()
                .chars()
                .take(12)
                .collect(),
            listener: Mutex::new(None),
        }
    }

    /// Runs `listener` after every new line.
    pub fn on_line(&self, listener: impl Fn() + Send + Sync + 'static) {
        *locked(&self.listener) = Some(Box::new(listener));
    }

    /// Adds a line.
    pub fn push(&self, at: Timestamp, level: &'static str, source: &str, message: &str) {
        {
            let mut ring = locked(&self.ring);
            ring.seq += 1;
            let seq = ring.seq;
            if ring.lines.len() >= RING_SIZE {
                ring.lines.pop_front();
            }
            ring.lines.push_back(Line {
                seq,
                at,
                level,
                source: source.to_owned(),
                message: message.to_owned(),
            });
        }
        if let Some(listener) = &*locked(&self.listener) {
            listener();
        }
    }

    /// The last `limit` lines; with `after` from this `run`, only the lines since.
    ///
    /// `full` says the lines replace what the caller holds: after a restart, or when lines it
    /// never saw have already left the ring.
    pub fn recent(&self, limit: i64, after: Option<i64>, run: Option<&str>) -> Value {
        let lines: Vec<Line> = locked(&self.ring).lines.iter().cloned().collect();
        let limit = usize::try_from(limit.max(1)).unwrap_or(1);
        let tail = |from: usize| -> Vec<Value> {
            let kept = &lines[from..];
            kept[kept.len().saturating_sub(limit)..]
                .iter()
                .map(Line::to_json)
                .collect()
        };
        let contiguous = |after: i64| {
            lines.first().is_none_or(|first| {
                i64::try_from(first.seq).unwrap_or(i64::MAX) <= after.saturating_add(1)
            })
        };
        match after {
            Some(after) if run == Some(self.run.as_str()) && contiguous(after) => {
                let newer = lines
                    .iter()
                    .position(|line| i64::try_from(line.seq).unwrap_or(i64::MAX) > after)
                    .unwrap_or(lines.len());
                json!({"run": self.run, "full": false, "lines": tail(newer)})
            }
            _ => json!({"run": self.run, "full": true, "lines": tail(0)}),
        }
    }
}

fn level_name(level: &Level) -> &'static str {
    match *level {
        Level::ERROR => "ERROR",
        Level::WARN => "WARNING",
        Level::INFO => "INFO",
        _ => "DEBUG",
    }
}

/// The logger name the UI shows: the module path without the program's name, dotted.
fn source_of(target: &str) -> String {
    let own = target.strip_prefix("kisetsu_").unwrap_or(target);
    own.replace("::", ".")
}

#[derive(Default)]
struct MessageOf(String);

impl Visit for MessageOf {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        if field.name() == "message" {
            let _ = write!(self.0, "{value:?}");
        }
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        if field.name() == "message" {
            self.0.push_str(value);
        }
    }
}

/// What feeds the ring from the program's logging.
pub struct RingLayer(pub Arc<LogRing>);

impl<S: Subscriber> Layer<S> for RingLayer {
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let meta = event.metadata();
        if *meta.level() > Level::INFO || !meta.target().starts_with(OWN_TARGET) {
            return;
        }
        let mut message = MessageOf::default();
        event.record(&mut message);
        self.0.push(
            Timestamp::now(),
            level_name(meta.level()),
            &source_of(meta.target()),
            &message.0,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ring_with(count: usize) -> LogRing {
        let ring = LogRing::new();
        for number in 1..=count {
            ring.push(
                Timestamp::UNIX_EPOCH,
                "INFO",
                "test",
                &format!("line {number}"),
            );
        }
        ring
    }

    fn seqs(page: &Value) -> Vec<u64> {
        page["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| line["seq"].as_u64().unwrap())
            .collect()
    }

    #[test]
    fn a_reader_with_its_last_line_gets_only_the_newer_ones() {
        let ring = ring_with(5);
        let run = ring.recent(10, None, None)["run"]
            .as_str()
            .unwrap()
            .to_owned();
        let page = ring.recent(10, Some(3), Some(&run));
        assert_eq!(page["full"], false);
        assert_eq!(seqs(&page), [4, 5]);
    }

    #[test]
    fn a_reader_from_another_run_or_with_a_gap_gets_everything() {
        let ring = ring_with(RING_SIZE + 10);
        let run = ring.recent(1, None, None)["run"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(ring.recent(2, Some(1), Some(&run))["full"], true);
        assert_eq!(ring.recent(2, Some(520), Some("other"))["full"], true);
        assert_eq!(seqs(&ring.recent(2, None, None)), [509, 510]);
    }
}
