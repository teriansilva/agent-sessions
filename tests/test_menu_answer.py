"""#1060 Phase 3 — answering a session's menu from the console, end to end.

Against the real scrollback ring, a real PTY and the real authenticated route: a tap on option N
delivers the digit ALONE (claude submits on the digit — verified in a real session, see
`tests/fixtures/claude_select_menu.PROVENANCE.md`), only when the menu the card showed is the menu
on the screen NOW, with the same label at that number. Every refusal sends nothing, and never
leaves a deliverable record behind.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import tty
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    engines,
    menu_answer,
    metadata,
    notifications,
    prefs,
    screen_menus,
    scrollback,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app

SID = "claude:22222222-2222-2222-2222-222222222222"
PHYS = engines.physical_key(SID)
FIXTURE = (Path(__file__).parent / "fixtures" / "claude_select_menu_v2_1_280.screen.txt").read_text(
    "utf-8"
)


def _paint(text: str) -> None:
    """Draw ``text`` as a fresh full frame, the way the agent repaints."""
    lines = text.rstrip("\n").split("\n")
    scrollback._buffer_append(PHYS, b"\x1b[H\x1b[2J" + "\r\n".join(lines).encode() + b"\r\n")


def _menu_now() -> dict | None:
    return screen_menus.parse(scrollback.live_tail_text(PHYS, 8000), "claude")


@pytest.fixture
def world(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta())
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())
    session_input.reset()
    master, slave = os.openpty()
    # RAW on the reading side, so a bare "2" is readable and a trailing CR is not rewritten.
    tty.setraw(slave)
    session_input.register_writer(PHYS, master, threading.Lock(), "headless")
    scrollback.note_cols(PHYS, 120)
    scrollback._LAST_ROWS[PHYS] = 40
    _paint(FIXTURE)
    menu = _menu_now()
    assert menu is not None, "the fixture must parse through the real ring"
    ledger.append(
        {
            "id": "esc-1",
            "state": "escalated",
            "verb": "escalate",
            "session_id": SID,
            "confidence": 0.9,
            "ts": time.time(),
            "expires_at": time.time() + 600,
            "observed_prompt": {"prompt_class": "choice", "menu": menu, "observed_at": time.time()},
        }
    )

    app = create_app(auth_cfg)
    app.state.session_registry.snapshot = lambda: [
        {"id": PHYS, "attached": False, "working": False, "last_output_at": None}
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

    def choose(body, action="esc-1"):
        return c.post(
            f"/api/pulse/actions/{action}/choose",
            json=body,
            headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
        )

    try:
        yield choose, slave
    finally:
        session_input.reset()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def _typed(slave: int) -> bytes:
    os.set_blocking(slave, False)
    time.sleep(0.05)
    try:
        return os.read(slave, 4096)
    except BlockingIOError:
        return b""


def _still_open() -> None:
    esc = ledger.get("esc-1")
    assert esc["state"] == "escalated", esc
    assert not esc.get("answered_by") and not esc.get("outcome"), esc
    assert str(esc.get("detail") or "").startswith("your answer was not sent"), esc


def _no_live_choose() -> None:
    """No `choose` may be left in a state a later delivery could claim."""
    for rec in ledger.latest_by_id().values():
        if rec.get("verb") == "choose":
            assert rec.get("state") not in ledger.CLAIMABLE_STATES, rec


def test_a_tap_delivers_the_digit_alone_and_answers_the_escalation(world):
    choose, slave = world
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 200, r.text
    body = r.json()
    # The row the card tapped comes back settled, so the card resolves it by its own id.
    assert body["id"] == "esc-1" and body["state"] == "rejected"
    out = body["choice"]
    assert out["state"] == "delivered"
    assert out["verb"] == "choose" and out["origin"] == "operator" and out["answers"] == "esc-1"
    # THE DIGIT ALONE: claude submits on it, and a CR would land in the agent's next prompt.
    assert _typed(slave) == b"2"
    esc = ledger.get("esc-1")
    assert esc["state"] == "rejected"
    assert esc["outcome"] == menu_answer.ANSWERED_OUTCOME
    assert esc["answered_by"] == out["id"]
    assert esc["detail"] == "you chose 2. Green"


def test_a_label_the_card_did_not_show_is_refused_and_nothing_moves(world):
    choose, slave = world
    r = choose({"option": 2, "label": "Blue"})
    assert r.status_code == 409
    assert "not on the menu this card showed" in r.json()["detail"]
    assert _typed(slave) == b""
    assert ledger.get("esc-1")["state"] == "escalated"
    _no_live_choose()


def test_a_prompt_that_moved_is_refused_and_the_decision_stays_open(world):
    choose, slave = world
    _paint("● Green\n\n❯ \n")  # answered in the terminal: no menu any more
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409
    assert "no longer at that menu" in r.json()["detail"]
    assert _typed(slave) == b""
    assert ledger.get("esc-1")["state"] == "escalated"
    _no_live_choose()


def test_a_menu_renumbered_under_the_card_is_refused(world):
    choose, slave = world
    _paint(FIXTURE.replace("2. Green", "2. Blue!").replace("3. Blue", "3. Green"))
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409
    assert "now reads 'Blue!'" in r.json()["detail"]
    assert _typed(slave) == b""
    _no_live_choose()


def test_a_second_tap_is_refused_once_the_first_answered(world):
    choose, slave = world
    assert choose({"option": 1, "label": "Red"}).status_code == 200
    _typed(slave)
    r = choose({"option": 1, "label": "Red"})
    assert r.status_code == 409
    assert "already rejected" in r.json()["detail"]
    assert _typed(slave) == b""


def test_a_screen_that_moves_between_the_check_and_the_write_sends_nothing(world, monkeypatch):
    """`deliver` re-verifies the frame immediately before the first byte; its refusal must leave the
    new `choose` terminal, never `approved` for somebody else to deliver later."""
    choose, slave = world
    real = menu_answer.prepare

    def prepare_then_move(*a, **kw):
        out = real(*a, **kw)
        _paint(FIXTURE.replace("Which colour do you prefer?", "Which colour, really?"))
        return out

    monkeypatch.setattr(menu_answer, "prepare", prepare_then_move)
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409, r.text
    assert "nothing was sent" in r.json()["detail"]
    assert _typed(slave) == b""
    _no_live_choose()
    # …AND THE DECISION IS STILL OPEN (#1082 review, finding 1): nothing was typed, so the
    # operator's question must not read as answered.
    _still_open()


@pytest.mark.parametrize(
    "body",
    [
        {"option": True, "label": "Red"},
        {"option": "2", "label": "Green"},
        {"option": 0, "label": "Red"},
        {"option": 21, "label": "Red"},
        {"option": 2, "label": ""},
        {"option": 2},
        {"option": 2, "label": "x" * 161},
    ],
)
def test_a_malformed_body_is_a_422_and_touches_nothing(world, body):
    choose, slave = world
    assert choose(body).status_code == 422
    assert ledger.get("esc-1")["state"] == "escalated"
    assert _typed(slave) == b""


def test_only_an_escalation_can_be_answered_this_way(world):
    choose, _slave = world
    ledger.append(
        {
            "id": "prop-1",
            "state": "proposed",
            "verb": "choose",
            "option": 1,
            "session_id": SID,
            "ts": time.time(),
            "expires_at": time.time() + 600,
        }
    )
    r = choose({"option": 1, "label": "Red"}, action="prop-1")
    assert r.status_code == 409
    assert choose({"option": 1, "label": "Red"}, action="nope").status_code == 404


def test_the_digit_alone_is_only_ever_for_an_operator_record():
    base = {"verb": "choose", "option": 3, "submit": "digit"}
    assert actuator.render({**base, "origin": "operator"}, {}) == b"3"
    # A model-built record cannot opt in, whatever fields it carries.
    assert actuator.render({**base, "origin": "model"}, {}) == b"3\r"
    assert actuator.render({"verb": "choose", "option": 3}, {}) == b"3\r"


def test_a_session_that_stops_being_live_before_the_write_reopens_the_decision(world, monkeypatch):
    """A zero-byte `failed` from `deliver` is a refusal, never a 200 (#1082 review, finding 2)."""
    choose, slave = world
    real = menu_answer.prepare

    def prepare_then_unlive(*a, **kw):
        out = real(*a, **kw)
        monkeypatch.setattr(session_input, "is_live", lambda *_a, **_k: False)
        return out

    monkeypatch.setattr(menu_answer, "prepare", prepare_then_unlive)
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409, r.text
    assert "nothing was sent" in r.json()["detail"]
    assert _typed(slave) == b""
    _still_open()
    _no_live_choose()


def test_a_write_the_writer_reports_as_failed_before_any_byte_reopens_the_decision(
    world, monkeypatch
):
    choose, slave = world
    monkeypatch.setattr(
        actuator.session_input,
        "send_input",
        lambda *a, **k: session_input.Outcome("failed", "write closed before any byte was sent"),
    )
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409, r.text
    _still_open()
    _no_live_choose()


def test_a_partial_write_is_indeterminate_and_the_decision_stays_answered(world, monkeypatch):
    """Bytes may be on the PTY: never reopen, never invite a blind retry of a keypress."""
    choose, _slave = world
    monkeypatch.setattr(
        actuator.session_input,
        "send_input",
        lambda *a, **k: session_input.Outcome("aborted", "partial write (closed, 1/2 bytes)"),
    )
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 502, r.text
    assert r.json()["state"] == "indeterminate"
    assert "may or may not have landed" in r.json()["detail"]
    esc = ledger.get("esc-1")
    assert esc["state"] == "rejected" and esc["outcome"] == menu_answer.ANSWERED_OUTCOME


def test_a_choose_that_cannot_be_recorded_reopens_the_decision(world, monkeypatch):
    choose, slave = world
    real_append = ledger.append

    def append(rec, *a, **k):
        if str(rec.get("id", "")).startswith("choose_"):
            raise OSError("disk full")
        return real_append(rec, *a, **k)

    monkeypatch.setattr(menu_answer.ledger, "append", append)
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 502, r.text
    assert "could not be recorded" in r.json()["detail"]
    assert _typed(slave) == b""
    _still_open()


def test_a_settle_lost_after_the_write_is_indeterminate_never_nothing_sent(world, monkeypatch):
    """#1082 review 5022: another settler (the mission-archive sweep, `recover_claimed`) moves the
    CLAIMED choose to `indeterminate` after the digit is typed, so `deliver`'s own settle loses and
    it returns the pre-claim snapshot. That is NOT a zero-byte refusal: the byte is on the PTY."""
    choose, slave = world
    real_send = actuator.session_input.send_input

    def send_then_race(*a, **k):
        out = real_send(*a, **k)
        [rec] = [
            r
            for r in ledger.latest_by_id().values()
            if r.get("verb") == "choose" and r.get("state") == "claimed"
        ]
        ledger.compare_and_set(
            rec["id"], frozenset({"claimed"}), "indeterminate", None, detail="archive sweep"
        )
        return out

    monkeypatch.setattr(actuator.session_input, "send_input", send_then_race)
    r = choose({"option": 2, "label": "Green"})
    assert _typed(slave) == b"2"
    assert r.status_code == 502, r.text
    assert r.json()["state"] == "indeterminate"
    esc = ledger.get("esc-1")
    assert esc["state"] == "rejected" and esc["outcome"] == menu_answer.ANSWERED_OUTCOME


def test_reopening_revives_the_bell_row_the_close_retired(world, monkeypatch, tmp_path):
    choose, _slave = world
    npath = notifications._notifications_path()
    notifications._write(
        npath,
        [
            {
                "id": "n1",
                "ts": time.time(),
                "read": False,
                "title": "t",
                "action_id": "esc-1",
                "session_id": SID,
                "escalation": True,
                "retired": False,
            }
        ],
    )
    monkeypatch.setattr(
        actuator.session_input,
        "send_input",
        lambda *a, **k: session_input.Outcome("failed", "write closed before any byte was sent"),
    )
    r = choose({"option": 2, "label": "Green"})
    assert r.status_code == 409, r.text
    _still_open()
    [row] = notifications._read(npath)
    assert row["retired"] is False and "settled_at" not in row


def test_unretire_touches_only_that_actions_escalation_rows(tmp_path):
    p = tmp_path / "n.json"
    notifications._write(
        p,
        [
            {"id": "a", "action_id": "x", "escalation": True, "retired": True, "settled_at": 1.0},
            {"id": "b", "action_id": "x", "escalation": False, "retired": True, "settled_at": 1.0},
            {"id": "c", "action_id": "y", "escalation": True, "retired": True, "settled_at": 1.0},
        ],
    )
    assert notifications.unretire_for_action("x", p) == 1
    rows = {r["id"]: r for r in notifications._read(p)}
    assert rows["a"]["retired"] is False and "settled_at" not in rows["a"]
    assert rows["b"]["retired"] is True and rows["c"]["retired"] is True
