"""`kisetsu --adopt-report`: a dry run of the first start against a throwaway database.

It imports the old settings and feeds, syncs AniList, reads the feeds and adopts what qBittorrent
already holds, then lists what one grab pass would add. qBittorrent is only ever read.
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from kisetsu.clients.qbittorrent import Qbit


class RecordingQbit(Qbit):
    """A client whose writes are noted and dropped."""

    def __init__(self, *args: Any, writes: List[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.writes = writes

    def _call(self, fn, *, write: bool = False):
        if write:
            frame = sys._getframe(1)
            while frame.f_code.co_name in ("_hash_call", "_rss_write"):
                frame = frame.f_back
            self.writes.append(frame.f_code.co_name)
            return None
        return super()._call(fn)

    def delete(self, hashes: List[str], *, delete_files: bool) -> None:
        self.writes.append(f"delete {len(hashes)} torrent(s){' with files' if delete_files else ''}: {', '.join(hashes)}")

    def add_torrent(self, url: str, **kwargs: Any) -> None:
        self.writes.append(f"add {url}")


def _old_completed(source: Path) -> Set[Tuple[int, int]]:
    db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "SELECT m.anilist_id, e.episode_number FROM episodes e JOIN monitored m ON m.id = e.monitored_id WHERE e.status = 'COMPLETED'"
        ).fetchall()
    finally:
        db.close()
    return {(a, n) for a, n in rows}


async def _run(source: Path) -> int:
    from sqlmodel import select

    from kisetsu.clients import rss
    from kisetsu.clients.anilist import AniListClient
    from kisetsu.db import store
    from kisetsu.db.schema import Download, Episode, Feed, Show
    from kisetsu.engine.jobs import Jobs
    from kisetsu.engine.runner import Engine
    from kisetsu.engine.scheduler import Scheduler
    from kisetsu.legacy_import import import_legacy
    from kisetsu.services import cycle

    db = store.get_engine()
    with store.session_scope(db) as session:
        counts = import_legacy(session, source)
    print(f"Imported {counts['feeds']} feeds and overrides for {counts['overrides']} shows.")

    writes: List[str] = []
    engine = Engine(db, qbit_factory=lambda host, user, password, read_only=False: RecordingQbit(host, user, password, writes=writes))
    await engine.start()
    http = rss.new_client()
    jobs = Jobs(engine, Scheduler(), anilist=AniListClient(), http=http)
    try:
        await jobs.startup()
        if not jobs.ready.is_set():
            print(f"Startup did not finish: {jobs.health}")
            return 2
        await engine.run(cycle.grab_cycle)
    finally:
        await http.aclose()
        await engine.close()

    old_done = _old_completed(source)
    violations: List[str] = []
    from sqlmodel import Session

    with Session(db) as session:
        feeds = {f.id: f.name for f in session.exec(select(Feed)).all()}
        episodes = {e.id: e for e in session.exec(select(Episode)).all()}
        downloads = session.exec(select(Download)).all()
        for show in session.exec(select(Show).order_by(Show.title_romaji)).all():
            mine = [d for d in downloads if episodes[d.episode_id].show_id == show.id]
            adopted = sorted(episodes[d.episode_id].number for d in mine if d.adopted)
            grab = sorted(episodes[d.episode_id].number for d in mine if not d.adopted)
            feed = feeds.get(show.feed_id, "none")
            print(f"{show.title('english')}: feed {feed} ({show.feed_state.name.lower()})")
            print(f"    adopted: {adopted or '-'}    would grab: {grab or '-'}")
            for number in grab:
                if (show.anilist_id, number) in old_done:
                    violations.append(f"{show.title('english')} episode {number}")
    deletes = [w for w in writes if w.startswith("delete ")]
    print(f"Would delete: {deletes or 'nothing'}")
    adds = [w for w in writes if w.startswith("add ")]
    others = Counter(w for w in writes if not w.startswith(("delete ", "add ")))
    print(f"Would add {len(adds)} torrent(s). Other writes: {', '.join(f'{k} x{v}' for k, v in sorted(others.items())) or 'none'}")
    if violations:
        print("GATE FAILED: episodes already completed in the old database would be grabbed again:")
        for line in violations:
            print(f"    {line}")
        return 1
    print("Gate passed: no episode completed in the old database would be grabbed again.")
    return 0 if not deletes else 1


def run(source: Optional[str] = None) -> int:
    scratch = tempfile.mkdtemp(prefix="kisetsu-adopt-report-")
    os.environ["KISETSU_DB_PATH"] = os.path.join(scratch, "kisetsu.db")
    os.environ["KISETSU_SANDBOX"] = "1"
    from kisetsu.config import LEGACY_DB_PATH
    from kisetsu.db import store
    from kisetsu.main import log_to_stderr

    log_to_stderr()
    path = Path(source).expanduser() if source else LEGACY_DB_PATH
    try:
        return asyncio.run(_run(path))
    finally:
        store.close_engine()
        shutil.rmtree(scratch, ignore_errors=True)
