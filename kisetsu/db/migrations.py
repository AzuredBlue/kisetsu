import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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


def _v4(engine: Engine) -> None:
    if "batch" not in {c["name"] for c in inspect(engine).get_columns("downloads")}:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE downloads ADD COLUMN batch BOOLEAN NOT NULL DEFAULT 0")


def _v5(engine: Engine) -> None:
    if "cover_color" not in {c["name"] for c in inspect(engine).get_columns("shows")}:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE shows ADD COLUMN cover_color VARCHAR")


def _v6(engine: Engine) -> None:
    columns = {c["name"] for c in inspect(engine).get_columns("shows")}
    with engine.begin() as conn:
        for name in ("cover_edge", "cover_edge_src"):
            if name not in columns:
                conn.exec_driver_sql(f"ALTER TABLE shows ADD COLUMN {name} VARCHAR")


def _v7(engine: Engine) -> None:
    # A replacement that gave up used to fail for good, which keeps its version from being tried again.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE downloads SET state = 'CANCELED' WHERE state = 'FAILED' AND supersedes_id IS NOT NULL "
            "AND (last_error LIKE 'Gave up after%' OR last_error LIKE 'Replacement did not finish%')"
        )


def _v8(engine: Engine) -> None:
    # The retry ladders used to count earlier endings by matching the error text; they count this column now.
    with engine.begin() as conn:
        if "end_reason" not in {c["name"] for c in inspect(engine).get_columns("downloads")}:
            conn.exec_driver_sql("ALTER TABLE downloads ADD COLUMN end_reason VARCHAR")
        conn.exec_driver_sql(
            "UPDATE downloads SET end_reason = 'ADD_GAVE_UP' "
            "WHERE state IN ('CANCELED', 'FAILED') AND last_error LIKE 'Gave up after % attempts%'"
        )
        conn.exec_driver_sql(
            "UPDATE downloads SET end_reason = 'REPLACEMENT_GAVE_UP' "
            "WHERE state IN ('CANCELED', 'FAILED') AND last_error LIKE 'Gave up after % hours%'"
        )
        conn.exec_driver_sql(
            "UPDATE downloads SET end_reason = 'REMOVED' "
            "WHERE state = 'CANCELED' AND last_error = 'Torrent is no longer present in qBittorrent; queued for a safe retry.'"
        )


def _v9(engine: Engine) -> None:
    # The minimum resolution was fixed at 1080p; it is a setting now, starting where it was.
    if "min_resolution" not in {c["name"] for c in inspect(engine).get_columns("settings")}:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE settings ADD COLUMN min_resolution INTEGER NOT NULL DEFAULT 1080")


def _v10(engine: Engine) -> None:
    # SubsPlease's RSS writes US Pacific time marked +0000, and its items were stored that way. Items its search API
    # gave (their torrent is a magnet) had nearly right dates, so they stay.
    pacific = ZoneInfo("America/Los_Angeles")
    with engine.begin() as conn:
        feeds = [
            feed_id for feed_id, url in conn.exec_driver_sql("SELECT id, url FROM feeds").all()
            if (urlparse(url).hostname or "").lower().removeprefix("www.") == "subsplease.org"
        ]
        for feed_id in feeds:
            rows = conn.exec_driver_sql(
                "SELECT id, published_at FROM feed_items WHERE feed_id = ? AND published_at IS NOT NULL "
                "AND (torrent_url IS NULL OR torrent_url NOT LIKE 'magnet:%')",
                (feed_id,),
            ).all()
            for item_id, stored in rows:
                wall = datetime.fromisoformat(str(stored))
                if wall.year <= 2000:
                    continue
                fixed = wall.replace(tzinfo=pacific).astimezone(timezone.utc).replace(tzinfo=None)
                conn.exec_driver_sql(
                    "UPDATE feed_items SET published_at = ? WHERE id = ?", (fixed.strftime("%Y-%m-%d %H:%M:%S.%f"), item_id),
                )


# Each step upgrades the schema from the version before it. Steps after the
# first are written as plain SQL against the previous layout.
MIGRATIONS: List[Tuple[int, Callable[[Engine], None]]] = [
    (1, _v1),
    (2, _v2),
    (3, _v3),
    (4, _v4),
    (5, _v5),
    (6, _v6),
    (7, _v7),
    (8, _v8),
    (9, _v9),
    (10, _v10),
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
    # The database runs in WAL mode, so copying the file alone would miss what has not been checkpointed yet.
    os.close(os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600))
    source = sqlite3.connect(path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
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
