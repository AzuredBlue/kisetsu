//! Where the app keeps its data and which mode it runs in.

use std::env;
use std::path::{Path, PathBuf};

/// The environment variable that makes a run a sandbox (`1`), like `--sandbox` but on the
/// database the environment names.
const SANDBOX_VARIABLE: &str = "KISETSU_SANDBOX";

/// Names the folder the UI's files are served from.
const STATIC_VARIABLE: &str = "KISETSU_STATIC_DIR";
/// Names, comma separated, the host names other than this machine's own that the UI may be
/// reached by.
const ALLOWED_HOSTS_VARIABLE: &str = "KISETSU_ALLOWED_HOSTS";
/// Where the UI's files are in a source checkout.
const CHECKOUT_STATIC: [&str; 3] = ["kisetsu", "web", "static"];

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

/// The host names, besides this machine's own, that requests may be addressed to.
pub fn allowed_hosts() -> Vec<String> {
    env::var(ALLOWED_HOSTS_VARIABLE)
        .unwrap_or_default()
        .split(',')
        .map(|name| name.trim().to_lowercase())
        .filter(|name| !name.is_empty())
        .collect()
}

fn holds_the_page(folder: &Path) -> bool {
    folder.join("index.html").is_file()
}

/// The folder with the UI's files: the one `KISETSU_STATIC_DIR` names, else `static` beside the
/// program, else the one of the source checkout the program was built in.
pub fn static_dir() -> Option<PathBuf> {
    if let Some(named) = env::var_os(STATIC_VARIABLE) {
        return Some(PathBuf::from(named));
    }
    let program = env::current_exe().ok()?;
    let beside = program.parent()?.join("static");
    if holds_the_page(&beside) {
        return Some(beside);
    }
    program.ancestors().skip(1).find_map(|folder| {
        let candidate = CHECKOUT_STATIC
            .iter()
            .fold(folder.to_path_buf(), |path, part| path.join(part));
        holds_the_page(&candidate).then_some(candidate)
    })
}
