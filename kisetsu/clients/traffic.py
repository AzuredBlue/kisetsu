"""How many requests Kisetsu has sent since it started, by site and kind, to see what costs requests.

Every attempt counts, retries included, since each one reaches the site. Kept in memory only.
"""

import sys
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

OK, RATE_LIMITED, ERROR = "ok", "rate_limited", "error"

_lock = threading.Lock()
_counts: Counter = Counter()
_started = datetime.now(timezone.utc)
# Called after each request is counted (from any thread), when something shows the counts.
on_change: Optional[Callable[[], None]] = None


def host_of(url: str) -> str:
    return urlparse(url).hostname or url


def record(kind: str, host: str, outcome: str = OK) -> None:
    with _lock:
        _counts[(kind, host, outcome)] += 1
    if on_change is not None:
        on_change()


def caller_outside_clients() -> str:
    """``module.function`` of the nearest caller outside ``kisetsu.clients``: which part of the app made a request."""
    frame = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if not module.startswith("kisetsu.clients"):
            return f"{module.rpartition('.')[2]}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "?"


def outcome_of(status_code: int) -> str:
    return RATE_LIMITED if status_code == 429 else ERROR if status_code >= 400 else OK


def snapshot() -> Dict[str, Any]:
    """The counts by kind and host, most requests first."""
    with _lock:
        counts = dict(_counts)
    rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for (kind, host, outcome), n in counts.items():
        row = rows.setdefault((kind, host), {"kind": kind, "host": host, "requests": 0, OK: 0, RATE_LIMITED: 0, ERROR: 0})
        row["requests"] += n
        row[outcome] += n
    ordered = sorted(rows.values(), key=lambda r: (-r["requests"], r["kind"], r["host"]))
    return {"since": _started.isoformat(), "total": sum(r["requests"] for r in ordered), "rows": ordered}


def reset() -> None:
    global _started
    with _lock:
        _counts.clear()
        _started = datetime.now(timezone.utc)
