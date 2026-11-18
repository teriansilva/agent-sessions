"""HQ's turn watcher and working notes (#2136).

A second model reviews HQ's working loop while a turn runs and judges whether it is converging on
the Commander's request. When it is stuck, the watcher gives a different STRATEGY and the turn goes
on. It never ends a turn: the operator's direction (2026-11-18) is that loop guards "should never
stop all, … sanity check the model to not repeat itself and continue the work". A turn ends when
HQ answers, fails for a real reason, or the Commander presses Stop.

- **Notes** (`Notes`): HQ's own working memory for one turn, set with the `note` tool. One bounded
  map (`NOTES_MAX` entries of `NOTE_MAX` characters, the entry updated longest ago evicted), every
  value through the store's text boundary. They travel with every request of the turn.
- **The watcher** (`Watcher`): fed every step and every round's end. At a trigger (every
  `EVERY_ROUNDS` rounds or `EVERY_S` seconds; at once on a mostly-repeated round, on the #2116
  guard's nudge or re-steer, or when the request starts dropping results for room) it judges a
  compact DIGEST of the turn — the request, tool names, a short id of each call's arguments, new
  or repeat, result sizes, what HQ said aloud, its notes, the strategies already tried and how
  many results were dropped for room — never a tool's output. One judgement in flight at a time;
  a trigger meanwhile allows ONE follow-up; `CALLS_MAX` per turn.
- **Verdicts** are `fine` and `replan`. A `replan` applies at the SAFE POINT between rounds, unless
  a newer judgement was applied or the Commander steered since; each one must differ from the
  strategies already tried (the digest lists them).
- **Trust.** The digest and the returned strategy are untrusted data: HQ can paraphrase hostile
  tool text into its notes and asides. The strategy reaches HQ as a field of a tool result, never
  as system text, and every effect still needs the Commander's card.
- **Failure is open for the turn**: an error, timeout or unparsable answer is no verdict.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: When the watcher looks: every this many rounds, or this many seconds, whichever comes first.
EVERY_ROUNDS = 5
EVERY_S = 60.0
#: A round whose calls are at least this share of repeats triggers a look at once.
REPEAT_TRIGGER = 0.5
#: One judgement's limit, and the most judgements one turn may ask for (a cost bound: past it the
#: #2116 guard's notes keep re-steering on their own).
TIMEOUT_S = 8.0
CALLS_MAX = 12
#: The digest's size in characters: the newest rounds in full, older ones as counts. Its parts
#: have budgets of their own: the notes `DIGEST_NOTES_MAX`, each text field `DIGEST_TEXT_MAX`
#: (the request three times that), so the rounds always keep room for the newest one.
DIGEST_MAX = 8_000
DIGEST_NOTES_MAX = 2_500
DIGEST_TEXT_MAX = 200
#: What a digest quotes of HQ's spoken asides, and of the strategies already tried.
DIGEST_ASIDES = 5
DIGEST_STRATEGIES = 4
#: Turn notes: entries, characters per value, characters per key.
NOTES_MAX = 40
NOTE_MAX = 200
NOTE_KEY_MAX = 80
#: What a verdict may carry, in characters, after the store's text boundary.
STRATEGY_MAX = 400
WHY_MAX = 200
#: Room for a reasoning model's thinking before its answer.
MAX_TOKENS = 1024
VERDICTS = frozenset({"fine", "replan"})
#: Tools that never count as progress or as a repeat, for the watcher nor the loop guard.
NEUTRAL_TOOLS = frozenset({"note"})


def _guard(text: str, cap: int) -> str:
    """Through the store's text boundary (known secrets redacted, credential-shaped text
    withheld) BEFORE the cut, so a cut never keeps a secret's prefix. Blocking."""
    from . import ask_store

    return " ".join(ask_store.guard_text(text, ask_store.TEXT_MAX).split())[:cap].strip()


class Notes:
    """HQ's working notes for one turn: key → line, the entry updated longest ago evicted."""

    def __init__(self) -> None:
        self.items: OrderedDict[str, str] = OrderedDict()

    def set(self, key: str, text: str) -> int:
        """Set or overwrite one entry (guarded, cut); returns how many entries there are. An
        empty text removes the entry. Blocking (the store boundary may read the secret store)."""
        key = _guard(key, NOTE_KEY_MAX)
        if not key:
            raise ValueError("a note needs a key")
        self.items.pop(key, None)
        value = _guard(text, NOTE_MAX)
        if value:
            self.items[key] = value  # newest last
        while len(self.items) > NOTES_MAX:
            self.items.popitem(last=False)
        return len(self.items)

    def render(self) -> list[dict]:
        return [{"key": k, "note": v} for k, v in self.items.items()]


@dataclass
class _Call:
    name: str
    #: A short digest of the canonical arguments: the watcher tells repeats apart, never reads them.
    ref: str
    repeat: bool
    size: int
    effect: bool
    refused: bool


@dataclass
class Verdict:
    kind: str
    text: str = ""
    why: str = ""
    #: The trajectory revision and the steering sequence it was judged at.
    revision: int = 0
    steer: int = 0


#: A judgement: the digest (JSON text) in, a parsed answer (or None) out.
Judge = Callable[[str], Awaitable[dict | None]]


@dataclass
class Watcher:
    request: str
    judge: Judge | None = None
    enabled: bool = True
    clock: Callable[[], float] = time.monotonic
    #: The Commander's steering sequence (#2124): a verdict judged before a newer steer is stale.
    steer: Callable[[], int] = lambda: 0
    notes: Notes | None = None

    rounds: list[list[_Call]] = field(default_factory=list)
    current: list[_Call] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    asides: list[str] = field(default_factory=list)
    #: What the Commander said while this turn worked (#2124), newest last.
    commander: list[str] = field(default_factory=list)
    #: The strategies applied so far, oldest first: the next must differ.
    strategies: list[str] = field(default_factory=list)
    revision: int = 0
    calls_used: int = 0
    applied_rev: int = -1
    pending: Verdict | None = None
    task: asyncio.Task | None = None
    dirty: bool = False
    guard_said: str | None = None
    #: Tool results the request has dropped to make room (`_fit`): HQ is losing what it read.
    forgotten: int = 0
    forgotten_seen: int = 0
    started: float = 0.0
    last_look_at: float = 0.0
    rounds_since_look: int = 0
    #: Every verdict the watcher returned, for the turn's record and the P2 trials.
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.started = self.last_look_at = self.clock()
        if self.judge is None:
            self.judge = judge_model

    # ---- feeding it ----------------------------------------------------------------------------

    def observe(self, name: str, raw_args: str, result: str, effect: bool) -> None:
        """One step of the current round."""
        if name in NEUTRAL_TOOLS:
            return
        try:
            canon = json.dumps(json.loads(raw_args or "{}"), sort_keys=True)
        except ValueError:
            canon = raw_args or ""
        key = f"{name}\0{canon}"
        ref = hashlib.blake2b(key.encode(), digest_size=4).hexdigest()
        repeat = key in self.seen
        self.seen.add(key)
        refused = result.startswith('{"error"')
        self.current.append(_Call(name, ref, repeat, len(result), effect, refused))

    def said(self, aside: str) -> None:
        self.asides.append(aside)
        del self.asides[:-DIGEST_ASIDES]

    def heard(self, words: str) -> None:
        """The Commander spoke while HQ worked (#2124): evidence, and a reason to look at the
        next round's end. ("What are you doing?" twice is a strong sign it is stuck.)"""
        self.commander.append(words[:300])
        del self.commander[:-DIGEST_ASIDES]
        self.dirty = True

    def end_round(self, guard: str | None = None, forgotten: int = 0) -> None:
        """Close a round and look if a trigger says so. ``guard``: the #2116 guard's verdict for
        this round; ``forgotten``: how many tool results the request has dropped for room."""
        calls, self.current = self.current, []
        self.rounds.append(calls)
        self.revision += 1
        self.rounds_since_look += 1
        self.guard_said = guard
        self.forgotten = forgotten
        repeat_share = sum(1 for c in calls if c.repeat) / len(calls) if calls else 0.0
        due = (
            self.rounds_since_look >= EVERY_ROUNDS
            or self.clock() - self.last_look_at >= EVERY_S
            or repeat_share >= REPEAT_TRIGGER
            or guard in ("nudge", "wrap")
            or self.forgotten > self.forgotten_seen  # it started losing what it read
            or self.dirty
        )
        if due:
            self._look()

    def available(self) -> bool:
        return self.enabled and self.calls_used < CALLS_MAX

    # ---- looking -------------------------------------------------------------------------------

    def _look(self) -> None:
        if not self.available():
            return
        if self.task is not None and not self.task.done():
            self.dirty = True  # one follow-up once this one returns
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no turn is running here: nothing to judge for
        self.dirty = False
        self.calls_used += 1
        self.forgotten_seen = self.forgotten
        self.rounds_since_look = 0
        self.last_look_at = self.clock()
        self.task = loop.create_task(self._judged(self.revision, self.steer(), self.digest()))

    async def _judged(self, revision: int, steer: int, digest: str) -> Verdict | None:
        assert self.judge is not None
        try:
            raw = await asyncio.wait_for(self.judge(digest), TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — failure is open: no verdict
            log.info("turn watcher gave no verdict", exc_info=True)
            return None
        v = await asyncio.to_thread(parse, raw)
        if v is None:
            return None
        v.revision, v.steer = revision, steer
        self.log.append(v.kind)
        if self.pending is None or v.revision >= self.pending.revision:
            self.pending = v
        return v

    def _take(self) -> Verdict | None:
        """The pending verdict if it is still fresh, consumed: never older than one already
        applied, never from before the Commander's newest steer."""
        v, self.pending = self.pending, None
        if v is None or v.revision <= self.applied_rev or v.steer != self.steer():
            return None
        return v

    def safe_point(self) -> tuple[str, str] | None:
        """What to do at the boundary between rounds, never an end: ``None`` (go on),
        ``("replan", strategy)``, or the #2116 guard's own note when no strategy is ready —
        ``("guard_resteer", "")`` (it would have wrapped) or ``("guard_nudge", "")``."""
        act = self._apply(self._take())
        if act is None and self.dirty:
            self._look()
        if act is not None:
            return act
        if self.guard_said == "wrap":
            return ("guard_resteer", "")
        if self.guard_said == "nudge":
            return ("guard_nudge", "")
        return None

    def _apply(self, v: Verdict | None) -> tuple[str, str] | None:
        if v is None:
            return None
        self.applied_rev = v.revision
        if v.kind != "replan" or v.text in self.strategies:
            # The same strategy again (a judgement that started before the last one applied)
            # would be no change of approach: never sent twice.
            return None
        self.strategies.append(v.text)
        return ("replan", v.text)

    async def close(self) -> None:
        """Cancel and drain the judgement in flight (it has no effect of its own)."""
        task, self.task = self.task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ---- the digest ----------------------------------------------------------------------------

    def digest(self) -> str:
        """The turn as the watcher sees it: never a tool's output, at most `DIGEST_MAX`. Each part
        has its own budget (Hermes 8438): a full notes map never pushes out the call details, nor
        the other way round. Notes: the most recently updated, up to `DIGEST_NOTES_MAX`
        characters (the rest counted). Rounds: newest first, older ones as counts — never fewer
        than the newest round, whose calls are cut to its newest ones if they alone are too
        long."""
        rounds = [
            {
                "round": i + 1,
                "calls": [
                    [c.name, c.ref, "repeat" if c.repeat else "new", c.size]
                    + (["refused"] if c.refused else [])
                    + (["effect"] if c.effect else [])
                    for c in calls
                ],
            }
            for i, calls in enumerate(self.rounds)
        ]
        notes, omitted = self._digest_notes()
        head = {
            "request": self.request[: DIGEST_TEXT_MAX * 3],
            "elapsed_s": int(self.clock() - self.started),
            "loop_guard": self.guard_said or "",
            "results_dropped_for_room": self.forgotten,
            "strategies_tried": [t[:DIGEST_TEXT_MAX] for t in self.strategies[-DIGEST_STRATEGIES:]],
            "said_aloud": [t[:DIGEST_TEXT_MAX] for t in self.asides],
            "commander_said_meanwhile": [t[:DIGEST_TEXT_MAX] for t in self.commander],
            "notes": notes,
            "notes_omitted": omitted,
        }
        older = {"rounds": 0, "calls": 0, "repeats": 0}

        def render() -> str:
            doc = {**head, "older_rounds": older, "rounds": rounds}
            return json.dumps(doc, ensure_ascii=False)

        text = render()
        while len(text) > DIGEST_MAX and len(rounds) > 1:
            gone = rounds.pop(0)
            older["rounds"] += 1
            older["calls"] += len(gone["calls"])
            older["repeats"] += sum(1 for c in gone["calls"] if c[2] == "repeat")
            text = render()
        if len(text) > DIGEST_MAX and rounds:
            newest = rounds[-1]
            calls = newest["calls"]
            while len(text) > DIGEST_MAX and len(calls) > 1:
                calls.pop(0)  # its newest calls stay
                newest["calls_omitted"] = newest.get("calls_omitted", 0) + 1
                text = render()
        return text

    def _digest_notes(self) -> tuple[list[dict], int]:
        """The most recently updated notes that fit `DIGEST_NOTES_MAX`, in their order, and how
        many were left out."""
        all_notes = self.notes.render() if self.notes else []
        kept: list[dict] = []
        used = 2
        for n in reversed(all_notes):
            size = len(json.dumps(n, ensure_ascii=False)) + 2
            if used + size > DIGEST_NOTES_MAX:
                break
            kept.append(n)
            used += size
        kept.reverse()
        return kept, len(all_notes) - len(kept)


def parse(raw: dict | None) -> Verdict | None:
    """A strict verdict, every text field through the store boundary before its cut, or ``None``.
    Blocking (the store boundary may read the secret store)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("verdict")
    if kind not in VERDICTS:
        return None
    why = _guard(raw["why"], WHY_MAX) if isinstance(raw.get("why"), str) else ""
    if kind == "replan":
        strategy = raw.get("strategy")
        text = _guard(strategy, STRATEGY_MAX) if isinstance(strategy, str) else ""
        if not text:
            return None
        return Verdict("replan", text, why)
    return Verdict("fine", "", why)


def resolve_model() -> tuple[str, str | None]:
    """The watcher's model and thinking level — ONE resolver, for production and the P2 trials:
    `hq_models.watcher`, else the question router's (`hq_route`), else the endpoint's ("")."""
    from . import prefs

    own = prefs.get_hq_models()["watcher"]
    route = prefs.get_hq_route()
    model = own["model"] or route["model"]
    thinking = own["thinking"] if own["thinking"] != "hq" else route["thinking"]
    return model, (None if thinking == "hq" else thinking)


async def judge_model(digest: str) -> dict | None:
    """One focused request: the registered, guarded `hq_turn_watch` prompt and the digest as
    quoted data, through the one transport. ``None`` for anything but a normal JSON answer."""
    from . import ai_providers, hq_usage, prompts, review

    cfg = review._require_config()
    model, thinking = resolve_model()
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": prompts.effective("hq_turn_watch")},
            {"role": "user", "content": "The turn so far (quoted JSON data):\n" + digest},
        ],
        "temperature": 0,
        "stream": False,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    with hq_usage.hq_call(), ai_providers.model_override(thinking=thinking, model=model):
        r = await review.post_chat_response(cfg, body)
    if r.status_code != 200:
        return None
    choice = r.json()["choices"][0]
    message = choice["message"]
    if choice.get("finish_reason") != "stop" or message.get("refusal"):
        return None
    obj = review._extract_json(str(message.get("content") or ""))
    return obj if isinstance(obj, dict) else None


# ---- remembering before forgetting (#2136) ------------------------------------------------------

#: What one summarising call may take: results, characters per result, characters in all.
SUMMARISE_ITEMS_MAX = 15
SUMMARISE_RESULT_MAX = 4_000
SUMMARISE_CHARS_MAX = 40_000
SUMMARISE_TIMEOUT_S = 30.0
SUMMARISE_MAX_TOKENS = 2048
#: Notes one summarising call may write.
SUMMARISE_NOTES_MAX = 30


async def summarise(request: str, notes: list[dict], items: list[dict]) -> list[tuple[str, str]]:
    """The notes HQ needs so that ``items`` — tool results about to be dropped for room — never
    have to be read again: one focused request through the one transport, with the registered,
    guarded `hq_turn_notes` prompt and everything else as quoted data (tool output is untrusted).
    Returns ``[(key, note)]``, or ``[]`` on any failure (the results are dropped as before)."""
    from . import hq_usage, prompts, review

    data, used = [], 0
    for it in items[:SUMMARISE_ITEMS_MAX]:
        result = str(it.get("result") or "")[:SUMMARISE_RESULT_MAX]
        used += len(result)
        if used > SUMMARISE_CHARS_MAX:
            break
        data.append({"tool": it.get("tool"), "args": it.get("args"), "result": result})
    if not data:
        return []
    doc = {"request": request[:1_000], "notes": notes, "results": data}
    try:
        cfg = review._require_config()
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("hq_turn_notes")},
                {
                    "role": "user",
                    "content": "Quoted JSON data:\n" + json.dumps(doc, ensure_ascii=False),
                },
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": SUMMARISE_MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        with hq_usage.hq_call():
            r = await asyncio.wait_for(review.post_chat_response(cfg, body), SUMMARISE_TIMEOUT_S)
        if r.status_code != 200:
            return []
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            return []
        obj = review._extract_json(str(choice["message"].get("content") or ""))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail open: the results are dropped as they were before
        log.info("turn notes: nothing summarised", exc_info=True)
        return []
    raw = obj.get("notes") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for n in raw[:SUMMARISE_NOTES_MAX]:
        if isinstance(n, dict) and isinstance(n.get("key"), str) and isinstance(n.get("note"), str):
            if n["key"].strip() and n["note"].strip():
                out.append((n["key"], n["note"]))
    return out
