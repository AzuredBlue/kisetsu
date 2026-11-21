//! Who may talk to the server: only pages served from this machine's own names.
//!
//! A rebound domain points at the loopback address, so the browser sends that domain as `Host`:
//! only IP literals, `localhost`, this machine's name and the names the user listed are served.
//! A page on another site cannot write either: its `Origin` must be the `Host` it was served from.

use std::net::{Ipv4Addr, Ipv6Addr};

/// The methods that change nothing.
const SAFE_METHODS: [&str; 3] = ["GET", "HEAD", "OPTIONS"];

/// What a sandbox refuses to change; every other write only reaches its throwaway database or
/// its pretend qBittorrent.
const SANDBOX_REFUSED: [&str; 2] = ["/api/backup/restore", "/api/backup/inspect"];

/// Why a request was refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    Host,
    Origin,
    Sandbox,
}

impl Refusal {
    pub fn reason(self) -> &'static str {
        match self {
            Self::Host => "Host not allowed.",
            Self::Origin => "Cross-origin requests are not allowed.",
            Self::Sandbox => "Not available in the sandbox.",
        }
    }
}

/// A request the guard cannot make sense of at all.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("the address is malformed")]
pub struct Malformed;

/// The names, besides IP literals and `localhost`, that may be used to reach the server.
#[derive(Debug, Clone, Default)]
pub struct Guard {
    own_name: Option<String>,
    extra_hosts: Vec<String>,
    sandbox: bool,
}

impl Guard {
    /// `extra_hosts` are the names the user allows, in any case.
    pub fn new(own_name: Option<String>, extra_hosts: &[String], sandbox: bool) -> Self {
        Self {
            own_name: own_name.map(|name| name.to_lowercase()),
            extra_hosts: extra_hosts
                .iter()
                .map(|name| name.trim().to_lowercase())
                .filter(|name| !name.is_empty())
                .collect(),
            sandbox,
        }
    }

    /// The name of this machine, as the operating system knows it.
    pub fn machine_name() -> Option<String> {
        gethostname::gethostname().into_string().ok()
    }

    /// `Ok(None)` when the request may go on, the reason when it may not, and `Err` when its
    /// `Origin` cannot be read.
    pub fn check(
        &self,
        method: &str,
        path: &str,
        host: Option<&str>,
        origin: Option<&str>,
    ) -> Result<Option<Refusal>, Malformed> {
        let host_header = host.unwrap_or_default();
        if !self.host_allowed(host_header) {
            return Ok(Some(Refusal::Host));
        }
        let safe = SAFE_METHODS.contains(&method);
        if let Some(origin) = origin.filter(|origin| !origin.is_empty()) {
            if !safe && split_netloc(origin)? != host_header {
                return Ok(Some(Refusal::Origin));
            }
        }
        if self.sandbox && !safe && SANDBOX_REFUSED.contains(&path) {
            return Ok(Some(Refusal::Sandbox));
        }
        Ok(None)
    }

    fn host_allowed(&self, host_header: &str) -> bool {
        let Ok(host) = request_host(host_header) else {
            return false;
        };
        let host = host.to_lowercase();
        if host.is_empty() {
            return false;
        }
        host == "localhost"
            || host.ends_with(".localhost")
            || self.extra_hosts.contains(&host)
            || is_ip_literal(&host)
            || self.own_name.as_ref() == Some(&host)
    }
}

/// Removes the characters a URL reader drops wherever they are.
fn cleaned(text: &str) -> String {
    text.chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect()
}

/// The part of `text` between `//` and the next `/`, `?` or `#`.
fn authority_of(text: &str) -> &str {
    let rest = &text[2..];
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    &rest[..end]
}

/// The `host[:port]` of an `Origin`: what follows `//` in a URL that has a scheme or none.
fn split_netloc(url: &str) -> Result<String, Malformed> {
    let url = cleaned(url);
    let mut rest = url.trim_start_matches(|c: char| c <= ' ');
    if let Some(colon) = rest.find(':') {
        let scheme = &rest[..colon];
        let valid = scheme
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_alphabetic())
            && scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.'));
        if valid {
            rest = &rest[colon + 1..];
        }
    }
    if !rest.starts_with("//") {
        return Ok(String::new());
    }
    let netloc = authority_of(rest);
    check_brackets(netloc)?;
    Ok(netloc.to_owned())
}

/// Brackets must pair up and hold an IPv6 address; text after the closing one may only be a port.
fn check_brackets(netloc: &str) -> Result<(), Malformed> {
    let (open, close) = (netloc.contains('['), netloc.contains(']'));
    if open != close {
        return Err(Malformed);
    }
    if !open {
        return Ok(());
    }
    let (_, after_open) = netloc.split_once('[').ok_or(Malformed)?;
    let (inside, after) = after_open.split_once(']').ok_or(Malformed)?;
    if !after.is_empty() && !after.starts_with(':') {
        return Err(Malformed);
    }
    if parse_ipv6(inside).is_none() {
        return Err(Malformed);
    }
    Ok(())
}

/// An IPv6 address, with a zone name after a `%` when it has one.
fn parse_ipv6(text: &str) -> Option<Ipv6Addr> {
    let address = match text.split_once('%') {
        Some((_, "")) => return None,
        Some((address, _)) => address,
        None => text,
    };
    address.parse().ok()
}

/// The host a `Host` header names: no port, no user information and no brackets.
fn request_host(header: &str) -> Result<String, Malformed> {
    let header = cleaned(header);
    let text = format!("//{header}");
    let netloc = authority_of(&text);
    check_brackets(netloc)?;
    let hostinfo = netloc.rsplit_once('@').map_or(netloc, |(_, host)| host);
    let host = match hostinfo.split_once('[') {
        Some((_, bracketed)) => bracketed
            .split_once(']')
            .map_or(bracketed, |(host, _)| host),
        None => hostinfo.split_once(':').map_or(hostinfo, |(host, _)| host),
    };
    Ok(host.to_owned())
}

fn is_ip_literal(host: &str) -> bool {
    host.parse::<Ipv4Addr>().is_ok() || parse_ipv6(host).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn guard() -> Guard {
        Guard::new(Some("Kisetsu-PC".into()), &["kisetsu.lan".into()], false)
    }

    #[test]
    fn only_this_machines_names_and_addresses_are_served() {
        let guard = guard();
        for good in [
            "localhost",
            "LOCALHOST:8085",
            "foo.localhost",
            ".localhost",
            "127.0.0.1:8085",
            "[::1]:8085",
            "[fe80::1%eth0]",
            "user@localhost",
            "KISETSU.LAN",
            "kisetsu-pc:8085",
        ] {
            assert!(guard.host_allowed(good), "{good}");
        }
        for bad in [
            "",
            "localhost.",
            "::1",
            "[::1]x",
            "[localhost]",
            "127.1",
            "localhost@evil.example",
            "evil.example/@localhost",
            " localhost",
            "notlocalhost",
        ] {
            assert!(!guard.host_allowed(bad), "{bad:?}");
        }
    }

    #[test]
    fn a_write_must_come_from_a_page_served_by_this_host() {
        let guard = guard();
        let check = |origin: &str| {
            guard.check("POST", "/api/refresh", Some("localhost:8085"), Some(origin))
        };
        assert_eq!(check("http://localhost:8085/path"), Ok(None));
        assert_eq!(check("//localhost:8085"), Ok(None));
        assert_eq!(check("http://localhost"), Ok(Some(Refusal::Origin)));
        assert_eq!(check("null"), Ok(Some(Refusal::Origin)));
        assert_eq!(check("http://[::1"), Err(Malformed));
    }
}
