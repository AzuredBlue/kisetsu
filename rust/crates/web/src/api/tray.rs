//! Whether the notification-area icon is shown.

use kisetsu_services::context::set_state;
use serde_json::{Value, json};

use super::{checked, ok};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::runtime::{Runtime, TRAY_KEY};

/// The icon as the UI shows it: whether it is up, and whether this run can show it at all.
fn view(runtime: &Runtime) -> Result<Value, ApiError> {
    let available = runtime.tray.available();
    let wanted = runtime.tray_wanted()?;
    Ok(json!({"visible": wanted && available, "available": available}))
}

/// Stores the choice and applies it at once.
fn apply(runtime: &Runtime, visible: bool) -> Result<Value, ApiError> {
    if !runtime.tray.available() {
        return Err(ApiError::Conflict(
            "The tray icon is not available in this run.".to_owned(),
        ));
    }
    runtime.engine.call(move |ctx| {
        set_state(ctx.conn, TRAY_KEY, if visible { "on" } else { "off" })?;
        ctx.emit(&["tray"]);
        Ok(())
    })?;
    // After the store is done: removing the icon waits for its thread, which may be asking the
    // engine for something.
    runtime.tray.set_visible(visible);
    view(runtime)
}

pub(super) fn get(call: &mut Call<'_, '_>) -> ApiResult {
    ok(&view(call.runtime())?)
}

pub(super) fn set(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let mut visible = None;
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        visible = reader.required_flag(body, "visible");
        reader.forbid_others(body, &["visible"]);
    }
    checked(problems)?;
    ok(&apply(call.runtime(), visible.unwrap_or_default())?)
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};

    use jiff::Timestamp;
    use kisetsu_engine::jobs::Jobs;
    use kisetsu_engine::remote::{Remote, WebRemote};
    use kisetsu_engine::runner::{Engine, Parts};
    use kisetsu_engine::scheduler::Scheduler;
    use kisetsu_services::fakes::{FakeClock, FakeQbit};

    use super::{apply, view};
    use crate::error::ApiError;
    use crate::logs::LogRing;
    use crate::runtime::{Runtime, TrayControl};

    /// An icon that only remembers what it was asked.
    struct FakeTray {
        available: bool,
        shown: AtomicBool,
        asked: Mutex<Vec<bool>>,
    }

    impl TrayControl for FakeTray {
        fn available(&self) -> bool {
            self.available
        }

        fn set_visible(&self, visible: bool) {
            self.shown.store(visible, Ordering::SeqCst);
            self.asked.lock().unwrap().push(visible);
        }
    }

    fn runtime(folder: &std::path::Path, available: bool) -> (Runtime, Arc<FakeTray>) {
        let engine = Arc::new(
            Engine::start(
                &folder.join("kisetsu.db"),
                Parts::new(Box::new(|_| {
                    Box::new(FakeQbit::new(FakeClock::new(Timestamp::UNIX_EPOCH)))
                })),
            )
            .unwrap(),
        );
        let scheduler = Scheduler::default();
        let remote: Arc<dyn Remote> =
            Arc::new(WebRemote::new(folder.join("kisetsu.db.userlist.json")));
        let jobs = Jobs::new(Arc::clone(&engine), scheduler.clone(), Arc::clone(&remote));
        let mut runtime = Runtime::new(
            engine,
            scheduler,
            jobs,
            remote,
            Arc::new(LogRing::new()),
            false,
        );
        let tray = Arc::new(FakeTray {
            available,
            shown: AtomicBool::new(false),
            asked: Mutex::new(Vec::new()),
        });
        runtime.tray = Arc::clone(&tray) as Arc<dyn TrayControl>;
        (runtime, tray)
    }

    #[test]
    fn the_icon_is_on_until_turned_off_and_the_choice_is_kept() {
        let folder = tempfile::tempdir().unwrap();
        let (runtime, tray) = runtime(folder.path(), true);
        assert_eq!(
            view(&runtime).unwrap(),
            serde_json::json!({"visible": true, "available": true})
        );

        let off = apply(&runtime, false).unwrap();
        assert_eq!(
            off,
            serde_json::json!({"visible": false, "available": true})
        );
        assert!(!runtime.tray_wanted().unwrap());
        assert!(!tray.shown.load(Ordering::SeqCst));

        let on = apply(&runtime, true).unwrap();
        assert_eq!(on, serde_json::json!({"visible": true, "available": true}));
        assert_eq!(*tray.asked.lock().unwrap(), [false, true]);
        runtime.engine.close();
    }

    #[test]
    fn a_run_without_an_icon_never_shows_one_and_refuses_the_change() {
        let folder = tempfile::tempdir().unwrap();
        let (runtime, tray) = runtime(folder.path(), false);
        assert_eq!(
            view(&runtime).unwrap(),
            serde_json::json!({"visible": false, "available": false})
        );
        assert!(matches!(apply(&runtime, true), Err(ApiError::Conflict(_))));
        assert!(runtime.tray_wanted().unwrap());
        assert!(tray.asked.lock().unwrap().is_empty());
        runtime.engine.close();
    }
}
