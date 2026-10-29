import json

from starlette.requests import Request

from kisetsu.clients.anilist import AniListError
from kisetsu.clients.qbittorrent import QbitUnavailable, RssStoreFeed
from kisetsu.db.schema import AppState, Feed, FeedState, Settings, Show
from kisetsu.web.api import status as status_api


def request_for(app):
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": [], "query_string": b""})


async def test_feeds_are_added_listed_reordered_and_deleted(web):
    created = await web.client.post("/api/feeds", json={"name": "Nyaa", "url": "https://nyaa/rss"})
    other = await web.client.post("/api/feeds", json={"name": "Sub", "url": "https://sub/rss"})
    assert created.status_code == 200 and "refresh_feeds" in web.pokes
    assert [f["name"] for f in (await web.client.get("/api/feeds")).json()] == ["Nyaa", "Sub"]
    await web.client.post("/api/feeds/reorder", json={"ids": [other.json()["id"], created.json()["id"]]})
    assert [f["name"] for f in (await web.client.get("/api/feeds")).json()] == ["Sub", "Nyaa"]
    show = web.show(feed_id=created.json()["id"], feed_state=FeedState.LOCKED)
    assert (await web.client.delete(f"/api/feeds/{created.json()['id']}")).json() == {"released_shows": 1}
    assert web.load(Show, show).feed_state == FeedState.NONE


async def test_a_feed_url_is_checked_and_never_added_twice(web):
    await web.client.post("/api/feeds", json={"name": "Nyaa", "url": "https://nyaa/rss"})
    again = await web.client.post("/api/feeds", json={"name": "Again", "url": "https://nyaa/rss"})
    assert again.status_code == 400 and "already" in again.json()["detail"]
    assert (await web.client.post("/api/feeds", json={"name": "X", "url": "ftp://x"})).status_code == 422
    assert (await web.client.patch("/api/feeds/999", json={"name": "Y"})).status_code == 404


async def test_editing_a_feed_url_rereads_it(web):
    feed = web.feed()
    web.pokes.clear()
    assert (await web.client.patch(f"/api/feeds/{feed}", json={"url": "https://new/rss"})).status_code == 200
    assert web.load(Feed, feed).url == "https://new/rss" and "refresh_feeds" in web.pokes
    assert web.load(Feed, feed).last_fetch_at is None


async def test_importing_feeds_from_qbittorrent(web):
    web.qbit.rss.append(RssStoreFeed("Folder\\New", "https://new/rss"))
    assert (await web.client.post("/api/feeds/sync")).json() == {"added": 1}
    assert [f["qbit_path"] for f in (await web.client.get("/api/feeds")).json()] == ["Folder\\New"]


async def test_settings_never_expose_the_password_and_validate_their_ranges(web):
    shown = (await web.client.get("/api/settings")).json()
    assert shown["qbit_password_set"] and "qbit_password" not in shown
    assert "download_mode" not in shown
    for bad in ({"hunt_interval_minutes": 1}, {"early_air_tolerance_hours": 200}, {"accent_color": "red"}, {"title_language": "klingon"}, {"default_seed_ratio": -1}):
        assert (await web.client.post("/api/settings", json=bad)).status_code == 422, bad


async def test_saving_settings_stores_them_and_sets_the_right_jobs_going(web):
    response = await web.client.post("/api/settings", json={
        "base_dir": "/media/anime", "anilist_username": "  me ", "hunt_interval_minutes": 10, "accent_color": "#AABBCC",
    })
    assert response.status_code == 200
    s = web.load(Settings, 1)
    assert (s.base_dir, s.anilist_username, s.hunt_interval_minutes, s.accent_color) == ("/media/anime/{name}", "me", 10, "#aabbcc")
    assert {"sync_anilist", "grab", "hunt"} <= set(web.pokes)


async def test_a_new_qbittorrent_host_needs_the_password_again(web):
    refused = await web.client.post("/api/settings", json={"qbit_host": "http://elsewhere:8080"})
    assert refused.status_code == 400 and "password" in refused.json()["detail"]
    ok = await web.client.post("/api/settings", json={"qbit_host": "http://elsewhere:8080", "qbit_password": "pw"})
    assert ok.status_code == 200 and web.load(Settings, 1).qbit_password == "pw"


async def test_the_connection_test_sends_the_stored_password_only_to_the_stored_host(web):
    ok = await web.client.post("/api/settings/test-qbit", json={})
    assert ok.json()["app_version"] == "v5.3.0" and web.probed == [("http://localhost:8080", "admin", "adminadmin")]
    other = await web.client.post("/api/settings/test-qbit", json={"qbit_host": "http://evil:1"})
    assert other.status_code == 400 and len(web.probed) == 1


async def test_a_failed_connection_test_answers_400_with_the_reason(web):
    def refuse(host, username, password):
        raise QbitUnavailable("Cannot reach qBittorrent at http://localhost:8080")

    web.runtime.probe = refuse
    response = await web.client.post("/api/settings/test-qbit", json={})
    assert response.status_code == 400 and "Cannot reach qBittorrent" in response.json()["detail"]


async def test_importing_feeds_while_qbittorrent_is_down_is_an_error_answer(web):
    web.qbit.unavailable = QbitUnavailable("down")
    response = await web.client.post("/api/feeds/sync")
    assert response.status_code == 502 and "down" in response.json()["detail"]


async def test_font_files_are_served_with_a_font_type(web):
    (web.static / "fonts").mkdir()
    (web.static / "fonts" / "inter.woff2").write_bytes(b"wOF2")
    response = await web.client.get("/static/fonts/inter.woff2")
    assert response.status_code == 200 and "font" in response.headers["content-type"]


async def test_changing_the_title_language_can_move_the_folders(web):
    web.query(lambda s: (setattr(s.get(Settings, 1), "base_dir", "/media/{name}"), s.commit()))
    show = web.show(1, "Sousou no Frieren", title_english="Frieren: Beyond Journey's End")
    web.qbit.seed("Sousou no Frieren - 01", save_path="/media/Frieren Beyond Journeys End", category="")
    preview = await web.client.get("/api/settings/title-language-preview", params={"lang": "romaji"})
    assert preview.status_code == 200
    assert (await web.client.get("/api/settings/title-language-preview", params={"lang": "x"})).status_code == 400
    assert show


async def test_clearing_everything_removes_every_show(web):
    web.show(1, "A")
    web.show(2, "B")
    assert (await web.client.post("/api/settings/clear-all")).json() == {"message": "Cleared all 2 shows."}
    assert (await web.client.get("/api/shows")).json() == []


async def test_the_wizard_saves_steps_then_finishes_once(fresh):
    assert (await fresh.client.get("/api/setup/status")).json() == {"setup_completed": False, "sandbox": False}
    assert (await fresh.client.post("/api/setup/save", json={"qbit_host": "http://q:1", "qbit_password": "x"})).status_code == 200
    outside = await fresh.client.post("/api/setup/save", json={"hunt_interval_minutes": 9})
    assert outside.status_code == 400
    assert (await fresh.client.post("/api/setup/complete", json={"anilist_username": ""})).status_code == 400
    fresh.anilist.users = set()
    gone = await fresh.client.post("/api/setup/complete", json={"anilist_username": "nobody"})
    assert gone.status_code == 400 and "not found" in gone.json()["detail"]


async def test_finishing_setup_reports_an_unreachable_anilist(fresh):
    fresh.anilist.error = AniListError("down")
    response = await fresh.client.post("/api/setup/complete", json={"anilist_username": "user"})
    assert response.status_code == 502 and "down" in response.json()["detail"]
    assert (await fresh.client.get("/api/setup/status")).json()["setup_completed"] is False


async def test_finishing_setup_starts_the_engine_and_closes_the_wizard(fresh):
    done = await fresh.client.post("/api/setup/complete", json={"anilist_username": "user"})
    assert done.status_code == 200 and "startup" in fresh.pokes
    assert fresh.load(Settings, 1).anilist_username == "user"
    assert (await fresh.client.post("/api/setup/complete", json={"anilist_username": "user"})).status_code == 409
    assert (await fresh.client.post("/api/setup/save", json={"qbit_host": "http://q:1"})).status_code == 409
    assert (await fresh.client.post("/api/setup/reset")).status_code == 404


async def test_the_sandbox_allows_only_the_wizard_and_can_reset_it(sandbox):
    assert (await sandbox.client.post("/api/shows/pause-all")).status_code == 403
    assert (await sandbox.client.post("/api/setup/save", json={"qbit_host": "http://q:1", "qbit_password": "x"})).status_code == 200
    assert (await sandbox.client.post("/api/setup/reset")).status_code == 200
    assert sandbox.load(Settings, 1).qbit_host == "http://localhost:8080"
    assert sandbox.load(AppState, "setup_completed") is None


async def test_status_reports_counts_and_what_the_engine_is_doing(web):
    web.show(1, "A")
    web.feed()
    body = (await web.client.get("/api/status")).json()
    assert body["shows"] == 1 and body["feeds"] == 1 and body["by_status"] == {"testing": 1}
    assert body["ready"] is False and body["qbit_error"] is None and body["failing_feeds"] == []


async def test_logs_are_returned_newest_last(web):
    import logging

    logging.getLogger("kisetsu").addHandler(web.runtime.logs)
    try:
        logging.getLogger("kisetsu.test").warning("first")
        logging.getLogger("kisetsu.test").warning("second")
    finally:
        logging.getLogger("kisetsu").removeHandler(web.runtime.logs)
    assert [line["message"] for line in (await web.client.get("/api/logs", params={"limit": 1})).json()] == ["second"]


async def test_a_manual_refresh_pokes_the_feed_read_and_the_anilist_sync(web):
    await web.client.post("/api/refresh")
    assert {"refresh_feeds", "sync_anilist"} <= set(web.pokes) and web.runtime.jobs.refresh_all


async def test_the_event_stream_starts_with_a_refetch_then_reports_changes_and_ends_on_shutdown(web):
    response = await status_api.stream(request_for(web.app))
    stream = response.body_iterator
    assert await stream.__anext__() == "retry: 3000\n\n"
    assert json.loads((await stream.__anext__())[6:])["changed"] == ["*"]
    web.show()
    await web.client.post("/api/shows/pause-all")
    message = json.loads((await stream.__anext__())[6:])
    assert "shows" in message["changed"]
    web.runtime.engine.bus.shutdown()
    assert [chunk async for chunk in stream] == []
    assert web.runtime.engine.bus.subscribers == 0


async def test_a_foreign_host_or_origin_is_refused(web):
    assert (await web.client.get("/api/shows", headers={"host": "evil.example"})).status_code == 403
    assert (await web.client.post("/api/refresh", headers={"origin": "http://evil.example"})).status_code == 403
    assert (await web.client.get("/api/shows", headers={"host": "127.0.0.1:8085"})).status_code == 200
    assert (await web.client.post("/api/refresh", headers={"origin": "http://localhost", "host": "localhost"})).status_code == 200


async def test_the_page_is_never_cached_and_its_assets_carry_their_hash(web):
    (web.static / "app.js").write_text("console.log(1)")
    (web.static / "index.html").write_text('<script src="/static/app.js?v=@@v:app.js@@"></script>')
    page = await web.client.get("/")
    assert page.headers["cache-control"].startswith("no-store") and "app.js?v=@@" not in page.text
    asset = await web.client.get("/static/app.js", params={"v": "abc"})
    assert "immutable" in asset.headers["cache-control"]
    assert (await web.client.get("/static/app.js")).headers["cache-control"] == "no-cache"


async def test_telling_the_server_to_stop_ends_open_streams(web):
    from kisetsu.web import app as web_app

    stream = (await status_api.stream(request_for(web.app))).body_iterator
    await stream.__anext__()
    await stream.__anext__()
    assert web_app.shutdown_hooks
    for hook in list(web_app.shutdown_hooks):
        hook()
    assert [chunk async for chunk in stream] == []


async def test_the_countdown_is_to_the_next_real_read_not_the_hunts_recheck(web):
    from datetime import timedelta

    jobs = web.runtime.jobs
    web.runtime.scheduler.add("refresh_feeds", jobs.refresh_feeds, interval=None, first=6 * 3600.0)
    web.runtime.scheduler.add("hunt", jobs.hunt, interval=None, first=3600.0)
    assert 6 * 3600 - 5 <= (await web.client.get("/api/status")).json()["next_feed_read_in"] <= 6 * 3600
    jobs.hunt_read_at = web.clock() + timedelta(hours=2)
    assert (await web.client.get("/api/status")).json()["next_feed_read_in"] == 2 * 3600
