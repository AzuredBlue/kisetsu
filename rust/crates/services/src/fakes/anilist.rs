use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet, HashSet};

use jiff::{SignedDuration, Timestamp};
use kisetsu_clients::anilist::{
    AiringEpisode, AniListError, ListEntry, ListStatus, MediaStatus, Schedules, Season,
};

use super::clock::FakeClock;
use crate::context::{AniListSource, Clock};

/// A question the fake AniList was asked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AniListCall {
    UserExists(String),
    FetchUserList(String),
    FetchUserSeasonalAnime(String),
    FetchMediaAiringSchedules(Vec<i64>),
    FetchUpcomingAiringSchedules(Vec<i64>),
}

/// An entry of the user's list: a releasing show the user is watching this fall.
pub fn anilist_show(anilist_id: i64, title: &str) -> ListEntry {
    ListEntry {
        anilist_id,
        display_name: title.to_owned(),
        title_romaji: title.to_owned(),
        title_english: title.to_owned(),
        aliases: vec![title.to_owned()],
        list_status: Some(ListStatus::Current),
        progress: None,
        status: MediaStatus::Releasing,
        total_episodes: None,
        next_airing_episode: None,
        next_airing_at: None,
        season: Some(Season::Fall),
        season_year: Some(2026),
        cover_image: String::new(),
        cover_color: String::new(),
        banner_image: String::new(),
    }
}

/// AniList with the public surface of `AniListSource`.
#[derive(Debug)]
pub struct FakeAniList {
    clock: FakeClock,
    pub shows: Vec<ListEntry>,
    pub schedules: BTreeMap<i64, Vec<AiringEpisode>>,
    pub users: BTreeSet<String>,
    /// Every question fails with this.
    pub error: Option<AniListError>,
    pub last_sync_at: Option<Timestamp>,
    pub due: bool,
    user_list: RefCell<Vec<ListEntry>>,
    user_list_at: RefCell<Option<Timestamp>>,
    calls: RefCell<Vec<AniListCall>>,
}

impl FakeAniList {
    pub fn new(clock: FakeClock) -> Self {
        Self {
            clock,
            shows: Vec::new(),
            schedules: BTreeMap::new(),
            users: BTreeSet::from(["user".to_owned()]),
            error: None,
            last_sync_at: None,
            due: true,
            user_list: RefCell::new(Vec::new()),
            user_list_at: RefCell::new(None),
            calls: RefCell::new(Vec::new()),
        }
    }

    /// Gives a show `count` episodes airing every `every` from `first_air`.
    pub fn set_schedule(
        &mut self,
        anilist_id: i64,
        first_air: Timestamp,
        count: i64,
        every: SignedDuration,
    ) {
        let episodes = (1..=count)
            .map(|episode| AiringEpisode {
                episode,
                airing_at: first_air
                    .checked_add(every * i32::try_from(episode - 1).unwrap_or(i32::MAX))
                    .unwrap_or(first_air),
            })
            .collect();
        self.schedules.insert(anilist_id, episodes);
    }

    /// The questions asked, in order.
    pub fn calls(&self) -> Vec<AniListCall> {
        self.calls.borrow().clone()
    }

    /// What the last fetch of the user's list returned.
    pub fn user_list(&self) -> Vec<ListEntry> {
        self.user_list.borrow().clone()
    }

    /// When the user's list was last fetched.
    pub fn user_list_at(&self) -> Option<Timestamp> {
        *self.user_list_at.borrow()
    }

    fn ask(&self, call: AniListCall) -> Result<(), AniListError> {
        self.calls.borrow_mut().push(call);
        match &self.error {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }

    fn fetched_list(&self) -> Vec<ListEntry> {
        *self.user_list.borrow_mut() = self.shows.clone();
        *self.user_list_at.borrow_mut() = Some(self.clock.now());
        self.shows.clone()
    }
}

impl AniListSource for FakeAniList {
    fn user_exists(&self, username: &str) -> Result<bool, AniListError> {
        self.ask(AniListCall::UserExists(username.to_owned()))?;
        Ok(self.users.contains(username))
    }

    fn fetch_user_list(
        &self,
        username: &str,
        _reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        self.ask(AniListCall::FetchUserList(username.to_owned()))?;
        Ok(self.fetched_list())
    }

    fn fetch_user_seasonal_anime(
        &self,
        username: &str,
        _monitored: &HashSet<i64>,
        _reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        self.ask(AniListCall::FetchUserSeasonalAnime(username.to_owned()))?;
        Ok(self.fetched_list())
    }

    fn fetch_media_airing_schedules(&self, media_ids: &[i64]) -> Result<Schedules, AniListError> {
        self.ask(AniListCall::FetchMediaAiringSchedules(media_ids.to_vec()))?;
        Ok(media_ids
            .iter()
            .filter_map(|id| Some((*id, self.schedules.get(id)?.clone())))
            .collect())
    }

    fn fetch_upcoming_airing_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> Result<Schedules, AniListError> {
        self.ask(AniListCall::FetchUpcomingAiringSchedules(
            media_ids.to_vec(),
        ))?;
        Ok(media_ids
            .iter()
            .map(|id| {
                let upcoming = self
                    .schedules
                    .get(id)
                    .map(|episodes| {
                        episodes
                            .iter()
                            .filter(|episode| episode.airing_at >= since)
                            .copied()
                            .collect()
                    })
                    .unwrap_or_default();
                (*id, upcoming)
            })
            .collect())
    }

    fn seconds_since_last_sync(&self, now: Timestamp) -> Option<f64> {
        self.last_sync_at
            .map(|last| now.duration_since(last).as_secs_f64())
    }

    fn is_sync_due(&self, _min_age_seconds: f64, _now: Timestamp) -> bool {
        self.due
    }
}
