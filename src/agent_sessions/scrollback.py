"""Per-session scrollback: the capped in-memory ring, its on-disk mirror, and the
(re)connect resume / scroll-up decisions (split out of ``webterm.py`` in #265 S2).

dtach has no scrollback of its own, so on reattach an inline agent (claude/codex/gemini)
only repaints its near-empty current frame, not the history — the terminal looks empty.
This module keeps a capped ring of recent PTY output per session, mirrors it to disk so it
survives an app restart (#206), and decides what to (re)send on connect: a byte-delta for a
same-width continuation, or the engine's saved transcript / a clean-load clear otherwise
(#242/#262). ``webterm.run`` (the WS↔PTY pump) drives this module and re-exports its public
names, so ``webterm.<name>`` keeps working for callers and tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

from . import perfstats, ptybridge, session_input, transcript, vtscreen

log = logging.getLogger("agent_sessions.scrollback")

# `_TOTALS[key]` is a monotonic count of *all* bytes ever sent for the key (not just
# what's still in the ring). It powers delta-resume: a reconnecting client reports the
# absolute offset it last saw (`?have=`), and we stream only the bytes since then — so
# a transient ws drop continues seamlessly instead of re-replaying the whole ring (or,
# worse, blanking). See docs/session-handling.md §Reconnect continuity.
# Per-session scrollback ring cap. This is a CEILING, not an up-front allocation: the
# buffer is a bytearray that grows with actual output and is then trimmed to this size,
# so an idle/quiet session costs only what it produced. Bumped 256KB → 4MB (#204), then
# 4MB → 8MB: 4MB still lost live scroll-up quickly on chatty agents. Resident memory scales
# with the number of LIVE sessions (each up to `_MAX_BUF`): `_MAX_BUFFERS` below caps only the
# retained *inactive/dead* buffers, not live ones (`_enforce_buffer_cap` keeps every live
# buffer), so concurrent-live is the real driver, not that count. The per-session footprint is
# the session's *recent* output, well under the cap for most. Operators on a tight VM (or wanting
# deeper history) can override via AGENT_SESSIONS_SCROLLBACK_BYTES; the floor keeps a malformed
# value from shrinking the ring below a few screens.


def _int_env(name: str, default: int) -> int:
    """A positive int from the environment, or ``default`` on absent/malformed/non-positive."""
    try:
        v = int(os.environ.get(name) or 0)
    except (ValueError, TypeError):
        return default
    return v if v > 0 else default


def _scrollback_bytes() -> int:
    try:
        v = int(os.environ.get("AGENT_SESSIONS_SCROLLBACK_BYTES") or 0)
    except (ValueError, TypeError):
        v = 0
    return v if v >= 256 * 1024 else 8 * 1024 * 1024


_MAX_BUF = _scrollback_bytes()

# Hard cap on how many *distinct* session buffers we retain at once. Each entry is
# capped at `_MAX_BUF`, but without a ceiling on the *count* every session that ever
# attached would leave up to `_MAX_BUF` resident for the process lifetime (audit MEDIUM:
# unbounded growth over many sessions). The buffers are insertion-/access-ordered
# (`OrderedDict`, move-to-end on touch); when over the cap we evict the
# least-recently-used entry. A still-attached/alive session is touched on every
# output chunk, so it stays at the hot end and survives eviction — delta-resume for
# live sessions is preserved. Dead sessions also get dropped eagerly at end-of-run
# (see `_maybe_evict_ended`), so the LRU cap is a worst-case backstop, not the
# primary reclaim path.
_MAX_BUFFERS = 64
_BUFFERS: OrderedDict[str, bytearray] = OrderedDict()
# Serialises STRUCTURAL access to the `_BUFFERS` registry — inserts, pops, `move_to_end`,
# and any *iteration* over it. The event loop is the normal writer (the WS pump + the
# headless SessionStream drain both land in `_buffer_append`), but the AI-review path reads
# the live tail from a WORKER THREAD (`asyncio.to_thread` → `live_tail_text` → `_ensure_loaded`),
# which can hydrate-and-insert a buffer concurrently with the loop's `_enforce_buffer_cap` scan.
# Unserialised, that races into `RuntimeError('OrderedDict mutated during iteration')` mid-scan —
# which used to propagate out of `_buffer_append`, collapse the byte pump, and disconnect the
# viewer (a "random" black-then-reconnect). In-place `bytearray.extend` of an *existing* ring
# value is deliberately NOT held under this lock (it doesn't change the dict's shape, and readers
# take an atomic slice copy); only the registry's structure is guarded. Re-entrant so the
# `_buffer_append → _enforce_buffer_cap → _drop_buffer` chain on one thread never self-deadlocks.
_RING_LOCK = threading.RLock()
_TOTALS: OrderedDict[str, int] = OrderedDict()
# Per-key width (cols) the buffer's bytes were last written at — i.e. the agent's current pty
# width. Used to decide clean-load vs replay on a fresh load (#244): replay only when the
# attaching client matches this width (no garble), else clear. Best-effort, in-memory.
_LAST_COLS: dict[str, int] = {}
# Per-key height (rows) the agent's pty is at — companion to `_LAST_COLS`, tracked so the VT live
# mirror (#273) can size its emulator to the agent's full geometry (height matters: Ink's cursor-up
# repaints must overwrite within the same screen height or they duplicate). Best-effort, in-memory.
_LAST_ROWS: dict[str, int] = {}
# Per-key wall-clock of the last byte we observed flowing from the agent (#156). Powers the
# "agent working" indicator (#156). Stamped from the byte-ingest path; the #183
# SessionStream keeps it fresh even with no browser attached. Best-effort and bounded by
# the same LRU as the buffers.
_LAST_OUTPUT_AT: OrderedDict[str, float] = OrderedDict()

# Post-attach replay grace (#195). A fresh ``dtach -a`` client triggers a screen REPLAY
# (the TUI repaints its current state via SIGWINCH) — a byte burst that is NOT new agent
# activity. For this long after an attach, ingested bytes still fill the scrollback ring
# but DON'T stamp the working signal, so merely selecting a session (or server startup
# discovery) can't flip the "agent working" dot. Genuine output after the window stamps
# as before. Per key → ``time.time()`` after which output counts as real again.
_ATTACH_REPLAY_GRACE_S = 0.5
_SUPPRESS_OUTPUT_UNTIL: dict[str, float] = {}


def note_attach(key: str) -> None:
    """Open the post-attach replay-grace window for ``key`` (#195). Called by every reader
    that attaches a fresh ``dtach -a`` client — the WS bridge (``run``) and the headless
    ``SessionStream`` — so the replay burst it triggers doesn't register as agent activity.
    """
    _SUPPRESS_OUTPUT_UNTIL[key] = time.time() + _ATTACH_REPLAY_GRACE_S


# --- Persistent scrollback (#206) --------------------------------------------------
# The in-memory ring above is wiped on every app restart/deploy and only ever holds
# output observed since the app started — so after a deploy a session can only scroll
# back ~one screen. We mirror each session's ring to a small per-session file so
# scrollback SURVIVES restarts: on the first touch of a key in a new process we hydrate
# the ring from its file, and every observed chunk is also appended to disk. The file is
# keyed by the engine-qualified id and head-trimmed to `_MAX_BUF`, so disk use mirrors the
# in-memory ceiling. Best-effort throughout — persistence never breaks the live stream.
# This does NOT recover output produced before the app first observed a session (that
# would need transcript replay from the engine's own store — see #203); it makes the
# rolling `_MAX_BUF` window durable across restarts.
_SCROLLBACK_DIR = Path(
    os.environ.get("AGENT_SESSIONS_SCROLLBACK_DIR")
    or (Path.home() / ".agent-sessions" / "scrollback")
)
_SCROLLBACK_SUFFIX = ".scrollback"
# Head-trim the file only once it grows a full `_MAX_BUF` past the cap, so trims are
# amortized (≈ one rewrite per `_MAX_BUF` of output) rather than on every chunk.
_DISK_TRIM_SLACK = _MAX_BUF
# In-memory ring trim slack fraction (#652 T1): the ring is allowed to overshoot `_MAX_BUF` by
# `_MAX_BUF // _RING_TRIM_DIVISOR` before trimming back — see `_buffer_append`. Computed from
# the LIVE `_MAX_BUF` at trim time (not frozen here) so a test that monkeypatches `_MAX_BUF`
# still exercises trimming. 8 ⇒ ~12.5 % RAM overshoot (~1 MB at the 8 MB default).
_RING_TRIM_DIVISOR = 8
# Keys hydrated from disk this process — so we read the file at most once per key.
_LOADED_FROM_DISK: set[str] = set()


def _scrollback_path(key: str) -> Path:
    # "<engine>:<native>" → filesystem-safe "<engine>__<native>". ':' is legal on Linux
    # but avoided for portability; native ids (uuid / ses_…) never contain '__', so the
    # mapping is unambiguous and reversible (see `_key_from_path`).
    return _SCROLLBACK_DIR / (key.replace(":", "__") + _SCROLLBACK_SUFFIX)


def _cols_path(key: str) -> Path:
    # Companion to the byte mirror (#348 Phase 1): the authored width of the persisted
    # ring. Restored on rehydrate so a post-restart same-width reconnect is recognized as
    # a continuation instead of discarding the ring for the capped transcript fallback.
    return _SCROLLBACK_DIR / (key.replace(":", "__") + ".cols")


# Keys whose retained raw ring holds bytes authored at MORE THAN ONE width (VT keeps the
# ring across width changes as the mirror's feed, #273). Such a ring must never be served
# as a raw same-width continuation — in-memory or post-restart (Hermes #360 r3+r4). The
# marker clears when the ring is reset/dropped (single-width again by construction).
_RING_MIXED: set[str] = set()


def ring_cols(key: str) -> int | None:
    """The authored width of the retained raw ring, usable for the same-width
    continuation decision — None when unknown OR the ring is mixed-width."""
    return None if key in _RING_MIXED else _LAST_COLS.get(key)


# --- Private-mode replay on attach (#397) -------------------------------------------
# Alt-screen TUIs (opencode) and inline agents (claude) enable xterm mouse reporting,
# alternate-scroll, and bracketed paste via DECSET private modes ONCE at startup. Those
# bytes are long gone from the stream by the time a fresh client attaches: the ring is
# capped/head-trimmed (the startup bytes scroll out), and an alt-screen attach replays
# nothing at all (it repaints via SIGWINCH, which redraws CONTENT but never re-emits the
# mode-set sequences). So the freshly attached xterm.js never learns the app wants mouse
# events → a wheel gesture falls back to the alt buffer's (nonexistent) scrollback and
# nothing happens. We scan the output stream for the private modes below, track the
# CURRENT on/off set (DECRST clears, so we never replay a mode the app has turned off),
# persist it beside the ring (#206 durability), and re-emit the active `CSI ? <m> h`
# sequences on every attach so the client re-learns them. The client needs no changes —
# xterm.js handles SGR mouse + alternate scroll natively once the modes are set.
_MODE_TRACK = frozenset({1000, 1002, 1003, 1005, 1006, 1015, 1007, 2004})
# Per-key CURRENT active private-mode set — in-memory mirror of the `.modes` sidecar.
_MODES: dict[str, set[int]] = {}
# Handoff readiness (#597 / PR #703 review round 4): keys whose TUI has been OBSERVED to
# paint a full screen at least once. In-memory mirror of the `.ready` sidecar, hydrated by
# `_ensure_loaded` exactly like `_MODES`. The handoff seed injector counts `out_bytes` per
# `run()` (live bytes only — scrollback REPLAY on a reconnect doesn't advance it), so a seed
# left pending because the first viewer dropped before the quiet window would otherwise see
# `painted == False` forever on the next attach even though the TUI is fully up. This flag
# preserves the first-paint evidence across attachments so the pending seed can be delivered.
_READY: set[str] = set()
# Per-key trailing partial private-mode sequence carried across chunk boundaries, so a
# DECSET/DECRST split between two reads is still recognized. The scan must be incremental
# (not derived from the retained ring like `_in_alt_screen`'s whole-ring rfind): the
# startup mode bytes may be head-trimmed out of the ring entirely, yet the modes are still
# active, so only a live scan that saw them can replay them.
_MODE_CARRY: dict[str, bytes] = {}
# A complete DECSET (`h`) / DECRST (`l`) private-mode sequence: ESC [ ? <params> h|l.
_DECSET_RE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
# A trailing fragment that could still BECOME a private-mode sequence: a bare ESC, ESC [,
# or ESC [ ? <digits/;> with no final byte yet. Anything else can't complete into one, so
# it is dropped rather than carried.
_MODE_PREFIX_RE = re.compile(rb"\x1b(?:\[(?:\?[0-9;]*)?)?\Z")
# Bound the carried fragment so a never-completing ESC can't grow without limit. A real
# private-mode param list is a handful of bytes — well under this.
_MODE_CARRY_MAX = 64


def _modes_path(key: str) -> Path:
    # Companion sidecar to the byte mirror (#397): the agent's CURRENT private-mode set, so
    # mouse reporting / alternate-scroll / bracketed paste survive a broker restart (#206).
    return _SCROLLBACK_DIR / (key.replace(":", "__") + ".modes")


def _persist_modes(key: str) -> None:
    """Mirror the current private-mode set to the `.modes` sidecar (best-effort). An empty
    set removes the file, so a session that turned every tracked mode back off leaves no
    stale claim for the next attach to replay."""
    try:
        _SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        path = _modes_path(key)
        active = _MODES.get(key) or set()
        if active:
            path.write_text(",".join(str(m) for m in sorted(active)))
        else:
            with contextlib.suppress(OSError):
                path.unlink()
    except OSError:
        pass  # best-effort, like the mirror itself


def _scan_modes(key: str, data: bytes) -> None:
    """Update the key's current private-mode set from a streamed output chunk (#397).

    Byte-oriented and resumable: a DECSET/DECRST split across chunk boundaries is carried in
    ``_MODE_CARRY`` and completed on the next call. DECRST (``l``) clears a mode, so we only
    ever retain — and later replay — modes the app currently WANTS. A single multi-mode
    sequence (``CSI ? 1000;1006 h``) sets every listed mode. Persists to the sidecar only
    when the set actually changes."""
    carry = _MODE_CARRY.get(key, b"")
    # Fast-path (#652 T6): a DECSET/DECRST needs an ESC, and a split one is only possible when
    # a fragment was carried. With neither, `carry + data` has no ESC → no matches, no new
    # carry, nothing to clear — so skip the allocation and the whole-chunk regex on the vast
    # majority of plain-output chunks. (Lazy: not materializing an empty `_MODES[key]` here is
    # fine — readers treat a missing set and an empty set identically.)
    if not carry and b"\x1b" not in data:
        return
    buf = carry + data
    active = _MODES.get(key)
    if active is None:
        active = set()
        _MODES[key] = active
    changed = False
    last_end = 0
    for m in _DECSET_RE.finditer(buf):
        last_end = m.end()
        on = m.group(2) == b"h"
        for raw in m.group(1).split(b";"):
            if not raw.isdigit():
                continue  # empty/garbage param (e.g. a stray ';') — skip, don't crash
            mode = int(raw)
            if mode not in _MODE_TRACK:
                continue
            if on and mode not in active:
                active.add(mode)
                changed = True
            elif not on and mode in active:
                active.discard(mode)
                changed = True
    # Carry only a trailing fragment that could still complete into a private-mode sequence,
    # searched AFTER the last complete match (earlier ESCs are consumed or VT-aborted).
    tail = buf[last_end:]
    esc = tail.rfind(b"\x1b")
    new_carry = b""
    if esc != -1:
        frag = tail[esc:]
        if len(frag) <= _MODE_CARRY_MAX and _MODE_PREFIX_RE.match(frag):
            new_carry = frag
    if new_carry:
        _MODE_CARRY[key] = new_carry
    else:
        _MODE_CARRY.pop(key, None)
    if changed:
        _persist_modes(key)


def has_mode(key: str, mode: int) -> bool:
    """True when the agent behind ``key`` currently has private mode ``mode`` set (tracked
    from its live output — see ``_MODE_TRACK``). The handoff seed injector (#597) gates on
    bracketed paste (2004): a TUI arming it is the reliable "input pipeline is up" signal."""
    _ensure_loaded(key)
    active = _MODES.get(key)
    return bool(active and mode in active)


def _ready_path(key: str) -> Path:
    return _SCROLLBACK_DIR / (key.replace(":", "__") + ".ready")


def note_first_paint(key: str) -> None:
    """Record — durably — that ``key``'s TUI has painted a full screen at least once
    (#597 / PR #703 review round 4). Idempotent + best-effort; persisted so a later attach
    inherits the readiness evidence that the live ``out_bytes`` counter can't carry across
    a reconnect."""
    if key in _READY:
        return
    _READY.add(key)
    try:
        _SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        _ready_path(key).write_text("1")
    except OSError:
        pass  # best-effort, like the mirror + `.modes` sidecar


def first_paint_seen(key: str) -> bool:
    """True when ``key``'s TUI has been observed to paint a full screen (persisted, so it
    survives the viewer disconnect the injector's per-``run()`` byte counter cannot)."""
    _ensure_loaded(key)
    return key in _READY


def attach_modes_payload(key: str) -> bytes:
    r"""The active private-mode DECSET sequences to replay on attach (#397), e.g.
    ``b"\x1b[?1000h\x1b[?1006h"``. Empty when no tracked mode is active. Prepended to the
    attach stream by ``webterm.run`` so a freshly attached client re-learns the modes the
    agent set ONCE at startup — independent of the scroll-up content decision, since it must
    survive the alt-screen empty payload AND the transcript/clean-load branches alike."""
    _ensure_loaded(key)
    active = _MODES.get(key)
    if not active:
        return b""
    return b"".join(b"\x1b[?" + str(m).encode() + b"h" for m in sorted(active))


def note_cols(key: str, cols: int, *, persist: bool = True) -> None:
    """Track the agent-pty width for ``key``; optionally persist it beside the mirror.

    The PERSISTED width is a contract about the retained raw bytes — "this ring was
    authored at this width" — not merely the last client width (Hermes #360 round 3:
    stamping a 40-col client onto a 120-col ring made a post-restart 40-col reconnect
    a fake continuation that replayed cross-width garble). ``persist=False`` updates
    only the in-memory tracker AND drops any on-disk claim, because the retained bytes
    are no longer known to be single-width."""
    _LAST_COLS[key] = cols
    if not persist:
        # The retained ring now mixes widths: poison neither the on-disk claim NOR the
        # in-memory continuation check (Hermes #360 r4 — the same-process variant).
        _RING_MIXED.add(key)
        with contextlib.suppress(OSError):
            _cols_path(key).unlink()
        return
    _RING_MIXED.discard(key)
    try:
        _SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        _cols_path(key).write_text(str(int(cols)))
    except OSError:
        pass  # best-effort, like the mirror itself


def note_attach_width(key: str, cols: int) -> None:
    """Attach-time width bookkeeping (#348). Keeps the persisted sidecar truthful:

    * ring empty or already authored at ``cols`` → coherent: persist the width.
    * retained bytes at a DIFFERENT/unknown width → mirror the #245 resize policy at
      attach: reset the ring (the agent re-renders at this client's width from here on;
      the old bytes could only ever garble a later same-width replay), then persist the
      now-truthful width.
    """
    _ensure_loaded(key)
    ring_len = len(_BUFFERS.get(key) or b"")
    coherent = ring_len == 0 or ring_cols(key) == cols
    if not coherent:
        _reset_ring(key)
        coherent = True
    note_cols(key, cols, persist=coherent)


def _key_from_path(p: Path) -> str:
    engine, _, native = p.name[: -len(_SCROLLBACK_SUFFIX)].partition("__")
    return f"{engine}:{native}" if native else engine


# mkdir the scrollback dir at most once per distinct dir (#652 T2): the per-chunk
# `mkdir(exist_ok=True)` in `_persist_append` is a pure syscall tax after the first. A bare
# "done" flag would be wrong — the test suite re-points `_SCROLLBACK_DIR` per case — so guard
# on the CURRENT dir value: re-create only when the target actually changes.
_DIR_READY_FOR: Path | None = None


def _ensure_scrollback_dir() -> None:
    global _DIR_READY_FOR
    if _DIR_READY_FOR != _SCROLLBACK_DIR:
        _SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        _DIR_READY_FOR = _SCROLLBACK_DIR


def _read_file_tail(path: Path, n: int) -> bytes:
    """Read the last ``n`` bytes of ``path`` without loading the whole file (#652 T2/T4).

    The mirror can be up to ``_MAX_BUF + _DISK_TRIM_SLACK`` on disk, so ``read_bytes()[-n:]``
    allocates the ENTIRE file (up to ~2×`_MAX_BUF`) just to keep the tail — on the event loop.
    Seek to ``size - n`` and read forward so only the retained window is touched."""
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > n:
            fh.seek(size - n)
        return fh.read()


def _persist_append(key: str, data: bytes) -> None:
    """Append observed output to the key's on-disk scrollback (best-effort), head-trimming
    to the last `_MAX_BUF` bytes once it grows past the cap + slack."""
    try:
        _ensure_scrollback_dir()
        path = _scrollback_path(key)
        with path.open("ab") as fh:
            fh.write(data)
            size = fh.tell()  # append-mode offset after write == file size; no extra stat()
        if size > _MAX_BUF + _DISK_TRIM_SLACK:
            tail = _read_file_tail(path, _MAX_BUF)  # bounded tail, not a whole-file read
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(tail)
            tmp.replace(path)  # atomic swap so a reader never sees a half-written file
    except OSError:
        pass  # scrollback persistence is best-effort; never break the stream


def _ensure_loaded(key: str) -> None:
    """On the first touch of a key in this process, hydrate its in-memory ring from the
    persisted file — so a reattach after a restart replays the prior scrollback. No-op if
    already loaded/live or no file exists.

    Thread-safety (Hermes #512): this runs on a WORKER THREAD via the AI-review path
    (``live_tail_text``) AND on the event loop (``_buffer_append``). The load marker
    (``_LOADED_FROM_DISK``) must be set ATOMICALLY WITH the registry insert, under ``_RING_LOCK``
    and set LAST — never up-front. The old "mark first, hydrate later" order let a concurrent
    first-touch observe the key as loaded while the durable ring was still mid-read, so the loop's
    ``_buffer_append`` would create a fresh empty ring and the persisted scrollback was silently
    dropped. The disk reads below are one-time, idempotent, and stay OUTSIDE the lock (slow I/O);
    only the apply step is serialised. A concurrent first-touch may read the same files twice —
    harmless — but the winner's hydrate is always preserved."""
    if key in _LOADED_FROM_DISK:
        return
    # --- all disk I/O OUTSIDE the lock; nothing here mutates shared state yet ---
    # Restore the ring's authored width (#348 Phase 1) — without it the post-restart reconnect
    # can never be a same-width continuation and the hydrated ring goes unused. Independent of
    # ring bytes existing: the width survives even an empty/missing mirror.
    cols: int | None = None
    if key not in _LAST_COLS:
        with contextlib.suppress(OSError, ValueError):
            cols = int(_cols_path(key).read_text().strip())
    # Restore the active private-mode set (#397) so a post-restart attach replays the
    # mouse/alternate-scroll/bracketed-paste modes the agent set at startup. Corrupt or
    # partial sidecar entries are skipped per-token rather than poisoning the whole set.
    modes: set[int] | None = None
    if key not in _MODES:
        with contextlib.suppress(OSError):
            raw = _modes_path(key).read_text().strip()
            modes = {
                int(tok)
                for tok in raw.split(",")
                if tok.strip().isdigit() and int(tok) in _MODE_TRACK
            }
    # Restore the handoff first-paint-readiness flag (#597 / PR #703 r4) so a reconnect can
    # deliver a still-pending seed to an already-painted, now-idle TUI.
    ready_seen = key not in _READY and _ready_path(key).exists()
    try:
        # #652 T4: bounded tail read — the on-disk mirror can be ~2×`_MAX_BUF`, and
        # `read_bytes()[-_MAX_BUF:]` allocated the whole file (up to 16 MB) on the first
        # touch of a key just to keep its tail. Seek to the tail instead.
        data = _read_file_tail(_scrollback_path(key), _MAX_BUF)
    except OSError:
        data = b""
    # --- apply + mark-loaded atomically under the lock; marker set LAST ---
    with _RING_LOCK:
        if key in _LOADED_FROM_DISK:
            return  # another first-touch finished the hydrate while we were reading disk
        if cols is not None and key not in _LAST_COLS:
            _LAST_COLS[key] = cols
        if modes is not None and key not in _MODES:
            _MODES[key] = modes
        if ready_seen:
            _READY.add(key)
        # Only seed the ring if nothing live already holds it (the loop's ring is authoritative).
        if data and key not in _BUFFERS:
            _BUFFERS[key] = bytearray(data)
            _BUFFERS.move_to_end(key)
            # Initialize the absolute total ONLY when unknown (#678): after a same-process
            # live-ring eviction the preserved `_TOTALS` entry is the real byte sequence —
            # overwriting it with the (head-trimmed) mirror length here would break a
            # reconnect carrying a pre-eviction `have` on any session that ever exceeded
            # `_MAX_BUF`. A fresh process (no entry) still seeds from the mirror tail.
            if key not in _TOTALS:
                _TOTALS[key] = len(data)
        _LOADED_FROM_DISK.add(key)
        if len(_BUFFERS) > _MAX_BUFFERS:
            _kick_cap_sweep()  # coalesced; hydrates burst over the cap between sweeps


def scrollback_cache_stats() -> dict[str, int]:
    """Total bytes + file count of the on-disk scrollback cache (best-effort)."""
    files = 0
    total = 0
    try:
        for p in _SCROLLBACK_DIR.glob("*" + _SCROLLBACK_SUFFIX):
            try:
                total += p.stat().st_size
                files += 1
            except OSError:
                pass
    except OSError:
        pass
    return {"bytes": total, "files": files}


# Every per-key on-disk artifact: the byte mirror plus its sidecars. The global
# `clear_scrollback()` enumerates ALL of these so a key that has shed its `.scrollback`
# file but kept a sidecar (e.g. `_reset_ring` unlinks the mirror yet preserves `.modes`,
# #397) is still fully cleared — keying off `.scrollback` alone left orphan sidecars that
# replayed stale modes/width after a supposed clear (Hermes #409).
_SIDECAR_SUFFIXES = (_SCROLLBACK_SUFFIX, ".cols", ".modes")


def _all_cached_keys() -> set[str]:
    """Every engine-qualified key with ANY on-disk artifact (mirror or sidecar)."""
    keys: set[str] = set()
    for suffix in _SIDECAR_SUFFIXES:
        try:
            for p in _SCROLLBACK_DIR.glob("*" + suffix):
                stem = p.name[: -len(suffix)]
                engine, _, native = stem.partition("__")
                keys.add(f"{engine}:{native}" if native else engine)
        except OSError:
            pass
    return keys


def clear_scrollback(keys: Iterable[str] | None = None) -> dict[str, int]:
    """Delete persisted scrollback files (mirror + every sidecar) and drop the matching
    in-memory rings/state (so a cleared session isn't re-served from memory or replayed).
    ``keys=None`` clears the whole cache; otherwise only the given engine-qualified keys.
    Returns ``{removed, bytes_freed}`` (``removed`` counts keys with at least one artifact
    deleted)."""
    key_list = sorted(_all_cached_keys()) if keys is None else list(keys)
    removed = 0
    freed = 0
    for key in key_list:
        cleared = False
        # The byte mirror first (it carries the freed-bytes count); then the width + private-
        # mode sidecars travel with it (#348/#397): a stale `.cols` would fake a continuation,
        # a stale `.modes` would replay mouse/paste state for an intentionally-cleared session.
        try:
            freed += _scrollback_path(key).stat().st_size
            _scrollback_path(key).unlink()
            cleared = True
        except OSError:
            pass  # mirror absent (e.g. already reset by `_reset_ring`) — sidecars may remain
        for sidecar in (_cols_path(key), _modes_path(key), _ready_path(key)):
            try:
                sidecar.unlink()
                cleared = True
            except OSError:
                pass
        if cleared:
            removed += 1
        _drop_buffer(key)  # in-memory ring + mode/sanitizer carry + VT mirror
    return {"removed": removed, "bytes_freed": freed}


def _drop_buffer(key: str) -> None:
    # In-memory only — the on-disk scrollback is durable and is removed solely by
    # `clear_scrollback` (Settings cache management). Dropping `_LOADED_FROM_DISK` lets a
    # later touch re-hydrate the ring from disk after an eviction.
    with _RING_LOCK:
        _BUFFERS.pop(key, None)
    _TOTALS.pop(key, None)
    _LAST_OUTPUT_AT.pop(key, None)
    _SUPPRESS_OUTPUT_UNTIL.pop(key, None)
    # Private-mode state (#397) is in-memory only here; the `.modes` sidecar is durable
    # (removed solely by `clear_scrollback`), so a later touch re-hydrates it via
    # `_ensure_loaded` — exactly like the ring itself.
    _MODES.pop(key, None)
    _MODE_CARRY.pop(key, None)
    # Handoff readiness (#597 / PR #703 r4) is in-memory only here; the `.ready` sidecar is
    # durable (removed solely by `clear_scrollback`), so a later touch re-hydrates it via
    # `_ensure_loaded` — exactly like `_MODES`.
    _READY.discard(key)
    _SUBMITTED.discard(key)
    _SANITIZE_CARRY.pop(key, None)
    _LOADED_FROM_DISK.discard(key)
    # A dropped key's probe verdict is from a dead generation — never let it speak for a
    # relaunched session under the same id (Hermes on PR #679).
    _PROBE_CACHE.pop(key, None)


def _reset_ring(key: str) -> None:
    """Drop the retained scrollback CONTENT (in-memory ring + on-disk mirror) while keeping the
    monotonic ``_TOTALS`` offset. Called on a WIDTH change (#244) so the ring never holds bytes
    written at a width different from the one a later same-width attach will replay at — which
    would re-garble (Hermes #245). The agent repaints fresh at the new width; ``_TOTALS`` stays
    monotonic so delta-resume offsets remain valid. (The width-aware screen model #242 makes this
    unnecessary by re-rendering history at any width.)"""
    with _RING_LOCK:
        _BUFFERS[key] = bytearray()
    with contextlib.suppress(OSError):
        _scrollback_path(key).unlink()
    _LOADED_FROM_DISK.add(key)  # don't re-hydrate the now-removed file
    _RING_MIXED.discard(key)  # empty ring is single-width by construction
    # NB: the private-mode set (#397) is deliberately NOT cleared here. A width reset drops
    # the width-fragile CONTENT but keeps session-level state (like `_TOTALS` above); the
    # agent does NOT re-emit its mode-set sequences on the SIGWINCH repaint, so wiping them
    # would re-break mouse scrolling after every resize. They are cleared only on an
    # intentional `clear_scrollback` (the sidecar + in-memory set both go).


def _session_alive(buf_key: str) -> bool:
    """Is the dtach master for this engine-qualified session id still running?

    Used to protect a live session's scrollback from eviction (it's needed for a
    reconnect's delta-resume) and to eagerly reclaim a dead one. Best-effort: an
    unparseable key / lookup error is treated as NOT alive (i.e. evictable).
    """
    try:
        from . import engines

        prov, native = engines.parse_key(buf_key)
        return ptybridge.session_exists(prov.engine_id, native)
    except Exception:
        return False


# --- buffer-cap enforcement, OFF the byte pump (#678) -----------------------------------
#
# `_enforce_buffer_cap` used to run inside `_buffer_append` — on the event loop, per output
# chunk — and each pass did blocking dtach-socket probes (`_session_alive`, a connect with a
# 0.2/0.5/1.0 s timeout ladder). With more live masters than `_MAX_BUFFERS` and "evict dead
# only" semantics, no victim was ever found and the FULL probe sweep repeated on every chunk:
# on the production box (200+ live sessions) the loop spent essentially all its time probing
# (py-spy: 15/15 main-thread samples; /healthz p50 555 ms) — every keystroke of every terminal
# queued behind it. Enforcement now runs in ONE coalesced periodic sweep whose probing happens
# in a worker thread, and the cap is made satisfiable by LRU-evicting live-but-idle rings
# (recoverable by design: the on-disk mirror rehydrates on next touch, exactly like after an
# app restart, and the eviction path preserves `_TOTALS` so pre-eviction `have` offsets stay
# valid). Rings with an attached viewer — plus a short post-detach grace so a transient WS
# drop can't race the sweep — are never evicted.

_SWEEP_INTERVAL_S = 30.0
# Post-detach pin grace: a reconnect after a transient drop arrives well inside this window,
# so its delta-resume `have` still finds the un-evicted ring.
_PIN_GRACE_S = 60.0
# Probe verdicts are cached briefly so a sweep over a large registry does not re-probe every
# socket each pass. Staleness only delays reclaiming a dead session's ring — harmless; the
# eager end-of-run reap (`_maybe_evict_ended`) is unaffected.
_PROBE_TTL_S = 45.0

# Attached-viewer refcounts + post-detach grace deadlines. Guarded by `_RING_LOCK` (they are
# read by the sweep worker thread), written from the event loop by the webterm bridge.
_PIN_COUNTS: dict[str, int] = {}
_PIN_GRACE_UNTIL: dict[str, float] = {}

# key -> (monotonic expiry, alive). Written/read only by the sweep worker (one at a time,
# coalesced), so it needs no lock of its own.
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}

# Armed by `run_cap_sweeper` on the event loop; `_kick_cap_sweep` is callable from any
# thread (worker-thread hydrates included) and coalesces naturally — setting an already-set
# event schedules nothing extra, so a hydrate burst wakes at most one sweep.
_sweep_wake: asyncio.Event | None = None
_sweep_loop: asyncio.AbstractEventLoop | None = None


def note_viewer_attached(key: str) -> None:
    """Pin ``key``'s ring while a viewer is attached (#678) — never a sweep victim."""
    with _RING_LOCK:
        _PIN_COUNTS[key] = _PIN_COUNTS.get(key, 0) + 1
        _PIN_GRACE_UNTIL.pop(key, None)


def note_viewer_detached(key: str) -> None:
    """Drop one viewer pin; the last detach starts the post-detach grace window."""
    with _RING_LOCK:
        n = _PIN_COUNTS.get(key, 0) - 1
        if n > 0:
            _PIN_COUNTS[key] = n
        else:
            _PIN_COUNTS.pop(key, None)
            _PIN_GRACE_UNTIL[key] = time.monotonic() + _PIN_GRACE_S


def _is_pinned_locked(key: str, now: float) -> bool:
    """Caller holds ``_RING_LOCK``. Attached, or inside the post-detach grace."""
    if _PIN_COUNTS.get(key, 0) > 0:
        return True
    until = _PIN_GRACE_UNTIL.get(key)
    if until is None:
        return False
    if until <= now:
        _PIN_GRACE_UNTIL.pop(key, None)  # expired — drop the entry so the dict stays bounded
        return False
    return True


def _kick_cap_sweep() -> None:
    """Wake the cap sweeper ahead of its interval. Thread-safe + coalescing; a no-op until
    the sweeper task is armed (tests driving the sync sweep directly, or startup order)."""
    loop, evt = _sweep_loop, _sweep_wake
    if loop is not None and evt is not None:
        with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
            loop.call_soon_threadsafe(evt.set)


def _session_verdict(buf_key: str) -> str:
    """Tri-state master liveness for the cap sweep: ``ptybridge.ALIVE`` / ``DEAD`` /
    ``UNKNOWN``. The boolean ``_session_alive`` maps UNKNOWN (probe timeouts on a loaded
    host) to False — safe for its non-destructive callers, but the sweep's dead branch is
    DESTRUCTIVE (``_drop_buffer`` erases ``_TOTALS``), so a timeout must never be read as
    proven dead (Hermes on PR #679 round 2): only a decisive DEAD may take that branch,
    while UNKNOWN is treated like live — at most the ring-only LRU path evicts it, which
    the mirror + preserved total recover from. An unparseable key stays DEAD (evictable),
    matching ``_session_alive``."""
    try:
        from . import engines

        prov, native = engines.parse_key(buf_key)
        sock = ptybridge.socket_path(prov.engine_id, native)
    except Exception:
        return ptybridge.DEAD
    if not sock.is_socket():
        return ptybridge.DEAD
    return ptybridge.probe_master(sock)


def _session_verdict_cached(buf_key: str) -> str:
    """`_session_verdict` behind the sweep's TTL verdict cache, with perfstats counters
    (`count` in the /api/perf snapshot is the event counter)."""
    now = time.monotonic()
    hit = _PROBE_CACHE.get(buf_key)
    if hit is not None and hit[0] > now:
        perfstats.record("cap_probe_cache_hit", 1.0)
        return hit[1]
    perfstats.record("cap_probe_attempt", 1.0)
    verdict = _session_verdict(buf_key)
    # Cache ALIVE only (Hermes on PR #679): a cached DEAD can outlive a rapid same-key
    # relaunch and make the next sweep fully drop the NEW live session's ring and
    # `_TOTALS` without ever probing it — and dead probes are an immediate errno anyway
    # (no timeout ladder), so re-asking is cheap. UNKNOWN is a transient condition by
    # definition and is counted separately for the production evidence.
    if verdict is ptybridge.ALIVE:
        _PROBE_CACHE[buf_key] = (now + _PROBE_TTL_S, verdict)
    else:
        _PROBE_CACHE.pop(buf_key, None)
        if verdict is ptybridge.UNKNOWN:
            perfstats.record("cap_probe_unknown", 1.0)
    if len(_PROBE_CACHE) > 1024:  # prune expired entries so the cache stays bounded
        for k in [k for k, (exp, _v) in _PROBE_CACHE.items() if exp <= now]:
            _PROBE_CACHE.pop(k, None)
    return verdict


def _evict_live_ring_locked(key: str) -> None:
    """Release a live-but-idle ring's BYTES only (caller holds ``_RING_LOCK``).

    Unlike ``_drop_buffer`` this PRESERVES ``_TOTALS`` — the absolute byte sequence — and all
    session-level state (modes, parser carries, output stamps): the on-disk mirror rehydrates
    the bytes on the next touch, and the preserved total keeps ``ring_start = total − len(ring)``
    and the reconnect ``have`` contract intact even for sessions that exceeded ``_MAX_BUF``
    (where the mirror tail is SHORTER than the true sequence — `_drop_buffer` here would have
    reset the total to that shorter length on rehydrate)."""
    _BUFFERS.pop(key, None)
    _LOADED_FROM_DISK.discard(key)  # next touch rehydrates from the mirror


def _enforce_buffer_cap() -> None:
    """One cap-enforcement pass — the SYNC sweep body, run in a worker thread by
    ``run_cap_sweeper`` (never on the event loop, never from ``_buffer_append``).

    Dead/orphan rings are reclaimed fully first (their sessions can never resume); if the
    registry is still over ``_MAX_BUFFERS``, live-but-idle rings are evicted oldest-LRU-first
    via `_evict_live_ring_locked` — skipping pinned keys (attached viewers + post-detach
    grace), with eligibility re-checked at victim-application time so an attach racing the
    probe phase can never have its freshly pinned ring evicted."""
    with perfstats.timed("buffer_cap_sweep_ms"):
        # Snapshot under the lock, probe outside it — the discipline from commit
        # `a4bbaea` (Hermes on PR #512) that keeps the worker-thread hydrate from
        # racing the scan into `OrderedDict mutated during iteration`.
        with _RING_LOCK:
            if len(_BUFFERS) <= _MAX_BUFFERS:
                return
            now = time.monotonic()
            candidates = [k for k in _BUFFERS if not _is_pinned_locked(k, now)]
        if not candidates:
            return  # everything retained is pinned — nothing safe to evict this pass
        # Classify the snapshot ONCE, then evict as many victims as the cap needs from that
        # single result (Hermes on PR #679 round 3): the previous evict-one-then-rescan loop
        # re-probed every candidate per victim — O(N²) probes, and with UNKNOWN deliberately
        # uncached an all-UNKNOWN registry at production scale (203 rings over a 64 cap)
        # meant ~18.6k timeout-ladder probes ≈ hours of worker time per sweep, under exactly
        # the host-load condition UNKNOWN represents. One probe per candidate per sweep,
        # linear by construction. ``candidates`` is LRU-ordered (oldest first), so each
        # eviction phase below consumes the stalest rings first.
        dead_keys: list[str] = []
        other_keys: list[str] = []  # ALIVE or UNKNOWN — never fully dropped
        for k in candidates:
            (dead_keys if _session_verdict_cached(k) is ptybridge.DEAD else other_keys).append(k)
        # Dead rings first (full reclaim — no resume possible), then live/UNKNOWN rings
        # oldest-LRU-first via the totals-preserving ring-only path, until the cap holds.
        # Victim-time re-check under the lock for BOTH phases (Hermes on PR #679): an attach
        # can pin a key while its probe was in flight; such a key is simply skipped.
        for k in dead_keys:
            with _RING_LOCK:
                if len(_BUFFERS) <= _MAX_BUFFERS:
                    return
                if k in _BUFFERS and not _is_pinned_locked(k, time.monotonic()):
                    _drop_buffer(k)
        for k in other_keys:
            with _RING_LOCK:
                if len(_BUFFERS) <= _MAX_BUFFERS:
                    return
                if k in _BUFFERS and not _is_pinned_locked(k, time.monotonic()):
                    _evict_live_ring_locked(k)


async def run_cap_sweeper() -> None:
    """Background buffer-cap sweeper (#678, reaper pattern; started from the app lifespan).

    Wakes every ``_SWEEP_INTERVAL_S`` or immediately on a `_kick_cap_sweep` (over-cap append /
    hydrate), and runs the sync sweep in a worker thread so its socket probes never touch the
    event loop. Failures are logged and never fatal — the next tick retries."""
    global _sweep_wake, _sweep_loop
    _sweep_wake = asyncio.Event()
    _sweep_loop = asyncio.get_running_loop()
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_sweep_wake.wait(), timeout=_SWEEP_INTERVAL_S)
        _sweep_wake.clear()
        try:
            await asyncio.to_thread(_enforce_buffer_cap)
        except Exception:
            log.exception("buffer-cap sweep failed; retrying next tick")


def _kick_review(reason: str) -> None:
    """Wake the AI-review loop ahead of its interval. Lazy import breaks the
    ``scrollback → ai_review_loop → review → scrollback`` module cycle; the kick is a no-op
    until the loop is armed. Only ever called on the event loop, where setting the loop's
    ``asyncio.Event`` is safe. Best-effort — a review-side hiccup must never break the byte
    pump or the input path."""
    try:
        from . import ai_review_loop

        ai_review_loop.request_review_soon()
    except Exception:  # pragma: no cover - defensive; the pump must survive anything here
        log.debug("%s AI-review kick failed — non-fatal", reason, exc_info=True)


def _kick_review_on_first_output() -> None:
    """Nudge the AI-review loop when a session first produces reviewable output (#552).

    The only launch-time kick (``request_review_soon`` from the new-session path) fires ~3s
    after create, when the session is still empty and ``review.gather_input`` has nothing to
    hash — so the first title/summary otherwise waits up to the full review interval. Firing
    here, at the single output chokepoint, lands the wake exactly when live-tail content first
    exists.
    """
    _kick_review("first-output")


# Sessions whose user has submitted at least one line (#611). Edge-trigger state for
# `note_user_submit`, cleared with the rest of a key's in-memory state in `_drop_buffer`.
_SUBMITTED: set[str] = set()


def note_user_submit(key: str) -> None:
    """The user just submitted a line to this session — wake the reviewer (#611).

    The first-output kick above fires on the agent's *banner*: at that moment there is no
    transcript and no first user message, so the reviewer titles a splash screen and the
    session shows ``(untitled)`` until the next periodic sweep. The moment worth reviewing is
    the one right after the user actually sends something.

    Edge-triggered per key — later submits ride the normal interval — and the sweep's
    fingerprint gate means even a redundant wake costs no endpoint call. Called from the ws
    input chokepoint only after the read-only gate has passed, so a secondary (read-only) tab
    can never kick.
    """
    if not key or key in _SUBMITTED:
        return
    _SUBMITTED.add(key)
    _kick_review("first-submit")


def _buffer_append(key: str, data: bytes) -> None:
    _ensure_loaded(key)  # hydrate prior scrollback from disk before the first append (#206)
    # Guard only the registry structure (get-or-create + LRU touch); the `extend` below mutates
    # the ring VALUE in place, which doesn't change the dict shape and so needs no lock.
    # The ring mutation is published as a SEQLOCK interval (#726), not as a single bump.
    # The orchestrator's write fence compares this so a proposal approved against one prompt
    # can never be typed into the next one — and neither single-bump ordering is sufficient:
    # bumping after leaves the bytes visible with a stale counter, bumping before lets the
    # sender capture the new value and still authorise against the old screen. Odd means a
    # change is in flight; even means stable.
    with session_input.screen_change(key):
        with _RING_LOCK:
            buf = _BUFFERS.get(key)
            if buf is None:
                buf = bytearray()
                _BUFFERS[key] = buf
            _BUFFERS.move_to_end(key)  # most-recently-used
        buf.extend(data)
    # Track DECSET/DECRST private modes off the SAME single chokepoint (#397) — both the
    # attached WS pump and the detached SessionStream land here, so mouse-reporting /
    # alternate-scroll / bracketed-paste state stays current with or without a viewer.
    _scan_modes(key, data)
    _persist_append(key, data)  # mirror to disk so scrollback survives a restart (#206)
    # #652 T7: `_TOTALS` is also an OrderedDict shared with the worker-thread hydrate
    # (`_ensure_loaded` writes it under `_RING_LOCK`), so serialize this mutation under the
    # same lock — the cross-thread OrderedDict race `_RING_LOCK` exists to prevent. O(1) work,
    # no expensive I/O held.
    with _RING_LOCK:
        _TOTALS[key] = _TOTALS.get(key, 0) + len(data)
        _TOTALS.move_to_end(key)
    # Skip the working-signal stamp while inside the post-attach replay grace (#195):
    # the screen-redraw burst is not new agent activity. Scrollback (buf/_TOTALS) is
    # always updated so a reattach still resumes the full screen.
    now = time.time()
    if now >= _SUPPRESS_OUTPUT_UNTIL.get(key, 0.0):
        first_output = key not in _LAST_OUTPUT_AT
        _LAST_OUTPUT_AT[key] = now
        _LAST_OUTPUT_AT.move_to_end(key)
        if first_output:
            # First genuine (post-replay-grace) output for this session (#552): wake the
            # AI-review loop now so a brand-new session's title/summary populates promptly
            # instead of waiting up to the review interval. Edge-triggered — once per key until
            # eviction — so chatty output never re-wakes; keyless + gated downstream, so it can
            # never force an endpoint call the periodic loop wouldn't have made.
            _kick_review_on_first_output()
    # #652 T1: amortized front-trim. Overshoot `_MAX_BUF` by up to `_MAX_BUF // _RING_TRIM_DIVISOR`
    # before dropping back, so the O(`_MAX_BUF`) memmove runs once per slack bytes instead of on
    # every chunk once full. Readers derive `ring_start = total - len(ring)` from the ACTUAL
    # length (nothing asserts `len == _MAX_BUF`), so an overshoot merely serves slightly more
    # scrollback with byte offsets still consistent — mirrors the on-disk `_DISK_TRIM_SLACK`.
    if len(buf) > _MAX_BUF + _MAX_BUF // _RING_TRIM_DIVISOR:
        del buf[: len(buf) - _MAX_BUF]
    # Cap enforcement is OFF this path (#678): probing dtach sockets per chunk on the event
    # loop was the typing-latency treadmill. Over-cap just wakes the coalesced sweeper.
    if len(_BUFFERS) > _MAX_BUFFERS:
        _kick_cap_sweep()


# ANSI/VT escape stripper for the live-tail accessor (#356): CSI sequences, OSC strings
# (BEL- or ST-terminated), other ESC-prefixed singles, and the remaining C0 controls
# except \n and \t. Bounded input keeps the regex work cheap.
# The string-control terminators are OPTIONAL: a tail slice of a live ring routinely cuts an
# OSC/DCS in half, and a pattern that insists on the terminator fails to match — leaving the ESC
# to be eaten as a stray C0 and the payload to render as visible text. That is how an OSC 52
# clipboard write reached the AI reviewer as `]52;c;<base64>`. A control-string payload is never
# display content; consume it to its terminator or to the end of the slice. The string controls
# must also precede the ESC-single alternative, whose `[@-Z\\^_]` class would otherwise claim the
# `P` of a DCS and leak the rest.
_ANSI_ESCAPES = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC … BEL / ST / unterminated
    rb"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"  # DCS / PM / APC / SOS … ST / unterminated
    rb"|\x1b\[[0-9;:?<=>]*[ -/]*[@-~]"  # CSI
    rb"|\x1b[@-Z\\^_]"  # other ESC singles (incl. ESC ( … handled below)
    rb"|\x1b[()][0-9A-Za-z]"  # charset designations
    rb"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # stray C0 controls (keep \n \t; \r handled after)
)


# How much of the ring's tail to replay through the screen renderer (#611). A repainting TUI
# only re-emits the cells it dirties, so the current frame is assembled from writes spread far
# back in the stream — a few KB is not enough to reconstruct it. 256 KiB renders a live codex
# frame faithfully in ~160 ms of worker-thread CPU, and the review sweep is bounded to
# SWEEP_CAP sessions every `interval_minutes`.
_SCREEN_TAIL_BYTES = _int_env("AGENT_SESSIONS_REVIEW_SCREEN_BYTES", 256 * 1024)


def _stripped_tail_text(raw: bytes, max_chars: int) -> str:
    """The pre-#611 accessor: delete escape sequences and keep what's left. Retained as the
    fallback for rings the screen renderer can't render (unknown geometry, mixed-width ring,
    a frame that comes out empty) so the reviewer never gets *less* than it used to."""
    # This re-slices `raw`, so it cuts the byte stream a SECOND time and can land inside a
    # control string all over again — the leak `live_tail_text` just guarded against.
    start = max(0, len(raw) - max_chars * 8)
    tail = raw[start:]
    if vtscreen.starts_inside_control_string(raw, start):
        tail = vtscreen.drop_open_control_prefix(tail)
    text = _ANSI_ESCAPES.sub(b"", tail).decode("utf-8", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse the blank-line runs full-screen repaints leave behind.
    text = re.sub(r"\n[ \t]*\n[ \t\n]*", "\n\n", text)
    return text[-max_chars:]


def live_tail_text(key: str, max_chars: int = 4000) -> str:
    """The session's current terminal SCREEN as plain text (#356 accessor, #611 renderer).

    The NARROW accessor the AI-review engine uses, so review code never reaches into the
    ``_BUFFERS`` module globals directly. The ring's tail is replayed through ``vtscreen``
    at the geometry the bytes were authored at, yielding the frame a human would see —
    rather than the escape-stripped byte soup an in-place repaint leaves behind (a codex
    spinner stripped of its cursor moves reads as ``Working•4orking•rking•king``).

    Falls back to the plain escape-strip when the frame can't be trusted: a mixed-width ring
    (``ring_cols`` → None), an unknown width, or a render that comes out empty. So this can
    only ever add information, never remove it.

    Called from a WORKER THREAD off the event loop (``asyncio.to_thread``), so every touch of
    the shared ring registry — the one-time hydrate in ``_ensure_loaded`` and the slice read
    below — is serialised under ``_RING_LOCK`` against the loop's concurrent writes/scan; the
    (bounded) render CPU work runs outside the lock. Returns ``""`` when the session has no
    observed output (headless / no PTY / evicted) — the caller falls back to a
    transcript-only review.
    """
    if max_chars <= 0:
        return ""
    _ensure_loaded(key)
    # Copy the slice under the lock so a concurrent `extend`/evict on the loop can't tear the
    # read. Sized for the renderer; the strip fallback re-slices its own (smaller) window.
    #
    # The cut can land INSIDE a control string, whose introducer then sits behind the slice
    # where no parser can see it — the payload that follows reads as ordinary text, which is how
    # an OSC 52 clipboard write leaked into the review input. Only this function holds the whole
    # ring, so only this function can tell; the look-back is a few `rfind`s over the prefix.
    with _RING_LOCK:
        ring = _BUFFERS.get(key)
        if not ring:
            return ""
        start = max(0, len(ring) - _SCREEN_TAIL_BYTES)
        open_control_string = vtscreen.starts_inside_control_string(ring, start)
        raw = bytes(ring[start:])
    if open_control_string:
        raw = vtscreen.drop_open_control_prefix(raw)
    if not raw:
        return ""
    # `ring_cols` is None for a mixed-width ring — rendering absolute cursor moves against the
    # wrong width is exactly the garble #245/#293 exists to prevent, so don't.
    cols = ring_cols(key)
    if cols:
        # `_LAST_ROWS` is in-memory only, so after a restart with no browser attached we don't
        # know the height. The agent tells us: the tallest absolute row it addressed IS the
        # screen it is drawing to.
        rows = _LAST_ROWS.get(key) or vtscreen.infer_rows(raw)
        if rows:
            screen = vtscreen.render(raw, rows, cols)
            if screen:
                return screen[-max_chars:]
    return _stripped_tail_text(raw, max_chars)


def get_last_output_at(key: str) -> float | None:
    """Wall-clock of the last byte observed from this session (#156). ``None`` if we've
    never seen output for it — either the session has no attached WS, or the buffer was
    evicted. Best-effort.
    """
    return _LAST_OUTPUT_AT.get(key)


def _maybe_evict_ended(buf_key: str | None) -> None:
    """Drop a session's retained buffer once its run ends and no dtach master survives.

    The buffer only earns its keep while the agent is still alive (a later reconnect
    delta-resumes from it). When the dtach master is gone there's nothing to resume,
    so we reclaim the memory immediately rather than waiting for the LRU backstop.
    Best-effort — any failure leaves the entry for `_enforce_buffer_cap` to reclaim.
    """
    if buf_key and not _session_alive(buf_key):
        _drop_buffer(buf_key)


def _resume_payload(key: str, have: int) -> tuple[bytes, int]:
    """Decide what to (re)send on connect: ``(payload, total)``.

    - No history yet → ``(b"", total)``.
    - Alt-screen TUI (opencode) → ``(b"", total)``: it repaints via SIGWINCH; replaying
      its frames corrupts the redraw, and we never blank on reconnect.
    - ``have`` is a valid absolute offset still inside the ring → the **delta** since
      ``have`` (seamless continuation across a drop).
    - Otherwise (fresh attach, or ``have`` fell behind the capped ring) → **full replay**.

    The caller follows the payload with a ``{"t":"seq","n":total}`` control frame so the
    client adopts ``total`` as its authoritative offset for the next reconnect.
    """
    _ensure_loaded(key)  # after a restart the ring is empty; restore it from disk (#206)
    total = _TOTALS.get(key, 0)
    ring = _BUFFERS.get(key) or b""
    if not ring or _in_alt_screen(bytes(ring)):
        return b"", total
    ring_start = total - len(ring)  # absolute offset of ring[0]
    if 0 < have <= total and have >= ring_start:
        return bytes(ring[have - ring_start :]), total
    return bytes(ring), total


# Clean-load clear sequence (#227): cursor home + clear screen + clear scrollback.
_CLEAN_LOAD_CLEAR = b"\x1b[H\x1b[2J\x1b[3J"
_CODEX_SCROLLBACK_ERASE = b"\x1b[3J"
_SANITIZE_CARRY: dict[str, bytes] = {}


def sanitize_live_output(key: str, data: bytes) -> bytes:
    """Return live PTY output safe for BattleLab-managed scrollback.

    Codex's ratatui repaint path periodically emits CSI 3J (erase scrollback) as part of a
    full-screen clear. In a standalone terminal that is reasonable; in BattleLab it deletes the
    semantic transcript/scrollback we intentionally keep above the live frame, leaving mobile touch
    scroll with nothing to move. Server-authored attach clears still use ``_CLEAN_LOAD_CLEAR``;
    this helper is only for bytes read from the live dtach PTY stream.
    """
    if not key.startswith("codex:"):
        _SANITIZE_CARRY.pop(key, None)
        return data
    src = _SANITIZE_CARRY.pop(key, b"") + data
    out = bytearray()
    i = 0
    while i < len(src):
        remaining = len(src) - i
        if remaining >= len(_CODEX_SCROLLBACK_ERASE) and src.startswith(_CODEX_SCROLLBACK_ERASE, i):
            i += len(_CODEX_SCROLLBACK_ERASE)
            continue
        if remaining < len(_CODEX_SCROLLBACK_ERASE) and _CODEX_SCROLLBACK_ERASE.startswith(src[i:]):
            _SANITIZE_CARRY[key] = src[i:]
            break
        out.append(src[i])
        i += 1
    return bytes(out)


def _is_same_width_continuation(have: int, total: int, buffer_cols: int | None, cols: int) -> bool:
    """Whether a reconnect can be satisfied with the raw byte-delta instead of re-rendering the
    scroll-up (#262). True only when the client already holds matching-width scrollback — it sent a
    real in-ring offset (``0 < have <= total``) AND its width equals the width we last served it
    (``buffer_cols==cols``) — a brief WS blip on a live same-width session, or a SAME-width
    ``have>0`` reconnect after a broker restart: the width sidecar (#348) restores the ring's
    authored width on rehydrate, so restarts no longer demote these to the transcript. Everything
    else — a fresh load (``have<=0``), a cross-width client, or a pre-#348 mirror without a width
    sidecar (``buffer_cols`` is None) — is False, so the caller renders the width-correct
    transcript rather than replaying the fixed-width raw ring at a wrong width.

    The ``have <= total`` guard (#484) mirrors the same invariant ``_resume_payload`` enforces:
    after an app restart the ring is rehydrated head-trimmed to ``_MAX_BUF`` while the authored
    width is restored, so a same-width reconnect can carry a pre-restart ``have`` that now exceeds
    the smaller ``total``. That is NOT a seamless continuation — the client holds MORE than the ring
    does — so it must fall through to the width-correct clear/transcript path (which begins with a
    clean-load clear), or the rehydrated ring is replayed UNDER the client's stale scrollback and
    the whole conversation renders twice."""
    return 0 < have <= total and buffer_cols == cols


def _clean_load_payload(total: int, client_cols: int, buffer_cols: int | None) -> bytes | None:
    """Clean-load fallback (#227, narrowed by #244) for when there's no transcript adapter: return a
    clear instead of replaying the inline scrollback, but ONLY when the client width differs from
    the width the buffer was written at. The caller already established this is **not** a same-width
    continuation (#262), so a clear here means a fresh/cross-width/post-restart load of a session
    whose raw ring can't be trusted at ``client_cols``.

    The garbling only happens on a width MISMATCH: the buffered cursor-positioning was written at
    ``buffer_cols`` and mis-positions when replayed at a different ``client_cols``. When they match
    (a desktop reload at the same width) the replay is clean, so keep the normal payload and keep
    scrollback. ``buffer_cols`` **unknown** (None — e.g. right after a restart, before any width is
    recorded) is treated as a MISMATCH too: we must never trust bytes of unproven width (a 120-col
    persisted ring replayed at 40 cols garbles), so we clear and let the agent repaint at the client
    width — the ring is then reset/rebuilt at a known width (Hermes #245).

    ``None`` → use the normal resume payload: width matches, or a brand-new session (nothing to
    clear). Superseded by the width-aware transcript (#242) wherever an adapter exists.
    """
    if total > 0 and client_cols != buffer_cols:
        return _CLEAN_LOAD_CLEAR
    return None


def _in_alt_screen(buf: bytes) -> bool:
    """True if the session is currently on the alternate screen buffer.

    Replaying raw scrollback is right for *inline* agents (claude/codex/gemini) but
    wrong for an alt-screen TUI (opencode): the alt buffer has no scrollback, and
    replaying its frames corrupts the redraw (blank screen that only partially
    reappears on scroll). Such sessions redraw themselves via SIGWINCH on attach,
    so we skip the replay. Detected by the last 1049h (enter) vs 1049l (leave)."""
    return buf.rfind(b"\x1b[?1049h") > buf.rfind(b"\x1b[?1049l")


# Fresh-load scroll-up from the engine's saved conversation transcript (#242). On by default;
# AGENT_SESSIONS_TRANSCRIPT_SCROLLBACK=0 falls back to raw-byte clean-load everywhere.
_TRANSCRIPT_SCROLLBACK = (os.environ.get("AGENT_SESSIONS_TRANSCRIPT_SCROLLBACK", "1") or "1") != "0"
# Engine id → display label for the "⏺ <label>" assistant marker.
_ENGINE_LABEL = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "opencode",
    "kimi": "Kimi",
}


def _transcript_payload(buf_key: str, cols: int, rows: int = 24) -> tuple[bytes, int] | None:
    """Render the engine's saved conversation transcript as a fresh-load scroll-up payload (#242):
    ``clear + rendered conversation + a full viewport of blank lines``. The blank lines push the
    WHOLE transcript into xterm's scrollback (above the visible screen) so the live agent repaints
    its current frame into a BLANK viewport — its repaint can't overwrite the transcript (the
    "half a page or nothing" bug, #301). Scroll-up then lands straight on the transcript.

    Returns ``(payload, history_cursor)``. ``history_cursor`` is the EXACT turn index the
    rendered payload starts at (``transcript.render_with_boundary``) — webterm forwards it to
    the client as the ``{"t":"hist","cursor":N}`` control frame so the first scroll-up
    lazy-load request asks for exactly ``before=N``. Only the renderer that built the payload
    knows this boundary; re-deriving it later from line counts at the (possibly resized)
    request width skipped turns (Hermes #365 r2 finding 1).

    Width-correct semantic text (no width-fragile raw-byte replay). Returns ``None`` to fall back to
    clean-load when the feature is off, the engine has no transcript adapter, the id can't be
    parsed, or there's nothing to render.

    Pure + side-effect-free (reads the on-disk transcript only); the caller runs it off the event
    loop and still sends the real ``seq`` total afterwards, so delta-resume offsets are untouched.
    """
    if not _TRANSCRIPT_SCROLLBACK:
        return None
    try:
        from . import engines

        prov, native = engines.parse_key(buf_key)
    except Exception:
        return None
    adapter = transcript.adapter_for(prov.engine_id)
    if adapter is None:
        return None
    try:
        turns = adapter(native, Path.home())
        body, boundary = transcript.render_with_boundary(
            turns, cols, assistant_label=_ENGINE_LABEL.get(prov.engine_id, prov.engine_id)
        )
    except Exception:
        return None
    if not body:
        return None
    # Clear, then the rendered conversation, then a FULL viewport of blank lines so the whole
    # transcript scrolls up into xterm's scrollback. The live agent repaints into the blank viewport
    # below — it can't clobber the transcript above (#301). Scroll-up lands on the transcript.
    return _CLEAN_LOAD_CLEAR + body + b"\r\n" * max(1, rows), boundary
