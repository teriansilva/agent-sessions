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
sentences rendered here for cards and questions. The model never speaks unstored prose, and raw
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
PROGRESS_LINES = {
    "find_sessions": "Looking through your sessions.",
    "list_sessions": "Looking through your sessions.",
    "session_status": "Checking that session.",
    "session_brief": "Checking that session.",
    "list_missions": "Checking your missions.",
    "mission_status": "Checking that mission.",
    "fleet_status": "Checking the fleet.",
    "agent_usage": "Checking your usage.",
}


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
    is_no_speech_phrase,
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
    #: room noise can fire), worker housekeeping, progress and stale acks are not.
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
    ) -> str | None:
        text = text.strip()
        if not text or self.state == "ended":
            return None
        sid = sid or uuid.uuid4().hex[:12]
        # Known BEFORE anything is awaited (#1668, Hermes 6983): a short line can be played, and
        # acked, while the sends below are still in flight.
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
            for pending in (self.ending, self.still_there):
                if pending is not None and ev.get("id") == pending["say_id"]:
                    pending["said_at"] = time.monotonic()
            await self.client({"t": "said", "id": ev.get("id")})
        elif t == "user_stopped":
            await self.client({"t": "user", "speaking": False})
        elif t == "interrupt":
            # The browser flushes what it had queued: those says never play, so their late or
            # duplicate acks are not speech (#1668).
            self.say_ids.clear()
            # Barge-in: the client drops what it has queued, at once — and a challenge whose
            # read-back was talked over is void. Pipecat sends this at EVERY user turn start, a
            # confirmation's too: an ARMED challenge's read-back has played out, so it stays.
            ch = self.challenge
            if ch is not None and ch.get("armed_at") is None:
                await self.void_challenge()
            await self.client({"t": "interrupt"})
            if self.state == "speaking":
                await self.set_state("listening")
        elif t == "bot_started":
            await self.set_state("speaking")
            await self.client({"t": "speaking", "id": ev.get("id"), "on": True})
        elif t == "bot_stopped":
            await self.client({"t": "speaking", "id": ev.get("id"), "on": False})
            if self.state == "speaking":
                await self.set_state("thinking" if self._turn_running() else "listening")
        elif t == "utterance" and is_no_speech_phrase(str(ev.get("text") or "")):
            # Room noise Whisper turned into a stock phrase (#1667): dropped BEFORE anything else
            # — no HQ turn, no language change, an armed confirmation keeps waiting.
            log.debug("voice call %s: dropped a no-speech phrase", self.id)
        elif t == "utterance":
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
        ack_task = asyncio.create_task(self._ack_later(gen))
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
        if self.state == "ended":
            return
        await self.client({"t": "turn", "turn": view})
        if gen != self.generation:
            pass  # a newer question superseded this one: its answer is on screen, not spoken
        elif view.get("status") == "done":
            ask = view.get("ask")
            if isinstance(ask, dict) and ask.get("question_id"):
                self.ask, self.ask_since = ask, self.user_starts  # presented now
            else:
                # A newer answer: an older question is no longer what a reply answers.
                self.ask = None
            words = speakable(view.get("answer") or "")
            cards = confirmable_cards(view)[:4]  # offered by voice; any more are screen-only
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
        self, cards: list[dict], *, turn_id: str | None = None, gen: int | None = None
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
              "expires_at": None, "gen": self.generation}  # fmt: skip
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
        verdict, idx = classify_confirmation(text, len(ch["cards"]))
        if verdict == "other":
            return False
        if verdict == "confirm" and idx is not None:
            self.challenge = None  # single use: consumed BEFORE anything is awaited
            card = ch["cards"][idx]
            await self.client(
                {"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text}
            )
            await self.client({"t": "challenge", "cards": []})
            self._side(self._settle_if_still_wanted(card))
            return True
        await self.client({"t": "caption", "who": "you", "id": uuid.uuid4().hex[:12], "text": text})
        if verdict == "cancel":
            await self.void_challenge()
            await self.say("Okay. It stays on your screen.", kind="outcome")
            return True
        # "all" / "ambiguous": HQ answers, and the challenge waits for THAT line to have played
        # before it arms again — so its own echo can never be taken for a confirmation.
        line = (
            "I can only confirm one at a time, Commander."
            if verdict == "all"
            else "Which one, Commander? Say confirm and the number."
        )
        sid = uuid.uuid4().hex[:12]
        # Disarmed BEFORE the line is sent; it re-arms only once THIS line has played.
        ch.update(say_id=sid, sent=False, armed_at=None, expires_at=None)
        await self.client({"t": "challenge", "cards": []})
        said = await self.say(line, kind="outcome", sid=sid)
        if self.challenge is ch and said is None:
            await self.void_challenge()
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
        cards = needs_you_cards(rows)
        rest = len(rows) - len(cards)
        lead = f"{len(rows)} {'thing needs' if len(rows) == 1 else 'things need'} you."
        if rest:
            lead += f" {rest} of them {'is' if rest == 1 else 'are'} for your screen only."
        await self.say(lead, kind="outcome")
        if cards and gen == self.generation:
            await self.offer(cards, gen=gen)

    async def _ack_later(self, gen: int) -> None:
        await asyncio.sleep(ACK_AFTER_S)
        if gen == self.generation and self.state == "thinking":
            line = ACKS[self._ack_i % len(ACKS)]
            self._ack_i += 1
            await self.say(line, kind="ack")

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


def confirmable_cards(view: dict) -> list[dict]:
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
        elif kind == "operation" and not p.get("needs"):
            # The EFFECT is what is authorized (deleting a project loses its memory; archiving
            # stops its agents): it is read out in full, or the operation stays on screen.
            effect = verbatim(p.get("text") or "")
            if effect is None:
                continue
            label = (p.get("label") or p.get("verb") or "A change").strip()[:60]
            out.append({"id": pid, "kind": "operation", "verb": "do it",
                        "line": _sentence(f"{label}, for {title}: {effect}")})  # fmt: skip
    return out


def needs_you_cards(rows: list[dict]) -> list[dict]:
    """Needs-you rows a voice may approve: a QUESTION whose pending action answers it with text.
    Permission prompts (`approval`), menus (`choice`) and unread screens stay on the screen."""
    out: list[dict] = []
    for r in rows:
        a = r.get("action") if isinstance(r.get("action"), dict) else None
        if not a or r.get("kind") != "question" or not a.get("can_approve"):
            continue
        if a.get("verb") != "answer" or not isinstance(a.get("id"), str):
            continue
        title = str(r.get("title") or "a session").strip()[:80]
        answer = verbatim(str(a.get("answer") or ""))
        if answer is None:
            continue  # the answer that would be sent can't be read back in full: screen only
        line = _sentence(f"{title} asks a question. The suggested answer is: {answer}")
        out.append({"id": a["id"], "kind": "needs_you", "verb": "send it", "line": line})
    return out[:4]


def _result_state(card: dict, body: dict):
    """The settled state in a route's answer: a delivery record's `state`, or — for proposal
    cards — the `{"proposal": view}` the decide route returns."""
    if card["kind"] in ("session", "operation"):
        prop = body.get("proposal")
        return prop.get("state") if isinstance(prop, dict) else None
    return body.get("state")


def settled_ok(card: dict, resp) -> bool:
    """Did the route's RESULT say it happened? A 200 is not enough: a delivery can come back
    `failed`, and a decide on an already-decided card answers 200 with that card's state."""
    if resp is None or not 200 <= resp.status < 300:
        return False
    if resp.status == 202:
        return True  # accepted; `_follow` says the end
    body = resp.body if isinstance(resp.body, dict) else {}
    if card["kind"] in ("nudge", "needs_you"):
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
        return final_sentence(card, _result_state(card, body) or "unknown")
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
