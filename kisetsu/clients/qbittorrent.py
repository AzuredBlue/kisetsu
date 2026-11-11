"""The qBittorrent WebUI client: torrents, and reading the feed list of its RSS tree (never written)."""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import qbittorrentapi
import requests

from kisetsu.clients import traffic


logger = logging.getLogger("kisetsu.clients.qbittorrent")

CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0
# While qBittorrent is unreachable, calls fail at once for this long, doubling per failure.
UNAVAILABLE_BACKOFF_SECONDS = (30, 60, 120, 300)
# A rejected login is not retried for a while: qBittorrent bans an address after a few failed logins.
AUTH_BACKOFF_SECONDS = 300
# qBittorrent's default save folder is asked again after this long.
DEFAULT_PATH_TTL_SECONDS = 300


class QbitError(Exception):
    """Base class of every error this client raises."""


class QbitUnavailable(QbitError):
    """qBittorrent is unreachable, or this client is backing off after it was."""


class QbitAuthError(QbitError):
    """qBittorrent rejected the configured credentials."""


class QbitConflict(QbitError):
    """qBittorrent answered 409: what the request names is taken or missing."""


class QbitDuplicate(QbitError):
    """qBittorrent already has this torrent."""


class QbitRejected(QbitError):
    """qBittorrent refused to add the torrent."""


class QbitReadOnly(QbitError):
    """A write was attempted on a read-only client (the sandbox)."""


@dataclass(frozen=True)
class Torrent:
    hash: str
    name: str
    state: str
    progress: float
    amount_left: int
    size: int
    save_path: str
    content_path: str
    category: str
    tags: FrozenSet[str]
    added_on: Optional[datetime]
    completion_on: Optional[datetime]
    # When data last moved; None for a torrent that has never been active (qBittorrent reports 0 or less).
    last_activity: Optional[datetime] = None

    @property
    def finished(self) -> bool:
        return self.amount_left == 0 and self.progress >= 1.0

    @property
    def errored(self) -> bool:
        return self.state.lower() in ERROR_STATES

    @property
    def checking(self) -> bool:
        return self.state.lower().startswith("checking") or self.state.lower() == "moving"


ERROR_STATES = {"error", "missingfiles", "unknown"}

_NEEDED_FIELDS = (
    "name", "state", "progress", "amount_left", "size", "save_path", "content_path",
    "category", "tags", "added_on", "completion_on", "last_activity",
)


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _torrent(info_hash: str, raw: Dict[str, Any]) -> Torrent:
    tags = frozenset(tag.strip() for tag in str(raw.get("tags") or "").split(",") if tag.strip())
    return Torrent(
        hash=info_hash.lower(),
        name=str(raw.get("name") or ""),
        state=str(raw.get("state") or "unknown"),
        progress=float(raw.get("progress") or 0.0),
        amount_left=int(raw.get("amount_left") or 0),
        size=int(raw.get("size") or 0),
        save_path=str(raw.get("save_path") or ""),
        content_path=str(raw.get("content_path") or ""),
        category=str(raw.get("category") or ""),
        tags=tags,
        added_on=_timestamp(raw.get("added_on")),
        completion_on=_timestamp(raw.get("completion_on")),
        last_activity=_timestamp(raw.get("last_activity")),
    )


@dataclass(frozen=True)
class SyncResult:
    full: bool
    changed: FrozenSet[str] = frozenset()
    removed: FrozenSet[str] = frozenset()

    def __bool__(self) -> bool:
        return self.full or bool(self.changed) or bool(self.removed)


@dataclass
class RssStoreFeed:
    path: str
    url: str


def _unreachable_text(host: str, e: BaseException) -> str:
    """Why qBittorrent could not be reached, in plain words instead of the transport library's exception dump."""
    text = str(e)
    low = text.lower()
    if isinstance(e, requests.exceptions.Timeout) or "timed out" in low or "read timeout" in low:
        return f"qBittorrent at {host} did not answer in time."
    if "getaddrinfo" in low or "11001" in text or "name or service not known" in low or "nodename nor servname" in low or "failed to resolve" in low:
        return f"The address of {host} could not be found. Check the host name."
    if "refused" in low or "10061" in text or "failed to establish a new connection" in low:
        return f"Nothing answered at {host}. Check that qBittorrent is running with its Web UI on (Options, Web UI) and that the address and port are right."
    if "ssl" in low or "certificate" in low:
        return f"The secure connection to {host} failed. Try http:// instead of https://."
    return f"Cannot reach qBittorrent at {host}: {e}"


def _is_auth_failure(exc: BaseException) -> bool:
    if isinstance(exc, qbittorrentapi.LoginFailed):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return status in (401, 403)


def _default_factory(host: str, username: str, password: str):
    def build() -> qbittorrentapi.Client:
        return qbittorrentapi.Client(
            host=host,
            username=username,
            password=password,
            VERIFY_WEBUI_CERTIFICATE=False,
            FORCE_SCHEME_FROM_HOST=True,
            REQUESTS_ARGS={"timeout": (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)},
        )

    return build


class Qbit:
    """One logged-in connection to qBittorrent.

    Methods run on the engine thread. A call never sleeps: while qBittorrent is down
    the client raises QbitUnavailable straight away until its backoff has passed.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        *,
        read_only: bool = False,
        client_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.host = host
        self.read_only = read_only
        self._factory = client_factory or _default_factory(host, username, password)
        self._clock = clock
        self._client: Optional[Any] = None
        self._failures = 0
        self._down_until = 0.0
        self._down_error: Optional[QbitError] = None
        self._rid = 0
        self._raw: Dict[str, Dict[str, Any]] = {}
        self._view: Dict[str, Torrent] = {}
        self._default_path: Optional[Tuple[float, str]] = None
        self.synced = False

    # -- connection ---------------------------------------------------------

    def _backoff(self, seconds: float, error: QbitError) -> None:
        self._down_until = self._clock() + seconds
        self._down_error = error

    def _connect(self):
        if self._client is not None:
            return self._client
        remaining = self._down_until - self._clock()
        if remaining > 0 and self._down_error is not None:
            raise type(self._down_error)(f"{self._down_error} (not retrying for another {int(remaining) + 1}s)")
        try:
            client = self._factory()
            client.auth_log_in()
        except Exception as e:
            traffic.record("qBittorrent login", traffic.host_of(self.host), traffic.ERROR)
            raise self._failed(e) from e
        traffic.record("qBittorrent login", traffic.host_of(self.host))
        self._client = client
        return client

    def _failed(self, e: BaseException) -> QbitError:
        """Turn a transport exception into one of ours and start the matching backoff."""
        self._client = None
        if isinstance(e, QbitError):
            return e
        if _is_auth_failure(e):
            logger.debug(f"qBittorrent login failed: {e}")
            if isinstance(e, qbittorrentapi.Forbidden403Error):
                text = "qBittorrent refused this address, usually after too many failed logins. Wait a while or restart qBittorrent, then try again."
            else:
                text = "qBittorrent rejected the username or password."
            error = QbitAuthError(text)
            self._backoff(AUTH_BACKOFF_SECONDS, error)
            return error
        if isinstance(e, qbittorrentapi.Conflict409Error):
            return QbitConflict(str(e))
        if isinstance(e, qbittorrentapi.HTTPError):
            # qBittorrent answered, so the connection is fine, unless a proxy in front of it did not.
            if getattr(getattr(e, "response", None), "status_code", None) in (502, 503, 504):
                return self._unavailable(e)
            return QbitError(str(e))
        if isinstance(e, (OSError, requests.exceptions.RequestException)):
            return self._unavailable(e)
        return QbitError(str(e))

    def _unavailable(self, e: BaseException) -> QbitUnavailable:
        steps = UNAVAILABLE_BACKOFF_SECONDS
        logger.debug(f"qBittorrent at {self.host} is unreachable: {e}")
        error = QbitUnavailable(_unreachable_text(self.host, e))
        self._backoff(steps[min(self._failures, len(steps) - 1)], error)
        self._failures += 1
        return error

    def _call(self, fn: Callable[[Any], Any], *, write: bool = False):
        if write and self.read_only:
            raise QbitReadOnly("This instance is read-only; it never changes qBittorrent.")
        client = self._connect()
        kind = f"qBittorrent {'change' if write else 'read'} ({traffic.caller_outside_clients()})"
        try:
            result = fn(client)
            traffic.record(kind, traffic.host_of(self.host))
            self._failures = 0
            return result
        except QbitError:
            raise
        except qbittorrentapi.Conflict409Error as e:
            traffic.record(kind, traffic.host_of(self.host), traffic.ERROR)
            raise QbitConflict(str(e)) from e
        except Exception as e:
            traffic.record(kind, traffic.host_of(self.host), traffic.ERROR)
            error = self._failed(e)
            if isinstance(error, QbitError) and not isinstance(error, (QbitUnavailable, QbitAuthError)):
                # A refused request says nothing about the connection; keep it.
                self._client = client
            raise error from e

    def close(self) -> None:
        self._client = None

    def versions(self) -> Dict[str, str]:
        """Verify the credentials and return the application and WebAPI versions."""
        return self._call(lambda c: {"app_version": str(c.app.version), "api_version": str(c.app.web_api_version)})

    # -- torrents -----------------------------------------------------------

    def sync(self, *, full: bool = False) -> SyncResult:
        """Bring the local torrent view up to date with one incremental request."""
        if full:
            self._rid = 0
        data = self._call(lambda c: c.sync_maindata(rid=self._rid))
        self._rid = int(data.get("rid", self._rid))
        incoming = data.get("torrents") or {}
        is_full = bool(data.get("full_update"))
        removed: Set[str] = {h.lower() for h in (data.get("torrents_removed") or [])}
        changed: Set[str] = set()
        if is_full:
            removed |= set(self._raw) - {h.lower() for h in incoming}
            self._raw = {}
        for info_hash, fields in incoming.items():
            key = info_hash.lower()
            entry = self._raw.setdefault(key, {})
            entry.update({name: fields[name] for name in _NEEDED_FIELDS if name in fields})
            changed.add(key)
        for key in removed:
            self._raw.pop(key, None)
            self._view.pop(key, None)
        for key in changed:
            self._view[key] = _torrent(key, self._raw[key])
        self.synced = True
        return SyncResult(full=is_full, changed=frozenset(changed), removed=frozenset(removed))

    @property
    def torrents(self) -> Dict[str, Torrent]:
        """Every torrent as of the last sync, by lowercase info hash."""
        return self._view

    def find_by_tag(self, tag: str) -> Optional[Torrent]:
        for torrent in self._view.values():
            if tag in torrent.tags:
                return torrent
        return None

    def files(self, info_hash: str) -> List[str]:
        """The torrent's files, as paths within its save folder."""
        return self._call(lambda c: [str(f.name) for f in c.torrents_files(torrent_hash=info_hash)])

    def add_torrent(
        self,
        url: str,
        *,
        save_path: str = "",
        category: str = "",
        tags: Iterable[str] = (),
        paused: bool = False,
        ratio_limit: Optional[float] = None,
        content_layout: Optional[str] = None,
    ) -> None:
        """Add a torrent by URL to an explicit path.

        A ``ratio_limit`` of 0 stops the torrent as soon as it finishes; None leaves qBittorrent's own limit alone.
        ``content_layout`` ("Original", "Subfolder" or "NoSubfolder") overrides qBittorrent's
        own setting for this torrent.
        """
        kwargs: Dict[str, Any] = {
            "urls": url,
            "save_path": save_path or None,
            "category": category or None,
            "is_paused": paused,
            "ratio_limit": ratio_limit,
            "use_auto_torrent_management": False,
            "share_limit_action": "Stop",
        }
        if content_layout:
            kwargs["content_layout"] = content_layout
        tag_text = ",".join(str(tag) for tag in tags)
        if tag_text:
            kwargs["tags"] = tag_text
        try:
            result = self._call(lambda c: c.torrents_add(**kwargs), write=True)
        except QbitConflict as e:
            raise QbitDuplicate(f"qBittorrent already has '{url}'") from e
        if _add_failed(result):
            raise QbitRejected(f"qBittorrent rejected torrent '{url}': {result}")

    def _hash_call(self, name: str, hashes: List[str], **kwargs: Any) -> None:
        if not hashes:
            return
        self._call(lambda c: getattr(c, name)(torrent_hashes=hashes, **kwargs), write=True)

    def pause(self, hashes: List[str]) -> None:
        self._hash_call("torrents_stop", hashes)

    def resume(self, hashes: List[str]) -> None:
        self._hash_call("torrents_start", hashes)

    def recheck(self, hashes: List[str]) -> None:
        self._hash_call("torrents_recheck", hashes)

    def delete(self, hashes: List[str], *, delete_files: bool) -> None:
        self._hash_call("torrents_delete", hashes, delete_files=delete_files)

    def set_location(self, hashes: List[str], location: str) -> None:
        if location:
            self._hash_call("torrents_set_location", hashes, location=location)

    def add_tags(self, hashes: List[str], tags: List[str]) -> None:
        if tags:
            self._hash_call("torrents_add_tags", hashes, tags=tags)

    def remove_tags(self, hashes: List[str], tags: List[str]) -> None:
        if tags:
            self._hash_call("torrents_remove_tags", hashes, tags=tags)

    def default_save_path(self) -> str:
        """qBittorrent's default folder. It is asked once in a while, not for every add of a pass."""
        if self._default_path is None or self._clock() - self._default_path[0] > DEFAULT_PATH_TTL_SECONDS:
            self._default_path = (self._clock(), self._call(lambda c: str(c.app_default_save_path() or "")))
        return self._default_path[1]

    def tags(self) -> List[str]:
        return self._call(lambda c: [str(tag) for tag in c.torrents_tags()])

    def delete_tags(self, tags: List[str]) -> None:
        if tags:
            self._call(lambda c: c.torrents_delete_tags(tags=tags), write=True)

    def ensure_category(self, category: str) -> None:
        if not category.strip():
            return

        def create(c):
            if category not in c.torrent_categories.categories:
                c.torrent_categories.create_category(name=category)

        self._call(create, write=True)

    # -- RSS ----------------------------------------------------------------

    def rss_feeds(self) -> List[RssStoreFeed]:
        """Every feed in qBittorrent's RSS tree."""
        tree = self._call(lambda c: c.rss_items(include_feed_data=False))
        feeds: List[RssStoreFeed] = []

        def walk(node: Dict[str, Any], prefix: str) -> None:
            for name, value in node.items():
                if not isinstance(value, dict):
                    continue
                path = f"{prefix}\\{name}" if prefix else name
                if "url" in value:
                    feeds.append(RssStoreFeed(path=path, url=str(value["url"])))
                else:
                    walk(value, path)

        walk(tree, "")
        return feeds



def _add_failed(result: Any) -> bool:
    """Whether qBittorrent's reply to an add says it refused the torrent.

    Newer versions answer with a failure count, older ones with the text 'Fails.'.
    """
    if result is None:
        return False
    count = getattr(result, "failure_count", None)
    if count is None and isinstance(result, dict):
        count = result.get("failure_count")
    if count is not None:
        return int(count) > 0
    text = str(result).strip().lower()
    if text in ("fails.", "fails"):
        return True
    match = re.search(r"failure_count['\"]?\s*:\s*(\d+)", text)
    return bool(match and int(match.group(1)) > 0)
