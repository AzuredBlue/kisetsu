//! Pure logic: no I/O, no clock, no database.

#![forbid(unsafe_code)]

pub mod enums;
pub mod fuzz;
pub mod matcher;
pub mod numbering;
pub mod parsing;
pub mod patterns;
pub mod pyre;
mod pystr;
pub mod schedule;
pub mod status;
mod unicode;
