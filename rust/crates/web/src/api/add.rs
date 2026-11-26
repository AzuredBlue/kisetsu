//! Adding a show by hand: the user's AniList list, the feed search for one entry, and the
//! confirmed add.

use kisetsu_clients::anilist::ListEntry;
use kisetsu_services::addshow::{
    self, BatchOffer, EpisodeOffer, FeedOffer, LIST_STATUSES, ListRow, Preview,
};
use kisetsu_services::context::iso_utc;
use serde_json::{Value, json};

use super::{checked, ok};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::{ApiError, ApiResult};
use crate::views::iso;

/// The entry of the user's list, if it is one that can be added.
fn entry_of(call: &Call<'_, '_>, anilist_id: i64) -> std::result::Result<ListEntry, ApiError> {
    call.runtime()
        .remote
        .user_list()
        .into_iter()
        .find(|entry| entry.anilist_id == anilist_id)
        .filter(|entry| {
            entry
                .list_status
                .as_ref()
                .is_some_and(|status| LIST_STATUSES.contains(&status.as_str()))
        })
        .ok_or_else(|| {
            ApiError::NotFound(
                "That entry is not on your AniList list. Press Sync Now and try again.".to_owned(),
            )
        })
}

fn row_json(row: &ListRow) -> Value {
    json!({
        "anilist_id": row.anilist_id,
        "title": row.title,
        "aliases": row.aliases,
        "cover": row.cover,
        "status": row.status,
        "list_status": row.list_status,
        "list_label": row.list_label,
        "total_episodes": row.total_episodes,
        "season": row.season,
        "followed": row.followed,
        "show_id": row.show_id,
    })
}

fn batch_json(batch: &BatchOffer) -> Value {
    json!({
        "item_key": batch.item_key,
        "title": batch.title,
        "numbers": batch.numbers,
        "ranged": batch.ranged,
        "published_at": iso(batch.published_at),
        "on_disk": batch.on_disk,
        "newer": batch.newer,
        "to_download": batch.to_download,
        "files_known": batch.files_known,
    })
}

fn episode_json(episode: &EpisodeOffer) -> Value {
    json!({
        "number": episode.number,
        "item_key": episode.item_key,
        "title": episode.title,
        "version": episode.version,
        "published_at": iso(episode.published_at),
        "on_disk": episode.on_disk,
    })
}

fn feed_json(feed: &FeedOffer) -> Value {
    json!({
        "feed_id": feed.feed_id,
        "feed_name": feed.feed_name,
        "batches": feed.batches.iter().map(batch_json).collect::<Vec<_>>(),
        "episodes": feed.episodes.iter().map(episode_json).collect::<Vec<_>>(),
    })
}

fn preview_json(preview: &Preview, stopped: Option<&str>) -> Value {
    json!({
        "anilist_id": preview.anilist_id,
        "nothing_offered": preview.nothing_offered,
        "title": preview.title,
        "status": preview.status,
        "total_episodes": preview.total_episodes,
        "episode_count": preview.episode_count,
        "feeds": preview.feeds.iter().map(feed_json).collect::<Vec<_>>(),
        "unsearchable": preview.unsearchable,
        "folder": preview.folder.as_ref().map(|folder| json!({"path": folder.path, "files": folder.files})),
        "preselect": preview.preselect,
        "note": preview.note,
        "watched": preview.watched,
        "stopped": stopped,
    })
}

pub(super) fn user_list(call: &mut Call<'_, '_>) -> ApiResult {
    let remote = &call.runtime().remote;
    let username = call
        .runtime()
        .engine
        .read(|ctx| Ok(ctx.settings.anilist_username.trim().to_owned()))?;
    if username.is_empty() {
        return Err(ApiError::Invalid(
            "Set your AniList user name in Settings first.".to_owned(),
        ));
    }
    let Some(fetched_at) = remote.user_list_at() else {
        return Err(ApiError::Invalid(
            "Your AniList list has not been read yet. Press Sync Now and try again.".to_owned(),
        ));
    };
    let entries = remote.user_list();
    let rows = call
        .runtime()
        .engine
        .read(|ctx| addshow::list_view(ctx, &entries))?;
    ok(&json!({
        "entries": rows.iter().map(row_json).collect::<Vec<_>>(),
        "fetched_at": iso_utc(fetched_at),
    }))
}

/// The entry a body names.
fn anilist_id(call: &mut Call<'_, '_>) -> std::result::Result<i64, ApiError> {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let mut id = None;
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        id = reader.required_integer(body, "anilist_id");
        reader.forbid_others(body, &["anilist_id"]);
    }
    checked(problems)?;
    Ok(id.unwrap_or_default())
}

pub(super) fn search(call: &mut Call<'_, '_>) -> ApiResult {
    let id = anilist_id(call)?;
    let entry = entry_of(call, id)?;
    let found = call.runtime().jobs.search_new_show(&entry)?;
    ok(&preview_json(&found.preview, found.stopped.as_deref()))
}

pub(super) fn confirm(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let (mut id, mut feed_id, mut item_keys) = (None, None, None);
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        id = reader.required_integer(body, "anilist_id");
        // 0 leaves the feed to discovery: for a show nothing was released for yet.
        feed_id = reader.required_integer(body, "feed_id");
        item_keys = reader.required_strings(body, "item_keys");
        reader.forbid_others(body, &["anilist_id", "feed_id", "item_keys"]);
    }
    checked(problems)?;
    let id = id.unwrap_or_default();
    let (feed_id, item_keys) = (feed_id.unwrap_or_default(), item_keys.unwrap_or_default());
    let entry = entry_of(call, id)?;
    let runtime = call.runtime();
    let found = runtime.jobs.found_for(id);
    let torrent_files = runtime.jobs.torrent_files_for(id);
    let result = runtime.engine.call(move |ctx| {
        addshow::confirm(ctx, &entry, feed_id, &item_keys, &found, &torrent_files)
    })?;
    // Dropped only once the add went through, so a refused confirm can be tried again.
    runtime.jobs.forget_found(id);
    let jobs = std::sync::Arc::clone(&runtime.jobs);
    call.app.spawn_background(move || jobs.after_manual_add());
    ok(&json!({
        "show_id": result.show_id,
        "started": result.started,
        "skipped": result.skipped,
        "errors": result.errors,
    }))
}
