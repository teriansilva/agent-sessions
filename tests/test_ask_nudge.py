"""Ask asks back and proposes nudges (#1443 Phase 2a).

* **ask_operator** ends the turn on a stored question; an answer names it, and an OPTION is
  resolved to the reply the server stored — the client picks an index, never the words.
* **send_nudge** (#1960) records a ledger `answer` action for a session NO mission holds and
  delivers it AT ONCE through `actuator.deliver` on HQ's own authority — no tap. The screen
  fingerprint taken when it is recorded is re-checked before the first byte. Refused, with nothing
  written anywhere: scope disabled or off, a mission-held, excluded, not-live, working or pending
  session, a permission dialog on screen, a key the turn did not find, text that carries a secret
  or looks like a credential.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from agent_sessions import (
    actuator,
    ask_orchestrator,
    ask_store,
    missions,
    orchestrator,
    orchestrator_ledger,
    prefs,
    scrollback,
    session_input,
    template_secrets,
)
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    CRED,
    PLANTED,
    S1,
    App,
    ai,
    answer,
    app,
    call,
    calls,
    found,
)

SCREEN = "Which file should I change next?\n> "


@pytest.fixture
def writer(monkeypatch):
    """The PTY writer: it asks the delivery's own precondition first — the real
    `check_precondition` / `screen_matches` — and records the bytes only when it holds."""
    sent: list[bytes] = []
    sent_quiet: list[bool] = []  # `require_quiet` of each write that landed (#1960 Phase 2)

    def send_input(phys, payload, *, precondition=None, final_guard=None, **kw):
        if final_guard is not None:
            ok, why = final_guard()
            if not ok:
                return session_input.Outcome("refused", why)
        if precondition is not None:
            ok, why = precondition()
            if not ok:
                return session_input.Outcome("stale", why)
        sent.append(payload)
        sent_quiet.append(kw.get("require_quiet", True))
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)
    monkeypatch.setattr(actuator, "INTERRUPT_SETTLE_S", 0.0)
    sent_quiet_ref[0] = sent_quiet
    return sent


#: The last `writer`'s `require_quiet` flags, one per landed write (module-level so tests reach it
#: without a second fixture).
sent_quiet_ref: list[list[bool]] = [[]]


@pytest.fixture
def target(monkeypatch, writer):
    """S1 is in scope, live, idle and at a plain question; session assistance is `suggest`. Its
    PTY is `writer`: what reaches it is recorded, only when delivery's own checks hold."""
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    state = {"screen": SCREEN, "permission": None, "menu": None, "live": True, "held": {}}

    async def catalog(self):
        return [
            {
                "id": S1,
                "engine": "claude",
                "title": "api rebase",
                "cwd": "/work/api",
                "project": {"kind": "folder", "id": "p1", "name": "api"},
                "state": "idle",
                "last_activity": time.time() - 3 * 86400,  # idle for days: still nudgeable
            }
        ]

    monkeypatch.setattr(ask_orchestrator._Turn, "catalog", catalog)
    monkeypatch.setattr(scrollback, "live_tail_text", lambda key, n=4000: state["screen"])
    monkeypatch.setattr(session_input, "is_live", lambda key: state["live"])
    monkeypatch.setattr(missions, "all_active_memberships", lambda **k: state["held"])
    real_observed = orchestrator.observed_screen

    def observed(key, **kw):
        out = real_observed(key, **kw)
        out["permission"] = state["permission"]
        out["menu"] = state["menu"]
        return out

    monkeypatch.setattr(orchestrator, "observed_screen", observed)
    return state


def _nudge_turn(ai, text="Rebase onto main, then run the tests."):
    return ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("send_nudge", {"key": S1, "text": text}, cid="n"),
        answer("Sent."),
    )


def _ledger_for(key=S1) -> list[dict]:
    return [r for r in orchestrator_ledger.latest_by_id().values() if r.get("session_id") == key]


def _result(script, n=2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


# ---- a nudge is a proposal, never a send ---------------------------------------------------------


def test_a_nudge_is_sent_at_once_and_the_card_shows_it(app, ai, found, target, writer):
    script = _nudge_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "tell the rebase session to rebase onto main").json()["turn"]
    assert _result(script)["sent"] is True
    assert writer == [session_input.bracketed_paste("Rebase onto main, then run the tests.")]
    (rec,) = _ledger_for()
    assert rec["state"] == "delivered" and rec["source"] == "ask"
    assert rec["ask_conversation"] == cid  # #1517: the conversation that follows it once delivered
    assert rec["verb"] == "answer" and rec["answer"] == "Rebase onto main, then run the tests."
    assert rec["authority"]["scope"] == "session" and rec["authority"].get("mission_id") is None
    assert rec["precondition"]["screen_fingerprint"]
    (card,) = turn["proposals"]
    assert card["action_id"] == rec["id"] and card["key"] == S1
    assert card["text"] == "Rebase onto main, then run the tests."
    assert card["state"] == "delivered" and card["projection"] == "settled"
    assert not card["can_approve"] and not card["can_reject"]


@pytest.mark.parametrize("tier", ["suggest", "yolo"])
def test_hq_sends_in_suggest_and_in_yolo_with_no_tap(app, ai, found, target, writer, tier):
    prefs.set_orchestrator({"enabled": True, "autonomy": tier})
    _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    (rec,) = _ledger_for()
    assert rec["state"] == "delivered" and rec["tier"] == tier and len(writer) == 1


def test_switching_off_before_the_write_sends_nothing(app, ai, found, target, writer, monkeypatch):
    """HQ's authority is asked again at the write boundary, not only when the send began."""
    fenced = session_input.send_input

    def off_then_write(phys, payload, **kw):
        prefs.set_orchestrator({"enabled": True, "autonomy": "off"})
        return fenced(phys, payload, **kw)

    monkeypatch.setattr(session_input, "send_input", off_then_write)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert _result(script)["sent"] is False and writer == []
    (rec,) = _ledger_for()
    assert rec["state"] != "delivered"


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda s: prefs.set_orchestrator({"enabled": False}), "scope_disabled"),
        (lambda s: prefs.set_orchestrator({"enabled": True, "autonomy": "off"}), "scope_disabled"),
        (lambda s: s["held"].update({S1: "msn_" + "a" * 32}), "mission_held"),
        (lambda s: s.update(live=False), "not_live"),
        (lambda s: s.update(permission={"options": ["Yes", "No"]}), "permission_dialog"),
        (lambda s: s.update(menu=[(1, "Yes"), (2, "No")]), "permission_dialog"),
    ],
    ids=["disabled", "observe-only", "mission-held", "not-live", "permission-dialog", "menu"],
)
def test_a_refused_nudge_writes_nothing_anywhere(app, ai, found, target, setup, code):
    setup(target)
    script = _nudge_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "tell it to rebase").json()["turn"]
    assert "refused" in _result(script)["error"]
    assert _ledger_for() == []
    assert turn["proposals"] == []
    tools = [r for r in ask_store.read(cid) if r["type"] == "tool" and r["name"] == "send_nudge"]
    assert tools[0]["meta"]["error"] == code


def test_an_excluded_session_is_refused(app, ai, found, target):
    from agent_sessions import metadata

    metadata.patch(S1, orchestrator_excluded=True)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert "excluded" in _result(script)["error"] and _ledger_for() == []


def test_a_working_session_is_typed_into_without_waiting_for_quiet(
    app, ai, found, target, writer, monkeypatch
):
    """#1960 Phase 2: HQ types into a session whatever it is doing. A working agent's output never
    goes quiet and a viewer may be watching it — neither refuses HQ's send any more."""
    monkeypatch.setattr(actuator, "busy_keys", lambda registry, now=None: {S1})
    monkeypatch.setattr(actuator, "_viewer_busy", lambda *a, **k: True)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert _result(script)["sent"] is True
    assert writer == [session_input.bracketed_paste("Rebase onto main, then run the tests.")]
    assert sent_quiet_ref[0] == [False]  # no quiet wait: a working TUI never goes quiet


def test_a_waiting_proposal_is_superseded_by_hqs_message(
    app, ai, found, target, writer, monkeypatch
):
    """#1960 Phase 2: a proposal nobody has started delivering no longer blocks HQ. It is retired
    (`stale`, "superseded") by CAS, then HQ's own row is appended and sent — never two live."""
    real = actuator.deliver

    async def claimed_elsewhere(*a, **k):
        raise actuator.NotDeliverable("another caller claimed or changed this action first")

    monkeypatch.setattr(actuator, "deliver", claimed_elsewhere)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert _result(script)["sent"] is False  # its row is still live, unclaimed
    monkeypatch.setattr(actuator, "deliver", real)
    script = _nudge_turn(ai, "And then push.")
    app.ask(app.new(), "and tell it to push")
    assert _result(script)["sent"] is True
    old, new = sorted(_ledger_for(), key=lambda r: r["ts"])
    assert old["state"] == "stale" and "superseded" in old["detail"]
    assert new["state"] == "delivered" and new["answer"] == "And then push."
    assert writer == [session_input.bracketed_paste("And then push.")]


def test_a_delivery_in_flight_still_refuses_a_second_message(
    app, ai, found, target, writer, monkeypatch
):
    """A CLAIMED action is being typed right now: HQ never adds input beside it (no duplicates)."""
    real = actuator.deliver

    async def claimed_elsewhere(*a, **k):
        raise actuator.NotDeliverable("another caller claimed or changed this action first")

    monkeypatch.setattr(actuator, "deliver", claimed_elsewhere)
    _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    (first,) = _ledger_for()
    assert orchestrator_ledger.claim(first["id"], orchestrator_ledger.CLAIMABLE_STATES)
    monkeypatch.setattr(actuator, "deliver", real)
    script = _nudge_turn(ai, "And then push.")
    app.ask(app.new(), "and tell it to push")
    assert "being delivered" in _result(script)["error"]
    assert [r["state"] for r in _ledger_for()] == ["claimed"] and writer == []


def test_a_send_claimed_elsewhere_after_it_was_recorded_keeps_its_budget_slot(
    app, ai, found, target, monkeypatch
):
    """Only a refusal that wrote nothing gives its slot back (#1960). A send whose row was recorded
    and then claimed by another caller may have been delivered by that caller: it is not nothing."""

    async def claimed_elsewhere(*a, **k):
        raise actuator.NotDeliverable("another caller claimed or changed this action first")

    monkeypatch.setattr(actuator, "deliver", claimed_elsewhere)
    monkeypatch.setitem(
        ask_orchestrator._BUDGETS, "send", ("sends", 1, "messages", "too_many_sends")
    )
    script = ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("send_nudge", {"key": S1, "text": "first"}, cid="a"),
        call("send_nudge", {"key": S1, "text": "second"}, cid="b"),
        answer("done"),
    )
    app.ask(app.new(), "tell it twice")
    first, second = (json.loads(r["content"]) for r in script.tool_results(3)[1:])
    assert first["sent"] is False and "claimed" in first["reason"]
    assert "at most 1 messages per turn" in second["error"]
    assert len(_ledger_for()) == 1


def test_a_key_the_turn_did_not_find_is_refused(app, ai, target):
    script = ai(call("send_nudge", {"key": S1, "text": "go"}), answer("can't"))
    app.ask(app.new(), "tell S1 to go")
    assert "returned in this turn" in _result(script, 1)["error"] and _ledger_for() == []


@pytest.mark.parametrize("kind", ["template-secret", "credential-shaped"])
def test_text_with_a_secret_or_a_credential_never_becomes_an_effect(app, ai, found, target, kind):
    if kind == "template-secret":
        template_secrets.register_typed([PLANTED])
        text = f"log in with {PLANTED} and retry"
    else:
        text = f"export {CRED} and retry"
    script = _nudge_turn(ai, text)
    cid = app.new()
    app.ask(cid, "tell it to log in")
    assert _result(script)["error"].startswith("refused: that text contains a secret")
    assert _ledger_for() == []
    raw = b"".join(p.read_bytes() for p in ask_store.root().rglob("*") if p.is_file())
    assert PLANTED.encode() not in raw and b"wJalrXUtnFEMI" not in raw


# ---- the fence it is sent through ----------------------------------------------------------------


def test_a_screen_that_moved_on_still_gets_hqs_text(app, ai, found, target, writer, monkeypatch):
    """#1960 Phase 2: a working agent's screen always moves, so HQ no longer needs it unchanged."""
    real = ask_orchestrator._nudge_refusal

    def then_moves(turn, key, card, **kw):
        out = real(turn, key, card, **kw)
        target["screen"] = "Running tests… 14/80"
        return out

    monkeypatch.setattr(ask_orchestrator, "_nudge_refusal", then_moves)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert _result(script)["sent"] is True and len(writer) == 1


@pytest.mark.parametrize("prompt", ["permission", "menu"])
def test_an_operator_prompt_appearing_before_the_write_sends_nothing(
    app, ai, found, target, writer, monkeypatch, prompt
):
    """What HQ never does: type into a permission dialog or a menu. Re-checked inside the fence,
    before byte one — a prompt that appears after the send was recorded still refuses."""
    real = ask_orchestrator._nudge_refusal

    def then_prompts(turn, key, card, **kw):
        out = real(turn, key, card, **kw)
        target[prompt] = {"options": ["Yes", "No"]} if prompt == "permission" else [(1, "Yes")]
        return out

    monkeypatch.setattr(ask_orchestrator, "_nudge_refusal", then_prompts)
    script = _nudge_turn(ai)
    cid = app.new()
    app.ask(cid, "tell it to rebase")
    assert _result(script)["sent"] is False and _result(script)["state"] == "stale"
    assert writer == []
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["state"] == "stale" and not live["can_approve"]


def test_an_unreadable_ledger_says_unknown_never_gone(app, ai, found, target, monkeypatch):
    _nudge_turn(ai)
    cid = app.new()
    app.ask(cid, "tell it to rebase")
    monkeypatch.setattr(
        orchestrator_ledger, "latest_by_id_checked", lambda path=None: ("error", {})
    )
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["projection"] == "unknown" and not live["can_approve"] and not live["can_reject"]


# ---- asking back ---------------------------------------------------------------------------------


def _ask_back(ai, **extra):
    args = {"question": "Which project?", "options": ["api", "web"], **extra}
    return ai(call("ask_operator", args), answer("never reached"))


def test_a_question_ends_the_turn_and_is_stored(app, ai):
    script = _ask_back(ai, allow_text=True)
    turn = app.ask(app.new(), "start something").json()["turn"]
    assert len(script.bodies) == 1  # the turn ended on the question — no further round
    assert turn["status"] == "done" and turn["answer"] == "Which project?"
    assert turn["ask"]["options"] == ["api", "web"] and turn["ask"]["allow_text"] is True
    assert turn["ask"]["answered"] is False


def test_an_option_is_answered_with_the_stored_reply_never_the_clients(app, ai):
    _ask_back(ai)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    script = ai(answer("ok, web"))
    r = app.c.post(
        f"/api/ask/conversations/{cid}/message",
        json={
            "turn_id": str(uuid.uuid4()),
            "text": "something else entirely",
            "answers": ask["question_id"],
            "option": 1,
        },
        headers=app.hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["turn"]["question"] == "web"
    last = script.bodies[0]["messages"][-1]
    assert last == {"role": "user", "content": 'Answering your question "Which project?": web'}
    turns = app.get(cid)["turns"]
    assert turns[0]["ask"]["answered"] is True and turns[1]["answers"] == ask["question_id"]


@pytest.mark.parametrize(
    "body, detail",
    [
        ({"option": 7}, "not one the question offered"),
        ({"text": "my own words"}, "pick one of the question's options"),
        ({"option": True}, "option must be an index"),
    ],
)
def test_an_answer_the_question_does_not_admit_is_refused(app, ai, body, detail):
    _ask_back(ai)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    payload = {"turn_id": str(uuid.uuid4()), "answers": ask["question_id"], **body}
    r = app.c.post(f"/api/ask/conversations/{cid}/message", json=payload, headers=app.hdr)
    assert r.status_code == 422 and detail in r.json()["detail"]


def test_an_answer_to_a_question_from_another_conversation_is_refused(app, ai):
    _ask_back(ai)
    ask = app.ask(app.new(), "start something").json()["turn"]["ask"]
    other = app.new()
    r = app.c.post(
        f"/api/ask/conversations/{other}/message",
        json={"turn_id": str(uuid.uuid4()), "answers": ask["question_id"], "option": 0},
        headers=app.hdr,
    )
    assert r.status_code == 422 and "not in this conversation" in r.json()["detail"]


def test_an_unanswered_question_never_blocks_a_new_message(app, ai):
    _ask_back(ai)
    cid = app.new()
    app.ask(cid, "start something")
    ai(answer("sure"))
    assert app.ask(cid, "never mind, what is running?").json()["turn"]["answer"] == "sure"


def test_one_question_per_turn(app, ai):
    script = ai(
        calls(
            ("ask_operator", {"question": "A?", "options": ["x"]}),
            ("ask_operator", {"question": "B?", "options": ["y"]}),
        ),
    )
    cid = app.new()
    turn = app.ask(cid, "q").json()["turn"]
    assert turn["ask"]["question"] == "A?"
    assert len(script.bodies) == 1
    tools = [r for r in ask_store.read(cid) if r["type"] == "tool"]
    # The first question ends the turn; the second call in the same reply is never run.
    assert [(t["name"], t["meta"].get("error")) for t in tools] == [("ask_operator", None)]


def test_an_answer_text_that_looks_like_a_credential_is_withheld_but_still_answers(app, ai):
    _ask_back(ai, allow_text=True)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    ai(answer("noted"))
    r = app.c.post(
        f"/api/ask/conversations/{cid}/message",
        json={"turn_id": str(uuid.uuid4()), "text": f"use {CRED}", "answers": ask["question_id"]},
        headers=app.hdr,
    )
    assert r.json()["turn"]["question"] == ask_store.WITHHELD


def test_the_app_shape_is_unchanged_for_plain_messages(auth_cfg, ai):
    a = App(auth_cfg)
    ai(answer("hi"))
    turn = a.ask(a.new(), "hello").json()["turn"]
    assert turn["ask"] is None and turn["proposals"] == [] and turn["answers"] is None


# ---- Hermes round 1 on #1489 ---------------------------------------------------------------------


def test_a_card_that_cannot_be_recorded_means_no_action_at_all(app, ai, found, target, monkeypatch):
    """Card first, action second: if the card's write fails, nothing reaches the ledger — so no
    approvable decision can exist without its card, and the next nudge is not blocked."""
    real = ask_orchestrator._append

    async def failing(cid, *records):
        if any(r.get("type") == "proposal" for r in records):
            raise ask_store.StoreRefused("the disk is full")
        return await real(cid, *records)

    monkeypatch.setattr(ask_orchestrator, "_append", failing)
    script = _nudge_turn(ai)
    turn = app.ask(app.new(), "tell it to rebase").json()["turn"]
    assert _result(script)["error"].startswith("refused: the proposal could not be recorded")
    assert _ledger_for() == [] and turn["proposals"] == []
    monkeypatch.setattr(ask_orchestrator, "_append", real)
    _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert [r["state"] for r in _ledger_for()] == ["delivered"]


@pytest.mark.parametrize("how", ["raises", "crash"])
def test_a_ledger_write_that_never_lands_leaves_an_inert_card_only(
    app, ai, found, target, monkeypatch, how
):
    """The card is written; the ledger append then fails (or the process stops before it). What is
    left is a card that offers nothing — never a live action without a card."""
    from agent_sessions import orchestrator_ledger as ledger

    def broken(records, *a, **k):
        if how == "raises":
            raise OSError("the disk is full")
        return [], list(records)  # as if the process stopped before the write

    monkeypatch.setattr(ledger, "append_batch_for_free_sessions", broken)
    _nudge_turn(ai)
    cid = app.new()
    app.ask(cid, "tell it to rebase")
    assert _ledger_for() == []
    (card,) = app.get(cid)["turns"][0]["proposals"]
    assert card["projection"] == "historical"
    assert not card["can_approve"] and not card["can_reject"]


def test_a_secret_registered_during_the_checks_never_reaches_the_ledger(
    app, ai, found, target, monkeypatch
):
    """The precheck passed; a template secret is registered while eligibility is read. The check
    INSIDE the ledger's lock catches it, and the raw text is never written there."""
    text = "deploy with tok-ABC123-late and retry"
    real = ask_orchestrator._nudge_refusal

    def registers_mid_way(turn, key, card, **kw):
        out = real(turn, key, card, **kw)
        template_secrets.register_typed(["tok-ABC123-late"])
        return out

    monkeypatch.setattr(ask_orchestrator, "_nudge_refusal", registers_mid_way)
    script = _nudge_turn(ai, text)
    app.ask(app.new(), "tell it to deploy")
    assert _result(script)["error"].startswith("refused: that text contains a secret")
    assert _ledger_for() == []
    raw = orchestrator_ledger._path().read_bytes() if orchestrator_ledger._path().exists() else b""
    assert b"tok-ABC123-late" not in raw


def test_an_answered_question_cannot_be_answered_again(app, ai):
    _ask_back(ai)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    ai(answer("ok"))

    def send(option):
        return app.c.post(
            f"/api/ask/conversations/{cid}/message",
            json={"turn_id": str(uuid.uuid4()), "answers": ask["question_id"], "option": option},
            headers=app.hdr,
        )

    assert send(0).status_code == 200
    r = send(1)  # a stale tab
    assert r.status_code == 409 and "already answered" in r.json()["detail"]


def test_a_failed_answer_can_be_retried(app, ai):
    import httpx

    _ask_back(ai)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    ai(httpx.Response(500, text="boom"))
    body = {"answers": ask["question_id"], "option": 0}
    first = app.c.post(
        f"/api/ask/conversations/{cid}/message",
        json={"turn_id": str(uuid.uuid4()), **body},
        headers=app.hdr,
    )
    assert first.json()["turn"]["status"] == "failed"
    ai(answer("ok"))
    again = app.c.post(
        f"/api/ask/conversations/{cid}/message",
        json={"turn_id": str(uuid.uuid4()), **body},
        headers=app.hdr,
    )
    assert again.status_code == 200 and again.json()["turn"]["status"] == "done"


def test_a_question_stops_the_calls_after_it_in_the_same_reply(app, ai, found, target):
    ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        calls(
            ("ask_operator", {"question": "Which branch?", "options": ["main"]}),
            ("send_nudge", {"key": S1, "text": "Rebase onto main."}),
        ),
    )
    cid = app.new()
    turn = app.ask(cid, "tell it to rebase").json()["turn"]
    assert turn["ask"]["question"] == "Which branch?"
    assert turn["proposals"] == [] and _ledger_for() == []
    names = [r["name"] for r in ask_store.read(cid) if r["type"] == "tool"]
    assert names == ["find_sessions", "ask_operator"]


@pytest.mark.parametrize("outcome", ["failed", "pending"])
def test_an_answer_that_did_not_settle_leaves_the_question_open(app, ai, outcome):
    _ask_back(ai)
    cid = app.new()
    ask = app.ask(cid, "start something").json()["turn"]["ask"]
    if outcome == "failed":
        import httpx

        ai(httpx.Response(500, text="boom"))
        app.c.post(
            f"/api/ask/conversations/{cid}/message",
            json={"turn_id": str(uuid.uuid4()), "answers": ask["question_id"], "option": 0},
            headers=app.hdr,
        )
    else:
        tid = str(uuid.uuid4())
        ask_store.append(
            cid,
            {
                "type": "user",
                "turn_id": tid,
                "text": "api",
                "qd": "0" * 64,
                "answers": ask["question_id"],
            },
            {"type": "status", "turn_id": tid, "status": "pending"},
        )
        fd = ask_store.try_turn_lock(cid)  # held elsewhere: stays pending
    try:
        turns = app.get(cid)["turns"]
        assert turns[1]["status"] == outcome
        assert turns[0]["ask"]["answered"] is False
    finally:
        if outcome == "pending":
            import os

            os.close(fd)


def test_the_shown_text_is_exactly_what_delivery_types(app, ai, found, target, writer):
    """Control bytes are stripped as delivery strips them, BEFORE the card shows the text."""
    _nudge_turn(ai, "Rebase\x1b[201~ then test\x07")
    cid = app.new()
    (card,) = app.ask(cid, "tell it to rebase").json()["turn"]["proposals"]
    assert card["text"] == "Rebase[201~ then test"
    (rec,) = _ledger_for()
    assert rec["answer"] == card["text"]
    assert writer == [session_input.bracketed_paste(card["text"])]


def test_text_with_nothing_typeable_is_refused(app, ai, found, target):
    script = _nudge_turn(ai, "\x1b\x07  ")
    app.ask(app.new(), "tell it")
    assert "nothing typeable" in _result(script)["error"] and _ledger_for() == []


# ---- Hermes round 3 on #1489 ---------------------------------------------------------------------


def _paused_write(monkeypatch):
    """Pause `ask_store.append` between its snapshot (admission) and its write."""
    import threading

    admitted, release = threading.Event(), threading.Event()
    real = ask_store._admit

    def slow_admit(rec, secrets):
        out = real(rec, secrets)
        if rec.get("type") == "user":
            admitted.set()
            assert release.wait(10)
        return out

    monkeypatch.setattr(ask_store, "_admit", slow_admit)
    return admitted, release


def _concurrently(register):
    """Run ``register`` in a thread; report whether it finished while the write was paused."""
    import threading

    done = threading.Event()
    errors: list[BaseException] = []

    def run():
        try:
            register()
        except BaseException as e:  # noqa: BLE001 — surfaced by the caller, never swallowed
            errors.append(e)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.errors = errors  # type: ignore[attr-defined]
    return t, done


@pytest.mark.parametrize("how", ["typed", "stored"])
def test_a_secret_registered_during_an_ask_write_waits_for_it(monkeypatch, how):
    """The registration fence: a secret cannot become known between an Ask write's snapshot and
    its write — registering it waits until the write is done (and the record was clean then)."""
    import threading

    from agent_sessions import template_vars

    cid = ask_store.create()
    admitted, release = _paused_write(monkeypatch)
    writer = threading.Thread(
        target=lambda: ask_store.append(
            cid, {"type": "user", "turn_id": str(uuid.uuid4()), "text": "hello", "qd": "0" * 64}
        ),
        daemon=True,
    )
    writer.start()
    assert admitted.wait(10)
    if how == "typed":
        register = lambda: template_secrets.register_typed(["fresh-secret-value-1234"])  # noqa: E731
    else:
        register = lambda: template_vars.create_variable(  # noqa: E731
            {"name": "fresh_token", "kind": "secret", "value": "fresh-secret-value-1234"}
        )
    t, done = _concurrently(register)
    assert not done.wait(0.5), "the registration must wait for the in-flight write"
    release.set()
    writer.join(10)
    t.join(10)
    assert done.is_set() and t.errors == []  # type: ignore[attr-defined]
    # …and a write that starts after it redacts the new secret.
    ask_store.append(
        cid,
        {
            "type": "user",
            "turn_id": str(uuid.uuid4()),
            "text": "fresh-secret-value-1234 x",
            "qd": "0" * 64,
        },
    )
    assert b"fresh-secret-value-1234" not in (ask_store.root() / f"{cid}.jsonl").read_bytes()


def test_an_append_that_wrote_then_failed_is_read_back_and_sent(
    app, ai, found, target, writer, monkeypatch
):
    """The record lands, then the directory fsync fails: the exception is ambiguous, so the exact
    action id is read back — present means recorded, and it is sent like any other."""
    from agent_sessions import orchestrator_ledger as ledger

    real = ledger.append_batch_for_free_sessions

    def wrote_then_failed(records, *a, **k):
        real(records, *a, **k)
        raise OSError("directory fsync failed")

    monkeypatch.setattr(ledger, "append_batch_for_free_sessions", wrote_then_failed)
    script = _nudge_turn(ai)
    cid = app.new()
    app.ask(cid, "tell it to rebase")
    assert _result(script)["sent"] is True and len(writer) == 1
    (rec,) = _ledger_for()
    assert rec["state"] == "delivered"
    (card,) = app.get(cid)["turns"][0]["proposals"]
    assert card["action_id"] == rec["id"] and card["state"] == "delivered"


def test_a_failed_append_that_left_nothing_is_reported_as_uncertain_not_refused(
    app, ai, found, target, monkeypatch
):
    from agent_sessions import orchestrator_ledger as ledger

    def nothing_then_failed(records, *a, **k):
        raise OSError("directory fsync failed")

    monkeypatch.setattr(ledger, "append_batch_for_free_sessions", nothing_then_failed)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert _result(script)["error"].startswith("refused: the proposal could not be confirmed")
    assert _ledger_for() == []


# ---- interrupt (#1960 Phase 2) -------------------------------------------------------------------


def _interrupt_turn(ai, text=None):
    args = {"key": S1, "interrupt": True}
    if text is not None:
        args["text"] = text
    return ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("send_nudge", args, cid="n"),
        answer("Stopped it."),
    )


def test_interrupt_types_the_key_alone_then_the_text(app, ai, found, target, writer, monkeypatch):
    """Stop, then send: the agent's manifest key (claude: Esc) as a write of its own, then the
    text as its usual paste. Never one write: a lone Esc beside the paste's ESC is a sequence."""
    monkeypatch.setattr(actuator, "busy_keys", lambda registry, now=None: {S1})
    script = _interrupt_turn(ai, "Stop that and rebase onto main instead.")
    cid = app.new()
    turn = app.ask(cid, "stop it and tell it to rebase").json()["turn"]
    res = _result(script)
    assert res["sent"] is True and "stopped the running turn" in res["detail"]
    assert writer == [
        b"\x1b",
        session_input.bracketed_paste("Stop that and rebase onto main instead."),
    ]
    (rec,) = _ledger_for()
    assert rec["interrupt"] is True and rec["state"] == "delivered"
    (card,) = turn["proposals"]
    assert card["interrupt"] is True and card["text"] == "Stop that and rebase onto main instead."


def test_interrupt_without_text_only_stops(app, ai, found, target, writer):
    script = _interrupt_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "stop the rebase session").json()["turn"]
    assert _result(script)["sent"] is True
    assert writer == [b"\x1b"]
    (rec,) = _ledger_for()
    assert rec["answer"] == "" and rec["detail"] == "stopped the running turn"
    (card,) = turn["proposals"]
    assert card["interrupt"] is True and card["text"] == ""


def test_text_is_still_required_without_interrupt(app, ai, found, target, writer):
    script = ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("send_nudge", {"key": S1}, cid="n"),
        answer("can't"),
    )
    app.ask(app.new(), "tell it")
    assert "text must be a non-empty string" in _result(script)["error"]
    assert _ledger_for() == [] and writer == []


def test_an_interrupt_that_did_not_land_types_no_text(app, ai, found, target, monkeypatch):
    """ "Stop, then do X" never becomes "do X after the turn you were told to stop"."""
    typed: list[bytes] = []

    def esc_refused(phys, payload, **kw):
        if payload == b"\x1b":
            return session_input.Outcome("refused", "the PTY is consuming input")
        typed.append(payload)
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", esc_refused)
    script = _interrupt_turn(ai, "Rebase instead.")
    app.ask(app.new(), "stop it and rebase")
    res = _result(script)
    assert res["sent"] is False and "could not be stopped" in res["reason"]
    assert typed == []


def test_an_agent_with_no_interrupt_key_is_refused_before_anything_is_written(
    app, ai, found, target, writer, monkeypatch
):
    from agent_sessions import engines

    monkeypatch.setattr(engines, "interrupt_bytes", lambda prov: None)
    script = _interrupt_turn(ai, "Rebase instead.")
    cid = app.new()
    turn = app.ask(cid, "stop it").json()["turn"]
    assert "no interrupt key" in _result(script)["error"]
    assert _ledger_for() == [] and writer == [] and turn["proposals"] == []


def test_only_the_commanders_instruction_may_stop_a_turn(
    app, ai, found, target, writer, monkeypatch
):
    """An interrupt row delivered by anything but HQ's send or the operator's tap — a supervisor or
    automation path, which never sets `attended` — is refused and stops nothing."""
    real = actuator.deliver

    async def claimed_elsewhere(*a, **k):
        raise actuator.NotDeliverable("another caller claimed or changed this action first")

    monkeypatch.setattr(actuator, "deliver", claimed_elsewhere)
    _interrupt_turn(ai, "Rebase instead.")
    app.ask(app.new(), "stop it")
    (row,) = _ledger_for()
    assert row["interrupt"] is True and row["state"] == "proposed"
    done = asyncio.run(real(row["id"], authority=lambda live: (True, "")))
    assert done["state"] == "failed" and "Commander's instruction" in done["detail"]
    assert writer == []


def _pause_writer(monkeypatch, during_pause):
    """A PTY whose Esc lands, after which ``during_pause`` changes the world; the text's write then
    asks the delivery's own final guard and precondition, as `send_input` does before byte one."""
    typed: list[bytes] = []

    def send_input(phys, payload, *, precondition=None, final_guard=None, **kw):
        if typed:  # the text, after the pause
            for check, refused in ((final_guard, "refused"), (precondition, "stale")):
                if check is not None:
                    ok, why = check()
                    if not ok:
                        return session_input.Outcome(refused, why)
        typed.append(payload)
        if payload == b"\x1b":
            during_pause()
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)
    monkeypatch.setattr(actuator, "INTERRUPT_SETTLE_S", 0.0)
    return typed


@pytest.mark.parametrize("change", ["off", "permission", "ownership", "secret"])
def test_the_pause_after_an_interrupt_is_a_withdrawal_window(
    app, ai, found, target, monkeypatch, change
):
    """Between the Esc and the text, everything delivery checks is asked again (Hermes on #1960):
    switching off, a dialog appearing, the session changing owner, a secret being registered."""
    from agent_sessions import automation

    def during_pause():
        if change == "off":
            prefs.set_orchestrator({"enabled": True, "autonomy": "off"})
        elif change == "permission":
            target["permission"] = {"options": ["Yes", "No"]}
        elif change == "ownership":
            monkeypatch.setattr(automation, "check", lambda rec, **k: (False, "owner changed"))
        else:
            template_secrets.register_typed(["rebase-token-9f3k"])

    typed = _pause_writer(monkeypatch, during_pause)
    script = _interrupt_turn(ai, "Use rebase-token-9f3k and rebase.")
    app.ask(app.new(), "stop it and rebase")
    assert typed == [b"\x1b"]  # the stop landed; the text did not
    res = _result(script)
    assert res["sent"] is False
    (rec,) = _ledger_for()
    assert rec["state"] in ("stale", "failed") and rec["detail"].startswith(
        "stopped the running turn"
    )


def test_a_text_write_that_raises_after_the_stop_is_unknown_and_never_replayed(
    app, ai, found, target, monkeypatch
):
    typed: list[bytes] = []

    def send_input(phys, payload, **kw):
        typed.append(payload)
        if payload != b"\x1b":
            raise OSError("the PTY went away mid-write")
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)
    monkeypatch.setattr(actuator, "INTERRUPT_SETTLE_S", 0.0)
    script = _interrupt_turn(ai, "Rebase instead.")
    app.ask(app.new(), "stop it and rebase")
    assert _result(script)["sent"] == "unknown"
    (rec,) = _ledger_for()
    assert rec["state"] == "indeterminate" and rec["detail"].startswith("stopped the running turn")
    assert typed.count(b"\x1b") == 1  # each half written at most once


def test_a_failure_between_the_stop_and_the_text_is_unknown_and_says_the_stop(
    app, ai, found, target, writer, monkeypatch
):
    """A crash between the two effects (Hermes on #1960): the Esc is in, the text never started.
    Settled `indeterminate`, saying the stop; never replayed."""

    def boom(text):
        raise RuntimeError("the server stopped mid-delivery")

    monkeypatch.setattr(actuator, "_hq_text_still_clean", boom)
    script = _interrupt_turn(ai, "Rebase instead.")
    app.ask(app.new(), "stop it and rebase")
    assert _result(script)["sent"] == "unknown"
    (rec,) = _ledger_for()
    assert rec["state"] == "indeterminate"
    assert rec["detail"].startswith("stopped the running turn; then delivery outcome is unknown")
    assert writer == [b"\x1b"]


def _one_send_budget(monkeypatch):
    monkeypatch.setitem(
        ask_orchestrator._BUDGETS, "send", ("sends", 1, "messages", "too_many_sends")
    )


def test_an_interrupt_only_send_spends_a_send_slot(app, ai, found, target, writer, monkeypatch):
    """Stopping a turn is an effect: it is counted like any send (Hermes on #1960)."""
    _one_send_budget(monkeypatch)
    script = ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("send_nudge", {"key": S1, "interrupt": True}, cid="a"),
        call("send_nudge", {"key": S1, "text": "and rebase"}, cid="b"),
        answer("done"),
    )
    app.ask(app.new(), "stop it, then tell it to rebase")
    first, second = (json.loads(r["content"]) for r in script.tool_results(3)[1:])
    assert first["sent"] is True
    assert "at most 1 messages per turn" in second["error"]
    assert writer == [b"\x1b"]


def test_a_stop_that_landed_keeps_its_slot_even_when_the_text_was_refused(
    app, ai, found, target, monkeypatch
):
    """ "No text sent" is not "no effect": once the Esc may have landed, a later refusal of the text
    does not give the slot back (Hermes on #1960)."""
    _one_send_budget(monkeypatch)
    typed = _pause_writer(
        monkeypatch, lambda: template_secrets.register_typed(["rebase-token-9f3k"])
    )
    script = ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call(
            "send_nudge", {"key": S1, "interrupt": True, "text": "Use rebase-token-9f3k"}, cid="a"
        ),
        call("send_nudge", {"key": S1, "text": "then rebase"}, cid="b"),
        answer("done"),
    )
    app.ask(app.new(), "stop it and redirect it")
    first, second = (json.loads(r["content"]) for r in script.tool_results(3)[1:])
    assert first["sent"] is False and "stopped the running turn" in first["reason"]
    assert "at most 1 messages per turn" in second["error"]
    assert typed == [b"\x1b"]


def test_an_unreadable_screen_never_authorizes_hqs_text(
    app, ai, found, target, writer, monkeypatch
):
    """Hermes on #2135: the fail-soft screen read turns an error into an EMPTY screen, which shows
    no dialog. HQ's prompt check reads strictly, so a reader error refuses instead."""
    real = ask_orchestrator._nudge_refusal

    def then_unreadable(turn, key, card, **kw):
        out = real(turn, key, card, **kw)

        def broken(*a, **k):
            raise OSError("the scrollback ring could not be read")

        monkeypatch.setattr(scrollback, "live_tail_text", broken)
        return out

    monkeypatch.setattr(ask_orchestrator, "_nudge_refusal", then_unreadable)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    res = _result(script)
    assert res["sent"] is False and "could not be read" in res["reason"]
    assert writer == []


def test_an_unreadable_screen_is_refused_before_anything_is_recorded(
    app, ai, found, target, writer, monkeypatch
):
    def broken(*a, **k):
        raise OSError("the scrollback ring could not be read")

    monkeypatch.setattr(scrollback, "live_tail_text", broken)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert "cannot read that session's screen" in _result(script)["error"]
    assert _ledger_for() == [] and writer == []
