//! A qBittorrent tag per show, named after it, so its torrents can be filtered there and told
//! apart here.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use kisetsu_clients::qbittorrent::{Qbit, QbitResult, Torrent};
use kisetsu_db::schema::Show;
use kisetsu_db::storage::{decode_list, encode_list};
use tracing::debug;

use crate::context::{Context, get_state, set_state};
use crate::error::{Error, Result};
use crate::library;

/// Tags of removed shows whose torrents were kept; each is deleted from qBittorrent once no
/// torrent carries it.
const RETIRED_KEY: &str = "retired_series_tags";

/// The name as a tag: qBittorrent splits tags on commas, so they go, and spaces are collapsed.
fn clean(name: &str) -> String {
    name.replace(',', " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// The tag each show should carry, by show id. Shows whose names come out the same get their
/// AniList id added.
pub fn wanted_tags(ctx: &Context<'_>) -> Result<BTreeMap<i64, String>> {
    let shows = Show::select(ctx.conn, "ORDER BY id", [])?;
    let language = &ctx.settings.title_language;
    let names: Vec<String> = shows
        .iter()
        .map(|show| {
            let name = clean(&show.title(language));
            if name.is_empty() {
                format!("AniList {}", show.anilist_id)
            } else {
                name
            }
        })
        .collect();
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for name in &names {
        *counts.entry(name).or_default() += 1;
    }
    Ok(shows
        .iter()
        .zip(&names)
        .filter_map(|(show, name)| {
            let tag = if counts[name.as_str()] > 1 {
                format!("{name} #{}", show.anilist_id)
            } else {
                name.clone()
            };
            show.id.map(|id| (id, tag))
        })
        .collect())
}

/// The tag for a torrent being added for this show; the first one is remembered on the show.
pub fn series_tag(ctx: &Context<'_>, show: &mut Show) -> Result<String> {
    let tag = wanted_tags(ctx)?
        .remove(&show.id.unwrap_or_default())
        .unwrap_or_default();
    if show.qbit_tag.as_deref().is_none_or(str::is_empty) {
        show.qbit_tag = Some(tag.clone());
        show.update(ctx.conn)?;
    }
    Ok(tag)
}

/// Which show each series tag names. A show not tagged yet is known by the tag it would get.
pub fn owners(ctx: &Context<'_>) -> Result<HashMap<String, i64>> {
    let wanted = wanted_tags(ctx)?;
    let mut found: HashMap<String, BTreeSet<i64>> = HashMap::new();
    for show in Show::select(ctx.conn, "ORDER BY id", [])? {
        let Some(id) = show.id else { continue };
        let tag = match show.qbit_tag.filter(|tag| !tag.is_empty()) {
            Some(tag) => tag,
            None => wanted.get(&id).cloned().unwrap_or_default(),
        };
        found.entry(tag).or_default().insert(id);
    }
    Ok(found
        .into_iter()
        .filter_map(|(tag, ids)| single(ids).map(|id| (tag, id)))
        .collect())
}

/// The only member, when there is exactly one.
fn single(ids: impl IntoIterator<Item = i64>) -> Option<i64> {
    let mut ids = ids.into_iter();
    let first = ids.next()?;
    ids.next().is_none().then_some(first)
}

/// The show the torrent's tags name, if they name exactly one.
pub fn owner_id(torrent: &Torrent, owned: &HashMap<String, i64>) -> Option<i64> {
    let ids: BTreeSet<i64> = torrent
        .tags
        .iter()
        .filter_map(|tag| owned.get(tag))
        .copied()
        .collect();
    single(ids)
}

pub fn retired(ctx: &Context<'_>) -> Result<Vec<String>> {
    Ok(decode_list(&get_state(ctx.conn, RETIRED_KEY, "[]")?))
}

fn store_retired(ctx: &Context<'_>, tags: &[String]) -> Result<()> {
    set_state(ctx.conn, RETIRED_KEY, &encode_list(tags))
}

/// Remembers the tag of a removed show whose torrents stay in qBittorrent.
pub fn retire(ctx: &Context<'_>, tag: Option<&str>) -> Result<()> {
    let Some(tag) = tag.filter(|tag| !tag.is_empty()) else {
        return Ok(());
    };
    let mut tags = retired(ctx)?;
    tags.push(tag.to_owned());
    store_retired(ctx, &tags)
}

/// Deletes the retired tags no torrent carries any more; returns how many.
///
/// A tag a followed show owns again (it was added back) is no longer retired and stays.
pub fn sweep_retired(ctx: &mut Context<'_>) -> Result<usize> {
    let pending = retired(ctx)?;
    if pending.is_empty() || ctx.qbit.is_none() {
        return Ok(0);
    }
    let live: HashSet<String> = owners(ctx)?.into_keys().collect();
    let mut deleted = 0;
    match delete_unused(ctx.qbit()?, &pending, &live, &mut deleted) {
        Ok(keep) => {
            store_retired(ctx, &keep)?;
            Ok(deleted)
        }
        Err(error) => {
            debug!("Could not clear retired series tags: {error}");
            Ok(deleted)
        }
    }
}

/// Deletes the pending tags that are neither live nor carried by a torrent; returns the carried
/// ones, which stay retired. `deleted` counts what went even when a later call fails.
fn delete_unused(
    qbit: &mut dyn Qbit,
    pending: &[String],
    live: &HashSet<String>,
    deleted: &mut usize,
) -> QbitResult<Vec<String>> {
    qbit.sync(false)?;
    let carried: HashSet<String> = qbit
        .torrents()
        .values()
        .flat_map(|torrent| torrent.tags.iter().cloned())
        .collect();
    let mut keep = Vec::new();
    for tag in pending {
        if live.contains(tag) {
            continue;
        }
        if carried.contains(tag) {
            keep.push(tag.clone());
            continue;
        }
        qbit.delete_tags(std::slice::from_ref(tag))?;
        *deleted += 1;
    }
    Ok(keep)
}

/// Gives every torrent Kisetsu tracks its show's tag, and moves the tag when the show's name
/// changed.
///
/// Returns how many torrents were tagged or retagged. A qBittorrent failure stops the pass;
/// what was not stored on a show is tried again next time.
pub fn sync_tags(ctx: &mut Context<'_>) -> Result<usize> {
    if ctx.qbit.is_none() {
        return Ok(0);
    }
    let mut changed = 0;
    match retag(ctx, &mut changed) {
        Err(Error::Qbit(error)) => debug!("Could not tag torrents by show: {error}"),
        other => other?,
    }
    Ok(changed)
}

fn retag(ctx: &mut Context<'_>, changed: &mut usize) -> Result<()> {
    if !ctx.qbit()?.synced() {
        ctx.qbit()?.sync(false)?;
    }
    let view: BTreeMap<String, BTreeSet<String>> = ctx
        .qbit()?
        .torrents()
        .iter()
        .map(|(hash, torrent)| (hash.clone(), torrent.tags.clone()))
        .collect();
    let wanted = wanted_tags(ctx)?;
    let in_use: HashSet<&String> = wanted.values().collect();
    for mut show in Show::select(ctx.conn, "ORDER BY id", [])? {
        let Some(tag) = show.id.and_then(|id| wanted.get(&id)) else {
            continue;
        };
        let old = show.qbit_tag.clone().filter(|old| !old.is_empty());
        let tracked: Vec<String> = library::show_torrent_hashes(ctx, &show)?
            .into_iter()
            .filter(|hash| view.contains_key(hash))
            .collect();
        if let Some(old) = old.as_ref().filter(|old| *old != tag) {
            let carrying: Vec<String> = tracked
                .iter()
                .filter(|hash| view[*hash].contains(old))
                .cloned()
                .collect();
            if !carrying.is_empty() {
                ctx.qbit()?
                    .remove_tags(&carrying, std::slice::from_ref(old))?;
            }
            let elsewhere = view
                .iter()
                .any(|(hash, tags)| !carrying.contains(hash) && tags.contains(old));
            if !in_use.contains(old) && !elsewhere {
                ctx.qbit()?.delete_tags(std::slice::from_ref(old))?;
            }
        }
        let missing: Vec<String> = tracked
            .iter()
            .filter(|hash| !view[*hash].contains(tag))
            .cloned()
            .collect();
        if !missing.is_empty() {
            ctx.qbit()?.add_tags(&missing, std::slice::from_ref(tag))?;
            *changed += missing.len();
        }
        if show.qbit_tag.as_ref() != Some(tag) {
            show.qbit_tag = Some(tag.clone());
            show.update(ctx.conn)?;
        }
    }
    Ok(())
}
