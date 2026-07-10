"""The review-only VT screen renderer (#611).

The load-bearing property: an agent that repaints in place must render to the frame a human
sees, not to the debris left behind when the cursor moves are merely deleted. The codex
spinner fixture below is a byte-for-byte capture of a live codex session's ring — the exact
stream that produced the review "Idle: agent appears to be typing random characters".
"""

from __future__ import annotations

import time

import pytest

from agent_sessions import vtscreen

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
