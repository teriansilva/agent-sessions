"""Calling an API session (#2341): the session leg of a call.

```
browser ──/ws/voice/{id}──▶ Call ──▶ party HQ:      ask_orchestrator turn (voice_calls)
                                └──▶ party session: the pane's own turn route, as the operator
                                                     ──▶ the session's reply, spoken in its voice
```

A call has a PARTY: HQ (``Call.leg is None``) or one API session (``Call.leg``, a `SessionLeg`).
The operator withdrew #1397 D1 for API sessions (comment 141858 on #2341): a call may go straight
to an API session, which answers in its own voice. Terminal sessions stay out of reach (#1613 D17).

- **Admission** (`resolve`): the key passes `engines.parse_key`; the manifest's structured client
  must take turns (`structured_runtime.describe`, never an engine name); the engine must still be
  admitted (not retired or removed); the session must exist, not be archived and not be read-only.
  Anything else is answered with a FIXED line and no leg.
- **A heard utterance is a composer turn.** It goes through the pane's own route
  (``POST /api/structured/sessions/{key}/turns``) in process, as the operator who started the call
  (`asgi_call`): the same login, CSRF and Origin checks, the same single-writer fence,
  queue-while-working and archived refusals. One operation id per utterance, minted when it is
  heard; an uncertain outcome is said as a FIXED line and NEVER resent.
  The request is marked the Commander's own (`asgi_call.COMMANDER`): their spoken words, so a
  session HQ drives (#2397) is taken over by them, exactly as typing in its composer does.
- **The session's reply is spoken**: its last message, `speakable_reply` (markdown removed, code
  and tables replaced by FIXED lines, bounded to `SESSION_SPOKEN_MAX`). While it works the call
  says FIXED progress lines chosen by tool kind, never the agent's text.
- **Nothing a session says reaches HQ.** Its words are never stored in HQ's conversation and never
  sent to HQ's model; the echo cut (`Call.audible`, every party's lines) drops them if heard back.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from . import (
    asgi_call,
    engines,
    hq_calls_settings,
    native_questions,
    prefs,
    prompts,
    structured_runtime,
    voice_callbacks,
)
from .voice_text import classify_decision, is_stop_all_phrase, is_stop_phrase, speakable

log = logging.getLogger(__name__)

#: The most of a session's reply that is spoken; past it a FIXED `REST_LINE` (#2341 §3).
SESSION_SPOKEN_MAX = 900
#: At most one progress line this often while the session works (#2341 §4)…
SESSION_PROGRESS_GAP_S = 12.0
#: …and none in the first seconds of a turn: a quick answer needs no "Reading files.".
SESSION_FIRST_PROGRESS_S = 4.0
#: The follower reads the turn at most this often; between reads it waits on the session's
#: event journal (`EVENTS_WAIT_S`, the route caps it at 3 s) where the client has one.
FOLLOW_POLL_S = 1.0
EVENTS_WAIT_S = 3.0
#: A follower gives up on a turn after this long (it never settled while the call was up).
FOLLOW_MAX_S = 6 * 3600.0
TITLE_SPOKEN_MAX = 60
#: The most voices the call screen offers a session (#2341 §3).
VOICES_OFFERED = 40
#: The marker every call turn starts with (#2341 S3): shown in the transcript, never hidden.
CALL_MARK = "[on a call]"

# -- FIXED lines (#2341): the only words of the call's own; the session's reply is its own ------

CODE_LINE = "I've put the code in the chat."
TABLE_LINE = "I've put a table in the chat."
REST_LINE = "The rest is in the chat."
BACK_LINE = "HQ again. Go ahead, Commander."
NO_HQ_LINE = "HQ isn't set up for calls, so I can't put you back."


def unavailable_line(title: str) -> str:
    return f"{title} can't take calls right now."


def greeting_line(title: str) -> str:
    return f"{title} here. Go ahead."


def uncertain_line(title: str) -> str:
    return f"I can't tell whether that reached {title}; check its chat."


def refused_line(title: str) -> str:
    return f"{title} couldn't take that just now. Check its chat."


def queued_line(title: str) -> str:
    return f"{title} is still working. That's queued next."


def done_line(title: str) -> str:
    return f"{title} is done."


def stopped_line(title: str) -> str:
    return f"{title} stopped."


def failed_line(title: str) -> str:
    return f"{title} ran into an error."


def decision_line(title: str) -> str:
    return f"{title} needs a decision from you. It's in its chat."


def asks_line(title: str, what: str) -> str:
    return f"{title} asks to run {what}." if what else f"{title} asks for permission."


ALLOW_ASK = "Allow it once, refuse it, or skip it?"
ON_SCREEN_ALLOW = "Allowing it is on screen. "
REFUSE_ASK = "Refuse it, or skip it?"
ALLOWED_LINE = "Allowed once."
REFUSED_LINE = "Refused."
SKIPPED_LINE = "Okay, it's waiting in its chat."
BROADER_LINE = "Allow always stays on screen. " + ALLOW_ASK
NO_ALLOW_LINE = "That one can only be refused or skipped here."
ANSWERED_LINE = "That one was answered or changed meanwhile. Check its chat."


def decision_unsure_line(title: str) -> str:
    return f"I can't tell whether that answer reached {title}; check its chat."


def decision_refused_line(title: str) -> str:
    return f"{title} didn't take that answer. Check its chat."


#: The most of a request's own words (its command) read back.
REQUEST_SPOKEN_MAX = 160


CALL_BACK_LINE = "I'll call you back when this is done."
NOTHING_RUNNING_LINE = "Nothing's running right now, so there's nothing to call back about."


def put_through_line(title: str) -> str:
    return f"Putting you through to {title}."


def transfer_failed_line(title: str) -> str:
    return f"Couldn't put you through to {title}. You're still with HQ."


#: A tool call's kind (#2341 §4), from its name, and the FIXED line said for it.
PROGRESS_LINES = {
    "read": "Reading files.",
    "edit": "Making changes.",
    "run": "Running a command.",
    "search": "Searching.",
    "web": "Checking the web.",
    "subagent": "Handing part of it to a helper.",
    "other": "Working on it.",
}
#: The call screen's working band for a session turn that has run no tool yet (review 9031 P2):
#: Stop is there from the moment the turn is followed, not from its first tool call.
WORKING_STEP = PROGRESS_LINES["other"]
_TOOL_KINDS = (
    ("subagent", ("task", "agent", "subagent", "spawn")),
    ("web", ("web", "fetch", "browse", "url", "http")),
    ("search", ("grep", "glob", "search", "find", "ls", "list")),
    ("edit", ("edit", "write", "patch", "replace", "create", "notebook", "apply")),
    ("run", ("bash", "sh", "exec", "execute", "run", "command", "terminal")),
    ("read", ("read", "view", "cat", "open", "file")),
)
_NAME_WORDS = re.compile(r"[a-z]+")


def tool_kind(name: object) -> str:
    """The kind of a tool call by its name, whatever client named it ("Bash", "execute",
    "read_file", "WebFetch"): never an engine's name, only words a tool's name is made of."""
    raw = str(name or "")
    words = _NAME_WORDS.findall(re.sub(r"([a-z])([A-Z])", r"\1 \2", raw).lower())
    for kind, stems in _TOOL_KINDS:
        if any(w.startswith(stem) for w in words for stem in stems):
            return kind
    return "other"


# -- the reply as words to say ------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_TABLE = re.compile(r"(?:^[ \t]*\|.*\|[ \t]*(?:\n|\Z))+", re.M)
_SENTENCE_END = re.compile(r"[.!?](?=\s|\Z)")


def last_message(turn: dict) -> str:
    """The reply's last message (#2341 §3): a long turn's earlier messages are its working notes;
    the last one is the answer. The whole reply when the client marks no message starts."""
    reply = turn.get("reply")
    if not isinstance(reply, str):
        return ""
    breaks = [
        b for b in turn.get("reply_breaks") or () if isinstance(b, int) and 0 <= b < len(reply)
    ]
    return reply[max(breaks) :] if breaks else reply


def speakable_reply(text: str) -> str:
    """A session's reply as the words to say (#2341 §3): fenced code and tables become ONE FIXED
    line each where they were, the rest goes through `voice_text.speakable` (markdown removed,
    links not read out), and past `SESSION_SPOKEN_MAX` it is cut at a sentence end and says the
    FIXED `REST_LINE`. Never words the reply did not hold, besides those fixed lines."""
    out = _FENCE.sub(f"\n{CODE_LINE}\n", text or "")
    out = _TABLE.sub(f"\n{TABLE_LINE}\n", out)
    # One fixed line for a run of them ("code, code" reads as one block in the chat).
    for line in (CODE_LINE, TABLE_LINE):
        out = re.sub(rf"(?:{re.escape(line)}\s*){{2,}}", f"{line} ", out)
    out = speakable(out)
    if len(out) > SESSION_SPOKEN_MAX:
        cut = out[:SESSION_SPOKEN_MAX]
        ends = [m.end() for m in _SENTENCE_END.finditer(cut)]
        end = ends[-1] if ends and ends[-1] > SESSION_SPOKEN_MAX // 4 else len(cut)
        out = f"{cut[:end].rstrip()} {REST_LINE}"
    return out.strip()


def spoken_title(raw: object) -> str:
    """A session's title as the call says it: plain words, bounded, never empty."""
    words = speakable(str(raw or ""))[:TITLE_SPOKEN_MAX].strip(" .,;:")
    return words or "the session"


# -- phrases ------------------------------------------------------------------------------------

_HQ_WORDS = re.compile(r"\b(?:h\s*q|headquarters|hauptquartier)\b")
_BACK_WORDS = frozenset({"back", "return", "zurück", "zurueck", "switch"})
_TAIL_WORDS = frozenset({"please", "bitte", "now", "jetzt", "thanks", "danke"})
BACK_MAX_WORDS = 8


def is_back_to_hq(said: str) -> bool:
    """Nothing but "back to HQ" (#2341 §1): a short utterance that ENDS on HQ and asks to go back
    ("back to HQ", "put me back to HQ please", "zurück zu HQ"), or "HQ" alone. "Tell HQ about it"
    is not: words for the session go to the session."""
    norm = _HQ_WORDS.sub(" hq ", (said or "").lower())
    words = re.findall(r"[^\W\d_]+", norm)
    while words and words[-1] in _TAIL_WORDS:
        words.pop()
    if not words or len(words) > BACK_MAX_WORDS or words[-1] != "hq":
        return False
    return len(words) == 1 or any(w in _BACK_WORDS for w in words)


_CALL_BACK = re.compile(
    r"\b(?:call|ring|phone) me (?:back|when)\b|\bruf(?:e)? mich (?:zurück|zurueck|an)\b"
)
CALL_BACK_MAX_WORDS = 12


def is_call_me_back(said: str) -> bool:
    """ "Call me back when you're done" (#2341 §4): a short utterance asking for a call back
    ("call me back", "ring me when it's done", "ruf mich zurück"). Longer words are a turn."""
    low = (said or "").lower()
    return len(low.split()) <= CALL_BACK_MAX_WORDS and bool(_CALL_BACK.search(low))


# -- the session's voice ------------------------------------------------------------------------


def voice_pool(block: dict) -> list[str]:
    """The voices a session may answer in: the TTS server's listed voices, else the configured
    group voices (#2341 §3)."""
    listed = [v for v in block.get("tts_voices") or () if isinstance(v, str) and v]
    if listed:
        return list(dict.fromkeys(listed))
    return list(dict.fromkeys(v for v in (block.get("voices") or {}).values() if v))


def session_voice(block: dict, key: str, hq_voice: str) -> tuple[str, bool]:
    """``(voice, shared)``: the voice session `key` answers in, never the HQ contact's on the same
    call. Its kept pick if that is still in the pool, else a stable hash of the key over the pool.
    ``shared``: the pool has no other voice, so the session speaks in HQ's (the screen says so)."""
    pool = [v for v in voice_pool(block) if v != hq_voice]
    if not pool:
        return hq_voice, True
    saved = (block.get("session_voices") or {}).get(key)
    if saved in pool:
        return saved, False
    i = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % len(pool)
    return pool[i], False


# -- admission ----------------------------------------------------------------------------------


class Unavailable(Exception):
    """A session that can't take calls (#2341 §6b): `line` is the FIXED line to say or show."""

    def __init__(self, line: str, why: str):
        super().__init__(why)
        self.line = line
        self.why = why


@dataclass(eq=False)
class SessionLeg:
    """One API session as the call's party. Compared by identity: a line or a follower of a leg
    that is no longer the call's is never spoken (#2341 §1b)."""

    key: str
    title: str
    engine: str
    project: str
    voice: str
    voice_shared: bool = False
    bypass: bool = False
    #: The voices the call screen offers for this session (the pool, never HQ's on this call).
    voices: tuple = ()
    #: The call instruction went with a turn of this leg already (#2341 S3): later turns carry
    #: only the marker.
    instructed: bool = False
    #: Turns submitted on this leg that have not settled yet, newest last.
    running: list = field(default_factory=list)
    #: Submissions in the order they were heard: one at a time.
    sending: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress_at: float = 0.0
    #: Pending requests (permission, question) already told about.
    told: set = field(default_factory=set)
    #: The permission prompt the Commander can answer by voice now (#2341 §6): ``{"req", "card",
    #: "since"}``. One at a time; allow-always is never on it.
    decision: dict | None = None

    def public(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "engine": self.engine,
            "project": self.project,
            "voice": self.voice,
            "voice_shared": self.voice_shared,
            "bypass": self.bypass,
            "voices": list(self.voices),
        }


def _body(resp: asgi_call.Response) -> dict:
    return resp.body if isinstance(resp.body, dict) else {}


async def resolve(app, operator, key: object, block: dict, hq_voice: str) -> SessionLeg:
    """The leg for session `key`, or `Unavailable` with the FIXED line (#2341 §1, §6b). Read as
    the operator, through the routes the pane reads (`GET /api/sessions/{key}` and the structured
    snapshot), so the scope rules are the pane's own."""
    nobody = unavailable_line("That session")
    if not isinstance(key, str) or len(key) > 200:
        raise Unavailable(nobody, "unknown")
    try:
        prov, native = engines.parse_key(key)
    except Exception:  # noqa: BLE001 — an unknown engine or a malformed id is no session
        raise Unavailable(nobody, "unknown") from None
    key = f"{prov.engine_id}:{native}"
    desc = await asyncio.to_thread(structured_runtime.describe, prov.engine_id, check_ready=False)
    if "submit" not in desc.operations:
        raise Unavailable(nobody, "not_structured")  # a terminal session (#1613 D17)
    if not engines.registry.admits(prov):
        raise Unavailable(nobody, "retired")
    row = await asgi_call.call(app, operator, "GET", f"/api/sessions/{key}", None)
    if row.status != 200 or not isinstance(row.body, dict):
        raise Unavailable(nobody, "removed")
    title = spoken_title(row.body.get("title"))
    if row.body.get("archived"):
        raise Unavailable(unavailable_line(title), "archived")
    snap = await asgi_call.call(app, operator, "GET", f"/api/structured/sessions/{key}", None)
    s = _body(snap)
    if (
        snap.status != 200
        or s.get("read_only")
        or s.get("start_discarded")
        or s.get("start_expired")
    ):
        raise Unavailable(unavailable_line(title), "unavailable")
    voice, shared = session_voice(block, key, hq_voice)
    if not shared and (block.get("session_voices") or {}).get(key) != voice:
        # Kept from the first call on (#2341 §3), so the pool growing never changes how it sounds.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(hq_calls_settings.set_session_voice, key, voice)
    project = row.body.get("project") if isinstance(row.body.get("project"), dict) else {}
    offered = tuple(v for v in voice_pool(block) if v != hq_voice)[:VOICES_OFFERED]
    return SessionLeg(
        key=key,
        title=title,
        engine=str(prov.manifest.display.name or "")[:40],
        project=str(project.get("name") or "")[:80],
        voice=voice,
        voice_shared=shared,
        bypass=bool(s.get("bypass")),
        voices=offered,
    )


async def set_voice(call, voice: object) -> None:
    """The call screen's voice pick for the session on the line (#2341 §3): one of the voices it
    was offered (never HQ's), kept for the session's next calls too."""
    leg = call.leg
    if leg is None or not isinstance(voice, str) or voice not in leg.voices:
        return
    leg.voice, leg.voice_shared = voice, False
    with contextlib.suppress(Exception):
        await asyncio.to_thread(hq_calls_settings.set_session_voice, leg.key, voice)
    if call.leg is leg:
        # The SAME party, re-described (review 9091): ``update`` tells the screen this is no party
        # switch, so its working band, Stop and challenge stay.
        msg = {"t": "party", "party": leg.public(), "hq": hq_available(), "update": True}
        await call.client(msg)


def hq_available() -> bool:
    """Put-through and Back to HQ need HQ's AI endpoint (#2341 §6b); a direct call does not."""
    return bool(prefs.public_ai_review().get("configured"))


async def attach(call, leg: SessionLeg) -> None:
    """The leg became the call's party: follow the turn the session is ALREADY working on (or
    waiting on a permission for), so its progress, its prompt and Stop work without a new
    message. At most one follower per turn."""
    snap = await _read(call, leg)
    active = (snap or {}).get("active_turn")
    if not _here(call, leg) or not isinstance(active, str) or active in leg.running:
        return
    leg.running.append(active)
    await call.set_state("thinking")
    call._side(follow(call, leg, active))


async def back_to_hq(call) -> None:
    """ "Back to HQ", spoken or tapped (#2341 §1): the same HQ conversation picks up. Without HQ's
    endpoint the session says so and the call stays with it."""
    leg = call.leg
    if leg is None or call.state == "ended":
        return
    if not hq_available():
        await call.say(NO_HQ_LINE, kind="status", leg=leg)
        return
    await call.switch_party(None)


# -- the leg's turns ----------------------------------------------------------------------------


def turn_text(words: str, *, first: bool) -> str:
    """What the session receives for heard `words` (#2341 S3): the visible `CALL_MARK`, on the
    leg's first turn with the registered `session_call` instruction, then the words as heard."""
    if first:
        return f"{CALL_MARK} {prompts.effective('session_call')}\n\n{words}"
    return f"{CALL_MARK} {words}"


def _here(call, leg: SessionLeg) -> bool:
    return call.leg is leg and call.state != "ended"


async def on_utterance(call, text: str) -> None:
    """Words heard on a session leg, after every fence of the call (no-speech, language, echo).
    "Back to HQ" goes back; "stop" while the session works stops its turn; anything else is the
    session's next turn. The operation id is minted HERE, before anything awaits."""
    leg = call.leg
    if leg is None:
        return
    if is_back_to_hq(text):
        await call.caption_you(text)
        await back_to_hq(call)
        return
    dec = leg.decision
    if dec is not None and not dec["armed"]:
        verdict, _ = classify_decision(text, dec["card"])
        if verdict != "other":
            # An answer to a read-back that has not played yet answers nothing: read it again.
            await call.caption_you(text)
            on_interrupt(call)
            return
    elif dec is not None and call.user_starts > dec["since"]:
        verdict, option = classify_decision(text, dec["card"])
        if verdict == "choose" and isinstance(option, dict):
            leg.decision = None
            await call.caption_you(text)
            call._side(decide(call, leg, dec, option["decision"]))
            return
        if verdict in ("skip", "cancel"):
            leg.decision = None
            await call.caption_you(text)
            await call.say(SKIPPED_LINE, kind="status", leg=leg)
            return
        if verdict in ("broader", "no_allow"):
            # "Allow always" is never by voice (#2341 §6); the prompt stays armed.
            await call.caption_you(text)
            dec["since"] = call.user_starts
            line = BROADER_LINE if verdict == "broader" else NO_ALLOW_LINE
            await call.say(line, kind="status", leg=leg)
            return
        leg.decision = None  # other words: the prompt stays in its chat, the words are a turn
    if is_call_me_back(text):
        await call.caption_you(text)
        await call_me_back(call, leg)
        return
    langs = call.listen_languages
    if leg.running and (is_stop_phrase(text, langs) or is_stop_all_phrase(text, langs)):
        await call.caption_you(text)
        stop_turn(call)
        return
    op = str(uuid.uuid4())
    await call.caption_you(text)
    call._side(submit(call, leg, text, op))


async def submit(call, leg: SessionLeg, words: str, op: str) -> None:
    """Send heard words as ONE turn with ONE operation id, in the order heard. Words whose leg is
    gone by now (a party change, the call ended) are dropped unsent: a switch and a submission
    never both act on them (#2341 §1b). Never resent: an uncertain outcome is said, no more."""
    async with leg.sending:
        if not _here(call, leg):
            return
        first = not leg.instructed
        body = {"operation_id": op, "text": turn_text(words, first=first)}
        path = f"/api/structured/sessions/{leg.key}/turns"
        await call.set_state("thinking")
        try:
            resp = await asgi_call.call(call.app, call.operator, "POST", path, body, commander=True)
        except Exception:  # noqa: BLE001 — a write that raised may still have landed
            log.warning(
                "voice call %s: a session turn's outcome is unknown", call.id, exc_info=True
            )
            await _settle_state(call, leg)
            await call.say(uncertain_line(leg.title), kind="error", leg=leg)
            return
        out = _body(resp)
        if resp.status >= 500 or (resp.truncated and resp.status < 400):
            await _settle_state(call, leg)
            await call.say(uncertain_line(leg.title), kind="error", leg=leg)
            return
        if resp.status >= 400:
            await _settle_state(call, leg)
            await call.say(refused_line(leg.title), kind="error", leg=leg)
            return
        if first:
            leg.instructed = True
        state = out.get("state")
        if out.get("handoff") == "uncertain" or state in (
            "uncertain",
            "interrupted",
            "unavailable",
        ):
            await _settle_state(call, leg)
            await call.say(uncertain_line(leg.title), kind="error", leg=leg)
            return
        leg.running.append(op)
    if state == "queued" or out.get("handoff") == "queued":
        await call.say(queued_line(leg.title), kind="status", heard=False, leg=leg)
    call._side(follow(call, leg, op))


#: A turn's states that end it (`structured_runtime._outcome`).
SETTLED = frozenset({"completed", "failed", "interrupted", "uncertain", "unavailable"})


async def _settle_state(call, leg: SessionLeg) -> None:
    if _here(call, leg) and not leg.running and call.state == "thinking":
        await call.set_state("listening")


async def _read(call, leg: SessionLeg) -> dict | None:
    resp = await asgi_call.call(
        call.app, call.operator, "GET", f"/api/structured/sessions/{leg.key}", None
    )
    return resp.body if resp.status == 200 and isinstance(resp.body, dict) else None


async def _wait(call, leg: SessionLeg, cursor: object, since: float) -> None:
    """Until the session's journal moves past `cursor` (at most `EVENTS_WAIT_S`), and at least
    `FOLLOW_POLL_S` after the last read. A client with no event journal is simply polled."""
    if isinstance(cursor, int) and cursor >= 0:
        with contextlib.suppress(Exception):
            await asgi_call.call(
                call.app,
                call.operator,
                "GET",
                f"/api/structured/sessions/{leg.key}/events",
                None,
                query=f"after={cursor}&limit=1&wait={EVENTS_WAIT_S:g}",
            )
    left = FOLLOW_POLL_S - (time.monotonic() - since)
    if left > 0:
        await asyncio.sleep(left)


async def follow(call, leg: SessionLeg, turn_id: str) -> None:
    """Follow one submitted turn while its leg is the call's party: progress lines from its tool
    calls, a FIXED line for a decision it waits on, and its reply once it settles. Stops the moment
    the party changes or the call ends: a late reply is never spoken on another party."""
    begun = time.monotonic()
    seen: set = set()
    try:
        await _work_started(call, leg)
        while _here(call, leg) and time.monotonic() - begun < FOLLOW_MAX_S:
            read_at = time.monotonic()
            try:
                snap = await _read(call, leg)
            except Exception:  # noqa: BLE001 — a failed read is retried, never fatal
                log.warning("voice call %s: a session read failed", call.id, exc_info=True)
                snap = None
            if not _here(call, leg):
                return
            if snap is not None:
                leg.bypass = bool(snap.get("bypass"))
                turn = next(
                    (t for t in snap.get("turns") or () if t.get("turn_id") == turn_id), None
                )
                await _decisions(call, leg, snap, turn_id)
                if turn is not None:
                    await _progress(call, leg, turn, seen, begun)
                    if turn.get("state") in SETTLED:
                        await _settled(call, leg, turn)
                        return
            await _wait(call, leg, (snap or {}).get("event_cursor"), read_at)
    finally:
        with contextlib.suppress(ValueError):
            leg.running.remove(turn_id)
        await _settle_state(call, leg)


#: The only payload fields a request approved by voice may carry: its command, and where it runs.
_SPOKEN_PAYLOAD = frozenset({"command", "cwd"})


def request_words(req: dict) -> str | None:
    """The request's WHOLE payload as words to read back (#2341 §6), or None when it can't be
    disclosed faithfully: anything but a command (a file edit, a patch), a payload with other
    fields, a command past `REQUEST_SPOKEN_MAX`, or one `speakable` would change (markup, a link).
    The directory it runs in is part of the payload, so it is read back too ("npm test, in
    /home/u/proj"): exactly as the payload carries it, or not at all (a directory that is not a
    string, is empty, carries spacing a read-back would lose, or that `speakable` would change
    leaves the request screen-only). Only a request read back in full may be allowed by voice: a
    digest proves which request, not that the Commander heard it."""
    payload = req.get("payload")
    if not isinstance(payload, dict) or not payload or set(payload) - _SPOKEN_PAYLOAD:
        return None
    raw = payload.get("command")
    if not isinstance(raw, str):
        return None
    words = " ".join(raw.split())
    if not words or speakable(words) != words:
        return None
    if "cwd" in payload:
        cwd = payload["cwd"]
        if not isinstance(cwd, str) or not cwd or " ".join(cwd.split()) != cwd:
            return None
        if not cwd.isprintable() or speakable(cwd) != cwd:
            return None
        words = f"{words}, in {cwd}"
    if len(words) > REQUEST_SPOKEN_MAX:
        return None
    return words


def decision_card(req: dict) -> dict | None:
    """A pending request as a permission the English grammar answers (#2341 §6, the existing
    grammar until O2's judged answers): allow it ONCE (``approve``) only when the request offers
    it AND its whole payload is read back (`request_words`), or refuse it (``reject``). Never
    ``always``. An agent's question, or a request with neither choice, is no voice decision: it
    is announced and answered in the chat."""
    choices = req.get("choices") if isinstance(req.get("choices"), list) else []
    if req.get("kind") == native_questions.ASK_TOOL:
        return None
    disclosed = request_words(req) is not None
    allow = {"decision": "approve"} if "approve" in choices and disclosed else None
    deny = {"decision": "reject"} if "reject" in choices else None
    if allow is None and deny is None:
        return None
    return {"kind": "permission", "allow": allow, "deny": deny}


async def _decisions(call, leg: SessionLeg, snap: dict, turn_id: str) -> None:
    """A permission prompt or question the turn waits on, said ONCE per request in the session's
    voice (#2341 §6). A permission is read with its choices and answered by voice through the
    pane's own decision route; a question, a second prompt while one is open, or anything the
    grammar can't answer stays in the chat."""
    for req in snap.get("pending_requests") or ():
        rid = req.get("request_id") if isinstance(req, dict) else None
        if not isinstance(rid, str) or req.get("turn_id") != turn_id or rid in leg.told:
            continue
        leg.told.add(rid)
        card = decision_card(req)
        if card is None or leg.decision is not None:
            await call.say(decision_line(leg.title), kind="status", leg=leg)
            continue
        words = request_words(req) if card["allow"] else None
        line = f"{asks_line(leg.title, words or '')} " + (
            ALLOW_ASK if card["allow"] else ON_SCREEN_ALLOW + REFUSE_ASK
        )
        # Armed only once THIS read-back has played out (`on_played`); talked over or flushed,
        # it is void and read again (`on_interrupt`).
        sid = uuid.uuid4().hex[:12]
        leg.decision = {"req": dict(req), "card": card, "say_id": sid, "armed": False,
                        "since": call.user_starts}  # fmt: skip
        if await call.say(line, kind="readback", sid=sid, leg=leg) is None:
            if leg.decision is not None and leg.decision["say_id"] == sid:
                leg.decision = None
                leg.told.discard(rid)


def on_played(call, say_id: object) -> None:
    """A say of the call played out: a permission read-back arms its spoken answer (#2341 §6).
    Only words BEGUN after this answer it."""
    leg = call.leg
    dec = leg.decision if leg is not None else None
    if dec is not None and not dec["armed"] and say_id == dec["say_id"]:
        dec["armed"], dec["since"] = True, call.user_starts


def on_interrupt(call) -> None:
    """Playback was cut (a barge-in): a permission read-back not yet played is void, and the
    follower reads it again. One already armed was heard and stays."""
    leg = call.leg
    dec = leg.decision if leg is not None else None
    if dec is not None and not dec["armed"]:
        leg.decision = None
        leg.told.discard(dec["req"].get("request_id"))


async def decide(call, leg: SessionLeg, dec: dict, decision: str) -> None:
    """Answer the prompt `dec` read back, through the pane's own decision route, bound to its
    session, turn, request id and payload digest. The binding is re-checked first: a request
    answered on screen meanwhile, or one whose bytes changed, refuses with a FIXED line; so do a
    logout and an archive (the route's own guards). One decision id; never resent."""
    req = dec["req"]
    snap = await _read(call, leg)
    live = next(
        (
            r
            for r in (snap or {}).get("pending_requests") or ()
            if r.get("request_id") == req.get("request_id")
            and r.get("turn_id") == req.get("turn_id")
            and r.get("payload_digest") == req.get("payload_digest")
        ),
        None,
    )
    if not _here(call, leg):
        return
    if live is None:
        await call.say(ANSWERED_LINE, kind="status", leg=leg)
        return
    body = {
        "decision_id": str(uuid.uuid4()),
        "turn_id": req["turn_id"],
        "request_id": req["request_id"],
        "decision": decision,
    }
    if isinstance(req.get("payload_digest"), str) and req["payload_digest"]:
        body["expected_digest"] = req["payload_digest"]
    path = f"/api/structured/sessions/{leg.key}/decisions"
    try:
        resp = await asgi_call.call(call.app, call.operator, "POST", path, body, commander=True)
    except Exception:  # noqa: BLE001 — a write that raised may still have landed
        log.warning(
            "voice call %s: a session decision's outcome is unknown", call.id, exc_info=True
        )
        await call.say(decision_unsure_line(leg.title), kind="error", leg=leg)
        return
    if resp.status >= 500 or (resp.truncated and resp.status < 400):
        await call.say(decision_unsure_line(leg.title), kind="error", leg=leg)
    elif resp.status >= 400:
        await call.say(decision_refused_line(leg.title), kind="error", leg=leg)
    else:
        await call.say(
            ALLOWED_LINE if decision == "approve" else REFUSED_LINE, kind="outcome", leg=leg
        )


async def _work_started(call, leg: SessionLeg) -> None:
    """Open the call screen's working band (and its Stop) for a turn being followed, before any
    tool event: a turn waiting on its first model reply, answering in prose only, or waiting on a
    permission can be stopped too (review 9031 P2). It stays until the turn settles
    (``leg_turn``). Carries no step count, so a band already open keeps its tally."""
    if _here(call, leg):
        await call.client({"t": "progress", "step": WORKING_STEP})


async def _progress(call, leg: SessionLeg, turn: dict, seen: set, begun: float) -> None:
    """At most one FIXED progress line per `SESSION_PROGRESS_GAP_S`, for the newest tool call
    not seen yet; never over the Commander, nor over a line of the call's still playing. The
    call screen's working band shows the step either way."""
    fresh = [t for t in turn.get("tools") or () if isinstance(t, dict) and t.get("id") not in seen]
    for t in fresh:
        seen.add(t.get("id"))
    if not fresh:
        return
    line = PROGRESS_LINES[tool_kind(fresh[-1].get("name"))]
    await call.client({"t": "progress", "step": line, "steps": len(seen)})
    now = time.monotonic()
    if (
        now - begun < SESSION_FIRST_PROGRESS_S
        or now - leg.progress_at < SESSION_PROGRESS_GAP_S
        or call.user_speaking
        or call.hq_quiet_at > now
    ):
        return
    leg.progress_at = now
    await call.say(line, kind="progress", heard=False, leg=leg)


async def _settled(call, leg: SessionLeg, turn: dict) -> None:
    state = turn.get("state")
    await call.client({"t": "leg_turn", "state": state})
    if any(t != turn.get("turn_id") for t in leg.running):
        await _work_started(call, leg)  # a queued turn still runs: its Stop stays on screen
    if state == "completed":
        words = speakable_reply(last_message(turn))
        await call.say(words or done_line(leg.title), kind="answer", leg=leg)
    elif state == "interrupted":
        await call.say(stopped_line(leg.title), kind="status", leg=leg)
    elif state == "uncertain":
        await call.say(uncertain_line(leg.title), kind="error", leg=leg)
    else:
        await call.say(failed_line(leg.title), kind="error", leg=leg)


async def call_me_back(call, leg: SessionLeg) -> None:
    """Record a one-shot callback for the turn running now (#2341 §4), say so, and hang up once
    that has played. The turn is the session's active one as its snapshot says, else this call's
    newest; with none, the call says there is nothing to call back about and stays up."""
    snap = await _read(call, leg)
    active = (snap or {}).get("active_turn")
    turn = active if isinstance(active, str) else (leg.running[-1] if leg.running else None)
    if not _here(call, leg):
        return
    if turn is None:
        await call.say(NOTHING_RUNNING_LINE, kind="status", leg=leg)
        return
    try:
        await asyncio.to_thread(
            voice_callbacks.add,
            key=leg.key,
            turn_id=turn,
            title=leg.title,
            project=leg.project,
            engine=leg.key.partition(":")[0],
        )
    except Exception:  # noqa: BLE001 — not recorded: say so, keep the call
        log.warning("voice call %s: the callback was not recorded", call.id, exc_info=True)
        await call.say(failed_line("The call back"), kind="error", leg=leg)
        return
    await call.hang_up_after(CALL_BACK_LINE, "call_back", leg=leg)


# -- stop ---------------------------------------------------------------------------------------


def stop_turn(call) -> bool:
    """Stop on the call (#2341 §7), or "stop" said while the session works: the session's running
    turn, through the pane's own interrupt route. The call stays up; the turn's settlement says
    "stopped". False when nothing of this leg runs."""
    leg = call.leg
    if leg is None or not leg.running or call.state == "ended":
        return False
    call._side(_interrupt(call, leg))
    return True


async def _interrupt(call, leg: SessionLeg) -> None:
    snap = await _read(call, leg)
    active = (snap or {}).get("active_turn")
    if not isinstance(active, str):
        return
    with contextlib.suppress(Exception):
        await asgi_call.call(
            call.app,
            call.operator,
            "POST",
            f"/api/structured/sessions/{leg.key}/interrupt",
            {"operation_id": str(uuid.uuid4()), "turn_id": active},
            commander=True,
        )
