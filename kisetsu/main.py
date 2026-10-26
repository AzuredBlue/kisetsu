import argparse
import logging
import os
import shutil
import sys
import tempfile

SANDBOX_DEFAULT_PORT = 8086
# A browser tab left open must not hold a restart up.
GRACEFUL_SHUTDOWN_SECONDS = 5


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kisetsu")
    parser.add_argument("port_arg", nargs="?", type=int, metavar="PORT", help="port to listen on (same as --port)")
    parser.add_argument("-p", "--port", type=int, default=None, help="port to listen on (default 8085, or 8086 with --sandbox)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (default 127.0.0.1; use 0.0.0.0 to expose the UI to the LAN)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="run a throwaway instance (temporary database, no background work) to try the first-run setup",
    )
    return parser.parse_args(argv)


def log_to_stderr() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("kisetsu")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "import-legacy":
        from kisetsu import legacy_import

        legacy_import.run(argv[1] if len(argv) > 1 else None)
        return
    if "--adopt-report" in argv:
        from kisetsu import adopt_report

        argv.remove("--adopt-report")
        raise SystemExit(adopt_report.run(argv[0] if argv else None))
    args = parse_args(argv)
    port = args.port if args.port is not None else args.port_arg
    if port is None:
        port = SANDBOX_DEFAULT_PORT if args.sandbox else 8085

    sandbox_dir = None
    if args.sandbox:
        # The config module reads these when the app is imported below.
        sandbox_dir = tempfile.mkdtemp(prefix="kisetsu-sandbox-")
        os.environ["KISETSU_DB_PATH"] = os.path.join(sandbox_dir, "kisetsu.db")
        os.environ["KISETSU_SANDBOX"] = "1"
        print(f"Sandbox: throwaway database in {sandbox_dir}; your real settings and qBittorrent are not touched.")

    import uvicorn

    from kisetsu.web import app as web_app

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            for hook in list(web_app.shutdown_hooks):
                hook()
            super().handle_exit(sig, frame)

    log_to_stderr()
    print(f"Starting Kisetsu on http://{args.host}:{port}")
    config = uvicorn.Config(
        web_app.create_app,
        factory=True,
        host=args.host,
        port=port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    try:
        Server(config).run()
    finally:
        if sandbox_dir:
            shutil.rmtree(sandbox_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
