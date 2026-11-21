//! Kisetsu: reads the user's AniList watching list and makes qBittorrent download each new episode.

#![deny(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod app;
mod cli;
mod config;
mod tray;

use std::process::ExitCode;
use std::sync::Arc;

use kisetsu_web::{LogRing, RingLayer};
use tracing::Level;
use tracing_subscriber::Layer;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

fn main() -> ExitCode {
    let logs = Arc::new(LogRing::new());
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(std::io::stderr)
                .with_filter(tracing_subscriber::filter::LevelFilter::from_level(
                    Level::INFO,
                )),
        )
        .with(RingLayer(Arc::clone(&logs)))
        .init();
    let args = match cli::parse(std::env::args_os().skip(1)) {
        Ok(cli::Action::Run(args)) => args,
        Ok(cli::Action::Unsupported(name)) => {
            eprintln!("`{name}` is not supported yet.");
            return ExitCode::from(2);
        }
        Err(error) => error.exit(),
    };
    match app::run(&args, logs) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
