"""Bounded VT screen renderer for the AI-review live tail (#611).

The review engine needs "what is on the terminal right now", but the scrollback ring holds
raw PTY bytes. Deleting the escape sequences with a regex (the pre-#611 behaviour) does not
undo the overwrites those escapes encode, so an agent that repaints in place — codex's
per-character spinner is the worst case — reaches the model as literal debris::

    Working•4orking•rking•king•ingng5WWo•Wor•Work•WorkiWorkin•Working

which reads exactly like "the agent is typing random characters". Replaying the bytes
through a screen model instead yields the frame a human would see.

Scope, deliberately narrow:

* Only the sequences real agent TUIs use to position and erase — CUP/HVP, CUU/CUD/CUF/CUB,
  CHA, VPA, ED, EL — plus ``\\r`` / ``\\n`` / ``\\t`` and bottom-row scrolling. SGR (colour),
  DECSET/DECRST private modes, OSC strings, scroll-region (DECSTBM) and charset selects are
  recognised only so they can be *skipped*: none of them changes which character sits in
  which cell, which is all the reviewer needs.
* No reflow, ever. The grid is rendered at the width the bytes were authored at
  (``scrollback._LAST_COLS``), which is why this does not repeat the failure of the reverted
  pyte-for-scroll-up attempt (PR #248/#249) — that tried to reflow absolute-positioned
  history to a *different* width, which cannot work.
* No scrollback history. Lines that scroll off the top are dropped; the conversation they
  held is what the engine's saved transcript is for (``transcript.py``). This module answers
  one question: what does the current screen say.

``pyte`` was measured against a live codex ring before this module was written: it raised
``TypeError`` inside its own CSI dispatch on the real byte stream (its FSM desyncs on the
``ESC [ 0 SP q`` cursor-style sequence codex emits every frame) and ran ~3.5× slower. A
renderer we own, that skips what it does not model instead of failing on it, is the smaller
and safer dependency.
"""

from __future__ import annotations

import re

# One pass over the bytes. Order matters, and two of the alternatives are load-bearing for
# reasons that are not obvious:
#
# * The string controls (OSC / DCS / APC / PM / SOS) carry a payload terminated by BEL or ST.
#   Their terminator is OPTIONAL here: an arbitrary tail slice of a live ring routinely cuts a
#   control string in half, and a pattern that insists on the terminator simply fails to match —
#   leaving the ESC to be eaten as a stray C0 and the payload to render as visible text. That is
#   how `\x1b]52;c;<base64>` (OSC 52, the clipboard) reached the model as `]52;c;<base64>`. A
#   control-string payload is never screen content, so it is consumed to the terminator or to the
#   end of the slice, whichever comes first.
# * They must precede the generic ESC-single alternative, whose `[@-Z\\^_]` class would otherwise
#   claim the `P` of a DCS (and `X`, `^`, `_`), dropping two bytes and leaking the rest.
#
# The bare-ESC / C0 catch-all stays last so a malformed escape is dropped rather than leaking its
# parameter bytes into the visible text.
_TOKEN = re.compile(
    rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC … BEL / ST / unterminated
    rb"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"  # DCS / PM / APC / SOS … ST / unterminated
    rb"|\x1b\[(?P<csi_params>[0-9;:?<=>]*)[ -/]*(?P<csi_final>[@-~])"  # CSI
    rb"|\x1b(?P<decsc>[78])"  # DECSC (save cursor) / DECRC (restore)
    rb"|\x1b[@-Z\\^_]"  # other ESC singles
    rb"|\x1b[()][0-9A-Za-z]"  # charset designation
    rb"|\x1b[ #%].?"  # ESC + intermediate
    rb"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # remaining C0, except \t \n \r
)

# A full-screen erase as the LAST thing in the captured bytes means we sliced the ring
# mid-repaint: the agent cleared the screen and the redraw had not been written yet. The
# honest frame in that case is the one immediately before the erase.
_FULL_ERASE = re.compile(rb"\x1b\[2J|\x1b\[H\x1b\[J")

# Guard rails. Both are already enforced upstream (webterm clamps a resize to 300×500) but a
# stale sidecar value must never be able to allocate an unbounded grid here.
MAX_ROWS = 300
MAX_COLS = 500

# Absolute row addressing — ``CSI <row> ; <col> H`` (or ``f``). Used to recover the screen
# height when the app never observed a resize (see `infer_rows`).
_CUP_ROW = re.compile(rb"\x1b\[(\d+);\d*[Hf]")

# The string controls: an introducer, an arbitrary payload, and a terminator (BEL or ST).
# OSC ] · DCS P · PM ^ · APC _ · SOS X.
_INTRODUCERS = (b"\x1b]", b"\x1bP", b"\x1b^", b"\x1b_", b"\x1bX")


def starts_inside_control_string(buf: bytes, start: int) -> bool:
    """Does byte offset ``start`` of ``buf`` fall INSIDE an unterminated control string?

    A parser handed only ``buf[start:]`` cannot answer this: the introducer is behind the cut,
    so the payload that follows looks exactly like ordinary text. That is how a clipboard write
    (``ESC ] 52 ; c ; <base64>``) whose introducer sat just before the review's tail slice
    reached the model as visible text. The caller owns the whole ring, so it can look back.

    Cheap: a handful of ``rfind``s over the prefix, no copy and no scan of the payload. The
    introducer search deliberately runs one byte past ``start`` so an introducer *straddling*
    the cut (``ESC`` at ``start - 1``, ``]`` at ``start``) is still seen.
    """
    if start <= 0:
        return False
    intro = max(buf.rfind(i, 0, start + 1) for i in _INTRODUCERS)
    if intro < 0:
        return False
    # Terminated before the cut ⇒ we are outside it. `\x1b\\` (ST) or a bare BEL closes it.
    return max(buf.rfind(b"\x07", intro, start), buf.rfind(b"\x1b\\", intro, start)) < 0


def drop_open_control_prefix(data: bytes) -> bytes:
    """Discard the leading fragment of a control-string payload from ``data``.

    Called only when :func:`starts_inside_control_string` says the slice began mid-payload. The
    payload ends at the first BEL, at ST, or — if the agent abandoned the string — at the first
    ESC, which necessarily begins a new sequence (an OSC/DCS payload cannot itself contain ESC).
    Whichever comes first wins, so no payload byte survives. A slice that is payload end-to-end
    yields ``b""``: better to review nothing than to review a secret.
    """
    bel = data.find(b"\x07")
    esc = data.find(b"\x1b")
    if bel != -1 and (esc == -1 or bel < esc):
        return data[bel + 1 :]
    if esc == -1:
        return b""
    if data[esc : esc + 2] == b"\x1b\\":  # ST — consume both bytes
        return data[esc + 2 :]
    return data[esc:]  # abandoned control string; parsing is sound from here


def infer_rows(data: bytes) -> int:
    """The screen height ``data`` was drawn against, read off the stream itself: the tallest
    absolute row the agent addressed. ``0`` when it never positioned the cursor absolutely
    (a purely line-oriented stream, where the caller has no reason to render a grid).

    ``scrollback._LAST_ROWS`` is in-memory only, so a server restart with no browser attached
    leaves the height unknown while the persisted ``.cols`` sidecar still gives the width.
    Rendering at the wrong height silently duplicates an agent's cursor-up repaints, so guess
    from evidence rather than from a default.
    """
    best = 0
    for m in _CUP_ROW.finditer(data):
        row = int(m.group(1))
        if row > best:
            best = row
    return min(best, MAX_ROWS)


class _Screen:
    """A character grid with a cursor. Cells only — no attributes, no scrollback."""

    __slots__ = ("rows", "cols", "grid", "row", "col", "saved")

    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self.grid: list[list[str]] = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.saved = (0, 0)

    def save_cursor(self) -> None:
        self.saved = (self.row, self.col)

    def restore_cursor(self) -> None:
        self.row, self.col = self.saved
        self._clamp()

    def _clamp(self) -> None:
        self.row = max(0, min(self.rows - 1, self.row))
        self.col = max(0, min(self.cols, self.col))

    def _scroll(self) -> None:
        self.grid.pop(0)
        self.grid.append([" "] * self.cols)
        self.row = self.rows - 1

    def _newline(self) -> None:
        self.row += 1
        if self.row >= self.rows:
            self._scroll()

    def write(self, data: bytes) -> None:
        for ch in data.decode("utf-8", "replace"):
            if ch == "\n":
                self._newline()
            elif ch == "\r":
                self.col = 0
            elif ch == "\t":
                self.col = min(self.cols, (self.col // 8 + 1) * 8)
            else:
                if self.col >= self.cols:  # autowrap
                    self.col = 0
                    self._newline()
                self.grid[self.row][self.col] = ch
                self.col += 1

    def _erase_row(self, row: int, start: int, end: int) -> None:
        line = self.grid[row]
        for c in range(max(0, start), min(self.cols, end)):
            line[c] = " "

    def csi(self, params: bytes, final: str) -> None:
        # Private (``?``) and secondary (``>``/``<``/``=``) parameter forms are DECSET/DECRST
        # and device queries — mode state, never cell content. Skip them wholesale.
        if params[:1] in (b"?", b">", b"<", b"="):
            return
        try:
            ps = [int(p) if p else 0 for p in params.split(b";")] if params else [0]
        except ValueError:
            return  # a colon-separated SGR sub-parameter; nothing positional to do
        n = ps[0] if ps else 0
        if final in ("H", "f"):
            self.row = (n or 1) - 1
            self.col = (ps[1] - 1) if len(ps) > 1 and ps[1] else 0
            self._clamp()
        elif final == "A":
            self.row -= max(1, n)
            self._clamp()
        elif final == "B":
            self.row += max(1, n)
            self._clamp()
        elif final == "C":
            self.col += max(1, n)
            self._clamp()
        elif final == "D":
            self.col -= max(1, n)
            self._clamp()
        elif final == "G":
            self.col = (n or 1) - 1
            self._clamp()
        elif final == "d":
            self.row = (n or 1) - 1
            self._clamp()
        elif final == "J":
            if n == 0:  # cursor → end of screen
                self._erase_row(self.row, self.col, self.cols)
                for r in range(self.row + 1, self.rows):
                    self.grid[r] = [" "] * self.cols
            elif n == 1:  # start of screen → cursor
                for r in range(self.row):
                    self.grid[r] = [" "] * self.cols
                self._erase_row(self.row, 0, self.col + 1)
            else:  # 2 / 3 — whole screen
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "K":
            if n == 0:
                self._erase_row(self.row, self.col, self.cols)
            elif n == 1:
                self._erase_row(self.row, 0, self.col + 1)
            else:
                self.grid[self.row] = [" "] * self.cols
        elif final == "s":  # SCOSC — save cursor
            self.save_cursor()
        elif final == "u":  # SCORC — restore cursor
            self.restore_cursor()
        # Everything else (SGR `m`, DECSTBM `r`, …) leaves cells alone.

    def display(self) -> str:
        lines = ["".join(row).rstrip() for row in self.grid]
        while lines and not lines[-1]:
            lines.pop()
        while lines and not lines[0]:
            lines.pop(0)
        return "\n".join(lines)


def _feed(data: bytes, rows: int, cols: int) -> str:
    screen = _Screen(rows, cols)
    pos = 0
    for m in _TOKEN.finditer(data):
        if m.start() > pos:
            screen.write(data[pos : m.start()])
        pos = m.end()
        final = m.group("csi_final")
        if final:
            screen.csi(m.group("csi_params") or b"", final.decode("ascii", "replace"))
            continue
        decsc = m.group("decsc")
        if decsc:
            screen.save_cursor() if decsc == b"7" else screen.restore_cursor()
    if pos < len(data):
        screen.write(data[pos:])
    return screen.display()


def render(data: bytes, rows: int, cols: int) -> str:
    """Replay ``data`` onto a ``rows × cols`` grid and return the visible frame as text.

    Blank leading/trailing rows are trimmed and every row is right-stripped, so an idle
    agent yields a few short lines rather than a wall of padding. Returns ``""`` when the
    frame is empty — the caller decides what to do with that (``scrollback.live_tail_text``
    falls back to the plain escape-strip, so a stream this module cannot render can never
    deliver *less* than before).
    """
    rows = max(1, min(MAX_ROWS, rows))
    cols = max(1, min(MAX_COLS, cols))
    if not data:
        return ""
    out = _feed(data, rows, cols)
    if out.strip():
        return out
    # Empty frame: we almost certainly cut the ring between a full-screen erase and the
    # repaint that follows it. Re-render the last frame that completed before that erase.
    last = None
    for m in _FULL_ERASE.finditer(data):
        last = m
    if last is not None and last.start() > 0:
        out = _feed(data[: last.start()], rows, cols)
        if out.strip():
            return out
    return ""
