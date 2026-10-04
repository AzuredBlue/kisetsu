from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock
from sqlmodel import Session, create_engine, SQLModel
from qbit_seasonal_anime.core.stall import check_and_handle_stalls
from qbit_seasonal_anime.db.models import Episode, EpisodeStatus, Feed, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings


def _utc_now():
    return datetime.now(timezone.utc)


class TestStall(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.settings = Settings(id=1, stall_wait_hours=24, base_dir="/tmp/Anime")
        self.session.add(self.settings)

        self.feed_1 = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.feed_2 = Feed(id=2, qbit_feed_name="Erai-raws", qbit_feed_url="https://www.erai-raws.info/rss-1080p/", priority=2)
        self.session.add(self.feed_1)
        self.session.add(self.feed_2)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_finished_show_transitions_to_completed(self):
        show = Monitored(
            id=1,
            anilist_id=1001,
            display_name="Finished Anime",
            aliases_json='["Finished Anime"]',
            status=MonitoredStatus.FIXED,
            total_episodes=12,
            last_confirmed_episode=12,
            next_airing_episode=None,
            next_airing_at=None,
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.COMPLETED)
        self.assertTrue(any("completed all 12 episodes" in log for log in logs))

    def test_direct_mode_requires_every_episode_before_completing(self):
        """A wanted episode keeps the season open even with a match on the last one."""
        self.settings.download_mode = "direct"
        self.session.add(self.settings)
        show = Monitored(
            id=90,
            anilist_id=1090,
            display_name="Partly Downloaded",
            aliases_json='["Partly Downloaded"]',
            status=MonitoredStatus.FIXED,
            total_episodes=3,
            last_confirmed_episode=3,
            next_airing_episode=None,
            next_airing_at=None,
        )
        self.session.add(show)
        self.session.flush()
        self.session.add(Episode(
            monitored_id=show.id, episode_number=2, status=EpisodeStatus.WANTED
        ))
        self.session.add(Episode(
            monitored_id=show.id, episode_number=3, status=EpisodeStatus.COMPLETED
        ))
        self.session.commit()

        check_and_handle_stalls(self.session, MagicMock(), self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.FIXED)

    def test_direct_mode_completes_once_every_episode_is_done(self):
        self.settings.download_mode = "direct"
        self.session.add(self.settings)
        show = Monitored(
            id=91,
            anilist_id=1091,
            display_name="Fully Downloaded",
            aliases_json='["Fully Downloaded"]',
            status=MonitoredStatus.FIXED,
            total_episodes=2,
            last_confirmed_episode=2,
            next_airing_episode=None,
            next_airing_at=None,
        )
        self.session.add(show)
        self.session.flush()
        for episode_number in (1, 2):
            self.session.add(Episode(
                monitored_id=show.id,
                episode_number=episode_number,
                status=EpisodeStatus.COMPLETED,
            ))
        self.session.commit()

        logs = check_and_handle_stalls(self.session, MagicMock(), self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.COMPLETED)
        self.assertTrue(any("completed all 2 episodes" in log for log in logs))

    def test_stall_triggers_fallback_to_next_feed(self):
        show = Monitored(
            id=2,
            anilist_id=1002,
            display_name="Stalled Anime",
            aliases_json='["Stalled Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            qbit_rule_name="[Seasonal] Stalled Anime",
            last_confirmed_episode=1,
            next_airing_episode=2,
            next_airing_at=_utc_now() - timedelta(hours=48),
        )
        self.session.add(show)

        hist = RuleHistory(
            id=1,
            monitored_id=2,
            feed_id=1,
            created_at=_utc_now() - timedelta(hours=48),
            outcome=RuleOutcome.PENDING,
        )
        self.session.add(hist)
        self.session.commit()

        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://www.erai-raws.info/rss-1080p/",
                "articles": [
                    {"title": "[Erai-raws] Stalled Anime - 02 [1080p].mkv", "torrentURL": "https://erai/2.torrent"}
                ]
            }
        }

        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)
        self.session.refresh(hist)

        mock_qbit.remove_rss_rule.assert_called_with(rule_name="[Seasonal] Stalled Anime")
        self.assertEqual(show.current_feed_id, 2)
        self.assertEqual(show.status, MonitoredStatus.UNCONFIRMED)
        self.assertEqual(hist.outcome, RuleOutcome.STALLED)
        self.assertTrue(any("Moved to feed 'Erai-raws'" in log for log in logs))

    def test_stall_never_abandons_a_feed_that_has_already_delivered(self):
        """A feed proven by a real download is kept even when an episode goes late."""
        show = Monitored(
            id=5,
            anilist_id=1005,
            display_name="Proven Feed Anime",
            aliases_json='["Proven Feed Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            learned_feed_id=1,
            qbit_rule_name="[Seasonal] Proven Feed Anime",
            last_confirmed_episode=1,
            next_airing_episode=2,
            next_airing_at=_utc_now() - timedelta(hours=48),
        )
        self.session.add(show)
        hist = RuleHistory(
            id=5,
            monitored_id=5,
            feed_id=1,
            created_at=_utc_now() - timedelta(hours=48),
            outcome=RuleOutcome.PENDING,
        )
        self.session.add(hist)
        self.session.commit()

        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://www.erai-raws.info/rss-1080p/",
                "articles": [
                    {"title": "[Erai-raws] Proven Feed Anime - 02 [1080p].mkv", "torrentURL": "https://erai/2.torrent"}
                ]
            }
        }

        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)
        self.session.refresh(hist)

        # The rule stays armed on the feed that delivered, and no failure is
        # recorded, so the feed is not excluded from anything later either.
        self.assertEqual(show.current_feed_id, 1)
        self.assertEqual(show.status, MonitoredStatus.UNCONFIRMED)
        self.assertEqual(hist.outcome, RuleOutcome.PENDING)
        mock_qbit.remove_rss_rule.assert_not_called()
        self.assertTrue(any("already proven" in log for log in logs))

    def test_stall_never_abandons_a_user_pinned_feed(self):
        show = Monitored(
            id=6,
            anilist_id=1006,
            display_name="Pinned Feed Anime",
            aliases_json='["Pinned Feed Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            feed_pinned=True,
            qbit_rule_name="[Seasonal] Pinned Feed Anime",
            last_confirmed_episode=1,
            next_airing_episode=2,
            next_airing_at=_utc_now() - timedelta(hours=48),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertEqual(show.current_feed_id, 1)
        mock_qbit.remove_rss_rule.assert_not_called()
        self.assertTrue(any("pinned by you" in log for log in logs))

    def test_fixed_show_never_stalls(self):
        show = Monitored(
            id=4,
            anilist_id=1004,
            display_name="Working Confirmed Anime",
            aliases_json='["Working Confirmed Anime"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            qbit_rule_name="[Seasonal] Working Confirmed Anime",
            last_confirmed_episode=5,
            next_airing_episode=6,
            next_airing_at=_utc_now() - timedelta(hours=72),
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.FIXED)
        self.assertEqual(show.current_feed_id, 1)
        mock_qbit.remove_rss_rule.assert_not_called()

    def test_stall_with_all_feeds_exhausted_enters_stalled_state(self):
        show = Monitored(
            id=3,
            anilist_id=1003,
            display_name="Single Feed Stalled Anime",
            aliases_json='["Single Feed Stalled Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            last_confirmed_episode=0,
            next_airing_episode=1,
            next_airing_at=_utc_now() - timedelta(hours=48),
        )
        self.session.add(show)

        h1 = RuleHistory(id=10, monitored_id=3, feed_id=1, created_at=_utc_now() - timedelta(hours=48), outcome=RuleOutcome.STALLED)
        h2 = RuleHistory(id=11, monitored_id=3, feed_id=2, created_at=_utc_now() - timedelta(hours=48), outcome=RuleOutcome.STALLED)
        self.session.add(h1)
        self.session.add(h2)
        self.session.commit()

        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {}

        logs = check_and_handle_stalls(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.STALLED)
        self.assertIsNone(show.current_feed_id)
        self.assertTrue(any("All feeds exhausted" in log for log in logs))


if __name__ == "__main__":
    unittest.main()
