from kisetsu.clients import traffic


def test_each_counted_request_tells_whoever_shows_the_counts():
    heard = []
    traffic.on_change = lambda: heard.append(1)
    try:
        traffic.record("Search", "nyaa.si")
        traffic.record("Search", "nyaa.si", traffic.RATE_LIMITED)
    finally:
        traffic.on_change = None
    traffic.record("Search", "nyaa.si")
    assert len(heard) == 2
