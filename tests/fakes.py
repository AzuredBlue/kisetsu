"""In-memory stand-ins for qBittorrent, AniList and the RSS feeds, for scenario tests."""

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from kisetsu.clients.qbittorrent import (
    QbitDuplicate,
    QbitError,
    QbitReadOnly,
    RssStoreFeed,
    SyncResult,
    Torrent,
)
from kisetsu.clients.rss import FeedArticle, FeedFetchError, magnet_parts, normalize_info_hash

START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    """A settable clock; calling it returns the current time."""

    def __init__(self, now: datetime = START):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


# The titles of the articles built by ``article()``, by torrent URL: what qBittorrent would
# read out of the torrent's metadata once it had added it.
ARTICLE_TITLES: Dict[str, str] = {}
_TORRENT_FILE = re.compile(r"/([0-9a-f]{40})\.torrent$")


def hash_for(url: str) -> str:
    """The info hash qBittorrent would give a torrent added from ``url``."""
    if url.startswith("magnet:"):
        hashed = normalize_info_hash(magnet_parts(url)[0])
        if hashed:
            return hashed
    match = _TORRENT_FILE.search(url)
    if match:
        return match.group(1)
    return hashlib.sha1(url.encode()).hexdigest()


def name_for(url: str) -> str:
    if url in ARTICLE_TITLES:
        return ARTICLE_TITLES[url]
    if url.startswith("magnet:"):
        name = magnet_parts(url)[1]
        if name:
            return name
    return url.rsplit("/", 1)[-1] or url


class FakeQbit:
    """qBittorrent with the public surface of ``clients.qbittorrent.Qbit``.

    Torrents live in ``truth``. The client only sees them after ``sync()``, as with the real
    one. Tests move torrents along with ``finish``, ``fail``, ``remove`` and friends.
    """

    def __init__(self, clock: Optional[FakeClock] = None, *, read_only: bool = False):
        self.clock = clock or FakeClock()
        self.read_only = read_only
        self.truth: Dict[str, Torrent] = {}
        self._view: Dict[str, Torrent] = {}
        self.synced = False
        self.calls: List[Tuple[str, Any]] = []
        self.unavailable: Optional[QbitError] = None
        self.reject_adds: Optional[Exception] = None
        self.hide_added = False
        self.hold_checks = False
        self.names: Dict[str, str] = {}
        self.sizes: Dict[str, int] = {}
        self.deleted_with_files: List[str] = []
        self.categories = set()
        self.tag_names: set = set()
        self.rss: List[RssStoreFeed] = []
        self.rules: Dict[str, Any] = {}
        self._hidden: Dict[str, Torrent] = {}

    # -- plumbing -------------------------------------------------------------

    def _enter(self, name: str, detail: Any = None, *, write: bool = False) -> None:
        self.calls.append((name, detail))
        if write and self.read_only:
            raise QbitReadOnly("This instance is read-only; it never changes qBittorrent.")
        if self.unavailable is not None:
            raise self.unavailable

    def calls_named(self, name: str) -> List[Any]:
        return [detail for call, detail in self.calls if call == name]

    def versions(self) -> Dict[str, str]:
        self._enter("versions")
        return {"app_version": "v5.3.0", "api_version": "2.16.2"}

    def close(self) -> None:
        pass

    # -- torrents -------------------------------------------------------------

    def sync(self, *, full: bool = False) -> SyncResult:
        self._enter("sync")
        if full:
            self._view = {}
        changed = frozenset(h for h, t in self.truth.items() if self._view.get(h) != t)
        removed = frozenset(set(self._view) - set(self.truth))
        was_full = not self.synced or full
        self._view = dict(self.truth)
        self.synced = True
        return SyncResult(full=was_full, changed=changed, removed=removed)

    @property
    def torrents(self) -> Dict[str, Torrent]:
        return self._view

    def find_by_tag(self, tag: str) -> Optional[Torrent]:
        return next((t for t in self._view.values() if tag in t.tags), None)

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
        self._enter("add_torrent", {
            "url": url, "save_path": save_path, "category": category, "tags": list(tags),
            "paused": paused, "ratio_limit": ratio_limit,
        }, write=True)
        if self.reject_adds is not None:
            raise self.reject_adds
        info_hash = hash_for(url)
        if info_hash in self.truth or info_hash in self._hidden:
            raise QbitDuplicate(f"qBittorrent already has '{url}'")
        name = self.names.get(url) or name_for(url)
        size = self.sizes.get(url, 1_000_000)
        torrent = Torrent(
            hash=info_hash,
            name=name,
            state="stoppedDL" if paused else "downloading",
            progress=0.0,
            amount_left=size,
            size=size,
            save_path=save_path,
            content_path=f"{save_path}/{name}" if save_path else name,
            category=category,
            tags=frozenset(tags),
            added_on=self.clock(),
            completion_on=None,
        )
        (self._hidden if self.hide_added else self.truth)[info_hash] = torrent

    def reveal(self) -> None:
        """Make torrents added while ``hide_added`` was set visible."""
        self.truth.update(self._hidden)
        self._hidden.clear()

    def _update(self, info_hash: str, **fields: Any) -> None:
        self.truth[info_hash] = replace(self.truth[info_hash], **fields)

    def pause(self, hashes: List[str]) -> None:
        self._enter("pause", list(hashes), write=True)
        for h in hashes:
            if h in self.truth:
                self._update(h, state="stoppedUP" if self.truth[h].finished else "stoppedDL")

    def resume(self, hashes: List[str]) -> None:
        self._enter("resume", list(hashes), write=True)
        for h in hashes:
            if h in self.truth:
                self._update(h, state="stalledUP" if self.truth[h].finished else "downloading")

    def recheck(self, hashes: List[str]) -> None:
        self._enter("recheck", list(hashes), write=True)
        for h in hashes:
            if h in self.truth and self.hold_checks:
                self._update(h, state="checkingUP" if self.truth[h].finished else "checkingDL")

    def finish_checks(self) -> None:
        for h, t in list(self.truth.items()):
            if t.checking:
                self._update(h, state="stoppedUP" if t.finished else "stoppedDL")

    def delete(self, hashes: List[str], *, delete_files: bool) -> None:
        self._enter("delete", {"hashes": list(hashes), "delete_files": delete_files}, write=True)
        for h in hashes:
            self.truth.pop(h, None)
            if delete_files:
                self.deleted_with_files.append(h)

    def set_location(self, hashes: List[str], location: str) -> None:
        self._enter("set_location", {"hashes": list(hashes), "location": location}, write=True)
        for h in hashes:
            if h in self.truth:
                t = self.truth[h]
                self._update(h, save_path=location, content_path=f"{location}/{t.name}")

    def add_tags(self, hashes: List[str], tags: List[str]) -> None:
        self._enter("add_tags", {"hashes": list(hashes), "tags": list(tags)}, write=True)
        for h in hashes:
            if h in self.truth:
                self._update(h, tags=self.truth[h].tags | frozenset(tags))
        self.tag_names |= set(tags)

    def remove_tags(self, hashes: List[str], tags: List[str]) -> None:
        self._enter("remove_tags", {"hashes": list(hashes), "tags": list(tags)}, write=True)
        for h in hashes:
            if h in self.truth:
                self._update(h, tags=self.truth[h].tags - frozenset(tags))

    def tags(self) -> List[str]:
        self._enter("tags")
        return sorted(self.tag_names | {tag for t in self.truth.values() for tag in t.tags})

    def delete_tags(self, tags: List[str]) -> None:
        self._enter("delete_tags", list(tags), write=True)
        self.tag_names -= set(tags)
        for h, t in list(self.truth.items()):
            if t.tags & frozenset(tags):
                self._update(h, tags=t.tags - frozenset(tags))

    def ensure_category(self, category: str) -> None:
        self._enter("ensure_category", category, write=True)
        if category.strip():
            self.categories.add(category)

    # -- test controls --------------------------------------------------------

    def torrent_for(self, url: str) -> Torrent:
        return self.truth[hash_for(url)]

    def finish(self, target: str) -> None:
        """Complete a torrent given its URL or hash."""
        info_hash = target if target in self.truth else hash_for(target)
        t = self.truth[info_hash]
        state = "stoppedUP" if t.state.startswith("stopped") or t.state.startswith("paused") else "stalledUP"
        self._update(info_hash, progress=1.0, amount_left=0, state=state, completion_on=self.clock())

    def fail(self, target: str, state: str = "error") -> None:
        info_hash = target if target in self.truth else hash_for(target)
        self._update(info_hash, state=state)

    def remove(self, target: str) -> None:
        """The user deletes a torrent in qBittorrent."""
        self.truth.pop(target if target in self.truth else hash_for(target), None)

    def seed(self, name: str, *, save_path: str, category: str = "", tags: Iterable[str] = (), finished: bool = True, url: Optional[str] = None) -> Torrent:
        """A torrent that was already in qBittorrent before Kisetsu ran."""
        key = url or f"seed:{name}"
        torrent = Torrent(
            hash=hash_for(key), name=name, state="stalledUP" if finished else "downloading",
            progress=1.0 if finished else 0.0, amount_left=0 if finished else 1000, size=1000,
            save_path=save_path, content_path=f"{save_path}/{name}", category=category,
            tags=frozenset(tags), added_on=self.clock(), completion_on=self.clock() if finished else None,
        )
        self.truth[torrent.hash] = torrent
        return torrent

    # -- RSS ------------------------------------------------------------------

    def rss_feeds(self) -> List[RssStoreFeed]:
        self._enter("rss_feeds")
        return [RssStoreFeed(f.path, f.url) for f in self.rss]

    def rss_rules(self) -> Dict[str, Any]:
        self._enter("rss_rules")
        return dict(self.rules)

    def rss_remove_rule(self, name: str) -> None:
        self._enter("rss_remove_rule", name, write=True)
        self.rules.pop(name, None)


def anilist_show(
    anilist_id: int,
    title: str = "Show",
    *,
    romaji: Optional[str] = None,
    status: str = "RELEASING",
    list_status: str = "CURRENT",
    season: str = "FALL",
    season_year: int = 2026,
    total_episodes: Optional[int] = None,
    next_airing_episode: Optional[int] = None,
    next_airing_at: Optional[datetime] = None,
    aliases: Iterable[str] = (),
) -> Dict[str, Any]:
    """One entry in the shape ``AniListClient.fetch_user_seasonal_anime`` returns."""
    return {
        "anilist_id": anilist_id,
        "display_name": title,
        "title_romaji": romaji if romaji is not None else title,
        "title_english": title,
        "aliases": list(dict.fromkeys([title, *(romaji and [romaji] or []), *aliases])),
        "list_status": list_status,
        "status": status,
        "total_episodes": total_episodes,
        "next_airing_episode": next_airing_episode,
        "next_airing_at": next_airing_at,
        "season": season,
        "season_year": season_year,
        "cover_image": "",
        "banner_image": "",
    }


class FakeAniList:
    """AniList with the public surface of ``clients.anilist.AniListClient``."""

    def __init__(self):
        self.shows: List[Dict[str, Any]] = []
        self.schedules: Dict[int, List[Dict[str, Any]]] = {}
        self.users = {"user"}
        self.error: Optional[Exception] = None
        self.calls: List[Tuple[str, Any]] = []
        self.last_sync_at: Optional[datetime] = None
        self.due = True
        self.user_list: List[Dict[str, Any]] = []
        self.user_list_at: Optional[datetime] = None

    def _enter(self, name: str, detail: Any = None) -> None:
        self.calls.append((name, detail))
        if self.error is not None:
            raise self.error

    def set_schedule(self, anilist_id: int, first_air: datetime, count: int, *, every: timedelta = timedelta(days=7)) -> None:
        self.schedules[anilist_id] = [
            {"episode": n, "airing_at": first_air + every * (n - 1)} for n in range(1, count + 1)
        ]

    async def user_exists(self, username: str) -> bool:
        self._enter("user_exists", username)
        return username in self.users

    async def fetch_user_list(self, username: str) -> List[Dict[str, Any]]:
        self._enter("fetch_user_list", username)
        self.user_list = [dict(show) for show in self.shows]
        self.user_list_at = datetime.now(timezone.utc)
        return list(self.user_list)

    async def fetch_user_seasonal_anime(self, username: str, monitored_anilist_ids: Optional[Iterable[int]] = None) -> List[Dict[str, Any]]:
        self._enter("fetch_user_seasonal_anime", username)
        self.user_list = [dict(show) for show in self.shows]
        self.user_list_at = datetime.now(timezone.utc)
        return [dict(show) for show in self.shows]

    async def fetch_media_airing_schedules(self, media_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
        self._enter("fetch_media_airing_schedules", list(media_ids))
        return {i: list(self.schedules[i]) for i in dict.fromkeys(media_ids) if i in self.schedules}

    async def fetch_upcoming_airing_schedules(self, media_ids: List[int], since: datetime) -> Dict[int, List[Dict[str, Any]]]:
        self._enter("fetch_upcoming_airing_schedules", list(media_ids))
        return {
            i: [e for e in self.schedules.get(i, []) if e["airing_at"] >= since]
            for i in dict.fromkeys(media_ids)
        }

    def seconds_since_last_sync(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.last_sync_at is None:
            return None
        return ((now or datetime.now(timezone.utc)) - self.last_sync_at).total_seconds()

    def is_sync_due(self, min_age_seconds: float, now: Optional[datetime] = None) -> bool:
        return self.due


def article(
    title: str,
    published_at: Optional[datetime] = None,
    *,
    guid: Optional[str] = None,
    info_hash: Optional[str] = None,
    torrent_url: Optional[str] = None,
) -> FeedArticle:
    """A feed item. Like a real one it carries its info hash, and its torrent URL names it."""
    guid = guid or f"guid:{title}"
    info_hash = info_hash or hashlib.sha1(guid.encode()).hexdigest()
    torrent_url = torrent_url or f"https://example.test/{info_hash}.torrent"
    ARTICLE_TITLES[torrent_url] = title
    return FeedArticle(guid, title, torrent_url, published_at, info_hash)


@dataclass
class FakeFeeds:
    """The RSS feeds Kisetsu fetches itself, and the searchable sites behind them."""

    items: Dict[str, List[FeedArticle]] = field(default_factory=dict)
    errors: Dict[str, Exception] = field(default_factory=dict)
    searchable: Dict[str, List[FeedArticle]] = field(default_factory=dict)
    fetched: List[str] = field(default_factory=list)
    searched: List[Tuple[str, str]] = field(default_factory=list)

    def post(self, url: str, title: str, published_at: Optional[datetime] = None, **kwargs: Any) -> FeedArticle:
        """Add a new item at the top of a feed."""
        item = article(title, published_at, **kwargs)
        self.items.setdefault(url, []).insert(0, item)
        return item

    def fail(self, url: str, message: str = "feed is down") -> None:
        self.errors[url] = FeedFetchError(message)

    def heal(self, url: str) -> None:
        self.errors.pop(url, None)

    async def fetch(self, url: str) -> List[FeedArticle]:
        self.fetched.append(url)
        if url in self.errors:
            raise self.errors[url]
        return list(self.items.get(url, []))

    async def search(self, feed_url: str, term: str) -> Optional[List[FeedArticle]]:
        """Items on the feed's site whose title contains the term, or None when the site cannot be searched."""
        self.searched.append((feed_url, term))
        if feed_url not in self.searchable:
            return None
        if feed_url in self.errors:
            raise self.errors[feed_url]
        return [a for a in self.searchable[feed_url] if term.casefold() in a.title.casefold()]
