use std::io;

/// A stored enum column holds a name this build does not know.
#[derive(Debug, thiserror::Error)]
#[error("unknown {kind} {name:?}")]
pub struct UnknownName {
    pub kind: &'static str,
    pub name: String,
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error(
        "This database has schema version {found}, newer than this Kisetsu understands ({latest}). \
         It belongs to a different release; point Kisetsu at a different database file."
    )]
    SchemaTooNew { found: i64, latest: i64 },
    #[error("This database has tables but no Kisetsu schema version; refusing to touch it.")]
    NoSchemaVersion,
    #[error("Not a stored timestamp: {0:?}")]
    BadTimestamp(String),
    #[error("The time zone database has no {0}")]
    MissingTimeZone(&'static str),
}

pub type Result<T, E = Error> = std::result::Result<T, E>;
