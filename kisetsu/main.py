import argparse
import os
import shutil
import tempfile
import uvicorn

SANDBOX_DEFAULT_PORT = 8086


def main():
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
    args = parser.parse_args()
    port = args.port if args.port is not None else args.port_arg
    if port is None:
        port = SANDBOX_DEFAULT_PORT if args.sandbox else 8085

    sandbox_dir = None
    if args.sandbox:
        # The app module reads these when it is imported by uvicorn below.
        sandbox_dir = tempfile.mkdtemp(prefix="kisetsu-sandbox-")
        os.environ["KISETSU_DB_PATH"] = os.path.join(sandbox_dir, "anime.db")
        os.environ["KISETSU_SANDBOX"] = "1"
        print(f"Sandbox: throwaway database in {sandbox_dir}; your real settings and qBittorrent are not touched.")

    print(f"Starting Kisetsu on http://{args.host}:{port}")
    try:
        uvicorn.run(
            "kisetsu.server.app:app",
            host=args.host,
            port=port,
            log_level="info",
            access_log=False,
        )
    finally:
        if sandbox_dir:
            shutil.rmtree(sandbox_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
