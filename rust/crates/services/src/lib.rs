//! The app's logic on top of the database and the clients. A service takes a [`context::Context`]
//! and does its work synchronously; it never commits.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

pub mod adoption;
pub mod context;
pub mod error;
pub mod events;
pub mod facts;
pub mod feeds;
pub mod feedsync;
pub mod ingest;
pub mod library;
pub mod paths;
pub mod tags;

pub use error::{Error, Result};
