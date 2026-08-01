"""Orchestrator actuation — the input seam, compare-and-execute, crash semantics (#726 Ph2).

This is the file that guards the dangerous half of the feature, so the tests are chosen for
what they would catch rather than for coverage:

* **The write boundary re-checks everything.** A `shell:*` engine, a session excluded *after*
  the proposal was minted, a screen that moved, a viewer at the keyboard — each must refuse at
  delivery time, not merely have been filtered upstream.
* **Only server-authored bytes reach a PTY.** `continue` sends the operator's template,
  `choose` a validated digit, `answer` sanitised prose; anything else is undeliverable.
* **Single-writer holds under ownership churn.** Attach/detach flips the byte owner; a write
  must follow it or refuse, never land on a stale fd.
* **At-most-once across a crash.** `claimed` is durable before the write, and recovery parks
  it as `indeterminate` rather than retrying or assuming.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import threading
import time
from unittest import mock

import pytest

from agent_sessions import (
    actuator,
    engines,
    metadata,
    orchestrator,
    prefs,
    scrollback,
    session_input,
)
from agent_sessions import (
    orchestrator_ledger as ledger,
)

KEY = "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SHELL_KEY = "shell:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    # `deliver()` is now fenced by the master switch, and the shipped default is OFF — which is
    # the correct default, so tests about delivery MECHANICS have to opt in explicitly. The
    # tests that are about the switch itself set their own prefs and override this.
    prefs.set_orchestrator({"enabled": True})
    session_input.reset()
    yield
    session_input.reset()


@pytest.fixture
def pty_pair():
    """A real pty. The seam writes to a genuine fd, so timeouts, partial writes and EBADF are
    exercised against the kernel rather than a mock that agrees with us."""
    master, slave = os.openpty()
    yield master, slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def _propose(**over) -> dict:
    rec = {
        "id": "act1",
        "state": "proposed",
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "session_id": KEY,
        "engine": "claude",
        "verb": "continue",
        "confidence": 0.9,
        "rationale": "stalled",
        "evidence": "none",
        "precondition": {},
        **over,
    }
    ledger.append(rec)
    return rec


# --- rendering: only server-authored bytes -----------------------------------------------


def test_continue_sends_the_operators_template_not_model_text():
    """The whole reason `continue` may run unattended: the model chose WHETHER, never WHAT."""
    prefs.set_orchestrator({"nudge_template": "keep going please"})
    out = actuator.render({"verb": "continue"}, prefs.get_orchestrator())
    assert out == b"\x1b[200~keep going please\x1b[201~\r"


def test_choose_renders_a_validated_digit_and_nothing_else():
    assert actuator.render({"verb": "choose", "option": 3}, {}) == b"3\r"
    for bad in (0, 99, "1", True, None):
        with pytest.raises(actuator.NotDeliverable):
            actuator.render({"verb": "choose", "option": bad}, {})


def test_answer_is_sanitised_so_an_escape_cannot_break_out_of_the_paste():
    """An ESC inside the payload could terminate the bracketed paste early and smuggle raw key
    input into the target — the reason handoff.sanitize_seed exists."""
    out = actuator.render({"verb": "answer", "answer": "use \x1b[201~ option two\x00"}, {})
    body = out[len(b"\x1b[200~") : -len(b"\x1b[201~\r")]
    assert b"\x1b" not in body and b"\x00" not in body


def test_dispatch_and_unknown_verbs_are_not_deliverable():
    for verb in ("dispatch", "observe", "escalate", "rm -rf"):
        with pytest.raises(actuator.NotDeliverable):
            actuator.render({"verb": verb}, {})


# --- the write boundary re-checks -------------------------------------------------------


def test_shell_engine_is_refused_at_the_write_boundary(monkeypatch):
    """Upstream filtering is not enough: the capability is re-checked where the bytes are."""
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(metadata, "get", lambda *a, **k: metadata.SessionMeta())
    ok, reason = actuator.check_precondition({"session_id": SHELL_KEY})
    assert ok is False and "not orchestrator-actuable" in reason


def test_session_excluded_after_the_proposal_is_refused(monkeypatch):
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(
        metadata, "get", lambda *a, **k: metadata.SessionMeta(orchestrator_excluded=True)
    )
    ok, reason = actuator.check_precondition({"session_id": KEY})
    assert ok is False and "no longer managed" in reason


def test_a_moved_screen_is_refused(monkeypatch):
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(metadata, "get", lambda *a, **k: metadata.SessionMeta())
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "prompt A")
    good = orchestrator.precondition_for(KEY)
    monkeypatch.setattr(actuator.scrollback, "live_tail_text", lambda *a, **k: "prompt A")
    assert actuator.check_precondition({"session_id": KEY, "precondition": good})[0] is True
    # the agent moved on
    monkeypatch.setattr(actuator.scrollback, "live_tail_text", lambda *a, **k: "totally different")
    ok, reason = actuator.check_precondition({"session_id": KEY, "precondition": good})
    assert ok is False and "screen changed" in reason


def test_an_attached_viewer_blocks_autonomous_delivery(monkeypatch):
    """The operator and the orchestrator must never type at once."""
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(metadata, "get", lambda *a, **k: metadata.SessionMeta())

    class Reg:
        def snapshot(self):
            return [{"id": engines.physical_key(KEY), "attached": True}]

    ok, reason = actuator.check_precondition({"session_id": KEY}, registry=Reg())
    assert ok is False and "viewer is attached" in reason


def test_a_broken_registry_reads_as_busy_not_as_permission(monkeypatch):
    """Fail closed: an error checking 'is someone there?' must never enable a write."""
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(metadata, "get", lambda *a, **k: metadata.SessionMeta())

    class Boom:
        def snapshot(self):
            raise RuntimeError("registry down")

    assert actuator.check_precondition({"session_id": KEY}, registry=Boom())[0] is False


# --- the seam ----------------------------------------------------------------------------


def test_no_writer_means_no_write():
    assert session_input.send_input("claude:nobody", b"x").state == "not_live"


def test_write_reaches_the_pty(pty_pair):
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    out = session_input.send_input(KEY, b"hello\r", require_quiet=False)
    assert out.ok
    assert b"hello" in os.read(slave, 1024)


def test_a_failed_precondition_writes_nothing(pty_pair):
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    out = session_input.send_input(
        KEY, b"nope\r", precondition=lambda: (False, "moved"), require_quiet=False
    )
    assert out.state == "stale"
    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 1024)  # nothing was written


def test_ownership_flip_is_followed_not_guessed(pty_pair):
    """Attach/detach moves the byte owner. A caller names a KEY, never an fd, so the seam
    resolves the current owner — a write must never land on the previous one."""
    master_a, slave_a = pty_pair
    master_b, slave_b = os.openpty()
    try:
        tok = session_input.register_writer(KEY, master_a, threading.Lock(), "headless")
        session_input.unregister_writer(KEY, tok)
        session_input.register_writer(KEY, master_b, threading.Lock(), "attached")
        assert session_input.send_input(KEY, b"second\r", require_quiet=False).ok
        assert b"second" in os.read(slave_b, 1024)
        os.set_blocking(slave_a, False)
        with pytest.raises(BlockingIOError):
            os.read(slave_a, 1024)  # the old owner got nothing
    finally:
        for fd in (master_b, slave_b):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_a_stale_token_cannot_unregister_a_newer_writer(pty_pair):
    """An attach landing while a detach unwinds must not have its registration deleted by the
    departing owner's cleanup."""
    master, _slave = pty_pair
    old = session_input.register_writer(KEY, master, threading.Lock(), "headless")
    session_input.register_writer(KEY, master, threading.Lock(), "attached")
    session_input.unregister_writer(KEY, old)  # late cleanup from the old owner
    assert session_input.is_live(KEY) is True
    assert session_input.current_writer(KEY).kind == "attached"


def test_a_closed_fd_fails_without_claiming_delivery(pty_pair):
    master, slave = pty_pair
    os.close(slave)
    os.close(master)
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    out = session_input.send_input(KEY, b"x", require_quiet=False)
    assert out.state == "failed" and not out.ok


def test_writes_serialise_through_the_shared_lock(pty_pair):
    """The seam must take the SAME lock pump_in and the seed injector use, or 'one writer at a
    time' becomes two guarantees that don't know about each other."""
    master, slave = pty_pair
    lock = threading.Lock()
    session_input.register_writer(KEY, master, lock, "attached")
    lock.acquire()  # simulate pump_in mid-write
    out = session_input.send_input(KEY, b"blocked", timeout_s=0.4, require_quiet=False)
    lock.release()
    assert out.state == "failed"  # timed out before any byte — clean to retry
    assert "before any byte" in out.detail


# --- at-most-once across a crash ----------------------------------------------------------


def test_claimed_is_durable_before_the_write(pty_pair, monkeypatch):
    """The record must prove a delivery was in flight even though it cannot prove the outcome.
    That ordering is what makes the crash recoverable."""
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose()
    seen: list[str] = []

    def spy(key, payload, **kw):
        seen.append(ledger.get("act1")["state"])  # what is on disk at write time?
        return session_input.Outcome("delivered")

    monkeypatch.setattr(session_input, "send_input", spy)
    rec = asyncio.run(actuator.deliver("act1"))
    assert seen == ["claimed"]
    assert rec["state"] == "delivered"


def test_a_crash_between_write_and_record_parks_as_indeterminate(pty_pair, monkeypatch):
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose()

    def die(key, payload, **kw):
        raise KeyboardInterrupt("process killed right after the bytes landed")

    monkeypatch.setattr(session_input, "send_input", die)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(actuator.deliver("act1"))
    assert ledger.get("act1")["state"] == "claimed"  # in-flight, outcome unknowable
    # Startup recovery refuses to guess in either direction.
    assert ledger.recover_claimed() == ["act1"]
    assert ledger.get("act1")["state"] == "indeterminate"
    # And it is not deliverable any more, so no retry can double-deliver it.
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act1"))


def test_a_second_approve_cannot_deliver_twice(pty_pair, monkeypatch):
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose()
    monkeypatch.setattr(
        session_input, "send_input", lambda *a, **k: session_input.Outcome("delivered")
    )
    asyncio.run(actuator.deliver("act1"))
    with pytest.raises(actuator.NotDeliverable):
        asyncio.run(actuator.deliver("act1"))


def test_an_expired_action_is_never_delivered(pty_pair):
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose(expires_at=time.time() - 1)
    assert asyncio.run(actuator.deliver("act1"))["state"] == "expired"


def test_a_partial_write_aborts_rather_than_replaying(pty_pair, monkeypatch):
    """An unterminated bracketed paste is already on the target's stdin; replaying it would
    corrupt the prompt, and claiming success would be a lie."""
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose()
    monkeypatch.setattr(
        session_input,
        "send_input",
        lambda *a, **k: session_input.Outcome("aborted", "partial write (error, 8/40 bytes)"),
    )
    rec = asyncio.run(actuator.deliver("act1"))
    assert rec["state"] == "failed"
    assert rec["outcome"] == "aborted"  # the ledger keeps WHY, for the operator


# --- tier gating ---------------------------------------------------------------------------


def test_auto_delivery_respects_the_ceiling_and_the_threshold(monkeypatch):
    delivered: list[str] = []

    async def fake_deliver(aid, **k):
        delivered.append(aid)
        return {"state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", fake_deliver)
    # `enabled` is now part of the auto gate (a disabled orchestrator must not deliver even
    # a pass-approved action), so this test opts in explicitly — it is about the CEILING.
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo", "confidence_min": 0.8})
    assert asyncio.run(actuator.deliver_auto({"id": "x", "verb": "continue", "confidence": 0.9}))
    # below threshold
    assert (
        asyncio.run(actuator.deliver_auto({"id": "y", "verb": "continue", "confidence": 0.5}))
        is None
    )
    # outside the v1 ceiling, however confident
    assert (
        asyncio.run(actuator.deliver_auto({"id": "z", "verb": "answer", "confidence": 1.0})) is None
    )
    # suggest never auto-delivers
    prefs.set_orchestrator({"autonomy": "suggest"})
    assert (
        asyncio.run(actuator.deliver_auto({"id": "w", "verb": "continue", "confidence": 1.0}))
        is None
    )
    assert delivered == ["x"]


# --- #729 review: the at-most-once guarantee must hold under concurrency ------------------


def test_two_concurrent_deliveries_write_once(pty_pair, monkeypatch):
    """`get()` then `transition()` is a read and a write across TWO lock holds, so two callers
    can both observe `proposed` and both write. That silently breaks at-most-once in the one
    direction that matters — a duplicate `choose` answers a prompt twice."""
    import threading

    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    _propose(verb="choose", option=2)

    writes: list[bytes] = []
    gate = threading.Barrier(2)

    def spy(key, payload, **kw):
        writes.append(payload)
        return session_input.Outcome("delivered")

    monkeypatch.setattr(session_input, "send_input", spy)

    results: list = []

    def run():
        gate.wait()  # maximise the overlap
        try:
            results.append(asyncio.run(actuator.deliver("act1")))
        except actuator.NotDeliverable as e:
            results.append(e)

    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert len(writes) == 1, f"the payload was written {len(writes)} times — not at-most-once"
    assert sum(isinstance(r, actuator.NotDeliverable) for r in results) == 1


def test_claim_is_a_compare_and_swap(tmp_path):
    """The primitive itself: only the first claimant wins, and only from a claimable state."""
    p = tmp_path / "l.jsonl"
    ledger.append({"id": "a", "state": "proposed"}, p)
    first = ledger.claim("a", actuator.CLAIMABLE_STATES, p)
    second = ledger.claim("a", actuator.CLAIMABLE_STATES, p)
    assert first is not None and first["state"] == "claimed"
    assert second is None, "a second claimant won a claim that was already taken"
    # and never from a terminal state
    ledger.append({"id": "b", "state": "delivered"}, p)
    assert ledger.claim("b", actuator.CLAIMABLE_STATES, p) is None


def test_yolo_approvals_are_actually_delivered(pty_pair, monkeypatch):
    """The tier was inert: `_decide` recorded `approved` and nothing ever sent it, so the
    operator was told the orchestrator acts on its own while it waited for a tap."""
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo", "confidence_min": 0.5})
    rec = _propose(state="approved")
    monkeypatch.setattr(
        session_input, "send_input", lambda *a, **k: session_input.Outcome("delivered")
    )
    monkeypatch.setattr(actuator, "DELIVERY_SPACING_S", 0)
    out = asyncio.run(actuator.deliver_pass_actions([rec]))
    assert [r["state"] for r in out] == ["delivered"]
    assert ledger.get(rec["id"])["state"] == "delivered"


def test_only_approved_actions_are_auto_delivered(pty_pair, monkeypatch):
    """A `proposed` action is waiting for a human. Auto-delivering it would make SUGGEST a lie."""
    master, _slave = pty_pair
    session_input.register_writer(engines.physical_key(KEY), master, threading.Lock(), "headless")
    sent: list = []
    monkeypatch.setattr(
        session_input,
        "send_input",
        lambda *a, **k: sent.append(1) or session_input.Outcome("delivered"),
    )
    monkeypatch.setattr(actuator, "DELIVERY_SPACING_S", 0)
    rec = _propose(state="proposed")
    assert asyncio.run(actuator.deliver_pass_actions([rec])) == []
    assert sent == []


# --- #729 re-review: live policy at the write boundary, and reject-as-CAS ----------------


def test_pass_delivery_stops_when_policy_is_withdrawn_mid_batch(tmp_path, monkeypatch):
    """A pass can persist and then deliver over many seconds (spacing between each action).
    An operator who switches orchestration OFF mid-batch must not have the remaining actions
    typed into their sessions on the strength of a decision made before they changed their
    mind. The stale `state == "approved"` is not authority to write."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})

    rec = {
        "id": "a1",
        "state": "approved",
        "verb": "continue",
        "confidence": 0.99,
        "session_id": "claude:abc",
        "engine": "claude",
    }
    ledger.append(rec)

    wrote: list = []
    monkeypatch.setattr(actuator, "deliver", lambda *a, **k: wrote.append(a))

    # The operator withdraws agency after the pass decided, before delivery runs.
    prefs.set_orchestrator({"enabled": False, "autonomy": "off"})

    out = asyncio.run(actuator.deliver_pass_actions([rec]))

    assert out == [], "a withdrawn policy still delivered a pass-approved action"
    assert wrote == [], "bytes were written after orchestration was switched off"


def test_reject_cannot_overwrite_a_delivered_action(tmp_path, monkeypatch):
    """A stale tab must not be able to rewrite history. Reporting `rejected` over `delivered`
    tells the operator nothing was sent when it was — the worst direction for this to fail."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    ledger.append({"id": "a1", "state": "proposed", "verb": "continue", "session_id": "claude:x"})
    ledger.transition("a1", "delivered")

    assert ledger.compare_and_set("a1", ledger.REJECTABLE_STATES, "rejected") is None
    assert ledger.get("a1")["state"] == "delivered"


def test_a_reject_racing_a_claim_cannot_both_win(tmp_path, monkeypatch):
    """`claimed -> rejected -> delivered` is the race: the reject returns success while the
    bytes are already on their way. Once claimed, reject must lose."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    ledger.append({"id": "a1", "state": "proposed", "verb": "continue", "session_id": "claude:x"})

    # Delivery claims it first.
    assert ledger.claim("a1", actuator.CLAIMABLE_STATES) is not None
    # The operator's reject now arrives.
    assert ledger.compare_and_set("a1", ledger.REJECTABLE_STATES, "rejected") is None
    assert ledger.get("a1")["state"] == "claimed", "a reject overtook an in-flight delivery"


def test_reject_still_works_from_every_waiting_state(tmp_path, monkeypatch):
    """The guard must not be so tight it breaks the normal path."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    for i, state in enumerate(sorted(ledger.REJECTABLE_STATES)):
        aid = f"a{i}"
        ledger.append({"id": aid, "state": state, "verb": "continue", "session_id": "claude:x"})
        got = ledger.compare_and_set(aid, ledger.REJECTABLE_STATES, "rejected")
        assert got is not None and got["state"] == "rejected", f"reject failed from {state}"


def test_the_master_switch_alone_stops_auto_delivery(monkeypatch):
    """`enabled: false` must stop WRITES, not merely stop new proposals — even at the yolo
    tier with a high-confidence allowed verb. Previously the gate checked only the tier."""
    delivered: list[str] = []

    async def fake_deliver(aid, **k):
        delivered.append(aid)
        return {"state": "delivered"}

    monkeypatch.setattr(actuator, "deliver", fake_deliver)
    prefs.set_orchestrator({"enabled": False, "autonomy": "yolo", "confidence_min": 0.5})
    got = asyncio.run(actuator.deliver_auto({"id": "x", "verb": "continue", "confidence": 0.99}))
    assert got is None and delivered == [], "a disabled orchestrator still wrote to a session"


# --- #729 re-review 2: fd-reuse and the master switch on the manual path -----------------


def test_a_torn_down_fd_cannot_be_reused_under_a_write(pty_pair):
    """The nastiest failure this seam can have. `send_input` used to snapshot the fd NUMBER;
    teardown then closed it and the kernel handed the same integer to the next open. The write
    succeeded — into an unrelated pipe — and the seam reported `delivered` while the real
    session got nothing. Pinning the file DESCRIPTION with dup() makes that impossible: our
    copy still refers to the original master, so a write after teardown fails closed."""
    master, slave = pty_pair
    token = session_input.register_writer(KEY, master, threading.Lock(), "headless")

    borrowed = session_input.borrow_writer(KEY)
    assert borrowed is not None
    dup_fd, _lock, got_token = borrowed
    assert got_token == token
    try:
        # Teardown runs: the registration goes, the master is closed, and the integer is now
        # free for the kernel to hand out again.
        session_input.unregister_writer(KEY, token)
        os.close(master)
        r, w = os.pipe()
        try:
            # The classic symptom is the pipe landing on the SAME number the pty had.
            if r == master or w == master:
                # Our borrowed fd must NOT be that pipe — it is a separate descriptor
                # referring to the original (now closed) pty master.
                assert dup_fd not in (r, w)
            # Writing through the borrowed fd cannot reach the pipe under any circumstance.
            with contextlib.suppress(OSError):
                os.write(dup_fd, b"ORCHESTRATOR_INPUT")
            os.set_blocking(r, False)
            with pytest.raises(BlockingIOError):
                os.read(r, 64)  # nothing was smuggled into the reused descriptor
        finally:
            os.close(r)
            os.close(w)
    finally:
        with contextlib.suppress(OSError):
            os.close(dup_fd)
        with contextlib.suppress(OSError):
            os.close(slave)


def test_the_master_switch_fences_manual_approval_too(tmp_path, monkeypatch, pty_pair):
    """A proposal in a stale tab must not be approvable after orchestration is switched off.
    `deliver()` read prefs only to render the payload, so the manual path was unfenced — while
    the OFF tier's own copy promises nothing is ever sent."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    master, _slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")

    ledger.append(
        {
            "id": "a1",
            "state": "proposed",
            "verb": "continue",
            "confidence": 0.99,
            "session_id": KEY,
            "engine": "claude",
        }
    )
    prefs.set_orchestrator({"enabled": False, "autonomy": "off"})

    rec = asyncio.run(actuator.deliver("a1"))

    assert rec["state"] == "stale", f"a write happened with orchestration off: {rec['state']}"
    assert ledger.get("a1")["state"] == "stale"


# --- #729 round 4: the guard must be fenced against the WRITE, not run earlier ------------


def test_policy_withdrawn_during_the_quiet_wait_blocks_the_write(tmp_path, monkeypatch, pty_pair):
    """Hermes's reproduction: pause inside the quiet wait, switch orchestration off, resume.
    Every check above ran before that wait, so an unfenced verdict still authorised the write.
    Nothing may reach the PTY."""
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    ledger.append(
        {
            "id": "a1",
            "state": "proposed",
            "verb": "continue",
            "confidence": 0.99,
            "session_id": KEY,
            "engine": "claude",
        }
    )

    real_wait = session_input._wait_quiet

    def wait_then_withdraw(key, deadline):
        out = real_wait(key, deadline)
        # The operator flips the switch while we were waiting for the session to go quiet.
        prefs.set_orchestrator({"enabled": False, "autonomy": "off"})
        return out

    monkeypatch.setattr(session_input, "_wait_quiet", wait_then_withdraw)
    monkeypatch.setattr(actuator, "check_precondition", lambda *a, **k: (True, ""))

    rec = asyncio.run(actuator.deliver("a1"))

    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)  # not one byte reached the session
    assert rec["state"] == "stale", f"wrote after the switch was flipped: {rec['state']}"


def test_a_viewer_attaching_during_the_wait_blocks_the_write(tmp_path, monkeypatch, pty_pair):
    """The screen contract is 'no viewer at the keyboard immediately before the first byte'.
    A browser that attaches after the early check but before the lock is acquired must win —
    otherwise the actuator types into a session somebody is actively using."""
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    ledger.append(
        {
            "id": "a1",
            "state": "proposed",
            "verb": "continue",
            "confidence": 0.99,
            "session_id": KEY,
            "engine": "claude",
        }
    )

    calls = {"n": 0}

    def precondition_then_attach(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, ""  # the early, unfenced check sees a clear screen
        return False, "a viewer is attached"  # by the final guard, someone is at the keyboard

    monkeypatch.setattr(actuator, "check_precondition", precondition_then_attach)

    rec = asyncio.run(actuator.deliver("a1"))

    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)
    assert rec["state"] == "stale", f"wrote into a session with a viewer: {rec['state']}"
    assert calls["n"] >= 2, "the precondition was never re-evaluated at the write boundary"


# --- #729 round 5: generation binding + CAS settlement ------------------------------------


def test_the_payload_does_not_follow_ownership_to_a_new_writer(pty_pair, monkeypatch):
    """The precondition's verdict — no viewer, screen still right — describes ONE writer. If
    the registry is swapped before the write, that verdict says nothing about the new owner.
    A probe caught the payload following ownership: the headless pty got nothing and a freshly
    attached one received the input."""
    master, slave = pty_pair
    other_m, other_s = pty.openpty()
    try:
        session_input.register_writer(KEY, master, threading.Lock(), "headless")

        def swap_then_ok():
            # A browser attaches in the window between the check and the write.
            session_input.register_writer(KEY, other_m, threading.Lock(), "attached")
            return True, ""

        out = session_input.send_input(
            KEY, b"FOLLOWED_ATTACHED\n", precondition=swap_then_ok, require_quiet=False
        )

        assert out.state == "stale", f"wrote against a verdict formed for another writer: {out}"
        for fd in (slave, other_s):
            os.set_blocking(fd, False)
            with pytest.raises(BlockingIOError):
                os.read(fd, 4096)  # neither pty received anything
    finally:
        for fd in (other_m, other_s):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_expiry_cannot_overwrite_a_claim_that_landed_since_the_snapshot(tmp_path, monkeypatch):
    """`expire_due` snapshots outside the lock, so an action can be claimed between being
    listed and being expired. Skipping `claimed` in the loop is not enough — without CAS the
    ledger records approved -> claimed -> expired -> delivered, an action expired and then
    delivered anyway."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    past = time.time() - 60
    ledger.append(
        {
            "id": "a1",
            "state": "approved",
            "verb": "continue",
            "session_id": KEY,
            "engine": "claude",
            "expires_at": past,
        }
    )

    real_live = ledger.live_actions

    def snapshot_then_claim(*a, **k):
        rows = real_live(*a, **k)
        # A delivery claims it after expiry has taken its snapshot.
        assert ledger.claim("a1", actuator.CLAIMABLE_STATES) is not None
        return rows

    monkeypatch.setattr(ledger, "live_actions", snapshot_then_claim)

    moved = ledger.expire_due()

    assert moved == [], "expiry overwrote a claim that landed after the snapshot"
    assert ledger.get("a1")["state"] == "claimed"


def test_a_pre_claim_settlement_cannot_overwrite_a_claim(tmp_path, monkeypatch):
    """Every early return in deliver() sits between the ledger read and the claim. A blind
    transition there records `failed`/`expired` on top of a delivery already underway."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": KEY})
    assert ledger.claim("a1", actuator.CLAIMABLE_STATES) is not None

    assert actuator._settle_waiting("a1", "failed", detail="session is not live") is None
    assert ledger.get("a1")["state"] == "claimed", "a settlement overwrote an in-flight claim"


# --- #729 round 6: auto-authority and ownership DURING the final guard --------------------


def test_leaving_yolo_mid_delivery_stops_an_auto_write(tmp_path, monkeypatch, pty_pair):
    """An automatic delivery rests on yolo + the verb ceiling + the confidence threshold.
    Re-checking only `enabled`/`autonomy != off` at the boundary let a yolo -> suggest switch
    through, and the payload was typed on the authority of a tier the operator had left."""
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo", "confidence_min": 0.5})
    action = {
        "id": "a1",
        "state": "approved",
        "verb": "continue",
        "confidence": 0.99,
        "session_id": KEY,
        "engine": "claude",
    }
    ledger.append(action)

    real_wait = session_input._wait_quiet

    def wait_then_downgrade(key, deadline):
        out = real_wait(key, deadline)
        prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
        return out

    monkeypatch.setattr(session_input, "_wait_quiet", wait_then_downgrade)
    monkeypatch.setattr(actuator, "check_precondition", lambda *a, **k: (True, ""))

    rec = asyncio.run(actuator.deliver_auto(action))

    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)
    assert (
        rec is not None and rec["state"] == "stale"
    ), f"auto-delivered on the authority of a tier the operator had left: {rec}"


def test_a_writer_swap_during_the_final_guard_blocks_the_write(pty_pair):
    """Ownership can change while the guard callback runs — the guard does I/O, so the window
    is real. The payload must not land on the pty that was current when the guard started."""
    master, slave = pty_pair
    other_m, other_s = pty.openpty()
    try:
        session_input.register_writer(KEY, master, threading.Lock(), "headless")

        def guard_then_swap():
            session_input.register_writer(KEY, other_m, threading.Lock(), "attached")
            return True, ""

        out = session_input.send_input(
            KEY, b"AFTER_GUARD_SWAP\n", final_guard=guard_then_swap, require_quiet=False
        )

        assert out.state != "delivered", f"wrote after ownership changed under the guard: {out}"
        for fd in (slave, other_s):
            os.set_blocking(fd, False)
            with pytest.raises(BlockingIOError):
                os.read(fd, 4096)
    finally:
        for fd in (other_m, other_s):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_a_release_during_the_final_guard_fails_closed(pty_pair):
    """An ABSENT writer used to read as success — the token was only compared when a current
    writer existed — so the payload went to a pinned fd nobody owned any more."""
    master, slave = pty_pair
    token = session_input.register_writer(KEY, master, threading.Lock(), "headless")

    def guard_then_release():
        session_input.unregister_writer(KEY, token)
        return True, ""

    out = session_input.send_input(
        KEY, b"AFTER_RELEASE\n", final_guard=guard_then_release, require_quiet=False
    )

    assert out.state != "delivered", f"wrote into a session nobody owns: {out}"
    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)


# --- #729 round 7: durability + operator-facing ------------------------------------------


def test_a_short_ledger_write_does_not_report_a_claim_it_did_not_persist(tmp_path, monkeypatch):
    """POSIX permits a short write. A single os.write that returns fewer bytes leaves a torn
    record, so `claim()` reported success while `get()` still read `approved` — delivery then
    proceeds with no durable claim, defeating restart at-most-once."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": KEY})

    real_write = os.write
    state = {"first": True}

    def short_once(fd, data):
        if state["first"] and len(data) > 12:
            state["first"] = False
            return real_write(fd, data[:12])  # a torn record
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_once)
    got = ledger.claim("a1", actuator.CLAIMABLE_STATES)
    monkeypatch.setattr(os, "write", real_write)

    # Either the claim completed durably, or it failed — never "succeeded" over a torn record.
    if got is not None:
        assert (
            ledger.get("a1")["state"] == "claimed"
        ), "claim() reported success but the record was not durable"
    # And the file must still parse, whatever happened.
    ledger.get("a1")


def test_pending_and_feed_are_disjoint(tmp_path, monkeypatch):
    """The UI renders both lists, so an action in each is shown twice. The e2e helper defaulted
    `feed` to empty, which hid the real response shape."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    ledger.append({"id": "a1", "state": "proposed", "verb": "continue", "session_id": KEY})
    ledger.append({"id": "a2", "state": "delivered", "verb": "continue", "session_id": KEY})

    live = ledger.live_actions()
    pending = [r for r in live if r.get("state") in ("proposed", "approved", "escalated")]
    pending_ids = {r["id"] for r in pending}
    feed = [r for r in ledger.feed(100) if r["id"] not in pending_ids]

    assert pending_ids == {"a1"}
    assert {r["id"] for r in feed} == {"a2"}
    assert not (pending_ids & {r["id"] for r in feed}), "an action would render twice"


# --- #726: the unified authorization fence (option 1) ------------------------------------


def test_policy_withdrawn_between_the_guard_and_byte_one_refuses(tmp_path, monkeypatch, pty_pair):
    """The probe the six previous fixes could not stop: flip policy AFTER the guard's snapshot
    but BEFORE the write. The guard's verdict is only as fresh as the moment it ran; the fence
    re-reads policy inside the lock, so a withdrawal at any point up to byte one refuses."""
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})

    def guard_then_withdraw():
        # Everything the guard checks passes...
        prefs.set_orchestrator({"enabled": False, "autonomy": "off"})
        return True, ""  # ...and policy is withdrawn on the way out.

    out = session_input.send_input(
        KEY,
        b"UNSAFE\n",
        final_guard=guard_then_withdraw,
        policy_fingerprint=actuator._policy_fingerprint,
        require_quiet=False,
    )

    assert out.state != "delivered", f"wrote after policy was withdrawn: {out}"
    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)


def test_a_viewer_attaching_during_authorization_refuses(pty_pair):
    """Viewer state lives in another lock domain entirely, so no check can observe it — only
    the epoch fences it. An attach must invalidate an authorization already in flight."""
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")

    def guard_then_attach():
        session_input.bump_epoch(KEY)  # what session_stream.on_attach does
        return True, ""

    out = session_input.send_input(
        KEY, b"ATTACHED\n", final_guard=guard_then_attach, require_quiet=False
    )

    assert out.state != "delivered", f"wrote while a viewer was attaching: {out}"
    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)


def test_every_chunk_of_a_payload_is_written_under_the_fence(pty_pair):
    """Fencing only chunk one let a multi-chunk payload split across owners: the first 256
    bytes to the current terminal, the rest to one that replaced it mid-write, with the ledger
    reporting success.

    Asserted structurally rather than by racing threads — a timing test here is flaky in both
    directions, and my first two attempts at one passed for the wrong reasons (the swap landed
    before the send even started; then an fd filter matched nothing because the fence writes
    through the dup `borrow_writer` pins, not the original master).

    The invariant IS the lock: if every `os.write` happens while the registry lock is held,
    then `register_writer` / `unregister_writer` / `bump_epoch` — all of which need that lock —
    cannot interleave with any part of the send. That is exactly what a split requires.
    """
    master, _slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    payload = b"X" * (session_input.WRITE_CHUNK * 3 + 7)  # forces several chunks

    real_write = os.write
    observed: list[bool] = []

    def watched_write(fd, data):
        observed.append(session_input._lock.locked())
        return real_write(fd, data)

    with mock.patch.object(os, "write", watched_write):
        out = session_input.send_input(KEY, payload, require_quiet=False)

    assert out.state == "delivered", f"the send did not complete: {out}"
    assert len(observed) >= 2, f"expected a multi-chunk write, saw {len(observed)} chunk(s)"
    assert all(observed), (
        f"{observed.count(False)} of {len(observed)} chunks were written WITHOUT the registry "
        "lock — ownership could change mid-payload and split it across terminals"
    )


def test_a_policy_write_is_ordered_against_an_in_flight_send(tmp_path, monkeypatch):
    """Hermes's remaining window: the policy fingerprint was READ inside the fence, but
    `prefs.set_orchestrator` never took that lock, so a write could still land between the read
    and `os.write`.

    Asserted as the ORDERING PRIMITIVE rather than by racing the microseconds between a read
    and a write — my first attempt withdrew policy in the guard, which runs BEFORE the fence,
    so it passed against the unfixed code and proved nothing.

    The property that actually closes it: a policy write must BLOCK while the registry lock is
    held. Then a send holding that lock for its payload cannot be overtaken, and a write that
    landed earlier is caught by the epoch compare.
    """
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    prefs.set_orchestrator({"enabled": True})

    started = threading.Event()
    finished = threading.Event()

    def writer():
        started.wait(timeout=5)
        prefs.set_orchestrator({"autonomy": "suggest"})
        finished.set()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    with session_input._lock:  # stand in for a send holding the fence
        started.set()
        # While the fence is held, the policy write must not complete.
        blocked = not finished.wait(timeout=1.0)
    t.join(timeout=5)

    assert blocked, (
        "a policy write completed while the write fence was held — policy mutation is not "
        "ordered against an in-flight send, so it can land between the check and byte one"
    )
    assert finished.is_set(), "the policy write never completed after the fence was released"


def test_the_policy_fence_needs_no_prefs_read(tmp_path, monkeypatch):
    """Guards the DEADLOCK, not just the race. If the fence read prefs while holding the
    registry lock, a concurrent `set_orchestrator` (prefs lock held, registry lock wanted)
    would deadlock both. An in-memory counter is what keeps that impossible."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    before = session_input.current_policy_epoch()
    prefs.set_orchestrator({"enabled": True})
    assert (
        session_input.current_policy_epoch() > before
    ), "set_orchestrator did not announce the change; the fence cannot see policy writes"


def test_a_per_session_opt_out_is_ordered_against_an_in_flight_send(tmp_path, monkeypatch):
    """The last hole in the fence. `orchestrator_excluded` is read by `check_precondition`
    inside the final guard, which runs BEFORE the write lock is taken — so an opt-out landing
    between the guard and byte one changed nothing the fence could observe: the writer
    generation was untouched and the policy epoch is global, not per-session.

    Asserted as the ordering primitive, like the policy case: the opt-out must BLOCK while the
    fence is held. Then a send holding it cannot be overtaken, and an opt-out that landed
    earlier is caught by the session epoch compare.
    """
    started = threading.Event()
    finished = threading.Event()

    def excluder():
        started.wait(timeout=5)
        with session_input.session_transaction(KEY):
            pass  # stands in for the route's metadata read-modify-write
        finished.set()

    t = threading.Thread(target=excluder, daemon=True)
    t.start()
    with session_input._lock:  # stands in for a send holding the fence
        started.set()
        blocked = not finished.wait(timeout=1.0)
    t.join(timeout=5)

    assert blocked, (
        "a per-session opt-out committed while the write fence was held — it can land between "
        "the guard and byte one, and the session still receives input"
    )
    assert finished.is_set(), "the opt-out never completed after the fence was released"


def test_the_opt_out_bumps_only_its_own_session(tmp_path):
    """Scoped, not global: opting one session out must not cancel an in-flight send to another.
    That is why this is a session transaction rather than reusing the policy one."""
    other = "claude:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    before_self = session_input.current_epoch(KEY)
    before_other = session_input.current_epoch(other)
    with session_input.session_transaction(KEY):
        pass
    assert session_input.current_epoch(KEY) > before_self
    assert (
        session_input.current_epoch(other) == before_other
    ), "opting one session out invalidated another session's in-flight authorization"


def test_agent_output_between_the_guard_and_byte_one_refuses(pty_pair, monkeypatch):
    """The last unfenced domain: the SCREEN.

    `check_precondition` compares a screen fingerprint, but that read happens in the final
    guard — which must run outside the fence because it does I/O. Ordinary agent output in the
    window between that read and the first byte changes the prompt without touching ownership,
    the session epoch or policy, so a stale `choose`/`answer` gets typed into a prompt nobody
    proposed against. Answering the wrong question is worse than not answering.
    """
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")

    def guard_then_output():
        # The guard approves the screen it just read...
        ok = (True, "")
        # ...and the agent prints before we reach byte one. Driven through the REAL ingest
        # chokepoint rather than the epoch API, so this test is a behavioural proof on code
        # that predates the fix (where it fails by DELIVERING, not by AttributeError).
        scrollback._buffer_append(KEY, b"\r\n> a different prompt now\r\n")
        return ok

    out = session_input.send_input(KEY, b"2\n", final_guard=guard_then_output, require_quiet=False)

    assert out.state != "delivered", f"wrote into a screen that changed under the guard: {out}"
    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)


def test_a_quiet_screen_still_delivers(pty_pair):
    """The fence must not refuse when nothing changed — otherwise it never delivers at all."""
    master, _slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")
    out = session_input.send_input(
        KEY, b"ok\n", final_guard=lambda: (True, ""), require_quiet=False
    )
    assert out.state == "delivered", f"a quiet screen was refused: {out}"


def test_the_screen_epoch_bumps_BEFORE_the_bytes_are_observable(pty_pair, monkeypatch):
    """Hermes's B1, and the gap my first screen test could not see.

    `_buffer_append` runs on a worker-thread pump. The bump used to sit AFTER `buf.extend`, so
    between the bytes entering the ring and the epoch moving there was a window — widened
    arbitrarily by anything slow in between (an FS stall in `_persist_append`, a regex over a
    256 KiB chunk in `_scan_modes`). The fence would read the old epoch while the screen had
    already changed, and the write would commit into it.

    Reproduced the way Hermes did: pause inside `_buffer_append` at the point where the bytes
    are in the ring, and assert the epoch has ALREADY moved. Bump-then-mutate is the only safe
    order; my synchronous test passed either way and therefore proved nothing about this.
    """
    observed: dict = {}
    real_scan = scrollback._scan_modes

    def scan_and_observe(key, data):
        # By here `buf.extend` has run — the new bytes ARE observable. The epoch must already
        # reflect them, or a fence reading now would authorise a write against a stale screen.
        observed["epoch_mid_ingest"] = session_input.current_screen_epoch(KEY)
        return real_scan(key, data)

    monkeypatch.setattr(scrollback, "_scan_modes", scan_and_observe)

    before = session_input.current_screen_epoch(KEY)
    scrollback._buffer_append(KEY, b"\r\n> a different prompt\r\n")

    assert observed.get("epoch_mid_ingest") is not None, "the ingest path did not run"
    assert observed["epoch_mid_ingest"] > before, (
        "the screen epoch had not moved while the new bytes were already in the ring — a "
        "fence reading here would authorise a write against a screen that has changed"
    )


def test_reset_clears_the_epochs_too(pty_pair):
    """Leaked epoch state makes the next test refuse a write for a change that never happened
    in it, which reads as a flaky fence rather than as leaked state."""
    session_input.register_writer(KEY, pty_pair[0], threading.Lock(), "headless")
    session_input.bump_screen_epoch(KEY)
    assert session_input.current_screen_epoch(KEY) > 0
    session_input.reset()
    assert session_input.current_screen_epoch(KEY) == 0, "reset() leaked a screen epoch"
    assert session_input.current_epoch(KEY) == 0, "reset() leaked a session epoch"


def test_a_sender_that_starts_mid_ingest_refuses(pty_pair, monkeypatch):
    """The inverse race my bump-before "fix" opened, and the reason a single counter cannot
    express this at all.

    With a lone bump moved BEFORE the ring mutation, a sender starting in the gap captures the
    already-incremented value, its guard reads the OLD screen, the append then lands with no
    further change — so the in-fence comparison matches and the write goes into the NEW screen.
    Hermes reproduced exactly that: guard saw 'PROMPT A', byte one went into 'PROMPT A\\nPROMPT
    B', epoch 2 both times, pty received the payload.

    The seqlock makes the in-flight state observable: odd means a mutation is underway, so a
    sender that starts there refuses instead of approving against a screen mid-change.
    """
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")

    # Enter the ingest interval and stay inside it — the state a sender must refuse.
    cm = session_input.screen_change(KEY)
    cm.__enter__()
    try:
        assert not session_input.screen_is_stable(KEY), "odd sequence not observable mid-change"
        out = session_input.send_input(
            KEY, b"CHOOSE-1\n", final_guard=lambda: (True, ""), require_quiet=False
        )
        assert out.state != "delivered", f"wrote while the screen was mid-change: {out}"
        os.set_blocking(slave, False)
        with pytest.raises(BlockingIOError):
            os.read(slave, 4096)
    finally:
        cm.__exit__(None, None, None)

    # And once the interval closes, the sequence is even again and a send is allowed.
    assert session_input.screen_is_stable(KEY)
    ok = session_input.send_input(KEY, b"ok\n", final_guard=lambda: (True, ""), require_quiet=False)
    assert ok.state == "delivered", f"a settled screen was refused: {ok}"


def test_the_ingest_interval_leaves_the_sequence_even(pty_pair):
    """A leaked odd sequence would refuse every future write for that session — the fence
    failing closed forever is as broken as failing open."""
    before = session_input.current_screen_epoch(KEY)
    scrollback._buffer_append(KEY, b"hello\r\n")
    after = session_input.current_screen_epoch(KEY)
    assert after > before, "the ingest did not publish a change"
    assert after % 2 == 0, f"the sequence was left ODD ({after}) — every later write refuses"


def test_ingest_cannot_land_between_the_epoch_read_and_byte_one(pty_pair, monkeypatch):
    """The seqlock proved the guard read a COHERENT screen; it could not RESERVE it.

    Comparing the epoch and then writing leaves ingestion free to enter and complete an entire
    interval in between — Hermes reproduced exactly that: the in-fence read returned the
    matching even value, another thread appended `PROMPT B` through the real
    `scrollback._buffer_append`, and byte one went into the new screen.

    The comparison and the first byte now happen while holding `_screen_lock`, which
    `screen_change()` must acquire to open an interval. So ingestion either completes before
    the comparison or starts after the input is committed — never between. Proven by having
    another thread attempt exactly that landing while the first write is in progress.
    """
    master, slave = pty_pair
    session_input.register_writer(KEY, master, threading.Lock(), "headless")

    landed_during_write: list[bool] = []
    real_write = os.write

    def write_and_probe(fd, data):
        # While the first byte is being written the fence holds `_screen_lock`, so an ingest
        # attempting to open an interval right now MUST be blocked.
        t = threading.Thread(
            target=scrollback._buffer_append, args=(KEY, b"\r\nPROMPT B\r\n"), daemon=True
        )
        t.start()
        t.join(timeout=0.4)
        landed_during_write.append(not t.is_alive())  # True == it got in (bad)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write_and_probe)
    out = session_input.send_input(
        KEY, b"CHOOSE-1\n", final_guard=lambda: (True, ""), require_quiet=False
    )
    monkeypatch.undo()

    assert out.state == "delivered", f"the settled-screen send was refused: {out}"
    assert landed_during_write, "the write path never ran"
    assert not landed_during_write[0], (
        "a screen change completed while the first byte was being written — the fence observes "
        "the screen but does not reserve it, so byte one can land in a prompt that just moved"
    )
