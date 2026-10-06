import json
import re
import unittest
from datetime import timedelta
from unittest.mock import MagicMock
from sqlmodel import Session, create_engine, SQLModel, select
from kisetsu.core.confirmation import has_downloaded_final_episode, verify_and_confirm_torrents
from kisetsu.db.models import Episode, EpisodeNumberMapping, EpisodeStatus, Feed, MatchHistory, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, utc_now

STEEL_BALL_RUN_ALIASES = [
    "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
    "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    "SBR",
    "JoJo's Bizarre Adventure: Part 7\u2013Steel Ball Run",
]


class TestConfirmation(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime")
        self.session.add(self.settings)

        self.feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(self.feed)

        self.show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=1,
            save_folder="Sousou no Frieren",
        )
        self.session.add(self.show)

        self.hist = RuleHistory(
            id=1,
            monitored_id=1,
            feed_id=1,
            outcome=RuleOutcome.PENDING,
            note="Initial rule",
        )
        self.session.add(self.hist)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_confirmation_true_positive(self):
        mock_qbit = MagicMock()
        call_order = []
        mock_qbit.set_rss_rule.side_effect = lambda **kwargs: call_order.append("rule")
        mock_received_time = utc_now()
        rule_name = "[Seasonal] Sousou no Frieren"
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        mock_qbit.find_log_acceptances.side_effect = lambda pairs: (
            call_order.append("lookup") or {(rule_name, release_title): mock_received_time}
        )
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {
                        "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                        "torrentURL": "https://subs/8.torrent",
                        "date": "03 Sep 2026 12:00:00 +0000",
                    }
                ]
            }
        }

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.close()
        self.session = Session(self.engine)
        self.show = self.session.get(Monitored, 1)
        self.hist = self.session.get(RuleHistory, 1)

        self.assertEqual(self.show.status, MonitoredStatus.FIXED)
        self.assertEqual(self.show.last_confirmed_episode, 8)
        self.assertEqual(self.hist.outcome, RuleOutcome.CONFIRMED)
        self.assertTrue(any("Confirmed rule" in log for log in logs))
        mock_qbit.find_log_acceptances.assert_called_once()
        self.assertEqual(call_order, ["rule", "lookup"])

        # Recorded with qBittorrent's own acceptance time, not the article date.
        m_hist = self.session.exec(select(MatchHistory)).all()
        self.assertEqual(len(m_hist), 1)
        self.assertEqual(m_hist[0].release_title, "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv")
        self.assertAlmostEqual(
            m_hist[0].created_at.replace(tzinfo=None),
            mock_received_time.replace(tzinfo=None),
            delta=timedelta(seconds=5),
        )

    def _frieren_feed(self, mock_qbit, release_title):
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {
                        "title": release_title,
                        "torrentURL": "https://subs/8.torrent",
                        "date": "03 Sep 2026 12:00:00 +0000",
                    }
                ],
            }
        }

    def test_no_history_row_when_qbittorrent_never_accepted_the_release(self):
        """A cached article our own fuzzy matcher likes is not proof of a download."""
        mock_qbit = MagicMock()
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        self._frieren_feed(mock_qbit, release_title)
        # No log acceptance, and no torrent for it either.
        mock_qbit.find_log_acceptances.return_value = {}
        mock_qbit.get_torrents.return_value = []

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)

        self.assertFalse(any("Skipped" in log for log in logs))
        self.assertEqual(self.session.exec(select(MatchHistory)).all(), [])
        # The supervisor still learns and repairs the rule from the release.
        self.assertEqual(self.show.status, MonitoredStatus.FIXED)
        self.assertIsNone(self.show.last_confirmed_episode)
        self.assertEqual(self.show.matched_title, "Sousou no Frieren")

    def test_absolute_feed_number_is_mapped_before_completion(self):
        self.show.total_episodes = 13
        self.show.status = MonitoredStatus.COMPLETED
        self.show.last_confirmed_episode = 23
        self.session.add(EpisodeNumberMapping(monitored_id=1, feed_id=1, offset=11))
        for episode_number in range(1, 14):
            self.session.add(
                Episode(
                    monitored_id=1,
                    episode_number=episode_number,
                    status=EpisodeStatus.COMPLETED if episode_number < 13 else EpisodeStatus.WANTED,
                )
            )
        self.session.commit()

        self.assertFalse(has_downloaded_final_episode(self.session, self.show))

        final_episode = self.session.exec(
            select(Episode).where(
                Episode.monitored_id == 1,
                Episode.episode_number == 13,
            )
        ).first()
        final_episode.status = EpisodeStatus.COMPLETED
        self.session.add(final_episode)
        self.session.commit()

        self.assertTrue(has_downloaded_final_episode(self.session, self.show))

    def test_live_absolute_release_updates_canonical_episode(self):
        release_title = "[SubsPlease] Sousou no Frieren - 23 (1080p) [A03549C5].mkv"
        self.show.total_episodes = 13
        self.session.add(EpisodeNumberMapping(monitored_id=1, feed_id=1, offset=11))
        mock_qbit = MagicMock()
        self._frieren_feed(mock_qbit, release_title)
        rule_name = "[Seasonal] Sousou no Frieren"
        mock_qbit.find_log_acceptances.return_value = {
            (rule_name, release_title): utc_now(),
        }

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)

        self.assertEqual(self.show.last_confirmed_episode, 12)
        history = self.session.exec(select(MatchHistory)).first()
        self.assertEqual(history.episode, 12)

    def test_an_older_mapped_release_never_moves_the_confirmed_episode_back(self):
        release_title = "[SubsPlease] Sousou no Frieren - 22 (1080p) [A03549C5].mkv"
        self.show.total_episodes = 13
        self.show.last_confirmed_episode = 12
        self.session.add(EpisodeNumberMapping(monitored_id=1, feed_id=1, offset=11))
        mock_qbit = MagicMock()
        self._frieren_feed(mock_qbit, release_title)
        rule_name = "[Seasonal] Sousou no Frieren"
        mock_qbit.find_log_acceptances.return_value = {
            (rule_name, release_title): utc_now(),
        }

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)

        self.assertEqual(self.show.last_confirmed_episode, 12)

    def test_history_row_falls_back_to_an_existing_torrent_when_log_rotated(self):
        mock_qbit = MagicMock()
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        self._frieren_feed(mock_qbit, release_title)
        mock_qbit.find_log_acceptances.return_value = {}

        added_on = utc_now() - timedelta(minutes=20)
        # Torrent names get reordered after download, so match structurally.
        renamed = MagicMock()
        renamed.name = "Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        renamed.added_on = added_on.timestamp()
        mock_qbit.get_torrents.return_value = [renamed]

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)

        m_hist = self.session.exec(select(MatchHistory)).all()
        self.assertEqual(len(m_hist), 1)
        self.assertAlmostEqual(
            m_hist[0].created_at.replace(tzinfo=None),
            added_on.replace(tzinfo=None),
            delta=timedelta(seconds=5),
        )

    def test_unrelated_torrent_is_not_treated_as_evidence(self):
        mock_qbit = MagicMock()
        release_title = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
        self._frieren_feed(mock_qbit, release_title)
        mock_qbit.find_log_acceptances.return_value = {}

        # Same episode number, different show: must not count as this release.
        other = MagicMock()
        other.name = "Sousou no Frieren Movie - 08 (1080p) [DEADBEEF].mkv"
        other.added_on = (utc_now() - timedelta(minutes=5)).timestamp()
        mock_qbit.get_torrents.return_value = [other]

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)

        self.assertEqual(self.session.exec(select(MatchHistory)).all(), [])

    def test_log_acceptance_lookup_requires_the_same_rule(self):
        from kisetsu.clients.qbit import QBitClient

        client = QBitClient(host="http://localhost:8080")
        underlying = MagicMock()
        underlying.log_main.return_value = [
            MagicMock(spec=["timestamp"], timestamp=1700000001),
            MagicMock(message="RSS article 'release-a' is accepted by rule 'rule-a'.", timestamp="invalid"),
            MagicMock(message="RSS article 'release-a' is accepted by rule 'rule-a'.", timestamp=1699999999),
            MagicMock(message="RSS article 'release-b' is accepted by rule 'other-rule'.", timestamp=1700000000),
        ]
        client.get_client = MagicMock(return_value=underlying)

        result = client.find_log_acceptances([
            ("rule-a", "release-a"),
            ("rule-b", "release-b"),
        ])

        self.assertIsNotNone(result[("rule-a", "release-a")])
        self.assertIsNone(result[("rule-b", "release-b")])
        underlying.log_main.assert_called_once()

    def test_log_acceptance_lookup_survives_an_unreadable_log(self):
        from kisetsu.clients.qbit import QBitClient

        client = QBitClient(host="http://localhost:8080")
        underlying = MagicMock()
        underlying.log_main.side_effect = RuntimeError("logs unavailable")
        client.get_client = MagicMock(return_value=underlying)

        result = client.find_log_acceptances([("rule-a", "release-a")])

        self.assertEqual(result, {("rule-a", "release-a"): None})

    def _split_arc_setup(self, show_id, **overrides):
        """Show armed on the #1 feed while the release only exists on the #2 feed."""
        from datetime import timedelta

        release_title = (
            "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 "
            "[1080p NF WEB-DL AVC AAC][MultiSub][78128421]"
        )
        subsplease = self.feed
        erai = Feed(id=2, qbit_feed_name="Erai-Raws 1080p", qbit_feed_url="https://nyaa.si/?page=rss&q=Erai-raws", priority=2)
        self.session.add(erai)
        fields = dict(
            id=show_id,
            anilist_id=174051,
            display_name="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            aliases_json=json.dumps(STEEL_BALL_RUN_ALIASES),
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=subsplease.id,
            qbit_rule_name="[Seasonal] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            total_episodes=24,
            next_airing_episode=2,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        fields.update(overrides)
        show = Monitored(**fields)
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        accepted_at = utc_now() - timedelta(hours=2)
        mock_qbit.find_log_acceptances.side_effect = lambda pairs: {p: accepted_at for p in pairs}
        released_at = accepted_at
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": subsplease.qbit_feed_url,
                "articles": [
                    {"title": "[SubsPlease] Link Click S3 - 08 (1080p) [FE7080C1].mkv", "torrentURL": "magnet:lc"},
                ],
            },
            "Erai-Raws 1080p": {
                "url": erai.qbit_feed_url,
                "articles": [
                    {
                        "title": release_title,
                        "torrentURL": "https://nyaa/2.torrent",
                        "date": released_at.strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    }
                ],
            },
        }
        return show, subsplease, erai, release_title, mock_qbit

    def test_confirmation_waits_five_minutes_then_moves_to_feed_that_carried_the_release(self):
        show, subsplease, erai, release_title, mock_qbit = self._split_arc_setup(3)

        first_logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        # First sighting only records the candidate and starts the grace clock.
        self.assertEqual(show.current_feed_id, subsplease.id)
        self.assertEqual(show.status, MonitoredStatus.UNCONFIRMED)
        self.assertEqual(show.candidate_feed_id, erai.id)
        self.assertIsNotNone(show.candidate_feed_since)
        self.assertTrue(any("waiting 5m" in log for log in first_logs))
        mock_qbit.set_rss_rule.assert_not_called()

        # Before the window is up nothing moves.
        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)
        self.assertEqual(show.current_feed_id, subsplease.id)
        mock_qbit.set_rss_rule.assert_not_called()

        # Once the five minutes are up the rule moves and learns the pattern.
        show.candidate_feed_since = show.candidate_feed_since - timedelta(seconds=301)
        self.session.add(show)
        self.session.commit()

        second_logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertTrue(any("moved rule" in log for log in second_logs))
        self.assertEqual(show.current_feed_id, erai.id)
        self.assertEqual(show.status, MonitoredStatus.FIXED)
        self.assertEqual(show.last_confirmed_episode, 2)
        self.assertEqual(show.matched_title, "JoJo no Kimyou na Bouken: Steel Ball Run")
        self.assertEqual(show.matched_release_group, "Erai-raws")
        self.assertIsNone(show.candidate_feed_id)
        self.assertIsNone(show.candidate_feed_since)

        moved = self.session.exec(select(RuleHistory).where(RuleHistory.monitored_id == 3)).all()
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].feed_id, erai.id)
        self.assertEqual(moved[0].outcome, RuleOutcome.CONFIRMED)

        written_defs = [call.kwargs["rule_def"] for call in mock_qbit.set_rss_rule.call_args_list]
        must_contain = written_defs[0]["mustContain"]
        self.assertEqual(written_defs[0]["affectedFeeds"], [erai.qbit_feed_url])
        self.assertTrue(re.search(must_contain, release_title, re.IGNORECASE))
        self.assertTrue(
            re.search(
                must_contain,
                "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 03 [1080p NF WEB-DL AVC AAC].mkv",
                re.IGNORECASE,
            )
        )

    def test_preferred_feed_wins_even_after_the_grace_window_expired(self):
        show, subsplease, erai, _, mock_qbit = self._split_arc_setup(4)

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)
        self.assertEqual(show.candidate_feed_id, erai.id)

        # The preferred feed finally posts the release, and the window has expired.
        show.candidate_feed_since = show.candidate_feed_since - timedelta(seconds=3600)
        self.session.add(show)
        self.session.commit()
        subsplease_release = "[SubsPlease] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE - 02 (1080p) [A1B2C3D4].mkv"
        mock_qbit.get_rss_items.return_value["SubsPlease"]["articles"] = [
            {"title": subsplease_release, "torrentURL": "magnet:sbr", "date": utc_now().strftime("%a, %d %b %Y %H:%M:%S +0000")},
        ]

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        # Stays on the preferred feed and forgets the candidate.
        self.assertEqual(show.current_feed_id, subsplease.id)
        self.assertEqual(show.status, MonitoredStatus.FIXED)
        self.assertIsNone(show.candidate_feed_id)
        self.assertIsNone(show.candidate_feed_since)

    def test_user_pinned_feed_is_never_moved(self):
        show, subsplease, erai, _, mock_qbit = self._split_arc_setup(5, feed_pinned=True)

        for _ in range(3):
            verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
            self.session.refresh(show)
            show.candidate_feed_since = show.candidate_feed_since - timedelta(seconds=600) if show.candidate_feed_since else None
            self.session.add(show)
            self.session.commit()

        self.assertEqual(show.current_feed_id, subsplease.id)
        self.assertEqual(show.status, MonitoredStatus.UNCONFIRMED)
        self.assertIsNone(show.candidate_feed_id)
        mock_qbit.set_rss_rule.assert_not_called()

    def test_confirmation_no_matching_article_remains_unconfirmed(self):
        mock_qbit = MagicMock()
        mock_qbit.get_rss_items.return_value = {
            "SubsPlease": {
                "url": "https://subsplease.org/rss/?r=1080",
                "articles": [
                    {"title": "[SubsPlease] Completely Unrelated Anime - 01 (1080p).mkv", "torrentURL": "https://subs/1.torrent"}
                ]
            }
        }

        verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(self.show)

        self.assertEqual(self.show.status, MonitoredStatus.UNCONFIRMED)

    def test_confirmation_learns_split_cour_release_and_rewrites_rule(self):
        import re

        release_title = (
            "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 "
            "[1080p NF WEB-DL AVC AAC][MultiSub][78128421]"
        )
        sbr_feed = Feed(id=2, qbit_feed_name="Erai-raws", qbit_feed_url="https://www.erai-raws.info/rss-1080p/", priority=2)
        self.session.add(sbr_feed)
        show = Monitored(
            id=2,
            anilist_id=174051,
            display_name="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            aliases_json=json.dumps(STEEL_BALL_RUN_ALIASES),
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=2,
            qbit_rule_name="[Seasonal] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            total_episodes=24,
            next_airing_episode=2,
        )
        self.session.add(show)
        self.session.commit()

        mock_qbit = MagicMock()
        accepted_at = utc_now() - timedelta(hours=2)
        mock_qbit.find_log_acceptances.side_effect = lambda pairs: {p: accepted_at for p in pairs}
        mock_qbit.get_rss_items.return_value = {
            "Erai-raws": {
                "url": "https://www.erai-raws.info/rss-1080p/",
                "articles": [
                    {
                        "title": release_title,
                        "torrentURL": "https://erai/2.torrent",
                        "date": "25 Sep 2026 12:00:00 +0000",
                    }
                ],
            }
        }

        logs = verify_and_confirm_torrents(self.session, mock_qbit, self.settings)
        self.session.refresh(show)

        self.assertEqual(show.status, MonitoredStatus.FIXED)
        self.assertEqual(show.last_confirmed_episode, 2)
        self.assertEqual(show.matched_title, "JoJo no Kimyou na Bouken: Steel Ball Run")
        self.assertEqual(show.matched_release_group, "Erai-raws")
        self.assertTrue(any("Confirmed rule" in log for log in logs))

        mock_qbit.set_rss_rule.assert_called_once()
        must_contain = mock_qbit.set_rss_rule.call_args.kwargs["rule_def"]["mustContain"]
        # The rewritten rule must match the release that just dropped, so
        # qBittorrent still downloads this very episode.
        self.assertTrue(re.search(must_contain, release_title, re.IGNORECASE))
        self.assertTrue(
            re.search(
                must_contain,
                "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 03 [1080p NF WEB-DL AVC AAC].mkv",
                re.IGNORECASE,
            )
        )
        self.assertFalse(
            re.search(must_contain, "[Erai-raws] Some Completely Different Show - 02 [1080p].mkv", re.IGNORECASE)
        )

        m_hist = self.session.exec(select(MatchHistory).where(MatchHistory.monitored_id == 2)).all()
        self.assertEqual(len(m_hist), 1)
        self.assertEqual(m_hist[0].release_title, release_title)
        self.assertEqual(m_hist[0].episode, 2)

    def test_canonical_episode_rejects_when_next_airing_in_future(self):
        from kisetsu.core.confirmation import _canonical_episode

        show = Monitored(
            anilist_id=210031,
            display_name="You and I Are Polar Opposites Season 2",
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            total_episodes=13,
            next_airing_episode=13,
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        self.session.add(EpisodeNumberMapping(monitored_id=show.id, feed_id=1, offset=11))
        self.session.commit()

        # Raw 24 with offset 11 maps to local 13, but AniList says 13 airs in 7 days -> must be rejected
        canonical = _canonical_episode(self.session, show, feed_id=1, raw_episode=24)
        self.assertIsNone(canonical)

    def test_canonical_episode_accepts_when_air_date_has_passed(self):
        from kisetsu.core.confirmation import _canonical_episode

        show = Monitored(
            anilist_id=210031,
            display_name="You and I Are Polar Opposites Season 2",
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            total_episodes=13,
            next_airing_episode=13,
            next_airing_at=utc_now() - timedelta(hours=2),
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        self.session.add(EpisodeNumberMapping(monitored_id=show.id, feed_id=1, offset=11))
        self.session.commit()

        canonical = _canonical_episode(self.session, show, feed_id=1, raw_episode=24)
        self.assertEqual(canonical, 13)

    def test_has_downloaded_final_episode_ledger_incomplete(self):
        from kisetsu.core.confirmation import has_downloaded_final_episode

        show = Monitored(
            anilist_id=210031,
            display_name="Incomplete Show",
            status=MonitoredStatus.FIXED,
            total_episodes=13,
            last_confirmed_episode=13,
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        self.session.add(Episode(monitored_id=show.id, episode_number=1, status=EpisodeStatus.WANTED))
        self.session.add(Episode(monitored_id=show.id, episode_number=13, status=EpisodeStatus.COMPLETED))
        self.session.commit()

        # Even though last_confirmed is 13 and ep 13 is completed, ep 1 is WANTED -> not finished
        self.assertFalse(has_downloaded_final_episode(self.session, show))

    def test_has_downloaded_final_episode_empty_ledger_respects_schedule(self):
        from kisetsu.core.confirmation import has_downloaded_final_episode

        # Empty ledger (no Episode rows exist)
        show = Monitored(
            anilist_id=210031,
            display_name="Polar Opposites",
            status=MonitoredStatus.FIXED,
            total_episodes=13,
            last_confirmed_episode=13,
            next_airing_episode=13,  # Episode 13 is scheduled next in the future -> finale has not aired
            next_airing_at=utc_now() + timedelta(days=7),
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        self.assertFalse(has_downloaded_final_episode(self.session, show))

    def test_has_downloaded_final_episode_handles_legacy_raw(self):
        from kisetsu.core.confirmation import has_downloaded_final_episode

        # Show with raw last_confirmed_episode (e.g. 23) and offset 11, total 12
        show = Monitored(
            anilist_id=210031,
            display_name="Legacy Show",
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            total_episodes=12,
            last_confirmed_episode=23,
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        self.session.add(EpisodeNumberMapping(monitored_id=show.id, feed_id=1, offset=11))
        self.session.commit()

        # 23 - 11 = 12 == total_episodes -> True and canonicalized
        self.assertTrue(has_downloaded_final_episode(self.session, show))
        self.session.refresh(show)
        self.assertEqual(show.last_confirmed_episode, 12)

    def test_has_downloaded_final_episode_requires_every_episode_of_the_season(self):
        """Episodes with no ledger row were never downloaded, so they block completion."""
        from kisetsu.core.confirmation import has_downloaded_final_episode

        show = Monitored(
            anilist_id=210032,
            display_name="Tracked Late",
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            total_episodes=13,
            last_confirmed_episode=13,
        )
        self.session.add(show)
        self.session.commit()
        self.session.refresh(show)

        for episode_number in (11, 12, 13):
            self.session.add(Episode(
                monitored_id=show.id,
                episode_number=episode_number,
                status=EpisodeStatus.COMPLETED,
            ))
        self.session.commit()

        self.assertFalse(has_downloaded_final_episode(self.session, show))

        for episode_number in range(1, 11):
            self.session.add(Episode(
                monitored_id=show.id,
                episode_number=episode_number,
                status=EpisodeStatus.COMPLETED,
            ))
        self.session.commit()

        self.assertTrue(has_downloaded_final_episode(self.session, show))


if __name__ == "__main__":
    unittest.main()
