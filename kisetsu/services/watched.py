"""Episodes the user has watched, going by their AniList progress.

With ``Settings.delete_watched`` on, a watched episode is not downloaded, and its torrent is deleted with its files.
Each show keeps the progress (``Show.watched``) and up to which episode it was acted on (``Show.watched_applied``), so
an episode is acted on once: one the user asks for again after it was watched is kept.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.clients.qbittorrent import Torrent
from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    Download,
    DownloadState,
    EndReason,
    Episode,
    EpisodeStatus,
    EventKind,
    Show,
)
from kisetsu.services import downloads, events, library
from kisetsu.services.context import Context

logger = logging.getLogger("kisetsu.services.watched")

# Watched episodes in these states that hold no torrent are skipped.
SKIPPABLE = (EpisodeStatus.WANTED, EpisodeStatus.MISSED, EpisodeStatus.FAILED)
# A watched episode's finished torrent waits for qBittorrent to stop seeding it (its seed ratio), but no longer than this
# after it finished: a release few people download may never reach the ratio.
SEED_MAX = timedelta(days=7)
_STOPPED = ("paused", "stopped")
_GONE_STATES = (DownloadState.SUPERSEDED, DownloadState.CANCELED)


def is_watched(ctx: Context, show: Show, number: int) -> bool:
    """Whether the episode is one to leave alone because the user watched it; always False with the setting off."""
    return ctx.settings.delete_watched and show.watched is not None and number <= show.watched


def _holds_nothing(ctx: Context, episode: Episode) -> bool:
    if downloads.active_download(ctx, episode) is not None:
        return False
    cur = downloads.current(ctx, episode)
    return cur is None or cur.info_hash is None


def skip_watched(ctx: Context, show: Show, after: int = 0, episodes: Optional[Iterable[Episode]] = None) -> int:
    """Skip the watched episodes past ``after`` that hold no torrent, so nothing downloads or searches for them.

    Returns how many were skipped. A torrent is left to ``apply``, which needs qBittorrent.
    """
    if not ctx.settings.delete_watched or not show.watched or show.watched <= after:
        return 0
    if episodes is None:
        episodes = ctx.session.exec(
            select(Episode).where(Episode.show_id == show.id, Episode.number > after, Episode.number <= show.watched)
        ).all()
    skipped = 0
    for episode in episodes:
        if after < episode.number <= show.watched and episode.status in SKIPPABLE and _holds_nothing(ctx, episode):
            episode.status, episode.last_error, episode.retry_after = EpisodeStatus.SKIPPED, None, None
            ctx.session.add(episode)
            skipped += 1
    if skipped:
        ctx.emit("episodes", f"show:{show.id}")
    return skipped


def note_progress(ctx: Context, show: Show, progress: int) -> bool:
    """Take in the progress a sync reported; returns whether the show changed.

    The first progress seen for a show is where it stands, not a watch. With deleting watched episodes off there is
    nothing to act on, so the progress counts as acted on at once. A lower one (a rewatch, a correction) lowers how far
    it was acted on too, so the episodes are acted on again when the progress passes them once more.
    """
    if show.watched == progress:
        return False
    if show.watched is None or show.watched_applied is None:
        show.watched = show.watched_applied = progress
        # Turned on before the progress was known: what was watched is not downloaded, and what is in qBittorrent stays.
        skip_watched(ctx, show)
        return True
    show.watched = progress
    if progress < show.watched_applied or not ctx.settings.delete_watched:
        show.watched_applied = progress
    else:
        # Before a grab could start them; their torrents wait for ``apply``.
        skip_watched(ctx, show, show.watched_applied)
    return True


def apply(ctx: Context) -> bool:
    """Act on what the user watched: mark the torrents of newly watched episodes, then delete those whose time has come.

    Runs after settle has synced with qBittorrent. Returns whether anything changed.
    """
    if not ctx.settings.delete_watched:
        return False
    changed = False
    due = ctx.session.exec(select(Show).where(Show.watched.is_not(None), Show.watched_applied < Show.watched)).all()
    for show in due:
        if _mark(ctx, show):
            show.watched_applied = show.watched
            ctx.session.add(show)
            changed = True
    if changed:
        ctx.checkpoint()
    return _sweep(ctx) or changed


def _mark(ctx: Context, show: Show) -> bool:
    """Mark the torrents of the episodes watched since the last pass; False while an add qBittorrent has not shown yet holds it up."""
    after = show.watched_applied or 0
    skip_watched(ctx, show, after)
    episodes = ctx.session.exec(
        select(Episode).where(Episode.show_id == show.id, Episode.number > after, Episode.number <= show.watched)
    ).all()
    for episode in episodes:
        active = ctx.session.exec(
            select(Download).where(Download.episode_id == episode.id, Download.state.in_(ACTIVE_DOWNLOAD_STATES))
        ).all()
        for dl in active:
            if downloads.locate(ctx, dl) is None:
                if dl.state == DownloadState.ADDING:
                    return False
                # A retry still waiting never reached qBittorrent, so there is nothing to delete.
                downloads.end_watched(ctx, dl, episode)
                continue
            dl.end_reason = EndReason.WATCHED
            ctx.session.add(dl)
        cur = downloads.current(ctx, episode)
        if cur is not None and cur.info_hash and cur.state in (DownloadState.DONE, DownloadState.FAILED):
            cur.end_reason = EndReason.WATCHED
            ctx.session.add(cur)
    return True


def _holders(ctx: Context, info_hash: str) -> List[Tuple[Download, Episode]]:
    """Every episode's release in this torrent: what is downloading from it, and what an episode has from it now."""
    found = []
    rows = ctx.session.exec(
        select(Download).where(Download.info_hash == info_hash, Download.state.not_in(_GONE_STATES))
    ).all()
    for dl in rows:
        episode = ctx.session.get(Episode, dl.episode_id)
        if episode is not None and (dl.state != DownloadState.DONE or episode.current_download_id == dl.id):
            found.append((dl, episode))
    return found


def _still_seeding(torrent: Torrent, held: List[Tuple[Download, Episode]], now: datetime) -> bool:
    state = torrent.state.lower()
    if torrent.errored or state.startswith(_STOPPED):
        return False
    finished = torrent.completion_on or max((dl.completed_at for dl, _ in held if dl.completed_at), default=None)
    return finished is not None and now - finished < SEED_MAX


def _sweep(ctx: Context) -> bool:
    """Delete the torrents every episode of which was watched, once a finished one is done seeding."""
    marked = ctx.session.exec(
        select(Download.info_hash).where(
            Download.end_reason == EndReason.WATCHED, Download.info_hash.is_not(None), Download.state.not_in(_GONE_STATES),
        )
    ).all()
    due: Dict[str, List[Tuple[Download, Episode]]] = {}
    for info_hash in set(marked):
        torrent = ctx.qbit.torrents.get(info_hash)
        # A torrent already gone is settled by ``downloads.settle``, as one removed in qBittorrent.
        if torrent is None or torrent.checking:
            continue
        held = _holders(ctx, info_hash)
        # A batch waits for its last episode to be watched.
        if not held or any(dl.end_reason != EndReason.WATCHED for dl, _ in held):
            continue
        if torrent.finished and _still_seeding(torrent, held, ctx.now):
            continue
        due[info_hash] = held
    if not due:
        return False
    _delete(ctx, list(due))
    ctx.qbit.sync()
    gone = [info_hash for info_hash in due if info_hash not in ctx.qbit.torrents]
    told = set()
    names: Dict[int, List[int]] = {}
    ended = [pair for info_hash in gone for pair in due[info_hash]]
    # By show and episode, the last first as History lists the newest on top; what an episode has now comes before a
    # replacement, so its event names that release.
    ended.sort(key=lambda pair: (pair[1].show_id, -pair[1].number, pair[1].current_download_id != pair[0].id))
    for dl, episode in ended:
        if episode.id not in told:
            told.add(episode.id)
            show = ctx.session.get(Show, episode.show_id)
            events.record(ctx, EventKind.DELETED, "Watched, so deleted", show=show, episode=episode.number, feed_id=dl.feed_id, title=dl.title)
            names.setdefault(episode.show_id, []).append(episode.number)
        downloads.end_watched(ctx, dl, episode)
    for show_id, numbers in names.items():
        show = ctx.session.get(Show, show_id)
        if show is not None:
            listed = ", ".join(str(n) for n in sorted(numbers))
            logger.info(f"'{show.title(ctx.settings.title_language)}' Ep {listed}: watched, so deleted from qBittorrent with the files.")
    if gone:
        ctx.emit("shows", "history", *(f"show:{show_id}" for show_id in names))
    return bool(gone)


def _delete(ctx: Context, hashes: List[str]) -> None:
    """Delete the torrents with their files, except a torrent that has a file in common with one that stays: it keeps them.

    Only torrents whose content paths nest can have files in common (a batch put straight in a show's folder holds that
    folder, as the singles next to it do), so only theirs are listed and compared.
    """
    torrents = ctx.qbit.torrents
    going = set(hashes)
    listed: Dict[str, Set[str]] = {}

    def files(info_hash: str) -> Set[str]:
        if info_hash not in listed:
            folder = torrents[info_hash].save_path
            listed[info_hash] = {library._norm(os.path.join(folder, name)) for name in ctx.qbit.files(info_hash)}
        return listed[info_hash]

    def nest(a: Torrent, b: Torrent) -> bool:
        return bool(a.content_path and b.content_path) and (
            library._within(a.content_path, b.content_path) or library._within(b.content_path, a.content_path)
        )

    shared = [
        h for h in hashes
        if any(nest(torrents[h], t) and files(h) & files(other) for other, t in torrents.items() if other not in going)
    ]
    ctx.qbit.pause(hashes)
    for chosen, delete_files in (([h for h in hashes if h not in shared], True), (shared, False)):
        if chosen:
            ctx.qbit.delete(chosen, delete_files=delete_files)
