//! Kisetsu: reads the user's AniList watching list and makes qBittorrent download each new episode.

#![cfg_attr(windows, windows_subsystem = "windows")]
#![deny(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod app;
mod cli;
mod config;
mod console;
mod logfile;
mod tray;
mod update;

use std::process::ExitCode;
use std::sync::{Arc, Mutex};

use kisetsu_web::{LogRing, RingLayer};
use tracing::{Level, error};
use tracing_subscriber::Layer;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

fn main() -> ExitCode {
    console::attach_to_parent();
    let args = match cli::parse(std::env::args_os().skip(1)) {
        Ok(cli::Action::Run(args)) => args,
        Ok(cli::Action::Unsupported(name)) => {
            eprintln!("`{name}` is not supported yet.");
            return ExitCode::from(2);
        }
        Err(error) => error.exit(),
    };
    // A sandbox started with the flag throws its database away, and keeps no log either.
    let log_file = (!args.sandbox)
        .then(config::database_path)
        .flatten()
        .and_then(|database| logfile::path_beside(&database))
        .and_then(|path| logfile::open(&path).ok());
    let logs = Arc::new(LogRing::new());
    // The D-Bus library behind the Linux tray and notifications warns about its own connection
    // closing when the app quits; only its errors are worth logging.
    let info = || {
        tracing_subscriber::filter::Targets::new()
            .with_default(Level::INFO)
            .with_target("zbus", Level::ERROR)
    };
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(std::io::stderr)
                .with_filter(info()),
        )
        .with(log_file.map(|file| {
            tracing_subscriber::fmt::layer()
                .with_ansi(false)
                .with_writer(Mutex::new(file))
                .with_filter(info())
        }))
        .with(RingLayer(Arc::clone(&logs)))
        .init();
    match app::run(&args, logs) {
        Ok(None) => ExitCode::SUCCESS,
        Ok(Some(staged)) => match update::restart_into(&staged) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                error!("Could not start the new version: {error}");
                ExitCode::FAILURE
            }
        },
        Err(error) => {
            error!("{error}");
            ExitCode::FAILURE
        }
    }
}
