import asyncio
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

import qbittorrentapi

from kisetsu.db.session import get_engine, get_settings
from kisetsu.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    Monitored,
    Feed,
    RuleHistory,
    MonitoredStatus,
    RuleOutcome,
    MatchHistory,
    Settings,
    normalize_mapping_source,
    utc_now,
)
from kisetsu.clients.qbit import QBitClient, QbitAuthenticationError, QbitClientError
from kisetsu.clients.anilist import AniListClient
from kisetsu.config import DEFAULT_DOWNLOAD_MODE
from kisetsu.core.discovery import discover_feed_for_show, flatten_rss_articles
from kisetsu.core.feedcache import cached_articles, ingest_rss
from kisetsu.core.grabber import cancel_episode_operations, direct_feed_matches, manual_grab, rebase_ledger, releases_in_other_feeds, restore_episode
from kisetsu.core.supervisor import Supervisor, delete_monitored_show
from kisetsu.core.matching import match_release_to_show, prepare_aliases
from kisetsu.core.palette import fetch_hues
from kisetsu.core.rules import (
    DEFAULT_MUST_NOT,
    RULE_LEAD_TIME,
    build_regex_pattern,
    build_rule_name,
    compress_home_path,
    create_or_update_rule,
    delete_rule,
    effective_title,
    is_show_rule_deferred,
    is_show_rule_enabled,
    is_show_rule_unreleased,
    resolve_save_path,
    sanitize_folder_name,
)
from kisetsu.workers.scheduler import calculate_next_poll_interval
from kisetsu.server.state import state

router = APIRouter(prefix="/api")
engine = get_engine()
anilist_client = AniListClient()

VALID_DOWNLOAD_MODES = {"rules", "direct"}


def get_db():
    with Session(engine) as session:
        yield session


def _qbit_from_settings(s: Settings) -> QBitClient:
    return QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)


def get_qbit(session: Session = Depends(get_db)) -> QBitClient:
    return _qbit_from_settings(get_settings(session))


def normalized_download_mode(session: Session) -> str:
    """The configured download engine, normalised to a known value."""
    settings = get_settings(session)
    mode = str(getattr(settings, "download_mode", DEFAULT_DOWNLOAD_MODE) or DEFAULT_DOWNLOAD_MODE)
    mode = mode.strip().lower()
    return mode if mode in VALID_DOWNLOAD_MODES else DEFAULT_DOWNLOAD_MODE


def uses_direct_engine(mode: str) -> bool:
    return mode == "direct"


CYCLE_WAIT_SECONDS = 45.0

# What the cycle slot's owner is doing, in words for the UI.
_CYCLE_LABELS = {
    "background": "background check",
    "api-cycle": "manual sync",
    "settle": "download check",
    "api": "another change",
}


def require_exclusive_cycle(wait: float = CYCLE_WAIT_SECONDS) -> None:
    """Claim the cycle slot for a mutating request, waiting for a running cycle.

    A change applied mid-cycle would land on top of a half-finished cycle, so
    the request waits for the slot instead. It blocks the calling thread, which
    is fine for the sync endpoints that run in the threadpool; async endpoints
    must call it through ``asyncio.to_thread``.
    """
    if not state.try_begin_cycle("api", wait=wait):
        raise HTTPException(
            status_code=409,
            detail="A background check is still running. Try again in a moment.",
        )


def release_cycle() -> None:
    state.end_cycle("api")


ACCENT_VERSION = 2


def _accent_source(show: Monitored) -> str:
    return show.banner_image or show.cover_image or ""


def _accent_key(show: Monitored) -> str:
    """What a stored value must match to be current: the image and the extraction version."""
    source = _accent_source(show)
    return f"{source}#{ACCENT_VERSION}" if source else ""


def _parse_accent(show: Monitored) -> Optional[List[float]]:
    if not show.accent_hues:
        return None
    try:
        hue, saturation, secondary, tint_hue, tint_saturation = show.accent_hues.split(",")
        return [int(hue), float(saturation), int(secondary), int(tint_hue), float(tint_saturation)]
    except ValueError:
        return None


def _accent_ready(show: Monitored) -> bool:
    key = _accent_key(show)
    return bool(key) and show.accent_src == key and show.accent_hues is not None


# One fetch per show at a time, so a page warming every show and a click on one of
# them do not download the same banner twice.
_accent_tasks: Dict[int, "asyncio.Task[Tuple[bool, Optional[Tuple[int, float, int, int, float]]]]"] = {}


@router.get("/shows/{show_id}/accent")
async def get_show_accent(show_id: int):
    with Session(engine) as session:
        show = session.get(Monitored, show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        if _accent_ready(show) or not _accent_source(show):
            return {"accent_hues": _parse_accent(show), "accent_ready": _accent_ready(show)}
        key, banner, cover = _accent_key(show), show.banner_image or "", show.cover_image or ""

    task = _accent_tasks.get(show_id)
    if task is None:
        task = asyncio.ensure_future(fetch_hues(banner, cover))
        _accent_tasks[show_id] = task
        task.add_done_callback(lambda _t: _accent_tasks.pop(show_id, None))
    ok, hues = await task

    with Session(engine) as session:
        show = session.get(Monitored, show_id)
        if not show:
            raise HTTPException(status_code=404, detail="Show not found")
        # A failed download is not remembered, so it is tried again next time.
        if ok:
            show.accent_src = key
            show.accent_hues = "" if hues is None else f"{hues[0]},{hues[1]:.3f},{hues[2]},{hues[3]},{hues[4]:.3f}"
            session.add(show)
            session.commit()
            session.refresh(show)
        return {"accent_hues": _parse_accent(show), "accent_ready": _accent_ready(show)}


@router.get("/shows")
def get_shows(session: Session = Depends(get_db)):
    settings = get_settings(session)
    now = datetime.now(timezone.utc)
    shows = session.exec(select(Monitored).order_by(Monitored.id)).all()
    feeds = {f.id: f.qbit_feed_name for f in session.exec(select(Feed)).all()}

    result = []
    episode_rows = session.exec(select(Episode)).all()
    episodes_by_show: Dict[int, List[Episode]] = {}
    for episode in episode_rows:
        episodes_by_show.setdefault(episode.monitored_id, []).append(episode)

    mappings_by_show: Dict[int, List[EpisodeNumberMapping]] = {}
    for mapping in session.exec(select(EpisodeNumberMapping)).all():
        mappings_by_show.setdefault(mapping.monitored_id, []).append(mapping)

    for s in shows:
        airing_at = s.next_airing_at
        if airing_at and airing_at.tzinfo is None:
            airing_at = airing_at.replace(tzinfo=timezone.utc)

        is_released = (
            s.status in (MonitoredStatus.FIXED, MonitoredStatus.COMPLETED)
            or (s.next_airing_episode is not None and s.next_airing_episode > 1)
            or (s.last_confirmed_episode is not None and s.last_confirmed_episode >= 1)
            or (airing_at is not None and airing_at <= now)
        )

        romaji_title = s.title_romaji or s.display_name
        effective_display_name = effective_title(s, settings.title_language)

        base_template = settings.base_dir or "~/Anime/{name}"
        is_custom_folder = bool(s.save_folder and s.save_folder != sanitize_folder_name(s.display_name) and s.save_folder != sanitize_folder_name(s.title_romaji or "") and s.save_folder != sanitize_folder_name(s.title_english or ""))
        if is_custom_folder and (s.save_folder.startswith("/") or s.save_folder.startswith("~")):
            resolved_save_path = s.save_folder
        else:
            folder_name = sanitize_folder_name(s.save_folder if is_custom_folder else effective_display_name)
            if "{name}" in base_template:
                resolved_save_path = base_template.replace("{name}", folder_name)
            else:
                resolved_save_path = f"{base_template.rstrip('/')}/{folder_name}"

        resolved_save_path = compress_home_path(resolved_save_path)

        show_episodes = episodes_by_show.get(s.id, [])
        downloaded_count = sum(1 for e in show_episodes if e.status == EpisodeStatus.COMPLETED)
        wanted_count = sum(1 for e in show_episodes if e.status == EpisodeStatus.WANTED)
        missed_count = sum(1 for e in show_episodes if e.status == EpisodeStatus.MISSED)
        failed_count = sum(1 for e in show_episodes if e.status == EpisodeStatus.FAILED)
        v2_count = sum(
            1 for e in show_episodes
            if e.status == EpisodeStatus.COMPLETED and (e.version or 1) > 1
        )
        episode_mappings = [
            {
                "feed_id": mapping.feed_id,
                "feed_name": feeds.get(mapping.feed_id),
                "offset": mapping.offset,
                "source": normalize_mapping_source(mapping.source),
                "evidence_count": mapping.evidence_count,
            }
            for mapping in sorted(mappings_by_show.get(s.id, []), key=lambda m: m.feed_id or 0)
        ]

        result.append({
            "id": s.id,
            "anilist_id": s.anilist_id,
            "display_name": effective_display_name,
            "title_romaji": romaji_title,
            "title_english": s.title_english,
            "cover_image": s.cover_image,
            "banner_image": s.banner_image,
            "accent_hues": _parse_accent(s),
            "accent_ready": _accent_ready(s),
            "season_name": s.season_name,
            "season_year": s.season_year,
            "status": s.status.value,
            "current_feed_id": s.current_feed_id,
            "current_feed_name": feeds.get(s.current_feed_id) if s.current_feed_id else None,
            "qbit_rule_name": s.qbit_rule_name,
            "total_episodes": s.total_episodes,
            "next_airing_episode": s.next_airing_episode,
            "next_airing_at": airing_at.isoformat() if airing_at else None,
            "next_airing_formatted": airing_at.strftime("%d/%m %H:%M") if airing_at else None,
            "last_confirmed_episode": s.last_confirmed_episode,
            "matched_title": s.matched_title,
            "matched_release_group": s.matched_release_group,
            "save_folder": s.save_folder,
            "save_path": resolved_save_path,
            "is_released": is_released,
            "custom_aliases": s.custom_aliases,
            "feed_pinned": bool(s.feed_pinned),
            "feed_learned": s.learned_feed_id is not None,
            "learned_feed_id": s.learned_feed_id or 0,
            "learned_feed_name": feeds.get(s.learned_feed_id) if s.learned_feed_id else None,
            "candidate_feed_id": s.candidate_feed_id,
            "downloaded_episodes_count": downloaded_count,
            "wanted_episodes_count": wanted_count,
            "missed_episodes_count": missed_count,
            "failed_episodes_count": failed_count,
            "v2_episodes_count": v2_count,
            "schedule_stale": bool(s.schedule_stale),
            "schedule_synced_at": s.schedule_synced_at.isoformat() if s.schedule_synced_at else None,
            "anilist_status": s.anilist_status,
            "episode_mappings": episode_mappings,
        })
    return result


@router.post("/shows/{show_id}/pause")
def toggle_pause_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    require_exclusive_cycle()
    try:
        return _toggle_pause_show(show_id, session, qbit)
    finally:
        release_cycle()


def _toggle_pause_show(show_id: int, session: Session, qbit: QBitClient):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    mode = normalized_download_mode(session)
    direct = uses_direct_engine(mode)
    owns_rules = mode == "rules"

    if show.status == MonitoredStatus.PAUSED:
        if show.status_before_pause:
            try:
                show.status = MonitoredStatus(show.status_before_pause)
            except ValueError:
                show.status = MonitoredStatus.UNCONFIRMED
        elif show.current_feed_id and show.matched_release_group:
            show.status = MonitoredStatus.FIXED
        else:
            show.status = MonitoredStatus.UNCONFIRMED
        show.status_before_pause = None

        if not direct and show.qbit_rule_name and owns_rules:
            try:
                rules = qbit.get_rss_rules()
                if show.qbit_rule_name in rules:
                    rdef = rules[show.qbit_rule_name]
                    rdef["enabled"] = True
                    qbit.set_rss_rule(show.qbit_rule_name, rdef)
            except Exception as e:
                state.add_log(f"Warning enabling qBit rule for '{show.display_name}': {e}", "WARNING")

        session.add(show)
        session.commit()
        state.add_log(f"Resumed show '{show.display_name}'.", "INFO")
        return {"status": "success", "new_status": show.status.value, "message": f"Resumed '{show.display_name}'"}
    else:
        show.status_before_pause = show.status.value
        show.status = MonitoredStatus.PAUSED

        if direct:
            for episode in session.exec(
                select(Episode).where(Episode.monitored_id == show.id)
            ).all():
                try:
                    cancel_episode_operations(session, qbit, show, episode, "Show paused by user.", keep_added=True)
                except Exception as e:
                    state.add_log(f"Warning cancelling operations for '{show.display_name}': {e}", "WARNING")
        elif show.qbit_rule_name and owns_rules:
            try:
                rules = qbit.get_rss_rules()
                if show.qbit_rule_name in rules:
                    rdef = rules[show.qbit_rule_name]
                    rdef["enabled"] = False
                    qbit.set_rss_rule(show.qbit_rule_name, rdef)
            except Exception as e:
                state.add_log(f"Warning disabling qBit rule for '{show.display_name}': {e}", "WARNING")

        session.add(show)
        session.commit()
        state.add_log(f"Paused show '{show.display_name}'.", "INFO")
        return {"status": "success", "new_status": show.status.value, "message": f"Paused '{show.display_name}'"}


@router.get("/shows/{show_id}/episodes")
def get_show_episodes(show_id: int, session: Session = Depends(get_db)):
    """The canonical episode ledger for a show, oldest episode first."""
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    episodes = session.exec(
        select(Episode)
        .where(Episode.monitored_id == show_id)
        .order_by(Episode.episode_number)
    ).all()
    feed_names = {feed.id: feed.qbit_feed_name for feed in session.exec(select(Feed)).all()}
    return [
        {
            "id": episode.id,
            "episode_number": episode.episode_number,
            "feed_name": feed_names.get(episode.feed_id),
            "source_episode": episode.source_episode,
            "air_at": episode.air_at.isoformat() if episode.air_at else None,
            "schedule_state": episode.schedule_state,
            "attempt_count": episode.attempt_count,
            "last_attempt_at": episode.last_attempt_at.isoformat() if episode.last_attempt_at else None,
            "retry_after": episode.retry_after.isoformat() if episode.retry_after else None,
            "status": episode.status.value,
            "version": episode.version,
            "release_title": episode.release_title,
            "release_group": episode.release_group,
            "torrent_hash": episode.torrent_hash,
            "downloaded_at": episode.downloaded_at.isoformat() if episode.downloaded_at else None,
            "last_error": episode.last_error,
        }
        for episode in episodes
    ]


@router.post("/shows/{show_id}/rediscover")
def rediscover_show(
    show_id: int,
    force: bool = False,
    session: Session = Depends(get_db),
    qbit: QBitClient = Depends(get_qbit),
):
    require_exclusive_cycle()
    try:
        return _rediscover_show(show_id, session, qbit, force=force)
    finally:
        release_cycle()


def _rediscover_show(show_id: int, session: Session, qbit: QBitClient, force: bool = False):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    # A feed that has already delivered a release is not a guess, and wiping it
    # is how a working show ends up hunting again. Releasing it takes an explicit
    # override, because the only way back is rediscovering from scratch.
    if show.learned_feed_id is not None and not force:
        feed = session.get(Feed, show.learned_feed_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{show.display_name}' has already downloaded from "
                f"'{feed.qbit_feed_name if feed else show.learned_feed_id}', so that feed is "
                f"locked. Pick a feed explicitly instead, or repeat the reset with force=true "
                f"to abandon the learned feed and rediscover from scratch."
            ),
        )

    old_feed_id = show.current_feed_id
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning: Could not delete rule '{show.qbit_rule_name}': {e}", "WARNING")

    direct = uses_direct_engine(normalized_download_mode(session))
    # In direct mode a reset says nothing about the feed, so it is not held against it.
    if old_feed_id and old_feed_id != show.learned_feed_id and not direct:
        hist = RuleHistory(
            monitored_id=show.id,
            feed_id=old_feed_id,
            outcome=RuleOutcome.FALSE_POSITIVE,
            note="Manually reset/rediscovered from WebUI.",
        )
        session.add(hist)

    show.current_feed_id = None
    show.learned_feed_id = None
    show.qbit_rule_name = None
    show.matched_title = None
    show.matched_release_group = None
    show.feed_pinned = False
    show.candidate_feed_id = None
    show.candidate_feed_since = None
    show.status = MonitoredStatus.UNCONFIRMED
    session.add(show)
    session.commit()

    if direct:
        return {"status": "success", "message": _auto_discover_now(session, qbit, show)}

    state.add_log(f"Reset rule for '{show.display_name}'. Will rediscover on next supervision cycle.", "INFO")
    return {"status": "success", "message": f"Reset '{show.display_name}'. Will rediscover on next cycle."}


def _effective_display_name(show: Monitored, settings: Settings) -> str:
    return effective_title(show, settings.title_language)


def _rule_download_params(rule: Dict[str, Any], show: Monitored, settings: Settings, display_name: str) -> Dict[str, Any]:
    torrent_params = rule.get("torrentParams") or {}

    default_names = {
        sanitize_folder_name(show.display_name),
        sanitize_folder_name(show.title_romaji or ""),
        sanitize_folder_name(show.title_english or ""),
    }
    save_folder = show.save_folder if (show.save_folder and show.save_folder not in default_names) else None

    ratio_limit = torrent_params.get("ratio_limit")
    if ratio_limit is None:
        ratio_limit = settings.default_seed_ratio

    return {
        "save_path": rule.get("savePath") or resolve_save_path(settings.base_dir, display_name, save_folder),
        "custom_save_folder": save_folder or "",
        "category": torrent_params.get("category") or rule.get("assignedCategory") or settings.default_category,
        "ratio_limit": ratio_limit,
        "is_paused": bool(rule.get("addPaused")),
    }


def _resolve_article_url(qbit: QBitClient, title: str, feed_urls: List[str]) -> Optional[str]:
    try:
        articles_by_url = flatten_rss_articles(qbit.get_rss_items(with_data=True))
    except Exception as e:
        state.add_log(f"Warning resolving RSS article '{title}' for manual download: {e}", "DEBUG")
        return None

    for feed_url in feed_urls:
        for article in articles_by_url.get(feed_url) or []:
            if isinstance(article, dict) and article.get("title") == title:
                return article.get("torrentURL") or None
    return None


def _show_rule_data(qbit: QBitClient, show: Monitored) -> Dict[str, Any]:
    """The qBittorrent rule backing this show, or {} (direct mode owns none)."""
    if not show.qbit_rule_name:
        return {}
    try:
        return (qbit.get_rss_rules() or {}).get(show.qbit_rule_name, {})
    except Exception as e:
        state.add_log(f"Warning fetching rule details from qBit: {e}", "WARNING")
        return {}


def _saved_patterns(session: Session, show: Monitored, qbit_rule_data: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(saved must-contain regex, must-not-contain regex) for a show."""
    saved_must_not = qbit_rule_data.get("mustNotContain") or show.custom_must_not or DEFAULT_MUST_NOT

    saved_regex = qbit_rule_data.get("mustContain") or show.custom_regex
    if not saved_regex and show.id:
        hist_match = session.exec(
            select(MatchHistory)
            .where(MatchHistory.monitored_id == show.id)
            .where(MatchHistory.matched_regex != None)
            .order_by(MatchHistory.created_at.desc())
            .limit(1)
        ).first()
        if hist_match and hist_match.matched_regex:
            saved_regex = hist_match.matched_regex

    if not saved_regex and show.matched_title:
        saved_regex = build_regex_pattern(
            show.effective_aliases,
            matched_title=show.matched_title,
        )
    return saved_regex, saved_must_not


@router.get("/shows/{show_id}/rule")
def get_show_rule_details(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    """Everything the show dialog needs to draw its form.

    This must stay cheap: the dialog opens on it. Anything that reads
    qBittorrent's RSS cache lives in ``/feed-matches`` and loads afterwards.
    """
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    feed = session.get(Feed, show.current_feed_id) if show.current_feed_id else None

    download_mode = normalized_download_mode(session)

    qbit_rule_data = _show_rule_data(qbit, show)
    saved_regex, saved_must_not = _saved_patterns(session, show, qbit_rule_data)

    # With no rule and nothing learned there is no pattern to show. Inventing one
    # from the aliases here would put a value in the field that nothing consumes,
    # because in direct mode matching is done from the episode ledger, not from a
    # qBittorrent regex.
    has_qbit_rule = bool(show.qbit_rule_name and feed)

    effective_display_name = _effective_display_name(show, settings)

    expected_rule_name = build_rule_name(show.id or 0, effective_display_name)
    rule_is_enabled = qbit_rule_data.get("enabled") if "enabled" in qbit_rule_data else is_show_rule_enabled(show)

    download_params = _rule_download_params(qbit_rule_data, show, settings, effective_display_name)

    # A pattern is only trustworthy once it was learned from a real release.
    # Before that the feed it sits on is just a default, not an assignment.
    is_upcoming = is_show_rule_unreleased(show)
    has_learned_pattern = bool(show.matched_title or show.custom_regex)

    return {
        "show_id": show.id,
        "display_name": effective_display_name,
        "cover_image": show.cover_image,
        "banner_image": show.banner_image,
        "has_rule": bool(show.current_feed_id or show.qbit_rule_name or show.status == MonitoredStatus.COMPLETED or saved_regex or show.matched_title),
        # Whether a real qBittorrent RSS rule backs this show. Direct mode owns no
        # rule, so it is the difference between a Must Contain field that is read
        # by something and one that only looks meaningful.
        "has_qbit_rule": has_qbit_rule,
        "rule_name": show.qbit_rule_name or expected_rule_name,
        "enabled": rule_is_enabled,
        "feed_name": feed.qbit_feed_name if feed else None,
        "feed_url": feed.qbit_feed_url if feed else None,
        "must_contain": saved_regex,
        "must_not_contain": saved_must_not,
        "matched_as": show.matched_title,
        "save_path": compress_home_path(download_params["save_path"]),
        "save_folder": download_params["custom_save_folder"],
        "current_feed_id": show.current_feed_id or 0,
        "category": download_params["category"],
        "ratio_limit": download_params["ratio_limit"],
        "status": show.status.value,
        "download_mode": download_mode,
        "custom_aliases": show.custom_aliases,
        "matched_title": show.matched_title,
        "matched_release_group": show.matched_release_group,
        "has_learned_pattern": has_learned_pattern,
        "is_upcoming": is_upcoming,
        "feed_pinned": bool(show.feed_pinned),
        "feed_learned": show.learned_feed_id is not None,
        "feed_locked": show.feed_is_locked,
        "learned_feed_id": show.learned_feed_id or 0,
        "learned_feed_name": feeds_map_name(session, show.learned_feed_id),
        "candidate_feed_id": show.candidate_feed_id or 0,
        "candidate_feed_name": feeds_map_name(session, show.candidate_feed_id),
        "candidate_feed_since": show.candidate_feed_since.isoformat() if show.candidate_feed_since else None,
    }


@router.get("/shows/{show_id}/feed-matches")
def get_show_feed_matches(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    """Releases currently in the show's feed, for the dialog's "In feed" list.

    Reads qBittorrent's RSS cache, so it is slower than ``/rule`` and is fetched
    after the dialog is already open.
    """
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    feed = session.get(Feed, show.current_feed_id) if show.current_feed_id else None

    direct = uses_direct_engine(normalized_download_mode(session))
    try:
        articles_by_url = cached_articles(session) if direct else flatten_rss_articles(qbit.get_rss_items(with_data=True))
    except Exception as e:
        state.add_log(f"Warning fetching cached feed articles: {e}", "DEBUG")
        articles_by_url = {}

    if direct:
        try:
            matches = direct_feed_matches(session, qbit, settings, show, articles_by_url=articles_by_url)
        except Exception as e:
            state.add_log(f"Warning listing direct feed matches: {e}", "DEBUG")
            matches = []
        other_feeds: List[Dict[str, Any]] = []
        if not matches:
            try:
                other_feeds = releases_in_other_feeds(session, show, articles_by_url)
            except Exception as e:
                state.add_log(f"Warning looking for releases in other feeds: {e}", "DEBUG")
        return {"matched_articles": [], "feed_matches": matches, "other_feeds": other_feeds}

    matched_articles: List[str] = []
    qbit_rule_data = _show_rule_data(qbit, show)
    if show.qbit_rule_name:
        try:
            articles_resp = qbit.get_matching_articles(show.qbit_rule_name)
            if isinstance(articles_resp, dict):
                for v in articles_resp.values():
                    if isinstance(v, list) and v:
                        matched_articles.extend(v)
            elif isinstance(articles_resp, list):
                matched_articles = articles_resp
        except Exception as e:
            state.add_log(f"Warning fetching rule details from qBit: {e}", "WARNING")

    feed_items = articles_by_url.get(feed.qbit_feed_url, []) if feed else []
    saved_regex, saved_must_not = _saved_patterns(session, show, qbit_rule_data)
    if not matched_articles and feed_items and saved_regex:
        try:
            must_re = re.compile(saved_regex, re.IGNORECASE)
            must_not_re = re.compile(saved_must_not, re.IGNORECASE) if saved_must_not else None

            for item in feed_items:
                title = item.get("title", "") if isinstance(item, dict) else str(item)
                if must_re.search(title):
                    if must_not_re and must_not_re.search(title):
                        continue
                    if title not in matched_articles:
                        matched_articles.append(title)
        except Exception as e:
            state.add_log(f"Warning matching against cached articles: {e}", "DEBUG")

    return {"matched_articles": matched_articles[:15], "feed_matches": []}


def feeds_map_name(session: Session, feed_id: Optional[int]) -> Optional[str]:
    if not feed_id:
        return None
    candidate = session.get(Feed, feed_id)
    return candidate.qbit_feed_name if candidate else None


class QuickDownloadRequest(BaseModel):
    title: str


@router.post("/shows/{show_id}/quick-download")
def quick_download_show_match(show_id: int, req: QuickDownloadRequest, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    if uses_direct_engine(normalized_download_mode(session)):
        return _direct_quick_download(show, req.title, session, qbit)

    if not show.qbit_rule_name:
        raise HTTPException(status_code=400, detail="This show has no RSS rule yet, so there is nothing to download with.")

    settings = get_settings(session)
    feed = session.get(Feed, show.current_feed_id) if show.current_feed_id else None

    try:
        rule = (qbit.get_rss_rules() or {}).get(show.qbit_rule_name) or {}
    except QbitClientError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not rule:
        raise HTTPException(status_code=400, detail="This show's RSS rule no longer exists in qBittorrent.")

    feed_urls = [feed.qbit_feed_url] if feed else []
    feed_urls += [u for u in (rule.get("affectedFeeds") or []) if u and u not in feed_urls]

    title = req.title.strip()
    url = _resolve_article_url(qbit, title, feed_urls)
    if not url:
        raise HTTPException(status_code=404, detail="That release is no longer in qBittorrent's RSS cache, so it cannot be downloaded.")

    params = _rule_download_params(rule, show, settings, _effective_display_name(show, settings))
    if params["category"]:
        qbit.ensure_category_exists(params["category"])

    try:
        qbit.add_torrent(
            urls=url,
            save_path=params["save_path"],
            category=params["category"],
            ratio_limit=params["ratio_limit"],
            is_paused=params["is_paused"],
        )
    except qbittorrentapi.Conflict409Error:
        raise HTTPException(status_code=409, detail="That release is already in qBittorrent.")
    except QbitClientError as e:
        raise HTTPException(status_code=502, detail=str(e))

    destination = params["save_path"] or "qBittorrent's default save path"
    msg = f"Manual download: '{title}' -> {destination} (category '{params['category']}', ratio {params['ratio_limit']})."
    state.add_log(msg, "INFO")

    return {
        "status": "success",
        "message": f"Downloading '{title}' with this rule's settings.",
        "save_path": params["save_path"],
        "category": params["category"],
        "ratio_limit": params["ratio_limit"],
    }


def _direct_quick_download(show: Monitored, title: str, session: Session, qbit: QBitClient) -> Dict[str, Any]:
    # The grab writes the episode ledger, so it must not interleave with a cycle.
    require_exclusive_cycle()
    try:
        # The show was read before this request got the slot; start from its current state.
        session.refresh(show)
        message = manual_grab(session, qbit, get_settings(session), show, title)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except QbitClientError as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        release_cycle()
    state.add_log(message, "INFO")
    return {"status": "success", "message": message}


@router.post("/shows/{show_id}/episodes/{episode_id}/restore")
def restore_show_episode(
    show_id: int,
    episode_id: int,
    session: Session = Depends(get_db),
    qbit: QBitClient = Depends(get_qbit),
):
    """Put a finished episode that Kisetsu holds no torrent for under management."""
    show = session.get(Monitored, show_id)
    episode = session.get(Episode, episode_id)
    if not show or not episode or episode.monitored_id != show_id:
        raise HTTPException(status_code=404, detail="Episode not found")
    if not uses_direct_engine(normalized_download_mode(session)):
        raise HTTPException(status_code=409, detail="Restoring an episode needs the direct download mode.")

    # The restore writes the episode ledger, so it must not interleave with a cycle.
    require_exclusive_cycle()
    try:
        # Both rows were read before this request got the slot; start from their current state.
        session.refresh(show)
        session.refresh(episode)
        message = restore_episode(session, qbit, get_settings(session), show, episode)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except QbitClientError as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        release_cycle()
    state.add_log(message, "INFO")
    return {"status": "success", "message": message}


class EditShowRequest(BaseModel):
    current_feed_id: Optional[int] = None
    save_folder: Optional[str] = None
    category: Optional[str] = None
    ratio_limit: Optional[float] = None
    must_contain: Optional[str] = None
    must_not_contain: Optional[str] = None
    aliases: Optional[List[str]] = None
    episode_offset: Optional[int] = None
    # Required to move a show off, or back to auto-discovery on, the feed that
    # has already delivered a release for it.
    release_learned_feed: bool = False


def _reject_learned_feed_change(
    show: Monitored,
    new_feed_id: Optional[int],
    release_learned_feed: bool,
    session: Session,
) -> None:
    """Refuse to silently discard the feed a show has actually downloaded from."""
    if show.learned_feed_id is None or release_learned_feed:
        return
    if new_feed_id == show.learned_feed_id:
        return
    feed = session.get(Feed, show.learned_feed_id)
    name = feed.qbit_feed_name if feed else f"feed {show.learned_feed_id}"
    target = "auto-discovery" if new_feed_id is None else "a different feed"
    raise HTTPException(
        status_code=409,
        detail=(
            f"'{show.display_name}' has already downloaded from '{name}', so it will not be "
            f"moved to {target} without releasing it. Retry with release_learned_feed=true "
            f"to abandon that feed, or pick '{name}' again to keep it."
        ),
    )


def _set_episode_offset(session: Session, show: Monitored, feed_id: Optional[int], offset: int) -> None:
    """Record a manual feed-to-canonical episode offset.

    Feeds that carry absolute or previous-season numbering cannot be resolved by
    inference alone, so a manual mapping is stored as MANUAL and is never
    overwritten by later evidence.
    """
    if not show.id or not feed_id:
        return
    mapping = session.exec(
        select(EpisodeNumberMapping).where(
            EpisodeNumberMapping.monitored_id == show.id,
            EpisodeNumberMapping.feed_id == feed_id,
        )
    ).first()
    now = utc_now()
    if mapping is None:
        mapping = EpisodeNumberMapping(
            monitored_id=show.id,
            feed_id=feed_id,
            offset=offset,
            source="MANUAL",
            evidence_count=0,
            first_evidence_at=now,
            last_evidence_at=now,
            updated_at=now,
        )
    else:
        mapping.offset = offset
        mapping.source = "MANUAL"
        mapping.last_evidence_at = now
        mapping.updated_at = now
    session.add(mapping)
    session.commit()
    # Releases stored under the feed's raw numbering move to the right rows now.
    rebase_ledger(session, show, feed_id, offset, target_count=show.total_episodes)


def _set_status_keeping_pause(show: Monitored, status: MonitoredStatus) -> None:
    """Apply a status change, deferring it until resume when the show is paused."""
    if show.status == MonitoredStatus.PAUSED:
        show.status_before_pause = status.value
    else:
        show.status = status


def _auto_discover_now(session: Session, qbit: QBitClient, show: Monitored) -> str:
    """Look for the show in the feed cache right away instead of at the next check.

    The feed is a finding, not proof, so ``learned_feed_id`` stays unset until a
    release is downloaded from it.
    """
    # An explicit request starts fresh: feeds that failed this show before are
    # not skipped here, only by the unattended check.
    feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    found = discover_feed_for_show(
        monitored=show,
        feeds=feeds,
        qbit_client=qbit,
        cached_articles_by_url=cached_articles(session),
        parsed_articles={},
    ) if feeds else None
    if not found:
        state.add_log(f"Auto-Discover for '{show.display_name}': nothing in the feed cache yet.", "INFO")
        return (
            f"'{show.display_name}' set to Auto-Discover. No matching release is in the feed cache yet; "
            "it will be picked up as soon as one appears."
        )
    feed, release_group, matched_title = found
    show.current_feed_id = feed.id
    show.matched_title = matched_title
    show.matched_release_group = release_group
    _set_status_keeping_pause(show, MonitoredStatus.FIXED)
    session.add(show)
    session.add(RuleHistory(
        monitored_id=show.id,
        feed_id=feed.id,
        outcome=RuleOutcome.CONFIRMED,
        note=f"Auto-detected on '{feed.qbit_feed_name}' (direct mode)",
    ))
    session.commit()
    state.add_log(f"Auto-Discover for '{show.display_name}': found on '{feed.qbit_feed_name}'.", "INFO")
    return f"Auto-discovered on '{feed.qbit_feed_name}' (matched as '{matched_title}')."


def _learn_name_from_feed(session: Session, qbit: QBitClient, show: Monitored, feed: Feed) -> Optional[str]:
    """Take the series name from a matching release already cached for this feed."""
    found = discover_feed_for_show(
        monitored=show,
        feeds=[feed],
        qbit_client=qbit,
        cached_articles_by_url=cached_articles(session),
        parsed_articles={},
    )
    if not found:
        return None
    _, release_group, matched_title = found
    show.matched_title = matched_title
    show.matched_release_group = release_group
    session.add(show)
    session.commit()
    return matched_title


@router.post("/shows/{show_id}/edit")
def edit_show(show_id: int, req: EditShowRequest, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    require_exclusive_cycle()
    try:
        return _edit_show(show_id, req, session, qbit)
    finally:
        release_cycle()


def _edit_show(show_id: int, req: EditShowRequest, session: Session, qbit: QBitClient):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    mode = normalized_download_mode(session)
    direct = uses_direct_engine(mode)
    previous_feed_id = show.current_feed_id
    new_feed_id = None if req.current_feed_id is not None and req.current_feed_id <= 0 else (req.current_feed_id if req.current_feed_id is not None else show.current_feed_id)

    if req.current_feed_id is not None:
        _reject_learned_feed_change(show, new_feed_id, req.release_learned_feed, session)
        # Checked before anything is edited: the episode offset below commits.
        if new_feed_id is not None and session.get(Feed, new_feed_id) is None:
            raise HTTPException(status_code=400, detail="Selected feed not found")

    if req.save_folder is not None:
        val = req.save_folder.strip()
        if not val or val == "{name}":
            show.save_folder = ""
        else:
            show.save_folder = val

    if req.must_contain is not None:
        show.custom_regex = req.must_contain.strip() if req.must_contain.strip() else None
    if req.must_not_contain is not None:
        show.custom_must_not = req.must_not_contain.strip() if req.must_not_contain.strip() else None
    if req.aliases is not None:
        # Only the hand-written set is replaced; AniList re-supplies its own
        # aliases on every schedule sync and those must stay untouched.
        show.custom_aliases = [alias.strip() for alias in req.aliases if alias and alias.strip()]

    if req.current_feed_id is not None:
        # An explicit pick pins the feed; auto-detect must not move it afterwards.
        show.feed_pinned = req.current_feed_id > 0
        show.candidate_feed_id = None
        show.candidate_feed_since = None
        if show.learned_feed_id is not None and show.learned_feed_id != new_feed_id:
            # The user explicitly released the proven feed (the guard above made
            # them ask for it). Done here rather than in each branch below so
            # every path that moves the show also drops the lock, instead of
            # leaving it pointing at a feed we have left.
            show.learned_feed_id = None

    if req.episode_offset is not None:
        _set_episode_offset(session, show, new_feed_id or show.current_feed_id, int(req.episode_offset))

    if show.status == MonitoredStatus.COMPLETED:
        if req.current_feed_id is not None and req.current_feed_id > 0:
            show.current_feed_id = req.current_feed_id
        session.add(show)
        session.commit()
        return {"status": "success", "message": f"Updated metadata for completed show '{show.display_name}'."}

    category = req.category.strip() if req.category is not None and req.category.strip() else settings.default_category
    ratio_limit = req.ratio_limit if req.ratio_limit is not None and req.ratio_limit >= 0 else settings.default_seed_ratio

    if direct:
        if show.qbit_rule_name:
            try:
                delete_rule(qbit, show.qbit_rule_name)
            except Exception as e:
                state.add_log(f"Warning deleting old rule '{show.qbit_rule_name}': {e}", "WARNING")
        show.qbit_rule_name = None

        if new_feed_id is None:
            show.current_feed_id = None
            show.learned_feed_id = None
            show.matched_title = None
            show.matched_release_group = None
            show.feed_pinned = False
            show.candidate_feed_id = None
            show.candidate_feed_since = None
            _set_status_keeping_pause(show, MonitoredStatus.UNCONFIRMED)
            session.add(show)
            session.commit()
            state.add_log(f"Reset '{show.display_name}' to Auto-Discover mode (direct engine).", "INFO")
            return {"status": "success", "message": _auto_discover_now(session, qbit, show)}

        feed = session.get(Feed, new_feed_id)
        if not feed:
            raise HTTPException(status_code=400, detail="Selected feed not found")
        show.current_feed_id = feed.id
        if show.status not in (MonitoredStatus.COMPLETED, MonitoredStatus.PAUSED):
            show.status = MonitoredStatus.FIXED
        session.add(show)
        session.commit()
        matched = _learn_name_from_feed(session, qbit, show, feed)
        verb = "Pinned to" if show.feed_pinned else "Assigned to"
        msg = (
            f"{verb} '{feed.qbit_feed_name}' (matched as '{matched}')."
            if matched
            else f"{verb} '{feed.qbit_feed_name}'. Nothing there matches yet; the engine will watch it for a matching release."
        )
        state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
        return {"status": "success", "message": msg}

    title_language = getattr(settings, "title_language", "english")
    feed_changed = req.current_feed_id is not None and new_feed_id != previous_feed_id
    matching_changed = req.must_contain is not None or req.aliases is not None
    if new_feed_id is not None and show.matched_title and not feed_changed and not matching_changed:
        # Only settings such as category, folder or ratio changed: rewrite the
        # existing rule in place and keep what the show has already learned.
        feed = session.get(Feed, new_feed_id)
        if not feed:
            raise HTTPException(status_code=400, detail="Selected feed not found")
        show.qbit_rule_name = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
            title_language=title_language,
        )
        session.add(show)
        session.commit()
        msg = f"Updated rule settings on '{feed.qbit_feed_name}'."
        state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
        return {"status": "success", "message": msg}

    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning deleting old rule '{show.qbit_rule_name}': {e}", "WARNING")
        show.qbit_rule_name = None

    if new_feed_id is None:
        show.current_feed_id = None
        show.learned_feed_id = None
        show.matched_title = None
        show.matched_release_group = None
        show.feed_pinned = False
        show.candidate_feed_id = None
        show.candidate_feed_since = None
        _set_status_keeping_pause(show, MonitoredStatus.UNCONFIRMED)
        session.add(show)
        session.commit()
        state.add_log(f"Reset '{show.display_name}' to Auto-Discover mode.", "INFO")
        return {"status": "success", "message": f"'{show.display_name}' set to Auto-Discover."}

    feed = session.get(Feed, new_feed_id)
    if not feed:
        raise HTTPException(status_code=400, detail="Selected feed not found")

    show.current_feed_id = feed.id

    matched_article = None
    aliases = show.effective_aliases
    test_pattern = build_regex_pattern(aliases)
    prepared_aliases = prepare_aliases(aliases)
    parsed_articles = {}
    try:
        rss_data = qbit.get_rss_items(with_data=True)
        articles_by_feed = flatten_rss_articles(rss_data)
        feed_articles = articles_by_feed.get(feed.qbit_feed_url, [])

        best_art = None
        best_parsed = None
        best_score = 0
        for art in feed_articles:
            is_match, score, parsed_info = match_release_to_show(
                art.get("title", ""),
                aliases,
                test_pattern=test_pattern,
                prepared_aliases=prepared_aliases,
                parsed_cache=parsed_articles,
            )
            if is_match and score > best_score:
                best_score = score
                best_art = art
                best_parsed = parsed_info
        matched_article = best_art
    except Exception as e:
        state.add_log(f"Warning searching feed cache: {e}", "WARNING")

    if matched_article and best_parsed:
        show.matched_title = best_parsed.get("title")
        show.matched_release_group = best_parsed.get("release_group")
        _set_status_keeping_pause(show, MonitoredStatus.FIXED)
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
            title_language=title_language,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}' and matched cached release: {matched_article.get('title')} (Status: Working)"
    else:
        show.matched_title = None
        show.matched_release_group = None
        _set_status_keeping_pause(show, MonitoredStatus.UNCONFIRMED)
        if is_show_rule_deferred(show):
            session.add(show)
            session.commit()
            msg = f"Assigned to '{feed.qbit_feed_name}'. The rule will be created {RULE_LEAD_TIME.days} days before the premiere."
            state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
            return {"status": "success", "message": msg}
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
            title_language=title_language,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}'. Rule created in Testing mode, waiting for next episode drop."

    session.add(show)
    session.commit()
    state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
    return {"status": "success", "message": msg}


@router.delete("/shows/{show_id}")
def delete_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    require_exclusive_cycle()
    try:
        return _delete_show(show_id, session, qbit)
    finally:
        release_cycle()


def _delete_show(show_id: int, session: Session, qbit: QBitClient):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    name = show.display_name
    try:
        delete_monitored_show(session, qbit, show, "Show deleted by user.")
    except QbitClientError as e:
        session.rollback()
        state.add_log(f"Could not delete rule for '{name}': {e}", "WARNING")
        raise HTTPException(
            status_code=409,
            detail=f"Could not delete existing rule: {e}",
        )
    session.commit()

    state.add_log(f"Deleted show '{name}' from monitoring.", "INFO")
    return {"status": "success", "message": f"Deleted '{name}' from monitoring."}


@router.get("/feeds")
def get_feeds(session: Session = Depends(get_db)):
    feeds = session.exec(select(Feed).order_by(Feed.priority)).all()
    return [{"id": f.id, "qbit_feed_name": f.qbit_feed_name, "qbit_feed_url": f.qbit_feed_url, "priority": f.priority} for f in feeds]


class ReorderItem(BaseModel):
    id: int
    priority: int


class ReorderRequest(BaseModel):
    feeds: List[ReorderItem]


@router.post("/feeds/reorder")
def reorder_feeds(req: ReorderRequest, session: Session = Depends(get_db)):
    require_exclusive_cycle()
    try:
        return _reorder_feeds(req, session)
    finally:
        release_cycle()


def _reorder_feeds(req: ReorderRequest, session: Session):
    for item in req.feeds:
        feed = session.get(Feed, item.id)
        if feed:
            feed.priority = item.priority
            session.add(feed)
    session.commit()
    state.add_log("RSS Feed priorities reordered.", "INFO")
    return {"status": "success", "message": "RSS Feed priorities updated."}


@router.post("/feeds/sync")
def sync_feeds(session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    require_exclusive_cycle()
    try:
        return _sync_feeds(session, qbit)
    finally:
        release_cycle()


def _sync_feeds(session: Session, qbit: QBitClient):
    settings = get_settings(session)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=settings)
    try:
        logs = sup.sync_feeds()
        for l in logs:
            state.add_log(l, "INFO")
        return {"status": "success", "logs": logs, "message": " | ".join(logs) if logs else "Feeds synced."}
    except Exception as e:
        state.add_log(f"Error syncing feeds: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/settings")
def get_current_settings(session: Session = Depends(get_db)):
    s = get_settings(session)
    base_dir = s.base_dir or "~/Anime/{name}"
    if base_dir and "{name}" not in base_dir:
        base_dir = f"{base_dir.rstrip('/')}/{{name}}"
    return {
        "qbit_host": s.qbit_host,
        "qbit_username": s.qbit_username,
        "qbit_password_set": bool(s.qbit_password),
        "base_dir": compress_home_path(base_dir),
        "default_category": s.default_category,
        "default_seed_ratio": s.default_seed_ratio,
        "anilist_username": s.anilist_username,
        "refresh_interval_minutes": s.refresh_interval_minutes,
        "stall_wait_hours": s.stall_wait_hours,
        "title_language": getattr(s, "title_language", "english") or "english",
        "download_mode": normalized_download_mode(session),
        "backfill_window_days": s.backfill_window_days,
        "early_air_tolerance_hours": s.early_air_tolerance_hours,
        "accent_color": s.accent_color or "#2dd4bf",
        "accent_tint": s.accent_tint or "subtle",
    }


class UpdateSettingsRequest(BaseModel):
    qbit_host: Optional[str] = None
    qbit_username: Optional[str] = None
    qbit_password: Optional[str] = None
    base_dir: Optional[str] = None
    default_category: Optional[str] = None
    default_seed_ratio: Optional[float] = None
    anilist_username: Optional[str] = None
    refresh_interval_minutes: Optional[int] = None
    stall_wait_hours: Optional[int] = None
    title_language: Optional[str] = None
    download_mode: Optional[str] = None
    backfill_window_days: Optional[int] = None
    early_air_tolerance_hours: Optional[int] = None
    accent_color: Optional[str] = None
    accent_tint: Optional[str] = None


ACCENT_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
VALID_ACCENT_TINTS = ("off", "subtle", "full")


@router.post("/settings")
def update_settings(req: UpdateSettingsRequest, session: Session = Depends(get_db)):
    if req.download_mode is not None:
        mode = req.download_mode.strip().lower()
        if mode not in VALID_DOWNLOAD_MODES:
            raise HTTPException(
                status_code=400,
                detail="download_mode must be rules or direct",
            )
    if req.backfill_window_days is not None and not (0 <= req.backfill_window_days <= 365):
        raise HTTPException(
            status_code=400,
            detail="backfill_window_days must be between 0 and 365",
        )
    if req.early_air_tolerance_hours is not None and not (0 <= req.early_air_tolerance_hours <= 168):
        raise HTTPException(
            status_code=400,
            detail="early_air_tolerance_hours must be between 0 and 168",
        )

    if req.accent_color is not None and not ACCENT_COLOR_RE.match(req.accent_color.strip()):
        raise HTTPException(status_code=400, detail="accent_color must be a #rrggbb colour")
    if req.accent_tint is not None and req.accent_tint.strip().lower() not in VALID_ACCENT_TINTS:
        raise HTTPException(status_code=400, detail="accent_tint must be off, subtle or full")

    appearance_only = all(
        value is None
        for name, value in req.model_dump().items()
        if name not in ("accent_color", "accent_tint")
    )
    if appearance_only:
        s = get_settings(session)
        if req.accent_color is not None:
            s.accent_color = req.accent_color.strip().lower()
        if req.accent_tint is not None:
            s.accent_tint = req.accent_tint.strip().lower()
        session.add(s)
        session.commit()
        return {"message": "Appearance saved."}

    require_exclusive_cycle()
    try:
        return _update_settings(req, session)
    finally:
        release_cycle()


def _update_settings(req: UpdateSettingsRequest, session: Session):
    s = get_settings(session)
    previous_mode = normalized_download_mode(session)
    preflight_logs: List[str] = []
    if req.qbit_host is not None:
        s.qbit_host = req.qbit_host.strip()
    if req.qbit_username is not None:
        s.qbit_username = req.qbit_username.strip()
    if req.qbit_password is not None and req.qbit_password != "":
        s.qbit_password = req.qbit_password
    if req.base_dir is not None:
        raw_base = req.base_dir.strip()
        if raw_base and "{name}" not in raw_base:
            trimmed = raw_base.rstrip("/\\")
            raw_base = f"{trimmed}/{{name}}"
        s.base_dir = raw_base
    if req.default_category is not None:
        s.default_category = req.default_category.strip()
    if req.default_seed_ratio is not None:
        s.default_seed_ratio = req.default_seed_ratio
    if req.anilist_username is not None:
        s.anilist_username = req.anilist_username.strip()
    if req.refresh_interval_minutes is not None:
        s.refresh_interval_minutes = req.refresh_interval_minutes
    if req.stall_wait_hours is not None:
        s.stall_wait_hours = req.stall_wait_hours
    if req.accent_color is not None:
        s.accent_color = req.accent_color.strip().lower()
    if req.accent_tint is not None:
        s.accent_tint = req.accent_tint.strip().lower()
    if req.title_language is not None:
        new_lang = req.title_language.strip().lower()
        if new_lang != s.title_language:
            s.title_language = new_lang
            for show in session.exec(select(Monitored)).all():
                en_t = show.title_english
                ro_t = show.title_romaji
                new_display = en_t if (new_lang == "english" and en_t) else (ro_t or show.display_name)
                show.display_name = new_display
                session.add(show)

    new_mode = previous_mode
    if req.download_mode is not None:
        new_mode = req.download_mode.strip().lower()
    if req.backfill_window_days is not None:
        s.backfill_window_days = req.backfill_window_days
    if req.early_air_tolerance_hours is not None:
        s.early_air_tolerance_hours = req.early_air_tolerance_hours

    if new_mode != previous_mode:
        supervisor = Supervisor(
            session=session,
            qbit=get_qbit(session),
            anilist=anilist_client,
            settings=s,
        )
        try:
            preflight_logs = supervisor.prepare_download_mode(new_mode, recheck_working=True)
        except Exception as e:
            session.rollback()
            state.add_log(f"Could not switch download mode: {e}", "ERROR")
            raise HTTPException(
                status_code=409,
                detail=f"Could not switch download mode: {e}",
            )
        s.download_mode = new_mode

    session.add(s)
    session.commit()

    if new_mode != previous_mode:
        if new_mode == "direct":
            state.request_rss_refresh()
        state.trigger_immediate_cycle()

    message = "Settings updated."
    if new_mode != previous_mode:
        message = f"Settings updated. Download engine is now '{new_mode}'."
    state.add_log(message, "INFO")
    return {
        "status": "success",
        "message": message,
        "download_mode": new_mode,
        "logs": preflight_logs,
    }


class TestQbitConnectionRequest(BaseModel):
    qbit_host: Optional[str] = None
    qbit_username: Optional[str] = None
    qbit_password: Optional[str] = None


@router.post("/settings/test-qbit")
def test_qbit_connection(req: Optional[TestQbitConnectionRequest] = None, session: Session = Depends(get_db)):
    s = get_settings(session)
    host = (req.qbit_host.strip() if req and req.qbit_host else "") or s.qbit_host
    username = (req.qbit_username.strip() if req and req.qbit_username else "") or s.qbit_username
    password = req.qbit_password if req and req.qbit_password else ""
    if not password:
        # The stored password may only go to the stored host; otherwise anyone who
        # can reach this API could point the test at their own server and read it.
        if host.rstrip("/") != (s.qbit_host or "").rstrip("/"):
            raise HTTPException(status_code=400, detail="Enter the password to test a different host.")
        password = s.qbit_password
    client = QBitClient(host=host, username=username, password=password, timeout=6)
    try:
        res = client.test_connection()
        return {"status": "success", "app_version": res.get("app_version"), "api_version": res.get("api_version")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection failed: {e}")


@router.post("/settings/sync-anilist")
async def sync_anilist_now(session: Session = Depends(get_db)):
    s = get_settings(session)
    if not s.anilist_username:
        raise HTTPException(status_code=400, detail="AniList username is not set in Settings.")

    await asyncio.to_thread(require_exclusive_cycle)
    try:
        return await _sync_anilist_now(session)
    finally:
        release_cycle()


async def _sync_anilist_now(session: Session):
    s = get_settings(session)
    mode = normalized_download_mode(session)
    direct = uses_direct_engine(mode)

    qbit = _qbit_from_settings(s)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)
    try:
        logs = []
        # Every step but the AniList fetch is blocking qBittorrent/DB work, so it
        # runs off the event loop to keep the WebUI responsive.
        logs.extend(await asyncio.to_thread(sup.sync_feeds))
        if direct:
            try:
                logs.extend(await asyncio.to_thread(sup.prepare_download_mode, mode))
            except Exception as e:
                raise HTTPException(
                    status_code=409,
                    detail=f"Could not prepare {mode} mode: {e}",
                ) from e
        sync_logs = await sup.sync_anilist_schedule(force=True, direct_mode=direct)
        logs.extend(sync_logs)
        bootstrap_logs = await asyncio.to_thread(
            sup.bootstrap_unassigned_shows,
            create_qbit_rules=not direct,
        )
        logs.extend(bootstrap_logs)
        if direct:
            from kisetsu.core.grabber import evaluate_and_grab_releases
            logs.extend(await asyncio.to_thread(evaluate_and_grab_releases, session, qbit, s, mode=mode))
        else:
            from kisetsu.core.confirmation import verify_and_confirm_torrents
            logs.extend(await asyncio.to_thread(verify_and_confirm_torrents, session, qbit, s))
        if sup.anilist_sync_succeeded is not False:
            logs.extend(await asyncio.to_thread(sup.reconcile_schedule_rollover))

        for l in logs:
            state.add_log(l, "INFO")

        msg = sync_logs[0] if sync_logs else "AniList synced."
        if bootstrap_logs:
            msg += f" Discovered feeds and created rules for {len(bootstrap_logs)} shows."
        return {"status": "success", "logs": logs, "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        state.add_log(f"AniList sync error: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/clear-all")
def clear_all_monitored(session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    require_exclusive_cycle()
    try:
        return _clear_all_monitored(session, qbit)
    finally:
        release_cycle()


def _clear_all_monitored(session: Session, qbit: QBitClient):
    shows = session.exec(select(Monitored)).all()
    count = len(shows)
    try:
        qrules = qbit.get_rss_rules()
        for rname in qrules:
            if (
                rname.startswith("[Seasonal]")
                or any(show.qbit_rule_name == rname for show in shows)
            ):
                delete_rule(qbit, rname, raise_on_error=True)
    except Exception as e:
        state.add_log(f"Could not clear qbit rules: {e}", "WARNING")
        raise HTTPException(status_code=409, detail=f"Could not clear existing rules: {e}")

    for h in session.exec(select(RuleHistory)).all():
        session.delete(h)
    session.flush()

    for s in shows:
        # Dispose of torrents an in-flight direct grab added; the cascade below
        # would otherwise orphan them in qBittorrent.
        for ep in session.exec(select(Episode).where(Episode.monitored_id == s.id)).all():
            cancel_episode_operations(session, qbit, s, ep, "All shows cleared by user.")
        session.flush()
        session.delete(s)
    session.commit()

    state.add_log(f"Cleared all {count} monitored shows and deleted qBittorrent rules.", "WARNING")
    return {"status": "success", "message": f"Cleared all {count} shows."}


_CYCLE_BUSY = "Another supervision or show mutation is already in progress"


@router.post("/cycle/run")
async def run_cycle_now(session: Session = Depends(get_db)):
    if state.is_running_cycle:
        raise HTTPException(status_code=409, detail=_CYCLE_BUSY)

    s = get_settings(session)
    qbit = _qbit_from_settings(s)

    # Probe first so the button does not launch a cycle that can only fail.
    try:
        await asyncio.to_thread(qbit.test_connection)
    except QbitAuthenticationError as e:
        state.add_log(f"Manual cycle skipped: qBittorrent rejected the credentials: {e}", "ERROR")
        raise HTTPException(status_code=503, detail=f"qBittorrent authentication failed: {e}")
    except QbitClientError as e:
        state.add_log(f"Manual cycle skipped: qBittorrent is unavailable: {e}", "ERROR")
        raise HTTPException(status_code=503, detail=f"qBittorrent is unavailable: {e}")

    # Refreshing qBittorrent's feeds is the slow part and needs none of our data,
    # so it is done before the slot is taken; the cycle then reads the cache.
    use_feed_cache = uses_direct_engine(normalized_download_mode(session))
    if use_feed_cache:
        try:
            await asyncio.to_thread(ingest_rss, session.get_bind(), qbit, True)
        except QbitClientError as e:
            state.add_log(f"Manual cycle skipped: could not refresh the RSS feeds: {e}", "ERROR")
            raise HTTPException(status_code=503, detail=f"Could not refresh the RSS feeds: {e}")

    if not state.try_begin_cycle("api-cycle"):
        raise HTTPException(status_code=409, detail=_CYCLE_BUSY)

    try:
        return await _run_cycle_now(session, s, qbit, use_feed_cache)
    finally:
        state.end_cycle("api-cycle")


async def _run_cycle_now(session: Session, s: Settings, qbit: QBitClient, use_feed_cache: bool = False):
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)

    state.add_log("Manual sync initiated from WebUI.", "INFO")
    state.trigger_immediate_rule_check()
    try:
        cycle_options = {"feed_cache": True} if use_feed_cache else {}
        logs = await sup.run_full_cycle(force_rss_refresh=True, force_anilist=True, **cycle_options)
        state.last_cycle_time = datetime.now(timezone.utc)
        for l in logs:
            state.add_log(f"Supervisor: {l}", "INFO")

        default_interval = max(60, s.refresh_interval_minutes * 60)
        sleep_sec, reason = await asyncio.to_thread(
            calculate_next_poll_interval,
            session,
            default_interval_seconds=default_interval,
            qbit_client=qbit,
            download_mode=s.download_mode,
            backfill_window_days=s.backfill_window_days,
            early_air_tolerance_hours=s.early_air_tolerance_hours,
        )
        now_utc = datetime.now(timezone.utc)
        state.next_check_seconds = sleep_sec
        state.next_check_reason = reason
        state.target_next_check_time = now_utc + timedelta(seconds=sleep_sec)

        return {
            "status": "success",
            "logs": logs,
            "next_check_reason": reason,
            "next_check_seconds": sleep_sec,
            "target_next_check_time": state.target_next_check_time.isoformat(),
            "message": "Supervision cycle completed successfully."
        }
    except QbitAuthenticationError as e:
        state.add_log(f"Manual cycle aborted: qBittorrent rejected the credentials: {e}", "ERROR")
        raise HTTPException(status_code=503, detail=f"qBittorrent authentication failed: {e}")
    except QbitClientError as e:
        state.add_log(f"Manual cycle aborted: qBittorrent became unavailable: {e}", "ERROR")
        raise HTTPException(status_code=503, detail=f"qBittorrent became unavailable: {e}")
    except Exception as e:
        state.add_log(f"Error during supervision cycle: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def get_system_status(session: Session = Depends(get_db)):
    shows = session.exec(select(Monitored)).all()
    feeds_count = session.exec(select(func.count(Feed.id))).one()
    now = utc_now()

    works = sum(1 for s in shows if s.status == MonitoredStatus.FIXED)
    stalled = sum(1 for s in shows if s.status == MonitoredStatus.STALLED)
    paused = sum(1 for s in shows if s.status == MonitoredStatus.PAUSED)
    completed = sum(1 for s in shows if s.status == MonitoredStatus.COMPLETED)

    upcoming = 0
    testing = 0
    for s in shows:
        if s.status == MonitoredStatus.UNCONFIRMED:
            if is_show_rule_unreleased(s):
                upcoming += 1
            else:
                testing += 1

    remaining_seconds = 0
    if state.target_next_check_time:
        rem = (state.target_next_check_time - now).total_seconds()
        remaining_seconds = max(0, int(rem))
    elif state.next_check_seconds:
        remaining_seconds = state.next_check_seconds

    wanted = session.exec(
        select(func.count(Episode.id)).where(Episode.status == EpisodeStatus.WANTED)
    ).one()

    cycle_owner, cycle_started = state.cycle_owner, state.cycle_started
    running = state.is_running_cycle and cycle_started is not None
    return {
        "daemon_active": state.daemon_active,
        "download_mode": normalized_download_mode(session),
        "wanted_episodes": wanted,
        "is_running_cycle": state.is_running_cycle,
        "cycle_label": _CYCLE_LABELS.get(cycle_owner or "", "background check") if running else None,
        "cycle_seconds": int(time.monotonic() - cycle_started) if running else None,
        "last_cycle_time": state.last_cycle_time.isoformat() if state.last_cycle_time else None,
        "next_check_reason": state.next_check_reason,
        "next_check_seconds": remaining_seconds,
        "target_next_check_time": state.target_next_check_time.isoformat() if state.target_next_check_time else None,
        "total_shows": len(shows),
        "counts": {
            "works": works,
            "upcoming": upcoming,
            "testing": testing,
            "stalled": stalled,
            "paused": paused,
            "completed": completed,
            "feeds": feeds_count,
        }
    }


@router.get("/logs")
def get_recent_logs(limit: int = 100):
    all_logs = list(state.logs)
    return all_logs[-limit:]


@router.get("/history")
def get_match_history(limit: int = 100, session: Session = Depends(get_db)):
    stmt = select(MatchHistory).order_by(MatchHistory.created_at.desc()).limit(limit)
    items = session.exec(stmt).all()


    shows_map = {s.id: s for s in session.exec(select(Monitored)).all()}
    res = []
    for item in items:
        created_str = None
        if item.created_at:
            dt = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=timezone.utc)
            created_str = dt.isoformat()

        matched_reg = item.matched_regex
        if not matched_reg and item.monitored_id and item.monitored_id in shows_map:
            s_obj = shows_map[item.monitored_id]
            matched_reg = s_obj.custom_regex or build_regex_pattern(
                s_obj.effective_aliases,
                matched_title=s_obj.matched_title,
            )

        res.append({
            "id": item.id,
            "monitored_id": item.monitored_id,
            "show_name": item.show_name,
            "rule_name": item.rule_name,
            "feed_name": item.feed_name,
            "release_title": item.release_title,
            "episode": item.episode,
            "created_at": created_str,
            "matched_regex": matched_reg,
        })
    return res


@router.delete("/history")
def clear_match_history(session: Session = Depends(get_db)):
    items = session.exec(select(MatchHistory)).all()
    for item in items:
        session.delete(item)
    session.commit()
    return {"status": "success", "message": "Match history cleared"}

