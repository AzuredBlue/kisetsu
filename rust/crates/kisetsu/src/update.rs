//! Updating to a newer release: a check a day, and on the user's word the new program is
//! downloaded and checked, the app stops as usual, the program file is replaced and the new one
//! starts with the same arguments.
//!
//! Only builds made with the release key (`KISETSU_UPDATE_KEY`) update themselves, so a build of
//! one's own is never replaced by a release.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;
use std::{env, fs, io};

use kisetsu_clients::update::{Release, Releases, is_newer};
use kisetsu_engine::bus::ChangeBus;
use kisetsu_engine::scheduler::{JobResult, Scheduler};
use kisetsu_web::{UpdateControl, UpdateStatus};
use tracing::{info, warn};

/// The public key the release's sums are signed with, in base64.
const KEY: Option<&str> = option_env!("KISETSU_UPDATE_KEY");
/// Where the latest release is read from.
const LATEST: &str = match option_env!("KISETSU_UPDATE_SOURCE") {
    Some(source) => source,
    None => "https://api.github.com/repos/AzuredBlue/kisetsu/releases/latest",
};
const CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);
const FIRST_CHECK: Duration = Duration::from_secs(60);

/// The new program is written beside the running one, so putting it in place is a rename.
fn staging_path(program: &Path) -> PathBuf {
    program.with_file_name(".kisetsu-update")
}

/// Where the running program is moved on Windows, which cannot replace a running program's file.
fn previous_path(program: &Path) -> PathBuf {
    program.with_extension("old")
}

pub struct Updater {
    key: &'static str,
    program: PathBuf,
    latest: Mutex<Option<Release>>,
    staged: Mutex<Option<PathBuf>>,
    installing: AtomicBool,
    stop: Sender<()>,
    bus: ChangeBus,
}

impl Updater {
    /// The updater of a release build; `None` for any other build. `stop` ends the run, after
    /// which [`Updater::staged`] says whether to restart into a new program.
    pub fn for_this_build(stop: Sender<()>, bus: ChangeBus) -> Option<Arc<Self>> {
        // The key's own line, also when the whole `.pub` file was given.
        let key = KEY?.lines().map(str::trim).rfind(|line| !line.is_empty())?;
        let program = env::current_exe().ok()?;
        // What an earlier update left behind.
        let _ = fs::remove_file(previous_path(&program));
        let _ = fs::remove_file(staging_path(&program));
        Some(Arc::new(Self {
            key,
            program,
            latest: Mutex::new(None),
            staged: Mutex::new(None),
            installing: AtomicBool::new(false),
            stop,
            bus,
        }))
    }

    /// Checks for a newer release now and once a day.
    pub fn schedule(self: &Arc<Self>, scheduler: &Scheduler) {
        let updater = Arc::clone(self);
        scheduler.add(
            "update_check",
            Arc::new(move || updater.check()),
            Some(CHECK_INTERVAL),
            Some(FIRST_CHECK),
        );
    }

    fn check(&self) -> JobResult {
        // On Windows the program an update replaced may still have been running at startup.
        let _ = fs::remove_file(previous_path(&self.program));
        // A day apart, so no connection is kept between checks.
        match Releases::new(LATEST).latest() {
            Ok(release) => {
                let newer = is_newer(&release.version, env!("CARGO_PKG_VERSION"));
                if newer {
                    info!("Kisetsu {} is available.", release.version);
                }
                let found = newer.then_some(release);
                let mut latest = self.latest.lock().unwrap_or_else(PoisonError::into_inner);
                if *latest != found {
                    *latest = found;
                    self.bus.publish(["update"]);
                }
            }
            Err(error) => warn!("Could not check for a newer Kisetsu: {error}"),
        }
        Ok(None)
    }

    /// The checked program to restart into, once an install stopped the run.
    pub fn staged(&self) -> Option<PathBuf> {
        self.staged
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn latest(&self) -> Option<Release> {
        self.latest
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

impl UpdateControl for Updater {
    fn status(&self) -> UpdateStatus {
        let Some(release) = self.latest() else {
            return UpdateStatus::default();
        };
        let blocked = (!release.installable())
            .then(|| "This release has no download for this system.".to_owned());
        UpdateStatus {
            latest: Some(release.version),
            page: Some(release.page),
            blocked,
        }
    }

    fn install(&self) -> Result<(), String> {
        let release = self
            .latest()
            .ok_or_else(|| "There is no newer version to install.".to_owned())?;
        if self.installing.swap(true, Ordering::SeqCst) {
            return Err("The update is already being installed.".to_owned());
        }
        let staging = staging_path(&self.program);
        info!("Downloading Kisetsu {}.", release.version);
        let downloaded = Releases::new(LATEST).download(&release, self.key, &staging);
        if let Err(error) = downloaded {
            self.installing.store(false, Ordering::SeqCst);
            warn!("Kisetsu {} was not installed: {error}", release.version);
            return Err(match error {
                kisetsu_clients::update::UpdateError::Save(cause) => format!(
                    "Kisetsu cannot write to its folder ({cause}). Download the new version from {}",
                    release.page
                ),
                error => error.to_string(),
            });
        }
        info!(
            "Kisetsu {} is checked; restarting into it.",
            release.version
        );
        *self.staged.lock().unwrap_or_else(PoisonError::into_inner) = Some(staging);
        let _ = self.stop.send(());
        Ok(())
    }
}

/// Puts the checked program in place of the running one and starts it with this run's
/// arguments. On Linux the new program takes over this process, so this returns only on failure.
pub fn restart_into(staged: &Path) -> io::Result<()> {
    let program = env::current_exe()?;
    let arguments: Vec<_> = env::args_os().skip(1).collect();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        use std::os::unix::process::CommandExt;

        fs::set_permissions(staged, fs::Permissions::from_mode(0o755))?;
        fs::rename(staged, &program)?;
        Err(std::process::Command::new(&program).args(arguments).exec())
    }
    #[cfg(not(unix))]
    {
        let previous = previous_path(&program);
        let _ = fs::remove_file(&previous);
        fs::rename(&program, &previous)?;
        if let Err(error) = fs::rename(staged, &program) {
            let _ = fs::rename(&previous, &program);
            return Err(error);
        }
        std::process::Command::new(&program)
            .args(arguments)
            .spawn()?;
        Ok(())
    }
}
