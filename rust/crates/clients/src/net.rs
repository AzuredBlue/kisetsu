//! What went wrong with an HTTP request, by cause.

use std::io::ErrorKind;

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
