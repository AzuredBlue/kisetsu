//! Python `re` patterns with Python's matching rules.
//!
//! Kisetsu's patterns are Python regex text (the text is stored and compared), so they are read
//! with Python's syntax and run with Python's semantics for `str` patterns: Unicode `\d`, `\s`,
//! `\w` and `\b` as Python defines them, Python's IGNORECASE, `$` before a final newline, and
//! Python's rules for empty matches when iterating. A pattern is translated to fancy-regex syntax;
//! fancy-regex hands any pattern without look-around wholly to regex-automata, the engine of the
//! `regex` crate, and runs only patterns with look-around on its backtracking VM.
//!
//! Offsets are byte offsets into the searched text, so slicing the text with them gives the same
//! substrings Python's code point offsets give.

mod syntax;
mod translate;

use std::fmt;
use std::sync::OnceLock;

use fancy_regex::{Captures, RegexBuilder, RegexInput};

/// Python's `re` flags that Kisetsu uses.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Flags {
    ignore_case: bool,
}

impl Flags {
    pub const NONE: Self = Self { ignore_case: false };
    pub const IGNORECASE: Self = Self { ignore_case: true };
}

/// A pattern Python would reject, or one using syntax this module does not read.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error(String);

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Error {}

/// A compiled pattern.
#[derive(Debug)]
pub struct Regex {
    program: fancy_regex::Regex,
    /// For a pattern with look-around, the pattern without it, which finds the first place a
    /// match can start without backtracking.
    starts: Option<fancy_regex::Regex>,
    /// The pattern made to match the whole text, for `fullmatch`, built on first use.
    whole: OnceLock<Option<fancy_regex::Regex>>,
    /// The pattern made to reject empty matches, for iterating past an empty match, built on
    /// first use.
    non_empty: OnceLock<Option<fancy_regex::Regex>>,
}

impl Regex {
    /// Python's `re.compile(pattern, flags)`.
    pub fn new(pattern: &str, flags: Flags) -> Result<Self, Error> {
        let translation = translate::translate(&syntax::parse(pattern)?, flags.ignore_case);
        let compile = |syntax: &str| {
            build(syntax, false).ok_or_else(|| Error(format!("cannot compile {pattern:?}")))
        };
        Ok(Self {
            program: compile(&translation.exact)?,
            starts: translation.relaxed.as_deref().map(compile).transpose()?,
            whole: OnceLock::new(),
            non_empty: OnceLock::new(),
        })
    }

    /// A pattern written in the code, which is valid: Python compiles the same text when its
    /// module loads, and the tests run every one.
    #[expect(
        clippy::expect_used,
        reason = "a pattern written in the code is valid, as Python's import of the same text shows"
    )]
    pub(crate) fn from_literal(pattern: &'static str, flags: Flags) -> Self {
        Self::new(pattern, flags).expect("a pattern written in the code compiles")
    }

    /// Whether the pattern matches anywhere in the text: `re.search(...)` is not None.
    pub fn is_match(&self, text: &str) -> bool {
        match self.starts {
            None => self.program.is_match(text).unwrap_or(false),
            Some(_) => self.search(text).is_some(),
        }
    }

    /// `re.search`: the first match.
    pub fn search<'h>(&self, text: &'h str) -> Option<Match<'h>> {
        self.search_from(text, 0)
    }

    /// The first match at or after `pos`.
    fn search_from<'h>(&self, text: &'h str, pos: usize) -> Option<Match<'h>> {
        // No match starts before the first place the pattern without look-around matches.
        let start = match &self.starts {
            Some(starts) => starts.find_from_pos(text, pos).ok().flatten()?.start(),
            None => pos,
        };
        captures(&self.program, text, start)
    }

    /// `re.match`: a match that starts at the start of the text.
    pub fn r#match<'h>(&self, text: &'h str) -> Option<Match<'h>> {
        // The first match starts at 0 whenever any match does, and is then the one Python finds.
        self.search(text).filter(|found| found.start() == 0)
    }

    /// `re.fullmatch`: a match that spans the whole text.
    pub fn fullmatch<'h>(&self, text: &'h str) -> Option<Match<'h>> {
        let whole = self
            .whole
            .get_or_init(|| build(&format!(r"\A(?:{})\z", self.program.as_str()), false));
        captures(whole.as_ref()?, text, 0)
    }

    /// `re.finditer`: the matches from left to right, empty ones included.
    pub fn finditer<'r, 'h>(&'r self, text: &'h str) -> Matches<'r, 'h> {
        Matches {
            regex: self,
            text,
            pos: 0,
            after_empty: false,
            done: false,
        }
    }

    /// `re.sub(repl, text, count)` with a template: `\1` and `\g<name>` insert a group (nothing for
    /// a group that did not take part), `\\` a backslash, `\n` and the other letter escapes their
    /// character, and any other escape stays as written. `count` 0 replaces every match.
    ///
    /// A template Python would reject, naming a group the pattern lacks or using an unknown letter
    /// escape, inserts nothing for that group and keeps that escape as written.
    pub fn sub(&self, repl: &str, text: &str, count: usize) -> String {
        self.sub_with(|found| expand(repl, found), text, count)
    }

    /// `re.sub(function, text, count)`: each match replaced by what `replace` returns for it.
    pub fn sub_with(
        &self,
        mut replace: impl FnMut(&Match<'_>) -> String,
        text: &str,
        count: usize,
    ) -> String {
        let mut out = String::with_capacity(text.len());
        let mut last = 0;
        for (index, found) in self.finditer(text).enumerate() {
            if count != 0 && index == count {
                break;
            }
            out.push_str(&text[last..found.start()]);
            out.push_str(&replace(&found));
            last = found.end();
        }
        out.push_str(&text[last..]);
        out
    }

    /// `re.split(text)` for a pattern without groups: the text between the matches. Python also
    /// lists the text of each group, so a pattern with groups is not split this way.
    pub fn split<'h>(&self, text: &'h str) -> Vec<&'h str> {
        let mut pieces = Vec::new();
        let mut last = 0;
        for found in self.finditer(text) {
            pieces.push(&text[last..found.start()]);
            last = found.end();
        }
        pieces.push(&text[last..]);
        pieces
    }

    /// The first match at or after `pos`, the search that follows an empty match ending there.
    ///
    /// Python first tries for a non-empty match at `pos` itself, then goes on one character later.
    fn after_empty<'h>(&self, text: &'h str, pos: usize) -> Option<Match<'h>> {
        let non_empty = self
            .non_empty
            .get_or_init(|| build(self.program.as_str(), true));
        if let Some(captures) = non_empty.as_ref().and_then(|program| {
            let at_pos = RegexInput::new(text).from_pos(pos).anchored(true);
            program.captures_input(at_pos).ok().flatten()
        }) {
            return Some(Match { captures });
        }
        let next = pos + text[pos..].chars().next()?.len_utf8();
        self.search_from(text, next)
    }
}

/// Compiles translated syntax; None only if fancy-regex refuses it.
fn build(pattern: &str, find_not_empty: bool) -> Option<fancy_regex::Regex> {
    RegexBuilder::new(pattern)
        // Python's engine has no backtracking limit, and a search that hit one would fail.
        .backtrack_limit(usize::MAX)
        .find_not_empty(find_not_empty)
        .build()
        .ok()
}

/// The first match at or after `pos`; the text before `pos` is still seen by look-behind.
fn captures<'h>(program: &fancy_regex::Regex, text: &'h str, pos: usize) -> Option<Match<'h>> {
    // fancy-regex fails a search only when it reaches its backtracking limit, which is off.
    program
        .captures_from_pos(text, pos)
        .ok()
        .flatten()
        .map(|captures| Match { captures })
}

/// One match and its groups.
#[derive(Debug)]
pub struct Match<'h> {
    captures: Captures<'h, str>,
}

impl<'h> Match<'h> {
    /// `m.start()`.
    pub fn start(&self) -> usize {
        self.captures.get(0).map_or(0, |whole| whole.start())
    }

    /// `m.end()`.
    pub fn end(&self) -> usize {
        self.captures.get(0).map_or(0, |whole| whole.end())
    }

    /// `m.group(0)`.
    pub fn as_str(&self) -> &'h str {
        self.captures.get(0).map_or("", |whole| whole.as_str())
    }

    /// `m.group(index)`: None when the group did not take part in the match.
    pub fn group(&self, index: usize) -> Option<&'h str> {
        self.captures.get(index).map(|group| group.as_str())
    }

    /// `m.group(name)`.
    pub fn name(&self, name: &str) -> Option<&'h str> {
        self.captures.name(name).map(|group| group.as_str())
    }

    /// `(m.start(index), m.end(index))`, or None when the group did not take part in the match.
    pub fn span(&self, index: usize) -> Option<(usize, usize)> {
        self.captures
            .get(index)
            .map(|group| (group.start(), group.end()))
    }
}

/// The matches of `Regex::finditer`.
pub struct Matches<'r, 'h> {
    regex: &'r Regex,
    text: &'h str,
    pos: usize,
    after_empty: bool,
    done: bool,
}

impl<'h> Iterator for Matches<'_, 'h> {
    type Item = Match<'h>;

    fn next(&mut self) -> Option<Match<'h>> {
        if self.done {
            return None;
        }
        let found = if self.after_empty {
            self.regex.after_empty(self.text, self.pos)
        } else {
            self.regex.search_from(self.text, self.pos)
        };
        match &found {
            Some(found) => {
                self.pos = found.end();
                self.after_empty = found.start() == found.end();
            }
            None => self.done = true,
        }
        found
    }
}

/// Python's `re.escape`.
pub fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if "()[]{}?*+-|^$\\.&~# \t\n\r\x0b\x0c".contains(c) {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

/// A `re.sub` template filled in for one match.
fn expand(template: &str, found: &Match<'_>) -> String {
    let mut out = String::with_capacity(template.len());
    let mut rest = template;
    while let Some(at) = rest.find('\\') {
        out.push_str(&rest[..at]);
        rest = &rest[at + 1..];
        let mut chars = rest.chars();
        let Some(c) = chars.next() else {
            out.push('\\');
            break;
        };
        let after = chars.as_str();
        rest = match c {
            'g' if after.starts_with('<') => match after.find('>') {
                Some(close) => {
                    let reference = &after[1..close];
                    let text = match reference.parse::<usize>() {
                        Ok(index) => found.group(index),
                        Err(_) => found.name(reference),
                    };
                    out.push_str(text.unwrap_or(""));
                    &after[close + 1..]
                }
                None => {
                    out.push_str("\\g");
                    after
                }
            },
            '1'..='9' => {
                // A group number has at most two digits.
                let digits = 1 + after
                    .chars()
                    .next()
                    .filter(char::is_ascii_digit)
                    .map_or(0, |_| 1);
                let index = rest[..digits].parse().unwrap_or(0);
                out.push_str(found.group(index).unwrap_or(""));
                &rest[digits..]
            }
            _ => {
                match c {
                    '\\' => out.push('\\'),
                    'a' => out.push('\x07'),
                    'b' => out.push('\x08'),
                    'f' => out.push('\x0c'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'v' => out.push('\x0b'),
                    c => {
                        out.push('\\');
                        out.push(c);
                    }
                }
                after
            }
        };
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    fn regex(pattern: &str) -> Regex {
        Regex::new(pattern, Flags::NONE).unwrap()
    }

    #[test]
    fn empty_matches_follow_python() {
        assert_eq!(regex("a*").sub("-", "baaab", 0), "-b--b-");
        assert_eq!(regex("|a").sub("-", "a", 0), "---");
        assert_eq!(regex(r"\b").split("a b"), ["", "a", " ", "b", ""]);
    }

    #[test]
    fn sub_reads_python_templates_and_counts() {
        let template = r"[\1\g<0>\\\n\-]";
        assert_eq!(
            regex("(a)|b").sub(template, "ab", 0),
            "[aa\\\n\\-][b\\\n\\-]"
        );
        assert_eq!(regex("a").sub("x", "aaa", 2), "xxa");
        let upper = regex("[a-z]+").sub_with(|found| found.as_str().to_uppercase(), "ab-cd", 0);
        assert_eq!(upper, "AB-CD");
    }

    #[test]
    fn fullmatch_backtracks_to_the_end_where_match_takes_the_first_alternative() {
        let either = regex("a|ab");
        assert_eq!(either.r#match("ab").unwrap().as_str(), "a");
        assert_eq!(either.fullmatch("ab").unwrap().as_str(), "ab");
        assert!(either.r#match("ba").is_none());
    }

    #[test]
    fn patterns_python_rejects_do_not_compile() {
        for pattern in [
            "a**",
            "*a",
            r"(?<=a+)b",
            r"\q",
            "(a",
            "a)",
            "[z-a]",
            "x{3,2}",
        ] {
            assert!(Regex::new(pattern, Flags::NONE).is_err(), "{pattern}");
        }
    }
}
