"""The engine's jobs: network work happens here, outside the engine thread; every database or qBittorrent step goes through ``engine.run``."""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import httpx2

from kisetsu.clients import palette as palette_client
from kisetsu.clients import rss, sites
from kisetsu.clients.anilist import AniListDeferred, AniListError
from kisetsu.clients.qbittorrent import QbitError
from kisetsu.config import DEFAULT_FEED_REFRESH_MINUTES
from kisetsu.db.schema import Show
from kisetsu.engine.runner import Engine
from kisetsu.engine.scheduler import Scheduler
from kisetsu.services import addshow, adoption, backfill, cycle, feedsync, tags
from kisetsu.services.context import setup_completed
from kisetsu.services.shows import SCHEDULE_FULL_LOOKBACK, SCHEDULE_REFRESH_LOOKBACK

logger = logging.getLogger("kisetsu.engine.jobs")

STARTUP_RETRY = 30.0
QBIT_RETRY = 30.0
ANILIST_ERROR_RETRY = 300.0
SETTLE_BUSY = 4.0
SETTLE_ACTIVE = 60.0
SETTLE_IDLE = 300.0
GRAB_FALLBACK = 15 * 60.0
BACKFILL_INTERVAL = 6 * 3600.0
HOUSEKEEPING_INTERVAL = 24 * 3600.0
HUNT_IDLE_MAX = 3600.0
# With no AniList user set there is nothing to sync until the settings change.
NO_USER_WAIT = 3600.0
# Until the first-run wizard is done the stored qBittorrent login is still the default one.
SETUP_WAIT = 3600.0

Fetcher = Callable[[str], Awaitable[List[rss.FeedArticle]]]

_KEEP: Any = object()


class Jobs:
    def __init__(
        self,
        engine: Engine,
        scheduler: Scheduler,
        *,
        anilist: Any,
        http: Optional[httpx2.AsyncClient] = None,
        fetcher: Optional[Fetcher] = None,
        searcher: Callable[..., Awaitable[Any]] = sites.search_feed,
        fetch_hues: Callable[..., Awaitable[Any]] = palette_client.fetch_hues,
        pause: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine = engine
        self.scheduler = scheduler
        self.anilist = anilist
        self.http = http
        self._fetcher = fetcher
        self.searcher = searcher
        self.fetch_hues = fetch_hues
        self.pause = pause
        self.ready = asyncio.Event()
        self.backoff = rss.Backoff()
        self.backfill_requests: Set[int] = set()
        self.backfill_all = False
        self.health: Dict[str, Any] = {"qbit": None, "anilist": None, "anilist_retry_in": None}

    # -- wiring -------------------------------------------------------------

    def register(self) -> None:
        add = self.scheduler.add
        add("startup", self.startup, interval=None, first=0.0)
        add("refresh_feeds", self.refresh_feeds, interval=DEFAULT_FEED_REFRESH_MINUTES * 60.0, first=None)
        add("hunt", self.hunt, interval=None, first=None)
        add("sync_anilist", self.sync_anilist, interval=NO_USER_WAIT, first=None)
        add("grab", self.grab, interval=GRAB_FALLBACK, first=None)
        add("settle", self.settle, interval=SETTLE_IDLE, first=None)
        add("backfill", self.backfill, interval=BACKFILL_INTERVAL, first=None)
        add("palette", self.palette, interval=None, first=None)
        add("housekeeping", self.housekeeping, interval=HOUSEKEEPING_INTERVAL, first=HOUSEKEEPING_INTERVAL)

    def poke(self, name: str, delay: float = 0.0) -> None:
        self.scheduler.poke(name, delay)

    async def _fetch(self, url: str) -> List[rss.FeedArticle]:
        if self._fetcher is not None:
            return await self._fetcher(url)
        return await rss.fetch_feed(self.http, url)

    def _set_health(self, qbit: Any = _KEEP, anilist: Any = _KEEP, anilist_retry_in: Any = _KEEP) -> None:
        """Record what is wrong with qBittorrent or AniList; a new problem is logged and every change reaches the UI."""
        changes = {"qbit": qbit, "anilist": anilist, "anilist_retry_in": anilist_retry_in}
        changed = False
        for key, value in changes.items():
            if value is _KEEP or self.health[key] == value:
                continue
            if value is not None and key != "anilist_retry_in":
                logger.warning(f"{'qBittorrent' if key == 'qbit' else 'AniList'}: {value}")
            if value is None and key == "qbit":
                logger.info("qBittorrent connection restored.")
            self.health[key] = value
            changed = True
        if changed:
            self.engine.bus.publish(["status"])

    def _qbit_failed(self, error: Exception) -> float:
        self._set_health(qbit=str(error))
        return QBIT_RETRY

    async def _configured(self) -> bool:
        return await self.engine.read(setup_completed)

    # -- startup ------------------------------------------------------------

    async def startup(self) -> Optional[float]:
        """Bring the app up in an order that never grabs or deletes before it knows what already exists."""
        if not await self._configured():
            return SETUP_WAIT
        try:
            await self.engine.run(lambda ctx: ctx.qbit.sync(full=True))
        except QbitError as e:
            return self._qbit_failed(e)
        self._set_health(qbit=None)
        await self.sync_anilist(startup=True)
        await self.refresh_feeds()
        await self.engine.run(adoption.adopt_existing)
        await self.backfill()
        await self.engine.run(adoption.reattribute)
        await self.engine.run(tags.sync_tags)
        await self.search_saved_files()
        self.ready.set()
        self.engine.bus.publish(["status"])
        for name in ("grab", "settle", "hunt", "palette"):
            self.poke(name)
        self.scheduler.delay("refresh_feeds", await self._feed_interval())
        self.scheduler.delay("backfill", BACKFILL_INTERVAL)
        return None

    async def search_saved_files(self, only: Optional[List[int]] = None) -> None:
        """Match shows to the feed their saved files came from; shows no feed accounts for are searched for first."""
        _, unmatched = await self.engine.run(cycle.lock_saved_files)
        targets = [i for i in unmatched if only is None or i in only]
        for show_id in targets:
            await self._backfill_show(show_id, force=True)
            if await self.engine.run(lambda ctx, i=show_id: backfill.search_completed(ctx, i)):
                await self.engine.run(cycle.release_hold, [show_id])
        if targets:
            await self.engine.run(cycle.lock_saved_files)

    async def on_new_shows(self, show_ids: List[int]) -> None:
        """Fill the stores for shows that were only just added, so the same pass can discover their feed."""
        await self.refresh_feeds()
        await self.engine.run(adoption.adopt_existing)
        for show_id in show_ids:
            await self._backfill_show(show_id, force=True)
        await self.engine.run(adoption.reattribute)
        await self.search_saved_files(show_ids)
        self.poke("grab")

    # -- feeds --------------------------------------------------------------

    async def _read_feeds(self, refs: List[feedsync.FeedRef]) -> Dict[int, feedsync.FetchResult]:
        """Fetch the feeds in parallel; a feed backing off after failures is skipped."""
        now = self.engine.clock()
        due = [ref for ref in refs if self.backoff.ready(str(ref.id), now)]

        async def one(ref: feedsync.FeedRef):
            try:
                return ref.id, await self._fetch(ref.url)
            except rss.FeedFetchError as e:
                return ref.id, str(e)
            except Exception as e:
                logger.warning(f"Reading '{ref.name}' failed: {e}")
                return ref.id, f"{ref.name} could not be read ({e})"

        results = dict(await asyncio.gather(*(one(ref) for ref in due)))
        for feed_id, result in results.items():
            if isinstance(result, str):
                self.backoff.failed(str(feed_id), now)
            else:
                self.backoff.succeeded(str(feed_id))
        return results

    async def _apply(self, results: Dict[int, feedsync.FetchResult]) -> feedsync.Outcome:
        outcome = await self.engine.run(feedsync.apply_fetch, results)
        if outcome.matched_show_ids or outcome.new:
            self.poke("grab")
        if outcome.gaps:
            self.backfill_all = True
            self.poke("backfill", 5.0)
        return outcome

    async def refresh_feeds(self) -> Optional[float]:
        """Fetch every feed and store what is new."""
        if not await self._configured():
            return None
        refs = await self.engine.run(feedsync.plan_fetch)
        results = await self._read_feeds(refs)
        if results:
            await self._apply(results)
        return await self._feed_interval()

    async def _feed_interval(self) -> float:
        return await self.engine.read(lambda ctx: ctx.settings.feed_refresh_minutes * 60.0)

    async def hunt(self) -> Optional[float]:
        """While shows are in their hunting window, fetch the feeds they may use more often."""
        if not await self._configured():
            return None
        info = await self.engine.run(feedsync.plan_hunt)
        if info.show_ids and info.feeds:
            results = await self._read_feeds(info.feeds)
            if results:
                await self._apply(results)
            return info.interval_minutes * 60.0
        start = info.plan.next_start
        if start is None:
            return HUNT_IDLE_MAX
        return max(5.0, min((start - self.engine.clock()).total_seconds(), HUNT_IDLE_MAX))

    # -- AniList ------------------------------------------------------------

    async def sync_anilist(self, startup: bool = False) -> Optional[float]:
        info = await self.engine.run(cycle.sync_input)
        if not info.username:
            return NO_USER_WAIT
        interval = max(1, info.sync_minutes) * 60.0
        if startup and info.last_sync_ago is not None and info.last_sync_ago < interval and info.monitored_ids:
            await self.engine.run(cycle.rollover)
            return max(30.0, interval - info.last_sync_ago)
        try:
            seasonal = await self.anilist.fetch_user_seasonal_anime(info.username, monitored_anilist_ids=info.monitored_ids)
        except AniListDeferred as e:
            self._set_health(anilist=str(e), anilist_retry_in=e.retry_in)
            await self.engine.run(cycle.rollover)
            return max(30.0, e.retry_in)
        except AniListError as e:
            self._set_health(anilist=str(e), anilist_retry_in=ANILIST_ERROR_RETRY)
            await self.engine.run(cycle.rollover)
            return ANILIST_ERROR_RETRY
        self._set_health(anilist=None, anilist_retry_in=None)
        applied = await self.engine.run(cycle.apply_sync, seasonal)
        retry = await self.refresh_schedules()
        if applied.added_ids and self.ready.is_set():
            await self.on_new_shows(applied.added_ids)
        for name in ("grab", "hunt", "palette"):
            self.poke(name)
        return max(30.0, retry) if retry else interval

    async def refresh_schedules(self) -> Optional[float]:
        """Date the episodes of the shows whose air times are old; returns the wait if AniList deferred part of it."""
        plan = await self.engine.run(cycle.schedule_plan)
        if not plan.due_ids:
            return None
        now = self.engine.clock()
        schedules: Dict[int, Any] = {}
        full = list(plan.full)
        deferral: Optional[AniListDeferred] = None
        try:
            if plan.windowed:
                schedules.update(await self.anilist.fetch_upcoming_airing_schedules(plan.windowed, now - SCHEDULE_REFRESH_LOOKBACK))
            if full:
                covering = await self.anilist.fetch_upcoming_airing_schedules(full, now - SCHEDULE_FULL_LOOKBACK)
                schedules.update(covering)
                full = await self.engine.run(cycle.uncovered, full, covering)
            if full:
                schedules.update(await self.anilist.fetch_media_airing_schedules(full))
        except AniListDeferred as e:
            deferral = e
            schedules.update(e.partial)
        except AniListError as e:
            logger.warning(f"Could not refresh episode airing schedules: {e}")
            return ANILIST_ERROR_RETRY
        if schedules:
            await self.engine.run(cycle.apply_schedule_results, plan.due_ids, schedules)
            for name in ("grab", "hunt"):
                self.poke(name)
        if deferral is not None:
            self._set_health(anilist=str(deferral), anilist_retry_in=deferral.retry_in)
            return deferral.retry_in
        return None

    # -- downloads ----------------------------------------------------------

    async def grab(self) -> Optional[float]:
        if not self.ready.is_set():
            return None
        try:
            result = await self.engine.run(cycle.grab_cycle)
        except QbitError as e:
            return self._qbit_failed(e)
        self._set_health(qbit=result.errors[0] if result.errors else None)
        if result.started or result.adopted:
            self.poke("settle")
        return result.next_candidate_in

    async def settle(self) -> Optional[float]:
        if not self.ready.is_set():
            return None
        try:
            result = await self.engine.run(cycle.settle_cycle)
        except QbitError as e:
            return self._qbit_failed(e)
        self._set_health(qbit=None)
        if result.changed:
            self.poke("grab")
        delay = SETTLE_BUSY if result.busy else SETTLE_ACTIVE if result.active else SETTLE_IDLE
        if result.next_due is not None:
            wait = (result.next_due - self.engine.clock()).total_seconds()
            delay = min(delay, max(1.0, wait))
        return delay

    # -- past releases ------------------------------------------------------

    def request_backfill(self, show_ids: Optional[List[int]] = None) -> None:
        if show_ids is None:
            self.backfill_all = True
        else:
            self.backfill_requests.update(show_ids)
        self.poke("backfill")

    async def backfill(self) -> Optional[float]:
        """Search the feeds' sites for episodes no stored item supplies."""
        requested, self.backfill_requests = self.backfill_requests, set()
        everything, self.backfill_all = self.backfill_all, False
        added = 0
        for show_id in requested:
            added += await self._backfill_show(show_id, force=True)
        if everything or not requested:
            due = await self.engine.run(lambda ctx: backfill.due(ctx, force=everything))
            added += await self._search(due)
        if added:
            self.poke("grab")
        return None

    async def search_older(self, show_id: int) -> int:
        """Search the show's feed sites for past episodes now; returns how many releases were found."""
        added = await self._backfill_show(show_id, force=True)
        if added:
            self.poke("grab")
        return added

    async def _backfill_show(self, show_id: int, force: bool = False, unmanaged: bool = False) -> int:
        due = await self.engine.run(lambda ctx: backfill.due(ctx, show_id=show_id, force=force, unmanaged=unmanaged))
        return await self._search(due)

    async def _search(self, due: "List[backfill.Due]") -> int:
        added = 0
        for position, item in enumerate(due):
            if position:
                await self.pause(sites.REQUEST_GAP_SECONDS)
            matcher = await self.engine.run(lambda ctx, i=item.show_id: backfill.matcher_for(ctx.session.get(Show, i)))
            found = await backfill.search(self.http, item, matcher, self.engine.clock(), pause=self.pause, searcher=self.searcher)
            added += await self.engine.run(backfill.apply, item, found)
            if found.stopped:
                logger.warning(f"Past-release search stopped: {found.stopped}")
                break
        return added

    async def search_new_show(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Search the feeds' sites for a show the user is adding, then preview what each feed carries for it."""
        plan = await self.engine.run(addshow.search_plan, entry)
        stopped = None
        if plan.terms and plan.feeds:
            total = entry.get("total_episodes") or max(1, (entry.get("next_airing_episode") or 2) - 1)
            due = backfill.Due(0, entry.get("display_name") or "", plan.terms, plan.feeds, list(range(1, total + 1)), 0)
            found = await backfill.search(self.http, due, addshow.matcher_for(entry), self.engine.clock(), pause=self.pause, searcher=self.searcher)
            stopped = found.stopped
            if found.articles:
                await self.engine.run(addshow.store_found, found.articles)
        result = await self.engine.dry_run(addshow.preview, entry, plan.unsearchable)
        result["stopped"] = stopped
        return result

    async def after_manual_add(self) -> None:
        """Date the episodes of a show the user just added, then let the grab and settle passes follow it."""
        try:
            await self.refresh_schedules()
        except Exception as e:
            logger.warning(f"Could not date the new show's episodes: {e}")
        for name in ("grab", "settle", "hunt", "palette"):
            self.poke(name)

    # -- upkeep -------------------------------------------------------------

    async def palette(self) -> Optional[float]:
        for show_id, key, banner, cover in await self.engine.run(cycle.palette_targets):
            ok, hues = await self.fetch_hues(banner, cover)
            # A failed download is not remembered, so it is tried again next time.
            if ok:
                await self.engine.run(cycle.store_palette, show_id, key, hues)
        return None

    async def housekeeping(self) -> Optional[float]:
        try:
            await self.engine.run(cycle.housekeeping)
        except QbitError as e:
            logger.debug(f"Housekeeping could not reach qBittorrent: {e}")
        return None

