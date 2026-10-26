"""Keeping what the feeds publish: store each new article once, and match it to the followed shows."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, func
from sqlmodel import select

from kisetsu.clients.rss import FeedArticle
from kisetsu.db.schema import Episode, Feed, FeedItem, FeedItemMatch, Show
from kisetsu.domain import matcher as matching
from kisetsu.domain.parsing import is_valid_release, parse_release_title
from kisetsu.domain.schedule import PREMIERE_SLACK, premiere_out_of_reach
from kisetsu.services.context import Context, get_state, set_state
from kisetsu.services.facts import facts_for

# Articles not matched to any show are kept this long.
UNMATCHED_KEEP = timedelta(days=30)
# Matches of a completed show are kept this long.
COMPLETED_KEEP = timedelta(days=60)
# Items kept per show, newest first.
PER_SHOW_CAP = 300
SIGNATURE_KEY = "match_signature"


@dataclass
class IngestResult:
    new: int = 0
    matched: int = 0
    show_ids: Set[int] = field(default_factory=set)


def followed_shows(ctx: Context) -> List[Show]:
    """Shows whose releases are looked for: not completed, and already near enough to premiere."""
    shows = list(ctx.session.exec(select(Show).where(Show.completed == False)).all())  # noqa: E712
    facts = facts_for(ctx, shows)
    return [show for show in shows if not premiere_out_of_reach(facts[show.id], ctx.now)]


def _matchers(ctx: Context) -> Tuple[int, List[Tuple[int, matching.Matcher]]]:
    shows = followed_shows(ctx)
    entries = [(show.id, show.effective_aliases, show.learned_name or "") for show in shows]
    current = matching.signature(entries)
    cached = ctx.caches.matchers
    if cached is not None and cached[0] == current:
        return cached
    built = [(show.id, matching.build_matcher(show.effective_aliases, show.learned_name)) for show in shows]
    ctx.caches.matchers = (current, built)
    return ctx.caches.matchers


def _known(ctx: Context) -> Set[Tuple[int, str]]:
    if ctx.caches.known_items is None:
        ctx.caches.known_items = set(ctx.session.exec(select(FeedItem.feed_id, FeedItem.guid)).all())
    return ctx.caches.known_items


def _new_item(feed: Feed, article: FeedArticle, first_seen_at) -> FeedItem:
    parsed = parse_release_title(article.title) if is_valid_release(article.title) else {}
    return FeedItem(
        feed_id=feed.id,
        guid=article.guid,
        info_hash=article.info_hash,
        title=article.title,
        torrent_url=article.torrent_url,
        published_at=article.published_at,
        first_seen_at=first_seen_at,
        episode_raw=parsed.get("episode"),
        version=int(parsed.get("version") or 1),
        release_group=parsed.get("release_group"),
        parsed_title=parsed.get("title"),
    )


def ingest(ctx: Context, feed: Feed, articles: Sequence[FeedArticle], only: Optional[Set[int]] = None) -> IngestResult:
    """Store the articles this feed has not shown before and match them to the followed shows (to ``only``, when given)."""
    result = IngestResult()
    known = _known(ctx)
    signature, matchers = _matchers(ctx)
    cache = ctx.caches.parsed_cache()
    fresh: List[FeedItem] = []
    for article in articles:
        key = (feed.id, article.guid)
        if key in known or not article.title:
            continue
        known.add(key)
        fresh.append(_new_item(feed, article, ctx.now))
    if not fresh:
        return result
    ctx.session.add_all(fresh)
    ctx.session.flush()
    result.new = len(fresh)
    ctx.emit("feed_items")
    for item in fresh:
        for show_id in matching.matching_show_ids(matchers, item.title, cache, only):
            ctx.session.add(FeedItemMatch(item_id=item.id, show_id=show_id))
            result.show_ids.add(show_id)
            result.matched += 1
    if result.matched:
        ctx.emit("shows")
    return result


def rematch(ctx: Context, show_ids: Optional[Iterable[int]] = None) -> int:
    """Match every stored item against the followed shows again; returns the matches made.

    With ``show_ids`` only those shows' matches are redone, after an alias edit.
    """
    only = None if show_ids is None else set(show_ids)
    signature, matchers = _matchers(ctx)
    if only is None:
        ctx.session.execute(delete(FeedItemMatch))
    elif only:
        ctx.session.execute(delete(FeedItemMatch).where(FeedItemMatch.show_id.in_(only)))
    else:
        return 0
    cache = ctx.caches.parsed_cache()
    made = 0
    for item in ctx.session.exec(select(FeedItem)).all():
        for show_id in matching.matching_show_ids(matchers, item.title, cache, only):
            ctx.session.add(FeedItemMatch(item_id=item.id, show_id=show_id))
            made += 1
    if only is None:
        set_state(ctx, SIGNATURE_KEY, str(signature))
    ctx.emit("shows")
    return made


def ensure_matches_current(ctx: Context) -> bool:
    """Redo all matches when the followed shows, their aliases or learned names changed since they were made."""
    signature, _ = _matchers(ctx)
    if get_state(ctx, SIGNATURE_KEY) == str(signature):
        return False
    rematch(ctx)
    return True


def prune(ctx: Context) -> int:
    """Drop what no longer makes sense to keep; returns how many rows went."""
    session = ctx.session
    removed = 0

    # A release published well before its show's first episode aired belongs to an earlier
    # season; nothing can use it. Undated items stay.
    first_airs = session.exec(
        select(Episode.show_id, func.min(Episode.air_at)).where(Episode.air_at.is_not(None)).group_by(Episode.show_id)
    ).all()
    for show_id, first_air in first_airs:
        stale = select(FeedItem.id).where(FeedItem.published_at.is_not(None), FeedItem.published_at < first_air - PREMIERE_SLACK)
        removed += session.execute(
            delete(FeedItemMatch).where(FeedItemMatch.show_id == show_id, FeedItemMatch.item_id.in_(stale))
        ).rowcount or 0

    # A show that has not started airing has no releases of its own.
    shows = list(session.exec(select(Show).where(Show.completed == False)).all())  # noqa: E712
    facts = facts_for(ctx, shows)
    not_started = [s.id for s in shows if premiere_out_of_reach(facts[s.id], ctx.now)]
    if not_started:
        removed += session.execute(delete(FeedItemMatch).where(FeedItemMatch.show_id.in_(not_started))).rowcount or 0

    completed = select(Show.id).where(Show.completed == True)  # noqa: E712
    old = select(FeedItem.id).where(func.coalesce(FeedItem.published_at, FeedItem.first_seen_at) < ctx.now - COMPLETED_KEEP)
    removed += session.execute(
        delete(FeedItemMatch).where(FeedItemMatch.show_id.in_(completed), FeedItemMatch.item_id.in_(old))
    ).rowcount or 0

    for show_id, count in session.exec(
        select(FeedItemMatch.show_id, func.count()).group_by(FeedItemMatch.show_id)
    ).all():
        if count <= PER_SHOW_CAP:
            continue
        keep = (
            select(FeedItem.id)
            .join(FeedItemMatch, FeedItemMatch.item_id == FeedItem.id)
            .where(FeedItemMatch.show_id == show_id)
            .order_by(FeedItem.published_at.desc(), FeedItem.first_seen_at.desc())
            .limit(PER_SHOW_CAP)
        )
        removed += session.execute(
            delete(FeedItemMatch).where(FeedItemMatch.show_id == show_id, FeedItemMatch.item_id.not_in(keep))
        ).rowcount or 0

    matched = select(FeedItemMatch.item_id)
    unmatched_cutoff = ctx.now - UNMATCHED_KEEP
    gone = session.exec(
        select(FeedItem.feed_id, FeedItem.guid).where(FeedItem.id.not_in(matched), FeedItem.first_seen_at < unmatched_cutoff)
    ).all()
    removed += session.execute(
        delete(FeedItem).where(FeedItem.id.not_in(matched), FeedItem.first_seen_at < unmatched_cutoff)
    ).rowcount or 0
    # A pruned item looks new again when the feed still lists it, which re-adds it and its matches.
    if ctx.caches.known_items is not None:
        ctx.caches.known_items.difference_update(gone)
    return removed
