from datetime import timedelta

import pytest
from sqlmodel import select

from kisetsu.db.schema import Download, DownloadState, EpisodeStatus, EventKind, Event, FeedState
from kisetsu.services import downloads, ingest, manual
from tests.fakes import START, article

A = "[Grp] Sousou no Frieren - 03 (1080p).mkv"
B = "[Other] Sousou no Frieren - 03 (1080p).mkv"
B2 = "[Grp] Sousou no Frieren - 03v2 (1080p).mkv"


def airing(world, **fields):
    return world.show(episodes=3, first_air=START - timedelta(days=14), **fields)


def post(world, feed, title):
    ingest.ingest(world.ctx(), feed, [article(title, world.clock())])
    world.commit()


def settle(world, hours=0):
    world.clock.advance(hours=hours)
    result = downloads.settle(world.ctx())
    world.commit()
    return result


def begin(world, *titles):
    """A show whose episode 3 is downloading release ``titles[0]`` while the feed also carries the others."""
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    for title in titles:
        post(world, feed, title)
    manual.manual_grab(world.ctx(), show, titles[0])
    world.commit()
    dl = world.session.exec(select(Download)).one()
    return feed, show, dl


def torrent_of(world, dl):
    return world.qbit.truth[dl.info_hash]


def test_a_torrent_with_no_activity_for_six_hours_is_stalled(world):
    _, _, dl = begin(world, A)
    settle(world, hours=5)
    assert world.caches.stalled == set()
    settle(world, hours=2)
    assert world.caches.stalled == {dl.id}


def test_recent_activity_a_stopped_state_or_a_finish_is_not_stalled(world):
    _, _, dl = begin(world, A)
    world.clock.advance(hours=7)
    world.qbit._update(dl.info_hash, last_activity=world.clock() - timedelta(minutes=5))
    settle(world)
    assert world.caches.stalled == set()
    world.qbit._update(dl.info_hash, last_activity=None, state="stoppedDL")
    settle(world)
    assert world.caches.stalled == set()
    world.qbit._update(dl.info_hash, state="queuedDL")
    settle(world)
    assert world.caches.stalled == set()
    world.qbit._update(dl.info_hash, state="downloading")
    settle(world)
    assert world.caches.stalled == {dl.id}
    world.qbit.finish(dl.info_hash)
    settle(world)
    assert world.caches.stalled == set()


def test_a_torrent_that_never_had_activity_counts_from_when_it_was_added(world):
    _, _, dl = begin(world, A)
    world.qbit._update(dl.info_hash, last_activity=None, state="metaDL")
    settle(world, hours=7)
    assert world.caches.stalled == {dl.id}


def test_try_another_drops_the_stalled_torrent_and_grabs_the_other_release_keeping_the_lock(world):
    feed, show, dl = begin(world, A, B)
    old_hash = dl.info_hash
    settle(world, hours=7)
    episode = world.episode(show, 3)
    assert manual.alternatives(world.ctx(), show, episode, dl)[0][1].item.title == B
    message = manual.try_another(world.ctx(), show, episode)
    world.commit()
    assert B in message and old_hash not in world.qbit.truth
    assert old_hash in world.qbit.deleted_with_files
    world.session.expire_all()
    rows = {d.title: d for d in world.session.exec(select(Download)).all()}
    assert rows[A].state == DownloadState.CANCELED and rows[B].state == DownloadState.DOWNLOADING
    assert world.episode(show, 3).current_download_id == rows[B].id
    assert (show.feed_id, show.feed_state) == (feed.id, FeedState.LOCKED)
    assert world.caches.stalled == set()


def test_try_another_can_never_pick_the_dead_release_and_says_so_when_nothing_else_exists(world):
    _, show, dl = begin(world, A)
    settle(world, hours=7)
    episode = world.episode(show, 3)
    assert manual.alternatives(world.ctx(), show, episode, dl) == []
    with pytest.raises(ValueError, match="No other release"):
        manual.try_another(world.ctx(), show, episode)
    assert dl.info_hash in world.qbit.truth and world.session.get(Download, dl.id).state == DownloadState.DOWNLOADING


def test_only_a_stalled_download_may_skip_the_in_progress_check(world):
    _, show, dl = begin(world, A, B)
    episode = world.episode(show, 3)
    with pytest.raises(ValueError, match="no stalled download"):
        manual.try_another(world.ctx(), show, episode)
    with pytest.raises(ValueError, match="already has a download in progress"):
        manual.manual_grab(world.ctx(), show, B)
    settle(world, hours=7)
    assert manual.stalled_download(world.ctx(), episode).id == dl.id
    with pytest.raises(ValueError, match="already has a download in progress"):
        manual.manual_grab(world.ctx(), show, B)


def test_a_stalled_replacement_leaves_the_finished_release_and_is_swapped_for_another_version(world):
    feed, show, _ = begin(world, A)
    world.qbit.finish(A_hash(world))
    settle(world)
    post(world, feed, B2)
    post(world, feed, "[Grp] Sousou no Frieren - 03v3 (1080p).mkv")
    manual.manual_grab(world.ctx(), show, B2)
    world.commit()
    settle(world, hours=7)
    episode = world.episode(show, 3)
    stalled = manual.stalled_download(world.ctx(), episode)
    assert stalled is not None and stalled.supersedes_id is not None
    manual.try_another(world.ctx(), show, episode)
    world.commit()
    world.session.expire_all()
    rows = {d.title: d for d in world.session.exec(select(Download)).all()}
    assert rows[A].state == DownloadState.DONE and rows[B2].state == DownloadState.CANCELED
    assert rows["[Grp] Sousou no Frieren - 03v3 (1080p).mkv"].supersedes_id == rows[A].id


def A_hash(world):
    return next(iter(world.qbit.truth))


def test_a_batch_download_is_never_swapped(world):
    _, show, dl = begin(world, A, B)
    dl.batch = True
    world.commit()
    settle(world, hours=7)
    assert manual.alternatives(world.ctx(), show, world.episode(show, 3), dl) == []


def test_a_torrent_removed_twice_fails_instead_of_coming_back_again(world):
    feed, show, dl = begin(world, A)
    world.qbit.remove(dl.info_hash)
    settle(world)
    settle(world)
    assert world.session.get(Download, dl.id).state == DownloadState.CANCELED
    world.clock.advance(minutes=16)
    again = downloads.start_download(world.ctx(), show, feed, world.episode(show, 3), _stored(world, A))
    world.commit()
    assert again.version == dl.version
    world.qbit.remove(again.info_hash)
    settle(world)
    settle(world)
    world.session.expire_all()
    episode = world.episode(show, 3)
    assert world.session.get(Download, again.id).state == DownloadState.FAILED and world.session.get(Download, again.id).info_hash is None
    assert episode.status == EpisodeStatus.FAILED
    failures = [e for e in world.session.exec(select(Event)).all() if e.kind == EventKind.FAILED]
    assert len(failures) == 1 and "again" in failures[0].message
    settle(world, hours=1)
    assert world.session.get(Download, again.id).state == DownloadState.FAILED


def _stored(world, title):
    from kisetsu.db.schema import FeedItem

    return world.session.exec(select(FeedItem).where(FeedItem.title == title)).one()
