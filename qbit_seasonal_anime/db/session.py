import os
from pathlib import Path
from typing import Optional
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select
from qbit_seasonal_anime.config import DB_PATH, CONFIG_DIR
from qbit_seasonal_anime.db.models import Settings, Feed, Monitored, RuleHistory, MatchHistory, EpisodeNumberMapping

_engine = None


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

        settings_cols = get_table_columns(session, "settings")
        if "title_language" not in settings_cols:
            session.exec(text("ALTER TABLE settings ADD COLUMN title_language VARCHAR DEFAULT 'english'"))
            session.commit()

        history_cols = get_table_columns(session, "match_history")
        if "matched_regex" not in history_cols:
            session.exec(text("ALTER TABLE match_history ADD COLUMN matched_regex VARCHAR"))
            session.commit()

        for index_name, table_name, columns in [
            ("ix_monitored_current_feed_id", "monitored", "current_feed_id"),
            ("ix_monitored_status_current_feed", "monitored", "status, current_feed_id"),
            ("ix_rule_history_feed_id", "rule_history", "feed_id"),
            ("ix_rule_history_created_at", "rule_history", "created_at"),
            ("ix_rule_history_monitored_created", "rule_history", "monitored_id, created_at DESC"),
            ("ix_rule_history_monitored_outcome_feed", "rule_history", "monitored_id, outcome, feed_id"),
        ]:
            session.exec(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({columns})"))
        session.commit()

        # Builds before the feed offset existed stored raw feed episode numbers in
        # these columns, so they read above total_episodes. The mapping is the only
        # thing that can translate them back.
        shows_to_check = session.exec(
            select(Monitored).where(Monitored.total_episodes.is_not(None))
        ).all()
        for show in shows_to_check:
            if not show.total_episodes or not show.current_feed_id:
                continue
            mapping = session.exec(
                select(EpisodeNumberMapping).where(
                    EpisodeNumberMapping.monitored_id == show.id,
                    EpisodeNumberMapping.feed_id == show.current_feed_id,
                )
            ).first()
            if not mapping or not mapping.offset:
                continue
            offset = mapping.offset
            if show.last_confirmed_episode and show.last_confirmed_episode > show.total_episodes:
                canonical = show.last_confirmed_episode - offset
                if 1 <= canonical <= show.total_episodes:
                    show.last_confirmed_episode = canonical
                    session.add(show)
            for history in session.exec(
                select(MatchHistory).where(
                    MatchHistory.monitored_id == show.id,
                    MatchHistory.episode.is_not(None),
                )
            ).all():
                if history.episode > show.total_episodes:
                    history.episode = history.episode - offset
                    session.add(history)
        session.commit()

        stmt = select(Settings)
        settings = session.exec(stmt).first()
        if not settings:
            settings = Settings()
            session.add(settings)
            session.commit()


def get_settings(session: Session) -> Settings:
    stmt = select(Settings)
    settings = session.exec(stmt).first()
    if not settings:
        settings = Settings()
        session.add(settings)
        session.commit()
        session.refresh(settings)
    return settings
