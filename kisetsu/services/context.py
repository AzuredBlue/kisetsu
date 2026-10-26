"""What a service function works with: one database session, qBittorrent, and the time."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlmodel import Session

from kisetsu.db.schema import AppState, Settings
from kisetsu.domain.matcher import Matcher

# The parsed-title cache is dropped once it grows past this many titles.
PARSED_CACHE_LIMIT = 20_000


@dataclass
class Caches:
    """In-memory state that outlives a single job. Everything here can be rebuilt from the database."""

    known_items: Optional[Set[Tuple[int, str]]] = None
    matchers: Optional[Tuple[int, List[Tuple[int, Matcher]]]] = None
    parsed: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Settle passes in which a tracked torrent was missing from qBittorrent, and the list size then.
    absent: Dict[str, int] = field(default_factory=dict)
    listed: Optional[int] = None
    # Shows whose grabs wait for their saved files to be matched to a feed, and since when.
    held: Dict[int, datetime] = field(default_factory=dict)
    # Torrents already reported as refused for adoption, so one that stays refused is mentioned once.
    refused: Set[str] = field(default_factory=set)
    # Past-release searches already run: show id -> (what was searched for, when).
    searched: Dict[int, Tuple[int, datetime]] = field(default_factory=dict)

    def parsed_cache(self) -> Dict[str, Dict[str, Any]]:
        if len(self.parsed) > PARSED_CACHE_LIMIT:
            self.parsed.clear()
        return self.parsed


@dataclass
class Context:
    session: Session
    settings: Settings
    qbit: Any
    now: datetime
    caches: Caches = field(default_factory=Caches)
    # Parts of the UI whose data changed; the engine publishes them after the job commits.
    topics: Set[str] = field(default_factory=set)
    notify: Optional[Callable[[str, str, Optional[str]], None]] = None

    def emit(self, *topics: str) -> None:
        self.topics.update(topics)

    def checkpoint(self) -> None:
        """Commit what is done so far, so an error later in the job cannot undo what qBittorrent already did."""
        self.session.commit()

    @property
    def tolerance_hours(self) -> int:
        return max(0, int(self.settings.early_air_tolerance_hours or 0))


def make_context(
    session: Session,
    qbit: Any = None,
    now: Optional[datetime] = None,
    caches: Optional[Caches] = None,
    notify: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> Context:
    settings = session.get(Settings, 1)
    if settings is None:
        settings = Settings(id=1)
        session.add(settings)
        session.flush()
    return Context(
        session=session,
        settings=settings,
        qbit=qbit,
        now=now or datetime.now(timezone.utc),
        caches=caches or Caches(),
        notify=notify,
    )


def get_state(ctx: Context, key: str, default: str = "") -> str:
    row = ctx.session.get(AppState, key)
    return row.value if row is not None else default


def set_state(ctx: Context, key: str, value: str) -> None:
    row = ctx.session.get(AppState, key)
    if row is None:
        ctx.session.add(AppState(key=key, value=value))
    else:
        row.value = value
        ctx.session.add(row)


SETUP_KEY = "setup_completed"


def setup_completed(ctx: Context) -> bool:
    return get_state(ctx, SETUP_KEY) == "1"
