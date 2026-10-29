"""The release files already on disk in a show's save folder.

A library carried over from another machine names the feed the show was downloaded from,
so the feed is read from it before anything is guessed.
"""

import logging
import os
import shutil
import stat
import sys
import time
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

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


# -- deleting a show's folder ---------------------------------------------------

# Leftover file names listed in the delete dialog; the rest are only counted.
LEFTOVERS_LISTED = 20


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _within(child: Any, parent: Any) -> bool:
    """Whether ``child`` is ``parent`` or somewhere under it, however either is spelled."""
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def base_root(base_dir: str) -> str:
    """The folder every show's own folder sits in, or "" when no download folder is set."""
    base = (base_dir or "").strip()
    if "{name}" in base:
        base = base.split("{name}", 1)[0]
    base = base.rstrip("/\\")
    return _norm(base) if base else ""


def folder_plan(ctx: Context, show: Show, hashes: List[str]) -> Dict[str, Any]:
    """What deleting a show does to its save folder, judged before its torrents go.

    ``remove`` is set only for a folder strictly inside the download folder that no other
    show saves to and that holds no torrent but the show's own (``hashes``); otherwise
    ``reason`` says why it is kept. ``leftovers`` are the files no torrent of the show owns.
    """
    settings = ctx.settings
    path = resolve_save_path(settings.base_dir, show.title(settings.title_language), show.save_folder)
    plan: Dict[str, Any] = {"path": path, "exists": False, "remove": False, "reason": None, "leftovers": [], "leftover_count": 0}
    if not path or not os.path.isdir(path):
        return plan
    plan["exists"] = True
    view = ctx.qbit.torrents
    own = [view[h] for h in hashes if h in view]
    names: List[str] = []
    try:
        for top, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(top, name)
                if not any(_within(full, t.content_path) for t in own if t.content_path):
                    names.append(os.path.relpath(full, path))
    except OSError as e:
        logger.debug(f"Could not read the folder of '{show.title()}': {e}")
    plan["leftovers"], plan["leftover_count"] = sorted(names)[:LEFTOVERS_LISTED], len(names)
    root = base_root(settings.base_dir)
    if not root:
        plan["reason"] = "No download folder is set in Settings, so Kisetsu never deletes folders."
    elif not _within(path, root) or _norm(path) == root:
        plan["reason"] = "The folder is not inside your download folder."
    elif any(
        (other_path := resolve_save_path(settings.base_dir, other.title(settings.title_language), other.save_folder))
        and (_within(other_path, path) or _within(path, other_path))
        for other in ctx.session.exec(select(Show).where(Show.id != show.id)).all()
    ):
        plan["reason"] = "Another show saves to this folder."
    elif any(
        t.hash not in hashes and ((t.content_path and _within(t.content_path, path)) or (t.save_path and _within(t.save_path, path)))
        for t in view.values()
    ):
        plan["reason"] = "It holds torrents that belong to no show in Kisetsu."
    else:
        plan["remove"] = True
    return plan


def torrents_gone(ctx: Context, hashes: List[str]) -> bool:
    """Whether qBittorrent no longer lists any of ``hashes``."""
    ctx.qbit.sync()
    return not any(h in ctx.qbit.torrents for h in hashes)


def remove_folder(path: str, base_dir: str, *, attempts: int = 5, pause: Callable[[float], None] = time.sleep) -> Optional[str]:
    """Delete a show's folder and everything left in it; returns the error, or None once it is gone.

    The folder is checked again against the download folder right before it goes. A file
    Windows still holds open is retried a few times.
    """
    root = base_root(base_dir)
    if not root or not _within(path, root) or _norm(path) == root:
        return "The folder is not inside your download folder."

    def writable(fn, target, _exc):
        # A read-only file stops rmtree on Windows; clear the flag and try once more.
        os.chmod(target, stat.S_IWRITE)
        fn(target)

    error: Optional[str] = None
    for attempt in range(attempts):
        if not os.path.exists(path):
            return None
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=writable)
            else:
                shutil.rmtree(path, onerror=lambda fn, target, info: writable(fn, target, info[1]))
            return None
        except OSError as e:
            error = str(e)
            if attempt < attempts - 1:
                pause(1.0)
    return error
