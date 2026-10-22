import base64
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple
from uuid import uuid4

from sqlmodel import Session, select

from kisetsu.clients.qbit import QBitClient, QbitClientError
from kisetsu.config import DEFAULT_EARLY_AIR_TOLERANCE_HOURS, HUNTING_RECENT_DAYS, MISSED_AFTER_HOURS
from kisetsu.core.confirmation import record_match_event
from kisetsu.core.discovery import RssSnapshot, flatten_rss_articles, parse_article_date
from kisetsu.core.notify import notify_episode
from kisetsu.core.matching import _alias_arc_requirement, match_release_to_show, prepare_aliases, release_arc_qualifier, release_season
from kisetsu.core import library
from kisetsu.core.feedcache import CachedRssSnapshot, cached_articles, first_seen_map, item_id_of, last_failed_feed_names, store_matches
from kisetsu.core.rules import (
    effective_title,
    resolve_save_path,
    show_match_patterns,
)
from kisetsu.db.models import (
    ACTIVE_OPERATION_STATUSES,
    CANCELLABLE_OPERATION_STATUSES,
    Episode,
    EpisodeMappingSource,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    Monitored,
    MonitoredStatus,
    Settings,
    TorrentOperation,
    TorrentOperationStatus,
    as_utc,
    normalize_mapping_source,
    utc_now,
)

logger = logging.getLogger("kisetsu.core.grabber")

AIR_DATE_TOLERANCE = timedelta(days=3)
# How long a release must stay on a lower-ranked feed before a show with no feed
# assignment adopts it, so a higher-ranked feed gets a chance to post first.
# Matches the grace windows used by rules-mode discovery and feed switching.
FEED_DISCOVERY_GRACE_SECONDS = 300
_FAILED_TORRENT_STATES = {"error", "missingfiles", "unknown"}
MAX_OPERATION_ATTEMPTS = 8
# A grab whose torrent never appeared (the site or the network was down) is tried again later
# instead of failing for good: the hours to wait after its 1st, 2nd and 3rd give-up. The next one fails it.
GIVE_UP_RETRY_HOURS = (2, 6, 12)
GAVE_UP_PREFIX = "Gave up after"
# How long a replacement may take to finish before the previous release is kept instead.
REPLACEMENT_GIVE_UP = timedelta(hours=48)
_INACTIVE_SHOW_STATUSES = (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED)


MANAGED_TAG = "kisetsu-managed"
OPERATION_TAG_PREFIX = "kisetsu-op-"


def _operation_tag() -> str:
    return f"{OPERATION_TAG_PREFIX}{uuid4().hex}"


def _episode_tags(operation_tag: str) -> List[str]:
    # MANAGED_TAG finds our torrents; the operation tag only exists so the
    # torrent can be found again until its hash is known.
    return [MANAGED_TAG, operation_tag]


def _torrent_hash(torrent: Any) -> Optional[str]:
    value = getattr(torrent, "hash", None)
    return str(value) if value else None


def _torrent_progress(torrent: Any) -> float:
    try:
        return float(getattr(torrent, "progress", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _torrent_state(torrent: Any) -> str:
    return str(getattr(torrent, "state", "") or "").lower()


def is_seeding_torrent(torrent: Any) -> bool:
    return _torrent_progress(torrent) >= 1.0 or _torrent_state(torrent) in {
        "uploading",
        "pausedup",
        "stoppedup",
        "queuedup",
        "forcedup",
        "stalledup",
    }


def _managed_torrents(qbit: QBitClient, settings: Settings) -> List[Any]:
    by_hash: Dict[str, Any] = {}
    try:
        for torrent in qbit.get_torrents(tag=MANAGED_TAG):
            torrent_hash = _torrent_hash(torrent)
            if torrent_hash:
                by_hash[torrent_hash] = torrent
    except Exception as e:
        raise QbitClientError(f"Managed torrent lookup failed: {e}") from e
    if settings.default_category:
        try:
            for torrent in qbit.get_torrents(category=settings.default_category):
                torrent_hash = _torrent_hash(torrent)
                if torrent_hash:
                    by_hash.setdefault(torrent_hash, torrent)
        except Exception as e:
            raise QbitClientError(f"Category torrent lookup failed: {e}") from e
    return list(by_hash.values())


def _find_tagged(qbit: QBitClient, tag: str) -> Optional[Any]:
    try:
        torrents = list(qbit.get_torrents(tag=tag))
    except Exception as e:
        raise QbitClientError(f"Could not query tagged torrent {tag}: {e}") from e
    return torrents[0] if torrents else None


def _release_operation_tag(qbit: QBitClient, torrent_hash: Optional[str], operation_tag: Optional[str]) -> None:
    """Drop the per-operation tag once the torrent has been identified by hash.

    The tag exists only because ``add_torrent`` does not return a hash, so the
    newly added torrent has to be findable by something. Once the hash is known
    that lookup is never needed again, and the tag is a unique random string per
    grab, so leaving it on the torrent just accumulates unreadable clutter in the
    user's qBittorrent. The other tags are stable and stay.
    """
    if not torrent_hash or not operation_tag:
        return
    try:
        qbit.remove_torrent_tags([torrent_hash], [operation_tag])
        # Taking the tag off the torrent leaves it in qBittorrent's tag list, so
        # delete it as well. It is unique to this operation.
        qbit.delete_tags([operation_tag])
    except QbitClientError as e:
        # The tag is cosmetic from here on, so a failure must not disturb the
        # download or the operation it belongs to.
        logger.debug(f"Could not remove operation tag {operation_tag}: {e}")


def _operation_for_episode(session: Session, episode_id: int) -> Optional[TorrentOperation]:
    stmt = select(TorrentOperation).where(
        TorrentOperation.episode_id == episode_id,
        TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
    )
    return session.exec(stmt.order_by(TorrentOperation.created_at.desc())).first()


def _set_episode_release(session: Session, episode: Episode, operation: TorrentOperation, torrent_hash: Optional[str], status: EpisodeStatus) -> None:
    episode.status = status
    episode.version = operation.version
    # A replacement from another feed makes that feed the episode's source; the
    # rollback restores ``old_feed_id`` if the replace fails.
    if operation.feed_id is not None:
        episode.feed_id = operation.feed_id
    episode.release_title = operation.release_title
    episode.release_group = operation.release_group
    episode.torrent_url = operation.new_torrent_url
    episode.operation_tag = operation.operation_tag
    episode.torrent_hash = torrent_hash
    episode.downloaded_at = None
    episode.last_error = None
    episode.retry_after = None
    session.add(episode)


def _complete_episode(session: Session, episode: Episode, reason: Optional[str] = None) -> None:
    episode.status = EpisodeStatus.COMPLETED
    episode.downloaded_at = episode.downloaded_at or utc_now()
    episode.last_error = reason
    episode.retry_after = None
    session.add(episode)
    show = session.get(Monitored, episode.monitored_id)
    if show:
        show.last_confirmed_episode = max(show.last_confirmed_episode or 0, episode.episode_number)
        session.add(show)


def _is_restore_operation(operation: TorrentOperation) -> bool:
    """A grab of an episode that was already COMPLETED, to bring its torrent under management."""
    return (
        operation.kind != "replace"
        and str(operation.old_episode_status or "").strip().lower() == EpisodeStatus.COMPLETED.value
    )


def _fail_operation(session: Session, operation: TorrentOperation, episode: Episode, error: str) -> None:
    operation.status = TorrentOperationStatus.FAILED
    operation.last_error = error
    operation.updated_at = utc_now()
    if operation.kind == "replace" or _is_restore_operation(operation):
        # The previous release is still intact, so the episode keeps it rather
        # than being reported as failed.
        _restore_episode_before_operation(session, operation, episode, error, None)
    else:
        episode.status = EpisodeStatus.FAILED
        episode.last_error = error
    episode.retry_after = None
    session.add(operation)
    session.add(episode)
    session.commit()


def _is_plain_grab(operation: TorrentOperation) -> bool:
    """A grab of an episode that had no release yet (neither a replacement nor a restore)."""
    return operation.kind != "replace" and not _is_restore_operation(operation)


def _give_up_retry_hours(session: Session, operation: TorrentOperation) -> Optional[int]:
    """Hours to wait before the episode of a grab that gave up is tried again, or None when it should fail.

    Only a plain grab qualifies: a replacement or restore keeps the release it started
    from, so failing it costs nothing and is what it did before.
    """
    if not _is_plain_grab(operation):
        return None
    earlier = session.exec(
        select(TorrentOperation.id).where(
            TorrentOperation.episode_id == operation.episode_id,
            TorrentOperation.status == TorrentOperationStatus.CANCELED,
            TorrentOperation.last_error.like(f"{GAVE_UP_PREFIX}%"),
        )
    ).all()
    return GIVE_UP_RETRY_HOURS[len(earlier)] if len(earlier) < len(GIVE_UP_RETRY_HOURS) else None


def _expire_operation(
    session: Session,
    operation: TorrentOperation,
    episode: Episode,
    show: Optional[Monitored],
    error: str,
    hours: int,
) -> datetime:
    """Stand a grab down and let the grabber try the episode again after ``hours``.

    The operation ends CANCELED, which does not mark its version as failed, and the
    feed it taught is released when nothing it delivered is left; both are what a
    failed operation would have blocked.
    """
    retry_at = utc_now() + timedelta(hours=hours)
    operation.status = TorrentOperationStatus.CANCELED
    operation.last_error = error
    operation.next_retry_at = None
    operation.updated_at = utc_now()
    _restore_episode_before_operation(session, operation, episode, error, retry_at)
    session.add(operation)
    session.add(episode)
    session.commit()
    if show is not None:
        _unwind_unproven_feed(session, show, operation.feed_id)
    return retry_at


def _sync_seen_items(
    session: Session,
    feeds: List[Feed],
    articles_by_url: Dict[str, List[Dict[str, Any]]],
) -> Dict[Tuple[str, str], datetime]:
    """When each item was first seen, keeping the ones that match a followed show."""
    store_matches(session, {feed.qbit_feed_url: articles_by_url.get(feed.qbit_feed_url, []) for feed in feeds})
    keys = [
        (feed.qbit_feed_url, item_id_of(article))
        for feed in feeds
        for article in articles_by_url.get(feed.qbit_feed_url, [])
        if article.get("title")
    ]
    known = first_seen_map(session, keys)
    now = utc_now()
    return {key: known.get(key, now) for key in keys}


def sync_show_episodes(session: Session, show: Monitored) -> List[Episode]:
    if not show.id:
        return []
    episodes = session.exec(select(Episode).where(Episode.monitored_id == show.id)).all()
    by_number = {episode.episode_number: episode for episode in episodes}
    target_count = show.total_episodes or max(show.next_airing_episode or 0, show.last_confirmed_episode or 0, 1)
    if show.total_episodes:
        target_count = min(target_count, show.total_episodes)
    changed = False
    for number in range(1, target_count + 1):
        if number in by_number:
            continue
        episode = Episode(
            monitored_id=show.id,
            episode_number=number,
            status=EpisodeStatus.WANTED,
        )
        session.add(episode)
        by_number[number] = episode
        changed = True
    if changed:
        session.commit()
    return list(by_number.values())


def _find_old_hash(qbit: QBitClient, settings: Settings, show: Monitored, episode: Episode) -> Optional[str]:
    if episode.torrent_hash:
        return episode.torrent_hash
    if not episode.release_title:
        return None
    for torrent in _managed_torrents(qbit, settings):
        if same_release(str(getattr(torrent, "name", "") or ""), episode.release_title):
            return _torrent_hash(torrent)
    return None


def _is_healthy_torrent(torrent: Any) -> bool:
    if _torrent_state(torrent) in _FAILED_TORRENT_STATES:
        return False
    return _torrent_progress(torrent) > 0


def _operation_retry(
    session: Session,
    operation: TorrentOperation,
    episode: Episode,
    error: str,
    minutes: int = 5,
    progress: bool = False,
) -> None:
    """Check the operation again later; ``progress`` marks a wait that is not an error.

    A plain wait (the replacement is downloading) stays on the operation only, so
    the episode is not shown with an error while the previous release is intact.
    """
    operation.last_error = error
    operation.next_retry_at = utc_now() + timedelta(minutes=minutes)
    operation.updated_at = utc_now()
    episode.last_error = None if progress else error
    episode.retry_after = None if progress else operation.next_retry_at
    session.add(operation)
    session.add(episode)
    session.commit()


def _operation_torrent(qbit: QBitClient, operation: TorrentOperation) -> Optional[Any]:
    if not operation.new_torrent_hash:
        return None
    try:
        torrents = list(qbit.get_torrents(hashes=[operation.new_torrent_hash]))
    except Exception as e:
        raise QbitClientError(f"Could not read replacement torrent: {e}") from e
    return torrents[0] if torrents else None


def _content_path(torrent: Any) -> Optional[str]:
    value = getattr(torrent, "content_path", None)
    return str(value) if isinstance(value, str) and value else None


def _delete_torrent_by_hash(qbit: QBitClient, torrent_hash: Optional[str], keep_files_of: Optional[str] = None) -> None:
    """Delete a torrent with its files, unless ``keep_files_of`` still needs that path.

    A replacement published under the same filename shares ``content_path`` with
    the release it replaces, so deleting its files would delete that release.
    """
    if not torrent_hash:
        return
    try:
        existing = list(qbit.get_torrents(hashes=[torrent_hash]))
        protected = list(qbit.get_torrents(hashes=[keep_files_of])) if keep_files_of and keep_files_of != torrent_hash else []
    except Exception as e:
        raise QbitClientError(f"Could not read torrent {torrent_hash}: {e}") from e
    if not existing:
        return
    path = _content_path(existing[0])
    shares_files = bool(protected) and path is not None and path == _content_path(protected[0])
    qbit.pause_torrents([torrent_hash])
    qbit.delete_torrents([torrent_hash], delete_files=not shares_files)


def _remove_superseded_torrent(qbit: QBitClient, operation: TorrentOperation, new_torrent: Any) -> None:
    """Delete the release a finished replacement has superseded.

    The files go with it, unless the replacement occupies the same path (a
    re-release under the same filename, which is why the new torrent is
    rechecked first); deleting them then would delete the new download.
    """
    old_hash = operation.old_torrent_hash
    if not old_hash or old_hash == operation.new_torrent_hash:
        return
    try:
        existing = list(qbit.get_torrents(hashes=[old_hash]))
    except Exception as e:
        raise QbitClientError(f"Could not read torrent {old_hash}: {e}") from e
    if not existing:
        return
    if new_torrent is None:
        # Without the replacement's path there is no telling whether the files are shared.
        raise QbitClientError(f"Replacement torrent {operation.new_torrent_hash} is not listed in qBittorrent")
    old_path = _content_path(existing[0])
    shares_files = old_path is not None and old_path == _content_path(new_torrent)
    qbit.delete_torrents([old_hash], delete_files=not shares_files)


def _finish_operation(session: Session, qbit: QBitClient, operation: TorrentOperation, episode: Episode) -> Optional[str]:
    if operation.status == TorrentOperationStatus.COMPLETED or not operation.new_torrent_hash:
        return None

    if operation.kind == "replace" and operation.old_torrent_hash and operation.old_torrent_hash != operation.new_torrent_hash:
        if operation.status == TorrentOperationStatus.ADDED:
            try:
                qbit.recheck_torrents([operation.new_torrent_hash])
                qbit.resume_torrents([operation.new_torrent_hash])
            except Exception as e:
                _operation_retry(session, operation, episode, str(e))
                return None
            operation.status = TorrentOperationStatus.NEW_VERIFIED
            operation.next_retry_at = None
            operation.last_error = None
            operation.updated_at = utc_now()
            session.add(operation)
            session.commit()
            return None
        if operation.status in (TorrentOperationStatus.NEW_VERIFIED, TorrentOperationStatus.SEEDING):
            try:
                torrent = _operation_torrent(qbit, operation)
            except QbitClientError as e:
                _operation_retry(session, operation, episode, str(e))
                return None
            if not torrent:
                # The hash was already exposed once, so a torrent that is gone now
                # was removed, not delayed. Retrying would block the episode forever;
                # failing a replace restores the previous release.
                _fail_operation(session, operation, episode, "Replacement torrent is no longer present in qBittorrent; the previous release was kept.")
                return None
            if not is_seeding_torrent(torrent) and utc_now() - as_utc(operation.created_at) > REPLACEMENT_GIVE_UP:
                # A replacement that never finishes would hold the episode in
                # "replacing" forever. Drop it and go back to the previous release.
                try:
                    _delete_torrent_by_hash(qbit, operation.new_torrent_hash, keep_files_of=operation.old_torrent_hash)
                except Exception as e:
                    logger.warning(f"Could not remove the abandoned replacement {operation.new_torrent_hash}: {e}")
                _fail_operation(
                    session, operation, episode,
                    "Replacement did not finish in time and was removed; the previous release was kept.",
                )
                return None
            state = _torrent_state(torrent)
            if state in _FAILED_TORRENT_STATES:
                _operation_retry(session, operation, episode, f"Replacement torrent state: {state}", minutes=15)
                return None
            if not _is_healthy_torrent(torrent):
                _operation_retry(
                    session, operation, episode,
                    "Replacement is rechecking; the previous torrent remains untouched.",
                    minutes=1, progress=True,
                )
                return None
            if not is_seeding_torrent(torrent):
                operation.status = TorrentOperationStatus.SEEDING
                _operation_retry(
                    session, operation, episode,
                    "Replacement is downloading; the previous torrent is kept until it finishes.",
                    minutes=1, progress=True,
                )
                return None
            try:
                _remove_superseded_torrent(qbit, operation, torrent)
            except Exception as e:
                _operation_retry(session, operation, episode, str(e))
                return None
            # OLD_STOPPED is kept as the stored name for "superseded release
            # removed" so existing rows keep loading.
            operation.status = TorrentOperationStatus.OLD_STOPPED
            operation.next_retry_at = None
            operation.last_error = None
            operation.updated_at = utc_now()
            session.add(operation)
            session.commit()
        elif operation.status == TorrentOperationStatus.OLD_STOPPED:
            try:
                torrent = _operation_torrent(qbit, operation)
                if torrent is None and list(qbit.get_torrents(hashes=[operation.old_torrent_hash])):
                    # The previous release is still there and the replacement is gone for good.
                    _fail_operation(session, operation, episode, "Replacement torrent is no longer present in qBittorrent; the previous release was kept.")
                    return None
                _remove_superseded_torrent(qbit, operation, torrent)
            except Exception as e:
                _operation_retry(session, operation, episode, str(e))
                return None

    _set_episode_release(session, episode, operation, operation.new_torrent_hash, EpisodeStatus.DOWNLOADING)
    try:
        torrent = _operation_torrent(qbit, operation)
    except QbitClientError:
        torrent = None
    # From here the ledger names the release as qBittorrent does; the feed title
    # it was chosen by stays on the operation and in the match history.
    torrent_name = getattr(torrent, "name", None)
    if isinstance(torrent_name, str) and torrent_name.strip():
        episode.release_title = torrent_name.strip()
    if torrent is not None and is_seeding_torrent(torrent):
        _complete_episode(session, episode)
    operation.status = TorrentOperationStatus.COMPLETED
    operation.next_retry_at = None
    operation.last_error = None
    operation.updated_at = utc_now()
    session.add(operation)
    session.add(episode)
    session.commit()
    show = session.get(Monitored, episode.monitored_id)
    feed = session.get(Feed, operation.feed_id) if operation.feed_id else None
    if show:
        record_match_event(
            session=session,
            monitored_id=show.id,
            show_name=show.display_name,
            rule_name=f"Direct: {show.display_name}",
            release_title=operation.release_title,
            feed_name=feed.qbit_feed_name if feed else None,
            episode=episode.episode_number,
        )
        session.commit()
    return None


def show_name(episode: Episode, session: Session) -> str:
    show = session.get(Monitored, episode.monitored_id)
    return show.display_name if show else str(episode.monitored_id)


def show_cover(episode: Episode, session: Session) -> Optional[str]:
    show = session.get(Monitored, episode.monitored_id)
    return show.cover_image if show else None


def _normalize_hashed_operation(operation: TorrentOperation) -> None:
    """Treat a pre-add status that already has a torrent hash as ADDED.

    Only the pre-add states are normalised. Every later step is left to
    :func:`_finish_operation`, which advances on the torrent's real state, so a
    replacement is never pushed forward just because a cycle went by.
    """
    if operation.status == TorrentOperationStatus.PREPARING:
        operation.status = TorrentOperationStatus.ADDED
    elif operation.status == TorrentOperationStatus.RETRY_WAIT and not operation.ambiguous:
        operation.status = TorrentOperationStatus.ADDED
    elif operation.status == TorrentOperationStatus.UNKNOWN:
        operation.status = TorrentOperationStatus.ADDED


def show_torrent_hashes(session: Session, show: Monitored) -> List[str]:
    """Every torrent hash the ledger holds for a show, including in-flight replacements."""
    if not show.id:
        return []
    hashes = set(session.exec(
        select(Episode.torrent_hash).where(Episode.monitored_id == show.id, Episode.torrent_hash.is_not(None))
    ).all())
    for new_hash, old_hash in session.exec(
        select(TorrentOperation.new_torrent_hash, TorrentOperation.old_torrent_hash)
        .join(Episode, Episode.id == TorrentOperation.episode_id)
        .where(Episode.monitored_id == show.id, TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)))
    ).all():
        hashes.update(value for value in (new_hash, old_hash) if value)
    return sorted(hashes)


def _added_before_air(torrent: Any, reference: Optional[datetime], tolerance_hours: int) -> bool:
    """Whether qBittorrent added the torrent well before ``reference``, so it cannot be a release of what aired then.

    A torrent with no usable ``added_on``, or no reference time, cannot be judged and is not held back.
    """
    added_on = getattr(torrent, "added_on", None)
    if reference is None or not isinstance(added_on, (int, float)):
        return False
    try:
        added = datetime.fromtimestamp(added_on, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return added < reference - timedelta(hours=max(PUBLISHED_BEFORE_AIR_SLACK_HOURS, tolerance_hours))


def _named_like_show(torrent: Any, show: Monitored) -> bool:
    """Whether the torrent's name is one of this show's releases; a name that is not text cannot be judged."""
    name = getattr(torrent, "name", None)
    if not isinstance(name, str):
        return True
    return match_release_to_show(name, show.effective_aliases, ignore_arc_marker=False)[0]


def show_torrent_preview(session: Session, qbit: QBitClient, settings: Settings, show: Monitored) -> List[Dict[str, Any]]:
    """The torrents deleting a show removes: ``hash``, ``name``, ``size`` and whether the ledger ``tracked`` it.

    The ledger knows the torrents Kisetsu added. Torrents the ledger lost track of
    (a rule added them, or the row was reset) are found in Kisetsu's category under
    the show's own save folder, unless another show saves to the same folder. A
    series folder can also hold the torrents of its earlier seasons, so one that
    qBittorrent added before this show's first episode aired, or whose name is not
    this show's, is left alone.
    """
    tracked = show_torrent_hashes(session, show)
    entries: Dict[str, Dict[str, Any]] = {
        value: {"hash": value, "name": None, "size": None, "tracked": True} for value in tracked
    }

    def describe(torrent: Any, entry: Dict[str, Any]) -> None:
        name, size = getattr(torrent, "name", None), getattr(torrent, "size", None)
        entry["name"] = name if isinstance(name, str) else None
        entry["size"] = int(size) if isinstance(size, (int, float)) else None

    for start in range(0, len(tracked), 100):
        for torrent in qbit.get_torrents(hashes=tracked[start:start + 100]):
            entry = entries.get(_torrent_hash(torrent) or "")
            if entry is not None:
                describe(torrent, entry)

    save_path = resolve_save_path(settings.base_dir, effective_title(show, settings.title_language), show.save_folder)
    if save_path and settings.default_category:
        shared = any(
            resolve_save_path(settings.base_dir, effective_title(other, settings.title_language), other.save_folder) == save_path
            for other in session.exec(select(Monitored).where(Monitored.id != show.id)).all()
        )
        if not shared:
            air_times = session.exec(
                select(Episode.air_at).where(Episode.monitored_id == show.id, Episode.air_at.is_not(None))
            ).all()
            first_air = min((as_utc(value) for value in air_times), default=None)
            tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
            for torrent in qbit.get_torrents(category=settings.default_category):
                torrent_hash = _torrent_hash(torrent)
                if not torrent_hash or torrent_hash in entries:
                    continue
                if not _same_dir(getattr(torrent, "save_path", ""), save_path):
                    continue
                if _added_before_air(torrent, first_air, tolerance_hours) or not _named_like_show(torrent, show):
                    continue
                entry = {"hash": torrent_hash, "name": None, "size": None, "tracked": False}
                describe(torrent, entry)
                entries[torrent_hash] = entry
    return sorted(entries.values(), key=lambda entry: (not entry["tracked"], entry["name"] or entry["hash"]))


def _same_dir(a: Any, b: str) -> bool:
    """Whether two folder paths are one, however each writes its separators (qBittorrent reports "/" on Windows)."""
    def uniform(path: Any) -> str:
        return str(path or "").replace("\\", "/").rstrip("/")
    return uniform(a) == uniform(b)


def plan_language_move(session: Session, qbit: QBitClient, settings: Settings, new_language: str) -> List[Dict[str, Any]]:
    """The folder renames switching to ``new_language`` causes, per show with torrents to move.

    ``settings.title_language`` must still be the current language. Torrents are
    judged by where qBittorrent has them now, so one that already moved (an
    earlier switch, or one cut short and reversed) is left alone or moved back,
    and one the user relocated by hand is never touched. Each entry has ``show``,
    ``old``, ``new``, ``hashes`` and ``moving``, the torrents qBittorrent is
    still moving.
    """
    plan: List[Dict[str, Any]] = []
    for show in session.exec(select(Monitored)).all():
        old = resolve_save_path(settings.base_dir, effective_title(show, settings.title_language), show.save_folder)
        new = resolve_save_path(settings.base_dir, effective_title(show, new_language), show.save_folder)
        if not old or not new or _same_dir(old, new):
            continue
        hashes = [entry["hash"] for entry in show_torrent_preview(session, qbit, settings, show)]
        found: List[Any] = []
        for start in range(0, len(hashes), 100):
            found.extend(qbit.get_torrents(hashes=hashes[start:start + 100]))
        here = [torrent for torrent in found if _same_dir(getattr(torrent, "save_path", ""), old)]
        moving = [torrent for torrent in found if _torrent_state(torrent) == "moving"]
        if here or moving:
            plan.append({
                "show": show,
                "old": old,
                "new": new,
                "hashes": [h for h in (_torrent_hash(torrent) for torrent in here) if h],
                "moving": len(moving),
            })
    return plan


def relocate_torrents(qbit: QBitClient, plan: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Move what ``plan_language_move`` found; returns the log lines and the names of shows that failed."""
    logs: List[str] = []
    failed: List[str] = []
    for item in plan:
        if not item["hashes"]:
            continue
        try:
            qbit.set_torrent_location(item["hashes"], item["new"])
        except QbitClientError as e:
            failed.append(item["show"].display_name)
            logs.append(f"Could not move the downloads of {item['show'].display_name}: {e}")
            continue
        logs.append(f"Moved {len(item['hashes'])} downloads of {item['show'].display_name} to {item['new']}")
    return logs, failed


def find_show_torrents(session: Session, qbit: QBitClient, settings: Settings, show: Monitored) -> List[str]:
    """The hashes of every torrent a deleted show leaves in qBittorrent (see ``show_torrent_preview``)."""
    return sorted(entry["hash"] for entry in show_torrent_preview(session, qbit, settings, show))


def _recover_operations(session: Session, qbit: QBitClient, settings: Settings) -> List[str]:
    logs: List[str] = []
    operations = session.exec(
        select(TorrentOperation).where(
            TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
        )
    ).all()
    now = utc_now()
    for operation in operations:
        episode = session.get(Episode, operation.episode_id)
        if not episode:
            continue
        show = session.get(Monitored, episode.monitored_id)
        if show is not None and show.status in _INACTIVE_SHOW_STATUSES and not operation.new_torrent_hash:
            if not operation.last_error:
                operation.last_error = "Suspended: show is paused or completed."
                operation.next_retry_at = None
                episode.retry_after = None
                session.add(operation)
                session.add(episode)
                session.commit()
            continue
        if not operation.new_torrent_hash:
            torrent = _find_tagged(qbit, operation.operation_tag)
            if torrent:
                operation.new_torrent_hash = _torrent_hash(torrent)
                operation.status = TorrentOperationStatus.ADDED
                operation.next_retry_at = None
                operation.last_error = None
                operation.updated_at = utc_now()
                session.add(operation)
                session.commit()
                _release_operation_tag(qbit, operation.new_torrent_hash, operation.operation_tag)
            else:
                retry_at = as_utc(operation.next_retry_at)
                if (
                    operation.status in {TorrentOperationStatus.RETRY_WAIT, TorrentOperationStatus.UNKNOWN}
                    and retry_at and retry_at <= now
                ):
                    if operation.attempt_count >= MAX_OPERATION_ATTEMPTS:
                        error = (
                            f"{GAVE_UP_PREFIX} {operation.attempt_count} attempts: "
                            f"{operation.last_error or 'qBittorrent never exposed the tagged torrent.'}"
                        )
                        name = show.display_name if show else "show"
                        hours = _give_up_retry_hours(session, operation)
                        if hours is None:
                            _fail_operation(session, operation, episode, error)
                            if show is not None and _is_plain_grab(operation):
                                _unwind_unproven_feed(session, show, operation.feed_id)
                            logs.append(f"Gave up on '{name}' Ep {episode.episode_number}: {operation.last_error}")
                        else:
                            _expire_operation(session, operation, episode, show, error, hours)
                            logs.append(
                                f"Could not add '{name}' Ep {episode.episode_number}; "
                                f"retrying in {hours}h: {error}"
                            )
                        continue
                    _attempt_operation_add(session, qbit, settings, operation, episode)
                    continue
                updated_at = as_utc(operation.updated_at) or now
                if operation.status == TorrentOperationStatus.PREPARING and updated_at <= now - timedelta(minutes=15):
                    operation.status = TorrentOperationStatus.UNKNOWN
                    operation.ambiguous = True
                    operation.next_retry_at = now
                    operation.last_error = "qBittorrent has not exposed the tagged torrent; retry is pending."
                    operation.updated_at = now
                    episode.status = EpisodeStatus.REPLACING if operation.kind == "replace" else EpisodeStatus.QUEUED
                    episode.last_error = operation.last_error
                    episode.retry_after = now
                    session.add(operation)
                    session.add(episode)
                    session.commit()
                continue
        else:
            retry_at = as_utc(operation.next_retry_at)
            if operation.kind == "replace" and retry_at and retry_at > now:
                # Already checked; the poll runs every few seconds, the wait is minutes.
                continue
            previous_status = operation.status
            _normalize_hashed_operation(operation)
            if operation.status != previous_status:
                operation.updated_at = utc_now()
                session.add(operation)
                session.commit()
        message = _finish_operation(session, qbit, operation, episode)
        if message:
            logs.append(message)
    return logs


def update_episode_status(session: Session, qbit: QBitClient, settings: Settings) -> List[str]:
    logs = _recover_operations(session, qbit, settings)
    torrents = _managed_torrents(qbit, settings)
    by_hash = {_torrent_hash(torrent): torrent for torrent in torrents if _torrent_hash(torrent)}
    untracked = session.exec(select(Episode).where(
        Episode.torrent_hash.is_(None),
        Episode.status == EpisodeStatus.COMPLETED,
    )).all()
    for episode in untracked:
        show = session.get(Monitored, episode.monitored_id)
        if not show:
            continue
        for torrent in torrents:
            name = str(getattr(torrent, "name", "") or "").strip()
            if not name or not episode.release_title:
                continue
            if not same_release(name, episode.release_title):
                continue
            episode.torrent_hash = _torrent_hash(torrent)
            episode.release_title = name
            episode.status = EpisodeStatus.COMPLETED if is_seeding_torrent(torrent) else EpisodeStatus.DOWNLOADING
            episode.last_error = None
            session.add(episode)
            break
    episodes = session.exec(select(Episode).where(
        Episode.status.in_([
            EpisodeStatus.QUEUED,
            EpisodeStatus.DOWNLOADING,
            EpisodeStatus.REPLACING,
        ])
        # A torrent in an error state (disk full, drive unmounted) can recover, so a
        # failed episode that still has its torrent keeps being watched.
        | ((Episode.status == EpisodeStatus.FAILED) & Episode.torrent_hash.is_not(None))
    )).all()
    finished: List[Tuple[str, int, Optional[str]]] = []
    for episode in episodes:
        torrent = by_hash.get(episode.torrent_hash)
        if not torrent:
            if episode.status in (EpisodeStatus.REPLACING, EpisodeStatus.FAILED):
                continue
            if episode.torrent_hash:
                reason = "Torrent is no longer present in qBittorrent; queued for a safe retry."
                episode.status = EpisodeStatus.WANTED
                episode.torrent_hash = None
                episode.operation_tag = None
                episode.downloaded_at = None
                episode.last_error = reason
                episode.retry_after = utc_now() + timedelta(minutes=15)
                session.add(episode)
                logs.append(f"Requeued {show_name(episode, session)} Ep {episode.episode_number} after qBittorrent removal")
            continue
        if episode.status == EpisodeStatus.REPLACING:
            continue
        if is_seeding_torrent(torrent):
            if episode.status != EpisodeStatus.COMPLETED:
                _complete_episode(session, episode)
                name = show_name(episode, session)
                logs.append(f"Completed {name} Ep {episode.episode_number}")
                finished.append((name, episode.episode_number, show_cover(episode, session)))
        elif _torrent_state(torrent) in _FAILED_TORRENT_STATES:
            error = f"qBittorrent torrent state: {_torrent_state(torrent)}"
            if episode.status != EpisodeStatus.FAILED or episode.last_error != error:
                episode.status = EpisodeStatus.FAILED
                episode.last_error = error
                session.add(episode)
        elif episode.status == EpisodeStatus.FAILED and _is_healthy_torrent(torrent):
            episode.status = EpisodeStatus.DOWNLOADING
            episode.last_error = None
            session.add(episode)
            logs.append(f"Resumed {show_name(episode, session)} Ep {episode.episode_number} after qBittorrent recovered")
    logs.extend(_release_stale_operation_tags(session, qbit))
    _sweep_operation_tags(session, qbit)
    session.commit()
    if settings.desktop_notifications:
        for name, number, cover in finished:
            notify_episode(name, number, cover_url=cover)
    return logs


def _present_torrents(qbit: QBitClient, hashes: List[str]) -> Dict[str, Any]:
    present: Dict[str, Any] = {}
    for start in range(0, len(hashes), 100):
        for torrent in qbit.get_torrents(hashes=hashes[start:start + 100]):
            torrent_hash = _torrent_hash(torrent)
            if torrent_hash:
                present[torrent_hash.lower()] = torrent
    return present


def reconcile_removed_torrents(session: Session, qbit: QBitClient) -> List[str]:
    """Forget the torrent of a finished episode that the user deleted in qBittorrent.

    The episode stays COMPLETED; with no stored torrent it reads "unmanaged" and
    offers Restore, the same state as an episode that was never adopted. If the
    torrent comes back, the import adopts it again. Torrents that are still there
    also give their real name to the episode. Runs once per AniList sync.
    """
    episodes = session.exec(select(Episode).where(
        Episode.status == EpisodeStatus.COMPLETED,
        Episode.torrent_hash.is_not(None),
    )).all()
    by_hash: Dict[str, List[Episode]] = {}
    for episode in episodes:
        by_hash.setdefault(str(episode.torrent_hash).lower(), []).append(episode)
    if not by_hash:
        return []

    try:
        hashes = list(by_hash)
        present = _present_torrents(qbit, hashes)
        renamed = False
        for torrent_hash, torrent in present.items():
            torrent_name = getattr(torrent, "name", None)
            if not isinstance(torrent_name, str) or not torrent_name.strip():
                continue
            for episode in by_hash.get(torrent_hash, []):
                if episode.release_title != torrent_name.strip():
                    episode.release_title = torrent_name.strip()
                    session.add(episode)
                    renamed = True
        if renamed:
            session.commit()
        missing = [h for h in hashes if h not in present]
        if not missing:
            return []
        if len(hashes) >= 3 and not present:
            # None of them exist: qBittorrent has most likely not loaded its torrents yet.
            return []
        # Ask again before believing it, so a moment of qBittorrent listing too little is not enough.
        time.sleep(2)
        still_present = _present_torrents(qbit, missing)
        gone = [h for h in missing if h not in still_present]
    except QbitClientError as e:
        logger.debug(f"Could not check for removed torrents: {e}")
        return []

    logs: List[str] = []
    for torrent_hash in gone:
        for episode in by_hash[torrent_hash]:
            episode.torrent_hash = None
            episode.last_error = None
            session.add(episode)
            logs.append(
                f"{show_name(episode, session)} Ep {episode.episode_number}: torrent removed in qBittorrent, no longer managed."
            )
    if logs:
        session.commit()
    return logs


def _release_stale_operation_tags(session: Session, qbit: QBitClient) -> List[str]:
    """
    Remove per-operation tags left behind before the tag was released on success.

    Earlier builds kept the random ``kisetsu-op-<uuid>`` tag on every torrent
    forever, because nothing removed it once the hash made it redundant. This
    sweeps whatever is already sitting in the user's qBittorrent and converges:
    the column is cleared, so each episode is only ever cleaned once. It runs
    here rather than as a migration because removing a tag needs qBittorrent,
    which the database layer has no access to.
    """
    logs: List[str] = []
    episodes = session.exec(
        select(Episode).where(
            Episode.operation_tag.isnot(None),
            Episode.torrent_hash.isnot(None),
        )
    ).all()
    for episode in episodes:
        if episode.status in {EpisodeStatus.QUEUED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING}:
            # Still in flight: the tag is the only way to find the torrent.
            continue
        _release_operation_tag(qbit, episode.torrent_hash, episode.operation_tag)
        episode.operation_tag = None
        session.add(episode)
    return logs


_OPERATION_TAG_PATTERN = re.compile(r"^kisetsu-op-[0-9a-f]{32}$")


def _sweep_operation_tags(session: Session, qbit: QBitClient) -> None:
    """Delete operation tags from qBittorrent's tag list that nothing needs any more.

    A tag is needed only while it is the sole way to find a torrent, i.e. for an
    operation or in-flight episode that has no hash yet. Every other
    ``kisetsu-op-`` tag is left over from a finished, failed or canceled
    operation. Tags with any other name are never touched.
    """
    try:
        defined = [tag for tag in qbit.get_tags() if _OPERATION_TAG_PATTERN.match(tag)]
        if not defined:
            return
        needed = set(session.exec(
            select(TorrentOperation.operation_tag).where(
                TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
                TorrentOperation.new_torrent_hash.is_(None),
            )
        ).all())
        needed.update(session.exec(
            select(Episode.operation_tag).where(
                Episode.status.in_([EpisodeStatus.QUEUED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING]),
                Episode.torrent_hash.is_(None),
            )
        ).all())
        stale = [tag for tag in defined if tag not in needed]
        if stale:
            qbit.delete_tags(stale)
            logger.debug(f"Deleted {len(stale)} leftover operation tag(s) from qBittorrent.")
    except QbitClientError as e:
        logger.debug(f"Could not sweep operation tags: {e}")


def has_unsettled_work(session: Session) -> bool:
    """Whether an operation or episode is still in flight and needs checking against qBittorrent."""
    if session.exec(
        select(TorrentOperation.id).where(TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES))).limit(1)
    ).first() is not None:
        return True
    return session.exec(
        select(Episode.id).where(
            Episode.status.in_([EpisodeStatus.QUEUED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING])
        ).limit(1)
    ).first() is not None


def _grab_feeds(session: Session, show: Monitored, feeds: List[Feed]) -> List[Feed]:
    """
    The feeds this show may read from this cycle.

    Feed assignment contract (direct engine)
    -----------------------------------------
    * ``learned_feed_id`` set -> exactly that feed, forever. It has already
      delivered a release, which outranks every heuristic we have, so nothing
      else is even fetched.
    * ``current_feed_id`` set (assigned or pinned) -> exactly that feed.
    * Neither set -> *discovery*. Every feed is offered, in ``(priority, id)``
      order, and the first one carrying a matching, mappable release wins. See
      :func:`evaluate_and_grab_releases` for the priority grace window applied
      before a lower-ranked feed is adopted.
    """
    if not feeds:
        return []
    if show.feed_is_locked:
        locked = next((feed for feed in feeds if feed.id == show.learned_feed_id or feed.id == show.current_feed_id), None)
        # A locked show reads one feed or none at all. Fanning out here would be
        # exactly the "grab from any feed" behaviour the lock exists to prevent.
        return [locked] if locked is not None else []
    assigned = next((feed for feed in feeds if feed.id == show.current_feed_id), None)
    if assigned is not None:
        return [assigned]
    return sorted(feeds, key=lambda feed: (feed.priority, feed.id))


def _discovery_window_open(
    session: Session,
    show: Monitored,
    feed: Feed,
    top_feed: Optional[Feed],
    article: Dict[str, Any],
    now: datetime,
) -> bool:
    """
    Whether a release found during feed discovery may be grabbed now.

    On the highest-ranked feed, yes. On any other feed, the show only adopts it
    once the same feed has held the release for the whole grace window, measured
    from the article's own publish time when qBittorrent gave us one. The
    candidate is recorded so the window survives between cycles, and it is
    cleared the moment a higher-ranked feed is seen carrying the release, because
    that feed is then simply the winner.
    """
    if top_feed is None or feed.id == top_feed.id:
        if show.candidate_feed_id is not None or show.candidate_feed_since is not None:
            show.candidate_feed_id = None
            show.candidate_feed_since = None
            session.add(show)
            session.commit()
        return True

    published_at = parse_article_date(article)
    # In UTC: the database drops the offset, and as_utc reads the stored value as UTC.
    reference = published_at.astimezone(timezone.utc) if published_at > datetime.min.replace(tzinfo=timezone.utc) else now

    if show.candidate_feed_id != feed.id:
        show.candidate_feed_id = feed.id
        show.candidate_feed_since = reference
        session.add(show)
        session.commit()
        logger.info(
            f"'{show.display_name}': '{article.get('title', '')}' is on #{feed.priority} feed "
            f"'{feed.qbit_feed_name}', not the #{top_feed.priority} feed "
            f"'{top_feed.qbit_feed_name}'; holding the adoption open for "
            f"{FEED_DISCOVERY_GRACE_SECONDS // 60}m to see whether a higher-ranked feed posts it first."
        )
        return False

    since = as_utc(show.candidate_feed_since) or reference
    if since + timedelta(seconds=FEED_DISCOVERY_GRACE_SECONDS) > now:
        return False

    logger.info(
        f"'{show.display_name}': #{feed.priority} feed '{feed.qbit_feed_name}' has held the "
        f"release for the full grace window; adopting it."
    )
    show.candidate_feed_id = None
    show.candidate_feed_since = None
    session.add(show)
    session.commit()
    return True


def _date_mapped_episode(
    session: Session,
    show: Monitored,
    raw_episode: int,
    target_count: int,
    article: Dict[str, Any],
    tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> Optional[int]:
    article_at = parse_article_date(article)
    if article_at.year <= 2000:
        return None
    scheduled = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number <= target_count,
            Episode.air_at.is_not(None),
        )
    ).all()
    dated = [
        (episode, as_utc(episode.air_at))
        for episode in scheduled
        if episode.air_at
    ]
    if not dated:
        return None

    # A release published at article_at can only be an episode that has already aired
    # (allowing the early air tolerance setting for feeds that post before AniList's air time).
    early_air_tolerance = timedelta(hours=max(0, tolerance_hours))
    valid_candidates = [
        (episode, air_at)
        for episode, air_at in dated
        if air_at <= article_at + early_air_tolerance
        and (article_at - air_at) <= AIR_DATE_TOLERANCE
        and 1 <= episode.episode_number <= target_count
    ]
    if not valid_candidates:
        return None

    # Pick the most recently aired episode prior to the release (closest to article_at)
    episode, air_at = max(valid_candidates, key=lambda item: item[1])
    return episode.episode_number


# A release is allowed to be published this much before the air time AniList gives
# (AniList air times drift by a day or more from the real release); an older season is off by months.
PUBLISHED_BEFORE_AIR_SLACK_HOURS = 48


def _published_before_air(
    article: Dict[str, Any],
    episode: Episode,
    show: Monitored,
    tolerance_hours: int,
    first_air: Optional[datetime] = None,
) -> bool:
    """Whether a dated release was published before its episode could have aired.

    Season tags and episode numbers differ from group to group, so a release of an
    earlier season can carry a number that fits this one. Its publish date cannot:
    it is long before the episode. An episode without an air time is held to the
    show's first one, since nothing can predate the whole run. Undated articles
    and shows with nothing scheduled cannot be judged and are left alone. (The
    stored air times stay good while a schedule sync is pending: they can move by
    hours, not by seasons, so ``schedule_stale`` does not switch this off.)
    """
    reference = episode.air_at or first_air
    if reference is None:
        return False
    published = parse_article_date(article)
    if published.year <= 2000:
        return False
    slack = timedelta(hours=max(PUBLISHED_BEFORE_AIR_SLACK_HOURS, tolerance_hours))
    return published < as_utc(reference) - slack


# Torrents already reported as refused for adoption, so one that stays refused is mentioned once.
_refused_adoptions: Set[str] = set()


def adoption_refusal(
    session: Session,
    show: Monitored,
    episode: Episode,
    torrent: Any,
    now: datetime,
    tolerance_hours: int,
    contexts: Dict[int, "_GrabContext"],
) -> Optional[str]:
    """Why an existing torrent cannot be taken as ``episode``'s release, or None when it can.

    A release name does not tell one season of a show from the next, so adoption goes
    by dates: an episode that has not aired has no release yet, and a torrent that
    qBittorrent added well before the episode could have aired is from an earlier run.
    ``contexts`` keeps each show's schedule between torrents.
    """
    context = contexts.get(show.id)
    if context is None:
        context = contexts[show.id] = _show_grab_context(
            session, show, now, _air_horizon(show, now, tolerance_hours),
        )
    reason: Optional[str] = None
    if context.latest_aired is None or episode.episode_number > context.latest_aired:
        reason = "the episode has not aired yet"
    else:
        first_air = min((as_utc(item.air_at) for item in context.episodes if item.air_at), default=None)
        if _added_before_air(torrent, as_utc(episode.air_at) or first_air, tolerance_hours):
            reason = "it was added before the episode aired"
    if reason:
        torrent_hash = str(getattr(torrent, "hash", "") or "")
        if torrent_hash not in _refused_adoptions:
            if len(_refused_adoptions) > 5000:
                _refused_adoptions.clear()
            _refused_adoptions.add(torrent_hash)
            logger.debug(
                f"Not adopting '{getattr(torrent, 'name', '')}' as Ep {episode.episode_number} of "
                f"'{show.display_name}': {reason}."
            )
    return reason


_RELEASE_HOLDING = {
    EpisodeStatus.COMPLETED,
    EpisodeStatus.DOWNLOADING,
    EpisodeStatus.QUEUED,
    EpisodeStatus.REPLACING,
}
_RELEASE_FIELDS = (
    "status",
    "version",
    "release_title",
    "release_group",
    "source_episode",
    "feed_id",
    "feed_item_id",
    "torrent_url",
    "torrent_hash",
    "operation_tag",
    "downloaded_at",
    "last_error",
    "retry_after",
    "last_attempt_at",
    "attempt_count",
)


def rebase_ledger(
    session: Session,
    show: Monitored,
    feed_id: Optional[int],
    offset: int,
    latest_aired: Optional[int] = None,
    target_count: Optional[int] = None,
) -> bool:
    """Renumber the show's downloaded releases so ``source_episode - offset`` is their row.

    Rows written before a feed's numbering was known (rules-era confirmations,
    adopted torrents) sit under the raw number the feed used. Once the real offset
    is known they must move, or the true episode would look already downloaded.
    Nothing changes unless every release can move safely: each target must exist,
    be an episode that has aired, and be free or itself moving, and no affected
    episode may have a torrent operation in flight.
    """
    if not show.id:
        return False
    rows = session.exec(select(Episode).where(Episode.monitored_id == show.id)).all()
    by_number = {row.episode_number: row for row in rows}
    movers = [
        row for row in rows
        if row.source_episode is not None
        and row.status in _RELEASE_HOLDING
        and row.feed_id in (None, feed_id)
    ]
    if not movers:
        return False
    limit = target_count or max(by_number, default=0)
    plan = []
    for row in movers:
        target = row.source_episode - offset
        if not 1 <= target <= limit:
            return False
        if latest_aired is not None and target > latest_aired:
            return False
        plan.append((row, target))
    mover_ids = {row.id for row in movers}
    for row, target in plan:
        occupant = by_number.get(target)
        if occupant is None:
            return False
        if occupant.id not in mover_ids and (occupant.status in _RELEASE_HOLDING or occupant.torrent_hash):
            return False
    touched = mover_ids | {by_number[target].id for _, target in plan}
    in_flight = session.exec(
        select(TorrentOperation.id).where(
            TorrentOperation.episode_id.in_(list(touched)),
            TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
        )
    ).first()
    if in_flight is not None:
        return False
    if all(row.episode_number == target for row, target in plan):
        return False

    snapshots = [({name: getattr(row, name) for name in _RELEASE_FIELDS}, target) for row, target in plan]
    for row, _ in plan:
        row.status = EpisodeStatus.WANTED
        row.version = 1
        for name in _RELEASE_FIELDS:
            if name not in ("status", "version", "attempt_count"):
                setattr(row, name, None)
        row.attempt_count = 0
        session.add(row)
    for values, target in snapshots:
        row = by_number[target]
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)
    completed = [row.episode_number for row in rows if row.status == EpisodeStatus.COMPLETED]
    show.last_confirmed_episode = max(completed, default=0)
    session.add(show)
    session.commit()
    return True


def _infer_offset_from_conflict(
    session: Session,
    show: Monitored,
    feed: Feed,
    raw_episode: int,
    local_episode: int,
    latest_aired: Optional[int],
    target_count: int,
) -> None:
    """Learn a feed's numbering offset when a release contradicts the ledger.

    The air date says this release is episode ``local_episode``, but that row
    already holds a release the feed numbered differently. When the feed is
    consistently higher (it counts an earlier cour or a split entry, as AniList
    lists them), the stored rows are shifted so the true episode is wanted again.
    """
    row = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number == local_episode,
        )
    ).first()
    if row is None or row.status not in _RELEASE_HOLDING:
        return
    if row.source_episode is None or row.source_episode == raw_episode or row.feed_id not in (None, feed.id):
        return
    offset = raw_episode - local_episode
    held = session.exec(
        select(Episode.source_episode).where(
            Episode.monitored_id == show.id,
            Episode.status.in_(list(_RELEASE_HOLDING)),
            Episode.source_episode.is_not(None),
        )
    ).all()
    if offset <= 0 or raw_episode <= max(held, default=0):
        return
    if rebase_ledger(session, show, feed.id, offset, latest_aired, target_count):
        logger.info(
            f"'{show.display_name}': '{feed.qbit_feed_name}' numbers episodes {offset} higher than "
            f"AniList; renumbered the stored releases and mapped raw {raw_episode} to episode {local_episode}."
        )


def _mapped_episode(
    session: Session,
    show: Monitored,
    feed: Feed,
    raw_episode: int,
    latest_aired: Optional[int],
    target_count: int,
    article: Dict[str, Any],
    learn: bool = True,
    newest_raw: Optional[int] = None,
    tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> Optional[int]:
    """Map a feed's raw episode number to the show's local one.

    ``learn=False`` is for read-only previews: it skips the offset inference
    that can renumber stored releases. ``newest_raw`` is the highest episode
    number among the releases being mapped together; it lets absolute numbers
    ("45", "46", "47", "48") be told apart when nothing else can.
    """
    mapping = session.exec(
        select(EpisodeNumberMapping).where(
            EpisodeNumberMapping.monitored_id == show.id,
            EpisodeNumberMapping.feed_id == feed.id,
        )
    ).first()
    if mapping is not None:
        local_episode = raw_episode - mapping.offset
        if not 1 <= local_episode <= target_count:
            return None
        if latest_aired is not None and local_episode > latest_aired:
            logger.debug(
                f"Release episode {raw_episode} mapped to {local_episode} for '{show.display_name}', "
                f"but latest aired is {latest_aired}. Rejecting mapping."
            )
            return None
        return local_episode

    if 1 <= raw_episode <= target_count and latest_aired is not None and raw_episode <= latest_aired + 1:
        # A number that fits the aired part of the season is taken as it is unless the
        # release is dated well before that episode aired (a feed numbering one higher
        # than AniList). Guessing from the date alone would shift releases a feed puts
        # out hours before AniList's air time ("03" posted before ep 3 airs is not ep 2).
        raw_row = session.exec(select(Episode).where(
            Episode.monitored_id == show.id, Episode.episode_number == raw_episode,
        )).first()
        raw_air_at = as_utc(raw_row.air_at) if raw_row is not None and raw_row.air_at else None
        published = parse_article_date(article)
        if raw_air_at is None:
            if raw_episode <= latest_aired:
                return raw_episode
        elif published.year <= 2000 or raw_air_at - published <= timedelta(hours=PUBLISHED_BEFORE_AIR_SLACK_HOURS):
            return raw_episode

    by_date = _date_mapped_episode(session, show, raw_episode, target_count, article, tolerance_hours)
    if by_date is not None:
        if learn:
            _infer_offset_from_conflict(session, show, feed, raw_episode, by_date, latest_aired, target_count)
        return by_date
    if latest_aired is not None and raw_episode > target_count and 1 <= latest_aired <= target_count:
        # "The newest release is the latest aired episode" only holds while the show is
        # current; for a season that ended long ago it would pin any new item to its end.
        latest_row = session.exec(select(Episode).where(
            Episode.monitored_id == show.id, Episode.episode_number == latest_aired,
        )).first()
        if latest_row is not None and latest_row.air_at is not None and (
            utc_now() - as_utc(latest_row.air_at) > timedelta(days=HUNTING_RECENT_DAYS)
        ):
            return None
        if newest_raw is None:
            return latest_aired
        # The newest release is taken to be the latest aired episode and the
        # older ones count back from it.
        local_episode = raw_episode - (newest_raw - latest_aired)
        return local_episode if 1 <= local_episode <= latest_aired else None
    if 1 <= raw_episode <= target_count and (latest_aired is None or raw_episode <= latest_aired + 1):
        return raw_episode
    logger.info(
        f"Ambiguous episode numbering for '{show.display_name}' on '{feed.qbit_feed_name}': "
        f"raw {raw_episode} with {target_count} episodes and latest aired {latest_aired}; awaiting manual mapping."
    )
    return None


def _record_mapping_evidence(
    session: Session,
    show: Monitored,
    feed: Feed,
    raw_episode: int,
    local_episode: int,
) -> None:
    """Persist/confirm a mapping only after a release was actually accepted."""
    now = utc_now()
    mapping = session.exec(
        select(EpisodeNumberMapping).where(
            EpisodeNumberMapping.monitored_id == show.id,
            EpisodeNumberMapping.feed_id == feed.id,
        )
    ).first()
    if mapping is None:
        mapping = EpisodeNumberMapping(
            monitored_id=show.id,
            feed_id=feed.id,
            offset=raw_episode - local_episode,
            source=EpisodeMappingSource.INFERRED.name,
            evidence_count=1,
            first_evidence_at=now,
            last_evidence_at=now,
        )
        session.add(mapping)
        session.commit()
        return
    if normalize_mapping_source(mapping.source) != EpisodeMappingSource.INFERRED.name:
        return
    evidence = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.feed_id == feed.id,
            Episode.source_episode.is_not(None),
        )
    ).all()
    consistent = [
        episode for episode in evidence
        if (episode.source_episode or 0) - episode.episode_number == mapping.offset
    ]
    mapping.evidence_count = max(mapping.evidence_count, len(consistent) + 1)
    mapping.last_evidence_at = now
    if len(consistent) >= 1:
        mapping.source = EpisodeMappingSource.CONFIRMED.name
    session.add(mapping)
    session.commit()


def _restore_episode_before_operation(
    session: Session,
    operation: TorrentOperation,
    episode: Episode,
    error: str,
    retry_at: Optional[datetime],
) -> None:
    if operation.kind == "replace" or _is_restore_operation(operation):
        episode.status = EpisodeStatus(operation.old_episode_status)
        episode.version = operation.old_version
        episode.feed_id = operation.old_feed_id
        episode.release_title = operation.old_release_title
        episode.release_group = operation.old_release_group
        episode.torrent_url = operation.old_torrent_url
        episode.torrent_hash = operation.old_torrent_hash
        episode.operation_tag = operation.old_operation_tag
    else:
        old_status = str(operation.old_episode_status or "").strip().lower()
        if old_status in EpisodeStatus._value2member_map_:
            episode.status = EpisodeStatus(old_status)
        else:
            episode.status = EpisodeStatus.WANTED
        episode.version = max(1, operation.old_version)
        episode.feed_id = None
        episode.release_title = None
        episode.release_group = None
        episode.torrent_url = None
        episode.torrent_hash = None
        episode.operation_tag = None
    episode.last_error = error
    episode.retry_after = retry_at
    session.add(episode)


def _set_operation_retry(
    session: Session,
    operation: TorrentOperation,
    episode: Episode,
    error: str,
    retry_at: datetime,
    status: TorrentOperationStatus,
    ambiguous: bool,
) -> None:
    operation.status = status
    operation.last_error = error
    operation.next_retry_at = retry_at
    operation.ambiguous = ambiguous
    operation.updated_at = utc_now()
    _restore_episode_before_operation(session, operation, episode, error, retry_at)
    session.add(operation)
    session.add(episode)
    session.commit()


_MAGNET_HASH_RE = re.compile(r"xt=urn:btih:([0-9a-z]+)", re.IGNORECASE)


def _magnet_info_hash(url: str) -> Optional[str]:
    """The lowercase hex info-hash of a magnet link, or None for any other URL."""
    match = _MAGNET_HASH_RE.search(url or "")
    if not match:
        return None
    value = match.group(1)
    if len(value) == 40:
        return value.lower()
    if len(value) == 32:
        try:
            return base64.b32decode(value.upper()).hex()
        except ValueError:
            return None
    return None


def _already_added_torrent(qbit: QBitClient, operation: TorrentOperation) -> Optional[Any]:
    """The torrent qBittorrent already holds for this operation's magnet link.

    A rejected add is ambiguous: qBittorrent answers a duplicate the same way it
    answers a bad link. A torrent with the release's own info-hash means the
    release is already there and only needs to be tracked.
    """
    info_hash = _magnet_info_hash(operation.new_torrent_url)
    if not info_hash:
        return _already_added_by_name(qbit, operation)
    try:
        existing = list(qbit.get_torrents(hashes=[info_hash]))
    except Exception as e:
        logger.debug(f"Could not look for an existing torrent {info_hash}: {e}")
        return None
    return existing[0] if existing else None


def release_key(name: str) -> str:
    """A release name reduced to its words, so naming styles compare equal.

    A feed title ("Blue Box S02E01 … H.264-VARYG (Ao no Hako …)") and the
    torrent's file name ("Blue.Box.S02E01.….H.264-VARYG.mkv") name one release.
    """
    key = re.sub(r"\.(mkv|mp4|avi)$", "", (name or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"[^0-9a-z]+", " ", key.lower()).strip()


def same_release(first: str, second: str) -> bool:
    """Whether two names are one release: equal words, or one extends the other by whole words."""
    a, b = release_key(first), release_key(second)
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")


def _find_torrent_by_name(qbit: QBitClient, title: str) -> Optional[Any]:
    """The one torrent qBittorrent holds under this release name, whatever its tag or category.

    A .torrent link carries no info-hash, so a torrent can only be recognised by
    name. Adopting needs a single unambiguous match.
    """
    if not release_key(title):
        return None
    try:
        matches = [
            torrent for torrent in qbit.get_torrents()
            if same_release(str(getattr(torrent, "name", "") or ""), title)
        ]
    except Exception as e:
        logger.debug(f"Could not look for an existing torrent named '{title}': {e}")
        return None
    return matches[0] if len(matches) == 1 else None


def _already_added_by_name(qbit: QBitClient, operation: TorrentOperation) -> Optional[Any]:
    return _find_torrent_by_name(qbit, operation.release_title)


def _attempt_operation_add(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    operation: TorrentOperation,
    episode: Episode,
) -> None:
    show = session.get(Monitored, episode.monitored_id)
    if not show:
        return
    retry_at = utc_now() + timedelta(minutes=15 if operation.ambiguous else 1)
    if settings.default_category and not qbit.ensure_category_exists(settings.default_category):
        _set_operation_retry(
            session,
            operation,
            episode,
            "Could not create the configured qBittorrent category.",
            retry_at,
            TorrentOperationStatus.RETRY_WAIT,
            False,
        )
        return
    save_path = resolve_save_path(
        settings.base_dir,
        effective_title(show, settings.title_language),
        show.save_folder,
    )
    operation.status = TorrentOperationStatus.PREPARING
    operation.attempt_count += 1
    operation.last_attempt_at = utc_now()
    operation.next_retry_at = None
    operation.last_error = None
    operation.ambiguous = False
    operation.updated_at = utc_now()
    episode.status = EpisodeStatus.REPLACING if operation.kind == "replace" else EpisodeStatus.QUEUED
    if operation.kind != "replace":
        episode.version = operation.version
        episode.feed_id = operation.feed_id
        episode.feed_item_id = operation.feed_item_id
        episode.release_title = operation.release_title
        episode.release_group = operation.release_group
        episode.source_episode = operation.source_episode
        episode.torrent_url = operation.new_torrent_url
        episode.operation_tag = operation.operation_tag
        episode.torrent_hash = None
        episode.downloaded_at = None
    episode.last_error = None
    episode.retry_after = None
    episode.last_attempt_at = operation.last_attempt_at
    episode.attempt_count = operation.attempt_count
    session.add(operation)
    session.add(episode)
    session.commit()
    tags = _episode_tags(operation.operation_tag)
    adopted: Optional[Any] = None
    try:
        qbit.add_torrent(
            urls=operation.new_torrent_url,
            save_path=save_path,
            category=settings.default_category,
            tags=tags,
            is_paused=operation.kind == "replace",
            ratio_limit=settings.default_seed_ratio,
        )
    except Exception as e:
        adopted = _already_added_torrent(qbit, operation)
        failure = e
        if adopted is not None:
            try:
                qbit.add_torrent_tags([_torrent_hash(adopted)], tags)
                failure = None
            except QbitClientError as tag_error:
                failure = tag_error
        if failure is not None:
            _set_operation_retry(
                session,
                operation,
                episode,
                str(failure),
                utc_now() + timedelta(minutes=15),
                TorrentOperationStatus.UNKNOWN,
                True,
            )
            return
    # The feed that just delivered a release is now proven for this show. It is
    # recorded as learned so nothing downstream may quietly move the show
    # somewhere else on the strength of a heuristic. An operation whose feed was
    # deleted since proves nothing about any feed.
    if operation.feed_id is not None:
        show.learned_feed_id = operation.feed_id
        show.current_feed_id = operation.feed_id
        show.candidate_feed_id = None
        show.candidate_feed_since = None
    # Learn the series name, never the filename. The raw title carries this one
    # episode's number, quality and group, so storing it would build a rule that
    # matches nothing but the episode it came from.
    show.matched_title = operation.parsed_title or operation.release_title
    show.matched_release_group = operation.release_group
    if show.status not in _INACTIVE_SHOW_STATUSES:
        show.status = MonitoredStatus.FIXED
    session.add(show)
    session.add(operation)
    session.commit()
    torrent = adopted if adopted is not None else _find_tagged(qbit, operation.operation_tag)
    if torrent:
        operation.new_torrent_hash = _torrent_hash(torrent)
        operation.status = TorrentOperationStatus.ADDED
        operation.updated_at = utc_now()
        session.add(operation)
        session.commit()
        _release_operation_tag(qbit, operation.new_torrent_hash, operation.operation_tag)
        _finish_operation(session, qbit, operation, episode)
        return
    episode.status = EpisodeStatus.REPLACING if operation.kind == "replace" else EpisodeStatus.QUEUED
    episode.last_error = "Waiting for qBittorrent to expose the new torrent."
    session.add(episode)
    session.commit()


def _start_operation(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    feed: Feed,
    episode: Episode,
    article: Dict[str, Any],
    parsed: Dict[str, Any],
    version: int,
    switch_source: bool = False,
) -> Optional[TorrentOperation]:
    if _operation_for_episode(session, episode.id):
        return None
    torrent_url = str(article.get("torrentURL") or article.get("link") or "")
    if not torrent_url:
        return None
    previous_version = episode.version
    has_existing = bool(
        episode.torrent_hash
        or episode.release_title
        or episode.status in {EpisodeStatus.DOWNLOADING, EpisodeStatus.COMPLETED, EpisodeStatus.REPLACING}
    )
    is_replace = (version > previous_version or switch_source) and has_existing
    old_torrent_hash = _find_old_hash(qbit, settings, show, episode) if is_replace else None
    if is_replace and not old_torrent_hash and episode.release_title and _find_torrent_by_name(qbit, episode.release_title) is None:
        # The previous release is gone from qBittorrent (the user deleted it), so
        # there is nothing to replace: take the newer one as a plain download.
        is_replace = False
    if is_replace and not old_torrent_hash:
        episode.last_error = "Cannot safely replace an episode without a verified torrent hash."
        episode.retry_after = None
        session.add(episode)
        session.commit()
        return None
    operation_tag = _operation_tag()
    item_id = str(article.get("id") or torrent_url or article.get("title", ""))
    operation = TorrentOperation(
        episode_id=episode.id,
        kind="replace" if is_replace else "grab",
        status=TorrentOperationStatus.PREPARING,
        operation_tag=operation_tag,
        release_title=article.get("title", ""),
        parsed_title=parsed.get("title"),
        release_group=parsed.get("release_group"),
        version=version,
        new_torrent_url=torrent_url,
        old_torrent_hash=old_torrent_hash,
        old_release_title=episode.release_title,
        old_release_group=episode.release_group,
        old_torrent_url=episode.torrent_url,
        old_feed_id=episode.feed_id,
        old_version=episode.version,
        old_episode_status=episode.status.value,
        old_operation_tag=episode.operation_tag,
        source_episode=int(parsed.get("episode")) if parsed.get("episode") is not None else None,
        feed_item_id=item_id,
        feed_id=feed.id,
        attempt_count=0,
    )
    session.add(operation)
    session.commit()
    _attempt_operation_add(session, qbit, settings, operation, episode)
    return operation


def _unwind_unproven_feed(session: Session, show: Monitored, feed_id: Optional[int]) -> None:
    """Forget what a grab taught about a feed once nothing of that grab is left.

    Grabbing from a feed locks the show to it and records how the feed numbers
    episodes. If every such grab was canceled or removed, neither was ever proven,
    so they are released instead of steering the next releases.
    """
    if feed_id is None or show.id is None:
        return
    holders = session.exec(
        select(Episode.id).where(
            Episode.monitored_id == show.id,
            Episode.feed_id == feed_id,
            (Episode.status.in_(list(_RELEASE_HOLDING))) | Episode.torrent_hash.is_not(None),
        ).limit(1)
    ).first()
    if holders is not None:
        return
    if show.learned_feed_id == feed_id:
        show.learned_feed_id = None
        if not show.feed_pinned and show.current_feed_id == feed_id:
            show.current_feed_id = None
        show.candidate_feed_id = None
        show.candidate_feed_since = None
        session.add(show)
    for mapping in session.exec(
        select(EpisodeNumberMapping).where(
            EpisodeNumberMapping.monitored_id == show.id,
            EpisodeNumberMapping.feed_id == feed_id,
        )
    ).all():
        # Only what a grab taught: a mapping the user set stays.
        if normalize_mapping_source(mapping.source) in (
            EpisodeMappingSource.INFERRED.name, EpisodeMappingSource.CONFIRMED.name,
        ):
            session.delete(mapping)
    session.commit()


def cancel_episode_operations(
    session: Session,
    qbit: QBitClient,
    show: Monitored,
    episode: Episode,
    reason: str,
    keep_added: bool = False,
) -> int:
    """Cancel in-flight operations for an episode and dispose of new torrents.

    With ``keep_added`` an operation whose torrent is already in qBittorrent is
    left to finish.

    Only a retried failure is cancellable; a hard failure is terminal. Any
    torrent the operation added is deleted, and a replacement restores the
    previous release so no episode is left without a file.
    """
    canceled = 0
    operations = session.exec(
        select(TorrentOperation)
        .where(
            TorrentOperation.episode_id == episode.id,
            TorrentOperation.status.in_(list(CANCELLABLE_OPERATION_STATUSES)),
        )
        .order_by(TorrentOperation.created_at.desc())
    ).all()
    for operation in operations:
        if operation.status == TorrentOperationStatus.SEEDING:
            continue
        if keep_added and operation.new_torrent_hash:
            continue
        if operation.new_torrent_hash and operation.new_torrent_hash != operation.old_torrent_hash:
            try:
                _delete_torrent_by_hash(
                    qbit,
                    operation.new_torrent_hash,
                    keep_files_of=operation.old_torrent_hash if operation.kind == "replace" else None,
                )
            except Exception as e:
                operation.last_error = f"{reason} Cleanup failed: {e}"
                session.add(operation)
                session.commit()
                continue
        operation.status = TorrentOperationStatus.CANCELED
        operation.last_error = reason
        operation.next_retry_at = None
        operation.updated_at = utc_now()
        _restore_episode_before_operation(
            session,
            operation,
            episode,
            reason,
            utc_now() + timedelta(minutes=15),
        )
        if operation.kind == "replace" or _is_restore_operation(operation):
            episode.retry_after = None
        else:
            episode.status = EpisodeStatus.WANTED
            episode.torrent_hash = None
            episode.retry_after = None
        session.add(operation)
        session.add(episode)
        session.commit()
        _unwind_unproven_feed(session, show, operation.feed_id)
        canceled += 1
    return canceled


def cancel_unfinished_downloads(
    session: Session,
    qbit: QBitClient,
    show: Monitored,
    keep_feed_id: Optional[int],
    reason: str,
) -> List[int]:
    """Cancel the show's unfinished downloads that came from a feed other than ``keep_feed_id``.

    For a user who switched feeds: what is still downloading from the old feed is
    deleted with its partial files and the episode is wanted again, so the new feed
    supplies it. A finished torrent is never touched, nor is an episode whose origin
    is unknown (no feed recorded). A torrent whose hash was never recorded is found
    by its operation tag. Returns the episode numbers that were reset.
    """
    reset: List[int] = []
    old_feeds: set = set()
    episodes = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.status.in_([EpisodeStatus.QUEUED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING]),
        ).order_by(Episode.episode_number)
    ).all()
    for episode in episodes:
        operations = list(session.exec(
            select(TorrentOperation).where(
                TorrentOperation.episode_id == episode.id,
                TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
            )
        ).all())
        replacing = any(op.kind == "replace" for op in operations)
        if episode.torrent_hash and not replacing:
            # A grab that finished its bookkeeping while its torrent is still downloading.
            # During a replace the hash is the release being replaced, which stays.
            operations += [
                op for op in session.exec(
                    select(TorrentOperation).where(
                        TorrentOperation.episode_id == episode.id,
                        TorrentOperation.status == TorrentOperationStatus.COMPLETED,
                        TorrentOperation.new_torrent_hash == episode.torrent_hash,
                    )
                ).all()
                if op not in operations
            ]
        from_feed = next((op.feed_id for op in operations if op.feed_id), None) or episode.feed_id
        if from_feed is None or from_feed == keep_feed_id:
            continue

        hashes = [op.new_torrent_hash for op in operations if op.new_torrent_hash]
        if episode.torrent_hash and not replacing:
            hashes.append(episode.torrent_hash)
        torrents: Dict[str, Any] = {}
        for torrent_hash in dict.fromkeys(hashes):
            for torrent in qbit.get_torrents(hashes=[torrent_hash]):
                torrents[str(_torrent_hash(torrent) or torrent_hash).lower()] = torrent
        for op in operations:
            if not op.new_torrent_hash and op.operation_tag:
                torrent = _find_tagged(qbit, op.operation_tag)
                if torrent is not None:
                    torrents[str(_torrent_hash(torrent)).lower()] = torrent
        if any(_torrent_progress(torrent) >= 1.0 for torrent in torrents.values()):
            continue

        old_hash = next((op.old_torrent_hash for op in operations if op.kind == "replace"), None)
        for torrent_hash in torrents:
            _delete_torrent_by_hash(qbit, torrent_hash, keep_files_of=old_hash)
        for op in operations:
            op.status = TorrentOperationStatus.CANCELED
            op.last_error = reason
            op.next_retry_at = None
            op.updated_at = utc_now()
            _restore_episode_before_operation(session, op, episode, reason, None)
            session.add(op)
        if not replacing:
            episode.status = EpisodeStatus.WANTED
            episode.version = 1
            episode.release_title = None
            episode.release_group = None
            episode.feed_id = None
            episode.feed_item_id = None
            episode.torrent_url = None
            episode.torrent_hash = None
            episode.operation_tag = None
            episode.source_episode = None
            episode.downloaded_at = None
        episode.last_error = reason
        episode.retry_after = None
        session.add(episode)
        session.commit()
        reset.append(episode.episode_number)
        old_feeds.add(from_feed)
    for feed_id in old_feeds:
        _unwind_unproven_feed(session, show, feed_id)
    return reset


def _air_horizon(show: Monitored, now: datetime, tolerance_hours: int) -> datetime:
    """When an episode counts as aired for this show.

    The early-air tolerance only covers a show's first episodes, because after
    the first release AniList's air times can be taken as stated.
    """
    return now + timedelta(hours=tolerance_hours) if show.in_premiere else now


class _ShowMatcher(NamedTuple):
    """What matching a show against many titles needs, built once per cycle."""
    test_pattern: str
    prepared_aliases: List[Tuple[str, str]]
    learned_pattern: Optional[str] = None
    strict: bool = False
    season_known: bool = False
    season: Optional[int] = None


def _show_matcher(show: Monitored) -> _ShowMatcher:
    aliases = show.effective_aliases
    pattern, learned_pattern = show_match_patterns(aliases, show.matched_title)
    return _ShowMatcher(pattern, prepare_aliases(aliases), learned_pattern)


def _matcher_for_feed(matcher: _ShowMatcher, show: Monitored, feed: Feed, episodes: Iterable[Episode]) -> _ShowMatcher:
    """``matcher`` narrowed to what the show's learned feed has delivered.

    On the learned feed only the learned name matches, and a release must name the
    same season as the releases already downloaded from it (or none, when they
    name none). Other feeds, and a show that has not learned a feed, are untouched.
    """
    if show.learned_feed_id is None or show.learned_feed_id != feed.id or not matcher.learned_pattern:
        return matcher
    held = [
        episode.release_title
        for episode in episodes
        if episode.release_title
        and episode.status in _RELEASE_HOLDING
        and episode.feed_id in (None, feed.id)
    ]
    named = {release_season(title) for title in held} | {release_season(show.matched_title or "")}
    named.discard(None)
    if len(named) > 1:
        return matcher._replace(strict=True)
    if named:
        return matcher._replace(strict=True, season_known=True, season=next(iter(named)))
    # Nothing downloaded names a season, so the feed does not write one for this show.
    return matcher._replace(strict=True, season_known=bool(held))


def _decide(
    show: Monitored,
    title: str,
    matcher: Optional[_ShowMatcher] = None,
    parsed_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    unmarked_ok: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    if matcher is None:
        matcher = _show_matcher(show)
    # A title in the learned name that carries no arc marker shows that the group
    # does not label this show's cour, so there is nothing to compare with the
    # aliases' marker. A title with a marker of its own is still checked.
    learned_without_marker = bool(
        matcher.learned_pattern
        and release_arc_qualifier(title) is None
        and re.search(matcher.learned_pattern, title, re.IGNORECASE)
    )
    matched, _, parsed = match_release_to_show(
        title,
        show.effective_aliases,
        test_pattern=matcher.learned_pattern if matcher.strict else matcher.test_pattern,
        prepared_aliases=matcher.prepared_aliases,
        parsed_cache=parsed_cache,
        ignore_arc_marker=learned_without_marker or (unmarked_ok and release_arc_qualifier(title) is None),
        allow_fuzzy=not matcher.strict,
    )
    if matched and matcher.season_known and release_season(title) != matcher.season:
        return False, parsed
    return matched, parsed


def _decide_article(
    session: Session,
    show: Monitored,
    article: Dict[str, Any],
    matcher: _ShowMatcher,
    parsed_cache: Dict[str, Dict[str, Any]],
    target_count: int,
    tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> Tuple[bool, Dict[str, Any]]:
    """``_decide`` for a feed article, which also knows when it was published.

    A show whose aliases name an arc ("2nd & 3rd STAGE") refuses releases that carry
    no arc marker, but groups usually do not write one for a split entry. A release
    published right after one of this show's own scheduled air times is on its
    schedule, which the numbering cannot tell us but the date can; it is accepted
    without the marker. A release that names a different arc, has no date or falls
    outside the schedule (the previous stage's) stays refused.
    """
    title = article.get("title", "")
    matched, parsed = _decide(show, title, matcher, parsed_cache)
    if matched:
        return matched, parsed
    raw_episode = parsed.get("episode")
    if (
        raw_episode is not None
        and raw_episode > 0
        and _alias_arc_requirement(show.effective_aliases)
        and release_arc_qualifier(title) is None
        and _date_mapped_episode(session, show, int(raw_episode), target_count, article, tolerance_hours) is not None
    ):
        return _decide(show, title, matcher, parsed_cache, unmarked_ok=True)
    return matched, parsed


def _newest_raw_episode(
    show: Monitored,
    articles: List[Dict[str, Any]],
    matcher: _ShowMatcher,
    parsed_cache: Dict[str, Dict[str, Any]],
) -> Optional[int]:
    """The highest episode number among the show's matching releases in a feed."""
    newest: Optional[int] = None
    for article in articles:
        title = article.get("title", "")
        if not title:
            continue
        matched, parsed = _decide(show, title, matcher, parsed_cache)
        raw_episode = parsed.get("episode")
        if matched and raw_episode is not None and raw_episode > 0 and (newest is None or raw_episode > newest):
            newest = int(raw_episode)
    return newest


def _mark_missed_episodes(
    session: Session,
    show: Monitored,
    episodes: List[Episode],
    now: datetime,
    air_horizon: datetime,
    latest_aired: Optional[int],
    covered: Collection[int] = (),
) -> List[str]:
    """Mark aired WANTED episodes that the feeds can no longer deliver as MISSED.

    A WANTED episode that is older than the latest aired one, or followed by a
    downloaded one, fell out of the RSS cache. The latest aired one is given
    MISSED_AFTER_HOURS to show up first. An episode a cached item can still
    supply (``covered``) is never missed. It may come back through a search
    of the feeds' sites, which is why MISSED stays grabbable.
    """
    logs: List[str] = []
    for episode in episodes:
        if episode.status != EpisodeStatus.WANTED:
            continue
        if episode.episode_number in covered:
            continue
        if episode.retry_after and as_utc(episode.retry_after) > now:
            # Requeued moments ago (e.g. its torrent was removed); give
            # the retry its chance before calling the episode missed.
            continue
        episode_air_at = as_utc(episode.air_at)
        # An episode MUST have reached its air time to ever be considered missed.
        # Future scheduled episodes must remain WANTED.
        if episode_air_at is not None:
            has_aired = episode_air_at <= air_horizon
        else:
            has_aired = latest_aired is not None and episode.episode_number <= latest_aired
        if not has_aired:
            continue

        is_older_than_latest = (
            latest_aired is not None
            and episode.episode_number < latest_aired
        )
        has_newer_downloaded = any(
            e.episode_number > episode.episode_number and e.status in _RELEASE_HOLDING
            for e in episodes
        )
        is_latest_and_overdue = (
            episode_air_at is not None
            and latest_aired is not None
            and episode.episode_number == latest_aired
            and now - episode_air_at > timedelta(hours=MISSED_AFTER_HOURS)
        )
        if is_older_than_latest or has_newer_downloaded or is_latest_and_overdue:
            episode.status = EpisodeStatus.MISSED
            session.add(episode)
            msg = f"{show.display_name} Ep {episode.episode_number} missed: not in the feed."
            logs.append(msg)
            logger.info(msg)
    return logs


class _GrabContext(NamedTuple):
    episodes: List[Episode]
    episodes_by_number: Dict[int, Episode]
    failed_versions: Dict[int, set]
    target_count: int
    latest_aired: Optional[int]


def _show_grab_context(
    session: Session,
    show: Monitored,
    now: datetime,
    air_horizon: datetime,
) -> _GrabContext:
    episodes = sync_show_episodes(session, show)
    episodes_by_number = {episode.episode_number: episode for episode in episodes}
    failed_versions: Dict[int, set] = {}
    episode_ids = [episode.id for episode in episodes if episode.id]
    if episode_ids:
        failed_operations = session.exec(
            select(TorrentOperation).where(
                TorrentOperation.episode_id.in_(episode_ids),
                TorrentOperation.status == TorrentOperationStatus.FAILED,
            )
        ).all()
        for failed_operation in failed_operations:
            failed_versions.setdefault(failed_operation.episode_id, set()).add(failed_operation.version)
    canonical_max = max((episode.episode_number for episode in episodes), default=0)
    target_count = show.total_episodes or max(show.next_airing_episode or 0, canonical_max, 1)
    known_aired = [
        episode.episode_number
        for episode in episodes
        if episode.air_at and as_utc(episode.air_at) <= air_horizon
    ]
    latest_aired = max(known_aired) if known_aired else None
    airing_at = as_utc(show.next_airing_at)
    if latest_aired is None and show.next_airing_episode:
        latest_aired = show.next_airing_episode if airing_at and airing_at <= now else max(0, show.next_airing_episode - 1)
    return _GrabContext(episodes, episodes_by_number, failed_versions, target_count, latest_aired)


def _resolve_feed_articles(
    session: Session,
    show: Monitored,
    feed: Feed,
    feed_articles: List[Dict[str, Any]],
    matcher: _ShowMatcher,
    parsed_cache: Dict[str, Dict[str, Any]],
    latest_aired: Optional[int],
    target_count: int,
    episodes_by_number: Dict[int, Episode],
    failed_versions: Dict[int, set],
    first_air: Optional[datetime],
    tolerance_hours: int,
    learn: bool = True,
) -> Tuple[List[Tuple[Any, ...]], Dict[int, int]]:
    """Which episode each of a feed's articles is for, and the newest usable version of each.

    The one place that decides whether an article is this show's release of one of
    its episodes: the grab loop acts on the result, and ``episodes_in_cache`` asks
    the same question without acting. ``learn=False`` keeps it read-only.
    """
    resolved: List[Tuple[Any, ...]] = []
    best_version: Dict[int, int] = {}
    matcher = _matcher_for_feed(matcher, show, feed, episodes_by_number.values())
    newest_raw = _newest_raw_episode(show, feed_articles, matcher, parsed_cache)
    for article in feed_articles:
        title = article.get("title", "")
        if not title:
            continue
        matched, parsed = _decide_article(session, show, article, matcher, parsed_cache, target_count, tolerance_hours)
        if not matched:
            continue
        raw_episode = parsed.get("episode")
        if raw_episode is None and target_count == 1:
            # A movie's release carries no episode number; its one episode is the whole release.
            raw_episode = 1
        if raw_episode is None or raw_episode <= 0:
            continue
        episode_number = _mapped_episode(
            session,
            show,
            feed,
            int(raw_episode),
            latest_aired,
            target_count,
            article,
            learn=learn,
            newest_raw=newest_raw,
            tolerance_hours=tolerance_hours,
        )
        if episode_number is None:
            continue
        episode = episodes_by_number.get(episode_number)
        if not episode:
            continue
        if _published_before_air(article, episode, show, tolerance_hours, first_air):
            logger.debug(
                f"'{show.display_name}': '{title}' was published before episode "
                f"{episode_number} aired; it belongs to an earlier season."
            )
            continue
        version = int(parsed.get("version") or 1)
        resolved.append((article, title, parsed, raw_episode, episode_number, episode, version))
        if version not in failed_versions.get(episode.id, set()):
            best_version[episode_number] = max(best_version.get(episode_number, 0), version)
    return resolved, best_version


def episodes_in_cache(
    session: Session,
    show: Monitored,
    feeds: List[Feed],
    articles_by_url: Dict[str, List[Dict[str, Any]]],
    settings: Settings,
    now: Optional[datetime] = None,
) -> Set[int]:
    """The show's episodes that the cached feed items can already supply.

    Read-only. These are the episodes the next check would take from the cache, so
    searching the feeds' sites for them would only find what is already here.
    """
    now = now or utc_now()
    tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
    episodes, episodes_by_number, failed_versions, target_count, latest_aired = _show_grab_context(
        session, show, now, _air_horizon(show, now, tolerance_hours),
    )
    first_air = min((episode.air_at for episode in episodes if episode.air_at), default=None)
    matcher = _show_matcher(show)
    parsed_cache: Dict[str, Dict[str, Any]] = {}
    covered: Set[int] = set()
    for feed in _grab_feeds(session, show, feeds):
        _, best_version = _resolve_feed_articles(
            session, show, feed, articles_by_url.get(feed.qbit_feed_url, []), matcher, parsed_cache,
            latest_aired, target_count, episodes_by_number, failed_versions,
            first_air, tolerance_hours, learn=False,
        )
        covered.update(best_version)
    return covered


def evaluate_and_grab_releases(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    feeds: Optional[List[Feed]] = None,
    mode: str = "direct",
    rss_snapshot: Optional[RssSnapshot] = None,
) -> List[str]:
    logs: List[str] = []
    if mode == "direct":
        logs.extend(update_episode_status(session, qbit, settings))
    if feeds is None:
        feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    if not feeds:
        return logs
    try:
        articles_by_url = rss_snapshot.get() if rss_snapshot else flatten_rss_articles(qbit.get_rss_items(with_data=True))
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS items for direct grab: {e}")
        raise
    if not isinstance(rss_snapshot, CachedRssSnapshot):
        # A cached snapshot is made of rows that are already stored; only a live read has articles to add.
        _sync_seen_items(session, feeds, articles_by_url)
    shows = session.exec(select(Monitored).where(Monitored.status.in_([
        MonitoredStatus.UNCONFIRMED,
        MonitoredStatus.FIXED,
        MonitoredStatus.STALLED,
    ]))).all()
    now = utc_now()
    tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
    parsed_cache: Dict[str, Dict[str, Any]] = {}
    for show in shows:
        if show.learned_feed_id is None and library.is_held(show.id):
            continue
        matcher = _show_matcher(show)
        air_horizon = _air_horizon(show, now, tolerance_hours)
        episodes, episodes_by_number, failed_versions, target_count, latest_aired = _show_grab_context(
            session, show, now, air_horizon,
        )
        airing_at = as_utc(show.next_airing_at)
        if show.next_airing_episode == 1 and airing_at and airing_at > air_horizon:
            continue
        first_air = min((episode.air_at for episode in episodes if episode.air_at), default=None)
        newest_wanted = max(
            (
                episode.episode_number
                for episode in episodes
                if episode.status == EpisodeStatus.WANTED
                and (latest_aired is None or episode.episode_number <= latest_aired)
            ),
            default=None,
        )
        candidate_feeds = _grab_feeds(session, show, feeds)
        # A show with no feed at all is being discovered. A release on the
        # highest-ranked feed is taken straight away; one on a lower-ranked feed
        # is only adopted after a grace window, so a better-ranked feed still
        # gets its chance to post the episode first. Nothing is grabbed while
        # the window is open: waiting costs a cycle, grabbing the wrong feed
        # costs the whole season.
        discovering = show.current_feed_id is None and show.learned_feed_id is None
        top_feed = candidate_feeds[0] if candidate_feeds else None
        grabbed_from: Optional[int] = None
        has_feed_articles = any(articles_by_url.get(feed.qbit_feed_url) for feed in candidate_feeds)
        feeds_to_read = candidate_feeds
        if discovering and len(candidate_feeds) > 1:
            # Only the best-ranked feed that carries a release for a wanted episode takes
            # part in the grace window. Two lower-ranked feeds carrying the same release
            # would otherwise take the single candidate slot from each other every cycle,
            # and neither would ever finish its window.
            for feed in candidate_feeds:
                _, carried = _resolve_feed_articles(
                    session, show, feed, articles_by_url.get(feed.qbit_feed_url, []), matcher, parsed_cache,
                    latest_aired, target_count, episodes_by_number, failed_versions,
                    first_air, tolerance_hours, learn=False,
                )
                if any(
                    episodes_by_number[number].status in (EpisodeStatus.WANTED, EpisodeStatus.MISSED)
                    for number in carried
                ):
                    feeds_to_read = [feed]
                    break
        for feed in feeds_to_read:
            feed_articles = articles_by_url.get(feed.qbit_feed_url, [])
            if grabbed_from is not None:
                break
            # Resolve every article first so a refresh carrying v1 and v2 of the
            # same episode only grabs the newest, instead of adding v1 and then
            # replacing it a moment later. A version that already failed is not
            # a candidate, so it cannot hide a lower one that would still work.
            resolved, best_version = _resolve_feed_articles(
                session, show, feed, feed_articles, matcher, parsed_cache,
                latest_aired, target_count, episodes_by_number, failed_versions,
                first_air, tolerance_hours,
            )
            for article, title, parsed, raw_episode, episode_number, episode, version in resolved:
                if version < best_version.get(episode_number, version):
                    continue
                episode_air_at = as_utc(episode.air_at)
                is_upgrade = version > episode.version
                # The whole season is eligible however long ago an episode aired; the date
                # guards above keep earlier seasons out. An episode with no air time at all
                # cannot be placed in the season, so only the newest wanted one is taken.
                if not is_upgrade and episode_air_at is None:
                    if newest_wanted is not None and episode_number < newest_wanted:
                        continue
                # An undated item cannot be checked against the season, so it only fills
                # an episode that aired recently.
                if (
                    not is_upgrade
                    and episode_air_at is not None
                    and parse_article_date(article).year <= 2000
                    and now - episode_air_at > timedelta(days=HUNTING_RECENT_DAYS)
                ):
                    continue
                if episode.retry_after and as_utc(episode.retry_after) > now:
                    continue
                if version in failed_versions.get(episode.id, set()):
                    continue
                if episode.status == EpisodeStatus.FAILED and version <= episode.version:
                    continue
                if episode.status in {EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING} and version <= episode.version:
                    continue
                if episode.status == EpisodeStatus.QUEUED and version <= episode.version:
                    continue
                if mode != "direct":
                    continue
                if discovering and not _discovery_window_open(
                    session, show, feed, top_feed, article, now
                ):
                    continue
                _record_mapping_evidence(session, show, feed, int(raw_episode), episode_number)
                operation = _start_operation(session, qbit, settings, show, feed, episode, article, parsed, version)
                if operation:
                    grabbed_from = feed.id
                    if operation.kind == "replace":
                        logs.append(f"Queued replacement for {show.display_name} Ep {episode_number} v{version}: {title}")
                    else:
                        logs.append(f"Queued {show.display_name} Ep {episode_number} v{version}: {title}")
        if mode == "direct" and has_feed_articles:
            # Only needed when a wanted episode could be marked; skip the extra feed pass otherwise.
            covered = (
                episodes_in_cache(session, show, feeds, articles_by_url, settings, now=now)
                if any(
                    episode.status == EpisodeStatus.WANTED
                    and (episode.air_at is None or as_utc(episode.air_at) <= air_horizon)
                    for episode in episodes
                )
                else ()
            )
            logs.extend(_mark_missed_episodes(
                session, show, episodes, now, air_horizon, latest_aired, covered,
            ))
        if grabbed_from is not None and show.current_feed_id != grabbed_from:
            # The feed that produced the release becomes the assignment, and the
            # grab itself marks it learned, so from here on it is the only feed
            # this show is read from.
            show.current_feed_id = grabbed_from
            session.add(show)
        session.commit()
    return logs


def _is_restore(episode: Episode) -> bool:
    """A downloaded episode Kisetsu holds no torrent for, so it cannot manage it."""
    return episode.status == EpisodeStatus.COMPLETED and not episode.torrent_hash


def _other_source(episode: Episode, feed: Feed, article: Dict[str, Any]) -> bool:
    """Whether ``article`` is another copy of a finished, managed episode.

    That is a different feed than the one the episode came from, or, when the
    feed was not recorded, a release that is not the one already downloaded.
    """
    if episode.status != EpisodeStatus.COMPLETED or not episode.torrent_hash:
        return False
    info_hash = str(article.get("infoHash") or "").lower()
    if info_hash and info_hash == episode.torrent_hash.lower():
        return False
    if episode.feed_id is not None:
        return episode.feed_id != feed.id
    return not (episode.release_title and same_release(episode.release_title, article.get("title", "")))


def _manual_refusal(
    session: Session,
    episode: Episode,
    version: int,
    other_source: bool = False,
) -> Optional[str]:
    """Why an explicit grab of ``version`` of ``episode`` must not go ahead."""
    if _operation_for_episode(session, episode.id):
        return f"Episode {episode.episode_number} already has a download in progress."
    if _is_restore(episode):
        return None
    if other_source:
        if episode.status == EpisodeStatus.COMPLETED and version < episode.version:
            return f"Episode {episode.episode_number} already has v{episode.version}; an older version won't replace it."
        return None
    if episode.status in {
        EpisodeStatus.COMPLETED,
        EpisodeStatus.DOWNLOADING,
        EpisodeStatus.REPLACING,
        EpisodeStatus.QUEUED,
    } and version <= episode.version:
        return f"Episode {episode.episode_number} v{episode.version} is already downloaded or queued."
    return None


def _manual_candidates(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    feeds: Optional[List[Feed]] = None,
) -> List[Tuple[Feed, Dict[str, Any], Dict[str, Any], int, int, int, Episode, _GrabContext]]:
    """Every feed article the grabber would accept for ``show``, with its episode.

    Reads only the feeds the show may use (so the feed lock is honoured) and never
    writes: the mapping is resolved with ``learn=False``. ``feeds`` replaces that
    set, for listing what other feeds carry; nothing is ever grabbed from them.
    """
    if feeds is None:
        all_feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
        candidate_feeds = _grab_feeds(session, show, all_feeds)
    else:
        candidate_feeds = feeds
    if not candidate_feeds:
        return []
    if articles_by_url is None:
        articles_by_url = cached_articles(session)
    now = utc_now()
    tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
    context = _show_grab_context(session, show, now, _air_horizon(show, now, tolerance_hours))
    found = []
    matcher = _show_matcher(show)
    parsed_cache: Dict[str, Dict[str, Any]] = {}
    base_matcher = matcher
    for feed in candidate_feeds:
        matcher = _matcher_for_feed(base_matcher, show, feed, context.episodes)
        feed_articles = articles_by_url.get(feed.qbit_feed_url, [])
        newest_raw = _newest_raw_episode(show, feed_articles, matcher, parsed_cache)
        for article in feed_articles:
            title = article.get("title", "")
            if not title:
                continue
            matched, parsed = _decide_article(session, show, article, matcher, parsed_cache, context.target_count, tolerance_hours)
            if not matched:
                continue
            raw_episode = parsed.get("episode")
            if raw_episode is None or raw_episode <= 0:
                continue
            episode_number = _mapped_episode(
                session, show, feed, int(raw_episode), context.latest_aired, context.target_count, article,
                learn=False, newest_raw=newest_raw, tolerance_hours=tolerance_hours,
            )
            episode = context.episodes_by_number.get(episode_number) if episode_number is not None else None
            if episode is None:
                continue
            version = int(parsed.get("version") or 1)
            found.append((feed, article, parsed, int(raw_episode), episode_number, version, episode, context))
    return found


def direct_feed_matches(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    limit: int = 30,
    articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """Releases the direct engine recognises, grouped by feed, for the UI.

    The feeds the show may read come first, each with a ``role`` (``locked``,
    ``picked``, ``assigned`` or ``discovery``) and its releases ready to download.
    Other feeds that carry releases follow as ``other`` groups, listed for
    information only: nothing is offered to download from them, because that
    would bypass the feed lock. Feeds with no releases are left out.
    """
    feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    if articles_by_url is None:
        articles_by_url = cached_articles(session)
    own = _grab_feeds(session, show, feeds)
    own_ids = {feed.id for feed in own}

    def role_of(feed: Feed) -> str:
        if feed.id == show.learned_feed_id:
            return "locked"
        if show.feed_pinned and feed.id == show.current_feed_id:
            return "picked"
        return "assigned" if feed.id == show.current_feed_id else "discovery"

    groups: Dict[int, Dict[str, Any]] = {}

    def add(feed: Feed, role: str, usable: bool, candidates) -> None:
        for _feed, article, _parsed, _raw, episode_number, version, episode, _context in candidates:
            group = groups.setdefault(feed.id, {
                "feed_id": feed.id, "feed_name": feed.qbit_feed_name, "role": role, "usable": usable, "matches": [],
            })
            if len(group["matches"]) >= limit:
                break
            title = article.get("title", "")
            if usable:
                replaces = _other_source(episode, feed, article)
                refusal = _manual_refusal(session, episode, version, replaces)
            else:
                replaces, refusal = False, "other feed"
            info_hash = str(article.get("infoHash") or "").lower()
            # Once the release is downloaded, name it as the ledger (and qBittorrent) does.
            is_this_release = bool(episode.release_title) and (
                bool(info_hash and episode.torrent_hash and episode.torrent_hash.lower() == info_hash)
                or same_release(episode.release_title, title)
            )
            group["matches"].append({
                "title": title,
                "display_title": episode.release_title if is_this_release else title,
                "feed_id": feed.id,
                "feed_name": feed.qbit_feed_name,
                "episode": episode_number,
                "version": version,
                "episode_status": episode.status.value,
                "restore": usable and _is_restore(episode),
                "replaces": bool(replaces) and refusal is None,
                "downloadable": refusal is None,
            })

    for feed, *rest in _manual_candidates(session, qbit, settings, show, articles_by_url):
        add(feed, role_of(feed), True, [(feed, *rest)])
    for feed in feeds:
        if feed.id not in own_ids:
            add(feed, "other", False, _manual_candidates(session, qbit, settings, show, articles_by_url, feeds=[feed]))
    # A feed qBittorrent could not read is dropped from the cache, so it would simply be
    # missing here. Say so for the feeds the show uses instead of leaving a silent gap.
    failed_names = set(last_failed_feed_names())
    for feed in own:
        if feed.qbit_feed_name in failed_names and feed.id not in groups:
            groups[feed.id] = {
                "feed_id": feed.id, "feed_name": feed.qbit_feed_name, "role": role_of(feed),
                "usable": True, "unavailable": True, "matches": [],
            }
    return [g for g in groups.values() if g["feed_id"] in own_ids] + [
        g for g in groups.values() if g["feed_id"] not in own_ids
    ]


def restore_episode(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    episode: Episode,
) -> str:
    """Bring a finished episode with no tracked torrent under management.

    The torrent is adopted if qBittorrent still holds it, otherwise the release
    is added again from the feed. Either way the ledger ends up with its hash.
    """
    if not _is_restore(episode):
        raise ValueError(f"Episode {episode.episode_number} is already managed.")
    if _operation_for_episode(session, episode.id):
        raise ValueError(f"Episode {episode.episode_number} already has a download in progress.")
    title = (episode.release_title or "").strip()
    if not title:
        raise ValueError(f"No release is recorded for episode {episode.episode_number}.")
    torrent = _find_torrent_by_name(qbit, title)
    if torrent is not None:
        torrent_hash = _torrent_hash(torrent)
        qbit.add_torrent_tags([torrent_hash], [MANAGED_TAG])
        episode.torrent_hash = torrent_hash
        episode.status = EpisodeStatus.COMPLETED if is_seeding_torrent(torrent) else EpisodeStatus.DOWNLOADING
        episode.last_error = None
        session.add(episode)
        session.commit()
        return f"Restored {show.display_name} Ep {episode.episode_number}: now managed."
    try:
        return manual_grab(session, qbit, settings, show, title, fuzzy=True, expect_episode=episode.episode_number)
    except ValueError as e:
        raise ValueError(f"Not in qBittorrent. {e}") from e


def manual_grab(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    title: str,
    fuzzy: bool = False,
    expect_episode: Optional[int] = None,
) -> str:
    """Queue one named release through the normal operation path.

    This is an explicit user action, so the backfill and discovery-grace windows
    do not apply: a MISSED or FAILED episode that is still in the feed may be
    fetched again. Everything else (ledger, tags, v2 replacement, feed lock)
    goes through ``_start_operation`` exactly like a cycle grab.

    With ``fuzzy`` the title may be the release under another name (a torrent's
    file name rather than the feed title); the one feed item that is the same
    release is used.
    """
    if show.status == MonitoredStatus.PAUSED:
        raise ValueError("This show is paused. Resume it before downloading.")
    wanted_title = title.strip()
    candidates = _manual_candidates(session, qbit, settings, show)
    candidate = next((item for item in candidates if item[1].get("title") == wanted_title), None)
    if candidate is None and fuzzy:
        near = [item for item in candidates if same_release(item[1].get("title", ""), wanted_title)]
        if len(near) > 1:
            raise ValueError("Several releases in the feed match that name; pick one from the In feed tab.")
        candidate = near[0] if near else None
    if candidate is None:
        raise ValueError(
            "That release is not in this show's feed any more, or does not match this show."
        )
    feed, article, parsed, raw_episode, episode_number, version, episode, _context = candidate
    if expect_episode is not None and episode_number != expect_episode:
        raise ValueError(f"The matching release in the feed is episode {episode_number}, not episode {expect_episode}.")
    wanted_title = article.get("title", wanted_title)
    switch_source = _other_source(episode, feed, article)
    refusal = _manual_refusal(session, episode, version, switch_source)
    if refusal:
        raise ValueError(refusal)
    _record_mapping_evidence(session, show, feed, raw_episode, episode_number)
    operation = _start_operation(
        session, qbit, settings, show, feed, episode, article, parsed, version, switch_source=switch_source,
    )
    if operation is None:
        raise ValueError(episode.last_error or "Could not start the download for that release.")
    if show.current_feed_id != feed.id:
        show.current_feed_id = feed.id
        session.add(show)
        session.commit()
    if operation.kind == "replace":
        action = "replacement"
    elif _is_restore_operation(operation):
        action = "restore"
    else:
        action = "download"
    return f"Manually queued {action} for {show.display_name} Ep {episode_number} v{version}: {wanted_title}"
