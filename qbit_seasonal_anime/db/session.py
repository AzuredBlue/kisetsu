import logging
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.config import DB_PATH, CONFIG_DIR
from qbit_seasonal_anime.db.models import ACTIVE_OPERATION_STATUSES, Episode, EpisodeMappingSource, EpisodeNumberMapping, EpisodeStatus, Monitored, MonitoredStatus, Settings, TorrentOperation, normalize_mapping_source

_engine = None
SCHEMA_VERSION = 1


def _database_path(engine: Engine) -> Optional[Path]:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _backup_database(engine: Engine) -> Optional[Path]:
    path = _database_path(engine)
    if not path or not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.{stamp}.bak")
    with sqlite3.connect(path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    os.chmod(backup_path, 0o600)
    return backup_path


EPISODE_LEAD_TOLERANCE = timedelta(hours=6)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _offset_contradicts_air_schedule(
    episodes: List[Episode],
    candidate_offset: int,
) -> Optional[str]:
    """Reason ``candidate_offset`` puts a release before its episode aired, else None.

    Episodes without both an air date and a download timestamp are not judged.
    """
    for episode in episodes:
        if not episode.air_at or not episode.downloaded_at or episode.source_episode is None:
            continue
        if int(episode.source_episode) - episode.episode_number != candidate_offset:
            continue
        if _as_utc(episode.air_at) > _as_utc(episode.downloaded_at) + EPISODE_LEAD_TOLERANCE:
            return (
                f"release {episode.source_episode} was recorded for episode "
                f"{episode.episode_number} at {_as_utc(episode.downloaded_at).isoformat()}, "
                f"before it aired at {_as_utc(episode.air_at).isoformat()}"
            )
    return None


def _repair_legacy_episode_state(session: Session) -> None:
    from qbit_seasonal_anime.core.matching import parse_release_title

    shows = {show.id: show for show in session.exec(select(Monitored)).all()}
    episodes = session.exec(select(Episode)).all()
    evidence = defaultdict(list)
    for episode in episodes:
        show = shows.get(episode.monitored_id)
        if not show or not episode.release_title:
            continue
        parsed = parse_release_title(episode.release_title)
        raw_episode = parsed.get("episode")
        if raw_episode is None:
            continue
        raw_episode = int(raw_episode)
        episode.source_episode = raw_episode
        feed_id = episode.feed_id or show.current_feed_id
        if feed_id:
            evidence[(show.id, feed_id)].append((raw_episode - episode.episode_number, raw_episode, episode.episode_number))
        session.add(episode)
        operations = session.exec(
            select(TorrentOperation).where(TorrentOperation.episode_id == episode.id)
        ).all()
        for operation in operations:
            operation.source_episode = raw_episode
            operation.feed_id = operation.feed_id or feed_id
            session.add(operation)

    for (show_id, feed_id), entries in evidence.items():
        mapping = session.exec(
            select(EpisodeNumberMapping).where(
                EpisodeNumberMapping.monitored_id == show_id,
                EpisodeNumberMapping.feed_id == feed_id,
            )
        ).first()
        if mapping is None:
            continue
        if normalize_mapping_source(mapping.source) == EpisodeMappingSource.MANUAL.name:
            continue
        counts = Counter(offset for offset, _, _ in entries)
        newest_entries = sorted(entries, key=lambda item: item[2], reverse=True)
        candidate_offset = newest_entries[0][0]

        # The ledger was written under this mapping, so re-deriving the offset
        # from it only reproduces its current value. AniList's aired schedule is
        # the one source that can judge it independently.
        show = shows.get(show_id)
        judged = [
            episode
            for episode in episodes
            if episode.monitored_id == show_id
            and (episode.feed_id or (show.current_feed_id if show else None)) == feed_id
        ]
        contradiction = _offset_contradicts_air_schedule(judged, candidate_offset)
        if contradiction:
            logging.getLogger("qbit_seasonal_anime.db.session").warning(
                f"Keeping stored episode offset {mapping.offset} for "
                f"'{show.display_name if show else show_id}' on feed {feed_id}: offset "
                f"{candidate_offset} implied by the episode ledger contradicts the "
                f"aired schedule ({contradiction})."
            )
            if normalize_mapping_source(mapping.source) != EpisodeMappingSource.LEGACY.name:
                mapping.source = EpisodeMappingSource.LEGACY.name
                session.add(mapping)
            continue

        count = counts[candidate_offset]
        newest_offsets = [entry[0] for entry in newest_entries[:2]]
        confirmed = len(newest_offsets) == 2 and newest_offsets[0] == newest_offsets[1]
        mapping.offset = candidate_offset
        mapping.evidence_count = max(mapping.evidence_count, count)
        mapping.source = (
            EpisodeMappingSource.CONFIRMED.name
            if confirmed
            else EpisodeMappingSource.LEGACY.name
        )
        mapping.last_evidence_at = datetime.now(timezone.utc)
        session.add(mapping)

    for show in shows.values():
        if show.status == MonitoredStatus.COMPLETED:
            air_at = show.next_airing_at
            if air_at and air_at.tzinfo is None:
                air_at = air_at.replace(tzinfo=timezone.utc)
            if air_at is not None and air_at > datetime.now(timezone.utc):
                show.schedule_stale = True
            session.add(show)
            continue
        show_episodes = [episode for episode in episodes if episode.monitored_id == show.id]
        scalar_conflict = bool(
            show.total_episodes
            and show.last_confirmed_episode
            and show.last_confirmed_episode > show.total_episodes
        )
        for episode in show_episodes:
            if (
                scalar_conflict
                and episode.status == EpisodeStatus.COMPLETED
                and not episode.release_title
                and not episode.torrent_hash
                and not episode.downloaded_at
            ):
                episode.status = EpisodeStatus.WANTED
                episode.last_error = "Legacy completion lacked canonical episode evidence."
                session.add(episode)
        completed = [episode for episode in show_episodes if episode.status == EpisodeStatus.COMPLETED]
        if completed:
            show.last_confirmed_episode = max(episode.episode_number for episode in completed)
            session.add(show)
        elif scalar_conflict:
            mapping = session.exec(
                select(EpisodeNumberMapping).where(
                    EpisodeNumberMapping.monitored_id == show.id,
                    EpisodeNumberMapping.feed_id == show.current_feed_id,
                )
            ).first() if show.current_feed_id else None
            if mapping and mapping.offset:
                canonical = show.last_confirmed_episode - mapping.offset
                if 1 <= canonical <= show.total_episodes:
                    show.last_confirmed_episode = canonical
                    session.add(show)
    session.commit()


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ensure SQLite enforces foreign key constraints."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_db_path() -> Path:
    return DB_PATH


def get_engine(db_path: Optional[Path] = None):
    global _engine
    if db_path is not None:
        target_path = db_path
        engine = create_engine(f"sqlite:///{target_path}", connect_args={"check_same_thread": False})
        return engine

    if _engine is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass

        _engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

        if DB_PATH.exists():
            try:
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass

    return _engine


def init_db(engine=None):
    if engine is None:
        engine = get_engine()
    database_path = _database_path(engine)
    needs_migration = False
    if database_path and database_path.exists():
        with engine.connect() as connection:
            schema_version = int(connection.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if schema_version < SCHEMA_VERSION:
            needs_migration = True
            _backup_database(engine)
    SQLModel.metadata.create_all(engine)
    def get_table_columns(session: Session, table: str) -> set:
        return {r[1] for r in session.exec(text(f"PRAGMA table_info({table})")).all()}

    with Session(engine) as session:
        monitored_cols = get_table_columns(session, "monitored")
        for col_name, col_type in [
            ("matched_title", "VARCHAR"),
            ("matched_release_group", "VARCHAR"),
            ("cover_image", "VARCHAR"),
            ("season_name", "VARCHAR"),
            ("season_year", "INTEGER"),
            ("title_romaji", "VARCHAR"),
            ("title_english", "VARCHAR"),
            ("status_before_pause", "VARCHAR"),
            ("custom_regex", "VARCHAR"),
            ("custom_must_not", "VARCHAR"),
            ("feed_pinned", "BOOLEAN"),
            ("candidate_feed_id", "INTEGER"),
            ("candidate_feed_since", "DATETIME"),
            ("custom_aliases_json", "VARCHAR"),
        ]:
            if col_name not in monitored_cols:
                session.exec(text(f"ALTER TABLE monitored ADD COLUMN {col_name} {col_type}"))
                session.commit()

        if "anilist_status" not in monitored_cols:
            session.exec(text("ALTER TABLE monitored ADD COLUMN anilist_status VARCHAR"))
            session.commit()
        if "schedule_synced_at" not in monitored_cols:
            session.exec(text("ALTER TABLE monitored ADD COLUMN schedule_synced_at TIMESTAMP"))
            session.commit()
        if "schedule_stale" not in monitored_cols:
            session.exec(text("ALTER TABLE monitored ADD COLUMN schedule_stale BOOLEAN DEFAULT 0"))
            session.commit()

        if "pinned_feed_id" in monitored_cols:
            session.exec(text(
                "UPDATE monitored SET feed_pinned = 1, current_feed_id = pinned_feed_id "
                "WHERE pinned_feed_id IS NOT NULL AND (feed_pinned IS NULL OR feed_pinned = 0)"
            ))
            session.commit()

        settings_cols = get_table_columns(session, "settings")
        if "title_language" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN title_language VARCHAR DEFAULT 'english'"))
            session.commit()
        if "download_mode" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN download_mode VARCHAR DEFAULT 'rules'"))
            session.commit()
        if "backfill_window_days" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN backfill_window_days INTEGER DEFAULT 14"))
            session.commit()
        if "early_air_tolerance_hours" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN early_air_tolerance_hours INTEGER DEFAULT 6"))
            session.commit()

        history_cols = get_table_columns(session, "match_history")
        if "matched_regex" not in history_cols:
            session.exec(text("ALTER TABLE match_history ADD COLUMN matched_regex VARCHAR"))
            session.commit()

        episode_cols = get_table_columns(session, "episodes")
        for col_name, col_type, default in [
            ("status", "VARCHAR", "'wanted'"),
            ("version", "INTEGER", "1"),
            ("release_title", "VARCHAR", None),
            ("release_group", "VARCHAR", None),
            ("feed_id", "INTEGER", None),
            ("feed_item_id", "VARCHAR", None),
            ("torrent_url", "VARCHAR", None),
            ("torrent_hash", "VARCHAR", None),
            ("operation_tag", "VARCHAR", None),
            ("downloaded_at", "TIMESTAMP", None),
            ("last_error", "VARCHAR", None),
            ("retry_after", "TIMESTAMP", None),
            ("air_at", "TIMESTAMP", None),
            ("schedule_state", "VARCHAR", "'unknown'"),
            ("source_episode", "INTEGER", None),
            ("last_attempt_at", "TIMESTAMP", None),
            ("attempt_count", "INTEGER", "0"),
        ]:
            if col_name not in episode_cols:
                suffix = f" DEFAULT {default}" if default else ""
                session.exec(text(f"ALTER TABLE episodes ADD COLUMN {col_name} {col_type}{suffix}"))
                session.commit()

        operation_cols = get_table_columns(session, "torrent_operations")
        for col_name, col_type, default in [
            ("old_release_title", "VARCHAR", None),
            ("old_release_group", "VARCHAR", None),
            ("old_torrent_url", "VARCHAR", None),
            ("old_feed_id", "INTEGER", None),
            ("old_version", "INTEGER", "1"),
            ("old_episode_status", "VARCHAR", "'wanted'"),
            ("old_operation_tag", "VARCHAR", None),
            ("source_episode", "INTEGER", None),
            ("feed_item_id", "VARCHAR", None),
            ("feed_id", "INTEGER", None),
            ("attempt_count", "INTEGER", "1"),
            ("last_attempt_at", "TIMESTAMP", None),
            ("next_retry_at", "TIMESTAMP", None),
            ("ambiguous", "BOOLEAN", "0"),
        ]:
            if col_name not in operation_cols:
                suffix = f" DEFAULT {default}" if default else ""
                session.exec(text(f"ALTER TABLE torrent_operations ADD COLUMN {col_name} {col_type}{suffix}"))
                session.commit()

        seen_cols = get_table_columns(session, "seen_feed_items")
        for col_name, col_type in [
            ("title", "VARCHAR DEFAULT ''"),
            ("created_at", "TIMESTAMP"),
            ("shielded_at", "TIMESTAMP"),
        ]:
            if col_name not in seen_cols:
                session.exec(text(f"ALTER TABLE seen_feed_items ADD COLUMN {col_name} {col_type}"))
                session.commit()

        session.exec(text("UPDATE seen_feed_items SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
        session.exec(text("DELETE FROM episodes WHERE id NOT IN (SELECT MIN(id) FROM episodes GROUP BY monitored_id, episode_number)"))
        session.exec(text("DELETE FROM seen_feed_items WHERE id NOT IN (SELECT MIN(id) FROM seen_feed_items GROUP BY feed_url, item_id)"))
        session.exec(text("DELETE FROM episode_number_mappings WHERE id NOT IN (SELECT MIN(id) FROM episode_number_mappings GROUP BY monitored_id, feed_id)"))
        # Enums are persisted by name, so every status literal must be uppercase
        # for the predicate to match rows at all.
        active_statuses = ", ".join(f"'{status}'" for status in ACTIVE_OPERATION_STATUSES)
        session.exec(text(
            "UPDATE torrent_operations SET status = 'CANCELED', "
            "last_error = 'Superseded duplicate in-flight operation.', next_retry_at = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            f"WHERE status IN ({active_statuses}) AND id NOT IN (SELECT MAX(id) FROM torrent_operations "
            f"WHERE status IN ({active_statuses}) GROUP BY episode_id)"
        ))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_episode_monitored_number ON episodes (monitored_id, episode_number)"))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_seen_feed_item ON seen_feed_items (feed_url, item_id)"))
        session.exec(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_episode_mapping_show_feed ON episode_number_mappings (monitored_id, feed_id)"))
        session.exec(text("DROP INDEX IF EXISTS ux_torrent_operation_active_episode"))
        session.exec(text(
            "UPDATE torrent_operations SET status = 'OLD_STOPPED' WHERE status = 'OLD_REMOVED'"
        ))

        for index_name, table_name, columns in [
            ("ix_monitored_current_feed_id", "monitored", "current_feed_id"),
            ("ix_monitored_status_current_feed", "monitored", "status, current_feed_id"),
            ("ix_episode_status_version", "episodes", "status, version"),
            ("ix_episode_air_at", "episodes", "air_at"),
            ("ix_episode_source_number", "episodes", "monitored_id, feed_id, source_episode"),
            ("ix_seen_feed_item_shielded", "seen_feed_items", "shielded_at"),
            ("ix_torrent_operation_status_updated", "torrent_operations", "status, updated_at"),
            ("ix_grab_decision_episode_created", "grab_decisions", "episode, created_at"),
            ("ix_grab_decision_item", "grab_decisions", "feed_url, feed_item_id"),
            ("ix_rule_history_feed_id", "rule_history", "feed_id"),
            ("ix_rule_history_created_at", "rule_history", "created_at"),
            ("ix_rule_history_monitored_created", "rule_history", "monitored_id, created_at DESC"),
            ("ix_rule_history_monitored_outcome_feed", "rule_history", "monitored_id, outcome, feed_id"),
        ]:
            session.exec(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})"))
        session.exec(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_torrent_operation_active_episode "
            f"ON torrent_operations(episode_id) WHERE status IN ({active_statuses})"
        ))
        session.commit()

        if needs_migration:
            _repair_legacy_episode_state(session)
            session.exec(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
            session.commit()

        stmt = select(Settings)
        settings = session.exec(stmt).first()
        if not settings:
            settings = Settings()
            session.add(settings)
            session.commit()


def acquire_supervision_lease(engine: Engine, owner: str, stale_after_minutes: int = 60) -> bool:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(minutes=max(1, stale_after_minutes))
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT owner, heartbeat_at FROM supervision_leases WHERE id = 1")
            ).mappings().first()
            if row and row["owner"] != owner and row["heartbeat_at"] > cutoff:
                return False
            if row:
                connection.execute(
                    text("UPDATE supervision_leases SET owner = :owner, acquired_at = :now, heartbeat_at = :now WHERE id = 1"),
                    {"owner": owner, "now": now},
                )
            else:
                connection.execute(
                    text("INSERT INTO supervision_leases (id, owner, acquired_at, heartbeat_at) VALUES (1, :owner, :now, :now)"),
                    {"owner": owner, "now": now},
                )
        return True
    except Exception:
        return False


def heartbeat_supervision_lease(engine: Engine, owner: str) -> bool:
    """Refresh the lease so a long cycle is not treated as abandoned."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text("UPDATE supervision_leases SET heartbeat_at = :now WHERE id = 1 AND owner = :owner"),
                {"owner": owner, "now": now},
            )
        return bool(result.rowcount)
    except Exception:
        return False


def release_supervision_lease(engine: Engine, owner: str) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM supervision_leases WHERE id = 1 AND owner = :owner"),
                {"owner": owner},
            )
    except Exception as e:
        logging.getLogger("qbit_seasonal_anime.db.session").warning(
            f"Could not release supervision lease: {e}"
        )


def get_settings(session: Session) -> Settings:
    stmt = select(Settings)
    settings = session.exec(stmt).first()
    if not settings:
        settings = Settings()
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings
