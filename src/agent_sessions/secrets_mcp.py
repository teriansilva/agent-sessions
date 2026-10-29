"""The BattleLab secrets tool (#1786 P2): one client of the app's secrets broker, two front ends.

* **MCP** (``python -I -m agent_sessions.secrets_mcp``): a minimal stdio server the agent CLI
  starts as its own child, beside the memory tool (`agent_tools`): ``secret_list`` and
  ``secret_path``.
* **Helper** (the ``battlelab-secrets`` console script, `$BATTLELAB_SECRETS_HELPER`): the same
  client for an agent without the MCP channel — its own shell runs ``[helper, "list"]`` or
  ``[helper, "path", NAME]``.

Its only inputs are its own environment: ``BATTLELAB_SECRETS_SOCKET`` and
``BATTLELAB_SECRETS_TOKEN``, the capability BattleLab minted for this launch. Every policy decision
is the broker's (`secrets_broker`): which secrets this launch's project may request, whether access
is on, the audit row. **No tool returns a secret's value**: ``secret_path`` returns the path of a
private copy (0600, RAM-backed) to pass to a command — ``--api-key-path``, ``KUBECONFIG=``,
``curl --cert`` — so the value stays out of the conversation unless the agent reads the file.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from .memory_mcp import PROTOCOL_VERSIONS, ToolError, _error, _result, _text, one_line, serve

ENV_SOCKET = "BATTLELAB_SECRETS_SOCKET"
ENV_TOKEN_FILE = "BATTLELAB_SECRETS_TOKEN_FILE"  # noqa: S105 — a variable NAME
TOKEN_MAX = 256
SERVER_NAME = "battlelab-secrets"
SERVER_VERSION = "1"
REPLY_MAX = 64 * 1024
TIMEOUT_S = 15.0

#: What a refusal code means, in words an agent can act on. Nothing else a refusal ever says.
MEANING = {
    "unknown_capability": "this session has no access to BattleLab secrets; restart it",
    "launch_ended": "this session's access ended; restart the session",
    "no_project": "this session belongs to no project, so no secrets are offered to it",
    "project_changed": "this session moved to another project; restart it",
    "not_offered": "no secret of that name is offered to this project",
    "access_off": "the operator has not given agents access to that secret",
    "not_bound": "that secret is not bound to this project",
    "needs_reentry": "that secret must be re-entered by the operator before it can be used",
    "store_unavailable": "BattleLab cannot read its secrets right now; try again later",
    "audit_unavailable": "BattleLab cannot record the request, so it was refused; try again later",
    "storage_unavailable": "BattleLab has no private RAM-backed place for copies; ask the operator",
    "bad_request": "that request is not valid",
    "unavailable": "the BattleLab secrets service is not reachable; try again later",
    "launch_unconfirmed": "BattleLab could not confirm this session is running; try again",
}
INSTRUCTIONS = (
    "BattleLab secrets: credentials, keys, certificates and secret files the operator gave this "
    "project's agents access to. Call secret_list to see what is offered (names and descriptions "
    "only), then secret_path with a name to get the absolute path of a private copy, and pass "
    "that path to the command that needs it (for example --api-key-path, KUBECONFIG=, "
    "curl --cert). Never print, echo or paste a secret's contents, and never copy it elsewhere: "
    "the operator can see every request and can revoke access at any time."
)
TOOLS = [
    {
        "name": "secret_list",
        "description": (
            "List the secrets the operator has made available to this project's agents: name, "
            "kind, description, and for a file its name and size. Never values."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "secret_path",
        "description": (
            "Get the absolute path of a private copy of one secret, by the name secret_list "
            "shows. Pass the path to the command that needs it; do not print the file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 32}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
]


class Unavailable(Exception):
    pass


def request(op: str, name: str | None = None, *, env: dict | None = None) -> dict:
    """One request to the broker over its socket; the broker's reply object."""
    env = os.environ if env is None else env
    path, token = env.get(ENV_SOCKET, ""), _read_token(env.get(ENV_TOKEN_FILE, ""))
    if not path.startswith("/") or not token:
        return {"ok": False, "error": "unknown_capability"}
    payload = {"token": token, "op": op}
    if name is not None:
        payload["name"] = name
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.connect(path)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) <= REPLY_MAX:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError:
        raise Unavailable from None
    try:
        reply = json.loads(data)
    except ValueError:
        raise Unavailable from None
    if not isinstance(reply, dict):
        raise Unavailable
    return reply


def _read_token(token_file: str) -> str:
    """This launch's token, read from its private file (never a link), or ``""``."""
    if not token_file.startswith("/"):
        return ""
    try:
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        raw = os.read(fd, TOKEN_MAX + 1)
    finally:
        os.close(fd)
    if len(raw) > TOKEN_MAX:
        return ""
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""


def _refusal(reply: dict) -> str:
    code = reply.get("error")
    return MEANING.get(code if isinstance(code, str) else "", MEANING["unavailable"])


def _listing(reply: dict) -> str:
    rows = reply.get("secrets") if isinstance(reply.get("secrets"), list) else []
    if not rows:
        return "No secrets are offered to this project's agents."
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = one_line(str(row.get("name", "")), 32)
        kind = "file" if row.get("kind") == "secret_file" else "secret"
        extra = ""
        if kind == "file":
            extra = f" ({one_line(str(row.get('file_name', '')), 120)}, {row.get('size')} bytes)"
        desc = one_line(str(row.get("description", "")), 200)
        lines.append(f"- {name} [{kind}]{extra}" + (f": {desc}" if desc else ""))
    return "\n".join(lines)


class Server:
    def secret_list(self, args: object) -> str:
        if args not in (None, {}):
            raise ToolError("secret_list takes no arguments")
        reply = self._ask("list")
        return _listing(reply)

    def secret_path(self, args: object) -> str:
        if not isinstance(args, dict) or set(args) != {"name"} or not isinstance(args["name"], str):
            raise ToolError("secret_path takes the name of one secret")
        reply = self._ask("path", args["name"])
        path = reply.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ToolError(MEANING["unavailable"])
        return path

    def _ask(self, op: str, name: str | None = None) -> dict:
        try:
            reply = request(op, name)
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            raise ToolError(_refusal(reply))
        return reply

    def handle(self, msg: object) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        method, rid = msg.get("method"), msg.get("id")
        is_request = "id" in msg
        if is_request and (isinstance(rid, bool) or not isinstance(rid, str | int)):
            return _error(None, -32600, "invalid request id")
        if not isinstance(method, str):
            return None if not is_request else _error(rid, -32600, "invalid request")
        if not is_request:
            return None
        params = msg.get("params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(rid, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                rid,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "tools/call":
            return _result(rid, self.call(params.get("name"), params.get("arguments")))
        return _error(rid, -32601, "method not found")

    def call(self, name: object, args: object) -> dict:
        tool = {"secret_list": self.secret_list, "secret_path": self.secret_path}.get(
            name if isinstance(name, str) else ""
        )
        if tool is None:
            return _text("unknown tool", error=True)
        try:
            return _text(tool(args))
        except ToolError as exc:
            return _text(str(exc), error=True)
        except (UnicodeError, ValueError, OSError):
            return _text(MEANING["unavailable"], error=True)


def main() -> int:
    """The MCP server (stdio)."""
    if not os.environ.get(ENV_SOCKET, "").startswith("/") or not os.environ.get(ENV_TOKEN_FILE):
        sys.stderr.write(
            f"battlelab secrets tool: {ENV_SOCKET} and {ENV_TOKEN_FILE} must be set; BattleLab "
            "sets them when it starts a session in a project\n"
        )
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Server())
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


USAGE = "usage: battlelab-secrets list | battlelab-secrets path NAME\n"


def cli(argv: list[str] | None = None) -> int:
    """The helper: ``list`` prints what is offered, ``path NAME`` prints the copy's path. Exit 0
    on success, 1 on a refusal (its meaning on stderr), 2 on a usage error."""
    args = sys.argv[1:] if argv is None else argv
    if args == ["list"]:
        op, name = "list", None
    elif len(args) == 2 and args[0] == "path":
        op, name = "path", args[1]
    else:
        sys.stderr.write(USAGE)
        return 2
    try:
        reply = request(op, name)
    except Unavailable:
        sys.stderr.write(MEANING["unavailable"] + "\n")
        return 1
    if reply.get("ok") is not True:
        sys.stderr.write(_refusal(reply) + "\n")
        return 1
    if op == "list":
        sys.stdout.write(_listing(reply) + "\n")
    else:
        sys.stdout.write(str(reply.get("path")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
