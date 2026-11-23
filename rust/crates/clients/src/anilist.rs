//! The AniList GraphQL client: the user's list and the air times of shows, spaced by a rate
//! governor that every request of the client draws on.

mod error;
mod governor;
mod list;
mod transport;

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use jiff::Timestamp;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::fetch::{Clock, SystemClock};
use crate::locked;
use crate::traffic::{self, Outcome};

pub use error::{AiringEpisode, AniListError, Deferral, DeferralKind, Schedules};
pub use governor::{RateGovernor, RateHeaders};
pub use list::{
    ListEntry, ListStatus, MediaStatus, Season, SeasonOfYear, current_and_next_season,
    select_seasonal,
};
pub use transport::{Answer, Transport, TransportError, UreqTransport};

use list::{Collection, SavedList};

pub const GRAPHQL_URL: &str = "https://graphql.anilist.co";
const HOST: &str = "graphql.anilist.co";

/// The most pages of air times asked for per show.
const MAX_SCHEDULE_PAGES: usize = 20;
/// How long one multi-request pass may spend asking AniList before it hands the rest to the next pass.
const PASS_BUDGET: f64 = 30.0;
const PASS_RESUME: f64 = 30.0;
/// Shows asked about in one batched schedule query.
const UPCOMING_CHUNK: usize = 40;
/// Pages of air times one request asks for, each under its own alias. AniList counts every alias
/// against its rate limit, so a request asks only for pages known to be wanted.
const PAGES_PER_REQUEST: usize = 8;
/// The longest part of an error body that goes into a message, in characters.
const ERROR_BODY_CHARS: usize = 200;
const RETRY_BACKOFF_START: f64 = 2.0;
const DEFAULT_RETRIES: u32 = 3;

pub const USER_LIST_QUERY: &str = r"
query ($userName: String) {
  MediaListCollection(userName: $userName, type: ANIME, status_in: [CURRENT, PLANNING, COMPLETED, PAUSED, DROPPED, REPEATING]) {
    lists {
      entries {
        status
        progress
        media {
          id
          title {
            romaji
            english
            native
            userPreferred
          }
          synonyms
          status
          episodes
          nextAiringEpisode {
            airingAt
            episode
          }
          season
          seasonYear
          coverImage {
            extraLarge
            large
            color
          }
          bannerImage
        }
      }
    }
  }
}
";

/// What a request is for; each kind is counted on its own (see [`traffic`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Query {
    UserList,
    FullSchedule,
    UpcomingSchedule,
}

impl Query {
    fn kind(self) -> &'static str {
        match self {
            Self::UserList => "AniList list sync",
            Self::FullSchedule => "AniList full schedule",
            Self::UpcomingSchedule => "AniList upcoming schedule",
        }
    }
}

/// One query for several pages of air times: `pages` holds (filter, page) pairs, the i-th
/// answered as `p{i}`. The filters are built from integers only, so they are written into the
/// query itself.
pub fn schedule_query(pages: &[(String, usize)]) -> String {
    let blocks: Vec<String> = pages
        .iter()
        .enumerate()
        .map(|(i, (filter, page))| {
            format!(
                "  p{i}: Page(page: {page}, perPage: 50) {{ pageInfo {{ hasNextPage }} \
                 airingSchedules({filter}, sort: TIME) {{ mediaId episode airingAt }} }}"
            )
        })
        .collect();
    format!("query {{\n{}\n}}", blocks.join("\n"))
}

/// What AniList answered to a query: its `data`, and the `errors` that came with it when partial
/// answers were accepted.
struct Payload {
    data: Map<String, Value>,
    errors: Option<Value>,
}

/// What an answer means for the request.
enum Verdict {
    Answered(Payload),
    /// The request failed in a way that may pass, for this reason.
    Retry(String),
}

/// The user's list as last fetched.
#[derive(Debug, Default)]
struct Listing {
    /// Empty once the list is saved to disk: it is read back from there when asked for.
    entries: Vec<ListEntry>,
    /// Whether the list is the one saved to disk.
    saved: bool,
    fetched_at: Option<Timestamp>,
    /// Whose list `entries` is, when it was fetched in this run.
    of: Option<String>,
}

#[derive(Debug, Default)]
struct State {
    last_sync_at: Option<Timestamp>,
    listing: Listing,
}

/// Asks AniList for the user's list and for air times.
pub struct AniListClient {
    transport: Box<dyn Transport>,
    clock: Arc<dyn Clock>,
    governor: Mutex<RateGovernor>,
    cache_path: Option<PathBuf>,
    state: Mutex<State>,
}

impl AniListClient {
    /// A client for the real AniList, keeping the list at `cache_path` between runs.
    pub fn new(cache_path: Option<PathBuf>) -> Self {
        Self::with_parts(
            Box::new(UreqTransport::new(Duration::from_secs(15))),
            Arc::<SystemClock>::default(),
            cache_path,
        )
    }

    pub fn with_parts(
        transport: Box<dyn Transport>,
        clock: Arc<dyn Clock>,
        cache_path: Option<PathBuf>,
    ) -> Self {
        let client = Self {
            transport,
            clock,
            governor: Mutex::new(RateGovernor::default()),
            cache_path,
            state: Mutex::new(State::default()),
        };
        client.load_saved_list();
        client
    }

    // -- the list on disk --

    /// Picks up the list kept on disk, so a restart does not leave the add-show search with nothing.
    fn load_saved_list(&self) {
        let Some(path) = &self.cache_path else { return };
        let text = match std::fs::read_to_string(path) {
            Ok(text) => text,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
            Err(error) => {
                tracing::warn!("Ignoring the saved AniList list: {error}");
                return;
            }
        };
        match serde_json::from_str::<SavedList>(&text) {
            Ok(saved) => {
                locked(&self.state).listing = Listing {
                    entries: Vec::new(),
                    saved: true,
                    fetched_at: Some(saved.fetched_at),
                    of: None,
                };
            }
            Err(error) => tracing::warn!("Ignoring the saved AniList list: {error}"),
        }
    }

    /// Saves the list to disk and lets go of it in memory; a list that could not be saved is
    /// kept.
    fn save_list(&self, listing: &mut Listing) {
        let (Some(path), Some(fetched_at)) = (&self.cache_path, listing.fetched_at) else {
            return;
        };
        let saved = SavedList {
            fetched_at,
            entries: std::mem::take(&mut listing.entries),
        };
        let written = serde_json::to_string(&saved)
            .map_err(|error| error.to_string())
            .and_then(|text| std::fs::write(path, text).map_err(|error| error.to_string()));
        match written {
            Ok(()) => listing.saved = true,
            Err(error) => {
                tracing::warn!("Could not save the AniList list: {error}");
                listing.entries = saved.entries;
            }
        }
    }

    /// The entries of the list saved to disk; none when it cannot be read.
    fn saved_entries(&self) -> Vec<ListEntry> {
        let Some(path) = &self.cache_path else {
            return Vec::new();
        };
        let read = std::fs::read_to_string(path)
            .map_err(|error| error.to_string())
            .and_then(|text| {
                serde_json::from_str::<SavedList>(&text).map_err(|error| error.to_string())
            });
        match read {
            Ok(saved) => saved.entries,
            Err(error) => {
                tracing::warn!("Could not read the saved AniList list: {error}");
                Vec::new()
            }
        }
    }

    // -- the state of the sync --

    /// The list as last fetched or loaded from disk.
    pub fn user_list(&self) -> Vec<ListEntry> {
        let state = locked(&self.state);
        if state.listing.saved {
            drop(state);
            return self.saved_entries();
        }
        state.listing.entries.clone()
    }

    /// When `user_list` was fetched.
    pub fn user_list_at(&self) -> Option<Timestamp> {
        locked(&self.state).listing.fetched_at
    }

    pub fn last_sync_at(&self) -> Option<Timestamp> {
        locked(&self.state).last_sync_at
    }

    /// When AniList may be asked again, and in how many seconds; `None` when it is not blocked.
    pub fn rate_limit_status(&self) -> RateLimitStatus {
        let governor = locked(&self.governor);
        let now = self.clock.now();
        RateLimitStatus {
            blocked_until: governor.blocked_until_epoch(now, epoch_seconds(self.clock.wall())),
            retry_in: Some(governor.retry_in(now)).filter(|&wait| wait > 0.0),
        }
    }

    // -- requests --

    /// Runs a GraphQL query, spaced by the governor.
    ///
    /// A 429 is never waited out here: the governor remembers when AniList may be asked again and
    /// the caller gets a deferral at once. A server error, a lost connection or an unreadable
    /// answer is retried with backoff; when every retry fails, requests pause for a growing while.
    /// Any other HTTP error is the request's own fault and is not retried.
    ///
    /// With `allow_partial` an answer that carries both `data` and `errors` returns the usable
    /// part, so one bad alias cannot discard the whole batch. `cost` is how much of the rate limit
    /// the query uses: AniList counts every page asked for under an alias.
    fn post_query(
        &self,
        query: &str,
        variables: &Value,
        max_retries: u32,
        allow_partial: bool,
        kind: Query,
        cost: usize,
    ) -> Result<Payload, AniListError> {
        let body = json!({"query": query, "variables": variables}).to_string();
        let mut backoff = RETRY_BACKOFF_START;
        let mut problem = String::from("AniList did not answer.");
        for attempt in 0..max_retries {
            let wait = locked(&self.governor)
                .acquire(self.clock.now(), cost)
                .map_err(AniListError::Deferred)?;
            if wait > 0.0 {
                self.clock.sleep(wait);
            }
            match self.transport.post_json(GRAPHQL_URL, body.as_bytes()) {
                Err(error) => {
                    traffic::record(kind.kind(), HOST, Outcome::Error);
                    problem = format!("AniList network connection failed: {error}");
                }
                Ok(answer) => {
                    traffic::record(kind.kind(), HOST, Outcome::of_status(answer.status));
                    match self.read_answer(&answer, allow_partial)? {
                        Verdict::Answered(payload) => return Ok(payload),
                        Verdict::Retry(reason) => problem = reason,
                    }
                }
            }
            if attempt + 1 < max_retries {
                self.clock.sleep(backoff);
                backoff *= 2.0;
            }
        }
        let wait = locked(&self.governor).note_failure(self.clock.now());
        Err(AniListError::Deferred(Deferral::new(
            DeferralKind::Unavailable,
            format!("{problem} Pausing AniList requests for {wait:.0}s."),
            wait,
        )))
    }

    /// Judges an answer: its payload, or why the request is to be tried again; an error when asking
    /// again would not help.
    fn read_answer(&self, answer: &Answer, allow_partial: bool) -> Result<Verdict, AniListError> {
        let headers = answer.rate_headers();
        let now = self.clock.now();
        let body_start = || -> String {
            String::from_utf8_lossy(&answer.body)
                .chars()
                .take(ERROR_BODY_CHARS)
                .collect()
        };
        let mut governor = locked(&self.governor);
        governor.note_response(now, &headers);
        match answer.status {
            429 => {
                let wait =
                    governor.note_rate_limited(now, epoch_seconds(self.clock.wall()), &headers);
                tracing::warn!("AniList rate limited (429). Not asking again for {wait:.0}s.");
                Err(AniListError::Deferred(Deferral::new(
                    DeferralKind::RateLimited,
                    format!("AniList is rate limiting this client; retry in {wait:.0}s."),
                    wait,
                )))
            }
            404 => Err(AniListError::NotFound { body: body_start() }),
            status @ 400..=499 => Err(AniListError::Rejected {
                status,
                body: body_start(),
            }),
            status if status >= 300 => Ok(Verdict::Retry(format!(
                "AniList HTTP error: {status} - {}",
                body_start()
            ))),
            _ => {
                let Ok(Value::Object(mut answered)) = serde_json::from_slice(&answer.body) else {
                    return Ok(Verdict::Retry(String::from(
                        "AniList returned an unreadable answer.",
                    )));
                };
                governor.note_success();
                let data = match answered.remove("data") {
                    Some(Value::Object(data)) => data,
                    _ => Map::new(),
                };
                match answered.remove("errors") {
                    None => Ok(Verdict::Answered(Payload { data, errors: None })),
                    Some(errors) if allow_partial => Ok(Verdict::Answered(Payload {
                        data,
                        errors: Some(errors),
                    })),
                    Some(errors) => Err(AniListError::GraphQl(errors.to_string())),
                }
            }
        }
    }

    // -- the user's list --

    /// Whether AniList knows this user name, told by asking for the user's list.
    ///
    /// The list is kept, so the sync that follows setup takes it instead of asking AniList again
    /// (`reuse_within`). One attempt only: the retry backoff would make a typo take several
    /// seconds to report.
    pub fn user_exists(&self, username: &str) -> Result<bool, AniListError> {
        match self.fetch_list(username, 1) {
            Ok(_) => Ok(true),
            Err(AniListError::NotFound { .. }) => Ok(false),
            Err(error) => Err(error),
        }
    }

    /// Every entry on the user's list in the statuses Kisetsu reads, unfiltered.
    ///
    /// The answer is kept (see [`Self::user_list`]) so the add-show search can look through it
    /// without asking AniList again. With `reuse_within`, a list of this user fetched less than
    /// that many seconds ago is taken instead of a new request.
    pub fn fetch_user_list(
        &self,
        username: &str,
        reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        let username = username.trim();
        if username.is_empty() {
            return Ok(Vec::new());
        }
        let fresh = {
            let state = locked(&self.state);
            let listing = &state.listing;
            reuse_within.is_some_and(|within| {
                listing.of.as_deref() == Some(username)
                    && listing.fetched_at.is_some_and(|at| {
                        self.clock.wall().duration_since(at).as_secs_f64() < within
                    })
            })
        };
        if fresh {
            return Ok(self.user_list());
        }
        self.fetch_list(username, DEFAULT_RETRIES)
    }

    fn fetch_list(&self, username: &str, max_retries: u32) -> Result<Vec<ListEntry>, AniListError> {
        let username = username.trim();
        let payload = self.post_query(
            USER_LIST_QUERY,
            &json!({"userName": username}),
            max_retries,
            false,
            Query::UserList,
            1,
        )?;
        let collection = match payload.data.get("MediaListCollection") {
            None | Some(Value::Null) => Collection::default(),
            Some(value) => Collection::deserialize(value)
                .map_err(|error| AniListError::Unexpected(error.to_string()))?,
        };
        let now = self.clock.wall();
        let entries = collection.into_entries();
        let mut listing = Listing {
            entries: entries.clone(),
            saved: false,
            fetched_at: Some(now),
            of: Some(username.to_owned()),
        };
        self.save_list(&mut listing);
        let mut state = locked(&self.state);
        state.last_sync_at = Some(now);
        state.listing = listing;
        Ok(entries)
    }

    /// The user's entries Kisetsu acts on (see [`select_seasonal`]), each with its own list status.
    pub fn fetch_user_seasonal_anime(
        &self,
        username: &str,
        monitored: &HashSet<i64>,
        reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        let entries = self.fetch_user_list(username, reuse_within)?;
        let seasons = current_and_next_season(self.clock.wall());
        Ok(select_seasonal(&entries, monitored, seasons))
    }

    // -- air times --

    /// Every page of air times each filter selects, several pages in each request.
    ///
    /// A request asks for the next page of up to [`PAGES_PER_REQUEST`] filters, each under its own
    /// alias; a filter's next page is asked for only once AniList says it has one. The result holds
    /// each filter's nodes, `None` for a filter AniList answered with an error. A deferral carries
    /// the filters finished so far.
    fn schedule_pages(
        &self,
        filters: &[String],
        kind: Query,
    ) -> Result<Vec<Option<Vec<Node>>>, Interrupted> {
        let mut nodes: Vec<Option<Vec<Node>>> = vec![Some(Vec::new()); filters.len()];
        let mut next_page = vec![1usize; filters.len()];
        let mut remaining: Vec<usize> = (0..filters.len()).collect();
        let deadline = self.clock.now() + PASS_BUDGET;
        let finished = |nodes: &[Option<Vec<Node>>], remaining: &[usize]| -> Finished {
            nodes
                .iter()
                .enumerate()
                .filter(|(key, _)| !remaining.contains(key))
                .map(|(key, found)| (key, found.clone()))
                .collect()
        };
        while !remaining.is_empty() {
            if self.clock.now() >= deadline {
                return Err(Interrupted::Deferred {
                    deferral: Deferral::new(
                        DeferralKind::RateLimited,
                        String::from("AniList request budget for this pass is used up."),
                        PASS_RESUME,
                    ),
                    finished: finished(&nodes, &remaining),
                });
            }
            let asked: Vec<(usize, usize)> = remaining
                .iter()
                .take(PAGES_PER_REQUEST)
                .map(|&key| (key, next_page[key]))
                .collect();
            let pages: Vec<(String, usize)> = asked
                .iter()
                .map(|&(key, page)| (filters[key].clone(), page))
                .collect();
            let payload = match self.post_query(
                &schedule_query(&pages),
                &json!({}),
                DEFAULT_RETRIES,
                true,
                kind,
                pages.len(),
            ) {
                Ok(payload) => payload,
                Err(AniListError::Deferred(deferral)) => {
                    return Err(Interrupted::Deferred {
                        deferral,
                        finished: finished(&nodes, &remaining),
                    });
                }
                Err(other) => return Err(Interrupted::Failed(other)),
            };
            let mut ended: HashSet<usize> = HashSet::new();
            for (i, &(key, page)) in asked.iter().enumerate() {
                if nodes[key].is_none() || ended.contains(&key) {
                    continue;
                }
                let block = payload
                    .data
                    .get(&format!("p{i}"))
                    .and_then(|block| Block::deserialize(block).ok());
                let Some(block) = block else {
                    tracing::warn!(
                        "AniList answered part of a schedule query with an error: {}",
                        payload
                            .errors
                            .as_ref()
                            .map(ToString::to_string)
                            .unwrap_or_default()
                    );
                    nodes[key] = None;
                    continue;
                };
                if let Some(found) = &mut nodes[key] {
                    found.extend(block.airing_schedules.unwrap_or_default());
                }
                next_page[key] = page + 1;
                // Only hasNextPage is reliable: AniList's page total is a placeholder.
                if !block
                    .page_info
                    .and_then(|info| info.has_next_page)
                    .unwrap_or(false)
                {
                    ended.insert(key);
                }
            }
            remaining.retain(|key| {
                nodes[*key].is_some()
                    && !ended.contains(key)
                    && next_page[*key] <= MAX_SCHEDULE_PAGES
            });
        }
        Ok(nodes)
    }

    /// Complete airing schedules, several shows in each request.
    ///
    /// A show AniList answers with an error is logged and left out, so it does not cost the others
    /// their schedules. When AniList defers the rest (a rate limit, a pause after repeated
    /// failures, or this pass's time budget), the shows already fetched travel on the deferral as
    /// `partial` and the rest are fetched on a later pass.
    pub fn fetch_media_airing_schedules(
        &self,
        media_ids: &[i64],
    ) -> Result<Schedules, AniListError> {
        let ids = unique(media_ids);
        let filters: Vec<String> = ids.iter().map(|id| format!("mediaId: {id}")).collect();
        match self.schedule_pages(&filters, Query::FullSchedule) {
            Ok(found) => {
                for (id, nodes) in ids.iter().zip(&found) {
                    if nodes.is_none() {
                        tracing::warn!("Could not fetch airing schedule for media {id}.");
                    }
                }
                Ok(full_schedules(ids.iter().copied().zip(found)))
            }
            Err(interrupted) => Err(interrupted.into_error(|finished| {
                full_schedules(finished.into_iter().map(|(key, nodes)| (ids[key], nodes)))
            })),
        }
    }

    /// Air times from `since` on for many shows at once, usually in a single request.
    ///
    /// A refresh of shows whose past episodes are already dated only needs what is still to come.
    /// Every id asked about is in the result (an empty list when nothing is scheduled), so a show
    /// that simply has no more episodes is not mistaken for one that failed. A chunk AniList
    /// rejects is fetched show by show instead. As with [`Self::fetch_media_airing_schedules`], a
    /// deferral carries what was already fetched.
    pub fn fetch_upcoming_airing_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> Result<Schedules, AniListError> {
        let ids = unique(media_ids);
        let chunks: Vec<&[i64]> = ids.chunks(UPCOMING_CHUNK).collect();
        let after = since.as_second();
        let filters: Vec<String> = chunks
            .iter()
            .map(|chunk| {
                let listed: Vec<String> = chunk.iter().map(i64::to_string).collect();
                format!(
                    "mediaId_in: [{}], airingAt_greater: {after}",
                    listed.join(", ")
                )
            })
            .collect();
        let found = match self.schedule_pages(&filters, Query::UpcomingSchedule) {
            Ok(found) => found,
            Err(interrupted) => {
                return Err(interrupted
                    .into_error(|finished| upcoming_schedules(&chunks, finished.into_iter())));
            }
        };
        let mut schedules = upcoming_schedules(&chunks, found.iter().cloned().enumerate());
        let rejected: Vec<i64> = chunks
            .iter()
            .zip(&found)
            .filter(|(_, nodes)| nodes.is_none())
            .flat_map(|(chunk, _)| chunk.iter().copied())
            .collect();
        if !rejected.is_empty() {
            tracing::warn!(
                "Batched airing schedule query failed; fetching those shows one at a time."
            );
            match self.fetch_media_airing_schedules(&rejected) {
                Ok(single) => schedules.extend(single),
                Err(AniListError::Deferred(mut deferral)) => {
                    schedules.extend(std::mem::take(&mut deferral.partial));
                    deferral.partial = schedules;
                    return Err(AniListError::Deferred(deferral));
                }
                Err(other) => return Err(other),
            }
        }
        Ok(schedules)
    }
}

/// When AniList may be asked again, for the status endpoint.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RateLimitStatus {
    /// The epoch second, `None` when AniList is not blocked.
    pub blocked_until: Option<f64>,
    pub retry_in: Option<f64>,
}

fn epoch_seconds(instant: Timestamp) -> f64 {
    instant.as_millisecond() as f64 / 1000.0
}

fn unique(ids: &[i64]) -> Vec<i64> {
    let mut seen = HashSet::new();
    ids.iter().copied().filter(|id| seen.insert(*id)).collect()
}

// -- air time pages --

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Node {
    media_id: Option<i64>,
    episode: Option<i64>,
    airing_at: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Block {
    page_info: Option<PageInfo>,
    airing_schedules: Option<Vec<Node>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PageInfo {
    has_next_page: Option<bool>,
}

/// The nodes found for each filter that was finished.
type Finished = Vec<(usize, Option<Vec<Node>>)>;

/// A pass that stopped before every filter was done.
enum Interrupted {
    /// AniList should not be asked again yet; the filters finished so far are kept.
    Deferred {
        deferral: Deferral,
        finished: Finished,
    },
    Failed(AniListError),
}

impl Interrupted {
    /// The error to report, with the finished filters turned into `partial` schedules.
    fn into_error(self, schedules: impl FnOnce(Finished) -> Schedules) -> AniListError {
        match self {
            Self::Deferred {
                mut deferral,
                finished,
            } => {
                deferral.partial = schedules(finished);
                AniListError::Deferred(deferral)
            }
            Self::Failed(error) => error,
        }
    }
}

fn instant(seconds: i64) -> Option<Timestamp> {
    Timestamp::from_second(seconds).ok()
}

/// Each answered show's nodes as its schedule, in air order, an episode listed twice kept once.
fn full_schedules(found: impl Iterator<Item = (i64, Option<Vec<Node>>)>) -> Schedules {
    let mut schedules = Schedules::new();
    for (media_id, nodes) in found {
        let Some(nodes) = nodes else { continue };
        let mut seen = HashSet::new();
        let mut schedule = Vec::new();
        for node in nodes {
            let (Some(episode), Some(airing_at)) = (node.episode, node.airing_at.and_then(instant))
            else {
                continue;
            };
            if seen.insert(episode) {
                schedule.push(AiringEpisode { episode, airing_at });
            }
        }
        schedules.insert(media_id, schedule);
    }
    schedules
}

/// The schedules of the chunks answered, by show in episode order, every show of such a chunk
/// included.
fn upcoming_schedules(
    chunks: &[&[i64]],
    found: impl Iterator<Item = (usize, Option<Vec<Node>>)>,
) -> Schedules {
    let mut schedules = Schedules::new();
    for (index, nodes) in found {
        let Some(nodes) = nodes else { continue };
        let mut by_show: std::collections::BTreeMap<
            i64,
            std::collections::BTreeMap<i64, Timestamp>,
        > = chunks[index]
            .iter()
            .map(|&id| (id, Default::default()))
            .collect();
        for node in nodes {
            let (Some(media_id), Some(episode), Some(airing_at)) = (
                node.media_id,
                node.episode,
                node.airing_at.and_then(instant),
            ) else {
                continue;
            };
            if let Some(episodes) = by_show.get_mut(&media_id) {
                episodes.insert(episode, airing_at);
            }
        }
        for (media_id, episodes) in by_show {
            schedules.insert(
                media_id,
                episodes
                    .into_iter()
                    .map(|(episode, airing_at)| AiringEpisode { episode, airing_at })
                    .collect(),
            );
        }
    }
    schedules
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;

    use super::*;

    struct Always(&'static str);

    impl Transport for Always {
        fn post_json(&self, _url: &str, _body: &[u8]) -> Result<Answer, TransportError> {
            Ok(Answer {
                status: 200,
                headers: Vec::new(),
                body: self.0.as_bytes().to_vec(),
            })
        }
    }

    fn row_count() -> u64 {
        traffic::snapshot()
            .rows
            .iter()
            .filter(|row| row.kind == Query::UserList.kind() && row.host == HOST)
            .map(|row| row.requests)
            .sum()
    }

    #[test]
    fn every_attempt_is_counted_under_its_kind() {
        let before = row_count();
        let client = AniListClient::with_parts(
            Box::new(Always(r#"{"data":{"MediaListCollection":{"lists":[]}}}"#)),
            Arc::<SystemClock>::default(),
            None,
        );
        assert_eq!(client.fetch_user_list("someone", None), Ok(Vec::new()));
        assert_eq!(row_count(), before + 1);
    }

    #[test]
    fn a_saved_list_that_cannot_be_read_is_ignored() {
        let folder = tempfile_dir("unreadable");
        for (index, content) in ["{not json", r#"{"entries": []}"#, ""].iter().enumerate() {
            let path = folder.join(format!("list-{index}.json"));
            std::fs::write(&path, content).unwrap();
            let client = AniListClient::with_parts(
                Box::new(Always("{}")),
                Arc::<SystemClock>::default(),
                Some(path),
            );
            assert!(client.user_list().is_empty() && client.user_list_at().is_none());
        }
        let missing = AniListClient::with_parts(
            Box::new(Always("{}")),
            Arc::<SystemClock>::default(),
            Some(folder.join("absent.json")),
        );
        assert!(missing.user_list().is_empty());
    }

    fn tempfile_dir(name: &str) -> PathBuf {
        let folder =
            std::env::temp_dir().join(format!("kisetsu-anilist-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        folder
    }

    #[test]
    fn the_connection_reads_headers_and_body_and_follows_no_redirect() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            if let Some(mut stream) = listener.incoming().flatten().next() {
                // Closing with unread request bytes resets the connection, so read it all.
                let mut request = Vec::new();
                let mut chunk = [0u8; 4096];
                while !request.ends_with(b"\r\n\r\n{}") {
                    match stream.read(&mut chunk) {
                        Ok(0) | Err(_) => break,
                        Ok(read) => request.extend_from_slice(&chunk[..read]),
                    }
                }
                let _ = stream.write_all(
                    b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/elsewhere\r\nX-RateLimit-Remaining: 7\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi",
                );
            }
        });
        let answer = UreqTransport::new(Duration::from_secs(5))
            .post_json(&url, b"{}")
            .unwrap();
        assert_eq!((answer.status, answer.body.as_slice()), (302, &b"hi"[..]));
        assert_eq!(answer.rate_headers().remaining, Some(7.0));
    }

    #[test]
    fn a_refused_connection_is_a_transport_error() {
        let url = {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            format!("http://{}/", listener.local_addr().unwrap())
        };
        assert!(
            UreqTransport::new(Duration::from_secs(5))
                .post_json(&url, b"{}")
                .is_err()
        );
    }
}
