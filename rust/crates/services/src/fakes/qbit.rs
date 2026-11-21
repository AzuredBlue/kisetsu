use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use kisetsu_clients::qbittorrent::{
    AddTorrent, ContentLayout, Qbit, QbitError, QbitResult, RssStoreFeed, SyncResult, Torrent,
    Versions,
};
use kisetsu_clients::rss::{magnet_parts, normalize_info_hash};
use sha1_smol::Sha1;

use super::clock::FakeClock;
use crate::context::Clock;

/// The size of a torrent nobody gave a size.
const DEFAULT_SIZE: i64 = 1_000_000;

/// The info hash qBittorrent would give a torrent added from `url`.
pub fn hash_for(url: &str) -> String {
    let magnet = url
        .starts_with("magnet:")
        .then(|| magnet_parts(url).0)
        .flatten()
        .and_then(|hash| normalize_info_hash(&hash));
    magnet
        .or_else(|| torrent_file_hash(url).map(str::to_owned))
        .unwrap_or_else(|| Sha1::from(url).digest().to_string())
}

/// The hash in an address that ends in `/<40 hex digits>.torrent`.
fn torrent_file_hash(url: &str) -> Option<&str> {
    let stem = url.strip_suffix(".torrent")?;
    let (head, hash) = stem.split_at_checked(stem.len().checked_sub(40)?)?;
    let hex = hash
        .bytes()
        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    (head.ends_with('/') && hex).then_some(hash)
}

/// A call the fake qBittorrent received, with what it was asked.
#[derive(Debug, Clone, PartialEq)]
pub enum QbitCall {
    Versions,
    Sync,
    Files(String),
    AddTorrent(AddTorrent),
    Pause(Vec<String>),
    Resume(Vec<String>),
    Recheck(Vec<String>),
    Delete {
        hashes: Vec<String>,
        delete_files: bool,
    },
    SetLocation {
        hashes: Vec<String>,
        location: String,
    },
    AddTags {
        hashes: Vec<String>,
        tags: Vec<String>,
    },
    RemoveTags {
        hashes: Vec<String>,
        tags: Vec<String>,
    },
    Tags,
    DeleteTags(Vec<String>),
    DefaultSavePath,
    EnsureCategory(String),
    RssFeeds,
}

impl QbitCall {
    /// The name of the `Qbit` method that was called.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Versions => "versions",
            Self::Sync => "sync",
            Self::Files(_) => "files",
            Self::AddTorrent(_) => "add_torrent",
            Self::Pause(_) => "pause",
            Self::Resume(_) => "resume",
            Self::Recheck(_) => "recheck",
            Self::Delete { .. } => "delete",
            Self::SetLocation { .. } => "set_location",
            Self::AddTags { .. } => "add_tags",
            Self::RemoveTags { .. } => "remove_tags",
            Self::Tags => "tags",
            Self::DeleteTags(_) => "delete_tags",
            Self::DefaultSavePath => "default_save_path",
            Self::EnsureCategory(_) => "ensure_category",
            Self::RssFeeds => "rss_feeds",
        }
    }
}

/// A torrent that was already in qBittorrent before Kisetsu ran.
#[derive(Debug, Clone)]
pub struct Seed {
    pub name: String,
    pub save_path: String,
    pub category: String,
    pub tags: BTreeSet<String>,
    pub finished: bool,
    /// The address the torrent is known by; its hash comes from this.
    pub url: Option<String>,
}

impl Seed {
    /// A finished torrent without category or tags.
    pub fn new(name: impl Into<String>, save_path: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            save_path: save_path.into(),
            category: String::new(),
            tags: BTreeSet::new(),
            finished: true,
            url: None,
        }
    }
}

/// qBittorrent with the public surface of the `Qbit` trait.
///
/// Torrents live in `truth`. The client only sees them after `sync`, as with the real one. Tests
/// move torrents along with `finish`, `fail`, `remove` and friends.
#[derive(Debug)]
pub struct FakeQbit {
    pub clock: FakeClock,
    pub truth: BTreeMap<String, Torrent>,
    /// What the last sync showed.
    pub view: BTreeMap<String, Torrent>,
    /// Torrents added while `hide_added` was set, until `reveal`.
    pub hidden: BTreeMap<String, Torrent>,
    pub synced: bool,
    pub calls: Vec<QbitCall>,
    /// Every call fails with this.
    pub unavailable: Option<QbitError>,
    /// Every add fails with this.
    pub reject_adds: Option<QbitError>,
    pub hide_added: bool,
    pub hold_checks: bool,
    /// The name of a torrent added from an address.
    pub names: BTreeMap<String, String>,
    /// The size of a torrent added from an address.
    pub sizes: BTreeMap<String, i64>,
    /// The title feed articles give their torrent address: what qBittorrent would read out of the
    /// torrent's metadata once it had added it.
    pub article_titles: BTreeMap<String, String>,
    pub deleted_with_files: Vec<String>,
    pub categories: BTreeSet<String>,
    pub tag_names: BTreeSet<String>,
    pub rss: Vec<RssStoreFeed>,
    /// A torrent's files within its save folder; one named like the torrent unless set here.
    pub file_names: BTreeMap<String, Vec<String>>,
    /// Errors the next calls of a method answer with, in place of running it. Such a call is not
    /// logged.
    pub overrides: HashMap<String, VecDeque<QbitError>>,
}

impl FakeQbit {
    pub fn new(clock: FakeClock) -> Self {
        Self {
            clock,
            truth: BTreeMap::new(),
            view: BTreeMap::new(),
            hidden: BTreeMap::new(),
            synced: false,
            calls: Vec::new(),
            unavailable: None,
            reject_adds: None,
            hide_added: false,
            hold_checks: false,
            names: BTreeMap::new(),
            sizes: BTreeMap::new(),
            article_titles: BTreeMap::new(),
            deleted_with_files: Vec::new(),
            categories: BTreeSet::new(),
            tag_names: BTreeSet::new(),
            rss: Vec::new(),
            file_names: BTreeMap::new(),
            overrides: HashMap::new(),
        }
    }

    /// Logs the call, then fails it the way the test arranged.
    fn begin(&mut self, call: QbitCall) -> QbitResult<()> {
        if let Some(error) = self
            .overrides
            .get_mut(call.name())
            .and_then(VecDeque::pop_front)
        {
            return Err(error);
        }
        self.calls.push(call);
        match &self.unavailable {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }

    /// The name qBittorrent gives a torrent added from `url`.
    pub fn name_for(&self, url: &str) -> String {
        if let Some(title) = self.article_titles.get(url) {
            return title.clone();
        }
        let magnet_name = url
            .starts_with("magnet:")
            .then(|| magnet_parts(url).1)
            .flatten()
            .filter(|name| !name.is_empty());
        if let Some(name) = magnet_name {
            return name;
        }
        match url.rsplit('/').next() {
            Some(last) if !last.is_empty() => last.to_owned(),
            _ => url.to_owned(),
        }
    }

    fn update(&mut self, hash: &str, change: impl FnOnce(&mut Torrent)) {
        if let Some(torrent) = self.truth.get_mut(hash) {
            change(torrent);
        }
    }

    // -- test controls --

    /// The torrent added from `url`.
    pub fn torrent_for(&self, url: &str) -> Option<&Torrent> {
        self.truth.get(&hash_for(url))
    }

    /// The hash of a torrent given its address or its hash.
    fn hash_of(&self, target: &str) -> String {
        if self.truth.contains_key(target) {
            target.to_owned()
        } else {
            hash_for(target)
        }
    }

    /// Completes a torrent given its address or its hash.
    pub fn finish(&mut self, target: &str) {
        let hash = self.hash_of(target);
        let now = self.clock.now();
        self.update(&hash, |torrent| {
            let stopped =
                torrent.state.starts_with("stopped") || torrent.state.starts_with("paused");
            torrent.state = if stopped { "stoppedUP" } else { "stalledUP" }.to_owned();
            torrent.progress = 1.0;
            torrent.amount_left = 0;
            torrent.completion_on = Some(now);
        });
    }

    /// Puts a torrent given its address or its hash in `state`.
    pub fn fail(&mut self, target: &str, state: &str) {
        let hash = self.hash_of(target);
        self.update(&hash, |torrent| state.clone_into(&mut torrent.state));
    }

    /// The user deletes a torrent in qBittorrent.
    pub fn remove(&mut self, target: &str) {
        let hash = self.hash_of(target);
        self.truth.remove(&hash);
    }

    /// Adds a torrent that was in qBittorrent before Kisetsu ran.
    pub fn seed(&mut self, seed: Seed) -> Torrent {
        let key = seed.url.unwrap_or_else(|| format!("seed:{}", seed.name));
        let now = self.clock.now();
        let torrent = Torrent {
            hash: hash_for(&key),
            content_path: format!("{}/{}", seed.save_path, seed.name),
            name: seed.name,
            state: if seed.finished {
                "stalledUP"
            } else {
                "downloading"
            }
            .to_owned(),
            progress: if seed.finished { 1.0 } else { 0.0 },
            amount_left: if seed.finished { 0 } else { 1000 },
            size: 1000,
            save_path: seed.save_path,
            category: seed.category,
            tags: seed.tags,
            added_on: Some(now),
            completion_on: seed.finished.then_some(now),
            last_activity: None,
        };
        self.truth.insert(torrent.hash.clone(), torrent.clone());
        torrent
    }

    /// Makes torrents added while `hide_added` was set visible.
    pub fn reveal(&mut self) {
        self.truth.append(&mut self.hidden);
    }

    /// Ends the checks that `hold_checks` kept running.
    pub fn finish_checks(&mut self) {
        let checking: Vec<String> = self
            .truth
            .values()
            .filter(|torrent| torrent.checking())
            .map(|torrent| torrent.hash.clone())
            .collect();
        for hash in checking {
            self.update(&hash, |torrent| {
                torrent.state = if torrent.finished() {
                    "stoppedUP"
                } else {
                    "stoppedDL"
                }
                .to_owned();
            });
        }
    }
}

impl Qbit for FakeQbit {
    fn versions(&mut self) -> QbitResult<Versions> {
        self.begin(QbitCall::Versions)?;
        Ok(Versions {
            app_version: "v5.3.0".to_owned(),
            api_version: "2.16.2".to_owned(),
        })
    }

    fn sync(&mut self, full: bool) -> QbitResult<SyncResult> {
        self.begin(QbitCall::Sync)?;
        if full {
            self.view.clear();
        }
        let changed = self
            .truth
            .iter()
            .filter(|(hash, torrent)| self.view.get(*hash) != Some(*torrent))
            .map(|(hash, _)| hash.clone())
            .collect();
        let removed = self
            .view
            .keys()
            .filter(|hash| !self.truth.contains_key(*hash))
            .cloned()
            .collect();
        let was_full = !self.synced || full;
        self.view = self.truth.clone();
        self.synced = true;
        Ok(SyncResult {
            full: was_full,
            changed,
            removed,
        })
    }

    fn torrents(&self) -> &BTreeMap<String, Torrent> {
        &self.view
    }

    fn synced(&self) -> bool {
        self.synced
    }

    fn files(&mut self, info_hash: &str) -> QbitResult<Vec<String>> {
        self.begin(QbitCall::Files(info_hash.to_owned()))?;
        if let Some(names) = self
            .file_names
            .get(info_hash)
            .filter(|names| !names.is_empty())
        {
            return Ok(names.clone());
        }
        match self.truth.get(info_hash) {
            Some(torrent) => Ok(vec![torrent.name.clone()]),
            None => Err(QbitError::Other(format!("no torrent {info_hash}"))),
        }
    }

    fn add_torrent(&mut self, add: &AddTorrent) -> QbitResult<()> {
        self.begin(QbitCall::AddTorrent(add.clone()))?;
        if let Some(error) = &self.reject_adds {
            return Err(error.clone());
        }
        let hash = hash_for(&add.url);
        if self.truth.contains_key(&hash) || self.hidden.contains_key(&hash) {
            return Err(QbitError::Duplicate(format!(
                "qBittorrent already has '{}'",
                add.url
            )));
        }
        let name = self
            .names
            .get(&add.url)
            .filter(|name| !name.is_empty())
            .cloned()
            .unwrap_or_else(|| self.name_for(&add.url));
        let size = self.sizes.get(&add.url).copied().unwrap_or(DEFAULT_SIZE);
        let content_path = if add.save_path.is_empty() {
            name.clone()
        } else if add.content_layout == Some(ContentLayout::NoSubfolder) {
            add.save_path.clone()
        } else {
            format!("{}/{name}", add.save_path)
        };
        let torrent = Torrent {
            hash: hash.clone(),
            name,
            state: if add.paused {
                "stoppedDL"
            } else {
                "downloading"
            }
            .to_owned(),
            progress: 0.0,
            amount_left: size,
            size,
            save_path: add.save_path.clone(),
            content_path,
            category: add.category.clone(),
            tags: add.tags.iter().cloned().collect(),
            added_on: Some(self.clock.now()),
            completion_on: None,
            last_activity: None,
        };
        let shelf = if self.hide_added {
            &mut self.hidden
        } else {
            &mut self.truth
        };
        shelf.insert(hash, torrent);
        Ok(())
    }

    fn pause(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::Pause(hashes.to_vec()))?;
        for hash in hashes {
            self.update(hash, |torrent| {
                torrent.state = if torrent.finished() {
                    "stoppedUP"
                } else {
                    "stoppedDL"
                }
                .to_owned();
            });
        }
        Ok(())
    }

    fn resume(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::Resume(hashes.to_vec()))?;
        for hash in hashes {
            self.update(hash, |torrent| {
                torrent.state = if torrent.finished() {
                    "stalledUP"
                } else {
                    "downloading"
                }
                .to_owned();
            });
        }
        Ok(())
    }

    fn recheck(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::Recheck(hashes.to_vec()))?;
        if self.hold_checks {
            for hash in hashes {
                self.update(hash, |torrent| {
                    torrent.state = if torrent.finished() {
                        "checkingUP"
                    } else {
                        "checkingDL"
                    }
                    .to_owned();
                });
            }
        }
        Ok(())
    }

    fn delete(&mut self, hashes: &[String], delete_files: bool) -> QbitResult<()> {
        self.begin(QbitCall::Delete {
            hashes: hashes.to_vec(),
            delete_files,
        })?;
        for hash in hashes {
            self.truth.remove(hash);
            if delete_files {
                self.deleted_with_files.push(hash.clone());
            }
        }
        Ok(())
    }

    fn set_location(&mut self, hashes: &[String], location: &str) -> QbitResult<()> {
        self.begin(QbitCall::SetLocation {
            hashes: hashes.to_vec(),
            location: location.to_owned(),
        })?;
        for hash in hashes {
            self.update(hash, |torrent| {
                location.clone_into(&mut torrent.save_path);
                torrent.content_path = format!("{location}/{}", torrent.name);
            });
        }
        Ok(())
    }

    fn add_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::AddTags {
            hashes: hashes.to_vec(),
            tags: tags.to_vec(),
        })?;
        for hash in hashes {
            self.update(hash, |torrent| torrent.tags.extend(tags.iter().cloned()));
        }
        self.tag_names.extend(tags.iter().cloned());
        Ok(())
    }

    fn remove_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::RemoveTags {
            hashes: hashes.to_vec(),
            tags: tags.to_vec(),
        })?;
        for hash in hashes {
            self.update(hash, |torrent| {
                torrent.tags.retain(|tag| !tags.contains(tag))
            });
        }
        Ok(())
    }

    fn default_save_path(&mut self) -> QbitResult<String> {
        self.begin(QbitCall::DefaultSavePath)?;
        Ok(String::new())
    }

    fn tags(&mut self) -> QbitResult<Vec<String>> {
        self.begin(QbitCall::Tags)?;
        let mut all = self.tag_names.clone();
        all.extend(
            self.truth
                .values()
                .flat_map(|torrent| torrent.tags.iter().cloned()),
        );
        Ok(all.into_iter().collect())
    }

    fn delete_tags(&mut self, tags: &[String]) -> QbitResult<()> {
        self.begin(QbitCall::DeleteTags(tags.to_vec()))?;
        self.tag_names.retain(|tag| !tags.contains(tag));
        for torrent in self.truth.values_mut() {
            torrent.tags.retain(|tag| !tags.contains(tag));
        }
        Ok(())
    }

    fn ensure_category(&mut self, category: &str) -> QbitResult<()> {
        self.begin(QbitCall::EnsureCategory(category.to_owned()))?;
        if !category.trim().is_empty() {
            self.categories.insert(category.to_owned());
        }
        Ok(())
    }

    fn rss_feeds(&mut self) -> QbitResult<Vec<RssStoreFeed>> {
        self.begin(QbitCall::RssFeeds)?;
        Ok(self.rss.clone())
    }

    fn close(&mut self) {}
}
