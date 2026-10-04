import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from sqlmodel import Session, select

from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.confirmation import record_match_event
from qbit_seasonal_anime.core.discovery import RssSnapshot, flatten_rss_articles, parse_article_date
from qbit_seasonal_anime.core.matching import match_release_to_show, parse_release_title
from qbit_seasonal_anime.core.rules import resolve_save_path
from qbit_seasonal_anime.db.models import (
    ACTIVE_OPERATION_STATUSES,
    CANCELLABLE_OPERATION_STATUSES,
    Episode,
    EpisodeMappingSource,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    GrabDecision,
    GrabDecisionType,
    Monitored,
    MonitoredStatus,
    SeenFeedItem,
    Settings,
    TorrentOperation,
    TorrentOperationStatus,
    normalize_mapping_source,
    utc_now,
)

logger = logging.getLogger("qbit_seasonal_anime.core.grabber")

AIR_DATE_TOLERANCE = timedelta(days=3)
_FAILED_TORRENT_STATES = {"error", "missingfiles", "unknown"}
MAX_OPERATION_ATTEMPTS = 8
_INACTIVE_SHOW_STATUSES = (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED)
# One-step exit ladder; each entry may be followed only by later entries.
_RESUME_SUCCESSOR: Dict[str, Optional[str]] = {
    TorrentOperationStatus.ADDED.name: TorrentOperationStatus.NEW_VERIFIED.name,
    TorrentOperationStatus.NEW_VERIFIED.name: TorrentOperationStatus.SEEDING.name,
    TorrentOperationStatus.SEEDING.name: TorrentOperationStatus.OLD_STOPPED.name,
    TorrentOperationStatus.OLD_STOPPED.name: None,
}


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _operation_tag() -> str:
    return f"qsa-op-{uuid4().hex}"


def _episode_tags(show: Monitored, episode: int, version: int, operation_tag: str) -> List[str]:
    return [
        "qsa-managed",
        f"qsa-show-{show.id}",
        f"qsa-ep-{episode}",
        f"qsa-v{version}",
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


def _is_seeding_torrent(torrent: Any) -> bool:
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
    episode.status = EpisodeStatus.FAILED
    episode.last_error = error
    episode.retry_after = None
    session.add(operation)
    session.add(episode)
    session.commit()


def _record_decision(
    session: Session,
    show: Monitored,
    episode: Optional[Episode],
    feed: Feed,
    article: Dict[str, Any],
    title: str,
    episode_number: int,
    version: int,
    decision: GrabDecisionType,
    reason: str,
) -> None:
    item_id = str(article.get("id") or article.get("torrentURL") or article.get("link") or title)
    existing = session.exec(
        select(GrabDecision).where(
            GrabDecision.monitored_id == show.id,
            GrabDecision.feed_url == feed.qbit_feed_url,
            GrabDecision.feed_item_id == item_id,
            GrabDecision.episode == episode_number,
            GrabDecision.version == version,
            GrabDecision.decision == decision,
        )
    ).first()
    if existing:
        return
    session.add(GrabDecision(
        monitored_id=show.id,
        episode_id=episode.id if episode else None,
        feed_url=feed.qbit_feed_url,
        feed_item_id=item_id,
        release_title=title,
        episode=episode_number,
        version=version,
        decision=decision,
        reason=reason,
    ))
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


def _stop_torrent_by_hash(qbit: QBitClient, torrent_hash: Optional[str]) -> None:
    if not torrent_hash:
        return
    try:
        existing = list(qbit.get_torrents(hashes=[torrent_hash]))
    except Exception as e:
        raise QbitClientError(f"Could not read torrent {torrent_hash}: {e}") from e
    if not existing:
        return
    qbit.stop_torrents([torrent_hash])


def _stop_superseded_torrent(qbit: QBitClient, operation: TorrentOperation) -> None:
    if not operation.old_torrent_hash or operation.old_torrent_hash == operation.new_torrent_hash:
        return
    _stop_torrent_by_hash(qbit, operation.old_torrent_hash)


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
                _operation_retry(session, operation, episode, "Replacement torrent is not visible yet; the previous torrent remains untouched.")
                return None
            state = _torrent_state(torrent)
            if state in _FAILED_TORRENT_STATES:
                _operation_retry(session, operation, episode, f"Replacement torrent state: {state}", minutes=15)
                return None
            if not _is_healthy_torrent(torrent):
                _operation_retry(session, operation, episode, "Replacement is rechecking; the previous torrent remains untouched.")
                return None
            if not _is_seeding_torrent(torrent):
                operation.status = TorrentOperationStatus.SEEDING
                _operation_retry(session, operation, episode, "Replacement is downloading; the previous torrent is kept until it finishes.")
                return None
            try:
                _stop_superseded_torrent(qbit, operation)
            except Exception as e:
                _operation_retry(session, operation, episode, str(e))
                return None
            operation.status = TorrentOperationStatus.OLD_STOPPED
            operation.next_retry_at = None
            operation.last_error = None
            operation.updated_at = utc_now()
            session.add(operation)
            session.commit()
        elif operation.status == TorrentOperationStatus.OLD_STOPPED:
            try:
                _stop_superseded_torrent(qbit, operation)
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


def _resume_step(operation: TorrentOperation) -> Optional[str]:
    """Ladder a resumed operation forward one step, never backwards."""
    if operation.status == TorrentOperationStatus.PREPARING:
        operation.status = TorrentOperationStatus.ADDED
        return None
    if operation.status == TorrentOperationStatus.RETRY_WAIT:
        if operation.ambiguous:
            return None
        operation.status = TorrentOperationStatus.ADDED
        return None
    if operation.status == TorrentOperationStatus.UNKNOWN:
        if operation.new_torrent_hash:
            operation.status = TorrentOperationStatus.ADDED
            return None
        return None
    if operation.status == TorrentOperationStatus.ADDED and operation.kind == "replace":
        if operation.old_torrent_hash and operation.old_torrent_hash != operation.new_torrent_hash:
            operation.status = TorrentOperationStatus.NEW_VERIFIED
        return None
    return _RESUME_SUCCESSOR.get(operation.status.name)


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
            else:
                retry_at = _aware(operation.next_retry_at)
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
                updated_at = _aware(operation.updated_at) or now
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
            stage = _resume_step(operation)
            if stage and stage != operation.status.name:
                operation.status = TorrentOperationStatus[stage]
                operation.updated_at = utc_now()
                session.add(operation)
                session.commit()
                if stage == TorrentOperationStatus.OLD_STOPPED.name:
                    message = _finish_operation(session, qbit, operation, episode)
                    if message:
                        logs.append(message)
                    continue
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
            episode.feed_id = show.current_feed_id
            episode.status = EpisodeStatus.COMPLETED if _is_seeding_torrent(torrent) else EpisodeStatus.DOWNLOADING
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
        episode.torrent_hash = _torrent_hash(torrent)
        if episode.status == EpisodeStatus.REPLACING:
            continue
        if _is_seeding_torrent(torrent):
            if episode.status != EpisodeStatus.COMPLETED:
                _complete_episode(session, episode)
                logs.append(f"Completed {show_name(episode, session)} Ep {episode.episode_number}")
        elif _torrent_state(torrent) in {"error", "missingfiles", "unknown"}:
            episode.status = EpisodeStatus.FAILED
            episode.last_error = f"qBittorrent torrent state: {_torrent_state(torrent)}"
            session.add(episode)
    session.commit()
    return logs


def _grab_feeds(session: Session, show: Monitored, feeds: List[Feed]) -> List[Feed]:
    """The one feed this show may download from."""
    if not feeds:
        return []
    assigned = next((feed for feed in feeds if feed.id == show.current_feed_id), None)
    if assigned is not None:
        return [assigned]
    return sorted(feeds, key=lambda feed: (feed.priority, feed.id))


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
        (episode, _aware(episode.air_at))
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


def _mapped_episode(
    session: Session,
    show: Monitored,
    feed: Feed,
    raw_episode: int,
    latest_aired: Optional[int],
    target_count: int,
    article: Dict[str, Any],
) -> Optional[int]:
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
    retry_at: datetime,
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
        show.title_english if getattr(settings, "title_language", "english") == "english" and show.title_english else (show.title_romaji or show.display_name),
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
            tags=_episode_tags(show, episode.episode_number, operation.version, operation.operation_tag),
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
    if show.current_feed_id is None:
        show.current_feed_id = operation.feed_id
    show.matched_title = operation.release_title
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
    for show in shows:
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
        tolerance_hours = max(0, int(getattr(settings, "early_air_tolerance_hours", 0) or 0))
        air_horizon = now + timedelta(hours=tolerance_hours)
        known_aired = [
            episode.episode_number
            for episode in episodes
            if episode.air_at and _aware(episode.air_at) <= air_horizon
        ]
        latest_aired = max(known_aired) if known_aired else None
        airing_at = _aware(show.next_airing_at)
        if latest_aired is None and show.next_airing_episode:
            latest_aired = show.next_airing_episode if airing_at and airing_at <= now else max(0, show.next_airing_episode - 1)
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
        grabbed_from: Optional[int] = None
        has_feed_articles = False
        for feed in candidate_feeds:
            feed_articles = articles_by_url.get(feed.qbit_feed_url, [])
            if feed_articles:
                has_feed_articles = True
            if grabbed_from is not None:
                break
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
                episode_air_at = _aware(episode.air_at)
                backfill_window_days = max(0, int(getattr(settings, "backfill_window_days", 14)))
                version = int(parsed.get("version") or 1)
                is_upgrade = version > episode.version
                if not is_upgrade:
                    if episode_air_at is None:
                        if newest_wanted is not None and episode_number < newest_wanted:
                            continue
                    elif now - episode_air_at > timedelta(days=backfill_window_days):
                        continue
                if episode.retry_after and _aware(episode.retry_after) > now:
                    continue
                if version in failed_versions.get(episode.id, set()):
                    continue
                if episode.status == EpisodeStatus.FAILED and version <= episode.version:
                    continue
                if episode.status in {EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADING, EpisodeStatus.REPLACING} and version <= episode.version:
                    continue
                if episode.status == EpisodeStatus.QUEUED and version <= episode.version:
                    continue
                if mode == "observe":
                    decision = GrabDecisionType.WOULD_REPLACE if version > episode.version else GrabDecisionType.WOULD_GRAB
                    reason = "Higher release version detected." if decision == GrabDecisionType.WOULD_REPLACE else "Wanted episode detected."
                    _record_decision(session, show, episode, feed, article, title, episode_number, version, decision, reason)
                    logs.append(f"Would {'replace' if decision == GrabDecisionType.WOULD_REPLACE else 'grab'} {show.display_name} Ep {episode_number} v{version}: {title}")
                    continue
                if mode != "direct":
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
            backfill_window_days = max(0, int(getattr(settings, "backfill_window_days", 14)))
            for episode in episodes:
                if episode.status != EpisodeStatus.WANTED:
                    continue
                episode_air_at = _aware(episode.air_at)
                # An episode MUST have reached its air time to ever be considered missed.
                # Future scheduled episodes must remain WANTED.
                has_aired = False
                if episode_air_at is not None:
                    has_aired = (episode_air_at <= air_horizon)
                elif latest_aired is not None:
                    has_aired = (episode.episode_number <= latest_aired)

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
                    e.episode_number > episode.episode_number
                    and e.status in {
                        EpisodeStatus.COMPLETED,
                        EpisodeStatus.DOWNLOADING,
                        EpisodeStatus.QUEUED,
                        EpisodeStatus.REPLACING,
                    }
                    for e in episodes
                )
                if is_stale_air or is_older_than_latest or has_newer_downloaded:
                    episode.status = EpisodeStatus.MISSED
                    session.add(episode)
                    msg = f"{show.display_name} Ep {episode.episode_number} was not found in RSS feed; marked as missed."
                    logs.append(msg)
                    logger.info(msg)
        if grabbed_from is not None and show.current_feed_id != grabbed_from:
            show.current_feed_id = grabbed_from
            session.add(show)
        session.commit()
    return logs
