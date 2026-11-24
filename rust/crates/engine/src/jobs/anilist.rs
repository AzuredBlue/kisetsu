//! Syncing with AniList.

use std::collections::HashSet;
use std::time::Duration;

use kisetsu_clients::anilist::{AniListError, Deferral, Schedules};
use kisetsu_services::cycle;
use kisetsu_services::shows::{SCHEDULE_FULL_LOOKBACK, SCHEDULE_REFRESH_LOOKBACK};
use tracing::warn;

use super::*;

/// Why the air times could not all be fetched.
enum Stop {
    AniList(AniListError),
    Engine(Error),
}

impl From<AniListError> for Stop {
    fn from(error: AniListError) -> Self {
        Self::AniList(error)
    }
}

impl From<Error> for Stop {
    fn from(error: Error) -> Self {
        Self::Engine(error)
    }
}

/// What the air time passes have fetched so far.
#[derive(Default)]
struct Fetched {
    schedules: Schedules,
    /// The shows still owed their whole schedule; if AniList defers, they are left undated for
    /// the next pass.
    owed: Vec<i64>,
}

fn minutes(seconds: f64) -> i64 {
    (seconds / 60.0).round() as i64
}

impl Jobs {
    pub(super) fn sync_anilist(&self, startup: bool) -> Result<Duration> {
        let _syncing = locked(&self.anilist_lock);
        self.sync(startup)
    }

    fn sync(&self, startup: bool) -> Result<Duration> {
        let input = self.engine.call(|ctx| cycle::sync_input(ctx))?;
        if input.username.is_empty() {
            return Ok(NO_USER_WAIT);
        }
        let interval =
            Duration::from_secs(u64::try_from(input.sync_minutes.max(1)).unwrap_or(1) * 60);
        if startup && !input.monitored_ids.is_empty() {
            if let Some(ago) = input
                .last_sync_ago
                .filter(|ago| *ago < interval.as_secs_f64())
            {
                self.engine.call(cycle::rollover)?;
                let wait = secs(interval.as_secs_f64() - ago).max(SYNC_RETRY_FLOOR);
                info!(
                    "AniList was synced {} min ago. Next sync in {} min.",
                    minutes(ago),
                    minutes(wait.as_secs_f64())
                );
                return Ok(wait);
            }
        }
        // Offline, every request would fail and pause AniList for a while after the network is back.
        if !self.online(true) {
            self.engine.call(cycle::rollover)?;
            return Ok(NETWORK_RETRY);
        }
        let monitored: HashSet<i64> = input.monitored_ids.iter().copied().collect();
        let reuse = startup.then_some(LIST_REUSE_SECONDS);
        let seasonal = match self.remote.seasonal(&input.username, &monitored, reuse) {
            Ok(seasonal) => seasonal,
            Err(AniListError::Deferred(deferral)) => {
                self.set_anilist(Some(deferral.to_string()), Some(deferral.retry_in));
                self.engine.call(cycle::rollover)?;
                return Ok(secs(deferral.retry_in).max(SYNC_RETRY_FLOOR));
            }
            Err(error) => {
                self.set_anilist(
                    Some(error.to_string()),
                    Some(ANILIST_ERROR_RETRY.as_secs_f64()),
                );
                self.engine.call(cycle::rollover)?;
                return Ok(ANILIST_ERROR_RETRY);
            }
        };
        self.set_anilist(None, None);
        let count = seasonal.len();
        let applied = self
            .engine
            .call(move |ctx| cycle::apply_sync(ctx, &seasonal))?;
        let wait = match self.refresh_schedules()? {
            Some(retry) => retry.max(SYNC_RETRY_FLOOR),
            None => interval,
        };
        info!(
            "AniList synced: {count} shows. Next sync in {} min.",
            minutes(wait.as_secs_f64())
        );
        if !applied.added_ids.is_empty() && self.is_ready() {
            self.on_new_shows(&applied.added_ids)?;
        }
        // Settle deletes what the new progress says was watched.
        self.poke_now(&["grab", "hunt", "palette", "settle"]);
        Ok(wait)
    }

    /// Dates the episodes of the shows whose air times are old; returns the wait if AniList
    /// deferred part of it.
    pub(super) fn refresh_schedules(&self) -> Result<Option<Duration>> {
        let plan = self.engine.call(|ctx| cycle::schedule_plan(ctx))?;
        if plan.due_ids.is_empty() {
            return Ok(None);
        }
        let now = self.engine.now();
        let mut fetched = Fetched::default();
        let mut deferral: Option<Deferral> = None;
        match self.fetch_schedules(&plan, now, &mut fetched) {
            Ok(()) => {}
            Err(Stop::Engine(error)) => return Err(error),
            Err(Stop::AniList(AniListError::Deferred(mut deferred))) => {
                fetched
                    .schedules
                    .extend(std::mem::take(&mut deferred.partial));
                for media_id in &fetched.owed {
                    fetched.schedules.remove(media_id);
                }
                deferral = Some(deferred);
            }
            Err(Stop::AniList(error)) => {
                warn!("Could not refresh episode airing schedules: {error}");
                return Ok(Some(ANILIST_ERROR_RETRY));
            }
        }
        if !fetched.schedules.is_empty() {
            let due_ids = plan.due_ids;
            let schedules = fetched.schedules;
            self.engine
                .call(move |ctx| cycle::apply_schedule_results(ctx, &due_ids, &schedules))?;
            self.poke_now(&["grab", "hunt"]);
        }
        Ok(deferral.map(|deferral| {
            self.set_anilist(Some(deferral.to_string()), Some(deferral.retry_in));
            secs(deferral.retry_in)
        }))
    }

    /// Asks AniList for the air times the plan calls for: what is still to come for the shows
    /// whose past episodes are dated, and the whole schedule of the others.
    fn fetch_schedules(
        &self,
        plan: &cycle::SchedulePlan,
        now: Timestamp,
        fetched: &mut Fetched,
    ) -> std::result::Result<(), Stop> {
        let mut full = plan.full.clone();
        if !plan.windowed.is_empty() {
            let since = back(now, SCHEDULE_REFRESH_LOOKBACK);
            fetched
                .schedules
                .extend(self.remote.upcoming_schedules(&plan.windowed, since)?);
        }
        if !full.is_empty() {
            fetched.owed.clone_from(&full);
            let since = back(now, SCHEDULE_FULL_LOOKBACK);
            let covering = self.remote.upcoming_schedules(&full, since)?;
            let asked = std::mem::take(&mut full);
            let (uncovered, covering) = self.engine.call(move |ctx| {
                let uncovered = cycle::uncovered(ctx, &asked, &covering)?;
                Ok((uncovered, covering))
            })?;
            fetched.schedules.extend(covering);
            full = uncovered;
            fetched.owed.clone_from(&full);
        }
        if !full.is_empty() {
            fetched
                .schedules
                .extend(self.remote.media_schedules(&full)?);
            fetched.owed.clear();
        }
        Ok(())
    }
}

fn back(from: Timestamp, by: SignedDuration) -> Timestamp {
    from.saturating_sub(by).unwrap_or(from)
}
