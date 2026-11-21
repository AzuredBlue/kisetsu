//! Per-show choices carried over from the old database, applied when AniList first adds the show.

use std::collections::HashSet;
use std::fmt;

use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{EpisodeOffset, Feed, OffsetSource, Show};
use serde::de::{MapAccess, Visitor};
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::context::{Context, get_state, set_state};
use crate::error::Result;
use crate::feeds;

const PENDING_KEY: &str = "legacy_overrides";

/// A numbering offset the old database had learned for a feed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OffsetOverride {
    pub feed_url: String,
    pub offset: i64,
    /// The stored name of an `OffsetSource`.
    pub source: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence_count: Option<i64>,
}

/// What the old database had set for one show.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Overrides {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub save_folder: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pinned_feed_url: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub aliases: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub offsets: Vec<OffsetOverride>,
}

/// The choices still waiting for a show, by AniList id, in the order they were stored.
struct Pending(Vec<(String, Overrides)>);

impl<'de> Deserialize<'de> for Pending {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct Entries;

        impl<'de> Visitor<'de> for Entries {
            type Value = Pending;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a map of show ids to their choices")
            }

            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Pending, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = map.next_entry::<String, Overrides>()? {
                    entries.push(entry);
                }
                Ok(Pending(entries))
            }
        }

        deserializer.deserialize_map(Entries)
    }
}

/// The choices written back in the order they were stored.
struct Entries<'a>(&'a [(String, &'a Overrides)]);

impl Serialize for Entries<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(self.0.len()))?;
        for (id, choices) in self.0 {
            map.serialize_entry(id, choices)?;
        }
        map.end()
    }
}

fn encode(entries: &[(String, &Overrides)]) -> String {
    serde_json::to_string(&Entries(entries)).unwrap_or_else(|_| "{}".to_owned())
}

/// Keeps the choices for shows AniList has not added yet, replacing any kept before.
pub fn store_pending(ctx: &Context<'_>, overrides: &[(i64, Overrides)]) -> Result<()> {
    let entries: Vec<(String, &Overrides)> = overrides
        .iter()
        .map(|(id, choices)| (id.to_string(), choices))
        .collect();
    set_state(ctx.conn, PENDING_KEY, &encode(&entries))
}

fn fold(name: &str) -> String {
    name.to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Applies and consumes the imported choices for a newly added show; returns whether there were
/// any.
pub fn apply_pending(ctx: &mut Context<'_>, show: &mut Show) -> Result<bool> {
    let raw = get_state(ctx.conn, PENDING_KEY, "")?;
    let Ok(Pending(mut pending)) = serde_json::from_str::<Pending>(&raw) else {
        return Ok(false);
    };
    let key = show.anilist_id.to_string();
    let Some(at) = pending.iter().position(|(id, _)| *id == key) else {
        return Ok(false);
    };
    let (_, entry) = pending.remove(at);
    let rest: Vec<(String, &Overrides)> = pending
        .iter()
        .map(|(id, choices)| (id.clone(), choices))
        .collect();
    set_state(ctx.conn, PENDING_KEY, &encode(&rest))?;
    if let Some(folder) = entry
        .save_folder
        .as_deref()
        .filter(|folder| !folder.is_empty())
    {
        show.save_folder = folder.to_owned();
    }
    let mut known: HashSet<String> = show
        .aliases()
        .iter()
        .map(String::as_str)
        .chain([
            show.title_romaji.as_deref().unwrap_or_default(),
            show.title_english.as_deref().unwrap_or_default(),
        ])
        .map(fold)
        .collect();
    let extra: Vec<&String> = entry
        .aliases
        .iter()
        .filter(|name| known.insert(fold(name)))
        .collect();
    if !extra.is_empty() {
        let mut custom = show.custom_aliases();
        custom.extend(extra.into_iter().cloned());
        show.set_custom_aliases(&custom);
    }
    let all_feeds = Feed::select(ctx.conn, "ORDER BY id", [])?;
    let feed_for = |url: &str| all_feeds.iter().find(|feed| feed.url == url);
    show.update(ctx.conn)?;
    if let Some(pinned) = entry.pinned_feed_url.as_deref().and_then(feed_for) {
        feeds::pin(ctx, show, pinned, false)?;
    }
    for item in &entry.offsets {
        let (Some(feed), Some(source)) = (
            feed_for(&item.feed_url),
            OffsetSource::from_name(&item.source),
        ) else {
            continue;
        };
        EpisodeOffset {
            id: None,
            show_id: show.id.unwrap_or_default(),
            feed_id: feed.id.unwrap_or_default(),
            offset: item.offset,
            source,
            evidence_count: item.evidence_count.unwrap_or_default(),
            first_evidence_at: None,
            last_evidence_at: None,
        }
        .insert(ctx.conn)?;
    }
    Ok(true)
}
