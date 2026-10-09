import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from kisetsu.clients.qbit import QbitClientError
from kisetsu.core.discovery import RssSnapshot
from kisetsu.core.grabber import FEED_DISCOVERY_GRACE_SECONDS, cancel_episode_operations, evaluate_and_grab_releases, show_torrent_hashes, sync_show_episodes, update_episode_status
from kisetsu.core.supervisor import Supervisor
from kisetsu.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    MatchedFeedItem,
    Monitored,
    MonitoredStatus,
    Settings,
    TorrentOperation,
    TorrentOperationStatus,
    utc_now,
)


def _database():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine, Session(engine)


def _show(session, **kwargs):
    values = {
        "id": 1,
        "anilist_id": 154587,
        "display_name": "Sousou no Frieren",
        "aliases_json": '["Sousou no Frieren", "Frieren"]',
        "status": MonitoredStatus.UNCONFIRMED,
        "total_episodes": 8,
        "next_airing_episode": 8,
        "next_airing_at": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    values.update(kwargs)
    show = Monitored(**values)
    session.add(show)
    session.commit()
    return show


def _qbit(title="[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", events=None):
    qbit = MagicMock()
    torrent = MagicMock(hash="hash-1", progress=0.1, state="downloading", name=title)
    qbit.ensure_category_exists.return_value = True
    old_release = MagicMock(hash="old-hash", progress=1.0, state="uploading")

    def get_torrents(**kwargs):
        # A finished episode's own torrent is still in qBittorrent.
        if kwargs.get("hashes") == ["old-hash"]:
            return [old_release]
        return [torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("tag") == "kisetsu-managed" else []

    qbit.get_torrents.side_effect = get_torrents
    qbit.add_torrent.side_effect = lambda **kwargs: events.append("add") if events is not None else True
    qbit.pause_torrents.side_effect = lambda *args: events.append("pause") if events is not None else None
    qbit.delete_torrents.side_effect = lambda *args, **kwargs: events.append("delete") if events is not None else None
    qbit.recheck_torrents.side_effect = lambda *args: events.append("recheck") if events is not None else None
    qbit.resume_torrents.side_effect = lambda *args: events.append("resume") if events is not None else None
    return qbit, torrent


def test_direct_grab_is_idempotent_across_cycles():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "hash-1"
    assert qbit.add_torrent.call_count == 1
    assert show.status == MonitoredStatus.FIXED
    session.close()
    engine.dispose()


def test_duplicate_feed_item_ids_are_ingested_once():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session)
    qbit, _ = _qbit()
    article = {
        "id": "duplicate-id",
        "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv",
        "torrentURL": "magnet:ep8",
    }
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [article, dict(article)],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert len(session.exec(select(MatchedFeedItem)).all()) == 1
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_an_undated_release_does_not_fill_an_old_episode():
    engine, session = _database()
    settings = Settings(
        id=1,
        default_category="Anime",
        download_mode="direct",
        backfill_window_days=14,
    )
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    now = datetime.now(timezone.utc)
    show = _show(
        session,
        total_episodes=8,
        next_airing_episode=9,
        next_airing_at=now + timedelta(days=7),
    )
    session.flush()
    session.add(Episode(
        monitored_id=show.id,
        episode_number=7,
        status=EpisodeStatus.WANTED,
        air_at=now - timedelta(days=30),
    ))
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "old-ep7",
                "title": "[SubsPlease] Sousou no Frieren - 07 (1080p).mkv",
                "torrentURL": "magnet:old-ep7",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 7)
    ).first()
    # Not taken, and not written off: it stays wanted for a dated release.
    assert episode.status == EpisodeStatus.WANTED
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_two_week_outage_without_retained_articles_preserves_canonical_backlog():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    now = datetime.now(timezone.utc)
    show = _show(
        session,
        total_episodes=8,
        next_airing_episode=9,
        next_airing_at=now + timedelta(days=7),
    )
    session.flush()
    for episode_number in (7, 8):
        session.add(Episode(
            monitored_id=show.id,
            episode_number=episode_number,
            status=EpisodeStatus.WANTED,
            air_at=now - timedelta(days=14 - episode_number),
        ))
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episodes = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number.in_([7, 8]),
        )
    ).all()
    assert all(episode.status == EpisodeStatus.WANTED for episode in episodes)
    assert qbit.add_torrent.call_count == 0
    session.close()
    engine.dispose()


def test_direct_cycle_queues_two_backlog_episodes_once():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        last_confirmed_episode=6,
        next_airing_episode=9,
        next_airing_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    torrents = {}

    def add_torrent(**kwargs):
        tags = kwargs["tags"]
        operation_tag = next(tag for tag in tags if tag.startswith("kisetsu-op-"))
        torrent = MagicMock(
            hash=f"hash-{operation_tag[-4:]}",
            progress=0.1,
            state="downloading",
            name=kwargs["urls"],
        )
        torrents[operation_tag] = torrent
        return True

    qbit = MagicMock()
    qbit.ensure_category_exists.return_value = True
    qbit.add_torrent.side_effect = add_torrent
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrents[kwargs["tag"]]]
        if kwargs.get("tag", "").startswith("kisetsu-op-") and kwargs["tag"] in torrents
        else list(torrents.values())
        if kwargs.get("tag") == "kisetsu-managed"
        else [torrent for torrent in torrents.values() if torrent.hash in kwargs.get("hashes", [])]
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:ep8"},
                {"id": "ep7", "title": "[SubsPlease] Sousou no Frieren - 07 (1080p).mkv", "torrentURL": "magnet:ep7"},
            ],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episodes = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number.in_([7, 8]),
        )
    ).all()
    operations = session.exec(select(TorrentOperation)).all()
    assert {episode.episode_number for episode in episodes} == {7, 8}
    assert all(episode.status == EpisodeStatus.DOWNLOADING for episode in episodes)
    assert len(operations) == 2
    assert qbit.add_torrent.call_count == 2
    session.close()
    engine.dispose()


def test_ambiguous_direct_add_retries_after_reconciliation_window():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session)
    qbit, torrent = _qbit()
    visible = {"value": False}
    attempts = {"count": 0}

    def add_torrent(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("response lost")
        visible["value"] = True
        return True

    qbit.add_torrent.side_effect = add_torrent
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent]
        if visible["value"] and (kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("tag") == "kisetsu-managed")
        else [torrent]
        if visible["value"] and torrent.hash in kwargs.get("hashes", [])
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    operation = session.exec(select(TorrentOperation)).first()
    operation.next_retry_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    episode = session.get(Episode, operation.episode_id)
    episode.retry_after = operation.next_retry_at
    session.add(operation)
    session.add(episode)
    session.commit()

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    session.refresh(operation)
    session.refresh(episode)
    assert operation.status == TorrentOperationStatus.COMPLETED
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert qbit.add_torrent.call_count == 2
    assert session.exec(select(TorrentOperation)).all() == [operation]
    session.close()
    engine.dispose()


def test_delayed_hash_after_restart_is_found_before_timeout_failure():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct")
    show = _show(session)
    episode = Episode(
        monitored_id=show.id,
        episode_number=8,
        status=EpisodeStatus.QUEUED,
        operation_tag="kisetsu-op-delayed",
    )
    session.add(episode)
    session.flush()
    operation = TorrentOperation(
        episode_id=episode.id,
        kind="grab",
        status=TorrentOperationStatus.PREPARING,
        operation_tag="kisetsu-op-delayed",
        release_title="[SubsPlease] Sousou no Frieren - 08 (1080p).mkv",
        version=1,
        new_torrent_url="magnet:ep8",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    session.add(operation)
    session.commit()
    torrent = MagicMock(hash="delayed-hash", progress=0.2, state="downloading", name=operation.release_title)
    qbit = MagicMock()
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent]
        if kwargs.get("tag") == operation.operation_tag
        or kwargs.get("tag") == "kisetsu-managed"
        or torrent.hash in kwargs.get("hashes", [])
        else []
    )

    update_episode_status(session, qbit, settings)

    session.refresh(operation)
    session.refresh(episode)
    assert operation.status == TorrentOperationStatus.COMPLETED
    assert operation.new_torrent_hash == "delayed-hash"
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "delayed-hash"
    session.close()
    engine.dispose()


def test_torrent_read_error_preserves_active_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct")
    show = _show(session)
    episode = Episode(
        monitored_id=show.id,
        episode_number=8,
        status=EpisodeStatus.DOWNLOADING,
        torrent_hash="active-hash",
    )
    session.add(episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_torrents.side_effect = QbitClientError("temporary read failure")

    try:
        update_episode_status(session, qbit, settings)
    except QbitClientError:
        pass
    else:
        raise AssertionError("Expected torrent lookup failure")

    session.refresh(episode)
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "active-hash"
    session.close()
    engine.dispose()


def test_direct_infers_absolute_feed_offset_from_latest_anilist_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        display_name="Re:ZERO -Starting Life in Another World- Season 4",
        aliases_json='["Re:ZERO -Starting Life in Another World- Season 4", "Re Zero kara Hajimeru Isekai Seikatsu"]',
        total_episodes=19,
        next_airing_episode=19,
        next_airing_at=datetime.now(timezone.utc) + timedelta(days=1),
        last_confirmed_episode=84,
    )
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "absolute-78",
                "title": "[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 78 (1080p) [30D08902].mkv",
                "torrentURL": "magnet:absolute-78",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 18)).first()
    mapping = session.exec(select(EpisodeNumberMapping).where(EpisodeNumberMapping.monitored_id == show.id)).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.source_episode == 78
    assert mapping.offset == 60
    qbit.add_torrent.assert_called_once()
    session.close()
    engine.dispose()


def test_confirmed_rezero_offset_maps_feed_84_to_local_18():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO Season 4", "Re Zero kara Hajimeru Isekai Seikatsu"]',
        total_episodes=19,
        next_airing_episode=19,
        next_airing_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    session.add(EpisodeNumberMapping(monitored_id=show.id, feed_id=feed.id, offset=66, evidence_count=2))
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "raw-84",
                "title": "[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 84 (1080p).mkv",
                "torrentURL": "magnet:raw-84",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 18)
    ).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.source_episode == 84
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_direct_grab_recovers_when_hash_appears_after_restart():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, torrent = _qbit()
    visible = {"value": False}
    qbit.get_torrents.side_effect = lambda **kwargs: [torrent] if visible["value"] and (kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("tag") == "kisetsu-managed") else []
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    visible["value"] = True
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert episode.torrent_hash == "hash-1"
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_pinned_feed_is_strict_in_direct_mode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    other = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    pinned = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(other)
    session.add(pinned)
    # A pinned show is pinned to its current feed, and that pin is strict: the
    # higher-priority feed must not be considered at all.
    show = _show(session, current_feed_id=pinned.id, feed_pinned=True)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": other.qbit_feed_url, "articles": [{"id": "sub", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:sub"}]},
        "Erai": {"url": pinned.qbit_feed_url, "articles": [{"id": "erai", "title": "[Erai-raws] Frieren - 08 [1080p].mkv", "torrentURL": "magnet:erai"}]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [other, pinned], mode="direct")

    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:erai"
    assert show.current_feed_id == pinned.id
    session.close()
    engine.dispose()


def test_v2_add_is_paused_and_old_torrent_is_removed_only_after_new_one_completes():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    new_torrent.hash = "new-hash"
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent]
        if kwargs.get("tag", "").startswith("kisetsu-op-")
        else [new_torrent]
        if kwargs.get("hashes") == ["new-hash"]
        else [old_torrent]
        if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent]
        if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "recheck", "resume"]
    assert episode.version == 1
    assert episode.torrent_hash == "old-hash"
    assert episode.status == EpisodeStatus.REPLACING
    qbit.delete_torrents.assert_not_called()

    # The replacement is still downloading: the previous files must stay, no
    # matter how many cycles go by.
    for _ in range(4):
        evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "recheck", "resume"]
    assert episode.version == 1
    assert episode.torrent_hash == "old-hash"
    qbit.delete_torrents.assert_not_called()
    operation = session.exec(select(TorrentOperation)).first()
    assert operation.status == TorrentOperationStatus.SEEDING

    # Only once the replacement has completed is the superseded one removed.
    new_torrent.progress = 1
    new_torrent.state = "stalledUP"
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "recheck", "resume", "delete"]
    assert episode.version == 2
    assert episode.torrent_hash == "new-hash"
    assert episode.status == EpisodeStatus.COMPLETED
    qbit.delete_torrents.assert_called_once_with(["old-hash"], delete_files=True)
    session.close()
    engine.dispose()


def test_stopped_seeding_torrent_is_completed_without_regrab():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, torrent = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    torrent.progress = 1
    torrent.state = "stoppedUP"
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.COMPLETED
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_missing_accepted_torrent_is_requeued_without_false_completion():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    qbit.get_torrents.side_effect = lambda **kwargs: []
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    assert episode.status == EpisodeStatus.WANTED
    assert episode.retry_after is not None
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_ambiguous_direct_add_failure_remains_retryable():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: []
    qbit.add_torrent.side_effect = RuntimeError("add failed")
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    operation = session.exec(select(TorrentOperation).where(TorrentOperation.episode_id == episode.id)).first()
    assert episode.status == EpisodeStatus.WANTED
    assert operation.status == TorrentOperationStatus.UNKNOWN
    assert operation.next_retry_at is not None
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_rejected_add_of_a_torrent_qbit_already_holds_is_adopted():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session)
    info_hash = "a" * 40
    present = MagicMock(hash=info_hash, progress=1, state="stoppedUP")
    present.name = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: [present] if kwargs.get("hashes") == [info_hash] else []
    qbit.add_torrent.side_effect = QbitClientError("qBittorrent rejected torrent: Fails.")
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": present.name, "torrentURL": f"magnet:?xt=urn:btih:{info_hash}"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)).first()
    operation = session.exec(select(TorrentOperation).where(TorrentOperation.episode_id == episode.id)).first()
    assert operation.new_torrent_hash == info_hash
    assert operation.status == TorrentOperationStatus.COMPLETED
    assert episode.torrent_hash == info_hash
    qbit.add_torrent_tags.assert_called_once()
    session.close()
    engine.dispose()


def test_v2_replacement_continues_when_v1_was_already_removed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    old_release = MagicMock(hash="old-hash", progress=1.0, state="uploading")
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent]
        if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"] or kwargs.get("tag") == "kisetsu-managed"
        else [old_release] if kwargs.get("hashes") == ["old-hash"] else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert events == ["add", "recheck", "resume"]
    assert episode.version == 1
    assert episode.torrent_hash == "old-hash"
    assert episode.status == EpisodeStatus.REPLACING
    qbit.delete_torrents.assert_not_called()
    session.close()
    engine.dispose()


def test_failed_v2_add_keeps_old_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    qbit, _ = _qbit(title="[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv")
    qbit.add_torrent.side_effect = RuntimeError("add failed")
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv", "torrentURL": "magnet:v2"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert episode.status == EpisodeStatus.COMPLETED
    assert episode.torrent_hash == "old-hash"
    assert qbit.add_torrent.call_count == 1
    qbit.delete_torrents.assert_not_called()
    session.close()
    engine.dispose()


def test_rules_transition_marks_owned_articles_read():
    engine, session = _database()
    settings = Settings(id=1, download_mode="rules")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id, total_episodes=1, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.release_title = "[Group] Sousou no Frieren - 01 (1080p).mkv"
    session.add(episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "ep1", "title": episode.release_title}]}
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = supervisor.shield_owned_articles()
    second_logs = supervisor.shield_owned_articles()

    assert any("previously downloaded RSS" in log for log in logs)
    assert second_logs == []
    qbit.mark_rss_article_read.assert_called_once_with("SubsPlease", "ep1")
    session.close()
    engine.dispose()


def test_shield_owned_articles_is_scoped_to_each_show():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    first = _show(session, id=1, anilist_id=1001, display_name="First Show", aliases_json='["First Show"]', current_feed_id=feed.id)
    second = _show(session, id=2, anilist_id=1002, display_name="Second Show", aliases_json='["Second Show"]', current_feed_id=feed.id)
    sync_show_episodes(session, first)
    sync_show_episodes(session, second)
    first_episode = session.exec(select(Episode).where(Episode.monitored_id == first.id, Episode.episode_number == 1)).first()
    second_episode = session.exec(select(Episode).where(Episode.monitored_id == second.id, Episode.episode_number == 1)).first()
    first_episode.status = EpisodeStatus.COMPLETED
    first_episode.release_title = "First release"
    second_episode.status = EpisodeStatus.COMPLETED
    second_episode.release_title = "Second release"
    session.add(first_episode)
    session.add(second_episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {"id": "first-item", "title": "First release"},
                {"id": "second-item", "title": "Second release"},
            ],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.shield_owned_articles()

    marked_ids = {call.args[1] for call in qbit.mark_rss_article_read.call_args_list}
    assert marked_ids == {"first-item", "second-item"}
    session.close()
    engine.dispose()


def test_shield_does_not_treat_hashless_queued_release_as_owned():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.QUEUED
    episode.release_title = "Queued but unconfirmed release"
    session.add(episode)
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feed_paths.return_value = {feed.qbit_feed_url: "SubsPlease"}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "queued-item", "title": episode.release_title}],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.shield_owned_articles()

    qbit.mark_rss_article_read.assert_not_called()
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_direct_mode_disables_managed_rules_without_creating_new_ones():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct", anilist_username="")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session, current_feed_id=feed.id, qbit_rule_name="[Seasonal] Sousou no Frieren")
    rules = {"[Seasonal] Sousou no Frieren": {"enabled": True}}
    qbit = MagicMock()
    qbit.get_rss_feeds_flat.return_value = [{"name": "SubsPlease", "url": feed.qbit_feed_url}]
    qbit.get_rss_rules.side_effect = lambda: dict(rules)
    qbit.set_rss_rule.side_effect = lambda name, definition: rules.__setitem__(name, definition)
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "isLoading": False,
            "hasError": False,
            "articles": [],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    await supervisor.run_full_cycle()

    assert qbit.set_rss_rule.call_count == 1
    assert qbit.set_rss_rule.call_args.args[1]["enabled"] is False
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_direct_preflight_disables_and_verifies_managed_rules():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    show = _show(session)
    show.qbit_rule_name = "[Seasonal] Sousou no Frieren"
    session.add(show)
    session.commit()
    qbit = MagicMock()
    rules = {"[Seasonal] Sousou no Frieren": {"enabled": True, "mustContain": "Frieren"}}
    qbit.get_rss_rules.return_value = rules
    qbit.set_rss_rule.side_effect = lambda name, rule_def: rules.__setitem__(name, rule_def)
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.disable_managed_rules()

    assert rules["[Seasonal] Sousou no Frieren"]["enabled"] is False
    assert qbit.set_rss_rule.call_count == 1
    session.close()
    engine.dispose()


def test_direct_preflight_blocks_when_rule_cannot_be_disabled():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    session.add(settings)
    qbit = MagicMock()
    qbit.get_rss_rules.return_value = {"[Seasonal] Show": {"enabled": True}}
    qbit.set_rss_rule.side_effect = RuntimeError("busy")
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    try:
        supervisor.disable_managed_rules()
    except QbitClientError:
        pass
    else:
        raise AssertionError("Expected direct ownership preflight to fail")
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_direct_cycle_keeps_rules_disabled():
    engine, session = _database()
    settings = Settings(
        id=1,
        default_category="Anime",
        base_dir="/tmp/Anime",
        download_mode="direct",
        anilist_username="",
    )
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session)
    qbit, torrent = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") else []
    )
    qbit.get_rss_feeds_flat.return_value = [{"name": "SubsPlease", "url": feed.qbit_feed_url}]
    qbit.get_rss_rules.return_value = {}
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    await supervisor.run_full_cycle()

    qbit.add_torrent.assert_called()
    qbit.refresh_rss_feeds.assert_not_called()
    qbit.set_rss_rule.assert_not_called()
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_forced_direct_cycle_refreshes_before_grabbing():
    engine, session = _database()
    settings = Settings(
        id=1,
        default_category="Anime",
        base_dir="/tmp/Anime",
        download_mode="direct",
        anilist_username="",
    )
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        status=MonitoredStatus.FIXED,
        current_feed_id=feed.id,
        last_confirmed_episode=7,
        next_airing_episode=9,
        next_airing_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    stale_tree = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "isLoading": False,
            "hasError": False,
            "articles": [],
        }
    }
    loading_tree = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "isLoading": True,
            "hasError": False,
            "articles": [],
        }
    }
    fresh_tree = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "isLoading": False,
            "hasError": False,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }
    events = []
    qbit, torrent = _qbit(events=events)
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") else []
    )
    qbit.get_rss_feeds_flat.return_value = [{"name": "SubsPlease", "url": feed.qbit_feed_url}]
    qbit.get_rss_rules.return_value = {}
    qbit.get_rss_items.side_effect = [stale_tree, loading_tree, fresh_tree]
    qbit.refresh_rss_feeds.side_effect = lambda: events.append("refresh") or True
    qbit.add_torrent.side_effect = lambda **kwargs: events.append("add") or True
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = await supervisor.run_full_cycle(force_rss_refresh=True)

    episode = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number == 8,
        )
    ).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert events == ["refresh", "add"]
    assert "Refreshed qBittorrent RSS feeds before direct evaluation." in logs
    assert qbit.get_rss_items.call_count == 3
    qbit.set_rss_rule.assert_not_called()
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_partial_feed_failure_still_uses_healthy_feed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", download_mode="direct", anilist_username="")
    healthy = Feed(id=1, qbit_feed_name="Healthy", qbit_feed_url="https://healthy.example/rss", priority=1)
    broken = Feed(id=2, qbit_feed_name="Broken", qbit_feed_url="https://broken.example/rss", priority=2)
    session.add(settings)
    session.add(healthy)
    session.add(broken)
    show = _show(session, current_feed_id=healthy.id)
    settled = {
        "Healthy": {
            "url": healthy.qbit_feed_url,
            "isLoading": False,
            "hasError": False,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv",
                "torrentURL": "magnet:ep8",
            }],
        },
        "Broken": {
            "url": broken.qbit_feed_url,
            "isLoading": False,
            "hasError": True,
            "articles": [],
        },
    }
    qbit, torrent = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") else []
    )
    qbit.get_rss_feeds_flat.return_value = [
        {"name": "Healthy", "url": healthy.qbit_feed_url},
        {"name": "Broken", "url": broken.qbit_feed_url},
    ]
    qbit.get_rss_rules.return_value = {}
    qbit.get_rss_items.return_value = settled
    qbit.refresh_rss_feeds.return_value = True
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = await supervisor.run_full_cycle(force_rss_refresh=True)

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert episode.status == EpisodeStatus.DOWNLOADING
    assert any("RSS feeds unavailable after refresh: Broken" in log for log in logs)
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_forced_direct_cycle_preserves_retry_when_preflight_fails():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct", anilist_username="")
    session.add(settings)
    qbit = MagicMock()
    qbit.get_rss_feeds_flat.return_value = []
    qbit.get_rss_rules.return_value = {"[Seasonal] Show": {"enabled": True}}
    qbit.set_rss_rule.side_effect = RuntimeError("busy")
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    with pytest.raises(QbitClientError):
        await supervisor.run_full_cycle(force_rss_refresh=True)

    qbit.refresh_rss_feeds.assert_not_called()
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_supervisor_ordinary_direct_cycle_also_fails_closed_on_preflight():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct", anilist_username="")
    session.add(settings)
    qbit = MagicMock()
    qbit.get_rss_feeds_flat.return_value = []
    qbit.get_rss_rules.return_value = {"[Seasonal] Show": {"enabled": True}}
    qbit.set_rss_rule.side_effect = RuntimeError("busy")
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    with pytest.raises(QbitClientError):
        await supervisor.run_full_cycle()

    assert qbit.get_rss_rules.call_count == 2
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_direct_preflight_disables_managed_rule_prefix():
    engine, session = _database()
    settings = Settings(id=1, download_mode="direct")
    session.add(settings)
    qbit = MagicMock()
    rules = {"[Seasonal] Managed Show": {"enabled": True}}
    qbit.get_rss_rules.side_effect = lambda: dict(rules)
    qbit.set_rss_rule.side_effect = lambda name, definition: rules.__setitem__(name, definition)
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.disable_managed_rules()

    assert rules["[Seasonal] Managed Show"]["enabled"] is False
    session.close()
    engine.dispose()


def test_unknown_air_date_only_takes_the_freshest_wanted_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=8, next_airing_episode=8, next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    sync_show_episodes(session, show)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {"id": "ep3", "title": "[SubsPlease] Sousou no Frieren - 03 (1080p).mkv", "torrentURL": "magnet:ep3"},
                {"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:ep8"},
            ],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    grabbed = session.exec(select(Episode).where(Episode.torrent_hash.is_not(None))).all()
    assert [episode.episode_number for episode in grabbed] == [8]
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_an_undated_release_does_not_fill_an_episode_from_two_months_ago():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct", backfill_window_days=14)
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=8, next_airing_episode=8, next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    episodes = sync_show_episodes(session, show)
    for episode in episodes:
        episode.air_at = datetime.now(timezone.utc) - timedelta(days=60)
    session.add_all(episodes)
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:ep8"}],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.call_count == 0
    session.close()
    engine.dispose()


def test_paused_show_still_finishes_a_replacement_already_in_qbittorrent():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"]
        else [old_torrent] if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent] if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}]}
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    operation = session.exec(select(TorrentOperation)).first()
    assert operation.status == TorrentOperationStatus.NEW_VERIFIED

    show.status = MonitoredStatus.PAUSED
    show.status_before_pause = MonitoredStatus.FIXED.value
    session.add(show)
    session.commit()
    new_torrent.progress = 1
    new_torrent.state = "stalledUP"

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    session.refresh(operation)
    session.refresh(episode)
    assert operation.status != TorrentOperationStatus.NEW_VERIFIED
    assert show.status == MonitoredStatus.PAUSED
    assert episode.torrent_hash == "new-hash"
    assert episode.status == EpisodeStatus.COMPLETED
    session.close()
    engine.dispose()


def test_completed_show_still_finishes_a_replacement_already_in_qbittorrent():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"]
        else [old_torrent] if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent] if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}]}
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    operation = session.exec(select(TorrentOperation)).first()

    show.status = MonitoredStatus.COMPLETED
    session.add(show)
    session.commit()
    new_torrent.progress = 1
    new_torrent.state = "stalledUP"

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    session.refresh(operation)
    assert operation.status != TorrentOperationStatus.NEW_VERIFIED
    assert show.status == MonitoredStatus.COMPLETED
    session.close()
    engine.dispose()


def test_cancel_episode_operations_restores_previous_release():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    events = []
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"]
        else [old_torrent] if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent] if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}]}
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    operation = session.exec(select(TorrentOperation)).first()

    canceled = cancel_episode_operations(session, qbit, show, episode, "Show paused.")

    assert canceled == 1
    session.refresh(operation)
    assert operation.status == TorrentOperationStatus.CANCELED
    assert operation.next_retry_at is None
    assert episode.torrent_hash == "old-hash"
    assert episode.version == 1
    qbit.delete_torrents.assert_called_once_with(["new-hash"], delete_files=True)
    session.close()
    engine.dispose()


def test_cancel_keeps_the_files_a_replacement_shares_with_the_previous_release():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title)
    new_torrent.hash = "new-hash"
    new_torrent.content_path = "/tmp/Anime/Frieren/episode.mkv"
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    old_torrent.content_path = "/tmp/Anime/Frieren/episode.mkv"
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"]
        else [old_torrent] if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent] if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}]}
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    cancel_episode_operations(session, qbit, show, episode, "Show paused.")

    qbit.delete_torrents.assert_called_once_with(["new-hash"], delete_files=False)
    session.close()
    engine.dispose()


def test_switching_to_rules_cancels_in_flight_replacements():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title)
    new_torrent.hash = "new-hash"
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title)
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") or kwargs.get("hashes") == ["new-hash"]
        else [old_torrent] if kwargs.get("hashes") == ["old-hash"]
        else [old_torrent, new_torrent] if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}]}
    }
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = supervisor.cancel_direct_operations()

    operation = session.exec(select(TorrentOperation)).first()
    assert operation.status == TorrentOperationStatus.CANCELED
    assert episode.torrent_hash == "old-hash"
    assert any("Canceled 1" in log for log in logs)
    session.close()
    engine.dispose()


def test_show_torrent_hashes_cover_episodes_and_active_operations():
    engine, session = _database()
    show = _show(session, total_episodes=3)
    sync_show_episodes(session, show)
    episodes = {e.episode_number: e for e in session.exec(select(Episode).where(Episode.monitored_id == show.id)).all()}
    episodes[1].torrent_hash = "hash-1"
    episodes[2].torrent_hash = "old-2"
    session.add(episodes[1])
    session.add(episodes[2])
    for number, status, new_hash in ((2, TorrentOperationStatus.NEW_VERIFIED, "new-2"), (3, TorrentOperationStatus.COMPLETED, "done-3")):
        session.add(TorrentOperation(
            episode_id=episodes[number].id, kind="replace", status=status, operation_tag=f"kisetsu-op-{number}",
            release_title="x", new_torrent_url="magnet:x", new_torrent_hash=new_hash, old_torrent_hash="old-2",
        ))
    session.commit()

    assert show_torrent_hashes(session, show) == ["hash-1", "new-2", "old-2"]
    session.close()
    engine.dispose()


def test_direct_imports_existing_qbit_torrents_before_deciding():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    other = Feed(id=2, qbit_feed_name="Other", qbit_feed_url="https://other.example/rss", priority=2)
    session.add(settings)
    session.add(feed)
    session.add(other)
    show = _show(session, current_feed_id=other.id)
    existing = MagicMock(hash="existing-hash", progress=1, state="stoppedUP")
    # ``name`` is a MagicMock constructor argument, so it has to be set afterwards.
    existing.name = "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv"
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: [existing] if kwargs.get("tag") == "kisetsu-managed" else []
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep8",
                "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [9A5C7E1B].mkv",
                "torrentURL": "magnet:ep8",
            }],
        }
    }
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    logs = supervisor.prepare_download_mode("direct")

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert episode.torrent_hash == "existing-hash"
    assert episode.status == EpisodeStatus.COMPLETED
    assert any("Imported 1 existing torrent" in log for log in logs)
    # The feed that carries the release is learned, not the show's current guess.
    assert episode.feed_id == feed.id
    session.refresh(show)
    assert show.learned_feed_id == feed.id
    assert show.current_feed_id == feed.id
    session.close()
    engine.dispose()


def test_import_of_a_release_no_feed_carries_never_locks_the_guessed_feed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    existing = MagicMock(hash="existing-hash", progress=1, state="stoppedUP")
    # ``name`` is a MagicMock constructor argument, so it has to be set afterwards.
    existing.name = "Sousou.no.Frieren.S01E08.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv"
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: [existing] if kwargs.get("tag") == "kisetsu-managed" else []
    qbit.get_rss_items.return_value = {"SubsPlease": {"url": feed.qbit_feed_url, "articles": []}}
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.import_existing_torrents()

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert episode.torrent_hash == "existing-hash"
    assert episode.feed_id is None
    session.refresh(show)
    assert show.learned_feed_id is None
    session.close()
    engine.dispose()


def test_import_without_a_category_does_not_adopt_unmanaged_torrents():
    engine, session = _database()
    settings = Settings(id=1, default_category="", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    unrelated = MagicMock(hash="unrelated-hash", progress=1, state="stoppedUP")
    unrelated.name = "Sousou.no.Frieren.S01E08.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv"
    qbit, _ = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: [] if kwargs.get("tag") == "kisetsu-managed" else [unrelated]
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.import_existing_torrents()

    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert episode is None or episode.torrent_hash is None
    session.close()
    engine.dispose()


def test_stuck_loading_feed_does_not_discard_healthy_feed_articles():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    healthy = Feed(id=1, qbit_feed_name="Healthy", qbit_feed_url="https://healthy.example/rss", priority=1)
    stuck = Feed(id=2, qbit_feed_name="Stuck", qbit_feed_url="https://stuck.example/rss", priority=2)
    session.add(settings)
    session.add(healthy)
    session.add(stuck)
    show = _show(session, current_feed_id=healthy.id)
    tree = {
        "Healthy": {
            "url": healthy.qbit_feed_url,
            "isLoading": False,
            "hasError": False,
            "articles": [{"id": "ep8", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", "torrentURL": "magnet:ep8"}],
        },
        "Stuck": {
            "url": stuck.qbit_feed_url,
            "isLoading": True,
            "hasError": False,
            "articles": [],
        },
    }
    qbit, torrent = _qbit()
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [torrent] if kwargs.get("tag", "").startswith("kisetsu-op-") else []
    )
    qbit.get_rss_items.return_value = tree
    qbit.refresh_rss_feeds.return_value = True
    snapshot = RssSnapshot(qbit)

    articles = snapshot.refresh(max_attempts=1, poll_interval_seconds=0, timeout_seconds=0)

    assert [item["id"] for item in articles[healthy.qbit_feed_url]] == ["ep8"]
    assert stuck.qbit_feed_url not in articles
    assert snapshot.failed_feed_names == ["Stuck"]

    evaluate_and_grab_releases(session, qbit, settings, [healthy, stuck], mode="direct", rss_snapshot=snapshot)
    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 8)
    ).first()
    assert episode.torrent_hash == "hash-1"
    session.close()
    engine.dispose()


SBR_ALIASES = [
    "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
    "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    "SBR",
    "JoJo's Bizarre Adventure: Part 7–Steel Ball Run",
]


def test_split_cour_show_rejects_bare_arc_title():
    from kisetsu.core.matching import match_release_to_show

    def decide(title, aliases):
        # The direct engine opts into arc enforcement: it chooses what to spend
        # bandwidth on, so it must not claim a previous cour's bare title.
        return match_release_to_show(title, aliases, ignore_arc_marker=False)[0]

    # The prior cour shares the same bare title, so it must not be claimed.
    assert decide(
        "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run - 01 (1080p) [A1B2C3].mkv",
        SBR_ALIASES,
    ) is False
    assert decide(
        "[Erai-raws] SBR - 01v2 [1080p].mkv",
        SBR_ALIASES,
    ) is False
    # Releases that name the cour are still matched.
    assert decide(
        "[SubsPlease] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE - 02 [1080p].mkv",
        SBR_ALIASES,
    ) is True
    assert decide(
        "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run 3rd STAGE - 02 [1080p].mkv",
        SBR_ALIASES,
    ) is True
    # A different cour of the same arc is still rejected.
    assert decide(
        "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run 1st STAGE - 02 [1080p].mkv",
        SBR_ALIASES,
    ) is False
    # Plain season markers keep working as before.
    assert decide(
        "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv",
        ["Sousou no Frieren", "Sousou no Frieren 2nd Season"],
    ) is True


def test_arc_enforcement_is_opt_in_for_interpretation_callers():
    from kisetsu.core.matching import match_release_to_show

    # Callers that only interpret an already-downloaded release (RSS rule mode)
    # keep the historical permissive fuzzy behaviour by default.
    bare = "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run - 01 (1080p) [A1B2C3].mkv"
    assert match_release_to_show(bare, SBR_ALIASES)[0] is True
    assert match_release_to_show(bare, SBR_ALIASES, ignore_arc_marker=False)[0] is False


def test_direct_does_not_queue_past_cour_release():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(
        session,
        display_name="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
        aliases_json=json.dumps(SBR_ALIASES),
        total_episodes=24,
        next_airing_episode=2,
        next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {
                    "id": "old",
                    "title": "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run - 01 (1080p) [A1B2C3].mkv",
                    "torrentURL": "magnet:old",
                },
                {
                    "id": "current",
                    "title": "[SubsPlease] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE - 02 (1080p).mkv",
                    "torrentURL": "magnet:current",
                },
            ],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    grabbed = session.exec(select(Episode).where(Episode.torrent_hash.is_not(None))).all()
    assert [episode.episode_number for episode in grabbed] == [2]
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_article_date_maps_to_nearest_scheduled_episode():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        display_name="Re:ZERO Season 4",
        aliases_json='["Re:ZERO Season 4", "Re Zero kara Hajimeru Isekai Seikatsu"]',
        total_episodes=25,
        next_airing_episode=26,
        next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    episodes = sync_show_episodes(session, show)
    anchor = datetime.now(timezone.utc) - timedelta(hours=2)
    for index, episode in enumerate(episodes, start=1):
        episode.air_at = anchor - timedelta(days=(len(episodes) - index))
        session.add(episode)
    session.commit()
    qbit, _ = _qbit()
    target = episodes[-1]
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "renumbered",
                "title": "[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 80 (1080p).mkv",
                "link": "https://subsplease.org/renumbered",
                "torrentURL": "magnet:renumbered",
                "pubDate": target.air_at.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    grabbed = session.exec(select(Episode).where(Episode.torrent_hash.is_not(None))).all()
    assert [episode.episode_number for episode in grabbed] == [target.episode_number]
    mapping = session.exec(select(EpisodeNumberMapping)).first()
    assert mapping is not None
    assert mapping.offset == 80 - target.episode_number
    session.close()
    engine.dispose()


def test_a_new_item_is_not_pinned_to_the_end_of_a_season_that_ended_long_ago():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=4, next_airing_episode=5, next_airing_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    episodes = sync_show_episodes(session, show)
    for episode in episodes:
        episode.air_at = datetime.now(timezone.utc) - timedelta(days=200)
        session.add(episode)
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "stale",
                "title": "[SubsPlease] Sousou no Frieren - 99 (1080p).mkv",
                "date": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                "torrentURL": "magnet:stale",
            }],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.call_count == 0
    session.close()
    engine.dispose()


def test_a_show_only_ever_downloads_from_its_assigned_feed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session, current_feed_id=subs.id)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    qbit.add_torrent.assert_not_called()
    assert show.current_feed_id == subs.id
    session.close()
    engine.dispose()


def test_the_first_feed_with_a_release_wins_and_becomes_the_assigned_feed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }

    # First sighting is on the #2 feed, so the adoption is only nominated: a
    # higher-ranked feed still gets the grace window to post the episode first.
    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    qbit.add_torrent.assert_not_called()
    assert show.current_feed_id is None
    assert show.learned_feed_id is None
    assert show.candidate_feed_id == erai.id
    assert show.candidate_feed_since is not None

    # Once the window has passed the same feed keeps holding it, so it is adopted
    # and the release is taken.
    show.candidate_feed_since = utc_now() - timedelta(seconds=FEED_DISCOVERY_GRACE_SECONDS + 30)
    session.add(show)
    session.commit()

    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:erai-ep8"
    assert show.current_feed_id == erai.id
    assert show.learned_feed_id == erai.id
    assert show.candidate_feed_id is None
    session.close()
    engine.dispose()


def test_a_release_on_the_top_feed_is_taken_without_waiting():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": [{
            "id": "subs-ep8",
            "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [A].mkv",
            "torrentURL": "magnet:subs-ep8",
        }]},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    # No grace window on the best-ranked feed, and nothing from the one behind it.
    assert qbit.add_torrent.call_count == 1
    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:subs-ep8"
    assert show.current_feed_id == subs.id
    assert show.learned_feed_id == subs.id
    session.close()
    engine.dispose()


def test_a_higher_ranked_feed_clears_a_pending_lower_ranked_candidate():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }
    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")
    assert show.candidate_feed_id == erai.id

    # The #1 feed catches up while the window is still open, so there is nothing
    # left to wait for.
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": [{
            "id": "subs-ep8",
            "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [A].mkv",
            "torrentURL": "magnet:subs-ep8",
        }]},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }
    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:subs-ep8"
    assert show.current_feed_id == subs.id
    assert show.candidate_feed_id is None
    session.close()
    engine.dispose()


def test_the_random_operation_tag_is_removed_once_the_hash_is_known():
    """It only exists to find the new torrent, which stops mattering once known."""
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session, current_feed_id=feed.id)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{
            "id": "a",
            "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [A].mkv",
            "torrentURL": "magnet:a",
        }]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    operation = session.exec(select(TorrentOperation)).first()
    added_tags = qbit.add_torrent.call_args.kwargs["tags"]
    assert any(tag.startswith("kisetsu-op-") for tag in added_tags)
    qbit.remove_torrent_tags.assert_called_once_with(["hash-1"], [operation.operation_tag])
    # The queried tags stay; only the per-operation one is dropped.
    assert "kisetsu-managed" in added_tags
    assert len(added_tags) == 2
    session.close()
    engine.dispose()


def test_leftover_operation_tags_are_swept_from_existing_torrents():
    """Older builds left the tag on every torrent; the cycle cleans that up once."""
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    qbit, torrent = _qbit()
    _qbit_title = str(torrent.name)
    torrent.state = "stalledUP"
    torrent.progress = 1.0
    session.add(Episode(
        monitored_id=show.id,
        episode_number=8,
        status=EpisodeStatus.COMPLETED,
        feed_id=feed.id,
        torrent_hash="hash-1",
        release_title=_qbit_title,
        operation_tag="kisetsu-op-834c6907af68485cabb2306495d634f9",
    ))
    session.commit()

    logs = update_episode_status(session, qbit, settings)

    qbit.remove_torrent_tags.assert_called_once_with(
        ["hash-1"], ["kisetsu-op-834c6907af68485cabb2306495d634f9"]
    )
    assert any("leftover operation tag" in line for line in logs)
    # Cleared, so the same episode is never swept again.
    session.expire_all()
    assert session.get(Episode, 1).operation_tag is None

    qbit.remove_torrent_tags.reset_mock()
    update_episode_status(session, qbit, settings)
    qbit.remove_torrent_tags.assert_not_called()
    session.close()
    engine.dispose()


def test_an_in_flight_operation_keeps_its_tag_for_lookup():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    qbit, torrent = _qbit()
    _qbit_title = str(torrent.name)
    session.add(Episode(
        monitored_id=show.id,
        episode_number=8,
        status=EpisodeStatus.QUEUED,
        feed_id=feed.id,
        torrent_hash="hash-1",
        release_title=_qbit_title,
        operation_tag="kisetsu-op-834c6907af68485cabb2306495d634f9",
    ))
    session.commit()

    update_episode_status(session, qbit, settings)

    # Still downloading, so the tag is the only handle on the new torrent.
    qbit.remove_torrent_tags.assert_not_called()
    session.expire_all()
    assert session.get(Episode, 1).operation_tag == "kisetsu-op-834c6907af68485cabb2306495d634f9"
    session.close()
    engine.dispose()


def test_a_failed_tag_removal_never_disturbs_the_download():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, current_feed_id=feed.id)
    qbit, torrent = _qbit()
    _qbit_title = str(torrent.name)
    torrent.state = "stalledUP"
    torrent.progress = 1.0
    session.add(Episode(
        monitored_id=show.id,
        episode_number=8,
        status=EpisodeStatus.COMPLETED,
        feed_id=feed.id,
        torrent_hash="hash-1",
        release_title=_qbit_title,
        operation_tag="kisetsu-op-834c6907af68485cabb2306495d634f9",
    ))
    session.commit()
    qbit.remove_torrent_tags.side_effect = QbitClientError("tag API unavailable")

    update_episode_status(session, qbit, settings)

    session.expire_all()
    episode = session.get(Episode, 1)
    assert episode.status == EpisodeStatus.COMPLETED
    assert episode.operation_tag is None
    session.close()
    engine.dispose()


def test_a_show_locked_to_a_delivering_feed_is_never_read_from_another():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session)
    show.learned_feed_id = erai.id
    session.add(show)
    session.commit()
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": [{
            "id": "subs-ep9",
            "title": "[SubsPlease] Sousou no Frieren - 09 (1080p) [A].mkv",
            "torrentURL": "magnet:subs-ep9",
        }]},
        "Erai": {"url": erai.qbit_feed_url, "articles": []},
    }

    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    # Even with no assignment at all, a learned feed narrows the read to itself.
    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_a_grab_makes_the_show_learn_the_series_name_not_the_filename():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(
        session,
        current_feed_id=feed.id,
        display_name="Blue Box Season 2",
        aliases_json='["Blue Box Season 2", "Ao no Hako Season 2"]',
        total_episodes=1,
        next_airing_episode=1,
    )
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": feed.qbit_feed_url, "articles": [{
            "id": "blue-ep1",
            "title": "[Varyg] Blue.Box.S02E01.Deja.Vu.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv",
            "torrentURL": "magnet:blue-ep1",
        }]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    # The raw filename carries this one episode's number, quality and group, so
    # learning it would build a pattern matching nothing else.
    assert show.matched_title == "Blue.Box"
    episode = session.exec(
        select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)
    ).first()
    assert episode.release_title.startswith("[Varyg] Blue.Box.S02E01")
    session.close()
    engine.dispose()


def test_two_releases_of_the_same_episode_download_only_once():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session, current_feed_id=feed.id)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [
                {"id": "a", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [A].mkv", "torrentURL": "magnet:a"},
                {"id": "b", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [B].mkv", "torrentURL": "magnet:b"},
                {"id": "c", "title": "[SubsPlease] Sousou no Frieren - 08 [1080p].mkv", "torrentURL": "magnet:c"},
            ],
        }
    }

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.call_count == 1
    assert qbit.add_torrent.call_args.kwargs["urls"] == "magnet:a"
    assert len(session.exec(select(TorrentOperation)).all()) == 1
    session.close()
    engine.dispose()


def test_a_refresh_carrying_v1_and_v2_grabs_only_the_newest():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    _show(session, current_feed_id=feed.id)
    qbit, _ = _qbit()
    articles = [
        {"id": "a", "title": "[SubsPlease] Sousou no Frieren - 08 (1080p) [A].mkv", "torrentURL": "magnet:a"},
        {"id": "b", "title": "[SubsPlease] Sousou no Frieren - 08v2 (1080p) [B].mkv", "torrentURL": "magnet:b"},
    ]
    qbit.get_rss_items.return_value = {"SubsPlease": {"url": feed.qbit_feed_url, "articles": articles}}

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    # Both are on the feed already, so only the newest version is worth adding.
    assert [c.kwargs["urls"] for c in qbit.add_torrent.call_args_list] == ["magnet:b"]
    session.close()
    engine.dispose()


def test_a_pinned_show_never_moves_off_its_feed():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    subs = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    erai = Feed(id=2, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=2)
    session.add(settings)
    session.add(subs)
    session.add(erai)
    show = _show(session, current_feed_id=subs.id, feed_pinned=True)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {"url": subs.qbit_feed_url, "articles": []},
        "Erai": {"url": erai.qbit_feed_url, "articles": [{
            "id": "erai-ep8",
            "title": "[Erai-raws] Sousou no Frieren - 08 [1080p].mkv",
            "torrentURL": "magnet:erai-ep8",
        }]},
    }

    evaluate_and_grab_releases(session, qbit, settings, [subs, erai], mode="direct")

    qbit.add_torrent.assert_not_called()
    assert show.current_feed_id == subs.id
    session.close()
    engine.dispose()


# Air times are relative to now: the grabber compares them with the real clock,
# so fixed dates would stop meaning "just aired" the day after they were written.
AIR = datetime.now(timezone.utc).replace(second=0, microsecond=0)
REAL_AIR = AIR - timedelta(hours=1)
EP9_TITLE = "[SubsPlease] Sousou no Frieren - 09 (1080p) [E9].mkv"


def _early_airing_show(session, anilist_offset_hours=1.0, tolerance=6):
    """A show whose ninth episode really aired an hour before AniList says."""
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(feed)
    show = _show(
        session,
        current_feed_id=feed.id,
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=9,
        last_confirmed_episode=8,
        next_airing_at=AIR + timedelta(hours=anilist_offset_hours - 1),
    )
    episodes = sync_show_episodes(session, show)
    for episode in episodes:
        episode.air_at = AIR + timedelta(hours=anilist_offset_hours - 1) - timedelta(days=7 * (9 - episode.episode_number))
        if episode.episode_number < 9:
            episode.status = EpisodeStatus.COMPLETED
        session.add(episode)
    episode9 = next(e for e in episodes if e.episode_number == 9)
    episode9.status = EpisodeStatus.WANTED
    session.commit()
    return show, feed, episode9


def _early_release_qbit(feed):
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "ep9",
                "title": EP9_TITLE,
                "torrentURL": "magnet:ep9",
                "date": format_datetime(REAL_AIR + timedelta(minutes=5)),
            }],
        }
    }
    return qbit


def test_a_release_that_lands_before_anilist_says_is_still_grabbed():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    show, feed, episode9 = _early_airing_show(session, anilist_offset_hours=1.0)
    qbit = _early_release_qbit(feed)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.called, "the release was available and must be taken"
    assert episode9.status == EpisodeStatus.DOWNLOADING
    assert episode9.torrent_hash is not None
    assert session.exec(select(TorrentOperation)).one().kind == "grab"
    session.close()
    engine.dispose()


def test_a_full_day_of_anilist_drift_still_gets_the_release():
    """AniList can be a whole day late, which no hour-scale tolerance would cover."""
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    show, feed, episode9 = _early_airing_show(session, anilist_offset_hours=26.0)
    qbit = _early_release_qbit(feed)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.called
    session.close()
    engine.dispose()


def test_a_release_before_the_season_premiere_is_not_grabbed():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(feed)
    _show(
        session,
        current_feed_id=feed.id,
        next_airing_episode=1,
        next_airing_at=AIR + timedelta(hours=12),
    )
    qbit = _early_release_qbit(feed)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def _article_qbit(feed, title, published):
    qbit, _ = _qbit()
    article = {"id": "item", "title": title, "torrentURL": "magnet:item"}
    if published is not None:
        article["date"] = format_datetime(published)
    qbit.get_rss_items.return_value = {"SubsPlease": {"url": feed.qbit_feed_url, "articles": [article]}}
    return qbit


def _grab_with_published(published, stale=False, title=EP9_TITLE):
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    show, feed, episode9 = _early_airing_show(session)
    if stale:
        show.schedule_stale = True
        session.add(show)
        session.commit()
    qbit = _article_qbit(feed, title, published)
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    grabbed = qbit.add_torrent.called
    session.close()
    engine.dispose()
    return grabbed


def test_a_release_of_an_earlier_season_is_not_grabbed_even_when_its_number_fits():
    # Published two years before episode 9 aired, but "09" fits the show.
    assert not _grab_with_published(REAL_AIR - timedelta(days=730))
    # Months early is still an earlier season, not drift.
    assert not _grab_with_published(REAL_AIR - timedelta(days=60))


def test_a_release_within_a_days_drift_of_the_air_time_is_grabbed():
    assert _grab_with_published(REAL_AIR - timedelta(hours=20))
    assert _grab_with_published(REAL_AIR + timedelta(hours=3))


def test_an_undated_release_is_not_judged_by_date():
    assert _grab_with_published(None)


def test_a_pending_schedule_sync_does_not_switch_the_date_check_off():
    assert not _grab_with_published(REAL_AIR - timedelta(days=730), stale=True)


def test_an_old_episode_of_the_season_is_still_taken_when_a_dated_release_turns_up():
    """There is no day limit: the whole season is eligible, as long as the release's date fits it."""
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    show, feed, episode9 = _early_airing_show(session, anilist_offset_hours=1.0)
    episode9.air_at = REAL_AIR - timedelta(days=30)
    session.add(episode9)
    session.commit()
    qbit = _early_release_qbit(feed)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.called
    session.close()
    engine.dispose()


PREMIERE_TITLE = "[SubsPlease] Ao Ashi S2 - 01 (1080p) [6DCF3E95].mkv"


def _premiere_release_qbit(feed, published_at, title=PREMIERE_TITLE):
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{
                "id": "premiere",
                "title": title,
                "torrentURL": "magnet:premiere",
                "pubDate": published_at.strftime("%a, %d %b %Y %H:%M:%S +0000"),
            }],
        }
    }
    return qbit


def _premiere_show(session, anilist_air, total=12):
    """A show about to premiere: AniList points at episode 1 with a future air time."""
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(feed)
    show = _show(
        session,
        display_name="Ao Ashi",
        aliases_json=json.dumps(["Ao Ashi Season 2", "Ao Ashi S2"]),
        current_feed_id=feed.id,
        status=MonitoredStatus.FIXED,
        total_episodes=total,
        next_airing_episode=1,
        next_airing_at=anilist_air,
    )
    episodes = sync_show_episodes(session, show)
    for episode in episodes:
        episode.air_at = anilist_air
        session.add(episode)
    session.commit()
    return show, feed, episodes


def test_a_premiere_that_lands_early_is_grabbed_inside_the_tolerance_window():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    now = utc_now()
    # The release is already out, but AniList still points three hours ahead.
    show, feed, episodes = _premiere_show(session, anilist_air=now + timedelta(hours=3))
    qbit = _premiere_release_qbit(feed, published_at=now - timedelta(hours=1))

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    episode1 = next(e for e in episodes if e.episode_number == 1)
    assert qbit.add_torrent.called, "the premiere is already published and must be taken"
    assert episode1.torrent_hash is not None
    session.close()
    engine.dispose()


def test_a_premiere_is_not_grabbed_before_the_tolerance_window_opens():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    now = utc_now()
    show, feed, episodes = _premiere_show(session, anilist_air=now + timedelta(hours=10))
    qbit = _premiere_release_qbit(feed, published_at=now - timedelta(hours=1))

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_zero_tolerance_keeps_a_premiere_strict():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=0,
    )
    session.add(settings)
    now = utc_now()
    show, feed, episodes = _premiere_show(session, anilist_air=now + timedelta(hours=3))
    qbit = _premiere_release_qbit(feed, published_at=now - timedelta(hours=1))

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    qbit.add_torrent.assert_not_called()
    session.close()
    engine.dispose()


def test_an_early_mid_season_release_is_grabbed():
    engine, session = _database()
    settings = Settings(
        id=1, default_category="Anime", base_dir="/tmp/Anime",
        download_mode="direct", early_air_tolerance_hours=6,
    )
    session.add(settings)
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(feed)
    now = utc_now()
    show = _show(
        session,
        current_feed_id=feed.id,
        status=MonitoredStatus.FIXED,
        total_episodes=12,
        next_airing_episode=5,
        next_airing_at=now + timedelta(hours=3),
    )
    episodes = sync_show_episodes(session, show)
    for episode in episodes:
        episode.air_at = now + timedelta(hours=3) - timedelta(days=7 * (5 - episode.episode_number))
        if episode.episode_number < 5:
            episode.status = EpisodeStatus.COMPLETED
        session.add(episode)
    session.commit()
    qbit = _premiere_release_qbit(
        feed,
        published_at=now - timedelta(hours=1),
        title="[SubsPlease] Sousou no Frieren - 05 (1080p) [F5].mkv",
    )

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert qbit.add_torrent.called
    session.close()
    engine.dispose()


def test_date_mapped_episode_picks_past_aired_episode_not_future():
    from kisetsu.core.grabber import _date_mapped_episode

    engine, session = _database()
    show = _show(
        session,
        display_name="Polar Opposites",
        total_episodes=13,
    )
    # Ep 11 aired Sep 15, Ep 12 is scheduled for Sep 18 (3 days later)
    ep11 = Episode(monitored_id=show.id, episode_number=11, status=EpisodeStatus.WANTED, air_at=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc))
    ep12 = Episode(monitored_id=show.id, episode_number=12, status=EpisodeStatus.WANTED, air_at=datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc))
    session.add(ep11)
    session.add(ep12)
    session.commit()

    # Release published on Sep 17 (2 days after ep 11, 1 day before ep 12)
    # In the old code, abs(Sep 18 - Sep 17) = 1 day < 2 days, so it erroneously picked Ep 12.
    article = {
        "title": "[SubsPlease] Polar Opposites - 23 (1080p).mkv",
        "date": "Thu, 17 Sep 2026 12:00:00 +0000",
    }
    mapped = _date_mapped_episode(session, show, raw_episode=23, target_count=13, article=article)
    # Must pick Ep 11, NOT the future-scheduled Ep 12
    assert mapped == 11

    session.close()
    engine.dispose()


def test_mapped_episode_rejects_when_offset_maps_beyond_latest_aired():
    from kisetsu.core.grabber import _mapped_episode

    engine, session = _database()
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(feed)
    show = _show(session, display_name="Polar Opposites", total_episodes=13)
    # Stored bad offset 11
    session.add(EpisodeNumberMapping(monitored_id=show.id, feed_id=feed.id, offset=11))
    session.commit()

    # Raw 24 with offset 11 would give 13, but latest_aired is only 12 -> must be rejected
    article = {"title": "[SubsPlease] Polar Opposites - 24 (1080p).mkv"}
    mapped = _mapped_episode(session, show, feed, raw_episode=24, latest_aired=12, target_count=13, article=article)
    assert mapped is None

    session.close()
    engine.dispose()


def test_initial_v2_release_is_grabbed_directly_without_v1():
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=12, next_airing_episode=1)
    episodes = sync_show_episodes(session, show)
    episode1 = next(e for e in episodes if e.episode_number == 1)
    assert episode1.status == EpisodeStatus.WANTED

    # Feed has v2 first (newer pubDate), then v1 (older pubDate)
    articles = [
        {"id": "v2", "title": "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [B].mkv", "torrentURL": "magnet:v2", "pubDate": format_datetime(AIR - timedelta(hours=2))},
        {"id": "v1", "title": "[SubsPlease] Sousou no Frieren - 01 (1080p) [A].mkv", "torrentURL": "magnet:v1", "pubDate": format_datetime(AIR - timedelta(days=1))},
    ]
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {"SubsPlease": {"url": feed.qbit_feed_url, "articles": articles}}

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    # Only the v2 torrent should be added, as a fresh "grab", never downloading v1
    assert [c.kwargs["urls"] for c in qbit.add_torrent.call_args_list] == ["magnet:v2"]
    ops = session.exec(select(TorrentOperation)).all()
    assert len(ops) == 1
    assert ops[0].kind == "grab"
    assert ops[0].version == 2
    assert episode1.version == 2

    session.close()
    engine.dispose()


def test_older_wanted_episode_not_in_feed_transitions_to_missed():
    from kisetsu.workers.scheduler import calculate_next_poll_interval

    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    now = datetime.now(timezone.utc)
    show = _show(
        session,
        display_name="Steel Ball Run",
        aliases_json='["Steel Ball Run"]',
        total_episodes=12,
        next_airing_episode=4,
        next_airing_at=now + timedelta(days=5),
        status=MonitoredStatus.FIXED,
        last_confirmed_episode=3,
    )
    # Episode 1 was wanted and aired in the past
    ep1 = Episode(monitored_id=show.id, episode_number=1, status=EpisodeStatus.WANTED, air_at=now - timedelta(days=9))
    ep2 = Episode(monitored_id=show.id, episode_number=2, status=EpisodeStatus.COMPLETED, air_at=now - timedelta(days=2))
    ep3 = Episode(monitored_id=show.id, episode_number=3, status=EpisodeStatus.COMPLETED, air_at=now + timedelta(days=5))
    session.add(ep1)
    session.add(ep2)
    session.add(ep3)
    session.commit()

    # Feed has articles, but none for ep 1
    articles = [
        {"id": "sbr3", "title": "[SubsPlease] Steel Ball Run - 03 (1080p).mkv", "torrentURL": "magnet:sbr3"},
    ]
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {"SubsPlease": {"url": feed.qbit_feed_url, "articles": articles}}

    logs = evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    session.refresh(ep1)
    assert ep1.status == EpisodeStatus.MISSED
    assert any("marked as missed" in l for l in logs)

    # Verify scheduler does not log backlog
    duration, reason = calculate_next_poll_interval(session, default_interval_seconds=21600, download_mode="direct")
    assert duration == 21600
    assert "Direct backlog" not in reason
    assert "direct ownership" in reason

    session.close()
    engine.dispose()



def _replace_scenario(session, events, old_path=None, new_path=None):
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, last_confirmed_episode=1)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 1
    episode.release_title = "[SubsPlease] Sousou no Frieren - 01 (1080p) [OLD].mkv"
    episode.torrent_hash = "old-hash"
    session.add(episode)
    session.commit()
    title = "[SubsPlease] Sousou no Frieren - 01v2 (1080p) [NEW].mkv"
    qbit, new_torrent = _qbit(title=title, events=events)
    new_torrent.hash = "new-hash"
    new_torrent.content_path = new_path
    old_torrent = MagicMock(hash="old-hash", progress=1, state="stoppedUP", name=episode.release_title, content_path=old_path)
    present = {"old-hash": old_torrent, "new-hash": new_torrent}
    qbit.delete_torrents.side_effect = lambda hashes, **kwargs: (
        events.append("delete"), [present.pop(h, None) for h in hashes]
    )
    qbit.get_torrents.side_effect = lambda **kwargs: (
        [new_torrent]
        if kwargs.get("tag", "").startswith("kisetsu-op-")
        else [present[h] for h in kwargs["hashes"] if h in present]
        if kwargs.get("hashes")
        else list(present.values())
        if kwargs.get("tag") == "kisetsu-managed"
        else []
    )
    qbit.get_rss_items.return_value = {
        "SubsPlease": {
            "url": feed.qbit_feed_url,
            "articles": [{"id": "v2", "title": title, "torrentURL": "magnet:v2"}],
        }
    }
    return settings, feed, show, episode, qbit, new_torrent


def test_import_never_rolls_back_a_superseded_release():
    engine, session = _database()
    events = []
    settings, feed, show, episode, qbit, new_torrent = _replace_scenario(session, events)
    qbit.delete_torrents.side_effect = lambda *args, **kwargs: None  # old torrent lingers
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    new_torrent.progress = 1
    new_torrent.state = "stalledUP"
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    assert episode.torrent_hash == "new-hash"

    for _ in range(3):
        supervisor.import_existing_torrents()
        evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    assert episode.torrent_hash == "new-hash"
    assert episode.version == 2
    assert qbit.add_torrent.call_count == 1
    session.close()
    engine.dispose()


def test_a_same_path_rerelease_removes_the_old_torrent_but_keeps_the_files():
    engine, session = _database()
    events = []
    path = "/tmp/Anime/Frieren/[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"
    settings, feed, show, episode, qbit, new_torrent = _replace_scenario(session, events, old_path=path, new_path=path)

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    new_torrent.progress = 1
    new_torrent.state = "stalledUP"
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")

    qbit.delete_torrents.assert_called_once_with(["old-hash"], delete_files=False)
    assert episode.torrent_hash == "new-hash"
    session.close()
    engine.dispose()


def test_giving_up_on_a_replacement_keeps_the_previous_release():
    engine, session = _database()
    events = []
    settings, feed, show, episode, qbit, new_torrent = _replace_scenario(session, events)
    # qBittorrent accepts the add but never exposes the new torrent; the previous one stays.
    old_release = MagicMock(hash="old-hash", progress=1.0, state="uploading")
    qbit.get_torrents.side_effect = lambda **kwargs: [old_release] if kwargs.get("hashes") == ["old-hash"] else []

    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    operation = session.exec(select(TorrentOperation)).first()
    operation.status = TorrentOperationStatus.RETRY_WAIT
    operation.attempt_count = 8
    operation.next_retry_at = utc_now() - timedelta(minutes=1)
    session.add(operation)
    session.commit()

    update_episode_status(session, qbit, settings)

    assert operation.status == TorrentOperationStatus.FAILED
    assert episode.status == EpisodeStatus.COMPLETED
    assert episode.torrent_hash == "old-hash"
    assert episode.version == 1
    session.close()
    engine.dispose()


def test_a_replacement_from_another_feed_becomes_the_episode_source():
    from kisetsu.core.grabber import _set_episode_release

    engine, session = _database()
    session.add(Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct"))
    old_feed = Feed(id=1, qbit_feed_name="Erai", qbit_feed_url="https://erai.example/rss", priority=1)
    new_feed = Feed(id=2, qbit_feed_name="Varyg", qbit_feed_url="https://varyg.example/rss", priority=2)
    session.add(old_feed)
    session.add(new_feed)
    show = _show(session, total_episodes=2, next_airing_episode=2)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    episode.status = EpisodeStatus.COMPLETED
    episode.feed_id = old_feed.id
    episode.torrent_hash = "old-hash"
    session.add(episode)
    operation = TorrentOperation(
        episode_id=episode.id, kind="replace", status=TorrentOperationStatus.NEW_VERIFIED,
        operation_tag="kisetsu-op-x", release_title="Frieren S01E01 Varyg", new_torrent_url="magnet:x",
        new_torrent_hash="new-hash", old_torrent_hash="old-hash", feed_id=new_feed.id, old_feed_id=old_feed.id,
    )
    session.add(operation)
    session.commit()

    _set_episode_release(session, episode, operation, "new-hash", EpisodeStatus.DOWNLOADING)

    assert episode.feed_id == new_feed.id
    session.close()
    engine.dispose()


def test_an_older_version_from_another_feed_is_not_offered_over_a_finished_repack():
    from kisetsu.core.grabber import direct_feed_matches

    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    feed = Feed(id=2, qbit_feed_name="Toonshub", qbit_feed_url="https://toons.example/rss", priority=1)
    session.add(settings)
    session.add(feed)
    show = _show(session, total_episodes=2, next_airing_episode=2, current_feed_id=feed.id)
    sync_show_episodes(session, show)
    episode = session.exec(select(Episode).where(Episode.monitored_id == show.id, Episode.episode_number == 1)).first()
    # The feed the repack came from is gone, so the ledger has no source feed.
    episode.status = EpisodeStatus.COMPLETED
    episode.version = 2
    episode.feed_id = None
    episode.torrent_hash = "repack-hash"
    episode.release_title = "Sousou.no.Frieren.S01E01.REPACK.1080p.WEB-DL-Group.mkv"
    session.add(episode)
    session.commit()
    qbit, _ = _qbit()
    articles = {feed.qbit_feed_url: [{
        "id": "v1", "title": "[SubsPlease] Sousou no Frieren - 01 (1080p) [AAAA1111].mkv", "torrentURL": "magnet:v1",
    }]}

    matches = direct_feed_matches(session, qbit, settings, show, articles_by_url=articles)

    assert matches and matches[0]["version"] == 1
    assert matches[0]["downloadable"] is False
    assert matches[0]["replaces"] is False
    session.close()
    engine.dispose()


def test_removing_a_feed_clears_it_from_the_episodes_it_supplied():
    engine, session = _database()
    # Installed databases added episodes.feed_id by migration, without the
    # foreign key, so nothing but the supervisor clears it there.
    session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    gone = Feed(id=1, qbit_feed_name="Old", qbit_feed_url="https://old.example/rss", priority=1)
    kept = Feed(id=2, qbit_feed_name="Kept", qbit_feed_url="https://kept.example/rss", priority=2)
    session.add(settings)
    session.add(gone)
    session.add(kept)
    show = _show(session, total_episodes=3, next_airing_episode=3)
    sync_show_episodes(session, show)
    episodes = {e.episode_number: e for e in session.exec(select(Episode).where(Episode.monitored_id == show.id)).all()}
    episodes[1].feed_id = gone.id
    episodes[2].feed_id = kept.id
    session.add(episodes[1])
    session.add(episodes[2])
    session.commit()
    qbit = MagicMock()
    qbit.get_rss_feeds_flat.return_value = [{"name": "Kept", "url": kept.qbit_feed_url}]
    supervisor = Supervisor(session=session, qbit=qbit, anilist=MagicMock(), settings=settings)

    supervisor.sync_feeds()

    session.refresh(episodes[1])
    session.refresh(episodes[2])
    assert episodes[1].feed_id is None
    assert episodes[2].feed_id == kept.id
    session.close()
    engine.dispose()


STAGE_ALIASES = [
    "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
    "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    "JoJo's Bizarre Adventure: Part 7–Steel Ball Run",
]


def _stage_show(session):
    """A split entry whose aliases insist on an arc marker the release groups never write."""
    feed = Feed(id=1, qbit_feed_name="Erai", qbit_feed_url="https://nyaa.si/?page=rss&u=Erai-raws", priority=1)
    session.add(feed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    show = _show(
        session,
        display_name="Steel Ball Run",
        aliases_json=json.dumps(STAGE_ALIASES),
        current_feed_id=1,
        status=MonitoredStatus.UNCONFIRMED,
        total_episodes=11,
        next_airing_episode=4,
        next_airing_at=now + timedelta(days=6),
    )
    episodes = sync_show_episodes(session, show)
    air = {number: now - timedelta(days=7 * (4 - number), hours=1) for number in range(1, 12)}
    for episode in episodes:
        episode.air_at = air[episode.episode_number]
        session.add(episode)
    session.commit()
    return feed, air


def _stage_grab(articles):
    engine, session = _database()
    settings = Settings(id=1, default_category="Anime", base_dir="/tmp/Anime", download_mode="direct")
    session.add(settings)
    feed, air = _stage_show(session)
    qbit, _ = _qbit()
    qbit.get_rss_items.return_value = {"Erai": {"url": feed.qbit_feed_url, "articles": articles(air)}}
    evaluate_and_grab_releases(session, qbit, settings, [feed], mode="direct")
    urls = [call.kwargs.get("urls") for call in qbit.add_torrent.call_args_list]
    session.close()
    engine.dispose()
    return urls


def _erai(title, published, item_id):
    return {
        "id": item_id, "title": f"[Erai-raws] {title} [1080p NF WEB-DL AVC AAC][MultiSub]",
        "torrentURL": f"magnet:{item_id}", "date": format_datetime(published),
    }


def test_a_release_on_the_shows_schedule_needs_no_arc_marker():
    urls = _stage_grab(lambda air: [
        _erai("JoJo no Kimyou na Bouken: Steel Ball Run - 04", air[3] + timedelta(minutes=11), "on-schedule"),
    ])
    assert urls == ["magnet:on-schedule"]


def test_a_release_off_the_schedule_or_naming_another_stage_still_needs_its_marker():
    urls = _stage_grab(lambda air: [
        # The previous stage's premiere: a week before this entry began.
        _erai("JoJo no Kimyou na Bouken: Steel Ball Run - 01", air[1] - timedelta(days=7), "previous-stage"),
        # On schedule, but it says it is another stage.
        _erai("JoJo no Kimyou na Bouken: Steel Ball Run 1st Stage - 04", air[3] + timedelta(minutes=11), "other-stage"),
        # On schedule but undated.
        {"id": "undated", "title": "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 04 [1080p]", "torrentURL": "magnet:undated"},
    ])
    assert urls == []


