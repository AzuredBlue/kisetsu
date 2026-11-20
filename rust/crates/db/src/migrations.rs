//! Brings a database up to the latest schema, one step at a time. A step upgrades the layout the
//! step before it left; the version lives in `PRAGMA user_version`.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use jiff::Timestamp;
use jiff::civil::DateTime;
use jiff::tz::TimeZone;
use rusqlite::backup::Backup;
use rusqlite::{Connection, Transaction, TransactionBehavior};

use crate::ddl::CREATE_STATEMENTS;
use crate::error::{Error, Result};
use crate::schema::Settings;
use crate::storage::format_timestamp;

type Step = fn(&Transaction<'_>) -> Result<()>;

/// Each step upgrades the schema from the version before it. Steps after the first are plain SQL
/// against the previous layout.
const MIGRATIONS: [(i64, Step); 11] = [
    (1, v1),
    (2, v2),
    (3, v3),
    (4, v4),
    (5, v5),
    (6, v6),
    (7, v7),
    (8, v8),
    (9, v9),
    (10, v10),
    (11, v11),
];

pub const LATEST_VERSION: i64 = MIGRATIONS[MIGRATIONS.len() - 1].0;

/// The migration backups kept next to the database: every one a migration just made, and the newest
/// of the earlier ones up to this many in all. A run through several steps keeps the copy from
/// before its first.
pub const BACKUPS_KEPT: usize = 3;

fn columns(tx: &Transaction<'_>, table: &str) -> Result<Vec<String>> {
    let mut statement = tx.prepare("SELECT name FROM pragma_table_info(?1)")?;
    let names = statement.query_map([table], |row| row.get(0))?;
    Ok(names.collect::<rusqlite::Result<_>>()?)
}

fn add_column(tx: &Transaction<'_>, table: &str, column: &str, definition: &str) -> Result<()> {
    if !columns(tx, table)?.iter().any(|name| name == column) {
        tx.execute_batch(&format!(
            "ALTER TABLE {table} ADD COLUMN {column} {definition}"
        ))?;
    }
    Ok(())
}

fn v1(tx: &Transaction<'_>) -> Result<()> {
    for statement in CREATE_STATEMENTS {
        tx.execute_batch(statement)?;
    }
    Ok(())
}

fn v2(tx: &Transaction<'_>) -> Result<()> {
    add_column(
        tx,
        "settings",
        "feed_refresh_minutes",
        "INTEGER NOT NULL DEFAULT 15",
    )
}

fn v3(tx: &Transaction<'_>) -> Result<()> {
    add_column(tx, "shows", "qbit_tag", "VARCHAR")
}

fn v4(tx: &Transaction<'_>) -> Result<()> {
    add_column(tx, "downloads", "batch", "BOOLEAN NOT NULL DEFAULT 0")
}

fn v5(tx: &Transaction<'_>) -> Result<()> {
    add_column(tx, "shows", "cover_color", "VARCHAR")
}

fn v6(tx: &Transaction<'_>) -> Result<()> {
    add_column(tx, "shows", "cover_edge", "VARCHAR")?;
    add_column(tx, "shows", "cover_edge_src", "VARCHAR")
}

/// A replacement that gave up used to fail for good, which keeps its version from being tried again.
fn v7(tx: &Transaction<'_>) -> Result<()> {
    tx.execute_batch(
        "UPDATE downloads SET state = 'CANCELED' WHERE state = 'FAILED' AND supersedes_id IS NOT NULL \
         AND (last_error LIKE 'Gave up after%' OR last_error LIKE 'Replacement did not finish%')",
    )?;
    Ok(())
}

/// The retry ladders used to count earlier endings by matching the error text; they count this
/// column now.
fn v8(tx: &Transaction<'_>) -> Result<()> {
    add_column(tx, "downloads", "end_reason", "VARCHAR")?;
    tx.execute_batch(
        "UPDATE downloads SET end_reason = 'ADD_GAVE_UP' \
         WHERE state IN ('CANCELED', 'FAILED') AND last_error LIKE 'Gave up after % attempts%'",
    )?;
    tx.execute_batch(
        "UPDATE downloads SET end_reason = 'REPLACEMENT_GAVE_UP' \
         WHERE state IN ('CANCELED', 'FAILED') AND last_error LIKE 'Gave up after % hours%'",
    )?;
    tx.execute_batch(
        "UPDATE downloads SET end_reason = 'REMOVED' \
         WHERE state = 'CANCELED' AND last_error = 'Torrent is no longer present in qBittorrent; queued for a safe retry.'",
    )?;
    Ok(())
}

/// The minimum resolution was fixed at 1080p; it is a setting now, starting where it was.
fn v9(tx: &Transaction<'_>) -> Result<()> {
    add_column(
        tx,
        "settings",
        "min_resolution",
        "INTEGER NOT NULL DEFAULT 1080",
    )
}

/// SubsPlease's RSS writes US Pacific time marked +0000, and its items were stored that way. Items
/// its search API gave (their torrent is a magnet) had nearly right dates, so they stay.
fn v10(tx: &Transaction<'_>) -> Result<()> {
    let pacific = TimeZone::get("America/Los_Angeles")
        .map_err(|_| Error::MissingTimeZone("America/Los_Angeles"))?;
    let feeds: Vec<(i64, String)> = tx
        .prepare("SELECT id, url FROM feeds")?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    for (feed_id, _) in feeds
        .iter()
        .filter(|(_, url)| host_of(url).as_deref() == Some("subsplease.org"))
    {
        let items: Vec<(i64, String)> = tx
            .prepare(
                "SELECT id, published_at FROM feed_items WHERE feed_id = ? AND published_at IS NOT NULL \
                 AND (torrent_url IS NULL OR torrent_url NOT LIKE 'magnet:%')",
            )?
            .query_map([feed_id], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        for (item_id, stored) in items {
            let wall: DateTime = stored
                .parse()
                .map_err(|_| Error::BadTimestamp(stored.clone()))?;
            if wall.year() <= 2000 {
                continue;
            }
            let fixed = wall
                .to_zoned_datetime(pacific.clone())
                .map_err(|_| Error::BadTimestamp(stored.clone()))?
                .timestamp();
            tx.execute(
                "UPDATE feed_items SET published_at = ? WHERE id = ?",
                (format_timestamp(fixed), item_id),
            )?;
        }
    }
    Ok(())
}

/// The lower-case host of a `scheme://authority/...` URL without a leading `www.`.
fn host_of(url: &str) -> Option<String> {
    let (scheme, rest) = url.split_once("//")?;
    let is_scheme = scheme.strip_suffix(':').is_some_and(|name| {
        name.starts_with(|c: char| c.is_ascii_alphabetic())
            && name
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c))
    });
    if !scheme.is_empty() && !is_scheme {
        return None;
    }
    let authority = rest.split(['/', '?', '#']).next()?;
    let host_and_port = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    let host = host_and_port.split(':').next()?.to_lowercase();
    Some(match host.strip_prefix("www.") {
        Some(bare) => bare.to_owned(),
        None => host,
    })
}

/// Deleting watched episodes: the setting, and each show's AniList progress and how far it was
/// acted on.
fn v11(tx: &Transaction<'_>) -> Result<()> {
    add_column(
        tx,
        "settings",
        "delete_watched",
        "BOOLEAN NOT NULL DEFAULT 0",
    )?;
    add_column(tx, "shows", "watched", "INTEGER")?;
    add_column(tx, "shows", "watched_applied", "INTEGER")
}

fn user_version(conn: &Connection) -> Result<i64> {
    Ok(conn.pragma_query_value(None, "user_version", |row| row.get(0))?)
}

/// Brings the database up to `LATEST_VERSION`, backing it up before any step on existing data.
/// Each step runs in one transaction with its version number, so one that stops partway leaves
/// nothing behind.
pub fn migrate(conn: &mut Connection) -> Result<()> {
    migrate_with(conn, &MIGRATIONS)?;
    let has_settings: bool = conn.query_row(
        "SELECT EXISTS (SELECT 1 FROM settings WHERE id = 1)",
        [],
        |row| row.get(0),
    )?;
    if !has_settings {
        Settings::default().insert(conn)?;
    }
    Ok(())
}

fn migrate_with(conn: &mut Connection, steps: &[(i64, Step)]) -> Result<()> {
    let latest = steps.last().map_or(0, |(version, _)| *version);
    let mut version = user_version(conn)?;
    if version > latest {
        return Err(Error::SchemaTooNew {
            found: version,
            latest,
        });
    }
    if version == 0 {
        let tables: i64 = conn.query_row(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'",
            [],
            |row| row.get(0),
        )?;
        if tables > 0 {
            return Err(Error::NoSchemaVersion);
        }
    }

    // A new database holds nothing worth a backup, also after its first steps.
    let existing = version > 0;
    let path = conn.path().map(PathBuf::from);
    let mut made = Vec::new();
    for (target, step) in steps {
        if *target <= version {
            continue;
        }
        if let Some(path) = path.as_deref().filter(|path| existing && path.exists()) {
            made.push(backup(conn, path, version)?);
        }
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        step(&tx)?;
        tx.pragma_update(None, "user_version", target)?;
        tx.commit()?;
        version = *target;
    }
    if !made.is_empty() {
        if let Some(path) = path.as_deref() {
            prune_backups(path, &made);
        }
    }
    Ok(())
}

fn backup_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Copies the database to `<db>.v<version>.<UTC stamp>.bak`. The database runs in WAL mode, so
/// copying the file alone would miss what has not been checkpointed yet.
fn backup(conn: &Connection, path: &Path, version: i64) -> Result<PathBuf> {
    let stamp = Timestamp::now().strftime("%Y%m%dT%H%M%SZ");
    let target = path.with_file_name(format!("{}.v{version}.{stamp}.bak", backup_name(path)));
    create_private(&target)?;
    let mut copy = Connection::open(&target)?;
    Backup::new(conn, &mut copy)?.run_to_completion(i32::MAX, Duration::ZERO, None)?;
    copy.close().map_err(|(_, error)| error)?;
    tracing::info!(
        "Backed up {} to {} before migrating.",
        backup_name(path),
        backup_name(&target)
    );
    Ok(target)
}

fn create_private(path: &Path) -> std::io::Result<()> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path).map(drop)
}

fn prune_backups(path: &Path, made: &[PathBuf]) {
    let name = backup_name(path);
    let folder = match path.parent() {
        Some(folder) if !folder.as_os_str().is_empty() => folder,
        _ => Path::new("."),
    };
    let Ok(entries) = fs::read_dir(folder) else {
        return;
    };
    let mut earlier: Vec<(String, PathBuf)> = entries
        .filter_map(|entry| {
            let candidate = entry.ok()?.path();
            let stamp = {
                let file = candidate.file_name()?.to_str()?;
                let middle = file
                    .strip_prefix(&name)?
                    .strip_prefix(".v")?
                    .strip_suffix(".bak")?;
                let (version, stamp) = middle.split_once('.')?;
                let is_version = !version.is_empty() && version.chars().all(|c| c.is_ascii_digit());
                is_version.then(|| stamp.to_owned())?
            };
            (!made.contains(&candidate)).then_some((stamp, candidate))
        })
        .collect();
    earlier.sort();
    let keep = BACKUPS_KEPT.saturating_sub(made.len());
    for (_, old) in &earlier[..earlier.len().saturating_sub(keep)] {
        if let Err(error) = fs::remove_file(old) {
            tracing::warn!(
                "Could not delete the old backup {}: {error}",
                backup_name(old)
            );
        }
    }
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    fn memory() -> Connection {
        Connection::open_in_memory().unwrap()
    }

    #[test]
    fn a_new_database_is_stamped_and_has_one_settings_row() {
        let mut conn = memory();
        migrate(&mut conn).unwrap();
        assert_eq!(user_version(&conn).unwrap(), LATEST_VERSION);
        let rows: i64 = conn
            .query_row("SELECT count(*) FROM settings", [], |row| row.get(0))
            .unwrap();
        assert_eq!(rows, 1);
        migrate(&mut conn).unwrap();
        let rows: i64 = conn
            .query_row("SELECT count(*) FROM settings", [], |row| row.get(0))
            .unwrap();
        assert_eq!(rows, 1);
    }

    #[test]
    fn a_database_with_foreign_tables_is_refused() {
        let mut conn = memory();
        conn.execute_batch("CREATE TABLE other (x)").unwrap();
        assert!(matches!(migrate(&mut conn), Err(Error::NoSchemaVersion)));
    }

    #[test]
    fn a_step_that_fails_leaves_neither_schema_nor_version_behind() {
        fn breaks(tx: &Transaction<'_>) -> Result<()> {
            tx.execute_batch("CREATE TABLE half (x); SELECT * FROM missing")?;
            Ok(())
        }
        let mut conn = memory();
        migrate_with(&mut conn, &[(1, v1)]).unwrap();
        let steps: [(i64, Step); 2] = [(1, v1), (2, breaks)];
        assert!(migrate_with(&mut conn, &steps).is_err());
        assert_eq!(user_version(&conn).unwrap(), 1);
        let half: i64 = conn
            .query_row(
                "SELECT count(*) FROM sqlite_master WHERE name = 'half'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(half, 0);
    }

    fn make_backups(folder: &Path, names: &[&str]) {
        for name in names {
            fs::write(folder.join(name), b"").unwrap();
        }
    }

    fn files(folder: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(folder)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        names
    }

    #[test]
    fn pruning_keeps_the_new_backups_and_the_newest_earlier_ones() {
        let folder = tempfile::tempdir().unwrap();
        let db = folder.path().join("k.db");
        make_backups(
            folder.path(),
            &[
                "k.db.v5.20260101T000000Z.bak",
                "k.db.v6.20260102T000000Z.bak",
                "k.db.v7.20260103T000000Z.bak",
                "k.db.vx.20260104T000000Z.bak",
                "k.db.v8.bak",
                "k.db.pre-import.bak",
                "other.db.v1.20260101T000000Z.bak",
                "k.db.v9.20260105T000000Z.bak",
            ],
        );
        prune_backups(&db, &[folder.path().join("k.db.v9.20260105T000000Z.bak")]);
        assert_eq!(
            files(folder.path()),
            [
                "k.db.pre-import.bak",
                "k.db.v6.20260102T000000Z.bak",
                "k.db.v7.20260103T000000Z.bak",
                "k.db.v8.bak",
                "k.db.v9.20260105T000000Z.bak",
                "k.db.vx.20260104T000000Z.bak",
                "other.db.v1.20260101T000000Z.bak",
            ]
        );
    }

    #[test]
    fn hosts_are_read_the_way_feed_urls_are_written() {
        assert_eq!(
            host_of("https://subsplease.org/rss/?t&r=1080"),
            Some("subsplease.org".into())
        );
        assert_eq!(
            host_of("https://www.SubsPlease.org:443/rss"),
            Some("subsplease.org".into())
        );
        assert_eq!(
            host_of("http://user:pw@subsplease.org?x=1"),
            Some("subsplease.org".into())
        );
        assert_eq!(
            host_of("//subsplease.org/rss"),
            Some("subsplease.org".into())
        );
        assert_eq!(host_of("https://nyaa.si/?page=rss"), Some("nyaa.si".into()));
        assert_eq!(host_of("subsplease.org/rss"), None);
        assert_eq!(host_of("not a url"), None);
    }
}
