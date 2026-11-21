//! Regular expressions over release titles.
//!
//! Patterns are compiled by `fancy-regex`, which hands every pattern without look-around to the
//! `regex` engine and runs only the ones with look-around on its backtracking matcher. Character
//! classes, word boundaries and case-insensitive matching use the crates' Unicode rules.
//!
//! Offsets are byte offsets into the searched text.

use std::borrow::Cow;

use fancy_regex::{Captures, RegexBuilder};

pub use fancy_regex::Error;

/// Whether letters match regardless of case.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Case {
    Sensitive,
    Insensitive,
}

/// The size of the lazily built automaton a search may keep, in bytes.
const DEFAULT_DFA_CACHE: usize = 2 * 1024 * 1024;

/// A compiled pattern.
///
/// A search that fails (the backtracking limit of a pattern with look-around) finds nothing.
#[derive(Debug)]
pub struct Regex {
    /// None for a pattern written in the code that does not compile: it matches nothing.
    program: Option<fancy_regex::Regex>,
}

impl Regex {
    /// A pattern that is kept for a long time, such as a show's: compiled and searched with as
    /// little memory as possible, which makes a search slower than a built-in pattern's.
    pub fn new(pattern: &str, case: Case) -> Result<Self, Error> {
        Self::build(pattern, case, 0)
    }

    /// A pattern written in the code. The tests compile every one.
    pub(crate) fn literal(pattern: &str, case: Case) -> Self {
        Self::build(pattern, case, DEFAULT_DFA_CACHE).unwrap_or(Self { program: None })
    }

    fn build(pattern: &str, case: Case, dfa_cache: usize) -> Result<Self, Error> {
        let program = RegexBuilder::new(pattern)
            .case_insensitive(case == Case::Insensitive)
            // Skips to where a match can start instead of trying every position of a pattern
            // with look-around.
            .seek(true)
            .delegate_dfa_size_limit(dfa_cache)
            .build()?;
        Ok(Self {
            program: Some(program),
        })
    }

    #[cfg(test)]
    pub(crate) fn compiled(&self) -> bool {
        self.program.is_some()
    }

    pub fn is_match(&self, text: &str) -> bool {
        self.program
            .as_ref()
            .is_some_and(|program| program.is_match(text).unwrap_or(false))
    }

    /// The first match.
    pub fn find<'h>(&self, text: &'h str) -> Option<Match<'h>> {
        let captures = self.program.as_ref()?.captures(text).ok().flatten()?;
        Match::new(captures)
    }

    /// The matches from left to right.
    pub fn find_iter<'h>(&self, text: &'h str) -> impl Iterator<Item = Match<'h>> {
        self.program
            .iter()
            .flat_map(|program| program.captures_iter(text))
            .map_while(|captures| Match::new(captures.ok()?))
    }

    /// Every match replaced by `template`, in which `$1` and `${name}` insert a group (nothing for
    /// a group that did not take part) and `$$` a dollar sign.
    pub fn replace_all<'h>(&self, text: &'h str, template: &str) -> Cow<'h, str> {
        if template.contains('$') {
            self.replace_each(text, |found, out| found.captures.expand(template, out))
        } else {
            self.replace_each(text, |_, out| out.push_str(template))
        }
    }

    /// Every match replaced by what `replace` returns for it.
    pub fn replace_all_with<'h>(
        &self,
        text: &'h str,
        mut replace: impl FnMut(&Match<'_>) -> String,
    ) -> Cow<'h, str> {
        self.replace_each(text, |found, out| out.push_str(&replace(found)))
    }

    fn replace_each<'h>(
        &self,
        text: &'h str,
        mut write: impl FnMut(&Match<'h>, &mut String),
    ) -> Cow<'h, str> {
        let mut matches = self.find_iter(text).peekable();
        if matches.peek().is_none() {
            return Cow::Borrowed(text);
        }
        let mut out = String::with_capacity(text.len());
        let mut last = 0;
        for found in matches {
            out.push_str(&text[last..found.start()]);
            write(&found, &mut out);
            last = found.end();
        }
        out.push_str(&text[last..]);
        Cow::Owned(out)
    }
}

/// One match and its groups.
#[derive(Debug)]
pub struct Match<'h> {
    captures: Captures<'h, str>,
    start: usize,
    end: usize,
}

impl<'h> Match<'h> {
    fn new(captures: Captures<'h, str>) -> Option<Self> {
        let whole = captures.get(0)?;
        let (start, end) = (whole.start(), whole.end());
        Some(Self {
            captures,
            start,
            end,
        })
    }

    pub fn start(&self) -> usize {
        self.start
    }

    pub fn end(&self) -> usize {
        self.end
    }

    pub fn as_str(&self) -> &'h str {
        self.group(0).unwrap_or_default()
    }

    /// A group by number; None when it did not take part in the match.
    pub fn group(&self, index: usize) -> Option<&'h str> {
        self.captures.get(index).map(|group| group.as_str())
    }

    /// A group by name.
    pub fn name(&self, name: &str) -> Option<&'h str> {
        self.captures.name(name).map(|group| group.as_str())
    }

    /// The start and end of a group, or None when it did not take part in the match.
    pub fn span(&self, index: usize) -> Option<(usize, usize)> {
        self.captures
            .get(index)
            .map(|group| (group.start(), group.end()))
    }
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    fn regex(pattern: &str) -> Regex {
        Regex::new(pattern, Case::Sensitive).unwrap()
    }

    #[test]
    fn replace_all_expands_groups_and_borrows_when_nothing_matches() {
        let swapped = regex(r"(\w+)-(\w+)").replace_all("ab-cd ef-gh", "$2-$1");
        assert_eq!(swapped, "cd-ab gh-ef");
        let untouched = regex("x").replace_all("abc", "y");
        assert!(matches!(untouched, Cow::Borrowed("abc")));
        let upper =
            regex("[a-z]+").replace_all_with("ab-cd", |found| found.as_str().to_uppercase());
        assert_eq!(upper, "AB-CD");
    }

    #[test]
    fn look_around_and_named_groups_find_their_text() {
        let found = regex(r"(?<![a-z\d])(?P<n>\d+)(?!\d)")
            .find("ab12 345")
            .unwrap();
        assert_eq!(found.name("n"), Some("345"));
        assert_eq!(found.span(1), Some((5, 8)));
    }

    #[test]
    fn invalid_patterns_do_not_compile() {
        for pattern in ["a**", "(a", "a)", "[z-a]", r"(?<=a+)b"] {
            assert!(Regex::new(pattern, Case::Sensitive).is_err(), "{pattern}");
        }
    }

    #[test]
    fn case_insensitive_matching_folds_non_ascii_letters() {
        let caseless = Regex::new("é", Case::Insensitive).unwrap();
        assert!(caseless.is_match("É"));
        assert!(!regex("é").is_match("É"));
    }
}
