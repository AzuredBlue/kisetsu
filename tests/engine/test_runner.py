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
