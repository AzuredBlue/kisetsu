import unittest
from datetime import datetime, timedelta, timezone
from sqlmodel import Session, SQLModel, create_engine, select
from kisetsu.db.models import (
    Episode,
    EpisodeStatus,
    Feed,
    Monitored,
    MonitoredStatus,
    TorrentOperation,
    TorrentOperationStatus,
    utc_now,
)
from kisetsu.workers.scheduler import calculate_next_poll_interval, is_hunting


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(feed)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_hunting_mode_when_show_aired_recently_without_feed(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Airing Anime",
            aliases_json='["Airing Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now - timedelta(minutes=15),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 300)
        self.assertIn("Hunting mode", reason)

    def test_wake_on_air_time_when_upcoming_show_airs_soon(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Upcoming Anime",
            aliases_json='["Upcoming Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now + timedelta(minutes=45),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertAlmostEqual(dur, 2700, delta=10)
        self.assertIn("Upcoming premiere", reason)

    def test_default_interval_when_all_shows_working(self):
        now = utc_now()
        show = Monitored(
            id=1,
            anilist_id=101,
            display_name="Working Anime",
            aliases_json='["Working Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=5,
            next_airing_episode=6,
            next_airing_at=now + timedelta(minutes=30),  # Airs in 30 mins, but rule is already working
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertIn("working rules", reason)

    def test_fixed_show_does_not_query_rss_refresh_interval(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=6,
            anilist_id=606,
            display_name="Already Working Anime",
            aliases_json='["Already Working Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=1,
            next_airing_at=utc_now() - timedelta(days=1),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
        )

        self.assertEqual(duration, 21600)
        self.assertIn("working rules", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_no_hunting_for_show_without_release_date(self):
        show = Monitored(
            id=2,
            anilist_id=202,
            display_name="Aoashi 2nd Season",
            aliases_json='["Aoashi 2nd Season", "Ao Ashi S2"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=None,
            next_airing_at=None,
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600)
        self.assertEqual(dur, 21600)
        self.assertNotIn("Hunting mode", reason)
        self.assertIn("waiting for air dates", reason)

    def test_dynamic_qbit_rss_refresh_interval(self):
        from unittest.mock import MagicMock
        now = utc_now()
        show = Monitored(
            id=3,
            anilist_id=303,
            display_name="Aired Show",
            aliases_json='["Aired Show"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=now - timedelta(minutes=10),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        mock_qbit.get_rss_refresh_interval_seconds.return_value = 315  # 5 min + 15 sec

        dur, reason = calculate_next_poll_interval(self.session, qbit_client=mock_qbit)
        self.assertEqual(dur, 315)
    def test_hunting_mode_when_previous_episode_aired_recently(self):
        now = utc_now()
        show = Monitored(
            id=4,
            anilist_id=404,
            display_name="Yomi no Tsugai",
            aliases_json='["Yomi no Tsugai"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=None,  # Ep 22 not confirmed yet
            next_airing_episode=23,
            next_airing_at=now + timedelta(days=7) - timedelta(hours=3),  # Previous episode aired 3 hours ago
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 300)
        self.assertIn("Hunting mode", reason)

    def test_no_hunting_during_a_hiatus_longer_than_a_week(self):
        now = utc_now()
        show = Monitored(
            id=6,
            anilist_id=606,
            display_name="Delayed Anime",
            aliases_json='["Delayed Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=4,
            next_airing_episode=6,
            next_airing_at=now + timedelta(days=14),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertNotIn("Hunting mode", reason)

    def test_no_hunting_when_unconfirmed_show_next_episode_is_days_away_and_no_recent_air(self):
        now = utc_now()
        show = Monitored(
            id=5,
            anilist_id=505,
            display_name="Future Premiere Anime",
            aliases_json='["Future Premiere Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=None,
            next_airing_episode=1,
            next_airing_at=now + timedelta(days=5),
        )
        self.session.add(show)
        self.session.commit()

        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertNotIn("Hunting mode", reason)


class TestIsHunting(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(feed)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _add(self, **kwargs):
        show = Monitored(
            anilist_id=kwargs.pop("anilist_id"),
            display_name=kwargs.pop("display_name", "Airing Anime"),
            aliases_json='["Airing Anime"]',
            **kwargs,
        )
        self.session.add(show)
        self.session.commit()
        return show

    def test_overdue_show_is_hunting(self):
        self._add(
            anilist_id=101,
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=utc_now() - timedelta(minutes=15),
        )
        self.assertTrue(is_hunting(self.session))
        dur, reason = calculate_next_poll_interval(self.session, hunting_interval_seconds=300)
        self.assertEqual(dur, 300)
        self.assertIn("Hunting mode", reason)

    def test_working_rules_are_not_hunting(self):
        self._add(
            anilist_id=102,
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=6,
            next_airing_at=utc_now() - timedelta(days=1),
        )
        self.assertFalse(is_hunting(self.session))
        dur, reason = calculate_next_poll_interval(self.session, default_interval_seconds=21600, hunting_interval_seconds=300)
        self.assertEqual(dur, 21600)
        self.assertIn("working rules", reason)

    def test_upcoming_premiere_is_not_hunting(self):
        self._add(
            anilist_id=103,
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            next_airing_episode=1,
            next_airing_at=utc_now() + timedelta(hours=3),
        )
        self.assertFalse(is_hunting(self.session))

    def test_no_shows_is_not_hunting(self):
        self.assertFalse(is_hunting(self.session))


if __name__ == "__main__":
    unittest.main()


class TestDirectScheduler(unittest.TestCase):
    """Cadence for the direct engines, where the app owns the downloads.

    A FIXED show means "the rule works" in rules mode, but in direct mode it
    only means "a release was matched", so the episode ledger decides whether
    another pass is needed.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(feed)
        self.session.commit()

    def tearDown(self):
        self.session.close()
    def test_direct_mode_uses_direct_ownership_message(self):
        show = Monitored(
            id=7,
            anilist_id=707,
            display_name="Direct Anime",
            aliases_json='["Direct Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=6,
            next_airing_at=utc_now() + timedelta(minutes=30),
        )
        self.session.add(show)
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            download_mode="direct",
        )

        self.assertAlmostEqual(duration, 1800, delta=10)
        self.assertIn("Upcoming premiere", reason)
        self.assertNotIn("working rules", reason)

    def test_direct_fixed_show_hunts_for_post_rollover_wanted_episode(self):
        show = Monitored(
            id=71,
            anilist_id=710,
            display_name="Link Click Season 3",
            aliases_json='["Link Click Season 3"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=7,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.WANTED))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            hunting_interval_seconds=300,
            download_mode="direct",
        )

        self.assertEqual(duration, 300)
        self.assertIn("Direct hunting", reason)
        self.assertIn("Link Click Season 3", reason)

    def test_direct_fixed_show_uses_routine_cadence_outside_window(self):
        now = utc_now()
        show = Monitored(
            id=76,
            anilist_id=760,
            display_name="Old Gap Anime",
            aliases_json='["Old Gap Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=9,
            next_airing_at=now + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(
            monitored_id=show.id,
            episode_number=8,
            status=EpisodeStatus.MISSED,
            air_at=now - timedelta(days=30),
        ))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            hunting_interval_seconds=300,
            download_mode="direct",
            backfill_window_days=14,
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)

    def test_direct_fixed_show_retains_old_canonical_episode_gap(self):
        show = Monitored(
            id=74,
            anilist_id=740,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO Season 4"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=18,
            next_airing_episode=19,
            next_airing_at=utc_now() + timedelta(days=5),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=12, status=EpisodeStatus.MISSED))
        self.session.add(Episode(monitored_id=show.id, episode_number=18, status=EpisodeStatus.COMPLETED))
        self.session.add(Episode(monitored_id=show.id, episode_number=19, status=EpisodeStatus.WANTED))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            download_mode="direct",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)

    def test_direct_fixed_show_ignores_future_wanted_episode(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=72,
            anilist_id=720,
            display_name="Caught Up Anime",
            aliases_json='["Caught Up Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=8,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.COMPLETED))
        self.session.add(Episode(monitored_id=show.id, episode_number=9, status=EpisodeStatus.WANTED))
        self.session.commit()
        mock_qbit = MagicMock()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
            download_mode="direct",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("direct ownership", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_rules_fixed_show_ignores_direct_episode_backlog(self):
        from unittest.mock import MagicMock

        show = Monitored(
            id=73,
            anilist_id=730,
            display_name="Rules Anime",
            aliases_json='["Rules Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            last_confirmed_episode=7,
            next_airing_episode=9,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(monitored_id=show.id, episode_number=8, status=EpisodeStatus.WANTED))
        self.session.commit()
        mock_qbit = MagicMock()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            qbit_client=mock_qbit,
            download_mode="rules",
        )

        self.assertEqual(duration, 21600)
        self.assertIn("working rules", reason)
        mock_qbit.get_rss_refresh_interval_seconds.assert_not_called()

    def test_direct_pending_operation_forces_fast_recovery_poll(self):
        show = Monitored(
            id=75,
            anilist_id=750,
            display_name="Pending Operation Anime",
            aliases_json='["Pending Operation Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            next_airing_episode=7,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.flush()
        episode = Episode(
            monitored_id=show.id,
            episode_number=6,
            status=EpisodeStatus.QUEUED,
        )
        self.session.add(episode)
        self.session.flush()
        self.session.add(TorrentOperation(
            episode_id=episode.id,
            kind="grab",
            status=TorrentOperationStatus.PREPARING,
            operation_tag="kisetsu-op-pending",
            release_title="Pending Release",
            version=1,
            new_torrent_url="magnet:pending",
        ))
        self.session.commit()

        duration, reason = calculate_next_poll_interval(
            self.session,
            default_interval_seconds=21600,
            hunting_interval_seconds=300,
            download_mode="direct",
        )

        self.assertEqual(duration, 300)
        self.assertIn("Direct hunting", reason)
        self.assertIn("Pending Operation Anime", reason)


AIR = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
REAL_AIR = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)


def _scheduled_show(session, air_at=AIR, wanted_episode=9, done_through=8):
    """A FIXED direct show whose next episode has a known air time."""
    show = Monitored(
        id=90,
        anilist_id=900,
        display_name="Early Bird",
        aliases_json='["Early Bird"]',
        status=MonitoredStatus.FIXED,
        current_feed_id=1,
        total_episodes=12,
        last_confirmed_episode=done_through,
        next_airing_episode=wanted_episode,
        next_airing_at=air_at,
    )
    session.add(show)
    session.flush()
    for number in range(1, wanted_episode + 1):
        session.add(Episode(
            monitored_id=show.id,
            episode_number=number,
            status=EpisodeStatus.COMPLETED if number <= done_through else EpisodeStatus.WANTED,
            air_at=air_at - timedelta(days=7 * (wanted_episode - number)),
        ))
    session.commit()
    return show


class TestEarlyAirTolerance(unittest.TestCase):
    """Hunting opens before the stated air time, so an early release is caught."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add(Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1))

    def tearDown(self):
        self.session.close()

    def _hunting_at(self, when, tolerance):
        from unittest.mock import patch
        with patch("kisetsu.workers.scheduler.utc_now", return_value=when):
            return is_hunting(
                self.session,
                download_mode="direct",
                early_air_tolerance_hours=tolerance,
            )

    def test_hunting_opens_the_tolerance_window_early(self):
        _scheduled_show(self.session, wanted_episode=1, done_through=0)
        # Five hours before the stated air time is inside a six hour window.
        self.assertFalse(self._hunting_at(AIR - timedelta(hours=7), 6))
        self.assertTrue(self._hunting_at(AIR - timedelta(hours=5, minutes=30), 6))
        self.assertTrue(self._hunting_at(AIR - timedelta(hours=1), 6))
        self.assertTrue(self._hunting_at(AIR + timedelta(hours=1), 6))

    def test_zero_tolerance_keeps_the_strict_window(self):
        _scheduled_show(self.session)
        self.assertFalse(self._hunting_at(AIR - timedelta(minutes=30), 0))
        self.assertTrue(self._hunting_at(AIR + timedelta(minutes=30), 0))

    def test_a_wider_tolerance_starts_hunting_even_earlier(self):
        _scheduled_show(self.session, wanted_episode=1, done_through=0)
        self.assertTrue(self._hunting_at(AIR - timedelta(hours=20), 24))
        self.assertFalse(self._hunting_at(AIR - timedelta(hours=20), 6))

    def test_a_show_that_already_has_episodes_waits_for_the_stated_time(self):
        _scheduled_show(self.session)
        self.assertFalse(self._hunting_at(AIR - timedelta(hours=5), 6))
        self.assertFalse(self._hunting_at(AIR - timedelta(minutes=30), 6))
        self.assertTrue(self._hunting_at(AIR + timedelta(minutes=30), 6))

    def test_a_resolved_episode_does_not_keep_the_show_hunting(self):
        show = _scheduled_show(self.session)
        episode = self.session.exec(
            select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 9)
        ).first()
        episode.status = EpisodeStatus.COMPLETED
        self.session.add(episode)
        self.session.commit()
        self.assertFalse(self._hunting_at(AIR - timedelta(hours=1), 6))
