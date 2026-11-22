//! The log kept on disk beside the database, so a run without a console still leaves a trace of
//! why it stopped.

use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};

/// Past this size the log is set aside as `kisetsu.log.1` when the app starts, replacing the one
/// set aside before.
const MAX_BYTES: u64 = 1024 * 1024;

/// Where the log of a run on `database` goes.
pub fn path_beside(database: &Path) -> Option<PathBuf> {
    Some(database.parent()?.join("kisetsu.log"))
}

/// Opens the log for appending, setting a full one aside first. The folder must exist already:
/// opening the database creates it with the right permissions.
pub fn open(path: &Path) -> io::Result<File> {
    if fs::metadata(path).is_ok_and(|meta| meta.len() > MAX_BYTES) {
        let mut previous = path.as_os_str().to_owned();
        previous.push(".1");
        fs::rename(path, previous)?;
    }
    OpenOptions::new().create(true).append(true).open(path)
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use super::*;

    #[test]
    fn the_log_sits_beside_the_database() {
        let database = Path::new("data").join("kisetsu.db");
        assert_eq!(
            path_beside(&database),
            Some(Path::new("data").join("kisetsu.log"))
        );
    }

    #[test]
    fn a_full_log_is_set_aside_and_a_new_one_started() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("kisetsu.log");
        fs::write(&path, vec![b'x'; usize::try_from(MAX_BYTES).unwrap() + 1]).unwrap();
        let mut log = open(&path).unwrap();
        log.write_all(b"new").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"new");
        let previous = folder.path().join("kisetsu.log.1");
        assert_eq!(fs::metadata(previous).unwrap().len(), MAX_BYTES + 1);
    }

    #[test]
    fn a_log_with_room_is_appended_to() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("kisetsu.log");
        fs::write(&path, b"old ").unwrap();
        open(&path).unwrap().write_all(b"new").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"old new");
    }
}
