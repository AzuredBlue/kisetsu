from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from kisetsu.clients.qbittorrent import QbitError, QbitRejected, QbitUnavailable
from kisetsu.db.schema import Download, DownloadState, EpisodeStatus, EventKind, FeedState
from kisetsu.services import downloads
from kisetsu.services.ingest import _new_item
from tests.fakes import START, article

V1 = "[Grp] Frieren - 01 (1080p).mkv"
V2 = "[Grp] Frieren - 01v2 (1080p).mkv"


def item(world, feed, title, **article_kwargs):
    row = _new_item(feed, article(title, START, **article_kwargs), world.clock())
    world.session.add(row)
    world.session.flush()
    return row


def setup(world, **show_fields):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=2, first_air=START - timedelta(days=1), **show_fields)
    return feed, show, world.episode(show, 1)


def grab(world, show, feed, episode, title=V1, torrent_url=None, **kwargs):
    row = item(world, feed, title, torrent_url=torrent_url)
    dl = downloads.start_download(world.ctx(), show, feed, episode, row, **kwargs)
    world.commit()
    return dl, row


def settle(world, minutes=0):
    world.clock.advance(minutes=minutes)
    result = downloads.settle(world.ctx())
    world.commit()
    return result


def test_a_grab_adds_the_torrent_tagged_and_in_the_show_folder_then_locks_the_feed(world):
    feed, show, ep = setup(world)
    world.ctx().settings.base_dir = "/anime"
    dl, row = grab(world, show, feed, ep)
    added = world.qbit.calls_named("add_torrent")[0]
    assert added["url"] == row.torrent_url and added["paused"] is False
    assert "kisetsu-managed" in added["tags"] and added["save_path"].endswith("Sousou no Frieren")
    assert dl.state == DownloadState.DOWNLOADING and dl.info_hash == row.info_hash and dl.tag is None
    assert world.episode(show, 1).status == EpisodeStatus.DOWNLOADING
    assert (show.feed_id, show.feed_state, show.learned_name) == (feed.id, FeedState.LOCKED, "Frieren")


def test_a_finished_torrent_completes_the_episode_and_notifies_once(world):
    feed, show, ep = setup(world)
    world.settings = world.ctx().settings
    world.settings.desktop_notifications = True
    dl, row = grab(world, show, feed, ep)
    world.qbit.finish(row.torrent_url)
    settle(world)
    settle(world)
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED
    assert dl.state == DownloadState.DONE
    assert len(world.notifications) == 1


def test_a_v2_is_added_paused_rechecked_and_the_v1_stays_until_it_is_seeding(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    assert world.qbit.calls_named("add_torrent")[1]["paused"] is True
    assert world.qbit.calls_named("recheck") and world.qbit.calls_named("resume")
    assert v2.supersedes_id == v1.id and v2.state == DownloadState.VERIFYING
    settle(world)
    assert row1.info_hash in world.qbit.truth and world.episode(show, 1).status == EpisodeStatus.COMPLETED
    world.qbit.finish(row2.torrent_url)
    settle(world)
    world.session.expire_all()
    assert row1.info_hash not in world.qbit.truth and row1.info_hash in world.qbit.deleted_with_files
    assert (v1.state, v2.state) == (DownloadState.SUPERSEDED, DownloadState.DONE)
    assert world.episode(show, 1).current_download_id == v2.id


def test_a_rerelease_under_the_same_path_keeps_the_files(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    world.qbit.names[row2_url := "https://example.test/same.torrent"] = world.qbit.truth[row1.info_hash].name
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2, torrent_url=row2_url)
    world.qbit.finish(row2_url)
    settle(world)
    assert row1.info_hash not in world.qbit.truth and row1.info_hash not in world.qbit.deleted_with_files


def test_a_replacement_whose_torrent_vanishes_keeps_the_previous_release(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit.remove(row2.torrent_url)
    settle(world)
    settle(world)
    assert v2.state == DownloadState.FAILED and v1.state == DownloadState.DONE
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED
    assert 2 in downloads.failed_versions(world.ctx(), world.episode(show, 1))


def test_a_replacement_continues_when_the_v1_is_already_gone(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit.remove(row1.torrent_url)
    world.qbit.finish(row2.torrent_url)
    settle(world)
    assert v2.state == DownloadState.DONE and world.episode(show, 1).current_download_id == v2.id


def test_a_replacement_that_never_finishes_is_dropped_after_48_hours(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    settle(world, minutes=49 * 60)
    assert v2.state == DownloadState.FAILED and row2.info_hash not in world.qbit.truth
    assert v1.state == DownloadState.DONE and row1.info_hash in world.qbit.truth


def test_a_torrent_qbit_has_not_shown_yet_is_found_on_a_later_pass(world):
    feed, show, ep = setup(world)
    world.qbit.hide_added = True
    dl, row = grab(world, show, feed, ep)
    assert dl.state == DownloadState.ADDING and world.episode(show, 1).status == EpisodeStatus.DOWNLOADING
    world.qbit.reveal()
    settle(world)
    assert dl.state == DownloadState.DOWNLOADING


def test_an_add_that_is_never_shown_is_retried_after_15_minutes(world):
    feed, show, ep = setup(world)
    world.qbit.hide_added = True
    dl, row = grab(world, show, feed, ep)
    settle(world, minutes=16)
    assert dl.state == DownloadState.RETRY_WAIT
    world.qbit.hide_added = False
    world.qbit.reveal()
    settle(world)
    assert dl.state == DownloadState.DOWNLOADING and dl.attempts == 1


def test_a_refused_add_is_retried_later_and_the_episode_is_wanted_again(world):
    feed, show, ep = setup(world)
    world.qbit.reject_adds = QbitRejected("bad link")
    dl, row = grab(world, show, feed, ep)
    assert dl.state == DownloadState.RETRY_WAIT
    assert dl.next_retry_at == world.clock() + timedelta(minutes=15)
    assert world.episode(show, 1).status == EpisodeStatus.WANTED
    assert show.feed_state == FeedState.NONE
    world.qbit.reject_adds = None
    settle(world, minutes=16)
    assert dl.state == DownloadState.DOWNLOADING and dl.attempts == 2


def test_a_refused_add_of_a_torrent_qbit_already_holds_adopts_it(world):
    feed, show, ep = setup(world)
    row = item(world, feed, V1)
    world.qbit.seed(V1, save_path="/d", category="Seasonal", url=row.torrent_url)
    world.qbit.sync()
    dl = downloads.start_download(world.ctx(), show, feed, ep, row)
    world.commit()
    assert dl.state == DownloadState.DONE or dl.state == DownloadState.DOWNLOADING
    assert dl.info_hash == row.info_hash and show.feed_state == FeedState.LOCKED


def test_an_unavailable_qbit_creates_no_download(world):
    feed, show, ep = setup(world)
    world.qbit.unavailable = QbitUnavailable("down")
    row = item(world, feed, V1)
    with pytest.raises(QbitUnavailable):
        downloads.start_download(world.ctx(), show, feed, ep, row)
    world.session.rollback()
    assert world.session.exec(select(Download)).all() == []


def test_a_grab_that_keeps_failing_gives_up_for_hours_then_fails_for_good(world):
    feed, show, ep = setup(world)
    world.qbit.reject_adds = QbitRejected("bad link")
    dl, row = grab(world, show, feed, ep)
    for _ in range(downloads.MAX_ATTEMPTS):
        settle(world, minutes=16)
    assert dl.state == DownloadState.CANCELED and dl.last_error.startswith("Gave up after")
    episode = world.episode(show, 1)
    assert episode.status == EpisodeStatus.WANTED and episode.retry_after == world.clock() + timedelta(hours=2)
    assert show.feed_state == FeedState.NONE
    for hours in (6, 12):
        dl, _ = grab(world, show, feed, world.episode(show, 1), V1.replace("Grp", f"G{hours}"))
        for _ in range(downloads.MAX_ATTEMPTS):
            settle(world, minutes=16)
        assert dl.state == DownloadState.CANCELED
        assert world.episode(show, 1).retry_after == world.clock() + timedelta(hours=hours)
    dl, _ = grab(world, show, feed, world.episode(show, 1), V1.replace("Grp", "Last"))
    for _ in range(downloads.MAX_ATTEMPTS):
        settle(world, minutes=16)
    assert dl.state == DownloadState.FAILED and world.episode(show, 1).status == EpisodeStatus.FAILED


def test_canceling_the_only_grab_deletes_the_torrent_and_releases_the_lock(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    assert downloads.cancel_episode(world.ctx(), ep, "Canceled by user") == 1
    world.commit()
    assert row.info_hash not in world.qbit.truth and dl.state == DownloadState.CANCELED
    assert world.episode(show, 1).status == EpisodeStatus.WANTED
    assert (show.feed_state, show.feed_id, show.learned_name) == (FeedState.NONE, None, None)


def test_canceling_keeps_a_grab_that_qbit_already_has_when_asked(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    assert downloads.cancel_episode(world.ctx(), ep, "x", keep_added=True) == 0
    assert dl.state == DownloadState.DOWNLOADING


def test_switching_feeds_cancels_unfinished_downloads_from_the_old_one_only(world):
    feed, show, _ = setup(world)
    other = world.feed("Low", "https://low/rss", 2)
    ep1, ep2 = world.episode(show, 1), world.episode(show, 2)
    d1, r1 = grab(world, show, feed, ep1)
    d2, r2 = grab(world, show, feed, ep2, "[Grp] Frieren - 02 (1080p).mkv")
    world.qbit.finish(r1.torrent_url)
    settle(world)
    reset = downloads.cancel_from_other_feeds(world.ctx(), show, other.id, "Feed changed")
    world.commit()
    assert reset == [2] and d1.state == DownloadState.DONE and d2.state == DownloadState.CANCELED


def test_a_torrent_error_fails_the_episode_and_recovers_with_the_torrent(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    world.qbit.fail(row.torrent_url, "missingFiles")
    settle(world)
    assert world.episode(show, 1).status == EpisodeStatus.FAILED
    world.qbit.fail(row.torrent_url, "downloading")
    world.qbit._update(row.info_hash, progress=0.3)
    settle(world)
    assert world.episode(show, 1).status == EpisodeStatus.DOWNLOADING


def test_a_deleted_torrent_of_a_finished_episode_is_forgotten_only_when_the_list_is_stable(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    other, row_other = grab(world, show, feed, world.episode(show, 2), "[Grp] Frieren - 02 (1080p).mkv")
    for url in (row.torrent_url, row_other.torrent_url):
        world.qbit.finish(url)
    settle(world)
    world.qbit.remove(row.torrent_url)
    assert settle(world).busy and dl.info_hash == row.info_hash
    settle(world)
    assert dl.info_hash is None and dl.state == DownloadState.DONE
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED


def test_nothing_is_forgotten_while_qbit_lists_nothing_after_it_restarted(world):
    feed, show, _ = setup(world)
    grabs = [grab(world, show, feed, world.episode(show, n), f"[Grp] Frieren - 0{n} (1080p).mkv") for n in (1, 2)]
    third = world.show(2, "Other", episodes=1, first_air=START - timedelta(days=1))
    grabs.append(grab(world, third, feed, world.episode(third, 1), "[Grp] Other - 01 (1080p).mkv"))
    for _, row in grabs:
        world.qbit.finish(row.torrent_url)
    settle(world)
    world.qbit.truth.clear()
    for _ in range(3):
        settle(world)
    assert all(dl.info_hash for dl, _ in grabs)


def test_an_unfinished_download_removed_in_qbit_is_wanted_again_after_a_pause(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    other, row_other = grab(world, show, feed, world.episode(show, 2), "[Grp] Frieren - 02 (1080p).mkv")
    world.qbit.remove(row.torrent_url)
    settle(world)
    settle(world)
    episode = world.episode(show, 1)
    assert dl.state == DownloadState.CANCELED and episode.status == EpisodeStatus.WANTED
    assert episode.retry_after == world.clock() + timedelta(minutes=15)


def test_a_paused_show_still_finishes_a_replacement_already_in_qbit(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    show.paused = True
    world.commit()
    world.qbit.finish(row2.torrent_url)
    settle(world)
    assert v2.state == DownloadState.DONE


def test_a_paused_show_does_not_retry_adds(world):
    feed, show, ep = setup(world)
    world.qbit.reject_adds = QbitRejected("bad link")
    dl, _ = grab(world, show, feed, ep)
    show.paused = True
    world.commit()
    world.qbit.reject_adds = None
    settle(world, minutes=30)
    assert dl.state == DownloadState.RETRY_WAIT


def test_a_per_show_category_and_ratio_override_the_defaults(world):
    feed, show, ep = setup(world, category="Movies", seed_ratio=2.5)
    grab(world, show, feed, ep)
    added = world.qbit.calls_named("add_torrent")[0]
    assert (added["category"], added["ratio_limit"]) == ("Movies", 2.5)


def test_grabbing_records_history_and_stale_operation_tags_are_swept(world):
    feed, show, ep = setup(world)
    grab(world, show, feed, ep)
    from kisetsu.db.schema import Event

    assert [e.kind for e in world.session.exec(select(Event)).all()].count(EventKind.GRABBED) == 1
    world.qbit.tag_names.add("kisetsu-op-" + "a" * 32)
    assert downloads.sweep_tags(world.ctx()) == 1


def test_a_failed_replacement_is_followed_no_more_and_settle_goes_quiet(world):
    from kisetsu.db.schema import Event

    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit.remove(row2.torrent_url)
    results = [settle(world) for _ in range(10)]
    failed = [e for e in world.session.exec(select(Event)).all() if e.kind == EventKind.FAILED]
    assert v2.state == DownloadState.FAILED and v2.info_hash is None and len(failed) == 1
    assert not results[-1].busy and not results[-1].changed


def test_the_fast_poll_is_bounded_while_a_replacement_sits_in_verification(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    world.qbit.hold_checks = True
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    assert settle(world).busy
    later = settle(world, minutes=10)
    assert not later.busy and later.active


def test_a_replacement_seen_first_while_checking_is_still_resumed_before_the_old_release_goes(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    world.qbit.hide_added = True
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit.reveal()
    world.qbit._update(row2.info_hash, state="checkingDL")
    settle(world)
    assert world.qbit.calls_named("recheck") and world.qbit.calls_named("resume")
    world.qbit.finish(row2.torrent_url)
    settle(world)
    names = [name for name, _ in world.qbit.calls]
    assert names.index("resume") < names.index("delete")
    assert v2.state == DownloadState.DONE


def test_a_finished_but_stopped_replacement_is_resumed_and_the_old_release_waits(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit._update(row2.info_hash, progress=1.0, amount_left=0, state="stoppedUP")
    settle(world)
    assert row1.info_hash in world.qbit.truth and v2.state != DownloadState.DONE
    assert world.qbit.calls_named("resume")[-1] == [row2.info_hash]
    world.qbit._update(row2.info_hash, state="stalledUP")
    settle(world)
    assert v2.state == DownloadState.DONE and row1.info_hash not in world.qbit.truth


def test_a_replacement_qbit_stopped_at_its_seed_limit_completes_and_the_old_release_goes(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    world.qbit._update(row2.info_hash, progress=1.0, amount_left=0, state="stoppedUP", seeding_time=371)
    settle(world)
    assert v2.state == DownloadState.DONE and row1.info_hash not in world.qbit.truth
    assert world.qbit.calls_named("resume") == [[row2.info_hash]]


def test_the_lock_survives_an_error_after_qbit_accepted_the_add(world):
    from kisetsu.clients.qbittorrent import QbitUnavailable

    feed, show, ep = setup(world)
    world.qbit.sync()
    real_sync = world.qbit.sync
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        raise QbitUnavailable("down")

    row = item(world, feed, V1)
    world.qbit.sync = flaky
    dl = downloads.start_download(world.ctx(), show, feed, ep, row)
    world.session.rollback()
    world.qbit.sync = real_sync
    assert dl.state == DownloadState.ADDING
    assert (show.feed_state, show.feed_id) == (FeedState.LOCKED, feed.id)
    settle(world)
    assert dl.state == DownloadState.DOWNLOADING


def test_a_qbit_read_failure_while_settling_leaves_the_downloads_untouched(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    world.qbit.unavailable = QbitUnavailable("down")
    with pytest.raises(QbitUnavailable):
        downloads.settle(world.ctx())
    world.session.rollback()
    assert world.episode(show, 1).status == EpisodeStatus.DOWNLOADING
    assert (dl.state, dl.info_hash) == (DownloadState.DOWNLOADING, row.info_hash)


def test_a_finished_torrent_that_qbit_stopped_completes_the_episode_without_another_add(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    world.qbit._update(row.info_hash, progress=1.0, amount_left=0, state="stoppedUP")
    settle(world)
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED and dl.state == DownloadState.DONE
    assert len(world.qbit.calls_named("add_torrent")) == 1


def test_a_refused_replacement_add_leaves_the_finished_episode_and_its_torrent_alone(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    world.qbit.reject_adds = QbitRejected("add failed")
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    settle(world, minutes=16)
    assert v2.state == DownloadState.RETRY_WAIT and v1.state == DownloadState.DONE
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED
    assert world.episode(show, 1).current_download_id == v1.id and row1.info_hash in world.qbit.truth
    assert world.qbit.calls_named("delete") == []


def test_a_completed_show_still_finishes_a_replacement_already_in_qbit(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    show.completed = True
    world.commit()
    world.qbit.finish(row2.torrent_url)
    settle(world)
    assert v2.state == DownloadState.DONE and row1.info_hash not in world.qbit.truth


def test_canceling_a_replacement_restores_the_previous_release_and_deletes_only_the_new_torrent(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    assert downloads.cancel_episode(world.ctx(), world.episode(show, 1), "Show paused.") == 1
    world.commit()
    episode = world.episode(show, 1)
    assert v2.state == DownloadState.CANCELED and v2.next_retry_at is None
    assert episode.current_download_id == v1.id and episode.status == EpisodeStatus.COMPLETED
    assert row1.info_hash in world.qbit.truth and row2.info_hash not in world.qbit.truth
    assert world.qbit.deleted_with_files == [row2.info_hash]


def test_canceling_a_replacement_keeps_the_files_it_shares_with_the_previous_release(world):
    feed, show, ep = setup(world)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    world.qbit.names[same := "https://example.test/same.torrent"] = world.qbit.truth[row1.info_hash].name
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2, torrent_url=same)
    assert downloads.cancel_episode(world.ctx(), world.episode(show, 1), "Canceled.") == 1
    assert row2.info_hash not in world.qbit.truth and world.qbit.deleted_with_files == []
    assert row1.info_hash in world.qbit.truth


def test_an_operation_tag_stays_while_qbit_has_not_shown_the_torrent(world):
    feed, show, ep = setup(world)
    world.qbit.hide_added = True
    dl, row = grab(world, show, feed, ep)
    settle(world)
    assert dl.tag and dl.info_hash == row.info_hash
    assert world.qbit.calls_named("remove_tags") == [] and world.qbit.calls_named("delete_tags") == []


def test_a_failing_tag_removal_never_disturbs_the_download(world):
    feed, show, ep = setup(world)

    def broken(*args, **kwargs):
        raise QbitError("tag API unavailable")

    world.qbit.remove_tags = broken
    dl, row = grab(world, show, feed, ep)
    world.qbit.finish(row.torrent_url)
    settle(world)
    assert dl.state == DownloadState.DONE and dl.tag
    assert world.episode(show, 1).status == EpisodeStatus.COMPLETED


def test_no_desktop_notification_is_sent_while_the_setting_is_off(world):
    feed, show, ep = setup(world)
    dl, row = grab(world, show, feed, ep)
    world.qbit.finish(row.torrent_url)
    settle(world)
    assert world.ctx().settings.desktop_notifications is False
    assert dl.state == DownloadState.DONE and world.notifications == []


def test_switching_feeds_leaves_a_download_of_unknown_origin_alone(world):
    feed, show, ep = setup(world)
    other = world.feed("Low", "https://low/rss", 2)
    dl, row = grab(world, show, feed, ep)
    dl.feed_id = None
    world.commit()
    assert downloads.cancel_from_other_feeds(world.ctx(), show, other.id, "Feed changed") == []
    assert dl.state == DownloadState.DOWNLOADING and row.info_hash in world.qbit.truth


def test_switching_feeds_cancels_a_replacement_and_restores_the_previous_release(world):
    feed, show, ep = setup(world)
    other = world.feed("Low", "https://low/rss", 2)
    v1, row1 = grab(world, show, feed, ep)
    world.qbit.finish(row1.torrent_url)
    settle(world)
    v2, row2 = grab(world, show, feed, world.episode(show, 1), V2)
    assert downloads.cancel_from_other_feeds(world.ctx(), show, other.id, "Feed changed") == [1]
    world.commit()
    episode = world.episode(show, 1)
    assert v2.state == DownloadState.CANCELED and row2.info_hash not in world.qbit.truth
    assert episode.current_download_id == v1.id and episode.status == EpisodeStatus.COMPLETED


def test_a_torrent_qbit_keeps_outside_the_requested_folder_is_reported_once_per_show(world):
    from kisetsu.db.schema import Event

    feed, show, ep = setup(world)
    ctx = world.ctx()
    ctx.settings.base_dir = "/home/u/Anime/{name}"
    world.commit()
    world.qbit.hide_added = True
    rows = [grab(world, show, feed, world.episode(show, n), V1.replace("01", f"0{n}"))[1] for n in (1, 2)]
    world.qbit.reveal()
    for row in rows:
        world.qbit._update(row.info_hash, save_path="/downloads")
    settle(world)
    notes = [e.message for e in world.session.exec(select(Event)).all() if "can see" in e.message]
    assert len(notes) == 1 and "/downloads" in notes[0] and str(Path("/home/u/Anime/Sousou no Frieren").resolve()) in notes[0]
