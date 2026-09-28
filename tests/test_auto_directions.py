"""Autonomous AI-written directions — the one approved broad-permission grant (#983 P4).

Every other phase of #983 keeps one property: **a model never authors the bytes typed into a
permission-bypassed agent**. This phase deliberately gives that up, and only inside the grant the
operator wrote on the issue — *"I approve the autonomous AI directions, YOLO only, threshold
0.90"*. So these tests pin the GRANT'S EDGES far harder than its happy path:

* **off by default**, at every tier and every confidence — the P3 behaviour, unchanged;
* **YOLO only**, and leaving YOLO turns it off *durably*, not just for the current read;
* **0.90 is a floor, not a default**: it can be raised, and a patch that lowers it is a 422;
* a draft below the threshold, or with no usable confidence, stays the P3 proposal;
* **the operator's own direction always wins**, before the claim and inside the write fence;
* **one autonomous send per objective episode**, counted from RECORDED provenance so that turning
  the mode off afterwards cannot un-spend it;
* **withdrawal after the guard** — the real barrier, exercised through a genuine window, including
  the sibling-instance case only the in-fence fingerprint can catch;
* a hand-edited prefs file cannot enable any of it;
* `choose` / `answer` / `dispatch` stay approval-only even with the pref on.

Delivery tests write to a real pty and assert on the bytes at the fd.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import tty

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    engines,
    handoff,
    metadata,
    missions,
    notifications,
    prefs,
    session_input,
)
from agent_sessions import mission_supervisor as sup
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app
from automation_helpers import current_action

SESSION = "claude:11111111-1111-1111-1111-111111111111"
KEY = "review"
DRAFT = "Add a regression test for the retry backoff next to the upload tests, run it, and push."


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_METADATA", str(tmp_path / "meta.json"))
    monkeypatch.setenv("HOME", str(tmp_path))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    session_input.reset()
    yield tmp_path
    session_input.reset()
    missions.reset_schema_cache_for_test()


def _on(threshold: float = 0.9) -> None:
    """The operator's opt-in, exactly as the settings panel writes it."""
    prefs.set_orchestrator(
        {
            "enabled": True,
            "autonomy": "yolo",
            "auto_ai_directions": True,
            "ai_direction_confidence_min": threshold,
        }
    )


def _mission() -> str:
    mid = missions.create_mission("ship the upload retry", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, SESSION)
    return mid


def _add(mid, key=KEY, *, direction=None, probe="forge_review"):
    op = {"op": "add", "key": key, "title": key, "probe": probe, "gate": True}
    if direction is not None:
        op["direction"] = direction
    missions.patch_objectives(mid, [op])


@contextlib.contextmanager
def _live(monkeypatch, *, excluded=False, screen="› waiting"):
    """A real pty registered as the session's writer, so delivery writes to a kernel fd."""
    metadata.patch(SESSION, orchestrator_excluded=excluded)
    monkeypatch.setattr(actuator.scrollback, "live_tail_text", lambda *a, **k: screen)
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(
        engines.physical_key(SESSION), master, threading.Lock(), "attached"
    )
    try:
        yield slave
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def _typed(slave) -> bytes:
    os.set_blocking(slave, False)
    out = b""
    with contextlib.suppress(BlockingIOError, OSError):
        while True:
            chunk = os.read(slave, 65536)
            if not chunk:
                break
            out += chunk
    return out


def _identity(mid, key=KEY) -> dict:
    snap = missions.objective_snapshot(mid, key)
    assert snap is not None, key
    return {
        "expect_episode": int(snap["episode"]),
        "expect_incarnation": str(snap["incarnation"]),
    }


async def _propose(mid, *, confidence, key=KEY, text=DRAFT, session=SESSION):
    """One draft through the shipped path, auto-sent iff the opt-in says so."""
    return await sup.propose_draft(
        mid,
        session_key=session,
        objective_key=key,
        text=text,
        confidence=confidence,
        **_identity(mid, key),
    )


def _drafts(state_in=None) -> list[dict]:
    rows = [r for r in ledger.latest_by_id().values() if r.get("verb") == sup.DRAFT_VERB]
    return [r for r in rows if state_in is None or r.get("state") in state_in]


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return c.get("/api/config").json()["csrf"]


# ---- off by default --------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("confidence", [0.0, 0.9, 0.99, 1.0])
@pytest.mark.parametrize("autonomy", ["off", "suggest", "yolo"])
async def test_a_default_install_never_auto_sends_a_draft(env, monkeypatch, autonomy, confidence):
    """THE DEFAULT IS THE P3 GUARANTEE. No tier and no confidence sends model prose until the
    operator opts in — the opt-in is the whole of the difference."""
    prefs.set_orchestrator({"enabled": True, "autonomy": autonomy})
    assert prefs.get_orchestrator()["auto_ai_directions"] is False
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=confidence)
        typed = _typed(slave)
    assert res["proposed"] is True and "auto_sent" not in res, res
    assert typed == b"", "model-authored text was typed with the opt-in off"
    assert [d["state"] for d in _drafts()] == ["proposed"]


# ---- the tier and the floor ------------------------------------------------------------------


def test_the_opt_in_is_refused_outside_yolo_and_cleared_when_yolo_is_left(env):
    """YOLO-only, and *durably*: a tier round-trip must not silently re-arm a mode the operator
    switched away from. Clamping on read alone would do exactly that."""
    assert prefs.validate_orchestrator_patch({"auto_ai_directions": True}) is not None
    assert (
        prefs.validate_orchestrator_patch({"auto_ai_directions": True, "autonomy": "yolo"}) is None
    )
    _on()
    assert prefs.get_orchestrator()["auto_ai_directions"] is True
    prefs.set_orchestrator({"autonomy": "suggest"})
    assert prefs.get_orchestrator()["auto_ai_directions"] is False
    prefs.set_orchestrator({"autonomy": "yolo"})
    assert prefs.get_orchestrator()["auto_ai_directions"] is False, "returning to yolo re-armed it"


def test_the_threshold_floor_is_the_approved_one_and_cannot_be_lowered(env):
    """0.90 is not a default to be tuned down — it is the number the operator approved."""
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": 0.89}) is not None
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": 0.9}) is None
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": 1.0}) is None
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": 1.01}) is not None
    # `isinstance(True, int)` is True in Python, so booleans are refused on TYPE.
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": True}) is not None
    assert prefs.validate_orchestrator_patch({"ai_direction_confidence_min": "0.95"}) is not None
    # …and a hand-edited file below the floor is clamped back to it on read.
    p = env / ".config" / "agent-sessions" / "prefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"orchestrator": {"ai_direction_confidence_min": 0.1}}))
    assert prefs.get_orchestrator(p)["ai_direction_confidence_min"] == 0.9


def test_the_string_false_does_not_enable_the_opt_in(env):
    """A truthy string is the classic way a hand-written payload arms a boolean."""
    assert prefs.validate_orchestrator_patch({"auto_ai_directions": "false"}) is not None
    p = env / ".config" / "agent-sessions" / "prefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"orchestrator": {"autonomy": "yolo", "auto_ai_directions": "false"}}))
    assert prefs.get_orchestrator(p)["auto_ai_directions"] is False


def test_the_prefs_route_enforces_the_grant(env, auth_cfg, fake_jsonl):  # noqa: ARG001
    """The same rules over HTTP, where an operator (or a stale client) actually reaches them."""
    c = _client(auth_cfg)
    csrf = _login(c, auth_cfg)
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}

    def post(block):
        return c.post("/api/prefs", json={"orchestrator": block}, headers=hdr)

    assert post({"auto_ai_directions": True}).status_code == 422  # not on suggest
    assert post({"ai_direction_confidence_min": 0.89}).status_code == 422
    assert post({"auto_ai_directions": "false"}).status_code == 422
    assert post({"allowed_verbs": ["continue", sup.DRAFT_VERB]}).status_code == 422
    r = post({"autonomy": "yolo", "auto_ai_directions": True})
    assert r.status_code == 200
    body = r.json()["orchestrator"]
    assert body["auto_ai_directions"] is True
    assert body["ai_direction_confidence_min"] == 0.9
    assert sup.DRAFT_VERB in body["auto_verbs_ceiling"], "the UI is not told the ceiling widened"
    assert (
        post({"ai_direction_confidence_min": 0.95}).json()["orchestrator"][
            "ai_direction_confidence_min"
        ]
        == 0.95
    )
    # …and dropping out of yolo through the route clears it, like every other path.
    assert post({"autonomy": "suggest"}).json()["orchestrator"]["auto_ai_directions"] is False


def test_a_hand_edited_prefs_file_cannot_put_the_draft_verb_in_the_ceiling(env, monkeypatch):
    """The verb list never enables itself: the PREF is the authority, and with it absent the
    clamp removes the verb exactly as it removes `answer`."""
    p = env / ".config" / "agent-sessions" / "prefs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(p))
    p.write_text(
        json.dumps(
            {
                "orchestrator": {
                    "enabled": True,
                    "autonomy": "yolo",
                    "allowed_verbs": ["continue", sup.DRAFT_VERB],
                }
            }
        )
    )
    cfg = prefs.get_orchestrator()
    assert cfg["allowed_verbs"] == ["continue"], "the clamp let a draft into the ceiling"
    assert cfg["auto_ai_directions"] is False
    assert actuator.draft_auto_allowed({"verb": sup.DRAFT_VERB, "confidence": 1.0}, cfg) is False


# ---- the threshold ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_draft_below_the_threshold_stays_a_proposal(env, monkeypatch):
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=0.89)
        typed = _typed(slave)
    assert "auto_sent" not in res, res
    assert typed == b""
    assert [d["state"] for d in _drafts()] == ["proposed"]


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [None, "0.99", True, float("nan"), float("inf"), 1.5, -1])
async def test_an_unusable_confidence_is_zero_and_never_auto_deliverable(env, raw):
    """A missing, non-numeric, boolean or non-finite confidence is NOT a low confidence — it is no
    confidence, and 0 is below every threshold the mode can be set to."""
    checklist = {
        "objectives": [
            {"key": KEY, "met": False, "has_direction": False, "episode": 1, "incarnation": "i1"}
        ]
    }
    got = sup._draft_reading({"objective_key": KEY, "text": DRAFT, "confidence": raw}, checklist)
    assert got is not None and got["confidence"] == 0.0, raw
    _on(0.9)
    assert (
        actuator.draft_auto_allowed(
            {"verb": sup.DRAFT_VERB, "confidence": raw}, prefs.get_orchestrator()
        )
        is False
    )


# ---- the happy path, and exactly what it types -----------------------------------------------


@pytest.mark.anyio
async def test_a_draft_at_the_threshold_types_exactly_the_persisted_draft(env, monkeypatch):
    """The bytes are the stored, sanitized draft in one bracketed paste — the same text the card
    would have shown — and the row records that nobody read it."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=0.9)
        typed = _typed(slave)
    assert res.get("auto_sent") == "delivered", res
    assert typed == session_input.bracketed_paste(handoff.sanitize_seed(DRAFT))
    row = _drafts()[0]
    assert row["state"] == "delivered"
    assert row["sent_by"] == "auto", "the delivery did not record HOW it was sent"
    assert row["delivered_text"] == handoff.sanitize_seed(DRAFT)


@pytest.mark.anyio
async def test_paste_terminators_and_control_bytes_are_sanitized(env, monkeypatch):
    """Model prose reaches a real pty here, so an embedded paste terminator must not end the
    bracketed paste early and leave the rest as raw key input."""
    nasty = "run the tests\x1b[201~\x07 and push\x00 now"
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95, text=nasty)
        typed = _typed(slave)
    assert b"\x1b[201~" not in typed[2:-6], "a paste terminator survived into the payload"
    assert b"\x00" not in typed and b"\x07" not in typed
    assert typed == session_input.bracketed_paste(handoff.sanitize_seed(nasty))


def test_over_cap_text_is_dropped_never_truncated(env):
    """A shortened draft is text nobody wrote, so it is refused rather than trimmed."""
    checklist = {
        "objectives": [
            {"key": KEY, "met": False, "has_direction": False, "episode": 1, "incarnation": "i1"}
        ]
    }
    over = "x" * (sup.DRAFT_MAX + 1)
    assert (
        sup._draft_reading({"objective_key": KEY, "text": over, "confidence": 1.0}, checklist)
        is None
    )
    at_cap = "y" * sup.DRAFT_MAX
    got = sup._draft_reading({"objective_key": KEY, "text": at_cap, "confidence": 1.0}, checklist)
    assert got is not None and got["text"] == at_cap


# ---- the operator's own text wins ------------------------------------------------------------


@pytest.mark.anyio
async def test_an_objective_with_your_own_direction_is_never_auto_sent(env, monkeypatch):
    """Operator text always wins: a draft is not even minted for an objective that has one."""
    _on(0.9)
    mid = _mission()
    _add(mid, direction="Open the failing check, fix the cause, push.")
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert res["proposed"] is False, res
    assert typed == b""


@pytest.mark.anyio
async def test_a_direction_written_after_the_guard_refuses_the_write(env, monkeypatch):
    """…and one written DURING the delivery refuses it at the fence, before byte one."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    real = actuator.check_precondition

    def then_write_a_direction(*a, **k):
        out = real(*a, **k)
        missions.patch_objectives(
            mid, [{"op": "set_direction", "key": KEY, "direction": "Do it my way instead."}]
        )
        return out

    monkeypatch.setattr(actuator, "check_precondition", then_write_a_direction)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"", "the AI's words were typed over a direction the operator had just written"
    assert res.get("auto_sent") != "delivered", res


# ---- one per episode -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_autonomous_send_per_objective_episode(env, monkeypatch):
    """The AI-text budget. A second draft in the same episode is a proposal, whatever its
    confidence; a NEW episode is eligible again."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        first = await _propose(mid, confidence=1.0)
        after_first = _typed(slave)
        second = await _propose(mid, confidence=1.0)
        after_second = _typed(slave)
    assert first.get("auto_sent") == "delivered", first
    assert after_first != b""
    assert "auto_sent" not in second, second
    assert after_second == b"", "a second AI-written direction was typed in one episode"
    b = sup.budget_state(mid, KEY)
    assert b["ai_sent"] == 1 and b["spent"] == 1


@pytest.mark.anyio
async def test_turning_the_mode_off_afterwards_does_not_un_spend_the_budget(env, monkeypatch):
    """The budget reads the RECORDED provenance, so it cannot be reset by a later pref change."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch):
        await _propose(mid, confidence=1.0)
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1
    prefs.set_orchestrator({"autonomy": "suggest"})
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1, "the allowance reset itself"


def test_an_action_compaction_has_dropped_still_counts_against_the_budget(env):
    """Fail closed: a bound action with no ledger row may well have been an autonomous send, so
    the allowance is charged rather than handed back."""
    mid = _mission()
    _add(mid)
    missions.record_supervisor_action(
        mid, session_key=SESSION, objective_key=KEY, episode=1, action_id="gone"
    )
    b = sup.budget_state(mid, KEY, episode=1)
    assert b["ai_sent"] == 1 and b["indeterminate"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("tap", [True, False])
async def test_how_a_draft_was_sent_is_derived_from_what_made_it_deliverable(env, monkeypatch, tap):
    """`sent_by` must not key on HOW the delivery was triggered.

    A draft is deliverable on exactly two grounds, and `deliver`'s own guard admits nothing else:
    the operator's tap, or the opt-in. So a delivery that is not a tap is autonomous WHOEVER called
    it. Keying the record on the `authority` argument instead would let a caller that passes none —
    a retry, a sweep, a route added later — deliver a draft that is recorded as operator-sent: the
    thread row would name the wrong author and the AI-text budget would go uncharged, which is
    exactly the accounting error that hands one episode a second unreviewed write.

    Today the only callers are the approve route (a tap) and `deliver_auto` (which passes an
    authority), so this pins the record correct against a caller that does not exist yet.
    """
    # A current proposal under the opt-in. Hold only the automatic dispatch, so this test
    # exercises direct delivery without changing (and thereby invalidating) its original grant.
    _on(0.9)

    async def held_auto(*args, **kwargs):
        return None

    monkeypatch.setattr(sup, "_maybe_auto_send", held_auto)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=0.95)
        assert "auto_sent" not in res, res
        aid = res["id"]
        # NEITHER the approve route NOR `deliver_auto`: no `authority` is passed at all.
        await actuator.deliver(aid, operator_approval=tap)
        typed = _typed(slave)
    assert typed == session_input.bracketed_paste(handoff.sanitize_seed(DRAFT))
    row = ledger.get(aid)
    assert row["state"] == "delivered"
    assert row["sent_by"] == ("operator" if tap else "auto")
    # …and only the autonomous one spends the episode's one AI-written send.
    assert sup.budget_state(mid, KEY)["ai_sent"] == (0 if tap else 1)


# ---- withdrawal after the guard, through a genuine window ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["through_the_api", "by_a_sibling_instance"])
async def test_withdrawing_the_opt_in_after_the_guard_refuses_the_write(env, monkeypatch, how):
    """THE REAL BARRIER.

    `_final_guard` runs before the registry lock, and the payload is written inside it — so a
    verdict formed in the guard is already history by the time the bytes go out. The flip happens
    in the LAST thing the guard does, which is a genuine window: `send_input` is the real function
    and the fd is a real pty.

    `by_a_sibling_instance` writes the prefs FILE directly, without
    `session_input.policy_transaction`, so the process-local policy epoch never moves. Nothing but
    the in-fence policy FINGERPRINT can catch that one, which is exactly why it exists.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    prefs_path = prefs._default_path()
    real = actuator.check_precondition
    calls: list[int] = []

    def then_withdraw(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        # ON THE SECOND CALL ONLY, and that is the whole point of this test.
        #
        # `send_input` invokes the precondition TWICE: once on its own, BEFORE `_final_guard`, and
        # once as the last thing the guard itself does. Withdrawing on the first call lands before
        # the guard reads policy, so the GUARD refuses and the fingerprint is never consulted — the
        # test would pass against a fingerprint that had forgotten this pref entirely (proved by
        # mutation: making `_policy_fingerprint` constant left it green). Withdrawing on the second
        # call puts the change after every check the guard makes and before byte one, which is the
        # window only the in-fence fingerprint comparison covers.
        if len(calls) < 2:
            return out
        if how == "through_the_api":
            prefs.set_orchestrator({"auto_ai_directions": False})
        else:
            doc = json.loads(prefs_path.read_text())
            doc["mission_orchestration"]["auto_ai_directions"] = False
            prefs_path.write_text(json.dumps(doc))
        return out

    monkeypatch.setattr(actuator, "check_precondition", then_withdraw)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"", f"typed on the authority of a mode the operator had switched off ({how})"
    assert res.get("auto_sent") != "delivered", res
    assert _drafts()[0]["state"] != "delivered"


SESSION_B = "claude:22222222-2222-2222-2222-222222222222"


@contextlib.contextmanager
def _live_pair(monkeypatch, gate):
    """Two sessions of ONE mission, each with a real pty, each held inside `send_input` by `gate`
    until the other has got there — so both deliveries are past their guards at the same time."""
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(
        actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta(orchestrator_excluded=False)
    )
    monkeypatch.setattr(actuator.scrollback, "live_tail_text", lambda *a, **k: "› waiting")
    real_wait = session_input._wait_quiet

    def wait_then_gate(key, deadline):
        out = real_wait(key, deadline)
        # WELL UNDER `session_input.WRITE_TIMEOUT_S` (5s). The barrier exists to overlap two
        # deliveries; once the bound holds only one caller ever reaches it, so a long timeout would
        # burn the write deadline and the winner would fail for a reason that is not the one under
        # test — which is exactly what a too-patient barrier did here first time round.
        with contextlib.suppress(threading.BrokenBarrierError, threading.ThreadError):
            gate.wait(timeout=1.0)
        return out

    monkeypatch.setattr(session_input, "_wait_quiet", wait_then_gate)
    fds: dict[str, tuple[int, int]] = {}
    for key in (SESSION, SESSION_B):
        master, slave = os.openpty()
        tty.setraw(slave)
        session_input.register_writer(
            engines.physical_key(key), master, threading.Lock(), "attached"
        )
        fds[key] = (master, slave)
    try:
        yield fds
    finally:
        session_input.reset()
        for master, slave in fds.values():
            for fd in (master, slave):
                with contextlib.suppress(OSError):
                    os.close(fd)


def _live_draft(mid, action_id, session, *, confidence=0.95, episode=1):
    """A draft already persisted `proposed` and bound to this objective episode.

    Built directly rather than through two overlapping `propose_draft` calls because the mint-time
    fence (`may_nudge` refusing while another action is live) is a READ followed by a write, and the
    one-send bound has to hold on its own whether or not that read happens to win the race.
    """
    missions.record_supervisor_action(
        mid, session_key=session, objective_key=KEY, episode=episode, action_id=action_id
    )
    rec = {
        "id": action_id,
        "state": "proposed",
        "verb": sup.DRAFT_VERB,
        "source": "supervisor",
        "session_id": session,
        "mission_id": mid,
        "objective_key": KEY,
        "objective_episode": episode,
        "objective_incarnation": missions.objective_incarnation(mid, KEY),
        "draft": DRAFT,
        "confidence": confidence,
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "precondition": {},
    }
    rec = current_action(rec)
    ledger.append(rec)
    return rec


@pytest.mark.anyio
async def test_two_sessions_in_one_episode_cannot_both_send_an_ai_direction(env, monkeypatch):
    """THE ONE-SEND BOUND IS A RESERVATION, not a count of completed sends.

    `budget_state` counts a claimed action as `live`, never as spent, so two supervisor calls on
    DIFFERENT sessions of the same mission and objective episode both read `ai_sent == 0` before
    the claim AND again inside their own write fences — neither has settled yet. The durable
    binding admits `NUDGE_BUDGET` (three), so it does not stop them either. Both then type, and the
    episode gets two unreviewed writes: the exact bound the operator accepted the risk on.

    Driven with two real PTYs and a barrier that holds each delivery inside `send_input` until the
    other has also reached it, so both are past every guard simultaneously.
    """
    _on(0.9)
    mid = _mission()
    missions.adopt(mid, SESSION_B)
    _add(mid)
    a = _live_draft(mid, "d_a", SESSION)
    b = _live_draft(mid, "d_b", SESSION_B)
    gate = threading.Barrier(2)
    with _live_pair(monkeypatch, gate) as fds:
        await asyncio.gather(
            sup._maybe_auto_send(a, mid, objective_key=KEY, episode=1),
            sup._maybe_auto_send(b, mid, objective_key=KEY, episode=1),
        )
        typed = {key: _typed(slave) for key, (_m, slave) in fds.items()}
    delivered = [r for r in (ledger.get("d_a"), ledger.get("d_b")) if r["state"] == "delivered"]
    wrote = {key: body for key, body in typed.items() if body}
    assert len(wrote) == 1, f"both sessions were typed into in one episode: {sorted(wrote)}"
    assert len(delivered) == 1, [r["state"] for r in (ledger.get("d_a"), ledger.get("d_b"))]
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1


def _sibling_set_direction(env, mid: str, key: str, direction: str) -> None:
    """Commit an operator direction from a SEPARATE PROCESS, as another app instance would.

    A sibling cannot move this interpreter's policy epoch, which is exactly why the in-fence
    comparison has to read the shared store rather than trust a local counter.
    """
    code = (
        "from agent_sessions import missions;"
        f"missions.patch_objectives({mid!r},"
        f"[{{'op':'set_direction','key':{key!r},'direction':{direction!r}}}])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_SESSIONS_MISSIONS_DB": str(env / "m.db")},
        timeout=60,
    )
    assert out.returncode == 0, f"the sibling write failed: {out.stderr[-600:]}"


@pytest.mark.anyio
async def test_a_sibling_writing_your_direction_after_the_guard_stops_the_send(env, monkeypatch):
    """OPERATOR TEXT ALWAYS WINS, and it has to win at the WRITE FENCE, not only at the guard.

    `has_direction` was asked in `_maybe_auto_send`'s authority callback and nowhere else: no
    fingerprint carried it. The fallback fingerprint is the supervisor state tuple — state,
    incarnation, episode, binding — which does not include the direction, and the render
    fingerprint covers a `continue`, not a draft. So a `set_direction` committed by ANOTHER
    INSTANCE after the guard ran still lost: this process's policy epoch never moves for a write
    another process made, and the AI-authored text landed in a session the operator had just
    written their own direction for.

    The write happens on the SECOND `check_precondition` call, which is the last thing the guard
    does — the same post-guard window the withdrawal test needed, and the one a first-call mutation
    cannot reach.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    real = actuator.check_precondition
    calls: list[int] = []

    def then_sibling_writes(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) >= 2:
            _sibling_set_direction(env, mid, KEY, "Open the failing check, fix the cause, push.")
        return out

    monkeypatch.setattr(actuator, "check_precondition", then_sibling_writes)
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"", "an AI-written direction was typed over the direction you had just written"
    assert res.get("auto_sent") != "delivered", res


# ---- a re-created objective is a different objective ------------------------------------------


@pytest.mark.anyio
async def test_a_recreated_objective_gets_its_own_allowance(env, monkeypatch):
    """The reservation is per INCARNATION, not per key.

    Dropping an objective resets the episode and clears the supervisor bindings, but a reservation
    keyed only on `(mission, key, episode)` survives — so the same key added back gets a fresh
    incarnation at episode 1 whose allowance already reads as spent, and its draft never sends.
    Fails closed, so it is correctness rather than safety, but it silently disables the feature for
    an objective the operator re-created.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        first = await _propose(mid, confidence=0.95)
        assert first.get("auto_sent") == "delivered", first
        assert _typed(slave) != b""
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1

    # Drop it and add the SAME KEY back: a new incarnation, episode 1 again.
    missions.patch_objectives(mid, [{"op": "drop", "key": KEY}])
    _add(mid)
    assert sup.budget_state(mid, KEY)["ai_sent"] == 0, "a re-created objective inherited the spend"
    with _live(monkeypatch) as slave:
        again = await _propose(mid, confidence=0.95)
        typed = _typed(slave)
    assert again.get("auto_sent") == "delivered", again
    assert typed != b"", "the re-created objective could never use its own allowance"


def _plant_stale_reservation(env, mid, incarnation, *, key=KEY, episode=1, action_id="old") -> None:
    """Put a reservation row back under an OLD incarnation, as if the drop had not cleared it."""
    con = sqlite3.connect(env / "m.db")
    try:
        con.execute(
            "INSERT OR REPLACE INTO mission_ai_directions "
            "(mission_id, objective_key, incarnation, episode, action_id, at) VALUES (?,?,?,?,?,?)",
            (mid, key, incarnation, episode, action_id, time.time()),
        )
        con.commit()
    finally:
        con.close()


@pytest.mark.anyio
async def test_a_reservation_from_an_older_incarnation_cannot_bind_the_new_one(env, monkeypatch):
    """The incarnation is part of the reservation KEY, and that is what makes a stale row inert.

    `_forget_objective` also deletes the row when an objective is dropped, so the ordinary
    drop-and-re-add path is covered twice over — and a mutation of the key alone leaves that test
    green, because the delete carries it. This pins the half that does NOT depend on the delete
    having run: a row left behind by an earlier incarnation, whether by a partial failure or by an
    older build that never deleted it, must not bind the objective holding the key now.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    stale = missions.objective_incarnation(mid, KEY)
    assert missions.reserve_ai_direction(
        mid, objective_key=KEY, episode=1, action_id="old", expect_incarnation=stale
    )
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1

    missions.patch_objectives(mid, [{"op": "drop", "key": KEY}])
    _add(mid)
    fresh = missions.objective_incarnation(mid, KEY)
    assert fresh != stale, "a re-added key must be a new incarnation"
    _plant_stale_reservation(env, mid, stale)

    assert (
        missions.ai_direction_holder(mid, KEY, 1) is None
    ), "a reservation from a previous incarnation bound the objective that reuses its key"
    assert sup.budget_state(mid, KEY)["ai_sent"] == 0
    # …and the new incarnation can still take its own, beside the stale row.
    assert missions.reserve_ai_direction(
        mid, objective_key=KEY, episode=1, action_id="new", expect_incarnation=fresh
    )
    assert missions.ai_direction_holder(mid, KEY, 1) == "new"


# ---- the announcement outlives the display ring ----------------------------------------------


def _auto_notes() -> list[dict]:
    return [
        n
        for n in notifications._read(notifications._notifications_path())
        if n.get("auto_direction") is True
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["dismissed", "evicted"])
async def test_a_cleared_announcement_is_not_recreated_by_the_next_sweep(env, monkeypatch, how):
    """The dedupe receipt must not live in the DISPLAY ring.

    `dismiss()` removes the row and `_evict()` drops it at the 200-row cap, so the receipt the
    repair path checks disappears with it — and the next reconciliation announces the same
    delivered draft again, as a NEW unread row. Clearing an announcement would be undone by the
    next sweep, and a busy install would recycle old ones indefinitely.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch):
        await _propose(mid, confidence=0.95)
    assert len(_auto_notes()) == 1
    if how == "dismissed":
        notifications.dismiss([_auto_notes()[0]["id"]])
    else:
        for i in range(notifications.NOTIFY_MAX + 5):
            notifications.add(
                title=f"filler {i}", project="", session_id=f"claude:f{i}", engine="claude"
            )
    cleared = len(_auto_notes())
    actuator.reconcile_delivered_nudges()
    assert len(_auto_notes()) == cleared, "a cleared announcement came back as unread"


@pytest.mark.anyio
async def test_a_lagging_announcer_cannot_resurrect_a_dismissed_notification(env, monkeypatch):
    """The dedupe read and the bell write must be ONE serialized step.

    Deciding BEFORE entering the notification store's lock leaves a window the sequential guard
    cannot see, because it is not sequential: a lagging announcer reads "not announced" and pauses,
    a peer announces and records, the operator dismisses the row, and the lagging one resumes and
    writes a NEW unread notification having never re-read the identity. Overlapping delivery and
    repair, or two sweeps on two instances, produce exactly this.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    # Deliver with the bell suppressed, so one announcement is owed and no receipt exists yet.
    real_announce = actuator._announce_auto_direction

    def boom(*a, **k):
        raise OSError("the bell was unwritable")

    monkeypatch.setattr(actuator, "_announce_auto_direction", boom)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    monkeypatch.setattr(actuator, "_announce_auto_direction", real_announce)
    assert _auto_notes() == []

    rec = _drafts()[0]
    entered, go = threading.Event(), threading.Event()
    # `add` reads its predecessor through the STRICT loader since #1086 Phase 4 (Hermes 5239: a
    # mutation starts from one checked snapshot), so that is where the dedupe read — and this hook —
    # lives now.
    real_load = notifications._load_doc_strict
    who: list = []
    seen: list[int] = []
    errors: list[BaseException] = []

    def lagging(path):
        """Hold the FIRST announcer inside the store lock, right after it reads the tombstones.

        Hooked on the strict loader because that is where the dedupe decision is now made —
        inside `add`'s lock — rather than on a receipt read taken before it. Gated on the
        announcer thread's FIRST read, so the peer's reads and `_write`'s own internal load are
        untouched.
        """
        out = real_load(path)
        if who and threading.current_thread() is who[0] and not seen:
            seen.append(1)
            entered.set()
            go.wait(timeout=15)
        return out

    monkeypatch.setattr(notifications, "_load_doc_strict", lagging)

    # EVERY ROW EVER CREATED FOR THIS ACTION, not the rows left at the end.
    #
    # The first version of this test asserted that no auto row SURVIVED, and that assertion cannot
    # fail: the peer dismisses every auto row it can see, so a duplicate created before the
    # dismissal is cleaned up by the dismissal itself. Disabling both the tombstone and the ring
    # dedupe left it green — a test measuring the wrong thing. Counting distinct row ids as they
    # are written, under the store lock, measures what the guarantee is actually about.
    created: set[str] = set()
    real_write = notifications._write

    def watching_write(path, rows, **kw):
        for r in rows:
            if r.get("auto_direction") is True and r.get("action_id") == rec["id"]:
                created.add(str(r.get("id")))
        return real_write(path, rows, **kw)

    monkeypatch.setattr(notifications, "_write", watching_write)

    def announcer():
        who.append(threading.current_thread())
        try:
            actuator._announce_auto_direction(rec)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    peer_started = threading.Event()

    def peer_then_dismiss():
        peer_started.set()
        try:
            actuator._announce_auto_direction(rec)
            for n in _auto_notes():
                notifications.dismiss([n["id"]])
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    a = threading.Thread(target=announcer)
    a.start()
    assert entered.wait(timeout=15), "the lagging announcer never read the receipt"
    b = threading.Thread(target=peer_then_dismiss)
    b.start()
    # A PEER-PROGRESS HANDSHAKE, not a timed sleep. A `sleep` here would be a wall-clock bet on a
    # shared runner — the kind of flake that gets blamed on somebody else's diff months later.
    assert peer_started.wait(timeout=15), "the peer announcer never started"
    go.set()
    a.join(timeout=30)
    b.join(timeout=30)
    assert not a.is_alive() and not b.is_alive(), "an announcer thread never terminated"
    assert not errors, errors
    assert (
        len(created) == 1
    ), f"the same send was announced {len(created)} times across two overlapping announcers"
    assert _auto_notes() == [], "a lagging announcer resurrected a dismissed notification"


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["dismissed", "evicted"])
async def test_a_failed_receipt_write_cannot_let_repair_undo_a_dismissal(env, monkeypatch, how):
    """THE ROW AND THE DEDUPE IDENTITY MUST LAND TOGETHER.

    The visible row is committed first, deliberately — writing the receipt first loses
    announcements whenever the bell write fails. But if the receipt commit then fails, or the
    process dies between the two, the row exists and the receipt does not. `dismiss()` is a DELETE
    and keeps nothing of the row's action identity, and `_evict` drops it at the cap, so the next
    repair announces the same delivery again as a NEW unread row.

    No lock closes this: two commits in two stores cannot be made atomic by one. The identity has
    to be written in the SAME store transaction as the row, or outlive the row by construction.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    real_record = missions.record_auto_announcement

    def boom(*a, **k):
        raise OSError("the receipt could not be committed")

    monkeypatch.setattr(missions, "record_auto_announcement", boom)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    # The operator-visible row exists; the receipt does not. Exactly the torn state.
    notes = _auto_notes()
    assert len(notes) == 1, notes
    aid = str(notes[0]["action_id"])
    monkeypatch.setattr(missions, "record_auto_announcement", real_record)
    assert not missions.auto_announcement_recorded(aid), "the fixture failed to lose the receipt"

    if how == "dismissed":
        notifications.dismiss([notes[0]["id"]])
    else:
        for i in range(notifications.NOTIFY_MAX + 5):
            notifications.add(
                title=f"filler {i}", project="", session_id=f"claude:f{i}", engine="claude"
            )
    cleared = len(_auto_notes())
    actuator.reconcile_delivered_nudges()
    assert (
        len(_auto_notes()) == cleared
    ), "a failed receipt write let the repair resurrect an announcement the operator cleared"


@pytest.mark.anyio
async def test_a_failed_receipt_converges_and_the_pin_releases(env, monkeypatch):
    """A TOMBSTONE HIT MUST REPAIR THE RECEIPT, not merely suppress the announcement.

    Returning early on a tombstone hit left the SQLite receipt permanently unwritten whenever its
    first write failed. `_preserve_deliveries` then pinned the delivered action FOR EVER — pinned
    rows are exempt from `HISTORY_MAX`, so "a while longer" was simply false — and that retained
    replay source outlives the tombstone, which is reclaimed FIFO after `ANNOUNCED_MAX` newer
    announcements. Row gone, tombstone gone, ledger row still pinned: the dismissed announcement
    returns with a new id and `read: false`.

    Two dedupe authorities that can diverge is the root cause. Convergence is the fix, and a
    tombstone must not be reclaimed while its own replay source is still pinned.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    real_record = missions.record_auto_announcement

    def boom(*a, **k):
        raise OSError("the receipt could not be committed")

    monkeypatch.setattr(missions, "record_auto_announcement", boom)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    notes = _auto_notes()
    assert len(notes) == 1, notes
    aid = str(notes[0]["action_id"])
    monkeypatch.setattr(missions, "record_auto_announcement", real_record)
    assert not missions.auto_announcement_recorded(aid), "the fixture failed to lose the receipt"

    # RECOVERY. A sweep must converge the receipt, and must not recreate the bell row to do it.
    actuator.reconcile_delivered_nudges()
    assert missions.auto_announcement_recorded(
        aid
    ), "a tombstone hit suppressed the announcement and never repaired the receipt"
    assert len(_auto_notes()) == 1, "the repair recreated the operator's row"

    # The operator clears it.
    notifications.dismiss([notes[0]["id"]])
    assert _auto_notes() == []

    # THE PIN MUST RELEASE once the receipt exists, so the replay source can age out normally.
    ledger.compact(history_max=0)
    assert (
        ledger.get(aid) is None
    ), "the delivered action stayed pinned even though its receipt had converged"

    # …and tombstone rollover must not resurrect it either way.
    for i in range(notifications.ANNOUNCED_MAX + 5):
        notifications.add(
            title=f"filler {i}",
            project="",
            session_id=f"claude:f{i}",
            engine="claude",
            action_id=f"act{i}",
            auto_direction=True,
        )
    actuator.reconcile_delivered_nudges()
    assert [
        n for n in _auto_notes() if n.get("action_id") == aid
    ] == [], "a dismissed announcement came back after its tombstone rolled over"


def _fill_owed(n: int, boom) -> set[str]:
    """`n` announcements whose receipt always fails, and which can never be reclaimed.

    `record` is passed because production always passes it (`actuator._announce_auto_direction`):
    without it `_remember` has no way to converge a candidate and so declines to reclaim anything,
    which silently turns every test built on this helper into a test of a disabled eviction path.
    """
    ids = {f"owed{i}" for i in range(n)}
    for i in range(n):
        notifications.add(
            title=f"filler {i}",
            project="",
            session_id=f"claude:f{i}",
            engine="claude",
            action_id=f"owed{i}",
            auto_direction=True,
            after=boom,
            record=lambda _aid: False,
        )
    return ids


def _unwritable(*a, **k):
    raise OSError("the missions store is unwritable")


def _fill_healthy(n: int, mid: str) -> set[str]:
    """`n` announcements against a WRITABLE receipt store, driven as production drives them.

    The distinction from `_fill_owed` is the one that matters, and conflating them made a test
    vacuous: `after` writes THIS announcement's receipt, while `record(aid)` makes an arbitrary
    CANDIDATE's receipt durable during reclamation. A filler whose `record` always refuses blocks
    reclamation of every older identity, so nothing is ever dropped and the receipt fallback the
    test exists to exercise is never reached.
    """
    ids = {f"ok{i}" for i in range(n)}
    for i in range(n):
        notifications.add(
            title=f"filler {i}",
            project="",
            session_id=f"claude:h{i}",
            engine="claude",
            action_id=f"ok{i}",
            auto_direction=True,
            after=(lambda a=f"ok{i}": actuator._ensure_receipt(a, mid)),
            record=lambda aid: actuator._ensure_receipt(aid, mid),
        )
    return ids


@pytest.mark.anyio
async def test_a_new_identity_is_not_evicted_by_a_full_set_of_owed_ones(env, monkeypatch):
    """THE IDENTITY BEING INSERTED must be safe before eviction runs.

    Eviction that skips owed identities, with the cap already full of them, has nothing else left
    to drop and so evicts the entry it has just appended — the one whose row is being written right
    now. Its receipt then fails too, so it is remembered nowhere: dismiss, reconcile, and it
    returns.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    fillers = _fill_owed(notifications.ANNOUNCED_MAX, _unwritable)

    monkeypatch.setattr(missions, "record_auto_announcement", _unwritable)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    mine = [n for n in _auto_notes() if str(n.get("action_id")) not in fillers]
    assert len(mine) == 1, mine
    aid = str(mine[0]["action_id"])

    notifications.dismiss([mine[0]["id"]])
    actuator.reconcile_delivered_nudges()
    assert [
        n for n in _auto_notes() if n.get("action_id") == aid
    ] == [], "the identity being inserted was evicted by a full set of owed ones, and came back"


@pytest.mark.anyio
async def test_a_converged_delivery_survives_tombstone_reclamation(env, monkeypatch):
    """A SUCCESSFUL RECEIPT IS THE DURABLE AUTHORITY, so reclaiming its tombstone changes nothing.

    Under pressure, reclamation converges each candidate and drops the ones it can make durable —
    so a healthy, recent delivery IS eventually dropped from the dedupe list. Everything that stops
    it being announced a second time then rests on the receipt, and if the announce path does not
    consult that receipt, the delivery comes back the moment its tombstone goes.

    Fails for a different reason than the test above: that one is about WHICH entry reclamation
    chooses, this one about whether anything durable is consulted once the entry is gone. The
    fillers must be HEALTHY for that to be true — a filler whose `record` refuses would block
    reclamation of this identity entirely, and the test would pass without ever reaching the
    receipt.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    notes = _auto_notes()
    assert len(notes) == 1, notes
    aid = str(notes[0]["action_id"])
    assert missions.auto_announcement_recorded(aid), "this delivery's receipt should have converged"

    notifications.dismiss([notes[0]["id"]])
    assert _auto_notes() == []
    _fill_healthy(notifications.ANNOUNCED_MAX, mid)
    assert (
        aid not in notifications._load(notifications._notifications_path())[1]
    ), "the fixture must actually reclaim this delivery's tombstone, or the receipt is never asked"

    actuator.reconcile_delivered_nudges()
    assert [
        n for n in _auto_notes() if n.get("action_id") == aid
    ] == [], "a converged delivery was re-announced once its tombstone was reclaimed"


@pytest.mark.anyio
async def test_a_transient_receipt_failure_survives_a_repair_backlog(env, monkeypatch):
    """THE RESIDUAL IS REACHABLE WITH A HEALTHY STORE, which is why it is not a residual.

    The intervening announcements need not be new autonomous sends. `reconcile_delivered_nudges`
    visits every retained delivery once per sweep, and the compaction pin holds unannounced ones
    beyond ordinary retention — so an earlier BELL-store outage leaves a large repair backlog while
    missions writes are perfectly healthy. One transient receipt failure early in that batch, with
    the writer recovering immediately and every later receipt succeeding, still rolled the identity
    out by the end of it, and the dismissed announcement came back.

    So reclamation must first make the candidate's receipt durable, and refuse to reclaim it when
    it cannot. Here the writer HAS recovered, so the identity is legitimately reclaimable — but
    only because converging it is what made it so, which the second assertion pins.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    real_record = missions.record_auto_announcement

    monkeypatch.setattr(missions, "record_auto_announcement", _unwritable)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    notes = _auto_notes()
    assert len(notes) == 1, notes
    aid = str(notes[0]["action_id"])

    # THE WRITER RECOVERS IMMEDIATELY — every later receipt in the batch succeeds.
    monkeypatch.setattr(missions, "record_auto_announcement", real_record)
    assert not missions.auto_announcement_recorded(aid), "the fixture failed to lose the receipt"
    notifications.dismiss([notes[0]["id"]])
    assert _auto_notes() == []

    # The repair backlog: healthy announcements, each with a durable receipt of its own, and each
    # carrying the same `record` production passes — so reclamation genuinely runs under pressure.
    for i in range(notifications.ANNOUNCED_MAX + 5):
        notifications.add(
            title=f"repair {i}",
            project="",
            session_id=f"claude:r{i}",
            engine="claude",
            action_id=f"rep{i}",
            auto_direction=True,
            record=lambda aid: actuator._ensure_receipt(aid, mid),
        )
        missions.record_auto_announcement(f"rep{i}", mid)

    actuator.reconcile_delivered_nudges()
    assert (
        [n for n in _auto_notes() if n.get("action_id") == aid] == []
    ), "a dismissed announcement returned after a healthy repair backlog rolled its tombstone out"
    assert missions.auto_announcement_recorded(
        aid
    ), "the identity was reclaimed without its receipt ever being made durable"


@pytest.mark.anyio
async def test_a_pending_announcement_survives_compaction(env, monkeypatch):
    """Compaction must not discard an announcement the send still owes.

    The notification write is best-effort and the repair reads the ledger row, so a store failure
    plus a retention boundary loses the obligation for good: the action is gone and no notification
    was ever made. The default retention has the same loss once the row ages out.
    """
    _on(0.9)
    mid = _mission()
    _add(mid)
    real = actuator._announce_auto_direction

    def boom(*a, **k):
        raise OSError("the bell was unwritable")

    monkeypatch.setattr(actuator, "_announce_auto_direction", boom)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.95)
        assert _typed(slave) != b"", "the delivery itself should have happened"
    monkeypatch.setattr(actuator, "_announce_auto_direction", real)
    assert _auto_notes() == []

    ledger.compact(history_max=0)
    actuator.reconcile_delivered_nudges()
    assert len(_auto_notes()) == 1, "compaction discarded an announcement the send still owed"


# ---- the ceiling still holds for everything else ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("verb", ["choose", "answer", "dispatch"])
async def test_the_other_verbs_stay_approval_only_with_the_opt_in_on(env, verb):
    """The grant widens `draft_direction` and nothing else."""
    _on(0.9)
    cfg = prefs.get_orchestrator()
    assert set(cfg["allowed_verbs"]) == {"continue", sup.DRAFT_VERB}
    assert (
        await actuator.deliver_auto({"id": "x", "verb": verb, "confidence": 1.0}) is None
    ), f"{verb} auto-delivered under the AI-direction opt-in"


# ---- every existing fence still refuses ------------------------------------------------------


@pytest.mark.anyio
async def test_an_excluded_session_is_still_refused(env, monkeypatch):
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch, excluded=True) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"" and res.get("auto_sent") != "delivered"


@pytest.mark.anyio
async def test_a_screen_that_moved_since_the_draft_is_refused(env, monkeypatch):
    """The draft was about a screen; a different screen is a different situation."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    seen: list[str] = []

    def moving_screen(*a, **k):
        seen.append("x")
        return "› waiting" if len(seen) <= 1 else "a totally different screen"

    with _live(monkeypatch) as slave:
        monkeypatch.setattr(actuator.scrollback, "live_tail_text", moving_screen)
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"" and res.get("auto_sent") != "delivered"


@pytest.mark.anyio
async def test_an_archiving_mission_accepts_no_autonomous_write(env, monkeypatch):
    _on(0.9)
    mid = _mission()
    _add(mid)
    monkeypatch.setattr(actuator, "check_precondition", lambda *a, **k: (True, ""))
    monkeypatch.setattr(missions, "sessions_barred_from_automation", lambda *a, **k: {SESSION})
    with _live(monkeypatch) as slave:
        res = await _propose(mid, confidence=1.0)
        typed = _typed(slave)
    assert typed == b"" and res.get("auto_sent") != "delivered"


@pytest.mark.anyio
async def test_a_shell_session_is_never_given_an_ai_written_direction(env, monkeypatch):
    """`shell` is an agentless bash, so a direction typed into one is a COMMAND."""
    _on(0.9)
    shell_key = "shell:22222222-2222-2222-2222-222222222222"
    mid = missions.create_mission("a shell", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, shell_key)
    _add(mid)
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(
        actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta(orchestrator_excluded=False)
    )
    monkeypatch.setattr(actuator.scrollback, "live_tail_text", lambda *a, **k: "$ ")
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(
        engines.physical_key(shell_key), master, threading.Lock(), "attached"
    )
    try:
        res = await _propose(mid, confidence=1.0, session=shell_key)
        typed = _typed(slave)
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)
    assert typed == b"", "a direction was typed into an agentless shell"
    assert res.get("auto_sent") != "delivered"


# ---- visibility ------------------------------------------------------------------------------


def _delivered_events(mid) -> list[dict]:
    return [
        e
        for e in missions.get_mission(mid)["events"]
        if e["kind"] == "action" and (e.get("meta") or {}).get("stage") == "delivered"
    ]


@pytest.mark.anyio
async def test_an_auto_send_writes_the_thread_row_and_exactly_one_notification(env, monkeypatch):
    """Every autonomous send is recorded where the operator reads, and announced once."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch):
        await _propose(mid, confidence=0.97)
    rows = _delivered_events(mid)
    assert len(rows) == 1, rows
    meta = rows[0]["meta"]
    assert meta["text_source"] == "ai_auto", meta
    assert meta["auto"] is True
    assert meta["confidence"] == pytest.approx(0.97)
    assert meta["objective_key"] == KEY
    assert rows[0]["text"] == handoff.sanitize_seed(DRAFT)

    notes = notifications._read(notifications._notifications_path())
    auto = [n for n in notes if n.get("auto_direction") is True]
    assert len(auto) == 1, notes
    assert auto[0]["escalation"] is False, "an autonomous send was filed as a decision to make"
    assert auto[0]["action_id"] == rows[0]["action_id"]
    assert DRAFT not in auto[0]["title"], "the bell carried session-derived prose"


@pytest.mark.anyio
async def test_the_repair_path_restores_a_lost_row_and_bell_exactly_once(env, monkeypatch):
    """A failure between the settlement and the two records must not lose them — and the repair
    must never re-send, nor announce twice, nor reset the AI budget."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    real_record = actuator._record_delivered_nudge
    real_announce = actuator._announce_auto_direction

    def boom(*a, **k):
        raise OSError("the store hiccuped after the bytes went out")

    monkeypatch.setattr(actuator, "_record_delivered_nudge", boom)
    monkeypatch.setattr(actuator, "_announce_auto_direction", boom)
    with _live(monkeypatch) as slave:
        await _propose(mid, confidence=0.93)
        typed = _typed(slave)
    assert typed != b"", "the delivery itself should have happened"
    assert _delivered_events(mid) == []
    # Restore by RE-SETTING, never `monkeypatch.undo()`: undo would also roll back the `env`
    # fixture's store paths and point the reconciler at the real ledger, which reports 0 and reads
    # exactly like a repair that did nothing.
    monkeypatch.setattr(actuator, "_record_delivered_nudge", real_record)
    monkeypatch.setattr(actuator, "_announce_auto_direction", real_announce)

    # The ONE repair path, run twice: it writes what is missing and then nothing.
    assert actuator.reconcile_delivered_nudges() == 1
    assert actuator.reconcile_delivered_nudges() == 0
    rows = _delivered_events(mid)
    assert len(rows) == 1 and rows[0]["meta"]["text_source"] == "ai_auto"
    assert rows[0]["meta"]["auto"] is True
    assert rows[0]["meta"]["confidence"] == pytest.approx(0.93)
    auto = [
        n
        for n in notifications._read(notifications._notifications_path())
        if n.get("auto_direction") is True
    ]
    assert len(auto) == 1, auto
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1, "the repair reset the AI-text budget"


@pytest.mark.anyio
async def test_the_recorded_provenance_survives_a_restart(env, monkeypatch):
    """`sent_by` is on the durable row, so a fresh process classifies the send the same way."""
    _on(0.9)
    mid = _mission()
    _add(mid)
    with _live(monkeypatch):
        await _propose(mid, confidence=1.0)
    aid = _drafts()[0]["id"]
    # A new reader over the same file — no in-process state at all.
    reread = ledger.latest_by_id()[aid]
    assert reread["sent_by"] == "auto" and reread["state"] == "delivered"
    time.sleep(0)
    assert sup.budget_state(mid, KEY)["ai_sent"] == 1
