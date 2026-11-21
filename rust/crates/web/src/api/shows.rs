//! Showing, editing, pausing and deleting shows, and acting on their episodes.

use std::thread;
use std::time::Duration;

use kisetsu_clients::qbittorrent::QbitError;
use kisetsu_db::schema::{FeedState, PausedBy};
use kisetsu_services::manual::{FeedGroup, Match, Rejected};
use kisetsu_services::shows::{Delete, Edit};
use kisetsu_services::{Error, Result, backfill, library, manual, shows};
use serde_json::{Value, json};
use tracing::{info, warn};

use super::{checked, episode_or_404, ok, poke, show_or_404};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::views::{self, iso};

/// How many times, half a second apart, a delete asks qBittorrent whether the torrents are gone
/// before removing the folder.
const GONE_CHECKS: u32 = 20;
const GONE_PAUSE: Duration = Duration::from_millis(500);
/// How many releases of a feed the matches list shows.
const MATCH_LIMIT: usize = 30;

/// The show a path names.
fn show_id(call: &Call<'_, '_>, problems: &mut Problems) -> i64 {
    Reader::new(problems, "path")
        .path_integer("show_id", &call.params[0])
        .unwrap_or_default()
}

fn episode_id(call: &Call<'_, '_>, problems: &mut Problems) -> i64 {
    Reader::new(problems, "path")
        .path_integer("episode_id", &call.params[1])
        .unwrap_or_default()
}

/// A route whose only input is the show in its path.
fn show_only(call: &Call<'_, '_>) -> std::result::Result<i64, ApiError> {
    let mut problems = Problems::default();
    let id = show_id(call, &mut problems);
    checked(problems)?;
    Ok(id)
}

pub(super) fn list(call: &mut Call<'_, '_>) -> ApiResult {
    ok(&call.runtime().engine.read(views::shows)?)
}

pub(super) fn pause_all(call: &mut Call<'_, '_>) -> ApiResult {
    let paused = call.runtime().engine.call(shows::pause_all)?;
    ok(&json!({"paused": paused}))
}

pub(super) fn resume_all(call: &mut Call<'_, '_>) -> ApiResult {
    let resumed = call.runtime().engine.call(shows::resume_all)?;
    poke(call, "grab");
    ok(&json!({"resumed": resumed}))
}

pub(super) fn auto_discover_all(call: &mut Call<'_, '_>) -> ApiResult {
    let released = call.runtime().engine.call(shows::auto_discover_all)?;
    poke(call, "grab");
    ok(&json!({"released": released}))
}

pub(super) fn get(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    let page = call.runtime().engine.read(|ctx| {
        let show = show_or_404(ctx, id)?;
        views::show_view(ctx, &show)
    })?;
    ok(&page)
}

fn match_json(found: &Match) -> Value {
    json!({
        "title": found.title,
        "display_title": found.display_title,
        "feed_id": found.feed_id,
        "feed_name": found.feed_name,
        "episode": found.episode,
        "version": found.version,
        "episode_status": views::value_of(found.episode_status),
        "this_release": found.this_release,
        "restore": found.restore,
        "replaces": found.replaces,
        "downloadable": found.downloadable,
    })
}

fn rejected_json(item: &Rejected) -> Value {
    json!({
        "title": item.title,
        "reason": item.reason,
        "published_at": iso(item.published_at),
    })
}

fn group_json(group: &FeedGroup) -> Value {
    let mut out = json!({
        "feed_id": group.feed_id,
        "feed_name": group.feed_name,
        "role": group.role.as_str(),
        "usable": group.usable,
        "matches": group.matches.iter().map(match_json).collect::<Vec<_>>(),
    });
    if let Some(map) = out.as_object_mut() {
        if group.unavailable {
            map.insert("unavailable".to_owned(), json!(true));
        }
        if let Some(rejected) = &group.rejected {
            map.insert(
                "rejected".to_owned(),
                Value::Array(rejected.iter().map(rejected_json).collect()),
            );
        }
    }
    out
}

pub(super) fn feed_matches(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    let groups = call.runtime().engine.read(|ctx| {
        let show = show_or_404(ctx, id)?;
        manual::feed_matches(ctx, &show, MATCH_LIMIT)
    })?;
    ok(&Value::Array(groups.iter().map(group_json).collect()))
}

pub(super) fn edit(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = show_id(call, &mut problems);
    let body = call.object(&mut problems);
    let mut change = Edit::default();
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        change.save_folder = reader.optional_string(body, "save_folder");
        change.aliases = reader.optional_strings(body, "aliases");
        change.category = reader.optional_string(body, "category");
        change.seed_ratio = reader.optional_float(body, "seed_ratio");
        change.feed_id = reader.optional_integer(body, "feed_id");
        change.release_lock = reader.flag(body, "release_lock");
        change.episode_offset = reader.optional_integer(body, "episode_offset");
        reader.forbid_others(
            body,
            &[
                "save_folder",
                "aliases",
                "category",
                "seed_ratio",
                "feed_id",
                "release_lock",
                "episode_offset",
            ],
        );
    }
    checked(problems)?;
    let (result, feed_changed) = call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        let before = show.feed_id;
        let result = shows::edit_show(ctx, &mut show, &change)?;
        Ok((result, show.feed_id.is_some() && show.feed_id != before))
    })?;
    poke(call, "grab");
    if feed_changed {
        // The new feed's RSS may have lost the show's older episodes: search its site for the ones
        // still missing.
        call.runtime().jobs.request_backfill(Some(&[id]));
    }
    ok(&json!({"message": result.message, "canceled": result.canceled}))
}

pub(super) fn pause(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        shows::pause(ctx, &mut show, PausedBy::User)
    })?;
    ok(&json!({"status": "paused"}))
}

pub(super) fn resume(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        shows::resume(ctx, &mut show)
    })?;
    poke(call, "grab");
    ok(&json!({"status": "resumed"}))
}

pub(super) fn rediscover(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = show_id(call, &mut problems);
    let pairs = call.query();
    let release_lock = Reader::new(&mut problems, "query").query_flag(&pairs, "release_lock");
    checked(problems)?;
    let locked = call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        if show.feed_state == FeedState::Locked && !release_lock {
            return Ok(Some(show.title("english")));
        }
        shows::release_feed(ctx, &mut show)?;
        Ok(None)
    })?;
    if let Some(title) = locked {
        return Err(ApiError::FeedLocked(format!(
            "'{title}' is locked to its feed"
        )));
    }
    poke(call, "grab");
    ok(&json!({"status": "released"}))
}

pub(super) fn search_releases(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    // How many aired episodes a search would look for; unknown when there is no feed or search
    // term to tell.
    let missing = call.runtime().engine.read(|ctx| {
        show_or_404(ctx, id)?;
        if backfill::search_unavailable(ctx, id, false)? {
            return Ok(None);
        }
        Ok(Some(backfill::missing_episodes(ctx, id, false)?.len()))
    })?;
    // Nothing missing means every aired episode is downloaded or in the feeds, so there was
    // nothing to search for.
    let found = if missing == Some(0) {
        0
    } else {
        call.runtime().jobs.search_older(id)?
    };
    ok(&json!({"found": found, "missing": missing}))
}

pub(super) fn download(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = show_id(call, &mut problems);
    let body = call.object(&mut problems);
    let (mut title, mut expect, mut fuzzy) = (None, None, false);
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        title = reader.required_string(body, "title");
        expect = reader.optional_integer(body, "expect_episode");
        fuzzy = reader.flag(body, "fuzzy");
        reader.forbid_others(body, &["title", "expect_episode", "fuzzy"]);
    }
    checked(problems)?;
    let title = title.unwrap_or_default();
    let message = call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        manual::manual_grab(ctx, &mut show, &title, fuzzy, expect)
    })?;
    poke(call, "settle");
    ok(&json!({"message": message}))
}

/// An action on one episode of a show that answers with a message.
fn on_episode(
    call: &mut Call<'_, '_>,
    act: impl FnOnce(
        &mut kisetsu_services::context::Context<'_>,
        &mut kisetsu_db::schema::Show,
        &mut kisetsu_db::schema::Episode,
    ) -> Result<String>
    + Send
    + 'static,
) -> std::result::Result<String, ApiError> {
    let mut problems = Problems::default();
    let (show, episode) = (
        show_id(call, &mut problems),
        episode_id(call, &mut problems),
    );
    checked(problems)?;
    Ok(call.runtime().engine.call(move |ctx| {
        let mut show = show_or_404(ctx, show)?;
        let mut episode = episode_or_404(ctx, &show, episode)?;
        act(ctx, &mut show, &mut episode)
    })?)
}

pub(super) fn restore(call: &mut Call<'_, '_>) -> ApiResult {
    let message = on_episode(call, |ctx, show, episode| {
        manual::restore_episode(ctx, show, episode)
    })?;
    poke(call, "settle");
    ok(&json!({"message": message}))
}

pub(super) fn try_another(call: &mut Call<'_, '_>) -> ApiResult {
    let message = on_episode(call, |ctx, show, episode| {
        manual::try_another(ctx, show, episode)
    })?;
    poke(call, "settle");
    ok(&json!({"message": message}))
}

pub(super) fn want(call: &mut Call<'_, '_>) -> ApiResult {
    let show = show_only(call)?;
    let message = on_episode(call, |ctx, show, episode| {
        shows::want_episode(ctx, show, episode)
    })?;
    // A stored release is grabbed right away; only one no feed has left needs the search.
    poke(call, "grab");
    call.runtime().jobs.request_backfill(Some(&[show]));
    ok(&json!({"message": message}))
}

fn folder_json(plan: &library::FolderPlan) -> Value {
    json!({
        "path": plan.path,
        "exists": plan.exists,
        "remove": plan.remove,
        "reason": plan.kept.map(|reason| reason.to_string()),
        "leftovers": plan.leftovers,
        "leftover_count": plan.leftover_count,
    })
}

pub(super) fn delete_preview(call: &mut Call<'_, '_>) -> ApiResult {
    let id = show_only(call)?;
    let preview = call.runtime().engine.call(move |ctx| {
        let show = show_or_404(ctx, id)?;
        let torrents = library::show_torrent_preview(ctx, &show)?;
        let hashes: Vec<String> = torrents
            .iter()
            .map(|torrent| torrent.hash.clone())
            .collect();
        let folder = library::folder_plan(ctx, &show, &hashes)?;
        Ok(json!({
            "torrents": torrents.iter().map(|torrent| json!({
                "hash": torrent.hash,
                "name": torrent.name,
                "size": torrent.size,
                "tracked": torrent.tracked,
            })).collect::<Vec<_>>(),
            "folder": folder_json(&folder),
        }))
    })?;
    ok(&preview)
}

/// What deleting a show did, before its folder is dealt with.
struct Deleted {
    name: String,
    removed: usize,
    hashes: Vec<String>,
    plan: Option<library::FolderPlan>,
    base_dir: String,
}

pub(super) fn delete(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = show_id(call, &mut problems);
    let pairs = call.query();
    let mut reader = Reader::new(&mut problems, "query");
    let (delete_torrents, delete_files) = (
        reader.query_flag(&pairs, "delete_torrents"),
        reader.query_flag(&pairs, "delete_files"),
    );
    checked(problems)?;
    let runtime = call.runtime();
    let deleted = runtime.engine.call(move |ctx| {
        let mut show = show_or_404(ctx, id)?;
        let name = show.title(&ctx.settings.title_language);
        let hashes = if delete_torrents {
            library::find_show_torrents(ctx, &show)?
        } else {
            Vec::new()
        };
        // The folder goes only with its files: keeping the files keeps the folder.
        let plan = if delete_torrents && delete_files {
            Some(library::folder_plan(ctx, &show, &hashes)?)
        } else {
            None
        };
        let options = Delete {
            dismiss: true,
            delete_torrents,
            delete_files,
            hashes: Some(&hashes),
        };
        let removed = shows::delete_show(ctx, &mut show, "Show deleted by user.", options)?;
        Ok(Deleted {
            name,
            removed,
            hashes: hashes.clone(),
            plan,
            base_dir: ctx.settings.base_dir.clone(),
        })
    })?;
    let folder = match &deleted.plan {
        Some(plan) if plan.exists => Some(settle_folder(call, &deleted, plan)),
        _ => None,
    };
    ok(&json!({"deleted_torrents": deleted.removed, "folder": folder}))
}

/// Deals with the show's folder once its torrents are deleted, and says what became of it.
fn settle_folder(call: &Call<'_, '_>, deleted: &Deleted, plan: &library::FolderPlan) -> Value {
    let runtime = call.runtime();
    let mut error = plan.kept.map(|reason| reason.to_string());
    if plan.remove {
        // qBittorrent removes the files in the background; the folder goes once it lists none of
        // the torrents.
        error = Some(
            "qBittorrent is still removing the torrents, so the folder was kept. Delete it yourself once they are gone."
                .to_owned(),
        );
        for _ in 0..GONE_CHECKS {
            let hashes = deleted.hashes.clone();
            match runtime
                .engine
                .call(move |ctx| library::torrents_gone(ctx, &hashes))
            {
                Ok(true) => {
                    error = None;
                    break;
                }
                Ok(false) => {}
                Err(kisetsu_engine::Error::Service(Error::Qbit(failure))) => {
                    error = Some(gone_unconfirmed(&failure));
                    break;
                }
                Err(_) => break,
            }
            thread::sleep(GONE_PAUSE);
        }
        if error.is_none() && runtime.sandbox {
            info!("[sandbox] Would delete the folder {}.", plan.path);
            error = Some("This is a sandbox, so the folder was kept.".to_owned());
        } else if error.is_none() {
            error = library::remove_folder(&plan.path, &deleted.base_dir, 5, thread::sleep)
                .err()
                .map(|failure| failure.to_string());
        }
    }
    let removed = plan.remove && error.is_none();
    if removed {
        info!("Deleted '{}' and its folder {}.", deleted.name, plan.path);
    } else {
        warn!(
            "Deleted '{}' but kept its folder {}: {}",
            deleted.name,
            plan.path,
            error.as_deref().unwrap_or_default()
        );
    }
    json!({"path": plan.path, "removed": removed, "reason": error})
}

fn gone_unconfirmed(failure: &QbitError) -> String {
    format!(
        "qBittorrent could not confirm the torrents are gone ({failure}), so the folder was kept."
    )
}
