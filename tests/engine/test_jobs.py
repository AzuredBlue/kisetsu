import asyncio
import logging
from datetime import timedelta

from sqlmodel import Session, select

from kisetsu.clients.anilist import AniListDeferred, AniListError
from kisetsu.clients.qbittorrent import QbitUnavailable
from kisetsu.config import DEFAULT_EARLY_AIR_TOLERANCE_HOURS
from kisetsu.db.schema import AppState, Download, DownloadState, Episode, Event, EventKind, Feed, FeedItem, FeedState, Show
from kisetsu.engine.jobs import SETUP_WAIT, Jobs
from tests.fakes import START, anilist_show

TOP = "https://top/rss"


def run(env, fn):
    async def go():
        await env.engine.start()
        try:
            return await fn()
        finally:
            await env.engine.close()

    return asyncio.run(go())


def title(n, group="Grp"):
    return f"[{group}] Sousou no Frieren - {n:02d} (1080p).mkv"


def airing(env, episodes=3):
    """AniList lists one show whose newest of ``episodes`` aired just now."""
    first = START - timedelta(days=7 * (episodes - 1))
    env.anilist.shows.append(anilist_show(1, "Sousou no Frieren", total_episodes=12, next_airing_episode=episodes + 1, next_airing_at=START + timedelta(days=7)))
    env.anilist.set_schedule(1, first, episodes)
    return first


def test_a_fresh_start_adopts_what_qbit_has_then_grabs_the_new_episode_and_finishes_it(env):
    first = airing(env)
    top = env.add_feed()
    env.feeds.post(TOP, title(1), first)
    env.feeds.post(TOP, title(2), first + timedelta(days=7))
    env.feeds.post(TOP, title(3), first + timedelta(days=14))
    env.qbit.seed(title(1), save_path="/anime/Sousou no Frieren", category="Seasonal")
    env.qbit.seed(title(2), save_path="/anime/Sousou no Frieren", category="Seasonal")
    env.qbit._view = {}

    async def go():
        assert await env.jobs.startup() is None
        assert env.jobs.ready.is_set()
        assert [env.statuses()[n] for n in (1, 2, 3)] == ["completed", "completed", "wanted"]
        show = env.show()
        assert (show.feed_id, show.feed_state) == (top, FeedState.LOCKED)
        assert await env.jobs.grab() is None
        assert env.statuses()[3] == "downloading"
        torrent = next(t for t in env.qbit.truth.values() if "- 03" in t.name)
        env.qbit.finish(torrent.hash)
        delay = await env.jobs.settle()
        assert env.statuses()[3] == "completed"
        return delay

    assert run(env, go) == 300.0
    assert {"grab", "settle", "hunt", "palette"} <= set(env.pokes)


def test_nothing_is_grabbed_before_qbit_has_been_read(env):
    airing(env)
    env.add_feed()
    env.feeds.post(TOP, title(3), START)
    env.qbit.unavailable = QbitUnavailable("down")

    async def go():
        retry = await env.jobs.startup()
        grabbed = await env.jobs.grab()
        settled = await env.jobs.settle()
        return retry, grabbed, settled

    retry, grabbed, settled = run(env, go)
    assert retry == 30.0 and grabbed is None and settled is None
    assert not env.jobs.ready.is_set() and env.query(lambda s: s.exec(select(Download)).all()) == []
    assert env.jobs.health["qbit"] == "down"


def test_nothing_contacts_qbit_until_the_wizard_is_done(env):
    env.query(lambda s: (s.delete(s.get(AppState, "setup_completed")), s.commit()))
    airing(env)

    async def go():
        return await env.jobs.startup()

    assert run(env, go) == SETUP_WAIT
    assert env.qbit.calls == [] and env.anilist.calls == [] and not env.jobs.ready.is_set()


def test_feeds_are_not_read_before_the_wizard_is_done(env):
    env.query(lambda s: (s.delete(s.get(AppState, "setup_completed")), s.commit()))
    env.add_feed()
    env.feeds.post(TOP, title(1), START)

    async def go():
        return await env.jobs.refresh_feeds(), await env.jobs.hunt()

    assert run(env, go) == (None, None)
    assert env.query(lambda s: s.exec(select(FeedItem)).all()) == []


def test_the_ui_hears_when_startup_finishes_and_when_qbit_goes_down_and_comes_back(env, caplog):
    airing(env)
    env.add_feed()

    async def drain(queue):
        await asyncio.sleep(0)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    async def go():
        queue = env.engine.bus.open_queue()
        env.qbit.unavailable = QbitUnavailable("down")
        await env.jobs.startup()
        down = await drain(queue)
        env.qbit.unavailable = None
        await env.jobs.startup()
        return down, await drain(queue)

    with caplog.at_level(logging.INFO):
        down, up = run(env, go)
    assert any("status" in m["changed"] for m in down) and any("status" in m["changed"] for m in up)
    assert "qBittorrent: down" in caplog.text
    # What the services report while taking AniList's list in reaches the log.
    assert any(r.name == "kisetsu.services.cycle" for r in caplog.records)


def test_a_feed_that_fails_is_marked_and_backs_off_while_healthy_feeds_carry_on(env):
    airing(env)
    good = env.add_feed("Good", "https://good/rss", 1)
    bad = env.add_feed("Bad", "https://bad/rss", 2)
    env.feeds.post("https://good/rss", title(3), START)
    env.feeds.fail("https://bad/rss", "bad.example did not answer in time")

    async def go():
        await env.engine.run(lambda ctx: ctx.qbit.sync(full=True))
        await env.jobs.sync_anilist()
        await env.jobs.refresh_feeds()
        first = env.feeds.fetched.count("https://bad/rss")
        await env.jobs.refresh_feeds()
        return first, env.feeds.fetched.count("https://bad/rss")

    first, second = run(env, go)
    feeds = {f.id: f for f in env.query(lambda s: s.exec(select(Feed)).all())}
    assert feeds[good].last_ok_at is not None and feeds[good].failing_since is None
    assert feeds[bad].failing_since == START and feeds[bad].last_error == "bad.example did not answer in time"
    assert (first, second) == (1, 1)
    assert "grab" in env.pokes


def test_anilist_deferring_keeps_the_shows_and_asks_to_be_called_again_later(env):
    airing(env)

    async def go():
        await env.jobs.sync_anilist()
        env.anilist.error = AniListDeferred("rate limited", retry_in=90)
        retry = await env.jobs.sync_anilist()
        return retry

    assert run(env, go) == 90
    assert env.show().anilist_id == 1 and "rate limited" in env.jobs.health["anilist"]


def test_a_failed_sync_keeps_the_last_known_airing_pointer(env):
    airing(env)

    async def go():
        await env.jobs.sync_anilist()
        before = env.show()
        env.anilist.error = AniListError("offline")
        retry = await env.jobs.sync_anilist()
        return before, retry

    before, retry = run(env, go)
    after = env.show()
    assert retry == 300.0 and env.jobs.health["anilist"] == "offline"
    assert (after.next_airing_episode, after.next_airing_at) == (before.next_airing_episode, before.next_airing_at)
    assert after.next_airing_episode == 4


def test_a_manual_sync_ignores_the_refresh_window_that_a_startup_sync_respects(env):
    airing(env)

    def fetches():
        return [name for name, _ in env.anilist.calls].count("fetch_user_seasonal_anime")

    async def go():
        await env.jobs.sync_anilist()
        await env.jobs.sync_anilist(startup=True)
        skipped = fetches()
        await env.jobs.sync_anilist()
        return skipped, fetches()

    assert run(env, go) == (1, 2)


def test_without_an_anilist_user_the_job_waits_for_the_settings(env):
    from kisetsu.db.schema import Settings

    def clear(session):
        settings = session.get(Settings, 1)
        settings.anilist_username = ""
        session.add(settings)
        session.commit()

    env.query(clear)
    assert run(env, env.jobs.sync_anilist) == 3600.0


def test_the_hunt_job_polls_while_a_show_airs_and_otherwise_sleeps_until_the_next_one(env):
    first = airing(env, 1)
    env.add_feed()

    async def go():
        await env.engine.run(lambda ctx: ctx.qbit.sync(full=True))
        await env.jobs.sync_anilist()
        env.feeds.post(TOP, title(1), first)
        hunting = await env.jobs.hunt()
        env.clock.advance(days=2)
        idle = await env.jobs.hunt()
        return hunting, idle

    hunting, idle = run(env, go)
    assert hunting == 5 * 60.0 and idle == 3600.0
    assert env.feeds.fetched.count(TOP) == 1


def test_a_read_that_misses_what_was_stored_asks_for_a_search(env):
    first = airing(env, 3)
    env.add_feed()
    env.feeds.items[TOP] = [env.feeds.post(TOP, title(3), START)]

    async def go():
        await env.engine.run(lambda ctx: ctx.qbit.sync(full=True))
        await env.jobs.sync_anilist()
        await env.jobs.refresh_feeds()
        env.feeds.items[TOP] = [env.feeds.post(TOP, title(9), START), env.feeds.post(TOP, title(10), START)]
        env.clock.advance(minutes=15)
        await env.jobs.refresh_feeds()

    run(env, go)
    assert "backfill" in env.pokes and env.jobs.backfill_all
    assert first


def test_backfill_finds_a_lost_episode_and_the_next_grab_takes_it(env):
    first = airing(env, 3)
    env.add_feed("Nyaa", "https://nyaa.si/?page=rss&c=1_2&f=0", 1)
    env.feeds.searchable["https://nyaa.si/?page=rss&c=1_2&f=0"] = [
        env.feeds.post("https://nyaa.si/?page=rss&c=1_2&f=0", title(2), first + timedelta(days=7)),
    ]
    env.feeds.items.clear()

    async def go():
        await env.engine.run(lambda ctx: ctx.qbit.sync(full=True))
        await env.jobs.sync_anilist()
        env.jobs.ready.set()
        await env.jobs.backfill()
        await env.jobs.grab()

    run(env, go)
    assert env.statuses()[2] == "downloading"


def test_housekeeping_and_palette_run_without_a_qbit_or_images(env):
    airing(env)

    async def go():
        await env.jobs.sync_anilist()
        await env.jobs.housekeeping()
        return await env.jobs.palette()

    assert run(env, go) is None


def test_the_colours_of_every_show_are_stored_together_as_one_change(env):
    airing(env)
    env.anilist.shows.append(anilist_show(2, "Kusuriya no Hitorigoto", total_episodes=12))
    env.jobs.fetch_hues = lambda *urls: asyncio.sleep(0, (True, (10, 0.5, 20, 30, 0.4)))

    def covers(session):
        for show in session.exec(select(Show)).all():
            show.cover_image = f"https://s4.anilist.co/{show.id}.jpg"
            session.add(show)
        session.commit()

    async def go():
        await env.jobs.sync_anilist()
        env.query(covers)
        queue = env.engine.bus.open_queue()
        await env.jobs.palette()
        await asyncio.sleep(0)
        return [queue.get_nowait()["changed"] for _ in range(queue.qsize())]

    changes = run(env, go)
    assert len(changes) == 1 and "shows" in changes[0]
    with Session(env.db) as session:
        assert {s.accent_hues for s in session.exec(select(Show)).all()} == {"10,0.500,20,30,0.400"}


def test_history_records_what_a_failed_replacement_did_exactly_once(env):
    first = airing(env, 1)
    env.add_feed()
    env.feeds.post(TOP, title(1), first)

    async def go():
        assert await env.jobs.startup() is None
        await env.jobs.grab()
        torrent = next(iter(env.qbit.truth.values()))
        env.qbit.finish(torrent.hash)
        await env.jobs.settle()
        env.feeds.post(TOP, "[Grp] Sousou no Frieren - 01v2 (1080p).mkv", first)
        # Nothing is missing, so the feed is read again only once it has been idle long enough.
        env.clock.advance(hours=6)
        await env.jobs.refresh_feeds()
        await env.jobs.grab()
        v2 = next(t for t in env.qbit.truth.values() if t.hash != torrent.hash)
        env.qbit.remove(v2.hash)
        delays = [await env.jobs.settle() for _ in range(8)]
        return delays

    delays = run(env, go)
    events = env.query(lambda s: s.exec(select(Event)).all())
    assert [e.kind for e in events].count(EventKind.FAILED) == 1
    assert delays[-1] == 300.0
    assert env.query(lambda s: [d.state for d in s.exec(select(Download)).all()]).count(DownloadState.FAILED) == 1
    assert env.query(lambda s: s.exec(select(Show)).one()).anilist_id == 1


def test_a_show_added_by_hand_gets_its_colours(env):
    async def go():
        await env.jobs.after_manual_add()

    run(env, go)
    assert "palette" in env.pokes


def test_a_routine_read_skips_an_idle_feed_but_startup_and_a_manual_refresh_read_it(env):
    env.add_feed()

    async def go():
        assert await env.jobs.startup() is None
        once = env.feeds.fetched.count(TOP)
        await env.jobs.refresh_feeds()
        idle = env.feeds.fetched.count(TOP)
        env.jobs.request_refresh()
        await env.jobs.refresh_feeds()
        return once, idle, env.feeds.fetched.count(TOP), env.jobs.refresh_all

    assert run(env, go) == (1, 1, 2, False)
    assert "refresh_feeds" in env.pokes


def test_with_nothing_missing_the_routine_read_sleeps_until_a_feed_is_idle_and_the_hunt_reports_no_read(env):
    env.anilist.shows.append(anilist_show(1, "Sousou no Frieren"))
    env.add_feed()

    async def go():
        await env.jobs.startup()
        return await env.jobs.refresh_feeds(), await env.jobs.hunt(), env.jobs.hunt_read_at

    refresh, hunt, hunt_read_at = run(env, go)
    assert refresh == 6 * 3600.0 and hunt is not None and hunt_read_at is None


def test_the_hunt_reports_when_it_will_next_read_even_past_its_hourly_recheck(env):
    env.anilist.shows.append(anilist_show(1, "Sousou no Frieren", total_episodes=12, next_airing_episode=1, next_airing_at=START + timedelta(days=3)))
    env.add_feed()

    async def go():
        await env.jobs.startup()
        return await env.jobs.hunt(), env.jobs.hunt_read_at

    hunt, hunt_read_at = run(env, go)
    # A premiere is looked for early by the early-air setting.
    assert hunt == 3600.0 and hunt_read_at == START + timedelta(days=3) - timedelta(hours=DEFAULT_EARLY_AIR_TOLERANCE_HOURS)


def test_each_anilist_sync_says_so_in_the_log_and_when_the_next_one_is(env, caplog):
    airing(env)

    async def go():
        await env.jobs.sync_anilist()
        await env.jobs.sync_anilist(startup=True)

    with caplog.at_level(logging.INFO, logger="kisetsu.engine.jobs"):
        run(env, go)
    lines = [r.getMessage() for r in caplog.records if "AniList" in r.getMessage()]
    assert any(line.startswith("AniList synced: 1 shows. Next sync in ") for line in lines)
    assert any(line.startswith("AniList was synced 0 min ago. Next sync in ") for line in lines)


def test_startup_books_the_next_anilist_sync_as_nothing_else_wakes_it(env):
    airing(env)
    env.add_feed()

    async def go():
        await env.jobs.startup()

    run(env, go)
    assert "delay:sync_anilist" in env.pokes


def test_housekeeping_first_runs_within_a_short_uptime_and_not_before_setup(env):
    airing(env)
    env.jobs.register()
    job = env.scheduler.jobs["housekeeping"]
    assert job.due - env.scheduler.clock() <= 3600.0

    def unfinished(session):
        session.exec(select(AppState).where(AppState.key == "setup_completed")).one().value = "0"
        session.commit()

    with Session(env.db) as session:
        unfinished(session)

    async def go():
        return await env.jobs.housekeeping()

    calls = []
    env.qbit.tags = lambda: calls.append("tags") or []
    run(env, go)
    assert calls == []


def _running(env):
    """A started app with episode 3 downloading, then the flags of a restart: nothing ready yet."""
    first = airing(env)
    env.add_feed()
    for n in (1, 2, 3):
        env.feeds.post(TOP, title(n), first + timedelta(days=7 * (n - 1)))
    return first


def test_downloads_are_followed_and_grabbed_before_the_backfill_searches(env):
    _running(env)

    async def go():
        await env.jobs.startup()
        await env.jobs.grab()
        torrent = next(t for t in env.qbit.truth.values() if "- 03" in t.name)
        env.jobs.ready.clear()
        env.jobs.synced.clear()
        assert await env.jobs.settle() is None
        seen = {}
        original = env.jobs.backfill

        async def slow_backfill():
            env.qbit.finish(torrent.hash)
            seen["delay"] = await env.jobs.settle()
            seen["status"] = env.statuses()[3]
            seen["ready"] = env.jobs.ready.is_set()
            return await original()

        env.jobs.backfill = slow_backfill
        await env.jobs.startup()
        return seen

    seen = run(env, go)
    # Adoption has looked at what qBittorrent holds, so grabbing need not wait for the slow searches.
    assert seen["status"] == "completed" and seen["ready"] is True and seen["delay"] is not None
    assert env.jobs.ready.is_set() and env.jobs.synced.is_set()


def test_a_retry_waits_for_adoption_but_not_for_the_backfill_searches(env):
    from kisetsu.clients.qbittorrent import QbitRejected

    _running(env)

    async def go():
        await env.jobs.startup()
        env.qbit.reject_adds = QbitRejected("bad link")
        await env.jobs.grab()
        assert env.statuses()[3] == "wanted"
        env.qbit.reject_adds = None
        env.clock.advance(minutes=20)
        env.jobs.ready.clear()
        env.jobs.synced.clear()
        before = len(env.qbit.calls_named("add_torrent"))
        assert await env.jobs.settle() is None
        assert len(env.qbit.calls_named("add_torrent")) == before
        during = {}
        original = env.jobs.backfill

        async def slow_backfill():
            await env.jobs.settle()
            during["adds"] = len(env.qbit.calls_named("add_torrent")) - before
            return await original()

        env.jobs.backfill = slow_backfill
        await env.jobs.startup()
        return during

    during = run(env, go)
    assert during["adds"] == 3 and env.statuses()[3] == "downloading"


def test_a_grab_that_never_reached_qbit_keeps_the_unreachable_warning(env):
    airing(env)
    env.add_feed()

    async def go():
        await env.jobs.startup()
        env.qbit.unavailable = QbitUnavailable("down")
        await env.jobs.settle()
        await env.jobs.grab()
        return env.jobs.health["qbit"]

    assert run(env, go) == "down"


def test_one_show_that_breaks_does_not_stop_the_grab_for_the_others(env, monkeypatch):
    from kisetsu.services import grab as grab_service

    first = airing(env)
    env.anilist.shows.append(anilist_show(2, "Dandadan", total_episodes=12, next_airing_episode=4, next_airing_at=START + timedelta(days=7)))
    env.anilist.set_schedule(2, first, 3)
    env.add_feed()
    env.feeds.post(TOP, "[Grp] Dandadan - 03 (1080p).mkv", START)
    env.feeds.post(TOP, title(3), START)
    real = grab_service._grab_show

    def flaky(ctx, show, *args):
        if show.anilist_id == 1:
            raise RuntimeError("boom")
        return real(ctx, show, *args)

    monkeypatch.setattr(grab_service, "_grab_show", flaky)

    async def go():
        await env.jobs.startup()
        await env.jobs.grab()

    run(env, go)
    assert env.statuses(2)[3] == "downloading"
    assert env.jobs.health["grab"] == "Sousou no Frieren: boom"


def test_startup_keeps_anilist_and_the_feeds_current_while_qbit_is_away(env):
    airing(env)
    env.add_feed()
    env.qbit.unavailable = QbitUnavailable("down")

    async def go():
        await env.jobs.startup()
        await env.jobs.startup()

    run(env, go)
    assert env.pokes.count("sync_anilist") == 1 and env.pokes.count("refresh_feeds") == 1


def test_two_reads_of_the_feeds_never_run_at_once(env):
    airing(env)
    env.add_feed()
    env.feeds.post(TOP, title(3), START)
    state = {"now": 0, "most": 0}
    real = env.feeds.fetch

    async def slow(url):
        state["now"] += 1
        state["most"] = max(state["most"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return await real(url)

    env.jobs._fetcher = slow

    async def yes():
        return True

    async def later():
        return 60.0

    # The in-memory test database has one shared connection, so the two reads around the fetch are kept out of the race.
    env.jobs._configured, env.jobs._next_read = yes, later

    async def go():
        await asyncio.gather(env.jobs.refresh_feeds(everything=True), env.jobs.refresh_feeds(everything=True))

    run(env, go)
    assert state["most"] == 1


def test_what_the_searches_learned_of_a_site_outlives_a_restart(env):
    env.jobs.grouping.saw("nyaa.si", 75)
    env.jobs.grouping.record("nyaa.si", True)
    restarted = Jobs(env.engine, env.scheduler, anilist=env.anilist, fetcher=env.feeds.fetch, searcher=env.jobs.searcher)

    async def go():
        await env.jobs.backfill()
        await restarted.backfill()

    run(env, go)
    assert restarted.grouping.works("nyaa.si") is True and restarted.grouping.complete("nyaa.si", 74)


def test_subsplease_schedule_is_read_once_and_only_with_a_subsplease_feed(env):
    reads = []

    async def read_schedule(client):
        reads.append(client)
        return ["Some Other Show"]

    env.jobs.read_schedule = read_schedule

    async def go():
        await env.jobs.backfill()
        env.add_feed("SubsPlease", "https://subsplease.org/rss/?t&r=1080", 1)
        await env.jobs.backfill()
        await env.jobs.backfill()

    run(env, go)
    assert len(reads) == 1


def test_a_burst_of_pokes_runs_the_job_at_most_once_a_second(env):
    now = [100.0]
    asked = []
    env.jobs.scheduler.clock = lambda: now[0]
    env.jobs.scheduler.poke = lambda name, delay=0.0: asked.append((name, round(now[0] + delay, 3)))
    for t in (100.0, 100.2, 100.5, 100.9, 101.3, 105.0):
        now[0] = t
        env.jobs._poke_soon("grab")
    # The first runs at once; the rest of the burst share one run a second, and a lone poke later runs at once.
    assert asked == [("grab", 100.0), ("grab", 101.0), ("grab", 101.0), ("grab", 101.0), ("grab", 102.0), ("grab", 105.0)]
    asked.clear()
    for t in (110.0, 111.0, 112.0):
        now[0] = t
        env.jobs._poke_soon("settle", 4.0)
    assert asked == [("settle", 110.0), ("settle", 114.0), ("settle", 114.0)]


def test_shows_owed_a_full_schedule_when_anilist_defers_it_are_fetched_on_the_next_pass(env):
    env.anilist.shows.append(anilist_show(1, "Long Show", next_airing_episode=30, next_airing_at=START + timedelta(days=7)))
    env.anilist.set_schedule(1, START - timedelta(days=196), 29)
    real_full = env.anilist.fetch_media_airing_schedules
    calls = []

    async def full(media_ids):
        calls.append(list(media_ids))
        if len(calls) == 1:
            raise AniListDeferred("budget used", retry_in=30)
        return await real_full(media_ids)

    env.anilist.fetch_media_airing_schedules = full

    async def go():
        wait = await env.jobs.sync_anilist()
        env.clock.advance(seconds=60)
        await env.jobs.refresh_schedules()
        return wait

    assert run(env, go) == 30.0
    assert len(calls) == 2
    undated = env.query(lambda s: [e.number for e in s.exec(select(Episode).where(Episode.number <= 3, Episode.air_at.is_(None))).all()])
    assert undated == []


def test_a_show_deleted_before_its_search_ran_is_skipped(env):
    from kisetsu.services import backfill

    gone = backfill.Due(999, "Gone", ["Gone"], [], [1], 0)

    async def go():
        return await env.jobs._search([gone])

    assert run(env, go) == 0


def test_a_feed_read_teaches_how_many_results_its_sites_pages_hold(env):
    env.add_feed("Nyaa", "https://nyaa.si/?page=rss&q=1080p")
    for number in range(1, 4):
        env.feeds.post("https://nyaa.si/?page=rss&q=1080p", f"[Grp] Show - {number:02d} (1080p).mkv")
    run(env, lambda: env.jobs.refresh_feeds(everything=True))
    assert env.jobs.grouping.complete("nyaa.si", 2) and not env.jobs.grouping.complete("nyaa.si", 3)


def test_a_first_search_checks_the_site_with_one_show_and_the_others_share_one_search(env):
    nyaa = "https://nyaa.si/?page=rss&q=1080p"
    env.add_feed("Nyaa", nyaa)
    names = [("Sousou no Frieren", "Frieren"), ("Kusuriya no Hitorigoto", "Apothecary Diaries"), ("Hotaru no Yomeiri", "Firefly Wedding")]
    first = START - timedelta(days=14)
    for index, (romaji, english) in enumerate(names, start=1):
        env.anilist.shows.append(anilist_show(index, english, romaji=romaji, total_episodes=12, next_airing_episode=4, next_airing_at=START + timedelta(days=7)))
        env.anilist.set_schedule(index, first, 3)
    # The feed lists only other shows by now; its site still finds the three.
    for number in range(20):
        env.feeds.post(nyaa, f"[Grp] Another Show {number} - 01 (1080p).mkv", START)
    env.feeds.searchable[nyaa] = [
        env.feeds.post("unlisted", f"[Grp] {romaji} - {n:02d} (1080p).mkv", first + timedelta(days=7 * (n - 1)))
        for romaji, _ in names for n in (1, 2, 3)
    ]

    async def go():
        await env.jobs.sync_anilist()
        await env.jobs.refresh_feeds(everything=True)
        await env.jobs.backfill()

    run(env, go)
    # One show asks for each of its two names and then for both at once, which checks the site; the other two share one search.
    assert len(env.feeds.searched) == 4
    assert env.query(lambda s: len(s.exec(select(FeedItem).where(FeedItem.title.not_like("%Another Show%"))).all())) == 9
