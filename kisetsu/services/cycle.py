"""The units of work the engine's jobs run on the engine thread: small functions over plain data in and out."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import delete
from sqlmodel import select

from kisetsu.db.schema import Event, Show
from kisetsu.services import adoption, downloads, feeds, grab, ingest, library, shows, tags
from kisetsu.services.context import Context, get_state, set_state

logger = logging.getLogger("kisetsu.services.cycle")

LAST_SYNC_KEY = "last_anilist_sync_at"
# Version of the colour extraction; a stored result from another version is recomputed.
ACCENT_VERSION = 2
EVENTS_KEEP = timedelta(days=90)


def _say(lines: List[str]) -> None:
    """What the services report goes to the log, where the Logs page shows it."""
    for line in lines:
        logger.info(line)


@dataclass
class SyncInput:
    username: str
    sync_minutes: int
    hunt_minutes: int
    monitored_ids: Set[int]
    last_sync_ago: Optional[float]


@dataclass
class SyncApplied:
    added_ids: List[int] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)


@dataclass
class SchedulePlan:
    due_ids: List[int] = field(default_factory=list)
    windowed: List[int] = field(default_factory=list)
    full: List[int] = field(default_factory=list)


@dataclass
class CycleResult:
    started: int = 0
    missed: int = 0
    adopted: int = 0
    errors: List[str] = field(default_factory=list)
    next_candidate_in: Optional[float] = None


def sync_input(ctx: Context) -> SyncInput:
    s = ctx.settings
    last = get_state(ctx, LAST_SYNC_KEY)
    ago = None
    if last:
        ago = (ctx.now - datetime.fromisoformat(last)).total_seconds()
    ids = set(ctx.session.exec(select(Show.anilist_id)).all())
    return SyncInput(s.anilist_username.strip(), s.anilist_sync_minutes, s.hunt_interval_minutes, ids, ago)


def apply_sync(ctx: Context, seasonal: List[Dict[str, Any]]) -> SyncApplied:
    """Take AniList's list in: update, add and remove shows, then settle what the new air times imply."""
    existing = {s.anilist_id: s for s in ctx.session.exec(select(Show)).all()}
    applied = shows.apply_anilist(ctx, seasonal, existing)
    logs = list(applied.logs)
    logs.extend(shows.remove_off_list(ctx, seasonal, existing))
    logs.extend(shows.rollover(ctx))
    _say(logs)
    set_state(ctx, LAST_SYNC_KEY, ctx.now.isoformat())
    ingest.ensure_matches_current(ctx)
    tags.sync_tags(ctx)
    ctx.emit("shows", "status")
    return SyncApplied([s.id for s in applied.added], logs)


def schedule_plan(ctx: Context) -> SchedulePlan:
    due = shows.schedules_due(ctx)
    plan = SchedulePlan()
    for show in due:
        plan.due_ids.append(show.anilist_id)
        (plan.windowed if shows.schedule_is_dated(ctx, show) else plan.full).append(show.anilist_id)
    return plan


def uncovered(ctx: Context, anilist_ids: List[int], covering: Dict[int, List[Dict[str, Any]]]) -> List[int]:
    """Those shows whose windowed fetch did not date every aired episode, so they need the full schedule."""
    result = []
    for anilist_id in anilist_ids:
        show = ctx.session.exec(select(Show).where(Show.anilist_id == anilist_id)).first()
        if show is not None and not shows.covers_undated(ctx, show, covering.get(anilist_id)):
            result.append(anilist_id)
    return result


def apply_schedule_results(ctx: Context, anilist_ids: List[int], schedules: Dict[int, Any]) -> Tuple[int, List[str]]:
    rows = [s for s in ctx.session.exec(select(Show).where(Show.anilist_id.in_(anilist_ids))).all()] if anilist_ids else []
    done = [s for s in rows if s.anilist_id in schedules]
    count, lines = shows.apply_schedules(ctx, done, schedules)
    _say(lines)
    return count, lines


def rollover(ctx: Context) -> List[str]:
    lines = shows.rollover(ctx)
    _say(lines)
    return lines


def grab_cycle(ctx: Context) -> CycleResult:
    """Adopt what qBittorrent already has, then start the downloads the stored items call for."""
    result = CycleResult()
    ingest.ensure_matches_current(ctx)
    adopted = adoption.adopt_existing(ctx)
    result.adopted = adopted.imported
    adoption.reattribute(ctx)
    ctx.checkpoint()
    outcome = grab.grab_pass(ctx)
    result.started, result.missed, result.errors = outcome.started, outcome.missed, outcome.errors
    due = feeds.next_candidate_due(ctx)
    if due is not None:
        result.next_candidate_in = max(1.0, (due - ctx.now).total_seconds())
    return result


def settle_cycle(ctx: Context) -> downloads.Settled:
    """Follow the downloads; a finished episode may complete its show, so air times are settled afterwards."""
    result = downloads.settle(ctx)
    if result.changed:
        _say(shows.rollover(ctx))
    return result


def lock_saved_files(ctx: Context) -> Tuple[int, List[int]]:
    """Lock shows to the feed their saved files came from; returns how many and the shows still unmatched.

    Unmatched shows are held, so the top feed is not grabbed from and locked before a search has run.
    """
    locked = adoption.lock_from_saved_files(ctx)
    unmatched = adoption.unmatched_saved_files(ctx)
    for show in unmatched:
        library.hold(ctx, show.id)
    return len(locked), [s.id for s in unmatched]


def release_hold(ctx: Context, show_ids: List[int]) -> None:
    for show_id in show_ids:
        library.release(ctx, show_id)


def housekeeping(ctx: Context) -> Dict[str, int]:
    pruned = shows.prune_past_seasons(ctx)
    _say(pruned)
    removed = {
        "items": ingest.prune(ctx),
        "tags": downloads.sweep_tags(ctx) if ctx.qbit is not None else 0,
        "shows": len(pruned),
    }
    tags.sync_tags(ctx)
    old = ctx.session.execute(delete(Event).where(Event.at < ctx.now - EVENTS_KEEP))
    removed["events"] = old.rowcount or 0
    return removed


def palette_targets(ctx: Context) -> List[Tuple[int, str, str, str]]:
    """Shows whose colours are not computed for their current banner: (id, key, banner, cover)."""
    targets = []
    for show in ctx.session.exec(select(Show)).all():
        source = show.banner_image or show.cover_image or ""
        key = f"{source}#{ACCENT_VERSION}" if source else ""
        if key and not (show.accent_src == key and show.accent_hues is not None):
            targets.append((show.id, key, show.banner_image or "", show.cover_image or ""))
    return targets


def store_palette(ctx: Context, show_id: int, key: str, hues: Optional[Tuple[int, float, int, int, float]]) -> None:
    show = ctx.session.get(Show, show_id)
    if show is None:
        return
    show.accent_src = key
    show.accent_hues = "" if hues is None else f"{hues[0]},{hues[1]:.3f},{hues[2]},{hues[3]},{hues[4]:.3f}"
    ctx.session.add(show)
    ctx.emit("shows", f"show:{show_id}")
