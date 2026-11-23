//! Serving the app over HTTP.

use std::io::{self, Write};
use std::net::TcpListener;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};

use percent_encoding::percent_decode_str;
use socket2::SockRef;
use tiny_http::{Header, Method, Response as WireResponse, StatusCode};
use tracing::{debug, warn};

use crate::app::App;
use crate::http::{Body, Request, Response};

/// How many requests are answered at the same time. A stream is written on a thread of its own,
/// so it never takes one of these. Each worker keeps its own scratch space for every pattern it
/// searches with, so a few are kept.
const WORKERS: usize = 3;

#[derive(Debug, thiserror::Error)]
pub enum ServeError {
    #[error("Cannot listen on {address}: {reason}")]
    Listen { address: String, reason: String },
    #[error("Cannot start a thread: {0}")]
    Thread(#[from] io::Error),
}

/// A running server. It stops when [`Self::stop`] is called or when it is dropped.
pub struct Server {
    listener: Arc<tiny_http::Server>,
    stopping: Arc<AtomicBool>,
    workers: Vec<JoinHandle<()>>,
}

impl Server {
    /// Starts answering requests to `app` on `host:port`.
    pub fn start(app: Arc<App>, host: &str, port: u16) -> Result<Self, ServeError> {
        let address = format!("{host}:{port}");
        let listen_error = |reason: String| ServeError::Listen {
            address: address.clone(),
            reason,
        };
        let socket =
            TcpListener::bind(&address).map_err(|error| listen_error(error.to_string()))?;
        // Accepted connections inherit this. Without it a response written in two parts waits for
        // the client's delayed ACK, about 40 ms, on every request after the first on a connection.
        SockRef::from(&socket)
            .set_tcp_nodelay(true)
            .map_err(|error| listen_error(error.to_string()))?;
        let listener = tiny_http::Server::from_listener(socket, None)
            .map_err(|error| listen_error(error.to_string()))?;
        let listener = Arc::new(listener);
        let stopping = Arc::new(AtomicBool::new(false));
        let mut workers = Vec::with_capacity(WORKERS);
        for number in 0..WORKERS {
            let (app, listener, stopping) = (
                Arc::clone(&app),
                Arc::clone(&listener),
                Arc::clone(&stopping),
            );
            workers.push(
                thread::Builder::new()
                    .name(format!("kisetsu-web-{number}"))
                    .spawn(move || work(&app, &listener, &stopping))?,
            );
        }
        Ok(Self {
            listener,
            stopping,
            workers,
        })
    }

    /// Stops taking requests and waits for the ones being answered. Open streams end when the
    /// engine's change bus is shut down.
    pub fn stop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        // Each call wakes one worker waiting for a request, now or once it is done with one.
        for _ in &self.workers {
            self.listener.unblock();
        }
        for worker in self.workers.drain(..) {
            let _ = worker.join();
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Answers requests until the server is stopped. A waiting worker sleeps until a request comes.
fn work(app: &Arc<App>, listener: &tiny_http::Server, stopping: &AtomicBool) {
    loop {
        match listener.recv() {
            Ok(request) => answer(app, request),
            Err(_) if stopping.load(Ordering::Acquire) => break,
            Err(error) => {
                warn!("The web server stopped listening: {error}");
                break;
            }
        }
    }
}

fn header_pairs(request: &tiny_http::Request) -> Vec<(String, String)> {
    request
        .headers()
        .iter()
        .map(|header| {
            (
                header.field.as_str().as_str().to_ascii_lowercase(),
                header.value.as_str().to_owned(),
            )
        })
        .collect()
}

fn answer(app: &Arc<App>, mut request: tiny_http::Request) {
    let headers = header_pairs(&request);
    let target = request.url().to_owned();
    let (raw_path, raw_query) = target.split_once('?').unwrap_or((&target, ""));
    let response = {
        let mut call = Request {
            method: request.method().as_str().to_owned(),
            path: percent_decode_str(raw_path)
                .decode_utf8_lossy()
                .into_owned(),
            raw_path: raw_path.to_owned(),
            raw_query: raw_query.to_owned(),
            headers,
            body: request.as_reader(),
        };
        app.handle(&mut call)
    };
    if let Err(error) = send(request, response) {
        debug!("A response was not delivered: {error}");
    }
}

fn wire_headers(headers: &[(String, String)]) -> Vec<Header> {
    headers
        .iter()
        .filter_map(|(name, value)| Header::from_bytes(name.as_bytes(), value.as_bytes()).ok())
        .collect()
}

fn send(request: tiny_http::Request, response: Response) -> io::Result<()> {
    let headers = wire_headers(&response.headers);
    let status = StatusCode(response.status);
    match response.body {
        Body::Empty => request.respond(WireResponse::new(
            status,
            headers,
            io::empty(),
            Some(0),
            None,
        )),
        Body::Bytes(bytes) => {
            let length = bytes.len();
            request.respond(WireResponse::new(
                status,
                headers,
                io::Cursor::new(bytes),
                Some(length),
                None,
            ))
        }
        Body::Reader { reader, length } => {
            let length = usize::try_from(length).ok();
            request.respond(WireResponse::new(status, headers, reader, length, None))
        }
        Body::Events(events) => {
            if *request.method() == Method::Head {
                return request.respond(WireResponse::new(
                    status,
                    headers,
                    io::empty(),
                    Some(0),
                    None,
                ));
            }
            // A stream stays open as long as the page does: it gets a thread of its own.
            thread::Builder::new()
                .name("kisetsu-web-stream".into())
                .spawn(move || {
                    if let Err(error) = stream(request, &response.headers, response.status, events)
                    {
                        debug!("An event stream ended: {error}");
                    }
                })
                .map(drop)
        }
    }
}

/// Writes events as they come, each flushed on its own, until the stream ends or the client
/// leaves.
fn stream(
    request: tiny_http::Request,
    headers: &[(String, String)],
    status: u16,
    events: Box<dyn Iterator<Item = Vec<u8>> + Send>,
) -> io::Result<()> {
    let mut out = request.into_writer();
    let mut head = format!("HTTP/1.1 {status} OK\r\n");
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("transfer-encoding: chunked\r\n\r\n");
    out.write_all(head.as_bytes())?;
    out.flush()?;
    for event in events {
        write!(out, "{:x}\r\n", event.len())?;
        out.write_all(&event)?;
        out.write_all(b"\r\n")?;
        out.flush()?;
    }
    out.write_all(b"0\r\n\r\n")?;
    out.flush()
}
