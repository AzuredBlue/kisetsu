"""What the UI shows, built from the database. The UI decides nothing: statuses, labels and roles are settled here."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlmodel import select

from kisetsu.db.schema import (
    Download,
    DownloadState,
    Episode,
    EpisodeOffset,
    EpisodeStatus,
    Event,
    EventKind,
    Feed,
    FeedItem,
    FeedItemMatch,
    FeedState,
    Show,
)
from kisetsu.domain.patterns import compress_home_path, resolve_save_path
from kisetsu.domain.schedule import is_unreleased
from kisetsu.domain.status import ShowStatus, show_status
from kisetsu.services import downloads, shows as shows_service
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for

STATUS_LABELS = {
    ShowStatus.PAUSED: "Paused",
    ShowStatus.COMPLETED: "Completed",
    ShowStatus.WORKING: "Working",
    ShowStatus.UPCOMING: "Upcoming",
    ShowStatus.WAITING: "Waiting",
    ShowStatus.TESTING: "Testing",
}
FEED_STATE_LABELS = {
    FeedState.NONE: "Not found yet",
    FeedState.CANDIDATE: "Found",
    FeedState.ASSIGNED: "Testing",
    FeedState.PINNED: "Picked",
    FeedState.LOCKED: "Working",
}
IN_FLIGHT = (DownloadState.ADDING, DownloadState.VERIFYING, DownloadState.DOWNLOADING, DownloadState.RETRY_WAIT)
HISTORY_KINDS = (EventKind.GRABBED, EventKind.DONE, EventKind.REPLACED)
ITEM_DOWNLOAD_LABELS = {
    DownloadState.ADDING: "Queued",
    DownloadState.VERIFYING: "Queued",
    DownloadState.RETRY_WAIT: "Queued",
    DownloadState.DOWNLOADING: "Downloading",
    DownloadState.DONE: "Downloaded",
}


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _hues(show: Show) -> Optional[List[float]]:
    if not show.accent_hues:
        return None
    try:
        return [float(part) for part in show.accent_hues.split(",")]
    except ValueError:
        return None


def _episode_counts(ctx: Context) -> Dict[int, Dict[EpisodeStatus, int]]:
    counts: Dict[int, Dict[EpisodeStatus, int]] = defaultdict(dict)
    rows = ctx.session.exec(select(Episode.show_id, Episode.status, func.count(Episode.id)).group_by(Episode.show_id, Episode.status))
    for show_id, status, total in rows:
        counts[show_id][status] = total
    return counts


def _replacing(ctx: Context) -> Dict[int, int]:
    rows = ctx.session.exec(
        select(Episode.show_id, func.count(Download.id))
        .join(Episode, Episode.id == Download.episode_id)
        .where(Download.supersedes_id.is_not(None), Download.state.in_(IN_FLIGHT))
        .group_by(Episode.show_id)
    )
    return dict(rows.all())


def _last_done(ctx: Context) -> Dict[int, int]:
    rows = ctx.session.exec(
        select(Episode.show_id, func.max(Episode.number)).where(Episode.status == EpisodeStatus.COMPLETED).group_by(Episode.show_id)
    )
    return dict(rows.all())


def _aired(ctx: Context) -> Dict[int, int]:
    rows = ctx.session.exec(
        select(Episode.show_id, func.count(Episode.id)).where(Episode.air_at <= ctx.now).group_by(Episode.show_id)
    )
    return dict(rows.all())


def attention(counts: Dict[EpisodeStatus, int], feed: Optional[Feed]) -> List[str]:
    """Only what needs the user's eye, in plain words."""
    notes = []
    if counts.get(EpisodeStatus.FAILED):
        notes.append(f"{counts[EpisodeStatus.FAILED]} failed")
    if counts.get(EpisodeStatus.MISSED):
        notes.append(f"{counts[EpisodeStatus.MISSED]} missed")
    if feed is not None and feed.failing_since is not None:
        notes.append(f"{feed.name} is failing")
    return notes


def show_card(
    ctx: Context,
    show: Show,
    status: ShowStatus,
    counts: Dict[EpisodeStatus, int],
    feed: Optional[Feed],
    *,
    replacing: int = 0,
    aired: int = 0,
    last_done: Optional[int] = None,
    released: bool = True,
) -> Dict[str, Any]:
    language = ctx.settings.title_language
    title = show.title(language)
    done = counts.get(EpisodeStatus.COMPLETED, 0)
    return {
        "id": show.id,
        "anilist_id": show.anilist_id,
        "title": title,
        "title_romaji": show.title_romaji,
        "title_english": show.title_english,
        "cover": show.cover_image,
        "banner": show.banner_image,
        "accent_hues": _hues(show),
        "season": " ".join(part for part in (show.season_name and show.season_name.title(), str(show.season_year or "") or None) if part) or None,
        "anilist_status": show.anilist_status,
        "status": status.value,
        "status_label": STATUS_LABELS[status],
        "released": released,
        "last_done": last_done,
        "paused": show.paused,
        "paused_by": show.paused_by.value if show.paused_by else None,
        "completed": show.completed,
        "feed": None if feed is None else {
            "id": feed.id,
            "name": feed.name,
            "state": show.feed_state.value,
            "state_label": FEED_STATE_LABELS[show.feed_state],
            "failing": feed.failing_since is not None,
        },
        "progress": {"done": done, "aired": max(aired, done), "total": show.total_episodes},
        "counts": {
            "completed": done,
            "downloading": counts.get(EpisodeStatus.DOWNLOADING, 0),
            "wanted": counts.get(EpisodeStatus.WANTED, 0),
            "missed": counts.get(EpisodeStatus.MISSED, 0),
            "failed": counts.get(EpisodeStatus.FAILED, 0),
            "replacing": replacing,
        },
        "attention": attention(counts, feed),
        "next_airing": None if not show.next_airing_at else {"episode": show.next_airing_episode, "at": iso(show.next_airing_at)},
        "save_folder": show.save_folder,
        "save_path": compress_home_path(resolve_save_path(ctx.settings.base_dir, title, show.save_folder)),
        "category": show.category,
        "seed_ratio": show.seed_ratio,
        "custom_aliases": show.custom_aliases,
        "learned_name": show.learned_name,
        "schedule_synced_at": iso(show.schedule_synced_at),
    }


def shows_view(ctx: Context) -> List[Dict[str, Any]]:
    shows = ctx.session.exec(select(Show)).all()
    feeds = {feed.id: feed for feed in ctx.session.exec(select(Feed)).all()}
    facts = facts_for(ctx, shows)
    counts, replacing, aired, last = _episode_counts(ctx), _replacing(ctx), _aired(ctx), _last_done(ctx)
    return [
        show_card(
            ctx, show, show_status(facts[show.id], ctx.now), counts.get(show.id, {}), feeds.get(show.feed_id),
            replacing=replacing.get(show.id, 0), aired=aired.get(show.id, 0), last_done=last.get(show.id),
            released=not is_unreleased(facts[show.id], ctx.now),
        )
        for show in shows
    ]


def episode_row(ctx: Context, episode: Episode, feed_names: Dict[int, str]) -> Dict[str, Any]:
    current = downloads.current(ctx, episode)
    active = downloads.active_download(ctx, episode)
    release = current or active
    return {
        "id": episode.id,
        "number": episode.number,
        "status": episode.status.value,
        "air_at": iso(episode.air_at),
        "schedule_state": episode.schedule_state.value,
        "attempts": episode.attempts,
        "retry_after": iso(episode.retry_after),
        "last_error": episode.last_error,
        "release": None if release is None else {
            "title": release.title,
            "version": release.version,
            "group": release.release_group,
            "feed": feed_names.get(release.feed_id),
            "source_episode": release.source_episode,
            "adopted": release.adopted,
            "tracked": release.info_hash is not None,
            "state": release.state.value,
            "completed_at": iso(release.completed_at),
        },
        "replacing": None if active is None or active.supersedes_id is None else {"title": active.title, "version": active.version, "state": active.state.value},
    }


def show_view(ctx: Context, show: Show) -> Dict[str, Any]:
    card = card_for(ctx, show)
    feed_names = {feed.id: feed.name for feed in ctx.session.exec(select(Feed)).all()}
    episodes = ctx.session.exec(select(Episode).where(Episode.show_id == show.id).order_by(Episode.number)).all()
    offsets = ctx.session.exec(select(EpisodeOffset).where(EpisodeOffset.show_id == show.id)).all()
    return {
        **card,
        "episodes": [episode_row(ctx, episode, feed_names) for episode in episodes],
        "episode_offsets": [
            {"feed_id": o.feed_id, "feed": feed_names.get(o.feed_id), "offset": o.offset, "source": o.source.value, "evidence": o.evidence_count}
            for o in sorted(offsets, key=lambda o: o.feed_id)
        ],
    }


def card_for(ctx: Context, show: Show) -> Dict[str, Any]:
    feed = ctx.session.get(Feed, show.feed_id) if show.feed_id else None
    facts = facts_for(ctx, [show])[show.id]
    counts = _episode_counts_for(ctx, show.id)
    return show_card(
        ctx, show, show_status(facts, ctx.now), counts, feed,
        replacing=_replacing(ctx).get(show.id, 0), aired=_aired(ctx).get(show.id, 0), last_done=_last_done(ctx).get(show.id),
        released=not is_unreleased(facts, ctx.now),
    )


def _episode_counts_for(ctx: Context, show_id: int) -> Dict[EpisodeStatus, int]:
    rows = ctx.session.exec(select(Episode.status, func.count(Episode.id)).where(Episode.show_id == show_id).group_by(Episode.status))
    return dict(rows.all())


def feeds_view(ctx: Context) -> List[Dict[str, Any]]:
    usage = dict(ctx.session.exec(select(Show.feed_id, func.count(Show.id)).where(Show.feed_id.is_not(None)).group_by(Show.feed_id)).all())
    locked = dict(ctx.session.exec(
        select(Show.feed_id, func.count(Show.id)).where(Show.feed_state == FeedState.LOCKED).group_by(Show.feed_id)
    ).all())
    stored = dict(ctx.session.exec(select(FeedItem.feed_id, func.count(FeedItem.id)).group_by(FeedItem.feed_id)).all())
    return [
        {
            "id": feed.id,
            "name": feed.name,
            "url": feed.url,
            "priority": feed.priority,
            "qbit_path": feed.qbit_path,
            "shows": usage.get(feed.id, 0),
            "locked": locked.get(feed.id, 0),
            "items": stored.get(feed.id, 0),
            "last_ok_at": iso(feed.last_ok_at),
            "failing_since": iso(feed.failing_since),
            "last_error": feed.last_error,
        }
        for feed in ctx.session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all()
    ]


def feed_items_view(ctx: Context, feed_id: Optional[int], matched: bool, limit: int, offset: int) -> Dict[str, Any]:
    """The articles Kisetsu has read from the feeds, newest first, with the shows they belong to and what became of them."""
    when = func.coalesce(FeedItem.published_at, FeedItem.first_seen_at)
    query = select(FeedItem)
    recent = select(func.count(FeedItem.id)).where(FeedItem.first_seen_at >= ctx.now - timedelta(days=1))
    if feed_id is not None:
        query = query.where(FeedItem.feed_id == feed_id)
        recent = recent.where(FeedItem.feed_id == feed_id)
    if matched:
        query = query.where(FeedItem.id.in_(select(FeedItemMatch.item_id)))
    total = ctx.session.exec(select(func.count()).select_from(query.subquery())).one()
    rows = ctx.session.exec(query.order_by(when.desc(), FeedItem.id.desc()).offset(offset).limit(limit)).all()
    ids = [row.id for row in rows]
    belongs: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    if ids:
        language = ctx.settings.title_language
        pairs = ctx.session.exec(
            select(FeedItemMatch.item_id, Show).join(Show, Show.id == FeedItemMatch.show_id).where(FeedItemMatch.item_id.in_(ids))
        ).all()
        for item_id, show in pairs:
            belongs[item_id].append({"id": show.id, "title": show.title(language)})
    taken: Dict[Any, str] = {}
    if rows:
        for download in ctx.session.exec(
            select(Download).where(Download.feed_item_guid.in_({row.guid for row in rows}), Download.feed_id.is_not(None)).order_by(Download.id)
        ).all():
            label = ITEM_DOWNLOAD_LABELS.get(download.state)
            if label:
                taken[(download.feed_id, download.feed_item_guid)] = label
    names = {f.id: f.name for f in ctx.session.exec(select(Feed)).all()}
    return {
        "items": [
            {
                "id": row.id,
                "feed_id": row.feed_id,
                "feed": names.get(row.feed_id),
                "title": row.title,
                "published_at": iso(row.published_at),
                "first_seen_at": iso(row.first_seen_at),
                "shows": belongs.get(row.id, []),
                "download": taken.get((row.feed_id, row.guid)),
            }
            for row in rows
        ],
        "total": total,
        "new_recent": ctx.session.exec(recent).one(),
    }


def history_view(ctx: Context, limit: int) -> List[Dict[str, Any]]:
    rows = ctx.session.exec(
        select(Event).where(Event.kind.in_(HISTORY_KINDS)).order_by(Event.at.desc(), Event.id.desc()).limit(limit * 3)
    ).all()
    seen = set()
    events = []
    for e in rows:
        key = (e.show_id, e.episode, e.title)
        if key not in seen:
            seen.add(key)
            events.append(e)
    events = events[:limit]
    shows = {s.id: s for s in ctx.session.exec(select(Show).where(Show.id.in_({e.show_id for e in events if e.show_id}))).all()} if events else {}
    feeds = {f.id: f.name for f in ctx.session.exec(select(Feed)).all()}
    return [
        {
            "id": e.id,
            "at": iso(e.at),
            "kind": e.kind.value,
            "show_id": e.show_id,
            "show": shows[e.show_id].title(ctx.settings.title_language) if e.show_id in shows else None,
            "episode": e.episode,
            "feed": feeds.get(e.feed_id),
            "title": e.title,
            "message": e.message,
        }
        for e in events
    ]


def counts_view(ctx: Context) -> Dict[str, Any]:
    """The totals the header and the nav badges need."""
    shows = ctx.session.exec(select(Show)).all()
    facts = facts_for(ctx, shows)
    by_status: Dict[str, int] = defaultdict(int)
    for show in shows:
        by_status[show_status(facts[show.id], ctx.now).value] += 1
    episodes = dict(ctx.session.exec(select(Episode.status, func.count(Episode.id)).group_by(Episode.status)).all())
    return {
        "shows": len(shows),
        "feeds": ctx.session.exec(select(func.count(Feed.id))).one(),
        "by_status": dict(by_status),
        "episodes": {status.value: episodes.get(status, 0) for status in EpisodeStatus},
        "failing_feeds": [f.name for f in ctx.session.exec(select(Feed).where(Feed.failing_since.is_not(None))).all()],
        "all_paused": shows_service.all_paused(ctx),
    }
