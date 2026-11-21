//! The notification-area icon: open the UI, pause all downloads, quit. Windows only; other
//! platforms run without one.

use std::sync::Arc;
use std::sync::mpsc::Sender;

use kisetsu_engine::runner::Engine;

/// The address the browser should use to reach a server bound to `host`.
#[cfg_attr(not(windows), allow(dead_code))]
fn browse_url(host: &str, port: u16) -> String {
    let host = if host == "0.0.0.0" { "127.0.0.1" } else { host };
    format!("http://{host}:{port}/")
}

#[cfg(windows)]
#[allow(unsafe_code)]
mod imp {
    use std::sync::Arc;
    use std::sync::mpsc::{self, Sender};
    use std::thread::JoinHandle;

    use kisetsu_engine::runner::Engine;
    use kisetsu_services::shows;
    use tracing::{info, warn};
    use tray_icon::menu::{Menu, MenuEvent, MenuItem};
    use tray_icon::{Icon, TrayIconBuilder};
    use windows_sys::Win32::System::Threading::GetCurrentThreadId;
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        DispatchMessageW, GetMessageW, MSG, PostThreadMessageW, SW_SHOWNORMAL, TranslateMessage,
        WM_QUIT,
    };

    const ICON_SIZE: u32 = 32;
    const CORNER_RADIUS: f32 = 7.0;

    /// A filled rounded square in the UI's violet.
    fn icon() -> Option<Icon> {
        let mut rgba = Vec::with_capacity((ICON_SIZE * ICON_SIZE * 4) as usize);
        let last = (ICON_SIZE - 1) as f32;
        for y in 0..ICON_SIZE {
            for x in 0..ICON_SIZE {
                let dx = (CORNER_RADIUS - x as f32).max(x as f32 - (last - CORNER_RADIUS));
                let dy = (CORNER_RADIUS - y as f32).max(y as f32 - (last - CORNER_RADIUS));
                let inside = dx.max(0.0).hypot(dy.max(0.0)) <= CORNER_RADIUS;
                let alpha = if inside { 255 } else { 0 };
                rgba.extend_from_slice(&[0xa7, 0x8b, 0xfa, alpha]);
            }
        }
        Icon::from_rgba(rgba, ICON_SIZE, ICON_SIZE).ok()
    }

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn open_in_browser(url: &str) {
        let target = wide(url);
        let verb = wide("open");
        // SAFETY: both strings are NUL-terminated and outlive the call; the other
        // arguments are documented as optional.
        let result = unsafe {
            ShellExecuteW(
                std::ptr::null_mut(),
                verb.as_ptr(),
                target.as_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                SW_SHOWNORMAL,
            )
        };
        // ShellExecuteW reports success with a value above 32.
        if result as usize <= 32 {
            warn!("Cannot open {url} in the browser.");
        }
    }

    /// The tray's thread, which owns the icon and its message pump.
    pub struct Tray {
        thread_id: u32,
        thread: JoinHandle<()>,
    }

    impl Tray {
        pub fn start(
            url: String,
            tooltip: &'static str,
            engine: Arc<Engine>,
            stop: Sender<()>,
        ) -> Option<Self> {
            let (ready, started) = mpsc::channel();
            let thread = std::thread::Builder::new()
                .name("tray".to_owned())
                .spawn(move || pump(&url, tooltip, &engine, &stop, &ready))
                .ok()?;
            match started.recv() {
                Ok(Some(thread_id)) => Some(Self { thread_id, thread }),
                _ => {
                    let _ = thread.join();
                    None
                }
            }
        }

        /// Removes the icon and ends the tray's thread.
        pub fn close(self) {
            // SAFETY: plain call with a thread id and no pointers.
            unsafe { PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0) };
            let _ = self.thread.join();
        }
    }

    /// Builds the icon, reports this thread's id, and serves menu clicks until `WM_QUIT`.
    fn pump(
        url: &str,
        tooltip: &str,
        engine: &Engine,
        stop: &Sender<()>,
        ready: &Sender<Option<u32>>,
    ) {
        let menu = Menu::new();
        let open = MenuItem::new("Open Kisetsu", true, None);
        let pause = MenuItem::new("Pause all downloads", true, None);
        let quit = MenuItem::new("Quit", true, None);
        let built = menu
            .append_items(&[&open, &pause, &quit])
            .ok()
            .zip(icon())
            .and_then(|((), icon)| {
                TrayIconBuilder::new()
                    .with_menu(Box::new(menu))
                    .with_tooltip(tooltip)
                    .with_icon(icon)
                    .build()
                    .ok()
            });
        let Some(_tray) = built else {
            warn!("Cannot create the tray icon; running without it.");
            let _ = ready.send(None);
            return;
        };
        // SAFETY: plain call without arguments.
        let _ = ready.send(Some(unsafe { GetCurrentThreadId() }));

        // SAFETY: MSG is plain data for which all-zero is a valid value.
        let mut message: MSG = unsafe { std::mem::zeroed() };
        // SAFETY: `message` is a valid MSG for the whole loop.
        while unsafe { GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) } > 0 {
            // SAFETY: `message` was filled in by GetMessageW.
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
            while let Ok(event) = MenuEvent::receiver().try_recv() {
                if event.id == *open.id() {
                    open_in_browser(url);
                } else if event.id == *pause.id() {
                    match engine.call(shows::pause_all) {
                        Ok(paused) => info!("Paused {paused} shows from the tray."),
                        Err(error) => warn!("Cannot pause all downloads: {error}"),
                    }
                } else if event.id == *quit.id() {
                    let _ = stop.send(());
                }
            }
        }
    }
}

#[cfg(windows)]
pub use imp::Tray;

/// Shows a tray icon for the app at `host`:`port`, or `None` when there is none to show.
#[cfg(windows)]
pub fn start(
    host: &str,
    port: u16,
    sandbox: bool,
    engine: &Arc<Engine>,
    stop: &Sender<()>,
) -> Option<Tray> {
    let tooltip = if sandbox {
        "Kisetsu (sandbox)"
    } else {
        "Kisetsu"
    };
    Tray::start(
        browse_url(host, port),
        tooltip,
        Arc::clone(engine),
        stop.clone(),
    )
}

/// There is no tray on this platform.
#[cfg(not(windows))]
pub struct Tray;

#[cfg(not(windows))]
impl Tray {
    pub fn close(self) {}
}

#[cfg(not(windows))]
pub fn start(
    _host: &str,
    _port: u16,
    _sandbox: bool,
    _engine: &Arc<Engine>,
    _stop: &Sender<()>,
) -> Option<Tray> {
    None
}

#[cfg(test)]
mod tests {
    use super::browse_url;

    #[test]
    fn an_open_address_is_reached_through_loopback() {
        assert_eq!(browse_url("0.0.0.0", 8085), "http://127.0.0.1:8085/");
        assert_eq!(browse_url("192.168.1.5", 80), "http://192.168.1.5:80/");
    }
}
