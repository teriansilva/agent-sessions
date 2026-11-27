"""Who drives an API session (#2397): the record, the Commander's take-over and hand-back, and the
fence an HQ drive request is admitted by. The races are run against the real native worker over
the fake native child (`host`): HQ's act through the in-process route, the Commander's typed turn
through the browser route, a peer's turn straight into the runtime, a mission's adoption through
its own fenced door."""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from agent_sessions import (
    asgi_call,
    ask_operations,
    ask_proposals,
    hq_authority,
    mission_dispatch,
    missions,
    session_driver,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from drive_harness import (  # noqa: F401
    card,
    card_row,
    drive_from,
    fleet,
    new_session,
    result,
    users,
)
from test_ask_orchestrator import ai, answer, app, call  # noqa: F401
from test_hq_authority import _operator, _set
from test_native_runtime import host, ident, project, settle  # noqa: F401

STEP = "Fix the date formatter for pl and rerun the tests."
TYPED = "I'll take it from here."
STARTS = {"reversible": "act", "starts": "act"}
#: Staging: starts asks (a card), whatever an earlier step of the test set.
ASK = {"reversible": "act", "starts": "ask"}


def _record(tainted: bool = False) -> dict:
    return {"class": "starts", "turn_id": "t" * 8, "via": None, "tainted": tainted}


def _act(a, cid, pid, grant=None):
    grant = grant or hq_authority.ActGrant.now()
    return asyncio.run(
        ask_operations.act(
            cid, pid, operator=_operator(a), app=a.app, grant=grant, record=_record()
        )
    )


def _stage(app, ai, fleet, key, cid, action="send", **args):
    """Under Ask: one HQ turn stages the drive card (and runs nothing). Returns its proposal id."""
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call("propose_session_change", {"action": action, "key": key, **args}, cid="s"),
        answer("Proposed."),
    )
    assert app.ask(cid, "do it").status_code == 200
    got = result(script, "s")
    assert got.get("proposed") is True and not got.get("ran"), got
    return card(cid, f"session.{action}")["id"]


def _driven(app, project):
    key = new_session(project)
    cid = app.new()
    drive_from(key, cid)
    return key, cid


def _type(app, key, text=TYPED):
    """The Commander types in the session's own composer (the browser route)."""
    return app.c.post(
        f"/api/structured/sessions/{key}/turns",
        json={"operation_id": ident(), "text": text},
        headers=app.hdr,
    )


def _texts(key) -> list[str]:
    snap = asyncio.run(runtime.snapshot(key))
    return [t.get("text") for t in snap["turns"] if t.get("kind") != "agent"]


def _before_post(monkeypatch, path_end: str, effect):
    """Run ``effect()`` (async) right before HQ's in-process POST to a path ending ``path_end``:
    after the claim and the act's second admission, before the route."""
    real = asgi_call.call

    async def wrapped(app, operator, method, path, body=None, **kw):
        if method == "POST" and path.endswith(path_end):
            await effect()
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", wrapped)


def _around_admit(monkeypatch, *, before=None, after=None):
    """Run ``before()`` / ``after()`` (blocking, in the admission's own thread) around the
    runtime's cross-process admission of HQ's guarded request."""
    real = session_input.admit_native

    def wrapped(key, check):
        if before is not None:
            before(key)
        tokens = real(key, check)
        if after is not None:
            after(key)
        return tokens

    monkeypatch.setattr(session_input, "admit_native", wrapped)


# ---- the record ---------------------------------------------------------------------------------


def test_the_store_is_private_and_generations_only_grow(host, project):
    key = new_session(project)
    rec = session_driver.record_start(key, "c" * 32, "web shop strings")
    assert rec["who"] == "hq" and rec["generation"] == 1
    assert stat.S_IMODE(os.stat(session_driver.path()).st_mode) == 0o600
    assert session_driver.record_start(key, "d" * 32) is None  # a replayed launch changes nothing
    taken = session_driver.take_over(key)
    assert taken["who"] == "commander" and taken["generation"] == 2 and taken["cid"] == "c" * 32
    assert session_driver.take_over(key) is None  # already the Commander's: no write at all
    back = session_driver.hand_back(key, 2)
    assert back["who"] == "hq" and back["generation"] == 3 and back["cid"] == "c" * 32


def test_a_session_with_no_record_is_the_commanders(host, project):
    key = new_session(project)
    assert session_driver.view(key) == {
        "who": "commander",
        "generation": 0,
        "cid": None,
        "since": None,
    }


def test_a_mission_is_derived_never_stored(host, project):
    key = new_session(project)
    session_driver.record_start(key, "c" * 32)
    m = missions.create_mission("ship the strings")
    mission_dispatch.fenced_adopt(m["id"], key)
    v = session_driver.view(key)
    assert v["who"] == "mission" and v["mission_id"] == m["id"]
    stored = session_driver.record(key)
    assert stored["who"] == "commander" and stored["generation"] == 2  # HQ let go, in the fence
    with pytest.raises(session_driver.DriverError) as e:
        session_driver.hand_back(key, 2)
    assert e.value.status == 409


def test_views_read_the_store_and_the_memberships_once(host, project, monkeypatch):
    keys = [new_session(project) for _ in range(3)]
    session_driver.record_start(keys[0], "c" * 32)
    reads = {"store": 0, "missions": 0}
    real_records, real_members = session_driver.records, missions.all_active_memberships

    def records():
        reads["store"] += 1
        return real_records()

    def members(**kw):
        reads["missions"] += 1
        return real_members(**kw)

    monkeypatch.setattr(session_driver, "records", records)
    monkeypatch.setattr(missions, "all_active_memberships", members)
    out = session_driver.views(keys)
    assert reads == {"store": 1, "missions": 1}
    assert out[keys[0]]["who"] == "hq" and out[keys[1]]["who"] == "commander"


# ---- the Commander takes over, and hands back ----------------------------------------------------


def test_typing_takes_over_and_says_so_in_the_conversation(app, host, project):
    key, cid = _driven(app, project)
    r = _type(app, key)
    assert r.status_code in (200, 202), r.text
    assert session_driver.record(key)["who"] == "commander"
    updates = app.get(cid)["updates"]
    assert [u["kind"] for u in updates] == ["taken_over"]
    view = app.c.get(f"/api/structured/sessions/{key}").json()["driver"]
    assert view["who"] == "commander" and view["can_hand_back"] is True
    assert view["conversation"]["id"] == cid


def test_hand_back_is_a_cas_refused_in_process_and_lets_hq_send_again(
    app, ai, host, project, fleet
):
    key, cid = _driven(app, project)
    _type(app, key)
    gen = session_driver.record(key)["generation"]
    stale = app.c.post(
        f"/api/sessions/{key}/driver", json={"who": "hq", "generation": gen - 1}, headers=app.hdr
    )
    assert stale.status_code == 409
    # HQ can never hand a session to itself.
    inproc = asyncio.run(
        asgi_call.call(
            app.app, _operator(app), "POST", f"/api/sessions/{key}/driver",
            {"who": "hq", "generation": gen},
        )
    )  # fmt: skip
    assert inproc.status == 403
    ok = app.c.post(
        f"/api/sessions/{key}/driver", json={"who": "hq", "generation": gen}, headers=app.hdr
    )
    assert ok.status_code == 200 and ok.json()["who"] == "hq", ok.text
    _set(**STARTS)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("propose_session_change", {"action": "send", "key": key, "message": STEP}, cid="s"),
        answer("Sent."),
    )
    app.ask(cid, "carry on")
    assert result(script, "s").get("ran") == "done"
    assert users(project).count(STEP) == 1


def test_a_session_hq_never_drove_offers_no_hand_back(app, host, project):
    key = new_session(project)
    view = app.c.get(f"/api/structured/sessions/{key}").json()["driver"]
    assert view["who"] == "commander" and view["can_hand_back"] is False
    r = app.c.post(
        f"/api/sessions/{key}/driver", json={"who": "hq", "generation": 0}, headers=app.hdr
    )
    assert r.status_code == 422 and "ask HQ to take it over" in r.json()["detail"]


def test_hq_reaches_a_sessions_turns_only_through_a_drive_request(app, host, project):
    key, _cid = _driven(app, project)
    for path, body in (
        ("turns", {"operation_id": ident(), "text": "side door"}),
        ("interrupt", {"operation_id": ident(), "turn_id": ident()}),
        ("withdraw", {"operation_id": ident(), "queued_turn_id": ident()}),
        ("send-now", {"operation_id": ident(), "turn_id": ident(), "queued_turn_id": ident()}),
    ):
        r = asyncio.run(
            asgi_call.call(
                app.app, _operator(app), "POST", f"/api/structured/sessions/{key}/{path}", body
            )
        )
        assert r.status == 403, (path, r.body)
    assert "side door" not in users(project)
    r = asyncio.run(
        asgi_call.call(
            app.app, _operator(app), "POST", f"/api/sessions/{key}/driver/adopt",
            {"generation": 1},
        )
    )  # fmt: skip
    assert r.status == 403


# ---- the take-over races, send ------------------------------------------------------------------


def test_take_over_between_the_claim_and_the_route_sends_nothing(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)

    async def commander_types():
        r = await asyncio.to_thread(_type, app, key)
        assert r.status_code in (200, 202), r.text

    _before_post(monkeypatch, "/turns", commander_types)
    kind, row = _act(app, cid, pid)
    assert kind == "ran" and row["state"] == "stale", row
    assert "Commander took this session over" in row["reason"]
    assert STEP not in users(project) and TYPED in _texts(key)


def test_take_over_before_the_runtime_admission_is_refused_there(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    # Inside `submit_turn`'s awaits, after the route: the take-over commits, then admission.
    _around_admit(monkeypatch, before=lambda k: session_driver.take_over(k))
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale" and "Commander took" in row["reason"], row
    assert STEP not in users(project)


def test_take_over_after_admission_before_the_claim_is_refused_by_the_worker(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    # Admitted, then the take-over replaces the tokens before the worker claims the turn.
    _around_admit(monkeypatch, after=lambda k: session_driver.take_over(k))
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale", row
    assert STEP not in users(project)
    assert session_driver.record(key)["who"] == "commander"


def test_hq_admitted_first_then_the_commander_types_and_takes_over(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "done", row
    asyncio.run(settle(key, session_driver.send_operation_id(pid)))
    assert _type(app, key).status_code in (200, 202)
    assert _texts(key)[-2:] == [STEP, TYPED]
    assert session_driver.record(key)["who"] == "commander"


def test_a_peer_turn_at_the_admission_time_newest_turn_check_makes_the_send_stale(
    app, ai, host, project, fleet, monkeypatch
):
    """A peer (a paired instance, a share visitor) does not change the driver: what refuses HQ is
    the newest operator turn, read INSIDE the admission (Hermes 141648)."""
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    loop_box: dict = {}
    peer = ident()

    def peer_sends(k):
        fut = asyncio.run_coroutine_threadsafe(
            runtime.submit_turn(k, operation_id=peer, text="a visitor's turn"), loop_box["loop"]
        )
        fut.result(timeout=60)

    _around_admit(monkeypatch, before=peer_sends)
    real = asgi_call.call

    async def remember(app_, operator, method, path, body=None, **kw):
        loop_box["loop"] = asyncio.get_running_loop()
        return await real(app_, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", remember)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale" and "newer turn" in row["reason"], row
    assert STEP not in users(project) and "a visitor's turn" in _texts(key)
    assert session_driver.record(key)["who"] == "hq"  # a peer never takes over


def test_a_newer_turn_from_another_submitter_since_staging_is_stale(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    first = ident()  # the session's first turn names it: a later one only moves its newest turn
    asyncio.run(runtime.submit_turn(key, operation_id=first, text="look at the strings"))
    asyncio.run(settle(key, first))
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    other = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=other, text="a share visitor"))
    _set(**STARTS)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale" and "newer turn" in row["reason"], row
    assert STEP not in users(project)


def test_an_act_and_a_tap_of_one_card_submit_once(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    _act(app, cid, pid)
    # Decided by the act: the decide route finds no pending card, and runs nothing.
    tap = app.c.post(
        f"/api/ask/proposals/{pid}/decide", json={"decision": "confirm"}, headers=app.hdr
    )
    assert tap.status_code >= 400 or tap.json()["proposal"]["state"] == "done", tap.text
    assert users(project).count(STEP) == 1
    snap = asyncio.run(runtime.snapshot(key))
    assert sum(1 for t in snap["turns"] if t.get("text") == STEP) == 1


# ---- the other verbs ----------------------------------------------------------------------------


def test_interrupt_take_over_after_admission_stops_nothing(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    hang = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=hang, text="HANG"))
    asyncio.run(settle(key, hang, state=("running",)))
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, action="interrupt")
    _set(**STARTS)
    _around_admit(monkeypatch, after=lambda k: session_driver.take_over(k))
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale", row
    assert asyncio.run(runtime.snapshot(key))["active_turn"] == hang  # still running


def test_interrupt_acts_on_the_running_turn(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    hang = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=hang, text="HANG"))
    asyncio.run(settle(key, hang, state=("running",)))
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, action="interrupt")
    _set(**STARTS)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "done" and "not yet confirmed" in row["outcome"], row
    asyncio.run(settle(key, hang, state=("interrupted", "failed", "completed")))


def test_withdraw_take_over_before_the_route_withdraws_nothing(
    app, ai, host, project, fleet, monkeypatch, tmp_path
):
    key, cid = _driven(app, project)
    busy = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=busy, text="WAIT_RELEASE"))
    asyncio.run(settle(key, busy, state=("running",)))
    _set(**ASK)
    send = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    _act(app, cid, send)  # queued behind the running turn
    queued = session_driver.send_operation_id(send)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, action="withdraw", turn_id=queued)
    _set(**STARTS)

    async def take():
        await asyncio.to_thread(session_driver.take_over, key)

    _before_post(monkeypatch, "/withdraw", take)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale", row
    snap = asyncio.run(runtime.snapshot(key))
    assert next(t for t in snap["turns"] if t["turn_id"] == queued)["state"] == "queued"
    (project / "release-queued-turn").write_text("")


def test_adopt_moves_the_conversation_and_the_first_ones_waiting_send_is_refused(
    app, ai, host, project, fleet
):
    key, first = _driven(app, project)
    _set(**ASK)
    waiting = _stage(app, ai, fleet, key, first, message=STEP)
    second = app.new()
    # A send from the second conversation is refused until it adopts.
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call("propose_session_change", {"action": "send", "key": key, "message": "x"}, cid="s"),
        call("propose_session_change", {"action": "adopt", "key": key}, cid="a"),
        answer("Adopted."),
    )
    app.ask(second, "take the web shop session over and drive it")
    refused = result(script, "s")
    assert "another HQ conversation" in refused["error"], refused
    adopt = card(second, "session.adopt")
    _set(**STARTS)
    kind, row = _act(app, second, adopt["id"])
    assert row["state"] == "done", row
    rec = session_driver.record(key)
    assert rec["cid"] == second and rec["generation"] == 2
    kind, row = _act(app, first, waiting)
    assert row["state"] == "stale", row
    assert STEP not in users(project)


def test_adopt_take_over_between_staging_and_the_route_is_stale(
    app, ai, host, project, fleet, monkeypatch
):
    key = new_session(project)
    cid = app.new()
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, action="adopt")
    _set(**STARTS)

    async def someone_else_adopts():
        # Another conversation took it over first (the generation moved).
        await asyncio.to_thread(
            lambda: session_driver.adopt(key, cid="e" * 32, generation=0, title="")
        )

    _before_post(monkeypatch, "/driver/adopt", someone_else_adopts)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale", row
    assert session_driver.record(key)["cid"] == "e" * 32


# ---- the effective class ------------------------------------------------------------------------


def _bypass_on(key):
    asyncio.run(runtime.enable_bypass(key, operation_id=ident(), user="admin"))


def test_starts_act_never_sends_into_a_session_that_skips_permission_prompts(
    app, ai, host, project, fleet
):
    key, cid = _driven(app, project)
    _bypass_on(key)
    _set(**STARTS)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("propose_session_change", {"action": "send", "key": key, "message": STEP}, cid="s"),
        call("propose_session_change", {"action": "adopt", "key": key}, cid="a"),
        answer("Proposed."),
    )
    app.ask(cid, "send it")
    assert result(script, "s").get("ran") in (None, False)
    row = card(cid, "session.send")
    assert row["state"] == "pending"
    assert hq_authority.effective_class(row) == "external"
    assert STEP not in users(project)


def test_bypass_on_between_staging_and_the_claim_asks_again(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _bypass_on(key)
    _set(**STARTS)
    kind, out = _act(app, cid, pid)
    assert kind == "waits" and "asks first" in out["reason"], (kind, out)
    assert ask_proposals._row(cid, pid)["state"] == "pending"
    assert STEP not in users(project)


def test_bypass_on_between_the_claim_and_the_boundary_is_refused(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)

    async def flip():
        await runtime.enable_bypass(key, operation_id=ident(), user="admin")

    _before_post(monkeypatch, "/turns", flip)
    kind, row = _act(app, cid, pid)
    assert row["state"] in ("stale", "pending"), row
    assert STEP not in users(project)


def _tap(app, cid, pid):
    """The Commander's tap: the decide route's own runner (`decide_locked`), awaited."""

    async def go():
        from agent_sessions import ask_store

        fd = await asyncio.to_thread(ask_store.try_decide_lock, cid)
        assert fd is not None
        row = await asyncio.to_thread(ask_proposals._row, cid, pid)
        status, _view = await ask_operations.decide_locked(
            cid, row, fd, "confirm", operator=_operator(app), app=app.app
        )
        task = ask_operations._TASKS.get(pid)
        if task is not None:
            await task
        return status

    return asyncio.run(go())


def test_the_commanders_tap_of_an_external_send_goes_through(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _bypass_on(key)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    assert hq_authority.effective_class(ask_proposals._row(cid, pid)) == "external"
    assert _tap(app, cid, pid) == 202
    row = ask_proposals._row(cid, pid)
    assert row["state"] == "done", row
    asyncio.run(settle(key, session_driver.send_operation_id(pid)))
    assert users(project).count(STEP) == 1


# ---- a mission keeps its sessions ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["send", "interrupt", "adopt"])
def test_every_drive_entry_is_refused_on_a_mission_held_session(
    app, ai, host, project, fleet, action
):
    key, cid = _driven(app, project)
    m = missions.create_mission("ship the strings")
    mission_dispatch.fenced_adopt(m["id"], key)
    fleet["cards"] = [card_row(key)]
    script = ai(
        call("find_sessions", {"query": "web shop"}, cid="f"),
        call("propose_session_change", {"action": action, "key": key, "message": STEP}
             if action == "send" else {"action": action, "key": key}, cid="s"),
        answer("No."),
    )  # fmt: skip
    app.ask(cid, "drive it")
    assert "mission holds" in result(script, "s")["error"]


def test_a_mission_adoption_after_staging_is_stale_at_the_boundary(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    m = missions.create_mission("ship the strings")
    # The mission adopts inside the admission window: before HQ's admission.
    _around_admit(monkeypatch, before=lambda k: mission_dispatch.fenced_adopt(m["id"], k))
    kind, row = _act(app, cid, pid)
    assert row["state"] == "stale", row
    assert STEP not in users(project)


def test_hq_admitted_before_a_mission_adoption_runs_and_the_mission_then_holds_it(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    kind, row = _act(app, cid, pid)
    assert row["state"] == "done", row
    m = missions.create_mission("ship the strings")
    mission_dispatch.fenced_adopt(m["id"], key)
    assert session_driver.view(key)["who"] == "mission"
    assert users(project).count(STEP) == 1


# ---- the grant ----------------------------------------------------------------------------------


def test_a_logout_while_the_send_waited_returns_the_card_and_sends_nothing(
    app, ai, host, project, fleet, monkeypatch
):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    _around_admit(monkeypatch, before=lambda k: asyncio.run(hq_authority.logged_out()))
    kind, out = _act(app, cid, pid)
    assert kind == "waits" and "signed out" in out["reason"], (kind, out)
    assert ask_proposals._row(cid, pid)["state"] == "pending"
    assert STEP not in users(project)


def test_authority_lowered_before_the_claim_sends_nothing(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    _set(**STARTS)
    grant = hq_authority.ActGrant.now()
    _set(**ASK)  # lowered after the turn began
    kind, out = _act(app, cid, pid, grant=grant)
    assert kind == "waits", (kind, out)
    assert STEP not in users(project)


def test_a_restart_mid_act_reads_interrupted_and_is_never_sent_again(app, ai, host, project, fleet):
    key, cid = _driven(app, project)
    _set(**ASK)
    pid = _stage(app, ai, fleet, key, cid, message=STEP)
    # A claim whose process died before the route: `deciding` with a free decide lock.
    ask_proposals._update(cid, pid, state="deciding", decided_by="hq", authority_rev=1)
    ask_proposals.recover(cid)
    assert ask_proposals._row(cid, pid)["state"] == "interrupted"
    _set(**STARTS)
    kind, _row = _act(app, cid, pid)
    assert kind == "decided"
    assert STEP not in users(project)
