"""What a request handler reaches the engine through, and how service errors become HTTP errors."""

from dataclasses import dataclass
from typing import Any, Callable, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kisetsu.clients.qbittorrent import Qbit, QbitError
from kisetsu.db.schema import Feed, Show
from kisetsu.engine.jobs import Jobs
from kisetsu.engine.runner import Engine
from kisetsu.engine.scheduler import Scheduler
from kisetsu.services.feeds import FeedLockedError
from kisetsu.web.logs import LogRing


class NotFound(LookupError):
    pass


def probe_qbit(host: str, username: str, password: str) -> Dict[str, str]:
    """Log in once with these credentials and report qBittorrent's versions."""
    client = Qbit(host, username, password)
    try:
        return client.versions()
    finally:
        client.close()


@dataclass
class Runtime:
    engine: Engine
    scheduler: Scheduler
    jobs: Jobs
    anilist: Any
    logs: LogRing
    sandbox: bool = False
    probe: Callable[[str, str, str], Dict[str, str]] = probe_qbit


def runtime(request: Request) -> Runtime:
    return request.app.state.runtime


async def read(request: Request, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await runtime(request).engine.read(fn, *args, **kwargs)


async def write(request: Request, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await runtime(request).engine.run(fn, *args, **kwargs)


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(NotFound)
    async def not_found(_request, e: NotFound):
        return JSONResponse({"detail": str(e) or "Not found."}, status_code=404)

    @app.exception_handler(FeedLockedError)
    async def locked(_request, e: FeedLockedError):
        return JSONResponse({"detail": str(e), "code": "feed_locked"}, status_code=409)

    @app.exception_handler(QbitError)
    async def qbit(_request, e: QbitError):
        return JSONResponse({"detail": f"qBittorrent: {e}"}, status_code=502)

    @app.exception_handler(ValueError)
    async def invalid(_request, e: ValueError):
        return JSONResponse({"detail": str(e)}, status_code=400)


def show_or_404(ctx, show_id: int):
    show = ctx.session.get(Show, show_id)
    if show is None:
        raise NotFound("Show not found.")
    return show


def feed_or_404(ctx, feed_id: int):
    feed = ctx.session.get(Feed, feed_id)
    if feed is None:
        raise NotFound("Feed not found.")
    return feed

