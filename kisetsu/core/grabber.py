import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from uuid import uuid4

from sqlmodel import Session, select

from kisetsu.clients.qbit import QBitClient, QbitClientError
from kisetsu.core.confirmation import record_match_event
from kisetsu.core.discovery import RssSnapshot, flatten_rss_articles, parse_article_date
from kisetsu.core.matching import match_release_to_show, parse_release_title
from kisetsu.core.rules import effective_title, resolve_save_path
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
    SeenFeedItem,
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
_INACTIVE_SHOW_STATUSES = (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED)


def _operation_tag() -> str:
    return f"qsa-op-{uuid4().hex}"


def _episode_tags(show: Monitored, operation_tag: str) -> List[str]:
    # Both are queried: qsa-managed finds our torrents, qsa-show-<id> scopes
    # pausing and resuming to one show.
    return [
        "qsa-managed",
        f"qsa-show-{show.id}",
        operation_tag,
    ]


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
        for torrent in qbit.get_torrents(tag="qsa-managed"):
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


def _fail_operation(session: Session, operation: TorrentOperation, episode: Episode, error: str) -> None:
    operation.status = TorrentOperationStatus.FAILED
    operation.last_error = error
    operation.updated_at = utc_now()
    if operation.kind == "replace":
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


def _sync_seen_items(
    session: Session,
    feeds: List[Feed],
    articles_by_url: Dict[str, List[Dict[str, Any]]],
) -> Dict[Tuple[str, str], datetime]:
    first_seen: Dict[Tuple[str, str], datetime] = {}
    now = utc_now()
    changed = False
    for feed in feeds:
        articles_by_id: Dict[str, Dict[str, Any]] = {}
        for article in articles_by_url.get(feed.qbit_feed_url, []):
            if not article.get("title"):
                continue
            item_id = str(
                article.get("id")
                or article.get("torrentURL")
                or article.get("link")
                or article.get("title", "")
            )
            articles_by_id.setdefault(item_id, article)
        if not articles_by_id:
            continue
        rows = session.exec(
            select(SeenFeedItem).where(
                SeenFeedItem.feed_url == feed.qbit_feed_url,
                SeenFeedItem.item_id.in_(list(articles_by_id)),
            )
        ).all()
        known = {row.item_id: row.created_at for row in rows}
        for item_id, article in articles_by_id.items():
            first_seen[(feed.qbit_feed_url, item_id)] = known.get(item_id, now)
            if item_id not in known:
                session.add(SeenFeedItem(
                    feed_url=feed.qbit_feed_url,
                    item_id=item_id,
                    title=article.get("title", ""),
                ))
                known[item_id] = now
                changed = True
    if changed:
        session.commit()
    return first_seen


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
    expected = episode.release_title.strip().casefold()
    for torrent in _managed_torrents(qbit, settings):
        name = str(getattr(torrent, "name", "") or "").strip().casefold()
        if name == expected:
            return _torrent_hash(torrent)
    return None


def _is_healthy_torrent(torrent: Any) -> bool:
    if _torrent_state(torrent) in _FAILED_TORRENT_STATES:
        return False
    return _torrent_progress(torrent) > 0


def _operation_retry(session: Session, operation: TorrentOperation, episode: Episode, error: str, minutes: int = 5) -> None:
    operation.last_error = error
    operation.next_retry_at = utc_now() + timedelta(minutes=minutes)
    operation.updated_at = utc_now()
    episode.last_error = error
    episode.retry_after = operation.next_retry_at
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


def _delete_torrent_by_hash(qbit: QBitClient, torrent_hash: Optional[str]) -> None:
    if not torrent_hash:
        return
    try:
        existing = list(qbit.get_torrents(hashes=[torrent_hash]))
    except Exception as e:
        raise QbitClientError(f"Could not read torrent {torrent_hash}: {e}") from e
    if not existing:
        return
    qbit.pause_torrents([torrent_hash])
    qbit.delete_torrents([torrent_hash], delete_files=True)


def _content_path(torrent: Any) -> Optional[str]:
    value = getattr(torrent, "content_path", None)
    return str(value) if isinstance(value, str) and value else None


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
            state = _torrent_state(torrent)
            if state in _FAILED_TORRENT_STATES:
                _operation_retry(session, operation, episode, f"Replacement torrent state: {state}", minutes=15)
                return None
            if not _is_healthy_torrent(torrent):
                _operation_retry(session, operation, episode, "Replacement is rechecking; the previous torrent remains untouched.")
                return None
            if not is_seeding_torrent(torrent):
                operation.status = TorrentOperationStatus.SEEDING
                _operation_retry(session, operation, episode, "Replacement is downloading; the previous torrent is kept until it finishes.")
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
                _remove_superseded_torrent(qbit, operation, _operation_torrent(qbit, operation))
            except Exception as e:
                _operation_retry(session, operation, episode, str(e))
                return None

    _set_episode_release(session, episode, operation, operation.new_torrent_hash, EpisodeStatus.DOWNLOADING)
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
            matched_regex=show.custom_regex,
        )
        session.commit()
    return f"Added '{show_name(episode, session)}' Ep {episode.episode_number} v{operation.version}"


def show_name(episode: Episode, session: Session) -> str:
    show = session.get(Monitored, episode.monitored_id)
    return show.display_name if show else str(episode.monitored_id)


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


def _pause_show_torrents(qbit: QBitClient, show: Monitored) -> None:
    if not show.id:
        return
    try:
        torrents = list(qbit.get_torrents(tag=f"qsa-show-{show.id}"))
    except Exception:
        return
    hashes = [str(t.hash) for t in torrents if getattr(t, "hash", None)]
    if hashes:
        try:
            qbit.pause_torrents(hashes)
        except Exception:
            pass


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
        if show is not None and show.status in _INACTIVE_SHOW_STATUSES:
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
                        _fail_operation(
                            session,
                            operation,
                            episode,
                            f"Gave up after {operation.attempt_count} attempts: "
                            f"{operation.last_error or 'qBittorrent never exposed the tagged torrent.'}",
                        )
                        logs.append(
                            f"Give up on '{show.display_name if show else 'show'}' Ep "
                            f"{episode.episode_number}: {operation.last_error}"
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
            if name.casefold() != episode.release_title.strip().casefold():
                continue
            episode.torrent_hash = _torrent_hash(torrent)
            episode.release_title = name
            episode.status = EpisodeStatus.COMPLETED if is_seeding_torrent(torrent) else EpisodeStatus.DOWNLOADING
            episode.last_error = None
            session.add(episode)
            break
    episodes = session.exec(select(Episode).where(Episode.status.in_([
        EpisodeStatus.QUEUED,
        EpisodeStatus.DOWNLOADING,
        EpisodeStatus.REPLACING,
    ]))).all()
    for episode in episodes:
        torrent = by_hash.get(episode.torrent_hash)
        if not torrent:
            if episode.status == EpisodeStatus.REPLACING:
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
                logs.append(f"Completed {show_name(episode, session)} Ep {episode.episode_number}")
        elif _torrent_state(torrent) in _FAILED_TORRENT_STATES:
            episode.status = EpisodeStatus.FAILED
            episode.last_error = f"qBittorrent torrent state: {_torrent_state(torrent)}"
            session.add(episode)
    logs.extend(_release_stale_operation_tags(session, qbit))
    session.commit()
    return logs


def _release_stale_operation_tags(session: Session, qbit: QBitClient) -> List[str]:
    """
    Remove per-operation tags left behind before the tag was released on success.

    Earlier builds kept the random ``qsa-op-<uuid>`` tag on every torrent
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
        logs.append(
            f"Removed leftover operation tag from {show_name(episode, session)} "
            f"Ep {episode.episode_number}"
        )
    return logs


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
    reference = published_at if published_at > datetime.min.replace(tzinfo=timezone.utc) else now

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
    # (allowing a small early air tolerance of 2 hours for timezone/stream variations).
    early_air_tolerance = timedelta(hours=2)
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
) -> Optional[int]:
    """Map a feed's raw episode number to the show's local one.

    ``learn=False`` is for read-only previews: it skips the offset inference
    that can renumber stored releases.
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

    by_date = _date_mapped_episode(session, show, raw_episode, target_count, article)
    if by_date is not None:
        if learn:
            _infer_offset_from_conflict(session, show, feed, raw_episode, by_date, latest_aired, target_count)
        return by_date
    if latest_aired is not None and raw_episode > target_count and 1 <= latest_aired <= target_count:
        return latest_aired
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
    if operation.kind == "replace":
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
    try:
        qbit.add_torrent(
            urls=operation.new_torrent_url,
            save_path=save_path,
            category=settings.default_category,
            tags=_episode_tags(show, operation.operation_tag),
            is_paused=operation.kind == "replace",
            ratio_limit=settings.default_seed_ratio,
        )
    except Exception as e:
        _set_operation_retry(
            session,
            operation,
            episode,
            str(e),
            utc_now() + timedelta(minutes=15),
            TorrentOperationStatus.UNKNOWN,
            True,
        )
        return
    # The feed that just delivered a release is now proven for this show. It is
    # recorded as learned so nothing downstream may quietly move the show
    # somewhere else on the strength of a heuristic.
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
    torrent = _find_tagged(qbit, operation.operation_tag)
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
    is_replace = (version > previous_version) and has_existing
    old_torrent_hash = _find_old_hash(qbit, settings, show, episode) if is_replace else None
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


def cancel_episode_operations(
    session: Session,
    qbit: QBitClient,
    show: Monitored,
    episode: Episode,
    reason: str,
) -> int:
    """Cancel in-flight operations for an episode and dispose of new torrents.

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
        if operation.new_torrent_hash and operation.new_torrent_hash != operation.old_torrent_hash:
            try:
                _delete_torrent_by_hash(qbit, operation.new_torrent_hash)
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
        if operation.kind == "replace":
            episode.retry_after = None
        else:
            episode.status = EpisodeStatus.WANTED
            episode.torrent_hash = None
            episode.retry_after = None
        session.add(operation)
        session.add(episode)
        session.commit()
        canceled += 1
    if canceled and show.status == MonitoredStatus.PAUSED:
        _pause_show_torrents(qbit, show)
    return canceled


def _decide(show: Monitored, title: str) -> Tuple[bool, Dict[str, Any]]:
    custom = (show.custom_regex or "").strip()
    if custom:
        try:
            if re.search(custom, title, re.IGNORECASE):
                excluded = show.custom_must_not or ""
                if excluded and re.search(excluded, title, re.IGNORECASE):
                    return False, {}
                return True, parse_release_title(title)
        except re.error:
            pass
    matched, _, parsed = match_release_to_show(title, show.aliases, ignore_arc_marker=False)
    if matched and show.custom_must_not:
        try:
            if re.search(show.custom_must_not, title, re.IGNORECASE):
                return False, {}
        except re.error:
            pass
    return matched, parsed


def _mark_missed_episodes(
    session: Session,
    show: Monitored,
    episodes: List[Episode],
    now: datetime,
    air_horizon: datetime,
    latest_aired: Optional[int],
    backfill_window_days: int,
) -> List[str]:
    """Mark aired WANTED episodes that the feeds can no longer deliver as MISSED.

    A WANTED episode that is stale, older than the latest aired one, or followed
    by a downloaded one fell out of the RSS cache and will not come back.
    """
    logs: List[str] = []
    for episode in episodes:
        if episode.status != EpisodeStatus.WANTED:
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

        is_stale_air = (
            episode_air_at is not None
            and now - episode_air_at > timedelta(days=backfill_window_days)
        )
        is_older_than_latest = (
            latest_aired is not None
            and episode.episode_number < latest_aired
        )
        has_newer_downloaded = any(
            e.episode_number > episode.episode_number and e.status in _RELEASE_HOLDING
            for e in episodes
        )
        if is_stale_air or is_older_than_latest or has_newer_downloaded:
            episode.status = EpisodeStatus.MISSED
            session.add(episode)
            msg = f"{show.display_name} Ep {episode.episode_number} was not found in RSS feed; marked as missed."
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
    _sync_seen_items(session, feeds, articles_by_url)
    shows = session.exec(select(Monitored).where(Monitored.status.in_([
        MonitoredStatus.UNCONFIRMED,
        MonitoredStatus.FIXED,
        MonitoredStatus.STALLED,
    ]))).all()
    now = utc_now()
    backfill_window_days = max(0, int(settings.backfill_window_days))
    tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
    air_horizon = now + timedelta(hours=tolerance_hours)
    for show in shows:
        episodes, episodes_by_number, failed_versions, target_count, latest_aired = _show_grab_context(
            session, show, now, air_horizon,
        )
        airing_at = as_utc(show.next_airing_at)
        if show.next_airing_episode == 1 and airing_at and airing_at > air_horizon:
            continue
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
        has_feed_articles = False
        for feed in candidate_feeds:
            feed_articles = articles_by_url.get(feed.qbit_feed_url, [])
            if feed_articles:
                has_feed_articles = True
            if grabbed_from is not None:
                break
            # Resolve every article first so a refresh carrying v1 and v2 of the
            # same episode only grabs the newest, instead of adding v1 and then
            # replacing it a moment later. A version that already failed is not
            # a candidate, so it cannot hide a lower one that would still work.
            resolved = []
            best_version: Dict[int, int] = {}
            for article in feed_articles:
                title = article.get("title", "")
                if not title:
                    continue
                matched, parsed = _decide(show, title)
                if not matched:
                    continue
                raw_episode = parsed.get("episode")
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
                )
                if episode_number is None:
                    continue
                episode = episodes_by_number.get(episode_number)
                if not episode:
                    continue
                version = int(parsed.get("version") or 1)
                resolved.append((article, title, parsed, raw_episode, episode_number, episode, version))
                if version not in failed_versions.get(episode.id, set()):
                    best_version[episode_number] = max(best_version.get(episode_number, 0), version)
            for article, title, parsed, raw_episode, episode_number, episode, version in resolved:
                if version < best_version.get(episode_number, version):
                    continue
                episode_air_at = as_utc(episode.air_at)
                is_upgrade = version > episode.version
                if not is_upgrade:
                    if episode_air_at is None:
                        if newest_wanted is not None and episode_number < newest_wanted:
                            continue
                    elif now - episode_air_at > timedelta(days=backfill_window_days):
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
            logs.extend(_mark_missed_episodes(
                session, show, episodes, now, air_horizon, latest_aired, backfill_window_days,
            ))
        if grabbed_from is not None and show.current_feed_id != grabbed_from:
            # The feed that produced the release becomes the assignment, and the
            # grab itself marks it learned, so from here on it is the only feed
            # this show is read from.
            show.current_feed_id = grabbed_from
            session.add(show)
        session.commit()
    return logs


def _manual_refusal(session: Session, episode: Episode, version: int) -> Optional[str]:
    """Why an explicit grab of ``version`` of ``episode`` must not go ahead."""
    if _operation_for_episode(session, episode.id):
        return f"Episode {episode.episode_number} already has a download in progress."
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
) -> List[Tuple[Feed, Dict[str, Any], Dict[str, Any], int, int, int, Episode, _GrabContext]]:
    """Every feed article the grabber would accept for ``show``, with its episode.

    Reads only the feeds the show may use (so the feed lock is honoured) and never
    writes: the mapping is resolved with ``learn=False``.
    """
    feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    candidate_feeds = _grab_feeds(session, show, feeds)
    if not candidate_feeds:
        return []
    if articles_by_url is None:
        articles_by_url = flatten_rss_articles(qbit.get_rss_items(with_data=True))
    now = utc_now()
    tolerance_hours = max(0, int(settings.early_air_tolerance_hours or 0))
    context = _show_grab_context(session, show, now, now + timedelta(hours=tolerance_hours))
    found = []
    for feed in candidate_feeds:
        for article in articles_by_url.get(feed.qbit_feed_url, []):
            title = article.get("title", "")
            if not title:
                continue
            matched, parsed = _decide(show, title)
            if not matched:
                continue
            raw_episode = parsed.get("episode")
            if raw_episode is None or raw_episode <= 0:
                continue
            episode_number = _mapped_episode(
                session, show, feed, int(raw_episode), context.latest_aired, context.target_count, article, learn=False,
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
    """Releases in the show's feed that the direct engine recognises, for the UI."""
    matches = []
    for feed, article, _parsed, _raw, episode_number, version, episode, _context in _manual_candidates(
        session, qbit, settings, show, articles_by_url,
    ):
        refusal = _manual_refusal(session, episode, version)
        matches.append({
            "title": article.get("title", ""),
            "feed_id": feed.id,
            "feed_name": feed.qbit_feed_name,
            "episode": episode_number,
            "version": version,
            "episode_status": episode.status.value,
            "downloadable": refusal is None,
        })
        if len(matches) >= limit:
            break
    return matches


def manual_grab(
    session: Session,
    qbit: QBitClient,
    settings: Settings,
    show: Monitored,
    title: str,
) -> str:
    """Queue one named release through the normal operation path.

    This is an explicit user action, so the backfill and discovery-grace windows
    do not apply: a MISSED or FAILED episode that is still in the feed may be
    fetched again. Everything else (ledger, tags, v2 replacement, feed lock)
    goes through ``_start_operation`` exactly like a cycle grab.
    """
    if show.status == MonitoredStatus.PAUSED:
        raise ValueError("This show is paused. Resume it before downloading.")
    wanted_title = title.strip()
    candidate = next(
        (item for item in _manual_candidates(session, qbit, settings, show) if item[1].get("title") == wanted_title),
        None,
    )
    if candidate is None:
        raise ValueError(
            "That release is not in this show's feed any more, or does not match this show."
        )
    feed, article, parsed, raw_episode, episode_number, version, episode, _context = candidate
    refusal = _manual_refusal(session, episode, version)
    if refusal:
        raise ValueError(refusal)
    _record_mapping_evidence(session, show, feed, raw_episode, episode_number)
    operation = _start_operation(session, qbit, settings, show, feed, episode, article, parsed, version)
    if operation is None:
        raise ValueError(episode.last_error or "Could not start the download for that release.")
    if show.current_feed_id != feed.id:
        show.current_feed_id = feed.id
        session.add(show)
        session.commit()
    action = "replacement" if operation.kind == "replace" else "download"
    return f"Manually queued {action} for {show.display_name} Ep {episode_number} v{version}: {wanted_title}"
