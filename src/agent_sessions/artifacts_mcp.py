"""The BattleLab artifacts tool (#1932 P1): ``battlelab_artifacts``, for sessions it is on for.

A minimal stdio MCP server (``python -I -m agent_sessions.artifacts_mcp``), the same shape as
`shelf_mcp`, started by the agent CLI as its own child (`agent_tools`). Two tools:

* ``artifact_publish`` — host a file or a directory (a static website) on BattleLab: the server
  COPIES it into its own store and answers with the artifact's stable link;
* ``artifact_list`` — this session's own artifacts and their links.

Its only inputs are its own environment: ``BATTLELAB_ARTIFACTS_SOCKET`` and the PATH of this
launch's token file (``BATTLELAB_ARTIFACTS_TOKEN_FILE``). It takes **no session argument**: the
broker (`artifact_broker`) maps the token to the launch. A relative path is resolved against this
process's own working directory and sent absolute; every rule is the server's (`artifacts`), and a
refusal comes back as one of `MEANING`. It never opens a file and makes no network request.

Import-light on purpose (the agent starts it): only `memory_mcp`'s JSON-RPC helpers.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from .memory_mcp import PROTOCOL_VERSIONS, ToolError, _error, _result, _text, serve

ENV_SOCKET = "BATTLELAB_ARTIFACTS_SOCKET"
ENV_TOKEN_FILE = "BATTLELAB_ARTIFACTS_TOKEN_FILE"  # noqa: S105 — a variable NAME
TOKEN_MAX = 256
SERVER_NAME = "battlelab-artifacts"
SERVER_VERSION = "1"
REPLY_MAX = 256 * 1024
REQUEST_MAX = 16 * 1024
#: A site of many files is copied and judged before the broker answers.
TIMEOUT_S = 300.0

#: What a refusal code means, in words an agent can act on. Nothing else a refusal ever says
#: (beside the offending path, when there is one). `artifact_store.REFUSALS` IS this table.
MEANING: dict[str, str] = {
    "bad_request": (
        "that request is not valid: give path (a file or a directory), a short title and a "
        "one-line why; kind (if given) is file or site; artifact_id (if given) is one of yours"
    ),
    "title_required": "every artifact needs a short title",
    "too_long": "too long: title is at most 120 characters, why 300",
    "kind_mismatch": (
        "the kind does not match the path: a site is a directory, a file is a single file "
        "(and a new revision keeps its artifact's kind)"
    ),
    "not_found": "no such file or directory",
    "not_a_file": "that is not a regular file or directory",
    "symlink": (
        "a symbolic link was found; artifacts never follow links — copy the real file into "
        "the directory and publish again"
    ),
    "bad_name": "a file name in that directory cannot be served (control characters or too long)",
    "outside_project": (
        "that is outside this session's project folders (or under an excluded folder); put it "
        "inside the project and publish that"
    ),
    "hidden": (
        "that is a hidden file or inside a hidden folder (a name starting with a dot); hidden "
        "files are never published"
    ),
    "too_large": "too large for one revision (200 MB unless the operator changed it)",
    "archive": (
        "archives (.zip, .tar, .gz, …) are never published: their contents would skip every "
        "check; publish the folder instead"
    ),
    "too_many_files": "too many files: one site is at most 2,000 files",
    "empty_site": "that directory has no files to publish (hidden files are never published)",
    "text_too_large": (
        "a text file is over 8 MB and cannot be checked for secrets; nothing was published"
    ),
    "credential": (
        "refused: a file is a key or credential file, or contains a secret or something that "
        "looks like a credential, so nothing was published; remove it and publish again"
    ),
    "store_full": "BattleLab's artifact store is full; ask the operator to delete old artifacts",
    "unknown_artifact": "no such artifact",
    "not_yours": "that artifact was published by another session; publish a new one instead",
    "unknown_capability": "this session cannot publish artifacts; it was not started with the tool",
    "not_public": (
        "the operator has not allowed this session to share publicly; give the operator the "
        "private link and ask them to share it"
    ),
    "public_off": "public links are off on this BattleLab (it runs without a login)",
    "store_unavailable": "BattleLab cannot store artifacts right now; try again later",
    "unavailable": "the BattleLab artifacts service is not reachable; try again later",
}
INSTRUCTIONS = (
    "BattleLab artifacts: host what you make so the operator can open it from a link. Use "
    "artifact_publish for a file (an image, a video, a PDF, a report) or a directory holding a "
    "static website (index.html plus its assets, relative links). BattleLab copies it: later "
    "edits change nothing until you publish again; pass artifact_id to publish a new revision "
    "behind the same link. Pages run sandboxed: their HTML, CSS, scripts, images and media load "
    "normally, but a script cannot fetch() the site's files or use localStorage, so inline data a "
    "page needs. Use RELATIVE links (style.css, img/a.png), never root-absolute ones (/style.css): "
    "the site is served under its own prefix. Hidden files and key files are never published, "
    "and a file with a secret in it refuses the whole publish. Artifacts are private to the "
    "operator unless the operator shares them. Give every artifact a short title and one line "
    "saying why."
)
TOOLS = [
    {
        "name": "artifact_publish",
        "description": (
            "Host a file or a static website (a directory with index.html) on BattleLab and get "
            "its link. path is inside the project; relative paths are from your working "
            "directory. Give a short title and a one-line why. Pass artifact_id (from an "
            "earlier publish) to add a new revision behind the same link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "title": {"type": "string", "maxLength": 120},
                "why": {"type": "string", "maxLength": 300},
                "kind": {"type": "string", "enum": ["file", "site"]},
                "artifact_id": {"type": "string"},
            },
            "required": ["path", "title", "why"],
            "additionalProperties": False,
        },
    },
    {
        "name": "artifact_share_public",
        "description": (
            "Make one of your artifacts public: anyone with the returned link can open it, with "
            "no sign-in. Only when the operator asked for it; the link follows the latest "
            "revision until the operator revokes it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "artifact_list",
        "description": "List the artifacts this session has published, with their links.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
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


def request(op: str, args: dict | None = None, *, env: dict | None = None) -> dict:
    """One request to the broker over its socket; the broker's reply object."""
    env = os.environ if env is None else env
    path, token = env.get(ENV_SOCKET, ""), _read_token(env.get(ENV_TOKEN_FILE, ""))
    if not path.startswith("/") or not token:
        return {"ok": False, "error": "unknown_capability"}
    payload = {"token": token, "op": op, "args": args or {}}
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


def _refusal(reply: dict) -> ToolError:
    code = reply.get("error")
    words = MEANING.get(code if isinstance(code, str) else "", MEANING["unavailable"])
    where = reply.get("path")
    if isinstance(where, str) and where and len(where) <= 1024:
        words = f"{words} (at {where})"
    return ToolError(words)


def _line(a: dict) -> str:
    parts = [f"{a.get('title', '')} — {a.get('url', '')}"]
    parts.append(f"id {a.get('id')}, {a.get('kind')}, revision {a.get('revision')}")
    if a.get("public_url"):
        parts.append(f"public: {a['public_url']}")
    else:
        parts.append("private")
    return " · ".join(str(p) for p in parts)


class Server:
    def artifact_publish(self, args: object) -> str:
        if not isinstance(args, dict):
            raise ToolError(MEANING["bad_request"])
        try:
            reply = request("publish", absolute(args))
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            raise _refusal(reply)
        a = reply.get("artifact") if isinstance(reply.get("artifact"), dict) else {}
        text = f"Published. {_line(a)}"
        skipped = reply.get("skipped_hidden")
        if isinstance(skipped, int) and skipped > 0:
            text += f" ({skipped} hidden file(s) or folder(s) were left out.)"
        return text + " Give the operator this link."

    def artifact_share_public(self, args: object) -> str:
        if not isinstance(args, dict):
            raise ToolError(MEANING["bad_request"])
        try:
            reply = request("share", {"artifact_id": args.get("artifact_id")})
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            raise _refusal(reply)
        a = reply.get("artifact") if isinstance(reply.get("artifact"), dict) else {}
        return f"Shared publicly. {_line(a)} Anyone with the public link can open it."

    def artifact_list(self, args: object) -> str:
        try:
            reply = request("list")
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            raise _refusal(reply)
        items = reply.get("items") if isinstance(reply.get("items"), list) else []
        if not items:
            return "This session has published no artifacts yet."
        return "\n".join(_line(a) for a in items if isinstance(a, dict))

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
        fn = {
            "artifact_publish": self.artifact_publish,
            "artifact_share_public": self.artifact_share_public,
            "artifact_list": self.artifact_list,
        }.get(name if isinstance(name, str) else "")
        if fn is None:
            return _text("unknown tool", error=True)
        try:
            return _text(fn(args))
        except ToolError as exc:
            return _text(str(exc), error=True)
        except (UnicodeError, ValueError, OSError):
            return _text(MEANING["unavailable"], error=True)


def main() -> int:
    """The MCP server (stdio)."""
    if not os.environ.get(ENV_SOCKET, "").startswith("/") or not os.environ.get(ENV_TOKEN_FILE):
        sys.stderr.write(
            f"battlelab artifacts tool: {ENV_SOCKET} and {ENV_TOKEN_FILE} must be set; BattleLab "
            "sets them when it starts a session with artifacts on\n"
        )
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Server())
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
