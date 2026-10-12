"""Opt-in real CLI proof that a chosen model AND reasoning effort reach each API client (#1407).

The operator supplies a private seed home via BATTLELAB_SESSION_DEFAULTS_SMOKE_HOME and absolute
binary paths as a JSON mapping via BATTLELAB_SESSION_DEFAULTS_SMOKE_BINARIES, like the other native
smokes. For each client it asks the client's OWN model list for a model that offers levels, creates
a session with that model and one of its levels, completes a turn, stops it, and completes a second
turn in a resumed successor. A level the client did not take fails the turn (Codex refuses an
unknown `effort`, Claude an unknown `--effort`, opencode's echo must confirm it), so two completed
turns are the evidence; what each client reports is printed for the issue's acceptance record.
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
from agent_sessions import native_models, native_runtime
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry

PROMPT = "Reply with exactly the word OK and nothing else. Do not use any tools."
# Cheapest-first hints for the model a smoke spends a turn on; any model with levels qualifies.
_CHEAP = ("free", "haiku", "mini", "flash", "small", "lite")


def _pick(listing) -> tuple[str, str]:
    with_levels = [m for m in listing.models if m["efforts"]]
    assert with_levels, "the client lists no model with reasoning levels"
    cheap = [m for m in with_levels if any(h in m["id"].lower() for h in _CHEAP)]
    model = (cheap or with_levels)[0]
    order = ("minimal", "low", "medium", "high")
    level = next((lv for lv in order if lv in model["efforts"]), model["efforts"][0])
    return model["id"], level


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _turn(key: str, label: str) -> dict:
    turn = str(uuid.uuid4())
    await runtime.submit_turn(key, operation_id=turn, text=PROMPT)
    settled = None
    for _ in range(600):
        snap = await runtime.snapshot(key)
        for request in snap["pending_requests"]:  # nothing is asked for; anything asked is refused
            await runtime.decide(
                key,
                decision_id=str(uuid.uuid4()),
                request_id=request["request_id"],
                turn_id=turn,
                decision="reject",
                user="session-defaults-smoke",
            )
        settled = next((t for t in snap["turns"] if t["turn_id"] == turn), None)
        if settled and settled["state"] not in {"running", "awaiting_approval"}:
            break
        await asyncio.sleep(0.5)
    assert settled and settled["state"] == "completed", f"{label}: turn did not complete: {settled}"
    return {"snap": snap, "turn": settled}


@pytest.mark.skipif(
    not os.environ.get("BATTLELAB_SESSION_DEFAULTS_SMOKE_HOME"),
    reason="opt-in: isolated authentication seed and real vendor CLIs required",
)
@pytest.mark.anyio
@pytest.mark.parametrize(
    "source,kind",
    [("codex", "codex-app-server"), ("claude", "claude-stream-json"), ("opencode", "opencode-acp")],
)
async def test_a_chosen_model_and_effort_reach_the_real_client_on_create_and_resume(
    tmp_home, tmp_path, monkeypatch, source, kind
):
    seed = Path(os.environ["BATTLELAB_SESSION_DEFAULTS_SMOKE_HOME"])
    shutil.copytree(seed, tmp_home, dirs_exist_ok=True, symlinks=True)
    binaries = json.loads(os.environ["BATTLELAB_SESSION_DEFAULTS_SMOKE_BINARIES"])
    monkeypatch.setenv(f"AGENT_SESSIONS_{source.upper()}_BIN", binaries[source])
    monkeypatch.delenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", raising=False)
    monkeypatch.delenv("AGENT_SESSIONS_CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv(
        "AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / ".local/share/opencode/opencode.db")
    )
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "plugin-state"))
    monkeypatch.setattr(native_runtime, "HOST", native_runtime.Host())
    monkeypatch.setattr(native_models, "_CACHE", {})
    engine = source + "-api"
    providers = [p for p in registry._PROVIDERS if p.engine_id != engine]
    providers.append(
        test_manifest_api._provider(
            test_manifest_api._api(name=engine, source=source, kind=kind), tmp_path
        )
    )
    monkeypatch.setattr(registry, "_PROVIDERS", providers)
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    listing = native_models.models(registry.get(engine))
    assert listing.status == "ok", listing.reason
    # BATTLELAB_SESSION_DEFAULTS_SMOKE_MODELS={"opencode": "provider/model"} pins a model when the
    # cheapest one listed is one the provider has since retired.
    pinned = json.loads(os.environ.get("BATTLELAB_SESSION_DEFAULTS_SMOKE_MODELS", "{}")).get(source)
    if pinned:
        offered = next(m for m in listing.models if m["id"] == pinned)
        model, level = pinned, _pick(type(listing)("ok", (offered,)))[1]
    else:
        model, level = _pick(listing)
    work = tmp_home / "work"
    work.mkdir()
    key = None
    try:
        created = await runtime.create_session(
            engine, str(work), operation_id=str(uuid.uuid4()), model=model, effort=level
        )
        key = created["session_key"]
        assert created["model_requested"] == model and created["effort_requested"] == level
        first = await _turn(key, "create")
        native = first["snap"]["native"]["native_id"]
        worker = first["snap"]["native"]["worker"]
        await runtime.stop(key)  # the next turn is a new generation that resumes the history
        second = await _turn(key, "resume")
        assert second["snap"]["native"]["native_id"] == native
        assert second["snap"]["native"]["worker"] not in {None, worker}
        assert second["snap"]["effort_requested"] == level
        for label, got in (("create", first), ("resume", second)):
            snap = got["snap"]
            print(
                f"{source} {label}: model={model} effort={level} "
                f"model_effective={snap.get('model_effective')!r} "
                f"reply={got['turn']['reply'].strip()[:40]!r}"
            )
    finally:
        if key:
            await runtime.stop(key)
