"""The launcher every project MCP server starts through (#2163): ``python -I -m
agent_sessions.mcp_launch <server-id>``.

An agent CLI starts this as one of its MCP servers. Its argv and env hold only the server id and
the launch's secrets capability PATHS (`agent_tools.project_server`) — never a setting value. It
asks the secrets broker (op ``mcp``) for the server's resolved settings, which arrive as a private
0600 file on RAM-backed storage; it reads and removes that file, then:

- **stdio** (an installed package): ``exec``s the server with the resolved environment added to a
  whitelisted base (``PATH``, ``HOME``, ``LANG``). The capability paths are NOT passed on: the
  third-party process never holds this launch's broker token.
- **http** (a remote server): relays newline-delimited JSON-RPC on stdio to the server's
  streamable-HTTP endpoint, adding the configured headers. TLS verification is always on, a
  redirect is refused (the URL is fixed at registration), and nothing the server sends back is
  logged here.

A refusal exits 1 with one line on stderr the CLI shows (*"nextcloud: credentials unavailable for
this project"*); the agent's launch itself is never failed for it. Shell-free: literal argv only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import threading

ID_RE = re.compile(r"^[0-9a-f]{16}$")
SERVER_ENV = "BATTLELAB_MCP_SERVER"
PROTOCOL_VERSION = "2025-06-18"
#: The largest JSON-RPC message relayed either way (a tool result can be large; never unbounded).
MESSAGE_MAX = 16 * 1024 * 1024
SETTINGS_MAX = 256 * 1024

#: Why a server did not start, in words (the broker's codes never carry more).
REASONS = {
    "credentials_missing": "credentials unavailable for this project",
    "check_required": "it needs a passing check for this project's settings",
    "not_enabled": "it is not enabled for this project",
    "server_disabled": "it is disabled in the Library",
    "server_unknown": "it was removed from the Library",
    "server_changed": "it changed while it was being checked",
    "not_installed": "it is not installed",
    "no_project": "this session has no project",
    "project_changed": "this session moved to another project",
    "launch_ended": "this session's launch ended",
}


class Refused(Exception):
    pass


def _fail(name: str, reason: str) -> int:
    sys.stderr.write(f"{name}: {reason}\n")
    sys.stderr.flush()
    return 1


def fetch_settings(server_id: str, env: dict[str, str]) -> dict:
    """The resolved settings for ``server_id`` from the broker; the private copy is removed once
    read. Raises `Refused` with words."""
    from . import secrets_mcp

    try:
        reply = secrets_mcp.request("mcp", server_id, env=env)
    except secrets_mcp.Unavailable:
        raise Refused("BattleLab's secrets broker is not answering") from None
    if not reply.get("ok"):
        code = str(reply.get("error") or "")
        raise Refused(REASONS.get(code, f"refused ({code or 'unknown'})"))
    path = reply.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise Refused("the broker's reply was malformed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            data = os.read(fd, SETTINGS_MAX + 1)
        finally:
            os.close(fd)
    except OSError:
        raise Refused("the settings copy could not be read") from None
    with contextlib.suppress(OSError):
        os.unlink(path)  # read once; the broker's reconcile accounts for the empty directory
    if len(data) > SETTINGS_MAX:
        raise Refused("the settings copy is too large")
    try:
        settings = json.loads(data)
    except ValueError:
        raise Refused("the settings copy is malformed") from None
    if not isinstance(settings, dict) or settings.get("transport") not in ("stdio", "http"):
        raise Refused("the settings copy is malformed")
    return settings


def _base_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def exec_stdio(settings: dict) -> None:
    command = settings.get("command")
    args = settings.get("args") or []
    extra = settings.get("env") or {}
    if not isinstance(command, str) or not command.startswith("/"):
        raise Refused("it has no installed command")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Refused("the settings copy is malformed")
    if not isinstance(extra, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in extra.items()
    ):
        raise Refused("the settings copy is malformed")
    os.execve(command, [command, *args], {**_base_env(), **extra})  # noqa: S606 - literal argv


# ---- the HTTP relay ----------------------------------------------------------------------------


class Relay:
    """stdio ↔ MCP streamable HTTP for one server. Requests run concurrently (a long tool call
    never blocks a listing); ``initialize`` runs first and alone, because it sets the session."""

    def __init__(self, url: str, headers: dict[str, str], out=None) -> None:
        import httpx

        self.url = url
        self.headers = dict(headers)
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.out = out if out is not None else sys.stdout.buffer
        self._out_lock = threading.Lock()
        self.client = httpx.Client(
            timeout=httpx.Timeout(30.0, read=600.0),
            follow_redirects=False,
            verify=True,
            trust_env=False,
        )

    def write(self, message: object) -> None:
        line = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        with self._out_lock:
            self.out.write(line)
            self.out.flush()

    def _error(self, msg: dict, text: str) -> None:
        if "id" in msg and msg.get("id") is not None:
            self.write(
                {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": text}}
            )

    def _headers(self) -> dict[str, str]:
        h = {
            **self.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _deliver(self, message: object, initialize: bool) -> None:
        for m in message if isinstance(message, list) else [message]:
            if (
                initialize
                and isinstance(m, dict)
                and isinstance(m.get("result"), dict)
                and isinstance(m["result"].get("protocolVersion"), str)
            ):
                self.protocol = m["result"]["protocolVersion"][:32]
            self.write(m)

    def _post(self, msg: dict, body: bytes) -> None:
        import httpx

        initialize = msg.get("method") == "initialize"
        host = httpx.URL(self.url).host
        try:
            with self.client.stream("POST", self.url, content=body, headers=self._headers()) as r:
                if 300 <= r.status_code < 400:
                    self._error(msg, f"{host} answered with a redirect, which is refused")
                    return
                if initialize and r.headers.get("mcp-session-id"):
                    self.session_id = r.headers["mcp-session-id"][:256]
                if r.status_code == 202:
                    return
                if r.status_code >= 400:
                    self._error(msg, f"HTTP {r.status_code} from {host}")
                    return
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype == "text/event-stream":
                    self._sse(r, initialize)
                    return
                data = b""
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MESSAGE_MAX:
                        self._error(msg, f"the reply from {host} is too large")
                        return
                if not data.strip():
                    return
                try:
                    self._deliver(json.loads(data), initialize)
                except ValueError:
                    self._error(msg, f"{host} sent a reply that is not JSON")
        except httpx.HTTPError as e:
            self._error(msg, f"{host} could not be reached ({type(e).__name__})")

    def _sse(self, r, initialize: bool) -> None:
        data: list[str] = []
        size = 0
        for line in r.iter_lines():
            if line == "":
                if data:
                    with contextlib.suppress(ValueError):
                        self._deliver(json.loads("\n".join(data)), initialize)
                data, size = [], 0
                continue
            if line.startswith("data:"):
                piece = line[5:].lstrip(" ")
                size += len(piece)
                if size > MESSAGE_MAX:
                    data, size = [], 0
                    continue
                data.append(piece)
        if data:
            with contextlib.suppress(ValueError):
                self._deliver(json.loads("\n".join(data)), initialize)

    def _end_session(self) -> None:
        if not self.session_id:
            return
        with contextlib.suppress(Exception):
            self.client.delete(self.url, headers=self._headers())

    def handle_line(self, line: bytes, threads: list[threading.Thread]) -> None:
        if not line.strip():
            return
        if len(line) > MESSAGE_MAX:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            self.write(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
            return
        if not isinstance(msg, dict):
            return
        if msg.get("method") == "initialize":
            self._post(msg, line)
            return
        t = threading.Thread(target=self._post, args=(msg, line), daemon=True)
        t.start()
        threads.append(t)

    def run(self, stdin=None) -> int:
        stdin = stdin if stdin is not None else sys.stdin.buffer
        threads: list[threading.Thread] = []
        try:
            for line in stdin:
                self.handle_line(line, threads)
                threads[:] = [t for t in threads if t.is_alive()]
        finally:
            for t in threads:
                t.join(timeout=5)
            self._end_session()
            self.client.close()
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not ID_RE.fullmatch(argv[0]) or os.environ.get(SERVER_ENV) != argv[0]:
        return _fail("mcp-launch", "usage: python -I -m agent_sessions.mcp_launch <server-id>")
    server_id = argv[0]
    try:
        settings = fetch_settings(server_id, dict(os.environ))
    except Refused as e:
        return _fail(f"MCP server {server_id}", str(e))
    name = str(settings.get("name") or server_id)[:32]
    try:
        if settings["transport"] == "stdio":
            exec_stdio(settings)  # does not return
            return 1
        url, headers = settings.get("url"), settings.get("headers") or {}
        if not isinstance(url, str) or not isinstance(headers, dict):
            raise Refused("the settings copy is malformed")
        return Relay(url, {str(k): str(v) for k, v in headers.items()}).run()
    except Refused as e:
        return _fail(name, str(e))
    except OSError as e:
        return _fail(name, f"could not be started ({e.strerror or type(e).__name__})")


if __name__ == "__main__":
    sys.exit(main())
