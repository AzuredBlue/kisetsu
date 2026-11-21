//! Why a service call failed.

use kisetsu_clients::qbittorrent::QbitError;
use kisetsu_db::rusqlite;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Database(#[from] rusqlite::Error),
    #[error(transparent)]
    Qbit(#[from] QbitError),
    #[error("qBittorrent is not set up")]
    NoQbit,
    /// No source of tags for the torrents being added was given.
    #[error("no source of download tags is set up")]
    NoTagSource,
    /// The show is locked to another feed and the caller did not agree to release it.
    #[error("'{title}' is locked to another feed")]
    FeedLocked { title: String },
    #[error("'{name}' already uses that URL")]
    FeedUrlTaken { name: String },
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
