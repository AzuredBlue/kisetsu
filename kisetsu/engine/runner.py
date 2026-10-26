"""The engine: one thread for everything that touches the database or qBittorrent, and the change bus for the UI."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional, Set, Tuple

from sqlalchemy.engine import Engine as SqlEngine
from sqlmodel import Session

from kisetsu.clients.qbittorrent import Qbit
from kisetsu.services.context import Caches, Context, make_context

logger = logging.getLogger("kisetsu.engine")

# How many changes a slow subscriber may lag behind before it is told to refetch everything.
SUBSCRIBER_BACKLOG = 64


class ChangeBus:
    """Tells the UI which of its views changed. Publishing is safe from any thread."""

    def __init__(self) -> None:
        self.rev = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: Set[asyncio.Queue] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, topics: Iterable[str]) -> None:
        names = sorted(set(topics))
        if not names or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._deliver, names)
        except RuntimeError:
            pass

    def _deliver(self, names: list) -> None:
        self.rev += 1
        message = {"changed": names, "rev": self.rev}
        for queue in list(self._subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"changed": ["*"], "rev": self.rev})
            else:
                queue.put_nowait(message)

    def open_queue(self) -> asyncio.Queue:
        """A queue of the changes from now on; ``None`` in it means the bus is shutting down."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self._subscribers.add(queue)
        return queue

    def close_queue(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    async def subscribe(self) -> AsyncIterator[Dict[str, Any]]:
        queue = self.open_queue()
        try:
            while True:
                message = await queue.get()
                if message is None:
                    return
                yield message
        finally:
            self.close_queue(queue)

    def shutdown(self) -> None:
        """End every subscription, so open event streams do not hold the server up."""
        for queue in list(self._subscribers):
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(None)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _NoQbit:
    """Stands in for qBittorrent in a read, so a view can never reach it."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("A read must not call qBittorrent.")


class Engine:
    """Runs service functions on one thread, each in its own short session.

    ``await engine.run(fn, *args)`` calls ``fn(ctx, *args)`` with a Context, commits when it
    returns and publishes the topics it touched. An error rolls the session back and drops the
    in-memory caches, which may hold rows that never reached the database.
    """

    def __init__(
        self,
        db_engine: SqlEngine,
        *,
        notify: Optional[Callable[[str, str, Optional[str]], None]] = None,
        clock: Callable[[], datetime] = _utcnow,
        qbit_factory: Optional[Callable[..., Any]] = None,
        read_only: bool = False,
    ) -> None:
        self.db = db_engine
        self.bus = ChangeBus()
        self.caches = Caches()
        self.notify = notify
        self.clock = clock
        self.read_only = read_only
        self._qbit_factory = qbit_factory or Qbit
        self._qbit: Optional[Any] = None
        self._qbit_key: Optional[Tuple] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kisetsu-engine")

    async def start(self) -> None:
        self.bus.bind(asyncio.get_running_loop())

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(self.call, fn, *args, **kwargs))

    async def read(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a read-only ``fn(ctx, ...)`` beside the engine thread; nothing it does is saved and qBittorrent is out of reach."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(self._read, fn, *args, **kwargs))

    def _read(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with Session(self.db, expire_on_commit=False) as session:
            try:
                ctx = make_context(session, _NoQbit(), self.clock(), Caches())
                return fn(ctx, *args, **kwargs)
            finally:
                session.rollback()

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` the way ``run`` does, on the calling thread. Only the engine thread may call it."""
        with Session(self.db, expire_on_commit=False) as session:
            ctx = make_context(session, None, self.clock(), self.caches, notify=self.notify)
            ctx.qbit = self._qbit_for(ctx)
            try:
                result = fn(ctx, *args, **kwargs)
                session.commit()
            except BaseException:
                session.rollback()
                self.caches.known_items = None
                self.caches.matchers = None
                self.bus.publish(ctx.topics)
                raise
        self.bus.publish(ctx.topics)
        return result

    def _qbit_for(self, ctx: Context) -> Any:
        s = ctx.settings
        key = (s.qbit_host, s.qbit_username, s.qbit_password, self.read_only)
        if key != self._qbit_key:
            if self._qbit is not None:
                self._qbit.close()
            self._qbit = self._qbit_factory(s.qbit_host, s.qbit_username, s.qbit_password, read_only=self.read_only)
            self._qbit_key = key
        return self._qbit

    async def close(self) -> None:
        def shut() -> None:
            if self._qbit is not None:
                self._qbit.close()
                self._qbit = None

        await asyncio.get_running_loop().run_in_executor(self._executor, shut)
        self._executor.shutdown(wait=True)
