"""A mission's API session carries the mission shelf tool (#1900), as a console launch has since
#1740: minted per worker GENERATION, for the mission a dispatch launches the first one for and then
for the session's current mission, ended with the worker. The token is a 0600 file; the agent's
MCP child gets only its path, and the agent's own environment gets nothing."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from agent_sessions import (
    missions,
    native_state,
    native_worker,
    runtime_cleanup,
    secrets_broker,
    shelf_broker,
)
from agent_sessions import structured_runtime as runtime
from test_native_runtime import ENGINES, anyio_backend, frames, host, ident, project, settle

__all__ = ["anyio_backend", "host", "project"]


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    root = tmp_path / "shelf-run"
    root.mkdir(mode=0o700)
    monkeypatch.setenv(secrets_broker.RUNTIME_ENV, str(root))
    monkeypatch.setattr(secrets_broker, "_ram_check", lambda path: True)
    monkeypatch.setattr(shelf_broker, "_server_running", True)


def _config(worker: str) -> dict:
    return json.loads((native_state.root() / "workers" / worker / "config.json").read_text())


def _live_caps(key: str) -> int:
    con = missions._ready()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM mission_shelf_caps WHERE phys_key=? AND revoked_at IS NULL",
            (key,),
        ).fetchone()[0]
    finally:
        con.close()


def test_the_worker_wires_a_valid_shelf_capability_and_refuses_a_malformed_one():
    env = {shelf_broker.ENV_SOCKET: "/run/x/shelf.sock", shelf_broker.ENV_TOKEN_FILE: "/run/t"}
    config = {"adapter": "claude-stream-json", "shelf_env": env, "secrets_env": {}}
    servers = {s.name: s for s in native_worker.tool_servers(config)}
    assert "battlelab_shelf" in servers
    # Paths only: the token never rides the MCP payload.
    assert {k: servers["battlelab_shelf"].env[k] for k in env} == env
    assert "battlelab_shelf" not in {
        s.name for s in native_worker.tool_servers({**config, "shelf_env": {}})
    }
    # The agent's own environment never carries the capability.
    assert not set(env) & set(native_worker.secrets_environment(config))
    for bad in (
        {shelf_broker.ENV_SOCKET: "/run/x"},
        {**env, "EXTRA": "/x"},
        {**env, shelf_broker.ENV_SOCKET: "relative"},
        {**env, shelf_broker.ENV_TOKEN_FILE: "/run/t\nX=1"},
        ["not", "a", "dict"],
    ):
        with pytest.raises(native_worker.WorkerError):
            native_worker.shelf_environment({"shelf_env": bad})


@pytest.mark.anyio
async def test_a_dispatched_generation_carries_the_shelf_and_a_replay_stays_identical(
    host, project, shelf
):
    mid = missions.create_mission("ship it", cwd=str(project))["id"]
    engine = ENGINES["claude"]
    op = ident()
    created = await runtime.create_session(engine, str(project), operation_id=op, shelf_mission=mid)
    key = created["session_key"]
    sid = key.split(":", 1)[1]
    record = native_state.read_session(sid)
    first = record["current_worker"]
    env = _config(first)["shelf_env"]
    assert set(env) == shelf_broker.ENV_KEYS
    token_file = Path(env[shelf_broker.ENV_TOKEN_FILE])
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text().strip()
    assert "shelf" not in json.dumps(record["request"])  # never part of the create's identity
    # The tool reaches the agent through its MCP flag: the token file's PATH, never the token.
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="one")
    await settle(key, turn)
    [argv] = {tuple(f["argv"]) for f in frames(project) if "models" not in f["frame"]}
    payload = json.loads(argv[argv.index("--mcp-config") + 1])
    assert payload["mcpServers"]["battlelab_shelf"]["env"][shelf_broker.ENV_TOKEN_FILE] == str(
        token_file
    )
    assert token not in json.dumps(payload)
    # An exact replay of the create launches nothing and changes nothing.
    again = await runtime.create_session(engine, str(project), operation_id=op)
    assert again["session_key"] == key
    assert native_state.read_session(sid)["request"] == record["request"]
    assert native_state.read_session(sid)["current_worker"] == first
    assert _live_caps(key) == 1

    # The next generation asks the session's CURRENT mission (it is on the roster now), and the
    # earlier generation's capability ends.
    missions.adopt(mid, key)
    host.kill(first)
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="two")
    snap, _ = await settle(key, nxt)
    second = snap["native"]["worker"]
    assert second != first
    assert set(_config(second)["shelf_env"]) == shelf_broker.ENV_KEYS
    assert not token_file.exists()
    assert _live_caps(key) == 1

    # Containment proved gone ends the capability with the worker (archive, teardown).
    engine_id, native = key.split(":", 1)
    await runtime_cleanup.contain_native(engine_id, native)
    assert _live_caps(key) == 0


@pytest.mark.anyio
async def test_a_session_no_mission_holds_gets_no_shelf(host, project, shelf):
    created = await runtime.create_session(ENGINES["claude"], str(project), operation_id=ident())
    worker = native_state.read_session(created["session_key"].split(":", 1)[1])["current_worker"]
    assert _config(worker)["shelf_env"] == {}
