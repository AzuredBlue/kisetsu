import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

import qbittorrentapi

from qbit_seasonal_anime.db.session import get_engine, get_settings
from qbit_seasonal_anime.db.models import Monitored, Feed, RuleHistory, MonitoredStatus, RuleOutcome, MatchHistory, Settings, utc_now
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.core.matching import match_release_to_show, prepare_aliases
from qbit_seasonal_anime.core.rules import DEFAULT_MUST_NOT, build_regex_pattern, delete_rule
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval
from qbit_seasonal_anime.server.state import state

router = APIRouter(prefix="/api")
engine = get_engine()
anilist_client = AniListClient()


def get_db():
    with Session(engine) as session:
        yield session


def get_qbit(session: Session = Depends(get_db)) -> QBitClient:
    s = get_settings(session)
    return QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)


@router.get("/shows")
def get_shows(session: Session = Depends(get_db)):
    settings = get_settings(session)
    now = datetime.now(timezone.utc)
    shows = session.exec(select(Monitored).order_by(Monitored.id)).all()
    feeds = {f.id: f.qbit_feed_name for f in session.exec(select(Feed)).all()}
    prefer_english = (getattr(settings, "title_language", "english") == "english")

    result = []
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

        english_title = s.title_english
        romaji_title = s.title_romaji or s.display_name
        effective_display_name = english_title if (prefer_english and english_title) else (romaji_title or s.display_name)

        from qbit_seasonal_anime.core.rules import sanitize_folder_name, compress_home_path
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

        result.append({
            "id": s.id,
            "anilist_id": s.anilist_id,
            "display_name": effective_display_name,
            "title_romaji": romaji_title,
            "title_english": s.title_english,
            "cover_image": s.cover_image,
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
        })
    return result


@router.post("/shows/{show_id}/pause")
def toggle_pause_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

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

        if show.qbit_rule_name:
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

        if show.qbit_rule_name:
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


@router.post("/shows/{show_id}/rediscover")
def rediscover_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    old_feed_id = show.current_feed_id
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning: Could not delete rule '{show.qbit_rule_name}': {e}", "WARNING")

    if old_feed_id:
        hist = RuleHistory(
            monitored_id=show.id,
            feed_id=old_feed_id,
            outcome=RuleOutcome.FALSE_POSITIVE,
            note="Manually reset/rediscovered from WebUI.",
        )
        session.add(hist)

    show.current_feed_id = None
    show.qbit_rule_name = None
    show.matched_title = None
    show.matched_release_group = None
    show.feed_pinned = False
    show.candidate_feed_id = None
    show.candidate_feed_since = None
    show.status = MonitoredStatus.UNCONFIRMED
    session.add(show)
    session.commit()

    state.add_log(f"Reset rule for '{show.display_name}'. Will rediscover on next supervision cycle.", "INFO")
    return {"status": "success", "message": f"Reset rule for '{show.display_name}'. Will rediscover on next cycle."}


def _effective_display_name(show: Monitored, settings: Settings) -> str:
    prefer_english = (getattr(settings, "title_language", "english") == "english")
    return show.title_english if (prefer_english and show.title_english) else (show.title_romaji or show.display_name)


def _rule_download_params(rule: Dict[str, Any], show: Monitored, settings: Settings, display_name: str) -> Dict[str, Any]:
    from qbit_seasonal_anime.core.rules import resolve_save_path, sanitize_folder_name

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
    from qbit_seasonal_anime.core.discovery import flatten_rss_articles

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


@router.get("/shows/{show_id}/rule")
def get_show_rule_details(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    feed = session.get(Feed, show.current_feed_id) if show.current_feed_id else None

    qbit_rule_data = {}
    matched_articles = []
    if show.qbit_rule_name:
        try:
            qrules = qbit.get_rss_rules()
            qbit_rule_data = qrules.get(show.qbit_rule_name, {})
            articles_resp = qbit.get_matching_articles(show.qbit_rule_name)
            if isinstance(articles_resp, dict):
                for k, v in articles_resp.items():
                    if isinstance(v, list) and v:
                        matched_articles.extend(v)
            elif isinstance(articles_resp, list):
                matched_articles = articles_resp
        except Exception as e:
            state.add_log(f"Warning fetching rule details from qBit: {e}", "WARNING")

    feed_items: List[Dict[str, Any]] = []
    if feed:
        try:
            from qbit_seasonal_anime.core.discovery import flatten_rss_articles
            rss_tree = qbit.get_rss_items(with_data=True)
            all_cached = flatten_rss_articles(rss_tree)
            feed_items = all_cached.get(feed.qbit_feed_url, [])
        except Exception as e:
            state.add_log(f"Warning fetching cached feed articles: {e}", "DEBUG")

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
            show.aliases,
            matched_title=show.matched_title,
            release_group=show.matched_release_group,
        )

    if not saved_regex and feed:
        saved_regex = build_regex_pattern(show.aliases)

    if not matched_articles and feed_items and saved_regex:
        try:
            import re
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

    effective_display_name = _effective_display_name(show, settings)

    from qbit_seasonal_anime.core.rules import build_rule_name, is_show_rule_enabled, is_show_rule_unreleased, compress_home_path
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
        "has_rule": bool(show.current_feed_id or show.qbit_rule_name or show.status == MonitoredStatus.COMPLETED or saved_regex),
        "rule_name": show.qbit_rule_name or expected_rule_name,
        "enabled": rule_is_enabled,
        "feed_name": feed.qbit_feed_name if feed else None,
        "feed_url": feed.qbit_feed_url if feed else None,
        "must_contain": saved_regex,
        "must_not_contain": saved_must_not,
        "save_path": compress_home_path(download_params["save_path"]),
        "save_folder": download_params["custom_save_folder"],
        "current_feed_id": show.current_feed_id or 0,
        "category": download_params["category"],
        "ratio_limit": download_params["ratio_limit"],
        "status": show.status.value,
        "matched_title": show.matched_title,
        "matched_release_group": show.matched_release_group,
        "matched_articles": matched_articles[:15],
        "has_learned_pattern": has_learned_pattern,
        "is_upcoming": is_upcoming,
        "feed_pinned": bool(show.feed_pinned),
        "candidate_feed_id": show.candidate_feed_id or 0,
        "candidate_feed_name": feeds_map_name(session, show.candidate_feed_id),
        "candidate_feed_since": show.candidate_feed_since.isoformat() if show.candidate_feed_since else None,
    }


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
            url=url,
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


class EditShowRequest(BaseModel):
    current_feed_id: Optional[int] = None
    save_folder: Optional[str] = None
    category: Optional[str] = None
    ratio_limit: Optional[float] = None
    must_contain: Optional[str] = None
    must_not_contain: Optional[str] = None


@router.post("/shows/{show_id}/edit")
def edit_show(show_id: int, req: EditShowRequest, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    settings = get_settings(session)
    old_feed_id = show.current_feed_id
    new_feed_id = None if req.current_feed_id is not None and req.current_feed_id <= 0 else (req.current_feed_id if req.current_feed_id is not None else show.current_feed_id)

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

    if req.current_feed_id is not None:
        # An explicit pick pins the feed; auto-detect must not move it afterwards.
        show.feed_pinned = req.current_feed_id > 0
        show.candidate_feed_id = None
        show.candidate_feed_since = None

    if show.status == MonitoredStatus.COMPLETED:
        if req.current_feed_id is not None and req.current_feed_id > 0:
            show.current_feed_id = req.current_feed_id
        session.add(show)
        session.commit()
        return {"status": "success", "message": f"Updated metadata for completed show '{show.display_name}'."}

    category = req.category.strip() if req.category is not None and req.category.strip() else settings.default_category
    ratio_limit = req.ratio_limit if req.ratio_limit is not None and req.ratio_limit >= 0 else settings.default_seed_ratio

    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning deleting old rule '{show.qbit_rule_name}': {e}", "WARNING")
        show.qbit_rule_name = None

    if new_feed_id is None:
        show.current_feed_id = None
        show.matched_title = None
        show.matched_release_group = None
        show.feed_pinned = False
        show.candidate_feed_id = None
        show.candidate_feed_since = None
        show.status = MonitoredStatus.UNCONFIRMED
        session.add(show)
        session.commit()
        state.add_log(f"Reset '{show.display_name}' to Auto-Discover mode.", "INFO")
        return {"status": "success", "message": f"'{show.display_name}' set to Auto-Discover."}

    feed = session.get(Feed, new_feed_id)
    if not feed:
        raise HTTPException(status_code=400, detail="Selected feed not found")

    show.current_feed_id = feed.id

    matched_article = None
    aliases = show.aliases
    test_pattern = build_regex_pattern(aliases)
    prepared_aliases = prepare_aliases(aliases)
    parsed_articles = {}
    try:
        rss_data = qbit.get_rss_items(with_data=True)
        from qbit_seasonal_anime.core.discovery import flatten_rss_articles
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

    from qbit_seasonal_anime.core.rules import create_or_update_rule
    if matched_article and best_parsed:
        show.matched_title = best_parsed.get("title")
        show.matched_release_group = best_parsed.get("release_group")
        show.status = MonitoredStatus.FIXED
        if req.must_contain is None:
            show.custom_regex = None
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}' and matched cached release: {matched_article.get('title')} (Status: Working)"
    else:
        show.matched_title = None
        show.matched_release_group = None
        show.status = MonitoredStatus.UNCONFIRMED
        rname = create_or_update_rule(
            qbit_client=qbit,
            monitored=show,
            feed=feed,
            base_dir=settings.base_dir,
            category=category,
            ratio_limit=ratio_limit,
            must_contain=show.custom_regex,
            must_not_contain=show.custom_must_not,
        )
        show.qbit_rule_name = rname
        msg = f"Assigned to '{feed.qbit_feed_name}'. Rule created in Testing mode, waiting for next episode drop."

    session.add(show)
    session.commit()
    state.add_log(f"Show '{show.display_name}': {msg}", "INFO")
    return {"status": "success", "message": msg}


@router.delete("/shows/{show_id}")
def delete_show(show_id: int, session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    show = session.get(Monitored, show_id)
    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    name = show.display_name
    if show.qbit_rule_name:
        try:
            delete_rule(qbit, show.qbit_rule_name)
        except Exception as e:
            state.add_log(f"Warning deleting rule for '{name}': {e}", "WARNING")

    for h in session.exec(select(RuleHistory).where(RuleHistory.monitored_id == show.id)).all():
        session.delete(h)
    session.flush()
    session.delete(show)
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
    from qbit_seasonal_anime.core.rules import compress_home_path
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


@router.post("/settings")
def update_settings(req: UpdateSettingsRequest, session: Session = Depends(get_db)):
    s = get_settings(session)
    if req.qbit_host is not None:
        s.qbit_host = req.qbit_host.strip()
    if req.qbit_username is not None:
        s.qbit_username = req.qbit_username.strip()
    if req.qbit_password is not None and req.qbit_password != "":
        s.qbit_password = req.qbit_password
    if req.base_dir is not None:
        raw_base = req.base_dir.strip()
        if raw_base and "{name}" not in raw_base:
            raw_base = f"{raw_base.rstrip('/\\')}/{{name}}"
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

    session.add(s)
    session.commit()
    state.add_log("Settings updated successfully.", "INFO")
    return {"status": "success", "message": "Settings updated."}


@router.post("/settings/test-qbit")
def test_qbit_connection(session: Session = Depends(get_db)):
    s = get_settings(session)
    client = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=6)
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

    qbit = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)
    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)
    try:
        logs = []
        logs.extend(sup.sync_feeds())
        sync_logs = await sup.sync_anilist_schedule(force=True)
        logs.extend(sync_logs)
        bootstrap_logs = sup.bootstrap_unassigned_shows()
        logs.extend(bootstrap_logs)
        from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents
        confirm_logs = verify_and_confirm_torrents(session, qbit, s)
        logs.extend(confirm_logs)
        rollover_logs = sup.reconcile_schedule_rollover()
        logs.extend(rollover_logs)

        for l in logs:
            state.add_log(l, "INFO")

        msg = sync_logs[0] if sync_logs else "AniList synced."
        if bootstrap_logs:
            msg += f" Discovered feeds and created rules for {len(bootstrap_logs)} shows."
        return {"status": "success", "logs": logs, "message": msg}
    except Exception as e:
        state.add_log(f"AniList sync error: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/settings/clear-all")
def clear_all_monitored(session: Session = Depends(get_db), qbit: QBitClient = Depends(get_qbit)):
    shows = session.exec(select(Monitored)).all()
    count = len(shows)
    try:
        qrules = qbit.get_rss_rules()
        for rname in qrules:
            if (
                rname.startswith("[Seasonal]")
                or rname.startswith("[qbit-seasonal-anime]")
                or any(show.qbit_rule_name == rname for show in shows)
            ):
                delete_rule(qbit, rname)
    except Exception as e:
        state.add_log(f"Warning clearing qbit rules: {e}", "WARNING")

    for h in session.exec(select(RuleHistory)).all():
        session.delete(h)
    session.flush()

    for s in shows:
        session.delete(s)
    session.commit()

    state.add_log(f"Cleared all {count} monitored shows and deleted qBittorrent rules.", "WARNING")
    return {"status": "success", "message": f"Cleared all {count} shows."}


@router.post("/cycle/run")
async def run_cycle_now(session: Session = Depends(get_db)):
    if state.is_running_cycle:
        return {"status": "busy", "message": "A supervision cycle is already in progress."}

    s = get_settings(session)
    qbit = QBitClient(host=s.qbit_host, username=s.qbit_username, password=s.qbit_password, timeout=10)

    # Probe first so the button does not launch a cycle that can only fail.
    try:
        await asyncio.to_thread(qbit.test_connection)
    except QbitClientError as e:
        state.add_log(f"Manual cycle skipped: qBittorrent is unavailable: {e}", "ERROR")
        raise HTTPException(status_code=503, detail=f"qBittorrent is unavailable: {e}")

    sup = Supervisor(session=session, qbit=qbit, anilist=anilist_client, settings=s)

    state.is_running_cycle = True
    state.add_log("Manual supervision cycle initiated from WebUI.", "INFO")
    state.trigger_immediate_rule_check()
    try:
        logs = await sup.run_full_cycle()
        state.last_cycle_time = datetime.now(timezone.utc)
        for l in logs:
            state.add_log(f"Supervisor: {l}", "INFO")

        default_interval = max(60, s.refresh_interval_minutes * 60)
        sleep_sec, reason = await asyncio.to_thread(
            calculate_next_poll_interval,
            session,
            default_interval_seconds=default_interval,
            qbit_client=qbit,
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
    except Exception as e:
        state.add_log(f"Error during supervision cycle: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        state.is_running_cycle = False


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
            airing_at = s.next_airing_at
            if airing_at and airing_at.tzinfo is None:
                airing_at = airing_at.replace(tzinfo=timezone.utc)
            is_unreleased = (
                (s.next_airing_episode == 1 or s.next_airing_episode is None)
                and (s.last_confirmed_episode or 0) == 0
                and (airing_at is None or airing_at > now)
            )
            if is_unreleased:
                upcoming += 1
            else:
                testing += 1

    remaining_seconds = 0
    if state.target_next_check_time:
        rem = (state.target_next_check_time - now).total_seconds()
        remaining_seconds = max(0, int(rem))
    elif state.next_check_seconds:
        remaining_seconds = state.next_check_seconds

    return {
        "daemon_active": True,
        "is_running_cycle": state.is_running_cycle,
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
                s_obj.aliases,
                matched_title=s_obj.matched_title,
                release_group=s_obj.matched_release_group,
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

