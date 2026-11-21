//! The engine: one thread that does everything touching the database or qBittorrent, the
//! scheduler that decides when each job runs, and the jobs themselves.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

pub mod error;
pub mod instance;
pub mod scheduler;

pub use error::{Error, Result};
