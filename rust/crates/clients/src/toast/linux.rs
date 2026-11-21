use notify_rust::Notification;

use super::{APP_NAME, linux_icon};
use crate::notify::{Toast, ToastBackend};

/// Shows toasts through the notification service on the session bus.
pub(super) struct LinuxToasts;

impl ToastBackend for LinuxToasts {
    fn show(&self, toast: &Toast<'_>) -> Result<(), String> {
        Notification::new()
            .appname(APP_NAME)
            .summary(toast.title)
            .body(toast.body)
            .icon(&linux_icon(toast.cover))
            .show()
            .map(drop)
            .map_err(|error| error.to_string())
    }
}
