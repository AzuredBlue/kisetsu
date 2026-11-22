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

/// The icon's side in pixels.
const ICON_SIZE: u32 = 32;
const CORNER_RADIUS: f32 = 7.0;
const VIOLET: [f32; 3] = [167.0, 139.0, 250.0];
const WHITE: [f32; 3] = [255.0, 255.0, 255.0];
/// Half the thickness of the letter's strokes, in pixels of a 32 pixel icon.
const STROKE_HALF_WIDTH: f32 = 2.6;
/// The letter's strokes as segments on a 32 pixel icon: the stem, then the upper and lower arm.
const STROKES: [([f32; 2], [f32; 2]); 3] = [
    ([11.0, 8.0], [11.0, 24.0]),
    ([12.5, 16.5], [22.0, 8.0]),
    ([14.5, 14.5], [22.5, 24.0]),
];

/// How far the point is from the segment `from`..`to`.
fn distance_to_segment(point: [f32; 2], from: [f32; 2], to: [f32; 2]) -> f32 {
    let along = [to[0] - from[0], to[1] - from[1]];
    let offset = [point[0] - from[0], point[1] - from[1]];
    let length_squared = along[0] * along[0] + along[1] * along[1];
    let t = ((offset[0] * along[0] + offset[1] * along[1]) / length_squared).clamp(0.0, 1.0);
    (point[0] - (from[0] + t * along[0])).hypot(point[1] - (from[1] + t * along[1]))
}

/// The RGBA pixels of a white K on a violet rounded square, `size` pixels on a side. Edges are
/// anti-aliased from how far each pixel centre lies from the shape.
#[cfg_attr(not(windows), allow(dead_code))]
fn icon_rgba(size: u32) -> Vec<u8> {
    let scale = size as f32 / ICON_SIZE as f32;
    let side = size as f32;
    let radius = CORNER_RADIUS * scale;
    let mut rgba = Vec::with_capacity((size * size * 4) as usize);
    for y in 0..size {
        for x in 0..size {
            let (cx, cy) = (x as f32 + 0.5, y as f32 + 0.5);
            let dx = (radius - cx).max(cx - (side - radius));
            let dy = (radius - cy).max(cy - (side - radius));
            let outside = dx.max(0.0).hypot(dy.max(0.0)) - radius;
            let alpha = (0.5 - outside).clamp(0.0, 1.0);
            let point = [cx / scale, cy / scale];
            let letter = STROKES
                .iter()
                .map(|&(from, to)| {
                    let reach = STROKE_HALF_WIDTH - distance_to_segment(point, from, to);
                    (reach * scale + 0.5).clamp(0.0, 1.0)
                })
                .fold(0.0_f32, f32::max);
            for channel in 0..3 {
                let value = VIOLET[channel] + (WHITE[channel] - VIOLET[channel]) * letter;
                rgba.push(value.round() as u8);
            }
            rgba.push((alpha * 255.0).round() as u8);
        }
    }
    rgba
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

    use super::{ICON_SIZE, icon_rgba};
    use windows_sys::Win32::System::Threading::GetCurrentThreadId;
    use windows_sys::Win32::UI::Shell::ShellExecuteW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        DispatchMessageW, GetMessageW, MSG, PostThreadMessageW, SW_SHOWNORMAL, TranslateMessage,
        WM_QUIT,
    };

    fn icon() -> Option<Icon> {
        Icon::from_rgba(icon_rgba(ICON_SIZE), ICON_SIZE, ICON_SIZE).ok()
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
    use super::{ICON_SIZE, Step, browse_url, icon_rgba, step};

    fn pixel(rgba: &[u8], x: u32, y: u32) -> &[u8] {
        let at = ((y * ICON_SIZE + x) * 4) as usize;
        &rgba[at..at + 4]
    }

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
    fn the_icon_is_a_white_k_on_a_violet_square_with_clear_corners() {
        let rgba = icon_rgba(ICON_SIZE);
        assert_eq!(rgba.len(), (ICON_SIZE * ICON_SIZE * 4) as usize);
        // The stem and both arms.
        for (x, y) in [(11, 16), (11, 10), (11, 21), (19, 11), (19, 21)] {
            let [r, g, b, a] = *pixel(&rgba, x, y) else {
                unreachable!()
            };
            assert!(r > 240 && g > 240 && b > 240 && a == 255, "({x}, {y})");
        }
        // The square around the letter, and its corners.
        assert_eq!(pixel(&rgba, 3, 16), [167, 139, 250, 255]);
        for (x, y) in [(0, 0), (31, 0), (0, 31), (31, 31)] {
            assert_eq!(pixel(&rgba, x, y)[3], 0, "({x}, {y})");
        }
    }

    #[test]
    fn an_open_address_is_reached_through_loopback() {
        assert_eq!(browse_url("0.0.0.0", 8085), "http://127.0.0.1:8085/");
        assert_eq!(browse_url("192.168.1.5", 80), "http://192.168.1.5:80/");
    }
}
