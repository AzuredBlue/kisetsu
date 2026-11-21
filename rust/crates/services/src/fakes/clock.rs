use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use jiff::{SignedDuration, Timestamp};

use crate::context::{Clock, TagSource};

/// Where a clock starts: 2026-10-01 12:00 UTC.
pub const START: Timestamp = Timestamp::constant(1_790_856_000, 0);

/// A clock the test sets. Clones share the time, so the fakes and the test see one clock.
#[derive(Debug, Clone)]
pub struct FakeClock {
    micros: Arc<AtomicI64>,
}

impl FakeClock {
    pub fn new(now: Timestamp) -> Self {
        Self {
            micros: Arc::new(AtomicI64::new(now.as_microsecond())),
        }
    }

    pub fn set(&self, now: Timestamp) {
        self.micros.store(now.as_microsecond(), Ordering::Relaxed);
    }

    /// Moves the clock forward and returns the new time.
    pub fn advance(&self, by: SignedDuration) -> Timestamp {
        let now = self.now().saturating_add(by).unwrap_or_else(|_| self.now());
        self.set(now);
        now
    }
}

impl Default for FakeClock {
    fn default() -> Self {
        Self::new(START)
    }
}

impl Clock for FakeClock {
    fn now(&self) -> Timestamp {
        Timestamp::from_microsecond(self.micros.load(Ordering::Relaxed)).unwrap_or(START)
    }
}

/// Download tags that count up: `kisetsu-op-` and the count as 32 hex digits.
#[derive(Debug, Default)]
pub struct CountedTags {
    issued: u128,
}

impl TagSource for CountedTags {
    fn next_tag(&mut self) -> String {
        self.issued += 1;
        format!("kisetsu-op-{:032x}", self.issued)
    }
}
