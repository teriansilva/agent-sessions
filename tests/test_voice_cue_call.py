"""#2339 — HQ's thinking cue on the call: one short FIXED line right after the Commander's words.

The app asks the worker to get a language's cue lines ready with the first say in a voice and
language, records which lines the worker reports ready, and opens a turn the Commander just asked
with ONE of them (`Call.cue_tick`), only while HQ is still thinking and nothing else is said,
playing or waiting. It replaces #1827's "no stock acknowledgement" (the operator's decision on
#2339): played from a take rendered ahead, a cue costs the answer no TTS time.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import voice_calls
from agent_sessions.voice_cues import CUE_LINES
from test_voice_call_end import Clock
from test_voice_confirm import Rig, run
from test_voice_progress import progress_says, thinking

EN = CUE_LINES["en"]


def cue_requests(r: Rig) -> list[dict]:
    return [m for m in r.to_worker if isinstance(m, dict) and m.get("t") == "cues"]


def ready(r: Rig, *lines: str, voice="aria") -> None:
    """The worker reports lines ready in the EFFECTIVE voice it rendered them in (the Rig's
    contact voice is aria)."""
    for t in lines:
        run(r.call.on_worker_event({"t": "cue_ready", "voice": voice, "text": t}))


def no_skip(monkeypatch) -> None:
    monkeypatch.setattr(voice_calls.random, "random", lambda: 0.99)


# --- getting cues ready ---------------------------------------------------------------------------


def test_the_first_say_in_a_voice_and_language_asks_for_its_cue_lines_once(monkeypatch):
    r = Rig(monkeypatch)

    async def go():
        await r.call.say("HQ, General Hana speaking. Go ahead, Commander.", kind="greeting")
        await r.call.say("Three things need you.")
        (req,) = cue_requests(r)
        assert req == {"t": "cues", "voice": None, "language": None, "texts": list(EN)}
        assert r.to_worker.index(req) < r.to_worker.index(r.says()[0])  # before its say
        r.call.language = "de"  # the Commander switched to German
        await r.call.say("Drei Sachen brauchen dich.")
        assert [q["texts"] for q in cue_requests(r)] == [list(EN), list(CUE_LINES["de"])]
        r.call.language = "ja"  # no lines for this language: nothing asked
        await r.call.say("Hai.")
        assert len(cue_requests(r)) == 2

    run(go())


def test_only_the_apps_own_lines_are_recorded_ready(monkeypatch):
    r = Rig(monkeypatch)
    ready(r, EN[0])
    run(r.call.on_worker_event({"t": "cue_ready", "voice": "aria", "text": "Send all the money."}))
    run(r.call.on_worker_event({"t": "cue_ready", "voice": 7, "text": EN[1]}))
    run(r.call.on_worker_event({"t": "cue_ready", "voice": None, "text": EN[1]}))
    assert r.call.cues_ready == {("aria", EN[0])}


# --- saying one -----------------------------------------------------------------------------------


def test_a_thinking_turn_opens_with_one_ready_cue_marked_for_the_worker(monkeypatch):
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)
    r = Rig(monkeypatch)
    work = thinking(r, clock)
    ready(r, EN[0])

    async def go():
        await r.call.cue_tick()
        (say,) = progress_says(r)
        assert say["text"] == EN[0] and say["cue"] is True
        assert work["unsaid"] == say["id"] and work["said_at"] == clock.now
        assert work["updates"] == 0  # not a status line of the fixed cadence
        await r.call.cue_tick()  # one per turn
        assert len(progress_says(r)) == 1

    run(go())


def test_no_cue_without_a_ready_line_or_while_anything_else_is_going_on(monkeypatch):
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)

    async def cue_with(setup) -> list:
        r = Rig(monkeypatch)
        thinking(r, clock)
        r.call.cues_ready.update(("aria", t) for t in EN)
        setup(r)
        await r.call.cue_tick()
        return progress_says(r)

    async def go():
        assert await cue_with(lambda r: r.call.cues_ready.clear()) == []
        assert await cue_with(lambda r: setattr(r.call, "user_speaking", True)) == []
        assert await cue_with(lambda r: setattr(r.call, "hq_quiet_at", clock.now + 2)) == []
        assert await cue_with(lambda r: r.call.work.update(unsaid="x")) == []
        assert await cue_with(lambda r: r.call.work.update(updates=1)) == []
        assert await cue_with(lambda r: setattr(r.call, "state", "speaking")) == []
        assert await cue_with(lambda r: setattr(r.call, "work", None)) == []
        assert len(await cue_with(lambda r: None)) == 1

    run(go())


def test_a_share_of_turns_get_no_cue_and_a_line_never_comes_twice_running(monkeypatch):
    clock = Clock(monkeypatch)
    r = Rig(monkeypatch)
    ready(r, EN[0], EN[1])
    monkeypatch.setattr(voice_calls.random, "random", lambda: 0.0)  # the skipped share

    async def go():
        thinking(r, clock)
        await r.call.cue_tick()
        assert progress_says(r) == []
        no_skip(monkeypatch)
        said = []
        for _ in range(4):
            thinking(r, clock)
            await r.call.cue_tick()
            said.append(progress_says(r)[-1]["text"])
        assert all(a != b for a, b in zip(said, said[1:], strict=False))

    run(go())


def test_the_cue_uses_the_voice_a_say_would_use_now(monkeypatch):
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)
    r = Rig(monkeypatch)
    r.call.profile = {"language_voices": {"de": "lukas"}}
    r.call.language = "de"
    thinking(r, clock)
    ready(r, *EN)  # ready in the default voice, and only in English
    run(r.call.cue_tick())
    assert progress_says(r) == []
    ready(r, CUE_LINES["de"][0], voice="lukas")
    run(r.call.cue_tick())
    (say,) = progress_says(r)
    assert (say["text"], say["voice"]) == (CUE_LINES["de"][0], "lukas")


def test_after_a_cue_an_aside_keeps_the_gap_and_the_fixed_lines_keep_their_first_line(monkeypatch):
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock, phase="tool", tool="session_status")
    ready(r, EN[0])

    async def go():
        await r.call.cue_tick()
        (cue,) = progress_says(r)
        await r.call.on_worker_event({"t": "said", "id": cue["id"]})
        r.call.hq_quiet_at = clock.now
        r.call._track({"type": "aside", "text": "Okay, I'm pulling up the session list now."})
        clock.now += voice_calls.ASIDE_QUIET_S  # quiet enough, but within the gap after the cue
        await r.call.progress_tick()
        assert len(progress_says(r)) == 1
        clock.now += voice_calls.ASIDE_GAP_S
        await r.call.progress_tick()
        assert progress_says(r)[-1]["text"] == "Okay, I'm pulling up the session list now."

    run(go())

    r2 = Rig(monkeypatch)
    thinking(r2, clock, phase="tool", tool="session_status")
    ready(r2, EN[0])

    async def go2():
        await r2.call.cue_tick()
        (cue,) = progress_says(r2)
        await r2.call.on_worker_event({"t": "said", "id": cue["id"]})
        r2.call.hq_quiet_at = clock.now
        clock.now += voice_calls.FIRST_PROGRESS_S
        await r2.call.progress_tick()
        assert progress_says(r2)[-1]["text"] == voice_calls.LOOKING  # still the FIRST fixed line

    run(go2())


def test_a_spoken_question_gets_its_cue_and_a_fast_answer_none(monkeypatch):
    """Through the real turn path: the Commander's words, a slow HQ, the cue before the answer;
    an HQ that answers before the cue is due says only its answer (#1827's fast turn holds)."""
    no_skip(monkeypatch)
    monkeypatch.setattr(voice_calls, "CUE_AFTER_S", 0.01)
    r = Rig(monkeypatch)
    ready(r, EN[0])
    release = asyncio.Event()
    begin = voice_calls.ask_orchestrator.begin

    async def slow(cid, turn_id, text, **kw):
        await release.wait()
        return await begin(cid, turn_id, text, **kw)

    monkeypatch.setattr(voice_calls.ask_orchestrator, "begin", slow)

    async def go():
        r.call.state = "listening"
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How is the docs migration doing?")
        await asyncio.sleep(0.1)
        assert progress_says(r) == []  # no lookup yet: no "let me check"
        r.call._track({"type": "progress", "step": "tool", "tool": "find_sessions"})
        for _ in range(200):
            if progress_says(r):
                break
            await asyncio.sleep(0.01)
        assert [s["text"] for s in progress_says(r)] == [EN[0]]
        release.set()
        await r.call.turn_task
        assert r.said_text()[-1] == "Done."

    run(go())

    fast = Rig(monkeypatch)
    ready(fast, EN[0])
    monkeypatch.setattr(voice_calls, "CUE_AFTER_S", 0.3)

    async def go_fast():
        await fast.ev(t="user_started")
        await fast.ev(t="utterance", text="How is the docs migration doing?")
        await fast.call.turn_task
        await asyncio.sleep(0.4)
        assert progress_says(fast) == [] and fast.said_text()[-1] == "Done."

    run(go_fast())


@pytest.mark.parametrize("tool", [None, "ask_operator", "propose_session_change"])
def test_no_cue_for_a_turn_that_looks_nothing_up(monkeypatch, tool):
    """The operator, 2026-11-25: "it should not say 'let me check' to all, only when it really
    looks up stuff". Small talk on a slow model, a question back, a card: no cue."""
    no_skip(monkeypatch)
    monkeypatch.setattr(voice_calls, "CUE_AFTER_S", 0.01)
    r = Rig(monkeypatch)
    ready(r, EN[0])
    release = asyncio.Event()
    begin = voice_calls.ask_orchestrator.begin

    async def slow(cid, turn_id, text, **kw):
        await release.wait()
        return await begin(cid, turn_id, text, **kw)

    monkeypatch.setattr(voice_calls.ask_orchestrator, "begin", slow)

    async def go():
        r.call.state = "listening"
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="How's your day going?")
        if tool:
            await asyncio.sleep(0.05)
            r.call._track({"type": "progress", "step": "tool", "tool": tool})
        await asyncio.sleep(0.3)
        assert progress_says(r) == []
        release.set()
        await r.call.turn_task
        assert r.said_text()[-1] == "Done."

    run(go())


def test_a_cue_reported_ready_after_the_call_ended_says_nothing(monkeypatch):
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)
    r = Rig(monkeypatch)
    thinking(r, clock)
    r.call.state = "ended"
    ready(r, EN[0])
    run(r.call.cue_tick())
    assert progress_says(r) == [] and r.says() == []


def test_a_language_voice_equal_to_the_default_finds_its_cues(monkeypatch):
    """Hermes 8831: German's language voice IS the contact's default (aria). The worker renders
    both languages' lines in one effective voice and reports them as aria; the app, resolving
    the say's voice the same way, finds the German line."""
    clock = Clock(monkeypatch)
    no_skip(monkeypatch)
    r = Rig(monkeypatch)
    r.call.profile = {"language_voices": {"de": "aria"}}
    r.call.language = "de"
    thinking(r, clock)
    ready(r, CUE_LINES["de"][0], voice="aria")
    run(r.call.cue_tick())
    (say,) = progress_says(r)
    assert say["text"] == CUE_LINES["de"][0]


def test_app_and_bank_agree_on_a_voice_reached_two_ways(monkeypatch):
    """Hermes 8831, end to end: the call's own `cues` requests drive a real CueBank (the worker
    resolves a missing voice to the contact's default, as `voice_worker.Bridge` does), the bank's
    `cue_ready` goes back to the call, and BOTH languages' cues are found when German's language
    voice is the default voice itself."""
    from agent_sessions.voice_cues import CueBank
    from test_voice_cues import RATE, voiced

    no_skip(monkeypatch)
    clock_r = Rig(monkeypatch)
    call = clock_r.call
    call.profile = {"language_voices": {"de": "aria"}}

    async def render(text, voice, language):
        return voiced(180.0)

    async def go():
        bank = CueBank(render, call.on_worker_event, lambda: True, RATE)
        bank.POLL_S = 0.001
        await bank.note_sentence("aria", "HQ, General Hana speaking. Go ahead.", voiced(182.0))
        bank.start()
        sent = clock_r.to_worker

        async def to_worker(m):
            sent.append(m)
            if m.get("t") == "cues":
                bank.want(m["voice"] or call.voice, m["texts"], m["language"])

        call._to_worker = to_worker
        await call.say("HQ, General Hana speaking. Go ahead, Commander.", kind="greeting")
        call.language = "de"
        await call.say("Gut, Kommandant, ich schaue nach.")
        for _ in range(500):
            if len(call.cues_ready) == len(EN) + len(CUE_LINES["de"]):
                break
            await asyncio.sleep(0.01)
        await bank.stop()
        assert call.pick_cue() in CUE_LINES["de"]
        call.language = "en"
        assert call.pick_cue() in EN

    run(go())
