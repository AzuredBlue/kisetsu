//! Release titles: what a title says about itself (name, episode, season, group, version,
//! resolution, episode range), and how closely its name matches a show's.

use std::collections::HashSet;
use std::sync::LazyLock;

use crate::fuzz;
use crate::patterns::build_regex_pattern;
use crate::rx::{Case, Match, Regex};

/// The value of a decimal digit: ASCII, or full-width as Japanese titles write them ("Season２").
fn digit_value(c: char) -> Option<u32> {
    c.to_digit(10).or_else(|| {
        ('０'..='９')
            .contains(&c)
            .then(|| u32::from(c) - u32::from('０'))
    })
}

pub(crate) fn is_digit(c: char) -> bool {
    digit_value(c).is_some()
}

/// Whether the text is made of digits only.
pub(crate) fn is_number(text: &str) -> bool {
    !text.is_empty() && text.chars().all(is_digit)
}

/// The value of a run of digits; a number too large for an `i64`, or written with any other
/// digits, is `i64::MAX`.
pub(crate) fn number(digits: &str) -> i64 {
    digits
        .chars()
        .try_fold(0_i64, |value, c| {
            let digit = i64::from(digit_value(c)?);
            Some(value.saturating_mul(10).saturating_add(digit))
        })
        .unwrap_or(i64::MAX)
}

/// Whether the character is part of a word.
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The fuzzy score from which a release's name is taken for a show's.
pub const FUZZY_MATCH_THRESHOLD: f64 = 85.0;
/// The lowest resolution a release may name to be downloaded, unless the user picks another.
pub const DEFAULT_MIN_RESOLUTION: i64 = 1080;

const STOPWORDS: &[&str] = &[
    "the", "a", "an", "no", "wa", "ga", "to", "de", "ni", "la", "le", "el",
];
/// Words that only say which season or part a title is; two titles sharing nothing else are not
/// one show.
const SEASON_WORDS: &[&str] = &["season", "part", "cour", "final", "stage"];
const ROMAN_NUMERALS: &[(&str, &str)] = &[
    ("x", "10"),
    ("ix", "9"),
    ("viii", "8"),
    ("vii", "7"),
    ("vi", "6"),
    ("v", "5"),
    ("iv", "4"),
    ("iii", "3"),
    ("ii", "2"),
];
const NON_GROUP_TAGS: &[&str] = &[
    "1080p",
    "720p",
    "480p",
    "540p",
    "576p",
    "2160p",
    "4k",
    "hevc",
    "x264",
    "x265",
    "avc",
    "h264",
    "h265",
    "aac",
    "flac",
    "opus",
    "web-dl",
    "webrip",
    "hdtv",
    "dvd",
    "bd",
    "bluray",
    "cr",
    "multisub",
    "multi-sub",
    "multi-subs",
    "multisubs",
    "eng",
    "english",
    "raw",
    "raws",
    "sub",
    "subs",
    "dub",
    "dual-audio",
    "batch",
    "dl",
];
/// Widths of a frame and the lines a full frame that wide has.
const LINES_BY_WIDTH: &[(i64, i64)] = &[(3840, 2160), (1920, 1080), (1280, 720)];
/// Which of `RANGE_REGEXES` finds a range in brackets.
const BRACKETED_RANGE: usize = 1;
const SAME_NAME_SCORE: f64 = 85.0;
/// Words a release adds to a show's name without naming anything by them.
const NAME_FILLERS: &[&str] = &["episode", "ep", "tv", "anime", "uncut", "dub", "dubbed"];
const ROMAN_SEASONS: &[(&str, i64)] = &[("ii", 2), ("iii", 3), ("iv", 4), ("v", 5), ("vi", 6)];

/// Patterns written in the code, each compiled on first use.
macro_rules! regexes {
    ($($(#[$doc:meta])* $name:ident = $pattern:expr, $flags:ident;)*) => {
        $(
            $(#[$doc])*
            static $name: LazyLock<Regex> =
                LazyLock::new(|| Regex::literal($pattern, Case::$flags));
        )*

        #[cfg(test)]
        fn written_regexes() -> Vec<&'static Regex> {
            let mut all = vec![$(&*$name),*];
            all.extend(RANGE_REGEXES.iter());
            all.extend(SEASON_NAME_PATTERNS.iter());
            all
        }
    };
}

regexes! {
    VERSION_REGEX = r"(E\d+|\b\d+)v\d+", Insensitive;
    ROMAN_NUMERAL = r"\b(x|ix|viii|vii|vi|v|iv|iii|ii)\b", Sensitive;
    BRACKETED = r"\[.*?\]|\(.*?\)", Sensitive;
    ORDINAL_SEASON = r"\b(\d+)(?:st|nd|rd|th)\s+season\b", Sensitive;
    SHORT_SEASON = r"\bs(\d+)\b", Sensitive;
    PUNCTUATION = r#"[:\-_/\\.|+~'\"`]"#, Sensitive;
    /// "SPY×FAMILY" is "Spy x Family".
    TIMES = r"[×✕]", Sensitive;
    /// Only letters and numbers make up a name. The "△" of "Yuru Camp△" and the accents or vowel
    /// marks that follow a letter as characters of their own separate words instead.
    NOT_WORD = r"[^\p{L}\p{N}\s]", Sensitive;
    SPACES = r"\s+", Sensitive;
    GROUP_AT_START = r"^\[(.*?)\]", Sensitive;
    /// A scene group ends the name ("...H.264-VARYG.mkv"); a hyphen earlier belongs to the title
    /// or to "WEB-DL".
    SCENE_GROUP = r"-([A-Za-z0-9_]+)(?:\s*\(|\s*\[|\s*\.|\s*$)", Sensitive;
    BRACKET_CONTENT = r"\[(.*?)\]", Sensitive;
    VERSION_TAG = r"(?:E\d+|\b\d+)v(\d+)\b|[\[(]v(\d+)[\])]|\b(?:v|ver\.?|version)\s*(\d+)\b", Insensitive;
    REPACK_OR_PROPER = r"\bREPACK\d*\b|\bPROPER\d*\b", Insensitive;
    REPACK_NUMBER = r"\bREPACK(\d+)\b", Insensitive;
    COUNTS_SOMETHING_ELSE = r"\b(?:season|part|cour|vol(?:ume)?)\.?$", Insensitive;
    /// A standalone episode number. A "." after it is a file extension, never a fraction ("12.5"
    /// is a recap, not episode 12). "more" is set when an episode title (" - The Hero's Party",
    /// " - 2 Heroes") or a language tag ("VOSTFR") follows the number.
    EPISODE_NUMBER = concat!(
        r"(?P<pre>\s*-\s*|\s+)(?:EP?|#)?(?P<num>\d{1,4})(?:\s+(?:END|FINAL|REPACK\d*|PROPER\d*))?",
        r"(?:\s*\(|\s*\[|\s*\.(?!\d)|\s*$|(?P<more>(?=\s+-\s+(?:[^\d\s]|\d+\s+[^\W\d_])|\s+(?:VOSTFR|VOSTA|VOSTEN|VF|RAW)\b)))",
    ), Insensitive;
    VIDEO_EXTENSION = r"\.(?:mkv|mp4|avi|webm|mov|m4v|ts)$", Insensitive;
    AUDIO_CHANNELS = r"\b(?:DDP?|EAC-?3|AC3|AAC|FLAC|OPUS|DTS(?:-HD)?|TrueHD|PCM|DD\+?|Dolby|Atmos)?\s*(?:[1-7]\.[0-2]|2\.0|5\.1|7\.1)\b", Insensitive;
    FRAME_RATE = r"\b\d+(?:\.\d+)?\s*fps\b", Insensitive;
    BIT_DEPTH = r"\b\d{1,2}\s*-?\s*bits?\b", Insensitive;
    QUALITY_TAGS = r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|Uncensored|Batch|Complete)\b", Insensitive;
    DUAL_NUMBERING = r"-\s*(\d{1,4})\s*\(\s*S(\d{1,2})E(\d{1,4})\s*\)", Insensitive;
    SEASON_EPISODE = r"\bS(\d{1,2})\s*[-_.]?\s*E(\d{1,4})\b", Insensitive;
    SEASON_DASH_EPISODE = r"\b(?:S|Season\s*)(\d{1,2})\s*-\s*(\d{1,4})\b", Insensitive;
    SEASON_ONLY = r"\b(?:S|Season\s*)(\d{1,2})\b", Insensitive;
    YEAR = r"\b(?:19|20)\d{2}\b", Sensitive;
    TRAILING_TAG = concat!(
        r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|AVC|HEVC|x264|x265|H\.?264|H\.?265|AV1|VP9|AAC|FLAC|OPUS|DDP?|EAC3|AC3|TrueHD|PCM|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|DUAL|NF|CR|AMZN|HIDIVE|BILI|REPACK\d?|PROPER|Uncensored|Batch|Complete)\b",
        r"[\s\-_|/:\(\[\{.]*$",
    ), Insensitive;
    TRAILING_SEPARATORS = r"[\s\-_|/:\(\[\{.]+$", Sensitive;
    LEADING_SEPARATORS = r"^[\s\-_|/:\)\]\}\.]+", Sensitive;
    RESOLUTION = r"\b(2160|1080|720|576|540|480|360)p\b|\b(4k)\b", Insensitive;
    /// A frame size, "1280x720". A cropped picture ("1920x804") is as wide as a full one, so the
    /// width decides when it says more.
    FRAME_SIZE = r"\b(\d{3,4})x(\d{3,4})\b", Insensitive;
    /// "Collection" only counts set apart in brackets: titles contain the word ("Kantai
    /// Collection").
    BATCH_REGEX = r"\b(?:batch|complete\s+series|complete\s+season)\b|[\[(]\s*collection\s*[\])]", Insensitive;
    BATCH_WORDS = r"[\[(]?\s*\b(?:batch|complete(?:\s+(?:series|season))?)\b\s*[\])]?|[\[(]\s*collection\s*[\])]", Insensitive;
    /// A file's name, or the CRC of one: what a single file is posted under, and a batch is not.
    SINGLE_FILE = r"\.(?:mkv|mp4|avi|webm|mov|m4v|ts)$|[\[(](?=\d*[A-Fa-f])[0-9A-Fa-f]{8}[\])]", Insensitive;
    OTHER_FORMAT = r"\b(?:movie|film|gekijouban|ova|oav|oads?|ona|specials?|recaps?)\b", Insensitive;
    SEASON_MARKERS = r"\bS\d{1,2}\b|\bseason\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b", Insensitive;
    STAGE = r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:-\s*\d+\s*(?:st|nd|rd|th)?\s*)?stage", Sensitive;
    DIGITS = r"\d+", Sensitive;
    PART = r"part\s*(\d+)", Sensitive;
    SEASON_NUMBER = r"season\s*(\d+)", Sensitive;
    EPISODE_TOKEN = r"\bS\d{1,2}\s*[-_.]?\s*E\d{1,4}(?:v\d+)?\b|\s-\s*\d{1,4}(?:v\d+)?(?=[\s\[(.]|$)", Insensitive;
    SEASON_TAG = r"\bS(\d{1,2})\s*[-_.]?\s*E\d{1,4}(?:v\d+)?\b", Insensitive;
    TRAILING_ROMAN = r"\s(ii|iii|iv|v|vi)\s*$", Insensitive;
    LEADING_GROUP = r"^\s*\[[^\]]*\]\s*", Sensitive;
    TRAILING_NAME_SEPARATORS = r"[\s\-_|/:]+$", Sensitive;
    KEY_EXTENSION = r"\.(mkv|mp4|avi)$", Insensitive;
    NOT_ALPHANUMERIC = r"[^0-9a-z]+", Sensitive;
}

/// An episode range: "S01E01-E12", "(01-12)", "[01 ~ 24]", "01~12", a bare "E1-E12", "01-12" or
/// "- 1-12" set apart by spaces, or "- 12 + 13". A bare "1-2" is left out: it is part of a title as
/// often as not ("Ranma 1-2 2024 S03E03").
static RANGE_REGEXES: LazyLock<[Regex; 7]> = LazyLock::new(|| {
    [
        (
            r"\bS\d{1,2}E(\d{1,4})\s*-\s*E?(\d{1,4})\b",
            Case::Insensitive,
        ),
        (
            r"[\[(]\s*(?:EP?\s*)?(\d{1,4})\s*[-~]\s*(?:EP?\s*)?(\d{1,4})\s*[\])]",
            Case::Insensitive,
        ),
        (r"\b(\d{1,4})\s*~\s*(\d{1,4})\b", Case::Sensitive),
        (
            r"(?:^|\s)EP?(\d{1,4})-(?:EP?)?(\d{1,4})(?=$|\s)",
            Case::Insensitive,
        ),
        (r"(?:^|\s)(\d{2,4})-(\d{2,4})(?=$|\s)", Case::Sensitive),
        (r"\s-\s+(\d{1,4})-(\d{1,4})(?=$|\s)", Case::Sensitive),
        (
            r"\s-\s+(\d{1,4})\s*[+&]\s*(\d{1,4})(?=$|[\s\[(])",
            Case::Sensitive,
        ),
    ]
    .map(|(pattern, flags)| Regex::literal(pattern, flags))
});

static SEASON_NAME_PATTERNS: LazyLock<[Regex; 3]> = LazyLock::new(|| {
    [
        r"\bseason\s*(\d{1,2})\b",
        r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b",
        r"\bS(\d{1,2})\b",
    ]
    .map(|pattern| Regex::literal(pattern, Case::Insensitive))
});

/// What a release title says about itself.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ParsedRelease {
    pub raw_title: String,
    pub title: String,
    pub episode: Option<i64>,
    pub season: Option<i64>,
    pub release_group: Option<String>,
    pub version: i64,
}

/// What `match_release_to_show` and `match_parsed` take besides the title and the aliases.
#[derive(Clone, Copy, Debug)]
pub struct MatchOptions<'a> {
    pub threshold: f64,
    /// `prepare_aliases` of the aliases; None prepares them.
    pub prepared_aliases: Option<&'a [(String, String)]>,
    /// For callers that only interpret an already-downloaded release. Callers that choose which
    /// torrents to download leave it false, so a release of another cour of the same arc is never
    /// claimed.
    pub ignore_arc_marker: bool,
    pub allow_fuzzy: bool,
    pub min_resolution: i64,
}

impl Default for MatchOptions<'_> {
    fn default() -> Self {
        Self {
            threshold: FUZZY_MATCH_THRESHOLD,
            prepared_aliases: None,
            ignore_arc_marker: false,
            allow_fuzzy: true,
            min_resolution: DEFAULT_MIN_RESOLUTION,
        }
    }
}

/// Each non-empty alias with its `normalize_alias` form, leaving out those that normalize to
/// nothing.
pub fn prepare_aliases(aliases: &[impl AsRef<str>]) -> Vec<(String, String)> {
    aliases
        .iter()
        .map(AsRef::as_ref)
        .filter(|alias| !alias.is_empty())
        .filter_map(|alias| {
            let normalized = normalize_alias(alias);
            (!normalized.is_empty()).then(|| (alias.to_owned(), normalized))
        })
        .collect()
}

/// `normalize_title` for a show's own name: square brackets there are part of it ("[Oshi no
/// Ko]"), not a tag.
pub fn normalize_alias(alias: &str) -> String {
    normalize_title(&alias.replace(['[', ']'], " "))
}

/// The words of a normalized title that name the show: no stopwords, season words or the
/// season's number.
pub fn meaningful_words(normalized: &str) -> HashSet<&str> {
    let mut words = HashSet::new();
    let mut previous = "";
    for word in normalized.split_whitespace() {
        if !STOPWORDS.contains(&word)
            && !SEASON_WORDS.contains(&word)
            && !(is_number(word) && SEASON_WORDS.contains(&previous))
        {
            words.insert(word);
        }
        previous = word;
    }
    words
}

/// A title in the form names are compared in: lowercase, without bracketed tags, roman numerals
/// and ordinal seasons as numbers ("2nd Season" and "S2" as "season 2"), and only words.
pub fn normalize_title(title: &str) -> String {
    let text = title.to_lowercase();
    let text = BRACKETED.replace_all(&text, " ");
    let text = ROMAN_NUMERAL.replace_all_with(&text, |found| {
        let numeral = found.group(1).unwrap_or_default();
        ROMAN_NUMERALS
            .iter()
            .find(|(roman, _)| *roman == numeral)
            .map_or(numeral, |&(_, number)| number)
            .to_owned()
    });
    let text = ORDINAL_SEASON.replace_all(&text, "season $1");
    let text = SHORT_SEASON.replace_all(&text, "season $1");
    let text = PUNCTUATION.replace_all(&text, " ");
    let text = TIMES.replace_all(&text, " x ");
    let text = NOT_WORD.replace_all(&text, " ");
    SPACES.replace_all(&text, " ").trim().to_owned()
}

fn is_non_group_tag(text: &str) -> bool {
    NON_GROUP_TAGS.contains(&text.to_lowercase().as_str())
}

/// A CRC in brackets: 8 or 4 hex digits.
fn is_checksum(text: &str) -> bool {
    matches!(text.len(), 4 | 8) && text.bytes().all(|b| b.is_ascii_hexdigit())
}

/// The release group, whether at the start ("[SubsPlease]"), at the end ("[Varyg]") or a scene
/// suffix ("-VARYG").
pub fn extract_release_group_tag(raw_title: &str) -> Option<String> {
    if let Some(found) = GROUP_AT_START.find(raw_title.trim()) {
        let candidate = found.group(1).unwrap_or_default().trim();
        // An empty leading bracket names no group and ends the search.
        if candidate.is_empty() {
            return None;
        }
        if !is_non_group_tag(candidate) && !is_number(candidate) {
            return Some(candidate.to_owned());
        }
    }

    let scene: Vec<&str> = SCENE_GROUP
        .find_iter(raw_title)
        .filter_map(|found| found.group(1))
        .collect();
    if let Some(candidate) = scene.into_iter().rev().find(|candidate| {
        !is_non_group_tag(candidate) && candidate.chars().count() >= 2 && !is_number(candidate)
    }) {
        return Some(candidate.to_owned());
    }

    let brackets: Vec<&str> = BRACKET_CONTENT
        .find_iter(raw_title)
        .filter_map(|found| found.group(1))
        .collect();
    brackets
        .into_iter()
        .rev()
        .map(str::trim)
        .find(|value| {
            !value.is_empty()
                && !is_checksum(value)
                && !is_non_group_tag(value)
                && !is_number(value)
        })
        .map(str::to_owned)
}

/// The release's version: "v2", "[v3]", "ver. 2", or a REPACK or PROPER (2, or one more than the
/// REPACK's number); 1 when it names none.
pub fn extract_release_version(raw_title: &str) -> i64 {
    if let Some(value) = VERSION_TAG
        .find(raw_title)
        .and_then(|found| (1..=3).find_map(|index| found.group(index)))
    {
        return number(value);
    }
    if REPACK_OR_PROPER.is_match(raw_title) {
        return REPACK_NUMBER
            .find(raw_title)
            .and_then(|found| found.group(1))
            .map_or(2, |digits| number(digits).saturating_add(1));
    }
    1
}

/// Parses a release title or file name into its name, episode, season, group and version.
///
/// A double release ("- 01-02") reads as its first episode; `episode_pair` gives the second.
/// The name is empty when nothing before the episode names the show, as in "[Group] [Show] - 01".
pub fn parse_release_title(raw_title: &str) -> ParsedRelease {
    if episode_pair(raw_title).is_some() {
        let mut parsed = parse_release_title(&first_of_pair(raw_title));
        parsed.raw_title = raw_title.to_owned();
        return parsed;
    }

    let version = extract_release_version(raw_title);
    let release_group = extract_release_group_tag(raw_title);
    let mut cleaned = raw_title.trim().to_owned();
    if let Some(group) = &release_group {
        cleaned = remove_group(&cleaned, group);
    }
    let cleaned = VERSION_REGEX.replace_all(&cleaned, "$1");
    let cleaned = VIDEO_EXTENSION.replace_all(&cleaned, "").trim().to_owned();
    let cleaned = AUDIO_CHANNELS.replace_all(&cleaned, " ");
    let cleaned = FRAME_RATE.replace_all(&cleaned, " ");
    let cleaned = BIT_DEPTH.replace_all(&cleaned, " ");
    let cleaned = QUALITY_TAGS.replace_all(&cleaned, " ");

    let primary_segment = cleaned
        .split_once('|')
        .map_or(&*cleaned, |(first, _)| first)
        .trim();
    let (title_candidate, episode, season) = episode_and_season(primary_segment);

    let mut title = BRACKETED.replace_all(title_candidate, "").into_owned();
    title = YEAR.replace_all(&title, "").into_owned();
    for _ in 0..5 {
        title = TRAILING_TAG.replace_all(&title, "").trim().to_owned();
        title = TRAILING_SEPARATORS
            .replace_all(&title, "")
            .trim()
            .to_owned();
    }
    title = LEADING_SEPARATORS.replace_all(&title, "").trim().to_owned();
    title = SPACES.replace_all(&title, " ").trim().to_owned();

    ParsedRelease {
        raw_title: raw_title.to_owned(),
        title,
        episode,
        season,
        release_group,
        version,
    }
}

/// The title without the group's tags: "[Group]" at the start or anywhere, and a "-Group" that
/// ends a word, each with the whitespace after it.
fn remove_group(title: &str, group: &str) -> String {
    let bracketed = format!("[{group}]");
    let title = match title.strip_prefix(&bracketed) {
        Some(rest) => rest.trim_start(),
        None => title,
    };
    let title = remove_tags(title, &bracketed, false);
    remove_tags(&title, &format!("-{group}"), true)
}

/// `text` without each `tag` and the whitespace after it, scanning left to right.
/// With `at_word_end`, only a tag followed by a word boundary (`\b`) is removed.
fn remove_tags(text: &str, tag: &str, at_word_end: bool) -> String {
    let ends_in_word = tag.chars().next_back().is_some_and(is_word);
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find(tag) {
        let after = &rest[at + tag.len()..];
        let word_follows = after.chars().next().is_some_and(is_word);
        if at_word_end && ends_in_word == word_follows {
            // No match here: the search goes on from the next character.
            let next = at + rest[at..].chars().next().map_or(1, char::len_utf8);
            out.push_str(&rest[..next]);
            rest = &rest[next..];
            continue;
        }
        out.push_str(&rest[..at]);
        rest = after.trim_start();
    }
    out.push_str(rest);
    out
}

/// The part of a title before its episode and season numbers, with the episode and season.
fn episode_and_season(segment: &str) -> (&str, Option<i64>, Option<i64>) {
    let before = |found: &Match<'_>| (segment[..found.start()]).trim();
    let group_number = |found: &Match<'_>, index| found.group(index).map(number);

    if let Some(found) = DUAL_NUMBERING.find(segment) {
        return (
            before(&found),
            group_number(&found, 1),
            group_number(&found, 2),
        );
    }
    if let Some(found) = SEASON_EPISODE.find(segment) {
        return (
            before(&found),
            group_number(&found, 2),
            group_number(&found, 1),
        );
    }
    if let Some(found) = SEASON_DASH_EPISODE.find(segment) {
        return (
            before(&found),
            group_number(&found, 2),
            group_number(&found, 1),
        );
    }
    if let Some(found) = EPISODE_NUMBER
        .find_iter(segment)
        .filter(|found| is_episode_number(segment, found))
        .last()
    {
        return (before(&found), found.name("num").map(number), None);
    }
    let season = SEASON_ONLY
        .find(segment)
        .and_then(|found| found.group(1))
        .map(number);
    (segment, None, season)
}

/// Whether a standalone number in the segment is an episode's: not a year, and not the number of
/// a season, part or volume.
fn is_episode_number(segment: &str, found: &Match<'_>) -> bool {
    let whole = found.as_str();
    // An episode title or a language tag may follow the number only when " - " sets it apart.
    if found.name("more").is_some() && !found.name("pre").unwrap_or_default().contains('-') {
        return false;
    }
    let prefixed = whole.trim_matches([' ', '-', '_']).starts_with(['E', '#']);
    if prefixed {
        return true;
    }
    let number = number(found.name("num").unwrap_or_default());
    if (1950..=2050).contains(&number) {
        return false;
    }
    // "Season 2", "Part 6" and "Volume 1" number something bigger than an episode.
    whole.contains('-') || !COUNTS_SOMETHING_ELSE.is_match(&segment[..found.start()])
}

/// The best fuzzy score of the title against the aliases, from 0 to 100, with the alias that gave
/// it.
///
/// An alias sharing no word that names the show (only "season 3" or "final season") is skipped.
pub fn calculate_match_score(
    parsed_title: &str,
    aliases: &[impl AsRef<str>],
    prepared_aliases: Option<&[(String, String)]>,
) -> (f64, Option<String>) {
    if parsed_title.is_empty() || aliases.is_empty() {
        return (0.0, None);
    }
    let norm_parsed = normalize_title(parsed_title);
    let tokens: Vec<&str> = norm_parsed
        .split_whitespace()
        .filter(|word| !STOPWORDS.contains(word))
        .collect();
    if tokens.is_empty() {
        return (0.0, None);
    }
    let named = meaningful_words(&norm_parsed);
    // One short word is compared whole, so it cannot match a longer name that contains it.
    let short = matches!(tokens.as_slice(), [only] if only.chars().count() < 5);

    let prepared_here;
    let prepared = match prepared_aliases {
        Some(prepared) => prepared,
        None => {
            prepared_here = prepare_aliases(aliases);
            &prepared_here
        }
    };

    let mut best_score = 0.0;
    let mut best_alias = None;
    for (alias, norm_alias) in prepared {
        if norm_parsed == *norm_alias {
            return (100.0, Some(alias.clone()));
        }
        if named.is_disjoint(&meaningful_words(norm_alias)) {
            continue;
        }
        let score = if short {
            fuzz::token_sort_ratio(&norm_parsed, norm_alias)
        } else {
            fuzz::token_set_ratio(&norm_parsed, norm_alias)
        };
        if score > best_score {
            best_score = score;
            best_alias = Some(alias);
        }
    }
    (best_score, best_alias.cloned())
}

/// The lines a release says it has ("720p" is 720, "4K" is 2160, "1280x720" is 720), or None when
/// it says nothing.
pub fn release_resolution(raw_title: &str) -> Option<i64> {
    if let Some(found) = RESOLUTION.find(raw_title) {
        return Some(found.group(1).map_or(2160, number));
    }
    let frame = FRAME_SIZE.find(raw_title)?;
    let width = number(frame.group(1)?);
    let height = number(frame.group(2)?);
    let full_frame = LINES_BY_WIDTH
        .iter()
        .find(|&&(wide, _)| width >= wide)
        .map_or(0, |&(_, lines)| lines);
    Some(height.max(full_frame))
}

/// The first range among `RANGE_REGEXES`, in their order, with the index of the regex that found
/// it and its first and last episode. "(2019-2021)" dates a release and is no range.
fn range_match(raw_title: &str) -> Option<(usize, Match<'_>, (i64, i64))> {
    RANGE_REGEXES.iter().enumerate().find_map(|(index, regex)| {
        regex.find_iter(raw_title).find_map(|found| {
            let first = number(found.group(1)?);
            let last = number(found.group(2)?);
            (0 < first && first < last && !(first >= 1900 && last <= 2100)).then_some((
                index,
                found,
                (first, last),
            ))
        })
    })
}

/// The first and last episode a title names as a range, or None when it names none.
pub fn batch_range(raw_title: &str) -> Option<(i64, i64)> {
    range_match(raw_title).map(|(_, _, span)| span)
}

/// The two episodes of a double release ("- 01-02", "- 12 + 13"): one torrent of two episodes in
/// a row. None for anything else; a longer range, or one called a batch, is a batch.
pub fn episode_pair(raw_title: &str) -> Option<(i64, i64)> {
    if BATCH_REGEX.is_match(raw_title) {
        return None;
    }
    batch_range(raw_title).filter(|&(first, last)| last == first + 1)
}

/// A double release's title with its range cut down to the first episode, so it reads like a
/// single one's.
fn first_of_pair(raw_title: &str) -> String {
    let Some((index, found, _)) = range_match(raw_title) else {
        return raw_title.to_owned();
    };
    if index == BRACKETED_RANGE {
        return format!(
            "{} - {} {}",
            &raw_title[..found.start()],
            found.group(1).unwrap_or_default(),
            &raw_title[found.end()..]
        );
    }
    match (found.span(1), found.span(2)) {
        (Some((_, first_end)), Some((_, last_end))) => {
            format!("{}{}", &raw_title[..first_end], &raw_title[last_end..])
        }
        _ => raw_title.to_owned(),
    }
}

/// Whether the title is one file's: a special or a movie when it carries no episode number, never
/// a whole season.
pub fn is_single_file(raw_title: &str) -> bool {
    SINGLE_FILE.is_match(raw_title.trim())
}

/// The words of a normalized name that tell it apart: no digits and no filler words.
fn name_words(normalized: &str) -> HashSet<&str> {
    meaningful_words(normalized)
        .into_iter()
        .filter(|word| !is_number(word) && !NAME_FILLERS.contains(word))
        .collect()
}

/// Whether the name is one of the show's with its words spaced or ordered another way
/// ("Daiseijo" for "Dai Seijo").
fn spelled_like_a_name(name: &str, known_names: &[String]) -> bool {
    let unspaced = name.replace(' ', "");
    known_names.iter().any(|known| {
        let score = fuzz::token_sort_ratio(name, known)
            .max(fuzz::ratio(&unspaced, &known.replace(' ', "")));
        score >= SAME_NAME_SCORE
    })
}

/// Whether a release of the show's name is really another thing: a movie or special, a
/// " - subtitle" the show's titles lack, or another show that only shares a word and a season with
/// it ("Grand Blue S2" and "Blue Box Season 2") or whose name holds the show's ("Monster Musume no
/// Oishasan" and "Monster").
pub fn names_something_else(parsed: &ParsedRelease, aliases: &[impl AsRef<str>]) -> bool {
    let title = parsed.title.trim_matches([' ', '-']);
    let all_names = aliases
        .iter()
        .map(AsRef::as_ref)
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    if OTHER_FORMAT
        .find_iter(title)
        .any(|word| !all_names.contains(&word.as_str().to_lowercase()))
    {
        return true;
    }

    let core = normalize_title(&SEASON_MARKERS.replace_all(title, " "));
    if !aliases.iter().any(|alias| {
        let alias_core = normalize_alias(&SEASON_MARKERS.replace_all(alias.as_ref(), " "));
        fuzz::token_set_ratio(&core, &alias_core) >= FUZZY_MATCH_THRESHOLD
    }) {
        return true;
    }

    let normalized = normalize_title(title);
    let named = name_words(&normalized);
    let known_names: Vec<String> = aliases
        .iter()
        .map(|alias| normalize_alias(alias.as_ref()))
        .collect();
    let known: Vec<HashSet<&str>> = known_names.iter().map(|name| name_words(name)).collect();
    // One of the show's names whole, going on with words none of its names has, is another show's
    // name, unless the words only differ in how one of the names is spelled.
    let has_other_words = named
        .iter()
        .any(|word| known.iter().all(|words| !words.contains(word)));
    if has_other_words
        && known
            .iter()
            .any(|words| !words.is_empty() && words.is_subset(&named))
        && !spelled_like_a_name(&normalized, &known_names)
    {
        return true;
    }
    // A subtitle that is part of one of the show's names ("Bleach - Sennen Kessen-hen" of "... -
    // Soukoku-tan") is its own.
    if !title.contains(" - ") || known.iter().any(|words| named.is_subset(words)) {
        return false;
    }
    !known_names
        .iter()
        .any(|name| fuzz::token_sort_ratio(&normalized, name) >= SAME_NAME_SCORE)
}

/// Whether a release names a season none of the show's names does ("Kingdom 5th Season" for
/// "Kingdom 6th Season"). A show whose names give no season, or a release that names none, never
/// conflicts.
pub fn season_conflict(raw_title: &str, aliases: &[impl AsRef<str>]) -> bool {
    let Some(named) = release_season(raw_title) else {
        return false;
    };
    let seasons: HashSet<i64> = aliases
        .iter()
        .filter_map(|alias| release_season(alias.as_ref()))
        .collect();
    !seasons.is_empty() && !seasons.contains(&named)
}

/// Whether a release holds a run of episodes in one torrent. A double episode (`episode_pair`) is
/// not one.
pub fn is_batch(raw_title: &str) -> bool {
    BATCH_REGEX.is_match(raw_title)
        || batch_range(raw_title).is_some_and(|(first, last)| last != first + 1)
}

/// The title without its batch markers and episode range, so it can be matched to a show like a
/// single release.
pub fn strip_batch(raw_title: &str) -> String {
    let mut text = BATCH_WORDS.replace_all(raw_title, " ").into_owned();
    for regex in RANGE_REGEXES.iter() {
        text = regex.replace_all(&text, " ").into_owned();
    }
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// How well a release fits the preferred quality, lower is better: the preferred resolution
/// itself, then the closest one above it, then a release that names none. One below the
/// preference (which is never taken) ranks last of all.
pub fn resolution_rank(raw_title: &str, preferred: i64) -> i64 {
    match release_resolution(raw_title) {
        None => 10_000,
        Some(resolution) if resolution >= preferred => resolution.saturating_sub(preferred),
        Some(_) => 100_000,
    }
}

/// Whether a release may be taken: no batch, and no resolution below the minimum. A release that
/// names none is kept.
pub fn is_valid_release(raw_title: &str, min_resolution: i64) -> bool {
    !is_batch(raw_title)
        && release_resolution(raw_title).is_none_or(|resolution| resolution >= min_resolution)
}

/// The arc, cour or part marker of a text as a token that keeps split seasons apart ("stage1",
/// "stage2-3", "part7", "season4"), or None when it carries none.
pub fn extract_arc_qualifiers(text: &str) -> Option<String> {
    if text.is_empty() {
        return None;
    }
    let lowered = text.to_lowercase();
    if let Some(stage) = STAGE.find(&lowered) {
        let numbers: Vec<&str> = DIGITS
            .find_iter(stage.as_str())
            .map(|found| found.as_str())
            .collect();
        return Some(format!("stage{}", numbers.join("-")));
    }
    if let Some(part) = PART.find(&lowered) {
        return Some(format!("part{}", part.group(1).unwrap_or_default()));
    }
    SEASON_NUMBER
        .find(&lowered)
        .map(|season| format!("season{}", season.group(1).unwrap_or_default()))
}

/// The title before its episode number ("S01E05" or " - 05"), or the whole title.
fn before_episode(raw_title: &str) -> &str {
    EPISODE_TOKEN
        .find(raw_title)
        .map_or(raw_title, |token| &raw_title[..token.start()])
}

/// The arc marker in a release's series name, ignoring what follows the episode number: episode
/// names, quality tags and alternate titles can mention a "Part 5" or "Part 1080p" that is not
/// the show's arc.
pub fn release_arc_qualifier(raw_title: &str) -> Option<String> {
    extract_arc_qualifiers(before_episode(raw_title))
}

/// The series name of a release: no leading group tag, nothing from the episode number on.
fn series_name_part(raw_title: &str) -> String {
    LEADING_GROUP
        .replace_all(before_episode(raw_title), "")
        .into_owned()
}

fn name_season(name: &str) -> Option<i64> {
    for pattern in SEASON_NAME_PATTERNS.iter() {
        if let Some(found) = pattern.find(name) {
            return found.group(1).map(number);
        }
    }
    let numeral = (TRAILING_ROMAN.find(name)?.group(1)?).to_lowercase();
    ROMAN_SEASONS
        .iter()
        .find(|(roman, _)| *roman == numeral)
        .map(|&(_, season)| season)
}

/// The season a release names for itself, or None when it names none: from the series name in
/// front of the episode number ("Link Click S3", "Foo 2nd Season", "Mob Psycho 100 III"), then
/// from an "S03E05" tag.
pub fn release_season(raw_title: &str) -> Option<i64> {
    name_season(&series_name_part(raw_title)).or_else(|| {
        SEASON_TAG
            .find(raw_title)
            .and_then(|tag| tag.group(1))
            .map(number)
    })
}

/// The arc marker a show's aliases insist on, if any. Only cour or stage and part markers count:
/// release groups often leave out a plain "Season N", but a bare title is ambiguous between the
/// arcs of a cour split.
pub fn alias_arc_requirement(aliases: &[impl AsRef<str>]) -> Option<String> {
    aliases
        .iter()
        .filter_map(|alias| extract_arc_qualifiers(alias.as_ref()))
        .find(|qualifier| qualifier.starts_with("stage") || qualifier.starts_with("part"))
}

fn arc_is_compatible(requirement: &str, qualifier: Option<&str>) -> bool {
    let Some(qualifier) = qualifier else {
        return false;
    };
    if requirement == qualifier {
        return true;
    }
    // "stage2-3" accepts a title that names either half of that cour pair.
    match (
        requirement.strip_prefix("stage"),
        qualifier.strip_prefix("stage"),
    ) {
        (Some(required), Some(named)) => {
            let required: HashSet<&str> = required.split('-').collect();
            named.split('-').any(|stage| required.contains(stage))
        }
        _ => false,
    }
}

/// Makes the parsed title carry the season marker the release writes in its name ("Ao Ashi S2").
fn keep_season_in_title(raw_title: &str, parsed: &mut ParsedRelease) {
    let name = series_name_part(raw_title);
    if name_season(&name).is_none() || name_season(&parsed.title).is_some() {
        return;
    }
    let name = TRAILING_NAME_SEPARATORS
        .replace_all(&name, "")
        .trim()
        .to_owned();
    if !name.is_empty() {
        parsed.title = name;
    }
}

/// `match_parsed` for a title not parsed yet, with the show's pattern as text (None builds it from
/// the aliases; text that does not compile matches nothing).
pub fn match_release_to_show(
    raw_title: &str,
    aliases: &[impl AsRef<str>],
    test_pattern: Option<&str>,
    options: &MatchOptions<'_>,
) -> (bool, f64, ParsedRelease) {
    let built;
    let pattern = match test_pattern {
        Some(pattern) => pattern,
        None => {
            built = build_regex_pattern(aliases, None);
            &built
        }
    };
    let regex = Regex::new(pattern, Case::Insensitive).ok();
    match_parsed(
        &parse_release_title(raw_title),
        aliases,
        regex.as_ref(),
        options,
    )
}

/// Whether a feed item or torrent is a release of the show, with its score and parse.
///
/// Batches and releases below the minimum resolution never match, nor, unless
/// `ignore_arc_marker`, a release without the arc marker the show's aliases insist on. Then the
/// show's pattern decides (None matches nothing), and failing that the fuzzy score of the parsed
/// name against `threshold`, when `allow_fuzzy`.
pub fn match_parsed(
    release: &ParsedRelease,
    aliases: &[impl AsRef<str>],
    pattern: Option<&Regex>,
    options: &MatchOptions<'_>,
) -> (bool, f64, ParsedRelease) {
    let raw_title = release.raw_title.as_str();
    let mut parsed = release.clone();
    if !is_valid_release(raw_title, options.min_resolution) {
        return (false, 0.0, parsed);
    }

    let requirement = (!options.ignore_arc_marker)
        .then(|| alias_arc_requirement(aliases))
        .flatten();
    if requirement.is_some_and(|requirement| {
        !arc_is_compatible(&requirement, release_arc_qualifier(raw_title).as_deref())
    }) {
        return (false, 0.0, parsed);
    }

    if let Some(found) = pattern.and_then(|regex| regex.find(raw_title)) {
        let token = found.as_str().trim();
        if !token.is_empty() {
            parsed.title = token.to_owned();
        }
        keep_season_in_title(raw_title, &mut parsed);
        return (true, 100.0, parsed);
    }

    if !options.allow_fuzzy {
        return (false, 0.0, parsed);
    }
    let (score, _) = calculate_match_score(&parsed.title, aliases, options.prepared_aliases);
    let is_match = score >= options.threshold;
    if is_match {
        keep_season_in_title(raw_title, &mut parsed);
    }
    (is_match, score, parsed)
}

/// A release name reduced to its words, so naming styles compare equal: a feed title ("Blue Box
/// S02E01 … H.264-VARYG (Ao no Hako …)") and the torrent's file name
/// ("Blue.Box.S02E01.….H.264-VARYG.mkv") name one release.
pub fn release_key(name: &str) -> String {
    let key = KEY_EXTENSION.replace_all(name.trim(), "");
    NOT_ALPHANUMERIC
        .replace_all(&key.to_lowercase(), " ")
        .trim()
        .to_owned()
}

/// Whether two names are one release: equal words, or one extends the other by whole words.
pub fn same_release(first: &str, second: &str) -> bool {
    let (a, b) = (release_key(first), release_key(second));
    if a.is_empty() || b.is_empty() {
        return false;
    }
    let extends = |longer: &str, shorter: &str| {
        longer
            .strip_prefix(shorter)
            .is_some_and(|rest| rest.starts_with(' '))
    };
    a == b || extends(&a, &b) || extends(&b, &a)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_regex_written_in_the_code_compiles() {
        assert!(written_regexes().iter().all(|regex| regex.compiled()));
    }

    #[test]
    fn a_scene_group_ending_in_a_symbol_is_removed_only_before_a_word() {
        assert_eq!(
            parse_release_title("Show -Foo!bar - 02 [Foo!]").title,
            "Show bar"
        );
        assert_eq!(
            parse_release_title("Show -Foo! bar - 02 [Foo!]").title,
            "Show -Foo! bar"
        );
    }

    #[test]
    fn an_empty_leading_bracket_ends_the_group_search() {
        for title in ["[] Show - 01", "[ ] Show - 01 [Group]"] {
            let parsed = parse_release_title(title);
            assert_eq!(parsed.release_group, None, "{title}");
            assert_eq!(parsed.title, "Show", "{title}");
            assert_eq!(parsed.episode, Some(1), "{title}");
        }
    }

    #[test]
    fn numbers_beyond_i64_saturate() {
        assert_eq!(
            extract_release_version("Show - 01v99999999999999999999"),
            i64::MAX
        );
        assert_eq!(
            extract_release_version("Show REPACK99999999999999999999 - 01"),
            i64::MAX
        );
        assert_eq!(resolution_rank("Show 1080p", i64::MIN), i64::MAX);
    }
}
