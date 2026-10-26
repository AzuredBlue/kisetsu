from datetime import timedelta

import pytest

from kisetsu.db.schema import Download, DownloadState, EpisodeStatus, FeedState
from kisetsu.services import downloads, grab, ingest, manual
from tests.fakes import START, article

V1 = "[Grp] Sousou no Frieren - 03 (1080p).mkv"
V2 = "[Grp] Sousou no Frieren - 03v2 (1080p).mkv"


def airing(world, episodes=3, **fields):
    return world.show(episodes=episodes, first_air=START - timedelta(days=7 * (episodes - 1)), **fields)


def post(world, feed, title, published=None):
    ingest.ingest(world.ctx(), feed, [article(title, published if published is not None else world.clock())])
    world.commit()


def test_a_missed_episode_still_in_the_feed_can_be_grabbed_by_hand(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    episode = world.episode(show, 3)
    episode.status = EpisodeStatus.MISSED
    world.commit()
    post(world, feed, V1)
    message = manual.manual_grab(world.ctx(), show, V1)
    world.commit()
    assert message.startswith("Manually queued download") and world.episode(show, 3).status == EpisodeStatus.DOWNLOADING
    assert (show.feed_id, show.feed_state) == (feed.id, FeedState.LOCKED)


def test_a_release_that_is_gone_from_the_feed_cannot_be_grabbed(world):
    world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    with pytest.raises(ValueError, match="not in this show's feed"):
        manual.manual_grab(world.ctx(), show, V1)


def test_a_paused_show_refuses_a_manual_grab(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world, paused=True)
    post(world, feed, V1)
    with pytest.raises(ValueError, match="paused"):
        manual.manual_grab(world.ctx(), show, V1)


def test_an_episode_already_downloaded_at_that_version_is_refused_but_a_v2_replaces_it(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    post(world, feed, V1)
    post(world, feed, V2)
    manual.manual_grab(world.ctx(), show, V1)
    world.commit()
    with pytest.raises(ValueError, match="already has a download in progress"):
        manual.manual_grab(world.ctx(), show, V2)
    row = world.qbit.truth
    for torrent in list(row):
        world.qbit.finish(torrent)
    downloads.settle(world.ctx())
    world.commit()
    with pytest.raises(ValueError, match="already downloaded"):
        manual.manual_grab(world.ctx(), show, V1)
    assert manual.manual_grab(world.ctx(), show, V2).startswith("Manually queued replacement")


def test_a_locked_show_lists_only_its_feed_as_downloadable_and_others_for_information(world):
    top = world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world, feed=low, feed_state=FeedState.LOCKED, learned_name="Sousou no Frieren")
    post(world, top, V1)
    post(world, low, V1)
    groups = manual.feed_matches(world.ctx(), show)
    assert [(g["feed_name"], g["role"], g["usable"]) for g in groups] == [("Low", "locked", True), ("Top", "other", False)]
    assert groups[0]["matches"][0]["downloadable"] and not groups[1]["matches"][0]["downloadable"]
    with pytest.raises(ValueError):
        manual.manual_grab(world.ctx(), show, V1.replace("Grp", "Nope"))


def test_a_failing_feed_the_show_uses_is_listed_as_unavailable(world):
    feed = world.feed("Top", "https://top/rss", 1)
    feed.failing_since = START
    world.commit()
    show = airing(world, feed=feed, feed_state=FeedState.PINNED)
    groups = manual.feed_matches(world.ctx(), show)
    assert groups[0]["unavailable"] and groups[0]["role"] == "picked"


def test_restoring_adopts_the_torrent_qbit_still_holds(world):
    feed = world.feed("Top", "https://top/rss", 1)
    world.ctx().settings.default_category = "Seasonal"
    show = airing(world)
    post(world, feed, V1)
    grab.grab_pass(world.ctx())
    world.commit()
    for torrent in list(world.qbit.truth):
        world.qbit.finish(torrent)
    downloads.settle(world.ctx())
    world.commit()
    episode = world.episode(show, 3)
    dl = downloads.current(world.ctx(), episode)
    held_hash = dl.info_hash
    dl.info_hash = None
    world.commit()
    message = manual.restore_episode(world.ctx(), show, episode)
    world.commit()
    assert message.endswith("now managed.") and dl.info_hash == held_hash and dl.state == DownloadState.DONE
    with pytest.raises(ValueError, match="already managed"):
        manual.restore_episode(world.ctx(), show, episode)


def test_restoring_adds_the_release_again_when_qbit_no_longer_has_it(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    post(world, feed, V1)
    grab.grab_pass(world.ctx())
    world.commit()
    for torrent in list(world.qbit.truth):
        world.qbit.finish(torrent)
    downloads.settle(world.ctx())
    world.commit()
    episode = world.episode(show, 3)
    dl = downloads.current(world.ctx(), episode)
    world.qbit.remove(dl.info_hash)
    dl.info_hash = None
    world.commit()
    message = manual.restore_episode(world.ctx(), show, episode)
    world.commit()
    assert "restore" in message
    assert world.episode(show, 3).status == EpisodeStatus.COMPLETED
    fresh = downloads.active_download(world.ctx(), world.episode(show, 3))
    assert fresh is not None and fresh.supersedes_id == dl.id


def test_an_older_version_is_not_offered_over_a_finished_repack_whose_feed_is_gone(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    episode = world.episode(show, 3)
    repack = Download(
        episode_id=episode.id, feed_id=None, title="Sousou.no.Frieren.S01E03.REPACK.1080p.WEB-DL-Group.mkv",
        version=2, state=DownloadState.DONE, info_hash="ab" * 20,
    )
    world.session.add(repack)
    world.session.flush()
    episode.current_download_id, episode.status = repack.id, EpisodeStatus.COMPLETED
    world.commit()
    post(world, feed, V1)
    match = manual.feed_matches(world.ctx(), show)[0]["matches"][0]
    assert match["version"] == 1 and match["downloadable"] is False and match["replaces"] is False
