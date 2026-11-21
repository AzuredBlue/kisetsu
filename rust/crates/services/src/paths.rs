//! Resolving folders against the file system.

use std::env;
use std::fs;
use std::path::{Component, Path, PathBuf, is_separator};

/// A leading '~' alone or before a separator is the home directory. A '~user' form is left as it
/// is.
fn expand_user(path: &str, home: &str) -> String {
    match path.strip_prefix('~') {
        Some(rest) if rest.is_empty() || rest.starts_with(is_separator) => format!("{home}{rest}"),
        _ => path.to_owned(),
    }
}

/// The path with a leading '~' replaced by the user's home directory.
pub fn expand_home(path: &str) -> String {
    let home = env::home_dir().unwrap_or_default();
    expand_user(path, &home.to_string_lossy())
}

/// The absolute, resolved form of a path with a leading '~' meaning the home directory `home`.
///
/// The path is made absolute against the working directory and `.` and `..` are resolved by its
/// text. Then the longest leading part that exists is replaced by its real path (links followed,
/// the case on disk), and the parts that don't exist are kept as written.
pub fn resolve(path: &str, home: &str) -> String {
    let expanded = PathBuf::from(expand_user(path, home));
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::path::absolute(&expanded)
            .or_else(|_| env::current_dir().map(|cwd| cwd.join(&expanded)))
            .unwrap_or(expanded)
    };
    let mut normal = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normal.pop();
            }
            other => normal.push(other),
        }
    }
    real_path(&normal).to_string_lossy().into_owned()
}

/// The path with its longest existing leading part replaced by that part's real path.
fn real_path(path: &Path) -> PathBuf {
    let mut missing = Vec::new();
    let mut existing = path;
    loop {
        if let Ok(real) = fs::canonicalize(existing) {
            let mut real = without_verbatim_prefix(real);
            real.extend(missing.iter().rev());
            return real;
        }
        match (existing.parent(), existing.file_name()) {
            (Some(parent), Some(name)) => {
                missing.push(name);
                existing = parent;
            }
            _ => return path.to_path_buf(),
        }
    }
}

/// A path made absolute and plain by its text alone, so two spellings of one folder compare
/// equal: `.` and `..` are resolved, and where the file system ignores case so is the case.
pub fn normalized(path: &str) -> String {
    let absolute = std::path::absolute(path).unwrap_or_else(|_| PathBuf::from(path));
    let mut plain = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                plain.pop();
            }
            other => plain.push(other),
        }
    }
    let text = plain.to_string_lossy().into_owned();
    if cfg!(windows) {
        text.to_lowercase()
    } else {
        text
    }
}

/// Whether `child` is `parent` or somewhere under it, however either is spelled.
pub fn within(child: &str, parent: &str) -> bool {
    Path::new(&normalized(child)).starts_with(normalized(parent))
}

/// A Windows real path without the `\\?\` prefix the system adds (`\\?\UNC\` becomes `\\`).
fn without_verbatim_prefix(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(unc) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{unc}"))
    } else if let Some(local) = text.strip_prefix(r"\\?\") {
        PathBuf::from(local)
    } else {
        path
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_existing_part_is_real_and_the_rest_stays_as_written() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("shows")).unwrap();
        let real = without_verbatim_prefix(fs::canonicalize(dir.path()).unwrap());
        let written = dir.path().join("shows").join(".").join("old").join("..");
        let resolved = resolve(&format!("{}/Bleach/Season 2", written.display()), "");
        assert_eq!(
            PathBuf::from(resolved),
            real.join("shows").join("Bleach").join("Season 2")
        );
    }

    #[test]
    fn the_verbatim_prefix_is_removed() {
        let local = without_verbatim_prefix(PathBuf::from(r"\\?\C:\Shows"));
        assert_eq!(local, PathBuf::from(r"C:\Shows"));
        let share = without_verbatim_prefix(PathBuf::from(r"\\?\UNC\host\share"));
        assert_eq!(share, PathBuf::from(r"\\host\share"));
        let plain = without_verbatim_prefix(PathBuf::from("/shows"));
        assert_eq!(plain, PathBuf::from("/shows"));
    }

    #[test]
    fn a_folder_is_within_itself_and_the_folders_above_it_however_it_is_spelled() {
        let base = std::env::temp_dir();
        let parent = base.join("Shows").to_string_lossy().into_owned();
        let child = base
            .join("Shows")
            .join("x")
            .join("..")
            .join("Bleach")
            .join("01.mkv");
        let child = child.to_string_lossy().into_owned();
        assert!(within(&child, &parent));
        assert!(within(&parent, &parent));
        assert!(!within(&parent, &child));
        let sibling = base.join("Shows 2").to_string_lossy().into_owned();
        assert!(!within(&sibling, &parent));
    }
    #[test]
    fn a_leading_tilde_is_the_home_directory() {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().to_string_lossy().into_owned();
        let real = without_verbatim_prefix(fs::canonicalize(dir.path()).unwrap());
        assert_eq!(PathBuf::from(resolve("~/Anime", &home)), real.join("Anime"));
        assert_eq!(PathBuf::from(resolve("~", &home)), real);
        assert_eq!(expand_user("~other/x", &home), "~other/x");
    }
}
