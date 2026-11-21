//! Spaces AniList requests so the limit is never exceeded, and remembers when to stop asking.

use std::collections::VecDeque;

use super::error::{Deferral, DeferralKind};

/// Requests per minute until AniList reports its own figure.
const DEFAULT_RATE_LIMIT: u32 = 30;
/// Requests per minute left unused, for actions the user starts by hand.
const RATE_LIMIT_RESERVE: u32 = 5;
/// The shortest gap between two requests, so a burst still reaches AniList one request at a time.
const MIN_REQUEST_GAP: f64 = 0.5;
/// The longest one request waits for its turn; a longer wait is reported to the caller.
const MAX_PACING_WAIT: f64 = 5.0;
/// How long to stop asking after a 429 that names no wait.
const BLOCK_FALLBACK: f64 = 60.0;
/// The most a 429 may impose.
const BLOCK_MAX: f64 = 120.0;
/// Requests pause after a call that failed on every retry: this long, doubling up to the maximum.
const BREAKER_START: f64 = 60.0;
const BREAKER_MAX: f64 = 900.0;

/// The rate-limit figures of one answer.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct RateHeaders {
    /// `x-ratelimit-limit`: requests allowed per minute.
    pub limit: Option<f64>,
    /// `x-ratelimit-remaining`.
    pub remaining: Option<f64>,
    /// `retry-after`, in seconds.
    pub retry_after: Option<f64>,
    /// `x-ratelimit-reset`, as epoch seconds.
    pub reset: Option<f64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Cause {
    RateLimit,
    Failures,
}

/// Decides when each AniList request may go out. Times are seconds on a monotonic clock (`now`);
/// `wall` is epoch seconds.
#[derive(Debug)]
pub struct RateGovernor {
    limit: u32,
    next_slot: f64,
    /// When each request of the last minute went (or will go) out, oldest first.
    sent: VecDeque<f64>,
    blocked_until: f64,
    blocked_by: Option<Cause>,
    failures: u32,
}

impl Default for RateGovernor {
    fn default() -> Self {
        Self {
            limit: DEFAULT_RATE_LIMIT,
            next_slot: 0.0,
            sent: VecDeque::new(),
            blocked_until: 0.0,
            blocked_by: None,
            failures: 0,
        }
    }
}

impl RateGovernor {
    /// Requests allowed in any minute: the limit minus the reserve.
    pub fn budget(&self) -> usize {
        self.limit.saturating_sub(RATE_LIMIT_RESERVE).max(1) as usize
    }

    /// Seconds until AniList may be asked again; zero when it is not blocked.
    pub fn retry_in(&self, now: f64) -> f64 {
        (self.blocked_until - now).max(0.0)
    }

    /// The epoch second AniList may be asked again, when it is blocked.
    pub fn blocked_until_epoch(&self, now: f64, wall: f64) -> Option<f64> {
        let wait = self.retry_in(now);
        (wait > 0.0).then_some(wall + wait)
    }

    fn block(&mut self, now: f64, seconds: f64, cause: Cause) {
        let until = now + seconds;
        if until > self.blocked_until {
            self.blocked_until = until;
            self.blocked_by = Some(cause);
        }
    }

    /// Takes this request's turn and returns how long to wait for it, or says why it may not go out:
    /// AniList is blocked, or the wait would be too long.
    ///
    /// Requests go out a short gap apart until the minute's budget is spent; then the next waits
    /// until the oldest of them is a minute old. No minute ever holds more than the budget, yet a
    /// burst (a first sync) is not spread out.
    pub fn acquire(&mut self, now: f64) -> Result<f64, Deferral> {
        if now < self.blocked_until {
            let wait = self.blocked_until - now;
            return Err(match self.blocked_by {
                Some(Cause::Failures) => Deferral::new(
                    DeferralKind::Unavailable,
                    format!("AniList is not answering; retry in {wait:.0}s."),
                    wait,
                ),
                _ => Deferral::new(
                    DeferralKind::RateLimited,
                    format!("AniList is rate limiting this client; retry in {wait:.0}s."),
                    wait,
                ),
            });
        }
        while self.sent.front().is_some_and(|&at| now - at >= 60.0) {
            self.sent.pop_front();
        }
        let mut slot = now.max(self.next_slot);
        let budget = self.budget();
        if self.sent.len() >= budget {
            if let Some(&oldest_in_window) = self.sent.get(self.sent.len() - budget) {
                slot = slot.max(oldest_in_window + 60.0);
            }
        }
        let wait = slot - now;
        if wait > MAX_PACING_WAIT {
            return Err(Deferral::new(
                DeferralKind::RateLimited,
                format!("AniList request budget used up; retry in {wait:.0}s."),
                wait,
            ));
        }
        self.sent.push_back(slot);
        self.next_slot = slot + MIN_REQUEST_GAP;
        Ok(wait)
    }

    /// Takes the limit AniList reports, and stops asking once it says none is left.
    pub fn note_response(&mut self, now: f64, headers: &RateHeaders) {
        if let Some(limit) = headers.limit.filter(|&limit| limit > 0.0) {
            self.limit = limit as u32;
        }
        if headers.remaining.is_some_and(|remaining| remaining <= 0.0) {
            self.block(now, BLOCK_FALLBACK, Cause::RateLimit);
        }
    }

    /// Records a 429 and returns the seconds AniList asked to wait.
    pub fn note_rate_limited(&mut self, now: f64, wall: f64, headers: &RateHeaders) -> f64 {
        let asked = headers
            .retry_after
            .or_else(|| headers.reset.map(|reset| reset - wall))
            .unwrap_or(BLOCK_FALLBACK);
        let wait = asked.clamp(1.0, BLOCK_MAX);
        self.block(now, wait, Cause::RateLimit);
        wait
    }

    pub fn note_success(&mut self) {
        self.failures = 0;
    }

    /// Records a call that failed on every retry; requests pause for a growing while.
    pub fn note_failure(&mut self, now: f64) -> f64 {
        self.failures = self.failures.saturating_add(1);
        let exponent = i32::try_from(self.failures - 1).unwrap_or(i32::MAX);
        let wait = (BREAKER_START * 2f64.powi(exponent)).min(BREAKER_MAX);
        self.block(now, wait, Cause::Failures);
        wait
    }
}
