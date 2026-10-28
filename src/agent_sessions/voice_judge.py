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

log = logging.getLogger("agent_sessions.voice_judge")

#: How long the call waits for a verdict before it decides nothing. Bounded, and well inside the
#: challenge's 20 s window: a reply needs an answer while the Commander still expects one.
JUDGE_TIMEOUT_S = 4.0
#: Room for a reasoning model's thinking before the one token (as for #1668's judge).
JUDGE_MAX_TOKENS = 1024

_CARD_VERDICT = re.compile(r"(confirm|cancel|all|other)(?: ([1-9]))?")


def parse_card_verdict(content: object, cards: int) -> tuple[str, int | None] | None:
    """The judge's answer as the call's outcome: `("confirm", i)` (0-based, a card that was read
    out), `("ambiguous", None)` (a confirm with no number for several cards, or a number that was
    not read out), `("cancel" | "all" | "other", None)` — or `None`: no verdict."""
    if not isinstance(content, str):
        return None
    m = _CARD_VERDICT.fullmatch(content.strip())
    if m is None:
        return None
    word, n = m.group(1), m.group(2)
    if n is not None and word != "confirm":
        return None  # "cancel 2" is not in the set
    if word != "confirm":
        return word, None
    if n is None:
        return ("confirm", 0) if cards == 1 else ("ambiguous", None)
    i = int(n) - 1
    return ("confirm", i) if i < cards else ("ambiguous", None)


def _user_message(readback: str, cards: int, reply: str) -> str:
    # JSON-quoted: the texts are data, never instructions (the guard clause says so as well).
    return (
        f"Cards read back: {cards}\n"
        f"HQ read back (quoted): {json.dumps(readback, ensure_ascii=False)}\n"
        f"The Commander replied (quoted): {json.dumps(reply, ensure_ascii=False)}"
    )


async def judge_cards(readback: str, cards: int, reply: str) -> tuple[str, int | None] | None:
    """Ask the HQ model what `reply` means for the `cards` card(s) HQ read back as `readback`.
    `None` when no verdict could be had — the caller decides nothing then."""
    from . import hq_usage, prompts, review

    try:
        cfg = review._require_config()
        body = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": prompts.effective("voice_confirm_judge")},
                {"role": "user", "content": _user_message(readback, cards, reply)},
            ],
            "temperature": 0,
            "stream": False,
            "max_tokens": JUDGE_MAX_TOKENS,
        }
        with hq_usage.hq_call():
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
