//! Exporting everything to one file, and restoring from it.

use std::fs::File;
use std::io::{self, Read, Write};
use std::path::Path;
use std::time::UNIX_EPOCH;

use kisetsu_services::Error;
use kisetsu_services::backup::{self, BackupError, MAX_BACKUP_BYTES, Staged, Summary};
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use tempfile::TempDir;

use super::{ok, poke};
use crate::app::Call;
use crate::assets::http_date;
use crate::check::query_value;
use crate::error::{ApiError, ApiResult};
use crate::http::{Body, Response};

/// The upload is the request body, so the password travels beside it, percent-encoded as headers
/// are Latin-1.
const PASSWORD_HEADER: &str = "x-kisetsu-qbit-password";

/// A file that is deleted along with its folder once it has been read.
struct Disposable {
    file: File,
    _folder: TempDir,
}

impl Read for Disposable {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.file.read(buffer)
    }
}

pub(super) fn export(call: &mut Call<'_, '_>) -> ApiResult {
    let pairs = call.request.query();
    let theme = query_value(&pairs, "theme").map(str::to_owned);
    let folder = tempfile::Builder::new()
        .prefix("kisetsu-export-")
        .tempdir()
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    let target = folder.path().join("backup.zip");
    let runtime = call.runtime();
    // On the engine thread, so no job writes halfway through the copy.
    let written = target.clone();
    runtime.engine.dry_run(move |ctx| {
        backup::write_backup(ctx, &written, theme.as_deref()).map_err(Error::from)
    })?;
    let file = File::open(&target).map_err(|error| ApiError::Internal(error.to_string()))?;
    let meta = file
        .metadata()
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    let length = meta.len();
    let modified = meta
        .modified()
        .ok()
        .and_then(|time| time.duration_since(UNIX_EPOCH).ok());
    let seconds = modified.map_or(0, |elapsed| i64::try_from(elapsed.as_secs()).unwrap_or(0));
    let etag = format!(
        "\"{length:x}-{:x}\"",
        modified.map_or(0, |elapsed| elapsed.as_nanos())
    );
    let day = runtime.engine.now().strftime("%Y-%m-%d");
    Ok(Response::new(
        200,
        Body::Reader {
            reader: Box::new(Disposable {
                file,
                _folder: folder,
            }),
            length,
        },
    )
    .with("content-type", "application/zip")
    .with("accept-ranges", "bytes")
    .with("etag", etag)
    .with("last-modified", http_date(seconds))
    .with(
        "content-disposition",
        format!("attachment; filename=\"kisetsu-backup-{day}.zip\""),
    ))
}

/// Stores the uploaded file and checks it.
fn stage(call: &mut Call<'_, '_>, scratch: &Path) -> Result<Staged, ApiError> {
    let archive = scratch.join("upload.zip");
    let mut out = File::create(&archive).map_err(|error| ApiError::Internal(error.to_string()))?;
    let copied = io::copy(
        &mut call.request.body.take(MAX_BACKUP_BYTES.saturating_add(1)),
        &mut out,
    )
    .map_err(|error| ApiError::Internal(error.to_string()))?;
    out.flush()
        .map_err(|error| ApiError::Internal(error.to_string()))?;
    if copied > MAX_BACKUP_BYTES {
        return Err(Error::from(BackupError::NotABackup).into());
    }
    backup::unpack(&archive, scratch).map_err(|error| Error::from(error).into())
}

fn summary_json(summary: &Summary) -> Value {
    json!({
        "shows": summary.shows,
        "feeds": summary.feeds,
        "downloaded": summary.downloaded,
        "qbit_host": summary.qbit_host,
        "anilist_username": summary.anilist_username,
    })
}

fn scratch() -> Result<TempDir, ApiError> {
    tempfile::Builder::new()
        .prefix("kisetsu-restore-")
        .tempdir()
        .map_err(|error| ApiError::Internal(error.to_string()))
}

/// What is in this file and what restoring it would replace. Changes nothing.
pub(super) fn inspect(call: &mut Call<'_, '_>) -> ApiResult {
    let scratch = scratch()?;
    let staged = stage(call, scratch.path())?;
    let preview = call
        .runtime()
        .engine
        .read(|ctx| backup::preview(ctx, &staged))?;
    ok(&json!({
        "exported_at": preview.exported_at,
        "backup": summary_json(&preview.backup),
        "current": summary_json(&preview.current),
        "password_needed": preview.password_needed,
    }))
}

pub(super) fn restore(call: &mut Call<'_, '_>) -> ApiResult {
    let password = call
        .request
        .header(PASSWORD_HEADER)
        .map(|encoded| percent_decode_str(encoded).decode_utf8_lossy().into_owned())
        .unwrap_or_default();
    let scratch = scratch()?;
    let staged = stage(call, scratch.path())?;
    let runtime = call.runtime();
    let engine = &runtime.engine;
    let restored = runtime
        .jobs
        .while_stopped(|| engine.call(move |ctx| backup::restore(ctx, &staged, &password)))?;
    // The startup pass skips a recent sync, but the list in memory may be another user's.
    poke(call, "sync_anilist");
    ok(&json!({
        "status": "success",
        "saved_as": restored.saved_as,
        "theme": restored.theme,
        "shows": restored.shows,
    }))
}
