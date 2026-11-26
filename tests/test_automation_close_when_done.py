"""Close the session when it's done (#2405): an automation approved with ``close_when_done``
archives the session its run started once the brief has finished, and only then.

The session state (`structured_runtime.snapshot`) and the archive route's server-side entry
(`routes.sessions.archive_key`) are faked at the seams `automation_runner.close_if_done` calls;
the store, the run's step log and the scheduler sweep are real, under `tmp_path`.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from agent_sessions import (
    automation_loop,
    automation_runner,
    native_runtime,
    structured_runtime,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions.routes import sessions as session_routes

KEY = "claude-api:11111111-2222-3333-4444-555555555555"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))

    class _M:
        runtime = "api"

    monkeypatch.setattr(automation_runner.engines, "manifest_of", lambda _e: _M())
    yield


@pytest.fixture
def world(monkeypatch):
    state = {
        "snap": {
            "state": "idle",
            "turns": [_turn("completed")],
            "pending_requests": [],
            "revision": 7,
        },
        "archived": [],
        "archive": (True, None),
        "live": None,
        "between": None,  # what happens between the look and the fence
    }

    async def snapshot(key):
        if state["snap"] is None:
            raise structured_runtime.StructuredError(404, "unknown session")
        return state["snap"]

    async def archive_key(key, *, precondition=None):
        if state["between"] is not None:
            state["between"](state)
        if precondition is not None and not await precondition():
            return False, "could not confirm the session stopped: the session changed"
        state["archived"].append(key)
        return state["archive"]

    monkeypatch.setattr(structured_runtime, "snapshot", snapshot)
    monkeypatch.setattr(session_routes, "archive_key", archive_key)
    monkeypatch.setattr(native_runtime, "_live_worker", lambda _sid: state["live"])
    return state


def _turn(state: str, kind: str = "operator") -> dict:
    return {"kind": kind, "turn_id": "t", "state": state}


def _run(close: bool = True, outcome: str = "ok", key: str | None = KEY) -> dict:
    """One stored run in the shape `begin_run` writes (the consented scope under
    ``consented_scope``), its outcome recorded. Written directly: what is under test is what
    happens after a run started its session."""
    rid = uuid.uuid4().hex
    consented = model.scope_of(_config(close), {})
    scope = {
        "config": _config(close),
        "pins": {},
        "consented_scope": consented,
        "consented_at": time.time(),
        "revision": 1,
    }
    with store._tx() as con:
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, state, outcome, scope, "
            "session_key, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, "a1", "webhook", "done", outcome, json.dumps(scope), key, time.time()),
        )
    return store.get_run(rid)


def _config(close: bool = True, bypass: bool = False) -> dict:
    return {
        "name": "review PRs",
        "trigger": model.validate_trigger({"kind": "manual"}),
        "action": model.validate_action(
            {
                "kind": "start_session",
                "engine": "claude-api",
                "folder": "/w",
                "message": {"text": "review"},
                "bypass": bypass,
                "close_when_done": close,
            }
        ),
        "policy": model.validate_policy({}),
    }


def _steps(rid: str) -> list[str]:
    return [s["step"] for s in store.get_run(rid)["steps"]]


@pytest.mark.anyio
async def test_a_finished_single_turn_session_is_archived_once(world):
    run = _run()
    assert [r["id"] for r in store.runs_to_close()] == [run["id"]]
    assert await automation_runner.close_if_done(run) == "closed"
    assert world["archived"] == [KEY]
    assert "session_closed" in _steps(run["id"])
    assert store.runs_to_close() == []  # settled: never looked at again


@pytest.mark.anyio
@pytest.mark.parametrize(
    "snap",
    [
        {"state": "running", "turns": [_turn("running")], "pending_requests": []},
        {
            "state": "awaiting_approval",
            "turns": [_turn("awaiting_approval")],
            "pending_requests": [{"request_id": "r"}],
        },
        {"state": "uncertain", "turns": [_turn("uncertain")], "pending_requests": []},
        {"state": "idle", "turns": [_turn("completed")], "pending_requests": [{"request_id": "r"}]},
    ],
)
async def test_a_session_still_working_is_left_alone(world, snap):
    world["snap"] = snap
    run = _run()
    assert await automation_runner.close_if_done(run) == "waiting"
    assert world["archived"] == []
    assert [r["id"] for r in store.runs_to_close()] == [run["id"]]


@pytest.mark.anyio
async def test_the_agents_own_background_work_keeps_the_session_open(world):
    world["snap"] = {
        "state": "idle",
        "turns": [_turn("completed")],
        "pending_requests": [],
        "native": {"background_active": True},
    }
    run = _run()
    assert await automation_runner.close_if_done(run) == "waiting"
    assert world["archived"] == []


@pytest.mark.anyio
async def test_a_session_someone_wrote_into_is_left_open_and_recorded(world):
    world["snap"] = {
        "state": "idle",
        "turns": [_turn("completed"), _turn("completed")],
        "pending_requests": [],
    }
    run = _run()
    assert await automation_runner.close_if_done(run) == "left_open"
    assert world["archived"] == []
    assert "session_left_open" in _steps(run["id"])
    assert store.runs_to_close() == []


@pytest.mark.anyio
async def test_the_agents_own_turns_do_not_count_as_someone_writing(world):
    world["snap"] = {
        "state": "idle",
        "turns": [_turn("completed"), _turn("completed", kind="agent")],
        "pending_requests": [],
        "revision": 9,
    }
    run = _run()
    assert await automation_runner.close_if_done(run) == "closed"


@pytest.mark.anyio
async def test_a_refused_archive_is_recorded_once_and_tried_again(world):
    world["archive"] = (False, "busy")
    run = _run()
    assert await automation_runner.close_if_done(run) == "refused"
    assert await automation_runner.close_if_done(run) == "refused"
    assert _steps(run["id"]).count("session_close_refused") == 1
    assert [r["id"] for r in store.runs_to_close()] == [run["id"]]
    world["archive"] = (True, None)
    assert await automation_runner.close_if_done(run) == "closed"


@pytest.mark.anyio
async def test_a_gone_session_settles_as_closed(world):
    world["snap"] = None
    run = _run()
    assert await automation_runner.close_if_done(run) == "closed"
    assert world["archived"] == []


def test_only_ok_runs_approved_with_close_and_recent_are_selected(world):
    keep = _run()
    _run(close=False)
    _run(outcome="failed")
    _run(key=None)
    assert [r["id"] for r in store.runs_to_close()] == [keep["id"]]
    later = time.time() + store.CLOSE_WINDOW_S + 60
    assert store.runs_to_close(now=later) == []


@pytest.mark.anyio
async def test_the_scheduler_tick_closes_finished_sessions(world, monkeypatch):
    run = _run()
    sched = automation_loop.Scheduler()
    monkeypatch.setattr(sched, "_own", lambda: _true())
    await sched.tick()
    assert world["archived"] == [KEY]
    assert "session_closed" in _steps(run["id"])


async def _true() -> bool:
    return True


@pytest.mark.parametrize("bypass", [False, True])
@pytest.mark.parametrize("close", [False, True])
def test_the_consent_lines_say_bypass_once_and_close_on_its_own_line(bypass, close):
    lines = model.describe(model.scope_of(_config(close, bypass), {}))
    on = [x for x in lines if x.startswith("Permission bypass: ON")]
    off = [x for x in lines if x == "Permission bypass: off"]
    assert (len(on), len(off)) == ((1, 0) if bypass else (0, 1))
    assert any("closed and archived" in x for x in lines) is close


def test_close_when_done_joins_the_scope_only_when_chosen():
    base = {
        "kind": "start_session",
        "engine": "e",
        "folder": "/w",
        "message": {"text": "hi"},
    }
    plain = model.validate_action(base)
    chosen = model.validate_action({**base, "close_when_done": True})
    assert "close_when_done" not in plain and chosen["close_when_done"] is True


@pytest.mark.anyio
async def test_a_live_worker_keeps_the_session_open_until_it_idles_out(world):
    world["live"] = "w1"
    run = _run()
    assert await automation_runner.close_if_done(run) == "waiting"
    world["live"] = None
    assert await automation_runner.close_if_done(run) == "closed"


@pytest.mark.anyio
async def test_a_message_between_the_look_and_the_fence_is_never_stopped(world):
    """Hermes on #2407: a turn admitted after the observation moves the journal revision (and
    relaunches a worker); the conditional archive sees it under the fence and stops nothing."""

    def a_message_arrives(state):
        state["snap"] = {**state["snap"], "revision": 8, "state": "running"}
        state["live"] = "w2"

    world["between"] = a_message_arrives
    run = _run()
    assert await automation_runner.close_if_done(run) == "waiting"
    assert world["archived"] == []
    assert "session_close_refused" not in _steps(run["id"])


@pytest.mark.anyio
async def test_background_work_starting_between_the_look_and_the_fence_is_never_stopped(world):
    def background_starts(state):
        state["live"] = "w2"

    world["between"] = background_starts
    run = _run()
    assert await automation_runner.close_if_done(run) == "waiting"
    assert world["archived"] == []
