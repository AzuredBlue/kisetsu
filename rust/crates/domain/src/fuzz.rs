//! Fuzzy title scores from 0 to 100: the Indel similarity of two strings (`ratio`), and the same
//! after sorting each string's words (`token_sort_ratio`) or comparing the shared and the
//! remaining words (`token_set_ratio`).
//!
//! Lengths and edits count code points. Words are separated by whitespace.

use std::collections::BTreeSet;

/// The normalized Indel similarity of the two strings, from 0 to 100.
pub fn ratio(s1: &str, s2: &str) -> f64 {
    let first: Vec<char> = s1.chars().collect();
    let second: Vec<char> = s2.chars().collect();
    let total = first.len() + second.len();
    if total == 0 {
        return 100.0;
    }
    (1.0 - indel_distance(&first, &second) as f64 / total as f64) * 100.0
}

/// `ratio` of the strings with their words sorted.
pub fn token_sort_ratio(s1: &str, s2: &str) -> f64 {
    ratio(&sorted_words(s1), &sorted_words(s2))
}

/// The best `ratio` among the shared words alone and the shared words
/// followed by each string's other words. 0 when either string has no words.
pub fn token_set_ratio(s1: &str, s2: &str) -> f64 {
    let words_a: BTreeSet<&str> = s1.split_whitespace().collect();
    let words_b: BTreeSet<&str> = s2.split_whitespace().collect();
    if words_a.is_empty() || words_b.is_empty() {
        return 0.0;
    }
    let shared: Vec<&str> = words_a.intersection(&words_b).copied().collect();
    let only_a: Vec<char> = joined(words_a.difference(&words_b).copied());
    let only_b: Vec<char> = joined(words_b.difference(&words_a).copied());
    if !shared.is_empty() && (only_a.is_empty() || only_b.is_empty()) {
        return 100.0;
    }

    let shared_len = joined(shared.iter().copied()).len();
    // The space between the shared words and the others.
    let separator = usize::from(shared_len != 0);
    let shared_a_len = shared_len + separator + only_a.len();
    let shared_b_len = shared_len + separator + only_b.len();

    let result = norm_distance(
        indel_distance(&only_a, &only_b),
        shared_a_len + shared_b_len,
    );
    if shared_len == 0 {
        return result;
    }
    // The shared words against the shared words followed by one string's others differ only by
    // what follows.
    let shared_a_ratio = norm_distance(separator + only_a.len(), shared_len + shared_a_len);
    let shared_b_ratio = norm_distance(separator + only_b.len(), shared_len + shared_b_len);
    result.max(shared_a_ratio).max(shared_b_ratio)
}

/// A distance as a score from 0 to 100, written the way token scores are computed.
fn norm_distance(distance: usize, total: usize) -> f64 {
    if total == 0 {
        return 100.0;
    }
    100.0 - 100.0 * distance as f64 / total as f64
}

fn sorted_words(text: &str) -> String {
    let mut words: Vec<&str> = text.split_whitespace().collect();
    words.sort_unstable();
    words.join(" ")
}

/// The words joined by single spaces, as code points.
fn joined<'a>(words: impl Iterator<Item = &'a str>) -> Vec<char> {
    let mut out = Vec::new();
    for word in words {
        if !out.is_empty() {
            out.push(' ');
        }
        out.extend(word.chars());
    }
    out
}

/// The insertions and deletions that turn one sequence into the other.
fn indel_distance(first: &[char], second: &[char]) -> usize {
    first.len() + second.len() - 2 * longest_common_subsequence(first, second)
}

fn longest_common_subsequence(first: &[char], second: &[char]) -> usize {
    // One row of the table: `row[j]` is the answer for the first characters read so far against
    // the first `j` of `second`.
    let mut row = vec![0; second.len() + 1];
    for &a in first {
        let mut diagonal = 0;
        for (j, &b) in second.iter().enumerate() {
            let above = row[j + 1];
            row[j + 1] = if a == b {
                diagonal + 1
            } else {
                above.max(row[j])
            };
            diagonal = above;
        }
    }
    row[second.len()]
}
