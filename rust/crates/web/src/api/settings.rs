//! The Settings page.

use kisetsu_db::schema::Show;
use kisetsu_domain::patterns::compress_home_path;
use kisetsu_services::shows::{self, Delete};
use kisetsu_services::watched;
use serde_json::{Value, json};

use super::{checked, ok, poke};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::settings::{
    LANGUAGES, SettingsPatch, apply_settings, check_password_travels_with_host, move_plan,
    settings_view,
};

/// What a change to these settings should set going again.
const SYNC_FIELDS: [&str; 2] = ["anilist_username", "anilist_sync_minutes"];
const GRAB_FIELDS: [&str; 6] = [
    "early_air_tolerance_hours",
    "title_language",
    "base_dir",
    "default_category",
    "default_seed_ratio",
    "min_resolution",
];
const HUNT_FIELDS: [&str; 2] = ["hunt_interval_minutes", "early_air_tolerance_hours"];
const FEED_FIELDS: [&str; 1] = ["feed_refresh_minutes"];
const CONNECTION_FIELDS: [&str; 3] = ["qbit_host", "qbit_username", "qbit_password"];
const WATCHED_FIELDS: [&str; 1] = ["delete_watched"];

pub(super) fn get(call: &mut Call<'_, '_>) -> ApiResult {
    ok(&call
        .runtime()
        .engine
        .read(|ctx| Ok(settings_view(&ctx.settings)))?)
}

pub(super) fn update(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let patch =
        body.map(|body| SettingsPatch::read(&mut Reader::new(&mut problems, "body"), &body));
    checked(problems)?;
    let patch = patch.unwrap_or_default();
    let changed = patch.given();
    let logs = {
        let patch = patch.clone();
        call.runtime()
            .engine
            .call(move |ctx| apply_settings(ctx, &patch))?
    };
    let touched = |fields: &[&str]| fields.iter().any(|field| changed.contains(field));
    for (fields, job) in [
        (&CONNECTION_FIELDS[..], "settle"),
        (&SYNC_FIELDS[..], "sync_anilist"),
        (&GRAB_FIELDS[..], "grab"),
        (&HUNT_FIELDS[..], "hunt"),
        (&FEED_FIELDS[..], "refresh_feeds"),
        // Settle deletes what was already watched, when the user chose that.
        (&WATCHED_FIELDS[..], "settle"),
    ] {
        if touched(fields) {
            poke(call, job);
        }
    }
    ok(&json!({"message": "Settings saved.", "logs": logs}))
}

pub(super) fn title_language_preview(call: &mut Call<'_, '_>) -> ApiResult {
    let pairs = call.query();
    let mut problems = Problems::default();
    let language = Reader::new(&mut problems, "query").required_query_text(&pairs, "lang");
    checked(problems)?;
    let language = language.unwrap_or_default().trim().to_lowercase();
    if !LANGUAGES.contains(&language.as_str()) {
        return Err(ApiError::Invalid(
            "lang must be english or romaji".to_owned(),
        ));
    }
    let preview = call.runtime().engine.call(move |ctx| {
        if language == ctx.settings.title_language {
            return Ok(json!({"shows": [], "torrents": 0}));
        }
        let plan = move_plan(ctx, &language)?;
        let home = std::env::home_dir().unwrap_or_default();
        let home = home.to_string_lossy();
        let language = ctx.settings.title_language.clone();
        let entries: Vec<Value> = plan
            .iter()
            .filter(|item| !item.hashes.is_empty())
            .map(|item| {
                json!({
                    "name": item.show.title(&language),
                    "old": compress_home_path(&item.old, &home),
                    "new": compress_home_path(&item.new, &home),
                    "torrents": item.hashes.len(),
                })
            })
            .collect();
        let torrents: usize = plan.iter().map(|item| item.hashes.len()).sum();
        Ok(json!({"shows": entries, "torrents": torrents}))
    })?;
    ok(&preview)
}

pub(super) fn watched_preview(call: &mut Call<'_, '_>) -> ApiResult {
    let backlog = call.runtime().engine.call(watched::backlog)?;
    ok(&json!({
        "episodes": backlog.episodes,
        "shows": backlog.shows.iter().map(|show| json!({
            "name": show.name,
            "numbers": show.numbers,
            "size": show.size,
        })).collect::<Vec<_>>(),
        "size": backlog.size,
    }))
}

pub(super) fn test_qbit(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let (mut host, mut username, mut password) = (None, None, None);
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        host = reader.optional_string(body, "qbit_host");
        username = reader.optional_string(body, "qbit_username");
        password = reader.optional_string(body, "qbit_password");
        reader.forbid_others(body, &["qbit_host", "qbit_username", "qbit_password"]);
    }
    checked(problems)?;
    let (host, username, password) = call.runtime().engine.read(move |ctx| {
        let blank = |value: &Option<String>| value.as_deref().unwrap_or_default().trim().to_owned();
        let stored = &ctx.settings;
        let host = Some(blank(&host))
            .filter(|host| !host.is_empty())
            .unwrap_or_else(|| stored.qbit_host.clone());
        let username = Some(blank(&username))
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| stored.qbit_username.clone());
        let mut password = password.unwrap_or_default();
        if password.is_empty() {
            check_password_travels_with_host(ctx, &host, None)?;
            password.clone_from(&ctx.settings.qbit_password);
        }
        Ok((host, username, password))
    })?;
    match (call.runtime().probe)(&host, &username, &password) {
        Ok(versions) => {
            let mut out = json!({"status": "success"});
            if let Some(map) = out.as_object_mut() {
                map.extend(versions.into_iter().map(|(key, value)| (key, json!(value))));
            }
            ok(&out)
        }
        Err(error) => Err(ApiError::Invalid(format!("Connection failed: {error}"))),
    }
}

pub(super) fn sync_anilist(call: &mut Call<'_, '_>) -> ApiResult {
    poke(call, "sync_anilist");
    ok(&json!({"status": "queued"}))
}

pub(super) fn clear_all(call: &mut Call<'_, '_>) -> ApiResult {
    let count = call.runtime().engine.call(|ctx| {
        let everything = Show::select(ctx.conn, "ORDER BY id", [])?;
        for mut show in everything.iter().cloned() {
            shows::delete_show(
                ctx,
                &mut show,
                "All shows cleared by user.",
                Delete::default(),
            )?;
        }
        Ok(everything.len())
    })?;
    ok(&json!({"message": format!("Cleared all {count} shows.")}))
}
