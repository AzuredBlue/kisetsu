//! Regular expressions over release titles.
//!
//! A pattern without look-around is compiled by `regex-automata`; one with look-around by
//! `fancy-regex`, which runs it on its backtracking matcher. Character classes, word boundaries
//! and case-insensitive matching use the crates' Unicode rules.
//!
//! Patterns written in the code spell digits as `[0-9０-９]`, the ASCII and full-width digits
//! titles are numbered with. `\d` would take every script's digits, which makes each pattern that
//! uses it many times larger in memory.
//!
//! Offsets are byte offsets into the searched text.

use std::borrow::Cow;

use fancy_regex::RegexBuilder;
use regex_automata::meta;
use regex_automata::util::captures::Captures as Groups;
use regex_automata::util::syntax;

pub use fancy_regex::Error;

/// Whether letters match regardless of case.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Case {
    Sensitive,
    Insensitive,
}

/// Patterns written in the code, each compiled on first use. In tests, `written_regexes()` lists
/// them all, so a new pattern is always checked to compile.
macro_rules! regexes {
    ($($(#[$doc:meta])* $name:ident = $pattern:expr, $flags:ident;)*) => {
        $(
            $(#[$doc])*
            static $name: ::std::sync::LazyLock<$crate::rx::Regex> =
                ::std::sync::LazyLock::new(|| {
                    $crate::rx::Regex::literal($pattern, $crate::rx::Case::$flags)
                });
        )*

        #[cfg(test)]
        fn written_regexes() -> Vec<&'static $crate::rx::Regex> {
            vec![$(&*$name),*]
        }
    };
}
pub(crate) use regexes;

/// A compiled pattern.
///
/// A search that fails (the backtracking limit of a pattern with look-around) finds nothing.
#[derive(Debug)]
pub struct Regex {
    /// None for a pattern written in the code that does not compile: it matches nothing.
    program: Option<Program>,
}

#[derive(Debug)]
enum Program {
    Plain(meta::Regex),
    LookAround(fancy_regex::Regex),
}

impl Regex {
    /// A pattern compiled to search with as little memory as possible. Each thread that searches
    /// keeps its own scratch space per pattern, so a lazily built automaton would be kept once
    /// per thread; without one a search runs slower, and the reverse automaton it would search
    /// backwards with is not built either.
    pub fn new(pattern: &str, case: Case) -> Result<Self, Error> {
        let insensitive = case == Case::Insensitive;
        // One grammar decides what is a pattern, whichever engine then runs it.
        fancy_regex::Expr::parse_tree(pattern)?;
        let plain = meta::Regex::builder()
            .configure(meta::Config::new().hybrid(false).dfa(false))
            .syntax(syntax::Config::new().case_insensitive(insensitive))
            .build(pattern);
        if let Ok(plain) = plain {
            return Ok(Self {
                program: Some(Program::Plain(plain)),
            });
        }
        let program = RegexBuilder::new(pattern)
            .case_insensitive(insensitive)
            // Skips to where a match can start instead of trying every position.
            .seek(true)
            .delegate_dfa_size_limit(0)
            .build()?;
        Ok(Self {
            program: Some(Program::LookAround(program)),
        })
    }

    /// A pattern written in the code. The tests compile every one.
    pub(crate) fn literal(pattern: &str, case: Case) -> Self {
        Self::new(pattern, case).unwrap_or(Self { program: None })
    }

    #[cfg(test)]
    pub(crate) fn compiled(&self) -> bool {
        self.program.is_some()
    }

    pub fn is_match(&self, text: &str) -> bool {
        match &self.program {
            None => false,
            Some(Program::Plain(program)) => program.is_match(text),
            Some(Program::LookAround(program)) => program.is_match(text).unwrap_or(false),
        }
    }

    /// The first match.
    pub fn find<'h>(&self, text: &'h str) -> Option<Match<'h>> {
        match self.program.as_ref()? {
            Program::Plain(program) => {
                let mut groups = program.create_captures();
                program.captures(text, &mut groups);
                Match::plain(text, groups)
            }
            Program::LookAround(program) => {
                Match::look_around(program.captures(text).ok().flatten()?)
            }
        }
    }

    /// The matches from left to right.
    pub fn find_iter<'r, 'h: 'r>(
        &'r self,
        text: &'h str,
    ) -> Box<dyn Iterator<Item = Match<'h>> + 'r> {
        match &self.program {
            None => Box::new(std::iter::empty()),
            Some(Program::Plain(program)) => Box::new(
                program
                    .captures_iter(text)
                    .map_while(move |groups| Match::plain(text, groups)),
            ),
            Some(Program::LookAround(program)) => Box::new(
                program
                    .captures_iter(text)
                    .map_while(|captures| Match::look_around(captures.ok()?)),
            ),
        }
    }

    /// Every match replaced by `template`, in which `$1` and `${name}` insert a group (nothing for
    /// a group that did not take part) and `$$` a dollar sign.
    pub fn replace_all<'h>(&self, text: &'h str, template: &str) -> Cow<'h, str> {
        if template.contains('$') {
            self.replace_each(text, |found, out| found.expand(template, out))
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
    captures: Found<'h>,
    start: usize,
    end: usize,
}

#[derive(Debug)]
enum Found<'h> {
    Plain { text: &'h str, groups: Groups },
    LookAround(fancy_regex::Captures<'h, str>),
}

impl<'h> Match<'h> {
    fn plain(text: &'h str, groups: Groups) -> Option<Self> {
        let whole = groups.get_group(0)?;
        Some(Self {
            start: whole.start,
            end: whole.end,
            captures: Found::Plain { text, groups },
        })
    }

    fn look_around(captures: fancy_regex::Captures<'h, str>) -> Option<Self> {
        let whole = captures.get(0)?;
        let (start, end) = (whole.start(), whole.end());
        Some(Self {
            captures: Found::LookAround(captures),
            start,
            end,
        })
    }

    fn expand(&self, template: &str, out: &mut String) {
        match &self.captures {
            Found::Plain { text, groups } => groups.interpolate_string_into(text, template, out),
            Found::LookAround(captures) => captures.expand(template, out),
        }
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
        match &self.captures {
            Found::Plain { text, groups } => groups.get_group(index).map(|span| &text[span]),
            Found::LookAround(captures) => captures.get(index).map(|group| group.as_str()),
        }
    }

    /// A group by name.
    pub fn name(&self, name: &str) -> Option<&'h str> {
        match &self.captures {
            Found::Plain { text, groups } => groups.get_group_by_name(name).map(|span| &text[span]),
            Found::LookAround(captures) => captures.name(name).map(|group| group.as_str()),
        }
    }

    /// The start and end of a group, or None when it did not take part in the match.
    pub fn span(&self, index: usize) -> Option<(usize, usize)> {
        match &self.captures {
            Found::Plain { groups, .. } => {
                groups.get_group(index).map(|span| (span.start, span.end))
            }
            Found::LookAround(captures) => captures
                .get(index)
                .map(|group| (group.start(), group.end())),
        }
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
