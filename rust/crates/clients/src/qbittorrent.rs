//! The qBittorrent Web API client: torrents, and the feed list of its RSS tree (read only).

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Arc;

use jiff::Timestamp;
use serde::Deserialize;
use serde::de::{self, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};
use std::fmt;
use thiserror::Error;

use crate::fetch::{Clock, SystemClock};
use crate::net::{Failure, classify};
use crate::traffic::{self, Outcome};

const CONNECT_TIMEOUT_SECONDS: u64 = 3;
const READ_TIMEOUT_SECONDS: u64 = 10;
/// While qBittorrent is unreachable, calls fail at once for this long (in seconds), doubling per failure.
const UNAVAILABLE_BACKOFF_SECONDS: [f64; 4] = [30.0, 60.0, 120.0, 300.0];
/// A rejected login is not retried for a while: qBittorrent bans an address after a few failed logins.
const AUTH_BACKOFF_SECONDS: f64 = 300.0;
/// qBittorrent's default save folder is asked again after this long.
pub const DEFAULT_PATH_TTL_SECONDS: f64 = 300.0;
/// The first Web API version that names its actions `stop` and `start` instead of `pause` and `resume`.
const STOP_START_API: (u32, u32) = (2, 11);

/// Why a qBittorrent call failed.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum QbitError {
    /// qBittorrent is unreachable, or the client is backing off after it was.
    #[error("{0}")]
    Unavailable(String),
    /// qBittorrent rejected the configured credentials.
    #[error("{0}")]
    Auth(String),
    /// qBittorrent answered 409: what the request names is taken or missing.
    #[error("{0}")]
    Conflict(String),
    /// qBittorrent already has this torrent.
    #[error("{0}")]
    Duplicate(String),
    /// qBittorrent refused to add the torrent.
    #[error("{0}")]
    Rejected(String),
    /// Anything else: an answer that makes no sense, or an error status.
    #[error("{0}")]
    Other(String),
}

pub type QbitResult<T> = Result<T, QbitError>;

/// A torrent as qBittorrent reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct Torrent {
    pub hash: String,
    pub name: String,
    pub state: String,
    pub progress: f64,
    pub amount_left: i64,
    pub size: i64,
    pub save_path: String,
    pub content_path: String,
    pub category: String,
    pub tags: BTreeSet<String>,
    pub added_on: Option<Timestamp>,
    pub completion_on: Option<Timestamp>,
    /// When data last moved; `None` for a torrent that has never been active.
    pub last_activity: Option<Timestamp>,
}

impl Torrent {
    pub fn finished(&self) -> bool {
        self.amount_left == 0 && self.progress >= 1.0
    }

    pub fn errored(&self) -> bool {
        matches!(
            self.state.to_lowercase().as_str(),
            "error" | "missingfiles" | "unknown"
        )
    }

    pub fn checking(&self) -> bool {
        let state = self.state.to_lowercase();
        state.starts_with("checking") || state == "moving"
    }

    /// The torrent a hash and qBittorrent's fields make.
    pub fn from_raw(info_hash: &str, raw: &Map<String, Value>) -> Self {
        let text = |key: &str| {
            raw.get(key)
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned()
        };
        let number = |key: &str| raw.get(key).and_then(Value::as_f64).unwrap_or(0.0);
        let state = text("state");
        Self {
            hash: info_hash.to_lowercase(),
            name: text("name"),
            state: if state.is_empty() {
                "unknown".to_owned()
            } else {
                state
            },
            progress: number("progress"),
            amount_left: number("amount_left") as i64,
            size: number("size") as i64,
            save_path: text("save_path"),
            content_path: text("content_path"),
            category: text("category"),
            tags: text("tags")
                .split(',')
                .map(str::trim)
                .filter(|tag| !tag.is_empty())
                .map(str::to_owned)
                .collect(),
            added_on: timestamp(raw.get("added_on")),
            completion_on: timestamp(raw.get("completion_on")),
            last_activity: timestamp(raw.get("last_activity")),
        }
    }
}

fn timestamp(value: Option<&Value>) -> Option<Timestamp> {
    let seconds = value?.as_f64().filter(|seconds| *seconds > 0.0)?;
    Timestamp::from_microsecond((seconds * 1e6).round() as i64).ok()
}

/// The fields of a torrent the client keeps.
const NEEDED_FIELDS: [&str; 12] = [
    "name",
    "state",
    "progress",
    "amount_left",
    "size",
    "save_path",
    "content_path",
    "category",
    "tags",
    "added_on",
    "completion_on",
    "last_activity",
];

/// What one sync changed.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncResult {
    pub full: bool,
    pub changed: BTreeSet<String>,
    pub removed: BTreeSet<String>,
}

impl SyncResult {
    /// Whether the sync brought anything.
    pub fn has_changes(&self) -> bool {
        self.full || !self.changed.is_empty() || !self.removed.is_empty()
    }
}

/// The torrents as the last syncs left them, built from qBittorrent's incremental answers.
#[derive(Debug, Default)]
pub struct TorrentView {
    rid: i64,
    raw: HashMap<String, Map<String, Value>>,
    torrents: BTreeMap<String, Torrent>,
    synced: bool,
}

impl TorrentView {
    /// The `rid` to ask the next incremental answer from.
    pub fn rid(&self) -> i64 {
        self.rid
    }

    /// Makes the next request ask for everything.
    pub fn restart(&mut self) {
        self.rid = 0;
    }

    /// Every torrent as of the last sync, by lowercase info hash.
    pub fn torrents(&self) -> &BTreeMap<String, Torrent> {
        &self.torrents
    }

    pub fn synced(&self) -> bool {
        self.synced
    }

    /// Merges one `sync/maindata` answer.
    pub fn apply(&mut self, data: &Value) -> SyncResult {
        self.rid = data
            .get("rid")
            .and_then(Value::as_f64)
            .map_or(self.rid, |rid| rid as i64);
        let empty = Map::new();
        let incoming = data
            .get("torrents")
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let full = data
            .get("full_update")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let mut removed: BTreeSet<String> = data
            .get("torrents_removed")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_lowercase)
            .collect();
        let mut changed = BTreeSet::new();
        if full {
            let present: BTreeSet<String> =
                incoming.keys().map(|hash| hash.to_lowercase()).collect();
            removed.extend(
                self.raw
                    .keys()
                    .filter(|hash| !present.contains(*hash))
                    .cloned(),
            );
            self.raw.clear();
        }
        for (info_hash, fields) in incoming {
            let key = info_hash.to_lowercase();
            let entry = self.raw.entry(key.clone()).or_default();
            if let Some(fields) = fields.as_object() {
                for name in NEEDED_FIELDS {
                    if let Some(value) = fields.get(name) {
                        entry.insert(name.to_owned(), value.clone());
                    }
                }
            }
            changed.insert(key);
        }
        for key in &removed {
            self.raw.remove(key);
            self.torrents.remove(key);
        }
        for key in &changed {
            if let Some(raw) = self.raw.get(key) {
                self.torrents
                    .insert(key.clone(), Torrent::from_raw(key, raw));
            }
        }
        self.synced = true;
        SyncResult {
            full,
            changed,
            removed,
        }
    }
}

/// A feed in qBittorrent's RSS tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RssStoreFeed {
    pub path: String,
    pub url: String,
}

/// A node of qBittorrent's RSS tree, its folders keeping the order they were written in.
enum Node {
    Folder(Vec<(String, Node)>),
    Leaf(Value),
}

impl<'de> Deserialize<'de> for Node {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Nodes;

        impl<'de> Visitor<'de> for Nodes {
            type Value = Node;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("any JSON value")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Node, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry::<String, Node>()? {
                    entries.push(entry);
                }
                Ok(Node::Folder(entries))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Node, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(Node::Leaf(Value::Null))
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Node, E> {
                Ok(Node::Leaf(Value::Bool(value)))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Node, E> {
                Ok(Node::Leaf(value.into()))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Node, E> {
                Ok(Node::Leaf(value.into()))
            }

            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Node, E> {
                Ok(Node::Leaf(Value::from(value)))
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Node, E> {
                Ok(Node::Leaf(Value::from(value)))
            }

            fn visit_unit<E: de::Error>(self) -> Result<Node, E> {
                Ok(Node::Leaf(Value::Null))
            }
        }

        deserializer.deserialize_any(Nodes)
    }
}

/// Every feed of qBittorrent's RSS tree (the body of `rss/items`), in tree order, with folders
/// joined by backslashes. Text that is no tree has none.
pub fn rss_feeds_of(body: &str) -> Vec<RssStoreFeed> {
    fn walk(node: &[(String, Node)], prefix: &str, feeds: &mut Vec<RssStoreFeed>) {
        for (name, child) in node {
            let Node::Folder(entries) = child else {
                continue;
            };
            let path = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}\\{name}")
            };
            match entries.iter().find(|(key, _)| key == "url") {
                Some((_, Node::Leaf(url))) => feeds.push(RssStoreFeed {
                    path,
                    url: url.as_str().map_or_else(|| url.to_string(), str::to_owned),
                }),
                Some(_) => {}
                None => walk(entries, &path, feeds),
            }
        }
    }
    let mut feeds = Vec::new();
    if let Ok(Node::Folder(entries)) = serde_json::from_str(body) {
        walk(&entries, "", &mut feeds);
    }
    feeds
}

/// Whether qBittorrent's reply to an add says it refused the torrent.
///
/// Newer versions answer with a failure count, older ones with the text `Fails.`.
pub fn add_failed(reply: &str) -> bool {
    if let Some(count) = serde_json::from_str::<Value>(reply)
        .ok()
        .and_then(|json| json.get("failure_count").and_then(Value::as_i64))
    {
        return count > 0;
    }
    let text = reply.trim().to_lowercase();
    if text == "fails." || text == "fails" {
        return true;
    }
    text.find("failure_count")
        .map(|at| {
            text[at + "failure_count".len()..]
                .trim_start_matches(['\'', '"'])
                .trim_start()
                .strip_prefix(':')
                .map(|rest| {
                    rest.trim_start()
                        .chars()
                        .take_while(char::is_ascii_digit)
                        .collect::<String>()
                })
                .and_then(|digits| digits.parse::<u64>().ok())
                .is_some_and(|count| count > 0)
        })
        .unwrap_or(false)
}

/// How a torrent's files are laid out in its folder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentLayout {
    Original,
    Subfolder,
    NoSubfolder,
}

impl ContentLayout {
    fn as_str(self) -> &'static str {
        match self {
            Self::Original => "Original",
            Self::Subfolder => "Subfolder",
            Self::NoSubfolder => "NoSubfolder",
        }
    }
}

/// A torrent to add by URL to an explicit path.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct AddTorrent {
    pub url: String,
    pub save_path: String,
    pub category: String,
    pub tags: Vec<String>,
    pub paused: bool,
    /// 0 stops the torrent as soon as it finishes; `None` leaves qBittorrent's own limit alone.
    pub ratio_limit: Option<f64>,
    /// Overrides qBittorrent's own setting for this torrent.
    pub content_layout: Option<ContentLayout>,
}

impl AddTorrent {
    pub fn new(url: impl Into<String>) -> Self {
        Self {
            url: url.into(),
            ..Self::default()
        }
    }
}

/// The versions qBittorrent reports.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Versions {
    pub app_version: String,
    pub api_version: String,
}

/// A qBittorrent as the rest of the app uses it: the real one, the sandbox's and test fakes.
///
/// Methods run on the engine thread. A call never sleeps: while qBittorrent is down it fails at
/// once until its backoff has passed.
pub trait Qbit {
    /// Verifies the credentials and returns the application and Web API versions.
    fn versions(&mut self) -> QbitResult<Versions>;
    /// Brings the torrent view up to date with one incremental request.
    fn sync(&mut self, full: bool) -> QbitResult<SyncResult>;
    /// Every torrent as of the last sync, by lowercase info hash.
    fn torrents(&self) -> &BTreeMap<String, Torrent>;
    /// Whether a sync has completed.
    fn synced(&self) -> bool;
    fn find_by_tag(&self, tag: &str) -> Option<&Torrent> {
        self.torrents()
            .values()
            .find(|torrent| torrent.tags.contains(tag))
    }
    /// The torrent's files, as paths within its save folder.
    fn files(&mut self, info_hash: &str) -> QbitResult<Vec<String>>;
    fn add_torrent(&mut self, add: &AddTorrent) -> QbitResult<()>;
    fn pause(&mut self, hashes: &[String]) -> QbitResult<()>;
    fn resume(&mut self, hashes: &[String]) -> QbitResult<()>;
    fn recheck(&mut self, hashes: &[String]) -> QbitResult<()>;
    fn delete(&mut self, hashes: &[String], delete_files: bool) -> QbitResult<()>;
    fn set_location(&mut self, hashes: &[String], location: &str) -> QbitResult<()>;
    fn add_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()>;
    fn remove_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()>;
    /// qBittorrent's default folder. It is asked once in a while, not for every add of a pass.
    fn default_save_path(&mut self) -> QbitResult<String>;
    fn tags(&mut self) -> QbitResult<Vec<String>>;
    fn delete_tags(&mut self, tags: &[String]) -> QbitResult<()>;
    fn ensure_category(&mut self, category: &str) -> QbitResult<()>;
    /// Every feed in qBittorrent's RSS tree.
    fn rss_feeds(&mut self) -> QbitResult<Vec<RssStoreFeed>>;
    /// Forgets the connection; the next call logs in again.
    fn close(&mut self);
}

/// Why qBittorrent could not be reached, in plain words.
fn unreachable_text(host: &str, failure: &Failure) -> String {
    match failure {
        Failure::Timeout => format!("qBittorrent at {host} did not answer in time."),
        Failure::HostNotFound => {
            format!("The address of {host} could not be found. Check the host name.")
        }
        Failure::Refused => format!(
            "Nothing answered at {host}. Check that qBittorrent is running with its Web UI on (Options, Web UI) and that the address and port are right."
        ),
        Failure::Tls(_) => {
            format!("The secure connection to {host} failed. Try http:// instead of https://.")
        }
        Failure::Other(detail) => format!("Cannot reach qBittorrent at {host}: {detail}"),
    }
}

struct Reply {
    status: u16,
    body: String,
    cookie: Option<String>,
}

#[derive(Default)]
struct Request {
    post: bool,
    path: String,
    query: Vec<(&'static str, String)>,
    form: Vec<(&'static str, String)>,
    multipart: bool,
}

impl Request {
    fn get(path: &str) -> Self {
        Self {
            path: path.to_owned(),
            ..Self::default()
        }
    }

    fn post(path: &str) -> Self {
        Self {
            post: true,
            path: path.to_owned(),
            ..Self::default()
        }
    }

    fn query(mut self, name: &'static str, value: impl Into<String>) -> Self {
        self.query.push((name, value.into()));
        self
    }

    fn field(mut self, name: &'static str, value: impl Into<String>) -> Self {
        self.form.push((name, value.into()));
        self
    }
}

fn hashes_field(hashes: &[String]) -> String {
    hashes.join("|")
}

/// One logged-in connection to qBittorrent.
pub struct QbitClient {
    host: String,
    base_url: String,
    username: String,
    password: String,
    agent: ureq::Agent,
    clock: Arc<dyn Clock>,
    cookie: Option<String>,
    api_version: Option<(u32, u32)>,
    failures: usize,
    down_until: f64,
    down_error: Option<QbitError>,
    view: TorrentView,
    default_path: Option<(f64, String)>,
}

impl QbitClient {
    pub fn new(host: &str, username: &str, password: &str) -> Self {
        Self::with_clock(host, username, password, Arc::<SystemClock>::default())
    }

    pub fn with_clock(host: &str, username: &str, password: &str, clock: Arc<dyn Clock>) -> Self {
        let base_url = if host.contains("://") {
            host.trim_end_matches('/').to_owned()
        } else {
            format!("http://{}", host.trim_end_matches('/'))
        };
        let tls = ureq::tls::TlsConfig::builder()
            .disable_verification(true)
            .build();
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .tls_config(tls)
            .timeout_connect(Some(std::time::Duration::from_secs(
                CONNECT_TIMEOUT_SECONDS,
            )))
            .timeout_per_read(Some(std::time::Duration::from_secs(READ_TIMEOUT_SECONDS)))
            .build()
            .new_agent();
        Self {
            host: host.to_owned(),
            base_url,
            username: username.to_owned(),
            password: password.to_owned(),
            agent,
            clock,
            cookie: None,
            api_version: None,
            failures: 0,
            down_until: 0.0,
            down_error: None,
            view: TorrentView::default(),
            default_path: None,
        }
    }

    // -- connection ---------------------------------------------------------

    fn backoff(&mut self, seconds: f64, error: &QbitError) {
        self.down_until = self.clock.now() + seconds;
        self.down_error = Some(error.clone());
    }

    fn exchange(&self, request: &Request, cookie: Option<&str>) -> Result<Reply, Failure> {
        let url = format!("{}/api/v2/{}", self.base_url, request.path);
        let response = if request.post {
            let mut builder = self.agent.post(&url);
            if let Some(cookie) = cookie {
                builder = builder.header("Cookie", cookie);
            }
            if request.multipart {
                builder
                    .content_type(format!("multipart/form-data; boundary={BOUNDARY}"))
                    .send(multipart_body(&request.form))
            } else {
                builder.send_form(
                    request
                        .form
                        .iter()
                        .map(|(name, value)| (*name, value.as_str())),
                )
            }
        } else {
            let mut builder = self.agent.get(&url);
            if let Some(cookie) = cookie {
                builder = builder.header("Cookie", cookie);
            }
            for (name, value) in &request.query {
                builder = builder.query(*name, value);
            }
            builder.call()
        };
        let mut response = response.map_err(|error| classify(&error))?;
        let status = response.status().as_u16();
        let cookie = response
            .headers()
            .get_all("set-cookie")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .filter_map(|value| value.split(';').next())
            .map(str::trim)
            .find(|pair| pair.contains('='))
            .map(str::to_owned);
        let body = response
            .body_mut()
            .read_to_string()
            .map_err(|error| classify(&error))?;
        Ok(Reply {
            status,
            body,
            cookie,
        })
    }

    fn login(&mut self) -> QbitResult<String> {
        let kind = "qBittorrent login";
        let host = traffic::host_of(&self.host);
        let request = Request::post("auth/login")
            .field("username", self.username.clone())
            .field("password", self.password.clone());
        let reply = match self.exchange(&request, None) {
            Ok(reply) => reply,
            Err(failure) => {
                traffic::record(kind, &host, Outcome::Error);
                return Err(self.unavailable(&failure));
            }
        };
        let accepted = reply.status == 200 && matches!(reply.body.as_str(), "" | "Ok.");
        match (accepted, reply.cookie) {
            (true, Some(cookie)) => {
                traffic::record(kind, &host, Outcome::Ok);
                Ok(cookie)
            }
            (true, None) => {
                traffic::record(kind, &host, Outcome::Ok);
                Ok(String::new())
            }
            (false, _) => {
                traffic::record(kind, &host, Outcome::Error);
                Err(self.refused(reply.status, &reply.body))
            }
        }
    }

    /// The session cookie, logging in first when there is none and no backoff holds.
    fn connect(&mut self) -> QbitResult<String> {
        if let Some(cookie) = &self.cookie {
            return Ok(cookie.clone());
        }
        let remaining = self.down_until - self.clock.now();
        if remaining > 0.0 {
            if let Some(error) = &self.down_error {
                let note = format!(
                    "{error} (not retrying for another {}s)",
                    remaining as u64 + 1
                );
                return Err(match error {
                    QbitError::Auth(_) => QbitError::Auth(note),
                    _ => QbitError::Unavailable(note),
                });
            }
        }
        let cookie = self.login()?;
        self.cookie = Some(cookie.clone());
        Ok(cookie)
    }

    fn unavailable(&mut self, failure: &Failure) -> QbitError {
        self.cookie = None;
        let error = QbitError::Unavailable(unreachable_text(&self.host, failure));
        let step =
            UNAVAILABLE_BACKOFF_SECONDS[self.failures.min(UNAVAILABLE_BACKOFF_SECONDS.len() - 1)];
        self.backoff(step, &error);
        self.failures += 1;
        error
    }

    /// The error for an answer that refused the credentials, with the backoff that follows it.
    fn refused(&mut self, status: u16, body: &str) -> QbitError {
        self.cookie = None;
        if matches!(status, 502..=504) {
            return self.unavailable(&Failure::Other(format!("HTTP {status}")));
        }
        let text = if status == 403 {
            "qBittorrent refused this address, usually after too many failed logins. Wait a while or restart qBittorrent, then try again."
        } else if matches!(status, 200 | 401) {
            "qBittorrent rejected the username or password."
        } else {
            return QbitError::Other(format!("qBittorrent answered HTTP {status}: {body}"));
        };
        let error = QbitError::Auth(text.to_owned());
        self.backoff(AUTH_BACKOFF_SECONDS, &error);
        error
    }

    /// Sends one request and returns the body of a good answer.
    fn send(&mut self, write: bool, request: &Request) -> QbitResult<String> {
        let kind = format!(
            "qBittorrent {} ({})",
            if write { "change" } else { "read" },
            traffic::current_caller()
        );
        let host = traffic::host_of(&self.host);
        for attempt in 0..2 {
            let cookie = self.connect()?;
            let cookie = (!cookie.is_empty()).then_some(cookie);
            match self.exchange(request, cookie.as_deref()) {
                Err(failure) => {
                    traffic::record(&kind, &host, Outcome::Error);
                    return Err(self.unavailable(&failure));
                }
                // The login may have expired: log in again once.
                Ok(reply) if reply.status == 403 && attempt == 0 => {
                    self.cookie = None;
                }
                Ok(reply) if reply.status >= 400 => {
                    traffic::record(&kind, &host, Outcome::Error);
                    return Err(match reply.status {
                        409 => QbitError::Conflict(reply.body),
                        401 | 403 | 502..=504 => self.refused(reply.status, &reply.body),
                        status => QbitError::Other(format!(
                            "qBittorrent answered HTTP {status}: {}",
                            reply.body
                        )),
                    });
                }
                Ok(reply) => {
                    traffic::record(&kind, &host, Outcome::Ok);
                    self.failures = 0;
                    return Ok(reply.body);
                }
            }
        }
        Err(QbitError::Other(
            "qBittorrent kept refusing the session".to_owned(),
        ))
    }

    fn json(&mut self, request: &Request) -> QbitResult<Value> {
        let body = self.send(false, request)?;
        serde_json::from_str(&body).map_err(|error| {
            QbitError::Other(format!("qBittorrent sent an unreadable answer: {error}"))
        })
    }

    fn api_version(&mut self) -> QbitResult<(u32, u32)> {
        if let Some(version) = self.api_version {
            return Ok(version);
        }
        let text = self.send(false, &Request::get("app/webapiVersion"))?;
        let mut parts = text
            .trim()
            .trim_start_matches('v')
            .split('.')
            .map(|part| part.parse::<u32>());
        let version = match (parts.next(), parts.next()) {
            (Some(Ok(major)), Some(Ok(minor))) => (major, minor),
            _ => (0, 0),
        };
        self.api_version = Some(version);
        Ok(version)
    }

    fn change(
        &mut self,
        path: &str,
        hashes: &[String],
        extra: &[(&'static str, String)],
    ) -> QbitResult<()> {
        if hashes.is_empty() {
            return Ok(());
        }
        let mut request = Request::post(path).field("hashes", hashes_field(hashes));
        for (name, value) in extra {
            request = request.field(name, value.clone());
        }
        self.send(true, &request).map(drop)
    }
}

const BOUNDARY: &str = "----kisetsu-5d1c0f3a9b7e";

fn multipart_body(fields: &[(&'static str, String)]) -> Vec<u8> {
    let mut body = Vec::new();
    for (name, value) in fields {
        body.extend_from_slice(
            format!(
                "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            )
            .as_bytes(),
        );
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}

impl Qbit for QbitClient {
    fn versions(&mut self) -> QbitResult<Versions> {
        let app_version = self.send(false, &Request::get("app/version"))?;
        let api_version = self.send(false, &Request::get("app/webapiVersion"))?;
        Ok(Versions {
            app_version: app_version.trim().to_owned(),
            api_version: api_version.trim().to_owned(),
        })
    }

    fn sync(&mut self, full: bool) -> QbitResult<SyncResult> {
        if full {
            self.view.restart();
        }
        let request = Request::get("sync/maindata").query("rid", self.view.rid().to_string());
        let data = self.json(&request)?;
        Ok(self.view.apply(&data))
    }

    fn torrents(&self) -> &BTreeMap<String, Torrent> {
        self.view.torrents()
    }

    fn synced(&self) -> bool {
        self.view.synced()
    }

    fn files(&mut self, info_hash: &str) -> QbitResult<Vec<String>> {
        let files = self.json(&Request::get("torrents/files").query("hash", info_hash))?;
        Ok(files
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|file| file.get("name").and_then(Value::as_str))
            .map(str::to_owned)
            .collect())
    }

    fn add_torrent(&mut self, add: &AddTorrent) -> QbitResult<()> {
        let paused = add.paused.to_string();
        let mut request = Request::post("torrents/add").field("urls", add.url.clone());
        request.multipart = true;
        if !add.category.is_empty() {
            request = request.field("category", add.category.clone());
        }
        if !add.tags.is_empty() {
            request = request.field("tags", add.tags.join(","));
        }
        if !add.save_path.is_empty() {
            request = request.field("savepath", add.save_path.clone());
        }
        request = request
            .field("paused", paused.clone())
            .field("stopped", paused);
        if let Some(layout) = add.content_layout {
            request = request.field("contentLayout", layout.as_str());
        }
        request = request.field("autoTMM", "false");
        if let Some(limit) = add.ratio_limit {
            request = request.field("ratioLimit", limit.to_string());
        }
        request = request.field("shareLimitAction", "Stop");
        let reply = match self.send(true, &request) {
            Ok(reply) => reply,
            Err(QbitError::Conflict(_)) => {
                return Err(QbitError::Duplicate(format!(
                    "qBittorrent already has '{}'",
                    add.url
                )));
            }
            Err(error) => return Err(error),
        };
        if add_failed(&reply) {
            return Err(QbitError::Rejected(format!(
                "qBittorrent rejected torrent '{}': {reply}",
                add.url
            )));
        }
        Ok(())
    }

    fn pause(&mut self, hashes: &[String]) -> QbitResult<()> {
        let path = if self.api_version()? >= STOP_START_API {
            "torrents/stop"
        } else {
            "torrents/pause"
        };
        self.change(path, hashes, &[])
    }

    fn resume(&mut self, hashes: &[String]) -> QbitResult<()> {
        let path = if self.api_version()? >= STOP_START_API {
            "torrents/start"
        } else {
            "torrents/resume"
        };
        self.change(path, hashes, &[])
    }

    fn recheck(&mut self, hashes: &[String]) -> QbitResult<()> {
        self.change("torrents/recheck", hashes, &[])
    }

    fn delete(&mut self, hashes: &[String], delete_files: bool) -> QbitResult<()> {
        self.change(
            "torrents/delete",
            hashes,
            &[("deleteFiles", delete_files.to_string())],
        )
    }

    fn set_location(&mut self, hashes: &[String], location: &str) -> QbitResult<()> {
        if location.is_empty() {
            return Ok(());
        }
        self.change(
            "torrents/setLocation",
            hashes,
            &[("location", location.to_owned())],
        )
    }

    fn add_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        if tags.is_empty() {
            return Ok(());
        }
        self.change("torrents/addTags", hashes, &[("tags", tags.join(","))])
    }

    fn remove_tags(&mut self, hashes: &[String], tags: &[String]) -> QbitResult<()> {
        if tags.is_empty() {
            return Ok(());
        }
        self.change("torrents/removeTags", hashes, &[("tags", tags.join(","))])
    }

    fn default_save_path(&mut self) -> QbitResult<String> {
        if let Some((asked_at, path)) = &self.default_path {
            if self.clock.now() - asked_at <= DEFAULT_PATH_TTL_SECONDS {
                return Ok(path.clone());
            }
        }
        let path = self
            .send(false, &Request::get("app/defaultSavePath"))?
            .trim()
            .to_owned();
        self.default_path = Some((self.clock.now(), path.clone()));
        Ok(path)
    }

    fn tags(&mut self) -> QbitResult<Vec<String>> {
        let tags = self.json(&Request::get("torrents/tags"))?;
        Ok(tags
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect())
    }

    fn delete_tags(&mut self, tags: &[String]) -> QbitResult<()> {
        if tags.is_empty() {
            return Ok(());
        }
        let request = Request::post("torrents/deleteTags").field("tags", tags.join(","));
        self.send(true, &request).map(drop)
    }

    fn ensure_category(&mut self, category: &str) -> QbitResult<()> {
        if category.trim().is_empty() {
            return Ok(());
        }
        let known = self.send(true, &Request::get("torrents/categories"))?;
        let exists = serde_json::from_str::<Value>(&known)
            .ok()
            .and_then(|categories| categories.get(category).map(|_| ()))
            .is_some();
        if !exists {
            let request = Request::post("torrents/createCategory").field("category", category);
            self.send(true, &request)?;
        }
        Ok(())
    }

    fn rss_feeds(&mut self) -> QbitResult<Vec<RssStoreFeed>> {
        let tree = self.send(false, &Request::get("rss/items").query("withData", "false"))?;
        Ok(rss_feeds_of(&tree))
    }

    fn close(&mut self) {
        self.cookie = None;
        self.api_version = None;
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::Mutex;

    use super::*;

    struct FakeClock(Mutex<f64>);

    impl Clock for FakeClock {
        fn now(&self) -> f64 {
            *self.0.lock().unwrap()
        }

        fn sleep(&self, _seconds: f64) {}
    }

    type Log = Arc<Mutex<Vec<String>>>;

    /// A qBittorrent that answers `(status, body)` per request line (`POST /path`) and logs
    /// every request line with its body.
    fn serve(answer: impl Fn(&str) -> (u16, String) + Send + 'static) -> (String, Log) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let log: Log = Arc::default();
        let seen = Arc::clone(&log);
        std::thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut data = Vec::new();
                let mut chunk = [0u8; 4096];
                let (head_end, length) = loop {
                    let n = stream.read(&mut chunk).unwrap_or(0);
                    data.extend_from_slice(&chunk[..n]);
                    let text = String::from_utf8_lossy(&data).into_owned();
                    if let Some(end) = text.find("\r\n\r\n") {
                        let length = text[..end]
                            .lines()
                            .find_map(|line| {
                                line.to_lowercase()
                                    .strip_prefix("content-length:")
                                    .map(|v| v.trim().parse().unwrap_or(0))
                            })
                            .unwrap_or(0usize);
                        break (end + 4, length);
                    }
                    if n == 0 {
                        break (data.len(), 0);
                    }
                };
                while data.len() < head_end + length {
                    let n = stream.read(&mut chunk).unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    data.extend_from_slice(&chunk[..n]);
                }
                let text = String::from_utf8_lossy(&data).into_owned();
                let line = text.lines().next().unwrap_or("").replace(" HTTP/1.1", "");
                let key = line.split('?').next().unwrap_or("").to_owned();
                seen.lock()
                    .unwrap()
                    .push(format!("{line} {}", &text[head_end.min(text.len())..]));
                let (status, body) = answer(&key);
                let cookie = if key.ends_with("auth/login") {
                    "Set-Cookie: SID=abc; HttpOnly\r\n"
                } else {
                    ""
                };
                let reply = format!(
                    "HTTP/1.1 {status} X\r\n{cookie}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        (url, log)
    }

    fn client(url: &str) -> (QbitClient, Arc<FakeClock>) {
        let clock = Arc::new(FakeClock(Mutex::new(1000.0)));
        (QbitClient::with_clock(url, "u", "p", clock.clone()), clock)
    }

    #[test]
    fn a_rejected_login_is_an_auth_error_that_is_not_retried_for_a_while() {
        let (url, log) = serve(|_| (200, "Fails.".to_owned()));
        let (mut client, clock) = client(&url);
        assert!(
            matches!(client.versions(), Err(QbitError::Auth(text)) if text.contains("username or password"))
        );
        let again = client.versions().unwrap_err();
        assert!(
            matches!(&again, QbitError::Auth(text) if text.contains("not retrying")),
            "{again}"
        );
        assert_eq!(log.lock().unwrap().len(), 1);
        *clock.0.lock().unwrap() += 301.0;
        assert!(client.versions().is_err());
        assert_eq!(log.lock().unwrap().len(), 2);
    }

    #[test]
    fn an_unreachable_qbit_backs_off_with_growing_steps() {
        let url = {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            format!("http://{}", listener.local_addr().unwrap())
        };
        let (mut client, clock) = client(&url);
        let first = client.sync(false).unwrap_err();
        assert!(
            matches!(&first, QbitError::Unavailable(text) if text.starts_with("Nothing answered at")),
            "{first}"
        );
        assert!(
            matches!(client.sync(false), Err(QbitError::Unavailable(text)) if text.contains("not retrying"))
        );
        assert_eq!(client.down_until - 1000.0, 30.0);
        *clock.0.lock().unwrap() += 31.0;
        client.sync(false).unwrap_err();
        assert_eq!(client.down_until - 1031.0, 60.0);
    }

    #[test]
    fn a_sync_reads_torrents_and_a_pause_uses_the_action_name_of_the_api_version() {
        let (url, log) = serve(|key| {
            match key {
            "POST /api/v2/auth/login" => (200, "Ok.".to_owned()),
            "GET /api/v2/sync/maindata" => (
                200,
                r#"{"rid":3,"full_update":true,"torrents":{"ABC":{"name":"Show - 01.mkv","state":"stalledUP","progress":1,"amount_left":0}}}"#.to_owned(),
            ),
            "GET /api/v2/app/webapiVersion" => (200, "2.16.2".to_owned()),
            _ => (200, String::new()),
        }
        });
        let (mut client, _) = client(&url);
        let result = client.sync(false).unwrap();
        assert!(result.full && result.changed.contains("abc"));
        assert!(client.torrents()["abc"].finished());
        client.pause(&["abc".to_owned(), "def".to_owned()]).unwrap();
        client.pause(&[]).unwrap();
        let log = log.lock().unwrap();
        assert!(
            log.iter()
                .any(|line| line.starts_with("POST /api/v2/torrents/stop")
                    && line.ends_with("hashes=abc%7Cdef")),
            "{log:?}"
        );
        assert!(
            log.iter()
                .any(|line| line.starts_with("POST /api/v2/auth/login")
                    && line.ends_with("username=u&password=p"))
        );
    }

    #[test]
    fn adding_maps_a_409_to_a_duplicate_and_a_failure_count_to_a_rejection() {
        let (url, log) = serve(|key| match key {
            "POST /api/v2/auth/login" => (200, "Ok.".to_owned()),
            "POST /api/v2/torrents/add" => {
                (200, r#"{"failure_count":1,"success_count":0}"#.to_owned())
            }
            _ => (200, String::new()),
        });
        let (mut client, _) = client(&url);
        let mut add = AddTorrent::new("magnet:x");
        add.save_path = r"D:\Anime".to_owned();
        add.tags = vec!["a".to_owned(), "b".to_owned()];
        add.ratio_limit = Some(0.0);
        assert!(matches!(
            client.add_torrent(&add),
            Err(QbitError::Rejected(_))
        ));
        let log = log.lock().unwrap();
        let sent = log
            .iter()
            .find(|line| line.starts_with("POST /api/v2/torrents/add"))
            .unwrap();
        for field in [
            "name=\"urls\"\r\n\r\nmagnet:x",
            "name=\"tags\"\r\n\r\na,b",
            "name=\"ratioLimit\"\r\n\r\n0",
            "name=\"autoTMM\"\r\n\r\nfalse",
            "name=\"shareLimitAction\"\r\n\r\nStop",
        ] {
            assert!(sent.contains(field), "{field} missing in {sent}");
        }
        drop(log);

        let (url, _) = serve(|key| match key {
            "POST /api/v2/auth/login" => (200, "Ok.".to_owned()),
            _ => (409, String::new()),
        });
        let (mut client, _) = self::client(&url);
        assert!(matches!(
            client.add_torrent(&AddTorrent::new("magnet:x")),
            Err(QbitError::Duplicate(_))
        ));
    }
}
