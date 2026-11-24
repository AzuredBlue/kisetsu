//! What went wrong with an HTTP request, by cause.

use std::io::ErrorKind;
use std::net::{Ipv4Addr, Ipv6Addr, SocketAddr, ToSocketAddrs, UdpSocket};

/// Why a request produced no answer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Failure {
    /// Nothing arrived within the allowed time.
    Timeout,
    /// The host name could not be resolved.
    HostNotFound,
    /// The host was found but would not take the connection.
    Refused,
    /// The secure connection could not be set up.
    Tls(String),
    /// Anything else, with the transport's own words.
    Other(String),
}

impl Failure {
    /// Whether the failure happened while connecting, before any request was sent.
    pub(crate) fn is_connect(&self) -> bool {
        matches!(self, Self::HostNotFound | Self::Refused)
    }
}

pub(crate) fn classify(error: &ureq::Error) -> Failure {
    use ureq::Error;
    match error {
        Error::Timeout(_) => Failure::Timeout,
        Error::HostNotFound => Failure::HostNotFound,
        Error::ConnectionFailed => Failure::Refused,
        Error::Io(io) => match io.kind() {
            ErrorKind::TimedOut | ErrorKind::WouldBlock => Failure::Timeout,
            ErrorKind::ConnectionRefused
            | ErrorKind::ConnectionReset
            | ErrorKind::ConnectionAborted
            | ErrorKind::NotConnected
            | ErrorKind::AddrNotAvailable
            | ErrorKind::HostUnreachable
            | ErrorKind::NetworkUnreachable => Failure::Refused,
            _ => Failure::Other(error.to_string()),
        },
        Error::Tls(_) | Error::Rustls(_) | Error::Pem(_) => Failure::Tls(error.to_string()),
        _ => Failure::Other(error.to_string()),
    }
}

/// Whether this machine is online: AniList's name resolves and the system has a route to it.
/// Nothing is sent to AniList, as connecting a UDP socket only picks the route. Behind a proxy
/// the names may only resolve at the proxy, so the machine is taken to be online.
pub fn online() -> bool {
    if ureq::Proxy::try_from_env().is_some() {
        return true;
    }
    let Ok(addresses) = (crate::anilist::HOST, 443).to_socket_addrs() else {
        return false;
    };
    addresses.into_iter().any(|address| {
        let local: SocketAddr = if address.is_ipv4() {
            (Ipv4Addr::UNSPECIFIED, 0).into()
        } else {
            (Ipv6Addr::UNSPECIFIED, 0).into()
        };
        UdpSocket::bind(local)
            .and_then(|socket| socket.connect(address))
            .is_ok()
    })
}
