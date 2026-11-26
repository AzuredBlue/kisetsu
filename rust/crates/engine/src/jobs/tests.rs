use std::sync::atomic::Ordering;

use kisetsu_clients::anilist::{AniListError, Deferral, DeferralKind, ListEntry, Schedules};
use kisetsu_clients::qbittorrent::QbitError;
use kisetsu_db::schema::{Download, Feed, FeedState, Show};
use kisetsu_services::backfill::{self, DueOptions};
use kisetsu_services::context::set_state;
use kisetsu_services::fakes::{AniListCall, START, Seed, anilist_show, article};

use super::*;
use crate::bus::RecvError;
use crate::testing::Env;

const TOP: &str = "https://top/rss";
const NYAA: &str = "https://nyaa.si/?page=rss&c=1_2&f=0";
const SUBSPLEASE: &str = "https://subsplease.org/rss/?r=1080";
const DAY: SignedDuration = SignedDuration::from_hours(24);

fn title(number: i64) -> String {
    format!("[Grp] Sousou no Frieren - {number:02} (1080p).mkv")
}

fn at(start: Timestamp, by: SignedDuration) -> Timestamp {
    start.checked_add(by).unwrap()
}

/// AniList lists one show whose newest of `episodes` aired just now; returns when the first aired.
fn airing(env: &Env, episodes: i64) -> Timestamp {
    let first = at(START, DAY * 7 * i32::try_from(1 - episodes).unwrap());
    env.anilist().shows.push(ListEntry {
        total_episodes: Some(12),
        next_airing_episode: Some(episodes + 1),
        next_airing_at: Some(at(START, DAY * 7)),
        ..anilist_show(1, "Sousou no Frieren")
    });
    env.anilist().set_schedule(1, first, episodes, DAY * 7);
    first
}

fn post(env: &Env, url: &str, name: &str, published: Timestamp) {
    env.feeds()
        .post(url, article(name, Some(published), None, None, None));
}

fn full_sync(env: &Env) {
    env.engine
        .call(|ctx| {
            ctx.qbit()?.sync(true)?;
            Ok(())
        })
        .unwrap();
}

fn deferral(retry_in: f64) -> AniListError {
    AniListError::Deferred(Deferral {
        kind: DeferralKind::RateLimited,
        message: "rate limited".to_owned(),
        retry_in,
        partial: Schedules::new(),
    })
}

fn drain(changes: &crate::bus::Subscription) -> Vec<String> {
    let mut seen = Vec::new();
    while let Ok(change) = changes.recv_timeout(Duration::from_millis(20)) {
        seen.extend(change.changed);
    }
    seen
}

fn fetched(env: &Env, url: &str) -> usize {
    env.feeds()
        .fetched()
        .iter()
        .filter(|fetched| *fetched == url)
        .count()
}

fn seasonal_fetches(env: &Env) -> usize {
    env.anilist()
        .calls()
        .iter()
        .filter(|call| matches!(call, AniListCall::FetchUserSeasonalAnime(_)))
        .count()
}

fn unfinished_setup(env: &Env) {
    env.engine
        .call(|ctx| set_state(ctx.conn, "setup_completed", "0"))
        .unwrap();
}

// -- startup and the gates --

#[test]
fn a_fresh_start_adopts_what_qbit_has_then_grabs_the_new_episode_and_finishes_it() {
    let env = Env::new();
    let first = airing(&env, 3);
    let top = env.add_feed("Top", TOP);
    for number in 1..=3 {
        post(
            &env,
            TOP,
            &title(number),
            at(first, DAY * 7 * i32::try_from(number - 1).unwrap()),
        );
    }
    for number in 1..=2 {
        env.qbit().seed(Seed {
            category: "Seasonal".to_owned(),
            ..Seed::new(title(number), "/anime/Sousou no Frieren")
        });
    }
    assert_eq!(env.jobs.startup().unwrap(), None);
    assert!(env.jobs.is_ready());
    assert_eq!(
        env.statuses(1)
            .values()
            .copied()
            .take(3)
            .collect::<Vec<_>>(),
        ["COMPLETED", "COMPLETED", "WANTED"]
    );
    let show = env.show(1);
    assert_eq!(
        (show.feed_id, show.feed_state),
        (Some(top), FeedState::Locked)
    );
    for name in ["grab", "settle", "hunt", "palette"] {
        assert_eq!(env.due_in(name), Some(Duration::ZERO), "{name}");
    }
    assert_eq!(env.jobs.grab().unwrap(), None);
    assert_eq!(env.statuses(1)[&3], "DOWNLOADING");
    let hash = env
        .qbit()
        .truth
        .values()
        .find(|torrent| torrent.name.ends_with(".torrent"))
        .map(|torrent| torrent.hash.clone())
        .unwrap();
    env.qbit().finish(&hash);
    assert_eq!(env.jobs.settle().unwrap(), Some(SETTLE_IDLE));
    assert_eq!(env.statuses(1)[&3], "COMPLETED");
}

#[test]
fn nothing_is_grabbed_before_qbit_has_been_read() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    post(&env, TOP, &title(3), START);
    env.qbit().unavailable = Some(QbitError::Unavailable("down".to_owned()));
    assert_eq!(env.jobs.startup().unwrap(), Some(QBIT_RETRY));
    assert_eq!(env.jobs.grab().unwrap(), None);
    assert_eq!(env.jobs.settle().unwrap(), None);
    assert!(!env.jobs.is_ready());
    let downloads = env
        .engine
        .read(|ctx| Ok(Download::select(ctx.conn, "", [])?.len()))
        .unwrap();
    assert_eq!(downloads, 0);
    assert_eq!(env.jobs.health().qbit.as_deref(), Some("down"));
}

#[test]
fn startup_keeps_anilist_and_the_feeds_current_while_qbit_is_away_and_asks_once() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    env.qbit().unavailable = Some(QbitError::Unavailable("down".to_owned()));
    env.jobs.startup().unwrap();
    for name in ["sync_anilist", "refresh_feeds"] {
        assert_eq!(env.due_in(name), Some(Duration::ZERO), "{name}");
    }
    env.forget_pokes();
    env.jobs.startup().unwrap();
    for name in ["sync_anilist", "refresh_feeds"] {
        assert_ne!(env.due_in(name), Some(Duration::ZERO), "{name}");
    }
}

#[test]
fn nothing_contacts_qbit_or_the_feeds_until_the_wizard_is_done() {
    let env = Env::new();
    unfinished_setup(&env);
    airing(&env, 3);
    env.add_feed("Top", TOP);
    post(&env, TOP, &title(1), START);
    assert_eq!(env.jobs.startup().unwrap(), Some(SETUP_WAIT));
    assert_eq!(env.jobs.refresh_feeds(false).unwrap(), None);
    assert_eq!(env.jobs.hunt().unwrap(), None);
    assert_eq!(env.jobs.housekeeping().unwrap(), None);
    assert!(env.qbit().calls.is_empty());
    assert!(env.anilist().calls().is_empty());
    assert!(env.feeds().fetched().is_empty());
    assert!(!env.jobs.is_ready());
}

#[test]
fn the_ui_hears_when_qbit_goes_down_and_comes_back() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    let changes = env.engine.bus().subscribe();
    env.qbit().unavailable = Some(QbitError::Unavailable("down".to_owned()));
    env.jobs.startup().unwrap();
    assert!(drain(&changes).contains(&"status".to_owned()));
    env.qbit().unavailable = None;
    env.jobs.startup().unwrap();
    assert!(drain(&changes).contains(&"status".to_owned()));
    assert_eq!(env.jobs.health().qbit, None);
    assert_eq!(
        changes.recv_timeout(Duration::from_millis(20)),
        Err(RecvError::Timeout)
    );
}

#[test]
fn startup_books_the_next_anilist_sync_as_nothing_else_wakes_it() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    env.jobs.startup().unwrap();
    assert_eq!(
        env.due_in("sync_anilist"),
        Some(Duration::from_secs(360 * 60))
    );
    assert_eq!(env.due_in("backfill"), Some(BACKFILL_INTERVAL));
}

#[test]
fn the_jobs_are_registered_with_their_first_runs() {
    let env = Env::new();
    assert_eq!(env.due_in("startup"), Some(Duration::ZERO));
    assert_eq!(env.due_in("housekeeping"), Some(HOUSEKEEPING_FIRST));
    for name in [
        "refresh_feeds",
        "hunt",
        "sync_anilist",
        "grab",
        "settle",
        "backfill",
        "palette",
    ] {
        assert_eq!(env.due_in(name), None, "{name}");
    }
}

#[test]
fn a_grab_that_never_reached_qbit_keeps_the_unreachable_warning_until_settle_clears_it() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    env.jobs.startup().unwrap();
    env.qbit().unavailable = Some(QbitError::Unavailable("down".to_owned()));
    assert_eq!(env.jobs.settle().unwrap(), Some(QBIT_RETRY));
    env.jobs.grab().unwrap();
    assert_eq!(env.jobs.health().qbit.as_deref(), Some("down"));
    env.qbit().unavailable = None;
    env.jobs.settle().unwrap();
    assert_eq!(env.jobs.health().qbit, None);
}

// -- feeds --

#[test]
fn a_feed_that_fails_is_marked_and_backs_off_while_healthy_feeds_carry_on() {
    let env = Env::new();
    airing(&env, 3);
    let good = env.add_feed("Good", "https://good/rss");
    let bad = env.add_feed("Bad", "https://bad/rss");
    post(&env, "https://good/rss", &title(3), START);
    env.feeds()
        .fail("https://bad/rss", "bad.example did not answer in time");
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.refresh_feeds(false).unwrap();
    let first = fetched(&env, "https://bad/rss");
    env.jobs.refresh_feeds(false).unwrap();
    assert_eq!((first, fetched(&env, "https://bad/rss")), (1, 1));
    let feeds = env
        .engine
        .read(|ctx| Ok(Feed::select(ctx.conn, "ORDER BY id", [])?))
        .unwrap();
    let by_id = |id: i64| feeds.iter().find(|feed| feed.id == Some(id)).unwrap();
    assert!(by_id(good).last_ok_at.is_some() && by_id(good).failing_since.is_none());
    assert_eq!(by_id(bad).failing_since, Some(START));
    assert!(
        by_id(bad)
            .last_error
            .as_deref()
            .is_some_and(|error| error.contains("bad.example did not answer in time"))
    );
    assert_eq!(env.due_in("grab"), Some(Duration::ZERO));
}

#[test]
fn a_routine_read_skips_an_idle_feed_but_startup_and_a_manual_refresh_read_it() {
    let env = Env::new();
    env.add_feed("Top", TOP);
    assert_eq!(env.jobs.startup().unwrap(), None);
    let once = fetched(&env, TOP);
    env.jobs.refresh_feeds(false).unwrap();
    let idle = fetched(&env, TOP);
    env.jobs.request_refresh();
    assert_eq!(env.due_in("refresh_feeds"), Some(Duration::ZERO));
    env.jobs.refresh_feeds(false).unwrap();
    assert_eq!((once, idle, fetched(&env, TOP)), (1, 1, 2));
    assert!(!locked(&env.jobs.state).refresh_all);
}

#[test]
fn with_nothing_missing_the_routine_read_sleeps_until_a_feed_is_idle_and_the_hunt_reports_no_read()
{
    let env = Env::new();
    env.anilist()
        .shows
        .push(anilist_show(1, "Sousou no Frieren"));
    env.add_feed("Top", TOP);
    env.jobs.startup().unwrap();
    assert_eq!(
        env.jobs.refresh_feeds(false).unwrap(),
        Some(Duration::from_secs(6 * 3600))
    );
    assert!(env.jobs.hunt().unwrap().is_some());
    assert_eq!(env.jobs.hunt_read_at(), None);
}

#[test]
fn the_hunt_polls_while_a_show_airs_and_otherwise_sleeps_until_the_next_one() {
    let env = Env::new();
    let first = airing(&env, 1);
    env.add_feed("Top", TOP);
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    post(&env, TOP, &title(1), first);
    assert_eq!(env.jobs.hunt().unwrap(), Some(Duration::from_secs(5 * 60)));
    assert_eq!(
        env.jobs.hunt_read_at(),
        Some(at(START, SignedDuration::from_mins(5)))
    );
    env.advance(DAY * 2);
    assert_eq!(env.jobs.hunt().unwrap(), Some(HUNT_IDLE_MAX));
    assert_eq!(fetched(&env, TOP), 1);
}

#[test]
fn the_hunt_reports_when_it_will_next_read_even_past_its_hourly_recheck() {
    let env = Env::new();
    env.anilist().shows.push(ListEntry {
        total_episodes: Some(12),
        next_airing_episode: Some(1),
        next_airing_at: Some(at(START, DAY * 3)),
        ..anilist_show(1, "Sousou no Frieren")
    });
    env.add_feed("Top", TOP);
    env.jobs.startup().unwrap();
    assert_eq!(env.jobs.hunt().unwrap(), Some(HUNT_IDLE_MAX));
    // A premiere is looked for early by the early-air setting.
    assert_eq!(
        env.jobs.hunt_read_at(),
        Some(at(START, DAY * 3 - SignedDuration::from_hours(6)))
    );
}

#[test]
fn a_read_that_misses_what_was_stored_asks_for_a_search() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    post(&env, TOP, &title(3), START);
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.ready.store(true, Ordering::Release);
    env.jobs.refresh_feeds(false).unwrap();
    post(&env, TOP, &title(9), START);
    post(&env, TOP, &title(10), START);
    env.feeds()
        .items
        .entry(TOP.to_owned())
        .and_modify(|items| items.truncate(2));
    env.advance(SignedDuration::from_mins(15));
    env.jobs.refresh_feeds(false).unwrap();
    assert!(locked(&env.jobs.state).backfill_all);
    assert_eq!(env.due_in("backfill"), Some(GAP_BACKFILL_DELAY));
}

#[test]
fn two_reads_of_the_feeds_never_run_at_once() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    *locked(&env.remote.read_time) = Duration::from_millis(40);
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    std::thread::scope(|scope| {
        for _ in 0..3 {
            scope.spawn(|| env.jobs.refresh_feeds(true).unwrap());
        }
    });
    assert_eq!(env.remote.most_reading.load(Ordering::SeqCst), 1);
    assert_eq!(fetched(&env, TOP), 3);
}

#[test]
fn feeds_are_read_in_parallel_within_one_pass() {
    let env = Env::new();
    for index in 0..3 {
        env.add_feed(&format!("F{index}"), &format!("https://f{index}/rss"));
    }
    *locked(&env.remote.read_time) = Duration::from_millis(60);
    env.jobs.refresh_feeds(true).unwrap();
    assert!(env.remote.most_reading.load(Ordering::SeqCst) > 1);
}

#[test]
fn a_feed_read_teaches_how_many_results_its_sites_pages_hold() {
    let env = Env::new();
    env.add_feed("Nyaa", NYAA);
    for number in 1..=4 {
        post(&env, NYAA, &title(number), START);
    }
    env.jobs.refresh_feeds(true).unwrap();
    env.jobs
        .with_grouping(|grouping| assert_eq!(grouping.page_size("nyaa.si"), 4));
}

// -- AniList --

#[test]
fn anilist_deferring_keeps_the_shows_and_asks_to_be_called_again_later() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    env.anilist().error = Some(deferral(90.0));
    assert_eq!(
        env.jobs.sync_anilist(false).unwrap(),
        Duration::from_secs(90)
    );
    assert_eq!(env.show(1).anilist_id, 1);
    let health = env.jobs.health();
    assert!(
        health
            .anilist
            .as_deref()
            .is_some_and(|text| text.contains("rate limited"))
    );
    assert_eq!(health.anilist_retry_in, Some(90.0));
    env.anilist().error = Some(deferral(5.0));
    assert_eq!(env.jobs.sync_anilist(false).unwrap(), SYNC_RETRY_FLOOR);
}

#[test]
fn a_failed_sync_keeps_the_last_known_airing_pointer_and_retries_in_five_minutes() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    let before = env.show(1);
    env.anilist().error = Some(AniListError::GraphQl("offline".to_owned()));
    assert_eq!(env.jobs.sync_anilist(false).unwrap(), ANILIST_ERROR_RETRY);
    let after = env.show(1);
    assert_eq!(
        (after.next_airing_episode, after.next_airing_at),
        (before.next_airing_episode, before.next_airing_at)
    );
    assert_eq!(after.next_airing_episode, Some(4));
    assert!(
        env.jobs
            .health()
            .anilist
            .is_some_and(|text| text.contains("offline"))
    );
    env.anilist().error = None;
    env.jobs.sync_anilist(false).unwrap();
    assert_eq!(env.jobs.health().anilist, None);
    assert_eq!(env.jobs.health().anilist_retry_in, None);
}

#[test]
fn a_manual_sync_ignores_the_refresh_window_that_a_startup_sync_respects() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.sync_anilist(true).unwrap();
    let skipped = seasonal_fetches(&env);
    env.jobs.sync_anilist(false).unwrap();
    assert_eq!((skipped, seasonal_fetches(&env)), (1, 2));
}

#[test]
fn a_sync_at_startup_that_is_within_the_window_waits_out_the_rest_of_it() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    env.advance(SignedDuration::from_mins(60));
    assert_eq!(
        env.jobs.sync_anilist(true).unwrap(),
        Duration::from_secs(300 * 60)
    );
}

#[test]
fn without_an_anilist_user_the_job_waits_for_the_settings() {
    let env = Env::new();
    env.change_settings(|settings| settings.anilist_username.clear());
    assert_eq!(env.jobs.sync_anilist(false).unwrap(), NO_USER_WAIT);
    assert!(env.anilist().calls().is_empty());
}

#[test]
fn shows_owed_a_full_schedule_when_anilist_defers_it_are_fetched_on_the_next_pass() {
    let env = Env::new();
    env.anilist().shows.push(ListEntry {
        next_airing_episode: Some(30),
        next_airing_at: Some(at(START, DAY * 7)),
        ..anilist_show(1, "Long Show")
    });
    env.anilist()
        .set_schedule(1, at(START, DAY * -196), 29, DAY * 7);
    locked(&env.remote.media_deferrals).push_back(deferral(30.0));
    assert_eq!(
        env.jobs.sync_anilist(false).unwrap(),
        Duration::from_secs(30)
    );
    env.advance(SignedDuration::from_secs(60));
    env.jobs.refresh_schedules().unwrap();
    assert_eq!(env.remote.media_calls.lock().unwrap().len(), 2);
    let undated = env
        .engine
        .read(|ctx| {
            Ok(ctx.conn.query_row(
                "SELECT count(*) FROM episodes WHERE number <= 3 AND air_at IS NULL",
                [],
                |row| row.get::<_, i64>(0),
            )?)
        })
        .unwrap();
    assert_eq!(undated, 0);
}

// -- downloads --

#[test]
fn settle_waits_the_shorter_of_its_pace_and_the_next_due_time() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Top", TOP);
    env.jobs.startup().unwrap();
    // No torrents and nothing due: the idle pace.
    assert_eq!(env.jobs.settle().unwrap(), Some(SETTLE_IDLE));
}

#[test]
fn a_burst_of_pokes_runs_the_job_at_most_once_a_second() {
    let env = Env::new();
    let ahead = |env: &Env| env.due_in("grab");
    env.jobs.poke_soon("grab", BURST_GAP);
    assert_eq!(ahead(&env), Some(Duration::ZERO));
    env.jobs.poke_soon("grab", BURST_GAP);
    assert_eq!(ahead(&env), Some(Duration::ZERO));
    // The first poke is still to run: later pokes join it instead of asking for more.
    env.jobs.poke_soon("settle", BURST_GAP);
    env.ticks.advance(Duration::from_millis(100));
    env.jobs.poke_soon("settle", BURST_GAP);
    assert_eq!(env.due_in("settle"), Some(Duration::ZERO));
    // Once that run happened, the next poke waits out the rest of the second.
    env.scheduler.delay("settle", Duration::from_secs(1_000));
    env.ticks.advance(Duration::from_millis(100));
    env.jobs.poke_soon("settle", BURST_GAP);
    assert_eq!(env.due_in("settle"), Some(Duration::from_millis(800)));
    // A lone poke long afterwards runs at once.
    env.ticks.advance(Duration::from_secs(10));
    env.scheduler.delay("settle", Duration::from_secs(1_000));
    env.jobs.poke_soon("settle", BURST_GAP);
    assert_eq!(env.due_in("settle"), Some(Duration::ZERO));
}

// -- past releases --

#[test]
fn backfill_finds_a_lost_episode_and_the_next_grab_takes_it() {
    let env = Env::new();
    let first = airing(&env, 3);
    env.add_feed("Nyaa", NYAA);
    env.feeds().searchable.insert(
        NYAA.to_owned(),
        vec![article(
            &title(2),
            Some(at(first, DAY * 7)),
            None,
            None,
            None,
        )],
    );
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.ready.store(true, Ordering::Release);
    assert_eq!(env.jobs.backfill().unwrap(), None);
    assert!(!env.feeds().searched().is_empty());
    assert_eq!(env.due_in("grab"), Some(Duration::ZERO));
    env.jobs.grab().unwrap();
    assert_eq!(env.statuses(1)[&2], "DOWNLOADING");
}

#[test]
fn what_the_searches_learned_of_a_site_outlives_a_restart() {
    let env = Env::new();
    env.jobs.with_grouping(|grouping| {
        grouping.saw("nyaa.si", 75);
        grouping.record("nyaa.si", true);
    });
    let restarted = Jobs::new(
        env.engine.clone(),
        env.scheduler.clone(),
        env.remote.clone(),
    );
    env.jobs.backfill().unwrap();
    restarted.backfill().unwrap();
    restarted.with_grouping(|grouping| {
        assert_eq!(grouping.works("nyaa.si"), Some(true));
        assert!(grouping.complete("nyaa.si", 74));
    });
}

#[test]
fn a_first_search_checks_the_site_with_one_show_and_the_others_share_one_search() {
    let env = Env::new();
    env.add_feed("Nyaa", NYAA);
    let names = [
        ("Sousou no Frieren", "Frieren"),
        ("Kusuriya no Hitorigoto", "Apothecary Diaries"),
        ("Hotaru no Yomeiri", "Firefly Wedding"),
    ];
    let first = at(START, DAY * -14);
    for (index, (romaji, english)) in names.iter().enumerate() {
        let id = i64::try_from(index + 1).unwrap();
        env.anilist().shows.push(ListEntry {
            title_romaji: (*romaji).to_owned(),
            aliases: vec![(*romaji).to_owned(), (*english).to_owned()],
            total_episodes: Some(12),
            next_airing_episode: Some(4),
            next_airing_at: Some(at(START, DAY * 7)),
            ..anilist_show(id, english)
        });
        env.anilist().set_schedule(id, first, 3, DAY * 7);
    }
    // The feed lists only other shows by now; its site still finds the three.
    for number in 0..20 {
        post(
            &env,
            NYAA,
            &format!("[Grp] Another Show {number} - 01 (1080p).mkv"),
            START,
        );
    }
    let found: Vec<_> = names
        .iter()
        .flat_map(|(romaji, _)| {
            (1..=3).map(move |n| {
                let published = at(first, DAY * 7 * (n - 1));
                article(
                    &format!("[Grp] {romaji} - {n:02} (1080p).mkv"),
                    Some(published),
                    None,
                    None,
                    None,
                )
            })
        })
        .collect();
    env.feeds().searchable.insert(NYAA.to_owned(), found);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.refresh_feeds(true).unwrap();
    env.jobs.backfill().unwrap();
    // One show asks for each of its two names and then for both at once, which checks the site;
    // the other two share one search.
    assert_eq!(env.feeds().searched().len(), 4);
    let stored = env
        .engine
        .read(|ctx| {
            Ok(ctx.conn.query_row(
                "SELECT count(*) FROM feed_items WHERE title NOT LIKE '%Another Show%'",
                [],
                |row| row.get::<_, i64>(0),
            )?)
        })
        .unwrap();
    assert_eq!(stored, 9);
}

#[test]
fn a_show_deleted_before_its_search_ran_is_skipped() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Nyaa", NYAA);
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    let due = env
        .engine
        .call(|ctx| backfill::due(ctx, DueOptions::default(), None))
        .unwrap();
    assert!(!due.is_empty());
    env.engine
        .call(|ctx| {
            ctx.conn.execute("DELETE FROM episodes", [])?;
            ctx.conn.execute("DELETE FROM shows", [])?;
            Ok(())
        })
        .unwrap();
    assert_eq!(env.jobs.search(due, false).unwrap(), 0);
    assert!(env.feeds().searched().is_empty());
}

/// Six shows, each with one name, all missing episodes on one searchable feed; returns what is due.
fn six_shows_due(env: &Env) -> Vec<backfill::Due> {
    env.add_feed("Nyaa", NYAA);
    env.feeds().searchable.insert(NYAA.to_owned(), Vec::new());
    let first = at(START, DAY * -14);
    for id in 1..=6 {
        let name = format!("Show Number {id}");
        env.anilist().shows.push(ListEntry {
            total_episodes: Some(12),
            next_airing_episode: Some(4),
            next_airing_at: Some(at(START, DAY * 7)),
            ..anilist_show(id, &name)
        });
        env.anilist().set_schedule(id, first, 3, DAY * 7);
    }
    full_sync(env);
    env.jobs.sync_anilist(false).unwrap();
    let due = env
        .engine
        .call(|ctx| backfill::due(ctx, DueOptions::default(), None))
        .unwrap();
    assert_eq!(due.len(), 6);
    assert!(due.iter().all(|item| item.terms.len() == 1));
    due
}

#[test]
fn up_to_four_shows_are_searched_at_the_same_time() {
    let env = Env::new();
    let due = six_shows_due(&env);
    // Each search waits for three others, so they only get on if four really overlap.
    env.remote.search_meeting.store(4, Ordering::SeqCst);
    env.jobs.search(due, false).unwrap();
    assert_eq!(env.remote.searches_begun(), 6);
    assert_eq!(env.remote.most_searching(), SEARCH_CONCURRENCY);
}

#[test]
fn a_site_cutting_a_search_short_stops_the_shows_not_yet_started() {
    let env = Env::new();
    let due = six_shows_due(&env);
    env.remote.search_meeting.store(4, Ordering::SeqCst);
    env.remote.search_rate_limited.store(true, Ordering::SeqCst);
    assert_eq!(env.jobs.search(due, false).unwrap(), 0);
    assert_eq!(env.remote.searches_begun(), SEARCH_CONCURRENCY);
}

#[test]
fn the_subsplease_schedule_is_read_once_and_only_with_a_subsplease_feed() {
    let env = Env::new();
    *locked(&env.remote.schedule) = Some(vec!["Sousou no Frieren".to_owned()]);
    env.jobs.read_schedule().unwrap();
    assert_eq!(env.remote.schedule_reads.load(Ordering::SeqCst), 0);
    env.add_feed("SubsPlease", SUBSPLEASE);
    env.jobs.read_schedule().unwrap();
    env.jobs.read_schedule().unwrap();
    assert_eq!(env.remote.schedule_reads.load(Ordering::SeqCst), 1);
    env.advance(SignedDuration::from_mins(61));
    env.jobs.read_schedule().unwrap();
    assert_eq!(env.remote.schedule_reads.load(Ordering::SeqCst), 2);
}

#[test]
fn requests_for_a_search_are_taken_up_by_the_next_backfill_run() {
    let env = Env::new();
    airing(&env, 3);
    env.add_feed("Nyaa", NYAA);
    full_sync(&env);
    env.jobs.sync_anilist(false).unwrap();
    env.jobs.request_backfill(Some(&[1]));
    assert_eq!(env.due_in("backfill"), Some(Duration::ZERO));
    assert_eq!(locked(&env.jobs.state).backfill_requests.len(), 1);
    env.jobs.backfill().unwrap();
    assert!(locked(&env.jobs.state).backfill_requests.is_empty());
    assert!(!locked(&env.jobs.state).backfill_all);
}

// -- upkeep --

#[test]
fn housekeeping_and_palette_run_without_a_qbit_or_images() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    assert_eq!(env.jobs.housekeeping().unwrap(), None);
    assert_eq!(env.jobs.palette().unwrap(), None);
}

#[test]
fn housekeeping_that_cannot_reach_qbit_is_not_a_failure() {
    let env = Env::new();
    env.qbit().unavailable = Some(QbitError::Unavailable("down".to_owned()));
    assert_eq!(env.jobs.housekeeping().unwrap(), None);
}

#[test]
fn the_colours_of_every_show_are_stored_together_as_one_change() {
    let env = Env::new();
    airing(&env, 3);
    env.anilist().shows.push(ListEntry {
        total_episodes: Some(12),
        ..anilist_show(2, "Kusuriya no Hitorigoto")
    });
    env.jobs.sync_anilist(false).unwrap();
    env.engine
        .call(|ctx| {
            ctx.conn.execute(
                "UPDATE shows SET cover_image = 'https://s4.anilist.co/' || id || '.jpg'",
                [],
            )?;
            Ok(())
        })
        .unwrap();
    let changes = env.engine.bus().subscribe();
    env.jobs.palette().unwrap();
    assert_eq!(env.remote.hue_calls.lock().unwrap().len(), 2);
    let mut messages = Vec::new();
    while let Ok(change) = changes.recv_timeout(Duration::from_millis(20)) {
        messages.push(change.changed);
    }
    assert_eq!(messages.len(), 1);
    assert!(messages[0].contains(&"shows".to_owned()));
    let stored = env
        .engine
        .read(|ctx| Ok(Show::select(ctx.conn, "", [])?.len()))
        .unwrap();
    assert_eq!(stored, 2);
}

#[test]
fn a_cover_that_could_not_be_read_is_tried_again_next_time() {
    let env = Env::new();
    airing(&env, 3);
    env.jobs.sync_anilist(false).unwrap();
    env.engine
        .call(|ctx| {
            ctx.conn.execute(
                "UPDATE shows SET cover_image = 'https://s4.anilist.co/1.jpg'",
                [],
            )?;
            Ok(())
        })
        .unwrap();
    env.remote.hues_fail.store(true, Ordering::SeqCst);
    env.jobs.palette().unwrap();
    env.remote.hues_fail.store(false, Ordering::SeqCst);
    env.jobs.palette().unwrap();
    // The first pass stored nothing, so the second asked again; the third finds it done.
    env.jobs.palette().unwrap();
    assert_eq!(env.remote.hue_calls.lock().unwrap().len(), 2);
}

#[test]
fn a_search_for_a_new_show_is_kept_until_the_show_is_added() {
    let env = Env::new();
    env.add_feed("Nyaa", NYAA);
    let entry = ListEntry {
        total_episodes: Some(12),
        ..anilist_show(7, "Sousou no Frieren")
    };
    env.feeds().searchable.insert(
        NYAA.to_owned(),
        vec![article(&title(1), Some(START), None, None, None)],
    );

    let searched = env.jobs.search_new_show(&entry).unwrap();

    assert_eq!(searched.preview.anilist_id, 7);
    assert!(
        env.jobs
            .found_for(7)
            .iter()
            .any(|(_, found)| !found.is_empty())
    );
    assert!(env.jobs.found_for(8).is_empty());
    env.jobs.forget_found(7);
    assert!(env.jobs.found_for(7).is_empty());
}

#[test]
fn holding_the_jobs_starts_them_over_as_after_a_restart() {
    let env = Env::new();
    env.jobs.ready.store(true, Ordering::Release);
    locked(&env.jobs.state).health.grab = Some("boom".to_owned());
    env.scheduler.start();

    let during = env
        .jobs
        .while_stopped(|| Ok(env.scheduler.is_started()))
        .unwrap();

    assert!(!during);
    assert!(env.scheduler.is_started());
    assert!(!env.jobs.is_ready());
    assert_eq!(env.jobs.health(), Health::default());
    env.scheduler.stop(Duration::from_secs(1));
}

#[test]
fn a_job_still_running_after_the_grace_refuses_the_work_and_the_jobs_go_on() {
    let env = Env::new();
    env.jobs.ready.store(true, Ordering::Release);
    let (began, began_rx) = std::sync::mpsc::channel();
    let (release, release_rx) = std::sync::mpsc::channel::<()>();
    let (began, release_rx) = (Mutex::new(began), Mutex::new(release_rx));
    env.scheduler.add(
        "slow",
        Arc::new(move || {
            let _ = locked(&began).send(());
            let _ = locked(&release_rx).recv_timeout(Duration::from_secs(10));
            Ok(None)
        }),
        None,
        Some(Duration::ZERO),
    );
    env.scheduler.start();
    began_rx.recv_timeout(Duration::from_secs(5)).unwrap();

    let mut worked = false;
    let refused = env
        .jobs
        .while_stopped_within(Duration::from_millis(100), || {
            worked = true;
            Ok(())
        })
        .unwrap_err();

    assert!(!worked);
    assert!(matches!(&refused, Error::JobsRunning(jobs) if *jobs == ["slow"]));
    assert!(refused.to_string().contains("slow"));
    assert!(env.scheduler.is_started());
    assert!(env.jobs.is_ready());
    release.send(()).unwrap();
    assert!(env.scheduler.stop(Duration::from_secs(5)).is_empty());
}

/// A `.torrent` file holding `files` at the top level.
fn torrent_of(files: &[String]) -> Vec<u8> {
    let list: String = files
        .iter()
        .map(|name| format!("d6:lengthi100e4:pathl{}:{name}ee", name.len()))
        .collect();
    format!("d4:infod5:filesl{list}e4:name4:Show12:piece lengthi16384eee").into_bytes()
}

#[test]
fn a_batch_next_to_files_on_disk_is_counted_by_its_files_and_replaces_the_older_ones() {
    use kisetsu_clients::anilist::MediaStatus;
    use kisetsu_services::addshow;
    use kisetsu_services::context::get_state;

    let env = Env::new();
    let dir = tempfile::tempdir().unwrap();
    let folder = dir.path().join("Grand Blue");
    std::fs::create_dir(&folder).unwrap();
    let disk: Vec<String> = (1..=12)
        .map(|n| format!("[SubsPlease] Grand Blue - {n:02} (1080p) [A13D69{n:02}].mkv"))
        .collect();
    for name in &disk {
        std::fs::write(folder.join(name), "v1").unwrap();
    }
    let root = dir
        .path()
        .to_string_lossy()
        .replace(std::path::MAIN_SEPARATOR, "/");
    let base = format!("{root}/{{name}}");
    env.change_settings(move |settings| settings.base_dir = base);
    let feed_id = env.add_feed("Nyaa", NYAA);
    let batch_url = "https://nyaa.si/download/2179602.torrent";
    let batch = article(
        "[SubsPlease] Grand Blue (01-12) (1080p) [Batch]",
        Some(START),
        None,
        None,
        Some(batch_url),
    );
    let key = format!("{feed_id}:{}", batch.guid);
    env.feeds()
        .searchable
        .insert(NYAA.to_owned(), vec![batch.clone()]);
    let listed: Vec<String> = (1..=12)
        .map(|n| {
            if n <= 5 {
                format!("[SubsPlease] Grand Blue - {n:02}v2 (1080p) [96D6C3{n:02}].mkv")
            } else {
                disk[n - 1].clone()
            }
        })
        .collect();
    env.remote
        .torrents
        .lock()
        .unwrap()
        .insert(batch_url.to_owned(), torrent_of(&listed));
    let entry = ListEntry {
        total_episodes: Some(12),
        status: MediaStatus::Finished,
        ..anilist_show(7, "Grand Blue")
    };

    let searched = env.jobs.search_new_show(&entry).unwrap();

    let offer = &searched.preview.feeds[0].batches[0];
    assert_eq!(offer.item_key, key);
    assert_eq!(
        (
            offer.files_known,
            offer.on_disk,
            offer.newer.len(),
            offer.to_download
        ),
        (true, 7, 5, 0)
    );
    assert_eq!(env.jobs.torrent_files_for(7)[&key], listed);

    let (found, files) = (env.jobs.found_for(7), env.jobs.torrent_files_for(7));
    let keys = vec![key];
    env.engine
        .call(move |ctx| addshow::confirm(ctx, &entry, feed_id, &keys, &found, &files))
        .unwrap();
    let waiting = env
        .engine
        .read(|ctx| {
            Ok(ctx
                .conn
                .prepare("SELECT key FROM app_state WHERE key LIKE 'older_files:%'")?
                .query_map([], |row| row.get::<_, String>(0))?
                .collect::<Result<Vec<_>, _>>()?)
        })
        .unwrap();
    assert_eq!(waiting.len(), 1, "{waiting:?}");
    let value = env
        .engine
        .read(|ctx| get_state(ctx.conn, &waiting[0], ""))
        .unwrap();
    for name in &disk[..5] {
        assert!(value.contains(name), "{value}");
    }
    // The files the batch holds too are listed only as the batch's own, never to delete.
    let files = value.split("\"batch_files\"").next().unwrap();
    assert!(!files.contains(&disk[5]), "{value}");

    env.jobs.ready.store(true, Ordering::Release);
    env.jobs.synced.store(true, Ordering::Release);
    env.jobs.settle().unwrap();
    assert!(folder.join(&disk[0]).exists());
    let hash = env.qbit().truth.keys().next().cloned().unwrap();
    env.qbit().finish(&hash);
    env.jobs.settle().unwrap();
    for (index, name) in disk.iter().enumerate() {
        assert_eq!(folder.join(name).exists(), index >= 5, "{name}");
    }
}

/// A show whose folder holds its twelve single-episode files, offered a batch with newer
/// versions of episodes 1 to 5. Torrent 1 is a finished one Kisetsu takes over, torrent 2 one
/// it leaves alone.
struct Replacement {
    env: Env,
    folder: std::path::PathBuf,
    disk: Vec<String>,
    entry: ListEntry,
    feed_id: i64,
    key: String,
    batch_url: &'static str,
    _dir: tempfile::TempDir,
}

fn replacement() -> Replacement {
    use kisetsu_clients::anilist::MediaStatus;
    use kisetsu_services::downloads::MANAGED_TAG;

    let env = Env::new();
    let dir = tempfile::tempdir().unwrap();
    let folder = dir.path().join("Grand Blue");
    std::fs::create_dir(&folder).unwrap();
    let disk: Vec<String> = (1..=12)
        .map(|n| format!("[SubsPlease] Grand Blue - {n:02} (1080p) [A13D69{n:02}].mkv"))
        .collect();
    for name in &disk {
        std::fs::write(folder.join(name), "v1").unwrap();
    }
    let root = dir
        .path()
        .to_string_lossy()
        .replace(std::path::MAIN_SEPARATOR, "/");
    let base = format!("{root}/{{name}}");
    env.change_settings(move |settings| settings.base_dir = base);
    let feed_id = env.add_feed("Nyaa", NYAA);
    let batch_url = "https://nyaa.si/download/2179602.torrent";
    let batch = article(
        "[SubsPlease] Grand Blue (01-12) (1080p) [Batch]",
        Some(START),
        None,
        None,
        Some(batch_url),
    );
    let key = format!("{feed_id}:{}", batch.guid);
    env.feeds().searchable.insert(NYAA.to_owned(), vec![batch]);
    let listed: Vec<String> = (1..=12)
        .map(|n| {
            if n <= 5 {
                format!("[SubsPlease] Grand Blue - {n:02}v2 (1080p) [96D6C3{n:02}].mkv")
            } else {
                disk[n - 1].clone()
            }
        })
        .collect();
    env.remote
        .torrents
        .lock()
        .unwrap()
        .insert(batch_url.to_owned(), torrent_of(&listed));
    let save = folder.to_string_lossy().into_owned();
    env.qbit().seed(Seed {
        tags: [MANAGED_TAG.to_owned()].into(),
        ..Seed::new(disk[0].clone(), save.clone())
    });
    env.qbit().seed(Seed::new(disk[1].clone(), save));
    full_sync(&env);
    let entry = ListEntry {
        total_episodes: Some(12),
        status: MediaStatus::Finished,
        ..anilist_show(7, "Grand Blue")
    };
    Replacement {
        env,
        folder,
        disk,
        entry,
        feed_id,
        key,
        batch_url,
        _dir: dir,
    }
}

impl Replacement {
    /// Searches, then adds the show from the batch.
    fn add(&self) {
        use kisetsu_services::addshow;

        self.env.jobs.search_new_show(&self.entry).unwrap();
        let (entry, feed_id) = (self.entry.clone(), self.feed_id);
        let found = self.env.jobs.found_for(7);
        let files = self.env.jobs.torrent_files_for(7);
        let keys = vec![self.key.clone()];
        self.env
            .engine
            .call(move |ctx| addshow::confirm(ctx, &entry, feed_id, &keys, &found, &files))
            .unwrap();
        self.env.jobs.ready.store(true, Ordering::Release);
        self.env.jobs.synced.store(true, Ordering::Release);
    }

    fn batch_hash(&self) -> String {
        let url = self.batch_url;
        self.env
            .qbit()
            .truth
            .values()
            .find(|torrent| url.ends_with(&torrent.name))
            .map(|torrent| torrent.hash.clone())
            .unwrap()
    }

    fn downloads(&self) -> Vec<Download> {
        self.env
            .engine
            .read(|ctx| Ok(Download::select(ctx.conn, "ORDER BY id", [])?))
            .unwrap()
    }

    fn wait(&self) -> String {
        use kisetsu_services::context::get_state;

        self.env
            .engine
            .read(|ctx| {
                let key: Option<String> = ctx
                    .conn
                    .prepare("SELECT key FROM app_state WHERE key LIKE 'older_files:%'")?
                    .query_map([], |row| row.get(0))?
                    .next()
                    .transpose()?;
                get_state(ctx.conn, &key.unwrap_or_default(), "")
            })
            .unwrap()
    }
}

#[test]
fn a_batch_replaces_a_taken_over_torrent_and_deletes_loose_older_files_but_not_held_ones() {
    use kisetsu_db::schema::DownloadState;

    let r = replacement();
    r.add();

    let downloads = r.downloads();
    let adopted = downloads.iter().find(|dl| dl.adopted).unwrap();
    let replacement = downloads
        .iter()
        .find(|dl| dl.supersedes_id.is_some())
        .unwrap();
    assert_eq!(replacement.supersedes_id, adopted.id);
    assert_eq!(adopted.state, DownloadState::Done);
    // The taken over torrent's file is the replacement's to deal with, not a loose file.
    let wait = r.wait();
    assert!(!wait.contains(&r.disk[0]), "{wait}");
    for name in &r.disk[1..5] {
        assert!(wait.contains(name), "{wait}");
    }

    r.env.jobs.settle().unwrap();
    assert!(
        r.env
            .qbit()
            .truth
            .contains_key(&adopted.info_hash.clone().unwrap())
    );
    let batch = r.batch_hash();
    r.env.qbit().finish(&batch);
    r.env.jobs.settle().unwrap();

    let old_hash = adopted.info_hash.clone().unwrap();
    assert!(r.env.qbit().deleted_with_files.contains(&old_hash));
    assert!(
        r.downloads()
            .iter()
            .any(|dl| dl.id == adopted.id && dl.state == DownloadState::Superseded)
    );
    // Loose files of episodes 3 to 5 are deleted; the one a torrent in qBittorrent holds stays,
    // as do the files the batch holds too.
    for (index, name) in r.disk.iter().enumerate() {
        let gone = (2..5).contains(&index);
        assert_eq!(r.folder.join(name).exists(), !gone, "{name}");
    }
}

#[test]
fn a_replacement_that_gives_up_leaves_the_files_of_the_batch_in_place() {
    use kisetsu_db::schema::DownloadState;

    let r = replacement();
    r.add();
    r.env.jobs.settle().unwrap();
    let old_hash = r
        .downloads()
        .iter()
        .find(|dl| dl.adopted)
        .and_then(|dl| dl.info_hash.clone())
        .unwrap();

    r.env.advance(SignedDuration::from_hours(49));
    r.env.jobs.settle().unwrap();

    assert!(r.env.qbit().deleted_with_files.is_empty());
    assert!(r.env.qbit().truth.contains_key(&old_hash));
    assert!(
        r.downloads()
            .iter()
            .any(|dl| dl.supersedes_id.is_some() && dl.state == DownloadState::Canceled)
    );
    for name in &r.disk {
        assert!(r.folder.join(name).exists(), "{name}");
    }
}
