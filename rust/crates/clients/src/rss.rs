//! Parsing RSS feeds, as nyaa and SubsPlease write them: items, dates, info hashes and magnet links.

use std::borrow::Borrow;
use std::collections::HashMap;
use std::fmt::Write as _;
use std::hash::Hash;

use jiff::civil::DateTime;
use jiff::tz::{Offset, TimeZone};
use jiff::{SignedDuration, Timestamp};
use quick_xml::escape::resolve_predefined_entity;
use quick_xml::events::{BytesStart, Event};
use quick_xml::{Reader, XmlVersion};
use thiserror::Error;

use crate::url;

/// A feed could not be read; the message says why in terms a user can act on.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum FeedFetchError {
    #[error("The feed is not valid XML ({0})")]
    InvalidXml(String),
    #[error("The address answers with <{0}>, which is not an RSS feed")]
    NotRss(String),
    #[error("{host} did not answer in time")]
    Timeout { host: String },
    #[error("{host} could not be reached ({detail})")]
    Unreachable { host: String, detail: String },
    #[error("{host} is rate limiting requests")]
    RateLimited { host: String },
    #[error("{host} is rate limiting requests; it can be asked again in {seconds:.0}s")]
    RateLimitedFor { host: String, seconds: u64 },
    #[error("{host} answered HTTP {status}")]
    Status { host: String, status: u16 },
    #[error("{host} sent a response larger than {megabytes} MB")]
    TooLarge { host: String, megabytes: u64 },
}

impl FeedFetchError {
    /// Whether the cause may pass on its own: no connection, a timeout or a server error.
    pub fn is_transient(&self) -> bool {
        match self {
            Self::Timeout { .. } | Self::Unreachable { .. } => true,
            Self::Status { status, .. } => *status >= 500,
            _ => false,
        }
    }
}

/// One item of a feed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeedArticle {
    pub guid: String,
    pub title: String,
    pub torrent_url: String,
    pub published_at: Option<Timestamp>,
    pub info_hash: Option<String>,
}

/// A 40-character lowercase hex info hash from hex or base32.
pub fn normalize_info_hash(value: &str) -> Option<String> {
    let text = value.trim();
    if text.len() == 40 && text.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Some(text.to_ascii_lowercase());
    }
    if text.len() == 32 {
        return base32_to_hex(text);
    }
    None
}

fn base32_to_hex(text: &str) -> Option<String> {
    let mut hex = String::with_capacity(40);
    let mut buffer = 0u32;
    let mut bits = 0;
    for byte in text.bytes() {
        let digit = match byte.to_ascii_uppercase() {
            letter @ b'A'..=b'Z' => letter - b'A',
            digit @ b'2'..=b'7' => digit - b'2' + 26,
            _ => return None,
        };
        buffer = buffer << 5 | u32::from(digit);
        bits += 5;
        if bits >= 8 {
            bits -= 8;
            let _ = write!(hex, "{:02x}", (buffer >> bits) & 0xff);
            buffer &= (1 << bits) - 1;
        }
    }
    Some(hex)
}

/// The info hash as written and the display name of a magnet link.
///
/// With several `xt` or `dn` parameters the last one counts.
pub fn magnet_parts(magnet: &str) -> (Option<String>, Option<String>) {
    let query = url::parse_qsl(url::split(magnet).query);
    let last = |key: &str| {
        query
            .iter()
            .rev()
            .find(|(name, _)| name == key)
            .map(|(_, value)| value.as_str())
    };
    let hash = last("xt")
        .map(|topic| topic.rsplit(':').next().unwrap_or(topic))
        .filter(|hash| !hash.is_empty())
        .map(str::to_owned);
    let name = last("dn").map(url::unquote).filter(|name| !name.is_empty());
    (hash, name)
}

/// The zone a feed's dates are really in when it marks them wrongly.
///
/// SubsPlease's RSS writes its dates in US Pacific time but marks them `+0000`.
pub fn feed_wall_zone(feed_url: &str) -> Option<TimeZone> {
    let host = url::hostname(feed_url)?;
    (host == "subsplease.org" || host.ends_with(".subsplease.org"))
        .then(|| TimeZone::get("America/Los_Angeles").ok())
        .flatten()
}

/// A feed date as an instant.
///
/// Accepts RFC 2822 dates, also without a weekday, with a `-0000` zone or a zone name such as `GMT`.
/// A date without a usable zone is read as UTC. With `wall_zone` the stated zone is ignored and the
/// time is read as a wall time there.
pub fn parse_date(text: &str, wall_zone: Option<&TimeZone>) -> Option<Timestamp> {
    let (civil, offset) = parse_rfc2822(text)?;
    match (wall_zone, offset) {
        (Some(zone), _) => zone.to_timestamp(civil).ok(),
        (None, offset) => offset.unwrap_or(Offset::UTC).to_timestamp(civil).ok(),
    }
}

const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];
const WEEKDAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

/// The wall time of a date and the offset it states, `None` when it states none.
fn parse_rfc2822(text: &str) -> Option<(DateTime, Option<Offset>)> {
    let mut parts: Vec<&str> = text.split_whitespace().collect();
    let first = parts.first().copied()?;
    if first.ends_with(',') || WEEKDAYS.contains(&first.to_lowercase().as_str()) {
        parts.remove(0);
    } else if let Some(comma) = first.rfind(',') {
        parts[0] = &first[comma + 1..];
    }
    if parts.len() == 3 {
        let pieces: Vec<&str> = parts[0].split('-').collect();
        if pieces.len() == 3 {
            parts = pieces
                .into_iter()
                .chain(parts.iter().skip(1).copied())
                .collect();
        }
    }
    if parts.len() == 4 {
        let last = parts[3];
        parts.truncate(3);
        match last
            .find('+')
            .or_else(|| last.find('-'))
            .filter(|&at| at > 0)
        {
            Some(at) => parts.extend([&last[..at], &last[at..]]),
            None => parts.extend([last, ""]),
        }
    }
    let [mut day, mut month, mut year, mut time, mut zone, ..] = parts[..] else {
        return None;
    };
    let mut month_of_year = month_number(month);
    if month_of_year.is_none() {
        std::mem::swap(&mut day, &mut month);
        month_of_year = month_number(month);
    }
    if year.contains(':') {
        std::mem::swap(&mut year, &mut time);
    }
    if !year.starts_with(|c: char| c.is_ascii_digit()) {
        std::mem::swap(&mut year, &mut zone);
    }
    let day: i8 = day.trim_end_matches(',').parse().ok()?;
    let mut year: i16 = year.trim_end_matches(',').parse().ok()?;
    if year < 100 {
        year += if year > 68 { 1900 } else { 2000 };
    }
    let mut clock = time.trim_end_matches(',').split([':', '.']);
    let hour: i8 = clock.next()?.parse().ok()?;
    let minute: i8 = clock.next()?.parse().ok()?;
    let second: i8 = clock.next().unwrap_or("0").parse().ok()?;
    if clock.next().is_some() {
        return None;
    }
    let civil = DateTime::new(year, month_of_year?, day, hour, minute, second, 0).ok()?;
    let offset = zone_offset(zone)?;
    Some((civil, offset))
}

const FULL_MONTHS: [&str; 12] = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
];

fn month_number(name: &str) -> Option<i8> {
    let lower = name.to_lowercase();
    let position = MONTHS
        .iter()
        .position(|&month| month == lower)
        .or_else(|| FULL_MONTHS.iter().position(|&month| month == lower))?;
    i8::try_from(position + 1).ok()
}

/// The offset a zone text states: `Some(None)` for `-0000` and for text that is no zone, `None` for
/// an offset of a day or more.
fn zone_offset(zone: &str) -> Option<Option<Offset>> {
    let named: Option<i32> = match zone.to_uppercase().as_str() {
        "UT" | "UTC" | "GMT" | "Z" => Some(0),
        "AST" => Some(-400),
        "ADT" => Some(-300),
        "EST" => Some(-500),
        "EDT" => Some(-400),
        "CST" => Some(-600),
        "CDT" => Some(-500),
        "MST" => Some(-700),
        "MDT" => Some(-600),
        "PST" => Some(-800),
        "PDT" => Some(-700),
        _ => None,
    };
    let hhmm = match named {
        Some(value) => value,
        None => match zone.parse::<i32>() {
            Ok(0) if zone.starts_with('-') => return Some(None),
            Ok(value) => value,
            Err(_) => return Some(None),
        },
    };
    let sign = if hhmm < 0 { -1 } else { 1 };
    let magnitude = hhmm.abs();
    let seconds = sign * ((magnitude / 100) * 3600 + (magnitude % 100) * 60);
    if seconds.abs() >= 86_400 {
        return None;
    }
    Some(Offset::from_seconds(seconds).ok())
}

/// The items of an RSS 2.0 body, as nyaa and SubsPlease write them. See [`parse_date`] for `wall_zone`.
pub fn parse_feed(
    body: &[u8],
    wall_zone: Option<&TimeZone>,
) -> Result<Vec<FeedArticle>, FeedFetchError> {
    let root = read_document(body)?;
    let root_name = &root.name;
    if !root_name.eq_ignore_ascii_case("rss") {
        // An error page or another format would read as a feed with no items, and the feed as healthy.
        return Err(FeedFetchError::NotRss(root_name.clone()));
    }
    let mut articles = Vec::new();
    for item in root.descendants("item") {
        let mut fields: HashMap<&str, &str> = HashMap::new();
        let mut enclosure: Option<&str> = None;
        for child in &item.children {
            if child.name == "enclosure" {
                enclosure = child.url.as_deref();
            } else {
                fields
                    .entry(child.name.as_str())
                    .or_insert(child.text.trim());
            }
        }
        let title = fields
            .get("title")
            .copied()
            .filter(|title| !title.is_empty());
        let link = fields
            .get("link")
            .copied()
            .filter(|link| !link.is_empty())
            .or(enclosure.filter(|link| !link.is_empty()));
        let (Some(title), Some(link)) = (title, link) else {
            continue;
        };
        let guid = fields
            .get("guid")
            .copied()
            .filter(|guid| !guid.is_empty())
            .unwrap_or(link);
        let torrent_url = enclosure.filter(|url| !url.is_empty()).unwrap_or(link);
        let info_hash = fields
            .get("infoHash")
            .and_then(|value| normalize_info_hash(value))
            .or_else(|| {
                torrent_url
                    .starts_with("magnet:")
                    .then(|| magnet_parts(torrent_url).0)
                    .flatten()
                    .and_then(|hash| normalize_info_hash(&hash))
            })
            .or_else(|| normalize_info_hash(guid));
        articles.push(FeedArticle {
            guid: guid.to_owned(),
            title: title.to_owned(),
            torrent_url: torrent_url.to_owned(),
            published_at: fields
                .get("pubDate")
                .and_then(|date| parse_date(date, wall_zone)),
            info_hash,
        });
    }
    Ok(articles)
}

/// An element with the text before its first child, and the `url` attribute it may carry.
#[derive(Debug, Default)]
struct Element {
    name: String,
    raw_name: String,
    text: String,
    url: Option<String>,
    children: Vec<Element>,
    text_done: bool,
}

impl Element {
    /// Every element below this one named `name` without a namespace prefix, in document order.
    fn descendants(&self, name: &str) -> Vec<&Element> {
        let mut found = Vec::new();
        self.collect(name, &mut found);
        found
    }

    fn collect<'a>(&'a self, name: &str, found: &mut Vec<&'a Element>) {
        for child in &self.children {
            if child.raw_name == name {
                found.push(child);
            }
            child.collect(name, found);
        }
    }
}

fn element_of(start: &BytesStart<'_>) -> Result<Element, FeedFetchError> {
    let mut element = Element {
        name: start.local_name().into_inner().to_owned(),
        raw_name: start.name().into_inner().to_owned(),
        ..Element::default()
    };
    if element.name == "enclosure" {
        for attribute in start.attributes() {
            let attribute = attribute.map_err(invalid_xml)?;
            if attribute.key.as_ref() == "url" {
                let value = attribute
                    .normalized_value(XmlVersion::Implicit1_0)
                    .map_err(invalid_xml)?;
                element.url = Some(value.into_owned());
            }
        }
    }
    Ok(element)
}

fn invalid_xml(error: impl std::fmt::Display) -> FeedFetchError {
    FeedFetchError::InvalidXml(error.to_string())
}

/// The whole document as a tree of its elements, the first thing in it being the root.
fn read_document(body: &[u8]) -> Result<Element, FeedFetchError> {
    let text = std::str::from_utf8(body).map_err(invalid_xml)?;
    let mut reader = Reader::from_str(text);
    let mut open: Vec<Element> = Vec::new();
    let mut root: Option<Element> = None;
    loop {
        let event = reader.read_event().map_err(invalid_xml)?;
        match event {
            Event::Start(start) => {
                if root.is_some() {
                    return Err(invalid_xml("junk after document element"));
                }
                if let Some(parent) = open.last_mut() {
                    parent.text_done = true;
                }
                open.push(element_of(&start)?);
            }
            Event::Empty(start) => {
                if root.is_some() {
                    return Err(invalid_xml("junk after document element"));
                }
                let element = element_of(&start)?;
                match open.last_mut() {
                    Some(parent) => {
                        parent.text_done = true;
                        parent.children.push(element);
                    }
                    None => root = Some(element),
                }
            }
            Event::End(_) => {
                let Some(element) = open.pop() else {
                    return Err(invalid_xml("unexpected closing tag"));
                };
                match open.last_mut() {
                    Some(parent) => parent.children.push(element),
                    None => root = Some(element),
                }
            }
            Event::Text(text) => add_text(&mut open, &root, &text.xml10_content())?,
            Event::CData(data) => add_text(&mut open, &root, &data.xml10_content())?,
            Event::GeneralRef(reference) => {
                let resolved = match reference.resolve_char_ref().map_err(invalid_xml)? {
                    Some(character) => character.to_string(),
                    None => resolve_predefined_entity(&reference)
                        .ok_or_else(|| invalid_xml(format!("undefined entity &{};", &*reference)))?
                        .to_owned(),
                };
                add_text(&mut open, &root, &resolved)?;
            }
            Event::Eof => break,
            Event::Comment(_) | Event::Decl(_) | Event::PI(_) | Event::DocType(_) => {}
        }
    }
    if !open.is_empty() {
        return Err(invalid_xml("unclosed token"));
    }
    root.ok_or_else(|| invalid_xml("no element found"))
}

fn add_text(
    open: &mut [Element],
    root: &Option<Element>,
    text: &str,
) -> Result<(), FeedFetchError> {
    match open.last_mut() {
        Some(element) if !element.text_done => element.text.push_str(text),
        Some(_) => {}
        None if text.trim().is_empty() => {}
        None if root.is_some() => return Err(invalid_xml("junk after document element")),
        None => return Err(invalid_xml("syntax error: text before the root element")),
    }
    Ok(())
}

/// Per-key exponential backoff, so a failing feed is retried less and less often.
#[derive(Debug, Clone)]
pub struct Backoff<K = String> {
    base: SignedDuration,
    cap: SignedDuration,
    failures: HashMap<K, u32>,
    retry_at: HashMap<K, Timestamp>,
}

impl<K: Hash + Eq> Backoff<K> {
    pub fn new(base: SignedDuration, cap: SignedDuration) -> Self {
        Self {
            base,
            cap,
            failures: HashMap::new(),
            retry_at: HashMap::new(),
        }
    }

    /// Records a failure of `key` at `now` and returns when it may be tried again.
    pub fn failed<Q>(&mut self, key: &Q, now: Timestamp) -> Timestamp
    where
        K: Borrow<Q>,
        Q: Hash + Eq + ToOwned<Owned = K> + ?Sized,
    {
        let count = self.failures.entry(key.to_owned()).or_default();
        *count += 1;
        let doublings = (*count - 1).min(32);
        let delay = self
            .base
            .checked_mul(1 << doublings)
            .map_or(self.cap, |delay| delay.min(self.cap));
        let retry_at = now.saturating_add(delay).unwrap_or(now);
        self.retry_at.insert(key.to_owned(), retry_at);
        retry_at
    }

    pub fn succeeded<Q>(&mut self, key: &Q)
    where
        K: Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        self.failures.remove(key);
        self.retry_at.remove(key);
    }

    pub fn ready<Q>(&self, key: &Q, now: Timestamp) -> bool
    where
        K: Borrow<Q>,
        Q: Hash + Eq + ?Sized,
    {
        self.retry_at.get(key).is_none_or(|&at| now >= at)
    }
}

impl<K: Hash + Eq> Default for Backoff<K> {
    fn default() -> Self {
        Self::new(SignedDuration::from_mins(1), SignedDuration::from_mins(30))
    }
}
