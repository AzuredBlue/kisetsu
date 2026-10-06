import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(prog="kisetsu")
    parser.add_argument("port_arg", nargs="?", type=int, metavar="PORT", help="port to listen on (same as --port)")
    parser.add_argument("-p", "--port", type=int, default=None, help="port to listen on (default 8085)")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (default 127.0.0.1; use 0.0.0.0 to expose the UI to the LAN)",
    )
    args = parser.parse_args()
    port = args.port if args.port is not None else (args.port_arg if args.port_arg is not None else 8085)

    print(f"Starting Kisetsu on http://{args.host}:{port}")
    uvicorn.run(
        "kisetsu.server.app:app",
        host=args.host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
