//! Wires the engine together and runs it until the process is told to stop.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc;
use std::time::Duration;

use kisetsu_clients::qbittorrent::QbitClient;
use kisetsu_clients::sandbox::{DOWNLOAD_SECONDS, SandboxQbit, SandboxState};
use kisetsu_db::rusqlite::params;
use kisetsu_db::store;
use kisetsu_engine::instance::{InstanceLock, LockError, lock_path};
use kisetsu_engine::jobs::Jobs;
use kisetsu_engine::remote::{Remote, WebRemote};
use kisetsu_engine::runner::{Engine, Parts, QbitFactory};
use kisetsu_engine::scheduler::Scheduler;
use kisetsu_web::{App, Assets, Guard, LogRing, Runtime, ServeError, Server};
use tracing::{info, warn};

use crate::cli::Args;
use crate::config;

/// How long the jobs get to finish what they are doing once the app is told to stop.
const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Lock(#[from] LockError),
    #[error(transparent)]
    Engine(#[from] kisetsu_engine::Error),
    #[error("Cannot set up the sandbox: {0}")]
    Sandbox(#[from] std::io::Error),
    #[error("There is no home folder to keep the database in; set KISETSU_DB_PATH.")]
    NoDatabasePath,
    #[error(
        "The UI's files were not found; set KISETSU_STATIC_DIR to the folder that holds index.html."
    )]
    NoStaticFiles,
    #[error(transparent)]
    Serve(#[from] ServeError),
    #[error("Cannot listen for the stop signal: {0}")]
    Signal(#[from] ctrlc::Error),
}

/// The title and info hash of the download that adds `url`, for the sandbox's pretend torrent.
fn release_of(database: &Path, url: &str) -> (Option<String>, Option<String>) {
    let Ok(conn) = store::connect(database) else {
        return (None, None);
    };
    conn.query_row(
        "SELECT title, info_hash FROM downloads WHERE torrent_url = ?1 ORDER BY id DESC LIMIT 1",
        params![url],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
    .unwrap_or((None, None))
}

/// qBittorrent as the settings name it; in a sandbox it is read but never changed.
fn qbit_factory(sandbox: bool, database: &Path) -> QbitFactory {
    if !sandbox {
        return Box::new(|settings| {
            Box::new(QbitClient::new(
                &settings.qbit_host,
                &settings.qbit_username,
                &settings.qbit_password,
            ))
        });
    }
    let mut state = SandboxState::new();
    let database = database.to_path_buf();
    state.set_release_for(Box::new(move |url| release_of(&database, url)));
    let shared = state.shared();
    info!(
        "Sandbox: downloads are only pretended and logged ({DOWNLOAD_SECONDS} s each); qBittorrent is not changed."
    );
    Box::new(move |settings| {
        let real = QbitClient::new(
            &settings.qbit_host,
            &settings.qbit_username,
            &settings.qbit_password,
        );
        Box::new(SandboxQbit::new(real, Arc::clone(&shared)))
    })
}

/// Runs Kisetsu until Ctrl+C (or the platform's stop signal).
pub fn run(args: &Args, logs: Arc<LogRing>) -> Result<(), Error> {
    // A sandbox started with the flag has a database of its own, thrown away when it ends.
    let sandbox_dir = if args.sandbox {
        Some(
            tempfile::Builder::new()
                .prefix("kisetsu-sandbox-")
                .tempdir()?,
        )
    } else {
        None
    };
    let sandbox = args.sandbox || config::sandbox_from_environment();
    let database: PathBuf = match &sandbox_dir {
        Some(dir) => dir.path().join("kisetsu.db"),
        None => config::database_path().ok_or(Error::NoDatabasePath)?,
    };
    let static_dir = config::static_dir().ok_or(Error::NoStaticFiles)?;
    let (stop, stopped) = mpsc::channel();
    ctrlc::set_handler(move || {
        let _ = stop.send(());
    })?;

    let lock = InstanceLock::acquire(&lock_path(&database))?;
    info!("Database: {}", database.display());
    let mut list_cache = database.clone().into_os_string();
    list_cache.push(".userlist.json");
    let engine = Arc::new(Engine::start(
        &database,
        Parts::new(qbit_factory(sandbox, &database)),
    )?);
    let scheduler = Scheduler::default();
    let remote: Arc<dyn Remote> = Arc::new(WebRemote::new(PathBuf::from(list_cache)));
    let jobs = Arc::new(Jobs::new(
        Arc::clone(&engine),
        scheduler.clone(),
        Arc::clone(&remote),
    ));
    let bus = engine.bus().clone();
    jobs.register();
    scheduler.start();

    let runtime = Runtime::new(
        Arc::clone(&engine),
        scheduler.clone(),
        Arc::clone(&jobs),
        remote,
        logs,
        sandbox,
    );
    let guard = Guard::new(Guard::machine_name(), &config::allowed_hosts(), sandbox);
    let app = Arc::new(App::new(runtime, guard, Assets::new(static_dir)));
    let mut server = Server::start(Arc::clone(&app), &args.host, args.port)?;
    info!("Listening on http://{}:{}", args.host, args.port);

    // The signal handler only sends; the stop itself happens here.
    let _ = stopped.recv();
    info!("Shutting down.");
    engine.stop();
    bus.shutdown();
    server.stop();
    app.wait_for_background();
    let running = scheduler.stop(SHUTDOWN_GRACE);
    if !running.is_empty() {
        warn!("Jobs still running at shutdown: {}", running.join(", "));
    }
    scheduler.clear();
    engine.close();
    drop(lock);
    Ok(())
}
