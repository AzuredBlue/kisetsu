//! The sandbox's qBittorrent: real torrents are read, every change is logged and only pretended.
//!
//! Torrents the sandbox adds are fakes that download for a while and then finish, so the whole flow
//! shows in the UI. Deleting, moving or tagging a real torrent only changes how the sandbox sees
//! it. The real client underneath is only ever read from, so a change can never reach qBittorrent.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

use jiff::Timestamp;
use sha1_smol::Sha1;
use tracing::{debug, info};

use crate::qbittorrent::{
    AddTorrent, ContentLayout, Qbit, QbitError, QbitResult, RssStoreFeed, SyncResult, Torrent,
    Versions,
};
use crate::rss::{magnet_parts, normalize_info_hash};

/// How long a pretend download takes to finish, in seconds.
pub const DOWNLOAD_SECONDS: f64 = 60.0;
const FAKE_SIZE: i64 = 500 * 1024 * 1024;

/// The release name and info hash a torrent URL is known by.
pub type ReleaseFor = dyn Fn(&str) -> (Option<String>, Option<String>) + Send + Sync;

/// A torrent the sandbox pretends to download.
#[derive(Debug, Clone)]
struct Fake {
    hash: String,
    name: String,
    save_path: String,
    content_path: String,
    category: String,
    tags: BTreeSet<String>,
    added_at: f64,
    stop_when_done: bool,
    paused: bool,
    /// Seconds of downloading done before the current run.
    banked: f64,
    /// When the current run began; `None` while paused.
    running_since: Option<f64>,
    completed_at: Option<f64>,
}

/// The fakes in the order they were added.
#[derive(Debug, Default)]
struct Fakes(Vec<Fake>);

impl Fakes {
    fn get(&self, hash: &str) -> Option<&Fake> {
        self.0.iter().find(|fake| fake.hash == hash)
    }

    fn get_mut(&mut self, hash: &str) -> Option<&mut Fake> {
        self.0.iter_mut().find(|fake| fake.hash == hash)
    }

    fn contains_key(&self, hash: &str) -> bool {
        self.get(hash).is_some()
    }

    fn insert(&mut self, fake: Fake) {
        self.0.push(fake);
    }

    fn remove(&mut self, hash: &str) -> Option<Fake> {
        let at = self.0.iter().position(|fake| fake.hash == hash)?;
        Some(self.0.remove(at))
    }
}

/// What the sandbox pretends happened in qBittorrent.
///
/// One object for the whole run, shared by every client built from it: the engine builds a new
/// client whenever the qBittorrent settings change, and the fakes must survive that.
pub struct SandboxState {
    /// Seconds since the epoch.
    clock: Box<dyn Fn() -> f64 + Send>,
    download_seconds: f64,
    release_for: Box<ReleaseFor>,
    fakes: Fakes,
    /// Real torrents the sandbox deleted.
    hidden: HashSet<String>,
    /// Its tag and folder changes to the others.
    tags_added: HashMap<String, BTreeSet<String>>,
    tags_removed: HashMap<String, BTreeSet<String>>,
    tags_deleted: BTreeSet<String>,
    locations: HashMap<String, String>,
}

impl SandboxState {
    /// A state on the wall clock, with torrent URLs known by nothing but themselves.
    pub fn new() -> Self {
        Self::with(
            Box::new(|| {
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map_or(0.0, |elapsed| elapsed.as_secs_f64())
            }),
            DOWNLOAD_SECONDS,
            Box::new(|_| (None, None)),
        )
    }

    pub fn with(
        clock: Box<dyn Fn() -> f64 + Send>,
        download_seconds: f64,
        release_for: Box<ReleaseFor>,
    ) -> Self {
        Self {
            clock,
            download_seconds,
            release_for,
            fakes: Fakes::default(),
            hidden: HashSet::new(),
            tags_added: HashMap::new(),
            tags_removed: HashMap::new(),
            tags_deleted: BTreeSet::new(),
            locations: HashMap::new(),
        }
    }

    /// Names the release and info hash a torrent URL is known by (from its download row), so a
    /// pretend torrent carries the hash the real one would and a duplicate is refused as
    /// qBittorrent would.
    pub fn set_release_for(&mut self, release_for: Box<ReleaseFor>) {
        self.release_for = release_for;
    }

    /// Shares the state between clients.
    pub fn shared(self) -> SharedSandbox {
        Arc::new(Mutex::new(self))
    }
}

impl Default for SandboxState {
    fn default() -> Self {
        Self::new()
    }
}

pub type SharedSandbox = Arc<Mutex<SandboxState>>;

fn when(seconds: Option<f64>) -> Option<Timestamp> {
    Timestamp::from_microsecond((seconds? * 1e6).round() as i64).ok()
}

fn hash_of(url: &str, known: Option<&str>) -> String {
    if let Some(known) = known.filter(|known| !known.is_empty()) {
        return known.to_lowercase();
    }
    if url.starts_with("magnet:") {
        if let Some(hashed) = magnet_parts(url)
            .0
            .and_then(|hash| normalize_info_hash(&hash))
        {
            return hashed;
        }
    }
    Sha1::from(url).digest().to_string()
}

fn fallback_name(url: &str) -> String {
    if url.starts_with("magnet:") {
        if let Some(name) = magnet_parts(url).1 {
            return name;
        }
    }
    match url.rsplit('/').next() {
        Some(last) if !last.is_empty() => last.to_owned(),
        _ => url.to_owned(),
    }
}

fn join(folder: &str, name: &str) -> String {
    Path::new(folder).join(name).to_string_lossy().into_owned()
}

fn base_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn locked(state: &SharedSandbox) -> MutexGuard<'_, SandboxState> {
    state
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A qBittorrent that reads the real one underneath and pretends every change.
pub struct SandboxQbit<Q: Qbit> {
    inner: Q,
    state: SharedSandbox,
    merged: BTreeMap<String, Torrent>,
}

impl<Q: Qbit> SandboxQbit<Q> {
    pub fn new(inner: Q, state: SharedSandbox) -> Self {
        Self {
            inner,
            state,
            merged: BTreeMap::new(),
        }
    }

    /// The real client underneath.
    pub fn inner_mut(&mut self) -> &mut Q {
        &mut self.inner
    }

    fn fake_torrent(download_seconds: f64, fake: &mut Fake, now: f64) -> Torrent {
        let running = fake.running_since.map_or(0.0, |since| now - since);
        let done = fake.banked + running;
        let progress = if download_seconds > 0.0 {
            (done / download_seconds).min(1.0)
        } else {
            1.0
        };
        if progress >= 1.0 && fake.completed_at.is_none() {
            fake.completed_at = Some(now);
            info!("[sandbox] '{}' finished downloading (pretend).", fake.name);
        }
        let status = if progress >= 1.0 {
            if fake.paused || fake.stop_when_done {
                "stoppedUP"
            } else {
                "uploading"
            }
        } else if fake.paused {
            "stoppedDL"
        } else {
            "downloading"
        };
        let active = if fake.running_since.is_some() {
            Some(now)
        } else {
            fake.completed_at.or(Some(fake.added_at))
        };
        Torrent {
            hash: fake.hash.clone(),
            name: fake.name.clone(),
            state: status.to_owned(),
            progress,
            amount_left: (FAKE_SIZE as f64 * (1.0 - progress)) as i64,
            size: FAKE_SIZE,
            save_path: fake.save_path.clone(),
            content_path: fake.content_path.clone(),
            category: fake.category.clone(),
            tags: fake.tags.clone(),
            added_on: when(Some(fake.added_at)),
            completion_on: when(fake.completed_at),
            last_activity: when(active),
        }
    }

    fn names(&self, hashes: &[String]) -> String {
        hashes
            .iter()
            .map(|hash| match self.merged.get(hash) {
                Some(torrent) => format!("'{}'", torrent.name),
                None => hash.clone(),
            })
            .collect::<Vec<_>>()
            .join(", ")
    }
}

impl<Q: Qbit> Qbit for SandboxQbit<Q> {
    fn versions(&mut self) -> QbitResult<Versions> {
        self.inner.versions()
    }

    fn sync(&mut self, full: bool) -> QbitResult<SyncResult> {
        let real = self.inner.sync(full)?;
        let mut state = locked(&self.state);
        let now = (state.clock)();
        let mut merged: BTreeMap<String, Torrent> = BTreeMap::new();
        for (hash, torrent) in self.inner.torrents() {
            if state.hidden.contains(hash) {
                continue;
            }
            let mut tags = torrent.tags.clone();
            if let Some(added) = state.tags_added.get(hash) {
                tags.extend(added.iter().cloned());
            }
            if let Some(removed) = state.tags_removed.get(hash) {
                tags.retain(|tag| !removed.contains(tag));
            }
            let mut seen = torrent.clone();
            if let Some(location) = state.locations.get(hash) {
                seen.save_path = location.clone();
            }
            seen.tags = tags;
            merged.insert(hash.clone(), seen);
        }
        let download_seconds = state.download_seconds;
        for fake in &mut state.fakes.0 {
            merged.insert(
                fake.hash.clone(),
                Self::fake_torrent(download_seconds, fake, now),
            );
        }
        drop(state);
        let before = std::mem::replace(&mut self.merged, merged);
        let changed = self
            .merged
            .iter()
            .filter(|(hash, torrent)| before.get(*hash) != Some(torrent))
            .map(|(hash, _)| hash.clone())
            .collect();
        let removed = before
            .keys()
            .filter(|hash| !self.merged.contains_key(*hash))
            .cloned()
            .collect();
        Ok(SyncResult {
            full: real.full,
            changed,
            removed,
        })
    }

    fn torrents(&self) -> &BTreeMap<String, Torrent> {
        &self.merged
    }

    fn synced(&self) -> bool {
        self.inner.synced()
    }

    fn files(&mut self, info_hash: &str) -> QbitResult<Vec<String>> {
        // A pretend torrent is one file named like the release.
        match locked(&self.state).fakes.get(info_hash) {
            Some(fake) => Ok(vec![fake.name.clone()]),
            None => self.inner.files(info_hash),
        }
    }

    fn tags(&mut self) -> QbitResult<Vec<String>> {
        let mut names: BTreeSet<String> = self.inner.tags()?.into_iter().collect();
        let state = locked(&self.state);
        names.retain(|name| !state.tags_deleted.contains(name));
        for fake in &state.fakes.0 {
            names.extend(fake.tags.iter().cloned());
        }
        for added in state.tags_added.values() {
            names.extend(added.iter().cloned());
        }
        Ok(names.into_iter().collect())
    }

    fn add_torrent(&mut self, add: &AddTorrent) -> QbitResult<()> {
        let mut state = locked(&self.state);
        let (title, known_hash) = (state.release_for)(&add.url);
        let info_hash = hash_of(&add.url, known_hash.as_deref());
        let name = title
            .filter(|title| !title.is_empty())
            .unwrap_or_else(|| fallback_name(&add.url));
        let real_has_it =
            self.inner.torrents().contains_key(&info_hash) && !state.hidden.contains(&info_hash);
        if state.fakes.contains_key(&info_hash) || real_has_it {
            info!(
                "[sandbox] Would add '{name}', but qBittorrent already has it, so it would refuse; the existing torrent is used."
            );
            return Err(QbitError::Duplicate(format!(
                "qBittorrent already has '{}'",
                add.url
            )));
        }
        let now = (state.clock)();
        state.hidden.remove(&info_hash);
        let content_path = if add.content_layout == Some(ContentLayout::NoSubfolder) {
            add.save_path.clone()
        } else {
            join(&add.save_path, &name)
        };
        state.fakes.insert(Fake {
            hash: info_hash,
            name: name.clone(),
            save_path: add.save_path.clone(),
            content_path,
            category: add.category.clone(),
            tags: add.tags.iter().cloned().collect(),
            added_at: now,
            stop_when_done: add.ratio_limit == Some(0.0),
            paused: add.paused,
            banked: 0.0,
            running_since: (!add.paused).then_some(now),
            completed_at: None,
        });
        let place = if add.save_path.is_empty() {
            "qBittorrent's default folder"
        } else {
            add.save_path.as_str()
        };
        info!(
            "[sandbox] Would add '{name}' to {place}{}.",
            if add.paused { " paused" } else { "" }
        );
        Ok(())
    }

    fn pause(&mut self, hashes: &[String]) -> QbitResult<()> {
        if hashes.is_empty() {
            return Ok(());
        }
        let mut state = locked(&self.state);
        let now = (state.clock)();
        for hash in hashes {
            if let Some(fake) = state.fakes.get_mut(hash).filter(|fake| !fake.paused) {
                if let Some(since) = fake.running_since {
                    fake.banked += now - since;
                }
                fake.running_since = None;
                fake.paused = true;
            }
        }
        drop(state);
        info!("[sandbox] Would pause {}.", self.names(hashes));
        Ok(())
    }

    fn resume(&mut self, hashes: &[String]) -> QbitResult<()> {
        if hashes.is_empty() {
            return Ok(());
        }
        let mut state = locked(&self.state);
        let now = (state.clock)();
        for hash in hashes {
            if let Some(fake) = state.fakes.get_mut(hash).filter(|fake| fake.paused) {
                fake.paused = false;
                fake.running_since = Some(now);
            }
        }
        drop(state);
        info!("[sandbox] Would resume {}.", self.names(hashes));
        Ok(())
    }

    fn recheck(&mut self, hashes: &[String]) -> QbitResult<()> {
        if !hashes.is_empty() {
            info!("[sandbox] Would recheck {}.", self.names(hashes));
        }
        Ok(())
    }

    fn delete(&mut self, hashes: &[String], delete_files: bool) -> QbitResult<()> {
        if hashes.is_empty() {
            return Ok(());
        }
        info!(
            "[sandbox] Would delete {} {}.",
            self.names(hashes),
            if delete_files {
                "with its files"
            } else {
                "and keep its files"
            }
        );
        let mut state = locked(&self.state);
        for hash in hashes {
            if state.fakes.remove(hash).is_none() {
                state.hidden.insert(hash.clone());
            }
        }
        Ok(())
    }

    fn set_location(&mut self, hashes: &[String], location: &str) -> QbitResult<()> {
        if location.is_empty() || hashes.is_empty() {
            return Ok(());
        }
        info!("[sandbox] Would move {} to {location}.", self.names(hashes));
        let mut state = locked(&self.state);
        for hash in hashes {
            match state.fakes.get_mut(hash) {
                Some(fake) => {
                    fake.content_path = join(location, &base_name(&fake.content_path));
                    fake.save_path = location.to_owned();
                }
                None => {
                    state.locations.insert(hash.clone(), location.to_owned());
                }
            }
        }
        Ok(())
    }

    fn add_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        if hashes.is_empty() || tags.is_empty() {
            return Ok(());
        }
        debug!(
            "[sandbox] Would tag {} with {}.",
            self.names(hashes),
            tags.join(", ")
        );
        let mut state = locked(&self.state);
        state.tags_deleted.retain(|tag| !tags.contains(tag));
        for hash in hashes {
            if let Some(fake) = state.fakes.get_mut(hash) {
                fake.tags.extend(tags.iter().cloned());
            } else {
                state
                    .tags_added
                    .entry(hash.clone())
                    .or_default()
                    .extend(tags.iter().cloned());
                if let Some(removed) = state.tags_removed.get_mut(hash) {
                    removed.retain(|tag| !tags.contains(tag));
                }
            }
        }
        Ok(())
    }

    fn remove_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        if hashes.is_empty() || tags.is_empty() {
            return Ok(());
        }
        debug!(
            "[sandbox] Would remove tags {} from {}.",
            tags.join(", "),
            self.names(hashes)
        );
        self.forget_tags(hashes, tags);
        Ok(())
    }

    fn default_save_path(&mut self) -> QbitResult<String> {
        self.inner.default_save_path()
    }

    fn delete_tags(&mut self, tags: &[String]) -> QbitResult<()> {
        if tags.is_empty() {
            return Ok(());
        }
        debug!("[sandbox] Would delete the tags {}.", tags.join(", "));
        locked(&self.state)
            .tags_deleted
            .extend(tags.iter().cloned());
        let mut carrying: Vec<String> = self
            .merged
            .iter()
            .filter(|(_, torrent)| tags.iter().any(|tag| torrent.tags.contains(tag)))
            .map(|(hash, _)| hash.clone())
            .collect();
        carrying.sort();
        self.remove_tags(&carrying, tags)
    }

    fn ensure_category(&mut self, category: &str) -> QbitResult<()> {
        if !category.trim().is_empty() {
            debug!("[sandbox] Would make sure the category '{category}' exists.");
        }
        Ok(())
    }

    fn rss_feeds(&mut self) -> QbitResult<Vec<RssStoreFeed>> {
        self.inner.rss_feeds()
    }

    fn close(&mut self) {
        self.inner.close();
    }
}

impl<Q: Qbit> SandboxQbit<Q> {
    fn forget_tags(&mut self, hashes: &[String], tags: &[String]) {
        let mut state = locked(&self.state);
        for hash in hashes {
            if let Some(fake) = state.fakes.get_mut(hash) {
                fake.tags.retain(|tag| !tags.contains(tag));
            } else {
                state
                    .tags_removed
                    .entry(hash.clone())
                    .or_default()
                    .extend(tags.iter().cloned());
                if let Some(added) = state.tags_added.get_mut(hash) {
                    added.retain(|tag| !tags.contains(tag));
                }
            }
        }
    }
}
