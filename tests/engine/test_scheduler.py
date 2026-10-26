import asyncio

from kisetsu.engine.scheduler import Scheduler


def run(coro):
    return asyncio.run(coro)


def test_a_job_runs_at_its_interval_and_a_returned_delay_replaces_it():
    async def go():
        scheduler = Scheduler()
        runs = []

        async def job():
            runs.append(len(runs))
            return 0.01 if len(runs) < 3 else 5.0

        scheduler.add("a", job, interval=5.0, first=0.0)
        scheduler.start()
        await asyncio.sleep(0.15)
        await scheduler.stop()
        return runs

    assert run(go()) == [0, 1, 2]


def test_a_job_never_overlaps_itself_and_a_poke_while_running_reruns_it_once():
    async def go():
        scheduler = Scheduler()
        active, peak, runs = [0], [0], [0]

        async def job():
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            runs[0] += 1
            await asyncio.sleep(0.05)
            active[0] -= 1

        scheduler.add("a", job, interval=None, first=0.0)
        scheduler.start()
        await asyncio.sleep(0.01)
        for _ in range(5):
            scheduler.poke("a")
        await asyncio.sleep(0.3)
        await scheduler.stop()
        return peak[0], runs[0]

    assert run(go()) == (1, 2)


def test_a_failing_job_is_retried_and_does_not_stop_the_others(monkeypatch):
    import kisetsu.engine.scheduler as module

    monkeypatch.setattr(module, "ERROR_RETRY_SECONDS", 0.02)

    async def go():
        scheduler = Scheduler()
        seen = {"bad": 0, "good": 0}

        async def bad():
            seen["bad"] += 1
            raise RuntimeError("no")

        async def good():
            seen["good"] += 1

        scheduler.add("bad", bad, interval=None, first=0.0)
        scheduler.add("good", good, interval=0.02, first=0.0)
        scheduler.start()
        await asyncio.sleep(0.2)
        error = scheduler.jobs["bad"].last_error
        await scheduler.stop()
        return seen, error

    seen, error = run(go())
    assert seen["bad"] >= 3 and seen["good"] >= 3 and error == "no"


def test_a_job_with_no_interval_waits_to_be_poked_and_delay_moves_it():
    async def go():
        scheduler = Scheduler()
        runs = []

        async def job():
            runs.append(1)

        scheduler.add("a", job, interval=None, first=None)
        scheduler.start()
        await asyncio.sleep(0.05)
        before = len(runs)
        scheduler.poke("a")
        await asyncio.sleep(0.05)
        scheduler.delay("a", 0.02)
        await asyncio.sleep(0.1)
        await scheduler.stop()
        return before, len(runs)

    assert run(go()) == (0, 2)


def test_the_scheduler_reports_when_a_job_starts_and_ends():
    async def go():
        scheduler = Scheduler()
        seen = []
        scheduler.on_change = lambda name, running: seen.append((name, running))

        async def job():
            return None

        scheduler.add("work", job, interval=None, first=0.0)
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()
        return seen

    assert asyncio.run(go()) == [("work", True), ("work", False)]


def test_stopping_cancels_a_job_that_is_waiting_to_retry_and_one_in_the_middle_of_waiting():
    async def go():
        scheduler = Scheduler()
        started = asyncio.Event()

        async def retrying():
            return 60.0

        async def waiting():
            started.set()
            await asyncio.sleep(60)

        scheduler.add("retry", retrying, interval=None, first=0.0)
        scheduler.add("wait", waiting, interval=None, first=0.0)
        scheduler.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.02)
        tasks = list(scheduler._tasks)
        await asyncio.wait_for(scheduler.stop(), 1)
        return tasks, scheduler.jobs

    tasks, jobs = run(go())
    assert all(task.done() for task in tasks) and not any(job.running for job in jobs.values())
