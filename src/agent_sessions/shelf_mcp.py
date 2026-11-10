"""The BattleLab shelf tool (#1740 P1a): ``battlelab_shelf`` for an agent working on a mission.

A minimal stdio MCP server (``python -I -m agent_sessions.shelf_mcp``), the same shape as
`hq_report_mcp`, started by the agent CLI as its own child (`agent_tools`). One tool,
``shelf_add``: put a result — a file, a link or a piece of text — on the mission's shelf for the
operator to review.

Its only inputs are its own environment: ``BATTLELAB_SHELF_SOCKET`` and the PATH of this launch's
token file (``BATTLELAB_SHELF_TOKEN_FILE``). It takes **no mission or session argument**: the broker
(`shelf_broker`) maps the token to the launch. A relative path is resolved against this process's
own working directory (the agent's workspace) and sent absolute; every rule — the project
folders, roots, exclusions, symlinks, sizes, sniffing, credentials — is the server's
(`mission_shelf`), and a refusal comes back as one of `MEANING`. It never opens the file and makes
no network request.

Import-light on purpose (the agent starts it): only `memory_mcp`'s JSON-RPC helpers.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from .memory_mcp import PROTOCOL_VERSIONS, ToolError, _error, _result, _text, serve

ENV_SOCKET = "BATTLELAB_SHELF_SOCKET"
ENV_TOKEN_FILE = "BATTLELAB_SHELF_TOKEN_FILE"  # noqa: S105 — a variable NAME
TOKEN_MAX = 256
SERVER_NAME = "battlelab-shelf"
SERVER_VERSION = "1"
REPLY_MAX = 64 * 1024
#: The broker's request cap (`shelf_broker.REQUEST_MAX`).
REQUEST_MAX = 512 * 1024
TIMEOUT_S = 60.0  # a large file is hashed before the broker answers

#: What a refusal code means, in words an agent can act on. Nothing else a refusal ever says.
#: `mission_shelf.REFUSALS` IS this table (one source of truth, kept import-light here).
MEANING: dict[str, str] = {
    "bad_request": (
        "that item is not valid: give a title, a one-line why, and exactly one of path, url or "
        "text; kind (if given) is one of image, video, audio, document, link, text"
    ),
    "title_required": "every shelf item needs a short title",
    "kind_mismatch": (
        "the kind you gave does not match the item (a path's kind follows its file type)"
    ),
    "too_long": "too long: title is at most 120 characters, why 300, text 64 KB",
    "not_found": "no such file",
    "not_a_file": "that path is not a regular file",
    "symlink": "that path is a symbolic link; shelve the file it points to, inside the project",
    "outside_project": (
        "that file is outside this mission's project folders (or under an excluded folder); "
        "save it inside the project and shelve that"
    ),
    "too_large": "that file is too large for the shelf (1 GB at most)",
    "content_mismatch": "that file's contents do not match its extension; nothing was shelved",
    "url_invalid": "a link must be an http or https URL with a host",
    "url_credentials": "refused: that URL carries credentials; shelve it without them",
    "credential": (
        "refused: that contains a secret or looks like a credential, so nothing was shelved; "
        "describe it without the value"
    ),
    "shelf_full": "this mission's shelf is full (2,000 items)",
    "not_in_mission": "this session no longer belongs to a mission, so it cannot shelve items",
    "unknown_capability": "this session cannot shelve items; it was not started with the tool",
    "store_unavailable": "BattleLab cannot record shelf items right now; try again later",
    "unavailable": "the BattleLab shelf service is not reachable; try again later",
}
INSTRUCTIONS = (
    "BattleLab shelf: this session works on a mission. Put every result the operator should see "
    "on the mission's shelf with shelf_add: files you produced (images, video, audio, documents), "
    "links (a pull request, a deploy, a page) and text (an answer, a report, a finding). Give each "
    "a short title and one line saying why it matters. Files must be inside the project. The "
    "operator reviews the shelf; nothing you shelve is acted on."
)
TOOLS = [
    {
        "name": "shelf_add",
        "description": (
            "Put one result on this mission's shelf for the operator to review: exactly one of "
            "path (a file inside the project; relative paths are from your working directory), "
            "url (an http/https link) or text (up to 64 KB). Always give a short title and a "
            "one-line why. Use it for everything you produced that the operator should see."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "why": {"type": "string", "maxLength": 300},
                "path": {"type": "string"},
                "url": {"type": "string"},
                "text": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["image", "video", "audio", "document", "link", "text"],
                },
            },
            "required": ["title", "why"],
            "additionalProperties": False,
        },
    }
]


class Unavailable(Exception):
    pass


def _read_token(token_file: str) -> str:
    """This launch's token, read from its private file (never a link), or ``""`` when absent."""
    if not token_file.startswith("/"):
        return ""
    try:
        fd = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError:
        raise Unavailable from None
    try:
        raw = os.read(fd, TOKEN_MAX + 1)
    except OSError:
        raise Unavailable from None
    finally:
        os.close(fd)
    if len(raw) > TOKEN_MAX:
        return ""
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""


def absolute(args: dict, cwd: str | None = None) -> dict:
    """The arguments with a relative ``path`` made absolute against the workspace (no realpath:
    the server resolves and judges it)."""
    out = dict(args)
    p = out.get("path")
    if isinstance(p, str) and p and not os.path.isabs(p):
        out["path"] = os.path.normpath(os.path.join(cwd or os.getcwd(), p))
    return out


def request(item: dict, *, env: dict | None = None) -> dict:
    """One ``add`` to the broker over its socket; the broker's reply object."""
    env = os.environ if env is None else env
    path, token = env.get(ENV_SOCKET, ""), _read_token(env.get(ENV_TOKEN_FILE, ""))
    if not path.startswith("/") or not token:
        return {"ok": False, "error": "unknown_capability"}
    payload = {"token": token, "op": "add", "item": item}
    try:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError):
        return {"ok": False, "error": "bad_request"}
    if len(line) > REQUEST_MAX:
        return {"ok": False, "error": "too_long"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.connect(path)
            sock.sendall(line)
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


class Server:
    def shelf_add(self, args: object) -> str:
        if not isinstance(args, dict):
            raise ToolError(MEANING["bad_request"])
        try:
            reply = request(absolute(args))
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            code = reply.get("error")
            raise ToolError(
                MEANING.get(code if isinstance(code, str) else "", MEANING["unavailable"])
            )
        kind = reply.get("kind") if isinstance(reply.get("kind"), str) else "item"
        return f"On the shelf ({kind}). The operator will review it."

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
        if name != "shelf_add":
            return _text("unknown tool", error=True)
        try:
            return _text(self.shelf_add(args))
        except ToolError as exc:
            return _text(str(exc), error=True)
        except (UnicodeError, ValueError, OSError):
            return _text(MEANING["unavailable"], error=True)


def main() -> int:
    """The MCP server (stdio)."""
    if not os.environ.get(ENV_SOCKET, "").startswith("/") or not os.environ.get(ENV_TOKEN_FILE):
        sys.stderr.write(
            f"battlelab shelf tool: {ENV_SOCKET} and {ENV_TOKEN_FILE} must be set; BattleLab sets "
            "them when it starts a mission's session\n"
        )
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Server())
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
