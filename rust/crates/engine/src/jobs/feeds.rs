//! Reading the feeds.

use std::time::Duration;

use kisetsu_clients::sites::host_of;
use kisetsu_services::feedsync::{self, FeedRef, FetchFailure, FetchResult, Outcome};

use super::*;
use crate::parallel::map_limited;

impl Jobs {
    /// Reads the feeds in parallel; a feed backing off after failures is skipped. Returns what
    /// each feed read gave, by feed id.
    fn read_feeds(&self, refs: Vec<FeedRef>) -> Vec<(i64, FetchResult)> {
        let now = self.engine.now();
        let due: Vec<FeedRef> = {
            let state = locked(&self.state);
            refs.into_iter()
                .filter(|feed| state.backoff.ready(&feed.id, now))
                .collect()
        };
        let results = map_limited(&due, due.len(), |feed| {
            self.remote
                .fetch_feed(&feed.url)
                .map_err(|error| FetchFailure::from(&error))
        });
        let mut state = locked(&self.state);
        let mut page_sizes = locked(&self.page_sizes);
        let mut read = Vec::with_capacity(due.len());
        for (feed, result) in due.iter().zip(results) {
            match &result {
                Err(_) => {
                    state.backoff.failed(&feed.id, now);
                }
                Ok(articles) => {
                    state.backoff.succeeded(&feed.id);
                    // A feed is a page of its site's search: how many it lists says how many a
                    // search's page holds.
                    let count = u64::try_from(articles.len()).unwrap_or(u64::MAX);
                    page_sizes.push((host_of(&feed.url), count));
                }
            }
            read.push((feed.id, result));
        }
        read
    }

    /// Stores what the feeds listed and asks for what that calls for.
    fn apply(&self, results: Vec<(i64, FetchResult)>) -> Result<Outcome> {
        let outcome = self
            .engine
            .call(move |ctx| feedsync::apply_fetch(ctx, &results))?;
        if !outcome.matched_show_ids.is_empty() || outcome.new > 0 {
            self.poke_now(&["grab"]);
        }
        if !outcome.gaps.is_empty() {
            locked(&self.state).backfill_all = true;
            // Until startup is done, its own search takes this request; a job run beside it would
            // search the same shows again.
            if self.is_ready() {
                self.poke("backfill", GAP_BACKFILL_DELAY);
            }
        }
        if !outcome.reconsider.is_empty() {
            // A feed their searches left out has just shown it carries them: what it holds may be
            // what they miss.
            let shows: Vec<i64> = outcome.reconsider.iter().copied().collect();
            self.request_backfill(Some(&shows));
        }
        Ok(outcome)
    }

    /// Reads every feed on the next pass, needed or not.
    pub fn request_refresh(&self) {
        locked(&self.state).refresh_all = true;
        self.poke_now(&["refresh_feeds"]);
    }

    /// Fetches the feeds a show needs, or all of them, and stores what is new.
    pub(super) fn refresh_feeds(&self, everything: bool) -> JobResult {
        if !self.configured()? {
            return Ok(None);
        }
        {
            let _reading = locked(&self.feeds_lock);
            let everything = everything || std::mem::take(&mut locked(&self.state).refresh_all);
            let refs = self
                .engine
                .call(move |ctx| feedsync::plan_fetch(ctx, everything))?;
            let results = self.read_feeds(refs);
            if !results.is_empty() {
                self.apply(results)?;
            }
        }
        Ok(Some(self.next_read()?))
    }

    /// The time until the routine read has a feed to read; never sooner than its interval, as
    /// that is how often it looks.
    pub(super) fn next_read(&self) -> Result<Duration> {
        let (interval, at) = self.engine.read(|ctx| {
            let interval = SignedDuration::from_mins(ctx.settings.feed_refresh_minutes);
            Ok((interval, feedsync::next_read_at(ctx, interval)?))
        })?;
        let interval = Duration::try_from(interval).unwrap_or(Duration::ZERO);
        Ok(match at {
            None => interval,
            Some(at) => interval.max(secs(at.duration_since(self.engine.now()).as_secs_f64())),
        })
    }

    /// While shows are in their hunting window, fetches the feeds they may use more often.
    pub(super) fn hunt(&self) -> JobResult {
        if !self.configured()? {
            return Ok(None);
        }
        // Planned under the lock, so a read still running (the startup one) counts as done
        // instead of being repeated.
        let info = {
            let _reading = locked(&self.feeds_lock);
            let info = self.engine.call(|ctx| feedsync::plan_hunt(ctx))?;
            if !info.show_ids.is_empty() && !info.feeds.is_empty() {
                let results = self.read_feeds(info.feeds.clone());
                if !results.is_empty() {
                    self.apply(results)?;
                }
            }
            info
        };
        let now = self.engine.now();
        let hunted = !info.show_ids.is_empty();
        if hunted && !info.feeds.is_empty() {
            let wait = secs(info.interval_minutes as f64 * 60.0);
            locked(&self.state).hunt_read_at = Some(later(now, wait.as_secs_f64()));
            return Ok(Some(wait));
        }
        if let Some(wait_seconds) = info.wait_seconds.filter(|_| hunted) {
            // Their feeds were just read by another job: hunt once the first of them is an
            // interval old.
            locked(&self.state).hunt_read_at = Some(later(now, wait_seconds));
            return Ok(Some(secs(wait_seconds).max(HUNT_FLOOR)));
        }
        let start = info.plan.next_start;
        locked(&self.state).hunt_read_at = start;
        Ok(Some(match start {
            None => HUNT_IDLE_MAX,
            Some(start) => secs(start.duration_since(now).as_secs_f64())
                .min(HUNT_IDLE_MAX)
                .max(HUNT_FLOOR),
        }))
    }
}
