from datetime import timedelta

import pytest
from sqlmodel import select

from kisetsu.clients.qbittorrent import RssStoreFeed
from kisetsu.db.schema import Download, DownloadState, EpisodeOffset, Feed, FeedState, OffsetSource
from kisetsu.services import feeds
from kisetsu.services.feeds import CANDIDATE_GRACE, Carrier, Evidence, FeedLockedError
from tests.fakes import START


def two_feeds(world):
    return world.feed("Top", "https://top/rss", 1), world.feed("Low", "https://low/rss", 2)


def test_the_top_ranked_feed_is_taken_at_once(world):
    top, low = two_feeds(world)
    show = world.show(episodes=1, first_air=START)
    chosen = feeds.assign(world.ctx(), show, [Carrier(low, START), Carrier(top, START)])
    assert chosen.id == top.id and show.feed_state == FeedState.ASSIGNED and show.feed_id == top.id


def test_a_lower_ranked_feed_waits_out_the_grace_from_its_publish_time(world):
    top, low = two_feeds(world)
    show = world.show(episodes=1, first_air=START)
    published = START
    assert feeds.assign(world.ctx(), show, [Carrier(low, published)]) is None
    assert show.feed_state == FeedState.CANDIDATE and show.feed_id == low.id
    world.clock.advance(seconds=CANDIDATE_GRACE.total_seconds() - 1)
    assert feeds.assign(world.ctx(), show, [Carrier(low, published)]) is None
    world.clock.advance(seconds=2)
    assert feeds.assign(world.ctx(), show, [Carrier(low, published)]).id == low.id
    assert show.feed_state == FeedState.ASSIGNED


def test_a_higher_ranked_feed_posting_late_replaces_the_candidate_at_once(world):
    top, low = two_feeds(world)
    show = world.show(episodes=1, first_air=START)
    feeds.assign(world.ctx(), show, [Carrier(low, START)])
    world.clock.advance(seconds=60)
    assert feeds.assign(world.ctx(), show, [Carrier(low, START), Carrier(top, START)]).id == top.id
    assert show.feed_state == FeedState.ASSIGNED and show.candidate_since is None


def test_no_feed_is_guessed_when_none_carries_a_release(world):
    two_feeds(world)
    show = world.show(episodes=1, first_air=START)
    assert feeds.assign(world.ctx(), show, []) is None
    assert show.feed_state == FeedState.NONE and show.feed_id is None


def test_a_feed_that_failed_for_the_show_is_not_considered(world):
    top, low = two_feeds(world)
    show = world.show(episodes=1, first_air=START)
    chosen = feeds.assign(world.ctx(), show, [Carrier(top, START), Carrier(low, START)], excluded={top.id})
    assert chosen.id == low.id


def test_an_assigned_show_moves_only_when_its_feed_does_not_carry_the_release(world):
    top, low = two_feeds(world)
    show = world.show(episodes=1, first_air=START, feed=low, feed_state=FeedState.ASSIGNED)
    assert feeds.assign(world.ctx(), show, [Carrier(low, START)]).id == low.id
    assert feeds.assign(world.ctx(), show, [Carrier(top, START)]).id == top.id


def test_pinned_and_locked_shows_are_never_moved_by_assignment(world):
    top, low = two_feeds(world)
    pinned = world.show(1, episodes=1, first_air=START, feed=low, feed_state=FeedState.PINNED)
    locked = world.show(2, "Other", episodes=1, first_air=START, feed=low, feed_state=FeedState.LOCKED)
    for show in (pinned, locked):
        assert feeds.assign(world.ctx(), show, [Carrier(top, START)]).id == low.id
        assert show.feed_id == low.id


def test_lock_needs_a_feed_and_is_refused_for_a_show_pinned_elsewhere(world):
    top, low = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.PINNED)
    assert not feeds.lock(world.ctx(), show, low, Evidence.GRAB)
    assert show.feed_state == FeedState.PINNED and show.feed_id == top.id
    assert feeds.lock(world.ctx(), show, top, Evidence.GRAB, learned_name="Frieren", learned_group="Grp")
    assert show.feed_state == FeedState.LOCKED and show.learned_name == "Frieren"


def test_moving_off_a_locked_feed_needs_confirmation_and_forgets_what_it_taught(world):
    top, low = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.LOCKED, learned_name="Frieren")
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=top.id, offset=12, source=OffsetSource.INFERRED))
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=low.id, offset=3, source=OffsetSource.MANUAL))
    world.commit()
    with pytest.raises(FeedLockedError):
        feeds.pin(world.ctx(), show, low)
    feeds.pin(world.ctx(), show, low, release_lock=True)
    world.commit()
    assert (show.feed_id, show.feed_state, show.learned_name) == (low.id, FeedState.PINNED, None)
    assert [o.source for o in world.session.exec(select(EpisodeOffset)).all()] == [OffsetSource.MANUAL]


def test_releasing_clears_a_pin(world):
    top, _ = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.PINNED)
    feeds.release(world.ctx(), show)
    assert (show.feed_id, show.feed_state) == (None, FeedState.NONE)


def test_canceling_the_only_grab_releases_the_lock_and_its_numbering(world):
    top, _ = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.LOCKED, learned_name="Frieren")
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=top.id, offset=12, source=OffsetSource.CONFIRMED))
    world.commit()
    assert feeds.unwind_unproven(world.ctx(), show, top.id)
    world.commit()
    assert (show.feed_id, show.feed_state, show.learned_name) == (None, FeedState.NONE, None)
    assert world.session.exec(select(EpisodeOffset)).all() == []


def test_a_pin_survives_unwinding(world):
    top, _ = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.PINNED)
    assert not feeds.unwind_unproven(world.ctx(), show, top.id)
    assert show.feed_state == FeedState.PINNED


def test_a_show_matched_before_it_airs_goes_back_to_discovery(world):
    top, _ = two_feeds(world)
    upcoming = world.show(1, episodes=1, first_air=START + timedelta(days=30), feed=top, feed_state=FeedState.ASSIGNED,
                          next_airing_episode=1, next_airing_at=START + timedelta(days=30), anilist_status="NOT_YET_RELEASED")
    airing = world.show(2, "Other", episodes=1, first_air=START - timedelta(days=1), feed=top, feed_state=FeedState.ASSIGNED)
    undone = feeds.unwind_unreleased(world.ctx())
    assert [s.id for s in undone] == [upcoming.id]
    assert upcoming.feed_state == FeedState.NONE and airing.feed_state == FeedState.ASSIGNED


def test_forgetting_a_feed_sends_its_shows_back_to_discovery_and_closes_the_priority_gap(world):
    top, low = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.LOCKED)
    ctx = world.ctx()
    assert feeds.forget_feed(ctx, top) == 1
    world.commit()
    assert (show.feed_id, show.feed_state) == (None, FeedState.NONE)
    assert [(f.name, f.priority) for f in world.session.exec(select(Feed)).all()] == [("Low", 1)]


def test_adding_editing_and_removing_a_feed_never_touches_qbittorrent_and_a_url_is_never_listed_twice(world):
    ctx = world.ctx()
    feed = feeds.add_feed(ctx, "Nyaa", "https://nyaa/rss")
    assert feeds.add_feed(ctx, "Again", "https://nyaa/rss") is feed
    feed.failing_since = world.clock()
    feed.last_error = "boom"
    feeds.edit_feed(ctx, feed, url="https://nyaa2/rss", name="Nyaa2")
    assert (feed.name, feed.url, feed.failing_since, feed.last_error) == ("Nyaa2", "https://nyaa2/rss", None, None)
    feeds.forget_feed(ctx, feed)
    assert not [name for name, _ in world.qbit.calls if name.startswith('rss')]


def test_importing_from_qbit_adds_only_unknown_urls_keeping_their_paths(world):
    world.feed("Known", "https://known/rss", 1)
    world.qbit.rss.append(RssStoreFeed("Known", "https://known/rss"))
    world.qbit.rss.append(RssStoreFeed("Folder\\New", "https://new/rss"))
    added = feeds.import_from_qbit(world.ctx())
    assert [(f.name, f.qbit_path, f.priority) for f in added] == [("New", "Folder\\New", 2)]


def test_reordering_renumbers_priorities(world):
    a, b = two_feeds(world)
    feeds.reorder_feeds(world.ctx(), [b.id, a.id])
    world.commit()
    assert [f.id for f in feeds.ranked(world.ctx())] == [b.id, a.id]
    assert [f.priority for f in feeds.ranked(world.ctx())] == [1, 2]


def test_the_candidate_timer_is_the_earliest_pending_one(world):
    top, low = two_feeds(world)
    world.show(1, feed=low, feed_state=FeedState.CANDIDATE, candidate_since=START)
    world.show(2, "B", feed=low, feed_state=FeedState.CANDIDATE, candidate_since=START + timedelta(seconds=100))
    assert feeds.next_candidate_due(world.ctx()) == START + CANDIDATE_GRACE


def test_canceling_one_grab_keeps_the_lock_while_another_download_from_the_feed_remains(world):
    top, _ = two_feeds(world)
    show = world.show(episodes=2, first_air=START, feed=top, feed_state=FeedState.LOCKED, learned_name="Frieren")
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=top.id, offset=12, source=OffsetSource.CONFIRMED))
    world.session.add(Download(episode_id=world.episode(show, 2).id, feed_id=top.id, title="x", state=DownloadState.DONE))
    world.commit()
    assert not feeds.unwind_unproven(world.ctx(), show, top.id)
    assert (show.feed_id, show.feed_state) == (top.id, FeedState.LOCKED)
    assert len(world.session.exec(select(EpisodeOffset)).all()) == 1


def test_a_mapping_the_user_set_survives_a_canceled_grab(world):
    top, _ = two_feeds(world)
    show = world.show(feed=top, feed_state=FeedState.LOCKED, learned_name="Frieren")
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=top.id, offset=12, source=OffsetSource.MANUAL))
    world.commit()
    assert feeds.unwind_unproven(world.ctx(), show, top.id)
    world.commit()
    assert show.feed_state == FeedState.NONE
    assert [o.source for o in world.session.exec(select(EpisodeOffset)).all()] == [OffsetSource.MANUAL]


def test_putting_a_show_back_leaves_pinned_locked_and_downloaded_shows_alone(world):
    top, _ = two_feeds(world)
    upcoming = dict(episodes=1, first_air=START + timedelta(days=30), feed=top, next_airing_episode=1,
                    next_airing_at=START + timedelta(days=30), anilist_status="NOT_YET_RELEASED")
    pinned = world.show(1, "A", feed_state=FeedState.PINNED, **upcoming)
    locked = world.show(2, "B", feed_state=FeedState.LOCKED, **upcoming)
    downloaded = world.show(3, "C", feed_state=FeedState.ASSIGNED, **upcoming)
    world.session.add(Download(episode_id=world.episode(downloaded, 1).id, feed_id=top.id, title="x", state=DownloadState.DONE))
    world.commit()
    assert feeds.unwind_unreleased(world.ctx()) == []
    assert [s.feed_state for s in (pinned, locked, downloaded)] == [FeedState.PINNED, FeedState.LOCKED, FeedState.ASSIGNED]
