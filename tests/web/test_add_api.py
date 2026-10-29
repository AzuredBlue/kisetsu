from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import Download, Episode, EpisodeStatus, FeedState, Settings, Show
from tests.fakes import START, anilist_show, article

NYAA = "https://nyaa.si/?page=rss&c=1_2&f=0"


def _user(web, base_dir=None):
    def set_user(session):
        settings = session.get(Settings, 1)
        settings.anilist_username = "user"
        settings.title_language = "romaji"
        if base_dir is not None:
            settings.base_dir = str(base_dir)
        session.commit()

    web.query(set_user)


def _finished(web, anilist_id=7, title="Sousou no Frieren", episodes=3, list_status="CURRENT"):
    web.anilist.shows.append(anilist_show(anilist_id, title, status="FINISHED", list_status=list_status, total_episodes=episodes, season="FALL", season_year=2023))


def _releases(web, title="Sousou no Frieren", group="Grp", count=3):
    web.feeds.searchable[NYAA] = [
        article(f"[{group}] {title} - {n:02d} (1080p).mkv", START - timedelta(days=400 - 7 * n)) for n in range(1, count + 1)
    ]


async def test_the_list_offers_the_users_entries_and_fetches_once(web):
    _user(web)
    _finished(web)
    web.anilist.shows.append(anilist_show(8, "Planned Only", list_status="PLANNING"))
    web.show(9, "Followed")
    web.anilist.shows.append(anilist_show(9, "Followed", list_status="CURRENT"))
    body = (await web.client.get("/api/add/list")).json()
    assert {e["anilist_id"]: e["followed"] for e in body["entries"]} == {7: False, 9: True}
    await web.client.get("/api/add/list")
    await web.client.get("/api/add/list", params={"refresh": "true"})
    assert [name for name, _ in web.anilist.calls].count("fetch_user_list") == 1


async def test_an_entry_marked_completed_is_not_offered(web):
    _user(web)
    _finished(web, 7)
    _finished(web, 8, "Done Show", list_status="COMPLETED")
    body = (await web.client.get("/api/add/list")).json()
    assert [e["anilist_id"] for e in body["entries"]] == [7]
    assert (await web.client.post("/api/add/search", json={"anilist_id": 8})).status_code == 404


async def test_the_list_needs_an_anilist_user(web):
    assert (await web.client.get("/api/add/list")).status_code == 400


async def test_searching_shows_each_feed_episode_by_episode_and_saves_no_show(web):
    _user(web)
    _finished(web)
    _releases(web)
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    body = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    assert [g["feed_id"] for g in body["feeds"]] == [feed]
    assert [e["number"] for e in body["feeds"][0]["episodes"]] == [1, 2, 3]
    assert body["preselect"] is None and body["folder"] is None
    assert web.query(lambda s: s.exec(select(Show)).all()) == []


async def test_confirming_follows_the_feed_grabs_the_ticked_episodes_and_skips_the_rest(web):
    _user(web)
    _finished(web)
    _releases(web)
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    items = {e["number"]: e["item_id"] for e in preview["feeds"][0]["episodes"]}
    body = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": [items[1], items[2]]})).json()
    assert body["started"] == 2 and body["skipped"] == 1 and body["errors"] == []
    show = web.load(Show, body["show_id"])
    assert show.feed_id == feed and show.feed_state == FeedState.LOCKED and show.anilist_status == "FINISHED"
    statuses = {n: web.episode(show.id, n).status for n in (1, 2, 3)}
    assert statuses == {1: EpisodeStatus.DOWNLOADING, 2: EpisodeStatus.DOWNLOADING, 3: EpisodeStatus.SKIPPED}
    assert len(web.qbit.calls_named("add_torrent")) == 2
    again = await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": []})
    assert again.status_code == 400


async def test_files_already_on_disk_preselect_their_feed_and_use_their_folder(web, tmp_path):
    base = tmp_path / "Anime"
    folder = base / "Frieren"
    folder.mkdir(parents=True)
    (folder / "[Grp] Sousou no Frieren - 01 (1080p).mkv").write_bytes(b"x")
    _user(web, base)
    web.anilist.shows.append(anilist_show(7, "Sousou no Frieren", aliases=["Frieren"], status="FINISHED", list_status="CURRENT", total_episodes=3, season="FALL", season_year=2023))
    _releases(web)
    web.feed("Other", "https://other/rss", 1)
    nyaa = web.feed("Nyaa", NYAA, 2)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    assert preview["preselect"] == nyaa and preview["folder"]["files"] == 1 and preview["note"] is None
    assert [e["on_disk"] for e in preview["feeds"][0]["episodes"]] == [True, False, False]
    items = [e["item_id"] for e in preview["feeds"][0]["episodes"]]
    body = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": nyaa, "item_ids": items})).json()
    show = web.load(Show, body["show_id"])
    assert show.save_folder == "Frieren" and show.feed_state == FeedState.LOCKED
    assert {call["save_path"] for call in web.qbit.calls_named("add_torrent")} == {str(folder.resolve())}


async def test_files_no_feed_accounts_for_are_reported_and_nothing_is_preselected(web, tmp_path):
    base = tmp_path / "Anime"
    folder = base / "Sousou no Frieren"
    folder.mkdir(parents=True)
    (folder / "[Else] Sousou no Frieren - 01.mkv").write_bytes(b"x")
    _user(web, base)
    _finished(web)
    _releases(web)
    web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    assert preview["preselect"] is None and "no feed matches" in preview["note"]


async def test_a_skipped_episode_can_be_wanted_again(web):
    show = web.show(episodes=1, completed=True)

    def skip(session):
        episode = session.exec(select(Episode).where(Episode.show_id == show)).one()
        episode.status = EpisodeStatus.SKIPPED
        session.commit()
        return episode.id

    episode = web.query(skip)
    response = await web.client.post(f"/api/shows/{show}/episodes/{episode}/want")
    assert response.status_code == 200
    assert web.episode(show, 1).status == EpisodeStatus.WANTED and not web.load(Show, show).completed
    assert (await web.client.post(f"/api/shows/{show}/episodes/{episode}/want")).status_code == 400
    assert web.query(lambda s: s.exec(select(Download)).all()) == []


async def test_a_batch_is_offered_and_its_episodes_share_one_torrent_without_its_own_folder(web, tmp_path):
    _user(web, tmp_path / "Anime")
    _finished(web)
    _releases(web)
    batch = article("[Grp] Sousou no Frieren (01-03) [1080p] [Batch]", START - timedelta(days=300))
    web.feeds.searchable[NYAA].append(batch)
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    batches = preview["feeds"][0]["batches"]
    assert [(b["title"], b["numbers"]) for b in batches] == [(batch.title, [1, 2, 3])]
    body = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": [batches[0]["item_id"]]})).json()
    assert body["started"] == 3 and body["skipped"] == 0
    hashes = web.query(lambda s: {d.info_hash for d in s.exec(select(Download)).all()})
    assert hashes == {batch.info_hash} and list(web.qbit.truth) == [batch.info_hash]
    layouts = {call["content_layout"] for call in web.qbit.calls_named("add_torrent")}
    assert layouts == {"NoSubfolder"}


async def test_an_episode_from_a_batch_is_never_replaced_by_a_later_version(web, tmp_path):
    from kisetsu.services import grab, manual

    _user(web, tmp_path / "Anime")
    _finished(web)
    batch = article("[Grp] Sousou no Frieren (01-03) [1080p] [Batch]", START - timedelta(days=300))
    web.feeds.searchable[NYAA] = [batch]
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    show_id = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": [preview["feeds"][0]["batches"][0]["item_id"]]})).json()["show_id"]
    web.qbit.finish(batch.info_hash)
    v2 = "[Grp] Sousou no Frieren - 02v2 (1080p).mkv"
    web.feeds.post(NYAA, v2, START)
    await web.runtime.jobs.refresh_feeds()

    def regrab(ctx):
        from kisetsu.services import downloads
        downloads.settle(ctx)
        grab.grab_pass(ctx)
        show = ctx.session.get(Show, show_id)
        try:
            manual.manual_grab(ctx, show, v2)
        except ValueError as e:
            return str(e)

    refusal = await web.runtime.engine.run(regrab)
    assert "came in a batch" in refusal
    assert len(web.qbit.truth) == 1 and web.episode(show_id, 2).status == EpisodeStatus.COMPLETED


async def test_single_episodes_after_a_batch_are_still_grabbed_from_the_feed(web, tmp_path):
    from kisetsu.services import grab

    _user(web, tmp_path / "Anime")
    _finished(web)
    batch = article("[Grp] Sousou no Frieren S01 (01-02) [1080p] [Batch]", START - timedelta(days=300))
    web.feeds.searchable[NYAA] = [batch]
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    body = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": [preview["feeds"][0]["batches"][0]["item_id"]]})).json()
    show_id = body["show_id"]
    assert web.load(Show, show_id).learned_name is None
    web.qbit.finish(batch.info_hash)
    web.feeds.post(NYAA, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START)
    await web.runtime.jobs.refresh_feeds()
    await web.runtime.engine.run(lambda ctx: grab.grab_pass(ctx))
    assert web.episode(show_id, 3).status == EpisodeStatus.DOWNLOADING


async def test_a_batch_is_added_once_even_before_qbittorrent_shows_it(web, tmp_path):
    from kisetsu.services import downloads

    _user(web, tmp_path / "Anime")
    _finished(web)
    batch = article("[Grp] Sousou no Frieren (01-03) [1080p] [Batch]", START - timedelta(days=300))
    web.feeds.searchable[NYAA] = [batch]
    feed = web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    web.qbit.hide_added = True
    body = (await web.client.post("/api/add/confirm", json={"anilist_id": 7, "feed_id": feed, "item_ids": [preview["feeds"][0]["batches"][0]["item_id"]]})).json()
    assert body["started"] == 3 and len(web.qbit.calls_named("add_torrent")) == 1
    web.qbit.reveal()
    await web.runtime.engine.run(lambda ctx: downloads.settle(ctx))
    assert [web.episode(body["show_id"], n).status for n in (1, 2, 3)] == [EpisodeStatus.DOWNLOADING] * 3
    assert len(web.qbit.calls_named("add_torrent")) == 1


async def test_a_batch_longer_than_the_season_is_another_seasons_and_not_offered(web, tmp_path):
    _user(web, tmp_path / "Anime")
    _finished(web, episodes=12)
    web.feeds.searchable[NYAA] = [article("[Grp] Sousou no Frieren (01-28) [1080p] [Batch]", START - timedelta(days=300))]
    web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    assert preview["feeds"] == []


async def test_a_sequel_is_never_offered_a_batch_of_another_season(web, tmp_path):
    _user(web, tmp_path / "Anime")
    web.anilist.shows.append(anilist_show(7, "Sousou no Frieren 2nd Season", aliases=["Sousou no Frieren 2nd Season"], status="FINISHED", list_status="CURRENT", total_episodes=3, season="FALL", season_year=2023))
    web.feeds.searchable[NYAA] = [
        article("[Grp] Sousou no Frieren S01 [1080p] [Batch]", START - timedelta(days=300)),
        article("[Grp] Sousou no Frieren [1080p] [Batch]", START - timedelta(days=300)),
        article("[Grp] Sousou no Frieren S2 [1080p] [Batch]", START - timedelta(days=30)),
    ]
    web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 7})).json()
    assert [b["title"] for b in preview["feeds"][0]["batches"]] == ["[Grp] Sousou no Frieren S2 [1080p] [Batch]"]


async def test_a_release_from_long_before_the_shows_season_is_not_offered(web):
    from datetime import datetime, timezone

    _user(web)
    web.anilist.shows.append(anilist_show(
        9, "THE GHOST IN THE SHELL", aliases=["The Ghost in the Shell", "Ghost in the Shell", "Koukaku Kidoutai"],
        status="FINISHED", list_status="CURRENT", total_episodes=10, season="SUMMER", season_year=2026,
    ))
    web.feeds.searchable[NYAA] = [
        article("[Erai-raws] Koukaku Kidoutai (2026) - 01 [1080p]", datetime(2026, 7, 10, tzinfo=timezone.utc)),
        article("[Commie] Ghost in the Shell SAC_2045 2", datetime(2022, 6, 13, tzinfo=timezone.utc)),
    ]
    web.feed("Nyaa", NYAA, 1)
    await web.client.get("/api/add/list")
    preview = (await web.client.post("/api/add/search", json={"anilist_id": 9})).json()
    assert [e["title"] for e in preview["feeds"][0]["episodes"]] == ["[Erai-raws] Koukaku Kidoutai (2026) - 01 [1080p]"]
