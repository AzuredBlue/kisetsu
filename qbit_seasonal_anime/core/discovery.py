from datetime import datetime, timezone
import logging
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.matching import match_release_to_show, prepare_aliases
from qbit_seasonal_anime.core.rules import build_regex_pattern
from qbit_seasonal_anime.db.models import Feed, Monitored

logger = logging.getLogger("qbit_seasonal_anime.core.discovery")


def parse_article_date(art: Dict[str, Any]) -> datetime:
    d = art.get("date")
    if not d:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(d)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def flatten_rss_articles(rss_tree: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Parse qBittorrent RSS item tree into a mapping:
    { feed_url: [ { "title": ..., "torrentURL": ..., ... }, ... ] }
    Articles are sorted newest-to-oldest by publication date.
    """
    feed_articles: Dict[str, List[Dict[str, Any]]] = {}

    def traverse(node: Dict[str, Any]):
        for key, val in node.items():
            if isinstance(val, dict):
                if "url" in val:
                    feed_url = val["url"]
                    articles = val.get("articles") or []
                    if isinstance(articles, list):
                        sorted_articles = sorted(articles, key=parse_article_date, reverse=True)
                    else:
                        sorted_articles = []
                    feed_articles[feed_url] = sorted_articles
                else:
                    traverse(val)

    traverse(rss_tree)
    return feed_articles


class RssSnapshot:
    def __init__(self, qbit_client: QBitClient):
        self.qbit_client = qbit_client
        self._articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._load_error: Optional[Exception] = None
        self._load_attempted = False

    def get(self) -> Dict[str, List[Dict[str, Any]]]:
        if self._load_attempted:
            if self._load_error is not None:
                raise self._load_error
            return self._articles_by_url or {}

        self._load_attempted = True
        try:
            rss_tree = self.qbit_client.get_rss_items(with_data=True)
            articles = flatten_rss_articles(rss_tree)
            self._articles_by_url = articles
        except Exception as e:
            self._articles_by_url = {}
            self._load_error = e
            raise
        return articles

    def invalidate(self) -> None:
        self._articles_by_url = None
        self._load_error = None
        self._load_attempted = False

    def refresh(self) -> Dict[str, List[Dict[str, Any]]]:
        self.invalidate()
        self.qbit_client.refresh_rss_feeds()
        rss_tree = self.qbit_client.get_rss_items(with_data=True)
        return flatten_rss_articles(rss_tree)


def discover_feed_for_show(
    monitored: Monitored,
    feeds: List[Feed],
    qbit_client: QBitClient,
    excluded_feed_ids: Optional[List[int]] = None,
    cached_articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    preferred_feed_grace_seconds: int = 300,  # 5 minutes / 1-2 refresh cycles
    rss_snapshot: Optional[RssSnapshot] = None,
    parsed_articles: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Tuple[Feed, Optional[str], Optional[str]]]:
    """
    Discover the best feed for a monitored show.
    Returns (selected_feed, observed_release_group, matched_title) or None if no feeds available.

    Selection strategy:
    1. Filter out excluded_feed_ids.
    2. Sort candidate feeds by priority.
    3. Prefer the highest-priority feed when it has a matching article.
    4. If only a lower-priority feed matches, apply the configured grace period and refresh the preferred feed before falling back.
    """
    excluded = set(excluded_feed_ids or [])
    available_feeds = [f for f in feeds if f.id not in excluded]

    if not available_feeds:
        logger.info(f"No available feeds left for '{monitored.display_name}' (all excluded/empty).")
        return None

    sorted_feeds = sorted(available_feeds, key=lambda f: f.priority)
    top_feed = sorted_feeds[0]
    aliases = monitored.effective_aliases
    test_pattern = build_regex_pattern(aliases)
    prepared_aliases = prepare_aliases(aliases)
    if parsed_articles is None:
        parsed_articles = {}

    try:
        if rss_snapshot is not None:
            articles_by_url = rss_snapshot.get()
        elif cached_articles_by_url is not None:
            articles_by_url = cached_articles_by_url
        else:
            rss_tree = qbit_client.get_rss_items(with_data=True)
            articles_by_url = flatten_rss_articles(rss_tree)
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS cache for discovery: {e}.")
        articles_by_url = {}

    top_articles = articles_by_url.get(top_feed.qbit_feed_url) or []
    for art in top_articles:
        title = art.get("title", "")
        is_match, score, parsed = match_release_to_show(
            title,
            aliases,
            test_pattern=test_pattern,
            prepared_aliases=prepared_aliases,
            parsed_cache=parsed_articles,
        )
        if is_match:
            logger.info(
                f"Discovered #1 priority match for '{monitored.display_name}' in feed '{top_feed.qbit_feed_name}': "
                f"'{title}' (score: {score:.1f})"
            )
            return top_feed, parsed.get("release_group"), parsed.get("title")

    lower_match = None
    for feed in sorted_feeds[1:]:
        articles = articles_by_url.get(feed.qbit_feed_url) or []
        for art in articles:
            title = art.get("title", "")
            is_match, _, parsed = match_release_to_show(
                title,
                aliases,
                test_pattern=test_pattern,
                prepared_aliases=prepared_aliases,
                parsed_cache=parsed_articles,
            )
            if is_match:
                lower_match = (feed, parsed.get("release_group"), parsed.get("title"), art)
                break
        if lower_match:
            break

    if lower_match:
        matched_feed, rel_group, m_title, matched_art = lower_match
        now = datetime.now(timezone.utc)
        air_at = monitored.next_airing_at
        if air_at and air_at.tzinfo is None:
            air_at = air_at.replace(tzinfo=timezone.utc)

        # The grace period lets a higher priority feed catch up once a release
        # drops. Measure it from when the release became visible here rather than
        # from AniList's airing time, which can be stale or still in the future.
        released_at = parse_article_date(matched_art) if matched_art else None
        if released_at and released_at > datetime.min.replace(tzinfo=timezone.utc):
            grace_reference = released_at
        else:
            grace_reference = air_at

        if grace_reference and (now - grace_reference).total_seconds() < preferred_feed_grace_seconds:
            elapsed_m = (now - grace_reference).total_seconds() / 60
            logger.info(
                f"Observed release on Priority #{matched_feed.priority} '{matched_feed.qbit_feed_name}' for '{monitored.display_name}', "
                f"but waiting {preferred_feed_grace_seconds / 60:.1f}m buffer for Priority #{top_feed.priority} '{top_feed.qbit_feed_name}' (elapsed: {elapsed_m:.1f}m)."
            )
            if rss_snapshot is not None:
                rss_snapshot.invalidate()
            qbit_client.refresh_rss_feeds()
            return None

        try:
            if rss_snapshot is not None:
                fresh_articles = rss_snapshot.refresh()
            else:
                qbit_client.refresh_rss_feeds()
                fresh_tree = qbit_client.get_rss_items(with_data=True)
                fresh_articles = flatten_rss_articles(fresh_tree)
            for art in (fresh_articles.get(top_feed.qbit_feed_url) or []):
                t = art.get("title", "")
                m, sc, pr = match_release_to_show(
                    t,
                    aliases,
                    test_pattern=test_pattern,
                    prepared_aliases=prepared_aliases,
                    parsed_cache=parsed_articles,
                )
                if m:
                    logger.info(f"Priority #1 feed '{top_feed.qbit_feed_name}' caught up after refresh for '{monitored.display_name}'!")
                    return top_feed, pr.get("release_group"), pr.get("title")
        except Exception as e:
            logger.debug(f"Fresh check of #1 feed skipped: {e}")

        logger.info(
            f"Fallback to Priority #{matched_feed.priority} feed '{matched_feed.qbit_feed_name}' for '{monitored.display_name}': "
            f"'{m_title}'"
        )
        return matched_feed, rel_group, m_title

    logger.debug(f"No cached releases found for '{monitored.display_name}'. Remaining pending on #1 feed.")
    return None
