//! Clients of the outside world: the sites Kisetsu reads feeds from, qBittorrent, AniList.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod net;
mod url;

pub mod anilist;
pub mod fetch;
pub mod palette;
pub mod qbittorrent;
pub mod rss;
pub mod sandbox;
pub mod sites;

pub mod traffic;
