import logging

from kisetsu.web.logs import RING_SIZE, LogRing


def ring_with(count: int) -> LogRing:
    ring = LogRing()
    logger = logging.getLogger("kisetsu.test_logs")
    logger.addHandler(ring)
    logger.setLevel(logging.INFO)
    try:
        for n in range(1, count + 1):
            logger.info(f"line {n}")
    finally:
        logger.removeHandler(ring)
    return ring


def messages(answer):
    return [line["message"] for line in answer["lines"]]


def test_only_the_lines_after_the_newest_one_held_are_sent():
    ring = ring_with(5)
    first = ring.recent(3)
    assert messages(first) == ["line 3", "line 4", "line 5"] and first["full"]
    answer = ring.recent(3, after=3, run=first["run"])
    assert messages(answer) == ["line 4", "line 5"] and not answer["full"]


def test_lines_from_another_run_are_replaced_by_this_runs():
    ring = ring_with(2)
    answer = ring.recent(10, after=1, run="an earlier run")
    assert messages(answer) == ["line 1", "line 2"] and answer["full"]


def test_a_caller_that_fell_behind_the_ring_gets_the_whole_list_again():
    ring = ring_with(RING_SIZE + 10)
    answer = ring.recent(3, after=5, run=ring.run)
    assert answer["full"] and messages(answer)[-1] == f"line {RING_SIZE + 10}"
