//! Everything the jobs ask of the outside world: the feeds and the sites behind them, SubsPlease's
//! schedule, AniList and the cover images.

use std::collections::HashSet;
use std::path::PathBuf;

use jiff::Timestamp;
use kisetsu_clients::anilist::{AniListClient, AniListError, ListEntry, Schedules};
use kisetsu_clients::fetch::Fetcher;
use kisetsu_clients::palette::{self, Hues, ImageError, WebImages};
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use kisetsu_clients::sites;
use kisetsu_services::backfill::Searcher;

/// The outside world, as far as the jobs go. Jobs ask it from several threads at once.
pub trait Remote: Send + Sync {
    /// The items a feed lists now.
    fn fetch_feed(&self, url: &str) -> Result<Vec<FeedArticle>, FeedFetchError>;

    /// The feed's releases of `term` on its site; `None` when this kind of feed cannot be searched.
    fn search(
        &self,
        feed_url: &str,
        term: &str,
        now: Timestamp,
    ) -> Result<Option<Vec<FeedArticle>>, FeedFetchError>;

    /// The titles SubsPlease airs this week; `None` when its answer lists none.
    fn subsplease_schedule(&self) -> Result<Option<Vec<String>>, FeedFetchError>;

    /// The colours of a show's banner, or of its cover when the banner has none.
    fn hues(&self, banner: &str, cover: &str) -> Result<Option<Hues>, ImageError>;

    /// The entries of the user's AniList list the sync acts on. With `reuse_within`, a list
    /// fetched less than that many seconds ago is taken instead of a new request.
    fn seasonal(
        &self,
        username: &str,
        monitored: &HashSet<i64>,
        reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError>;

    /// The air times of the shows from `since` on.
    fn upcoming_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> Result<Schedules, AniListError>;

    /// The whole air schedule of each show.
    fn media_schedules(&self, media_ids: &[i64]) -> Result<Schedules, AniListError>;
}

/// The real sites, over the network. One of these serves every job, since the pacing of requests
/// to a host is only kept by the client that makes them.
pub struct WebRemote {
    fetcher: Fetcher,
    images: WebImages,
    anilist: AniListClient,
}

impl WebRemote {
    /// `list_cache` is where the user's AniList list is kept between runs.
    pub fn new(list_cache: PathBuf) -> Self {
        Self {
            fetcher: Fetcher::default(),
            images: WebImages::default(),
            anilist: AniListClient::new(Some(list_cache)),
        }
    }
}

impl Remote for WebRemote {
    fn fetch_feed(&self, url: &str) -> Result<Vec<FeedArticle>, FeedFetchError> {
        self.fetcher.fetch_feed(url)
    }

    fn search(
        &self,
        feed_url: &str,
        term: &str,
        now: Timestamp,
    ) -> Result<Option<Vec<FeedArticle>>, FeedFetchError> {
        sites::search_feed(&self.fetcher, feed_url, term, now)
    }

    fn subsplease_schedule(&self) -> Result<Option<Vec<String>>, FeedFetchError> {
        sites::subsplease_schedule(&self.fetcher)
    }

    fn hues(&self, banner: &str, cover: &str) -> Result<Option<Hues>, ImageError> {
        palette::fetch_hues(&self.images, banner, cover)
    }

    fn seasonal(
        &self,
        username: &str,
        monitored: &HashSet<i64>,
        reuse_within: Option<f64>,
    ) -> Result<Vec<ListEntry>, AniListError> {
        self.anilist
            .fetch_user_seasonal_anime(username, monitored, reuse_within)
    }

    fn upcoming_schedules(
        &self,
        media_ids: &[i64],
        since: Timestamp,
    ) -> Result<Schedules, AniListError> {
        self.anilist
            .fetch_upcoming_airing_schedules(media_ids, since)
    }

    fn media_schedules(&self, media_ids: &[i64]) -> Result<Schedules, AniListError> {
        self.anilist.fetch_media_airing_schedules(media_ids)
    }
}

/// A remote's search, as the past-release search asks for it.
pub(crate) struct RemoteSearch<'a>(pub &'a dyn Remote);

impl Searcher for RemoteSearch<'_> {
    fn search(
        &self,
        feed_url: &str,
        term: &str,
        now: Timestamp,
    ) -> Result<Option<Vec<FeedArticle>>, FeedFetchError> {
        self.0.search(feed_url, term, now)
    }
}
