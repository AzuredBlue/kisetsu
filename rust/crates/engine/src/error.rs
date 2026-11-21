//! Why engine work did not finish.

use std::io;

use kisetsu_db::rusqlite;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Service(#[from] kisetsu_services::Error),
    #[error(transparent)]
    Database(#[from] kisetsu_db::Error),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Io(#[from] io::Error),
    /// The engine is shutting down and takes no more work.
    #[error("Kisetsu is shutting down")]
    Stopped,
    /// Jobs were still running after the grace they were given, so the work that needed them
    /// stopped was not done.
    #[error("A job is still running ({}); try again in a moment.", .0.join(", "))]
    JobsRunning(Vec<String>),
    /// The work stopped on a bug of ours instead of returning.
    #[error("{0}")]
    Panicked(String),
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
