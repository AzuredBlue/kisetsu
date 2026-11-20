//! Deciding which feed titles belong to a show.
//!
//! Two levels share this module so a release can never be stored but never grabbed, or the
//! reverse. The broad level (`broad_match`) decides what is kept from a feed at all. The
//! decision level (`decide`) is what grabbing and discovery use: it enforces a cour or stage
//! marker the show's aliases insist on, and on a feed the show is locked to it narrows to the
//! name and season that feed has been delivering.

use std::collections::HashSet;
use std::fmt::Write as _;

use sha1_smol::Sha1;

use crate::parsing::{
    MatchOptions, ParsedRelease, UnknownNumeral, alias_arc_requirement, match_release_to_show,
    meaningful_words, normalize_title, parse_release_title, prepare_aliases, release_arc_qualifier,
    release_season, season_conflict,
};
use crate::patterns::show_match_patterns;
use crate::pyre::{Flags, Regex};

/// A name pattern kept with the text it was compiled from.
///
/// Text that does not compile matches nothing.
#[derive(Debug)]
pub struct Pattern {
    text: String,
    regex: Option<Regex>,
}

impl Pattern {
    pub fn new(text: impl Into<String>) -> Self {
        let text = text.into();
        let regex = Regex::new(&text, Flags::IGNORECASE).ok();
        Self { text, regex }
    }

    pub fn as_str(&self) -> &str {
        &self.text
    }

    fn is_match(&self, title: &str) -> bool {
        self.regex
            .as_ref()
            .is_some_and(|regex| regex.is_match(title))
    }
}

#[derive(Debug)]
pub struct Matcher {
    pub aliases: Vec<String>,
    pub prepared: Vec<(String, String)>,
    pub pattern: String,
    pub learned_pattern: Option<Pattern>,
    /// On a locked feed: only the learned name matches, and the season must agree.
    pub strict: bool,
    pub season_known: bool,
    pub season: Option<i64>,
    /// Releases marked below this resolution are never the show's (the user's setting).
    pub min_resolution: i64,
}

pub fn build_matcher(
    aliases: &[String],
    learned_name: Option<&str>,
    min_resolution: i64,
) -> Matcher {
    let (pattern, learned_pattern) = show_match_patterns(aliases, learned_name);
    Matcher {
        aliases: aliases.to_vec(),
        prepared: prepare_aliases(aliases),
        pattern,
        learned_pattern: learned_pattern.map(Pattern::new),
        strict: false,
        season_known: false,
        season: None,
        min_resolution,
    }
}

/// `matcher` narrowed to what a show's locked feed has delivered.
///
/// Only the learned name matches, and a release must name the same season as the releases
/// already downloaded from the feed (or none, when they name none).
pub fn narrow_to_feed(
    matcher: Matcher,
    learned_name: Option<&str>,
    held_titles: &[impl AsRef<str>],
) -> Result<Matcher, UnknownNumeral> {
    if matcher.learned_pattern.is_none() {
        return Ok(matcher);
    }
    let mut named = HashSet::new();
    for title in held_titles {
        named.extend(release_season(title.as_ref())?);
    }
    named.extend(release_season(learned_name.unwrap_or_default())?);
    Ok(match named.len() {
        0 => Matcher {
            strict: true,
            season_known: !held_titles.is_empty(),
            ..matcher
        },
        1 => Matcher {
            strict: true,
            season_known: true,
            season: named.into_iter().next(),
            ..matcher
        },
        _ => Matcher {
            strict: true,
            ..matcher
        },
    })
}

/// Whether the release names a season the show's names don't. The feed's own naming (the
/// learned name) is trusted.
fn other_season(matcher: &Matcher, title: &str) -> Result<bool, UnknownNumeral> {
    if matcher
        .learned_pattern
        .as_ref()
        .is_some_and(|learned| learned.is_match(title))
    {
        return Ok(false);
    }
    season_conflict(title, &matcher.aliases)
}

/// Whether the title could be one of the show's releases; the arc marker is not enforced.
pub fn broad_match(matcher: &Matcher, title: &str) -> Result<bool, UnknownNumeral> {
    if other_season(matcher, title)? {
        return Ok(false);
    }
    let options = MatchOptions {
        test_pattern: Some(&matcher.pattern),
        prepared_aliases: Some(&matcher.prepared),
        ignore_arc_marker: true,
        min_resolution: matcher.min_resolution,
        ..MatchOptions::default()
    };
    Ok(match_release_to_show(title, &matcher.aliases, &options)?.0)
}

/// Whether the title is one of the show's releases, with its parse.
pub fn decide(
    matcher: &Matcher,
    title: &str,
    unmarked_ok: bool,
) -> Result<(bool, ParsedRelease), UnknownNumeral> {
    // A title in the learned name that carries no arc marker shows that the group does not
    // label this show's cour, so there is nothing to compare with the aliases' marker. A
    // title with a marker of its own is still checked.
    let unmarked = release_arc_qualifier(title).is_none();
    let learned_without_marker = unmarked
        && matcher
            .learned_pattern
            .as_ref()
            .is_some_and(|learned| learned.is_match(title));
    let options = MatchOptions {
        test_pattern: if matcher.strict {
            matcher.learned_pattern.as_ref().map(Pattern::as_str)
        } else {
            Some(&matcher.pattern)
        },
        prepared_aliases: Some(&matcher.prepared),
        ignore_arc_marker: learned_without_marker || (unmarked_ok && unmarked),
        allow_fuzzy: !matcher.strict,
        min_resolution: matcher.min_resolution,
        ..MatchOptions::default()
    };
    let (matched, _, parsed) = match_release_to_show(title, &matcher.aliases, &options)?;
    if matched && matcher.season_known && release_season(title)? != matcher.season {
        return Ok((false, parsed));
    }
    if matched && !matcher.strict && other_season(matcher, title)? {
        return Ok((false, parsed));
    }
    Ok((matched, parsed))
}

/// `decide` for a release whose publish date is known.
///
/// A show whose aliases name an arc ("2nd & 3rd STAGE") refuses releases that carry no arc
/// marker, but groups usually do not write one for a split entry. A release published right
/// after one of this show's own scheduled air times is on its schedule, which the numbering
/// cannot tell us but the date can; `on_schedule(raw_episode)` says so, and the release is
/// then accepted without the marker. A release that names a different arc, has no date or
/// falls outside the schedule stays refused.
pub fn decide_dated(
    matcher: &Matcher,
    title: &str,
    mut on_schedule: impl FnMut(i64) -> bool,
) -> Result<(bool, ParsedRelease), UnknownNumeral> {
    let (matched, parsed) = decide(matcher, title, false)?;
    if matched {
        return Ok((matched, parsed));
    }
    let on_its_schedule = parsed.episode.filter(|&raw| raw > 0).is_some_and(|raw| {
        alias_arc_requirement(&matcher.aliases).is_some()
            && release_arc_qualifier(title).is_none()
            && on_schedule(raw)
    });
    if on_its_schedule {
        decide(matcher, title, true)
    } else {
        Ok((matched, parsed))
    }
}

/// The highest episode number among the show's matching releases in a feed.
pub fn newest_raw_episode(
    matcher: &Matcher,
    titles: impl IntoIterator<Item = impl AsRef<str>>,
) -> Result<Option<i64>, UnknownNumeral> {
    let mut newest = None;
    for title in titles {
        let title = title.as_ref();
        if title.is_empty() {
            continue;
        }
        let (matched, parsed) = decide(matcher, title, false)?;
        if let Some(raw) = parsed.episode.filter(|&raw| matched && raw > 0) {
            newest = newest.max(Some(raw));
        }
    }
    Ok(newest)
}

/// Changes whenever a show is added or removed, or its aliases or learned name change.
///
/// It is stored, so it must not change between runs: a SHA-1 of the entries as JSON text.
pub fn signature<'a>(
    entries: impl IntoIterator<Item = (i64, &'a [String], &'a str)>,
    extra: &str,
) -> String {
    let mut rows: Vec<_> = entries.into_iter().collect();
    rows.sort_unstable();
    let mut json = String::from("[[");
    for (index, (show_id, aliases, learned)) in rows.into_iter().enumerate() {
        if index > 0 {
            json.push_str(", ");
        }
        // Writing into a String cannot fail.
        let _ = write!(json, "[{show_id}, [");
        for (index, alias) in aliases.iter().enumerate() {
            if index > 0 {
                json.push_str(", ");
            }
            push_json_string(&mut json, alias);
        }
        json.push_str("], ");
        push_json_string(&mut json, learned);
        json.push(']');
    }
    json.push_str("], ");
    push_json_string(&mut json, extra);
    json.push(']');
    Sha1::from(json).digest().to_string()
}

/// Appends `text` as a JSON string; non-ASCII characters stay as they are.
fn push_json_string(json: &mut String, text: &str) {
    json.push('"');
    for character in text.chars() {
        match character {
            '"' => json.push_str("\\\""),
            '\\' => json.push_str("\\\\"),
            '\n' => json.push_str("\\n"),
            '\r' => json.push_str("\\r"),
            '\t' => json.push_str("\\t"),
            '\u{8}' => json.push_str("\\b"),
            '\u{c}' => json.push_str("\\f"),
            control if control < ' ' => {
                // Writing into a String cannot fail.
                let _ = write!(json, "\\u{:04x}", u32::from(control));
            }
            other => json.push(other),
        }
    }
    json.push('"');
}

/// The ids of the shows a title belongs to, in the order of `matchers`; with `only`, just those.
pub fn matching_show_ids(
    matchers: &[(i64, Matcher)],
    title: &str,
    only: Option<&[i64]>,
) -> Result<Vec<i64>, UnknownNumeral> {
    let mut matched = Vec::new();
    for (show_id, matcher) in matchers {
        if broad_match(matcher, title)? {
            matched.push((*show_id, matcher));
        }
    }
    Ok(most_plainly_named(matched, title)?
        .into_iter()
        .map(|(show_id, _)| show_id)
        .filter(|show_id| only.is_none_or(|only| only.contains(show_id)))
        .collect())
}

/// Of several shows one title matched, those it names most plainly: the ones of its season,
/// then the closest names.
pub fn most_plainly_named<'a>(
    matched: Vec<(i64, &'a Matcher)>,
    title: &str,
) -> Result<Vec<(i64, &'a Matcher)>, UnknownNumeral> {
    let matched = if matched.len() > 1 {
        of_the_named_season(matched, title)?
    } else {
        matched
    };
    Ok(if matched.len() > 1 {
        closest_names(matched, title)
    } else {
        matched
    })
}

/// Of several shows one title matched, those whose name adds the fewest words the title lacks.
///
/// "Boku no Hero Academia - 160" fits "Boku no Hero Academia: FINAL SEASON" (only season words
/// added) better than "Vigilante: Boku no Hero Academia Illegals", which names a different
/// show. Equally close shows all stay.
fn closest_names<'a>(matched: Vec<(i64, &'a Matcher)>, title: &str) -> Vec<(i64, &'a Matcher)> {
    let name = parse_release_title(title).title;
    let normalized = normalize_title(&name);
    let words: HashSet<&str> = normalized.split_whitespace().collect();
    if words.is_empty() {
        return matched;
    }
    let extra = |matcher: &Matcher| {
        matcher
            .prepared
            .iter()
            .map(|(_, normalized)| {
                meaningful_words(normalized)
                    .difference(&words)
                    .filter(|word| !word.chars().all(char::is_numeric))
                    .count()
            })
            .min()
            .unwrap_or(0)
    };
    let distances: Vec<usize> = matched.iter().map(|(_, matcher)| extra(matcher)).collect();
    let best = distances.iter().copied().min();
    matched
        .into_iter()
        .zip(distances)
        .filter(|&(_, distance)| Some(distance) == best)
        .map(|(show, _)| show)
        .collect()
}

/// Of several shows one title matched, those whose aliases name the season the title names,
/// when any do.
///
/// "Foo 2nd Season - 01" matches both "Foo" and "Foo 2nd Season"; it belongs to the second
/// only. Without a show that names the season the title's own claim is not trusted (a long
/// show's aliases often omit it), and nothing is dropped.
fn of_the_named_season<'a>(
    matched: Vec<(i64, &'a Matcher)>,
    title: &str,
) -> Result<Vec<(i64, &'a Matcher)>, UnknownNumeral> {
    let Some(season) = release_season(title)? else {
        return Ok(matched);
    };
    let mut naming = Vec::new();
    for &(show_id, matcher) in &matched {
        for alias in &matcher.aliases {
            if release_season(alias)? == Some(season) {
                naming.push((show_id, matcher));
                break;
            }
        }
    }
    Ok(if naming.is_empty() { matched } else { naming })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature_escapes_control_characters_and_keeps_other_text() {
        let tricky = vec![
            "a\"b\\c\n\r\t\u{8}\u{c}\u{1}\u{1f}\u{7f}".to_owned(),
            "é 日本 \u{2028} 😀".to_owned(),
        ];
        let plain = vec!["z".to_owned()];
        let hash = signature(
            [(2, tricky.as_slice(), "l\0"), (1, plain.as_slice(), "")],
            "x\"",
        );
        assert_eq!(hash, "b726788fcff0ee89f94b17a6932b9b31d10b2dd3");
    }
}
