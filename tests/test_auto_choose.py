"""#1060 Phase 4 — mission control answers an engine's menu on its own, and only under the grant.

The grant, all of which must hold: the session's mission opted in (`missions.auto_choose`) and is
running; orchestration is on at `yolo`; the screen is the engine's own menu parsed server-side (a
permission dialog never is); the option is on that menu and still carries the label the pass bound;
the confidence clears the higher of `confidence_min` and `ORCH_AUTO_CHOOSE_CONF_LO`. The global
ceiling `AUTO_VERBS_V1` is untouched.

Driven end to end against the real scrollback ring, a real raw PTY and a real missions store.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import tty
from pathlib import Path

import pytest

from agent_sessions import (
    actuator,
    engines,
    metadata,
    missions,
    orchestrator,
    prefs,
    scrollback,
    session_input,
)
from agent_sessions import orchestrator_ledger as ledger
from automation_helpers import current_action

SID = "claude:44444444-4444-4444-4444-444444444444"
PHYS = engines.physical_key(SID)
FIXTURES = Path(__file__).parent / "fixtures"
MENU = (FIXTURES / "claude_select_menu_v2_1_280.screen.txt").read_text("utf-8")


def _paint(text: str) -> None:
    lines = text.rstrip("\n").split("\n")
    scrollback._buffer_append(PHYS, b"\x1b[H\x1b[2J" + "\r\n".join(lines).encode() + b"\r\n")


def _typed(fd: int) -> bytes:
    os.set_blocking(fd, False)
    time.sleep(0.05)
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""


def _running_mission(opt_in: bool = True) -> str:
    mid = missions.create_mission("pick a colour", cwd="/repo")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, SID, role="primary")
    if opt_in:
        missions.set_auto_choose(mid, True)
    return mid


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "p.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("AGENT_SESSIONS_NOTIFICATIONS", str(tmp_path / "n.json"))
    missions.reset_schema_cache_for_test()
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    monkeypatch.setattr(actuator.metadata, "resolve_key", lambda k: k)
    monkeypatch.setattr(actuator.metadata, "get", lambda *a, **k: metadata.SessionMeta())
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())
    session_input.reset()
    master, slave = os.openpty()
    tty.setraw(slave)
    session_input.register_writer(PHYS, master, threading.Lock(), "headless")
    scrollback.note_cols(PHYS, 120)
    scrollback._LAST_ROWS[PHYS] = 40
    _paint(MENU)
    try:
        yield slave
    finally:
        session_input.reset()
        missions.reset_schema_cache_for_test()
        for fd in (master, slave):
            with contextlib.suppress(OSError):
                os.close(fd)


def _approved_record(option: int = 2, confidence: float = 0.95) -> dict:
    """What `run_pass` writes for an auto-approved choose: the context read from the live frame."""
    ctx = orchestrator.auto_choose_context(SID, option, _seen())
    assert ctx is not None
    rec = {
        "id": f"act_{option}_{int(confidence * 100)}",
        "verb": "choose",
        "option": option,
        "session_id": SID,
        "state": "approved",
        "confidence": confidence,
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "auto_choose": True,
        "mission_id": ctx["mission_id"],
        "label": ctx["label"],
        "menu": ctx["menu"],
        "precondition": ctx["precondition"],
        "submit": "digit",
    }
    rec = current_action(rec)
    ledger.append(rec)
    return rec


# ---- the store: the opt-in -----------------------------------------------------------------------


def test_the_opt_in_is_off_by_default_and_only_a_real_boolean_sets_it(world):
    mid = _running_mission(opt_in=False)
    assert missions.get_mission(mid)["auto_choose"] is False
    assert missions.auto_choose_mission(SID) is None
    for bad in (1, "true", None):
        with pytest.raises(missions.MissionError) as e:
            missions.set_auto_choose(mid, bad)
        assert e.value.status == 422
    missions.set_auto_choose(mid, True)
    assert missions.get_mission(mid)["auto_choose"] is True
    assert missions.auto_choose_mission(SID) == mid


def test_only_a_running_mission_that_still_holds_the_session_grants_it(world):
    mid = _running_mission()
    assert missions.auto_choose_mission(SID) == mid
    missions.set_state(mid, "running", "done", outcome="done")
    assert missions.auto_choose_mission(SID) is None


def test_a_v30_store_upgrades_to_the_column_off_for_existing_missions(world):
    """A genuine prior-schema store (review 5374, note): a v30 file has no `auto_choose`.

    Opening it adds the column, off for the mission already there; a second run is a no-op.
    """
    import sqlite3

    mid = _running_mission(opt_in=False)
    db = os.environ["AGENT_SESSIONS_MISSIONS_DB"]
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER IF EXISTS automation_owner_menu")
    con.execute("ALTER TABLE missions DROP COLUMN auto_choose")
    con.execute("PRAGMA user_version=30")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    assert missions.get_mission(mid)["auto_choose"] is False  # opening the store migrates it
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(missions)")]
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION
    con.close()
    assert cols[-1] == "auto_choose"  # appended, where a fresh install also puts it last
    with contextlib.closing(missions._ready()) as c:
        missions._migrate_30_to_31(c)  # idempotent
    assert missions.get_mission(mid)["auto_choose"] is False


# ---- the pass: what may be approved --------------------------------------------------------------


def test_the_context_binds_the_label_and_the_frame(world):
    mid = _running_mission()
    ctx = orchestrator.auto_choose_context(SID, 2, _seen())
    assert ctx["mission_id"] == mid
    assert ctx["label"] == "Green"
    assert ctx["precondition"]["prompt_class"] == "choice"
    assert ctx["precondition"]["screen_fingerprint"] == orchestrator._screen_fingerprint(
        scrollback.live_tail_text(PHYS, orchestrator.PROMPT_SCREEN_CHARS)
    )


def test_no_context_without_the_opt_in_a_menu_or_the_option(world):
    mid = _running_mission(opt_in=False)
    seen = _seen()
    assert orchestrator.auto_choose_context(SID, 2, seen) is None  # not opted in
    missions.set_auto_choose(mid, True)
    assert orchestrator.auto_choose_context(SID, 9, seen) is None  # not on the menu
    _paint("● all done\n\n❯ \n")
    assert orchestrator.auto_choose_context(SID, 2, seen) is None  # no menu at all


def test_a_permission_dialog_is_never_a_menu_the_pass_may_answer(world):
    _running_mission()
    seen = _seen()
    _paint(MENU.replace("Which colour do you prefer?", "Do you want to proceed?"))
    assert orchestrator.auto_choose_context(SID, 1, seen) is None


@pytest.mark.parametrize(
    "cfg, auto, conf, want",
    [
        ({"autonomy": "yolo", "confidence_min": 0.75}, True, 0.95, "approved"),
        # Its own floor holds even when the operator's threshold is lower.
        ({"autonomy": "yolo", "confidence_min": 0.75}, True, 0.85, "escalated_low_confidence"),
        # …and the operator's threshold holds when it is higher.
        ({"autonomy": "yolo", "confidence_min": 0.97}, True, 0.95, "escalated_low_confidence"),
        # No opt-in: a choose is outside the ceiling, a tap as ever.
        ({"autonomy": "yolo", "confidence_min": 0.75}, False, 0.99, "proposed"),
        # Not yolo: nothing is autonomous, opt-in or not.
        ({"autonomy": "suggest", "confidence_min": 0.75}, True, 0.99, "proposed"),
    ],
)
def test_decide_approves_a_choose_only_under_the_grant(cfg, auto, conf, want):
    cfg = {"enabled": True, "allowed_verbs": ["continue"], **cfg}
    action = {"verb": "choose", "option": 2, "confidence": conf}
    assert orchestrator._decide(action, cfg, auto_choose=auto)[0] == want


def test_the_global_ceiling_is_untouched():
    assert frozenset({"continue"}) == prefs.AUTO_VERBS_V1


# ---- the write: what is typed --------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_approved_answer_types_the_digit_alone(world):
    slave = world
    _running_mission()
    rec = _approved_record(2)
    out = await actuator.deliver_auto(rec)
    assert out is not None and out["state"] == "delivered", out
    assert _typed(slave) == b"2"


@pytest.mark.anyio
async def test_turning_the_opt_in_off_withdraws_an_answer_already_approved(world):
    slave = world
    mid = _running_mission()
    rec = _approved_record(2)
    missions.set_auto_choose(mid, False)
    out = await actuator.deliver_auto(rec)
    assert out is None or out["state"] == "stale"
    assert _typed(slave) == b""


@pytest.mark.anyio
async def test_a_menu_that_changed_under_the_answer_types_nothing(world):
    slave = world
    _running_mission()
    rec = _approved_record(2)
    _paint(MENU.replace("2. Green", "2. Purple"))
    out = await actuator.deliver_auto(rec)
    assert out is None or out["state"] != "delivered", out
    assert _typed(slave) == b""


@pytest.mark.anyio
async def test_leaving_yolo_withdraws_it(world):
    slave = world
    _running_mission()
    rec = _approved_record(2)
    prefs.set_orchestrator({"autonomy": "suggest"})
    out = await actuator.deliver_auto(rec)
    assert out is None or out["state"] == "stale"
    assert _typed(slave) == b""


@pytest.mark.anyio
async def test_a_record_claiming_the_grant_for_a_mission_that_never_opted_in_is_refused(world):
    slave = world
    _running_mission(opt_in=False)
    ctx_mid = missions.create_mission("other", cwd="/repo")["id"]
    rec = {
        "id": "forged",
        "verb": "choose",
        "option": 2,
        "session_id": SID,
        "state": "approved",
        "confidence": 0.99,
        "ts": time.time(),
        "expires_at": time.time() + 600,
        "auto_choose": True,
        "mission_id": ctx_mid,
        "label": "Green",
    }
    ledger.append(rec)
    assert actuator.choose_auto_allowed(rec, prefs.get_orchestrator()) is False
    out = await actuator.deliver_auto(rec)
    assert out is None or out["state"] == "stale"
    assert _typed(slave) == b""


@pytest.mark.parametrize(
    "over",
    [
        {"confidence": True},
        {"confidence": 1.5},
        {"confidence": 0.89},
        {"option": True},
        {"option": 0},
        {"label": ""},
        {"mission_id": ""},
        {"auto_choose": "yes"},
    ],
)
def test_the_grant_refuses_every_malformed_or_underconfident_field(world, over):
    _running_mission()
    rec = {**_approved_record(2), **over}
    assert actuator.choose_auto_allowed(rec, prefs.get_orchestrator()) is False


def test_digit_only_is_for_the_operator_or_an_autonomous_menu_answer_nothing_else():
    base = {"verb": "choose", "option": 3, "submit": "digit"}
    assert actuator.render({**base, "auto_choose": True}, {}) == b"3"
    assert actuator.render({**base, "origin": "operator"}, {}) == b"3"
    assert actuator.render({**base, "origin": "model"}, {}) == b"3\r"
    assert actuator.render({**base, "auto_choose": "true"}, {}) == b"3\r"


@pytest.mark.anyio
async def test_a_delivered_answer_is_written_on_the_missions_thread(world, monkeypatch):
    slave = world
    mid = _running_mission()
    rec = _approved_record(2)
    monkeypatch.setattr(actuator, "DELIVERY_SPACING_S", 0)
    out = await actuator.deliver_pass_actions([rec])
    assert [r["state"] for r in out] == ["delivered"]
    assert _typed(slave) == b"2"
    rows = [e for e in missions.get_mission(mid)["events"] if e["kind"] == "action"]
    assert rows and rows[0]["text"] == "mission control answered the menu itself: 2. Green"
    assert rows[0]["meta"]["auto_choose"] is True and rows[0]["session_key"] == SID


# ---- the route: the operator's switch ------------------------------------------------------------


@pytest.fixture
def client(world, auth_cfg, fake_jsonl):  # noqa: ARG001
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]

    def patch(mid, body, *, with_csrf=True):
        headers = {"Origin": auth_cfg.origin}
        if with_csrf:
            headers["X-CSRF-Token"] = csrf
        return c.patch(f"/api/missions/{mid}/autonomy", json=body, headers=headers)

    return patch


def test_the_route_turns_it_on_and_off(client):
    mid = _running_mission(opt_in=False)
    r = client(mid, {"auto_choose": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": mid, "auto_choose": True}
    assert missions.auto_choose_mission(SID) == mid
    assert client(mid, {"auto_choose": False}).json()["auto_choose"] is False
    assert missions.auto_choose_mission(SID) is None


@pytest.mark.parametrize(
    "body",
    [{"auto_choose": 1}, {"auto_choose": "true"}, {}, {"auto_choose": True, "extra": 1}],
)
def test_the_route_takes_exactly_a_boolean(client, body):
    mid = _running_mission(opt_in=False)
    assert client(mid, body).status_code == 422
    assert missions.get_mission(mid)["auto_choose"] is False


def test_the_route_needs_csrf_and_a_real_mission(client):
    mid = _running_mission(opt_in=False)
    assert client(mid, {"auto_choose": True}, with_csrf=False).status_code in (401, 403)
    assert missions.get_mission(mid)["auto_choose"] is False
    assert client("msn_" + "0" * 32, {"auto_choose": True}).status_code == 404


# ---- review 5374: the races ----------------------------------------------------------------------


def _seen() -> dict:
    """The menu as the model saw it: the digest form of the live menu."""
    from agent_sessions import screen_menus

    return orchestrator._digest_menu(
        screen_menus.parse(scrollback.live_tail_text(PHYS, 8000), "claude")
    )


def test_a_menu_that_changed_after_the_model_saw_it_is_never_answered(world):
    """Finding 1: the model chose "2" against "2. Green"; by the time the pass records it, 2 reads
    "Purple". No autonomous answer — the number means something the model never saw."""
    _running_mission()
    seen = _seen()
    _paint(MENU.replace("2. Green", "2. Purple"))
    assert orchestrator.auto_choose_context(SID, 2, seen) is None
    # …and a menu the model saw but the context was not given one for is not answerable either.
    _paint(MENU)
    assert orchestrator.auto_choose_context(SID, 2, None) is None
    assert orchestrator.auto_choose_context(SID, 2, seen)["label"] == "Green"


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [False, True])
async def test_mission_reading_only_types_the_menu_the_model_saw(world, monkeypatch, changed):
    # #1019 moves the approved grant out of the standalone pass into the existing mission call.
    from agent_sessions import mission_choices, mission_supervisor, review

    mid = _running_mission()
    monkeypatch.setattr(review, "_require_config", lambda: {})
    monkeypatch.setattr(review, "gather_input", lambda *a: ("agent menu", "input-1"))

    async def model(messages, **kwargs):
        assert '"label": "Green"' in messages[-1]["content"]
        if changed:
            _paint(MENU.replace("2. Green", "2. Purple"))
        return {"choose": {"option": 2, "confidence": 0.97, "reason": "Green"}}

    monkeypatch.setattr(review, "complete_json", model)
    reading = await mission_supervisor.consider(mid, SID)
    out = await mission_choices.propose(mid, SID, reading)
    if changed:
        assert out is None
        assert ledger.latest_by_id() == {}
        assert _typed(world) == b""
    else:
        assert out["state"] == "delivered" and out["auto_choose"] is True
        assert out["mission_id"] == mid and out["authority"]["scope"] == "mission"
        assert out["menu"] == _seen() and out["label"] == "Green"
        assert _typed(world) == b"2"


@pytest.mark.anyio
async def test_a_withdrawal_after_the_final_guard_still_stops_the_write(world, monkeypatch):
    """Finding 2: the opt-in is withdrawn AFTER the write fence's final guard passed and before
    byte one — here by a sibling instance writing the store directly, which no in-process lock can
    see. The in-fence fingerprint carries the grant, so nothing is typed."""
    slave = world
    mid = _running_mission()
    rec = _approved_record(2)
    real_send = actuator.session_input.send_input

    def send(*a, **k):
        guard = k["final_guard"]

        def guard_then_withdraw():
            verdict = guard()
            with contextlib.closing(missions._ready()) as con:
                con.execute("UPDATE missions SET auto_choose=0 WHERE id=?", (mid,))
                con.commit()
            return verdict

        k["final_guard"] = guard_then_withdraw
        return real_send(*a, **k)

    monkeypatch.setattr(actuator.session_input, "send_input", send)
    out = await actuator.deliver_auto(rec)
    assert out is None or out["state"] != "delivered", out
    assert _typed(slave) == b""


def test_turning_it_off_commits_inside_the_write_fence(world, monkeypatch):
    """Finding 2, same process: the setter holds `policy_transaction()`, so it cannot land between
    a write's checks and its byte — it waits for the write, or the write sees the new epoch."""
    mid = _running_mission()
    from agent_sessions import session_input as si

    entered: list = []
    real = si.policy_transaction

    def spy(scope=None):
        entered.append(scope)
        return real(scope)

    monkeypatch.setattr(si, "policy_transaction", spy)
    missions.set_auto_choose(mid, False)
    assert entered == ["mission"]
