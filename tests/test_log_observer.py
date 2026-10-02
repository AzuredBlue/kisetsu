import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine, select

from qbit_seasonal_anime.clients.qbit import QbitClientError
from qbit_seasonal_anime.core.confirmation import ingest_log_acceptances, parse_acceptance_log_line
from qbit_seasonal_anime.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    MatchHistory,
    Monitored,
    MonitoredStatus,
    QbitRuleWatermark,
    Settings,
)

POLAR_RULE = "[Seasonal] You and I Are Polar Opposites Season 2"
POLAR_EP24 = "RSS article '[SubsPlease] Seihantai na Kimi to Boku - 24 (1080p) [78085A98].mkv' " \
             f"is accepted by rule '{POLAR_RULE}'. Trying to add torrent..."


def _entry(message, entry_id=1, when=None):
    return (entry_id, message, when or datetime(2026, 9, 27, 8, 33, 58, tzinfo=timezone.utc))


class TestParseAcceptanceLogLine(unittest.TestCase):
    def test_extracts_title_and_rule(self):
        self.assertEqual(
            parse_acceptance_log_line(POLAR_EP24),
            ("[SubsPlease] Seihantai na Kimi to Boku - 24 (1080p) [78085A98].mkv", POLAR_RULE),
        )

    def test_ignores_unrelated_lines(self):
        self.assertIsNone(parse_acceptance_log_line("RSS feed updated. Added 0 new articles."))
        self.assertIsNone(parse_acceptance_log_line(""))


class TestIngestLogAcceptances(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        self.feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss/?r=1080", priority=1)
        self.session.add(self.feed)

        self.show = Monitored(
            id=1,
            anilist_id=1,
            display_name="You and I Are Polar Opposites Season 2",
            aliases_json='["Seihantai na Kimi to Boku"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            qbit_rule_name=POLAR_RULE,
            save_folder="Polar Opposites",
            total_episodes=13,
            last_confirmed_episode=12,
        )
        self.session.add(self.show)
        self.session.commit()

        self.session.add(EpisodeNumberMapping(monitored_id=1, feed_id=1, offset=11, source="CONFIRMED"))
        self.session.commit()

        for number in range(1, 14):
            self.session.add(Episode(monitored_id=1, episode_number=number, status=EpisodeStatus.WANTED))
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _episode(self, number):
        return self.session.exec(
            select(Episode).where(Episode.monitored_id == 1, Episode.episode_number == number)
        ).first()

    def test_records_match_and_completes_episode(self):
        logs = ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])

        self.assertEqual(len(logs), 1)
        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)

        episode = self._episode(13)
        self.assertEqual(episode.status, EpisodeStatus.COMPLETED)
        self.assertEqual(episode.source_episode, 24)
        self.assertEqual(episode.release_title, "[SubsPlease] Seihantai na Kimi to Boku - 24 (1080p) [78085A98].mkv")

        history = self.session.exec(select(MatchHistory)).all()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].episode, 13)
        self.assertEqual(history[0].feed_name, "SubsPlease")
        self.assertEqual(history[0].created_at, datetime(2026, 9, 27, 8, 33, 58))

    def test_reingesting_same_buffer_is_a_no_op(self):
        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])
        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])
        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])

        self.assertEqual(len(self.session.exec(select(MatchHistory)).all()), 1)
        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)

    def test_survives_match_history_retention_pruning(self):
        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])

        for row in self.session.exec(select(MatchHistory)).all():
            self.session.delete(row)
        self.session.commit()

        self.assertEqual(ingest_log_acceptances(self.session, [_entry(POLAR_EP24)]), [])

        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)

    def test_never_rewinds_last_confirmed_episode(self):
        older = (
            1,
            "RSS article '[SubsPlease] Seihantai na Kimi to Boku - 22 (1080p) [728FF942].mkv' "
            f"is accepted by rule '{POLAR_RULE}'. Trying to add torrent...",
            datetime(2026, 9, 6, 17, 25, 1, tzinfo=timezone.utc),
        )

        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)])
        self.assertEqual(ingest_log_acceptances(self.session, [older]), [])

        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)
        self.assertEqual(self._episode(11).status, EpisodeStatus.WANTED)

    def test_older_entry_in_same_pass_does_not_regress_a_newer_one(self):
        older = (
            1,
            "RSS article '[SubsPlease] Seihantai na Kimi to Boku - 22 (1080p) [728FF942].mkv' "
            f"is accepted by rule '{POLAR_RULE}'. Trying to add torrent...",
            datetime(2026, 9, 6, 17, 25, 1, tzinfo=timezone.utc),
        )

        logs = ingest_log_acceptances(self.session, [_entry(POLAR_EP24, entry_id=2), older])

        self.assertEqual(len(logs), 1)
        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)
        self.assertEqual(self._episode(11).status, EpisodeStatus.WANTED)

    def test_ignores_rules_we_do_not_manage(self):
        foreign = _entry(
            "RSS article '[SubsPlease] Some Other Show - 01 (1080p) [ABCDEF01].mkv' "
            "is accepted by rule '[Seasonal] Something Else'. Trying to add torrent...",
            entry_id=2,
        )
        self.assertEqual(ingest_log_acceptances(self.session, [foreign]), [])
        self.assertEqual(self.session.exec(select(MatchHistory)).all(), [])

    def test_records_each_accepted_release_in_order(self):
        ep25 = (
            2,
            "RSS article '[SubsPlease] Seihantai na Kimi to Boku - 25 (1080p) [11223344].mkv' "
            f"is accepted by rule '{POLAR_RULE}'. Trying to add torrent...",
            datetime(2026, 9, 28, 8, 33, 58, tzinfo=timezone.utc),
        )

        self.assertEqual(len(ingest_log_acceptances(self.session, [_entry(POLAR_EP24), ep25])), 1)

        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 13)
        self.assertEqual(self._episode(13).source_episode, 24)
        self.assertEqual(len(self.session.exec(select(MatchHistory)).all()), 1)

    def test_uses_rule_pattern_for_recorded_regex(self):
        ingest_log_acceptances(self.session, [_entry(POLAR_EP24)], {POLAR_RULE: "Seihantai.*"})

        history = self.session.exec(select(MatchHistory)).first()
        self.assertEqual(history.matched_regex, "Seihantai.*")

    def test_episode_outside_mapping_is_not_recorded(self):
        out_of_range = _entry(
            "RSS article '[SubsPlease] Seihantai na Kimi to Boku - 99 (1080p) [FFFFFFFF].mkv' "
            f"is accepted by rule '{POLAR_RULE}'. Trying to add torrent...",
            entry_id=3,
        )
        self.assertEqual(ingest_log_acceptances(self.session, [out_of_range]), [])
        self.session.refresh(self.show)
        self.assertEqual(self.show.last_confirmed_episode, 12)


class TestRuleObserver(unittest.IsolatedAsyncioTestCase):
    RULE = "[Seasonal] A"
    MARKER = "27 Sep 2026 01:32:05 +0000"

    def setUp(self):
        from qbit_seasonal_anime.server.state import state

        state.log_wake_event.clear()
        self.engine = create_engine("sqlite:///:memory:")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as session:
            session.add(Settings(id=1, qbit_host="http://qbit:8080"))
            session.commit()

    def _watermarks(self):
        with Session(self.engine) as session:
            return {w.rule_name: w.last_match for w in session.exec(select(QbitRuleWatermark)).all()}

    def _seed_watermark(self, value):
        with Session(self.engine) as session:
            session.add(QbitRuleWatermark(rule_name=self.RULE, last_match=value))
            session.commit()

    async def _run(self, client, iterations=2):
        from qbit_seasonal_anime.server.app import qbit_rule_observer_task
        from qbit_seasonal_anime.server.state import state

        calls = {"markers": 0}

        def markers():
            calls["markers"] += 1
            if calls["markers"] > iterations:
                raise asyncio.CancelledError()
            state.log_wake_event.set()
            return {self.RULE: self.MARKER}

        client.get_rule_match_markers.side_effect = markers
        client.get_rule_patterns.return_value = {}

        with patch("qbit_seasonal_anime.server.app.get_engine", return_value=self.engine), \
                patch("qbit_seasonal_anime.server.app.QBitClient", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                await qbit_rule_observer_task()

        return calls

    async def test_reads_the_log_once_then_settles(self):
        client = MagicMock()
        client.fetch_log_entries.return_value = []

        calls = await self._run(client)

        self.assertGreater(calls["markers"], 1)
        client.fetch_log_entries.assert_called_once()
        self.assertEqual(self._watermarks(), {self.RULE: self.MARKER})

    async def test_skips_the_log_when_the_watermark_already_matches(self):
        self._seed_watermark(self.MARKER)
        client = MagicMock()
        client.fetch_log_entries.side_effect = AssertionError("log must not be read")

        calls = await self._run(client)

        self.assertGreater(calls["markers"], 1)
        client.fetch_log_entries.assert_not_called()

    async def test_keeps_retrying_when_the_log_read_fails(self):
        client = MagicMock()
        client.fetch_log_entries.side_effect = QbitClientError("qBittorrent went away")

        calls = await self._run(client, iterations=3)

        self.assertGreater(calls["markers"], 2)
        self.assertEqual(self._watermarks(), {})
