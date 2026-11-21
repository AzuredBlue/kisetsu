//! The few URL operations the clients need, on text.

/// The parts of a URL, split the way a browser's address bar would show them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Parts<'a> {
    pub scheme: &'a str,
    pub netloc: &'a str,
    pub path: &'a str,
    pub query: &'a str,
    pub fragment: &'a str,
}

pub(crate) fn split(url: &str) -> Parts<'_> {
    let (rest, fragment) = url.split_once('#').unwrap_or((url, ""));
    let (rest, query) = rest.split_once('?').unwrap_or((rest, ""));
    let (scheme, rest) = match rest.split_once(':') {
        Some((scheme, after)) if is_scheme(scheme) => (scheme, after),
        _ => ("", rest),
    };
    let (netloc, path) = match rest.strip_prefix("//") {
        Some(after) => match after.find('/') {
            Some(end) => after.split_at(end),
            None => (after, ""),
        },
        None => ("", rest),
    };
    Parts {
        scheme,
        netloc,
        path,
        query,
        fragment,
    }
}

fn is_scheme(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'))
}

/// The lowercase host name, without user, port or brackets; `None` when the URL names none.
pub(crate) fn hostname(url: &str) -> Option<String> {
    let netloc = split(url).netloc;
    let host = netloc.rsplit_once('@').map_or(netloc, |(_, host)| host);
    let host = match host.strip_prefix('[') {
        Some(inner) => inner.split_once(']').map_or(inner, |(address, _)| address),
        None => host.split_once(':').map_or(host, |(name, _)| name),
    };
    (!host.is_empty()).then(|| host.to_lowercase())
}

/// The host of a URL, or the whole text when it has none.
pub(crate) fn host_or_url(url: &str) -> String {
    hostname(url).unwrap_or_else(|| url.to_owned())
}

/// The text with its `%XX` escapes decoded; bytes that are not UTF-8 become U+FFFD.
pub(crate) fn unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while let Some(&byte) = bytes.get(index) {
        let escaped = (byte == b'%')
            .then(|| bytes.get(index + 1..index + 3))
            .flatten()
            .and_then(|pair| Some(hex_digit(pair[0])? << 4 | hex_digit(pair[1])?));
        match escaped {
            Some(value) => {
                decoded.push(value);
                index += 3;
            }
            None => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

fn hex_digit(byte: u8) -> Option<u8> {
    char::from(byte)
        .to_digit(16)
        .and_then(|digit| u8::try_from(digit).ok())
}

/// The `name=value` pairs of a query, decoded, blank values kept.
pub(crate) fn parse_qsl(query: &str) -> Vec<(String, String)> {
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (name, value) = pair.split_once('=').unwrap_or((pair, ""));
            (
                unquote(&name.replace('+', " ")),
                unquote(&value.replace('+', " ")),
            )
        })
        .collect()
}

/// Percent-encodes everything but letters, digits, `_.-~` and the characters of `safe`.
pub(crate) fn quote(text: &str, safe: &str) -> String {
    let mut quoted = String::with_capacity(text.len());
    for &byte in text.as_bytes() {
        let character = char::from(byte);
        if character.is_ascii_alphanumeric()
            || "_.-~".contains(character)
            || safe.contains(character)
        {
            quoted.push(character);
        } else {
            quoted.push_str(&format!("%{byte:02X}"));
        }
    }
    quoted
}

/// A query: `name=value` pairs joined by `&`, spaces written as `+`.
pub(crate) fn urlencode(pairs: &[(String, String)]) -> String {
    let encode = |text: &str| quote(text, " ").replace(' ', "+");
    pairs
        .iter()
        .map(|(name, value)| format!("{}={}", encode(name), encode(value)))
        .collect::<Vec<_>>()
        .join("&")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_a_feed_url() {
        let parts = split("https://Nyaa.si:443/path?page=rss&q=a#top");
        assert_eq!(parts.scheme, "https");
        assert_eq!(parts.netloc, "Nyaa.si:443");
        assert_eq!(parts.path, "/path");
        assert_eq!(parts.query, "page=rss&q=a");
        assert_eq!(parts.fragment, "top");
        assert_eq!(
            hostname("https://u:p@Nyaa.si:443/x").as_deref(),
            Some("nyaa.si")
        );
        assert_eq!(hostname("http://[::1]:8080/").as_deref(), Some("::1"));
        assert_eq!(host_or_url("not a url"), "not a url");
    }
}
