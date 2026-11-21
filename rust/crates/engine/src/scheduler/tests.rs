use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::thread::sleep;

use super::*;
use crate::error::Error;
use crate::testing::ManualMonotonic;

fn secs(seconds: u64) -> Duration {
    Duration::from_secs(seconds)
}

fn job(name: &str, interval: Option<Duration>, due: Option<Duration>) -> Job {
    Job {
        name: name.to_owned(),
        run: Arc::new(|| Ok(None)),
        interval,
        due,
        running: false,
        rerun_at: None,
        last_error: None,
        runs: 0,
    }
}

fn state_of(jobs: Vec<Job>) -> State {
    State {
        jobs,
        stopping: false,
    }
}

fn eventually(what: &str, mut condition: impl FnMut() -> bool) {
    for _ in 0..1000 {
        if condition() {
            return;
        }
        sleep(Duration::from_millis(5));
    }
    panic!("gave up waiting for {what}");
}

#[test]
fn a_run_ends_with_the_delay_it_returned_or_else_the_interval() {
    let mut state = state_of(vec![job("a", Some(secs(5)), Some(secs(0)))]);
    assert_eq!(state.ready(secs(0)), Some(0));
    state.begin(0);
    state.finish("a", Ok(Some(Duration::from_millis(10))), secs(0));
    assert_eq!(state.jobs[0].due, Some(Duration::from_millis(10)));
    state.begin(0);
    state.finish("a", Ok(None), secs(1));
    assert_eq!(state.jobs[0].due, Some(secs(6)));
}

#[test]
fn a_job_with_no_interval_waits_to_be_poked_and_delay_moves_it() {
    let mut state = state_of(vec![job("a", None, None)]);
    assert_eq!(state.ready(secs(100)), None);
    assert_eq!(state.next_due(), None);
    state.poke("a", secs(100));
    assert_eq!(state.ready(secs(100)), Some(0));
    state.begin(0);
    state.finish("a", Ok(None), secs(100));
    assert_eq!(state.jobs[0].due, None);
    state.delay("a", secs(120));
    assert_eq!(state.next_due(), Some(secs(120)));
    state.delay("a", secs(110));
    assert_eq!(state.next_due(), Some(secs(110)));
}

#[test]
fn a_job_never_overlaps_itself_and_pokes_during_a_run_rerun_it_once() {
    let mut state = state_of(vec![job("a", None, Some(secs(0)))]);
    state.begin(0);
    for at in [3, 2, 5, 4, 9] {
        state.poke("a", secs(at));
    }
    assert_eq!(state.ready(secs(100)), None);
    assert_eq!(state.next_due(), None);
    state.finish("a", Ok(None), secs(1));
    assert_eq!(state.jobs[0].due, Some(secs(2)));
    assert_eq!(state.ready(secs(2)), Some(0));
    state.begin(0);
    state.finish("a", Ok(None), secs(3));
    assert_eq!(state.jobs[0].due, None);
}

#[test]
fn a_poke_never_moves_a_due_time_later() {
    let mut state = state_of(vec![job("a", None, Some(secs(5)))]);
    state.poke("a", secs(8));
    assert_eq!(state.jobs[0].due, Some(secs(5)));
    state.poke("a", secs(2));
    assert_eq!(state.jobs[0].due, Some(secs(2)));
    state.poke("nobody", secs(0));
}

#[test]
fn a_delay_set_during_a_run_is_replaced_by_the_end_of_the_run() {
    let mut state = state_of(vec![job("a", Some(secs(50)), Some(secs(0)))]);
    state.begin(0);
    state.delay("a", secs(3));
    state.finish("a", Ok(Some(secs(20))), secs(1));
    assert_eq!(state.jobs[0].due, Some(secs(21)));
}

#[test]
fn a_failed_run_is_retried_after_a_minute_and_says_why() {
    let mut state = state_of(vec![
        job("bad", None, Some(secs(0))),
        job("good", Some(secs(2)), Some(secs(0))),
    ]);
    state.begin(0);
    state.begin(1);
    state.finish("bad", Err("no".to_owned()), secs(10));
    state.finish("good", Ok(None), secs(10));
    assert_eq!(state.jobs[0].due, Some(secs(10) + ERROR_RETRY));
    assert_eq!(state.jobs[0].last_error.as_deref(), Some("no"));
    assert_eq!(state.jobs[1].due, Some(secs(12)));
    assert_eq!(state.jobs[1].last_error, None);
    state.begin(0);
    state.finish("bad", Ok(None), secs(80));
    assert_eq!(state.jobs[0].last_error, None);
    assert_eq!(state.jobs[0].runs, 2);
}

#[test]
fn only_a_job_not_running_is_due() {
    let mut state = state_of(vec![
        job("a", None, Some(secs(0))),
        job("b", None, Some(secs(4))),
    ]);
    state.begin(0);
    assert_eq!(state.ready(secs(1)), None);
    assert_eq!(state.next_due(), Some(secs(4)));
    assert_eq!(state.ready(secs(4)), Some(1));
}

#[test]
fn pokes_and_delays_count_from_the_schedulers_clock() {
    let clock = Arc::new(ManualMonotonic::default());
    let scheduler = Scheduler::new(clock.clone());
    scheduler.add("a", Arc::new(|| Ok(None)), None, None);
    scheduler.add("b", Arc::new(|| Ok(None)), None, Some(secs(30)));
    clock.advance(secs(10));
    scheduler.poke("a", secs(5));
    assert_eq!(scheduler.status("a").unwrap().due, Some(secs(15)));
    scheduler.delay("a", secs(2));
    assert_eq!(scheduler.status("a").unwrap().due, Some(secs(12)));
    assert_eq!(scheduler.status("b").unwrap().due, Some(secs(30)));
    assert_eq!(scheduler.status("c"), None);
}

#[test]
fn the_listener_hears_when_a_job_starts_and_ends() {
    let scheduler = Scheduler::default();
    let (sender, heard) = mpsc::channel();
    scheduler.on_change(move |name, running| {
        let _ = sender.send((name.to_owned(), running));
    });
    scheduler.add("work", Arc::new(|| Ok(None)), None, Some(Duration::ZERO));
    scheduler.start();
    let first = heard.recv_timeout(secs(5)).unwrap();
    let second = heard.recv_timeout(secs(5)).unwrap();
    assert_eq!(
        [first, second],
        [("work".to_owned(), true), ("work".to_owned(), false)]
    );
    assert!(scheduler.stop(secs(5)).is_empty());
}

#[test]
fn a_job_that_fails_or_panics_is_retried_and_does_not_stop_the_others() {
    let scheduler = Scheduler::default();
    let good_runs = Arc::new(AtomicUsize::new(0));
    let counted = good_runs.clone();
    scheduler.add(
        "failing",
        Arc::new(|| Err(Error::Stopped)),
        None,
        Some(Duration::ZERO),
    );
    scheduler.add(
        "panicking",
        Arc::new(|| -> JobResult { panic!("boom") }),
        None,
        Some(Duration::ZERO),
    );
    scheduler.add(
        "good",
        Arc::new(move || {
            counted.fetch_add(1, Ordering::SeqCst);
            Ok(None)
        }),
        Some(Duration::from_millis(5)),
        Some(Duration::ZERO),
    );
    scheduler.start();
    eventually("the failures to be recorded", || {
        let failed = |name: &str| {
            scheduler
                .status(name)
                .is_some_and(|s| s.last_error.is_some())
        };
        failed("failing") && failed("panicking") && good_runs.load(Ordering::SeqCst) >= 3
    });
    assert!(scheduler.stop(secs(5)).is_empty());
    let panicked = scheduler.status("panicking").unwrap();
    assert_eq!(panicked.last_error.as_deref(), Some("boom"));
    assert!(panicked.due.is_some_and(|due| due > scheduler.now()));
}

#[test]
fn a_poke_wakes_a_waiting_job_and_pokes_during_a_run_make_one_more_run() {
    let scheduler = Scheduler::default();
    let (started, running) = mpsc::channel();
    let (release, released) = mpsc::channel::<()>();
    let released = Mutex::new(released);
    let runs = Arc::new(AtomicUsize::new(0));
    let counted = runs.clone();
    scheduler.add(
        "a",
        Arc::new(move || {
            counted.fetch_add(1, Ordering::SeqCst);
            let _ = started.send(());
            let _ = locked(&released).recv_timeout(secs(5));
            Ok(None)
        }),
        None,
        None,
    );
    scheduler.start();
    sleep(Duration::from_millis(30));
    assert_eq!(runs.load(Ordering::SeqCst), 0);
    scheduler.poke("a", Duration::ZERO);
    running.recv_timeout(secs(5)).unwrap();
    for _ in 0..5 {
        scheduler.poke("a", Duration::ZERO);
    }
    release.send(()).unwrap();
    running.recv_timeout(secs(5)).unwrap();
    release.send(()).unwrap();
    eventually("the second run to end", || {
        scheduler.status("a").is_some_and(|s| s.runs == 2)
    });
    sleep(Duration::from_millis(30));
    assert_eq!(runs.load(Ordering::SeqCst), 2);
    assert!(scheduler.stop(secs(5)).is_empty());
}

#[test]
fn stopping_reports_the_job_still_running_and_starts_nothing_more() {
    let scheduler = Scheduler::default();
    let (started, running) = mpsc::channel();
    let (release, released) = mpsc::channel::<()>();
    let released = Mutex::new(released);
    scheduler.add(
        "slow",
        Arc::new(move || {
            let _ = started.send(());
            let _ = locked(&released).recv_timeout(secs(5));
            Ok(Some(Duration::ZERO))
        }),
        None,
        Some(Duration::ZERO),
    );
    scheduler.add("waiting", Arc::new(|| Ok(None)), None, Some(secs(3600)));
    scheduler.start();
    running.recv_timeout(secs(5)).unwrap();
    assert_eq!(scheduler.stop(Duration::from_millis(20)), ["slow"]);
    release.send(()).unwrap();
    eventually("the slow job to end", || {
        scheduler.status("slow").is_some_and(|s| !s.running)
    });
    assert_eq!(running.recv_timeout(Duration::from_millis(50)).ok(), None);
    assert_eq!(scheduler.status("waiting").unwrap().runs, 0);
}

#[test]
fn clearing_drops_the_jobs() {
    let scheduler = Scheduler::default();
    scheduler.add("a", Arc::new(|| Ok(None)), None, None);
    scheduler.clear();
    assert_eq!(scheduler.status("a"), None);
}
