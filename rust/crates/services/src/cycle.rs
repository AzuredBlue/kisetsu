//! The units of work the engine's jobs run on the engine thread: small functions over plain data
//! in and out.

use std::collections::BTreeSet;

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::anilist::{ListEntry, Schedules};
use kisetsu_clients::palette::Hues;
use kisetsu_db::schema::Show;
use kisetsu_db::storage::format_timestamp;
use tracing::info;

use crate::context::{Context, get_state, iso_utc, set_state};
use crate::downloads::{self, Settled};
use crate::error::Result;
use crate::{adoption, feeds, grab, ingest, library, shows, tags, watched};

pub const LAST_SYNC_KEY: &str = "last_anilist_sync_at";
/// Version of the colour extraction; a stored result from another version is recomputed.
const ACCENT_VERSION: u32 = 4;
const EVENTS_KEEP: SignedDuration = SignedDuration::from_hours(90 * 24);

/// What the services report goes to the log, where the Logs page shows it.
fn say(lines: &[String]) {
    for line in lines {
        info!("{line}");
    }
}

/// What the AniList sync needs to know before it asks.
#[derive(Debug, Clone, PartialEq)]
pub struct SyncInput {
    pub username: String,
    pub sync_minutes: i64,
    /// The AniList ids of the shows already followed.
    pub monitored_ids: BTreeSet<i64>,
    /// Seconds since the last sync; `None` when there was none.
    pub last_sync_ago: Option<f64>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SyncApplied {
    pub added_ids: Vec<i64>,
    pub logs: Vec<String>,
}

/// Which shows' air times are due, and how much of each schedule to fetch.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SchedulePlan {
    /// The AniList ids of the shows due.
    pub due_ids: Vec<i64>,
    /// Shows whose past episodes are all dated: only what is still to come is fetched.
    pub windowed: Vec<i64>,
    /// Shows that need their whole schedule.
    pub full: Vec<i64>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct CycleResult {
    pub started: usize,
    pub missed: usize,
    pub adopted: usize,
    pub errors: Vec<String>,
    pub failed: Vec<String>,
    /// Seconds until the earliest pending candidate feed may be assigned.
    pub next_candidate_in: Option<f64>,
}

/// What housekeeping removed.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Removed {
    pub items: usize,
    pub tags: usize,
    pub events: usize,
}

/// A show whose colours are not computed for its current banner.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaletteTarget {
    pub show_id: i64,
    pub key: String,
    pub banner: String,
    pub cover: String,
}

pub fn sync_input(ctx: &Context<'_>) -> Result<SyncInput> {
    let last = get_state(ctx.conn, LAST_SYNC_KEY, "")?;
    let last_sync_ago = last
        .parse::<Timestamp>()
        .ok()
        .map(|last| ctx.now.duration_since(last).as_secs_f64());
    let monitored_ids = ctx
        .conn
        .prepare_cached("SELECT anilist_id FROM shows")?
        .query_map([], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    Ok(SyncInput {
        username: ctx.settings.anilist_username.trim().to_owned(),
        sync_minutes: ctx.settings.anilist_sync_minutes,
        monitored_ids,
        last_sync_ago,
    })
}

/// Takes AniList's list in: updates and adds shows, then settles what the new air times imply.
pub fn apply_sync(ctx: &mut Context<'_>, seasonal: &[ListEntry]) -> Result<SyncApplied> {
    let applied = shows::apply_anilist(ctx, seasonal)?;
    let mut logs = applied.logs;
    logs.extend(shows::rollover(ctx)?);
    say(&logs);
    set_state(ctx.conn, LAST_SYNC_KEY, &iso_utc(ctx.now))?;
    ingest::ensure_matches_current(ctx)?;
    tags::sync_tags(ctx)?;
    ctx.emit(&["shows", "status"]);
    Ok(SyncApplied {
        added_ids: applied.added.iter().filter_map(|show| show.id).collect(),
        logs,
    })
}

pub fn schedule_plan(ctx: &Context<'_>) -> Result<SchedulePlan> {
    let mut plan = SchedulePlan::default();
    for show in shows::schedules_due(ctx)? {
        plan.due_ids.push(show.anilist_id);
        if shows::schedule_is_dated(ctx, &show)? {
            plan.windowed.push(show.anilist_id);
        } else {
            plan.full.push(show.anilist_id);
        }
    }
    Ok(plan)
}

/// Those shows whose windowed fetch did not date every aired episode, so they need the full
/// schedule.
pub fn uncovered(ctx: &Context<'_>, anilist_ids: &[i64], covering: &Schedules) -> Result<Vec<i64>> {
    let mut result = Vec::new();
    for anilist_id in anilist_ids {
        let show = Show::find(ctx.conn, "WHERE anilist_id = ?", [anilist_id])?;
        if let Some(show) = show {
            let entries = covering.get(anilist_id).map(Vec::as_slice);
            if !shows::covers_undated(ctx, &show, entries)? {
                result.push(*anilist_id);
            }
        }
    }
    Ok(result)
}

/// Writes fetched air times onto the shows they are for; returns the shows updated and the names
/// with no schedule.
pub fn apply_schedule_results(
    ctx: &mut Context<'_>,
    anilist_ids: &[i64],
    schedules: &Schedules,
) -> Result<(usize, Vec<String>)> {
    let mut rows = if anilist_ids.is_empty() {
        Vec::new()
    } else {
        let ids = anilist_ids
            .iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(", ");
        Show::select(
            ctx.conn,
            &format!("WHERE anilist_id IN ({ids}) ORDER BY id"),
            [],
        )?
    };
    rows.retain(|show| schedules.contains_key(&show.anilist_id));
    let (count, lines) = shows::apply_schedules(ctx, &mut rows, schedules)?;
    say(&lines);
    Ok((count, lines))
}

pub fn rollover(ctx: &mut Context<'_>) -> Result<Vec<String>> {
    let lines = shows::rollover(ctx)?;
    say(&lines);
    Ok(lines)
}

/// Adopts what qBittorrent already has, then starts the downloads the stored items call for.
pub fn grab_cycle(ctx: &mut Context<'_>) -> Result<CycleResult> {
    ingest::ensure_matches_current(ctx)?;
    let adopted = adoption::adopt_existing(ctx)?;
    adoption::reattribute(ctx)?;
    ctx.checkpoint()?;
    let outcome = grab::grab_pass(ctx, None, None)?;
    let next_candidate_in = feeds::next_candidate_due(ctx)?
        .map(|due| ctx.now.duration_until(due).as_secs_f64().max(1.0));
    Ok(CycleResult {
        started: outcome.started,
        missed: outcome.missed,
        adopted: adopted.imported,
        errors: outcome.errors,
        failed: outcome.failed,
        next_candidate_in,
    })
}

/// Follows the downloads and deletes what the user watched; a finished episode may complete its
/// show, so air times are settled afterwards.
pub fn settle_cycle(ctx: &mut Context<'_>, observe_only: bool) -> Result<Settled> {
    let mut result = downloads::settle(ctx, observe_only)?;
    if !observe_only && watched::apply(ctx)? {
        result.changed = true;
    }
    if result.changed {
        say(&shows::rollover(ctx)?);
    }
    Ok(result)
}

/// Locks shows to the feed their saved files came from; returns how many and the shows still
/// unmatched.
///
/// Unmatched shows are held, so the top feed is not grabbed from and locked before a search has
/// run.
pub fn lock_saved_files(ctx: &mut Context<'_>) -> Result<(usize, Vec<i64>)> {
    let locked = adoption::lock_from_saved_files(ctx)?;
    let unmatched = adoption::unmatched_saved_files(ctx)?;
    for show in &unmatched {
        library::hold(ctx, show.id.unwrap_or_default());
    }
    Ok((
        locked.len(),
        unmatched.iter().filter_map(|show| show.id).collect(),
    ))
}

pub fn release_hold(ctx: &mut Context<'_>, show_ids: &[i64]) {
    for show_id in show_ids {
        library::release(ctx, *show_id);
    }
}

pub fn housekeeping(ctx: &mut Context<'_>) -> Result<Removed> {
    let items = ingest::prune(ctx)?;
    let tags = if ctx.qbit.is_some() {
        downloads::sweep_tags(ctx)?
    } else {
        0
    };
    tags::sync_tags(ctx)?;
    tags::sweep_retired(ctx)?;
    let cutoff = ctx.now.saturating_sub(EVENTS_KEEP).unwrap_or(ctx.now);
    let events = ctx.conn.execute(
        "DELETE FROM events WHERE at < ?",
        [format_timestamp(cutoff)],
    )?;
    Ok(Removed {
        items,
        tags,
        events,
    })
}

/// Shows whose colours are not computed for their current banner.
pub fn palette_targets(ctx: &Context<'_>) -> Result<Vec<PaletteTarget>> {
    let mut targets = Vec::new();
    for show in Show::select(ctx.conn, "ORDER BY id", [])? {
        let banner = show.banner_image.as_deref().unwrap_or_default();
        let cover = show.cover_image.as_deref().unwrap_or_default();
        let source = if banner.is_empty() { cover } else { banner };
        if source.is_empty() {
            continue;
        }
        let key = format!("{source}#{ACCENT_VERSION}");
        let computed = show.accent_src.as_deref() == Some(&key) && show.accent_hues.is_some();
        if !computed {
            targets.push(PaletteTarget {
                show_id: show.id.unwrap_or_default(),
                key,
                banner: banner.to_owned(),
                cover: cover.to_owned(),
            });
        }
    }
    Ok(targets)
}

/// Remembers the colours computed for a show's banner; `None` records that the image has none.
pub fn store_palette(
    ctx: &mut Context<'_>,
    show_id: i64,
    key: &str,
    hues: Option<&Hues>,
) -> Result<()> {
    let Some(mut show) = Show::find(ctx.conn, "WHERE id = ?", [show_id])? else {
        return Ok(());
    };
    show.accent_src = Some(key.to_owned());
    show.accent_hues = Some(hues.map_or_else(String::new, |hues| {
        format!(
            "{},{:.3},{},{},{:.3}",
            hues.accent_hue,
            hues.accent_saturation,
            hues.secondary_hue,
            hues.tint_hue,
            hues.tint_saturation
        )
    }));
    show.update(ctx.conn)?;
    ctx.emit(&["shows", &format!("show:{show_id}")]);
    Ok(())
}
