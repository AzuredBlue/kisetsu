//! In-memory stand-ins for qBittorrent, AniList and the feeds, and a settable clock, for tests of
//! code built on the services.

mod anilist;
mod clock;
mod feeds;
mod qbit;

pub use anilist::{AniListCall, FakeAniList, anilist_show};
pub use clock::{CountedTags, FakeClock, START};
pub use feeds::{FakeFeeds, article};
pub use qbit::{FakeQbit, QbitCall, Seed, hash_for};

#[cfg(test)]
mod tests;
