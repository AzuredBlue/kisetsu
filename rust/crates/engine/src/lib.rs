//! The engine: one thread that does everything touching the database or qBittorrent, the
//! scheduler that decides when each job runs, and the jobs themselves.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

use std::sync::{Mutex, MutexGuard};

pub mod bus;
pub mod error;
pub mod instance;
pub mod jobs;
mod parallel;
pub mod remote;
pub mod runner;
pub mod scheduler;
#[cfg(test)]
mod testing;

pub use error::{Error, Result};

/// Locks the mutex, also after a thread panicked while holding it.
fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}
