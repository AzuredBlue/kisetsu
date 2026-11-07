"""Searching the feeds' sites for past episodes that scrolled out of the RSS ("Search older")."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx2
from sqlmodel import select

from kisetsu.clients import sites
from kisetsu.clients.rss import FeedArticle, FeedFetchError
from kisetsu.db.schema import Episode, EpisodeStatus, Feed, FeedState, Show
from kisetsu.config import DEFAULT_MIN_RESOLUTION
from kisetsu.domain.matcher import broad_match, build_matcher, decide
from kisetsu.domain.numbering import map_episode
from kisetsu.domain.schedule import plausible_for_show
from kisetsu.services import downloads, feeds, grab, ingest
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for

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
    # What maps a feed's episode numbers onto the show's, as the grab does.
    air_times: Dict[int, datetime] = field(default_factory=dict)
    target_count: Optional[int] = None
    latest_aired: Optional[int] = None
    tolerance_hours: int = 0

    def numbers(self, releases: List[Tuple[int, Optional[datetime]]], now: datetime) -> Set[int]:
        """The show's episodes that one feed's releases (raw number, published) are for.

        A feed counting on from an earlier season ("49" for episode 1) is mapped, so its
        releases count as found. Without the show's schedule the numbers are taken as they are.
        """
        if self.target_count is None:
            return {raw for raw, _ in releases}
        newest_raw = max((raw for raw, _ in releases), default=None)
        found: Set[int] = set()
        for raw, published_at in releases:
            local = map_episode(
                raw, offset=None, latest_aired=self.latest_aired, target_count=self.target_count,
                air_times=self.air_times, published_at=published_at, tolerance_hours=self.tolerance_hours,
                now=now, newest_raw=newest_raw,
            ).local
            if local is not None:
                found.add(local)
        return found


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
        if not force and last is None and show.last_backfill_at is not None and ctx.now - show.last_backfill_at < REPEAT:
            # Searched before a restart, which forgot what was searched for: the stored time stands in for it.
            continue
        state = grab.show_state(ctx, show, facts_for(ctx, [show])[show.id])
        result.append(Due(
            show.id, show.title(ctx.settings.title_language), terms, show_feeds, missing, signature, state.first_air,
            state.air_times, state.target_count, state.newest_aired, ctx.tolerance_hours,
        ))
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


def search_pending(ctx: Context, show_id: int, numbers: List[int]) -> bool:
    """Whether a search of the feeds' sites for these episodes is still to run, so they may yet turn up."""
    last = ctx.caches.searched.get(show_id)
    if last is not None and last[2].issuperset(numbers):
        return False
    return bool(due(ctx, show_id=show_id))


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
    across_feeds: bool = False,
    grouping: Optional[sites.Grouping] = None,
) -> Searched:
    """Search the feeds for the show, one request after another.

    A feed's remaining terms are skipped once its results already cover every missing
    episode; until then the next term may still reach releases the earlier ones did not.
    With ``across_feeds`` what one feed found counts for the next, and once every missing
    episode is covered the remaining feeds are not asked at all.

    With ``grouping`` a nyaa feed is asked for all the show's names in one search, once a
    check has shown the site answers that as it answers the names one by one. A page short of
    full then holds every match, so the names are not asked for alone; a full page may have
    left some out, so they still are.
    """
    found = Searched()
    cache: Dict[str, dict] = {}
    covered: Set[int] = set()

    async def ask(feed: Feed, term: str) -> Optional[List[FeedArticle]]:
        try:
            return await searcher(client, feed.url, term, now=now)
        except FeedFetchError as e:
            if "rate limiting" in str(e):
                found.stopped = str(e)
            else:
                logger.warning(f"Searching '{feed.name}' for '{term}' failed: {e}")
            return None

    for feed in site_feeds:
        if not across_feeds:
            covered = set()
        elif covered.issuperset(item.missing):
            break
        before = covered
        releases: List[Tuple[int, Optional[datetime]]] = []
        host = urlparse(feed.url).hostname or feed.url
        # Several names in one search is nyaa's form; even there it is used only once checked.
        groupable = grouping is not None and sites.site_of(feed.url) == "nyaa" and len(item.terms) > 1
        works = grouping.works(host) if groupable else None
        grouped = sites.any_of(item.terms)
        queries = [grouped, *item.terms] if groupable and works else list(item.terms)
        # The search that checks the host asks for every name alone, so the grouped search can be compared with them.
        claimed = groupable and works is None and grouping.claim(host)
        checking = claimed
        # Each page found, and whether it held every match (judged before it counts towards the host's page size).
        pages: Dict[str, Tuple[List[FeedArticle], bool]] = {}
        try:
            for term in queries:
                articles = await ask(feed, term)
                if found.stopped:
                    return found
                if articles is None:
                    checking = False
                    continue
                complete = grouping is not None and grouping.complete(host, len(articles))
                if grouping is not None:
                    grouping.saw(host, len(articles))
                pages[term] = (articles, complete)
                found.articles.setdefault(feed.id, []).extend(articles)
                for article in articles:
                    if not plausible_for_show(article.published_at, item.first_air, now):
                        continue
                    matched, parsed = decide(show_matcher, article.title, cache)
                    raw = parsed.get("episode")
                    if matched and raw is not None and raw > 0:
                        releases.append((int(raw), article.published_at))
                covered = before | item.numbers(releases, now)
                if groupable and works and term == grouped and complete:
                    # The grouped page held every match: the names alone could find nothing more.
                    break
                if not checking and covered.issuperset(item.missing):
                    break
            if checking:
                await _check_grouping(grouping, host, feed, grouped, pages, ask)
                if found.stopped:
                    return found
        finally:
            if claimed:
                grouping.release(host)
    return found


async def _check_grouping(
    grouping: sites.Grouping, host: str, feed: Feed, grouped: str, pages: Dict[str, Tuple[List[FeedArticle], bool]], ask,
) -> None:
    """Ask the grouped search once, and keep grouping only if it found exactly what the names found alone.

    Nothing is decided from empty pages or pages that may have been cut off: an empty one
    cannot tell a search that works from one that ignores the names, and a cut one may have
    left matches out.
    """
    alone = {article.guid for articles, _ in pages.values() for article in articles}
    if not alone or not all(complete for _, complete in pages.values()):
        return
    together = await ask(feed, grouped)
    if together is None:
        return
    works = grouping.complete(host, len(together)) and {article.guid for article in together} == alone
    grouping.saw(host, len(together))
    grouping.record(host, works)
    if works:
        logger.info(f"{host} finds a show's names in one search, so they are asked for together from now on.")
    else:
        logger.warning(f"{host} did not answer a search for several names as it answers each alone, so each is asked for alone.")


async def search(
    client: httpx2.AsyncClient,
    item: Due,
    show_matcher,
    now: datetime,
    searcher=sites.search_feed,
    stop_when_found: bool = False,
    grouping: Optional[sites.Grouping] = None,
) -> Searched:
    """Run the searches for one show: sites in parallel, each site's requests in turn.

    With ``stop_when_found`` the feeds are asked one after another by priority instead, and
    the lower ones are skipped once what was found covers every missing episode. That saves
    most searches of a first run; it is not for finding which feed holds a show's saved files,
    nor for showing what every feed carries.
    """
    if stop_when_found:
        return await _search_site(client, item, list(item.feeds), show_matcher, now, searcher, across_feeds=True, grouping=grouping)
    by_site: Dict[Optional[str], List[Feed]] = {}
    for feed in item.feeds:
        by_site.setdefault(sites.site_of(feed.url), []).append(feed)
    results = await asyncio.gather(
        *(_search_site(client, item, group, show_matcher, now, searcher, grouping=grouping) for group in by_site.values())
    )
    merged = Searched()
    for result in results:
        for feed_id, articles in result.articles.items():
            merged.articles.setdefault(feed_id, []).extend(articles)
        merged.stopped = merged.stopped or result.stopped
    return merged


def matcher_for(show: Show, min_resolution: int = DEFAULT_MIN_RESOLUTION):
    return build_matcher(show.effective_aliases, show.learned_name, min_resolution)


def apply(ctx: Context, item: Due, found: Searched) -> int:
    """Store what a search found: dated plausibly for the show, and titled so that it could be the show's. Returns the rows added."""
    show = ctx.session.get(Show, item.show_id)
    if show is None:
        return 0
    added = 0
    matcher, cache = matcher_for(show, ctx.settings.min_resolution), ctx.caches.parsed_cache()
    for feed_id, articles in found.articles.items():
        feed = ctx.session.get(Feed, feed_id)
        # Only what this show would match is stored: a stored article is marked known, so a later RSS read would skip it and a
        # followed show it belongs to would never get it. What is kept is matched as any article is, so a title this show and
        # another both match reaches the right one. (A completed show is not followed, hence its own matcher.)
        kept = [
            a for a in articles
            if plausible_for_show(a.published_at, item.first_air, ctx.now) and broad_match(matcher, a.title, cache)
        ]
        if feed is None or not kept:
            continue
        added += ingest.ingest(ctx, feed, kept).new
    if added and show.completed and _outstanding(ctx, show, False):
        # A finished show is not followed, so an episode it never fetched would not be grabbed.
        show.completed = False
    if found.stopped is None:
        # Only a search that ran to the end counts as done; one a site cut short is tried again.
        ctx.caches.searched[show.id] = (item.signature, ctx.now, frozenset(item.missing))
        show.last_backfill_at = ctx.now
    ctx.session.add(show)
    return added
