"""#1668 — a call ends when the operator means it.

HQ judges whether what was said means the call is over (any wording, any language) and, if so,
uses `end_call` on that call turn. The call then ends after HQ's farewell has PLAYED (its own
`played` ack), or, when no ack comes, once it had time to play after its audio went out (an
unconfirmed-playback end). An answer that asks something, speaking again after the bye, newer
speech, a card that still needs the operator, or an empty/failed answer keep it open. No automatic
end otherwise: after 30 minutes without a sign of the operator HQ asks once, and only no answer
within 60 s of that question having played ends the call (`no_answer`).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from agent_sessions import ask_orchestrator, voice_calls
from test_ask_orchestrator import ai, answer, app, call  # noqa: F401 — fixtures
from test_voice_confirm import NUDGE, Rig, run

END = {"end_call": True, "answer": "Goodbye, Commander."}


def last_say(r: Rig) -> dict:
    return r.says()[-1]


# --- HQ's tool -----------------------------------------------------------------------------------


def test_end_call_is_offered_on_call_turns_only():
    typed = {s["function"]["name"] for s in ask_orchestrator.tool_specs()}
    call = {s["function"]["name"] for s in ask_orchestrator.tool_specs("call")}
    assert "end_call" not in typed
    assert "end_call" in call
    assert call - typed == {"end_call"}  # nothing else differs


def test_end_call_is_refused_off_a_call():
    """Even if a typed turn calls it anyway, the server refuses: nothing can end there."""
    turn = ask_orchestrator._Turn(
        cid="c", turn_id="t", question="bye", working_keys=set(), progress=lambda ev: None
    )
    with pytest.raises(ask_orchestrator._ToolRefused):
        run(ask_orchestrator._end_call(turn, {}))
    assert turn.end_call is False
    turn.via = "call"
    run(ask_orchestrator._end_call(turn, {}))
    assert turn.end_call is True


# --- ending after the farewell -------------------------------------------------------------------


def test_the_call_ends_once_the_farewell_has_played(monkeypatch):
    r = Rig(monkeypatch, END)

    async def go():
        await r.ask("Okay, that's all, thanks, bye")
        assert r.call.state != "ended"  # not before the goodbye was heard
        farewell = last_say(r)
        assert farewell["text"] == "Goodbye, Commander."
        await r.call.on_played("someone-else")  # a stale ack for another say: nothing
        assert r.call.state != "ended"
        await r.call.on_played(farewell["id"])

    run(go())
    assert r.call.state == "ended"
    assert r.call.ended_reason == "hq_ended"


def test_without_a_played_ack_it_ends_once_the_farewell_had_time_to_play(monkeypatch):
    """The unconfirmed-playback end: a backgrounded tab never acks; the intent was judged. The
    fallback counts from when the worker sent the farewell's audio (`said`), plus how long its
    words take to say."""
    r = Rig(monkeypatch, END)
    clock = {"now": 1000.0}
    monkeypatch.setattr(voice_calls.time, "monotonic", lambda: clock["now"])
    window = voice_calls.speech_seconds(END["answer"]) + voice_calls.PLAYBACK_FALLBACK_S

    async def go():
        await r.ask("Tschüss")
        clock["now"] += window * 5  # its audio has not gone out yet: no window has started
        await r.call.check_silence()
        assert r.call.state != "ended"
        await r.ev(t="said", id=last_say(r)["id"])
        clock["now"] += window - 0.1
        await r.call.check_silence()
        assert r.call.state != "ended"
        clock["now"] += 0.2
        await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "hq_ended"


def test_a_turn_without_end_call_never_ends_the_call(monkeypatch):
    r = Rig(monkeypatch, {"answer": "Say bye to the docs session? It's idle; I can close it."})

    async def go():
        await r.ask("Say bye to the docs session")
        await r.call.on_played(last_say(r)["id"])
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


def test_speaking_again_after_the_farewell_cancels_the_end(monkeypatch):
    r = Rig(monkeypatch, END)

    async def go():
        await r.ask("bye")
        farewell = last_say(r)
        await r.ev(t="user_started")  # "…wait, one more thing"
        await r.call.on_played(farewell["id"])

    run(go())
    assert r.call.state != "ended"


def test_a_card_that_still_needs_the_operator_keeps_the_call(monkeypatch):
    r = Rig(monkeypatch, {**NUDGE, **END})

    async def go():
        await r.ask("bye")
        for s in r.says():
            await r.call.on_played(s["id"])

    run(go())
    assert r.call.state != "ended"


@pytest.mark.parametrize(
    "view",
    [{"end_call": True, "answer": ""}, {"end_call": True, "status": "failed", "detail": "x"}],
)
def test_an_empty_or_failed_answer_never_ends_the_call(monkeypatch, view):
    r = Rig(monkeypatch, view)

    async def go():
        await r.ask("bye")
        for s in r.says():
            await r.call.on_played(s["id"])
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


# --- silence: HQ asks, only no answer ends -------------------------------------------------------


class Clock:
    def __init__(self, monkeypatch):
        self.now = 10_000.0
        monkeypatch.setattr(voice_calls.time, "monotonic", lambda: self.now)


def still_there_says(r: Rig) -> list[dict]:
    return [s for s in r.says() if s["text"] == voice_calls.STILL_THERE_LINE]


def test_a_quiet_call_is_asked_at_30_minutes_and_not_before(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S - 1
        await r.call.check_silence()
        assert still_there_says(r) == []
        clock.now += 2
        await r.call.check_silence()
        assert len(still_there_says(r)) == 1
        await r.call.check_silence()  # asked once, not every tick
        assert len(still_there_says(r)) == 1

    run(go())
    assert r.call.state != "ended"


def test_no_answer_ends_it_60_s_after_the_question_played_and_never_before(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        q = still_there_says(r)[0]
        clock.now += 5  # still playing
        await r.call.on_played(q["id"])
        clock.now += voice_calls.NO_ANSWER_S - 0.5
        await r.call.check_silence()
        assert r.call.state != "ended"
        clock.now += 1
        await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "no_answer"


def test_without_an_ack_the_countdown_starts_once_the_question_had_time_to_play(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now
    played = (
        voice_calls.speech_seconds(voice_calls.STILL_THERE_LINE) + voice_calls.PLAYBACK_FALLBACK_S
    )

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        await r.ev(t="said", id=still_there_says(r)[-1]["id"])
        clock.now += played + voice_calls.NO_ANSWER_S - 0.5
        await r.call.check_silence()
        assert r.call.state != "ended"
        clock.now += 1
        await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "no_answer"


def test_a_reply_keeps_the_call_even_while_the_question_is_still_playing(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        await r.ask("Yes, still here")  # before the question's ack
        clock.now += voice_calls.NO_ANSWER_S * 3
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


def test_a_responsive_call_survives_past_an_hour(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        for _ in range(3):  # 3 × (30 min of quiet, the question, a reply)
            clock.now += voice_calls.SILENCE_ASK_S + 1
            await r.call.check_silence()
            q = still_there_says(r)[-1]
            await r.call.on_played(q["id"])
            clock.now += 10
            await r.ask("Still here")
            await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"
    assert len(still_there_says(r)) == 3


def test_housekeeping_and_stale_acks_do_not_reset_the_silence_clock(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S - 60
        # Worker housekeeping, a flush and a stale ack are not signs of the operator.
        await r.ev(t="user_stopped")
        await r.ev(t="interrupt")
        await r.call.on_played("not-a-say-of-this-call")
        clock.now += 61
        await r.call.check_silence()

    run(go())
    assert len(still_there_says(r)) == 1


def test_mute_does_not_defer_the_question(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now
    r.call.muted = True

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()

    run(go())
    assert len(still_there_says(r)) == 1


# --- the focused "is the call over?" judgement ---------------------------------------------------


def _turn(question: str, via: str | None = "call") -> ask_orchestrator._Turn:
    return ask_orchestrator._Turn(
        cid="c", turn_id="t", question=question, working_keys=set(), progress=lambda ev: None,
        via=via,
    )  # fmt: skip


def test_the_judgement_looks_only_at_short_call_turns_hq_did_not_end():
    assert ask_orchestrator._judges_call_end(_turn("Bye for now"))
    assert not ask_orchestrator._judges_call_end(_turn("Bye for now", via=None))  # typed
    ended = _turn("Bye")
    ended.end_call = True
    assert not ask_orchestrator._judges_call_end(ended)  # HQ already hung up
    long = " ".join(["word"] * (ask_orchestrator.CALL_END_JUDGE_MAX_WORDS + 1))
    assert not ask_orchestrator._judges_call_end(_turn(long))


class _Reply:
    def __init__(self, status: int, content: object, finish: str = "stop"):
        self.status_code = status
        self._content = content
        self._finish = finish

    def json(self):
        return {"choices": [{"message": {"content": self._content}, "finish_reason": self._finish}]}


@pytest.mark.parametrize(
    ("status", "content", "over"),
    [
        (200, "yes", True),
        (200, "Yes.", True),
        (200, "no", False),
        (200, "yes, probably", False),  # anything but a plain yes keeps the call
        (200, None, False),
        (500, "yes", False),  # an endpoint failure keeps the call
    ],
)
def test_the_judgement_fails_closed(monkeypatch, status, content, over):
    from agent_sessions import review

    seen: list[dict] = []

    async def fake_post(cfg, body, **kw):
        seen.append(body)
        return _Reply(status, content)

    monkeypatch.setattr(review, "post_chat_response", fake_post)
    got = run(ask_orchestrator._call_is_over(_turn("Bye for now"), {"model": "m"}, "Goodbye."))
    assert got is over
    msgs = seen[0]["messages"]
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    assert "tools" not in seen[0]  # a judgement, never a tool-using turn


def test_the_judgement_never_raises(monkeypatch):
    from agent_sessions import review

    async def boom(cfg, body, **kw):
        raise review.ReviewError("down")

    monkeypatch.setattr(review, "post_chat_response", boom)
    assert run(ask_orchestrator._call_is_over(_turn("Bye"), {"model": "m"}, "Bye.")) is False


# --- interleavings (Hermes 6983) -----------------------------------------------------------------


def test_a_farewell_acked_while_its_say_is_still_being_sent_still_ends_the_call(monkeypatch):
    """The browser can play a short farewell (and ack it) before the awaited worker send returns:
    the end intent and the say id must already be in place by then."""
    r = Rig(monkeypatch, END)
    real = r.call._to_worker

    async def to_worker(m):
        await real(m)
        if isinstance(m, dict) and m.get("t") == "say" and m.get("text") == END["answer"]:
            await r.call.on_played(m["id"])  # acked during the send

    r.call._to_worker = to_worker

    async def go():
        await r.ask("Okay, that's all, bye")

    run(go())
    assert r.call.state == "ended"
    assert r.call.ended_reason == "hq_ended"


def test_a_reply_during_the_still_there_send_is_never_lost(monkeypatch):
    """A reply that lands while the question is being sent clears it; the question is never
    installed afterwards to end a responsive call 70 s later."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now
    real = r.call._to_worker

    async def to_worker(m):
        await real(m)
        if isinstance(m, dict) and m.get("text") == voice_calls.STILL_THERE_LINE:
            await r.call.on_utterance("Yes, I'm here")  # replied during the send

    r.call._to_worker = to_worker

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        if r.call.turn_task is not None:
            await r.call.turn_task
        clock.now += voice_calls.PLAYBACK_FALLBACK_S + voice_calls.NO_ANSWER_S + 5
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"
    assert r.call.still_there is None


def test_a_duplicate_or_old_ack_is_not_new_speech(monkeypatch):
    """Each say counts as heard ONCE: a second ack for an old greeting doesn't reset the clock."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)

    async def go():
        sid = await r.call.say("HQ, General Seo speaking.", kind="greeting")
        await r.call.on_played(sid)  # heard: real speech
        r.call.last_speech = clock.now
        clock.now += voice_calls.SILENCE_ASK_S - 1
        await r.call.on_played(sid)  # the same ack again, a second before the 30 minutes
        clock.now += 2
        await r.call.check_silence()

    run(go())
    assert len(still_there_says(r)) == 1  # the question came on time


def test_an_ack_for_an_interrupted_say_is_not_speech(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)

    async def go():
        sid = await r.call.say("A long answer that gets talked over.")
        await r.ev(t="interrupt")  # the browser flushed it: it never played
        r.call.last_speech = clock.now
        clock.now += voice_calls.SILENCE_ASK_S - 1
        await r.call.on_played(sid)
        clock.now += 2
        await r.call.check_silence()

    run(go())
    assert len(still_there_says(r)) == 1


# --- the fallback clock starts when the line is SENT (Hermes 6987) -------------------------------


def _held_worker(r: Rig, text: str):
    """Hold the worker send of the say with this text until `release` is set."""
    import asyncio

    release = asyncio.Event()
    real = r.call._to_worker

    async def to_worker(m):
        if isinstance(m, dict) and m.get("t") == "say" and m.get("text") == text:
            await release.wait()
        await real(m)

    r.call._to_worker = to_worker
    return release


def test_a_farewell_still_being_sent_is_not_hung_up_on_by_the_fallback(monkeypatch):
    import asyncio

    clock = Clock(monkeypatch)
    r = Rig(monkeypatch, END)

    async def go():
        release = _held_worker(r, END["answer"])
        ask = asyncio.ensure_future(r.ask("bye"))
        await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        clock.now += voice_calls.PLAYBACK_FALLBACK_S * 3  # the send is stuck for 30 s
        await r.call.check_silence()
        assert r.call.state != "ended"  # never before the farewell went out
        release.set()
        await ask
        await r.ev(t="said", id=last_say(r)["id"])  # its audio went out
        window = voice_calls.speech_seconds(END["answer"]) + voice_calls.PLAYBACK_FALLBACK_S
        clock.now += window - 0.1
        await r.call.check_silence()
        assert r.call.state != "ended"
        clock.now += 0.2
        await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "hq_ended"


def test_the_still_there_countdown_starts_when_the_question_was_sent(monkeypatch):
    import asyncio

    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        release = _held_worker(r, voice_calls.STILL_THERE_LINE)
        clock.now += voice_calls.SILENCE_ASK_S + 1
        ask = asyncio.ensure_future(r.call.check_silence())
        for _ in range(5):
            await asyncio.sleep(0)
        clock.now += voice_calls.PLAYBACK_FALLBACK_S + voice_calls.NO_ANSWER_S + 5  # stuck send
        await r.call.check_silence()
        assert r.call.state != "ended"  # the window never started: nothing was asked yet
        release.set()
        await ask
        await r.ev(t="said", id=still_there_says(r)[-1]["id"])
        clock.now += (
            voice_calls.speech_seconds(voice_calls.STILL_THERE_LINE)
            + voice_calls.PLAYBACK_FALLBACK_S
            + voice_calls.NO_ANSWER_S
            - 1
        )
        await r.call.check_silence()
        assert r.call.state != "ended"
        clock.now += 2
        await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "no_answer"


# --- the independent review's findings (#1673) ---------------------------------------------------


def test_an_answer_that_asks_something_never_hangs_up(monkeypatch):
    """However it was judged, HQ asking — "before you go, shall I…?" — is no farewell."""
    clock = Clock(monkeypatch)
    q = "Before you go, Commander: shall I still send that message to the docs session?"
    r = Rig(monkeypatch, {"end_call": True, "answer": q})

    async def go():
        await r.ask("Okay, bye")
        await r.ev(t="said", id=last_say(r)["id"])
        await r.call.on_played(last_say(r)["id"])
        clock.now += 600
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended" and r.call.ending is None


def test_the_judgement_weighs_hq_s_answer_and_has_room_to_think(app, ai):  # noqa: F811
    q = "Before you go, Commander: shall I still send that message to the docs session?"
    script = ai(answer(q), answer("no"))
    cid = app.new()

    async def go():
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "Okay, bye", working_keys=set(), via="call"
        )
        return await task

    view = run(go())
    judge = script.bodies[1]
    said = judge["messages"][1]["content"]
    assert "Okay, bye" in said and q in said  # the words AND what HQ answered them
    assert judge["max_tokens"] >= 512  # a reasoning model gets to its one word
    assert not view.get("end_call")


def test_a_cut_off_judgement_keeps_the_call(app, ai):  # noqa: F811
    ai(answer("Goodbye, Commander."), {"role": "assistant", "content": "yes", "_finish": "length"})
    cid = app.new()

    async def go():
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "bye", working_keys=set(), via="call"
        )
        return await task

    assert not run(go()).get("end_call")


def test_the_judgement_also_runs_where_the_endpoint_refuses_tools(app, ai, monkeypatch):  # noqa: F811
    from agent_sessions import pulse_chat, review

    ai()  # the endpoint's settings; its replies are this transport's
    judged: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "tools" in body:  # this endpoint refuses every request that offers tools
            return httpx.Response(400, json={"error": {"message": "tools are not supported"}})
        judged.append(body)
        msg = {"role": "assistant", "content": "yes"}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))

    async def fake_ask(query, history=None, **kw):
        return {"answer": "Goodbye, Commander.", "matches": [], "mission_matches": []}

    monkeypatch.setattr(pulse_chat, "ask", fake_ask)
    cid = app.new()

    async def go():
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "bye for now", working_keys=set(), via="call"
        )
        return await task

    view = run(go())
    assert view.get("end_call") is True
    assert "HQ answered: Goodbye, Commander." in judged[-1]["messages"][1]["content"]


def _held_turn(monkeypatch, view: dict) -> asyncio.Event:
    gate = asyncio.Event()

    async def slow_begin(cid, turn_id, text, **kw):
        await gate.wait()
        return None, {"turn_id": turn_id, "status": "done", **view}

    monkeypatch.setattr(ask_orchestrator, "begin", slow_begin)
    return gate


@pytest.mark.parametrize("acked", [True, False])
def test_speaking_again_before_the_farewell_is_planned_keeps_the_call(monkeypatch, acked):
    """ "Okay, bye." … "oh wait, one more thing" starts while HQ still answers the bye: its
    words are still on the way, so the farewell must not hang up — acked or not."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch, END)
    gate = _held_turn(monkeypatch, END)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Okay, bye.")
        await asyncio.sleep(0)
        await r.ev(t="user_started")  # the next request begins
        await r.ev(t="interrupt")
        gate.set()
        await r.call.turn_task
        farewell = last_say(r)
        assert farewell["text"] == END["answer"]
        await r.ev(t="said", id=farewell["id"])
        if acked:
            await r.call.on_played(farewell["id"])
        clock.now += 120
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


def test_a_long_farewell_is_never_cut_off_while_it_plays(monkeypatch):
    """A detailed goodbye of ~25 s: the browser acks only at its end, so nothing may hang up
    10 s in — the fallback waits for the words to have been said."""
    clock = Clock(monkeypatch)
    long = "The brief is filed, the fleet is quiet and nothing needs you tonight. " * 4
    r = Rig(monkeypatch, {"end_call": True, "answer": long + "Goodbye, Commander."})

    async def go():
        await r.ask("Give me the full summary and then I'm done")
        farewell = last_say(r)
        await r.ev(t="said", id=farewell["id"])
        clock.now += 25
        await r.call.check_silence()
        assert r.call.state != "ended"
        await r.call.on_played(farewell["id"])  # it played out

    run(go())
    assert r.call.ended_reason == "hq_ended"


def test_a_reply_to_still_there_answers_that_question_not_an_old_one(monkeypatch):
    """HQ asked "Archive the three idle sessions?" (Yes/No) and the Commander walked away. Half
    an hour later "Yes" answers "Are you still there?" — never the archive question."""
    clock = Clock(monkeypatch)
    old = {
        "answer": "",
        "ask": {
            "question_id": "q1",
            "question": "Archive the three idle sessions?",
            "options": ["Yes", "No"],
            "allow_text": False,
        },
    }
    r = Rig(monkeypatch, old)
    r.call.last_speech = clock.now
    seen: list[dict] = []

    async def rec_begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        return None, {"turn_id": turn_id, "status": "done", "answer": "Good to hear."}

    async def go():
        await r.ask("Should I clean up?")
        assert r.call.ask and r.call.ask["question_id"] == "q1"
        monkeypatch.setattr(ask_orchestrator, "begin", rec_begin)
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        assert last_say(r)["text"] == voice_calls.STILL_THERE_LINE
        assert r.call.ask is None  # the old question is no longer what anything answers
        await r.ask("Yes")

    run(go())
    assert seen[-1]["text"] == "Yes"
    assert seen[-1]["answers"] is None and seen[-1]["option"] is None
    assert seen[-1]["replying_to"] == voice_calls.STILL_THERE_LINE


def _typing_rig(monkeypatch, app, view=None):  # noqa: F811
    """A call on a REAL conversation: typed turns go through the HTTP route and the real
    orchestrator; only the call's own spoken turns are recorded."""
    real_begin = ask_orchestrator.begin
    r = Rig(monkeypatch, view)
    r.fake_begin, r.real_begin = ask_orchestrator.begin, real_begin
    cid = app.new()
    r.call.cid = cid
    monkeypatch.setattr(voice_calls.REGISTRY, "current", r.call)
    monkeypatch.setattr(ask_orchestrator, "begin", real_begin)  # the route's typed turns
    return r, cid


def test_a_typed_answer_to_still_there_leaves_the_next_card_to_the_voice(app, ai, monkeypatch):  # noqa: F811
    """Typed "still here" answers the still-there question; HQ's answer to it offers a Yes/No card
    (stored in the conversation), and a spoken "Yes" decides THAT card (Hermes 7002, 7004)."""
    clock = Clock(monkeypatch)
    ai(
        call("ask_operator", {"question": "Archive the idle sessions?", "options": ["Yes", "No"]}),
        answer("never reached"),
    )
    r, cid = _typing_rig(monkeypatch, app)
    r.call.last_speech = clock.now
    seen: list[dict] = []

    async def rec_begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    async def ask_still_there():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()

    run(ask_still_there())
    assert r.call.replying_still_there
    assert app.ask(cid, "Still here, typing").status_code == 200
    asked = r.call.ask  # the typed turn's settled question, synced into the call
    assert asked and asked["options"] == ["Yes", "No"]
    monkeypatch.setattr(ask_orchestrator, "begin", rec_begin)
    run(r.ask("Yes"))
    assert seen[-1]["answers"] == asked["question_id"] and seen[-1]["option"] == 0
    assert seen[-1]["replying_to"] is None


@pytest.mark.parametrize("acked", [True, False])
def test_typing_during_the_farewell_keeps_the_call(app, ai, monkeypatch, acked):  # noqa: F811
    """ "Bye" … then, before HQ's goodbye has played, the Commander types "wait, one more thing":
    an accepted typed turn calls the farewell off — acked or not (Hermes 7004)."""
    clock = Clock(monkeypatch)
    ai(answer("Go ahead, Commander."))
    r, cid = _typing_rig(monkeypatch, app, END)

    async def bye():
        monkeypatch.setattr(ask_orchestrator, "begin", r.fake_begin)
        await r.ask("bye")
        assert r.call.ending is not None

    run(bye())
    monkeypatch.setattr(ask_orchestrator, "begin", r.real_begin)
    assert app.ask(cid, "wait, one more thing").status_code == 200
    farewell = last_say(r)

    async def after():
        await r.ev(t="said", id=farewell["id"])
        if acked:
            await r.call.on_played(farewell["id"])
        clock.now += 120
        await r.call.check_silence()

    run(after())
    assert r.call.state != "ended"


def test_a_refused_typed_turn_does_not_call_the_farewell_off(app, ai, monkeypatch):  # noqa: F811
    ai(answer("unused"))
    r, cid = _typing_rig(monkeypatch, app, END)

    async def bye():
        monkeypatch.setattr(ask_orchestrator, "begin", r.fake_begin)
        await r.ask("bye")

    run(bye())
    monkeypatch.setattr(ask_orchestrator, "begin", r.real_begin)
    # Answering a question this conversation never asked: `begin` refuses it, nothing accepted.
    resp = app.ask(cid, "Yes", answers=str(uuid.uuid4()))
    assert resp.status_code == 422
    assert "not in this conversation" in resp.json()["detail"]
    assert r.call.ending is not None
    run(r.call.on_played(last_say(r)["id"]))
    assert r.call.ended_reason == "hq_ended"


def test_hq_is_told_which_question_a_reply_answers():
    t = _turn("Yes")
    t.replying_to = voice_calls.STILL_THERE_LINE
    assert ask_orchestrator._user_content(t) == (
        f'Answering your question "{voice_calls.STILL_THERE_LINE}": Yes'
    )


def test_voice_activity_alone_never_defers_the_still_there_question(monkeypatch):
    """Voice activity without words (`user_started`, which room noise fires too) is not a sign of
    the Commander: a forgotten tab is still asked at 30 minutes (Hermes 7000)."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S - 60
        await r.ev(t="user_started")
        await r.ev(t="user_stopped")
        clock.now += 61
        await r.call.check_silence()

    run(go())
    assert len(still_there_says(r)) == 1


def test_voice_activity_alone_never_answers_the_still_there_question(monkeypatch):
    """During the question's no-answer window, noise firing the VAD again and again without any
    words keeps nothing alive: the forgotten call still ends `no_answer`."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        await r.ev(t="said", id=still_there_says(r)[-1]["id"])  # its countdown can start
        for _ in range(15):  # 106 s: well past the question's no-answer window
            await r.ev(t="user_started")
            await r.ev(t="interrupt")
            await r.ev(t="user_stopped")
            clock.now += 7.1
            await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "no_answer"


def test_typing_on_the_call_s_conversation_keeps_the_call(app, ai, monkeypatch):  # noqa: F811
    ai(answer("Noted."))
    cid = app.new()
    call = voice_calls.Call(
        id="c", cid=cid, contact="europe-man", voice="v", operator=None, app=None,
        registry=None, state="listening",
    )  # fmt: skip
    call.last_speech = 0.0
    call.still_there = {"say_id": "s", "said_at": None, "played_at": None, "speech_s": 1.0}
    monkeypatch.setattr(voice_calls.REGISTRY, "current", call)
    assert app.ask(cid, "Still here, typing").status_code == 200
    assert call.still_there is None and call.last_speech > 0


# --- the second independent review (#1673, 96d58128) ----------------------------------------------


def test_noise_over_the_still_there_question_still_ends_a_forgotten_call(monkeypatch):
    """A door slams while "Are you still there?" plays: the interruption drops the worker's `said`
    and the browser never acks. The forgotten call still ends, counted from the send."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S + 1
        await r.call.check_silence()
        assert still_there_says(r)
        await r.ev(t="user_started")
        await r.ev(t="interrupt")
        await r.ev(t="user_stopped")
        for _ in range(30):  # 2.5 min of 5 s watchdog ticks
            clock.now += 5
            await r.call.check_silence()

    run(go())
    assert r.call.ended_reason == "no_answer"


def test_a_late_goodbye_transcript_never_hangs_up_on_the_next_sentence(monkeypatch):
    """ "Okay, bye." (turn 1) … the Commander starts turn 2 before turn 1's words land: the bye
    belongs to an older turn, so its farewell never ends the call."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch, END)

    async def go():
        await r.ev(t="user_started")  # turn 1
        await r.ev(t="user_started")  # turn 2 begins: "actually wait, before you go…"
        await r.ev(t="utterance", text="Okay, bye.", turn=1)  # turn 1's words, late
        if r.call.turn_task is not None:
            await r.call.turn_task
        farewell = last_say(r)
        await r.ev(t="said", id=farewell["id"])
        await r.call.on_played(farewell["id"])
        clock.now += 120
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


@pytest.mark.parametrize("a_settles_first", [False, True])
def test_words_begun_before_a_question_was_presented_never_answer_it(monkeypatch, a_settles_first):
    """B is begun while A runs; A then ends on a question. Whether A was superseded (its question
    never spoken) or settled before B's words landed (spoken, but after B began), B is a new
    request — never an answer to A's question (#1673 reviews)."""
    question = {"question_id": "qa", "question": "Which project should it run in?",
                "options": ["Yes", "No"], "allow_text": True}  # fmt: skip
    r = Rig(monkeypatch)
    gate = asyncio.Event()
    seen: list[dict] = []
    stored = {"open": None}  # the conversation's open question, as the store would hold it
    monkeypatch.setattr(
        ask_orchestrator, "open_question", lambda cid: stored["open"], raising=False
    )

    async def begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        if len(seen) == 1:
            await gate.wait()  # A runs while B is begun
            stored["open"] = {**question, "answered": False}
            return None, {"turn_id": turn_id, "status": "done", "answer": "", "ask": question}
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    monkeypatch.setattr(ask_orchestrator, "begin", begin)

    async def go():
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Start a session for the auth refactor")
        await asyncio.sleep(0)
        await r.ev(t="user_started")  # B begins while A runs
        if a_settles_first:
            gate.set()
            await r.call.turn_task  # A settles, its question spoken, B's words not landed yet
            await r.ev(t="utterance", text="Yes, and also check the fleet")
        else:
            await r.ev(t="utterance", text="Yes, and also check the fleet")  # queued
            gate.set()
        while r.call.turn_task is not None and not r.call.turn_task.done():
            await r.call.turn_task

    run(go())
    assert seen[1]["text"] == "Yes, and also check the fleet"
    assert seen[1]["answers"] is None and seen[1]["option"] is None


def test_a_typed_turn_accepted_before_the_farewell_is_planned_keeps_the_call(monkeypatch):
    """The spoken bye-turn settles; before the call plans the farewell, a typed "wait, one more
    thing" is accepted. The farewell is never planned, and nothing ends the call."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch, END)

    async def begin(cid, turn_id, text, **kw):
        r.call.on_typed_turn()  # accepted in the window after the spoken turn's slot freed
        return None, {"turn_id": turn_id, "status": "done", **END}

    monkeypatch.setattr(ask_orchestrator, "begin", begin)

    async def go():
        await r.ask("bye")
        farewell = last_say(r)
        assert farewell["text"] == END["answer"]
        assert r.call.ending is None
        await r.ev(t="said", id=farewell["id"])
        await r.call.on_played(farewell["id"])
        clock.now += 120
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


def test_a_late_goodbye_transcript_never_ends_the_call(monkeypatch):
    """Speech-to-text took longer than the worker's grace: the bye arrives as a LATE transcript,
    its turn gone, while a newer turn may already be under way. It never ends the call."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch, END)

    async def go():
        await r.ev(t="user_started")  # turn 1: "Okay, bye."
        await r.ev(t="user_stopped")
        await r.ev(t="user_started")  # turn 2 begins
        await r.ev(t="utterance", text="Okay, bye.", late=True)
        if r.call.turn_task is not None:
            await r.call.turn_task
        farewell = last_say(r)
        assert farewell["text"] == END["answer"]
        assert r.call.ending is None
        await r.ev(t="said", id=farewell["id"])
        await r.call.on_played(farewell["id"])
        clock.now += 120
        await r.call.check_silence()

    run(go())
    assert r.call.state != "ended"


def test_a_typed_turn_without_a_question_retires_the_spoken_one(app, ai, monkeypatch):  # noqa: F811
    """HQ asked a Yes/No question on the call; the Commander then typed something else, and HQ
    answered without a question. A spoken "Yes" is now a new request (Hermes 7008)."""
    view = {
        "answer": "",
        "ask": {"question_id": "q-old", "question": "Archive the idle sessions?",
                "options": ["Yes", "No"], "allow_text": False},
    }  # fmt: skip
    ai(answer("All quiet, Commander."))
    r, cid = _typing_rig(monkeypatch, app, view)
    seen: list[dict] = []

    async def rec_begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    async def spoken_question():
        monkeypatch.setattr(ask_orchestrator, "begin", r.fake_begin)
        await r.ask("Should I clean up?")
        assert r.call.ask and r.call.ask["question_id"] == "q-old"

    run(spoken_question())
    monkeypatch.setattr(ask_orchestrator, "begin", r.real_begin)
    assert app.ask(cid, "never mind, what's the status?").status_code == 200
    assert r.call.ask is None
    monkeypatch.setattr(ask_orchestrator, "begin", rec_begin)
    run(r.ask("Yes"))
    assert seen[-1]["answers"] is None and seen[-1]["option"] is None


@pytest.mark.parametrize("late", [False, True])
def test_words_begun_before_the_still_there_question_never_answer_it(monkeypatch, late):
    """The Commander began speaking just before the 30-minute mark; the question went out before
    their words landed (or they landed late). They are their own request, never its reply (7008)."""
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    r.call.last_speech = clock.now
    seen: list[dict] = []

    async def rec_begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    monkeypatch.setattr(ask_orchestrator, "begin", rec_begin)

    async def go():
        clock.now += voice_calls.SILENCE_ASK_S - 1
        await r.ev(t="user_started")  # "Yes, I'm here — what's the fleet doing?" begins
        clock.now += 2
        await r.call.check_silence()  # the question goes out before the words land
        assert still_there_says(r)
        if late:
            await r.ev(t="utterance", text="Yes, what's the fleet doing?", late=True)
        else:
            await r.ev(t="utterance", text="Yes, what's the fleet doing?")
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert seen[-1]["replying_to"] is None
    assert r.call.still_there is None  # real words: the Commander is there


def test_replaying_a_settled_typed_turn_changes_nothing_on_the_call(app, ai, monkeypatch):  # noqa: F811
    """A client retries a typed request that already settled (same turn id): it is no new turn,
    so the question HQ asked on the call since then stays the one a spoken "Yes" decides (7010)."""
    view = {
        "answer": "",
        "ask": {"question_id": "q-new", "question": "Archive the idle sessions?",
                "options": ["Yes", "No"], "allow_text": False},
    }  # fmt: skip
    ai(answer("Noted, Commander."))
    r, cid = _typing_rig(monkeypatch, app, view)
    seen: list[dict] = []

    async def rec_begin(cid, turn_id, text, **kw):
        seen.append({"text": text, **kw})
        return None, {"turn_id": turn_id, "status": "done", "answer": "Done."}

    typed_id = str(uuid.uuid4())
    assert app.ask(cid, "Note this down", turn_id=typed_id).status_code == 200

    async def spoken_question():
        monkeypatch.setattr(ask_orchestrator, "begin", r.fake_begin)
        await r.ask("Should I clean up?")
        assert r.call.ask and r.call.ask["question_id"] == "q-new"

    run(spoken_question())
    typed_before = r.call.typed_turns
    monkeypatch.setattr(ask_orchestrator, "begin", r.real_begin)
    assert app.ask(cid, "Note this down", turn_id=typed_id).status_code == 200  # the replay
    assert r.call.ask and r.call.ask["question_id"] == "q-new"
    assert r.call.typed_turns == typed_before
    monkeypatch.setattr(ask_orchestrator, "begin", rec_begin)
    run(r.ask("Yes"))
    assert seen[-1]["answers"] == "q-new" and seen[-1]["option"] == 0


def test_a_retry_of_a_typed_turn_still_running_is_counted_once(app, ai, monkeypatch):  # noqa: F811
    """A slow typed turn, and the client retries it (same turn id) while it still runs: `begin`
    hands back the SAME task. The retry is no new turn — it must not answer the still-there
    question HQ asked meanwhile, nor restart the clock (Hermes 7012)."""
    import threading

    from agent_sessions import review

    ai()  # the endpoint's settings
    entered, release = threading.Event(), threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.to_thread(release.wait, 30)  # HQ is slow to answer
        msg = {"role": "assistant", "content": "Noted, Commander."}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    r, cid = _typing_rig(monkeypatch, app)
    real_begin = ask_orchestrator.begin
    begun: list[object] = []

    async def counting_begin(*a, **kw):
        out = await real_begin(*a, **kw)
        begun.append(out[0])
        return out

    monkeypatch.setattr(ask_orchestrator, "begin", counting_begin)
    tid = str(uuid.uuid4())
    results: list[object] = []

    def post() -> None:
        try:
            results.append(app.ask(cid, "Note this", turn_id=tid).status_code)
        except Exception as e:  # noqa: BLE001 — reported by the assertion below
            results.append(e)

    first = threading.Thread(target=post)
    first.start()
    assert entered.wait(15)
    assert r.call.typed_turns == 1
    # HQ asks "Are you still there?" while that typed turn is still running …
    r.call.still_there = {"say_id": "s", "sent_at": 1.0, "said_at": None, "played_at": None,
                          "speech_s": 1.0}  # fmt: skip
    r.call.replying_still_there = True
    clock_before = r.call.last_speech
    # … and the client retries the same request: the SAME running task comes back.
    retry = threading.Thread(target=post)
    retry.start()
    for _ in range(100):
        if len(begun) == 2:
            break
        retry.join(0.05)
    assert len(begun) == 2 and begun[0] is begun[1] is not None
    assert r.call.typed_turns == 1
    assert r.call.still_there is not None and r.call.replying_still_there
    assert r.call.last_speech == clock_before
    release.set()
    first.join(15)
    retry.join(15)
    # The first request settles normally. (The retry, on TestClient's second thread, awaits a task
    # owned by the first thread's event loop — a harness artifact a real server never has; what
    # matters, the call state after its `begin`, is asserted above.)
    assert 200 in results, results
