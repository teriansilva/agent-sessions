"""Ask asks back and proposes nudges (#1443 Phase 2a).

* **ask_operator** ends the turn on a stored question; an answer names it, and an OPTION is
  resolved to the reply the server stored — the client picks an index, never the words.
* **propose_nudge** stages a ledger `answer` action for a session NO mission holds. It starts
  `proposed` whatever the tier; only the operator's tap (the existing approve route →
  `actuator.deliver`) sends it, and the screen fingerprint taken at proposal time is re-checked
  before the first byte. Refused, with nothing written anywhere: scope disabled or observe-only, a
  mission-held, excluded, not-live, working or pending session, a permission dialog on screen, a
  key the turn did not find, text that carries a secret or looks like a credential.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

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
def target(monkeypatch):
    """S1 is in scope, live, idle and at a plain question; session assistance is `suggest`."""
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    state = {"screen": SCREEN, "permission": None, "live": True, "held": {}}

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
        return out

    monkeypatch.setattr(orchestrator, "observed_screen", observed)
    return state


def _nudge_turn(ai, text="Rebase onto main, then run the tests."):
    return ai(
        call("find_sessions", {"query": "rebase"}, cid="f"),
        call("propose_nudge", {"key": S1, "text": text}, cid="n"),
        answer("I proposed the message; approve the card to send it."),
    )


def _ledger_for(key=S1) -> list[dict]:
    return [r for r in orchestrator_ledger.latest_by_id().values() if r.get("session_id") == key]


def _result(script, n=2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


# ---- a nudge is a proposal, never a send ---------------------------------------------------------


def test_a_nudge_is_a_proposed_ledger_action_and_a_card(app, ai, found, target):
    script = _nudge_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "tell the rebase session to rebase onto main").json()["turn"]
    assert _result(script)["proposed"] is True
    (rec,) = _ledger_for()
    assert rec["state"] == "proposed" and rec["source"] == "ask"
    assert rec["ask_conversation"] == cid  # #1517: the conversation that follows it once delivered
    assert rec["verb"] == "answer" and rec["answer"] == "Rebase onto main, then run the tests."
    assert rec["authority"]["scope"] == "session" and rec["authority"].get("mission_id") is None
    assert rec["precondition"]["screen_fingerprint"]
    (card,) = turn["proposals"]
    assert card["action_id"] == rec["id"] and card["key"] == S1
    assert card["text"] == "Rebase onto main, then run the tests."
    assert card["projection"] == "actionable" and card["can_approve"] and card["can_reject"]


def test_yolo_never_makes_an_ask_nudge_deliverable_on_its_own(app, ai, found, target):
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    (rec,) = _ledger_for()
    assert rec["state"] == "proposed" and rec["tier"] == "yolo"


@pytest.mark.parametrize(
    "setup, code",
    [
        (lambda s: prefs.set_orchestrator({"enabled": False}), "scope_disabled"),
        (lambda s: prefs.set_orchestrator({"enabled": True, "autonomy": "off"}), "scope_disabled"),
        (lambda s: s["held"].update({S1: "msn_" + "a" * 32}), "mission_held"),
        (lambda s: s.update(live=False), "not_live"),
        (lambda s: s.update(permission={"options": ["Yes", "No"]}), "permission_dialog"),
    ],
    ids=["disabled", "observe-only", "mission-held", "not-live", "permission-dialog"],
)
def test_a_refused_nudge_writes_nothing_anywhere(app, ai, found, target, setup, code):
    setup(target)
    script = _nudge_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "tell it to rebase").json()["turn"]
    assert "refused" in _result(script)["error"]
    assert _ledger_for() == []
    assert turn["proposals"] == []
    tools = [r for r in ask_store.read(cid) if r["type"] == "tool" and r["name"] == "propose_nudge"]
    assert tools[0]["meta"]["error"] == code


def test_an_excluded_session_is_refused(app, ai, found, target):
    from agent_sessions import metadata

    metadata.patch(S1, orchestrator_excluded=True)
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert "excluded" in _result(script)["error"] and _ledger_for() == []


def test_a_working_session_is_refused(app, ai, found, target, monkeypatch):
    monkeypatch.setattr(actuator, "busy_keys", lambda registry, now=None: {S1})
    script = _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    assert "working right now" in _result(script)["error"] and _ledger_for() == []


def test_a_session_with_a_decision_waiting_gets_no_second_one(app, ai, found, target):
    _nudge_turn(ai)
    app.ask(app.new(), "tell it to rebase")
    script = _nudge_turn(ai, "And then push.")
    app.ask(app.new(), "and tell it to push")
    assert "already has a decision waiting" in _result(script)["error"]
    assert len(_ledger_for()) == 1


def test_a_key_the_turn_did_not_find_is_refused(app, ai, target):
    script = ai(call("propose_nudge", {"key": S1, "text": "go"}), answer("can't"))
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


# ---- the operator's tap: the existing route, the existing fence ----------------------------------


@pytest.fixture
def writer(monkeypatch):
    """The PTY writer: it asks the delivery's own precondition first — the real
    `check_precondition` / `screen_matches` — and records the bytes only when it holds."""
    sent: list[bytes] = []

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
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)
    return sent


def test_approve_delivers_through_the_existing_route_and_the_card_follows(
    app, ai, found, target, writer
):
    _nudge_turn(ai)
    cid = app.new()
    (card,) = app.ask(cid, "tell it to rebase").json()["turn"]["proposals"]
    r = app.c.post(f"/api/pulse/actions/{card['action_id']}/approve", headers=app.hdr)
    assert r.status_code == 200, r.text
    assert len(writer) == 1 and b"Rebase onto main, then run the tests." in writer[0]
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["state"] == "delivered" and live["projection"] == "settled"
    assert not live["can_approve"]


def test_a_screen_that_moved_sends_nothing_and_the_card_says_so(app, ai, found, target, writer):
    _nudge_turn(ai)
    cid = app.new()
    (card,) = app.ask(cid, "tell it to rebase").json()["turn"]["proposals"]
    target["screen"] = "Running tests… 14/80"
    r = app.c.post(f"/api/pulse/actions/{card['action_id']}/approve", headers=app.hdr)
    assert r.status_code == 409
    assert writer == []
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["state"] == "stale" and not live["can_approve"]


def test_dismiss_rejects_it_and_nothing_is_sent(app, ai, found, target, writer):
    _nudge_turn(ai)
    cid = app.new()
    (card,) = app.ask(cid, "tell it to rebase").json()["turn"]["proposals"]
    r = app.c.post(f"/api/pulse/actions/{card['action_id']}/reject", headers=app.hdr)
    assert r.status_code == 200
    assert writer == []
    (live,) = app.get(cid)["turns"][0]["proposals"]
    assert live["state"] == "rejected"


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
    assert [r["state"] for r in _ledger_for()] == ["proposed"]


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

    def registers_mid_way(turn, key, card):
        out = real(turn, key, card)
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
            ("propose_nudge", {"key": S1, "text": "Rebase onto main."}),
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
    r = app.c.post(f"/api/pulse/actions/{card['action_id']}/approve", headers=app.hdr)
    assert r.status_code == 200, r.text
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


def test_an_append_that_wrote_then_failed_is_reported_as_proposed(
    app, ai, found, target, monkeypatch
):
    """The record lands, then the directory fsync fails: the exception is ambiguous, so the exact
    action id is read back — present means proposed, and the card stays live."""
    from agent_sessions import orchestrator_ledger as ledger

    real = ledger.append_batch_for_free_sessions

    def wrote_then_failed(records, *a, **k):
        real(records, *a, **k)
        raise OSError("directory fsync failed")

    monkeypatch.setattr(ledger, "append_batch_for_free_sessions", wrote_then_failed)
    script = _nudge_turn(ai)
    cid = app.new()
    app.ask(cid, "tell it to rebase")
    assert _result(script)["proposed"] is True
    (rec,) = _ledger_for()
    assert rec["state"] == "proposed"
    (card,) = app.get(cid)["turns"][0]["proposals"]
    assert card["action_id"] == rec["id"] and card["projection"] == "actionable"


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
