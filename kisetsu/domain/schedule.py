"""When a show is hunted, when an episode counts as missed, and which release dates are plausible.

Everything here is a pure function of plain data and an explicit ``now``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Collection, Dict, Iterable, List, Optional, Sequence, Tuple

from kisetsu.db.schema import EpisodeStatus

# An aired episode is hunted for this long; after that it is only picked up by the routine feed read.
HUNT_WINDOW = timedelta(hours=24)
# How long the newest aired episode may stay unfound before it counts as missed.
MISSED_AFTER = HUNT_WINDOW
# A release is mapped to the episode that aired at most this long before it was published.
AIR_DATE_TOLERANCE = timedelta(days=3)
# A release may be published this much before the air time AniList gives: those times drift
# by a day or more from the real release, while an earlier season is off by months.
PUBLISHED_BEFORE_AIR_SLACK = timedelta(hours=48)
# A searched release may predate the show's first episode by this much (feeds post early).
PREMIERE_SLACK = timedelta(days=3)
# Nothing can have been published this far in the future.
FUTURE_SLACK = timedelta(days=1)
# A show whose premiere is further out than this is not matched against releases yet.
PREMIERE_LEAD_TIME = timedelta(days=7)
# The usual gap between episodes, used to guess when the previous one aired.
WEEK = timedelta(days=7)

# Statuses of an episode that already has, or is getting, a release.
RELEASE_HOLDING = (EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADING)


@dataclass(frozen=True)
class EpisodeFacts:
    number: int
    air_at: Optional[datetime]
    status: EpisodeStatus
    retry_after: Optional[datetime] = None


@dataclass(frozen=True)
class ShowFacts:
    id: int
    name: str
    paused: bool
    completed: bool
    # Nothing downloaded yet and no feed proven: AniList's air time is still unverified.
    in_premiere: bool
    # A feed has proven it carries the show.
    working: bool
    anilist_status: Optional[str]
    next_airing_episode: Optional[int]
    next_airing_at: Optional[datetime]
    episodes: Tuple[EpisodeFacts, ...] = ()

    @property
    def active(self) -> bool:
        return not (self.paused or self.completed)

    @property
    def has_download(self) -> bool:
        return any(episode.status in RELEASE_HOLDING for episode in self.episodes)


def air_horizon(show: ShowFacts, now: datetime, tolerance_hours: int) -> datetime:
    """When an episode counts as aired for this show.

    The early-air tolerance only covers a show's first episodes, because after the
    first release AniList's air times are taken as stated.
    """
    if show.in_premiere:
        return now + timedelta(hours=max(0, tolerance_hours))
    return now


def latest_aired(show: ShowFacts, now: datetime, horizon: datetime) -> Optional[int]:
    """The number of the newest episode that has aired, or None when none has.

    Stored air times decide. Without any, AniList's pointer does: its next episode
    has aired once its air time has passed, otherwise the one before it has. A show
    that finished airing with no air times at all (an old show AniList has no schedule
    for) has aired every episode.
    """
    known = [e.number for e in show.episodes if e.air_at is not None and e.air_at <= horizon]
    if known:
        return max(known)
    if show.next_airing_episode and show.next_airing_at is not None:
        if show.next_airing_at <= horizon:
            return show.next_airing_episode
        return max(0, show.next_airing_episode - 1) or None
    if show.anilist_status == "FINISHED" and show.episodes and all(e.air_at is None for e in show.episodes):
        return max(e.number for e in show.episodes)
    return None


def _air_time_of(show: ShowFacts, number: int) -> Optional[datetime]:
    for episode in show.episodes:
        if episode.number == number and episode.air_at is not None:
            return episode.air_at
    if show.next_airing_at is not None and show.next_airing_episode:
        if number == show.next_airing_episode:
            return show.next_airing_at
        if number == show.next_airing_episode - 1:
            # The previous episode's own time is unknown; a week before the next is the usual guess.
            return show.next_airing_at - WEEK
    return None


def _still_wanted(show: ShowFacts, number: int) -> bool:
    for episode in show.episodes:
        if episode.number == number:
            return episode.status == EpisodeStatus.WANTED
    return not any(
        episode.number >= number and episode.status == EpisodeStatus.COMPLETED
        for episode in show.episodes
    )


def past_hunting_window(air_at: Optional[datetime], now: datetime) -> bool:
    """Whether an aired episode is past the day it is hunted for."""
    return air_at is not None and now - air_at > HUNT_WINDOW


def is_hunting(show: ShowFacts, now: datetime, tolerance_hours: int) -> bool:
    """Whether the show's newest aired episode is still wanted and aired within the hunting window."""
    if not show.active:
        return False
    number = latest_aired(show, now, air_horizon(show, now, tolerance_hours))
    if not number:
        return False
    if not _still_wanted(show, number):
        return False
    air_at = _air_time_of(show, number)
    if air_at is None:
        return False
    # A guessed time may lie ahead of now (a hiatus pushed the next episode out): that is not "just aired".
    if air_at > now + timedelta(hours=max(0, tolerance_hours)):
        return False
    return not past_hunting_window(air_at, now)


def next_hunt_start(show: ShowFacts, now: datetime, tolerance_hours: int) -> Optional[datetime]:
    """The next moment the show will start being hunted, or None when nothing is scheduled."""
    if not show.active:
        return None
    lead = timedelta(hours=max(0, tolerance_hours)) if show.in_premiere else timedelta(0)
    candidates = [e.air_at - lead for e in show.episodes if e.air_at is not None and e.air_at - lead > now]
    if not candidates and show.next_airing_at is not None and show.next_airing_at - lead > now:
        candidates.append(show.next_airing_at - lead)
    return min(candidates, default=None)


@dataclass(frozen=True)
class HuntPlan:
    hunting: Tuple[int, ...]
    next_start: Optional[datetime]


def hunt_plan(shows: Iterable[ShowFacts], now: datetime, tolerance_hours: int) -> HuntPlan:
    """The shows to hunt now and when the next one will need hunting."""
    hunting: List[int] = []
    starts: List[datetime] = []
    for show in shows:
        if is_hunting(show, now, tolerance_hours):
            hunting.append(show.id)
            continue
        start = next_hunt_start(show, now, tolerance_hours)
        if start is not None:
            starts.append(start)
    return HuntPlan(tuple(hunting), min(starts, default=None))


def is_unreleased(show: ShowFacts, now: datetime) -> bool:
    """True while a show has not started airing and nothing has been downloaded.

    A show AniList calls releasing or finished that has no air time at all (typically a movie)
    counts as released.
    """
    if show.next_airing_at is None and show.anilist_status in ("RELEASING", "FINISHED"):
        return False
    if show.has_download:
        return False
    return (
        (show.next_airing_episode in (1, None))
        and (show.next_airing_at is None or show.next_airing_at > now)
    )


def premiere_out_of_reach(show: ShowFacts, now: datetime) -> bool:
    """True while AniList says a show has not started airing and its premiere is unknown or far off.

    Nothing a feed carries can be this show's yet, only an earlier season under the same name,
    so such a show is not matched against releases.
    """
    if show.anilist_status != "NOT_YET_RELEASED" or not is_unreleased(show, now):
        return False
    return show.next_airing_at is None or show.next_airing_at > now + PREMIERE_LEAD_TIME


def waiting_for_release(show: ShowFacts, now: datetime) -> bool:
    """A show that aired, was hunted for a day, and is now left to the routine feed read.

    The newest stored episode that has aired decides, because AniList's next air time
    moves on to the following episode as soon as one airs. A finished show with no air
    time at all (a movie) was never hunted, so it waits from the start.
    """
    if not show.active or show.working:
        return False
    aired = [e for e in show.episodes if e.air_at is not None and e.air_at <= now]
    if aired:
        newest = max(aired, key=lambda e: e.number)
        return newest.status in (EpisodeStatus.WANTED, EpisodeStatus.MISSED) and past_hunting_window(newest.air_at, now)
    if show.next_airing_at is not None:
        return past_hunting_window(show.next_airing_at, now)
    return show.anilist_status == "FINISHED"


def missed_numbers(
    episodes: Sequence[EpisodeFacts],
    now: datetime,
    horizon: datetime,
    newest_aired: Optional[int],
    covered: Collection[int] = (),
) -> List[int]:
    """The WANTED episodes the feeds can no longer deliver.

    A wanted episode older than the newest aired one, or followed by a downloaded one,
    fell out of the feeds. The newest aired one is given a day to show up first. An
    episode a stored feed item can still supply (``covered``) is never missed. Missed
    episodes stay grabbable: they come back through a later feed read or a search.
    """
    result: List[int] = []
    for episode in episodes:
        if episode.status != EpisodeStatus.WANTED or episode.number in covered:
            continue
        if episode.retry_after is not None and episode.retry_after > now:
            continue
        if episode.air_at is not None:
            has_aired = episode.air_at <= horizon
        else:
            has_aired = newest_aired is not None and episode.number <= newest_aired
        if not has_aired:
            continue
        older_than_newest = newest_aired is not None and episode.number < newest_aired
        followed_by_download = any(
            other.number > episode.number and other.status in RELEASE_HOLDING for other in episodes
        )
        newest_and_overdue = (
            episode.air_at is not None
            and newest_aired is not None
            and episode.number == newest_aired
            and now - episode.air_at > MISSED_AFTER
        )
        if older_than_newest or followed_by_download or newest_and_overdue:
            result.append(episode.number)
    return result


def date_mapped_episode(
    air_times: Dict[int, datetime],
    published_at: datetime,
    target_count: int,
    tolerance_hours: int,
) -> Optional[int]:
    """The episode a release published at ``published_at`` can be, going by air times alone.

    It can only be an episode that has already aired (allowing the early-air tolerance
    for feeds that post before AniList's time) and aired within AIR_DATE_TOLERANCE. The
    one that aired closest before the release wins.
    """
    if published_at.year <= 2000:
        return None
    early = timedelta(hours=max(0, tolerance_hours))
    candidates = [
        (air_at, number)
        for number, air_at in air_times.items()
        if 1 <= number <= target_count
        and air_at <= published_at + early
        and published_at - air_at <= AIR_DATE_TOLERANCE
    ]
    if not candidates:
        return None
    return max(candidates)[1]


def published_before_air(
    published_at: Optional[datetime],
    reference: Optional[datetime],
    tolerance_hours: int,
) -> bool:
    """Whether a dated release was published before its episode could have aired.

    Season tags and episode numbers differ from group to group, so a release of an
    earlier season can carry a number that fits this one; its publish date cannot.
    ``reference`` is the episode's air time, or the show's first one when the episode
    has none. Undated releases and shows with nothing scheduled cannot be judged.
    """
    if reference is None or published_at is None or published_at.year <= 2000:
        return False
    slack = max(PUBLISHED_BEFORE_AIR_SLACK, timedelta(hours=max(0, tolerance_hours)))
    return published_at < reference - slack


def plausible_for_show(published_at: Optional[datetime], first_air: Optional[datetime], now: datetime) -> bool:
    """Whether a searched release's publish date could belong to a show that premiered at ``first_air``.

    A search reaches into history, where the show's earlier seasons live and their releases can
    carry a season or episode number that fits. Undated releases are dropped, since a search
    cannot vouch for them. Without a premiere time only the future check applies.
    """
    if published_at is None or published_at.year <= 2000:
        return False
    if published_at > now + FUTURE_SLACK:
        return False
    return first_air is None or published_at >= first_air - PREMIERE_SLACK
