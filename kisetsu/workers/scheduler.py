import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from sqlmodel import Session, select
from kisetsu.clients.qbit import QBitClient
from kisetsu.config import (
    DEFAULT_BACKFILL_WINDOW_DAYS,
    DEFAULT_DOWNLOAD_MODE,
    DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
)
from kisetsu.db.models import (
    ACTIVE_OPERATION_STATUSES,
    Episode,
    EpisodeStatus,
    Monitored,
    MonitoredStatus,
    TorrentOperation,
    as_utc,
    utc_now,
)

logger = logging.getLogger("kisetsu.workers.scheduler")

MIN_RETRY_SLEEP_SECONDS = 30


@dataclass(frozen=True)
class PollClassification:
    """Which monitored shows make the next pass urgent."""

    has_shows: bool
    hunting: List[Monitored]
    unresolved_upcoming: List[Tuple[datetime, Monitored]]
    now: datetime


def earliest_scheduled_retry(session: Session, now: Optional[datetime] = None) -> Optional[int]:
    """Seconds until the next queued retry becomes due, if any."""
    now = now or utc_now()
    deadlines = [value for value in session.exec(
        select(Episode.retry_after).where(
            Episode.retry_after.is_not(None),
            Episode.retry_after > now,
        )
    ).all() if value]
    deadlines += [value for value in session.exec(
        select(TorrentOperation.next_retry_at).where(
            TorrentOperation.next_retry_at.is_not(None),
            TorrentOperation.next_retry_at > now,
        )
    ).all() if value]
    if not deadlines:
        return None
    soonest = min(as_utc(value) for value in deadlines)
    return max(0, int((soonest - now).total_seconds()))


def classify_shows(
    session: Session,
    *,
    download_mode: str = DEFAULT_DOWNLOAD_MODE,
    backfill_window_days: int = DEFAULT_BACKFILL_WINDOW_DAYS,
    early_air_tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> PollClassification:
    """Split active shows into hunting and not-yet-aired.

    In rules mode a ``FIXED`` show is ignored, because qBittorrent downloads it
    on its own. The direct engines own their downloads, so a ``FIXED`` show
    still has to be polled while its latest aired episode is outstanding.
    """
    now = utc_now()
    hunting_shows: List[Monitored] = []
    unresolved_upcoming: List[Tuple[datetime, Monitored]] = []

    mode = str(download_mode or DEFAULT_DOWNLOAD_MODE).strip().lower()
    is_direct = mode == "direct"

    shows = session.exec(select(Monitored)).all()
    if not shows:
        return PollClassification(
            has_shows=False, hunting=[], unresolved_upcoming=[], now=now
        )

    operation_shows: set = set()
    if is_direct:
        pending_episode_ids = set(session.exec(
            select(TorrentOperation.episode_id).where(
                TorrentOperation.status.in_(list(ACTIVE_OPERATION_STATUSES)),
            )
        ).all())
        if pending_episode_ids:
            pending_show_ids = set(session.exec(
                select(Episode.monitored_id).where(Episode.id.in_(pending_episode_ids))
            ).all())
            operation_shows = {s.id for s in shows if s.id in pending_show_ids}

    for s in shows:
        airing_at = as_utc(s.next_airing_at)
        suspended = s.status in (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED)

        if s.id in operation_shows and not suspended:
            hunting_shows.append(s)
            continue

        if suspended:
            continue

        if s.status == MonitoredStatus.FIXED:
            if not is_direct or s.id is None:
                continue
            scheduled = session.exec(
                select(Episode).where(
                    Episode.monitored_id == s.id,
                    Episode.air_at.is_not(None),
                )
            ).all()
            air_times = {episode.episode_number: as_utc(episode.air_at) for episode in scheduled}
            # Start hunting a little before the stated air time, so a release
            # that lands early is still picked up promptly.
            # The early-air window only covers a show's first episodes; after that
            # AniList's air times are taken as stated.
            horizon = now + timedelta(hours=max(0, early_air_tolerance_hours)) if s.in_premiere else now
            aired_numbers = [n for n, air in air_times.items() if air and air <= horizon]
            latest_aired_episode = max(aired_numbers, default=0)
            if latest_aired_episode == 0 and airing_at is not None and s.next_airing_episode is not None:
                latest_aired_episode = (
                    s.next_airing_episode
                    if airing_at <= now
                    else max(0, s.next_airing_episode - 1)
                )
            if latest_aired_episode >= 1:
                latest_air_at = air_times.get(latest_aired_episode)
                latest_is_recent = (
                    latest_air_at is None
                    or now - latest_air_at <= timedelta(days=max(0, backfill_window_days))
                )
                newest_wanted = session.exec(
                    select(Episode.id).where(
                        Episode.monitored_id == s.id,
                        Episode.status == EpisodeStatus.WANTED,
                        Episode.episode_number == latest_aired_episode,
                    ).limit(1)
                ).first()
                if newest_wanted is not None and latest_is_recent:
                    hunting_shows.append(s)
            future_airings = [air for air in air_times.values() if air and air > horizon]
            next_airing = min(future_airings, default=None)
            if next_airing is None and airing_at is not None and airing_at > now:
                next_airing = airing_at
            if next_airing is not None:
                unresolved_upcoming.append((next_airing, s))
            continue

        if airing_at is None:
            continue

        if s.current_feed_id is None or s.status == MonitoredStatus.STALLED:
            if airing_at <= now:
                hunting_shows.append(s)
            else:
                unresolved_upcoming.append((airing_at, s))
        elif s.status == MonitoredStatus.UNCONFIRMED:
            is_near = (airing_at <= now)
            if not is_near and (s.next_airing_episode or 1) > 1:
                previous_air = airing_at - timedelta(days=7)
                expected_ep = (s.next_airing_episode or 1) - 1
                # previous_air lies in the future when the next airing is more
                # than a week out (hiatus or delay); that is not "just aired".
                since_previous = now - previous_air
                if (
                    timedelta(0) <= since_previous <= timedelta(hours=24)
                    and (s.last_confirmed_episode or 0) < expected_ep
                ):
                    is_near = True

            if is_near:
                hunting_shows.append(s)
            else:
                unresolved_upcoming.append((airing_at, s))

    return PollClassification(
        has_shows=True,
        hunting=hunting_shows,
        unresolved_upcoming=unresolved_upcoming,
        now=now,
    )


def is_hunting(
    session: Session,
    *,
    download_mode: str = DEFAULT_DOWNLOAD_MODE,
    backfill_window_days: int = DEFAULT_BACKFILL_WINDOW_DAYS,
    early_air_tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> bool:
    """True when at least one show is waiting on a release we have not confirmed."""
    return bool(
        classify_shows(
            session,
            download_mode=download_mode,
            backfill_window_days=backfill_window_days,
            early_air_tolerance_hours=early_air_tolerance_hours,
        ).hunting
    )


def calculate_next_poll_interval(
    session: Session,
    default_interval_seconds: int = 21600,
    hunting_interval_seconds: Optional[int] = None,
    qbit_client: Optional[QBitClient] = None,
    download_mode: str = DEFAULT_DOWNLOAD_MODE,
    backfill_window_days: int = DEFAULT_BACKFILL_WINDOW_DAYS,
    early_air_tolerance_hours: int = DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
) -> Tuple[int, str]:
    """
    Dynamically calculate the optimal sleep duration until the next check:
    - Shows whose RSS rules already work (FIXED) are ignored in rules mode, because qBittorrent downloads them automatically. The direct engines keep polling them while an episode is outstanding.
    - Shows without release dates (next_airing_at is None) are ignored from hunting; they wait for AniList schedule.
    - If any upcoming/unconfirmed show has aired recently (next_airing_at <= now) -> Hunting mode (qBittorrent RSS refresh rate + 15s).
    - If the next upcoming unconfirmed show airs sooner than default interval -> Sleep until its TV air time to bind rule.
    - A queued retry always wins over the calculated sleep, because a pending operation must not wait for the next routine cycle.
    - Otherwise (all active shows have working rules or are waiting for air dates) -> Sleep default interval (e.g. 6 hours).
    """
    mode = str(download_mode or DEFAULT_DOWNLOAD_MODE).strip().lower()
    uses_direct_engine = mode == "direct"

    classification = classify_shows(
        session,
        download_mode=download_mode,
        backfill_window_days=backfill_window_days,
        early_air_tolerance_hours=early_air_tolerance_hours,
    )
    hunting_shows = classification.hunting
    unresolved_upcoming = classification.unresolved_upcoming
    now = classification.now

    if not classification.has_shows:
        return default_interval_seconds, "No monitored shows. Sleeping default interval."

    effective_hunting_interval = hunting_interval_seconds
    sleep_duration = default_interval_seconds
    reason: Optional[str] = None

    if hunting_shows:
        if effective_hunting_interval is None:
            if qbit_client:
                effective_hunting_interval = qbit_client.get_rss_refresh_interval_seconds()
            else:
                effective_hunting_interval = 315
        sleep_duration = effective_hunting_interval
        names = ", ".join(f"'{s.display_name}'" for s in hunting_shows[:3])
        if len(hunting_shows) > 3:
            names += f" and {len(hunting_shows) - 3} more"
        h_mins = effective_hunting_interval // 60
        h_secs = effective_hunting_interval % 60
        interval_str = f"{h_mins}m {h_secs}s" if h_secs else f"{h_mins}m"
        if uses_direct_engine:
            reason = f"Direct hunting: {len(hunting_shows)} show(s) waiting for a matching release ({names}). Checking every {interval_str}."
        else:
            reason = f"Hunting mode: {len(hunting_shows)} show(s) waiting for rule/release ({names}). Checking every {interval_str}."
    else:
        upcoming = sorted(unresolved_upcoming, key=lambda x: x[0])
        if upcoming:
            earliest_air, earliest_show = upcoming[0]
            seconds_until_air = int((earliest_air - now).total_seconds())
            if seconds_until_air < default_interval_seconds:
                sleep_duration = max(60, seconds_until_air)
                air_str = earliest_air.strftime("%d/%m %H:%M UTC")
                verb = "evaluate direct grabs" if uses_direct_engine else "create rule"
                reason = f"Upcoming premiere: '{earliest_show.display_name}' airs on {air_str} (in {sleep_duration // 60}m). Sleeping until air time to {verb}."
        if reason is None:
            reason = _idle_reason(
                mode=mode,
                default_interval_seconds=default_interval_seconds,
            )

    retry_in = earliest_scheduled_retry(session)
    if retry_in is not None and retry_in < sleep_duration:
        retry_sleep = max(MIN_RETRY_SLEEP_SECONDS, retry_in)
        return retry_sleep, f"{reason} Retrying a pending operation in {retry_sleep}s."
    return sleep_duration, reason


def _idle_reason(
    *,
    mode: str,
    default_interval_seconds: int,
) -> str:
    """Explain a routine-length sleep: nothing is due before the next pass."""
    minutes = default_interval_seconds // 60
    if mode == "direct":
        return f"All active shows have direct ownership or are waiting for air dates. Sleeping {minutes}m until next routine check."
    return f"All active shows have working rules or are waiting for air dates. Sleeping {minutes}m until next routine check."
