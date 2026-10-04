import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Set
import httpx2

logger = logging.getLogger("qbit_seasonal_anime.clients.anilist")

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

MAX_SCHEDULE_PAGES = 20

USER_SEASONAL_QUERY = """
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME, status_in: [CURRENT, PLANNING, COMPLETED]) {
    lists {
      entries {
        status
        media {
          id
          title {
            romaji
            english
            native
            userPreferred
          }
          synonyms
          status
          episodes
          nextAiringEpisode {
            airingAt
            episode
          }
          season
          seasonYear
          coverImage {
            large
          }
        }
      }
    }
  }
}
"""

MEDIA_DETAILS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title {
      romaji
      english
      native
      userPreferred
    }
    synonyms
    status
    episodes
    nextAiringEpisode {
      airingAt
      episode
    }
  }
}
"""


AIRING_SCHEDULE_QUERY = """
query ($mediaId: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo {
      hasNextPage
      currentPage
    }
    airingSchedules(mediaId: $mediaId, sort: TIME) {
      episode
      airingAt
    }
  }
}
"""


class AniListError(Exception):
    """Base exception for AniList API errors."""
    pass


class AniListRateLimited(AniListError):
    """Raised when AniList keeps rate limiting this client."""


def get_current_and_next_season(dt: Optional[datetime] = None):
    """
    Calculate current and next anime season and year.
    Seasons: WINTER (Jan-Mar), SPRING (Apr-Jun), SUMMER (Jul-Sep), FALL (Oct-Dec)
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    month = dt.month
    year = dt.year

    if 1 <= month <= 3:
        cur_season = "WINTER"
        next_season = "SPRING"
        cur_year = year
        next_year = year
    elif 4 <= month <= 6:
        cur_season = "SPRING"
        next_season = "SUMMER"
        cur_year = year
        next_year = year
    elif 7 <= month <= 9:
        cur_season = "SUMMER"
        next_season = "FALL"
        cur_year = year
        next_year = year
    else:
        cur_season = "FALL"
        next_season = "WINTER"
        cur_year = year
        next_year = year + 1

    return (cur_season, cur_year), (next_season, next_year)


class AniListClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.last_sync_at: Optional[datetime] = None
        self._rate_limited_until = 0.0

    @property
    def rate_limited(self) -> bool:
        """True while a previously observed Retry-After window is still open."""
        return time.monotonic() < self._rate_limited_until

    def seconds_since_last_sync(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since the last successful query, or None if there has not been one."""
        if self.last_sync_at is None:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        return (now - self.last_sync_at).total_seconds()

    def is_sync_due(self, min_age_seconds: float, now: Optional[datetime] = None) -> bool:
        """True when the cached schedule is older than min_age_seconds, or absent."""
        age = self.seconds_since_last_sync(now)
        if age is None:
            return True
        return age >= min_age_seconds

    async def _post_query(
        self,
        query: str,
        variables: Dict[str, Any],
        max_retries: int = 3,
        allow_partial: bool = False,
    ) -> Dict[str, Any]:
        """Execute GraphQL query with exponential backoff on rate limits (HTTP 429).

        With ``allow_partial`` a GraphQL response that carries both ``data`` and
        ``errors`` returns the usable part instead of raising, so one bad alias
        cannot discard the whole batch.
        """
        backoff = 2.0
        async with httpx2.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                try:
                    resp = await client.post(
                        ANILIST_GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                    if resp.status_code == 429:
                        raw_retry = resp.headers.get("Retry-After")
                        try:
                            retry_after = float(raw_retry) if raw_retry else backoff
                        except (ValueError, TypeError):
                            retry_after = backoff
                        logger.warning(f"AniList rate limited (429). Waiting {retry_after} seconds...")
                        self._rate_limited_until = time.monotonic() + retry_after
                        if attempt == max_retries - 1:
                            raise AniListRateLimited(
                                "AniList is rate limiting this client; schedule sync is deferred."
                            )
                        await asyncio.sleep(retry_after)
                        backoff *= 2
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    if "errors" in data:
                        if allow_partial:
                            payload = data.get("data") or {}
                            payload["__errors__"] = data["errors"]
                            return payload
                        raise AniListError(f"AniList GraphQL error: {data['errors']}")
                    return data.get("data") or {}
                except httpx2.HTTPStatusError as e:
                    if attempt == max_retries - 1:
                        raise AniListError(f"AniList HTTP error: {e.response.status_code} - {e.response.text}") from e
                    await asyncio.sleep(backoff)
                    backoff *= 2
                except httpx2.RequestError as e:
                    if attempt == max_retries - 1:
                        raise AniListError(f"AniList network connection failed: {e}") from e
                    await asyncio.sleep(backoff)
                    backoff *= 2

        raise AniListError("Max retries exceeded querying AniList API")

    async def fetch_user_seasonal_anime(
        self, username: str, monitored_anilist_ids: Optional[Set[int]] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch user's anime filtered to:
        1. Currently releasing anime
        2. Upcoming anime planned for next season (or current season if not yet released)
        3. Finished anime from the current season (or extending cour monitored shows)
        4. Already-monitored anime the user has marked COMPLETED on their list

        Each returned dict carries the entry's own list status as "list_status", so
        the caller can treat "I finished watching this" as a completion signal.
        """
        if not username.strip():
            return []

        data = await self._post_query(USER_SEASONAL_QUERY, {"userName": username.strip()})
        self.last_sync_at = datetime.now(timezone.utc)
        collection = data.get("MediaListCollection") or {}
        lists = collection.get("lists", [])

        (cur_season, cur_year), (next_season, next_year) = get_current_and_next_season()

        anime_dict: Dict[int, Dict[str, Any]] = {}
        for l in lists:
            for entry in l.get("entries", []):
                media = entry.get("media")
                if not media:
                    continue

                media_id = media["id"]
                if media_id in anime_dict:
                    continue

                # The user's own list status (CURRENT/PLANNING/COMPLETED), distinct
                # from media["status"] which is the show's broadcast state.
                list_status = entry.get("status")

                status = media.get("status")
                season = media.get("season")
                season_year = media.get("seasonYear")
                next_airing = media.get("nextAiringEpisode")

                is_currently_releasing = (status == "RELEASING")
                is_current_or_next_season_planned = (
                    (season == next_season and season_year == next_year) or
                    (season == cur_season and season_year == cur_year and status == "NOT_YET_RELEASED")
                )
                is_current_season_finished = (
                    season == cur_season and season_year == cur_year and status == "FINISHED"
                )
                is_monitored = bool(monitored_anilist_ids and media_id in monitored_anilist_ids)

                # A list-completed entry means the user has finished watching, so it
                # must never be introduced as a new show - the broadcast-state clauses
                # below can match an old finished show from a current season and would
                # otherwise have us arm a download rule for something already watched.
                # Monitored shows still fall through, because the caller has to see
                # them in order to stand their existing rule down.
                if list_status == "COMPLETED" and not is_monitored:
                    continue

                # is_monitored also carries list-completed entries through: a show the
                # user finished watching is no longer CURRENT/PLANNING, so none of the
                # broadcast-state clauses would match, but we still need to see it in
                # order to mark it COMPLETED. The rest of the completed backlog falls
                # out here.
                if not (is_currently_releasing or is_current_or_next_season_planned or is_current_season_finished or is_monitored):
                    continue

                titles = media.get("title", {})
                aliases = set()
                for key in ["romaji", "english", "native", "userPreferred"]:
                    val = titles.get(key)
                    if val and isinstance(val, str) and val.strip():
                        aliases.add(val.strip())
                for syn in media.get("synonyms", []):
                    if syn and isinstance(syn, str) and syn.strip():
                        aliases.add(syn.strip())

                next_airing_episode = next_airing.get("episode") if next_airing else None
                next_airing_at = None
                if next_airing and next_airing.get("airingAt"):
                    next_airing_at = datetime.fromtimestamp(next_airing["airingAt"], tz=timezone.utc)

                preferred_title = (
                    titles.get("userPreferred")
                    or titles.get("english")
                    or titles.get("romaji")
                    or f"Anime_{media_id}"
                )

                anime_dict[media_id] = {
                    "anilist_id": media_id,
                    "display_name": preferred_title,
                    "title_romaji": titles.get("romaji") or "",
                    "title_english": titles.get("english") or "",
                    "aliases": list(aliases),
                    "list_status": list_status or "",
                    "status": status or "UNKNOWN",
                    "total_episodes": media.get("episodes"),
                    "next_airing_episode": next_airing_episode,
                    "next_airing_at": next_airing_at,
                    "season": season,
                    "season_year": season_year,
                    "cover_image": (media.get("coverImage") or {}).get("large") or "",
                }

        return list(anime_dict.values())

    async def fetch_media_airing_schedules(
        self,
        media_ids: List[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Fetch complete airing schedules, isolating per-show failures.

        A single unusable media ID, or an AniList-wide rate limit, must not cost
        the schedules of every other show, so failures are logged and skipped
        and only a rate limit aborts the whole pass.
        """
        unique_ids = list(dict.fromkeys(int(media_id) for media_id in media_ids))
        if not unique_ids:
            return {}
        schedules: Dict[int, List[Dict[str, Any]]] = {}
        for media_id in unique_ids:
            schedule: List[Dict[str, Any]] = []
            page = 1
            seen_episodes = set()
            try:
                while True:
                    data = await self._post_query(
                        AIRING_SCHEDULE_QUERY,
                        {"mediaId": media_id, "page": page},
                        allow_partial=True,
                    )
                    block = data.get("Page") or {}
                    nodes = block.get("airingSchedules") or []
                    for node in nodes:
                        episode = node.get("episode")
                        airing_at = node.get("airingAt")
                        if episode is None or airing_at is None:
                            continue
                        episode = int(episode)
                        if episode in seen_episodes:
                            continue
                        seen_episodes.add(episode)
                        schedule.append({
                            "episode": episode,
                            "airing_at": datetime.fromtimestamp(airing_at, tz=timezone.utc),
                        })
                    page_info = block.get("pageInfo") or {}
                    if not page_info.get("hasNextPage") or page >= MAX_SCHEDULE_PAGES:
                        break
                    page += 1
            except AniListRateLimited:
                raise
            except AniListError as e:
                logger.warning(f"Could not fetch airing schedule for media {media_id}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Unexpected airing schedule failure for media {media_id}: {e}")
                continue
            schedules[media_id] = schedule
        return schedules

    async def fetch_media_airing_schedule(self, media_id: int) -> List[Dict[str, Any]]:
        schedules = await self.fetch_media_airing_schedules([media_id])
        return schedules.get(int(media_id), [])

    async def fetch_media_details(self, media_id: int) -> Optional[Dict[str, Any]]:
        """Fetch updated episode and airing details for a specific media ID."""
        data = await self._post_query(MEDIA_DETAILS_QUERY, {"id": media_id})
        media = data.get("Media")
        if not media:
            return None

        titles = media.get("title", {})
        aliases = set()
        for key in ["romaji", "english", "native", "userPreferred"]:
            val = titles.get(key)
            if val and isinstance(val, str) and val.strip():
                aliases.add(val.strip())
        for syn in media.get("synonyms", []):
            if syn and isinstance(syn, str) and syn.strip():
                aliases.add(syn.strip())

        next_airing = media.get("nextAiringEpisode")
        next_airing_episode = next_airing["episode"] if next_airing else None
        next_airing_at = None
        if next_airing and next_airing.get("airingAt"):
            next_airing_at = datetime.fromtimestamp(next_airing["airingAt"], tz=timezone.utc)

        return {
            "anilist_id": media_id,
            "display_name": titles.get("userPreferred") or titles.get("english") or titles.get("romaji"),
            "aliases": list(aliases),
            "status": media.get("status"),
            "total_episodes": media.get("episodes"),
            "next_airing_episode": next_airing_episode,
            "next_airing_at": next_airing_at,
        }
