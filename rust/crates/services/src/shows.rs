//! The shows Kisetsu follows: taking AniList's list and schedules in, completion, pausing,
//! editing and deleting.
//!
//! A show is never deleted on its own: it stays until the user deletes it.

use std::collections::{BTreeSet, HashMap, HashSet};

use jiff::SignedDuration;
use kisetsu_clients::anilist::{AiringEpisode, ListEntry, Schedules};
use kisetsu_db::StoredEnum;
use kisetsu_db::schema::{
    DismissedShow, Download, DownloadState, EndReason, Episode, EpisodeOffset, Feed, FeedState,
    OffsetSource, PausedBy, ScheduleState, Show,
};
use kisetsu_domain::enums::EpisodeStatus;
use tracing::debug;

use crate::context::{Context, get_state, set_state};
use crate::downloads::{self, state_list};
use crate::error::{Error, Result};
use crate::library::{self, MovePlan};
use crate::{feeds, ingest, legacy, tags, watched};

/// Per-episode air times are refreshed this often.
pub const SCHEDULE_MAX_AGE: SignedDuration = SignedDuration::from_hours(6);
/// Shows whose past episodes are all dated only need what is still to come; this far back is
/// re-read.
pub const SCHEDULE_REFRESH_LOOKBACK: SignedDuration = SignedDuration::from_hours(3 * 24);
/// A first fetch reaches back far enough to date every episode of a current-season show.
pub const SCHEDULE_FULL_LOOKBACK: SignedDuration = SignedDuration::from_hours(180 * 24);
/// A finished show keeps its season open this long after the finale aired if it was missed.
const FINALE_GRACE: SignedDuration = SignedDuration::from_hours(14 * 24);
const ALL_PAUSED_KEY: &str = "all_paused";

/// Why a change to a show was not made. The messages are shown to the user.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Refusal {
    #[error("Selected feed not found")]
    FeedNotFound,
    #[error(
        "Some of this show's downloads are still moving to their folder. Try again in a moment."
    )]
    DownloadsMoving,
    #[error("Pick a feed before setting its episode offset.")]
    NoFeedForOffset,
    #[error("Episode {number} is not skipped.")]
    NotSkipped { number: i64 },
}

/// What taking AniList's list in did.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ApplyResult {
    /// The shows that were new.
    pub added: Vec<Show>,
    pub logs: Vec<String>,
}

/// What editing a show did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EditResult {
    pub message: String,
    /// The numbers of the episodes whose downloads from the old feed were canceled.
    pub canceled: Vec<i64>,
}

/// The changes the user asks for in a show. A field left `None` stays as it is.
#[derive(Debug, Clone, Default)]
pub struct Edit {
    pub save_folder: Option<String>,
    /// Replaces the hand-written names; the names AniList gives stay.
    pub aliases: Option<Vec<String>>,
    pub category: Option<String>,
    /// A negative ratio means the default.
    pub seed_ratio: Option<f64>,
    /// 0 or less returns the show to Auto-Discover; any other value pins that feed. Moving off a
    /// proven feed needs `release_lock`; what is still downloading from the old feed is then
    /// canceled.
    pub feed_id: Option<i64>,
    pub release_lock: bool,
    pub episode_offset: Option<i64>,
}

/// What deleting a show may also remove.
#[derive(Debug, Clone, Copy, Default)]
pub struct Delete<'a> {
    /// Remember the delete, so the next sync does not add the show back while it is still on the
    /// AniList list.
    pub dismiss: bool,
    pub delete_torrents: bool,
    pub delete_files: bool,
    /// The torrents to delete, when the caller already looked them up.
    pub hashes: Option<&'a [String]>,
}

pub fn all_paused(ctx: &Context<'_>) -> Result<bool> {
    Ok(get_state(ctx.conn, ALL_PAUSED_KEY, "")? == "1")
}

/// The show's episode rows, creating the ones from 1 up to the known season length.
pub fn ensure_episodes(ctx: &Context<'_>, show: &Show) -> Result<Vec<Episode>> {
    let mut rows = Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY id", [show.id])?;
    if let Some(total) = show.total_episodes.filter(|total| *total != 0) {
        rows = without_surplus(ctx, rows, total)?;
    }
    let mut known: BTreeSet<i64> = rows.iter().map(|row| row.number).collect();
    let target = match show.total_episodes.filter(|total| *total != 0) {
        Some(total) => total,
        None => show
            .next_airing_episode
            .unwrap_or(0)
            .max(known.last().copied().unwrap_or(0))
            .max(1),
    };
    for number in 1..=target {
        if known.contains(&number) {
            continue;
        }
        let mut row = Episode {
            id: None,
            show_id: show.id.unwrap_or_default(),
            number,
            air_at: None,
            schedule_state: ScheduleState::Unknown,
            status: EpisodeStatus::Wanted,
            current_download_id: None,
            retry_after: None,
            last_error: None,
            attempts: 0,
        };
        if watched::is_watched(ctx, show, number) {
            row.status = EpisodeStatus::Skipped;
        }
        row.id = Some(row.insert(ctx.conn)?);
        known.insert(number);
        rows.push(row);
    }
    Ok(rows)
}

/// Drops the rows past a season length AniList has lowered, which would wait for a release that
/// never comes. One that ever had a download, or that the user skipped, stays.
fn without_surplus(ctx: &Context<'_>, rows: Vec<Episode>, total: i64) -> Result<Vec<Episode>> {
    let surplus: Vec<i64> = rows
        .iter()
        .filter(|row| {
            row.number > total
                && matches!(row.status, EpisodeStatus::Wanted | EpisodeStatus::Missed)
        })
        .filter_map(|row| row.id)
        .collect();
    let mut used: HashSet<i64> = HashSet::new();
    let mut statement = ctx
        .conn
        .prepare_cached("SELECT 1 FROM downloads WHERE episode_id = ? LIMIT 1")?;
    for id in &surplus {
        if statement.exists([id])? {
            used.insert(*id);
        }
    }
    let mut kept = Vec::with_capacity(rows.len());
    for row in rows {
        match row.id {
            Some(id) if surplus.contains(&id) && !used.contains(&id) => {
                ctx.conn
                    .execute("DELETE FROM episodes WHERE id = ?", [id])?;
            }
            _ => kept.push(row),
        }
    }
    Ok(kept)
}

/// Whether every episode of a finished season is accounted for.
///
/// Every episode up to the season length must be completed, skipped by the user or missed (no
/// longer retrievable), and at least one completed, unless the user watched them all. A finale
/// that was only just given up on keeps the season open for as long as the grabber keeps looking
/// for it.
pub fn final_episode_accounted(ctx: &Context<'_>, show: &Show) -> Result<bool> {
    let (Some(total), Some(show_id)) = (show.total_episodes.filter(|total| *total > 0), show.id)
    else {
        return Ok(false);
    };
    if show
        .next_airing_episode
        .is_some_and(|next| next != 0 && next < total)
    {
        return Ok(false);
    }
    if show.next_airing_episode == Some(total) && show.next_airing_at.is_some_and(|at| at > ctx.now)
    {
        return Ok(false);
    }
    let episodes = Episode::select(
        ctx.conn,
        "WHERE show_id = ? AND number <= ? ORDER BY id",
        (show_id, total),
    )?;
    if episodes.is_empty() {
        return Ok(false);
    }
    let accounted = |episode: &Episode| match episode.status {
        EpisodeStatus::Completed | EpisodeStatus::Skipped => true,
        EpisodeStatus::Missed => {
            let recent_finale = episode.number == total
                && episode
                    .air_at
                    .is_some_and(|air_at| ctx.now.duration_since(air_at) <= FINALE_GRACE);
            !recent_finale
        }
        _ => false,
    };
    // A season the user watched to the end needs nothing downloaded, even when every episode was
    // skipped as watched.
    let obtained = episodes
        .iter()
        .any(|episode| episode.status == EpisodeStatus::Completed)
        || watched::is_watched(ctx, show, total);
    Ok(episodes.iter().all(accounted) && obtained)
}

fn set_if<T: PartialEq>(field: &mut T, value: T) -> bool {
    if *field == value {
        return false;
    }
    *field = value;
    true
}

/// `Some(text)` unless `text` is empty.
fn present(text: &str) -> Option<String> {
    (!text.is_empty()).then(|| text.to_owned())
}

fn update_existing(
    ctx: &mut Context<'_>,
    show: &mut Show,
    data: &ListEntry,
    logs: &mut Vec<String>,
) -> Result<bool> {
    let mut updated = false;
    for (field, value) in [
        (&mut show.title_romaji, present(&data.title_romaji)),
        (&mut show.title_english, present(&data.title_english)),
        (&mut show.cover_image, present(&data.cover_image)),
        (&mut show.cover_color, present(&data.cover_color)),
        (&mut show.banner_image, present(&data.banner_image)),
        (
            &mut show.season_name,
            data.season
                .as_ref()
                .and_then(|season| present(season.as_str())),
        ),
    ] {
        if value.is_some() {
            updated |= set_if(field, value);
        }
    }
    if let Some(year) = data.season_year.filter(|year| *year != 0) {
        updated |= set_if(&mut show.season_year, Some(i64::from(year)));
    }
    updated |= set_if(&mut show.total_episodes, data.total_episodes);
    updated |= set_if(&mut show.anilist_status, present(data.status.as_str()));
    updated |= set_if(
        &mut show.list_status,
        data.list_status
            .as_ref()
            .and_then(|status| present(status.as_str())),
    );
    if let Some(progress) = data.progress {
        updated |= watched::note_progress(ctx, show, progress)?;
    }

    let incoming: BTreeSet<&str> = data
        .aliases
        .iter()
        .map(|alias| alias.trim())
        .filter(|alias| !alias.is_empty())
        .collect();
    let mut aliases = show.aliases();
    let missing: Vec<&str> = incoming
        .iter()
        .copied()
        .filter(|alias| !aliases.iter().any(|known| known == alias))
        .collect();
    if !missing.is_empty() {
        aliases.extend(missing.into_iter().map(str::to_owned));
        show.set_aliases(&aliases);
        updated = true;
    }

    let name = show.title(&ctx.settings.title_language);
    // The user's own list status never completes a show: a show finished watching keeps
    // downloading what it lacks.
    let finished = data.status.as_str() == "FINISHED" && data.next_airing_episode.is_none();
    if finished {
        if final_episode_accounted(ctx, show)? {
            updated |= set_if(&mut show.next_airing_episode, None);
            updated |= set_if(&mut show.next_airing_at, None);
            if !show.completed {
                show.completed = true;
                updated = true;
                let total = show.total_episodes.unwrap_or_default();
                logs.push(format!("'{name}' finished ({total}/{total} episodes)."));
            }
        } else if let Some(last) = show
            .total_episodes
            .filter(|total| *total != 0)
            .or(show.next_airing_episode.filter(|next| *next != 0))
        {
            updated |= set_if(&mut show.next_airing_episode, Some(last));
        }
    } else {
        updated |= set_if(&mut show.next_airing_episode, data.next_airing_episode);
        updated |= set_if(&mut show.next_airing_at, data.next_airing_at);
    }

    // A show completed on download evidence is reopened if that evidence was wrong.
    if show.completed && !final_episode_accounted(ctx, show)? {
        show.completed = false;
        show.schedule_synced_at = None;
        updated = true;
    }
    Ok(updated)
}

/// A new, unsaved show for one AniList list entry.
pub fn show_from_entry(data: &ListEntry, paused_all: bool) -> Show {
    let mut show = Show {
        id: None,
        anilist_id: data.anilist_id,
        title_romaji: present(&data.title_romaji),
        title_english: present(&data.title_english),
        aliases_json: "[]".to_owned(),
        custom_aliases_json: "[]".to_owned(),
        cover_image: present(&data.cover_image),
        cover_color: present(&data.cover_color),
        banner_image: present(&data.banner_image),
        accent_hues: None,
        accent_src: None,
        cover_edge: None,
        cover_edge_src: None,
        season_name: data
            .season
            .as_ref()
            .map(|season| season.as_str().to_owned()),
        season_year: data.season_year.map(i64::from),
        total_episodes: data.total_episodes,
        anilist_status: present(data.status.as_str()),
        list_status: data
            .list_status
            .as_ref()
            .and_then(|status| present(status.as_str())),
        next_airing_episode: data.next_airing_episode,
        next_airing_at: data.next_airing_at,
        schedule_synced_at: None,
        feed_id: None,
        feed_state: FeedState::None,
        candidate_since: None,
        learned_name: None,
        learned_group: None,
        save_folder: String::new(),
        category: None,
        seed_ratio: None,
        qbit_tag: None,
        paused: paused_all,
        paused_by: paused_all.then_some(PausedBy::All),
        completed: false,
        last_backfill_at: None,
        watched: data.progress,
        watched_applied: data.progress,
    };
    show.set_aliases(&data.aliases);
    show
}

/// Updates the followed shows from AniList's list and adds the new ones.
pub fn apply_anilist(ctx: &mut Context<'_>, seasonal_list: &[ListEntry]) -> Result<ApplyResult> {
    let mut result = ApplyResult::default();
    let mut existing: HashMap<i64, Show> = Show::select(ctx.conn, "ORDER BY id", [])?
        .into_iter()
        .map(|show| (show.anilist_id, show))
        .collect();
    // Shows the user deleted stay out until they leave the AniList list.
    let mut dismissed: HashSet<i64> = DismissedShow::select(ctx.conn, "ORDER BY anilist_id", [])?
        .into_iter()
        .map(|row| row.anilist_id)
        .collect();
    let listed: HashSet<i64> = seasonal_list.iter().map(|entry| entry.anilist_id).collect();
    if !listed.is_empty() {
        for gone in dismissed.difference(&listed) {
            ctx.conn
                .execute("DELETE FROM dismissed_shows WHERE anilist_id = ?", [gone])?;
        }
        dismissed.retain(|id| listed.contains(id));
    }
    let paused_all = all_paused(ctx)?;
    for data in seasonal_list {
        let anilist_id = data.anilist_id;
        if anilist_id == 0
            || (dismissed.contains(&anilist_id) && !existing.contains_key(&anilist_id))
        {
            continue;
        }
        if let Some(show) = existing.get_mut(&anilist_id) {
            if update_existing(ctx, show, data, &mut result.logs)? {
                show.update(ctx.conn)?;
            }
            ensure_episodes(ctx, show)?;
            continue;
        }
        let mut show = show_from_entry(data, paused_all);
        show.id = Some(show.insert(ctx.conn)?);
        legacy::apply_pending(ctx, &mut show)?;
        ensure_episodes(ctx, &show)?;
        existing.insert(anilist_id, show.clone());
        result.added.push(show);
    }
    if !result.added.is_empty() {
        result.logs.push(format!(
            "Added {} new show(s) from AniList.",
            result.added.len()
        ));
        ctx.emit(&["shows"]);
    }
    Ok(result)
}

/// Removes a show and its history; returns how many torrents were deleted from qBittorrent.
pub fn delete_show(
    ctx: &mut Context<'_>,
    show: &mut Show,
    reason: &str,
    options: Delete<'_>,
) -> Result<usize> {
    let hashes: Vec<String> = if !options.delete_torrents {
        Vec::new()
    } else if let Some(given) = options.hashes {
        given.to_vec()
    } else {
        library::find_show_torrents(ctx, show)?
    };
    // Only a tag Kisetsu put on this show's torrents is its own to clear.
    let show_tag = show.qbit_tag.clone().filter(|tag| !tag.is_empty());
    for mut episode in Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY id", [show.id])? {
        // What is still downloading may be finished by now, so its files only go if the user asked for that.
        let leave = !(options.delete_torrents && options.delete_files);
        downloads::cancel_episode(ctx, show, &mut episode, reason, false, leave)?;
    }
    if options.dismiss {
        let row = DismissedShow {
            anilist_id: show.anilist_id,
            created_at: ctx.now,
        };
        if row.update(ctx.conn)? == 0 {
            row.insert(ctx.conn)?;
        }
    }
    ctx.conn
        .execute("DELETE FROM shows WHERE id = ?", [show.id])?;
    if !hashes.is_empty() {
        ctx.qbit()?.delete(&hashes, options.delete_files)?;
    }
    match show_tag {
        // The torrents stay, so the tag does too until none carries it; housekeeping clears it then.
        Some(tag) if !options.delete_torrents => tags::retire(ctx, Some(&tag))?,
        Some(tag) => {
            if let Err(error) = ctx.qbit()?.delete_tags(std::slice::from_ref(&tag)) {
                debug!("Could not delete the tag '{tag}': {error}");
            }
        }
        None => {}
    }
    ctx.caches.matchers = None;
    ctx.emit(&["shows", "history"]);
    Ok(hashes.len())
}

/// Takes back the user's choice to skip an episode, or gets one deleted after it was watched
/// again, so it is grabbed like any other.
pub fn want_episode(
    ctx: &mut Context<'_>,
    show: &mut Show,
    episode: &mut Episode,
) -> Result<String> {
    let cur = downloads::current(ctx, episode)?;
    let deleted = episode.status == EpisodeStatus::Completed
        && cur.is_some_and(|cur| {
            cur.end_reason == Some(EndReason::Watched) && cur.info_hash.is_none()
        });
    if episode.status != EpisodeStatus::Skipped && !deleted {
        return Err(Refusal::NotSkipped {
            number: episode.number,
        }
        .into());
    }
    if deleted {
        // The deleted release stays in the history; the episode no longer has it.
        episode.current_download_id = None;
    }
    episode.status = EpisodeStatus::Wanted;
    episode.update(ctx.conn)?;
    if show.completed {
        show.completed = false;
        show.update(ctx.conn)?;
    }
    ctx.emit(&["shows", &format!("show:{}", show.id.unwrap_or_default())]);
    Ok(format!("Episode {} will be downloaded.", episode.number))
}

// -- schedules ----------------------------------------------------------------------------------

/// Episode numbers that should already have aired but have no air time yet.
pub fn undated_aired_numbers(ctx: &Context<'_>, show: &Show) -> Result<BTreeSet<i64>> {
    let aired = match show.next_airing_episode.filter(|next| *next != 0) {
        Some(next) => next - 1,
        None => show.total_episodes.unwrap_or(0),
    };
    if aired <= 0 {
        return Ok(BTreeSet::new());
    }
    let dated: BTreeSet<i64> = ctx
        .conn
        .prepare_cached(
            "SELECT number FROM episodes WHERE show_id = ? AND number <= ? AND air_at IS NOT NULL",
        )?
        .query_map((show.id, aired), |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok((1..=aired)
        .filter(|number| !dated.contains(number))
        .collect())
}

pub fn schedule_is_dated(ctx: &Context<'_>, show: &Show) -> Result<bool> {
    Ok(show.schedule_synced_at.is_some() && undated_aired_numbers(ctx, show)?.is_empty())
}

/// Whether a windowed fetch returned an air time for every aired episode that still lacked one.
pub fn covers_undated(
    ctx: &Context<'_>,
    show: &Show,
    entries: Option<&[AiringEpisode]>,
) -> Result<bool> {
    let Some(entries) = entries else {
        return Ok(false);
    };
    let returned: BTreeSet<i64> = entries.iter().map(|entry| entry.episode).collect();
    Ok(undated_aired_numbers(ctx, show)?.is_subset(&returned))
}

/// Shows whose air times are older than the refresh age, the longest ago first.
pub fn schedules_due(ctx: &Context<'_>) -> Result<Vec<Show>> {
    let cutoff = ctx.now.saturating_sub(SCHEDULE_MAX_AGE).unwrap_or(ctx.now);
    let mut due: Vec<Show> = Show::select(ctx.conn, "WHERE completed = 0 ORDER BY id", [])?
        .into_iter()
        .filter(|show| show.schedule_synced_at.is_none_or(|synced| synced < cutoff))
        .collect();
    due.sort_by_key(|show| show.schedule_synced_at);
    Ok(due)
}

/// Writes fetched air dates onto the shows' episodes; returns the shows updated and the names
/// with no schedule.
pub fn apply_schedules(
    ctx: &mut Context<'_>,
    due: &mut [Show],
    schedules: &Schedules,
) -> Result<(usize, Vec<String>)> {
    let mut missing = Vec::new();
    let mut updated = 0;
    for show in due {
        let Some(entries) = schedules.get(&show.anilist_id) else {
            missing.push(show.title(&ctx.settings.title_language));
            continue;
        };
        let mut by_number: HashMap<i64, Episode> = ensure_episodes(ctx, show)?
            .into_iter()
            .map(|row| (row.number, row))
            .collect();
        for entry in entries {
            let Some(episode) = by_number.get_mut(&entry.episode) else {
                continue;
            };
            episode.air_at = Some(entry.airing_at);
            episode.schedule_state = if entry.airing_at <= ctx.now {
                ScheduleState::Aired
            } else {
                ScheduleState::Scheduled
            };
            if entry.airing_at > ctx.now && episode.status == EpisodeStatus::Missed {
                // It was given up on by an air time AniList has since moved: it has not aired,
                // so nothing was missed.
                episode.status = EpisodeStatus::Wanted;
            }
            episode.update(ctx.conn)?;
        }
        show.schedule_synced_at = Some(ctx.now);
        show.update(ctx.conn)?;
        updated += 1;
    }
    if updated > 0 {
        ctx.emit(&["shows"]);
    }
    Ok((updated, missing))
}

/// Deals with shows whose AniList air time has passed.
///
/// The schedule is never invented: an overdue episode is recorded as aired against its real air
/// time and left there, because guessing a weekly rollover would renumber a season against feeds
/// that carry absolute numbering.
pub fn rollover(ctx: &mut Context<'_>) -> Result<Vec<String>> {
    let mut logs = Vec::new();
    for mut show in Show::select(ctx.conn, "WHERE completed = 0 ORDER BY id", [])? {
        let Some(next_at) = show
            .next_airing_at
            .filter(|at| !show.paused && *at <= ctx.now)
        else {
            continue;
        };
        let current = show
            .next_airing_episode
            .filter(|next| *next != 0)
            .unwrap_or(1);
        if let Some(total) = show.total_episodes.filter(|total| *total != 0) {
            if current >= total {
                if final_episode_accounted(ctx, &show)? {
                    show.next_airing_episode = None;
                    show.next_airing_at = None;
                    show.completed = true;
                    show.update(ctx.conn)?;
                    logs.push(format!(
                        "'{}' finished ({total}/{total} episodes).",
                        show.title(&ctx.settings.title_language)
                    ));
                }
                continue;
            }
        }
        match Episode::find(
            ctx.conn,
            "WHERE show_id = ? AND number = ?",
            (show.id, current),
        )? {
            Some(mut episode) => {
                if episode.schedule_state != ScheduleState::Aired {
                    episode.schedule_state = ScheduleState::Aired;
                    episode.update(ctx.conn)?;
                }
            }
            None => {
                Episode {
                    id: None,
                    show_id: show.id.unwrap_or_default(),
                    number: current,
                    air_at: Some(next_at),
                    schedule_state: ScheduleState::Aired,
                    status: EpisodeStatus::Wanted,
                    current_download_id: None,
                    retry_after: None,
                    last_error: None,
                    attempts: 0,
                }
                .insert(ctx.conn)?;
            }
        }
    }
    if !logs.is_empty() {
        ctx.emit(&["shows"]);
    }
    Ok(logs)
}

// -- pausing ------------------------------------------------------------------------------------

/// Stops the show's grabs. Torrents already added keep going; only adds that qBittorrent has not
/// shown yet are canceled.
pub fn pause(ctx: &mut Context<'_>, show: &mut Show, by: PausedBy) -> Result<()> {
    show.paused = true;
    show.paused_by = Some(by);
    show.update(ctx.conn)?;
    for mut episode in Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY id", [show.id])? {
        downloads::cancel_episode(ctx, show, &mut episode, "Show paused by user.", true, false)?;
    }
    ctx.emit(&["shows", &format!("show:{}", show.id.unwrap_or_default())]);
    Ok(())
}

pub fn resume(ctx: &mut Context<'_>, show: &mut Show) -> Result<()> {
    show.paused = false;
    show.paused_by = None;
    show.update(ctx.conn)?;
    ctx.emit(&["shows", &format!("show:{}", show.id.unwrap_or_default())]);
    Ok(())
}

/// Pauses every show that is not paused or completed; returns how many.
pub fn pause_all(ctx: &mut Context<'_>) -> Result<usize> {
    let shows = Show::select(
        ctx.conn,
        "WHERE completed = 0 AND paused = 0 ORDER BY id",
        [],
    )?;
    let count = shows.len();
    for mut show in shows {
        pause(ctx, &mut show, PausedBy::All)?;
    }
    set_state(ctx.conn, ALL_PAUSED_KEY, "1")?;
    Ok(count)
}

/// Resumes only the shows `pause_all` paused; shows the user paused by hand stay paused.
pub fn resume_all(ctx: &mut Context<'_>) -> Result<usize> {
    let shows = Show::select(
        ctx.conn,
        "WHERE paused_by = ? ORDER BY id",
        [PausedBy::All.name()],
    )?;
    let count = shows.len();
    for mut show in shows {
        resume(ctx, &mut show)?;
    }
    set_state(ctx.conn, ALL_PAUSED_KEY, "0")?;
    Ok(count)
}

/// Returns every hand-pinned show to automatic discovery; shows with a proven feed keep it.
pub fn auto_discover_all(ctx: &mut Context<'_>) -> Result<usize> {
    let shows = Show::select(
        ctx.conn,
        "WHERE feed_state = ? ORDER BY id",
        [FeedState::Pinned.name()],
    )?;
    let count = shows.len();
    for mut show in shows {
        release_feed(ctx, &mut show)?;
    }
    Ok(count)
}

/// Back to automatic discovery. Adds qBittorrent has not shown yet are canceled: they come from
/// the old feed and, once they appeared, would lock the show to it again.
pub fn release_feed(ctx: &mut Context<'_>, show: &mut Show) -> Result<()> {
    feeds::release(ctx, show)?;
    let pending = Download::select(
        ctx.conn,
        &format!(
            "WHERE episode_id IN (SELECT id FROM episodes WHERE show_id = ?) \
             AND state IN ({}) ORDER BY id",
            state_list(&[DownloadState::Adding, DownloadState::RetryWait])
        ),
        [show.id],
    )?;
    for mut dl in pending {
        if downloads::locate(ctx, &mut dl)?.is_none() {
            downloads::cancel_download(
                ctx,
                show,
                &mut dl,
                "Canceled: the show went back to auto-discover.",
                true,
            )?;
        }
    }
    Ok(())
}

// -- editing ------------------------------------------------------------------------------------

/// The user's own numbering offset for a feed; it survives a canceled grab.
pub fn set_manual_offset(
    ctx: &Context<'_>,
    show: &Show,
    feed: Option<&Feed>,
    offset: i64,
) -> Result<()> {
    let Some(feed) = feed else {
        return Err(Refusal::NoFeedForOffset.into());
    };
    let mut row = EpisodeOffset::find(
        ctx.conn,
        "WHERE show_id = ? AND feed_id = ?",
        (show.id, feed.id),
    )?
    .unwrap_or(EpisodeOffset {
        id: None,
        show_id: show.id.unwrap_or_default(),
        feed_id: feed.id.unwrap_or_default(),
        offset: 0,
        source: OffsetSource::Inferred,
        evidence_count: 0,
        first_evidence_at: None,
        last_evidence_at: None,
    });
    row.offset = offset;
    row.source = OffsetSource::Manual;
    row.last_evidence_at = Some(ctx.now);
    if row.id.is_some() {
        row.update(ctx.conn)?;
    } else {
        row.insert(ctx.conn)?;
    }
    Ok(())
}

/// Moves the downloads already made to the show's new folder, unless qBittorrent cannot be asked.
fn plan_folder_move(
    ctx: &mut Context<'_>,
    show: &Show,
    folder: &str,
) -> Result<Option<Vec<MovePlan>>> {
    let settings = ctx.settings.clone();
    let language = settings.title_language.clone();
    let plan = library::plan_move(
        ctx,
        std::slice::from_ref(show),
        |shown| {
            let mut moved = shown.clone();
            moved.save_folder = folder.to_owned();
            library::save_path(&settings, &moved, &language)
        },
        true,
    );
    match plan {
        Ok(plan) => Ok(Some(plan)),
        Err(Error::Qbit(_)) => Ok(None),
        Err(other) => Err(other),
    }
}

/// Applies the user's changes to a show.
pub fn edit_show(ctx: &mut Context<'_>, show: &mut Show, edit: &Edit) -> Result<EditResult> {
    let feed = match edit.feed_id.filter(|id| *id > 0) {
        Some(id) => Some(feeds::feed_by_id(ctx, Some(id))?.ok_or(Refusal::FeedNotFound)?),
        None => None,
    };
    if edit.feed_id.is_some()
        && show.feed_state == FeedState::Locked
        && !edit.release_lock
        && feed.as_ref().is_none_or(|feed| show.feed_id != feed.id)
    {
        return Err(Error::FeedLocked {
            title: show.title("english"),
        });
    }
    let mut moves: Option<Vec<MovePlan>> = None;
    let mut moves_unreachable = false;
    if let Some(requested) = &edit.save_folder {
        let value = requested.trim();
        let folder = if value.is_empty() || value == "{name}" {
            ""
        } else {
            value
        };
        if folder != show.save_folder {
            match plan_folder_move(ctx, show, folder)? {
                Some(plan) => {
                    if plan.iter().any(|item| item.moving > 0) {
                        return Err(Refusal::DownloadsMoving.into());
                    }
                    moves = Some(plan);
                }
                None => moves_unreachable = true,
            }
            show.save_folder = folder.to_owned();
        }
    }
    if let Some(aliases) = &edit.aliases {
        // Only the hand-written set is replaced; AniList re-supplies its own on every sync.
        let names: Vec<&str> = aliases
            .iter()
            .map(|alias| alias.trim())
            .filter(|alias| !alias.is_empty())
            .collect();
        show.set_custom_aliases(&names);
        ctx.caches.matchers = None;
    }
    if let Some(category) = &edit.category {
        show.category = present(category.trim());
    }
    if let Some(ratio) = edit.seed_ratio {
        show.seed_ratio = (ratio >= 0.0).then_some(ratio);
    }
    // What is saved so far is what the steps below read.
    show.update(ctx.conn)?;
    let mut result = EditResult {
        message: "Saved.".to_owned(),
        canceled: Vec::new(),
    };
    if edit.feed_id.is_some() {
        match &feed {
            None => {
                release_feed(ctx, show)?;
                result.message = format!(
                    "'{}' set to Auto-Discover.",
                    show.title(&ctx.settings.title_language)
                );
            }
            Some(feed) => {
                feeds::pin(ctx, show, feed, edit.release_lock)?;
                result.canceled = downloads::cancel_from_other_feeds(
                    ctx,
                    show,
                    feed.id,
                    "Canceled: the show's feed was changed.",
                )?;
                result.message = format!("Pinned to '{}'.", feed.name);
                if !result.canceled.is_empty() {
                    let numbers = result
                        .canceled
                        .iter()
                        .map(i64::to_string)
                        .collect::<Vec<_>>()
                        .join(", ");
                    result.message.push_str(&format!(
                        " Canceled {} unfinished download(s) from the old feed (Ep {numbers}); they will be fetched from the new one.",
                        result.canceled.len()
                    ));
                }
            }
        }
    }
    if let Some(offset) = edit.episode_offset {
        let target = match feed {
            Some(feed) => Some(feed),
            None => feeds::feed_by_id(ctx, show.feed_id)?,
        };
        set_manual_offset(ctx, show, target.as_ref(), offset)?;
    }
    show.update(ctx.conn)?;
    if edit.aliases.is_some() {
        ingest::ensure_matches_current(ctx)?;
    }
    ctx.emit(&["shows", &format!("show:{}", show.id.unwrap_or_default())]);
    match moves.filter(|plan| !plan.is_empty()) {
        Some(plan) => {
            ctx.checkpoint()?;
            let moved = library::relocate_torrents(ctx, &plan)?;
            result.message.push(' ');
            result.message.push_str(&moved.logs.join(" "));
        }
        None if moves_unreachable => result.message.push_str(
            " qBittorrent could not be reached, so the downloads already made stayed in the old folder.",
        ),
        None => {}
    }
    Ok(result)
}
