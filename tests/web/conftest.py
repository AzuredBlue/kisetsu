import httpx2
import pytest

from kisetsu.db.migrations import migrate
from kisetsu.db.schema import AppState, Episode, Feed, ScheduleState, Show
from kisetsu.db.store import make_engine
from kisetsu.engine.jobs import Jobs
from kisetsu.engine.runner import Engine
from kisetsu.engine.scheduler import Scheduler
from kisetsu.web.app import create_app
from kisetsu.web.deps import Runtime
from kisetsu.web.logs import LogRing
from sqlmodel import Session, select
from tests.fakes import START, FakeAniList, FakeClock, FakeFeeds, FakeQbit

from datetime import timedelta


class Web:
    """The whole web app on a file database, with the fakes at every network edge."""

    def __init__(self, tmp_path, *, sandbox: bool = False, setup: bool = True):
        (tmp_path / "static").mkdir(exist_ok=True)
        self.static = tmp_path / "static"
        self.db = make_engine(tmp_path / "k.db")
        migrate(self.db)
        self.clock = FakeClock()
        self.qbit = FakeQbit(self.clock)
        self.anilist = FakeAniList()
        self.feeds = FakeFeeds()
        self.pokes = []
        self.probed = []
        engine = Engine(self.db, clock=self.clock, qbit_factory=lambda *a, **k: self.qbit)
        scheduler = Scheduler()
        scheduler.poke = lambda name, delay=0.0: self.pokes.append(name)

        async def pause(seconds):
            return None

        async def search(client, url, term, now=None):
            return await self.feeds.search(url, term)

        async def hues(*urls):
            return True, None

        jobs = Jobs(engine, scheduler, anilist=self.anilist, fetcher=self.feeds.fetch, searcher=search, fetch_hues=hues, pause=pause)
        self.probe_result = {"app_version": "v5.3.0", "api_version": "2.16.2"}

        def probe(host, username, password):
            self.probed.append((host, username, password))
            return self.probe_result

        self.runtime = Runtime(engine, scheduler, jobs, self.anilist, LogRing(), sandbox=sandbox, probe=probe)
        self.app = create_app(
            runtime_factory=lambda: self.runtime, sandbox=sandbox, run_jobs=False,
            static_dir=self.static, lock_path=tmp_path / "k.lock",
        )
        if setup:
            self.query(lambda s: (s.add(AppState(key="setup_completed", value="1")), s.commit()))

    def query(self, fn):
        with Session(self.db, expire_on_commit=False) as session:
            return fn(session)

    def feed(self, name="Top", url="https://top/rss", priority=1) -> int:
        def add(session):
            feed = Feed(name=name, url=url, priority=priority)
            session.add(feed)
            session.commit()
            return feed.id

        return self.query(add)

    def show(self, anilist_id=1, title="Sousou no Frieren", *, episodes=3, first_air=START - timedelta(days=14), **fields) -> int:
        def add(session):
            show = Show(anilist_id=anilist_id, title_romaji=title, anilist_status="RELEASING", **fields)
            show.aliases = [title]
            session.add(show)
            session.flush()
            for number in range(1, episodes + 1):
                air = first_air + timedelta(days=7) * (number - 1)
                session.add(Episode(show_id=show.id, number=number, air_at=air, schedule_state=ScheduleState.SCHEDULED))
            session.commit()
            return show.id

        return self.query(add)

    def load(self, model, ident):
        return self.query(lambda s: s.get(model, ident))

    def episode(self, show_id, number):
        return self.query(lambda s: s.exec(select(Episode).where(Episode.show_id == show_id, Episode.number == number)).one())


@pytest.fixture
async def web(tmp_path):
    w = Web(tmp_path)
    async with w.app.router.lifespan_context(w.app):
        w.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=w.app), base_url="http://localhost")
        yield w
        await w.client.aclose()


@pytest.fixture
async def sandbox(tmp_path):
    w = Web(tmp_path, sandbox=True, setup=False)
    async with w.app.router.lifespan_context(w.app):
        w.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=w.app), base_url="http://localhost")
        yield w
        await w.client.aclose()


@pytest.fixture
async def fresh(tmp_path):
    w = Web(tmp_path, setup=False)
    async with w.app.router.lifespan_context(w.app):
        w.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=w.app), base_url="http://localhost")
        yield w
        await w.client.aclose()
