"""A terminal screen as a redacted plain-text frame (#1246 §5, #2056).

Pure functions over a grid: nothing here reads a session, a store row or the channel.

**The grid is matched untrimmed.** A frame arrives as exactly ``rows`` strings of exactly ``cols``
characters (``scrollback.screen_export``), blanks included. A terminal wraps a long value across
rows, so the grid is matched twice: row by row, and on the **row-joined text** (the full-width
rows concatenated with no line breaks). The joined text is exactly what was written across the
wrap, internal and trailing blanks included, and offset ``i`` is row ``i // cols``, column
``i % cols``. A match on the joined text masks its cells in every row it spans.

**The same values and variants as** ``template_secrets.redact_text``: every value of
``redaction_values()``, raw, JSON-escaped and URL-encoded. The variant list is
``template_secrets``'s own and is not repeated here.

**Complete values only.** A value that is only partly on the screen is NOT masked: its beginning
scrolled off the top, or its second part has not been written yet. Fragments are deliberately not
matched, because masking by partial match tells an observer which prefixes are secrets (the oracle
``share_api.py`` documents). This is the stated limit of a terminal view; a test pins it.

**The mask keeps the grid.** Each masked cell becomes one :data:`MASK` in place, so a line never
grows beyond ``cols`` and every other cell keeps its column. ``redact_text``'s ``[secret]``
placeholder is not used: it would shift or overflow cells. Trailing blanks are dropped per line
only AFTER masking.
"""

from __future__ import annotations

import re

from .. import template_secrets, vtscreen

MASK = "█"  # █
#: What stands in a cell that held a character no line of a frame may carry.
REPLACEMENT = "�"
#: Characters a frame line never carries: C0 and C1 controls, DEL, the Unicode line and paragraph
#: separators, lone surrogates, and the bidirectional embedding, override and isolate controls
#: (which would let one line of a peer's screen reorder how it is read). The owner replaces them
#: with :data:`REPLACEMENT`; the receiver refuses a frame that holds one.
FORBIDDEN_RE = re.compile("[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩\ud800-\udfff]")
FINGERPRINT_RE = re.compile(r"\A[0-9a-f]{32}\Z")
FINGERPRINT_LABEL = "federation-terminal-frame"
ROWS_MAX = vtscreen.MAX_ROWS
COLS_MAX = vtscreen.MAX_COLS


class BadGrid(ValueError):
    """The grid is not ``rows`` strings of ``cols`` characters: nothing is matched on it."""


def valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and FINGERPRINT_RE.fullmatch(value) is not None


def _spans(text: str, values: list[str]) -> list[tuple[int, int]]:
    """Every occurrence of every value in ``text``, overlapping ones included."""
    out: list[tuple[int, int]] = []
    for value in values:
        at = text.find(value)
        while at != -1:
            out.append((at, at + len(value)))
            at = text.find(value, at + 1)
    return out


def redact_grid(grid: tuple[str, ...] | list[str], cols: int, secrets: list[str]) -> list[str]:
    """``grid`` with every complete secret masked cell for cell. The result has the same shape:
    the same number of rows, each exactly ``cols`` characters, every unmasked cell unchanged.

    ``secrets`` is ``template_secrets.redaction_values()``; the caller reads it for THIS call (it
    raises ``RedactionUnavailable``, and then nothing is sent)."""
    if cols < 1 or any(len(row) != cols for row in grid):
        raise BadGrid("a frame is matched on the untrimmed grid only")
    if not secrets or not grid:
        return list(grid)
    joined = "".join(grid)
    variants = template_secrets._all_variants(secrets)
    # A variant inside one row is inside the joined text too, so only those can match anywhere.
    present = [v for v in template_secrets._candidates(variants, joined) if v in joined]
    if not present:
        return list(grid)
    masked = bytearray(len(joined))
    # Row by row: a value inside one line.
    for index, row in enumerate(grid):
        for start, end in _spans(row, present):
            base = index * cols
            masked[base + start : base + end] = b"\x01" * (end - start)
    # The row-joined text: a value the terminal wrapped, masked in every row it spans.
    for start, end in _spans(joined, present):
        masked[start:end] = b"\x01" * (end - start)
    cells = [MASK if masked[i] else ch for i, ch in enumerate(joined)]
    return ["".join(cells[r * cols : (r + 1) * cols]) for r in range(len(grid))]


def transport_lines(rows: list[str]) -> list[str]:
    """The MASKED rows as they travel: trailing blanks dropped per line, and every character a
    frame line may not carry replaced. Never called on an unmasked grid: dropping blanks first
    would join ``AB`` and ``CD`` across a wrap that held ``AB  CD``."""
    return [FORBIDDEN_RE.sub(REPLACEMENT, row).rstrip(" ") for row in rows]


def redact(grid: tuple[str, ...] | list[str], cols: int) -> list[str]:
    """The lines of a frame for one call: matched and masked on the untrimmed grid with the
    values read NOW, then cut for transport. Fails closed: ``RedactionUnavailable`` propagates."""
    return transport_lines(redact_grid(grid, cols, template_secrets.redaction_values()))


def fingerprint(grant_id: str, rows: int, cols: int, lines: list[str]) -> str:
    """A keyed digest of the REDACTED frame, bound to the grant. It reveals nothing about masked
    content (it is computed after masking), cannot be computed without this instance's key, and
    differs between two grants for the same screen. ``SecretKeyUnavailable`` propagates."""
    body = "\n".join(lines)
    return template_secrets.keyed_digest(FINGERPRINT_LABEL, f"{grant_id}\0{rows}\0{cols}\0{body}")[
        :32
    ]


def check_lines(lines: object, rows: int, cols: int) -> None:
    """What a receiver asks of the lines a peer sent: at most ``rows`` of them, each a string of
    at most ``cols`` characters and none a frame may not carry. ``ValueError`` otherwise."""
    if not isinstance(lines, list) or len(lines) > rows:
        raise ValueError("lines")
    for line in lines:
        if not isinstance(line, str) or len(line) > cols:
            raise ValueError("line length")
        if FORBIDDEN_RE.search(line):
            raise ValueError("control character")
