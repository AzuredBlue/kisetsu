//! The release files already on disk in a show's save folder, the torrents a show owns in
//! qBittorrent, and moving them when a folder changes.
//!
//! A library carried over from another machine names the feed the show was downloaded from, so
//! the feed is read from it before anything is guessed.

use std::collections::{BTreeMap, HashSet};
use std::env;
use std::fs;

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
        .filter(|name| {
            let lower = name.to_lowercase();
            VIDEO_EXTENSIONS.iter().any(|ext| lower.ends_with(ext))
                && decide(&matcher, &parse_release_title(name), false).0
        })
        .collect()
}

/// The files directly in the folder and in the folders directly under it.
fn file_names(folder: &str) -> std::io::Result<Vec<String>> {
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
