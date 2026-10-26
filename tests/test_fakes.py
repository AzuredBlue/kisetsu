import asyncio
from datetime import timedelta

import pytest

from kisetsu.clients.qbittorrent import QbitDuplicate, QbitReadOnly, QbitUnavailable
from kisetsu.clients.rss import FeedFetchError
from tests.fakes import START, FakeAniList, FakeClock, FakeFeeds, FakeQbit, anilist_show, hash_for


def run(coro):
    return asyncio.run(coro)


def test_an_added_torrent_is_seen_only_after_a_sync():
    qbit = FakeQbit()
    qbit.add_torrent("magnet:?xt=urn:btih:A0FEE4B4B218CB7D6E6B50C7380F0476E9E9309D&dn=Show%20-%2001.mkv", save_path="/d", category="C", tags=["t1"])
    assert qbit.torrents == {}
    result = qbit.sync()
    assert result.full and result.changed == {"a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d"}
    t = qbit.torrents["a0fee4b4b218cb7d6e6b50c7380f0476e9e9309d"]
    assert (t.name, t.save_path, t.category, t.state) == ("Show - 01.mkv", "/d", "C", "downloading")
    assert t.tags == {"t1"} and qbit.find_by_tag("t1") is t


def test_sync_reports_only_what_changed_since_the_last_one():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent")
    qbit.sync()
    assert not qbit.sync()
    qbit.finish("https://x/1.torrent")
    result = qbit.sync()
    assert not result.full and result.changed == {hash_for("https://x/1.torrent")}
    assert qbit.torrents[hash_for("https://x/1.torrent")].finished


def test_a_torrent_removed_by_the_user_shows_up_as_removed():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent")
    qbit.sync()
    qbit.remove("https://x/1.torrent")
    assert qbit.sync().removed == {hash_for("https://x/1.torrent")}
    assert qbit.torrents == {}


def test_adding_the_same_torrent_twice_is_a_duplicate():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent")
    with pytest.raises(QbitDuplicate):
        qbit.add_torrent("https://x/1.torrent")


def test_a_hidden_add_appears_only_when_revealed():
    qbit = FakeQbit()
    qbit.hide_added = True
    qbit.add_torrent("https://x/1.torrent")
    qbit.sync()
    assert qbit.torrents == {}
    with pytest.raises(QbitDuplicate):
        qbit.add_torrent("https://x/1.torrent")
    qbit.reveal()
    qbit.sync()
    assert len(qbit.torrents) == 1


def test_pause_resume_and_finish_move_the_state():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent", paused=True)
    h = hash_for("https://x/1.torrent")
    assert qbit.truth[h].state == "stoppedDL"
    qbit.resume([h])
    assert qbit.truth[h].state == "downloading"
    qbit.finish(h)
    assert qbit.truth[h].finished and qbit.truth[h].state == "stalledUP"
    qbit.pause([h])
    assert qbit.truth[h].state == "stoppedUP"


def test_held_checks_stay_checking_until_finished():
    qbit = FakeQbit()
    qbit.hold_checks = True
    qbit.add_torrent("https://x/1.torrent", paused=True)
    h = hash_for("https://x/1.torrent")
    qbit.recheck([h])
    assert qbit.truth[h].checking
    qbit.finish_checks()
    assert not qbit.truth[h].checking


def test_delete_records_whether_files_went_too():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent")
    qbit.add_torrent("https://x/2.torrent")
    qbit.delete([hash_for("https://x/1.torrent")], delete_files=True)
    qbit.delete([hash_for("https://x/2.torrent")], delete_files=False)
    assert qbit.deleted_with_files == [hash_for("https://x/1.torrent")]
    assert qbit.truth == {}


def test_tags_can_be_added_removed_and_deleted():
    qbit = FakeQbit()
    qbit.add_torrent("https://x/1.torrent", tags=["a"])
    h = hash_for("https://x/1.torrent")
    qbit.add_tags([h], ["b"])
    assert qbit.truth[h].tags == {"a", "b"}
    qbit.remove_tags([h], ["a"])
    qbit.delete_tags(["b"])
    assert qbit.truth[h].tags == frozenset()


def test_an_unavailable_qbit_fails_every_call_and_records_it():
    qbit = FakeQbit()
    qbit.unavailable = QbitUnavailable("down")
    with pytest.raises(QbitUnavailable):
        qbit.sync()
    with pytest.raises(QbitUnavailable):
        qbit.add_torrent("https://x/1.torrent")
    assert [c[0] for c in qbit.calls] == ["sync", "add_torrent"]


def test_a_read_only_fake_refuses_writes_but_not_reads():
    qbit = FakeQbit(read_only=True)
    qbit.sync()
    with pytest.raises(QbitReadOnly):
        qbit.add_torrent("https://x/1.torrent")
    assert qbit.rss_feeds() == []


def test_a_seeded_torrent_is_already_there_for_the_first_sync():
    qbit = FakeQbit()
    qbit.seed("Show - 01.mkv", save_path="/d/Show", category="Seasonal", tags=["kisetsu-managed"])
    result = qbit.sync()
    assert result.full and len(result.changed) == 1
    assert next(iter(qbit.torrents.values())).finished


def test_the_clock_advances():
    clock = FakeClock()
    assert clock() == START
    clock.advance(hours=2)
    assert clock() == START + timedelta(hours=2)


def test_anilist_serves_shows_and_schedules_since_a_date():
    anilist = FakeAniList()
    anilist.shows.append(anilist_show(1, "Frieren", next_airing_episode=2, next_airing_at=START))
    anilist.set_schedule(1, START - timedelta(days=7), 3)
    assert run(anilist.fetch_user_seasonal_anime("user"))[0]["anilist_id"] == 1
    everything = run(anilist.fetch_media_airing_schedules([1, 2]))
    assert set(everything) == {1} and len(everything[1]) == 3
    upcoming = run(anilist.fetch_upcoming_airing_schedules([1, 2], START))
    assert [e["episode"] for e in upcoming[1]] == [2, 3] and upcoming[2] == []
    assert run(anilist.user_exists("user")) and not run(anilist.user_exists("nobody"))


def test_anilist_can_be_made_to_fail():
    anilist = FakeAniList()
    anilist.error = RuntimeError("down")
    with pytest.raises(RuntimeError):
        run(anilist.fetch_user_seasonal_anime("user"))


def test_feeds_serve_posted_items_newest_first_and_can_fail():
    feeds = FakeFeeds()
    feeds.post("https://f", "Show - 01")
    feeds.post("https://f", "Show - 02")
    assert [a.title for a in run(feeds.fetch("https://f"))] == ["Show - 02", "Show - 01"]
    feeds.fail("https://f")
    with pytest.raises(FeedFetchError):
        run(feeds.fetch("https://f"))
    feeds.heal("https://f")
    assert len(run(feeds.fetch("https://f"))) == 2
    assert feeds.fetched == ["https://f"] * 3


def test_feeds_search_only_the_sites_that_can_be_searched():
    feeds = FakeFeeds()
    feeds.searchable["https://f"] = [feeds.post("https://f", "Frieren - 01"), feeds.post("https://f", "Other - 01")]
    assert [a.title for a in run(feeds.search("https://f", "frieren"))] == ["Frieren - 01"]
    assert run(feeds.search("https://other", "frieren")) is None


def test_a_feed_item_carries_the_hash_its_torrent_url_resolves_to():
    from tests.fakes import article

    item = article("[Group] Show - 01 (1080p).mkv")
    assert len(item.info_hash) == 40
    assert hash_for(item.torrent_url) == item.info_hash
    qbit = FakeQbit()
    qbit.add_torrent(item.torrent_url)
    qbit.sync()
    assert qbit.torrents[item.info_hash].name == "[Group] Show - 01 (1080p).mkv"
    assert article("[Group] Show - 01 (1080p).mkv").info_hash == item.info_hash
    assert article("[Group] Show - 02 (1080p).mkv").info_hash != item.info_hash
