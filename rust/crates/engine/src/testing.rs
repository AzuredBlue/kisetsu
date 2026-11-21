//! Stand-ins for the tests of this crate.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};

use kisetsu_clients::qbittorrent::{
    AddTorrent, Qbit, QbitResult, RssStoreFeed, SyncResult, Torrent, Versions,
};
use kisetsu_services::fakes::FakeQbit;

pub fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A fake qBittorrent the test keeps a hand on while the engine owns the client: both lock the
/// same [`FakeQbit`].
pub struct SharedQbit {
    fake: Arc<Mutex<FakeQbit>>,
    view: BTreeMap<String, Torrent>,
}

impl SharedQbit {
    pub fn new(fake: &Arc<Mutex<FakeQbit>>) -> Self {
        Self {
            fake: Arc::clone(fake),
            view: BTreeMap::new(),
        }
    }

    fn with<R>(&mut self, call: impl FnOnce(&mut FakeQbit) -> R) -> R {
        let mut fake = locked(&self.fake);
        let answer = call(&mut fake);
        self.view.clone_from(fake.torrents());
        answer
    }
}

impl Qbit for SharedQbit {
    fn versions(&mut self) -> QbitResult<Versions> {
        self.with(|fake| fake.versions())
    }

    fn sync(&mut self, full: bool) -> QbitResult<SyncResult> {
        self.with(|fake| fake.sync(full))
    }

    fn torrents(&self) -> &BTreeMap<String, Torrent> {
        &self.view
    }

    fn synced(&self) -> bool {
        locked(&self.fake).synced()
    }

    fn files(&mut self, info_hash: &str) -> QbitResult<Vec<String>> {
        self.with(|fake| fake.files(info_hash))
    }

    fn add_torrent(&mut self, add: &AddTorrent) -> QbitResult<()> {
        self.with(|fake| fake.add_torrent(add))
    }

    fn pause(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.pause(hashes))
    }

    fn resume(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.resume(hashes))
    }

    fn recheck(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.recheck(hashes))
    }

    fn delete(&mut self, hashes: &[String], delete_files: bool) -> QbitResult<()> {
        self.with(|fake| fake.delete(hashes, delete_files))
    }

    fn set_location(&mut self, hashes: &[String], location: &str) -> QbitResult<()> {
        self.with(|fake| fake.set_location(hashes, location))
    }

    fn add_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.add_tags(hashes, tags))
    }

    fn remove_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.remove_tags(hashes, tags))
    }

    fn default_save_path(&mut self) -> QbitResult<String> {
        self.with(|fake| fake.default_save_path())
    }

    fn tags(&mut self) -> QbitResult<Vec<String>> {
        self.with(|fake| fake.tags())
    }

    fn delete_tags(&mut self, tags: &[String]) -> QbitResult<()> {
        self.with(|fake| fake.delete_tags(tags))
    }

    fn ensure_category(&mut self, category: &str) -> QbitResult<()> {
        self.with(|fake| fake.ensure_category(category))
    }

    fn rss_feeds(&mut self) -> QbitResult<Vec<RssStoreFeed>> {
        self.with(|fake| fake.rss_feeds())
    }

    fn close(&mut self) {
        self.with(|fake| fake.close());
    }
}
