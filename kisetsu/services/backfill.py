"""Searching the feeds' sites for past episodes that scrolled out of the RSS ("Search older")."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx2
from sqlmodel import select

from kisetsu.clients import sites
from kisetsu.clients.rss import FeedArticle, FeedFetchError
from kisetsu.db.schema import Episode, EpisodeStatus, Feed, FeedState, Show
from kisetsu.domain.matcher import build_matcher, decide
from kisetsu.domain.parsing import parse_release_title
from kisetsu.domain.schedule import plausible_for_show
from kisetsu.services import downloads, feeds, grab, ingest
from kisetsu.services.context import Context

logger = logging.getLogger("kisetsu.services.backfill")

# The same search is not repeated for the same missing episodes within this time.
REPEAT = timedelta(hours=6)


@dataclass
class Due:
    show_id: int
    name: str
    terms: List[str]
    feeds: List[Feed]
    missing: List[int]
    signature: int
    first_air: Optional[datetime] = None


@dataclass
class Searched:
    """What a search returned, by feed id, and whether it stopped early."""

    articles: Dict[int, List[FeedArticle]] = field(default_factory=dict)
    stopped: Optional[str] = None


def _searchable_feeds(ctx: Context, show: Show) -> List[Feed]:
    all_feeds = feeds.ranked(ctx)
    if show.feed_state in (FeedState.LOCKED, FeedState.PINNED):
        # A locked show reads one feed; searching others would only fill the store with releases it may not use.
        all_feeds = [f for f in all_feeds if f.id == show.feed_id][:1]
    return [f for f in all_feeds if sites.site_of(f.url)]


def due(
    ctx: Context,
    *,
    show_id: Optional[int] = None,
    force: bool = False,
    unmanaged: bool = False,
    unsearchable: Optional[List[int]] = None,
) -> List[Due]:
    """Shows with an aired episode that is still wanted and that no stored feed item can supply.

    Searching is only for what the feeds have lost. ``unmanaged`` also counts finished
    episodes Kisetsu holds no torrent for. ``unsearchable``, when given, collects the ids of
    shows skipped because no feed can be searched or the show has no usable search term.
    """
    query = select(Show)
    if show_id is not None:
        # Searching one show by hand also covers a paused one, and with ``unmanaged`` a finished one.
        query = query.where(Show.id == show_id)
    else:
        query = query.where(Show.completed == False, Show.paused == False)  # noqa: E712
    result: List[Due] = []
    for show in ctx.session.exec(query).all():
        if show_id is None and show.completed:
            continue
        wanted = _outstanding(ctx, show, unmanaged)
        if not wanted:
            continue
        # The names that found releases before come first, so the few terms searched include them.
        titles = [show.learned_name, *show.custom_aliases, show.title_romaji, show.title_english, *show.aliases]
        terms = sites.search_terms(titles)
        show_feeds = _searchable_feeds(ctx, show)
        if not terms or not show_feeds:
            if unsearchable is not None:
                unsearchable.append(show.id)
            continue
        covered = grab.covered_numbers(ctx, show)
        missing = [n for n in wanted if n not in covered]
        if not missing:
            continue
        signature = hash((tuple(terms), tuple(f.id for f in show_feeds), tuple(missing)))
        last = ctx.caches.searched.get(show.id)
        if not force and last and last[0] == signature and ctx.now - last[1] < REPEAT:
            continue
        air_times = ctx.session.exec(select(Episode.air_at).where(Episode.show_id == show.id, Episode.air_at.is_not(None))).all()
        result.append(Due(show.id, show.title(ctx.settings.title_language), terms, show_feeds, missing, signature, min(air_times, default=None)))
    return result


def _outstanding(ctx: Context, show: Show, unmanaged: bool) -> List[int]:
    numbers = []
    rows = ctx.session.exec(
        select(Episode).where(Episode.show_id == show.id, Episode.air_at.is_not(None), Episode.air_at <= ctx.now).order_by(Episode.number)
    ).all()
    for episode in rows:
        if episode.status in (EpisodeStatus.WANTED, EpisodeStatus.MISSED):
            numbers.append(episode.number)
        elif unmanaged and episode.status == EpisodeStatus.COMPLETED:
            cur = downloads.current(ctx, episode)
            if cur is None or cur.info_hash is None:
                numbers.append(episode.number)
    return numbers


def search_completed(ctx: Context, show_id: int) -> bool:
    """Whether the show's aired episodes were searched for, or no search has anything left to look for.

    A show without air dates has not been searched, however empty the search looks.
    """
    dated = ctx.session.exec(select(Episode.id).where(Episode.show_id == show_id, Episode.air_at.is_not(None)).limit(1)).first()
    if dated is None:
        return False
    return show_id in ctx.caches.searched or not due(ctx, show_id=show_id, force=True)


def missing_episodes(ctx: Context, show_id: int, unmanaged: bool = False) -> List[int]:
    """The episodes of one show that a forced search would look for (empty: nothing is missing)."""
    found = due(ctx, show_id=show_id, force=True, unmanaged=unmanaged)
    return found[0].missing if found else []


def search_unavailable(ctx: Context, show_id: int, unmanaged: bool = False) -> bool:
    """Whether the show has episodes to look for but no feed or search term to look with."""
    skipped: List[int] = []
    due(ctx, show_id=show_id, force=True, unmanaged=unmanaged, unsearchable=skipped)
    return show_id in skipped


async def _search_site(
    client: httpx2.AsyncClient,
    item: Due,
    site_feeds: List[Feed],
    show_matcher,
    now: datetime,
    searcher,
) -> Searched:
    """Search one site's feeds for the show, one request after another.

    A feed's remaining terms are skipped once its results already cover every missing
    episode; until then the next term may still reach releases the earlier ones did not.
    """
    found = Searched()
    cache: Dict[str, dict] = {}
    for feed in site_feeds:
        covered: set = set()
        for term in item.terms:
            try:
                articles = await searcher(client, feed.url, term, now=now)
            except FeedFetchError as e:
                if "rate limiting" in str(e):
                    found.stopped = str(e)
                    return found
                logger.warning(f"Searching '{feed.name}' for '{term}' failed: {e}")
                continue
            if not articles:
                continue
            found.articles.setdefault(feed.id, []).extend(articles)
            for article in articles:
                if plausible_for_show(article.published_at, item.first_air, now) and decide(show_matcher, article.title, cache)[0]:
                    covered.add(parse_release_title(article.title).get("episode"))
            if covered.issuperset(item.missing):
                break
    return found


async def search(
    client: httpx2.AsyncClient,
    item: Due,
    show_matcher,
    now: datetime,
    searcher=sites.search_feed,
) -> Searched:
    """Run the searches for one show: sites in parallel, each site's requests in turn."""
    by_site: Dict[Optional[str], List[Feed]] = {}
    for feed in item.feeds:
        by_site.setdefault(sites.site_of(feed.url), []).append(feed)
    results = await asyncio.gather(*(_search_site(client, item, group, show_matcher, now, searcher) for group in by_site.values()))
    merged = Searched()
    for result in results:
        for feed_id, articles in result.articles.items():
            merged.articles.setdefault(feed_id, []).extend(articles)
        merged.stopped = merged.stopped or result.stopped
    return merged


def matcher_for(show: Show):
    return build_matcher(show.effective_aliases, show.learned_name)


def apply(ctx: Context, item: Due, found: Searched) -> int:
    """Store what a search found: dated plausibly for the show, matched to it only. Returns the rows added."""
    show = ctx.session.get(Show, item.show_id)
    if show is None:
        return 0
    added = 0
    for feed_id, articles in found.articles.items():
        feed = ctx.session.get(Feed, feed_id)
        kept = [a for a in articles if plausible_for_show(a.published_at, item.first_air, ctx.now)]
        if feed is None or not kept:
            continue
        added += ingest.ingest(ctx, feed, kept, only={show.id}).new
    if added and show.completed and _outstanding(ctx, show, False):
        # A finished show is not followed, so an episode it never fetched would not be grabbed.
        show.completed = False
    if found.stopped is None:
        ctx.caches.searched[show.id] = (item.signature, ctx.now)
    show.last_backfill_at = ctx.now
    ctx.session.add(show)
    return added
