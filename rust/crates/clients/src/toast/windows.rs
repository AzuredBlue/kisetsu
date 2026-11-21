use tauri_winrt_notification::{IconCrop, Toast};

use super::{APP_NAME, windows_cover};
use crate::notify::{Toast as Message, ToastBackend};

/// Shows toasts through WinRT.
///
/// The program is not installed with an AppUserModelID of its own, so the toast is sent under
/// PowerShell's and Windows names PowerShell as its source.
pub(super) struct WindowsToasts;

impl ToastBackend for WindowsToasts {
    fn show(&self, message: &Message<'_>) -> Result<(), String> {
        let mut toast = Toast::new(Toast::POWERSHELL_APP_ID)
            .title(message.title)
            .text1(message.body);
        if let Some(cover) = windows_cover(message.cover) {
            toast = toast.icon(&cover, IconCrop::Square, APP_NAME);
        }
        toast.show().map_err(|error| error.to_string())
    }
}
