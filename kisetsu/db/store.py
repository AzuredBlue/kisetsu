import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from kisetsu.config import CONFIG_DIR, DB_PATH
from kisetsu.db.migrations import migrate

_engine: Optional[Engine] = None


def make_engine(path: Optional[Path] = None) -> Engine:
    """An engine for ``path``, or an in-memory database when ``path`` is None."""
    if path is None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: sqlite3.Connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def get_engine() -> Engine:
    """The process-wide engine for the configured database, created and migrated on first use."""
    global _engine
    if _engine is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CONFIG_DIR, 0o700)
        except OSError:
            pass
        _engine = make_engine(DB_PATH)
        migrate(_engine, DB_PATH)
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    return _engine


def close_engine() -> None:
    """Fold the WAL back into the database file and drop the engine."""
    global _engine
    if _engine is None:
        return
    with _engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA optimize")
    _engine.dispose()
    _engine = None


@contextmanager
def session_scope(engine: Optional[Engine] = None) -> Iterator[Session]:
    """A short session that commits on success and rolls back on any error."""
    session = Session(engine or get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
