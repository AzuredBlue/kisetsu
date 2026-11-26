//! The engine's jobs. Network work happens on the thread of the job; every database or
//! qBittorrent step goes through [`Engine::call`].

mod addshow;
mod anilist;
mod backfill;
mod downloads;
mod feeds;
mod startup;
mod upkeep;

use std::collections::{BTreeSet, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::qbittorrent::QbitError;
use kisetsu_clients::rss::Backoff;
use kisetsu_clients::sites::Grouping;
use kisetsu_clients::traffic;
use kisetsu_db::schema::Settings;
use kisetsu_services::Error as ServiceError;
use kisetsu_services::context::setup_completed;
use tracing::{info, warn};

use crate::error::{Error, Result};
use crate::locked;
use crate::remote::Remote;
use crate::runner::Engine;
use crate::scheduler::{JobFn, JobResult, Scheduler};

const QBIT_RETRY: Duration = Duration::from_secs(30);
const ANILIST_ERROR_RETRY: Duration = Duration::from_secs(300);
const SETTLE_BUSY: Duration = Duration::from_secs(4);
const SETTLE_ACTIVE: Duration = Duration::from_secs(60);
const SETTLE_IDLE: Duration = Duration::from_secs(300);
const GRAB_FALLBACK: Duration = Duration::from_secs(15 * 60);
const BACKFILL_INTERVAL: Duration = Duration::from_secs(6 * 3600);
/// How many shows are searched for alone, one after another, to check a site before the others
/// share their searches.
const SITE_CHECK_SEARCHES: usize = 3;
/// Cover images read at the same time (AniList's image server).
const PALETTE_CONCURRENCY: usize = 4;
/// Shows searched for past releases at the same time; each site still gets one request at a time.
const SEARCH_CONCURRENCY: usize = 4;
const HOUSEKEEPING_INTERVAL: Duration = Duration::from_secs(24 * 3600);
/// Well before a whole interval, or a machine restarted every day would never get to it.
const HOUSEKEEPING_FIRST: Duration = Duration::from_secs(3600);
const HUNT_IDLE_MAX: Duration = Duration::from_secs(3600);
/// The list the setup check fetched is taken by the sync that follows within this time, instead of
/// asking again.
pub const LIST_REUSE_SECONDS: f64 = 120.0;
/// A burst of pokes (a search finishing every second or so) runs the job at most once in this
/// time; a lone poke runs it at once.
const BURST_GAP: Duration = Duration::from_secs(1);
/// SubsPlease's schedule of the shows it airs is read again for a past-release search after this
/// long.
const SCHEDULE_KEEP: SignedDuration = SignedDuration::from_hours(1);
/// Where what the searches learned of each site (`Grouping`) is kept across restarts.
const GROUPING_KEY: &str = "search_grouping";
/// With no AniList user set there is nothing to sync until the settings change.
const NO_USER_WAIT: Duration = Duration::from_secs(3600);
/// Until the first-run wizard is done the stored qBittorrent login is still the default one.
const SETUP_WAIT: Duration = Duration::from_secs(3600);
/// The least a deferral or a gap between syncs is waited out.
const SYNC_RETRY_FLOOR: Duration = Duration::from_secs(30);
/// A feed read the grab asks for after a backfill finds gaps waits this long, so the startup
/// searches are not repeated beside it.
const GAP_BACKFILL_DELAY: Duration = Duration::from_secs(5);
/// The shortest wait the hunt takes between looks.
const HUNT_FLOOR: Duration = Duration::from_secs(5);
/// While the machine is offline, how often the jobs that need the network look again.
const NETWORK_RETRY: Duration = Duration::from_secs(15);

/// How long the running jobs get to end when they are held.
const STOP_GRACE: Duration = Duration::from_secs(30);

/// What is wrong with qBittorrent, AniList or the grab, for the UI's status.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Health {
    pub qbit: Option<String>,
    pub anilist: Option<String>,
    /// Seconds until AniList is asked again.
    pub anilist_retry_in: Option<f64>,
    pub grab: Option<String>,
}

/// How far the searches before the first-run review got: shows searched (or with nothing to search
/// for) out of the followed shows.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SetupProgress {
    pub searched: usize,
    pub total: usize,
    /// Set once startup's searches are over, however they ended.
    pub done: bool,
}

/// What the jobs remember between runs.
struct State {
    health: Health,
    backoff: Backoff<i64>,
    backfill_requests: BTreeSet<i64>,
    backfill_all: bool,
    refresh_all: bool,
    /// When the hunt will next read a feed (`None`: nothing is due to air), for the UI's countdown.
    hunt_read_at: Option<Timestamp>,
    /// Set once startup found qBittorrent away and handed AniList and the feeds to their own timers.
    offline_started: bool,
    /// Set while the machine was last found offline.
    network_down: bool,
    /// When `poke_soon` last let each job run, by the scheduler's time.
    poked_at: HashMap<&'static str, Duration>,
    /// SubsPlease's schedule: the titles, and when they were read.
    schedule: Option<Vec<String>>,
    schedule_at: Option<Timestamp>,
    /// What of the grouping is stored; `None` until it is read back.
    grouping_kept: Option<String>,
    /// Only while the first-run setup waits for the user to confirm its first downloads.
    setup_progress: Option<SetupProgress>,
}

/// What the jobs remember, for a test to set.
#[cfg(feature = "testing")]
#[derive(Debug, Clone, Default)]
pub struct Memory {
    pub health: Health,
    pub backfill_requests: BTreeSet<i64>,
    pub backfill_all: bool,
    pub refresh_all: bool,
    pub hunt_read_at: Option<Timestamp>,
    pub ready: bool,
    pub synced: bool,
}

pub struct Jobs {
    engine: Arc<Engine>,
    scheduler: Scheduler,
    remote: Arc<dyn Remote>,
    /// Set once adoption has seen what qBittorrent holds, so grabbing can begin.
    ready: AtomicBool,
    /// Set once the first full qBittorrent sync is in. Downloads can be followed from then on (but
    /// not added to, deleted or forgotten: see `settle`); grabbing waits for `ready`.
    synced: AtomicBool,
    state: Mutex<State>,
    /// What the searches learned of each site, shared by every search.
    grouping: Grouping,
    /// How many results the feeds' pages held, waiting for the next search to take them in, so
    /// that reading a feed never waits for a search.
    page_sizes: Mutex<Vec<(String, u64)>>,
    /// What the searches for shows being added found, by AniList id and when, and the file lists
    /// of their batches.
    found: Mutex<
        HashMap<
            i64,
            (
                std::time::Instant,
                addshow::Found,
                kisetsu_services::addshow::TorrentFiles,
            ),
        >,
    >,
    /// A direct call (startup, a new show, Search older) and the same job must never run at once.
    feeds_lock: Mutex<()>,
    anilist_lock: Mutex<()>,
    search_locks: Mutex<HashMap<i64, Arc<Mutex<()>>>>,
}

/// A wait of `seconds`; a negative or unusable number is no wait.
fn secs(seconds: f64) -> Duration {
    Duration::try_from_secs_f64(seconds).unwrap_or(Duration::ZERO)
}

/// The wait until `at`; none when `at` has passed.
fn until(now: Timestamp, at: Timestamp) -> Duration {
    Duration::try_from(at.duration_since(now)).unwrap_or(Duration::ZERO)
}

fn later(from: Timestamp, wait: Duration) -> Timestamp {
    SignedDuration::try_from(wait)
        .ok()
        .and_then(|wait| from.saturating_add(wait).ok())
        .unwrap_or(from)
}

fn wall_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

/// The qBittorrent error a failed engine call is made of, when it is one.
fn qbit_error(error: &Error) -> Option<&QbitError> {
    match error {
        Error::Service(ServiceError::Qbit(error)) => Some(error),
        _ => None,
    }
}

impl Jobs {
    pub fn new(engine: Arc<Engine>, scheduler: Scheduler, remote: Arc<dyn Remote>) -> Arc<Self> {
        Arc::new(Self {
            engine,
            scheduler,
            remote,
            ready: AtomicBool::new(false),
            synced: AtomicBool::new(false),
            state: Mutex::new(State {
                health: Health::default(),
                backoff: Backoff::default(),
                backfill_requests: BTreeSet::new(),
                backfill_all: false,
                refresh_all: false,
                hunt_read_at: None,
                offline_started: false,
                network_down: false,
                poked_at: HashMap::new(),
                schedule: None,
                schedule_at: None,
                grouping_kept: None,
                setup_progress: None,
            }),
            grouping: Grouping::new(Box::new(wall_seconds)),
            page_sizes: Mutex::new(Vec::new()),
            found: Mutex::new(HashMap::new()),
            feeds_lock: Mutex::new(()),
            anilist_lock: Mutex::new(()),
            search_locks: Mutex::new(HashMap::new()),
        })
    }

    // -- wiring --

    /// Hands every job to the scheduler.
    pub fn register(self: &Arc<Self>) {
        let add = |name: &'static str,
                   run: fn(&Self) -> JobResult,
                   interval: Option<Duration>,
                   first: Option<Duration>| {
            let jobs = Arc::clone(self);
            let job: JobFn = Arc::new(move || {
                let _caller = traffic::caller(&format!("jobs.{name}"));
                run(&jobs)
            });
            self.scheduler.add(name, job, interval, first);
        };
        let feed_refresh = Settings::default().feed_refresh_minutes;
        add("startup", Self::startup, None, Some(Duration::ZERO));
        add(
            "refresh_feeds",
            |jobs| jobs.refresh_feeds(false),
            Some(Duration::from_secs(
                u64::try_from(feed_refresh).unwrap_or(0) * 60,
            )),
            None,
        );
        add("hunt", Self::hunt, None, None);
        add(
            "sync_anilist",
            |jobs| jobs.sync_anilist(false).map(Some),
            Some(NO_USER_WAIT),
            None,
        );
        add("grab", Self::grab, Some(GRAB_FALLBACK), None);
        add("settle", Self::settle, Some(SETTLE_IDLE), None);
        add("backfill", Self::backfill, Some(BACKFILL_INTERVAL), None);
        add("palette", Self::palette, None, None);
        add(
            "housekeeping",
            Self::housekeeping,
            Some(HOUSEKEEPING_INTERVAL),
            Some(HOUSEKEEPING_FIRST),
        );
    }

    /// Holds every job while `work` replaces the database, since a job keeps ids between its
    /// steps, then starts them over as after a restart.
    ///
    /// A job still running after the grace cannot be held: the jobs start again as they were, the
    /// database is left alone and the error names the jobs.
    pub fn while_stopped<R>(self: &Arc<Self>, work: impl FnOnce() -> Result<R>) -> Result<R> {
        self.while_stopped_within(STOP_GRACE, work)
    }

    fn while_stopped_within<R>(
        self: &Arc<Self>,
        grace: Duration,
        work: impl FnOnce() -> Result<R>,
    ) -> Result<R> {
        let was_running = self.scheduler.is_started();
        let busy = self.scheduler.stop(grace);
        if !busy.is_empty() {
            warn!(
                "Jobs still running, so the database is not replaced: {}",
                busy.join(", ")
            );
            if was_running {
                self.scheduler.start();
            }
            return Err(Error::JobsRunning(busy));
        }
        let result = work();
        self.ready.store(false, Ordering::Release);
        self.synced.store(false, Ordering::Release);
        {
            let mut state = locked(&self.state);
            state.backoff = Backoff::default();
            state.backfill_requests.clear();
            state.backfill_all = false;
            state.refresh_all = false;
            state.hunt_read_at = None;
            state.offline_started = false;
            state.network_down = false;
            state.health = Health::default();
        }
        if was_running {
            self.scheduler.clear();
            self.register();
            self.scheduler.start();
        }
        self.publish_status();
        result
    }

    /// Runs the job soon.
    pub fn poke(&self, name: &str, delay: Duration) {
        self.scheduler.poke(name, delay);
    }

    fn poke_now(&self, names: &[&str]) {
        for name in names {
            self.scheduler.poke(name, Duration::ZERO);
        }
    }

    /// Pokes the job now, or `gap` after the run the last such poke asked for, so a burst makes
    /// one run in that time. A poke while that run is still to come asks for it again (in case it
    /// ran early on another poke), not for a later one.
    fn poke_soon(&self, name: &'static str, gap: Duration) {
        let now = self.scheduler.now();
        let at = {
            let mut state = locked(&self.state);
            let at = match state.poked_at.get(name) {
                None => now,
                Some(&last) if last > now => last,
                Some(&last) => now.max(last.saturating_add(gap)),
            };
            state.poked_at.insert(name, at);
            at
        };
        self.scheduler.poke(name, at.saturating_sub(now));
    }

    /// Replaces what the jobs remember.
    #[cfg(feature = "testing")]
    pub fn remember(&self, memory: Memory) {
        {
            let mut state = locked(&self.state);
            state.health = memory.health;
            state.backfill_requests = memory.backfill_requests;
            state.backfill_all = memory.backfill_all;
            state.refresh_all = memory.refresh_all;
            state.hunt_read_at = memory.hunt_read_at;
        }
        self.ready.store(memory.ready, Ordering::Release);
        self.synced.store(memory.synced, Ordering::Release);
    }

    // -- what the UI shows --

    pub fn health(&self) -> Health {
        locked(&self.state).health.clone()
    }

    /// When the hunt will next read a feed; `None` when nothing is due to air.
    pub fn hunt_read_at(&self) -> Option<Timestamp> {
        locked(&self.state).hunt_read_at
    }

    /// Whether startup has seen what qBittorrent holds, so grabbing has begun.
    pub fn is_ready(&self) -> bool {
        self.ready.load(Ordering::Acquire)
    }

    pub fn setup_progress(&self) -> Option<SetupProgress> {
        locked(&self.state).setup_progress
    }

    fn publish_status(&self) {
        self.engine.bus().publish(["status"]);
    }

    /// Notes a problem with qBittorrent, or that it is gone; a new problem is logged.
    fn set_qbit(&self, problem: Option<String>) {
        let changed = {
            let mut state = locked(&self.state);
            if state.health.qbit == problem {
                false
            } else {
                match &problem {
                    Some(text) => warn!("qBittorrent: {text}"),
                    None => info!("qBittorrent connection restored."),
                }
                state.health.qbit = problem;
                true
            }
        };
        if changed {
            self.publish_status();
        }
    }

    fn set_anilist(&self, problem: Option<String>, retry_in: Option<f64>) {
        let changed = {
            let mut state = locked(&self.state);
            let mut changed = false;
            if state.health.anilist != problem {
                if let Some(text) = &problem {
                    warn!("AniList: {text}");
                }
                state.health.anilist = problem;
                changed = true;
            }
            if state.health.anilist_retry_in != retry_in {
                state.health.anilist_retry_in = retry_in;
                changed = true;
            }
            changed
        };
        if changed {
            self.publish_status();
        }
    }

    fn set_grab(&self, problem: Option<String>) {
        let changed = {
            let mut state = locked(&self.state);
            if state.health.grab == problem {
                false
            } else {
                if let Some(text) = &problem {
                    warn!("Grab: {text}");
                }
                state.health.grab = problem;
                true
            }
        };
        if changed {
            self.publish_status();
        }
    }

    /// Notes that qBittorrent could not be reached and says when to try again.
    fn qbit_failed(&self, error: &QbitError) -> Option<Duration> {
        self.set_qbit(Some(error.to_string()));
        Some(QBIT_RETRY)
    }

    /// Whether the machine is online. It is asked when `check` is set or it was last found
    /// offline; otherwise it is taken to be online. A change is logged.
    fn online(&self, check: bool) -> bool {
        if !check && !locked(&self.state).network_down {
            return true;
        }
        let online = self.remote.online();
        let changed = {
            let mut state = locked(&self.state);
            std::mem::replace(&mut state.network_down, !online) == online
        };
        if changed {
            if online {
                info!("Network connection is up.");
            } else {
                info!("Waiting for a network connection.");
            }
        }
        online
    }

    fn configured(&self) -> Result<bool> {
        self.engine.read(|ctx| setup_completed(ctx.conn))
    }
}

pub use addshow::NewShowSearch;

#[cfg(test)]
mod tests;
