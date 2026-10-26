from datetime import datetime, timedelta, timezone

from kisetsu.domain.numbering import LedgerRow, inferred_offset, map_episode, rebase_plan

NOW = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)


def at(*args):
    return datetime(*args, tzinfo=timezone.utc)


def mapped(raw, **kwargs):
    values = dict(
        offset=None, latest_aired=None, target_count=13, air_times={}, published_at=None,
        tolerance_hours=6, now=NOW,
    )
    values.update(kwargs)
    return map_episode(raw, **values)


# A stored offset

def test_a_stored_offset_maps_the_feeds_number_to_the_local_one():
    assert mapped(84, offset=66, target_count=19, latest_aired=18).local == 18


def test_a_stored_offset_that_lands_beyond_the_latest_aired_episode_is_rejected():
    assert mapped(24, offset=11, target_count=13, latest_aired=12).local is None


def test_a_stored_offset_that_lands_outside_the_season_is_rejected():
    assert mapped(5, offset=11, target_count=13, latest_aired=12).local is None
    assert mapped(30, offset=11, target_count=13, latest_aired=12).local is None


# Numbers that fit the season

AIR = {1: at(2026, 9, 26, 15), 2: at(2026, 10, 3, 15), 3: at(2026, 10, 10, 15)}


def test_a_feed_that_posts_before_the_air_time_keeps_its_own_numbers():
    early_three = mapped(3, latest_aired=3, air_times=AIR, published_at=at(2026, 10, 10, 9, 31))
    assert early_three == (3, False)
    late_one = mapped(1, latest_aired=3, air_times=AIR, published_at=at(2026, 10, 3, 16, 31))
    assert late_one == (1, False)
    late_two = mapped(2, latest_aired=3, air_times=AIR, published_at=at(2026, 10, 3, 16, 32))
    assert late_two == (2, False)


def test_a_number_without_a_stored_air_time_is_kept_when_it_has_aired():
    assert mapped(4, latest_aired=5).local == 4
    assert mapped(6, latest_aired=5).local == 6
    assert mapped(7, latest_aired=5).local is None


def test_a_release_dated_long_before_its_air_time_maps_by_date():
    air = {1: at(2026, 9, 1, 15), 2: at(2026, 9, 8, 15), 3: at(2026, 10, 10, 15)}
    result = mapped(3, latest_aired=3, air_times=air, published_at=at(2026, 9, 9, 12))
    assert result == (2, True)


# Numbers that do not fit

def test_an_absolute_number_maps_to_the_latest_aired_episode():
    assert mapped(78, target_count=19, latest_aired=18).local == 18


def test_several_absolute_numbers_count_back_from_the_newest():
    kwargs = dict(target_count=19, latest_aired=18, newest_raw=78)
    assert mapped(78, **kwargs).local == 18
    assert mapped(77, **kwargs).local == 17
    assert mapped(60, **kwargs).local is None
    assert mapped(40, **kwargs).local is None


def test_a_season_that_ended_long_ago_is_not_pinned_to_by_a_new_item():
    air = {n: NOW - timedelta(days=200) for n in range(1, 5)}
    assert mapped(99, target_count=4, latest_aired=4, air_times=air, published_at=NOW).local is None


def test_an_ambiguous_number_maps_to_nothing():
    assert mapped(20, target_count=13, latest_aired=None).local is None


def test_with_nothing_known_a_number_inside_the_season_is_taken_as_it_is():
    assert mapped(2, target_count=13, latest_aired=None).local == 2


# Learning an offset

def row(number, holds=True, source=None, this_feed=True, torrent=False):
    return LedgerRow(number, holds, source, this_feed, torrent)


def test_a_contradicting_release_teaches_the_difference():
    holder = row(18, source=18)
    assert inferred_offset(raw=78, local=18, holder=holder, held_sources=[18]) == 60


def test_no_offset_is_learned_from_a_matching_or_missing_holder():
    assert inferred_offset(78, 18, None, []) is None
    assert inferred_offset(78, 18, row(18, holds=False, source=18), [18]) is None
    assert inferred_offset(18, 18, row(18, source=18), [18]) is None
    assert inferred_offset(78, 18, row(18, source=None), []) is None
    assert inferred_offset(78, 18, row(18, source=18, this_feed=False), [18]) is None


def test_no_offset_is_learned_from_a_lower_or_equal_number():
    assert inferred_offset(10, 18, row(18, source=18), [18]) is None
    assert inferred_offset(78, 18, row(18, source=18), [80]) is None


# Rebasing stored releases

def test_releases_stored_under_the_raw_number_move_to_their_true_episode():
    rows = [row(n, holds=n in (16, 17), source=n if n in (16, 17) else None) for n in range(1, 20)]
    moves = rebase_plan(rows, offset=1, latest_aired=18)
    assert sorted(moves) == [(16, 15), (17, 16)]


def test_a_rebase_may_move_into_a_row_that_is_itself_moving():
    rows = [row(1, holds=False), row(2, holds=True, source=2), row(3, holds=True, source=3), row(4, holds=False)]
    assert sorted(rebase_plan(rows, offset=1, latest_aired=4)) == [(2, 1), (3, 2)]


def test_a_rebase_is_refused_when_a_target_already_holds_a_release():
    rows = [row(1, holds=True, source=None), row(2, holds=True, source=2)]
    assert rebase_plan(rows, offset=1, latest_aired=2) is None


def test_a_rebase_is_refused_when_a_target_has_a_torrent():
    rows = [row(1, holds=False, torrent=True), row(2, holds=True, source=2)]
    assert rebase_plan(rows, offset=1, latest_aired=2) is None


def test_a_rebase_is_refused_when_a_target_has_not_aired():
    rows = [row(1, holds=False), row(2, holds=True, source=3)]
    assert rebase_plan(rows, offset=0, latest_aired=2) is None


def test_a_rebase_that_changes_nothing_is_refused():
    rows = [row(1, holds=True, source=1), row(2, holds=False)]
    assert rebase_plan(rows, offset=0, latest_aired=2) is None


def test_a_rebase_with_nothing_to_move_is_refused():
    assert rebase_plan([row(1, holds=False)], offset=1, latest_aired=1) is None
