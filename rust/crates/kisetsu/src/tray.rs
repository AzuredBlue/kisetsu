//! The notification-area icon: open the UI, pause all downloads, quit. On Windows it is a
//! shell icon; on Linux a StatusNotifierItem, which KDE Plasma shows natively. Other platforms
//! run without one.

use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex, PoisonError};

use kisetsu_engine::runner::Engine;
use kisetsu_web::TrayControl;

/// The program's icon file, which also holds the tray's images.
#[cfg(any(test, target_os = "linux"))]
const ICON: &[u8] = include_bytes!("../../../../kisetsu/web/static/favicon.ico");

/// The address the browser should use to reach a server bound to `host`.
#[cfg_attr(not(any(windows, target_os = "linux")), allow(dead_code))]
fn browse_url(host: &str, port: u16) -> String {
    let host = if host == "0.0.0.0" { "127.0.0.1" } else { host };
    format!("http://{host}:{port}/")
}

/// Pauses every show, as the menu's "Pause all downloads" asks.
#[cfg(any(windows, target_os = "linux"))]
fn pause_all(engine: &Engine) {
    use kisetsu_services::shows;
    use tracing::{info, warn};

    match engine.call(shows::pause_all) {
        Ok(paused) => info!("Paused {paused} shows from the tray."),
        Err(error) => warn!("Cannot pause all downloads: {error}"),
    }
}

/// The 32-bit bitmap images in an icon file, each as its side and RGBA rows from the top. PNG
/// images and anything malformed are left out.
#[cfg(any(test, target_os = "linux"))]
fn icon_images(ico: &[u8]) -> Vec<(u32, Vec<u8>)> {
    let u16_at = |at: usize| Some(u16::from_le_bytes(ico.get(at..at + 2)?.try_into().ok()?));
    let u32_at = |at: usize| Some(u32::from_le_bytes(ico.get(at..at + 4)?.try_into().ok()?));
    let image = |entry: usize| -> Option<(u32, Vec<u8>)> {
        let size = usize::try_from(u32_at(entry + 8)?).ok()?;
        let offset = usize::try_from(u32_at(entry + 12)?).ok()?;
        let data = ico.get(offset..offset.checked_add(size)?)?;
        // A bitmap image starts with its header's length, then its width and doubled height
        // (colour rows and the mask), then at byte 14 its bits per pixel.
        let header = usize::try_from(u32::from_le_bytes(data.get(..4)?.try_into().ok()?)).ok()?;
        let side = u32::from_le_bytes(data.get(4..8)?.try_into().ok()?);
        let bits = u16::from_le_bytes(data.get(14..16)?.try_into().ok()?);
        if header < 40 || bits != 32 || side == 0 {
            return None;
        }
        let row = usize::try_from(side).ok()? * 4;
        let pixels = data.get(header..header + row * usize::try_from(side).ok()?)?;
        // Rows run from the bottom up, each pixel blue, green, red, alpha.
        let rgba = pixels
            .chunks_exact(row)
            .rev()
            .flat_map(|line| line.chunks_exact(4))
            .flat_map(|pixel| [pixel[2], pixel[1], pixel[0], pixel[3]])
            .collect();
        Some((side, rgba))
    };
    if (u16_at(0), u16_at(2)) != (Some(0), Some(1)) {
        return Vec::new();
    }
    (0..usize::from(u16_at(4).unwrap_or(0)))
        .filter_map(|index| image(6 + 16 * index))
        .collect()
}

#[cfg(windows)]
#[allow(unsafe_code)]
mod imp {
    use std::sync::Arc;
    use std::sync::mpsc::{self, Sender};
    use std::thread::JoinHandle;

    use kisetsu_engine::runner::Engine;
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
                    super::pause_all(engine);
                } else if event.id == *quit.id() {
                    let _ = stop.send(());
                }
            }
        }
    }
}

#[cfg(target_os = "linux")]
mod imp {
    use std::process::Command;
    use std::sync::Arc;
    use std::sync::mpsc::Sender;

    use kisetsu_engine::runner::Engine;
    use ksni::blocking::{Handle, TrayMethods};
    use ksni::menu::StandardItem;
    use tracing::warn;

    use super::{ICON, icon_images};

    fn open_in_browser(url: &str) {
        match Command::new("xdg-open").arg(url).spawn() {
            // xdg-open can wait for the browser it starts, so it is waited for elsewhere.
            Ok(mut child) => {
                std::thread::spawn(move || child.wait());
            }
            Err(error) => warn!("Cannot open {url} in the browser: {error}"),
        }
    }

    /// The icon's state, which the tray service reads and the menu acts on.
    struct Item {
        url: String,
        tooltip: &'static str,
        engine: Arc<Engine>,
        stop: Sender<()>,
        icons: Vec<ksni::Icon>,
    }

    impl ksni::Tray for Item {
        fn id(&self) -> String {
            "kisetsu".to_owned()
        }

        fn title(&self) -> String {
            self.tooltip.to_owned()
        }

        /// Every size drawn for the icon, so the panel picks the one nearest its own.
        fn icon_pixmap(&self) -> Vec<ksni::Icon> {
            self.icons.clone()
        }

        fn tool_tip(&self) -> ksni::ToolTip {
            ksni::ToolTip {
                title: self.tooltip.to_owned(),
                ..ksni::ToolTip::default()
            }
        }

        /// A left click opens the UI.
        fn activate(&mut self, _x: i32, _y: i32) {
            open_in_browser(&self.url);
        }

        fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
            vec![
                StandardItem {
                    label: "Open Kisetsu".to_owned(),
                    activate: Box::new(|item: &mut Self| open_in_browser(&item.url)),
                    ..StandardItem::default()
                }
                .into(),
                StandardItem {
                    label: "Pause all downloads".to_owned(),
                    activate: Box::new(|item: &mut Self| super::pause_all(&item.engine)),
                    ..StandardItem::default()
                }
                .into(),
                StandardItem {
                    label: "Quit".to_owned(),
                    activate: Box::new(|item: &mut Self| {
                        let _ = item.stop.send(());
                    }),
                    ..StandardItem::default()
                }
                .into(),
            ]
        }
    }

    /// The icon's D-Bus service, which runs on its own thread.
    pub struct Tray {
        handle: Handle<Item>,
    }

    impl Tray {
        pub fn start(
            url: String,
            tooltip: &'static str,
            engine: Arc<Engine>,
            stop: Sender<()>,
        ) -> Option<Self> {
            // The service wants each pixel as alpha, red, green, blue.
            let icons = icon_images(ICON)
                .into_iter()
                .filter_map(|(side, mut data)| {
                    data.chunks_exact_mut(4)
                        .for_each(|pixel| pixel.rotate_right(1));
                    let side = i32::try_from(side).ok()?;
                    Some(ksni::Icon {
                        width: side,
                        height: side,
                        data,
                    })
                })
                .collect();
            let item = Item {
                url,
                tooltip,
                engine,
                stop,
                icons,
            };
            // A panel that is not up yet, as when the app starts with the session, is waited
            // for instead of failing the icon.
            match item.assume_sni_available(true).spawn() {
                Ok(handle) => Some(Self { handle }),
                Err(error) => {
                    warn!("Cannot create the tray icon; running without it: {error}");
                    None
                }
            }
        }

        /// Removes the icon and ends its service.
        pub fn close(self) {
            self.handle.shutdown().wait();
        }
    }
}

#[cfg(any(windows, target_os = "linux"))]
pub use imp::Tray;

/// Shows a tray icon for the app at `host`:`port`, or `None` when there is none to show.
#[cfg(any(windows, target_os = "linux"))]
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
#[cfg(not(any(windows, target_os = "linux")))]
pub struct Tray;

#[cfg(not(any(windows, target_os = "linux")))]
impl Tray {
    pub fn close(self) {}
}

#[cfg(not(any(windows, target_os = "linux")))]
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
    use super::{ICON, Step, browse_url, icon_images, step};

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
    fn the_bitmaps_read_as_a_white_k_on_the_blue_tile_with_clear_corners() {
        let images = icon_images(ICON);
        let sides: Vec<u32> = images.iter().map(|(side, _)| *side).collect();
        assert_eq!(sides, [16, 20, 24, 32, 48]);
        let (_, rgba) = &images[0];
        let pixel = |x: usize, y: usize| &rgba[(y * 16 + x) * 4..(y * 16 + x) * 4 + 4];
        // The top row is read first, the K's stem is white and the tile is the palette's blue.
        assert_eq!(pixel(0, 0)[3], 0);
        assert_eq!(pixel(5, 8), [255, 255, 255, 255]);
        assert_eq!(pixel(14, 8), [0x42, 0x53, 0xA3, 255]);
    }

    #[test]
    fn a_damaged_icon_file_has_no_images() {
        assert!(icon_images(&[]).is_empty());
        assert!(icon_images(&ICON[..40]).is_empty());
    }

    #[test]
    fn an_open_address_is_reached_through_loopback() {
        assert_eq!(browse_url("0.0.0.0", 8085), "http://127.0.0.1:8085/");
        assert_eq!(browse_url("192.168.1.5", 80), "http://192.168.1.5:80/");
    }
}
