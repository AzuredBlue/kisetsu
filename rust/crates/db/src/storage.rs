//! How values sit in the database file: enum names, timestamps, JSON lists and the macros that
//! turn a column list into a row struct.

use std::collections::HashSet;
use std::error::Error as StdError;
use std::fmt::Write as _;

use jiff::Timestamp;
use jiff::civil::DateTime;
use jiff::tz::TimeZone;
use kisetsu_domain::enums::EpisodeStatus;
use rusqlite::types::{Type, Value, ValueRef};
use rusqlite::{Error as SqlError, Row};

use crate::error::UnknownName;

/// An enum stored as the upper-case name of its member. Members are never removed: a row holding
/// one would no longer load.
pub trait StoredEnum: Sized + Copy + 'static {
    const KIND: &'static str;
    const ALL: &'static [Self];

    fn name(self) -> &'static str;

    fn from_name(name: &str) -> Option<Self> {
        Self::ALL
            .iter()
            .copied()
            .find(|member| member.name() == name)
    }
}

macro_rules! stored_enum {
    (
        $(#[$meta:meta])*
        $name:ident {
            $($(#[$vmeta:meta])* $variant:ident => $stored:literal),+ $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $($(#[$vmeta])* $variant),+
        }

        impl $crate::storage::StoredEnum for $name {
            const KIND: &'static str = stringify!($name);
            const ALL: &'static [Self] = &[$(Self::$variant),+];

            fn name(self) -> &'static str {
                match self {
                    $(Self::$variant => $stored),+
                }
            }
        }
    };
}
pub(crate) use stored_enum;

impl StoredEnum for EpisodeStatus {
    const KIND: &'static str = "EpisodeStatus";
    const ALL: &'static [Self] = &[
        Self::Wanted,
        Self::Downloading,
        Self::Completed,
        Self::Failed,
        Self::Missed,
        Self::Skipped,
    ];

    fn name(self) -> &'static str {
        match self {
            Self::Wanted => "WANTED",
            Self::Downloading => "DOWNLOADING",
            Self::Completed => "COMPLETED",
            Self::Failed => "FAILED",
            Self::Missed => "MISSED",
            Self::Skipped => "SKIPPED",
        }
    }
}

/// Timestamps are naive UTC text with six fractional digits, `2026-11-20 22:59:25.148516`.
pub fn format_timestamp(timestamp: Timestamp) -> String {
    let at = timestamp.to_zoned_datetime(TimeZone::UTC).datetime();
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}.{:06}",
        at.year(),
        at.month(),
        at.day(),
        at.hour(),
        at.minute(),
        at.second(),
        at.subsec_nanosecond() / 1000,
    )
}

/// Reads a stored timestamp, with or without the fractional digits.
pub fn parse_timestamp(text: &str) -> Option<Timestamp> {
    let at: DateTime = text.parse().ok()?;
    Some(at.to_zoned_datetime(TimeZone::UTC).ok()?.timestamp())
}

/// The current time at the precision the database keeps.
pub fn utc_now() -> Timestamp {
    let now = Timestamp::now();
    Timestamp::from_microsecond(now.as_microsecond()).unwrap_or(now)
}

/// A JSON array of strings with repeats dropped in order, in the stored text format of existing
/// rows: `", "` between items and everything outside printable ASCII escaped.
pub fn encode_list<S: AsRef<str>>(items: &[S]) -> String {
    let mut seen = HashSet::new();
    let mut out = String::from("[");
    for item in items
        .iter()
        .map(AsRef::as_ref)
        .filter(|item| seen.insert(*item))
    {
        if out.len() > 1 {
            out.push_str(", ");
        }
        out.push('"');
        for c in item.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                ' '..='~' => out.push(c),
                _ => {
                    for unit in c.encode_utf16(&mut [0; 2]) {
                        let _ = write!(out, "\\u{unit:04x}");
                    }
                }
            }
        }
        out.push('"');
    }
    out.push(']');
    out
}

/// The strings of a stored JSON array; anything else reads as empty.
pub fn decode_list(raw: &str) -> Vec<String> {
    serde_json::from_str(raw).unwrap_or_default()
}

/// A value that has a column representation.
pub(crate) trait Column: Sized {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self>;
    fn to_value(&self) -> Value;
}

fn conversion_failed(
    index: usize,
    kind: Type,
    source: Box<dyn StdError + Send + Sync>,
) -> SqlError {
    SqlError::FromSqlConversionFailure(index, kind, source)
}

impl Column for i64 {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        row.get(index)
    }
    fn to_value(&self) -> Value {
        Value::Integer(*self)
    }
}

impl Column for f64 {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        row.get(index)
    }
    fn to_value(&self) -> Value {
        Value::Real(*self)
    }
}

impl Column for bool {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        row.get(index)
    }
    fn to_value(&self) -> Value {
        Value::Integer(i64::from(*self))
    }
}

impl Column for String {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        row.get(index)
    }
    fn to_value(&self) -> Value {
        Value::Text(self.clone())
    }
}

impl Column for Timestamp {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        let text: String = row.get(index)?;
        parse_timestamp(&text).ok_or_else(|| {
            conversion_failed(
                index,
                Type::Text,
                format!("not a timestamp: {text:?}").into(),
            )
        })
    }
    fn to_value(&self) -> Value {
        Value::Text(format_timestamp(*self))
    }
}

impl<T: StoredEnum> Column for T {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        let name: String = row.get(index)?;
        T::from_name(&name).ok_or_else(|| {
            conversion_failed(
                index,
                Type::Text,
                Box::new(UnknownName {
                    kind: T::KIND,
                    name,
                }),
            )
        })
    }
    fn to_value(&self) -> Value {
        Value::Text(self.name().to_owned())
    }
}

impl<T: Column> Column for Option<T> {
    fn read(row: &Row<'_>, index: usize) -> rusqlite::Result<Self> {
        match row.get_ref(index)? {
            ValueRef::Null => Ok(None),
            _ => T::read(row, index).map(Some),
        }
    }
    fn to_value(&self) -> Value {
        self.as_ref().map_or(Value::Null, Column::to_value)
    }
}

/// Reads a row's columns in order.
pub(crate) struct Cursor<'r, 'stmt> {
    row: &'r Row<'stmt>,
    next: usize,
}

impl<'r, 'stmt> Cursor<'r, 'stmt> {
    pub(crate) fn new(row: &'r Row<'stmt>) -> Self {
        Self { row, next: 0 }
    }

    pub(crate) fn take<T: Column>(&mut self) -> rusqlite::Result<T> {
        let index = self.next;
        self.next += 1;
        T::read(self.row, index)
    }
}

/// Declares a table as a struct: one field per column, in the order of `COLUMNS`.
macro_rules! table {
    (
        $(#[$meta:meta])*
        $name:ident, $table:literal {
            $(#[$first_meta:meta])* $first:ident: $first_type:ty
            $(, $(#[$field_meta:meta])* $field:ident: $field_type:ty)* $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq)]
        pub struct $name {
            $(#[$first_meta])* pub $first: $first_type,
            $($(#[$field_meta])* pub $field: $field_type,)*
        }

        impl $name {
            pub const TABLE: &'static str = $table;
            /// The quoted column names, ready for a `SELECT` list.
            pub const COLUMNS: &'static str = concat!(
                "\"", stringify!($first), "\""
                $(, ", \"", stringify!($field), "\"")*
            );

            /// Reads a row selected with `COLUMNS`.
            pub fn from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Self> {
                let mut cursor = $crate::storage::Cursor::new(row);
                Ok(Self {
                    $first: cursor.take()?,
                    $($field: cursor.take()?,)*
                })
            }

            /// Inserts the row and returns its rowid.
            pub fn insert(&self, conn: &rusqlite::Connection) -> rusqlite::Result<i64> {
                use $crate::storage::Column;
                let values = [
                    self.$first.to_value()
                    $(, self.$field.to_value())*
                ];
                let marks = vec!["?"; values.len()].join(", ");
                let sql = format!(
                    "INSERT INTO {} ({}) VALUES ({marks})",
                    Self::TABLE,
                    Self::COLUMNS,
                );
                conn.prepare_cached(&sql)?.execute(rusqlite::params_from_iter(values))?;
                Ok(conn.last_insert_rowid())
            }
        }
    };
}
pub(crate) use table;

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests build fixed values")]
mod tests {
    use super::*;

    #[test]
    fn timestamps_read_back_with_or_without_fraction() {
        let want = Timestamp::from_second(1_767_323_045).unwrap();
        assert_eq!(parse_timestamp("2026-01-02 03:04:05"), Some(want));
        assert_eq!(parse_timestamp("2026-01-02 03:04:05.000000"), Some(want));
        assert_eq!(
            parse_timestamp("2026-01-02 03:04:05.5"),
            Some(Timestamp::from_microsecond(1_767_323_045_500_000).unwrap())
        );
        assert_eq!(parse_timestamp("yesterday"), None);
    }

    #[test]
    fn lists_that_are_not_string_arrays_read_as_empty() {
        assert_eq!(decode_list(r#"["a", "b"]"#), ["a", "b"]);
        assert!(decode_list("not json").is_empty());
        assert!(decode_list(r#"{"a": 1}"#).is_empty());
        assert!(decode_list("").is_empty());
    }
}
