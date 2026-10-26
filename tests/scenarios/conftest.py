from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional

import pytest
from sqlmodel import Session, select

from kisetsu.db.migrations import migrate
from kisetsu.db.schema import Episode, EpisodeStatus, Feed, FeedState, ScheduleState, Show
from kisetsu.db.store import make_engine
from kisetsu.services.context import Caches, Context, make_context
from tests.fakes import FakeAniList, FakeClock, FakeFeeds, FakeQbit


class World:
    """A database, a clock and the fakes, with helpers to set up and look at what a scenario needs."""

    def __init__(self):
        self.engine = make_engine()
        migrate(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.clock = FakeClock()
        self.qbit = FakeQbit(self.clock)
        self.anilist = FakeAniList()
        self.feeds = FakeFeeds()
        self.caches = Caches()
        self.notifications = []

    def ctx(self) -> Context:
        """A context at the clock's current time; it shares caches across calls like the engine does."""
        ctx = make_context(self.session, self.qbit, self.clock(), self.caches, notify=lambda *a: self.notifications.append(a))
        return ctx

    def commit(self) -> None:
        self.session.commit()

    def feed(self, name: str, url: str, priority: int) -> Feed:
        feed = Feed(name=name, url=url, priority=priority)
        self.session.add(feed)
        self.session.commit()
        return feed

    def show(
        self,
        anilist_id: int = 1,
        title: str = "Sousou no Frieren",
        *,
        aliases: Iterable[str] = (),
        episodes: int = 0,
        first_air: Optional[datetime] = None,
        every: timedelta = timedelta(days=7),
        feed: Optional[Feed] = None,
        feed_state: FeedState = FeedState.NONE,
        **fields: Any,
    ) -> Show:
        """A followed show, with ``episodes`` WANTED rows aired weekly from ``first_air``."""
        show = Show(
            anilist_id=anilist_id,
            title_english=title,
            title_romaji=fields.pop("title_romaji", title),
            anilist_status=fields.pop("anilist_status", "RELEASING"),
            feed_id=feed.id if feed else None,
            feed_state=feed_state,
            **fields,
        )
        show.aliases = list(dict.fromkeys([title, *aliases]))
        self.session.add(show)
        self.session.flush()
        for number in range(1, episodes + 1):
            air = first_air + every * (number - 1) if first_air else None
            self.session.add(Episode(
                show_id=show.id, number=number, air_at=air,
                schedule_state=ScheduleState.SCHEDULED if air else ScheduleState.UNKNOWN,
            ))
        self.session.commit()
        return show

    def episode(self, show: Show, number: int) -> Episode:
        self.session.expire_all()
        return self.session.exec(select(Episode).where(Episode.show_id == show.id, Episode.number == number)).one()

    def statuses(self, show: Show) -> Dict[int, EpisodeStatus]:
        self.session.expire_all()
        rows = self.session.exec(select(Episode).where(Episode.show_id == show.id).order_by(Episode.number)).all()
        return {row.number: row.status for row in rows}


@pytest.fixture
def world():
    w = World()
    yield w
    w.session.close()
    w.engine.dispose()
