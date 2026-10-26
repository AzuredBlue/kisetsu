from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import Download, DownloadState, EpisodeOffset, EpisodeStatus, FeedState, OffsetSource
from kisetsu.services import grab, ingest, library
from tests.fakes import START, article


def post(world, feed, title, published=None, **kwargs):
    published = published if published is not None else world.clock()
    ingest.ingest(world.ctx(), feed, [article(title, published, **kwargs)])
    world.commit()


def run(world, **kwargs):
    result = grab.grab_pass(world.ctx(), **kwargs)
    world.commit()
    return result


def airing(world, episodes=3, **fields):
    """A show whose newest of ``episodes`` aired now, the others weekly before."""
    return world.show(episodes=episodes, first_air=START - timedelta(days=7 * (episodes - 1)), **fields)


def downloaded(world, show):
    world.session.expire_all()
    rows = world.session.exec(select(Download)).all()
    return sorted((world.session.get(grab.Episode, d.episode_id).number, d.version, d.state) for d in rows)


def test_the_top_feed_release_is_grabbed_at_once(world):
    top = world.feed("Top", "https://top/rss", 1)
    world.feed("Low", "https://low/rss", 2)
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 1
    assert world.episode(show, 3).status == EpisodeStatus.DOWNLOADING
    assert (show.feed_id, show.feed_state) == (top.id, FeedState.LOCKED)


def test_a_lower_feed_release_waits_for_the_grace_window_from_its_publish_time(world):
    world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world)
    post(world, low, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START)
    assert run(world).started == 0 and show.feed_state == FeedState.CANDIDATE
    world.clock.advance(minutes=6)
    assert run(world).started == 1 and show.feed_id == low.id


def test_a_higher_ranked_feed_carrying_it_first_wins_the_window(world):
    top = world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world)
    post(world, low, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START)
    run(world)
    world.clock.advance(minutes=2)
    post(world, top, "[Other] Sousou no Frieren - 03 (1080p).mkv", START)
    assert run(world).started == 1 and show.feed_id == top.id


def test_a_locked_show_reads_only_its_feed(world):
    top = world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world, feed=low, feed_state=FeedState.LOCKED, learned_name="Sousou no Frieren")
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 0
    post(world, low, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 1 and show.feed_id == low.id


def test_v1_and_v2_in_one_read_grab_only_the_newest(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    post(world, top, "[Grp] Sousou no Frieren - 03v2 (1080p).mkv")
    assert run(world).started == 1
    assert downloaded(world, show) == [(3, 2, DownloadState.DOWNLOADING)]


def test_a_failed_version_is_not_retried_and_does_not_hide_a_lower_one(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    post(world, top, "[Grp] Sousou no Frieren - 03v2 (1080p).mkv")
    episode = world.episode(show, 3)
    world.session.add(Download(episode_id=episode.id, title="x", version=2, state=DownloadState.FAILED))
    world.commit()
    assert run(world).started == 1
    assert downloaded(world, show) == [(3, 1, DownloadState.DOWNLOADING), (3, 2, DownloadState.FAILED)]


def test_a_release_without_a_number_is_the_only_episode_of_a_movie(world):
    top = world.feed("Top", "https://top/rss", 1)
    movie = world.show(episodes=1, first_air=START - timedelta(days=2), total_episodes=1, title="Kimi no Na wa")
    post(world, top, "[Grp] Kimi no Na wa (BD 1080p).mkv")
    assert run(world).started == 1 and world.episode(movie, 1).status == EpisodeStatus.DOWNLOADING


def test_a_release_published_before_its_episode_aired_is_an_earlier_season(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START - timedelta(days=200))
    assert run(world).started == 0 and world.episode(show, 3).status == EpisodeStatus.WANTED


def test_an_older_wanted_episode_is_missed_and_still_grabbed_when_it_turns_up(world):
    top = world.feed("Top", "https://top/rss", 1)
    top.last_ok_at = START
    world.commit()
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    result = run(world)
    assert result.started == 1 and result.missed == 2
    assert [world.episode(show, n).status for n in (1, 2)] == [EpisodeStatus.MISSED] * 2
    post(world, top, "[Grp] Sousou no Frieren - 02 (1080p).mkv", START - timedelta(days=7))
    assert run(world).started == 1 and world.episode(show, 2).status == EpisodeStatus.DOWNLOADING


def test_the_newest_episode_is_missed_only_after_a_day(world):
    top = world.feed("Top", "https://top/rss", 1)
    top.last_ok_at = START
    world.commit()
    show = airing(world, episodes=1)
    assert run(world).missed == 0
    world.clock.advance(hours=25)
    assert run(world).missed == 1 and world.episode(show, 1).status == EpisodeStatus.MISSED


def test_an_episode_a_stored_item_can_supply_is_never_missed(world):
    top = world.feed("Top", "https://top/rss", 1)
    top.last_ok_at = START
    world.commit()
    show = airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 01 (1080p).mkv", START - timedelta(days=14))
    show_episode = world.episode(show, 1)
    show_episode.retry_after = START + timedelta(hours=1)
    world.commit()
    run(world)
    assert world.episode(show, 1).status == EpisodeStatus.WANTED


def test_an_episode_waiting_for_a_retry_is_not_grabbed(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    episode = world.episode(show, 3)
    episode.retry_after = START + timedelta(hours=2)
    world.commit()
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 0
    world.clock.advance(hours=3)
    assert run(world).started == 1


def test_a_paused_or_held_show_is_not_grabbed_for(world):
    top = world.feed("Top", "https://top/rss", 1)
    airing(world, paused=True)
    held = airing(world, 3, anilist_id=2, title="Another Show")
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    post(world, top, "[Grp] Another Show - 03 (1080p).mkv")
    assert run(world, held={held.id}).started == 0
    assert run(world).started == 1 and world.episode(held, 3).status == EpisodeStatus.DOWNLOADING


def test_a_show_that_has_not_aired_its_premiere_is_not_grabbed_for(world):
    top = world.feed("Top", "https://top/rss", 1)
    world.show(episodes=1, first_air=START + timedelta(days=2), next_airing_episode=1,
               next_airing_at=START + timedelta(days=2), anilist_status="NOT_YET_RELEASED")
    post(world, top, "[Grp] Sousou no Frieren - 01 (1080p).mkv")
    assert run(world).started == 0


def test_a_release_numbered_by_the_feed_is_mapped_by_its_air_date_and_the_offset_learned(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world, episodes=3)
    post(world, top, "[Grp] Sousou no Frieren - 15 (1080p).mkv", START)
    assert run(world).started == 1
    assert world.episode(show, 3).status == EpisodeStatus.DOWNLOADING
    offset = world.session.exec(select(EpisodeOffset)).one()
    assert (offset.offset, offset.source) == (12, OffsetSource.INFERRED)
    post(world, top, "[Grp] Sousou no Frieren - 14 (1080p).mkv", START - timedelta(days=7))
    run(world)
    assert world.session.exec(select(EpisodeOffset)).one().source == OffsetSource.CONFIRMED
    assert world.episode(show, 2).status == EpisodeStatus.DOWNLOADING


def test_stored_releases_under_the_feeds_numbers_move_when_the_offset_is_learned(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world, episodes=3, feed=top, feed_state=FeedState.ASSIGNED)
    wrong = world.episode(show, 2)
    dl = Download(episode_id=wrong.id, feed_id=top.id, title="[Grp] Sousou no Frieren - 02", version=1, source_episode=2,
                  state=DownloadState.DONE, info_hash="aa" * 20)
    world.session.add(dl)
    world.session.flush()
    wrong.current_download_id = dl.id
    wrong.status = EpisodeStatus.COMPLETED
    world.commit()
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START - timedelta(days=7))
    assert run(world).started == 1
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED
    assert world.episode(show, 1).current_download_id == dl.id
    assert world.episode(show, 2).status == EpisodeStatus.DOWNLOADING


def test_an_undated_item_does_not_fill_an_old_episode(world):
    top = world.feed("Top", "https://top/rss", 1)
    airing(world, episodes=4)
    ingest.ingest(world.ctx(), top, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", None)])
    world.commit()
    assert run(world).started == 0


def test_an_episode_without_an_air_time_takes_only_the_newest_wanted(world):
    top = world.feed("Top", "https://top/rss", 1)
    world.show(episodes=3, total_episodes=3, next_airing_episode=4)
    post(world, top, "[Grp] Sousou no Frieren - 01 (1080p).mkv")
    assert run(world).started == 0


def test_a_feed_that_is_not_taken_does_not_move_stored_releases(world):
    top = world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world, episodes=3)
    wrong = world.episode(show, 2)
    dl = Download(episode_id=wrong.id, feed_id=None, title="[Grp] Sousou no Frieren - 02", version=1, source_episode=2,
                  state=DownloadState.DONE, info_hash="aa" * 20)
    world.session.add(dl)
    world.session.flush()
    wrong.current_download_id = dl.id
    wrong.status = EpisodeStatus.COMPLETED
    world.commit()
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv", START)
    post(world, low, "[Other] Sousou no Frieren - 03 (1080p).mkv", START - timedelta(days=7))
    run(world)
    world.session.expire_all()
    assert world.session.get(Download, dl.id).episode_id == wrong.id
    assert world.episode(show, 3).status == EpisodeStatus.DOWNLOADING


def test_a_feed_that_is_down_marks_nothing_missed(world):
    top = world.feed("Top", "https://top/rss", 1)
    top.last_ok_at, top.failing_since = START - timedelta(hours=1), START - timedelta(minutes=5)
    world.commit()
    show = airing(world)
    assert run(world).missed == 0 and world.episode(show, 1).status == EpisodeStatus.WANTED


def test_a_pinned_show_grabs_only_from_its_feed_even_when_a_higher_one_carries_the_release(world):
    top = world.feed("Top", "https://top/rss", 1)
    low = world.feed("Low", "https://low/rss", 2)
    show = airing(world, feed=low, feed_state=FeedState.PINNED)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 0
    post(world, low, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 1 and (show.feed_id, show.feed_state) == (low.id, FeedState.LOCKED)


def test_running_the_pass_again_over_the_same_release_adds_one_torrent(world):
    top = world.feed("Top", "https://top/rss", 1)
    airing(world)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 1
    assert run(world).started == 0
    assert len(world.qbit.calls_named("add_torrent")) == 1


def test_releases_of_one_episode_and_version_from_several_groups_download_once(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    for group in ("A", "B", "C"):
        post(world, top, f"[{group}] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 1
    assert len(world.qbit.calls_named("add_torrent")) == 1
    assert downloaded(world, show) == [(3, 1, DownloadState.DOWNLOADING)]


def test_with_two_lower_feeds_carrying_a_release_the_better_ranked_one_is_adopted(world):
    world.feed("Top", "https://top/rss", 1)
    second = world.feed("Second", "https://second/rss", 2)
    third = world.feed("Third", "https://third/rss", 3)
    show = airing(world)
    published = START - timedelta(hours=2)
    post(world, third, "[Third] Sousou no Frieren - 03 (1080p).mkv", published)
    post(world, second, "[Second] Sousou no Frieren - 03 (1080p).mkv", published)
    run(world)
    run(world)
    wanted = article("[Second] Sousou no Frieren - 03 (1080p).mkv", published).torrent_url
    assert [call["url"] for call in world.qbit.calls_named("add_torrent")] == [wanted]
    assert show.feed_id == second.id


def test_a_show_held_for_its_saved_files_is_not_given_a_guessed_feed(world):
    top = world.feed("Top", "https://top/rss", 1)
    show = airing(world)
    library.hold(world.ctx(), show.id)
    post(world, top, "[Grp] Sousou no Frieren - 03 (1080p).mkv")
    assert run(world).started == 0
    assert (show.feed_id, show.feed_state) == (None, FeedState.NONE)
