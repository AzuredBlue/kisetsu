"""Adding torrents for episodes and following them in qBittorrent until they are done."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from uuid import uuid4

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError, QbitUnavailable, Torrent
from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    FIXED_FEED_STATES,
    Download,
    DownloadState,
    Episode,
    EpisodeStatus,
    EventKind,
    Feed,
    FeedItem,
    Show,
)
from kisetsu.domain.parsing import same_release
from kisetsu.domain.patterns import resolve_save_path
from kisetsu.services import events, feeds
from kisetsu.services.context import Context
from kisetsu.services.feeds import Evidence
from kisetsu.services.library import _same_dir

logger = logging.getLogger("kisetsu.services.downloads")

MANAGED_TAG = "kisetsu-managed"
TAG_PREFIX = "kisetsu-op-"
_OPERATION_TAG = re.compile(r"^kisetsu-op-[0-9a-f]{32}$")

MAX_ATTEMPTS = 8
# A plain grab that gave up is tried again after these many hours (its 1st, 2nd and 3rd give-up); the next one fails it.
GIVE_UP_RETRY_HOURS = (2, 6, 12)
GAVE_UP_PREFIX = "Gave up after"
# A replacement that has not finished by then is dropped and the previous release kept.
REPLACEMENT_GIVE_UP = timedelta(hours=48)
# An add that qBittorrent never showed is treated as lost after this long.
ADDING_STUCK = timedelta(minutes=15)
AMBIGUOUS_RETRY = timedelta(minutes=15)
ADD_RETRY = timedelta(minutes=1)
ERROR_RETRY = timedelta(minutes=15)
# A download that was only just added or is being verified is watched closely for this long.
FAST_WINDOW = timedelta(minutes=5)
# A torrent missing from qBittorrent is forgotten after this many consecutive passes with a list of the same size.
ABSENT_PASSES = 2
# Several missing torrents with nothing listed means qBittorrent is still loading.
LOADING_GUESS = 3

PRESENT_STATES = (DownloadState.VERIFYING, DownloadState.DOWNLOADING)


@dataclass
class Settled:
    """What a settle pass found: whether to look again soon, and when the next retry is due."""

    busy: bool = False
    active: bool = False
    next_due: Optional[datetime] = None
    changed: bool = False


def _tag() -> str:
    return f"{TAG_PREFIX}{uuid4().hex}"


def current(ctx: Context, episode: Episode) -> Optional[Download]:
    return ctx.session.get(Download, episode.current_download_id) if episode.current_download_id else None


def active_download(ctx: Context, episode: Episode) -> Optional[Download]:
    return ctx.session.exec(
        select(Download)
        .where(Download.episode_id == episode.id, Download.state.in_(ACTIVE_DOWNLOAD_STATES))
        .order_by(Download.created_at.desc(), Download.id.desc())
    ).first()


def recompute_episode(ctx: Context, episode: Episode) -> None:
    """Set the episode's status from the release it currently has."""
    cur = current(ctx, episode)
    if cur is None:
        if episode.status not in (EpisodeStatus.WANTED, EpisodeStatus.MISSED):
            episode.status = EpisodeStatus.WANTED
        episode.attempts = 0
    elif cur.state in (DownloadState.ADDING, *PRESENT_STATES):
        episode.status, episode.last_error, episode.retry_after = EpisodeStatus.DOWNLOADING, None, None
    elif cur.state == DownloadState.DONE:
        episode.status, episode.last_error, episode.retry_after = EpisodeStatus.COMPLETED, None, None
    elif cur.state == DownloadState.FAILED:
        episode.status, episode.last_error, episode.retry_after = EpisodeStatus.FAILED, cur.last_error, None
    else:
        episode.status, episode.last_error, episode.retry_after = EpisodeStatus.WANTED, cur.last_error, cur.next_retry_at
    if cur is not None:
        episode.attempts = cur.attempts
    ctx.session.add(episode)
    ctx.emit("shows", f"show:{episode.show_id}")


def failed_versions(ctx: Context, episode: Episode) -> set:
    """Versions of this episode that failed for good; they are not offered again."""
    return set(
        ctx.session.exec(
            select(Download.version).where(Download.episode_id == episode.id, Download.state == DownloadState.FAILED)
        ).all()
    )


def locate(ctx: Context, dl: Download) -> Optional[Torrent]:
    """The torrent qBittorrent holds for a download: by info hash, else by its operation tag."""
    view = ctx.qbit.torrents
    torrent = view.get(dl.info_hash) if dl.info_hash else None
    if torrent is None and dl.tag:
        torrent = ctx.qbit.find_by_tag(dl.tag)
        if torrent is not None:
            dl.info_hash = torrent.hash
    return torrent


def _by_name(ctx: Context, title: str) -> Optional[Torrent]:
    """The one torrent holding this release name, whatever its tag or category."""
    matches = [t for t in ctx.qbit.torrents.values() if same_release(t.name, title)]
    return matches[0] if len(matches) == 1 else None


def start_download(
    ctx: Context,
    show: Show,
    feed: Feed,
    episode: Episode,
    item: FeedItem,
    *,
    switch_source: bool = False,
) -> Optional[Download]:
    """Add the torrent of a feed item for an episode.

    An episode that already has a release gets a replacement: the new torrent is added
    paused and the old release stays until it has finished. Returns None when there is
    nothing to start.
    """
    if active_download(ctx, episode) is not None or not item.torrent_url:
        return None
    if not ctx.qbit.synced:
        ctx.qbit.sync()
    cur = current(ctx, episode)
    supersedes: Optional[Download] = None
    old_present = False
    if cur is not None and cur.state in (DownloadState.VERIFYING, DownloadState.DOWNLOADING, DownloadState.DONE, DownloadState.FAILED):
        old_torrent = ctx.qbit.torrents.get(cur.info_hash) if cur.info_hash else None
        old_present = old_torrent is not None
        if cur.state == DownloadState.DONE and not cur.info_hash:
            # A finished episode whose torrent Kisetsu lost track of: the new add restores it and keeps it completed meanwhile.
            supersedes = cur
        elif item.version > cur.version or switch_source:
            if old_present or cur.state == DownloadState.DONE:
                supersedes = cur
            elif old_torrent is None and cur.info_hash is None and cur.title and _by_name(ctx, cur.title) is not None:
                supersedes, old_present = cur, True
                cur.info_hash = _by_name(ctx, cur.title).hash
    dl = Download(
        episode_id=episode.id,
        feed_id=feed.id,
        feed_item_guid=item.guid,
        title=item.title,
        parsed_title=item.parsed_title,
        release_group=item.release_group,
        version=item.version,
        source_episode=item.episode_raw,
        torrent_url=item.torrent_url,
        info_hash=item.info_hash,
        tag=_tag(),
        state=DownloadState.ADDING,
        supersedes_id=supersedes.id if supersedes else None,
        attempts=0,
        created_at=ctx.now,
        updated_at=ctx.now,
    )
    ctx.session.add(dl)
    ctx.session.flush()
    if supersedes is None:
        if cur is not None and cur.state != DownloadState.DONE:
            cur.state = DownloadState.SUPERSEDED
            ctx.session.add(cur)
        episode.current_download_id = dl.id
    _attempt_add(ctx, show, episode, dl, paused=supersedes is not None and old_present)
    events.record(
        ctx, EventKind.GRABBED, f"Added {item.title}", show=show, episode=episode.number, feed_id=feed.id, title=item.title,
    )
    return dl


def _retry(ctx: Context, dl: Download, episode: Episode, error: str, delay: timedelta) -> None:
    dl.state = DownloadState.RETRY_WAIT
    dl.last_error = error
    dl.next_retry_at = ctx.now + delay
    dl.updated_at = ctx.now
    ctx.session.add(dl)
    recompute_episode(ctx, episode)


def _attempt_add(ctx: Context, show: Show, episode: Episode, dl: Download, *, paused: bool) -> None:
    settings = ctx.settings
    category = show.category or settings.default_category
    ratio = show.seed_ratio if show.seed_ratio is not None else settings.default_seed_ratio
    try:
        if category:
            ctx.qbit.ensure_category(category)
    except QbitError as e:
        _retry(ctx, dl, episode, f"Could not create the qBittorrent category: {e}", ADD_RETRY)
        return
    dl.attempts += 1
    dl.next_retry_at = None
    dl.last_error = None
    dl.updated_at = ctx.now
    save_path = resolve_save_path(settings.base_dir, show.title(settings.title_language), show.save_folder)
    ctx.checkpoint()
    try:
        ctx.qbit.add_torrent(
            dl.torrent_url, save_path=save_path, category=category, tags=[MANAGED_TAG, dl.tag],
            paused=paused, ratio_limit=ratio,
        )
    except QbitUnavailable as e:
        _retry(ctx, dl, episode, str(e), ADD_RETRY)
        return
    except QbitError as e:
        # A refused add and a duplicate look alike: the release may already be in qBittorrent.
        existing = _existing(ctx, dl)
        if existing is None:
            _retry(ctx, dl, episode, str(e), AMBIGUOUS_RETRY)
            return
        try:
            ctx.qbit.add_tags([existing.hash], [MANAGED_TAG, dl.tag])
        except QbitError as tag_error:
            _retry(ctx, dl, episode, str(tag_error), AMBIGUOUS_RETRY)
            return
        dl.info_hash = existing.hash
        paused = False
    # qBittorrent accepted the release, so its feed is proven for this show.
    if dl.feed_id is not None:
        feed = ctx.session.get(Feed, dl.feed_id)
        if feed is not None:
            feeds.lock(ctx, show, feed, Evidence.GRAB, learned_name=dl.parsed_title or dl.title, learned_group=dl.release_group)
    dl.state = DownloadState.ADDING
    ctx.session.add(dl)
    ctx.checkpoint()
    try:
        ctx.qbit.sync()
    except QbitError:
        dl.last_error = "Waiting for qBittorrent to show the new torrent."
        recompute_episode(ctx, episode)
        return
    torrent = locate(ctx, dl)
    if torrent is None:
        dl.last_error = "Waiting for qBittorrent to show the new torrent."
        recompute_episode(ctx, episode)
        return
    _exposed(ctx, dl, episode, torrent)


def _existing(ctx: Context, dl: Download) -> Optional[Torrent]:
    """The torrent qBittorrent already holds for a download whose add was refused."""
    try:
        ctx.qbit.sync()
    except QbitError:
        return None
    if dl.info_hash and dl.info_hash in ctx.qbit.torrents:
        return ctx.qbit.torrents[dl.info_hash]
    return _by_name(ctx, dl.title)


def _release_tag(ctx: Context, dl: Download) -> None:
    """Drop the temporary tag once the torrent is known by its hash."""
    if not dl.tag or not dl.info_hash:
        return
    try:
        ctx.qbit.remove_tags([dl.info_hash], [dl.tag])
        ctx.qbit.delete_tags([dl.tag])
    except QbitError:
        return
    dl.tag = None


def _check_folder(ctx: Context, show: Show, torrent: Torrent) -> None:
    """Say once per show when a torrent is not in the folder Kisetsu asked qBittorrent to use."""
    wanted = resolve_save_path(ctx.settings.base_dir, show.title(ctx.settings.title_language), show.save_folder)
    if not wanted or _same_dir(torrent.save_path, wanted) or show.id in ctx.caches.misplaced:
        return
    ctx.caches.misplaced.add(show.id)
    message = (
        f"qBittorrent has a torrent of this show in {torrent.save_path or 'its default folder'}, not in {wanted}. "
        "The folder must be one qBittorrent itself can see: with qBittorrent in Docker, use the path inside the container."
    )
    logger.warning(f"{show.title(ctx.settings.title_language)}: {message}")
    events.record(ctx, EventKind.INFO, message, show=show, title=torrent.name)


def _exposed(ctx: Context, dl: Download, episode: Episode, torrent: Torrent) -> None:
    """The torrent is in qBittorrent: a replacement is verified first, anything else just downloads."""
    dl.info_hash = torrent.hash
    dl.last_error = None
    _release_tag(ctx, dl)
    show = ctx.session.get(Show, episode.show_id)
    if show is not None and not dl.adopted:
        _check_folder(ctx, show, torrent)
    if show is not None and dl.feed_id is not None and not dl.adopted and show.feed_state not in FIXED_FEED_STATES:
        feed = ctx.session.get(Feed, dl.feed_id)
        if feed is not None:
            feeds.lock(ctx, show, feed, Evidence.GRAB, learned_name=dl.parsed_title or dl.title, learned_group=dl.release_group)
    if dl.supersedes_id is not None:
        # Added paused so the old release stays until this one is checked and has finished.
        try:
            ctx.qbit.recheck([torrent.hash])
            ctx.qbit.resume([torrent.hash])
        except QbitError as e:
            _retry(ctx, dl, episode, str(e), ADD_RETRY)
            return
        dl.state = DownloadState.VERIFYING
    else:
        dl.state = DownloadState.DOWNLOADING
    dl.updated_at = ctx.now
    ctx.session.add(dl)
    recompute_episode(ctx, episode)
    if torrent.finished and dl.supersedes_id is None:
        _complete(ctx, dl, episode)


def _complete(ctx: Context, dl: Download, episode: Episode) -> None:
    show = ctx.session.get(Show, episode.show_id)
    dl.state = DownloadState.DONE
    dl.completed_at = dl.completed_at or ctx.now
    dl.last_error = None
    dl.next_retry_at = None
    dl.updated_at = ctx.now
    ctx.session.add(dl)
    episode.current_download_id = dl.id
    recompute_episode(ctx, episode)
    if show is None:
        return
    if dl.supersedes_id is None:
        events.record(ctx, EventKind.DONE, "Downloaded", show=show, episode=episode.number, feed_id=dl.feed_id, title=dl.title)
        if ctx.settings.desktop_notifications and ctx.notify is not None:
            ctx.notify(f"{show.title(ctx.settings.title_language)} - Episode {episode.number}", "Download finished", show.cover_image)


def _fail(ctx: Context, dl: Download, episode: Episode, error: str) -> None:
    dl.state = DownloadState.FAILED
    if dl.supersedes_id is not None:
        # A failed replacement's torrent is gone or was deleted; nothing is left to follow.
        dl.info_hash = None
    dl.last_error = error
    dl.next_retry_at = None
    dl.updated_at = ctx.now
    ctx.session.add(dl)
    recompute_episode(ctx, episode)
    show = ctx.session.get(Show, episode.show_id)
    events.record(ctx, EventKind.FAILED, error, show=show, episode=episode.number, feed_id=dl.feed_id, title=dl.title)


def _cancel(ctx: Context, dl: Download, episode: Episode, reason: str) -> None:
    dl.state = DownloadState.CANCELED
    dl.last_error = reason
    dl.next_retry_at = None
    dl.updated_at = ctx.now
    ctx.session.add(dl)
    if episode.current_download_id == dl.id:
        episode.current_download_id = None
    recompute_episode(ctx, episode)


def _give_up_hours(ctx: Context, dl: Download) -> Optional[int]:
    if dl.supersedes_id is not None:
        return None
    earlier = ctx.session.exec(
        select(Download.id).where(
            Download.episode_id == dl.episode_id,
            Download.state == DownloadState.CANCELED,
            Download.last_error.like(f"{GAVE_UP_PREFIX}%"),
        )
    ).all()
    return GIVE_UP_RETRY_HOURS[len(earlier)] if len(earlier) < len(GIVE_UP_RETRY_HOURS) else None


def _give_up(ctx: Context, dl: Download, episode: Episode, show: Optional[Show]) -> None:
    error = f"{GAVE_UP_PREFIX} {dl.attempts} attempts: {dl.last_error or 'qBittorrent never showed the torrent.'}"
    hours = _give_up_hours(ctx, dl)
    if hours is None:
        _fail(ctx, dl, episode, error)
    else:
        _cancel(ctx, dl, episode, error)
        episode.retry_after = ctx.now + timedelta(hours=hours)
        ctx.session.add(episode)
    if show is not None and dl.supersedes_id is None:
        feeds.unwind_unproven(ctx, show, dl.feed_id)


def _delete(ctx: Context, info_hash: Optional[str], keep_files_of: Optional[str] = None) -> None:
    """Delete a torrent with its files, unless the release named by ``keep_files_of`` uses the same files."""
    if not info_hash:
        return
    torrent = ctx.qbit.torrents.get(info_hash)
    if torrent is None:
        return
    protected = ctx.qbit.torrents.get(keep_files_of) if keep_files_of and keep_files_of != info_hash else None
    shares = protected is not None and bool(torrent.content_path) and torrent.content_path == protected.content_path
    ctx.qbit.pause([info_hash])
    ctx.qbit.delete([info_hash], delete_files=not shares)


def cancel_download(ctx: Context, dl: Download, reason: str) -> bool:
    """Stop a download and delete the torrent it added; a replacement leaves the previous release as it is."""
    episode = ctx.session.get(Episode, dl.episode_id)
    if episode is None:
        return False
    old = ctx.session.get(Download, dl.supersedes_id) if dl.supersedes_id else None
    try:
        _delete(ctx, dl.info_hash, keep_files_of=old.info_hash if old else None)
    except QbitError as e:
        dl.last_error = f"{reason} Cleanup failed: {e}"
        ctx.session.add(dl)
        return False
    _cancel(ctx, dl, episode, reason)
    show = ctx.session.get(Show, episode.show_id)
    if show is not None:
        feeds.unwind_unproven(ctx, show, dl.feed_id)
    return True


def cancel_episode(ctx: Context, episode: Episode, reason: str, *, keep_added: bool = False) -> int:
    """Cancel the episode's unfinished downloads; with ``keep_added`` one already in qBittorrent finishes."""
    canceled = 0
    downloads = ctx.session.exec(
        select(Download).where(Download.episode_id == episode.id, Download.state.in_(ACTIVE_DOWNLOAD_STATES))
    ).all()
    for dl in downloads:
        if dl.supersedes_id is not None and dl.state == DownloadState.DOWNLOADING:
            continue
        if keep_added and locate(ctx, dl) is not None:
            continue
        if cancel_download(ctx, dl, reason):
            canceled += 1
    return canceled


def cancel_from_other_feeds(ctx: Context, show: Show, keep_feed_id: Optional[int], reason: str) -> List[int]:
    """After the user switched feeds: delete what is still downloading from another feed.

    A finished torrent is never touched, nor is a download whose feed is unknown. Returns the
    episode numbers that are wanted again.
    """
    reset: List[int] = []
    rows = ctx.session.exec(
        select(Download, Episode)
        .join(Episode, Episode.id == Download.episode_id)
        .where(
            Episode.show_id == show.id,
            Download.state.in_([*ACTIVE_DOWNLOAD_STATES, DownloadState.FAILED]),
            Download.feed_id.is_not(None),
            Download.feed_id != keep_feed_id,
        )
        .order_by(Episode.number)
    ).all()
    for dl, episode in rows:
        torrent = ctx.qbit.torrents.get(dl.info_hash) if dl.info_hash else locate(ctx, dl)
        if torrent is not None and torrent.finished:
            continue
        if cancel_download(ctx, dl, reason):
            reset.append(episode.number)
    return reset


def _missing(ctx: Context, info_hash: str) -> bool:
    """Whether a tracked torrent is really gone: missing on consecutive passes while the list did not grow."""
    if info_hash in ctx.qbit.torrents:
        ctx.caches.absent.pop(info_hash, None)
        return False
    count = ctx.caches.absent.get(info_hash, 0) + 1
    ctx.caches.absent[info_hash] = count
    return count >= ABSENT_PASSES


def settle(ctx: Context) -> Settled:
    """Bring downloads up to date with qBittorrent: expose, verify, finish, replace, retry and forget."""
    result = Settled()
    ctx.qbit.sync()
    listed = len(ctx.qbit.torrents)
    if ctx.caches.listed != listed:
        ctx.caches.absent.clear()
        ctx.caches.listed = listed
    tracked = ctx.session.exec(
        select(Download).where(
            (Download.state.in_([*ACTIVE_DOWNLOAD_STATES, DownloadState.FAILED]))
            | ((Download.state == DownloadState.DONE) & Download.info_hash.is_not(None))
        )
    ).all()
    missing = [d for d in tracked if d.info_hash and d.info_hash not in ctx.qbit.torrents]
    loading = len(missing) >= LOADING_GUESS and listed == 0
    for dl in tracked:
        episode = ctx.session.get(Episode, dl.episode_id)
        if episode is None:
            continue
        before = (dl.state, dl.info_hash)
        _advance(ctx, dl, episode, result, loading)
        if (dl.state, dl.info_hash) != before:
            result.changed = True
        if dl.state in ACTIVE_DOWNLOAD_STATES:
            result.active = True
        if (dl.state in (DownloadState.ADDING, DownloadState.VERIFYING) and ctx.now - dl.updated_at < FAST_WINDOW) or (
            dl.info_hash and dl.info_hash in ctx.caches.absent
        ):
            result.busy = True
        ctx.checkpoint()
        if dl.next_retry_at is not None and dl.state in ACTIVE_DOWNLOAD_STATES:
            if result.next_due is None or dl.next_retry_at < result.next_due:
                result.next_due = dl.next_retry_at
    if result.changed:
        ctx.emit("shows", "history")
    return result


def _advance(ctx: Context, dl: Download, episode: Episode, result: Settled, loading: bool) -> None:
    if dl.state in (DownloadState.SUPERSEDED, DownloadState.CANCELED):
        return
    show = ctx.session.get(Show, episode.show_id)
    suspended = show is not None and (show.paused or show.completed)
    if dl.state in (DownloadState.ADDING, DownloadState.RETRY_WAIT):
        torrent = locate(ctx, dl)
        if torrent is not None:
            _exposed(ctx, dl, episode, torrent)
            return
        if suspended:
            return
        if dl.state == DownloadState.ADDING:
            if dl.updated_at + ADDING_STUCK <= ctx.now:
                _retry(ctx, dl, episode, "qBittorrent has not shown the torrent; trying again.", timedelta(0))
            return
        if dl.next_retry_at is not None and dl.next_retry_at <= ctx.now and show is not None:
            if dl.attempts >= MAX_ATTEMPTS:
                _give_up(ctx, dl, episode, show)
            else:
                _attempt_add(ctx, show, episode, dl, paused=_adds_paused(ctx, dl))
        return

    if dl.info_hash is None:
        return
    torrent = ctx.qbit.torrents.get(dl.info_hash)
    if torrent is None:
        if loading or not _missing(ctx, dl.info_hash):
            return
        _forget(ctx, dl, episode, show)
        return
    ctx.caches.absent.pop(dl.info_hash, None)

    if dl.state == DownloadState.DONE:
        return
    if dl.supersedes_id is not None:
        _advance_replacement(ctx, dl, episode, torrent)
        return
    if torrent.finished:
        _complete(ctx, dl, episode)
    elif torrent.errored:
        if dl.state != DownloadState.FAILED or dl.last_error != f"qBittorrent torrent state: {torrent.state}":
            dl.state = DownloadState.FAILED
            dl.last_error = f"qBittorrent torrent state: {torrent.state}"
            ctx.session.add(dl)
            recompute_episode(ctx, episode)
    elif dl.state == DownloadState.FAILED and not torrent.checking and torrent.progress > 0:
        dl.state = DownloadState.DOWNLOADING
        dl.last_error = None
        ctx.session.add(dl)
        recompute_episode(ctx, episode)
    elif dl.state == DownloadState.VERIFYING and not torrent.checking:
        dl.state = DownloadState.DOWNLOADING
        ctx.session.add(dl)


def _adds_paused(ctx: Context, dl: Download) -> bool:
    old = ctx.session.get(Download, dl.supersedes_id) if dl.supersedes_id else None
    return old is not None and old.info_hash is not None and old.info_hash in ctx.qbit.torrents


def _forget(ctx: Context, dl: Download, episode: Episode, show: Optional[Show]) -> None:
    """A tracked torrent was removed in qBittorrent."""
    if dl.state == DownloadState.DONE:
        dl.info_hash = None
        ctx.session.add(dl)
        events.record(ctx, EventKind.INFO, "Torrent removed in qBittorrent, no longer managed", show=show, episode=episode.number, title=dl.title)
        return
    ctx.caches.absent.pop(dl.info_hash or "", None)
    if dl.supersedes_id is not None:
        _fail(ctx, dl, episode, "Replacement torrent is no longer present in qBittorrent; the previous release was kept.")
        return
    _cancel(ctx, dl, episode, "Torrent is no longer present in qBittorrent; queued for a safe retry.")
    episode.retry_after = ctx.now + ERROR_RETRY
    ctx.session.add(episode)
    if show is not None:
        feeds.unwind_unproven(ctx, show, dl.feed_id)
        events.record(ctx, EventKind.INFO, "Requeued after qBittorrent removal", show=show, episode=episode.number, title=dl.title)


def _advance_replacement(ctx: Context, dl: Download, episode: Episode, torrent: Torrent) -> None:
    if dl.next_retry_at is not None and dl.next_retry_at > ctx.now and not torrent.finished:
        return
    old = ctx.session.get(Download, dl.supersedes_id)
    if not torrent.finished and ctx.now - dl.created_at > REPLACEMENT_GIVE_UP:
        try:
            _delete(ctx, dl.info_hash, keep_files_of=old.info_hash if old else None)
        except QbitError:
            pass
        _fail(ctx, dl, episode, "Replacement did not finish in time and was removed; the previous release was kept.")
        return
    if torrent.errored:
        dl.last_error = f"Replacement torrent state: {torrent.state}"
        dl.next_retry_at = ctx.now + ERROR_RETRY
        ctx.session.add(dl)
        return
    if torrent.checking or (torrent.progress <= 0 and not torrent.finished):
        return
    if not torrent.finished:
        dl.state = DownloadState.DOWNLOADING
        dl.last_error = None
        dl.next_retry_at = None
        ctx.session.add(dl)
        return
    if torrent.state.lower().startswith(("stopped", "paused")) and torrent.seeding_time == 0:
        # Complete but not running and never seeded: it has not been resumed, so the old release must stay.
        # One that already seeded was stopped by qBittorrent at its seed limit and counts as seeding.
        try:
            ctx.qbit.resume([torrent.hash])
        except QbitError as e:
            dl.last_error = str(e)
            ctx.session.add(dl)
        return
    try:
        if old is not None and old.info_hash and old.info_hash != dl.info_hash:
            _delete(ctx, old.info_hash, keep_files_of=dl.info_hash)
    except QbitError as e:
        dl.last_error = str(e)
        dl.next_retry_at = ctx.now + timedelta(minutes=5)
        ctx.session.add(dl)
        return
    if old is not None:
        old.state = DownloadState.SUPERSEDED
        old.updated_at = ctx.now
        ctx.session.add(old)
    _complete(ctx, dl, episode)
    show = ctx.session.get(Show, episode.show_id)
    events.record(ctx, EventKind.REPLACED, f"Replaced by {dl.title}", show=show, episode=episode.number, feed_id=dl.feed_id, title=dl.title)


def sweep_tags(ctx: Context) -> int:
    """Delete operation tags that no pending add is waiting for; returns how many."""
    waiting = set(
        ctx.session.exec(
            select(Download.tag).where(Download.tag.is_not(None), Download.state.in_(ACTIVE_DOWNLOAD_STATES))
        ).all()
    )
    stale = [t for t in ctx.qbit.tags() if _OPERATION_TAG.match(t) and t not in waiting]
    if stale:
        ctx.qbit.delete_tags(stale)
    return len(stale)
