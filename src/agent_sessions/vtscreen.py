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
from typing import NamedTuple

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

# The string controls as whole sequences, for `has_visible_bytes`: the first two `_TOKEN`
# alternatives, with the terminator optional for the same reason.
_STRING_CONTROL = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[P^_X][^\x1b]*(?:\x1b\\)?")


def has_visible_bytes(data: bytes) -> bool:
    """Does this output chunk carry anything besides string controls (OSC / DCS / PM / APC / SOS)?

    A window-title write is output, but it changes no cell on the screen. A codex session waiting
    on the operator rewrites its title about once a second (#969), so counting that as activity
    marks exactly the sessions that need a decision as busy. Everything else — text, CSI cursor
    moves, erases — counts, because any of it can change what the screen shows.

    Called per output chunk, so the common case (no introducer at all) is a byte scan, not a regex.
    A control string split across two chunks leaves its tail looking visible; that errs towards
    "busy", which only ever withholds a proposal.
    """
    if not data:
        return False
    if b"\x1b" not in data or not any(i in data for i in _INTRODUCERS):
        return True
    return bool(_STRING_CONTROL.sub(b"", data))


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
    """A character grid with a cursor. Cells only, no scrollback — and, OPT-IN, two colour layers.

    ``colours=True`` (#1213) keeps each cell's foreground and background SGR colour beside its
    character. It exists for exactly one reader: a TUI whose selection is drawn ONLY as colour
    (opencode's permission dialog highlights the chosen option by its background), where the text
    grid alone cannot say which option the cursor is on. Off, the colour grids are never allocated
    and every existing render is byte-identical.
    """

    __slots__ = (
        "rows",
        "cols",
        "grid",
        "row",
        "col",
        "saved",
        "fg",
        "bg",
        "fgs",
        "bgs",
        "in_alt",
        "main",
        "untrusted",
    )

    def __init__(self, rows: int, cols: int, *, colours: bool = False) -> None:
        self.rows = rows
        self.cols = cols
        self.grid: list[list[str]] = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.saved = (0, 0)
        # The current pen, as the SGR colour it names (`"2;245;167;66"`, `"5;208"`, `"33"`), or
        # "" for the terminal default. Only kept when `colours` is on.
        self.fg = ""
        self.bg = ""
        self.fgs: list[list[str]] | None = [[""] * cols for _ in range(rows)] if colours else None
        self.bgs: list[list[str]] | None = [[""] * cols for _ in range(rows)] if colours else None
        # The alternate screen (#1218 review): only modelled in colours mode, which is the
        # authorisation read. `main` holds the normal buffer while the alternate one is up.
        self.in_alt = False
        self.main: tuple | None = None
        #: Why this frame cannot be trusted for authorisation, or None (colours mode only).
        self.untrusted: str | None = None

    def alt(self, on: bool) -> None:
        """DECSET/DECRST 1049 / 1047 / 47 — switch between the normal and alternate buffers.

        Entering saves the normal buffer and starts a blank alternate one. Leaving restores the
        normal buffer — whatever the alternate screen showed (a dialog) is GONE from view. Leaving
        an alternate screen whose entry this slice never saw (the ring was cut inside it) cannot
        restore anything true, so the screen is blanked: unknown, never the old dialog.
        """
        if on:
            if not self.in_alt:
                self.main = (self.grid, self.fgs, self.bgs, self.row, self.col)
                self.in_alt = True
            self._blank_all()
            return
        if self.in_alt and self.main is not None:
            self.grid, self.fgs, self.bgs, self.row, self.col = self.main
        else:
            self._blank_all()
        self.in_alt = False
        self.main = None

    def _blank_all(self) -> None:
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        if self.fgs is not None and self.bgs is not None:
            self.fgs = [[""] * self.cols for _ in range(self.rows)]
            self.bgs = [[""] * self.cols for _ in range(self.rows)]

    def sgr(self, params: bytes) -> None:
        """Track the pen's colours from one SGR sequence. Only called when `colours` is on.

        Colours only: bold, underline, reverse and the rest do not move a cell's colour, which is
        all a colour-drawn selection needs. Colon sub-parameters are read as semicolons."""
        try:
            ps = [int(p) if p else 0 for p in params.replace(b":", b";").split(b";")]
        except ValueError:
            return
        if not ps:
            ps = [0]
        i = 0
        while i < len(ps):
            p = ps[i]
            if p == 0:
                self.fg = self.bg = ""
            elif p in (38, 48):
                mode = ps[i + 1] if i + 1 < len(ps) else None
                if mode == 2 and i + 4 < len(ps):
                    val = "2;" + ";".join(str(x) for x in ps[i + 2 : i + 5])
                    i += 4
                elif mode == 5 and i + 2 < len(ps):
                    val = f"5;{ps[i + 2]}"
                    i += 2
                else:
                    return  # malformed: stop rather than misread the rest as attributes
                if p == 38:
                    self.fg = val
                else:
                    self.bg = val
            elif p == 39:
                self.fg = ""
            elif p == 49:
                self.bg = ""
            elif 30 <= p <= 37 or 90 <= p <= 97:
                self.fg = str(p)
            elif 40 <= p <= 47 or 100 <= p <= 107:
                self.bg = str(p)
            i += 1

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
        if self.fgs is not None and self.bgs is not None:
            self.fgs.pop(0)
            self.fgs.append([""] * self.cols)
            self.bgs.pop(0)
            self.bgs.append([""] * self.cols)
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
                if self.fgs is not None and self.bgs is not None:
                    self.fgs[self.row][self.col] = self.fg
                    self.bgs[self.row][self.col] = self.bg
                self.col += 1

    def _erase_row(self, row: int, start: int, end: int) -> None:
        line = self.grid[row]
        for c in range(max(0, start), min(self.cols, end)):
            line[c] = " "
        if self.fgs is not None and self.bgs is not None:
            # An erase paints the CURRENT background (ECMA-48 "background colour erase").
            for c in range(max(0, start), min(self.cols, end)):
                self.fgs[row][c] = ""
                self.bgs[row][c] = self.bg

    def _blank_rows(self, start: int, end: int) -> None:
        for r in range(start, end):
            self.grid[r] = [" "] * self.cols
            if self.fgs is not None and self.bgs is not None:
                self.fgs[r] = [""] * self.cols
                self.bgs[r] = [self.bg] * self.cols

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
                self._blank_rows(self.row + 1, self.rows)
            elif n == 1:  # start of screen → cursor
                self._blank_rows(0, self.row)
                self._erase_row(self.row, 0, self.col + 1)
            else:  # 2 / 3 — whole screen
                self._blank_rows(0, self.rows)
        elif final == "K":
            if n == 0:
                self._erase_row(self.row, self.col, self.cols)
            elif n == 1:
                self._erase_row(self.row, 0, self.col + 1)
            else:
                self._erase_row(self.row, 0, self.cols)
        elif final == "s":  # SCOSC — save cursor
            self.save_cursor()
        elif final == "u":  # SCORC — restore cursor
            self.restore_cursor()
        # Everything else (SGR `m`, DECSTBM `r`, …) leaves cells alone.

    def _kept_rows(self) -> tuple[int, int]:
        """The row span `display` keeps: blank leading and trailing rows trimmed."""
        texts = ["".join(row).rstrip() for row in self.grid]
        end = len(texts)
        while end and not texts[end - 1]:
            end -= 1
        start = 0
        while start < end and not texts[start]:
            start += 1
        return start, end

    def display(self) -> str:
        start, end = self._kept_rows()
        return "\n".join("".join(row).rstrip() for row in self.grid[start:end])

    def cells(self) -> list[Cells]:
        """The rows `display` keeps, each with its per-character colours (colours mode only)."""
        assert self.fgs is not None and self.bgs is not None
        start, end = self._kept_rows()
        out: list[Cells] = []
        for r in range(start, end):
            text = "".join(self.grid[r]).rstrip()
            out.append(
                Cells(text, tuple(self.fgs[r][: len(text)]), tuple(self.bgs[r][: len(text)]))
            )
        return out


class Cells(NamedTuple):
    """One rendered row: its text and, per character, the SGR foreground and background colour
    (`""` = terminal default). The three are always the same length."""

    text: str
    fg: tuple[str, ...]
    bg: tuple[str, ...]


# ---- the FAIL-CLOSED allow-list for the authorisation read (#1213, #1218 review 5405) -----------
#
# `render` skips what it does not model, which is right for a reviewer's text. An ANSWER must not
# be authorised against a frame that some unmodelled control may have changed (scrolled away,
# deleted, switched buffers): so in colours mode every token is checked against what this module
# models exactly, and ANY other one marks the whole frame untrusted — no dialog, no answer.
#
# The reference terminal is the app's own (xterm.js), and the list was measured against every
# claude/opencode ring on the host: string controls (OSC/APC/PM/SOS) draw nothing there; the
# queries and input/output modes below move no cell.

#: DEC private modes that change no cell: cursor keys/blink/visibility, mouse, focus, paste,
#: synchronized output, grapheme/theme reports.
_SAFE_DEC_MODES = frozenset(
    {1, 12, 25, 1000, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 2004, 2026, 2027, 2031}
)
#: The alternate screen, modelled by `_Screen.alt`.
_ALT_DEC_MODES = frozenset({47, 1047, 1049})
#: Positional/erase/SGR finals `_Screen.csi` / `sgr` model exactly (no prefix, no intermediate).
_MODELLED_FINALS = frozenset(b"HfABCDGdJKm")


def _dec_modes(params: bytes) -> list[int] | None:
    try:
        return [int(p) for p in params.split(b";") if p]
    except ValueError:
        return None


def _untrusted(m: re.Match) -> str | None:
    """Why this token makes a coloured frame untrusted, or None when it is modelled or inert."""
    tok = m.group(0)
    final = m.group("csi_final")
    if final:
        params = m.group("csi_params") or b""
        inter = tok[2 + len(params) : -1]
        pre = params[:1] if params[:1] in (b"?", b">", b"<", b"=") else b""
        if pre == b"?":
            body = params[1:]
            if final in b"hl" and not inter:
                modes = _dec_modes(body)
                if modes is not None and all(
                    n in _SAFE_DEC_MODES or n in _ALT_DEC_MODES for n in modes
                ):
                    return None
            if final == b"u" and not body and not inter:
                return None  # kitty keyboard query
            if final == b"p" and inter == b"$":
                return None  # DECRQM query
            return f"DEC {tok!r}"
        if pre:
            # modifyOtherKeys, XTVERSION, kitty keyboard flags, secondary DA: input/queries only.
            return None if final in b"mqucn" and not inter else f"CSI {tok!r}"
        if inter:
            return None if (inter == b" " and final == b"q") else f"CSI {tok!r}"
        if final[0] in _MODELLED_FINALS:
            return None
        if final in b"su" and not params:
            return None  # SCOSC / SCORC, modelled
        if final == b"c" and params in (b"", b"0"):
            return None  # primary DA query
        if final == b"n" and params in (b"5", b"6"):
            return None  # status / cursor-position report requests
        if final == b"t":
            modes = _dec_modes(params)
            # Reports and the title stack only; 1–10 would move or resize the window.
            return None if modes and modes[0] >= 11 else f"CSI {tok!r}"
        if final == b"r" and not params:
            return None  # DECSTBM reset to the full screen — the only region this models
        return f"CSI {tok!r}"
    if tok.startswith(b"\x1b]") or tok[:2] in (b"\x1b_", b"\x1b^", b"\x1bX"):
        return None  # OSC / APC / PM / SOS draw no cell in the reference terminal
    if tok.startswith(b"\x1bP"):
        return None if tok[2:4] in (b"+q", b"$q") else f"DCS {tok[:8]!r}"
    if m.group("decsc"):
        return None
    if tok == b"\x1b(B":
        return None  # G0 = ASCII
    if tok in (b"\x07", b"\x0f", b"\x00"):
        return None  # BEL; SI (back to G0); NUL
    return f"control {tok[:8]!r}"


def _feed_screen(data: bytes, rows: int, cols: int, *, colours: bool = False) -> _Screen:
    screen = _Screen(rows, cols, colours=colours)
    pos = 0
    for m in _TOKEN.finditer(data):
        if m.start() > pos:
            screen.write(data[pos : m.start()])
        pos = m.end()
        if colours and screen.untrusted is None:
            screen.untrusted = _untrusted(m)
        final = m.group("csi_final")
        if final:
            params = m.group("csi_params") or b""
            if colours and final == b"m" and params[:1] not in (b"?", b">", b"<", b"="):
                screen.sgr(params)
                continue
            if colours and final in (b"h", b"l") and params[:1] == b"?":
                # Numerically: `?01049l` is `?1049l` (#1218 review 5405).
                if set(_dec_modes(params[1:]) or ()) & _ALT_DEC_MODES:
                    screen.alt(final == b"h")
                continue
            screen.csi(params, final.decode("ascii", "replace"))
            continue
        decsc = m.group("decsc")
        if decsc:
            screen.save_cursor() if decsc == b"7" else screen.restore_cursor()
    if pos < len(data):
        screen.write(data[pos:])
    return screen


def _feed(data: bytes, rows: int, cols: int) -> str:
    return _feed_screen(data, rows, cols).display()


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


def render_cells(data: bytes, rows: int, cols: int) -> list[Cells]:
    """The CURRENT grid with each character's colours (#1213) — and nothing else.

    Same frame and trimming as :func:`render`, but deliberately WITHOUT its empty-frame fallback:
    `render` may show the frame from before a trailing full-screen erase (a display nicety for a
    reviewer), whereas this is what an ANSWER is authorised against, and a screen that was just
    cleared shows no dialog to answer (#1218 review). ``[]`` when the current grid is empty, which
    is also the only case where the two differ — the tests pin both halves.
    """
    rows = max(1, min(MAX_ROWS, rows))
    cols = max(1, min(MAX_COLS, cols))
    if not data:
        return []
    screen = _feed_screen(data, rows, cols, colours=True)
    if screen.untrusted is not None:
        return []  # something this renderer does not model may have changed the screen
    cells = screen.cells()
    return cells if any(c.text.strip() for c in cells) else []
