"""Opt-in real CLI + systemd proof that an API agent can read its own BattleLab id (#1406).

The operator supplies a private seed home via BATTLELAB_SESSION_ENV_SMOKE_HOME and absolute
binary paths as a JSON mapping via BATTLELAB_SESSION_ENV_SMOKE_BINARIES, like the document smoke.
Each client's agent runs ``echo "$BATTLELAB_SESSION"`` in its own tool shell, on the creating
generation and again on a resumed successor; the output must be the session key, never the
native id.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

import test_manifest_api
from agent_sessions import native_runtime
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

ECHO = 'echo "BL=[$BATTLELAB_SESSION]"'
PROMPT = (
    f"Use your shell command tool to run exactly: {ECHO}. "
    "Reply with its exact output only. Do not run anything else."
)
# Codex presents the command it will run wrapped in its login shell; the payload is the same.
_WRAPPERS = ("/bin/bash -lc ", "bash -lc ", "/bin/sh -lc ", "sh -lc ")


def _commands(value):
    """Every string under a ``command`` key anywhere in a native approval payload."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "command" and isinstance(v, str):
                yield v
            else:
                yield from _commands(v)
    elif isinstance(value, list):
        for v in value:
            yield from _commands(v)


def _unwrap(command: str) -> str:
    for wrapper in _WRAPPERS:
        quoted = command.removeprefix(wrapper)
        if quoted != command and len(quoted) >= 2 and quoted[0] == quoted[-1] == "'":
            return quoted[1:-1]
    return command


# The one action each adapter may have approved, by its complete shape (Hermes on #1410): the
# adapter's own command kind, and nothing but the command (plus a free-text description) in it.
_CLAUDE_INPUT = {"command", "description"}
_OPENCODE_INPUT = {"command", "description"}


def _is_echo(command) -> bool:
    return isinstance(command, str) and _unwrap(command) == ECHO


def approvable(request: dict) -> bool:
    """Approve only the exact echo, as each adapter's own command action, and nothing else.

    Codex: a ``command`` request (never ``file_change``). Claude: the ``Bash`` tool whose input is
    only the command (no timeout, background or sandbox switch). opencode: an ``execute`` tool
    call whose raw input is only the command, touching no locations. Every ``command`` anywhere
    in the payload must also be the echo. Everything else is declined, so a model deviation or
    an injected proposal (an edit or fetch carrying the echo, a compound command) never runs
    unreviewed.
    """
    payload = request.get("payload")
    if "approve" not in request.get("choices", ()) or not isinstance(payload, dict):
        return False
    commands = list(_commands(payload))
    if not commands or not all(_is_echo(c) for c in commands):
        return False
    if payload.get("kind") == "command":  # codex commandExecution
        return _is_echo(payload.get("command")) and "changes" not in payload
    if payload.get("tool_name") == "Bash":  # claude
        tool_input = payload.get("input")
        return (
            isinstance(tool_input, dict)
            and set(tool_input) <= _CLAUDE_INPUT
            and _is_echo(tool_input.get("command"))
            and _unwrap(tool_input["command"]) == tool_input["command"]
        )
    call = payload.get("toolCall")
    if isinstance(call, dict) and call.get("kind") == "execute":  # opencode
        raw = call.get("rawInput")
        return (
            isinstance(raw, dict)
            and set(raw) <= _OPENCODE_INPUT
            and _is_echo(raw.get("command"))
            and _unwrap(raw["command"]) == raw["command"]
            and not call.get("locations")
            and "content" not in call
        )
    return False


def test_the_smoke_approves_only_the_exact_echo():
    def req(payload):
        return {"choices": ["approve", "reject"], "payload": payload}

    claude = {"tool_name": "Bash", "input": {"command": ECHO, "description": "Print it"}}
    codex = {"kind": "command", "command": f"/bin/bash -lc '{ECHO}'", "cwd": "/w"}
    opencode = {"toolCall": {"kind": "execute", "locations": [], "rawInput": {"command": ECHO}}}
    for good in (claude, codex, opencode):
        assert approvable(req(good)), good
    for payload in (
        # compound or appended commands
        {**codex, "command": f"touch /home/operator/critical && {ECHO}"},
        {**codex, "command": f"/bin/bash -lc 'touch x; {ECHO}'"},
        {**codex, "command": f"{ECHO}\nrm -rf ~"},
        {**codex, "commandActions": [{"command": "curl example.invalid"}]},
        {"tool_name": "Bash", "input": {"command": f"{ECHO}; rm -rf ~"}},
        # the right command inside the wrong action (Hermes round 2's edit shape)
        {
            "toolCall": {
                "kind": "edit",
                "locations": [{"path": "/w/critical.md"}],
                "rawInput": {"filepath": "/w/critical.md", "diff": "-old\n+new\n", "command": ECHO},
            }
        },
        {"toolCall": {"kind": "fetch", "rawInput": {"url": "https://x.invalid", "command": ECHO}}},
        {"toolCall": {"kind": "execute", "rawInput": {"command": ECHO, "workdir": "/etc"}}},
        {"toolCall": {**opencode["toolCall"], "locations": [{"path": "/w/x"}]}},
        {"kind": "file_change", "command": ECHO, "changes": [{"path": "/w/x"}]},
        {"tool_name": "Edit", "input": {"file_path": "/etc/hosts", "command": ECHO}},
        {"tool_name": "Write", "input": {"file_path": "/etc/hosts", "content": ECHO}},
        {"tool_name": "Bash", "input": {"command": ECHO, "dangerouslyDisableSandbox": True}},
        {"tool_name": "Bash", "input": {"command": ECHO, "run_in_background": True}},
        {"tool_name": "Bash", "input": {"command": f"bash -lc '{ECHO}'"}},  # not claude's shape
        {"command": ECHO},  # no adapter action at all
        {},
    ):
        assert not approvable(req(payload)), payload
    assert not approvable({"choices": ["reject"], "payload": claude})


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _turn(key: str, label: str) -> dict:
    turn = str(uuid.uuid4())
    await runtime.submit_turn(key, operation_id=turn, text=PROMPT)
    for _ in range(600):
        snap = await runtime.snapshot(key)
        for request in snap["pending_requests"]:
            ok = approvable(request)  # one-shot, the exact echo only; everything else declined
            shown = json.dumps(request["payload"])[:300]
            print(f"{label}: {'approve' if ok else 'reject'} {shown}")
            await runtime.decide(
                key,
                decision_id=str(uuid.uuid4()),
                request_id=request["request_id"],
                turn_id=turn,
                decision="approve" if ok else "reject",
                user="session-env-smoke",
            )
        settled = next((t for t in snap["turns"] if t["turn_id"] == turn), None)
        if settled and settled["state"] not in {"running", "awaiting_approval"}:
            break
        await asyncio.sleep(0.5)
    assert settled and settled["state"] == "completed", f"{label}: turn did not complete"
    return {"snap": snap, "turn": settled}


@pytest.mark.skipif(
    not os.environ.get("BATTLELAB_SESSION_ENV_SMOKE_HOME"),
    reason="opt-in: isolated authentication seed and real vendor CLIs required",
)
@pytest.mark.anyio
@pytest.mark.parametrize(
    "source,kind",
    [
        ("codex", "codex-app-server"),
        ("claude", "claude-stream-json"),
        ("opencode", "opencode-acp"),
        ("copilot", "copilot-acp"),
    ],
)
async def test_real_agent_tool_shell_sees_its_battlelab_session(
    tmp_home, tmp_path, monkeypatch, source, kind
):
    seed = Path(os.environ["BATTLELAB_SESSION_ENV_SMOKE_HOME"])
    shutil.copytree(seed, tmp_home, dirs_exist_ok=True, symlinks=True)
    binaries = json.loads(os.environ["BATTLELAB_SESSION_ENV_SMOKE_BINARIES"])
    monkeypatch.setenv(f"AGENT_SESSIONS_{source.upper()}_BIN", binaries[source])
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv(
        "AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / ".local/share/opencode/opencode.db")
    )
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    monkeypatch.setattr(native_runtime, "HOST", native_runtime.Host())
    engine = source + "-api"
    providers = [p for p in registry._PROVIDERS if p.engine_id != engine]
    providers.append(
        test_manifest_api._provider(
            test_manifest_api._api(name=engine, source=source, kind=kind), tmp_path
        )
    )
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    work = tmp_home / "work"
    work.mkdir()
    key = None
    try:
        created = await runtime.create_session(engine, str(work), operation_id=str(uuid.uuid4()))
        key = created["session_key"]
        first = await _turn(key, "create")
        native = first["snap"]["native"]["native_id"]
        worker = first["snap"]["native"]["worker"]
        assert native and native != key.partition(":")[2]
        await runtime.stop(key)  # the next turn is a new generation that resumes the history
        second = await _turn(key, "resume")
        assert second["snap"]["native"]["native_id"] == native
        assert second["snap"]["native"]["worker"] not in {None, worker}
        for label, got in (("create", first), ("resume", second)):
            reply = got["turn"]["reply"]
            assert f"BL=[{key}]" in reply, f"{label}: {reply!r}"
            assert got["turn"]["tools"], f"{label}: the value must come from the tool shell"
            print(f"{label}: {key} -> /s/{key.replace(':', '/', 1)} reply={reply.strip()!r}")
        print(f"{source}: native id {native} (never echoed)")
    finally:
        if key:
            await runtime.stop(key)
