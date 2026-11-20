//! The tables, with the same names and columns as in every existing database.

use jiff::Timestamp;
use kisetsu_domain::enums::EpisodeStatus;

use crate::storage::{decode_list, encode_list, stored_enum, table};

stored_enum! {
    FeedState {
        None => "NONE",
        Candidate => "CANDIDATE",
        Assigned => "ASSIGNED",
        Pinned => "PINNED",
        Locked => "LOCKED",
    }
}

stored_enum! {
    PausedBy {
        User => "USER",
        All => "ALL",
    }
}

stored_enum! {
    ScheduleState {
        Unknown => "UNKNOWN",
        Scheduled => "SCHEDULED",
        Aired => "AIRED",
        /// Never set today; kept so a stored row with it still loads.
        Postponed => "POSTPONED",
    }
}

stored_enum! {
    DownloadState {
        Adding => "ADDING",
        Verifying => "VERIFYING",
        Downloading => "DOWNLOADING",
        Done => "DONE",
        Superseded => "SUPERSEDED",
        RetryWait => "RETRY_WAIT",
        /// Two kinds: qBittorrent reported the torrent errored (the hash is kept and it can go back
        /// to `Downloading`), or the download failed for good (the hash is cleared).
        Failed => "FAILED",
        Canceled => "CANCELED",
    }
}

stored_enum! {
    /// Why a canceled or failed download ended, for the retry ladders that count earlier endings.
    EndReason {
        AddGaveUp => "ADD_GAVE_UP",
        ReplacementGaveUp => "REPLACEMENT_GAVE_UP",
        Removed => "REMOVED",
        /// The user watched the episode on AniList, so its torrent is deleted with its files.
        Watched => "WATCHED",
    }
}

stored_enum! {
    OffsetSource {
        Inferred => "INFERRED",
        Confirmed => "CONFIRMED",
        Manual => "MANUAL",
    }
}

stored_enum! {
    EventKind {
        Grabbed => "GRABBED",
        Done => "DONE",
        Replaced => "REPLACED",
        Failed => "FAILED",
        Missed => "MISSED",
        Adopted => "ADOPTED",
        Feed => "FEED",
        Info => "INFO",
        Deleted => "DELETED",
    }
}

/// Downloads that still occupy a torrent slot or are waiting to be retried.
pub const ACTIVE_DOWNLOAD_STATES: [DownloadState; 4] = [
    DownloadState::Adding,
    DownloadState::Verifying,
    DownloadState::Downloading,
    DownloadState::RetryWait,
];

/// Feed states in which no automatic path may move a show to another feed.
pub const FIXED_FEED_STATES: [FeedState; 2] = [FeedState::Pinned, FeedState::Locked];

const DEFAULT_QBIT_HOST: &str = "http://localhost:8080";
const DEFAULT_QBIT_USERNAME: &str = "admin";
const DEFAULT_QBIT_PASSWORD: &str = "adminadmin";
const DEFAULT_SEED_RATIO: f64 = 1.0;
const DEFAULT_MIN_RESOLUTION: i64 = 1080;
const DEFAULT_ANILIST_SYNC_MINUTES: i64 = 360;
const DEFAULT_FEED_REFRESH_MINUTES: i64 = 15;
const DEFAULT_HUNT_INTERVAL_MINUTES: i64 = 5;
const DEFAULT_EARLY_AIR_TOLERANCE_HOURS: i64 = 6;

table! {
    Settings, "settings" {
        id: i64,
        qbit_host: String,
        qbit_username: String,
        qbit_password: String,
        base_dir: String,
        default_category: String,
        default_seed_ratio: f64,
        min_resolution: i64,
        anilist_username: String,
        anilist_sync_minutes: i64,
        feed_refresh_minutes: i64,
        hunt_interval_minutes: i64,
        early_air_tolerance_hours: i64,
        title_language: String,
        accent_color: String,
        accent_tint: String,
        desktop_notifications: bool,
        /// Episodes the user's AniList progress has passed are not downloaded, and are deleted once
        /// watched.
        delete_watched: bool,
    }
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            id: 1,
            qbit_host: DEFAULT_QBIT_HOST.to_owned(),
            qbit_username: DEFAULT_QBIT_USERNAME.to_owned(),
            qbit_password: DEFAULT_QBIT_PASSWORD.to_owned(),
            base_dir: String::new(),
            default_category: String::new(),
            default_seed_ratio: DEFAULT_SEED_RATIO,
            min_resolution: DEFAULT_MIN_RESOLUTION,
            anilist_username: String::new(),
            anilist_sync_minutes: DEFAULT_ANILIST_SYNC_MINUTES,
            feed_refresh_minutes: DEFAULT_FEED_REFRESH_MINUTES,
            hunt_interval_minutes: DEFAULT_HUNT_INTERVAL_MINUTES,
            early_air_tolerance_hours: DEFAULT_EARLY_AIR_TOLERANCE_HOURS,
            title_language: "english".to_owned(),
            accent_color: "#2dd4bf".to_owned(),
            accent_tint: "off".to_owned(),
            desktop_notifications: false,
            delete_watched: false,
        }
    }
}

table! {
    /// Runtime state the app keeps for itself, apart from what the user configures.
    AppState, "app_state" {
        key: String,
        value: String,
    }
}

table! {
    Feed, "feeds" {
        id: Option<i64>,
        name: String,
        url: String,
        /// Lower number = higher priority (1 is top).
        priority: i64,
        /// The item path in qBittorrent's RSS tree this feed was copied from; `None` when added here.
        qbit_path: Option<String>,
        last_fetch_at: Option<Timestamp>,
        last_ok_at: Option<Timestamp>,
        failing_since: Option<Timestamp>,
        last_error: Option<String>,
    }
}

table! {
    /// Every article seen on a feed. Which followed shows it matches is in `FeedItemMatch`.
    FeedItem, "feed_items" {
        id: Option<i64>,
        feed_id: i64,
        guid: String,
        info_hash: Option<String>,
        title: String,
        torrent_url: Option<String>,
        published_at: Option<Timestamp>,
        first_seen_at: Timestamp,
        episode_raw: Option<i64>,
        version: i64,
        release_group: Option<String>,
        parsed_title: Option<String>,
    }
}

table! {
    /// A feed item that belongs to a followed show. An item can belong to several (franchise overlaps).
    FeedItemMatch, "feed_item_matches" {
        item_id: i64,
        show_id: i64,
    }
}

table! {
    Show, "shows" {
        id: Option<i64>,
        anilist_id: i64,
        title_romaji: Option<String>,
        title_english: Option<String>,
        /// A JSON array of strings; see `aliases` and `set_aliases`.
        aliases_json: String,
        /// A JSON array of strings; see `custom_aliases` and `set_custom_aliases`.
        custom_aliases_json: String,
        cover_image: Option<String>,
        /// AniList's own colour for the cover, as `#rrggbb`.
        cover_color: Option<String>,
        banner_image: Option<String>,
        accent_hues: Option<String>,
        accent_src: Option<String>,
        /// The colour along the cover's bottom edge, `#rrggbb` (empty when it has none).
        cover_edge: Option<String>,
        /// The cover the edge colour was read from.
        cover_edge_src: Option<String>,
        season_name: Option<String>,
        season_year: Option<i64>,
        total_episodes: Option<i64>,
        anilist_status: Option<String>,
        list_status: Option<String>,
        next_airing_episode: Option<i64>,
        next_airing_at: Option<Timestamp>,
        schedule_synced_at: Option<Timestamp>,
        feed_id: Option<i64>,
        feed_state: FeedState,
        /// Only meaningful while `feed_state` is `Candidate`: when the lower-ranked feed was first seen.
        candidate_since: Option<Timestamp>,
        learned_name: Option<String>,
        learned_group: Option<String>,
        save_folder: String,
        category: Option<String>,
        seed_ratio: Option<f64>,
        /// The per-series tag put on this show's torrents in qBittorrent.
        qbit_tag: Option<String>,
        paused: bool,
        paused_by: Option<PausedBy>,
        completed: bool,
        last_backfill_at: Option<Timestamp>,
        /// The user's AniList progress (`None` until a sync reports it).
        watched: Option<i64>,
        /// Up to which episode the deletion of watched episodes has acted on `watched`.
        watched_applied: Option<i64>,
    }
}

impl Show {
    pub fn aliases(&self) -> Vec<String> {
        decode_list(&self.aliases_json)
    }

    pub fn set_aliases<S: AsRef<str>>(&mut self, aliases: &[S]) {
        self.aliases_json = encode_list(aliases);
    }

    pub fn custom_aliases(&self) -> Vec<String> {
        decode_list(&self.custom_aliases_json)
    }

    pub fn set_custom_aliases<S: AsRef<str>>(&mut self, aliases: &[S]) {
        self.custom_aliases_json = encode_list(aliases);
    }

    /// Everything a release may be matched against.
    pub fn effective_aliases(&self) -> Vec<String> {
        let mut all = self.aliases();
        all.extend(self.custom_aliases());
        let mut seen = std::collections::HashSet::new();
        all.retain(|alias| seen.insert(alias.clone()));
        all
    }

    /// The name in the configured language, falling back to the other one.
    pub fn title(&self, language: &str) -> String {
        let english = self
            .title_english
            .as_deref()
            .filter(|title| !title.is_empty());
        let romaji = self
            .title_romaji
            .as_deref()
            .filter(|title| !title.is_empty());
        match (language, english) {
            ("english", Some(title)) => title.to_owned(),
            _ => romaji
                .or(english)
                .map_or_else(|| format!("AniList {}", self.anilist_id), str::to_owned),
        }
    }

    pub fn feed_is_fixed(&self) -> bool {
        FIXED_FEED_STATES.contains(&self.feed_state)
    }
}

table! {
    Episode, "episodes" {
        id: Option<i64>,
        show_id: i64,
        number: i64,
        air_at: Option<Timestamp>,
        schedule_state: ScheduleState,
        status: EpisodeStatus,
        /// The download that currently supplies this episode. Not a foreign key: downloads point back
        /// at their episode, and a cycle would block table creation.
        current_download_id: Option<i64>,
        retry_after: Option<Timestamp>,
        last_error: Option<String>,
        attempts: i64,
    }
}

table! {
    /// One torrent Kisetsu added or adopted for an episode.
    Download, "downloads" {
        id: Option<i64>,
        episode_id: i64,
        feed_id: Option<i64>,
        feed_item_guid: Option<String>,
        title: String,
        parsed_title: Option<String>,
        release_group: Option<String>,
        version: i64,
        source_episode: Option<i64>,
        torrent_url: Option<String>,
        info_hash: Option<String>,
        /// Random tag put on the torrent while its hash is unknown; removed once the hash is.
        tag: Option<String>,
        state: DownloadState,
        supersedes_id: Option<i64>,
        adopted: bool,
        /// Whether the torrent is a whole-season batch, which a title without a batch marker can still be.
        batch: bool,
        attempts: i64,
        next_retry_at: Option<Timestamp>,
        last_error: Option<String>,
        end_reason: Option<EndReason>,
        created_at: Timestamp,
        updated_at: Timestamp,
        completed_at: Option<Timestamp>,
    }
}

table! {
    /// How a feed's episode numbers map onto the show's AniList numbers.
    EpisodeOffset, "episode_offsets" {
        id: Option<i64>,
        show_id: i64,
        feed_id: i64,
        offset: i64,
        source: OffsetSource,
        evidence_count: i64,
        first_evidence_at: Option<Timestamp>,
        last_evidence_at: Option<Timestamp>,
    }
}

table! {
    /// What the app did, shown in the History tab.
    Event, "events" {
        id: Option<i64>,
        at: Timestamp,
        kind: EventKind,
        show_id: Option<i64>,
        episode: Option<i64>,
        feed_id: Option<i64>,
        title: String,
        message: String,
    }
}

table! {
    /// A show the user deleted while it is still on their AniList list. Sync does not add it back.
    DismissedShow, "dismissed_shows" {
        anilist_id: i64,
        created_at: Timestamp,
    }
}
