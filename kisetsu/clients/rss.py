"""Fetching and parsing RSS feeds directly, without qBittorrent."""

import asyncio
import base64
import binascii
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlparse
from zoneinfo import ZoneInfo

import httpx2

from kisetsu.clients import traffic

USER_AGENT = "Kisetsu (feed reader)"
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 8 * 1024 * 1024
# Requests to one host (feed reads and searches alike) leave at least this long between them, so a burst such as a first
# run's searches never trips the site's rate limit.
MIN_REQUEST_INTERVAL_SECONDS = 1.0
# A 429 is still honoured: one naming no wait leaves the host alone this long, it is retried this many
# times, and a wait longer than the most is not sat out.
RATE_LIMIT_WAIT_SECONDS = 10.0
RATE_LIMIT_RETRIES = 2
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0
# A host that refuses or cannot be found (no network yet at boot, say) is tried again after each of these waits.
CONNECT_RETRY_WAITS = (2.0, 4.0, 8.0)

# SubsPlease's RSS writes its pubDate in US Pacific time but marks it +0000.
_PACIFIC = ZoneInfo("America/Los_Angeles")

_HEX_HASH = re.compile(r"^[0-9a-fA-F]{40}$")
_BASE32_HASH = re.compile(r"^[A-Za-z2-7]{32}$")


class FeedFetchError(Exception):
    """A feed could not be read; the message says why in terms a user can act on.

    ``transient`` when the cause may pass on its own: no connection, a timeout or a server error.
    """

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


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


def parse_date(text: Optional[str], wall_zone: Optional[tzinfo] = None) -> Optional[datetime]:
    """A feed date as aware UTC. Dates without a weekday or with a '-0000' zone are accepted.

    With ``wall_zone`` the stated zone is ignored and the time is read as a wall time there.
    """
    if not text:
        return None
    try:
        value = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if wall_zone is not None:
        return value.replace(tzinfo=wall_zone).astimezone(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def feed_wall_zone(url: str) -> Optional[tzinfo]:
    """The zone a feed's dates are really in when it marks them wrongly, else None."""
    host = (urlparse(url).hostname or "").lower()
    return _PACIFIC if host == "subsplease.org" or host.endswith(".subsplease.org") else None


def parse_feed(body: bytes, wall_zone: Optional[tzinfo] = None) -> List[FeedArticle]:
    """The items of an RSS 2.0 body, as nyaa and SubsPlease write them. ``wall_zone``: see ``parse_date``."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedFetchError(f"The feed is not valid XML ({e})") from e
    if _local(root.tag).lower() != "rss":
        # An error page or another format would read as a feed with no items, and the feed as healthy.
        raise FeedFetchError(f"The address answers with <{_local(root.tag)}>, which is not an RSS feed")
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
        articles.append(FeedArticle(guid, title, torrent_url, parse_date(fields.get("pubDate"), wall_zone), info_hash))
    return articles


class HostGate:
    """Spaces the requests to each host, and holds back every request to a host that answered 429 until the wait it asked for is over.

    Each request takes the next free slot of its host before it waits, so requests started together still go one interval apart.
    """

    def __init__(self, sleep=asyncio.sleep, clock=time.monotonic, interval: float = MIN_REQUEST_INTERVAL_SECONDS):
        self._sleep = sleep
        self._clock = clock
        self.interval = interval
        self._open_at: Dict[str, float] = {}
        self._next_slot: Dict[str, float] = {}

    async def wait(self, host: str) -> None:
        now = self._clock()
        remaining = self._open_at.get(host, 0.0) - now
        if remaining > MAX_RATE_LIMIT_WAIT_SECONDS:
            raise FeedFetchError(f"{host} is rate limiting requests; it can be asked again in {remaining:.0f}s")
        slot = max(now, self._open_at.get(host, 0.0), self._next_slot.get(host, 0.0))
        self._next_slot[host] = slot + self.interval
        if slot > now:
            await self._sleep(slot - now)

    def close(self, host: str, seconds: float) -> None:
        self._open_at[host] = max(self._open_at.get(host, 0.0), self._clock() + seconds)


_host_gate = HostGate()


def _retry_after(response: httpx2.Response) -> float:
    """The seconds a 429 asks to wait: its Retry-After, in seconds or as a date, else the default."""
    value = (response.headers.get("Retry-After") or "").strip()
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    return RATE_LIMIT_WAIT_SECONDS


async def _read_capped(response: httpx2.Response) -> Optional[bytes]:
    """The response's body, or None once it has grown past ``MAX_BODY_BYTES``; the rest is then left unread."""
    chunks: List[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_bytes(client: httpx2.AsyncClient, url: str, kind: str = "Feed read") -> bytes:
    """GET ``url``, waiting out and retrying a 429 as long as its host asks, and for a feed read a refused
    connection a few times (``CONNECT_RETRY_WAITS``). Every failure becomes a FeedFetchError.

    Each attempt is counted under ``kind`` (see ``traffic``).
    """
    host = urlparse(url).hostname or url
    connect_waits = iter(CONNECT_RETRY_WAITS if kind == "Feed read" else ())
    rate_limited = 0
    while True:
        await _host_gate.wait(host)
        try:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as response:
                body = await _read_capped(response)
        except httpx2.TimeoutException as e:
            traffic.record(kind, host, traffic.ERROR)
            raise FeedFetchError(f"{host} did not answer in time", transient=True) from e
        except httpx2.ConnectError as e:
            traffic.record(kind, host, traffic.ERROR)
            wait = next(connect_waits, None)
            if wait is None:
                raise FeedFetchError(f"{host} could not be reached ({e})", transient=True) from e
            _host_gate.close(host, wait)
            continue
        except httpx2.RequestError as e:
            traffic.record(kind, host, traffic.ERROR)
            raise FeedFetchError(f"{host} could not be reached ({e})", transient=True) from e
        traffic.record(kind, host, traffic.outcome_of(response.status_code))
        if response.status_code != 429:
            break
        wait = _retry_after(response)
        _host_gate.close(host, wait)
        if rate_limited == RATE_LIMIT_RETRIES or wait > MAX_RATE_LIMIT_WAIT_SECONDS:
            raise FeedFetchError(f"{host} is rate limiting requests")
        rate_limited += 1
    if response.status_code >= 400:
        raise FeedFetchError(f"{host} answered HTTP {response.status_code}", transient=response.status_code >= 500)
    if body is None:
        raise FeedFetchError(f"{host} sent a response larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
    return body


async def fetch_feed(client: httpx2.AsyncClient, url: str) -> List[FeedArticle]:
    """Fetch and parse one feed."""
    return parse_feed(await fetch_bytes(client, url), feed_wall_zone(url))


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
