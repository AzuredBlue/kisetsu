import json
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlalchemy import Index, MetaData, UniqueConstraint
from sqlalchemy.orm import registry
from sqlalchemy.types import DateTime, TypeDecorator
from sqlmodel import Field, SQLModel

from kisetsu.config import (
    DEFAULT_ANILIST_SYNC_MINUTES,
    DEFAULT_BASE_DIR,
    DEFAULT_CATEGORY,
    DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
    DEFAULT_FEED_REFRESH_MINUTES,
    DEFAULT_HUNT_INTERVAL_MINUTES,
    DEFAULT_QBIT_HOST,
    DEFAULT_QBIT_PASSWORD,
    DEFAULT_QBIT_USERNAME,
    DEFAULT_SEED_RATIO,
)


class Base(SQLModel, registry=registry()):
    """Own metadata, so these tables never share a namespace with other SQLModel classes."""

    metadata = MetaData()


class UTCDateTime(TypeDecorator):
    """A datetime stored as naive UTC and always returned timezone-aware.

    Values handed in without a timezone are taken to be UTC, so naive and aware
    datetimes can never be mixed in a comparison after a round trip.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(**kwargs):
    return Field(sa_type=UTCDateTime, **kwargs)


class FeedState(str, Enum):
    NONE = "none"
    CANDIDATE = "candidate"
    ASSIGNED = "assigned"
    PINNED = "pinned"
    LOCKED = "locked"


class PausedBy(str, Enum):
    USER = "user"
    ALL = "all"


class ScheduleState(str, Enum):
    UNKNOWN = "unknown"
    SCHEDULED = "scheduled"
    AIRED = "aired"
    POSTPONED = "postponed"


class EpisodeStatus(str, Enum):
    WANTED = "wanted"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSED = "missed"
    # The user chose not to download it; nothing grabs it until they want it again.
    SKIPPED = "skipped"


class DownloadState(str, Enum):
    ADDING = "adding"
    VERIFYING = "verifying"
    DOWNLOADING = "downloading"
    DONE = "done"
    SUPERSEDED = "superseded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELED = "canceled"


class OffsetSource(str, Enum):
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    MANUAL = "manual"


class EventKind(str, Enum):
    GRABBED = "grabbed"
    DONE = "done"
    REPLACED = "replaced"
    FAILED = "failed"
    MISSED = "missed"
    ADOPTED = "adopted"
    FEED = "feed"
    INFO = "info"


# Downloads that still occupy a torrent slot or are waiting to be retried.
ACTIVE_DOWNLOAD_STATES = (
    DownloadState.ADDING,
    DownloadState.VERIFYING,
    DownloadState.DOWNLOADING,
    DownloadState.RETRY_WAIT,
)
# Feed states in which no automatic path may move a show to another feed.
FIXED_FEED_STATES = (FeedState.PINNED, FeedState.LOCKED)


class Settings(Base, table=True):
    __tablename__ = "settings"

    id: int = Field(default=1, primary_key=True)
    qbit_host: str = Field(default=DEFAULT_QBIT_HOST)
    qbit_username: str = Field(default=DEFAULT_QBIT_USERNAME)
    qbit_password: str = Field(default=DEFAULT_QBIT_PASSWORD)
    base_dir: str = Field(default=DEFAULT_BASE_DIR)
    default_category: str = Field(default=DEFAULT_CATEGORY)
    default_seed_ratio: float = Field(default=DEFAULT_SEED_RATIO)
    anilist_username: str = Field(default="")
    anilist_sync_minutes: int = Field(default=DEFAULT_ANILIST_SYNC_MINUTES)
    feed_refresh_minutes: int = Field(default=DEFAULT_FEED_REFRESH_MINUTES)
    hunt_interval_minutes: int = Field(default=DEFAULT_HUNT_INTERVAL_MINUTES)
    early_air_tolerance_hours: int = Field(default=DEFAULT_EARLY_AIR_TOLERANCE_HOURS)
    title_language: str = Field(default="english")
    accent_color: str = Field(default="#2dd4bf")
    accent_tint: str = Field(default="subtle")
    desktop_notifications: bool = Field(default=False)


class AppState(Base, table=True):
    """Runtime state the app keeps for itself, apart from what the user configures."""

    __tablename__ = "app_state"

    key: str = Field(primary_key=True)
    value: str = Field(default="")


class Feed(Base, table=True):
    __tablename__ = "feeds"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    url: str = Field(unique=True, index=True)
    priority: int = Field(default=1, index=True)  # Lower number = higher priority (1 is top)
    # The item path in qBittorrent's RSS tree this feed is mirrored to.
    qbit_path: Optional[str] = Field(default=None, nullable=True)
    last_fetch_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    last_ok_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    failing_since: Optional[datetime] = _timestamp(default=None, nullable=True)
    last_error: Optional[str] = Field(default=None, nullable=True)


class FeedItem(Base, table=True):
    """Every article seen on a feed. Which followed shows it matches is in ``FeedItemMatch``."""

    __tablename__ = "feed_items"
    __table_args__ = (UniqueConstraint("feed_id", "guid", name="uq_feed_item"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    feed_id: int = Field(foreign_key="feeds.id", ondelete="CASCADE", index=True)
    guid: str
    info_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    title: str = Field(default="")
    torrent_url: Optional[str] = Field(default=None, nullable=True)
    published_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    first_seen_at: datetime = _timestamp(default_factory=utc_now, index=True)
    episode_raw: Optional[int] = Field(default=None, nullable=True)
    version: int = Field(default=1)
    release_group: Optional[str] = Field(default=None, nullable=True)
    parsed_title: Optional[str] = Field(default=None, nullable=True)


class FeedItemMatch(Base, table=True):
    """A feed item that belongs to a followed show. An item can belong to several (franchise overlaps)."""

    __tablename__ = "feed_item_matches"

    item_id: int = Field(foreign_key="feed_items.id", ondelete="CASCADE", primary_key=True)
    show_id: int = Field(foreign_key="shows.id", ondelete="CASCADE", primary_key=True, index=True)


def _decode_list(raw: Optional[str]) -> List[str]:
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


class Show(Base, table=True):
    __tablename__ = "shows"

    id: Optional[int] = Field(default=None, primary_key=True)
    anilist_id: int = Field(unique=True, index=True)

    title_romaji: Optional[str] = Field(default=None, nullable=True)
    title_english: Optional[str] = Field(default=None, nullable=True)
    aliases_json: str = Field(default="[]")
    custom_aliases_json: str = Field(default="[]")
    cover_image: Optional[str] = Field(default=None, nullable=True)
    banner_image: Optional[str] = Field(default=None, nullable=True)
    accent_hues: Optional[str] = Field(default=None, nullable=True)
    accent_src: Optional[str] = Field(default=None, nullable=True)

    season_name: Optional[str] = Field(default=None, nullable=True)
    season_year: Optional[int] = Field(default=None, nullable=True)
    total_episodes: Optional[int] = Field(default=None, nullable=True)
    anilist_status: Optional[str] = Field(default=None, nullable=True, index=True)
    list_status: Optional[str] = Field(default=None, nullable=True)
    next_airing_episode: Optional[int] = Field(default=None, nullable=True)
    next_airing_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    schedule_synced_at: Optional[datetime] = _timestamp(default=None, nullable=True)

    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    feed_state: FeedState = Field(default=FeedState.NONE, index=True)
    # Only meaningful while ``feed_state`` is CANDIDATE: when the lower-ranked feed was first seen.
    candidate_since: Optional[datetime] = _timestamp(default=None, nullable=True)
    learned_name: Optional[str] = Field(default=None, nullable=True)
    learned_group: Optional[str] = Field(default=None, nullable=True)

    save_folder: str = Field(default="")
    category: Optional[str] = Field(default=None, nullable=True)
    seed_ratio: Optional[float] = Field(default=None, nullable=True)
    # The per-series tag Kisetsu put on this show's torrents in qBittorrent.
    qbit_tag: Optional[str] = Field(default=None, nullable=True)

    paused: bool = Field(default=False, index=True)
    paused_by: Optional[PausedBy] = Field(default=None, nullable=True)
    completed: bool = Field(default=False, index=True)
    last_backfill_at: Optional[datetime] = _timestamp(default=None, nullable=True)

    @property
    def aliases(self) -> List[str]:
        return _decode_list(self.aliases_json)

    @aliases.setter
    def aliases(self, val: List[str]) -> None:
        self.aliases_json = json.dumps(list(dict.fromkeys(val)))

    @property
    def custom_aliases(self) -> List[str]:
        return _decode_list(self.custom_aliases_json)

    @custom_aliases.setter
    def custom_aliases(self, val: List[str]) -> None:
        self.custom_aliases_json = json.dumps(list(dict.fromkeys(val)))

    @property
    def effective_aliases(self) -> List[str]:
        """Everything a release may be matched against."""
        return list(dict.fromkeys(self.aliases + self.custom_aliases))

    def title(self, language: str = "english") -> str:
        """The name in the configured language, falling back to the other one."""
        if language == "english" and self.title_english:
            return self.title_english
        return self.title_romaji or self.title_english or f"AniList {self.anilist_id}"

    @property
    def feed_is_fixed(self) -> bool:
        return self.feed_state in FIXED_FEED_STATES


class Episode(Base, table=True):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("show_id", "number", name="uq_episode_show_number"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    show_id: int = Field(foreign_key="shows.id", ondelete="CASCADE", index=True)
    number: int
    air_at: Optional[datetime] = _timestamp(default=None, nullable=True, index=True)
    schedule_state: ScheduleState = Field(default=ScheduleState.UNKNOWN)
    status: EpisodeStatus = Field(default=EpisodeStatus.WANTED, index=True)
    # The download that currently supplies this episode. Not a foreign key:
    # downloads point back at their episode, and a cycle would block table creation.
    current_download_id: Optional[int] = Field(default=None, nullable=True, index=True)
    retry_after: Optional[datetime] = _timestamp(default=None, nullable=True)
    last_error: Optional[str] = Field(default=None, nullable=True)
    attempts: int = Field(default=0)


class Download(Base, table=True):
    """One torrent Kisetsu added or adopted for an episode."""

    __tablename__ = "downloads"
    __table_args__ = (Index("ix_download_state_updated", "state", "updated_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    episode_id: int = Field(foreign_key="episodes.id", ondelete="CASCADE", index=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    feed_item_guid: Optional[str] = Field(default=None, nullable=True)
    title: str
    parsed_title: Optional[str] = Field(default=None, nullable=True)
    release_group: Optional[str] = Field(default=None, nullable=True)
    version: int = Field(default=1)
    source_episode: Optional[int] = Field(default=None, nullable=True)
    torrent_url: Optional[str] = Field(default=None, nullable=True)
    info_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    # Random tag put on the torrent while its hash is unknown; removed once the hash is.
    tag: Optional[str] = Field(default=None, nullable=True, unique=True)
    state: DownloadState = Field(default=DownloadState.ADDING, index=True)
    supersedes_id: Optional[int] = Field(default=None, foreign_key="downloads.id", ondelete="SET NULL", nullable=True, index=True)
    adopted: bool = Field(default=False)
    # Whether the torrent is a whole-season batch, which a title without a batch marker can still be.
    batch: bool = Field(default=False)
    attempts: int = Field(default=1)
    next_retry_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    last_error: Optional[str] = Field(default=None, nullable=True)
    created_at: datetime = _timestamp(default_factory=utc_now)
    updated_at: datetime = _timestamp(default_factory=utc_now)
    completed_at: Optional[datetime] = _timestamp(default=None, nullable=True)


class EpisodeOffset(Base, table=True):
    """How a feed's episode numbers map onto the show's AniList numbers."""

    __tablename__ = "episode_offsets"
    __table_args__ = (UniqueConstraint("show_id", "feed_id", name="uq_offset_show_feed"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    show_id: int = Field(foreign_key="shows.id", ondelete="CASCADE", index=True)
    feed_id: int = Field(foreign_key="feeds.id", ondelete="CASCADE", index=True)
    offset: int = Field(default=0)
    source: OffsetSource = Field(default=OffsetSource.INFERRED)
    evidence_count: int = Field(default=0)
    first_evidence_at: Optional[datetime] = _timestamp(default=None, nullable=True)
    last_evidence_at: Optional[datetime] = _timestamp(default=None, nullable=True)


class Event(Base, table=True):
    """What the app did, shown in the History tab."""

    __tablename__ = "events"

    id: Optional[int] = Field(default=None, primary_key=True)
    at: datetime = _timestamp(default_factory=utc_now, index=True)
    kind: EventKind = Field(index=True)
    show_id: Optional[int] = Field(default=None, foreign_key="shows.id", ondelete="SET NULL", nullable=True, index=True)
    episode: Optional[int] = Field(default=None, nullable=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True)
    title: str = Field(default="")
    message: str = Field(default="")


class DismissedShow(Base, table=True):
    """A show the user deleted while it is still on their AniList list. Sync does not add it back."""

    __tablename__ = "dismissed_shows"

    anilist_id: int = Field(primary_key=True)
    created_at: datetime = _timestamp(default_factory=utc_now)
