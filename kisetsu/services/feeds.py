"""The feed list and which feed each show downloads from."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Set

from sqlmodel import select

from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    FIXED_FEED_STATES,
    Download,
    DownloadState,
    Episode,
    EpisodeOffset,
    EventKind,
    Feed,
    FeedState,
    OffsetSource,
    Show,
)
from kisetsu.domain.schedule import premiere_out_of_reach
from kisetsu.services import events
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for

# A lower-ranked feed that carries a release first waits this long for a higher-ranked one.
CANDIDATE_GRACE = timedelta(seconds=300)


class Evidence(str, Enum):
    """What proved that a show's releases really come from a feed."""

    GRAB = "grab"
    ADOPTED_UNIQUE = "adopted_unique"
    SAVED_FILES_UNIQUE = "saved_files_unique"


class FeedLockedError(Exception):
    """The show is locked to another feed and the caller did not agree to release it."""


@dataclass(frozen=True)
class Carrier:
    """A feed that carries a release for a show, and when that release was published."""

    feed: Feed
    published_at: Optional[datetime] = None


def ranked(ctx: Context) -> List[Feed]:
    return list(ctx.session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all())


def _reprioritise(ctx: Context) -> None:
    for index, feed in enumerate(ranked(ctx), start=1):
        if feed.priority != index:
            feed.priority = index
            ctx.session.add(feed)


def add_feed(ctx: Context, name: str, url: str, qbit_path: Optional[str] = None) -> Feed:
    """Add a feed at the lowest priority; a URL that is already listed is returned as it is."""
    existing = ctx.session.exec(select(Feed).where(Feed.url == url)).first()
    if existing is not None:
        return existing
    feed = Feed(name=name, url=url, priority=len(ranked(ctx)) + 1, qbit_path=qbit_path)
    ctx.session.add(feed)
    ctx.session.flush()
    ctx.emit("feeds")
    return feed


def edit_feed(ctx: Context, feed: Feed, *, name: Optional[str] = None, url: Optional[str] = None) -> Feed:
    """Rename a feed or point it at another URL. Items already stored from the old URL stay."""
    if url is not None and url != feed.url:
        clash = ctx.session.exec(select(Feed).where(Feed.url == url, Feed.id != feed.id)).first()
        if clash is not None:
            raise ValueError(f"'{clash.name}' already uses that URL")
        feed.url = url
        feed.failing_since = None
        feed.last_error = None
        # Not read at its new address yet, so the next pass reads it.
        feed.last_fetch_at = None
    if name is not None and name != feed.name:
        feed.name = name
    ctx.session.add(feed)
    ctx.emit("feeds")
    return feed


def _leaf(path: str) -> str:
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def reorder_feeds(ctx: Context, feed_ids: Sequence[int]) -> None:
    """Give the listed feeds priorities 1..n in that order; feeds not listed follow in their old order."""
    feeds = {feed.id: feed for feed in ranked(ctx)}
    order = [feeds[i] for i in dict.fromkeys(feed_ids) if i in feeds]
    order += [feed for feed in feeds.values() if feed not in order]
    for index, feed in enumerate(order, start=1):
        if feed.priority != index:
            feed.priority = index
            ctx.session.add(feed)
    ctx.emit("feeds", "shows")


def import_from_qbit(ctx: Context) -> List[Feed]:
    """Add the feeds in qBittorrent's RSS tree that Kisetsu does not list yet; returns the new ones."""
    added: List[Feed] = []
    known = {feed.url for feed in ranked(ctx)}
    for item in ctx.qbit.rss_feeds():
        if not item.url or item.url in known:
            continue
        known.add(item.url)
        added.append(add_feed(ctx, _leaf(item.path), item.url, qbit_path=item.path))
    return added


def forget_feed(ctx: Context, feed: Feed) -> int:
    """Remove a feed. Shows that were using it go back to discovery; returns how many."""
    shows = list(ctx.session.exec(select(Show).where(Show.feed_id == feed.id)).all())
    for show in shows:
        _clear(show)
        ctx.session.add(show)
    for offset in ctx.session.exec(select(EpisodeOffset).where(EpisodeOffset.feed_id == feed.id)).all():
        ctx.session.delete(offset)
    if ctx.caches.known_items is not None:
        ctx.caches.known_items = {key for key in ctx.caches.known_items if key[0] != feed.id}
    ctx.session.delete(feed)
    ctx.session.flush()
    _reprioritise(ctx)
    ctx.emit("feeds", "shows")
    return len(shows)


def _clear(show: Show) -> None:
    show.feed_id = None
    show.feed_state = FeedState.NONE
    show.candidate_since = None
    show.learned_name = None
    show.learned_group = None


def _drop_offsets(ctx: Context, show: Show, feed_id: Optional[int], sources: Iterable[OffsetSource]) -> None:
    query = select(EpisodeOffset).where(EpisodeOffset.show_id == show.id, EpisodeOffset.source.in_(list(sources)))
    if feed_id is not None:
        query = query.where(EpisodeOffset.feed_id == feed_id)
    for offset in ctx.session.exec(query).all():
        ctx.session.delete(offset)


def carriers_in_order(ctx: Context, carriers: Sequence[Carrier], excluded: Set[int]) -> List[Carrier]:
    rank = {feed.id: index for index, feed in enumerate(ranked(ctx))}
    usable = [c for c in carriers if c.feed.id not in excluded]
    return sorted(usable, key=lambda c: rank.get(c.feed.id, len(rank)))


def assign(ctx: Context, show: Show, carriers: Sequence[Carrier], excluded: Set[int] = frozenset()) -> Optional[Feed]:
    """Decide which feed a show without a proven one may download from now.

    ``carriers`` are the feeds that carry a release the show could use. The best-ranked feed
    is taken at once when it is the top-ranked feed overall; a lower-ranked one is only a
    candidate until the grace period, counted from when it published the release, has passed
    without a higher-ranked feed carrying it. Returns the feed to grab from, or None while
    the show has to wait.
    """
    if show.feed_state in FIXED_FEED_STATES:
        return ctx.session.get(Feed, show.feed_id)
    usable = carriers_in_order(ctx, carriers, excluded)
    if not usable:
        return ctx.session.get(Feed, show.feed_id) if show.feed_state == FeedState.ASSIGNED else None
    best = usable[0]
    if show.feed_state == FeedState.ASSIGNED and any(c.feed.id == show.feed_id for c in usable):
        return ctx.session.get(Feed, show.feed_id)
    top = next((f for f in ranked(ctx) if f.id not in excluded), None)
    if top is not None and best.feed.id == top.id:
        _set_assigned(ctx, show, best.feed)
        return best.feed
    if show.feed_state != FeedState.CANDIDATE or show.feed_id != best.feed.id:
        show.feed_id = best.feed.id
        show.feed_state = FeedState.CANDIDATE
        show.candidate_since = best.published_at or ctx.now
        ctx.session.add(show)
        ctx.emit("shows")
    since = show.candidate_since or ctx.now
    if since + CANDIDATE_GRACE > ctx.now:
        return None
    _set_assigned(ctx, show, best.feed)
    return best.feed


def next_candidate_due(ctx: Context) -> Optional[datetime]:
    """When the earliest pending candidate becomes assignable, for the scheduler's timer."""
    due = [
        (show.candidate_since or ctx.now) + CANDIDATE_GRACE
        for show in ctx.session.exec(select(Show).where(Show.feed_state == FeedState.CANDIDATE)).all()
    ]
    return min(due) if due else None


def _set_assigned(ctx: Context, show: Show, feed: Feed) -> None:
    if show.feed_state == FeedState.ASSIGNED and show.feed_id == feed.id:
        return
    show.feed_id = feed.id
    show.feed_state = FeedState.ASSIGNED
    show.candidate_since = None
    ctx.session.add(show)
    ctx.emit("shows")


def lock(
    ctx: Context,
    show: Show,
    feed: Feed,
    evidence: Evidence,
    *,
    learned_name: Optional[str] = None,
    learned_group: Optional[str] = None,
) -> bool:
    """Make a feed the show's only source because a real release came from it.

    A show pinned to another feed is not moved; returns whether the lock was set.
    """
    if show.feed_state == FeedState.PINNED and show.feed_id != feed.id:
        return False
    if show.feed_state == FeedState.LOCKED and show.feed_id == feed.id:
        changed = False
    else:
        show.feed_id = feed.id
        show.feed_state = FeedState.LOCKED
        show.candidate_since = None
        changed = True
    if learned_name and show.learned_name != learned_name:
        show.learned_name = learned_name
        changed = True
    if learned_group and show.learned_group != learned_group:
        show.learned_group = learned_group
        changed = True
    if changed:
        ctx.session.add(show)
        ctx.emit("shows")
        events.record(ctx, EventKind.FEED, f"Locked to '{feed.name}' ({evidence.value.replace('_', ' ')}).", show=show, feed_id=feed.id)
    return changed


def pin(ctx: Context, show: Show, feed: Feed, *, release_lock: bool = False) -> None:
    """The user picks the feed. Moving off a locked feed needs ``release_lock``."""
    if show.feed_state == FeedState.LOCKED and show.feed_id != feed.id:
        if not release_lock:
            raise FeedLockedError(f"'{show.title()}' is locked to another feed")
        _drop_offsets(ctx, show, show.feed_id, (OffsetSource.INFERRED, OffsetSource.CONFIRMED))
        show.learned_name = None
        show.learned_group = None
    if not (show.feed_state == FeedState.LOCKED and show.feed_id == feed.id):
        show.feed_id = feed.id
        show.feed_state = FeedState.PINNED
    show.candidate_since = None
    ctx.session.add(show)
    ctx.emit("shows")


def release(ctx: Context, show: Show) -> None:
    """Back to automatic discovery: clears a pin or a lock and what the lock taught."""
    _drop_offsets(ctx, show, None, (OffsetSource.INFERRED, OffsetSource.CONFIRMED))
    _clear(show)
    ctx.session.add(show)
    ctx.emit("shows")


def unwind_unproven(ctx: Context, show: Show, feed_id: Optional[int]) -> bool:
    """Drop the lock on a feed once nothing downloaded from it is left (every such grab was canceled).

    Returns whether anything was released. A pin stays, and so do offsets the user set.
    """
    if feed_id is None or show.id is None:
        return False
    holder = ctx.session.exec(
        select(Download.id)
        .join(Episode, Episode.id == Download.episode_id)
        .where(
            Episode.show_id == show.id,
            Download.feed_id == feed_id,
            Download.state.in_([*ACTIVE_DOWNLOAD_STATES, DownloadState.DONE, DownloadState.SUPERSEDED]),
        )
        .limit(1)
    ).first()
    if holder is not None:
        return False
    _drop_offsets(ctx, show, feed_id, (OffsetSource.INFERRED, OffsetSource.CONFIRMED))
    if show.feed_state == FeedState.LOCKED and show.feed_id == feed_id:
        _clear(show)
        ctx.session.add(show)
        ctx.emit("shows")
        return True
    return False


def unwind_unreleased(ctx: Context) -> List[Show]:
    """Put back to discovery the shows matched before they started airing.

    Such a match is an earlier season's release. A show that is locked, pinned or has a
    download is left alone.
    """
    candidates = list(
        ctx.session.exec(
            select(Show).where(
                Show.completed == False,  # noqa: E712
                Show.feed_state.in_([FeedState.CANDIDATE, FeedState.ASSIGNED]),
            )
        ).all()
    )
    if not candidates:
        return []
    facts = facts_for(ctx, candidates)
    downloaded = set(
        ctx.session.exec(select(Episode.show_id).join(Download, Download.episode_id == Episode.id)).all()
    )
    undone: List[Show] = []
    for show in candidates:
        if show.id in downloaded or not premiere_out_of_reach(facts[show.id], ctx.now):
            continue
        _clear(show)
        ctx.session.add(show)
        undone.append(show)
        events.record(
            ctx,
            EventKind.INFO,
            "Put back to Upcoming: it hasn't started airing, so the releases it matched were from an earlier season.",
            show=show,
        )
    if undone:
        ctx.emit("shows")
    return undone
