//! Bringing the app up, and what follows a show being added.

use std::sync::atomic::Ordering;
use std::thread;

use kisetsu_services::backfill::{self, DueOptions};
use kisetsu_services::{adoption, cycle, tags};

use super::*;

impl Jobs {
    /// Brings the app up in an order that never grabs or deletes before it knows what already
    /// exists.
    pub(super) fn startup(&self) -> JobResult {
        if !self.configured()? {
            return Ok(Some(SETUP_WAIT));
        }
        // qBittorrent was already synced on a pass that then waited for the network.
        if self.synced.load(Ordering::Acquire) && !self.online(true) {
            return Ok(Some(NETWORK_RETRY));
        }
        let synced = self.engine.call(|ctx| {
            ctx.qbit()?.sync(true)?;
            Ok(())
        });
        if let Err(error) = synced {
            let Some(failure) = qbit_error(&error) else {
                return Err(error);
            };
            let first = !std::mem::replace(&mut locked(&self.state).offline_started, true);
            if first {
                // AniList and the feeds don't need qBittorrent: keep them current on their own
                // timers while it is away.
                self.poke_now(&["sync_anilist", "refresh_feeds"]);
            }
            return Ok(self.qbit_failed(failure));
        }
        self.set_qbit(None);
        // What is downloading shows progress from here on, instead of after every backfill
        // search has finished.
        self.synced.store(true, Ordering::Release);
        self.poke_now(&["settle"]);
        // AniList, the feeds and the searches below all need the network.
        if !self.online(true) {
            return Ok(Some(NETWORK_RETRY));
        }
        // AniList and the feeds are different sites, so they are read together. Items stored
        // before a show is added are matched to it by the grab pass (`ensure_matches_current`).
        let (sync, feeds, schedule) = thread::scope(|scope| {
            let sync = scope.spawn(|| self.sync_anilist(true));
            let feeds = scope.spawn(|| self.refresh_feeds(true));
            let schedule = self.read_schedule();
            (join(sync), join(feeds), schedule)
        });
        let next_sync = sync?;
        feeds?;
        schedule?;
        self.engine.call(adoption::adopt_existing)?;
        self.engine.call(adoption::reattribute)?;
        self.engine.call(tags::sync_tags)?;
        // Shows whose saved files no feed accounts for yet are held, so nothing guesses their
        // feed before the search below.
        let (_, unmatched) = self.engine.call(cycle::lock_saved_files)?;
        // Adoption has seen what qBittorrent holds: grabbing what the feeds already carry need
        // not wait for the searches.
        self.ready.store(true, Ordering::Release);
        self.publish_status();
        self.poke_now(&["grab", "settle", "hunt", "palette"]);
        self.scheduler.delay("sync_anilist", next_sync);
        self.scheduler.delay("refresh_feeds", self.next_read()?);
        self.backfill()?;
        self.search_held(&unmatched)?;
        self.scheduler.delay("backfill", BACKFILL_INTERVAL);
        Ok(None)
    }

    /// Matches shows to the feed their saved files came from; shows no feed accounts for are
    /// searched for first.
    pub fn search_saved_files(&self, only: Option<&[i64]>) -> Result<()> {
        let (_, unmatched) = self.engine.call(cycle::lock_saved_files)?;
        let targets: Vec<i64> = unmatched
            .into_iter()
            .filter(|id| only.is_none_or(|only| only.contains(id)))
            .collect();
        self.search_held(&targets)
    }

    /// Searches for the held shows' releases, releases each one searched, then locks what the
    /// saved files now prove.
    fn search_held(&self, targets: &[i64]) -> Result<()> {
        for &show_id in targets {
            // Every feed is searched: the saved files are matched to the one feed whose items
            // hold them.
            let options = DueOptions {
                force: true,
                ..DueOptions::default()
            };
            self.backfill_show(show_id, options, true)?;
            if self
                .engine
                .call(move |ctx| backfill::search_completed(ctx, show_id))?
            {
                self.engine.call(move |ctx| {
                    cycle::release_hold(ctx, &[show_id]);
                    Ok(())
                })?;
            }
        }
        if !targets.is_empty() {
            self.engine.call(cycle::lock_saved_files)?;
            self.poke_now(&["grab"]);
        }
        Ok(())
    }

    /// Fills the stores for shows that were only just added, so the same pass can discover their
    /// feed.
    pub fn on_new_shows(&self, show_ids: &[i64]) -> Result<()> {
        self.refresh_feeds(true)?;
        self.engine.call(adoption::adopt_existing)?;
        let options = DueOptions {
            force: true,
            ..DueOptions::default()
        };
        for &show_id in show_ids {
            self.backfill_show(show_id, options, false)?;
        }
        self.engine.call(adoption::reattribute)?;
        self.search_saved_files(Some(show_ids))?;
        self.poke_now(&["grab"]);
        Ok(())
    }
}

/// What a scoped thread returned; its panic goes on as the caller's.
fn join<T>(handle: thread::ScopedJoinHandle<'_, T>) -> T {
    handle
        .join()
        .unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}
