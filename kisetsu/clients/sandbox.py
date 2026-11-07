"""The sandbox's qBittorrent: real torrents are read, every change is logged and only pretended.

Torrents the sandbox adds are fakes that download for ``DOWNLOAD_SECONDS`` and then finish, so
the whole flow shows in the UI. Deleting, moving or tagging a real torrent only changes how the
sandbox sees it. The real client underneath is read-only, so a write missed here fails instead of
reaching qBittorrent.
"""

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from kisetsu.clients.qbittorrent import Qbit, QbitDuplicate, SyncResult, Torrent
from kisetsu.clients.rss import magnet_parts, normalize_info_hash

logger = logging.getLogger("kisetsu.sandbox")

# How long a pretend download takes to finish.
DOWNLOAD_SECONDS = 60.0
FAKE_SIZE = 500 * 1024 * 1024


@dataclass
class Fake:
    hash: str
    name: str
    save_path: str
    content_path: str
    category: str
    tags: Set[str]
    added_at: float
    stop_when_done: bool
    paused: bool
    # Seconds of downloading done before the current run, and when the current run began (None while paused).
    banked: float = 0.0
    running_since: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass
class SandboxState:
    """What the sandbox pretends happened in qBittorrent.

    One object for the whole run: the engine builds a new client whenever the qBittorrent
    settings change, and the fakes must survive that.
    """

    clock: Callable[[], float] = time.time
    download_seconds: float = DOWNLOAD_SECONDS
    # The release name and info hash a torrent URL is known by (from its download row), so a
    # pretend torrent carries the hash the real one would and a duplicate is refused as qBittorrent would.
    release_for: Callable[[str], Tuple[Optional[str], Optional[str]]] = lambda url: (None, None)
    fakes: Dict[str, Fake] = field(default_factory=dict)
    # Real torrents the sandbox deleted, and its tag and folder changes to the others.
    hidden: Set[str] = field(default_factory=set)
    tags_added: Dict[str, Set[str]] = field(default_factory=dict)
    tags_removed: Dict[str, Set[str]] = field(default_factory=dict)
    tags_deleted: Set[str] = field(default_factory=set)
    locations: Dict[str, str] = field(default_factory=dict)


def _when(seconds: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds is not None else None


def _hash_of(url: str, known: Optional[str]) -> str:
    if known:
        return known.lower()
    if url.startswith("magnet:"):
        hashed = normalize_info_hash(magnet_parts(url)[0])
        if hashed:
            return hashed
    return hashlib.sha1(url.encode()).hexdigest()


def _fallback_name(url: str) -> str:
    if url.startswith("magnet:"):
        name = magnet_parts(url)[1]
        if name:
            return name
    return url.rsplit("/", 1)[-1] or url


class SandboxQbit(Qbit):
    def __init__(self, host: str, username: str = "", password: str = "", *, state: SandboxState, **kwargs) -> None:
        kwargs["read_only"] = True
        super().__init__(host, username, password, **kwargs)
        self.state = state
        self._merged: Dict[str, Torrent] = {}

    # -- reading ------------------------------------------------------------

    def _fake_torrent(self, fake: Fake, now: float) -> Torrent:
        done = fake.banked + (now - fake.running_since if fake.running_since is not None else 0.0)
        progress = min(1.0, done / self.state.download_seconds) if self.state.download_seconds > 0 else 1.0
        if progress >= 1.0 and fake.completed_at is None:
            fake.completed_at = now
            logger.info(f"[sandbox] '{fake.name}' finished downloading (pretend).")
        finished = progress >= 1.0
        if finished:
            state = "stoppedUP" if fake.paused or fake.stop_when_done else "uploading"
        else:
            state = "stoppedDL" if fake.paused else "downloading"
        return Torrent(
            hash=fake.hash,
            name=fake.name,
            state=state,
            progress=progress,
            amount_left=int(FAKE_SIZE * (1.0 - progress)),
            size=FAKE_SIZE,
            save_path=fake.save_path,
            content_path=fake.content_path,
            category=fake.category,
            tags=frozenset(fake.tags),
            added_on=_when(fake.added_at),
            completion_on=_when(fake.completed_at),
            last_activity=_when(now if fake.running_since is not None else fake.completed_at or fake.added_at),
        )

    def sync(self, *, full: bool = False) -> SyncResult:
        real = super().sync(full=full)
        s = self.state
        now = s.clock()
        merged: Dict[str, Torrent] = {}
        for h, t in self._view.items():
            if h in s.hidden:
                continue
            tags = (t.tags | s.tags_added.get(h, set())) - s.tags_removed.get(h, set())
            if h in s.locations:
                t = replace(t, save_path=s.locations[h])
            merged[h] = t if tags == t.tags else replace(t, tags=frozenset(tags))
        for h, fake in s.fakes.items():
            merged[h] = self._fake_torrent(fake, now)
        before, self._merged = self._merged, merged
        changed = frozenset(h for h, t in merged.items() if before.get(h) != t)
        return SyncResult(full=real.full, changed=changed, removed=frozenset(set(before) - set(merged)))

    @property
    def torrents(self) -> Dict[str, Torrent]:
        return self._merged

    def find_by_tag(self, tag: str) -> Optional[Torrent]:
        return next((t for t in self._merged.values() if tag in t.tags), None)

    def tags(self) -> List[str]:
        names = set(super().tags()) - self.state.tags_deleted
        for fake in self.state.fakes.values():
            names |= fake.tags
        for added in self.state.tags_added.values():
            names |= added
        return sorted(names)

    def _names(self, hashes: Iterable[str]) -> str:
        return ", ".join(f"'{self._merged[h].name}'" if h in self._merged else h for h in hashes)

    # -- pretend writes -----------------------------------------------------

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
        s = self.state
        title, known_hash = s.release_for(url)
        info_hash = _hash_of(url, known_hash)
        name = title or _fallback_name(url)
        if info_hash in s.fakes or (info_hash in self._view and info_hash not in s.hidden):
            logger.info(f"[sandbox] Would add '{name}', but qBittorrent already has it, so it would refuse; the existing torrent is used.")
            raise QbitDuplicate(f"qBittorrent already has '{url}'")
        now = s.clock()
        s.hidden.discard(info_hash)
        s.fakes[info_hash] = Fake(
            hash=info_hash,
            name=name,
            save_path=save_path,
            content_path=save_path if content_layout == "NoSubfolder" else os.path.join(save_path, name),
            category=category,
            tags=set(tags),
            added_at=now,
            stop_when_done=ratio_limit == 0,
            paused=paused,
            running_since=None if paused else now,
        )
        where = save_path or "qBittorrent's default folder"
        logger.info(f"[sandbox] Would add '{name}' to {where}{' paused' if paused else ''}.")

    def pause(self, hashes: List[str]) -> None:
        if not hashes:
            return
        now = self.state.clock()
        for h in hashes:
            fake = self.state.fakes.get(h)
            if fake is not None and not fake.paused:
                if fake.running_since is not None:
                    fake.banked += now - fake.running_since
                fake.running_since = None
                fake.paused = True
        logger.info(f"[sandbox] Would pause {self._names(hashes)}.")

    def resume(self, hashes: List[str]) -> None:
        if not hashes:
            return
        now = self.state.clock()
        for h in hashes:
            fake = self.state.fakes.get(h)
            if fake is not None and fake.paused:
                fake.paused = False
                fake.running_since = now
        logger.info(f"[sandbox] Would resume {self._names(hashes)}.")

    def recheck(self, hashes: List[str]) -> None:
        if hashes:
            logger.info(f"[sandbox] Would recheck {self._names(hashes)}.")

    def delete(self, hashes: List[str], *, delete_files: bool) -> None:
        if not hashes:
            return
        logger.info(f"[sandbox] Would delete {self._names(hashes)} {'with its files' if delete_files else 'and keep its files'}.")
        for h in hashes:
            if self.state.fakes.pop(h, None) is None:
                self.state.hidden.add(h)

    def set_location(self, hashes: List[str], location: str) -> None:
        if not location or not hashes:
            return
        logger.info(f"[sandbox] Would move {self._names(hashes)} to {location}.")
        for h in hashes:
            fake = self.state.fakes.get(h)
            if fake is not None:
                fake.content_path = os.path.join(location, os.path.basename(fake.content_path))
                fake.save_path = location
            else:
                self.state.locations[h] = location

    def add_tags(self, hashes: List[str], tags: List[str]) -> None:
        if not hashes or not tags:
            return
        logger.debug(f"[sandbox] Would tag {self._names(hashes)} with {', '.join(tags)}.")
        self.state.tags_deleted -= set(tags)
        for h in hashes:
            fake = self.state.fakes.get(h)
            if fake is not None:
                fake.tags |= set(tags)
            else:
                self.state.tags_added.setdefault(h, set()).update(tags)
                self.state.tags_removed.get(h, set()).difference_update(tags)

    def remove_tags(self, hashes: List[str], tags: List[str]) -> None:
        if not hashes or not tags:
            return
        logger.debug(f"[sandbox] Would remove tags {', '.join(tags)} from {self._names(hashes)}.")
        for h in hashes:
            fake = self.state.fakes.get(h)
            if fake is not None:
                fake.tags -= set(tags)
            else:
                self.state.tags_removed.setdefault(h, set()).update(tags)
                self.state.tags_added.get(h, set()).difference_update(tags)

    def delete_tags(self, tags: List[str]) -> None:
        if not tags:
            return
        logger.debug(f"[sandbox] Would delete the tags {', '.join(tags)}.")
        self.state.tags_deleted |= set(tags)
        carrying = [h for h, t in self._merged.items() if set(tags) & t.tags]
        self.remove_tags(carrying, list(tags))

    def ensure_category(self, category: str) -> None:
        if category.strip():
            logger.debug(f"[sandbox] Would make sure the category '{category}' exists.")
