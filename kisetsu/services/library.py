"""The release files already on disk in a show's save folder.

A library carried over from another machine names the feed the show was downloaded from,
so the feed is read from it before anything is guessed.
"""

import logging
import os
from datetime import timedelta
from typing import Any, Callable, Dict, List, Set, Tuple

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.db.schema import ACTIVE_DOWNLOAD_STATES, Download, DownloadState, Episode, Settings, Show
from kisetsu.domain.matcher import build_matcher, decide
from kisetsu.domain.patterns import resolve_save_path
from kisetsu.domain.schedule import PUBLISHED_BEFORE_AIR_SLACK
from kisetsu.services import tags
from kisetsu.services.context import Context

logger = logging.getLogger("kisetsu.services.library")

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi")
# How long a grab waits for the save folder to be matched to a feed, so a search
# that never completes cannot keep a show from downloading.
HOLD = timedelta(hours=1)


def saved_release_names(show: Show, settings: Settings) -> List[str]:
    """Names of the show's video files in its save folder, top level and one folder down."""
    path = resolve_save_path(settings.base_dir, show.title(settings.title_language), show.save_folder)
    if not path or not os.path.isdir(path):
        return []
    names: List[str] = []
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                names.append(entry.name)
            elif entry.is_dir():
                names.extend(inner.name for inner in os.scandir(entry.path) if inner.is_file())
    except OSError as e:
        logger.debug(f"Could not read the save folder of '{show.title()}': {e}")
        return []
    matcher = build_matcher(show.effective_aliases, show.learned_name)
    cache: Dict[str, dict] = {}
    return [
        name for name in sorted(names)
        if name.lower().endswith(VIDEO_EXTENSIONS) and decide(matcher, name, cache)[0]
    ]


def hold(ctx: Context, show_id: int) -> bool:
    """Start holding a show's grabs; True when it was not held already."""
    new = show_id not in ctx.caches.held
    ctx.caches.held.setdefault(show_id, ctx.now)
    return new


def release(ctx: Context, show_id: int) -> None:
    ctx.caches.held.pop(show_id, None)


def held_ids(ctx: Context) -> Set[int]:
    return {show_id for show_id, since in ctx.caches.held.items() if ctx.now - since < HOLD}


def _same_dir(a: Any, b: str) -> bool:
    """Whether two folder paths are one, however each writes its separators (qBittorrent reports "/" on Windows)."""

    def uniform(path: Any) -> str:
        return str(path or "").replace("\\", "/").rstrip("/")

    return uniform(a) == uniform(b)


def show_torrent_hashes(ctx: Context, show: Show) -> List[str]:
    """Every torrent hash the downloads table holds for a show, including in-flight replacements."""
    rows = ctx.session.exec(
        select(Download.info_hash)
        .join(Episode, Episode.id == Download.episode_id)
        .where(
            Episode.show_id == show.id,
            Download.info_hash.is_not(None),
            Download.state.in_([*ACTIVE_DOWNLOAD_STATES, DownloadState.DONE, DownloadState.FAILED]),
        )
    ).all()
    return sorted(set(rows))


def show_torrent_preview(ctx: Context, show: Show) -> List[Dict[str, Any]]:
    """The torrents deleting a show removes: ``hash``, ``name``, ``size`` and whether Kisetsu ``tracked`` it.

    Torrents Kisetsu lost track of are found in its category under the show's own save
    folder, unless another show saves to the same folder. A series folder can also hold the
    torrents of its earlier seasons, so one that qBittorrent added before this show's first
    episode aired, or whose name is not this show's, is left alone.
    """
    ctx.qbit.sync()
    view = ctx.qbit.torrents
    entries: Dict[str, Dict[str, Any]] = {}
    for value in show_torrent_hashes(ctx, show):
        torrent = view.get(value)
        entries[value] = {"hash": value, "name": torrent.name if torrent else None, "size": torrent.size if torrent else None, "tracked": True}
    owned = tags.owners(ctx)
    for torrent in view.values():
        # Its series tag is proof the torrent is this show's, even when the database lost track of it.
        if torrent.hash not in entries and tags.owner_id(torrent, owned) == show.id:
            entries[torrent.hash] = {"hash": torrent.hash, "name": torrent.name, "size": torrent.size, "tracked": True}
    settings = ctx.settings
    save_path = resolve_save_path(settings.base_dir, show.title(settings.title_language), show.save_folder)
    if save_path and settings.default_category:
        shared = any(
            resolve_save_path(settings.base_dir, other.title(settings.title_language), other.save_folder) == save_path
            for other in ctx.session.exec(select(Show).where(Show.id != show.id)).all()
        )
        if not shared:
            first_air = min(
                (v for v in ctx.session.exec(select(Episode.air_at).where(Episode.show_id == show.id, Episode.air_at.is_not(None))).all()),
                default=None,
            )
            slack = max(PUBLISHED_BEFORE_AIR_SLACK, timedelta(hours=ctx.tolerance_hours))
            matcher = build_matcher(show.effective_aliases, show.learned_name)
            cache: Dict[str, dict] = {}
            for torrent in view.values():
                if torrent.hash in entries or torrent.category != settings.default_category:
                    continue
                if tags.owner_id(torrent, owned) not in (None, show.id):
                    continue
                if not _same_dir(torrent.save_path, save_path):
                    continue
                if (first_air is not None and torrent.added_on is not None and torrent.added_on < first_air - slack) or not decide(matcher, torrent.name, cache)[0]:
                    continue
                entries[torrent.hash] = {"hash": torrent.hash, "name": torrent.name, "size": torrent.size, "tracked": False}
    return sorted(entries.values(), key=lambda entry: (not entry["tracked"], entry["name"] or entry["hash"]))


def find_show_torrents(ctx: Context, show: Show) -> List[str]:
    """The hashes of every torrent a deleted show leaves in qBittorrent."""
    return sorted(entry["hash"] for entry in show_torrent_preview(ctx, show))


def plan_language_move(ctx: Context, new_language: str) -> List[Dict[str, Any]]:
    """The folder renames switching to ``new_language`` causes, per show with torrents to move.

    ``settings.title_language`` must still be the current language.
    """
    base_dir = ctx.settings.base_dir
    return plan_move(ctx, list(ctx.session.exec(select(Show)).all()), lambda show: resolve_save_path(base_dir, show.title(new_language), show.save_folder))


def plan_move(ctx: Context, shows: List[Show], new_path: Callable[[Show], str], *, anywhere: bool = False) -> List[Dict[str, Any]]:
    """The folder moves ``new_path`` (a show's future save path) causes, per show with torrents to move.

    The settings and shows must still be as they are now. Torrents are judged by where
    qBittorrent has them now, so one that already moved is left alone. By default only
    torrents in the show's current folder move, so one the user relocated by hand is never
    touched; with ``anywhere`` every download Kisetsu tracks moves, wherever it is now.
    Each entry has ``show``, ``old``, ``new``, ``hashes`` and ``moving``, the torrents
    qBittorrent is still moving.
    """
    plan: List[Dict[str, Any]] = []
    settings = ctx.settings
    for show in shows:
        old = resolve_save_path(settings.base_dir, show.title(settings.title_language), show.save_folder)
        new = new_path(show)
        if not new or (not anywhere and (not old or _same_dir(old, new))):
            continue
        entries = show_torrent_preview(ctx, show)
        found = [(e["tracked"], ctx.qbit.torrents[e["hash"]]) for e in entries if e["hash"] in ctx.qbit.torrents]
        if anywhere:
            here = [t for tracked, t in found if (tracked or _same_dir(t.save_path, old)) and not _same_dir(t.save_path, new)]
        else:
            here = [t for _, t in found if _same_dir(t.save_path, old)]
        moving = [t for _, t in found if t.state.lower() == "moving"]
        if here or moving:
            plan.append({"show": show, "old": old, "new": new, "hashes": [t.hash for t in here], "moving": len(moving)})
    return plan


def relocate_torrents(ctx: Context, plan: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Move what ``plan_language_move`` found; returns the log lines and the names of shows that failed."""
    logs: List[str] = []
    failed: List[str] = []
    for item in plan:
        if not item["hashes"]:
            continue
        name = item["show"].title(ctx.settings.title_language)
        try:
            ctx.qbit.set_location(item["hashes"], item["new"])
        except QbitError as e:
            failed.append(name)
            logs.append(f"Could not move the downloads of {name}: {e}")
            continue
        logs.append(f"Moved {len(item['hashes'])} downloads of {name} to {item['new']}")
    return logs, failed
