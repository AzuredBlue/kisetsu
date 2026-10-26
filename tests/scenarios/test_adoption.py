from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import Download, DownloadState, EpisodeOffset, EpisodeStatus, FeedState, OffsetSource
from kisetsu.services import adoption, ingest, library
from tests.fakes import START, article

TITLE = "[Grp] Sousou no Frieren - 03 (1080p).mkv"


def airing(world, episodes=3, **fields):
    world.ctx().settings.default_category = "Seasonal"
    return world.show(episodes=episodes, first_air=START - timedelta(days=7 * (episodes - 1)), **fields)


def post(world, feed, title, published=None):
    ingest.ingest(world.ctx(), feed, [article(title, published if published is not None else world.clock())])
    world.commit()


def adopt(world):
    result = adoption.adopt_existing(world.ctx())
    world.commit()
    return result


def test_a_finished_torrent_in_the_category_fills_the_episode_and_locks_the_one_feed_that_carries_it(world):
    feed = world.feed("Top", "https://top/rss", 1)
    world.feed("Low", "https://low/rss", 2)
    show = airing(world)
    post(world, feed, TITLE)
    world.qbit.seed(TITLE, save_path="/d/Frieren", category="Seasonal")
    world.qbit.sync()
    assert adopt(world).imported == 1
    episode = world.episode(show, 3)
    assert episode.status == EpisodeStatus.COMPLETED
    dl = world.session.get(Download, episode.current_download_id)
    assert dl.adopted and dl.state == DownloadState.DONE and dl.feed_id == feed.id
    assert (show.feed_id, show.feed_state) == (feed.id, FeedState.LOCKED)


def test_a_release_carried_by_two_feeds_proves_neither(world):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    show = airing(world)
    post(world, one, TITLE)
    post(world, two, TITLE)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    assert show.feed_state == FeedState.NONE and world.episode(show, 3).status == EpisodeStatus.COMPLETED


def test_an_unfinished_torrent_is_adopted_as_downloading(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal", finished=False)
    world.qbit.sync()
    adopt(world)
    assert world.episode(show, 3).status == EpisodeStatus.DOWNLOADING


def test_a_torrent_with_neither_the_tag_nor_the_category_is_not_ours(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="")
    world.qbit.sync()
    assert adopt(world).imported == 0 and world.episode(show, 3).status == EpisodeStatus.WANTED


def test_a_tagged_torrent_is_adopted_even_when_other_torrents_are_tagged(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Other", tags=["kisetsu-managed"])
    world.qbit.seed("Something else", save_path="/d", category="Other", tags=["mine"])
    world.qbit.sync()
    assert adopt(world).imported == 1 and world.episode(show, 3).status == EpisodeStatus.COMPLETED


def test_a_torrent_for_an_episode_that_has_not_aired_is_refused(world):
    world.ctx().settings.default_category = "Seasonal"
    show = world.show(episodes=4, first_air=START - timedelta(days=14))
    world.qbit.seed("[Grp] Sousou no Frieren - 04 (1080p).mkv", save_path="/d", category="Seasonal")
    world.qbit.sync()
    result = adopt(world)
    assert result.imported == 0 and result.refused == 1 and world.episode(show, 4).status == EpisodeStatus.WANTED


def test_a_torrent_added_before_the_episode_aired_is_from_an_earlier_season(world):
    show = airing(world)
    world.qbit.clock.advance(days=-200)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.clock.advance(days=200)
    world.qbit.sync()
    result = adopt(world)
    assert result.imported == 0 and result.refused == 1 and world.episode(show, 3).status == EpisodeStatus.WANTED


def test_a_tracked_release_is_never_overwritten_or_readopted(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    other = "[Other] Sousou no Frieren - 03 (1080p).mkv"
    world.qbit.seed(other, save_path="/d", category="Seasonal")
    world.qbit.sync()
    assert adopt(world).imported == 0
    assert world.episode(show, 3).status == EpisodeStatus.COMPLETED


def test_a_forgotten_release_can_be_adopted_again_when_it_comes_back(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    dl = world.session.exec(select(Download)).one()
    dl.info_hash = None
    world.commit()
    world.qbit.seed("[Grp] Sousou no Frieren - 03v2 (1080p).mkv", save_path="/d", category="Seasonal")
    world.qbit.sync()
    assert adopt(world).imported == 1
    assert world.episode(show, 3).current_download_id != dl.id


def test_adoption_never_rolls_back_a_superseded_release(world):
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    dl = world.session.exec(select(Download)).one()
    dl.state = DownloadState.SUPERSEDED
    show_episode = world.episode(show, 3)
    show_episode.current_download_id = None
    world.commit()
    assert adopt(world).imported == 0


def test_saved_files_that_one_feed_carries_lock_the_show(world, tmp_path):
    feed = world.feed("Top", "https://top/rss", 1)
    world.feed("Low", "https://low/rss", 2)
    folder = tmp_path / "Frieren"
    folder.mkdir()
    (folder / TITLE).write_text("x")
    show = airing(world, save_folder=str(folder))
    post(world, feed, TITLE)
    assert adoption.lock_from_saved_files(world.ctx()) == [show]
    assert (show.feed_id, show.feed_state, show.learned_name) == (feed.id, FeedState.LOCKED, "Sousou no Frieren")


def test_saved_files_no_feed_carries_hold_the_show_until_released(world, tmp_path):
    world.feed("Top", "https://top/rss", 1)
    folder = tmp_path / "Frieren"
    folder.mkdir()
    (folder / TITLE).write_text("x")
    show = airing(world, save_folder=str(folder))
    assert adoption.unmatched_saved_files(world.ctx()) == [show]
    assert library.hold(world.ctx(), show.id) and not library.hold(world.ctx(), show.id)
    assert library.held_ids(world.ctx()) == {show.id}
    world.clock.advance(hours=2)
    assert library.held_ids(world.ctx()) == set()


def test_a_torrent_adopted_before_its_feed_item_was_stored_gets_its_feed_afterwards(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    assert show.feed_state == FeedState.NONE
    dl = world.session.exec(select(Download)).one()
    assert dl.feed_id is None
    post(world, feed, TITLE)
    assert adoption.reattribute(world.ctx()) == 1
    world.commit()
    assert dl.feed_id == feed.id and (show.feed_id, show.feed_state) == (feed.id, FeedState.LOCKED)


def test_a_feed_that_counts_from_the_series_start_is_mapped_by_its_known_offset(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    title = "[Grp] Sousou no Frieren - 52 (1080p).mkv"
    post(world, feed, title)
    world.session.add(EpisodeOffset(show_id=show.id, feed_id=feed.id, offset=49, source=OffsetSource.CONFIRMED))
    world.commit()
    world.qbit.seed(title, save_path="/d", category="Seasonal")
    world.qbit.sync()
    assert adopt(world).imported == 1
    assert world.episode(show, 3).status == EpisodeStatus.COMPLETED


def test_a_release_in_two_feeds_belongs_to_the_one_the_show_is_pinned_to(world):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    show = airing(world, feed=two, feed_state=FeedState.PINNED)
    post(world, one, TITLE)
    post(world, two, TITLE)
    world.qbit.seed(TITLE, save_path="/d", category="Seasonal")
    world.qbit.sync()
    adopt(world)
    dl = world.session.get(Download, world.episode(show, 3).current_download_id)
    assert dl.feed_id == two.id and show.feed_state == FeedState.LOCKED


def saved_folder(tmp_path, *names):
    folder = tmp_path / "Frieren"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_text("x")
    return str(folder)


def test_saved_files_that_two_feeds_carry_lock_no_feed(world, tmp_path):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    show = airing(world, save_folder=saved_folder(tmp_path, TITLE))
    post(world, one, TITLE)
    post(world, two, TITLE)
    assert adoption.lock_from_saved_files(world.ctx()) == []
    assert show.feed_state == FeedState.NONE


def test_saved_files_leave_pinned_and_locked_shows_alone(world, tmp_path):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    folder = saved_folder(tmp_path, TITLE)
    pinned = airing(world, save_folder=folder, feed=one, feed_state=FeedState.PINNED)
    locked = airing(world, anilist_id=2, save_folder=folder, feed=one, feed_state=FeedState.LOCKED)
    post(world, two, TITLE)
    assert adoption.lock_from_saved_files(world.ctx()) == []
    assert [(s.feed_id, s.feed_state) for s in (pinned, locked)] == [(one.id, FeedState.PINNED), (one.id, FeedState.LOCKED)]


def test_saved_files_move_a_show_a_guess_had_put_on_another_feed(world, tmp_path):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    show = airing(world, save_folder=saved_folder(tmp_path, TITLE), feed=one, feed_state=FeedState.ASSIGNED)
    post(world, two, TITLE)
    assert adoption.lock_from_saved_files(world.ctx()) == [show]
    assert (show.feed_id, show.feed_state) == (two.id, FeedState.LOCKED)


def test_the_feed_carrying_the_newest_saved_file_wins_the_lock(world, tmp_path):
    one = world.feed("A", "https://a/rss", 1)
    two = world.feed("B", "https://b/rss", 2)
    older = "[Grp] Sousou no Frieren - 02 (1080p).mkv"
    show = airing(world, save_folder=saved_folder(tmp_path, older, TITLE))
    post(world, one, older)
    post(world, two, TITLE)
    assert adoption.lock_from_saved_files(world.ctx()) == [show]
    assert show.feed_id == two.id


def test_saved_files_that_a_feed_carries_neither_hold_the_show_nor_need_a_search(world, tmp_path):
    feed = world.feed("A", "https://a/rss", 1)
    show = airing(world, save_folder=saved_folder(tmp_path, TITLE))
    post(world, feed, TITLE)
    assert adoption.unmatched_saved_files(world.ctx()) == []
    assert library.held_ids(world.ctx()) == set() and show.feed_state == FeedState.NONE


def test_saved_release_names_lists_this_shows_video_files_one_folder_deep(world, tmp_path):
    folder = tmp_path / "Frieren"
    (folder / "Batch").mkdir(parents=True)
    (folder / "[Grp] Sousou no Frieren - 01 (1080p).mkv").write_text("x")
    (folder / "Batch" / "[Grp] Sousou no Frieren - 02 (1080p).mkv").write_text("x")
    (folder / "[Grp] Sousou no Frieren - 03 (1080p).srt").write_text("x")
    (folder / "[Grp] Another Show - 01 (1080p).mkv").write_text("x")
    show = airing(world, save_folder=str(folder))
    assert library.saved_release_names(show, world.ctx().settings) == [
        "[Grp] Sousou no Frieren - 01 (1080p).mkv",
        "[Grp] Sousou no Frieren - 02 (1080p).mkv",
    ]


def test_saved_release_names_is_empty_without_a_folder(world, tmp_path):
    show = airing(world, save_folder=str(tmp_path / "missing"))
    assert library.saved_release_names(show, world.ctx().settings) == []
    assert library.saved_release_names(airing(world, anilist_id=2), world.ctx().settings) == []
