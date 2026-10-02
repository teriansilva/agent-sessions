"""#1213 — answering an agent's TOOL-PERMISSION dialog from its decision card, end to end.

Against the real scrollback ring (painted with REAL captured bytes), a real PTY and the real
authenticated `/choose` route. A tap sends the kind's proven keys, built server-side from the
option and the LIVE cursor — opencode: `→`×n then Enter; claude: the digit alone — and only while
the dialog on screen is the one the card showed, field for field. Every refusal sends nothing and
reopens the decision; a cursor moved between the tap and the write refuses inside the fence.
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
    metadata,
    missions,
    orchestrator,
    permission_prompts,
    prefs,
    scrollback,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app
from automation_helpers import append_current_action

FIX = Path(__file__).parent / "fixtures"
OC_SID = "opencode:ses_Perm1213Test"
CL_SID = "claude:33333333-3333-3333-3333-333333333333"
GEOM = {OC_SID: (65, 46), CL_SID: (100, 40)}
LONE = "claude:66666666-6666-6666-6666-666666666666"


def _paint(sid: str, name: str) -> None:
    """Replace the session's ring with a real capture — the screen is now exactly that frame."""
    phys = engines.physical_key(sid)
    scrollback._reset_ring(phys)
    scrollback._LAST_ROWS[phys] = GEOM[sid][1]
    scrollback._buffer_append(phys, (FIX / name).read_bytes())


def _escalate(sid: str, esc_id: str = "esc-p") -> dict:
    phys = engines.physical_key(sid)
    observed = orchestrator.observed_prompt_for(phys)
    assert observed["permission"] is not None, "the capture must parse through the real ring"
    return append_current_action(
        {
            "id": esc_id,
            "state": "escalated",
            "verb": "escalate",
            "session_id": sid,
            "engine": engines.parse_key(sid)[0].engine_id,
            "confidence": 1.0,
            "ts": time.time(),
            "expires_at": time.time() + 600,
            "observed_prompt": observed,
        }
    )


@pytest.fixture
def world(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True})
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta())
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())
    session_input.reset()
    fds: dict[str, tuple[int, int]] = {}
    for sid, (cols, _rows) in GEOM.items():
        phys = engines.physical_key(sid)
        master, slave = os.openpty()
        tty.setraw(slave)
        session_input.register_writer(phys, master, threading.Lock(), "headless")
        scrollback.note_cols(phys, cols)
        fds[sid] = (master, slave)

    app = create_app(auth_cfg)
    app.state.session_registry.snapshot = lambda: [
        {"id": engines.physical_key(s), "attached": False, "working": False, "last_output_at": None}
        for s in GEOM
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

    def choose(body, action="esc-p"):
        return c.post(
            f"/api/pulse/actions/{action}/choose",
            json=body,
            headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf},
        )

    def typed(sid: str) -> bytes:
        slave = fds[sid][1]
        os.set_blocking(slave, False)
        time.sleep(0.05)
        try:
            return os.read(slave, 4096)
        except BlockingIOError:
            return b""

    try:
        yield choose, typed
    finally:
        session_input.reset()
        for pair in fds.values():
            for fd in pair:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _no_live_choose() -> None:
    for rec in ledger.latest_by_id().values():
        if rec.get("verb") == "choose":
            assert rec.get("state") not in ledger.CLAIMABLE_STATES, rec


def _still_open(esc_id: str = "esc-p") -> None:
    esc = ledger.get(esc_id)
    assert esc["state"] == "escalated", esc
    assert not esc.get("answered_by"), esc


def test_opencode_reject_sends_two_rights_and_enter(world):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)
    r = choose({"option": 3, "label": "Reject"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "rejected" and body["choice"]["state"] == "delivered"
    assert body["choice"]["submit"] == "permission" and body["choice"]["origin"] == "operator"
    assert typed(OC_SID) == b"\x1b[C\x1b[C\r"


def test_opencode_allow_once_is_enter_alone(world):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_grep.raw")
    _escalate(OC_SID)
    assert choose({"option": 1, "label": "Allow once"}).status_code == 200
    assert typed(OC_SID) == b"\r"


def test_the_keys_start_from_the_LIVE_cursor(world):
    """The cursor moved (by hand) before the tap: the same dialog, so the tap is valid — and the
    keys count from where the cursor IS, not from where the card saw it."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)
    _paint(OC_SID, "opencode_permission_shell_cursor2.raw")
    assert choose({"option": 3, "label": "Reject"}).status_code == 200
    assert typed(OC_SID) == b"\x1b[C\r"


def test_a_different_command_under_the_same_labels_is_refused(world):
    """Grep was on the card; Shell is on the screen. "Allow once" reads the same over both."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_grep.raw")
    _escalate(OC_SID)
    _paint(OC_SID, "opencode_permission_shell.raw")
    r = choose({"option": 1, "label": "Allow once"})
    assert r.status_code == 409
    assert "different permission prompt" in r.json()["detail"]
    assert typed(OC_SID) == b""
    _still_open()
    _no_live_choose()


def test_a_dialog_that_is_gone_is_refused(world):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_grep.raw")
    _escalate(OC_SID)
    _paint(OC_SID, "opencode_permission_shell_truncated.raw")
    r = choose({"option": 1, "label": "Allow once"})
    assert r.status_code == 409
    assert "no longer showing this permission prompt" in r.json()["detail"]
    assert typed(OC_SID) == b""
    _still_open()


def test_a_label_the_card_did_not_show_is_refused(world):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)
    r = choose({"option": 1, "label": "Allow always"})
    assert r.status_code == 409
    assert typed(OC_SID) == b""
    assert ledger.get("esc-p")["state"] == "escalated"


def test_a_second_tap_is_refused(world):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)
    assert choose({"option": 1, "label": "Allow once"}).status_code == 200
    typed(OC_SID)
    r = choose({"option": 1, "label": "Allow once"})
    assert r.status_code == 409
    assert typed(OC_SID) == b""


def test_claude_sends_the_digit_alone(world):
    choose, typed = world
    _paint(CL_SID, "claude_permission_bash.raw")
    _escalate(CL_SID)
    assert choose({"option": 3, "label": "No"}).status_code == 200
    assert typed(CL_SID) == b"3"


def test_claude_a_look_alike_dialog_from_another_call_is_refused(world):
    """The second capture is the SAME command from another session: its description and its
    "don't ask again" path differ. Same labels, same numbers — still a different question."""
    choose, typed = world
    _paint(CL_SID, "claude_permission_bash.raw")
    _escalate(CL_SID)
    _paint(CL_SID, "claude_permission_bash_cursor2.raw")
    r = choose({"option": 1, "label": "Yes"})
    assert r.status_code == 409
    assert typed(CL_SID) == b""
    _still_open()


def test_a_cursor_moved_between_tap_and_write_refuses_inside_the_fence(world):
    """`prepare` pinned the cursor; the operator moved it before byte one. The text fingerprint
    cannot see that (opencode draws the cursor as colour) — the pinned digest does."""
    _paint(OC_SID, "opencode_permission_shell.raw")
    phys = engines.physical_key(OC_SID)
    screen, cells = scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS)
    live = permission_prompts.parse(screen, "opencode", cells)
    pre = {
        "key": phys,
        "screen_fingerprint": orchestrator._screen_fingerprint(screen),
        "prompt_class": orchestrator._prompt_class(screen),
        "permission_digest": permission_prompts.digest(live),
        "engine": "opencode",
    }
    assert actuator.screen_matches(phys, pre) == (True, "")
    _paint(OC_SID, "opencode_permission_shell_cursor2.raw")
    ok, why = actuator.screen_matches(phys, pre)
    assert not ok and "selection moved" in why
    # …and without the digest, the text alone would have let it through.
    assert actuator.screen_matches(
        phys, {k: v for k, v in pre.items() if k != "permission_digest"}
    )[0]


def test_the_escalation_record_carries_the_dialog(world):
    _paint(OC_SID, "opencode_permission_always_stage.raw")
    rec = _escalate(OC_SID)
    perm = rec["observed_prompt"]["permission"]
    assert perm["heading"] == "Always allow" and "- mkdir *" in perm["detail"]
    assert rec["observed_prompt"]["prompt_class"] == "confirm"
    # The menu reader stays blind to it: no autonomous path can see this dialog.
    assert rec["observed_prompt"]["menu"] is None


def test_a_screen_cleared_after_the_tap_refuses_inside_the_fence(world):
    """#1218 review, finding 2: the pre-erase frame must never stand in for the current one."""
    _paint(OC_SID, "opencode_permission_shell.raw")
    phys = engines.physical_key(OC_SID)
    screen, cells = scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS)
    live = permission_prompts.parse(screen, "opencode", cells)
    pre = {
        "key": phys,
        "screen_fingerprint": orchestrator._screen_fingerprint(screen),
        "prompt_class": orchestrator._prompt_class(screen),
        "permission_digest": permission_prompts.digest(live),
        "engine": "opencode",
    }
    assert actuator.screen_matches(phys, pre) == (True, "")
    scrollback._buffer_append(phys, b"\x1b[2J")
    assert scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS) == ("", None)
    ok, _why = actuator.screen_matches(phys, pre)
    assert not ok


def test_leaving_the_alternate_screen_after_the_tap_refuses_inside_the_fence(world):
    """#1218 review 5403, finding 2: the dialog lived on the alternate screen; its exit restores
    the normal buffer, and the old dialog must not be authorised against."""
    _paint(OC_SID, "opencode_permission_shell.raw")
    phys = engines.physical_key(OC_SID)
    screen, cells = scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS)
    live = permission_prompts.parse(screen, "opencode", cells)
    pre = {
        "key": phys,
        "screen_fingerprint": orchestrator._screen_fingerprint(screen),
        "prompt_class": orchestrator._prompt_class(screen),
        "permission_digest": permission_prompts.digest(live),
        "engine": "opencode",
    }
    assert actuator.screen_matches(phys, pre) == (True, "")
    scrollback._buffer_append(phys, b"\x1b[?1049l")
    ok, _why = actuator.screen_matches(phys, pre)
    assert not ok


def _identity(rec: dict) -> str:
    return rec["observed_prompt"]["permission"]["identity"]


def _hold(rec: dict, choose_id: str = "choose_prev") -> None:
    missions.permission_hold_add(
        choose_id, session_key=OC_SID, identity=_identity(rec), mission_id=None, growth_mark=None
    )


def test_an_open_hold_refuses_a_second_answer(world):
    """An earlier answer to this dialog may have landed (#1218 review): nothing is sent."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    rec = _escalate(OC_SID)
    _hold(rec)
    r = choose({"option": 1, "label": "Allow once"})
    assert r.status_code == 409
    assert "may already have reached the session" in r.json()["detail"]
    assert typed(OC_SID) == b""
    _still_open()


def test_a_hold_survives_ledger_compaction(world):
    """#1218 review 5405, finding 1: the hold is not a ledger row, so no history tail drops it."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    rec = _escalate(OC_SID)
    _hold(rec)
    for i in range(ledger.HISTORY_MAX + 1):
        ledger.append(
            {"id": f"old-{i}", "state": "rejected", "verb": "continue", "session_id": LONE}
        )
    ledger.compact()
    assert choose({"option": 1, "label": "Allow once"}).status_code == 409
    assert typed(OC_SID) == b""


def test_an_answer_takes_its_hold_before_the_claim_and_delivery_releases_it(world, monkeypatch):
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    rec = _escalate(OC_SID)
    seen: list = []
    real = actuator.deliver

    async def deliver(action_id, **kw):
        # Held BEFORE the first byte can go out.
        seen.append(missions.permission_holds_open(OC_SID, _identity(rec)))
        return await real(action_id, **kw)

    monkeypatch.setattr(actuator, "deliver", deliver)
    assert choose({"option": 1, "label": "Allow once"}).status_code == 200
    assert typed(OC_SID) == b"\r"
    assert len(seen) == 1 and len(seen[0]) == 1
    assert missions.permission_holds_open(OC_SID) == []  # delivered in full: released


def test_an_uncertain_delivery_keeps_the_hold(world, monkeypatch):
    """The delivering process fails after the claim: the answer may have landed, so the dialog is
    held — a second tap on it sends nothing."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    rec = _escalate(OC_SID)

    async def dies_after_claim(action_id, **kw):
        ledger.claim(action_id, ledger.CLAIMABLE_STATES)
        raise RuntimeError("the delivering process died")

    monkeypatch.setattr(actuator, "deliver", dies_after_claim)
    r = choose({"option": 1, "label": "Allow once"})
    assert r.status_code == 502 and r.json()["state"] == "indeterminate"
    assert len(missions.permission_holds_open(OC_SID, _identity(rec))) == 1
    # A fresh card for the same dialog cannot be answered while the hold stands.
    _escalate(OC_SID, "esc-q")
    monkeypatch.setattr(actuator, "deliver", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    r = choose({"option": 1, "label": "Allow once"}, action="esc-q")
    assert r.status_code == 409


def test_a_zero_byte_refusal_releases_its_own_hold(world):
    """The screen moved between the tap and the write: nothing typed, so nothing is held."""
    choose, typed = world
    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)
    # A viewer attached and typed recently: `deliver` refuses before byte one.
    world_snapshot = [
        {"id": engines.physical_key(OC_SID), "attached": True, "last_output_at": time.time()}
    ]
    import agent_sessions.menu_answer as ma

    orig = ma.actuator.deliver

    async def refusing(action_id, **kw):
        kw["registry"].snapshot = lambda: world_snapshot
        return await orig(action_id, **kw)

    ma.actuator.deliver = refusing
    try:
        r = choose({"option": 1, "label": "Allow once"})
    finally:
        ma.actuator.deliver = orig
    assert r.status_code == 409, r.text
    assert typed(OC_SID) == b""
    assert missions.permission_holds_open(OC_SID) == []


def test_unreadable_holds_refuse(world, monkeypatch):
    """#1218 review 5403, finding 4, on the new store: unreadable is never "nothing is held"."""
    from agent_sessions import menu_answer

    _paint(OC_SID, "opencode_permission_shell.raw")
    _escalate(OC_SID)

    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(missions, "permission_holds_open", broken)
    with pytest.raises(menu_answer.Refused) as e:
        menu_answer.prepare("esc-p", 1, "Allow once")
    assert e.value.status == 503


@pytest.mark.parametrize(
    "tail",
    [
        b"\x1b[46S",  # scroll every row away
        b"\x1b[H\x1b[46M",  # delete every row
        b"\x1b[?01049l",  # the alternate-screen exit, spelled with a leading zero
        b"\x1b[3L",  # insert lines
        b"\x1bM",  # reverse index
        b"\x1bc",  # full reset
        b"\x08",  # backspace
    ],
)
def test_an_unmodelled_screen_mutation_after_the_tap_refuses_inside_the_fence(world, tail):
    """#1218 review 5405, finding 3: the authorisation render fails closed on anything it does not
    model exactly."""
    _paint(OC_SID, "opencode_permission_shell.raw")
    phys = engines.physical_key(OC_SID)
    screen, cells = scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS)
    live = permission_prompts.parse(screen, "opencode", cells)
    pre = {
        "key": phys,
        "screen_fingerprint": orchestrator._screen_fingerprint(screen),
        "prompt_class": orchestrator._prompt_class(screen),
        "permission_digest": permission_prompts.digest(live),
        "engine": "opencode",
    }
    assert actuator.screen_matches(phys, pre) == (True, "")
    scrollback._buffer_append(phys, tail)
    assert scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS) == ("", None)
    assert actuator.screen_matches(phys, pre)[0] is False
