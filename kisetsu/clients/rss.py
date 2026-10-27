"""Fetching and parsing RSS feeds directly, without qBittorrent."""

import asyncio
import base64
import binascii
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlparse

import httpx2

USER_AGENT = "Kisetsu (feed reader)"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 8 * 1024 * 1024
# Minimum gap between two requests to the same host, as in qBittorrent's RSS settings.
SAME_HOST_DELAY_SECONDS = 2.0

_HEX_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32_HASH = re.compile(r"^[A-Za-z2-7]{32}$")


class FeedFetchError(Exception):
    """A feed could not be read; the message says why in terms a user can act on."""


@dataclass(frozen=True)
class FeedArticle:
    guid: str
    title: str
    torrent_url: Optional[str]
    published_at: Optional[datetime]
    info_hash: Optional[str] = None


def normalize_info_hash(value: Optional[str]) -> Optional[str]:
    """A 40-character lowercase hex info hash from hex or base32, or None."""
    text = (value or "").strip()
    if _HEX_HASH.match(text):
        return text.lower()
    if _BASE32_HASH.match(text):
        try:
            return base64.b32decode(text.upper()).hex()
        except (binascii.Error, ValueError):
            return None
    return None


def magnet_parts(magnet: str):
    """(info hash as written, display name) from a magnet link."""
    query = dict(parse_qsl(urlparse(magnet).query, keep_blank_values=True))
    btih = (query.get("xt") or "").rpartition(":")[2] or None
    return btih, unquote(query.get("dn", "")) or None


def parse_date(text: Optional[str]) -> Optional[datetime]:
    """A feed date as aware UTC. Dates without a weekday or with a '-0000' zone are accepted."""
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(body: bytes) -> List[FeedArticle]:
    """The items of an RSS 2.0 body, as nyaa and SubsPlease write them."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedFetchError(f"The feed is not valid XML ({e})") from e
    articles: List[FeedArticle] = []
    for item in root.iter("item"):
        fields: Dict[str, str] = {}
        enclosure: Optional[str] = None
        for child in item:
            name = _local(child.tag)
            if name == "enclosure":
                enclosure = child.attrib.get("url")
            else:
                fields.setdefault(name, (child.text or "").strip())
        title = fields.get("title")
        link = fields.get("link") or enclosure
        if not title or not link:
            continue
        guid = fields.get("guid") or link
        torrent_url = enclosure or link
        info_hash = normalize_info_hash(fields.get("infoHash"))
        if info_hash is None and torrent_url.startswith("magnet:"):
            info_hash = normalize_info_hash(magnet_parts(torrent_url)[0])
        if info_hash is None:
            info_hash = normalize_info_hash(guid)
        articles.append(FeedArticle(guid, title, torrent_url, parse_date(fields.get("pubDate")), info_hash))
    return articles


class HostDelay:
    """Spaces out requests to one host, however many are waiting at once."""

    def __init__(self, seconds: float = SAME_HOST_DELAY_SECONDS):
        self.seconds = seconds
        self._next_slot: Dict[str, float] = {}

    async def wait(self, host: str) -> None:
        now = time.monotonic()
        slot = max(now, self._next_slot.get(host, now))
        self._next_slot[host] = slot + self.seconds
        if slot > now:
            await asyncio.sleep(slot - now)


_host_delay = HostDelay()


async def fetch_bytes(client: httpx2.AsyncClient, url: str) -> bytes:
    """GET ``url``, at most one request per SAME_HOST_DELAY_SECONDS to its host. Every failure becomes a FeedFetchError."""
    host = urlparse(url).hostname or url
    await _host_delay.wait(host)
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    except httpx2.TimeoutException as e:
        raise FeedFetchError(f"{host} did not answer in time") from e
    except httpx2.RequestError as e:
        raise FeedFetchError(f"{host} could not be reached ({e})") from e
    if response.status_code == 429:
        raise FeedFetchError(f"{host} is rate limiting requests")
    if response.status_code >= 400:
        raise FeedFetchError(f"{host} answered HTTP {response.status_code}")
    body = response.content
    if len(body) > MAX_BODY_BYTES:
        raise FeedFetchError(f"{host} sent a response larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    return body


async def fetch_feed(client: httpx2.AsyncClient, url: str) -> List[FeedArticle]:
    """Fetch and parse one feed."""
    return parse_feed(await fetch_bytes(client, url))


def new_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
    )


class Backoff:
    """Per-key exponential backoff, so a failing feed is retried less and less often."""

    def __init__(self, base: timedelta = timedelta(minutes=1), cap: timedelta = timedelta(minutes=30)):
        self.base = base
        self.cap = cap
        self._failures: Dict[str, int] = {}
        self._retry_at: Dict[str, datetime] = {}

    def failed(self, key: str, now: datetime) -> datetime:
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        delay = min(self.base * (2 ** (count - 1)), self.cap)
        self._retry_at[key] = now + delay
        return self._retry_at[key]

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
        self._retry_at.pop(key, None)

    def ready(self, key: str, now: datetime) -> bool:
        return now >= self._retry_at.get(key, now)
