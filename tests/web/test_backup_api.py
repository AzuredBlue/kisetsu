import asyncio
import io
import json
import sqlite3
import zipfile
from urllib.parse import quote

from sqlmodel import select

from kisetsu.db.migrations import LATEST_VERSION
from kisetsu.db.schema import AppState, Base, Download, DownloadState, Episode, EpisodeStatus, Event, EventKind, Feed, FeedItem, FeedState, Settings, Show
from kisetsu.services import backup as service
from kisetsu.services.backup import FROM_NEWER_RELEASE, NOT_A_BACKUP
from tests.fakes import START


def fill(web):
    """A little of everything, on qBittorrent at the host of the settings."""
    feed = web.feed(name="Nyaa", url="https://nyaa/rss?key=secret")
    show = web.show(feed_id=feed, feed_state=FeedState.LOCKED)

    def more(session):
        settings = session.get(Settings, 1)
        settings.qbit_host = "http://nas:8080"
        settings.qbit_password = "hunter2"
        settings.anilist_username = "someone"
        settings.accent_color = "#123456"
        session.add(settings)
        episode = session.exec(select(Episode).where(Episode.show_id == show, Episode.number == 1)).one()
        episode.status = EpisodeStatus.COMPLETED
        session.add(episode)
        session.add(FeedItem(feed_id=feed, guid="g1", title="[Sub] Sousou no Frieren - 01", published_at=START))
        first = Download(episode_id=episode.id, feed_id=feed, title="v1", state=DownloadState.DONE, info_hash="a" * 40)
        session.add(first)
        session.flush()
        session.add(Download(episode_id=episode.id, feed_id=feed, title="v2", state=DownloadState.DOWNLOADING, info_hash="b" * 40, supersedes_id=first.id))
        session.add(Event(kind=EventKind.INFO, message="something happened", show_id=show))
        session.commit()

    web.query(more)
    return show


async def export(web, theme="light"):
    response = await web.client.get("/api/backup/export", params={"theme": theme})
    assert response.status_code == 200, response.text
    return response


def members(content):
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        return json.loads(bundle.read("manifest.json")), bundle.read("kisetsu.db")


def rezip(content, *, manifest=None, database=None):
    old_manifest, old_database = members(content)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({**old_manifest, **(manifest or {})}))
        bundle.writestr("kisetsu.db", old_database if database is None else database)
    return out.getvalue()


def every_row(path):
    """The raw contents of every table, as SQLite stores them."""
    conn = sqlite3.connect(path)
    try:
        return {table.name: sorted(map(repr, conn.execute(f"SELECT * FROM {table.name}"))) for table in Base.metadata.sorted_tables}
    finally:
        conn.close()


def wipe(web):
    """The instance moves on after the backup was taken."""

    def change(session):
        for row in session.exec(select(Show)).all():
            session.delete(row)
        settings = session.get(Settings, 1)
        settings.accent_color = "#654321"
        session.add(settings)
        session.add(Feed(name="Other", url="https://other/rss"))
        session.commit()

    web.query(change)


async def test_export_is_one_named_zip_with_a_manifest_and_no_password(web, tmp_path):
    fill(web)
    response = await export(web)
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="kisetsu-backup-' in response.headers["content-disposition"]
    manifest, database = members(response.content)
    assert manifest["app"] == "kisetsu" and manifest["schema"] == LATEST_VERSION and manifest["theme"] == "light"
    assert b"hunter2" not in database and b"hunter2" not in response.content
    path = tmp_path / "exported.db"
    path.write_bytes(database)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT qbit_password, qbit_host FROM settings").fetchone() == ("", "http://nas:8080")
    assert conn.execute("SELECT count(*) FROM shows").fetchone() == (1,)
    assert conn.execute("PRAGMA user_version").fetchone() == (LATEST_VERSION,)
    conn.close()


async def test_export_leaves_no_temp_files_and_an_unknown_theme_is_dropped(web, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    manifest, _ = members((await export(web, theme="<script>")).content)
    assert manifest["theme"] is None
    assert list(scratch.iterdir()) == []


async def test_inspect_compares_the_backup_with_what_is_here_and_changes_nothing(web):
    fill(web)
    content = (await export(web)).content
    wipe(web)
    response = await web.client.post("/api/backup/inspect", content=content)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["backup"] == {"shows": 1, "feeds": 1, "downloaded": 1, "qbit_host": "http://nas:8080", "anilist_username": "someone"}
    assert plan["current"]["shows"] == 0 and plan["current"]["feeds"] == 2
    assert plan["password_needed"] is False and plan["exported_at"]
    assert web.query(lambda s: s.exec(select(Show)).all()) == []


async def test_restore_brings_every_table_back_and_keeps_the_password_for_the_same_host(web, tmp_path):
    show = fill(web)
    before = every_row(tmp_path / "k.db")
    content = (await export(web)).content
    wipe(web)
    response = await web.client.post("/api/backup/restore", content=content)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["theme"] == "light" and result["shows"] == 1

    assert every_row(tmp_path / "k.db") == before
    settings = web.load(Settings, 1)
    assert settings.accent_color == "#123456" and settings.qbit_password == "hunter2"
    assert web.load(Show, show).feed_state == FeedState.LOCKED
    assert web.episode(show, 1).status == EpisodeStatus.COMPLETED
    assert "sync_anilist" in web.pokes


async def test_restore_saves_what_was_here_as_a_backup_that_restores(web, tmp_path):
    fill(web)
    content = (await export(web)).content
    wipe(web)
    result = (await web.client.post("/api/backup/restore", content=content)).json()
    saved = list(tmp_path.glob("k.db.pre-restore.*.zip"))
    assert len(saved) == 1 and saved[0].name in result["saved_as"]
    assert (await web.client.post("/api/backup/restore", content=saved[0].read_bytes())).status_code == 200
    assert web.load(Settings, 1).accent_color == "#654321"
    assert "Other" in [f.name for f in web.query(lambda s: s.exec(select(Feed)).all())]
    assert web.query(lambda s: s.exec(select(Show)).all()) == []


async def test_a_backup_from_another_host_asks_for_the_password_and_never_sends_the_saved_one(web):
    fill(web)
    content = (await export(web)).content

    def elsewhere(session):
        settings = session.get(Settings, 1)
        settings.qbit_host = "http://evil:8080"
        session.add(settings)
        session.commit()

    web.query(elsewhere)
    assert (await web.client.post("/api/backup/inspect", content=content)).json()["password_needed"] is True
    assert (await web.client.post("/api/backup/restore", content=content)).status_code == 200
    settings = web.load(Settings, 1)
    assert settings.qbit_host == "http://nas:8080" and settings.qbit_password == ""

    web.query(elsewhere)
    typed = "pä ss+wörd"
    response = await web.client.post("/api/backup/restore", content=content, headers={"X-Kisetsu-Qbit-Password": quote(typed)})
    assert response.status_code == 200
    assert web.load(Settings, 1).qbit_password == typed


async def test_a_fresh_install_needs_the_password_even_on_the_default_host(web):
    content = (await export(web)).content

    def unfinished(session):
        session.delete(session.get(AppState, "setup_completed"))
        session.commit()

    web.query(unfinished)
    assert (await web.client.post("/api/backup/inspect", content=content)).json()["password_needed"] is True
    assert (await web.client.post("/api/backup/restore", content=content)).status_code == 200
    assert web.load(AppState, "setup_completed").value == "1"


async def test_rows_left_without_their_parent_do_not_block_a_backup(web, tmp_path):
    show = fill(web)
    conn = sqlite3.connect(tmp_path / "k.db")  # foreign keys are off on a plain connection
    conn.execute("DELETE FROM shows WHERE id = ?", (show,))
    conn.commit()
    conn.close()
    assert every_row(tmp_path / "k.db")["episodes"]

    response = await export(web)
    plan = await web.client.post("/api/backup/inspect", content=response.content)
    assert plan.status_code == 200, plan.text
    restored = await web.client.post("/api/backup/restore", content=response.content)
    assert restored.status_code == 200, restored.text
    assert every_row(tmp_path / "k.db")["episodes"] == [] and every_row(tmp_path / "k.db")["downloads"] == []
    assert [f.name for f in web.query(lambda s: s.exec(select(Feed)).all())] == ["Nyaa"]


async def test_restore_clears_what_the_engine_remembers(web):
    fill(web)
    content = (await export(web)).content
    caches = web.runtime.engine.caches
    caches.absent["a" * 40] = 1
    caches.known_items = {(1, "stale")}
    assert (await web.client.post("/api/backup/restore", content=content)).status_code == 200
    assert web.runtime.engine.caches.absent == {} and web.runtime.engine.caches.known_items is None


async def test_files_that_are_not_a_good_backup_are_refused_and_nothing_changes(web, tmp_path):
    fill(web)
    content = (await export(web)).content

    other_app = tmp_path / "other.db"
    conn = sqlite3.connect(other_app)
    conn.execute("CREATE TABLE notes (id INTEGER)")
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION}")
    conn.commit()
    conn.close()

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    bad_enum = tmp_path / "enum.db"
    bad_enum.write_bytes(members(content)[1])
    conn = sqlite3.connect(bad_enum)
    conn.execute("UPDATE episodes SET status = 'NOT_A_STATUS'")
    conn.commit()
    conn.close()

    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as bundle:
        bundle.writestr("readme.txt", "hello")

    cases = {
        "text": (b"not a zip at all", NOT_A_BACKUP),
        "empty body": (b"", NOT_A_BACKUP),
        "another zip": (plain_zip.getvalue(), NOT_A_BACKUP),
        "another app's manifest": (rezip(content, manifest={"app": "other"}), NOT_A_BACKUP),
        "not sqlite": (rezip(content, database=b"garbage" * 1000), NOT_A_BACKUP),
        "another app's database": (rezip(content, database=other_app.read_bytes()), NOT_A_BACKUP),
        "an empty database": (rezip(content, database=empty.read_bytes()), NOT_A_BACKUP),
        "a row that cannot load": (rezip(content, database=bad_enum.read_bytes()), NOT_A_BACKUP),
        "a newer format": (rezip(content, manifest={"format": 999}), FROM_NEWER_RELEASE),
    }
    wipe(web)
    for name, (body, message) in cases.items():
        for path in ("/api/backup/inspect", "/api/backup/restore"):
            response = await web.client.post(path, content=body)
            assert (response.status_code, response.json()["detail"]) == (400, message), name
    assert web.load(Settings, 1).accent_color == "#654321"
    assert web.query(lambda s: s.exec(select(Show)).all()) == []


async def test_a_database_from_a_newer_release_is_refused(web, tmp_path):
    content = (await export(web)).content
    path = tmp_path / "newer.db"
    path.write_bytes(members(content)[1])
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    conn.commit()
    conn.close()
    response = await web.client.post("/api/backup/restore", content=rezip(content, database=path.read_bytes()))
    assert (response.status_code, response.json()["detail"]) == (400, FROM_NEWER_RELEASE)


async def test_a_backup_from_an_older_release_is_migrated(web, tmp_path):
    show = fill(web)
    content = (await export(web)).content
    path = tmp_path / "older.db"
    path.write_bytes(members(content)[1])
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE shows DROP COLUMN cover_edge")
    conn.execute("ALTER TABLE shows DROP COLUMN cover_edge_src")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()
    response = await web.client.post("/api/backup/restore", content=rezip(content, database=path.read_bytes()))
    assert response.status_code == 200, response.text
    assert web.load(Show, show).cover_edge is None


async def test_a_zip_entry_cannot_write_outside_the_scratch_folder(web, tmp_path):
    content = (await export(web)).content
    manifest, database = members(content)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("kisetsu.db", database)
        bundle.writestr("../../escaped.txt", "x")
    assert (await web.client.post("/api/backup/restore", content=out.getvalue())).status_code == 200
    assert not list(tmp_path.rglob("escaped.txt"))


async def test_a_failed_restore_rolls_back_and_the_jobs_start_again(web, monkeypatch):
    fill(web)
    content = (await export(web)).content
    wipe(web)
    jobs = web.runtime.jobs

    async def job():
        await asyncio.sleep(3600)

    scheduler = web.runtime.scheduler
    scheduler.add("startup", job, interval=None, first=0.0)
    scheduler.start()
    registered = []
    monkeypatch.setattr(jobs, "register", lambda: registered.append(1))
    real_copy = service._copy_tables
    calls = []

    def fails_on_the_live_database(rows, target):
        calls.append(1)
        if len(calls) == 1:
            return real_copy(rows, target)  # the safety copy
        target.execute(Settings.__table__.update().values(accent_color="#ffffff"))
        raise LookupError("midway")

    monkeypatch.setattr(service, "_copy_tables", fails_on_the_live_database)
    response = await web.client.post("/api/backup/restore", content=content)
    assert response.status_code == 400 and "nothing changed" in response.json()["detail"]
    assert web.load(Settings, 1).accent_color == "#654321"
    assert registered == [1] and scheduler.running
    await scheduler.stop()


async def test_restore_stops_running_jobs_and_runs_them_from_the_start(web):
    fill(web)
    content = (await export(web)).content
    scheduler = web.runtime.scheduler
    jobs = web.runtime.jobs
    jobs.register()
    jobs.backfill_requests.add(99)
    old = dict(scheduler.jobs)
    # Nothing runs before the restore: only its effect on the wiring is under test.
    for job in scheduler.jobs.values():
        job.due = None
    scheduler.start()
    assert (await web.client.post("/api/backup/restore", content=content)).status_code == 200
    assert scheduler.running and all(scheduler.jobs[name] is not old[name] for name in old)
    assert jobs.backfill_requests == set()
    await scheduler.stop()


async def test_the_sandbox_refuses_to_restore(sandbox):
    assert (await sandbox.client.post("/api/backup/restore", content=b"x")).status_code == 403
    assert (await sandbox.client.post("/api/backup/inspect", content=b"x")).status_code == 403


async def test_a_page_on_another_site_cannot_restore(web):
    response = await web.client.post("/api/backup/restore", content=b"x", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
