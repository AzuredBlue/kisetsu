//! The regexes that find a show's releases among feed titles, and the folder a show saves into.

use std::env;
use std::fs;
use std::path::{Component, Path, PathBuf, is_separator};
use std::sync::LazyLock;

use crate::parsing::{is_digit, is_number, number};
use crate::rx::{Case, Regex};

const ROMAN_TO_INT: [(&str, i64); 6] = [
    ("I", 1),
    ("II", 2),
    ("III", 3),
    ("IV", 4),
    ("V", 5),
    ("VI", 6),
];
const MIN_PATTERN_CHARS: usize = 6;
/// Release groups join the words of a title with spaces, dots, underscores or hyphens (a feed title
/// and the torrent's file name often differ), so matching ignores which.
const NAME_SEPARATORS: &str = r"[\s._\-:–—/]+";

/// A character `NAME_SEPARATORS` matches.
fn is_name_separator(c: char) -> bool {
    c.is_whitespace() || "._-:–—/".contains(c)
}
const WORD_START: &str = r"(?<![A-Za-z0-9])";
/// An alias this short (an acronym like "SBR") is only trusted as a whole word.
const SHORT_ALIAS_CHARS: usize = 4;

static SEASON_MARK: LazyLock<Regex> = LazyLock::new(|| {
    Regex::literal(
        r"\b(?:season|s)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+season\b|\b(?:season|part)\s+(I|II|III|IV|V|VI)\b|\b(II|III|IV|V|VI)\b",
        Case::Insensitive,
    )
});
static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::literal(r"\s+", Case::Sensitive));
static DANGLING_SEPARATORS: LazyLock<Regex> =
    LazyLock::new(|| Regex::literal(r"^[\s:\-_–—/]+|[\s:\-_–—/]+$", Case::Sensitive));
static METACHARACTER: LazyLock<Regex> =
    LazyLock::new(|| Regex::literal(r"([\\^$.|?*+()\[\]{}])", Case::Sensitive));
static SLASH_IN_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::literal(r"(\w+)[/](\w+)", Case::Sensitive));
static SLASH: LazyLock<Regex> = LazyLock::new(|| Regex::literal(r"[/\\|]", Case::Sensitive));
static COLON: LazyLock<Regex> = LazyLock::new(|| Regex::literal(r":\s*", Case::Sensitive));
static FORBIDDEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::literal(r#"[<>:"/\\|?*\x00-\x1f]"#, Case::Sensitive));
static DOUBLE_DASH: LazyLock<Regex> =
    LazyLock::new(|| Regex::literal(r"\s*-\s*-\s*", Case::Sensitive));

/// The season number a roman numeral stands for.
fn roman_to_int(numeral: &str) -> Option<i64> {
    ROMAN_TO_INT
        .iter()
        .find(|(roman, _)| *roman == numeral)
        .map(|&(_, number)| number)
}

fn int_to_roman(number: i64) -> Option<&'static str> {
    ROMAN_TO_INT
        .iter()
        .find(|&&(_, value)| value == number)
        .map(|&(roman, _)| roman)
}

/// Common release group season abbreviations of an alias ("Mushoku Tensei S3", "Mushoku Tensei
/// Season 3").
pub fn generate_season_variants(alias: &str) -> Vec<String> {
    let mut variants = vec![alias.to_owned()];
    let Some(mark) = SEASON_MARK.find(alias) else {
        return variants;
    };
    // Whichever alternative matched is the only group that captured anything.
    let Some(token) = (1..=4)
        .filter_map(|index| mark.group(index))
        .find(|t| !t.is_empty())
    else {
        return variants;
    };
    let season = if is_number(token) {
        number(token)
    } else {
        roman_to_int(&token.to_uppercase()).unwrap_or(0)
    };
    if season == 0 {
        return variants;
    }

    // Cut the season off the whole alias. Separators inside a title ("Re:Zero", "Kaguya-sama",
    // "Fate/stay night") are part of the name, so the base is never truncated at one; only
    // dangling ones left by the removal are trimmed.
    let base = SEASON_MARK.replace_all(alias, "");
    let base = SPACES.replace_all(&base, " ");
    let base = DANGLING_SEPARATORS.replace_all(base.trim(), "");
    let base = base.trim();

    if base.chars().count() >= 3 {
        let tag = if season < 10 { "S0?" } else { "S" };
        variants.push(format!("{base} {tag}{season}"));
        variants.push(format!("{base} Season {season}"));
        variants.push(format!("{base} {season}"));
        if let Some(roman) = int_to_roman(season) {
            variants.push(format!("{base} {roman}"));
        }
    }
    variants
}

/// The lookahead that keeps a name's trailing season number from matching a longer one.
///
/// "Foo 3" must not match "Foo 30" and "Foo II" must not match "Foo III", but "Foo 3 - 01" and
/// "Foo S03E01" still match.
pub fn name_end_guard(name: &str) -> &'static str {
    let stripped = name.trim();
    if stripped.chars().next_back().is_some_and(is_digit) {
        return r"(?!\d)";
    }
    let words: Vec<&str> = stripped.split_whitespace().collect();
    match words.as_slice() {
        [_, .., last] if roman_to_int(&last.to_uppercase()).is_some() => r"(?![A-Za-z0-9])",
        _ => "",
    }
}

/// A letter, not a character such as a roman numeral that only counts as alphabetic.
fn is_letter(c: char) -> bool {
    c.is_alphabetic() && !c.is_numeric()
}

fn short_end_guard(alias: &str) -> &'static str {
    let stripped = alias.trim();
    let letters_and_digits = stripped.chars().filter(char::is_ascii_alphanumeric).count();
    if letters_and_digits <= SHORT_ALIAS_CHARS
        && stripped.chars().next_back().is_some_and(is_letter)
    {
        r"(?![A-Za-z0-9])"
    } else {
        ""
    }
}

/// Escapes true regex metacharacters while keeping spaces and hyphens clean and human-readable.
pub fn sanitize_regex_token(title: &str) -> String {
    let protected = title
        .replace("(0?)", "\0ZERO_OPT_P\0")
        .replace("0?", "\0ZERO_OPT\0");
    let escaped = METACHARACTER
        .replace_all(protected.trim(), r"\$1")
        .replace("\0ZERO_OPT_P\0", "(0?)")
        .replace("\0ZERO_OPT\0", "0?");
    SPACES.replace_all(&escaped, " ").into_owned()
}

/// A regex from a release name observed on a feed.
///
/// The name already showed how the release group writes the title, so the pattern is that name
/// with only regex metacharacters escaped. The episode number and quality tags are not part of the
/// name, which keeps the pattern valid for every later episode of the same show.
pub fn build_release_name_pattern(release_name: &str) -> String {
    let stripped = release_name.trim();
    sanitize_regex_token(stripped) + name_end_guard(stripped)
}

/// A regex for a series name that matches whichever separators a release uses.
///
/// Only the words are fixed; "Blue Box S02", "Blue.Box.S02" and "Blue_Box_S02" all match, while a
/// missing separator ("BlueBox") does not.
pub fn build_tolerant_name_pattern(name: &str) -> String {
    let stripped = name.trim();
    let tokens: Vec<&str> = stripped
        .split(is_name_separator)
        .filter(|token| !token.is_empty())
        .collect();
    match tokens.as_slice() {
        [] => return String::new(),
        [_] => return build_release_name_pattern(stripped),
        _ => {}
    }
    let escaped: Vec<String> = tokens
        .iter()
        .map(|token| {
            let mut safe = sanitize_regex_token(token).replace('\'', "['\u{2019}]?");
            if is_number(token) {
                // "Foo 3" must not match "Foo 30".
                safe.push_str(r"\b");
            }
            safe
        })
        .collect();
    let mut pattern = escaped.join(NAME_SEPARATORS);
    if tokens.last().is_some_and(|last| !is_number(last)) {
        pattern.push_str(name_end_guard(&tokens.join(" ")));
    }
    pattern
}

/// (pattern that finds a show's releases, the learned-name pattern or None).
///
/// The learned name is how the release group writes the title, so it is tried alongside the
/// aliases.
pub fn show_match_patterns(
    aliases: &[impl AsRef<str>],
    matched_title: Option<&str>,
) -> (String, Option<String>) {
    let pattern = build_regex_pattern(aliases, None);
    let learned = matched_title.unwrap_or_default().trim();
    if learned.chars().count() < MIN_PATTERN_CHARS {
        return (pattern, None);
    }
    let learned_pattern = build_tolerant_name_pattern(learned);
    (
        format!("{learned_pattern}|{pattern}"),
        Some(learned_pattern),
    )
}

/// The case-insensitive pattern that finds a show's releases among feed titles.
///
/// If `matched_title` is known, the pattern is that name; otherwise it is an alternation of the
/// aliases.
pub fn build_regex_pattern(aliases: &[impl AsRef<str>], matched_title: Option<&str>) -> String {
    if let Some(matched_title) = matched_title.filter(|title| !title.trim().is_empty()) {
        // The release group has already shown its naming, so the learned rule is exactly that
        // name. Season hedges ("S2", "Season 2", "II") belong on the armed path below, where the
        // naming is still unknown.
        return build_release_name_pattern(matched_title);
    }

    let valid: Vec<&str> = aliases
        .iter()
        .map(|alias| alias.as_ref().trim())
        .filter(|alias| !alias.is_empty())
        .collect();
    let latin: Vec<&str> = valid
        .iter()
        .copied()
        .filter(|alias| alias.chars().any(|c| c.is_ascii_alphanumeric()))
        .collect();
    let target = if latin.is_empty() { valid } else { latin };
    if target.is_empty() {
        return ".*".to_owned();
    }

    let mut expanded = Vec::new();
    for alias in target {
        expanded.extend(generate_season_variants(alias));
        // "[Oshi no Ko] 3rd Season" is written "Oshi no Ko S3" by release groups: the brackets are
        // part of the name only.
        let spaced = alias.replace(['[', ']'], " ");
        let unbracketed = SPACES.replace_all(&spaced, " ");
        let unbracketed = unbracketed.trim();
        if unbracketed != alias && unbracketed.chars().count() >= MIN_PATTERN_CHARS {
            expanded.extend(generate_season_variants(unbracketed));
        }
    }

    // A variant ending in a season number ("Foo 3", "Foo S03", "Foo II") must not match a longer
    // one ("Foo 30", "Foo III"). A lookahead rather than \b, so "Foo S03E01" still matches; it is
    // appended after escaping so it stays a regex. A name never starts inside another word, and a
    // short one ("SBR") never ends inside one either.
    let mut tokens: Vec<String> = Vec::new();
    for alias in &expanded {
        let token = format!(
            "{WORD_START}{}{}{}",
            sanitize_regex_token(alias),
            name_end_guard(alias),
            short_end_guard(alias)
        );
        if !tokens.contains(&token) {
            tokens.push(token);
        }
    }
    format!("({})", tokens.join("|"))
}

/// A show's title as a safe, valid, cross-platform folder name.
///
/// - Replaces slashes between words or fractions ('Ranma 1/2' -> 'Ranma 1-2', 'Fate/stay' ->
///   'Fate-stay')
/// - Replaces remaining slashes and backslashes with ' - '
/// - Replaces colons with ' - '
/// - Removes forbidden filesystem characters: < > : " / \ | ? * and control chars
/// - Collapses multiple hyphens and whitespace
/// - Strips leading/trailing dots and spaces
pub fn sanitize_folder_name(name: &str) -> String {
    let s = SLASH_IN_NAME.replace_all(name, "$1-$2");
    let s = SLASH.replace_all(&s, " - ");
    let s = COLON.replace_all(&s, " - ");
    let s = FORBIDDEN.replace_all(&s, "");
    let s = DOUBLE_DASH.replace_all(&s, " - ");
    let s = SPACES.replace_all(&s, " ");
    match s.trim_matches([' ', '.']) {
        "" => "Anime".to_owned(),
        s => s.to_owned(),
    }
}

/// A path inside the home directory `home` with that prefix written as '~', for display.
pub fn compress_home_path(path: &str, home: &str) -> String {
    let stripped = path.trim();
    if stripped == home {
        return "~".to_owned();
    }
    match stripped.strip_prefix(home) {
        Some(rest) if rest.starts_with(['/', '\\']) => format!("~{rest}"),
        _ => path.to_owned(),
    }
}

/// The absolute folder a show saves into, with '~' meaning the home directory `home`.
///
/// A custom folder is used when set: `{name}` in it becomes the show's folder name, and a relative
/// one goes under `base_dir`. Otherwise the show's folder name fills `base_dir`'s `{name}`, or is
/// appended to it. With neither set it is "", so qBittorrent uses its default location.
pub fn resolve_save_path(
    base_dir: &str,
    display_name: &str,
    custom_save_folder: Option<&str>,
    home: &str,
) -> String {
    let clean_name = sanitize_folder_name(display_name);
    let base_template = base_dir.trim();

    if let Some(custom) = custom_save_folder
        .map(str::trim)
        .filter(|custom| !custom.is_empty())
    {
        let raw = if custom.contains("{name}") {
            custom.replace("{name}", &clean_name)
        } else if is_absolute(custom) {
            custom.to_owned()
        } else if base_template.is_empty() {
            // A relative subfolder ("Bleach Season 2") with no base folder to put it in.
            clean_name
        } else {
            let prefix = base_template
                .replace("{name}", "")
                .trim_end_matches(['/', '\\'])
                .to_owned();
            format!("{prefix}/{custom}")
        };
        return if is_absolute(&raw) {
            resolve(&raw, home)
        } else {
            raw
        };
    }

    if base_template.is_empty() {
        return String::new();
    }
    let expanded = if base_template.contains("{name}") {
        base_template.replace("{name}", &clean_name)
    } else {
        join(base_template, &clean_name)
    };
    resolve(&expanded, home)
}

/// Whether a saved folder is taken as absolute: rooted, under '~', or after a drive letter.
fn is_absolute(raw: &str) -> bool {
    raw.starts_with(['/', '~']) || (raw.chars().count() > 2 && raw.chars().nth(1) == Some(':'))
}

/// `name` under `base`, for a name without separators or a drive.
fn join(base: &str, name: &str) -> String {
    let bare_drive = cfg!(windows) && base.len() == 2 && base.ends_with(':');
    if base.ends_with(is_separator) || bare_drive {
        format!("{base}{name}")
    } else {
        format!("{base}{}{name}", std::path::MAIN_SEPARATOR)
    }
}

/// A leading '~' alone or before a separator is the home directory. A
/// '~user' form is left as it is.
fn expand_user(path: &str, home: &str) -> String {
    match path.strip_prefix('~') {
        Some(rest) if rest.is_empty() || rest.starts_with(is_separator) => format!("{home}{rest}"),
        _ => path.to_owned(),
    }
}

/// The absolute, resolved form of a path with a leading '~' expanded.
///
/// The path is made absolute against the working directory and `.` and `..` are resolved by its
/// text. Then the longest leading part that exists is replaced by its real path
/// (links followed, the case on disk), and the parts that don't exist are kept as written.
fn resolve(path: &str, home: &str) -> String {
    let expanded = PathBuf::from(expand_user(path, home));
    let absolute = if expanded.is_absolute() {
        expanded
    } else {
        std::path::absolute(&expanded)
            .or_else(|_| env::current_dir().map(|cwd| cwd.join(&expanded)))
            .unwrap_or(expanded)
    };
    let mut normal = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normal.pop();
            }
            other => normal.push(other),
        }
    }
    real_path(&normal).to_string_lossy().into_owned()
}

/// The path with its longest existing leading part replaced by that part's real path.
fn real_path(path: &Path) -> PathBuf {
    let mut missing = Vec::new();
    let mut existing = path;
    loop {
        if let Ok(real) = fs::canonicalize(existing) {
            let mut real = without_verbatim_prefix(real);
            real.extend(missing.iter().rev());
            return real;
        }
        match (existing.parent(), existing.file_name()) {
            (Some(parent), Some(name)) => {
                missing.push(name);
                existing = parent;
            }
            _ => return path.to_path_buf(),
        }
    }
}

/// A Windows real path without the `\\?\` prefix the system adds (`\\?\UNC\` becomes `\\`).
fn without_verbatim_prefix(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(unc) = text.strip_prefix(r"\\?\UNC\") {
        PathBuf::from(format!(r"\\{unc}"))
    } else if let Some(local) = text.strip_prefix(r"\\?\") {
        PathBuf::from(local)
    } else {
        path
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_regex_written_in_the_code_compiles() {
        let all = [
            &SEASON_MARK,
            &SPACES,
            &DANGLING_SEPARATORS,
            &METACHARACTER,
            &SLASH_IN_NAME,
            &SLASH,
            &COLON,
            &FORBIDDEN,
            &DOUBLE_DASH,
        ];
        assert!(all.iter().all(|regex| regex.compiled()));
    }
}
