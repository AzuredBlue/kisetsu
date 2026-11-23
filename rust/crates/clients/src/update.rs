//! New releases on GitHub: finding the latest one, and downloading its program for this system
//! after checking it against the release's signed list of SHA-256 sums.

use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::time::Duration;

use minisign_verify::{PublicKey, Signature};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::traffic::{self, Outcome};

const USER_AGENT: &str = "Kisetsu (update check)";
/// The traffic kind of these requests.
const KIND: &str = "Update";
const SUMS: &str = "SHA256SUMS";
const SUMS_SIGNATURE: &str = "SHA256SUMS.minisig";
const MAX_RELEASE_BYTES: u64 = 1 << 20;
const MAX_SUMS_BYTES: u64 = 64 << 10;
const MAX_PROGRAM_BYTES: u64 = 128 << 20;

/// The program a release has for this system, by the target the release workflow builds.
const TARGET: Option<&str> = if cfg!(all(windows, target_arch = "x86_64")) {
    Some("x86_64-pc-windows-msvc")
} else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
    Some("x86_64-unknown-linux-gnu")
} else {
    None
};

#[derive(Debug, thiserror::Error)]
pub enum UpdateError {
    #[error("GitHub could not be reached: {0}")]
    Unreachable(String),
    #[error("GitHub answered {0}.")]
    Status(u16),
    #[error("GitHub's answer could not be read.")]
    Unreadable,
    #[error("This release has no download for this system.")]
    NoDownload,
    #[error("The release's signature does not match Kisetsu's key, so it was not installed.")]
    BadSignature,
    #[error("The download does not match the release's signed checksum, so it was not installed.")]
    Mismatch,
    #[error("The download is larger than a Kisetsu program can be.")]
    TooLarge,
    #[error("The new version could not be saved: {0}")]
    Save(#[from] std::io::Error),
}

/// The latest release.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Release {
    /// Its version without the leading `v`, like `0.2.0`.
    pub version: String,
    /// Its page on GitHub.
    pub page: String,
    /// Where the program for this system, the sums and their signature are, when the release has
    /// all three.
    files: Option<Files>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Files {
    program_name: String,
    program: String,
    sums: String,
    signature: String,
}

impl Release {
    /// Whether it has a program for this system to install.
    pub fn installable(&self) -> bool {
        self.files.is_some()
    }
}

#[derive(Deserialize)]
struct Answer {
    tag_name: String,
    html_url: String,
    #[serde(default)]
    assets: Vec<AnswerAsset>,
}

#[derive(Deserialize)]
struct AnswerAsset {
    name: String,
    browser_download_url: String,
}

/// The name of the program a release tagged `tag` has for `target`.
fn program_name(tag: &str, target: &str) -> String {
    let suffix = if target.contains("windows") {
        ".exe"
    } else {
        ""
    };
    format!("kisetsu-{tag}-{target}{suffix}")
}

fn release_of(answer: Answer, target: Option<&str>) -> Release {
    let url_of = |name: &str| {
        answer
            .assets
            .iter()
            .find(|asset| asset.name == name)
            .map(|asset| asset.browser_download_url.clone())
    };
    let files = target.and_then(|target| {
        let program_name = program_name(&answer.tag_name, target);
        Some(Files {
            program: url_of(&program_name)?,
            sums: url_of(SUMS)?,
            signature: url_of(SUMS_SIGNATURE)?,
            program_name,
        })
    });
    Release {
        version: answer
            .tag_name
            .strip_prefix('v')
            .unwrap_or(&answer.tag_name)
            .to_owned(),
        page: answer.html_url,
        files,
    }
}

fn numbers(version: &str) -> Option<Vec<u64>> {
    version
        .split('.')
        .map(|part| part.parse().ok())
        .collect::<Option<Vec<u64>>>()
        .filter(|parts| !parts.is_empty())
}

/// Whether `candidate` is a later version than `current`; one that is not plain numbers is never
/// later.
pub fn is_newer(candidate: &str, current: &str) -> bool {
    match (numbers(candidate), numbers(current)) {
        (Some(candidate), Some(current)) => candidate > current,
        _ => false,
    }
}

/// The SHA-256 the list gives for `name`, in lower-case hex.
fn listed_sum(sums: &str, name: &str) -> Option<String> {
    sums.lines().find_map(|line| {
        let (sum, listed) = line.split_once(char::is_whitespace)?;
        let listed = listed.trim_start();
        let listed = listed.strip_prefix('*').unwrap_or(listed);
        (listed == name).then(|| sum.to_ascii_lowercase())
    })
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Reads releases from GitHub.
pub struct Releases {
    agent: ureq::Agent,
    latest_url: String,
}

impl Releases {
    /// `latest_url` answers with the latest release, as GitHub's `releases/latest` does.
    pub fn new(latest_url: &str) -> Self {
        let timeout = Duration::from_secs(20);
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .user_agent(USER_AGENT)
            // Checks are a day apart.
            .max_idle_connections(0)
            .timeout_connect(Some(timeout))
            .timeout_per_read(Some(timeout))
            .build()
            .new_agent();
        Self {
            agent,
            latest_url: latest_url.to_owned(),
        }
    }

    fn get(&self, url: &str) -> Result<ureq::Body, UpdateError> {
        let host = traffic::host_of(url);
        let response = self
            .agent
            .get(url)
            .header(
                "accept",
                "application/vnd.github+json, application/octet-stream",
            )
            .call()
            .map_err(|error| {
                traffic::record(KIND, &host, Outcome::Error);
                UpdateError::Unreachable(error.to_string())
            })?;
        let status = response.status().as_u16();
        traffic::record(KIND, &host, Outcome::of_status(status));
        if status >= 300 {
            return Err(UpdateError::Status(status));
        }
        Ok(response.into_body())
    }

    fn get_small(&self, url: &str, limit: u64) -> Result<Vec<u8>, UpdateError> {
        self.get(url)?
            .with_config()
            .limit(limit)
            .read_to_vec()
            .map_err(|error| match error {
                ureq::Error::BodyExceedsLimit(_) => UpdateError::TooLarge,
                error => UpdateError::Unreachable(error.to_string()),
            })
    }

    pub fn latest(&self) -> Result<Release, UpdateError> {
        let bytes = self.get_small(&self.latest_url, MAX_RELEASE_BYTES)?;
        let answer: Answer = serde_json::from_slice(&bytes).map_err(|_| UpdateError::Unreadable)?;
        Ok(release_of(answer, TARGET))
    }

    /// Downloads the release's program into `into` and checks it. A file that fails a check is
    /// removed.
    pub fn download(&self, release: &Release, key: &str, into: &Path) -> Result<(), UpdateError> {
        let files = release.files.as_ref().ok_or(UpdateError::NoDownload)?;
        let sums = self.get_small(&files.sums, MAX_SUMS_BYTES)?;
        let signature = self.get_small(&files.signature, MAX_SUMS_BYTES)?;
        verify_sums(key, &sums, &signature)?;
        let expected = listed_sum(&String::from_utf8_lossy(&sums), &files.program_name)
            .ok_or(UpdateError::NoDownload)?;
        let outcome = self.save(&files.program, into).and_then(|sum| {
            if sum == expected {
                Ok(())
            } else {
                Err(UpdateError::Mismatch)
            }
        });
        if outcome.is_err() {
            let _ = std::fs::remove_file(into);
        }
        outcome
    }

    /// Writes the program to `into` a piece at a time and returns its SHA-256.
    fn save(&self, url: &str, into: &Path) -> Result<String, UpdateError> {
        let mut body = self.get(url)?;
        let mut reader = body.with_config().limit(MAX_PROGRAM_BYTES).reader();
        let mut file = File::create(into)?;
        let mut hasher = Sha256::new();
        let mut buffer = vec![0; 64 << 10];
        loop {
            let read = match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => read,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    let cause = error
                        .into_inner()
                        .and_then(|inner| inner.downcast::<ureq::Error>().ok());
                    return Err(match cause.map(|cause| *cause) {
                        Some(ureq::Error::BodyExceedsLimit(_)) => UpdateError::TooLarge,
                        Some(cause) => UpdateError::Unreachable(cause.to_string()),
                        None => UpdateError::Unreachable("the download stopped".to_owned()),
                    });
                }
            };
            hasher.update(&buffer[..read]);
            file.write_all(&buffer[..read])?;
        }
        file.sync_all()?;
        Ok(hex(&hasher.finalize()))
    }
}

/// Checks the minisign signature of the sums against `key`, a public key in base64.
fn verify_sums(key: &str, sums: &[u8], signature: &[u8]) -> Result<(), UpdateError> {
    let key = PublicKey::from_base64(key.trim()).map_err(|_| UpdateError::BadSignature)?;
    let signature = Signature::decode(&String::from_utf8_lossy(signature))
        .map_err(|_| UpdateError::BadSignature)?;
    // Both kinds minisign writes: the file's hash signed, or (older releases) the file itself.
    key.verify(sums, &signature, true)
        .map_err(|_| UpdateError::BadSignature)
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    #[test]
    fn versions_compare_by_their_numbers() {
        assert!(is_newer("0.2.0", "0.1.9"));
        assert!(is_newer("0.10.0", "0.9.0"));
        assert!(is_newer("1.0", "0.9.9"));
        assert!(!is_newer("0.1.0", "0.1.0"));
        assert!(!is_newer("0.1.0", "0.2.0"));
        assert!(!is_newer("0.2.0-rc1", "0.1.0"));
    }

    #[test]
    fn a_release_finds_the_program_for_its_target() {
        let answer: Answer = serde_json::from_str(
            r#"{"tag_name":"v0.2.0","html_url":"https://github.com/o/r/releases/tag/v0.2.0","assets":[
            {"name":"kisetsu-v0.2.0-x86_64-pc-windows-msvc.exe","browser_download_url":"https://d/win"},
            {"name":"kisetsu-v0.2.0-x86_64-unknown-linux-gnu","browser_download_url":"https://d/linux"},
            {"name":"SHA256SUMS","browser_download_url":"https://d/sums"},
            {"name":"SHA256SUMS.minisig","browser_download_url":"https://d/sig"}]}"#,
        )
        .unwrap();
        let release = release_of(answer, Some("x86_64-pc-windows-msvc"));
        assert_eq!(release.version, "0.2.0");
        let files = release.files.unwrap();
        assert_eq!(files.program, "https://d/win");
        assert_eq!(
            files.program_name,
            "kisetsu-v0.2.0-x86_64-pc-windows-msvc.exe"
        );

        let answer: Answer =
            serde_json::from_str(r#"{"tag_name":"v0.2.0","html_url":"h","assets":[]}"#).unwrap();
        assert!(!release_of(answer, Some("x86_64-unknown-linux-gnu")).installable());
    }

    #[test]
    fn sums_are_read_in_either_mode_of_sha256sum() {
        let sums = "AB12  kisetsu-v1-a\ncd34 *kisetsu-v1-b.exe\n";
        assert_eq!(listed_sum(sums, "kisetsu-v1-a").as_deref(), Some("ab12"));
        assert_eq!(
            listed_sum(sums, "kisetsu-v1-b.exe").as_deref(),
            Some("cd34")
        );
        assert_eq!(listed_sum(sums, "kisetsu-v1"), None);
    }

    /// A key made for this test only, and its signature of `abc  kisetsu-v9-x\n`.
    const TEST_KEY: &str = "RWTvOsbAHxrPX7w6hfYeH9MgHzE0PFFy6/7lyDjDXZL4/Bf8LNMNUaxU";
    const TEST_SIGNATURE: &str = r#"untrusted comment: test
RUTvOsbAHxrPX4HR9pkSeNDlm6APNHaQ4Dxk0gQyV1zpGSf65LiXnINtHwOD/QQMOZx9xEw8exU68evyymVaLwB/cVqkIDkZLg4=
trusted comment: timestamp:0	file:sums
NDDf35YUK9/I80pqEL+KOubjOEu9bLIwgEGl3KcrqNrJMb2qz51mIm7DG3nHgf89HLEcyP+7OKwOpGsMadOVAQ==
"#;

    #[test]
    fn only_sums_signed_by_the_key_are_trusted() {
        let sums = b"abc  kisetsu-v9-x\n";
        assert!(verify_sums(TEST_KEY, sums, TEST_SIGNATURE.as_bytes()).is_ok());
        assert!(matches!(
            verify_sums(TEST_KEY, b"abd  kisetsu-v9-x\n", TEST_SIGNATURE.as_bytes()),
            Err(UpdateError::BadSignature)
        ));
        assert!(matches!(
            verify_sums("not a key", sums, TEST_SIGNATURE.as_bytes()),
            Err(UpdateError::BadSignature)
        ));
    }
}
