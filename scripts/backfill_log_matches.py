"""Record releases from a qBittorrent log file that the live log no longer holds.

qBittorrent's application log is a ring: once it rotates, a match it accepted is
only provable from the rotated file. Rotated files are not reachable over the
WebUI API, so this reads them from the host instead and feeds them through the
same ingest path the live observer uses.

Usage:
    python -m scripts.backfill_log_matches ~/.config/qbit-seasonal-anime/anime.db \\
        /path/to/qBittorrent/logs/qbittorrent.log.bak98 [--apply]

Without --apply the work is done against a throwaway copy of the database.
"""

import argparse
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

from sqlmodel import Session

from qbit_seasonal_anime.core.confirmation import ingest_log_acceptances
from qbit_seasonal_anime.db.session import get_engine, init_db

LINE_RE = re.compile(
    r"^\((?P<marker>[A-Z])\)\s*(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s*-\s*(?P<message>.*)$"
)


def read_log_entries(path: Path, utc_offset_hours: float) -> List[Tuple[int, str, datetime]]:
    """Log file lines as (index, message, utc_timestamp)."""
    entries: List[Tuple[int, str, datetime]] = []
    offset = timedelta(hours=utc_offset_hours)

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            match = LINE_RE.match(line.rstrip("\n"))
            if not match:
                continue
            try:
                stamp = datetime.fromisoformat(match.group("stamp"))
            except ValueError:
                continue
            entries.append((index, match.group("message"), (stamp - offset).replace(tzinfo=timezone.utc)))

    return entries


def run(database: Path, entries: List[Tuple[int, str, datetime]]) -> List[str]:
    engine = get_engine(database)
    init_db(engine)
    with Session(engine) as session:
        return ingest_log_acceptances(session, entries)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill matches from a qBittorrent log file.")
    parser.add_argument("database", type=Path, help="Path to anime.db")
    parser.add_argument("log_files", type=Path, nargs="+", help="qBittorrent log file(s) to read")
    parser.add_argument(
        "--utc-offset-hours",
        type=float,
        default=0.0,
        help="Offset of the log timestamps from UTC, since qBittorrent writes local time.",
    )
    parser.add_argument("--apply", action="store_true", help="Write to the real database.")
    args = parser.parse_args()

    if not args.database.exists():
        print(f"Database not found: {args.database}", file=sys.stderr)
        return 1

    missing = [str(p) for p in args.log_files if not p.exists()]
    if missing:
        print(f"Log file not found: {', '.join(missing)}", file=sys.stderr)
        return 1

    entries: List[Tuple[int, str, datetime]] = []
    for path in args.log_files:
        file_entries = read_log_entries(path, args.utc_offset_hours)
        print(f"Read {len(file_entries)} log entries from {path}")
        entries.extend(file_entries)

    entries.sort(key=lambda item: item[2])

    target = args.database
    if not args.apply:
        scratch = Path(tempfile.mkdtemp(prefix="qbit-backfill-")) / "anime.db"
        shutil.copy2(args.database, scratch)
        target = scratch
        print(f"Dry run against a copy at {scratch}")

    logs = run(target, entries)

    if not logs:
        print("No new matches to record.")
        return 0

    print(f"{len(logs)} match(es) recorded:")
    for message in logs:
        print(f"  {message}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write to the real database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
