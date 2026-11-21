use std::collections::{BTreeMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::time::Duration;

use kisetsu_db::schema::Feed;
use kisetsu_services::context::MatcherSet;
use kisetsu_services::fakes::FakeQbit;
use kisetsu_services::{Error as ServiceError, feeds};

use super::*;
use crate::bus::RecvError;
use crate::testing::SharedQbit;

const WAIT: Duration = Duration::from_secs(5);

/// An engine on a database file, whose qBittorrent is a fake that records the hosts it was built for.
struct Fixture {
    engine: Engine,
    hosts: Arc<Mutex<Vec<String>>>,
    path: PathBuf,
    _folder: tempfile::TempDir,
}

fn fixture() -> Fixture {
    let folder = tempfile::tempdir().unwrap();
    let path = folder.path().join("kisetsu.db");
    let hosts = Arc::new(Mutex::new(Vec::new()));
    let fake = Arc::new(Mutex::new(FakeQbit::new(Default::default())));
    let built = Arc::clone(&hosts);
    let mut parts = Parts::new(Box::new(move |settings| {
        locked(&built).push(settings.qbit_host.clone());
        Box::new(SharedQbit::new(&fake))
    }));
    parts.bus = ChangeBus::new(Duration::ZERO);
    Fixture {
        engine: Engine::start(&path, parts).unwrap(),
        hosts,
        path,
        _folder: folder,
    }
}

fn feed_names(ctx: &mut Context<'_>) -> kisetsu_services::Result<Vec<String>> {
    Ok(Feed::select(ctx.conn, "ORDER BY id", [])?
        .into_iter()
        .map(|feed| feed.name)
        .collect())
}

#[test]
fn every_call_runs_on_the_one_engine_thread_one_at_a_time_and_commits() {
    let Fixture { engine, .. } = fixture();
    let engine = Arc::new(engine);
    let active = Arc::new(AtomicUsize::new(0));
    let peak = Arc::new(AtomicUsize::new(0));
    let callers: Vec<_> = (0..5)
        .map(|index| {
            let (engine, active, peak) = (engine.clone(), active.clone(), peak.clone());
            thread::spawn(move || {
                engine
                    .call(move |ctx| {
                        let now = active.fetch_add(1, Ordering::SeqCst) + 1;
                        peak.fetch_max(now, Ordering::SeqCst);
                        let name = format!("f{index}");
                        feeds::add_feed(ctx, &name, &format!("https://{name}/rss"), None)?;
                        active.fetch_sub(1, Ordering::SeqCst);
                        Ok(thread::current().name().map(str::to_owned))
                    })
                    .unwrap()
            })
        })
        .collect();
    for caller in callers {
        assert_eq!(caller.join().unwrap().as_deref(), Some("kisetsu-engine"));
    }
    assert_eq!(peak.load(Ordering::SeqCst), 1);
    let mut names = engine.read(feed_names).unwrap();
    names.sort();
    assert_eq!(names, ["f0", "f1", "f2", "f3", "f4"]);
}

#[test]
fn an_error_rolls_back_drops_the_caches_and_leaves_the_engine_usable() {
    let Fixture { engine, .. } = fixture();
    engine
        .call(|ctx| {
            ctx.caches.known_items = Some(HashSet::from([(1, "x".to_owned())]));
            Ok(())
        })
        .unwrap();
    let kept = engine
        .call(|ctx| Ok(ctx.caches.known_items.is_some()))
        .unwrap();
    assert!(kept);
    let changes = engine.bus().subscribe();
    let failed = engine.call(|ctx| {
        feeds::add_feed(ctx, "Lost", "https://lost/rss", None)?;
        ctx.caches.matchers = Some(Arc::new(MatcherSet {
            signatures: BTreeMap::new(),
            matchers: Vec::new(),
        }));
        ctx.emit(&["feeds"]);
        Err::<(), _>(ServiceError::NoQbit)
    });
    assert!(matches!(failed, Err(Error::Service(ServiceError::NoQbit))));
    let (names, known, matchers) = engine
        .call(|ctx| {
            let names = feed_names(ctx)?;
            Ok((
                names,
                ctx.caches.known_items.is_some(),
                ctx.caches.matchers.is_some(),
            ))
        })
        .unwrap();
    assert_eq!((names.is_empty(), known, matchers), (true, false, false));
    // What a failed job touched is published all the same: it may have committed part of its work.
    assert_eq!(changes.recv_timeout(WAIT).unwrap().changed, ["feeds"]);
}

#[test]
fn a_panic_is_an_error_like_any_other_and_the_engine_goes_on() {
    let Fixture { engine, .. } = fixture();
    let failed = engine.call(|ctx| -> kisetsu_services::Result<()> {
        feeds::add_feed(ctx, "Lost", "https://lost/rss", None)?;
        panic!("boom");
    });
    match failed {
        Err(Error::Panicked(message)) => assert_eq!(message, "boom"),
        other => panic!("expected a panic error, got {other:?}"),
    }
    assert!(engine.call(feed_names).unwrap().is_empty());
}

#[test]
fn topics_are_published_once_the_work_is_committed() {
    let Fixture { engine, .. } = fixture();
    let changes = engine.bus().subscribe();
    engine
        .call(|ctx| {
            feeds::add_feed(ctx, "A", "https://a/rss", None)?;
            ctx.emit(&["feeds", "status"]);
            Ok(())
        })
        .unwrap();
    let change = changes.recv_timeout(WAIT).unwrap();
    assert_eq!(
        (change.changed, change.rev),
        (vec!["feeds".into(), "status".into()], 1)
    );
    engine.call(|_| Ok(())).unwrap();
    assert_eq!(
        changes.recv_timeout(Duration::from_millis(50)),
        Err(RecvError::Timeout)
    );
}

#[test]
fn the_qbit_connection_follows_the_settings_and_is_reused() {
    let Fixture { engine, hosts, .. } = fixture();
    engine.call(|_| Ok(())).unwrap();
    engine.call(|_| Ok(())).unwrap();
    assert_eq!(locked(&hosts).len(), 1);
    engine
        .call(|ctx| {
            ctx.settings.qbit_host = "http://other:8080".to_owned();
            ctx.settings.update(ctx.conn)?;
            Ok(())
        })
        .unwrap();
    engine.call(|_| Ok(())).unwrap();
    let hosts = locked(&hosts).clone();
    assert_eq!(hosts.len(), 2);
    assert_eq!(hosts[1], "http://other:8080");
}

#[test]
fn every_write_context_can_tag_a_torrent_it_adds() {
    let Fixture { engine, .. } = fixture();
    let tags = engine
        .call(|ctx| Ok((ctx.next_download_tag()?, ctx.next_download_tag()?)))
        .unwrap();
    for tag in [&tags.0, &tags.1] {
        let digits = tag.strip_prefix("kisetsu-op-").unwrap();
        assert!(digits.len() == 32 && digits.chars().all(|c| c.is_ascii_hexdigit()));
    }
    assert_ne!(tags.0, tags.1);
}

#[test]
fn a_read_sees_committed_rows_saves_nothing_and_cannot_reach_qbit() {
    let Fixture { engine, .. } = fixture();
    engine
        .call(|ctx| feeds::add_feed(ctx, "A", "https://a/rss", None).map(drop))
        .unwrap();
    let seen = engine
        .read(|ctx| {
            let names = feed_names(ctx)?;
            feeds::add_feed(ctx, "B", "https://b/rss", None)?;
            Ok((names, feed_names(ctx)?))
        })
        .unwrap();
    assert_eq!(
        seen,
        (vec!["A".to_owned()], vec!["A".to_owned(), "B".to_owned()])
    );
    assert!(matches!(
        engine.read(|ctx| ctx.qbit().map(|_| ())),
        Err(Error::Service(ServiceError::NoQbit))
    ));
    assert_eq!(engine.call(feed_names).unwrap(), ["A"]);
}

#[test]
fn a_dry_run_runs_on_the_engine_thread_and_saves_nothing() {
    let Fixture { engine, .. } = fixture();
    let on_engine_thread = engine
        .dry_run(|ctx| {
            feeds::add_feed(ctx, "A", "https://a/rss", None)?;
            assert_eq!(feed_names(ctx)?, ["A"]);
            Ok(thread::current().name().map(str::to_owned))
        })
        .unwrap();
    assert_eq!(on_engine_thread.as_deref(), Some("kisetsu-engine"));
    assert!(matches!(
        engine.dry_run(|ctx| ctx.qbit().map(|_| ())),
        Err(Error::Service(ServiceError::NoQbit))
    ));
    assert!(engine.call(feed_names).unwrap().is_empty());
}

#[test]
fn a_read_is_given_what_the_last_pass_learned_of_stalled_and_carried() {
    let Fixture { engine, .. } = fixture();
    engine
        .call(|ctx| {
            ctx.caches.stalled = HashSet::from([7]);
            ctx.caches.carried.insert(3, true);
            Ok(())
        })
        .unwrap();
    let (stalled, carried, known) = engine
        .read(|ctx| {
            Ok((
                ctx.caches.stalled.clone(),
                ctx.caches.carried.clone(),
                ctx.caches.known_items.is_some(),
            ))
        })
        .unwrap();
    assert_eq!(
        (stalled, carried.get(&3).copied(), known),
        (HashSet::from([7]), Some(true), false)
    );
}

#[test]
fn a_read_does_not_wait_for_the_engine_thread() {
    let Fixture { engine, .. } = fixture();
    let engine = Arc::new(engine);
    engine
        .call(|ctx| feeds::add_feed(ctx, "A", "https://a/rss", None).map(drop))
        .unwrap();
    let (started, running) = mpsc::channel();
    let (release, released) = mpsc::channel::<()>();
    let busy = {
        let engine = engine.clone();
        thread::spawn(move || {
            engine.call(move |_| {
                let _ = started.send(());
                let _ = released.recv_timeout(WAIT);
                Ok(())
            })
        })
    };
    running.recv_timeout(WAIT).unwrap();
    assert_eq!(engine.read(feed_names).unwrap(), ["A"]);
    release.send(()).unwrap();
    busy.join().unwrap().unwrap();
}

#[test]
fn the_caller_named_in_the_request_counts_follows_the_work_onto_the_engine_thread() {
    let Fixture { engine, .. } = fixture();
    let _caller = traffic::caller("jobs.grab");
    let seen = engine.call(|_| Ok(traffic::current_caller())).unwrap();
    assert_eq!(seen, "jobs.grab");
}

#[test]
fn a_closed_engine_refuses_work_ends_its_subscriptions_and_leaves_the_database_closed() {
    let Fixture { engine, path, .. } = fixture();
    let changes = engine.bus().subscribe();
    engine.call(|_| Ok(())).unwrap();
    engine.stop();
    assert!(matches!(engine.call(|_| Ok(())), Err(Error::Stopped)));
    assert!(matches!(engine.read(|_| Ok(())), Err(Error::Stopped)));
    engine.close();
    engine.close();
    assert_eq!(changes.recv_timeout(WAIT), Err(RecvError::Ended));
    assert!(matches!(engine.call(|_| Ok(())), Err(Error::Stopped)));
    drop(engine);
    // Nothing holds the database any more, so its files can go.
    assert!(kisetsu_db::store::open(&path).is_ok());
}
