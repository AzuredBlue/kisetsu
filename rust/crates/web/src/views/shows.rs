//! The Shows page and the show page.

use std::collections::{BTreeMap, HashMap};

use jiff::{SignedDuration, Timestamp};
use kisetsu_db::StoredEnum;
use kisetsu_db::rusqlite::{Row, params_from_iter};
use kisetsu_db::schema::{
    ACTIVE_DOWNLOAD_STATES, Download, EndReason, Episode, EpisodeOffset, Feed, FeedState, Show,
};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::patterns::compress_home_path;
use kisetsu_domain::schedule::{ShowFacts, is_unreleased};
use kisetsu_domain::status::{ShowStatus, show_status};
use kisetsu_services::context::Context;
use kisetsu_services::facts::{facts_for, release_delays};
use kisetsu_services::{Result, grab, library, manual};
use serde_json::{Map, Value, json};

use super::{bound, episode_counts, iso, status_value, value_of};

/// Air dates older than this are not sent: a long-running show would otherwise send hundreds of
/// years-old dates on every refresh.
const AIRINGS_KEEP: SignedDuration = SignedDuration::from_hours(24 * 365);

fn status_label(status: ShowStatus) -> &'static str {
    match status {
        ShowStatus::Paused => "Paused",
        ShowStatus::Completed => "Completed",
        ShowStatus::Working => "Working",
        ShowStatus::Upcoming => "Upcoming",
        ShowStatus::Waiting => "Waiting",
        // Looking for its first release: the feeds are tried by priority.
        ShowStatus::Testing => "Searching",
    }
}

/// The one vocabulary for how a show's feed was chosen; the UI uses these words everywhere.
fn feed_state_label(state: FeedState) -> &'static str {
    match state {
        FeedState::None => "Auto-discover",
        FeedState::Candidate | FeedState::Assigned => "Trying",
        FeedState::Pinned => "Picked by you",
        FeedState::Locked => "Locked",
    }
}

/// The colours the banner was read for, or nothing when they are unreadable.
fn hues(show: &Show) -> Value {
    let Some(text) = show.accent_hues.as_deref().filter(|text| !text.is_empty()) else {
        return Value::Null;
    };
    let parsed: std::result::Result<Vec<f64>, _> = text
        .split(',')
        .map(|part| part.trim().parse::<f64>())
        .collect();
    parsed.map_or(Value::Null, |hues| json!(hues))
}

/// The values of one query per show: the rows of `sql` (which holds `{filter}` where a single
/// show's rows may be asked for) as `(show id, value)`.
fn per_show<T>(
    ctx: &Context<'_>,
    sql: &str,
    show_id: Option<i64>,
    mut params: Vec<kisetsu_db::rusqlite::types::Value>,
    read: impl Fn(&Row<'_>) -> kisetsu_db::rusqlite::Result<(i64, T)>,
) -> Result<HashMap<i64, T>> {
    let filter = match show_id {
        Some(id) => {
            params.push(id.into());
            " AND episodes.show_id = ?"
        }
        None => "",
    };
    let sql = sql.replace("{filter}", filter);
    let mut statement = ctx.conn.prepare_cached(&sql)?;
    let rows = statement.query_map(params_from_iter(params), |row| read(row))?;
    Ok(rows.collect::<std::result::Result<_, _>>()?)
}

fn active_states() -> String {
    ACTIVE_DOWNLOAD_STATES
        .iter()
        .map(|state| format!("'{}'", state.name()))
        .collect::<Vec<_>>()
        .join(", ")
}

/// How many downloads of each show replace another release and are under way.
fn replacing(ctx: &Context<'_>, show_id: Option<i64>) -> Result<HashMap<i64, i64>> {
    let sql = format!(
        "SELECT episodes.show_id, COUNT(downloads.id) FROM downloads \
         JOIN episodes ON episodes.id = downloads.episode_id \
         WHERE downloads.supersedes_id IS NOT NULL AND downloads.state IN ({}){{filter}} \
         GROUP BY episodes.show_id",
        active_states()
    );
    per_show(ctx, &sql, show_id, Vec::new(), |row| {
        Ok((row.get(0)?, row.get(1)?))
    })
}

/// The number of each show's newest completed episode.
fn last_done(ctx: &Context<'_>, show_id: Option<i64>) -> Result<HashMap<i64, i64>> {
    per_show(
        ctx,
        "SELECT episodes.show_id, MAX(episodes.number) FROM episodes \
         WHERE episodes.status = 'COMPLETED'{filter} GROUP BY episodes.show_id",
        show_id,
        Vec::new(),
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
}

/// Each show's episodes skipped because the user watched them; while watched episodes are
/// deleted they count as done.
fn watched_skipped(ctx: &Context<'_>, show_id: Option<i64>) -> Result<HashMap<i64, i64>> {
    if !ctx.settings.delete_watched {
        return Ok(HashMap::new());
    }
    per_show(
        ctx,
        "SELECT episodes.show_id, COUNT(episodes.id) FROM episodes \
         JOIN shows ON shows.id = episodes.show_id \
         WHERE episodes.status = 'SKIPPED' AND shows.watched IS NOT NULL \
         AND episodes.number <= shows.watched{filter} GROUP BY episodes.show_id",
        show_id,
        Vec::new(),
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
}

/// The shows whose every aired episode was skipped because the user watched it, with the newest
/// one's air time. Nothing is looked for in such a show until its next episode airs.
pub(super) fn caught_up(
    ctx: &Context<'_>,
    show_id: Option<i64>,
) -> Result<HashMap<i64, Timestamp>> {
    if !ctx.settings.delete_watched {
        return Ok(HashMap::new());
    }
    let rows: HashMap<i64, (i64, i64, Option<Timestamp>)> = per_show(
        ctx,
        "SELECT episodes.show_id, COUNT(episodes.id), \
         SUM(CASE WHEN episodes.status = 'SKIPPED' AND episodes.number <= shows.watched THEN 1 ELSE 0 END), \
         MAX(episodes.air_at) FROM episodes JOIN shows ON shows.id = episodes.show_id \
         WHERE shows.watched IS NOT NULL AND episodes.air_at IS NOT NULL AND episodes.air_at <= ?{filter} \
         GROUP BY episodes.show_id",
        show_id,
        vec![bound(ctx.now)],
        |row| {
            let newest: Option<String> = row.get(3)?;
            let newest = newest.and_then(|text| kisetsu_db::storage::parse_timestamp(&text));
            Ok((
                row.get(0)?,
                (
                    row.get(1)?,
                    row.get::<_, Option<i64>>(2)?.unwrap_or(0),
                    newest,
                ),
            ))
        },
    )?;
    Ok(rows
        .into_iter()
        .filter_map(|(id, (aired, skipped, newest))| {
            (aired > 0 && skipped == aired)
                .then_some(newest)
                .flatten()
                .map(|newest| (id, newest))
        })
        .collect())
}

/// How the show is presented. One that has nothing to look for until its next episode airs, and
/// that a feed carries, is not searching for a release even before a grab has proven its feed.
pub(super) fn standing(
    ctx: &Context<'_>,
    show: &Show,
    facts: &ShowFacts,
    caught_up: &HashMap<i64, Timestamp>,
) -> ShowStatus {
    let status = show_status(facts, ctx.now);
    let carried = show
        .id
        .is_some_and(|id| ctx.caches.carried.get(&id).copied().unwrap_or(false));
    if status == ShowStatus::Testing
        && carried
        && show.id.is_some_and(|id| caught_up.contains_key(&id))
    {
        return ShowStatus::Working;
    }
    status
}

fn aired(ctx: &Context<'_>, show_id: Option<i64>) -> Result<HashMap<i64, i64>> {
    per_show(
        ctx,
        "SELECT episodes.show_id, COUNT(episodes.id) FROM episodes \
         WHERE episodes.air_at <= ?{filter} GROUP BY episodes.show_id",
        show_id,
        vec![bound(ctx.now)],
        |row| Ok((row.get(0)?, row.get(1)?)),
    )
}

/// Each show's known episode air times of the last year, for the calendar: breaks between
/// episodes are real gaps, not every week.
fn airings(ctx: &Context<'_>, show_id: Option<i64>) -> Result<HashMap<i64, Vec<Value>>> {
    let since = ctx.now.checked_sub(AIRINGS_KEEP).unwrap_or(ctx.now);
    let filter = if show_id.is_some() {
        " AND episodes.show_id = ?"
    } else {
        ""
    };
    let sql = format!(
        "SELECT episodes.show_id, episodes.number, episodes.air_at, episodes.status, shows.watched \
         FROM episodes JOIN shows ON shows.id = episodes.show_id \
         WHERE episodes.air_at IS NOT NULL AND episodes.air_at >= ?{filter} \
         ORDER BY episodes.number"
    );
    let mut params = vec![bound(since)];
    params.extend(show_id.map(Into::into));
    let counting = ctx.settings.delete_watched;
    let mut statement = ctx.conn.prepare_cached(&sql)?;
    let mut rows = statement.query(params_from_iter(params))?;
    let mut all: HashMap<i64, Vec<Value>> = HashMap::new();
    while let Some(row) = rows.next()? {
        let number: i64 = row.get(1)?;
        let at: String = row.get(2)?;
        let status: String = row.get(3)?;
        let seen: Option<i64> = row.get(4)?;
        let status = EpisodeStatus::from_name(&status);
        // An episode skipped because the user watched it is done too, while watched episodes are
        // deleted.
        let done = status == Some(EpisodeStatus::Completed)
            || (counting
                && status == Some(EpisodeStatus::Skipped)
                && seen.is_some_and(|seen| number <= seen));
        all.entry(row.get(0)?).or_default().push(json!({
            "episode": number,
            "at": iso(kisetsu_db::storage::parse_timestamp(&at)),
            "status": status.map(value_of),
            "done": done,
        }));
    }
    Ok(all)
}

/// The Shows page section: Completed once it finished airing, even while episodes are still
/// downloading.
fn section(show: &Show, released: bool) -> &'static str {
    if show.completed || show.anilist_status.as_deref() == Some("FINISHED") {
        "completed"
    } else if released {
        "releasing"
    } else {
        "planned"
    }
}

/// `"FALL"` and 2023 as `"Fall 2023"`.
fn season_text(show: &Show) -> Value {
    let name = show.season_name.as_deref().filter(|name| !name.is_empty());
    let name = name.map(|name| {
        let mut letters = name.chars();
        letters.next().map_or_else(String::new, |first| {
            first
                .to_uppercase()
                .chain(letters.flat_map(char::to_lowercase))
                .collect()
        })
    });
    let year = show
        .season_year
        .filter(|year| *year != 0)
        .map(|year| year.to_string());
    let text = [name, year]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>()
        .join(" ");
    if text.is_empty() {
        Value::Null
    } else {
        json!(text)
    }
}

/// What the card of one show is made of beyond the show itself.
struct Standing<'a> {
    status: ShowStatus,
    counts: &'a HashMap<EpisodeStatus, i64>,
    feed: Option<&'a Feed>,
    replacing: i64,
    aired: i64,
    last_done: Option<i64>,
    released: bool,
    airings: Vec<Value>,
    release_delay: Option<SignedDuration>,
    watched_skipped: i64,
}

fn card(ctx: &Context<'_>, show: &Show, standing: Standing<'_>) -> Value {
    let language = &ctx.settings.title_language;
    let count = |status| standing.counts.get(&status).copied().unwrap_or(0);
    let completed = count(EpisodeStatus::Completed);
    // The episodes skipped because the user watched them are done as well.
    let done = completed + standing.watched_skipped;
    let home = std::env::home_dir().unwrap_or_default();
    let save_path = compress_home_path(
        &library::save_path(&ctx.settings, show, language),
        &home.to_string_lossy(),
    );
    let feed = standing.feed.map_or(Value::Null, |feed| {
        json!({
            "id": feed.id,
            "name": feed.name,
            "state": value_of(show.feed_state),
            "state_label": feed_state_label(show.feed_state),
            "failing": feed.failing_since.is_some(),
        })
    });
    json!({
        "id": show.id,
        "anilist_id": show.anilist_id,
        "title": show.title(language),
        "title_romaji": show.title_romaji,
        "title_english": show.title_english,
        "cover": show.cover_image,
        "cover_color": show.cover_color,
        "banner": show.banner_image,
        "accent_hues": hues(show),
        "season": season_text(show),
        "anilist_status": show.anilist_status,
        "status": status_value(standing.status),
        "status_label": status_label(standing.status),
        "released": standing.released,
        "section": section(show, standing.released),
        "last_done": standing.last_done,
        "paused": show.paused,
        "paused_by": show.paused_by.map(value_of),
        "completed": show.completed,
        "feed": feed,
        "progress": {"done": done, "aired": standing.aired.max(done), "total": show.total_episodes},
        // The AniList progress, while watched episodes are deleted; the episodes up to it are
        // not downloaded.
        "watched": if ctx.settings.delete_watched { json!(show.watched) } else { Value::Null },
        "counts": {
            "completed": completed,
            "downloading": count(EpisodeStatus::Downloading),
            "wanted": count(EpisodeStatus::Wanted),
            "missed": count(EpisodeStatus::Missed),
            "failed": count(EpisodeStatus::Failed),
            "replacing": standing.replacing,
        },
        "next_airing": show.next_airing_at.map_or(Value::Null, |at| json!({
            "episode": show.next_airing_episode,
            "at": iso(Some(at)),
        })),
        "airings": standing.airings,
        // How long after its air time a release usually comes out; the UI adds it to each air
        // time in the viewer's timezone.
        "release_delay_minutes": standing.release_delay.map(|delay| (delay.as_secs_f64() / 60.0).round_ties_even() as i64),
        "save_folder": show.save_folder,
        "save_path": save_path,
        "category": show.category,
        "seed_ratio": show.seed_ratio,
        "custom_aliases": show.custom_aliases(),
        "learned_name": show.learned_name,
        "schedule_synced_at": iso(show.schedule_synced_at),
    })
}

/// Every show's card, for the Shows page.
pub fn shows(ctx: &mut Context<'_>) -> Result<Value> {
    let all = Show::select(ctx.conn, "ORDER BY id", [])?;
    let feeds: HashMap<i64, Feed> = Feed::select(ctx.conn, "", [])?
        .into_iter()
        .filter_map(|feed| feed.id.map(|id| (id, feed)))
        .collect();
    let facts: HashMap<i64, ShowFacts> = facts_for(ctx, &all)?
        .into_iter()
        .map(|facts| (facts.id, facts))
        .collect();
    let counts = episode_counts(ctx)?;
    let (replacing, aired, last) = (
        replacing(ctx, None)?,
        aired(ctx, None)?,
        last_done(ctx, None)?,
    );
    let mut airings = airings(ctx, None)?;
    let delays = release_delays(ctx, None)?;
    let seen = watched_skipped(ctx, None)?;
    let caught_up = caught_up(ctx, None)?;
    let no_counts = HashMap::new();
    let mut cards = Vec::with_capacity(all.len());
    for show in &all {
        let id = show.id.unwrap_or_default();
        let Some(facts) = facts.get(&id) else {
            continue;
        };
        cards.push(card(
            ctx,
            show,
            Standing {
                status: standing(ctx, show, facts, &caught_up),
                counts: counts.get(&id).unwrap_or(&no_counts),
                feed: show.feed_id.and_then(|feed_id| feeds.get(&feed_id)),
                replacing: replacing.get(&id).copied().unwrap_or(0),
                aired: aired.get(&id).copied().unwrap_or(0),
                last_done: last.get(&id).copied(),
                released: !is_unreleased(facts, ctx.now),
                airings: airings.remove(&id).unwrap_or_default(),
                release_delay: delays.get(&id).copied(),
                watched_skipped: seen.get(&id).copied().unwrap_or(0),
            },
        ));
    }
    Ok(Value::Array(cards))
}

/// One show's card.
pub fn card_for(ctx: &mut Context<'_>, show: &Show) -> Result<Value> {
    let id = show.id.unwrap_or_default();
    let feed = match show.feed_id {
        Some(feed_id) => Feed::find(ctx.conn, "WHERE id = ?", [feed_id])?,
        None => None,
    };
    let facts = facts_for(ctx, std::slice::from_ref(show))?;
    let Some(facts) = facts.first() else {
        return Ok(Value::Null);
    };
    let counts: HashMap<EpisodeStatus, i64> = episode_counts(ctx)?.remove(&id).unwrap_or_default();
    let caught = caught_up(ctx, Some(id))?;
    let delays = release_delays(ctx, Some(&[id]))?;
    let standing = Standing {
        status: standing(ctx, show, facts, &caught),
        counts: &counts,
        feed: feed.as_ref(),
        replacing: replacing(ctx, Some(id))?.get(&id).copied().unwrap_or(0),
        aired: aired(ctx, Some(id))?.get(&id).copied().unwrap_or(0),
        last_done: last_done(ctx, Some(id))?.get(&id).copied(),
        released: !is_unreleased(facts, ctx.now),
        airings: airings(ctx, Some(id))?.remove(&id).unwrap_or_default(),
        release_delay: delays.get(&id).copied(),
        watched_skipped: watched_skipped(ctx, Some(id))?
            .get(&id)
            .copied()
            .unwrap_or(0),
    };
    Ok(card(ctx, show, standing))
}

/// An episode whose active download the last settle pass found stalled, and how many other
/// releases could replace it.
fn stalled(
    ctx: &mut Context<'_>,
    show: &Show,
    episode: &Episode,
    active: Option<&Download>,
    found: &mut Option<Vec<manual::Candidate>>,
) -> Result<Value> {
    let Some(active) = active else {
        return Ok(Value::Null);
    };
    if !active.id.is_some_and(|id| ctx.caches.stalled.contains(&id)) {
        return Ok(Value::Null);
    }
    if found.is_none() {
        *found = Some(manual::candidates(ctx, show, None, None)?);
    }
    let alternatives = manual::alternatives(ctx, show, episode, active, found.as_deref())?;
    Ok(json!({"alternatives": alternatives.len()}))
}

/// What the show's downloads were read as: by id, and the newest active one by episode.
struct Loaded {
    by_id: HashMap<i64, Download>,
    active: HashMap<i64, Download>,
}

fn episode_row(
    ctx: &mut Context<'_>,
    episode: &Episode,
    feed_names: &HashMap<i64, String>,
    show: &Show,
    found: &mut Option<Vec<manual::Candidate>>,
    loaded: &Loaded,
) -> Result<Value> {
    let current = episode
        .current_download_id
        .and_then(|id| loaded.by_id.get(&id));
    let active = episode.id.and_then(|id| loaded.active.get(&id));
    let release = current.or(active);
    let stalled = stalled(ctx, show, episode, active, found)?;
    Ok(json!({
        "stalled": stalled,
        "id": episode.id,
        "number": episode.number,
        "status": value_of(episode.status),
        "air_at": iso(episode.air_at),
        "schedule_state": value_of(episode.schedule_state),
        "attempts": episode.attempts,
        "retry_after": iso(episode.retry_after),
        "last_error": episode.last_error,
        "release": release.map(|release| json!({
            "title": release.title,
            "version": release.version,
            "group": release.release_group,
            "feed": release.feed_id.and_then(|id| feed_names.get(&id)),
            "source_episode": release.source_episode,
            "adopted": release.adopted,
            "tracked": release.info_hash.is_some(),
            "state": value_of(release.state),
            "completed_at": iso(release.completed_at),
            // Watched on AniList: "pending" while its torrent waits to be deleted, "deleted"
            // once it is.
            "watched": match (release.end_reason, &release.info_hash) {
                (Some(EndReason::Watched), Some(hash)) if !hash.is_empty() => json!("pending"),
                (Some(EndReason::Watched), _) => json!("deleted"),
                _ => Value::Null,
            },
        })),
        "replacing": active.filter(|active| active.supersedes_id.is_some()).map(|active| json!({
            "title": active.title,
            "version": active.version,
            "state": value_of(active.state),
        })),
    }))
}

/// One show's page: its card, its episodes with the release each holds, and its episode offsets.
pub fn show_view(ctx: &mut Context<'_>, show: &Show) -> Result<Value> {
    let Value::Object(mut page) = card_for(ctx, show)? else {
        return Ok(Value::Null);
    };
    let show_id = show.id.unwrap_or_default();
    let feed_names: HashMap<i64, String> = Feed::select(ctx.conn, "", [])?
        .into_iter()
        .filter_map(|feed| feed.id.map(|id| (id, feed.name)))
        .collect();
    let episodes = Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY number", [show_id])?;
    let offsets = EpisodeOffset::select(ctx.conn, "WHERE show_id = ?", [show_id])?;
    // Every download of the show at once, instead of two queries per episode.
    let downloads = downloads_of(ctx, show_id)?;
    let mut loaded = Loaded {
        by_id: HashMap::new(),
        active: HashMap::new(),
    };
    for download in downloads {
        if ACTIVE_DOWNLOAD_STATES.contains(&download.state) {
            loaded.active.insert(download.episode_id, download.clone());
        }
        if let Some(id) = download.id {
            loaded.by_id.insert(id, download);
        }
    }
    let mut found = None;
    let mut rows = Vec::with_capacity(episodes.len());
    for episode in &episodes {
        rows.push(episode_row(
            ctx,
            episode,
            &feed_names,
            show,
            &mut found,
            &loaded,
        )?);
    }
    let facts = facts_for(ctx, std::slice::from_ref(show))?;
    let matched_season = match facts.first() {
        Some(facts) => grab::enforced_season(ctx, show, facts)?,
        None => None,
    };
    let mut sorted: BTreeMap<i64, &EpisodeOffset> = BTreeMap::new();
    for offset in &offsets {
        sorted.insert(offset.feed_id, offset);
    }
    let offsets: Vec<Value> = sorted
        .values()
        .map(|offset| {
            json!({
                "feed_id": offset.feed_id,
                "feed": feed_names.get(&offset.feed_id),
                "offset": offset.offset,
                "source": value_of(offset.source),
                "evidence": offset.evidence_count,
            })
        })
        .collect();
    let extra: Map<String, Value> = [
        ("episodes".to_owned(), Value::Array(rows)),
        ("matched_season".to_owned(), json!(matched_season)),
        ("episode_offsets".to_owned(), Value::Array(offsets)),
    ]
    .into_iter()
    .collect();
    page.extend(extra);
    Ok(Value::Object(page))
}

/// Every download of the show, oldest first.
fn downloads_of(ctx: &Context<'_>, show_id: i64) -> Result<Vec<Download>> {
    Ok(Download::select(
        ctx.conn,
        "WHERE episode_id IN (SELECT id FROM episodes WHERE show_id = ?) \
         ORDER BY created_at, id",
        [show_id],
    )?)
}
