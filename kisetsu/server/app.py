import asyncio
import ipaddress
import os
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urlsplit
import logging
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session, select

from kisetsu.config import RULE_OBSERVER_INTERVAL_SECONDS
from kisetsu.db.models import QbitRuleWatermark
from kisetsu.db.session import get_engine, get_settings, init_db
from kisetsu.clients.qbit import QBitClient, QbitAuthenticationError, QbitClientError, QbitRSSRefreshError
from kisetsu.clients.anilist import AniListClient
from kisetsu.core.confirmation import ingest_log_acceptances
from kisetsu.core.feedcache import backfill_cache, ingest_rss, prune_cache
from kisetsu.core.grabber import has_unsettled_work, update_episode_status
from kisetsu.core.supervisor import Supervisor
from kisetsu.workers.scheduler import calculate_next_poll_interval, is_hunting
from kisetsu.server.api import router
from kisetsu.server.state import state
from kisetsu.server.web_ui import get_web_ui_html

logger = logging.getLogger("kisetsu.server")


# Escalating waits used while qBittorrent is unreachable. The delay settles at
# 60s so a container that restarts is picked up within a minute.
QBIT_RETRY_DELAYS = (1, 2, 4, 8, 16, 30, 60)
# A rejected password will not fix itself, so stop hammering it quickly.
QBIT_AUTH_RETRY_SECONDS = 300
STARTUP_GRACE_SECONDS = 5
BUSY_RETRY_SECONDS = 5
SETTLE_INTERVAL_SECONDS = 4
INGEST_INTERVAL_SECONDS = 60
INGEST_ERROR_SECONDS = 30
PRUNE_INTERVAL_SECONDS = 24 * 3600
SETTLE_ERROR_SECONDS = 30


def _format_sleep(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"


def _set_next_check(seconds: int, reason: str) -> None:
    now_utc = datetime.now(timezone.utc)
    state.next_check_seconds = seconds
    state.next_check_reason = reason
    state.target_next_check_time = now_utc + timedelta(seconds=seconds)
    state.add_log(f"Next check: {reason} (Sleeping {_format_sleep(seconds)}).", "INFO")


def _next_qbit_retry(retry_index: int) -> int:
    return QBIT_RETRY_DELAYS[min(retry_index, len(QBIT_RETRY_DELAYS) - 1)]


def _backoff(retry_index: int) -> tuple[int, int]:
    """The wait for this failure and the retry index to carry into the next one."""
    return _next_qbit_retry(retry_index), min(retry_index + 1, len(QBIT_RETRY_DELAYS) - 1)


async def background_supervisor_task():
    """Supervisor loop running concurrently with the WebUI server."""
    engine = get_engine()
    anilist = AniListClient()
    state.add_log("Background supervisor service initialized.", "INFO")
    retry_index = 0

    await asyncio.sleep(STARTUP_GRACE_SECONDS)
    hunting_next = False
    needs_rss_refresh = True
    busy_logged = False
    while True:
        connection_ready = False
        sleep_seconds = 60
        deferred_reason: Optional[str] = None
        try:
            with Session(engine) as session:
                settings = get_settings(session)
                qbit = QBitClient(host=settings.qbit_host, username=settings.qbit_username, password=settings.qbit_password, timeout=10)

                # Probe before doing any work. Running a cycle against a qbit that
                # is still booting only produces a burst of per-show failures.
                try:
                    await asyncio.to_thread(qbit.test_connection)
                    connection_ready = True
                    if retry_index:
                        state.add_log("qBittorrent connection restored.", "INFO")
                        retry_index = 0
                except QbitAuthenticationError as e:
                    sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                    _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                    state.add_log("qBittorrent authentication failed; check the configured username and password.", "ERROR")
                except QbitClientError as e:
                    sleep_seconds, retry_index = _backoff(retry_index)
                    _set_next_check(sleep_seconds, f"qBittorrent is unavailable: {e}")

                if connection_ready:
                    state.daemon_active = True
                    # A switch to the direct engine asks for fresh feeds first.
                    needs_rss_refresh = state.consume_rss_refresh_request() or needs_rss_refresh
                    supervisor = Supervisor(session=session, qbit=qbit, anilist=anilist, settings=settings)
                    use_feed_cache = (settings.download_mode or "rules") == "direct"
                    cycle_forced = needs_rss_refresh
                    feeds_seconds = 0
                    holding_slot = False
                    try:
                        if use_feed_cache:
                            # Waiting for qBittorrent's feeds to settle is the slow part of a
                            # check and touches none of our data, so it happens before the
                            # slot is taken; the check then reads the copy in the database.
                            feeds_started = time.monotonic()
                            await asyncio.to_thread(ingest_rss, engine, qbit, needs_rss_refresh)
                            feeds_seconds = int(time.monotonic() - feeds_started)
                            needs_rss_refresh = False
                        if not state.try_begin_cycle("background"):
                            # A manual cycle or show edit holds the slot. Wait for it
                            # briefly instead of re-probing and logging every second.
                            if not busy_logged and state.cycle_owner != "settle":
                                state.add_log("Another supervision or show mutation is already in progress; waiting for it to finish.", "INFO")
                                busy_logged = True
                            await asyncio.sleep(BUSY_RETRY_SECONDS)
                            continue
                        holding_slot = True
                        busy_logged = False
                        state.add_log("Executing background supervision check...", "INFO")
                        cycle_started = time.monotonic()
                        logs = await supervisor.run_full_cycle(
                            hunting=hunting_next,
                            force_rss_refresh=cycle_forced,
                            feed_cache=use_feed_cache,
                        )
                        needs_rss_refresh = False
                        state.last_cycle_time = datetime.now(timezone.utc)
                        retry_index = 0
                        cycle_seconds = int(time.monotonic() - cycle_started)
                        if cycle_seconds >= 10 or feeds_seconds >= 10:
                            state.add_log(
                                f"Supervision check took {cycle_seconds}s (feeds ready in {feeds_seconds}s, before it started).",
                                "INFO",
                            )
                        for l in logs:
                            state.add_log(f"Supervisor: {l}", "INFO")
                        if not logs:
                            state.add_log("Supervisor: All shows and rules up to date.", "INFO")
                    except QbitAuthenticationError as e:
                        connection_ready = False
                        needs_rss_refresh = True
                        sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                        _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                    except QbitRSSRefreshError as e:
                        needs_rss_refresh = True
                        deferred_reason = f"qBittorrent RSS refresh failed; direct evaluation deferred: {e}"
                        sleep_seconds = min(_next_qbit_retry(retry_index), 60)
                    except QbitClientError as e:
                        connection_ready = False
                        needs_rss_refresh = True
                        sleep_seconds, retry_index = _backoff(retry_index)
                        _set_next_check(sleep_seconds, f"qBittorrent became unavailable during the cycle: {e}")
                    except Exception as e:
                        needs_rss_refresh = True
                        state.add_log(f"Supervisor cycle error: {e}", "ERROR")
                        logger.error(f"Supervisor error: {e}", exc_info=True)
                    finally:
                        if holding_slot:
                            state.end_cycle("background")

                    if connection_ready:
                        default_interval = max(60, settings.refresh_interval_minutes * 60)
                        try:
                            sleep_seconds, reason = await asyncio.to_thread(
                                calculate_next_poll_interval,
                                session,
                                default_interval_seconds=default_interval,
                                qbit_client=qbit,
                                download_mode=settings.download_mode,
                                backfill_window_days=settings.backfill_window_days,
                                early_air_tolerance_hours=settings.early_air_tolerance_hours,
                            )
                        except QbitAuthenticationError as e:
                            connection_ready = False
                            sleep_seconds = QBIT_AUTH_RETRY_SECONDS
                            _set_next_check(sleep_seconds, f"qBittorrent authentication failed: {e}")
                        except QbitClientError as e:
                            connection_ready = False
                            sleep_seconds, retry_index = _backoff(retry_index)
                            _set_next_check(sleep_seconds, f"qBittorrent became unavailable while scheduling the next check: {e}")
                        else:
                            hunting_next = await asyncio.to_thread(
                                is_hunting,
                                session,
                                download_mode=settings.download_mode,
                                backfill_window_days=settings.backfill_window_days,
                                early_air_tolerance_hours=settings.early_air_tolerance_hours,
                            )
                            _set_next_check(sleep_seconds, deferred_reason or reason)
                else:
                    state.daemon_active = False

            state.wake_event.clear()
            try:
                await asyncio.wait_for(state.wake_event.wait(), timeout=sleep_seconds)
                if not connection_ready:
                    state.add_log("Reconnect wait interrupted by manual WebUI trigger; re-probing qBittorrent.", "INFO")
                else:
                    state.add_log("Supervisor woke up early from manual WebUI trigger.", "INFO")
            except asyncio.TimeoutError:
                pass

        except asyncio.CancelledError:
            state.add_log("Background supervisor stopped.", "INFO")
            break
        except Exception as e:
            state.add_log(f"Unexpected error in background task: {e}", "ERROR")
            logger.error(f"Scheduler loop error: {e}", exc_info=True)
            await asyncio.sleep(60)


def _settle_once(engine) -> Optional[List[str]]:
    """Check in-flight downloads against qBittorrent once; None when there was nothing to do.

    Runs on a thread: it opens its own session and holds the cycle slot only
    while it works, and skips the round if a cycle or request has the slot.
    """
    with Session(engine) as session:
        settings = get_settings(session)
        if (settings.download_mode or "rules") != "direct":
            return None
        if not has_unsettled_work(session) or not state.try_begin_cycle("settle"):
            return None
        try:
            qbit = QBitClient(
                host=settings.qbit_host,
                username=settings.qbit_username,
                password=settings.qbit_password,
                timeout=10,
            )
            return update_episode_status(session, qbit, settings)
        finally:
            state.end_cycle("settle")


async def settle_task():
    """Pick up torrents qBittorrent lists late and mark finished ones complete.

    The full cycle only runs every so often, so a torrent that qBittorrent was
    still fetching when it was added, or one that has since finished, would
    otherwise wait for it.
    """
    engine = get_engine()
    await asyncio.sleep(STARTUP_GRACE_SECONDS)
    while True:
        delay = SETTLE_INTERVAL_SECONDS
        try:
            logs = await asyncio.to_thread(_settle_once, engine)
            for line in logs or []:
                state.add_log(f"Supervisor: {line}", "INFO")
        except asyncio.CancelledError:
            raise
        except QbitClientError as e:
            logger.debug(f"Settle check skipped, qBittorrent unavailable: {e}")
            delay = SETTLE_ERROR_SECONDS
        except Exception as e:
            logger.warning(f"Settle check failed: {e}", exc_info=True)
            delay = SETTLE_ERROR_SECONDS
        await asyncio.sleep(delay)


async def ingest_task():
    """Keep the feed cache current between checks.

    Checks, the feed list and discovery read the copy in the database, so this
    is the only place that waits on qBittorrent's RSS feeds.
    """
    engine = get_engine()
    await asyncio.sleep(STARTUP_GRACE_SECONDS)
    try:
        moved = await asyncio.to_thread(backfill_cache, engine)
        if moved:
            logger.info(f"Moved {moved} cached feed item(s) to the matched items table.")
    except Exception as e:
        logger.warning(f"Could not move the old feed cache: {e}", exc_info=True)
    last_prune = float("-inf")
    while True:
        delay = INGEST_INTERVAL_SECONDS
        try:
            with Session(engine) as session:
                settings = get_settings(session)
                qbit = QBitClient(
                    host=settings.qbit_host,
                    username=settings.qbit_username,
                    password=settings.qbit_password,
                    timeout=10,
                )
                direct = (settings.download_mode or "rules") == "direct"
            if direct:
                await asyncio.to_thread(ingest_rss, engine, qbit, False)
                if time.monotonic() - last_prune >= PRUNE_INTERVAL_SECONDS:
                    removed = await asyncio.to_thread(prune_cache, engine)
                    last_prune = time.monotonic()
                    if removed:
                        logger.info(f"Pruned {removed} old feed cache item(s).")
        except asyncio.CancelledError:
            raise
        except QbitClientError as e:
            logger.debug(f"Feed cache update skipped, qBittorrent unavailable: {e}")
            delay = INGEST_ERROR_SECONDS
        except Exception as e:
            logger.warning(f"Feed cache update failed: {e}", exc_info=True)
            delay = INGEST_ERROR_SECONDS
        await asyncio.sleep(delay)


async def qbit_rule_observer_task():
    """Watch qBittorrent's rule state and record newly accepted releases.

    Only relevant in rules mode: the direct engines own their downloads and keep
    no RSS rules, so there is nothing for the rule log to confirm.
    """
    engine = get_engine()

    while True:
        try:
            with Session(engine) as session:
                settings = get_settings(session)
                if (settings.download_mode or "rules") != "rules":
                    await _wait_for_log_wakeup()
                    continue

                qbit = QBitClient(
                    host=settings.qbit_host,
                    username=settings.qbit_username,
                    password=settings.qbit_password,
                    timeout=10,
                )

                markers = await asyncio.to_thread(qbit.get_rule_match_markers)
                if not markers:
                    logger.debug("No RSS rules to observe yet.")
                    await _wait_for_log_wakeup()
                    continue

                stored = {
                    w.rule_name: w.last_match
                    for w in session.exec(select(QbitRuleWatermark)).all()
                }
                if any(stored.get(name) != value for name, value in markers.items()):
                    entries = await asyncio.to_thread(qbit.fetch_log_entries)
                    patterns = await asyncio.to_thread(qbit.get_rule_patterns)
                    for message in ingest_log_acceptances(session, entries, patterns):
                        state.add_log(message, "INFO")

                    for name, value in markers.items():
                        watermark = session.get(QbitRuleWatermark, name)
                        if watermark is None:
                            watermark = QbitRuleWatermark(rule_name=name)
                            session.add(watermark)
                        watermark.last_match = value
                        watermark.updated_at = datetime.now(timezone.utc)
                    session.commit()

        except QbitClientError as e:
            state.add_log(f"Rule check skipped: {e}", "WARNING")
        except Exception as e:
            state.add_log(f"Rule check error: {e}", "ERROR")
            logger.error(f"Rule observer error: {e}", exc_info=True)

        await _wait_for_log_wakeup()


async def _wait_for_log_wakeup() -> None:
    try:
        await asyncio.wait_for(state.log_wake_event.wait(), timeout=RULE_OBSERVER_INTERVAL_SECONDS)
    except asyncio.TimeoutError:
        pass
    finally:
        state.log_wake_event.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    init_db(engine)
    state.bind_loop(asyncio.get_running_loop())

    bg_task = asyncio.create_task(background_supervisor_task())
    observer_task = asyncio.create_task(qbit_rule_observer_task())
    settle = asyncio.create_task(settle_task())
    ingest = asyncio.create_task(ingest_task())
    yield
    for task in (bg_task, observer_task, settle, ingest):
        task.cancel()
    for task in (bg_task, observer_task, settle, ingest):
        try:
            await task
        except asyncio.CancelledError:
            pass


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _request_host(host_header: str) -> str:
    """The hostname of a Host header, without port or IPv6 brackets."""
    return urlsplit(f"//{host_header}").hostname or ""


def _host_allowed(host_header: str) -> bool:
    """Whether the Host header names this machine rather than a rebound domain.

    DNS rebinding points an attacker's domain at the loopback address, so the
    browser sends that domain as Host. IP literals, localhost and this
    machine's own hostname are accepted; KISETSU_ALLOWED_HOSTS (comma
    separated) adds names for reverse proxies or a LAN hostname.
    """
    try:
        host = _request_host(host_header).lower()
    except ValueError:
        return False
    if not host or host == "localhost" or host.endswith(".localhost"):
        return bool(host)
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    extra = {name.strip().lower() for name in os.environ.get("KISETSU_ALLOWED_HOSTS", "").split(",") if name.strip()}
    return host in extra or host == socket.gethostname().lower()


def create_app() -> FastAPI:
    app = FastAPI(title="Kisetsu", lifespan=lifespan)
    app.include_router(router)

    @app.middleware("http")
    async def reject_foreign_hosts(request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header):
            return JSONResponse({"detail": "Host not allowed."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and request.method not in _SAFE_METHODS and urlsplit(origin).netloc != host_header:
            return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # The UI is a single generated page that changes with every deploy, so it
        # must never be served from a browser cache.
        return get_web_ui_html(headers={"Cache-Control": "no-store, must-revalidate"})

    return app


app = create_app()
