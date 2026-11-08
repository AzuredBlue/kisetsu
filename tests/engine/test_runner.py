import asyncio
import threading

import pytest
from sqlmodel import select

from kisetsu.db.migrations import migrate
from kisetsu.db.schema import Feed
from kisetsu.db.store import make_engine
from kisetsu.engine.instance import AlreadyRunning, InstanceLock
from kisetsu.engine.runner import Engine
from kisetsu.services import feeds
from tests.fakes import FakeQbit


def make():
    db = make_engine()
    migrate(db)
    created = []

    def factory(host, username, password, read_only=False):
        qbit = FakeQbit(read_only=read_only)
        created.append((host, qbit))
        return qbit

    return Engine(db, qbit_factory=factory), created


def run(coro):
    return asyncio.run(coro)


def test_every_call_runs_on_the_one_engine_thread_and_commits():
    async def go():
        engine, _ = make()
        await engine.start()
        threads = set()

        def add(ctx, name):
            threads.add(threading.current_thread().name)
            feeds.add_feed(ctx, name, f"https://{name}/rss")
            return name

        await asyncio.gather(*(engine.run(add, f"f{i}") for i in range(5)))
        names = await engine.run(lambda ctx: [f.name for f in ctx.session.exec(select(Feed)).all()])
        await engine.close()
        return threads, sorted(names)

    threads, names = run(go())
    assert threads == {"kisetsu-engine_0"} and names == [f"f{i}" for i in range(5)]


def test_an_error_rolls_back_drops_the_caches_and_leaves_the_engine_usable():
    async def go():
        engine, _ = make()
        await engine.start()

        def broken(ctx):
            feeds.add_feed(ctx, "Lost", "https://lost/rss")
            engine.caches.known_items = {(1, "x")}
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await engine.run(broken)
        after = await engine.run(lambda ctx: ctx.session.exec(select(Feed)).all())
        known = engine.caches.known_items
        await engine.close()
        return after, known

    after, known = run(go())
    assert after == [] and known is None


def test_topics_are_published_once_the_work_is_committed():
    async def go():
        engine, _ = make()
        await engine.start()
        seen = []

        async def listen():
            async for message in engine.bus.subscribe():
                seen.append(message)
                return

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        await engine.run(lambda ctx: feeds.add_feed(ctx, "A", "https://a/rss"))
        await asyncio.wait_for(task, 1)
        await engine.close()
        return seen

    assert run(go()) == [{"changed": ["feeds"], "rev": 1}]


def test_a_slow_subscriber_is_told_to_refetch_everything():
    async def go():
        engine, _ = make()
        await engine.start()
        stream = engine.bus.subscribe()
        waiting = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        for index in range(100):
            engine.bus._deliver([f"show:{index}"])
        first = await waiting
        await engine.close()
        return first

    assert run(go())["rev"] >= 1


def test_the_qbit_connection_follows_the_settings_and_is_reused():
    async def go():
        engine, created = make()
        await engine.start()
        await engine.run(lambda ctx: None)
        await engine.run(lambda ctx: None)
        await engine.run(lambda ctx: setattr(ctx.settings, "qbit_host", "http://other:8080") or ctx.session.add(ctx.settings))
        await engine.run(lambda ctx: None)
        await engine.close()
        return created

    created = run(go())
    assert [host for host, _ in created][-1] == "http://other:8080" and len(created) == 2


def test_objects_can_cross_the_await_boundary_detached():
    async def go():
        engine, _ = make()
        await engine.start()
        feed = await engine.run(lambda ctx: feeds.add_feed(ctx, "A", "https://a/rss"))
        await engine.close()
        return feed

    feed = run(go())
    assert feed.name == "A" and feed.url == "https://a/rss"


def test_a_second_process_cannot_take_the_same_database(tmp_path):
    first = InstanceLock(tmp_path / "kisetsu.lock")
    first.acquire()
    with pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "kisetsu.lock").acquire()
    first.release()
    with InstanceLock(tmp_path / "kisetsu.lock"):
        pass


def test_a_read_sees_committed_rows_saves_nothing_and_cannot_reach_qbit():
    async def go():
        engine, created = make()
        await engine.start()
        await engine.run(lambda ctx: feeds.add_feed(ctx, "A", "https://a/rss"))

        def look(ctx):
            names = [f.name for f in ctx.session.exec(select(Feed)).all()]
            ctx.session.add(Feed(name="B", url="https://b/rss"))
            ctx.session.flush()
            return names

        names = await engine.read(look)
        with pytest.raises(RuntimeError):
            await engine.read(lambda ctx: ctx.qbit.sync())
        after = await engine.run(lambda ctx: [f.name for f in ctx.session.exec(select(Feed)).all()])
        await engine.close()
        return names, after

    assert run(go()) == (["A"], ["A"])


def test_shutting_the_bus_down_ends_every_subscription():
    async def go():
        engine, _ = make()
        await engine.start()
        seen = []

        async def listen():
            async for message in engine.bus.subscribe():
                seen.append(message)

        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        engine.bus.shutdown()
        await asyncio.wait_for(task, 1)
        await engine.close()
        return seen, engine.bus.subscribers

    assert run(go()) == ([], 0)


def test_a_read_reuses_the_titles_the_engine_already_parsed():
    async def go():
        engine, _ = make()
        await engine.start()
        engine.caches.parsed["[Grp] Show - 01 (1080p).mkv"] = {"title": "Show", "episode": 1}
        seen = await engine.read(lambda ctx: dict(ctx.caches.parsed_cache()))
        assert "[Grp] Show - 01 (1080p).mkv" in seen

    run(go())


def test_a_cached_parse_that_vanishes_mid_lookup_is_parsed_again():
    from kisetsu.domain.parsing import match_release_to_show

    class Vanishing(dict):
        def __contains__(self, key):
            return True  # another thread clears the cache right after this check

    ok, _, parsed = match_release_to_show("[Grp] Sousou no Frieren - 01 (1080p).mkv", ["Sousou no Frieren"], parsed_cache=Vanishing())
    assert ok and parsed["episode"] == 1


def test_a_topic_published_after_a_delay_reaches_subscribers_once_however_often_it_is_published():
    from kisetsu.engine.runner import ChangeBus

    async def go():
        bus = ChangeBus()
        bus.bind(asyncio.get_running_loop())
        queue = bus.open_queue()
        for _ in range(50):
            bus.publish_after("requests", 0.05)
        await asyncio.sleep(0.01)
        early = queue.qsize()
        await asyncio.sleep(0.1)
        first = [queue.get_nowait()["changed"] for _ in range(queue.qsize())]
        bus.publish_after("requests", 0.01)
        await asyncio.sleep(0.05)
        second = [queue.get_nowait()["changed"] for _ in range(queue.qsize())]
        return early, first, second

    assert run(go()) == (0, [["requests"]], [["requests"]])


def test_changes_published_close_together_reach_subscribers_as_one_message():
    from kisetsu.engine.runner import ChangeBus

    async def go():
        bus = ChangeBus(batch_seconds=0.05)
        bus.bind(asyncio.get_running_loop())
        queue = bus.open_queue()
        bus.publish(["shows", "show:1"])
        bus.publish(["shows", "show:2"])
        bus.publish(["logs"])
        await asyncio.sleep(0.01)
        early = queue.qsize()
        await asyncio.sleep(0.1)
        return early, [queue.get_nowait()["changed"] for _ in range(queue.qsize())]

    assert run(go()) == (0, [["logs", "show:1", "show:2", "shows"]])


def test_the_shutdown_signal_survives_a_subscriber_that_fell_behind():
    from kisetsu.engine.runner import SUBSCRIBER_BACKLOG, ChangeBus

    async def go():
        bus = ChangeBus(batch_seconds=0)
        bus.bind(asyncio.get_running_loop())
        queue = bus.open_queue()
        for _ in range(SUBSCRIBER_BACKLOG - 1):
            bus._deliver(["shows"])
        bus.shutdown()
        bus._deliver(["status"])
        drained = []
        while not queue.empty():
            drained.append(queue.get_nowait())
        return drained

    assert None in asyncio.run(go())
