//! When a show is hunted, when an episode counts as missed, and which release dates are plausible.
//!
//! Everything here is a pure function of plain data and an explicit `now`. Instants are UTC.

use std::cmp::Reverse;

use jiff::{RoundMode, SignedDuration, SignedDurationRound, Timestamp, Unit, tz::TimeZone};

use crate::enums::EpisodeStatus;

/// An aired episode is hunted for this long; after that it is only picked up by the routine feed read.
pub const HUNT_WINDOW: SignedDuration = SignedDuration::from_hours(24);
/// How long the newest aired episode may stay unfound before it counts as missed.
pub const MISSED_AFTER: SignedDuration = HUNT_WINDOW;
/// A release is mapped to the episode that aired at most this long before it was published.
pub const AIR_DATE_TOLERANCE: SignedDuration = SignedDuration::from_hours(3 * 24);
/// A release may be published this much before the air time AniList gives: those times drift
/// by a day or more from the real release, while an earlier season is off by months.
pub const PUBLISHED_BEFORE_AIR_SLACK: SignedDuration = SignedDuration::from_hours(48);
/// A searched release may predate the show's first episode by this much (feeds post early).
pub const PREMIERE_SLACK: SignedDuration = SignedDuration::from_hours(3 * 24);
/// Nothing can have been published this far in the future.
pub const FUTURE_SLACK: SignedDuration = SignedDuration::from_hours(24);
/// A show whose premiere is further out than this is not matched against releases yet.
pub const PREMIERE_LEAD_TIME: SignedDuration = SignedDuration::from_hours(7 * 24);
/// The usual gap between episodes, used to guess when the previous one aired.
pub const WEEK: SignedDuration = SignedDuration::from_hours(7 * 24);

/// A show's usual release delay is learned from its newest releases on its feed, once at least two of them agree.
pub const DELAY_SAMPLES: usize = 3;
pub const DELAY_AGREEMENT: SignedDuration = SignedDuration::from_hours(1);
/// A release further from its air time than this is not a normal weekly release (a re-upload, a late find) and is not learned from.
pub const DELAY_EARLIEST: SignedDuration = SignedDuration::from_hours(-12);
pub const DELAY_LATEST: SignedDuration = SignedDuration::from_hours(24);

/// A feed that knows no date writes the epoch; nothing this old or older says when a release was published.
pub const UNDATED_UNTIL_YEAR: i16 = 2000;

/// Statuses of an episode that already has, or is getting, a release.
pub const RELEASE_HOLDING: [EpisodeStatus; 2] =
    [EpisodeStatus::Completed, EpisodeStatus::Downloading];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpisodeFacts {
    pub number: i64,
    pub air_at: Option<Timestamp>,
    pub status: EpisodeStatus,
    pub retry_after: Option<Timestamp>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShowFacts {
    pub id: i64,
    pub name: String,
    pub paused: bool,
    pub completed: bool,
    /// Nothing downloaded yet and no feed proven: AniList's air time is still unverified.
    pub in_premiere: bool,
    /// A feed has proven it carries the show.
    pub working: bool,
    pub anilist_status: Option<String>,
    pub next_airing_episode: Option<i64>,
    pub next_airing_at: Option<Timestamp>,
    pub episodes: Vec<EpisodeFacts>,
}

impl ShowFacts {
    pub fn active(&self) -> bool {
        !(self.paused || self.completed)
    }

    pub fn has_download(&self) -> bool {
        self.episodes
            .iter()
            .any(|episode| RELEASE_HOLDING.contains(&episode.status))
    }

    fn anilist_status_is(&self, status: &str) -> bool {
        self.anilist_status.as_deref() == Some(status)
    }

    /// AniList's next episode number, where 0 counts as none.
    fn next_airing_number(&self) -> Option<i64> {
        self.next_airing_episode.filter(|&number| number != 0)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HuntPlan {
    pub hunting: Vec<i64>,
    pub next_start: Option<Timestamp>,
}

/// instant moved forward by duration, stopping at the end of the representable range.
fn later(instant: Timestamp, duration: SignedDuration) -> Timestamp {
    instant.saturating_add(duration).unwrap_or(instant)
}

/// instant moved back by duration, stopping at the start of the representable range.
fn earlier(instant: Timestamp, duration: SignedDuration) -> Timestamp {
    instant.saturating_sub(duration).unwrap_or(instant)
}

/// The early-air tolerance, which is never negative.
fn early_air_tolerance(tolerance_hours: i64) -> SignedDuration {
    SignedDuration::from_secs(tolerance_hours.max(0).saturating_mul(3600))
}

/// The publish date when it is a real one.
fn dated(published_at: Option<Timestamp>) -> Option<Timestamp> {
    published_at.filter(|published_at| {
        published_at.to_zoned_datetime(TimeZone::UTC).year() > UNDATED_UNTIL_YEAR
    })
}

/// Whether a release carries a real publish date.
pub fn is_dated(published_at: Option<Timestamp>) -> bool {
    dated(published_at).is_some()
}

/// When an episode counts as aired for this show.
///
/// The early-air tolerance only covers a show's first episodes, because after the
/// first release AniList's air times are taken as stated.
pub fn air_horizon(show: &ShowFacts, now: Timestamp, tolerance_hours: i64) -> Timestamp {
    if show.in_premiere {
        later(now, early_air_tolerance(tolerance_hours))
    } else {
        now
    }
}

/// The number of the newest episode that has aired, or None when none has.
///
/// Stored air times decide. Without any, AniList's pointer does: its next episode
/// has aired once its air time has passed, otherwise the one before it has. A show
/// that finished airing with no air times at all (an old show AniList has no schedule
/// for) has aired every episode.
pub fn latest_aired(show: &ShowFacts, horizon: Timestamp) -> Option<i64> {
    let known = show
        .episodes
        .iter()
        .filter(|episode| episode.air_at.is_some_and(|air_at| air_at <= horizon))
        .map(|episode| episode.number)
        .max();
    if known.is_some() {
        return known;
    }
    if let (Some(next), Some(next_at)) = (show.next_airing_number(), show.next_airing_at) {
        return if next_at <= horizon {
            Some(next)
        } else {
            Some(next.saturating_sub(1).max(0)).filter(|&number| number != 0)
        };
    }
    let no_air_times = show.episodes.iter().all(|episode| episode.air_at.is_none());
    if show.anilist_status_is("FINISHED") && !show.episodes.is_empty() && no_air_times {
        return show.episodes.iter().map(|episode| episode.number).max();
    }
    None
}

fn air_time_of(show: &ShowFacts, number: i64) -> Option<Timestamp> {
    let stored = show.episodes.iter().find_map(|episode| {
        (episode.number == number)
            .then_some(episode.air_at)
            .flatten()
    });
    if stored.is_some() {
        return stored;
    }
    let (next_at, next) = show.next_airing_at.zip(show.next_airing_number())?;
    if number == next {
        Some(next_at)
    } else if number.checked_add(1) == Some(next) {
        // The previous episode's own time is unknown; a week before the next is the usual guess.
        Some(earlier(next_at, WEEK))
    } else {
        None
    }
}

fn still_wanted(show: &ShowFacts, number: i64) -> bool {
    match show
        .episodes
        .iter()
        .find(|episode| episode.number == number)
    {
        Some(episode) => episode.status == EpisodeStatus::Wanted,
        None => !show
            .episodes
            .iter()
            .any(|episode| episode.number >= number && episode.status == EpisodeStatus::Completed),
    }
}

/// Whether an aired episode is past the day it is hunted for.
pub fn past_hunting_window(air_at: Option<Timestamp>, now: Timestamp) -> bool {
    air_at.is_some_and(|air_at| now.duration_since(air_at) > HUNT_WINDOW)
}

/// Whether the show's newest aired episode is still wanted and aired within the hunting window.
pub fn is_hunting(show: &ShowFacts, now: Timestamp, tolerance_hours: i64) -> bool {
    if !show.active() {
        return false;
    }
    let horizon = air_horizon(show, now, tolerance_hours);
    let Some(number) = latest_aired(show, horizon).filter(|&number| number != 0) else {
        return false;
    };
    if !still_wanted(show, number) {
        return false;
    }
    let Some(air_at) = air_time_of(show, number) else {
        return false;
    };
    // A guessed time may lie ahead of now (a hiatus pushed the next episode out): that is not "just aired".
    if air_at > later(now, early_air_tolerance(tolerance_hours)) {
        return false;
    }
    !past_hunting_window(Some(air_at), now)
}

/// The next moment the show will start being hunted, or None when nothing is scheduled.
pub fn next_hunt_start(
    show: &ShowFacts,
    now: Timestamp,
    tolerance_hours: i64,
) -> Option<Timestamp> {
    if !show.active() {
        return None;
    }
    let lead = if show.in_premiere {
        early_air_tolerance(tolerance_hours)
    } else {
        SignedDuration::ZERO
    };
    let upcoming = |air_at: Timestamp| Some(earlier(air_at, lead)).filter(|&start| start > now);
    show.episodes
        .iter()
        .filter_map(|episode| episode.air_at.and_then(upcoming))
        .min()
        .or_else(|| show.next_airing_at.and_then(upcoming))
}

/// The shows to hunt now and when the next one will need hunting.
pub fn hunt_plan(shows: &[ShowFacts], now: Timestamp, tolerance_hours: i64) -> HuntPlan {
    let mut hunting = Vec::new();
    let mut next_start = None;
    for show in shows {
        if is_hunting(show, now, tolerance_hours) {
            hunting.push(show.id);
        } else if let Some(start) = next_hunt_start(show, now, tolerance_hours) {
            next_start = Some(next_start.map_or(start, |current: Timestamp| current.min(start)));
        }
    }
    HuntPlan {
        hunting,
        next_start,
    }
}

/// True while a show has not started airing and nothing has been downloaded.
///
/// A show AniList calls releasing or finished that has no air time at all (typically a movie)
/// counts as released.
pub fn is_unreleased(show: &ShowFacts, now: Timestamp) -> bool {
    if show.next_airing_at.is_none()
        && (show.anilist_status_is("RELEASING") || show.anilist_status_is("FINISHED"))
    {
        return false;
    }
    if show.has_download() {
        return false;
    }
    matches!(show.next_airing_episode, None | Some(1))
        && show.next_airing_at.is_none_or(|next_at| next_at > now)
}

/// True while AniList says a show has not started airing and its premiere is unknown or far off.
///
/// Nothing a feed carries can be this show's yet, only an earlier season under the same name,
/// so such a show is not matched against releases.
pub fn premiere_out_of_reach(show: &ShowFacts, now: Timestamp) -> bool {
    show.anilist_status_is("NOT_YET_RELEASED")
        && is_unreleased(show, now)
        && show
            .next_airing_at
            .is_none_or(|next_at| next_at > later(now, PREMIERE_LEAD_TIME))
}

/// A show that aired, was hunted for a day, and is now left to the routine feed read.
///
/// The newest stored episode that has aired decides, because AniList's next air time
/// moves on to the following episode as soon as one airs. A finished show with no air
/// time at all (a movie) was never hunted, so it waits from the start.
pub fn waiting_for_release(show: &ShowFacts, now: Timestamp) -> bool {
    if !show.active() || show.working {
        return false;
    }
    let newest = show
        .episodes
        .iter()
        .filter(|episode| episode.air_at.is_some_and(|air_at| air_at <= now))
        // The first of equally numbered episodes decides.
        .reduce(|newest, episode| {
            if episode.number > newest.number {
                episode
            } else {
                newest
            }
        });
    if let Some(newest) = newest {
        return matches!(newest.status, EpisodeStatus::Wanted | EpisodeStatus::Missed)
            && past_hunting_window(newest.air_at, now);
    }
    if show.next_airing_at.is_some() {
        return past_hunting_window(show.next_airing_at, now);
    }
    show.anilist_status_is("FINISHED")
}

/// The WANTED episodes the feeds can no longer deliver.
///
/// A wanted episode older than the newest aired one, or followed by a downloaded one,
/// fell out of the feeds. The newest aired one is given a day to show up first. An
/// episode a stored feed item can still supply (`covered`) is never missed. Missed
/// episodes stay grabbable: they come back through a later feed read or a search.
pub fn missed_numbers(
    episodes: &[EpisodeFacts],
    now: Timestamp,
    horizon: Timestamp,
    newest_aired: Option<i64>,
    covered: &[i64],
) -> Vec<i64> {
    let mut result = Vec::new();
    for episode in episodes {
        if episode.status != EpisodeStatus::Wanted || covered.contains(&episode.number) {
            continue;
        }
        if episode
            .retry_after
            .is_some_and(|retry_after| retry_after > now)
        {
            continue;
        }
        let has_aired = match episode.air_at {
            Some(air_at) => air_at <= horizon,
            None => newest_aired.is_some_and(|newest| episode.number <= newest),
        };
        if !has_aired {
            continue;
        }
        let older_than_newest = newest_aired.is_some_and(|newest| episode.number < newest);
        let followed_by_download = episodes
            .iter()
            .any(|other| other.number > episode.number && RELEASE_HOLDING.contains(&other.status));
        let newest_and_overdue = newest_aired == Some(episode.number)
            && episode
                .air_at
                .is_some_and(|air_at| now.duration_since(air_at) > MISSED_AFTER);
        if older_than_newest || followed_by_download || newest_and_overdue {
            result.push(episode.number);
        }
    }
    result
}

/// The episode a release published at `published_at` can be, going by air times alone.
///
/// It can only be an episode that has already aired (allowing the early-air tolerance
/// for feeds that post before AniList's time) and aired within `AIR_DATE_TOLERANCE`. The
/// one that aired closest before the release wins.
pub fn date_mapped_episode(
    air_times: &[(i64, Timestamp)],
    published_at: Timestamp,
    target_count: i64,
    tolerance_hours: i64,
) -> Option<i64> {
    let published_at = dated(Some(published_at))?;
    let aired_by = later(published_at, early_air_tolerance(tolerance_hours));
    air_times
        .iter()
        .filter(|&&(number, air_at)| {
            (1..=target_count).contains(&number)
                && air_at <= aired_by
                && published_at.duration_since(air_at) <= AIR_DATE_TOLERANCE
        })
        .map(|&(number, air_at)| (air_at, number))
        .max()
        .map(|(_, number)| number)
}

/// Whether a dated release was published before its episode could have aired.
///
/// Season tags and episode numbers differ from group to group, so a release of an
/// earlier season can carry a number that fits this one; its publish date cannot.
/// `reference` is the episode's air time, or the show's first one when the episode
/// has none. Undated releases and shows with nothing scheduled cannot be judged.
pub fn published_before_air(
    published_at: Option<Timestamp>,
    reference: Option<Timestamp>,
    tolerance_hours: i64,
) -> bool {
    let (Some(reference), Some(published_at)) = (reference, dated(published_at)) else {
        return false;
    };
    let slack = PUBLISHED_BEFORE_AIR_SLACK.max(early_air_tolerance(tolerance_hours));
    published_at < earlier(reference, slack)
}

/// Whether a searched release's publish date could belong to a show that premiered at `first_air`.
///
/// A search reaches into history, where the show's earlier seasons live and their releases can
/// carry a season or episode number that fits. Undated releases are dropped, since a search
/// cannot vouch for them. Without a premiere time only the future check applies.
pub fn plausible_for_show(
    published_at: Option<Timestamp>,
    first_air: Option<Timestamp>,
    now: Timestamp,
) -> bool {
    let Some(published_at) = dated(published_at) else {
        return false;
    };
    if published_at > later(now, FUTURE_SLACK) {
        return false;
    }
    first_air.is_none_or(|first_air| published_at >= earlier(first_air, PREMIERE_SLACK))
}

/// How long after its air time a show's release usually comes out, from (air time, published) pairs, one per episode.
///
/// The newest `DELAY_SAMPLES` releases are looked at, and the largest group of them within `DELAY_AGREEMENT`
/// of each other decides; None until at least two agree.
pub fn release_delay(samples: &[(Timestamp, Timestamp)]) -> Option<SignedDuration> {
    let mut newest = samples.to_vec();
    newest.sort_by_key(|&(air, _)| Reverse(air));
    let mut delays: Vec<SignedDuration> = newest
        .iter()
        .map(|&(air, published)| published.duration_since(air))
        .filter(|delay| (DELAY_EARLIEST..=DELAY_LATEST).contains(delay))
        .take(DELAY_SAMPLES)
        .collect();
    delays.sort();
    let mut best: &[SignedDuration] = &[];
    for (start, &low) in delays.iter().enumerate() {
        let from_low = &delays[start..];
        let within =
            from_low.partition_point(|&delay| delay.saturating_sub(low) <= DELAY_AGREEMENT);
        if within > best.len() {
            best = &from_low[..within];
        }
    }
    (best.len() >= 2).then(|| median(best))
}

/// The median of a sorted, non-empty slice. Halving the sum of the two middle values rounds
/// to the nearest microsecond, ties to even.
fn median(sorted: &[SignedDuration]) -> SignedDuration {
    let middle = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        return sorted[middle];
    }
    let half = sorted[middle - 1].saturating_add(sorted[middle]) / 2;
    let microseconds = SignedDurationRound::new()
        .smallest(Unit::Microsecond)
        .mode(RoundMode::HalfEven);
    half.round(microseconds).unwrap_or(half)
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    fn delay_of(microseconds: [i64; 2]) -> Option<SignedDuration> {
        let air = Timestamp::UNIX_EPOCH;
        let samples = microseconds.map(|micros| {
            let published = Timestamp::from_microsecond(micros).unwrap();
            (air, published)
        });
        release_delay(&samples)
    }

    #[test]
    fn median_of_an_even_count_rounds_half_to_even_microsecond() {
        assert_eq!(delay_of([0, 1]), Some(SignedDuration::from_micros(0)));
        assert_eq!(delay_of([1, 2]), Some(SignedDuration::from_micros(2)));
        assert_eq!(delay_of([-1, -2]), Some(SignedDuration::from_micros(-2)));
    }
}
