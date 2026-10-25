"""#1681 P2 — Needs-you decisions by voice: one row per challenge, through the screen's routes.

The rows are real Needs-you rows (`needs_you.build` over the real permission captures). Pinned:

* a permission dialog readable as a whole is offered with "refuse it", and "allow it" only with
  an identified single-use option; each settles through `/choose` with that RECORDED option's
  number and label — the screen's payload; a broader grant can never be said;
* a menu's option is named by its number, of the row being offered only; past its options, void;
* a question's answer is sent with "confirm" through `/approve`, as before;
* one row per challenge: "skip" moves on, a settled row moves on, anything else voids;
* unarmed, late and non-English replies, and a tap, void the challenge;
* what is said after comes from the route's result, and a refusal is read as the route gave it;
* at the LIVE route boundary (the real app, a real PTY, real captures): a changed dialog under
  the same labels is refused with nothing typed, and a tap racing a voice decision settles once.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import tty

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    actuator,
    asgi_call,
    engines,
    metadata,
    missions,
    permission_prompts,
    prefs,
    scrollback,
    session_input,
    voice_calls,
    voice_text,
)
from agent_sessions import orchestrator_ledger as ledger
from agent_sessions.main import create_app
from test_permission_answer import CL_SID, GEOM, OC_SID, _escalate, _paint
from test_voice_confirm import Rig, run
from test_voice_needs_you import BASH_SAID, CONFIRM, MENU, _card, _escalation, _perm, _rows

DELIVERED = {"state": "rejected", "choice": {"state": "delivered"}}


def _choose_routes(r: Rig, monkeypatch, *, status: int = 200, body: dict | None = None) -> None:
    """`/choose` answers like the real route: the settled escalation, the choice under `choice`."""
    rig_call = asgi_call.call

    async def fake(app, operator, method, path, b, **kw):
        if path.endswith("/choose"):
            r.routes.append((method, path, b))
            return asgi_call.Response(status, DELIVERED if body is None else body)
        return await rig_call(app, operator, method, path, b, **kw)

    monkeypatch.setattr(asgi_call, "call", fake)


def _posts(r: Rig) -> list[tuple[str, object]]:
    return [(p, b) for m, p, b in r.routes if m == "POST"]


P_SID, M_SID, Q_SID = "claude:p1", "codex:m1", "claude:q1"


def _permission_body(rec: dict | None = None, sid: str = P_SID) -> dict:
    rec = rec or _perm("claude_permission_bash.raw")
    return _rows([_card(sid, "deploy", "infra")], [_escalation(sid, 900.0, permission=rec)],
                 {sid: CONFIRM})  # fmt: skip


def _menu_body() -> dict:
    esc = _escalation(M_SID, 700.0, menu=MENU)
    return _rows([_card(M_SID, "docs")], [esc], {M_SID: {"prompt_class": "choice", "menu": MENU}})


def _rig(monkeypatch, needs: dict, **routes) -> Rig:
    r = Rig(monkeypatch)
    r.needs_body = needs
    _choose_routes(r, monkeypatch, **routes)
    return r


# --- the grammar -------------------------------------------------------------------------------

PERM_CARD = {"kind": "permission", "allow": {"option": 1, "label": "Yes", "as": "allow"},
             "deny": {"option": 3, "label": "No", "as": "refuse"}}  # fmt: skip


@pytest.mark.parametrize(
    "said, verdict, option",
    [
        ("Allow it.", "choose", 1),
        ("Yes, allow it once.", "choose", 1),
        ("okay allow", "choose", 1),
        ("Refuse it.", "choose", 3),
        ("deny", "choose", 3),
        ("Reject it please", "choose", 3),
        ("Skip.", "skip", None),
        ("next one", "skip", None),
        ("No.", "cancel", None),
        ("No, refuse it.", "cancel", None),  # starts with a no: leave it on the screen
        ("Allow all.", "broader", None),
        ("Refuse all.", "all", None),
        # broader grants and anything else: never a decision
        ("Allow always.", "broader", None),
        ("Yes, and don't ask again.", "other", None),
        ("Allow it for this session", "broader", None),
        ("Confirm.", "other", None),
        ("Yes.", "other", None),
        ("Allow it?", "other", None),
        ("The second one.", "other", None),
        ("Don't allow it.", "other", None),
        ("Allow it, but not rm", "other", None),
    ],
)
def test_a_permission_reply_is_one_named_answer(said, verdict, option):
    v, choice = voice_text.classify_decision(said, PERM_CARD)
    assert v == verdict
    assert (choice or {}).get("option") == option


def test_allow_is_never_a_decision_without_a_single_use_option():
    card = dict(PERM_CARD, allow=None)
    assert voice_text.classify_decision("Allow it.", card) == ("no_allow", None)
    assert voice_text.classify_decision("Refuse it.", card)[1]["option"] == 3


MENU_CARD = {
    "kind": "menu",
    "options": [
        {"option": 1, "label": "main", "as": "pick"},
        {"option": 2, "label": "release 2.0", "as": "pick"},
    ],
}


@pytest.mark.parametrize(
    "said, option",
    [
        ("The second one.", 2),
        ("option two", 2),
        ("Pick the first.", 1),
        ("2", 2),
        ("One.", 1),
        ("Option one.", 1),
        ("Choose one.", None),  # may mean "any one": never option one
        ("pick one", None),
        ("The third one.", None),  # past the menu's options: void
        ("five", None),
        ("The second one, I think", None),
        ("second or first", None),
        ("main", None),  # a label is not a number: only the number is heard
        ("Allow it.", None),
    ],
)
def test_a_menu_reply_names_one_of_its_own_options_by_number(said, option):
    v, choice = voice_text.classify_decision(said, MENU_CARD)
    assert (choice or {}).get("option") == option
    assert v == ("choose" if option else "other")


# --- permission dialogs ------------------------------------------------------------------------


def test_a_permission_is_read_back_and_allowed_once_through_choose(monkeypatch):
    r = _rig(monkeypatch, _permission_body())

    async def go():
        await r.ask("What needs me?")
        # the WHOLE dialog — every option, the broader grant included — is SAID before "allow
        # it" can settle (Hermes 7021): in the answer, and again in the read-back that arms
        assert (
            r.said_text()[-2]
            == f"1 thing needs you. deploy in infra {BASH_SAID} Let's decide that one."
        )
        assert r.said_text()[-1] == (
            f"deploy in infra {BASH_SAID} Say allow it to allow it once, refuse it, or skip."
        )
        assert "two, Yes, and don't ask again for mkdir" in r.said_text()[-1]
        await r.readback_played()
        await r.ask("Allow it.")

    run(go())
    assert _posts(r)[-1] == (
        "/api/pulse/actions/act-claude:p1/choose",
        {"option": 1, "label": "Yes"},
    )
    assert r.said_text()[-1] == "Allowed, once."


def test_a_permission_is_refused_through_choose_with_its_recorded_refusal(monkeypatch):
    r = _rig(monkeypatch, _permission_body(_perm("opencode_permission_shell.raw"), "opencode:p1"))

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Refuse it.")

    run(go())
    assert _posts(r)[-1] == (
        "/api/pulse/actions/act-opencode:p1/choose",
        {"option": 3, "label": "Reject"},
    )
    assert r.said_text()[-1] == "Refused."


def test_without_a_single_use_allow_only_refuse_is_offered(monkeypatch):
    rec = _perm("claude_permission_bash.raw")
    rec = dict(rec, options=[dict(o) for o in rec["options"]])
    rec["options"][0]["persistent"] = True  # its "Yes" grants more than this one call
    r = _rig(monkeypatch, _permission_body(rec))

    async def go():
        await r.ask("What needs me?")
        assert r.said_text()[-1].endswith("Say refuse it, or skip.")
        await r.readback_played()
        await r.ask("Allow it.")  # not on offer: HQ says so, and nothing is chosen
        assert r.said_text()[-1] == (
            "I can't allow this one by voice, Commander. Say refuse it, or skip."
        )
        assert r.call.challenge["armed_at"] is None  # re-arms only once that line has played
        await r.readback_played()
        await r.ask("Refuse it.")

    run(go())
    assert _posts(r) == [(f"/api/pulse/actions/act-{P_SID}/choose", {"option": 3, "label": "No"})]
    assert "Allow it." not in r.asked


@pytest.mark.parametrize("said", ["Allow always.", "Yes, and don't ask again.", "Confirm."])
def test_a_broader_grant_or_a_bare_confirm_never_answers_a_dialog(said, monkeypatch):
    r = _rig(monkeypatch, _permission_body())

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))


def test_a_dialog_that_cannot_be_read_whole_is_never_offered_refuse_included(monkeypatch):
    rec = _perm("claude_permission_bash.raw")
    rec = dict(rec, detail="git log | head -5")
    r = _rig(monkeypatch, _permission_body(rec))

    async def go():
        await r.ask("What needs me?")
        assert r.call.challenge is None

    run(go())
    assert r.said_text()[-1].endswith("wants a permission I can't read out — it's on your screen.")


def test_a_route_refusal_is_read_as_the_route_gave_it(monkeypatch):
    detail = (
        "nothing was sent: the session is showing a different permission prompt now (the command"
        " or its options changed) — open it to see what it asks"
    )
    r = _rig(monkeypatch, _permission_body(), status=409, body={"detail": detail})

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Allow it.")

    run(go())
    # a FIXED sentence: the route's detail can quote the agent's own label (#1683 review)
    assert r.said_text()[-1] == (
        "Nothing was sent, Commander. The session changed, or it was already decided. It's on"
        " your screen."
    )
    settled = [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "settled"]
    assert settled[-1]["ok"] is False


def test_a_200_without_a_delivered_choice_is_not_success(monkeypatch):
    r = _rig(
        monkeypatch, _permission_body(), body={"state": "rejected", "choice": {"state": "failed"}}
    )

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Refuse it.")

    run(go())
    assert r.said_text()[-1] == "It wasn't sent, Commander. Check the session."


# --- menus and questions -----------------------------------------------------------------------


def test_a_menu_option_is_chosen_by_its_number(monkeypatch):
    r = _rig(monkeypatch, _menu_body())

    async def go():
        await r.ask("What needs me?")
        assert r.said_text()[-1] == (
            "docs in web asks: Which branch should I rebase onto? The options are: one, main;"
            " two, release 2.0. Say the option's number, or skip."
        )
        await r.readback_played()
        await r.ask("The second one.")

    run(go())
    assert _posts(r)[-1] == (
        "/api/pulse/actions/act-codex:m1/choose",
        {"option": 2, "label": "release 2.0"},
    )
    assert r.said_text()[-1] == "Chosen: release 2.0."


def test_a_number_past_the_menu_voids_it(monkeypatch):
    r = _rig(monkeypatch, _menu_body())

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("The third one.")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.call.challenge is None


# --- one row at a time -------------------------------------------------------------------------


def _three() -> dict:
    cards = [_card(P_SID, "deploy", "infra"), _card(M_SID, "docs"), _card(Q_SID, "api-rebase")]
    pending = [
        _escalation(P_SID, 900.0, permission=_perm("claude_permission_bash.raw")),
        _escalation(M_SID, 800.0, menu=MENU),
        {"id": "ans-q", "session_id": Q_SID, "verb": "answer", "state": "pending",
         "can_approve": True, "can_reject": True, "ts": 700.0, "answer": "Use main."},
    ]  # fmt: skip
    screens = {P_SID: CONFIRM, M_SID: {"prompt_class": "choice", "menu": MENU},
               Q_SID: {"prompt_class": "question", "menu": None}}  # fmt: skip
    return _rows(cards, pending, screens)


def test_rows_are_offered_one_at_a_time_and_an_ordinal_only_names_the_current_row(monkeypatch):
    r = _rig(monkeypatch, _three())

    async def go():
        await r.ask("What needs me?")
        assert r.said_text()[-2] == (
            f"3 things need you. deploy in infra {BASH_SAID}"
            " docs in web asks: Which branch should I rebase onto? The options are: one, main;"
            " two, release 2.0."
            " api-rebase in web asks a question. The suggested answer is: Use main."
            " Let's take those one at a time."
        )
        assert [c["id"] for c in r.call.challenge["cards"]] == [f"act-{P_SID}"]
        await r.readback_played()
        # a menu number while the PERMISSION row is offered: never the menu's option
        await r.ask("The second one.")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.call.challenge is None  # void: a new question went to HQ
    assert r.asked[-1] == "The second one."


def test_skip_moves_on_and_a_settled_row_moves_on(monkeypatch):
    r = _rig(monkeypatch, _three())

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Skip.")
        assert [c["id"] for c in r.call.challenge["cards"]] == [f"act-{M_SID}"]
        assert r.said_text()[-1].startswith("docs in web asks: Which branch")
        await r.readback_played()
        await r.ask("The second one.")
        assert r.said_text()[-2] == "Chosen: release 2.0."
        assert [c["id"] for c in r.call.challenge["cards"]] == ["ans-q"]
        assert r.said_text()[-1] == (
            "api-rebase in web asks a question. The suggested answer is: Use main. Say confirm to"
            " send it, skip, or cancel."
        )
        await r.readback_played()
        await r.ask("Skip.")

    run(go())
    assert _posts(r) == [
        (f"/api/pulse/actions/act-{M_SID}/choose", {"option": 2, "label": "release 2.0"})
    ]
    assert r.said_text()[-1] == "Okay. That was the last one, Commander."
    assert r.call.challenge is None


def test_a_tap_voids_the_offered_row_and_a_tapped_queued_row_is_never_offered(monkeypatch):
    r = _rig(monkeypatch, _three())

    async def go():
        await r.ask("What needs me?")
        await r.call.on_card_decided(f"act-{M_SID}")  # the menu, still queued, tapped on screen
        await r.readback_played()
        await r.ask("Skip.")
        assert [c["id"] for c in r.call.challenge["cards"]] == ["ans-q"]
        await r.call.on_card_decided("ans-q")  # the offered row, tapped
        assert r.call.challenge is None

    run(go())
    assert _posts(r) == []


@pytest.mark.parametrize("how", ["unarmed", "late", "german"])
def test_unarmed_late_or_non_english_replies_never_decide(how, monkeypatch):
    r = _rig(monkeypatch, _permission_body())
    if how == "german":
        r.call.detects_language = True

    async def go():
        await r.ask("What needs me?")
        if how == "unarmed":
            await r.ask("Allow it.")  # its read-back has not played
            return
        await r.readback_played()
        if how == "late":
            await r.ev(t="utterance", text="Allow it.", late=True)
            await r.side()
            return
        r.call.heard_language = "de"
        await r.ask("Allow it.")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))


# --- the live route boundary -------------------------------------------------------------------


@pytest.fixture
def live(auth_cfg, fake_jsonl, tmp_path, monkeypatch):  # noqa: ARG001
    """The real app over real PTYs (as `test_permission_answer`), and an operator the call acts
    as: the logged-in browser's cookie, CSRF token and origin."""
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
    for sid, (cols, _) in GEOM.items():
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
    r = c.post("/login", data={"username": "marcus", "password": "hunter2"},
               follow_redirects=False, headers={"Origin": auth_cfg.origin})  # fmt: skip
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    cookie = "; ".join(f"{k}={v}" for k, v in c.cookies.items())
    operator = asgi_call.Operator(
        headers=((b"cookie", cookie.encode()), (b"x-csrf-token", csrf.encode()),
                 (b"origin", auth_cfg.origin.encode()), (b"host", b"testserver")),
        scheme="https", server=("testserver", 443), client=("127.0.0.1", 50000),
    )  # fmt: skip

    def tap(body, action="esc-p"):
        return c.post(f"/api/pulse/actions/{action}/choose", json=body,
                      headers={"Origin": auth_cfg.origin, "X-CSRF-Token": csrf})  # fmt: skip

    def typed(sid: str) -> bytes:
        slave = fds[sid][1]
        os.set_blocking(slave, False)
        import time

        time.sleep(0.05)
        try:
            return os.read(slave, 4096)
        except BlockingIOError:
            return b""

    try:
        yield app, operator, tap, typed
    finally:
        session_input.reset()
        for pair in fds.values():
            for fd in pair:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _voice(app, operator) -> tuple[voice_calls.Call, list[str]]:
    said: list[str] = []
    call = voice_calls.Call(
        id="c", cid="conv", contact="asia-woman", voice="aria", operator=operator, app=app,
        registry=None, state="listening",
    )  # fmt: skip

    async def to_worker(m):
        if isinstance(m, dict) and m.get("t") == "say":
            said.append(m["text"])

    async def to_client(m):
        return None

    call._to_worker = to_worker
    call._to_client = to_client
    return call, said


def _card_for(sid: str) -> dict:
    """The voice card the call builds for the session's recorded escalation, as Needs-you shows
    it: through `needs_you._public_action`, the real projection."""
    from agent_sessions import needs_you

    esc = ledger.get("esc-p")
    row = {"title": "deploy", "project": {"id": "", "name": ""}, "kind": "approval",
           "action": needs_you._public_action(esc)}  # fmt: skip
    card = voice_calls._permission_card(row)
    assert card is not None, row
    return card


def test_a_voice_allow_once_types_the_digit_through_the_real_route(live):
    app, operator, _tap, typed = live
    _paint(CL_SID, "claude_permission_bash.raw")
    _escalate(CL_SID)
    call, said = _voice(app, operator)
    card = _card_for(CL_SID)
    run(call.settle({**card, "choice": card["allow"]}))
    assert typed(CL_SID) == b"1"  # the single-use "Yes", nothing broader
    assert said[-1] == "Allowed, once."


def test_a_changed_dialog_under_the_same_labels_is_refused_at_the_route(live):
    """Grep was recorded; Shell is on the screen. "Allow once" reads the same over both."""
    app, operator, _tap, typed = live
    _paint(OC_SID, "opencode_permission_grep.raw")
    _escalate(OC_SID)
    _paint(OC_SID, "opencode_permission_shell.raw")
    call, said = _voice(app, operator)
    card = _card_for(OC_SID)
    run(call.settle({**card, "choice": card["allow"]}))
    assert typed(OC_SID) == b""
    assert said[-1].startswith("Nothing was sent, Commander.")
    assert ledger.get("esc-p")["state"] == "escalated"  # still open for the screen


def test_a_tap_racing_a_voice_decision_settles_once(live):
    app, operator, tap, typed = live
    _paint(CL_SID, "claude_permission_bash.raw")
    _escalate(CL_SID)
    call, said = _voice(app, operator)
    card = _card_for(CL_SID)

    async def race():
        loop = asyncio.get_running_loop()
        tapped = loop.run_in_executor(None, tap, {"option": 3, "label": "No"})
        await call.settle({**card, "choice": card["allow"]})
        return await tapped

    r = run(race())
    out = typed(CL_SID)
    assert out in (b"1", b"3"), out  # exactly one answer reached the dialog
    voice_won = said[-1] == "Allowed, once."
    assert (r.status_code == 200) is not voice_won
    assert sum(1 for rec in ledger.latest_by_id().values()
               if rec.get("verb") == "choose" and rec.get("state") == "delivered") == 1  # fmt: skip


# --- what a row must be to be decided by voice -------------------------------------------------


def _hand_row(**action) -> dict:
    """A Needs-you row as the route could deliver it, built by hand: the voice checks the row it
    was GIVEN, not only what the projection would produce."""
    perm = permission_prompts.projection(_perm("claude_permission_bash.raw"))
    base = {"id": "esc-1", "verb": "escalate", "permission": perm}
    return {"title": "deploy", "project": {"id": "", "name": ""}, "kind": "approval",
            "action": {**base, **action}}  # fmt: skip


def test_a_permission_card_needs_a_pending_escalation_and_a_refusal_on_the_dialog():
    assert voice_calls._permission_card(_hand_row()) is not None
    assert voice_calls._permission_card(_hand_row(verb="answer")) is None  # not a `/choose`
    perm = dict(permission_prompts.projection(_perm("claude_permission_bash.raw")), deny=7)
    assert voice_calls._permission_card(_hand_row(permission=perm)) is None  # not an option


def test_allow_is_offered_only_for_an_option_the_dialog_says_is_single_use():
    perm = permission_prompts.projection(_perm("claude_permission_bash.raw"))
    perm = dict(perm, options=[dict(o) for o in perm["options"]])
    perm["options"][0]["persistent"] = True  # `once` names it, but the dialog says it persists
    card = voice_calls._permission_card(_hand_row(permission=perm))
    assert card["allow"] is None and card["deny"]["option"] == 3
    perm["options"][0].pop("persistent")  # unknown is not single-use either
    assert voice_calls._permission_card(_hand_row(permission=perm))["allow"] is None


def test_a_menu_row_that_recorded_a_permission_is_never_a_menu_card():
    row = _hand_row(menu=MENU, permission=None)
    row["kind"] = "choice"
    assert voice_calls._menu_card(row) is None
    assert voice_calls._permission_card(row) is None
    assert voice_calls.needs_you_sentence(row).endswith("a permission I can't read out — it's on"
                                                        " your screen.")  # fmt: skip


def test_a_delivered_choice_is_reported_settled_to_the_screen(monkeypatch):
    r = _rig(monkeypatch, _permission_body())

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Refuse it.")

    run(go())
    settled = [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "settled"]
    assert settled[-1] == {"t": "settled", "card": f"act-{P_SID}", "ok": True, "status": 200}


def test_a_choice_that_may_have_landed_is_never_said_as_failed(monkeypatch):
    body = {"detail": "the choice may or may not have landed (TimeoutError)",
            "state": "indeterminate", "action_id": "choose_1"}  # fmt: skip
    r = _rig(monkeypatch, _permission_body(), status=502, body=body)

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Allow it.")

    run(go())
    assert r.said_text()[-1] == (
        "I can't tell whether that reached the session, Commander. Check it before deciding again."
    )


def test_a_broader_grant_is_said_back_as_screen_only_and_decides_nothing(monkeypatch):
    r = _rig(monkeypatch, _permission_body())

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ask("Allow it always.")
        assert r.said_text()[-1] == (
            "By voice I can only allow it once, Commander. Anything broader stays on your screen."
        )

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))


@pytest.mark.parametrize("newer", ["question", "needs_you"])
def test_a_newer_question_during_a_settle_owns_the_call(newer, monkeypatch):
    """The next queued row is offered only if nothing newer took the call while the decided one
    settled: `offer` voids the current challenge FIRST, so a stale queue would void the newer
    question's challenge (#1683 review)."""
    r = Rig(monkeypatch, {"proposals": [{"kind": "nudge", "action_id": "act-new",
                                         "title": "web", "text": "Rerun the tests."}]})  # fmt: skip
    r.needs_body = _three()
    gate = asyncio.Event()
    rig_call = asgi_call.call

    async def slow(app, operator, method, path, b, **kw):
        if path.endswith("/choose"):
            r.routes.append((method, path, b))
            await gate.wait()
            return asgi_call.Response(200, DELIVERED)
        return await rig_call(app, operator, method, path, b, **kw)

    monkeypatch.setattr(asgi_call, "call", slow)

    async def go():
        await r.ask("What needs me?")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Refuse it.")  # its settle waits on the route …
        for _ in range(20):
            await asyncio.sleep(0)
        if newer == "question":
            await r.ev(t="user_started")
            await r.ev(t="utterance", text="Nudge the web session")
            await r.call.turn_task
            assert r.call.challenge["cards"][0]["id"] == "act-new"
        else:
            await r.call.on_needs_you_request()
            for _ in range(50):
                await asyncio.sleep(0)
        mine = r.call.challenge
        assert mine is not None
        gate.set()  # … and finishes after the newer question took the call
        await r.side()
        # never voided or replaced by the decided row's stale queue (the menu)
        assert r.call.challenge is mine
        assert r.call.challenge["cards"][0]["id"] != f"act-{M_SID}"

    run(go())


@pytest.mark.parametrize("how", ["cancel", "expire", "talked_over", "new_question"])
def test_every_item_was_told_in_full_however_the_first_offer_ends(how, monkeypatch):
    """Hermes 7021: the answer to "What needs me?" must not depend on getting through the queue.
    Whatever ends the first offer, every row's content was already in the answer."""
    r = _rig(monkeypatch, _three())
    clock = [1000.0]
    monkeypatch.setattr(voice_calls.time, "monotonic", lambda: clock[0])

    async def go():
        await r.ask("What needs me?")
        if how == "talked_over":
            await r.ev(t="user_started")  # before the read-back played: voids it
            await r.ev(t="interrupt")
            return
        await r.readback_played()
        if how == "cancel":
            await r.ask("Cancel.")
        elif how == "expire":
            clock[0] += voice_calls.CHALLENGE_S + 1
            await r.ask("Refuse it.")  # too late: a new question
        else:
            await r.ask("How is the docs refresh?")

    run(go())
    told = next(t for t in r.said_text() if t.startswith("3 things need you."))
    for content in (
        "mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt",
        "The options are: one, Yes; two, Yes, and don't ask again",
        "Which branch should I rebase onto? The options are: one, main; two, release 2.0.",
        "The suggested answer is: Use main.",
    ):
        assert content in told
    assert not any(p.endswith("/choose") for p, _ in _posts(r))


def test_a_dialog_too_long_to_read_whole_is_screen_only(monkeypatch):
    # every part within its own caps — each label under the parser's 200 — but together longer
    # than one dialog a call reads out
    rec = _perm("claude_permission_bash.raw")
    long = "Yes, and don't ask again for these commands in " + "the folder " * 13
    assert len(long) < permission_prompts.LABEL_MAX
    opts = [{"n": 1, "label": "Yes", "selected": True, "persistent": False}]
    opts += [{"n": i, "label": f"{long}{i}", "selected": False, "persistent": True}
             for i in (2, 3, 4)]  # fmt: skip
    opts += [{"n": 5, "label": "No", "selected": False, "persistent": False}]
    rec = dict(rec, options=opts, detail="echo " + "word " * 28 + "\n" + "Prints " + "words " * 22)
    proj = permission_prompts.projection(rec)
    assert proj is not None and proj["deny"] == 5 and proj["command"]  # the projection reads it
    assert voice_text.verbatim(f"{proj['command']} {proj['note']}")  # the request is readable
    r = _rig(monkeypatch, _permission_body(rec))

    async def go():
        await r.ask("What needs me?")

    run(go())
    assert r.call.challenge is None
    assert r.said_text()[-1].endswith("wants a permission I can't read out — it's on your screen.")
