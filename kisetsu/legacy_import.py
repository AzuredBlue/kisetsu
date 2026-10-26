"""`kisetsu import-legacy`: copy what cannot be re-derived from the database of the earlier releases.

The old database is opened read-only. Feed locks, episode states and history are not imported;
they are re-proven from qBittorrent and the feeds.
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from kisetsu.config import LEGACY_DB_PATH
from kisetsu.db.schema import AppState, DismissedShow, Feed, Settings
from kisetsu.domain.patterns import resolve_save_path
from kisetsu.services import legacy
from kisetsu.services.context import SETUP_KEY, make_context, set_state

KEPT_SETTINGS = (
    "qbit_host", "qbit_username", "qbit_password", "base_dir", "default_category", "default_seed_ratio",
    "anilist_username", "hunt_interval_minutes", "early_air_tolerance_hours", "title_language",
    "accent_color", "accent_tint", "desktop_notifications",
)
KEPT_OFFSET_SOURCES = ("CONFIRMED", "MANUAL")
# Numbering learned before offsets had sources; only a real shift is worth keeping, as an inferred one.
LEGACY_OFFSET_SOURCE = "LEGACY"


class LegacyImportError(Exception):
    pass


def _rows(db: sqlite3.Connection, sql: str) -> List[sqlite3.Row]:
    return db.execute(sql).fetchall()


def _custom_folder(base_dir: str, title: str, stored: Optional[str]) -> Optional[str]:
    """The stored folder when it differs from the one the base directory would give the show."""
    if not stored or not stored.strip() or stored.strip() == "{name}":
        return None
    if resolve_save_path(base_dir, title, stored) == resolve_save_path(base_dir, title, None):
        return None
    return stored.strip()


def _overrides(db: sqlite3.Connection, base_dir: str, feed_urls: Dict[int, str]) -> Dict[int, Dict[str, Any]]:
    offsets: Dict[int, List[Dict[str, Any]]] = {}
    for row in _rows(db, "SELECT monitored_id, feed_id, \"offset\", source, evidence_count FROM episode_number_mappings"):
        source = row["source"]
        if source == LEGACY_OFFSET_SOURCE and row["offset"]:
            source = "INFERRED"
        elif source not in KEPT_OFFSET_SOURCES:
            continue
        if row["feed_id"] in feed_urls:
            offsets.setdefault(row["monitored_id"], []).append(
                {"feed_url": feed_urls[row["feed_id"]], "offset": row["offset"], "source": source, "evidence_count": row["evidence_count"]}
            )
    out: Dict[int, Dict[str, Any]] = {}
    for row in _rows(db, "SELECT * FROM monitored"):
        entry: Dict[str, Any] = {}
        folder = _custom_folder(base_dir, row["display_name"], row["save_folder"])
        if folder:
            entry["save_folder"] = folder
        if row["feed_pinned"]:
            pin_id = row["pinned_feed_id"] or row["current_feed_id"]
            if pin_id in feed_urls:
                entry["pinned_feed_url"] = feed_urls[pin_id]
        names = json.loads(row["custom_aliases_json"] or "[]") + json.loads(row["aliases_json"] or "[]")
        if row["matched_title"]:
            names.append(row["matched_title"])
        if names:
            entry["aliases"] = names
        if row["id"] in offsets:
            entry["offsets"] = offsets[row["id"]]
        if entry:
            out[row["anilist_id"]] = entry
    return out


def import_legacy(session: Session, source: Path) -> Dict[str, int]:
    """Copy the kept data from ``source`` into the database behind ``session``."""
    if not source.exists():
        raise LegacyImportError(f"No old database at {source}.")
    if session.exec(select(Feed)).first() is not None or session.get(AppState, SETUP_KEY) is not None:
        raise LegacyImportError("This database already has feeds or finished setup; import into a fresh one.")
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        old = db.execute("SELECT * FROM settings").fetchone()
        if old is None:
            raise LegacyImportError("The old database has no settings.")
        settings = session.get(Settings, 1) or Settings(id=1)
        for name in KEPT_SETTINGS:
            setattr(settings, name, bool(old[name]) if name == "desktop_notifications" else old[name])
        settings.anilist_sync_minutes = old["refresh_interval_minutes"]
        session.add(settings)
        ctx = make_context(session)
        if old["setup_completed"]:
            set_state(ctx, SETUP_KEY, "1")

        feed_urls: Dict[int, str] = {}
        feeds = _rows(db, "SELECT * FROM feeds ORDER BY priority, id")
        for rank, row in enumerate(feeds, start=1):
            session.add(Feed(name=row["qbit_feed_name"], url=row["qbit_feed_url"], priority=rank, qbit_path=row["qbit_feed_name"]))
            feed_urls[row["id"]] = row["qbit_feed_url"]
        for row in _rows(db, "SELECT anilist_id FROM dismissed_shows"):
            session.add(DismissedShow(anilist_id=row["anilist_id"]))
        overrides = _overrides(db, old["base_dir"], feed_urls)
        legacy.store_pending(ctx, overrides)
        session.flush()
        return {"feeds": len(feeds), "overrides": len(overrides), "dismissed": len(_rows(db, "SELECT 1 FROM dismissed_shows"))}
    finally:
        db.close()


def run(source: Optional[str] = None) -> None:
    from kisetsu.db.store import close_engine, session_scope

    path = Path(source).expanduser() if source else LEGACY_DB_PATH
    try:
        with session_scope() as session:
            counts = import_legacy(session, path)
    except LegacyImportError as e:
        raise SystemExit(f"import-legacy: {e}")
    finally:
        close_engine()
    print(f"Imported {counts['feeds']} feeds, {counts['dismissed']} dismissed shows and overrides for {counts['overrides']} shows from {path}.")
