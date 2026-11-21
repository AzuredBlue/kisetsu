//! The user's AniList list: its entries, how they are read from AniList's answer, which of them
//! Kisetsu follows, and how the list is kept on disk.

use std::collections::HashSet;

use jiff::Timestamp;
use jiff::tz::TimeZone;
use serde::{Deserialize, Serialize};

/// An enum written as AniList's own upper-case word; a word it does not know is kept as it came.
macro_rules! named_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $text:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(from = "String", into = "String")]
        pub enum $name {
            $($variant,)+
            Other(String),
        }

        impl From<String> for $name {
            fn from(text: String) -> Self {
                match text.as_str() {
                    $($text => Self::$variant,)+
                    _ => Self::Other(text),
                }
            }
        }

        impl From<$name> for String {
            fn from(value: $name) -> Self {
                value.as_str().to_owned()
            }
        }

        impl $name {
            pub fn as_str(&self) -> &str {
                match self {
                    $(Self::$variant => $text,)+
                    Self::Other(text) => text,
                }
            }
        }
    };
}

named_enum! {
    /// Where a show stands on AniList's broadcast calendar.
    MediaStatus {
        Finished = "FINISHED",
        Releasing = "RELEASING",
        NotYetReleased = "NOT_YET_RELEASED",
        Cancelled = "CANCELLED",
        Hiatus = "HIATUS",
        Unknown = "UNKNOWN",
    }
}

named_enum! {
    /// The user's own status for a list entry.
    ListStatus {
        Current = "CURRENT",
        Planning = "PLANNING",
        Completed = "COMPLETED",
        Paused = "PAUSED",
        Dropped = "DROPPED",
        Repeating = "REPEATING",
    }
}

named_enum! {
    /// A broadcast season.
    Season {
        Winter = "WINTER",
        Spring = "SPRING",
        Summer = "SUMMER",
        Fall = "FALL",
    }
}

/// List statuses whose entries only update shows already followed; the sync never adds them as new.
const FOLLOWED_ONLY: [ListStatus; 4] = [
    ListStatus::Completed,
    ListStatus::Paused,
    ListStatus::Dropped,
    ListStatus::Repeating,
];

/// One entry of the user's list, with its show's titles, aliases and broadcast state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ListEntry {
    pub anilist_id: i64,
    pub display_name: String,
    /// Empty when AniList has no such title.
    pub title_romaji: String,
    pub title_english: String,
    /// Every title and synonym, trimmed, each once.
    pub aliases: Vec<String>,
    /// The user's own status, distinct from the show's broadcast `status`.
    #[serde(with = "optional_name")]
    pub list_status: Option<ListStatus>,
    /// How many episodes the user has watched; `None` when AniList did not say.
    pub progress: Option<i64>,
    pub status: MediaStatus,
    pub total_episodes: Option<i64>,
    pub next_airing_episode: Option<i64>,
    #[serde(with = "iso_utc_option")]
    pub next_airing_at: Option<Timestamp>,
    pub season: Option<Season>,
    pub season_year: Option<i32>,
    /// Empty when AniList has no cover.
    pub cover_image: String,
    pub cover_color: String,
    pub banner_image: String,
}

/// `None` is written as an empty string.
mod optional_name {
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    use super::ListStatus;

    pub fn serialize<S: Serializer>(
        value: &Option<ListStatus>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        value
            .as_ref()
            .map_or("", ListStatus::as_str)
            .serialize(serializer)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<ListStatus>, D::Error> {
        let text = Option::<String>::deserialize(deserializer)?.unwrap_or_default();
        Ok((!text.is_empty()).then(|| ListStatus::from(text)))
    }
}

/// An instant as `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`.
pub(crate) fn iso_utc(instant: Timestamp) -> String {
    let base = TimeZone::UTC
        .to_datetime(instant)
        .strftime("%Y-%m-%dT%H:%M:%S")
        .to_string();
    match instant.subsec_nanosecond() / 1000 {
        0 => format!("{base}+00:00"),
        micros => format!("{base}.{micros:06}+00:00"),
    }
}

pub(crate) mod iso_utc_option {
    use jiff::Timestamp;
    use serde::de::Error;
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    pub fn serialize<S: Serializer>(
        value: &Option<Timestamp>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        value.map(super::iso_utc).serialize(serializer)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Timestamp>, D::Error> {
        Option::<String>::deserialize(deserializer)?
            .filter(|text| !text.is_empty())
            .map(|text| text.parse().map_err(D::Error::custom))
            .transpose()
    }
}

// -- AniList's answer --

#[derive(Debug, Default, Deserialize)]
pub(crate) struct Collection {
    lists: Option<Vec<ListGroup>>,
}

#[derive(Debug, Deserialize)]
struct ListGroup {
    entries: Option<Vec<RawEntry>>,
}

#[derive(Debug, Deserialize)]
struct RawEntry {
    status: Option<String>,
    progress: Option<i64>,
    media: Option<Media>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Media {
    id: Option<i64>,
    title: Option<Titles>,
    synonyms: Option<Vec<Option<String>>>,
    status: Option<String>,
    episodes: Option<i64>,
    next_airing_episode: Option<NextAiring>,
    season: Option<String>,
    season_year: Option<i32>,
    cover_image: Option<Cover>,
    banner_image: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Titles {
    romaji: Option<String>,
    english: Option<String>,
    native: Option<String>,
    user_preferred: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NextAiring {
    airing_at: Option<i64>,
    episode: Option<i64>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Cover {
    extra_large: Option<String>,
    large: Option<String>,
    color: Option<String>,
}

/// The first of `candidates` with any text in it.
fn first_text<'a>(candidates: &[&'a Option<String>]) -> Option<&'a str> {
    candidates
        .iter()
        .find_map(|candidate| candidate.as_deref().filter(|text| !text.is_empty()))
}

fn text_or_empty(text: &Option<String>) -> String {
    text.clone().unwrap_or_default()
}

fn list_entry(
    list_status: Option<&str>,
    progress: Option<i64>,
    media: &Media,
    id: i64,
) -> ListEntry {
    let no_titles = Titles::default();
    let titles = media.title.as_ref().unwrap_or(&no_titles);
    let no_cover = Cover::default();
    let cover = media.cover_image.as_ref().unwrap_or(&no_cover);
    let mut aliases: Vec<String> = Vec::new();
    let named = [
        &titles.romaji,
        &titles.english,
        &titles.native,
        &titles.user_preferred,
    ];
    let synonyms = media.synonyms.iter().flatten();
    for name in named.into_iter().chain(synonyms) {
        let trimmed = name.as_deref().map_or("", str::trim);
        if !trimmed.is_empty() && !aliases.iter().any(|alias| alias == trimmed) {
            aliases.push(trimmed.to_owned());
        }
    }
    let next = media.next_airing_episode.as_ref();
    let next_airing_at = next
        .and_then(|next| next.airing_at)
        .filter(|&at| at != 0)
        .and_then(|at| Timestamp::from_second(at).ok());
    ListEntry {
        anilist_id: id,
        display_name: first_text(&[&titles.user_preferred, &titles.english, &titles.romaji])
            .map_or_else(|| format!("Anime_{id}"), str::to_owned),
        title_romaji: text_or_empty(&titles.romaji),
        title_english: text_or_empty(&titles.english),
        aliases,
        list_status: list_status
            .filter(|status| !status.is_empty())
            .map(|status| ListStatus::from(status.to_owned())),
        progress,
        status: media
            .status
            .as_deref()
            .filter(|status| !status.is_empty())
            .map_or(MediaStatus::Unknown, |status| {
                MediaStatus::from(status.to_owned())
            }),
        total_episodes: media.episodes,
        next_airing_episode: next.and_then(|next| next.episode),
        next_airing_at,
        season: media.season.clone().map(Season::from),
        season_year: media.season_year,
        cover_image: first_text(&[&cover.extra_large, &cover.large])
            .unwrap_or_default()
            .to_owned(),
        cover_color: text_or_empty(&cover.color),
        banner_image: text_or_empty(&media.banner_image),
    }
}

impl Collection {
    /// Every show once, in list order, with the user's status and progress for it.
    pub(crate) fn into_entries(self) -> Vec<ListEntry> {
        let mut seen = HashSet::new();
        let mut entries = Vec::new();
        for entry in self
            .lists
            .into_iter()
            .flatten()
            .flat_map(|list| list.entries)
            .flatten()
        {
            let Some(media) = &entry.media else { continue };
            let Some(id) = media.id else { continue };
            if seen.insert(id) {
                entries.push(list_entry(
                    entry.status.as_deref(),
                    entry.progress,
                    media,
                    id,
                ));
            }
        }
        entries
    }
}

// -- which entries are followed --

/// A season and its year.
pub type SeasonOfYear = (Season, i32);

/// The season `now` falls in and the one after it.
pub fn current_and_next_season(now: Timestamp) -> (SeasonOfYear, SeasonOfYear) {
    let date = TimeZone::UTC.to_datetime(now);
    let year = i32::from(date.year());
    match date.month() {
        1..=3 => ((Season::Winter, year), (Season::Spring, year)),
        4..=6 => ((Season::Spring, year), (Season::Summer, year)),
        7..=9 => ((Season::Summer, year), (Season::Fall, year)),
        _ => ((Season::Fall, year), (Season::Winter, year + 1)),
    }
}

/// The entries Kisetsu acts on: shows airing now, those planned for this or next season, those of
/// this season that already finished, and every show already followed.
///
/// Only Watching and Planning entries are introduced as new shows; finished, paused or dropped
/// ones come through only for shows already followed, so they stay up to date.
pub fn select_seasonal(
    entries: &[ListEntry],
    monitored: &HashSet<i64>,
    (current, next): (SeasonOfYear, SeasonOfYear),
) -> Vec<ListEntry> {
    let in_season = |entry: &ListEntry, (season, year): &SeasonOfYear| {
        entry.season.as_ref() == Some(season) && entry.season_year == Some(*year)
    };
    entries
        .iter()
        .filter(|entry| {
            let followed = monitored.contains(&entry.anilist_id);
            if !followed
                && entry
                    .list_status
                    .as_ref()
                    .is_some_and(|status| FOLLOWED_ONLY.contains(status))
            {
                return false;
            }
            let releasing = entry.status == MediaStatus::Releasing;
            let planned = in_season(entry, &next)
                || (in_season(entry, &current) && entry.status == MediaStatus::NotYetReleased);
            let finished_this_season =
                in_season(entry, &current) && entry.status == MediaStatus::Finished;
            releasing || planned || finished_this_season || followed
        })
        .cloned()
        .collect()
}

// -- the list kept on disk --

/// The list as it is saved, so a restart does not leave the add-show search with nothing.
#[derive(Debug, Serialize, Deserialize)]
pub(crate) struct SavedList {
    #[serde(with = "iso_utc")]
    pub fetched_at: Timestamp,
    pub entries: Vec<ListEntry>,
}

mod iso_utc {
    use jiff::Timestamp;
    use serde::de::Error;
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(value: &Timestamp, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&super::iso_utc(*value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Timestamp, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(D::Error::custom)
    }
}
