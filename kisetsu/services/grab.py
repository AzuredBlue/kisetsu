"""Deciding which stored feed item becomes which episode's download, and which episodes are missed."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError
from kisetsu.config import HUNTING_RECENT_DAYS
from kisetsu.db.schema import (
    ACTIVE_DOWNLOAD_STATES,
    Download,
    DownloadState,
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

EPOCH_YEAR = 2000


@dataclass
class GrabResult:
    started: int = 0
    missed: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class Resolved:
    item: FeedItem
    raw: int
    number: int
    episode: Episode
    version: int


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


def _state(ctx: Context, show: Show, facts) -> ShowState:
    episodes = list(ctx.session.exec(select(Episode).where(Episode.show_id == show.id).order_by(Episode.number)).all())
    ids = [e.id for e in episodes]
    current: Dict[int, Optional[Download]] = {}
    failed: Dict[int, Set[int]] = {}
    if ids:
        by_id = {d.id: d for d in ctx.session.exec(select(Download).where(Download.episode_id.in_(ids))).all()}
        for episode in episodes:
            current[episode.id] = by_id.get(episode.current_download_id)
        for dl in by_id.values():
            if dl.state == DownloadState.FAILED:
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
    base = matching.build_matcher(show.effective_aliases, show.learned_name)
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


def resolve(ctx: Context, state: ShowState, feed: Feed, *, learn: bool = True) -> Tuple[List[Resolved], Dict[int, int]]:
    """Which episode each stored item of a feed is for, and the newest usable version of each.

    The one place that decides whether an item is this show's release of one of its episodes:
    the grab acts on the result and the missed rule asks the same question with ``learn=False``,
    which never renumbers stored releases.
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

    for item in items:
        matched, parsed = matching.decide_dated(matcher, item.title, cache, on_schedule(item))
        if not matched:
            continue
        raw = parsed.get("episode")
        if raw is None and state.target_count == 1:
            raw = 1
        if raw is None or raw <= 0:
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
            continue
        if mapped.by_date and learn:
            _learn_offset(ctx, state, feed, int(raw), number)
        episode = state.by_number.get(number)
        if episode is None:
            continue
        if published_before_air(item.published_at, episode.air_at or state.first_air, ctx.tolerance_hours):
            continue
        version = int(parsed.get("version") or 1)
        resolved.append(Resolved(item, int(raw), number, episode, version))
        if version not in state.failed.get(episode.id, set()):
            best[number] = max(best.get(number, 0), version)
    return resolved, best


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
    for show in shows:
        if show.feed_state != FeedState.LOCKED and held and show.id in held:
            continue
        try:
            _grab_show(ctx, show, facts[show.id], all_feeds, result)
            ctx.checkpoint()
        except QbitError as e:
            result.errors.append(f"{show.title(ctx.settings.title_language)}: {e}")
            break
    if result.started or result.missed:
        ctx.emit("shows", "history")
    return result


def _grab_show(ctx: Context, show: Show, facts, all_feeds: List[Feed], result: GrabResult) -> None:
    state = _state(ctx, show, facts)
    if premiere_out_of_reach(facts, ctx.now) or (
        show.next_airing_episode == 1 and show.next_airing_at is not None and show.next_airing_at > state.horizon
    ):
        return
    newest_wanted = max(
        (e.number for e in state.episodes if e.status == EpisodeStatus.WANTED and (state.newest_aired is None or e.number <= state.newest_aired)),
        default=None,
    )
    candidates = _candidate_feeds(ctx, show, all_feeds)
    # Choosing a feed reads every candidate without learning from it; only the feed taken is read again with learning.
    resolved_by_feed = {f.id: resolve(ctx, state, f, learn=False) for f in candidates}
    chosen = _choose_feed(ctx, state, candidates, resolved_by_feed)
    grabbed_from: Optional[int] = None
    if chosen is not None:
        resolved, best = resolve(ctx, state, chosen)
        resolved_by_feed[chosen.id] = (resolved, best)
        for entry in resolved:
            if _take(ctx, state, chosen, entry, best, newest_wanted):
                grabbed_from = chosen.id
                result.started += 1
    if any(f.last_ok_at is not None and f.failing_since is None for f in candidates):
        result.missed += _mark_missed(ctx, state, facts, resolved_by_feed)
    if grabbed_from is not None and show.feed_state in (FeedState.NONE, FeedState.CANDIDATE):
        show.feed_id, show.feed_state, show.candidate_since = grabbed_from, FeedState.ASSIGNED, None
        ctx.session.add(show)


def _choose_feed(ctx: Context, state: ShowState, candidates: List[Feed], resolved_by_feed) -> Optional[Feed]:
    """The feed to grab from now: the locked, pinned or assigned one, else the discovery winner."""
    show = state.show
    if show.feed_state in (FeedState.LOCKED, FeedState.PINNED):
        return candidates[0] if candidates else None
    carriers: List[Carrier] = []
    for feed in candidates:
        resolved, best = resolved_by_feed[feed.id]
        carried = [r for r in resolved if _wants(r.episode) and r.version >= best.get(r.number, r.version)]
        if carried:
            dates = [r.item.published_at for r in carried if not _undated(r.item)]
            carriers.append(Carrier(feed, min(dates) if dates else None))
    if not carriers:
        # An assigned feed is still read: it may teach its numbering even when nothing there is wanted.
        if show.feed_state == FeedState.ASSIGNED:
            return next((f for f in candidates if f.id == show.feed_id), None)
        return None
    return feeds.assign(ctx, show, carriers)


def _take(ctx: Context, state: ShowState, feed: Feed, entry: Resolved, best: Dict[int, int], newest_wanted: Optional[int]) -> bool:
    episode, version, item = entry.episode, entry.version, entry.item
    if version < best.get(entry.number, version):
        return False
    held = state.held_version(episode)
    upgrade = version > held
    cur = state.current.get(episode.id)
    if upgrade and cur is not None and downloads.is_batch_download(cur) and episode.status in RELEASE_HOLDING:
        # An episode that came in a batch keeps it: replacing it would delete the whole batch with its files.
        return False
    air_at = episode.air_at
    if not upgrade and air_at is None and newest_wanted is not None and entry.number < newest_wanted:
        return False
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
    dl = downloads.start_download(ctx, state.show, feed, episode, item)
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
    marked = 0
    for number in missed_numbers(list(refreshed.episodes), ctx.now, state.horizon, state.newest_aired, covered):
        episode = state.by_number.get(number)
        if episode is None or episode.status != EpisodeStatus.WANTED:
            continue
        episode.status = EpisodeStatus.MISSED
        ctx.session.add(episode)
        events.record(
            ctx, EventKind.MISSED, "Not in the feed", show=state.show, episode=number,
        )
        marked += 1
    return marked


def covered_numbers(ctx: Context, show: Show) -> Set[int]:
    """The show's episodes that the stored feed items can already supply; read only."""
    facts = facts_for(ctx, [show])[show.id]
    state = _state(ctx, show, facts)
    covered: Set[int] = set()
    for feed in _candidate_feeds(ctx, show, feeds.ranked(ctx)):
        _, best = resolve(ctx, state, feed, learn=False)
        covered.update(best)
    return covered
