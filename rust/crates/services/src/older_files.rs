//! The files a batch replaces: when a show is added from a batch that holds newer versions of
//! files already in its folder, those older files are deleted once the batch has finished.
//!
//! Each wait is one `app_state` entry keyed by the first of the batch's downloads, so it needs no
//! table of its own.

use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;

use kisetsu_clients::qbittorrent::Torrent;
use kisetsu_db::schema::{Download, DownloadState};
use serde::{Deserialize, Serialize};
use tracing::{debug, info, warn};

use crate::context::{Context, set_state};
use crate::downloads::is_final_failure;
use crate::error::{Error, Result};

const KEY_PREFIX: &str = "older_files:";

#[derive(Debug, Serialize, Deserialize)]
struct Wait {
    /// The show's name for the log.
    show: String,
    /// Every download of the batch's torrent.
    downloads: Vec<i64>,
    /// The paths to delete.
    files: Vec<String>,
    /// The paths of the batch's own files, which are never deleted.
    batch_files: Vec<String>,
}

/// A path compared without its case or separator style.
fn path_key(path: &str) -> String {
    path.replace('\\', "/").to_lowercase()
}

/// Waits for the batch whose `downloads` are given to finish, then deletes `files`.
pub fn wait_for_batch(
    ctx: &Context<'_>,
    show: &str,
    downloads: &[i64],
    files: &[String],
    batch_files: &[String],
) -> Result<()> {
    let Some(first) = downloads.first() else {
        return Ok(());
    };
    let wait = Wait {
        show: show.to_owned(),
        downloads: downloads.to_vec(),
        files: files.to_vec(),
        batch_files: batch_files.to_vec(),
    };
    set_state(
        ctx.conn,
        &format!("{KEY_PREFIX}{first}"),
        &serde_json::to_string(&wait).map_err(|error| Error::Invalid(error.to_string()))?,
    )
}

fn waits(ctx: &Context<'_>) -> Result<Vec<(String, String)>> {
    Ok(ctx
        .conn
        .prepare_cached("SELECT key, value FROM app_state WHERE key LIKE ? ORDER BY key")?
        .query_map([format!("{KEY_PREFIX}%")], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })?
        .collect::<std::result::Result<_, _>>()?)
}

/// What the batch's downloads came to.
enum Outcome {
    Finished,
    /// One may still finish.
    Pending,
    /// None will: they were canceled, superseded, failed for good or removed.
    Ended,
}

fn outcome(ctx: &Context<'_>, downloads: &[i64]) -> Result<Outcome> {
    let mut pending = false;
    for id in downloads {
        let Some(dl) = Download::find(ctx.conn, "WHERE id = ?", [id])? else {
            continue;
        };
        match dl.state {
            DownloadState::Done => return Ok(Outcome::Finished),
            DownloadState::Canceled | DownloadState::Superseded => {}
            _ if is_final_failure(&dl) => {}
            _ => pending = true,
        }
    }
    Ok(if pending {
        Outcome::Pending
    } else {
        Outcome::Ended
    })
}

/// Whether `file` is `parent` or inside it.
fn under(file: &str, parent: &str) -> bool {
    let (file, parent) = (path_key(file), path_key(parent));
    let parent = parent.trim_end_matches('/');
    !parent.is_empty() && (file == parent || file.starts_with(&format!("{parent}/")))
}

/// The paths among `files` that a torrent in qBittorrent holds, by their keys.
fn held_by_torrents(ctx: &mut Context<'_>, files: &[String]) -> Result<HashSet<String>> {
    let qbit = ctx.qbit()?;
    let torrents: Vec<Torrent> = qbit.torrents().values().cloned().collect();
    let mut held = HashSet::new();
    for torrent in &torrents {
        let place = if torrent.content_path.is_empty() {
            &torrent.save_path
        } else {
            &torrent.content_path
        };
        if !files.iter().any(|file| under(file, place)) {
            continue;
        }
        let root = torrent.save_path.trim_end_matches(['/', '\\']);
        let named: HashSet<String> = qbit
            .files(&torrent.hash)?
            .iter()
            .map(|name| path_key(&format!("{root}/{name}")))
            .collect();
        held.extend(
            files
                .iter()
                .map(|file| path_key(file))
                .filter(|file| named.contains(file)),
        );
    }
    Ok(held)
}

/// Deletes what a finished batch replaces, and gives up on a batch that ended without finishing.
/// Files a torrent in qBittorrent holds stay. While qBittorrent cannot be asked, nothing is
/// deleted and the wait goes on.
pub fn settle(ctx: &mut Context<'_>) -> Result<()> {
    for (key, value) in waits(ctx)? {
        let wait: Option<Wait> = serde_json::from_str(&value).ok();
        let done = match &wait {
            None => true,
            Some(wait) => match outcome(ctx, &wait.downloads)? {
                Outcome::Finished => match held_by_torrents(ctx, &wait.files) {
                    Ok(held) => {
                        delete(wait, &held);
                        true
                    }
                    Err(error) => {
                        debug!("The older files of '{}' wait: {error}", wait.show);
                        false
                    }
                },
                Outcome::Ended => true,
                Outcome::Pending => false,
            },
        };
        if done {
            ctx.conn
                .execute("DELETE FROM app_state WHERE key = ?", [&key])?;
        }
    }
    Ok(())
}

fn files_text(count: usize) -> &'static str {
    if count == 1 { "file" } else { "files" }
}

fn delete(wait: &Wait, held: &HashSet<String>) {
    let own: Vec<String> = wait.batch_files.iter().map(|path| path_key(path)).collect();
    let (mut deleted, mut kept) = (Vec::new(), Vec::new());
    for path in &wait.files {
        let key = path_key(path);
        if own.contains(&key) {
            continue;
        }
        if held.contains(&key) {
            kept.push(path.as_str());
            continue;
        }
        match fs::remove_file(path) {
            Ok(()) => deleted.push(path.as_str()),
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => warn!("Could not delete the older file {path}: {error}"),
        }
    }
    if !deleted.is_empty() {
        info!(
            "'{}': deleted {} older {} the finished batch replaces: {}.",
            wait.show,
            deleted.len(),
            files_text(deleted.len()),
            deleted.join(", ")
        );
    }
    if !kept.is_empty() {
        info!(
            "'{}': kept {} older {} because a torrent in qBittorrent uses them: {}.",
            wait.show,
            kept.len(),
            files_text(kept.len()),
            kept.join(", ")
        );
    }
}

#[cfg(test)]
mod tests {
    use jiff::Timestamp;
    use kisetsu_clients::qbittorrent::{Qbit, QbitError};
    use kisetsu_db::store;

    use super::*;
    use crate::context::{Caches, get_state};
    use crate::fakes::{FakeClock, FakeQbit, Seed};

    fn download(id: i64, state: DownloadState, hash: Option<&str>) -> Download {
        Download {
            id: Some(id),
            episode_id: id,
            feed_id: None,
            feed_item_guid: None,
            title: "[Grp] Show (01-12) [Batch]".to_owned(),
            parsed_title: None,
            release_group: None,
            version: 1,
            source_episode: None,
            torrent_url: None,
            info_hash: hash.map(str::to_owned),
            tag: None,
            state,
            supersedes_id: None,
            adopted: false,
            batch: true,
            attempts: 0,
            next_retry_at: None,
            last_error: None,
            end_reason: None,
            created_at: Timestamp::UNIX_EPOCH,
            updated_at: Timestamp::UNIX_EPOCH,
            completed_at: None,
        }
    }

    /// A folder with an older file and one the batch holds too, and a wait for download 1 that
    /// lists both for deletion.
    fn setup(
        conn: &kisetsu_db::rusqlite::Connection,
        state: DownloadState,
    ) -> (tempfile::TempDir, String, String) {
        conn.execute_batch("PRAGMA foreign_keys = OFF").unwrap();
        let dir = tempfile::tempdir().unwrap();
        let old = dir.path().join("Show - 01.mkv");
        let shared = dir.path().join("Show - 02.mkv");
        fs::write(&old, "v1").unwrap();
        fs::write(&shared, "same").unwrap();
        let (old, shared) = (
            old.to_string_lossy().into_owned(),
            shared.to_string_lossy().into_owned(),
        );
        download(1, state, Some("abc")).insert(conn).unwrap();
        let mut caches = Caches::default();
        let ctx = Context::new(conn, Timestamp::UNIX_EPOCH, &mut caches).unwrap();
        wait_for_batch(
            &ctx,
            "Show",
            &[1],
            &[old.clone(), shared.to_uppercase()],
            std::slice::from_ref(&shared),
        )
        .unwrap();
        (dir, old, shared)
    }

    fn run_with(conn: &kisetsu_db::rusqlite::Connection, qbit: &mut FakeQbit) -> String {
        let mut caches = Caches::default();
        let mut ctx = Context::new(conn, Timestamp::UNIX_EPOCH, &mut caches)
            .unwrap()
            .with_qbit(qbit);
        settle(&mut ctx).unwrap();
        get_state(conn, &format!("{KEY_PREFIX}1"), "gone").unwrap()
    }

    fn run(conn: &kisetsu_db::rusqlite::Connection) -> String {
        run_with(conn, &mut FakeQbit::new(FakeClock::default()))
    }

    #[test]
    fn the_older_files_are_deleted_once_the_batch_is_done_but_not_the_batchs_own() {
        let conn = store::open_in_memory().unwrap();
        let (_dir, old, shared) = setup(&conn, DownloadState::Downloading);
        assert_ne!(run(&conn), "gone");
        assert!(fs::exists(&old).unwrap());

        let dl = download(1, DownloadState::Done, Some("abc"));
        dl.update(&conn).unwrap();
        assert_eq!(run(&conn), "gone");
        assert!(!fs::exists(&old).unwrap());
        assert!(fs::exists(&shared).unwrap());
    }

    #[test]
    fn the_older_files_stay_when_the_batch_ends_without_finishing() {
        for state in [DownloadState::Canceled, DownloadState::Superseded] {
            let conn = store::open_in_memory().unwrap();
            let (_dir, old, _) = setup(&conn, state);
            assert_eq!(run(&conn), "gone");
            assert!(fs::exists(&old).unwrap());
        }
        let conn = store::open_in_memory().unwrap();
        let (_dir, old, _) = setup(&conn, DownloadState::Failed);
        // Failed with its hash kept is an errored torrent that can recover.
        assert_ne!(run(&conn), "gone");
        let dl = download(1, DownloadState::Failed, None);
        dl.update(&conn).unwrap();
        assert_eq!(run(&conn), "gone");
        assert!(fs::exists(&old).unwrap());
    }

    #[test]
    fn the_older_files_stay_when_the_batch_download_was_removed() {
        let conn = store::open_in_memory().unwrap();
        let (_dir, old, _) = setup(&conn, DownloadState::Downloading);
        conn.execute("DELETE FROM downloads", []).unwrap();
        assert_eq!(run(&conn), "gone");
        assert!(fs::exists(&old).unwrap());
    }

    #[test]
    fn a_file_a_torrent_in_qbittorrent_holds_is_kept_and_the_others_are_deleted() {
        let conn = store::open_in_memory().unwrap();
        let (dir, old, shared) = setup(&conn, DownloadState::Done);
        let other = dir.path().join("Show - 03.mkv");
        fs::write(&other, "v1").unwrap();
        let other = other.to_string_lossy().into_owned();
        let mut caches = Caches::default();
        let ctx = Context::new(&conn, Timestamp::UNIX_EPOCH, &mut caches).unwrap();
        wait_for_batch(&ctx, "Show", &[1], &[old.clone(), other.clone()], &[shared]).unwrap();
        let mut qbit = FakeQbit::new(FakeClock::default());
        qbit.seed(Seed::new("Show - 03.mkv", dir.path().to_string_lossy()));
        qbit.sync(true).unwrap();

        assert_eq!(run_with(&conn, &mut qbit), "gone");

        assert!(!fs::exists(&old).unwrap());
        assert!(fs::exists(&other).unwrap());
    }

    #[test]
    fn the_older_files_wait_while_qbittorrent_cannot_be_asked() {
        let conn = store::open_in_memory().unwrap();
        let (dir, old, _) = setup(&conn, DownloadState::Done);
        let mut qbit = FakeQbit::new(FakeClock::default());
        qbit.seed(Seed::new("Show - 01.mkv", dir.path().to_string_lossy()));
        qbit.sync(true).unwrap();
        qbit.unavailable = Some(QbitError::Unavailable("down".to_owned()));

        assert_ne!(run_with(&conn, &mut qbit), "gone");
        assert!(fs::exists(&old).unwrap());

        // Asked again, qBittorrent still holds the file, so it stays; with the torrent gone it goes.
        qbit.unavailable = None;
        assert_eq!(run_with(&conn, &mut qbit), "gone");
        assert!(fs::exists(&old).unwrap());
    }

    #[test]
    fn the_older_files_are_deleted_once_the_holding_torrent_is_gone() {
        let conn = store::open_in_memory().unwrap();
        let (dir, old, _) = setup(&conn, DownloadState::Done);
        let mut qbit = FakeQbit::new(FakeClock::default());
        qbit.seed(Seed::new("Show - 01.mkv", dir.path().to_string_lossy()));
        qbit.sync(true).unwrap();
        let hash = qbit.truth.keys().next().cloned().unwrap();
        qbit.delete(&[hash], false).unwrap();
        qbit.sync(false).unwrap();
        assert_eq!(run_with(&conn, &mut qbit), "gone");
        assert!(!fs::exists(&old).unwrap());
    }

    #[test]
    fn the_older_files_wait_when_there_is_no_qbittorrent_to_ask() {
        let conn = store::open_in_memory().unwrap();
        let (_dir, old, _) = setup(&conn, DownloadState::Done);
        let mut caches = Caches::default();
        let mut ctx = Context::new(&conn, Timestamp::UNIX_EPOCH, &mut caches).unwrap();
        settle(&mut ctx).unwrap();
        assert!(fs::exists(&old).unwrap());
    }
}
