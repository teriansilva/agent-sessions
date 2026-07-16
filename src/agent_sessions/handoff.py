"""Cross-engine session handoff (#597, Phase 1 — Quick mode).

Hands an open session's context to a *new* session in another engine. Three cooperating
pieces, all server-owned so seed text never travels through URLs, WebSocket query params,
or argv (the transport contract shares the shell-free guarantee's rationale):

- **Quick seed builder** — an engine-neutral markdown handoff document built from the tail
  of the source session's parsed transcript (the same per-engine adapters the scroll-up
  renderer uses). Local only; sent nowhere.
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
SEED_MAX_TURNS = _env_int("AGENT_SESSIONS_HANDOFF_TURNS", 6)
# Hard byte cap on the seed document (long transcripts must never blow the target's first
# prompt); oldest turns drop first, and a single oversized turn is truncated.
SEED_CAP_BYTES = _env_int("AGENT_SESSIONS_HANDOFF_CAP_BYTES", 8192)
# Handle lifetime. Refreshed at commit so a committed handoff has the full window again to
# reach its ws launch; an abandoned preview simply expires (nothing was spawned).
HANDLE_TTL_S = float(_env_int("AGENT_SESSIONS_HANDOFF_TTL_S", 600))


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


def build_quick_seed(
    engine: str, native: str, *, title: str = "", cwd: str = ""
) -> tuple[str, dict]:
    """The Quick (last-N-turns) handoff document + its meta, from the source session's
    parsed transcript. Engine-neutral ``[user]``/``[agent]`` labels; hard byte cap.

    Raises ``HandoffError(409)`` when the transcript yields no usable turns (a brand-new
    or unreadable session has nothing to hand off).
    """
    adapter = transcript.adapter_for(engine)
    turns: list[transcript.Turn] = []
    if adapter is not None:
        try:
            turns = adapter(native, Path.home())
        except Exception:
            turns = []
    texts = [
        (("user" if t.role == "user" else "agent"), _clean(t.text).strip())
        for t in turns
        if t.kind == "text" and t.role in ("user", "assistant") and t.text.strip()
    ]
    if not texts:
        raise HandoffError(409, "source transcript is empty — nothing to hand off")
    tail = texts[-SEED_MAX_TURNS:]

    def _doc(rows: list[tuple[str, str]]) -> str:
        head = [
            f"# Handoff — continued from a {engine} session",
            "",
            "You are taking over an in-progress task from another agent session.",
            "Read the recent turns below, then continue the work.",
            "",
            "## Source",
            f"- engine: {engine}",
        ]
        if title:
            head.append(f"- task: {_clean(title)}")
        if cwd:
            head.append(f"- workdir: {_clean(cwd)}")
        head += ["", "## Recent turns", ""]
        body = [f"[{role}] {text}" for role, text in rows]
        return "\n".join(head + body) + "\n"

    doc = _doc(tail)
    # Cap: drop oldest turns first; if even one turn overflows, truncate its text.
    while len(doc.encode("utf-8")) > SEED_CAP_BYTES and len(tail) > 1:
        tail = tail[1:]
        doc = _doc(tail)
    if len(doc.encode("utf-8")) > SEED_CAP_BYTES:
        role, text = tail[0]
        overhead = len(_doc([(role, "")]).encode("utf-8"))
        keep = max(200, SEED_CAP_BYTES - overhead)
        doc = _doc([(role, text.encode("utf-8")[:keep].decode("utf-8", "ignore") + " …")])
    meta = {
        "mode": "quick",
        "turns": len(tail),
        "bytes": len(doc.encode("utf-8")),
        "cap": SEED_CAP_BYTES,
    }
    return doc, meta


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


def commit(handle: str) -> dict:
    """Bind ``handle`` to a freshly minted target session id (the client then navigates to
    the normal ``/s/:engine/:id`` launch route, which redeems the seed at spawn time).

    The id shape follows the engine's launch model, exactly like the picker's new-session
    flow: engines that mint their own id (codex/opencode) get a ``new-<uuid>`` placeholder
    and reconcile; pinned-id engines (claude) get the final uuid up front.
    """
    from . import engines  # late import: engines never imports handoff, but keep startup lean

    with _lock:
        _sweep_locked()
        h = _HANDLES.get(handle)
        if h is None:
            raise HandoffError(404, "unknown or expired handoff handle")
        if h.target_key is not None:
            raise HandoffError(409, "handoff already committed")
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
