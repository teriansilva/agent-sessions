# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""Automations start, send to and start missions on API sessions through structured turns (#1901).

The real path end to end: `automation_runner` → `mission_structured.automation_start` /
`automation_send` → the structured facade → the real native worker over the fake native child
(`host`), with the automation, mission and native stores all under `tmp_path`. Claude's API client
is the one mission-ready kind (`kinds.API_MISSION_READY`); Codex's is not, so it is the negative.
"""

from __future__ import annotations

import time
import uuid

import pytest

from agent_sessions import (
    automation_runner,
    headless_dispatch,
    mission_structured,
    missions,
    prefs,
    template_vars,
)
from agent_sessions import automations as model
from agent_sessions import automations_store as store
from agent_sessions import structured_runtime as runtime
from agent_sessions import templates as tstore
from agent_sessions.plugins import kinds
from agent_sessions.routes import automations as routes
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    KINDS,
    frames,
    host,
    project,
    settle,
)

CLAUDE = ENGINES["claude"]
CODEX = ENGINES["codex"]
SECRET = "hunter2-PLANTED-secret-value"  # noqa: S105 — a planted test value


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def env(host, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AUTOMATION_LOOP", "1")
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    # Production state, pinned here so the test says what it relies on.
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["codex"], False)
    missions.reset_schema_cache_for_test()
    automation_runner._TASKS.clear()
    yield
    missions.reset_schema_cache_for_test()


def _users(work) -> list[str]:
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("type") == "user":
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
    return out


def _made(cfg: dict) -> str:
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


def _start_config(folder, engine=CLAUDE, **action) -> dict:
    return {
        "name": "morning session",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "start_session",
            "engine": engine,
            "folder": str(folder),
            "message": {"text": "triage the inbox"},
            **action,
        },
    }


def _send_config(key: str, message: dict | None = None) -> dict:
    return {
        "name": "nightly nudge",
        "trigger": {"kind": "manual"},
        "action": {
            "kind": "send_to_session",
            "session_key": key,
            "message": message or {"text": "check the build"},
        },
    }


async def _api_session(work) -> str:
    """An idle API session the operator started (not an automation's)."""
    snap = await runtime.create_session(CLAUDE, str(work), operation_id=str(uuid.uuid4()))
    return snap["session_key"]


# ---- admission at save and at run -------------------------------------------------------------


def test_a_mission_ready_api_client_is_admitted_and_one_that_is_not_is_refused(project):
    assert automation_runner.target_state(CLAUDE) == (True, "")
    ok, why = automation_runner.target_state(CODEX)
    assert not ok and why == automation_runner.NOT_ADMITTED
    # The rollback switch: flipping the bit off refuses the client again.
    kinds.API_MISSION_READY[KINDS["claude"]] = False
    assert automation_runner.target_state(CLAUDE)[0] is False


def test_ask_hq_still_never_offers_an_api_client_it_cannot_launch(project, monkeypatch):
    """`engine_state` stays the CONSOLE launcher's answer (Ask HQ starts sessions only through
    `headless_dispatch`): admitting an API client for automations must not make Ask offer it."""
    from agent_sessions import ask_orchestrator, engines
    from agent_sessions.engines import registry

    # As on an install where the Claude API client is present (the fixture's client is not).
    monkeypatch.setattr(registry, "present_providers", lambda: [engines.get(CLAUDE)])
    assert automation_runner.target_state(CLAUDE) == (True, "")
    ok, why = automation_runner.engine_state(CLAUDE)
    assert not ok and why
    rows = {r["id"]: r for r in ask_orchestrator._agent_rows()}
    assert rows[CLAUDE]["can_start"] is False


def test_save_refuses_a_non_admitted_api_client_for_both_session_actions(project):
    with pytest.raises(model.AutomationError, match="can't run unattended yet"):
        routes._create(_start_config(project, engine=CODEX))
    codex_key = f"{CODEX}:11111111-2222-3333-4444-555555555555"
    with pytest.raises(model.AutomationError, match="can't run unattended yet"):
        routes._create(_send_config(codex_key))
    assert routes._create(_start_config(project))["action"]["engine"] == CLAUDE


def test_an_api_target_never_skips_permission_prompts(project):
    with pytest.raises(model.AutomationError, match="can't skip permission prompts"):
        routes._create(_start_config(project, bypass=True))


def _secret_template() -> dict:
    template_vars.create_variable({"name": "token", "kind": "secret", "value": SECRET})
    return tstore.create_template(
        {
            "name": "deploy",
            "body": "deploy with {{token}} to {{env}}",
            "fields": [
                {"name": "token", "source": "library", "kind": "secret"},
                {"name": "env", "default": "staging"},
            ],
        }
    )


def test_a_secret_template_is_refused_for_an_api_session_at_save(project):
    t = _secret_template()
    key = f"{CLAUDE}:11111111-2222-3333-4444-555555555555"
    with pytest.raises(model.AutomationError, match=automation_runner.NO_API_SINK):
        routes._create(_send_config(key, {"template_id": t["id"], "values": {}}))


# ---- start_session ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_session_creates_one_api_session_and_one_fenced_first_turn(project):
    aid = _made(_start_config(project))
    run = _claim(aid)
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    session_id, brief_id, _send = mission_structured.automation_ids(run["id"])
    key = f"{CLAUDE}:{session_id}"
    recorded = store.get_run(run["id"])
    assert recorded["session_key"] == key
    assert [s["step"] for s in recorded["steps"]][1:4] == ["launched", "started", "briefed"]
    assert store.origins()[key]["name"] == "morning session"
    snap = await runtime.snapshot(key)
    assert not snap["bypass"], "an automation's API session never skips permission prompts"
    first = next(t for t in snap["turns"] if t["turn_id"] == brief_id)
    assert first["context"] == {"reservation_id": run["id"]}
    assert _users(project).count("triage the inbox") == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_repeated_call_for_the_same_run_makes_one_session_and_one_turn(project):
    """Defence in depth: `automation_loop` never replays a run, but a repeated call of the same
    run's effect derives the same ids and replays instead of creating a second of anything."""
    aid = _made(_start_config(project))
    run = _claim(aid)
    scope = run["scope"]
    for _ in range(2):
        outcome, _why = await automation_runner._start_session(
            run, scope["config"], scope["pins"], registry=None
        )
        assert outcome == "ok"
    session_id, brief_id, _send = mission_structured.automation_ids(run["id"])
    key = f"{CLAUDE}:{session_id}"
    snap = await runtime.snapshot(key)
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count("triage the inbox") == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_consent_revoked_after_the_claim_sends_nothing(project):
    aid = _made(_start_config(project))
    run = _claim(aid)
    routes._simple(aid, "disable")
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "stopped"
    session_id, _b, _s = mission_structured.automation_ids(run["id"])
    assert not await runtime.may_have_worker(f"{CLAUDE}:{session_id}")
    assert _users(project) == []


@pytest.mark.anyio
async def test_authority_withdrawn_inside_the_fence_refuses_the_first_turn_and_stops_the_worker(
    project, monkeypatch
):
    """The automation is turned off AFTER the create and before the first turn's admission: the
    turn is refused inside the write fence, nothing reaches the agent, and the worker is stopped."""
    aid = _made(_start_config(project))
    run = _claim(aid)
    real_create = runtime.create_session

    async def create_then_withdraw(*a, **kw):
        out = await real_create(*a, **kw)
        # The operator's disable, committed while the run holds the effect lock (it would answer
        # `in_flight`): written straight to the store, exactly what the fence reads.
        store.mutate(aid, lambda _r: {"enabled": False})
        return out

    monkeypatch.setattr(runtime, "create_session", create_then_withdraw)
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "stopped", res["run"]
    steps = [s["step"] for s in store.get_run(run["id"])["steps"]]
    assert "launched" in steps and "briefed" not in steps and "worker_stopped" in steps
    assert _users(project) == []
    session_id, _b, _s = mission_structured.automation_ids(run["id"])
    assert (await runtime.probe(f"{CLAUDE}:{session_id}"))["containment"] == "gone"


# ---- send_to_session --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_text_send_is_one_guarded_turn_into_the_api_session(project):
    key = await _api_session(project)
    aid = _made(_send_config(key))
    run = _claim(aid)
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    _sid, _b, send_id = mission_structured.automation_ids(run["id"])
    snap, turn = await settle(key, send_id)
    assert turn["context"] == {"reservation_id": run["id"]}
    assert _users(project).count("check the build") == 1
    recorded = store.get_run(run["id"])
    assert recorded["session_key"] == key and recorded["inputs"] == {"message": "check the build"}
    assert key not in store.origins()  # typing into a session did not START it
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_secret_free_template_is_delivered_and_a_secret_at_run_is_refused(
    project, monkeypatch
):
    key = await _api_session(project)
    template_vars.create_variable({"name": "thing", "kind": "text", "value": "ci"})
    t = tstore.create_template(
        {
            "name": "check",
            "body": "check {{thing}}",
            "fields": [{"name": "thing", "source": "library", "kind": "text"}],
        }
    )
    aid = _made(_send_config(key, {"template_id": t["id"], "values": {}}))
    res = await automation_runner.execute(_claim(aid), registry=None)
    assert res["run"]["outcome"] == "ok", res["run"]["reason"]
    assert _users(project).count("check ci") == 1
    # The library reports the variable secret at the last moment before the turn (through
    # `execute` the pins catch a kind change first, as a drift): the send's own render refuses it
    # with the API reason, and nothing is sent.
    monkeypatch.setattr(template_vars, "secret_state", lambda: {"thing": "ok"})
    run = _claim(aid)
    scope = run["scope"]
    outcome, why = await automation_runner._send_structured(run, scope["config"], scope["pins"])
    assert (outcome, why) == ("refused", automation_runner.NO_API_SINK)
    assert _users(project).count("check ci") == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_send_never_relaunches_an_ended_worker(project):
    key = await _api_session(project)
    await runtime.stop(key)
    aid = _made(_send_config(key))
    res = await automation_runner.execute(_claim(aid), registry=None)
    assert res["run"]["outcome"] == "refused" and "isn't running" in res["run"]["reason"]
    assert (await runtime.probe(key))["containment"] == "gone"
    assert _users(project) == []


@pytest.mark.anyio
async def test_a_send_outside_the_boundary_is_refused_from_the_native_records_cwd(project):
    key = await _api_session(project)
    aid = _made(_send_config(key))
    prefs.set_folder_exclusions([str(project)])
    res = await automation_runner.execute(_claim(aid), registry=None)
    assert res["run"]["outcome"] == "refused"
    assert "outside your project folders" in res["run"]["reason"]
    assert _users(project) == []
    await runtime.stop(key)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raised,code",
    [
        (runtime.StructuredError(404, "gone"), "gone"),
        (runtime.StructuredError(409, "busy"), "refused"),
        (runtime.StructuredError(422, "bad"), "refused"),
        (runtime.StructuredError(502, "lost"), "uncertain"),
        (TimeoutError(), "uncertain"),
    ],
)
async def test_the_send_outcome_is_derived_from_the_raw_codes(monkeypatch, raised, code):
    async def submit(*_a, **_kw):
        raise raised

    monkeypatch.setattr(runtime, "submit_turn", submit)
    got, _detail = await mission_structured.automation_send(
        binding="automation:a1:r1",
        run_id="r1",
        key=f"{CLAUDE}:x",
        text="t",
        check=lambda: (True, ""),
    )
    assert got == code


@pytest.mark.anyio
@pytest.mark.parametrize(
    "handoff,code",
    [("sent", "sent"), ("queued", "queued"), ("not_sent", "refused"), ("uncertain", "uncertain")],
)
async def test_the_handoff_code_maps_without_the_ledger_words(monkeypatch, handoff, code):
    async def submit(*_a, **_kw):
        return {"handoff": handoff}

    monkeypatch.setattr(runtime, "submit_turn", submit)
    got, _detail = await mission_structured.automation_send(
        binding="automation:a1:r1",
        run_id="r1",
        key=f"{CLAUDE}:x",
        text="t",
        check=lambda: (True, ""),
    )
    assert got == code
    expected = {"sent": "ok", "queued": "ok", "uncertain": "failed"}.get(code, "refused")
    assert automation_runner._API_SEND_OUTCOME[code] == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "target,words",
    [
        ({"gone": True}, "no longer exists"),
        ({"unreadable": "lost"}, "could not be read"),
        ({"cwd": None, "state": "idle", "live": True, "read_only": "retiring"}, "no new messages"),
        ({"cwd": None, "state": "idle", "live": False}, "isn't running"),
        ({"cwd": None, "state": "idle", "live": True, "pending": True}, "approval or a question"),
        ({"cwd": None, "state": "running", "live": True}, "was busy"),
        ({"cwd": None, "state": "failed", "live": True}, "last message did not complete"),
    ],
)
async def test_every_send_refusal_before_the_turn_sends_nothing(
    project, monkeypatch, target, words
):
    key = f"{CLAUDE}:11111111-2222-3333-4444-555555555555"
    aid = _made(_send_config(key))

    async def fake_target(_key):
        return {**target, "cwd": str(project)} if "state" in target else target

    async def never(**_kw):
        raise AssertionError("nothing may be sent")

    monkeypatch.setattr(mission_structured, "automation_target", fake_target)
    monkeypatch.setattr(mission_structured, "automation_send", never)
    res = await automation_runner.execute(_claim(aid), registry=None)
    assert res["run"]["outcome"] == "refused" and words in res["run"]["reason"], res["run"]


@pytest.mark.anyio
async def test_a_first_turn_the_agent_did_not_take_is_refused_and_its_worker_stopped(
    project, monkeypatch
):
    aid = _made(_start_config(project))
    run = _claim(aid)

    async def not_taken(*_a, **_kw):
        return {"handoff": "not_sent"}

    monkeypatch.setattr(runtime, "submit_turn", not_taken)
    res = await automation_runner.execute(run, registry=None)
    assert res["run"]["outcome"] == "refused", res["run"]
    assert "worker was stopped" in res["run"]["reason"]
    assert "worker_stopped" in [s["step"] for s in store.get_run(run["id"])["steps"]]
    session_id, _b, _s = mission_structured.automation_ids(run["id"])
    assert (await runtime.probe(f"{CLAUDE}:{session_id}"))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_worker_that_could_not_be_stopped_is_a_failure_on_record(project, monkeypatch):
    from agent_sessions import runtime_cleanup

    aid = _made(_start_config(project))
    run = _claim(aid)

    async def not_taken(*_a, **_kw):
        return {"handoff": "not_sent"}

    async def cannot_stop(*_a, **_kw):
        raise runtime_cleanup.UnresolvableRuntime("could not be confirmed stopped")

    monkeypatch.setattr(runtime, "submit_turn", not_taken)
    monkeypatch.setattr(runtime_cleanup, "contain_native", cannot_stop)
    res = await automation_runner.execute(run, registry=None)
    assert (
        res["run"]["outcome"] == "failed" and "stopping its worker failed" in res["run"]["reason"]
    )
    assert "stop_failed" in [s["step"] for s in store.get_run(run["id"])["steps"]]
    session_id, _b, _s = mission_structured.automation_ids(run["id"])
    await runtime.stop(f"{CLAUDE}:{session_id}")


# ---- start_mission ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_automation_mission_on_an_api_engine_dispatches_structured(project, monkeypatch):
    """Nothing new for `start_mission`: the real `dispatch_approved` → `mission_dispatch.run`
    already sends an API plan through `mission_structured.dispatch` (#1898)."""
    from agent_sessions import projects, scopedspawn
    from agent_sessions.routes import missions as mroutes

    proj = projects.create("repo", folders=[str(project)], default_folder=str(project))
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(scopedspawn, "enabled", lambda: True)
    monkeypatch.setattr(scopedspawn, "available", lambda: True)

    async def producers(mid):  # what the planner and the objective producer would have written
        m = missions.get_mission(mid)
        missions.put_plan(mid, project_id=m["project_id"], cwd=m["cwd"], engine=CLAUDE, brief="go")
        missions.patch_objectives(mid, [{"op": "add", "key": "pr", "title": "a PR", "gate": True}])
        missions.settle_objectives_state(mid, "done")

    monkeypatch.setattr(mroutes, "_produce_for_new_mission", producers)

    async def no_console(**_kw):
        raise AssertionError("an API plan never reaches the console launcher")

    monkeypatch.setattr(headless_dispatch, "dispatch", no_console)
    aid = _made(
        {
            "name": "nightly audit",
            "trigger": {"kind": "manual"},
            "action": {
                "kind": "start_mission",
                "project_id": proj.id,
                "instruction": {"text": "audit the dependencies"},
                "autonomy": "dispatch",
            },
        }
    )
    res = await automation_runner.execute(_claim(aid), registry=object())
    assert res["run"]["outcome"] == "started", res["run"]["reason"]
    mid = res["run"]["mission_id"]
    key = res["run"]["session_key"]
    assert key.startswith(f"{CLAUDE}:")
    snap = await runtime.snapshot(key)
    assert not snap["bypass"]
    [brief] = [t for t in snap["turns"] if t.get("kind") != "agent"]
    assert brief["context"]["mission_id"] == mid
    assert _users(project).count("go") == 1
    await runtime.stop(key)
