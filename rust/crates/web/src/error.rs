//! Why a request was not answered with what it asked for, and how that reaches the UI.

use kisetsu_engine::Error as EngineError;
use kisetsu_services::Error as ServiceError;
use serde_json::json;
use tracing::error;

use crate::check::Problems;
use crate::http::Response;

#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    /// 404: what the request names is not there.
    #[error("{0}")]
    NotFound(String),
    /// 400: the request is well formed but cannot be done; the text says why.
    #[error("{0}")]
    Invalid(String),
    /// 409: the show is locked to its feed.
    #[error("{0}")]
    FeedLocked(String),
    /// 409: the request cannot be done while something else is going on; the text says what.
    #[error("{0}")]
    Conflict(String),
    /// 502: qBittorrent did not do it.
    #[error("qBittorrent: {0}")]
    Qbit(String),
    /// 502: another service the app talks to did not answer.
    #[error("{0}")]
    Upstream(String),
    /// 422: the request's parts do not fit the route.
    #[error("the request is not valid")]
    Unprocessable(Problems),
    /// 500: a failure of our own.
    #[error("{0}")]
    Internal(String),
}

impl ApiError {
    pub fn into_response(self) -> Response {
        let detail = |status: u16, text: &str| Response::json(status, &json!({"detail": text}));
        match self {
            Self::NotFound(text) => detail(404, &text),
            Self::Invalid(text) => detail(400, &text),
            Self::FeedLocked(text) => {
                Response::json(409, &json!({"detail": text, "code": "feed_locked"}))
            }
            Self::Conflict(text) => detail(409, &text),
            Self::Qbit(text) => detail(502, &format!("qBittorrent: {text}")),
            Self::Upstream(text) => detail(502, &text),
            Self::Unprocessable(problems) => Response::json(422, &problems.to_json()),
            Self::Internal(reason) => {
                error!("A request failed: {reason}");
                internal_error()
            }
        }
    }
}

/// The answer to a request that failed for a reason of the app's own.
pub fn internal_error() -> Response {
    Response::text(500, "text/plain; charset=utf-8", "Internal Server Error")
}

impl From<ServiceError> for ApiError {
    fn from(error: ServiceError) -> Self {
        match error {
            ServiceError::Qbit(error) => Self::Qbit(error.to_string()),
            ServiceError::FeedLocked { .. } => Self::FeedLocked(error.to_string()),
            ServiceError::NotFound(what) => Self::NotFound(what),
            ServiceError::Invalid(why) => Self::Invalid(why),
            ServiceError::FeedUrlTaken { .. }
            | ServiceError::Refused(_)
            | ServiceError::ShowRefused(_)
            | ServiceError::Backup(_)
            | ServiceError::AddShowRefused(_) => Self::Invalid(error.to_string()),
            ServiceError::Database(_) | ServiceError::NoQbit | ServiceError::NoTagSource => {
                Self::Internal(error.to_string())
            }
        }
    }
}

impl From<EngineError> for ApiError {
    fn from(error: EngineError) -> Self {
        match error {
            EngineError::Service(error) => error.into(),
            EngineError::JobsRunning(_) => Self::Conflict(error.to_string()),
            other => Self::Internal(other.to_string()),
        }
    }
}

pub type ApiResult = Result<Response, ApiError>;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::http::Body;

    #[test]
    fn jobs_still_running_answer_409_with_the_jobs_named() {
        let error = ApiError::from(EngineError::JobsRunning(vec![
            "grab".to_owned(),
            "backfill".to_owned(),
        ]));
        let response = error.into_response();
        assert_eq!(response.status, 409);
        let Body::Bytes(bytes) = response.body else {
            panic!("the answer has a body");
        };
        let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            body,
            json!({"detail": "A job is still running (grab, backfill); try again in a moment."})
        );
    }
}
