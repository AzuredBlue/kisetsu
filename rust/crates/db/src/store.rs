//! Opening the database and the scope that groups writes.

use std::env;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, Transaction, TransactionBehavior};

use crate::error::{Error, Result};
use crate::migrations::migrate;

const APP_NAME: &str = "kisetsu";
const DB_FILE: &str = "kisetsu.db";

/// The folder Kisetsu keeps its files in: `%APPDATA%\kisetsu` on Windows, `~/.config/kisetsu`
/// elsewhere. `None` when the user has no home folder.
pub fn config_dir() -> Option<PathBuf> {
    config_dir_in(cfg!(windows), env::var_os("APPDATA"), env::home_dir())
}

fn config_dir_in(
    windows: bool,
    appdata: Option<OsString>,
    home: Option<PathBuf>,
) -> Option<PathBuf> {
    let base = if windows {
        match appdata.filter(|dir| !dir.is_empty()) {
            Some(dir) => PathBuf::from(dir),
            None => home?.join("AppData").join("Roaming"),
        }
    } else {
        home?.join(".config")
    };
    Some(base.join(APP_NAME))
}

/// The database to use: `KISETSU_DB_PATH` when set, else `kisetsu.db` in the config folder.
pub fn default_path() -> Option<PathBuf> {
    database_path(env::var_os("KISETSU_DB_PATH"), config_dir())
}

fn database_path(chosen: Option<OsString>, config_dir: Option<PathBuf>) -> Option<PathBuf> {
    match chosen.filter(|path| !path.is_empty()) {
        Some(path) => Some(PathBuf::from(path)),
        None => Some(config_dir?.join(DB_FILE)),
    }
}

fn configure(conn: &Connection) -> Result<()> {
    conn.pragma_update(None, "foreign_keys", "ON")?;
    conn.pragma_update_and_check(None, "journal_mode", "WAL", |_| Ok(()))?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "busy_timeout", 5000)?;
    // The database is small and the disk cache holds it, so each connection keeps few pages.
    conn.pragma_update(None, "cache_size", -128)?;
    Ok(())
}

/// A connection to the database at `path` with the app's settings, created if missing. Its schema
/// is left as found; `open` also migrates it.
pub fn connect(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)?;
    configure(&conn)?;
    Ok(conn)
}

/// Opens the database at `path`, creating it when missing, and brings it to the latest schema.
pub fn open(path: &Path) -> Result<Connection> {
    if let Some(folder) = path
        .parent()
        .filter(|folder| !folder.as_os_str().is_empty())
    {
        create_private_dir(folder)?;
    }
    let mut conn = connect(path)?;
    migrate(&mut conn)?;
    restrict_to_owner(path);
    Ok(conn)
}

/// A migrated database that lives only as long as its connection.
pub fn open_in_memory() -> Result<Connection> {
    let mut conn = Connection::open_in_memory()?;
    configure(&conn)?;
    migrate(&mut conn)?;
    Ok(conn)
}

/// Lets SQLite refresh its statistics, then closes the connection, which folds the write-ahead log
/// back into the database file.
pub fn close(conn: Connection) -> Result<()> {
    conn.execute_batch("PRAGMA optimize")?;
    conn.close().map_err(|(_, error)| Error::from(error))
}

/// Runs `work` in one write transaction: committed when it returns `Ok`, rolled back on any error.
/// The write lock is taken up front, so a transaction never fails halfway through on a competing
/// writer. Reads don't belong here: they would queue behind every writer holding the lock.
pub fn session_scope<T, E: From<rusqlite::Error>>(
    conn: &mut Connection,
    work: impl FnOnce(&Transaction<'_>) -> Result<T, E>,
) -> Result<T, E> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let value = work(&tx)?;
    tx.commit()?;
    Ok(value)
}

/// Runs `work` on a consistent view of the database that is never saved: the transaction is rolled
/// back whatever `work` returns. No lock is taken up front, so a reader never queues behind a
/// writer; a write `work` makes is undone, though it still needs the write lock while it runs.
pub fn read_scope<T, E: From<rusqlite::Error>>(
    conn: &mut Connection,
    work: impl FnOnce(&Transaction<'_>) -> Result<T, E>,
) -> Result<T, E> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Deferred)?;
    let outcome = work(&tx);
    tx.rollback()?;
    outcome
}

fn create_private_dir(folder: &Path) -> std::io::Result<()> {
    if folder.exists() {
        return Ok(());
    }
    fs::create_dir_all(folder)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(folder, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

/// The database holds the user's qBittorrent password: on Unix only the owner may read it.
fn restrict_to_owner(path: &Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = path;
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;
    use crate::schema::{Feed, Show};

    #[test]
    fn the_config_folder_follows_the_platform() {
        let home = Some(PathBuf::from("home"));
        assert_eq!(
            config_dir_in(true, Some("appdata".into()), home.clone()),
            Some(PathBuf::from("appdata").join("kisetsu"))
        );
        assert_eq!(
            config_dir_in(true, Some("".into()), home.clone()),
            Some(
                PathBuf::from("home")
                    .join("AppData")
                    .join("Roaming")
                    .join("kisetsu")
            )
        );
        assert_eq!(
            config_dir_in(false, Some("appdata".into()), home),
            Some(PathBuf::from("home").join(".config").join("kisetsu"))
        );
        assert_eq!(config_dir_in(false, None, None), None);
    }

    #[test]
    fn the_environment_choice_beats_the_config_folder() {
        let config = Some(PathBuf::from("cfg"));
        assert_eq!(
            database_path(Some("x.db".into()), config.clone()),
            Some(PathBuf::from("x.db"))
        );
        assert_eq!(
            database_path(Some("".into()), config.clone()),
            Some(PathBuf::from("cfg").join("kisetsu.db"))
        );
        assert_eq!(database_path(None, None), None);
    }

    #[test]
    fn a_file_database_opens_with_the_app_pragmas_and_survives_a_reopen() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("nested").join("k.db");
        let conn = open(&path).unwrap();
        let pragma = |name: &str| -> i64 {
            conn.pragma_query_value(None, name, |row| row.get(0))
                .unwrap()
        };
        assert_eq!(pragma("foreign_keys"), 1);
        assert_eq!(pragma("synchronous"), 1);
        assert_eq!(pragma("busy_timeout"), 5000);
        let mode: String = conn
            .pragma_query_value(None, "journal_mode", |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
        close(conn).unwrap();
        assert!(open(&path).is_ok());
    }

    #[test]
    fn a_scope_commits_on_success_and_rolls_back_on_error() {
        let mut conn = open_in_memory().unwrap();
        let feed = Feed {
            id: None,
            name: "n".into(),
            url: "u".into(),
            priority: 1,
            qbit_path: None,
            last_fetch_at: None,
            last_ok_at: None,
            failing_since: None,
            last_error: None,
        };
        let kept: Result<i64> = session_scope(&mut conn, |tx| Ok(feed.insert(tx)?));
        assert_eq!(kept.unwrap(), 1);
        let lost: Result<()> = session_scope(&mut conn, |tx| {
            let mut second = feed.clone();
            second.url = "v".into();
            second.insert(tx)?;
            Err(Error::NoSchemaVersion)
        });
        assert!(lost.is_err());
        let count: i64 = conn
            .query_row("SELECT count(*) FROM feeds", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn a_read_scope_sees_committed_rows_and_keeps_nothing_it_wrote() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("k.db");
        let mut writer = open(&path).unwrap();
        let mut reader = connect(&path).unwrap();
        let feed = Feed {
            id: None,
            name: "n".into(),
            url: "u".into(),
            priority: 1,
            qbit_path: None,
            last_fetch_at: None,
            last_ok_at: None,
            failing_since: None,
            last_error: None,
        };
        session_scope(&mut writer, |tx| Ok::<_, Error>(feed.insert(tx)?)).unwrap();
        let count = |conn: &Connection| -> i64 {
            conn.query_row("SELECT count(*) FROM feeds", [], |row| row.get(0))
                .unwrap()
        };
        let seen = read_scope(&mut reader, |tx| {
            let mut second = feed.clone();
            second.url = "v".into();
            second.insert(tx)?;
            Ok::<_, Error>(count(tx))
        });
        assert_eq!(seen.unwrap(), 2);
        assert_eq!(count(&reader), 1);
        assert_eq!(count(&writer), 1);
    }

    #[test]
    fn rows_are_selected_found_and_updated_by_their_first_column() {
        let conn = open_in_memory().unwrap();
        let mut feed = Feed {
            id: None,
            name: "n".into(),
            url: "u".into(),
            priority: 2,
            qbit_path: None,
            last_fetch_at: None,
            last_ok_at: None,
            failing_since: None,
            last_error: None,
        };
        feed.id = Some(feed.insert(&conn).unwrap());
        feed.name = "renamed".into();
        assert_eq!(feed.update(&conn).unwrap(), 1);
        let found = Feed::find(&conn, "WHERE url = ?", ["u"]).unwrap();
        assert_eq!(found, Some(feed.clone()));
        assert_eq!(Feed::select(&conn, "ORDER BY id", []).unwrap(), [feed]);
        assert_eq!(Feed::find(&conn, "WHERE url = ?", ["x"]).unwrap(), None);
    }

    #[test]
    fn a_row_holding_an_unknown_enum_name_is_an_error() {
        let conn = open_in_memory().unwrap();
        conn.execute("INSERT INTO shows (anilist_id, aliases_json, custom_aliases_json, feed_state, save_folder, paused, completed) VALUES (1, '[]', '[]', 'locked', '', 0, 0)", [])
            .unwrap();
        let sql = format!("SELECT {} FROM shows", Show::COLUMNS);
        let read = conn.query_row(&sql, [], Show::from_row);
        assert!(read.is_err());
    }
}
