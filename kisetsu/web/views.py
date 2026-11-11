"""What the UI shows, built from the database. The UI decides nothing: statuses, labels and roles are settled here."""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlmodel import select

from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    Download,
    DownloadState,
    EndReason,
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
from kisetsu.services import downloads, grab, manual, shows as shows_service
from kisetsu.services.context import Context, get_state
from kisetsu.services.facts import facts_for, release_delays

STATUS_LABELS = {
    ShowStatus.PAUSED: "Paused",
    ShowStatus.COMPLETED: "Completed",
    ShowStatus.WORKING: "Working",
    ShowStatus.UPCOMING: "Upcoming",
    ShowStatus.WAITING: "Waiting",
    # Looking for its first release: the feeds are tried by priority.
    ShowStatus.TESTING: "Searching",
}
# The one vocabulary for how a show's feed was chosen; the UI uses these words everywhere.
FEED_STATE_LABELS = {
    FeedState.NONE: "Auto-discover",
    FeedState.CANDIDATE: "Trying",
    FeedState.ASSIGNED: "Trying",
    FeedState.PINNED: "Picked by you",
    FeedState.LOCKED: "Locked",
}
HISTORY_KINDS = (EventKind.GRABBED, EventKind.DONE, EventKind.REPLACED, EventKind.FAILED, EventKind.MISSED)
# An add that qBittorrent still hasn't shown after this long means qBittorrent is not picking up new torrents.
STUCK_ADD_AFTER = timedelta(hours=1)
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


def _replacing(ctx: Context, show_id: Optional[int] = None) -> Dict[int, int]:
    query = (
        select(Episode.show_id, func.count(Download.id))
        .join(Episode, Episode.id == Download.episode_id)
        .where(Download.supersedes_id.is_not(None), Download.state.in_(ACTIVE_DOWNLOAD_STATES))
    )
    if show_id is not None:
        query = query.where(Episode.show_id == show_id)
    return dict(ctx.session.exec(query.group_by(Episode.show_id)).all())


def _last_done(ctx: Context, show_id: Optional[int] = None) -> Dict[int, int]:
    query = select(Episode.show_id, func.max(Episode.number)).where(Episode.status == EpisodeStatus.COMPLETED)
    if show_id is not None:
        query = query.where(Episode.show_id == show_id)
    return dict(ctx.session.exec(query.group_by(Episode.show_id)).all())


def _aired(ctx: Context, show_id: Optional[int] = None) -> Dict[int, int]:
    query = select(Episode.show_id, func.count(Episode.id)).where(Episode.air_at <= ctx.now)
    if show_id is not None:
        query = query.where(Episode.show_id == show_id)
    return dict(ctx.session.exec(query.group_by(Episode.show_id)).all())


# Air dates older than this are not sent: a long-running show would otherwise send hundreds of years-old dates on every refresh.
AIRINGS_KEEP = timedelta(days=365)


def _airings(ctx: Context, show_id: Optional[int] = None) -> Dict[int, List[Dict[str, Any]]]:
    """Each show's known episode air times of the last year, for the calendar: breaks between episodes are real gaps, not every week."""
    query = select(Episode.show_id, Episode.number, Episode.air_at, Episode.status).where(
        Episode.air_at.is_not(None), Episode.air_at >= ctx.now - AIRINGS_KEEP,
    )
    if show_id is not None:
        query = query.where(Episode.show_id == show_id)
    airings: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for sid, number, air_at, status in ctx.session.exec(query.order_by(Episode.number)).all():
        airings[sid].append({"episode": number, "at": iso(air_at), "status": status.value, "done": status == EpisodeStatus.COMPLETED})
    return airings


def show_section(show: Show, released: bool) -> str:
    """The Shows page section: Completed once it finished airing, even while episodes are still downloading."""
    if show.completed or show.anilist_status == "FINISHED":
        return "completed"
    return "releasing" if released else "planned"


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
    airings: Optional[List[Dict[str, Any]]] = None,
    release_delay: Optional[timedelta] = None,
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
        "cover_color": show.cover_color,
        "banner": show.banner_image,
        "accent_hues": _hues(show),
        "season": " ".join(part for part in (show.season_name and show.season_name.title(), str(show.season_year or "") or None) if part) or None,
        "anilist_status": show.anilist_status,
        "status": status.value,
        "status_label": STATUS_LABELS[status],
        "released": released,
        "section": show_section(show, released),
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
        "next_airing": None if not show.next_airing_at else {"episode": show.next_airing_episode, "at": iso(show.next_airing_at)},
        "airings": airings or [],
        # How long after its air time a release usually comes out; the UI adds it to each air time in the viewer's timezone.
        "release_delay_minutes": None if release_delay is None else round(release_delay.total_seconds() / 60),
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
    airings = _airings(ctx)
    delays = release_delays(ctx)
    return [
        show_card(
            ctx, show, show_status(facts[show.id], ctx.now), counts.get(show.id, {}), feeds.get(show.feed_id),
            replacing=replacing.get(show.id, 0), aired=aired.get(show.id, 0), last_done=last.get(show.id),
            released=not is_unreleased(facts[show.id], ctx.now), airings=airings.get(show.id), release_delay=delays.get(show.id),
        )
        for show in shows
    ]


def _stalled(ctx: Context, show: Show, episode: Episode, active: Optional[Download], memo: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Set on an episode whose active download the last settle pass found stalled; ``alternatives`` is how many other releases could replace it."""
    if active is None or active.id not in ctx.caches.stalled:
        return None
    if "found" not in memo:
        memo["found"] = manual.candidates(ctx, show)
    return {"alternatives": len(manual.alternatives(ctx, show, episode, active, memo["found"]))}


def episode_row(
    ctx: Context, episode: Episode, feed_names: Dict[int, str], show: Optional[Show] = None, memo: Optional[Dict[str, Any]] = None,
    loaded: Optional[Tuple[Dict[int, Download], Dict[int, Download]]] = None,
) -> Dict[str, Any]:
    """One episode's row. ``loaded`` is (downloads by id, newest active download by episode id) read once for the whole show."""
    if loaded is None:
        current = downloads.current(ctx, episode)
        active = downloads.active_download(ctx, episode)
    else:
        by_id, active_by_episode = loaded
        current = by_id.get(episode.current_download_id) if episode.current_download_id else None
        active = active_by_episode.get(episode.id)
    release = current or active
    return {
        "stalled": None if show is None or memo is None else _stalled(ctx, show, episode, active, memo),
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
    memo: Dict[str, Any] = {}
    # Every download of the show at once, instead of two queries per episode.
    rows = ctx.session.exec(
        select(Download).join(Episode, Episode.id == Download.episode_id).where(Episode.show_id == show.id)
        .order_by(Download.created_at, Download.id)
    ).all()
    by_id = {dl.id: dl for dl in rows}
    active_by_episode = {dl.episode_id: dl for dl in rows if dl.state in ACTIVE_DOWNLOAD_STATES}
    return {
        **card,
        "episodes": [episode_row(ctx, episode, feed_names, show, memo, (by_id, active_by_episode)) for episode in episodes],
        "matched_season": grab.enforced_season(ctx, show, facts_for(ctx, [show])[show.id]),
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
        replacing=_replacing(ctx, show.id).get(show.id, 0), aired=_aired(ctx, show.id).get(show.id, 0), last_done=_last_done(ctx, show.id).get(show.id),
        released=not is_unreleased(facts, ctx.now), airings=_airings(ctx, show.id).get(show.id),
        release_delay=release_delays(ctx, [show.id]).get(show.id),
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


def _superseded_grabs(ctx: Context) -> Set[int]:
    """Grabs that aren't queued any more: something else has happened to the episode since, like another release, an adoption or a failure."""
    newest_at: Dict[Tuple[int, int], datetime] = {}
    for show_id, episode, at in ctx.session.exec(
        select(Event.show_id, Event.episode, func.max(Event.at))
        .where(Event.kind != EventKind.GRABBED, Event.show_id.is_not(None), Event.episode.is_not(None))
        .group_by(Event.show_id, Event.episode)
    ).all():
        newest_at[(show_id, episode)] = at
    grabs = ctx.session.exec(
        select(Event.id, Event.show_id, Event.episode, Event.at)
        .where(Event.kind == EventKind.GRABBED, Event.show_id.is_not(None), Event.episode.is_not(None))
        .order_by(Event.id.desc())
    ).all()
    latest = set()
    superseded = set()
    for id_, show_id, episode, at in grabs:
        key = (show_id, episode)
        settled = newest_at.get(key)
        if key in latest or (settled is not None and settled >= at):
            superseded.add(id_)
        latest.add(key)
    return superseded


def history_view(ctx: Context, limit: int) -> List[Dict[str, Any]]:
    rows = ctx.session.exec(
        select(Event).where(Event.kind.in_(HISTORY_KINDS)).order_by(Event.at.desc(), Event.id.desc()).limit(limit * 3)
    ).all()
    superseded = _superseded_grabs(ctx)
    seen = set()
    events = []
    for e in rows:
        key = (e.show_id, e.episode, e.title)
        if e.id not in superseded and key not in seen:
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
            "cover": shows[e.show_id].cover_image if e.show_id in shows else None,
            "cover_color": shows[e.show_id].cover_color if e.show_id in shows else None,
            "episode": e.episode,
            "feed": feeds.get(e.feed_id),
            "title": e.title,
            "message": e.message,
        }
        for e in events
    ]


ATTENTION_ORDER = {"stalled": 0, "failed": 1, "retrying": 2, "missed": 3}
# The attention rows the user closed, as a JSON list of keys. Only the list hides them: the episode itself is left as it is.
ATTENTION_DISMISSED_KEY = "attention_dismissed"


def dismissed_attention(ctx: Context) -> Set[str]:
    try:
        keys = json.loads(get_state(ctx, ATTENTION_DISMISSED_KEY) or "[]")
    except ValueError:
        return set()
    return set(keys) if isinstance(keys, list) else set()


def attention_view(ctx: Context, include_dismissed: bool = False) -> List[Dict[str, Any]]:
    """Episodes across every show that need the user's eye, each with the one action that helps, if any.

    Stalled and failed downloads first, then those waiting to be tried again, then episodes no feed carries any more.
    Paused and completed shows are left out, and so are rows the user closed. A closed row is keyed by its episode, its
    kind and its current download, so the same episode coming back for another reason is shown again. A show's missed
    episodes share one row, which holds the key of each.
    """
    language = ctx.settings.title_language
    dismissed = set() if include_dismissed else dismissed_attention(ctx)
    shows = {s.id: s for s in ctx.session.exec(select(Show).where(Show.paused.is_(False), Show.completed.is_(False))).all()}
    if not shows:
        return []
    stalled_episodes = {
        dl.episode_id: dl for dl in ctx.session.exec(select(Download).where(Download.id.in_(ctx.caches.stalled))).all()
    } if ctx.caches.stalled else {}
    rows = ctx.session.exec(select(Episode).where(Episode.show_id.in_(list(shows)))).all()
    found_for: Dict[int, List[Any]] = {}
    missed_rows: Dict[int, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []
    for episode in rows:
        show = shows[episode.show_id]
        aired = episode.air_at is None or episode.air_at <= ctx.now
        if episode.id in stalled_episodes:
            kind, reason = "stalled", "The torrent has moved no data for hours."
        elif episode.status == EpisodeStatus.FAILED:
            kind, reason = "failed", episode.last_error or "The download failed."
        elif episode.status == EpisodeStatus.WANTED and aired and episode.last_error and episode.retry_after and episode.retry_after > ctx.now:
            kind, reason = "retrying", episode.last_error
        elif episode.status == EpisodeStatus.MISSED:
            kind, reason = "missed", "No feed carries it any more. Search older asks the feeds' sites for it."
        else:
            continue
        key = f"{episode.id}:{kind}:{episode.current_download_id or 0}"
        if key in dismissed:
            continue
        action = None
        if kind == "missed":
            action = {"type": "search"}
        elif kind in ("failed", "stalled"):
            if show.id not in found_for:
                found_for[show.id] = manual.candidates(ctx, show)
            if kind == "stalled":
                if manual.alternatives(ctx, show, episode, stalled_episodes[episode.id], found_for[show.id]):
                    action = {"type": "try_another"}
            else:
                retry = manual.retry_release(ctx, show, episode, found_for[show.id])
                if retry is not None:
                    action = {"type": "download", "title": retry}
        if kind == "missed" and show.id in missed_rows:
            # One search looks for every missed episode of a show, so they share one row and one Search older.
            row = missed_rows[show.id]
            row["episodes"].append(episode.number)
            row["keys"].append(key)
            row["reason"] = "No feed carries them any more. Search older asks the feeds' sites for them."
            continue
        row = {
            "show_id": show.id,
            "show": show.title(language),
            "cover": show.cover_image,
            "episode_id": episode.id,
            "episodes": [episode.number],
            "kind": kind,
            "keys": [key],
            "reason": reason,
            "retry_after": iso(episode.retry_after) if kind == "retrying" else None,
            "action": action,
        }
        if kind == "missed":
            missed_rows[show.id] = row
        out.append(row)
    for row in out:
        row["episodes"].sort()
    out.sort(key=lambda row: (ATTENTION_ORDER[row["kind"]], row["show"].casefold(), -row["episodes"][-1]))
    return out


def stuck_adds(ctx: Context) -> int:
    """Episodes whose add qBittorrent seems to be ignoring, from the database alone (read sessions cannot reach qBittorrent).

    Either an add that is still ADDING or RETRY_WAIT after STUCK_ADD_AFTER, or a give-up after repeated attempts
    whose episode is waiting to be tried again and has not been supplied since. A replacement that gave up after
    48 hours is a slow torrent, not a lost add, so it does not count. Paused and completed shows are left out.
    """
    live = (Show.paused.is_(False), Show.completed.is_(False))
    waiting = ctx.session.exec(
        select(Download.episode_id)
        .join(Episode, Episode.id == Download.episode_id)
        .join(Show, Show.id == Episode.show_id)
        .where(
            Download.state.in_((DownloadState.ADDING, DownloadState.RETRY_WAIT)),
            Download.created_at < ctx.now - STUCK_ADD_AFTER,
            Episode.status != EpisodeStatus.COMPLETED,
            *live,
        )
    ).all()
    gave_up = ctx.session.exec(
        select(Download.episode_id)
        .join(Episode, Episode.id == Download.episode_id)
        .join(Show, Show.id == Episode.show_id)
        .where(
            Download.state == DownloadState.CANCELED,
            Download.end_reason == EndReason.ADD_GAVE_UP,
            Episode.retry_after > ctx.now,
            Episode.status.not_in((EpisodeStatus.COMPLETED, EpisodeStatus.SKIPPED)),
            *live,
        )
    ).all()
    return len(set(waiting) | set(gave_up))


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
        "stuck_adds": stuck_adds(ctx),
    }
