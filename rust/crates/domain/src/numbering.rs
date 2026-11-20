//! How a feed's episode numbers map onto the show's AniList numbers.
//!
//! Some feeds count from a different base (absolute numbers, or an earlier cour included).
//! The mapping is a per-show, per-feed offset: `local = raw - offset`.

use std::collections::{HashMap, HashSet};

use jiff::{SignedDuration, Timestamp};

use crate::schedule::{PUBLISHED_BEFORE_AIR_SLACK, date_mapped_episode, is_dated};

/// A season that ended this long ago cannot take a new item as its latest episode.
pub const SEASON_ENDED_AFTER: SignedDuration = SignedDuration::from_hours(14 * 24);

/// A show's episode numbering as far as it is known.
#[derive(Debug, Clone, Copy)]
pub struct SeasonFacts<'a> {
    pub offset: Option<i64>,
    pub latest_aired: Option<i64>,
    pub target_count: i64,
    /// The stored air times by local number.
    pub air_times: &'a [(i64, Timestamp)],
    pub tolerance_hours: i64,
    pub now: Timestamp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Mapped {
    pub local: Option<i64>,
    /// The air dates alone decided it. The caller may learn an offset from it when it
    /// contradicts a stored release.
    pub by_date: bool,
}

impl Mapped {
    /// A mapping the air dates did not decide.
    pub const fn new(local: Option<i64>) -> Self {
        Self {
            local,
            by_date: false,
        }
    }
}

impl SeasonFacts<'_> {
    fn air_time(&self, number: i64) -> Option<Timestamp> {
        self.air_times
            .iter()
            .find(|&&(known, _)| known == number)
            .map(|&(_, air_at)| air_at)
    }

    /// Maps a feed's raw episode number to the show's local one, or None when it is ambiguous.
    ///
    /// `newest_raw` is the highest raw number among the releases being mapped together; it lets
    /// absolute numbers ("45", "46", "47", "48") be told apart when nothing else can.
    pub fn map_episode(
        &self,
        raw: i64,
        published_at: Option<Timestamp>,
        newest_raw: Option<i64>,
    ) -> Mapped {
        if let Some(offset) = self.offset {
            let local = raw.checked_sub(offset);
            return Mapped::new(local.filter(|&local| {
                (1..=self.target_count).contains(&local)
                    && self.latest_aired.is_none_or(|latest| local <= latest)
            }));
        }

        let published = published_at.unwrap_or(Timestamp::UNIX_EPOCH);
        let dated = is_dated(published_at);
        let fits_season = (1..=self.target_count).contains(&raw);
        let within_aired = self
            .latest_aired
            .is_none_or(|latest| raw <= latest.saturating_add(1));

        // A number that fits the aired part of the season is taken as it is unless the release
        // is dated well before that episode aired (a feed numbering one higher than AniList).
        // Guessing from the date alone would shift releases a feed puts out hours before
        // AniList's air time ("03" posted before ep 3 airs is not ep 2).
        let taken = self
            .latest_aired
            .filter(|_| fits_season && within_aired)
            .is_some_and(|latest| match self.air_time(raw) {
                None => raw <= latest,
                Some(air_at) => {
                    !dated || air_at.duration_since(published) <= PUBLISHED_BEFORE_AIR_SLACK
                }
            });
        if taken {
            return Mapped::new(Some(raw));
        }

        if let Some(by_date) = date_mapped_episode(
            self.air_times,
            published,
            self.target_count,
            self.tolerance_hours,
        ) {
            return Mapped {
                local: Some(by_date),
                by_date: true,
            };
        }

        // A show of one episode (a film) has no numbering to count on from: "12" under its
        // name is a series' episode.
        let current = self.latest_aired.filter(|latest| {
            self.target_count > 1
                && raw > self.target_count
                && (1..=self.target_count).contains(latest)
        });
        if let Some(latest) = current {
            // "The newest release is the latest aired episode" only holds while the show is
            // current; for a season that ended long ago it would pin any new item to its end.
            if self
                .air_time(latest)
                .is_some_and(|air_at| self.now.duration_since(air_at) > SEASON_ENDED_AFTER)
            {
                return Mapped::new(None);
            }
            let Some(newest_raw) = newest_raw else {
                return Mapped::new(Some(latest));
            };
            let local = newest_raw
                .checked_sub(latest)
                .and_then(|gap| raw.checked_sub(gap));
            return Mapped::new(local.filter(|local| (1..=latest).contains(local)));
        }

        Mapped::new((fits_season && within_aired).then_some(raw))
    }
}

/// What the numbering logic needs to know about a stored episode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LedgerRow {
    pub number: i64,
    pub holds_release: bool,
    pub source_episode: Option<i64>,
    /// The release came from the feed being rebased, or from a feed that is not recorded.
    pub from_this_feed: bool,
    pub has_torrent: bool,
}

impl LedgerRow {
    /// A row with no torrent.
    pub const fn new(
        number: i64,
        holds_release: bool,
        source_episode: Option<i64>,
        from_this_feed: bool,
    ) -> Self {
        Self {
            number,
            holds_release,
            source_episode,
            from_this_feed,
            has_torrent: false,
        }
    }
}

/// The offset a release teaches when its air date contradicts the ledger.
///
/// The air date says the release is episode `local`, but that row already holds a release the
/// feed numbered differently. When the feed is consistently higher (it counts an earlier cour
/// or a split entry, as AniList lists them), the offset is the difference.
pub fn inferred_offset(
    raw: i64,
    local: i64,
    holder: Option<&LedgerRow>,
    held_sources: &[i64],
) -> Option<i64> {
    let holder = holder.filter(|holder| holder.holds_release)?;
    if holder.source_episode.is_none_or(|source| source == raw) || !holder.from_this_feed {
        return None;
    }
    let offset = raw.saturating_sub(local);
    let newest_held = held_sources.iter().copied().max().unwrap_or(0);
    (offset > 0 && raw > newest_held).then_some(offset)
}

/// The moves that renumber stored releases so `source_episode - offset` is their row.
///
/// Rows written before a feed's numbering was known (adopted torrents) sit under the raw number
/// the feed used. Once the real offset is known they must move, or the true episode would look
/// already downloaded. Returns `(from_number, to_number)` pairs, or None when nothing changes or
/// a move is unsafe: each target must exist, be an episode that has aired, and be free or itself
/// moving.
pub fn rebase_plan(
    rows: &[LedgerRow],
    offset: i64,
    latest_aired: Option<i64>,
    limit: Option<i64>,
) -> Option<Vec<(i64, i64)>> {
    let by_number: HashMap<i64, &LedgerRow> = rows.iter().map(|row| (row.number, row)).collect();
    let movers: Vec<(&LedgerRow, i64)> = rows
        .iter()
        .filter(|row| row.holds_release && row.from_this_feed)
        .filter_map(|row| Some((row, row.source_episode?)))
        .collect();
    if movers.is_empty() {
        return None;
    }
    let ceiling = limit.unwrap_or_else(|| by_number.keys().copied().max().unwrap_or(0));
    let mut moves = Vec::with_capacity(movers.len());
    for &(row, source) in &movers {
        let target = source.checked_sub(offset)?;
        if !(1..=ceiling).contains(&target) || latest_aired.is_some_and(|latest| target > latest) {
            return None;
        }
        moves.push((row.number, target));
    }
    let moving: HashSet<i64> = movers.iter().map(|(row, _)| row.number).collect();
    for &(_, target) in &moves {
        let occupant = by_number.get(&target)?;
        if !moving.contains(&occupant.number) && (occupant.holds_release || occupant.has_torrent) {
            return None;
        }
    }
    moves
        .iter()
        .any(|(source, target)| source != target)
        .then_some(moves)
}
