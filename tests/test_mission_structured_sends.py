# ruff: noqa: F811 — `api`, `host`, `project` are shared fixtures imported below
"""Mission sends reach API sessions as structured turns (#1899, #1273 P3c).

The real path: the relay route → `actuator.deliver` → `mission_structured.send` → the real native
worker over the fake native child (`host`). The mission store, prefs, ledger and native state are
all under `tmp_path` (conftest pins every store; `host` pins native state).
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import (
    actuator,
    mission_structured,
    missions,
    orchestrator,
    session_input,
)
from agent_sessions import structured_runtime as runtime
from test_missions_api import _create, api  # noqa: F401 — the logged-in client fixture
from test_native_runtime import ENGINES, frames, host, ident, project, settle  # noqa: F401

ENGINE = ENGINES["claude"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


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


def _session(project) -> str:
    return asyncio.run(runtime.create_session(ENGINE, str(project), operation_id=ident()))[
        "session_key"
    ]


def _holding(c, hdr, proj, key):
    from agent_sessions import prefs

    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})  # relays need it on
    m = _create(c, hdr, project_id=proj.id)
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    missions.adopt(m["id"], key)
    return m


def _relay(c, hdr, mid, key, text):
    return c.post(
        f"/api/missions/{mid}/relay", json={"session_key": key, "text": text}, headers=hdr
    )


def test_a_relay_reaches_an_api_session_as_one_guarded_turn(api, host, project):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    r = _relay(c, hdr, m["id"], key, "please look at the logs")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "delivered", r.json()
    assert _users(project).count("please look at the logs") == 1
    snap = asyncio.run(runtime.snapshot(key))
    turn = next(t for t in snap["turns"] if t.get("text") == "please look at the logs")
    assert turn["turn_id"] == mission_structured.action_operation_id(r.json()["action_id"])
    assert turn["context"]["mission_id"] == m["id"]


def test_a_stand_down_between_admission_and_claim_sends_nothing(api, host, project, monkeypatch):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    real_admit = session_input.admit_native

    def admit_then_withdraw(k, check):
        tokens = real_admit(k, check)
        with session_input.sessions_transaction([k]):  # the mission withdraws its authority
            pass
        return tokens

    monkeypatch.setattr(mission_structured.session_input, "admit_native", admit_then_withdraw)
    r = _relay(c, hdr, m["id"], key, "TOO LATE")
    assert r.json().get("state") in {"stale", "failed"}, r.json()
    assert "TOO LATE" not in _users(project)


@pytest.mark.anyio
async def test_a_send_whose_guard_refuses_in_the_fence_is_stale(host, project):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    state, detail = await mission_structured.send(
        action_id="act-1",
        key=key,
        text="REFUSED",
        mission_id="m-1",
        check=lambda: (False, "the mission stood down"),
        precondition=None,
        autonomous=False,
    )
    assert state == "stale" and "stood down" in detail
    assert "REFUSED" not in _users(project)


@pytest.mark.anyio
async def test_an_automatic_send_needs_the_session_as_it_was_proposed(host, project):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    pre = await mission_structured.precondition(key)
    assert pre["api_idle"] is True
    newer = ident()
    await runtime.submit_turn(key, operation_id=newer, text="the operator spoke first")
    await settle(key, newer)
    state, detail = await mission_structured.send(
        action_id="act-2",
        key=key,
        text="STALE NUDGE",
        mission_id="m-1",
        check=lambda: (True, ""),
        precondition=pre,
        autonomous=True,
    )
    assert state == "stale" and "newer turn" in detail
    assert "STALE NUDGE" not in _users(project)


@pytest.mark.anyio
async def test_an_unreadable_proposal_snapshot_fails_closed(host, project, monkeypatch):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]

    async def unreadable(_key):
        raise runtime.StructuredError(503, "unavailable")

    with monkeypatch.context() as m:  # never `undo()`: it drops the host fixture's pins too
        m.setattr(mission_structured.runtime, "snapshot", unreadable)
        pre = await asyncio.to_thread(orchestrator.precondition_for, key)
    state, _ = await mission_structured.send(
        action_id="act-3",
        key=key,
        text="NEVER",
        mission_id=None,
        check=lambda: (True, ""),
        precondition=pre,
        autonomous=True,
    )
    assert state == "stale"
    assert "NEVER" not in _users(project)


@pytest.mark.anyio
async def test_a_busy_session_queues_the_operators_words_but_not_an_automatic_send(host, project):
    key = (await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    await runtime.submit_turn(key, operation_id=ident(), text="WAIT_RELEASE")
    for _ in range(400):
        if (await runtime.snapshot(key))["state"] != "idle":
            break
        await asyncio.sleep(0.05)
    auto, _ = await mission_structured.send(
        action_id="act-auto",
        key=key,
        text="AUTO",
        mission_id=None,
        check=lambda: (True, ""),
        precondition=None,
        autonomous=True,
    )
    mine, detail = await mission_structured.send(
        action_id="act-mine",
        key=key,
        text="MINE",
        mission_id=None,
        check=lambda: (True, ""),
        precondition=None,
        autonomous=False,
    )
    assert auto == "stale"
    assert mine == "delivered" and "queued" in detail


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "want"),
    [(409, "stale"), (422, "stale"), (404, "failed"), (503, "indeterminate")],
)
async def test_each_facade_answer_maps_to_one_ledger_state(
    host, project, monkeypatch, status, want
):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]

    async def answer(*_a, **_kw):
        raise runtime.StructuredError(status, "an answer")

    monkeypatch.setattr(mission_structured.runtime, "submit_turn", answer)
    state, _ = await mission_structured.send(
        action_id=f"act-{status}",
        key=key,
        text="x",
        mission_id=None,
        check=lambda: (True, ""),
        precondition=None,
        autonomous=False,
    )
    assert state == want


@pytest.mark.anyio
async def test_an_uncertain_handoff_is_indeterminate_and_never_sent_again(
    host, project, monkeypatch
):
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    calls = []

    async def uncertain(*_a, **_kw):
        calls.append(1)
        return {"handoff": "uncertain"}

    monkeypatch.setattr(mission_structured.runtime, "submit_turn", uncertain)
    state, _ = await mission_structured.send(
        action_id="act-u",
        key=key,
        text="x",
        mission_id=None,
        check=lambda: (True, ""),
        precondition=None,
        autonomous=False,
    )
    assert state == "indeterminate" and calls == [1]


def test_a_choice_is_never_typed_into_an_api_session():
    with pytest.raises(actuator.NotDeliverable, match="its own conversation"):
        actuator.render_text({"verb": "choose", "option": 1}, {})
    # …while a terminal still gets its keystroke, unchanged.
    assert actuator.render({"verb": "choose", "option": 2}, {}) == b"2\r"


def test_render_and_render_text_type_the_same_words():
    rec = {"verb": "relay", "answer": "same words"}
    assert actuator.render(rec, {}) == session_input.bracketed_paste(actuator.render_text(rec, {}))


# --- through `actuator.deliver`, as every caller reaches it (independent review on #1899) -------


def _action(mid, key, *, text="hello", verb="relay", origin="operator", **extra) -> str:
    import uuid

    from agent_sessions import automation

    grant = automation.capture(key, mid)
    action_id = f"t_{uuid.uuid4().hex}"
    automation.append_operator_action(
        {
            "id": action_id,
            "verb": verb,
            "session_id": key,
            "answer": text,
            "state": "approved",
            "confidence": 1.0,
            "origin": origin,
            "mission_id": mid,
            "authority": grant,
            **extra,
        }
    )
    return action_id


def _auto(_live):
    return True, ""


def _deliver(action_id, **kw):
    return asyncio.run(actuator.deliver(action_id, **kw))


def test_policy_switched_off_before_the_admission_is_stale(api, host, project, monkeypatch):
    from agent_sessions import prefs

    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    real_admit = session_input.admit_native

    def switch_off_then_admit(k, check):
        prefs.set_orchestrator({"enabled": False})
        return real_admit(k, check)

    monkeypatch.setattr(mission_structured.session_input, "admit_native", switch_off_then_admit)
    rec = _deliver(_action(m["id"], key, text="POLICY OFF"))
    assert rec["state"] == "stale", rec
    assert "POLICY OFF" not in _users(project)


def test_an_opted_out_session_takes_nothing(api, host, project):
    from agent_sessions import metadata

    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    metadata.patch(key, orchestrator_excluded=True)
    rec = _deliver(_action(m["id"], key, text="OPTED OUT"))
    assert rec["state"] == "stale" and "no longer managed" in rec["detail"], rec
    assert "OPTED OUT" not in _users(project)


def test_an_automatic_send_without_a_recorded_precondition_is_stale(api, host, project):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    rec = _deliver(_action(m["id"], key, text="NO PRE", origin="pulse"), authority=_auto)
    assert rec["state"] == "stale" and "as it was when proposed" in rec["detail"], rec
    assert "NO PRE" not in _users(project)


def test_an_automatic_send_after_an_operator_turn_is_stale(api, host, project):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    pre = orchestrator.precondition_for(key)
    turn = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=turn, text="operator first"))
    asyncio.run(settle(key, turn))
    rec = _deliver(
        _action(m["id"], key, text="LATE AUTO", origin="pulse", precondition=pre), authority=_auto
    )
    assert rec["state"] == "stale" and "newer turn" in rec["detail"], rec
    assert "LATE AUTO" not in _users(project)


def test_an_automatic_send_into_a_pending_approval_is_stale(api, host, project):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    asyncio.run(runtime.submit_turn(key, operation_id=ident(), text="APPROVE"))
    for _ in range(400):
        if asyncio.run(runtime.snapshot(key))["pending_requests"]:
            break
        import time

        time.sleep(0.05)
    pre = {
        "key": key,
        "api_last_turn": mission_structured.last_operator_turn(asyncio.run(runtime.snapshot(key))),
        "api_idle": True,
    }
    rec = _deliver(
        _action(m["id"], key, text="INTO APPROVAL", origin="pulse", precondition=pre),
        authority=_auto,
    )
    assert rec["state"] == "stale" and "approval or a question" in rec["detail"], rec
    assert "INTO APPROVAL" not in _users(project)


def test_an_operator_relay_into_a_busy_session_queues_but_an_automatic_send_does_not(
    api, host, project
):
    c, hdr, proj = api
    key = asyncio.run(runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    m = _holding(c, hdr, proj, key)
    running = ident()
    asyncio.run(runtime.submit_turn(key, operation_id=running, text="WAIT_RELEASE"))
    for _ in range(400):
        if asyncio.run(runtime.snapshot(key))["state"] != "idle":
            break
        import time

        time.sleep(0.05)
    # Proposed against this very turn while idle (the agent started working after the proposal).
    pre = {"key": key, "api_last_turn": running, "api_idle": True}
    auto = _deliver(
        _action(m["id"], key, text="AUTO BUSY", origin="pulse", precondition=pre),
        authority=_auto,
    )
    mine = _deliver(_action(m["id"], key, text="MINE BUSY"))
    assert auto["state"] == "stale" and "waits for an idle session" in auto["detail"], auto
    assert mine["state"] == "delivered", mine


def test_no_git_runs_inside_the_write_fence(api, host, project, monkeypatch):
    """Independent review on #1899: a supervisor nudge's full guard resolves the checkout target
    with `git`; only its git-free half may run inside `session_input`'s fence."""
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    seen = []

    def fake_render(rec, cfg, *, resolve_target=True):
        seen.append((resolve_target, session_input._lock.locked()))
        return {"text": "nudge text", "digest": "d1"}

    monkeypatch.setattr(actuator, "supervisor_render", fake_render)
    # The text comes from elsewhere here, so every `resolve_target=True` render is the GUARD's.
    monkeypatch.setattr(actuator, "render_text", lambda _a, _c: "nudge text")
    pre = orchestrator.precondition_for(key)
    action = _action(
        m["id"],
        key,
        verb="continue",
        origin="pulse",
        source="supervisor",
        render={"digest": "d1"},
        precondition=pre,
    )
    rec = _deliver(action, operator_approval=True)
    assert rec["state"] == "delivered", rec
    assert (True, True) not in seen, f"git ran inside the fence: {seen}"
    assert (False, True) in seen, f"the in-fence render check never ran: {seen}"
    assert (True, False) in seen, f"the full render (with git) never ran outside the fence: {seen}"


def test_a_callers_authority_makes_even_an_operator_action_automatic(api, host, project):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    rec = _deliver(_action(m["id"], key, text="AUTO OPERATOR", origin="operator"), authority=_auto)
    assert rec["state"] == "stale" and "as it was when proposed" in rec["detail"], rec
    assert "AUTO OPERATOR" not in _users(project)


def test_a_full_guard_that_raises_settles_stale_not_claimed(api, host, project, monkeypatch):
    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)

    def boom():
        raise ValueError("unreadable policy")

    monkeypatch.setattr(actuator, "check_precondition", lambda *_a, **_k: boom())
    rec = _deliver(_action(m["id"], key, text="RAISED"))
    assert rec["state"] == "stale" and "could not be verified" in rec["detail"], rec
    assert "RAISED" not in _users(project)


def test_a_pending_api_decision_survives_the_withdrawal_sweep(api, host, project):
    """Hermes on #1926: an API session has no PTY writer and no dtach socket, so the console
    liveness rule read every healthy API session as dead and withdrew its waiting decisions (a
    `continue` awaiting its tap, an approved draft) at the next sweep. Delivery's own structured
    precondition is what refuses an API send that can no longer succeed."""
    from agent_sessions import orchestrator_ledger as ledger

    c, hdr, proj = api
    key = _session(project)
    m = _holding(c, hdr, proj, key)
    waiting = _action(m["id"], key, text="AWAITING TAP")
    assert ledger.get(waiting)["state"] in actuator.CLAIMABLE_STATES
    expired, withdrawn = actuator.housekeep_pending()
    assert waiting not in withdrawn and waiting not in expired, ledger.get(waiting)
    assert ledger.get(waiting)["state"] == "approved"
    # …and it still delivers as one guarded turn.
    rec = _deliver(waiting)
    assert rec["state"] == "delivered", rec


@pytest.mark.anyio
async def test_a_send_that_cannot_be_identified_is_stale_never_raises(host, project):
    """Hermes on #1926 (non-blocking): the guard and the operation id are built before anything is
    sent, so a malformed binding or action id settles `stale` instead of raising past the claim."""
    key = (await runtime.create_session(ENGINE, str(project), operation_id=ident()))["session_key"]
    for mission_id, action_id in (("bad id\n", "a1"), ("msn_ok", "")):
        state, detail = await mission_structured.send(
            action_id=action_id,
            key=key,
            text="NEVER",
            mission_id=mission_id,
            check=lambda: (True, ""),
            precondition=None,
            autonomous=False,
        )
        assert state == "stale" and "could not be identified" in detail
    assert "NEVER" not in _users(project)
