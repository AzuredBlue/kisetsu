//! Writes a pattern tree as fancy-regex syntax that matches what Python's `re` matches.
//!
//! Every character set is written out as explicit code point ranges taken from Python's tables, so
//! no Unicode data of the Rust crates takes part: `\d`, `\s`, `\w` and `\b` follow Python's
//! definitions, and case-insensitive matching follows Python's rule: a character matches when its
//! simple lowercase form is one the pattern accepts, where a pattern's cased character accepts its
//! own lowercase form and the lowercase forms that share its uppercase form.

use std::fmt::Write;
use std::sync::LazyLock;

use regex_syntax::hir::{ClassUnicode, ClassUnicodeRange};

use super::syntax::{Category, Item, Node, Set};
use crate::unicode::{DECIMAL, LOWER, LOWER_INVERSE, SAME_UPPERCASE, SPACE, WORD};

pub(super) struct Translation {
    /// The pattern itself.
    pub exact: String,
    /// The pattern without its look-around assertions, when it has any. It matches wherever the
    /// pattern does and needs no backtracking, so it finds where a match can start.
    pub relaxed: Option<String>,
}

pub(super) fn translate(node: &Node, ignore_case: bool) -> Translation {
    let write = |relaxed| {
        let mut out = String::new();
        Translator {
            ignore_case,
            relaxed,
        }
        .write(node, &mut out);
        out
    };
    let exact = write(false);
    let relaxed = Some(write(true)).filter(|relaxed| *relaxed != exact);
    Translation { exact, relaxed }
}

/// Python's `\w` as a set, written once because every `\b` repeats it four times.
static WORD_SET: LazyLock<String> = LazyLock::new(|| {
    let mut out = String::new();
    write_set(&table(WORD), &mut out);
    out
});

struct Translator {
    ignore_case: bool,
    /// Leave out the assertions that need look-around.
    relaxed: bool,
}

impl Translator {
    fn write(&self, node: &Node, out: &mut String) {
        match node {
            Node::Literal(c) => {
                if self.ignore_case {
                    write_set(&case_closure(&lowered(&single(*c))), out);
                } else {
                    write_char(*c, out);
                }
            }
            Node::Set(set) => write_set(&self.set(set), out),
            Node::Start => out.push_str(r"\A"),
            Node::EndOfText => out.push_str(r"\z"),
            Node::End | Node::WordBoundary | Node::Look { .. } if self.relaxed => {}
            Node::End => out.push_str(r"(?=\x{a}?\z)"),
            Node::WordBoundary => {
                let word = &*WORD_SET;
                // Python's `\b` never matches in an empty text, and neither does this.
                let _ = write!(out, "(?:(?<={word})(?!{word})|(?<!{word})(?={word}))");
            }
            Node::Capture(name, node) => {
                out.push('(');
                if let Some(name) = name {
                    let _ = write!(out, "?P<{name}>");
                }
                self.write(node, out);
                out.push(')');
            }
            Node::Group(node) => self.group(node, out),
            Node::Look {
                behind,
                negated,
                node,
            } => {
                out.push_str(match (behind, negated) {
                    (false, false) => "(?=",
                    (false, true) => "(?!",
                    (true, false) => "(?<=",
                    (true, true) => "(?<!",
                });
                self.write(node, out);
                out.push(')');
            }
            Node::Repeat {
                node,
                min,
                max,
                greedy,
            } => {
                self.group(node, out);
                let _ = match max {
                    Some(max) => write!(out, "{{{min},{max}}}"),
                    None => write!(out, "{{{min},}}"),
                };
                if !greedy {
                    out.push('?');
                }
            }
            Node::Concat(nodes) => {
                for node in nodes {
                    self.write(node, out);
                }
            }
            // The parser puts an alternation only at the top or directly inside a group.
            Node::Alternation(branches) => {
                for (index, branch) in branches.iter().enumerate() {
                    if index > 0 {
                        out.push('|');
                    }
                    self.write(branch, out);
                }
            }
        }
    }

    fn group(&self, node: &Node, out: &mut String) {
        out.push_str("(?:");
        self.write(node, out);
        out.push(')');
    }

    /// The characters a set matches. Under IGNORECASE Python compares the lowercase form of the
    /// text's character with the set's characters lowercased, while categories are not lowercased.
    fn set(&self, set: &Set) -> ClassUnicode {
        let mut chars = ClassUnicode::empty();
        let mut categories = ClassUnicode::empty();
        for item in &set.items {
            match *item {
                Item::Range(low, high) => chars.push(ClassUnicodeRange::new(low, high)),
                Item::Category(category, negated) => {
                    let mut class = table(match category {
                        Category::Digit => DECIMAL,
                        Category::Space => SPACE,
                        Category::Word => WORD,
                    });
                    if negated {
                        class.negate();
                    }
                    categories.union(&class);
                }
            }
        }
        let mut class = if self.ignore_case {
            let mut accepted = lowered(&chars);
            accepted.union(&categories);
            case_closure(&accepted)
        } else {
            chars.union(&categories);
            chars
        };
        if set.negated {
            class.negate();
        }
        class
    }
}

fn table(ranges: &[(char, char)]) -> ClassUnicode {
    ClassUnicode::new(
        ranges
            .iter()
            .map(|&(low, high)| ClassUnicodeRange::new(low, high)),
    )
}

fn single(c: char) -> ClassUnicode {
    ClassUnicode::new([ClassUnicodeRange::new(c, c)])
}

/// The `(char, lowercase)` pairs of a table sorted by its first column whose first column lies in
/// the class.
fn pairs_within<'t>(
    pairs: &'t [(char, char)],
    class: &'t ClassUnicode,
) -> impl Iterator<Item = (char, char)> + 't {
    class.ranges().iter().flat_map(move |range| {
        let from = pairs.partition_point(|&(key, _)| key < range.start());
        pairs[from..]
            .iter()
            .take_while(move |&&(key, _)| key <= range.end())
            .copied()
    })
}

/// Every character of the class lowercased, plus the lowercase forms that share an uppercase form
/// with one of those.
fn lowered(class: &ClassUnicode) -> ClassUnicode {
    let changed: Vec<(char, char)> = pairs_within(LOWER, class).collect();
    let mut result = class.clone();
    result.difference(&ClassUnicode::new(
        changed.iter().map(|&(c, _)| ClassUnicodeRange::new(c, c)),
    ));
    result.union(&ClassUnicode::new(
        changed
            .iter()
            .map(|&(_, lower)| ClassUnicodeRange::new(lower, lower)),
    ));
    let alike: Vec<ClassUnicodeRange> = SAME_UPPERCASE
        .iter()
        .filter(|(lower, _)| contains(&result, *lower))
        .flat_map(|(_, others)| others.iter().map(|&c| ClassUnicodeRange::new(c, c)))
        .collect();
    result.union(&ClassUnicode::new(alike));
    result
}

/// The characters whose lowercase form is in the class.
fn case_closure(class: &ClassUnicode) -> ClassUnicode {
    let mut result = class.clone();
    result.difference(&ClassUnicode::new(
        pairs_within(LOWER, class).map(|(c, _)| ClassUnicodeRange::new(c, c)),
    ));
    result.union(&ClassUnicode::new(
        pairs_within(LOWER_INVERSE, class).map(|(_, c)| ClassUnicodeRange::new(c, c)),
    ));
    result
}

fn contains(class: &ClassUnicode, c: char) -> bool {
    let ranges = class.ranges();
    let index = ranges.partition_point(|range| range.end() < c);
    ranges.get(index).is_some_and(|range| range.start() <= c)
}

fn write_set(class: &ClassUnicode, out: &mut String) {
    match class.ranges() {
        [] => out.push_str(r"[^\x{0}-\x{10ffff}]"),
        [range] if range.start() == range.end() => write_char(range.start(), out),
        ranges => {
            out.push('[');
            for range in ranges {
                write_char(range.start(), out);
                if range.end() != range.start() {
                    out.push('-');
                    write_char(range.end(), out);
                }
            }
            out.push(']');
        }
    }
}

/// A character as itself when it means nothing in the syntax, else as a code point escape.
fn write_char(c: char, out: &mut String) {
    if c.is_ascii_alphanumeric() {
        out.push(c);
    } else {
        let _ = write!(out, r"\x{{{:x}}}", u32::from(c));
    }
}
