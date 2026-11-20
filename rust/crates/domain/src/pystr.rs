//! Python `str` methods whose character rules differ from Rust's: Python's whitespace includes
//! `\x1c`-`\x1f`, its `isdigit` accepts superscripts and circled digits, its `isalpha` only letters,
//! `int` reads the decimal digits of every script, and `lower` follows Python's Unicode version.

use crate::unicode::{ALPHA, CASE_IGNORABLE, CASED, DECIMAL, DIGIT, LOWER, SPACE, WORD};

/// The range of a sorted table that holds `c`.
fn range_of(table: &[(char, char)], c: char) -> Option<(char, char)> {
    let index = table.partition_point(|&(_, high)| high < c);
    table.get(index).copied().filter(|&(low, _)| low <= c)
}

fn is_space(c: char) -> bool {
    range_of(SPACE, c).is_some()
}

/// `text.strip()`.
pub(crate) fn strip(text: &str) -> &str {
    text.trim_matches(is_space)
}

/// `text.lstrip()`.
pub(crate) fn lstrip(text: &str) -> &str {
    text.trim_start_matches(is_space)
}

/// `text.split()`: the words between runs of whitespace.
pub(crate) fn split(text: &str) -> impl Iterator<Item = &str> {
    text.split(is_space).filter(|word| !word.is_empty())
}

/// `text.isdigit()`.
pub(crate) fn isdigit(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| range_of(DIGIT, c).is_some())
}

/// `c.isalpha()`.
pub(crate) fn is_alpha(c: char) -> bool {
    range_of(ALPHA, c).is_some()
}

/// `str(int(digits))` for decimal digits of any script, as `\d+` matches them.
pub(crate) fn int_text(digits: &str) -> String {
    let ascii: String = digits
        .chars()
        .filter_map(|c| {
            // Each range of the table starts at a zero and runs in tens.
            let (zero, _) = range_of(DECIMAL, c)?;
            char::from_digit((u32::from(c) - u32::from(zero)) % 10, 10)
        })
        .collect();
    match ascii.trim_start_matches('0') {
        "" => "0".to_owned(),
        value => value.to_owned(),
    }
}

/// `int(digits)` for decimal digits of any script, as `\d+` matches them. A number too large for
/// an `i64` is `i64::MAX`.
pub(crate) fn int(digits: &str) -> i64 {
    digits.chars().fold(0, |value: i64, c| {
        let digit =
            range_of(DECIMAL, c).map_or(0, |(zero, _)| (u32::from(c) - u32::from(zero)) % 10);
        value.saturating_mul(10).saturating_add(i64::from(digit))
    })
}

/// Whether `\w` matches `c`: `c.isalnum()` or `_`.
pub(crate) fn is_word(c: char) -> bool {
    range_of(WORD, c).is_some()
}

/// `text.lower()`: each character's lowercase mapping, `İ` as `i̇`, and a capital sigma that ends
/// a word as `ς`.
pub(crate) fn lower(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for (index, c) in text.char_indices() {
        match c {
            'İ' => out.push_str("i\u{307}"),
            'Σ' if is_final_sigma(text, index) => out.push('ς'),
            c => out.push(
                LOWER
                    .binary_search_by_key(&c, |&(upper, _)| upper)
                    .map_or(c, |found| LOWER[found].1),
            ),
        }
    }
    out
}

/// Whether the capital sigma at byte `index` ends a word: looking past case-ignorable characters,
/// a cased character comes before it and none comes after it.
fn is_final_sigma(text: &str, index: usize) -> bool {
    fn cased_next(mut chars: impl Iterator<Item = char>) -> bool {
        chars
            .find(|&c| range_of(CASE_IGNORABLE, c).is_none())
            .is_some_and(|c| range_of(CASED, c).is_some())
    }
    cased_next(text[..index].chars().rev()) && !cased_next(text[index + 'Σ'.len_utf8()..].chars())
}
