"""Search the feeds' own sites for releases that qBittorrent's RSS no longer holds.

A feed only lists its newest items. When Kisetsu or qBittorrent was off for a
while, the episodes released meanwhile can have scrolled out of it and would
never be seen. For each show that still has an aired, unfetched episode this
asks the feed's site for that show's releases directly (nyaa's search RSS,
SubsPlease's search API) and puts what it finds into the feed cache, keyed by
the feed's own URL, so the grabber reads them like any other feed item.
"""

import base64
import binascii
import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

import httpx2
from sqlalchemy import func
from sqlmodel import Session, select

from kisetsu.core import feedcache
from kisetsu.core.discovery import parse_article_date
from kisetsu.core.grabber import episodes_in_cache
from kisetsu.db.models import Episode, EpisodeStatus, Feed, Monitored, MonitoredStatus, utc_now
from kisetsu.db.session import get_settings

logger = logging.getLogger("kisetsu.core.backfill")

# Seconds between two requests to the same site.
REQUEST_GAP_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 15.0
# A show with outstanding episodes is searched again after this long.
REPEAT_SECONDS = 6 * 3600
MAX_TERMS = 3
_USER_AGENT = "Kisetsu (feed backfill)"

Fetch = Callable[[str], str]


class BackfillAbort(Exception):
    """The site asked us to slow down or is failing; stop searching for now."""


# --- search terms ----------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"\s*[:：–—]\s*|\s+-\s+")
_MARKERS = re.compile(
    r"\b(?:the\s+)?final\s+season\b"
    r"|\b(?:season|stage|part|cour|s)\s*\d+\b"
    r"|\b\d+(?:st|nd|rd|th)(?:\s+(?:season|stage|part|cour))?\b"
    r"|\b(?:season|stage|part|cour)\b",
    re.IGNORECASE,
)
_PUNCTUATION = re.compile(r"[^\w\s']", re.UNICODE)


def _clean_segment(segment: str) -> str:
    segment = _MARKERS.sub(" ", segment)
    segment = _PUNCTUATION.sub(" ", segment)
    return " ".join(segment.split())


def search_terms(show: Monitored) -> List[str]:
    """Up to MAX_TERMS short names to search for, most specific source first.

    The feed's own search is broad on purpose; every result is matched against
    the show again before it is kept, so a loose term costs nothing but a few
    discarded rows.
    """
    terms: List[str] = []
    seen: set = set()
    for title in (show.matched_title, show.title_romaji, show.title_english):
        if not title:
            continue
        for segment in _SEGMENT_SPLIT.split(title):
            term = _clean_segment(segment)
            words = term.split()
            if not (len(words) >= 2 or (len(words) == 1 and len(words[0]) >= 4)):
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(term)
    return terms[:MAX_TERMS]


# --- feed adapters -----------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def nyaa_search_url(feed_url: str, term: str) -> str:
    """The feed's own URL with ``term`` added to its search, keeping user, category and filter."""
    parts = urlparse(feed_url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    for index, (key, value) in enumerate(pairs):
        if key == "q":
            pairs[index] = (key, f"{value} {term}".strip())
            break
    else:
        pairs.append(("q", term))
    return urlunparse(parts._replace(query=urlencode(pairs)))


def parse_nyaa_rss(body: str) -> List[Dict[str, Any]]:
    """Nyaa RSS items in the shape qBittorrent's RSS articles have."""
    root = ET.fromstring(body)
    articles: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        fields = {_local(child.tag): (child.text or "").strip() for child in item}
        title = fields.get("title")
        link = fields.get("link")
        if not title or not link:
            continue
        article: Dict[str, Any] = {
            "id": fields.get("guid") or link,
            "title": title,
            "link": link,
            "torrentURL": link,
        }
        if fields.get("pubDate"):
            article["date"] = fields["pubDate"]
        if fields.get("infoHash"):
            article["infoHash"] = fields["infoHash"]
        if fields.get("size"):
            article["size"] = fields["size"]
        articles.append(article)
    return articles


def _subsplease_resolution(feed_url: str) -> str:
    for key, value in parse_qsl(urlparse(feed_url).query, keep_blank_values=True):
        if key == "r" and value:
            return value
    return "1080"


def subsplease_search_url(term: str) -> str:
    return f"https://subsplease.org/api/?f=search&tz=UTC&s={quote(term)}"


def _magnet_parts(magnet: str) -> Tuple[Optional[str], Optional[str]]:
    query = dict(parse_qsl(urlparse(magnet).query, keep_blank_values=True))
    btih = (query.get("xt") or "").rpartition(":")[2] or None
    return btih, unquote(query.get("dn", "")) or None


def _info_hash_hex(btih: str) -> Optional[str]:
    if len(btih) == 40:
        return btih.lower()
    try:
        return base64.b32decode(btih.upper()).hex()
    except (binascii.Error, ValueError):
        return None


def parse_subsplease_search(payload: Any, resolution: str, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """SubsPlease search results in the shape its RSS items have.

    The ids are the ones the RSS uses (the magnet's hash), so an item found by
    both is one item. The API's ``release_date`` runs some hours later than the
    RSS's (seen: 7h), which is harmless for the day-sized checks it feeds, but it
    must never be in the future, so it is capped at ``now``.
    """
    articles: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return articles
    now = now or utc_now()
    for entry in payload.values():
        for download in (entry or {}).get("downloads") or []:
            if str(download.get("res")) != resolution:
                continue
            magnet = download.get("magnet") or ""
            btih, name = _magnet_parts(magnet)
            if not btih or not name:
                continue
            article: Dict[str, Any] = {"id": btih, "title": name, "link": magnet, "torrentURL": magnet}
            info_hash = _info_hash_hex(btih)
            if info_hash:
                article["infoHash"] = info_hash
            released = parse_article_date({"date": entry.get("release_date")})
            if released.year > 2000:
                article["date"] = format_datetime(min(released, now))
            articles.append(article)
    return articles


def _site_of(feed_url: str) -> Optional[str]:
    parts = urlparse(feed_url)
    host = (parts.hostname or "").lower()
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if host in {"nyaa.si", "sukebei.nyaa.si"} and query.get("page") == "rss":
        return "nyaa"
    if host.endswith("subsplease.org") and parts.path.startswith("/rss"):
        return "subsplease"
    return None


def search_feed(feed: Feed, term: str, fetch: Fetch) -> Optional[List[Dict[str, Any]]]:
    """The feed's releases for ``term``; None when this kind of feed cannot be searched."""
    site = _site_of(feed.qbit_feed_url)
    if site == "nyaa":
        return parse_nyaa_rss(fetch(nyaa_search_url(feed.qbit_feed_url, term)))
    if site == "subsplease":
        payload = json.loads(fetch(subsplease_search_url(term)))
        return parse_subsplease_search(payload, _subsplease_resolution(feed.qbit_feed_url))
    return None


def http_fetch(url: str) -> str:
    try:
        with httpx2.Client(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = client.get(url)
    except httpx2.RequestError as e:
        raise BackfillAbort(f"{urlparse(url).hostname} could not be reached: {e}") from e
    if response.status_code == 429 or response.status_code >= 500:
        raise BackfillAbort(f"{urlparse(url).hostname} answered HTTP {response.status_code}")
    response.raise_for_status()
    return response.text


# --- which shows are due -----------------------------------------------------

# show id -> (what was searched, monotonic time of the search)
_searched: Dict[int, Tuple[int, float]] = {}
_searched_lock = threading.Lock()


@dataclass
class _Due:
    show: Monitored
    feeds: List[Feed]
    terms: List[str]
    missing: List[int]
    signature: int


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _searchable_feeds(show: Monitored, feeds: List[Feed]) -> List[Feed]:
    if show.feed_is_locked:
        # A locked show reads one feed; searching others would only fill the cache with
        # releases it may not use.
        wanted = show.learned_feed_id if show.learned_feed_id is not None else show.current_feed_id
        feeds = [f for f in feeds if f.id == wanted][:1]
    return [f for f in feeds if _site_of(f.qbit_feed_url)]


def backfill_due(
    session: Session,
    *,
    now: Optional[datetime] = None,
    show_id: Optional[int] = None,
    force: bool = False,
) -> List[_Due]:
    """Shows with an aired episode of the current season that is still wanted and that no cached feed item can supply.

    Searching is only for what the feeds have lost. An episode the cache already
    holds a usable release for is taken from there by the next check, so it never
    triggers a search; neither does one that is queued, downloading or done.
    """
    settings = get_settings(session)
    now = _naive_utc(now or utc_now())
    feeds = session.exec(select(Feed).order_by(Feed.priority, Feed.id)).all()
    if not feeds:
        return []

    outstanding: Dict[int, List[int]] = {}
    for monitored_id, number in session.exec(
        select(Episode.monitored_id, Episode.episode_number).where(
            Episode.status.in_([EpisodeStatus.WANTED, EpisodeStatus.MISSED]),
            Episode.air_at.is_not(None),
            Episode.air_at <= now,
        ).order_by(Episode.episode_number)
    ).all():
        outstanding.setdefault(monitored_id, []).append(number)
    if not outstanding:
        return []

    query = select(Monitored).where(
        Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED, MonitoredStatus.STALLED]),
        Monitored.id.in_(list(outstanding)),
    )
    if show_id is not None:
        query = query.where(Monitored.id == show_id)
    shows = session.exec(query).all()
    if not shows:
        return []

    cache = feedcache.cached_articles(session)
    due: List[_Due] = []
    monotonic = time.monotonic()
    for show in shows:
        terms = search_terms(show)
        show_feeds = _searchable_feeds(show, feeds)
        if not terms or not show_feeds:
            continue
        covered = episodes_in_cache(session, show, feeds, cache, settings, now=now.replace(tzinfo=timezone.utc))
        missing = [number for number in outstanding[show.id] if number not in covered]
        if not missing:
            continue
        signature = hash((tuple(terms), tuple(f.id for f in show_feeds), tuple(missing)))
        with _searched_lock:
            last = _searched.get(show.id)
        if not force and last and last[0] == signature and monotonic - last[1] < REPEAT_SECONDS:
            continue
        due.append(_Due(show, show_feeds, terms, missing, signature))
    return due


# --- date gate -----------------------------------------------------------------

# A search reaches into history, where the show's earlier seasons live. Their
# releases can carry a season or episode number that fits this show, so the
# publish date decides: nothing from before the show's first episode is kept.
FUTURE_SLACK = timedelta(days=1)


def date_gate(
    articles: List[Dict[str, Any]],
    first_air: Optional[datetime],
    now: datetime,
) -> List[Dict[str, Any]]:
    """The articles that are dated and could belong to a show that premiered at ``first_air``.

    Undated ones are dropped: a search cannot vouch for them. Without a premiere
    time (nothing scheduled) only the future check applies.
    """
    cutoff = feedcache.premiere_cutoff(first_air) if first_air else None
    kept = []
    for article in articles:
        published = parse_article_date(article)
        if published.year <= 2000:
            continue
        published = published.astimezone(timezone.utc).replace(tzinfo=None)
        if published > now + FUTURE_SLACK:
            continue
        if cutoff and published < cutoff:
            continue
        kept.append(article)
    return kept


def _first_air(session: Session, show: Monitored) -> Optional[datetime]:
    return session.exec(
        select(func.min(Episode.air_at)).where(Episode.monitored_id == show.id, Episode.air_at.is_not(None))
    ).first()


def missing_episodes(engine, show_id: int) -> List[int]:
    """The episodes of one show that a forced search would look for (empty: nothing is missing)."""
    with Session(engine) as session:
        due = backfill_due(session, show_id=show_id, force=True)
    return due[0].missing if due else []


# --- running -------------------------------------------------------------------

def run_backfill(
    engine,
    *,
    show_id: Optional[int] = None,
    force: bool = False,
    fetch: Optional[Fetch] = None,
    pause: Optional[Callable[[float], None]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, int]:
    """Search for the due shows' releases; returns ``{show name: rows added}`` for the ones that found some."""
    fetch = fetch or http_fetch
    pause = time.sleep if pause is None else pause
    found: Dict[str, int] = {}
    with Session(engine) as session:
        due = backfill_due(session, now=now, show_id=show_id, force=force)
        if not due:
            return found
        followed = feedcache._followed(session)
        naive_now = _naive_utc(now or utc_now())
        requests_made = 0
        for item in due:
            name = item.show.display_name
            logger.info(
                f"Searching the feeds for '{name}' episode(s) "
                f"{', '.join(str(n) for n in item.missing)}: not in any feed's current items."
            )
            articles_by_url: Dict[str, List[Dict[str, Any]]] = {}
            try:
                for feed in item.feeds:
                    for term in item.terms:
                        if requests_made:
                            pause(REQUEST_GAP_SECONDS)
                        requests_made += 1
                        try:
                            articles = search_feed(feed, term, fetch)
                        except BackfillAbort:
                            raise
                        except Exception as e:
                            logger.warning(f"Searching '{feed.qbit_feed_name}' for '{term}' failed: {e}")
                            continue
                        if articles:
                            articles_by_url.setdefault(feed.qbit_feed_url, []).extend(articles)
            except BackfillAbort as e:
                logger.warning(f"Past-release search stopped: {e}")
                break
            first_air = _first_air(session, item.show)
            kept_by_url = {url: date_gate(arts, first_air, naive_now) for url, arts in articles_by_url.items()}
            dropped = sum(len(arts) for arts in articles_by_url.values()) - sum(len(arts) for arts in kept_by_url.values())
            if dropped:
                logger.debug(f"'{name}': {dropped} searched release(s) dropped for their dates")
            added = feedcache.store_matches(
                session, kept_by_url, followed=followed, only_show_ids={item.show.id},
            ) if any(kept_by_url.values()) else 0
            with _searched_lock:
                _searched[item.show.id] = (item.signature, time.monotonic())
            if added:
                found[name] = added
    return found
