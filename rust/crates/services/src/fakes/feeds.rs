use std::cell::RefCell;
use std::collections::BTreeMap;

use jiff::Timestamp;
use kisetsu_clients::rss::{FeedArticle, FeedFetchError};
use sha1_smol::Sha1;

use crate::context::FeedSource;

/// A feed item. Like a real one it carries its info hash, and its torrent address names it.
///
/// The guid is `guid:<title>` and the hash is the guid's SHA-1 unless given.
pub fn article(
    title: &str,
    published_at: Option<Timestamp>,
    guid: Option<&str>,
    info_hash: Option<&str>,
    torrent_url: Option<&str>,
) -> FeedArticle {
    let guid = guid.map_or_else(|| format!("guid:{title}"), str::to_owned);
    let info_hash = info_hash.map_or_else(|| Sha1::from(&guid).digest().to_string(), str::to_owned);
    let torrent_url = torrent_url.map_or_else(
        || format!("https://example.test/{info_hash}.torrent"),
        str::to_owned,
    );
    FeedArticle {
        guid,
        title: title.to_owned(),
        torrent_url,
        published_at,
        info_hash: Some(info_hash),
    }
}

/// The RSS feeds Kisetsu fetches itself, and the searchable sites behind them.
#[derive(Debug, Default)]
pub struct FakeFeeds {
    pub items: BTreeMap<String, Vec<FeedArticle>>,
    pub errors: BTreeMap<String, FeedFetchError>,
    pub searchable: BTreeMap<String, Vec<FeedArticle>>,
    fetched: RefCell<Vec<String>>,
    searched: RefCell<Vec<(String, String)>>,
}

impl FakeFeeds {
    /// Adds a new item at the top of a feed.
    pub fn post(&mut self, url: &str, item: FeedArticle) {
        self.items
            .entry(url.to_owned())
            .or_default()
            .insert(0, item);
    }

    /// Makes reading the feed fail.
    pub fn fail(&mut self, url: &str, message: &str) {
        self.errors.insert(
            url.to_owned(),
            FeedFetchError::Unreachable {
                host: url.to_owned(),
                detail: message.to_owned(),
            },
        );
    }

    pub fn heal(&mut self, url: &str) {
        self.errors.remove(url);
    }

    /// The addresses fetched, in order.
    pub fn fetched(&self) -> Vec<String> {
        self.fetched.borrow().clone()
    }

    /// The feed addresses and terms searched, in order.
    pub fn searched(&self) -> Vec<(String, String)> {
        self.searched.borrow().clone()
    }
}

impl FeedSource for FakeFeeds {
    fn fetch(&self, url: &str) -> Result<Vec<FeedArticle>, FeedFetchError> {
        self.fetched.borrow_mut().push(url.to_owned());
        match self.errors.get(url) {
            Some(error) => Err(error.clone()),
            None => Ok(self.items.get(url).cloned().unwrap_or_default()),
        }
    }

    /// The items on the feed's site whose title contains the term, or `None` when the site
    /// cannot be searched. A grouped term (`((a)|(b))`, as nyaa takes it) finds what any of its
    /// names would.
    fn search(
        &self,
        feed_url: &str,
        term: &str,
    ) -> Result<Option<Vec<FeedArticle>>, FeedFetchError> {
        self.searched
            .borrow_mut()
            .push((feed_url.to_owned(), term.to_owned()));
        let Some(site) = self.searchable.get(feed_url) else {
            return Ok(None);
        };
        if let Some(error) = self.errors.get(feed_url) {
            return Err(error.clone());
        }
        let names: Vec<String> = match term
            .strip_prefix("((")
            .and_then(|rest| rest.strip_suffix("))"))
        {
            Some(inner) => inner.split(")|(").map(str::to_lowercase).collect(),
            None => vec![term.to_lowercase()],
        };
        Ok(Some(
            site.iter()
                .filter(|article| {
                    let title = article.title.to_lowercase();
                    names.iter().any(|name| title.contains(name))
                })
                .cloned()
                .collect(),
        ))
    }
}
