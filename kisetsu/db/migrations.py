import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kisetsu.db.schema import Base, Settings

logger = logging.getLogger("kisetsu.db.migrations")


class SchemaError(RuntimeError):
    """The database cannot be used by this version of Kisetsu."""


def _v1(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def _v2(engine: Engine) -> None:
    if "feed_refresh_minutes" not in {c["name"] for c in inspect(engine).get_columns("settings")}:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN feed_refresh_minutes INTEGER NOT NULL DEFAULT 15")


def _v3(engine: Engine) -> None:
    if "qbit_tag" not in {c["name"] for c in inspect(engine).get_columns("shows")}:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE shows ADD COLUMN qbit_tag VARCHAR")


# Each step upgrades the schema from the version before it. Steps after the
# first are written as plain SQL against the previous layout.
MIGRATIONS: List[Tuple[int, Callable[[Engine], None]]] = [
    (1, _v1),
    (2, _v2),
    (3, _v3),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def _user_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def _set_user_version(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


def _backup(path: Path, version: int) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.v{version}.{stamp}.bak")
    shutil.copy2(path, target)
    logger.info("Backed up %s to %s before migrating.", path.name, target.name)


def migrate(engine: Engine, path: Optional[Path] = None) -> None:
    """Bring the database up to ``LATEST_VERSION``, backing it up before any step on existing data."""
    version = _user_version(engine)
    if version > LATEST_VERSION:
        raise SchemaError(
            f"This database has schema version {version}, newer than this Kisetsu understands ({LATEST_VERSION}). "
            "It belongs to a different release; point Kisetsu at a different database file."
        )
    if version == 0 and inspect(engine).get_table_names():
        raise SchemaError("This database has tables but no Kisetsu schema version; refusing to touch it.")

    for target, step in MIGRATIONS:
        if target <= version:
            continue
        if version > 0 and path is not None and path.exists():
            _backup(path, version)
        step(engine)
        _set_user_version(engine, target)
        version = target

    with Session(engine) as session:
        if session.get(Settings, 1) is None:
            session.add(Settings(id=1))
            session.commit()
