import asyncio
from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import EpisodeStatus, FeedItemMatch, FeedState
from kisetsu.services import backfill, grab, ingest
from tests.fakes import START, article

NYAA = "https://nyaa.si/?page=rss&c=1_2&f=0&q=x"


def title(n, group="Grp"):
    return f"[{group}] Sousou no Frieren - {n:02d} (1080p).mkv"


def airing(world, episodes=4, **fields):
    return world.show(episodes=episodes, first_air=START - timedelta(days=7 * (episodes - 1)), **fields)


def run_search(world, item, show):
    async def fake(client, url, term, now=None):
        return await world.feeds.search(url, term)

    found = asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=fake))
    added = backfill.apply(world.ctx(), item, found)
    world.commit()
    return found, added


def test_a_show_with_an_aired_episode_no_item_supplies_is_due_for_a_search(world):
    feed = world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    ingest.ingest(world.ctx(), feed, [article(title(4), START)])
    world.commit()
    [item] = backfill.due(world.ctx())
    assert (item.show_id, item.missing, item.terms[0]) == (show.id, [1, 2, 3], "Sousou no Frieren")
    assert item.feeds == [feed]


def test_nothing_is_due_without_a_searchable_feed_or_when_the_show_is_paused_or_done(world):
    plain = world.feed("Plain", "https://example.test/rss", 1)
    show = airing(world)
    assert backfill.due(world.ctx()) == []
    skipped = []
    backfill.due(world.ctx(), show_id=show.id, force=True, unsearchable=skipped)
    assert skipped == [show.id] and backfill.search_unavailable(world.ctx(), show.id)
    world.feed("Nyaa", NYAA, 2)
    show.paused = True
    world.commit()
    assert backfill.due(world.ctx()) == [] and len(backfill.due(world.ctx(), show_id=show.id, force=True)) == 1
    assert plain.id


def test_a_locked_show_searches_only_its_feed(world):
    world.feed("Top", "https://example.test/rss", 1)
    nyaa = world.feed("Nyaa", NYAA, 2)
    show = airing(world, feed=nyaa, feed_state=FeedState.LOCKED)
    assert backfill.due(world.ctx())[0].feeds == [nyaa]
    show.feed_state = FeedState.NONE
    world.commit()
    assert backfill.due(world.ctx())[0].feeds == [nyaa]


def test_searched_releases_are_stored_for_the_show_and_then_grabbed_by_the_normal_pass(world):
    feed = world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    world.feeds.searchable[NYAA] = [
        world.feeds.post(NYAA, title(2), START - timedelta(days=14)),
        world.feeds.post(NYAA, title(3), START - timedelta(days=7)),
        world.feeds.post(NYAA, "[Old] Sousou no Frieren - 02 (1080p).mkv", START - timedelta(days=400)),
    ]
    [item] = backfill.due(world.ctx())
    found, added = run_search(world, item, show)
    assert added == 2 and found.stopped is None
    result = grab.grab_pass(world.ctx())
    world.commit()
    assert result.started == 2
    assert [world.episode(show, n).status for n in (2, 3)] == [EpisodeStatus.DOWNLOADING] * 2
    assert feed.id and show.last_backfill_at == START


def test_the_same_search_is_not_repeated_for_six_hours(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    [item] = backfill.due(world.ctx())
    run_search(world, item, show)
    assert backfill.due(world.ctx()) == []
    assert len(backfill.due(world.ctx(), force=True)) == 1
    world.clock.advance(hours=7)
    assert len(backfill.due(world.ctx())) == 1
    assert backfill.search_completed(world.ctx(), show.id) is True


def test_a_rate_limited_site_stops_the_search_and_it_is_tried_again(world):
    from kisetsu.clients.rss import FeedFetchError

    world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    [item] = backfill.due(world.ctx())

    async def limited(client, url, term, now=None):
        raise FeedFetchError("nyaa.si is rate limiting requests")

    found = asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=limited))
    backfill.apply(world.ctx(), item, found)
    assert found.stopped and len(backfill.due(world.ctx())) == 1


def test_finished_episodes_without_a_tracked_torrent_count_only_when_asked(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world, episodes=1)
    episode = world.episode(show, 1)
    episode.status = EpisodeStatus.COMPLETED
    world.commit()
    assert backfill.missing_episodes(world.ctx(), show.id) == []
    assert backfill.missing_episodes(world.ctx(), show.id, unmanaged=True) == [1]


def test_search_results_are_matched_only_to_the_show_that_was_searched(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    other = world.show(2, "Another Show", episodes=1, first_air=START - timedelta(days=1))
    both = [
        world.feeds.post(NYAA, title(2), START - timedelta(days=14)),
        world.feeds.post(NYAA, "[Grp] Another Show - 01 (1080p).mkv", START - timedelta(days=1)),
    ]

    async def everything(client, url, term, now=None):
        return list(both)

    [item] = [i for i in backfill.due(world.ctx()) if i.show_id == show.id]
    found = asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=everything))
    backfill.apply(world.ctx(), item, found)
    world.commit()
    assert {m.show_id for m in world.session.exec(select(FeedItemMatch)).all()} == {show.id}
    assert other.id not in {m.show_id for m in world.session.exec(select(FeedItemMatch)).all()}


def test_a_search_repeats_within_six_hours_when_another_episode_goes_missing(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    world.episode(show, 3).status = EpisodeStatus.COMPLETED
    world.commit()
    [item] = backfill.due(world.ctx())
    run_search(world, item, show)
    assert backfill.due(world.ctx()) == []
    world.episode(show, 3).status = EpisodeStatus.MISSED
    world.commit()
    [again] = backfill.due(world.ctx())
    assert 3 in again.missing


def test_the_names_that_found_releases_before_are_searched_first(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world, title="Aoashi", learned_name="Ao Ashi")
    show.custom_aliases = ["Ao Ashi S2"]
    world.commit()
    [item] = backfill.due(world.ctx())
    assert item.terms[:2] == ["Ao Ashi", "Aoashi"]
