//! Showing a toast with the desktop's own notification service: Windows toasts on Windows, the
//! freedesktop notification service on Linux.

use std::sync::Arc;

use crate::notify::ToastBackend;

#[cfg(target_os = "linux")]
mod linux;
#[cfg(windows)]
mod windows;

/// The name the notifications are shown under.
const APP_NAME: &str = "Kisetsu";

/// The theme icon a Linux toast shows when it has no cover.
#[cfg(any(test, target_os = "linux"))]
const FALLBACK_ICON: &str = "folder-download";

/// The toast backend of this platform; `None` where there is none.
pub fn system_backend() -> Option<Arc<dyn ToastBackend>> {
    #[cfg(windows)]
    return Some(Arc::new(windows::WindowsToasts));
    #[cfg(target_os = "linux")]
    return Some(Arc::new(linux::LinuxToasts));
    #[cfg(not(any(windows, target_os = "linux")))]
    return None;
}

/// What a Linux notification shows beside its text: the cover's file, else a theme icon.
#[cfg(any(test, target_os = "linux"))]
fn linux_icon(cover: Option<&std::path::Path>) -> String {
    cover.map_or_else(
        || FALLBACK_ICON.to_owned(),
        |path| path.to_string_lossy().into_owned(),
    )
}

/// The cover as Windows wants it: an absolute path. `None` when it has no usable path.
#[cfg(any(test, windows))]
fn windows_cover(cover: Option<&std::path::Path>) -> Option<std::path::PathBuf> {
    std::path::absolute(cover?).ok()
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn a_linux_toast_without_a_cover_shows_the_theme_icon() {
        assert_eq!(linux_icon(None), "folder-download");
    }

    #[test]
    fn a_linux_toast_shows_the_cover_file() {
        let cover = Path::new("/home/user/covers/abc.img");
        assert_eq!(linux_icon(Some(cover)), "/home/user/covers/abc.img");
    }

    #[test]
    fn a_windows_cover_path_is_absolute() {
        assert_eq!(windows_cover(None), None);
        let relative = windows_cover(Some(Path::new("covers/abc.img"))).unwrap();
        assert!(relative.is_absolute());
        assert!(relative.ends_with(Path::new("covers").join("abc.img")));
        let absolute = std::env::temp_dir().join("abc.img");
        assert_eq!(windows_cover(Some(&absolute)), Some(absolute));
    }
}
