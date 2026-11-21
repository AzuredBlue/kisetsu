//! The engine: one thread for everything that touches the database or qBittorrent.
//!
//! [`Engine::call`] hands a unit of work to that thread, which runs it in one write transaction
//! and publishes the parts of the UI it changed. [`Engine::read`] runs a unit of work beside the
//! engine thread on a reader connection, saves nothing and cannot reach qBittorrent.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex, MutexGuard, TryLockError};
use std::thread::{self, JoinHandle};
use std::time::{SystemTime, UNIX_EPOCH};

use jiff::Timestamp;
use kisetsu_clients::qbittorrent::Qbit;
use kisetsu_clients::traffic;
use kisetsu_db::rusqlite::Connection;
use kisetsu_db::schema::Settings;
use kisetsu_db::store;
use kisetsu_services::context::{Caches, Clock, Context, Notify, SystemClock, TagSource};
use sha1_smol::Sha1;
use tracing::warn;

use crate::bus::ChangeBus;
use crate::error::{Error, Result};
use crate::scheduler::panic_message;

/// How many reads run at the same time, kept apart from the engine thread so a slow read never
/// holds up a page.
pub const READ_CONNECTIONS: usize = 2;

/// Builds the connection to qBittorrent for the settings in force. It runs on the engine thread,
/// whenever the host or the login changes.
pub type QbitFactory = Box<dyn FnMut(&Settings) -> Box<dyn Qbit> + Send>;

/// What an engine is made of beyond its database.
pub struct Parts {
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub qbit: QbitFactory,
    /// Where finished downloads are announced; `None` announces nothing.
    pub notifier: Option<Arc<dyn Notify + Send + Sync>>,
    pub bus: ChangeBus,
}

impl Parts {
    pub fn new(qbit: QbitFactory) -> Self {
        Self {
            clock: Arc::new(SystemClock),
            qbit,
            notifier: None,
            bus: ChangeBus::default(),
        }
    }
}

/// The names that tell the torrents Kisetsu added apart while their hash is not known.
#[derive(Debug, Default)]
struct OperationTags {
    issued: u64,
}

impl TagSource for OperationTags {
    /// `kisetsu-op-` and 32 hexadecimal digits, different on every call and every run.
    fn next_tag(&mut self) -> String {
        self.issued += 1;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_nanos());
        let digest = Sha1::from(format!("{nanos}:{}:{}", std::process::id(), self.issued)).digest();
        let mut tag = String::from(kisetsu_services::downloads::TAG_PREFIX);
        tag.extend(digest.to_string().chars().take(32));
        tag
    }
}

/// The connection to qBittorrent, kept while the settings that made it stay the same.
struct QbitSlot {
    factory: QbitFactory,
    current: Option<(Login, Box<dyn Qbit>)>,
}

#[derive(PartialEq, Eq)]
struct Login {
    host: String,
    username: String,
    password: String,
}

impl QbitSlot {
    fn get(&mut self, settings: &Settings) -> &mut dyn Qbit {
        let login = Login {
            host: settings.qbit_host.clone(),
            username: settings.qbit_username.clone(),
            password: settings.qbit_password.clone(),
        };
        let held = match self.current.take() {
            Some((held, qbit)) if held == login => (held, qbit),
            other => {
                if let Some((_, mut stale)) = other {
                    stale.close();
                }
                (login, (self.factory)(settings))
            }
        };
        self.current.insert(held).1.as_mut()
    }

    fn close(&mut self) {
        if let Some((_, mut qbit)) = self.current.take() {
            qbit.close();
        }
    }
}

/// The parts of the engine's caches a read is given a copy of.
#[derive(Debug, Default, Clone)]
struct ReadCaches {
    stalled: HashSet<i64>,
    carried: HashMap<i64, bool>,
}

/// What the engine thread owns.
struct Worker {
    conn: Connection,
    caches: Caches,
    qbit: QbitSlot,
    tags: OperationTags,
    clock: Arc<dyn Clock + Send + Sync>,
    notifier: Option<Arc<dyn Notify + Send + Sync>>,
    bus: ChangeBus,
    read_caches: Arc<Mutex<ReadCaches>>,
}

impl Worker {
    fn run<R>(
        &mut self,
        caller: &str,
        work: impl FnOnce(&mut Context<'_>) -> kisetsu_services::Result<R>,
    ) -> Result<R> {
        let _caller = traffic::caller(caller);
        let now = self.clock.now();
        let mut topics = BTreeSet::new();
        let Self {
            conn,
            caches,
            qbit,
            tags,
            notifier,
            ..
        } = self;
        let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
            store::session_scope(conn, |tx| {
                let mut ctx = Context::new(tx, now, caches)?;
                let connection = qbit.get(&ctx.settings);
                ctx.qbit = Some(connection);
                ctx.download_tags = Some(tags);
                ctx.notifier = notifier.as_deref().map(|notifier| notifier as &dyn Notify);
                let result = work(&mut ctx);
                topics = std::mem::take(&mut ctx.topics);
                result
            })
        }));
        let result = match outcome {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(error)) => Err(Error::Service(error)),
            Err(panic) => Err(Error::Panicked(panic_message(&*panic))),
        };
        if result.is_err() {
            // What the caches hold may be rows that never reached the database.
            self.caches.known_items = None;
            self.caches.matchers = None;
        }
        self.bus.publish(topics);
        self.share_caches();
        result
    }

    /// Runs `work` on a view of the database that is never saved, and without qBittorrent.
    fn dry_run<R>(
        &mut self,
        work: impl FnOnce(&mut Context<'_>) -> kisetsu_services::Result<R>,
    ) -> Result<R> {
        let now = self.clock.now();
        let shared = locked(&self.read_caches).clone();
        let mut caches = Caches {
            stalled: shared.stalled,
            carried: shared.carried,
            ..Caches::default()
        };
        let conn = &mut self.conn;
        let outcome = panic::catch_unwind(AssertUnwindSafe(|| {
            store::read_scope(conn, |tx| {
                let mut ctx = Context::new(tx, now, &mut caches)?;
                work(&mut ctx)
            })
        }));
        match outcome {
            Ok(result) => result.map_err(Error::Service),
            Err(panic) => Err(Error::Panicked(panic_message(&*panic))),
        }
    }

    /// Leaves what a read may use where it can find it.
    fn share_caches(&self) {
        let mut shared = locked(&self.read_caches);
        if shared.stalled != self.caches.stalled {
            shared.stalled.clone_from(&self.caches.stalled);
        }
        if shared.carried != self.caches.carried {
            shared.carried.clone_from(&self.caches.carried);
        }
    }

    fn close(mut self) {
        self.qbit.close();
        if let Err(error) = store::close(self.conn) {
            warn!("The database did not close cleanly: {error}");
        }
    }
}

enum Message {
    Work(Box<dyn FnOnce(&mut Worker) + Send>),
    Close,
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// The reader connections: [`READ_CONNECTIONS`] slots, each opened when first used.
struct Readers {
    path: PathBuf,
    slots: [Mutex<Option<Connection>>; READ_CONNECTIONS],
    next: AtomicUsize,
}

impl Readers {
    fn new(path: &Path) -> Self {
        Self {
            path: path.to_path_buf(),
            slots: std::array::from_fn(|_| Mutex::new(None)),
            next: AtomicUsize::new(0),
        }
    }

    /// A slot nobody is using, or else the next one in turn once its user is done.
    fn acquire(&self) -> MutexGuard<'_, Option<Connection>> {
        for slot in &self.slots {
            match slot.try_lock() {
                Ok(free) => return free,
                Err(TryLockError::Poisoned(poisoned)) => return poisoned.into_inner(),
                Err(TryLockError::WouldBlock) => {}
            }
        }
        let turn = self.next.fetch_add(1, Ordering::Relaxed) % READ_CONNECTIONS;
        locked(&self.slots[turn])
    }

    fn close(&self) {
        for slot in &self.slots {
            *locked(slot) = None;
        }
    }
}

/// The engine. It is shared between threads; every call blocks until the engine thread (or, for a
/// read, a reader connection) has done the work.
pub struct Engine {
    sender: Sender<Message>,
    thread: Mutex<Option<JoinHandle<()>>>,
    stopped: AtomicBool,
    clock: Arc<dyn Clock + Send + Sync>,
    bus: ChangeBus,
    readers: Readers,
    read_caches: Arc<Mutex<ReadCaches>>,
}

impl Engine {
    /// Opens (and if need be migrates) the database at `path` and starts the engine thread.
    pub fn start(path: &Path, parts: Parts) -> Result<Self> {
        let conn = store::open(path)?;
        let read_caches = Arc::new(Mutex::new(ReadCaches::default()));
        let (sender, messages) = mpsc::channel::<Message>();
        let (factory, clock, notifier) = (parts.qbit, Arc::clone(&parts.clock), parts.notifier);
        let (bus, shared) = (parts.bus.clone(), Arc::clone(&read_caches));
        // The connection to qBittorrent is made on this thread and never leaves it.
        let thread = thread::Builder::new()
            .name("kisetsu-engine".into())
            .spawn(move || {
                let worker = Worker {
                    conn,
                    caches: Caches::default(),
                    qbit: QbitSlot {
                        factory,
                        current: None,
                    },
                    tags: OperationTags::default(),
                    clock,
                    notifier,
                    bus,
                    read_caches: shared,
                };
                serve(worker, &messages);
            })?;
        Ok(Self {
            sender,
            thread: Mutex::new(Some(thread)),
            stopped: AtomicBool::new(false),
            clock: parts.clock,
            bus: parts.bus,
            readers: Readers::new(path),
            read_caches,
        })
    }

    pub fn bus(&self) -> &ChangeBus {
        &self.bus
    }

    /// The time, by the engine's clock.
    pub fn now(&self) -> Timestamp {
        self.clock.now()
    }

    /// Whether the engine takes no more work.
    pub fn is_stopped(&self) -> bool {
        self.stopped.load(Ordering::Acquire)
    }

    /// Runs `work` on the engine thread, in one write transaction: committed when it returns
    /// `Ok`, rolled back when it fails. What it touched is published either way, as a failed
    /// job may have committed part of its work already.
    pub fn call<R: Send + 'static>(
        &self,
        work: impl FnOnce(&mut Context<'_>) -> kisetsu_services::Result<R> + Send + 'static,
    ) -> Result<R> {
        if self.is_stopped() {
            return Err(Error::Stopped);
        }
        let caller = traffic::current_caller();
        let (reply, answer) = mpsc::channel();
        let message = Message::Work(Box::new(move |worker| {
            let _ = reply.send(worker.run(&caller, work));
        }));
        self.sender.send(message).map_err(|_| Error::Stopped)?;
        answer.recv().map_err(|_| Error::Stopped)?
    }

    /// Runs `work` on the engine thread like [`Self::call`], but on a view of the database that is
    /// never saved and without qBittorrent: `work` may write while it computes (a preview, a copy
    /// of the database) without racing the engine for the database.
    pub fn dry_run<R: Send + 'static>(
        &self,
        work: impl FnOnce(&mut Context<'_>) -> kisetsu_services::Result<R> + Send + 'static,
    ) -> Result<R> {
        if self.is_stopped() {
            return Err(Error::Stopped);
        }
        let (reply, answer) = mpsc::channel();
        let message = Message::Work(Box::new(move |worker| {
            let _ = reply.send(worker.dry_run(work));
        }));
        self.sender.send(message).map_err(|_| Error::Stopped)?;
        answer.recv().map_err(|_| Error::Stopped)?
    }

    /// Runs `work` on a reader connection beside the engine thread: it sees what is committed,
    /// nothing it does is saved, and qBittorrent is out of its reach.
    pub fn read<R>(
        &self,
        work: impl FnOnce(&mut Context<'_>) -> kisetsu_services::Result<R>,
    ) -> Result<R> {
        if self.is_stopped() {
            return Err(Error::Stopped);
        }
        let mut slot = self.readers.acquire();
        let conn = match &mut *slot {
            Some(conn) => conn,
            empty => empty.insert(store::connect(&self.readers.path)?),
        };
        let shared = locked(&self.read_caches).clone();
        let mut caches = Caches {
            stalled: shared.stalled,
            carried: shared.carried,
            ..Caches::default()
        };
        let now = self.clock.now();
        let outcome = store::read_scope(conn, |tx| {
            let mut ctx = Context::new(tx, now, &mut caches)?;
            work(&mut ctx)
        });
        outcome.map_err(Error::Service)
    }

    /// Refuses new work from now on, so what is running ends at its next call.
    pub fn stop(&self) {
        self.stopped.store(true, Ordering::Release);
    }

    /// Refuses new work, ends the engine thread once it has finished what it is doing, and closes
    /// the database. Can be called again.
    pub fn close(&self) {
        self.stop();
        self.bus.shutdown();
        let _ = self.sender.send(Message::Close);
        if let Some(thread) = locked(&self.thread).take() {
            let _ = thread.join();
        }
        self.readers.close();
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.close();
    }
}

/// The engine thread: runs what it is sent until it is told to close.
fn serve(mut worker: Worker, messages: &mpsc::Receiver<Message>) {
    while let Ok(message) = messages.recv() {
        match message {
            Message::Work(work) => work(&mut worker),
            Message::Close => break,
        }
    }
    worker.close();
}

#[cfg(test)]
mod tests;
