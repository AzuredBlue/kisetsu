//! Newer releases: whether there is one, and installing it.

use serde_json::json;

use super::ok;
use crate::app::Call;
use crate::error::{ApiError, ApiResult};

pub(super) fn get(call: &mut Call<'_, '_>) -> ApiResult {
    let status = call.runtime().updates.status();
    ok(&json!({
        "version": env!("CARGO_PKG_VERSION"),
        "latest": status.latest,
        "page": status.page,
        "blocked": status.blocked,
    }))
}

/// Answers once the new version is downloaded and checked; the app then restarts into it.
pub(super) fn install(call: &mut Call<'_, '_>) -> ApiResult {
    call.runtime()
        .updates
        .install()
        .map_err(ApiError::Conflict)?;
    ok(&json!({"restarting": true}))
}
