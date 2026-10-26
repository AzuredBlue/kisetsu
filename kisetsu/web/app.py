"""The web app: builds the engine, the scheduler and the jobs, and serves the API and the UI."""

import asyncio
import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, List, Optional

import anyio.to_thread
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from kisetsu import config
from kisetsu.clients import rss
from kisetsu.clients.anilist import AniListClient
from kisetsu.clients.notify import notify
from kisetsu.db import store
from kisetsu.engine.instance import InstanceLock
from kisetsu.engine.jobs import Jobs
from kisetsu.engine.runner import Engine
from kisetsu.engine.scheduler import Scheduler
from kisetsu.web import security
from kisetsu.web.api import feeds, settings, setup, shows, status
from kisetsu.web.deps import Runtime, install_errors
from kisetsu.web.logs import LogRing

logger = logging.getLogger("kisetsu.web")

STATIC_DIR = Path(__file__).parent / "static"
# Blocking work that is not the engine's: file scans and the like.
EXECUTOR_THREADS = 2
# FastAPI's own threadpool, for the few handlers that are not async.
THREADPOOL_TOKENS = 4
HASH_MARK = re.compile(r"@@v:([\w./-]+)@@")
# The jobs whose progress the header shows; the others run too often to be worth a message.
VISIBLE_JOBS = {"startup", "refresh_feeds", "hunt", "sync_anilist", "backfill"}
# Called when the server is told to stop, before it waits for open connections: an event
# stream never ends by itself, so it has to be closed first.
shutdown_hooks: List[Callable[[], None]] = []


class HashedStatic(StaticFiles):
    """Files are served for a year when the URL carries their hash, and revalidated otherwise."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return response


def stamped_index(static_dir: Path) -> HTMLResponse:
    """index.html with every ``@@v:path@@`` replaced by a hash of that file, so a deploy busts the browser cache."""
    html = (static_dir / "index.html").read_text(encoding="utf-8")

    def digest(match: re.Match) -> str:
        try:
            return hashlib.sha1((static_dir / match.group(1)).read_bytes()).hexdigest()[:10]
        except OSError:
            return "0"

    return HTMLResponse(HASH_MARK.sub(digest, html), headers={"Cache-Control": "no-store, must-revalidate"})


def build_runtime(*, sandbox: bool) -> Runtime:
    """The real thing: the configured database, qBittorrent, AniList and the internet."""
    db = store.get_engine()
    logs = LogRing()
    engine = Engine(db, notify=notify, read_only=sandbox)
    logs.on_line = lambda: engine.bus.publish(["logs"])
    logging.getLogger("kisetsu").addHandler(logs)
    scheduler = Scheduler()
    anilist = AniListClient()
    http = rss.new_client()
    jobs = Jobs(engine, scheduler, anilist=anilist, http=http)
    return Runtime(engine=engine, scheduler=scheduler, jobs=jobs, anilist=anilist, logs=logs, sandbox=sandbox)


def create_app(
    *,
    runtime_factory: Optional[Callable[[], Runtime]] = None,
    sandbox: Optional[bool] = None,
    run_jobs: Optional[bool] = None,
    static_dir: Path = STATIC_DIR,
    lock_path: Optional[Path] = None,
) -> FastAPI:
    sandbox = config.SANDBOX if sandbox is None else sandbox
    run_jobs = (not sandbox) if run_jobs is None else run_jobs
    factory = runtime_factory or (lambda: build_runtime(sandbox=sandbox))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(EXECUTOR_THREADS, thread_name_prefix="kisetsu-io"))
        anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
        lock = InstanceLock(lock_path or Path(str(config.DB_PATH) + ".lock"))
        lock.acquire()
        rt = factory()
        rt.sandbox = sandbox
        app.state.runtime = rt
        await rt.engine.start()
        hook = lambda: loop.call_soon_threadsafe(rt.engine.bus.shutdown)  # noqa: E731
        shutdown_hooks.append(hook)
        # A sandbox only serves the setup wizard; nothing runs against the real qBittorrent.
        if run_jobs:
            rt.scheduler.on_change = lambda name, running: rt.engine.bus.publish(["status"]) if name in VISIBLE_JOBS else None
            rt.jobs.register()
            rt.scheduler.start()
        try:
            yield
        finally:
            if hook in shutdown_hooks:
                shutdown_hooks.remove(hook)
            rt.engine.bus.shutdown()
            await rt.scheduler.stop()
            if rt.jobs.http is not None:
                await rt.jobs.http.aclose()
            await rt.engine.close()
            logging.getLogger("kisetsu").removeHandler(rt.logs)
            store.close_engine()
            lock.release()

    app = FastAPI(title="Kisetsu", lifespan=lifespan)
    extra_hosts = {name.strip().lower() for name in os.environ.get("KISETSU_ALLOWED_HOSTS", "").split(",") if name.strip()}
    security.install(app, sandbox=sandbox, extra_hosts=extra_hosts)
    install_errors(app)
    for module in (shows, feeds, settings, setup, status):
        app.include_router(module.router)
    if static_dir.is_dir():
        app.mount("/static", HashedStatic(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return stamped_index(static_dir)

    return app
