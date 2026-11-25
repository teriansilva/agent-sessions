"""Checking a project MCP server (#2163): does it start, answer, list its tools and — optionally —
answer one probe call, with exactly the settings one scope resolves to.

A check runs the REAL delivery path: a check-only capability (`secrets_broker.mint_check`) for
``(server, revision, scope)``, the same launcher every agent CLI starts
(``python -I -m agent_sessions.mcp_launch``), in a private empty working directory, bounded to
`CHECK_TIMEOUT_S`. The result is settled by `mcp_registry.record_check`, which publishes
``passed`` only if the revision and the resolved-set digest are unchanged since the check began.

**Server output is treated as a possible credential leak.** Everything the server emits that
the check keeps — stderr, ``serverInfo``, tool names, the probe's result, errors — passes
`withhold` against the scope's resolved credential values (raw, base64, URL-encoded, and inside
any base64 token such as a ``Basic`` header). A field that contains one is withheld WHOLE, never
redacted. Nothing a server sends is logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, unquote

from . import mcp_registry, secrets_broker

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 60.0
STDERR_TAIL = 4096
PROBE_TEXT_MAX = 2048
FIELD_MAX = 512
TOOL_PAGES_MAX = 10
WITHHELD = "withheld: contained a credential"
_B64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{8,}={0,2}")


# ---- withholding -------------------------------------------------------------------------------


def _decodings(token: str) -> list[bytes]:
    out = []
    for alt in (False, True):
        padded = token + "=" * (-len(token) % 4)
        try:
            out.append(
                base64.urlsafe_b64decode(padded) if alt else base64.b64decode(padded, validate=True)
            )
        except (binascii.Error, ValueError):
            continue
    return out


def contains_credential(text: str, values: tuple[str, ...] | list[str]) -> bool:
    """Whether ``text`` carries any of ``values``: raw, URL-encoded or base64 (also inside a longer
    base64 token, e.g. ``Basic base64(user:value)``)."""
    if not text or not values:
        return False
    plain = [v for v in values if v]
    if not plain:
        return False
    candidates = {text}
    with contextlib.suppress(Exception):
        candidates.add(unquote(text))
    for v in plain:
        forms = {v, quote(v, safe=""), quote(v)}
        raw = v.encode()
        for enc in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
            forms.add(enc.decode())
            forms.add(enc.decode().rstrip("="))
        if any(f and f in c for f in forms for c in candidates):
            return True
    for token in _B64_TOKEN.findall(text):
        for decoded in _decodings(token):
            if any(v.encode() in decoded for v in plain):
                return True
    return False


def withhold(value: object, values: tuple[str, ...] | list[str], cap: int = FIELD_MAX) -> str:
    """``value`` as text for a sink (Activity, the API, an HQ record), or `WITHHELD` whole when it
    carries a credential. Capped AFTER the check, so a cut can never split a credential past it."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if contains_credential(text, values):
        return WITHHELD
    return text[:cap]


# ---- the client side of one check --------------------------------------------------------------


class _Failed(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step = step
        self.detail = detail


class _Session:
    """A minimal MCP client over the launcher's stdio."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.next_id = 0

    async def send(self, msg: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        await self.proc.stdin.drain()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        await self.send(msg)
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise EOFError
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # a server that logs to stdout: not a reply
            if isinstance(reply, dict) and reply.get("id") == rid:
                return reply


def _scope_dir() -> Path:
    from . import ptybridge

    root = ptybridge.runtime_dir() / "mcp-checks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="check-", dir=root))


MALFORMED = "the server's reply does not follow the MCP protocol"


def _result(reply: object, step: str, values: tuple[str, ...]) -> dict:
    """A JSON-RPC reply's ``result`` object, or `_Failed` in words: an error reply says its message
    (withheld), anything else malformed says so — never an exception out of the check."""
    if not isinstance(reply, dict):
        raise _Failed(step, MALFORMED)
    if "error" in reply:
        err = reply["error"]
        message = err.get("message") if isinstance(err, dict) else None
        raise _Failed(step, withhold(message if isinstance(message, str) else "error", values))
    result = reply.get("result")
    if not isinstance(result, dict):
        raise _Failed(step, MALFORMED if "result" in reply else "no result")
    return result


def _text(value: object, default: str = "?") -> str:
    return value if isinstance(value, str) else default


async def _converse(
    session: _Session, rec: dict, values: tuple[str, ...], steps: list, t0: float
) -> list[str]:
    reply = await session.call(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "BattleLab check", "version": "1"},
        },
    )
    result = _result(reply, "handshake", values)
    info = result.get("serverInfo")
    if info is None:
        info = {}
    if not isinstance(info, dict):
        raise _Failed("handshake", MALFORMED)
    steps.append(
        {
            "step": "started",
            "ok": True,
            "detail": f"{int((time.monotonic() - t0) * 1000)} ms",
        }
    )
    steps.append(
        {
            "step": "handshake",
            "ok": True,
            "detail": withhold(
                f"{_text(info.get('name'))} {_text(info.get('version'), '')} · protocol "
                f"{_text(result.get('protocolVersion'))}",
                values,
            ),
        }
    )
    await session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools: list[dict] = []
    cursor = None
    for _ in range(TOOL_PAGES_MAX):
        reply = await session.call("tools/list", {"cursor": cursor} if cursor else {})
        result = _result(reply, "tools", values)
        page = result.get("tools")
        if page is None:
            page = []
        if not isinstance(page, list):
            raise _Failed("tools", MALFORMED)
        tools += [t for t in page if isinstance(t, dict)]
        cursor = result.get("nextCursor")
        if cursor is not None and not isinstance(cursor, str):
            raise _Failed("tools", MALFORMED)
        if not cursor or len(tools) >= mcp_registry.TOOLS_MAX:
            break
    names = []
    annotated: dict[str, bool] = {}
    for t in tools[: mcp_registry.TOOLS_MAX]:
        name = t.get("name")
        if not isinstance(name, str) or not mcp_registry.TOOL_NAME_RE.fullmatch(name):
            continue
        if contains_credential(name, values):
            continue  # never a credential in a snapshot Copilot is given
        names.append(name)
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        annotated[name] = ann.get("readOnlyHint") is True
    steps.append({"step": "tools", "ok": True, "detail": f"{len(names)} tools"})
    probe = rec.get("probe")
    if probe:
        tool = probe["tool"]
        if tool not in names:
            raise _Failed("probe", "the probe tool is not among the server's tools")
        if not annotated.get(tool) and not probe.get("confirmed_write"):
            raise _Failed(
                "probe", "the probe tool is not marked read-only; confirm it may change data"
            )
        reply = await session.call("tools/call", {"name": tool, "arguments": probe["arguments"]})
        result = _result(reply, "probe", values)
        content = result.get("content") if isinstance(result.get("content"), list) else []
        text = " ".join(
            c["text"]
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
        )
        shown = withhold(text, values, PROBE_TEXT_MAX)
        if result.get("isError"):
            raise _Failed("probe", shown or "the tool reported an error")
        steps.append({"step": "probe", "ok": True, "detail": shown})
    return names


async def _stderr_tail(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stderr is not None
    tail = b""
    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            return tail
        tail = (tail + chunk)[-STDERR_TAIL:]


def _kill(proc: asyncio.subprocess.Process) -> None:
    pid = proc.pid
    if not isinstance(pid, int) or pid <= 1:
        return  # never killpg(1) (= every process we may signal)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


async def run_check(sid: str, scope: str) -> dict:
    """Check server ``sid`` for ``scope`` (`mcp_registry.SHARED` or a project id); returns the
    settled result row. Never raises for the server's behaviour (that is a failed check)."""
    rec = mcp_registry.get(sid)
    records = await asyncio.to_thread(mcp_registry._variables_records)
    resolved = mcp_registry.resolve(rec, scope, records=records, with_values=True)
    started = (rec["rev"], resolved.digest)
    values = resolved.secret_values
    steps: list[dict] = []
    tools: list[str] = []

    def settle(outcome: str) -> dict:
        return mcp_registry.record_check(sid, scope, started, outcome, steps=steps, tools=tools)

    if resolved.missing:
        steps.append(
            {
                "step": "settings",
                "ok": False,
                "detail": "missing credentials: " + ", ".join(resolved.missing),
            }
        )
        return await asyncio.to_thread(settle, "failed")
    steps.append({"step": "settings", "ok": True, "detail": "resolved"})
    if rec["transport"] == "stdio":
        try:
            mcp_registry._command(rec)
        except mcp_registry.RegistryError as e:
            steps.append({"step": "installed", "ok": False, "detail": str(e)})
            return await asyncio.to_thread(settle, "failed")
        steps.append({"step": "installed", "ok": True, "detail": "installed"})
    minted = await asyncio.to_thread(
        secrets_broker.mint_check, sid, rec["rev"], scope, resolved.digest
    )
    if minted is None:
        steps.append(
            {
                "step": "started",
                "ok": False,
                "detail": "BattleLab's secrets broker is unavailable (it needs RAM-backed storage)",
            }
        )
        return await asyncio.to_thread(settle, "failed")
    cap_id, cap_env = minted
    workdir = _scope_dir()
    env = {
        "PATH": os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME") or str(Path.home()),
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        **cap_env,
        "BATTLELAB_MCP_SERVER": sid,
    }
    proc = None
    stderr_task = None
    outcome = "failed"
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "agent_sessions.mcp_launch",
            sid,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(workdir),
            start_new_session=True,
            limit=mcp_registry.TOOLS_MAX * 4096,
        )
        stderr_task = asyncio.create_task(_stderr_tail(proc))
        try:
            tools = await asyncio.wait_for(
                _converse(_Session(proc), rec, values, steps, t0), timeout=CHECK_TIMEOUT_S
            )
            outcome = "passed"
        except _Failed as f:
            steps.append({"step": f.step, "ok": False, "detail": f.detail})
        except TimeoutError:
            steps.append(
                {"step": "timeout", "ok": False, "detail": f"no answer in {CHECK_TIMEOUT_S:.0f} s"}
            )
        except (EOFError, ConnectionError, BrokenPipeError):
            steps.append({"step": "started", "ok": False, "detail": "the server exited"})
        except Exception:  # noqa: BLE001 — a reply nothing above anticipated is a failed check
            log.warning("MCP check of %s: an unexpected reply", sid)  # never the reply itself
            steps.append({"step": "protocol", "ok": False, "detail": MALFORMED})
    finally:
        if proc is not None:
            _kill(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
        tail = b""
        if stderr_task is not None:
            with contextlib.suppress(Exception):
                tail = await asyncio.wait_for(stderr_task, timeout=2)
        await asyncio.to_thread(secrets_broker.end_check, cap_id)
        # Our own mkdtemp directory; rmtree never follows a link the server planted in it.
        shutil.rmtree(workdir, ignore_errors=True)
    if outcome != "passed" and tail.strip():
        steps.append(
            {
                "step": "stderr",
                "ok": False,
                "detail": withhold(tail.decode("utf-8", "replace"), values, STDERR_TAIL),
            }
        )
    return await asyncio.to_thread(settle, outcome)
