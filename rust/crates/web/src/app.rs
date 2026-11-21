//! The app: the guard, the routes and the files, as one function from request to response.

use std::io::{self, Read};
use std::sync::Mutex;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{Value, json};

use crate::api;
use crate::assets::Assets;
use crate::check::{Place, Problem, Problems};
use crate::error::{ApiError, ApiResult, internal_error};
use crate::guard::Guard;
use crate::http::{Body, KEEPALIVE, Request, Response};
use crate::runtime::Runtime;

/// The largest request body a route that reads JSON accepts.
const JSON_BODY_LIMIT: u64 = 1024 * 1024;
/// Where the UI's files are served.
const STATIC_PREFIX: &str = "/static/";

/// A route's handler. It answers from the request, or says why it cannot.
pub type Handler = fn(&mut Call<'_, '_>) -> ApiResult;

pub(crate) struct Route {
    method: &'static str,
    pattern: &'static str,
    handler: Handler,
}

/// The routes, in the order they are tried: the first whose path and method both fit answers.
pub(crate) const fn route(method: &'static str, pattern: &'static str, handler: Handler) -> Route {
    Route {
        method,
        pattern,
        handler,
    }
}

/// How a path fits a pattern.
enum Fit {
    No,
    /// The parts of the path that stand for the pattern's `{names}`.
    Yes(Vec<String>),
}

/// The values a pattern's `{names}` stand for in `path`, if the path has the pattern's shape.
fn fit(pattern: &str, path: &str) -> Fit {
    let mut names = pattern.split('/');
    let mut parts = path.split('/');
    let mut values = Vec::new();
    loop {
        match (names.next(), parts.next()) {
            (None, None) => return Fit::Yes(values),
            (Some(name), Some(part)) if name.starts_with('{') && !part.is_empty() => {
                values.push(part.to_owned());
            }
            (Some(name), Some(part)) if name == part => {}
            _ => return Fit::No,
        }
    }
}

/// What a handler is given.
pub struct Call<'r, 'b> {
    pub app: &'r App,
    pub request: &'r mut Request<'b>,
    /// What the pattern's `{names}` stand for, in order.
    pub params: Vec<String>,
}

impl Call<'_, '_> {
    pub fn runtime(&self) -> &Runtime {
        &self.app.runtime
    }

    /// The query as decoded name and value pairs.
    pub fn query(&self) -> Vec<(String, String)> {
        self.request.query()
    }

    /// The whole body, refusing one larger than `limit` bytes.
    pub fn read_body(&mut self, limit: u64) -> io::Result<Option<Vec<u8>>> {
        let mut bytes = Vec::new();
        let read = self
            .request
            .body
            .take(limit.saturating_add(1))
            .read_to_end(&mut bytes)?;
        Ok((u64::try_from(read).unwrap_or(u64::MAX) <= limit).then_some(bytes))
    }

    /// The body as the JSON object a route takes. Anything else is noted in `problems`.
    pub fn object(&mut self, problems: &mut Problems) -> Option<Value> {
        let body = match self.read_body(JSON_BODY_LIMIT) {
            Ok(Some(body)) => body,
            _ => {
                problems.push(body_problem("missing", "Field required", Value::Null, None));
                return None;
            }
        };
        if body.is_empty() {
            problems.push(body_problem("missing", "Field required", Value::Null, None));
            return None;
        }
        // Without a content type the body is taken as plain text, not as JSON.
        let json = self.request.header("content-type").is_some_and(|kind| {
            let kind = kind
                .split(';')
                .next()
                .unwrap_or_default()
                .trim()
                .to_lowercase();
            kind == "application/json"
                || (kind.starts_with("application/") && kind.ends_with("+json"))
        });
        if !json {
            let text = String::from_utf8_lossy(&body).into_owned();
            problems.push(body_problem(
                "model_attributes_type",
                "Input should be a valid dictionary or object to extract fields from",
                Value::String(text),
                None,
            ));
            return None;
        }
        match serde_json::from_slice::<Value>(&body) {
            Ok(value) if value.is_object() => Some(value),
            Ok(value) => {
                problems.push(body_problem(
                    "model_attributes_type",
                    "Input should be a valid dictionary or object to extract fields from",
                    value,
                    None,
                ));
                None
            }
            Err(error) => {
                let ended = error.classify() == serde_json::error::Category::Eof;
                let offset = error_offset(&body, error.line(), error.column(), ended);
                problems.push(Problem {
                    kind: "json_invalid",
                    place: vec![Place::Key("body".to_owned()), Place::Index(offset)],
                    message: "JSON decode error".to_owned(),
                    input: json!({}),
                    context: Some(json!({"error": error.to_string()})),
                });
                None
            }
        }
    }
}

fn body_problem(
    kind: &'static str,
    message: &str,
    input: Value,
    context: Option<Value>,
) -> Problem {
    Problem {
        kind,
        place: vec![Place::Key("body".to_owned())],
        message: message.to_owned(),
        input,
        context,
    }
}

/// Where in the text a JSON error was found, as a count of characters before it. A text that
/// ends too soon is at fault at its end, which is where the column counts up to.
fn error_offset(body: &[u8], line: usize, column: usize, ended: bool) -> usize {
    let text = String::from_utf8_lossy(body);
    let before: usize = text
        .split_inclusive('\n')
        .take(line.saturating_sub(1))
        .map(|line| line.chars().count())
        .sum();
    before
        + if ended {
            column
        } else {
            column.saturating_sub(1)
        }
}

/// The app. Share it between threads.
pub struct App {
    pub runtime: Runtime,
    guard: Guard,
    assets: Assets,
    keepalive: Duration,
    /// Work a request started and answered before it was done.
    background: Mutex<Vec<JoinHandle<()>>>,
}

impl App {
    pub fn new(runtime: Runtime, guard: Guard, assets: Assets) -> Self {
        Self {
            runtime,
            guard,
            assets,
            keepalive: KEEPALIVE,
            background: Mutex::new(Vec::new()),
        }
    }

    /// How long an event stream waits for a message before it writes a comment.
    #[must_use]
    pub fn with_keepalive(mut self, keepalive: Duration) -> Self {
        self.keepalive = keepalive;
        self
    }

    pub fn keepalive(&self) -> Duration {
        self.keepalive
    }

    /// Runs `work` on a thread of its own, after the request that asked for it has been answered.
    pub fn spawn_background(&self, work: impl FnOnce() + Send + 'static) {
        let handle = thread::spawn(work);
        let mut running = self
            .background
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        running.retain(|started| !started.is_finished());
        running.push(handle);
    }

    /// Waits for the work requests started in the background.
    pub fn wait_for_background(&self) {
        let running = std::mem::take(
            &mut *self
                .background
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        );
        for handle in running {
            let _ = handle.join();
        }
    }

    /// Answers one request.
    pub fn handle(&self, request: &mut Request<'_>) -> Response {
        let refusal = self.guard.check(
            &request.method,
            &request.path,
            request.header("host"),
            request.header("origin"),
        );
        match refusal {
            Err(_) => return internal_error(),
            Ok(Some(refusal)) => return Response::json(403, &json!({"detail": refusal.reason()})),
            Ok(None) => {}
        }
        self.route(request)
    }

    fn route(&self, request: &mut Request<'_>) -> Response {
        let routes = api::ROUTES;
        let path = request.path.clone();
        let method = request.method.clone();
        if let Some(rest) = path.strip_prefix(STATIC_PREFIX) {
            return match method.as_str() {
                "GET" | "HEAD" => self.head_only(&method, self.assets.file(rest, request)),
                _ => method_not_allowed("GET, HEAD"),
            };
        }
        if path == "/" {
            return match method.as_str() {
                "GET" => self.assets.page(),
                _ => method_not_allowed("GET"),
            };
        }
        let mut partial: Option<&Route> = None;
        for route in routes {
            if let Fit::Yes(params) = fit(route.pattern, &path) {
                if route.method == method {
                    let mut call = Call {
                        app: self,
                        request,
                        params,
                    };
                    return (route.handler)(&mut call).unwrap_or_else(ApiError::into_response);
                }
                partial.get_or_insert(route);
            }
        }
        if let Some(route) = partial {
            return method_not_allowed(route.method);
        }
        self.missing(request, routes)
    }

    /// A path with or without its trailing slash may be a route: send the client there. Anything
    /// else is not found.
    fn missing(&self, request: &Request<'_>, routes: &[Route]) -> Response {
        let path = &request.path;
        let other = match path.strip_suffix('/') {
            Some(trimmed) => trimmed.to_owned(),
            None => format!("{path}/"),
        };
        let exists = other.starts_with(STATIC_PREFIX)
            || routes
                .iter()
                .any(|route| matches!(fit(route.pattern, &other), Fit::Yes(_)));
        if !exists {
            return Response::json(404, &json!({"detail": "Not Found"}));
        }
        let host = request.header("host").unwrap_or_default();
        let query = if request.raw_query.is_empty() {
            String::new()
        } else {
            format!("?{}", request.raw_query)
        };
        Response::new(307, Body::Empty).with("location", format!("http://{host}{other}{query}"))
    }

    /// A response to a `HEAD` request has the headers of the `GET` one and no body.
    fn head_only(&self, method: &str, response: Response) -> Response {
        if method == "HEAD" {
            return Response {
                body: Body::Empty,
                ..response
            };
        }
        response
    }
}

fn method_not_allowed(allow: &str) -> Response {
    Response::json(405, &json!({"detail": "Method Not Allowed"})).with("allow", allow)
}
