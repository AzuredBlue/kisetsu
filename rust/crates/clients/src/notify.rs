//! Desktop notifications when an episode downloads; never fails and never blocks the caller.
//!
//! Showing a toast is the platform's business: a [`ToastBackend`] does it. This module fetches the
//! show's cover, keeps it on disk and hands the backend the finished message.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use sha2::{Digest, Sha256};

use crate::palette::{ImageSource, is_allowed_image_url};

/// What the covers are counted as in the request counts.
const NOTIFICATION_COVER: &str = "Notification cover";
/// Characters of the address's digest that name its cover file.
const NAME_LENGTH: usize = 24;

/// A notification ready to show.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Toast<'a> {
    pub title: &'a str,
    pub body: &'a str,
    /// The show's cover on disk, when there is one.
    pub cover: Option<&'a Path>,
}

/// Shows toasts on the desktop.
pub trait ToastBackend: Send + Sync {
    fn show(&self, toast: &Toast<'_>) -> Result<(), String>;
}

/// Sends notifications from a background thread.
pub struct Notifier {
    images: Arc<dyn ImageSource>,
    cover_dir: PathBuf,
    backend: Option<Arc<dyn ToastBackend>>,
}

impl Notifier {
    /// A notifier keeping covers in `cover_dir`. Without a `backend` there is nothing to show
    /// the toast with, and notifications are skipped.
    pub fn new(
        images: Arc<dyn ImageSource>,
        cover_dir: PathBuf,
        backend: Option<Arc<dyn ToastBackend>>,
    ) -> Self {
        Self {
            images,
            cover_dir,
            backend,
        }
    }

    /// Shows a notification from a background thread and returns at once.
    pub fn notify(self: &Arc<Self>, title: &str, body: &str, cover_url: Option<&str>) {
        let notifier = Arc::clone(self);
        let (title, body, cover_url) = (
            title.to_owned(),
            body.to_owned(),
            cover_url.map(str::to_owned),
        );
        let spawned = std::thread::Builder::new()
            .name("notify".into())
            .spawn(move || notifier.deliver(&title, &body, cover_url.as_deref()));
        if let Err(error) = spawned {
            tracing::debug!("Desktop notification failed: {error}");
        }
    }

    /// Shows a notification, waiting for the cover to be fetched.
    pub fn deliver(&self, title: &str, body: &str, cover_url: Option<&str>) {
        let Some(backend) = &self.backend else {
            tracing::debug!("No notification backend available; skipping desktop notification");
            return;
        };
        let cover = cover_url.and_then(|address| self.cached_cover(address));
        let toast = Toast {
            title,
            body,
            cover: cover.as_deref(),
        };
        if let Err(error) = backend.show(&toast) {
            tracing::debug!("Desktop notification failed: {error}");
        }
    }

    /// The cover as a local file, downloaded once; `None` when it can't be had.
    pub fn cached_cover(&self, address: &str) -> Option<PathBuf> {
        if !is_allowed_image_url(address) {
            return None;
        }
        let path = self.cover_dir.join(cover_file_name(address));
        if path.exists() {
            return Some(path);
        }
        let bytes = match self.images.get(address, NOTIFICATION_COVER) {
            Ok(bytes) => bytes,
            Err(error) => {
                tracing::debug!("Could not cache cover {address}: {error}");
                return None;
            }
        };
        match store(&self.cover_dir, &path, &bytes) {
            Ok(()) => Some(path),
            Err(error) => {
                tracing::debug!("Could not cache cover {address}: {error}");
                None
            }
        }
    }
}

/// The name of the file a cover is kept in.
pub fn cover_file_name(address: &str) -> String {
    let digest = Sha256::digest(address.as_bytes());
    let mut name: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    name.truncate(NAME_LENGTH);
    name.push_str(".img");
    name
}

/// Writes the file whole or not at all: two episodes of a show can finish together.
fn store(folder: &Path, path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    static WRITES: AtomicU64 = AtomicU64::new(0);
    std::fs::create_dir_all(folder)?;
    let temporary = path.with_extension(format!("{}.tmp", WRITES.fetch_add(1, Ordering::Relaxed)));
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&temporary);
    })
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use crate::palette::ImageError;

    use super::*;

    struct Covers {
        asked: Mutex<Vec<String>>,
        bytes: Option<Vec<u8>>,
    }

    impl ImageSource for Covers {
        fn get(&self, address: &str, _kind: &str) -> Result<Vec<u8>, ImageError> {
            self.asked.lock().unwrap().push(address.to_owned());
            self.bytes.clone().ok_or(ImageError::TooLarge)
        }
    }

    #[derive(Default)]
    struct Shown(Mutex<Vec<(String, String, Option<PathBuf>)>>);

    impl ToastBackend for Shown {
        fn show(&self, toast: &Toast<'_>) -> Result<(), String> {
            self.0.lock().unwrap().push((
                toast.title.to_owned(),
                toast.body.to_owned(),
                toast.cover.map(Path::to_path_buf),
            ));
            Ok(())
        }
    }

    const COVER: &str = "https://s4.anilist.co/file/cover.jpg";

    #[test]
    fn cover_files_keep_the_names_of_the_existing_cache() {
        assert_eq!(
            cover_file_name(
                "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx1.png"
            ),
            "e494aef49b503ab481a20dc9.img"
        );
    }

    fn folder(name: &str) -> PathBuf {
        let folder =
            std::env::temp_dir().join(format!("kisetsu-notify-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&folder);
        folder
    }

    fn notifier(name: &str, bytes: Option<&[u8]>) -> (Notifier, Arc<Covers>, Arc<Shown>) {
        let covers = Arc::new(Covers {
            asked: Mutex::default(),
            bytes: bytes.map(<[u8]>::to_vec),
        });
        let shown = Arc::new(Shown::default());
        let notifier = Notifier::new(
            Arc::clone(&covers) as Arc<dyn ImageSource>,
            folder(name),
            Some(Arc::clone(&shown) as Arc<dyn ToastBackend>),
        );
        (notifier, covers, shown)
    }

    #[test]
    fn a_cover_is_fetched_once_and_shown_from_disk() {
        let (notifier, covers, shown) = notifier("once", Some(b"image"));
        notifier.deliver("Show - Episode 3", "Download finished", Some(COVER));
        notifier.deliver("Show - Episode 4", "Download finished", Some(COVER));
        assert_eq!(covers.asked.lock().unwrap().len(), 1);
        let shown = shown.0.lock().unwrap();
        let path = shown[0].2.as_ref().unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"image");
        assert_eq!(shown[1].2.as_ref(), Some(path));
        assert!(path.file_name().unwrap().to_string_lossy().len() == NAME_LENGTH + ".img".len());
    }

    #[test]
    fn a_cover_that_cannot_be_had_still_gets_the_message_shown() {
        let (notifier, covers, shown) = notifier("missing", None);
        notifier.deliver("a", "b", Some(COVER));
        notifier.deliver("c", "d", Some("https://evil.example/cover.jpg"));
        notifier.deliver("e", "f", None);
        assert_eq!(covers.asked.lock().unwrap().len(), 1);
        assert!(
            shown
                .0
                .lock()
                .unwrap()
                .iter()
                .all(|(_, _, cover)| cover.is_none())
        );
        assert_eq!(shown.0.lock().unwrap().len(), 3);
    }

    #[test]
    fn without_a_backend_nothing_is_fetched() {
        let covers = Arc::new(Covers {
            asked: Mutex::default(),
            bytes: Some(b"image".to_vec()),
        });
        let notifier = Notifier::new(
            Arc::clone(&covers) as Arc<dyn ImageSource>,
            folder("none"),
            None,
        );
        notifier.deliver("a", "b", Some(COVER));
        assert!(covers.asked.lock().unwrap().is_empty());
    }
}
