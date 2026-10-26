from datetime import datetime, timedelta, timezone

from kisetsu.db.schema import EpisodeStatus
from kisetsu.domain.schedule import (
    EpisodeFacts,
    ShowFacts,
    air_horizon,
    date_mapped_episode,
    hunt_plan,
    is_hunting,
    is_unreleased,
    latest_aired,
    missed_numbers,
    next_hunt_start,
    published_before_air,
    waiting_for_release,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
AIR = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
W, C, M = EpisodeStatus.WANTED, EpisodeStatus.COMPLETED, EpisodeStatus.MISSED


def show(**fields) -> ShowFacts:
    values = dict(
        id=1, name="Show", paused=False, completed=False, in_premiere=False, working=False,
        anilist_status="RELEASING", next_airing_episode=None, next_airing_at=None, episodes=(),
    )
    values.update(fields)
    return ShowFacts(**values)


def ep(number, status=W, air_at=None, retry_after=None):
    return EpisodeFacts(number=number, air_at=air_at, status=status, retry_after=retry_after)


def hunting(s, now=NOW, tolerance=6):
    return is_hunting(s, now, tolerance)


# Hunting from AniList's pointer alone

def test_a_show_that_aired_minutes_ago_is_hunted():
    s = show(next_airing_episode=1, next_airing_at=NOW - timedelta(minutes=15), in_premiere=True)
    assert hunting(s)


def test_a_show_that_aired_hours_ago_is_hunted_and_not_waiting():
    s = show(next_airing_episode=1, next_airing_at=NOW - timedelta(hours=2))
    assert hunting(s)
    assert not waiting_for_release(s, NOW)


def test_a_show_a_day_past_its_air_time_is_left_to_the_routine_read():
    s = show(next_airing_episode=1, next_airing_at=NOW - timedelta(days=8), anilist_status="FINISHED")
    assert not hunting(s)
    assert waiting_for_release(s, NOW)


def test_the_previous_episode_that_aired_hours_ago_is_hunted():
    s = show(next_airing_episode=23, next_airing_at=NOW + timedelta(days=7) - timedelta(hours=3))
    assert hunting(s)


def test_nothing_is_hunted_during_a_hiatus_longer_than_a_week():
    s = show(next_airing_episode=6, next_airing_at=NOW + timedelta(days=14), episodes=(ep(4, C),))
    assert not hunting(s)


def test_a_show_without_a_release_date_is_never_hunted():
    s = show(next_airing_episode=None, next_airing_at=None)
    assert not hunting(s)
    assert next_hunt_start(s, NOW, 6) is None


def test_a_premiere_days_away_is_not_hunted_but_starts_before_its_air_time():
    air = NOW + timedelta(days=5)
    s = show(next_airing_episode=1, next_airing_at=air, in_premiere=True)
    assert not hunting(s)
    assert next_hunt_start(s, NOW, 6) == air - timedelta(hours=6)


def test_a_working_show_with_a_near_air_time_wakes_at_the_air_time():
    air = NOW + timedelta(minutes=30)
    s = show(working=True, next_airing_episode=6, next_airing_at=air, episodes=(ep(5, C),))
    assert not hunting(s)
    assert next_hunt_start(s, NOW, 6) == air


# Hunting from the episode ledger

def test_a_wanted_episode_left_after_a_rollover_is_hunted():
    s = show(working=True, next_airing_episode=9, next_airing_at=NOW + timedelta(days=7), episodes=(ep(8),))
    assert hunting(s)


def test_a_missed_episode_far_in_the_past_is_not_hunted():
    s = show(
        working=True, next_airing_episode=9, next_airing_at=NOW + timedelta(days=7),
        episodes=(ep(8, M, air_at=NOW - timedelta(days=30)),),
    )
    assert not hunting(s)


def test_an_old_gap_does_not_keep_a_caught_up_show_hunted():
    s = show(
        working=True, next_airing_episode=19, next_airing_at=NOW + timedelta(days=5),
        episodes=(ep(12, M), ep(18, C), ep(19)),
    )
    assert not hunting(s)


def test_a_future_wanted_episode_is_not_hunted():
    s = show(
        working=True, next_airing_episode=9, next_airing_at=NOW + timedelta(days=7),
        episodes=(ep(8, C), ep(9)),
    )
    assert not hunting(s)


def _scheduled(wanted=9, done_through=8, air=AIR, in_premiere=False):
    episodes = tuple(
        ep(n, C if n <= done_through else W, air_at=air - timedelta(days=7 * (wanted - n)))
        for n in range(1, wanted + 1)
    )
    return show(working=True, in_premiere=in_premiere, next_airing_episode=wanted, next_airing_at=air, episodes=episodes)


def test_hunting_opens_the_tolerance_window_for_a_premiere():
    s = _scheduled(wanted=1, done_through=0, in_premiere=True)
    assert not hunting(s, AIR - timedelta(hours=7))
    assert hunting(s, AIR - timedelta(hours=5, minutes=30))
    assert hunting(s, AIR - timedelta(hours=1))
    assert hunting(s, AIR + timedelta(hours=1))


def test_zero_tolerance_keeps_the_strict_window():
    s = _scheduled(in_premiere=True)
    assert not hunting(s, AIR - timedelta(minutes=30), tolerance=0)
    assert hunting(s, AIR + timedelta(minutes=30), tolerance=0)


def test_a_wider_tolerance_starts_hunting_even_earlier():
    s = _scheduled(wanted=1, done_through=0, in_premiere=True)
    assert hunting(s, AIR - timedelta(hours=20), tolerance=24)
    assert not hunting(s, AIR - timedelta(hours=20), tolerance=6)


def test_a_show_that_already_has_episodes_waits_for_the_stated_time():
    s = _scheduled()
    assert not hunting(s, AIR - timedelta(hours=5))
    assert not hunting(s, AIR - timedelta(minutes=30))
    assert hunting(s, AIR + timedelta(minutes=30))


def test_a_resolved_episode_does_not_keep_the_show_hunted():
    s = _scheduled(done_through=9)
    assert not hunting(s, AIR - timedelta(hours=1))


def test_a_paused_or_completed_show_is_not_hunted_and_has_no_start():
    for flag in ({"paused": True}, {"completed": True}):
        s = show(next_airing_episode=1, next_airing_at=NOW - timedelta(hours=1), **flag)
        assert not hunting(s)
        assert next_hunt_start(s, NOW, 6) is None


def test_the_tolerance_only_widens_the_horizon_of_a_premiere():
    s = show(in_premiere=True)
    assert air_horizon(s, NOW, 6) == NOW + timedelta(hours=6)
    assert air_horizon(show(), NOW, 6) == NOW


def test_latest_aired_prefers_stored_air_times_over_the_pointer():
    s = show(next_airing_episode=9, next_airing_at=NOW + timedelta(days=1), episodes=(ep(7, C, NOW - timedelta(days=9)), ep(8, W, NOW - timedelta(days=2))))
    assert latest_aired(s, NOW, NOW) == 8


def test_hunt_plan_lists_hunted_shows_and_the_next_start():
    aired = show(id=1, next_airing_episode=1, next_airing_at=NOW - timedelta(hours=1))
    later = show(id=2, next_airing_episode=3, next_airing_at=NOW + timedelta(hours=40), working=True, episodes=(ep(2, C),))
    soon = show(id=3, next_airing_episode=3, next_airing_at=NOW + timedelta(hours=30), working=True, episodes=(ep(2, C),))
    plan = hunt_plan([aired, later, soon], NOW, 6)
    assert plan.hunting == (1,)
    assert plan.next_start == NOW + timedelta(hours=30)


# Show states

def test_a_premiere_that_has_not_aired_is_unreleased_until_something_downloads():
    s = show(next_airing_episode=1, next_airing_at=NOW + timedelta(days=2))
    assert is_unreleased(s, NOW)
    assert not is_unreleased(show(next_airing_episode=1, next_airing_at=NOW - timedelta(hours=1)), NOW)
    assert not is_unreleased(show(next_airing_episode=1, next_airing_at=NOW + timedelta(days=2), episodes=(ep(1, C),)), NOW)


def test_a_movie_with_no_air_time_counts_as_released_and_waits():
    s = show(anilist_status="FINISHED", next_airing_episode=None, next_airing_at=None, episodes=(ep(1, M),))
    assert not is_unreleased(s, NOW)
    assert waiting_for_release(s, NOW)


def test_a_working_show_is_never_waiting():
    s = show(working=True, next_airing_episode=1, next_airing_at=NOW - timedelta(days=8))
    assert not waiting_for_release(s, NOW)


def test_the_newest_aired_episode_decides_whether_a_show_waits():
    s = show(
        next_airing_episode=5, next_airing_at=NOW + timedelta(days=3),
        episodes=(ep(3, C, NOW - timedelta(days=11)), ep(4, M, NOW - timedelta(days=4))),
    )
    assert waiting_for_release(s, NOW)
    fresh = show(
        next_airing_episode=5, next_airing_at=NOW + timedelta(days=3),
        episodes=(ep(3, C, NOW - timedelta(days=11)), ep(4, W, NOW - timedelta(hours=5))),
    )
    assert not waiting_for_release(fresh, NOW)


# Missed episodes

def test_a_wanted_episode_older_than_the_newest_aired_is_missed():
    eps = (ep(1, W, NOW - timedelta(days=14)), ep(2, W, NOW - timedelta(days=7)), ep(3, W, NOW - timedelta(hours=2)))
    assert missed_numbers(eps, NOW, NOW, 3) == [1, 2]


def test_the_newest_aired_episode_gets_a_day_before_it_is_missed():
    within = (ep(5, W, NOW - timedelta(hours=23)),)
    overdue = (ep(5, W, NOW - timedelta(hours=25)),)
    assert missed_numbers(within, NOW, NOW, 5) == []
    assert missed_numbers(overdue, NOW, NOW, 5) == [5]


def test_an_episode_followed_by_a_download_is_missed():
    eps = (ep(1, W, None), ep(2, C, None))
    assert missed_numbers(eps, NOW, NOW, 2) == [1]


def test_a_covered_or_retrying_or_future_episode_is_never_missed():
    old = NOW - timedelta(days=10)
    eps = (ep(1, W, old), ep(2, W, old, retry_after=NOW + timedelta(minutes=5)), ep(3, W, NOW + timedelta(days=3)), ep(4, W, NOW - timedelta(hours=1)))
    assert missed_numbers(eps, NOW, NOW, 4, covered={1}) == []


def test_only_wanted_episodes_can_become_missed():
    eps = (ep(1, M, NOW - timedelta(days=9)), ep(2, C, NOW - timedelta(days=8)), ep(3, W, NOW - timedelta(hours=1)))
    assert missed_numbers(eps, NOW, NOW, 3) == []


# Release dates

def test_a_release_maps_to_the_episode_that_aired_just_before_it():
    air = {1: NOW - timedelta(days=14), 2: NOW - timedelta(days=7), 3: NOW - timedelta(days=1)}
    assert date_mapped_episode(air, NOW, 12, 6) == 3
    assert date_mapped_episode(air, NOW - timedelta(days=12), 12, 6) == 1


def test_a_release_cannot_map_to_an_episode_that_has_not_aired():
    air = {3: NOW + timedelta(days=3)}
    assert date_mapped_episode(air, NOW, 12, 6) is None
    assert date_mapped_episode({3: NOW + timedelta(hours=5)}, NOW, 12, 6) == 3
    assert date_mapped_episode({3: NOW + timedelta(hours=5)}, NOW, 12, 0) is None


def test_a_release_days_after_every_air_time_maps_to_nothing():
    assert date_mapped_episode({1: NOW - timedelta(days=30)}, NOW, 12, 6) is None


def test_an_undated_release_maps_to_nothing():
    assert date_mapped_episode({1: NOW - timedelta(days=1)}, datetime(1970, 1, 1, tzinfo=timezone.utc), 12, 6) is None


def test_a_release_published_long_before_its_episode_is_an_earlier_season():
    air = NOW
    assert published_before_air(air - timedelta(days=60), air, 6)
    assert not published_before_air(air - timedelta(hours=40), air, 6)
    assert not published_before_air(air - timedelta(days=60), None, 6)
    assert not published_before_air(None, air, 6)
    assert not published_before_air(datetime(1970, 1, 1, tzinfo=timezone.utc), air, 6)
    assert not published_before_air(air - timedelta(hours=60), air, 72)


def test_a_searched_release_must_be_dated_inside_the_shows_run():
    from kisetsu.domain.schedule import plausible_for_show

    first = NOW - timedelta(days=30)
    assert plausible_for_show(NOW - timedelta(days=2), first, NOW)
    assert plausible_for_show(first - timedelta(days=2), first, NOW)
    assert not plausible_for_show(first - timedelta(days=4), first, NOW)
    assert not plausible_for_show(NOW + timedelta(days=2), first, NOW)
    assert not plausible_for_show(None, first, NOW)
    assert plausible_for_show(NOW - timedelta(days=400), None, NOW)


def test_a_premiere_with_no_date_or_a_far_date_is_out_of_reach_until_it_is_near():
    from kisetsu.domain.schedule import premiere_out_of_reach

    undated = show(anilist_status="NOT_YET_RELEASED", next_airing_episode=None, next_airing_at=None)
    far = show(anilist_status="NOT_YET_RELEASED", next_airing_episode=1, next_airing_at=NOW + timedelta(days=30))
    near = show(anilist_status="NOT_YET_RELEASED", next_airing_episode=1, next_airing_at=NOW + timedelta(days=3))
    assert premiere_out_of_reach(undated, NOW) and premiere_out_of_reach(far, NOW)
    assert not premiere_out_of_reach(near, NOW)
    assert not premiere_out_of_reach(show(anilist_status="RELEASING", next_airing_episode=1, next_airing_at=NOW + timedelta(days=30)), NOW)
