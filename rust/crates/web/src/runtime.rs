//! What a request handler reaches the engine through.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use kisetsu_clients::qbittorrent::{Qbit, QbitClient, QbitError};
use kisetsu_clients::traffic;
use kisetsu_engine::jobs::Jobs;
use kisetsu_engine::remote::Remote;
use kisetsu_engine::runner::Engine;
use kisetsu_engine::scheduler::Scheduler;

use crate::logs::LogRing;

/// Jobs whose runs the UI shows.
const VISIBLE_JOBS: [&str; 5] = [
    "startup",
    "refresh_feeds",
    "hunt",
    "sync_anilist",
    "backfill",
];
/// How soon at most the UI is told that the request counts changed.
const REQUESTS_PUBLISH: Duration = Duration::from_secs(2);

/// Logs in once with these credentials and reports qBittorrent's versions and its default save
/// folder: `(host, username, password)`.
pub type Probe =
    Box<dyn Fn(&str, &str, &str) -> Result<BTreeMap<String, String>, QbitError> + Send + Sync>;

/// Everything the handlers talk to.
pub struct Runtime {
    pub engine: Arc<Engine>,
    pub scheduler: Scheduler,
    pub jobs: Arc<Jobs>,
    pub remote: Arc<dyn Remote>,
    pub logs: Arc<LogRing>,
    pub sandbox: bool,
    pub probe: Probe,
}

impl Runtime {
    /// A runtime whose connection test asks the real qBittorrent. It tells the UI, through the
    /// engine's change bus, when a log line is written, when a job it shows starts or ends and, in
    /// a sandbox, that the request counts moved.
    pub fn new(
        engine: Arc<Engine>,
        scheduler: Scheduler,
        jobs: Arc<Jobs>,
        remote: Arc<dyn Remote>,
        logs: Arc<LogRing>,
        sandbox: bool,
    ) -> Self {
        let bus = engine.bus().clone();
        {
            let bus = bus.clone();
            scheduler.on_change(move |name, _| {
                if VISIBLE_JOBS.contains(&name) {
                    bus.publish(["status"]);
                }
            });
        }
        {
            let bus = bus.clone();
            logs.on_line(move || bus.publish(["logs"]));
        }
        // They change with every request, so the UI hears of it every few seconds.
        if sandbox {
            traffic::set_listener(Some(Arc::new(move || {
                bus.publish_after("requests", REQUESTS_PUBLISH);
            })));
        }
        Self {
            engine,
            scheduler,
            jobs,
            remote,
            logs,
            sandbox,
            probe: Box::new(probe_qbit),
        }
    }
}

fn probe_qbit(
    host: &str,
    username: &str,
    password: &str,
) -> Result<BTreeMap<String, String>, QbitError> {
    let mut client = QbitClient::new(host, username, password);
    let outcome = (|| {
        let versions = client.versions()?;
        let mut found = BTreeMap::from([
            ("app_version".to_owned(), versions.app_version),
            ("api_version".to_owned(), versions.api_version),
        ]);
        // The folder only fills in a suggestion, so a connection that answers is not failed over it.
        if let Ok(folder) = client.default_save_path() {
            found.insert("default_save_path".to_owned(), folder);
        }
        Ok(found)
    })();
    client.close();
    outcome
}

impl Drop for Runtime {
    fn drop(&mut self) {
        if self.sandbox {
            traffic::set_listener(None);
        }
    }
}
