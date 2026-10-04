from unittest.mock import AsyncMock, MagicMock
import asyncio
import importlib
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.clients.qbit import (
    QbitAuthenticationError,
    QbitClientError,
    QbitConnectionError,
    QbitRSSRefreshError,
)
app_module = importlib.import_module("qbit_seasonal_anime.server.app")
from qbit_seasonal_anime.server import api as api_module
from qbit_seasonal_anime.server.app import create_app
from qbit_seasonal_anime.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    MatchHistory,
    Monitored,
    Feed,
    MonitoredStatus,
    Settings,
)
from qbit_seasonal_anime.server.api import get_db, get_qbit
from qbit_seasonal_anime.db.session import acquire_supervision_lease, init_db, release_supervision_lease


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        settings = Settings(id=1, qbit_host="http://localhost:8080", default_category="Anime")
        s.add(settings)
        s.commit()
    return engine


@pytest.fixture
def session(db_engine):
    with Session(db_engine) as s:
        yield s


@pytest.fixture
def mock_qbit():
    q = MagicMock()
    q.get_rss_rules.return_value = {}
    q.remove_rss_rule.return_value = True
    return q


@pytest.fixture
def client(db_engine, mock_qbit, monkeypatch):
    async def idle_background_task():
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "background_supervisor_task", idle_background_task)
    monkeypatch.setattr(app_module, "get_engine", lambda: db_engine)
    app = create_app()

    def override_get_db():
        with Session(db_engine) as s:
            yield s

    def override_get_qbit():
        return mock_qbit

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qbit] = override_get_qbit

    with TestClient(app) as test_client:
        yield test_client


def test_init_db_adds_query_indexes_to_legacy_schema(db_engine):
    expected_indexes = {
        "ix_monitored_current_feed_id",
        "ix_monitored_status_current_feed",
        "ix_episode_status_version",
        "ix_rule_history_feed_id",
        "ix_rule_history_created_at",
        "ix_rule_history_monitored_created",
        "ix_rule_history_monitored_outcome_feed",
    }
    with db_engine.begin() as connection:
        for index_name in expected_indexes:
            connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")

    init_db(db_engine)

    actual_indexes = {
        index["name"]
        for table_name in ("monitored", "episodes", "rule_history")
        for index in inspect(db_engine).get_indexes(table_name)
    }
    assert expected_indexes <= actual_indexes


def test_index_returns_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "qbit-seasonal-anime" in response.text
    assert "Releasing" in response.text
    assert "Planned" in response.text
    assert "Calendar" in response.text
    assert "tab-calendar" in response.text
    assert "calendar-weekly-grid" in response.text


def test_get_shows(client, session):
    show1 = Monitored(
        anilist_id=1001,
        display_name="Bleach S1",
        status=MonitoredStatus.FIXED,
        cover_image="https://example.com/bleach.jpg",
    )
    show2 = Monitored(
        anilist_id=1002,
        display_name="Ao Ashi S2",
        status=MonitoredStatus.UNCONFIRMED,
        next_airing_episode=1,
    )
    session.add(show1)
    session.add(show2)
    session.commit()

    response = client.get("/api/shows")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    
    b_show = next(s for s in data if s["display_name"] == "Bleach S1")
    assert b_show["is_released"] is True
    assert b_show["cover_image"] == "https://example.com/bleach.jpg"

    a_show = next(s for s in data if s["display_name"] == "Ao Ashi S2")
    assert a_show["is_released"] is False


def test_toggle_pause_show(client, session):
    show = Monitored(anilist_id=2001, display_name="Test Pause Show", status=MonitoredStatus.UNCONFIRMED)
    session.add(show)
    session.commit()
    session.refresh(show)

    res1 = client.post(f"/api/shows/{show.id}/pause")
    assert res1.status_code == 200
    assert res1.json()["new_status"] == MonitoredStatus.PAUSED.value

    res2 = client.post(f"/api/shows/{show.id}/pause")
    assert res2.status_code == 200
    assert res2.json()["new_status"] == MonitoredStatus.UNCONFIRMED.value


def test_feeds_and_reorder(client, session):
    f1 = Feed(qbit_feed_name="Feed A", qbit_feed_url="https://feed.a/rss", priority=1)
    f2 = Feed(qbit_feed_name="Feed B", qbit_feed_url="https://feed.b/rss", priority=2)
    session.add(f1)
    session.add(f2)
    session.commit()
    session.refresh(f1)
    session.refresh(f2)

    res = client.get("/api/feeds")
    assert res.status_code == 200
    feeds = res.json()
    assert len(feeds) >= 2

    reorder_res = client.post("/api/feeds/reorder", json={
        "feeds": [
            {"id": f1.id, "priority": 2},
            {"id": f2.id, "priority": 1},
        ]
    })
    assert reorder_res.status_code == 200
    assert reorder_res.json()["status"] == "success"

    session.expire_all()
    updated_f1 = session.get(Feed, f1.id)
    updated_f2 = session.get(Feed, f2.id)
    assert updated_f1.priority == 2
    assert updated_f2.priority == 1


def test_settings_endpoints(client, session):
    res = client.get("/api/settings")
    assert res.status_code == 200
    s_data = res.json()
    assert "qbit_host" in s_data

    update_res = client.post("/api/settings", json={
        "qbit_host": "http://192.168.1.50:8080",
        "default_category": "anime-seasonal",
        "default_seed_ratio": 1.5,
    })
    assert update_res.status_code == 200

    session.expire_all()
    s = session.exec(select(Settings)).first()
    assert s.qbit_host == "http://192.168.1.50:8080"
    assert s.default_category == "anime-seasonal"
    assert s.default_seed_ratio == 1.5


def test_system_status(client, session):
    session.add(Feed(qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.example/rss", priority=1))
    session.add(Feed(qbit_feed_name="Feed 2", qbit_feed_url="https://feed2.example/rss", priority=2))
    session.add(Monitored(anilist_id=7001, display_name="Working", status=MonitoredStatus.FIXED))
    session.add(Monitored(anilist_id=7002, display_name="Upcoming", status=MonitoredStatus.UNCONFIRMED))
    session.add(Monitored(anilist_id=7003, display_name="Stalled", status=MonitoredStatus.STALLED))
    session.add(Monitored(anilist_id=7004, display_name="Paused", status=MonitoredStatus.PAUSED))
    session.add(Monitored(anilist_id=7005, display_name="Completed", status=MonitoredStatus.COMPLETED))
    session.commit()

    res = client.get("/api/status")
    assert res.status_code == 200
    st = res.json()
    # daemon_active reports the real supervisor task state, so it is False here:
    # no background task runs without the app lifespan.
    assert st["daemon_active"] is api_module.state.daemon_active
    assert st["download_mode"] == "rules"
    assert st["total_shows"] == 5
    assert st["counts"] == {
        "works": 1,
        "upcoming": 1,
        "testing": 0,
        "stalled": 1,
        "paused": 1,
        "completed": 1,
        "feeds": 2,
    }


def test_manual_cycle_offloads_scheduler_calculation(client, monkeypatch):
    qbit = MagicMock()
    qbit.test_connection.return_value = {"app_version": "test", "api_version": "test"}
    monkeypatch.setattr(api_module, "QBitClient", MagicMock(return_value=qbit))
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=["Cycle complete"])
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    for name, value in {
        "is_running_cycle": False,
        "last_cycle_time": None,
        "next_check_reason": api_module.state.next_check_reason,
        "next_check_seconds": api_module.state.next_check_seconds,
        "target_next_check_time": api_module.state.target_next_check_time,
    }.items():
        monkeypatch.setattr(api_module.state, name, value)

    scheduler = MagicMock(return_value=(321, "Next cycle"))
    monkeypatch.setattr(api_module, "calculate_next_poll_interval", scheduler)
    to_thread = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    fake_asyncio = MagicMock()
    fake_asyncio.to_thread = to_thread
    monkeypatch.setattr(api_module, "asyncio", fake_asyncio)

    response = client.post("/api/cycle/run")

    assert response.status_code == 200
    assert response.json()["next_check_seconds"] == 321
    assert response.json()["next_check_reason"] == "Next cycle"
    # A manual cycle always re-reads the RSS feeds first.
    supervisor.run_full_cycle.assert_awaited_once_with(force_rss_refresh=True)
    assert [c.args[0] for c in to_thread.await_args_list].count(scheduler) == 1
    scheduler.assert_called_once()
    assert api_module.state.is_running_cycle is False


def test_manual_cycle_reports_qbit_unavailable(client, monkeypatch):
    qbit = MagicMock()
    qbit.test_connection.side_effect = QbitConnectionError("still starting")
    monkeypatch.setattr(api_module, "QBitClient", MagicMock(return_value=qbit))
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=[])
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module.state, "is_running_cycle", False)

    response = client.post("/api/cycle/run")

    assert response.status_code == 503
    assert "qBittorrent is unavailable" in response.json()["detail"]
    supervisor.run_full_cycle.assert_not_awaited()
    assert api_module.state.is_running_cycle is False


@pytest.fixture
def supervisor_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, qbit_host="http://qbit:8080"))
        session.commit()
    return engine


def _patch_supervisor_loop(monkeypatch, engine, qbit, supervisor):
    """Wire the loop to in-memory doubles; returns the Supervisor factory mock."""
    supervisor_factory = MagicMock(return_value=supervisor)
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "QBitClient", MagicMock(return_value=qbit))
    monkeypatch.setattr(app_module, "Supervisor", supervisor_factory)
    monkeypatch.setattr(app_module, "calculate_next_poll_interval", MagicMock(return_value=(60, "normal")))
    monkeypatch.setattr(app_module, "STARTUP_GRACE_SECONDS", 0)
    return supervisor_factory


def _run_loop_until_cancelled(monkeypatch, stop_after=1):
    """Let the supervisor loop run, recording the delay of each blocking wait."""
    recorder = {"waits": 0, "delays": []}

    async def stop_wait(awaitable, *args, **kwargs):
        awaitable.close()
        recorder["delays"].append(kwargs.get("timeout"))
        recorder["waits"] += 1
        if recorder["waits"] >= stop_after:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module.asyncio, "wait_for", stop_wait)
    return recorder


def _log_messages():
    return [entry["message"] for entry in list(app_module.state.logs)]


async def test_background_supervisor_waits_for_qbit_before_running_a_cycle(supervisor_engine, monkeypatch):
    """qBittorrent was not up yet, so no cycle may start until a probe succeeds."""
    events = []
    probes = {"count": 0}

    def probe():
        probes["count"] += 1
        events.append("probe")
        if probes["count"] < 3:
            raise QbitConnectionError("connection refused")
        return {"app_version": "test", "api_version": "test"}

    qbit = MagicMock()
    qbit.test_connection.side_effect = probe
    supervisor = MagicMock()

    async def run_cycle(**kwargs):
        events.append("cycle")
        return []

    supervisor.run_full_cycle = AsyncMock(side_effect=run_cycle)
    _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (1, 2, 4))
    _run_loop_until_cancelled(monkeypatch, stop_after=3)

    await app_module.background_supervisor_task()

    assert events == ["probe", "probe", "probe", "cycle"]
    supervisor.run_full_cycle.assert_awaited_once()
    assert app_module.state.next_check_reason == "normal"
    assert app_module.state.next_check_seconds == 60


async def test_background_supervisor_marks_the_second_pass_as_hunting(supervisor_engine, monkeypatch):
    """A hunt reported by the scheduler must label the following pass as hunting."""
    qbit = MagicMock()
    qbit.test_connection.return_value = {"app_version": "test", "api_version": "test"}
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=[])
    _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "is_hunting", MagicMock(return_value=True))
    _run_loop_until_cancelled(monkeypatch, stop_after=2)

    await app_module.background_supervisor_task()

    first_call, second_call = supervisor.run_full_cycle.await_args_list
    assert first_call.kwargs["hunting"] is False
    assert second_call.kwargs["hunting"] is True


async def test_background_supervisor_escalates_and_then_caps_the_retry_delay(supervisor_engine, monkeypatch):
    qbit = MagicMock()
    qbit.test_connection.side_effect = QbitConnectionError("connection refused")
    supervisor = MagicMock()
    supervisor_factory = _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (1, 2, 4, 8, 16, 30, 60))
    recorder = _run_loop_until_cancelled(monkeypatch, stop_after=8)

    await app_module.background_supervisor_task()

    # 8 failures walk the whole ladder, then stay pinned to the last delay.
    assert recorder["delays"] == [1, 2, 4, 8, 16, 30, 60, 60]
    supervisor_factory.assert_not_called()


async def test_background_supervisor_backs_off_hard_on_authentication_failure(supervisor_engine, monkeypatch):
    """A wrong password must not be retried on the fast connection ladder."""
    qbit = MagicMock()
    qbit.test_connection.side_effect = QbitAuthenticationError("login failed")
    supervisor = MagicMock()
    supervisor_factory = _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (1, 2, 4))
    recorder = _run_loop_until_cancelled(monkeypatch, stop_after=2)

    await app_module.background_supervisor_task()

    assert recorder["delays"] == [app_module.QBIT_AUTH_RETRY_SECONDS] * 2
    assert "authentication failed" in app_module.state.next_check_reason
    supervisor_factory.assert_not_called()


async def test_background_supervisor_resets_backoff_and_reports_recovery(supervisor_engine, monkeypatch):
    probes = {"count": 0}

    def probe():
        probes["count"] += 1
        if probes["count"] == 1:
            raise QbitConnectionError("connection refused")
        return {"app_version": "test", "api_version": "test"}

    qbit = MagicMock()
    qbit.test_connection.side_effect = probe
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=[])
    _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (1, 2, 4))
    _run_loop_until_cancelled(monkeypatch, stop_after=3)

    await app_module.background_supervisor_task()

    assert "qBittorrent connection restored." in _log_messages()
    assert app_module.state.next_check_reason == "normal"
    # A successful cycle resets the ladder, so the next outage starts at 1s again.
    assert probes["count"] == 3


async def test_background_supervisor_reenters_backoff_when_qbit_drops_mid_cycle(supervisor_engine, monkeypatch):
    """A connection lost during the cycle must not be swallowed as a normal cycle."""
    probes = {"count": 0}

    def probe():
        probes["count"] += 1
        if probes["count"] == 2:
            raise QbitConnectionError("reset by peer")
        return {"app_version": "test", "api_version": "test"}

    qbit = MagicMock()
    qbit.test_connection.side_effect = probe
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(side_effect=QbitConnectionError("reset by peer"))
    _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (1, 2, 4))
    app_module.state.last_cycle_time = None
    recorder = _run_loop_until_cancelled(monkeypatch, stop_after=1)

    await app_module.background_supervisor_task()

    assert "became unavailable during the cycle" in app_module.state.next_check_reason
    assert recorder["delays"] == [1]
    assert app_module.state.last_cycle_time is None
    assert app_module.state.is_running_cycle is False


async def test_background_supervisor_cancels_cleanly_during_reconnect_wait(supervisor_engine, monkeypatch):
    qbit = MagicMock()
    qbit.test_connection.side_effect = QbitConnectionError("connection refused")
    supervisor = MagicMock()
    supervisor_factory = _patch_supervisor_loop(monkeypatch, supervisor_engine, qbit, supervisor)
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (60,))

    # Let the startup grace pass, then cancel during the reconnect wait.
    real_sleep = asyncio.sleep

    async def cancel_sleep(*args, **kwargs):
        if args and args[0] == 60:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(app_module.asyncio, "sleep", cancel_sleep)
    _run_loop_until_cancelled(monkeypatch, stop_after=1)

    await app_module.background_supervisor_task()

    supervisor_factory.assert_not_called()
    assert app_module.state.is_running_cycle is False
    assert "Background supervisor stopped." in _log_messages()


def test_format_sleep_switches_to_seconds_below_a_minute():
    assert app_module._format_sleep(1) == "1s"
    assert app_module._format_sleep(30) == "30s"
    assert app_module._format_sleep(59) == "59s"
    assert app_module._format_sleep(60) == "1m"
    assert app_module._format_sleep(21600) == "360m"


def test_delete_show(client, session):
    show = Monitored(anilist_id=3001, display_name="Show To Delete", status=MonitoredStatus.UNCONFIRMED)
    session.add(show)
    session.commit()
    session.refresh(show)
    show_id = show.id

    res = client.delete(f"/api/shows/{show_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

    session.expire_all()
    deleted = session.get(Monitored, show_id)
    assert deleted is None


def test_edit_show_endpoint(client, session, mock_qbit):
    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {
            "uid": "1",
            "url": "https://feed1.org/rss",
            "articles": [
                {"id": "1", "title": "[SubsPlease] Bleach - 01 (1080p) [ABCD].mkv"}
            ]
        }
    }
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(anilist_id=4001, display_name="Bleach", aliases_json='["Bleach"]', status=MonitoredStatus.UNCONFIRMED)
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    res = client.post(f"/api/shows/{show.id}/edit", json={
        "current_feed_id": feed.id,
        "save_folder": "Bleach Custom",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.save_folder == "Bleach Custom"
    assert updated.current_feed_id == feed.id
    # An explicit feed choice pins it, so auto-detect leaves it alone.
    assert updated.feed_pinned is True


def test_editing_other_fields_leaves_the_feed_and_pin_alone(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4010,
        display_name="Blue Box Season 2",
        aliases_json='["Blue Box Season 2"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        feed_pinned=True,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    res = client.post(f"/api/shows/{show.id}/edit", json={"ratio_limit": 2.5})
    assert res.status_code == 200

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.current_feed_id == feed.id
    assert updated.feed_pinned is True
    # The edit still applied — to the rule, which is where ratio lives.
    written = mock_qbit.set_rss_rule.call_args.kwargs["rule_def"]
    assert written["ratioLimit"] == 2.5


def test_picking_auto_discover_feed_unpins_the_show(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4002,
        display_name="Re:Zero",
        aliases_json='["Re:Zero"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        feed_pinned=True,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    res = client.post(f"/api/shows/{show.id}/edit", json={"current_feed_id": 0})
    assert res.status_code == 200

    session.expire_all()
    updated = session.get(Monitored, show.id)
    assert updated.current_feed_id is None
    assert updated.feed_pinned is False


def test_rule_details_flags_unlearned_pattern(client, session, mock_qbit):
    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4003,
        display_name="Upcoming Anime",
        aliases_json='["Upcoming Anime", "Upcoming Anime 2nd Season"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        next_airing_episode=1,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    res = client.get(f"/api/shows/{show.id}/rule")
    assert res.status_code == 200
    data = res.json()
    assert data["has_learned_pattern"] is False
    assert data["is_upcoming"] is True
    assert data["feed_pinned"] is False
    assert data["candidate_feed_id"] == 0


def test_rule_details_reports_testing_show_not_upcoming(client, session, mock_qbit):
    from datetime import timedelta

    from qbit_seasonal_anime.db.models import utc_now

    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    show = Monitored(
        anilist_id=4005,
        display_name="Aired But Unmatched Anime",
        aliases_json='["Aired But Unmatched Anime"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        next_airing_episode=3,
        next_airing_at=utc_now() - timedelta(hours=6),
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["is_upcoming"] is False
    assert data["has_learned_pattern"] is False


def test_rule_details_reports_watched_candidate_feed(client, session, mock_qbit):
    from datetime import timedelta

    from qbit_seasonal_anime.db.models import utc_now

    feed = Feed(id=1, qbit_feed_name="Feed 1", qbit_feed_url="https://feed1.org/rss", priority=1)
    other = Feed(id=2, qbit_feed_name="Feed 2", qbit_feed_url="https://feed2.org/rss", priority=2)
    show = Monitored(
        anilist_id=4004,
        display_name="Split Cour Anime",
        aliases_json='["Split Cour Anime"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        candidate_feed_id=other.id,
        candidate_feed_since=utc_now() - timedelta(minutes=1),
    )
    session.add(feed)
    session.add(other)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_items.return_value = {
        "Feed 1": {"url": "https://feed1.org/rss", "articles": []}
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["candidate_feed_id"] == other.id
    assert data["candidate_feed_name"] == "Feed 2"
    assert data["candidate_feed_since"] is not None


def test_title_language_setting_switch(client, session):
    show = Monitored(
        anilist_id=5001,
        display_name="Kusuriya no Hitorigoto",
        title_romaji="Kusuriya no Hitorigoto",
        title_english="The Apothecary Diaries",
        status=MonitoredStatus.FIXED,
    )
    session.add(show)
    session.commit()

    res = client.get("/api/shows")
    assert res.status_code == 200
    s_default = next(s for s in res.json() if s["anilist_id"] == 5001)
    assert s_default["display_name"] == "The Apothecary Diaries"

    up_res = client.post("/api/settings", json={"title_language": "romaji"})
    assert up_res.status_code == 200

    res_ro = client.get("/api/shows")
    assert res_ro.status_code == 200
    s_ro = next(s for s in res_ro.json() if s["anilist_id"] == 5001)
    assert s_ro["display_name"] == "Kusuriya no Hitorigoto"

    client.post("/api/settings", json={"title_language": "english"})
    res_en = client.get("/api/shows")
    s_en = next(s for s in res_en.json() if s["anilist_id"] == 5001)
    assert s_en["display_name"] == "The Apothecary Diaries"


def test_get_show_rule_is_read_only_when_a_article_matches(client, session, mock_qbit):
    """Opening the rule modal must not confirm shows, write rules, or record history."""
    feed = Feed(id=2, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    show = Monitored(
        anilist_id=6001,
        display_name="Yomi no Tsugai",
        aliases_json='["Yomi no Tsugai"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
        qbit_rule_name="[Seasonal] Yomi no Tsugai",
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    mock_qbit.get_rss_rules.return_value = {
        "[Seasonal] Yomi no Tsugai": {"enabled": True, "mustContain": "(Yomi\\s+no\\s+Tsugai)"}
    }
    mock_qbit.get_matching_articles.return_value = {
        "https://subsplease.org/rss": ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [3B57467D].mkv"]
    }
    mock_qbit.get_rss_items.return_value = {
        "https://subsplease.org/rss": {
            "articles": [
                {
                    "title": "[SubsPlease] Yomi no Tsugai - 22 (1080p) [3B57467D].mkv",
                    "date": "01 Sep 2026 12:00:00 +0000",
                }
            ]
        }
    }

    res = client.get(f"/api/shows/{show.id}/rule")
    assert res.status_code == 200
    data = res.json()
    # The article is still reported as currently matching...
    assert len(data["matched_articles"]) == 1

    # ...but nothing was mutated and no match was invented from the cache.
    session.expire_all()
    updated_show = session.get(Monitored, show.id)
    assert updated_show.status == MonitoredStatus.UNCONFIRMED
    assert updated_show.last_confirmed_episode is None
    assert updated_show.matched_title is None
    mock_qbit.set_rss_rule.assert_not_called()
    assert client.get("/api/history").json() == []


def test_rule_details_reports_only_live_matches(client, session, mock_qbit):
    feed = Feed(id=3, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    show = Monitored(
        anilist_id=6002,
        display_name="Yomi no Tsugai",
        aliases_json='["Yomi no Tsugai"]',
        status=MonitoredStatus.FIXED,
        current_feed_id=feed.id,
        qbit_rule_name="[Seasonal] Yomi no Tsugai",
        matched_title="Yomi no Tsugai",
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)
    session.add(MatchHistory(
        monitored_id=show.id,
        show_name=show.display_name,
        rule_name=show.qbit_rule_name,
        release_title="[SubsPlease] Yomi no Tsugai - 21 (1080p) [OLD].mkv",
        episode=21,
    ))
    session.commit()

    mock_qbit.get_rss_rules.return_value = {"[Seasonal] Yomi no Tsugai": {"enabled": True, "mustContain": "Yomi no Tsugai"}}
    mock_qbit.get_matching_articles.return_value = {
        "https://subsplease.org/rss": ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv"]
    }

    data = client.get(f"/api/shows/{show.id}/rule").json()
    assert data["matched_articles"] == ["[SubsPlease] Yomi no Tsugai - 22 (1080p) [NEW].mkv"]
    # Already-recorded releases are not re-listed alongside live matches; the
    # History tab is their only home.
    assert "history_articles" not in data
    assert [h["release_title"] for h in client.get("/api/history").json()] == [
        "[SubsPlease] Yomi no Tsugai - 21 (1080p) [OLD].mkv"
    ]


def test_history_endpoint_and_delete_still_work(client, session, mock_qbit):
    show = Monitored(anilist_id=6003, display_name="History Show", aliases_json='["History Show"]')
    session.add(show)
    session.commit()
    session.refresh(show)
    session.add(MatchHistory(
        monitored_id=show.id,
        show_name=show.display_name,
        rule_name="[Seasonal] History Show",
        release_title="[SubsPlease] History Show - 01 (1080p).mkv",
        episode=1,
    ))
    session.commit()

    hist_data = client.get("/api/history").json()
    assert len(hist_data) == 1
    assert hist_data[0]["episode"] == 1

    del_res = client.delete("/api/history")
    assert del_res.status_code == 200
    assert client.get("/api/history").json() == []


# ---------------------------------------------------------------------------
# Direct download engine: schema, settings and endpoints
# ---------------------------------------------------------------------------


def test_init_db_repairs_active_numbering_without_reopening_completed_shows(tmp_path):
    database_path = tmp_path / "anime.db"
    engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        rezero = Monitored(
            id=1,
            anilist_id=101,
            display_name="Re:ZERO Season 4",
            aliases_json='["Re:ZERO", "Re Zero kara Hajimeru Isekai Seikatsu"]',
            status=MonitoredStatus.FIXED,
            total_episodes=19,
            last_confirmed_episode=84,
            current_feed_id=1,
        )
        false_completed = Monitored(
            id=2,
            anilist_id=102,
            display_name="False Completed Anime",
            aliases_json='["False Completed Anime"]',
            status=MonitoredStatus.COMPLETED,
            total_episodes=3,
            last_confirmed_episode=10,
            current_feed_id=1,
        )
        session.add(feed)
        session.add(rezero)
        session.add(false_completed)
        session.flush()
        session.add(Episode(monitored_id=rezero.id, episode_number=1, status=EpisodeStatus.COMPLETED))
        session.add(Episode(
            monitored_id=rezero.id,
            episode_number=16,
            status=EpisodeStatus.COMPLETED,
            feed_id=feed.id,
            release_title="[Erai-raws] Re:Zero kara Hajimeru Isekai Seikatsu 4th Season - 16 (1080p).mkv",
        ))
        session.add(Episode(
            monitored_id=rezero.id,
            episode_number=17,
            status=EpisodeStatus.COMPLETED,
            feed_id=feed.id,
            release_title="[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 83 (1080p).mkv",
        ))
        session.add(Episode(
            monitored_id=rezero.id,
            episode_number=18,
            status=EpisodeStatus.COMPLETED,
            feed_id=feed.id,
            release_title="[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 84 (1080p).mkv",
        ))
        session.add(Episode(monitored_id=false_completed.id, episode_number=1, status=EpisodeStatus.WANTED))
        session.commit()
        session.exec(text("PRAGMA user_version = 0"))
        session.commit()

    init_db(engine)

    with Session(engine) as session:
        mapping = session.exec(
            select(EpisodeNumberMapping).where(
                EpisodeNumberMapping.monitored_id == 1,
                EpisodeNumberMapping.feed_id == 1,
            )
        ).first()
        inferred_gap = session.exec(
            select(Episode).where(Episode.monitored_id == 1, Episode.episode_number == 1)
        ).first()
        rezero = session.get(Monitored, 1)
        preserved = session.get(Monitored, 2)
        # Migration must not invent a mapping; numbering is resolved on first use.
        assert mapping is None
        assert inferred_gap.status == EpisodeStatus.WANTED
        assert rezero.last_confirmed_episode == 18
        assert preserved.status == MonitoredStatus.COMPLETED
        assert preserved.last_confirmed_episode == 10
        assert session.exec(text("PRAGMA user_version")).one()[0] == 1
    assert list(tmp_path.glob("anime.db.*.bak"))
    engine.dispose()


def test_init_db_keeps_manual_feed_pin_from_main(tmp_path):
    """A feed pinned before the direct engine existed must survive migration."""
    database_path = tmp_path / "anime.db"
    engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        session.add(feed)
        session.add(Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            feed_pinned=True,
        ))
        session.commit()

    init_db(engine)

    with Session(engine) as session:
        show = session.get(Monitored, 1)
        assert show.feed_pinned is True
        assert show.current_feed_id == 1
    engine.dispose()


def test_init_db_migrates_the_legacy_pinned_feed_id_column(tmp_path):
    """An early direct-download build's pin must not be silently dropped."""
    database_path = tmp_path / "anime.db"
    engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.exec(text("ALTER TABLE monitored ADD COLUMN pinned_feed_id INTEGER"))
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        session.add(feed)
        session.flush()
        session.add(Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
        ))
        session.commit()
        # The legacy column is not part of the model, so it is written directly.
        session.connection().execute(
            text("UPDATE monitored SET pinned_feed_id = :fid WHERE id = 1"),
            {"fid": feed.id},
        )
        session.commit()

    init_db(engine)

    with Session(engine) as session:
        show = session.get(Monitored, 1)
        assert show.feed_pinned is True
        assert show.current_feed_id == 1
    engine.dispose()


def test_supervision_lease_allows_only_one_owner():
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    assert acquire_supervision_lease(engine, "owner-a") is True
    assert acquire_supervision_lease(engine, "owner-b") is False
    release_supervision_lease(engine, "owner-a")
    assert acquire_supervision_lease(engine, "owner-b") is True
    release_supervision_lease(engine, "owner-b")
    engine.dispose()


def test_init_db_adds_rules_default_to_legacy_settings(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY,
                qbit_host VARCHAR,
                qbit_username VARCHAR,
                qbit_password VARCHAR,
                base_dir VARCHAR,
                default_category VARCHAR,
                default_seed_ratio FLOAT,
                anilist_username VARCHAR,
                refresh_interval_minutes INTEGER,
                stall_wait_hours INTEGER,
                title_language VARCHAR
            )
        """)
        connection.exec_driver_sql("INSERT INTO settings (id, title_language) VALUES (1, 'english')")

    init_db(engine)

    with Session(engine) as session:
        settings = session.exec(select(Settings)).first()
        # An existing install must not silently switch download engines.
        assert settings.download_mode == "rules"
        assert settings.backfill_window_days == 14
    engine.dispose()


def test_settings_switch_to_direct_verifies_rule_ownership(client, session, monkeypatch):
    supervisor = MagicMock()
    supervisor.prepare_download_mode.return_value = ["Disabled managed RSS rules."]
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())

    response = client.post("/api/settings", json={"download_mode": "direct"})

    assert response.status_code == 200
    assert response.json()["download_mode"] == "direct"
    supervisor.prepare_download_mode.assert_called_once_with("direct")
    session.expire_all()
    assert session.exec(select(Settings)).first().download_mode == "direct"


def test_settings_switch_failure_keeps_rules_mode(client, session, monkeypatch):
    supervisor = MagicMock()
    supervisor.prepare_download_mode.side_effect = RuntimeError("rule still active")
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())

    response = client.post("/api/settings", json={"download_mode": "direct"})

    assert response.status_code == 409
    session.expire_all()
    assert session.exec(select(Settings)).first().download_mode == "rules"


def test_settings_rejects_unknown_download_mode(client):
    assert client.post("/api/settings", json={"download_mode": "yolo"}).status_code == 400


def test_settings_rejects_out_of_range_backfill_window(client):
    assert client.post("/api/settings", json={"backfill_window_days": -1}).status_code == 400
    assert client.post("/api/settings", json={"backfill_window_days": 400}).status_code == 400
    assert client.post("/api/settings", json={"backfill_window_days": 0}).status_code == 200


def test_manual_cycle_rejects_overlapping_supervision(client, monkeypatch):
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=[])
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    assert app_module.state.try_begin_cycle("test-owner")

    try:
        response = client.post("/api/cycle/run")
    finally:
        app_module.state.end_cycle("test-owner")

    assert response.status_code == 409
    supervisor.run_full_cycle.assert_not_awaited()
    assert app_module.state.is_running_cycle is False


def test_edit_show_sets_manual_episode_offset(client, session):
    feed = Feed(id=8, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    show = Monitored(
        anilist_id=4080,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO Season 4"]',
        status=MonitoredStatus.FIXED,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.refresh(show)

    response = client.post(f"/api/shows/{show.id}/edit", json={
        "current_feed_id": feed.id,
        "episode_offset": 66,
    })

    assert response.status_code == 200
    session.expire_all()
    mapping = session.exec(
        select(EpisodeNumberMapping).where(EpisodeNumberMapping.monitored_id == show.id)
    ).first()
    assert mapping.offset == 66
    # Stored by name, matching how the column is persisted.
    assert mapping.source == "MANUAL"


def test_direct_rule_details_do_not_create_qbit_rules(client, session, mock_qbit):
    settings = session.exec(select(Settings)).first()
    settings.download_mode = "direct"
    feed = Feed(id=3, qbit_feed_name="Direct Feed", qbit_feed_url="https://direct.example/rss", priority=1)
    show = Monitored(
        anilist_id=4100,
        display_name="Direct Show",
        aliases_json='["Direct Show"]',
        status=MonitoredStatus.UNCONFIRMED,
        current_feed_id=feed.id,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    mock_qbit.get_rss_items.return_value = {
        "Direct Feed": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "1", "title": "[Group] Direct Show - 01 [1080p].mkv"}],
        }
    }

    response = client.get(f"/api/shows/{show.id}/rule")

    assert response.status_code == 200
    assert response.json()["download_mode"] == "direct"
    mock_qbit.set_rss_rule.assert_not_called()


def test_get_direct_episode_records(client, session):
    show = Monitored(
        anilist_id=4200,
        display_name="Episode Show",
        aliases_json='["Episode Show"]',
        status=MonitoredStatus.FIXED,
        total_episodes=2,
    )
    session.add(show)
    session.flush()
    session.add(Episode(monitored_id=show.id, episode_number=1, status=EpisodeStatus.COMPLETED))
    session.add(Episode(monitored_id=show.id, episode_number=2, status=EpisodeStatus.WANTED))
    session.commit()

    response = client.get(f"/api/shows/{show.id}/episodes")

    assert response.status_code == 200
    payload = response.json()
    assert [episode["episode_number"] for episode in payload] == [1, 2]
    assert [episode["status"] for episode in payload] == ["completed", "wanted"]


def test_shows_report_direct_engine_counters(client, session):
    feed = Feed(id=9, qbit_feed_name="Counter", qbit_feed_url="https://counter.example/rss", priority=1)
    show = Monitored(
        anilist_id=4300,
        display_name="Counter Show",
        aliases_json='["Counter Show"]',
        status=MonitoredStatus.FIXED,
        current_feed_id=9,
    )
    session.add(feed)
    session.add(show)
    session.commit()
    session.flush()
    session.add(Episode(monitored_id=show.id, episode_number=1, status=EpisodeStatus.COMPLETED, version=2))
    session.add(Episode(monitored_id=show.id, episode_number=2, status=EpisodeStatus.WANTED))
    session.add(Episode(monitored_id=show.id, episode_number=3, status=EpisodeStatus.FAILED))
    session.commit()

    payload = client.get("/api/shows").json()
    entry = next(item for item in payload if item["anilist_id"] == 4300)

    assert entry["downloaded_episodes_count"] == 1
    assert entry["wanted_episodes_count"] == 1
    assert entry["failed_episodes_count"] == 1
    assert entry["v2_episodes_count"] == 1


def test_sync_anilist_stands_rules_down_before_grabbing(client, session, monkeypatch):
    """The grabber must never run while managed rules are still active."""
    settings = session.exec(select(Settings)).first()
    settings.download_mode = "direct"
    settings.anilist_username = "TestUser"
    session.commit()

    supervisor = MagicMock()
    supervisor.prepare_download_mode.return_value = ["Disabled managed RSS rule."]
    supervisor.sync_feeds.return_value = []
    supervisor.anilist_sync_succeeded = True
    supervisor.sync_anilist_schedule = AsyncMock(return_value=[])
    supervisor.bootstrap_unassigned_shows.return_value = []
    supervisor.reconcile_schedule_rollover.return_value = []
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())
    monkeypatch.setattr("qbit_seasonal_anime.core.grabber.evaluate_and_grab_releases", MagicMock(return_value=[]))

    response = client.post("/api/settings/sync-anilist")

    assert response.status_code == 200
    supervisor.prepare_download_mode.assert_called_once_with("direct")
    # The preflight has to happen before anything can add a torrent.
    order = [c[0] for c in supervisor.method_calls]
    assert order.index("prepare_download_mode") < order.index("bootstrap_unassigned_shows")


def test_sync_anilist_aborts_when_rules_cannot_be_stood_down(client, session, monkeypatch):
    settings = session.exec(select(Settings)).first()
    settings.download_mode = "direct"
    settings.anilist_username = "TestUser"
    session.commit()

    supervisor = MagicMock()
    supervisor.prepare_download_mode.side_effect = QbitClientError("managed RSS rules remain active")
    supervisor.sync_feeds.return_value = []
    monkeypatch.setattr(api_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(api_module, "QBitClient", MagicMock())
    grab = MagicMock(return_value=[])
    monkeypatch.setattr("qbit_seasonal_anime.core.grabber.evaluate_and_grab_releases", grab)

    response = client.post("/api/settings/sync-anilist")

    assert response.status_code == 409
    grab.assert_not_called()


def test_pause_in_observe_mode_does_not_enable_rules():
    app = create_app()
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, download_mode="observe", default_category="Anime", base_dir="/tmp/Anime"))
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
        session.add(feed)
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
            current_feed_id=1,
            qbit_rule_name="[Seasonal] Sousou no Frieren",
        )
        session.add(show)
        session.commit()

        qbit = MagicMock()
        qbit.get_rss_rules.return_value = {"[Seasonal] Sousou no Frieren": {"enabled": False}}
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[get_qbit] = lambda: qbit
        try:
            client = TestClient(app)
            paused = client.post("/api/shows/1/pause")
            assert paused.status_code == 200
            assert session.get(Monitored, 1).status == MonitoredStatus.PAUSED
            qbit.set_rss_rule.assert_not_called()
            resumed = client.post("/api/shows/1/pause")
            assert resumed.status_code == 200
            assert session.get(Monitored, 1).status == MonitoredStatus.FIXED
            qbit.set_rss_rule.assert_not_called()
        finally:
            app.dependency_overrides.clear()
    engine.dispose()


def test_delete_show_reports_a_rule_that_cannot_be_deleted(client, session, mock_qbit):
    show = Monitored(
        anilist_id=4400,
        display_name="Stubborn Show",
        aliases_json='["Stubborn Show"]',
        status=MonitoredStatus.FIXED,
        qbit_rule_name="[Seasonal] Stubborn Show",
    )
    session.add(show)
    session.commit()
    # The real client wraps any failure as QbitClientError.
    mock_qbit.remove_rss_rule.side_effect = QbitClientError("qBittorrent said no")

    response = client.delete(f"/api/shows/{show.id}")

    assert response.status_code == 409
    session.expire_all()
    # The show must survive, or its rule would be left downloading it.
    assert session.get(Monitored, show.id) is not None


@pytest.mark.asyncio
async def test_background_supervisor_forces_rss_refresh_only_on_first_healthy_cycle(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, qbit_host="http://qbit:8080", download_mode="direct"))
        session.commit()

    qbit = MagicMock()
    qbit.test_connection.return_value = {"app_version": "test", "api_version": "test"}
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(return_value=[])
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "QBitClient", MagicMock(return_value=qbit))
    monkeypatch.setattr(app_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(app_module, "calculate_next_poll_interval", MagicMock(return_value=(60, "normal")))

    wait_calls = 0

    async def stop_after_second_cycle(awaitable, *args, **kwargs):
        nonlocal wait_calls
        awaitable.close()
        wait_calls += 1
        if wait_calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module.asyncio, "wait_for", stop_after_second_cycle)
    await app_module.background_supervisor_task()

    # Only the first pass needs a forced refresh; the cache is then reused, and
    # paying for a refresh on every pass would slow the fast cadence down.
    assert [c.kwargs["force_rss_refresh"] for c in supervisor.run_full_cycle.await_args_list] == [
        True,
        False,
    ]


@pytest.mark.asyncio
async def test_background_supervisor_retries_failed_startup_rss_refresh(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, qbit_host="http://qbit:8080", download_mode="direct"))
        session.commit()

    qbit = MagicMock()
    qbit.test_connection.return_value = {"app_version": "test", "api_version": "test"}
    supervisor = MagicMock()
    supervisor.run_full_cycle = AsyncMock(side_effect=QbitRSSRefreshError("feed unavailable"))
    app_module.state.last_cycle_time = None
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "QBitClient", MagicMock(return_value=qbit))
    monkeypatch.setattr(app_module, "Supervisor", MagicMock(return_value=supervisor))
    monkeypatch.setattr(app_module, "QBIT_RETRY_DELAYS", (0,))

    async def cancel_after_first_cycle(awaitable, *args, **kwargs):
        awaitable.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(app_module.asyncio, "wait_for", cancel_after_first_cycle)
    await app_module.background_supervisor_task()

    # A feed that would not settle is not a lost connection: the next pass has
    # to force a refresh again rather than trust the stale snapshot.
    assert supervisor.run_full_cycle.await_args.kwargs["force_rss_refresh"] is True
    assert "RSS refresh failed" in app_module.state.next_check_reason
    assert app_module.state.last_cycle_time is None


@pytest.mark.asyncio
async def test_background_supervisor_reports_no_rules_without_warning(monkeypatch):
    """No RSS rules yet is a normal pre-bootstrap state, not an error."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, qbit_host="http://qbit:8080"))
        session.commit()

    qbit = MagicMock()
    qbit.get_rule_match_markers.return_value = {}
    # state.logs is shared process-wide, so only entries this run adds count.
    already_logged = len(app_module.state.logs)

    async def cancel_wait():
        raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "QBitClient", MagicMock(return_value=qbit))
    monkeypatch.setattr(app_module, "_wait_for_log_wakeup", cancel_wait)
    with pytest.raises(asyncio.CancelledError):
        await app_module.qbit_rule_observer_task()

    qbit.fetch_log_entries.assert_not_called()
    new_entries = list(app_module.state.logs)[already_logged:]
    assert not any("Rule check skipped" in entry["message"] for entry in new_entries)


@pytest.mark.asyncio
async def test_rule_observer_is_idle_outside_rules_mode(monkeypatch):
    """Direct mode owns its downloads, so there is no rule log to watch."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Settings(id=1, qbit_host="http://qbit:8080", download_mode="direct"))
        session.commit()

    qbit = MagicMock()

    async def cancel_wait():
        raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "QBitClient", MagicMock(return_value=qbit))
    monkeypatch.setattr(app_module, "_wait_for_log_wakeup", cancel_wait)
    with pytest.raises(asyncio.CancelledError):
        await app_module.qbit_rule_observer_task()

    qbit.get_rule_match_markers.assert_not_called()



def test_early_air_tolerance_setting_round_trips(client, session):
    res = client.get("/api/settings")
    assert res.status_code == 200
    assert res.json()["early_air_tolerance_hours"] == 6

    res = client.post("/api/settings", json={"early_air_tolerance_hours": 12})
    assert res.status_code == 200
    session.expire_all()
    assert session.exec(select(Settings)).first().early_air_tolerance_hours == 12

    res = client.get("/api/settings")
    assert res.json()["early_air_tolerance_hours"] == 12


def test_early_air_tolerance_rejects_nonsense(client):
    assert client.post("/api/settings", json={"early_air_tolerance_hours": -1}).status_code == 400
    assert client.post("/api/settings", json={"early_air_tolerance_hours": 500}).status_code == 400
    assert client.post("/api/settings", json={"early_air_tolerance_hours": 0}).status_code == 200


def test_init_db_adds_the_early_air_tolerance_column_to_a_legacy_settings_table(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY,
                qbit_host VARCHAR,
                qbit_username VARCHAR,
                qbit_password VARCHAR,
                base_dir VARCHAR,
                default_category VARCHAR,
                default_seed_ratio FLOAT,
                anilist_username VARCHAR,
                refresh_interval_minutes INTEGER,
                stall_wait_hours INTEGER,
                title_language VARCHAR
            )
        """)
        connection.exec_driver_sql("INSERT INTO settings (id, title_language) VALUES (1, 'english')")

    init_db(engine)

    with Session(engine) as session:
        settings = session.exec(select(Settings)).first()
        assert settings.download_mode == "rules"
        assert settings.backfill_window_days == 14
        assert settings.early_air_tolerance_hours == 6
    engine.dispose()
