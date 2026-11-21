//! One Kisetsu process per database: a lock file next to it.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use fs4::{FileExt, TryLockError};

#[derive(Debug, thiserror::Error)]
pub enum LockError {
    /// Another Kisetsu process holds the lock on this database.
    #[error("Kisetsu is already running on {}", .folder.display())]
    AlreadyRunning { folder: PathBuf },
    #[error("Cannot take the lock file: {0}")]
    Io(#[from] io::Error),
}

/// The lock file of the database at `database`.
pub fn lock_path(database: &Path) -> PathBuf {
    let mut path = OsString::from(database);
    path.push(".lock");
    PathBuf::from(path)
}

/// Holds the lock until it is dropped. The file keeps the process id of the holder.
#[derive(Debug)]
pub struct InstanceLock {
    file: File,
}

impl InstanceLock {
    pub fn acquire(path: &Path) -> Result<Self, LockError> {
        let folder = path
            .parent()
            .filter(|folder| !folder.as_os_str().is_empty());
        if let Some(folder) = folder {
            fs::create_dir_all(folder)?;
        }
        // The file is not emptied before the lock is held: it names the process that holds it.
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        match FileExt::try_lock(&file) {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err(LockError::AlreadyRunning {
                    folder: folder.map(Path::to_path_buf).unwrap_or_default(),
                });
            }
            Err(TryLockError::Error(error)) => return Err(error.into()),
        }
        file.set_len(0)?;
        write!(file, "{}", std::process::id())?;
        file.flush()?;
        Ok(Self { file })
    }
}

impl Drop for InstanceLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.file);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_second_holder_is_refused_until_the_first_lets_go() {
        let folder = tempfile::tempdir().unwrap();
        let path = lock_path(&folder.path().join("kisetsu.db"));
        assert_eq!(path.file_name().unwrap(), "kisetsu.db.lock");
        let first = InstanceLock::acquire(&path).unwrap();
        assert!(matches!(
            InstanceLock::acquire(&path),
            Err(LockError::AlreadyRunning { .. })
        ));
        drop(first);
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            std::process::id().to_string()
        );
        assert!(InstanceLock::acquire(&path).is_ok());
    }

    #[test]
    fn the_folder_of_the_lock_is_created() {
        let folder = tempfile::tempdir().unwrap();
        let path = folder.path().join("not").join("yet").join("k.lock");
        assert!(InstanceLock::acquire(&path).is_ok());
    }
}
