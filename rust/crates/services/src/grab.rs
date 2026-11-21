//! Deciding which stored feed item becomes which episode's download, and which episodes are
//! missed.
//!
//! A pass holds one copy of each show and episode it works on and hands it to what it calls, so
//! what a download or a feed lock changes is seen by the rest of the pass.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use jiff::{SignedDuration, Timestamp};
use kisetsu_db::schema::{
    Download, Episode, EpisodeOffset, EventKind, Feed, FeedItem, FeedState, OffsetSource, Show,
};
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::matcher::{
    Matcher, broad_match, build_matcher, decide_dated, narrow_to_feed, newest_raw_episode,
};
use kisetsu_domain::numbering::{LedgerRow, SeasonFacts, inferred_offset, rebase_plan};
use kisetsu_domain::parsing::{
    ParsedRelease, episode_pair, is_batch, names_something_else, parse_release_title,
    release_resolution, resolution_rank, season_conflict, strip_batch,
};
use kisetsu_domain::schedule::{
    RELEASE_HOLDING, ShowFacts, air_horizon, date_mapped_episode, is_dated, latest_aired,
    missed_numbers, premiere_out_of_reach, published_before_air,
};
use tracing::warn;

use crate::backfill;
use crate::context::Context;
use crate::downloads::{self, StartOptions};
use crate::error::{Error, Result};
use crate::events::{self, Subject};
use crate::facts::facts_for;
use crate::feeds::{self, Carrier};
use crate::ingest::{self, followed_shows};
use crate::library;
use crate::watched;

/// An episode older than this is not taken from an item whose publish date says nothing.
const HUNTING_RECENT: SignedDuration = SignedDuration::from_hours(14 * 24);

/// What a grab pass did.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct GrabResult {
    pub started: usize,
    pub missed: usize,
    /// qBittorrent errors, which stop the pass.
    pub errors: Vec<String>,
    /// Shows whose pass broke on something else ("Name: error"); the others were still grabbed
    /// for.
    pub failed: Vec<String>,
    /// By show looked at: whether a feed it may read carries any release of it.
    pub carried: BTreeMap<i64, bool>,
}

/// A stored feed item that is some episode of the show.
#[derive(Debug, Clone, PartialEq)]
pub struct Resolved {
    pub item: FeedItem,
    /// The number the feed gives the episode.
    pub raw: i64,
    /// The episode's number in the show.
    pub number: i64,
    pub version: i64,
    /// The item's torrent holds another episode too (a double episode), so it is added once and
    /// never replaced on its own.
    pub shared: bool,
}

/// What grabbing for one show needs: its episodes, schedule and the downloads they hold.
#[derive(Debug, Clone)]
pub struct ShowState {
    pub episodes: Vec<Episode>,
    index: HashMap<i64, usize>,
    /// By episode id: the download it currently has its release from.
    pub current: HashMap<i64, Option<Download>>,
    /// By episode id: the versions that failed for good.
    pub failed: HashMap<i64, HashSet<i64>>,
    pub target_count: i64,
    pub newest_aired: Option<i64>,
    pub horizon: Timestamp,
    pub air_times: Vec<(i64, Timestamp)>,
    pub first_air: Option<Timestamp>,
}

impl ShowState {
    pub fn episode(&self, number: i64) -> Option<&Episode> {
        self.index.get(&number).map(|at| &self.episodes[*at])
    }

    fn episode_mut(&mut self, number: i64) -> Option<&mut Episode> {
        let at = *self.index.get(&number)?;
        self.episodes.get_mut(at)
    }

    /// The download an episode has its release from.
    pub fn current_of(&self, episode: &Episode) -> Option<&Download> {
        self.current
            .get(&episode.id.unwrap_or_default())
            .and_then(Option::as_ref)
    }

    /// The version the episode holds, or 1 when it holds none.
    pub fn held_version(&self, episode: &Episode) -> i64 {
        match self.current_of(episode) {
            Some(cur)
                if RELEASE_HOLDING.contains(&episode.status)
                    || episode.status == EpisodeStatus::Failed =>
            {
                cur.version
            }
            _ => 1,
        }
    }

    fn failed_versions(&self, episode: &Episode) -> Option<&HashSet<i64>> {
        self.failed.get(&episode.id.unwrap_or_default())
    }
}

pub fn show_state(ctx: &Context<'_>, show: &Show, facts: &ShowFacts) -> Result<ShowState> {
    let episodes = Episode::select(ctx.conn, "WHERE show_id = ? ORDER BY number", [show.id])?;
    let mut current = HashMap::new();
    let mut failed: HashMap<i64, HashSet<i64>> = HashMap::new();
    if !episodes.is_empty() {
        let downloads = Download::select(
            ctx.conn,
            "WHERE episode_id IN (SELECT id FROM episodes WHERE show_id = ?)",
            [show.id],
        )?;
        let by_id: HashMap<i64, &Download> = downloads
            .iter()
            .filter_map(|dl| Some((dl.id?, dl)))
            .collect();
        for episode in &episodes {
            let held = episode
                .current_download_id
                .and_then(|id| by_id.get(&id))
                .map(|dl| (*dl).clone());
            current.insert(episode.id.unwrap_or_default(), held);
        }
        for dl in &downloads {
            if downloads::is_final_failure(dl) {
                failed.entry(dl.episode_id).or_default().insert(dl.version);
            }
        }
    }
    let canonical_max = episodes
        .iter()
        .map(|episode| episode.number)
        .max()
        .unwrap_or(0);
    let target_count = show
        .total_episodes
        .filter(|total| *total != 0)
        .unwrap_or_else(|| {
            show.next_airing_episode
                .unwrap_or(0)
                .max(canonical_max)
                .max(1)
        });
    let horizon = air_horizon(facts, ctx.now, ctx.tolerance_hours());
    let air_times: Vec<(i64, Timestamp)> = episodes
        .iter()
        .filter_map(|episode| Some((episode.number, episode.air_at?)))
        .collect();
    let index = episodes
        .iter()
        .enumerate()
        .map(|(at, episode)| (episode.number, at))
        .collect();
    Ok(ShowState {
        index,
        current,
        failed,
        target_count,
        newest_aired: latest_aired(facts, horizon),
        horizon,
        first_air: air_times.iter().map(|(_, air_at)| *air_at).min(),
        air_times,
        episodes,
    })
}

fn offset_of(ctx: &Context<'_>, show: &Show, feed: &Feed) -> Result<Option<EpisodeOffset>> {
    Ok(EpisodeOffset::find(
        ctx.conn,
        "WHERE show_id = ? AND feed_id = ?",
        (show.id, feed.id),
    )?)
}

/// How to tell the show's releases from other titles on this feed.
pub fn matcher_for(ctx: &Context<'_>, show: &Show, state: &ShowState, feed: &Feed) -> Matcher {
    let base = build_matcher(
        &show.effective_aliases(),
        show.learned_name.as_deref(),
        ctx.settings.min_resolution,
    );
    if show.feed_state != FeedState::Locked || show.feed_id != feed.id {
        return base;
    }
    let held: Vec<&str> = state
        .episodes
        .iter()
        .filter(|episode| RELEASE_HOLDING.contains(&episode.status))
        .filter_map(|episode| state.current_of(episode))
        // A batch names its season its own way ("S01"), which says nothing of how the feed names
        // single episodes.
        .filter(|cur| {
            cur.feed_id.is_none_or(|from| Some(from) == feed.id)
                && !downloads::is_batch_download(cur)
        })
        .map(|cur| cur.title.as_str())
        .collect();
    narrow_to_feed(base, show.learned_name.as_deref(), &held)
}

/// The season a release must name to be grabbed from the show's locked feed, or `None` when no
/// season is required.
pub fn enforced_season(ctx: &Context<'_>, show: &Show, facts: &ShowFacts) -> Result<Option<i64>> {
    if show.feed_state != FeedState::Locked {
        return Ok(None);
    }
    let Some(feed) = feeds::feed_by_id(ctx, show.feed_id)? else {
        return Ok(None);
    };
    let state = show_state(ctx, show, facts)?;
    let matcher = matcher_for(ctx, show, &state, &feed);
    Ok(matcher.season.filter(|_| matcher.season_known))
}

/// The feed's stored items of the show, newest first.
fn items_of(ctx: &Context<'_>, show: &Show, feed: &Feed) -> Result<Vec<FeedItem>> {
    Ok(FeedItem::select(
        ctx.conn,
        "JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
         WHERE feed_item_matches.show_id = ? AND feed_items.feed_id = ? \
         ORDER BY feed_items.published_at DESC, feed_items.id DESC",
        (show.id, feed.id),
    )?)
}

/// What `resolve` is asked for beyond the items.
#[derive(Debug, Default)]
pub struct ResolveOptions<'a> {
    /// Learn the feed's numbering from items its air dates number: this renumbers stored
    /// releases.
    pub learn: bool,
    /// Collects the items turned down, each with why.
    pub rejected: Option<&'a mut Vec<(FeedItem, String)>>,
    /// Collects the items numbered by their air date alone, the only ones a feed's numbering is
    /// learned from.
    pub dated: Option<&'a mut Vec<FeedItem>>,
}

/// Which episode each stored item of a feed is for, and the newest usable version of each.
///
/// The one place that decides whether an item is this show's release of one of its episodes:
/// the grab acts on the result and the missed rule asks the same question without `learn`.
pub fn resolve(
    ctx: &mut Context<'_>,
    show: &Show,
    state: &mut ShowState,
    feed: &Feed,
    mut options: ResolveOptions<'_>,
) -> Result<(Vec<Resolved>, BTreeMap<i64, i64>)> {
    let matcher = matcher_for(ctx, show, state, feed);
    let items = items_of(ctx, show, feed)?;
    let parsed: Vec<ParsedRelease> = items
        .iter()
        .map(|item| parse_release_title(&item.title))
        .collect();
    let newest_raw = newest_raw_episode(&matcher, &parsed);
    let offset = offset_of(ctx, show, feed)?.map(|row| row.offset);
    let mut names = show.effective_aliases();
    names.extend(show.learned_name.clone());
    let tolerance = ctx.tolerance_hours();
    let mut resolved = Vec::new();
    let mut best: BTreeMap<i64, i64> = BTreeMap::new();

    for (item, parsed) in items.iter().zip(&parsed) {
        let mut turn_down = |reason: String| {
            if let Some(rejected) = options.rejected.as_deref_mut() {
                rejected.push((item.clone(), reason));
            }
        };
        let on_schedule = |_raw: i64| {
            item.published_at.is_some_and(|published| {
                date_mapped_episode(&state.air_times, published, state.target_count, tolerance)
                    .is_some()
            })
        };
        let (matched, _) = decide_dated(&matcher, parsed, on_schedule);
        if !matched {
            turn_down(why_not_matched(ctx, &matcher, &item.title));
            continue;
        }
        // A movie, special or side story under the show's name ("… - Coleus no Yume - 01") is not
        // one of its episodes, nor is a release of another show whose name holds this one's.
        if names_something_else(parsed, &names) {
            turn_down(
                "A movie, special, side story or another show whose name holds this one's, not one of the episodes. \
                 If it is this show, add its full name as a custom alias."
                    .to_owned(),
            );
            continue;
        }
        let raw = parsed
            .episode
            .or_else(|| (state.target_count == 1).then_some(1));
        let Some(raw) = raw.filter(|raw| *raw > 0) else {
            turn_down(
                "Its name has no episode number (a recap like \"12.5\", or a special).".to_owned(),
            );
            continue;
        };
        let mapped = SeasonFacts {
            offset,
            latest_aired: state.newest_aired,
            target_count: state.target_count,
            air_times: &state.air_times,
            tolerance_hours: tolerance,
            now: ctx.now,
        }
        .map_episode(raw, item.published_at, newest_raw);
        let Some(number) = mapped.local else {
            turn_down(format!(
                "Its episode {raw} does not fit this season's aired episodes (yet)."
            ));
            continue;
        };
        if mapped.by_date {
            if let Some(dated) = options.dated.as_deref_mut() {
                dated.push(item.clone());
            }
            if options.learn {
                learn_offset(ctx, show, state, feed, raw, number)?;
            }
        }
        let Some(episode) = state.episode(number) else {
            turn_down(format!("This season has no episode {number}."));
            continue;
        };
        if published_before_air(
            item.published_at,
            episode.air_at.or(state.first_air),
            tolerance,
        ) {
            turn_down(
                "Published before this episode aired, so it is an earlier season's release."
                    .to_owned(),
            );
            continue;
        }
        let version = parsed.version.max(1);
        let pair = episode_pair(&item.title);
        let second = pair
            .filter(|pair| pair.0 == raw)
            .and_then(|_| state.episode(number + 1))
            // The second half has not aired by AniList's times: only the first is taken from it.
            .filter(|second| {
                let unaired = state
                    .newest_aired
                    .is_some_and(|newest| second.number > newest)
                    || published_before_air(
                        item.published_at,
                        second.air_at.or(state.first_air),
                        tolerance,
                    );
                !unaired
            });
        let mut entries = vec![Resolved {
            item: item.clone(),
            raw,
            number,
            version,
            shared: pair.is_some(),
        }];
        if let Some(second) = second {
            entries.push(Resolved {
                item: item.clone(),
                raw: raw + 1,
                number: second.number,
                version,
                shared: true,
            });
        }
        for entry in entries {
            let failed_for_good = state
                .episode(entry.number)
                .and_then(|episode| state.failed_versions(episode))
                .is_some_and(|versions| versions.contains(&version));
            if !failed_for_good {
                let top = best.entry(entry.number).or_insert(0);
                *top = (*top).max(version);
            }
            resolved.push(entry);
        }
    }
    Ok((resolved, best))
}

/// Why a release carrying the show's name is still not taken as one of its episodes.
pub fn why_not_matched(ctx: &Context<'_>, matcher: &Matcher, title: &str) -> String {
    if is_batch(title) {
        return "A batch (several episodes in one torrent). Download it by hand, or add the show again from Add show to take it.".to_owned();
    }
    let minimum = ctx.settings.min_resolution;
    if let Some(resolution) = release_resolution(title).filter(|resolution| *resolution < minimum) {
        return format!(
            "{resolution}p is below your preferred quality ({minimum}p, in Settings), and lower ones are never taken."
        );
    }
    if season_conflict(title, &matcher.aliases) {
        return "It names another season.".to_owned();
    }
    if matcher.strict {
        return "Not named the way this feed names the show's releases.".to_owned();
    }
    "Its name does not match this show's names closely enough. A custom alias can fix that."
        .to_owned()
}

/// The feed's recent items that carry the show's name but were never matched to it: batches and
/// lower resolutions.
pub fn unmatched_releases(
    ctx: &Context<'_>,
    show: &Show,
    feed: &Feed,
    seen: &HashSet<i64>,
    limit: usize,
) -> Result<Vec<(FeedItem, String)>> {
    let aliases = show.effective_aliases();
    let relaxed = build_matcher(&aliases, show.learned_name.as_deref(), 0);
    let strict = build_matcher(
        &aliases,
        show.learned_name.as_deref(),
        ctx.settings.min_resolution,
    );
    let rows = FeedItem::select(
        ctx.conn,
        "WHERE feed_id = ? ORDER BY published_at DESC, id DESC LIMIT ?",
        (feed.id, i64::try_from(limit).unwrap_or(i64::MAX)),
    )?;
    let mut found = Vec::new();
    for item in rows {
        if item.id.is_some_and(|id| seen.contains(&id)) {
            continue;
        }
        let name = if is_batch(&item.title) {
            strip_batch(&item.title)
        } else {
            item.title.clone()
        };
        if broad_match(&relaxed, &parse_release_title(&name)) {
            let why = why_not_matched(ctx, &strict, &item.title);
            found.push((item, why));
        }
    }
    Ok(found)
}

fn ledger(state: &ShowState, feed: &Feed) -> Vec<LedgerRow> {
    state
        .episodes
        .iter()
        .map(|episode| {
            let cur = state
                .current_of(episode)
                .filter(|_| RELEASE_HOLDING.contains(&episode.status));
            LedgerRow {
                number: episode.number,
                holds_release: cur.is_some(),
                source_episode: cur.and_then(|cur| cur.source_episode),
                from_this_feed: cur
                    .is_some_and(|cur| cur.feed_id.is_none_or(|from| Some(from) == feed.id)),
                has_torrent: cur.is_some_and(|cur| {
                    cur.info_hash
                        .as_deref()
                        .is_some_and(|hash| !hash.is_empty())
                }),
            }
        })
        .collect()
}

/// Learns a feed's numbering when a release's air date contradicts the stored releases, and
/// renumbers them.
fn learn_offset(
    ctx: &mut Context<'_>,
    show: &Show,
    state: &mut ShowState,
    feed: &Feed,
    raw: i64,
    local: i64,
) -> Result<()> {
    let rows = ledger(state, feed);
    let holder = rows.iter().find(|row| row.number == local);
    let held_sources: Vec<i64> = rows
        .iter()
        .filter(|row| row.holds_release)
        .filter_map(|row| row.source_episode)
        .collect();
    let Some(offset) = inferred_offset(raw, local, holder, &held_sources) else {
        return Ok(());
    };
    let Some(moves) = rebase_plan(&rows, offset, state.newest_aired, state.target_count)
        .filter(|moves| !moves.is_empty())
    else {
        return Ok(());
    };
    let touched: BTreeSet<i64> = moves.iter().flat_map(|(from, to)| [*from, *to]).collect();
    for number in &touched {
        let Some(episode) = state.episode(*number) else {
            continue;
        };
        if downloads::active_download(ctx, episode)?.is_some() {
            return Ok(());
        }
    }
    // What each moving episode holds, read before anything moves.
    let mut carried: HashMap<i64, (Option<i64>, Vec<Download>)> = HashMap::new();
    for (source, _) in &moves {
        let Some(episode) = state.episode(*source) else {
            continue;
        };
        let history = Download::select(ctx.conn, "WHERE episode_id = ? ORDER BY id", [episode.id])?;
        carried.insert(
            *source,
            (state.current_of(episode).and_then(|cur| cur.id), history),
        );
    }
    for number in &touched {
        if let Some(episode) = state.episode_mut(*number) {
            episode.current_download_id = None;
        }
    }
    for (source, target) in &moves {
        let Some((current, history)) = carried.remove(source) else {
            continue;
        };
        let Some(target_id) = state.episode(*target).and_then(|episode| episode.id) else {
            continue;
        };
        for mut dl in history {
            dl.episode_id = target_id;
            dl.update(ctx.conn)?;
        }
        if let Some(episode) = state.episode_mut(*target) {
            episode.current_download_id = current;
        }
    }
    for number in &touched {
        let Some(at) = state.index.get(number).copied() else {
            continue;
        };
        let mut episode = state.episodes[at].clone();
        episode.update(ctx.conn)?;
        downloads::recompute_episode(ctx, &mut episode)?;
        let held = downloads::current(ctx, &episode)?;
        state.current.insert(episode.id.unwrap_or_default(), held);
        state.episodes[at] = episode;
    }
    // An episode whose release moved away is wanted again, unless it was watched.
    let mut moved: Vec<Episode> = touched
        .iter()
        .filter_map(|number| state.episode(*number).cloned())
        .collect();
    watched::skip_among(ctx, show, 0, moved.iter_mut())?;
    for episode in moved {
        if let Some(at) = state.index.get(&episode.number).copied() {
            state.episodes[at] = episode;
        }
    }
    events::record(
        ctx,
        EventKind::Info,
        &format!(
            "'{}' numbers episodes {offset} higher than AniList; renumbered the stored releases.",
            feed.name
        ),
        Subject {
            show_id: show.id,
            feed_id: feed.id,
            ..Subject::default()
        },
    )?;
    Ok(())
}

/// Keeps or confirms a feed's numbering offset once a release was actually accepted.
pub fn record_evidence(
    ctx: &Context<'_>,
    show: &Show,
    state: &ShowState,
    feed: &Feed,
    raw: i64,
    local: i64,
) -> Result<()> {
    let Some(mut row) = offset_of(ctx, show, feed)? else {
        EpisodeOffset {
            id: None,
            show_id: show.id.unwrap_or_default(),
            feed_id: feed.id.unwrap_or_default(),
            offset: raw - local,
            source: OffsetSource::Inferred,
            evidence_count: 1,
            first_evidence_at: Some(ctx.now),
            last_evidence_at: Some(ctx.now),
        }
        .insert(ctx.conn)?;
        return Ok(());
    };
    if row.source != OffsetSource::Inferred {
        return Ok(());
    }
    let consistent = state
        .episodes
        .iter()
        .filter(|episode| RELEASE_HOLDING.contains(&episode.status))
        .filter_map(|episode| Some((episode, state.current_of(episode)?)))
        .filter(|(episode, cur)| {
            cur.feed_id == feed.id
                && cur
                    .source_episode
                    .is_some_and(|source| source - episode.number == row.offset)
        })
        .count();
    row.evidence_count = row.evidence_count.max(
        i64::try_from(consistent)
            .unwrap_or(i64::MAX)
            .saturating_add(1),
    );
    row.last_evidence_at = Some(ctx.now);
    if consistent > 0 {
        row.source = OffsetSource::Confirmed;
    }
    row.update(ctx.conn)?;
    Ok(())
}

/// The feeds a show may read.
pub fn candidate_feeds(show: &Show, all_feeds: &[Feed]) -> Vec<Feed> {
    if show.feed_is_fixed() {
        // A show with a proven or chosen feed reads that one or none; fanning out is what the
        // lock prevents.
        return all_feeds
            .iter()
            .filter(|feed| feed.id == show.feed_id)
            .cloned()
            .collect();
    }
    all_feeds.to_vec()
}

fn wants(episode: &Episode) -> bool {
    matches!(
        episode.status,
        EpisodeStatus::Wanted | EpisodeStatus::Missed
    )
}

/// Starts downloads for the stored feed items that are some followed show's next episodes.
///
/// `held` are shows waiting for their saved files to be matched to a feed; they are not grabbed
/// for until released. `None` asks what is held now.
pub fn grab_pass(
    ctx: &mut Context<'_>,
    only: Option<&HashSet<i64>>,
    held: Option<&HashSet<i64>>,
) -> Result<GrabResult> {
    let mut result = GrabResult::default();
    ingest::ensure_matches_current(ctx)?;
    feeds::unwind_unreleased(ctx)?;
    let all_feeds = feeds::ranked(ctx)?;
    if all_feeds.is_empty() {
        return Ok(result);
    }
    let shows: Vec<Show> = followed_shows(ctx)?
        .into_iter()
        .filter(|show| {
            !show.paused && only.is_none_or(|only| show.id.is_some_and(|id| only.contains(&id)))
        })
        .collect();
    if shows.is_empty() {
        return Ok(result);
    }
    let held = match held {
        Some(held) => held.clone(),
        None => library::held_ids(ctx),
    };
    let facts = facts_for(ctx, &shows)?;
    // Every torrent the pass adds is looked for with one qBittorrent sync at the end, not one
    // sync per add.
    downloads::batched_adds(ctx, |ctx| {
        for (mut show, facts) in shows.into_iter().zip(&facts) {
            let held_back = show.id.is_some_and(|id| held.contains(&id));
            if show.feed_state != FeedState::Locked && held_back {
                continue;
            }
            let name = show.title(&ctx.settings.title_language);
            match grab_show(ctx, &mut show, facts, &all_feeds, &mut result) {
                Ok(()) => ctx.checkpoint()?,
                Err(Error::Qbit(error)) => {
                    result.errors.push(format!("{name}: {error}"));
                    break;
                }
                Err(error) => {
                    // One show's bad data must not stop every other show from being grabbed for.
                    ctx.rollback_to_checkpoint()?;
                    warn!("Grabbing for '{name}' failed: {error}");
                    result.failed.push(format!("{name}: {error}"));
                }
            }
        }
        Ok(())
    })?;
    let mut carried = ctx.caches.carried.clone();
    carried.extend(result.carried.iter().map(|(show, found)| (*show, *found)));
    if carried != ctx.caches.carried {
        // Whether a feed carries a show tells a show the user watched to the end apart from one
        // no feed has.
        ctx.caches.carried = carried;
        ctx.emit(&["shows"]);
    }
    if result.started > 0 || result.missed > 0 {
        ctx.emit(&["episodes", "history"]);
    }
    Ok(result)
}

/// What reading one feed for a show found.
struct FeedRead {
    resolved: Vec<Resolved>,
    best: BTreeMap<i64, i64>,
    /// The items numbered by their air date alone.
    dated: Vec<FeedItem>,
}

fn grab_show(
    ctx: &mut Context<'_>,
    show: &mut Show,
    facts: &ShowFacts,
    all_feeds: &[Feed],
    result: &mut GrabResult,
) -> Result<()> {
    let mut state = show_state(ctx, show, facts)?;
    let airs_later = show.next_airing_episode == Some(1)
        && show.next_airing_at.is_some_and(|at| at > state.horizon);
    if premiere_out_of_reach(facts, ctx.now) || airs_later {
        return Ok(());
    }
    let candidates = candidate_feeds(show, all_feeds);
    // Choosing a feed reads every candidate without learning from it; only the feed taken is
    // read again with learning, and only when it has something to learn from.
    let mut resolved_by_feed: Vec<FeedRead> = Vec::new();
    for feed in &candidates {
        let mut dated = Vec::new();
        let (resolved, best) = resolve(
            ctx,
            show,
            &mut state,
            feed,
            ResolveOptions {
                dated: Some(&mut dated),
                ..ResolveOptions::default()
            },
        )?;
        resolved_by_feed.push(FeedRead {
            resolved,
            best,
            dated,
        });
    }
    result.carried.insert(
        show.id.unwrap_or_default(),
        resolved_by_feed
            .iter()
            .any(|read| !read.resolved.is_empty()),
    );
    let chosen = choose_feed(ctx, show, &state, &candidates, &resolved_by_feed)?;
    let mut grabbed = false;
    if let Some(chosen) = &chosen {
        let at = candidates.iter().position(|feed| feed.id == chosen.id);
        if let Some(at) = at {
            if !resolved_by_feed[at].dated.is_empty() {
                let (resolved, best) = resolve(
                    ctx,
                    show,
                    &mut state,
                    chosen,
                    ResolveOptions {
                        learn: true,
                        ..ResolveOptions::default()
                    },
                )?;
                resolved_by_feed[at].resolved = resolved;
                resolved_by_feed[at].best = best;
            }
            let FeedRead { resolved, best, .. } = &resolved_by_feed[at];
            // Of an episode's releases the preferred quality goes first, then the closest above
            // it; the first taken wins.
            let preferred = ctx.settings.min_resolution;
            let mut ordered: Vec<&Resolved> = resolved.iter().collect();
            ordered.sort_by_key(|entry| resolution_rank(&entry.item.title, preferred));
            // The torrents this pass added that hold a second episode, by item: that episode
            // follows the same torrent.
            let mut added: HashMap<i64, String> = HashMap::new();
            for entry in ordered {
                if take(ctx, show, &mut state, chosen, entry, best, &mut added)? {
                    grabbed = true;
                    result.started += 1;
                }
            }
        }
    }
    let read_ok = candidates
        .iter()
        .any(|feed| feed.last_ok_at.is_some() && feed.failing_since.is_none());
    if read_ok {
        let covered: BTreeSet<i64> = resolved_by_feed
            .iter()
            .flat_map(|read| read.best.keys().copied())
            .collect();
        result.missed += mark_missed(ctx, show, &state, &covered)?;
    }
    if let Some(chosen) = chosen.filter(|_| grabbed) {
        if matches!(show.feed_state, FeedState::None | FeedState::Candidate) {
            feeds::set_assigned(ctx, show, &chosen)?;
        }
    }
    Ok(())
}

/// The feed to grab from now: the locked, pinned or assigned one, else the discovery winner.
fn choose_feed(
    ctx: &mut Context<'_>,
    show: &mut Show,
    state: &ShowState,
    candidates: &[Feed],
    resolved_by_feed: &[FeedRead],
) -> Result<Option<Feed>> {
    if show.feed_is_fixed() {
        return Ok(candidates.first().cloned());
    }
    let mut carriers = Vec::new();
    for (feed, FeedRead { resolved, best, .. }) in candidates.iter().zip(resolved_by_feed) {
        let carried: Vec<&Resolved> = resolved
            .iter()
            .filter(|entry| {
                let Some(episode) = state.episode(entry.number) else {
                    return false;
                };
                // A failed episode is still served by a version newer than the one that failed.
                let serves = wants(episode)
                    || (episode.status == EpisodeStatus::Failed
                        && entry.version > state.held_version(episode));
                serves && entry.version >= best.get(&entry.number).copied().unwrap_or(entry.version)
            })
            .collect();
        if !carried.is_empty() {
            let first = carried
                .iter()
                .filter(|entry| is_dated(entry.item.published_at))
                .filter_map(|entry| entry.item.published_at)
                .min();
            carriers.push(Carrier {
                feed: feed.clone(),
                published_at: first,
            });
        }
    }
    if carriers.is_empty() {
        // An assigned feed is still read: it may teach its numbering even when nothing there is
        // wanted.
        if show.feed_state == FeedState::Assigned {
            return Ok(candidates
                .iter()
                .find(|feed| feed.id == show.feed_id)
                .cloned());
        }
        return Ok(None);
    }
    feeds::assign(ctx, show, &carriers, &HashSet::new())
}

/// Starts the download of one resolved item when the episode may take it; whether it did.
fn take(
    ctx: &mut Context<'_>,
    show: &mut Show,
    state: &mut ShowState,
    feed: &Feed,
    entry: &Resolved,
    best: &BTreeMap<i64, i64>,
    added: &mut HashMap<i64, String>,
) -> Result<bool> {
    let Some(episode) = state.episode(entry.number).cloned() else {
        return Ok(false);
    };
    let (version, item) = (entry.version, &entry.item);
    // The user chose to go without this one: only taking that back, or grabbing it by hand, gets it.
    if episode.status == EpisodeStatus::Skipped {
        return Ok(false);
    }
    if version < best.get(&entry.number).copied().unwrap_or(version) {
        return Ok(false);
    }
    let held = state.held_version(&episode);
    let upgrade = version > held;
    let cur = state.current_of(&episode);
    if upgrade
        && cur.is_some_and(downloads::is_batch_download)
        && RELEASE_HOLDING.contains(&episode.status)
    {
        // An episode that came in a batch keeps it: replacing it would delete the whole batch with its files.
        return Ok(false);
    }
    if cur.is_some_and(|cur| cur.end_reason == Some(kisetsu_db::schema::EndReason::Watched)) {
        // Watched, and deleted or about to be: a newer version is not wanted either.
        return Ok(false);
    }
    // An episode with no air time is taken like any other (the whole season is eligible);
    // the mapping already refuses numbers past the newest aired one.
    if !upgrade
        && !is_dated(item.published_at)
        && episode
            .air_at
            .is_some_and(|air_at| ctx.now.duration_since(air_at) > HUNTING_RECENT)
    {
        return Ok(false);
    }
    if episode.retry_after.is_some_and(|after| after > ctx.now) {
        return Ok(false);
    }
    if state
        .failed_versions(&episode)
        .is_some_and(|versions| versions.contains(&version))
    {
        return Ok(false);
    }
    if episode.status == EpisodeStatus::Failed && version <= held {
        return Ok(false);
    }
    if RELEASE_HOLDING.contains(&episode.status) && version <= held {
        return Ok(false);
    }
    if downloads::active_download(ctx, &episode)?.is_some() {
        return Ok(false);
    }
    record_evidence(ctx, show, state, feed, entry.raw, entry.number)?;
    let item_id = item.id.unwrap_or_default();
    let Some(at) = state.index.get(&entry.number).copied() else {
        return Ok(false);
    };
    let mut episode = state.episodes[at].clone();
    let dl = match added.get(&item_id) {
        Some(hash) => Some(downloads::follow_download(
            ctx,
            show,
            feed,
            &mut episode,
            item,
            hash,
            Some(entry.raw),
        )?),
        None => {
            let started = downloads::start_download(
                ctx,
                show,
                feed,
                &mut episode,
                item,
                StartOptions {
                    source_episode: Some(entry.raw),
                    ..StartOptions::default()
                },
            )?;
            if let Some(hash) = started
                .as_ref()
                .filter(|_| entry.shared)
                .and_then(|dl| dl.info_hash.clone())
            {
                added.insert(item_id, hash);
            }
            started
        }
    };
    let Some(dl) = dl else {
        // Nothing changed, but start_download may have refreshed the episode's copy.
        state.episodes[at] = episode;
        return Ok(false);
    };
    if episode.current_download_id == dl.id {
        state
            .current
            .insert(episode.id.unwrap_or_default(), Some(dl));
    }
    state.episodes[at] = episode;
    Ok(true)
}

fn mark_missed(
    ctx: &mut Context<'_>,
    show: &Show,
    state: &ShowState,
    covered: &BTreeSet<i64>,
) -> Result<usize> {
    let facts = facts_for(ctx, std::slice::from_ref(show))?;
    let Some(refreshed) = facts.first() else {
        return Ok(0);
    };
    if !refreshed
        .episodes
        .iter()
        .any(|episode| episode.status == EpisodeStatus::Wanted)
    {
        return Ok(0);
    }
    let covered: Vec<i64> = covered.iter().copied().collect();
    let numbers: Vec<i64> = missed_numbers(
        &refreshed.episodes,
        ctx.now,
        state.horizon,
        state.newest_aired,
        &covered,
    )
    .into_iter()
    .filter(|number| {
        state
            .episode(*number)
            .is_some_and(|episode| episode.status == EpisodeStatus::Wanted)
    })
    .collect();
    // Not missed while the search of the feeds' sites for it is still to run: it may well find it.
    if numbers.is_empty() || backfill::search_pending(ctx, show.id.unwrap_or_default(), &numbers)? {
        return Ok(0);
    }
    for number in &numbers {
        let Some(mut episode) = Episode::find(
            ctx.conn,
            "WHERE show_id = ? AND number = ?",
            (show.id, number),
        )?
        else {
            continue;
        };
        episode.status = EpisodeStatus::Missed;
        episode.update(ctx.conn)?;
        events::record(
            ctx,
            EventKind::Missed,
            "Not in the feed",
            Subject {
                show_id: show.id,
                episode: Some(*number),
                ..Subject::default()
            },
        )?;
    }
    ctx.emit(&[&format!("show:{}", show.id.unwrap_or_default())]);
    Ok(numbers.len())
}

/// The show's episodes that the stored feed items can already supply; read only.
pub fn covered_numbers(ctx: &mut Context<'_>, show: &Show) -> Result<BTreeSet<i64>> {
    let facts = facts_for(ctx, std::slice::from_ref(show))?;
    let Some(facts) = facts.first() else {
        return Ok(BTreeSet::new());
    };
    let mut state = show_state(ctx, show, facts)?;
    let mut covered = BTreeSet::new();
    for feed in candidate_feeds(show, &feeds::ranked(ctx)?) {
        let (_, best) = resolve(ctx, show, &mut state, &feed, ResolveOptions::default())?;
        covered.extend(best.into_keys());
    }
    Ok(covered)
}

#[cfg(test)]
mod tests {
    use jiff::ToSpan;
    use kisetsu_db::schema::{DownloadState, FeedItemMatch, ScheduleState};
    use kisetsu_db::store;

    use super::*;
    use crate::context::Caches;
    use crate::fakes::{CountedTags, FakeClock, FakeQbit, START};

    fn show() -> Show {
        Show {
            id: None,
            anilist_id: 1,
            title_romaji: None,
            title_english: Some("Sousou no Frieren".to_owned()),
            aliases_json: r#"["Sousou no Frieren"]"#.to_owned(),
            custom_aliases_json: "[]".to_owned(),
            cover_image: None,
            cover_color: None,
            banner_image: None,
            accent_hues: None,
            accent_src: None,
            cover_edge: None,
            cover_edge_src: None,
            season_name: None,
            season_year: None,
            total_episodes: Some(1),
            anilist_status: None,
            list_status: None,
            next_airing_episode: None,
            next_airing_at: None,
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
            paused: false,
            paused_by: None,
            completed: false,
            last_backfill_at: None,
            watched: None,
            watched_applied: None,
        }
    }

    // A grab changes the show (the feed lock, its series tag) and the episode inside the
    // download it starts; what the pass writes afterwards must keep those changes.
    #[test]
    fn a_grab_keeps_the_lock_the_tag_and_the_episode_it_set_while_the_pass_goes_on() {
        let conn = store::open_in_memory().unwrap();
        let now = START;
        let mut feed = Feed {
            id: None,
            name: "Top".to_owned(),
            url: "https://top/rss".to_owned(),
            priority: 1,
            qbit_path: None,
            last_fetch_at: Some(now),
            last_ok_at: Some(now),
            failing_since: None,
            last_error: None,
        };
        feed.id = Some(feed.insert(&conn).unwrap());
        let mut show = show();
        show.id = Some(show.insert(&conn).unwrap());
        let mut episode = Episode {
            id: None,
            show_id: show.id.unwrap(),
            number: 1,
            air_at: Some(now.checked_sub(24.hours()).unwrap()),
            schedule_state: ScheduleState::Aired,
            status: EpisodeStatus::Wanted,
            current_download_id: None,
            retry_after: None,
            last_error: None,
            attempts: 0,
        };
        episode.id = Some(episode.insert(&conn).unwrap());
        let mut item = FeedItem {
            id: None,
            feed_id: feed.id.unwrap(),
            guid: "g1".to_owned(),
            info_hash: None,
            title: "[Grp] Sousou no Frieren - 01 (1080p).mkv".to_owned(),
            torrent_url: Some("https://example.test/a.torrent".to_owned()),
            published_at: Some(now.checked_sub(12.hours()).unwrap()),
            first_seen_at: now,
            episode_raw: Some(1),
            version: 1,
            release_group: Some("Grp".to_owned()),
            parsed_title: Some("Sousou no Frieren".to_owned()),
        };
        item.id = Some(item.insert(&conn).unwrap());
        FeedItemMatch {
            item_id: item.id.unwrap(),
            show_id: show.id.unwrap(),
        }
        .insert(&conn)
        .unwrap();

        let mut caches = Caches::default();
        let mut qbit = FakeQbit::new(FakeClock::new(now));
        let mut tags = CountedTags::default();
        let mut ctx = Context::new(&conn, now, &mut caches)
            .unwrap()
            .with_qbit(&mut qbit)
            .with_download_tags(&mut tags);
        let result = grab_pass(&mut ctx, None, None).unwrap();
        assert_eq!((result.started, result.errors.len()), (1, 0));

        let show = Show::find(&conn, "WHERE id = ?", [show.id])
            .unwrap()
            .unwrap();
        assert_eq!(
            (show.feed_state, show.feed_id),
            (FeedState::Locked, feed.id)
        );
        assert_eq!(show.qbit_tag.as_deref(), Some("Sousou no Frieren"));
        let episode = Episode::find(&conn, "WHERE id = ?", [episode.id])
            .unwrap()
            .unwrap();
        assert_eq!(episode.status, EpisodeStatus::Downloading);
        let dl = Download::find(&conn, "WHERE id = ?", [episode.current_download_id])
            .unwrap()
            .unwrap();
        assert_eq!(dl.state, DownloadState::Downloading);
    }
}
