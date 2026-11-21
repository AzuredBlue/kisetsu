//! The requests the app answers and the responses it gives, apart from how they travel.

use std::io::Read;
use std::time::Duration;

use percent_encoding::percent_decode_str;
use serde_json::Value;

/// A request, as far as the app cares: the transport fills it in.
pub struct Request<'a> {
    pub method: String,
    /// The path as sent, still percent-encoded.
    pub raw_path: String,
    /// The path with its escapes decoded.
    pub path: String,
    pub raw_query: String,
    /// Lower-case names; a header sent twice is listed twice.
    pub headers: Vec<(String, String)>,
    pub body: &'a mut dyn Read,
}

impl Request<'_> {
    /// The last value of a header, which is the one that counts when it is repeated.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .rev()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }

    /// The query as decoded name and value pairs, in the order sent.
    pub fn query(&self) -> Vec<(String, String)> {
        self.raw_query
            .split('&')
            .filter(|pair| !pair.is_empty())
            .map(|pair| {
                let (name, value) = pair.split_once('=').unwrap_or((pair, ""));
                (decode_form(name), decode_form(value))
            })
            .collect()
    }
}

/// A form-encoded text: `+` is a space and escapes are decoded.
fn decode_form(text: &str) -> String {
    percent_decode_str(&text.replace('+', " "))
        .decode_utf8_lossy()
        .into_owned()
}

/// What a response carries.
pub enum Body {
    Empty,
    Bytes(Vec<u8>),
    /// A body of known length, read as it is sent.
    Reader {
        reader: Box<dyn Read + Send>,
        length: u64,
    },
    /// Server-sent events: each item is written and flushed on its own, and the stream ends when
    /// the iterator does.
    Events(Box<dyn Iterator<Item = Vec<u8>> + Send>),
}

pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Body,
}

impl Response {
    pub fn new(status: u16, body: Body) -> Self {
        Self {
            status,
            headers: Vec::new(),
            body,
        }
    }

    pub fn json(status: u16, value: &Value) -> Self {
        Self::new(status, Body::Bytes(value.to_string().into_bytes()))
            .with("content-type", "application/json")
    }

    pub fn text(status: u16, content_type: &str, text: impl Into<String>) -> Self {
        Self::new(status, Body::Bytes(text.into().into_bytes())).with("content-type", content_type)
    }

    pub fn with(mut self, name: &str, value: impl Into<String>) -> Self {
        self.headers.push((name.to_owned(), value.into()));
        self
    }

    /// The value of the header, if the response has it.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// The body of a response that holds its bytes.
    pub fn bytes(&self) -> &[u8] {
        match &self.body {
            Body::Bytes(bytes) => bytes,
            _ => &[],
        }
    }
}

/// How long an event stream waits for a message before it writes a comment, so a proxy does not
/// close it as idle.
pub const KEEPALIVE: Duration = Duration::from_secs(30);
