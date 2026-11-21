//! The feeds the shows are read from.

use kisetsu_db::schema::Feed;
use kisetsu_services::{Error, feeds};
use serde_json::{Value, json};

use super::{checked, feed_or_404, ok, poke, success};
use crate::app::Call;
use crate::check::{Problems, Reader};
use crate::error::ApiResult;
use crate::views;

/// The most items a page may ask for.
const ITEMS_LIMIT_MAX: i64 = 200;

const ALREADY_LISTED: &str = "That feed is already in the list.";

/// A feed's name or URL, stripped; `None` when it is blank.
fn cleaned(value: &str) -> Option<String> {
    let value = value.trim();
    (!value.is_empty()).then(|| value.to_owned())
}

/// Reads a name or URL field: it may not be blank, and a URL must be a web address. `required`
/// says whether the field may be left out.
fn text_field(reader: &mut Reader<'_>, body: &Value, name: &str, required: bool) -> Option<String> {
    let value = if required {
        reader.required_string(body, name)?
    } else {
        reader.optional_string(body, name)?
    };
    let Some(value) = cleaned(&value) else {
        reader.refuse_value(
            name,
            &raw_input(body, name),
            "The feed name and URL can't be empty.",
        );
        return None;
    };
    if name == "url" && !is_web_address(&value) {
        reader.refuse_value(
            name,
            &raw_input(body, name),
            "The feed URL must start with http:// or https://",
        );
        return None;
    }
    Some(value)
}

fn raw_input(body: &Value, name: &str) -> String {
    body.get(name)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn is_web_address(url: &str) -> bool {
    let lowered = url.to_lowercase();
    lowered.starts_with("http://") || lowered.starts_with("https://")
}

fn url_taken(
    ctx: &kisetsu_services::context::Context<'_>,
    url: &str,
) -> kisetsu_services::Result<bool> {
    Ok(Feed::find(ctx.conn, "WHERE url = ?", [url])?.is_some())
}

pub(super) fn list(call: &mut Call<'_, '_>) -> ApiResult {
    ok(&call.runtime().engine.read(|ctx| views::feeds(ctx))?)
}

pub(super) fn items(call: &mut Call<'_, '_>) -> ApiResult {
    let pairs = call.query();
    let mut problems = Problems::default();
    let mut reader = Reader::new(&mut problems, "query");
    let feed_id = reader.query_integer(&pairs, "feed_id");
    let matched = reader.query_flag(&pairs, "matched");
    let limit = reader.query_integer(&pairs, "limit").unwrap_or(100);
    let offset = reader.query_integer(&pairs, "offset").unwrap_or(0);
    checked(problems)?;
    let (limit, offset) = (limit.clamp(1, ITEMS_LIMIT_MAX), offset.max(0));
    ok(&call
        .runtime()
        .engine
        .read(|ctx| views::feed_items(ctx, feed_id, matched, limit, offset))?)
}

pub(super) fn add(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let (mut name, mut url) = (None, None);
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        name = text_field(&mut reader, body, "name", true);
        url = text_field(&mut reader, body, "url", true);
        reader.forbid_others(body, &["name", "url"]);
    }
    checked(problems)?;
    let (name, url) = (name.unwrap_or_default(), url.unwrap_or_default());
    let id = call.runtime().engine.call(move |ctx| {
        if url_taken(ctx, &url)? {
            return Err(Error::Invalid(ALREADY_LISTED.to_owned()));
        }
        Ok(feeds::add_feed(ctx, &name, &url, None)?.id)
    })?;
    poke(call, "refresh_feeds");
    ok(&json!({"id": id}))
}

pub(super) fn reorder(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let body = call.object(&mut problems);
    let ids = body.and_then(|body| {
        let mut reader = Reader::new(&mut problems, "body");
        let ids = reader.required_integers(&body, "ids");
        reader.forbid_others(&body, &["ids"]);
        ids
    });
    checked(problems)?;
    let ids = ids.unwrap_or_default();
    call.runtime()
        .engine
        .call(move |ctx| feeds::reorder_feeds(ctx, &ids))?;
    poke(call, "grab");
    success()
}

pub(super) fn sync_from_qbit(call: &mut Call<'_, '_>) -> ApiResult {
    let added = call
        .runtime()
        .engine
        .call(|ctx| Ok(feeds::import_from_qbit(ctx)?.len()))?;
    if added > 0 {
        poke(call, "refresh_feeds");
    }
    ok(&json!({"added": added}))
}

pub(super) fn edit(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = Reader::new(&mut problems, "path")
        .path_integer("feed_id", &call.params[0])
        .unwrap_or_default();
    let body = call.object(&mut problems);
    let (mut name, mut url) = (None, None);
    if let Some(body) = &body {
        let mut reader = Reader::new(&mut problems, "body");
        name = text_field(&mut reader, body, "name", false);
        url = text_field(&mut reader, body, "url", false);
        reader.forbid_others(body, &["name", "url"]);
    }
    checked(problems)?;
    let changed_url = url.clone();
    call.runtime().engine.call(move |ctx| {
        let mut feed = feed_or_404(ctx, id)?;
        if let Some(url) = &url {
            if *url != feed.url && url_taken(ctx, url)? {
                return Err(Error::Invalid(ALREADY_LISTED.to_owned()));
            }
        }
        feeds::edit_feed(ctx, &mut feed, name.as_deref(), url.as_deref())
    })?;
    if changed_url.is_some() {
        poke(call, "refresh_feeds");
    }
    success()
}

pub(super) fn delete(call: &mut Call<'_, '_>) -> ApiResult {
    let mut problems = Problems::default();
    let id = Reader::new(&mut problems, "path")
        .path_integer("feed_id", &call.params[0])
        .unwrap_or_default();
    checked(problems)?;
    let released = call.runtime().engine.call(move |ctx| {
        let feed = feed_or_404(ctx, id)?;
        feeds::forget_feed(ctx, &feed)
    })?;
    poke(call, "grab");
    ok(&json!({"released_shows": released}))
}
