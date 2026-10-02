"""#1049 / #973 — an Approve tapped in the mission console AFTER the operator looked at the pane.

The session pane's decision strip is gone (#1049), but the mission console's `ActionRow` still
offers **Open session** beside Approve, and the bell and push deep-link every decision to `/s/…`.
So the operator can still: open the pane, look, go back to the console, tap Approve.

This file answers, against the real scrollback ring and the real authenticated approve route,
whether that approval survives. The two halves are deliberately separate:

* **Attaching alone** is covered by the `operator_approval` exemption in `_viewer_busy` (#969):
  an attached viewer does not refuse an explicit approval. Pinned green below.
* **Attaching at another width** makes the agent repaint for that width. The ring is reset and
  re-authored (`scrollback.note_attach_width`), the replayed screen is a different frame, and
  the precondition's screen fingerprint no longer matches — so the approval comes back 409
  stale. That is the #1049 failure on the one Approve surface left. It is NOT fixed here: the
  guard is #973's. The test is `xfail(strict=True)`, so it turns red (and must be un-marked) the
  day #973 makes it pass.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    engines,
    metadata,
    orchestrator,
    prefs,
    scrollback,
    session_input,
)
from agent_sessions.main import create_app
from automation_helpers import append_current_action

SID = "claude:11111111-1111-1111-1111-111111111111"
PHYS = engines.physical_key(SID)


def _frame(cols: int) -> bytes:
    """A claude-style permission prompt drawn for a `cols`-wide terminal: a box as wide as the
    screen, the question, and a numbered choice. What an agent repaints on SIGWINCH."""
    inner = cols - 2
    lines = [
        "╭" + "─" * inner + "╮",
        "│" + " Do you want to run the migration?".ljust(inner) + "│",
        "╰" + "─" * inner + "╯",
        "› 1. Yes",
        "  2. No",
    ]
    return b"\x1b[H\x1b[2J" + "\r\n".join(lines).encode() + b"\r\n"


@pytest.fixture
def world(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta())
    # Fresh geometry trackers: the rows/cols the renderer reads are module state.
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())
    session_input.reset()
    master, slave = os.openpty()
    session_input.register_writer(PHYS, master, threading.Lock(), "attached")

    # THE PROPOSAL, made while the session ran headless at 120 columns.
    scrollback.note_cols(PHYS, 120)
    scrollback._LAST_ROWS[PHYS] = 30
    scrollback._buffer_append(PHYS, _frame(120))
    append_current_action(
        {
            "id": "act-1049",
            "state": "proposed",
            "verb": "choose",
            "option": 1,
            "session_id": SID,
            "confidence": 0.9,
            "ts": time.time(),
            "expires_at": time.time() + 600,
            "precondition": orchestrator.precondition_for(PHYS),
        }
    )

    app = create_app(auth_cfg)
    # The operator's pane is attached. Its repaint is old news by the time they tap Approve in
    # the console — they looked, went back, and decided — so no RECENT output refuses the write.
    app.state.session_registry.snapshot = lambda: [
        {"id": PHYS, "attached": True, "working": False, "last_output_at": None}
    ]
    c = TestClient(app, base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]

    def approve():
        return c.post(
            "/api/pulse/actions/act-1049/approve",
            headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
        )

    try:
        yield approve, slave
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def _typed(slave: int) -> bytes:
    os.set_blocking(slave, False)
    try:
        return os.read(slave, 4096)
    except BlockingIOError:
        return b""


def test_opening_the_pane_at_the_same_width_does_not_refuse_the_console_approval(world):
    """The attached half: the #969 exemption covers a viewer that merely attached."""
    approve, slave = world
    scrollback.note_attach_width(PHYS, 120)  # the viewer happens to match: no reset, no repaint

    r = approve()

    assert r.status_code == 200, r.text
    assert r.json()["state"] == "delivered"
    # The pty line discipline maps CR to NL on the reading side; the keypress is the "1".
    assert _typed(slave).startswith(b"1")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#973: a viewer attaching at another width makes the agent repaint, the precondition's "
        "screen fingerprint no longer matches, and the approval is refused as stale. Removing "
        "the pane's strip (#1049) does not change this; the guard fix is #973's."
    ),
)
def test_opening_the_pane_at_another_width_then_approving_in_the_console_delivers(world):
    """The exact scenario: push → pane (a phone, 60 columns) → agent repaints → back to the
    console → Approve. Expected by the operator: delivered. Observed today: 409 stale."""
    approve, slave = world
    scrollback.note_attach_width(PHYS, 60)  # different width: the ring is reset…
    scrollback._LAST_ROWS[PHYS] = 40
    scrollback._buffer_append(PHYS, _frame(60))  # …and the agent repaints for the new width

    r = approve()

    assert r.status_code == 200, r.text
    assert r.json()["state"] == "delivered"
    # The pty line discipline maps CR to NL on the reading side; the keypress is the "1".
    assert _typed(slave).startswith(b"1")
