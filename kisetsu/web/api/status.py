import asyncio
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func
from sqlmodel import select

from kisetsu.clients import traffic
from kisetsu.clients.anilist import rate_limit_status
from kisetsu.db.schema import Download, Event, Feed
from kisetsu.services.context import get_state, set_state
from kisetsu.services.cycle import LAST_SYNC_KEY
from kisetsu.web import views
from kisetsu.web.deps import NotFound, read, runtime, write

router = APIRouter(prefix="/api")

KEEPALIVE_SECONDS = 30
HISTORY_LIMIT_MAX = 500


def _totals(ctx):
    return {
        **views.counts_view(ctx),
        "last_feed_read_at": views.iso(ctx.session.exec(select(func.max(Feed.last_fetch_at))).one()),
        # For the first-run checklist: has AniList ever been read, and has anything ever been downloaded.
        "anilist_synced_at": get_state(ctx, LAST_SYNC_KEY) or None,
        "any_download": ctx.session.exec(select(Download.id).limit(1)).first() is not None,
    }


def _clear_history(ctx):
    ctx.session.execute(delete(Event).where(Event.kind.in_(views.HISTORY_KINDS)))
    ctx.emit("history")


def _live(request: Request):
    rt = runtime(request)
    now = rt.scheduler.clock()
    # The routine read wakes only when it has a feed to read; the hunt's own wake-ups are rechecks, so its next real read counts.
    refresh = rt.scheduler.jobs.get("refresh_feeds")
    dues = [refresh.due - now] if refresh is not None and refresh.due is not None else []
    if rt.jobs.hunt_read_at is not None:
        dues.append((rt.jobs.hunt_read_at - rt.engine.clock()).total_seconds())
    blocked_until = rate_limit_status()["blocked_until"]
    return {
        "ready": rt.jobs.ready.is_set(),
        "sandbox": rt.sandbox,
        "running": sorted(name for name, job in rt.scheduler.jobs.items() if job.running),
        "next_feed_read_in": max(0, int(min(dues))) if dues else None,
        "qbit_error": rt.jobs.health.get("qbit"),
        "anilist_error": rt.jobs.health.get("anilist"),
        "grab_error": rt.jobs.health.get("grab"),
        # Background jobs whose last run raised; each is retried a minute later.
        "jobs_failing": {name: job.last_error for name, job in rt.scheduler.jobs.items() if job.last_error},
        "anilist_blocked_until": None if not blocked_until else datetime.fromtimestamp(blocked_until, tz=timezone.utc).isoformat(),
        "rev": rt.engine.bus.rev,
    }


@router.get("/status")
async def get_status(request: Request):
    body = {**await read(request, _totals), **_live(request)}
    if body["qbit_error"]:
        # Adds can't be judged while qBittorrent is unreachable; that chip already says so.
        body["stuck_adds"] = 0
    return body


class Dismiss(BaseModel):
    keys: List[str]


def _dismiss_attention(ctx, keys: List[str]) -> None:
    current = {key for row in views.attention_view(ctx, include_dismissed=True) for key in row["keys"]}
    closing = current.intersection(keys)
    if not closing:
        raise NotFound("That no longer needs attention.")
    # Keys of rows that are gone are dropped, so the list only ever holds what it still hides.
    kept = (views.dismissed_attention(ctx) & current) | closing
    set_state(ctx, views.ATTENTION_DISMISSED_KEY, json.dumps(sorted(kept)))
    ctx.emit("shows")


@router.get("/attention")
async def get_attention(request: Request):
    return await read(request, views.attention_view)


@router.post("/attention/dismiss")
async def dismiss_attention(request: Request, body: Dismiss):
    await write(request, _dismiss_attention, body.keys)
    return {"status": "success"}


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100, after: Optional[int] = None, run: Optional[str] = None):
    return runtime(request).logs.recent(limit, after, run)


@router.get("/requests")
async def get_requests():
    """The requests sent to each site since start (or the last reset), by kind."""
    return traffic.snapshot()


@router.delete("/requests")
async def reset_requests():
    traffic.reset()
    return {"status": "success"}


@router.get("/history")
async def get_history(request: Request, limit: int = 100):
    return await read(request, views.history_view, max(1, min(limit, HISTORY_LIMIT_MAX)))


@router.delete("/history")
async def clear_history(request: Request):
    await write(request, _clear_history)
    return {"status": "success"}


@router.post("/refresh")
async def refresh(request: Request):
    jobs = runtime(request).jobs
    jobs.request_refresh()
    jobs.poke("sync_anilist")
    return {"status": "queued"}


def _message(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.get("/stream")
async def stream(request: Request):
    bus = runtime(request).engine.bus

    async def events():
        queue = bus.open_queue()
        try:
            yield "retry: 3000\n\n"
            # A client that connects (or reconnects) refetches everything once, so it never misses a change.
            yield _message({"changed": ["*"], "rev": bus.rev})
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if message is None:
                    return
                yield _message(message)
        finally:
            bus.close_queue(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
