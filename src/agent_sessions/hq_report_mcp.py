"""The BattleLab HQ report tool (#1882, P1a of #1878): ``battlelab_hq`` for a pty agent.

A minimal stdio MCP server (``python -I -m agent_sessions.hq_report_mcp``), the same shape as
`secrets_mcp`, started by the agent CLI as its own child (`agent_tools`). One tool, ``hq_report``.

Its only inputs are its own environment: ``BATTLELAB_HQ_SOCKET`` and the PATH of this launch's
token file (``BATTLELAB_HQ_TOKEN_FILE``). It takes **no session argument**: the broker
(`hq_report_broker`) maps the token to the launch, so through it an agent reports only as itself
(not a sandbox against a same-user process reading token files: `hq-reports.md`). Every
rule — limits, the credential guard, rate limits, what reaches the Commander — is the server's
(`agent_reports`). Nothing it files can act: an answer only ever comes from the Commander's tap.
"""

from __future__ import annotations

import json
import os
import socket
import sys

from .memory_mcp import PROTOCOL_VERSIONS, ToolError, _error, _result, _text, serve

ENV_SOCKET = "BATTLELAB_HQ_SOCKET"
ENV_TOKEN_FILE = "BATTLELAB_HQ_TOKEN_FILE"  # noqa: S105 — a variable NAME
TOKEN_MAX = 256
SERVER_NAME = "battlelab-hq"
SERVER_VERSION = "1"
REPLY_MAX = 64 * 1024
#: The broker's request cap (`hq_report_broker.REQUEST_MAX`; this module stays import-light). A
#: request is sent as UTF-8, and one past the cap is refused here as `too_long` — it could only
#: carry a field past its cap — instead of failing as an unreachable service.
REQUEST_MAX = 16384
TIMEOUT_S = 15.0

#: What a refusal code means, in words an agent can act on. Nothing else a refusal ever says.
MEANING = {
    "unknown_capability": "this session cannot report to HQ; it was not started with the tool",
    "launch_ended": "this session's report access ended; it was restarted",
    "launch_unconfirmed": "BattleLab could not confirm this session is running; try again",
    "bad_request": (
        "that report is not valid: state is one of blocked, needs_decision, progress, done; "
        "a non-empty summary; needs_decision requires a question and 1–4 distinct non-empty "
        "options (use blocked otherwise); question and options only with needs_decision"
    ),
    "too_long": "too long: summary and question are at most 280 characters, options 80",
    "rate_limited": "refused: too many reports — wait (one per 30 seconds, 20 an hour)",
    "credential": (
        "refused: that report contains a secret or looks like a credential, so nothing was "
        "sent; describe the situation without it"
    ),
    "store_unavailable": "BattleLab cannot record reports right now; try again later",
    "unavailable": "the BattleLab HQ report service is not reachable; try again later",
}
DONE = {
    "blocked": (
        "Reported. The Commander sees that you are blocked. End your turn now: their answer "
        "arrives as your next input."
    ),
    "needs_decision": (
        "Reported. The Commander sees your question and options. End your turn now: their "
        'choice arrives as your next input, typed as "Decision: <option>".'
    ),
    "progress": "Reported. Carry on.",
    "done": "Reported.",
}
INSTRUCTIONS = (
    "BattleLab HQ: tell the Commander where this session stands. Use hq_report when you cannot "
    "continue without the Commander (state blocked), or need them to choose (state "
    "needs_decision, which requires a question and 1–4 short options), and to report progress "
    "or that you are done. After blocked or needs_decision, end your turn: the answer arrives as "
    "your next input. What you write is shown to the Commander as your own words."
)
TOOLS = [
    {
        "name": "hq_report",
        "description": (
            "Report this session's state to the Commander (BattleLab HQ). Use it when you can't "
            "continue without the Commander (blocked), or need them to choose "
            "(needs_decision requires a question and 1–4 short options — use blocked otherwise; "
            'their pick arrives as your next input, typed as "Decision: <option>"), and to report '
            "progress or done. Plain text only. After blocked or needs_decision, end your turn "
            "and wait."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["blocked", "needs_decision", "progress", "done"],
                },
                "summary": {"type": "string", "maxLength": 280},
                "question": {"type": "string", "maxLength": 280},
                "options": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {"type": "string", "maxLength": 80},
                },
            },
            "required": ["state", "summary"],
            "additionalProperties": False,
        },
    }
]


class Unavailable(Exception):
    pass


def _read_token(token_file: str) -> str:
    """This launch's token, read from its private file (never a link), or ``""`` when there is
    none (no path, or the file is ABSENT). Any other read error is UNKNOWN: `Unavailable` (try
    again), never "this session cannot report"."""
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


def request(report: dict, *, env: dict | None = None) -> dict:
    """One ``report`` to the broker over its socket; the broker's reply object."""
    env = os.environ if env is None else env
    path, token = env.get(ENV_SOCKET, ""), _read_token(env.get(ENV_TOKEN_FILE, ""))
    if not path.startswith("/") or not token:
        return {"ok": False, "error": "unknown_capability"}
    payload = {"token": token, "op": "report", "report": report}
    try:
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeEncodeError):  # a lone surrogate, a non-JSON value
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
    def hq_report(self, args: object) -> str:
        if not isinstance(args, dict):
            raise ToolError(MEANING["bad_request"])
        try:
            reply = request(args)
        except Unavailable:
            raise ToolError(MEANING["unavailable"]) from None
        if reply.get("ok") is not True:
            code = reply.get("error")
            meaning = MEANING.get(code if isinstance(code, str) else "", MEANING["unavailable"])
            raise ToolError(meaning)
        return DONE.get(str(reply.get("state") or ""), "Reported.")

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
        if name != "hq_report":
            return _text("unknown tool", error=True)
        try:
            return _text(self.hq_report(args))
        except ToolError as exc:
            return _text(str(exc), error=True)
        except (UnicodeError, ValueError, OSError):
            return _text(MEANING["unavailable"], error=True)


def main() -> int:
    """The MCP server (stdio)."""
    if not os.environ.get(ENV_SOCKET, "").startswith("/") or not os.environ.get(ENV_TOKEN_FILE):
        sys.stderr.write(
            f"battlelab hq tool: {ENV_SOCKET} and {ENV_TOKEN_FILE} must be set; BattleLab sets "
            "them when it starts a session\n"
        )
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Server())
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
