"""Independent timers for the engine's jobs: each job is its own task, never runs twice at once, and can be poked."""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("kisetsu.engine.scheduler")

# A job that raised is tried again after this many seconds.
ERROR_RETRY_SECONDS = 60.0

JobFn = Callable[[], Awaitable[Optional[float]]]


class Job:
    def __init__(self, name: str, fn: JobFn, interval: Optional[float], first: Optional[float]):
        self.name = name
        self.fn = fn
        self.interval = interval
        self.due: Optional[float] = first
        self.running = False
        self.rerun_at: Optional[float] = None
        self.wake = asyncio.Event()
        self.last_error: Optional[str] = None
        self.runs = 0


class Scheduler:
    """Runs each registered job when it is due. A job returns the seconds until its next run, or None for its default interval."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.jobs: Dict[str, Job] = {}
        self._tasks: List[asyncio.Task] = []
        self._stopping = False
        # Told ``(name, running)`` whenever a job starts or ends.
        self.on_change: Callable[[str, bool], None] = lambda name, running: None

    def add(self, name: str, fn: JobFn, *, interval: Optional[float], first: Optional[float] = 0.0) -> Job:
        """Register a job. ``first`` is the delay before its first run (None: only when poked)."""
        job = Job(name, fn, interval, None if first is None else self.clock() + first)
        self.jobs[name] = job
        return job

    def poke(self, name: str, delay: float = 0.0) -> None:
        """Run the job soon. A poke during a run makes it run once more afterwards."""
        job = self.jobs.get(name)
        if job is None:
            return
        at = self.clock() + max(0.0, delay)
        if job.running:
            job.rerun_at = at if job.rerun_at is None else min(job.rerun_at, at)
        else:
            job.due = at if job.due is None else min(job.due, at)
        job.wake.set()

    def delay(self, name: str, seconds: float) -> None:
        """Move the job's next run to ``seconds`` from now, later or earlier."""
        job = self.jobs.get(name)
        if job is not None:
            job.due = self.clock() + seconds
            job.wake.set()

    def start(self) -> None:
        self._stopping = False
        self._tasks = [asyncio.create_task(self._loop(job), name=f"job-{job.name}") for job in self.jobs.values()]

    async def stop(self) -> None:
        self._stopping = True
        for job in self.jobs.values():
            job.wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def _loop(self, job: Job) -> None:
        while not self._stopping:
            if job.due is None:
                await job.wake.wait()
                job.wake.clear()
                continue
            wait = job.due - self.clock()
            if wait > 0:
                try:
                    await asyncio.wait_for(job.wake.wait(), wait)
                except asyncio.TimeoutError:
                    pass
                job.wake.clear()
                continue
            job.running = True
            job.due = None
            self.on_change(job.name, True)
            try:
                result = await job.fn()
                job.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Job '{job.name}' failed: {e}", exc_info=True)
                job.last_error = str(e)
                result = ERROR_RETRY_SECONDS
            finally:
                job.running = False
                self.on_change(job.name, False)
            job.runs += 1
            seconds = result if result is not None else job.interval
            job.due = None if seconds is None else self.clock() + seconds
            if job.rerun_at is not None:
                job.due = job.rerun_at if job.due is None else min(job.due, job.rerun_at)
                job.rerun_at = None
