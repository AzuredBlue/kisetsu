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
    /// An explicit action on a release that was not carried out, with the reason to show.
    #[error(transparent)]
    Refused(#[from] crate::manual::Refusal),
    /// A change to a show that was not made.
    #[error(transparent)]
    ShowRefused(#[from] crate::shows::Refusal),
    #[error(transparent)]
    Backup(#[from] crate::backup::BackupError),
    /// A show that was not added.
    #[error(transparent)]
    AddShowRefused(#[from] crate::addshow::Refusal),
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
