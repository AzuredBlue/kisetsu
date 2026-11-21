//! Adding a show by hand: the search for the releases of a list entry, kept until the user adds
//! the show, and what follows the add.

use std::time::Instant;

use kisetsu_clients::anilist::ListEntry;
use kisetsu_clients::rss::FeedArticle;
use kisetsu_services::addshow::{self, Preview};
use kisetsu_services::backfill::{self, Due};
use tracing::warn;

use super::*;
use crate::remote::RemoteSearch;

/// How long what a search found is kept for the user to add the show.
pub(super) const FOUND_KEEP: Duration = Duration::from_secs(3600);

/// What a search found for a show not yet added, by feed.
pub(super) type Found = Vec<(i64, Vec<FeedArticle>)>;

/// What searching for a new show showed.
#[derive(Debug, Clone, PartialEq)]
pub struct NewShowSearch {
    pub preview: Preview,
    /// Why a site cut the search short.
    pub stopped: Option<String>,
}

impl Jobs {
    /// Searches the feeds' sites for a show the user is adding, then previews what each feed
    /// carries for it.
    ///
    /// What the search found is kept in memory, and stored only if the user adds the show
    /// ([`Self::found_for`]).
    pub fn search_new_show(&self, entry: &ListEntry) -> Result<NewShowSearch> {
        let planned = entry.clone();
        let plan = self
            .engine
            .call(move |ctx| addshow::search_plan(ctx, &planned))?;
        let next = entry.next_airing_episode.filter(|next| *next > 0);
        let total = entry
            .total_episodes
            .filter(|total| *total > 0)
            .unwrap_or_else(|| (next.unwrap_or(2) - 1).max(1));
        let aired = next.map_or(total, |next| next - 1);
        // Episodes the user watched are not looked for; with every aired one watched there is
        // nothing to search.
        let wanted: Vec<i64> = if plan.watched == 0 || plan.watched < aired {
            (plan.watched + 1..=total).collect()
        } else {
            Vec::new()
        };
        let mut stopped = None;
        let mut articles = Found::new();
        if !plan.terms.is_empty() && !plan.feeds.is_empty() && !wanted.is_empty() {
            self.load_grouping()?;
            let due = Due {
                show_id: 0,
                name: entry.display_name.clone(),
                terms: plan.terms.clone(),
                feeds: plan.feeds.clone(),
                missing: wanted,
                signature: 0,
                first_air: None,
                air_times: Vec::new(),
                target_count: None,
                latest_aired: None,
                tolerance_hours: 0,
                subsplease_name: None,
            };
            let matcher = addshow::matcher_for(entry, plan.min_resolution);
            let now = self.engine.now();
            let searcher = RemoteSearch(self.remote.as_ref());
            let found = self.with_grouping(|grouping| {
                backfill::search(&due, &matcher, now, &searcher, false, Some(grouping))
            });
            stopped = found.stopped;
            articles = found.articles;
            self.keep_grouping()?;
        }
        {
            let mut kept = locked(&self.found);
            kept.retain(|_, (at, _)| at.elapsed() < FOUND_KEEP);
            kept.insert(entry.anilist_id, (Instant::now(), articles.clone()));
        }
        let (previewed, unsearchable) = (entry.clone(), plan.unsearchable);
        let preview = self
            .engine
            .dry_run(move |ctx| addshow::preview(ctx, &previewed, &unsearchable, &articles))?;
        Ok(NewShowSearch { preview, stopped })
    }

    /// What the last search for this entry found, to store when the show is added; kept until
    /// [`Self::forget_found`].
    pub fn found_for(&self, anilist_id: i64) -> Vec<(i64, Vec<FeedArticle>)> {
        locked(&self.found)
            .get(&anilist_id)
            .filter(|(at, _)| at.elapsed() < FOUND_KEEP)
            .map(|(_, found)| found.clone())
            .unwrap_or_default()
    }

    pub fn forget_found(&self, anilist_id: i64) {
        locked(&self.found).remove(&anilist_id);
    }

    /// Dates the episodes of a show the user just added, then lets the grab and settle passes
    /// follow it.
    pub fn after_manual_add(&self) {
        if let Err(error) = self.refresh_schedules() {
            warn!("Could not date the new show's episodes: {error}");
        }
        self.poke_now(&["grab", "settle", "hunt", "palette"]);
    }
}
