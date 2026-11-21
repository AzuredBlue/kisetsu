//! Searching a feed's own site for releases that have scrolled out of its RSS.
//!
//! A feed lists only its newest items. When Kisetsu or the feed was down for a while, the episodes
//! released meanwhile can be gone from it. For feeds on nyaa and SubsPlease the site's search finds
//! them again.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::{LazyLock, Mutex, MutexGuard};

use jiff::Timestamp;
use regex::Regex;
use serde::Deserialize;
use serde::de::{Deserializer, MapAccess, Visitor};
use serde_json::{Value, json};

use crate::fetch::{Fetcher, RequestKind};
use crate::rss::{
    FeedArticle, FeedFetchError, magnet_parts, normalize_info_hash, parse_date, parse_feed,
};
use crate::url;

/// Search terms per show.
pub const MAX_TERMS: usize = 3;
/// How long a check of whether a site takes several names in one search holds, in seconds.
pub const GROUPING_RECHECK_SECONDS: f64 = 24.0 * 3600.0;

pub const SUBSPLEASE_SCHEDULE_URL: &str = "https://subsplease.org/api/?f=schedule&tz=UTC";

static SEGMENT_SPLIT: LazyLock<Regex> = LazyLock::new(|| compiled(r"\s*[:：–—]\s*|\s+-\s+"));
static MARKERS: LazyLock<Regex> = LazyLock::new(|| {
    compiled(
        r"(?i)\b(?:the\s+)?final\s+season\b|\b(?:season|stage|part|cour|s)\s*\d+\b|\b\d+(?:st|nd|rd|th)(?:\s+(?:season|stage|part|cour))?\b|\b(?:season|stage|part|cour)\b",
    )
});
static PUNCTUATION: LazyLock<Regex> = LazyLock::new(|| compiled(r"[^\w\s']"));

/// A pattern written in this file; a typo shows up in every test that searches.
#[expect(clippy::expect_used, reason = "the patterns are literals of this file")]
fn compiled(pattern: &str) -> Regex {
    Regex::new(pattern).expect("pattern literal compiles")
}

fn clean_segment(segment: &str) -> String {
    let without_markers = MARKERS.replace_all(segment, " ");
    let without_punctuation = PUNCTUATION.replace_all(&without_markers, " ");
    without_punctuation
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Up to [`MAX_TERMS`] short names to search for, from the most specific title first.
///
/// The site's search is broad on purpose; every result is matched against the show again before it
/// is kept, so a loose term costs nothing but a few discarded rows.
pub fn search_terms<'a>(titles: impl IntoIterator<Item = Option<&'a str>>) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    let mut seen = HashSet::new();
    for title in titles
        .into_iter()
        .flatten()
        .filter(|title| !title.is_empty())
    {
        for segment in SEGMENT_SPLIT.split(title) {
            let term = clean_segment(segment);
            let mut words = term.split_whitespace();
            let (first, second) = (words.next(), words.next());
            let long_enough = match (first, second) {
                (Some(_), Some(_)) => true,
                (Some(word), None) => word.chars().count() >= 4,
                _ => false,
            };
            if long_enough && seen.insert(term.to_lowercase()) {
                terms.push(term);
            }
        }
    }
    // Release names are written in Latin letters, so a name in another script is only asked for
    // when it is all there is.
    let latin: Vec<String> = terms
        .iter()
        .filter(|term| term.chars().any(|c| c.is_ascii_alphabetic()))
        .cloned()
        .collect();
    let chosen = if latin.is_empty() { terms } else { latin };
    chosen.into_iter().take(MAX_TERMS).collect()
}

/// Several names as one nyaa search: `((a)|(b))`.
///
/// Each name is grouped so its words must all match, and the whole is grouped so the feed's own
/// words (`1080p -HEVC`) still apply to every name; without the outer brackets they do not. That the
/// host really answers it so is never assumed: [`Grouping`] checks it.
pub fn any_of(terms: &[String]) -> String {
    match terms {
        [only] => only.clone(),
        _ => format!(
            "({})",
            terms
                .iter()
                .map(|term| format!("({term})"))
                .collect::<Vec<_>>()
                .join("|")
        ),
    }
}

/// By host: whether its search answers several names at once ([`any_of`]) as it answers them one
/// by one, and how many results its pages are known to hold.
///
/// Neither is assumed. Until a check shows that the grouped search finds exactly the releases the
/// names find alone, each name is asked for alone; a check that finds a difference turns grouping
/// off. Either result is checked again after a while. One search at a time checks a host, as a check
/// asks for every name and costs more than a plain search. What was learned is kept across restarts
/// ([`Grouping::dump`], [`Grouping::load`]), so the clock is the wall clock.
///
/// Searches running at the same time share one `Grouping`: each method holds it only for its own
/// short step.
pub struct Grouping {
    state: Mutex<GroupingState>,
}

struct GroupingState {
    clock: Box<dyn Fn() -> f64 + Send>,
    checked: HashMap<String, (f64, bool)>,
    checking: HashSet<String>,
    largest: HashMap<String, u64>,
}

impl GroupingState {
    fn works(&self, host: &str) -> Option<bool> {
        let (at, works) = *self.checked.get(host)?;
        self.current(at).then_some(works)
    }

    fn current(&self, checked_at: f64) -> bool {
        (0.0..=GROUPING_RECHECK_SECONDS).contains(&((self.clock)() - checked_at))
    }

    fn saw(&mut self, host: &str, count: u64) {
        let largest = self.largest.entry(host.to_owned()).or_insert(0);
        *largest = (*largest).max(count);
    }
}

impl Grouping {
    pub fn new(clock: Box<dyn Fn() -> f64 + Send>) -> Self {
        Self {
            state: Mutex::new(GroupingState {
                clock,
                checked: HashMap::new(),
                checking: HashSet::new(),
                largest: HashMap::new(),
            }),
        }
    }

    fn state(&self) -> MutexGuard<'_, GroupingState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Whether the caller is to check the host now; it then calls [`Grouping::release`] when done.
    pub fn claim(&self, host: &str) -> bool {
        let mut state = self.state();
        if state.works(host).is_some() || state.checking.contains(host) {
            return false;
        }
        state.checking.insert(host.to_owned());
        true
    }

    pub fn release(&self, host: &str) {
        self.state().checking.remove(host);
    }

    /// `Some` once checked, `None` while it is still to be checked.
    pub fn works(&self, host: &str) -> Option<bool> {
        self.state().works(host)
    }

    pub fn record(&self, host: &str, works: bool) {
        let mut state = self.state();
        let now = (state.clock)();
        state.checked.insert(host.to_owned(), (now, works));
    }

    /// Whether a page of `count` results holds every match.
    ///
    /// A site's page size is not assumed. A page with no results was not cut off, and one with
    /// fewer results than the host has returned before had room for more.
    pub fn complete(&self, host: &str, count: u64) -> bool {
        count == 0 || count < self.page_size(host)
    }

    /// The most results one of the host's pages was seen to hold (0: none seen yet).
    pub fn page_size(&self, host: &str) -> u64 {
        self.state().largest.get(host).copied().unwrap_or(0)
    }

    /// Notes how many results one of the host's pages held.
    pub fn saw(&self, host: &str, count: u64) {
        self.state().saw(host, count);
    }

    pub fn dump(&self) -> String {
        let state = self.state();
        let checked: serde_json::Map<String, Value> = state
            .checked
            .iter()
            .map(|(host, (at, works))| (host.clone(), json!([at, works])))
            .collect();
        json!({"checked": checked, "largest": state.largest}).to_string()
    }

    /// Takes back what [`Grouping::dump`] kept. A host whose check is due again starts over, page
    /// size included. Text that is no dump changes nothing.
    pub fn load(&self, text: &str) {
        let Some((checked, largest)) = parse_dump(text) else {
            return;
        };
        let mut state = self.state();
        state.checked = checked
            .into_iter()
            .filter(|(_, (at, _))| state.current(*at))
            .collect();
        for (host, count) in largest {
            if state.checked.contains_key(&host) {
                state.saw(&host, count);
            }
        }
    }
}

type Checked = HashMap<String, (f64, bool)>;

fn parse_dump(text: &str) -> Option<(Checked, HashMap<String, u64>)> {
    let data: Value = serde_json::from_str(if text.is_empty() { "{}" } else { text }).ok()?;
    let checked = data
        .get("checked")?
        .as_object()?
        .iter()
        .map(|(host, entry)| {
            let pair = entry.as_array()?;
            Some((
                host.clone(),
                (pair.first()?.as_f64()?, pair.get(1)?.as_bool()?),
            ))
        })
        .collect::<Option<Checked>>()?;
    let largest = data
        .get("largest")?
        .as_object()?
        .iter()
        .map(|(host, count)| Some((host.clone(), count.as_u64()?)))
        .collect::<Option<HashMap<_, _>>>()?;
    Some((checked, largest))
}

/// A feed on a site that can be searched.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Site {
    Nyaa,
    SubsPlease,
}

/// The site a feed is on, when it can be searched.
pub fn site_of(feed_url: &str) -> Option<Site> {
    let parts = url::split(feed_url);
    let host = url::hostname(feed_url).unwrap_or_default();
    let query = url::parse_qsl(parts.query);
    let page_is_rss = query
        .iter()
        .rev()
        .find(|(name, _)| name == "page")
        .is_some_and(|(_, value)| value == "rss");
    if (host == "nyaa.si" || host == "sukebei.nyaa.si") && page_is_rss {
        Some(Site::Nyaa)
    } else if host.ends_with("subsplease.org") && parts.path.starts_with("/rss") {
        Some(Site::SubsPlease)
    } else {
        None
    }
}

/// The host a feed address points to, or the whole address when it names none.
pub fn host_of(feed_url: &str) -> String {
    url::host_or_url(feed_url)
}

/// The feed's own URL with `term` added to its search, keeping user, category and filter.
pub fn nyaa_search_url(feed_url: &str, term: &str) -> String {
    let parts = url::split(feed_url);
    let mut pairs = url::parse_qsl(parts.query);
    match pairs.iter_mut().find(|(name, _)| name == "q") {
        Some((_, value)) => *value = format!("{value} {term}").trim().to_owned(),
        None => pairs.push(("q".to_owned(), term.to_owned())),
    }
    let mut rebuilt = String::new();
    if !parts.scheme.is_empty() {
        rebuilt.push_str(parts.scheme);
        rebuilt.push(':');
    }
    if !parts.netloc.is_empty() {
        rebuilt.push_str("//");
        rebuilt.push_str(parts.netloc);
    }
    rebuilt.push_str(parts.path);
    rebuilt.push('?');
    rebuilt.push_str(&url::urlencode(&pairs));
    if !parts.fragment.is_empty() {
        rebuilt.push('#');
        rebuilt.push_str(parts.fragment);
    }
    rebuilt
}

pub fn subsplease_search_url(term: &str) -> String {
    format!(
        "https://subsplease.org/api/?f=search&tz=UTC&s={}",
        url::quote(term, "/")
    )
}

/// The resolution a SubsPlease feed asks for (`r=` in its address), 1080 when it names none.
pub fn subsplease_resolution(feed_url: &str) -> String {
    url::parse_qsl(url::split(feed_url).query)
        .into_iter()
        .find(|(name, value)| name == "r" && !value.is_empty())
        .map_or_else(|| "1080".to_owned(), |(_, value)| value)
}

/// A JSON object whose entries keep the order they were written in.
struct Ordered(Vec<(String, Value)>);

impl<'de> Deserialize<'de> for Ordered {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Entries;

        impl<'de> Visitor<'de> for Entries {
            type Value = Ordered;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON object")
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Ordered, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry::<String, Value>()? {
                    entries.push(entry);
                }
                Ok(Ordered(entries))
            }
        }

        deserializer.deserialize_map(Entries)
    }
}

/// The titles of the shows SubsPlease's weekly schedule lists; `None` when the answer is not a
/// schedule.
pub fn parse_subsplease_schedule(body: &str) -> Option<Vec<String>> {
    #[derive(Deserialize)]
    struct Schedule {
        schedule: Ordered,
    }
    let Schedule { schedule } = serde_json::from_str(body).ok()?;
    let titles: Vec<String> = schedule
        .0
        .iter()
        .filter_map(|(_, day)| day.as_array())
        .flatten()
        .filter_map(|entry| entry.get("title")?.as_str())
        .filter(|title| !title.trim().is_empty())
        .map(str::to_owned)
        .collect();
    (!titles.is_empty()).then_some(titles)
}

/// The shows SubsPlease is airing this week, in one request; `None` when it could not be read.
pub fn subsplease_schedule(fetcher: &Fetcher) -> Result<Option<Vec<String>>, FeedFetchError> {
    let body = fetcher.fetch_bytes(SUBSPLEASE_SCHEDULE_URL, RequestKind::Schedule)?;
    Ok(std::str::from_utf8(&body)
        .ok()
        .and_then(parse_subsplease_schedule))
}

/// SubsPlease search results as feed articles.
///
/// The guids are the ones the RSS uses (the magnet's hash), so an item found by both is one item.
/// The API's `release_date` can be an hour off, which is harmless for the day-sized checks it
/// feeds, but it must never be in the future, so it is capped at `now`. Entries and downloads of
/// any other shape are skipped, and an answer that is no result list gives none.
pub fn parse_subsplease_search(body: &str, resolution: &str, now: Timestamp) -> Vec<FeedArticle> {
    let Ok(Ordered(entries)) = serde_json::from_str(body) else {
        return Vec::new();
    };
    let mut articles = Vec::new();
    for (_, entry) in &entries {
        let Some(downloads) = entry.get("downloads").and_then(Value::as_array) else {
            continue;
        };
        let released = entry
            .get("release_date")
            .and_then(Value::as_str)
            .and_then(|date| parse_date(date, None));
        for download in downloads {
            if !download.is_object() || !names_resolution(download.get("res"), resolution) {
                continue;
            }
            let magnet = download.get("magnet").and_then(Value::as_str);
            let (Some(magnet), (Some(hash), Some(name))) =
                (magnet, magnet.map(magnet_parts).unwrap_or((None, None)))
            else {
                continue;
            };
            articles.push(FeedArticle {
                guid: hash.clone(),
                title: name,
                torrent_url: magnet.to_owned(),
                published_at: released.map(|released| released.min(now)),
                info_hash: normalize_info_hash(&hash),
            });
        }
    }
    articles
}

fn names_resolution(res: Option<&Value>, resolution: &str) -> bool {
    match res {
        Some(Value::String(text)) => text == resolution,
        Some(Value::Number(number)) => number.to_string() == resolution,
        _ => false,
    }
}

/// The feed's releases for `term`; `None` when this kind of feed cannot be searched.
pub fn search_feed(
    fetcher: &Fetcher,
    feed_url: &str,
    term: &str,
    now: Timestamp,
) -> Result<Option<Vec<FeedArticle>>, FeedFetchError> {
    match site_of(feed_url) {
        Some(Site::Nyaa) => {
            let body =
                fetcher.fetch_bytes(&nyaa_search_url(feed_url, term), RequestKind::Search)?;
            parse_feed(&body, None).map(Some)
        }
        Some(Site::SubsPlease) => {
            let body = fetcher.fetch_bytes(&subsplease_search_url(term), RequestKind::Search)?;
            let text = String::from_utf8_lossy(&body);
            Ok(Some(parse_subsplease_search(
                &text,
                &subsplease_resolution(feed_url),
                now,
            )))
        }
        None => Ok(None),
    }
}
