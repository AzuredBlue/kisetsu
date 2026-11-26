//! Adding a show by hand from the user's AniList list.
//!
//! The user picks an entry of their list, the feeds' sites are searched for it, and they choose
//! the feed and the episodes to download. Nothing is saved for the show until they confirm, so
//! the grabber can never pick a feed or start a download for it on its own meanwhile.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::MAIN_SEPARATOR;

use jiff::civil;
use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::anilist::ListEntry;
use kisetsu_clients::rss::FeedArticle;
use kisetsu_clients::sites;
use kisetsu_db::schema::{Episode, Feed, FeedItem, Show};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::matcher::{Matcher, build_matcher, decide};
use kisetsu_domain::parsing::{
    batch_range, is_batch, is_single_file, names_something_else, parse_release_title,
    release_season, resolution_rank, strip_batch,
};
use kisetsu_domain::patterns::{in_english_letters, sanitize_folder_name};
use tracing::{debug, info};

use crate::context::Context;
use crate::downloads::{self, StartOptions};
use crate::error::Result;
use crate::feeds::{self, Evidence};
use crate::library::{self, is_video_file};
use crate::manual::{self, Candidate};
use crate::older_files;
use crate::{adoption, ingest, shows, watched};

/// The list statuses offered for adding: what the user watches, paused or dropped. Completed ones
/// are left out.
pub const LIST_STATUSES: [&str; 4] = ["CURRENT", "REPEATING", "PAUSED", "DROPPED"];
/// Releases older than the season start by more than this are not the show's: a season's first
/// episodes can air in the weeks before AniList's nominal start (a FALL show in late September).
const SEASON_SLACK: SignedDuration = SignedDuration::from_hours(30 * 24);
const STILL_AIRING: [&str; 3] = ["RELEASING", "NOT_YET_RELEASED", "HIATUS"];

/// Why a show was not added. The messages are shown to the user.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    #[error("Kisetsu already follows this show.")]
    AlreadyFollowed,
    #[error("Selected feed not found")]
    FeedNotFound,
    /// The search that offered the releases is gone (it is kept an hour); adding the show now
    /// would quietly start none of them.
    #[error("Some of the chosen releases are no longer known. Search again.")]
    ReleasesUnknown,
}

/// What searching for a new show needs: its names and the feeds whose sites can be searched.
#[derive(Debug, Clone, PartialEq)]
pub struct Plan {
    pub terms: Vec<String>,
    pub feeds: Vec<Feed>,
    /// The names of the feeds whose sites cannot be searched.
    pub unsearchable: Vec<String>,
    pub min_resolution: i64,
    /// The episodes up to this one were watched and are not searched for (0: none).
    pub watched: i64,
}

/// A list entry that can be added.
#[derive(Debug, Clone, PartialEq)]
pub struct ListRow {
    pub anilist_id: i64,
    pub title: String,
    pub aliases: Vec<String>,
    pub cover: Option<String>,
    pub status: Option<String>,
    pub list_status: Option<String>,
    pub list_label: Option<String>,
    pub total_episodes: Option<i64>,
    pub season: Option<String>,
    /// Whether Kisetsu follows the show already, and its id then.
    pub followed: bool,
    pub show_id: Option<i64>,
}

/// A folder in the download folder named after the show, with its video files.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExistingFolder {
    pub name: String,
    pub path: String,
    pub files: Vec<String>,
}

/// A batch release of the show and the episodes it holds.
#[derive(Debug, Clone, PartialEq)]
pub struct Batch {
    pub item: FeedItem,
    pub numbers: Vec<i64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct BatchOffer {
    pub item_key: String,
    pub title: String,
    pub numbers: Vec<i64>,
    /// Whether the title gives the range of episodes it holds.
    pub ranged: bool,
    pub published_at: Option<Timestamp>,
    /// Where its `.torrent` file is, or its magnet link.
    pub torrent_url: String,
    /// How many of its files are in the folder on disk under the same name. Until the batch's
    /// file list is read (`files_known`) it is how many of its episodes have any file there.
    pub on_disk: usize,
    /// The episodes whose file in the batch is another version of one in the folder.
    pub newer: Vec<i64>,
    /// How many of its files are neither in the folder nor another version of one.
    pub to_download: usize,
    /// Whether `on_disk`, `newer` and `to_download` come from the batch's file list.
    pub files_known: bool,
}

/// The video files of batch torrents, by item key, as paths inside the torrent.
pub type TorrentFiles = BTreeMap<String, Vec<String>>;

#[derive(Debug, Clone, PartialEq)]
pub struct EpisodeOffer {
    pub number: i64,
    pub item_key: String,
    pub title: String,
    pub version: i64,
    pub published_at: Option<Timestamp>,
    pub on_disk: bool,
}

/// What one feed carries for the show.
#[derive(Debug, Clone, PartialEq)]
pub struct FeedOffer {
    pub feed_id: i64,
    pub feed_name: String,
    pub batches: Vec<BatchOffer>,
    pub episodes: Vec<EpisodeOffer>,
}

/// The folder found on disk, as the dialog shows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FolderSummary {
    pub path: String,
    pub files: usize,
    /// The names of those files.
    pub names: Vec<String>,
}

/// What each feed carries for the entry, and the feed its files on disk came from.
#[derive(Debug, Clone, PartialEq)]
pub struct Preview {
    pub anilist_id: i64,
    /// No feed offers a release: any feed may carry one later.
    pub nothing_offered: bool,
    pub title: String,
    pub status: Option<String>,
    pub total_episodes: Option<i64>,
    pub episode_count: usize,
    pub feeds: Vec<FeedOffer>,
    pub unsearchable: Vec<String>,
    pub folder: Option<FolderSummary>,
    /// The feed the files on disk came from, when it is among those offered.
    pub preselect: Option<i64>,
    pub note: Option<String>,
    /// The episodes up to this one were watched, so they are not picked unless the user ticks
    /// them.
    pub watched: Option<i64>,
}

/// What adding the show did.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Confirmed {
    pub show_id: i64,
    pub started: usize,
    pub skipped: usize,
    pub errors: Vec<String>,
}

fn list_label(status: &str) -> &str {
    match status {
        "CURRENT" => "Watching",
        "REPEATING" => "Rewatching",
        "PAUSED" => "Paused",
        "DROPPED" => "Dropped",
        other => other,
    }
}

fn title_case(word: &str) -> String {
    let mut letters = word.chars();
    match letters.next() {
        Some(first) => first
            .to_uppercase()
            .chain(letters.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

/// `Some(text)` unless `text` is empty.
fn present(text: &str) -> Option<String> {
    (!text.is_empty()).then(|| text.to_owned())
}

/// The user's list entries that can be added, each marked when Kisetsu already follows it.
pub fn list_view(ctx: &Context<'_>, entries: &[ListEntry]) -> Result<Vec<ListRow>> {
    let followed: HashMap<i64, i64> = ctx
        .conn
        .prepare_cached("SELECT anilist_id, id FROM shows")?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<std::result::Result<_, _>>()?;
    let english_first = ctx.settings.title_language == "english";
    let mut rows = Vec::new();
    for entry in entries {
        let list_status = entry.list_status.as_ref().map(|status| status.as_str());
        if !list_status.is_some_and(|status| LIST_STATUSES.contains(&status)) {
            continue;
        }
        let (english, romaji) = (entry.title_english.as_str(), entry.title_romaji.as_str());
        let chosen = if english_first { english } else { romaji };
        let title = [chosen, english, romaji, entry.display_name.as_str()]
            .into_iter()
            .find(|title| !title.is_empty())
            .unwrap_or_default();
        let season = [
            entry
                .season
                .as_ref()
                .map(|season| title_case(season.as_str())),
            entry
                .season_year
                .filter(|year| *year != 0)
                .map(|year| year.to_string()),
        ]
        .into_iter()
        .flatten()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
        rows.push(ListRow {
            anilist_id: entry.anilist_id,
            title: title.to_owned(),
            aliases: entry.aliases.clone(),
            cover: present(&entry.cover_image),
            status: present(entry.status.as_str()),
            list_status: list_status.map(str::to_owned),
            list_label: list_status.map(|status| list_label(status).to_owned()),
            total_episodes: entry.total_episodes,
            season: present(&season),
            followed: followed.contains_key(&entry.anilist_id),
            show_id: followed.get(&entry.anilist_id).copied(),
        });
    }
    rows.sort_by_key(|row| row.title.to_lowercase());
    Ok(rows)
}

fn titles(entry: &ListEntry) -> Vec<&str> {
    [entry.title_romaji.as_str(), entry.title_english.as_str()]
        .into_iter()
        .chain(entry.aliases.iter().map(String::as_str))
        .filter(|title| !title.is_empty())
        .collect()
}

/// The matcher for the names of a list entry: its titles and its other names written in English
/// letters, as a followed show is matched.
pub fn matcher_for(entry: &ListEntry, min_resolution: i64) -> Matcher {
    let mut seen = HashSet::new();
    let own = [entry.title_romaji.as_str(), entry.title_english.as_str()];
    let names: Vec<String> = titles(entry)
        .into_iter()
        .filter(|name| own.contains(name) || in_english_letters(name))
        .filter(|title| seen.insert(*title))
        .map(str::to_owned)
        .collect();
    build_matcher(&names, None, min_resolution)
}

pub fn search_plan(ctx: &Context<'_>, entry: &ListEntry) -> Result<Plan> {
    let all_feeds = feeds::ranked(ctx)?;
    let (searchable, unsearchable): (Vec<Feed>, Vec<Feed>) = all_feeds
        .into_iter()
        .partition(|feed| sites::site_of(&feed.url).is_some());
    Ok(Plan {
        terms: sites::search_terms(titles(entry).into_iter().map(Some)),
        feeds: searchable,
        unsearchable: unsearchable.into_iter().map(|feed| feed.name).collect(),
        min_resolution: ctx.settings.min_resolution,
        watched: if ctx.settings.delete_watched {
            entry.progress.unwrap_or(0)
        } else {
            0
        },
    })
}

/// Keeps what the search found as ordinary feed items, so the chosen ones can be grabbed;
/// returns the new rows.
pub fn store_found(ctx: &mut Context<'_>, found: &[(i64, Vec<FeedArticle>)]) -> Result<usize> {
    let mut added = 0;
    for (feed_id, articles) in found {
        if let Some(feed) = feeds::feed_by_id(ctx, Some(*feed_id))? {
            if !articles.is_empty() {
                added += ingest::ingest(ctx, &feed, articles)?.new;
            }
        }
    }
    Ok(added)
}

/// The download folder as the user wrote it in the settings, for showing paths ("" when none is
/// set). The folder is compared and read through the normalized form `library::base_root` gives.
fn configured_root(base_dir: &str) -> String {
    let base = base_dir.trim();
    let base = base.split_once("{name}").map_or(base, |(root, _)| root);
    crate::paths::expand_home(base.trim_end_matches(['/', '\\']))
}

/// `name` inside `root`, joined with the separator `root` is written with.
fn join_shown(root: &str, name: &str) -> String {
    if root.is_empty() {
        return name.to_owned();
    }
    let separator = root
        .chars()
        .rfind(|c| matches!(c, '/' | '\\'))
        .unwrap_or(MAIN_SEPARATOR);
    format!("{root}{separator}{name}")
}

/// A folder in the download folder named after one of the show's titles, with the show's video
/// files in it.
pub fn existing_folder(ctx: &Context<'_>, entry: &ListEntry) -> Option<ExistingFolder> {
    let root = library::base_root(&ctx.settings.base_dir);
    if root.is_empty() || !fs::metadata(&root).is_ok_and(|meta| meta.is_dir()) {
        return None;
    }
    let wanted: HashSet<String> = titles(entry)
        .into_iter()
        .map(|title| sanitize_folder_name(title).to_lowercase())
        .collect();
    let matcher = matcher_for(entry, ctx.settings.min_resolution);
    let mut folders: Vec<String> = Vec::new();
    match fs::read_dir(&root) {
        Ok(entries) => {
            for found in entries.flatten() {
                let name = found.file_name().to_string_lossy().into_owned();
                if found.path().is_dir() && wanted.contains(&name.to_lowercase()) {
                    folders.push(name);
                }
            }
        }
        Err(error) => {
            debug!("Could not read the download folder: {error}");
            return None;
        }
    }
    folders.sort();
    let shown_root = configured_root(&ctx.settings.base_dir);
    for name in folders {
        let path = join_shown(&shown_root, &name);
        let Ok(names) = library::file_names(&path) else {
            continue;
        };
        let mut videos: Vec<String> = names
            .into_iter()
            .filter(|file| {
                is_video_file(file) && decide(&matcher, &parse_release_title(file), false).0
            })
            .collect();
        videos.sort();
        if !videos.is_empty() {
            return Some(ExistingFolder {
                name,
                path,
                files: videos,
            });
        }
    }
    None
}

/// The save folder setting that points the show at a folder found on disk ("" when that is its
/// usual one).
fn save_folder(ctx: &Context<'_>, show: &Show, folder: Option<&ExistingFolder>) -> String {
    let Some(folder) = folder else {
        return String::new();
    };
    let default = sanitize_folder_name(&show.title(&ctx.settings.title_language));
    if default.to_lowercase() == folder.name.to_lowercase() {
        String::new()
    } else {
        folder.name.clone()
    }
}

/// The show as the grabber will see it, saved so that releases can be numbered against it.
fn draft(
    ctx: &mut Context<'_>,
    entry: &ListEntry,
    folder: Option<&ExistingFolder>,
) -> Result<Show> {
    let mut show = shows::show_from_entry(entry, false);
    show.save_folder = save_folder(ctx, &show, folder);
    show.id = Some(show.insert(ctx.conn)?);
    shows::ensure_episodes(ctx, &show)?;
    ingest::ensure_matches_current(ctx)?;
    Ok(show)
}

/// The first day of the show's AniList season, or `None` when it names none.
fn season_start(show: &Show) -> Option<Timestamp> {
    let month = match show.season_name.as_deref()?.to_uppercase().as_str() {
        "WINTER" => 1,
        "SPRING" => 4,
        "SUMMER" => 7,
        "FALL" => 10,
        _ => return None,
    };
    let year = i16::try_from(show.season_year.filter(|year| *year != 0)?).ok()?;
    let start = civil::date(year, month, 1).in_tz("UTC").ok()?;
    Some(start.timestamp())
}

/// Whether the item was published long before the show's season, so it is some older show's.
///
/// The engine's own air-time check cannot tell yet: the show has no air times until it is
/// confirmed.
fn too_old(show: &Show, item: &FeedItem) -> bool {
    match (season_start(show), item.published_at) {
        (Some(start), Some(published)) => start
            .checked_sub(SEASON_SLACK)
            .is_ok_and(|limit| published < limit),
        _ => false,
    }
}

/// The release the feed offers for each episode: the highest version, then the preferred
/// quality, then the newest.
fn releases(ctx: &mut Context<'_>, show: &Show, feed: &Feed) -> Result<BTreeMap<i64, Candidate>> {
    let preferred = ctx.settings.min_resolution;
    let aliases = show.effective_aliases();
    let mut best: BTreeMap<i64, Candidate> = BTreeMap::new();
    let key = |candidate: &Candidate| {
        let item = &candidate.entry.item;
        (
            candidate.entry.version,
            Reverse(resolution_rank(&item.title, preferred)),
            item.published_at.unwrap_or(item.first_seen_at),
        )
    };
    for candidate in manual::candidates(ctx, show, Some(std::slice::from_ref(feed)), None)? {
        // A number in a special's subtitle ("- The Case of Ultra 30") reads as an episode, and
        // only the show's titles tell them apart.
        if too_old(show, &candidate.entry.item)
            || names_something_else(&parse_release_title(&candidate.entry.item.title), &aliases)
        {
            continue;
        }
        let number = candidate.entry.number;
        if best
            .get(&number)
            .is_none_or(|held| key(&candidate) > key(held))
        {
            best.insert(number, candidate);
        }
    }
    Ok(best)
}

/// Whether one release can hold the whole season: it has a known length and has finished airing.
fn whole_season_exists(show: &Show) -> bool {
    show.total_episodes.is_some_and(|total| total != 0)
        && !show
            .anilist_status
            .as_deref()
            .is_some_and(|status| STILL_AIRING.contains(&status))
}

/// The show's episodes a batch holds: its range, read as the season's own numbers when they fit.
///
/// Without a range it holds the whole season, if there can be one yet.
fn covered(span: Option<(i64, i64)>, target: i64, whole: bool) -> Vec<i64> {
    let Some((first, last)) = span else {
        return if whole {
            (1..=target).collect()
        } else {
            Vec::new()
        };
    };
    if last <= target {
        (first..=last).collect()
    } else if last - first + 1 == target {
        // Numbered from the series' start: the batch is this whole season.
        (1..=target).collect()
    } else {
        // Longer than the season: another season's, or several seasons together.
        Vec::new()
    }
}

/// The feed's batches of the show, the ones holding most episodes first.
///
/// A batch names its season more often than single releases do, so it must name the same season
/// as the show's titles; naming none counts as the first. A release with neither a batch marker
/// nor an episode number is one too when the season has several episodes and has ended: a whole
/// season posted under its plain name. It covers every episode, as a batch with no range does.
/// One file's name or CRC rules that out, and a movie, special or subtitled release is never the
/// show's.
fn batches(ctx: &Context<'_>, show: &Show, feed: &Feed) -> Result<Vec<Batch>> {
    let aliases = show.effective_aliases();
    let matcher = build_matcher(&aliases, None, ctx.settings.min_resolution);
    let target = match show.total_episodes.filter(|total| *total != 0) {
        Some(total) => total,
        None => i64::try_from(shows::ensure_episodes(ctx, show)?.len()).unwrap_or(i64::MAX),
    };
    let season = aliases
        .iter()
        .filter_map(|alias| release_season(alias))
        .filter(|season| *season != 0)
        .max()
        .unwrap_or(1);
    let whole = whole_season_exists(show);
    let mut found = Vec::new();
    for item in FeedItem::select(ctx.conn, "WHERE feed_id = ? ORDER BY id", [feed.id])? {
        if item.torrent_url.as_deref().is_none_or(str::is_empty) || too_old(show, &item) {
            continue;
        }
        if !is_batch(&item.title)
            && (!whole
                || target <= 1
                || is_single_file(&item.title)
                || parse_release_title(&item.title).episode.is_some())
        {
            continue;
        }
        let name = strip_batch(&item.title);
        let parsed = parse_release_title(&name);
        if release_season(&name).unwrap_or(1) != season
            || names_something_else(&parsed, &aliases)
            || !decide(&matcher, &parsed, false).0
        {
            continue;
        }
        let numbers = covered(batch_range(&item.title), target, whole);
        if !numbers.is_empty() {
            found.push(Batch { item, numbers });
        }
    }
    found.sort_by_key(|batch| {
        (
            Reverse(batch.numbers.len()),
            Reverse(batch.item.published_at.unwrap_or(batch.item.first_seen_at)),
        )
    });
    Ok(found)
}

fn group_of(name: &str) -> Option<String> {
    parse_release_title(name)
        .release_group
        .map(|group| group.to_lowercase())
}

/// The feed the files on disk came from.
///
/// Files a batch put there name no release of the feed; their release group then points at the
/// one feed with a batch from that group.
fn feed_from_files(ctx: &Context<'_>, show: &Show, files: &[String]) -> Result<Option<Feed>> {
    if let Some(found) = adoption::feed_from_saved_files(ctx, show, files, &[])? {
        return Ok(Some(found.feed));
    }
    let groups: HashSet<String> = files.iter().filter_map(|name| group_of(name)).collect();
    if groups.is_empty() {
        return Ok(None);
    }
    let mut carriers = Vec::new();
    for feed in feeds::ranked(ctx)? {
        let carries = batches(ctx, show, &feed)?
            .iter()
            .any(|batch| group_of(&batch.item.title).is_some_and(|group| groups.contains(&group)));
        if carries {
            carriers.push(feed);
        }
    }
    Ok(match carriers.len() {
        1 => carriers.pop(),
        _ => None,
    })
}

/// A release's name for the add flow: its feed and guid. A searched release has no row until the
/// show is added.
pub fn item_key(item: &FeedItem) -> String {
    format!("{}:{}", item.feed_id, item.guid)
}

/// A release name compared without its extension or case, as file names and feed titles differ
/// there.
pub fn library_key(name: &str) -> String {
    let stem = match name.rfind('.') {
        Some(at) if name[..at].chars().any(|c| c != '.') && is_video_file(&name[at..]) => {
            &name[..at]
        }
        _ => name,
    };
    stem.to_lowercase().trim().to_owned()
}

/// What a batch's files are to the folder the show already has.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct FileCounts {
    /// Files the folder holds under the same name.
    pub on_disk: usize,
    /// The episodes whose batch file is another version of a file in the folder.
    pub newer: Vec<i64>,
    /// Files the folder has nothing like.
    pub to_download: usize,
    /// The folder's files the newer ones replace, with their episode numbers.
    pub older: Vec<(i64, String)>,
}

/// The name of a file in a torrent: its last path component.
fn file_name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or(path)
}

/// Sorts the batch's video files by what the folder's `folder_files` already hold.
pub fn classify(batch_files: &[String], folder_files: &[String]) -> FileCounts {
    let names: Vec<&str> = batch_files
        .iter()
        .map(|path| file_name(path))
        .filter(|name| is_video_file(name))
        .collect();
    let batch_keys: HashSet<String> = names.iter().map(|name| library_key(name)).collect();
    let disk_keys: HashSet<String> = folder_files.iter().map(|name| library_key(name)).collect();
    let disk_numbers: BTreeSet<i64> = folder_files
        .iter()
        .filter_map(|name| parse_release_title(name).episode)
        .collect();
    let mut counts = FileCounts::default();
    let mut replaced = BTreeSet::new();
    for name in names {
        let held = parse_release_title(name)
            .episode
            .filter(|number| disk_numbers.contains(number));
        if disk_keys.contains(&library_key(name)) {
            counts.on_disk += 1;
        } else if let Some(number) = held {
            counts.newer.push(number);
            replaced.insert(number);
        } else {
            counts.to_download += 1;
        }
    }
    counts.older = folder_files
        .iter()
        .filter_map(|file| {
            let number = parse_release_title(file).episode?;
            (replaced.contains(&number) && !batch_keys.contains(&library_key(file)))
                .then(|| (number, file.clone()))
        })
        .collect();
    counts
}

/// Whether the batch holds another file for episode `number` than the one named `held`.
fn replaces(batch_files: &[String], number: i64, held: &str) -> bool {
    let held = library_key(held);
    let mut files = batch_files
        .iter()
        .map(|path| file_name(path))
        .filter(|name| is_video_file(name) && parse_release_title(name).episode == Some(number))
        .peekable();
    files.peek().is_some() && files.all(|name| library_key(name) != held)
}

/// Counts each batch's files against the folder from the file lists read for `files`. A batch
/// with no list keeps its count by episode number.
pub fn count_batch_files(preview: &mut Preview, files: &TorrentFiles) {
    let Some(folder) = &preview.folder else {
        return;
    };
    for batch in preview
        .feeds
        .iter_mut()
        .flat_map(|feed| feed.batches.iter_mut())
    {
        let Some(listed) = files.get(&batch.item_key) else {
            continue;
        };
        let counts = classify(listed, &folder.names);
        batch.on_disk = counts.on_disk;
        batch.newer = counts.newer;
        batch.to_download = counts.to_download;
        batch.files_known = true;
    }
}

/// What each feed carries for the entry, episode by episode and in batches, and the feed its
/// files on disk came from.
///
/// Meant for a dry run: the show it builds to number the releases exactly as the grabber will is
/// rolled back with the rest of the run.
pub fn preview(
    ctx: &mut Context<'_>,
    entry: &ListEntry,
    unsearchable: &[String],
    found: &[(i64, Vec<FeedArticle>)],
) -> Result<Preview> {
    if followed(ctx, entry.anilist_id)? {
        return Err(Refusal::AlreadyFollowed.into());
    }
    // Stored only for this dry run: the releases are kept for real when the user adds the show.
    store_found(ctx, found)?;
    let folder = existing_folder(ctx, entry);
    let show = draft(ctx, entry, folder.as_ref())?;
    let mut on_disk_feed: Option<Feed> = None;
    let mut note = None;
    if let Some(folder) = &folder {
        on_disk_feed = feed_from_files(ctx, &show, &folder.files)?;
        if on_disk_feed.is_none() {
            note = Some(format!(
                "{} in {} {} no feed's naming. Pick the feed yourself.",
                match folder.files.len() {
                    1 => "1 file".to_owned(),
                    count => format!("{count} files"),
                },
                folder.path,
                if folder.files.len() == 1 {
                    "matches"
                } else {
                    "match"
                }
            ));
        }
    }
    let files: &[String] = folder.as_ref().map_or(&[], |folder| &folder.files);
    let disk_keys: HashSet<String> = files.iter().map(|name| library_key(name)).collect();
    let disk_numbers: BTreeSet<i64> = files
        .iter()
        .filter_map(|name| parse_release_title(name).episode)
        .collect();
    let mut groups = Vec::new();
    for feed in feeds::ranked(ctx)? {
        let offered = releases(ctx, &show, &feed)?;
        let held = batches(ctx, &show, &feed)?;
        if offered.is_empty() && held.is_empty() {
            continue;
        }
        groups.push(FeedOffer {
            feed_id: feed.id.unwrap_or_default(),
            feed_name: feed.name.clone(),
            batches: held
                .iter()
                .map(|batch| BatchOffer {
                    item_key: item_key(&batch.item),
                    title: batch.item.title.clone(),
                    numbers: batch.numbers.clone(),
                    ranged: batch_range(&batch.item.title).is_some(),
                    published_at: batch.item.published_at,
                    torrent_url: batch.item.torrent_url.clone().unwrap_or_default(),
                    on_disk: batch
                        .numbers
                        .iter()
                        .filter(|number| disk_numbers.contains(number))
                        .count(),
                    newer: Vec::new(),
                    to_download: 0,
                    files_known: false,
                })
                .collect(),
            episodes: offered
                .iter()
                .map(|(number, release)| EpisodeOffer {
                    number: *number,
                    item_key: item_key(&release.entry.item),
                    title: release.entry.item.title.clone(),
                    version: release.entry.version,
                    published_at: release.entry.item.published_at,
                    on_disk: disk_keys.contains(&library_key(&release.entry.item.title)),
                })
                .collect(),
        });
    }
    let nothing_offered = groups.is_empty();
    if nothing_offered {
        // No feed offers a release (an upcoming show, or one the feeds lost): any feed may carry
        // it later, or the first one to post wins.
        groups = feeds::ranked(ctx)?
            .into_iter()
            .map(|feed| FeedOffer {
                feed_id: feed.id.unwrap_or_default(),
                feed_name: feed.name,
                batches: Vec::new(),
                episodes: Vec::new(),
            })
            .collect();
    }
    let preselect = on_disk_feed
        .and_then(|feed| feed.id)
        .filter(|id| groups.iter().any(|group| group.feed_id == *id));
    Ok(Preview {
        anilist_id: entry.anilist_id,
        nothing_offered,
        title: show.title(&ctx.settings.title_language),
        status: present(entry.status.as_str()),
        total_episodes: show.total_episodes,
        episode_count: shows::ensure_episodes(ctx, &show)?.len(),
        feeds: groups,
        unsearchable: unsearchable.to_vec(),
        folder: folder.map(|folder| FolderSummary {
            path: folder.path,
            files: folder.files.len(),
            names: folder.files,
        }),
        preselect,
        note,
        watched: show
            .watched
            .filter(|watched| ctx.settings.delete_watched && *watched != 0),
    })
}

fn followed(ctx: &Context<'_>, anilist_id: i64) -> Result<bool> {
    Ok(ctx
        .conn
        .prepare_cached("SELECT 1 FROM shows WHERE anilist_id = ?")?
        .exists([anilist_id])?)
}

/// Follows the entry from `feed_id` and grabs the chosen releases; the feed's other episodes are
/// skipped.
///
/// `feed_id` 0 leaves the feed to discovery (for a show nothing was released for yet). `found`
/// is what the search found; it is stored now, so that a search the user backed out of leaves
/// nothing behind.
pub fn confirm(
    ctx: &mut Context<'_>,
    entry: &ListEntry,
    feed_id: i64,
    item_keys: &[String],
    found: &[(i64, Vec<FeedArticle>)],
    torrent_files: &TorrentFiles,
) -> Result<Confirmed> {
    if followed(ctx, entry.anilist_id)? {
        return Err(Refusal::AlreadyFollowed.into());
    }
    let feed = match feed_id {
        0 => None,
        id => Some(feeds::feed_by_id(ctx, Some(id))?.ok_or(Refusal::FeedNotFound)?),
    };
    store_found(ctx, found)?;
    if let Some(feed) = &feed {
        let prefix = format!("{}:", feed.id.unwrap_or_default());
        let known: HashSet<String> =
            FeedItem::select(ctx.conn, "WHERE feed_id = ? ORDER BY id", [feed.id])?
                .iter()
                .map(item_key)
                .collect();
        if item_keys
            .iter()
            .any(|key| key.starts_with(&prefix) && !known.contains(key))
        {
            return Err(Refusal::ReleasesUnknown.into());
        }
    }
    let folder = existing_folder(ctx, entry);
    let paused_all = shows::all_paused(ctx)?;
    let mut show = shows::show_from_entry(entry, paused_all);
    show.save_folder = save_folder(ctx, &show, folder.as_ref());
    show.id = Some(show.insert(ctx.conn)?);
    ctx.conn.execute(
        "DELETE FROM dismissed_shows WHERE anilist_id = ?",
        [show.anilist_id],
    )?;
    let mut episodes = shows::ensure_episodes(ctx, &show)?;
    ingest::ensure_matches_current(ctx)?;
    let show_id = show.id.unwrap_or_default();
    let topic = format!("show:{show_id}");
    let name = show.title(&ctx.settings.title_language);
    let Some(feed) = feed else {
        info!("Added '{name}' by hand; its feed is left to discovery.");
        ctx.emit(&["shows", &topic]);
        return Ok(Confirmed {
            show_id,
            ..Confirmed::default()
        });
    };
    feeds::pin(ctx, &mut show, &feed, false)?;
    if let Some(folder) = &folder {
        let saved = adoption::feed_from_saved_files(ctx, &show, &folder.files, &[])?;
        if let Some(saved) = saved.filter(|saved| saved.feed.id == feed.id) {
            // The user kept the feed the files on disk came from: that is the evidence a lock
            // needs.
            feeds::lock(
                ctx,
                &mut show,
                &feed,
                Evidence::SavedFilesUnique,
                saved.title.as_deref(),
                saved.release_group.as_deref(),
            )?;
        }
    }
    let chosen: HashSet<&str> = item_keys.iter().map(String::as_str).collect();
    let offered_releases = releases(ctx, &show, &feed)?;
    let offered_batches = batches(ctx, &show, &feed)?;
    let batch_ids: HashSet<Option<i64>> =
        offered_batches.iter().map(|batch| batch.item.id).collect();
    // Each episode comes from one release: a chosen batch holds its episodes, singles fill in
    // the rest.
    let mut picked: BTreeMap<i64, FeedItem> = BTreeMap::new();
    for batch in &offered_batches {
        if chosen.contains(item_key(&batch.item).as_str()) {
            // A batch is followed for the episodes not yet watched; the watched ones' files go
            // with it once those are watched too.
            let unwatched: Vec<i64> = batch
                .numbers
                .iter()
                .copied()
                .filter(|number| !watched::is_watched(ctx, &show, *number))
                .collect();
            let numbers = if unwatched.is_empty() {
                &batch.numbers
            } else {
                &unwatched
            };
            for number in numbers {
                if episodes.iter().any(|episode| episode.number == *number) {
                    picked.entry(*number).or_insert_with(|| batch.item.clone());
                }
            }
        }
    }
    for (number, release) in &offered_releases {
        if chosen.contains(item_key(&release.entry.item).as_str()) {
            picked
                .entry(*number)
                .or_insert_with(|| release.entry.item.clone());
        }
    }
    let offered: BTreeSet<i64> = offered_releases
        .keys()
        .copied()
        .chain(
            offered_batches
                .iter()
                .flat_map(|batch| batch.numbers.iter().copied()),
        )
        .collect();
    for episode in &mut episodes {
        if offered.contains(&episode.number) && !picked.contains_key(&episode.number) {
            episode.status = EpisodeStatus::Skipped;
            episode.update(ctx.conn)?;
        } else if picked.contains_key(&episode.number) && episode.status == EpisodeStatus::Skipped {
            // Watched, but the user ticked it.
            episode.status = EpisodeStatus::Wanted;
            episode.update(ctx.conn)?;
        }
    }
    info!("Added '{name}' by hand, from {}.", feed.name);
    ctx.emit(&["shows", &topic]);
    let mut result = Confirmed {
        show_id,
        skipped: episodes
            .iter()
            .filter(|episode| episode.status == EpisodeStatus::Skipped)
            .count(),
        ..Confirmed::default()
    };
    if paused_all || picked.is_empty() {
        return Ok(result);
    }
    ctx.checkpoint()?;
    // Torrents already in qBittorrent are taken over rather than added twice.
    adoption::adopt_existing(ctx)?;
    let Some(mut show) = Show::find(ctx.conn, "WHERE id = ?", [show_id])? else {
        return Ok(result);
    };
    // By item, the torrent added for it and the first episode it went to.
    let mut added: HashMap<i64, (String, i64)> = HashMap::new();
    // By batch item, the downloads that follow its torrent.
    let mut batch_downloads: HashMap<i64, Vec<i64>> = HashMap::new();
    // By batch item, the episodes those downloads are for.
    let mut started_numbers: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    for (number, item) in &picked {
        let Some(mut episode) = Episode::find(
            ctx.conn,
            "WHERE show_id = ? AND number = ?",
            (show_id, number),
        )?
        else {
            continue;
        };
        let item_id = item.id.unwrap_or_default();
        let wanted = matches!(
            episode.status,
            EpisodeStatus::Wanted | EpisodeStatus::Missed
        );
        // A finished release of the episode, such as a torrent just taken over from qBittorrent, is
        // replaced by the batch's file when that is another version of it.
        let replaced = match torrent_files.get(&item_key(item)) {
            Some(listed) if !wanted && batch_ids.contains(&item.id) => {
                downloads::replaceable(ctx, &episode)?
                    .filter(|old| replaces(listed, *number, &old.title))
            }
            _ => None,
        };
        if !wanted && replaced.is_none() {
            continue;
        }
        let started = if let Some((info_hash, first)) = added.get(&item_id) {
            // The rest of a batch or double episode already in qBittorrent follow that torrent;
            // adding it again would fetch it again.
            let source = item.episode_raw.map(|raw| raw + number - first);
            Some(downloads::follow_download(
                ctx,
                &mut show,
                &feed,
                &mut episode,
                item,
                downloads::Follow {
                    info_hash,
                    source_episode: source,
                    supersedes: replaced.as_ref().and_then(|old| old.id),
                },
            )?)
        } else {
            let options = StartOptions {
                batch: batch_ids.contains(&item.id),
                switch_source: replaced.is_some(),
                ..StartOptions::default()
            };
            let dl = downloads::start_download(ctx, &mut show, &feed, &mut episode, item, options)?;
            if let Some(dl) = &dl {
                let hash = dl.info_hash.clone().filter(|hash| !hash.is_empty());
                if let Some(hash) =
                    hash.or_else(|| item.info_hash.clone().filter(|hash| !hash.is_empty()))
                {
                    if downloads::is_batch_download(dl) {
                        added.insert(item_id, (hash, *number));
                    }
                }
            }
            dl
        };
        match started {
            Some(dl) => {
                result.started += 1;
                if replaced.is_none()
                    && batch_ids.contains(&item.id)
                    && downloads::is_batch_download(&dl)
                {
                    batch_downloads.entry(item_id).or_default().extend(dl.id);
                    started_numbers.entry(item_id).or_default().insert(*number);
                }
            }
            None => result.errors.push(format!(
                "Ep {number}: {}",
                episode
                    .last_error
                    .as_deref()
                    .filter(|error| !error.is_empty())
                    .unwrap_or("could not start the download")
            )),
        }
    }
    if let Some(folder) = &folder {
        for batch in &offered_batches {
            let item_id = batch.item.id.unwrap_or_default();
            let (Some(listed), Some(waiting), Some(numbers)) = (
                torrent_files.get(&item_key(&batch.item)),
                batch_downloads.get(&item_id),
                started_numbers.get(&item_id),
            ) else {
                continue;
            };
            // Only the files of episodes this batch downloads: a file of any other episode is not
            // the batch's to delete.
            let older: Vec<String> = classify(listed, &folder.files)
                .older
                .iter()
                .filter(|(number, _)| numbers.contains(number))
                .map(|(_, file)| join_shown(&folder.path, file))
                .collect();
            if !older.is_empty() {
                let own: Vec<String> = listed
                    .iter()
                    .map(|path| join_shown(&folder.path, path))
                    .collect();
                older_files::wait_for_batch(ctx, &name, waiting, &older, &own)?;
            }
        }
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_release_name_is_compared_without_its_video_extension_or_case() {
        assert_eq!(library_key("[Grp] Show - 01.MKV"), "[grp] show - 01");
        assert_eq!(library_key("  Show 01.mp4 "), "show 01.mp4");
        assert_eq!(library_key("Show 01.srt"), "show 01.srt");
        assert_eq!(library_key(".mkv"), ".mkv");
        assert_eq!(library_key("No Extension"), "no extension");
    }

    #[test]
    fn the_download_folder_is_shown_as_the_user_wrote_it() {
        assert_eq!(
            configured_root(r"  D:\Torrents\Seasonal\{name}"),
            r"D:\Torrents\Seasonal"
        );
        assert_eq!(configured_root("D:/Anime/"), "D:/Anime");
        assert_eq!(configured_root(""), "");
    }

    #[test]
    fn a_folder_is_joined_to_the_root_with_the_separator_the_root_uses() {
        assert_eq!(
            join_shown("D:/Torrents/Seasonal", "Grand Blue Season 3"),
            "D:/Torrents/Seasonal/Grand Blue Season 3"
        );
        assert_eq!(
            join_shown(r"D:\Torrents\Seasonal", "Grand Blue"),
            r"D:\Torrents\Seasonal\Grand Blue"
        );
        assert_eq!(
            join_shown(r"D:/Mixed\Seasonal", "Show"),
            r"D:/Mixed\Seasonal\Show"
        );
        assert_eq!(join_shown("", "Show"), "Show");
        assert_eq!(
            join_shown("Seasonal", "Show"),
            format!("Seasonal{MAIN_SEPARATOR}Show")
        );
    }

    #[test]
    fn the_found_folder_is_shown_with_the_separator_of_the_setting() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_string_lossy().replace('\\', "/");
        let folder = dir.path().join("Grand Blue Season 3");
        fs::create_dir(&folder).unwrap();
        fs::write(
            folder.join("[SubsPlease] Grand Blue S3 - 01 (1080p) [A13D691B].mkv"),
            "",
        )
        .unwrap();
        let conn = kisetsu_db::store::open_in_memory().unwrap();
        let mut caches = crate::context::Caches::default();
        let mut ctx = Context::new(&conn, Timestamp::UNIX_EPOCH, &mut caches).unwrap();
        ctx.settings.base_dir = format!("{root}/{{name}}");
        let entry = crate::fakes::anilist_show(1, "Grand Blue Season 3");

        let found = existing_folder(&ctx, &entry).unwrap();

        assert_eq!(found.path, format!("{root}/Grand Blue Season 3"));
        assert_eq!(found.files.len(), 1);
    }

    fn disk_files() -> Vec<String> {
        (1..=12)
            .map(|n| format!("[SubsPlease] Grand Blue S3 - {n:02} (1080p) [A13D69{n:02}].mkv"))
            .collect()
    }

    /// The batch's files: a new version of episodes 1 to 5 and, for 6 to 12, the files on disk.
    fn batch_files() -> Vec<String> {
        let own = disk_files();
        (1..=12)
            .map(|n| {
                if n <= 5 {
                    format!("[SubsPlease] Grand Blue S3 - {n:02}v2 (1080p) [96D6C3{n:02}].mkv")
                } else {
                    own[n - 1].clone()
                }
            })
            .collect()
    }

    #[test]
    fn a_batch_is_counted_by_its_files_against_the_folder() {
        let mut listed = batch_files();
        listed.push("Extras/Creditless OP.MKV".to_owned());
        listed.push("Show.nfo".to_owned());
        let counts = classify(&listed, &disk_files());
        assert_eq!(counts.on_disk, 7);
        assert_eq!(counts.newer, [1, 2, 3, 4, 5]);
        assert_eq!(counts.to_download, 1);
        let older: Vec<(i64, String)> = (1..=5).zip(disk_files()).collect();
        assert_eq!(counts.older, older);
    }

    #[test]
    fn a_batch_matching_the_folder_in_full_has_nothing_to_download() {
        let counts = classify(&disk_files(), &disk_files());
        assert_eq!(
            counts,
            FileCounts {
                on_disk: 12,
                ..FileCounts::default()
            }
        );
    }

    #[test]
    fn the_batch_counts_reach_the_offer_only_when_its_files_were_read() {
        let offer = |key: &str| BatchOffer {
            item_key: key.to_owned(),
            title: "Batch".to_owned(),
            numbers: (1..=12).collect(),
            ranged: true,
            published_at: None,
            torrent_url: String::new(),
            on_disk: 12,
            newer: Vec::new(),
            to_download: 0,
            files_known: false,
        };
        let mut preview = Preview {
            anilist_id: 1,
            nothing_offered: false,
            title: String::new(),
            status: None,
            total_episodes: Some(12),
            episode_count: 12,
            feeds: vec![FeedOffer {
                feed_id: 1,
                feed_name: "Feed".to_owned(),
                batches: vec![offer("1:read"), offer("1:unread")],
                episodes: Vec::new(),
            }],
            unsearchable: Vec::new(),
            folder: Some(FolderSummary {
                path: "D:/x".to_owned(),
                files: 12,
                names: disk_files(),
            }),
            preselect: None,
            note: None,
            watched: None,
        };
        let files = TorrentFiles::from([("1:read".to_owned(), batch_files())]);

        count_batch_files(&mut preview, &files);

        let [read, unread] = &preview.feeds[0].batches[..] else {
            panic!("two batches");
        };
        assert_eq!(
            (
                read.files_known,
                read.on_disk,
                read.newer.len(),
                read.to_download
            ),
            (true, 7, 5, 0)
        );
        assert_eq!((unread.files_known, unread.on_disk), (false, 12));
    }
}
