"""Spoken confirmation, judged (#1715 P1): what a reply to a read-back card means, when the fixed
English phrases (`voice_text.classify_confirmation`) don't cover it — any wording, any language.

One focused model call, the pattern of #1668's call-end judge: the registered, guarded prompt
`voice_confirm_judge` with its locked answer format, `temperature: 0`, over the single transport
(`review.post_chat_response`), counted as HQ's. The read-back and the reply go in as quoted DATA in
the user message; the instructions live only in the system prompt.

It FAILS CLOSED, and keeps two outcomes apart:
- a verdict the model gave: `confirm` (with the card), `ambiguous`, `cancel`, `all` or `other`
  (not an answer: the words go on as a new question);
- NO verdict (`None`): an error, a non-200, a refusal, any finish other than a normal `stop`
  (a cut-off, a content filter, a missing reason), an answer outside the closed set, or no answer
  within `JUDGE_TIMEOUT_S`. Nothing is decided.

The parse is strict: the whole answer, trimmed of whitespace only, must be one token of the set —
`confirm.`, `Confirm`, `yes` and `confirm please` are no verdict. Whether a verdict may still ACT
is the call's to decide afterwards (its freshness fence); this module never settles anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

log = logging.getLogger("agent_sessions.voice_judge")

#: How long the call waits for a verdict before it decides nothing. Bounded, and well inside the
#: challenge's 20 s window: a reply needs an answer while the Commander still expects one.
JUDGE_TIMEOUT_S = 4.0
#: Room for a reasoning model's thinking before the one token (as for #1668's judge).
JUDGE_MAX_TOKENS = 1024
#: How long the call waits for the interjection verdict (#2354). Its own budget: the live HQ
#: model took 4.4-5.2 s for "No hurry" (1.5 s for "What are you doing?"), so `JUDGE_TIMEOUT_S`
#: left most asides with no verdict, and they were queued and answered a whole turn later.
INTERJECT_TIMEOUT_S = 8.0

#: The most cards one challenge may hold, and so the highest card number a verdict may name
#: (#2389: `voice_calls.BATCH_MAX`; kept here so the parser needs no app import).
CARDS_MAX = 20
_CARD_VERDICT = re.compile(r"(confirm|dismiss|cancel|all|other)(?: ([1-9][0-9]?))?")
#: Each identifying descriptor the judge is shown, cut to this many characters (#2389).
DESCRIPTOR_MAX = 80


def parse_card_verdict(content: object, cards: int) -> tuple[str, int | None] | None:
    """The judge's answer as the call's outcome: `("confirm", i)` (0-based, a card that was read
    out), `("ambiguous", None)` (a confirm with no number for several cards, or a number within
    the contract that names no card of this challenge), `("cancel" | "all" | "other", None)` —
    or `None`: no verdict. A number outside 1..`CARDS_MAX` (0, 21, a leading zero) is outside the
    locked contract, so no verdict (#2389)."""
    if not isinstance(content, str):
        return None
    m = _CARD_VERDICT.fullmatch(content.strip())
    if m is None:
        return None
    word, n = m.group(1), m.group(2)
    if n is not None and word not in ("confirm", "dismiss"):
        return None  # "cancel 2" is not in the set
    if word not in ("confirm", "dismiss"):
        return word, None
    # A dismiss names its card the way a confirm does (#2120).
    unclear = "ambiguous" if word == "confirm" else "ambiguous_dismiss"
    if n is None:
        return (word, 0) if cards == 1 else (unclear, None)
    if not 1 <= int(n) <= CARDS_MAX:
        return None
    i = int(n) - 1
    return (word, i) if i < cards else (unclear, None)


def _cut(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= DESCRIPTOR_MAX else text[: DESCRIPTOR_MAX - 1] + "…"


def descriptors(cards: list[dict]) -> list[dict]:
    """What identifies each card of a challenge, server-built from the card dicts the call offered
    (#2389): its number as read back, its kind, its target and what it changes, each cut to
    `DESCRIPTOR_MAX`, at most `CARDS_MAX` of them. The short spoken line no longer names the
    cards, so this is how "just the federation one" resolves to its card; nothing outside the
    challenge is listed, so nothing outside it can be named."""
    out: list[dict] = []
    for i, c in enumerate(cards[:CARDS_MAX]):
        out.append(
            {
                "n": i + 1,
                "kind": _cut(c.get("op") or c.get("kind")),
                "target": _cut(c.get("target")),
                "change": _cut(c.get("change")),
            }
        )
    return out


def _user_message(readback: str, cards: int, reply: str, listed: list[dict] | None = None) -> str:
    # JSON-quoted: the texts are data, never instructions (the guard clause says so as well).
    lines = [f"Cards read back: {cards}"]
    if listed:
        # Each card as quoted data (#2389): the spoken line of a batch is short, but a reply may
        # name one card by what it is.
        lines.append(f"The cards (quoted): {json.dumps(listed, ensure_ascii=False)}")
    lines += [
        f"HQ read back (quoted): {json.dumps(readback, ensure_ascii=False)}",
        f"The Commander replied (quoted): {json.dumps(reply, ensure_ascii=False)}",
    ]
    return "\n".join(lines)


async def judge_cards(
    readback: str, cards: int, reply: str, listed: list[dict] | None = None
) -> tuple[str, int | None] | None:
    """Ask the HQ model what `reply` means for the `cards` card(s) HQ read back as `readback`.
    `listed`: the challenge's cards as bounded descriptors (`descriptors`, #2389). `None` when no
    verdict could be had — the caller decides nothing then."""
    from . import ai_providers, hq_calls_settings, hq_usage, prompts, review

    try:
        cfg = review._require_config()
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("voice_confirm_judge")},
                {"role": "user", "content": _user_message(readback, cards, reply, listed)},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": JUDGE_MAX_TOKENS,
        }
        # A spoken turn (#1996): the call's own thinking level and model, off and HQ's by default.
        with hq_usage.hq_call(), ai_providers.spoken_turn(**hq_calls_settings.spoken()):
            r = await asyncio.wait_for(review.post_chat_response(cfg, body), JUDGE_TIMEOUT_S)
        if r.status_code != 200:
            return None
        choice = r.json()["choices"][0]
        message = choice["message"]
        # Only a NORMALLY completed answer counts: a cut-off (`length`), a filtered one
        # (`content_filter`), a missing or unknown reason, or a refusal is no verdict, whatever
        # its content happens to say (Hermes 7282).
        if choice.get("finish_reason") != "stop" or message.get("refusal"):
            return None
        content = message.get("content")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — no verdict: nothing is decided
        log.info("voice confirmation judge gave no verdict", exc_info=True)
        return None
    return parse_card_verdict(content, cards)


# ---- a likely goodbye while HQ is idle (2026-11-25) ----------------------------------------------

#: The call-end judgement's budget for a likely goodbye: past it, HQ answers the words as usual.
GOODBYE_TIMEOUT_S = 5.0
GOODBYE_MAX_TOKENS = 1024


async def judge_goodbye(said: str, reply: str) -> bool:
    """#1668's focused "is the call over?" judgement (`hq_call_end`), asked at once for words that
    look like a plain goodbye while HQ is idle, instead of a whole HQ turn (that took 36 s). The
    words decide nothing by themselves (operator, 2026-10-24: whether the call is over is HQ's
    call, never a word list's). One request on the call's own thinking level and model, bounded,
    and failing closed: anything but a clean "yes" in time means HQ answers as any other words."""
    from . import ai_providers, hq_calls_settings, hq_usage, prompts, review

    try:
        cfg = review._require_config()
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("hq_call_end")},
                {"role": "user", "content": f"The Commander said: {said}\n\nHQ answered: {reply}"},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": GOODBYE_MAX_TOKENS,
        }
        with hq_usage.hq_call(), ai_providers.spoken_turn(**hq_calls_settings.spoken()):
            r = await asyncio.wait_for(review.post_chat_response(cfg, body), GOODBYE_TIMEOUT_S)
        if r.status_code != 200:
            return False
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            return False
        content = choice["message"].get("content")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — no judgement: the words go to HQ
        log.info("goodbye judgement gave no verdict", exc_info=True)
        return False
    return isinstance(content, str) and content.strip().strip(".!").lower() == "yes"


# ---- the Commander speaks while HQ works (#2113 P3) ---------------------------------------------

#: What the call may do with words heard while HQ works.
INTERJECT_VERDICTS = frozenset({"status", "steer", "later", "switch"})
#: The spoken line a verdict carries, before the call's own text boundary (`aside_text`).
INTERJECT_SAY_MAX = 300
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_interjection(content: object) -> tuple[str, str] | None:
    """The judgement's answer as ``(do, say)``, or ``None``: no verdict. Strict: one JSON object
    (a single code fence around it is tolerated), ``do`` in the closed set, ``say`` a non-empty
    string of at most `INTERJECT_SAY_MAX` characters; anything else is no verdict."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    do, say = value.get("do"), value.get("say")
    if not isinstance(do, str) or do not in INTERJECT_VERDICTS or not isinstance(say, str):
        return None
    say = " ".join(say.split())
    if not say or len(say) > INTERJECT_SAY_MAX:
        return None
    return do, say


def _interject_message(question: str, doing: str, said: str) -> str:
    # JSON-quoted: the texts are data, never instructions (the guard clause says so as well).
    q, d, s = (json.dumps(t, ensure_ascii=False) for t in (question, doing, said))
    return (
        f"The Commander's request HQ is working on (quoted): {q}\n"
        f"What HQ is doing right now (quoted): {d}\n"
        f"The Commander just said (quoted): {s}"
    )


async def judge_interjection(question: str, doing: str, said: str) -> tuple[str, str] | None:
    """Ask the HQ model how to answer `said`, heard while HQ works on `question` and is `doing`
    something. ``(do, say)`` or ``None`` — no verdict, the caller falls back. Never acts.
    Each judgement logs its verdict class, or why there was none, and how long it took (#2354):
    never the words, nor the line it says."""
    from . import ai_providers, hq_calls_settings, hq_usage, prompts, review

    started = time.monotonic()
    verdict: tuple[str, str] | None = None
    reason = "error"
    try:
        cfg = review._require_config()
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("hq_call_interject")},
                {"role": "user", "content": _interject_message(question, doing, said)},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": JUDGE_MAX_TOKENS,
        }
        with hq_usage.hq_call(), ai_providers.spoken_turn(**hq_calls_settings.spoken()):
            r = await asyncio.wait_for(review.post_chat_response(cfg, body), INTERJECT_TIMEOUT_S)
        if r.status_code != 200:
            reason = f"http_{int(r.status_code)}"
            return None
        choice = r.json()["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") != "stop" or message.get("refusal"):
            reason = "refusal" if message.get("refusal") else "finish"
            return None
        verdict = parse_interjection(message.get("content"))
        reason = "ok" if verdict is not None else "parse"
        return verdict
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except TimeoutError:
        reason = "timeout"
        return None
    except Exception:  # noqa: BLE001 — no verdict: the words wait, as before
        log.info("interjection judgement gave no verdict", exc_info=True)
        return None
    finally:
        log.info(
            "interjection verdict=%s reason=%s ms=%d",
            verdict[0] if verdict is not None else "none",
            reason,
            round((time.monotonic() - started) * 1000),
        )
