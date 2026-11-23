//! Independent timers for the engine's jobs: each job runs on its own thread when it is due,
//! never twice at once, and can be poked.
//!
//! What a job decides lives in [`State`], which only ever sees the time it is given; the thread
//! that drives it just sleeps until the earliest due time.

use std::any::Any;
use std::panic::{self, AssertUnwindSafe};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use tracing::warn;

use crate::error::Result;
use crate::locked;

/// A job that failed is tried again after this long.
pub const ERROR_RETRY: Duration = Duration::from_secs(60);

/// What a job returns: the time until its next run, or `None` for its own interval.
pub type JobResult = Result<Option<Duration>>;

/// The work of a job. It runs on a thread of its own each time the job is due.
pub type JobFn = Arc<dyn Fn() -> JobResult + Send + Sync>;

/// Time that only moves forward, as a distance from some start.
pub trait Monotonic: Send + Sync {
    fn now(&self) -> Duration;
}

/// The operating system's monotonic clock.
#[derive(Debug)]
pub struct SystemMonotonic {
    start: Instant,
}

impl Default for SystemMonotonic {
    fn default() -> Self {
        Self {
            start: Instant::now(),
        }
    }
}

impl Monotonic for SystemMonotonic {
    fn now(&self) -> Duration {
        self.start.elapsed()
    }
}

/// How a job stands, for whoever shows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobStatus {
    /// When the job runs next; `None` while it waits to be poked or is running.
    pub due: Option<Duration>,
    pub running: bool,
    /// How many runs have ended.
    pub runs: u64,
    /// Why the last run failed; `None` when it did not.
    pub last_error: Option<String>,
}

struct Job {
    name: String,
    run: JobFn,
    interval: Option<Duration>,
    due: Option<Duration>,
    running: bool,
    /// When a poke that came during a run asks for the next one.
    rerun_at: Option<Duration>,
    last_error: Option<String>,
    runs: u64,
}

impl Job {
    fn status(&self) -> JobStatus {
        JobStatus {
            due: self.due,
            running: self.running,
            runs: self.runs,
            last_error: self.last_error.clone(),
        }
    }
}

/// The jobs and when each is due.
#[derive(Default)]
struct State {
    jobs: Vec<Job>,
    stopping: bool,
}

fn earliest(a: Option<Duration>, b: Duration) -> Duration {
    a.map_or(b, |a| a.min(b))
}

impl State {
    fn find(&mut self, name: &str) -> Option<&mut Job> {
        self.jobs.iter_mut().find(|job| job.name == name)
    }

    fn poke(&mut self, name: &str, at: Duration) {
        let Some(job) = self.find(name) else { return };
        if job.running {
            job.rerun_at = Some(earliest(job.rerun_at, at));
        } else {
            job.due = Some(earliest(job.due, at));
        }
    }

    /// Moves the next run to `at`, later or earlier. A run in progress still ends by setting its
    /// own next run.
    fn delay(&mut self, name: &str, at: Duration) {
        if let Some(job) = self.find(name) {
            job.due = Some(at);
        }
    }

    /// The first job that is due and not running.
    fn ready(&self, now: Duration) -> Option<usize> {
        self.jobs
            .iter()
            .position(|job| !job.running && job.due.is_some_and(|due| due <= now))
    }

    /// Marks the job as running and hands out what to run.
    fn begin(&mut self, at: usize) -> Option<(String, JobFn)> {
        let job = self.jobs.get_mut(at)?;
        job.running = true;
        job.due = None;
        Some((job.name.clone(), Arc::clone(&job.run)))
    }

    /// Takes in how a run ended and books the next one.
    fn finish(
        &mut self,
        name: &str,
        outcome: std::result::Result<Option<Duration>, String>,
        now: Duration,
    ) {
        let Some(job) = self.find(name) else { return };
        job.running = false;
        job.runs += 1;
        let wait = match outcome {
            Ok(wait) => {
                job.last_error = None;
                wait.or(job.interval)
            }
            Err(message) => {
                job.last_error = Some(message);
                Some(ERROR_RETRY)
            }
        };
        job.due = wait.map(|wait| now.saturating_add(wait));
        if let Some(rerun) = job.rerun_at.take() {
            job.due = Some(earliest(job.due, rerun));
        }
    }

    /// The earliest time a job not running is due.
    fn next_due(&self) -> Option<Duration> {
        self.jobs
            .iter()
            .filter(|job| !job.running)
            .filter_map(|job| job.due)
            .min()
    }

    fn any_running(&self) -> bool {
        self.jobs.iter().any(|job| job.running)
    }
}

type Listener = Arc<dyn Fn(&str, bool) + Send + Sync>;

struct Inner {
    clock: Arc<dyn Monotonic>,
    state: Mutex<State>,
    /// Told when a job becomes due earlier, when one ends, and when the scheduler stops.
    changed: Condvar,
    listener: Mutex<Option<Listener>>,
}

/// Runs each registered job when it is due. Cloning gives another handle to the same jobs.
#[derive(Clone)]
pub struct Scheduler {
    inner: Arc<Inner>,
    driver: Arc<Mutex<Option<JoinHandle<()>>>>,
}

impl Default for Scheduler {
    fn default() -> Self {
        Self::new(Arc::<SystemMonotonic>::default())
    }
}

impl Scheduler {
    pub fn new(clock: Arc<dyn Monotonic>) -> Self {
        Self {
            inner: Arc::new(Inner {
                clock,
                state: Mutex::new(State::default()),
                changed: Condvar::new(),
                listener: Mutex::new(None),
            }),
            driver: Arc::new(Mutex::new(None)),
        }
    }

    /// The scheduler's time.
    pub fn now(&self) -> Duration {
        self.inner.clock.now()
    }

    /// Tells `listener` the name of every job that starts (`true`) or ends (`false`).
    pub fn on_change(&self, listener: impl Fn(&str, bool) + Send + Sync + 'static) {
        *locked(&self.inner.listener) = Some(Arc::new(listener));
    }

    /// Registers a job. `first` is the delay before its first run (`None`: only when poked); a
    /// job with no `interval` runs only when poked or given a time by its own result.
    pub fn add(&self, name: &str, run: JobFn, interval: Option<Duration>, first: Option<Duration>) {
        let due = first.map(|first| self.now().saturating_add(first));
        let mut state = locked(&self.inner.state);
        state.jobs.push(Job {
            name: name.to_owned(),
            run,
            interval,
            due,
            running: false,
            rerun_at: None,
            last_error: None,
            runs: 0,
        });
        self.inner.changed.notify_all();
    }

    /// Runs the job `delay` from now. A poke during a run makes it run once more afterwards.
    pub fn poke(&self, name: &str, delay: Duration) {
        let at = self.now().saturating_add(delay);
        locked(&self.inner.state).poke(name, at);
        self.inner.changed.notify_all();
    }

    /// Moves the job's next run to `by` from now, later or earlier.
    pub fn delay(&self, name: &str, by: Duration) {
        let at = self.now().saturating_add(by);
        locked(&self.inner.state).delay(name, at);
        self.inner.changed.notify_all();
    }

    /// Sets when the job runs next and why its last run failed, as if it had run.
    #[cfg(feature = "testing")]
    pub fn seed(&self, name: &str, due: Option<Duration>, last_error: Option<String>) {
        let mut state = locked(&self.inner.state);
        if let Some(job) = state.find(name) {
            job.due = due;
            job.last_error = last_error;
        }
    }

    pub fn status(&self, name: &str) -> Option<JobStatus> {
        let state = locked(&self.inner.state);
        state
            .jobs
            .iter()
            .find(|job| job.name == name)
            .map(Job::status)
    }

    /// How every registered job stands, in the order they were registered.
    pub fn statuses(&self) -> Vec<(String, JobStatus)> {
        locked(&self.inner.state)
            .jobs
            .iter()
            .map(|job| (job.name.clone(), job.status()))
            .collect()
    }

    /// Whether the jobs are being run as they fall due.
    pub fn is_started(&self) -> bool {
        locked(&self.driver).is_some()
    }

    /// Starts running the jobs as they fall due. Does nothing when already started.
    pub fn start(&self) {
        let mut driver = locked(&self.driver);
        if driver.is_some() {
            return;
        }
        locked(&self.inner.state).stopping = false;
        let inner = Arc::clone(&self.inner);
        match thread::Builder::new()
            .name("kisetsu-scheduler".into())
            .spawn(move || drive(&inner))
        {
            Ok(handle) => *driver = Some(handle),
            Err(error) => warn!("The scheduler could not start: {error}"),
        }
    }

    /// Stops starting jobs, then waits up to `grace` for the ones running to end. Returns the
    /// names of those still running; their threads are left to end on their own.
    pub fn stop(&self, grace: Duration) -> Vec<String> {
        locked(&self.inner.state).stopping = true;
        self.inner.changed.notify_all();
        if let Some(driver) = locked(&self.driver).take() {
            let _ = driver.join();
        }
        let deadline = Instant::now() + grace;
        let mut state = locked(&self.inner.state);
        while state.any_running() {
            let Some(left) = deadline.checked_duration_since(Instant::now()) else {
                break;
            };
            state = self
                .inner
                .changed
                .wait_timeout(state, left)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
        state
            .jobs
            .iter()
            .filter(|job| job.running)
            .map(|job| job.name.clone())
            .collect()
    }

    /// Drops the registered jobs, which end any cycle through the objects they hold.
    pub fn clear(&self) {
        locked(&self.inner.state).jobs.clear();
    }
}

fn drive(inner: &Arc<Inner>) {
    let mut state = locked(&inner.state);
    while !state.stopping {
        let now = inner.clock.now();
        if let Some(at) = state.ready(now) {
            if let Some((name, run)) = state.begin(at) {
                if let Err(error) = launch(inner, name.clone(), run) {
                    let message = format!("the job could not start: {error}");
                    state.finish(&name, Err(message), now);
                }
            }
            continue;
        }
        state = match state.next_due() {
            None => inner
                .changed
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
            Some(due) => {
                inner
                    .changed
                    .wait_timeout(state, due.saturating_sub(now))
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .0
            }
        };
    }
}

/// Runs a job on a thread of its own and books its end.
fn launch(inner: &Arc<Inner>, name: String, run: JobFn) -> std::io::Result<()> {
    let inner = Arc::clone(inner);
    thread::Builder::new()
        .name(format!("kisetsu-job-{name}"))
        .spawn(move || {
            tell(&inner, &name, true);
            let outcome = match panic::catch_unwind(AssertUnwindSafe(|| run())) {
                Ok(Ok(wait)) => Ok(wait),
                Ok(Err(error)) => {
                    warn!("Job '{name}' failed: {error}");
                    Err(error.to_string())
                }
                Err(panic) => {
                    let message = panic_message(&*panic);
                    warn!("Job '{name}' failed: {message}");
                    Err(message)
                }
            };
            locked(&inner.state).finish(&name, outcome, inner.clock.now());
            inner.changed.notify_all();
            tell(&inner, &name, false);
        })
        .map(drop)
}

fn tell(inner: &Inner, name: &str, running: bool) {
    let listener = locked(&inner.listener).clone();
    if let Some(listener) = listener {
        listener(name, running);
    }
}

/// What a panic said, for the log.
pub(crate) fn panic_message(panic: &(dyn Any + Send)) -> String {
    panic
        .downcast_ref::<&str>()
        .map(|text| (*text).to_owned())
        .or_else(|| panic.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "the work stopped unexpectedly".to_owned())
}

#[cfg(test)]
mod tests;
