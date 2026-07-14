"""Pulse "Ask" — natural-language retrieval chat over past sessions (#522).

The user asks in natural language ("I worked on the websocket reconnect bug, which session
was that?"); the assistant answers with one short line plus the matching sessions in the
exact Pulse-card shape the frontend ``Card`` already renders — so a chat match and its
sidebar row always agree, and "Jump in" works unchanged.

Two-stage retrieval, a hard ceiling of **2 LLM calls per ask**:

* **Stage 1 (catalog)** — ``pulse.build_cards(window_days=None)`` over the FULL non-archived
  history (the no-window mode is consumed only here), keyword-prefiltered to a bounded slice
  (keyword hits are never dropped by the cap; recency fills the rest), one
  ``review.complete_json`` call returning strict JSON ``{"answer", "matches": [{"id","why"}]}``.
* **Stage 2 (content confirmation)** — for the top Stage-1 picks whose engine has a
  transcript adapter, the transcript+live tail (``review.gather_input`` under
  ``asyncio.to_thread``) feeds ONE more call to confirm/refine the answer and ranking. A
  per-candidate ``ReviewError`` (adapter-less engine, nothing to review) skips that candidate
  — one broken candidate never poisons the others — and a total Stage-2 failure degrades to
  the Stage-1 result (the user still gets an answer).

Anti-hallucination: every model-returned id is validated against the slice **actually sent**
to the model in that stage (never the whole catalog) and shape-checked via
``engines.parse_key``; unknowns are dropped, duplicates collapsed, the list capped. All
model output is server-capped and rendered as plain text by the UI (model output is DATA).

Conversation state is the frontend's: it replays a bounded history tail per request — no
server-side persistence (matching the repo's no-per-conversation-store ethos). Transcript
snippets go ONLY to the operator-configured AI endpoint, entirely server-side
(``review.complete_json`` / ``gather_input`` — ``trust_env=False`` keeps the key off ambient
proxies); the endpoint URL and key never reach the browser.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from . import engines, pulse, review

# --- request bounds (#522, server-owned) ----------------------------------------------
QUERY_MAX = 2_000
HISTORY_TURNS_MAX = 8
HISTORY_TURN_CHARS_MAX = 2_000
CATALOG_SLICE_MAX = 150
TITLE_MAX = 80
SUMMARY_MAX = 160
# The per-entry cap on the fuller ``ai_recap`` (#481, ≤1500 chars) when it stands in for the
# one-line ``ai_summary`` in a Stage-1 catalog entry (#653). Deliberately conservative: it is
# paid up to ``CATALOG_SLICE_MAX`` times per ask, so the ceiling here — not the recap's own
# length — is what bounds the Stage-1 prompt. Roomier than ``SUMMARY_MAX`` so the ranking model
# sees the chronological brief, not a single distilled line.
CATALOG_RECAP_MAX = 500
CWD_TAIL_CHARS = 120
PROJECT_MAX = 40
STAGE2_CANDIDATES = 5
STAGE2_TAIL_CHARS = 4_000
MATCHES_MAX = 8
ANSWER_MAX = 600
WHY_MAX = 160

_STAGE1_SYSTEM_PROMPT = (
    "You help a developer find their past AI-coding sessions. You are given their question "
    "(and possibly prior conversation turns) plus a catalog of sessions: id, title, project, "
    "working-directory tail, a summary (which may be a short chronological recap of what "
    "happened, one step per line), age in hours. Pick the sessions that best "
    "answer the question, best match first, and answer in one short sentence. Only use ids "
    "that appear in the catalog; return an empty matches list when nothing fits. "
    'Reply with ONLY a JSON object: {"answer": "<one short sentence, max 500 chars>", '
    '"matches": [{"id": "<catalog id>", "why": "<one line, max 140 chars>"}]}.'
)

_STAGE2_SYSTEM_PROMPT = (
    "You verify which of several candidate AI-coding sessions actually answer the "
    "developer's question. You are given the question and, per candidate: id, title, the "
    "catalog-stage reason, and an excerpt of the session's actual transcript. Confirm, "
    "re-rank, or drop candidates based on what the transcripts really contain, best match "
    "first, and refine the one-sentence answer. Only use ids from the candidate list; "
    "return an empty matches list when none truly fit. "
    'Reply with ONLY a JSON object: {"answer": "<one short sentence, max 500 chars>", '
    '"matches": [{"id": "<candidate id>", "why": "<one line, max 140 chars>"}]}.'
)

# Tokens shorter than this are noise ("a", "on", "ws" survives at 2 — keep it permissive).
_WORD_RE = re.compile(r"[a-z0-9_\-./]{2,}")
# Question words / glue that would match everything and starve the real keywords.
_STOPWORDS = frozenset(
    "the a an i on in at of to for was were is are did do done with which what when where "
    "who how that this it its my me we our you your session sessions worked working work "
    "one about and or not".split()
)


def _keywords(query: str) -> list[str]:
    return [w for w in _WORD_RE.findall(query.lower()) if w not in _STOPWORDS]


def build_catalog(*, now: float | None = None, working_keys: set[str] | None = None) -> list[dict]:
    """The full-history candidate set: ``pulse.build_cards`` with the recency window OFF.
    Same scan → archived/`review_excluded` filter → ``display_title`` → ``projects.resolve``
    pipeline as the sidebar/Pulse, and the output IS the Pulse-card shape the frontend
    renders — result assembly is a field-strip away. Pure FS + metadata; run under
    ``asyncio.to_thread``."""
    return pulse.build_cards(window_days=None, now=now, working_keys=working_keys)


def _card_haystack(card: dict) -> str:
    project = card.get("project") or {}
    return " ".join(
        str(v)
        for v in (
            card.get("title"),
            card.get("ai_summary"),
            # The fuller chronological recap (#481/#653): a topic that lives here but not in the
            # one-line summary now registers as a keyword hit, so the prefilter keeps it ahead of
            # recency noise. Internal (`_`-prefixed) — never leaves the server (see `_public_card`).
            card.get("_ai_recap"),
            card.get("cwd"),
            project.get("name"),
        )
        if v
    ).lower()


def _prefilter(catalog: list[dict], query: str, cap: int = CATALOG_SLICE_MAX) -> list[dict]:
    """Bound the catalog slice sent to the model: keyword HITS are taken first (so a match is
    preferred over mere recency), then most-recent-first recency fills the remainder up to
    ``cap``. Hits are still bounded by ``cap`` — when more than ``cap`` cards match (likelier now
    the recap feeds the haystack, #653) the later hits are dropped in ``build_cards`` order.
    ``catalog`` arrives ranked by state/recency, so "fill the rest" is a stable prefix walk."""
    keywords = _keywords(query)
    hits: list[dict] = []
    rest: list[dict] = []
    for card in catalog:
        hay = _card_haystack(card)
        (hits if keywords and any(k in hay for k in keywords) else rest).append(card)
    slice_ = hits[:cap]
    if len(slice_) < cap:
        slice_ += rest[: cap - len(slice_)]
    return slice_


def _catalog_entry(card: dict, now: float) -> dict:
    """The trimmed per-entry view the model sees — bounded fields only, never the internal
    keys.

    ``summary`` prefers the fuller ``ai_recap`` (#481, capped at ``CATALOG_RECAP_MAX``) so the
    ranking model sees the chronological brief; it falls back to the one-line ``ai_summary``
    (capped at ``SUMMARY_MAX``, byte-for-byte as before) when a session has no recap yet (#653).
    The recap arrives on the internal ``_ai_recap`` key — consumed here as retrieval input, never
    echoed to the client (``_public_card`` strips every ``_``-prefixed field)."""
    project = card.get("project") or {}
    recap = str(card.get("_ai_recap") or "").strip()
    summary = (
        recap[:CATALOG_RECAP_MAX] if recap else str(card.get("ai_summary") or "")[:SUMMARY_MAX]
    )
    return {
        "id": card["id"],
        "title": str(card.get("title") or "")[:TITLE_MAX],
        "project": str(project.get("name") or "")[:PROJECT_MAX],
        "cwd": str(card.get("cwd") or "")[-CWD_TAIL_CHARS:],
        "summary": summary,
        "age_hours": round((now - float(card.get("last_activity") or now)) / 3600, 1),
    }


def bound_history(history: object) -> list[dict]:
    """Clamp the replayed conversation tail: the last ``HISTORY_TURNS_MAX`` well-formed
    user/assistant turns, each capped to ``HISTORY_TURN_CHARS_MAX`` chars; anything
    malformed is dropped. The client is untrusted — this is the server-side gate."""
    if not isinstance(history, list):
        return []
    turns: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()[:HISTORY_TURN_CHARS_MAX]
        if text:
            turns.append({"role": role, "content": text})
    return turns[-HISTORY_TURNS_MAX:]


def _clamp_line(value: object, cap: int) -> str:
    """Model output is DATA: collapse whitespace, cap the length, empty on junk."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap]


def _validate_matches(obj: dict, sent_ids: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """Narrow a model reply to ``(answer, [(id, why), …])``. Ids are validated against the
    slice ACTUALLY SENT in this stage (anti-hallucination) and shape-checked via
    ``engines.parse_key``; unknowns dropped, de-duped in model order, capped."""
    answer = _clamp_line(obj.get("answer"), ANSWER_MAX)
    raw = obj.get("matches")
    matches: list[tuple[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if len(matches) >= MATCHES_MAX:
                break
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not isinstance(mid, str) or mid not in sent_ids or mid in seen:
                continue
            try:
                engines.parse_key(mid)
            except Exception:  # noqa: S112 — a bad-shape id is data, not an event to log
                continue
            seen.add(mid)
            matches.append((mid, _clamp_line(item.get("why"), WHY_MAX)))
    return answer, matches


def _public_card(card: dict) -> dict:
    return {k: v for k, v in card.items() if not k.startswith("_")}


def _result(
    answer: str, matches: list[tuple[str, str]], by_id: dict[str, dict], stage: str
) -> dict:
    return {
        "answer": answer,
        "matches": [{**_public_card(by_id[mid]), "why": why} for mid, why in matches],
        "stage": stage,
        "configured": True,
    }


async def _stage2_refine(
    query: str,
    history: list[dict],
    matches: list[tuple[str, str]],
    by_id: dict[str, dict],
) -> tuple[str, list[tuple[str, str]]] | None:
    """Content confirmation: gather each top candidate's transcript+live tail and make ONE
    refine call. Returns the refined ``(answer, matches)`` or ``None`` when Stage 2 could
    not run / failed entirely (caller keeps the Stage-1 result). Per-candidate gather
    failures (``ReviewError``: adapter-less engine, nothing to review) skip that candidate
    only."""
    candidates: list[dict] = []
    for mid, why in matches[:STAGE2_CANDIDATES]:
        try:
            tail, _ = await asyncio.to_thread(review.gather_input, mid, STAGE2_TAIL_CHARS)
        except review.ReviewError:
            continue  # this candidate has no reviewable content — never poisons the rest
        candidates.append(
            {
                "id": mid,
                "title": str(by_id[mid].get("title") or "")[:TITLE_MAX],
                "catalog_reason": why,
                "transcript_tail": tail,
            }
        )
    if not candidates:
        return None
    user = {"question": query, "candidates": candidates}
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": _STAGE2_SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": json.dumps(user)},
            ]
        )
    except review.NotConfiguredError:
        raise  # config can't vanish mid-ask in practice, but never mask it as a degrade
    except review.ReviewError:
        return None  # total Stage-2 failure → the Stage-1 answer stands
    answer, refined = _validate_matches(obj, {c["id"] for c in candidates})
    return answer, refined


async def ask(query: str, history: object = None, *, working_keys: set[str] | None = None) -> dict:
    """One ask: catalog ranking, then transcript-tail confirmation for the top picks.

    Returns ``{"answer", "matches": [PulseCard + "why", …], "stage", "configured": True}``
    with ``stage`` ∈ ``empty`` (no catalog → 0 LLM calls) / ``catalog`` (Stage-1 only) /
    ``content`` (Stage-2 confirmed). Raises :class:`review.NotConfiguredError` when the
    endpoint isn't configured (route → 409) and :class:`review.ReviewError` when Stage 1
    fails (route → 502) — a chat has no useful non-LLM fallback, unlike a Pulse scan.
    """
    # Check config BEFORE any FS work so the unconfigured 409 stays instant (and the
    # "empty catalog → 0 calls" path still surfaces an unconfigured endpoint honestly).
    # `_require_config` is review's own gate — the single source of truth for "configured".
    review._require_config()
    turns = bound_history(history)
    catalog = await asyncio.to_thread(build_catalog, working_keys=working_keys)
    if not catalog:
        return {
            "answer": "No sessions found yet — start one and ask again.",
            "matches": [],
            "stage": "empty",
            "configured": True,
        }
    now = time.time()
    slice_ = _prefilter(catalog, query)
    by_id = {c["id"]: c for c in slice_}
    user = {
        "question": query,
        "catalog": [_catalog_entry(c, now) for c in slice_],
    }
    obj = await review.complete_json(
        [
            {"role": "system", "content": _STAGE1_SYSTEM_PROMPT},
            *turns,
            {"role": "user", "content": json.dumps(user)},
        ]
    )
    answer, matches = _validate_matches(obj, set(by_id))
    if not matches:
        return _result(answer, [], by_id, "catalog")
    refined = await _stage2_refine(query, turns, matches, by_id)
    if refined is None:
        return _result(answer, matches, by_id, "catalog")
    answer2, matches2 = refined
    # A refine that verified but returned no usable answer keeps the Stage-1 line.
    return _result(answer2 or answer, matches2, by_id, "content")
