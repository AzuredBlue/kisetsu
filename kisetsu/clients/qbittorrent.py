"""The qBittorrent WebUI client: torrents, and the RSS tree Kisetsu mirrors its feeds into."""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set

import qbittorrentapi
import requests


logger = logging.getLogger("kisetsu.clients.qbittorrent")

CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0
# While qBittorrent is unreachable, calls fail at once for this long, doubling per failure.
UNAVAILABLE_BACKOFF_SECONDS = (30, 60, 120, 300)
# A rejected login is not retried for a while: qBittorrent bans an address after a few failed logins.
AUTH_BACKOFF_SECONDS = 300


class QbitError(Exception):
    """Base class of every error this client raises."""


class QbitUnavailable(QbitError):
    """qBittorrent is unreachable, or this client is backing off after it was."""


class QbitAuthError(QbitError):
    """qBittorrent rejected the configured credentials."""


class QbitConflict(QbitError):
    """qBittorrent answered 409: an RSS feed name or URL is taken, or the path is missing."""


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
    seeding_time: int = 0

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
    "category", "tags", "added_on", "completion_on", "seeding_time",
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
        seeding_time=int(raw.get("seeding_time") or 0),
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
            raise self._failed(e) from e
        self._client = client
        return client

    def _failed(self, e: BaseException) -> QbitError:
        """Turn a transport exception into one of ours and start the matching backoff."""
        self._client = None
        if isinstance(e, QbitError):
            return e
        if _is_auth_failure(e):
            error = QbitAuthError(f"qBittorrent rejected the login: {e}")
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
        error = QbitUnavailable(f"Cannot reach qBittorrent at {self.host}: {e}")
        self._backoff(steps[min(self._failures, len(steps) - 1)], error)
        self._failures += 1
        return error

    def _call(self, fn: Callable[[Any], Any], *, write: bool = False):
        if write and self.read_only:
            raise QbitReadOnly("This instance is read-only; it never changes qBittorrent.")
        client = self._connect()
        try:
            result = fn(client)
            self._failures = 0
            return result
        except QbitError:
            raise
        except qbittorrentapi.Conflict409Error as e:
            raise QbitConflict(str(e)) from e
        except Exception as e:
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

    def add_torrent(
        self,
        url: str,
        *,
        save_path: str = "",
        category: str = "",
        tags: Iterable[str] = (),
        paused: bool = False,
        ratio_limit: Optional[float] = None,
    ) -> None:
        """Add a torrent by URL to an explicit path.

        A ``ratio_limit`` of 0 means seed forever; None leaves qBittorrent's own limit alone.
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

    def rss_rules(self) -> Dict[str, Any]:
        return self._call(lambda c: c.rss_rules())

    def rss_remove_rule(self, name: str) -> None:
        self._call(lambda c: c.rss_remove_rule(rule_name=name), write=True)


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
