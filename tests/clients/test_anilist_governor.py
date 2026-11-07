import asyncio

import pytest

from kisetsu.clients import anilist
from kisetsu.clients.anilist import AniListRateLimited, RateGovernor


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def governed(monkeypatch):
    clock = Clock()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(anilist.asyncio, "sleep", sleep)
    return RateGovernor(clock=clock, wall=clock), clock, slept


def test_a_burst_within_the_minutes_budget_goes_out_a_short_gap_apart(governed):
    governor, clock, slept = governed
    for _ in range(10):
        asyncio.run(governor.acquire())
    assert slept == [anilist.MIN_REQUEST_GAP_SECONDS] * 9


def test_no_minute_holds_more_than_the_budget(governed):
    governor, clock, slept = governed
    governor.limit = 8  # a budget of 3 a minute
    sent = []
    for _ in range(3):
        asyncio.run(governor.acquire())
        sent.append(clock.now)
    # The fourth would have to wait for the first to be a minute old, longer than a request may wait.
    with pytest.raises(AniListRateLimited):
        asyncio.run(governor.acquire())
    for at in (57.0, 59.0, 61.0, 62.0, 100.0, 118.0):
        clock.now = max(clock.now, at)
        try:
            asyncio.run(governor.acquire())
            sent.append(clock.now)
        except AniListRateLimited:
            pass
    assert len(sent) > 3
    assert all(sum(1 for t in sent if start <= t < start + 60.0) <= 3 for start in sent)


def test_a_request_just_short_of_the_window_waits_for_it(governed):
    governor, clock, slept = governed
    governor.limit = 7  # a budget of 2 a minute
    asyncio.run(governor.acquire())
    asyncio.run(governor.acquire())
    clock.now = 58.0
    asyncio.run(governor.acquire())
    assert slept[-1] == pytest.approx(2.0) and clock.now == pytest.approx(60.0)
