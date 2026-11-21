//! Grabbing releases and following the downloads.

use std::sync::atomic::Ordering;
use std::time::Duration;

use kisetsu_services::cycle;

use super::*;

impl Jobs {
    pub(super) fn grab(&self) -> JobResult {
        if !self.is_ready() {
            return Ok(None);
        }
        let result = match self.engine.call(cycle::grab_cycle) {
            Ok(result) => result,
            Err(error) => {
                return match qbit_error(&error) {
                    Some(failure) => Ok(self.qbit_failed(failure)),
                    None => Err(error),
                };
            }
        };
        if let Some(first) = result.errors.first() {
            // Only a failed call says qBittorrent is away; a pass with nothing to add never asked
            // it, so settle gives the all-clear.
            self.set_qbit(Some(first.clone()));
        }
        self.set_grab(result.failed.first().cloned());
        if result.started > 0 || result.adopted > 0 {
            // The grab already took up the torrents qBittorrent shows; settle follows them as
            // often as it does a busy queue.
            self.poke_soon("settle", SETTLE_BUSY);
        }
        Ok(result.next_candidate_in.map(secs))
    }

    /// Follows the downloads. Until startup is done it only mirrors qBittorrent: adoption has not
    /// yet looked at what exists, so nothing is added, deleted or forgotten.
    pub(super) fn settle(&self) -> JobResult {
        if !self.synced.load(Ordering::Acquire) {
            return Ok(None);
        }
        let observe_only = !self.is_ready();
        let result = match self
            .engine
            .call(move |ctx| cycle::settle_cycle(ctx, observe_only))
        {
            Ok(result) => result,
            Err(error) => {
                return match qbit_error(&error) {
                    Some(failure) => Ok(self.qbit_failed(failure)),
                    None => Err(error),
                };
            }
        };
        self.set_qbit(None);
        if result.changed {
            self.poke_now(&["grab"]);
        }
        let mut delay = if result.busy {
            SETTLE_BUSY
        } else if result.active {
            SETTLE_ACTIVE
        } else {
            SETTLE_IDLE
        };
        if let Some(next_due) = result.next_due {
            let wait = secs(next_due.duration_since(self.engine.now()).as_secs_f64());
            delay = delay.min(wait.max(Duration::from_secs(1)));
        }
        Ok(Some(delay))
    }
}
