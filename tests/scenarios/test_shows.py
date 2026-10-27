from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import select

from kisetsu.db.schema import (
    DismissedShow, Download, Episode, EpisodeOffset, EpisodeStatus, FeedState, OffsetSource, PausedBy, ScheduleState, Show,
)
from kisetsu.services import downloads, grab, ingest, shows
from kisetsu.services.feeds import FeedLockedError
from kisetsu.services.ingest import _new_item
from tests.fakes import START, article


def entry(anilist_id=1, title="Sousou no Frieren", **fields):
    data = {
        "anilist_id": anilist_id, "title_english": title, "title_romaji": title, "aliases": [title],
        "status": "RELEASING", "list_status": "CURRENT", "total_episodes": 12,
        "next_airing_episode": 4, "next_airing_at": START + timedelta(days=2), "season": "FALL", "season_year": 2026,
    }
    data.update(fields)
    return data


def apply(world, *entries):
    result = shows.apply_anilist(world.ctx(), list(entries))
    world.commit()
    return result


def test_a_new_show_is_added_with_its_aliases_and_then_updated_in_place(world):
    result = apply(world, entry())
    show = result.added[0]
    assert (show.anilist_id, show.aliases, show.save_folder) == (1, ["Sousou no Frieren"], "")
    apply(world, entry(total_episodes=28, aliases=["Frieren", "Sousou no Frieren"]))
    assert show.total_episodes == 28 and set(show.aliases) == {"Sousou no Frieren", "Frieren"}
    assert world.session.exec(select(Show)).all() == [show]


def test_shows_added_while_everything_is_paused_start_paused_by_the_bulk_action(world):
    shows.pause_all(world.ctx())
    show = apply(world, entry()).added[0]
    assert show.paused and show.paused_by == PausedBy.ALL


def test_a_show_completed_by_the_list_status_reopens_when_the_status_goes_back_to_current(world):
    show = apply(world, entry()).added[0]
    apply(world, entry(list_status="COMPLETED"))
    assert show.completed and show.next_airing_episode is None
    apply(world, entry(list_status="CURRENT", next_airing_episode=5, next_airing_at=START + timedelta(days=7)))
    assert not show.completed and show.next_airing_episode == 5 and show.next_airing_at is not None


def test_the_sync_refreshes_aliases_but_never_absorbs_or_removes_hand_written_ones(world):
    show = apply(world, entry(aliases=["Sousou no Frieren"])).added[0]
    show.custom_aliases = ["Ao Ashi"]
    world.commit()
    apply(world, entry(aliases=["Sousou no Frieren", "Frieren JP"]))
    assert show.custom_aliases == ["Ao Ashi"] and "Frieren JP" in show.aliases
    assert "Ao Ashi" not in show.aliases and "Ao Ashi" in show.effective_aliases


def test_the_sync_does_not_rewrite_a_show_whose_data_is_unchanged(world):
    show = apply(world, entry()).added[0]
    world.session.expire_all()
    with patch.object(world.session, "add", wraps=world.session.add) as add:
        shows.apply_anilist(world.ctx(), [entry()])
    assert all(call.args[0] is not show for call in add.call_args_list)


def test_a_deleted_show_stays_out_until_it_leaves_the_list_and_returns(world):
    show = apply(world, entry()).added[0]
    shows.delete_show(world.ctx(), show, reason="user", dismiss=True)
    world.commit()
    assert apply(world, entry()).added == []
    apply(world, entry(2, "Other"))
    assert world.session.get(DismissedShow, 1) is None
    assert len(apply(world, entry()).added) == 1


def test_a_show_marked_completed_on_the_list_is_completed_even_with_episodes_left(world):
    show = apply(world, entry()).added[0]
    apply(world, entry(list_status="COMPLETED"))
    assert show.completed and show.next_airing_episode is None
    apply(world, entry(list_status="COMPLETED", next_airing_episode=5))
    assert show.completed


def test_a_finished_show_completes_only_when_every_episode_is_accounted_for(world):
    show = apply(world, entry(status="FINISHED", total_episodes=2, next_airing_episode=None, next_airing_at=None)).added[0]
    shows.ensure_episodes(world.ctx(), show)
    world.commit()
    apply(world, entry(status="FINISHED", total_episodes=2, next_airing_episode=None, next_airing_at=None))
    assert not show.completed and show.next_airing_episode == 2
    for number in (1, 2):
        world.episode(show, number).status = EpisodeStatus.COMPLETED
        world.commit()
    apply(world, entry(status="FINISHED", total_episodes=2, next_airing_episode=None, next_airing_at=None))
    assert show.completed


def test_a_just_missed_finale_keeps_the_season_open_for_two_weeks(world):
    show = apply(world, entry(status="FINISHED", total_episodes=2, next_airing_episode=None, next_airing_at=None)).added[0]
    shows.ensure_episodes(world.ctx(), show)
    world.commit()
    first, last = world.episode(show, 1), world.episode(show, 2)
    first.status, last.status = EpisodeStatus.COMPLETED, EpisodeStatus.MISSED
    last.air_at = START - timedelta(days=3)
    world.commit()
    assert not shows.final_episode_accounted(world.ctx(), show)
    last.air_at = START - timedelta(days=20)
    world.commit()
    assert shows.final_episode_accounted(world.ctx(), show)


def test_a_show_completed_on_downloads_alone_reopens_when_that_evidence_goes(world):
    show = apply(world, entry(status="FINISHED", total_episodes=1, next_airing_episode=None, next_airing_at=None)).added[0]
    shows.ensure_episodes(world.ctx(), show)
    world.episode(show, 1).status = EpisodeStatus.COMPLETED
    world.commit()
    apply(world, entry(status="FINISHED", total_episodes=1, next_airing_episode=None, next_airing_at=None))
    assert show.completed
    world.episode(show, 1).status = EpisodeStatus.WANTED
    world.commit()
    apply(world, entry(status="FINISHED", total_episodes=1, next_airing_episode=None, next_airing_at=None))
    assert not show.completed and show.schedule_synced_at is None


def test_a_show_that_left_the_list_is_removed_but_an_empty_list_is_a_bad_read(world):
    first = apply(world, entry(1), entry(2, "Other")).added
    existing = {s.anilist_id: s for s in first}
    ctx = world.ctx()
    assert "empty list" in shows.remove_off_list(ctx, [], existing)[0]
    assert len(world.session.exec(select(Show)).all()) == 2
    logs = shows.remove_off_list(ctx, [entry(1)], existing)
    world.commit()
    assert logs == ["Removed 'Other': no longer on your AniList list."]
    assert [s.anilist_id for s in world.session.exec(select(Show)).all()] == [1]


def test_deleting_a_show_cancels_what_it_has_in_flight_and_can_remove_its_torrents(world):
    feed = world.feed("Top", "https://top/rss", 1)
    world.ctx().settings.default_category = "Seasonal"
    show = world.show(episodes=2, first_air=START - timedelta(days=7))
    ingest.ingest(world.ctx(), feed, [article("[Grp] Sousou no Frieren - 02 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    assert len(world.qbit.truth) == 1
    removed = shows.delete_show(world.ctx(), show, reason="user", dismiss=True, delete_torrents=True, delete_files=True)
    world.commit()
    assert removed == 1 and world.qbit.truth == {} and world.session.get(DismissedShow, show.anilist_id) is not None
    assert world.session.exec(select(Episode)).all() == [] and world.session.exec(select(Download)).all() == []


def test_deleting_a_show_also_removes_an_in_flight_replacement_and_the_torrent_it_replaces(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1))
    episode = world.episode(show, 1)
    v1 = downloads.start_download(world.ctx(), show, feed, episode, _new_item(feed, article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START), START))
    world.commit()
    world.qbit.finish(v1.info_hash)
    downloads.settle(world.ctx())
    v2 = downloads.start_download(world.ctx(), show, feed, episode, _new_item(feed, article("[Grp] Sousou no Frieren - 01v2 (1080p).mkv", START), START))
    world.commit()
    assert len(world.qbit.truth) == 2
    removed = shows.delete_show(world.ctx(), show, reason="user", delete_torrents=True, delete_files=True)
    world.commit()
    assert removed == 2 and world.qbit.truth == {}
    assert set(world.qbit.deleted_with_files) == {v1.info_hash, v2.info_hash}


def test_air_times_are_written_onto_the_episodes_and_the_show_is_marked_synced(world):
    show = world.show(total_episodes=3, anilist_id=5)
    schedules = {5: [{"episode": 1, "airing_at": START - timedelta(days=7)}, {"episode": 2, "airing_at": START + timedelta(days=7)}]}
    updated, missing = shows.apply_schedules(world.ctx(), [show, world.show(anilist_id=6, title="Other")], schedules)
    world.commit()
    assert updated == 1 and missing == ["Other"]
    assert world.episode(show, 1).schedule_state == ScheduleState.AIRED
    assert world.episode(show, 2).schedule_state == ScheduleState.SCHEDULED
    assert show.schedule_synced_at == START


def test_shows_are_due_for_a_schedule_after_six_hours_oldest_first(world):
    old = world.show(1, "A", schedule_synced_at=START - timedelta(hours=7))
    older = world.show(2, "B", schedule_synced_at=START - timedelta(days=2))
    world.show(3, "C", schedule_synced_at=START - timedelta(hours=1))
    never = world.show(4, "D")
    done = world.show(5, "E", completed=True)
    assert [s.id for s in shows.schedules_due(world.ctx())] == [never.id, older.id, old.id]
    assert done not in shows.schedules_due(world.ctx())


def test_a_dated_schedule_only_needs_a_look_ahead(world):
    show = world.show(next_airing_episode=3, schedule_synced_at=START, episodes=2, first_air=START - timedelta(days=7))
    assert shows.schedule_is_dated(world.ctx(), show)
    undated = world.show(2, "U", next_airing_episode=3, schedule_synced_at=START, episodes=2)
    assert not shows.schedule_is_dated(world.ctx(), undated)
    assert shows.covers_undated(world.ctx(), undated, [{"episode": 1, "airing_at": START}, {"episode": 2, "airing_at": START}])
    assert not shows.covers_undated(world.ctx(), undated, [{"episode": 1, "airing_at": START}])


def test_an_overdue_episode_is_recorded_as_aired_and_a_final_one_completes_the_show(world):
    show = world.show(total_episodes=2, next_airing_episode=1, next_airing_at=START - timedelta(hours=1), episodes=2, first_air=START - timedelta(hours=1))
    shows.rollover(world.ctx())
    world.commit()
    assert world.episode(show, 1).schedule_state == ScheduleState.AIRED
    finale = world.show(2, "F", total_episodes=1, next_airing_episode=1, next_airing_at=START - timedelta(hours=1), episodes=1)
    world.episode(finale, 1).status = EpisodeStatus.COMPLETED
    world.commit()
    logs = shows.rollover(world.ctx())
    world.commit()
    assert finale.completed and "finished" in logs[0]


def test_only_completed_past_season_shows_with_everything_downloaded_are_pruned(world):
    old = dict(season_name="SUMMER", season_year=2025)
    done = world.show(1, "Done", episodes=1, total_episodes=1, completed=True, **old)
    world.episode(done, 1).status = EpisodeStatus.COMPLETED
    movie = world.show(2, "Movie", episodes=1, total_episodes=1, **old)
    gap = world.show(3, "Gap", episodes=1, total_episodes=1, completed=True, **old)
    current = world.show(4, "Current", episodes=1, total_episodes=1, completed=True, season_name="FALL", season_year=2026)
    world.episode(current, 1).status = EpisodeStatus.COMPLETED
    world.commit()
    logs = shows.prune_past_seasons(world.ctx())
    world.commit()
    assert logs == ["Removed finished show 'Done' (SUMMER 2025)."]
    assert {s.id for s in world.session.exec(select(Show)).all()} == {movie.id, gap.id, current.id}


def test_pausing_cancels_unadded_grabs_but_never_touches_added_torrents(world):
    from kisetsu.clients.qbittorrent import QbitRejected

    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=2, first_air=START - timedelta(days=7))
    ingest.ingest(world.ctx(), feed, [article("[Grp] Sousou no Frieren - 02 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    ingest.ingest(world.ctx(), feed, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START - timedelta(days=7))])
    world.commit()
    world.qbit.reject_adds = QbitRejected("no")
    grab.grab_pass(world.ctx())
    world.commit()
    shows.pause(world.ctx(), show)
    world.commit()
    states = {world.session.get(Episode, d.episode_id).number: d.state.value for d in world.session.exec(select(Download)).all()}
    assert show.paused and show.paused_by == PausedBy.USER
    assert states == {1: "canceled", 2: "downloading"}
    assert len(world.qbit.truth) == 1 and not world.qbit.calls_named("delete")


def test_pause_all_and_resume_all_touch_only_the_shows_they_paused(world):
    manual = world.show(1, "A")
    auto = world.show(2, "B")
    other = world.show(3, "C", completed=True)
    shows.pause(world.ctx(), manual)
    assert shows.pause_all(world.ctx()) == 1
    assert auto.paused_by == PausedBy.ALL and manual.paused_by == PausedBy.USER and not other.paused
    assert shows.resume_all(world.ctx()) == 1
    assert not auto.paused and manual.paused
    assert not shows.all_paused(world.ctx())


def test_editing_the_feed_pins_it_and_cancels_unfinished_downloads_from_the_old_one(world):
    old = world.feed("Old", "https://old/rss", 1)
    new = world.feed("New", "https://new/rss", 2)
    show = world.show(episodes=2, first_air=START - timedelta(days=7), feed=old, feed_state=FeedState.ASSIGNED)
    ingest.ingest(world.ctx(), old, [article("[Grp] Sousou no Frieren - 02 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    assert show.feed_state == FeedState.LOCKED
    with pytest.raises(FeedLockedError):
        shows.edit_show(world.ctx(), show, feed_id=new.id)
    result = shows.edit_show(world.ctx(), show, feed_id=new.id, release_lock=True)
    world.commit()
    assert result.canceled == [2] and "Pinned to 'New'" in result.message
    assert (show.feed_id, show.feed_state) == (new.id, FeedState.PINNED)
    assert world.qbit.truth == {}


def test_auto_discover_clears_a_pin_and_a_manual_offset_is_kept_per_feed(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(feed=feed, feed_state=FeedState.PINNED)
    shows.edit_show(world.ctx(), show, episode_offset=12)
    world.commit()
    offset = world.session.exec(select(EpisodeOffset)).one()
    assert (offset.offset, offset.source, offset.feed_id) == (12, OffsetSource.MANUAL, feed.id)
    result = shows.edit_show(world.ctx(), show, feed_id=0)
    assert "Auto-Discover" in result.message and show.feed_state == FeedState.NONE


def test_editing_aliases_and_folder_rematches_stored_releases(world):
    feed = world.feed("Top", "https://top/rss", 1)
    show = world.show(episodes=1, first_air=START - timedelta(days=1))
    ingest.ingest(world.ctx(), feed, [article("[Grp] Frieren Beyond - 01 (1080p).mkv", START)])
    world.commit()
    assert grab.grab_pass(world.ctx()).started == 0
    shows.edit_show(world.ctx(), show, aliases=["Frieren Beyond"], save_folder=" /media/anime/{name} ", category="  ", seed_ratio=-1)
    world.commit()
    assert show.save_folder == "/media/anime/{name}" and show.category is None and show.seed_ratio is None
    assert grab.grab_pass(world.ctx()).started == 1


def test_the_torrent_preview_lists_tracked_ones_and_untracked_ones_in_the_show_folder(world):
    feed = world.feed("Top", "https://top/rss", 1)
    ctx = world.ctx()
    ctx.settings.default_category = "Seasonal"
    base = Path("/anime").resolve()
    ctx.settings.base_dir = base.as_posix()
    show = world.show(episodes=1, first_air=START - timedelta(days=1))
    ingest.ingest(ctx, feed, [article("[Grp] Sousou no Frieren - 01 (1080p).mkv", START)])
    world.commit()
    grab.grab_pass(world.ctx())
    world.commit()
    world.qbit.seed("[Old] Sousou no Frieren - 00 (1080p).mkv", save_path=(base / "Sousou no Frieren").as_posix(), category="Seasonal")
    world.qbit.seed("[Old] Other Show - 01 (1080p).mkv", save_path=(base / "Sousou no Frieren").as_posix(), category="Seasonal")
    from kisetsu.services import library

    preview = library.show_torrent_preview(world.ctx(), show)
    assert [(e["tracked"], e["name"].split(" - ")[0]) for e in preview] == [(True, "[Grp] Sousou no Frieren"), (False, "[Old] Sousou no Frieren")]


def test_changing_the_title_language_moves_only_torrents_that_are_where_the_old_folder_is(world):
    from kisetsu.services import library

    ctx = world.ctx()
    ctx.settings.default_category = "Seasonal"
    base = Path("/anime").resolve()
    ctx.settings.base_dir = base.as_posix()
    show = world.show(episodes=1, first_air=START - timedelta(days=1), title="Frieren", title_romaji="Sousou no Frieren")
    world.qbit.seed("[Grp] Frieren - 01 (1080p).mkv", save_path=(base / "Frieren").as_posix(), category="Seasonal")
    world.qbit.sync()
    plan = library.plan_language_move(world.ctx(), "romaji")
    assert [(p["old"], p["new"], len(p["hashes"])) for p in plan] == [(str(base / "Frieren"), str(base / "Sousou no Frieren"), 1)]
    logs, failed = library.relocate_torrents(world.ctx(), plan)
    assert failed == [] and "Moved 1 downloads" in logs[0] and show.id


def test_a_show_with_no_airing_schedule_still_gets_its_episode_rows(world):
    from kisetsu.db.schema import Episode

    movie = apply(world, entry(7, "Kimi no Na wa", total_episodes=1, next_airing_episode=None, next_airing_at=None, status="NOT_YET_RELEASED")).added[0]
    assert [e.number for e in world.session.exec(select(Episode).where(Episode.show_id == movie.id)).all()] == [1]
    apply(world, entry(7, "Kimi no Na wa", total_episodes=1, next_airing_episode=None, next_airing_at=None, status="NOT_YET_RELEASED"))
    assert len(world.session.exec(select(Episode).where(Episode.show_id == movie.id)).all()) == 1


def test_auto_discover_all_releases_hand_pins_and_keeps_proven_feeds(world):
    top = world.feed("Top", "https://top/rss", 1)
    pinned = world.show(1, "Pinned", feed=top, feed_state=FeedState.PINNED)
    locked = world.show(2, "Locked", feed=top, feed_state=FeedState.LOCKED)
    assert shows.auto_discover_all(world.ctx()) == 1
    assert (pinned.feed_id, pinned.feed_state) == (None, FeedState.NONE)
    assert (locked.feed_id, locked.feed_state) == (top.id, FeedState.LOCKED)
