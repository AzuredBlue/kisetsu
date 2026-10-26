"""The recent log lines the Logs page shows."""

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List

RING_SIZE = 500


class LogRing(logging.Handler):
    """Keeps the last lines logged under ``kisetsu``; ``on_line`` tells the UI a new one arrived."""

    def __init__(self, on_line: Callable[[], None] = lambda: None, level: int = logging.INFO):
        super().__init__(level)
        self.lines: Deque[Dict[str, Any]] = deque(maxlen=RING_SIZE)
        self.on_line = on_line

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append({
                "at": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "source": record.name.removeprefix("kisetsu."),
                "message": record.getMessage(),
            })
            self.on_line()
        except Exception:
            self.handleError(record)

    def recent(self, limit: int) -> List[Dict[str, Any]]:
        return list(self.lines)[-max(1, limit):]
