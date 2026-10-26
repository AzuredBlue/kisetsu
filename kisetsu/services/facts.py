from collections import defaultdict
from typing import Dict, Iterable, List

from sqlmodel import select

from kisetsu.db.schema import Episode, EpisodeStatus, FeedState, Show
from kisetsu.domain.schedule import EpisodeFacts, ShowFacts
from kisetsu.services.context import Context


def facts_for(ctx: Context, shows: Iterable[Show]) -> Dict[int, ShowFacts]:
    """Plain-data views of shows for the pure scheduling and status rules."""
    shows = list(shows)
    ids = [show.id for show in shows]
    episodes: Dict[int, List[Episode]] = defaultdict(list)
    if ids:
        for episode in ctx.session.exec(select(Episode).where(Episode.show_id.in_(ids))).all():
            episodes[episode.show_id].append(episode)
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
            episodes=tuple(
                EpisodeFacts(e.number, e.air_at, e.status, e.retry_after) for e in sorted(rows, key=lambda e: e.number)
            ),
        )
    return result
