//! Clients of the outside world: the sites Kisetsu reads feeds from, qBittorrent, AniList.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

use std::sync::{Mutex, MutexGuard};

mod net;
mod url;

pub mod anilist;
pub mod fetch;
pub mod notify;
pub mod palette;
pub mod qbittorrent;
pub mod rss;
pub mod sandbox;
pub mod sites;
pub mod toast;

pub mod traffic;
pub mod update;

pub use net::online;

/// Locks the mutex, also after a thread panicked while holding it.
fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}
