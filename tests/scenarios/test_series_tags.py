from datetime import timedelta
from pathlib import Path

from kisetsu.db.schema import EpisodeStatus
from kisetsu.services import adoption, downloads, grab, ingest, library, shows, tags
from kisetsu.services.ingest import _new_item
from tests.fakes import START, article

TITLE = "[Grp] Sousou no Frieren - 03 (1080p).mkv"


def airing(world, **fields):
    world.ctx().settings.default_category = "Seasonal"
    return world.show(episodes=3, first_air=START - timedelta(days=14), **fields)


def adopt(world):
    result = adoption.adopt_existing(world.ctx())
    world.commit()
    return result


def test_a_new_download_carries_its_shows_tag_and_the_show_remembers_it(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1), title="The Apothecary Diaries Season 3")
    ingest.ingest(world.ctx(), feed, [article("[Grp] The Apothecary Diaries Season 3 - 01 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    added = world.qbit.calls_named("add_torrent")[0]
    assert "The Apothecary Diaries Season 3" in added["tags"] and "kisetsu-managed" in added["tags"]
    assert show.qbit_tag == "The Apothecary Diaries Season 3"


def test_commas_are_dropped_from_a_tag_and_shows_with_the_same_name_are_told_apart_by_id(world):
    one = world.show(anilist_id=11, title="Kaiju, No. 8")
    two = world.show(anilist_id=12, title="Same Name")
    three = world.show(anilist_id=13, title="Same Name")
    assert tags.wanted_tags(world.ctx()) == {one.id: "Kaiju No. 8", two.id: "Same Name #12", three.id: "Same Name #13"}


def test_sync_tags_tags_the_torrents_kisetsu_tracks_and_leaves_the_others(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.seed("Something else", save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    mine = next(t for t in world.qbit.truth.values() if t.name == TITLE)
    world.qbit.remove_tags([mine.hash], ["Sousou no Frieren"])
    world.qbit.sync()
    assert tags.sync_tags(world.ctx()) == 1
    world.commit()
    assert "Sousou no Frieren" in world.qbit.truth[mine.hash].tags
    assert [t.tags for t in world.qbit.truth.values() if t.name == "Something else"] == [frozenset()]
    assert show.qbit_tag == "Sousou no Frieren"
    world.qbit.sync()
    assert tags.sync_tags(world.ctx()) == 0


def test_a_renamed_show_moves_its_tag_and_the_old_one_is_deleted(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    show.title_english = "Frieren: Beyond Journey's End"
    world.session.add(show)
    world.commit()
    world.qbit.sync()
    tags.sync_tags(world.ctx())
    world.commit()
    torrent = next(iter(world.qbit.truth.values()))
    assert "Frieren: Beyond Journey's End" in torrent.tags and "Sousou no Frieren" not in torrent.tags
    assert "Sousou no Frieren" not in world.qbit.tags() and show.qbit_tag == "Frieren: Beyond Journey's End"


def test_a_torrent_tagged_for_another_show_is_never_adopted_by_name(world):
    world.show(anilist_id=1, title="Frieren Season 1", completed=True)
    second = airing(world, anilist_id=2, title="Frieren Season 2", aliases=["Sousou no Frieren"])
    world.qbit.seed(TITLE, save_path="/d", tags=["kisetsu-managed", "Frieren Season 1"])
    world.qbit.sync()
    assert adopt(world).imported == 0 and world.episode(second, 3).status == EpisodeStatus.WANTED


def test_a_torrent_tagged_for_a_show_is_adopted_into_it_whatever_it_is_called(world):
    airing(world, title="Frieren Season 2")
    world.qbit.seed("[Grp] Totally Different - 03 (1080p).mkv", save_path="/d", tags=["kisetsu-managed", "Frieren Season 2"])
    world.qbit.sync()
    assert adopt(world).imported == 1


def test_the_delete_preview_finds_a_tagged_torrent_the_database_lost_and_skips_another_shows(world):
    ctx = world.ctx()
    ctx.settings.default_category = "Seasonal"
    base = Path("/anime").resolve()
    ctx.settings.base_dir = base.as_posix()
    show = world.show(anilist_id=1, title="Sousou no Frieren")
    world.show(anilist_id=2, title="Other Show")
    folder = (base / "Sousou no Frieren").as_posix()
    world.qbit.seed("[Grp] Renamed By Hand - 01.mkv", save_path="/elsewhere", tags=["Sousou no Frieren"])
    world.qbit.seed("[Grp] Sousou no Frieren - 02.mkv", save_path=folder, category="Seasonal", tags=["Other Show"])
    world.qbit.seed("[Grp] Sousou no Frieren - 03.mkv", save_path=folder, category="Seasonal")
    preview = library.show_torrent_preview(world.ctx(), show)
    names = {e["name"] for e in preview}
    assert names == {"[Grp] Renamed By Hand - 01.mkv", "[Grp] Sousou no Frieren - 03.mkv"}
    assert next(e for e in preview if e["name"].startswith("[Grp] Renamed"))["tracked"] is True


def test_deleting_a_show_with_its_torrents_deletes_its_tag_too(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1))
    ingest.ingest(world.ctx(), feed, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    assert "Sousou no Frieren" in world.qbit.tags()
    shows.delete_show(world.ctx(), show, reason="user", delete_torrents=True)
    world.commit()
    assert "Sousou no Frieren" not in world.qbit.tags()


def test_a_replacement_and_the_release_it_replaces_both_carry_the_tag(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1))
    episode = world.episode(show, 1)
    v1 = downloads.start_download(world.ctx(), show, feed, episode, _new_item(feed, article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START), START))
    world.commit()
    world.qbit.finish(v1.info_hash)
    downloads.settle(world.ctx())
    v2 = downloads.start_download(world.ctx(), show, feed, episode, _new_item(feed, article("[Grp] Sousou no Frieren - 01v2 (1080p).mkv", START), START))
    world.commit()
    assert all("Sousou no Frieren" in world.qbit.truth[h].tags for h in (v1.info_hash, v2.info_hash))


def grabbed_show(world, title="Sousou no Frieren"):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1), title=title)
    ingest.ingest(world.ctx(), feed, [article(f"[Grp] {title} - 01 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    return show


def test_a_removed_show_keeps_its_torrents_and_tag_and_the_tag_goes_when_no_torrent_carries_it(world):
    show = grabbed_show(world)
    world.qbit.finish(next(iter(world.qbit.truth)))
    downloads.settle(world.ctx())
    world.commit()
    shows.delete_show(world.ctx(), show, reason="Show pruned at season change.")
    world.commit()
    assert len(world.qbit.truth) == 1 and tags.retired(world.ctx()) == ["Sousou no Frieren"]
    assert tags.sweep_retired(world.ctx()) == 0 and "Sousou no Frieren" in world.qbit.tags()
    world.qbit.truth.clear()
    assert tags.sweep_retired(world.ctx()) == 1
    world.commit()
    assert "Sousou no Frieren" not in world.qbit.tags() and tags.retired(world.ctx()) == []


def test_a_tag_a_followed_show_owns_again_is_not_deleted(world):
    show = grabbed_show(world)
    shows.delete_show(world.ctx(), show, reason="Show pruned at season change.")
    world.commit()
    world.show(anilist_id=2, title="Sousou no Frieren")
    world.qbit.truth.clear()
    assert tags.sweep_retired(world.ctx()) == 0
    world.commit()
    assert tags.retired(world.ctx()) == []


def test_a_torrent_with_a_retired_tag_is_not_adopted_by_name_into_another_show(world):
    show = grabbed_show(world, "Frieren Season 1")
    shows.delete_show(world.ctx(), show, reason="Show pruned at season change.")
    world.commit()
    second = airing(world, anilist_id=2, title="Frieren Season 2", aliases=["Sousou no Frieren"])
    world.qbit.seed(TITLE, save_path="/d", tags=["kisetsu-managed", "Frieren Season 1"])
    world.qbit.sync()
    assert adopt(world).imported == 0 and world.episode(second, 3).status == EpisodeStatus.WANTED


def test_a_tag_kisetsu_never_put_on_a_show_is_not_deleted_with_it(world):
    show = world.show(title="Sousou no Frieren")
    world.qbit.seed("Mine", save_path="/d", tags=["Sousou no Frieren"])
    world.qbit.sync()
    shows.delete_show(world.ctx(), show, reason="user")
    world.commit()
    assert tags.sweep_retired(world.ctx()) == 0 and tags.retired(world.ctx()) == []
    assert "Sousou no Frieren" in world.qbit.tags()
