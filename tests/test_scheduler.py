import unittest
from datetime import timedelta
from sqlmodel import Session, SQLModel, create_engine
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, utc_now
from qbit_seasonal_anime.workers.scheduler import calculate_next_poll_interval, is_hunting


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
