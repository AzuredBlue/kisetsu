from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.core.supervisor import Supervisor
from qbit_seasonal_anime.clients.anilist import AniListClient
from qbit_seasonal_anime.clients.qbit import QbitClientError, QbitConnectionError
from qbit_seasonal_anime.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    MatchHistory,
    Monitored,
    MonitoredStatus,
    RuleHistory,
    RuleOutcome,
    Settings,
    utc_now,
)
from qbit_seasonal_anime.db.session import get_settings
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


@pytest.mark.asyncio
async def test_supervisor_full_cycle():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, default_category="Anime", anilist_username="TestUser", base_dir="/tmp/Anime")
    session.add(settings)

    show = Monitored(
        id=1,
        anilist_id=154587,
        display_name="Sousou no Frieren",
        aliases_json='["Sousou no Frieren", "Frieren"]',
        status=MonitoredStatus.UNCONFIRMED,
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_qbit.get_rss_feeds_flat.return_value = [
        {"name": "SubsPlease", "url": "https://subsplease.org/rss/?r=1080"},
        {"name": "Erai-raws", "url": "https://www.erai-raws.info/rss-1080p/"},
    ]
    mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])

    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=settings)

    logs = await supervisor.run_full_cycle()
    full_rss_calls = [
        call for call in mock_qbit.get_rss_items.call_args_list
        if call.kwargs.get("with_data") is True
    ]
    assert len(full_rss_calls) == 1
    session.refresh(show)

    feeds = session.exec(select(Feed)).all()
    assert len(feeds) == 2

    assert show.status == MonitoredStatus.FIXED
    assert show.current_feed_id is not None
    assert show.last_confirmed_episode is None

    hist = session.exec(select(RuleHistory)).all()
    assert len(hist) >= 1
    assert any(h.outcome == RuleOutcome.CONFIRMED for h in hist)


@pytest.mark.asyncio
async def test_anilist_sync_leaves_hand_written_aliases_alone():
    """AniList re-supplies its own aliases and must not absorb the hand-written ones."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, anilist_username="TestUser", base_dir="/tmp/Anime")
    show = Monitored(
        id=1,
        anilist_id=191788,
        display_name="Aoashi Season 2",
        aliases_json='["Aoashi Season 2"]',
        custom_aliases_json='["Ao Ashi"]',
        status=MonitoredStatus.FIXED,
        total_episodes=24,
    )
    session.add(settings)
    session.add(show)
    session.commit()

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
        "anilist_id": 191788,
        "display_name": "Aoashi Season 2",
        "status": "RELEASING",
        "total_episodes": 24,
        "next_airing_episode": 1,
        "next_airing_at": None,
        "aliases": ["Aoashi Season 2", "アオアシ 第2期"],
    }])
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=mock_anilist, settings=settings)

    await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.custom_aliases == ["Ao Ashi"]
    assert "アオアシ 第2期" in show.aliases
    assert "Ao Ashi" not in show.aliases
    assert "Ao Ashi" in show.effective_aliases


@pytest.mark.asyncio
async def test_anilist_reopens_stale_completed_show_when_finale_is_not_confirmed():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, anilist_username="TestUser", base_dir="/tmp/Anime")
    show = Monitored(
        id=1,
        anilist_id=154587,
        display_name="Sousou no Frieren",
        aliases_json='["Sousou no Frieren"]',
        status=MonitoredStatus.COMPLETED,
        total_episodes=13,
        next_airing_episode=12,
        last_confirmed_episode=23,
    )
    session.add(settings)
    session.add(show)
    session.commit()

    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
        "anilist_id": 154587,
        "display_name": "Sousou no Frieren",
        "status": "RELEASING",
        "total_episodes": 13,
        "next_airing_episode": 12,
        "next_airing_at": None,
    }])
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=mock_anilist, settings=settings)

    await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 12


MUSHOKU_RULE = "[Seasonal] Mushoku Tensei - Jobless Reincarnation Season 3"


def _anilist_completed_fixture():
    """A working show whose finale was downloaded but never confirmed (last_confirmed < total)."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    session.add(Settings(id=1, anilist_username="TestUser", base_dir="/tmp/Anime"))
    show = Monitored(
        id=8,
        anilist_id=178789,
        display_name="Mushoku Tensei: Jobless Reincarnation Season 3",
        aliases_json='["Mushoku Tensei S3"]',
        status=MonitoredStatus.FIXED,
        total_episodes=14,
        next_airing_episode=14,
        next_airing_at=utc_now() - timedelta(hours=2),
        last_confirmed_episode=13,
        qbit_rule_name=MUSHOKU_RULE,
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_qbit.get_rss_rules.return_value = {MUSHOKU_RULE: {"enabled": True}}

    return session, show, mock_qbit


def _anilist_payload(**overrides):
    payload = {
        "anilist_id": 178789,
        "display_name": "Mushoku Tensei: Jobless Reincarnation Season 3",
        "list_status": "COMPLETED",
        "status": "RELEASING",
        "total_episodes": 14,
        "next_airing_episode": 14,
        "next_airing_at": utc_now(),
        "season": "SUMMER",
        "season_year": 2026,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_anilist_completed_list_status_completes_show_without_finale_confirmed():
    """The user finishing the show on AniList stands the rule down even if we never confirmed the finale."""
    session, show, mock_qbit = _anilist_completed_fixture()
    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[_anilist_payload()])
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=get_settings(session))

    logs = await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.status == MonitoredStatus.COMPLETED
    assert show.next_airing_episode is None
    assert show.next_airing_at is None
    assert show.last_confirmed_episode == 13  # untouched: AniList does not prove the download
    assert any("marked COMPLETED on AniList" in l for l in logs)
    mock_qbit.set_rss_rule.assert_called_with(rule_name=MUSHOKU_RULE, rule_def={"enabled": False})


@pytest.mark.asyncio
async def test_anilist_completion_survives_the_next_cycle():
    """The re-open guard must not undo a completion the user asked for on AniList."""
    session, show, mock_qbit = _anilist_completed_fixture()
    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[_anilist_payload()])
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=get_settings(session))

    await supervisor.sync_anilist_schedule()
    await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.status == MonitoredStatus.COMPLETED


@pytest.mark.asyncio
async def test_anilist_completion_reverts_when_list_status_goes_back_to_current():
    """Symmetric: moving the list entry off COMPLETED re-opens the show and restores the air date."""
    session, show, mock_qbit = _anilist_completed_fixture()
    mock_anilist = MagicMock()
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[_anilist_payload()])
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=mock_anilist, settings=get_settings(session))
    await supervisor.sync_anilist_schedule()

    airing_soon = utc_now() + timedelta(days=7)
    mock_anilist.fetch_user_seasonal_anime = AsyncMock(
        return_value=[_anilist_payload(list_status="CURRENT", next_airing_episode=14, next_airing_at=airing_soon)]
    )
    await supervisor.sync_anilist_schedule()
    session.refresh(show)

    assert show.status == MonitoredStatus.FIXED
    assert show.next_airing_episode == 14
    assert show.next_airing_at is not None


def _rule_sync_fixture(n_shows):
    """Engine + session with n monitored shows that each need a rule written."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime")
    session.add(settings)

    shows = []
    for i in range(n_shows):
        feed = Feed(name=f"Feed {i}", qbit_feed_url=f"https://example.com/{i}/rss", qbit_feed_name=f"Feed {i}")
        session.add(feed)
        session.commit()
        session.refresh(feed)
        show = Monitored(
            anilist_id=1000 + i,
            display_name=f"Show {i}",
            aliases_json=f'["Show {i}"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=feed.id,
        )
        session.add(show)
        shows.append(show)
    session.commit()
    for show in shows:
        session.refresh(show)
    return session, settings, shows


def test_sync_active_rules_stops_at_the_first_lost_connection():
    """A dead socket fails every remaining rule, so emit one warning and stop."""
    session, settings, shows = _rule_sync_fixture(4)

    qbit = MagicMock()
    qbit.get_client.return_value.rss_rules.return_value = {}
    qbit.set_rss_rule.side_effect = QbitConnectionError("connection reset by peer")

    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    with pytest.raises(QbitConnectionError):
        supervisor.sync_active_rules()

    assert qbit.set_rss_rule.call_count == 1


def test_sync_active_rules_keeps_going_for_rule_specific_failures():
    """A single malformed rule must not block the others."""
    session, settings, shows = _rule_sync_fixture(4)

    qbit = MagicMock()
    qbit.get_client.return_value.rss_rules.return_value = {}
    qbit.set_rss_rule.side_effect = [QbitClientError("bad regex"), None, None, None]

    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = supervisor.sync_active_rules()

    assert qbit.set_rss_rule.call_count == 4
    assert sum(1 for l in logs if "Failed updating rule" in l) == 1
    assert any("Synchronized 3 updated rules" in l for l in logs)


def test_sync_active_rules_commits_rules_written_before_a_lost_connection():
    """A rule that made it into qBittorrent must stay recorded in the database."""
    session, settings, shows = _rule_sync_fixture(3)

    qbit = MagicMock()
    qbit.get_client.return_value.rss_rules.return_value = {}
    qbit.set_rss_rule.side_effect = [None, QbitConnectionError("connection reset by peer")]

    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    with pytest.raises(QbitConnectionError):
        supervisor.sync_active_rules()

    session.expire_all()
    committed = [s.qbit_rule_name for s in session.exec(select(Monitored)).all()]
    assert committed[0] is not None
    assert committed[1] is None


def _throttle_fixture(refresh_interval_minutes=360):
    """Real AniListClient so the cadence stamp is genuine."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, anilist_username="TestUser", refresh_interval_minutes=refresh_interval_minutes)
    session.add(settings)
    session.add(
        Monitored(
            anilist_id=4242,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
        )
    )
    session.commit()

    anilist = AniListClient()
    anilist.last_sync_at = utc_now()
    return session, settings, anilist


@pytest.mark.asyncio
async def test_anilist_sync_is_deferred_inside_the_refresh_window():
    """No AniList request while the cached schedule is still inside its window."""
    session, settings, anilist = _throttle_fixture()
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

    with patch.object(anilist, "_post_query", new_callable=AsyncMock) as mock_post:
        logs = await supervisor.sync_anilist_schedule()

    mock_post.assert_not_called()
    assert len(logs) == 1
    assert "deferred" in logs[0]
    assert "refresh interval 360m" in logs[0]


@pytest.mark.asyncio
async def test_forced_anilist_sync_ignores_the_refresh_window():
    """force=True ignores the window."""
    session, settings, anilist = _throttle_fixture()
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

    with patch.object(anilist, "_post_query", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"MediaListCollection": {"lists": []}}
        logs = await supervisor.sync_anilist_schedule(force=True)

    mock_post.assert_awaited_once()
    assert not any("deferred" in l for l in logs)


@pytest.mark.asyncio
async def test_anilist_sync_runs_again_once_the_window_expires():
    session, settings, anilist = _throttle_fixture(refresh_interval_minutes=10)
    anilist.last_sync_at = utc_now() - timedelta(minutes=11)
    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

    with patch.object(anilist, "_post_query", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = {"MediaListCollection": {"lists": []}}
        logs = await supervisor.sync_anilist_schedule()

    mock_post.assert_awaited_once()
    assert not any("deferred" in l for l in logs)


@pytest.mark.asyncio
async def test_hunting_cycle_defers_anilist_but_still_verifies_and_syncs_rules():
    """A hunting pass still discovers rules and confirms releases."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    settings = Settings(id=1, anilist_username="TestUser", default_category="Anime", base_dir="/tmp/Anime")
    session.add(settings)
    show = Monitored(
        anilist_id=154587,
        display_name="Sousou no Frieren",
        aliases_json='["Sousou no Frieren", "Frieren"]',
        status=MonitoredStatus.UNCONFIRMED,
        next_airing_episode=1,
        next_airing_at=utc_now() - timedelta(minutes=15),
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    mock_qbit.get_rss_feeds_flat.return_value = [{"name": "SubsPlease", "url": "https://subsplease.org/rss/?r=1080"}]
    mock_qbit.get_rss_items.return_value = MOCK_QBIT_RSS_ITEMS
    mock_qbit.get_client.return_value.rss_rules.return_value = {}

    anilist = AniListClient()
    anilist.last_sync_at = utc_now()
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=anilist, settings=settings)

    with patch.object(anilist, "_post_query", new_callable=AsyncMock) as mock_post:
        logs = await supervisor.run_full_cycle(hunting=True)

    mock_post.assert_not_called()
    assert any("deferred (hunting pass)" in l for l in logs)
    assert any("Created verified rule for 'Sousou no Frieren'" in l for l in logs)
    assert any(l.startswith("Summary:") for l in logs)
    session.refresh(show)
    assert show.status == MonitoredStatus.FIXED


def test_prune_past_season_preserves_non_completed_show():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    # Show from 2020 (definitely past season), status FIXED, last_confirmed == total
    show = Monitored(
        id=15,
        anilist_id=210031,
        display_name="You and I Are Polar Opposites Season 2",
        season_name="SUMMER",
        season_year=2020,
        status=MonitoredStatus.FIXED,
        total_episodes=13,
        last_confirmed_episode=13,
        next_airing_at=utc_now() - timedelta(hours=2),
        qbit_rule_name="[Seasonal] Polar Opposites",
    )
    session.add(show)
    session.commit()

    mock_qbit = MagicMock()
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=MagicMock(), settings=Settings(id=1))
    logs = supervisor.prune_past_season_shows()

    assert logs == []
    mock_qbit.remove_rss_rule.assert_not_called()
    assert session.get(Monitored, 15) is not None


def test_prune_past_season_preserves_show_within_airing_grace_period():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    # Completed show whose air_at was just 2 hours ago (within 7-day grace period)
    show = Monitored(
        id=16,
        anilist_id=210032,
        display_name="Recently Aired Show",
        season_name="SUMMER",
        season_year=2020,
        status=MonitoredStatus.COMPLETED,
        total_episodes=12,
        last_confirmed_episode=12,
        next_airing_at=utc_now() - timedelta(hours=2),
    )
    session.add(show)
    session.commit()

    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=MagicMock(), settings=Settings(id=1))
    logs = supervisor.prune_past_season_shows()

    assert logs == []
    assert session.get(Monitored, 16) is not None


def test_prune_past_season_preserves_show_with_wanted_episodes():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    show = Monitored(
        id=17,
        anilist_id=210033,
        display_name="Show With Missing Episodes",
        season_name="SUMMER",
        season_year=2020,
        status=MonitoredStatus.COMPLETED,
        total_episodes=13,
        last_confirmed_episode=13,
        next_airing_at=utc_now() - timedelta(days=30),
    )
    session.add(show)
    session.commit()

    # Episode 1 is still WANTED
    session.add(Episode(monitored_id=17, episode_number=1, status=EpisodeStatus.WANTED))
    session.add(Episode(monitored_id=17, episode_number=13, status=EpisodeStatus.COMPLETED))
    session.commit()

    supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=MagicMock(), settings=Settings(id=1))
    logs = supervisor.prune_past_season_shows()

    assert logs == []
    assert session.get(Monitored, 17) is not None


def test_prune_past_season_prunes_completed_show_and_cleans_up_records():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://feed.url", priority=1)
    session.add(feed)

    show = Monitored(
        id=18,
        anilist_id=210034,
        display_name="Truly Completed Past Season Show",
        season_name="SUMMER",
        season_year=2020,
        status=MonitoredStatus.COMPLETED,
        total_episodes=12,
        last_confirmed_episode=12,
        next_airing_at=utc_now() - timedelta(days=30),
        qbit_rule_name="[Seasonal] Truly Completed Show",
    )
    session.add(show)
    session.commit()

    # Add associated records: Episode, EpisodeNumberMapping, RuleHistory, MatchHistory
    session.add(Episode(monitored_id=18, episode_number=12, status=EpisodeStatus.COMPLETED))
    session.add(EpisodeNumberMapping(monitored_id=18, feed_id=1, offset=0))
    session.add(RuleHistory(monitored_id=18, feed_id=1, outcome=RuleOutcome.CONFIRMED))
    session.add(MatchHistory(monitored_id=18, show_name=show.display_name, rule_name="[Seasonal] Truly Completed Show", release_title="Rel 12", episode=12))
    session.commit()

    mock_qbit = MagicMock()
    supervisor = Supervisor(session=session, qbit=mock_qbit, anilist=MagicMock(), settings=Settings(id=1))
    logs = supervisor.prune_past_season_shows()

    assert len(logs) == 1
    assert "Pruned completed show 'Truly Completed Past Season Show'" in logs[0]
    mock_qbit.remove_rss_rule.assert_called_once_with(rule_name="[Seasonal] Truly Completed Show")

    assert session.get(Monitored, 18) is None
    assert session.exec(select(Episode).where(Episode.monitored_id == 18)).all() == []
    assert session.exec(select(EpisodeNumberMapping).where(EpisodeNumberMapping.monitored_id == 18)).all() == []
    assert session.exec(select(RuleHistory).where(RuleHistory.monitored_id == 18)).all() == []
    # Verify no leaked MatchHistory rows
    assert session.exec(select(MatchHistory).where(MatchHistory.monitored_id == 18)).all() == []
    assert session.exec(select(MatchHistory).where(MatchHistory.monitored_id == None)).all() == []

