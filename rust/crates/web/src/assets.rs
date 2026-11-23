//! The UI's files: the page, and everything under `/static`.

use std::borrow::Cow;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::time::UNIX_EPOCH;

use jiff::Timestamp;
use jiff::fmt::rfc2822;
use serde_json::json;
use sha1_smol::Sha1;

use crate::http::{Body, Request, Response};

/// The folders whose scripts the page imports; each module is mapped to its URL with its hash.
const MODULE_DIRS: [&str; 2] = ["js", "vendor"];
/// Where the page takes the import map that gives every module its hash, so only what changed
/// is fetched again.
const IMPORT_MAP_MARK: &str = "@@importmap@@";
const HASH_MARK_START: &str = "@@v:";
const HASH_MARK_END: &str = "@@";

/// A year, for files whose URL carries their hash.
const CACHE_FOREVER: &str = "public, max-age=31536000, immutable";

include!(concat!(env!("OUT_DIR"), "/embedded.rs"));

/// The UI's files: built into the program, or read from a folder.
#[derive(Debug, Clone)]
pub struct Assets {
    dir: Option<PathBuf>,
}

/// A file's bytes, the tag that changes with them, and when it last changed if that is known.
struct Found {
    bytes: Cow<'static, [u8]>,
    etag: String,
    modified: Option<i64>,
}

impl Assets {
    /// The files in `dir`, read on every request, so changes show without a rebuild.
    pub fn new(dir: PathBuf) -> Self {
        Self { dir: Some(dir) }
    }

    /// The files built into the program.
    pub fn embedded() -> Self {
        Self { dir: None }
    }

    fn built_in(relative: &str) -> Option<&'static (&'static str, &'static [u8], &'static str)> {
        let relative = relative.trim_start_matches('/');
        EMBEDDED.iter().find(|(path, _, _)| *path == relative)
    }

    /// The first ten hex digits of the SHA-1 of the file, or `0` when it cannot be read.
    fn digest(&self, relative: &str) -> String {
        let Some(dir) = &self.dir else {
            return Self::built_in(relative)
                .map_or("0", |(_, _, digest)| digest)
                .to_owned();
        };
        locate(dir, relative)
            .and_then(|path| fs::read(path).ok())
            .map_or_else(
                || "0".to_owned(),
                |bytes| {
                    Sha1::from(bytes)
                        .digest()
                        .to_string()
                        .chars()
                        .take(10)
                        .collect()
                },
            )
    }

    /// The paths of the scripts under [`MODULE_DIRS`].
    fn modules(&self) -> Vec<String> {
        let Some(dir) = &self.dir else {
            return EMBEDDED
                .iter()
                .map(|(path, _, _)| *path)
                .filter(|path| {
                    path.ends_with(".js")
                        && MODULE_DIRS.iter().any(|folder| {
                            path.strip_prefix(folder)
                                .is_some_and(|rest| rest.starts_with('/'))
                        })
                })
                .map(str::to_owned)
                .collect();
        };
        let mut modules = Vec::new();
        for folder in MODULE_DIRS {
            collect_scripts(&dir.join(folder), &mut modules);
        }
        modules
            .iter()
            .filter_map(|path| path.strip_prefix(dir).ok())
            .map(slashed)
            .collect()
    }

    fn read(&self, relative: &str) -> Option<Found> {
        let Some(dir) = &self.dir else {
            let (_, bytes, digest) = Self::built_in(relative)?;
            return Some(Found {
                bytes: Cow::Borrowed(bytes),
                etag: format!("\"{digest}\""),
                modified: None,
            });
        };
        let path = locate(dir, relative)?;
        let meta = fs::metadata(&path).ok()?;
        let bytes = fs::read(&path).ok()?;
        let modified = meta
            .modified()
            .ok()
            .and_then(|time| time.duration_since(UNIX_EPOCH).ok());
        Some(Found {
            bytes: Cow::Owned(bytes),
            etag: format!(
                "\"{:x}-{:x}\"",
                meta.len(),
                modified.map_or(0, |elapsed| elapsed.as_nanos())
            ),
            modified: Some(
                modified.map_or(0, |elapsed| i64::try_from(elapsed.as_secs()).unwrap_or(0)),
            ),
        })
    }

    /// Every module under [`MODULE_DIRS`] mapped to its URL with its hash, so the modules a
    /// module imports are versioned too.
    fn import_map(&self) -> String {
        let mut urls: Vec<String> = self
            .modules()
            .iter()
            .map(|relative| format!("/static/{relative}"))
            .collect();
        urls.sort();
        let entries: Vec<String> = urls
            .iter()
            .map(|url| {
                let versioned = format!(
                    "{url}?v={}",
                    self.digest(url.trim_start_matches("/static/"))
                );
                format!("{}: {}", json!(url), json!(versioned))
            })
            .collect();
        format!("{{\"imports\": {{{}}}}}", entries.join(", "))
    }

    /// `index.html` with every `@@v:path@@` replaced by a hash of that file and `@@importmap@@`
    /// by the import map, so a deploy busts the browser cache.
    pub fn page(&self) -> Response {
        let Some(found) = self.read("index.html") else {
            return not_found();
        };
        // The page reads the same whatever line endings the files were checked out with.
        let raw = String::from_utf8_lossy(&found.bytes).replace("\r\n", "\n");
        let mut html = String::with_capacity(raw.len());
        let mut rest = raw.as_str();
        while let Some(start) = rest.find(HASH_MARK_START) {
            let after = &rest[start + HASH_MARK_START.len()..];
            let marked = after.find(HASH_MARK_END).map(|end| &after[..end]);
            match marked.filter(|path| is_mark_path(path)) {
                Some(path) => {
                    html.push_str(&rest[..start]);
                    html.push_str(&self.digest(path));
                    rest = &after[path.len() + HASH_MARK_END.len()..];
                }
                None => {
                    html.push_str(&rest[..start + HASH_MARK_START.len()]);
                    rest = after;
                }
            }
        }
        html.push_str(rest);
        if html.contains(IMPORT_MAP_MARK) {
            html = html.replace(IMPORT_MAP_MARK, &self.import_map());
        }
        Response::text(200, "text/html; charset=utf-8", html)
            .with("cache-control", "no-store, must-revalidate")
    }

    /// A file under `/static`: served for a year when the URL carries its hash, and revalidated
    /// otherwise. Honours ranges and conditional requests.
    pub fn file(&self, relative: &str, request: &Request<'_>) -> Response {
        let Some(Found {
            bytes,
            etag,
            modified,
        }) = self.read(relative)
        else {
            return not_found();
        };
        let versioned = request.raw_query.contains("v=");
        let cache = if versioned { CACHE_FOREVER } else { "no-cache" };
        if is_unchanged(request, &etag, modified) {
            return Response::new(304, Body::Empty)
                .with("cache-control", cache)
                .with("etag", etag);
        }
        let size = bytes.len();
        let base = |response: Response| {
            let response = response
                .with("accept-ranges", "bytes")
                .with("cache-control", cache)
                .with("etag", etag.clone());
            match modified {
                Some(seconds) => response.with("last-modified", http_date(seconds)),
                None => response,
            }
        };
        let content_type = content_type_of(Path::new(relative));
        match request.header("range").map(|range| byte_range(range, size)) {
            Some(ByteRange::Unsatisfiable) => {
                return Response::text(416, "text/plain; charset=utf-8", "Range Not Satisfiable")
                    .with("content-range", format!("bytes */{size}"));
            }
            Some(ByteRange::Part(start, end)) => {
                let part = bytes.get(start..=end).unwrap_or_default().to_vec();
                return base(
                    Response::new(206, Body::Bytes(part))
                        .with("content-type", content_type)
                        .with("content-range", format!("bytes {start}-{end}/{size}")),
                );
            }
            Some(ByteRange::Whole) | None => {}
        }
        base(Response::new(200, Body::Bytes(bytes.into_owned())).with("content-type", content_type))
    }
}

/// The file a path under `/static` names, if it is one inside `dir`.
fn locate(dir: &Path, relative: &str) -> Option<PathBuf> {
    let mut path = dir.to_path_buf();
    for part in Path::new(relative.trim_start_matches('/')).components() {
        match part {
            Component::Normal(name) if !name.to_string_lossy().contains(['\\', ':']) => {
                path.push(name);
            }
            _ => return None,
        }
    }
    path.is_file().then_some(path)
}

fn not_found() -> Response {
    Response::json(404, &json!({"detail": "Not Found"}))
}

/// A path a hash mark may name: letters, digits, dots, slashes and dashes.
fn is_mark_path(path: &str) -> bool {
    !path.is_empty()
        && path
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '/' | '-'))
}

fn slashed(path: &Path) -> String {
    path.components()
        .map(|part| part.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

fn collect_scripts(folder: &Path, found: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(folder) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            collect_scripts(&path, found);
        } else if path.extension().is_some_and(|extension| extension == "js") {
            found.push(path);
        }
    }
}

/// The types the UI's files are served with; the same on every platform.
fn content_type_of(path: &Path) -> &'static str {
    match path.extension().and_then(|extension| extension.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("js") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        Some("json") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("ico") => "image/x-icon",
        Some("txt") => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

pub(crate) fn http_date(seconds: i64) -> String {
    Timestamp::from_second(seconds)
        .map(|at| at.strftime("%a, %d %b %Y %H:%M:%S GMT").to_string())
        .unwrap_or_default()
}

/// Whether the client holds the current file: its `ETag`, or else the time it was fetched after
/// the file last changed.
fn is_unchanged(request: &Request<'_>, etag: &str, modified: Option<i64>) -> bool {
    if let Some(held) = request.header("if-none-match") {
        return held
            .split(',')
            .map(|tag| tag.trim().trim_start_matches("W/"))
            .any(|tag| tag == etag || tag == "*");
    }
    request
        .header("if-modified-since")
        .and_then(|date| rfc2822::parse(without_weekday(date)).ok())
        .is_some_and(|date| {
            modified.is_some_and(|modified| modified <= date.timestamp().as_second())
        })
}

/// The date without its day name, which is not checked against the date.
fn without_weekday(date: &str) -> &str {
    date.split_once(',')
        .map_or(date, |(_, rest)| rest.trim_start())
}

enum ByteRange {
    /// A range the file has no bytes for.
    Unsatisfiable,
    /// The first and last byte, both included.
    Part(usize, usize),
    /// Not a single byte range: the whole file is sent.
    Whole,
}

/// One range of a `Range: bytes=` header.
fn byte_range(header: &str, size: usize) -> ByteRange {
    let Some(spec) = header.trim().strip_prefix("bytes=") else {
        return ByteRange::Whole;
    };
    if spec.contains(',') {
        return ByteRange::Whole;
    }
    let Some((first, last)) = spec.split_once('-') else {
        return ByteRange::Whole;
    };
    let last_byte = size.saturating_sub(1);
    let (start, end) = match (first.trim().parse::<usize>(), last.trim().parse::<usize>()) {
        (Ok(start), Ok(end)) => (start, end.min(last_byte)),
        (Ok(start), Err(_)) if last.trim().is_empty() => (start, last_byte),
        (Err(_), Ok(suffix)) if first.trim().is_empty() && suffix > 0 => {
            (size.saturating_sub(suffix), last_byte)
        }
        _ => return ByteRange::Whole,
    };
    if size == 0 || start > end || start >= size {
        ByteRange::Unsatisfiable
    } else {
        ByteRange::Part(start, end)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_range_is_read_from_its_header() {
        assert!(matches!(
            byte_range("bytes=0-9", 100),
            ByteRange::Part(0, 9)
        ));
        assert!(matches!(
            byte_range("bytes=90-", 100),
            ByteRange::Part(90, 99)
        ));
        assert!(matches!(
            byte_range("bytes=-10", 100),
            ByteRange::Part(90, 99)
        ));
        assert!(matches!(
            byte_range("bytes=0-999", 100),
            ByteRange::Part(0, 99)
        ));
        assert!(matches!(
            byte_range("bytes=200-", 100),
            ByteRange::Unsatisfiable
        ));
        assert!(matches!(byte_range("bytes=0-1,5-6", 100), ByteRange::Whole));
        assert!(matches!(byte_range("items=0-1", 100), ByteRange::Whole));
    }

    #[test]
    fn hash_marks_name_plain_paths_only() {
        assert!(is_mark_path("css/tokens.css"));
        assert!(!is_mark_path("a b"));
        assert!(!is_mark_path(""));
    }
}
