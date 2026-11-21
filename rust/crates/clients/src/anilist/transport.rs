//! The HTTP call to AniList, behind a trait so tests can script the answers.

use std::fmt;
use std::time::Duration;

use super::governor::RateHeaders;

/// The most of an answer that is read.
const MAX_ANSWER_BYTES: u64 = 32 * 1024 * 1024;

/// What AniList answered.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Answer {
    pub status: u16,
    /// Header names in lowercase.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Answer {
    fn header_number(&self, name: &str) -> Option<f64> {
        self.headers
            .iter()
            .find(|(key, _)| key == name)
            .and_then(|(_, value)| value.trim().parse().ok())
    }

    /// The rate-limit headers of the answer.
    pub fn rate_headers(&self) -> RateHeaders {
        RateHeaders {
            limit: self.header_number("x-ratelimit-limit"),
            remaining: self.header_number("x-ratelimit-remaining"),
            retry_after: self.header_number("retry-after"),
            reset: self.header_number("x-ratelimit-reset"),
        }
    }
}

/// The request went out but no answer came back, in the transport's own words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransportError(pub String);

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TransportError {}

/// Sends one JSON POST.
pub trait Transport: Send + Sync {
    fn post_json(&self, url: &str, body: &[u8]) -> Result<Answer, TransportError>;
}

/// The real connection to AniList.
pub struct UreqTransport {
    agent: ureq::Agent,
}

impl UreqTransport {
    pub fn new(timeout: Duration) -> Self {
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .max_redirects(0)
            .max_redirects_will_error(false)
            .timeout_connect(Some(timeout))
            .timeout_per_read(Some(timeout))
            .build()
            .new_agent();
        Self { agent }
    }
}

impl Transport for UreqTransport {
    fn post_json(&self, url: &str, body: &[u8]) -> Result<Answer, TransportError> {
        let failed = |error: ureq::Error| TransportError(error.to_string());
        let response = self
            .agent
            .post(url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json")
            .send(body)
            .map_err(failed)?;
        let status = response.status().as_u16();
        let headers = response
            .headers()
            .iter()
            .filter_map(|(name, value)| {
                Some((name.as_str().to_owned(), value.to_str().ok()?.to_owned()))
            })
            .collect();
        let body = response
            .into_body()
            .with_config()
            .limit(MAX_ANSWER_BYTES)
            .read_to_vec()
            .map_err(failed)?;
        Ok(Answer {
            status,
            headers,
            body,
        })
    }
}
