from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Deque, Dict, Any, Optional
import asyncio


class ServerState:
    def __init__(self):
        self.logs: Deque[Dict[str, Any]] = deque(maxlen=500)
        self.wake_event: asyncio.Event = asyncio.Event()
        self.log_wake_event: asyncio.Event = asyncio.Event()
        self.is_running_cycle: bool = False
        self.cycle_owner: Optional[str] = None
        self.daemon_active: bool = False
        self.last_cycle_time: Optional[datetime] = None
        self.next_check_reason: str = "Initializing supervisor..."
        self.next_check_seconds: int = 0
        self.target_next_check_time: Optional[datetime] = None
        # One mutex guards the cycle ownership and the refresh request, so a
        # request can never be consumed by a cycle that did not start because
        # it already held the cycle.
        self._rss_refresh_requested = False
        self._cycle_mutex = Lock()

    def try_begin_cycle(self, owner: str) -> bool:
        """Claim the single supervision cycle slot, or report it busy."""
        with self._cycle_mutex:
            if self.is_running_cycle:
                return False
            self.is_running_cycle = True
            self.cycle_owner = owner
            return True

    def end_cycle(self, owner: Optional[str] = None) -> None:
        """Release the cycle slot, ignoring a release from a different owner."""
        with self._cycle_mutex:
            if owner is not None and self.cycle_owner not in {None, owner}:
                return
            self.is_running_cycle = False
            self.cycle_owner = None

    def request_rss_refresh(self) -> None:
        with self._cycle_mutex:
            self._rss_refresh_requested = True

    def consume_rss_refresh_request(self) -> bool:
        with self._cycle_mutex:
            requested = self._rss_refresh_requested
            self._rss_refresh_requested = False
            return requested

    def add_log(self, message: str, level: str = "INFO"):
        now = datetime.now(timezone.utc)
        self.logs.append({
            "timestamp": now.isoformat(),
            "time_str": now.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        })

    def trigger_immediate_cycle(self):
        self.wake_event.set()

    def trigger_immediate_rule_check(self):
        self.log_wake_event.set()


state = ServerState()