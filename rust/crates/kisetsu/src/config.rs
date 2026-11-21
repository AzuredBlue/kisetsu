//! Where the app keeps its data and which mode it runs in.

use std::env;
use std::path::PathBuf;

/// The environment variable that makes a run a sandbox (`1`), like `--sandbox` but on the
/// database the environment names.
const SANDBOX_VARIABLE: &str = "KISETSU_SANDBOX";

/// Whether the environment asks for a sandbox: qBittorrent is read, but every change to it is only
/// logged and pretended.
pub fn sandbox_from_environment() -> bool {
    env::var_os(SANDBOX_VARIABLE).is_some_and(|value| value == "1")
}

/// The database a normal run uses: `KISETSU_DB_PATH`, else `kisetsu.db` in the config folder.
/// `None` when the user has no home folder.
pub fn database_path() -> Option<PathBuf> {
    kisetsu_db::store::default_path()
}
