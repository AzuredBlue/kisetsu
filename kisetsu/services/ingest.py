"""Keeping what the feeds publish: store each new article once, and match it to the followed shows."""

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, func
from sqlmodel import select

from kisetsu.clients.rss import FeedArticle
from kisetsu.db.schema import Episode, Feed, FeedItem, FeedItemMatch, Show
from kisetsu.domain import matcher as matching
from kisetsu.domain.parsing import is_batch, parse_release_title
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
    # Shows matched on a feed their searches had left out as not carrying them: they are to be searched again.
    reconsider: Set[int] = field(default_factory=set)


def followed_shows(ctx: Context) -> List[Show]:
    """Shows whose releases are looked for: not completed, and already near enough to premiere."""
    shows = list(ctx.session.exec(select(Show).where(Show.completed == False)).all())  # noqa: E712
    facts = facts_for(ctx, shows)
    return [show for show in shows if not premiere_out_of_reach(facts[show.id], ctx.now)]


def _matchers(ctx: Context) -> Tuple[Dict[int, str], List[Tuple[int, matching.Matcher]]]:
    """Each followed show's matcher, and the signature of what it was built from, by show id."""
    shows = followed_shows(ctx)
    minimum = ctx.settings.min_resolution
    # The minimum resolution decides what matches too, so a change to it rematches every show.
    signatures = {
        show.id: matching.signature([(show.id, show.effective_aliases, show.learned_name or "")], extra=f"min_resolution={minimum}")
        for show in shows
    }
    key = json.dumps(sorted(signatures.items()))
    cached = ctx.caches.matchers
    if cached is not None and cached[0] == key:
        return cached[1], cached[2]
    built = [(show.id, matching.build_matcher(show.effective_aliases, show.learned_name, minimum)) for show in shows]
    ctx.caches.matchers = (key, signatures, built)
    return signatures, built


def _known(ctx: Context) -> Set[Tuple[int, str]]:
    if ctx.caches.known_items is None:
        ctx.caches.known_items = set(ctx.session.exec(select(FeedItem.feed_id, FeedItem.guid)).all())
    return ctx.caches.known_items


def _new_item(feed: Feed, article: FeedArticle, first_seen_at) -> FeedItem:
    # Every single release is parsed, whatever its resolution: the minimum is a setting that can change later.
    parsed = parse_release_title(article.title) if not is_batch(article.title) else {}
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


def ingest(ctx: Context, feed: Feed, articles: Sequence[FeedArticle]) -> IngestResult:
    """Store the articles this feed has not shown before and match them to the followed shows."""
    result = IngestResult()
    known = _known(ctx)
    _, matchers = _matchers(ctx)
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
        for show_id in matching.matching_show_ids(matchers, item.title, cache):
            ctx.session.add(FeedItemMatch(item_id=item.id, show_id=show_id))
            result.show_ids.add(show_id)
            result.matched += 1
            if feed.id in ctx.caches.skipped.get(show_id, ()):
                result.reconsider.add(show_id)
    if result.matched:
        ctx.emit("shows")
    return result


def rematch(ctx: Context, show_ids: Optional[Iterable[int]] = None) -> int:
    """Match every stored item against the followed shows again; returns the matches made.

    With ``show_ids`` only those shows' matches are redone, after an alias edit.
    """
    only = None if show_ids is None else set(show_ids)
    signatures, matchers = _matchers(ctx)
    if only is None:
        # A completed show is not matched any more, so its Releases tab lives on what it has until ``prune`` ages that out.
        completed = select(Show.id).where(Show.completed == True)  # noqa: E712
        ctx.session.execute(delete(FeedItemMatch).where(FeedItemMatch.show_id.not_in(completed)))
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
        set_state(ctx, SIGNATURE_KEY, json.dumps(signatures))
    ctx.emit("shows")
    return made


def ensure_matches_current(ctx: Context) -> bool:
    """Redo the matches of the shows that were added, removed, or whose aliases or learned name changed since they were made.

    Only those shows are tried against every stored item; the items they touch are then matched again against every
    followed show, so a title several shows compete for still goes to the right one. Returns whether anything was redone.
    """
    signatures, matchers = _matchers(ctx)
    try:
        stored = json.loads(get_state(ctx, SIGNATURE_KEY) or "null")
    except ValueError:
        stored = None
    if not isinstance(stored, dict):
        # Nothing to compare with (a new database, or one from before signatures were kept per show).
        rematch(ctx)
        return True
    stored = {int(show_id): value for show_id, value in stored.items()}
    changed = {show_id for show_id, value in signatures.items() if stored.get(show_id) != value}
    removed = set(stored) - set(signatures)
    if not changed and not removed:
        return False
    _rematch_affected(ctx, matchers, changed, removed)
    set_state(ctx, SIGNATURE_KEY, json.dumps(signatures))
    ctx.emit("shows")
    return True


def _rematch_affected(ctx: Context, matchers: List[Tuple[int, matching.Matcher]], changed: Set[int], removed: Set[int]) -> None:
    """``rematch`` limited to the items the changed or removed shows matched before or match now."""
    completed = set(ctx.session.exec(select(Show.id).where(Show.completed == True)).all())  # noqa: E712
    touched = changed | removed
    affected = set(ctx.session.exec(select(FeedItemMatch.item_id).where(FeedItemMatch.show_id.in_(touched))).all())
    by_id = dict(matchers)
    newcomers = [by_id[show_id] for show_id in changed if show_id in by_id]
    cache = ctx.caches.parsed_cache()
    titles = dict(ctx.session.exec(select(FeedItem.id, FeedItem.title)).all())
    for item_id, title in titles.items():
        if item_id not in affected and any(matching.broad_match(m, title, cache) for m in newcomers):
            affected.add(item_id)
    gone = removed - completed
    if gone:
        # A completed show keeps its matches for its Releases tab, as ``rematch`` does.
        ctx.session.execute(delete(FeedItemMatch).where(FeedItemMatch.show_id.in_(gone)))
    ordered = sorted(affected)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        ctx.session.execute(delete(FeedItemMatch).where(FeedItemMatch.item_id.in_(chunk), FeedItemMatch.show_id.not_in(completed)))
    for item_id in ordered:
        title = titles.get(item_id)
        if title is None:
            continue
        for show_id in matching.matching_show_ids(matchers, title, cache):
            if show_id not in completed:
                ctx.session.add(FeedItemMatch(item_id=item_id, show_id=show_id))
    ctx.session.flush()


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
