"""AI session review engine (#356).

One bounded, non-streaming chat completion per review against the user-configured
OpenAI-compatible endpoint (prefs `ai_review` block). The input has four parts (#611): a
bounded ``## Session`` context header naming the engine and the agent's last-output age; the
engine's saved transcript (via the engine-agnostic ``transcript`` adapters, resolved through
``engines.logical_key`` so a reconciled placeholder still finds its store); the current
terminal SCREEN (via the narrow ``scrollback.live_tail_text`` accessor — never the ring
globals — which replays the ring onto a grid rather than merely stripping its escapes); and
any unsent compose-box draft. Only the transcript is elastic: it is tail-trimmed so the whole
body fits ``max_input_chars`` while every section keeps its heading. The context header sits
OUTSIDE both that budget and the fingerprint, because it carries a clock. The response must be
JSON shaped as ``{"summary", "title", "intervention_required", "reason"}``; a server-owned
shape guard + length caps treat the model output strictly as data (no tool calls, no actions).

Fail-soft contract (#356 staleness semantics): ANY failure — endpoint down, timeout, bad
JSON, empty input — raises :class:`ReviewError` and persists NOTHING, so the last good
result (and its ``reviewed_at`` stale age) survives instead of a failure masquerading as a
fresh review. The API key never appears in errors or logs; callers surface ``str(exc)``.

The periodic scheduler lives in ``ai_review_loop`` (#356 Phase 2); this module stays
deliberately scheduler-free.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from . import metadata, prefs, scrollback, transcript

# Output field caps — server-owned, applied AFTER parsing so an over-long model reply is
# truncated rather than rejected (the shape is the contract; the length is hygiene).
SUMMARY_MAX = 200
TITLE_MAX = 120
REASON_MAX = 280

# How much of the rendered terminal screen to feed the model (chars). Bounded separately from
# max_input_chars so a chatty terminal can't crowd out the transcript.
LIVE_TAIL_CHARS = 4000


def _screen_budget(max_input_chars: int) -> int:
    """The screen CONTENT's share of ``max_input_chars``: at most ``LIVE_TAIL_CHARS``, and never
    so much that the section (heading included) exceeds half the payload. The cap alone was not
    enough to keep the promise above — with the pref at its 1 000-char floor, a 4 000-char screen
    consumed the whole payload and the transcript was truncated out of it entirely, heading and
    all. The heading is deducted here so ``_assemble`` can guarantee its total by construction."""
    return max(0, min(LIVE_TAIL_CHARS, max_input_chars // 2 - len(_LIVE_TAIL_SECTION)))


# Recap (#481): a SECOND review pass over the WHOLE-session transcript producing a
# chronological "what happened" brief for the session-brief modal. Output capped
# server-side; input bounded to a wider budget than the tail review (head+tail sampled when
# over) so the opening phases of a long session aren't lost.
RECAP_MAX = 1500
RECAP_INPUT_CHARS = 16000


def recap_input_chars(cfg: dict | None = None) -> int:
    """The recap's input budget. The recap sees the WHOLE session (head+tail sampled), so it must
    never get a *narrower* view than the tail review's ``max_input_chars`` — which the bare
    ``RECAP_INPUT_CHARS`` constant did whenever the user raised that pref above 16 000 (its default
    is 24 000, so the recap was the narrower of the two out of the box, contradicting its own
    docstring)."""
    block = cfg if cfg is not None else prefs.get_ai_review()
    try:
        return max(RECAP_INPUT_CHARS, int(block["max_input_chars"]))
    except (KeyError, TypeError, ValueError):
        return RECAP_INPUT_CHARS


RECAP_SYSTEM_PROMPT = (
    "You write a brief for a developer returning to a coding-agent session. From the session "
    "transcript (you may see the beginning and the most recent part, with the middle elided) "
    "plus any live terminal tail, write a SHORT CHRONOLOGICAL recap of what happened: 3 to 6 "
    "terse past-tense steps in the order they occurred, each on its own line, with the LAST "
    "line stating the current state or what is pending. The client renders the lines as a "
    "numbered timeline, so do NOT number or bullet them yourself, and write no preamble and no "
    "headings. Inside a line you may use **bold** for the leading action verb and backticks for "
    "file names, commands and identifiers — no other markdown. Reply with ONLY a JSON object: "
    '{"recap": "<chronological recap, max ~900 chars, one step per line>"}.'
)

# Leading ordinal/bullet glyphs to strip from a recap line (#744). The <ol> in the session brief
# supplies the ordinal, so a model that numbers its steps anyway would render "1. 1. …". Matches
# only a glyph that OPENS the line and is followed by space, so prose like "3.5x faster" or a
# step starting with "-Wall" survives untouched.
_RECAP_LEADING_MARK = re.compile(r"^(?:[-*+•‣▪–—]|\(?\d{1,2}[.)])\s+")


# Hard request timeout for the review completion call. Sized for SLOW LOCAL MODELS
# (#391): ~13 tok/s generation plus prompt processing on multi-thousand-token
# transcripts means real reviews take 40-90s — 30s aborted every one while the
# gateway logged no error. Env-tunable; the floor keeps a typo from zeroing it.
def _timeout_env(name: str, default: float) -> float:
    try:
        return max(10.0, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def request_timeout(cfg: dict | None = None) -> float:
    """Per-call timeout for the review completion request (#391 follow-up). Resolution:
    the Settings value (prefs ``ai_review.request_timeout``) wins when set, else the
    ``AGENT_SESSIONS_AI_REVIEW_TIMEOUT`` env var, else 120s — floored at 10s everywhere.
    Resolved per call (prefs are read per call by design), so a Settings change applies
    to the next review without a restart."""
    block = cfg if cfg is not None else prefs.get_ai_review()
    pref = block.get("request_timeout")
    if isinstance(pref, int | float) and not isinstance(pref, bool):
        return max(10.0, float(pref))
    return _timeout_env("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", 120.0)


MODELS_TIMEOUT_S = 10.0

# /models proxy cache (#356): tiny TTL so the Settings refresh button stays honest while
# repeated dropdown opens don't hammer the endpoint.
MODELS_CACHE_TTL_S = 60.0
_models_cache: dict[str, tuple[float, list[str]]] = {}

# Test seam: when set, every client this module builds routes through this transport
# (httpx.MockTransport in tests — CI never touches the network).
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class ReviewError(Exception):
    """Fail-soft review failure. The message is operator-safe: it never embeds the API
    key. Review (chat-completion) failures never embed raw endpoint response bodies;
    the /models validation probe is the one exception — it carries a BOUNDED,
    key-redacted extract of the gateway's error text (#382) so Settings can show why
    save-time validation failed."""


class NotConfiguredError(ReviewError):
    """The ai_review endpoint is not configured (missing base URL / API key)."""


def _client(timeout: float) -> httpx.AsyncClient:
    # trust_env=False (Hermes on PR #367): httpx defaults to honoring ambient
    # HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env vars, which would silently route the
    # operator's Bearer API key through whatever proxy the host environment has
    # configured. The AI endpoint is operator-provided and explicit — never proxy it.
    return httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT, trust_env=False)


def _require_config() -> dict:
    cfg = prefs.get_ai_review()
    if not str(cfg["base_url"]).strip() or not cfg["api_key"]:
        raise NotConfiguredError("AI review endpoint is not configured")
    return cfg


def _headers(cfg: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg['api_key']}"}


def _base(cfg: dict) -> str:
    return str(cfg["base_url"]).strip().rstrip("/")


# --- input assembly ------------------------------------------------------------------


def _plain_transcript(key: str, aliases: dict[str, str] | None = None) -> str:
    """The engine's saved conversation rendered as plain text (no ANSI). Fail-soft: any
    adapter/parse error → "" (live-tail-only review).

    ``key`` is the session's PHYSICAL key — for codex / opencode / antigravity that is the
    ``new-<uuid>`` placeholder the master was launched under, which ``parse_key`` rejects. Map
    it to the id the engine's own transcript store uses (#611) or every in-app-created session
    on those engines reviews with an empty transcript, on nothing but its terminal screen.
    """
    try:
        from . import engines

        prov, native = engines.parse_key(engines.logical_key(key, aliases))
    except Exception:
        return ""
    adapter = transcript.adapter_for(prov.engine_id)
    if adapter is None:
        return ""
    try:
        turns = adapter(native, Path.home())
    except Exception:
        return ""
    lines: list[str] = []
    for t in turns:
        text = (t.text or "").strip()
        if not text:
            continue
        label = t.role if t.kind == "text" else f"{t.role}/{t.kind}"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


# Live-screen framing (#560 pending-draft rule, re-worded for the #611 renderer).
#
# The section below is now a RENDERED SNAPSHOT of the terminal grid (`scrollback.live_tail_text`
# replays the ring through `vtscreen`), not a strip of raw bytes. The old wording — "any text in
# the input line may be an UNSENT DRAFT the user is still typing" — was written for that byte
# soup and actively primed the failure it was meant to prevent: handed the debris a repainting
# spinner leaves behind, the model dutifully reported that the agent was "typing random
# characters". Describe what the section actually is, and keep the one caveat that is still
# true: the agent's input box holds text that has not been submitted yet.
_LIVE_TAIL_SECTION = (
    "## Terminal screen (rendered snapshot of what is on screen right now)\n"
    "This is the current frame of the agent's terminal, not a log of finalized history. Text "
    "sitting in the agent's input box has NOT been submitted — treat it as PENDING intent, "
    "never as work the agent has already done. Progress spinners, status bars and box-drawing "
    "borders are the agent's own chrome, not output it produced.\n"
)

# Session-context framing (#611). The payload used to be a transcript and a screen with no
# statement of what they belonged to: the model was never told which engine it was reading, so
# it could not know that codex's `Working ·` status line is chrome rather than a literal word,
# and it had no ground truth for idleness — the session whose review read "Idle: agent appears
# to be typing random characters" had emitted bytes milliseconds earlier. These are facts the
# app already holds; they cost three lines and they anchor `intervention_required`.
_ACTIVITY_FRESH_S = 5.0

# The header sits OUTSIDE `max_input_chars` rather than eating into it, so this bounds it
# instead. Deducting it from the body budget would be worse than it looks: the header embeds a
# last-output age whose DIGIT COUNT grows with the age ("0s" → "1234s"), so the body's
# truncation point — and therefore its fingerprint — would drift purely with the clock, and
# every session would look permanently changed. A fixed ceiling here keeps the payload bounded
# at `max_input_chars + SESSION_CONTEXT_MAX` while the hashed body stays clock-independent.
SESSION_CONTEXT_MAX = 200


def _session_context(key: str, phys_key: str, aliases: dict[str, str] | None = None) -> str:
    """A short, bounded ``## Session`` header: which engine produced the transcript + screen
    below, and how long ago the agent last wrote a byte. Fail-soft: an unresolvable key or an
    unobserved session simply omits the field it can't state. Never part of the fingerprint —
    see ``gather_input``."""
    lines: list[str] = []
    with contextlib.suppress(Exception):
        from . import engines

        prov, _ = engines.parse_key(engines.logical_key(key, aliases))
        lines.append(f"- agent: {prov.engine_id}")
    last = scrollback.get_last_output_at(phys_key)
    if last is not None:
        age = max(0.0, time.time() - last)
        state = "RUNNING (producing output right now)" if age < _ACTIVITY_FRESH_S else "quiet"
        lines.append(f"- last terminal output: {age:.0f}s ago — the agent is {state}")
    if not lines:
        return ""
    return ("## Session\n" + "\n".join(lines))[:SESSION_CONTEXT_MAX]


# How much of the compose-box draft to feed the model — bounded SEPARATELY from the PTY tail so a
# long draft can't crowd out the transcript.
PENDING_DRAFT_MAX = 2000

_TRANSCRIPT_TAIL_HEADING = "## Transcript (tail)\n"
_TRANSCRIPT_FULL_HEADING = "## Transcript (full)\n"
_DRAFT_HEADING = (
    "## Pending draft (UNSENT — the user is still composing this in the app; it has NOT been "
    "sent to the agent)\n"
)


def _draft_budget(max_input_chars: int) -> int:
    """The draft's share of ``max_input_chars`` — at most ``PENDING_DRAFT_MAX``, and never more
    than a fifth of the payload. Without this the draft was bounded only by its own constant, so a
    2 000-char draft against the pref's 1 000-char floor filled the whole body; the joined text was
    then tail-truncated, which sheared off EVERY heading — including the draft's own "UNSENT"
    label. Unsent text arriving unlabeled is exactly the failure #560 added the label to prevent."""
    return max(0, min(PENDING_DRAFT_MAX, max_input_chars // 5 - len(_DRAFT_HEADING)))


def _section(heading: str, content: str, budget: int, *, keep: str = "tail") -> str | None:
    """A headed section fitted into ``budget`` TOTAL characters, or ``None`` when the budget
    cannot hold the heading plus at least one character of content. A section is therefore always
    complete and always labeled: it is never half a heading, and content is trimmed rather than
    the label. ``keep`` selects which end of over-long content survives."""
    if not content:
        return None
    room = budget - len(heading)
    if room <= 0:
        return None
    body = content[-room:] if keep == "tail" else content[:room]
    return heading + body


def _pending_draft_section(key: str, budget: int) -> str:
    """The session's unsent compose-box draft (#477 ``SessionMeta.draft``) rendered as a clearly
    labeled PENDING section, or ``""`` when there is none. Fail-soft: any metadata error → "" (a
    draft is advisory context, never a reason to fail a review). Only the already-sanitized draft
    text + attachment names stored in the sidecar are used — no blobs, no extra file reads."""
    try:
        meta = metadata.get(metadata.resolve_key(key))
    except Exception:
        return ""
    if not metadata.has_draft(meta):
        return ""
    d = meta.draft or {}
    text = str(d.get("text", "")).strip()
    names = [
        str(a.get("name", "")).strip()
        for a in (d.get("attachments") or [])
        if isinstance(a, dict) and str(a.get("name", "")).strip()
    ]
    body: list[str] = []
    if text:
        body.append(text)
    if names:
        body.append("attachments: " + ", ".join(names))
    if not body:
        return ""
    # Keep the HEAD of a long draft — what the user started typing — and drop the section whole
    # if the budget cannot carry its label. Unlabeled unsent text is worse than no draft at all.
    return (
        _section(_DRAFT_HEADING, "\n".join(body), budget + len(_DRAFT_HEADING), keep="head") or ""
    )


def _physical(key: str) -> str:
    """The physical key ``key``'s live resources (ring, lock, socket) are under. Fail-soft."""
    try:
        from . import engines

        return engines.physical_key(key)
    except Exception:
        return key


def _load_aliases() -> dict[str, str]:
    """The placeholder→real alias map, fail-soft. Callers inside a sweep should load it once
    and thread it through rather than re-reading the sidecar per session."""
    try:
        return metadata.load_aliases()
    except Exception:
        return {}


def _with_context(key: str, phys_key: str, body: str, aliases: dict[str, str] | None) -> str:
    header = _session_context(key, phys_key, aliases)
    return f"{header}\n\n{body}" if header else body


def gather_input(
    key: str, max_input_chars: int, aliases: dict[str, str] | None = None
) -> tuple[str, str]:
    """Build ``(review_input, fingerprint)`` for a session: a session-context header, the
    transcript tail, the rendered terminal screen, and any unsent compose-box draft — with the
    body tail-truncated to ``max_input_chars``. The screen goes through the bounded
    ``scrollback.live_tail_text`` accessor; the transcript through the engine adapters, resolved
    to the engine's own id (#611). The screen and the draft are framed as PENDING (#560) so a
    typed-but-unsent instruction is never read as completed work. Raises :class:`ReviewError`
    when there is nothing at all to review (no transcript adapter output AND no observed PTY
    output — a draft alone is supplementary).

    The fingerprint is a hash of the assembled input — it changes exactly when the reviewable
    content changes, the property the Phase-2 scheduler's change-detection needs (timestamp quirks
    don't move it). The compose-box draft (#560) is deliberately part of that input, so editing a
    pending draft DOES move the fingerprint and re-triggers review — the one intentional exception
    to "metadata-only writes don't move it."

    The ``## Session`` context header (#611) is deliberately OUTSIDE the fingerprint and outside
    the truncation budget. It carries a last-output *age*, which by construction changes on every
    sweep — hashing it would mark every session permanently changed and re-review the whole
    registry forever. It is prepended after the hash so it also can never be the thing that falls
    off the front when the body is truncated.
    """
    phys_key = _physical(key)
    if aliases is None:
        aliases = _load_aliases()
    transcript_text = _plain_transcript(key, aliases)
    live_text = scrollback.live_tail_text(phys_key, _screen_budget(max_input_chars))
    if not transcript_text and not live_text:
        raise ReviewError("nothing to review: no transcript and no live terminal output")
    body = _assemble(
        key,
        max_input_chars,
        transcript_text,
        _TRANSCRIPT_TAIL_HEADING,
        live_text,
        keep_transcript="tail",
    )
    fingerprint = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
    return _with_context(key, phys_key, body, aliases), fingerprint


def _assemble(
    key: str,
    budget: int,
    transcript_text: str,
    transcript_heading: str,
    live_text: str,
    *,
    keep_transcript: str,
    sample: Callable[[str, int], str] | None = None,
) -> str:
    """Join the body's sections so the total never exceeds ``budget`` **by construction**, and no
    section is ever emitted without its heading.

    The screen and the draft take fixed shares (``_screen_budget`` / ``_draft_budget``); the
    transcript is elastic and takes what remains. A section whose share cannot carry its heading
    plus a character of content is dropped whole rather than sliced. The previous approach — join
    everything, then tail-truncate the result — could shear every heading off the front, leaving
    the model an unlabeled blend of transcript, terminal output, and (worst) the user's UNSENT
    draft, which #560 exists to keep distinguishable from completed work.
    """
    sections: list[str] = []
    spent = 0

    screen = _section(_LIVE_TAIL_SECTION, live_text, len(_LIVE_TAIL_SECTION) + len(live_text))
    draft = _pending_draft_section(key, _draft_budget(budget))
    for fixed in (screen, draft):
        if fixed:
            spent += len(fixed) + 2  # + the "\n\n" separator

    if transcript_text:
        room = budget - spent
        if sample is not None:
            transcript_text = sample(transcript_text, max(0, room - len(transcript_heading)))
        head = _section(transcript_heading, transcript_text, room, keep=keep_transcript)
        if head:
            sections.append(head)
    if screen:
        sections.append(screen)
    if draft:
        sections.append(draft)
    return "\n\n".join(sections)


def _head_tail_sample(text: str, max_chars: int) -> str:
    """Bound ``text`` to ``max_chars`` keeping the HEAD and the TAIL — the chronological
    bookends a recap needs — and eliding the middle with a marker. Under the cap the text is
    returned unchanged. Unlike ``gather_input``'s tail-truncation this preserves the opening
    of a long session so the recap can describe how the work started."""
    if len(text) <= max_chars:
        return text
    marker = "\n\n…[middle elided]…\n\n"
    budget = max_chars - len(marker)
    if budget <= 0:
        return text[:max_chars]
    head = budget * 3 // 5
    tail = budget - head
    return text[:head] + marker + text[-tail:]


def gather_recap_input(
    key: str, max_chars: int, aliases: dict[str, str] | None = None
) -> tuple[str, str]:
    """Build ``(recap_input, fingerprint)`` for a session from the WHOLE saved transcript
    (#481) plus the rendered terminal screen — a wider view than ``gather_input``'s tail so the
    recap can describe the whole session. Bounded by ``_head_tail_sample`` (head+tail when
    over ``max_chars``). Raises :class:`ReviewError` when there is nothing to review. The
    fingerprint hashes the assembled (post-sampling) BODY so the recap regenerates only when
    the content it actually sees changes — its own change-detection, independent of the tail
    review's fingerprint, and (like ``gather_input``) blind to the context header's clock."""
    phys_key = _physical(key)
    if aliases is None:
        aliases = _load_aliases()
    transcript_text = _plain_transcript(key, aliases)
    live_text = scrollback.live_tail_text(phys_key, _screen_budget(max_chars))
    if not transcript_text and not live_text:
        raise ReviewError("nothing to recap: no transcript and no live terminal output")
    # Sample the TRANSCRIPT head+tail rather than the joined body: eliding the middle of the
    # joined text could drop the screen's and the draft's headings, the same mislabeling
    # `_assemble` exists to prevent.
    body = _assemble(
        key,
        max_chars,
        transcript_text,
        _TRANSCRIPT_FULL_HEADING,
        live_text,
        keep_transcript="head",
        sample=_head_tail_sample,
    )
    fingerprint = hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()
    return _with_context(key, phys_key, body, aliases), fingerprint


# --- response parsing ----------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def _extract_json(content: str) -> dict:
    """Tolerant JSON extraction: plain JSON, fenced JSON, or the first JSON object
    embedded in prose. Raises ReviewError when no object can be decoded."""
    s = _FENCE_RE.sub("", content or "").strip()
    for candidate in (s,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    start = s.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    raise ReviewError("review response was not valid JSON")


def _shape_guard(obj: dict) -> dict:
    """Server-owned shape guard + length caps: the model's output is DATA, nothing more.
    Missing/garbage fields fail the review (drop, keep the last good result) rather than
    persisting junk."""
    summary = obj.get("summary")
    title = obj.get("title")
    required = obj.get("intervention_required")
    reason = obj.get("reason", "")
    if not isinstance(summary, str) or not summary.strip():
        raise ReviewError("review response missing a usable summary")
    if not isinstance(title, str):
        title = ""
    if not isinstance(required, bool):
        raise ReviewError("review response missing intervention_required")
    if not isinstance(reason, str):
        reason = ""
    summary = " ".join(summary.split())[:SUMMARY_MAX]
    title = " ".join(title.split())[:TITLE_MAX]
    reason = " ".join(reason.split())[:REASON_MAX]
    return {
        "summary": summary,
        "title": title,
        # A clean review CLEARS the badge and its reason (#356): reason only survives
        # alongside required=True.
        "intervention_required": required,
        "reason": reason if required else "",
    }


def _recap_shape_guard(obj: dict) -> str:
    """Server-owned guard for the recap response (#481): the model output is DATA. Requires a
    non-empty ``recap`` string; collapses intra-line whitespace but KEEPS newlines (the recap
    is a short newline-separated timeline), strips any leading bullet / number the model added
    despite the prompt (#744 — the client's ``<ol>`` owns the ordinal), drops blank lines, and
    caps total length to ``RECAP_MAX``. A missing / empty recap raises ``ReviewError``
    (drop → keep the last good value)."""
    recap = obj.get("recap")
    if not isinstance(recap, str) or not recap.strip():
        raise ReviewError("recap response missing usable text")
    # Order matters: collapsing whitespace FIRST is what makes the strip safe. The marker pattern
    # requires a space after the glyph, so once a line is collapsed a bare "- " has become "-"
    # and no longer matches — the strip can therefore never empty a line that had content, and
    # never blanks a previously good recap.
    lines = [_RECAP_LEADING_MARK.sub("", " ".join(ln.split())) for ln in recap.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    return cleaned[:RECAP_MAX]


# --- the review ----------------------------------------------------------------------


async def run_review(key: str, aliases: dict[str, str] | None = None) -> dict:
    """Review one session now: assemble input, call the endpoint, guard the shape,
    persist via ``metadata.patch``. Returns the persisted fields (plus the fingerprint).
    Raises NotConfiguredError / ReviewError — never partial writes.

    ``aliases`` (``metadata.load_aliases()``) is threaded in by the sweep so the sidecar is read
    once per pass rather than once per session; omit it and each gather reads it itself."""
    cfg = _require_config()
    text, fingerprint = await asyncio.to_thread(
        gather_input, key, int(cfg["max_input_chars"]), aliases
    )
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg["prompt"]},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "stream": False,
    }
    try:
        async with _client(request_timeout(cfg)) as client:
            r = await client.post(
                _base(cfg) + "/chat/completions", json=body, headers=_headers(cfg)
            )
    except httpx.HTTPError as e:
        # Never echo the exception repr — httpx errors can embed request headers.
        raise ReviewError(f"review endpoint unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ReviewError(f"review endpoint returned HTTP {r.status_code}")
    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ReviewError("review endpoint returned an unexpected response shape") from None
    result = _shape_guard(_extract_json(str(content)))
    # Write against the RESOLVED sidecar key (Hermes on PR #367): for a reconciled
    # opencode session the existing title/sticky/archive sidecar lives under the
    # placeholder physical key — patching the logical key would create a sparse
    # shadowing entry and hide that state from the list read path.
    rk = metadata.resolve_key(key)
    meta = metadata.patch(
        rk,
        ai_summary=result["summary"],
        ai_title=result["title"],
        intervention_required=result["intervention_required"],
        intervention_reason=result["reason"],
        reviewed_at=time.time(),
        review_fingerprint=fingerprint,
    )
    # Chronological recap (#481): a SECOND, INDEPENDENT pass over the whole-session transcript.
    # Best-effort by design — a recap failure leaves the last good ``ai_recap`` untouched and
    # never rolls back the summary/intervention write just persisted (and a summary failure
    # raises above, before we reach here, so the recap never blocks it). Its own fingerprint
    # skips a redundant call when the whole-session content is unchanged.
    try:
        recap_text, recap_fp = await asyncio.to_thread(
            gather_recap_input, key, recap_input_chars(cfg), aliases
        )
        if recap_fp != meta.recap_fingerprint:
            obj = await complete_json(
                [
                    {"role": "system", "content": RECAP_SYSTEM_PROMPT},
                    {"role": "user", "content": recap_text},
                ]
            )
            meta = metadata.patch(rk, ai_recap=_recap_shape_guard(obj), recap_fingerprint=recap_fp)
    except ReviewError:
        pass  # keep the last good recap; the summary/intervention write above stands
    return {
        "ai_summary": meta.ai_summary,
        "ai_title": meta.ai_title,
        "intervention_required": meta.intervention_required,
        "intervention_reason": meta.intervention_reason,
        "reviewed_at": meta.reviewed_at,
        "review_fingerprint": meta.review_fingerprint,
        "review_excluded": meta.review_excluded,
        "ai_recap": meta.ai_recap,
        "recap_fingerprint": meta.recap_fingerprint,
    }


async def complete_json(messages: list[dict], *, model: str | None = None) -> dict:
    """One bounded, non-streaming chat completion against the configured AI-review endpoint,
    returning the parsed JSON object (#424 Phase 6 — reused by the auto-sorter). Reuses the
    same client / auth / tolerant JSON extraction as ``run_review``; raises NotConfiguredError
    / ReviewError, never partial state. Shape validation is the caller's concern."""
    cfg = _require_config()
    body = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }
    try:
        async with _client(request_timeout(cfg)) as client:
            r = await client.post(
                _base(cfg) + "/chat/completions", json=body, headers=_headers(cfg)
            )
    except httpx.HTTPError as e:
        raise ReviewError(f"endpoint unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ReviewError(f"endpoint returned HTTP {r.status_code}")
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ReviewError("endpoint returned an unexpected response shape") from None
    return _extract_json(str(content))


# --- model discovery -----------------------------------------------------------------

# Bound on the gateway-error extract surfaced to Settings (#382): enough for a full
# LiteLLM/OpenAI auth message, short enough to stay a one-liner in the panel.
GATEWAY_ERROR_MAX = 300


def _gateway_error(r: httpx.Response, cfg: dict) -> str:
    """A bounded extract of the gateway's OWN error text for a failed /models probe
    (#382): Settings shows WHY save-time validation failed (e.g. LiteLLM's 401
    "Virtual Key expected…") instead of a bare status code. Tries the OpenAI-style
    JSON shapes (``error.message`` / ``error`` / ``message`` / ``detail``) before
    falling back to a plain-text snippet. The configured API key is redacted
    defensively in case a gateway echoes the Authorization header back."""
    msg = ""
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        elif isinstance(err, str):
            msg = err
        elif isinstance(payload.get("message"), str):
            msg = payload["message"]
        elif isinstance(payload.get("detail"), str):
            msg = payload["detail"]
    if not msg:
        msg = r.text
    key = str(cfg.get("api_key") or "")
    if key:
        msg = msg.replace(key, "[redacted]")
    msg = " ".join(msg.split())[:GATEWAY_ERROR_MAX]
    base = f"model listing returned HTTP {r.status_code}"
    return f"{base}: {msg}" if msg else base


def _cache_key(cfg: dict) -> str:
    # Key fingerprint, never the key itself, so the cache key is log-safe.
    digest = hashlib.sha256(str(cfg["api_key"]).encode()).hexdigest()[:16]
    return f"{_base(cfg)}|{digest}"


async def list_models(*, force: bool = False) -> list[str]:
    """Proxy ``GET {base_url}/models`` with the stored key (#356): the browser never sees
    the key (and would hit CORS anyway). Small in-memory TTL cache; ``force`` (the UI
    refresh button) bypasses it. Raises NotConfiguredError when unset, ReviewError when
    the endpoint can't serve a list (404 / error / timeout) — the caller falls back to
    free-text model entry. This call doubles as the save-time validation probe (#394):
    a non-200 carries a bounded extract of the gateway's own error text (#382) so the
    Settings panel can show WHY the endpoint/key were rejected."""
    cfg = _require_config()
    ck = _cache_key(cfg)
    now = time.monotonic()
    if not force:
        hit = _models_cache.get(ck)
        if hit and now - hit[0] < MODELS_CACHE_TTL_S:
            return hit[1]
    try:
        async with _client(MODELS_TIMEOUT_S) as client:
            r = await client.get(_base(cfg) + "/models", headers=_headers(cfg))
    except httpx.HTTPError as e:
        raise ReviewError(f"model listing unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ReviewError(_gateway_error(r, cfg))
    try:
        payload = r.json()
    except ValueError:
        raise ReviewError("model listing returned invalid JSON") from None
    raw = payload.get("data") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict):
        raw = payload.get("models")
    if not isinstance(raw, list):
        raise ReviewError("model listing returned an unexpected shape")
    models: list[str] = []
    for item in raw:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    models = sorted(set(models))
    _models_cache[ck] = (now, models)
    return models
