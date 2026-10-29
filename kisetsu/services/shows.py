"""The shows Kisetsu follows: taking AniList's list and schedules in, completion, pausing, editing and deleting.

A show is never deleted on its own: it stays until the user deletes it.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.config import HUNTING_RECENT_DAYS
from kisetsu.db.schema import (
    DismissedShow,
    Episode,
    EpisodeOffset,
    EpisodeStatus,
    Feed,
    FeedState,
    OffsetSource,
    PausedBy,
    ScheduleState,
    Show,
)
from kisetsu.domain.patterns import resolve_save_path
from kisetsu.services import downloads, feeds, ingest, legacy, library, tags
from kisetsu.services.context import Context, get_state, set_state

logger = logging.getLogger("kisetsu.services.shows")

# Per-episode air times are refreshed this often.
SCHEDULE_MAX_AGE = timedelta(hours=6)
# Shows whose past episodes are all dated only need what is still to come; this far back is re-read.
SCHEDULE_REFRESH_LOOKBACK = timedelta(days=3)
# A first fetch reaches back far enough to date every episode of a current-season show.
SCHEDULE_FULL_LOOKBACK = timedelta(days=180)
# A finished show keeps its season open this long after the finale aired if it was missed.
FINALE_GRACE = timedelta(days=HUNTING_RECENT_DAYS)
ALL_PAUSED_KEY = "all_paused"


@dataclass
class ApplyResult:
    added: List[Show] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)


@dataclass
class EditResult:
    message: str
    canceled: List[int] = field(default_factory=list)


def all_paused(ctx: Context) -> bool:
    return get_state(ctx, ALL_PAUSED_KEY) == "1"


def ensure_episodes(ctx: Context, show: Show) -> List[Episode]:
    """The show's episode rows, creating the ones from 1 up to the known season length."""
    rows = list(ctx.session.exec(select(Episode).where(Episode.show_id == show.id)).all())
    by_number = {row.number: row for row in rows}
    target = show.total_episodes or max(show.next_airing_episode or 0, max(by_number, default=0), 1)
    if show.total_episodes:
        target = min(target, show.total_episodes)
    for number in range(1, target + 1):
        if number not in by_number:
            row = Episode(show_id=show.id, number=number)
            ctx.session.add(row)
            by_number[number] = row
            rows.append(row)
    return rows


def final_episode_accounted(ctx: Context, show: Show) -> bool:
    """Whether every episode of a finished season is accounted for.

    Every episode up to the season length must be completed or missed (no longer
    retrievable), and at least one completed. A finale that was only just given up on keeps
    the season open for as long as the grabber keeps looking for it.
    """
    if not show.total_episodes or show.total_episodes <= 0 or show.id is None:
        return False
    if show.next_airing_episode and show.next_airing_episode < show.total_episodes:
        return False
    if show.next_airing_episode == show.total_episodes and show.next_airing_at and show.next_airing_at > ctx.now:
        return False
    episodes = ctx.session.exec(
        select(Episode).where(Episode.show_id == show.id, Episode.number <= show.total_episodes)
    ).all()
    if not episodes:
        return False

    def accounted(episode: Episode) -> bool:
        if episode.status == EpisodeStatus.COMPLETED:
            return True
        if episode.status != EpisodeStatus.MISSED:
            return False
        recent_finale = (
            episode.number == show.total_episodes
            and episode.air_at is not None
            and ctx.now - episode.air_at <= FINALE_GRACE
        )
        return not recent_finale

    return all(accounted(e) for e in episodes) and any(e.status == EpisodeStatus.COMPLETED for e in episodes)


def _set_if(show: Show, attr: str, value: Any) -> bool:
    if getattr(show, attr) != value:
        setattr(show, attr, value)
        return True
    return False


def _update_existing(ctx: Context, show: Show, data: Dict[str, Any], logs: List[str]) -> bool:
    updated = False
    for attr, key in (
        ("title_romaji", "title_romaji"), ("title_english", "title_english"), ("cover_image", "cover_image"),
        ("banner_image", "banner_image"), ("season_name", "season"), ("season_year", "season_year"),
    ):
        if data.get(key):
            updated |= _set_if(show, attr, data[key])
    updated |= _set_if(show, "total_episodes", data.get("total_episodes"))
    updated |= _set_if(show, "anilist_status", data.get("status") or None)
    updated |= _set_if(show, "list_status", data.get("list_status") or None)

    incoming = {a.strip() for a in data.get("aliases", []) if a and a.strip()}
    if not incoming.issubset(set(show.aliases)):
        show.aliases = list(set(show.aliases) | incoming)
        updated = True

    name = show.title(ctx.settings.title_language)
    # The user's own list status never completes a show: a show finished watching keeps downloading what it lacks.
    finished = data.get("status") == "FINISHED" and data.get("next_airing_episode") is None
    if finished:
        if final_episode_accounted(ctx, show):
            updated |= _set_if(show, "next_airing_episode", None)
            updated |= _set_if(show, "next_airing_at", None)
            if not show.completed:
                show.completed = True
                updated = True
                logs.append(f"'{name}' finished ({show.total_episodes}/{show.total_episodes} episodes).")
        else:
            final_ep = show.total_episodes or show.next_airing_episode
            if final_ep:
                updated |= _set_if(show, "next_airing_episode", final_ep)
    else:
        updated |= _set_if(show, "next_airing_episode", data.get("next_airing_episode"))
        updated |= _set_if(show, "next_airing_at", data.get("next_airing_at"))

    # A show completed on download evidence is reopened if that evidence was wrong.
    if show.completed and not final_episode_accounted(ctx, show):
        show.completed = False
        show.schedule_synced_at = None
        updated = True
    return updated


def apply_anilist(ctx: Context, seasonal_list: List[Dict[str, Any]], existing: Optional[Dict[int, Show]] = None) -> ApplyResult:
    """Update the followed shows from AniList's list and add the new ones."""
    result = ApplyResult()
    session = ctx.session
    if existing is None:
        existing = {s.anilist_id: s for s in session.exec(select(Show)).all()}
    # Shows the user deleted stay out until they leave the AniList list.
    dismissed = {row.anilist_id for row in session.exec(select(DismissedShow)).all()}
    listed = {data.get("anilist_id") or data.get("id") for data in seasonal_list}
    if listed:
        for gone in dismissed - listed:
            session.delete(session.get(DismissedShow, gone))
        dismissed &= listed
    paused_all = all_paused(ctx)
    for data in seasonal_list:
        anilist_id = data.get("anilist_id") or data.get("id")
        if not anilist_id or (anilist_id in dismissed and anilist_id not in existing):
            continue
        if anilist_id in existing:
            show = existing[anilist_id]
            if _update_existing(ctx, show, data, result.logs):
                session.add(show)
            session.flush()
            ensure_episodes(ctx, show)
            continue
        show = Show(
            anilist_id=anilist_id,
            title_romaji=data.get("title_romaji"),
            title_english=data.get("title_english"),
            total_episodes=data.get("total_episodes"),
            next_airing_episode=data.get("next_airing_episode"),
            next_airing_at=data.get("next_airing_at"),
            cover_image=data.get("cover_image"),
            banner_image=data.get("banner_image"),
            season_name=data.get("season"),
            season_year=data.get("season_year"),
            anilist_status=data.get("status") or None,
            list_status=data.get("list_status") or None,
            paused=paused_all,
            paused_by=PausedBy.ALL if paused_all else None,
        )
        show.aliases = list(data.get("aliases", []))
        session.add(show)
        session.flush()
        legacy.apply_pending(ctx, show)
        existing[anilist_id] = show
        ensure_episodes(ctx, show)
        result.added.append(show)
    if result.added:
        result.logs.append(f"Added {len(result.added)} new show(s) from AniList.")
        ctx.emit("shows")
    return result


def delete_show(
    ctx: Context,
    show: Show,
    *,
    reason: str,
    dismiss: bool = False,
    delete_torrents: bool = False,
    delete_files: bool = False,
) -> int:
    """Remove a show and its history; returns how many torrents were deleted from qBittorrent.

    A user's delete is remembered (``dismiss``) so the next sync does not add the show back
    while it is still on the AniList list.
    """
    hashes = library.find_show_torrents(ctx, show) if delete_torrents else []
    # Only a tag Kisetsu put on this show's torrents is its own to clear.
    show_tag = show.qbit_tag
    for episode in ctx.session.exec(select(Episode).where(Episode.show_id == show.id)).all():
        downloads.cancel_episode(ctx, episode, reason)
    if dismiss:
        ctx.session.merge(DismissedShow(anilist_id=show.anilist_id))
    ctx.session.flush()
    ctx.session.delete(show)
    ctx.session.flush()
    if hashes:
        ctx.qbit.delete(hashes, delete_files=delete_files)
    if show_tag and not delete_torrents:
        # The torrents stay, so the tag does too until none carries it; housekeeping clears it then.
        tags.retire(ctx, show_tag)
    elif show_tag:
        try:
            ctx.qbit.delete_tags([show_tag])
        except QbitError as e:
            logger.debug(f"Could not delete the tag '{show_tag}': {e}")
    ctx.caches.matchers = None
    ctx.emit("shows", "history")
    return len(hashes)


# -- schedules ----------------------------------------------------------------


def undated_aired_numbers(ctx: Context, show: Show) -> Set[int]:
    """Episode numbers that should already have aired but have no air time yet."""
    aired = (show.next_airing_episode - 1) if show.next_airing_episode else (show.total_episodes or 0)
    if aired <= 0:
        return set()
    dated = set(
        ctx.session.exec(
            select(Episode.number).where(Episode.show_id == show.id, Episode.number <= aired, Episode.air_at.is_not(None))
        ).all()
    )
    return set(range(1, aired + 1)) - dated


def schedule_is_dated(ctx: Context, show: Show) -> bool:
    return show.schedule_synced_at is not None and not undated_aired_numbers(ctx, show)


def covers_undated(ctx: Context, show: Show, entries: Optional[List[Dict[str, Any]]]) -> bool:
    """Whether a windowed fetch returned an air time for every aired episode that still lacked one."""
    if entries is None:
        return False
    returned = {int(entry["episode"]) for entry in entries if entry.get("airing_at")}
    return undated_aired_numbers(ctx, show) <= returned


def schedules_due(ctx: Context) -> List[Show]:
    """Shows whose air times are older than the refresh age, the longest ago first."""
    cutoff = ctx.now - SCHEDULE_MAX_AGE
    due = [
        s for s in ctx.session.exec(select(Show).where(Show.completed == False)).all()  # noqa: E712
        if s.schedule_synced_at is None or s.schedule_synced_at < cutoff
    ]
    due.sort(key=lambda s: s.schedule_synced_at or datetime.min.replace(tzinfo=ctx.now.tzinfo))
    return due


def apply_schedules(ctx: Context, due: Iterable[Show], schedules: Dict[int, Any]) -> Tuple[int, List[str]]:
    """Write fetched air dates onto the shows' episodes; returns the shows updated and the names with no schedule."""
    missing: List[str] = []
    updated = 0
    for show in due:
        entries = schedules.get(show.anilist_id)
        if entries is None:
            missing.append(show.title(ctx.settings.title_language))
            continue
        by_number = {row.number: row for row in ensure_episodes(ctx, show)}
        for entry in entries:
            episode = by_number.get(int(entry["episode"]))
            air_at = entry.get("airing_at")
            if episode is None or not air_at:
                continue
            episode.air_at = air_at
            episode.schedule_state = ScheduleState.AIRED if air_at <= ctx.now else ScheduleState.SCHEDULED
            ctx.session.add(episode)
        show.schedule_synced_at = ctx.now
        ctx.session.add(show)
        updated += 1
    if updated:
        ctx.emit("shows")
    return updated, missing


def rollover(ctx: Context) -> List[str]:
    """Deal with shows whose AniList air time has passed.

    The schedule is never invented: an overdue episode is recorded as aired against its
    real air time and left there, because guessing a weekly rollover would renumber a
    season against feeds that carry absolute numbering.
    """
    logs: List[str] = []
    for show in ctx.session.exec(select(Show).where(Show.completed == False)).all():  # noqa: E712
        if show.paused or not show.next_airing_at or show.next_airing_at > ctx.now:
            continue
        current = show.next_airing_episode or 1
        if show.total_episodes and current >= show.total_episodes:
            if final_episode_accounted(ctx, show):
                show.next_airing_episode = None
                show.next_airing_at = None
                show.completed = True
                ctx.session.add(show)
                logs.append(f"'{show.title(ctx.settings.title_language)}' finished ({show.total_episodes}/{show.total_episodes} episodes).")
            continue
        episode = ctx.session.exec(select(Episode).where(Episode.show_id == show.id, Episode.number == current)).first()
        if episode is None:
            episode = Episode(show_id=show.id, number=current, air_at=show.next_airing_at)
        if episode.schedule_state != ScheduleState.AIRED:
            episode.schedule_state = ScheduleState.AIRED
        ctx.session.add(episode)
    if logs:
        ctx.emit("shows")
    return logs


# -- pausing ------------------------------------------------------------------


def pause(ctx: Context, show: Show, by: PausedBy = PausedBy.USER) -> None:
    """Stop the show's grabs. Torrents already added keep going; only adds that qBittorrent has not shown yet are canceled."""
    show.paused, show.paused_by = True, by
    for episode in ctx.session.exec(select(Episode).where(Episode.show_id == show.id)).all():
        downloads.cancel_episode(ctx, episode, "Show paused by user.", keep_added=True)
    ctx.session.add(show)
    ctx.emit("shows", f"show:{show.id}")


def resume(ctx: Context, show: Show) -> None:
    show.paused, show.paused_by = False, None
    ctx.session.add(show)
    ctx.emit("shows", f"show:{show.id}")


def pause_all(ctx: Context) -> int:
    count = 0
    for show in ctx.session.exec(select(Show).where(Show.completed == False, Show.paused == False)).all():  # noqa: E712
        pause(ctx, show, PausedBy.ALL)
        count += 1
    set_state(ctx, ALL_PAUSED_KEY, "1")
    return count


def resume_all(ctx: Context) -> int:
    """Resume only the shows 'pause all' paused; shows the user paused by hand stay paused."""
    count = 0
    for show in ctx.session.exec(select(Show).where(Show.paused_by == PausedBy.ALL)).all():
        resume(ctx, show)
        count += 1
    set_state(ctx, ALL_PAUSED_KEY, "0")
    return count


def auto_discover_all(ctx: Context) -> int:
    """Return every hand-pinned show to automatic discovery; shows with a proven feed keep it."""
    count = 0
    for show in ctx.session.exec(select(Show).where(Show.feed_state == FeedState.PINNED)).all():
        feeds.release(ctx, show)
        count += 1
    return count


# -- editing ------------------------------------------------------------------


def set_manual_offset(ctx: Context, show: Show, feed: Optional[Feed], offset: int) -> None:
    """The user's own numbering offset for a feed; it survives a canceled grab."""
    if feed is None:
        raise ValueError("Pick a feed before setting its episode offset.")
    row = ctx.session.exec(
        select(EpisodeOffset).where(EpisodeOffset.show_id == show.id, EpisodeOffset.feed_id == feed.id)
    ).first()
    if row is None:
        row = EpisodeOffset(show_id=show.id, feed_id=feed.id)
    row.offset, row.source, row.last_evidence_at = offset, OffsetSource.MANUAL, ctx.now
    ctx.session.add(row)


def edit_show(
    ctx: Context,
    show: Show,
    *,
    save_folder: Optional[str] = None,
    aliases: Optional[List[str]] = None,
    category: Optional[str] = None,
    seed_ratio: Optional[float] = None,
    feed_id: Optional[int] = None,
    release_lock: bool = False,
    episode_offset: Optional[int] = None,
) -> EditResult:
    """Apply the user's changes to a show.

    ``feed_id`` None leaves the feed alone, a value of 0 or less returns the show to
    Auto-Discover and any other value pins that feed. Moving off a proven feed needs
    ``release_lock``; what is still downloading from the old feed is then canceled.
    """
    session = ctx.session
    feed: Optional[Feed] = None
    if feed_id is not None and feed_id > 0:
        feed = session.get(Feed, feed_id)
        if feed is None:
            raise ValueError("Selected feed not found")
    if feed_id is not None and show.feed_state == FeedState.LOCKED and not release_lock and (feed is None or show.feed_id != feed.id):
        raise feeds.FeedLockedError(f"'{show.title()}' is locked to another feed")
    moves: Optional[List[Dict[str, Any]]] = None
    moves_unreachable = False
    if save_folder is not None:
        value = save_folder.strip()
        folder = "" if not value or value == "{name}" else value
        if folder != show.save_folder:
            settings = ctx.settings
            try:
                moves = library.plan_move(
                    ctx, [show], lambda s: resolve_save_path(settings.base_dir, s.title(settings.title_language), folder),
                    anywhere=True,
                )
            except QbitError:
                moves_unreachable = True
            if moves and sum(item["moving"] for item in moves):
                raise ValueError("Some of this show's downloads are still moving to their folder. Try again in a moment.")
        show.save_folder = folder
    if aliases is not None:
        # Only the hand-written set is replaced; AniList re-supplies its own on every sync.
        show.custom_aliases = [a.strip() for a in aliases if a and a.strip()]
        ctx.caches.matchers = None
    if category is not None:
        show.category = category.strip() or None
    if seed_ratio is not None:
        show.seed_ratio = seed_ratio if seed_ratio >= 0 else None
    result = EditResult("Saved.")
    if feed_id is not None:
        if feed is None:
            feeds.release(ctx, show)
            result.message = f"'{show.title(ctx.settings.title_language)}' set to Auto-Discover."
        else:
            feeds.pin(ctx, show, feed, release_lock=release_lock)
            result.canceled = downloads.cancel_from_other_feeds(ctx, show, feed.id, "Canceled: the show's feed was changed.")
            result.message = f"Pinned to '{feed.name}'."
            if result.canceled:
                numbers = ", ".join(str(n) for n in result.canceled)
                result.message += f" Canceled {len(result.canceled)} unfinished download(s) from the old feed (Ep {numbers}); they will be fetched from the new one."
    if episode_offset is not None:
        target = feed or (session.get(Feed, show.feed_id) if show.feed_id else None)
        set_manual_offset(ctx, show, target, int(episode_offset))
    session.add(show)
    session.flush()
    if aliases is not None:
        ingest.rematch(ctx, [show.id])
    ctx.emit("shows", f"show:{show.id}")
    if moves:
        ctx.checkpoint()
        logs, _ = library.relocate_torrents(ctx, moves)
        result.message += " " + " ".join(logs)
    elif moves_unreachable:
        result.message += " qBittorrent could not be reached, so the downloads already made stayed in the old folder."
    return result
