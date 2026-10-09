"""Kisetsu's own copy of the feed releases that matter, kept in ``matched_feed_items``.

qBittorrent's RSS cache is what the feeds delivered last: it must be refreshed
and settled before it can be read, it is mostly releases for shows nobody
follows, and it is gone when a feed is removed. This keeps the releases that
matched a followed show, slimmed down, so checks, discovery and the UI read the
database instead of waiting on qBittorrent.

Every feed is matched against every followed show, so a show has rows from all
the feeds that carried it and switching its feed finds them at once.
"""

import json
import logging
import threading
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from kisetsu.clients.qbit import QBitClient
from kisetsu.core.discovery import RssSnapshot, parse_article_date
from kisetsu.core.matching import match_release_to_show, prepare_aliases
from kisetsu.core.rules import show_match_patterns
from kisetsu.db.models import Episode, Feed, MatchedFeedItem, Monitored, MonitoredStatus, SeenFeedItem, utc_now

logger = logging.getLogger("kisetsu.core.feedcache")

# The only article fields anything reads; the rest (description, seeders, ...) is dropped.
CACHE_FIELDS = ("id", "title", "link", "torrentURL", "date", "infoHash", "size")
# A finished show's releases stay this long (its Restore may still be wanted).
COMPLETED_KEEP_DAYS = 60
# Newest releases kept per show; a season of episodes and their variants fits well within it.
PER_SHOW_CAP = 300
# Rules mode's shield rows aside, an unshielded "seen" row has no use beyond this.
SEEN_KEEP_DAYS = 3
# Releases published more than this before a show's first episode are not for it.
PREMIERE_SLACK_DAYS = 3
_CHUNK = 500

# Feeds the last ingest could not read; their (stale) cache is not treated as current.
_failed_feed_names: List[str] = []
# One copy from qBittorrent at a time; a second caller just waits and reads the settled result.
_ingest_lock = threading.Lock()
# Articles already matched against the followed shows are not matched again.
_EVALUATED_CAP = 50_000


class _Evaluated:
    """The articles already matched for one database, valid for one set of followed shows."""

    def __init__(self):
        self.signature = None
        self.keys: set = set()
        self.lock = threading.Lock()


_evaluated_by_engine: "weakref.WeakKeyDictionary[Any, _Evaluated]" = weakref.WeakKeyDictionary()
_evaluated_guard = threading.Lock()


def _evaluated_for(session: Session) -> _Evaluated:
    engine = session.get_bind()
    with _evaluated_guard:
        state = _evaluated_by_engine.get(engine)
        if state is None:
            state = _evaluated_by_engine[engine] = _Evaluated()
        return state


def last_failed_feed_names() -> List[str]:
    return list(_failed_feed_names)


def item_id_of(article: Dict[str, Any]) -> str:
    return str(
        article.get("id")
        or article.get("torrentURL")
        or article.get("link")
        or article.get("title", "")
    )


def _slim(article: Dict[str, Any]) -> Dict[str, Any]:
    return {key: article[key] for key in CACHE_FIELDS if article.get(key) not in (None, "")}


def _published_at(article: Dict[str, Any]) -> Optional[datetime]:
    """Naive UTC publication time, or None when the feed gave none."""
    parsed = parse_article_date(article)
    if parsed.year <= 2000:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def premiere_cutoff(first_air: datetime) -> datetime:
    """Naive UTC time before which a release cannot be this show's."""
    return _naive(first_air) - timedelta(days=PREMIERE_SLACK_DAYS)


class _FollowedShows:
    """Which followed show, if any, a release title belongs to."""

    def __init__(self, shows: Iterable[Monitored]):
        self._entries = []
        parts = []
        for show in shows:
            aliases = show.effective_aliases
            pattern, _ = show_match_patterns(aliases, show.matched_title)
            self._entries.append((show.id, aliases, pattern, prepare_aliases(aliases)))
            parts.append((show.id, tuple(aliases), show.matched_title or ""))
        # Changes whenever a show is added or removed, or its aliases or learned name change.
        self.signature = hash(tuple(sorted(parts)))
        self._parsed: Dict[str, Dict[str, Any]] = {}

    def matching_ids(self, title: str) -> List[int]:
        return [
            show_id
            for show_id, aliases, pattern, prepared in self._entries
            if match_release_to_show(
                title,
                aliases,
                test_pattern=pattern,
                prepared_aliases=prepared,
                parsed_cache=self._parsed,
            )[0]
        ]


def _followed(session: Session) -> _FollowedShows:
    shows = session.exec(select(Monitored).where(Monitored.status != MonitoredStatus.COMPLETED)).all()
    return _FollowedShows(shows)


def _insert_rows(session: Session, rows: List[Dict[str, Any]]) -> int:
    """Insert rows, ignoring any that exist already (safe against a concurrent writer)."""
    added = 0
    for start in range(0, len(rows), _CHUNK):
        result = session.execute(
            sqlite_insert(MatchedFeedItem).values(rows[start:start + _CHUNK]).on_conflict_do_nothing(
                index_elements=["monitored_id", "feed_url", "item_id"]
            )
        )
        added += max(0, result.rowcount or 0)
    return added


def store_matches(
    session: Session,
    articles_by_url: Dict[str, List[Dict[str, Any]]],
    followed: Optional[_FollowedShows] = None,
    now: Optional[datetime] = None,
    only_show_ids: Optional[set] = None,
) -> int:
    """Keep the articles that match a followed show; returns how many rows were added.

    A release that matches two shows is kept for both. Rows that exist already
    are left alone, so their first-seen time is the first one. An article that
    was matched before is not matched again until the followed shows change.

    ``only_show_ids`` keeps rows for those shows alone, for a caller that vetted
    the articles for one show (a search for it) and cannot vouch for the others.
    Such articles are not marked as evaluated, so the regular copy still sees them.
    """
    followed = followed or _followed(session)
    now = _naive(now or utc_now())
    state = _evaluated_for(session)
    with state.lock:
        if state.signature != followed.signature or len(state.keys) > _EVALUATED_CAP:
            state.keys.clear()
            state.signature = followed.signature
        known = set(state.keys)
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for feed_url, articles in articles_by_url.items():
        for article in articles:
            title = article.get("title") or ""
            if not title:
                continue
            item_id = item_id_of(article)
            if (feed_url, item_id) in seen or (feed_url, item_id) in known:
                continue
            if only_show_ids is None:
                seen.add((feed_url, item_id))
            show_ids = followed.matching_ids(title)
            if only_show_ids is not None:
                show_ids = [show_id for show_id in show_ids if show_id in only_show_ids]
            if not show_ids:
                continue
            data = json.dumps(_slim(article), separators=(",", ":"))
            published = _published_at(article)
            for show_id in show_ids:
                rows.append({
                    "monitored_id": show_id,
                    "feed_url": feed_url,
                    "item_id": item_id,
                    "title": title,
                    "published_at": published,
                    "first_seen_at": now,
                    "data_json": data,
                })
    added = _insert_rows(session, rows) if rows else 0
    session.commit()
    with state.lock:
        if state.signature == followed.signature:
            state.keys.update(seen)
    return added


def first_seen_map(session: Session, keys: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], datetime]:
    """When each (feed_url, item_id) was first seen, for the ones that are stored."""
    by_feed: Dict[str, List[str]] = {}
    for feed_url, item_id in keys:
        by_feed.setdefault(feed_url, []).append(item_id)
    seen: Dict[Tuple[str, str], datetime] = {}
    for feed_url, item_ids in by_feed.items():
        for start in range(0, len(item_ids), _CHUNK):
            for item_id, first in session.exec(
                select(MatchedFeedItem.item_id, func.min(MatchedFeedItem.first_seen_at))
                .where(MatchedFeedItem.feed_url == feed_url, MatchedFeedItem.item_id.in_(item_ids[start:start + _CHUNK]))
                .group_by(MatchedFeedItem.item_id)
            ).all():
                seen[(feed_url, item_id)] = first
    return seen


def cached_articles(session: Session) -> Dict[str, List[Dict[str, Any]]]:
    """The cache in the shape ``flatten_rss_articles`` returns, newest first.

    Every known feed has an entry (empty when nothing is kept for it), except
    feeds the last ingest could not read, which are left out as before.
    """
    articles: Dict[str, List[Dict[str, Any]]] = {
        url: [] for url in session.exec(select(Feed.qbit_feed_url)).all()
    }
    seen: set = set()
    rows = session.exec(
        select(MatchedFeedItem.feed_url, MatchedFeedItem.item_id, MatchedFeedItem.data_json)
        .order_by(MatchedFeedItem.published_at.desc(), MatchedFeedItem.first_seen_at.desc())
    ).all()
    for feed_url, item_id, data_json in rows:
        if (feed_url, item_id) in seen:
            continue
        seen.add((feed_url, item_id))
        try:
            article = json.loads(data_json)
        except (TypeError, ValueError):
            continue
        articles.setdefault(feed_url, []).append(article)
    if _failed_feed_names:
        failed_urls = {
            url for url, name in session.exec(select(Feed.qbit_feed_url, Feed.qbit_feed_name)).all()
            if name in _failed_feed_names
        }
        for url in failed_urls:
            articles.pop(url, None)
    return articles


def prune(session: Session, now: Optional[datetime] = None) -> int:
    """Drop what no longer makes sense to keep; returns how many cache rows went.

    Rows go when their feed is gone, when their show has been COMPLETED for
    COMPLETED_KEEP_DAYS, or beyond the PER_SHOW_CAP newest of a show. (Rows of a
    deleted show go with it.) Unshielded "seen" rows from before the cache, and
    from rules mode, older than SEEN_KEEP_DAYS go too.
    """
    now = _naive(now or utc_now())
    removed = 0

    feed_urls = set(session.exec(select(Feed.qbit_feed_url)).all())
    if feed_urls:
        removed += session.execute(
            delete(MatchedFeedItem).where(MatchedFeedItem.feed_url.not_in(feed_urls))
        ).rowcount or 0

    # A release published well before its show's first episode aired belongs to an
    # earlier season (or is a re-release); nothing can use it. Undated rows stay.
    for monitored_id, first_air in session.exec(
        select(Episode.monitored_id, func.min(Episode.air_at))
        .where(Episode.air_at.is_not(None))
        .group_by(Episode.monitored_id)
    ).all():
        removed += session.execute(
            delete(MatchedFeedItem).where(
                MatchedFeedItem.monitored_id == monitored_id,
                MatchedFeedItem.published_at.is_not(None),
                MatchedFeedItem.published_at < premiere_cutoff(first_air),
            )
        ).rowcount or 0

    completed_ids = select(Monitored.id).where(Monitored.status == MonitoredStatus.COMPLETED)
    cutoff = now - timedelta(days=COMPLETED_KEEP_DAYS)
    removed += session.execute(
        delete(MatchedFeedItem).where(
            MatchedFeedItem.monitored_id.in_(completed_ids),
            func.coalesce(MatchedFeedItem.published_at, MatchedFeedItem.first_seen_at) < cutoff,
        )
    ).rowcount or 0

    for monitored_id, count in session.exec(
        select(MatchedFeedItem.monitored_id, func.count(MatchedFeedItem.id)).group_by(MatchedFeedItem.monitored_id)
    ).all():
        if count <= PER_SHOW_CAP:
            continue
        keep = select(MatchedFeedItem.id).where(MatchedFeedItem.monitored_id == monitored_id).order_by(
            MatchedFeedItem.published_at.desc(), MatchedFeedItem.first_seen_at.desc()
        ).limit(PER_SHOW_CAP)
        removed += session.execute(
            delete(MatchedFeedItem).where(
                MatchedFeedItem.monitored_id == monitored_id,
                MatchedFeedItem.id.not_in(keep),
            )
        ).rowcount or 0

    removed += session.execute(
        delete(SeenFeedItem).where(
            SeenFeedItem.shielded_at.is_(None),
            SeenFeedItem.created_at < now - timedelta(days=SEEN_KEEP_DAYS),
        )
    ).rowcount or 0
    session.commit()
    return removed


def _parse_stored_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _naive(value)
    try:
        return _naive(datetime.fromisoformat(str(value))) if value else None
    except ValueError:
        return None


def backfill_from_seen(session: Session) -> int:
    """Move releases kept in ``seen_feed_items`` by the first version of the cache.

    Runs while ``matched_feed_items`` is empty and only if the old ``data_json``
    column exists. The old data is cleared afterwards so nothing is kept twice.
    """
    if session.exec(select(MatchedFeedItem.id).limit(1)).first() is not None:
        return 0
    columns = {row[1] for row in session.exec(text("PRAGMA table_info(seen_feed_items)")).all()}
    if "data_json" not in columns:
        return 0
    old_rows = session.exec(text(
        "SELECT feed_url, item_id, title, data_json, published_at, created_at "
        "FROM seen_feed_items WHERE data_json IS NOT NULL"
    )).all()
    followed = _followed(session)
    rows: List[Dict[str, Any]] = []
    for feed_url, item_id, title, data_json, published_at, created_at in old_rows:
        for show_id in followed.matching_ids(title or ""):
            rows.append({
                "monitored_id": show_id,
                "feed_url": feed_url,
                "item_id": item_id,
                "title": title or "",
                "published_at": _parse_stored_time(published_at),
                "first_seen_at": _parse_stored_time(created_at) or _naive(utc_now()),
                "data_json": data_json,
            })
    added = _insert_rows(session, rows) if rows else 0
    session.execute(text("UPDATE seen_feed_items SET data_json = NULL WHERE data_json IS NOT NULL"))
    session.commit()
    return added


def ingest_rss(engine, qbit: QBitClient, force: bool = False) -> int:
    """Copy what qBittorrent's feeds hold into the cache; returns rows added.

    This is the one place that waits on qBittorrent's RSS: it blocks until the
    feeds have settled (and, when forced, refreshed). It runs on a thread with
    its own session and does not need the cycle slot.
    """
    with _ingest_lock:
        snapshot = RssSnapshot(qbit)
        articles = snapshot.refresh() if force else snapshot.get()
        _failed_feed_names[:] = snapshot.failed_feed_names
        with Session(engine) as session:
            return store_matches(session, articles)


def prune_cache(engine) -> int:
    with Session(engine) as session:
        return prune(session)


def backfill_cache(engine) -> int:
    with Session(engine) as session:
        return backfill_from_seen(session)


class CachedRssSnapshot(RssSnapshot):
    """A snapshot that reads the cache instead of waiting on qBittorrent."""

    def __init__(self, qbit_client: QBitClient, session: Session):
        super().__init__(qbit_client)
        self.session = session

    def get(self) -> Dict[str, List[Dict[str, Any]]]:
        self.failed_feed_names = last_failed_feed_names()
        return cached_articles(self.session)

    def invalidate(self) -> None:
        pass

    def refresh(self, **kwargs) -> Dict[str, List[Dict[str, Any]]]:
        """A live refresh, for the rare caller that needs a fresh look; its result is cached."""
        live = RssSnapshot(self.qbit_client)
        articles = live.refresh(**kwargs)
        _failed_feed_names[:] = live.failed_feed_names
        store_matches(self.session, articles)
        return self.get()
