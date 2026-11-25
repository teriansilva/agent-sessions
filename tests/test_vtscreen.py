"""The review-only VT screen renderer (#611).

The load-bearing property: an agent that repaints in place must render to the frame a human
sees, not to the debris left behind when the cursor moves are merely deleted. The codex
spinner fixture below is a byte-for-byte capture of a live codex session's ring — the exact
stream that produced the review "Idle: agent appears to be typing random characters".
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_sessions import vtscreen

FIX = Path(__file__).parent / "fixtures"

# A real codex frame: sync-update markers, absolute cursor moves, truecolor SGR, erase-line,
# and the `ESC [ 0 SP q` cursor-style sequence whose intermediate byte desyncs naive parsers.
CODEX_SPINNER = (
    b"\x1b[?2026h\x1b[66;2H\x1b[0m\x1b[49m\x1b[K"
    b"\x1b[67;1H\x1b[1m\xe2\x80\xba\x1b[22m \x1b[2mImprove documentation\x1b[K"
    b"\x1b[68;3H\x1b[38;2;246;226;183;49mgpt-5.5 xhigh\x1b[39m\x1b[49m\x1b[0m\x1b[0 q\x1b[?2026l"
    # …then three spinner ticks, each rewriting the SAME cell via an absolute cursor move.
    b"\x1b[?2026h\x1b[66;2HWorking\x1b[?2026l"
    b"\x1b[?2026h\x1b[66;2HWorking\xe2\x80\xa2\x1b[?2026l"
    b"\x1b[?2026h\x1b[66;2HWorking\x1b[?2026l"
)


def test_codex_spinner_renders_one_clean_line_not_soup():
    out = vtscreen.render(CODEX_SPINNER, 70, 120)
    # The pre-#611 escape-strip produced `Working•4orking•rking•king•ingng`. The screen has
    # exactly one `Working`, because every tick overwrote the same cells.
    assert out.count("Working") == 1
    assert "orking•rking" not in out
    assert "Improve documentation" in out
    assert "gpt-5.5 xhigh" in out
    assert "\x1b" not in out
    # Escape parameter bytes must never leak through as literal text.
    assert "2026" not in out and "[66;2H" not in out


def test_cursor_up_repaint_overwrites_rather_than_appends():
    # claude / agy style: print a line, move back up, rewrite it.
    data = b"first\r\nsecond\r\n\x1b[2A\x1b[2Krewritten\r\n"
    out = vtscreen.render(data, 10, 40)
    assert "rewritten" in out
    assert "first" not in out


def test_carriage_return_progress_bar_keeps_only_the_last_frame():
    out = vtscreen.render(b"10%\r50%\r99%", 5, 20)
    assert out.strip() == "99%"


def test_plain_crlf_stream_is_unchanged():
    out = vtscreen.render(b"hello\r\nworld\r\n", 10, 40)
    assert out == "hello\nworld"


def test_bare_lf_moves_down_without_returning_to_column_zero():
    # Faithful VT semantics, and what xterm.js shows: LF is line-feed, not newline. Agents run
    # their tty in raw mode (no ONLCR), so a bare \n really does stagger — rendering it any
    # other way would put text in cells the user never saw.
    assert vtscreen.render(b"hello\nworld", 10, 40) == "hello\n     world"


def test_erase_display_clears_the_screen():
    out = vtscreen.render(b"gone\r\n\x1b[2Jkept", 10, 40)
    assert "gone" not in out
    assert "kept" in out


def test_trailing_full_erase_falls_back_to_the_last_complete_frame():
    # Slicing a live ring can land between a full-screen erase and the repaint that follows.
    # Rendering that literally yields a blank screen; the honest frame is the prior one.
    data = b"real content here\r\n\x1b[H\x1b[J"
    assert vtscreen.render(data, 10, 40) == "real content here"


def test_unrenderable_stream_returns_empty_for_the_caller_to_fall_back():
    assert vtscreen.render(b"", 10, 40) == ""
    assert vtscreen.render(b"\x1b[2J", 10, 40) == ""


def test_autowrap_and_scroll_keep_the_visible_window():
    out = vtscreen.render(b"\r\n".join(f"line{i}".encode() for i in range(20)), 5, 40)
    lines = out.splitlines()
    assert len(lines) <= 5
    assert "line19" in out
    assert "line0" not in out  # scrolled off the top


def test_infer_rows_reads_the_tallest_absolute_row():
    assert vtscreen.infer_rows(b"\x1b[12;1Hx\x1b[70;4Hy\x1b[3;1Hz") == 70
    assert vtscreen.infer_rows(b"no cursor moves here") == 0
    assert vtscreen.infer_rows(b"\x1b[9999;1Hx") == vtscreen.MAX_ROWS


def test_geometry_is_clamped_so_a_stale_sidecar_cannot_allocate_unbounded():
    out = vtscreen.render(b"hi", 10**6, 10**6)
    assert out == "hi"


@pytest.mark.parametrize("rows,cols", [(0, 0), (-5, -5)])
def test_degenerate_geometry_does_not_crash(rows, cols):
    assert isinstance(vtscreen.render(b"hi", rows, cols), str)


def test_render_of_a_large_tail_stays_within_budget():
    # 256 KiB is the slice `scrollback.live_tail_text` feeds; it runs in a worker thread on a
    # sweep bounded to SWEEP_CAP sessions. Generous ceiling — this guards a pathological
    # regression (e.g. per-character rescanning), not a precise number.
    data = CODEX_SPINNER * 2000
    t0 = time.perf_counter()
    vtscreen.render(data[:262_144], 70, 200)
    assert time.perf_counter() - t0 < 5.0


# ---- Hermes on PR #618: positioning controls + control-string payload leaks --------------


def test_decsc_decrc_restore_the_cursor_rather_than_printing_7_and_8():
    # ESC 7 / ESC 8 are save/restore, not text. They used to leak literal `7`/`8` AND leave the
    # restored write in the wrong cell.
    assert (
        vtscreen.render(b"prompt> \x1b7\x1b[2;1HSTATUS\x1b8typed", 5, 40) == "prompt> typed\nSTATUS"
    )


def test_csi_s_u_save_and_restore_the_cursor():
    assert (
        vtscreen.render(b"prompt> \x1b[s\x1b[2;1HSTATUS\x1b[utyped", 5, 40)
        == "prompt> typed\nSTATUS"
    )


def test_unterminated_osc_payload_never_reaches_the_screen():
    # A tail slice routinely cuts a control string in half. OSC 52 is the clipboard: its payload
    # is not display content and must never be handed to the AI reviewer.
    assert vtscreen.render(b"visible \x1b]52;c;SECRET", 5, 40) == "visible"
    assert "SECRET" not in vtscreen.render(b"visible \x1b]52;c;SECRET", 5, 40)


def test_terminated_and_unterminated_string_controls_are_both_consumed():
    assert vtscreen.render(b"a\x1b]0;window title\x07b", 5, 40) == "ab"
    assert vtscreen.render(b"a\x1b]0;window title\x1b\\b", 5, 40) == "ab"
    assert vtscreen.render(b"a\x1bPsecret\x1b\\b", 5, 40) == "ab"  # DCS
    assert vtscreen.render(b"a\x1bPunterminated", 5, 40) == "a"
    assert vtscreen.render(b"a\x1b_apc payload\x1b\\b", 5, 40) == "ab"
    assert vtscreen.render(b"a\x1b^pm payload\x1b\\b", 5, 40) == "ab"


# ---- backspace (#2058) -----------------------------------------------------------------------
#
# BS moves the cursor one cell left, erases nothing and stops at column 0. The recorded rings are
# what a real `bash` wrote to a PTY (`tests/fixtures/shell_backspace.PROVENANCE.md`); each has the
# screen tmux 3.4 showed for the same bytes beside it, so the tests need no terminal at run time.

SHELL_RINGS = {
    "shell_backspace_edit": (24, 80),
    "shell_backspace_edit_dumb": (24, 80),
    "shell_backspace_col0": (24, 80),
    "shell_backspace_wrap": (6, 20),
    "shell_backspace_margin": (6, 20),
}


def _ring(name: str) -> bytes:
    return (FIX / f"{name}.raw").read_bytes()


def _shown(grid: list[str] | None) -> str:
    """A strict grid the way `tmux capture-pane -p` prints a screen."""
    assert grid is not None, "the frame was refused"
    return "\n".join(row.rstrip() for row in grid).rstrip("\n") + "\n"


def _strict(data: bytes, rows: int, cols: int) -> list[str] | None:
    """Both strict readers, which must agree: the grid, or None when the frame is refused."""
    grid = vtscreen.render_grid(data, rows, cols)
    cells = vtscreen.render_cells(data, rows, cols)
    if grid is None:
        assert cells == []
        return None
    assert "\n".join(c.text for c in cells) == vtscreen.render(data, rows, cols)
    return grid


@pytest.mark.parametrize("name", sorted(SHELL_RINGS))
def test_a_recorded_shell_ring_with_backspaces_renders_as_the_reference_terminal_shows_it(name):
    rows, cols = SHELL_RINGS[name]
    reference = (FIX / f"{name}.tmux.txt").read_text()
    assert _shown(_strict(_ring(name), rows, cols)) == reference
    assert vtscreen.render(_ring(name), rows, cols) + "\n" == reference


@pytest.mark.parametrize("name", ["shell_backspace_edit", "shell_backspace_edit_dumb"])
def test_a_bash_line_edited_with_backspaces_renders_as_the_final_line(name):
    raw = _ring(name)
    assert raw.count(b"\x08 \x08") == 4  # the line editor's erase, once per erased character
    assert _shown(_strict(raw, 24, 80)) == "$ echo password\npassword\n$\n"


def test_backspace_moves_the_cursor_left_and_erases_nothing():
    assert _strict(b"abc\x08", 2, 10) == ["abc       ", " " * 10]
    assert _strict(b"abc\x08\x08X", 2, 10)[0] == "aXc       "
    assert _strict(b"abc\x08 \x08", 2, 10)[0] == "ab        "  # the erase idiom
    assert vtscreen.render(b"pasword\x08\x08\x08\x08sword", 2, 20) == "password"


def test_backspace_stops_at_column_zero():
    assert _strict(b"\x08\x08ab\x08\x08\x08\x08X", 2, 10)[0] == "Xb        "
    # ...on a later row too: it never climbs to the row above.
    assert _strict(b"up\r\n\x08\x08\x08Y", 2, 10) == ["up        ", "Y         "]
    # A real program doing it, under a real shell.
    assert _shown(_strict(_ring("shell_backspace_col0"), 24, 80)).splitlines()[1] == "Xb"


def test_backspace_never_wraps_back_onto_the_row_above():
    # Seven characters on a five-column screen: `fg` is on the second row. No number of
    # backspaces leaves that row (reverse wrap is off, as it is by default in a terminal).
    assert _strict(b"abcdefg" + b"\x08" * 5 + b"X", 3, 5) == ["abcde", "Xg   ", "     "]
    # The line editor crosses a wrap by moving up itself, and that renders as the final line.
    raw = _ring("shell_backspace_wrap")
    assert b"\x08" in raw and b"\x1b[A" in raw
    assert _shown(_strict(raw, 6, 20)).splitlines()[0] == "$ echo abcdefREST"


@pytest.mark.parametrize(
    "tail",
    [b"\x08", b"\x08\x08", b"\x08" * 7, b"\x08X", b"\x08\x08X", b"\x08 \x08", b"\x08\r\nnext"],
    ids=["one", "two", "many", "one_then_text", "two_then_text", "erase_idiom", "then_a_new_line"],
)
def test_backspace_at_the_right_margin_is_refused_because_no_reference_pins_it(tail):
    """`abcde` on five columns leaves the cursor in the pending-autowrap state (`col == cols`).

    What BS does there is where terminals are known to disagree. tmux 3.4, measured (see the
    provenance file), keeps the cursor on the last cell: `abcde BS X` shows `abcdX`, two BS
    `abcXe`. The reference #2058 names is xterm with default resources, and there is no xterm
    on the recording host to measure. So no case is pinned, and one BS, repeated BS and the
    printable that follows all leave the frame untrusted. Nothing is guessed."""
    assert _strict(b"abcde", 3, 5) == ["abcde", "     ", "     "]  # the state itself is trusted
    assert _strict(b"abcde" + tail, 3, 5) is None
    # The same margin reached without writing in the last column (the model cannot tell them
    # apart: both are `col == cols`).
    assert _strict(b"ab\x1b[9C" + tail, 3, 5) is None
    # One column short of the margin nothing is pending, and the frame is read.
    assert _strict(b"abcd" + tail, 3, 5) is not None
    # The forgiving reader still answers; at the margin it leaves the cursor where it was.
    assert vtscreen.render(b"abcde" + tail, 3, 5).startswith("abcde")


@pytest.mark.parametrize(
    "seq",
    [
        b"abcde\x1b[D\x08X",  # PR #2328 review: CUB off the pending wrap, then BS
        b"abcde\x1b[2D\x1b[C\x08X",  # back and forth, still never set absolutely
        b"abcde\x1b[A\x08X",  # up from the pending wrap
        b"abcde\x1b7\r\x1b8\x08X",  # the margin saved, a CR, then restored (DECSC/DECRC)
        b"abcde\x1b[s\r\x1b[u\x08X",  # the same with SCOSC/SCORC
        b"ab\x1b[9C\x1b[D\x08X",  # the margin reached by a clamped move, then CUB
        # Through the alternate screen (#2328 review 8829): the flag is saved with the buffer.
        b"abcde\x1b[D\x1b[?1049h\r\x1b[?1049l\x08X",
        b"abcde\x1b[D\x1b[?1047h\r\x1b[?1047l\x08X",
        b"abcde\x1b[D\x1b[?47h\r\x1b[?47l\x08X",
        # 47/1047 do not restore the cursor in xterm.js, so ANY column after leaving is unpinned.
        b"abc\x1b[?1047h\r\x1b[?1047l\x08X",
        # An exit whose entry this slice never saw restores an unknown cursor.
        b"abc\x1b[?1049l\x08X",
    ],
    ids=[
        "cub",
        "cub_cuf",
        "cuu",
        "decsc",
        "scosc",
        "clamped_then_cub",
        "alt_1049",
        "alt_1047",
        "alt_47",
        "alt_1047_any_column",
        "alt_exit_unseen",
    ],
)
def test_backspace_from_a_column_reached_relatively_from_the_margin_is_refused(seq):
    """xterm.js, the app's reference terminal, clamps the pending-wrap cursor to the last cell
    before any relative move; this model does not, so after a relative move from the margin the
    two columns may differ by one. `abcde CSI D BS X` shows `abXde` in xterm.js and would be
    `abcXe` here. A BS there is refused until the column is set absolutely again."""
    assert _strict(seq, 3, 5) is None


@pytest.mark.parametrize(
    "seq, row0",
    [
        (b"abcde\r\x08X", "Xbcde"),  # CR: column 0, BS stays there
        (b"abcde\x1b[1;4H\x08X", "abXde"),  # CUP
        (b"abcde\x1b[4G\x08X", "abXde"),  # CHA
    ],
    ids=["cr", "cup", "cha"],
)
def test_an_absolute_column_after_the_margin_makes_backspace_exact_again(seq, row0):
    assert _strict(seq, 3, 5) == [row0, "     ", "     "]


@pytest.mark.parametrize(
    "seq",
    [
        b"ab\x1b7c\x1b[?1049h\r\x1b[?1049l\x1b8\x08X",  # 1049's entry overwrites the saved cursor
        b"ab\x1b7\x1b[?1047hxy\x1b8\x08X",  # a restore inside the other buffer
        b"abc\x1b[?47h\r\n\x1b[?47l\r\x08X",  # 47 keeps the alt row; a CR fixes the column only
        b"abc\x1b[?47h\r\n\x1b[?47l\x1b[3G\x08X",  # ... and so does a CHA
        b"abc\x1b[?1047h\r\n\x1b[?1047l\r\x08X",
        b"abc\x1b[?1047h\r\n\x1b[?1047l\x1b[3G\x08X",
        # Review 8836: a saved cursor cannot launder a lost position past a CUP ...
        b"abc\x1b[?47h\r\n\x1b[?47l\x1b7\x1b[1;1H\x1b8\x08X",
        b"abc\x1b[?1047h\r\n\x1b[?1047l\x1b[s\x1b[1;1H\x1b[u\x08X",
        # ... and a 1049 exit after a 47 entry restores a cursor 1049's entry never saved.
        b"ab\x1b7c\x1b[?47h\r\x1b[?1049l\x08X",
        # Refused by design, though exact in xterm.js: no restore and no exit is trusted.
        b"ab\x1b7cd\x1b8\x08X",
        b"abc\x1b[?1049h\r\x1b[?1049l\x08X",
    ],
    ids=[
        "decrc_after_1049",
        "decrc_across_buffers",
        "47_cr",
        "47_cha",
        "1047_cr",
        "1047_cha",
        "decsc_launders_past_cup",
        "scosc_launders_past_cup",
        "47_entry_1049_exit",
        "decrc_same_buffer",
        "1049_round_trip",
    ],
)
def test_backspace_at_a_position_the_model_cannot_vouch_for_is_refused(seq):
    """#2328 review 8833. xterm.js keeps a saved cursor per buffer (1049's entry writes the
    normal one), and 47/1047 leave the cursor where the alternate screen left it, row included.
    Here: `ab ESC7 c 1049h CR 1049l ESC8 BS X` is `abX` in xterm-headless 5.5.0 and would be
    `aXc`; `abc 47h CR LF 47l CR BS X` puts the X on the SECOND row there. Every alternate-screen
    exit and every cursor restore is untrusted, including the ones xterm.js would agree with
    (review 8836: modelling the combinations kept missing one). Only a full CUP/HVP pins the
    position again."""
    assert vtscreen.render_grid(seq, 3, 5) is None
    assert vtscreen.render_cells(seq, 3, 5) == []


@pytest.mark.parametrize(
    "seq, row0",
    [
        (b"abc\x1b[?47h\r\n\x1b[?47l\x1b[1;3H\x08X", "aXc  "),  # a CUP pins it again
        (b"abc\x1b[?1049h\r\x1b[?1049l\x1b[1;3H\x08X", "aXc  "),
        (b"ab\x1b7cd\x1b8\x1b[1;3H\x08X", "aXcd "),  # after a restore too
    ],
    ids=["cup_after_47", "cup_after_1049", "cup_after_decrc"],
)
def test_a_pinned_position_keeps_backspace_exact(seq, row0):
    """Read with the grid alone: the forgiving reader `_strict` compares with does not model the
    alternate screen."""
    assert vtscreen.render_grid(seq, 3, 5) == [row0, "     ", "     "]


#: Every C0 control and DEL the strict readers accept. BS is the one #2058 added.
TRUSTED_C0 = {0x00, 0x07, 0x08, 0x09, 0x0A, 0x0D, 0x0F}


def test_the_trust_list_grew_by_backspace_and_nothing_else():
    assert _strict(b"abc\x08", 3, 10) is not None
    for code in [*range(0x20), 0x7F]:
        frame = _strict(b"abc" + bytes([code]) + b"d", 3, 10)
        assert (frame is not None) == (code in TRUSTED_C0), hex(code)


@pytest.mark.parametrize(
    "control",
    [
        b"\x1b[?45h",  # reverse wrap
        b"\x1b[?1045h",  # xterm's extended reverse wrap
        b"\x1b[?45l",  # even switching it off: the mode is not modelled at all
        b"\x1b[P",  # delete character, the other way a line editor erases
        b"\x1b[@",  # insert character
        b"\x7f",  # DEL
        b"\x1b[?7l",  # autowrap off, which would change what BS means at the margin
    ],
)
def test_reverse_wrap_and_every_other_unsupported_control_are_still_refused(control):
    for data in (b"abc" + control, control + b"abc\x08\x08X", b"abc\x08" + control + b"X"):
        assert _strict(data, 3, 10) is None
    assert _strict(b"abc\x08\x08X", 3, 10) is not None  # the backspaces alone are read


def test_backspace_after_a_character_of_unknown_width_is_refused():
    """The grid gives every character one column. A combining mark takes none on a real screen,
    so a BS after it lands one cell further left there than here: `1. Yes` + U+0301, three
    backspaces and `No ` shows `1. No` on a terminal and would read `1. YNo` here."""
    assert vtscreen.render_cells(b"1. Yes\xcc\x81", 2, 20)  # read today, and still
    assert vtscreen.render_cells(b"1. Yes\xcc\x81\x08\x08\x08No ", 2, 20) == []
    assert vtscreen.render_grid(b"1. Yes\xcc\x81\x08\x08\x08No ", 2, 20) is None
