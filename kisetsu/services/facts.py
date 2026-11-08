from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlmodel import select

from kisetsu.db.schema import Download, Episode, EpisodeStatus, FeedItem, FeedState, Show
from kisetsu.domain.schedule import EpisodeFacts, ShowFacts, release_delay
from kisetsu.services.context import Context


def facts_for(ctx: Context, shows: Iterable[Show]) -> Dict[int, ShowFacts]:
    """Plain-data views of shows for the pure scheduling and status rules."""
    shows = list(shows)
    ids = [show.id for show in shows]
    episodes: Dict[int, List[EpisodeFacts]] = defaultdict(list)
    if ids:
        # Only the columns the rules read: loading every episode as a full row costs far more on a big library.
        columns = select(Episode.show_id, Episode.number, Episode.air_at, Episode.status, Episode.retry_after)
        for show_id, number, air_at, status, retry_after in ctx.session.exec(columns.where(Episode.show_id.in_(ids))).all():
            episodes[show_id].append(EpisodeFacts(number, air_at, status, retry_after))
    result: Dict[int, ShowFacts] = {}
    for show in shows:
        rows = episodes.get(show.id, [])
        has_completed = any(e.status == EpisodeStatus.COMPLETED for e in rows)
        locked = show.feed_state == FeedState.LOCKED
        result[show.id] = ShowFacts(
            id=show.id,
            name=show.title(ctx.settings.title_language),
            paused=show.paused,
            completed=show.completed,
            in_premiere=not has_completed and not locked,
            working=locked,
            anilist_status=show.anilist_status,
            next_airing_episode=show.next_airing_episode,
            next_airing_at=show.next_airing_at,
            episodes=tuple(sorted(rows, key=lambda e: e.number)),
        )
    return result


def release_delays(ctx: Context, show_ids: Optional[Iterable[int]] = None) -> Dict[int, timedelta]:
    """Each show's learned delay from air time to release (``schedule.release_delay``), for the shows that have one.

    Learned from the show's downloads that came from an item of its current feed, adopted ones included: first
    versions of single episodes with an AniList air time, dated by the feed itself, so when Kisetsu happened to read
    the feed does not count.
    """
    query = (
        select(Episode.show_id, Episode.id, Episode.air_at, FeedItem.published_at)
        .join(Download, Download.episode_id == Episode.id)
        .join(Show, Show.id == Episode.show_id)
        .join(FeedItem, (FeedItem.feed_id == Download.feed_id) & (FeedItem.guid == Download.feed_item_guid))
        .where(
            Download.feed_id == Show.feed_id,
            Download.version == 1,
            Download.batch == False,  # noqa: E712
            Download.supersedes_id.is_(None),
            Episode.air_at.is_not(None),
            FeedItem.published_at.is_not(None),
        )
    )
    if show_ids is not None:
        query = query.where(Episode.show_id.in_(list(show_ids)))
    # One release per episode: a retried add is the same release again.
    samples: Dict[int, Dict[int, Tuple[datetime, datetime]]] = defaultdict(dict)
    for show_id, episode_id, air_at, published_at in ctx.session.exec(query).all():
        if published_at.year <= 2000:
            continue
        known = samples[show_id].get(episode_id)
        if known is None or published_at < known[1]:
            samples[show_id][episode_id] = (air_at, published_at)
    delays = {show_id: release_delay(pairs.values()) for show_id, pairs in samples.items()}
    return {show_id: delay for show_id, delay in delays.items() if delay is not None}
