import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Set
import httpx2

logger = logging.getLogger("kisetsu.clients.anilist")

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

MAX_SCHEDULE_PAGES = 20

# Requests per minute until AniList reports its own figure in x-ratelimit-limit.
DEFAULT_RATE_LIMIT = 30
# Requests per minute left unused, for actions the user starts by hand.
RATE_LIMIT_RESERVE = 5
# The longest one request waits for its turn; a longer wait is reported to the caller.
MAX_PACING_WAIT_SECONDS = 5.0
# How long to stop asking after a 429 that names no wait, and the most a 429 may impose.
BLOCK_FALLBACK_SECONDS = 60.0
BLOCK_MAX_SECONDS = 120.0
# Requests pause after a call that failed on every retry: this long, doubling up to the maximum.
BREAKER_START_SECONDS = 60.0
BREAKER_MAX_SECONDS = 900.0
# How long one multi-request pass may spend asking AniList before it hands the rest to the next pass.
PASS_BUDGET_SECONDS = 30.0
PASS_RESUME_SECONDS = 30.0

USER_EXISTS_QUERY = """
query ($name: String) {
  User(name: $name) { id }
}
"""

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
          bannerImage
        }
      }
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


class AniListDeferred(AniListError):
    """AniList should not be asked again yet, so a request was not sent or a pass stopped early.

    ``retry_in`` is the number of seconds to wait. ``partial`` holds what a call that
    makes several requests had already fetched when it stopped.
    """

    def __init__(self, message: str, retry_in: float = 0.0, partial: Optional[Dict[Any, Any]] = None):
        super().__init__(message)
        self.retry_in = max(0.0, float(retry_in))
        self.partial: Dict[Any, Any] = partial if partial is not None else {}


class AniListRateLimited(AniListDeferred):
    """AniList is rate limiting this client, or the request budget for now is used up."""


class AniListUnavailable(AniListDeferred):
    """AniList failed on every retry; requests are paused for a while."""


def _header_number(headers: Mapping[str, str], name: str) -> Optional[float]:
    try:
        return float(headers.get(name))
    except (TypeError, ValueError):
        return None


class RateGovernor:
    """Spaces AniList requests so the limit is never exceeded, and remembers when to stop asking.

    One instance serves every AniListClient, so the background loop and the API draw
    on the same allowance. Every method runs on the event loop and none awaits between
    reading and updating its state, so they are never interleaved.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self._clock = clock
        self._wall = wall
        self.reset()

    def reset(self) -> None:
        self.limit = DEFAULT_RATE_LIMIT
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._blocked_by = ""
        self._failures = 0

    @property
    def interval(self) -> float:
        """Seconds between two requests: the limit minus the reserve, spread over a minute."""
        return 60.0 / max(1, self.limit - RATE_LIMIT_RESERVE)

    def retry_in(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def blocked_until_epoch(self) -> Optional[float]:
        wait = self.retry_in()
        return self._wall() + wait if wait > 0 else None

    def _block(self, seconds: float, reason: str) -> None:
        until = self._clock() + seconds
        if until > self._blocked_until:
            self._blocked_until = until
            self._blocked_by = reason

    def _deferral(self, wait: float) -> AniListDeferred:
        if self._blocked_by == "failure":
            return AniListUnavailable(f"AniList is not answering; retry in {wait:.0f}s.", retry_in=wait)
        return AniListRateLimited(f"AniList is rate limiting this client; retry in {wait:.0f}s.", retry_in=wait)

    async def acquire(self) -> None:
        """Wait for this request's turn; raise AniListDeferred when AniList is blocked or the wait is too long."""
        now = self._clock()
        if now < self._blocked_until:
            raise self._deferral(self._blocked_until - now)
        slot = max(now, self._next_slot)
        wait = slot - now
        if wait > MAX_PACING_WAIT_SECONDS:
            raise AniListRateLimited(f"AniList request budget used up; retry in {wait:.0f}s.", retry_in=wait)
        self._next_slot = slot + self.interval
        if wait > 0:
            await asyncio.sleep(wait)

    def note_response(self, headers: Mapping[str, str]) -> None:
        """Take the limit AniList reports, and stop asking once it says none is left."""
        limit = _header_number(headers, "x-ratelimit-limit")
        if limit and limit > 0:
            self.limit = int(limit)
        remaining = _header_number(headers, "x-ratelimit-remaining")
        if remaining is not None and remaining <= 0:
            self._block(BLOCK_FALLBACK_SECONDS, "rate")

    def note_rate_limited(self, headers: Mapping[str, str]) -> float:
        """Record a 429 and return the seconds AniList asked to wait."""
        wait = _header_number(headers, "retry-after")
        if wait is None:
            reset = _header_number(headers, "x-ratelimit-reset")
            wait = reset - self._wall() if reset is not None else BLOCK_FALLBACK_SECONDS
        wait = min(max(wait, 1.0), BLOCK_MAX_SECONDS)
        self._block(wait, "rate")
        return wait

    def note_success(self) -> None:
        self._failures = 0

    def note_failure(self) -> float:
        """Record a call that failed on every retry; requests pause for a growing while."""
        self._failures += 1
        wait = min(BREAKER_START_SECONDS * 2 ** (self._failures - 1), BREAKER_MAX_SECONDS)
        self._block(wait, "failure")
        return wait


rate_governor = RateGovernor()


def rate_limit_status() -> Dict[str, Optional[float]]:
    """When AniList may be asked again, for the status endpoint (None when it is not blocked)."""
    return {"blocked_until": rate_governor.blocked_until_epoch(), "retry_in": rate_governor.retry_in() or None}


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
        """Execute a GraphQL query, spaced by the shared rate governor.

        A 429 is never waited out here: the governor remembers when AniList may be
        asked again and the caller gets AniListRateLimited at once. A server error, a
        lost connection or an unreadable answer is retried with backoff; when every
        retry fails, requests pause for a growing while (AniListUnavailable). Any other
        HTTP error is the request's own fault and raises AniListError without a retry.

        With ``allow_partial`` a GraphQL response that carries both ``data`` and
        ``errors`` returns the usable part instead of raising, so one bad alias
        cannot discard the whole batch.
        """
        backoff = 2.0
        problem = "AniList did not answer."
        async with httpx2.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(max_retries):
                await rate_governor.acquire()
                try:
                    resp = await client.post(
                        ANILIST_GRAPHQL_URL,
                        json={"query": query, "variables": variables},
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                    rate_governor.note_response(resp.headers)
                    if resp.status_code == 429:
                        wait = rate_governor.note_rate_limited(resp.headers)
                        logger.warning(f"AniList rate limited (429). Not asking again for {wait:.0f}s.")
                        raise AniListRateLimited(
                            f"AniList is rate limiting this client; retry in {wait:.0f}s.", retry_in=wait
                        )
                    if 400 <= resp.status_code < 500:
                        raise AniListError(f"AniList HTTP error: {resp.status_code} - {resp.text[:200]}")
                    resp.raise_for_status()
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise ValueError("AniList answered with something other than an object")
                except httpx2.HTTPStatusError as e:
                    problem = f"AniList HTTP error: {e.response.status_code} - {e.response.text[:200]}"
                except httpx2.RequestError as e:
                    problem = f"AniList network connection failed: {e}"
                except ValueError:
                    problem = "AniList returned an unreadable answer."
                else:
                    rate_governor.note_success()
                    if "errors" in data:
                        if allow_partial:
                            payload = data.get("data") or {}
                            payload["__errors__"] = data["errors"]
                            return payload
                        raise AniListError(f"AniList GraphQL error: {data['errors']}")
                    return data.get("data") or {}
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2

        wait = rate_governor.note_failure()
        raise AniListUnavailable(f"{problem} Pausing AniList requests for {wait:.0f}s.", retry_in=wait)

    async def user_exists(self, username: str) -> bool:
        """Whether AniList knows this user name.

        One attempt only: the retry backoff of `_post_query` would make a typo take
        several seconds to report. A missing user comes back as HTTP 404.
        """
        try:
            async with httpx2.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    ANILIST_GRAPHQL_URL,
                    json={"query": USER_EXISTS_QUERY, "variables": {"name": username.strip()}},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
        except httpx2.RequestError as e:
            raise AniListError(f"AniList network connection failed: {e}") from e
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise AniListError(f"AniList HTTP error: {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise AniListError("AniList returned an unreadable answer.") from e
        return bool((data.get("data") or {}).get("User"))

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
                    "banner_image": media.get("bannerImage") or "",
                }

        return list(anime_dict.values())

    async def fetch_media_airing_schedules(
        self,
        media_ids: List[int],
    ) -> Dict[int, List[Dict[str, Any]]]:
        """Fetch complete airing schedules, isolating per-show failures.

        A single unusable media ID must not cost the schedules of every other show,
        so its failure is logged and skipped. When AniList defers the rest (a rate
        limit, a pause after repeated failures, or this pass's time budget), the shows
        already fetched are not lost: they travel on the AniListDeferred as ``partial``
        and the remaining ids are fetched on a later pass.
        """
        unique_ids = list(dict.fromkeys(int(media_id) for media_id in media_ids))
        if not unique_ids:
            return {}
        schedules: Dict[int, List[Dict[str, Any]]] = {}
        deadline = time.monotonic() + PASS_BUDGET_SECONDS
        for media_id in unique_ids:
            if time.monotonic() >= deadline:
                raise AniListRateLimited(
                    "AniList request budget for this pass is used up.",
                    retry_in=PASS_RESUME_SECONDS,
                    partial=schedules,
                )
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
            except AniListDeferred as e:
                e.partial = schedules
                raise
            except AniListError as e:
                logger.warning(f"Could not fetch airing schedule for media {media_id}: {e}")
                continue
            except Exception as e:
                logger.warning(f"Unexpected airing schedule failure for media {media_id}: {e}")
                continue
            schedules[media_id] = schedule
        return schedules
