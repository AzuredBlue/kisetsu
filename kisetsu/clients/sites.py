"""Searching a feed's own site for releases that have scrolled out of its RSS.

A feed lists only its newest items. When Kisetsu or the feed was down for a while, the
episodes released meanwhile can be gone from it. For feeds on nyaa and SubsPlease the
site's search finds them again.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx2

from kisetsu.clients.rss import FeedArticle, fetch_bytes, magnet_parts, normalize_info_hash, parse_date, parse_feed

# Search terms per show.
MAX_TERMS = 3

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


def search_terms(titles: Iterable[Optional[str]]) -> List[str]:
    """Up to MAX_TERMS short names to search for, from the most specific title first.

    The site's search is broad on purpose; every result is matched against the show again
    before it is kept, so a loose term costs nothing but a few discarded rows.
    """
    terms: List[str] = []
    seen = set()
    for title in titles:
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


def site_of(feed_url: str) -> Optional[str]:
    """'nyaa' or 'subsplease' for a feed on a site that can be searched, else None."""
    parts = urlparse(feed_url)
    host = (parts.hostname or "").lower()
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if host in {"nyaa.si", "sukebei.nyaa.si"} and query.get("page") == "rss":
        return "nyaa"
    if host.endswith("subsplease.org") and parts.path.startswith("/rss"):
        return "subsplease"
    return None


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


def subsplease_search_url(term: str) -> str:
    return f"https://subsplease.org/api/?f=search&tz=UTC&s={quote(term)}"


def subsplease_resolution(feed_url: str) -> str:
    for key, value in parse_qsl(urlparse(feed_url).query, keep_blank_values=True):
        if key == "r" and value:
            return value
    return "1080"


def parse_subsplease_search(payload: Any, resolution: str, now: Optional[datetime] = None) -> List[FeedArticle]:
    """SubsPlease search results as feed articles.

    The guids are the ones the RSS uses (the magnet's hash), so an item found by both is one
    item. The API's ``release_date`` runs some hours later than the RSS's (seen: 7h), which
    is harmless for the day-sized checks it feeds, but it must never be in the future, so it
    is capped at ``now``.
    """
    articles: List[FeedArticle] = []
    if not isinstance(payload, dict):
        return articles
    now = now or datetime.now(timezone.utc)
    for entry in payload.values():
        for download in (entry or {}).get("downloads") or []:
            if str(download.get("res")) != resolution:
                continue
            magnet = download.get("magnet") or ""
            btih, name = magnet_parts(magnet)
            if not btih or not name:
                continue
            released = parse_date((entry or {}).get("release_date"))
            articles.append(FeedArticle(
                guid=btih,
                title=name,
                torrent_url=magnet,
                published_at=min(released, now) if released else None,
                info_hash=normalize_info_hash(btih),
            ))
    return articles


async def search_feed(
    client: httpx2.AsyncClient,
    feed_url: str,
    term: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[List[FeedArticle]]:
    """The feed's releases for ``term``; None when this kind of feed cannot be searched."""
    site = site_of(feed_url)
    if site == "nyaa":
        return parse_feed(await fetch_bytes(client, nyaa_search_url(feed_url, term)))
    if site == "subsplease":
        body = await fetch_bytes(client, subsplease_search_url(term))
        try:
            payload = json.loads(body)
        except ValueError:
            return []
        return parse_subsplease_search(payload, subsplease_resolution(feed_url), now)
    return None
