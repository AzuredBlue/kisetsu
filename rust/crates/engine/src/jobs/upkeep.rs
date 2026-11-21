//! Colours and housekeeping.

use kisetsu_services::cycle;
use tracing::debug;

use super::*;
use crate::parallel::map_limited;

impl Jobs {
    /// Reads the colours of the banners that changed, a few at a time, and stores them all as one
    /// change.
    pub(super) fn palette(&self) -> JobResult {
        let targets = self.engine.call(|ctx| cycle::palette_targets(ctx))?;
        if targets.is_empty() {
            return Ok(None);
        }
        let found = map_limited(&targets, PALETTE_CONCURRENCY, |target| {
            self.remote.hues(&target.banner, &target.cover)
        });
        self.engine.call(move |ctx| {
            // A failed download is not remembered, so it is tried again next time.
            for (target, hues) in targets.iter().zip(found) {
                if let Ok(hues) = hues {
                    cycle::store_palette(ctx, target.show_id, &target.key, hues.as_ref())?;
                }
            }
            Ok(())
        })?;
        Ok(None)
    }

    pub(super) fn housekeeping(&self) -> JobResult {
        if !self.configured()? {
            return Ok(None);
        }
        if let Err(error) = self.engine.call(cycle::housekeeping) {
            match qbit_error(&error) {
                Some(failure) => debug!("Housekeeping could not reach qBittorrent: {failure}"),
                None => return Err(error),
            }
        }
        Ok(None)
    }
}
