"""HQ's subagents (#2398, epic #2070 B2): spawn, steer, wait, collect and stop API sessions.

The epic's B2 acceptance tests. A spawn goes through the session card's own claim and launch
(`api_world`'s stand-in `hq_start` runs the card's fenced check before the create and before the
first turn, as the real one does); the handles, the session-limit queue, the stop flag, the live
taint and D13 are pinned at the admissions they guard. The drive races against a real native
session are in `test_session_driver.py` and `tests/test_hq_drive.py`; a steer is that same
`session.send`.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from agent_sessions import (
    asgi_call,
    ask_operations,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    hq_authority,
    hq_subagents,
    hq_taint,
    mission_dispatch,
    missions,
    session_capacity,
    session_driver,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from drive_harness import (  # noqa: F401
    card_row,
    drive_from,
    fleet,
    new_session,
    result,
    users,
)
from test_ask_operations import (  # noqa: F401 — fixtures are used by name
    ai,
    answer,
    app,
    call,
    calls,
    live,
)
from test_ask_sessions import world  # noqa: F401
from test_hq_authority import _operator, _set
from test_hq_authority_starts import api_world  # noqa: F401
from test_native_runtime import host, ident, project, settle  # noqa: F401
from test_session_driver import STEP, TYPED, _before_post

STARTS = {"reversible": "act", "starts": "act"}
BRIEF = "Research an i18n library for web-shop and report the pick."
H = hq_subagents


@pytest.fixture(autouse=True)
def _clean():
    H.reset_for_test()
    hq_taint.reset_for_test()
    yield
    H.reset_for_test()
    hq_taint.reset_for_test()


@pytest.fixture
def limit(monkeypatch):
    """The session limit as `spawn`'s advisory check sees it: ``state["full"]`` raises."""
    state = {"full": False, "checks": 0}
    real = session_capacity.check

    def check(*, origin=session_capacity.OTHER):
        state["checks"] += 1
        if state["full"]:
            raise session_capacity.SessionLimitReached(6, 6)
        return real(origin=origin)

    monkeypatch.setattr(session_capacity, "check", check)
    return state


@pytest.fixture
def create_limit(api_world, monkeypatch):
    """`api_world`, whose create is refused by the session limit ``api_world["refuse"]`` times
    (nothing created), as `native_runtime`'s own admission refuses it."""
    from agent_sessions import headless_dispatch, mission_structured

    api_world["refuse"] = 0
    inner = mission_structured.hq_start

    async def hq_start(*, proposal_id, engine, cwd, brief, on_key, check, **kw):
        if api_world["refuse"] > 0:
            api_world["refuse"] -= 1
            api_world["refused"] = api_world.get("refused", 0) + 1
            session_id, _b = mission_structured.hq_ids(proposal_id)
            out = headless_dispatch.Dispatch(
                key=f"{engine}:{session_id}", engine=engine, native=session_id, cwd=cwd
            )
            on_key(out.key)
            out.reason = "Session limit reached: 6 of 6 running. Nothing was started."
            out.capacity = session_capacity.SESSION_LIMIT
            return out
        return await inner(
            proposal_id=proposal_id, engine=engine, cwd=cwd, brief=brief, on_key=on_key,
            check=check, **kw,
        )  # fmt: skip

    monkeypatch.setattr(mission_structured, "hq_start", hq_start)
    return api_world


def _spawn_turn(ai, *, n=1, brief=BRIEF, then=()):
    spawns = [
        call(
            "spawn", {"project_index": 0, "engine_index": 0, "brief": f"{brief} #{i}"}, cid=f"s{i}"
        )
        for i in range(n)
    ]
    return ai(calls(("list_projects", {}), ("list_agents", {})), *spawns, *then, answer("Done."))


def _turn(a, cid: str, turn_id: str | None = None) -> ask_orchestrator._Turn:
    """A task context of conversation ``cid``, as `run_turn` builds one: this turn's lists, the
    Commander's identity and a grant carrying the live taint and the stop flag."""
    turn = ask_orchestrator._Turn(
        cid=cid,
        turn_id=turn_id or str(uuid.uuid4()),
        question="q",
        working_keys=set(),
        progress=lambda ev: None,
        operator=_operator(a),
        app=a.app,
    )
    turn.grant = ask_orchestrator._act_grant(
        cid, None, turn.operator, turn.app, stopped=turn.stop.is_set
    )
    return turn


async def _entry_of(turn) -> dict:
    turn.projects = [
        {"id": "p1", "name": "api", "cwd": str(_WORLD["proj"]), "startable": True, "index": 0}
    ]
    turn.agents = [{"index": 0, "id": "claude", "name": "Claude Code", "can_start": True}]
    entry, card = await ask_orchestrator._session_entry(
        turn, {"project_index": 0, "engine_index": 0, "brief": BRIEF}, subagent=True
    )
    return {"entry": entry, "card": card, "title": entry["project"], "agent": entry["agent"]}


_WORLD: dict = {}


@pytest.fixture
def ctx(live, api_world):
    _WORLD.clear()
    _WORLD.update(api_world)
    return live


def _eventually(ok, timeout: float = 60.0) -> None:
    import time

    end = time.monotonic() + timeout
    while not ok() and time.monotonic() < end:
        time.sleep(0.1)


def _run(a, fn, *args):
    """Run on the app's own loop, so a launch task outlives the call (as in the running app)."""
    return a.c.portal.call(fn, *args)


# ---- spawn --------------------------------------------------------------------------------------


def test_under_act_spawn_starts_an_api_session_driven_by_this_task(live, ai, api_world):
    _set(**STARTS)
    script = _spawn_turn(ai)
    cid = live.new()
    assert live.ask(cid, "add Polish to web-shop").status_code == 200
    got = result(script, "s0")
    assert got["handle"] == "sp-1" and got["outcome"] == "started", got
    key = got["key"]
    assert api_world["turns"] == [(key, f"{BRIEF} #0")]
    (entry,) = ask_store.read_proposals(cid)
    assert entry["subagent"] == {"handle": "sp-1"} and entry["bypass"] is False
    assert entry["decided_by"] == "hq"
    # The driver record names this task: HQ · task 1.
    rec = session_driver.record(key)
    assert rec["who"] == "hq" and rec["cid"] == cid and rec["task"]["n"] == 1
    assert session_driver.row_fact(rec, False) == {"who": "hq", "cid": cid, "task_label": "task 1"}
    # The handle is a step record of the task; the conversation lists it.
    subs = live.get(cid)["subagents"]
    assert [(s["handle"], s["outcome"], s["key"], s["task_n"]) for s in subs] == [
        ("sp-1", "started", key, 1)
    ]
    card = live.card(cid)
    assert card["subagent"] == "sp-1" and card["decided_by"] == "hq"


def test_under_ask_spawn_is_a_start_a_subagent_card_and_nothing_starts(live, ai, api_world):
    _set("standard")
    script = _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "add Polish")
    got = result(script, "s0")
    assert got["outcome"] == "asks" and got["proposed"] is True, got
    assert api_world["starts"] == 0
    card = live.card(cid)
    assert card["state"] == "pending" and card["subagent"] == "sp-1"
    # The Commander's tap starts it.
    assert live.decide(card["action_id"]).status_code in (200, 202)
    assert live.settled(cid)["state"] == "started" and api_world["starts"] == 1


def test_a_terminal_only_agent_is_never_a_subagent(live, ai, api_world, monkeypatch):
    _set(**STARTS)
    monkeypatch.setattr(ask_proposals, "is_api", lambda engine: False)
    script = _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "spawn one")
    got = result(script, "s0")
    assert "can't be a subagent" in got["error"], got
    assert ask_store.read_proposals(cid) == [] and api_world["starts"] == 0


def test_spawn_never_starts_a_bypass_session(live, ai, api_world, monkeypatch):
    """HQ's own start of a subagent never skips permission prompts, even where the project's
    default preselects bypass for the Commander's tap (#2351): the act always starts with bypass
    off. Only the Commander's own tap may turn it on."""
    from agent_sessions import prefs

    _set(**STARTS)
    monkeypatch.setattr(ask_proposals, "can_bypass", lambda engine: True)
    prefs.set_agent_defaults({"bypass": True})
    _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "spawn one")
    (entry,) = ask_store.read_proposals(cid)
    assert entry["decided_by"] == "hq" and entry["bypass"] is False and entry["runtime"] == "api"
    assert hq_authority.effective_class(entry) == "starts"


# ---- stop against a subagent launch --------------------------------------------------------------


def _pending_card(a, ai, cid) -> str:
    _set("standard")
    _spawn_turn(ai)
    a.ask(cid, "spawn one")
    (entry,) = ask_store.read_proposals(cid)
    assert entry["state"] == "pending"
    _set(**STARTS)
    return entry["id"]


def _act(a, cid, pid, grant):
    return asyncio.run(
        ask_operations.act(
            cid, pid, operator=_operator(a), app=a.app, grant=grant,
            record={"class": "starts", "turn_id": "t" * 8, "via": None, "tainted": False},
            wait=10.0,
        )
    )  # fmt: skip


def test_stop_before_the_claim_claims_nothing_and_creates_nothing(live, ai, api_world):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    grant = hq_authority.ActGrant.now(cid=cid, stopped=lambda: True)
    state, out = _act(live, cid, pid, grant)
    assert state == "waits" and out["reason"] == hq_authority.STOPPED_REASON
    assert ask_proposals._row(cid, pid)["state"] == "pending"
    assert api_world["starts"] == 0 and api_world["created"] == []


def test_stop_between_the_claim_and_the_create_returns_it_and_creates_nothing(live, ai, api_world):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    flag = {"stop": False}
    api_world["before_create"] = lambda: flag.update(stop=True)
    grant = hq_authority.ActGrant.now(cid=cid, stopped=lambda: flag["stop"])
    state, _out = _act(live, cid, pid, grant)
    assert state == "waits"
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "pending" and hq_authority.STOPPED_REASON in row["reason"]
    assert api_world["created"] == [] and api_world["turns"] == []


def test_stop_after_the_create_sends_no_first_turn_and_says_both(live, ai, api_world):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    flag = {"stop": False}
    api_world["before_turn"] = lambda: flag.update(stop=True)
    grant = hq_authority.ActGrant.now(cid=cid, stopped=lambda: flag["stop"])
    state, _out = _act(live, cid, pid, grant)
    assert state == "ran"
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "failed" and "first message was not sent" in row["reason"]
    assert len(api_world["created"]) == 1 and api_world["turns"] == []
    assert api_world["contained"] == api_world["created"]  # no retry, the worker contained


def test_a_restart_mid_launch_reads_interrupted_and_adopts_the_derived_session(
    live, ai, api_world, monkeypatch
):
    from agent_sessions import mission_structured, native_runtime

    cid = live.new()
    pid = _pending_card(live, ai, cid)
    ask_proposals._update(cid, pid, state=ask_proposals.DECIDING)
    session_id, _b = mission_structured.hq_ids(pid)
    exists = {"yes": False}

    def session(engine_id, sid, *, retiring_ok=False):
        if exists["yes"] and sid == session_id:
            return None, {"session_key": f"{engine_id}:{sid}"}
        raise native_runtime.NativeError(404, "no such native session")

    monkeypatch.setattr(native_runtime, "_session", session)
    # Nothing was created: interrupted, nothing linked, never relaunched.
    ask_proposals.recover(cid)
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "interrupted" and not row.get("session_key")
    # The derived session exists: reported and adopted into the record, never created again.
    ask_proposals._update(cid, pid, state=ask_proposals.DECIDING)
    exists["yes"] = True
    ask_proposals.recover(cid)
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "interrupted" and row["session_key"] == f"claude:{session_id}"
    assert row["reason"] == ask_proposals.INTERRUPTED_EXISTS
    assert ask_proposals.view(row)["session_key"] == f"claude:{session_id}"
    # A tap or an act on it runs nothing: it is not pending.
    assert _act(live, cid, pid, hq_authority.ActGrant.now(cid=cid))[0] == "decided"
    assert api_world["starts"] == 0


# ---- live taint and D13 -------------------------------------------------------------------------


def test_a_hostile_read_in_one_task_parks_a_sibling_effect(live, ai, api_world):
    """Two task contexts share one conversation: task A's untrusted read (collect) marks the
    conversation; task B, admitted at its claim afterwards, gets a card with the D4 reason."""
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    grant_b = hq_authority.ActGrant.now(cid=cid)  # task B began before A read anything
    assert grant_b.admits("starts") == ""
    hq_taint.mark(cid)  # task A's collect returned agent text (`_call`, in the same step)
    assert hq_taint.persist(cid) and hq_taint.tainted(cid)
    state, out = _act(live, cid, pid, grant_b)
    assert state == "waits" and out["reason"] == hq_authority.TAINT_REASON
    assert api_world["starts"] == 0
    # The switch on: the same sibling acts.
    rev = hq_authority.view()["revision"]
    hq_authority.update({"revision": rev, "untrusted": {"starts": True}})
    grant_b = hq_authority.ActGrant.now(cid=cid)
    assert _act(live, cid, pid, grant_b)[0] == "ran" and api_world["starts"] == 1


def test_a_sibling_read_after_the_claim_refuses_the_create(live, ai, api_world):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    api_world["before_create"] = lambda: hq_taint.mark(cid)
    state, _ = _act(live, cid, pid, hq_authority.ActGrant.now(cid=cid))
    assert state == "waits"
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "pending" and row["reason"] == hq_authority.TAINT_REASON
    assert api_world["created"] == []


def test_a_sibling_read_after_the_create_refuses_the_first_turn(live, ai, api_world):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    api_world["before_turn"] = lambda: hq_taint.mark(cid)
    _act(live, cid, pid, hq_authority.ActGrant.now(cid=cid))
    assert len(api_world["created"]) == 1 and api_world["turns"] == []


def test_the_taint_flag_is_durable_and_seen_by_a_new_process_view(live):
    _set(**STARTS)
    cid = live.new()
    assert not hq_taint.tainted(cid)
    hq_taint.mark(cid)
    hq_taint.persist(cid)
    hq_taint.reset_for_test()  # a restart: memory gone, the file stays
    assert hq_taint.tainted(cid)
    assert hq_authority.ActGrant.now(cid=cid).admits("starts") == hq_authority.TAINT_REASON


def test_an_untrusted_read_tool_sets_the_live_flag_before_its_result_returns(
    live, ai, api_world, monkeypatch
):
    seen = {}
    real = hq_taint.persist

    def persist(cid):
        seen["persisted"] = real(cid)
        return seen["persisted"]

    monkeypatch.setattr(hq_taint, "persist", persist)
    ai(calls(("list_sessions", {})), answer("Listed."))
    cid = live.new()
    live.ask(cid, "list my sessions")
    assert seen.get("persisted") is True and hq_taint.tainted(cid)
    # A trusted read leaves it unset.
    ai(calls(("list_projects", {})), answer("Listed."))
    cid2 = live.new()
    live.ask(cid2, "list my projects")
    assert not hq_taint.tainted(cid2)


@pytest.mark.parametrize("what", ["logout", "lowered", "restart"])
def test_an_effect_after_logout_restart_or_a_lowering_parks_as_a_card(live, ai, api_world, what):
    cid = live.new()
    pid = _pending_card(live, ai, cid)
    grant = hq_authority.ActGrant.now(cid=cid)
    if what == "logout":
        asyncio.run(hq_authority.logged_out())
    elif what == "lowered":
        _set("standard")  # while the effect waited for its claim
    else:
        grant = None  # after a restart the task is gone: nothing holds a grant
    if grant is not None:
        state, out = _act(live, cid, pid, grant)
        assert state == "waits", out
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "pending" and api_world["starts"] == 0
    # The card is the Commander's own signed-in tap.
    assert live.decide(pid).status_code in (200, 202)
    assert live.settled(cid)["state"] == "started"


# ---- handles and the session-limit queue ---------------------------------------------------------


def test_at_the_session_limit_spawn_queues_holding_no_slot(live, ai, api_world, limit):
    _set(**STARTS)
    limit["full"] = True
    script = _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "spawn one")
    got = result(script, "s0")
    assert got["outcome"] == "queued" and "session limit 6 / 6" in got["reason"], got
    assert ask_store.read_proposals(cid) == [] and api_world["starts"] == 0
    # The turn settled: the queued handle never starts, it reads not_started and is listed.
    (sub,) = live.get(cid)["subagents"]
    assert sub["outcome"] == "not_started" and sub["handle"] == "sp-1"
    assert "Not started: sp-1" in live.get(cid)["turns"][-1]["answer"]


def _queued(a, cid, turn, n=1):
    async def go():
        made = []
        for _ in range(n):
            spec = await _entry_of(turn)
            h = H.task(cid, turn.turn_id).new(**spec)
            h.outcome = H.QUEUED
            h.deadline = asyncio.get_running_loop().time() + H.SPAWN_QUEUE_MAX_S
            made.append(h)
        return made

    return _run(a, go)


def test_two_simultaneous_waits_on_one_queued_handle_launch_it_once(ctx, api_world, limit):
    _set(**STARTS)
    cid = ctx.new()
    turn = _turn(ctx, cid)
    (h,) = _queued(ctx, cid, turn)

    async def both():
        t = H.task(cid, turn.turn_id)
        await asyncio.gather(
            ask_orchestrator._launch_handle(turn, h, act_wait=5.0),
            ask_orchestrator._launch_handle(turn, h, act_wait=5.0),
            ask_orchestrator._tick_queue(turn, t),
        )

    _run(ctx, both)
    _eventually(lambda: api_world["starts"] >= 1)
    assert api_world["starts"] == 1 and len(ask_store.read_proposals(cid)) == 1
    assert h.outcome == H.STARTED and h.key


def test_a_create_time_limit_returns_it_to_the_queue_and_it_launches_once(ctx, create_limit, limit):
    _set(**STARTS)
    cid = ctx.new()
    turn = _turn(ctx, cid)
    (h,) = _queued(ctx, cid, turn)
    create_limit["refuse"] = 1
    _run(ctx, lambda: ask_orchestrator._launch_handle(turn, h, act_wait=5.0))
    assert h.outcome == H.QUEUED and "Session limit" in h.reason, h
    (row,) = ask_store.read_proposals(cid)
    assert row["state"] == "pending" and row["notice"] == "session_limit"
    assert create_limit["created"] == []  # nothing created
    pid = h.pid
    # The next tick advances the SAME card: one card, one launch.
    t = H.task(cid, turn.turn_id)
    _run(ctx, lambda: ask_orchestrator._tick_queue(turn, t))
    assert h.outcome == H.STARTED and h.pid == pid
    _eventually(lambda: len(create_limit["created"]) >= 1)
    assert len(ask_store.read_proposals(cid)) == 1 and len(create_limit["created"]) == 1


def test_a_slot_frees_and_the_queued_spawn_starts_once_while_the_task_waits(ctx, api_world, limit):
    _set(**STARTS)
    cid = ctx.new()
    turn = _turn(ctx, cid)
    hs = _queued(ctx, cid, turn, n=3)
    limit["full"] = True
    t = H.task(cid, turn.turn_id)
    _run(ctx, lambda: ask_orchestrator._tick_queue(turn, t))
    assert [h.outcome for h in hs] == [H.QUEUED] * 3 and api_world["starts"] == 0
    assert "session limit 6 / 6" in hs[0].reason
    limit["full"] = False
    checks = limit["checks"]
    t.checked_at = -1e9
    _run(ctx, lambda: ask_orchestrator._tick_queue(turn, t))
    # ONE capacity check for the whole task, and at most one start per tick.
    assert limit["checks"] == checks + 1
    assert [h.outcome for h in hs] == [H.STARTED, H.QUEUED, H.QUEUED]
    _eventually(lambda: api_world["starts"] == 1)  # its launch runs on as a task
    assert api_world["starts"] == 1


def test_a_queued_spawn_past_its_deadline_reads_not_started(ctx, api_world, limit):
    cid = ctx.new()
    turn = _turn(ctx, cid)
    (h,) = _queued(ctx, cid, turn)
    h.deadline = 0.0
    t = H.task(cid, turn.turn_id)
    _run(ctx, lambda: ask_orchestrator._tick_queue(turn, t))
    assert h.outcome == H.NOT_STARTED and h.reason == H.DEADLINE_REASON
    assert api_world["starts"] == 0
    (rec,) = [r for r in ask_store.read(cid) if r.get("type") == "subagent"]
    assert rec["outcome"] == "not_started"


def test_stop_and_cancel_on_a_queued_handle_cancel_it(ctx, api_world, limit):
    _set(**STARTS)
    cid = ctx.new()
    turn = _turn(ctx, cid)
    a, b = _queued(ctx, cid, turn, n=2)
    turn.keys = set()
    out, _ = _run(ctx, lambda: ask_orchestrator._stop_subagent(turn, {"key": a.name}))
    assert out["outcome"] == "cancelled" and a.outcome == H.CANCELLED
    r = ctx.c.post(
        f"/api/ask/conversations/{cid}/subagents/{turn.turn_id}/{b.name}/cancel", headers=ctx.hdr
    )
    assert r.status_code == 200 and r.json()["outcome"] == "cancelled"
    assert b.outcome == H.CANCELLED
    # Cancelled is final: Cancel again is 409, and nothing ever starts.
    r = ctx.c.post(
        f"/api/ask/conversations/{cid}/subagents/{turn.turn_id}/{b.name}/cancel", headers=ctx.hdr
    )
    assert r.status_code == 409
    t = H.task(cid, turn.turn_id)
    _run(ctx, lambda: ask_orchestrator._tick_queue(turn, t))
    assert api_world["starts"] == 0
    outs = [s["outcome"] for s in ctx.get(cid)["subagents"]]
    assert outs == ["cancelled", "cancelled"]


def test_the_cancel_route_is_the_commanders_own(ctx, api_world):
    cid = ctx.new()
    turn = _turn(ctx, cid)
    (h,) = _queued(ctx, cid, turn)
    r = ctx.c.post(f"/api/ask/conversations/{cid}/subagents/{turn.turn_id}/{h.name}/cancel")
    assert r.status_code in (401, 403) and h.outcome == H.QUEUED  # no CSRF
    resp = asyncio.run(
        asgi_call.call(
            ctx.app,
            _operator(ctx),
            "POST",
            f"/api/ask/conversations/{cid}/subagents/{turn.turn_id}/{h.name}/cancel",
            {},
        )
    )
    assert resp.status == 403 and h.outcome == H.QUEUED  # never HQ's own in-process call


def test_the_owning_turn_settling_and_a_restart_never_start_a_queued_handle(ctx, api_world):
    cid = ctx.new()
    turn = _turn(ctx, cid)
    (h,) = _queued(ctx, cid, turn)
    ask_store.append(cid, H.record(turn.turn_id, h))
    # A restart: the live registry is gone; the stored queued record reads not_started.
    H.reset_for_test()
    (row,) = H.fold(ask_store.read(cid), running=None)
    assert row["outcome"] == "not_started" and row["reason"] == H.RESTART_REASON
    # While its turn runs it reads queued.
    (row,) = H.fold(ask_store.read(cid), running=turn.turn_id)
    assert row["outcome"] == "queued"
    # The owning turn settles: still-queued handles become not_started records.
    t = H.task(cid, turn.turn_id)
    h2 = t.new(entry={}, card={}, title="api", agent="Claude Code")
    h2.outcome = H.QUEUED
    recs = H.settle(cid, turn.turn_id)
    assert [(r["handle"], r["outcome"]) for r in recs] == [("sp-1", "not_started")]
    assert H.live(cid, turn.turn_id) is None and api_world["starts"] == 0


def test_the_pool_max_instances_is_respected(monkeypatch):
    from agent_sessions import specialist_capacity, specialists_store

    monkeypatch.setattr(session_capacity, "check", lambda **kw: None)
    monkeypatch.setattr(specialists_store, "active_counts", lambda: ({"writer": 2}, 4))
    monkeypatch.setattr(specialist_capacity, "max_instances", lambda: 4)
    monkeypatch.setattr(
        specialists_store, "get", lambda sid: {"current": {"capacity": 3}, "archived": False}
    )
    pinned = {"specialist": {"id": "writer", "version": 1}}
    assert "pool is full" in H.capacity([pinned])[0]
    monkeypatch.setattr(specialist_capacity, "max_instances", lambda: 10)
    # Two more of a capacity-3 specialist with 2 live: the first fits, the second does not.
    whys = H.capacity([pinned, pinned])
    assert whys[0] == "" and "at capacity" in whys[1]
    assert H.capacity([{}]) == [""]


# ---- steer, wait, collect, stop ------------------------------------------------------------------


def _driven(app, project):
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    return key, cid


def test_a_take_over_between_steers_claim_and_its_route_refuses_it(
    app, ai, host, project, fleet, monkeypatch
):
    """The Commander types into a subagent between `steer`'s claim and its route call: refused by
    generation at the route's own admission, nothing sent, the task is told and goes on."""
    _set(**STARTS)
    key, cid = _driven(app, project)
    fleet["cards"] = [card_row(key)]

    async def commander_types():
        r = await asyncio.to_thread(
            app.c.post,
            f"/api/structured/sessions/{key}/turns",
            json={"operation_id": ident(), "text": TYPED},
            headers=app.hdr,
        )
        assert r.status_code in (200, 202), r.text

    _before_post(monkeypatch, "/turns", commander_types)
    script = ai(
        call("steer", {"key": key, "text": STEP}, cid="s"),
        call("wait", {"keys": [key], "seconds": 0}, cid="w"),
        answer("The Commander took it over."),
    )
    assert app.ask(cid, "keep the writer going").status_code == 200
    got = result(script, "s")
    assert got.get("ran") == "stale" and "took this session over" in got["outcome"], got
    assert STEP not in users(project)
    # The task is told and carries on: its next tool ran, and says who drives it now.
    waited = result(script, "w")
    assert waited["items"][0]["driver"]["who"] == "you"
    assert session_driver.record(key)["who"] == "commander"


def test_a_sibling_read_after_the_claim_refuses_a_steer_at_the_real_fence(
    app, ai, host, project, fleet, monkeypatch
):
    """A sibling's hostile read lands after the steer's claim, before its admission: refused
    inside `session_input.admit_native` (A7's ExecutionGuard), nothing sent."""
    _set(**STARTS)
    key, cid = _driven(app, project)
    fleet["cards"] = [card_row(key)]
    real = session_input.admit_native

    def admit(k, check):
        hq_taint.mark(cid)  # the sibling's read completed just now
        return real(k, check)

    monkeypatch.setattr(session_input, "admit_native", admit)
    script = ai(call("steer", {"key": key, "text": STEP}, cid="s"), answer("Parked."))
    app.ask(cid, "keep going")
    got = result(script, "s")
    assert got.get("ran") in (False, None) or got.get("state") in ("pending", "stale"), got
    assert STEP not in users(project)
    (row,) = [r for r in ask_store.read_proposals(cid) if r.get("op") == "session.send"]
    assert row["state"] == "pending" and row["reason"] == hq_authority.TAINT_REASON


def test_collect_reads_the_result_and_taints_the_conversation_live(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    fleet["cards"] = [card_row(key)]
    tid = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=tid, text="look at the strings"))
    asyncio.run(settle(key, tid))
    script = ai(call("collect", {"key": key}, cid="c"), answer("Collected."))
    app.ask(cid, "what did it find?")
    got = result(script, "c")
    assert "untrusted_reply" in got, got
    assert got["untrusted_reply"] == "echo:look at the strings" and got["turn_id"] == tid
    assert hq_taint.tainted(cid)
    hq_taint.reset_for_test()
    assert hq_taint.tainted(cid)  # durable: written before the result went back


@pytest.mark.parametrize("tool", ["steer", "stop"])
def test_a_mission_held_session_is_never_steered_or_stopped_by_hq(
    app, ai, host, project, fleet, tool
):
    _set(**STARTS)
    key, cid = _driven(app, project)
    m = missions.create_mission("ship the strings")
    mission_dispatch.fenced_adopt(m["id"], key)
    fleet["cards"] = [card_row(key)]
    args = {"key": key, "text": STEP} if tool == "steer" else {"key": key}
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call(tool, args, cid="s"),
        answer("No."),
    )
    app.ask(cid, "drive it")
    assert "mission holds" in result(script, "s")["error"]
    assert not [r for r in ask_store.read_proposals(cid) if r.get("op")]


def test_wait_returns_server_facts_and_follows_a_queued_handle(ctx, api_world, limit):
    _set(**STARTS)
    cid = ctx.new()
    turn = _turn(ctx, cid)
    hs = _queued(ctx, cid, turn, n=2)
    limit["full"] = True
    out, meta = _run(
        ctx,
        lambda: ask_orchestrator._wait(
            turn, {"keys": [h.name for h in hs], "mode": "any", "seconds": 1}
        ),
    )
    assert out["settled"] is False and [i["state"] for i in out["items"]] == ["queued", "queued"]
    assert "wait" in ask_orchestrator.TRUSTED_READS  # states only: it never taints
    assert json.dumps(out).count("untrusted") == 0
    hs[1].outcome = H.CANCELLED
    out, _ = _run(
        ctx,
        lambda: ask_orchestrator._wait(
            turn, {"keys": [h.name for h in hs], "mode": "any", "seconds": 5}
        ),
    )
    assert out["settled"] is True and out["items"][1]["state"] == "cancelled"
    with pytest.raises(ask_orchestrator._ToolRefused):
        _run(ctx, lambda: ask_orchestrator._wait(turn, {"keys": ["sp-9"] * 9}))
    with pytest.raises(ask_orchestrator._ToolRefused):
        _run(ctx, lambda: ask_orchestrator._wait(turn, {"keys": ["sp-1"], "seconds": 121}))


def test_stop_ends_a_wait_within_a_tick(ctx, api_world, limit):
    import time

    cid = ctx.new()
    turn = _turn(ctx, cid)
    _queued(ctx, cid, turn)
    limit["full"] = True

    async def go():
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, turn.stop.set)
        t0 = time.monotonic()
        out, _ = await ask_orchestrator._wait(turn, {"keys": ["sp-1"], "seconds": 60})
        return out, time.monotonic() - t0

    out, took = _run(ctx, go)
    assert out["note"] == "the Commander pressed Stop" and took < 4.0


def test_the_driver_fact_on_every_session_row(monkeypatch):
    key, other = f"claude:{uuid.uuid4()}", f"claude:{uuid.uuid4()}"
    cid = "c" * 32
    rec = {"who": "hq", "cid": cid, "generation": 1, "since": 0.0, "title": "t",
           "task": {"turn_id": str(uuid.uuid4()), "n": 2}}  # fmt: skip
    assert session_driver.row_fact(session_driver._coerce(rec), False) == {
        "who": "hq",
        "cid": cid,
        "task_label": "task 2",
    }
    assert session_driver.row_fact(None, False) == {"who": "commander"}
    assert session_driver.row_fact(session_driver._coerce(rec), True) == {"who": "mission"}
    taken = {**rec, "who": "commander", "generation": 2}
    assert session_driver.row_fact(session_driver._coerce(taken), False) == {"who": "commander"}
    del key, other


# ---- reading the group after the task settled (Hermes 9131) --------------------------------------


@pytest.mark.parametrize("decision", ["start", "dismiss"])
def test_a_card_decided_after_its_task_settled_reads_its_real_outcome(
    live, ai, api_world, decision
):
    """Under Ask the task ends on the card; the Commander taps it later. The group reads the
    card, not the handle's last record: started with its key (Open, Stop, Hand back offered), or
    cancelled when dismissed."""
    _set("standard")
    _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "add Polish")
    (sub,) = live.get(cid)["subagents"]
    assert sub["outcome"] == "asks" and "key" not in sub
    card = live.card(cid)
    assert live.decide(card["action_id"], {"decision": decision}).status_code in (200, 202)
    if decision == "start":
        assert live.settled(cid)["state"] == "started"
    r = live.c.get(f"/api/ask/conversations/{cid}/subagents")
    assert r.status_code == 200, r.text
    (row,) = r.json()["subagents"]
    (stored,) = live.get(cid)["subagents"]
    if decision == "start":
        key = live.card(cid)["session_key"]
        assert row["outcome"] == stored["outcome"] == "started"
        assert row["key"] == stored["key"] == key and row.get("driver")
    else:
        assert row["outcome"] == stored["outcome"] == "cancelled"
        assert "dismissed" in row["reason"]


def test_a_starting_or_unreadable_session_is_never_reported_gone(live, ai, api_world, monkeypatch):
    """Hermes 9134: `gone` only when the runtime answered 404 for a start that settled. A card
    still deciding (its create may not have happened) reads `starting`; any other read failure
    reads `unreadable`; both recover to the session's real state on the next read."""
    _set("standard")
    _spawn_turn(ai)
    cid = live.new()
    live.ask(cid, "add Polish")
    pid = live.card(cid)["action_id"]
    state = {"raise": runtime.StructuredError(404, "no such native session")}

    async def snapshot(key):
        if state["raise"] is not None:
            raise state["raise"]
        return {"active_turn": "t-1", "turns": [], "pending_requests": []}

    monkeypatch.setattr(runtime, "snapshot", snapshot)

    def read() -> dict:
        r = live.c.get(f"/api/ask/conversations/{cid}/subagents")
        assert r.status_code == 200, r.text
        (row,) = r.json()["subagents"]
        return row

    # Deciding, the driver record written before the create, the session not there yet.
    ask_proposals._update(cid, pid, state=ask_proposals.DECIDING)
    row = read()
    assert row["outcome"] == "started" and row["state"] == "starting" and row["key"]
    # Created: it reads working.
    state["raise"] = None
    assert read()["state"] == "working"
    # Settled started: a transient failure is unreadable, never gone; then working again.
    ask_proposals._update(cid, pid, state=ask_proposals.STARTED, session_key=row["key"])
    state["raise"] = RuntimeError("the worker socket hiccuped")
    assert read()["state"] == "unreadable"
    state["raise"] = runtime.StructuredError(503, "busy")
    assert read()["state"] == "unreadable"
    state["raise"] = None
    assert read()["state"] == "working"
    # Only a 404 for a settled start is gone.
    state["raise"] = runtime.StructuredError(404, "no such native session")
    assert read()["state"] == "gone"
