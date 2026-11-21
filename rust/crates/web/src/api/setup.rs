//! The first-run wizard.

use std::collections::HashSet;

use kisetsu_clients::anilist::AniListError;
use kisetsu_db::schema::DismissedShow;
use kisetsu_engine::jobs::LIST_REUSE_SECONDS;
use kisetsu_services::context::{SETUP_KEY, set_state, setup_completed};
use serde_json::json;

use super::{checked, ok, poke, success};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::http::Response;
use crate::settings::{SETUP_FIELDS, SettingsPatch, apply_settings};

const FINISHED: &str = "Setup is already finished.";

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
    let done = call
        .runtime()
        .engine
        .read(|ctx| setup_completed(ctx.conn))?;
    ok(&json!({"setup_completed": done, "sandbox": call.runtime().sandbox}))
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
