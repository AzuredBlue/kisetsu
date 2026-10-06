from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from kisetsu.core.supervisor import FEED_SWITCH_GRACE_SECONDS, Supervisor
from kisetsu.clients.anilist import AniListClient
from kisetsu.clients.qbit import QbitClientError, QbitConnectionError
from kisetsu.db.models import (
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
from kisetsu.db.session import get_settings
from tests.fixtures import MOCK_QBIT_RSS_ITEMS


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

    await supervisor.run_full_cycle()
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

@pytest.mark.asyncio
async def test_sync_anilist_persists_per_episode_airing_schedule():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO"]',
            status=MonitoredStatus.FIXED,
            total_episodes=20,
            next_airing_episode=20,
            next_airing_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        session.add(settings)
        session.add(show)
        session.commit()
        now = datetime.now(timezone.utc)
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
            "anilist_id": 42,
            "display_name": "Re:ZERO Season 4",
            "title_romaji": "Re:ZERO Season 4",
            "title_english": "Re:ZERO Season 4",
            "aliases": ["Re:ZERO"],
            "status": "RELEASING",
            "total_episodes": 20,
            "next_airing_episode": 20,
            "next_airing_at": now + timedelta(days=7),
            "season": "FALL",
            "season_year": 2026,
        }])
        anilist.fetch_media_airing_schedules = AsyncMock(return_value={
            42: [
                {"episode": 17, "airing_at": now - timedelta(days=21)},
                {"episode": 18, "airing_at": now - timedelta(days=14)},
                {"episode": 19, "airing_at": now - timedelta(days=7)},
                {"episode": 20, "airing_at": now + timedelta(days=7)},
            ]
        })
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        await supervisor.sync_anilist_schedule(direct_mode=True)

        episodes = session.exec(
            select(Episode).where(
                Episode.monitored_id == show.id,
                Episode.episode_number.in_([17, 18, 19, 20]),
            )
        ).all()
        assert supervisor.anilist_sync_succeeded is True
        assert show.anilist_status == "RELEASING"
        assert show.schedule_stale is False
        # The direct engine gates every backfill on a known air date, so the
        # per-episode schedule is what it acts on.
        assert {episode.episode_number: episode.schedule_state for episode in episodes} == {
            17: "aired",
            18: "aired",
            19: "aired",
            20: "scheduled",
        }
        anilist.fetch_media_airing_schedules.assert_awaited_once_with([42])
    engine.dispose()


@pytest.mark.asyncio
async def test_stale_episode_schedule_timestamp_is_compared_without_tz_error():
    """A schedule synced on a previous run comes back naive from SQLite and must still compare."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO"]',
            status=MonitoredStatus.FIXED,
            total_episodes=20,
            next_airing_episode=20,
            next_airing_at=datetime.now(timezone.utc) + timedelta(days=7),
            # Written by an earlier run: SQLite persists this without an offset.
            schedule_synced_at=(datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None),
        )
        session.add(settings)
        session.add(show)
        session.commit()
        session.refresh(show)
        assert show.schedule_synced_at.tzinfo is None

        now = datetime.now(timezone.utc)
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
            "anilist_id": 42,
            "display_name": "Re:ZERO Season 4",
            "title_romaji": "Re:ZERO Season 4",
            "title_english": "Re:ZERO Season 4",
            "aliases": ["Re:ZERO"],
            "status": "RELEASING",
            "total_episodes": 20,
            "next_airing_episode": 20,
            "next_airing_at": now + timedelta(days=7),
            "season": "FALL",
            "season_year": 2026,
        }])
        anilist.fetch_media_airing_schedules = AsyncMock(return_value={
            42: [{"episode": 20, "airing_at": now + timedelta(days=7)}],
        })
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        await supervisor.sync_anilist_schedule(direct_mode=True)

        # Older than the 6h refresh window, so it is fetched again rather than skipped.
        anilist.fetch_media_airing_schedules.assert_awaited_once_with([42])
        session.refresh(show)
        assert show.schedule_stale is False
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_anilist_skips_episode_schedules_outside_direct_mode():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO"]',
            status=MonitoredStatus.FIXED,
            total_episodes=20,
            next_airing_episode=20,
            next_airing_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        session.add(settings)
        session.add(show)
        session.commit()
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])
        anilist.fetch_media_airing_schedules = AsyncMock(return_value={})
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        await supervisor.sync_anilist_schedule(direct_mode=False)

        # Rules mode has no episode ledger to fill, so the extra query (one per
        # show) is never paid for.
        anilist.fetch_media_airing_schedules.assert_not_awaited()
    engine.dispose()


@pytest.mark.asyncio
async def test_user_completed_show_is_never_reopened_by_a_still_airing_schedule():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(timezone.utc)
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Completed But Scheduled",
            aliases_json='["Completed But Scheduled"]',
            status=MonitoredStatus.COMPLETED,
            total_episodes=12,
            next_airing_episode=13,
            next_airing_at=now + timedelta(days=7),
        )
        session.add(settings)
        session.add(show)
        session.commit()
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[{
            "anilist_id": 42,
            "display_name": "Completed But Scheduled",
            "aliases": ["Completed But Scheduled"],
            # The user marked it COMPLETED on their list while the broadcast
            # status still says RELEASING.
            "list_status": "COMPLETED",
            "status": "RELEASING",
            "total_episodes": 13,
            "next_airing_episode": 13,
            "next_airing_at": now + timedelta(days=7),
        }])
        anilist.fetch_media_airing_schedules = AsyncMock(return_value={})
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        await supervisor.sync_anilist_schedule(direct_mode=True)

        session.refresh(show)
        # A deliberate user completion stands, whatever the schedule says.
        assert show.status == MonitoredStatus.COMPLETED


@pytest.mark.asyncio
async def test_sync_anilist_failure_marks_schedule_stale_without_erasing_pointer():
    from kisetsu.clients.anilist import AniListError

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        air_at = datetime.now(timezone.utc) + timedelta(days=7)
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO"]',
            status=MonitoredStatus.FIXED,
            total_episodes=20,
            next_airing_episode=20,
            next_airing_at=air_at,
            schedule_stale=False,
        )
        session.add(settings)
        session.add(show)
        session.commit()
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(side_effect=AniListError("offline"))
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        logs = await supervisor.sync_anilist_schedule()

        session.refresh(show)
        assert "AniList schedule sync error" in logs[0]
        assert supervisor.anilist_sync_succeeded is False
        # A failed sync must not read as "the show moved on": the last known
        # pointer is kept, and the data is flagged as not fresh.
        assert show.schedule_stale is True
        assert show.next_airing_episode == 20
        assert show.next_airing_at.replace(tzinfo=timezone.utc) == air_at
    engine.dispose()


@pytest.mark.asyncio
async def test_sync_anilist_deferral_does_not_block_the_rollover():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, anilist_username="TestUser")
        show = Monitored(
            id=1,
            anilist_id=42,
            display_name="Deferred",
            aliases_json='["Deferred"]',
            status=MonitoredStatus.FIXED,
            total_episodes=12,
            next_airing_episode=12,
            next_airing_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        session.add(settings)
        session.add(show)
        session.commit()
        anilist = MagicMock()
        anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])
        anilist.last_sync_at = datetime.now(timezone.utc)
        anilist.seconds_since_last_sync = lambda now=None: 60.0
        anilist.is_sync_due = lambda *args, **kwargs: False
        anilist.fetch_media_airing_schedules = AsyncMock(return_value={})
        supervisor = Supervisor(session=session, qbit=MagicMock(), anilist=anilist, settings=settings)

        logs = await supervisor.sync_anilist_schedule()

        assert any("deferred" in line for line in logs)
        # A deferred pass says nothing about quality, so the rollover may still
        # act on the data we already have.
        assert supervisor.anilist_sync_succeeded is None
    engine.dispose()


def _direct_supervisor(session, settings, qbit):
    anilist = MagicMock()
    anilist.fetch_user_seasonal_anime = AsyncMock(return_value=[])
    anilist.is_sync_due = lambda *a, **k: False
    anilist.seconds_since_last_sync = lambda now=None: 60.0
    return Supervisor(session=session, qbit=qbit, anilist=anilist, settings=settings)


def test_direct_show_moves_to_the_feed_that_carries_its_release():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=subs.id,
        )
        session.add(show)
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
            "Erai": {"url": erai.qbit_feed_url, "articles": [{
                "id": "erai-ep8",
                "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
                "torrentURL": "magnet:erai-ep8",
            }]},
        }
        supervisor = _direct_supervisor(session, settings, qbit)

        # The first sighting only nominates the candidate.
        logs = supervisor._reassign_direct_feeds([subs, erai])
        session.refresh(show)
        assert show.current_feed_id == subs.id
        assert show.candidate_feed_id == erai.id
        assert logs == []

        # The move only happens once the same candidate holds for the window.
        show.candidate_feed_since = utc_now() - timedelta(seconds=FEED_SWITCH_GRACE_SECONDS + 30)
        session.add(show)
        session.commit()
        logs = supervisor._reassign_direct_feeds([subs, erai])
        session.refresh(show)
        assert show.current_feed_id == erai.id
        assert show.candidate_feed_id is None
        assert any("Erai" in line for line in logs)
    engine.dispose()


def test_direct_show_stays_put_once_it_has_downloaded_from_its_feed():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=subs.id,
        )
        session.add(show)
        session.flush()
        session.add(Episode(
            monitored_id=show.id,
            episode_number=1,
            status=EpisodeStatus.COMPLETED,
            feed_id=subs.id,
            torrent_hash="hash-1",
        ))
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
            "Erai": {"url": erai.qbit_feed_url, "articles": [{
                "id": "erai-ep8",
                "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
                "torrentURL": "magnet:erai-ep8",
            }]},
        }
        supervisor = _direct_supervisor(session, settings, qbit)

        logs = supervisor._reassign_direct_feeds([subs, erai])
        session.refresh(show)

        assert show.current_feed_id == subs.id
        assert logs == []
    engine.dispose()


def test_pinned_direct_show_is_never_moved_between_feeds():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=subs.id,
            feed_pinned=True,
        )
        session.add(show)
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
            "Erai": {"url": erai.qbit_feed_url, "articles": [{
                "id": "erai-ep8",
                "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
                "torrentURL": "magnet:erai-ep8",
            }]},
        }
        supervisor = _direct_supervisor(session, settings, qbit)

        assert supervisor._reassign_direct_feeds([subs, erai]) == []
        session.refresh(show)
        assert show.current_feed_id == subs.id
    engine.dispose()


def test_a_show_that_delivered_a_release_is_never_moved_between_feeds():
    """A learned feed outranks every heuristic, with no downloaded hash to lean on."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=subs.id,
            learned_feed_id=subs.id,
        )
        session.add(show)
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
            "Erai": {"url": erai.qbit_feed_url, "articles": [{
                "id": "erai-ep8",
                "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
                "torrentURL": "magnet:erai-ep8",
            }]},
        }
        supervisor = _direct_supervisor(session, settings, qbit)

        logs = supervisor._reassign_direct_feeds([subs, erai])
        session.refresh(show)

        # No episode row carries a torrent hash, so the old guard would have let
        # this through; the learned feed is what holds it now.
        assert logs == []
        assert show.candidate_feed_id is None
        assert show.current_feed_id == subs.id
    engine.dispose()


def test_a_learned_show_is_never_sent_back_to_auto_discovery():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        # current_feed_id was cleared by a reset, but the feed is still proven.
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.UNCONFIRMED,
            current_feed_id=None,
            learned_feed_id=subs.id,
        )
        session.add(show)
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_items.return_value = {
            "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
            "Erai": {"url": erai.qbit_feed_url, "articles": []},
        }
        supervisor = _direct_supervisor(session, settings, qbit)

        logs = supervisor.bootstrap_unassigned_shows(create_qbit_rules=False)
        session.refresh(show)

        # Never re-guessed, and the proven feed is put back.
        assert show.current_feed_id == subs.id
        assert any("Restored" in line for line in logs)
        assert supervisor._restore_learned_feeds() == []
    engine.dispose()


def test_a_learned_feed_that_no_longer_exists_is_released():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        settings = Settings(id=1, base_dir="/tmp", download_mode="direct")
        subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=1)
        session.add(settings)
        session.add(subs)
        session.add(erai)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            learned_feed_id=1,
        )
        session.add(show)
        session.commit()
        # An install migrated by ALTER TABLE has no foreign key on this column,
        # so a feed removed from qBittorrent leaves the id dangling rather than
        # nulling it. Reproduce that shape here.
        session.exec(text("PRAGMA foreign_keys=OFF"))
        session.exec(text("DELETE FROM feeds WHERE id = 1"))
        session.commit()
        session.exec(text("PRAGMA foreign_keys=ON"))
        session.commit()

        qbit = MagicMock()
        supervisor = _direct_supervisor(session, settings, qbit)

        supervisor._restore_learned_feeds()
        session.refresh(show)

        assert show.learned_feed_id is None
        assert show.current_feed_id is None
        assert show.feed_is_locked is False
    engine.dispose()


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

