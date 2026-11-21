//! Backups: everything Kisetsu keeps, as one zip file that can be restored here or on another
//! machine.
//!
//! The zip holds a copy of the database and a small manifest. A backup from an older release goes
//! through the same migrations as an old database. Rows are copied table by table in both
//! directions, so only the columns Kisetsu knows ever leave or enter the live database, and a
//! restore that fails changes nothing.
//!
//! The qBittorrent password is never in a backup: a stored password only goes to the host it was
//! saved for, and a file that held it would undo that for anyone who can open the UI. The covers
//! and the saved AniList list are caches, and refill by themselves.

use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

use jiff::Timestamp;
use jiff::civil::DateTime;
use kisetsu_db::migrations::{LATEST_VERSION, migrate};
use kisetsu_db::rusqlite::types::Value as Sql;
use kisetsu_db::rusqlite::{Connection, params_from_iter};
use kisetsu_db::schema::{
    AppState, DismissedShow, Download, Episode, EpisodeOffset, Event, Feed, FeedItem,
    FeedItemMatch, Settings, Show,
};
use kisetsu_db::store;
use kisetsu_domain::patterns::compress_home_path;
use serde::Serialize;
use serde_json::Value;
use tracing::info;
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

use crate::context::{Caches, Context, iso_utc, setup_completed};
use crate::error::Result;

const FORMAT: i64 = 1;
const DB_FILE: &str = "kisetsu.db";
const MANIFEST_FILE: &str = "manifest.json";
/// Far past any real database; stops a damaged or hostile file from filling the disk.
const MAX_BACKUP_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_MANIFEST_BYTES: u64 = 1024 * 1024;
const THEMES: [&str; 3] = ["system", "light", "dark"];

/// Every table, parents before the tables that refer to them.
const TABLES: [&str; 11] = [
    "settings",
    "app_state",
    "feeds",
    "shows",
    "dismissed_shows",
    "episodes",
    "feed_items",
    "downloads",
    "feed_item_matches",
    "episode_offsets",
    "events",
];

/// Why a backup could not be written, read or restored. The messages are shown to the user.
#[derive(Debug, thiserror::Error)]
pub enum BackupError {
    #[error("This file is not a Kisetsu backup, or it is damaged.")]
    NotABackup,
    #[error("This backup comes from a newer version of Kisetsu. Update Kisetsu, then restore it.")]
    FromNewerRelease,
    #[error("The backup could not be restored, so nothing changed: {0}")]
    NotRestored(String),
    #[error("The backup could not be written: {0}")]
    Write(String),
}

/// What a database holds, for the user to compare before restoring.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    pub shows: i64,
    pub feeds: i64,
    pub downloaded: i64,
    pub qbit_host: String,
    pub anilist_username: String,
}

/// A checked backup, unpacked and migrated, ready to restore.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Staged {
    pub db_path: PathBuf,
    pub summary: Summary,
    pub exported_at: Option<String>,
    pub theme: Option<String>,
}

/// What a restore would change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Preview {
    pub exported_at: Option<String>,
    pub backup: Summary,
    pub current: Summary,
    pub password_needed: bool,
}

/// What a restore did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Restored {
    /// Where the data that was replaced was saved, when there was a file to save it beside.
    pub saved_as: Option<String>,
    pub theme: Option<String>,
    pub shows: i64,
}

#[derive(Serialize)]
struct Manifest<'a> {
    app: &'a str,
    format: i64,
    schema: i64,
    exported_at: String,
    theme: Option<&'a str>,
}

fn write_failed(error: impl std::fmt::Display) -> BackupError {
    BackupError::Write(error.to_string())
}

fn restore_failed(error: impl std::fmt::Display) -> BackupError {
    BackupError::NotRestored(error.to_string())
}

/// A file that cannot be read as a backup, whatever the reason.
fn unreadable<E>(_: E) -> BackupError {
    BackupError::NotABackup
}

/// The columns of a table, in order.
fn columns(conn: &Connection, table: &str) -> kisetsu_db::rusqlite::Result<Vec<String>> {
    conn.prepare(&format!("PRAGMA table_info(\"{table}\")"))?
        .query_map([], |row| row.get(1))?
        .collect()
}

/// Replaces the target's rows with the source's, inside the transaction the caller began.
fn copy_tables(source: &Connection, target: &Connection) -> kisetsu_db::rusqlite::Result<()> {
    target.execute_batch("PRAGMA defer_foreign_keys=ON")?;
    for table in TABLES.iter().rev() {
        target.execute(&format!("DELETE FROM \"{table}\""), [])?;
    }
    for table in TABLES {
        let names = columns(target, table)?;
        let list = names
            .iter()
            .map(|name| format!("\"{name}\""))
            .collect::<Vec<_>>()
            .join(", ");
        let marks = vec!["?"; names.len()].join(", ");
        let mut insert = target.prepare(&format!(
            "INSERT INTO \"{table}\" ({list}) VALUES ({marks})"
        ))?;
        let mut select = source.prepare(&format!("SELECT {list} FROM \"{table}\""))?;
        let mut rows = select.query([])?;
        while let Some(row) = rows.next()? {
            let mut values = (0..names.len())
                .map(|index| row.get::<_, Sql>(index))
                .collect::<kisetsu_db::rusqlite::Result<Vec<_>>>()?;
            if table == "settings" {
                if let Some(at) = names.iter().position(|name| name == "qbit_password") {
                    values[at] = Sql::Text(String::new());
                }
            }
            insert.execute(params_from_iter(values))?;
        }
    }
    Ok(())
}

/// Removes rows whose parent is gone, as nothing can use them and a restore would refuse them.
/// Removing one can orphan another.
fn drop_orphans(conn: &Connection) -> kisetsu_db::rusqlite::Result<()> {
    loop {
        let orphans: Vec<(String, i64)> = conn
            .prepare("PRAGMA foreign_key_check")?
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<kisetsu_db::rusqlite::Result<_>>()?;
        if orphans.is_empty() {
            return Ok(());
        }
        for (table, rowid) in orphans {
            conn.execute(&format!("DELETE FROM \"{table}\" WHERE rowid = ?"), [rowid])?;
        }
    }
}

fn count(conn: &Connection, sql: &str) -> kisetsu_db::rusqlite::Result<i64> {
    conn.query_row(sql, [], |row| row.get(0))
}

fn summary(conn: &Connection) -> kisetsu_db::rusqlite::Result<Summary> {
    let (qbit_host, anilist_username) = conn.query_row(
        "SELECT qbit_host, anilist_username FROM settings",
        [],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )?;
    Ok(Summary {
        shows: count(conn, "SELECT count(*) FROM shows")?,
        feeds: count(conn, "SELECT count(*) FROM feeds")?,
        downloaded: count(
            conn,
            "SELECT count(*) FROM episodes WHERE status = 'COMPLETED'",
        )?,
        qbit_host,
        anilist_username,
    })
}

/// Writes everything to the zip file `target`.
///
/// Must run on the engine thread, so no job writes halfway through the copy.
pub fn write_backup(
    ctx: &Context<'_>,
    target: &Path,
    theme: Option<&str>,
) -> std::result::Result<(), BackupError> {
    let scratch = tempfile::Builder::new()
        .prefix("kisetsu-backup-")
        .tempdir()
        .map_err(write_failed)?;
    let db_path = scratch.path().join(DB_FILE);
    {
        let out = store::open(&db_path).map_err(write_failed)?;
        out.execute_batch("PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE")
            .map_err(write_failed)?;
        copy_tables(ctx.conn, &out)
            .and_then(|()| drop_orphans(&out))
            .and_then(|()| out.execute_batch("COMMIT"))
            .map_err(write_failed)?;
        store::close(out).map_err(write_failed)?;
    }
    let manifest = Manifest {
        app: "kisetsu",
        format: FORMAT,
        schema: LATEST_VERSION,
        exported_at: iso_utc(ctx.now),
        theme: theme.filter(|theme| THEMES.contains(theme)),
    };
    let manifest = serde_json::to_string_pretty(&manifest).map_err(write_failed)?;
    zip_up(target, &manifest, &db_path).map_err(write_failed)
}

fn zip_up(
    target: &Path,
    manifest: &str,
    db_path: &Path,
) -> std::result::Result<(), zip::result::ZipError> {
    let mut bundle = ZipWriter::new(File::create(target)?);
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    bundle.start_file(MANIFEST_FILE, options)?;
    bundle.write_all(manifest.as_bytes())?;
    bundle.start_file(DB_FILE, options)?;
    io::copy(&mut File::open(db_path)?, &mut bundle)?;
    bundle.finish()?;
    Ok(())
}

/// Unpacks the database and returns the manifest. Only the two known names are ever read from the
/// zip.
fn extract(archive: &Path, db_path: &Path) -> std::result::Result<Value, BackupError> {
    let mut bundle =
        ZipArchive::new(File::open(archive).map_err(unreadable)?).map_err(unreadable)?;
    let manifest = {
        let entry = bundle.by_name(MANIFEST_FILE).map_err(unreadable)?;
        if entry.size() > MAX_MANIFEST_BYTES {
            return Err(BackupError::NotABackup);
        }
        let mut text = Vec::new();
        entry
            .take(MAX_MANIFEST_BYTES)
            .read_to_end(&mut text)
            .map_err(unreadable)?;
        serde_json::from_slice::<Value>(&text).map_err(unreadable)?
    };
    let mut packed = bundle.by_name(DB_FILE).map_err(unreadable)?;
    if packed.size() > MAX_BACKUP_BYTES {
        return Err(BackupError::NotABackup);
    }
    let mut out = File::create(db_path).map_err(unreadable)?;
    io::copy(&mut (&mut packed).take(MAX_BACKUP_BYTES), &mut out).map_err(unreadable)?;
    let known = manifest["app"] == "kisetsu";
    let Some(format) = manifest["format"].as_i64().filter(|_| known) else {
        return Err(BackupError::NotABackup);
    };
    if format > FORMAT {
        return Err(BackupError::FromNewerRelease);
    }
    Ok(manifest)
}

/// Brings the unpacked database up to date and reads every row, so a bad one is refused before
/// anything is replaced.
fn checked_summary(db_path: &Path) -> std::result::Result<Summary, BackupError> {
    let mut conn = store::connect(db_path).map_err(unreadable)?;
    let version: i64 = conn
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(unreadable)?;
    if version > LATEST_VERSION {
        return Err(BackupError::FromNewerRelease);
    }
    if version < 1 {
        return Err(BackupError::NotABackup);
    }
    migrate(&mut conn).map_err(unreadable)?;
    let intact: String = conn
        .pragma_query_value(None, "integrity_check", |row| row.get(0))
        .map_err(unreadable)?;
    let orphans = conn
        .prepare("PRAGMA foreign_key_check")
        .and_then(|mut check| check.exists([]))
        .map_err(unreadable)?;
    if intact != "ok" || orphans {
        return Err(BackupError::NotABackup);
    }
    read_every_row(&conn).map_err(unreadable)?;
    summary(&conn).map_err(unreadable)
}

/// Reads every row of every table as the app does, which refuses a value it cannot represent.
fn read_every_row(conn: &Connection) -> kisetsu_db::rusqlite::Result<()> {
    Settings::select(conn, "", [])?;
    AppState::select(conn, "", [])?;
    Feed::select(conn, "", [])?;
    Show::select(conn, "", [])?;
    DismissedShow::select(conn, "", [])?;
    Episode::select(conn, "", [])?;
    FeedItem::select(conn, "", [])?;
    Download::select(conn, "", [])?;
    FeedItemMatch::select(conn, "", [])?;
    EpisodeOffset::select(conn, "", [])?;
    Event::select(conn, "", [])?;
    Ok(())
}

/// Checks an uploaded file and unpacks it into `scratch`. Touches nothing else.
pub fn unpack(archive: &Path, scratch: &Path) -> std::result::Result<Staged, BackupError> {
    let db_path = scratch.join(DB_FILE);
    let manifest = extract(archive, &db_path)?;
    let summary = checked_summary(&db_path)?;
    let exported_at = manifest["exported_at"]
        .as_str()
        .filter(|text| text.parse::<Timestamp>().is_ok() || text.parse::<DateTime>().is_ok())
        .map(str::to_owned);
    let theme = manifest["theme"]
        .as_str()
        .filter(|theme| THEMES.contains(theme))
        .map(str::to_owned);
    Ok(Staged {
        db_path,
        summary,
        exported_at,
        theme,
    })
}

/// The saved password stays when the backup uses the same qBittorrent. Before setup is finished
/// it is only the default.
fn keeps_password(ctx: &Context<'_>, host: &str) -> Result<bool> {
    Ok(setup_completed(ctx.conn)?
        && host.trim_end_matches('/') == ctx.settings.qbit_host.trim_end_matches('/'))
}

/// What a restore would change, for the user to confirm.
pub fn preview(ctx: &Context<'_>, staged: &Staged) -> Result<Preview> {
    Ok(Preview {
        exported_at: staged.exported_at.clone(),
        backup: staged.summary.clone(),
        current: summary(ctx.conn)?,
        password_needed: !keeps_password(ctx, &staged.summary.qbit_host)?,
    })
}

/// Saves what is about to be replaced as a backup beside the database, so a wrong restore can be
/// restored away.
fn safety_copy(ctx: &Context<'_>) -> std::result::Result<Option<PathBuf>, BackupError> {
    let Some(path) = ctx
        .conn
        .path()
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
    else {
        return Ok(None);
    };
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let stamp = ctx.now.strftime("%Y%m%dT%H%M%SZ");
    let target = path.with_file_name(format!("{name}.pre-restore.{stamp}.zip"));
    write_backup(ctx, &target, None)?;
    Ok(Some(target))
}

/// Replaces everything with the backup. An error anywhere rolls back to what was there.
pub fn restore(ctx: &mut Context<'_>, staged: &Staged, password: &str) -> Result<Restored> {
    let keep = if keeps_password(ctx, &staged.summary.qbit_host)? {
        ctx.settings.qbit_password.clone()
    } else {
        String::new()
    };
    let source = store::connect(&staged.db_path).map_err(restore_failed)?;
    let saved = safety_copy(ctx).map_err(restore_failed)?;
    ctx.conn
        .execute_batch("SAVEPOINT restore")
        .map_err(restore_failed)?;
    let replaced = copy_tables(&source, ctx.conn)
        .and_then(|()| {
            ctx.conn.execute(
                "UPDATE settings SET qbit_password = ?",
                [if password.is_empty() { &keep } else { password }],
            )
        })
        .and_then(|_| ctx.conn.execute_batch("RELEASE restore"));
    if let Err(error) = replaced {
        // The savepoint is dropped either way, so nothing of the half-copied rows stays.
        let _ = ctx
            .conn
            .execute_batch("ROLLBACK TO restore; RELEASE restore");
        return Err(restore_failed(error).into());
    }
    ctx.checkpoint().map_err(restore_failed)?;
    *ctx.caches = Caches::default();
    ctx.emit(&["*"]);
    let home = std::env::home_dir().unwrap_or_default();
    let saved_as =
        saved.map(|path| compress_home_path(&path.to_string_lossy(), &home.to_string_lossy()));
    info!(
        "Restored a backup from {}. The previous data was saved as {}.",
        staged.exported_at.as_deref().unwrap_or("an unknown date"),
        saved_as.as_deref().unwrap_or("nothing")
    );
    Ok(Restored {
        saved_as,
        theme: staged.theme.clone(),
        shows: staged.summary.shows,
    })
}

#[cfg(test)]
mod tests {
    use kisetsu_db::store::{open, open_in_memory};

    use super::*;
    use crate::fakes::START;

    fn feed(name: &str) -> Feed {
        Feed {
            id: None,
            name: name.to_owned(),
            url: format!("https://{name}.test/rss"),
            priority: 1,
            qbit_path: None,
            last_fetch_at: None,
            last_ok_at: None,
            failing_since: None,
            last_error: None,
        }
    }

    #[test]
    fn every_table_is_copied() {
        let conn = open_in_memory().unwrap();
        let mut found: Vec<String> = conn
            .prepare(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'",
            )
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<std::result::Result<_, _>>()
            .unwrap();
        found.sort();
        let mut copied: Vec<String> = TABLES.iter().map(|table| (*table).to_owned()).collect();
        copied.sort();
        assert_eq!(found, copied);
    }

    #[test]
    fn a_backup_restores_what_it_held_and_never_carries_the_password() {
        let dir = tempfile::tempdir().unwrap();
        let source = open(&dir.path().join("a.db")).unwrap();
        let mut caches = Caches::default();
        {
            let mut ctx = Context::new(&source, START, &mut caches).unwrap();
            ctx.settings.qbit_host = "http://nas:8080".to_owned();
            ctx.settings.qbit_password = "secret".to_owned();
            ctx.settings.update(&source).unwrap();
            feed("one").insert(&source).unwrap();
        }
        let archive = dir.path().join("backup.zip");
        let ctx = Context::new(&source, START, &mut caches).unwrap();
        write_backup(&ctx, &archive, Some("dark")).unwrap();

        let scratch = dir.path().join("scratch");
        std::fs::create_dir(&scratch).unwrap();
        let staged = unpack(&archive, &scratch).unwrap();
        assert_eq!(staged.theme.as_deref(), Some("dark"));
        assert_eq!(staged.summary.feeds, 1);
        assert_eq!(staged.summary.qbit_host, "http://nas:8080");

        let target = open(&dir.path().join("b.db")).unwrap();
        let mut other = Caches::default();
        let mut ctx = Context::new(&target, START, &mut other).unwrap();
        assert!(preview(&ctx, &staged).unwrap().password_needed);
        let restored = restore(&mut ctx, &staged, "typed").unwrap();
        assert_eq!(restored.theme.as_deref(), Some("dark"));
        assert!(restored.saved_as.is_some());
        let feeds = Feed::select(&target, "", []).unwrap();
        assert_eq!(feeds.len(), 1);
        let settings = Settings::find(&target, "WHERE id = 1", [])
            .unwrap()
            .unwrap();
        assert_eq!(settings.qbit_password, "typed");
        assert_eq!(settings.qbit_host, "http://nas:8080");
    }

    #[test]
    fn a_restore_that_fails_halfway_changes_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let source = open(&dir.path().join("a.db")).unwrap();
        let mut caches = Caches::default();
        feed("incoming").insert(&source).unwrap();
        let archive = dir.path().join("backup.zip");
        let ctx = Context::new(&source, START, &mut caches).unwrap();
        write_backup(&ctx, &archive, None).unwrap();
        let scratch = dir.path().join("scratch");
        std::fs::create_dir(&scratch).unwrap();
        let staged = unpack(&archive, &scratch).unwrap();
        // The last table to be copied is gone from the backup.
        {
            let damaged = store::connect(&staged.db_path).unwrap();
            damaged.execute_batch("DROP TABLE events").unwrap();
        }

        let target = open(&dir.path().join("b.db")).unwrap();
        feed("keep").insert(&target).unwrap();
        let mut other = Caches::default();
        let mut ctx = Context::new(&target, START, &mut other).unwrap();
        let outcome = restore(&mut ctx, &staged, "");
        assert!(matches!(
            outcome,
            Err(crate::Error::Backup(BackupError::NotRestored(_)))
        ));
        let names: Vec<String> = Feed::select(&target, "", [])
            .unwrap()
            .into_iter()
            .map(|feed| feed.name)
            .collect();
        assert_eq!(names, ["keep"]);
        assert!(target.is_autocommit());
    }

    #[test]
    fn a_file_that_is_not_a_backup_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("x.zip");
        std::fs::write(&file, b"not a zip").unwrap();
        assert!(matches!(
            unpack(&file, dir.path()),
            Err(BackupError::NotABackup)
        ));
    }
}
