//! What a service function works with: the database, qBittorrent, the time and the seams to the
//! rest of the app.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use jiff::Timestamp;
use kisetsu_clients::anilist::{AniListError, ListEntry, Schedules};
use kisetsu_clients::qbittorrent::Qbit;
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use kisetsu_db::rusqlite::{Connection, OptionalExtension};
use kisetsu_db::schema::{AppState, Settings};
use kisetsu_domain::matcher::Matcher;

use crate::error::{Error, Result};

/// The current time.
pub trait Clock {
    fn now(&self) -> Timestamp;
}

/// The system clock, at the precision the database keeps.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> Timestamp {
        kisetsu_db::storage::utc_now()
    }
}

/// Where desktop notifications go: a title, a body and the address of a cover image.
pub trait Notify {
    fn notify(&self, title: &str, body: &str, cover_url: Option<&str>);
}

/// The tags that mark a torrent while its hash is not known yet. Each is used once.
pub trait TagSource {
    fn next_tag(&mut self) -> String;
}

/// The feeds Kisetsu reads, and the sites behind them that can be searched.
pub trait FeedSource {
    fn fetch(&self, url: &str) -> std::result::Result<Vec<FeedArticle>, FeedFetchError>;

    /// The items on the feed's site that match the term, or `None` when the site cannot be searched.
    fn search(
        &self,
        feed_url: &str,
        term: &str,
    ) -> std::result::Result<Option<Vec<FeedArticle>>, FeedFetchError>;
}

/// AniList, as far as the services ask it questions.
pub trait AniListSource {
    fn user_exists(&self, username: &str) -> std::result::Result<bool, AniListError>;

    fn fetch_user_list(
        &self,
        username: &str,
        reuse_within: Option<f64>,
    ) -> std::result::Result<Vec<ListEntry>, AniListError>;

    fn fetch_user_seasonal_anime(
        &self,
        username: &str,
        monitored: &HashSet<i64>,
        reuse_within: Option<f64>,
    ) -> std::result::Result<Vec<ListEntry>, AniListError>;

    fn fetch_media_airing_schedules(
        &self,
        media_ids: &[i64],
    ) -> std::result::Result<Schedules, AniListError>;

    fn fetch_upcoming_airing_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> std::result::Result<Schedules, AniListError>;

    fn seconds_since_last_sync(&self, now: Timestamp) -> Option<f64>;

    fn is_sync_due(&self, min_age_seconds: f64, now: Timestamp) -> bool;
}

/// The matchers of the followed shows, rebuilt when `signatures` change.
#[derive(Debug)]
pub struct MatcherSet {
    /// What each matcher was built from, by show id.
    pub signatures: BTreeMap<i64, String>,
    pub matchers: Vec<(i64, Matcher)>,
}

/// In-memory state that outlives a single job. Everything here can be rebuilt from the database.
#[derive(Debug, Default)]
pub struct Caches {
    /// The `(feed id, guid)` of every stored feed item.
    pub known_items: Option<HashSet<(i64, String)>>,
    pub matchers: Option<Arc<MatcherSet>>,
    /// Shows whose grabs wait for their saved files to be matched to a feed, and since when.
    pub held: HashMap<i64, Timestamp>,
    /// By show: the feeds its searches leave out as not carrying it.
    pub skipped: HashMap<i64, HashSet<i64>>,
    /// By feed: the oldest release date its last successful read listed, and when that read was.
    pub read_windows: HashMap<i64, (Timestamp, Timestamp)>,
}

/// One database connection, qBittorrent and the time, for a unit of work.
///
/// The connection is a write transaction for a job that changes things; a service never commits.
pub struct Context<'a> {
    pub conn: &'a Connection,
    pub settings: Settings,
    pub qbit: Option<&'a mut dyn Qbit>,
    pub now: Timestamp,
    pub caches: &'a mut Caches,
    /// Parts of the UI whose data changed; the engine publishes them after the job commits.
    pub topics: BTreeSet<String>,
    pub notifier: Option<&'a dyn Notify>,
    pub download_tags: Option<&'a mut dyn TagSource>,
    pub feeds: Option<&'a dyn FeedSource>,
    pub anilist: Option<&'a dyn AniListSource>,
}

impl<'a> Context<'a> {
    /// A context at `now` with the settings row of `conn`, which is added when it is missing.
    pub fn new(conn: &'a Connection, now: Timestamp, caches: &'a mut Caches) -> Result<Self> {
        let settings = match Settings::find(conn, "WHERE id = 1", [])? {
            Some(settings) => settings,
            None => {
                let settings = Settings::default();
                settings.insert(conn)?;
                settings
            }
        };
        Ok(Self {
            conn,
            settings,
            qbit: None,
            now,
            caches,
            topics: BTreeSet::new(),
            notifier: None,
            download_tags: None,
            feeds: None,
            anilist: None,
        })
    }

    pub fn with_qbit(mut self, qbit: &'a mut dyn Qbit) -> Self {
        self.qbit = Some(qbit);
        self
    }

    pub fn with_notifier(mut self, notifier: &'a dyn Notify) -> Self {
        self.notifier = Some(notifier);
        self
    }

    pub fn with_download_tags(mut self, tags: &'a mut dyn TagSource) -> Self {
        self.download_tags = Some(tags);
        self
    }

    pub fn with_feeds(mut self, feeds: &'a dyn FeedSource) -> Self {
        self.feeds = Some(feeds);
        self
    }

    pub fn with_anilist(mut self, anilist: &'a dyn AniListSource) -> Self {
        self.anilist = Some(anilist);
        self
    }

    /// Marks parts of the UI as changed.
    pub fn emit(&mut self, topics: &[&str]) {
        self.topics
            .extend(topics.iter().map(|topic| (*topic).to_owned()));
    }

    /// How early before its air time an episode may be released, in hours.
    pub fn tolerance_hours(&self) -> i64 {
        self.settings.early_air_tolerance_hours.max(0)
    }

    /// qBittorrent, for a service that cannot do its work without it.
    pub fn qbit(&mut self) -> Result<&mut dyn Qbit> {
        match self.qbit.as_deref_mut() {
            Some(qbit) => Ok(qbit),
            None => Err(Error::NoQbit),
        }
    }
}

pub fn get_state(conn: &Connection, key: &str, default: &str) -> Result<String> {
    let value = conn
        .prepare_cached("SELECT value FROM app_state WHERE key = ?")?
        .query_row([key], |row| row.get(0))
        .optional()?;
    Ok(value.unwrap_or_else(|| default.to_owned()))
}

pub fn set_state(conn: &Connection, key: &str, value: &str) -> Result<()> {
    let row = AppState {
        key: key.to_owned(),
        value: value.to_owned(),
    };
    if row.update(conn)? == 0 {
        row.insert(conn)?;
    }
    Ok(())
}
