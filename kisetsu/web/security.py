"""Who may talk to the server: only pages served from this machine's own names."""

import ipaddress
import socket
from typing import Callable, Optional, Set
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# The only writes a sandbox allows: the wizard's own calls, which touch the throwaway database
# or only read from qBittorrent.
SANDBOX_WRITES = {
    "/api/setup/save",
    "/api/setup/complete",
    "/api/setup/reset",
    "/api/settings/test-qbit",
    "/api/feeds/sync",
    "/api/feeds/reorder",
}


def request_host(host_header: str) -> str:
    """The hostname of a Host header, without port or IPv6 brackets."""
    return urlsplit(f"//{host_header}").hostname or ""


def host_allowed(host_header: str, extra: Set[str], own_name: Callable[[], str] = socket.gethostname) -> bool:
    """Whether the Host header names this machine rather than a rebound domain.

    DNS rebinding points an attacker's domain at the loopback address, so the browser sends
    that domain as Host. IP literals, localhost and this machine's own hostname are accepted;
    ``extra`` adds names for reverse proxies or a LAN hostname.
    """
    try:
        host = request_host(host_header).lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host in extra:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        return host == own_name().lower()
    except OSError:
        return False


class Guard:
    """Pure ASGI middleware, so streamed responses pass through untouched."""

    def __init__(self, app, *, sandbox: bool, extra_hosts: Set[str]):
        self.app = app
        self.sandbox = sandbox
        self.extra_hosts = extra_hosts

    def refusal(self, scope, headers) -> Optional[str]:
        host_header = headers.get("host", "")
        if not host_allowed(host_header, self.extra_hosts):
            return "Host not allowed."
        method = scope["method"]
        origin = headers.get("origin")
        if origin and method not in SAFE_METHODS and urlsplit(origin).netloc != host_header:
            return "Cross-origin requests are not allowed."
        if self.sandbox and method not in SAFE_METHODS and scope["path"] not in SANDBOX_WRITES:
            return "Not available in the sandbox."
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        reason = self.refusal(scope, headers)
        if reason is not None:
            await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install(app: FastAPI, *, sandbox: bool, extra_hosts: Set[str]) -> None:
    app.add_middleware(Guard, sandbox=sandbox, extra_hosts=extra_hosts)
