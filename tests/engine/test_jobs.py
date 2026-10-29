import asyncio
import logging
from datetime import timedelta

from sqlmodel import select

from kisetsu.clients.anilist import AniListDeferred, AniListError
from kisetsu.clients.qbittorrent import QbitUnavailable
from kisetsu.config import DEFAULT_EARLY_AIR_TOLERANCE_HOURS
from kisetsu.db.schema import AppState, Download, DownloadState, Event, EventKind, Feed, FeedItem, FeedState, Show
from kisetsu.engine.jobs import SETUP_WAIT
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
