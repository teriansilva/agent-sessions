"""Calling HQ (#1397 P1): the call registry and the broker between the browser and the worker.

```
browser ──/ws/voice/{id}──▶ Call (this module, in the app) ──UDS ws──▶ voice worker (Pipecat)
   mic PCM16 16 kHz  ─────────────────────────────────────────────────▶  VAD + Smart Turn → STT
   speaker PCM16 24 kHz ◀─────────────────────────────────────────────  TTS ◀── "say"
           utterance ──▶ HQ turn (ask_orchestrator, via="call") ──▶ stored answer ──▶ say
```

The worker is a **speech terminal**: it hears (VAD, end-of-turn, STT) and speaks (TTS) and knows
nothing about HQ. Everything HQ does happens HERE, in the app, as an ordinary HQ turn with the
operator taken from the socket that opened the call — so a call reaches exactly what typing in HQ
reaches, through the same tools, limits and confirm routes (no new authority).

What HQ says is HQ's STORED text: the answer as `ask_store.append` kept it (redacted), plus fixed
sentences rendered here for cards and questions, and — while a slow turn runs — fixed status lines
built from the turn's own progress events (#1702). The model never speaks unstored prose, and raw
tool output never reaches the TTS endpoint.

Limits: one call per instance; bounded frames and queues. A call ends when the operator ends it
(#1668): the hang-up, or words HQ judges mean the call is over (`end_call`, after its farewell has
played). No idle or duration limit: after SILENCE_ASK_S without real speech HQ asks once, and only
no answer within NO_ANSWER_S of that question ends the call. On transport loss the call ends: mic
and playback stop on the client, nothing reconnects without a new tap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import ask_orchestrator, ask_store, hq_calls_settings, hq_contacts, voice_setup

log = logging.getLogger(__name__)

#: No automatic end (#1668): after this long without REAL speech (a heard utterance, or an HQ say
#: the browser played), HQ asks whether the operator is still there…
SILENCE_ASK_S = 30 * 60
#: …and only no answer within this long of that question having played ends the call.
NO_ANSWER_S = 60.0
#: A say whose `played` ack never comes (a backgrounded tab) counts as played this long after the
#: worker reported its audio sent (`said`), plus how long its words take to say: an
#: UNCONFIRMED-playback end, not proof it was heard.
PLAYBACK_FALLBACK_S = 10.0
#: How fast speech is assumed to go when estimating how long a say takes to play (deliberately
#: slow: a longer estimate only ends an unacked call later).
SPEECH_CHARS_PER_S = 10.0
STILL_THERE_LINE = "Are you still there, Commander?"


def speech_seconds(text: str) -> float:
    """About how long `text` takes to say (at `SPEECH_CHARS_PER_S`): the no-ack fallback waits
    for it to have played."""
    return len(text) / SPEECH_CHARS_PER_S


def asks_something(text: str) -> bool:
    """Whether HQ's answer asks the operator something (any script's question mark): an answer
    that asks is never a farewell, whatever was judged (#1668)."""
    return any(q in text for q in "?？؟")


#: How many recent worker turns' starts a call remembers (`turn_starts`).
TURN_STARTS_KEPT = 32
#: How many recent say ids a call remembers to tell its own `played` acks from stale ones.
SAY_IDS_KEPT = 64
#: A client audio frame: 20–100 ms of 16 kHz PCM16 is 640–3200 bytes; anything past this is refused.
MAX_AUDIO_FRAME = 64 * 1024
MAX_CONTROL_FRAME = 8 * 1024
#: Ingress budget per second of wall time (16 kHz PCM16 = 32 KB/s; generous headroom).
MAX_INGRESS_BPS = 128 * 1024
IN_RATE = 16_000
OUT_RATE = 24_000
WORKER_START_S = 30.0
#: A call that no socket of its session joins within this ends ("not_connected").
DIAL_TIMEOUT_S = 30.0
#: A spoken confirmation's window, from the moment its read-back finished playing (#1397 P2).
CHALLENGE_S = 20.0
#: How long a 202 (a session starting, an operation running) is followed to its end state.
OUTCOME_POLL_S = 30.0
OUTCOME_EVERY_S = 1.5
#: "What needs me?" — answered from the server's Needs-you list, not by a model.
NEEDS_YOU_ASK = re.compile(
    r"\b(what|who|anything)\b.*\bneeds?\b.*\b(me|you|my attention|attention)\b"
)
LANG = re.compile(r"^[a-z]{2}\Z")  # `$` would also match "de\n"
#: Say something if HQ has not answered by then, so silence never reads as a dropped call.
ACK_AFTER_S = 1.2

#: Fixed acknowledgements while HQ works. Not model output: picked in rotation.
ACKS = (
    "One moment, Commander.",
    "Checking now, Commander.",
    "Stand by, Commander.",
)
#: What each HQ tool is doing, as a fixed phrase (#1702): keyed by the tool's NAME only — never
#: its arguments or result. Every HQ tool but `end_call` has one (a test ratchets it).
TOOL_ACTIVITY = {
    "find_sessions": "looking through your sessions",
    "list_sessions": "looking through your sessions",
    "session_status": "checking that session",
    "session_brief": "checking that session",
    "list_missions": "checking your missions",
    "mission_status": "checking that mission",
    "fleet_status": "checking the fleet",
    "agent_usage": "checking your usage",
    "list_projects": "reading your projects",
    "list_agents": "reading your agents",
    "list_folders": "reading your folders",
    "list_playbooks": "reading your playbooks",
    "playbook_detail": "reading that playbook",
    "playbook_schema": "reading the playbook format",
    "list_skills": "reading your skills",
    "skill_detail": "reading that skill",
    "list_automations": "reading your automations",
    "automation_detail": "reading that automation",
    "read_settings": "reading your settings",
    "ask_operator": "preparing a question for you",
    "propose_nudge": "preparing a card for you",
    "propose_session": "preparing a card for you",
    "propose_session_change": "preparing a card for you",
    "propose_project_change": "preparing a card for you",
    "propose_library": "preparing a card for you",
}
#: Said once per turn as a tool starts (the lookups worth naming at once).
PROGRESS_LINES = {
    name: TOOL_ACTIVITY[name].capitalize() + "."
    for name in (
        "find_sessions",
        "list_sessions",
        "session_status",
        "session_brief",
        "list_missions",
        "mission_status",
        "fleet_status",
        "agent_usage",
    )
}
#: While an HQ turn runs on a call, HQ never goes quiet for long (#1702): a fixed status line once
#: HQ has been quiet this long (counted from when its last line had time to play)…
FIRST_PROGRESS_S = 12.0
#: …then after every this much quiet…
PROGRESS_EVERY_S = 25.0
#: …and, once the model has kept HQ waiting this long, a stall line every STALL_EVERY_S.
STALL_S = 90.0
STALL_EVERY_S = 45.0
PROGRESS_TICK_S = 1.0
#: `pulse_chat`'s sub-steps (the find pipeline, also the no-tools fallback): looking through
#: sessions.
FIND_STEPS = frozenset({"catalog", "content"})


def spoken_duration(seconds: float) -> str:
    """An elapsed time as said on a call: "15 seconds" (to the nearest 5) under a minute, then
    "over 2 minutes"."""
    if seconds < 60:
        return f"{max(5, min(55, int(5 * round(seconds / 5))))} seconds"
    minutes = int(seconds // 60)
    return f"over {minutes} minute{'s' if minutes != 1 else ''}"


def progress_line(work: dict, now: float) -> str:
    """The fixed status line for an HQ turn still in flight (#1702), from its real state only:
    which tool step runs (a fixed phrase by tool NAME), how many steps have returned, and how long
    the current wait (this tool, or this model request) has lasted. Never model text, tool
    arguments or tool output."""
    phase = work.get("phase")
    waited = now - work.get("phase_since", now)
    if phase == "tool":
        activity = TOOL_ACTIVITY.get(str(work.get("tool") or ""), "working on that step")
        return f"Still {activity}, {spoken_duration(waited)} so far."
    if phase == "model":
        if waited >= STALL_S:
            return (
                "The model hasn't answered yet, Commander. "
                f"{spoken_duration(waited).capitalize()} so far, still waiting."
            )
        steps = int(work.get("done") or 0)
        if steps:
            return (
                f"{steps} step{'s' if steps != 1 else ''} done, "
                f"waiting on the model, {spoken_duration(waited)} so far."
            )
        return f"Still waiting on the model, {spoken_duration(waited)} so far."
    return f"Still working on it, {spoken_duration(now - work.get('since', now))} so far."


#: How long ending a call waits on each socket send before giving up on it.
SEND_BUDGET_S = 2.0
#: How long the "can the worker's interpreter import Pipecat?" probe may take.
RUNTIME_PROBE_S = 30.0


class CallError(Exception):
    def __init__(self, status: int, detail: str, code: str = "refused"):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


# Spoken-text helpers live in `voice_text` (no app imports: the worker uses them too).
from .voice_text import (  # noqa: E402,F401
    CHUNK_WORDS,
    card_sentences,
    classify_confirmation,
    classify_decision,
    is_no_speech_phrase,
    is_skip,
    pick_option,
    readback,
    readback_screen_only,
    speakable,
    split_speech,
    verbatim,
)

# --- the call ----------------------------------------------------------------------------------


@dataclass
class Call:
    id: str
    cid: str
    contact: str
    voice: str
    operator: object
    app: object
    registry: object
    #: The CSRF token of the session that admitted the call. The call's socket must present it
    #: (and the same session's cookie): a call's audio belongs to the session that started it.
    session_csrf: str = ""
    dial_timer: asyncio.Task | None = None
    started: float = field(default_factory=time.monotonic)
    #: The last sign the operator is there (#1668): an utterance heard, a typed turn on the call's
    #: conversation, or an HQ say the browser played. Voice activity alone (`user_started`, which
    #: room noise can fire), worker housekeeping, progress, status lines (#1702) and stale acks
    #: are not.
    last_speech: float = field(default_factory=time.monotonic)
    #: Every time the operator starts speaking (`user_started`): a farewell planned for an
    #: utterance is dropped if they began speaking again before it was installed (#1668).
    user_starts: int = 0
    #: Every ACCEPTED typed turn on the call's conversation: a farewell planned for an earlier
    #: utterance is off if the Commander typed after it (#1668).
    typed_turns: int = 0
    #: The typed turns' tasks already counted: a client retrying a turn that is still running gets
    #: the same task back, and that is no new turn (Hermes 7012).
    typed_tasks: weakref.WeakSet = field(default_factory=weakref.WeakSet, repr=False)
    #: Every say this call sent (bounded): only their `played` acks count as HQ having spoken.
    say_ids: list = field(default_factory=list)
    #: HQ judged the call over (`end_call`): ``{"say_id", "gen", "heard", "said_at", "speech_s"}``
    #: of its farewell. `heard` is what the bye's words were heard after; the call
    #: ends only while it still holds (`_still_heard`).
    ending: dict | None = None
    #: The still-there question in flight: ``{"say_id", "sent_at", "said_at", "played_at",
    #: "speech_s"}``.
    still_there: dict | None = None
    #: HQ's last question was the still-there one: the next utterance answers it. Kept apart
    #: from `still_there`, which any sign of the operator (their voice) already clears.
    replying_still_there: bool = False
    #: …and `user_starts` when it was asked: only words BEGUN after it answer it (Hermes 7008).
    still_there_since: int = 0
    state: str = "dialing"  # dialing | listening | thinking | speaking | ended
    ended_reason: str = ""
    muted: bool = False
    #: The latest utterance heard while HQ was still answering the previous one, with what it was
    #: heard after (``(text, heard)``, see `on_utterance`).
    queued: tuple | None = None
    #: Bumped by every new utterance: an answer from an older generation is not spoken.
    generation: int = 0
    turn_task: asyncio.Task | None = None
    #: The `ask_operator` question the Commander can answer by voice: one HQ asked on this call, or
    #: one a typed turn on its conversation put on screen (`on_typed_settled`)…
    ask: dict | None = None
    #: …and `user_starts` when it was presented: only words the Commander BEGAN after that answer
    #: it (#1668 review: words begun while HQ was still answering are a new request).
    ask_since: int = 0
    #: `user_starts` at each worker turn's start (bounded): what an utterance of that turn was begun
    #: after, whenever its transcript lands.
    turn_starts: dict = field(default_factory=dict, repr=False)
    #: The pending spoken confirmation (#1397 P2): `{cards, say_id, armed_at, expires_at}`. Server
    #: state only; armed when its read-back has played; one use; void on anything else.
    challenge: dict | None = None
    #: Settlements and Needs-you read-outs run here, never in the worker relay loop (a confirm
    #: following its card for 30 s must not freeze the call's audio and events).
    side_tasks: set = field(default_factory=set, repr=False)
    #: Cards tapped on screen during this call: a voice settlement already under way skips them.
    tapped: set = field(default_factory=set, repr=False)
    #: The contact's resolved voice profile (#1397 P3): voice, style (only if advertised), speed.
    profile: dict | None = None
    #: The call's language (#1397 P3): the operator's fixed choice, else the last one detected.
    language: str | None = None
    fixed_language: str | None = None
    #: Does the STT endpoint report languages on this call (#1397 P3)? True: a confirmation must
    #: itself be heard as English. False (TESTED, it cannot): English is assumed. None (untested,
    #: or its endpoint changed since): unknown — spoken confirmation fails closed.
    detects_language: bool | None = None
    #: The shared `voice/call.lock` (#1397 P4) held for this call's lifetime.
    _held: contextlib.ExitStack | None = field(default=None, repr=False)
    #: Did the TTS server accept a `language` field (conformance `tts.language`)? Else none is sent.
    tts_takes_language: bool = False
    heard_language: str | None = None
    #: The settings this call was admitted with: the worker is configured from THESE, so the
    #: call's language gate and the worker's STT config can never come from two reads (#1635).
    settings: dict | None = field(default=None, repr=False)
    #: When the latest utterance STARTED (the worker's `user_started`): a confirmation must begin
    #: after its challenge armed, so HQ's own read-back echoing into the mic cannot confirm it.
    user_started_at: float = 0.0
    #: The worker's number for the latest user turn to START. A transcript arrives up to the STT
    #: grace after its turn ended — after a newer turn's `user_started` — so a confirmation is
    #: judged by ITS OWN turn number: only a turn that started after arming confirms.
    user_turn: int = 0
    _to_client: Callable[[dict | bytes], Awaitable[None]] | None = None
    _to_worker: Callable[[dict | bytes], Awaitable[None]] | None = None
    _ack_i: int = 0
    #: The HQ turn in flight on this call (#1702): ``{"since", "phase", "phase_since", "tool",
    #: "steps" (started), "done" (returned), "updates", "stalled", "unsaid" (the status line whose
    #: audio has not gone out yet)}`` from its own progress events; None between turns.
    work: dict | None = None
    #: When HQ's last line will have had time to play (sent + `speech_seconds`): the quiet the
    #: progress lines are counted from.
    hq_quiet_at: float = 0.0
    #: The Commander is mid-utterance (`user_started` … `user_stopped`): no status line over them.
    user_speaking: bool = False
    _ended: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set once nothing of this call runs any more (its worker stopped, or it never had one): only
    #: then may the registry admit the next call.
    torn_down: asyncio.Event = field(default_factory=asyncio.Event)
    worker_started: bool = False
    #: "brief" | "detailed" — Settings → HQ calls → answers (picks the registered call prompt).
    answers: str = "brief"

    def public(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.cid,
            "contact": self.contact,
            "state": self.state,
            "muted": self.muted,
            "elapsed_s": int(time.monotonic() - self.started),
            "ended_reason": self.ended_reason,
        }

    # -- wiring ---------------------------------------------------------------------------------

    async def client(self, msg: dict | bytes) -> None:
        if self._to_client is not None:
            with contextlib.suppress(Exception):
                await self._to_client(msg)

    async def worker(self, msg: dict | bytes) -> None:
        if self._to_worker is not None:
            with contextlib.suppress(Exception):
                await self._to_worker(msg)

    async def set_state(self, state: str) -> None:
        if self.state == "ended" or self.state == state:
            return
        self.state = state
        await self.client({"t": "state", "state": state})

    async def say(
        self,
        text: str,
        *,
        kind: str = "answer",
        turn_id: str | None = None,
        sid: str | None = None,
        heard: bool = True,
    ) -> str | None:
        """`heard=False`: its `played` ack is no sign of the operator (#1702 status lines — HQ
        talking while it waits proves nobody is listening)."""
        text = text.strip()
        if not text or self.state == "ended":
            return None
        sid = sid or uuid.uuid4().hex[:12]
        self.hq_quiet_at = max(self.hq_quiet_at, time.monotonic() + speech_seconds(text))
        # Known BEFORE anything is awaited (#1668, Hermes 6983): a short line can be played, and
        # acked, while the sends below are still in flight.
        if heard:
            self.say_ids.append(sid)
            del self.say_ids[:-SAY_IDS_KEPT]
        await self.client({"t": "caption", "who": "hq", "id": sid, "text": text, "kind": kind,
                           "turn_id": turn_id})  # fmt: skip
        lang = self.language if self.tts_takes_language else None
        # A voice per language (#1397 P3): the contact's voice for the call's language, if set.
        voice = (self.profile or {}).get("language_voices", {}).get(self.language or "")
        await self.worker(
            {"t": "say", "id": sid, "text": text, "language": lang, "voice": voice}
        )  # fmt: skip
        return sid

    # -- events from the worker -----------------------------------------------------------------

    async def on_worker_event(self, ev: dict) -> None:
        t = ev.get("t")
        if t == "ready":
            await self.set_state("listening")
            await self.client({"t": "connected"})
            await self.greet()
        elif t == "user_started":
            self.user_started_at = time.monotonic()
            self.user_starts += 1
            self.user_speaking = True
            self.ending = None  # speaking again after HQ's farewell: the call goes on (#1668)
            # NOT a sign of the operator for the silence clock (Hermes 7000): voice activity fires
            # on room noise too, and a forgotten tab must still be asked and hung up.
            n = _turn_number(ev.get("turn"))
            if n is not None and n > self.user_turn:
                self.user_turn = n
            if n is not None:
                self.turn_starts[n] = self.user_starts
                while len(self.turn_starts) > TURN_STARTS_KEPT:
                    self.turn_starts.pop(next(iter(self.turn_starts)))
            ch = self.challenge
            if ch is not None and ch.get("armed_at") is None:
                await self.void_challenge()  # talked over the read-back: never armed
            await self.client({"t": "user", "speaking": True})
        elif t == "said":
            # The worker has SENT this say's audio. Forwarded in order behind it, so the
            # browser's playback queue can ack when it has actually PLAYED it (`played`).
            ch = self.challenge
            if ch is not None and ev.get("id") == ch["say_id"]:
                ch["sent"] = True  # BEFORE forwarding: the browser's `played` may come back at once
            # The no-ack fallbacks (#1668) count from HERE, once its audio went out, not from when
            # it was handed to the worker: a long farewell, or one queued behind another say, is
            # never cut off while it is still playing.
            work = self.work
            if work is not None and ev.get("id") == work.get("unsaid"):
                work["unsaid"] = None  # its audio went out: the next status line may follow
            for pending in (self.ending, self.still_there):
                if pending is not None and ev.get("id") == pending["say_id"]:
                    pending["said_at"] = time.monotonic()
            await self.client({"t": "said", "id": ev.get("id")})
        elif t == "user_stopped":
            self.user_speaking = False
            await self.client({"t": "user", "speaking": False})
        elif t == "interrupt":
            # The browser flushes what it had queued: those says never play, so their late or
            # duplicate acks are not speech (#1668).
            self.say_ids.clear()
            if self.work is not None:
                self.work["unsaid"] = None  # flushed: it will never be said
            # Barge-in: the client drops what it has queued, at once — and a challenge whose
            # read-back was talked over is void. Pipecat sends this at EVERY user turn start, a
            # confirmation's too: an ARMED challenge's read-back has played out, so it stays.
            ch = self.challenge
            if ch is not None and ch.get("armed_at") is None:
                await self.void_challenge()
            await self.client({"t": "interrupt"})
            if self.state == "speaking":
                # Back to what HQ is doing: a turn still running is still `thinking`, so its status
                # lines (#1702) carry on after a barge-in over the ack or a status line.
                await self.set_state("thinking" if self._turn_running() else "listening")
        elif t == "bot_started":
            self.hq_quiet_at = max(self.hq_quiet_at, time.monotonic())
            await self.set_state("speaking")
            await self.client({"t": "speaking", "id": ev.get("id"), "on": True})
        elif t == "bot_stopped":
            # HQ's audio really stopped NOW (a say queued behind TTS plays later than its send
            # suggested): status lines count their quiet from here at the earliest (#1702).
            self.hq_quiet_at = max(self.hq_quiet_at, time.monotonic())
            await self.client({"t": "speaking", "id": ev.get("id"), "on": False})
            if self.state == "speaking":
                await self.set_state("thinking" if self._turn_running() else "listening")
        elif t == "utterance" and is_no_speech_phrase(str(ev.get("text") or "")):
            # Room noise Whisper turned into a stock phrase (#1667): dropped BEFORE anything else
            # — no HQ turn, no language change, an armed confirmation keeps waiting.
            log.debug("voice call %s: dropped a no-speech phrase", self.id)
        elif t == "utterance":
            self.user_speaking = False
            heard = ev.get("heard_language")
            self.heard_language = heard if isinstance(heard, str) and LANG.match(heard) else None
            lang = ev.get("language")
            if self.fixed_language is None and isinstance(lang, str) and LANG.match(lang):
                self.language = lang
            text = str(ev.get("text") or "").strip()[: ask_orchestrator.QUERY_MAX]
            if text:
                # `late`: its turn already went out; it began before anything now armed.
                await self.on_utterance(
                    text, late=bool(ev.get("late")), turn=_turn_number(ev.get("turn"))
                )
        elif t == "error":
            await self.end(
                f"{ev.get('endpoint') or 'speech'}_failed",
                detail=str(ev.get("detail") or "the speech endpoint stopped answering")[:200],
                endpoint=str(ev.get("endpoint") or ""),
            )

    def _turn_running(self) -> bool:
        return self.turn_task is not None and not self.turn_task.done()

    async def greet(self) -> None:
        name = ""
        with contextlib.suppress(KeyError):
            name = hq_contacts.get(self.contact).name.split()[-1]
        who = f"General {name} " if name else ""
        await self.say(f"HQ, {who}speaking. Go ahead, Commander.", kind="greeting")

    async def on_played(self, say_id: object) -> None:
        """The browser played a say out to its end (its playback queue reached the `said` marker).
        The ONLY thing that arms a challenge: an interrupted, flushed, muted or suspended playback
        never acks, so it never arms (#1397 P2)."""
        if say_id in self.say_ids and self.state != "ended":
            self.say_ids.remove(say_id)  # heard ONCE: a duplicate or old ack is not new speech
            now = time.monotonic()
            self.last_speech = now  # HQ was heard: real speech (#1668)
            st = self.still_there
            if st is not None and st["say_id"] == say_id and st["played_at"] is None:
                st["played_at"] = now
            end = self.ending
            if end is not None and end["say_id"] == say_id and self._ending_holds(end):
                await self.end("hq_ended")
                return
        ch = self.challenge
        if ch is None or ch.get("armed_at") is not None or not ch.get("sent"):
            return
        if say_id != ch["say_id"] or self.muted or self.state == "ended":
            return
        if ch["gen"] != self.generation:
            return  # installed for a question something newer has superseded
        await self._arm(ch)

    async def _arm(self, ch: dict) -> None:
        now = time.monotonic()
        ch["armed_at"] = now
        ch["armed_turn"] = self.user_turn  # every turn up to this one began before arming
        ch["expires_at"] = now + CHALLENGE_S
        await self.client(
            {"t": "challenge", "cards": [c["id"] for c in ch["cards"]], "expires_s": CHALLENGE_S}
        )

    async def on_card_decided(self, card_id: object) -> None:
        """A tap decided a card: a spoken challenge naming it is now inert."""
        if isinstance(card_id, str):
            self.tapped.add(card_id)
        ch = self.challenge
        if ch is not None and any(c["id"] == card_id for c in ch["cards"]):
            await self.void_challenge()

    async def void_challenge(self) -> None:
        if self.challenge is not None:
            self.challenge = None
            await self.client({"t": "challenge", "cards": []})

    async def on_needs_you_request(self) -> None:
        """The call panel's "What needs me?" chip: the same path as saying it — answered from the
        server's Needs-you list, never a model turn, and never over a running turn."""
        if self.state == "ended" or self._turn_running():
            return
        # A new question: its generation is taken BEFORE anything is awaited, so an older offer
        # still in flight sees it is stale and never installs its challenge (#1631 review 6874).
        self.generation += 1
        gen = self.generation
        await self.void_challenge()
        self._side(self.read_needs_you(gen))

    def _side(self, coro) -> None:
        """Run `coro` beside the relay loop; ended with the call."""
        task = asyncio.create_task(coro)
        self.side_tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self.side_tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("voice call side task failed", exc_info=t.exception())

        task.add_done_callback(done)

    async def on_utterance(self, text: str, *, late: bool = False, turn: int | None = None) -> None:
        if self.muted:
            return
        # Answering HQ's still-there question (#1668): HQ is told what the words answer, and an
        # older HQ question is no longer what they reply to.
        began = None if late or turn is None else self.turn_starts.get(turn)
        replying_to = (
            STILL_THERE_LINE
            if self.replying_still_there and began is not None and began > self.still_there_since
            else None
        )
        self.touch()
        if await self._answer_challenge(text, late=late, turn=turn):
            return
        # A new question. Its generation is taken BEFORE anything is awaited — the void below
        # included — so an older offer still in flight sees it is stale and never installs its
        # challenge (#1631 review 6874).
        self.generation += 1
        gen = self.generation
        await self.void_challenge()
        await self.client({"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text})
        if NEEDS_YOU_ASK.search(text.lower()) and not self._turn_running():
            self._side(self.read_needs_you(gen))
            return
        # What these words were heard after (#1668): how often the Commander had started speaking
        # and typed, the worker's turn they belong to (none for a LATE transcript, whose turn is
        # gone), when that turn BEGAN, and the HQ line they answer.
        heard = {
            "starts": self.user_starts,
            "typed": self.typed_turns,
            "turn": None if late else turn,
            "began": began,
            "replying_to": replying_to,
        }
        if self._turn_running():
            # HQ is still answering the previous question: keep only the newest one and ask it
            # when the running turn settles; that turn's answer is no longer spoken.
            self.queued = (text, heard)
            return
        self.turn_task = asyncio.create_task(self._ask(text, gen, heard))

    def on_typed_turn(self, task: object = None) -> bool:
        """The Commander typed a turn on this call's conversation and it was ACCEPTED (#1668): they
        are there, and a farewell HQ planned is off — they have more to say. Once per turn `task`:
        a retry of one still running is no new turn. Whether this was the first time."""
        if task is not None:
            if task in self.typed_tasks:
                return False
            self.typed_tasks.add(task)
        self.typed_turns += 1
        self.touch()
        self.ending = None
        self.ask = None  # a newer turn: until it settles, no older question is what a reply answers
        return True

    def on_typed_settled(self, view: object) -> None:
        """A typed turn on this call's conversation settled (#1668): if it ended on an
        `ask_operator` question, that is the one a spoken reply begun from now on answers."""
        if self.state == "ended" or not isinstance(view, dict) or view.get("status") != "done":
            return
        ask = view.get("ask")
        if isinstance(ask, dict) and ask.get("question_id"):
            self.ask, self.ask_since = ask, self.user_starts
        else:
            self.ask = None  # it settled without a question: no older one is still waiting

    def touch(self) -> None:
        """The operator is there (#1668): their words, or a typed turn. Restarts the silence clock
        and answers any still-there question — a typed answer too, so the next spoken words are
        not taken as its reply (Hermes 7002)."""
        self.last_speech = time.monotonic()
        self.still_there = None
        self.replying_still_there = False

    def _still_heard(self, heard: dict) -> bool:
        """Nothing came after the words `heard` describes (#1668): the Commander has not started
        speaking again (nor did a newer worker turn begin before they landed) and typed nothing.
        Checked when a farewell is planned AND when it would end the call, whatever the order."""
        turn = heard.get("turn")
        return (
            turn is not None  # a late transcript's turn is gone: it never ends the call
            and self.user_starts == heard["starts"]
            and self.typed_turns == heard["typed"]
            and self.user_turn <= turn
        )

    def _ending_holds(self, end: dict) -> bool:
        return end["gen"] == self.generation and self._still_heard(end["heard"])

    async def _ask(self, text: str, gen: int, heard: dict | None = None) -> None:
        """`heard`: what these words were heard after (`on_utterance`)."""
        if heard is None:
            heard = {"starts": self.user_starts, "typed": self.typed_turns, "turn": None,
                     "began": None, "replying_to": None}  # fmt: skip
        replying_to = heard["replying_to"]
        await self.set_state("thinking")
        option = None
        answers = None
        # HQ's open question, if these words were BEGUN after it was presented (#1668): words begun
        # while HQ was still answering, or a late transcript, are a new request. Never for a reply
        # to the still-there question.
        ask = None
        began = heard.get("began")
        if self.ask and replying_to is None and began is not None and began > self.ask_since:
            ask, self.ask = self.ask, None
        if ask:
            idx = pick_option(text, ask.get("options") or [])
            if idx is not None:
                option, answers, text = idx, ask["question_id"], ""
            elif ask.get("allow_text"):
                answers = ask["question_id"]
        turn_id = str(uuid.uuid4())
        now = time.monotonic()
        self.work = {"since": now, "phase": None, "phase_since": now, "tool": None, "steps": 0,
                     "done": 0, "updates": 0}  # fmt: skip
        ack_task = asyncio.create_task(self._report_progress(gen))
        try:
            view = await self._run_turn(turn_id, text, answers, option, replying_to=replying_to)
        except ask_orchestrator.TurnError as e:
            view = {"status": "failed", "detail": e.detail}
        except ask_store.StoreRefused:
            view = {"status": "failed", "detail": "HQ could not store this question"}
        except Exception:  # noqa: BLE001 — never a call stuck in "thinking" with a lost queue
            log.warning("voice call turn failed", exc_info=True)
            view = {"status": "failed", "detail": ""}
        finally:
            ack_task.cancel()
            self.work = None
        if self.state == "ended":
            return
        await self.client({"t": "turn", "turn": view})
        # New Library drafts are offered with a spoken summary of their BOUND draft (#1681): read
        # before the generation check below, so a question asked meanwhile still wins.
        summaries = (
            await draft_summaries(view)
            if gen == self.generation and view.get("status") == "done"
            else {}
        )
        if gen != self.generation or self.state == "ended":
            pass  # a newer question superseded this one: its answer is on screen, not spoken
        elif view.get("status") == "done":
            ask = view.get("ask")
            if isinstance(ask, dict) and ask.get("question_id"):
                self.ask, self.ask_since = ask, self.user_starts  # presented now
            else:
                # A newer answer: an older question is no longer what a reply answers.
                self.ask = None
            words = speakable(view.get("answer") or "")
            # offered by voice; any more are screen-only
            cards = confirmable_cards(view, summaries=summaries)[:4]
            lines = list(card_sentences(view, skip={c["id"] for c in cards}))
            sid = uuid.uuid4().hex[:12]
            # HQ judged the call over (#1668): it ends once THIS farewell has played — never over
            # a card that still needs the operator, an empty answer, an answer that ASKS
            # something, or once the operator started speaking again after these words (their
            # next words may still be on the way). Installed BEFORE the farewell is sent: its ack
            # can come back while the send is still in flight.
            if (
                view.get("end_call") is True
                and words.strip()
                and not cards
                and not lines
                and not asks_something(words)
                and self._still_heard(heard)
            ):
                self.ending = {
                    "say_id": sid,
                    "gen": gen,
                    "heard": heard,
                    "said_at": None,
                    "speech_s": speech_seconds(words),
                }
            if await self.say(words, turn_id=view.get("turn_id"), sid=sid) is None:
                self.ending = None
            for line in lines:
                await self.say(line, turn_id=view.get("turn_id"))
            if cards:
                await self.offer(cards, turn_id=view.get("turn_id"), gen=gen)
        else:
            await self.say(
                "Sorry, Commander, I couldn't answer that. "
                + (str(view.get("detail") or "")[:160]),
                kind="error",
            )
        nxt, self.queued = self.queued, None
        if nxt and self.state != "ended":
            text, heard = nxt
            self.turn_task = asyncio.create_task(self._ask(text, self.generation, heard))
        elif self.state == "thinking":
            await self.set_state("listening")

    # -- spoken confirmation (#1397 P2) -------------------------------------------------------

    async def offer(
        self,
        cards: list[dict],
        *,
        turn_id: str | None = None,
        gen: int | None = None,
        queue: list[dict] | None = None,
    ) -> None:
        """Read the cards back; the challenge arms once that read-back has played. `gen`: the
        question this offer answers — checked with no await between it and installing the
        challenge, which keeps that generation: a slower, older offer can never displace a newer
        one, and a challenge outlived by its generation never arms or confirms (#1631 reviews).

        The confirmation matcher is English-only (#1397 P3): on a call in any other language the
        cards are read back for the SCREEN and no challenge is armed, so a translated or
        misdetected command can never settle one."""
        await self.void_challenge()
        cards = cards[:4]
        if gen is not None and gen != self.generation:
            return
        if self.language not in (None, "en"):
            await self.say(readback_screen_only(cards), kind="readback", turn_id=turn_id)
            return
        # The PENDING challenge exists before its read-back is sent: the worker's `said`, the
        # browser's `played` — and an interruption, mute, tap or new question, which void it —
        # all meet the real challenge, whatever order the awaits resolve in (#1631 reviews).
        sid = uuid.uuid4().hex[:12]
        ch = {"cards": cards, "say_id": sid, "sent": False, "armed_at": None,
              "expires_at": None, "gen": self.generation,
              # The Needs-you items still to offer, one at a time, after this one (#1681 P2).
              "queue": list(queue or [])}  # fmt: skip
        self.challenge = ch
        said = await self.say(readback(cards), kind="readback", turn_id=turn_id, sid=sid)
        if self.challenge is not ch:
            return  # voided or replaced while its read-back was being sent
        if said is None or (gen is not None and gen != self.generation):
            await self.void_challenge()

    async def _answer_challenge(
        self, text: str, *, late: bool = False, turn: int | None = None
    ) -> bool:
        """True when `text` was the reply to a pending confirmation (and was handled). False: it is
        a new question — awaiting nothing, so the caller supersedes before its first await."""
        ch = self.challenge
        if ch is None:
            return False
        armed = ch.get("armed_at")
        if (
            late  # a transcript whose turn already went out: it began before this was armed
            or armed is None
            or ch["gen"] != self.generation  # installed for a superseded question
            or turn is None  # an utterance with no turn number cannot show when it began
            or turn <= ch["armed_turn"]  # its turn began before arming (arriving late)
            or time.monotonic() > ch["expires_at"]
            or self.user_started_at <= armed
        ):
            # Not armed yet, superseded, expired, late, or its turn began before arming or while
            # HQ was still reading it back: a new question (the caller voids the challenge).
            return False
        if not self._confirmation_language_ok():
            # Fail closed: a turn not known to be English never confirms (the matcher is
            # English-only), whatever its words were transcribed as. The caller voids it.
            return False
        first = ch["cards"][0]
        if len(ch["cards"]) == 1 and first["kind"] in ("permission", "menu"):
            # A Needs-you decision (#1681 P2): the reply names WHICH answer, of this row only.
            verdict, choice = classify_decision(text, first)
            idx = 0 if verdict == "choose" else None
            if choice is not None:
                first = {**first, "choice": choice}
        elif len(ch["cards"]) == 1 and first["kind"] == "needs_you" and is_skip(text):
            verdict, idx = "skip", None
        else:
            verdict, idx = classify_confirmation(text, len(ch["cards"]))
        if verdict == "other":
            return False
        if verdict in ("confirm", "choose") and idx is not None:
            self.challenge = None  # single use: consumed BEFORE anything is awaited
            card = first if verdict == "choose" else ch["cards"][idx]
            await self.client(
                {"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text}
            )
            await self.client({"t": "challenge", "cards": []})
            self._side(self._settle_then_next(card, ch["queue"], ch["gen"]))
            return True
        if verdict == "skip":
            self.challenge = None
            await self.client(
                {"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text}
            )
            await self.client({"t": "challenge", "cards": []})
            if not await self._offer_next(ch["queue"], ch["gen"]):
                await self.say("Okay. That was the last one, Commander.", kind="outcome")
            return True
        await self.client({"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text})
        if verdict == "cancel":
            await self.void_challenge()
            await self.say("Okay. It stays on your screen.", kind="outcome")
            return True
        # "all" / "ambiguous" / "broader" / "no_allow": HQ answers, and the challenge waits for
        # THAT line to have played before it arms again — so its own echo can never be taken
        # for a confirmation.
        line = {
            "all": "I can only confirm one at a time, Commander.",
            "broader": "By voice I can only allow it once, Commander. Anything broader stays on"
            " your screen.",
            "no_allow": "I can't allow this one by voice, Commander. Say refuse it, or skip.",
        }.get(verdict, "Which one, Commander? Say confirm and the number.")
        sid = uuid.uuid4().hex[:12]
        # Disarmed BEFORE the line is sent; it re-arms only once THIS line has played.
        ch.update(say_id=sid, sent=False, armed_at=None, expires_at=None)
        await self.client({"t": "challenge", "cards": []})
        said = await self.say(line, kind="outcome", sid=sid)
        if self.challenge is ch and said is None:
            await self.void_challenge()
        return True

    async def _settle_then_next(self, card: dict, queue: list[dict], gen: int) -> None:
        """Settle one card, then offer the next queued Needs-you item — unless something newer
        (a question, an offer) took the call meanwhile."""
        await self._settle_if_still_wanted(card)
        if self.challenge is None and gen == self.generation:
            await self._offer_next(queue, gen)

    async def _offer_next(self, queue: list[dict], gen: int) -> bool:
        """Offer the first queued item not tapped meanwhile, the rest queued behind it. False when
        none is left (or the call moved on)."""
        rest = [c for c in queue if c["id"] not in self.tapped]
        if not rest or gen != self.generation or self.state == "ended":
            return False
        await self.offer(rest[:1], gen=gen, queue=rest[1:])
        return True

    async def _settle_if_still_wanted(self, card: dict) -> None:
        """Settle unless the call ended, was muted, or the card was tapped meanwhile."""
        if self.state == "ended" or self.muted or card["id"] in self.tapped:
            return
        await self.settle(card)

    def _confirmation_language_ok(self) -> bool:
        """English is KNOWN for this utterance. Where the endpoint detects language — on Auto AND
        on a fixed-English call — THIS utterance must have been heard as English; German, mixed or
        unknown fails closed. Only where the endpoint was TESTED and cannot detect is a
        fixed-English or Auto call taken as English (Settings says so: there is nothing to check
        against); an untested endpoint fails closed (#1635 review)."""
        if self.fixed_language not in (None, "en"):
            return False
        if self.detects_language is None:
            return False  # never tested (or its endpoint changed): unknown fails closed
        if self.detects_language:
            return self.heard_language == "en" and self.language in (None, "en")
        return True

    async def settle(self, card: dict) -> None:
        """The card's OWN route, in-process, as the operator (the #1559 mechanism): every guard and
        re-check a tap gets. What is said is rendered from the route's result."""
        from . import asgi_call

        kind = card["kind"]
        if kind in ("nudge", "needs_you"):
            method, path, body = "POST", f"/api/pulse/actions/{card['id']}/approve", None
        elif kind in ("permission", "menu"):
            # The screen's own route and payload: the RECORDED option's number and label. The
            # route re-checks the live screen against what the escalation recorded (#1213).
            choice = card["choice"]
            method, path = "POST", f"/api/pulse/actions/{card['id']}/choose"
            body = {"option": choice["option"], "label": choice["label"]}
        elif kind == "session":
            method, path = "POST", f"/api/ask/proposals/{card['id']}/decide"
            body = {"decision": "start", "bypass": False}
        else:  # operation
            method, path = "POST", f"/api/ask/proposals/{card['id']}/decide"
            body = {"decision": "confirm"}
        try:
            resp = await asgi_call.call(self.app, self.operator, method, path, body)
        except Exception:  # noqa: BLE001 — never speak an exception; say it failed
            log.warning("voice confirmation route failed", exc_info=True)
            resp = None
        ok = settled_ok(card, resp)
        await self.client({"t": "settled", "card": card["id"], "ok": ok,
                           "status": resp.status if resp else 0})  # fmt: skip
        await self.say(outcome_sentence(card, resp), kind="outcome")
        if resp is not None and resp.status == 202 and card["kind"] in ("session", "operation"):
            # Accepted is not finished: follow the card to its end state and say THAT.
            final = await self._follow(card["id"])
            await self.client({"t": "settled", "card": card["id"], "final": final})
            await self.say(final_sentence(card, final), kind="outcome")

    async def _follow(self, pid: str) -> str | None:
        from . import ask_proposals

        deadline = time.monotonic() + OUTCOME_POLL_S
        while time.monotonic() < deadline and self.state != "ended":
            await asyncio.sleep(OUTCOME_EVERY_S)
            status, entries = await asyncio.to_thread(ask_proposals.views, self.cid)
            state = (entries.get(pid) or {}).get("state") if status == "ok" else None
            if state not in (None, "pending", "deciding"):
                return state
        return None

    async def read_needs_you(self, gen: int) -> None:
        """Answer "what needs me?" from the server's own Needs-you list, as fixed sentences.
        Fenced to the question that asked (`gen`, taken when it was asked): if anything newer was
        said or offered meanwhile, this read-out is dropped (it must never replace a newer
        challenge)."""
        from . import asgi_call

        try:
            resp = await asgi_call.call(
                self.app, self.operator, "GET", "/api/pulse/needs-you", None
            )
        except Exception:  # noqa: BLE001
            resp = None
        if gen != self.generation or self.state == "ended":
            return  # superseded while reading: a newer question owns the call now
        if resp is None or resp.status != 200 or not isinstance(resp.body, dict):
            await self.say("I couldn't read what needs you just now, Commander.", kind="outcome")
            return
        rows = [r for r in resp.body.get("rows") or [] if isinstance(r, dict)]
        if not rows:
            await self.say("Nothing needs you right now, Commander.", kind="outcome")
            return
        total = resp.body.get("total")
        if not isinstance(total, int) or isinstance(total, bool) or total < len(rows):
            total = len(rows)
        told, cards = needs_you_readout(rows, total)
        # ONE utterance, so nothing newer can land between its sentences; the cards a voice may
        # settle are then read back by `offer` itself — the read-back that arms the challenge.
        await self.say(told, kind="outcome")
        if cards and gen == self.generation:
            if self.language not in (None, "en"):
                await self.offer(cards, gen=gen)  # read for the screen: nothing arms
            else:
                # ONE row per challenge (#1681 P2): a reply can only name something of THAT row.
                await self.offer(cards[:1], gen=gen, queue=cards[1:])

    async def _report_progress(self, gen: int) -> None:
        """While HQ's turn runs: the ack, then a fixed status line whenever HQ has been quiet too
        long (#1702), so a slow model never reads as a dropped call. Cancelled as the turn
        settles."""
        await asyncio.sleep(ACK_AFTER_S)
        if gen == self.generation and self.state == "thinking":
            line = ACKS[self._ack_i % len(ACKS)]
            self._ack_i += 1
            await self.say(line, kind="ack")
        while self.state != "ended":
            await asyncio.sleep(PROGRESS_TICK_S)
            await self.progress_tick()

    def _track(self, ev: dict) -> None:
        """What the running turn is doing now, from its own progress events (#1702)."""
        work = self.work
        if work is None:
            return
        step = ev.get("step")
        now = time.monotonic()
        if step == "thinking":
            # A model round starts only once the previous round's tools have all returned: every
            # step started so far is DONE (a tool's own event comes before it runs).
            work.update(phase="model", phase_since=now, tool=None, done=work["steps"])
        elif step == "tool":
            work.update(phase="tool", phase_since=now, tool=str(ev.get("tool") or ""))
            work["steps"] += 1
        elif step in FIND_STEPS and work["phase"] != "tool":
            # The no-tools fallback runs the find pipeline without a `tool` event.
            work.update(phase="tool", phase_since=now, tool="find_sessions")
            work["steps"] += 1

    async def progress_tick(self) -> None:
        """Say where the running turn is, if HQ has been quiet long enough (#1702): FIRST_PROGRESS_S
        before the first status line, PROGRESS_EVERY_S between them, STALL_EVERY_S after a stall
        line. Only while HQ is thinking (never over its own audio), the Commander isn't speaking
        and the last status line has gone out — a best-effort cadence of ELIGIBLE quiet, never a
        guarantee of audible speech by a deadline."""
        work = self.work
        if work is None or self.state != "thinking" or self.user_speaking:
            return
        if work.get("unsaid"):
            # The last status line hasn't gone out yet (TTS or the worker is slow): never queue
            # another behind it — at most one is ever waiting.
            return
        now = time.monotonic()
        if not work["updates"]:
            need = FIRST_PROGRESS_S
        else:
            # The stall line comes at the usual pace; only the lines after it slow down.
            need = STALL_EVERY_S if work.get("stalled") else PROGRESS_EVERY_S
        if now - self.hq_quiet_at < need:
            return
        work["updates"] += 1
        work["stalled"] = work["phase"] == "model" and now - work["phase_since"] >= STALL_S
        sid = uuid.uuid4().hex[:12]
        work["unsaid"] = sid  # BEFORE the send: its `said` can come back while it is in flight
        if await self.say(progress_line(work, now), kind="progress", sid=sid, heard=False) is None:
            work["unsaid"] = None

    async def _run_turn(
        self, turn_id: str, text: str, answers, option, *, replying_to: str | None = None
    ) -> dict:
        from . import actuator  # the same snapshot the HQ route takes

        task, view = await ask_orchestrator.begin(
            self.cid,
            turn_id,
            text,
            working_keys=actuator.working_keys(self.registry),
            busy_keys=actuator.busy_keys(self.registry),
            answers=answers,
            option=option,
            contact=self.contact,
            via="call",
            call_answers=self.answers,
            replying_to=replying_to,
            operator=self.operator,
            app=self.app,
        )
        if task is None:
            return view
        queue = ask_orchestrator.listen(self.cid, task)
        said: set[str] = set()
        try:
            while not task.done():
                get = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    ev = get.result()
                    self._track(ev)
                    step = ev.get("tool") or ev.get("step")
                    line = PROGRESS_LINES.get(str(step))
                    if line and line not in said and self.state == "thinking":
                        said.add(line)
                        await self.say(line, kind="progress")
                    await self.client({"t": "progress", "step": str(step)[:40]})
                else:
                    get.cancel()
            return await asyncio.shield(task)
        finally:
            ask_orchestrator.unlisten(self.cid, queue)

    # -- ending ---------------------------------------------------------------------------------

    async def check_silence(self) -> None:
        """The call's clock, ticked by the socket's watchdog (#1668). Ends the call only when HQ
        judged it over and the farewell had its chance to play, or when nobody answered HQ's
        still-there question. Nothing else ends a call on time."""
        if self.state == "ended":
            return
        now = time.monotonic()
        end = self.ending
        if end is not None and end["gen"] == self.generation:
            # Counted from when its audio went out (`said`), never from the send: a goodbye queued
            # behind another say, or slow to synthesise, is not cut off. (An interruption that
            # drops `said` comes with `user_started`, which keeps the call anyway.)
            out = end["said_at"]
            late = out is not None and now - out >= end["speech_s"] + PLAYBACK_FALLBACK_S
            if late and self._ending_holds(end):
                await self.end("hq_ended")  # unconfirmed playback
            return
        st = self.still_there
        if st is None:
            if now - self.last_speech >= SILENCE_ASK_S:
                # Installed BEFORE it is sent (Hermes 6983): a reply landing during the send
                # clears it, and it is never put back afterwards.
                sid = uuid.uuid4().hex[:12]
                self.still_there = {
                    "say_id": sid,
                    "sent_at": None,
                    "said_at": None,
                    "played_at": None,
                    "speech_s": speech_seconds(STILL_THERE_LINE),
                }
                # The reply answers THIS question, not one HQ left open half an hour ago.
                self.ask = None
                self.replying_still_there = True
                self.still_there_since = self.user_starts
                said = await self.say(STILL_THERE_LINE, kind="still_there", sid=sid)
                st = self.still_there
                if st is not None and st["say_id"] == sid:
                    if said is None:
                        self.still_there = None
                    else:
                        st["sent_at"] = time.monotonic()
            return
        heard = st["played_at"]
        if heard is None:  # no ack (a backgrounded tab): count it as played after the fallback
            # From its audio going out (`said`), else from its send: noise interrupting it drops
            # the worker's `said` marker, and a forgotten tab must still be hung up (#1668).
            out = st["said_at"] or st["sent_at"]
            if out is None:
                return  # still being sent: no window has started yet
            heard = out + st["speech_s"] + PLAYBACK_FALLBACK_S
        if now - heard >= NO_ANSWER_S:
            await self.end("no_answer")

    async def end(self, reason: str, *, detail: str = "", endpoint: str = "") -> None:
        if self.state == "ended":
            return
        self.challenge = None  # a dropped call settles nothing
        self.state = "ended"
        # Side tasks are NOT cancelled: a settlement checks the call before it starts, and one
        # already inside its card's route must finish that route, never stop half way.
        self.ended_reason = reason
        try:
            # Bounded: a stalled client or worker socket must not keep a hang-up from finishing.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.client({"t": "ended", "reason": reason, "detail": detail,
                                 "endpoint": endpoint}), SEND_BUDGET_S)  # fmt: skip
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.worker({"t": "stop"}), SEND_BUDGET_S)
        finally:
            self._ended.set()
            if not self.worker_started:
                self.mark_torn_down()  # nothing to tear down

    def mark_torn_down(self) -> None:
        """Nothing of this call runs any more (its worker stopped, or it never had one): the next
        call may be admitted, and `voice setup`/`remove` may proceed (#1397 P4)."""
        self.release()
        self.torn_down.set()

    def release(self) -> None:
        """Drop the shared `voice/call.lock` (#1397 P4). Once a worker started, only after it has
        been STOPPED: setup must not swap or restart what a call is still tearing down."""
        if self._held is not None:
            self._held.close()
            self._held = None

    async def wait_ended(self) -> None:
        await self._ended.wait()


def _turn_number(v: object) -> int | None:
    """The worker's user-turn number, or None for anything that is not a positive int."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


def _sentence(s: str) -> str:
    return s if s.endswith((".", "!", "?")) else s + "."


def _voice_operation(op_id: object) -> bool:
    """May a voice confirm this HQ operation at all? Only one the catalog knows (an unknown id
    fails closed) and that is not a Library operation. A Library card offers Confirm only once
    its content is on screen (#1611), and enabling or widening an automation consents to a scope
    the card shows: none of that is in its one-sentence summary, so a voice never confirms it."""
    from . import ask_library, ask_operations

    op = ask_operations.CATALOG.get(op_id) if isinstance(op_id, str) else None
    return op is not None and op.group not in ask_library.GROUPS


#: The Library operations a voice may confirm (#1681): NEW drafts that run nothing, each only
#: with a spoken summary of its bound draft (`draft_summary`). Edits, copies, enabling and
#: widening stay screen decisions.
VOICE_DRAFTS = frozenset({"playbook.create", "skill.create"})
#: The longest Library summary a call reads out (characters).
DRAFT_TOLD_MAX = 700


def draft_summary(op_id: str, files: dict) -> str | None:
    """A new draft as a FIXED spoken summary (#1681), every field read verbatim — or ``None``
    when anything it promises can't be said in full (the card stays on screen).

    - playbook: "Save a new playbook, “{name}”: {summary}. It has {n} steps: {titles}." — or, with
      several flows, every flow by its title with all of its steps (none is left unsaid);
    - skill: "Save a new skill, “{name}”: {description}. It has {n} files: {paths}."
    """
    if (
        not isinstance(files, dict)
        or not files
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    ):
        return None
    if op_id == "playbook.create":
        from .playbooks import loader, store

        try:
            card = loader.card(loader.validate_tree(store.tree_from_files(files)))
        except Exception:  # noqa: BLE001 — not a valid playbook: nothing to summarise
            return None
        name = _said_name(card.get("name"))
        about = str(card.get("summary") or "")
        said_about = verbatim(about) if about else ""
        # EVERY flow and every step (review of 1420f59e): a step of a second flow is in the
        # playbook too, so a summary that left it out would not be the playbook confirmed.
        flows = sorted(card.get("flows") or [], key=lambda f: f.get("id") != card["default_flow"])
        told = []
        for f in flows:
            steps = [verbatim(str(st.get("title") or "")) for st in f.get("steps") or []]
            if not steps or any(t is None for t in steps):
                return None
            n = len(steps)
            told.append((verbatim(str(f.get("title") or "")), f"{n} step{'s' if n != 1 else ''}: "
                         + "; ".join(steps)))  # type: ignore[arg-type]  # fmt: skip
        if name is None or said_about is None or not told:
            return None
        head = f"Save a new playbook, “{name}”" + (f": {_sentence(said_about)}" if about else ".")
        if len(told) == 1:
            line = f"{head} It has {told[0][1]}."
        else:
            if any(title is None for title, _ in told):
                return None
            parts = " ".join(f"{title}: {steps}." for title, steps in told)
            line = f"{head} It has {len(told)} flows. {parts}"
    elif op_id == "skill.create":
        text = files.get("SKILL.md") or ""
        name = _said_name(_front_matter(text, "name"))
        about = _front_matter(text, "description")
        said_about = verbatim(about) if about else ("" if about == "" else None)
        paths = [verbatim(p) for p in sorted(files)]
        if not name or said_about is None or any(p is None for p in paths):
            return None
        head = f"Save a new skill, “{name}”" + (f": {_sentence(said_about)}" if about else ".")
        n = len(paths)
        line = f"{head} It has {n} file{'s' if n != 1 else ''}: {', '.join(paths)}."  # type: ignore[arg-type]
    else:
        return None
    return line if len(line) <= DRAFT_TOLD_MAX else None


def _said_name(raw: object) -> str | None:
    """A draft's name as said inside the summary's quotes: verbatim, and with no quote marks of
    its own — a name can't close the quotes and read on as the summary's words."""
    name = verbatim(str(raw or "")) if isinstance(raw, str) else None
    if not name or any(q in name for q in '“”"'):
        return None
    return name


def _front_matter(text: str, key: str) -> str | None:
    """A SKILL.md front matter value, ``""`` when absent — or ``None`` when it can't be read as
    one plain line: a value continued on the next line, or a block scalar, would be cut."""
    if not text.startswith("---"):
        return ""
    lines = text.split("\n")[1:40]
    for i, line in enumerate(lines):
        if line.strip() == "---":
            break
        k, sep, v = line.partition(":")
        if not sep or k.strip() != key:
            continue
        v = v.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not v or v[0] in "|>" or (nxt[:1] in (" ", "\t") and nxt.strip()):
            return None  # a block, or a value that continues: not one line to read
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]  # YAML's own quotes are not part of the value
        return v
    return ""


async def draft_summaries(view: dict) -> dict[str, str]:
    """The spoken summary of each new Library draft a turn proposed, read from its BOUND draft —
    `ask_operations.content`, which serves it only while it still hashes to what the card is
    bound to, so what is read is what a confirm would save."""
    from . import ask_operations

    out: dict[str, str] = {}
    for p in view.get("proposals") or []:
        pid = p.get("action_id")
        if (
            p.get("kind") != "operation"
            or p.get("op") not in VOICE_DRAFTS
            or not isinstance(pid, str)
        ):
            continue
        try:
            status, shown = await asyncio.to_thread(ask_operations.content, pid)
        except Exception:  # noqa: BLE001 — unreadable: the card stays on screen
            log.warning("a draft card's content could not be read", exc_info=True)
            continue
        if status != 200 or not isinstance(shown, dict) or shown.get("kind") != "files":
            continue
        files = {
            f.get("path"): f.get("text") for f in shown.get("files") or [] if isinstance(f, dict)
        }
        summary = draft_summary(str(p["op"]), files)
        if summary is not None:
            out[pid] = summary
    return out


def confirmable_cards(view: dict, *, summaries: dict[str, str] | None = None) -> list[dict]:
    """The cards of a settled HQ turn that a voice may confirm: nudges, new sessions, and HQ
    operations that need no on-screen input. What will be SENT is read back in full, word for
    word (`voice_text.verbatim`); a card whose text can't be is left to the screen (#1631)."""
    out: list[dict] = []
    for p in view.get("proposals") or []:
        pid = p.get("action_id")
        if not isinstance(pid, str) or not pid:
            continue
        title = (p.get("title") or "").strip()[:80] or "that session"
        kind = p.get("kind")
        if kind == "nudge":
            text = verbatim(p.get("text") or "")
            if text is None:
                continue  # it can't be heard in full: confirm it on screen
            out.append({"id": pid, "kind": "nudge", "verb": "send it",
                        "line": _sentence(f"A message to {title}: {text}")})  # fmt: skip
        elif kind == "session":
            brief = verbatim(p.get("text") or "")
            if brief is None:
                continue  # the brief is what starts: unheard, it stays a screen decision
            out.append({"id": pid, "kind": "session", "verb": "start it",
                        "line": _sentence(f"A new session in {title}: {brief}")})  # fmt: skip
        elif kind == "operation" and p.get("op") in VOICE_DRAFTS and not p.get("needs"):
            # A new draft: its spoken SUMMARY of the bound draft is what is confirmed, or it stays
            # on screen (#1681).
            summary = (summaries or {}).get(pid)
            if summary is None:
                continue
            out.append({"id": pid, "kind": "operation", "verb": "save it", "line": summary})
        elif kind == "operation" and not p.get("needs") and _voice_operation(p.get("op")):
            # The EFFECT is what is authorized (deleting a project loses its memory; archiving
            # stops its agents): it is read out in full, or the operation stays on screen.
            effect = verbatim(p.get("text") or "")
            if effect is None:
                continue
            label = (p.get("label") or p.get("verb") or "A change").strip()[:60]
            out.append({"id": pid, "kind": "operation", "verb": "do it",
                        "line": _sentence(f"{label}, for {title}: {effect}")})  # fmt: skip
    return out


#: Needs-you items a call describes at most; the rest are counted ("and 7 more").
NEEDS_YOU_TOLD = 5
#: The longest permission dialog a call reads out (characters of the sentence): each part is read
#: verbatim, and a dialog whose whole reading is longer is a screen decision.
PERMISSION_TOLD_MAX = 900
#: Options a menu may have to be read out; a longer menu is a screen decision.
MENU_OPTIONS_TOLD = 5
_NUMBER_WORDS = ("one", "two", "three", "four", "five")
_ICON = re.compile(r"^[^\w\s]{1,2}\s+(?=\w)")  # a tool line's glyph ("# Shell command", "✱ Grep")
_PROMPT = re.compile(r"^\$\s+")  # a shell prompt mark before a command ("$ mkdir -p …")
#: Said after an item the call describes but a voice cannot settle.
_ON_SCREEN = "It's on your screen."


def _who(row: dict) -> str:
    """The session, and where it is: "{title} in {project}". A title or project name is the
    agent's or the operator's text, so it is said only when `verbatim` reads it as written (and it
    is short); otherwise "A session", and no project."""
    title = verbatim(str(row.get("title") or ""))
    if title is None or len(title) > 80:
        return "A session"
    project = row.get("project") if isinstance(row.get("project"), dict) else {}
    name = verbatim(str(project.get("name") or ""))
    return f"{title} in {name}" if name and len(name) <= 60 and name != title else title


def _permission_words(perm: object) -> str | None:
    """What the recorded permission dialog asks, as read out — ONLY when the dialog can be read as
    a whole (#1681): the projection named its request (``command``, and the ``note`` drawn under
    it), its tool line, request, question and every option label read back verbatim, and its
    refusal identified. Otherwise ``None``: the dialog is a screen decision and only that is
    said."""
    if not isinstance(perm, dict):
        return None
    deny = perm.get("deny")
    if not isinstance(deny, int) or isinstance(deny, bool):
        return None
    tool = verbatim(_ICON.sub("", str(perm.get("title") or "")))
    raw = perm.get("command")
    command = verbatim(_PROMPT.sub("", raw.strip())) if isinstance(raw, str) else None
    if tool is None or command is None:
        return None
    note = perm.get("note")
    said_note = verbatim(note) if isinstance(note, str) else None
    if note is not None and said_note is None:
        return None  # a line of the dialog that can't be read: the whole dialog is screen-only
    if verbatim(f"{command} {said_note or ''}") is None:  # within one read-back's bound
        return None
    question = verbatim(str(perm.get("question") or ""))
    if question is None:
        return None
    options = perm.get("options")
    if not isinstance(options, list) or not (1 <= len(options) <= len(_NUMBER_WORDS)):
        return None
    labels = [verbatim(str(o.get("label") or "")) if isinstance(o, dict) else None for o in options]
    if any(lbl is None for lbl in labels):
        return None
    # The WHOLE dialog is SAID, not only checked (Hermes 7021): the request, the question and
    # every option — the broader grants included, so the Commander hears what stays on screen.
    line = f"asks permission for {tool}: {_sentence(command)}"
    if said_note:
        line += f" The dialog also says: {_sentence(said_note)}"
    listed = "; ".join(f"{_NUMBER_WORDS[i]}, {lbl}" for i, lbl in enumerate(labels))
    line += f" It asks: {_sentence(question)} The options are: {_sentence(listed)}"
    return line if len(line) <= PERMISSION_TOLD_MAX else None


def _menu_words(menu: object) -> tuple[str, list[str]] | None:
    """A menu's question and option labels, ONLY when every one reads back verbatim."""
    if not isinstance(menu, dict):
        return None
    question = verbatim(str(menu.get("question") or ""))
    options = menu.get("options")
    if question is None or not isinstance(options, list):
        return None
    if not (1 <= len(options) <= MENU_OPTIONS_TOLD):
        return None
    labels = [verbatim(str(o.get("label") or "")) if isinstance(o, dict) else None for o in options]
    if any(lbl is None for lbl in labels):
        return None
    return question, labels  # type: ignore[return-value]


def _question_card(row: dict) -> dict | None:
    """A QUESTION row whose pending action answers it with text a voice may send."""
    a = row.get("action") if isinstance(row.get("action"), dict) else None
    if not a or row.get("kind") != "question" or not a.get("can_approve"):
        return None
    if a.get("verb") != "answer" or not isinstance(a.get("id"), str):
        return None
    answer = verbatim(str(a.get("answer") or ""))
    if answer is None:
        return None  # the answer that would be sent can't be read back in full: screen only
    line = _sentence(f"{_who(row)} asks a question. The suggested answer is: {answer}")
    return {"id": a["id"], "kind": "needs_you", "verb": "send it", "line": line,
            "ask": "Say confirm to send it, skip, or cancel."}  # fmt: skip


def _escalation(row: dict) -> dict | None:
    """The row's pending ESCALATION — the decision `/choose` answers — or None."""
    a = row.get("action") if isinstance(row.get("action"), dict) else None
    if not a or a.get("verb") != "escalate" or not isinstance(a.get("id"), str) or not a["id"]:
        return None
    return a


def _recorded_option(options: list, n: object, as_: str) -> dict | None:
    """Option ``n`` of a recorded list as a voice choice: its number and its label exactly as
    recorded (what `/choose` compares), or None."""
    if not isinstance(n, int) or isinstance(n, bool):
        return None
    hits = [o for o in options if isinstance(o, dict) and o.get("n") == n]
    if len(hits) != 1 or not isinstance(hits[0].get("label"), str) or not hits[0]["label"]:
        return None
    return {"option": n, "label": hits[0]["label"], "as": as_}


def _permission_card(row: dict) -> dict | None:
    """A permission dialog a voice may decide (#1681 P2): ONLY one readable as a whole, with its
    refusal identified (`_permission_words`). Refuse is always offered then; allow only with an
    identified single-use option — never an option that grants more than this one call."""
    a = _escalation(row)
    if a is None or row.get("kind") not in ("choice", "approval"):
        return None
    perm = a.get("permission")
    words = _permission_words(perm)
    if words is None:
        return None
    options = perm["options"]  # type: ignore[index]
    deny = _recorded_option(options, perm.get("deny"), "refuse")  # type: ignore[union-attr]
    if deny is None:
        return None
    allow = _recorded_option(options, perm.get("once"), "allow")  # type: ignore[union-attr]
    if allow is not None and any(
        o.get("n") == allow["option"] and o.get("persistent") is not False for o in options
    ):
        allow = None  # only a single-use allow, and only when the dialog SAYS it is one
    ask = (
        "Say allow it to allow it once, refuse it, or skip." if allow else "Say refuse it, or skip."
    )
    return {"id": a["id"], "kind": "permission", "verb": "decide", "line": f"{_who(row)} {words}",
            "allow": allow, "deny": deny, "ask": ask}  # fmt: skip


def _menu_card(row: dict) -> dict | None:
    """A menu a voice may answer (#1681 P2): the menu the escalation RECORDED (the one `/choose`
    checks a reply against), every option read verbatim."""
    a = _escalation(row)
    if a is None or row.get("kind") != "choice" or "permission" in a:
        return None
    menu = a.get("menu")
    words = _menu_words(menu)
    if words is None:
        return None
    question, labels = words
    options = [_recorded_option(menu["options"], o.get("n"), "pick") for o in menu["options"]]  # type: ignore[index]
    if any(o is None for o in options):
        return None
    listed = "; ".join(f"{_NUMBER_WORDS[i]}, {lbl}" for i, lbl in enumerate(labels))
    line = _sentence(f"{_who(row)} asks: {_sentence(question)} The options are: {listed}")
    return {"id": a["id"], "kind": "menu", "verb": "decide", "line": line, "options": options,
            "ask": "Say the option's number, or skip."}  # fmt: skip


def needs_you_sentence(row: dict) -> str:
    """What one Needs-you item is waiting on, as FIXED sentences (#1681): the item's own words
    inside them only where `verbatim` reads them in full, else a fixed screen-only line. A
    rationale or summary is never read as the request."""
    who = _who(row)
    kind = row.get("kind")
    action = row.get("action") if isinstance(row.get("action"), dict) else {}
    if kind in ("choice", "approval") and "permission" in action:
        words = _permission_words(action.get("permission"))
        if words is None:
            return f"{who} wants a permission I can't read out — it's on your screen."
        return f"{who} {words} {_ON_SCREEN}"
    if kind == "choice":
        menu = _menu_words(row.get("menu"))
        if menu is None:
            return f"{who} is showing a menu I can't read out — it's on your screen."
        question, labels = menu
        listed = "; ".join(f"{_NUMBER_WORDS[i]}, {lbl}" for i, lbl in enumerate(labels))
        return _sentence(f"{who} asks: {_sentence(question)} The options are: {listed}") + (
            f" {_ON_SCREEN}"
        )
    if kind == "approval":
        return f"{who} wants you to confirm something I can't read out — it's on your screen."
    if kind == "question":
        if action.get("verb") == "answer" and verbatim(str(action.get("answer") or "")):
            answer = verbatim(str(action.get("answer") or ""))
            line = _sentence(f"{who} asks a question. The suggested answer is: {answer}")
            return f"{line} {_ON_SCREEN}"
        return f"{who} asks a question — it's on your screen."
    return f"{who}'s screen needs a look — I couldn't read it."


def needs_you_readout(rows: list[dict], total: int) -> tuple[str, list[dict]]:
    """ "What needs me?" answered with content (#1681): how many, then up to NEEDS_YOU_TOLD items
    in Needs-you order, then how many more (from the list's own ``total``). An item a voice may
    decide is told in full here AND returned as a card, which `offer` reads back again — the
    read-back that arms its challenge."""
    told = rows[:NEEDS_YOU_TOLD]
    cards: list[dict] = []
    parts = [f"{total} {'thing needs' if total == 1 else 'things need'} you."]
    for r in told:
        card = (
            (_question_card(r) or _permission_card(r) or _menu_card(r)) if len(cards) < 4 else None
        )
        if card is not None:
            # Named here in ONE line, in its place (#1683 review): if its offer is cancelled,
            # expires or is talked over, the Commander still heard what it was.
            # Its WHOLE content, in its place (Hermes 7021): if its offer is cancelled, expires or
            # is talked over, the Commander still heard what it asks. The offer reads it again —
            # the read-back that arms its challenge.
            cards.append(card)
            parts.append(card["line"])
        else:
            parts.append(needs_you_sentence(r))
    if cards:
        parts.append(
            "Let's decide that one." if len(cards) == 1 else "Let's take those one at a time."
        )
    more = total - len(told)
    if more > 0:
        parts.append(f"And {more} more on your screen.")
    return " ".join(parts), cards


def needs_you_cards(rows: list[dict]) -> list[dict]:
    """The Needs-you rows a voice may settle, as cards (#1681 P2): a QUESTION whose pending action
    answers it with text, a permission dialog readable as a whole, a menu read in full. The rest
    are described by :func:`needs_you_readout` and settled on the screen."""
    return needs_you_readout(rows, len(rows))[1]


def _result_state(card: dict, body: dict):
    """The settled state in a route's answer: a delivery record's `state`, or — for proposal
    cards — the `{"proposal": view}` the decide route returns."""
    if card["kind"] in ("session", "operation"):
        prop = body.get("proposal")
        return prop.get("state") if isinstance(prop, dict) else None
    if card["kind"] in ("permission", "menu"):
        # `/choose` answers the settled escalation, with the delivered choice under `choice`.
        choice = body.get("choice")
        return choice.get("state") if isinstance(choice, dict) else None
    return body.get("state")


def settled_ok(card: dict, resp) -> bool:
    """Did the route's RESULT say it happened? A 200 is not enough: a delivery can come back
    `failed`, and a decide on an already-decided card answers 200 with that card's state."""
    if resp is None or not 200 <= resp.status < 300:
        return False
    if resp.status == 202:
        return True  # accepted; `_follow` says the end
    body = resp.body if isinstance(resp.body, dict) else {}
    if card["kind"] in ("nudge", "needs_you", "permission", "menu"):
        return _result_state(card, body) == "delivered"
    return _result_state(card, body) in ("done", "started")


def outcome_sentence(card: dict, resp) -> str:
    """What happened, from the route's own status AND result — never model prose."""
    if resp is None:
        return "That didn't go through, Commander. It's still on your screen."
    if resp.status == 202:
        return "Accepted. It's starting now."  # not finished: `final_sentence` says the end
    if 200 <= resp.status < 300:
        body = resp.body if isinstance(resp.body, dict) else {}
        if card["kind"] in ("nudge", "needs_you"):
            if _result_state(card, body) == "delivered":
                return "Sent."
            return "It wasn't sent, Commander. The card has the reason."
        if card["kind"] in ("permission", "menu"):
            if _result_state(card, body) != "delivered":
                return "It wasn't sent, Commander. Check the session."
            choice = card.get("choice") or {}
            if choice.get("as") == "allow":
                return "Allowed, once."
            if choice.get("as") == "refuse":
                return "Refused."
            return _sentence(f"Chosen: {choice.get('label')}")
        return final_sentence(card, _result_state(card, body) or "unknown")
    if card["kind"] in ("permission", "menu"):
        # FIXED sentences, never the route's `detail` (#1683 review): a refusal can quote the
        # live screen's label, which is the agent's text.
        body = resp.body if isinstance(resp.body, dict) else {}
        if body.get("state") == "indeterminate":
            return (
                "I can't tell whether that reached the session, Commander. Check it before "
                "deciding again."
            )
        if resp.status in (404, 409):
            return (
                "Nothing was sent, Commander. The session changed, or it was already decided. "
                "It's on your screen."
            )
    if resp.status == 409:
        return "That card changed, so I didn't send it. It's on your screen."
    return f"That didn't go through ({resp.status}). It's still on your screen."


_FINAL = {
    "started": "The session is up.",
    "done": "Done.",
    "partly_done": "Partly done. The card has the details.",
    "failed": "It failed. The card has the reason.",
    "stale": "That card changed, so nothing ran.",
    "dismissed": "It was dismissed, so nothing ran.",
    "interrupted": "It was interrupted. Check your sessions.",
    "unconfirmed": "It may have started, but I couldn't confirm it. Check your sessions.",
}


def final_sentence(card: dict, state: str | None) -> str:
    """The END of an accepted card, from its stored state — never model prose."""
    if state is None:
        return "Still running. The card will show when it's done."
    return _FINAL.get(state, "The card has the result.")


async def end_if_never_joined(call: Call) -> None:
    await asyncio.sleep(DIAL_TIMEOUT_S)
    if call.state == "dialing" and call._to_client is None:
        await call.end("not_connected")


# --- the registry ------------------------------------------------------------------------------


class Registry:
    """At most one call per instance (#1397 limits)."""

    def __init__(self) -> None:
        self.current: Call | None = None
        self._lock = asyncio.Lock()

    async def admit(self, **kw) -> Call:
        async with self._lock:
            cur = self.current
            if cur is not None and cur.state != "ended":
                raise CallError(409, "a call is already in progress — end it first", "busy")
            if cur is not None and not cur.torn_down.is_set():
                raise CallError(409, "the last call is still hanging up — try again", "busy")
            held = contextlib.ExitStack()
            try:
                held.enter_context(voice_setup.call_lock())
            except BlockingIOError:
                raise CallError(
                    503, "the voice setup is being changed — try again", "busy"
                ) from None
            except OSError as e:  # the voice dir can't be made or locked: named, never a 500
                raise CallError(
                    503,
                    f"the call lock is unavailable ({e.strerror or type(e).__name__})",
                    "runtime",
                ) from None
            call = Call(id=secrets.token_urlsafe(16), **kw)
            call._held = held  # released when the call ends (`voice remove` waits for that)
            self.current = call
            return call

    def get(self, call_id: str) -> Call | None:
        c = self.current
        if (
            c is not None
            and isinstance(call_id, str)
            and call_id.isascii()
            and secrets.compare_digest(c.id, call_id)
        ):
            return c
        return None

    async def end_all(self, reason: str) -> None:
        if self.current is not None:
            await self.current.end(reason)


REGISTRY = Registry()


# --- the worker process ------------------------------------------------------------------------


def worker_python() -> str:
    """The interpreter that runs the worker: `AGENT_SESSIONS_VOICE_PYTHON` (an explicit override
    wins), else the runtime `agent-sessions voice setup` installed (#1397 P4), else the app's own
    (the `voice` extra). Without `voice setup`, a separate environment: docs/voice/runtime.md."""
    override = os.environ.get("AGENT_SESSIONS_VOICE_PYTHON")
    if override:
        return override
    py = voice_setup.runtime_python()
    return str(py) if py is not None else sys.executable


_PACKAGE_DIR = Path(__file__).resolve().parent


def _shim(d: Path) -> str:
    """A dir whose `agent_sessions` is THIS running release's package, for the worker's
    PYTHONPATH: a separate voice environment has Pipecat but not the app, and must run the
    current code after every upgrade."""
    link = d / "agent_sessions"
    if not link.exists():
        os.symlink(_PACKAGE_DIR, link)
    return str(d)


_RUNTIME_CACHE: dict[str, tuple[float, bool, str]] = {}


async def runtime_ready() -> tuple[bool, str]:
    """Whether the worker's interpreter can import Pipecat (cached for a minute)."""
    py = worker_python()
    hit = _RUNTIME_CACHE.get(py)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1], hit[2]
    try:
        with tempfile.TemporaryDirectory(prefix="bl-voice-check-") as d:
            proc = await asyncio.create_subprocess_exec(
                py, "-c", "import pipecat, agent_sessions.voice_worker",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=_worker_env({}, shim=_shim(Path(d))), cwd=d,
            )  # fmt: skip
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=RUNTIME_PROBE_S)
            except BaseException:  # a timeout, or the asking request cancelled: never leave it
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.shield(proc.wait())
                raise
        ok = proc.returncode == 0
        why = "" if ok else (err.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1][:200]
    except (OSError, TimeoutError) as e:
        ok, why = False, str(e)[:200]
    if not ok:
        why = f"the voice runtime is not installed ({why})"
    _RUNTIME_CACHE[py] = (time.monotonic(), ok, why)
    return ok, why


def _worker_env(tokens: dict[str, str], shim: str | None = None) -> dict[str, str]:
    """A sanitized environment: no app secrets, no agent credentials — only what Python needs to
    start, plus the two speech tokens."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PYTHONPATH", "VIRTUAL_ENV")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    if shim is not None:
        env["PYTHONPATH"] = shim  # this release's package, ahead of anything inherited
    # A worker is one call: keep its native thread pools small (#1362's budget spirit).
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        env[k] = "2"
    env.update({k: v for k, v in tokens.items() if v})
    # Where this machine's speech server listens (its socket; never a TCP port, #1397 P4).
    from . import voice_setup

    env["AGENT_SESSIONS_SPEECH_SOCKET"] = str(voice_setup.socket_path())
    return env


@dataclass
class Worker:
    proc: asyncio.subprocess.Process
    sock: Path
    dir: Path

    async def stop(self) -> None:
        """TERM, then KILL, the worker's whole process group (it was started in a session of
        its own, so the group id is its pid): children it forked go with it."""
        pgid = self.proc.pid
        if pgid is None or pgid <= 1:  # killpg(1) would be kill(-1): every process we own
            return
        if self.proc.returncode is not None and not _group_alive(pgid):
            # Already gone (a normal hang-up exits by itself): never signal a number that may
            # have been reused by another process group.
            if not os.environ.get("VOICE_KEEP_WORKER_DIR"):
                shutil.rmtree(self.dir, ignore_errors=True)
            return
        for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, sig)
            try:
                if self.proc.returncode is None:
                    await asyncio.wait_for(self.proc.wait(), timeout=wait)
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.2)
                if not _group_alive(pgid):
                    break
            except TimeoutError:
                continue
        if not os.environ.get("VOICE_KEEP_WORKER_DIR"):
            shutil.rmtree(self.dir, ignore_errors=True)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def start_worker(settings: dict) -> Worker:
    """Spawn the worker on a fresh private socket. The tokens ride the environment, never argv
    (argv is world-readable in /proc on a shared host)."""
    d = Path(tempfile.mkdtemp(prefix="bl-voice-"))
    os.chmod(d, 0o700)
    sock = d / "w.sock"
    tokens = {
        "VOICE_STT_TOKEN": settings["stt"]["api_key"],
        "VOICE_TTS_TOKEN": settings["tts"]["api_key"],
    }
    # stderr goes to a file in the private dir, never an unread pipe: a chatty worker would
    # fill a pipe's buffer and block its own event loop mid-call.
    log_path = d / "worker.log"
    try:
        with open(log_path, "wb") as log_f:
            proc = await asyncio.create_subprocess_exec(
                worker_python(), "-m", "agent_sessions.voice_worker", "--socket", str(sock),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=log_f, env=_worker_env(tokens, shim=_shim(d)), cwd=str(d),
                start_new_session=True,
            )  # fmt: skip
    except BaseException as e:  # no interpreter, no fork, or cancelled: the dir must go
        shutil.rmtree(d, ignore_errors=True)
        if not isinstance(e, OSError):
            raise
        raise CallError(
            503, f"the voice worker could not start: {e.strerror or e}", "worker"
        ) from None
    deadline = time.monotonic() + WORKER_START_S
    try:
        while not sock.exists():
            if proc.returncode is not None or time.monotonic() > deadline:
                tail = ["no output"]
                with contextlib.suppress(OSError):
                    lines = log_path.read_text("utf-8", "replace").strip().splitlines()
                    tail = lines[-1:] or tail
                raise CallError(503, f"the voice worker did not start: {tail[0][:200]}", "worker")
            await asyncio.sleep(0.05)
    except BaseException:
        # Failed, timed out, or the caller was cancelled while waiting: the spawned worker (and
        # its dir) never outlive this function unless it returns them.
        await asyncio.shield(Worker(proc, sock, d).stop())
        raise
    return Worker(proc, sock, d)


def worker_config(settings: dict, voice: str, profile: dict | None = None) -> dict:
    """What the worker is told at start: endpoints WITHOUT tokens (those are in its env)."""
    tt = settings["turn_taking"]
    lang = settings.get("language", "auto")
    detect = settings.get("stt_detects_language") is True and lang in ("auto", "en")
    return {
        "t": "config",
        "stt": {"base_url": settings["stt"]["base_url"], "model": settings["stt"]["model"]},
        "tts": {
            "base_url": settings["tts"]["base_url"],
            "model": settings["tts"]["model"],
            "voice": voice,
            "style": (profile or {}).get("style"),
            "speed": (profile or {}).get("speed"),
        },
        # P3: ask STT for the language only where the endpoint was found to report it.
        # Detection also runs on a fixed-ENGLISH call, where it gates spoken confirmation only;
        # the STT language hint is then left off so the detection is real, not echoed back.
        "detect_language": detect,
        "language": None if detect or lang == "auto" else lang,
        # TTS gets a language only where the conformance probe proved the field is accepted.
        "tts_language_ok": bool(settings.get("tts_accepts_language")),
        "in_rate": IN_RATE,
        "out_rate": OUT_RATE,
        # Silence after speech before the turn may end (VAD stop), per the turn-taking setting.
        "stop_secs": {"quick": 0.2, "natural": 0.35, "patient": 0.6}[tt],
        "interrupt": bool(settings["interrupt"]),
    }


def profile_for(settings: dict, contact: str) -> dict:
    return hq_calls_settings.profile(settings, contact)


def voice_for(settings: dict, contact: str) -> str:
    group = "woman"
    with contextlib.suppress(KeyError):
        group = hq_contacts.get(contact).voice_group or "woman"
    return settings["voices"].get(group) or settings["voices"]["woman"]


def json_msg(msg: dict) -> str:
    out = json.dumps(msg, separators=(",", ":"))
    return out


__all__ = [
    "Call",
    "CallError",
    "REGISTRY",
    "card_sentences",
    "hq_calls_settings",
    "pick_option",
    "speakable",
]
