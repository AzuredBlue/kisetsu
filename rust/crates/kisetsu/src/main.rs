//! Kisetsu: reads the user's AniList watching list and makes qBittorrent download each new episode.

#![forbid(unsafe_code)]
#![cfg_attr(test, allow(clippy::unwrap_used, clippy::expect_used, clippy::panic))]

mod app;
mod cli;
mod config;

use std::process::ExitCode;

use tracing::Level;

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_max_level(Level::INFO)
        .with_writer(std::io::stderr)
        .init();
    let args = match cli::parse(std::env::args_os().skip(1)) {
        Ok(cli::Action::Run(args)) => args,
        Ok(cli::Action::Unsupported(name)) => {
            eprintln!("`{name}` is not supported yet.");
            return ExitCode::from(2);
        }
        Err(error) => error.exit(),
    };
    match app::run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
