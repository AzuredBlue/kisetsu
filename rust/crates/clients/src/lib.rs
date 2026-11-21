//! Clients of the outside world: the sites Kisetsu reads feeds from, qBittorrent, AniList.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod net;
mod url;

pub mod fetch;
pub mod rss;

pub mod traffic;
