import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func
from sqlmodel import select

from kisetsu.clients.anilist import rate_limit_status
from kisetsu.db.schema import Event, Feed
from kisetsu.web import views
from kisetsu.web.deps import read, runtime, write

router = APIRouter(prefix="/api")

KEEPALIVE_SECONDS = 30
HISTORY_LIMIT_MAX = 500


def _totals(ctx):
    return {**views.counts_view(ctx), "last_feed_read_at": views.iso(ctx.session.exec(select(func.max(Feed.last_fetch_at))).one())}


def _clear_history(ctx):
    ctx.session.execute(delete(Event).where(Event.kind.in_(views.HISTORY_KINDS)))
    ctx.emit("history")


def _live(request: Request):
    rt = runtime(request)
    now = rt.scheduler.clock()
    dues = [job.due for job in (rt.scheduler.jobs.get("refresh_feeds"), rt.scheduler.jobs.get("hunt")) if job is not None and job.due is not None]
    blocked_until = rate_limit_status()["blocked_until"]
    return {
        "ready": rt.jobs.ready.is_set(),
        "sandbox": rt.sandbox,
        "running": sorted(name for name, job in rt.scheduler.jobs.items() if job.running),
        "next_feed_read_in": max(0, int(min(dues) - now)) if dues else None,
        "qbit_error": rt.jobs.health.get("qbit"),
        "anilist_error": rt.jobs.health.get("anilist"),
        "anilist_blocked_until": None if not blocked_until else datetime.fromtimestamp(blocked_until, tz=timezone.utc).isoformat(),
        "rev": rt.engine.bus.rev,
    }


@router.get("/status")
async def get_status(request: Request):
    return {**await read(request, _totals), **_live(request)}


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100):
    return runtime(request).logs.recent(limit)


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
