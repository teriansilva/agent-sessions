# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""Missions dispatch to API clients through structured turns (#1898, #1273 P3b).

The real path end to end: `mission_dispatch.run` → `mission_structured.dispatch` → the structured
facade → the real native worker over the fake native child (`host`), with the mission store,
prefs and native state all under `tmp_path`. `API_MISSION_READY` is set per test; production
ships it all-false until #1900.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path

import pytest

from agent_sessions import (
    handoff,
    headless_dispatch,
    mission_dispatch,
    mission_fence,
    mission_plan,
    mission_structured,
    missions,
    prefs,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    KINDS,
    frames,
    host,
    ident,
    project,
)

ENGINE = ENGINES["claude"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(host, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "metadata.json"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    missions.reset_schema_cache_for_test()
    handoff.reset_for_tests()
    yield tmp_path
    handoff.reset_for_tests()
    missions.reset_schema_cache_for_test()


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)


def _claimed(cwd: Path, brief: str = "go and look"):
    m = missions.create_mission("ship it", cwd=str(cwd))
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd=str(cwd), engine=ENGINE, brief=brief)
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"])


def _users(work: Path) -> list[str]:
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


async def _run(mid, claimed, **kw):
    return await mission_dispatch.run(mid, claimed, registry=object(), **kw)


# --- the contract ---------------------------------------------------------------------------


def test_the_ids_are_derived_from_the_attempt_and_pinned():
    assert mission_structured.ids("plan-1") == (
        str(uuid.uuid5(mission_structured.MISSION_NAMESPACE, "plan-1:session")),
        str(uuid.uuid5(mission_structured.MISSION_NAMESPACE, "plan-1:brief")),
    )
    # The namespace is part of every recovered id: it never changes.
    assert str(mission_structured.MISSION_NAMESPACE) == "3f1c9a52-1898-5e2b-9a41-6d0f2c7b1e83"
    assert mission_structured.ids("plan-1") != mission_structured.ids("plan-2")
    with pytest.raises(ValueError):
        mission_structured.ids("")


def test_mission_ready_is_a_per_kind_capability_never_readiness(host, monkeypatch):
    assert set(kinds.API_MISSION_READY) == set(kinds.API_KINDS)
    # #1900 proved exactly one kind end to end (`test_mission_api_e2e.py`); the rest stay off.
    assert {k for k, on in kinds.API_MISSION_READY.items() if on} == {"claude-stream-json"}
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], False)
    assert not runtime.describe(ENGINE, check_ready=False).mission_ready
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)
    unchecked = runtime.describe(ENGINE, check_ready=False)
    assert unchecked.mission_ready and not unchecked.ready  # the bit, without readiness
    assert not runtime.describe(ENGINES["codex"], check_ready=False).mission_ready


def test_plan_admits_an_api_client_only_when_mission_ready_and_ready(store, monkeypatch):
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], False)  # the rollback switch
    offered = {o["id"] for o in mission_plan.engine_options()}
    assert ENGINE not in offered
    listed = {u["id"]: u["reason"] for u in mission_plan.engine_unavailable()}
    assert listed.get(ENGINE) == mission_structured.NOT_MISSION_READY
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], True)
    assert ENGINE in {o["id"] for o in mission_plan.engine_options()}
    monkeypatch.setattr(mission_structured.registry, "can_start", lambda _p: False)
    assert ENGINE not in {o["id"] for o in mission_plan.engine_options()}


def test_no_mission_module_reaches_the_facade_but_through_mission_structured():
    """The #1897 fence is caller-supplied, so ONE helper builds it and nothing else may submit."""
    root = Path(mission_structured.__file__).parent
    watched = [
        p
        for p in root.rglob("*.py")
        if p.name.startswith(("mission", "actuator", "orchestrator", "automation"))
        and p.name != "mission_structured.py"
    ]
    assert watched, "the ratchet watches nothing"
    names = {"create_session", "submit_turn"}
    offenders = []
    for path in watched:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Attribute) and node.attr in names)
                or (isinstance(node, ast.Name) and node.id in names)
                or (isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names))
                or (isinstance(node, ast.Constant) and node.value in names)  # getattr(x, "…")
            )
            if hit:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


# --- end to end ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_mission_dispatches_one_api_session_and_one_fenced_brief(
    store, ready, project, monkeypatch
):
    monkeypatch.setattr(prefs, "unattended_bypass_granted", lambda: True)  # #1215 grant ON
    mid, claimed = _claimed(project, brief="look at the logs")
    result = await _run(mid, claimed)
    session_id, brief_id = mission_structured.ids(claimed["plan_id"])
    key = f"{ENGINE}:{session_id}"
    assert result["outcome"] == "started", result
    assert result["session_key"] == key
    assert missions.get_mission(mid)["state"] == "running"
    snap = await runtime.snapshot(key)
    assert not snap.get("bypass"), "a mission's API session never skips permission prompts"
    brief = next(t for t in snap["turns"] if t["turn_id"] == brief_id)
    assert brief["context"] == {"mission_id": mid, "reservation_id": claimed["plan_id"]}
    assert _users(project).count("look at the logs") == 1
    details = [
        str((e.get("meta") or {}).get("detail") or "") for e in missions.get_mission(mid)["events"]
    ]
    dispatched = next(d for d in details if d.startswith("dispatched"))
    assert "permission bypass off" in dispatched, dispatched


@pytest.mark.anyio
async def test_a_client_that_is_ready_but_not_mission_ready_is_refused(store, project, monkeypatch):
    monkeypatch.setitem(kinds.API_MISSION_READY, KINDS["claude"], False)  # the rollback switch
    mid, claimed = _claimed(project)
    result = await _run(mid, claimed)
    assert result["outcome"] == "refused"
    assert mission_structured.NOT_MISSION_READY in result["reason"]
    assert _users(project) == []


@pytest.mark.anyio
async def test_a_withdrawal_after_the_create_refuses_the_brief_and_stops_the_session(
    store, ready, project, monkeypatch
):
    mid, claimed = _claimed(project, brief="FENCED BRIEF")
    state = {"why": None}
    real_create = mission_structured.runtime.create_session

    async def create_then_withdraw(*a, **kw):
        snap = await real_create(*a, **kw)
        state["why"] = "the operator stood the mission down"  # lands between create and brief
        return snap

    monkeypatch.setattr(mission_structured.runtime, "create_session", create_then_withdraw)
    result = await _run(mid, claimed, extra_authorize=lambda: state["why"])
    assert result["outcome"] == "failed", result
    assert "FENCED BRIEF" not in _users(project)
    session_id, _ = mission_structured.ids(claimed["plan_id"])
    probed = await runtime.probe(f"{ENGINE}:{session_id}")
    assert probed["containment"] == "gone", "the session the failed dispatch started kept running"
    row = missions.get_mission(mid)
    assert row["state"] != "running"


@pytest.mark.anyio
async def test_the_in_flight_api_session_is_named_by_every_mission_withdrawal(
    store, ready, project
):
    mid, claimed = _claimed(project)
    session_id, _ = mission_structured.ids(claimed["plan_id"])
    key = f"{ENGINE}:{session_id}"
    assert missions.note_dispatch_session(mid, key, expect_plan=claimed["plan_id"])
    assert key in mission_fence.held_keys(mid)
    tokens = session_input.admit_native(key, lambda: (True, ""))
    with session_input.sessions_transaction(mission_fence.held_keys(mid)):
        pass
    from agent_sessions import native_authority

    assert not native_authority.current(session_id, tokens)


@pytest.mark.anyio
async def test_a_repeated_dispatch_of_the_same_attempt_replays_one_session_and_one_brief(
    store, ready, project
):
    mid, claimed = _claimed(project, brief="only once")

    def authorize(_observed):
        return None

    kw = dict(
        mission_id=mid,
        plan_id=claimed["plan_id"],
        engine=ENGINE,
        cwd=str(project),
        brief="only once",
        on_key=lambda _k: None,
        authorize=authorize,
    )
    first = await mission_structured.dispatch(**kw)
    again = await mission_structured.dispatch(**kw)  # a crash after the brief, recovered by replay
    assert first.key == again.key == f"{ENGINE}:{mission_structured.ids(claimed['plan_id'])[0]}"
    assert first.briefed and again.briefed
    assert _users(project).count("only once") == 1
    assert len(host_launches()) == 1


def host_launches():
    from agent_sessions import native_runtime

    return native_runtime.HOST.launches


@pytest.mark.anyio
async def test_an_uncertain_brief_is_recorded_unknown_and_never_sent_again(
    store, ready, project, monkeypatch
):
    mid, claimed = _claimed(project)

    async def uncertain(*_a, **_kw):
        return {"handoff": "uncertain"}

    monkeypatch.setattr(mission_structured.runtime, "submit_turn", uncertain)
    out = await mission_structured.dispatch(
        mission_id=mid,
        plan_id=claimed["plan_id"],
        engine=ENGINE,
        cwd=str(project),
        brief="maybe",
        on_key=lambda _k: None,
        authorize=lambda _o: None,
    )
    assert out.started and not out.briefed and not out.ok
    assert out.seed_outcome == "unknown"
    assert out.seed_outcome not in missions.RETRYABLE_SEED_OUTCOMES


@pytest.mark.anyio
async def test_the_brief_is_sanitised_like_the_console_seed(store, ready, project):
    mid, claimed = _claimed(project)
    with pytest.raises(headless_dispatch.DispatchError, match="too large"):
        await mission_structured.dispatch(
            mission_id=mid,
            plan_id=claimed["plan_id"],
            engine=ENGINE,
            cwd=str(project),
            brief="x" * (handoff.SEED_CAP_BYTES + 1),
            on_key=lambda _k: None,
            authorize=lambda _o: None,
        )
    assert host_launches() == []


@pytest.mark.anyio
async def test_a_create_that_never_becomes_ready_is_a_failed_dispatch_with_its_record_kept(
    store, ready, project, monkeypatch
):
    mid, claimed = _claimed(project)

    real_create = mission_structured.runtime.create_session

    async def never_ready(*a, **kw):
        await real_create(*a, **kw)  # a worker exists and runs…
        raise runtime.StructuredError(503, "the native worker never became ready")  # …unready

    monkeypatch.setattr(mission_structured.runtime, "create_session", never_ready)
    out = await mission_structured.dispatch(
        mission_id=mid,
        plan_id=claimed["plan_id"],
        engine=ENGINE,
        cwd=str(project),
        brief="go",
        on_key=lambda _k: None,
        authorize=lambda _o: None,
    )
    assert out.launched and not out.ok and out.seed_outcome == "not_attempted"


@pytest.mark.anyio
async def test_teardown_of_an_api_session_proves_it_gone_or_keeps_the_record(
    store, ready, project, monkeypatch
):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    assert await mission_fence.abandon(key) == "stopped"
    assert (await runtime.probe(key))["containment"] == "gone"
    other = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))[
        "session_key"
    ]

    async def unconfirmed(_key):
        return {"containment": "unknown"}

    monkeypatch.setattr(runtime, "stop", unconfirmed)
    assert await mission_fence.abandon(other) == "leaked"


def test_the_reaper_frees_an_api_spawn_only_on_a_proved_gone(store, ready, project, monkeypatch):
    from agent_sessions.routes import missions as routes

    parent = f"{ENGINE}:{ident()}"
    mid, claimed = _claimed(project)
    assert missions.note_dispatch_session(mid, parent, expect_plan=claimed["plan_id"])
    assert missions.settle_dispatch(
        mid, to="running", detail="up", session_key=parent, expect_plan=claimed["plan_id"]
    )["settled"]
    missions.claim_spawn(mid, parent_key=parent, engine=ENGINE, cwd=str(project), brief="child")
    child = f"{ENGINE}:{ident()}"
    assert missions.settle_dispatch(mid, to="running", detail="child up", session_key=child)[
        "settled"
    ]
    assert missions.open_spawn_count(mid) == 1
    answer = {"containment": "unknown"}

    async def probe(_key):
        return dict(answer)

    monkeypatch.setattr(runtime, "probe", probe)
    assert routes._reap_dead_spawns(mid) == 0
    answer["containment"] = "live"
    assert routes._reap_dead_spawns(mid) == 0
    answer["containment"] = "gone"
    assert routes._reap_dead_spawns(mid) == 1


DEAD_OWNER = "4194305:1"  # a pid no host has, as `test_late_bound_dispatch` uses


def _claimed_by_a_dead_process(cwd: Path, brief: str):
    m = missions.create_mission("ship it", cwd=str(cwd))
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(m["id"], project_id="prj_a", cwd=str(cwd), engine=ENGINE, brief=brief)
    return m["id"], missions.claim_plan(m["id"], plan["plan_id"], owner=DEAD_OWNER)


async def _crashed_after(stage: str, cwd: Path, brief: str):
    """A dispatch whose process died after ``stage`` (`create` or `brief`), never settled."""
    mid, claimed = _claimed_by_a_dead_process(cwd, brief)
    session_id, brief_id = mission_structured.ids(claimed["plan_id"])
    key = f"{ENGINE}:{session_id}"
    assert missions.note_dispatch_session(mid, key, expect_plan=claimed["plan_id"])
    await runtime.create_session(ENGINE, str(cwd), operation_id=session_id)
    if stage == "brief":
        await runtime.submit_turn(key, operation_id=brief_id, text=brief)
    return mid, key


@pytest.mark.anyio
async def test_recovery_adopts_a_briefed_api_session_and_never_sends_again(store, ready, project):
    from agent_sessions import mission_dispatch_recover

    mid, key = await _crashed_after("brief", project, "recover me")
    assert await mission_dispatch_recover.recover_once() == 1
    # Settled like every recovered dispatch: failed, with the session ADOPTED for the operator.
    assert missions.holder_of(key) == mid
    assert (await runtime.probe(key))["containment"] == "live", "an adopted session was stopped"
    details = [
        str((e.get("meta") or {}).get("detail")) for e in missions.get_mission(mid)["events"]
    ]
    assert any("its brief was handed to the agent" in d for d in details), details
    assert _users(project).count("recover me") == 1


@pytest.mark.anyio
async def test_recovery_stops_an_api_session_that_never_got_its_brief(store, ready, project):
    from agent_sessions import mission_dispatch_recover

    mid, key = await _crashed_after("create", project, "never sent")
    assert await mission_dispatch_recover.recover_once() == 1
    assert missions.get_mission(mid)["state"] != "running"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert "never sent" not in _users(project)


@pytest.mark.anyio
async def test_an_abandon_between_create_and_brief_sends_nothing(
    store, ready, project, monkeypatch
):
    """Independent review on #1898: an abandon leaves the dispatch row's `plan_id` intact, so the
    brief's fenced check must ask the MISSION too, or fresh tokens let the brief through."""
    mid, claimed = _claimed(project, brief="ABANDONED BRIEF")
    real_create = mission_structured.runtime.create_session

    async def create_then_abandon(*a, **kw):
        snap = await real_create(*a, **kw)
        missions.set_state(mid, "dispatching", "abandoned")  # the operator abandons it now
        return snap

    monkeypatch.setattr(mission_structured.runtime, "create_session", create_then_abandon)
    await _run(mid, claimed)
    assert "ABANDONED BRIEF" not in _users(project)
    session_id, _ = mission_structured.ids(claimed["plan_id"])
    assert (await runtime.probe(f"{ENGINE}:{session_id}"))["containment"] == "gone"


@pytest.mark.anyio
async def test_a_withdrawal_between_the_briefs_admission_and_its_claim_refuses_it(
    store, ready, project, monkeypatch
):
    mid, claimed = _claimed(project, brief="CLAIM WINDOW")
    real_admit = session_input.admit_native
    seen = {"admits": 0}

    def admit_then_withdraw(key, check):
        tokens = real_admit(key, check)
        seen["admits"] += 1
        if seen["admits"] == 2:  # the brief's admission; the create's was the first
            with session_input.sessions_transaction(mission_fence.held_keys(mid)):
                pass
        return tokens

    monkeypatch.setattr(mission_structured.session_input, "admit_native", admit_then_withdraw)
    result = await _run(mid, claimed)
    assert result["outcome"] == "failed", result
    assert "CLAIM WINDOW" not in _users(project)
    session_id, _ = mission_structured.ids(claimed["plan_id"])
    assert (await runtime.probe(f"{ENGINE}:{session_id}"))["containment"] == "gone"


@pytest.mark.anyio
async def test_the_session_limit_is_a_refusal_the_operator_can_press_again(
    store, ready, project, monkeypatch
):
    """Independent review on #1898: a create refused before anything ran (the session limit)
    settles `planned`, never a failed dispatch whose record names a worker that never existed."""
    mid, claimed = _claimed(project)

    async def at_the_limit(*_a, **_kw):
        raise runtime.StructuredError(429, "the session limit is reached")

    monkeypatch.setattr(mission_structured.runtime, "create_session", at_the_limit)
    result = await _run(mid, claimed)
    assert result["outcome"] == "refused", result
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_dispatch(mid) is None, "a record survived for a worker that never existed"


@pytest.mark.anyio
async def test_teardown_of_a_never_recorded_api_session_is_proved_empty(store, ready):
    assert await mission_fence.abandon(f"{ENGINE}:{ident()}") == "stopped"


@pytest.mark.anyio
async def test_recovery_finds_a_brief_scrolled_out_of_the_snapshot(store, ready, project):
    from agent_sessions import mission_dispatch_recover

    mid, key = await _crashed_after("brief", project, "first of many")
    for n in range(runtime.SNAPSHOT_TURNS + 2):
        turn = ident()
        await runtime.submit_turn(key, operation_id=turn, text=f"later {n}")
        for _ in range(200):
            t = next(x for x in (await runtime.snapshot(key))["turns"] if x.get("turn_id") == turn)
            if t.get("state") == "completed":
                break
            await asyncio.sleep(0.02)
    assert await mission_dispatch_recover.recover_once() == 1
    assert missions.holder_of(key) == mid, "a briefed session was read as never briefed"


@pytest.mark.anyio
@pytest.mark.parametrize("damage", ["mismatched", "lost"])
async def test_a_lost_or_mismatched_record_never_proves_a_live_worker_gone(
    store, ready, project, damage
):
    """Independent review on #1898: a probe 404 is a missing/mismatched RECORD, not "no worker".
    The worker side must agree before a teardown may call the boundary empty."""
    from agent_sessions import native_state

    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    native = key.partition(":")[2]
    path = native_state.session_path(native)
    record = native_state.read_session(native)
    if damage == "mismatched":
        native_state.write_session(native, {**record, "source_engine": "someone-else"})
    else:
        path.unlink()
    try:
        assert await mission_fence.abandon(key) == "leaked"
    finally:
        native_state.write_session(native, record)
    assert (await runtime.probe(key))["containment"] == "live"


@pytest.mark.anyio
async def test_an_unreadable_probe_after_a_failed_create_keeps_the_record(
    store, ready, project, monkeypatch
):
    mid, claimed = _claimed(project)

    async def refused(*_a, **_kw):
        raise runtime.StructuredError(503, "the native runtime is unavailable")

    async def unreadable(_key):
        raise runtime.StructuredError(503, "the native runtime is unavailable")

    monkeypatch.setattr(mission_structured.runtime, "create_session", refused)
    monkeypatch.setattr(mission_structured.runtime, "probe", unreadable)
    out = await mission_structured.dispatch(
        mission_id=mid,
        plan_id=claimed["plan_id"],
        engine=ENGINE,
        cwd=str(project),
        brief="go",
        on_key=lambda _k: None,
        authorize=lambda _o: None,
    )
    assert out.launched and not out.ok, "an unreadable answer was taken as nothing running"


@pytest.mark.anyio
async def test_the_console_branch_still_hands_its_launch_the_mission_shelf(
    store, project, monkeypatch
):
    """Hermes on #1921: the structured branch restructured the console call; #1740's shelf
    must still ride every console mission launch (and the API branch takes none yet)."""
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        raise headless_dispatch.DispatchError("stop here")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    m = missions.create_mission("ship it", cwd=str(project))
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(
        m["id"], project_id="prj_a", cwd=str(project), engine="claude", brief="go"
    )
    claimed = missions.claim_plan(m["id"], plan["plan_id"])
    await _run(m["id"], claimed)
    assert seen.get("shelf_mission") == m["id"]
