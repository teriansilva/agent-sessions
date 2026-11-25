"""#2137 P1 — every word the Commander says on a call reaches HQ, or leaves a record of why not.

On 2026-11-18 a spoken mission request contained "what … need … you": a phrase list routed the
whole utterance to a fixed Needs-you read-out, HQ never saw it, and nothing of it was stored. The
phrase list is gone. Pinned here:

* a request with those words, and the call panel's chip, reach HQ as a turn;
* every utterance leaves exactly one `heard` record as it arrives (before anything routes it),
  and a `heard_link` with where it went; a dropped one keeps no words;
* HQ's own spoken lines outside answers are `said` records, and later turns see them;
* the bounds hold, past them the call counts instead of storing;
* the log line says what happened, never the words;
* `offer_decisions`: only on a call, only decisions this turn read, and a decision that changed
  on the live list is never read back.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest

from agent_sessions import ask_orchestrator, ask_store, voice_calls
from test_ask_orchestrator import ai, answer, app  # noqa: F401 — fixtures
from test_voice_confirm import NUDGE, Rig, run

QD = "0" * 64
MISSION = "So what I need you to do is plan a mission for the TikTok quizzes"


def _store_rig(monkeypatch, view: dict | None = None) -> Rig:
    """A rig whose call writes to a real (temporary) conversation."""
    r = Rig(monkeypatch, view)
    r.call.cid = ask_store.create()
    return r


def _calls(cid: str) -> list[dict]:
    return [
        r for r in ask_store.read(cid) if r["type"] in ("heard", "heard_link", "said", "counts")
    ]


async def _flushed(r: Rig) -> None:
    await r.side()
    await r.call.flush_records()


# --- the words reach HQ ------------------------------------------------------------------------


def test_a_request_with_what_need_you_in_it_reaches_hq(monkeypatch):
    r = Rig(monkeypatch)

    async def go():
        await r.ask(MISSION)

    run(go())
    assert r.asked == [MISSION]
    assert not any(p == "/api/pulse/needs-you" for _, p, _ in r.routes)  # no read-out
    assert r.said_text()[-1] == "Done."


@pytest.mark.parametrize(
    "words",
    ["What needs me?", "anything need my attention", "tell me what you need from me"],
)
def test_no_phrase_is_answered_around_the_model(words, monkeypatch):
    r = Rig(monkeypatch, {"answer": "HQ answered."})

    async def go():
        await r.ask(words)

    run(go())
    assert r.asked == [words]
    assert r.said_text()[-1] == "HQ answered."


def test_the_chip_while_hq_works_is_asked_after_it(monkeypatch):
    r = Rig(monkeypatch)
    gate = asyncio.Event()
    asked = r.asked

    async def slow_begin(cid, turn_id, text, **kw):
        asked.append(text)
        if len(asked) == 1:
            await gate.wait()
        return None, {"turn_id": turn_id, "status": "done", "answer": f"Re: {text}"}

    monkeypatch.setattr(ask_orchestrator, "begin", slow_begin)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is the fleet?")
        await asyncio.sleep(0)
        await r.call.on_needs_you_request()  # a turn runs: queued, never dropped
        gate.set()
        await r.side()

    run(go())
    assert asked == ["How is the fleet?", voice_calls.NEEDS_YOU_CHIP]


# --- every utterance on record -----------------------------------------------------------------


def test_a_request_is_recorded_as_heard_then_linked_to_its_turn(monkeypatch):
    r = _store_rig(monkeypatch)

    async def go():
        await r.ask(MISSION)
        await _flushed(r)

    run(go())
    heard, *rest = [x for x in _calls(r.call.cid) if x["type"] != "said"]
    assert heard["route"] == "received" and heard["text"] == MISSION
    assert heard["chars"] == len(MISSION) and heard["turn"] == 1
    (link,) = rest
    assert link["heard_id"] == heard["heard_id"] and link["route"] == "hq"
    assert ask_store.valid_turn_id(link["turn_id"])


def test_the_heard_record_is_taken_before_anything_is_awaited(monkeypatch):
    """Recorded as it arrives: queued synchronously, before the call routes the words."""
    r = _store_rig(monkeypatch)
    seen: list[str] = []
    real = r.call._record

    def spy(rec):
        seen.append(rec["type"])
        real(rec)

    r.call._record = spy

    async def no_route(*a, **kw):
        assert seen == ["heard"], "routed before it was recorded"

    monkeypatch.setattr(r.call, "on_utterance", no_route)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text=MISSION)

    run(go())
    assert seen == ["heard"]


@pytest.mark.parametrize(
    "why, event",
    [
        ("foreign", {"text": "Bună ziua, ce mai faci azi", "heard_language": "ro"}),
        ("no_speech", {"text": "Thank you."}),
    ],
)
def test_a_dropped_utterance_keeps_no_words(why, event, monkeypatch):
    r = _store_rig(monkeypatch)
    r.call.listen_languages = frozenset({"en"})

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", **event)
        await _flushed(r)

    run(go())
    (heard,) = [x for x in _calls(r.call.cid) if x["type"] == "heard"]
    assert heard["route"] == "dropped" and heard["why"] == why
    assert "text" not in heard and heard["chars"] == len(event["text"])
    assert r.asked == []


def test_hqs_own_echo_is_dropped_without_its_words(monkeypatch):
    r = _store_rig(monkeypatch)
    line = "The fleet is quiet and nothing is waiting on you right now."

    async def go():
        await r.call.say(line)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text=line)
        await _flushed(r)

    run(go())
    (heard,) = [x for x in _calls(r.call.cid) if x["type"] == "heard"]
    assert (heard["route"], heard["why"]) == ("dropped", "echo")
    assert "text" not in heard


def test_the_store_refuses_words_on_a_dropped_record():
    with pytest.raises(ValueError, match="keeps no words"):
        ask_store._admit({"type": "heard", "heard_id": "a" * 12, "route": "dropped",
                          "why": "echo", "chars": 3, "text": "hey"}, [])  # fmt: skip


def test_kept_words_pass_the_store_boundary_and_their_length_survives_the_cap():
    """Kept words go through `guard_text` like every stored text: a stored secret is redacted, a
    credential-shaped utterance is withheld whole, and the cap never hides how long it was."""
    secret = "hunter2-correct-horse-battery"
    redacted = ask_store._admit({"type": "heard", "heard_id": "a" * 12, "route": "received",
                                 "text": f"the password is {secret} ok", "chars": 50},
                                [secret])  # fmt: skip
    assert secret not in redacted["text"]
    token = "ghp_" + "Zq8Lx2Vb9Tn4Mw7Rk1Yp5Hc3Jd6Gf0Sa8Qe2"  # a random-looking token
    withheld = ask_store._admit({"type": "heard", "heard_id": "b" * 12, "route": "received",
                                 "text": token, "chars": len(token)}, [])  # fmt: skip
    assert withheld["text"] == ask_store.WITHHELD
    long = ask_store._admit({"type": "heard", "heard_id": "c" * 12, "route": "received",
                             "text": "word " * 2000, "chars": 10_000}, [])  # fmt: skip
    assert len(long["text"]) == ask_store.TEXT_MAX and long["chars"] == 10_000


def test_muted_words_are_linked_as_muted(monkeypatch):
    r = _store_rig(monkeypatch)
    r.call.muted = True

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Plan the mission")
        await _flushed(r)

    run(go())
    links = [x["route"] for x in _calls(r.call.cid) if x["type"] == "heard_link"]
    assert links == ["muted"]
    assert r.asked == []


def test_a_confirmation_reply_is_linked_as_confirm(monkeypatch):
    r = _store_rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests on api-rebase")
        await r.readback_played()
        await r.ask("confirm")
        await _flushed(r)

    run(go())
    links = [x["route"] for x in _calls(r.call.cid) if x["type"] == "heard_link"]
    assert links == ["hq", "confirm"]
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes


def test_words_asked_while_hq_works_are_queued_then_linked_to_their_turn(monkeypatch):
    r = _store_rig(monkeypatch)
    gate = asyncio.Event()

    async def slow_begin(cid, turn_id, text, **kw):
        r.asked.append(text)
        if len(r.asked) == 1:
            await gate.wait()
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    monkeypatch.setattr(ask_orchestrator, "begin", slow_begin)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is the fleet?")
        await asyncio.sleep(0)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="And the docs refresh?")
        gate.set()
        await _flushed(r)

    run(go())
    recs = _calls(r.call.cid)
    second = [x for x in recs if x["type"] == "heard"][1]
    routes = [x["route"] for x in recs if x.get("heard_id") == second["heard_id"]]
    assert routes == ["received", "queued", "hq"]


def test_every_utterance_has_exactly_one_heard_record(monkeypatch):
    r = _store_rig(monkeypatch)
    r.call.listen_languages = frozenset({"en"})

    async def go():
        for text, lang in [("Plan a mission", "en"), ("Ce faci", "ro"), ("Thank you.", "en"),
                           ("And deploy it daily", "en")]:  # fmt: skip
            await r.ev(t="user_started")
            await r.ev(t="utterance", text=text, heard_language=lang)
            await r.side()
        await _flushed(r)

    run(go())
    heard = [x for x in _calls(r.call.cid) if x["type"] == "heard"]
    assert len(heard) == 4 and len({h["heard_id"] for h in heard}) == 4
    assert [h["route"] for h in heard] == ["received", "dropped", "dropped", "received"]


# --- HQ's own lines ----------------------------------------------------------------------------


def test_the_greeting_and_a_read_back_are_said_records_and_status_lines_are_not(monkeypatch):
    r = _store_rig(monkeypatch, NUDGE)

    async def go():
        await r.call.greet()
        await r.call.say("Still on it.", kind="status")
        await r.ask("Rerun the tests on api-rebase")
        await _flushed(r)

    run(go())
    said = [(x["kind"], x["text"]) for x in _calls(r.call.cid) if x["type"] == "said"]
    assert [k for k, _ in said] == ["greeting", "readback"]
    assert said[1][1].startswith("A message to api-rebase")


def test_later_turns_see_what_the_call_read_out_and_a_note_for_foreign_words():
    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    records = [
        {"type": "user", "turn_id": a, "text": "Rerun the tests", "via": "call"},
        {"type": "assistant", "turn_id": a, "text": "I drafted a message."},
        {"type": "status", "turn_id": a, "status": "done"},
        {"type": "said", "kind": "readback", "turn_id": a, "text": "A message to api: Rerun."},
        {"type": "said", "kind": "outcome", "text": "Sent."},
        {"type": "said", "kind": "greeting", "text": "HQ speaking."},
        {"type": "heard", "heard_id": "a" * 12, "route": "received", "text": "Rerun the tests",
         "chars": 15},
        {"type": "heard", "heard_id": "b" * 12, "route": "dropped", "why": "foreign", "chars": 9},
        {"type": "user", "turn_id": b, "text": "Plan me a mission", "via": "call"},
        {"type": "assistant", "turn_id": b, "text": "Which project?"},
        {"type": "status", "turn_id": b, "status": "done"},
        {"type": "user", "turn_id": c, "text": "The game one", "via": "call"},
    ]  # fmt: skip
    history, _ = ask_orchestrator._history(records, c)
    first_answer = history[1]["content"]
    assert "[Said on the call: A message to api: Rerun.]" in first_answer
    assert "[Said on the call: Sent.]" in first_answer
    assert "HQ speaking." not in str(history)  # the greeting is not news
    assert history[2]["content"].startswith(ask_orchestrator.FOREIGN_NOTE)
    # The heard words are the turn's question already: never replayed a second time.
    assert sum(m["content"].count("Rerun the tests") for m in history) == 1


# --- bounds and the log line -------------------------------------------------------------------


def test_past_the_bounds_the_call_counts_instead_of_storing(monkeypatch):
    monkeypatch.setattr(ask_store, "HEARD_DROPPED_MAX", 2)
    monkeypatch.setattr(ask_store, "HEARD_SAID_MAX", 5)
    r = _store_rig(monkeypatch)
    r.call.listen_languages = frozenset({"en"})

    async def go():
        for _ in range(4):
            await r.ev(t="user_started")
            await r.ev(t="utterance", text="Ce faci azi", heard_language="ro")
        for i in range(4):
            await r.call.say(f"Read back {i}.", kind="readback")
        await _flushed(r)

    run(go())
    recs = _calls(r.call.cid)
    assert sum(x["type"] == "heard" for x in recs) == 2
    assert sum(x["type"] == "said" for x in recs) == 3  # 5 in all: 2 heard + 3 said
    (counts,) = [x for x in recs if x["type"] == "counts"]
    assert (counts["heard_dropped"], counts["heard_said"]) == (2, 1)


def test_the_log_line_says_what_happened_and_never_the_words(monkeypatch, caplog):
    r = _store_rig(monkeypatch)
    r.call.listen_languages = frozenset({"en"})
    caplog.set_level(logging.INFO, logger="agent_sessions.voice_calls")

    async def go():
        await r.ask(MISSION)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Bună ziua prieteni", heard_language="ro")
        await _flushed(r)

    run(go())
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "agent_sessions.voice_calls"]
    assert any("received" in x for x in lines)
    assert any("dropped (foreign)" in x for x in lines)
    assert any(": hq (turn " in x for x in lines)
    for word in ("TikTok", "mission", "quizzes", "Bună", "prieteni"):
        assert not any(word in x for x in lines)


# --- offer_decisions ---------------------------------------------------------------------------


def _turn(via: str | None = "call", seen=()) -> SimpleNamespace:
    return SimpleNamespace(via=via, needs_you_seen=set(seen), offers=[])


def test_offer_decisions_is_refused_off_a_call():
    with pytest.raises(ask_orchestrator._ToolRefused, match="not on a call"):
        run(ask_orchestrator._offer_decisions(_turn(None, {"a1"}), {"decisions": ["a1"]}))


def test_offer_decisions_takes_only_what_this_turn_read():
    turn = _turn(seen={"a1"})
    with pytest.raises(ask_orchestrator._ToolRefused, match="only a decision"):
        run(ask_orchestrator._offer_decisions(turn, {"decisions": ["a1", "made-up"]}))
    assert turn.offers == []


def test_offer_decisions_is_only_offered_on_a_call():
    names = lambda specs: {s["function"]["name"] for s in specs}  # noqa: E731
    assert "offer_decisions" in names(ask_orchestrator.tool_specs("call"))
    assert "offer_decisions" not in names(ask_orchestrator.tool_specs(None))
    assert {"needs_you", "notifications"} <= names(ask_orchestrator.tool_specs(None))


def test_a_decision_that_changed_on_the_live_list_is_never_read_back(monkeypatch):
    """HQ read act-old; by the time the call reads its offer back, the row waits on a NEW
    decision. Nothing is offered: a voice never settles a decision HQ did not read."""
    row = {
        "title": "api",
        "kind": "question",
        "action": {"id": "act-new", "verb": "answer", "can_approve": True, "answer": "Yes."},
    }
    r = Rig(monkeypatch, {"answer": "One waits.", "offers": ["act-old"]})
    r.needs_rows = [row]  # fmt: skip

    async def go():
        await r.ask("What is waiting?")

    run(go())
    assert r.call.challenge is None
    assert r.said_text()[-1] == "One waits."


# --- review 8444 -------------------------------------------------------------------------------


def test_the_question_right_after_a_dropped_foreign_utterance_carries_the_note(app, ai):  # noqa: F811
    """Hermes 8444: settled exchange → words dropped for their language → the CURRENT question.
    The note rides on the request that goes out now, not only once this question is history."""
    script = ai(answer("Hello."), answer("I did not catch that, say it again?"))
    cid = app.new()
    assert app.ask(cid, "Hello there").status_code == 200
    ask_store.append(cid, {"type": "heard", "heard_id": "f" * 12, "route": "dropped",
                           "why": "foreign", "chars": 18, "lang": "ro"})  # fmt: skip
    assert app.ask(cid, "Did you hear that?").status_code == 200
    question = script.bodies[1]["messages"][-1]["content"]
    assert question.startswith(ask_orchestrator.FOREIGN_NOTE)
    assert question.endswith("Did you hear that?")
    # The earlier question, replayed, does not carry it: the drop came after it.
    history = [m["content"] for m in script.bodies[1]["messages"] if m["role"] == "user"][:-1]
    assert not any(ask_orchestrator.FOREIGN_NOTE in h for h in history)


def test_the_note_is_for_the_next_question_only():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    drop = {"type": "heard", "heard_id": "f" * 12, "route": "dropped", "why": "foreign", "chars": 3}
    assert ask_orchestrator._dropped_for_language([drop, {"type": "user", "turn_id": a}], a)
    # recorded a step behind the question's own record: still this question's
    assert ask_orchestrator._dropped_for_language([{"type": "user", "turn_id": a}, drop], a)
    assert not ask_orchestrator._dropped_for_language(
        [drop, {"type": "user", "turn_id": a}, {"type": "user", "turn_id": b}], b
    )
    echo = {**drop, "why": "echo"}
    assert not ask_orchestrator._dropped_for_language([echo, {"type": "user", "turn_id": a}], a)


def test_the_record_writer_retires_with_the_call_and_a_late_record_is_still_written(monkeypatch):
    """Hermes 8444 (non-blocking): the writer never outlives the call; a record that lands after
    the call ended (a settlement finishing) starts a writer of its own and is stored."""
    r = _store_rig(monkeypatch)

    async def go():
        await r.ask("How is the fleet?")
        await r.call.end("hangup")
        w = r.call._writer
        await asyncio.sleep(0)
        assert w is not None and w.done()
        r.call._record({"type": "said", "kind": "outcome", "text": "Sent."})
        assert not r.call._writer.done()
        await r.call.flush_records()
        r.call._writer.cancel()

    run(go())
    said = [x["text"] for x in _calls(r.call.cid) if x["type"] == "said"]
    assert said[-1] == "Sent."


def test_an_idle_writer_retires_on_its_own(monkeypatch):
    monkeypatch.setattr(voice_calls, "RECORD_WRITER_IDLE_S", 0.05)
    r = _store_rig(monkeypatch)

    async def go():
        await r.ask("How is the fleet?")
        await r.call.flush_records()
        await asyncio.sleep(0.2)
        assert r.call._writer.done()

    run(go())
