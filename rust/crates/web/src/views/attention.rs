//! What across every show needs the user's eye.

use std::collections::hash_map::Entry;
use std::collections::{BTreeSet, HashMap, HashSet};

use jiff::Timestamp;
use kisetsu_db::schema::{Download, Episode, Feed, Show};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::schedule::past_hunting_window;
use kisetsu_services::Result;
use kisetsu_services::context::{Context, get_state};
use kisetsu_services::manual::{self, Candidate};
use serde_json::{Value, json};

use super::iso;
use super::shows::caught_up;

/// The attention rows the user closed, as a JSON list of keys. Only the list hides them: the
/// episode itself is left as it is.
pub const ATTENTION_DISMISSED_KEY: &str = "attention_dismissed";

/// The keys of the rows the user closed.
pub fn dismissed_attention(ctx: &Context<'_>) -> Result<HashSet<String>> {
    let kept = get_state(ctx.conn, ATTENTION_DISMISSED_KEY, "[]")?;
    Ok(serde_json::from_str::<Vec<String>>(&kept)
        .map(|keys| keys.into_iter().collect())
        .unwrap_or_default())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Stalled,
    Failed,
    Retrying,
    Missed,
    Unfound,
}

impl Kind {
    fn name(self) -> &'static str {
        match self {
            Self::Stalled => "stalled",
            Self::Failed => "failed",
            Self::Retrying => "retrying",
            Self::Missed => "missed",
            Self::Unfound => "unfound",
        }
    }

    fn order(self) -> u8 {
        self as u8
    }
}

/// One row of the Attention list.
pub struct Row {
    show_id: i64,
    show: String,
    cover: Option<String>,
    episode_id: i64,
    pub episodes: Vec<i64>,
    kind: Kind,
    pub keys: Vec<String>,
    reason: String,
    retry_after: Option<Timestamp>,
    action: Option<Value>,
}

impl Row {
    fn to_json(&self) -> Value {
        json!({
            "show_id": self.show_id,
            "show": self.show,
            "cover": self.cover,
            "episode_id": self.episode_id,
            "episodes": self.episodes,
            "kind": self.kind.name(),
            "keys": self.keys,
            "reason": self.reason,
            "retry_after": if self.kind == Kind::Retrying { iso(self.retry_after) } else { Value::Null },
            "action": self.action,
        })
    }
}

/// Episodes across every show that need the user's eye, each with the one action that helps, if
/// any.
///
/// Stalled and failed downloads first, then those waiting to be tried again, then episodes no
/// feed carries any more, then shows the user watched every aired episode of that no feed
/// carries. Paused and completed shows are left out, and so are rows the user closed. A closed
/// row is keyed by its episode, its kind and its current download, so the same episode coming
/// back for another reason is shown again. A show's missed episodes share one row, which holds
/// the key of each.
pub fn rows(ctx: &mut Context<'_>, include_dismissed: bool) -> Result<Vec<Row>> {
    let language = ctx.settings.title_language.clone();
    let dismissed = if include_dismissed {
        HashSet::new()
    } else {
        dismissed_attention(ctx)?
    };
    let shows: HashMap<i64, Show> = Show::select(
        ctx.conn,
        "WHERE paused = 0 AND completed = 0 ORDER BY id",
        [],
    )?
    .into_iter()
    .filter_map(|show| show.id.map(|id| (id, show)))
    .collect();
    if shows.is_empty() {
        return Ok(Vec::new());
    }
    let stalled: HashMap<i64, Download> = if ctx.caches.stalled.is_empty() {
        HashMap::new()
    } else {
        let ids = ctx
            .caches
            .stalled
            .iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        Download::select(ctx.conn, &format!("WHERE id IN ({ids})"), [])?
            .into_iter()
            .map(|download| (download.episode_id, download))
            .collect()
    };
    let show_ids = shows
        .keys()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    let episodes = Episode::select(
        ctx.conn,
        &format!("WHERE show_id IN ({show_ids}) ORDER BY id"),
        [],
    )?;
    let mut found: HashMap<i64, Vec<Candidate>> = HashMap::new();
    let mut missed_rows: HashMap<i64, usize> = HashMap::new();
    let mut out: Vec<Row> = Vec::new();
    for episode in &episodes {
        let Some(show) = shows.get(&episode.show_id) else {
            continue;
        };
        let episode_id = episode.id.unwrap_or_default();
        let aired = episode.air_at.is_none_or(|at| at <= ctx.now);
        let (kind, reason) = if stalled.contains_key(&episode_id) {
            (
                Kind::Stalled,
                "The torrent has moved no data for hours.".to_owned(),
            )
        } else if episode.status == EpisodeStatus::Failed {
            (
                Kind::Failed,
                episode
                    .last_error
                    .clone()
                    .filter(|error| !error.is_empty())
                    .unwrap_or_else(|| "The download failed.".to_owned()),
            )
        } else if episode.status == EpisodeStatus::Wanted
            && aired
            && episode
                .last_error
                .as_deref()
                .is_some_and(|error| !error.is_empty())
            && episode.retry_after.is_some_and(|after| after > ctx.now)
        {
            (
                Kind::Retrying,
                episode.last_error.clone().unwrap_or_default(),
            )
        } else if episode.status == EpisodeStatus::Missed {
            (
                Kind::Missed,
                "No feed carries it any more. Search older asks the feeds' sites for it."
                    .to_owned(),
            )
        } else {
            continue;
        };
        let key = format!(
            "{episode_id}:{}:{}",
            kind.name(),
            episode.current_download_id.unwrap_or(0)
        );
        if dismissed.contains(&key) {
            continue;
        }
        let mut action = None;
        match kind {
            Kind::Missed => action = Some(json!({"type": "search"})),
            Kind::Failed | Kind::Stalled => {
                let show_id = episode.show_id;
                if let Entry::Vacant(slot) = found.entry(show_id) {
                    slot.insert(manual::candidates(ctx, show, None, None)?);
                }
                let candidates = found.get(&show_id).map(Vec::as_slice);
                if let Some(stalled) = stalled.get(&episode_id).filter(|_| kind == Kind::Stalled) {
                    if !manual::alternatives(ctx, show, episode, stalled, candidates)?.is_empty() {
                        action = Some(json!({"type": "try_another"}));
                    }
                } else if kind == Kind::Failed {
                    if let Some(title) = manual::retry_release(ctx, show, episode, candidates)? {
                        action = Some(json!({"type": "download", "title": title}));
                    }
                }
            }
            Kind::Retrying | Kind::Unfound => {}
        }
        if kind == Kind::Missed {
            if let Some(&at) = missed_rows.get(&episode.show_id) {
                // One search looks for every missed episode of a show, so they share one row and
                // one Search older.
                let row = &mut out[at];
                row.episodes.push(episode.number);
                row.keys.push(key);
                row.reason =
                    "No feed carries them any more. Search older asks the feeds' sites for them."
                        .to_owned();
                continue;
            }
            missed_rows.insert(episode.show_id, out.len());
        }
        out.push(Row {
            show_id: episode.show_id,
            show: show.title(&language),
            cover: show.cover_image.clone(),
            episode_id,
            episodes: vec![episode.number],
            kind,
            keys: vec![key],
            reason,
            retry_after: episode.retry_after,
            action,
        });
    }
    out.extend(unfound(ctx, &shows, &episodes, &dismissed)?);
    for row in &mut out {
        row.episodes.sort_unstable();
    }
    out.sort_by_cached_key(|row| {
        (
            row.kind.order(),
            row.show.to_lowercase(),
            std::cmp::Reverse(row.episodes.last().copied()),
        )
    });
    Ok(out)
}

/// The rows as the page reads them.
pub fn attention(ctx: &mut Context<'_>) -> Result<Value> {
    let rows = rows(ctx, false)?;
    Ok(Value::Array(rows.iter().map(Row::to_json).collect()))
}

/// Shows whose every aired episode the user watched, so none was missed, that the last grab pass
/// found in no feed.
///
/// Like a missed episode, the newest is given a day to show up in a feed first, and the feeds
/// must be reading.
fn unfound(
    ctx: &Context<'_>,
    shows: &HashMap<i64, Show>,
    episodes: &[Episode],
    dismissed: &HashSet<String>,
) -> Result<Vec<Row>> {
    let flagged: BTreeSet<i64> = caught_up(ctx, None)?
        .into_iter()
        .filter(|(id, newest)| {
            shows.contains_key(id)
                && ctx.caches.carried.get(id) == Some(&false)
                && past_hunting_window(Some(*newest), ctx.now)
        })
        .map(|(id, _)| id)
        .collect();
    let reading = Feed::select(ctx.conn, "", [])?
        .iter()
        .any(|feed| feed.last_ok_at.is_some() && feed.failing_since.is_none());
    if flagged.is_empty() || !reading {
        return Ok(Vec::new());
    }
    let mut aired: HashMap<i64, Vec<&Episode>> = HashMap::new();
    for episode in episodes {
        if flagged.contains(&episode.show_id) && episode.air_at.is_some_and(|at| at <= ctx.now) {
            aired.entry(episode.show_id).or_default().push(episode);
        }
    }
    let mut out = Vec::new();
    for show_id in flagged {
        let mut list = aired.remove(&show_id).unwrap_or_default();
        list.sort_by_key(|episode| episode.number);
        let Some(last) = list.last() else { continue };
        let last_id = last.id.unwrap_or_default();
        let key = format!("{last_id}:unfound:0");
        if dismissed.contains(&key) {
            continue;
        }
        let Some(show) = shows.get(&show_id) else {
            continue;
        };
        out.push(Row {
            show_id,
            show: show.title(&ctx.settings.title_language),
            cover: show.cover_image.clone(),
            episode_id: last_id,
            episodes: list.iter().map(|episode| episode.number).collect(),
            kind: Kind::Unfound,
            keys: vec![key],
            reason: "No feed carries this show, so its next episode won't be found either. Check its names or pick its feed on the show page.".to_owned(),
            retry_after: None,
            action: None,
        });
    }
    Ok(out)
}
