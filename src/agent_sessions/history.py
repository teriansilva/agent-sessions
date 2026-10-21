"""Paged transcript history for scroll-up lazy-load (issue #348 Phase 3).

The attach payload delivers a bounded slice of the engine's saved conversation
(:mod:`transcript`); everything older is reachable only on demand. This module pages
BACKWARDS over the transcript adapter's turn list so the client can lazy-load older
content when the user scrolls to the top of the viewport.

Cursor contract (the issue's hard requirement): a cursor is a **turn index into the
adapter's output** — ``cursor == N`` means "turns[:N] are older than everything already
delivered". It is engine-native and width-independent: re-requesting the same cursor at
a different ``cols`` yields the same TURNS (re-wrapped), never a shifted window. It is
deliberately NOT a rendered-line offset — those move with width and render caps.

Width independence is structural, not incidental (Hermes #365 review): a page consumes a
FIXED NUMBER OF TURNS (``AGENT_SESSIONS_HISTORY_PAGE_TURNS``, default 50) — turn selection
never consults the rendered size, so the same ``before`` selects the same turn window and
yields the same next cursor at every ``cols``. (The earlier design walked turns until a
rendered line/byte budget was hit; long wrapping turns then made the SAME cursor cover
different turn sets at different widths, so a resize could skip/duplicate pages.)

The FIRST cursor is exported by the attach, not guessed here (Hermes #365 r2): a transcript
attach sends ``{"t":"hist","cursor":N}`` (its renderer's exact turn boundary, see
``transcript.render_with_boundary``), and the seeded client always passes ``before=``.
``before=None`` survives only as the width-independent approximate fallback for clients
that never got that frame — see :func:`fetch_page`.

Render-output caps (so scroll-up can't queue unbounded threadpool work):
- lines per page: capped at ``AGENT_SESSIONS_HISTORY_PAGE_LINES`` (default 500);
- rendered bytes per page: capped at ``AGENT_SESSIONS_HISTORY_PAGE_BYTES`` (default
  512 KiB).
Both caps truncate the RENDERED TEXT oldest-first and never move the cursor — a page
whose window renders over-cap simply shows its newest turns, while the cursor still
steps past the whole window. The newest turn of a window is always served (bounded by
the lines cap), so a page is never empty for a non-empty window.

Concurrency control (single in-flight render per session) lives in the route
(:mod:`routes.history`); this module is pure and synchronous so the route can run it
in the thread pool, like the attach-time ``_transcript_payload``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import transcript


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def page_turns() -> int:
    """Turns consumed per page (env-overridable) — the WIDTH-INDEPENDENT cursor step.

    Turn selection depends on nothing but this count and ``before``, so the cursor
    sequence is identical at every ``cols`` (Hermes #365 finding 1)."""
    return _env_int("AGENT_SESSIONS_HISTORY_PAGE_TURNS", 50)


def page_lines_cap() -> int:
    """Hard ceiling on rendered lines per page (env-overridable). Render-output cap
    ONLY — truncates oldest-first, never moves the cursor."""
    return _env_int("AGENT_SESSIONS_HISTORY_PAGE_LINES", 500)


def page_bytes_cap() -> int:
    """Hard ceiling on rendered bytes per page (env-overridable). Render-output cap
    ONLY — truncates oldest-first, never moves the cursor."""
    return _env_int("AGENT_SESSIONS_HISTORY_PAGE_BYTES", 512 * 1024)


@dataclass
class HistoryPage:
    """One rendered page of older history.

    ``ansi``: the rendered block (UTF-8 ANSI text, oldest line first).
    ``cursor``: the next-older cursor to request, or ``None`` at the oldest turn.
    ``has_more``: whether older turns remain before this page.
    """

    ansi: bytes
    cursor: int | None
    has_more: bool


def _line_count(rendered: bytes) -> int:
    """Lines in a rendered block (``transcript.render`` joins lines with CRLF)."""
    return rendered.count(b"\r\n") + 1 if rendered else 0


def load_turns(engine_id: str, native_id: str, home: Path | None = None) -> list | None:
    """The engine's full adapter output for ``native_id`` — the list the cursor indexes
    into. ``None`` when the engine has no transcript adapter (→ the route answers the
    empty no-history shape); ``[]`` on a read error (fail-soft, same as attach)."""
    adapter = transcript.adapter_for(engine_id)
    if adapter is None:
        return None
    try:
        return adapter(native_id, home or Path.home())
    except Exception:
        return []


def fetch_page(
    engine_id: str,
    native_id: str,
    *,
    before: int | None = None,
    cols: int = 80,
    lines: int | None = None,
    home: Path | None = None,
) -> HistoryPage:
    """Load the adapter turns and render one page of history ending at ``before``.

    The page's turn window is FIXED-SIZE (:func:`page_turns`) — width-independent by
    construction — and the cursor always steps past the whole window. The lines/bytes
    caps then bound only the RENDERED OUTPUT, dropping the window's oldest renders
    first. Synchronous + read-only — run it in the thread pool.

    ``before=None`` is the no-shared-coordinates FALLBACK: a transcript attach exports
    its exact turn boundary to the client via the ``{"t":"hist","cursor":N}`` control
    frame (see ``scrollback._transcript_payload`` / ``webterm``), so a seeded client
    always sends ``before=``. Only a client that never received that frame (the attach
    came from the VT mirror, a same-width ring continuation, or a clean-load clear —
    no turn coordinate system exists) omits it, and then the boundary is APPROXIMATE
    by design: everything older than the newest page-sized window. Crucially it is
    WIDTH-INDEPENDENT — it consults only the turn count, never rendered line counts,
    so a resize between attach and first lazy-load can shift it by nothing (the
    pre-fix ``initial_cursor`` walked rendered lines at the request width and could
    skip turns; Hermes #365 r2 finding 1). The accepted cost is up to one page of
    overlap with content already on screen.
    """
    turns = load_turns(engine_id, native_id, home)
    if not turns:  # no adapter (None) and empty/unreadable transcript look the same here
        return HistoryPage(b"", None, False)
    cols = max(20, min(500, cols))
    cap = page_lines_cap()
    want_lines = min(lines, cap) if lines else cap
    max_bytes = page_bytes_cap()

    end = max(0, len(turns) - page_turns()) if before is None else max(0, min(before, len(turns)))
    if end <= 0:
        return HistoryPage(b"", None, False)
    # Width-independent turn selection: a page is exactly page_turns() turns (fewer only
    # at the oldest end). The cursor NEVER depends on the rendered size below.
    start = max(0, end - page_turns())

    # Render the window newest-turn-first, keeping renders while they fit the output
    # caps; once a cap is hit the remaining (older) renders are dropped — render-level
    # truncation only, the cursor still points at `start`. The newest non-empty render
    # is always kept (itself bounded by the lines cap), so the page makes visible
    # progress even for one huge turn.
    chunks: list[bytes] = []  # newest-first while walking; reversed for output
    used_lines = 0
    used_bytes = 0
    for i in range(end, start, -1):
        chunk = transcript.render([turns[i - 1]], cols, max_lines=want_lines)
        if not chunk:
            continue
        n = _line_count(chunk)
        if chunks and (used_lines + n > want_lines or used_bytes + len(chunk) > max_bytes):
            break  # output caps spent — older renders of THIS window are truncated away
        chunks.append(chunk)
        used_lines += n
        used_bytes += len(chunk) + 2  # +2 for the CRLF join
        if used_lines >= want_lines or used_bytes >= max_bytes:
            break
    body = b"\r\n".join(reversed(chunks))
    return HistoryPage(body, start if start > 0 else None, start > 0)
