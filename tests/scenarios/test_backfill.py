import asyncio
from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import EpisodeStatus, FeedItem, FeedItemMatch, FeedState
from kisetsu.services import backfill, feedsync, grab, ingest
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


def test_a_search_leaves_another_shows_release_for_the_ordinary_feed_read(world):
    feed = world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    other = world.show(2, "Another Show", episodes=1, first_air=START - timedelta(days=1))
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    theirs = world.feeds.post(NYAA, "[Grp] Another Show - 01 (1080p).mkv", START - timedelta(days=1))
    mine = world.feeds.post(NYAA, title(2), START - timedelta(days=14))

    async def everything(client, url, term, now=None):
        return [mine, theirs]

    [item] = [i for i in backfill.due(world.ctx()) if i.show_id == show.id]
    found = asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=everything))
    assert backfill.apply(world.ctx(), item, found) == 1
    world.commit()
    # Only the searched show's release was stored, so the other one is still new to the next ordinary read.
    assert [i.title for i in world.session.exec(select(FeedItem)).all()] == [mine.title]
    result = ingest.ingest(world.ctx(), feed, [mine, theirs])
    world.commit()
    assert result.new == 1 and result.show_ids == {other.id}
    assert {(m.show_id) for m in world.session.exec(select(FeedItemMatch)).all()} == {show.id, other.id}


def test_a_completed_shows_search_still_stores_its_release_and_reopens_the_show(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world, completed=True)
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    release = world.feeds.post(NYAA, title(2), START - timedelta(days=14))

    async def one(client, url, term, now=None):
        return [release]

    [item] = backfill.due(world.ctx(), show_id=show.id, force=True)
    found = asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=one))
    assert backfill.apply(world.ctx(), item, found) == 1
    world.commit()
    world.session.refresh(show)
    assert show.completed is False


def test_a_sequels_release_found_by_the_first_seasons_search_reaches_the_sequel(world):
    world.feed("Nyaa", NYAA, 1)
    first = airing(world)
    sequel = world.show(2, "Sousou no Frieren 2nd Season", aliases=["Sousou no Frieren 2nd Season"], episodes=4, first_air=START - timedelta(days=21))
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    release = world.feeds.post(NYAA, "[Grp] Sousou no Frieren 2nd Season - 01 (1080p).mkv", START)

    async def found(client, url, term, now=None):
        return [release]

    [item] = [i for i in backfill.due(world.ctx()) if i.show_id == first.id]
    backfill.apply(world.ctx(), item, asyncio.run(backfill.search(None, item, backfill.matcher_for(first), world.clock(), searcher=found)))
    world.commit()
    assert {m.show_id for m in world.session.exec(select(FeedItemMatch)).all()} == {sequel.id}
    assert grab.covered_numbers(world.ctx(), sequel) == {1}


def test_a_search_that_stops_when_found_skips_the_lower_feeds_once_everything_is_found(world):
    top = "https://subsplease.org/rss/?t&r=1080"
    world.feed("SubsPlease", top, 1)
    world.feed("Nyaa", NYAA, 2)
    show = airing(world)
    world.feeds.searchable[top] = [world.feeds.post(top, title(n), START - timedelta(days=7 * (4 - n))) for n in (1, 2, 3, 4)]
    world.feeds.searchable[NYAA] = []
    [item] = backfill.due(world.ctx())

    async def fake(client, url, term, now=None):
        return await world.feeds.search(url, term)

    def searched_with(**kwargs):
        world.feeds.searched.clear()
        asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=fake, **kwargs))
        return {url for url, _ in world.feeds.searched}

    assert searched_with(stop_when_found=True) == {top}
    # By default (finding the feed of saved files, or showing what each feed carries) every feed is asked.
    assert searched_with() == {top, NYAA}


# How many results a page of the fake site was seen to hold.
PAGE = 75


def names_show(world):
    world.feed("Nyaa", NYAA, 1)
    show = world.show(title="Sousou no Frieren", aliases=["Frieren Beyond Journeys End", "Frieren the Slayer"], episodes=4,
                      first_air=START - timedelta(days=21))
    [item] = backfill.due(world.ctx())
    assert len(item.terms) == 3
    return show, item


def queries(world, item, show, grouping, grouped_answer=None):
    """The searches sent; ``grouped_answer`` stands in for a site that answers the grouped search its own way."""
    async def fake(client, url, term, now=None):
        if grouped_answer is not None and term.startswith("(("):
            world.feeds.searched.append((url, term))
            return grouped_answer
        return await world.feeds.search(url, term)

    world.feeds.searched.clear()
    asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=fake, grouping=grouping))
    return [term for _, term in world.feeds.searched]


def test_a_nyaa_feed_is_asked_for_all_of_a_shows_names_at_once_once_that_is_shown_to_work(world):
    from kisetsu.clients import sites
    show, item = names_show(world)
    grouped = sites.any_of(item.terms)
    grouping = sites.Grouping()
    grouping.saw("nyaa.si", PAGE)  # earlier searches showed the host's pages hold this many
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(2), START - timedelta(days=14))]
    # Not yet known: every name is asked for alone, then the grouped search, which found the same.
    assert queries(world, item, show, grouping) == [*item.terms, grouped]
    assert grouping.works("nyaa.si") is True
    # Shown to work: one search, and a page short of full holds every match.
    assert queries(world, item, show, grouping) == [grouped]
    # A full page may have left some out: then each name is asked for, as before.
    world.feeds.searchable[NYAA] = [
        world.feeds.post(NYAA, f"[Grp{n}] Sousou no Frieren - 01 (1080p).mkv", START - timedelta(days=21)) for n in range(PAGE)
    ]
    assert queries(world, item, show, grouping) == [grouped, *item.terms]


def test_a_site_that_answers_the_grouped_search_differently_is_asked_for_each_name_alone(world):
    from kisetsu.clients import sites
    show, item = names_show(world)
    grouped = sites.any_of(item.terms)
    grouping = sites.Grouping()
    grouping.saw("nyaa.si", PAGE)  # earlier searches showed the host's pages hold this many
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(2), START - timedelta(days=14))]
    # The site takes the brackets literally and finds nothing.
    assert queries(world, item, show, grouping, grouped_answer=[]) == [*item.terms, grouped]
    assert grouping.works("nyaa.si") is False
    assert queries(world, item, show, grouping) == item.terms


def test_while_one_search_checks_the_site_the_others_search_as_before(world):
    from kisetsu.clients import sites
    show, item = names_show(world)
    grouping = sites.Grouping()
    grouping.saw("nyaa.si", PAGE)  # earlier searches showed the host's pages hold this many
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(n), START - timedelta(days=7 * (4 - n))) for n in (1, 2, 3, 4)]
    assert grouping.claim("nyaa.si")
    # The first name finds every missing episode, so the others are not asked for.
    assert queries(world, item, show, grouping) == item.terms[:1]
    grouping.release("nyaa.si")
    assert queries(world, item, show, grouping) == [*item.terms, sites.any_of(item.terms)]


def test_a_page_that_may_have_been_cut_off_decides_nothing_about_grouping(world):
    from kisetsu.clients import sites
    show, item = names_show(world)
    grouping = sites.Grouping()
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(2), START - timedelta(days=14))]
    # The host has never returned more than this one result, so its page may hold just one.
    assert queries(world, item, show, grouping) == item.terms
    assert grouping.works("nyaa.si") is None


def test_nothing_found_by_any_name_decides_nothing_about_grouping(world):
    from kisetsu.clients import sites
    show, item = names_show(world)
    grouping = sites.Grouping()
    grouping.saw("nyaa.si", PAGE)  # earlier searches showed the host's pages hold this many
    world.feeds.searchable[NYAA] = []
    # An empty grouped answer could mean it works or that it ignores the names, so it is not asked.
    assert queries(world, item, show, grouping) == item.terms
    assert grouping.works("nyaa.si") is None


def test_releases_a_feed_numbers_from_an_earlier_season_count_as_found(world):
    top = "https://subsplease.org/rss/?t&r=1080"
    world.feed("SubsPlease", top, 1)
    world.feed("Nyaa", NYAA, 2)
    show = airing(world)
    # The feed counts on from the first season: episodes 1-4 are its 29-32.
    world.feeds.searchable[top] = [world.feeds.post(top, title(28 + n), START - timedelta(days=7 * (4 - n))) for n in (1, 2, 3, 4)]
    world.feeds.searchable[NYAA] = []
    [item] = backfill.due(world.ctx())

    async def fake(client, url, term, now=None):
        return await world.feeds.search(url, term)

    world.feeds.searched.clear()
    asyncio.run(backfill.search(None, item, backfill.matcher_for(show), world.clock(), searcher=fake, stop_when_found=True))
    assert {url for url, _ in world.feeds.searched} == {top}


def test_an_episode_is_missed_only_once_the_search_for_it_found_nothing(world):
    feed = world.feed("Nyaa", NYAA, 1)
    feed.last_ok_at = START
    show = airing(world)
    ingest.ingest(world.ctx(), feed, [article(title(4), START)])
    world.commit()
    first = grab.grab_pass(world.ctx())
    world.commit()
    # The search for the older episodes has not run yet: they may still turn up, so nothing is missed.
    assert first.started == 1 and first.missed == 0
    assert [world.episode(show, n).status for n in (1, 2, 3)] == [EpisodeStatus.WANTED] * 3
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(2), START - timedelta(days=14))]
    [item] = backfill.due(world.ctx())
    run_search(world, item, show)
    second = grab.grab_pass(world.ctx())
    world.commit()
    assert second.started == 1 and second.missed == 2
    assert [world.episode(show, n).status for n in (1, 2, 3)] == [EpisodeStatus.MISSED, EpisodeStatus.DOWNLOADING, EpisodeStatus.MISSED]


def test_a_show_whose_searches_keep_finding_nothing_is_searched_less_often(world):
    world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    world.feeds.searchable[NYAA] = []

    def search_when_due():
        due = [i for i in backfill.due(world.ctx()) if i.show_id == show.id]
        if due:
            run_search(world, due[0], show)
        return bool(due)

    assert search_when_due()
    waits = []
    for _ in range(4):
        hours = 0
        while not search_when_due():
            world.clock.advance(hours=1)
            hours += 1
        waits.append(hours)
    assert waits == [6, 12, 24, 24]
    # Something found starts the waits over (what is still missing is searched for at once).
    world.feeds.searchable[NYAA] = [world.feeds.post(NYAA, title(2), START - timedelta(days=14))]
    world.clock.advance(hours=24)
    assert search_when_due()
    world.feeds.searchable[NYAA] = []
    assert search_when_due()
    hours = 0
    while not search_when_due():
        world.clock.advance(hours=1)
        hours += 1
    assert hours == 6


def test_a_restart_does_not_search_again_a_show_searched_in_the_last_six_hours(world):
    from kisetsu.services.context import Caches
    world.feed("Nyaa", NYAA, 1)
    show = airing(world, last_backfill_at=START - timedelta(hours=1))
    world.caches = Caches()  # a fresh process remembers no searches
    assert backfill.due(world.ctx()) == []
    assert [d.show_id for d in backfill.due(world.ctx(), force=True)] == [show.id]
    world.clock.advance(hours=6)
    assert [d.show_id for d in backfill.due(world.ctx())] == [show.id]


NYAA_B = "https://nyaa.si/?page=rss&c=1_2&f=0&q=y"


def other(n, days_ago):
    return article(f"[Grp] Some Other Show - {n:02d} (1080p).mkv", START - timedelta(days=days_ago))


def test_a_feed_whose_last_read_reached_back_past_an_episode_yet_held_nothing_of_the_show_is_not_searched(world):
    lacking = world.feed("Lacking", NYAA, 1)
    carrying = world.feed("Carrying", NYAA_B, 2)
    airing(world)
    # Both reads reach back 30 days, past episodes 1-3; only one of them holds anything of the show.
    feedsync.apply_fetch(world.ctx(), {
        lacking.id: [other(1, 30), other(2, 1)],
        carrying.id: [other(1, 30), article(title(4), START)],
    })
    world.commit()
    [item] = backfill.due(world.ctx())
    assert item.feeds == [carrying] and item.missing == [1, 2, 3]


def test_a_read_too_short_or_without_dates_says_nothing_about_what_a_feed_carries(world):
    short = world.feed("Short", NYAA, 1)
    undated = world.feed("Undated", NYAA_B, 2)
    airing(world)
    # Episode 3 aired 7 days ago: a read reaching back 8 days could hold a release posted up to 2 days before it,
    # and one reaching back 30 days that is a day old cannot yet hold a release posted 3 days late.
    feedsync.apply_fetch(world.ctx(), {short.id: [other(1, 8), other(2, 0)], undated.id: [article("[Grp] Some Other Show - 01.mkv")]})
    world.commit()
    assert backfill.due(world.ctx())[0].feeds == [short, undated]


def test_a_show_no_feed_carries_is_not_searched_and_its_aired_episodes_are_missed_at_once(world):
    feed = world.feed("Nyaa", NYAA, 1)
    feed.last_ok_at = START
    show = airing(world)
    feedsync.apply_fetch(world.ctx(), {feed.id: [other(1, 30), other(2, 0)]})
    world.commit()
    assert backfill.due(world.ctx()) == [] and backfill.search_completed(world.ctx(), show.id)
    grab.grab_pass(world.ctx())
    world.commit()
    assert [world.episode(show, n).status for n in (1, 2, 3)] == [EpisodeStatus.MISSED] * 3


def test_a_show_added_after_the_read_is_searched_on_the_feeds_whose_read_held_it(world):
    feed = world.feed("Nyaa", NYAA, 1)
    feedsync.apply_fetch(world.ctx(), {feed.id: [other(1, 30), article(title(4), START)]})
    world.commit()
    show = airing(world)
    ingest.ensure_matches_current(world.ctx())
    world.commit()
    [item] = backfill.due(world.ctx())
    assert item.show_id == show.id and item.feeds == [feed]


def test_saved_files_are_still_looked_for_on_a_feed_that_no_longer_carries_the_show(world):
    feed = world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    for n in (1, 2, 3, 4):
        world.episode(show, n).status = EpisodeStatus.COMPLETED
    world.commit()
    feedsync.apply_fetch(world.ctx(), {feed.id: [other(1, 30), other(2, 0)]})
    world.commit()
    [item] = backfill.due(world.ctx(), show_id=show.id, force=True, unmanaged=True)
    assert item.feeds == [feed]


def test_a_search_the_user_asks_for_still_asks_a_feed_that_seems_not_to_carry_the_show(world):
    feed = world.feed("Nyaa", NYAA, 1)
    show = airing(world)
    feedsync.apply_fetch(world.ctx(), {feed.id: [other(1, 30), other(2, 0)]})
    world.commit()
    assert backfill.due(world.ctx(), show_id=show.id, force=True) == []
    assert backfill.missing_episodes(world.ctx(), show.id) == [1, 2, 3, 4]
    [item] = backfill.due(world.ctx(), show_id=show.id, force=True, everywhere=True)
    assert item.feeds == [feed]


SUBS = "https://subsplease.org/rss/?t&r=1080"


def weekly_now(world, anilist_id=1, title="Sousou no Frieren"):
    """A show with an episode in the last week and one still to come."""
    return world.show(anilist_id, title, episodes=5, first_air=START - timedelta(days=21))


def test_a_subsplease_feed_is_not_searched_for_an_airing_show_its_schedule_leaves_out(world):
    subs = world.feed("SubsPlease", SUBS, 1)
    nyaa = world.feed("Nyaa", NYAA, 2)
    show = weekly_now(world)
    backfill.note_schedule(world.ctx(), ["Some Other Show"])
    assert backfill.due(world.ctx())[0].feeds == [nyaa]
    # A search the user asks for still asks it.
    assert backfill.due(world.ctx(), show_id=show.id, force=True, everywhere=True)[0].feeds == [subs, nyaa]
    backfill.note_schedule(world.ctx(), ["Some Other Show", "Sousou no Frieren"])
    assert backfill.due(world.ctx())[0].feeds == [subs, nyaa]
    # A schedule that could not be read says nothing.
    backfill.note_schedule(world.ctx(), ["Some Other Show"])
    backfill.note_schedule(world.ctx(), None)
    assert backfill.due(world.ctx())[0].feeds == [subs, nyaa]


def test_only_a_show_airing_week_by_week_is_judged_by_the_schedule(world):
    subs = world.feed("SubsPlease", SUBS, 1)
    airing(world)  # its last episode aired now: none is still to come
    backfill.note_schedule(world.ctx(), ["Some Other Show"])
    assert backfill.due(world.ctx())[0].feeds == [subs]


def test_a_schedule_that_leaves_out_a_show_the_feed_carries_is_not_used(world, caplog):
    subs = world.feed("SubsPlease", SUBS, 1)
    weekly_now(world)
    other = weekly_now(world, 2, "Kusuriya no Hitorigoto")
    ingest.ingest(world.ctx(), subs, [article("[SubsPlease] Kusuriya no Hitorigoto - 03 (1080p).mkv", START - timedelta(days=7))])
    world.commit()
    backfill.note_schedule(world.ctx(), ["Some Other Show"])
    assert {item.show_id: item.feeds for item in backfill.due(world.ctx())}[other.id] == [subs]
    assert [item.feeds for item in backfill.due(world.ctx())] == [[subs], [subs]]
    assert "leaves out" in caplog.text
