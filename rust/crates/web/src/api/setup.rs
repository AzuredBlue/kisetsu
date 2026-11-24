//! The first-run wizard.

use std::collections::HashSet;

use kisetsu_clients::anilist::AniListError;
use kisetsu_db::schema::{DismissedShow, Show};
use kisetsu_engine::jobs::LIST_REUSE_SECONDS;
use kisetsu_services::backfill::grabbable;
use kisetsu_services::context::{
    SETUP_KEY, SETUP_REVIEW_KEY, set_state, setup_completed, setup_reviewing,
};
use kisetsu_services::{ingest, shows};
use serde_json::{Value, json};

use super::{checked, ok, poke, success};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::http::Response;
use crate::settings::{SETUP_FIELDS, SettingsPatch, apply_settings};

const FINISHED: &str = "Setup is already finished.";
const DECIDED: &str = "The first downloads were already decided.";

/// The AniList user name a route was given, stripped; it may not be blank.
fn user_name(call: &mut Call<'_, '_>) -> std::result::Result<String, ApiError> {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let name = body.map(|body| {
        let mut reader = Reader::new(&mut problems, "body");
        let name = reader.text_or_absent(&body, "anilist_username");
        reader.forbid_others(&body, &["anilist_username"]);
        name
    });
    checked(problems)?;
    let name = name.flatten().unwrap_or_default().trim().to_owned();
    if name.is_empty() {
        return Err(ApiError::Invalid("Enter your AniList username.".to_owned()));
    }
    Ok(name)
}

fn upstream(error: &AniListError) -> ApiError {
    ApiError::Upstream(format!("Couldn't reach AniList: {error}"))
}

fn finished() -> ApiResult {
    Ok(Response::json(409, &json!({"detail": FINISHED})))
}

pub(super) fn status(call: &mut Call<'_, '_>) -> ApiResult {
    let (done, reviewing) = call
        .runtime()
        .engine
        .read(|ctx| Ok((setup_completed(ctx.conn)?, setup_reviewing(ctx.conn)?)))?;
    ok(&json!({
        "setup_completed": done,
        "reviewing": reviewing,
        "sandbox": call.runtime().sandbox,
    }))
}

pub(super) fn save(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let patch =
        body.map(|body| SettingsPatch::read(&mut Reader::new(&mut problems, "body"), &body));
    checked(problems)?;
    let patch = patch.unwrap_or_default();
    // Only open until setup is finished; afterwards the Settings page owns this.
    let saved = call.runtime().engine.call(move |ctx| {
        if setup_completed(ctx.conn)? {
            return Ok(false);
        }
        let outside: Vec<&str> = patch
            .given()
            .into_iter()
            .filter(|field| !SETUP_FIELDS.contains(field))
            .collect();
        if !outside.is_empty() {
            return Err(kisetsu_services::Error::Invalid(format!(
                "Not part of setup: {}.",
                outside.join(", ")
            )));
        }
        apply_settings(ctx, &patch)?;
        Ok(true)
    })?;
    if saved { success() } else { finished() }
}

/// Whether the user exists, and how many shows the first sync would add.
pub(super) fn check_anilist(call: &mut Call<'_, '_>) -> ApiResult {
    let name = user_name(call)?;
    let remote = &call.runtime().remote;
    match remote.user_exists(&name) {
        Ok(true) => {}
        Ok(false) => {
            return Err(ApiError::Invalid(format!(
                "AniList user '{name}' was not found."
            )));
        }
        Err(error) => return Err(upstream(&error)),
    }
    // `user_exists` has just read the list, so this takes it instead of asking AniList again.
    let seasonal = remote
        .seasonal(&name, &HashSet::new(), Some(LIST_REUSE_SECONDS))
        .map_err(|error| upstream(&error))?;
    let dismissed: HashSet<i64> = call.runtime().engine.read(|ctx| {
        Ok(DismissedShow::select(ctx.conn, "", [])?
            .into_iter()
            .map(|row| row.anilist_id)
            .collect())
    })?;
    let shows = seasonal
        .iter()
        .filter(|entry| !dismissed.contains(&entry.anilist_id))
        .count();
    ok(&json!({"shows": shows}))
}

pub(super) fn complete(call: &mut Call<'_, '_>) -> ApiResult {
    let name = user_name(call)?;
    let runtime = call.runtime();
    if runtime.engine.read(|ctx| setup_completed(ctx.conn))? {
        return finished();
    }
    match runtime.remote.user_exists(&name) {
        Ok(true) => {}
        Ok(false) => {
            return Err(ApiError::Invalid(format!(
                "AniList user '{name}' was not found."
            )));
        }
        Err(error) => return Err(upstream(&error)),
    }
    let finished_now = runtime.engine.call(move |ctx| {
        if setup_completed(ctx.conn)? {
            return Ok(false);
        }
        ctx.settings.anilist_username = name;
        ctx.settings.update(ctx.conn)?;
        // Every show comes in paused, so nothing is downloaded before the user has seen what
        // would be.
        shows::pause_all(ctx)?;
        set_state(ctx.conn, SETUP_REVIEW_KEY, "1")?;
        set_state(ctx.conn, SETUP_KEY, "1")?;
        ctx.emit(&["settings", "status"]);
        Ok(true)
    })?;
    if !finished_now {
        return finished();
    }
    poke(call, "startup");
    success()
}

/// Each followed show with the episodes the first grab would download, those with any first.
pub(super) fn review(call: &mut Call<'_, '_>) -> ApiResult {
    let rows = call.runtime().engine.call(|ctx| {
        // Releases the searches stored are matched to the shows before they are counted.
        ingest::ensure_matches_current(ctx)?;
        let language = ctx.settings.title_language.clone();
        let mut rows = Vec::new();
        for show in Show::select(ctx.conn, "WHERE completed = 0 ORDER BY id", [])? {
            let episodes = grabbable(ctx, &show)?;
            rows.push((show.title(&language), show, episodes));
        }
        Ok(rows)
    })?;
    let mut rows = rows;
    rows.sort_by(|a, b| {
        a.2.is_empty()
            .cmp(&b.2.is_empty())
            .then_with(|| a.0.to_lowercase().cmp(&b.0.to_lowercase()))
    });
    let shows: Vec<Value> = rows
        .into_iter()
        .map(|(title, show, episodes)| {
            json!({
                "id": show.id,
                "title": title,
                "cover": show.cover_image,
                "episodes": episodes,
            })
        })
        .collect();
    ok(&json!({"shows": shows}))
}

/// Starts the first downloads (`download`), or leaves every show paused for now.
pub(super) fn decide(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let download = body.and_then(|body| {
        let mut reader = Reader::new(&mut problems, "body");
        let download = reader.required_flag(&body, "download");
        reader.forbid_others(&body, &["download"]);
        download
    });
    checked(problems)?;
    let download = download.unwrap_or(false);
    let decided = call.runtime().engine.call(move |ctx| {
        if !setup_reviewing(ctx.conn)? {
            return Ok(false);
        }
        if download {
            shows::resume_all(ctx)?;
        }
        set_state(ctx.conn, SETUP_REVIEW_KEY, "0")?;
        ctx.emit(&["settings", "status", "shows"]);
        Ok(true)
    })?;
    if !decided {
        return Ok(Response::json(409, &json!({"detail": DECIDED})));
    }
    call.runtime().jobs.clear_setup_progress();
    if download {
        poke(call, "grab");
    }
    success()
}
