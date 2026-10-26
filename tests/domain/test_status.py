from datetime import datetime, timedelta, timezone

from kisetsu.db.schema import EpisodeStatus
from kisetsu.domain.schedule import EpisodeFacts, ShowFacts
from kisetsu.domain.status import ShowStatus, show_status

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def show(**fields):
    values = dict(
        id=1, name="Show", paused=False, completed=False, in_premiere=False, working=False,
        anilist_status="RELEASING", next_airing_episode=3, next_airing_at=NOW + timedelta(days=2),
        episodes=(EpisodeFacts(2, NOW - timedelta(days=5), EpisodeStatus.WANTED),),
    )
    values.update(fields)
    return ShowFacts(**values)


def test_paused_outranks_everything():
    assert show_status(show(paused=True, working=True, completed=True), NOW) is ShowStatus.PAUSED


def test_completed_outranks_working():
    assert show_status(show(completed=True, working=True), NOW) is ShowStatus.COMPLETED


def test_a_show_with_a_proven_feed_is_working():
    assert show_status(show(working=True), NOW) is ShowStatus.WORKING


def test_a_show_that_has_not_aired_is_upcoming():
    s = show(next_airing_episode=1, next_airing_at=NOW + timedelta(days=2), episodes=())
    assert show_status(s, NOW) is ShowStatus.UPCOMING


def test_an_aired_episode_still_missing_after_a_day_is_waiting():
    assert show_status(show(), NOW) is ShowStatus.WAITING


def test_a_freshly_aired_episode_is_testing():
    s = show(episodes=(EpisodeFacts(2, NOW - timedelta(hours=3), EpisodeStatus.WANTED),))
    assert show_status(s, NOW) is ShowStatus.TESTING
