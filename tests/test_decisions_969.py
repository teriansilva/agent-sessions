"""#969 — a working session gets no proposal, an undeliverable one is withdrawn, and an explicit
approval is not refused by the viewer it was tapped in.

The end-to-end cases (a sweep against a visibly working session; an approval through the real
authenticated route into a real pty) sit beside the rest of the orchestrator in
`test_orchestrator.py`. This file pins the pieces those rest on, each against its real helper:

* **Busy means VISIBLE output.** Measured on the live host, a codex session waiting on the operator
  rewrites its window title about once a second. That is output, so the `working` indicator sees
  it — and a busy test built on it would silence exactly the sessions that need a decision.
* **Withdrawal is irreversible, so it only takes conditions that do not clear on their own.** The
  screen check is re-run on every read (no shortcut: a height-only resize changes what renders
  without new bytes), and a missing writer is death only when the dtach master is gone too (attach
  and detach hand the writer over).
* **Withdrawal adds no screen publisher.** A second opener of `screen_change` intervals could
  overlap the append path's and leave an even epoch while a mutation was in flight (#975 review).
* **An operator approval narrows only the attached half of the viewer check.**
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time

import pytest

from agent_sessions import (
    actuator,
    engines,
    metadata,
    orchestrator,
    prefs,
    scrollback,
    session_input,
    session_stream,
    vtscreen,
    webterm,
)
from agent_sessions import (
    orchestrator_ledger as ledger,
)
from automation_helpers import current_action

KEY = "claude:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PHYS = engines.physical_key(KEY)
# Exactly what the live codex sessions emitted while waiting on the operator.
TITLE_BLINK = b"\x1b]0;[ ! ] Action Required | Separate missions and sessions | agent-sessions\x07"
SCREEN = "› waiting on you"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(metadata, "get", lambda *a, **k: metadata.SessionMeta())
    session_input.reset()
    yield
    session_input.reset()


@pytest.fixture(autouse=True)
def master(monkeypatch):
    """The session's dtach master, as REAL Unix sockets at a path this test owns.

    Absent by default, so no test depends on what exists on the host running the suite. Call it
    with a verdict to stand one up: ``alive`` listens, ``dead`` is a bound socket nobody listens on
    (connect refused, as after a crash), and ``unknown`` is a listening socket whose connect is
    made to time out at `_probe_once`, the documented starved-host outcome. Short `/tmp` dir: a
    pytest `tmp_path` can exceed the AF_UNIX path limit.
    """
    import shutil
    import socket
    import tempfile
    from pathlib import Path

    d = tempfile.mkdtemp(prefix="m969", dir="/tmp")
    sock_path = Path(d) / "m.sock"
    monkeypatch.setattr(actuator.ptybridge, "socket_path", lambda *_a: sock_path)
    opened: list[socket.socket] = []

    def stand_up(verdict: str) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(sock_path))
        if verdict in ("alive", "unknown"):
            s.listen(1)
            opened.append(s)
        else:
            s.close()  # the file stays; nothing will ever accept on it
        if verdict == "unknown":
            monkeypatch.setattr(
                actuator.ptybridge, "_probe_once", lambda *_a: actuator.ptybridge.UNKNOWN
            )

    yield stand_up
    for s in opened:
        s.close()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def live():
    """A real pty registered as the session's writer, so `is_live` is a fact, not a stub."""
    master_fd, slave_fd = os.openpty()
    session_input.register_writer(PHYS, master_fd, threading.Lock(), "headless")
    yield master_fd, slave_fd
    for fd in (master_fd, slave_fd):
        with contextlib.suppress(OSError):
            os.close(fd)


class _Registry:
    def __init__(self, *rows):
        self.rows = [dict(r) for r in rows]

    def snapshot(self):
        return self.rows


def _capture(monkeypatch, screen: str = SCREEN) -> dict:
    monkeypatch.setattr(scrollback, "live_tail_text", lambda *a, **k: screen)
    return orchestrator.precondition_for(PHYS)


def _propose(pre: dict, **over) -> dict:
    rec = {
        "id": "act1",
        "state": "proposed",
        "verb": "continue",
        "session_id": KEY,
        "confidence": 0.9,
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "precondition": pre,
        **over,
    }
    rec = current_action(rec)
    ledger.append(rec)
    return rec


def _count_replays(monkeypatch, screen: str = SCREEN) -> list[int]:
    replays: list[int] = []

    def read(*_a, **_k):
        replays.append(1)
        return screen

    monkeypatch.setattr(scrollback, "live_tail_text", read)
    return replays


# --- A. busy means visible output ------------------------------------------------------------


@pytest.mark.parametrize(
    "chunk",
    [
        TITLE_BLINK,
        TITLE_BLINK * 3,
        b"\x1b]0;title with ST\x1b\\",
        b"\x1bPq#0;2;0;0;0\x1b\\",  # DCS
        b"\x1b]0;a title cut off mid-chunk",
    ],
)
def test_a_chunk_of_string_controls_alone_is_not_visible(chunk):
    assert vtscreen.has_visible_bytes(chunk) is False


@pytest.mark.parametrize(
    "chunk",
    [
        b"Working",
        "\x1b[2K\r✻ Cooking…".encode(),
        b"\x1b[H",  # a cursor move can change what is drawn next
        TITLE_BLINK + b"x",
        b"x" + TITLE_BLINK,
    ],
)
def test_anything_that_can_change_a_cell_is_visible(chunk):
    assert vtscreen.has_visible_bytes(chunk) is True


def test_an_empty_chunk_is_not_visible():
    assert vtscreen.has_visible_bytes(b"") is False


def test_a_title_blink_stamps_output_but_not_visible_output(monkeypatch):
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0)
    webterm._buffer_append(PHYS, TITLE_BLINK)
    # The `working` indicator still sees it — its behaviour is deliberately unchanged…
    assert webterm.get_last_output_at(PHYS) == 1000.0
    # …but nothing on the screen changed.
    assert webterm.get_last_visible_output_at(PHYS) is None

    monkeypatch.setattr(webterm.time, "time", lambda: 1001.0)
    webterm._buffer_append(PHYS, b"\x1b[2K\rWorking")
    assert webterm.get_last_visible_output_at(PHYS) == 1001.0


def test_replay_grace_bytes_do_not_stamp_visible_output(monkeypatch):
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0)
    webterm.note_attach(PHYS)
    webterm._buffer_append(PHYS, b"\x1b[2Jreplayed screen")
    assert webterm.get_last_visible_output_at(PHYS) is None


def test_dropping_the_buffer_forgets_visible_output():
    webterm._buffer_append(PHYS, b"output")
    assert webterm.get_last_visible_output_at(PHYS) is not None
    webterm._drop_buffer(PHYS)
    assert webterm.get_last_visible_output_at(PHYS) is None


def test_the_registry_row_carries_visible_output_and_leaves_working_alone(monkeypatch):
    monkeypatch.setattr(webterm.time, "time", lambda: 1000.0)
    webterm._buffer_append(PHYS, TITLE_BLINK)
    reg = session_stream.SessionRegistry()
    reg._sessions[PHYS] = {"engine": "claude", "sid": "x", "started_at": 0.0, "attached": True}

    row = reg._row(PHYS)

    assert row["working"] is True and row["last_output_at"] == 1000.0
    assert row["visible_output_at"] is None


def test_busy_keys_counts_only_fresh_visible_output():
    now = 5000.0
    reg = _Registry(
        {"id": "claude:busy", "visible_output_at": now - 1, "working": True, "attached": True},
        {"id": "claude:blinking", "visible_output_at": None, "working": True, "attached": True},
        {"id": "claude:open-and-idle", "visible_output_at": now - 600, "attached": True},
        {"id": "claude:edge", "visible_output_at": now - actuator.BUSY_WINDOW_S},
        {"id": "claude:bool", "visible_output_at": True},
    )
    assert actuator.busy_keys(reg, now=now) == {"claude:busy"}


def test_the_busy_window_is_the_working_window():
    assert actuator.BUSY_WINDOW_S == session_stream._WORKING_WINDOW_S


def test_no_registry_or_a_broken_one_yields_no_busy_set():
    class Boom:
        def snapshot(self):
            raise RuntimeError("registry down")

    assert actuator.busy_keys(None) == set()
    assert actuator.busy_keys(Boom()) == set()


# --- B. withdrawal ---------------------------------------------------------------------------


def test_an_unchanged_screen_is_kept(monkeypatch, live):
    _propose(_capture(monkeypatch))
    replays = _count_replays(monkeypatch)

    assert actuator.withdraw_undeliverable() == []
    assert replays == [1], "the screen check must run on every read"
    assert ledger.get("act1")["state"] == "proposed"


def test_a_moved_screen_is_withdrawn_as_stale(monkeypatch, live):
    _propose(_capture(monkeypatch))
    _count_replays(monkeypatch, screen="✻ Working…")

    assert actuator.withdraw_undeliverable() == ["act1"]
    rec = ledger.get("act1")
    assert rec["state"] == "stale"
    assert rec["detail"] == "the session's screen changed since this was proposed"


def test_output_that_leaves_the_screen_as_it_was_is_kept(monkeypatch, live):
    _propose(_capture(monkeypatch))
    webterm._buffer_append(PHYS, TITLE_BLINK)  # bytes arrived; the rendered screen did not change

    assert actuator.withdraw_undeliverable() == []
    assert ledger.get("act1")["state"] == "proposed"


def test_a_height_only_resize_with_no_new_output_is_withdrawn(monkeypatch, live):
    """The #975 review's third finding, through the real renderer.

    A height-only resize changes `_LAST_ROWS` without resetting the ring or appending a byte,
    and `live_tail_text` renders at that height. A withdrawal that trusted anything but the
    rendered screen would keep offering a proposal delivery refuses.
    """
    monkeypatch.setitem(scrollback._LAST_COLS, PHYS, 20)
    monkeypatch.setitem(scrollback._LAST_ROWS, PHYS, 4)
    webterm._buffer_append(PHYS, b"\x1b[1;1HTOP\x1b[3;1HBOTTOM\x1b[4;1HPROMPT")
    _propose(orchestrator.precondition_for(PHYS))  # the real capture, at four rows
    assert actuator.withdraw_undeliverable() == [], "an unchanged geometry must be kept"

    monkeypatch.setitem(scrollback._LAST_ROWS, PHYS, 2)  # the pane got shorter; no output at all

    assert actuator.withdraw_undeliverable() == ["act1"]
    assert ledger.get("act1")["detail"] == "the session's screen changed since this was proposed"


@pytest.mark.parametrize("verdict", ["alive", "unknown"])
@pytest.mark.parametrize("handoff", ["attach", "detach"])
def test_a_writer_handoff_does_not_withdraw_a_valid_proposal(monkeypatch, master, handoff, verdict):
    """#975 reviews 4836 and 4846. `is_live` means "a writer is registered in this process", and
    attach/detach unregister one writer before registering the next. A poll in that gap used to
    CAS the proposal to `stale` for good — erasing the decision the operator was opening the pane
    to approve. The master probe must PROVE death, so a master that is alive, or that did not
    answer in time (`unknown`), both keep it. Driven through the real registration primitives both
    `SessionStream` and `webterm.run` use, and the real `probe_master` over a real socket."""
    master(verdict)
    outgoing, incoming = (
        ("headless", "attached") if handoff == "attach" else ("attached", "headless")
    )
    fds = [os.openpty() for _ in range(2)]
    try:
        token = session_input.register_writer(PHYS, fds[0][0], threading.Lock(), outgoing)
        _propose(_capture(monkeypatch))

        session_input.unregister_writer(PHYS, token)  # the gap
        assert session_input.is_live(PHYS) is False
        assert actuator.withdraw_undeliverable() == []
        assert ledger.get("act1")["state"] == "proposed"

        session_input.register_writer(PHYS, fds[1][0], threading.Lock(), incoming)
        assert actuator.withdraw_undeliverable() == []
        assert ledger.get("act1")["state"] == "proposed"
        assert actuator.check_precondition(ledger.get("act1")) == (True, "")
    finally:
        for pair in fds:
            for fd in pair:
                with contextlib.suppress(OSError):
                    os.close(fd)


@pytest.mark.parametrize("verdict", ["absent", "dead"])
def test_a_session_whose_master_is_confirmed_gone_is_withdrawn(monkeypatch, master, verdict):
    """Proof of death: no socket at all, or a stale socket whose connect is refused."""
    if verdict == "dead":
        master("dead")
    _propose(_capture(monkeypatch))  # and no writer registered
    replays = _count_replays(monkeypatch)

    assert actuator.withdraw_undeliverable() == ["act1"]
    assert ledger.get("act1")["detail"] == "session is not live"
    assert replays == []  # death never needed the screen


def test_an_unresolvable_master_path_keeps_the_decision(monkeypatch):
    """A lookup error is doubt, not proof — the irreversible step does not take it."""

    def boom(*_a):
        raise actuator.ptybridge.PtyBridgeError("no socket path for this key")

    monkeypatch.setattr(actuator.ptybridge, "socket_path", boom)
    _propose(_capture(monkeypatch))

    assert actuator.withdraw_undeliverable() == []
    assert ledger.get("act1")["state"] == "proposed"


@pytest.mark.parametrize("helper", ["reset", "drop", "seeding hydrate"])
def test_ring_housekeeping_opens_no_screen_change_intervals(helper):
    """The #975 review's first finding was overlap between screen-change publishers: two open
    intervals leave an EVEN epoch, which reads as stable while a mutation is in flight. The append
    path is the only publisher `session_input`'s fence was designed around, so these must not
    become second ones."""
    webterm._buffer_append(PHYS, b"a screen")
    if helper == "seeding hydrate":
        with scrollback._RING_LOCK:
            scrollback._BUFFERS.pop(PHYS, None)
        scrollback._LOADED_FROM_DISK.discard(PHYS)
    before = session_input.current_screen_epoch(PHYS)

    if helper == "reset":
        scrollback._reset_ring(PHYS)
    elif helper == "drop":
        scrollback._drop_buffer(PHYS)
    else:
        scrollback._ensure_loaded(PHYS)
        assert PHYS in scrollback._BUFFERS, "the hydrate did not seed the ring"

    assert session_input.current_screen_epoch(PHYS) == before


def test_a_claimed_action_is_never_withdrawn(monkeypatch):
    _propose(_capture(monkeypatch))
    ledger.claim("act1", ledger.CLAIMABLE_STATES)

    assert actuator.withdraw_undeliverable() == []
    assert ledger.get("act1")["state"] == "claimed"


def test_the_models_own_question_is_not_a_candidate(monkeypatch):
    """`escalated` has nothing to deliver, so there is nothing for withdrawal to refuse."""
    _propose(_capture(monkeypatch), state="escalated", verb="escalate")
    assert actuator.withdraw_undeliverable() == []
    assert ledger.get("act1")["state"] == "escalated"


def test_a_claim_that_lands_first_wins(monkeypatch, live):
    _propose(_capture(monkeypatch))

    def claim_then_mismatch(_phys, _pre):
        ledger.claim("act1", ledger.CLAIMABLE_STATES)  # a delivery claims inside the window
        return False, "the session's screen changed since this was proposed"

    monkeypatch.setattr(actuator, "screen_matches", claim_then_mismatch)

    assert actuator.withdraw_undeliverable() == []
    assert ledger.get("act1")["state"] == "claimed"


def test_withdrawal_uses_the_same_screen_check_as_delivery(monkeypatch, live):
    """One helper, not a near-copy: patching it changes both verdicts."""
    _propose(_capture(monkeypatch))
    monkeypatch.setattr(actuator, "screen_matches", lambda *_a: (False, "sentinel"))

    assert actuator.check_precondition(ledger.get("act1")) == (False, "sentinel")
    assert actuator.withdraw_undeliverable() == ["act1"]
    assert ledger.get("act1")["detail"] == "sentinel"


def test_housekeeping_expires_then_withdraws(monkeypatch):
    _propose(_capture(monkeypatch), id="overdue", expires_at=time.time() - 1)
    _propose(_capture(monkeypatch), id="dead")

    expired, withdrawn = actuator.housekeep_pending()

    assert expired == ["overdue"] and withdrawn == ["dead"]


def test_a_withdrawn_supervisor_nudge_is_free():
    """Withdrawal settles `stale`, which the supervisor's budget does not charge."""
    from agent_sessions import mission_supervisor

    assert "stale" in mission_supervisor._FREE_TERMINAL


# --- C. an explicit operator approval --------------------------------------------------------


def _viewer(**row) -> _Registry:
    return _Registry({"id": PHYS, **row})


def test_an_operator_approval_ignores_only_the_attached_viewer():
    reg = _viewer(attached=True, last_output_at=None)
    assert actuator.check_precondition({"session_id": KEY}, registry=reg)[0] is False
    assert actuator.check_precondition(
        {"session_id": KEY}, registry=reg, operator_approval=True
    ) == (True, "")


def test_recent_output_still_refuses_an_operator_approval():
    reg = _viewer(attached=True, last_output_at=time.time())
    ok, why = actuator.check_precondition({"session_id": KEY}, registry=reg, operator_approval=True)
    assert ok is False and "viewer" in why


def test_a_broken_registry_still_refuses_an_operator_approval():
    class Boom:
        def snapshot(self):
            raise RuntimeError("registry down")

    ok, _why = actuator.check_precondition(
        {"session_id": KEY}, registry=Boom(), operator_approval=True
    )
    assert ok is False


def test_a_changed_screen_still_refuses_an_operator_approval(monkeypatch):
    pre = _capture(monkeypatch)
    monkeypatch.setattr(scrollback, "live_tail_text", lambda *a, **k: "✻ Working…")
    reg = _viewer(attached=True, last_output_at=None)
    ok, why = actuator.check_precondition(
        {"session_id": KEY, "precondition": pre}, registry=reg, operator_approval=True
    )
    assert ok is False and "screen changed" in why


def test_title_only_output_is_eligible_but_an_approval_into_it_is_still_refused(monkeypatch):
    """Eligibility is not deliverability, and this issue does not claim otherwise.

    A codex session blinking its title is NOT busy, so it may be proposed. `_viewer_busy` still
    reads raw `last_output_at`, so the approval is refused as recently active. The actual outcome
    is recorded here; making such a session deliverable is a separate follow-up (#973).
    """
    webterm._buffer_append(PHYS, TITLE_BLINK)
    reg = _viewer(
        attached=True,
        last_output_at=webterm.get_last_output_at(PHYS),
        visible_output_at=webterm.get_last_visible_output_at(PHYS),
    )

    assert actuator.busy_keys(reg) == set()
    ok, why = actuator.check_precondition({"session_id": KEY}, registry=reg, operator_approval=True)
    assert ok is False and why == "a viewer is attached or was just active"


@pytest.mark.parametrize("operator_approval", [True, False])
def test_both_precondition_callbacks_carry_the_same_flag(monkeypatch, live, operator_approval):
    _propose({})
    seen: list[bool] = []
    real = actuator.check_precondition

    def spy(action, *, registry=None, operator_approval=False):
        seen.append(operator_approval)
        return real(action, registry=registry, operator_approval=operator_approval)

    monkeypatch.setattr(actuator, "check_precondition", spy)
    reg = _viewer(attached=True, last_output_at=None)

    rec = asyncio.run(actuator.deliver("act1", registry=reg, operator_approval=operator_approval))

    if operator_approval:
        # Delivered means BOTH callbacks ran and both were told it is the operator's approval.
        assert len(seen) >= 2, f"expected the precondition AND the final guard, saw {seen}"
        assert set(seen) == {True}
        assert rec["state"] == "delivered"
    else:
        # Without the flag the attached viewer refuses at the FIRST callback, so the second is
        # never reached — and it was not handed a flag the route never set.
        assert seen == [False]
        assert rec["state"] == "stale"
