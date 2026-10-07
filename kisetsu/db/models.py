import json
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple
from pydantic import NaiveDatetime
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


from kisetsu.config import (
    DEFAULT_BASE_DIR,
    DEFAULT_BACKFILL_WINDOW_DAYS,
    DEFAULT_CATEGORY,
    DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
    DEFAULT_DOWNLOAD_MODE,
    DEFAULT_QBIT_HOST,
    DEFAULT_QBIT_PASSWORD,
    DEFAULT_QBIT_USERNAME,
    DEFAULT_REFRESH_INTERVAL_MINUTES,
    DEFAULT_SEED_RATIO,
    DEFAULT_STALL_WAIT_HOURS,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Return ``value`` as an aware UTC datetime.

    SQLite hands datetimes back naive; they are stored as UTC, so a naive value
    is labelled UTC and an aware one is converted.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class MonitoredStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"
    FIXED = "fixed"
    STALLED = "stalled"
    COMPLETED = "completed"
    PAUSED = "paused"


class EpisodeStatus(str, Enum):
    WANTED = "wanted"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    REPLACING = "replacing"
    FAILED = "failed"
    MISSED = "missed"


class TorrentOperationStatus(str, Enum):
    PREPARING = "preparing"
    ADDED = "added"
    NEW_VERIFIED = "new_verified"
    OLD_STOPPED = "old_stopped"
    RETRY_WAIT = "retry_wait"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    FAILED = "failed"
    # Historical name: a replacement that is still downloading, waiting to
    # finish before the release it supersedes is removed.
    SEEDING = "seeding"
    CANCELED = "canceled"


# Statuses of an operation that is still in flight, in lifecycle order.
RESUME_STATUS_ORDER: Tuple[TorrentOperationStatus, ...] = (
    TorrentOperationStatus.PREPARING,
    TorrentOperationStatus.ADDED,
    TorrentOperationStatus.RETRY_WAIT,
    TorrentOperationStatus.UNKNOWN,
    TorrentOperationStatus.NEW_VERIFIED,
    TorrentOperationStatus.SEEDING,
    TorrentOperationStatus.OLD_STOPPED,
)

ACTIVE_OPERATION_STATUSES: Tuple[str, ...] = tuple(
    status.name for status in RESUME_STATUS_ORDER
)
# In-flight statuses a user action may cancel. FAILED is terminal, and SEEDING
# (a replacement still downloading) is left to finish.
CANCELLABLE_OPERATION_STATUSES: Tuple[str, ...] = (
    TorrentOperationStatus.ADDED.name,
    TorrentOperationStatus.NEW_VERIFIED.name,
    TorrentOperationStatus.OLD_STOPPED.name,
    TorrentOperationStatus.UNKNOWN.name,
    TorrentOperationStatus.RETRY_WAIT.name,
)


class RuleOutcome(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    STALLED = "stalled"
    REPLACED = "replaced"


class EpisodeMappingSource(str, Enum):
    INFERRED = "inferred"
    CONFIRMED = "confirmed"
    MANUAL = "manual"
    LEGACY = "legacy"


class EpisodeScheduleState(str, Enum):
    UNKNOWN = "unknown"
    SCHEDULED = "scheduled"
    AIRED = "aired"
    POSTPONED = "postponed"
    UNKNOWN_PAST = "unknown_past"


def normalize_mapping_source(value: object) -> str:
    """Canonical form of an ``episode_number_mappings.source`` value.

    The column is a plain string column (it predates the enum and older rows
    were written in mixed case), while the enum members below are persisted by
    name. Always compare through this helper so a value read back from SQLite
    compares equal to the member it was written from.
    """
    if isinstance(value, EpisodeMappingSource):
        return value.name
    text = str(value or "").strip().upper()
    return text or EpisodeMappingSource.INFERRED.name


class Settings(SQLModel, table=True):
    __tablename__ = "settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    qbit_host: str = Field(default=DEFAULT_QBIT_HOST)
    qbit_username: str = Field(default=DEFAULT_QBIT_USERNAME)
    qbit_password: str = Field(default=DEFAULT_QBIT_PASSWORD)
    base_dir: str = Field(default=DEFAULT_BASE_DIR)
    default_category: str = Field(default=DEFAULT_CATEGORY)
    default_seed_ratio: float = Field(default=DEFAULT_SEED_RATIO)
    anilist_username: str = Field(default="")
    refresh_interval_minutes: int = Field(default=DEFAULT_REFRESH_INTERVAL_MINUTES)
    stall_wait_hours: int = Field(default=DEFAULT_STALL_WAIT_HOURS)
    title_language: str = Field(default="english")  # "english" or "romaji"
    download_mode: str = Field(default=DEFAULT_DOWNLOAD_MODE)
    backfill_window_days: int = Field(default=DEFAULT_BACKFILL_WINDOW_DAYS)
    early_air_tolerance_hours: int = Field(default=DEFAULT_EARLY_AIR_TOLERANCE_HOURS)
    accent_color: str = Field(default="#2dd4bf")
    accent_tint: str = Field(default="subtle")  # "off", "subtle" or "full"


class SupervisionLease(SQLModel, table=True):
    __tablename__ = "supervision_leases"

    id: int = Field(default=1, primary_key=True)
    owner: str = Field(index=True)
    acquired_at: NaiveDatetime = Field(default_factory=utc_now, index=True)
    heartbeat_at: NaiveDatetime = Field(default_factory=utc_now, index=True)


class Feed(SQLModel, table=True):
    __tablename__ = "feeds"

    id: Optional[int] = Field(default=None, primary_key=True)
    qbit_feed_name: str = Field(index=True)
    qbit_feed_url: str = Field(unique=True, index=True)
    priority: int = Field(default=1, index=True)  # Lower number = higher priority (1 is top)


def _decode_aliases(raw: Optional[str]) -> List[str]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []


class Monitored(SQLModel, table=True):
    __tablename__ = "monitored"

    id: Optional[int] = Field(default=None, primary_key=True)
    anilist_id: int = Field(unique=True, index=True)
    display_name: str = Field(index=True)
    title_romaji: Optional[str] = Field(default=None, nullable=True)
    title_english: Optional[str] = Field(default=None, nullable=True)
    aliases_json: str = Field(default="[]")
    custom_aliases_json: str = Field(default="[]")
    status: MonitoredStatus = Field(default=MonitoredStatus.UNCONFIRMED, index=True)
    current_feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    # The feed that actually delivered a release for this show. Set once, when a
    # release is accepted, and never cleared automatically: from then on this is
    # the only feed the show is ever read from. Distinct from ``feed_pinned``,
    # which is the user overriding the engine before anything was downloaded.
    learned_feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    qbit_rule_name: Optional[str] = Field(default=None, nullable=True)
    total_episodes: Optional[int] = Field(default=None, nullable=True)
    next_airing_episode: Optional[int] = Field(default=None, nullable=True)
    next_airing_at: Optional[NaiveDatetime] = Field(default=None, nullable=True)
    last_confirmed_episode: Optional[int] = Field(default=None, nullable=True)
    save_folder: str = Field(default="")
    matched_title: Optional[str] = Field(default=None, nullable=True)
    matched_release_group: Optional[str] = Field(default=None, nullable=True)
    cover_image: Optional[str] = Field(default=None, nullable=True)
    banner_image: Optional[str] = Field(default=None, nullable=True)
    # Dominant colours of the banner (or cover) as "hue,saturation,secondary hue", or
    # "" when the image has none. accent_src is the image they were read from.
    accent_hues: Optional[str] = Field(default=None, nullable=True)
    accent_src: Optional[str] = Field(default=None, nullable=True)
    season_name: Optional[str] = Field(default=None, nullable=True)
    season_year: Optional[int] = Field(default=None, nullable=True)
    status_before_pause: Optional[str] = Field(default=None, nullable=True)
    custom_regex: Optional[str] = Field(default=None, nullable=True)
    custom_must_not: Optional[str] = Field(default=None, nullable=True)
    feed_pinned: bool = Field(default=False)
    candidate_feed_id: Optional[int] = Field(default=None, nullable=True)
    candidate_feed_since: Optional[NaiveDatetime] = Field(default=None, nullable=True)
    anilist_status: Optional[str] = Field(default=None, nullable=True, index=True)
    schedule_synced_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    schedule_stale: bool = Field(default=False, index=True)

    @property
    def aliases(self) -> List[str]:
        return _decode_aliases(self.aliases_json)

    @aliases.setter
    def aliases(self, val: List[str]) -> None:
        self.aliases_json = json.dumps(list(dict.fromkeys(val)))  # unique while preserving order

    @property
    def custom_aliases(self) -> List[str]:
        """Aliases typed in by hand, kept apart from the ones AniList supplies."""
        return _decode_aliases(self.custom_aliases_json)

    @custom_aliases.setter
    def custom_aliases(self, val: List[str]) -> None:
        self.custom_aliases_json = json.dumps(list(dict.fromkeys(val)))

    @property
    def effective_aliases(self) -> List[str]:
        """Everything a release may be matched against."""
        return list(dict.fromkeys(self.aliases + self.custom_aliases))

    @property
    def feed_is_locked(self) -> bool:
        """
        True when no automatic path may move this show to another feed.

        Either the user picked the feed outright (``feed_pinned``) or a release
        was really downloaded from it (``learned_feed_id``). A feed that has
        proven it carries the show outranks every heuristic we have, so stall
        handling, candidate nomination and feed rediscovery all defer to it.
        """
        return bool(self.feed_pinned) or self.learned_feed_id is not None


class EpisodeNumberMapping(SQLModel, table=True):
    __tablename__ = "episode_number_mappings"
    __table_args__ = (
        UniqueConstraint("monitored_id", "feed_id", name="uq_episode_mapping_show_feed"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    feed_id: int = Field(foreign_key="feeds.id", ondelete="CASCADE", index=True)
    offset: int = Field(default=0)
    source: str = Field(default=EpisodeMappingSource.INFERRED.name, index=True)
    evidence_count: int = Field(default=0)
    first_evidence_at: Optional[NaiveDatetime] = Field(default=None, nullable=True)
    last_evidence_at: Optional[NaiveDatetime] = Field(default=None, nullable=True)
    updated_at: NaiveDatetime = Field(default_factory=utc_now, index=True)


class Episode(SQLModel, table=True):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("monitored_id", "episode_number", name="uq_episode_monitored_number"),
        Index("ix_episode_status_version", "status", "version"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    episode_number: int = Field(index=True)
    status: EpisodeStatus = Field(default=EpisodeStatus.WANTED, index=True)
    version: int = Field(default=1)
    release_title: Optional[str] = Field(default=None, nullable=True)
    release_group: Optional[str] = Field(default=None, nullable=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    feed_item_id: Optional[str] = Field(default=None, nullable=True)
    torrent_url: Optional[str] = Field(default=None, nullable=True)
    torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    operation_tag: Optional[str] = Field(default=None, nullable=True, index=True)
    downloaded_at: Optional[NaiveDatetime] = Field(default=None, nullable=True)
    last_error: Optional[str] = Field(default=None, nullable=True)
    retry_after: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    air_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    schedule_state: str = Field(default=EpisodeScheduleState.UNKNOWN.name.lower(), index=True)
    source_episode: Optional[int] = Field(default=None, nullable=True, index=True)
    last_attempt_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    attempt_count: int = Field(default=0)


class TorrentOperation(SQLModel, table=True):
    __tablename__ = "torrent_operations"
    __table_args__ = (
        Index("ix_torrent_operation_status_updated", "status", "updated_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    episode_id: int = Field(foreign_key="episodes.id", ondelete="CASCADE", index=True)
    kind: str = Field(index=True)
    status: TorrentOperationStatus = Field(default=TorrentOperationStatus.PREPARING, index=True)
    operation_tag: str = Field(unique=True, index=True)
    release_title: str
    # The release title reduced to its series name (episode, quality and group
    # tags stripped). ``release_title`` stays the raw filename for matching and
    # replacement; this is what the show learns as its naming pattern.
    parsed_title: Optional[str] = Field(default=None, nullable=True)
    release_group: Optional[str] = Field(default=None, nullable=True)
    version: int = Field(default=1)
    new_torrent_url: str
    new_torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    old_torrent_hash: Optional[str] = Field(default=None, nullable=True, index=True)
    old_release_title: Optional[str] = Field(default=None, nullable=True)
    old_release_group: Optional[str] = Field(default=None, nullable=True)
    old_torrent_url: Optional[str] = Field(default=None, nullable=True)
    old_feed_id: Optional[int] = Field(default=None, nullable=True, index=True)
    old_version: int = Field(default=1)
    old_episode_status: str = Field(default=EpisodeStatus.WANTED.name.lower())
    old_operation_tag: Optional[str] = Field(default=None, nullable=True)
    source_episode: Optional[int] = Field(default=None, nullable=True, index=True)
    feed_item_id: Optional[str] = Field(default=None, nullable=True, index=True)
    feed_id: Optional[int] = Field(default=None, nullable=True, index=True)
    attempt_count: int = Field(default=1)
    last_attempt_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    next_retry_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)
    ambiguous: bool = Field(default=False)
    last_error: Optional[str] = Field(default=None, nullable=True)
    created_at: NaiveDatetime = Field(default_factory=utc_now, index=True)
    updated_at: NaiveDatetime = Field(default_factory=utc_now, index=True)


class SeenFeedItem(SQLModel, table=True):
    __tablename__ = "seen_feed_items"
    __table_args__ = (
        UniqueConstraint("feed_url", "item_id", name="uq_seen_feed_item"),
        Index("ix_seen_feed_item_created", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    feed_url: str = Field(index=True)
    item_id: str = Field(index=True)
    title: str = Field(default="")
    created_at: NaiveDatetime = Field(default_factory=utc_now, index=True)
    shielded_at: Optional[NaiveDatetime] = Field(default=None, nullable=True, index=True)


class RuleHistory(SQLModel, table=True):
    __tablename__ = "rule_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: int = Field(foreign_key="monitored.id", ondelete="CASCADE", index=True)
    feed_id: Optional[int] = Field(default=None, foreign_key="feeds.id", ondelete="SET NULL", nullable=True, index=True)
    created_at: NaiveDatetime = Field(default_factory=utc_now, index=True)
    outcome: RuleOutcome = Field(default=RuleOutcome.PENDING, index=True)
    note: Optional[str] = Field(default=None, nullable=True)


class QbitRuleWatermark(SQLModel, table=True):
    __tablename__ = "qbit_rule_watermarks"

    rule_name: str = Field(primary_key=True)
    last_match: str = Field(default="")
    updated_at: NaiveDatetime = Field(default_factory=utc_now)


class MatchHistory(SQLModel, table=True):
    __tablename__ = "match_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    monitored_id: Optional[int] = Field(default=None, foreign_key="monitored.id", ondelete="SET NULL", nullable=True, index=True)
    show_name: str = Field(index=True)
    rule_name: str
    feed_name: Optional[str] = None
    release_title: str = Field(index=True)
    episode: Optional[int] = None
    created_at: NaiveDatetime = Field(default_factory=utc_now, index=True)
    matched_regex: Optional[str] = Field(default=None, nullable=True)

