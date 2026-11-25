"""Opt-in real CLI + systemd document consumption, with isolated homes and stores.

The operator supplies a private seed home via BATTLELAB_DOCUMENT_SMOKE_HOME and absolute
binary paths as a JSON mapping via BATTLELAB_DOCUMENT_SMOKE_BINARIES. This test
never reads the real home. A seed may hold vendor authentication and a minimal provider
configuration, but must not contain session history, tools or instructions.
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
from agent_sessions import native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

pytestmark = pytest.mark.skipif(
    not os.environ.get("BATTLELAB_DOCUMENT_SMOKE_HOME"),
    reason="opt-in: isolated authentication seed and real vendor CLIs required",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
async def test_real_client_reads_private_document(tmp_home, tmp_path, monkeypatch, source, kind):
    seed = Path(os.environ["BATTLELAB_DOCUMENT_SMOKE_HOME"])
    shutil.copytree(seed, tmp_home, dirs_exist_ok=True)
    binaries = json.loads(os.environ["BATTLELAB_DOCUMENT_SMOKE_BINARIES"])
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
    uploads = tmp_home / ".agent-sessions/uploads"
    uploads.mkdir(parents=True, mode=0o700)
    stored = "20261010-010000-acceptance.txt"
    path = uploads / stored
    marker = "DOCUMENT-" + uuid.uuid4().hex
    path.write_text(marker + "\n")
    path.chmod(0o600)
    key = None
    approvals = 0
    try:
        created = await runtime.create_session(engine, str(work), operation_id=str(uuid.uuid4()))
        key = created["session_key"]
        worker = created["native"]["worker"]
        assert native_state.read_lifecycle(worker)["invocation_id"]
        turn = str(uuid.uuid4())
        await runtime.submit_turn(
            key,
            operation_id=turn,
            text=(
                "Use your command tool to run cat on the attached text file. "
                "Use that command, not a separate read tool. "
                "Reply with its exact contents. Do not write files or run other tasks. "
                "If access is denied, stop and say so."
            ),
            attachments=[stored],
        )
        settled = None
        for _ in range(600):
            snap = await runtime.snapshot(key)
            for request in snap["pending_requests"]:
                payload = json.dumps(request["payload"])
                # One-shot access only to this disposable test document. Everything else is
                # declined; no standing grants and no permission-mode changes.
                decision = (
                    "approve"
                    if str(path) in payload and "approve" in request["choices"]
                    else "reject"
                )
                await runtime.decide(
                    key,
                    decision_id=str(uuid.uuid4()),
                    request_id=request["request_id"],
                    turn_id=turn,
                    decision=decision,
                    user="document-smoke",
                )
                approvals += decision == "approve"
            settled = next((t for t in snap["turns"] if t["turn_id"] == turn), None)
            if settled and settled["state"] not in {"running", "awaiting_approval"}:
                break
            await asyncio.sleep(0.5)
        assert settled and settled["state"] == "completed", "real document turn did not complete"
        assert marker in settled["reply"], "agent did not read the document's private contents"
        assert settled["tools"], "document contents must come from the agent's normal tool"
        with pytest.raises(runtime.StructuredError) as missing:
            await runtime.submit_turn(
                key,
                operation_id=str(uuid.uuid4()),
                text="read",
                attachments=["20261010-010000-missing.txt"],
            )
        assert missing.value.status == 422
        assert len((await runtime.snapshot(key))["turns"]) == 1
        print(
            f"{source}: real contained worker read 0600 upload; {approvals} one-shot approvals; "
            "missing file refused before handoff"
        )
    finally:
        if key:
            assert (await runtime.stop(key))["containment"] == "gone"
