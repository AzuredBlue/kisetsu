//! The web layer: the HTTP API, the live-update stream and the UI's files.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod api;
mod app;
mod assets;
mod check;
mod error;
mod guard;
mod http;
mod logs;
mod runtime;
mod server;
mod settings;
pub mod views;

pub use app::App;
pub use assets::Assets;
pub use guard::Guard;
pub use http::{Body, Request, Response};
pub use logs::{LogRing, RingLayer};
pub use runtime::{Probe, Runtime, TrayControl};
pub use server::{ServeError, Server};
