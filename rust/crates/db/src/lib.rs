//! The database: its tables, how values are stored in them, opening it and migrating its schema.
//! Everything here is synchronous.

#![forbid(unsafe_code)]

mod error;
pub mod schema;
pub mod storage;

pub use error::{Error, Result, UnknownName};
pub use rusqlite;
pub use storage::StoredEnum;
