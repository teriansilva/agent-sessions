"""The only way out for a package tool reviewing or installing an MCP server (#2163 P1b).

npm's ``--registry`` picks the DEFAULT registry; it does not stop a published package's metadata
from naming a dependency by URL (``http://…/x.tgz``), and npm fetches that URL while resolving,
before any lock could be checked. uv and git likewise follow what metadata says. So every tool
run gets this proxy as its only egress (``HTTPS_PROXY`` / npm's ``proxy`` / git's ``https`` only):
it tunnels to the fixed registry hosts and refuses every other host BEFORE resolving or
connecting to it. A redirect to another host is a new request through the proxy, refused the
same way. Rejecting the final lock stays as the second guard.

Threaded, loopback-only, one instance per tool run.
"""

from __future__ import annotations

import contextlib
import logging
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

HEAD_MAX = 16 * 1024
CONNECT_TIMEOUT_S = 30
IDLE_TIMEOUT_S = 300


def origin(url: str) -> tuple[str, int]:
    """``(host, port)`` of a URL, with the scheme's default port."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(CONNECT_TIMEOUT_S)
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
                if len(head) > HEAD_MAX:
                    return self._refuse(client, "?", 431)
        except OSError:
            return
        header, _, rest = head.partition(b"\r\n\r\n")
        lines = header.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return self._refuse(client, "?", 400)
        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host = host.strip("[]").lower()
            try:
                port = int(port_s)
            except ValueError:
                return self._refuse(client, target, 400)
            if (host, port) not in self.server.allowed:
                return self._refuse(client, f"{host}:{port}", 403)
            upstream = self._connect(client, host, port)
            if upstream is None:
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            return self._pipe(client, upstream)
        # An absolute-form request (plain HTTP through the proxy).
        host, port = origin(target)
        if not target.lower().startswith("http://") or (host, port) not in self.server.allowed:
            return self._refuse(client, f"{host}:{port}", 403)
        parts = urlsplit(target)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        kept = [
            line
            for line in lines[1:]
            if not line.lower().startswith(("proxy-", "connection:", "keep-alive:"))
        ]
        request = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
        upstream = self._connect(client, host, port)
        if upstream is None:
            return
        upstream.sendall(request.encode("latin-1") + rest)
        self._pipe(client, upstream)

    def _refuse(self, client: socket.socket, what: str, code: int) -> None:
        with self.server.lock:
            self.server.refused.append(what)
        log.info("MCP install egress refused: %s", what)
        with contextlib.suppress(OSError):
            client.sendall(
                f"HTTP/1.1 {code} Refused by BattleLab\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".encode()
            )

    def _connect(self, client: socket.socket, host: str, port: int) -> socket.socket | None:
        try:
            return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
        except OSError:
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return None

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        try:
            socks = [a, b]
            while True:
                ready, _, _ = select.select(socks, [], [], IDLE_TIMEOUT_S)
                if not ready:
                    return
                for s in ready:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                b.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, allowed: set[tuple[str, int]]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.allowed = {(h.lower(), p) for h, p in allowed}
        self.refused: list[str] = []
        self.lock = threading.Lock()


class EgressProxy:
    """``with EgressProxy({("registry.npmjs.org", 443)}) as p: … p.url …``"""

    def __init__(self, allowed: set[tuple[str, int]]) -> None:
        self._server = _Server(allowed)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def refused(self) -> list[str]:
        with self._server.lock:
            return list(self._server.refused)

    def __enter__(self) -> EgressProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
