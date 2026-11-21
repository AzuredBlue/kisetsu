//! Tells the UI which of its views changed. Publishing is safe from any thread.

use std::collections::{BTreeSet, VecDeque};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use tracing::warn;

/// How many changes a slow subscriber may lag behind before it is told to refetch everything.
pub const SUBSCRIBER_BACKLOG: usize = 64;
/// Changes published this close together reach the UI as one message, so a burst (a colour stored
/// for every show, say) makes it refetch once instead of once per change.
pub const BATCH: Duration = Duration::from_millis(250);

/// The topic that tells a subscriber to refetch everything.
pub const EVERYTHING: &str = "*";

/// What changed, and which message this is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    pub changed: Vec<String>,
    pub rev: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum RecvError {
    #[error("nothing changed in time")]
    Timeout,
    /// The bus shut down; nothing follows.
    #[error("the change bus shut down")]
    Ended,
}

#[derive(Default)]
struct QueueState {
    items: VecDeque<Change>,
    ended: bool,
}

#[derive(Default)]
struct Queue {
    state: Mutex<QueueState>,
    ready: Condvar,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl Queue {
    fn push(&self, change: Change) {
        let mut state = locked(&self.state);
        if state.items.len() >= SUBSCRIBER_BACKLOG {
            state.items.clear();
            state.items.push_back(Change {
                changed: vec![EVERYTHING.to_owned()],
                rev: change.rev,
            });
        } else {
            state.items.push_back(change);
        }
        self.ready.notify_one();
    }

    fn end(&self) {
        locked(&self.state).ended = true;
        self.ready.notify_one();
    }
}

/// The changes from the moment of subscribing on, until the bus shuts down.
pub struct Subscription {
    queue: Arc<Queue>,
    bus: Arc<Inner>,
}

impl Subscription {
    /// The next change, waiting at most `wait` for it.
    pub fn recv_timeout(&self, wait: Duration) -> Result<Change, RecvError> {
        let deadline = Instant::now() + wait;
        let mut state = locked(&self.queue.state);
        loop {
            if let Some(change) = state.items.pop_front() {
                return Ok(change);
            }
            if state.ended {
                return Err(RecvError::Ended);
            }
            let Some(left) = deadline.checked_duration_since(Instant::now()) else {
                return Err(RecvError::Timeout);
            };
            state = self
                .queue
                .ready
                .wait_timeout(state, left)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
    }
}

impl Drop for Subscription {
    fn drop(&mut self) {
        locked(&self.bus.state)
            .subscribers
            .retain(|queue| !Arc::ptr_eq(queue, &self.queue));
    }
}

/// The bus without its timer: what is held back, and until when.
#[derive(Default)]
struct State {
    rev: u64,
    subscribers: Vec<Arc<Queue>>,
    /// Topics published after a delay, with the time each goes out.
    held: Vec<(Instant, String)>,
    batch: BTreeSet<String>,
    batch_due: Option<Instant>,
    closed: bool,
    /// Whether a thread is waiting for the next time something goes out.
    timer_running: bool,
}

impl State {
    fn publish(&mut self, names: BTreeSet<String>, batch: Duration, now: Instant) {
        if names.is_empty() || self.closed {
            return;
        }
        if batch.is_zero() {
            self.deliver(names.into_iter().collect());
            return;
        }
        if self.batch.is_empty() {
            self.batch_due = Some(now + batch);
        }
        self.batch.extend(names);
    }

    fn publish_after(&mut self, topic: &str, wait: Duration, now: Instant) {
        if self.closed || self.held.iter().any(|(_, held)| held == topic) {
            return;
        }
        self.held.push((now + wait, topic.to_owned()));
    }

    /// The time the first thing held back goes out.
    fn next_due(&self) -> Option<Instant> {
        self.held
            .iter()
            .map(|(due, _)| *due)
            .chain(self.batch_due)
            .min()
    }

    /// Sends what is due by `now`: the batch as one message, each held topic on its own.
    fn flush_due(&mut self, now: Instant) {
        if self.batch_due.is_some_and(|due| due <= now) {
            self.batch_due = None;
            let names = std::mem::take(&mut self.batch);
            if !names.is_empty() {
                self.deliver(names.into_iter().collect());
            }
        }
        let (due, later): (Vec<_>, Vec<_>) = self.held.drain(..).partition(|(due, _)| *due <= now);
        self.held = later;
        for (_, topic) in due {
            self.deliver(vec![topic]);
        }
    }

    fn deliver(&mut self, changed: Vec<String>) {
        self.rev += 1;
        let change = Change {
            changed,
            rev: self.rev,
        };
        for queue in &self.subscribers {
            queue.push(change.clone());
        }
    }

    fn shutdown(&mut self) {
        self.closed = true;
        self.batch.clear();
        self.batch_due = None;
        self.held.clear();
        for queue in self.subscribers.drain(..) {
            queue.end();
        }
    }
}

struct Inner {
    state: Mutex<State>,
    changed: Condvar,
    batch: Duration,
}

/// The change bus. Cloning gives another handle to the same bus.
#[derive(Clone)]
pub struct ChangeBus {
    inner: Arc<Inner>,
}

impl Default for ChangeBus {
    fn default() -> Self {
        Self::new(BATCH)
    }
}

impl ChangeBus {
    /// A bus that sends changes published within `batch` of each other as one message; with no
    /// batch every change goes out at once.
    pub fn new(batch: Duration) -> Self {
        Self {
            inner: Arc::new(Inner {
                state: Mutex::new(State::default()),
                changed: Condvar::new(),
                batch,
            }),
        }
    }

    /// The number of messages sent so far.
    pub fn rev(&self) -> u64 {
        locked(&self.inner.state).rev
    }

    pub fn subscribers(&self) -> usize {
        locked(&self.inner.state).subscribers.len()
    }

    /// The changes from now on. After a shutdown a subscription is over at once.
    pub fn subscribe(&self) -> Subscription {
        let queue = Arc::new(Queue::default());
        let mut state = locked(&self.inner.state);
        if state.closed {
            queue.end();
        } else {
            state.subscribers.push(Arc::clone(&queue));
        }
        Subscription {
            queue,
            bus: Arc::clone(&self.inner),
        }
    }

    pub fn publish<S: AsRef<str>>(&self, topics: impl IntoIterator<Item = S>) {
        let names: BTreeSet<String> = topics
            .into_iter()
            .map(|topic| topic.as_ref().to_owned())
            .collect();
        let mut state = locked(&self.inner.state);
        state.publish(names, self.inner.batch, Instant::now());
        self.arm(state);
    }

    /// Publishes `topic` once, `wait` from the first call; the calls made meanwhile add nothing.
    ///
    /// For what changes many times a second, so the UI refetches it now and then instead of every
    /// time.
    pub fn publish_after(&self, topic: &str, wait: Duration) {
        let mut state = locked(&self.inner.state);
        state.publish_after(topic, wait, Instant::now());
        self.arm(state);
    }

    /// Sends what is held back at once, whatever time it is due.
    #[cfg(feature = "testing")]
    pub fn flush(&self) {
        let mut state = locked(&self.inner.state);
        state.flush_due(Instant::now() + Duration::from_secs(365 * 24 * 3600));
    }

    /// Ends every subscription, so open event streams do not hold the server up.
    pub fn shutdown(&self) {
        let mut state = locked(&self.inner.state);
        state.shutdown();
        self.inner.changed.notify_all();
    }

    /// Makes sure a thread is waiting for whatever is held back.
    fn arm(&self, mut state: MutexGuard<'_, State>) {
        if state.next_due().is_none() {
            return;
        }
        self.inner.changed.notify_all();
        if state.timer_running {
            return;
        }
        let inner = Arc::clone(&self.inner);
        match thread::Builder::new()
            .name("kisetsu-bus".into())
            .spawn(move || wait_and_send(&inner))
        {
            Ok(_) => state.timer_running = true,
            Err(error) => {
                warn!("Changes cannot be held back: {error}");
                state.flush_due(Instant::now() + Duration::from_secs(3600 * 24 * 365));
            }
        }
    }
}

/// Sends what is held back as it falls due, and ends when nothing is.
fn wait_and_send(inner: &Inner) {
    let mut state = locked(&inner.state);
    loop {
        let now = Instant::now();
        state.flush_due(now);
        let Some(due) = state.next_due() else {
            state.timer_running = false;
            return;
        };
        state = inner
            .changed
            .wait_timeout(state, due.saturating_duration_since(now))
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .0;
        if state.closed {
            state.timer_running = false;
            return;
        }
    }
}

#[cfg(test)]
mod tests;
