"""Cross-engine session handoff (#597, Phases 1–2).

Hands an open session's context to a *new* session in another engine. Three cooperating
pieces, all server-owned so seed text never travels through URLs, WebSocket query params,
or argv (the transport contract shares the shell-free guarantee's rationale):

- **Seed builders** — an engine-neutral markdown handoff document built from the source
  session's parsed transcript (the same per-engine adapters the scroll-up renderer uses):
  *Quick* is the last-N-turns tail, built locally and sent nowhere; *AI* (Phase 2) asks the
  **already-configured AI-review endpoint** for a structured state/open-items/next-steps
  brief and renders it into the same doc shape (the model's output is DATA — server-owned
  shape guard + caps, exactly like ``review._shape_guard``). An unconfigured/failing
  endpoint DEGRADES to Quick with a visible notice rather than failing the handoff.
- **Handle store** — ``prepare`` mints an opaque, short-TTL handle referencing the seed
  (stored here, server-side only); ``commit`` binds the handle to a freshly minted target
  session id; the ws launch path *redeems* the seed atomically at injection time. A handle
  is single-redemption: a WS reconnect (or a second viewer) finds it already consumed and
  simply launches unseeded — never a double paste.
- **Provenance state machine** — sidecar provenance (``handoff_from``/``handoff_to``) is
  written only after the spawn passes the same aliveness gate the picker-start flow uses
  (master up past the instant-exit window). For mint-their-own-id engines the source
  backlink waits for placeholder→real reconciliation and inherits its fail-safe: an
  ambiguous/timed-out reconcile leaves the backlink absent rather than wrong. Every
  transition is idempotent under one lock, so reconnect/attach paths can never replay one.

The seed reaches the target CLI as terminal input (a bracketed paste written server-side
to the session's PTY — the process's stdin), never argv: see ``webterm.run``'s injector.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import metadata, transcript

# --- tunables (env-overridable like the transcript/scrollback knobs) -------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# How many trailing user/assistant text turns the Quick seed carries.
SEED_MAX_TURNS = max(1, _env_int("AGENT_SESSIONS_HANDOFF_TURNS", 6))
# Hard byte cap on the seed document (long transcripts must never blow the target's first
# prompt); oldest turns drop first, and the document is truncated to fit. Floored at
# ``MIN_CAP_BYTES``: the env knob is an operator tuning dial, and a 0/negative/absurd value
# would configure a handoff that can carry no handoff (#703 review round 3). ``_cap`` is
# still correct for ANY non-negative cap — the floor stops a nonsense deployment, the
# function keeps the invariant.
MIN_CAP_BYTES = 256
SEED_CAP_BYTES = max(MIN_CAP_BYTES, _env_int("AGENT_SESSIONS_HANDOFF_CAP_BYTES", 8192))
# Handle lifetime. Refreshed at commit so a committed handoff has the full window again to
# reach its ws launch; an abandoned preview simply expires (nothing was spawned).
HANDLE_TTL_S = float(_env_int("AGENT_SESSIONS_HANDOFF_TTL_S", 600))
# AI mode (#597 Phase 2). How much of the source transcript's tail is offered to the
# endpoint, and the caps applied to what it returns. The model's output is DATA: every
# field is length-capped and re-rendered by us — the endpoint never authors the document.
AI_INPUT_CHARS = _env_int("AGENT_SESSIONS_HANDOFF_AI_INPUT_CHARS", 24000)
AI_STATE_MAX = 800
AI_ITEM_MAX = 200
AI_ITEMS_MAX = 8

AI_SYSTEM_PROMPT = (
    "You write handoff briefs between AI coding-agent sessions. You are given the tail of a "
    "transcript from one agent session. Summarize it so a DIFFERENT agent, with no other "
    "context, can take the work over.\n"
    "Reply with ONLY a JSON object of this exact shape:\n"
    '{"state": "<what has been done so far and where the work stands, 2-5 sentences>", '
    '"open_items": ["<unresolved item>", ...], '
    '"next_steps": ["<concrete next action>", ...]}\n'
    "Be concrete and factual: name files, commands, errors, and decisions from the transcript. "
    "Never invent work that is not in the transcript. Use at most 8 items per list; use an "
    "empty list when there are none."
)


class HandoffError(RuntimeError):
    """A handoff request the server refuses. ``status`` maps to the HTTP response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --- capability -------------------------------------------------------------------------------

# Reasons shown on disabled engine tiles AND used for the server-side rejection — one source,
# so the UI can never claim support the server would refuse (issue #597 guard).
_REASON_NO_SEED = "no seed-capable start yet"
_REASON_NOT_AGENT = "not an agent engine"
_REASON_NOT_INSTALLED = "not installed"


def seed_start_state(prov, *, present: bool) -> tuple[bool, str | None]:
    """``(supported, reason)`` for ``prov`` as a handoff *target*. ``reason`` is ``None``
    exactly when supported. The single capability source for /api/engines and the routes."""
    if getattr(prov, "engine_id", "") == "shell":
        return False, _REASON_NOT_AGENT
    if not getattr(prov, "supports_seed_start", False):
        return False, _REASON_NO_SEED
    if not present:
        return False, _REASON_NOT_INSTALLED
    return True, None


# --- quick seed builder -------------------------------------------------------------------


# Strip control bytes (keep \n and \t) from transcript-derived text. This is a security
# boundary, not cosmetics: the seed is delivered as a bracketed paste, and an ESC embedded in
# transcript content could otherwise terminate the paste early (`ESC [ 201 ~`) and smuggle
# raw key input into the target agent.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(text: str) -> str:
    return _CTRL_RE.sub("", text)


def _source_texts(engine: str, native: str) -> list[tuple[str, str]]:
    """The source session's user/assistant text turns as engine-neutral ``(role, text)``
    pairs, control-bytes stripped. Empty when the transcript can't be read/parsed — the
    callers turn that into the 409 "nothing to hand off"."""
    adapter = transcript.adapter_for(engine)
    turns: list[transcript.Turn] = []
    if adapter is not None:
        try:
            turns = adapter(native, Path.home())
        except Exception:
            turns = []
    return [
        (("user" if t.role == "user" else "agent"), _clean(t.text).strip())
        for t in turns
        if t.kind == "text" and t.role in ("user", "assistant") and t.text.strip()
    ]


# The header's task line is a one-line label, not content: `first_user_message` can be
# arbitrarily long, and an unbounded one crowds the actual handoff out of the cap (it made
# `_cap` return a document that was ALL title — #703 review round 2).
HEAD_TITLE_MAX = 200


def resolve_source_location(engine: str, native: str, *, include: bool) -> str | None:
    """Where the source session keeps its full transcript, or ``None`` (#716).

    Resolved ONCE per document build and passed into the header: the Quick builder re-renders
    its header while trimming turns, and a filesystem glob / sqlite probe per iteration would be
    pure waste. ``None`` whenever the option is off OR the locator can't resolve *this* session,
    and the header then omits the line entirely — never a guessed location.

    AI mode must call this only AFTER its summarization request returns, so the location is
    never part of the payload sent to the review endpoint (only the target engine sees it).
    """
    if not include:
        return None
    return transcript.source_location(engine, native, Path.home())


def _head_lines(
    engine: str, native: str, title: str, cwd: str, *, transcript_loc: str | None = None
) -> list[str]:
    """The shared handoff-document header — identical for Quick and AI, so a target agent
    reads the same provenance framing either way. The title is bounded; `_cap` is the
    backstop for the document as a whole.

    ``- session:`` is ALWAYS present — the engine-qualified id is pure provenance and costs the
    receiving agent nothing. ``- transcript:`` is opt-in (#716), because *following* it is what
    spends tokens, and it appears only when the locator resolved this exact session.
    """
    head = [
        f"# Handoff — continued from a {engine} session",
        "",
        "You are taking over an in-progress task from another agent session.",
        "Read the brief below, then continue the work.",
        "",
        "## Source",
        f"- engine: {engine}",
        f"- session: {engine}:{_clean(native)}",
    ]
    if title:
        one_line = " ".join(_clean(title).split())[:HEAD_TITLE_MAX]
        head.append(f"- task: {one_line}")
    if cwd:
        head.append(f"- workdir: {_clean(cwd)}")
    if transcript_loc:
        head += [
            f"- transcript: {_clean(transcript_loc)}",
            "",
            "The brief below is capped. The full transcript is at the location above — read it"
            " only if you need more context than the brief gives you.",
        ]
    return head


def build_quick_seed(
    engine: str,
    native: str,
    *,
    title: str = "",
    cwd: str = "",
    include_source_ref: bool = False,
) -> tuple[str, dict]:
    """The Quick (last-N-turns) handoff document + its meta, from the source session's
    parsed transcript. Engine-neutral ``[user]``/``[agent]`` labels; hard byte cap.

    Raises ``HandoffError(409)`` when the transcript yields no usable turns (a brand-new
    or unreadable session has nothing to hand off).
    """
    texts = _source_texts(engine, native)
    if not texts:
        raise HandoffError(409, "source transcript is empty — nothing to hand off")
    tail = texts[-SEED_MAX_TURNS:]
    # Resolved once — `_doc` re-renders the header on every trim iteration below.
    loc = resolve_source_location(engine, native, include=include_source_ref)

    def _doc(rows: list[tuple[str, str]]) -> str:
        head = _head_lines(engine, native, title, cwd, transcript_loc=loc) + [
            "",
            "## Recent turns",
            "",
        ]
        body = [f"[{role}] {text}" for role, text in rows]
        return "\n".join(head + body) + "\n"

    doc = _doc(tail)
    # Drop OLDEST turns first — the tail is what a taking-over agent needs most.
    while len(doc.encode("utf-8")) > SEED_CAP_BYTES and len(tail) > 1:
        tail = tail[1:]
        doc = _doc(tail)
    # …then hand the result to the ONE capper. This used to be a bespoke truncation whose
    # arithmetic overshot (`max(200, …)` plus an appended " …"), and which never bounded the
    # HEADER at all — an oversized first_user_message title produced a 20 KB "capped" doc
    # (PR #703 review round 2). Every generated document now exits through `_cap`, so the
    # advertised cap is the real one whatever the input shape.
    doc = _cap(doc)
    meta = {
        "mode": "quick",
        "turns": len(tail),
        "bytes": len(doc.encode("utf-8")),
        "cap": SEED_CAP_BYTES,
    }
    return doc, meta


def _ai_input(texts: list[tuple[str, str]]) -> str:
    """The transcript tail offered to the endpoint, budgeted by characters (oldest turns
    drop first). Same engine-neutral labelling as the Quick doc.

    A single newest turn LARGER than the whole budget is truncated rather than admitted
    whole (PR #703 review): dropping it would send nothing, but letting it through blew
    the budget it exists to enforce. The final slice makes the cap unconditional — the
    per-line accounting excludes the join's separators, so it alone is not a guarantee.
    """
    rows: list[str] = []
    total = 0
    for role, text in reversed(texts):
        line = f"[{role}] {text}"
        if total + len(line) > AI_INPUT_CHARS:
            if rows:
                break
            line = line[:AI_INPUT_CHARS]  # singleton oversized turn → truncate, don't skip
            rows.append(line)
            break
        rows.append(line)
        total += len(line)
    return "\n".join(reversed(rows))[:AI_INPUT_CHARS]


def _ai_shape_guard(obj: dict) -> tuple[str, list[str], list[str]]:
    """Server-owned shape guard for the AI brief — the model's output is DATA (same
    discipline as ``review._shape_guard``): required non-empty ``state``, list fields
    coerced + item/count capped, everything whitespace-collapsed and control-stripped.
    A missing/garbage ``state`` raises so the caller degrades to Quick rather than
    seeding the target with junk."""
    state = obj.get("state")
    if not isinstance(state, str) or not state.strip():
        raise HandoffError(502, "AI handoff response missing a usable state")
    state = _clean(" ".join(state.split()))[:AI_STATE_MAX]

    def _items(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for item in value[:AI_ITEMS_MAX]:
            if isinstance(item, str) and item.strip():
                out.append(_clean(" ".join(item.split()))[:AI_ITEM_MAX])
        return out

    return state, _items(obj.get("open_items")), _items(obj.get("next_steps"))


async def build_ai_seed(
    engine: str,
    native: str,
    *,
    title: str = "",
    cwd: str = "",
    include_source_ref: bool = False,
) -> tuple[str, dict]:
    """The AI-summarized handoff document + its meta (#597 Phase 2), via the
    already-configured AI-review endpoint (no new endpoint — the issue's constraint).

    Raises ``HandoffError(409)`` for an empty source transcript (same as Quick), and
    ``review.NotConfiguredError`` / ``review.ReviewError`` / ``HandoffError(502)`` when
    the endpoint is absent or its answer is unusable — the route turns those into the
    documented degrade-to-Quick-with-a-notice path.
    """
    from . import review  # late import: review pulls httpx + prefs; keep handoff import-light

    texts = await asyncio.to_thread(_source_texts, engine, native)
    if not texts:
        raise HandoffError(409, "source transcript is empty — nothing to hand off")
    obj = await review.complete_json(
        [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": _ai_input(texts)},
        ]
    )
    state, open_items, next_steps = _ai_shape_guard(obj)
    # AFTER the endpoint call, deliberately (#716): the transcript location must never appear in
    # the payload sent to the review endpoint — only the target engine is meant to receive it.
    loc = resolve_source_location(engine, native, include=include_source_ref)
    lines = _head_lines(engine, native, title, cwd, transcript_loc=loc) + [
        "",
        "## State",
        "",
        state,
    ]
    if open_items:
        lines += ["", "## Open items", ""] + [f"- {i}" for i in open_items]
    if next_steps:
        lines += ["", "## Next steps", ""] + [f"- {i}" for i in next_steps]
    doc = "\n".join(lines) + "\n"
    doc = _cap(doc)
    return doc, {
        "mode": "ai",
        "turns": len(texts),
        "bytes": len(doc.encode("utf-8")),
        "cap": SEED_CAP_BYTES,
    }


# Appended when a rendered document is truncated. Its bytes are RESERVED out of the cap
# before slicing (PR #703 review): appending it after slicing to the full cap overshot the
# very limit the function documents.
_CAP_MARKER = "\n…\n"


def _cap(doc: str) -> str:
    """Byte-cap a rendered document. The result is ALWAYS <= ``SEED_CAP_BYTES`` bytes, for
    EVERY non-negative cap: the truncation marker is reserved before slicing, and dropped
    entirely when the cap is too small to hold it (it is a courtesy, never a reason to
    exceed the limit — #703 review round 3: a cap below the marker's 5 bytes still emitted
    the 5-byte marker). The slice decodes with ``errors="ignore"`` so a multibyte character
    split by the cut is dropped rather than mojibaked."""
    cap = max(0, SEED_CAP_BYTES)
    raw = doc.encode("utf-8")
    if len(raw) <= cap:
        return doc
    marker = _CAP_MARKER.encode("utf-8")
    if cap <= len(marker):
        # No room for both content and the marker — content wins; the cap is absolute.
        return raw[:cap].decode("utf-8", "ignore")
    return raw[: cap - len(marker)].decode("utf-8", "ignore").rstrip() + _CAP_MARKER


def sanitize_seed(text: str) -> str:
    """Normalize a CLIENT-SUPPLIED seed (the Phase-2 editable preview) to exactly the
    guarantees the builders provide: control bytes stripped (the bracketed-paste breakout
    guard — an ESC could otherwise end the paste early and smuggle key input) and the hard
    byte cap enforced.

    Over-cap text is REJECTED, not truncated (PR #703 review): silently shortening
    user-authored prose and reporting success would hand the target a brief the author
    never wrote. The builders truncate their own generated output (``_cap``) because
    there is no author to tell; here there is. Raises ``HandoffError(422)`` on empty or
    over-cap input — ``meta.cap`` tells the client the limit up front.
    """
    cleaned = _clean(text).strip()
    if not cleaned:
        raise HandoffError(422, "handoff seed cannot be empty")
    # The seed is stored EXACTLY as validated — no trailing newline is appended. Appending
    # one made a brief whose visible size equalled ``meta.cap`` weigh cap+1 server-side, so
    # the modal enabled a handoff the server then rejected (#703 review round 2). The only
    # transforms left are strip-controls and trim, and both SHRINK: a client counting its
    # raw textarea bytes against ``meta.cap`` can therefore never invite a 422. The paste is
    # submitted by the delivery's own CR, so the newline was cosmetic anyway.
    size = len(cleaned.encode("utf-8"))
    if size > SEED_CAP_BYTES:
        raise HandoffError(422, f"handoff seed is too large: {size} bytes (cap {SEED_CAP_BYTES})")
    return cleaned


# --- handle store + provenance state machine ------------------------------------------------


@dataclass
class _Handoff:
    handle: str
    source_key: str
    target_engine: str
    mode: str
    cwd: str
    seed: str | None
    created_at: float = field(default_factory=time.monotonic)
    target_key: str | None = None  # set at commit (engine-qualified physical key)
    # A delivery claim is outstanding (claim/ack protocol — PR #701 review round 2): the
    # seed is consumed only on a delivered/aborted ACK, never at claim time, so a failed
    # delivery can release the claim and leave the seed intact for the next attach.
    seed_claimed: bool = False
    spawned: bool = False  # aliveness gate passed (master alive past the instant-exit window)
    watch_armed: bool = False  # a spawn-watch task exists (never arm two)
    real_target_key: str | None = None  # reconciled real id (mint-own-id engines)
    # Publication flags are set ONLY after their sidecar patch succeeded (round-2 P2): a
    # failed metadata write leaves the flag clear, so a later transition retries it instead
    # of stranding one-sided provenance forever.
    target_published: bool = False  # target's handoff_from/mode/at written
    backlink_published: bool = False  # source's handoff_to written


_lock = threading.Lock()
_HANDLES: dict[str, _Handoff] = {}
_BY_TARGET: dict[str, str] = {}  # target phys key → handle


def _entry_locked(target_key: str) -> _Handoff | None:
    handle = _BY_TARGET.get(target_key)
    return _HANDLES.get(handle) if handle else None


def _release_if_done_locked(h: _Handoff) -> None:
    """Drop the entry once EVERY concern is settled: seed consumed (delivered or aborted),
    spawn seen, and both provenance writes durable. Provenance publication must never evict
    a still-unredeemed seed (PR #701 review P1): the aliveness gate fires at ~8 s while the
    PTY injector may legitimately wait tens of seconds for the TUI to arm bracketed paste —
    and on an injector timeout the unconsumed seed must survive for the next attach. The
    TTL sweep stays the backstop for entries that never fully settle."""
    if (
        h.seed is None
        and h.spawned
        and h.target_published
        and h.backlink_published
        and h.target_key
    ):
        _HANDLES.pop(h.handle, None)
        _BY_TARGET.pop(h.target_key, None)


def _sweep_locked() -> None:
    now = time.monotonic()
    for handle, h in list(_HANDLES.items()):
        if now - h.created_at > HANDLE_TTL_S:
            _HANDLES.pop(handle, None)
            if h.target_key:
                _BY_TARGET.pop(h.target_key, None)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_handle(source_key: str, target_engine: str, mode: str, seed: str, *, cwd: str) -> str:
    """Store a prepared handoff; returns the opaque handle. Nothing is spawned or persisted
    — an abandoned preview just expires."""
    handle = secrets.token_urlsafe(24)
    with _lock:
        _sweep_locked()
        _HANDLES[handle] = _Handoff(
            handle=handle,
            source_key=source_key,
            target_engine=target_engine,
            mode=mode,
            cwd=cwd,
            seed=seed,
        )
    return handle


def commit(handle: str, seed: str | None = None) -> dict:
    """Bind ``handle`` to a freshly minted target session id (the client then navigates to
    the normal ``/s/:engine/:id`` launch route, which redeems the seed at spawn time).

    ``seed`` (#597 Phase 2 — the editable preview) replaces the prepared text with the
    user's edit. It is re-sanitized here through the SAME guarantees the builders give
    (control-strip + byte cap): a client-supplied seed is untrusted input, and this is the
    only place it can enter the store. Omit it to commit the prepared text unchanged.

    The id shape follows the engine's launch model, exactly like the picker's new-session
    flow: engines that mint their own id (codex/opencode) get a ``new-<uuid>`` placeholder
    and reconcile; pinned-id engines (claude) get the final uuid up front.
    """
    from . import engines  # late import: engines never imports handoff, but keep startup lean

    edited = sanitize_seed(seed) if seed is not None else None
    with _lock:
        _sweep_locked()
        h = _HANDLES.get(handle)
        if h is None:
            raise HandoffError(404, "unknown or expired handoff handle")
        if h.target_key is not None:
            raise HandoffError(409, "handoff already committed")
        if edited is not None:
            h.seed = edited
            h.mode = f"{h.mode}+edited" if not h.mode.endswith("+edited") else h.mode
        prov = engines.get(h.target_engine)
        if prov is None:  # provider vanished since prepare — fail closed
            raise HandoffError(404, "unknown engine")
        mint_placeholder = bool(getattr(prov, "new_session_reconciles", False))
        native = f"new-{uuid.uuid4()}" if mint_placeholder else str(uuid.uuid4())
        target_key = f"{h.target_engine}:{native}"
        h.target_key = target_key
        h.created_at = time.monotonic()  # full TTL again to reach the ws launch
        _BY_TARGET[target_key] = handle
        return {"id": target_key, "engine": h.target_engine, "native": native, "cwd": h.cwd}


def has_pending_seed(target_key: str) -> bool:
    """True while a committed-but-unredeemed seed exists for ``target_key`` (cheap check the
    ws route uses to decide whether to hand ``webterm.run`` a seed source)."""
    with _lock:
        _sweep_locked()
        handle = _BY_TARGET.get(target_key)
        h = _HANDLES.get(handle) if handle else None
        return bool(h and h.seed is not None)


def claim_seed(target_key: str) -> str | None:
    """Claim the seed for delivery WITHOUT consuming it (claim/ack — review round 2).
    Claimants are serialized: while a claim is outstanding every other caller gets ``None``
    (so two viewers can never double-write), and the seed is consumed only by
    ``ack_seed(..., "delivered")`` / ``"abort"`` — a failed delivery releases the claim
    with ``"retry"`` and the seed stays pending for the next attach."""
    with _lock:
        _sweep_locked()
        h = _entry_locked(target_key)
        if h is None or h.seed is None or h.seed_claimed:
            return None
        h.seed_claimed = True
        return h.seed


def ack_seed(target_key: str, outcome: str) -> None:
    """Settle an outstanding claim. ``outcome``:
    - ``"delivered"`` — the FULL paste+CR reached the PTY: consume the seed (the
      single-delivery guarantee) and release the entry if everything else is settled.
    - ``"retry"`` — nothing was written: release the claim, seed stays pending.
    - ``"abort"`` — a PARTIAL write reached the PTY: consume the seed without retry (an
      unterminated bracketed paste already polluted the input; a blind replay would
      corrupt the prompt — the caller logs this explicitly).
    """
    with _lock:
        h = _entry_locked(target_key)
        if h is None:
            return
        h.seed_claimed = False
        if outcome in ("delivered", "abort"):
            h.seed = None
            _release_if_done_locked(h)


def arm_watch(target_key: str) -> bool:
    """Claim the right to run the one spawn-watch task for ``target_key``. Idempotent: only
    the first LAUNCH connection gets ``True``; reconnects never arm a second watch."""
    with _lock:
        _sweep_locked()
        handle = _BY_TARGET.get(target_key)
        h = _HANDLES.get(handle) if handle else None
        if h is None or h.watch_armed:
            return False
        h.watch_armed = True
        return True


def mark_spawned(target_key: str) -> None:
    """The aliveness gate passed (master alive beyond the instant-exit window): publish
    whatever provenance is still unpublished. Retryable, not one-shot (review round 2 P2):
    each publication flag is set only after its sidecar patch succeeded, so a failed write
    raises to the caller (the spawn watch retries) and a later call performs exactly the
    missing writes. Publishing NEVER evicts an unredeemed seed (round-1 P1); a mint-own-id
    target's backlink additionally waits for ``note_reconciled``'s real id."""
    with _lock:
        h = _entry_locked(target_key)
        if h is None:
            return
        h.spawned = True
    _publish(target_key)


def _publish(target_key: str) -> None:
    """Perform whichever provenance writes are still unpublished, marking each flag only
    AFTER its ``metadata.patch`` succeeded. Raises on a failed write — the state stays
    retryable and the entry is retained until every required write is durable (or TTL)."""
    with _lock:
        h = _entry_locked(target_key)
        if h is None or not h.spawned:
            return
        is_placeholder = target_key.partition(":")[2].startswith("new-")
        backlink = h.real_target_key or (None if is_placeholder else target_key)
        need_target = not h.target_published
        need_backlink = backlink is not None and not h.backlink_published
        source_key, mode = h.source_key, h.mode
        if not (need_target or need_backlink):
            _release_if_done_locked(h)
            return
    at = _now_iso()
    if need_target:
        metadata.patch(target_key, handoff_from=source_key, handoff_mode=mode, handoff_at=at)
        with _lock:
            h2 = _entry_locked(target_key)
            if h2 is not None:
                h2.target_published = True
    if need_backlink:
        metadata.patch(metadata.resolve_key(source_key), handoff_to=backlink, handoff_at=at)
        with _lock:
            h2 = _entry_locked(target_key)
            if h2 is not None:
                h2.backlink_published = True
    with _lock:
        h2 = _entry_locked(target_key)
        if h2 is not None:
            _release_if_done_locked(h2)


def abort_spawn(target_key: str) -> None:
    """The launch died inside the instant-exit window: drop the handoff without any sidecar
    write — a failed spawn must never leave a dangling link (issue #597 acceptance)."""
    with _lock:
        handle = _BY_TARGET.pop(target_key, None)
        if handle:
            _HANDLES.pop(handle, None)


def note_reconciled(placeholder_key: str, real_key: str) -> None:
    """Placeholder→real reconciliation resolved the target's real id: write the source's
    backlink to the REAL key (never the placeholder). Called after the alias is durable.
    If the spawn-watch hasn't fired yet, the real key is parked for ``mark_spawned``."""
    with _lock:
        h = _entry_locked(placeholder_key)
        if h is None:
            return
        h.real_target_key = real_key
        spawned = h.spawned
    if spawned:
        # Retryable publication (round-2 P2): this also re-attempts a target write that
        # failed earlier — flags gate exactly the missing patches.
        _publish(placeholder_key)


def reset_for_tests() -> None:
    """Drop all in-memory handoff state (test isolation)."""
    with _lock:
        _HANDLES.clear()
        _BY_TARGET.clear()
