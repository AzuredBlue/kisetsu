//! Fetching feeds and other pages: spacing the requests to a host, waiting out a rate limit and
//! retrying a refused connection.

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use jiff::Timestamp;

use crate::net::{Failure, classify};
use crate::rss::{FeedArticle, FeedFetchError, feed_wall_zone, parse_date, parse_feed};
use crate::traffic::{self, Outcome};
use crate::url;

const USER_AGENT: &str = "Kisetsu (feed reader)";

/// What a request is for; each kind is counted on its own (see [`traffic`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestKind {
    FeedRead,
    Search,
    Schedule,
}

impl RequestKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::FeedRead => "Feed read",
            Self::Search => "Search",
            Self::Schedule => "Schedule",
        }
    }
}

/// How requests are paced and how long they may take.
#[derive(Debug, Clone)]
pub struct FetchConfig {
    pub connect_timeout: Duration,
    /// How long a read of the answer may wait for data.
    pub read_timeout: Duration,
    pub max_body_bytes: u64,
    /// Requests to one host (feed reads and searches alike) leave at least this long between them,
    /// so a burst such as a first run's searches never trips the site's rate limit.
    pub min_request_interval: f64,
    /// A 429 naming no wait leaves the host alone this long, in seconds.
    pub rate_limit_wait: f64,
    /// How often a 429 is waited out and retried.
    pub rate_limit_retries: u32,
    /// A wait longer than this is not sat out, in seconds.
    pub max_rate_limit_wait: f64,
    /// A feed host that refuses or cannot be found (no network yet at boot, say) is tried again
    /// after each of these waits, in seconds.
    pub connect_retry_waits: Vec<f64>,
}

impl Default for FetchConfig {
    fn default() -> Self {
        Self {
            connect_timeout: Duration::from_secs(5),
            read_timeout: Duration::from_secs(10),
            max_body_bytes: 8 * 1024 * 1024,
            min_request_interval: 1.0,
            rate_limit_wait: 10.0,
            rate_limit_retries: 2,
            max_rate_limit_wait: 60.0,
            connect_retry_waits: vec![2.0, 4.0, 8.0],
        }
    }
}

/// Time as the fetcher sees it, so waits can be skipped in tests.
pub trait Clock: Send + Sync {
    /// Seconds on a monotonic clock.
    fn now(&self) -> f64;
    fn sleep(&self, seconds: f64);
    /// The current instant on the wall clock.
    fn wall(&self) -> Timestamp {
        Timestamp::now()
    }
}

/// The real clock.
#[derive(Debug)]
pub struct SystemClock {
    start: Instant,
}

impl Default for SystemClock {
    fn default() -> Self {
        Self {
            start: Instant::now(),
        }
    }
}

impl Clock for SystemClock {
    fn now(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }

    fn sleep(&self, seconds: f64) {
        if let Ok(wait) = Duration::try_from_secs_f64(seconds) {
            std::thread::sleep(wait);
        }
    }
}

/// Spaces the requests to each host, and holds back every request to a host that answered 429 until
/// the wait it asked for is over.
///
/// Each request takes the next free slot of its host before it waits, so requests started together
/// still go one interval apart.
#[derive(Debug)]
pub struct HostGate {
    interval: f64,
    max_wait: f64,
    open_at: HashMap<String, f64>,
    next_slot: HashMap<String, f64>,
}

impl HostGate {
    pub fn new(interval: f64, max_wait: f64) -> Self {
        Self {
            interval,
            max_wait,
            open_at: HashMap::new(),
            next_slot: HashMap::new(),
        }
    }

    /// Takes the next slot of `host` and returns how many seconds to wait for it, or an error when
    /// the host asked to be left alone for longer than a request sits out.
    pub fn reserve(&mut self, host: &str, now: f64) -> Result<f64, FeedFetchError> {
        let open_at = self.open_at.get(host).copied().unwrap_or(0.0);
        let remaining = open_at - now;
        if remaining > self.max_wait {
            return Err(FeedFetchError::RateLimitedFor {
                host: host.to_owned(),
                seconds: remaining.round() as u64,
            });
        }
        let next = self.next_slot.get(host).copied().unwrap_or(0.0);
        let slot = now.max(open_at).max(next);
        self.next_slot.insert(host.to_owned(), slot + self.interval);
        Ok((slot - now).max(0.0))
    }

    /// Holds back `host` for `seconds` from `now`.
    pub fn close(&mut self, host: &str, seconds: f64, now: f64) {
        let open_at = self.open_at.entry(host.to_owned()).or_insert(0.0);
        *open_at = open_at.max(now + seconds);
    }
}

/// Reads feeds and pages over HTTP, all through one [`HostGate`].
pub struct Fetcher {
    agent: ureq::Agent,
    config: FetchConfig,
    gate: Mutex<HostGate>,
    clock: Box<dyn Clock>,
}

impl Default for Fetcher {
    fn default() -> Self {
        Self::new(FetchConfig::default(), Box::<SystemClock>::default())
    }
}

fn locked<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl Fetcher {
    pub fn new(config: FetchConfig, clock: Box<dyn Clock>) -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .user_agent(USER_AGENT)
            // Requests are minutes apart, and an idle connection would keep its buffers and TLS
            // state until the next one.
            .max_idle_connections(0)
            .timeout_connect(Some(config.connect_timeout))
            .timeout_per_read(Some(config.read_timeout))
            .build()
            .new_agent();
        let gate = HostGate::new(config.min_request_interval, config.max_rate_limit_wait);
        Self {
            agent,
            config,
            gate: Mutex::new(gate),
            clock,
        }
    }

    /// GETs `url`, waiting out and retrying a 429 as long as its host asks, and for a feed read a
    /// refused connection a few times. Each attempt is counted under `kind`.
    pub fn fetch_bytes(&self, url: &str, kind: RequestKind) -> Result<Vec<u8>, FeedFetchError> {
        let host = url::host_or_url(url);
        let mut connect_waits = match kind {
            RequestKind::FeedRead => self.config.connect_retry_waits.iter(),
            _ => [].iter(),
        };
        let mut rate_limited = 0;
        loop {
            let wait = locked(&self.gate).reserve(&host, self.clock.now())?;
            if wait > 0.0 {
                self.clock.sleep(wait);
            }
            let response = match self.agent.get(url).call() {
                Ok(response) => response,
                Err(error) => {
                    traffic::record(kind.as_str(), &host, Outcome::Error);
                    match classify(&error) {
                        Failure::Timeout => return Err(FeedFetchError::Timeout { host }),
                        failure if failure.is_connect() => {
                            let Some(&wait) = connect_waits.next() else {
                                return Err(unreachable(host, &error));
                            };
                            locked(&self.gate).close(&host, wait, self.clock.now());
                            continue;
                        }
                        _ => return Err(unreachable(host, &error)),
                    }
                }
            };
            let status = response.status().as_u16();
            if status == 429 {
                traffic::record(kind.as_str(), &host, Outcome::RateLimited);
                let wait = retry_after(
                    response
                        .headers()
                        .get("retry-after")
                        .and_then(|value| value.to_str().ok()),
                    self.config.rate_limit_wait,
                    Timestamp::now(),
                );
                locked(&self.gate).close(&host, wait, self.clock.now());
                if rate_limited == self.config.rate_limit_retries
                    || wait > self.config.max_rate_limit_wait
                {
                    return Err(FeedFetchError::RateLimited { host });
                }
                rate_limited += 1;
                continue;
            }
            if status >= 400 {
                traffic::record(kind.as_str(), &host, Outcome::of_status(status));
                return Err(FeedFetchError::Status { host, status });
            }
            return match response
                .into_body()
                .with_config()
                .limit(self.config.max_body_bytes)
                .read_to_vec()
            {
                Ok(body) => {
                    traffic::record(kind.as_str(), &host, Outcome::Ok);
                    Ok(body)
                }
                Err(ureq::Error::BodyExceedsLimit(_)) => {
                    traffic::record(kind.as_str(), &host, Outcome::Ok);
                    Err(FeedFetchError::TooLarge {
                        host,
                        megabytes: self.config.max_body_bytes / (1024 * 1024),
                    })
                }
                Err(error) => {
                    traffic::record(kind.as_str(), &host, Outcome::Error);
                    Err(match classify(&error) {
                        Failure::Timeout => FeedFetchError::Timeout { host },
                        _ => unreachable(host, &error),
                    })
                }
            };
        }
    }

    /// Fetches and parses one feed.
    pub fn fetch_feed(&self, url: &str) -> Result<Vec<FeedArticle>, FeedFetchError> {
        let body = self.fetch_bytes(url, RequestKind::FeedRead)?;
        parse_feed(&body, feed_wall_zone(url).as_ref())
    }
}

fn unreachable(host: String, error: &ureq::Error) -> FeedFetchError {
    FeedFetchError::Unreachable {
        host,
        detail: error.to_string(),
    }
}

/// The seconds a 429 asks to wait: its `Retry-After`, in seconds or as a date, else `default`.
pub fn retry_after(header: Option<&str>, default: f64, now: Timestamp) -> f64 {
    let value = header.unwrap_or("").trim();
    if value.is_empty() {
        return default;
    }
    if let Ok(seconds) = value.parse::<f64>() {
        return seconds.max(0.0);
    }
    match parse_date(value, None) {
        Some(date) => (date.as_millisecond() - now.as_millisecond()).max(0) as f64 / 1000.0,
        None => default,
    }
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::Arc;

    use super::*;

    #[derive(Default)]
    struct FakeClock {
        now: Mutex<f64>,
        slept: Mutex<Vec<f64>>,
    }

    impl Clock for Arc<FakeClock> {
        fn now(&self) -> f64 {
            *locked(&self.now)
        }

        fn sleep(&self, seconds: f64) {
            locked(&self.slept).push(seconds);
            *locked(&self.now) += seconds;
        }
    }

    /// Answers each connection with the next canned response (the last one repeats) and counts them.
    fn serve(responses: Vec<&'static str>) -> (String, Arc<Mutex<usize>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/feed", listener.local_addr().unwrap());
        let served = Arc::new(Mutex::new(0));
        let count = Arc::clone(&served);
        std::thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut request = [0u8; 2048];
                let _ = stream.read(&mut request);
                let index = {
                    let mut served = locked(&count);
                    *served += 1;
                    (*served - 1).min(responses.len() - 1)
                };
                let _ = stream.write_all(responses[index].as_bytes());
            }
        });
        (url, served)
    }

    fn fetcher(config: FetchConfig) -> (Fetcher, Arc<FakeClock>) {
        let clock = Arc::new(FakeClock::default());
        (Fetcher::new(config, Box::new(Arc::clone(&clock))), clock)
    }

    const OK: &str = "HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello";

    #[test]
    fn a_429_is_waited_out_for_as_long_as_the_site_asks_then_retried() {
        let (url, served) = serve(vec![
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: 3\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            OK,
        ]);
        let (fetcher, clock) = fetcher(FetchConfig::default());
        let body = fetcher.fetch_bytes(&url, RequestKind::Search).unwrap();
        assert_eq!(body, b"hello");
        assert_eq!(*locked(&served), 2);
        assert_eq!(*locked(&clock.slept), vec![3.0]);
    }

    #[test]
    fn a_long_rate_limit_fails_at_once_and_holds_back_the_next_request() {
        let (url, served) = serve(vec![
            "HTTP/1.1 429 Too Many Requests\r\nRetry-After: 600\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        ]);
        let (fetcher, _) = fetcher(FetchConfig::default());
        for _ in 0..2 {
            let error = fetcher.fetch_bytes(&url, RequestKind::Search).unwrap_err();
            assert!(error.to_string().contains("rate limiting"), "{error}");
        }
        assert_eq!(*locked(&served), 1);
    }

    #[test]
    fn a_server_error_is_transient_and_a_missing_page_is_not() {
        let (url, _) = serve(vec![
            "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
            "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        ]);
        let (fetcher, _) = fetcher(FetchConfig::default());
        let error = fetcher.fetch_bytes(&url, RequestKind::Search).unwrap_err();
        assert!(error.is_transient() && error.to_string().ends_with("HTTP 503"));
        let error = fetcher.fetch_bytes(&url, RequestKind::Search).unwrap_err();
        assert!(!error.is_transient() && error.to_string().ends_with("HTTP 404"));
    }

    #[test]
    fn an_oversized_body_is_refused() {
        let (url, _) = serve(vec![OK]);
        let config = FetchConfig {
            max_body_bytes: 3,
            ..FetchConfig::default()
        };
        let error = fetcher(config)
            .0
            .fetch_bytes(&url, RequestKind::Search)
            .unwrap_err();
        assert!(matches!(error, FeedFetchError::TooLarge { .. }), "{error}");
    }

    #[test]
    fn a_refused_connection_is_retried_after_each_wait() {
        let url = {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            format!("http://{}/feed", listener.local_addr().unwrap())
        };
        let config = FetchConfig {
            connect_retry_waits: vec![2.0],
            ..FetchConfig::default()
        };
        let (fetcher, clock) = fetcher(config);
        let error = fetcher
            .fetch_bytes(&url, RequestKind::FeedRead)
            .unwrap_err();
        assert!(
            matches!(error, FeedFetchError::Unreachable { .. }),
            "{error}"
        );
        assert!(error.is_transient());
        assert_eq!(*locked(&clock.slept), vec![2.0]);
    }

    #[test]
    fn a_silent_server_is_a_timeout() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/feed", listener.local_addr().unwrap());
        let config = FetchConfig {
            read_timeout: Duration::from_millis(150),
            ..FetchConfig::default()
        };
        let error = fetcher(config)
            .0
            .fetch_bytes(&url, RequestKind::Search)
            .unwrap_err();
        assert!(matches!(error, FeedFetchError::Timeout { .. }), "{error}");
        drop(listener);
    }
}
