from datetime import timedelta

from sqlmodel import select

from kisetsu.db.schema import Download, DownloadState, EndReason, EpisodeStatus, Event, EventKind
from kisetsu.services import downloads, shows, watched
from kisetsu.services.ingest import _new_item
from tests.fakes import START, article, hash_for


def item(world, feed, title):
    row = _new_item(feed, article(title, START), world.clock())
    world.session.add(row)
    world.session.flush()
    return row


def setup(world, episodes=3, progress=0):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=episodes, first_air=START - timedelta(days=21))
    ctx = world.ctx()
    ctx.settings.delete_watched = True
    ctx.settings.base_dir = "/anime"
    show.watched = show.watched_applied = progress
    world.commit()
    return feed, show


def grab(world, show, feed, number, title=None):
    row = item(world, feed, title or f"[Grp] Frieren - {number:02d} (1080p).mkv")
    dl = downloads.start_download(world.ctx(), show, feed, world.episode(show, number), row)
    world.commit()
    return dl, row


def finished(world, show, feed, number):
    dl, row = grab(world, show, feed, number)
    world.qbit.finish(row.torrent_url)
    settle(world)
    assert dl.state == DownloadState.DONE
    return dl, row


def watch(world, show, progress):
    assert watched.note_progress(world.ctx(), show, progress)
    world.commit()


def settle(world, **advance):
    world.clock.advance(**advance)
    ctx = world.ctx()
    downloads.settle(ctx)
    changed = watched.apply(ctx)
    world.commit()
    return changed


def test_the_first_progress_seen_skips_the_watched_episodes_that_hold_nothing(world):
    feed, show = setup(world)
    show.watched = show.watched_applied = None
    world.commit()
    finished(world, show, feed, 1)
    watch(world, show, 2)
    assert (show.watched, show.watched_applied) == (2, 2)
    assert world.statuses(show) == {1: EpisodeStatus.COMPLETED, 2: EpisodeStatus.SKIPPED, 3: EpisodeStatus.WANTED}
    # Where the user stood when first seen is not a watch: the downloaded episode stays.
    settle(world)
    assert world.qbit.deleted_with_files == []


def test_a_finished_episode_is_deleted_with_its_files_once_qbittorrent_stops_seeding_it(world):
    feed, show = setup(world)
    dl, row = finished(world, show, feed, 1)
    watch(world, show, 1)
    settle(world)
    assert row.info_hash in world.qbit.truth and dl.end_reason == EndReason.WATCHED
    world.qbit.pause([row.info_hash])
    settle(world)
    assert world.qbit.deleted_with_files == [row.info_hash]
    world.session.refresh(dl)
    assert (dl.state, dl.info_hash, dl.end_reason) == (DownloadState.DONE, None, EndReason.WATCHED)
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED
    kinds = world.session.exec(select(Event.kind).where(Event.episode == 1)).all()
    assert EventKind.DELETED in kinds


def test_a_torrent_that_never_reaches_its_ratio_is_deleted_a_week_after_it_finished(world):
    feed, show = setup(world)
    _, row = finished(world, show, feed, 1)
    watch(world, show, 1)
    settle(world, days=6)
    assert world.qbit.deleted_with_files == []
    settle(world, days=1, minutes=1)
    assert world.qbit.deleted_with_files == [row.info_hash]


def test_a_watched_episode_still_downloading_is_deleted_at_once_and_skipped(world):
    feed, show = setup(world)
    dl, row = grab(world, show, feed, 1)
    watch(world, show, 1)
    settle(world)
    assert world.qbit.deleted_with_files == [row.info_hash]
    world.session.refresh(dl)
    assert dl.state == DownloadState.CANCELED and dl.info_hash is None
    assert world.episode(show, 1).status == EpisodeStatus.SKIPPED


def test_a_batch_waits_for_its_last_episode_to_be_watched(world):
    feed, show = setup(world, episodes=2)
    row = item(world, feed, "[Grp] Frieren (01-02) [Batch] (1080p)")
    ctx = world.ctx()
    info_hash = hash_for(row.torrent_url)
    downloads.start_download(ctx, show, feed, world.episode(show, 1), row, batch=True)
    downloads.follow_download(ctx, show, feed, world.episode(show, 2), row, info_hash)
    world.commit()
    world.qbit.finish(info_hash)
    world.qbit.pause([info_hash])
    settle(world)
    watch(world, show, 1)
    settle(world)
    assert world.qbit.deleted_with_files == []
    watch(world, show, 2)
    settle(world)
    assert world.qbit.deleted_with_files == [info_hash]
    assert world.statuses(show) == {1: EpisodeStatus.COMPLETED, 2: EpisodeStatus.COMPLETED}


def test_a_torrent_sharing_a_file_with_one_that_stays_is_deleted_without_its_files(world):
    feed, show = setup(world)
    _, row = finished(world, show, feed, 1)
    going = world.qbit.truth[row.info_hash]
    other = world.qbit.seed("other", save_path=going.save_path, url="https://elsewhere/other.torrent")
    # The kept torrent holds the folder the watched one's file sits in.
    world.qbit._update(other.hash, content_path=going.save_path)
    world.qbit.file_names[other.hash] = [going.name]
    world.qbit.pause([row.info_hash])
    watch(world, show, 1)
    settle(world)
    assert row.info_hash not in world.qbit.truth and other.hash in world.qbit.truth
    assert world.qbit.deleted_with_files == []


def test_turning_the_setting_off_keeps_what_waited_to_be_deleted(world):
    feed, show = setup(world)
    dl, row = finished(world, show, feed, 1)
    watch(world, show, 1)
    settle(world)
    assert dl.end_reason == EndReason.WATCHED
    ctx = world.ctx()
    ctx.settings.delete_watched = False
    watched.switched(ctx)
    world.commit()
    world.qbit.pause([row.info_hash])
    settle(world)
    world.session.refresh(dl)
    assert dl.end_reason is None and row.info_hash in world.qbit.truth


def test_turning_it_on_with_the_backlog_deletes_what_was_already_watched(world):
    feed, show = setup(world)
    ctx = world.ctx()
    ctx.settings.delete_watched = False
    world.commit()
    _, row = finished(world, show, feed, 1)
    watch(world, show, 1)
    world.qbit.pause([row.info_hash])
    settle(world)
    assert world.qbit.deleted_with_files == []
    ctx = world.ctx()
    ctx.settings.delete_watched = True
    watched.switched(ctx, backlog=True)
    world.commit()
    settle(world)
    assert world.qbit.deleted_with_files == [row.info_hash]


def test_the_backlog_lists_the_downloaded_episodes_already_watched_with_their_size(world):
    feed, show = setup(world)
    finished(world, show, feed, 1)
    finished(world, show, feed, 2)
    show.watched = 1
    world.commit()
    preview = watched.backlog(world.ctx())
    assert preview["episodes"] == 1 and preview["size"] == 1_000_000
    assert [(s["name"], s["numbers"]) for s in preview["shows"]] == [("Sousou no Frieren", [1])]


def test_a_lower_progress_lets_the_episodes_be_acted_on_again(world):
    feed, show = setup(world)
    watch(world, show, 2)
    settle(world)
    assert show.watched_applied == 2
    watch(world, show, 1)
    assert (show.watched, show.watched_applied) == (1, 1)


def test_an_add_qbittorrent_has_not_shown_yet_holds_the_pass(world):
    feed, show = setup(world)
    world.qbit.hide_added = True
    dl, row = grab(world, show, feed, 1)
    assert dl.state == DownloadState.ADDING
    watch(world, show, 1)
    settle(world)
    assert show.watched_applied == 0 and world.qbit.deleted_with_files == []
    world.qbit.reveal()
    settle(world)
    assert show.watched_applied == 1
    assert world.qbit.deleted_with_files == [hash_for(row.torrent_url)]


def test_a_deleted_episode_can_be_downloaded_again_and_is_then_kept(world):
    feed, show = setup(world)
    _, row = finished(world, show, feed, 1)
    watch(world, show, 1)
    world.qbit.pause([row.info_hash])
    settle(world)
    episode = world.episode(show, 1)
    shows.want_episode(world.ctx(), show, episode)
    world.commit()
    episode = world.episode(show, 1)
    assert episode.status == EpisodeStatus.WANTED and episode.current_download_id is None
    again = world.session.exec(select(Download).where(Download.episode_id == episode.id)).all()
    assert all(d.info_hash is None for d in again)
