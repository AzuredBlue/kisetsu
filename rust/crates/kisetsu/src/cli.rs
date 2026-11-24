//! The command line: which port and address to serve on, and whether to run a sandbox.

use clap::{Arg, ArgAction, Command};

/// The port a normal run listens on.
pub const DEFAULT_PORT: u16 = 8085;
/// The port a sandbox listens on, so it never meets a normal run.
pub const SANDBOX_PORT: u16 = 8086;

/// What the command line asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub host: String,
    pub port: u16,
    pub sandbox: bool,
    pub no_tray: bool,
}

impl Args {
    /// Whether this run can show an icon: only on Windows and Linux, and not when declined.
    pub fn tray_available(&self) -> bool {
        cfg!(any(windows, target_os = "linux")) && !self.no_tray
    }
}

/// What to do, once the command line is read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    Run(Args),
    /// A command that is not available in this build.
    Unsupported(&'static str),
}

fn command() -> Command {
    Command::new("kisetsu")
        .arg(
            Arg::new("port_arg")
                .value_name("PORT")
                .value_parser(clap::value_parser!(u16))
                .help("port to listen on (same as --port)"),
        )
        .arg(
            Arg::new("port")
                .short('p')
                .long("port")
                .value_parser(clap::value_parser!(u16))
                .help("port to listen on (default 8085, or 8086 with --sandbox)"),
        )
        .arg(
            Arg::new("host")
                .long("host")
                .default_value("127.0.0.1")
                .help("address to bind (default 127.0.0.1; use 0.0.0.0 to expose the UI to the LAN)"),
        )
        .arg(
            Arg::new("sandbox")
                .long("sandbox")
                .action(ArgAction::SetTrue)
                .help(
                    "run a throwaway instance with a temporary database: setup, shows and downloads \
                     all work, but downloads are only pretended and logged",
                ),
        )
        .arg(
            Arg::new("no_tray")
                .long("no-tray")
                .action(ArgAction::SetTrue)
                .help("do not show the notification-area icon (Windows)"),
        )
}

/// Reads the command line, without the program name.
pub fn parse<I, T>(argv: I) -> Result<Action, clap::Error>
where
    I: IntoIterator<Item = T>,
    T: Into<std::ffi::OsString> + Clone,
{
    let argv: Vec<std::ffi::OsString> = argv.into_iter().map(Into::into).collect();
    if argv.first().is_some_and(|first| first == "import-legacy") {
        return Ok(Action::Unsupported("import-legacy"));
    }
    if argv.iter().any(|arg| arg == "--adopt-report") {
        return Ok(Action::Unsupported("--adopt-report"));
    }
    let matches = command()
        .try_get_matches_from(std::iter::once(std::ffi::OsString::from("kisetsu")).chain(argv))?;
    let sandbox = matches.get_flag("sandbox");
    let port = matches
        .get_one::<u16>("port")
        .or_else(|| matches.get_one::<u16>("port_arg"))
        .copied()
        .unwrap_or(if sandbox { SANDBOX_PORT } else { DEFAULT_PORT });
    let host = matches
        .get_one::<String>("host")
        .cloned()
        .unwrap_or_else(|| "127.0.0.1".to_owned());
    Ok(Action::Run(Args {
        host,
        port,
        sandbox,
        no_tray: matches.get_flag("no_tray"),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(argv: &[&str]) -> Args {
        match parse(argv.iter().copied()).unwrap() {
            Action::Run(args) => args,
            other => panic!("expected a run, got {other:?}"),
        }
    }

    #[test]
    fn the_port_comes_from_the_flag_then_the_argument_then_the_mode() {
        assert_eq!(run(&[]).port, 8085);
        assert_eq!(run(&["--sandbox"]).port, 8086);
        assert_eq!(run(&["9000"]).port, 9000);
        assert_eq!(run(&["-p", "9001", "9000"]).port, 9001);
        assert_eq!(run(&["--sandbox", "--port", "8090"]).port, 8090);
    }

    #[test]
    fn the_host_is_local_unless_asked_otherwise() {
        assert_eq!(run(&[]).host, "127.0.0.1");
        assert_eq!(run(&["--host", "0.0.0.0"]).host, "0.0.0.0");
    }

    #[test]
    fn the_tray_is_shown_unless_declined() {
        assert!(!run(&[]).no_tray);
        assert!(run(&["--no-tray"]).no_tray);
    }

    #[test]
    fn the_flag_takes_the_icon_away() {
        assert!(!run(&["--no-tray"]).tray_available());
        assert_eq!(
            run(&[]).tray_available(),
            cfg!(any(windows, target_os = "linux"))
        );
    }

    #[test]
    fn commands_that_are_not_available_yet_are_named() {
        assert_eq!(
            parse(["import-legacy", "x.db"]).unwrap(),
            Action::Unsupported("import-legacy")
        );
        assert_eq!(
            parse(["--adopt-report"]).unwrap(),
            Action::Unsupported("--adopt-report")
        );
    }

    #[test]
    fn a_port_that_is_no_port_is_refused() {
        assert!(parse(["--port", "70000"]).is_err());
        assert!(parse(["--nonsense"]).is_err());
    }
}
