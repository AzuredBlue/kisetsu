"""The engine's jobs: network work happens here, outside the engine thread; every database or qBittorrent step goes through ``engine.run``."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

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
from kisetsu.services.context import get_state, set_state, setup_completed
from kisetsu.services.shows import SCHEDULE_FULL_LOOKBACK, SCHEDULE_REFRESH_LOOKBACK

logger = logging.getLogger("kisetsu.engine.jobs")

QBIT_RETRY = 30.0
ANILIST_ERROR_RETRY = 300.0
SETTLE_BUSY = 4.0
SETTLE_ACTIVE = 60.0
SETTLE_IDLE = 300.0
GRAB_FALLBACK = 15 * 60.0
BACKFILL_INTERVAL = 6 * 3600.0
# Shows whose past releases are searched for at the same time; each site still gets one request at a time.
SEARCH_CONCURRENCY = 4
# Cover images read at the same time for their colours (AniList's image server).
PALETTE_CONCURRENCY = 4
HOUSEKEEPING_INTERVAL = 24 * 3600.0
# Well before a whole interval, or a machine restarted every day would never get to it.
HOUSEKEEPING_FIRST = 3600.0
HUNT_IDLE_MAX = 3600.0
# How long Add show keeps a search's results for the Add button.
FOUND_KEEP_SECONDS = 3600.0
# The list the setup check fetched is taken by the sync that follows within this time, instead of asking again.
LIST_REUSE_SECONDS = 120.0
# A burst of pokes (a search finishing every second or so) runs the job at most once in this time; a lone poke runs it at once.
BURST_GAP_SECONDS = 1.0
# SubsPlease's schedule of the shows it airs is read again for a past-release search after this long.
SCHEDULE_KEEP = timedelta(hours=1)
# Where what the searches learned of each site (``sites.Grouping``) is kept across restarts.
GROUPING_KEY = "search_grouping"
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
    ) -> None:
        self.engine = engine
        self.scheduler = scheduler
        self.anilist = anilist
        self.http = http
        self._fetcher = fetcher
        self.searcher = searcher
        # Whether nyaa takes a show's names in one search; checked by the searches themselves.
        self.grouping = sites.Grouping()
        # What of it is stored; None until it is read back.
        self._grouping_kept: Optional[str] = None
        # SubsPlease's schedule (``sites.subsplease_schedule``): the titles, and when they were read.
        self.read_schedule = sites.subsplease_schedule
        self._schedule: Optional[List[str]] = None
        self._schedule_at: Optional[datetime] = None
        # When ``_poke_soon`` last let each job run.
        self._poked_at: Dict[str, float] = {}
        self.fetch_hues = fetch_hues
        self.ready = asyncio.Event()
        # Set once the first full qBittorrent sync is in. Downloads can be followed from then on (but not added to, deleted
        # or forgotten: see ``settle``); grabbing waits for ``ready``.
        self.synced = asyncio.Event()
        self.backoff = rss.Backoff()
        self.backfill_requests: Set[int] = set()
        self.backfill_all = False
        self.refresh_all = False
        # When the hunt will next read a feed (None: nothing is due to air), for the UI's countdown.
        self.hunt_read_at: Optional[datetime] = None
        self.health: Dict[str, Any] = {"qbit": None, "anilist": None, "anilist_retry_in": None, "grab": None}
        # Set once startup found qBittorrent away and handed AniList and the feeds to their own timers.
        self.offline_started = False
        # A direct call (startup, a new show, Search older) and the same job must never run at once.
        self._feeds_lock = asyncio.Lock()
        self._anilist_lock = asyncio.Lock()
        self._search_locks: Dict[int, asyncio.Lock] = {}
        # Add show: what a search found, by AniList id, until the user adds the show or it ages out.
        self._found: Dict[int, Any] = {}

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
        add("housekeeping", self.housekeeping, interval=HOUSEKEEPING_INTERVAL, first=HOUSEKEEPING_FIRST)

    def poke(self, name: str, delay: float = 0.0) -> None:
        self.scheduler.poke(name, delay)

    def _poke_soon(self, name: str, gap: float = BURST_GAP_SECONDS) -> None:
        """Poke the job now, or ``gap`` after the run the last such poke asked for, so a burst makes one run in that time.

        A poke while that run is still to come asks for it again (in case it ran early on another poke), not for a later one.
        """
        now = self.scheduler.clock()
        last = self._poked_at.get(name, now - gap)
        at = last if last > now else max(now, last + gap)
        self._poked_at[name] = at
        self.poke(name, at - now)

    @asynccontextmanager
    async def stopped(self) -> AsyncIterator[None]:
        """Hold every job while the database is replaced, since a job keeps ids between its steps, then start them over as after a restart."""
        running = self.scheduler.running
        await self.scheduler.stop()
        try:
            yield
        finally:
            self.ready.clear()
            self.synced.clear()
            self.backoff = rss.Backoff()
            self.backfill_requests, self.backfill_all, self.refresh_all = set(), False, False
            self.hunt_read_at = None
            self.offline_started = False
            self.health.update(qbit=None, anilist=None, anilist_retry_in=None, grab=None)
            if running:
                self.register()
                self.scheduler.start()
            self.engine.bus.publish(["status"])

    async def _fetch(self, url: str) -> List[rss.FeedArticle]:
        if self._fetcher is not None:
            return await self._fetcher(url)
        return await rss.fetch_feed(self.http, url)

    def _set_health(self, qbit: Any = _KEEP, anilist: Any = _KEEP, anilist_retry_in: Any = _KEEP, grab: Any = _KEEP) -> None:
        """Record what is wrong with qBittorrent, AniList or the grab; a new problem is logged and every change reaches the UI."""
        changes = {"qbit": qbit, "anilist": anilist, "anilist_retry_in": anilist_retry_in, "grab": grab}
        names = {"qbit": "qBittorrent", "anilist": "AniList", "grab": "Grab"}
        changed = False
        for key, value in changes.items():
            if value is _KEEP or self.health[key] == value:
                continue
            if value is not None and key != "anilist_retry_in":
                logger.warning(f"{names[key]}: {value}")
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
            if not self.offline_started:
                # AniList and the feeds don't need qBittorrent: keep them current on their own timers while it is away.
                self.offline_started = True
                self.poke("sync_anilist")
                self.poke("refresh_feeds")
            return self._qbit_failed(e)
        self._set_health(qbit=None)
        # What is downloading shows progress from here on, instead of after every backfill search has finished.
        self.synced.set()
        self.poke("settle")
        # AniList and the feeds are different sites, so they are read together. Items stored before a show is added are
        # matched to it by the grab pass (``ensure_matches_current``).
        next_sync, _, _ = await asyncio.gather(
            self.sync_anilist(startup=True), self.refresh_feeds(everything=True), self._read_schedule(),
        )
        await self.engine.run(adoption.adopt_existing)
        await self.engine.run(adoption.reattribute)
        await self.engine.run(tags.sync_tags)
        # Shows whose saved files no feed accounts for yet are held, so nothing guesses their feed before the search below.
        _, unmatched = await self.engine.run(cycle.lock_saved_files)
        # Adoption has seen what qBittorrent holds: grabbing what the feeds already carry need not wait for the searches.
        self.ready.set()
        self.engine.bus.publish(["status"])
        for name in ("grab", "settle", "hunt", "palette"):
            self.poke(name)
        self.scheduler.delay("sync_anilist", next_sync)
        self.scheduler.delay("refresh_feeds", await self._next_read())
        await self.backfill()
        await self._search_held(unmatched)
        self.scheduler.delay("backfill", BACKFILL_INTERVAL)
        return None

    async def search_saved_files(self, only: Optional[List[int]] = None) -> None:
        """Match shows to the feed their saved files came from; shows no feed accounts for are searched for first."""
        _, unmatched = await self.engine.run(cycle.lock_saved_files)
        await self._search_held([i for i in unmatched if only is None or i in only])

    async def _search_held(self, targets: List[int]) -> None:
        """Search for the held shows' releases, release each one searched, then lock what the saved files now prove."""
        for show_id in targets:
            # Every feed is searched: the saved files are matched to the one feed whose items hold them.
            await self._backfill_show(show_id, force=True, exhaustive=True)
            if await self.engine.run(lambda ctx, i=show_id: backfill.search_completed(ctx, i)):
                await self.engine.run(cycle.release_hold, [show_id])
        if targets:
            await self.engine.run(cycle.lock_saved_files)
            self.poke("grab")

    async def on_new_shows(self, show_ids: List[int]) -> None:
        """Fill the stores for shows that were only just added, so the same pass can discover their feed."""
        await self.refresh_feeds(everything=True)
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
                return ref.id, feedsync.FetchFailure(str(e), e.transient)
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
        if outcome.reconsider:
            # A feed their searches left out has just shown it carries them: what it holds may be what they miss.
            self.request_backfill(sorted(outcome.reconsider))
        return outcome

    def request_refresh(self) -> None:
        """Read every feed on the next pass, needed or not."""
        self.refresh_all = True
        self.poke("refresh_feeds")

    async def refresh_feeds(self, everything: bool = False) -> Optional[float]:
        """Fetch the feeds a show needs, or all of them, and store what is new."""
        if not await self._configured():
            return None
        async with self._feeds_lock:
            everything, self.refresh_all = everything or self.refresh_all, False
            refs = await self.engine.run(feedsync.plan_fetch, everything)
            results = await self._read_feeds(refs)
            if results:
                await self._apply(results)
        return await self._next_read()

    async def _next_read(self) -> float:
        """Seconds until the routine read has a feed to read; never sooner than its interval, as that is how often it looks."""
        interval = await self.engine.read(lambda ctx: ctx.settings.feed_refresh_minutes * 60.0)
        at = await self.engine.read(feedsync.next_read_at, timedelta(seconds=interval))
        if at is None:
            return interval
        return max(interval, (at - self.engine.clock()).total_seconds())

    async def hunt(self) -> Optional[float]:
        """While shows are in their hunting window, fetch the feeds they may use more often."""
        if not await self._configured():
            return None
        # Planned under the lock, so a read still running (the startup one) counts as done instead of being repeated.
        async with self._feeds_lock:
            info = await self.engine.run(feedsync.plan_hunt)
            if info.show_ids and info.feeds:
                results = await self._read_feeds(info.feeds)
                if results:
                    await self._apply(results)
        if info.show_ids and info.feeds:
            self.hunt_read_at = self.engine.clock() + timedelta(minutes=info.interval_minutes)
            return info.interval_minutes * 60.0
        if info.show_ids and info.wait_seconds is not None:
            # Their feeds were just read by another job: hunt once the first of them is an interval old.
            self.hunt_read_at = self.engine.clock() + timedelta(seconds=info.wait_seconds)
            return max(5.0, info.wait_seconds)
        start = info.plan.next_start
        self.hunt_read_at = start
        if start is None:
            return HUNT_IDLE_MAX
        return max(5.0, min((start - self.engine.clock()).total_seconds(), HUNT_IDLE_MAX))

    # -- AniList ------------------------------------------------------------

    async def sync_anilist(self, startup: bool = False) -> Optional[float]:
        async with self._anilist_lock:
            return await self._sync_anilist(startup)

    async def _sync_anilist(self, startup: bool) -> Optional[float]:
        info = await self.engine.run(cycle.sync_input)
        if not info.username:
            return NO_USER_WAIT
        interval = max(1, info.sync_minutes) * 60.0
        if startup and info.last_sync_ago is not None and info.last_sync_ago < interval and info.monitored_ids:
            await self.engine.run(cycle.rollover)
            wait = max(30.0, interval - info.last_sync_ago)
            logger.info(f"AniList was synced {round(info.last_sync_ago / 60)} min ago. Next sync in {round(wait / 60)} min.")
            return wait
        try:
            seasonal = await self.anilist.fetch_user_seasonal_anime(
                info.username, monitored_anilist_ids=info.monitored_ids, reuse_within=LIST_REUSE_SECONDS if startup else None,
            )
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
        wait = max(30.0, retry) if retry else interval
        logger.info(f"AniList synced: {len(seasonal)} shows. Next sync in {round(wait / 60)} min.")
        if applied.added_ids and self.ready.is_set():
            await self.on_new_shows(applied.added_ids)
        for name in ("grab", "hunt", "palette"):
            self.poke(name)
        return wait

    async def refresh_schedules(self) -> Optional[float]:
        """Date the episodes of the shows whose air times are old; returns the wait if AniList deferred part of it."""
        plan = await self.engine.run(cycle.schedule_plan)
        if not plan.due_ids:
            return None
        now = self.engine.clock()
        schedules: Dict[int, Any] = {}
        full = list(plan.full)
        # The shows still owed their whole schedule; if AniList defers, they are left undated for the next pass.
        owed: List[int] = []
        deferral: Optional[AniListDeferred] = None
        try:
            if plan.windowed:
                schedules.update(await self.anilist.fetch_upcoming_airing_schedules(plan.windowed, now - SCHEDULE_REFRESH_LOOKBACK))
            if full:
                owed = full
                covering = await self.anilist.fetch_upcoming_airing_schedules(full, now - SCHEDULE_FULL_LOOKBACK)
                schedules.update(covering)
                full = owed = await self.engine.run(cycle.uncovered, full, covering)
            if full:
                schedules.update(await self.anilist.fetch_media_airing_schedules(full))
                owed = []
        except AniListDeferred as e:
            deferral = e
            schedules.update(e.partial)
            for media_id in owed:
                schedules.pop(media_id, None)
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
        if result.errors:
            # Only a failed call says qBittorrent is away; a pass with nothing to add never asked it, so settle gives the all-clear.
            self._set_health(qbit=result.errors[0])
        self._set_health(grab=result.failed[0] if result.failed else None)
        if result.started or result.adopted:
            # The grab already took up the torrents qBittorrent shows; settle follows them as often as it does a busy queue.
            self._poke_soon("settle", SETTLE_BUSY)
        return result.next_candidate_in

    async def settle(self) -> Optional[float]:
        """Follow the downloads. Until startup is done it only mirrors qBittorrent: adoption has not yet looked at what exists, so nothing is added, deleted or forgotten."""
        if not self.synced.is_set():
            return None
        try:
            result = await self.engine.run(cycle.settle_cycle, not self.ready.is_set())
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
            await self._read_schedule()
            await self.engine.run(backfill.note_schedule, self._schedule)
            due = await self.engine.run(lambda ctx: backfill.due(ctx, force=everything))
            added += await self._search(due)
        if added:
            self.poke("grab")
        return None

    async def _read_schedule(self) -> None:
        """Read which shows SubsPlease airs, when a feed is on SubsPlease and the last read is old. One request."""
        now = self.engine.clock()
        if self._schedule_at is not None and now - self._schedule_at < SCHEDULE_KEEP:
            return
        if not await self.engine.read(backfill.subsplease_feeds):
            return
        try:
            titles = await self.read_schedule(self.http)
        except rss.FeedFetchError as e:
            logger.debug(f"SubsPlease's schedule could not be read: {e}")
            return
        self._schedule, self._schedule_at = titles, now

    async def search_older(self, show_id: int) -> int:
        """Search the show's feed sites for past episodes now; returns how many releases were found."""
        added = await self._backfill_show(show_id, force=True, everywhere=True)
        if added:
            self.poke("grab")
        return added

    async def _backfill_show(
        self, show_id: int, force: bool = False, unmanaged: bool = False, exhaustive: bool = False, everywhere: bool = False,
    ) -> int:
        due = await self.engine.run(
            lambda ctx: backfill.due(ctx, show_id=show_id, force=force, unmanaged=unmanaged, everywhere=everywhere)
        )
        return await self._search(due, exhaustive=exhaustive)

    async def _search(self, due: "List[backfill.Due]", exhaustive: bool = False) -> int:
        """Search for the shows' past releases, a few shows at a time; each site's own pacing keeps its requests spaced.

        Unless ``exhaustive``, a show's lower feeds are skipped once its missing episodes are found. A search a site
        cut short (rate limiting) stops the shows not yet started.
        """
        gate = asyncio.Semaphore(SEARCH_CONCURRENCY)
        stopped: List[str] = []
        await self._load_grouping()

        async def one(item: "backfill.Due") -> int:
            async with gate:
                if stopped:
                    return 0
                # The backfill job and Search older never search one show twice at once.
                async with self._search_locks.setdefault(item.show_id, asyncio.Lock()):
                    matcher = await self.engine.run(lambda ctx, i=item.show_id: backfill.matcher_for(ctx.session.get(Show, i), ctx.settings.min_resolution))
                    found = await backfill.search(
                        self.http, item, matcher, self.engine.clock(), searcher=self.searcher, stop_when_found=not exhaustive,
                        grouping=self.grouping,
                    )
                    added = await self.engine.run(backfill.apply, item, found)
                if found.stopped:
                    if not stopped:
                        logger.warning(f"Past-release search stopped: {found.stopped}")
                    stopped.append(found.stopped)
                    return added
                # Whatever it found, a finished search lets the grab mark what is still not there as missed.
                self._poke_soon("grab")
                return added

        added = sum(await asyncio.gather(*(one(item) for item in due)))
        await self._keep_grouping()
        return added

    async def _load_grouping(self) -> None:
        """Take back what the searches learned of each site before a restart, so it is not checked again."""
        if self._grouping_kept is None:
            self._grouping_kept = await self.engine.run(lambda ctx: get_state(ctx, GROUPING_KEY))
            self.grouping.load(self._grouping_kept)

    async def _keep_grouping(self) -> None:
        kept = self.grouping.dump()
        if kept != self._grouping_kept:
            await self.engine.run(lambda ctx: set_state(ctx, GROUPING_KEY, kept))
            self._grouping_kept = kept

    async def search_new_show(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Search the feeds' sites for a show the user is adding, then preview what each feed carries for it.

        What the search found is kept in memory, and stored only if the user adds the show (``found_for``).
        """
        plan = await self.engine.run(addshow.search_plan, entry)
        stopped = None
        articles: Dict[int, Any] = {}
        if plan.terms and plan.feeds:
            await self._load_grouping()
            total = entry.get("total_episodes") or max(1, (entry.get("next_airing_episode") or 2) - 1)
            due = backfill.Due(0, entry.get("display_name") or "", plan.terms, plan.feeds, list(range(1, total + 1)), 0)
            found = await backfill.search(
                self.http, due, addshow.matcher_for(entry, plan.min_resolution), self.engine.clock(), searcher=self.searcher,
                grouping=self.grouping,
            )
            stopped = found.stopped
            articles = found.articles
            await self._keep_grouping()
        now = time.monotonic()
        self._found = {k: v for k, v in self._found.items() if now - v[0] < FOUND_KEEP_SECONDS}
        self._found[entry["anilist_id"]] = (now, articles)
        result = await self.engine.dry_run(addshow.preview, entry, plan.unsearchable, articles)
        result["stopped"] = stopped
        return result

    def found_for(self, anilist_id: int) -> Dict[int, Any]:
        """What the last search for this entry found, to store when the show is added; kept until ``forget_found``."""
        kept = self._found.get(anilist_id)
        return kept[1] if kept and time.monotonic() - kept[0] < FOUND_KEEP_SECONDS else {}

    def forget_found(self, anilist_id: int) -> None:
        self._found.pop(anilist_id, None)

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
        """Read the colours of the banners that changed, a few at a time, and store them all as one change.

        The colour along each poster's bottom edge is read by the browser, from the cover it shows (``edges.js``).
        """
        hue_targets = await self.engine.run(cycle.palette_targets)
        gate = asyncio.Semaphore(PALETTE_CONCURRENCY)

        async def hues(banner: str, cover: str):
            async with gate:
                return await self.fetch_hues(banner, cover)

        got_hues = await asyncio.gather(*(hues(banner, cover) for _, _, banner, cover in hue_targets))

        def store(ctx) -> None:
            # A failed download is not remembered, so it is tried again next time.
            for (show_id, key, _, _), (ok, found) in zip(hue_targets, got_hues):
                if ok:
                    cycle.store_palette(ctx, show_id, key, found)

        if hue_targets:
            await self.engine.run(store)
        return None

    async def housekeeping(self) -> Optional[float]:
        if not await self._configured():
            return None
        try:
            await self.engine.run(cycle.housekeeping)
        except QbitError as e:
            logger.debug(f"Housekeeping could not reach qBittorrent: {e}")
        return None

