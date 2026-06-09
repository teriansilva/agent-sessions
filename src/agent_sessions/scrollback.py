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

import contextlib
import os
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path

from . import ptybridge, transcript, vtsidecar

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
# Keys hydrated from disk this process — so we read the file at most once per key.
_LOADED_FROM_DISK: set[str] = set()


def _scrollback_path(key: str) -> Path:
    # "<engine>:<native>" → filesystem-safe "<engine>__<native>". ':' is legal on Linux
    # but avoided for portability; native ids (uuid / ses_…) never contain '__', so the
    # mapping is unambiguous and reversible (see `_key_from_path`).
    return _SCROLLBACK_DIR / (key.replace(":", "__") + _SCROLLBACK_SUFFIX)


def _key_from_path(p: Path) -> str:
    engine, _, native = p.name[: -len(_SCROLLBACK_SUFFIX)].partition("__")
    return f"{engine}:{native}" if native else engine


def _persist_append(key: str, data: bytes) -> None:
    """Append observed output to the key's on-disk scrollback (best-effort), head-trimming
    to the last `_MAX_BUF` bytes once it grows past the cap + slack."""
    try:
        _SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
        path = _scrollback_path(key)
        with path.open("ab") as fh:
            fh.write(data)
        if path.stat().st_size > _MAX_BUF + _DISK_TRIM_SLACK:
            tail = path.read_bytes()[-_MAX_BUF:]
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(tail)
            tmp.replace(path)  # atomic swap so a reader never sees a half-written file
    except OSError:
        pass  # scrollback persistence is best-effort; never break the stream


def _ensure_loaded(key: str) -> None:
    """On the first touch of a key in this process, hydrate its in-memory ring from the
    persisted file — so a reattach after a restart replays the prior scrollback. No-op if
    already loaded/live or no file exists."""
    if key in _LOADED_FROM_DISK:
        return
    _LOADED_FROM_DISK.add(key)
    if key in _BUFFERS:  # already live in this process — the ring is authoritative
        return
    try:
        data = _scrollback_path(key).read_bytes()[-_MAX_BUF:]
    except OSError:
        return
    if not data:
        return
    _BUFFERS[key] = bytearray(data)
    _BUFFERS.move_to_end(key)
    _TOTALS[key] = len(data)


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


def clear_scrollback(keys: Iterable[str] | None = None) -> dict[str, int]:
    """Delete persisted scrollback files and drop the matching in-memory rings (so a
    cleared session isn't re-served from memory). ``keys=None`` clears the whole cache;
    otherwise only the given engine-qualified keys. Returns ``{removed, bytes_freed}``."""
    try:
        if keys is None:
            paths = list(_SCROLLBACK_DIR.glob("*" + _SCROLLBACK_SUFFIX))
        else:
            paths = [_scrollback_path(k) for k in keys]
    except OSError:
        return {"removed": 0, "bytes_freed": 0}
    removed = 0
    freed = 0
    for p in paths:
        try:
            sz = p.stat().st_size
        except OSError:
            continue  # not present → nothing to clear
        try:
            p.unlink()
        except OSError:
            continue
        removed += 1
        freed += sz
        _drop_buffer(_key_from_path(p))
    return {"removed": removed, "bytes_freed": freed}


def _drop_buffer(key: str) -> None:
    # In-memory only — the on-disk scrollback is durable and is removed solely by
    # `clear_scrollback` (Settings cache management). Dropping `_LOADED_FROM_DISK` lets a
    # later touch re-hydrate the ring from disk after an eviction.
    _BUFFERS.pop(key, None)
    _TOTALS.pop(key, None)
    _LAST_OUTPUT_AT.pop(key, None)
    _SUPPRESS_OUTPUT_UNTIL.pop(key, None)
    _LOADED_FROM_DISK.discard(key)
    # Tear down the session's VT-sidecar emulator too (#273). No-op unless the flag is on.
    vtsidecar.note_session_end(key)


def _reset_ring(key: str) -> None:
    """Drop the retained scrollback CONTENT (in-memory ring + on-disk mirror) while keeping the
    monotonic ``_TOTALS`` offset. Called on a WIDTH change (#244) so the ring never holds bytes
    written at a width different from the one a later same-width attach will replay at — which
    would re-garble (Hermes #245). The agent repaints fresh at the new width; ``_TOTALS`` stays
    monotonic so delta-resume offsets remain valid. (The width-aware screen model #242 makes this
    unnecessary by re-rendering history at any width.)"""
    _BUFFERS[key] = bytearray()
    with contextlib.suppress(OSError):
        _scrollback_path(key).unlink()
    _LOADED_FROM_DISK.add(key)  # don't re-hydrate the now-removed file


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


def _enforce_buffer_cap() -> None:
    """Bound the number of retained buffers — but only by evicting buffers whose dtach
    master is GONE. A live session's scrollback is never evicted (an idle/attached
    session produces no output to refresh its LRU recency, yet still needs the buffer
    for delta-resume — the bug Hermes caught). So the cap reclaims dead/orphan buffers
    only; concurrent *live* sessions are all retained (their memory is legitimate and
    bounded by real concurrency), and dead ones are normally reaped eagerly at
    end-of-run via `_maybe_evict_ended`.
    """
    while len(_BUFFERS) > _MAX_BUFFERS:
        victim = next((k for k in _BUFFERS if not _session_alive(k)), None)
        if victim is None:
            break  # everything retained is live — keep it all
        _drop_buffer(victim)


def _buffer_append(key: str, data: bytes) -> None:
    _ensure_loaded(key)  # hydrate prior scrollback from disk before the first append (#206)
    buf = _BUFFERS.get(key)
    if buf is None:
        buf = bytearray()
        _BUFFERS[key] = buf
    _BUFFERS.move_to_end(key)  # most-recently-used
    buf.extend(data)
    # Feed the VT live mirror (#273) the same bytes, in order. No-op unless the flag is on AND a
    # client has opened the session (note_resize), so detached-but-unviewed sessions cost nothing.
    # This is the single chokepoint for ALL agent output — both the attached WS pump and the
    # server-owned SessionStream drain land here — so the mirror stays current either way.
    vtsidecar.note_feed(key, data)
    _persist_append(key, data)  # mirror to disk so scrollback survives a restart (#206)
    _TOTALS[key] = _TOTALS.get(key, 0) + len(data)
    _TOTALS.move_to_end(key)
    # Skip the working-signal stamp while inside the post-attach replay grace (#195):
    # the screen-redraw burst is not new agent activity. Scrollback (buf/_TOTALS) is
    # always updated so a reattach still resumes the full screen.
    now = time.time()
    if now >= _SUPPRESS_OUTPUT_UNTIL.get(key, 0.0):
        _LAST_OUTPUT_AT[key] = now
        _LAST_OUTPUT_AT.move_to_end(key)
    if len(buf) > _MAX_BUF:
        del buf[: len(buf) - _MAX_BUF]
    _enforce_buffer_cap()


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


def _is_same_width_continuation(have: int, buffer_cols: int | None, cols: int) -> bool:
    """Whether a reconnect can be satisfied with the raw byte-delta instead of re-rendering the
    scroll-up (#262). True only when the client already holds matching-width scrollback — it sent a
    real in-ring offset (``have>0``) AND its width equals the width we last served it
    (``buffer_cols==cols``), i.e. a brief WS blip on a live same-width session. Everything else —
    a fresh load (``have<=0``), a cross-width client, or a post-restart reconnect where
    ``_LAST_COLS`` was wiped (``buffer_cols`` is None) — is False, so the caller renders the
    width-correct transcript rather than replaying the fixed-width raw ring at a wrong width."""
    return have > 0 and buffer_cols == cols


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
_ENGINE_LABEL = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "opencode"}


def _transcript_payload(buf_key: str, cols: int, rows: int = 24) -> bytes | None:
    """Render the engine's saved conversation transcript as a fresh-load scroll-up payload (#242):
    ``clear + rendered conversation + a full viewport of blank lines``. The blank lines push the
    WHOLE transcript into xterm's scrollback (above the visible screen) so the live agent repaints
    its current frame into a BLANK viewport — its repaint can't overwrite the transcript (the
    "half a page or nothing" bug, #301). Scroll-up then lands straight on the transcript.

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
        body = transcript.render(
            turns, cols, assistant_label=_ENGINE_LABEL.get(prov.engine_id, prov.engine_id)
        )
    except Exception:
        return None
    if not body:
        return None
    # Clear, then the rendered conversation, then a FULL viewport of blank lines so the whole
    # transcript scrolls up into xterm's scrollback. The live agent repaints into the blank viewport
    # below — it can't clobber the transcript above (#301). Scroll-up lands on the transcript.
    return _CLEAN_LOAD_CLEAR + body + b"\r\n" * max(1, rows)


async def _vt_snapshot_payload(buf_key: str, cols: int, rows: int) -> bytes | None:
    """Path B faithful scroll-up (#271/#273): snapshot the session's LIVE mirror emulator — the
    persistent emulator fed the agent's PTY output incrementally and resized in step with the agent
    (see ``vtsidecar`` + ``_buffer_append``) — reflowed to the client width and framed like the
    transcript payload (clear + rows + blank line).

    The mirror is the only faithful source: it processed every byte in order at the agent geometry,
    so Ink's repaints overwrote in place and scrollback holds each line once (no duplication). The
    superseded one-shot ``rebuild`` from the saved ring could not — the ring is a mixed-geometry
    soup of repaints and duplicated.

    Flag-gated + fail-safe: returns ``None`` when the flag is off, the session isn't mirrored yet
    (cold — e.g. just deployed / never attached this process), it's alt-screen, or the sidecar is
    unhealthy/slow — so the caller falls back to the (clean) transcript path, NEVER a dup-prone
    ring replay. Synthetic scroll-up; the caller leaves ``_TOTALS``/``have``/``seq`` untouched."""
    if not vtsidecar.enabled():
        return None
    _ensure_loaded(buf_key)
    ring = bytes(_BUFFERS.get(buf_key) or b"")
    if not ring or _in_alt_screen(ring):
        return None  # nothing to show / alt-screen TUIs repaint themselves
    snap = await vtsidecar.live_snapshot(buf_key, cols, rows)
    if not snap:
        return None  # cold mirror → caller falls back to the clean transcript, not a dup replay
    return _CLEAN_LOAD_CLEAR + snap + b"\r\n"
