# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""The automation queue (#2422) through the REAL launch path: `automation_runner` →
`mission_structured.automation_start` → the structured facade → `native_runtime.create` over the
fake native worker (`host`), with the automation, specialist and native stores under `tmp_path`.

Pins what only the real create can show: the launch uses the slots the gate holds, a full pool
leaves no session record behind (the slot is taken before the record), and a capacity refusal
reaches the runner TYPED through every wrapper, so the run goes back to the queue.
"""

from __future__ import annotations

import time

import pytest

from agent_sessions import (
    automation_queue,
    automation_runner,
    mission_structured,
    missions,
    native_state,
    resource_limits,
)
from agent_sessions import automations_store as store
from agent_sessions import specialists_store as sstore
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from agent_sessions.routes import automations as routes
from test_native_runtime import ENGINES, KINDS, host, ident, project  # noqa: F401

CLAUDE = ENGINES["claude"]

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def env(host, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)
    # A generous session limit, so the specialist / pool refusals these tests expect never meet the
    # host-derived session limit first (Hermes on #2425: it depends on the runner's capacity).
    monkeypatch.setattr(resource_limits, "max_sessions", lambda: 128)
    missions.reset_schema_cache_for_test()
    automation_runner._TASKS.clear()
    automation_runner._FROM_QUEUE.clear()
    automation_runner._QUEUED.clear()
    yield
    missions.reset_schema_cache_for_test()


def _spec(capacity: int = 1) -> dict:
    return sstore.create(
        {
            "title": "Reviewer",
            "summary": "",
            "skills": [],
            "engine": CLAUDE,
            "model": "default",
            "effort": None,
            "capacity": capacity,
        }
    )


def _made(folder, spec: dict) -> str:
    cfg = {
        "name": "review",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_session",
            "engine": CLAUDE,
            "folder": str(folder),
            "message": {"text": "review the change"},
            "specialist": {"specialist_id": spec["id"], "version": 1},
        },
    }
    aid = routes._create(cfg)["id"]
    pub = routes.public(store.get(aid))
    routes._enable(
        aid, {"revision": pub["revision"], "consent": True, "scope_digest": pub["scope_digest"]}
    )
    return aid


def _claim(aid: str) -> dict:
    res = store.begin_run(aid, trigger="manual", slot=f"manual:{time.time_ns()}", fire_at=None)
    assert res["claimed"], res
    return res["run"]


def _session_id(run_id: str) -> str:
    return mission_structured.automation_ids(run_id)[0]


async def _interactive(project, spec) -> str:
    snap = await runtime.create_session(
        CLAUDE, str(project), operation_id=ident(), specialist=(spec["id"], 1)
    )
    return snap["session_key"]


async def test_a_full_specialist_queues_the_run_and_creates_no_session(project):
    spec = _spec(capacity=1)
    held = await _interactive(project, spec)
    aid = _made(project, spec)
    run = _claim(aid)
    await automation_runner.execute(run, registry=None)
    got = store.get_run(run["id"])
    assert got["state"] == "queued" and got["queue"]["capacity"] == "specialist"
    assert native_state.read_session(_session_id(run["id"])) is None, "no empty session"
    await runtime.stop(held)


async def test_the_launch_uses_the_slots_the_gate_holds(project):
    spec = _spec(capacity=1)
    aid = _made(project, spec)
    run = _claim(aid)
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    [slot] = sstore.active()
    key = f"{CLAUDE}:{_session_id(run['id'])}"
    assert (slot["session_key"], slot["origin"]) == (key, "automation")
    record = native_state.read_session(_session_id(run["id"]))
    assert record["specialist_slot"]["reservation_id"] == slot["reservation_id"]
    await runtime.stop(key)


async def test_a_capacity_refusal_reaches_the_runner_typed_and_requeues_with_nothing_created(
    project, monkeypatch
):
    """The post-precheck race: the gate's check passed without holding the specialist's slot
    (forced here), and an interactive start took it. The create is refused by the pool BEFORE its
    record is written, the refusal arrives typed (`capacity`), and the run waits again."""
    spec = _spec(capacity=1)
    aid = _made(project, spec)
    run = _claim(aid)

    def no_hold(_run):
        return automation_queue.LaunchHold(key=None)

    monkeypatch.setattr(automation_queue, "launch_slot", no_hold)
    held = await _interactive(project, spec)
    await automation_runner.execute(run, registry=None)
    got = store.get_run(run["id"])
    assert got["state"] == "queued", got
    assert got["queue"]["capacity"] == "specialist"
    assert native_state.read_session(_session_id(run["id"])) is None
    assert store.get(aid)["consecutive_failures"] == 0
    await runtime.stop(held)


async def test_an_interactive_create_refused_by_the_pool_leaves_no_session_record(project):
    spec = _spec(capacity=1)
    held = await _interactive(project, spec)
    op = ident()
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(
            CLAUDE, str(project), operation_id=op, specialist=(spec["id"], 1)
        )
    assert refused.value.capacity == "specialist"
    assert native_state.read_session(op) is None, "a refused start leaves no empty session"
    await runtime.stop(held)
