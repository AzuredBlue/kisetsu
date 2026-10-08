"""Kisetsu's own copy of the RSS articles that matter, kept in ``seen_feed_items``.

qBittorrent's RSS cache is what the feeds delivered last: it must be refreshed
and settled before it can be read, it is mostly releases for shows nobody
follows, and it is gone when a feed is removed. This keeps the releases that
matter to followed shows, slimmed down, so checks, discovery and the UI read the
database instead of waiting on qBittorrent.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from kisetsu.clients.qbit import QBitClient
from kisetsu.core.discovery import RssSnapshot, parse_article_date
from kisetsu.core.matching import match_release_to_show, prepare_aliases
from kisetsu.core.rules import show_match_patterns
from kisetsu.db.models import Feed, Monitored, MonitoredStatus, SeenFeedItem, utc_now

logger = logging.getLogger("kisetsu.core.feedcache")

# The only article fields anything reads; the rest (description, seeders, ...) is dropped.
CACHE_FIELDS = ("id", "title", "link", "torrentURL", "date", "infoHash", "size")
# Items this young are kept whatever they are, so a show added today can still be
# found in what the feeds posted in the last few days.
RECENT_DAYS = 3
# Older items are kept only while they match a followed show, and never past a season.
MAX_AGE_DAYS = 120
PER_FEED_CAP = 1000
_CHUNK = 500

# Feeds the last ingest could not read; their (stale) cache is not treated as current.
_failed_feed_names: List[str] = []
# One copy from qBittorrent at a time; a second caller just waits and reads the settled result.
_ingest_lock = threading.Lock()


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


class _FollowedShows:
    """Whether a release title belongs to a show that is still being followed."""

    def __init__(self, shows: Iterable[Monitored]):
        self._entries = []
        for show in shows:
            aliases = show.effective_aliases
            pattern, _ = show_match_patterns(aliases, show.matched_title)
            self._entries.append((aliases, pattern, prepare_aliases(aliases)))
        self._parsed: Dict[str, Dict[str, Any]] = {}

    def matches(self, title: str) -> bool:
        return any(
            match_release_to_show(
                title,
                aliases,
                test_pattern=pattern,
                prepared_aliases=prepared,
                parsed_cache=self._parsed,
            )[0]
            for aliases, pattern, prepared in self._entries
        )


def _followed(session: Session) -> _FollowedShows:
    shows = session.exec(select(Monitored).where(Monitored.status != MonitoredStatus.COMPLETED)).all()
    return _FollowedShows(shows)


def upsert_feed_items(
    session: Session,
    articles_by_url: Dict[str, List[Dict[str, Any]]],
    followed: Optional[_FollowedShows] = None,
    now: Optional[datetime] = None,
) -> int:
    """Record the articles worth keeping; returns how many rows were added.

    Safe against a concurrent writer: new rows are inserted with ON CONFLICT DO
    NOTHING, and first-seen and shield times of existing rows are never touched.
    """
    followed = followed or _followed(session)
    now = _naive(now or utc_now())
    recent_cutoff = now - timedelta(days=RECENT_DAYS)
    added = 0
    for feed_url, articles in articles_by_url.items():
        wanted: Dict[str, tuple] = {}
        for article in articles:
            title = article.get("title") or ""
            if not title:
                continue
            item_id = item_id_of(article)
            if item_id in wanted:
                continue
            published = _published_at(article)
            if (published is None or published >= recent_cutoff) or followed.matches(title):
                wanted[item_id] = (article, published)
        if not wanted:
            continue

        ids = list(wanted)
        existing: Dict[str, SeenFeedItem] = {}
        for start in range(0, len(ids), _CHUNK):
            for row in session.exec(
                select(SeenFeedItem).where(
                    SeenFeedItem.feed_url == feed_url,
                    SeenFeedItem.item_id.in_(ids[start:start + _CHUNK]),
                )
            ).all():
                existing[row.item_id] = row

        new_rows = []
        for item_id, (article, published) in wanted.items():
            row = existing.get(item_id)
            if row is None:
                new_rows.append({
                    "feed_url": feed_url,
                    "item_id": item_id,
                    "title": article.get("title", ""),
                    "created_at": now,
                    "data_json": json.dumps(_slim(article), separators=(",", ":")),
                    "published_at": published,
                })
            elif row.data_json is None:
                row.data_json = json.dumps(_slim(article), separators=(",", ":"))
                row.published_at = published
                session.add(row)
        for start in range(0, len(new_rows), _CHUNK):
            chunk = new_rows[start:start + _CHUNK]
            result = session.execute(
                sqlite_insert(SeenFeedItem).values(chunk).on_conflict_do_nothing(
                    index_elements=["feed_url", "item_id"]
                )
            )
            added += max(0, result.rowcount or 0)
    session.commit()
    return added


def cached_articles(session: Session) -> Dict[str, List[Dict[str, Any]]]:
    """The cache in the shape ``flatten_rss_articles`` returns, newest first.

    Every known feed has an entry (empty when nothing is kept for it), except
    feeds the last ingest could not read, which are left out as before.
    """
    articles: Dict[str, List[Dict[str, Any]]] = {
        url: [] for url in session.exec(select(Feed.qbit_feed_url)).all()
    }
    rows = session.exec(
        select(SeenFeedItem.feed_url, SeenFeedItem.data_json)
        .where(SeenFeedItem.data_json.is_not(None))
        .order_by(SeenFeedItem.published_at.desc(), SeenFeedItem.created_at.desc())
    ).all()
    for feed_url, data_json in rows:
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
    """Drop what no longer makes sense to keep; returns how many rows went.

    Unshielded rows older than RECENT_DAYS go unless they match a followed show,
    and nothing stays past MAX_AGE_DAYS. Each feed is then cut to PER_FEED_CAP.
    """
    now = _naive(now or utc_now())
    recent_cutoff = now - timedelta(days=RECENT_DAYS)
    old_cutoff = now - timedelta(days=MAX_AGE_DAYS)
    followed = _followed(session)
    stale: List[int] = []
    for row_id, title, created_at, published_at in session.exec(
        select(SeenFeedItem.id, SeenFeedItem.title, SeenFeedItem.created_at, SeenFeedItem.published_at)
        .where(SeenFeedItem.shielded_at.is_(None))
    ).all():
        reference = published_at or created_at
        if reference is None or reference >= recent_cutoff:
            continue
        if reference < old_cutoff or not followed.matches(title or ""):
            stale.append(row_id)

    for feed_url, count in session.exec(
        select(SeenFeedItem.feed_url, func.count(SeenFeedItem.id)).group_by(SeenFeedItem.feed_url)
    ).all():
        if count <= PER_FEED_CAP:
            continue
        keep = set(session.exec(
            select(SeenFeedItem.id)
            .where(SeenFeedItem.feed_url == feed_url)
            .order_by(SeenFeedItem.published_at.desc(), SeenFeedItem.created_at.desc())
            .limit(PER_FEED_CAP)
        ).all())
        stale.extend(
            row_id for row_id in session.exec(
                select(SeenFeedItem.id).where(SeenFeedItem.feed_url == feed_url, SeenFeedItem.shielded_at.is_(None))
            ).all() if row_id not in keep
        )

    stale = list(dict.fromkeys(stale))
    for start in range(0, len(stale), _CHUNK):
        for row in session.exec(select(SeenFeedItem).where(SeenFeedItem.id.in_(stale[start:start + _CHUNK]))).all():
            session.delete(row)
    session.commit()
    return len(stale)


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
            return upsert_feed_items(session, articles)


def prune_cache(engine) -> int:
    with Session(engine) as session:
        return prune(session)


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
        upsert_feed_items(self.session, articles)
        return self.get()
