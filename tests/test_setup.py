import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from kisetsu.clients.anilist import AniListClient, AniListError
from kisetsu.db.models import Feed, Settings
from kisetsu.db.session import get_engine, init_db
from kisetsu.server import api as api_module
from kisetsu.server.api import get_db, get_qbit

app_module = importlib.import_module("kisetsu.server.app")


@pytest.fixture
def fresh_engine():
    """A brand-new database: the one settings row has not been through the wizard."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Settings(id=1, qbit_host="http://localhost:8080"))
        s.commit()
    return engine


def _make_client(engine, monkeypatch, sandbox=False):
    async def idle_task():
        await asyncio.Event().wait()

    for name in ("background_supervisor_task", "qbit_rule_observer_task", "settle_task", "ingest_task"):
        monkeypatch.setattr(app_module, name, idle_task)
    monkeypatch.setattr(app_module, "get_engine", lambda: engine)
    monkeypatch.setattr(app_module, "SANDBOX", sandbox)
    monkeypatch.setattr(api_module, "SANDBOX", sandbox)
    app = app_module.create_app()

    def override_get_db():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_qbit] = lambda: MagicMock()
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def setup_client(fresh_engine, monkeypatch):
    with _make_client(fresh_engine, monkeypatch) as client:
        yield client


@pytest.fixture
def sandbox_client(fresh_engine, monkeypatch):
    with _make_client(fresh_engine, monkeypatch, sandbox=True) as client:
        yield client


def _settings(engine) -> Settings:
    with Session(engine) as s:
        return s.exec(select(Settings)).first()


def test_old_database_counts_as_already_set_up(tmp_path):
    engine = get_engine(tmp_path / "old.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Settings(id=1, qbit_host="http://qbit:8080"))
        s.commit()
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE settings DROP COLUMN setup_completed")

    init_db(engine)

    assert _settings(engine).setup_completed is True


def test_new_database_starts_unfinished(tmp_path):
    engine = get_engine(tmp_path / "new.db")

    init_db(engine)

    assert _settings(engine).setup_completed is False


def test_setup_status_reports_the_flag(setup_client, fresh_engine):
    assert setup_client.get("/api/setup/status").json() == {"setup_completed": False, "sandbox": False}
    with Session(fresh_engine) as s:
        row = s.exec(select(Settings)).first()
        row.setup_completed = True
        s.add(row)
        s.commit()
    assert setup_client.get("/api/setup/status").json()["setup_completed"] is True


def test_setup_save_writes_the_step_without_preparing_the_mode(setup_client, fresh_engine, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the wizard must not prepare a download mode")

    monkeypatch.setattr(api_module.Supervisor, "prepare_download_mode", fail)
    res = setup_client.post("/api/setup/save", json={
        "qbit_host": "http://localhost:9090",
        "qbit_username": "me",
        "qbit_password": "secret",
        "download_mode": "Rules",
        "base_dir": "/data/anime",
        "default_category": "Anime",
        "default_seed_ratio": 2.0,
        "title_language": "romaji",
        "accent_color": "#FB7185",
        "desktop_notifications": True,
    })

    assert res.status_code == 200
    s = _settings(fresh_engine)
    assert (s.qbit_host, s.qbit_username, s.qbit_password) == ("http://localhost:9090", "me", "secret")
    assert s.download_mode == "rules"
    assert s.base_dir == "/data/anime/{name}"
    assert (s.default_category, s.default_seed_ratio, s.title_language) == ("Anime", 2.0, "romaji")
    assert s.accent_color == "#fb7185"
    assert s.desktop_notifications is True
    assert s.setup_completed is False


@pytest.mark.parametrize("body", [
    {"download_mode": "observe"},
    {"accent_color": "red"},
    {"title_language": "klingon"},
    {"default_seed_ratio": -1},
])
def test_setup_save_rejects_bad_values(setup_client, body):
    assert setup_client.post("/api/setup/save", json=body).status_code == 400


def test_setup_save_needs_the_password_to_move_the_host(setup_client, fresh_engine):
    with Session(fresh_engine) as s:
        row = s.exec(select(Settings)).first()
        row.qbit_password = "stored"
        s.add(row)
        s.commit()

    res = setup_client.post("/api/setup/save", json={"qbit_host": "http://elsewhere:8080"})

    assert res.status_code == 400
    assert _settings(fresh_engine).qbit_host == "http://localhost:8080"


def test_setup_is_closed_once_finished(setup_client, fresh_engine):
    with Session(fresh_engine) as s:
        row = s.exec(select(Settings)).first()
        row.setup_completed = True
        s.add(row)
        s.commit()

    assert setup_client.post("/api/setup/save", json={"qbit_username": "x"}).status_code == 409
    assert setup_client.post("/api/setup/complete", json={"anilist_username": "x"}).status_code == 409


def test_setup_complete_marks_it_done_and_wakes_the_loops(setup_client, fresh_engine, monkeypatch):
    monkeypatch.setattr(api_module.anilist_client, "user_exists", AsyncMock(return_value=True))
    wake = MagicMock()
    monkeypatch.setattr(api_module.state, "trigger_immediate_cycle", wake)

    res = setup_client.post("/api/setup/complete", json={"anilist_username": "  alvaro "})

    assert res.status_code == 200
    s = _settings(fresh_engine)
    assert s.setup_completed is True
    assert s.anilist_username == "alvaro"
    wake.assert_called_once()


def test_setup_complete_rejects_unknown_and_blank_users(setup_client, fresh_engine, monkeypatch):
    monkeypatch.setattr(api_module.anilist_client, "user_exists", AsyncMock(return_value=False))

    unknown = setup_client.post("/api/setup/complete", json={"anilist_username": "nobody"})
    blank = setup_client.post("/api/setup/complete", json={"anilist_username": "  "})

    assert unknown.status_code == 400
    assert "nobody" in unknown.json()["detail"]
    assert blank.status_code == 400
    assert _settings(fresh_engine).setup_completed is False


def test_setup_complete_reports_an_unreachable_anilist(setup_client, fresh_engine, monkeypatch):
    monkeypatch.setattr(api_module.anilist_client, "user_exists", AsyncMock(side_effect=AniListError("down")))

    res = setup_client.post("/api/setup/complete", json={"anilist_username": "alvaro"})

    assert res.status_code == 502
    assert _settings(fresh_engine).setup_completed is False


def test_setup_reset_only_exists_in_the_sandbox(setup_client):
    assert setup_client.post("/api/setup/reset").status_code == 404


def test_sandbox_reset_gives_back_a_first_run(sandbox_client, fresh_engine):
    with Session(fresh_engine) as s:
        s.add(Feed(qbit_feed_name="Feed", qbit_feed_url="https://example.com/rss"))
        row = s.exec(select(Settings)).first()
        row.setup_completed = True
        row.qbit_username = "changed"
        s.add(row)
        s.commit()

    assert sandbox_client.post("/api/setup/reset").status_code == 200

    with Session(fresh_engine) as s:
        assert s.exec(select(Feed)).all() == []
        settings = s.exec(select(Settings)).all()
    assert len(settings) == 1
    assert settings[0].setup_completed is False
    assert settings[0].qbit_username == "admin"


def test_sandbox_refuses_writes_that_could_reach_the_real_qbit(sandbox_client):
    assert sandbox_client.post("/api/cycle/run").status_code == 403
    assert sandbox_client.post("/api/settings", json={"download_mode": "rules"}).status_code == 403
    assert sandbox_client.post("/api/settings/sync-anilist").status_code == 403
    assert sandbox_client.post("/api/settings/clear-all").status_code == 403


def test_sandbox_still_lets_the_wizard_work(sandbox_client, monkeypatch):
    connection = MagicMock()
    connection.test_connection.return_value = {"app_version": "5.0", "api_version": "2.11"}
    monkeypatch.setattr(api_module, "QBitClient", MagicMock(return_value=connection))

    assert sandbox_client.get("/api/setup/status").json()["sandbox"] is True
    assert sandbox_client.post("/api/setup/save", json={"qbit_username": "me"}).status_code == 200
    assert sandbox_client.post("/api/settings/test-qbit", json={"qbit_password": "x"}).status_code == 200


def _answer(status_code, payload=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    return response


@pytest.mark.parametrize("response, expected", [
    (_answer(200, {"data": {"User": {"id": 5}}}), True),
    (_answer(200, {"data": {"User": None}}), False),
    (_answer(404, {"errors": [{"message": "Not Found."}]}), False),
])
async def test_user_exists_reads_the_answer(monkeypatch, response, expected):
    post = AsyncMock(return_value=response)
    monkeypatch.setattr("httpx2.AsyncClient.post", post)

    assert await AniListClient().user_exists("alvaro") is expected
    post.assert_awaited_once()


async def test_user_exists_raises_when_anilist_misbehaves(monkeypatch):
    monkeypatch.setattr("httpx2.AsyncClient.post", AsyncMock(return_value=_answer(429)))

    with pytest.raises(AniListError):
        await AniListClient().user_exists("alvaro")
