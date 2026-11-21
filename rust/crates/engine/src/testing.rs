//! Stand-ins for the tests of this crate.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::Duration;

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::anilist::{AniListError, ListEntry, Schedules};
use kisetsu_clients::palette::{Hues, ImageError};
use kisetsu_clients::qbittorrent::{
    AddTorrent, Qbit, QbitResult, RssStoreFeed, SyncResult, Torrent, Versions,
};
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use kisetsu_db::schema::{Episode, Settings, Show};
use kisetsu_db::storage::StoredEnum;
use kisetsu_services::context::{AniListSource, FeedSource, SETUP_KEY, set_state};
use kisetsu_services::fakes::{FakeAniList, FakeClock, FakeFeeds, FakeQbit};
use kisetsu_services::feeds;

use crate::bus::ChangeBus;
use crate::jobs::Jobs;
use crate::remote::Remote;
use crate::runner::{Engine, Parts};
use crate::scheduler::{Monotonic, Scheduler};

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

/// A clock the test moves.
#[derive(Debug, Default)]
pub struct ManualMonotonic(AtomicU64);

impl ManualMonotonic {
    pub fn advance(&self, by: Duration) {
        self.0.fetch_add(
            u64::try_from(by.as_millis()).unwrap_or(u64::MAX),
            Ordering::SeqCst,
        );
    }
}

impl Monotonic for ManualMonotonic {
    fn now(&self) -> Duration {
        Duration::from_millis(self.0.load(Ordering::SeqCst))
    }
}

/// The outside world, scripted: the feeds and AniList are the services' fakes behind a lock.
pub struct FakeRemote {
    pub feeds: Mutex<FakeFeeds>,
    pub anilist: Mutex<FakeAniList>,
    /// The titles SubsPlease's schedule lists.
    pub schedule: Mutex<Option<Vec<String>>>,
    pub schedule_reads: AtomicUsize,
    /// The banners whose colours were asked for.
    pub hue_calls: Mutex<Vec<String>>,
    pub hues_fail: AtomicBool,
    /// How long reading a feed takes.
    pub read_time: Mutex<Duration>,
    /// What the next asks for whole schedules fail with, in turn, before they are answered.
    pub media_deferrals: Mutex<VecDeque<AniListError>>,
    /// The shows each ask for whole schedules named.
    pub media_calls: Mutex<Vec<Vec<i64>>>,
    /// Makes each search wait until this many are in flight, or give up after a few seconds.
    pub search_meeting: AtomicUsize,
    /// Whether every search answers that the site is rate limiting.
    pub search_rate_limited: AtomicBool,
    searches: Mutex<SearchCount>,
    searches_changed: Condvar,
    reading: AtomicUsize,
    pub most_reading: AtomicUsize,
}

/// How many searches are in flight, how many were at once at most, and how many began.
#[derive(Default)]
struct SearchCount {
    now: usize,
    most: usize,
    begun: usize,
    /// Set once `search_meeting` searches were in flight together; later searches pass at once.
    met: bool,
}

impl FakeRemote {
    /// The most searches that were in flight at once.
    pub fn most_searching(&self) -> usize {
        locked(&self.searches).most
    }

    /// How many searches began.
    pub fn searches_begun(&self) -> usize {
        locked(&self.searches).begun
    }

    /// Counts a search as in flight; with `search_meeting` set it waits for the others.
    fn meet_other_searches(&self) {
        let meeting = self.search_meeting.load(Ordering::SeqCst);
        let mut count = locked(&self.searches);
        count.now += 1;
        count.begun += 1;
        count.most = count.most.max(count.now);
        count.met |= meeting > 0 && count.now >= meeting;
        self.searches_changed.notify_all();
        if meeting > 0 {
            count = self
                .searches_changed
                .wait_timeout_while(count, Duration::from_secs(5), |count| !count.met)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
        count.now -= 1;
    }

    pub fn new(clock: FakeClock) -> Self {
        Self {
            feeds: Mutex::new(FakeFeeds::default()),
            anilist: Mutex::new(FakeAniList::new(clock)),
            schedule: Mutex::new(None),
            schedule_reads: AtomicUsize::new(0),
            hue_calls: Mutex::new(Vec::new()),
            hues_fail: AtomicBool::new(false),
            read_time: Mutex::new(Duration::ZERO),
            media_deferrals: Mutex::new(VecDeque::new()),
            media_calls: Mutex::new(Vec::new()),
            search_meeting: AtomicUsize::new(0),
            search_rate_limited: AtomicBool::new(false),
            searches: Mutex::new(SearchCount::default()),
            searches_changed: Condvar::new(),
            reading: AtomicUsize::new(0),
            most_reading: AtomicUsize::new(0),
        }
    }
}

impl Remote for FakeRemote {
    fn fetch_feed(&self, url: &str) -> Result<Vec<FeedArticle>, FeedFetchError> {
        let now = self.reading.fetch_add(1, Ordering::SeqCst) + 1;
        self.most_reading.fetch_max(now, Ordering::SeqCst);
        let time = *locked(&self.read_time);
        if !time.is_zero() {
            thread::sleep(time);
        }
        let answer = locked(&self.feeds).fetch(url);
        self.reading.fetch_sub(1, Ordering::SeqCst);
        answer
    }

    fn search(
        &self,
        feed_url: &str,
        term: &str,
        _now: Timestamp,
    ) -> Result<Option<Vec<FeedArticle>>, FeedFetchError> {
        self.meet_other_searches();
        if self.search_rate_limited.load(Ordering::SeqCst) {
            return Err(FeedFetchError::RateLimited {
                host: feed_url.to_owned(),
            });
        }
        locked(&self.feeds).search(feed_url, term)
    }

    fn subsplease_schedule(&self) -> Result<Option<Vec<String>>, FeedFetchError> {
        self.schedule_reads.fetch_add(1, Ordering::SeqCst);
        Ok(locked(&self.schedule).clone())
    }

    fn hues(&self, banner: &str, _cover: &str) -> Result<Option<Hues>, ImageError> {
        locked(&self.hue_calls).push(banner.to_owned());
        if self.hues_fail.load(Ordering::SeqCst) {
            return Err(ImageError::Unreachable {
                host: "images.test".to_owned(),
                detail: "no answer".to_owned(),
            });
        }
        Ok(None)
    }

    fn seasonal(
        &self,
        username: &str,
        monitored: &HashSet<i64>,
        reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        locked(&self.anilist).fetch_user_seasonal_anime(username, monitored, reuse_within)
    }

    fn user_exists(&self, username: &str) -> Result<bool, AniListError> {
        locked(&self.anilist).user_exists(username)
    }

    fn user_list(&self) -> Vec<ListEntry> {
        locked(&self.anilist).user_list()
    }

    fn user_list_at(&self) -> Option<Timestamp> {
        locked(&self.anilist).user_list_at()
    }

    fn anilist_blocked_until(&self) -> Option<f64> {
        None
    }

    fn upcoming_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> Result<Schedules, AniListError> {
        locked(&self.anilist).fetch_upcoming_airing_schedules(media_ids, since)
    }

    fn media_schedules(&self, media_ids: &[i64]) -> Result<Schedules, AniListError> {
        locked(&self.media_calls).push(media_ids.to_vec());
        if let Some(error) = locked(&self.media_deferrals).pop_front() {
            return Err(error);
        }
        locked(&self.anilist).fetch_media_airing_schedules(media_ids)
    }
}

/// A real engine on a database file, with the fakes behind every edge, and the jobs on a
/// scheduler that is never started: the tests run the jobs themselves and look at what they asked
/// the scheduler for.
pub struct Env {
    pub engine: Arc<Engine>,
    pub clock: FakeClock,
    pub qbit: Arc<Mutex<FakeQbit>>,
    pub remote: Arc<FakeRemote>,
    pub ticks: Arc<ManualMonotonic>,
    pub scheduler: Scheduler,
    pub jobs: Arc<Jobs>,
    _folder: tempfile::TempDir,
}

impl Env {
    /// The app after its first-run setup: an AniList user and a download category.
    pub fn new() -> Self {
        let folder = tempfile::tempdir().unwrap();
        let clock = FakeClock::default();
        let qbit = Arc::new(Mutex::new(FakeQbit::new(clock.clone())));
        let shared = Arc::clone(&qbit);
        let mut parts = Parts::new(Box::new(move |_| Box::new(SharedQbit::new(&shared))));
        parts.clock = Arc::new(clock.clone());
        parts.bus = ChangeBus::new(Duration::ZERO);
        let engine = Arc::new(Engine::start(&folder.path().join("kisetsu.db"), parts).unwrap());
        let remote = Arc::new(FakeRemote::new(clock.clone()));
        let ticks = Arc::new(ManualMonotonic::default());
        let scheduler = Scheduler::new(ticks.clone());
        let jobs = Jobs::new(Arc::clone(&engine), scheduler.clone(), remote.clone());
        jobs.register();
        let env = Self {
            engine,
            clock,
            qbit,
            remote,
            ticks,
            scheduler,
            jobs,
            _folder: folder,
        };
        env.change_settings(|settings| {
            "user".clone_into(&mut settings.anilist_username);
            "Seasonal".clone_into(&mut settings.default_category);
        });
        env.engine
            .call(|ctx| set_state(ctx.conn, SETUP_KEY, "1"))
            .unwrap();
        env
    }

    pub fn change_settings(&self, change: impl FnOnce(&mut Settings) + Send + 'static) {
        self.engine
            .call(move |ctx| {
                change(&mut ctx.settings);
                ctx.settings.update(ctx.conn)?;
                Ok(())
            })
            .unwrap();
    }

    pub fn add_feed(&self, name: &str, url: &str) -> i64 {
        let (name, url) = (name.to_owned(), url.to_owned());
        self.engine
            .call(move |ctx| {
                Ok(feeds::add_feed(ctx, &name, &url, None)?
                    .id
                    .unwrap_or_default())
            })
            .unwrap()
    }

    pub fn show(&self, anilist_id: i64) -> Show {
        self.engine
            .read(|ctx| Ok(Show::find(ctx.conn, "WHERE anilist_id = ?", [anilist_id])?))
            .unwrap()
            .unwrap()
    }

    /// The status name of each episode of the show, by number.
    pub fn statuses(&self, anilist_id: i64) -> BTreeMap<i64, &'static str> {
        let show = self.show(anilist_id);
        self.engine
            .read(move |ctx| {
                Ok(
                    Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY number", [show.id])?
                        .into_iter()
                        .map(|episode| (episode.number, episode.status.name()))
                        .collect(),
                )
            })
            .unwrap()
    }

    /// How long until the scheduler runs the job, `None` while it waits to be poked.
    pub fn due_in(&self, name: &str) -> Option<Duration> {
        let now = self.scheduler.now();
        self.scheduler
            .status(name)
            .and_then(|status| status.due)
            .map(|due| due.saturating_sub(now))
    }

    /// Puts the job's due time back, as if nothing had asked for it.
    pub fn forget_pokes(&self) {
        for name in JOBS {
            self.scheduler.delay(name, Duration::from_secs(1_000_000));
        }
    }

    pub fn advance(&self, by: SignedDuration) {
        self.clock.advance(by);
    }

    pub fn qbit(&self) -> MutexGuard<'_, FakeQbit> {
        locked(&self.qbit)
    }

    pub fn feeds(&self) -> MutexGuard<'_, FakeFeeds> {
        locked(&self.remote.feeds)
    }

    pub fn anilist(&self) -> MutexGuard<'_, FakeAniList> {
        locked(&self.remote.anilist)
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        self.engine.close();
    }
}

const JOBS: [&str; 9] = [
    "startup",
    "refresh_feeds",
    "hunt",
    "sync_anilist",
    "grab",
    "settle",
    "backfill",
    "palette",
    "housekeeping",
];
