//! Searching the feeds' sites for past releases.

use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::thread;

use kisetsu_clients::sites::Grouping;
use kisetsu_db::schema::Show;
use kisetsu_domain::matcher::Matcher;
use kisetsu_services::backfill::{self, Due, DueOptions, Searched};
use kisetsu_services::context::{Context, get_state, set_state, setup_reviewing};
use tracing::{debug, warn};

use super::*;
use crate::remote::RemoteSearch;
use crate::scheduler::panic_message;

/// The show's matcher for a search, or `None` when the show was deleted while the search waited
/// its turn.
fn matcher_of(ctx: &mut Context<'_>, show_id: i64) -> kisetsu_services::Result<Option<Matcher>> {
    let show = Show::find(ctx.conn, "WHERE id = ?", [show_id])?;
    Ok(show.map(|show| backfill::matcher_for(&show, ctx.settings.min_resolution)))
}

/// How many shows are followed: not finished.
fn followed_count(ctx: &mut Context<'_>) -> kisetsu_services::Result<usize> {
    let count: i64 = ctx
        .conn
        .prepare_cached("SELECT COUNT(*) FROM shows WHERE completed = 0")?
        .query_row([], |row| row.get(0))?;
    Ok(usize::try_from(count).unwrap_or(0))
}

/// What a run of searches has found out so far.
#[derive(Default)]
struct Run {
    /// What a search shared by several shows already found for each.
    shared: HashMap<i64, Searched>,
    /// Why a site cut the searches short; the shows not yet started are then left alone.
    stopped: Option<String>,
    added: usize,
}

impl Jobs {
    /// Requests a search of past releases for the shows, or for every show.
    pub fn request_backfill(&self, show_ids: Option<&[i64]>) {
        {
            let mut state = locked(&self.state);
            match show_ids {
                None => state.backfill_all = true,
                Some(ids) => state.backfill_requests.extend(ids.iter().copied()),
            }
        }
        self.poke_now(&["backfill"]);
    }

    /// Searches the feeds' sites for episodes no stored item supplies.
    pub(super) fn backfill(&self) -> JobResult {
        if !self.online(false) {
            return Ok(Some(NETWORK_RETRY));
        }
        let requested = std::mem::take(&mut locked(&self.state).backfill_requests);
        let everything = std::mem::take(&mut locked(&self.state).backfill_all);
        let mut added = 0;
        for &show_id in &requested {
            let options = DueOptions {
                force: true,
                ..DueOptions::default()
            };
            added += self.backfill_show(show_id, options, false)?;
        }
        if everything || requested.is_empty() {
            self.read_schedule()?;
            let schedule = locked(&self.state).schedule.clone();
            self.engine
                .call(move |ctx| backfill::note_schedule(ctx, schedule.as_deref()))?;
            // Before the first downloads are confirmed every show is paused, and searched all the
            // same so the user sees what would be downloaded.
            let reviewing = self.engine.read(|ctx| setup_reviewing(ctx.conn))?;
            let options = DueOptions {
                force: everything,
                paused: reviewing,
                ..DueOptions::default()
            };
            let (due, followed) = self.engine.call(move |ctx| {
                let due = backfill::due(ctx, options, None)?;
                Ok((due, followed_count(ctx)?))
            })?;
            if reviewing {
                self.begin_setup_progress(followed, due.len());
            }
            added += self.search(due, false)?;
        }
        if added > 0 {
            self.poke_now(&["grab"]);
        }
        Ok(None)
    }

    /// Reads which shows SubsPlease airs, when a feed is on SubsPlease and the last read is old.
    /// One request.
    pub(super) fn read_schedule(&self) -> Result<()> {
        let now = self.engine.now();
        let fresh = locked(&self.state)
            .schedule_at
            .is_some_and(|at| now.duration_since(at) < SCHEDULE_KEEP);
        if fresh
            || self
                .engine
                .read(|ctx| backfill::subsplease_feeds(ctx))?
                .is_empty()
        {
            return Ok(());
        }
        match self.remote.subsplease_schedule() {
            Ok(titles) => {
                let mut state = locked(&self.state);
                state.schedule = titles;
                state.schedule_at = Some(now);
            }
            Err(error) => debug!("SubsPlease's schedule could not be read: {error}"),
        }
        Ok(())
    }

    /// Searches the show's feed sites for past episodes now; returns how many releases were found.
    pub fn search_older(&self, show_id: i64) -> Result<usize> {
        let options = DueOptions {
            force: true,
            everywhere: true,
            ..DueOptions::default()
        };
        let added = self.backfill_show(show_id, options, false)?;
        if added > 0 {
            self.poke_now(&["grab"]);
        }
        Ok(added)
    }

    pub(super) fn backfill_show(
        &self,
        show_id: i64,
        options: DueOptions,
        exhaustive: bool,
    ) -> Result<usize> {
        let options = DueOptions {
            show_id: Some(show_id),
            ..options
        };
        let due = self
            .engine
            .call(move |ctx| backfill::due(ctx, options, None))?;
        self.search(due, exhaustive)
    }

    /// Runs `with` on what the searches know of the sites, after taking in how many results the
    /// feeds' pages held. Searches share it; it holds itself only for each short step.
    pub(super) fn with_grouping<R>(&self, with: impl FnOnce(&Grouping) -> R) -> R {
        for (host, count) in locked(&self.page_sizes).drain(..) {
            self.grouping.saw(&host, count);
        }
        with(&self.grouping)
    }

    /// Searches for the shows' past releases, a few shows at a time; each site's own pacing keeps
    /// its requests spaced.
    ///
    /// Unless `exhaustive`, a show's lower feeds are skipped once its missing episodes are found.
    /// A search a site cut short (rate limiting) stops the shows not yet started.
    pub(super) fn search(&self, due: Vec<Due>, exhaustive: bool) -> Result<usize> {
        self.load_grouping()?;
        let mut due = due;
        let run = Mutex::new(Run::default());
        if !exhaustive && due.len() > 1 {
            // A site not yet checked for taking several names at once is checked by one show's
            // own search.
            for _ in 0..SITE_CHECK_SEARCHES {
                let first = self.with_grouping(|grouping| {
                    backfill::unchecked(&due, grouping).map(|item| item.show_id)
                });
                let Some(show_id) = first.filter(|_| locked(&run).stopped.is_none()) else {
                    break;
                };
                if let Some(at) = due.iter().position(|item| item.show_id == show_id) {
                    let item = due.remove(at);
                    self.search_one(&item, exhaustive, &run)?;
                    self.note_searched();
                }
            }
            if locked(&run).stopped.is_none() {
                self.search_shared(&due, &run)?;
            }
        }
        self.search_each(&due, exhaustive, &run)?;
        self.keep_grouping()?;
        let added = locked(&run).added;
        Ok(added)
    }

    /// Searches for each show on its own, up to [`SEARCH_CONCURRENCY`] shows at a time.
    fn search_each(&self, due: &[Due], exhaustive: bool, run: &Mutex<Run>) -> Result<()> {
        let next = AtomicUsize::new(0);
        let failed = AtomicBool::new(false);
        let work = || -> Result<()> {
            while !failed.load(Ordering::Acquire) {
                let Some(item) = due.get(next.fetch_add(1, Ordering::Relaxed)) else {
                    break;
                };
                self.search_one(item, exhaustive, run).inspect_err(|_| {
                    failed.store(true, Ordering::Release);
                })?;
                self.note_searched();
            }
            Ok(())
        };
        thread::scope(|scope| {
            let workers: Vec<_> = (0..SEARCH_CONCURRENCY.min(due.len()))
                .map(|_| scope.spawn(work))
                .collect();
            let mut outcome = Ok(());
            for worker in workers {
                let result = worker
                    .join()
                    .unwrap_or_else(|panic| Err(Error::Panicked(panic_message(&*panic))));
                outcome = outcome.and(result);
            }
            outcome
        })
    }

    /// Searches once for the shows that share a site's page, and keeps what covers each.
    fn search_shared(&self, due: &[Due], run: &Mutex<Run>) -> Result<()> {
        let ids: Vec<i64> = due.iter().map(|item| item.show_id).collect();
        let matchers = self.engine.call(move |ctx| {
            let mut matchers = HashMap::new();
            for show_id in ids {
                if let Some(matcher) = matcher_of(ctx, show_id)? {
                    matchers.insert(show_id, matcher);
                }
            }
            Ok(matchers)
        })?;
        let by_show: HashMap<i64, &Matcher> = matchers.iter().map(|(id, m)| (*id, m)).collect();
        let now = self.engine.now();
        let searcher = RemoteSearch(self.remote.as_ref());
        let found = self.with_grouping(|grouping| {
            backfill::search_shared(due, &by_show, now, &searcher, grouping)
        });
        locked(run).shared.extend(found);
        Ok(())
    }

    /// Searches for one show's releases, unless a site stopped the searches, and stores what was
    /// found.
    fn search_one(&self, item: &Due, exhaustive: bool, run: &Mutex<Run>) -> Result<()> {
        if locked(run).stopped.is_some() {
            return Ok(());
        }
        let show_id = item.show_id;
        // The backfill job and Search older never search one show twice at once.
        let lock = Arc::clone(locked(&self.search_locks).entry(show_id).or_default());
        let _searching = locked(&lock);
        let Some(matcher) = self.engine.call(move |ctx| matcher_of(ctx, show_id))? else {
            return Ok(());
        };
        let shared = locked(run).shared.remove(&show_id);
        let found = match shared {
            Some(found) => found,
            None => {
                let now = self.engine.now();
                let searcher = RemoteSearch(self.remote.as_ref());
                self.with_grouping(|grouping| {
                    backfill::search(item, &matcher, now, &searcher, !exhaustive, Some(grouping))
                })
            }
        };
        let stopped = found.stopped.clone();
        let (stored, kept) = (item.clone(), found);
        let added = self
            .engine
            .call(move |ctx| backfill::apply(ctx, &stored, &kept))?;
        let mut state = locked(run);
        state.added += added;
        if let Some(reason) = stopped {
            if state.stopped.is_none() {
                warn!("Past-release search stopped: {reason}");
                state.stopped = Some(reason);
            }
            return Ok(());
        }
        drop(state);
        // Whatever it found, a finished search lets the grab mark what is still not there as
        // missed.
        self.poke_soon("grab", BURST_GAP);
        Ok(())
    }

    /// Starts counting the setup review's searches; the shows with nothing to search for count
    /// as searched. Once done, a later backfill leaves the count alone.
    fn begin_setup_progress(&self, followed: usize, due: usize) {
        {
            let mut state = locked(&self.state);
            if state.setup_progress.is_some_and(|progress| progress.done) {
                return;
            }
            state.setup_progress = Some(SetupProgress {
                searched: followed.saturating_sub(due),
                total: followed,
                done: false,
            });
        }
        self.publish_status();
    }

    fn note_searched(&self) {
        {
            let mut state = locked(&self.state);
            let Some(progress) = state
                .setup_progress
                .as_mut()
                .filter(|progress| !progress.done)
            else {
                return;
            };
            progress.searched = (progress.searched + 1).min(progress.total);
        }
        self.publish_status();
    }

    /// Ends the setup review's count once startup's searches are over.
    pub(super) fn finish_setup_progress(&self) -> Result<()> {
        let reviewing = self.engine.read(|ctx| setup_reviewing(ctx.conn))?;
        if !reviewing {
            return Ok(());
        }
        let followed = self.engine.call(followed_count)?;
        locked(&self.state).setup_progress = Some(SetupProgress {
            searched: followed,
            total: followed,
            done: true,
        });
        self.publish_status();
        Ok(())
    }

    /// Forgets the setup review's count once the user decided.
    pub fn clear_setup_progress(&self) {
        locked(&self.state).setup_progress = None;
        self.publish_status();
    }

    /// Takes back what the searches learned of each site before a restart, so it is not checked
    /// again.
    pub(super) fn load_grouping(&self) -> Result<()> {
        if locked(&self.state).grouping_kept.is_some() {
            return Ok(());
        }
        let kept = self
            .engine
            .call(|ctx| get_state(ctx.conn, GROUPING_KEY, ""))?;
        self.with_grouping(|grouping| grouping.load(&kept));
        locked(&self.state).grouping_kept = Some(kept);
        Ok(())
    }

    pub(super) fn keep_grouping(&self) -> Result<()> {
        let kept = self.with_grouping(|grouping| grouping.dump());
        if locked(&self.state).grouping_kept.as_deref() == Some(kept.as_str()) {
            return Ok(());
        }
        let stored = kept.clone();
        self.engine
            .call(move |ctx| set_state(ctx.conn, GROUPING_KEY, &stored))?;
        locked(&self.state).grouping_kept = Some(kept);
        Ok(())
    }
}
