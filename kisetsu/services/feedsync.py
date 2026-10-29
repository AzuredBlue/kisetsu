"""Reading the feeds: what a read does to the stored items and the feeds' health."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Set, Union

from sqlmodel import select

from kisetsu.clients.rss import FeedArticle
from kisetsu.db.schema import Episode, EpisodeStatus, Feed, FeedState, Show
from kisetsu.domain.schedule import HuntPlan, hunt_plan
from kisetsu.services import ingest
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for

FetchResult = Union[List[FeedArticle], str]

# A feed no show needs is still read this often, for v2s, batches and releases ahead of their air time.
IDLE_FEED_REFRESH = timedelta(hours=6)
# An aired episode still missing keeps its show's feeds on every routine read this long; then only the idle read looks.
MISSING_FOR = timedelta(days=3)


@dataclass(frozen=True)
class FeedRef:
    id: int
    name: str
    url: str


@dataclass
class Outcome:
    new: int = 0
    matched_show_ids: Set[int] = field(default_factory=set)
    # Feeds whose read had nothing in common with what was stored: items may have scrolled past unseen.
    gaps: List[int] = field(default_factory=list)
    failed: Dict[int, str] = field(default_factory=dict)


@dataclass
class HuntInfo:
    show_ids: List[int]
    feeds: List[FeedRef]
    plan: HuntPlan
    interval_minutes: int = 5


def _ref(feed: Feed) -> FeedRef:
    return FeedRef(feed.id, feed.name, feed.url)


def _needed(ctx: Context, all_feeds: List[Feed]) -> Set[int]:
    """The feeds a show with a recently aired, still missing episode reads: its own, or every feed while it has none."""
    rows = ctx.session.exec(
        select(Show.feed_id, Show.feed_state)
        .join(Episode, Episode.show_id == Show.id)
        .where(
            Show.completed == False,  # noqa: E712
            Show.paused == False,  # noqa: E712
            Episode.status.in_((EpisodeStatus.WANTED, EpisodeStatus.MISSED)),
            Episode.air_at.is_not(None),
            Episode.air_at <= ctx.now,
            Episode.air_at >= ctx.now - MISSING_FOR,
        )
        .distinct()
    ).all()
    needed: Set[int] = set()
    for feed_id, state in rows:
        if state in (FeedState.LOCKED, FeedState.PINNED, FeedState.ASSIGNED) and feed_id is not None:
            needed.add(feed_id)
        else:
            return {f.id for f in all_feeds}
    return needed


def plan_fetch(ctx: Context, everything: bool = False) -> List[FeedRef]:
    """The feeds to read now, in priority order: those a show needs, and the rest once they have been idle long enough."""
    all_feeds = list(ctx.session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all())
    if everything:
        return [_ref(feed) for feed in all_feeds]
    needed = _needed(ctx, all_feeds)
    return [
        _ref(feed) for feed in all_feeds
        if feed.id in needed or feed.last_fetch_at is None or ctx.now - feed.last_fetch_at >= IDLE_FEED_REFRESH
    ]


def apply_fetch(ctx: Context, results: Dict[int, FetchResult]) -> Outcome:
    """Store what the reads returned and record each feed's health. A string result is the error of a failed read."""
    outcome = Outcome()
    known = ingest._known(ctx)
    stored_feeds = {key[0] for key in known}
    for feed_id, result in results.items():
        feed = ctx.session.get(Feed, feed_id)
        if feed is None:
            continue
        feed.last_fetch_at = ctx.now
        if isinstance(result, str):
            feed.last_error = result
            feed.failing_since = feed.failing_since or ctx.now
            outcome.failed[feed_id] = result
            ctx.session.add(feed)
            ctx.emit("feeds")
            continue
        if feed.failing_since is not None or feed.last_error:
            ctx.emit("feeds")
        feed.last_ok_at, feed.failing_since, feed.last_error = ctx.now, None, None
        ctx.session.add(feed)
        if result and feed.id in stored_feeds and not any((feed.id, a.guid) in known for a in result):
            outcome.gaps.append(feed.id)
        stored = ingest.ingest(ctx, feed, result)
        outcome.new += stored.new
        outcome.matched_show_ids |= stored.show_ids
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
    return HuntInfo(list(plan.hunting), [_ref(f) for f in wanted.values()], plan, max(1, ctx.settings.hunt_interval_minutes))
