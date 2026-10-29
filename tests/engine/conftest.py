import pytest
from sqlmodel import Session, select

from kisetsu.db.migrations import migrate
from kisetsu.db.schema import AppState, Episode, Feed, Settings, Show
from kisetsu.db.store import make_engine
from kisetsu.engine.jobs import Jobs
from kisetsu.engine.runner import Engine
from kisetsu.engine.scheduler import Scheduler
from tests.fakes import FakeAniList, FakeClock, FakeFeeds, FakeQbit


class Env:
    """A real Engine on an in-memory database with the fakes behind every network edge."""

    def __init__(self):
        self.db = make_engine()
        migrate(self.db)
        self.clock = FakeClock()
        self.qbit = FakeQbit(self.clock)
        self.anilist = FakeAniList()
        self.feeds = FakeFeeds()
        self.engine = Engine(self.db, clock=self.clock, qbit_factory=lambda *a, **k: self.qbit)
        self.scheduler = Scheduler()

        async def search(client, url, term, now=None):
            return await self.feeds.search(url, term)

        async def hues(*urls):
            return True, None

        self.jobs = Jobs(
            self.engine, self.scheduler, anilist=self.anilist, fetcher=self.feeds.fetch,
            searcher=search, fetch_hues=hues,
        )
        self.pokes = []
        self.scheduler.poke = lambda name, delay=0.0: self.pokes.append(name)
        self.scheduler.delay = lambda name, seconds: self.pokes.append(f"delay:{name}")
        with Session(self.db) as session:
            settings = session.get(Settings, 1)
            settings.anilist_username = "user"
            settings.default_category = "Seasonal"
            session.add(settings)
            session.add(AppState(key="setup_completed", value="1"))
            session.commit()

    def add_feed(self, name="Top", url="https://top/rss", priority=1):
        with Session(self.db) as session:
            feed = Feed(name=name, url=url, priority=priority)
            session.add(feed)
            session.commit()
            session.refresh(feed)
            return feed.id

    def query(self, fn):
        with Session(self.db, expire_on_commit=False) as session:
            return fn(session)

    def show(self, anilist_id=1):
        return self.query(lambda s: s.exec(select(Show).where(Show.anilist_id == anilist_id)).one())

    def statuses(self, anilist_id=1):
        def read(session):
            show = session.exec(select(Show).where(Show.anilist_id == anilist_id)).one()
            rows = session.exec(select(Episode).where(Episode.show_id == show.id).order_by(Episode.number)).all()
            return {row.number: row.status.value for row in rows}

        return self.query(read)


@pytest.fixture
def env():
    yield Env()
