//! Python `str` methods whose character rules differ from Rust's: Python's whitespace includes
//! `\x1c`-`\x1f`, its `isdigit` accepts superscripts and circled digits, its `isalpha` only letters,
//! and `int` reads the decimal digits of every script.

use crate::unicode::{ALPHA, DECIMAL, DIGIT, SPACE};

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
