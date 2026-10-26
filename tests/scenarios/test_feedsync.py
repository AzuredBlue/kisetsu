from datetime import timedelta

from kisetsu.clients.qbittorrent import RssStoreFeed
from kisetsu.db.schema import FeedState
from kisetsu.services import feedsync
from tests.fakes import START, article

URL = "https://top/rss"


def test_every_feed_is_fetched_by_kisetsu_in_priority_order_whatever_qbit_is_set_to(world):
    low = world.feed("Low", "https://low/rss", 2)
    top = world.feed("Top", URL, 1)
    world.qbit.rss.append(RssStoreFeed(path="Top", url=URL))
    assert [r.id for r in feedsync.plan_fetch(world.ctx())] == [top.id, low.id]
    assert not [name for name, _ in world.qbit.calls if name.startswith('rss')]


def test_a_successful_read_stores_items_and_marks_the_feed_healthy_and_a_failure_starts_the_clock(world):
    feed = world.feed("Top", URL, 1)
    world.show(episodes=1, first_air=START - timedelta(days=1))
    out = feedsync.apply_fetch(world.ctx(), {feed.id: [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)]})
    world.commit()
    assert out.new == 1 and len(out.matched_show_ids) == 1 and not out.gaps
    assert (feed.last_ok_at, feed.failing_since) == (START, None)
    world.clock.advance(minutes=15)
    out = feedsync.apply_fetch(world.ctx(), {feed.id: "top.example did not answer in time"})
    assert out.failed == {feed.id: "top.example did not answer in time"} and feed.failing_since == world.clock()
    assert feed.last_ok_at == START
    world.clock.advance(minutes=15)
    feedsync.apply_fetch(world.ctx(), {feed.id: []})
    assert feed.failing_since is None and feed.last_error is None


def test_a_read_with_nothing_in_common_with_the_stored_items_is_a_gap(world):
    feed = world.feed("Top", URL, 1)
    world.show(episodes=1, first_air=START - timedelta(days=1))
    first = [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)]
    assert feedsync.apply_fetch(world.ctx(), {feed.id: first}).gaps == []
    assert feedsync.apply_fetch(world.ctx(), {feed.id: first}).gaps == []
    later = [article("[Grp] Sousou no Frieren - 05 (1080p).mkv", START), article("[Grp] Sousou no Frieren - 06 (1080p).mkv", START)]
    assert feedsync.apply_fetch(world.ctx(), {feed.id: later}).gaps == [feed.id]
    assert feedsync.apply_fetch(world.ctx(), {feed.id: later}).gaps == []


def test_hunting_reads_only_the_feeds_the_hunted_shows_may_use(world):
    top = world.feed("Top", URL, 1)
    low = world.feed("Low", "https://low/rss", 2)
    world.show(1, "Locked", episodes=1, first_air=START - timedelta(hours=2), feed=low, feed_state=FeedState.LOCKED)
    info = feedsync.plan_hunt(world.ctx())
    assert [r.id for r in info.feeds] == [low.id] and len(info.show_ids) == 1
    world.show(2, "Free", episodes=1, first_air=START - timedelta(hours=1))
    assert {r.id for r in feedsync.plan_hunt(world.ctx()).feeds} == {top.id, low.id}


def test_with_nothing_airing_the_plan_says_when_hunting_starts(world):
    world.feed("Top", URL, 1)
    world.show(episodes=2, first_air=START + timedelta(days=2))
    info = feedsync.plan_hunt(world.ctx())
    assert info.show_ids == [] and info.plan.next_start == START + timedelta(days=2) - timedelta(hours=world.ctx().tolerance_hours)
