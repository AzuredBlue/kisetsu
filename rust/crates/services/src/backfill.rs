//! Searching the feeds' sites for past episodes that scrolled out of the RSS ("Search older").

use std::collections::{BTreeSet, HashMap, HashSet};
use std::hash::{DefaultHasher, Hash, Hasher};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use kisetsu_clients::sites::{self, Grouping, Site};
use kisetsu_db::rusqlite::Params;
use kisetsu_db::schema::{Episode, Feed, Show};
use kisetsu_db::storage::format_timestamp;
use kisetsu_domain::enums::EpisodeStatus;
use kisetsu_domain::matcher::{Matcher, broad_match, build_matcher, decide};
use kisetsu_domain::numbering::SeasonFacts;
use kisetsu_domain::parsing::parse_release_title;
use kisetsu_domain::schedule::{
    AIR_DATE_TOLERANCE, PUBLISHED_BEFORE_AIR_SLACK, WEEK, plausible_for_show,
};
use tracing::{debug, info, warn};

use crate::context::{Context, SearchRecord};
use crate::downloads;
use crate::error::Result;
use crate::facts::facts_for;
use crate::feeds;
use crate::grab;
use crate::ingest;

/// The same search is not repeated for the same missing episodes within this time,
const REPEAT: SignedDuration = SignedDuration::from_hours(6);
/// which doubles, up to this, for each search in a row that found nothing (a show no feed
/// carries).
const REPEAT_MAX: SignedDuration = SignedDuration::from_hours(24);

/// A show with episodes to search for, and what the search is made of.
#[derive(Debug, Clone, PartialEq)]
pub struct Due {
    pub show_id: i64,
    pub name: String,
    pub terms: Vec<String>,
    pub feeds: Vec<Feed>,
    pub missing: Vec<i64>,
    /// What was asked: the terms, the feeds and the episodes.
    pub signature: u64,
    pub first_air: Option<Timestamp>,
    /// What maps a feed's episode numbers onto the show's, as the grab does.
    pub air_times: Vec<(i64, Timestamp)>,
    pub target_count: Option<i64>,
    pub latest_aired: Option<i64>,
    pub tolerance_hours: i64,
    /// The name SubsPlease's schedule gives the show, searched for first on SubsPlease.
    pub subsplease_name: Option<String>,
}

/// Which shows `due` looks at, and how hard.
#[derive(Debug, Clone, Copy, Default)]
pub struct DueOptions {
    /// Only this show, which also covers a paused one, and with `unmanaged` a finished one.
    pub show_id: Option<i64>,
    /// Ignore that the same search ran lately.
    pub force: bool,
    /// Also count finished episodes Kisetsu holds no torrent for.
    pub unmanaged: bool,
    /// A search the user asked for: feeds that seem not to carry the show are asked too.
    pub everywhere: bool,
}

fn hash_of(terms: &[String], feeds: &[Feed], missing: &[i64]) -> u64 {
    let mut hasher = DefaultHasher::new();
    terms.hash(&mut hasher);
    feeds
        .iter()
        .map(|feed| feed.id)
        .collect::<Vec<_>>()
        .hash(&mut hasher);
    missing.hash(&mut hasher);
    hasher.finish()
}

/// The feeds a show's search may ask.
fn searchable_feeds(ctx: &Context<'_>, show: &Show) -> Result<Vec<Feed>> {
    let mut all_feeds = feeds::ranked(ctx)?;
    if show.feed_is_fixed() {
        // A locked show reads one feed; searching others would only fill the store with releases
        // it may not use.
        all_feeds.retain(|feed| feed.id == show.feed_id);
        all_feeds.truncate(1);
    }
    all_feeds.retain(|feed| sites::site_of(&feed.url).is_some());
    Ok(all_feeds)
}

/// The feeds that do not carry the show, so a search of them would find nothing.
///
/// A feed that ever listed the show, or on which a search found it, carries it. Otherwise:
///
/// - A feed lists its newest releases. When its last read reached back past one of the show's
///   aired episodes, far enough that a release of it would be listed even if posted early, and
///   was read late enough that it would have been posted, the feed lacks the show.
/// - A SubsPlease feed lacks an airing show SubsPlease's own schedule leaves out
///   (`note_schedule`).
fn lacking(ctx: &Context<'_>, show: &Show, show_feeds: &[Feed]) -> Result<HashSet<i64>> {
    let windows: HashMap<i64, (Timestamp, Timestamp)> = show_feeds
        .iter()
        .filter_map(|feed| {
            let id = feed.id?;
            Some((id, *ctx.caches.read_windows.get(&id)?))
        })
        .collect();
    let off_schedule = show
        .id
        .is_some_and(|id| ctx.caches.off_schedule.contains(&id));
    if windows.is_empty() && !off_schedule {
        return Ok(HashSet::new());
    }
    let carrying: HashSet<i64> = ctx
        .conn
        .prepare_cached(
            "SELECT DISTINCT feed_items.feed_id FROM feed_items \
             JOIN feed_item_matches ON feed_item_matches.item_id = feed_items.id \
             WHERE feed_item_matches.show_id = ?",
        )?
        .query_map([show.id], |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    let mut lacking: HashSet<i64> = show_feeds
        .iter()
        .filter(|feed| {
            off_schedule
                && sites::site_of(&feed.url) == Some(Site::SubsPlease)
                && feed.id.is_some_and(|id| !carrying.contains(&id))
        })
        .filter_map(|feed| feed.id)
        .collect();
    let aired: Vec<Timestamp> = if windows.is_empty() {
        Vec::new()
    } else {
        Episode::select(
            ctx.conn,
            "WHERE show_id = ? AND air_at IS NOT NULL AND air_at <= ?",
            (show.id, format_timestamp(ctx.now)),
        )?
        .into_iter()
        .filter_map(|episode| episode.air_at)
        .collect()
    };
    lacking.extend(
        windows
            .iter()
            .filter(|(feed_id, _)| !carrying.contains(feed_id))
            .filter(|(_, (oldest, read_at))| {
                aired.iter().any(|air| {
                    oldest
                        .saturating_add(PUBLISHED_BEFORE_AIR_SLACK)
                        .is_ok_and(|from| from <= *air)
                        && air
                            .saturating_add(AIR_DATE_TOLERANCE)
                            .is_ok_and(|to| to <= *read_at)
                })
            })
            .map(|(feed_id, _)| *feed_id),
    );
    Ok(lacking)
}

fn subsplease_feeds(ctx: &Context<'_>) -> Result<Vec<i64>> {
    Ok(Feed::select(ctx.conn, "ORDER BY id", [])?
        .into_iter()
        .filter(|feed| sites::site_of(&feed.url) == Some(Site::SubsPlease))
        .filter_map(|feed| feed.id)
        .collect())
}

/// The show ids a query selects.
fn show_ids(ctx: &Context<'_>, sql: &str, params: impl Params) -> Result<HashSet<i64>> {
    Ok(ctx
        .conn
        .prepare_cached(sql)?
        .query_map(params, |row| row.get(0))?
        .collect::<std::result::Result<_, _>>()?)
}

/// Takes SubsPlease's schedule in: which airing shows it leaves out, so its feeds are not
/// searched for them.
///
/// Only a show airing week by week now (an episode in the last week and one still to come) is
/// judged, as that is all the schedule lists. It is matched as a release named after the
/// schedule's title would be. The schedule is not assumed complete: when it leaves out a show
/// airing now that a SubsPlease feed carries, it is not used at all.
pub fn note_schedule(ctx: &mut Context<'_>, titles: Option<&[String]>) -> Result<()> {
    ctx.caches.off_schedule.clear();
    ctx.caches.schedule_names.clear();
    let feeds_on = subsplease_feeds(ctx)?;
    let Some(titles) = titles.filter(|titles| !titles.is_empty()) else {
        return Ok(());
    };
    if feeds_on.is_empty() {
        return Ok(());
    }
    let week_ago = ctx.now.saturating_sub(WEEK).unwrap_or(ctx.now);
    let mut airing_ids = show_ids(
        ctx,
        "SELECT show_id FROM episodes WHERE air_at > ? AND air_at <= ?",
        (format_timestamp(week_ago), format_timestamp(ctx.now)),
    )?;
    let upcoming = show_ids(
        ctx,
        "SELECT show_id FROM episodes WHERE air_at > ?",
        [format_timestamp(ctx.now)],
    )?;
    airing_ids.retain(|id| upcoming.contains(id));
    let set = ingest::matchers(ctx)?;
    for (show_id, matcher) in &set.matchers {
        let named = titles.iter().find(|title| {
            let release = parse_release_title(&format!("[SubsPlease] {title} - 01 (1080p).mkv"));
            broad_match(matcher, &release)
        });
        if let Some(named) = named {
            ctx.caches.schedule_names.insert(*show_id, named.clone());
        }
    }
    let off: HashSet<i64> = set
        .matchers
        .iter()
        .map(|(show_id, _)| *show_id)
        .filter(|id| airing_ids.contains(id) && !ctx.caches.schedule_names.contains_key(id))
        .collect();
    let feed_list = feeds_on
        .iter()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ");
    let carried = show_ids(
        ctx,
        &format!(
            "SELECT feed_item_matches.show_id FROM feed_item_matches \
             JOIN feed_items ON feed_items.id = feed_item_matches.item_id \
             WHERE feed_items.feed_id IN ({feed_list})"
        ),
        [],
    )?;
    let both: BTreeSet<i64> = off.intersection(&carried).copied().collect();
    if !both.is_empty() {
        let mut names = Vec::new();
        for id in &both {
            if let Some(show) = Show::find(ctx.conn, "WHERE id = ?", [id])? {
                names.push(show.title(&ctx.settings.title_language));
            }
        }
        names.sort();
        warn!(
            "SubsPlease's schedule leaves out {}, which its feed carries, so it is not used to skip searches.",
            names.join(", ")
        );
        return Ok(());
    }
    ctx.caches.off_schedule = off;
    Ok(())
}

/// Shows with an aired episode that is still wanted and that no stored feed item can supply.
///
/// Searching is only for what the feeds have lost, and only on feeds that may carry the show
/// (`lacking`), unless `everywhere` (a search the user asked for) or `unmanaged`, which also
/// counts finished episodes Kisetsu holds no torrent for. `unsearchable`, when given, collects
/// the ids of shows skipped because no feed can be searched or the show has no usable search term.
pub fn due(
    ctx: &mut Context<'_>,
    options: DueOptions,
    mut unsearchable: Option<&mut Vec<i64>>,
) -> Result<Vec<Due>> {
    let shows = match options.show_id {
        Some(id) => Show::select(ctx.conn, "WHERE id = ?", [id])?,
        None => Show::select(
            ctx.conn,
            "WHERE completed = 0 AND paused = 0 ORDER BY id",
            [],
        )?,
    };
    let mut result = Vec::new();
    for show in shows {
        let wanted = outstanding(ctx, &show, options.unmanaged)?;
        if wanted.is_empty() {
            continue;
        }
        let Some(show_id) = show.id else {
            continue;
        };
        // The names that found releases before come first, so the few terms searched include them.
        let custom = show.custom_aliases();
        let aliases = show.aliases();
        let titles = std::iter::once(show.learned_name.as_deref())
            .chain(custom.iter().map(|alias| Some(alias.as_str())))
            .chain([show.title_romaji.as_deref(), show.title_english.as_deref()])
            .chain(aliases.iter().map(|alias| Some(alias.as_str())));
        let terms = sites::search_terms(titles);
        let mut show_feeds = searchable_feeds(ctx, &show)?;
        if terms.is_empty() || show_feeds.is_empty() {
            if let Some(unsearchable) = unsearchable.as_deref_mut() {
                unsearchable.push(show_id);
            }
            continue;
        }
        let covered = grab::covered_numbers(ctx, &show)?;
        let missing: Vec<i64> = wanted
            .into_iter()
            .filter(|number| !covered.contains(number))
            .collect();
        if missing.is_empty() {
            continue;
        }
        if !(options.unmanaged || options.everywhere) {
            // Saved files may come from a feed that has since stopped carrying the show, so they
            // are looked for everywhere.
            let skipped = lacking(ctx, &show, &show_feeds)?;
            show_feeds.retain(|feed| feed.id.is_some_and(|id| !skipped.contains(&id)));
            // A release from one of them later has the show searched again
            // (`ingest::IngestResult::reconsider`).
            if skipped.is_empty() {
                ctx.caches.skipped.remove(&show_id);
            } else {
                ctx.caches.skipped.insert(show_id, skipped);
            }
            if show_feeds.is_empty() {
                continue;
            }
        }
        let signature = hash_of(&terms, &show_feeds, &missing);
        let last = ctx.caches.searched.get(&show_id);
        if !options.force {
            if let Some(last) = last {
                if last.signature == signature
                    && ctx.now.duration_since(last.at) < repeat(last.fruitless)
                {
                    continue;
                }
            } else if show
                .last_backfill_at
                .is_some_and(|at| ctx.now.duration_since(at) < REPEAT)
            {
                // Searched before a restart, which forgot what was searched for: the stored time
                // stands in for it.
                continue;
            }
        }
        let facts = facts_for(ctx, std::slice::from_ref(&show))?;
        let Some(facts) = facts.first() else {
            continue;
        };
        let state = grab::show_state(ctx, &show, facts)?;
        result.push(Due {
            show_id,
            name: show.title(&ctx.settings.title_language),
            terms,
            feeds: show_feeds,
            missing,
            signature,
            first_air: state.first_air,
            air_times: state.air_times,
            target_count: Some(state.target_count),
            latest_aired: state.newest_aired,
            tolerance_hours: ctx.tolerance_hours(),
            subsplease_name: ctx.caches.schedule_names.get(&show_id).cloned(),
        });
    }
    Ok(result)
}

/// How long the same search waits: longer after each one in a row that found nothing.
fn repeat(fruitless: u32) -> SignedDuration {
    let doublings = fruitless.saturating_sub(1).min(16);
    REPEAT
        .checked_mul(1_i32 << doublings)
        .map_or(REPEAT_MAX, |wait| wait.min(REPEAT_MAX))
}

fn outstanding(ctx: &Context<'_>, show: &Show, unmanaged: bool) -> Result<Vec<i64>> {
    let rows = Episode::select(
        ctx.conn,
        "WHERE show_id = ? AND air_at IS NOT NULL AND air_at <= ? ORDER BY number",
        (show.id, format_timestamp(ctx.now)),
    )?;
    let mut numbers = Vec::new();
    for episode in rows {
        match episode.status {
            EpisodeStatus::Wanted | EpisodeStatus::Missed => numbers.push(episode.number),
            EpisodeStatus::Completed if unmanaged => {
                let cur = downloads::current(ctx, &episode)?;
                if cur.is_none_or(|cur| cur.info_hash.is_none()) {
                    numbers.push(episode.number);
                }
            }
            _ => {}
        }
    }
    Ok(numbers)
}

/// Whether the show's aired episodes were searched for, or no search has anything left to look
/// for.
///
/// A show without air dates has not been searched, however empty the search looks.
pub fn search_completed(ctx: &mut Context<'_>, show_id: i64) -> Result<bool> {
    let dated = ctx
        .conn
        .prepare_cached("SELECT 1 FROM episodes WHERE show_id = ? AND air_at IS NOT NULL")?
        .exists([show_id])?;
    if !dated {
        return Ok(false);
    }
    if ctx.caches.searched.contains_key(&show_id) {
        return Ok(true);
    }
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        ..DueOptions::default()
    };
    Ok(due(ctx, options, None)?.is_empty())
}

/// Whether a search of the feeds' sites for these episodes is still to run, so they may yet turn
/// up.
pub fn search_pending(ctx: &mut Context<'_>, show_id: i64, numbers: &[i64]) -> Result<bool> {
    if let Some(last) = ctx.caches.searched.get(&show_id) {
        if numbers.iter().all(|number| last.episodes.contains(number)) {
            return Ok(false);
        }
    }
    let options = DueOptions {
        show_id: Some(show_id),
        ..DueOptions::default()
    };
    Ok(!due(ctx, options, None)?.is_empty())
}

/// The episodes of one show that a search the user asks for would look for (empty: nothing is
/// missing).
pub fn missing_episodes(ctx: &mut Context<'_>, show_id: i64, unmanaged: bool) -> Result<Vec<i64>> {
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        unmanaged,
        everywhere: true,
    };
    Ok(due(ctx, options, None)?
        .into_iter()
        .next()
        .map(|item| item.missing)
        .unwrap_or_default())
}

/// Whether the show has episodes to look for but no feed or search term to look with.
pub fn search_unavailable(ctx: &mut Context<'_>, show_id: i64, unmanaged: bool) -> Result<bool> {
    let mut skipped = Vec::new();
    let options = DueOptions {
        show_id: Some(show_id),
        force: true,
        unmanaged,
        everywhere: false,
    };
    due(ctx, options, Some(&mut skipped))?;
    Ok(skipped.contains(&show_id))
}

/// A search shared by several shows is sized to fill at most this share of a page, as the
/// releases it will list are only estimated (one per aired episode), and a page that comes back
/// full has left the oldest of them out.
const SHARED_PAGE_SHARE: f64 = 0.6;

impl Due {
    /// The show's episodes that one feed's releases (raw number, published) are for.
    ///
    /// A feed counting on from an earlier season ("49" for episode 1) is mapped, so its releases
    /// count as found. Without the show's schedule the numbers are taken as they are.
    pub fn numbers(&self, releases: &[(i64, Option<Timestamp>)], now: Timestamp) -> BTreeSet<i64> {
        let Some(target_count) = self.target_count else {
            return releases.iter().map(|(raw, _)| *raw).collect();
        };
        let newest_raw = releases.iter().map(|(raw, _)| *raw).max();
        let facts = SeasonFacts {
            offset: None,
            latest_aired: self.latest_aired,
            target_count,
            air_times: &self.air_times,
            tolerance_hours: self.tolerance_hours,
            now,
        };
        releases
            .iter()
            .filter_map(|(raw, published_at)| {
                facts.map_episode(*raw, *published_at, newest_raw).local
            })
            .collect()
    }

    fn covers_everything(&self, covered: &BTreeSet<i64>) -> bool {
        self.missing.iter().all(|number| covered.contains(number))
    }
}

/// What a search returned, by feed id, and whether it stopped early.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Searched {
    pub articles: Vec<(i64, Vec<FeedArticle>)>,
    /// Why the site cut the search short.
    pub stopped: Option<String>,
}

impl Searched {
    fn add(&mut self, feed_id: i64, articles: &[FeedArticle]) {
        match self.articles.iter_mut().find(|(id, _)| *id == feed_id) {
            Some((_, held)) => held.extend_from_slice(articles),
            None => self.articles.push((feed_id, articles.to_vec())),
        }
    }
}

/// Asks a site's search for a feed's releases of a term.
pub trait Searcher {
    /// The releases, or `None` when this kind of feed cannot be searched.
    fn search(
        &self,
        feed_url: &str,
        term: &str,
        now: Timestamp,
    ) -> std::result::Result<Option<Vec<FeedArticle>>, FeedFetchError>;
}

/// How to tell the show's releases from other titles.
pub fn matcher_for(show: &Show, min_resolution: i64) -> Matcher {
    build_matcher(
        &show.effective_aliases(),
        show.learned_name.as_deref(),
        min_resolution,
    )
}

/// The releases of the show among the articles, as (raw number, published).
fn releases_in(
    articles: &[FeedArticle],
    item: &Due,
    matcher: &Matcher,
    now: Timestamp,
) -> Vec<(i64, Option<Timestamp>)> {
    articles
        .iter()
        .filter(|article| plausible_for_show(article.published_at, item.first_air, now))
        .filter_map(|article| {
            let parsed = parse_release_title(&article.title);
            let (matched, _) = decide(matcher, &parsed, false);
            let raw = parsed.episode.filter(|raw| matched && *raw > 0)?;
            Some((raw, article.published_at))
        })
        .collect()
}

/// One search of a site; a rate-limited site is recorded as the reason to stop.
fn ask(
    searcher: &dyn Searcher,
    found: &mut Searched,
    feed: &Feed,
    term: &str,
    now: Timestamp,
) -> Option<Vec<FeedArticle>> {
    match searcher.search(&feed.url, term, now) {
        Ok(articles) => articles,
        Err(error) => {
            let message = error.to_string();
            if message.contains("rate limiting") {
                found.stopped = Some(message);
            } else {
                warn!("Searching '{}' for '{term}' failed: {message}", feed.name);
            }
            None
        }
    }
}

/// A search for one show.
struct SiteSearch<'a> {
    item: &'a Due,
    matcher: &'a Matcher,
    now: Timestamp,
    searcher: &'a dyn Searcher,
}

impl SiteSearch<'_> {
    /// Searches the feeds for the show, one request after another.
    ///
    /// A feed's remaining terms are skipped once its results already cover every missing
    /// episode; until then the next term may still reach releases the earlier ones did not. With
    /// `across_feeds` what one feed found counts for the next, and once every missing episode is
    /// covered the remaining feeds are not asked at all.
    ///
    /// With `grouping` a nyaa feed is asked for all the show's names in one search, once a check
    /// has shown the site answers that as it answers the names one by one. A page short of full
    /// then holds every match, so the names are not asked for alone; a full page may have left
    /// some out, so they still are.
    fn run(
        &self,
        site_feeds: &[Feed],
        across_feeds: bool,
        mut grouping: Option<&mut Grouping>,
    ) -> Searched {
        let item = self.item;
        let mut found = Searched::default();
        let mut covered: BTreeSet<i64> = BTreeSet::new();
        for feed in site_feeds {
            if !across_feeds {
                covered = BTreeSet::new();
            } else if item.covers_everything(&covered) {
                break;
            }
            let before = covered.clone();
            let mut releases: Vec<(i64, Option<Timestamp>)> = Vec::new();
            let host = sites::host_of(&feed.url);
            let site = sites::site_of(&feed.url);
            // Several names in one search is nyaa's form; even there it is used only once checked.
            let groupable = grouping.is_some() && site == Some(Site::Nyaa) && item.terms.len() > 1;
            let works = grouping
                .as_deref()
                .filter(|_| groupable)
                .and_then(|grouping| grouping.works(&host));
            let grouped = sites::any_of(&item.terms);
            let mut queries: Vec<String> = if groupable && works == Some(true) {
                std::iter::once(grouped.clone())
                    .chain(item.terms.iter().cloned())
                    .collect()
            } else {
                item.terms.clone()
            };
            if let Some(name) = item
                .subsplease_name
                .as_ref()
                .filter(|_| site == Some(Site::SubsPlease))
            {
                let lower = name.to_lowercase();
                queries.retain(|query| query.to_lowercase() != lower);
                queries.insert(0, name.clone());
            }
            // The search that checks the host asks for every name alone, so the grouped search can
            // be compared with them.
            let claimed = groupable
                && works.is_none()
                && grouping
                    .as_deref_mut()
                    .is_some_and(|grouping| grouping.claim(&host));
            let mut checking = claimed;
            // Each page found, and whether it held every match (judged before it counts towards
            // the host's page size).
            let mut pages: Vec<(Vec<FeedArticle>, bool)> = Vec::new();
            let mut stopped = false;
            for term in &queries {
                let articles = ask(self.searcher, &mut found, feed, term, self.now);
                if found.stopped.is_some() {
                    stopped = true;
                    break;
                }
                let Some(articles) = articles else {
                    checking = false;
                    continue;
                };
                let size = u64::try_from(articles.len()).unwrap_or(u64::MAX);
                let complete = grouping
                    .as_deref()
                    .is_some_and(|grouping| grouping.complete(&host, size));
                if let Some(grouping) = grouping.as_deref_mut() {
                    grouping.saw(&host, size);
                }
                found.add(feed.id.unwrap_or_default(), &articles);
                releases.extend(releases_in(&articles, item, self.matcher, self.now));
                pages.push((articles, complete));
                covered = before
                    .union(&item.numbers(&releases, self.now))
                    .copied()
                    .collect();
                if groupable && works == Some(true) && *term == grouped && complete {
                    // The grouped page held every match: the names alone could find nothing more.
                    break;
                }
                if !checking && item.covers_everything(&covered) {
                    break;
                }
            }
            if checking && !stopped {
                if let Some(grouping) = grouping.as_deref_mut() {
                    self.check_grouping(grouping, &host, feed, &grouped, &pages, &mut found);
                }
            }
            if claimed {
                if let Some(grouping) = grouping.as_deref_mut() {
                    grouping.release(&host);
                }
            }
            if found.stopped.is_some() {
                return found;
            }
        }
        found
    }

    /// Asks the grouped search once, and keeps grouping only if it found exactly what the names
    /// found alone.
    ///
    /// Nothing is decided from empty pages or pages that may have been cut off: an empty one
    /// cannot tell a search that works from one that ignores the names, and a cut one may have
    /// left matches out.
    fn check_grouping(
        &self,
        grouping: &mut Grouping,
        host: &str,
        feed: &Feed,
        grouped: &str,
        pages: &[(Vec<FeedArticle>, bool)],
        found: &mut Searched,
    ) {
        let alone: HashSet<&str> = pages
            .iter()
            .flat_map(|(articles, _)| articles.iter().map(|article| article.guid.as_str()))
            .collect();
        if alone.is_empty() || !pages.iter().all(|(_, complete)| *complete) {
            return;
        }
        let Some(together) = ask(self.searcher, found, feed, grouped, self.now) else {
            return;
        };
        let size = u64::try_from(together.len()).unwrap_or(u64::MAX);
        let same: HashSet<&str> = together
            .iter()
            .map(|article| article.guid.as_str())
            .collect();
        let works = grouping.complete(host, size) && same == alone;
        grouping.saw(host, size);
        grouping.record(host, works);
        if works {
            info!(
                "{host} finds a show's names in one search, so they are asked for together from now on."
            );
        } else {
            warn!(
                "{host} did not answer a search for several names as it answers each alone, so each is asked for alone."
            );
        }
    }
}

/// Runs the searches for one show: sites in turn, each site's requests one after another.
///
/// With `stop_when_found` the feeds are asked one after another by priority instead, and the
/// lower ones are skipped once what was found covers every missing episode. That saves most
/// searches of a first run; it is not for finding which feed holds a show's saved files, nor for
/// showing what every feed carries.
pub fn search(
    item: &Due,
    matcher: &Matcher,
    now: Timestamp,
    searcher: &dyn Searcher,
    stop_when_found: bool,
    mut grouping: Option<&mut Grouping>,
) -> Searched {
    let run = SiteSearch {
        item,
        matcher,
        now,
        searcher,
    };
    if stop_when_found {
        return run.run(&item.feeds, true, grouping);
    }
    let mut by_site: Vec<(Option<Site>, Vec<Feed>)> = Vec::new();
    for feed in &item.feeds {
        let site = sites::site_of(&feed.url);
        match by_site.iter_mut().find(|(known, _)| *known == site) {
            Some((_, feeds)) => feeds.push(feed.clone()),
            None => by_site.push((site, vec![feed.clone()])),
        }
    }
    let mut merged = Searched::default();
    for (_, group) in &by_site {
        let result = run.run(group, false, grouping.as_deref_mut());
        for (feed_id, articles) in &result.articles {
            merged.add(*feed_id, articles);
        }
        if merged.stopped.is_none() {
            merged.stopped = result.stopped;
        }
    }
    merged
}

/// The feed a show's search starts with and its host, when that feed is on nyaa.
fn first_nyaa_feed(item: &Due) -> Option<(&Feed, String)> {
    let feed = item.feeds.first()?;
    (sites::site_of(&feed.url) == Some(Site::Nyaa)).then(|| (feed, sites::host_of(&feed.url)))
}

/// A show whose own search would check whether its first feed's host takes several names at
/// once, if any host is still to be checked.
pub fn unchecked<'a>(items: &'a [Due], grouping: &Grouping) -> Option<&'a Due> {
    items.iter().find(|item| {
        item.terms.len() > 1
            && first_nyaa_feed(item).is_some_and(|(_, host)| grouping.works(&host).is_none())
    })
}

/// The shows in groups whose releases one page should hold, going by how many episodes of each
/// have aired.
fn shared_groups(items: &[&Due], room: u64) -> Vec<Vec<usize>> {
    let mut groups: Vec<Vec<usize>> = vec![Vec::new()];
    let mut used: u64 = 0;
    for (at, item) in items.iter().enumerate() {
        let aired = item
            .latest_aired
            .filter(|aired| *aired != 0)
            .unwrap_or_else(|| i64::try_from(item.missing.len()).unwrap_or(i64::MAX));
        let expected = u64::try_from(aired).unwrap_or(0) + 1;
        if groups.last().is_some_and(|group| !group.is_empty()) && used + expected > room {
            groups.push(Vec::new());
            used = 0;
        }
        if let Some(group) = groups.last_mut() {
            group.push(at);
        }
        used += expected;
    }
    groups
}

/// Searches for several shows with one request per nyaa feed they start on; returns, by show,
/// what covered it.
///
/// Only for hosts shown to take several names in one search. A show counts as covered when the
/// page supplies each of its missing episodes, full or not: its own search would have stopped
/// there too. Every other show is left out and is searched for on its own, so a shared search can
/// cost a request but never a release.
pub fn search_shared(
    items: &[Due],
    matchers: &HashMap<i64, &Matcher>,
    now: Timestamp,
    searcher: &dyn Searcher,
    grouping: &mut Grouping,
) -> Vec<(i64, Searched)> {
    let mut found: Vec<(i64, Searched)> = Vec::new();
    let mut by_feed: Vec<(&Feed, String, Vec<&Due>)> = Vec::new();
    for item in items {
        let Some((feed, host)) = first_nyaa_feed(item) else {
            continue;
        };
        if grouping.works(&host) != Some(true) || !matchers.contains_key(&item.show_id) {
            continue;
        }
        match by_feed.iter_mut().find(|(known, _, _)| known.id == feed.id) {
            Some((_, _, sharing)) => sharing.push(item),
            None => by_feed.push((feed, host, vec![item])),
        }
    }
    for (feed, host, sharing) in by_feed {
        let room = (grouping.page_size(&host) as f64 * SHARED_PAGE_SHARE) as u64;
        for group in shared_groups(&sharing, room) {
            if group.len() < 2 {
                continue;
            }
            let mut terms: Vec<String> = Vec::new();
            for term in group.iter().flat_map(|at| sharing[*at].terms.iter()) {
                if !terms.contains(term) {
                    terms.push(term.clone());
                }
            }
            let articles = match searcher.search(&feed.url, &sites::any_of(&terms), now) {
                // Each show's own search follows and reports what is wrong with the site.
                Err(error) => {
                    debug!("A shared search of '{}' failed: {error}", feed.name);
                    return found;
                }
                Ok(None) => break,
                Ok(Some(articles)) => articles,
            };
            grouping.saw(&host, u64::try_from(articles.len()).unwrap_or(u64::MAX));
            for at in group {
                let item = sharing[at];
                let Some(matcher) = matchers.get(&item.show_id) else {
                    continue;
                };
                let releases = releases_in(&articles, item, matcher, now);
                if item.covers_everything(&item.numbers(&releases, now)) {
                    found.push((
                        item.show_id,
                        Searched {
                            articles: vec![(feed.id.unwrap_or_default(), articles.clone())],
                            stopped: None,
                        },
                    ));
                }
            }
        }
    }
    found
}

/// Stores what a search found: dated plausibly for the show, and titled so that it could be the
/// show's. Returns the rows added.
pub fn apply(ctx: &mut Context<'_>, item: &Due, found: &Searched) -> Result<usize> {
    let Some(mut show) = Show::find(ctx.conn, "WHERE id = ?", [item.show_id])? else {
        return Ok(0);
    };
    let mut added = 0;
    let matcher = matcher_for(&show, ctx.settings.min_resolution);
    for (feed_id, articles) in &found.articles {
        let feed = Feed::find(ctx.conn, "WHERE id = ?", [feed_id])?;
        // Only what this show would match is stored: a stored article is marked known, so a later
        // RSS read would skip it and a followed show it belongs to would never get it. What is
        // kept is matched as any article is, so a title this show and another both match reaches
        // the right one. (A completed show is not followed, hence its own matcher.)
        let kept: Vec<FeedArticle> = articles
            .iter()
            .filter(|article| {
                plausible_for_show(article.published_at, item.first_air, ctx.now)
                    && broad_match(&matcher, &parse_release_title(&article.title))
            })
            .cloned()
            .collect();
        let Some(feed) = feed.filter(|_| !kept.is_empty()) else {
            continue;
        };
        added += ingest::ingest(ctx, &feed, &kept)?.new;
    }
    if added > 0 && show.completed && !outstanding(ctx, &show, false)?.is_empty() {
        // A finished show is not followed, so an episode it never fetched would not be grabbed.
        show.completed = false;
    }
    if found.stopped.is_none() {
        // Only a search that ran to the end counts as done; one a site cut short is tried again.
        let fruitless = if added > 0 {
            0
        } else {
            match ctx.caches.searched.get(&item.show_id) {
                Some(last) if last.signature == item.signature => last.fruitless + 1,
                _ => 1,
            }
        };
        ctx.caches.searched.insert(
            item.show_id,
            SearchRecord {
                signature: item.signature,
                at: ctx.now,
                episodes: item.missing.iter().copied().collect(),
                fruitless,
            },
        );
        show.last_backfill_at = Some(ctx.now);
    }
    show.update(ctx.conn)?;
    Ok(added)
}
