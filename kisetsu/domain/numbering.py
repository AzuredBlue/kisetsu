"""How a feed's episode numbers map onto the show's AniList numbers.

Some feeds count from a different base (absolute numbers, or an earlier cour included).
The mapping is a per-show, per-feed offset: ``local = raw - offset``.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from kisetsu.domain.schedule import PUBLISHED_BEFORE_AIR_SLACK, date_mapped_episode

# A season that ended this long ago cannot take a new item as its latest episode.
SEASON_ENDED_AFTER = timedelta(days=14)
_UNDATED = datetime(1970, 1, 1, tzinfo=timezone.utc)


class Mapped(NamedTuple):
    local: Optional[int]
    # The air dates alone decided it. The caller may learn an offset from it when it
    # contradicts a stored release.
    by_date: bool = False


def map_episode(
    raw: int,
    *,
    offset: Optional[int],
    latest_aired: Optional[int],
    target_count: int,
    air_times: Dict[int, datetime],
    published_at: Optional[datetime],
    tolerance_hours: int,
    now: datetime,
    newest_raw: Optional[int] = None,
) -> Mapped:
    """Map a feed's raw episode number to the show's local one, or None when it is ambiguous.

    ``air_times`` are the stored air times by local number. ``newest_raw`` is the highest
    raw number among the releases being mapped together; it lets absolute numbers
    ("45", "46", "47", "48") be told apart when nothing else can.
    """
    if offset is not None:
        local = raw - offset
        if not 1 <= local <= target_count:
            return Mapped(None)
        if latest_aired is not None and local > latest_aired:
            return Mapped(None)
        return Mapped(local)

    published = published_at or _UNDATED
    dated = published.year > 2000

    if 1 <= raw <= target_count and latest_aired is not None and raw <= latest_aired + 1:
        # A number that fits the aired part of the season is taken as it is unless the
        # release is dated well before that episode aired (a feed numbering one higher
        # than AniList). Guessing from the date alone would shift releases a feed puts
        # out hours before AniList's air time ("03" posted before ep 3 airs is not ep 2).
        raw_air_at = air_times.get(raw)
        if raw_air_at is None:
            if raw <= latest_aired:
                return Mapped(raw)
        elif not dated or raw_air_at - published <= PUBLISHED_BEFORE_AIR_SLACK:
            return Mapped(raw)

    by_date = date_mapped_episode(air_times, published, target_count, tolerance_hours)
    if by_date is not None:
        return Mapped(by_date, True)

    if latest_aired is not None and raw > target_count and 1 <= latest_aired <= target_count:
        # "The newest release is the latest aired episode" only holds while the show is
        # current; for a season that ended long ago it would pin any new item to its end.
        latest_air_at = air_times.get(latest_aired)
        if latest_air_at is not None and now - latest_air_at > SEASON_ENDED_AFTER:
            return Mapped(None)
        if newest_raw is None:
            return Mapped(latest_aired)
        local = raw - (newest_raw - latest_aired)
        return Mapped(local if 1 <= local <= latest_aired else None)

    if 1 <= raw <= target_count and (latest_aired is None or raw <= latest_aired + 1):
        return Mapped(raw)
    return Mapped(None)


class LedgerRow(NamedTuple):
    """What the numbering logic needs to know about a stored episode."""

    number: int
    holds_release: bool
    source_episode: Optional[int]
    # The release came from the feed being rebased, or from a feed that is not recorded.
    from_this_feed: bool
    has_torrent: bool = False


def inferred_offset(
    raw: int,
    local: int,
    holder: Optional[LedgerRow],
    held_sources: Sequence[int],
) -> Optional[int]:
    """The offset a release teaches when its air date contradicts the ledger.

    The air date says the release is episode ``local``, but that row already holds a
    release the feed numbered differently. When the feed is consistently higher (it counts
    an earlier cour or a split entry, as AniList lists them), the offset is the difference.
    """
    if holder is None or not holder.holds_release:
        return None
    if holder.source_episode is None or holder.source_episode == raw or not holder.from_this_feed:
        return None
    offset = raw - local
    if offset <= 0 or raw <= max(held_sources, default=0):
        return None
    return offset


def rebase_plan(
    rows: Sequence[LedgerRow],
    offset: int,
    latest_aired: Optional[int],
    limit: Optional[int] = None,
) -> Optional[List[Tuple[int, int]]]:
    """The moves that renumber stored releases so ``source_episode - offset`` is their row.

    Rows written before a feed's numbering was known (adopted torrents) sit under the raw
    number the feed used. Once the real offset is known they must move, or the true episode
    would look already downloaded. Returns ``(from_number, to_number)`` pairs, or None when
    nothing changes or a move is unsafe: each target must exist, be an episode that has
    aired, and be free or itself moving.
    """
    by_number = {row.number: row for row in rows}
    movers = [
        row for row in rows
        if row.source_episode is not None and row.holds_release and row.from_this_feed
    ]
    if not movers:
        return None
    ceiling = limit or max(by_number, default=0)
    moves: List[Tuple[int, int]] = []
    for row in movers:
        target = row.source_episode - offset
        if not 1 <= target <= ceiling:
            return None
        if latest_aired is not None and target > latest_aired:
            return None
        moves.append((row.number, target))
    moving = {row.number for row in movers}
    for _, target in moves:
        occupant = by_number.get(target)
        if occupant is None:
            return None
        if occupant.number not in moving and (occupant.holds_release or occupant.has_torrent):
            return None
    if all(source == target for source, target in moves):
        return None
    return moves
