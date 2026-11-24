//! The notification-area icon: open the UI, pause all downloads, quit. Windows only; other
//! platforms run without one.

use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, PoisonError};

use kisetsu_engine::runner::Engine;
use kisetsu_web::TrayControl;

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
        DispatchMessageW, GetMessageW, GetSystemMetrics, MSG, PostThreadMessageW, SM_CXSMICON,
        SM_CYSMICON, SW_SHOWNORMAL, TranslateMessage, WM_QUIT,
    };

    /// The program's icon, which `build.rs` embeds as resource 1.
    const ICON_RESOURCE: u16 = 1;

    /// The embedded icon at the size Windows uses for small icons on this display, so it picks
    /// the image drawn for that size.
    fn icon() -> Option<Icon> {
        // SAFETY: plain calls with constant arguments.
        let (width, height) =
            unsafe { (GetSystemMetrics(SM_CXSMICON), GetSystemMetrics(SM_CYSMICON)) };
        let size = (u32::try_from(width).ok()?, u32::try_from(height).ok()?);
        info!("Tray icon at {}x{} pixels.", size.0, size.1);
        Icon::from_resource(ICON_RESOURCE, Some(size)).ok()
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

/// What to do with the icon when the choice or the run changes.
#[derive(Debug, PartialEq, Eq)]
enum Step {
    Start,
    Stop,
    Keep,
}

/// The step that takes the icon from `shown` to what is `wanted`. Once the app is `closed`, no
/// icon is started again.
fn step(shown: bool, wanted: bool, closed: bool) -> Step {
    match (shown, wanted && !closed) {
        (false, true) => Step::Start,
        (true, false) => Step::Stop,
        _ => Step::Keep,
    }
}

#[derive(Default)]
struct Shown {
    tray: Option<Tray>,
    closed: bool,
}

/// The icon for one run, which the UI can show and remove while the app runs.
pub struct TrayHandle {
    available: bool,
    host: String,
    port: u16,
    sandbox: bool,
    engine: Arc<Engine>,
    stop: Sender<()>,
    shown: Mutex<Shown>,
}

impl TrayHandle {
    pub fn new(
        available: bool,
        host: &str,
        port: u16,
        sandbox: bool,
        engine: &Arc<Engine>,
        stop: &Sender<()>,
    ) -> Self {
        Self {
            available,
            host: host.to_owned(),
            port,
            sandbox,
            engine: Arc::clone(engine),
            stop: stop.clone(),
            shown: Mutex::new(Shown::default()),
        }
    }

    /// Removes the icon for good; a later request cannot bring it back.
    pub fn close(&self) {
        self.apply(false, true);
    }

    fn apply(&self, wanted: bool, closing: bool) {
        let mut shown = self.shown.lock().unwrap_or_else(PoisonError::into_inner);
        shown.closed |= closing;
        match step(shown.tray.is_some(), wanted, shown.closed) {
            Step::Start => {
                shown.tray = start(
                    &self.host,
                    self.port,
                    self.sandbox,
                    &self.engine,
                    &self.stop,
                );
            }
            Step::Stop => {
                if let Some(tray) = shown.tray.take() {
                    tray.close();
                }
            }
            Step::Keep => {}
        }
    }
}

impl TrayControl for TrayHandle {
    fn available(&self) -> bool {
        self.available
    }

    fn set_visible(&self, visible: bool) {
        if self.available {
            self.apply(visible, false);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Step, browse_url, step};

    /// The icon `build.rs` embeds in the program.
    const ICON: &[u8] = include_bytes!("../../../../kisetsu/web/static/favicon.ico");

    #[test]
    fn the_icon_follows_the_choice_until_the_app_is_closed() {
        assert_eq!(step(false, true, false), Step::Start);
        assert_eq!(step(true, false, false), Step::Stop);
        assert_eq!(step(true, true, false), Step::Keep);
        assert_eq!(step(false, false, false), Step::Keep);
        assert_eq!(step(false, true, true), Step::Keep);
        assert_eq!(step(true, true, true), Step::Stop);
    }

    #[test]
    fn the_icon_holds_an_image_for_each_tray_and_program_size() {
        let read = |at: usize| u16::from_le_bytes([ICON[at], ICON[at + 1]]);
        // An icon file starts with 0, type 1 and its image count, then 16 bytes per image whose
        // first byte is the width (0 meaning 256).
        assert_eq!((read(0), read(2)), (0, 1));
        let sizes: Vec<u32> = (0..usize::from(read(4)))
            .map(|image| match ICON[6 + 16 * image] {
                0 => 256,
                width => u32::from(width),
            })
            .collect();
        assert_eq!(sizes, [16, 20, 24, 32, 48, 256]);
    }

    #[test]
    fn an_open_address_is_reached_through_loopback() {
        assert_eq!(browse_url("0.0.0.0", 8085), "http://127.0.0.1:8085/");
        assert_eq!(browse_url("192.168.1.5", 80), "http://192.168.1.5:80/");
    }
}
