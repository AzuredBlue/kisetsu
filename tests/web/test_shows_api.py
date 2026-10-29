from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from kisetsu.db.schema import (
    Download,
    DownloadState,
    Episode,
    EpisodeStatus,
    Event,
    EventKind,
    Feed,
    FeedItem,
    FeedItemMatch,
    FeedState,
    PausedBy,
    Show,
)
from kisetsu.clients.qbittorrent import QbitError
from kisetsu.db.schema import Settings
from kisetsu.services import downloads, ingest, manual
from tests.fakes import START, article


async def test_the_shows_list_gives_each_show_a_status_label_and_progress(web):
    feed = web.feed()
    web.show(1, "Locked", feed_id=feed, feed_state=FeedState.LOCKED, total_episodes=12)
    web.show(2, "Plain")
    cards = {c["title"]: c for c in (await web.client.get("/api/shows")).json()}
    assert cards["Locked"]["status_label"] == "Working" and cards["Locked"]["feed"]["state_label"] == "Working"
    assert cards["Locked"]["progress"] == {"done": 0, "aired": 3, "total": 12}
    assert cards["Plain"]["status_label"] == "Testing" and cards["Plain"]["progress"]["total"] is None


async def test_a_show_that_finished_airing_is_in_the_completed_section_while_it_still_downloads(web):
    finished = web.show(1, "Finished", total_episodes=3)
    web.show(2, "Airing")

    def finish(session):
        session.get(Show, finished).anilist_status = "FINISHED"
        session.commit()

    web.query(finish)
    cards = {c["title"]: c for c in (await web.client.get("/api/shows")).json()}
    assert cards["Finished"]["section"] == "completed" and not cards["Finished"]["completed"]
    assert cards["Airing"]["section"] == "releasing"


async def test_only_what_needs_attention_is_listed_in_plain_words(web):
    show = web.show(1, "Frieren")

    def fail(session):
        episodes = session.query(Episode).filter(Episode.show_id == show).order_by(Episode.number).all()
        episodes[0].status, episodes[1].status = EpisodeStatus.FAILED, EpisodeStatus.MISSED
        session.commit()

    web.query(fail)
    card = (await web.client.get("/api/shows")).json()[0]
    assert card["attention"] == ["1 failed", "1 missed"]


async def test_reading_never_reaches_qbittorrent(web):
    show = web.show()
    web.feed()
    for path in ("/api/shows", f"/api/shows/{show}", f"/api/shows/{show}/feed-matches", "/api/feeds", "/api/settings", "/api/status", "/api/history"):
        assert (await web.client.get(path)).status_code == 200, path
    assert web.qbit.calls == []


async def test_the_show_page_lists_episodes_with_their_release(web):
    show = web.show(episodes=2)
    page = (await web.client.get(f"/api/shows/{show}")).json()
    assert [e["number"] for e in page["episodes"]] == [1, 2]
    assert page["episodes"][0]["release"] is None and page["episodes"][0]["status"] == "wanted"
    assert (await web.client.get("/api/shows/999")).status_code == 404


async def test_pausing_and_resuming_a_show(web):
    show = web.show()
    assert (await web.client.post(f"/api/shows/{show}/pause")).status_code == 200
    card = (await web.client.get(f"/api/shows/{show}")).json()
    assert card["paused"] and card["status_label"] == "Paused" and card["paused_by"] == "user"
    await web.client.post(f"/api/shows/{show}/resume")
    assert not web.load(Show, show).paused and "grab" in web.pokes


async def test_resume_all_only_resumes_what_pause_all_paused(web):
    mine, other = web.show(1, "Mine"), web.show(2, "Other")
    await web.client.post(f"/api/shows/{mine}/pause")
    await web.client.post("/api/shows/resume-all")
    await web.client.post("/api/shows/pause-all")
    assert web.load(Show, other).paused_by == PausedBy.ALL
    await web.client.post("/api/shows/resume-all")
    assert web.load(Show, mine).paused and not web.load(Show, other).paused


async def test_auto_discover_all_releases_pins_but_not_proven_feeds(web):
    feed = web.feed()
    pinned = web.show(1, "Pinned", feed_id=feed, feed_state=FeedState.PINNED)
    locked = web.show(2, "Locked", feed_id=feed, feed_state=FeedState.LOCKED)
    assert (await web.client.post("/api/shows/auto-discover-all")).json() == {"released": 1}
    assert web.load(Show, pinned).feed_state == FeedState.NONE and web.load(Show, locked).feed_state == FeedState.LOCKED


async def test_pinning_a_feed_and_moving_off_a_locked_one_needs_confirmation(web):
    top, low = web.feed("Top", "https://top/rss", 1), web.feed("Low", "https://low/rss", 2)
    show = web.show(feed_id=top, feed_state=FeedState.LOCKED)
    refused = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": low})
    assert refused.status_code == 409 and refused.json()["code"] == "feed_locked"
    assert web.load(Show, show).feed_state == FeedState.LOCKED
    moved = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": low, "release_lock": True})
    assert moved.status_code == 200 and "Pinned to 'Low'" in moved.json()["message"]
    assert web.load(Show, show).feed_state == FeedState.PINNED


async def test_editing_the_save_folder_and_aliases(web):
    show = web.show()
    response = await web.client.post(f"/api/shows/{show}/edit", json={"save_folder": "Frieren S2", "aliases": ["Frieren 2"]})
    assert response.status_code == 200
    loaded = web.load(Show, show)
    assert loaded.save_folder == "Frieren S2" and loaded.custom_aliases == ["Frieren 2"]
    assert (await web.client.post(f"/api/shows/{show}/edit", json={"bogus": 1})).status_code == 422


async def test_deleting_a_show_dismisses_it_and_reports_the_torrents(web):
    show = web.show()
    response = await web.client.delete(f"/api/shows/{show}")
    assert response.status_code == 200 and response.json() == {"deleted_torrents": 0, "folder": None}
    assert web.load(Show, show) is None
    assert (await web.client.delete(f"/api/shows/{show}")).status_code == 404


async def test_search_older_reports_what_it_found_and_pokes_the_grab(web):
    show = web.show()
    web.feed()
    response = await web.client.post(f"/api/shows/{show}/search-releases")
    assert response.status_code == 200 and "found" in response.json()


async def test_the_history_lists_downloads_and_can_be_cleared(web):
    show = web.show()

    def add(session):
        session.add(Event(at=START, kind=EventKind.GRABBED, show_id=show, episode=1, title="Frieren - 01", message="Queued"))
        session.add(Event(at=START, kind=EventKind.INFO, message="not history"))
        session.commit()

    web.query(add)
    rows = (await web.client.get("/api/history")).json()
    assert [(r["show"], r["episode"], r["kind"]) for r in rows] == [("Sousou no Frieren", 1, "grabbed")]
    await web.client.delete("/api/history")
    assert (await web.client.get("/api/history")).json() == []


async def test_a_release_that_was_queued_and_finished_is_one_history_row(web):
    show = web.show()

    def add(session):
        session.add(Event(at=START, kind=EventKind.GRABBED, show_id=show, episode=1, title="Frieren - 01", message="Queued"))
        session.add(Event(at=START + timedelta(minutes=5), kind=EventKind.DONE, show_id=show, episode=1, title="Frieren - 01", message="Done"))
        session.add(Event(at=START + timedelta(minutes=6), kind=EventKind.GRABBED, show_id=show, episode=2, title="Frieren - 02", message="Queued"))
        session.commit()

    web.query(add)
    rows = (await web.client.get("/api/history?limit=2")).json()
    assert [(r["episode"], r["kind"]) for r in rows] == [(2, "grabbed"), (1, "done")]


async def test_feed_items_list_newest_first_with_their_shows_and_what_became_of_them(web):
    top, low = web.feed("Top", "https://top/rss", 1), web.feed("Low", "https://low/rss", 2)
    show = web.show()
    episode = web.episode(show, 1)

    def add(session):
        old = FeedItem(feed_id=top, guid="a", title="[Grp] Frieren - 01", published_at=START - timedelta(days=2), first_seen_at=START)
        mid = FeedItem(feed_id=top, guid="b", title="[Grp] Frieren - 02", published_at=START - timedelta(hours=2), first_seen_at=START)
        other = FeedItem(feed_id=low, guid="c", title="[Grp] Something else - 07", published_at=START - timedelta(hours=1), first_seen_at=START)
        session.add_all([old, mid, other])
        session.flush()
        session.add(FeedItemMatch(item_id=old.id, show_id=show))
        session.add(FeedItemMatch(item_id=mid.id, show_id=show))
        session.add(Download(episode_id=episode.id, feed_id=top, feed_item_guid="a", title="x", state=DownloadState.DONE))
        session.add(Download(episode_id=episode.id, feed_id=top, feed_item_guid="b", title="x", state=DownloadState.FAILED))
        session.commit()

    web.query(add)
    everything = (await web.client.get("/api/feeds/items")).json()
    assert [i["title"] for i in everything["items"]] == ["[Grp] Something else - 07", "[Grp] Frieren - 02", "[Grp] Frieren - 01"]
    by_title = {i["title"]: i for i in everything["items"]}
    assert by_title["[Grp] Frieren - 01"]["download"] == "Downloaded" and by_title["[Grp] Frieren - 02"]["download"] is None
    assert by_title["[Grp] Frieren - 01"]["shows"] == [{"id": show, "title": "Sousou no Frieren"}]
    assert by_title["[Grp] Something else - 07"]["shows"] == [] and by_title["[Grp] Something else - 07"]["feed"] == "Low"
    only_low = (await web.client.get(f"/api/feeds/items?feed_id={low}")).json()
    assert [i["feed"] for i in only_low["items"]] == ["Low"]
    mine = (await web.client.get("/api/feeds/items?matched=true")).json()
    assert [i["title"] for i in mine["items"]] == ["[Grp] Frieren - 02", "[Grp] Frieren - 01"]


async def test_feed_items_come_in_pages_and_count_what_is_new(web):
    feed = web.feed()

    def add(session):
        for n in range(5):
            session.add(FeedItem(feed_id=feed, guid=str(n), title=f"item {n}", published_at=START - timedelta(hours=n), first_seen_at=START - timedelta(days=3 if n == 4 else 0)))
        session.commit()

    web.query(add)
    first = (await web.client.get("/api/feeds/items?limit=2")).json()
    second = (await web.client.get("/api/feeds/items?limit=2&offset=2")).json()
    last = (await web.client.get("/api/feeds/items?limit=2&offset=4")).json()
    assert [i["title"] for i in first["items"] + second["items"] + last["items"]] == [f"item {n}" for n in range(5)]
    assert first["total"] == second["total"] == last["total"] == 5
    assert first["new_recent"] == 4


async def test_reading_a_busy_show_never_reaches_qbittorrent(web):
    """Matched releases, a managed episode, a forgotten one and a replacement in flight."""
    feed = web.feed()
    show_id = web.show(episodes=3, first_air=START - timedelta(days=14), feed_id=feed, feed_state=FeedState.LOCKED)
    engine = web.runtime.engine
    v1, v2 = "[Grp] Sousou no Frieren - 03 (1080p).mkv", "[Grp] Sousou no Frieren - 03v2 (1080p).mkv"

    def post(ctx, title):
        ingest.ingest(ctx, ctx.session.get(Feed, feed), [article(title, ctx.now)])

    def grab_by_hand(ctx, title):
        return manual.manual_grab(ctx, ctx.session.get(Show, show_id), title)

    await engine.run(post, v1)
    await engine.run(grab_by_hand, v1)
    web.qbit.finish(next(iter(web.qbit.truth)))
    await engine.run(downloads.settle)
    await engine.run(post, v2)
    await engine.run(grab_by_hand, v2)

    def forget_episode_one(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show_id, Episode.number == 1)).one()
        row = Download(episode_id=episode.id, title="[Grp] Sousou no Frieren - 01.mkv", state=DownloadState.DONE, info_hash=None)
        session.add(row)
        session.flush()
        episode.status, episode.current_download_id = EpisodeStatus.COMPLETED, row.id
        session.commit()

    web.query(forget_episode_one)
    web.qbit.calls.clear()
    page = (await web.client.get(f"/api/shows/{show_id}")).json()
    matches = await web.client.get(f"/api/shows/{show_id}/feed-matches")
    assert matches.status_code == 200 and (await web.client.get("/api/shows")).status_code == 200
    by_number = {e["number"]: e for e in page["episodes"]}
    assert by_number[3]["replacing"]["version"] == 2 and by_number[1]["release"]["tracked"] is False
    assert web.qbit.calls == []


async def test_a_manual_pause_or_resume_during_pause_all_drops_the_bulk_marker(web):
    resumed, repaused, untouched = web.show(1, "A"), web.show(2, "B"), web.show(3, "C")
    await web.client.post("/api/shows/pause-all")
    await web.client.post(f"/api/shows/{resumed}/resume")
    await web.client.post(f"/api/shows/{repaused}/pause")
    assert web.load(Show, repaused).paused_by == PausedBy.USER and web.load(Show, untouched).paused_by == PausedBy.ALL
    assert (await web.client.post("/api/shows/resume-all")).json() == {"resumed": 1}
    assert not web.load(Show, resumed).paused and web.load(Show, repaused).paused and not web.load(Show, untouched).paused


async def test_editing_other_fields_leaves_the_feed_and_pin_alone(web):
    feed = web.feed()
    show = web.show(feed_id=feed, feed_state=FeedState.PINNED)
    response = await web.client.post(f"/api/shows/{show}/edit", json={"category": "Movies", "seed_ratio": 2.0, "save_folder": "Frieren"})
    assert response.status_code == 200
    loaded = web.load(Show, show)
    assert (loaded.feed_id, loaded.feed_state, loaded.category, loaded.seed_ratio) == (feed, FeedState.PINNED, "Movies", 2.0)


async def test_a_category_only_edit_keeps_the_learned_match_and_the_pause(web):
    feed = web.feed()
    show = web.show(feed_id=feed, feed_state=FeedState.LOCKED, learned_name="Frieren", learned_group="Grp", paused=True, paused_by=PausedBy.USER)
    assert (await web.client.post(f"/api/shows/{show}/edit", json={"category": "Anime"})).status_code == 200
    loaded = web.load(Show, show)
    assert (loaded.feed_state, loaded.learned_name, loaded.learned_group, loaded.paused) == (FeedState.LOCKED, "Frieren", "Grp", True)


async def test_choosing_auto_discover_for_a_locked_show_is_refused_until_the_lock_is_released(web):
    feed = web.feed()
    show = web.show(feed_id=feed, feed_state=FeedState.LOCKED, learned_name="Frieren")
    refused = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": 0})
    assert refused.status_code == 409 and refused.json()["code"] == "feed_locked"
    assert (web.load(Show, show).feed_id, web.load(Show, show).feed_state) == (feed, FeedState.LOCKED)
    assert (await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": feed})).status_code == 200
    released = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": 0, "release_lock": True})
    assert released.status_code == 200
    loaded = web.load(Show, show)
    assert (loaded.feed_id, loaded.feed_state, loaded.learned_name) == (None, FeedState.NONE, None)


async def test_a_show_waiting_on_a_candidate_feed_says_so(web):
    feed = web.feed("Low", "https://low/rss", 2)
    show = web.show(feed_id=feed, feed_state=FeedState.CANDIDATE, candidate_since=START)
    card = (await web.client.get(f"/api/shows/{show}")).json()
    assert card["feed"]["name"] == "Low" and card["feed"]["state"] == "candidate"


async def test_the_save_path_is_blank_without_a_base_directory_and_resolved_once_it_is_set(web):
    show = web.show()

    def set_base(value):
        web.query(lambda s: (setattr(s.get(Settings, 1), "base_dir", value), s.commit()))

    set_base("")
    assert (await web.client.get(f"/api/shows/{show}")).json()["save_path"] == ""
    set_base("/media/{name}")
    assert (await web.client.get(f"/api/shows/{show}")).json()["save_path"] == str(Path("/media/Sousou no Frieren").resolve())


async def test_a_show_is_kept_when_qbit_cannot_remove_its_torrents(web):
    show = web.show(episodes=1)

    def track(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show)).one()
        row = Download(episode_id=episode.id, title="[Grp] Sousou no Frieren - 01.mkv", state=DownloadState.DONE, info_hash="ab" * 20)
        session.add(row)
        session.flush()
        episode.current_download_id, episode.status = row.id, EpisodeStatus.COMPLETED
        session.commit()

    web.query(track)

    def refuse(*args, **kwargs):
        raise QbitError("cannot delete")

    web.qbit.delete = refuse
    response = await web.client.delete(f"/api/shows/{show}", params={"delete_torrents": "true"})
    assert response.status_code == 502 and "cannot delete" in response.json()["detail"]
    assert web.load(Show, show) is not None
    assert web.query(lambda s: len(s.exec(select(Episode).where(Episode.show_id == show)).all())) == 1


async def test_resetting_a_paused_show_keeps_it_paused(web):
    feed = web.feed()
    show = web.show(feed_id=feed, feed_state=FeedState.PINNED, paused=True, paused_by=PausedBy.USER)
    assert (await web.client.post(f"/api/shows/{show}/rediscover")).status_code == 200
    loaded = web.load(Show, show)
    assert loaded.paused and loaded.feed_state == FeedState.NONE


async def test_editing_with_an_unknown_feed_changes_nothing(web):
    show = web.show()
    response = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": 999, "category": "Movies"})
    assert response.status_code == 400
    assert web.load(Show, show).category is None and web.load(Show, show).feed_state == FeedState.NONE


async def test_picking_the_same_feed_again_or_auto_discover_cancels_nothing(web):
    feed = web.feed()
    show = web.show(episodes=1, feed_id=feed, feed_state=FeedState.LOCKED)

    def downloading(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show)).one()
        row = Download(episode_id=episode.id, feed_id=feed, title="[Grp] Sousou no Frieren - 01.mkv", state=DownloadState.DOWNLOADING, info_hash="ab" * 20)
        session.add(row)
        session.flush()
        episode.current_download_id, episode.status = row.id, EpisodeStatus.DOWNLOADING
        session.commit()
        return row.id

    row = web.query(downloading)
    same = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": feed})
    auto = await web.client.post(f"/api/shows/{show}/edit", json={"feed_id": 0, "release_lock": True})
    assert same.json()["canceled"] == [] and auto.json()["canceled"] == []
    assert web.load(Download, row).state == DownloadState.DOWNLOADING and web.qbit.calls_named("delete") == []


async def test_searching_when_nothing_is_missing_asks_no_site(web):
    show = web.show(episodes=0)
    web.feed("Nyaa", "https://nyaa.si/?page=rss&c=1_2&f=0", 1)
    response = await web.client.post(f"/api/shows/{show}/search-releases")
    assert response.json() == {"found": 0} and web.feeds.searched == []


async def test_a_finished_show_reopens_when_the_search_finds_its_missed_episode(web):
    url = "https://nyaa.si/?page=rss&c=1_2&f=0"
    show = web.show(completed=True)
    web.feed("Nyaa", url, 1)
    web.feeds.searchable[url] = [article("[Grp] Sousou no Frieren - 02 (1080p).mkv", START - timedelta(days=7))]
    response = await web.client.post(f"/api/shows/{show}/search-releases")
    assert response.json() == {"found": 1}
    assert web.load(Show, show).completed is False and "grab" in web.pokes


RELEASE = "[Grp] Sousou no Frieren - 01 (1080p).mkv"


def forgotten_episode(web, show, feed):
    """Episode 1 is completed but Kisetsu holds no torrent for it any more."""

    def forget(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show)).one()
        row = Download(episode_id=episode.id, feed_id=feed, title=RELEASE, state=DownloadState.DONE, info_hash=None)
        session.add(row)
        session.flush()
        episode.status, episode.current_download_id = EpisodeStatus.COMPLETED, row.id
        session.commit()
        return episode.id

    return web.query(forget)


async def test_restore_adds_the_release_again_from_the_stored_items_after_it_left_the_rss(web):
    feed = web.feed()
    show = web.show(episodes=1, feed_id=feed, feed_state=FeedState.LOCKED, learned_name="Sousou no Frieren")
    episode_id = forgotten_episode(web, show, feed)
    stored = article(RELEASE, START - timedelta(days=14) + timedelta(hours=1))
    await web.runtime.engine.run(lambda ctx: ingest.ingest(ctx, ctx.session.get(Feed, feed), [stored]))
    assert web.feeds.items.get("https://top/rss", []) == []
    response = await web.client.post(f"/api/shows/{show}/episodes/{episode_id}/restore")
    assert response.status_code == 200 and len(web.qbit.calls_named("add_torrent")) == 1


async def test_restore_says_so_when_the_release_is_nowhere_to_be_found(web):
    feed = web.feed()
    show = web.show(episodes=1, feed_id=feed, feed_state=FeedState.LOCKED)
    episode_id = forgotten_episode(web, show, feed)
    response = await web.client.post(f"/api/shows/{show}/episodes/{episode_id}/restore")
    assert response.status_code == 400 and response.json()["detail"].startswith("Not in qBittorrent.")
    assert web.qbit.calls_named("add_torrent") == []


def _library(web, tmp_path, title="Sousou no Frieren"):
    """A download folder holding the show's folder with one tracked torrent's file and a stray subtitle."""
    base = tmp_path / "Anime"
    folder = base / title
    folder.mkdir(parents=True)
    (folder / "[Grp] Sousou no Frieren - 01.mkv").write_bytes(b"x")
    (folder / "extra.ass").write_text("subs")
    web.query(lambda s: (setattr(s.get(Settings, 1), "base_dir", str(base)), setattr(s.get(Settings, 1), "title_language", "romaji"), s.commit()))
    show = web.show(1, title, episodes=1)
    torrent = web.qbit.seed("[Grp] Sousou no Frieren - 01.mkv", save_path=str(folder))

    def track(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show)).one()
        row = Download(episode_id=episode.id, title=torrent.name, state=DownloadState.DONE, info_hash=torrent.hash)
        session.add(row)
        session.flush()
        episode.current_download_id, episode.status = row.id, EpisodeStatus.COMPLETED
        session.commit()

    web.query(track)
    return show, folder, torrent


async def test_the_delete_preview_lists_the_folder_and_the_files_no_torrent_owns(web, tmp_path):
    show, folder, torrent = _library(web, tmp_path)
    preview = (await web.client.get(f"/api/shows/{show}/delete-preview")).json()
    assert [t["hash"] for t in preview["torrents"]] == [torrent.hash]
    assert preview["folder"]["remove"] and preview["folder"]["leftovers"] == ["extra.ass"]


async def test_deleting_a_show_removes_its_torrents_files_and_folder(web, tmp_path):
    show, folder, torrent = _library(web, tmp_path)
    response = await web.client.delete(f"/api/shows/{show}", params={"delete_torrents": "true", "delete_files": "true"})
    body = response.json()
    assert response.status_code == 200 and body["deleted_torrents"] == 1 and body["folder"]["removed"]
    assert web.qbit.deleted_with_files == [torrent.hash]
    assert not folder.exists() and folder.parent.exists()


async def test_keeping_the_files_keeps_the_folder(web, tmp_path):
    show, folder, torrent = _library(web, tmp_path)
    body = (await web.client.delete(f"/api/shows/{show}", params={"delete_torrents": "true", "delete_files": "false"})).json()
    assert body["deleted_torrents"] == 1 and body["folder"] is None
    assert web.qbit.deleted_with_files == [] and folder.exists()


async def test_a_folder_holding_a_torrent_of_no_show_is_kept(web, tmp_path):
    show, folder, torrent = _library(web, tmp_path)
    web.qbit.seed("[Grp] Sousou no Frieren S0 - 01.mkv", save_path=str(folder))
    body = (await web.client.delete(f"/api/shows/{show}", params={"delete_torrents": "true", "delete_files": "true"})).json()
    assert not body["folder"]["removed"] and "no show" in body["folder"]["reason"]
    assert folder.exists() and web.qbit.deleted_with_files == [torrent.hash]


async def test_a_folder_another_show_saves_to_is_kept(web, tmp_path):
    show, folder, _ = _library(web, tmp_path)
    web.show(2, "Other", episodes=0, save_folder=str(folder))
    body = (await web.client.delete(f"/api/shows/{show}", params={"delete_torrents": "true", "delete_files": "true"})).json()
    assert not body["folder"]["removed"] and "Another show" in body["folder"]["reason"]
    assert folder.exists()


def test_a_folder_outside_the_download_folder_is_never_removed(tmp_path):
    from kisetsu.services import library

    base, outside = tmp_path / "Anime", tmp_path / "Elsewhere"
    base.mkdir()
    outside.mkdir()
    assert library.remove_folder(str(outside), str(base), pause=lambda s: None) is not None
    assert library.remove_folder(str(base), str(base), pause=lambda s: None) is not None
    assert library.remove_folder(str(base), "", pause=lambda s: None) is not None
    assert outside.exists() and base.exists()
