//! Why a request to AniList gave no answer.

use std::collections::BTreeMap;
use std::fmt;

use jiff::Timestamp;
use thiserror::Error;

/// An episode and when it airs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AiringEpisode {
    pub episode: i64,
    pub airing_at: Timestamp,
}

/// The air times of shows by AniList id, each in episode order.
pub type Schedules = BTreeMap<i64, Vec<AiringEpisode>>;

/// Why AniList is not being asked right now.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeferralKind {
    /// AniList limits this client, or the request budget for now is used up.
    RateLimited,
    /// AniList failed on every retry; requests are paused for a while.
    Unavailable,
}

/// AniList should not be asked again yet, so a request was not sent or a pass stopped early.
#[derive(Debug, Clone, PartialEq)]
pub struct Deferral {
    pub kind: DeferralKind,
    pub message: String,
    /// Seconds to wait before asking again.
    pub retry_in: f64,
    /// What a call that makes several requests had already fetched when it stopped.
    pub partial: Schedules,
}

impl Deferral {
    pub(crate) fn new(kind: DeferralKind, message: String, retry_in: f64) -> Self {
        Self {
            kind,
            message,
            retry_in: retry_in.max(0.0),
            partial: Schedules::new(),
        }
    }
}

impl fmt::Display for Deferral {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

/// A request to AniList failed.
#[derive(Debug, Clone, PartialEq, Error)]
pub enum AniListError {
    #[error("{0}")]
    Deferred(Deferral),
    /// AniList answered 404: the user (or show) asked about does not exist.
    #[error("AniList HTTP error: 404 - {body}")]
    NotFound { body: String },
    /// AniList refused the request itself, so asking again would not help.
    #[error("AniList HTTP error: {status} - {body}")]
    Rejected { status: u16, body: String },
    #[error("AniList GraphQL error: {0}")]
    GraphQl(String),
    /// An answer that was not shaped as the query asked for.
    #[error("AniList answered with an unexpected shape: {0}")]
    Unexpected(String),
}
