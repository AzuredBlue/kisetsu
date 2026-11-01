"""Backups: everything Kisetsu keeps, as one zip file that can be restored here or on another machine.

The zip holds a copy of the database and a small manifest. A backup from an older release goes
through the same migrations as an old database. Rows are copied table by table in both directions,
so only the columns Kisetsu knows ever leave or enter the live database, and a restore that fails
changes nothing.

The qBittorrent password is never in a backup: a stored password only goes to the host it was saved
for (see ``check_password_travels_with_host``), and a file that held it would undo that for anyone
who can open the UI. The covers and the saved AniList list are caches, and refill by themselves.
"""

import json
import logging
import shutil
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from kisetsu.db.migrations import LATEST_VERSION, SchemaError, migrate
from kisetsu.db.schema import Base, Episode, EpisodeStatus, Feed, Settings, Show
from kisetsu.db.store import make_engine
from kisetsu.domain.patterns import compress_home_path
from kisetsu.services.context import Context, setup_completed

logger = logging.getLogger("kisetsu.services.backup")

FORMAT = 1
DB_FILE = "kisetsu.db"
MANIFEST_FILE = "manifest.json"
# Far past any real database; stops a damaged or hostile file from filling the disk.
MAX_BACKUP_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
COPY_CHUNK = 1000
THEMES = ("system", "light", "dark")

NOT_A_BACKUP = "This file is not a Kisetsu backup, or it is damaged."
FROM_NEWER_RELEASE = "This backup comes from a newer version of Kisetsu. Update Kisetsu, then restore it."


class BackupError(ValueError):
    """The file cannot be restored; the message says why."""


@dataclass(frozen=True)
class Staged:
    """A checked backup, unpacked and migrated, ready to restore."""

    db_path: Path
    summary: Dict[str, Any]
    exported_at: Optional[str]
    theme: Optional[str]


def _copy_tables(source: Connection, target: Connection) -> None:
    """Replace the target's rows with the source's, inside the transaction the caller began."""
    target.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    tables = Base.metadata.sorted_tables
    for table in reversed(tables):
        target.execute(table.delete())
    for table in tables:
        for rows in source.execute(select(table)).mappings().partitions(COPY_CHUNK):
            values = [dict(row) for row in rows]
            if table.name == Settings.__tablename__:
                for row in values:
                    row["qbit_password"] = ""
            target.execute(table.insert(), values)


def _drop_orphans(conn: Connection) -> None:
    """Remove rows whose parent is gone, as nothing can use them and a restore would refuse them. Removing one can orphan another."""
    while orphans := conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
        for table, rowid, _parent, _key in orphans:
            conn.exec_driver_sql(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))


def _summary(conn: Connection) -> Dict[str, Any]:
    def count(model, *where) -> int:
        return conn.execute(select(func.count()).select_from(model.__table__).where(*where)).scalar_one()

    host, username = conn.execute(select(Settings.__table__.c.qbit_host, Settings.__table__.c.anilist_username)).one()
    return {
        "shows": count(Show),
        "feeds": count(Feed),
        "downloaded": count(Episode, Episode.__table__.c.status == EpisodeStatus.COMPLETED),
        "qbit_host": host,
        "anilist_username": username,
    }


def write_backup(ctx: Context, target: Path, theme: Optional[str] = None) -> None:
    """Write everything to the zip file ``target``. Must run on the engine thread, so no job writes halfway through the copy."""
    with tempfile.TemporaryDirectory(prefix="kisetsu-backup-") as scratch:
        db_path = Path(scratch) / DB_FILE
        db = make_engine(db_path)
        try:
            migrate(db)
            with db.connect() as out:
                out.exec_driver_sql("PRAGMA foreign_keys=OFF")
                out.exec_driver_sql("BEGIN IMMEDIATE")
                _copy_tables(ctx.session.connection(), out)
                _drop_orphans(out)
                out.commit()
        finally:
            db.dispose()
        manifest = {
            "app": "kisetsu",
            "format": FORMAT,
            "schema": LATEST_VERSION,
            "exported_at": ctx.now.isoformat(),
            "theme": theme if theme in THEMES else None,
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(MANIFEST_FILE, json.dumps(manifest, indent=2))
            bundle.write(db_path, DB_FILE)


def _extract(archive: Path, db_path: Path) -> Dict[str, Any]:
    """Unpack the database and return the manifest. Only the two known names are ever read from the zip."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            info = bundle.getinfo(MANIFEST_FILE)
            packed = bundle.getinfo(DB_FILE)
            if info.file_size > MAX_MANIFEST_BYTES or packed.file_size > MAX_BACKUP_BYTES:
                raise BackupError(NOT_A_BACKUP)
            manifest = json.loads(bundle.read(info))
            with bundle.open(packed) as data, db_path.open("wb") as out:
                shutil.copyfileobj(data, out)
    except (zipfile.BadZipFile, KeyError, ValueError, RuntimeError, NotImplementedError, EOFError, OSError, zlib.error) as e:
        if isinstance(e, BackupError):
            raise
        raise BackupError(NOT_A_BACKUP) from e
    if not isinstance(manifest, dict) or manifest.get("app") != "kisetsu" or not isinstance(manifest.get("format"), int):
        raise BackupError(NOT_A_BACKUP)
    if manifest["format"] > FORMAT:
        raise BackupError(FROM_NEWER_RELEASE)
    return manifest


def _checked_summary(db_path: Path) -> Dict[str, Any]:
    """Bring the unpacked database up to date and read every row, so a bad one is refused before anything is replaced."""
    db = make_engine(db_path)
    try:
        with db.connect() as conn:
            version = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if version > LATEST_VERSION:
            raise BackupError(FROM_NEWER_RELEASE)
        if version < 1:
            raise BackupError(NOT_A_BACKUP)
        migrate(db)
        with db.connect() as conn:
            if conn.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok" or conn.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise BackupError(NOT_A_BACKUP)
            for table in Base.metadata.sorted_tables:
                for _row in conn.execute(select(table)):
                    pass
            return _summary(conn)
    except BackupError:
        raise
    except (SchemaError, DBAPIError, LookupError, ValueError, TypeError) as e:
        raise BackupError(NOT_A_BACKUP) from e
    finally:
        db.dispose()


def unpack(archive: Path, scratch: Path) -> Staged:
    """Check an uploaded file and unpack it into ``scratch``. Touches nothing else."""
    db_path = scratch / DB_FILE
    manifest = _extract(archive, db_path)
    summary = _checked_summary(db_path)
    exported_at = manifest.get("exported_at")
    try:
        datetime.fromisoformat(exported_at)
    except (TypeError, ValueError):
        exported_at = None
    theme = manifest.get("theme")
    return Staged(db_path, summary, exported_at, theme if theme in THEMES else None)


def _keeps_password(ctx: Context, host: str) -> bool:
    """The saved password stays when the backup uses the same qBittorrent. Before setup is finished it is only the default."""
    return setup_completed(ctx) and host.rstrip("/") == (ctx.settings.qbit_host or "").rstrip("/")


def preview(ctx: Context, staged: Staged) -> Dict[str, Any]:
    """What a restore would change, for the user to confirm."""
    return {
        "exported_at": staged.exported_at,
        "backup": staged.summary,
        "current": _summary(ctx.session.connection()),
        "password_needed": not _keeps_password(ctx, staged.summary["qbit_host"]),
    }


def _safety_copy(ctx: Context) -> Optional[Path]:
    """Save what is about to be replaced as a backup beside the database, so a wrong restore can be restored away."""
    database = ctx.session.get_bind().url.database
    if not database:
        return None
    path = Path(database)
    target = path.with_name(f"{path.name}.pre-restore.{ctx.now.strftime('%Y%m%dT%H%M%SZ')}.zip")
    write_backup(ctx, target)
    return target


def restore(ctx: Context, staged: Staged, password: str = "") -> Dict[str, Any]:
    """Replace everything with the backup. An error anywhere rolls back to what was there."""
    keep = ctx.settings.qbit_password if _keeps_password(ctx, staged.summary["qbit_host"]) else ""
    source = make_engine(staged.db_path)
    try:
        saved = _safety_copy(ctx)
        target = ctx.session.connection()
        target.exec_driver_sql("BEGIN IMMEDIATE")
        with source.connect() as rows:
            _copy_tables(rows, target)
        target.execute(update(Settings.__table__).values(qbit_password=password or keep))
        ctx.checkpoint()
    except (DBAPIError, LookupError, TypeError, OSError) as e:
        raise BackupError(f"The backup could not be restored, so nothing changed: {e}") from e
    finally:
        source.dispose()
    ctx.caches.clear()
    ctx.emit("*")
    saved_as = compress_home_path(str(saved)) if saved else None
    logger.info(f"Restored a backup from {staged.exported_at or 'an unknown date'}. The previous data was saved as {saved_as}.")
    return {"status": "success", "saved_as": saved_as, "theme": staged.theme, "shows": staged.summary["shows"]}
