"""#2113 P3 — the Commander speaks while HQ works, and HQ answers like a person interrupted.

Words heard while THIS call's own turn runs (not a bare stop phrase) are judged beside the relay
(`voice_judge.judge_interjection`, faked by the Rig) and answered at once:

* ``status`` — HQ says the line and carries on; its answer is still said afterwards;
* ``steer``  — HQ says the line; the words reach the running turn's NEXT model request, once
  (stored as one `interjection` record); words no request carried are asked next, once;
* ``later``  — HQ says the line; its answer is said first, then the words are asked;
* ``switch`` — HQ says the line; the turn is stopped (no "Stopped." on top) and the words are
  asked once it has settled.

No verdict: the fallback line, and the words wait as before. The fences: only the NEWEST
judgement acts, only on the SAME turn; a turn that settled meanwhile gets the words as a new
question; Stop, mute and the end drop a pending judgement. Driven through the call with the
REAL orchestrator and a scripted model endpoint."""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import ask_orchestrator, ask_store, prompts, review, voice_calls, voice_judge
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures
from test_ask_orchestrator import ai, answer, app, call  # noqa: F401 — fixtures
from test_voice_confirm import Rig
from test_voice_draft_turns import _speak, _until

#: The real judgement, taken before any Rig fakes it.
REAL_JUDGE = voice_judge.judge_interjection


@pytest.fixture
def rig(monkeypatch, app):
    real_begin = ask_orchestrator.begin
    r = Rig(monkeypatch)
    monkeypatch.setattr(ask_orchestrator, "begin", real_begin)  # the real orchestrator
    r.call.cid = app.new()
    return r


def _statuses(cid: str) -> list[str]:
    return [r["status"] for r in ask_store.read(cid) if r.get("type") == "status"]


def _questions(cid: str) -> list[str]:
    return [r["text"] for r in ask_store.read(cid) if r.get("type") == "user"]


async def _working(rig, script, question: str = "Where is the federation epic?") -> str:
    await _speak(rig, question)
    await asyncio.wait_for(script.reached.wait(), 15)
    tid = ask_orchestrator.running_turn(rig.call.cid)
    assert tid and rig.call.call_turn and rig.call.call_turn[0] == tid
    return tid


# ---- the four verdicts ---------------------------------------------------------------------------


def test_status_answers_at_once_and_the_answer_is_still_said(rig, held):
    rig.interject_verdict = ("status", "Still digging through Forgejo for the epic.")

    async def scenario():
        script = held(answer("It is issue 2059, in phase three."))
        await _working(rig, script)
        await _speak(rig, "What are you doing?")
        await _until(lambda: "Still digging through Forgejo for the epic." in rig.said_text())
        assert rig.call.queued is None  # answered: nothing waits
        script.go.set()
        await _until(lambda: "It is issue 2059, in phase three." in rig.said_text())

    run(scenario())
    assert rig.said_text() == [
        "Still digging through Forgejo for the epic.",
        "It is issue 2059, in phase three.",
    ]
    assert _statuses(rig.call.cid) == ["pending", "done"]
    assert _questions(rig.call.cid) == ["Where is the federation epic?"]
    (question, doing, said) = rig.interjected[0]
    assert (question, said) == ("Where is the federation epic?", "What are you doing?")
    assert doing  # what HQ is doing, never empty


def test_steer_reaches_the_next_request_once_and_is_stored(rig, held):
    rig.interject_verdict = ("steer", "Ah, got it, checking that.")

    async def scenario():
        # Request 1 answers at once (a tool call); request 2 is held while the Commander speaks.
        script = held(
            call("list_projects", cid="a"),
            call("list_agents", cid="b"),
            answer("It is 2059."),
            free=1,
        )
        await _working(rig, script)
        await _speak(rig, "It's issue 2059.")
        await _until(lambda: "Ah, got it, checking that." in rig.said_text())
        script.go.set()
        await _until(lambda: "It is 2059." in rig.said_text())
        return script

    script = run(scenario())
    carried = [
        json.loads(m["content"]).get("commander_said")
        for b in script.bodies
        for m in b["messages"]
        if m.get("role") == "tool"
    ]
    assert carried.count("It's issue 2059.") == 1  # in exactly one request's newest result
    recs = [r for r in ask_store.read(rig.call.cid) if r["type"] == "interjection"]
    assert [r["text"] for r in recs] == ["It's issue 2059."]
    assert _statuses(rig.call.cid) == ["pending", "done"]  # no second turn: it was used
    assert rig.said_text() == ["Ah, got it, checking that.", "It is 2059."]


def test_a_steer_the_turn_never_used_is_asked_next_exactly_once(rig, held):
    """The words arrive while the turn's LAST request is in flight: it settles without them, and
    they come back to the call (`take_unconsumed`) and are asked as the next question."""
    rig.interject_verdict = ("steer", "Ah, got it.")

    async def scenario():
        script = held(answer("Here is the epic."), answer("Issue 2059 is in phase three."))
        await _working(rig, script)
        await _speak(rig, "It's issue 2059.")
        await _until(lambda: "Ah, got it." in rig.said_text())
        script.go.set()
        # Waited on the LAST spoken line: it is said after its turn's status is written.
        await _until(lambda: "Issue 2059 is in phase three." in rig.said_text())

    run(scenario())
    assert _questions(rig.call.cid) == ["Where is the federation epic?", "It's issue 2059."]
    assert not [r for r in ask_store.read(rig.call.cid) if r["type"] == "interjection"]
    assert rig.said_text() == ["Ah, got it.", "Here is the epic.", "Issue 2059 is in phase three."]


def test_later_says_the_running_answer_first_then_asks_the_new_words(rig, held):
    rig.interject_verdict = ("later", "Sure, one sec, finishing this first.")

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("Two sessions need you."))
        await _working(rig, script)
        await _speak(rig, "Also, what needs me?")
        await _until(lambda: "Sure, one sec, finishing this first." in rig.said_text())
        assert rig.call.queued is not None
        script.go.set()
        await _until(lambda: "Two sessions need you." in rig.said_text())

    run(scenario())
    assert rig.said_text() == [
        "Sure, one sec, finishing this first.",
        "The epic is in phase three.",  # a person finishes their sentence first
        "Two sessions need you.",
    ]
    assert _questions(rig.call.cid) == ["Where is the federation epic?", "Also, what needs me?"]


def test_switch_stops_the_turn_quietly_and_asks_the_new_words_after_it_settled(rig, held):
    rig.interject_verdict = ("switch", "Okay, dropping that.")

    async def scenario():
        # A held request is recorded only once released: the stopped one never is, so the
        # replacement's request is the script's first.
        script = held(answer("Staging is green."))
        await _working(rig, script)
        await _speak(rig, "Forget that, how is staging?")
        await _until(lambda: _statuses(rig.call.cid)[:2] == ["pending", "stopped"])
        script.go.set()  # the replacement's request
        await _until(lambda: "Staging is green." in rig.said_text())

    run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "stopped", "pending", "done"]
    assert voice_calls.STOPPED_LINE not in rig.said_text()  # HQ already said why
    assert rig.said_text() == ["Okay, dropping that.", "Staging is green."]
    assert _questions(rig.call.cid)[-1] == "Forget that, how is staging?"


# ---- no verdict ----------------------------------------------------------------------------------


CRED_LINE = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize("verdict", [None, ("status", ""), ("status", CRED_LINE)])
def test_no_usable_verdict_says_the_fallback_and_the_words_wait(rig, held, verdict):
    """No verdict, an empty line, or a line the text boundary withholds: the fixed fallback, and
    the words are queued exactly as before #2113."""
    rig.interject_verdict = verdict

    async def scenario():
        script = held(answer("first"), answer("second"))
        await _working(rig, script)
        await _speak(rig, "And the other thing?")
        await _until(lambda: voice_calls.INTERJECT_FALLBACK in rig.said_text())
        assert rig.call.queued is not None
        script.go.set()
        await _until(lambda: "second" in rig.said_text())

    run(scenario())
    assert rig.said_text() == [voice_calls.INTERJECT_FALLBACK, "first", "second"]
    assert all("AWS_SECRET" not in t for t in rig.said_text())


def test_a_bare_stop_phrase_still_stops_without_a_judgement(rig, held):
    async def scenario():
        script = held(answer("never said"))
        await _working(rig, script)
        await _speak(rig, "Stop.")
        await _until(lambda: voice_calls.STOPPED_LINE in rig.said_text())

    run(scenario())
    assert rig.interjected == []
    assert rig.said_text() == ["Stopped."]


# ---- the fences ----------------------------------------------------------------------------------


def test_words_during_a_judgement_join_it_and_only_the_newest_verdict_acts(rig, held):
    """Two judgements finishing in reverse order: the first (older) verdict does nothing."""
    first_go = asyncio.Event()
    verdicts = iter([("switch", "Okay, dropping that."), ("status", "Still on the epic.")])

    async def judged():
        v = next(verdicts)
        if v[0] == "switch":
            await first_go.wait()  # the older judgement finishes LAST
        return v

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."))
        await _working(rig, script)
        await _speak(rig, "Hey,")
        await _until(lambda: len(rig.interjected) == 1)
        await _speak(rig, "what are you doing?")
        await _until(lambda: "Still on the epic." in rig.said_text())
        first_go.set()  # the stale switch now returns
        await asyncio.sleep(0.2)
        assert ask_orchestrator.running_turn(rig.call.cid) is not None  # never stopped
        script.go.set()
        await _until(lambda: "The epic is in phase three." in rig.said_text())

    run(scenario())
    assert rig.interjected[1][2] == "Hey, what are you doing?"  # joined, judged again
    assert rig.said_text() == ["Still on the epic.", "The epic is in phase three."]
    assert _statuses(rig.call.cid) == ["pending", "done"]


def test_a_turn_that_settled_during_the_judgement_gets_the_words_as_a_new_question(rig, held):
    """A late `switch` never stops anything: its turn is gone, so the words are simply asked."""
    gate = asyncio.Event()

    async def judged():
        await gate.wait()
        return ("switch", "Okay, dropping that.")

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("Staging is green."))
        await _working(rig, script)
        await _speak(rig, "Forget that, how is staging?")
        await _until(lambda: len(rig.interjected) == 1)
        script.go.set()
        await _until(lambda: _statuses(rig.call.cid) == ["pending", "done"])
        gate.set()
        await _until(lambda: "Staging is green." in rig.said_text())

    run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "done", "pending", "done"]
    assert "Okay, dropping that." not in rig.said_text()
    assert rig.said_text() == ["The epic is in phase three.", "Staging is green."]


@pytest.mark.parametrize("how", ["stop", "mute", "end"])
def test_stop_mute_or_the_end_drop_a_pending_judgement(rig, held, how):
    gate = asyncio.Event()

    async def judged():
        await gate.wait()
        return ("later", "Sure, one sec.")

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("the answer"))
        await _working(rig, script)
        await _speak(rig, "And the other thing?")
        await _until(lambda: len(rig.interjected) == 1)
        if how == "stop":
            assert rig.call.stop_turn()
        elif how == "mute":
            rig.call.muted = True
        else:
            await rig.call.end("hung_up")
        gate.set()
        await asyncio.sleep(0.2)
        assert rig.call.queued is None
        assert "Sure, one sec." not in rig.said_text()
        script.go.set()

    run(scenario())


def test_the_judgement_is_told_what_hq_is_doing_never_tool_output():
    work = {"phase": "tool", "tool": "connector_search", "last_aside": "Looking in Forgejo."}
    assert voice_calls._doing(work) == "Looking in Forgejo. (running its connector_search tool)"
    assert voice_calls._doing({"phase": "model"}) == "thinking about the answer"
    assert voice_calls._doing(None) == "thinking about the answer"


# ---- the judgement's own parse -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "verdict"),
    [
        ('{"do": "status", "say": "Still on it."}', ("status", "Still on it.")),
        ('```json\n{"do": "later", "say": "Sure."}\n```', ("later", "Sure.")),
        ('{"do": "steer", "say": "  Got   it. "}', ("steer", "Got it.")),
        ('{"do": "stop", "say": "x"}', None),
        ('{"do": "status", "say": ""}', None),
        ('{"do": "status"}', None),
        ('{"do": "status", "say": "' + "x" * 400 + '"}', None),
        ("status: still on it", None),
        ("[]", None),
        (None, None),
    ],
)
def test_the_verdict_parse_is_strict(content, verdict):
    assert voice_judge.parse_interjection(content) == verdict


# ---- Hermes on #2124 -----------------------------------------------------------------------------


def test_stop_after_an_accepted_steer_never_restarts_it(rig, held):
    """The steer was accepted while the turn's LAST request was in flight, then the Commander
    pressed Stop: the correction is dropped with the work, never asked as a new question."""
    rig.interject_verdict = ("steer", "Ah, got it.")

    async def scenario():
        script = held(answer("never said"))
        await _working(rig, script)
        await _speak(rig, "Check issue 2059.")
        await _until(lambda: "Ah, got it." in rig.said_text())
        assert rig.call.stop_turn()
        await _until(lambda: _statuses(rig.call.cid) == ["pending", "stopped"])
        await asyncio.sleep(0.3)

    run(scenario())
    assert _statuses(rig.call.cid) == ["pending", "stopped"]
    assert _questions(rig.call.cid) == ["Where is the federation epic?"]


def test_a_switch_after_an_accepted_steer_asks_only_the_switch(rig, held):
    verdicts = iter([("steer", "Ah, got it."), ("switch", "Okay, dropping that.")])

    async def judged():
        return next(verdicts)

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("Staging is green."))
        await _working(rig, script)
        await _speak(rig, "Check issue 2059.")
        await _until(lambda: "Ah, got it." in rig.said_text())
        await _speak(rig, "Forget that, how is staging?")
        await _until(lambda: _statuses(rig.call.cid)[:2] == ["pending", "stopped"])
        script.go.set()
        await _until(lambda: "Staging is green." in rig.said_text())

    run(scenario())
    assert _questions(rig.call.cid) == [
        "Where is the federation epic?",
        "Forget that, how is staging?",
    ]


def test_a_turn_that_settles_while_the_caption_goes_out_gets_the_words_as_a_question(rig, held):
    """Hermes on #2124: the caption is awaited between the check and the judgement. If the turn
    settles in that window the words are asked as a new question, never lost."""

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("I'm free now."))
        real_client = rig.call._to_client

        async def client(m):
            await real_client(m)
            if isinstance(m, dict) and m.get("t") == "caption" and m.get("text") == "Hey, HQ?":
                script.go.set()  # the running turn answers while this caption is being sent
                await _until(lambda: rig.call.call_turn is None)

        rig.call._to_client = client
        await _working(rig, script)
        await _speak(rig, "Hey, HQ?")
        await _until(lambda: "I'm free now." in rig.said_text())

    run(scenario())
    assert rig.interjected == []  # never judged: there was nothing running any more
    assert _questions(rig.call.cid) == ["Where is the federation epic?", "Hey, HQ?"]


@pytest.mark.parametrize(
    "content", ['{"do": [], "say": "x"}', '{"do": {}, "say": "x"}', "[1]", "not json at all"]
)
def test_a_malformed_verdict_from_the_endpoint_says_the_fallback_and_keeps_the_words(
    rig, held, monkeypatch, content
):
    """Through the REAL judgement and transport: a verdict of the wrong shape is no verdict."""
    monkeypatch.setattr(voice_judge, "judge_interjection", REAL_JUDGE)
    judge_prefix = prompts.effective("hq_call_interject")[:80]

    async def scenario():
        script = held(answer("first"), answer("second"))
        held_handler = script.handler

        async def handler(request):
            body = json.loads(request.content)
            system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
            if isinstance(system, str) and system.startswith(judge_prefix):
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
                )
            return await held_handler(request)

        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
        await _working(rig, script)
        await _speak(rig, "And the other thing?")
        await _until(lambda: voice_calls.INTERJECT_FALLBACK in rig.said_text())
        assert rig.call.queued is not None
        script.go.set()
        await _until(lambda: "second" in rig.said_text())

    run(scenario())
    assert rig.said_text() == [voice_calls.INTERJECT_FALLBACK, "first", "second"]


def test_a_quick_mute_and_unmute_drops_the_pending_judgement(rig, held):
    """Hermes on #2124: mute → unmute → the old verdict returns. It acts on nothing."""
    gate = asyncio.Event()

    async def judged():
        await gate.wait()
        return ("switch", "Okay, dropping that.")

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."))
        await _working(rig, script)
        await _speak(rig, "Forget that.")
        await _until(lambda: len(rig.interjected) == 1)
        rig.call.on_mute(True)
        rig.call.on_mute(False)
        gate.set()
        await asyncio.sleep(0.2)
        assert ask_orchestrator.running_turn(rig.call.cid) is not None  # not stopped
        script.go.set()
        await _until(lambda: "The epic is in phase three." in rig.said_text())

    run(scenario())
    assert "Okay, dropping that." not in rig.said_text()
    assert _statuses(rig.call.cid) == ["pending", "done"]


def test_corrections_a_turn_used_reach_the_next_turns_context(rig, held):
    """Hermes on #2124: an `interjection` the turn consumed is replayed with its question, in
    order, so "continue with that issue" still knows which issue."""
    verdicts = iter([("steer", "Got it."), ("steer", "Okay.")])

    async def judged():
        return next(verdicts)

    rig.interject_verdict = judged

    async def scenario():
        script = held(
            call("list_projects", cid="a"),
            call("list_agents", cid="b"),
            answer("Found it."),
            answer("Continuing."),
            free=1,
        )
        await _working(rig, script)
        await _speak(rig, "Use issue 2059.")
        await _until(lambda: "Got it." in rig.said_text())
        await _speak(rig, "Not 2059, I meant 2060.")
        await _until(lambda: "Okay." in rig.said_text())
        script.go.set()
        await _until(lambda: "Found it." in rig.said_text())
        await _speak(rig, "Continue with that issue.")
        await _until(lambda: "Continuing." in rig.said_text())
        return script

    script = run(scenario())
    # Both words reached the turn (carried together into the one request that followed them).
    stored = " ".join(
        r["text"] for r in ask_store.read(rig.call.cid) if r["type"] == "interjection"
    )
    assert stored.index("Use issue 2059.") < stored.index("Not 2059, I meant 2060.")
    # The "Continue" turn's own request (the last one with tools; the call-end check follows it).
    last = [b for b in script.bodies if b.get("tools")][-1]["messages"]
    replayed = next(m["content"] for m in last if m["role"] == "user" and "federation" in str(m))
    assert replayed.index("Use issue 2059.") < replayed.index("Not 2059, I meant 2060.")
    assert ask_orchestrator.STEER_NOTE.split("{")[0] in replayed


def test_history_replays_interjections_with_their_own_question_only():
    recs = [
        {"type": "user", "turn_id": "t1", "text": "Where is the epic?"},
        {"type": "interjection", "turn_id": "t1", "text": "Use 2059."},
        {"type": "interjection", "turn_id": "t1", "text": "No, 2060."},
        {"type": "assistant", "turn_id": "t1", "text": "2060 is in phase three."},
        {"type": "status", "turn_id": "t1", "status": "done"},
        {"type": "user", "turn_id": "t2", "text": "Thanks."},
        {"type": "assistant", "turn_id": "t2", "text": "Sure."},
        {"type": "status", "turn_id": "t2", "status": "done"},
    ]
    out, _ = ask_orchestrator._history(recs, "t3")
    first, second = out[0]["content"], out[2]["content"]
    assert first.startswith("Where is the epic?")
    assert first.index("Use 2059.") < first.index("No, 2060.")
    assert second == "Thanks."


# ---- Hermes on #2124, round 2: the window while the caption goes out ----------------------------


def _hook_caption(rig, text: str, during):
    """Run ``during()`` while the caption of ``text`` is being sent (after it went out, before
    the send returns), like a slow socket."""
    real_client = rig.call._to_client

    async def client(m):
        await real_client(m)
        if isinstance(m, dict) and m.get("t") == "caption" and m.get("text") == text:
            await during()

    rig.call._to_client = client


def test_stop_during_the_caption_send_takes_the_words_back(rig, held):
    rig.interject_verdict = ("steer", "Ah, got it.")

    async def scenario():
        script = held(answer("never said"))

        async def stop_now():
            assert rig.call.stop_turn()
            await _until(lambda: _statuses(rig.call.cid) == ["pending", "stopped"])

        _hook_caption(rig, "Check issue 2059.", stop_now)
        await _working(rig, script)
        await _speak(rig, "Check issue 2059.")
        await asyncio.sleep(0.3)

    run(scenario())
    assert rig.interjected == []  # never judged
    assert _statuses(rig.call.cid) == ["pending", "stopped"]
    assert _questions(rig.call.cid) == ["Where is the federation epic?"]  # never restarted


def test_a_stale_switch_returning_during_the_next_caption_acts_on_nothing(rig, held):
    """The old judgement ("Forget that,") returns while the correction's caption is still being
    sent: the correction already holds the newest sequence, so the old switch does nothing."""
    first_go = asyncio.Event()
    verdicts = iter([("switch", "Dropping it."), ("status", "Okay, keeping at it.")])

    async def judged():
        v = next(verdicts)
        if v[0] == "switch":
            await first_go.wait()
        return v

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."))

        async def release_old_switch():
            first_go.set()
            await asyncio.sleep(0.3)  # the old verdict returns while this caption is in flight
            assert ask_orchestrator.running_turn(rig.call.cid) is not None

        _hook_caption(rig, "actually keep going.", release_old_switch)
        await _working(rig, script)
        await _speak(rig, "Forget that,")
        await _until(lambda: len(rig.interjected) == 1)
        await _speak(rig, "actually keep going.")
        await _until(lambda: "Okay, keeping at it." in rig.said_text())
        script.go.set()
        await _until(lambda: "The epic is in phase three." in rig.said_text())

    run(scenario())
    assert "Dropping it." not in rig.said_text()
    assert _statuses(rig.call.cid) == ["pending", "done"]
    assert rig.interjected[1][2] == "Forget that, actually keep going."


def test_a_quick_mute_and_unmute_during_the_caption_takes_the_words_back(rig, held):
    rig.interject_verdict = ("switch", "Okay, dropping that.")

    async def scenario():
        script = held(answer("The epic is in phase three."))

        async def mute_unmute():
            rig.call.on_mute(True)
            rig.call.on_mute(False)

        _hook_caption(rig, "Forget that.", mute_unmute)
        await _working(rig, script)
        await _speak(rig, "Forget that.")
        await asyncio.sleep(0.3)
        assert ask_orchestrator.running_turn(rig.call.cid) is not None
        script.go.set()
        await _until(lambda: "The epic is in phase three." in rig.said_text())

    run(scenario())
    assert rig.interjected == []
    assert "Okay, dropping that." not in rig.said_text()
    assert _statuses(rig.call.cid) == ["pending", "done"]


# ---- the settle window (flaky under load before: #2113 follow-up) -------------------------------


def test_a_verdict_for_a_turn_settled_but_still_in_its_slot_is_a_new_question(rig, held):
    """`running_turn` still names a turn for a moment after its settle; the fence asks
    `turn_live`. A `switch` returning in that window stops nothing and says nothing about it:
    the words are a new question, after the turn's own answer."""
    gate = asyncio.Event()

    async def judged():
        await gate.wait()
        return ("switch", "Okay, dropping that.")

    rig.interject_verdict = judged

    async def scenario():
        script = held(answer("The epic is in phase three."), answer("Staging is green."))
        tid = await _working(rig, script)
        await _speak(rig, "Forget that, how is staging?")
        await _until(lambda: len(rig.interjected) == 1)
        turn = ask_orchestrator._RUNNING[rig.call.cid].turn
        turn.settled = True  # the window: settled, its slot not released yet
        assert ask_orchestrator.running_turn(rig.call.cid) == tid
        assert not ask_orchestrator.turn_live(rig.call.cid, tid)
        gate.set()
        await _until(lambda: rig.call.queued is not None)
        assert "Okay, dropping that." not in rig.said_text()
        script.go.set()
        await _until(lambda: "Staging is green." in rig.said_text())

    run(scenario())
    assert rig.said_text() == ["The epic is in phase three.", "Staging is green."]
    assert _questions(rig.call.cid)[-1] == "Forget that, how is staging?"


def test_turn_live_is_stricter_than_running_turn():
    a = ask_orchestrator
    t = a._Turn(
        cid="c-live", turn_id="t1", question="q", working_keys=set(), progress=lambda e: None
    )
    a._RUNNING["c-live"] = a._Running(turn_id="t1", task=None, turn=t)
    try:
        assert a.running_turn("c-live") == "t1" and a.turn_live("c-live", "t1")
        assert not a.turn_live("c-live", "other")
        t.stop.set()
        assert a.running_turn("c-live") == "t1" and not a.turn_live("c-live", "t1")
        t.stop.clear()
        t.settled = True
        assert a.running_turn("c-live") == "t1" and not a.turn_live("c-live", "t1")
    finally:
        del a._RUNNING["c-live"]
