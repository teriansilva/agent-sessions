"""Width-aware terminal snapshot serializer (issue #242).

The durable half of the garbled-console fix (#227), after clean-load (#241).

`webterm` keeps a **raw PTY byte buffer** per session (#206) and on a fresh attach
replays it verbatim. Those bytes embed cursor-positioning written at whatever
width(s) the agent rendered at, so replaying them at a different client width
mis-positions everything — the "messy / chaotic" console. dtach is a dumb
passthrough with no screen model, so the server has nothing better to replay.

This module is the durable fix: a **pure render-on-attach transform**. Given the
raw buffer and the *attaching client's* `(cols, rows)`, it builds an ephemeral
`pyte` screen, feeds the raw bytes, and serializes the resulting **scrollback
history + current screen back to ANSI at that width**. The screen is discarded
after serializing — nothing is persisted, no long-lived emulator, no shared
mutable dimension (each attach renders independently at its own width, so
multi-client / secondary-tab widths can't poison anything — see #184).

Design notes / invariants (see issue #242):
- **Pure function.** `render_snapshot` reads only its arguments; it never touches
  `webterm` globals (`_TOTALS`, `_BUFFERS`, …). The caller is responsible for the
  `seq` offset contract: send this synthetic payload, then
  ``{"t":"seq","n":<real _TOTALS total>}`` so the client adopts the *real* byte
  offset and the synthetic payload's length never leaks into ``have``.
- **Bounded.** History rows fed into the model are capped (`MAX_HISTORY_ROWS`),
  and the serialized ANSI is hard-capped (`MAX_ANSI_BYTES`); over the cap we drop
  oldest history first, and if even the current screen won't fit we return
  ``None`` so the caller falls back to the #241 clean-load clear.
- **Honest fallback.** Any parse/serialize failure returns ``None`` (caller clears
  rather than show a misleading partial render). **Alt-screen TUIs (opencode) must
  not be passed here** — they carry no meaningful scrollback and repaint via
  SIGWINCH; the caller routes them to the existing no-replay path.

256-colour and truecolour both surface from pyte as a 6-hex string (e.g.
``"ff8700"``) and are emitted as truecolour SGR; the 8 named colours map to the
30–37 / 40–47 range. A clean current frame at the right width always beats
garbled history, so when in doubt we return ``None``.
"""

from __future__ import annotations

import contextlib

import pyte

# Max scrollback rows the ephemeral model retains (older lines drop). Distinct
# from `webterm._MAX_BUF` (raw bytes) and from the client xterm `scrollback`
# option, which must be >= this for the replayed history to be fully scrollable.
MAX_HISTORY_ROWS = 2000

# Hard cap on the serialized ANSI snapshot. Over this we drop oldest history;
# if the current screen alone still won't fit we return None (caller clears).
MAX_ANSI_BYTES = 256 * 1024

# Clear screen + scrollback + home, so the rendered snapshot starts from a clean
# base (matches webterm._CLEAN_LOAD_CLEAR).
_CLEAR = "\x1b[H\x1b[2J\x1b[3J"

# pyte names for the 8 base colours → SGR foreground codes (bg = +10). pyte uses
# "brown" for yellow. "default" and anything unrecognised fall back to 39 / 49.
_NAMED_FG = {
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
}
_HEXDIGITS = set("0123456789abcdefABCDEF")


def _is_hex_color(color: str) -> bool:
    return len(color) == 6 and all(c in _HEXDIGITS for c in color)


def _color_codes(color: str, *, bg: bool) -> list[int]:
    """SGR parameter list for one pyte colour value (fg unless ``bg``).

    Handles ``default``, the 8 base names (30–37 / 40–47), their ``bright*``
    variants (pyte names them ``brightred`` etc. → 90–97 / 100–107), and 256-colour
    / truecolour (both surface from pyte as a 6-hex string → truecolour SGR).
    """
    if color == "default":
        return [49 if bg else 39]
    if _is_hex_color(color):
        r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
        return [48 if bg else 38, 2, r, g, b]
    bright = color.startswith("bright")
    code = _NAMED_FG.get(color[len("bright") :] if bright else color)
    if code is None:  # unrecognised name → terminal default
        return [49 if bg else 39]
    if bright:
        code += 60  # 30–37 → 90–97 (bg then +10: 100–107)
    return [code + 10 if bg else code]


def _sgr(char: pyte.screens.Char) -> str:
    """Full SGR escape (``\\x1b[0;…m``) reproducing this cell's attributes.

    Always leads with a reset so the escape is absolute (no dependence on prior
    state) — emitted only when a cell's attributes differ from the previous one.
    """
    codes: list[int] = [0]
    if char.bold:
        codes.append(1)
    if char.italics:
        codes.append(3)
    if char.underscore:
        codes.append(4)
    if char.blink:
        codes.append(5)
    if char.reverse:
        codes.append(7)
    if char.strikethrough:
        codes.append(9)
    codes += _color_codes(char.fg, bg=False)
    codes += _color_codes(char.bg, bg=True)
    return "\x1b[" + ";".join(str(c) for c in codes) + "m"


def _attr_key(char: pyte.screens.Char) -> tuple:
    return (
        char.fg,
        char.bg,
        char.bold,
        char.italics,
        char.underscore,
        char.strikethrough,
        char.reverse,
        char.blink,
    )


_DEFAULT_KEY = ("default", "default", False, False, False, False, False, False)


def _render_line(row, cols: int) -> str:
    """Serialize one screen/history row (a col→Char mapping) to ANSI.

    Trailing default cells are trimmed to keep the snapshot small; SGR is emitted
    only on attribute change and reset at the line end so colour never bleeds into
    the next line's leading blanks. Wide-char continuation cells (empty ``data``)
    are skipped — the wide glyph itself already occupies the width.
    """
    # Last column that carries anything worth emitting (glyph or styled blank).
    last = -1
    for c in range(cols):
        ch = row[c]
        if (ch.data not in (" ", "")) or _attr_key(ch) != _DEFAULT_KEY:
            last = c
    if last < 0:
        return ""  # entirely blank, default-styled line

    out: list[str] = []
    cur = _DEFAULT_KEY
    for c in range(last + 1):
        ch = row[c]
        if ch.data == "":  # wide-char continuation — already covered by the glyph
            continue
        key = _attr_key(ch)
        if key != cur:
            out.append(_sgr(ch))
            cur = key
        out.append(ch.data or " ")
    if cur != _DEFAULT_KEY:
        out.append("\x1b[0m")  # don't let styling bleed past the line
    return "".join(out)


def render_snapshot(
    raw: bytes,
    cols: int,
    rows: int,
    *,
    max_history_rows: int = MAX_HISTORY_ROWS,
    max_ansi_bytes: int = MAX_ANSI_BYTES,
) -> bytes | None:
    """Render ``raw`` PTY output as ANSI reproducing screen + history at ``cols×rows``.

    Returns the snapshot bytes, or ``None`` when there's nothing to render or the
    snapshot can't be produced within bounds (caller falls back to clean-load).
    Pure: never mutates any external state.
    """
    if not raw or cols <= 0 or rows <= 0:
        return None
    try:
        screen = pyte.HistoryScreen(cols, rows, history=max_history_rows, ratio=0.5)
        stream = pyte.ByteStream(screen)
        stream.feed(raw)
        history_lines = [_render_line(r, cols) for r in screen.history.top]
        screen_lines = [_render_line(screen.buffer[y], cols) for y in range(rows)]
        cy = max(0, min(rows - 1, screen.cursor.y))
        cx = max(0, min(cols - 1, screen.cursor.x))
    except Exception:
        # Unsupported escape / wide-char / emulator edge: a clean clear beats a
        # misleading partial render. Caller falls back to #241 clean-load.
        return None

    cursor = f"\x1b[0m\x1b[{cy + 1};{cx + 1}H"

    def assemble(hist: list[str]) -> bytes:
        # History lines scroll into the client's scrollback; the trailing `rows`
        # screen lines stay visible. No newline after the last line so the visible
        # region is exactly the screen and absolute cursor addressing is correct.
        body = "\r\n".join(hist + screen_lines)
        return (_CLEAR + body + cursor).encode("utf-8", "replace")

    payload = assemble(history_lines)
    # Over the cap → drop oldest history first (most recent scrollback is the
    # most useful). Halve-then-shrink keeps this O(log n) re-serializations.
    while len(payload) > max_ansi_bytes and history_lines:
        drop = max(1, len(history_lines) // 2)
        history_lines = history_lines[drop:]
        payload = assemble(history_lines)
    if len(payload) > max_ansi_bytes:
        return None  # current screen alone won't fit — let the caller clear
    return payload


def screen_text(raw: bytes, cols: int, rows: int, *, max_history_rows: int = MAX_HISTORY_ROWS):
    """(history_lines, display_lines) of plain text for ``raw`` at ``cols×rows``.

    Test/diagnostic helper — the logical text the emulator shows at this width,
    independent of serialization. Used to assert that ``render_snapshot`` round-
    trips: feeding its output into a fresh screen reproduces this same text.
    """
    screen = pyte.HistoryScreen(cols, rows, history=max_history_rows, ratio=0.5)
    stream = pyte.ByteStream(screen)
    with contextlib.suppress(Exception):
        stream.feed(raw)
    history = [
        "".join(row[c].data or " " for c in range(cols)).rstrip() for row in screen.history.top
    ]
    display = [line.rstrip() for line in screen.display]
    return history, display
