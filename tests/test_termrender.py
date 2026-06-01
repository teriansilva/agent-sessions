"""Tests for the width-aware snapshot serializer (issue #242, PR1).

Pure serializer only — no `webterm` wiring yet (that's PR2). The core property is
a **width round-trip**: feeding the serialized snapshot back into a fresh
emulator at the same width reproduces exactly the grid the emulator shows for the
raw bytes — so any client width loads cleanly while keeping scroll-up history.
The other load-bearing property is **flatness**: the snapshot contains no
layout-dependent escapes (cursor-up, mid-stream line clears, save/restore), which
is precisely what makes raw replay garble across widths.
"""

from __future__ import annotations

import re
from pathlib import Path

import pyte
import pytest

from agent_sessions import termrender as tr

FIXTURES = Path(__file__).parent / "fixtures" / "term"
TRACES = [
    "claude_repaint_wide.bin",
    "claude_repaint_narrow.bin",
    "claude_repaint_resize_mobile.bin",
]
WIDTHS = [(40, 24), (80, 24), (100, 30), (120, 40)]


def _raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize("name", TRACES)
@pytest.mark.parametrize("cols,rows", WIDTHS)
def test_snapshot_roundtrips_at_any_width(name, cols, rows):
    """The core fix: the snapshot rendered at (cols,rows) reproduces the same
    screen + history the emulator shows for the raw bytes at that width."""
    raw = _raw(name)
    snap = tr.render_snapshot(raw, cols, rows)
    assert snap is not None
    assert tr.screen_text(snap, cols, rows) == tr.screen_text(raw, cols, rows)


@pytest.mark.parametrize("name", TRACES)
def test_snapshot_is_flat_no_layout_dependent_escapes(name):
    """A snapshot carries only styled text + line breaks, a leading clear, and a
    trailing absolute home — no cursor-up / line-clear / save-restore. That
    flatness is what lets it render cleanly at *any* width (the garble cure)."""
    snap = tr.render_snapshot(_raw(name), 80, 24)
    assert snap is not None
    # Strip the single leading clear and the trailing cursor-home we emit on purpose.
    body = snap
    assert body.startswith(b"\x1b[H\x1b[2J\x1b[3J")
    body = body[len(b"\x1b[H\x1b[2J\x1b[3J") :]
    body = re.sub(rb"\x1b\[0m\x1b\[\d+;\d+H$", b"", body)  # trailing reset + home
    assert re.search(rb"\x1b\[\d*A", body) is None, "no cursor-up moves"
    assert re.search(rb"\x1b\[\d*B", body) is None, "no cursor-down moves"
    assert b"\x1b[2K" not in body and b"\x1b[K" not in body, "no line clears"
    assert b"\x1b7" not in body and b"\x1b8" not in body, "no cursor save/restore"
    assert b"\x1b[3J" not in body, "no extra scrollback clears"


@pytest.mark.parametrize("name", TRACES)
def test_snapshot_preserves_scrollback_history(name):
    """Multi-turn sessions keep scroll-up history — the user's primary ask."""
    snap = tr.render_snapshot(_raw(name), 40, 24)
    assert snap is not None
    history, _display = tr.screen_text(snap, 40, 24)
    assert len(history) > 0, "narrow render should have scrollback to scroll up into"


def test_snapshot_caps_size_by_dropping_oldest_history():
    """Over the byte cap we drop OLDEST history first and keep the current frame."""
    raw = _raw("claude_repaint_resize_mobile.bin")
    full = tr.render_snapshot(raw, 80, 24)
    small = tr.render_snapshot(raw, 80, 24, max_ansi_bytes=1500)
    assert full is not None and small is not None
    assert len(small) <= 1500
    full_hist, _ = tr.screen_text(full, 80, 24)
    small_hist, _ = tr.screen_text(small, 80, 24)
    assert len(small_hist) < len(full_hist), "capped render dropped some history"
    # The visible current frame is identical — only old scrollback was sacrificed.
    assert tr.screen_text(small, 80, 24)[1] == tr.screen_text(raw, 80, 24)[1]
    # What history survives is the MOST RECENT slice (a tail of the full history).
    assert small_hist == full_hist[len(full_hist) - len(small_hist) :]


def test_snapshot_returns_none_when_screen_alone_exceeds_cap():
    """If even the current screen can't fit the cap, return None so the caller
    falls back to the #241 clean-load clear rather than emit a partial render."""
    raw = _raw("claude_repaint_wide.bin")
    assert tr.render_snapshot(raw, 80, 24, max_ansi_bytes=8) is None


def test_snapshot_none_on_empty_or_degenerate_dimensions():
    assert tr.render_snapshot(b"", 80, 24) is None
    assert tr.render_snapshot(b"hello", 0, 24) is None
    assert tr.render_snapshot(b"hello", 80, 0) is None


def test_snapshot_ends_with_absolute_cursor_home():
    """Snapshot positions the cursor with an absolute move so the live stream that
    follows continues from the right cell regardless of how history scrolled."""
    snap = tr.render_snapshot(_raw("claude_repaint_narrow.bin"), 80, 24)
    assert snap is not None
    assert re.search(rb"\x1b\[\d+;\d+H$", snap) is not None


def _cell_attrs(data: bytes, cols: int, rows: int) -> dict:
    """Per-cell (glyph, fg, bg, bold, underscore) for every non-blank cell — so a
    colour regression is caught by *attributes*, not just text."""
    s = pyte.Screen(cols, rows)
    st = pyte.ByteStream(s)
    st.feed(data)
    out = {}
    for y in range(rows):
        for x in range(cols):
            ch = s.buffer[y][x]
            if ch.data not in (" ", ""):
                out[(y, x)] = (ch.data, ch.fg, ch.bg, ch.bold, ch.underscore)
    return out


def test_named_256_and_truecolor_roundtrip():
    """Named, 256-colour, and truecolour all survive serialization (256/truecolour
    both surface from pyte as hex and are re-emitted as truecolour SGR)."""
    raw = (
        b"\x1b[31mRED\x1b[0m\r\n"
        b"\x1b[38;5;208m256\x1b[0m\r\n"
        b"\x1b[38;2;10;200;30mTRUE\x1b[0m\r\n"
        b"\x1b[1;4mBOLDU\x1b[0m\r\n"
    )
    snap = tr.render_snapshot(raw, 20, 6)
    assert snap is not None
    assert _cell_attrs(snap, 20, 6) == _cell_attrs(raw, 20, 6)


def test_bright_fg_and_bg_colors_roundtrip():
    """Bright SGR (90–97 / 100–107) surfaces from pyte as ``bright*`` names; they
    must be re-emitted, not dropped to terminal default (Hermes #248)."""
    raw = (
        b"\x1b[90mBRIGHTBLACK\x1b[0m\r\n"  # bright fg
        b"\x1b[101mBRIGHTREDBG\x1b[0m\r\n"  # bright bg
        b"\x1b[93;1mBRIGHTYELLOW\x1b[0m\r\n"  # bright yellow (pyte: brightbrown) + bold
    )
    snap = tr.render_snapshot(raw, 20, 6)
    assert snap is not None
    before = _cell_attrs(raw, 20, 6)
    # Guard the test itself: the raw really does carry bright fg AND bg (not default).
    assert any(fg.startswith("bright") for _, fg, _, _, _ in before.values())
    assert any(bg.startswith("bright") for _, _, bg, _, _ in before.values())
    assert _cell_attrs(snap, 20, 6) == before


def test_wide_char_preserved():
    raw = "AB世C\r\nplain\r\n".encode()
    snap = tr.render_snapshot(raw, 10, 4)
    assert snap is not None
    assert tr.screen_text(snap, 10, 4) == tr.screen_text(raw, 10, 4)


def test_render_snapshot_does_not_mutate_webterm_offsets():
    """Offset invariant (#242): serializing is pure — it never shifts `_TOTALS` /
    `_BUFFERS`, so the `seq` the caller sends afterwards stays the real byte total
    and the synthetic snapshot length never leaks into the client's `have`."""
    from agent_sessions import webterm

    key = "claude:offset-invariant-test"
    webterm._drop_buffer(key)
    try:
        webterm._buffer_append(key, b"\x1b[32mhello\x1b[0m world\r\n")
        webterm._buffer_append(key, b"second line of output\r\n")
        totals_before = dict(webterm._TOTALS)
        buf_before = bytes(webterm._BUFFERS[key])

        snap = tr.render_snapshot(buf_before, 80, 24)
        assert snap is not None

        assert webterm._TOTALS == totals_before
        assert bytes(webterm._BUFFERS[key]) == buf_before
    finally:
        webterm._drop_buffer(key)
