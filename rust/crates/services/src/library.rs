//! The release files already on disk in a show's save folder, the torrents a show owns in
//! qBittorrent, and moving them when a folder changes.
//!
//! A library carried over from another machine names the feed the show was downloaded from, so
//! the feed is read from it before anything is guessed.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::qbittorrent::Torrent;
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{ACTIVE_DOWNLOAD_STATES, DownloadState, Settings, Show};
use kisetsu_db::storage::Column;
use kisetsu_domain::matcher::{build_matcher, decide};
use kisetsu_domain::parsing::parse_release_title;
use kisetsu_domain::patterns::resolve_save_path;
use kisetsu_domain::schedule::PUBLISHED_BEFORE_AIR_SLACK;
use tracing::debug;

use crate::context::Context;
use crate::error::Result;
use crate::{paths, tags};

const VIDEO_EXTENSIONS: [&str; 3] = [".mkv", ".mp4", ".avi"];

/// Whether a file name is a video file.
pub fn is_video_file(name: &str) -> bool {
    let lower = name.to_lowercase();
    VIDEO_EXTENSIONS.iter().any(|ext| lower.ends_with(ext))
}

/// How long a grab waits for the save folder to be matched to a feed, so a search that never
/// completes cannot keep a show from downloading.
const HOLD: SignedDuration = SignedDuration::from_hours(1);

/// The absolute folder the show saves into, with its name in `language`.
pub fn save_path(settings: &Settings, show: &Show, language: &str) -> String {
    let home = env::home_dir().unwrap_or_default();
    let home = home.to_string_lossy();
    resolve_save_path(
        &settings.base_dir,
        &show.title(language),
        Some(&show.save_folder),
        |raw| paths::resolve(raw, &home),
    )
}

/// Names of the show's video files in its save folder, top level and one folder down, that are
/// releases of the show.
pub fn saved_release_names(show: &Show, settings: &Settings) -> Vec<String> {
    let path = save_path(settings, show, &settings.title_language);
    if path.is_empty() || !fs::metadata(&path).is_ok_and(|meta| meta.is_dir()) {
        return Vec::new();
    }
    let mut names = match file_names(&path) {
        Ok(names) => names,
        Err(error) => {
            debug!(
                "Could not read the save folder of '{}': {error}",
                show.title(&settings.title_language)
            );
            return Vec::new();
        }
    };
    names.sort();
    let matcher = build_matcher(
        &show.effective_aliases(),
        show.learned_name.as_deref(),
        settings.min_resolution,
    );
    names
        .into_iter()
        .filter(|name| is_video_file(name) && decide(&matcher, &parse_release_title(name), false).0)
        .collect()
}

/// The files directly in the folder and in the folders directly under it.
pub(crate) fn file_names(folder: &str) -> std::io::Result<Vec<String>> {
    let mut names = Vec::new();
    for entry in fs::read_dir(folder)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if path.is_file() {
            names.push(name);
        } else if path.is_dir() {
            for inner in fs::read_dir(&path)? {
                let inner = inner?;
                if inner.path().is_file() {
                    names.push(inner.file_name().to_string_lossy().into_owned());
                }
            }
        }
    }
    Ok(names)
}

/// Starts holding a show's grabs; true when it was not held already.
pub fn hold(ctx: &mut Context<'_>, show_id: i64) -> bool {
    let new = !ctx.caches.held.contains_key(&show_id);
    ctx.caches.held.entry(show_id).or_insert(ctx.now);
    new
}

pub fn release(ctx: &mut Context<'_>, show_id: i64) {
    ctx.caches.held.remove(&show_id);
}

pub fn held_ids(ctx: &Context<'_>) -> HashSet<i64> {
    ctx.caches
        .held
        .iter()
        .filter(|(_, since)| ctx.now.duration_since(**since) < HOLD)
        .map(|(show_id, _)| *show_id)
        .collect()
}

/// Whether two folder paths are one, however each writes its separators (qBittorrent reports
/// "/" on Windows).
pub fn same_dir(a: &str, b: &str) -> bool {
    let uniform = |path: &str| path.replace('\\', "/").trim_end_matches('/').to_owned();
    uniform(a) == uniform(b)
}

/// Every torrent hash the downloads table holds for a show, including in-flight replacements.
pub fn show_torrent_hashes(ctx: &Context<'_>, show: &Show) -> Result<Vec<String>> {
    let states = [
        ACTIVE_DOWNLOAD_STATES.as_slice(),
        &[DownloadState::Done, DownloadState::Failed],
    ]
    .concat()
    .iter()
    .map(|state| format!("'{}'", state.name()))
    .collect::<Vec<_>>()
    .join(", ");
    let sql = format!(
        "SELECT DISTINCT downloads.info_hash FROM downloads \
         JOIN episodes ON episodes.id = downloads.episode_id \
         WHERE episodes.show_id = ? AND downloads.info_hash IS NOT NULL \
         AND downloads.state IN ({states}) ORDER BY downloads.info_hash"
    );
    let hashes = ctx
        .conn
        .prepare_cached(&sql)?
        .query_map([show.id], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(hashes)
}

/// A torrent deleting a show removes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreviewEntry {
    pub hash: String,
    pub name: Option<String>,
    pub size: Option<i64>,
    /// Whether Kisetsu knew of the torrent.
    pub tracked: bool,
}

impl PreviewEntry {
    fn of(torrent: &Torrent, tracked: bool) -> Self {
        Self {
            hash: torrent.hash.clone(),
            name: Some(torrent.name.clone()),
            size: Some(torrent.size),
            tracked,
        }
    }
}

/// The torrents deleting a show removes.
///
/// Torrents Kisetsu lost track of are found in its category under the show's own save folder,
/// unless another show saves to the same folder. A series folder can also hold the torrents of
/// its earlier seasons, so one that qBittorrent added before this show's first episode aired,
/// or whose name is not this show's, is left alone.
pub fn show_torrent_preview(ctx: &mut Context<'_>, show: &Show) -> Result<Vec<PreviewEntry>> {
    let view = synced_view(ctx)?;
    let mut entries: Vec<PreviewEntry> = Vec::new();
    let mut listed: HashSet<String> = HashSet::new();
    for hash in show_torrent_hashes(ctx, show)? {
        let entry = match view.get(&hash) {
            Some(torrent) => PreviewEntry::of(torrent, true),
            None => PreviewEntry {
                hash: hash.clone(),
                name: None,
                size: None,
                tracked: true,
            },
        };
        listed.insert(hash);
        entries.push(entry);
    }
    let owned = tags::owners(ctx)?;
    for torrent in view.values() {
        // Its series tag is proof the torrent is this show's, even when the database lost track of it.
        if !listed.contains(&torrent.hash) && tags::owner_id(torrent, &owned) == show.id {
            listed.insert(torrent.hash.clone());
            entries.push(PreviewEntry::of(torrent, true));
        }
    }
    let settings = &ctx.settings;
    let language = &settings.title_language;
    let path = save_path(settings, show, language);
    if !path.is_empty() && !settings.default_category.is_empty() {
        let others = Show::select(ctx.conn, "WHERE id != ? ORDER BY id", [show.id])?;
        let shared = others
            .iter()
            .any(|other| save_path(settings, other, language) == path);
        if !shared {
            let first_air = first_air(ctx, show)?;
            let slack =
                PUBLISHED_BEFORE_AIR_SLACK.max(SignedDuration::from_hours(ctx.tolerance_hours()));
            let matcher = build_matcher(
                &show.effective_aliases(),
                show.learned_name.as_deref(),
                settings.min_resolution,
            );
            for torrent in view.values() {
                if listed.contains(&torrent.hash) || torrent.category != settings.default_category {
                    continue;
                }
                if tags::owner_id(torrent, &owned).is_some_and(|owner| Some(owner) != show.id) {
                    continue;
                }
                if !same_dir(&torrent.save_path, &path) {
                    continue;
                }
                let before_first_air = first_air.is_some_and(|first| {
                    torrent
                        .added_on
                        .is_some_and(|added| added < first.saturating_sub(slack).unwrap_or(first))
                });
                if before_first_air
                    || !decide(&matcher, &parse_release_title(&torrent.name), false).0
                {
                    continue;
                }
                listed.insert(torrent.hash.clone());
                entries.push(PreviewEntry::of(torrent, false));
            }
        }
    }
    entries.sort_by(|a, b| {
        let key = |entry: &PreviewEntry| {
            (
                !entry.tracked,
                entry.name.clone().unwrap_or_else(|| entry.hash.clone()),
            )
        };
        key(a).cmp(&key(b))
    });
    Ok(entries)
}

/// qBittorrent's torrents after a fresh sync.
fn synced_view(ctx: &mut Context<'_>) -> Result<BTreeMap<String, Torrent>> {
    let qbit = ctx.qbit()?;
    qbit.sync(false)?;
    Ok(qbit.torrents().clone())
}

fn first_air(ctx: &Context<'_>, show: &Show) -> Result<Option<Timestamp>> {
    let mut statement = ctx.conn.prepare_cached(
        "SELECT min(air_at) FROM episodes WHERE show_id = ? AND air_at IS NOT NULL",
    )?;
    let first = statement.query_row([show.id], |row| Option::<Timestamp>::read(row, 0))?;
    Ok(first)
}

/// The hashes of every torrent a deleted show leaves in qBittorrent.
pub fn find_show_torrents(ctx: &mut Context<'_>, show: &Show) -> Result<Vec<String>> {
    let mut hashes: Vec<String> = show_torrent_preview(ctx, show)?
        .into_iter()
        .map(|entry| entry.hash)
        .collect();
    hashes.sort();
    Ok(hashes)
}

/// The torrents of one show that a folder change moves.
#[derive(Debug, Clone, PartialEq)]
pub struct MovePlan {
    pub show: Show,
    pub old: String,
    pub new: String,
    pub hashes: Vec<String>,
    /// How many torrents qBittorrent is still moving.
    pub moving: usize,
}

/// The folder renames switching to `new_language` causes, per show with torrents to move.
///
/// `ctx.settings.title_language` must still be the current language.
pub fn plan_language_move(ctx: &mut Context<'_>, new_language: &str) -> Result<Vec<MovePlan>> {
    let shows = Show::select(ctx.conn, "ORDER BY id", [])?;
    let settings = ctx.settings.clone();
    plan_move(
        ctx,
        &shows,
        |show| save_path(&settings, show, new_language),
        false,
    )
}

/// The folder moves `new_path` (a show's future save path) causes, per show with torrents to move.
///
/// The settings and shows must still be as they are now. Torrents are judged by where
/// qBittorrent has them now, so one that already moved is left alone. By default only torrents
/// in the show's current folder move, so one the user relocated by hand is never touched; with
/// `anywhere` every download Kisetsu tracks moves, wherever it is now.
pub fn plan_move(
    ctx: &mut Context<'_>,
    shows: &[Show],
    mut new_path: impl FnMut(&Show) -> String,
    anywhere: bool,
) -> Result<Vec<MovePlan>> {
    let mut plan = Vec::new();
    for show in shows {
        let old = save_path(&ctx.settings, show, &ctx.settings.title_language);
        let new = new_path(show);
        if new.is_empty() || (!anywhere && (old.is_empty() || same_dir(&old, &new))) {
            continue;
        }
        let entries = show_torrent_preview(ctx, show)?;
        let view = ctx.qbit()?.torrents();
        let found: Vec<(bool, &Torrent)> = entries
            .iter()
            .filter_map(|entry| {
                view.get(&entry.hash)
                    .map(|torrent| (entry.tracked, torrent))
            })
            .collect();
        let here: Vec<&Torrent> = if anywhere {
            found
                .iter()
                .filter(|(tracked, torrent)| {
                    (*tracked || same_dir(&torrent.save_path, &old))
                        && !same_dir(&torrent.save_path, &new)
                })
                .map(|(_, torrent)| *torrent)
                .collect()
        } else {
            found
                .iter()
                .filter(|(_, torrent)| same_dir(&torrent.save_path, &old))
                .map(|(_, torrent)| *torrent)
                .collect()
        };
        let moving = found
            .iter()
            .filter(|(_, torrent)| torrent.state.eq_ignore_ascii_case("moving"))
            .count();
        if !here.is_empty() || moving > 0 {
            plan.push(MovePlan {
                show: show.clone(),
                old,
                new,
                hashes: here.iter().map(|torrent| torrent.hash.clone()).collect(),
                moving,
            });
        }
    }
    Ok(plan)
}

/// What `relocate_torrents` did: a log line per show, and the names of the shows that failed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Relocation {
    pub logs: Vec<String>,
    pub failed: Vec<String>,
}

/// Moves what `plan_language_move` found.
pub fn relocate_torrents(ctx: &mut Context<'_>, plan: &[MovePlan]) -> Result<Relocation> {
    let mut done = Relocation::default();
    for item in plan.iter().filter(|item| !item.hashes.is_empty()) {
        let name = item.show.title(&ctx.settings.title_language);
        match ctx.qbit()?.set_location(&item.hashes, &item.new) {
            Err(error) => {
                done.failed.push(name.clone());
                done.logs
                    .push(format!("Could not move the downloads of {name}: {error}"));
            }
            Ok(()) => done.logs.push(format!(
                "Moved {} downloads of {name} to {}",
                item.hashes.len(),
                item.new
            )),
        }
    }
    Ok(done)
}

// -- deleting a show's folder ---------------------------------------------------------------------

/// Leftover file names listed in the delete dialog; the rest are only counted.
const LEFTOVERS_LISTED: usize = 20;

/// Why a show's folder is kept when the show is deleted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum KeepReason {
    #[error("No download folder is set in Settings, so Kisetsu never deletes folders.")]
    NoDownloadFolder,
    #[error("The folder is not inside your download folder.")]
    OutsideDownloadFolder,
    #[error("Another show saves to this folder.")]
    SharedWithAnotherShow,
    #[error("It holds torrents that belong to no show in Kisetsu.")]
    HoldsOtherTorrents,
}

/// What deleting a show does to its save folder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FolderPlan {
    pub path: String,
    pub exists: bool,
    /// Set only for a folder strictly inside the download folder that no other show saves to and
    /// that holds no torrent but the show's own.
    pub remove: bool,
    pub kept: Option<KeepReason>,
    /// The first files no torrent of the show owns.
    pub leftovers: Vec<String>,
    /// How many files no torrent of the show owns.
    pub leftover_count: usize,
}

fn within(child: &str, parent: &str) -> bool {
    paths::within(&paths::expand_home(child), &paths::expand_home(parent))
}

fn normalized(path: &str) -> String {
    paths::normalized(&paths::expand_home(path))
}

/// The folder every show's own folder sits in, or "" when no download folder is set.
pub fn base_root(base_dir: &str) -> String {
    let base = base_dir.trim();
    let base = base.split_once("{name}").map_or(base, |(root, _)| root);
    let base = base.trim_end_matches(['/', '\\']);
    if base.is_empty() {
        String::new()
    } else {
        normalized(base)
    }
}

/// Whether `path` is a folder this app may delete: strictly inside the download folder `root`.
fn inside_root(path: &str, root: &str) -> bool {
    !root.is_empty() && within(path, root) && normalized(path) != root
}

/// Every file under `folder`, however deep.
fn files_below(folder: &Path, found: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(folder) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        match entry.file_type() {
            Ok(kind) if kind.is_dir() => files_below(&path, found),
            Ok(kind) if kind.is_symlink() && path.is_dir() => {}
            _ => found.push(path),
        }
    }
}

/// What deleting a show does to its save folder, judged before its torrents go.
///
/// `hashes` are the show's own torrents.
pub fn folder_plan(ctx: &mut Context<'_>, show: &Show, hashes: &[String]) -> Result<FolderPlan> {
    let language = ctx.settings.title_language.clone();
    let path = save_path(&ctx.settings, show, &language);
    let mut plan = FolderPlan {
        path: path.clone(),
        exists: false,
        remove: false,
        kept: None,
        leftovers: Vec::new(),
        leftover_count: 0,
    };
    if path.is_empty() || !fs::metadata(&path).is_ok_and(|meta| meta.is_dir()) {
        return Ok(plan);
    }
    plan.exists = true;
    let view = ctx.qbit()?.torrents().clone();
    let own: Vec<&Torrent> = hashes.iter().filter_map(|hash| view.get(hash)).collect();
    let mut files = Vec::new();
    files_below(Path::new(&path), &mut files);
    let mut names: Vec<String> = files
        .iter()
        .filter(|file| {
            let full = file.to_string_lossy();
            !own.iter().any(|torrent| {
                !torrent.content_path.is_empty() && within(&full, &torrent.content_path)
            })
        })
        .map(|file| {
            let relative = file.strip_prefix(&path).unwrap_or(file);
            relative.to_string_lossy().into_owned()
        })
        .collect();
    names.sort();
    plan.leftover_count = names.len();
    names.truncate(LEFTOVERS_LISTED);
    plan.leftovers = names;
    let root = base_root(&ctx.settings.base_dir);
    plan.kept = if root.is_empty() {
        Some(KeepReason::NoDownloadFolder)
    } else if !inside_root(&path, &root) {
        Some(KeepReason::OutsideDownloadFolder)
    } else if Show::select(ctx.conn, "WHERE id != ? ORDER BY id", [show.id])?
        .iter()
        .any(|other| {
            let other_path = save_path(&ctx.settings, other, &language);
            !other_path.is_empty() && (within(&other_path, &path) || within(&path, &other_path))
        })
    {
        Some(KeepReason::SharedWithAnotherShow)
    } else if view.values().any(|torrent| {
        !hashes.contains(&torrent.hash)
            && ((!torrent.content_path.is_empty() && within(&torrent.content_path, &path))
                || (!torrent.save_path.is_empty() && within(&torrent.save_path, &path)))
    }) {
        Some(KeepReason::HoldsOtherTorrents)
    } else {
        None
    };
    plan.remove = plan.kept.is_none();
    Ok(plan)
}

/// Whether qBittorrent no longer lists any of `hashes`.
pub fn torrents_gone(ctx: &mut Context<'_>, hashes: &[String]) -> Result<bool> {
    let qbit = ctx.qbit()?;
    qbit.sync(false)?;
    Ok(!hashes.iter().any(|hash| qbit.torrents().contains_key(hash)))
}

/// Why a show's folder was not deleted.
#[derive(Debug, thiserror::Error)]
pub enum RemoveFolderError {
    #[error("The folder is not inside your download folder.")]
    OutsideDownloadFolder,
    #[error(transparent)]
    Io(#[from] std::io::Error),
}

/// Deletes a show's folder and everything left in it.
///
/// The folder is checked again against the download folder right before it goes. A file the
/// system still holds open is retried `attempts` times, with `pause` between the tries.
pub fn remove_folder(
    path: &str,
    base_dir: &str,
    attempts: u32,
    mut pause: impl FnMut(Duration),
) -> Result<(), RemoveFolderError> {
    if !inside_root(path, &base_root(base_dir)) {
        return Err(RemoveFolderError::OutsideDownloadFolder);
    }
    let target = Path::new(path);
    let mut last = None;
    for attempt in 0..attempts {
        if !target.exists() {
            return Ok(());
        }
        match fs::remove_dir_all(target) {
            Ok(()) => return Ok(()),
            Err(error) => last = Some(error),
        }
        if attempt + 1 < attempts {
            pause(Duration::from_secs(1));
        }
    }
    last.map_or(Ok(()), |error| Err(error.into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_folder_outside_the_download_folder_or_the_download_folder_itself_is_never_removed() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("Anime");
        let other = dir.path().join("Other");
        fs::create_dir_all(root.join("Show")).unwrap();
        fs::create_dir_all(&other).unwrap();
        let base = format!("{}/{{name}}", root.display());
        let escaping = root.join("Show").join("..").join("..").join("Other");
        for path in [&other, &root, &escaping] {
            let refused = remove_folder(&path.to_string_lossy(), &base, 1, |_| {});
            assert!(matches!(
                refused,
                Err(RemoveFolderError::OutsideDownloadFolder)
            ));
        }
        assert!(other.is_dir() && root.join("Show").is_dir());
        assert!(matches!(
            remove_folder(&root.to_string_lossy(), "", 1, |_| {}),
            Err(RemoveFolderError::OutsideDownloadFolder)
        ));
    }

    #[test]
    fn a_folder_with_a_read_only_file_is_removed() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("Anime");
        let show = root.join("Show");
        fs::create_dir_all(&show).unwrap();
        let file = show.join("01.mkv");
        fs::write(&file, "x").unwrap();
        let mut permissions = fs::metadata(&file).unwrap().permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&file, permissions).unwrap();
        let removed = remove_folder(&show.to_string_lossy(), &root.to_string_lossy(), 1, |_| {});
        assert!(removed.is_ok());
        assert!(!show.exists());
        assert!(root.is_dir());
    }
}
