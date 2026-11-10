"""Deciding which stored feed item becomes which episode's download, and which episodes are missed."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.config import HUNTING_RECENT_DAYS
from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    Download,
    Episode,
    EpisodeOffset,
    EpisodeStatus,
    EventKind,
    Feed,
    FeedItem,
    FeedItemMatch,
    FeedState,
    OffsetSource,
    Show,
)
from kisetsu.domain import matcher as matching
from kisetsu.domain.numbering import LedgerRow, inferred_offset, map_episode, rebase_plan
from kisetsu.domain.parsing import episode_pair, is_batch, names_something_else, release_resolution, resolution_rank, season_conflict, strip_batch
from kisetsu.domain.schedule import (
    RELEASE_HOLDING,
    air_horizon,
    date_mapped_episode,
    latest_aired,
    missed_numbers,
    premiere_out_of_reach,
    published_before_air,
)
from kisetsu.services import downloads, events, feeds, ingest, library
from kisetsu.services.context import Context
from kisetsu.services.facts import facts_for
from kisetsu.services.feeds import Carrier
from kisetsu.services.ingest import followed_shows

logger = logging.getLogger("kisetsu.services.grab")

EPOCH_YEAR = 2000


@dataclass
class GrabResult:
    started: int = 0
    missed: int = 0
    # qBittorrent errors, which stop the pass.
    errors: List[str] = field(default_factory=list)
    # Shows whose pass broke on something else ("Name: error"); the others were still grabbed for.
    failed: List[str] = field(default_factory=list)


@dataclass
class Resolved:
    item: FeedItem
    raw: int
    number: int
    episode: Episode
    version: int
    # The item's torrent holds another episode too (a double episode), so it is added once and never replaced on its own.
    shared: bool = False


@dataclass
class ShowState:
    """What grabbing for one show needs: its episodes, schedule and the downloads they hold."""

    show: Show
    episodes: List[Episode]
    by_number: Dict[int, Episode]
    current: Dict[int, Optional[Download]]
    failed: Dict[int, Set[int]]
    target_count: int
    newest_aired: Optional[int]
    horizon: datetime
    air_times: Dict[int, datetime]
    first_air: Optional[datetime]

    def held_version(self, episode: Episode) -> int:
        cur = self.current.get(episode.id)
        return cur.version if cur is not None and episode.status in (*RELEASE_HOLDING, EpisodeStatus.FAILED) else 1


def show_state(ctx: Context, show: Show, facts) -> ShowState:
    episodes = list(ctx.session.exec(select(Episode).where(Episode.show_id == show.id).order_by(Episode.number)).all())
    ids = [e.id for e in episodes]
    current: Dict[int, Optional[Download]] = {}
    failed: Dict[int, Set[int]] = {}
    if ids:
        by_id = {d.id: d for d in ctx.session.exec(select(Download).where(Download.episode_id.in_(ids))).all()}
        for episode in episodes:
            current[episode.id] = by_id.get(episode.current_download_id)
        for dl in by_id.values():
            if downloads.is_final_failure(dl):
                failed.setdefault(dl.episode_id, set()).add(dl.version)
    canonical_max = max((e.number for e in episodes), default=0)
    target_count = show.total_episodes or max(show.next_airing_episode or 0, canonical_max, 1)
    horizon = air_horizon(facts, ctx.now, ctx.tolerance_hours)
    air_times = {e.number: e.air_at for e in episodes if e.air_at is not None}
    return ShowState(
        show=show,
        episodes=episodes,
        by_number={e.number: e for e in episodes},
        current=current,
        failed=failed,
        target_count=target_count,
        newest_aired=latest_aired(facts, ctx.now, horizon),
        horizon=horizon,
        air_times=air_times,
        first_air=min(air_times.values(), default=None),
    )


def _offset(ctx: Context, show: Show, feed: Feed) -> Optional[EpisodeOffset]:
    return ctx.session.exec(
        select(EpisodeOffset).where(EpisodeOffset.show_id == show.id, EpisodeOffset.feed_id == feed.id)
    ).first()


def _matcher_for(ctx: Context, state: ShowState, feed: Feed) -> matching.Matcher:
    show = state.show
    base = matching.build_matcher(show.effective_aliases, show.learned_name, ctx.settings.min_resolution)
    if show.feed_state != FeedState.LOCKED or show.feed_id != feed.id:
        return base
    held = [
        cur.title
        for episode in state.episodes
        if (cur := state.current.get(episode.id)) is not None
        and episode.status in RELEASE_HOLDING
        and cur.feed_id in (None, feed.id)
        # A batch names its season its own way ("S01"), which says nothing of how the feed names single episodes.
        and not downloads.is_batch_download(cur)
    ]
    return matching.narrow_to_feed(base, show.learned_name, held)


def _items(ctx: Context, show: Show, feed: Feed) -> List[FeedItem]:
    return list(
        ctx.session.exec(
            select(FeedItem)
            .join(FeedItemMatch, FeedItemMatch.item_id == FeedItem.id)
            .where(FeedItemMatch.show_id == show.id, FeedItem.feed_id == feed.id)
            .order_by(FeedItem.published_at.desc(), FeedItem.id.desc())
        ).all()
    )


def _undated(item: FeedItem) -> bool:
    return item.published_at is None or item.published_at.year <= EPOCH_YEAR


def resolve(
    ctx: Context, state: ShowState, feed: Feed, *, learn: bool = True, rejected: Optional[List[Tuple[FeedItem, str]]] = None,
    dated: Optional[List[FeedItem]] = None,
) -> Tuple[List[Resolved], Dict[int, int]]:
    """Which episode each stored item of a feed is for, and the newest usable version of each.

    The one place that decides whether an item is this show's release of one of its episodes:
    the grab acts on the result and the missed rule asks the same question with ``learn=False``,
    which never renumbers stored releases. ``rejected`` collects the items it turned down, each with why,
    and ``dated`` the items numbered by their air date alone, the only ones a feed's numbering is learned from.
    """
    show = state.show
    matcher = _matcher_for(ctx, state, feed)
    cache = ctx.caches.parsed_cache()
    items = _items(ctx, show, feed)
    newest_raw = matching.newest_raw_episode(matcher, [i.title for i in items], cache)
    offset_row = _offset(ctx, show, feed)
    resolved: List[Resolved] = []
    best: Dict[int, int] = {}

    def on_schedule(item: FeedItem):
        def check(raw: int) -> bool:
            if item.published_at is None:
                return False
            return date_mapped_episode(state.air_times, item.published_at, state.target_count, ctx.tolerance_hours) is not None

        return check

    names = [*show.effective_aliases, *([show.learned_name] if show.learned_name else [])]

    def turn_down(item: FeedItem, reason: str) -> None:
        if rejected is not None:
            rejected.append((item, reason))

    for item in items:
        matched, parsed = matching.decide_dated(matcher, item.title, cache, on_schedule(item))
        if not matched:
            turn_down(item, why_not_matched(ctx, matcher, item.title))
            continue
        # A movie, special or side story under the show's name ("… - Coleus no Yume - 01") is not one of its episodes,
        # nor is a release of another show whose name holds this one's.
        if names_something_else(item.title, names):
            turn_down(item, (
                "A movie, special, side story or another show whose name holds this one's, not one of the episodes. "
                "If it is this show, add its full name as a custom alias."
            ))
            continue
        raw = parsed.get("episode")
        if raw is None and state.target_count == 1:
            raw = 1
        if raw is None or raw <= 0:
            turn_down(item, "Its name has no episode number (a recap like \"12.5\", or a special).")
            continue
        mapped = map_episode(
            int(raw),
            offset=offset_row.offset if offset_row is not None else None,
            latest_aired=state.newest_aired,
            target_count=state.target_count,
            air_times=state.air_times,
            published_at=item.published_at,
            tolerance_hours=ctx.tolerance_hours,
            now=ctx.now,
            newest_raw=newest_raw,
        )
        number = mapped.local
        if number is None:
            turn_down(item, f"Its episode {raw} does not fit this season's aired episodes (yet).")
            continue
        if mapped.by_date and dated is not None:
            dated.append(item)
        if mapped.by_date and learn:
            _learn_offset(ctx, state, feed, int(raw), number)
        episode = state.by_number.get(number)
        if episode is None:
            turn_down(item, f"This season has no episode {number}.")
            continue
        if published_before_air(item.published_at, episode.air_at or state.first_air, ctx.tolerance_hours):
            turn_down(item, "Published before this episode aired, so it is an earlier season's release.")
            continue
        version = int(parsed.get("version") or 1)
        pair = episode_pair(item.title)
        second = state.by_number.get(number + 1) if pair is not None and pair[0] == int(raw) else None
        if second is not None and (
            (state.newest_aired is not None and second.number > state.newest_aired)
            or published_before_air(item.published_at, second.air_at or state.first_air, ctx.tolerance_hours)
        ):
            # The second half has not aired by AniList's times: only the first is taken from it.
            second = None
        entries = [Resolved(item, int(raw), number, episode, version, shared=pair is not None)]
        if second is not None:
            entries.append(Resolved(item, int(raw) + 1, second.number, second, version, shared=True))
        for entry in entries:
            resolved.append(entry)
            if version not in state.failed.get(entry.episode.id, set()):
                best[entry.number] = max(best.get(entry.number, 0), version)
    return resolved, best


def why_not_matched(ctx: Context, matcher: matching.Matcher, title: str) -> str:
    """Why a release carrying the show's name is still not taken as one of its episodes."""
    if is_batch(title):
        return "A batch (several episodes in one torrent). Download it by hand, or add the show again from Add show to take it."
    resolution = release_resolution(title)
    minimum = ctx.settings.min_resolution
    if resolution is not None and resolution < minimum:
        return f"{resolution}p is below your preferred quality ({minimum}p, in Settings), and lower ones are never taken."
    if season_conflict(title, list(matcher.aliases)):
        return "It names another season."
    if matcher.strict:
        return "Not named the way this feed names the show's releases."
    return "Its name does not match this show's names closely enough. A custom alias can fix that."


def unmatched_releases(ctx: Context, show: Show, feed: Feed, seen: Set[int], limit: int = 300) -> List[Tuple[FeedItem, str]]:
    """The feed's recent items that carry the show's name but were never matched to it: batches and lower resolutions."""
    relaxed = matching.build_matcher(show.effective_aliases, show.learned_name, 0)
    strict = matching.build_matcher(show.effective_aliases, show.learned_name, ctx.settings.min_resolution)
    cache = ctx.caches.parsed_cache()
    rows = ctx.session.exec(
        select(FeedItem).where(FeedItem.feed_id == feed.id).order_by(FeedItem.published_at.desc(), FeedItem.id.desc()).limit(limit)
    ).all()
    found: List[Tuple[FeedItem, str]] = []
    for item in rows:
        if item.id in seen:
            continue
        name = strip_batch(item.title) if is_batch(item.title) else item.title
        if matching.broad_match(relaxed, name, cache):
            found.append((item, why_not_matched(ctx, strict, item.title)))
    return found


def _ledger(state: ShowState, feed: Feed) -> List[LedgerRow]:
    rows = []
    for episode in state.episodes:
        cur = state.current.get(episode.id)
        holds = episode.status in RELEASE_HOLDING and cur is not None
        rows.append(
            LedgerRow(
                number=episode.number,
                holds_release=holds,
                source_episode=cur.source_episode if holds else None,
                from_this_feed=holds and cur.feed_id in (None, feed.id),
                has_torrent=holds and bool(cur.info_hash),
            )
        )
    return rows


def _learn_offset(ctx: Context, state: ShowState, feed: Feed, raw: int, local: int) -> None:
    """Learn a feed's numbering when a release's air date contradicts the stored releases, and renumber them."""
    rows = _ledger(state, feed)
    holder = next((r for r in rows if r.number == local), None)
    held_sources = [r.source_episode for r in rows if r.holds_release and r.source_episode is not None]
    offset = inferred_offset(raw, local, holder, held_sources)
    if offset is None:
        return
    moves = rebase_plan(rows, offset, state.newest_aired, state.target_count)
    if not moves:
        return
    touched = {n for pair in moves for n in pair}
    busy = any(
        ctx.session.exec(
            select(Download.id).where(Download.episode_id == state.by_number[n].id, Download.state.in_(ACTIVE_DOWNLOAD_STATES)).limit(1)
        ).first()
        for n in touched
        if n in state.by_number
    )
    if busy:
        return
    carried = {n: (state.current[state.by_number[n].id], _history(ctx, state.by_number[n])) for n, _ in moves}
    for number in touched:
        episode = state.by_number[number]
        episode.current_download_id = None
        ctx.session.add(episode)
    for source, target in moves:
        current_dl, history = carried[source]
        for dl in history:
            dl.episode_id = state.by_number[target].id
            ctx.session.add(dl)
        state.by_number[target].current_download_id = current_dl.id if current_dl is not None else None
    ctx.session.flush()
    for number in touched:
        episode = state.by_number[number]
        downloads.recompute_episode(ctx, episode)
        state.current[episode.id] = downloads.current(ctx, episode)
    events.record(
        ctx,
        EventKind.INFO,
        f"'{feed.name}' numbers episodes {offset} higher than AniList; renumbered the stored releases.",
        show=state.show,
        feed_id=feed.id,
    )


def _history(ctx: Context, episode: Episode) -> List[Download]:
    return list(ctx.session.exec(select(Download).where(Download.episode_id == episode.id)).all())


def record_evidence(ctx: Context, state: ShowState, feed: Feed, raw: int, local: int) -> None:
    """Keep or confirm a feed's numbering offset once a release was actually accepted."""
    show = state.show
    row = _offset(ctx, show, feed)
    if row is None:
        ctx.session.add(
            EpisodeOffset(
                show_id=show.id, feed_id=feed.id, offset=raw - local, source=OffsetSource.INFERRED,
                evidence_count=1, first_evidence_at=ctx.now, last_evidence_at=ctx.now,
            )
        )
        return
    if row.source != OffsetSource.INFERRED:
        return
    consistent = [
        e for e in state.episodes
        if (cur := state.current.get(e.id)) is not None
        and e.status in RELEASE_HOLDING
        and cur.feed_id == feed.id
        and cur.source_episode is not None
        and cur.source_episode - e.number == row.offset
    ]
    row.evidence_count = max(row.evidence_count, len(consistent) + 1)
    row.last_evidence_at = ctx.now
    if consistent:
        row.source = OffsetSource.CONFIRMED
    ctx.session.add(row)


def _candidate_feeds(ctx: Context, show: Show, all_feeds: List[Feed]) -> List[Feed]:
    if show.feed_state in (FeedState.LOCKED, FeedState.PINNED):
        # A show with a proven or chosen feed reads that one or none; fanning out is what the lock prevents.
        return [f for f in all_feeds if f.id == show.feed_id]
    return all_feeds


def _wants(episode: Episode) -> bool:
    return episode.status in (EpisodeStatus.WANTED, EpisodeStatus.MISSED)


def grab_pass(ctx: Context, only: Optional[Set[int]] = None, held: Optional[Set[int]] = None) -> GrabResult:
    """Start downloads for the stored feed items that are some followed show's next episodes.

    ``held`` are shows waiting for their saved files to be matched to a feed; they are not
    grabbed for until released.
    """
    result = GrabResult()
    ingest.ensure_matches_current(ctx)
    feeds.unwind_unreleased(ctx)
    all_feeds = feeds.ranked(ctx)
    if not all_feeds:
        return result
    shows = [
        s for s in followed_shows(ctx)
        if not s.paused and (only is None or s.id in only)
    ]
    if not shows:
        return result
    if held is None:
        held = library.held_ids(ctx)
    facts = facts_for(ctx, shows)
    # Every torrent the pass adds is looked for with one qBittorrent sync at the end, not one sync per add.
    with downloads.batched_adds(ctx):
        for show in shows:
            if show.feed_state != FeedState.LOCKED and held and show.id in held:
                continue
            name = show.title(ctx.settings.title_language)
            try:
                _grab_show(ctx, show, facts[show.id], all_feeds, result)
                ctx.checkpoint()
            except QbitError as e:
                result.errors.append(f"{name}: {e}")
                break
            except Exception as e:
                # One show's bad data must not stop every other show from being grabbed for.
                ctx.session.rollback()
                logger.warning(f"Grabbing for '{name}' failed: {e}", exc_info=True)
                result.failed.append(f"{name}: {e}")
    if result.started or result.missed:
        ctx.emit("episodes", "history")
    return result


def _grab_show(ctx: Context, show: Show, facts, all_feeds: List[Feed], result: GrabResult) -> None:
    state = show_state(ctx, show, facts)
    if premiere_out_of_reach(facts, ctx.now) or (
        show.next_airing_episode == 1 and show.next_airing_at is not None and show.next_airing_at > state.horizon
    ):
        return
    candidates = _candidate_feeds(ctx, show, all_feeds)
    # Choosing a feed reads every candidate without learning from it; only the feed taken is read again with learning,
    # and only when it has something to learn from.
    dated: Dict[int, List[FeedItem]] = {f.id: [] for f in candidates}
    resolved_by_feed = {f.id: resolve(ctx, state, f, learn=False, dated=dated[f.id]) for f in candidates}
    chosen = _choose_feed(ctx, state, candidates, resolved_by_feed)
    grabbed_from: Optional[int] = None
    if chosen is not None:
        if dated[chosen.id]:
            resolved_by_feed[chosen.id] = resolve(ctx, state, chosen)
        resolved, best = resolved_by_feed[chosen.id]
        # Of an episode's releases the preferred quality goes first, then the closest above it; the first taken wins.
        preferred = ctx.settings.min_resolution
        # The torrents this pass added that hold a second episode, by item: that episode follows the same torrent.
        added: Dict[int, str] = {}
        for entry in sorted(resolved, key=lambda r: resolution_rank(r.item.title, preferred)):
            if _take(ctx, state, chosen, entry, best, added):
                grabbed_from = chosen.id
                result.started += 1
    if any(f.last_ok_at is not None and f.failing_since is None for f in candidates):
        result.missed += _mark_missed(ctx, state, facts, resolved_by_feed)
    if grabbed_from is not None and show.feed_state in (FeedState.NONE, FeedState.CANDIDATE):
        feeds.set_assigned(ctx, show, chosen)


def _choose_feed(ctx: Context, state: ShowState, candidates: List[Feed], resolved_by_feed) -> Optional[Feed]:
    """The feed to grab from now: the locked, pinned or assigned one, else the discovery winner."""
    show = state.show
    if show.feed_state in (FeedState.LOCKED, FeedState.PINNED):
        return candidates[0] if candidates else None
    carriers: List[Carrier] = []
    for feed in candidates:
        resolved, best = resolved_by_feed[feed.id]
        carried = [
            r for r in resolved
            # A failed episode is still served by a version newer than the one that failed.
            if (_wants(r.episode) or (r.episode.status == EpisodeStatus.FAILED and r.version > state.held_version(r.episode)))
            and r.version >= best.get(r.number, r.version)
        ]
        if carried:
            dates = [r.item.published_at for r in carried if not _undated(r.item)]
            carriers.append(Carrier(feed, min(dates) if dates else None))
    if not carriers:
        # An assigned feed is still read: it may teach its numbering even when nothing there is wanted.
        if show.feed_state == FeedState.ASSIGNED:
            return next((f for f in candidates if f.id == show.feed_id), None)
        return None
    return feeds.assign(ctx, show, carriers)


def _take(ctx: Context, state: ShowState, feed: Feed, entry: Resolved, best: Dict[int, int], added: Dict[int, str]) -> bool:
    episode, version, item = entry.episode, entry.version, entry.item
    # The user chose to go without this one: only taking that back, or grabbing it by hand, gets it.
    if episode.status == EpisodeStatus.SKIPPED:
        return False
    if version < best.get(entry.number, version):
        return False
    held = state.held_version(episode)
    upgrade = version > held
    cur = state.current.get(episode.id)
    if upgrade and cur is not None and downloads.is_batch_download(cur) and episode.status in RELEASE_HOLDING:
        # An episode that came in a batch keeps it: replacing it would delete the whole batch with its files.
        return False
    air_at = episode.air_at
    # An episode with no air time is taken like any other (the whole season is eligible); map_episode already
    # refuses numbers past the newest aired one.
    if not upgrade and air_at is not None and _undated(item) and ctx.now - air_at > timedelta(days=HUNTING_RECENT_DAYS):
        return False
    if episode.retry_after is not None and episode.retry_after > ctx.now:
        return False
    if version in state.failed.get(episode.id, set()):
        return False
    if episode.status == EpisodeStatus.FAILED and version <= held:
        return False
    if episode.status in RELEASE_HOLDING and version <= held:
        return False
    if downloads.active_download(ctx, episode) is not None:
        return False
    record_evidence(ctx, state, feed, entry.raw, entry.number)
    if item.id in added:
        dl = downloads.follow_download(ctx, state.show, feed, episode, item, added[item.id], source_episode=entry.raw)
    else:
        dl = downloads.start_download(ctx, state.show, feed, episode, item, source_episode=entry.raw)
        if dl is not None and entry.shared and dl.info_hash:
            added[item.id] = dl.info_hash
    if dl is None:
        return False
    state.current[episode.id] = dl if episode.current_download_id == dl.id else state.current.get(episode.id)
    return True


def _mark_missed(ctx: Context, state: ShowState, facts, resolved_by_feed) -> int:
    covered: Set[int] = set()
    for resolved, best in resolved_by_feed.values():
        covered.update(best)
    refreshed = facts_for(ctx, [state.show])[state.show.id]
    wanted = [e for e in refreshed.episodes if e.status == EpisodeStatus.WANTED]
    if not wanted:
        return 0
    numbers = [
        n for n in missed_numbers(list(refreshed.episodes), ctx.now, state.horizon, state.newest_aired, covered)
        if (e := state.by_number.get(n)) is not None and e.status == EpisodeStatus.WANTED
    ]
    from kisetsu.services import backfill  # backfill imports this module

    # Not missed while the search of the feeds' sites for it is still to run: it may well find it.
    if not numbers or backfill.search_pending(ctx, state.show.id, numbers):
        return 0
    marked = 0
    for number in numbers:
        episode = state.by_number[number]
        episode.status = EpisodeStatus.MISSED
        ctx.session.add(episode)
        events.record(
            ctx, EventKind.MISSED, "Not in the feed", show=state.show, episode=number,
        )
        marked += 1
    ctx.emit(f"show:{state.show.id}")
    return marked


def covered_numbers(ctx: Context, show: Show) -> Set[int]:
    """The show's episodes that the stored feed items can already supply; read only."""
    facts = facts_for(ctx, [show])[show.id]
    state = show_state(ctx, show, facts)
    covered: Set[int] = set()
    for feed in _candidate_feeds(ctx, show, feeds.ranked(ctx)):
        _, best = resolve(ctx, state, feed, learn=False)
        covered.update(best)
    return covered
