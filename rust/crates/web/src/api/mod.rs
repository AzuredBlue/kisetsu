//! The routes, in the order they are tried.

mod add;
mod backup;
mod feeds;
mod settings;
mod setup;
mod shows;
mod status;
mod tray;
mod update;

use std::time::Duration;

use kisetsu_db::schema::{Episode, Feed, Show};
use kisetsu_services::context::Context;
use kisetsu_services::{Error, Result};
use serde_json::{Value, json};

use crate::app::{Call, Route, route};
use crate::check::Problems;
use crate::error::{ApiError, ApiResult};
use crate::http::Response;

pub(crate) const ROUTES: &[Route] = &[
    route("GET", "/api/add/list", add::user_list),
    route("POST", "/api/add/search", add::search),
    route("POST", "/api/add/confirm", add::confirm),
    route("GET", "/api/shows", shows::list),
    route("POST", "/api/shows/pause-all", shows::pause_all),
    route("POST", "/api/shows/resume-all", shows::resume_all),
    route(
        "POST",
        "/api/shows/auto-discover-all",
        shows::auto_discover_all,
    ),
    route("GET", "/api/shows/{show_id}", shows::get),
    route(
        "GET",
        "/api/shows/{show_id}/feed-matches",
        shows::feed_matches,
    ),
    route("POST", "/api/shows/{show_id}/edit", shows::edit),
    route("POST", "/api/shows/{show_id}/pause", shows::pause),
    route("POST", "/api/shows/{show_id}/resume", shows::resume),
    route("POST", "/api/shows/{show_id}/rediscover", shows::rediscover),
    route(
        "POST",
        "/api/shows/{show_id}/search-releases",
        shows::search_releases,
    ),
    route("POST", "/api/shows/{show_id}/download", shows::download),
    route(
        "POST",
        "/api/shows/{show_id}/episodes/{episode_id}/restore",
        shows::restore,
    ),
    route(
        "POST",
        "/api/shows/{show_id}/episodes/{episode_id}/try-another",
        shows::try_another,
    ),
    route(
        "POST",
        "/api/shows/{show_id}/episodes/{episode_id}/want",
        shows::want,
    ),
    route(
        "GET",
        "/api/shows/{show_id}/delete-preview",
        shows::delete_preview,
    ),
    route("DELETE", "/api/shows/{show_id}", shows::delete),
    route("GET", "/api/feeds", feeds::list),
    route("GET", "/api/feeds/items", feeds::items),
    route("POST", "/api/feeds", feeds::add),
    route("POST", "/api/feeds/reorder", feeds::reorder),
    route("POST", "/api/feeds/sync", feeds::sync_from_qbit),
    route("PATCH", "/api/feeds/{feed_id}", feeds::edit),
    route("DELETE", "/api/feeds/{feed_id}", feeds::delete),
    route("GET", "/api/settings", settings::get),
    route("POST", "/api/settings", settings::update),
    route(
        "GET",
        "/api/settings/title-language-preview",
        settings::title_language_preview,
    ),
    route(
        "GET",
        "/api/settings/watched-preview",
        settings::watched_preview,
    ),
    route("POST", "/api/settings/test-qbit", settings::test_qbit),
    route("POST", "/api/settings/sync-anilist", settings::sync_anilist),
    route("POST", "/api/settings/clear-all", settings::clear_all),
    route("GET", "/api/setup/status", setup::status),
    route("POST", "/api/setup/save", setup::save),
    route("POST", "/api/setup/check-anilist", setup::check_anilist),
    route("POST", "/api/setup/complete", setup::complete),
    route("GET", "/api/setup/review", setup::review),
    route("POST", "/api/setup/review", setup::decide),
    route("GET", "/api/status", status::get),
    route("GET", "/api/tray", tray::get),
    route("PUT", "/api/tray", tray::set),
    route("GET", "/api/update", update::get),
    route("POST", "/api/update", update::install),
    route("GET", "/api/attention", status::attention),
    route("POST", "/api/attention/dismiss", status::dismiss_attention),
    route("GET", "/api/logs", status::logs),
    route("GET", "/api/requests", status::requests),
    route("DELETE", "/api/requests", status::reset_requests),
    route("GET", "/api/history", status::history),
    route("DELETE", "/api/history", status::clear_history),
    route("POST", "/api/refresh", status::refresh),
    route("GET", "/api/stream", status::stream),
    route("GET", "/api/backup/export", backup::export),
    route("POST", "/api/backup/inspect", backup::inspect),
    route("POST", "/api/backup/restore", backup::restore),
];

/// The answer to a request that went well.
fn ok(body: &Value) -> ApiResult {
    Ok(Response::json(200, body))
}

fn success() -> ApiResult {
    ok(&json!({"status": "success"}))
}

/// Ends a request whose parts did not fit the route.
fn checked(problems: Problems) -> std::result::Result<(), ApiError> {
    if problems.is_empty() {
        Ok(())
    } else {
        Err(ApiError::Unprocessable(problems))
    }
}

/// Asks the job to run now.
fn poke(call: &Call<'_, '_>, job: &str) {
    call.runtime().jobs.poke(job, Duration::ZERO);
}

fn show_or_404(ctx: &Context<'_>, show_id: i64) -> Result<Show> {
    Show::find(ctx.conn, "WHERE id = ?", [show_id])?
        .ok_or_else(|| Error::NotFound("Show not found.".to_owned()))
}

fn feed_or_404(ctx: &Context<'_>, feed_id: i64) -> Result<Feed> {
    Feed::find(ctx.conn, "WHERE id = ?", [feed_id])?
        .ok_or_else(|| Error::NotFound("Feed not found.".to_owned()))
}

/// The episode of the show, which must be one of its own.
fn episode_or_404(ctx: &Context<'_>, show: &Show, episode_id: i64) -> Result<Episode> {
    Episode::find(
        ctx.conn,
        "WHERE id = ? AND show_id = ?",
        (episode_id, show.id),
    )?
    .ok_or_else(|| Error::NotFound("Episode not found.".to_owned()))
}
