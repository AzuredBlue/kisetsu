"""The recent log lines the Logs page shows."""

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional
from uuid import uuid4

RING_SIZE = 500


class LogRing(logging.Handler):
    """Keeps the last lines logged under ``kisetsu``; ``on_line`` tells the UI a new one arrived."""

    def __init__(self, on_line: Callable[[], None] = lambda: None, level: int = logging.INFO):
        super().__init__(level)
        self.lines: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
        self.on_line = on_line
        # Each line is numbered, so the Logs page can ask for only the newer ones; ``run`` tells this run's numbers from
        # an earlier run's, which started over.
        self.seq = 0
        self.run = uuid4().hex[:12]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seq += 1
            self.lines.append({
                "seq": self.seq,
                "at": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "source": record.name.removeprefix("kisetsu."),
                "message": record.getMessage(),
            })
            self.on_line()
        except Exception:
            self.handleError(record)

    def recent(self, limit: int, after: Optional[int] = None, run: Optional[str] = None) -> Dict[str, Any]:
        """The last ``limit`` lines; with ``after`` from this ``run``, only the lines since.

        ``full`` says the lines replace what the caller holds: after a restart, or when lines it never saw have already
        left the ring.
        """
        # Lines are added from other threads, under the same lock.
        with self.lock:
            lines = list(self.lines)
        limit = max(1, limit)
        if after is not None and run == self.run and (not lines or lines[0]["seq"] <= after + 1):
            return {"run": self.run, "full": False, "lines": [line for line in lines if line["seq"] > after][-limit:]}
        return {"run": self.run, "full": True, "lines": lines[-limit:]}
