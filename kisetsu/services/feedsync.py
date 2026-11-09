"""Reading the feeds: what a read does to the stored items and the feeds' health."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Union

from sqlalchemy import func
from sqlmodel import select

from kisetsu.clients.rss import FeedArticle
from kisetsu.db.schema import FIXED_FEED_STATES, Episode, EpisodeStatus, Feed, FeedState, Show
from kisetsu.domain.schedule import HUNT_WINDOW, HuntPlan, hunt_plan
from kisetsu.services import ingest
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for

logger = logging.getLogger("kisetsu.services.feedsync")


class FetchFailure(str):
    """The error of a failed read; ``transient`` when it may pass on its own (see ``FeedFetchError``)."""

    def __new__(cls, message: str, transient: bool = False):
        failure = super().__new__(cls, message)
        failure.transient = transient
        return failure


FetchResult = Union[List[FeedArticle], str]

# A feed no show needs is still read this often, for v2s, batches and releases ahead of their air time
# (only the feeds of locked and pinned shows while every followed show has one: see ``_idle_read``).
IDLE_FEED_REFRESH = timedelta(hours=6)
# An aired episode still missing keeps its show's feeds read this long; then only the idle read looks.
MISSING_FOR = timedelta(days=3)
# Past the day it is hunted for, a still missing episode's feeds are read this often instead of on every routine read.
LATE_READ_EVERY = timedelta(hours=1)
# A needed feed is read again this much before its interval has passed since its last read, since a read is
# stamped a little after the routine read was planned; a feed the hunt read more recently than that is left to it.
READ_SLACK = timedelta(minutes=2)


@dataclass(frozen=True)
class FeedRef:
    id: int
    name: str
    url: str


@dataclass
class Outcome:
    new: int = 0
    matched_show_ids: Set[int] = field(default_factory=set)
    # Shows a feed their searches left out turned out to carry (``ingest.IngestResult.reconsider``).
    reconsider: Set[int] = field(default_factory=set)
    # Feeds whose read had nothing in common with what was stored: items may have scrolled past unseen.
    gaps: List[int] = field(default_factory=list)
    failed: Dict[int, str] = field(default_factory=dict)


@dataclass
class HuntInfo:
    show_ids: List[int]
    feeds: List[FeedRef]
    plan: HuntPlan
    interval_minutes: int = 5
    # Hunted shows whose feeds were all read less than an interval ago: the seconds until the first is due again.
    wait_seconds: Optional[float] = None


def _ref(feed: Feed) -> FeedRef:
    return FeedRef(feed.id, feed.name, feed.url)


def _needed(ctx: Context, all_feeds: List[Feed]) -> Dict[int, timedelta]:
    """The feeds a show with a recently aired, still missing episode reads (its own, or every feed while it has none),
    and how often: every routine read while that episode is hunted for, then every ``LATE_READ_EVERY``.
    """
    rows = ctx.session.exec(
        select(Show.feed_id, Show.feed_state, Episode.air_at)
        .join(Episode, Episode.show_id == Show.id)
        .where(
            Show.completed == False,  # noqa: E712
            Show.paused == False,  # noqa: E712
            Episode.status.in_((EpisodeStatus.WANTED, EpisodeStatus.MISSED)),
            Episode.air_at.is_not(None),
            Episode.air_at <= ctx.now,
            Episode.air_at >= ctx.now - MISSING_FOR,
        )
    ).all()
    routine = timedelta(minutes=ctx.settings.feed_refresh_minutes)
    late = max(routine, LATE_READ_EVERY)
    needed: Dict[int, timedelta] = {}
    for feed_id, state, air_at in rows:
        every = routine if ctx.now - air_at <= HUNT_WINDOW else late
        if state in (FeedState.LOCKED, FeedState.PINNED, FeedState.ASSIGNED) and feed_id is not None:
            ids = [feed_id]
        else:
            ids = [f.id for f in all_feeds]
        for feed in ids:
            needed[feed] = min(needed.get(feed, every), every)
    return needed


def _idle_read(ctx: Context, all_feeds: List[Feed]) -> Set[int]:
    """The feeds the idle read keeps up: every feed while a followed show may still take its releases from any,
    else only the feeds the locked and pinned shows read, since nothing another feed carries can be theirs.
    """
    shows = [s for s in ingest.followed_shows(ctx) if not s.paused]
    if any(s.feed_state not in FIXED_FEED_STATES or s.feed_id is None for s in shows):
        return {f.id for f in all_feeds}
    return {s.feed_id for s in shows}


def next_read_at(ctx: Context, interval: timedelta) -> Optional[datetime]:
    """When the routine read next has a feed to read: after ``interval`` while a show needs one,
    else when the first feed has been idle long enough or the next wanted episode airs (None: neither).
    """
    all_feeds = list(ctx.session.exec(select(Feed)).all())
    if not all_feeds:
        return None
    if _needed(ctx, all_feeds):
        return ctx.now + interval
    idle = _idle_read(ctx, all_feeds)
    times = [
        ctx.now if f.last_fetch_at is None else f.last_fetch_at + IDLE_FEED_REFRESH
        for f in all_feeds if f.last_fetch_at is None or f.id in idle
    ]
    airs = ctx.session.exec(
        select(func.min(Episode.air_at))
        .join(Show, Show.id == Episode.show_id)
        .where(
            Show.completed == False,  # noqa: E712
            Show.paused == False,  # noqa: E712
            Episode.status.in_((EpisodeStatus.WANTED, EpisodeStatus.MISSED)),
            Episode.air_at > ctx.now,
        )
    ).first()
    if airs is not None:
        times.append(airs)
    if not times:
        return None
    return max(ctx.now, min(times))


def plan_fetch(ctx: Context, everything: bool = False) -> List[FeedRef]:
    """The feeds to read now, in priority order: those a show needs, and the rest once they have been idle long enough."""
    all_feeds = list(ctx.session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all())
    if everything:
        return [_ref(feed) for feed in all_feeds]
    needed = _needed(ctx, all_feeds)
    idle = _idle_read(ctx, all_feeds)
    return [
        _ref(feed) for feed in all_feeds
        if feed.last_fetch_at is None
        or (feed.id in needed and ctx.now - feed.last_fetch_at >= needed[feed.id] - READ_SLACK)
        or (feed.id in idle and ctx.now - feed.last_fetch_at >= IDLE_FEED_REFRESH)
    ]


def apply_fetch(ctx: Context, results: Dict[int, FetchResult]) -> Outcome:
    """Store what the reads returned and record each feed's health. A string result is the error of a failed read.

    A feed a show reads that failed transiently is left unread, so the next routine read tries it again.
    """
    outcome = Outcome()
    known = ingest._known(ctx)
    stored_feeds = {key[0] for key in known}
    used: Optional[Set[int]] = None
    for feed_id, result in results.items():
        feed = ctx.session.get(Feed, feed_id)
        if feed is None:
            continue
        feed.last_fetch_at = ctx.now
        if isinstance(result, str):
            if getattr(result, "transient", False):
                if used is None:
                    all_feeds = list(ctx.session.exec(select(Feed)).all())
                    used = set(_needed(ctx, all_feeds)) | _idle_read(ctx, all_feeds)
                if feed.id in used:
                    feed.last_fetch_at = None
            if feed.failing_since is None:
                logger.warning(f"Feed '{feed.name}' could not be read: {result}")
            feed.last_error = str(result)
            feed.failing_since = feed.failing_since or ctx.now
            outcome.failed[feed_id] = result
            ctx.session.add(feed)
            ctx.emit("feeds")
            continue
        if feed.failing_since is not None:
            logger.info(f"Feed '{feed.name}' can be read again.")
        if feed.failing_since is not None or feed.last_error:
            ctx.emit("feeds")
        feed.last_ok_at, feed.failing_since, feed.last_error = ctx.now, None, None
        ctx.session.add(feed)
        if result and feed.id in stored_feeds and not any((feed.id, a.guid) in known for a in result):
            outcome.gaps.append(feed.id)
        dated = [a.published_at for a in result if a.published_at is not None]
        if dated:
            ctx.caches.read_windows[feed.id] = (min(dated), ctx.now)
        stored = ingest.ingest(ctx, feed, result)
        outcome.new += stored.new
        outcome.matched_show_ids |= stored.show_ids
        outcome.reconsider |= stored.reconsider
    return outcome


def plan_hunt(ctx: Context) -> HuntInfo:
    """The shows in their hunting window, the feeds they may read, and when the next show starts hunting."""
    shows = [s for s in ctx.session.exec(select(Show).where(Show.completed == False, Show.paused == False)).all()]  # noqa: E712
    facts = facts_for(ctx, shows)
    plan = hunt_plan(facts.values(), ctx.now, ctx.tolerance_hours)
    all_feeds = list(ctx.session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all())
    wanted: Dict[int, Feed] = {}
    by_id = {s.id: s for s in shows}
    for show_id in plan.hunting:
        show = by_id[show_id]
        if show.feed_state in (FeedState.LOCKED, FeedState.PINNED, FeedState.ASSIGNED) and show.feed_id is not None:
            chosen = [f for f in all_feeds if f.id == show.feed_id]
        else:
            chosen = all_feeds
        for feed in chosen:
            wanted[feed.id] = feed
    interval = max(1, ctx.settings.hunt_interval_minutes)
    # A feed another read fetched moments ago (the startup read, say) has nothing new yet.
    due_at = {f.id: f.last_fetch_at + timedelta(minutes=interval) if f.last_fetch_at else ctx.now for f in wanted.values()}
    due = [_ref(f) for f in wanted.values() if due_at[f.id] <= ctx.now]
    wait = None
    if wanted and not due:
        wait = (min(due_at.values()) - ctx.now).total_seconds()
    return HuntInfo(list(plan.hunting), due, plan, interval, wait)
