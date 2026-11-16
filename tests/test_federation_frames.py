"""A terminal screen as a redacted text frame (#2056): the untrimmed render, the strict exporter
and the frame redaction.

Everything here is the owner's side below the verb (`test_federation_terminal.py` drives the verb
over a channel). The rings are fed through `scrollback._buffer_append`, the one chokepoint both
the viewer bridge and the headless reader write through, with real captures where one exists:
`opencode_permission_shell.raw` is an alternate-screen TUI at 46 × 65, `claude_permission_bash.raw`
an inline agent at 40 × 100 (`tests/fixtures/permission_prompts.PROVENANCE.md`).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from agent_sessions import scrollback, share_api, template_secrets, vtscreen
from agent_sessions.federation import frames

FIX = Path(__file__).parent / "fixtures"
OC = (46, 65)  # rows, cols the opencode captures were authored at
CL = (40, 100)
KEY = "claude:99999999-9999-9999-9999-999999999999"
SECRET = "s3cret-value-123"
MASK = "█"
# What a plain shell writes: a title, bracketed paste, a prompt, a command and its output.
SHELL = (
    b"\x1b]0;op@box: ~/work\x07\x1b[?2004h$ echo hello\r\n\x1b[?2004l"
    b"hello\r\n\x1b[?2004h$ ls\r\n\x1b[?2004lREADME.md  src  tests\r\n\x1b[?2004h$ "
)


@pytest.fixture(autouse=True)
def _geometry(monkeypatch):
    """The geometry tables are process-wide and not part of conftest's ring isolation."""
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())


def sized(rows: int, cols: int, key: str = KEY) -> str:
    """What a viewer's attach records (`webterm.run`): the size it drains the session at."""
    scrollback._LAST_ROWS[key] = rows
    scrollback.note_attach_width(key, cols)
    scrollback.note_geometry(key, rows, cols)
    return key


def paint(data: bytes, rows: int, cols: int, key: str = KEY) -> str:
    """A session whose ring holds ``data``, written at a geometry a viewer reported."""
    sized(rows, cols, key)
    scrollback._buffer_append(key, data)
    return key


def restart() -> None:
    """A new process: every in-memory table is gone, the disk mirror and its sidecars stay."""
    for table in (
        scrollback._BUFFERS,
        scrollback._TOTALS,
        scrollback._LAST_COLS,
        scrollback._LAST_ROWS,
        scrollback._RING_MIXED,
        scrollback._LOADED_FROM_DISK,
        scrollback._GEOM_MARK,
        scrollback._GEOM_POS,
    ):
        table.clear()


def refused(key: str = KEY) -> str:
    with pytest.raises(scrollback.ScreenUnavailable) as exc:
        scrollback.screen_export(key)
    return exc.value.reason


# ── vtscreen: the untrimmed render ────────────────────────────────────


def test_the_untrimmed_render_is_exactly_rows_by_cols_with_every_cell_in_place():
    grid = vtscreen.render_grid(b"\x1b[3;5Hab  cd\x1b[6;1Hlast", 8, 12)
    assert grid is not None and len(grid) == 8 and all(len(row) == 12 for row in grid)
    assert grid[2] == "    ab  cd  " and grid[5] == "last        "
    assert [i for i, row in enumerate(grid) if row.strip()] == [2, 5]  # blank rows are kept
    # The trimmed readers drop the blank edge rows and the padding: their output is unchanged.
    assert vtscreen.render(b"\x1b[3;5Hab  cd\x1b[6;1Hlast", 8, 12) == "    ab  cd\n\n\nlast"


def test_a_value_wrapped_with_blanks_survives_only_in_the_untrimmed_grid():
    """The reason the exporter does not use the trimmed render: `AB  CD` at width 4."""
    grid = vtscreen.render_grid(b"AB  CD", 3, 4)
    assert grid == ["AB  ", "CD  ", "    "]
    assert "".join(grid).startswith("AB  CD")
    assert vtscreen.render(b"AB  CD", 3, 4) == "AB\nCD"  # joined, that reads ABCD


@pytest.mark.parametrize(
    "name,geom",
    [
        ("opencode_permission_shell.raw", OC),
        ("opencode_permission_grep.raw", OC),
        ("claude_permission_bash.raw", CL),
        ("claude_permission_write.raw", CL),
    ],
)
def test_the_untrimmed_render_is_the_frame_the_trimmed_readers_show(name, geom):
    raw = (FIX / name).read_bytes()
    rows, cols = geom
    grid = vtscreen.render_grid(raw, rows, cols)
    assert grid is not None and len(grid) == rows and {len(row) for row in grid} == {cols}
    cells = vtscreen.render_cells(raw, rows, cols)
    assert cells  # the existing callers still get their trimmed frame
    text = "\n".join(row.rstrip() for row in grid).strip("\n")
    assert text == "\n".join(c.text for c in cells) == vtscreen.render(raw, rows, cols)


@pytest.mark.parametrize(
    "tail", [b"\x08", b"\x1b[2P", b"\x1b[5S", b"\x1b[3;9r", b"\x1b[8;10;20t", b"\x1b"]
)
def test_the_untrimmed_render_keeps_the_fail_closed_trust_checks(tail):
    raw = (FIX / "opencode_permission_shell.raw").read_bytes()
    assert vtscreen.render_grid(raw, *OC) is not None
    assert vtscreen.render_grid(raw + tail, *OC) is None  # not modelled exactly: no frame


def test_the_untrimmed_render_refuses_a_geometry_it_would_have_to_clamp():
    assert vtscreen.render_grid(b"x", vtscreen.MAX_ROWS + 1, 10) is None
    assert vtscreen.render_grid(b"x", 10, vtscreen.MAX_COLS + 1) is None
    assert vtscreen.render_grid(b"x", 0, 10) is None and vtscreen.render_grid(b"x", 10, 0) is None
    assert vtscreen.render_grid(b"", 10, 10) is None
    blank = vtscreen.render_grid(b"x\x1b[2J", 2, 3)
    assert blank == ["   ", "   "]  # a blank grid is a grid: the caller decides what it means


# ── scrollback.screen_export on recorded rings ────────────────────────


def test_a_plain_shell_exports_its_screen_untrimmed():
    export = scrollback.screen_export(paint(SHELL, 24, 80))
    assert (export.rows, export.cols) == (24, 80)
    assert len(export.grid) == 24 and {len(row) for row in export.grid} == {80}
    assert [row.rstrip() for row in export.grid[:5]] == [
        "$ echo hello",
        "hello",
        "$ ls",
        "README.md  src  tests",
        "$",
    ]
    assert not any(row.strip() for row in export.grid[5:])
    assert "op@box" not in "".join(export.grid)  # the title is a control string, not a cell


def test_an_alternate_screen_tui_exports_the_frame_the_strict_reader_shows():
    raw = (FIX / "opencode_permission_shell.raw").read_bytes()
    assert b"\x1b[?1049h" in raw
    export = scrollback.screen_export(paint(raw, *OC))
    assert (export.rows, export.cols) == OC and len(export.grid) == OC[0]
    text, cells = scrollback.live_tail_frame(KEY, 100_000)
    assert cells and "\n".join(row.rstrip() for row in export.grid).strip("\n") == text
    assert "Permission required" in text and "Allow once" in text


def test_the_export_is_refused_without_observed_output():
    assert refused("claude:never-seen") == "no_output"
    sized(24, 80)
    assert refused() == "no_output"


def test_the_export_is_refused_when_the_geometry_is_unknown():
    scrollback._buffer_append(KEY, SHELL)
    assert refused() == "geometry"  # neither is known
    scrollback.note_cols(KEY, 80)
    assert refused() == "geometry"  # the height is not known
    scrollback._LAST_ROWS[KEY] = 24
    assert refused() == "geometry"  # a size, but no mark saying which bytes were written at it


def test_a_geometry_recorded_later_never_certifies_bytes_that_were_already_there():
    """The recorded size is a claim about the bytes written AFTER it, like the persisted width.
    Output from before any size was known stays of unknown geometry: the export is refused while
    it is in the tail, and is back when the screen was erased and homed after the mark."""
    scrollback._buffer_append(KEY, SHELL)  # written before anything was known
    scrollback.note_cols(KEY, 80)
    scrollback._LAST_ROWS[KEY] = 24
    scrollback.note_geometry(KEY, 24, 80)
    assert refused() == "mixed_geometry"
    scrollback._buffer_append(KEY, b"more output\r\n")  # after the mark, on the old screen
    assert refused() == "mixed_geometry"
    scrollback._buffer_append(KEY, b"\x1b[2J")  # an erase without home: the cursor is unknown
    assert refused() == "mixed_geometry"
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[2Jrepainted")
    export = scrollback.screen_export(KEY)
    assert export.grid[0].rstrip() == "repainted" and not any(r.strip() for r in export.grid[1:])
    # The same geometry again changes nothing; the mark stays where it was.
    mark = scrollback._GEOM_MARK[KEY]
    scrollback.note_geometry(KEY, 24, 80)
    assert scrollback._GEOM_MARK[KEY] == mark and scrollback.screen_export(KEY) == export


def test_the_export_never_infers_a_height_from_the_stream():
    """The forgiving readers guess the height from the tallest row an agent addressed. A guess
    that is too small scrolls the top of a wrapped value off a grid the real screen still shows
    in full, so the exporter refuses until a height was observed."""
    raw = (FIX / "opencode_permission_shell.raw").read_bytes()
    scrollback.note_cols(KEY, OC[1])
    scrollback._buffer_append(KEY, raw)
    assert vtscreen.infer_rows(raw) > 0 and scrollback.live_tail_frame(KEY)[1]  # they do render
    assert refused() == "geometry"


def test_the_export_is_refused_for_a_mixed_width_ring():
    paint(SHELL, 24, 80)
    scrollback.note_cols(KEY, 120, persist=False)  # a viewer of another width joined
    assert scrollback.ring_cols(KEY) is None
    assert refused() == "mixed_width"
    assert scrollback.live_tail_text(KEY)  # the forgiving reader falls back to stripping; not this


def test_the_export_is_refused_when_the_render_comes_out_empty():
    paint(b"\x1b[?25l\x1b[H   \r\n  \x1b[?25h", 24, 80)
    assert refused() == "empty"


def test_the_export_never_shows_the_frame_from_before_a_trailing_full_screen_erase():
    raw = (FIX / "opencode_permission_shell.raw").read_bytes()
    paint(raw, *OC)
    assert scrollback.screen_export(KEY)
    scrollback._buffer_append(KEY, b"\x1b[2J")
    # The reviewer's reader shows the dialog from before the erase. The exporter does not.
    assert "Permission required" in scrollback.live_tail_text(KEY, 100_000)
    assert refused() == "empty"
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[Jafter the repaint")
    assert scrollback.screen_export(KEY).grid[0].rstrip() == "after the repaint"


def test_the_export_has_no_escape_strip_fallback_for_a_frame_it_cannot_trust():
    paint(b"$ pasword\x08\x08\x08\x08sword\r\n", 24, 80)  # a backspace: not modelled exactly
    assert "pasword" in scrollback.live_tail_text(KEY)  # the forgiving reader shows something
    assert refused() == "untrusted"


@pytest.mark.parametrize(
    "rows,cols", [(250, 450), (201, 500), (300, 334), (vtscreen.MAX_ROWS + 1, 10), (10, 501)]
)
def test_a_geometry_over_the_cell_cap_is_refused_before_anything_is_rendered(
    monkeypatch, rows, cols
):
    def never(*_a, **_k):
        raise AssertionError("an oversize grid must not reach the renderer")

    paint(b"x", rows, cols)  # one character: the refusal is about the geometry, not the content
    for name in ("render_grid", "render_cells", "render", "_feed_screen"):
        monkeypatch.setattr(vtscreen, name, never)
    assert refused() == "oversize"


def test_a_geometry_at_the_cell_cap_is_rendered():
    assert scrollback.SCREEN_EXPORT_MAX_CELLS == 100_000
    export = scrollback.screen_export(paint(b"corner", 200, 500))
    assert export.rows * export.cols == 100_000 and export.grid[0].startswith("corner")


def test_a_resize_between_the_two_reads_of_the_geometry_refuses(monkeypatch):
    paint(SHELL, 24, 80)
    real = scrollback._coherent_tail

    def resized(key, since):
        raw = real(key, since)
        scrollback._LAST_ROWS[key] = 50  # the viewer resized while the ring was being copied
        scrollback.note_geometry(key, 50, 80)
        return raw

    monkeypatch.setattr(scrollback, "_coherent_tail", resized)
    assert refused() == "geometry_changed"


def test_the_version_moves_with_every_change_the_export_could_see():
    paint(SHELL, 24, 80)
    first = scrollback.screen_export(KEY)
    assert first.version == scrollback.screen_version(KEY)
    seen = {first.version}
    scrollback._buffer_append(KEY, b"more")
    seen.add(scrollback.screen_version(KEY))
    scrollback._LAST_ROWS[KEY] = 30
    seen.add(scrollback.screen_version(KEY))
    scrollback.note_cols(KEY, 100, persist=False)
    seen.add(scrollback.screen_version(KEY))
    scrollback._reset_ring(KEY)
    seen.add(scrollback.screen_version(KEY))
    assert len(seen) == 5
    with pytest.raises(scrollback.ScreenUnavailable) as exc:
        scrollback.screen_export(KEY)
    assert exc.value.version == scrollback.screen_version(KEY)  # a refusal carries one too


def test_the_export_only_reads(monkeypatch):
    """No attach, no signal, no write, no resize: every seam that could touch the owner's
    terminal raises, and the export still answers."""
    import asyncio
    import fcntl
    import os
    import subprocess

    from agent_sessions import ptybridge, session_input, webterm

    paint((FIX / "opencode_permission_shell.raw").read_bytes(), *OC)
    scrollback.screen_export(KEY)  # hydrated and rendered once before the seams close
    before = (bytes(scrollback._BUFFERS[KEY]), scrollback.screen_version(KEY))

    def touched(*_a, **_k):
        raise AssertionError("the export touched the session")

    for module, name in (
        (os, "write"),
        (os, "kill"),
        (os, "killpg"),
        (os, "openpty"),
        (fcntl, "ioctl"),
        (subprocess, "Popen"),
        (subprocess, "run"),
        (asyncio, "create_subprocess_exec"),
        (webterm, "_set_winsize"),
        (ptybridge, "attach_argv"),
        (ptybridge, "launch_argv"),
        (session_input, "send_input"),
        (session_input, "register_writer"),
        (scrollback, "_buffer_append"),
        (scrollback, "_reset_ring"),
        (scrollback, "note_cols"),
        (scrollback, "note_attach"),
        (scrollback, "note_viewer_attached"),
        (scrollback, "note_geometry"),
        (scrollback, "note_reader_geometry"),
    ):
        monkeypatch.setattr(module, name, touched)
    for _ in range(5):
        assert scrollback.screen_export(KEY).rows == OC[0]
    assert (bytes(scrollback._BUFFERS[KEY]), scrollback.screen_version(KEY)) == before


def test_the_export_is_safe_against_the_ring_from_a_worker_thread():
    """The export runs off the event loop while the loop appends, resets and evicts. It copies
    under `_RING_LOCK` and renders outside it: no exception but a refusal, and never a torn grid."""
    errors: list[BaseException] = []
    stop = threading.Event()
    keys = [f"claude:worker-{i}" for i in range(scrollback._MAX_BUFFERS + 8)]
    for key in keys:
        sized(10, 40, key)

    def reader() -> None:
        i = 0
        try:
            while not stop.is_set():
                try:
                    export = scrollback.screen_export(keys[i % len(keys)])
                except scrollback.ScreenUnavailable:
                    pass
                else:
                    assert len(export.grid) == 10 and {len(r) for r in export.grid} == {40}
                i += 1
        except BaseException as exc:  # noqa: BLE001 — catching a race is the whole point
            errors.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for n in range(200):
            key = keys[n % len(keys)]
            scrollback._buffer_append(key, b"\x1b[2;3Hsome output\r\n")
            if n % 37 == 0:
                scrollback._reset_ring(key)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors, errors[:3]


# ── the geometry is a claim about the bytes authored at it ────────────
#
# A session's persisted output, a restart, and the headless reader attaching: what
# `session_stream.SessionStream.start` does is `note_reader_geometry(key, <its PTY's size>)`.

# Written at 6 rows × 10 columns: a secret the terminal wrapped over rows 0-1, then four lines.
WRAPPED = f"{SECRET}\r\nl2\r\nl3\r\nl4\r\nl5".encode()


def persisted(rows: int, cols: int, data: bytes = WRAPPED) -> None:
    """Output drawn at ``rows × cols`` by an earlier process: only the disk holds it now."""
    paint(data, rows, cols)
    restart()


def test_the_same_geometry_before_and_after_a_restart_is_available_at_once():
    persisted(6, 10)
    export = scrollback.screen_export(KEY)  # before any reader attached: the mark was persisted
    assert (export.rows, export.cols) == (6, 10)
    assert frames.redact_grid(export.grid, 10, [SECRET])[:2] == [MASK * 10, MASK * 6 + " " * 4]
    assert scrollback.reader_size(KEY) == (6, 10)  # the reader attaches at the size it had
    scrollback.note_reader_geometry(KEY, 6, 10)  # ...and that changes nothing
    assert scrollback.screen_export(KEY).grid == export.grid
    scrollback._buffer_append(KEY, b"\r\nl6")
    assert scrollback.screen_export(KEY).grid[5].rstrip() == "l6"


def test_a_reader_at_another_height_never_exports_a_frame_that_cuts_a_wrapped_secret():
    """80 × 40 before, 80 × 24 after, in small: the output was drawn at 6 rows and the reader's
    PTY has 5. Replayed on 5 rows the first row of the wrapped secret scrolls off and its rest
    would go out unmasked, although the real screen showed it whole. So a HEIGHT change is a
    geometry change like a width change: nothing drawn before it is rendered at the new one."""
    persisted(6, 10)
    wrong = vtscreen.render_grid(WRAPPED, 5, 10)
    assert SECRET[10:] in "".join(wrong) and SECRET[:10] not in "".join(wrong)  # the hazard
    assert frames.redact_grid(wrong, 10, [SECRET]) == wrong  # ...which no redaction can see

    seen: list[str] = []

    def attempt() -> str | None:
        try:
            export = scrollback.screen_export(KEY)
        except scrollback.ScreenUnavailable as exc:
            return exc.reason
        seen.append("".join(export.grid))
        return None

    scrollback.note_reader_geometry(KEY, 5, 10)  # the reader attached at 5 rows
    assert (scrollback._LAST_ROWS[KEY], scrollback.ring_cols(KEY)) == (5, 10)
    assert bytes(scrollback._BUFFERS[KEY]) == WRAPPED  # the same width: the ring is kept
    assert attempt() == "mixed_geometry"
    for chunk in (b"\r\n", b"after\r\n", b"\x1b[2Kmore", b"\x1b[2J", b"\x1b[3;1Hx"):
        scrollback._buffer_append(KEY, chunk)  # new output, on a screen that still shows the old
        assert attempt() == "mixed_geometry", chunk
    restart()  # ...and a restart in the middle of that certifies nothing either
    assert attempt() == "mixed_geometry"
    assert seen == []  # not one frame left while the old bytes could be on it

    # Coherent again: the program erased and homed after the mark, as a repaint on a resize does.
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[2Jfresh\r\nscreen")
    assert attempt() is None
    export = scrollback.screen_export(KEY)
    assert (export.rows, export.cols) == (5, 10)
    assert [row.rstrip() for row in export.grid] == ["fresh", "screen", "", "", ""]
    for part in (SECRET, SECRET[:10], SECRET[10:], "l5"):
        assert all(part not in frame for frame in seen), part


def test_without_an_erase_the_export_is_back_when_the_old_bytes_left_the_rendered_tail(
    monkeypatch,
):
    monkeypatch.setattr(scrollback, "_SCREEN_TAIL_BYTES", 64)
    persisted(6, 10)
    scrollback.note_reader_geometry(KEY, 3, 10)
    for n in range(5):
        scrollback._buffer_append(KEY, f"line {n:04d}\r\n".encode())  # 55 bytes after the mark
        assert refused() == "mixed_geometry"
    scrollback._buffer_append(KEY, b"line 0005\r\nlast")  # the tail is all after the mark now
    export = scrollback.screen_export(KEY)
    assert [row.rstrip() for row in export.grid] == ["line 0004", "line 0005", "last"]
    assert SECRET[10:] not in "".join(export.grid)


def test_a_reader_at_another_width_resets_the_ring_and_a_frame_follows_the_repaint():
    """120 × 40 before, 80 × 24 after: the width differs, so the ring's own rule for a width
    change applies (the retained bytes are dropped, #245) and nothing of them is ever rendered."""
    persisted(40, 120, f"token {SECRET} drawn at 120 columns\r\n".encode())
    assert scrollback.screen_export(KEY).cols == 120
    scrollback.note_reader_geometry(KEY, 24, 80)
    assert not scrollback._BUFFERS[KEY] and scrollback.ring_cols(KEY) == 80
    assert refused() == "no_output"  # until the program has written at the new geometry
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[Jrepainted at 80")
    export = scrollback.screen_export(KEY)
    assert (export.rows, export.cols) == (24, 80) and export.grid[0].rstrip() == "repainted at 80"
    restart()
    assert scrollback.screen_export(KEY).grid == export.grid  # and that survives a restart


def test_a_viewers_height_only_resize_is_a_new_geometry_too():
    paint(WRAPPED, 6, 10)
    assert scrollback.screen_export(KEY).rows == 6
    scrollback._LAST_ROWS[KEY] = 3  # `webterm.run` on a height-only resize: no ring reset
    scrollback.note_geometry(KEY, 3, 10)
    assert refused() == "mixed_geometry"
    scrollback.note_geometry(KEY, 6, 10)  # back to the old height: still a new mark
    scrollback._LAST_ROWS[KEY] = 6
    assert refused() == "mixed_geometry"


def test_the_repaint_nudge_and_an_unfinished_one_leave_no_claim_on_the_bytes_between():
    paint(b"\x1b[H\x1b[2Jbefore", 24, 80)
    assert scrollback.screen_export(KEY)
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[2Jdrawn while shrunk")
    scrollback.note_geometry_break(KEY)  # the nudge restored the size
    assert refused() == "mixed_geometry"
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[2Jrestored")
    assert scrollback.screen_export(KEY).grid[0].rstrip() == "restored"
    scrollback.forget_geometry(KEY)  # a nudge that never restored it
    assert refused() == "geometry" and not scrollback._geom_path(KEY).exists()
    restart()
    assert refused() == "geometry"


def test_the_mark_follows_the_mirror_through_a_head_trim_and_a_dropped_ring(monkeypatch):
    monkeypatch.setattr(scrollback, "_MAX_BUF", 1000)
    monkeypatch.setattr(scrollback, "_DISK_TRIM_SLACK", 1000)
    old = (b"o" * 48 + b"\r\n") * 30  # 1500 bytes drawn at 6 rows
    paint(old, 6, 60)
    scrollback._LAST_ROWS[KEY] = 9
    scrollback.note_geometry(KEY, 9, 60)
    assert scrollback._geom_path(KEY).read_text() == "9 60 1500"
    scrollback._buffer_append(KEY, (b"n" * 48 + b"\r\n") * 12)  # 600 more: the mirror is trimmed
    assert scrollback._scrollback_path(KEY).stat().st_size == 1000
    assert scrollback._geom_path(KEY).read_text() == "9 60 400"  # 400 old bytes are still in it
    for forget in (restart, lambda: scrollback._drop_buffer(KEY)):
        forget()
        assert refused() == "mixed_geometry"  # ...and are not certified by either way back
        assert scrollback._GEOM_MARK[KEY][:2] == (9, 60)
    scrollback._buffer_append(KEY, b"\x1b[H\x1b[2Jclean")
    assert scrollback.screen_export(KEY).rows == 9
    scrollback.clear_scrollback([KEY])
    assert not scrollback._geom_path(KEY).exists() and KEY not in scrollback._GEOM_MARK


# ── frames: redaction on the untrimmed grid ───────────────────────────


def export_of(data: bytes, rows: int, cols: int) -> scrollback.ScreenExport:
    return scrollback.screen_export(paint(data, rows, cols))


def test_a_complete_secret_in_one_row_is_masked_cell_for_cell():
    export = export_of(f"$ export TOKEN={SECRET} # set\r\n$ ".encode(), 6, 40)
    rows = frames.redact_grid(export.grid, export.cols, [SECRET])
    plain = "$ export TOKEN=" + SECRET + " # set"
    assert rows[0] == ("$ export TOKEN=" + MASK * len(SECRET) + " # set").ljust(40)
    assert len(rows) == 6 and {len(row) for row in rows} == {40}  # the grid keeps its shape
    # every unmasked cell is where it was
    assert [i for i, (a, b) in enumerate(zip(export.grid[0], rows[0], strict=True)) if a != b] == (
        list(range(plain.index(SECRET), plain.index(SECRET) + len(SECRET)))
    )
    assert rows[1:] == list(export.grid[1:])
    assert SECRET not in "\n".join(rows)


@pytest.mark.parametrize("cols,spans", [(16, 2), (12, 2), (8, 3), (6, 3)])
def test_a_secret_wrapped_across_rows_is_masked_in_every_row_it_spans(cols, spans):
    lead = "k="
    export = export_of((lead + SECRET + " ok").encode(), 6, cols)
    whole = lead + SECRET + " ok"
    assert [r for r in export.grid if r.strip()] == [
        whole[i : i + cols].ljust(cols) for i in range(0, len(whole), cols)
    ]
    assert not any(SECRET in row for row in export.grid)  # no single row holds it
    rows = frames.redact_grid(export.grid, cols, [SECRET])
    assert "".join(rows).startswith(lead + MASK * len(SECRET) + " ok")
    touched = [i for i, row in enumerate(rows) if MASK in row]
    assert len(touched) >= spans and touched == list(range(len(touched)))
    assert {len(row) for row in rows} == {cols}  # a masked line is never longer than cols
    for part in (SECRET, SECRET[:6], SECRET[-6:]):
        assert part not in "".join(rows) and part not in "\n".join(rows)


def test_a_secret_containing_spaces_wrapped_so_a_row_ends_in_blanks_is_masked():
    """`AB  CD` at width 4, through the real exporter: the rows are `AB  ` and `CD  `. Trimmed
    first, they would join to `ABCD` and the complete value would be missed."""
    export = export_of(b"AB  CD", 3, 4)
    assert export.grid == ("AB  ", "CD  ", "    ")
    rows = frames.redact_grid(export.grid, 4, ["AB  CD"])
    assert rows == [MASK * 4, MASK * 2 + "  ", "    "]
    assert frames.transport_lines(rows) == [MASK * 4, MASK * 2, ""]
    # the wrong order, for the record: trimming before matching loses the value
    assert "AB  CD" not in "".join(row.rstrip() for row in export.grid)


def test_a_longer_secret_with_spaces_at_the_wrap_is_masked():
    secret = "s3cr  et-val9"
    export = export_of(("xx" + secret + "!").encode(), 4, 8)
    assert export.grid[0] == "xxs3cr  " and export.grid[1] == "et-val9!"
    rows = frames.redact_grid(export.grid, 8, [secret])
    assert rows[:2] == ["xx" + MASK * 6, MASK * 7 + "!"]


def test_matching_refuses_a_grid_that_was_trimmed():
    with pytest.raises(frames.BadGrid):
        frames.redact_grid(["AB", "CD"], 4, ["AB  CD"])
    with pytest.raises(frames.BadGrid):
        frames.redact_grid(["ABCD"], 0, ["ABCD"])


def test_the_same_variants_as_redact_text_are_masked():
    secret = 'pa"ss w0rd/x'
    variants = template_secrets._variants(secret)
    assert len(variants) == 4  # raw, JSON-escaped, URL-encoded, form-encoded
    for variant in variants:
        text = f"v={variant};"
        assert template_secrets.redact_text(text, [secret]) == "v=[secret];"
        (row,) = frames.redact_grid([text.ljust(40)], 40, [secret])
        assert row == ("v=" + MASK * len(variant) + ";").ljust(40), variant


def test_every_occurrence_and_overlapping_values_are_masked():
    rows = frames.redact_grid(
        ["abcdefgh-abcdefgh  ", "xx cdefghij xx     "], 19, ["abcdefgh", "cdefghij"]
    )
    assert rows == [MASK * 8 + "-" + MASK * 8 + "  ", "xx " + MASK * 8 + " xx     "]
    (row,) = frames.redact_grid(["abcdefghij "], 11, ["abcdefgh", "cdefghij"])
    assert row == MASK * 10 + " "  # the union of two overlapping values


def test_documented_limit_a_secret_whose_beginning_scrolled_off_the_top_is_not_masked():
    """THE DOCUMENTED LIMIT of a terminal view (#2056), pinned so no later change can claim more
    than the share dialog says: a known secret is hidden when it is COMPLETELY on the screen. A
    value that is only partly on screen is NOT masked. Here its first row scrolled off the top;
    what is left is a fragment, and fragments are deliberately not matched (masking by partial
    match would tell an observer which prefixes are secrets: the oracle `share_api.py` documents).
    If this test starts failing because the fragment IS masked, the contract changed: change the
    dialog's wording and `docs/invariants/federation.md` in the same change, or revert."""
    cols = 10
    wrapped = (SECRET + "\r\n").encode()  # 16 characters: rows `s3cret-val` and `ue-123`
    export = export_of(wrapped + b"one", 3, cols)
    assert export.grid[0].rstrip() == SECRET[:cols] and export.grid[1].rstrip() == SECRET[cols:]
    whole = frames.redact_grid(export.grid, cols, [SECRET])
    assert whole[0] == MASK * cols and whole[1].rstrip() == MASK * (len(SECRET) - cols)

    scrollback._buffer_append(KEY, b"\r\ntwo")  # one more line: the first row leaves the screen
    export = scrollback.screen_export(KEY)
    assert export.grid[0].rstrip() == SECRET[cols:]  # `ue-123`, the end of the secret
    rows = frames.redact_grid(export.grid, cols, [SECRET])
    assert rows == list(export.grid) and MASK not in "".join(rows)
    assert frames.transport_lines(rows)[0] == SECRET[cols:]  # sent as it is: the stated limit


def test_documented_limit_a_secret_an_application_wrapped_inside_a_pane_is_not_masked():
    """ALSO A DOCUMENTED LIMIT (#2056), pinned for the same reason: the row-joined match finds a
    value the TERMINAL wrapped at the screen's edge. A value an application breaks across lines
    itself (a pane narrower than the screen, a border between the parts) is completely on screen
    and still not found: the cells between its parts are not part of the value. Matching "with
    anything in between" would be fragment matching, which is deliberately not done."""
    top, bottom = SECRET[:8], SECRET[8:]
    pane = f"| {top} |  other pane\r\n| {bottom} |  other pane".encode()
    export = export_of(pane, 4, 30)
    assert top in export.grid[0] and bottom in export.grid[1]
    rows = frames.redact_grid(export.grid, 30, [SECRET])
    assert rows == list(export.grid) and MASK not in "".join(rows)


def test_a_fragment_of_a_secret_is_never_matched():
    for part in (SECRET[:-1], SECRET[1:], SECRET[:8], SECRET[4:12]):
        (row,) = frames.redact_grid([part.ljust(30)], 30, [SECRET])
        assert row == part.ljust(30), part
    assert share_api.CUT_TRIM_MIN  # the oracle note this rule follows lives in share_api.py


def test_trailing_blanks_are_dropped_per_line_only_after_masking():
    assert frames.transport_lines(["ab   ", "     ", "  cd ", MASK * 2 + "   "]) == [
        "ab",
        "",
        "  cd",
        MASK * 2,
    ]
    # a line never carries a control, a line separator or a bidirectional override
    assert frames.transport_lines(["a\x85b c‮d\x07 "]) == ["a�b�c�d�"]
    frames.check_lines(["ok", ""], 2, 2)
    for lines, rows, cols in (
        (["a", "b", "c"], 2, 5),  # more lines than rows
        (["abcdef"], 2, 5),  # a line longer than cols
        (["a\x1b[31mb"], 2, 20),
        (["a\nb"], 2, 5),
        (["a‮b"], 2, 5),
        (["a\x85b"], 2, 5),
        ([7], 2, 5),
        ("ab", 2, 5),
        (None, 2, 5),
    ):
        with pytest.raises(ValueError):
            frames.check_lines(lines, rows, cols)


def test_redaction_reads_the_values_per_call_and_fails_closed(monkeypatch):
    grid = (f"token {SECRET}".ljust(30),)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    assert frames.redact(grid, 30) == ["token " + MASK * len(SECRET)]

    def unavailable():
        raise template_secrets.RedactionUnavailable("store unreadable")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    with pytest.raises(template_secrets.RedactionUnavailable):
        frames.redact(grid, 30)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])
    assert frames.redact(grid, 30) == [f"token {SECRET}"]  # nothing known: nothing masked


def test_the_real_redaction_set_masks_a_registered_value():
    template_secrets.register_typed([SECRET])
    assert SECRET in template_secrets.redaction_values()
    assert frames.redact((f"x {SECRET} y".ljust(40),), 40) == ["x " + MASK * len(SECRET) + " y"]


def test_the_fingerprint_is_of_the_redacted_frame_and_bound_to_the_grant():
    grant_a, grant_b = "a" * 22, "b" * 22
    other = "t0ken-other-4567"
    assert len(other) == len(SECRET)
    one = frames.transport_lines(frames.redact_grid((f"k={SECRET}".ljust(30),), 30, [SECRET]))
    two = frames.transport_lines(frames.redact_grid((f"k={other}".ljust(30),), 30, [other]))
    assert one == two  # two screens that differ only inside a masked value
    fp = frames.fingerprint(grant_a, 1, 30, one)
    assert frames.valid_fingerprint(fp) and fp == frames.fingerprint(grant_a, 1, 30, two)
    assert fp != frames.fingerprint(grant_b, 1, 30, one)  # never comparable across grants
    assert fp != frames.fingerprint(grant_a, 1, 30, ["k=changed"])
    assert fp != frames.fingerprint(grant_a, 2, 30, one) != frames.fingerprint(grant_a, 1, 31, one)
    for bad in (None, 7, "", "g" * 32, "A" * 32, "a" * 31, "a" * 33, ["a" * 32]):
        assert not frames.valid_fingerprint(bad)


def test_the_fingerprint_fails_closed_without_the_key(monkeypatch):
    def no_key(*_a, **_k):
        raise template_secrets.SecretKeyUnavailable("key unreadable")

    monkeypatch.setattr(template_secrets, "keyed_digest", no_key)
    with pytest.raises(template_secrets.SecretKeyUnavailable):
        frames.fingerprint("a" * 22, 1, 2, ["x"])


# ── characters that are not one cell wide (#2056 review) ──────────────

SYNTHETIC = "SYNTHETIC-12345"

NOT_ONE_CELL = {
    "a combining mark": "é",
    "six combining marks": "e" + "́" * 6,
    "a zero-width joiner": "a‍b",
    "a variation selector": "❤️",
    "a wide CJK character": "中",
    "a fullwidth letter": "Ａ",
    "an emoji": "\U0001f600",
    "an invalid byte sequence": b"\xff".decode("utf-8", "replace"),
    "a private-use character": "",
    "a bidi control": "‮",
    # Letters of neutral East Asian width and no combining class that a terminal gives NO column
    # or two: no property test catches these, which is why the rule is an allowlist.
    "a zero-width Hangul filler": "\u1160",
    "a Hangul trailing consonant": "\u11a8",
    "a Hangul syllable": "\ud55c",
    "an Arabic letter": "\u0627",
    "a Devanagari letter": "\u0915",
    "a Thai vowel sign": "\u0e31",
    "a soft hyphen": "\u00ad",
    "an unassigned code point": "\u0378",
}


def test_the_renderer_knows_which_characters_are_exactly_one_cell():
    for ch in ("a", " ", "~", "é", "ß", "─", "│", "✻", "⏺", "→", "•", "█"):
        assert vtscreen._one_cell(ch), repr(ch)
    for name, text in NOT_ONE_CELL.items():
        assert not all(vtscreen._one_cell(ch) for ch in text), name


@pytest.mark.parametrize("name", sorted(NOT_ONE_CELL))
def test_a_frame_holding_a_character_that_is_not_one_cell_wide_is_refused(name):
    """This renderer gives every character one column. One that a terminal draws in none or in
    two shifts everything after it, so the grid is not the screen: no export, no fallback."""
    text = NOT_ONE_CELL[name]
    assert vtscreen.render_grid(f"before {text} after".encode("utf-8", "replace"), 4, 40) is None
    paint(f"$ echo {text}\r\n".encode("utf-8", "replace"), 24, 80)
    assert refused() == "untrusted"
    # The readers that only need the text are unchanged: they still show the frame.
    assert vtscreen.render(f"x{text}y".encode("utf-8", "replace"), 4, 40) is not None


def test_a_wholly_visible_secret_behind_combining_marks_never_leaves_as_a_fragment():
    """The reviewer's case. On a 2 × 10 screen a real terminal draws `e` with six combining
    marks in ONE cell, so `SYNTHETIC-12345` after it is whole on the screen (9 + 6 cells). Giving
    each mark a column scrolled its beginning off this grid and sent `THETIC-123` / `45`
    unmasked. Now there is no grid at all."""
    data = ("e" + "́" * 6 + SYNTHETIC).encode()
    assert vtscreen.render_grid(data, 2, 10) is None
    paint(data, 2, 10)
    assert refused() == "untrusted"
    # The control: the same secret without the marks is exported and masked completely.
    control = vtscreen.render_grid(("e" + SYNTHETIC).encode(), 2, 10)
    assert control == ["eSYNTHETIC", "-12345    "]
    masked = frames.redact_grid(control, 10, [SYNTHETIC])
    sent = frames.transport_lines(masked)
    assert "SYNTH" not in "".join(sent) and "12345" not in "".join(sent)
    assert sent == ["e" + frames.MASK * 9, frames.MASK * 6]


def test_a_wholly_visible_secret_behind_zero_width_hangul_never_leaves_as_a_fragment():
    """The second reviewer's case: U+1160 is category Lo, neutral width, combining class 0, and
    a terminal gives it no column. Same screen, same outcome as the combining marks: no grid."""
    data = ("e" + "\u1160" * 6 + SYNTHETIC).encode()
    assert vtscreen.render_grid(data, 2, 10) is None
    paint(data, 2, 10)
    assert refused() == "untrusted"


def test_one_cell_is_an_allowlist_so_an_unlisted_script_is_refused_whatever_its_properties():
    import unicodedata

    # Every character outside ASCII that is accepted lies in a listed block...
    accepted = [c for c in map(chr, range(0x80, 0x30000)) if vtscreen._one_cell(c)]
    assert accepted
    for ch in accepted:
        assert any(low <= ord(ch) <= high for low, high in vtscreen._ONE_CELL_BLOCKS), hex(ord(ch))
        assert not unicodedata.combining(ch)
        assert unicodedata.east_asian_width(ch) not in ("W", "F")
        assert unicodedata.category(ch)[0] not in ("M", "C"), hex(ord(ch))
    # ...and no Hangul, CJK, kana, Arabic, Hebrew, Indic or Thai character is.
    for low, high in ((0x0590, 0x0FFF), (0x1100, 0x11FF), (0x2E80, 0xD7FF), (0xF900, 0xFFEF)):
        assert not any(vtscreen._one_cell(chr(c)) for c in range(low, high + 1)), hex(low)


def test_a_character_written_earlier_and_erased_still_refuses_the_frame():
    """Conservative on purpose: the columns after it were already wrong when later text was
    placed, and what scrolled off because of it cannot be known."""
    assert vtscreen.render_grid("é\r\n".encode() + b"\x1b[2J\x1b[Hclean", 4, 20) is None
