"""A paired instance views a terminal session as a redacted text frame (#2056): the verb, the
grant kind, the settle rule, the receiving side.

Two real services joined by the in-memory transport (`test_federation_sharing.paired`). A terminal
session is named by key; the `live` fixture answers `shares.session_state` for it, and its screen
is what the test writes through `scrollback._buffer_append` (the chokepoint both the viewer bridge
and the headless reader write through) at a geometry recorded the way a viewer's attach records
it. `wire` holds every answer body exactly as a channel wrote it.

The test at the end runs two real app instances over a real `dtach` session, drained by the
production headless reader (`session_stream.SessionStream`).
"""

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import contextlib
import inspect
import os
import shutil
import sqlite3
import subprocess
import termios
import threading
import time
import tty
import uuid
from pathlib import Path

import pytest

from agent_sessions import (
    engines,
    ptybridge,
    scrollback,
    session_input,
    session_stream,
    shares,
    template_secrets,
    vtscreen,
    webterm,
)
from agent_sessions.federation import channel as ch
from agent_sessions.federation import frames, grants, remote, sharing, store, terminal
from federation_helpers import until
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_remote import Receiver, entry
from test_federation_routes import (  # noqa: F401 — `two_instances` is a fixture
    _client,
    _login,
    _trusted_peer,
    _wait,
    two_instances,
)
from test_federation_service import _same_channel
from test_federation_sharing import (  # noqa: F401 — fixtures
    ACTIONS,
    EMOJI,
    NO_GRANT,
    UNAVAILABLE,
    Pair,
    _end_grant,
    _pair_instances,
    _rows_for,
    data_bodies,
    fake,
    op_turn,
    paired,
    ref,
    sent,
    wire,
)

FIX = Path(__file__).parent / "fixtures"
OC = (46, 65)  # rows, cols the opencode captures were authored at
SECRET = "s3cret-value-123"
MASK = "█"
MARK = "SCREEN-CONTENT-MARKER"
SHELL = f"$ echo {MARK}\r\n{MARK}\r\n$ ".encode()
SETTLING = b'{"ok":true,"result":{"settling":true}}'


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _geometry(monkeypatch):
    """The geometry tables are process-wide and not part of conftest's ring isolation."""
    monkeypatch.setattr(scrollback, "_LAST_COLS", {})
    monkeypatch.setattr(scrollback, "_LAST_ROWS", {})
    monkeypatch.setattr(scrollback, "_RING_MIXED", set())


def term_key() -> str:
    return f"claude:{uuid.uuid4()}"


def quiet(key: str) -> None:
    """The screen has shown nothing for a minute."""
    scrollback._LAST_VISIBLE_OUTPUT_AT[key] = time.time() - 60


def busy(key: str) -> None:
    """The screen never settles: its last visible output is always "just now"."""
    scrollback._LAST_VISIBLE_OUTPUT_AT[key] = time.time() + 3600


def write(key: str, data: bytes, *, settled: bool = True) -> None:
    scrollback._buffer_append(key, data)
    if settled:
        quiet(key)


def paint(key: str, data: bytes = SHELL, rows: int = 24, cols: int = 80, **kw) -> str:
    """A terminal session's ring at a geometry a viewer reported."""
    scrollback._LAST_ROWS[key] = rows
    scrollback.note_attach_width(key, cols)
    scrollback.note_geometry(key, rows, cols)
    write(key, data, **kw)
    return key


async def terminal_grant(p: Pair, data: bytes = SHELL, **kw) -> tuple[store.Grant, str]:
    key = paint(term_key(), data, **kw)
    return await p.grant(key), key


def frames_sent(bodies: list[bytes]) -> list[bytes]:
    return [b for b in bodies if b'"lines"' in b]


def answers(bodies: list[bytes]) -> list[bytes]:
    """The recorded answers to this test's own calls (not beta's background list reads)."""
    return [b for b in data_bodies(bodies) if b'"grants"' not in b]


@pytest.fixture
def renders(monkeypatch):
    """How often the exporter rendered, per session key."""
    calls: list[str] = []
    real = scrollback.screen_export

    def counted(key):
        calls.append(key)
        return real(key)

    monkeypatch.setattr(scrollback, "screen_export", counted)
    return calls


# ── the frame ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_quiet_screen_answers_at_once_with_the_current_frame(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        assert made.kind == "terminal" and made.level == "view"
        wire.clear()
        started = time.monotonic()
        got = await p.call("terminal.frame", ref(made))  # no `wait`: a quiet screen needs none
        assert time.monotonic() - started < terminal.SETTLE_S + 2
        assert set(got) == {"fingerprint", "cols", "rows", "lines", "settled"}
        assert (got["rows"], got["cols"], got["settled"]) == (24, 80, True)
        assert got["lines"][:3] == [f"$ echo {MARK}", MARK, "$"]
        assert len(got["lines"]) == 24 and set(got["lines"][3:]) == {""}
        assert frames.valid_fingerprint(got["fingerprint"])
        sent(wire, got)  # the measured answer is the one that was written
        # No byte stream, no escape sequence, no key, no cwd: lines of text and nothing else.
        body = frames_sent(wire)[0]
        assert b"\\u001b" not in body and key.encode() not in body and b"cwd" not in body


@pytest.mark.anyio
async def test_an_alternate_screen_tui_gives_a_usable_frame_on_a_first_read_and_after_a_reconnect(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    raw = (FIX / "opencode_permission_shell.raw").read_bytes()
    assert b"\x1b[?1049h" in raw  # an alternate-screen TUI
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, raw, rows=OC[0], cols=OC[1])
        first = await p.call("terminal.frame", ref(made))  # a first read: no `have`
        text = "\n".join(first["lines"])
        assert (first["rows"], first["cols"]) == OC and first["settled"] is True
        assert "Permission required" in text and "Allow once" in text and "Reject" in text
        assert all(len(line) <= OC[1] for line in first["lines"])
        assert "\x1b" not in text
        # It is the frame the owner's own strict reader shows.
        assert text.strip("\n") == scrollback.live_tail_frame(key, 100_000)[0]

        # The channel drops and comes back. A reconnect sends no `have` and gets the whole
        # current frame again: there is no cursor to resume, so nothing can be split.
        old = p.b.channel_for(p.a.peer_id)
        old.close("test")
        await until(lambda: p.b.channel_for(p.a.peer_id) not in (None, old))
        await _same_channel(p.a, p.b)
        again = await p.call("terminal.frame", ref(made))
        assert again == first


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case",
    [
        "no_output",
        "another_geometry",
        "unknown_width",
        "unknown_height",
        "mixed_width",
        "empty_render",
        "before_a_trailing_erase",
        "oversize_grid",
        "untrusted",
    ],
)
async def test_a_frame_that_cannot_be_rendered_and_trusted_is_unavailable_and_nothing_is_sent(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    case,
):
    async with paired(tmp_path, monkeypatch) as p:
        key = term_key()
        if case == "no_output":
            scrollback.note_cols(key, 80)
            scrollback._LAST_ROWS[key] = 24
            scrollback.note_geometry(key, 24, 80)
        elif case == "another_geometry":
            paint(key)  # drawn at 24 rows...
            scrollback._LAST_ROWS[key] = 12  # ...and the session is drained at 12 now
            scrollback.note_geometry(key, 12, 80)
        elif case == "unknown_width":
            scrollback._LAST_ROWS[key] = 24
            write(key, SHELL)
        elif case == "unknown_height":
            scrollback.note_cols(key, 80)
            write(key, SHELL + b"\x1b[20;1H" + MARK.encode())  # a height could be inferred
        elif case == "mixed_width":
            paint(key)
            scrollback.note_cols(key, 120, persist=False)
        elif case == "empty_render":
            paint(key, b"\x1b[?25l   \r\n  ")
        elif case == "before_a_trailing_erase":
            paint(key, SHELL + b"\x1b[2J")
            assert MARK in scrollback.live_tail_text(key)  # the forgiving reader still shows it
        elif case == "oversize_grid":
            paint(key, rows=250, cols=450)
        else:
            paint(key, SHELL + b"\x08")
        made = await p.grant(key)
        wire.clear()
        for extra in ({}, {"wait": 2}):
            assert await p.refused("terminal.frame", ref(made, **extra)) == "unavailable"
        bodies = answers(wire)
        assert bodies == [UNAVAILABLE, UNAVAILABLE]
        assert MARK.encode() not in b"".join(wire)
        assert p.a.frame_waits == {}


# ── redaction ─────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "cols,spans", [(60, 1), (16, 2), (8, 3)], ids=["in_one_row", "two_rows", "three_rows"]
)
async def test_a_complete_secret_is_masked_in_a_row_and_when_wrapped_across_rows(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    cols,
    spans,
):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    text = f"k={SECRET} ok"
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, text.encode(), rows=6, cols=cols)
        plain = scrollback.screen_export(key).grid
        assert SECRET in "".join(plain)  # the owner's screen holds it
        wire.clear()
        got = await p.call("terminal.frame", ref(made))
        lines = got["lines"]
        masked_rows = [i for i, line in enumerate(lines) if MASK in line]
        assert len(masked_rows) == spans
        assert "".join(line.ljust(cols) for line in lines).startswith(
            "k=" + MASK * len(SECRET) + " ok"
        )
        # A masked line is never longer than `cols`, and every unmasked cell kept its column.
        assert all(len(line) <= cols for line in lines)
        for row, line in zip(plain, lines, strict=True):
            for col, char in enumerate(line):
                assert char == MASK or char == row[col], (row, line)
        everything = b"".join(wire).decode()
        for part in (SECRET, SECRET[:8], SECRET[-8:]):
            assert part not in everything
        sent(wire, got)


@pytest.mark.anyio
async def test_a_secret_containing_spaces_wrapped_so_a_row_ends_in_blanks_is_masked(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """The `AB  CD` at width 4 case, through the real exporter and the verb."""
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: ["AB  CD"])
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, b"AB  CD", rows=3, cols=4)
        assert scrollback.screen_export(key).grid == (
            "AB  ",
            "CD  ",
            "    ",
        )  # a row ends in blanks
        wire.clear()
        got = await p.call("terminal.frame", ref(made))
        assert got["lines"] == [MASK * 4, MASK * 2, ""] and got["cols"] == 4
        assert b"AB" not in frames_sent(wire)[0] and b"CD" not in frames_sent(wire)[0]

        # ...and a value of ordinary length.
        secret = "s3cr  et-val9"
        monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
        longer, _key = await terminal_grant(p, ("xx" + secret + "!").encode(), rows=4, cols=8)
        got = await p.call("terminal.frame", ref(longer))
        assert got["lines"][:2] == ["xx" + MASK * 6, MASK * 7 + "!"]


@pytest.mark.anyio
async def test_a_read_between_the_two_writes_of_a_secret_gets_no_frame_with_its_first_part(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """The read arrives inside the settle window. It waits; the frame it then gets has the value
    masked. (Settling narrows this window. It is not what keeps a partly written value secret:
    past the busy bound a first part alone would be sent. See the documented-limit test.)"""
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    monkeypatch.setattr(terminal, "SETTLE_S", 2.0)
    first, second = SECRET[:9], SECRET[9:]
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, b"$ cat token\r\n")
        wire.clear()
        write(key, first.encode(), settled=False)  # the first write of the secret, just now
        call = asyncio.create_task(p.call("terminal.frame", ref(made, wait=20), timeout=30))
        await asyncio.sleep(0.3)
        assert not call.done()  # inside the settle window: no frame yet
        assert await p.call("terminal.frame", ref(made)) == {"settling": True}  # nor without wait
        written = time.monotonic()
        write(key, second.encode() + b"\r\n$ ", settled=False)  # the second write
        got = await call
        assert time.monotonic() - written >= terminal.SETTLE_S - 0.1  # it waited for the screen
        assert got["settled"] is True
        assert got["lines"][1] == MASK * len(SECRET)
        everything = b"".join(wire).decode()
        assert first not in everything and second not in everything and SECRET not in everything
        assert len(frames_sent(wire)) == 1  # one frame left, and it is the masked one


@pytest.mark.anyio
async def test_documented_limit_a_secret_whose_beginning_scrolled_off_is_sent_unmasked(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """THE DOCUMENTED LIMIT of a terminal view (#2056), end to end: a known secret is hidden when
    it is COMPLETELY on the screen. Once its beginning has scrolled off the top, what is left is a
    fragment, fragments are deliberately never matched, and the peer receives it as it is. This
    test pins that the limit exists and is exactly this wide; it is what the share dialog tells
    the operator. A change that makes it fail by masking the fragment changed the contract."""
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    cols = 10
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, f"{SECRET}\r\none".encode(), rows=3, cols=cols)
        whole = await p.call("terminal.frame", ref(made))
        assert whole["lines"] == [MASK * cols, MASK * (len(SECRET) - cols), "one"]

        write(key, b"\r\ntwo")  # one more line: the first row of the value leaves the screen
        wire.clear()
        partly = await p.call("terminal.frame", ref(made))
        assert partly["lines"] == [SECRET[cols:], "one", "two"]  # `ue-123`, not masked
        assert MASK not in "".join(partly["lines"])
        assert SECRET[cols:].encode() in frames_sent(wire)[0]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["redaction", "secret_key"])
async def test_redaction_unavailable_sends_nothing_and_recovery_serves_a_freshly_redacted_frame(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    renders,
    failure,
):
    """While redaction (or the keyed digest) is unavailable nothing is sent. The render made
    during the outage IS reused afterwards, and that is safe only because what is kept is the
    unredacted grid, never an answer: the call after recovery redacts it with the values of that
    call. A cached frame from the outage would show the secret; this one does not."""
    available = {"values": True, "key": True}
    real_digest = template_secrets.keyed_digest

    def values():
        if not available["values"]:
            raise template_secrets.RedactionUnavailable("store unreadable")
        return [SECRET]

    def digest(label, value):
        if not available["key"]:
            raise template_secrets.SecretKeyUnavailable("key unreadable")
        return real_digest(label, value)

    monkeypatch.setattr(template_secrets, "redaction_values", values)
    monkeypatch.setattr(template_secrets, "keyed_digest", digest)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, f"token {SECRET}\r\n{MARK}".encode())
        available["values" if failure == "redaction" else "key"] = False
        wire.clear()
        for extra in ({}, {"wait": 1}):
            assert await p.refused("terminal.frame", ref(made, **extra)) == "unavailable"
        assert set(answers(wire)) == {UNAVAILABLE}
        assert MARK.encode() not in b"".join(wire) and SECRET.encode() not in b"".join(wire)
        assert renders == [key]  # the screen WAS rendered during the outage, once

        available["values"] = available["key"] = True
        wire.clear()
        got = await p.call("terminal.frame", ref(made))  # the next call after recovery
        assert got["lines"][:2] == ["token " + MASK * len(SECRET), MARK] and got["settled"] is True
        assert renders == [key]  # served from the kept render, redacted for THIS call
        assert SECRET.encode() not in b"".join(wire)

        # A value that becomes known later masks the same kept render on the next call.
        monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET, MARK])
        later = await p.call("terminal.frame", ref(made))
        assert later["lines"][1] == MASK * len(MARK) and later["fingerprint"] != got["fingerprint"]
        assert renders == [key]


# ── size ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_geometry_over_the_cell_cap_is_refused_before_rendering_whatever_it_contains(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    rendered: list[tuple] = []
    real = vtscreen.render_grid

    def spy(data, rows, cols):
        rendered.append((rows, cols))
        return real(data, rows, cols)

    monkeypatch.setattr(vtscreen, "render_grid", spy)
    async with paired(tmp_path, monkeypatch) as p:
        # 250 × 450 = 112 500 cells, holding one short line: tiny on any wire, refused all the same.
        made, _key = await terminal_grant(p, b"tiny", rows=250, cols=450)
        fits, _key = await terminal_grant(p, b"tiny", rows=200, cols=500)  # exactly the cap
        wire.clear()
        assert await p.refused("terminal.frame", ref(made)) == "unavailable"
        assert rendered == []  # the renderer was never called
        assert answers(wire) == [UNAVAILABLE]
        assert (await p.call("terminal.frame", ref(fits)))["lines"][0] == "tiny"
        assert rendered == [(200, 500)]


#: A non-BMP character a terminal draws in ONE cell (twelve bytes on the wire). An emoji is two
#: cells wide, and a frame holding one is refused before it is measured (#2056 review).
NON_BMP = "\U0001d400"


@pytest.mark.anyio
async def test_a_grid_under_the_cell_cap_filled_with_non_bmp_characters_is_refused_by_the_budget(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """The cell cap is not a promise about the wire: 50 000 cells of a non-BMP character are
    600 KB as the channel writes them. That refusal is the serialized budget's, measured with the
    production serializer on the answer itself."""
    measured: list[tuple[dict, int]] = []
    real = sharing.wire_size

    def spy(result):
        size = real(result)
        measured.append((result, size))
        return size

    monkeypatch.setattr(sharing, "wire_size", spy)
    rows, cols = 100, 500
    assert rows * cols < scrollback.SCREEN_EXPORT_MAX_CELLS
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(
            p, (NON_BMP * (rows * cols)).encode(), rows=rows, cols=cols
        )
        assert scrollback.screen_export(key).grid[-1] == NON_BMP * cols  # it renders: the cap held
        wire.clear()
        assert await p.refused("terminal.frame", ref(made)) == "unavailable"
        assert answers(wire) == [UNAVAILABLE]
        ((answer, size),) = [m for m in measured if "lines" in m[0]]
        assert answer["lines"] == [NON_BMP * cols] * rows
        # ...and that number is the production serializer's, envelope included.
        assert size == len(ch.encode_answer({"ok": True, "result": answer})) > 2 * sharing.BUDGET
        assert NON_BMP.encode() not in b"".join(wire) and b"\\ud835" not in b"".join(wire)


@pytest.mark.anyio
async def test_transport_size_is_measured_on_the_masked_frame_and_a_fitting_one_is_sent(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """A screen whose unmasked frame is over the budget and whose masked frame fits: it is sent,
    masked. (A long secret of non-BMP characters: 12 bytes a cell unmasked, 6 masked.)"""
    rows, cols = 40, 500
    secret = "".join(chr(0x1D400 + (i % 40)) for i in range(rows * cols))
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [secret])
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, secret.encode(), rows=rows, cols=cols)
        plain = list(scrollback.screen_export(key).grid)
        unmasked = {"fingerprint": "0" * 32, "cols": cols, "rows": rows, "lines": plain}
        assert sharing.wire_size({**unmasked, "settled": True}) > sharing.BUDGET
        wire.clear()
        got = await p.call("terminal.frame", ref(made))
        assert got["lines"] == [MASK * cols] * rows
        body = sent(wire, got)
        assert len(body) <= sharing.BUDGET and b"\\ud835" not in body


# ── settling ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_busy_screen_answers_settling_with_no_frame_until_this_call_waited_out_the_bound(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    renders,
):
    assert (terminal.SETTLE_S, terminal.BUSY_BOUND_S) == (0.5, 3.0)  # the contract's numbers
    monkeypatch.setattr(terminal, "SETTLE_S", 0.6)
    monkeypatch.setattr(terminal, "BUSY_BOUND_S", 1.2)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        busy(key)  # a spinner: visible output never stops
        wire.clear()

        # `wait = 0`: no frame, and it is neither an error nor `unavailable`.
        assert await p.call("terminal.frame", ref(made)) == {"settling": True}
        assert await p.call("terminal.frame", ref(made, wait=0)) == {"settling": True}
        # A wait shorter than the settle interval does not shorten settling.
        started = time.monotonic()
        assert await p.call("terminal.frame", ref(made, wait=0.3)) == {"settling": True}
        assert 0.25 <= time.monotonic() - started < 1.2
        # A wait that ends before THIS call's own busy bound: still no frame.
        assert await p.call("terminal.frame", ref(made, wait=0.8)) == {"settling": True}
        assert answers(wire) == [SETTLING] * 4
        assert frames_sent(wire) == [] and MARK.encode() not in b"".join(wire)
        assert renders == []  # nothing was even rendered for them

        # A call that itself waits out the bound gets the frame, marked as not settled.
        started = time.monotonic()
        got = await p.call("terminal.frame", ref(made, wait=20), timeout=30)
        elapsed = time.monotonic() - started
        assert terminal.BUSY_BOUND_S - 0.05 <= elapsed < 10
        assert got["settled"] is False and got["lines"][1] == MARK
        # The earlier calls' waiting does not count for a later one: each call has its own bound.
        assert await p.call("terminal.frame", ref(made, wait=0.3)) == {"settling": True}

        # The screen goes quiet: the next call answers at once, settled.
        quiet(key)
        assert (await p.call("terminal.frame", ref(made)))["settled"] is True


@pytest.mark.anyio
async def test_a_call_parked_on_a_quiet_screen_does_not_export_mid_write_when_output_starts(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    """The busy bound counts the time THIS call watched the screen stay busy, not the time since
    it arrived: a call parked for longer than the bound on a quiet screen must not hand out an
    unsettled frame the moment output begins."""
    monkeypatch.setattr(terminal, "SETTLE_S", 0.6)
    monkeypatch.setattr(terminal, "BUSY_BOUND_S", 1.2)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        have = (await p.call("terminal.frame", ref(made)))["fingerprint"]
        parked = asyncio.create_task(
            p.call("terminal.frame", ref(made, have=have, wait=20), timeout=30)
        )
        await until(lambda: p.a.frame_waits.get(p.b.peer_id) == 1)
        await asyncio.sleep(terminal.BUSY_BOUND_S + 0.3)  # parked for longer than the bound
        assert not parked.done()
        began = time.monotonic()
        write(key, b"first half", settled=False)
        busy(key)  # ...and the screen stays busy from here on
        got = await parked
        assert time.monotonic() - began >= terminal.BUSY_BOUND_S - 0.05
        assert got["settled"] is False and "first half" in got["lines"][2]


# ── `have` ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_have_waits_for_a_different_frame_and_otherwise_answers_unchanged(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    renders,
):
    monkeypatch.setattr(terminal, "SETTLE_S", 0.3)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        first = await p.call("terminal.frame", ref(made))
        have = first["fingerprint"]

        # Nothing changed: without a wait at once, with a wait after it.
        wire.clear()
        assert await p.call("terminal.frame", ref(made, have=have)) == {
            "fingerprint": have,
            "unchanged": True,
        }
        started = time.monotonic()
        held = await p.call("terminal.frame", ref(made, have=have, wait=0.5))
        assert held == {"fingerprint": have, "unchanged": True}
        assert time.monotonic() - started >= 0.45  # it waited for a different frame
        assert frames_sent(wire) == [] and MARK.encode() not in b"".join(wire)
        assert renders == [key]  # an unchanged screen is not rendered again

        # A `have` that is not the current frame is answered with the frame at once.
        assert await p.call("terminal.frame", ref(made, have="0" * 32)) == first

        # The screen changes while a call is parked: it answers the new frame, once settled.
        parked = asyncio.create_task(
            p.call("terminal.frame", ref(made, have=have, wait=20), timeout=30)
        )
        await until(lambda: p.a.frame_waits.get(p.b.peer_id) == 1)
        started = time.monotonic()
        write(key, b"new output\r\n$ ", settled=False)
        got = await parked
        assert time.monotonic() - started < 10  # the change woke it, not the end of its 20 s
        assert got["settled"] is True and got["fingerprint"] != have
        assert got["lines"][2] == "$ new output"
        assert p.a.frame_waits == {}
        assert renders == [key, key]  # one render per change


@pytest.mark.anyio
async def test_the_fingerprint_differs_between_two_grants_for_the_same_screen(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, f"token {SECRET}".encode())
        first = await p.call("terminal.frame", ref(made))
        assert await p.call("terminal.frame", ref(made)) == first  # stable for one grant
        # The same screen through another grant (the first one ended; a peer holds one per
        # session): the same lines, another fingerprint.
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        other = await p.grant(key)
        second = await p.call("terminal.frame", ref(other))
        assert second["lines"] == first["lines"] and second["fingerprint"] != first["fingerprint"]
        # A fingerprint of another grant is not "the frame you have".
        assert "lines" in await p.call(
            "terminal.frame", ref(other, have=first["fingerprint"], wait=0)
        )
        # It is the keyed digest of the REDACTED frame, bound to the grant.
        assert second["fingerprint"] == frames.fingerprint(other.grant_id, 24, 80, second["lines"])
        assert SECRET not in "\n".join(second["lines"])


@pytest.mark.anyio
async def test_several_watchers_cost_one_render_per_change_and_each_call_is_redacted_for_itself(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
    renders,
):
    """The render is shared and serialized per session. Redaction, the fingerprint and the
    authorization are not: every call does its own."""
    redactions: list[int] = []
    slow = threading.Event()
    real_redact = frames.redact

    def redact(grid, cols):
        redactions.append(1)
        return real_redact(grid, cols)

    monkeypatch.setattr(frames, "redact", redact)
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    counted = scrollback.screen_export

    def held(key):
        slow.wait(5)  # every watcher arrives while the first render is still running
        return counted(key)

    monkeypatch.setattr(scrollback, "screen_export", held)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p, f"token {SECRET}".encode())
        calls = [asyncio.create_task(p.call("terminal.frame", ref(made))) for _ in range(4)]
        await asyncio.sleep(0.3)
        slow.set()
        answers = await asyncio.gather(*calls)
        assert renders == [key]  # four watchers, one render
        assert len(redactions) == 4  # ...and four redactions
        assert all(a == answers[0] for a in answers) and MASK in answers[0]["lines"][0]

        write(key, b"\r\nmore")
        calls = [asyncio.create_task(p.call("terminal.frame", ref(made))) for _ in range(4)]
        assert all("more" in a["lines"][1] for a in await asyncio.gather(*calls))
        assert renders == [key, key] and len(redactions) == 8

        # A refusal is kept the same way: an unreadable screen is not rendered once per poll.
        write(key, b"\x08")
        for _ in range(3):
            assert await p.refused("terminal.frame", ref(made)) == "unavailable"
        assert renders == [key, key, key]


# ── the grant kind ────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_an_api_grant_cannot_read_a_frame_and_a_terminal_grant_cannot_read_a_session(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    fake,  # noqa: F811
    live,  # noqa: F811
):
    fake.turns = [op_turn("an API turn", "PROJECTED-REPLY")]
    fake.emit()
    async with paired(tmp_path, monkeypatch) as p:
        api = await p.grant()
        paint(api.session_key)  # even with a screen under the API session's key
        term, _key = await terminal_grant(p)
        assert (api.kind, term.kind) == ("api", "terminal")
        # each is served by its own verbs...
        assert (await p.call("session.snapshot", ref(api)))["turns"]
        assert (await p.call("terminal.frame", ref(term)))["lines"]

        # ...and refused by the other kind's, as `no_grant`, byte for byte.
        wire.clear()
        assert await p.refused("terminal.frame", ref(api)) == "no_grant"
        assert await p.refused("terminal.frame", ref(api, wait=1)) == "no_grant"
        assert await p.refused("session.snapshot", ref(term)) == "no_grant"
        assert await p.refused("session.events", ref(term, after=0)) == "no_grant"
        assert await p.refused("session.events", ref(term, after=0, wait=1)) == "no_grant"
        turn = fake.turns[0]["turn_id"]
        assert await p.refused("session.history", ref(term, before=turn)) == "no_grant"
        assert answers(wire) == [NO_GRANT] * 6
        everything = b"".join(wire)
        assert MARK.encode() not in everything and b"PROJECTED-REPLY" not in everything
        assert p.a.frame_waits == {} and p.a.waits == {}

        # The STORED kind is what the operator agreed to. A row whose kind says one thing while
        # its session is the other serves neither verb.
        con = sqlite3.connect(p.a.store.db_path)
        con.execute("UPDATE grants SET kind = 'terminal' WHERE grant_id = ?", (api.grant_id,))
        con.execute("UPDATE grants SET kind = 'api' WHERE grant_id = ?", (term.grant_id,))
        con.commit()
        con.close()
        wire.clear()
        for made in (api, term):
            assert await p.refused("terminal.frame", ref(made)) == "no_grant"
            assert await p.refused("session.snapshot", ref(made)) == "no_grant"
        assert answers(wire) == [NO_GRANT] * 4


def test_a_terminal_session_is_granted_view_only_now_and_later(tmp_path, live, monkeypatch):  # noqa: F811
    st = store.Store(tmp_path / "fed")
    peer = trusted(st)
    key = term_key()

    def create(session_key=key, level="view"):
        return grants.create(
            st, session_key=session_key, peer_id=peer.peer_id, level=level, expires_in=None
        )

    made = create()
    assert (made.kind, made.level) == ("terminal", "view")
    assert grants.public(made)["kind"] == "terminal"
    assert st.get_grant(made.grant_id).kind == "terminal"
    assert [g.grant_id for g in grants.listing(st, key)] == [made.grant_id]
    api = create(api_key())
    assert api.kind == "api"

    for level in ("send", "suggest", "write", "", None, 7):
        with pytest.raises(grants.GrantError) as exc:
            create(term_key(), level)
        assert exc.value.code == "invalid"
    # "and later": when this build grants more levels for an API session, a terminal stays view.
    monkeypatch.setattr(grants, "LEVELS", ("view", "suggest", "send"))
    monkeypatch.setitem(grants.DEFAULT_EXPIRY_S, "send", 86400)
    monkeypatch.setitem(grants.DEFAULT_EXPIRY_S, "suggest", 86400)
    assert create(api_key(), "send").level == "send"
    for level in ("send", "suggest"):
        with pytest.raises(grants.GrantError) as exc:
            create(term_key(), level)
        assert exc.value.code == "invalid" and "view only" in exc.value.detail
    # ...and a terminal row that somehow carries another level is not served, nor listed.
    con = sqlite3.connect(st.db_path)
    con.execute("UPDATE grants SET level = 'send' WHERE grant_id = ?", (made.grant_id,))
    con.commit()
    con.close()
    stored = st.get_grant(made.grant_id)
    assert grants.authorize(st, made.grant_id, peer.peer_id, 1, kind="terminal") is None
    assert grants.serves(stored) is False
    with pytest.raises(grants.GrantError):
        grants.change(st, made.grant_id, expires_in=3600, revision=1)

    # A chat endpoint has no terminal and no API session: it cannot be shared at all.
    with pytest.raises(grants.GrantError) as exc:
        create(f"apichat:{uuid.uuid4()}")
    assert exc.value.code in ("not_shareable", "not_found")


def test_authorize_requires_the_kind_and_refuses_the_other_one(tmp_path, live):  # noqa: F811
    st = store.Store(tmp_path / "fed")
    peer = trusted(st)
    term = grants.create(
        st, session_key=term_key(), peer_id=peer.peer_id, level="view", expires_in=None
    )
    api = grants.create(
        st, session_key=api_key(), peer_id=peer.peer_id, level="view", expires_in=None
    )
    assert inspect.signature(grants.authorize).parameters["kind"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        grants.authorize(st, term.grant_id, peer.peer_id, 1)  # a verb must say what it reads
    assert grants.authorize(st, term.grant_id, peer.peer_id, 1, kind="terminal") == term
    assert grants.authorize(st, api.grant_id, peer.peer_id, 1, kind="api") == api
    assert grants.authorize(st, term.grant_id, peer.peer_id, 1, kind="api") is None
    assert grants.authorize(st, api.grant_id, peer.peer_id, 1, kind="terminal") is None
    for kind in ("", "pty", None, "TERMINAL"):
        assert grants.authorize(st, term.grant_id, peer.peer_id, 1, kind=kind) is None


def test_schema_2_migrates_in_place_with_a_backup_and_its_grants_are_api_grants(tmp_path):
    """A store exactly as #2054 left it: one live grant, one mirrored row, no `kind` column."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from agent_sessions.federation import noise

    directory = tmp_path / "fed"
    directory.mkdir(parents=True)
    path = directory / store.DB_FILE
    con = sqlite3.connect(path)
    for stmt in (*store._SCHEMA_V1, *store._SCHEMA_V2):
        con.execute(stmt)
    key = noise.public_bytes(X25519PrivateKey.generate())
    con.execute(
        "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision, created_at,"
        " updated_at) VALUES (?, ?, 'old box', 'trusted', '[]', 1, 1.0, 1.0)",
        ("a" * 32, key),
    )
    con.execute(
        "INSERT INTO grants (grant_id, session_key, principal, level, expires_at, revision,"
        " state, created_at) VALUES (?, 'codex-api:x', ?, 'view', 4e9, 1, 'live', 1.0)",
        ("g" * 22, "peer:" + "a" * 32),
    )
    con.execute(
        "INSERT INTO remote_shares (peer_id, grant_id, engine, native_id, title, level,"
        " expires_at, revision, seen_at) VALUES (?, ?, 'codex-api', 'n1', 't', 'view', 4e9, 1, 1)",
        ("a" * 32, "r" * 22),
    )
    con.execute("PRAGMA user_version=2")
    con.commit()
    con.close()

    st = store.Store(directory)
    assert st.get_grant("g" * 22).kind == "api"  # the first use migrates
    assert st.get_remote("a" * 32, "r" * 22).kind == "api"
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 3
    # The database is the arbiter: a kind this build does not know is never stored.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE grants SET kind = 'pty'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE remote_shares SET kind = 'byte-stream'")
    con.close()

    (backup,) = directory.glob(f"{store.DB_FILE}.bak-v2-*")
    old = sqlite3.connect(backup)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 2  # taken BEFORE the migration
    assert "kind" not in {r[1] for r in old.execute("PRAGMA table_info(grants)")}
    assert old.execute("SELECT grant_id FROM grants").fetchone()[0] == "g" * 22
    old.close()
    store.Store(directory).list_peers()  # a second open migrates and copies nothing
    assert len(list(directory.glob("*.bak-*"))) == 1
    with pytest.raises(store.Invalid):
        st.create_grant(
            session_key="claude:x", peer_id="a" * 32, level="view", expires_at=4e9, kind="pty"
        )


# ── every way a grant ends, against `terminal.frame` ──────────────────


async def _frame_refused(p: Pair, bodies: list[bytes], made: store.Grant, **over) -> None:
    bodies.clear()
    args = {**ref(made), **over}
    assert await p.refused("terminal.frame", args) == "no_grant"
    assert await p.refused("terminal.frame", {**args, "wait": 1}) == "no_grant"
    assert answers(bodies) == [NO_GRANT, NO_GRANT]
    assert MARK.encode() not in b"".join(bodies)
    assert p.a.frame_waits == {}


@pytest.mark.anyio
async def test_every_refusal_of_terminal_frame_is_no_grant_and_sends_nothing(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        # A live grant is served, so the cases below are refusals and not a broken harness.
        served, _key = await terminal_grant(p)
        assert (await p.call("terminal.frame", ref(served)))["lines"][1] == MARK

        # An unknown id, and another peer's grant.
        await _frame_refused(p, wire, served, grant_id="z" * 22)
        stranger = await asyncio.to_thread(trusted, p.a.store, "stranger")
        theirs = await asyncio.to_thread(
            grants.create,
            p.a.store,
            session_key=paint(term_key()),
            peer_id=stranger.peer_id,
            level="view",
            expires_in=None,
        )
        await _frame_refused(p, wire, theirs)

        # A stale revision (and one from the future).
        changed = await asyncio.to_thread(
            grants.change, p.a.store, served.grant_id, expires_in=3600, revision=1
        )
        await _frame_refused(p, wire, served)  # still carries revision 1
        await _frame_refused(p, wire, served, revision=3)
        assert (await p.call("terminal.frame", ref(changed)))["lines"]

        # An expired grant, on the owner's clock (its sweep may or may not have run yet).
        short, short_key = await terminal_grant(p)
        short = await asyncio.to_thread(
            grants.change, p.a.store, short.grant_id, expires_in=60, revision=1
        )
        assert (await p.call("terminal.frame", ref(short)))["lines"]
        with monkeypatch.context() as m:
            m.setattr(grants, "_clock", lambda: time.time() + 61)
            await _frame_refused(p, wire, short)
        assert (await p.call("terminal.frame", ref(changed)))["lines"]

        # An archived session: the grant is still live in the store, the session is not.
        live[changed.session_key] = "archived"
        await _frame_refused(p, wire, changed)
        live.clear()
        # ...and an archive through the wrapper every archive path uses.
        monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(p.a.dir))
        async with shares.archiving(changed.session_key):
            pass
        await _frame_refused(p, wire, changed)
        assert p.a.store.get_grant(changed.grant_id).end_reason == "archived"

        # A revoked grant.
        revoked, _key = await terminal_grant(p)
        await asyncio.to_thread(grants.revoke, p.a.store, revoked.grant_id)
        await _frame_refused(p, wire, revoked)

        # A revoked peer: written by ANOTHER process, while the channel is still open.
        last, _key = await terminal_grant(p)
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        await asyncio.to_thread(store.Store(p.a.dir).revoke, p.b.peer_id)
        channel = p.a.channel_for(p.b.peer_id)
        assert channel is not None and not channel.closed
        await _frame_refused(p, wire, last)
        assert not channel.closed  # refused by the check, not by the socket


@pytest.mark.anyio
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("woken", [False, True], ids=["ends_the_wait", "woken_by_a_new_frame"])
async def test_a_parked_frame_wait_ends_as_no_grant_and_sends_no_frame(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    action,
    woken,
):
    # `woken`: the wait is never re-checked while parked, the screen changes AFTER the grant
    # ended, and only the check directly before the answer stands between that new frame and the
    # peer. Otherwise: the parked wait itself notices, well before its 20 s are over.
    monkeypatch.setattr(sharing, "WAIT_SLICE_S", 60.0 if woken else 0.05)
    monkeypatch.setattr(terminal, "SETTLE_S", 0.2)
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        have = (await p.call("terminal.frame", ref(made)))["fingerprint"]
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        channel = p.a.channel_for(p.b.peer_id)
        wire.clear()
        started = time.monotonic()
        parked = asyncio.create_task(
            p.call("terminal.frame", ref(made, have=have, wait=20), timeout=30)
        )
        await until(lambda: p.a.frame_waits.get(p.b.peer_id) == 1)
        assert not parked.done()

        await _end_grant(action, p, made, monkeypatch)
        if woken:
            await asyncio.sleep(0.1)
            assert not parked.done()  # still parked: nothing re-checked it
            write(key, b"WRITTEN-AFTER-THE-GRANT-ENDED\r\n")  # the new frame that wakes it
        with pytest.raises(ch.CallError) as exc:
            async with asyncio.timeout(10):
                await parked
        assert exc.value.code == "no_grant"
        assert time.monotonic() - started < 10  # ended by the check, not by its 20 s
        assert answers(wire) == [NO_GRANT]
        assert b"WRITTEN-AFTER" not in b"".join(wire) and frames_sent(wire) == []
        assert p.a.frame_waits == {}  # the slot is free again

        # The channel is still open, and the next call on it is refused too.
        assert p.a.channel_for(p.b.peer_id) is channel and not channel.closed
        await _frame_refused(p, wire, made)


@pytest.mark.anyio
async def test_a_frame_built_before_a_revoke_is_not_sent(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    """The second check sits after the frame was rendered, redacted and measured."""
    async with paired(tmp_path, monkeypatch) as p:
        made, _key = await terminal_grant(p)
        built = asyncio.Event()
        release = asyncio.Event()
        real = frames.fingerprint
        loop = asyncio.get_running_loop()

        def slow(*args):
            out = real(*args)
            loop.call_soon_threadsafe(built.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
            return out

        monkeypatch.setattr(frames, "fingerprint", slow)
        wire.clear()
        call = asyncio.create_task(p.call("terminal.frame", ref(made)))
        await built.wait()  # the redacted frame exists now
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        release.set()
        with pytest.raises(ch.CallError) as exc:
            await call
        assert exc.value.code == "no_grant"
        assert answers(wire) == [NO_GRANT]
        assert MARK.encode() not in b"".join(wire)


def test_the_second_check_is_the_last_await_before_the_frame_is_returned():
    """Pinned in the source, like the session verbs (`test_federation_sharing`): the last `await`
    of the handler is the per-call check, the statement after it is the `return` of the answer,
    and that check asks for a TERMINAL grant."""
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(terminal.terminal_frame)))
    body = tree.body[0].body
    last, check = body[-1], body[-2]
    assert isinstance(last, ast.Return) and isinstance(last.value, ast.Name)
    assert isinstance(check, ast.Expr) and isinstance(check.value, ast.Await)
    assert check.value.value.func.id == "_authorize"
    first = next(n for n in ast.walk(tree) if isinstance(n, ast.Await))
    assert first.value.func.id == "_authorize"
    # `_authorize` here is this module's own: one await, of the shared check, for kind terminal.
    source = textwrap.dedent(inspect.getsource(terminal._authorize))
    (awaited,) = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Await)]
    assert ast.unparse(awaited.value.func) == "sharing._authorize"
    assert [ast.unparse(k.value) for k in awaited.value.keywords if k.arg == "kind"] == [
        "grants.KIND_TERMINAL"
    ]
    assert sharing._authorize.__kwdefaults__ == {"kind": "api"}


@pytest.mark.anyio
async def test_at_most_four_frame_waits_per_peer_are_parked_and_the_fifth_is_busy(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    assert terminal.FRAME_WAITS_PER_PEER == 4
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        have = (await p.call("terminal.frame", ref(made)))["fingerprint"]
        held = ref(made, have=have, wait=20)
        parked = [asyncio.create_task(p.call("terminal.frame", held, timeout=30)) for _ in range(4)]
        await until(lambda: p.a.frame_waits.get(p.b.peer_id) == 4)
        assert await p.refused("terminal.frame", held, timeout=5) == "busy"
        # a read that does not wait is not a parked wait, and the events waits are counted apart
        assert await p.call("terminal.frame", ref(made, have=have)) == {
            "fingerprint": have,
            "unchanged": True,
        }
        assert p.a.waits == {}
        write(key, b"wake")
        answers = await asyncio.gather(*parked)
        assert all("$ wake" in a["lines"][2] for a in answers)
        assert p.a.frame_waits == {}
        # a wait whose channel goes away frees its slot too
        have = answers[0]["fingerprint"]
        gone = asyncio.create_task(
            p.call("terminal.frame", ref(made, have=have, wait=20), timeout=30)
        )
        await until(lambda: p.a.frame_waits.get(p.b.peer_id) == 1)
        p.a.channel_for(p.b.peer_id).close("test")
        with pytest.raises(ch.CallError):
            await gone
        await until(lambda: p.a.frame_waits == {})


@pytest.mark.anyio
@pytest.mark.parametrize("part", [0, 1])
async def test_malformed_frame_arguments_reset_the_stream_and_reach_no_handler(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
    part,
):
    async with paired(tmp_path, monkeypatch) as p:
        made, key = await terminal_grant(p)
        good = ref(made)
        bad_calls = [
            {"grant_id": made.grant_id},
            {**good, "revision": True},
            {**good, "have": "not-a-fingerprint"},
            {**good, "have": "A" * 32},
            {**good, "have": 7},
            {**good, "have": ""},
            {**good, "wait": 20.5},
            {**good, "wait": True},
            {**good, "wait": "1"},
            {**good, "wait": float("inf")},
            {**good, "session_key": key},  # never a key
            {**good, "cols": 200, "rows": 50},  # never a size
            {**good, "input": "ls\n"},  # never input
            {**good, "after": 0},  # never a cursor
        ][part::2]  # the channel closes at its eighth violation: seven per channel
        wire.clear()
        for args in bad_calls:
            assert await p.refused("terminal.frame", args) == "refused", args
        assert data_bodies(wire) == []  # the stream was reset: no handler ran, no answer
        assert p.a.channel_for(p.b.peer_id).stats.refused == len(bad_calls) == 7
        # the largest wait is accepted (and a quiet screen answers it at once)
        assert (await p.call("terminal.frame", {**good, "wait": 20}, timeout=30))["lines"]


# ── the owner's terminal is never touched ─────────────────────────────


@pytest.mark.anyio
async def test_frame_reads_send_no_input_no_resize_and_attach_nothing(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    """The session has a real PTY registered as its writer, as an attached or headless session
    does. During any number of frame reads nothing arrives on it, its size does not change, and
    every seam that could attach a client, signal, write or resize raises if it is called."""
    import fcntl

    monkeypatch.setattr(terminal, "SETTLE_S", 0.2)
    monkeypatch.setattr(terminal, "BUSY_BOUND_S", 0.5)
    async with paired(tmp_path, monkeypatch) as p:
        raw = (FIX / "opencode_permission_shell.raw").read_bytes()
        made, key = await terminal_grant(p, raw, rows=OC[0], cols=OC[1])
        master, slave = os.openpty()
        tty.setraw(slave)
        termios.tcsetwinsize(slave, OC)
        os.set_blocking(slave, False)
        token = session_input.register_writer(key, master, threading.Lock(), "headless")
        try:
            ring = (bytes(scrollback._BUFFERS[key]), scrollback._TOTALS[key])
            geometry = (scrollback._LAST_ROWS[key], scrollback._LAST_COLS[key])

            def touched(*_a, **_k):
                raise AssertionError("a frame read touched the session")

            real_write = os.write

            def guarded_write(fd, data):
                if fd in (master, slave):
                    touched()
                return real_write(fd, data)

            monkeypatch.setattr(os, "write", guarded_write)
            for module, name in (
                (os, "kill"),
                (os, "killpg"),
                (os, "openpty"),
                (fcntl, "ioctl"),
                (termios, "tcsetwinsize"),
                (subprocess, "Popen"),
                (asyncio, "create_subprocess_exec"),
                (webterm, "_set_winsize"),
                (ptybridge, "attach_argv"),
                (ptybridge, "launch_argv"),
                (session_input, "send_input"),
                (session_stream.SessionStream, "start"),
                (scrollback, "_reset_ring"),
                (scrollback, "note_cols"),
                (scrollback, "note_attach"),
                (scrollback, "note_attach_width"),
                (scrollback, "note_viewer_attached"),
                (scrollback, "note_geometry"),
                (scrollback, "note_reader_geometry"),
            ):
                monkeypatch.setattr(module, name, touched)

            first = await p.call("terminal.frame", ref(made))
            have = first["fingerprint"]
            for _ in range(5):
                assert await p.call("terminal.frame", ref(made)) == first
            assert (await p.call("terminal.frame", ref(made, have=have, wait=0.4)))["unchanged"]
            busy(key)
            assert await p.call("terminal.frame", ref(made, wait=0.2)) == {"settling": True}
            assert (await p.call("terminal.frame", ref(made, wait=5)))["settled"] is False

            with pytest.raises(BlockingIOError):
                os.read(slave, 1024)  # not one byte of input reached the terminal
            assert termios.tcgetwinsize(slave) == OC  # and it was not resized
            assert (bytes(scrollback._BUFFERS[key]), scrollback._TOTALS[key]) == ring
            assert (scrollback._LAST_ROWS[key], scrollback._LAST_COLS[key]) == geometry
        finally:
            session_input.unregister_writer(key, token)
            os.close(master)
            os.close(slave)


def test_the_frame_modules_import_nothing_that_drives_a_terminal():
    """`terminal.py` and `frames.py` read a screen. They import none of the modules that attach
    to, write to, resize or launch a session."""
    forbidden = {
        "webterm",
        "ptybridge",
        "session_input",
        "session_stream",
        "tty_health",
        "headless_seed",
        "headless_launch",
        "actuator",
        "runtime_cleanup",
        "subprocess",
        "os",
        "pty",
        "termios",
        "fcntl",
        "signal",
    }
    for module in (terminal, frames):
        tree = ast.parse(inspect.getsource(module))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[-1] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {alias.name for alias in node.names} | {(node.module or "").split(".")[-1]}
        assert not names & forbidden, (module.__name__, names & forbidden)


# ── the list and the receiving side ───────────────────────────────────


@pytest.mark.anyio
async def test_the_list_and_the_mirror_carry_the_kind_and_an_unknown_kind_is_skipped(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        term, key = await terminal_grant(p)
        api = await p.grant()
        _rows_for(monkeypatch, {key: "a terminal", api.session_key: "an API session"})
        page = await p.call("share.list")
        kinds = {g["grant_id"]: (g["kind"], g["level"], g["title"]) for g in page["grants"]}
        assert kinds == {
            term.grant_id: ("terminal", "view", "a terminal"),
            api.grant_id: ("api", "view", "an API session"),
        }
        assert all(set(g) == remote.ENTRY_KEYS | {"kind"} for g in page["grants"])

        assert await remote.refresh(p.b, p.a.peer_id) is True
        rows = {r.grant_id: r.kind for r in p.b.store.list_remote(p.a.peer_id)}
        assert rows == {term.grant_id: "terminal", api.grant_id: "api"}
        shown = {r["grant_id"]: r["kind"] for r in remote.listing(p.b)}
        assert shown == rows

        # The receiver asks the owner only for what the mirrored kind can answer.
        wire.clear()
        for grant_id, verb, check, kind in (
            (term.grant_id, "session.snapshot", remote.check_snapshot, "api"),
            (api.grant_id, "terminal.frame", remote.check_frame, "terminal"),
        ):
            with pytest.raises(remote.ReadError) as exc:
                await remote.read(p.b, p.a.peer_id, grant_id, verb, {}, check=check, kind=kind)
            assert exc.value.code == "no_grant"
        assert data_bodies(wire) == []  # no call was made
        got = await remote.read(
            p.b,
            p.a.peer_id,
            term.grant_id,
            "terminal.frame",
            {"wait": 0.0},
            check=remote.check_frame,
            kind="terminal",
        )
        assert got["lines"][1] == MARK

        # Two instances are updated independently. An owner one version ahead lists a kind this
        # build does not know: that entry is skipped (not listed, never stored as another kind)
        # and the rest of the list is mirrored.
        real = sharing._list_entry
        as_kind = {"value": "byte_stream"}

        def newer(grant, *args):
            out = real(grant, *args)
            if out and grant.grant_id == term.grant_id:
                return {**out, "kind": as_kind["value"]}
            return out

        monkeypatch.setattr(sharing, "_list_entry", newer)
        assert await remote.refresh(p.b, p.a.peer_id) is True
        assert {r.grant_id: r.kind for r in p.b.store.list_remote(p.a.peer_id)} == {
            api.grant_id: "api"
        }
        # A kind that is not even the shape of one is a malformed known field: the whole refresh
        # fails and the mirror stays as it was.
        as_kind["value"] = "Byte-Stream!"
        assert await remote.refresh(p.b, p.a.peer_id) is False
        assert {r.grant_id: r.kind for r in p.b.store.list_remote(p.a.peer_id)} == {
            api.grant_id: "api"
        }
        monkeypatch.setattr(sharing, "_list_entry", real)
        assert await remote.refresh(p.b, p.a.peer_id) is True
        assert {r.grant_id: r.kind for r in p.b.store.list_remote(p.a.peer_id)} == rows


@pytest.mark.anyio
async def test_an_owner_on_the_previous_build_lists_no_kind_and_its_grants_are_api_grants(
    tmp_path,
    monkeypatch,
    fake,  # noqa: F811
    live,  # noqa: F811
):
    fake.turns = [op_turn("from the older owner", "ok")]
    async with paired(tmp_path, monkeypatch) as p:
        api = await p.grant()
        _rows_for(monkeypatch, {api.session_key: "an API session"})
        real = sharing._list_entry

        def older(*args):  # the build before #2056: an entry has no `kind`
            out = real(*args)
            return out and {k: v for k, v in out.items() if k != "kind"}

        monkeypatch.setattr(sharing, "_list_entry", older)
        assert "kind" not in (await p.call("share.list"))["grants"][0]
        assert await remote.refresh(p.b, p.a.peer_id) is True
        (row,) = p.b.store.list_remote(p.a.peer_id)
        assert (row.grant_id, row.kind) == (api.grant_id, "api")
        got = await remote.read(
            p.b, p.a.peer_id, api.grant_id, "session.snapshot", {}, check=remote.check_snapshot
        )
        assert [t["text"] for t in got["turns"]] == ["from the older owner"]


@pytest.mark.anyio
async def test_a_receiver_unaware_of_kinds_reads_a_terminal_grant_as_a_session_and_gets_no_grant(
    tmp_path,
    monkeypatch,
    wire,  # noqa: F811
    fake,  # noqa: F811
    live,  # noqa: F811
):
    """A receiver that does not know kinds ignores the key (the forward-compatible list), lists a
    terminal grant like any other and calls `session.snapshot` on it. The OWNER is what keeps the
    kinds apart: the call is refused as `no_grant` and nothing of the screen or of any session is
    sent. That is the safe outcome, and it does not depend on the receiver's build."""
    fake.turns = [op_turn("an API turn", "PROJECTED-REPLY")]
    fake.emit()
    async with paired(tmp_path, monkeypatch) as p:
        term, key = await terminal_grant(p)
        _rows_for(monkeypatch, {key: "a terminal"})
        real = remote.clean_entry

        def unaware(raw):  # the previous build: `kind` is an unknown key, ignored
            if isinstance(raw, dict):
                raw = {k: v for k, v in raw.items() if k != "kind"}
            return real(raw)

        monkeypatch.setattr(remote, "clean_entry", unaware)
        assert (await p.call("share.list"))["grants"][0]["kind"] == "terminal"  # the owner says so
        assert await remote.refresh(p.b, p.a.peer_id) is True
        (row,) = p.b.store.list_remote(p.a.peer_id)
        assert (row.grant_id, row.kind) == (term.grant_id, "api")  # listed as if it were one

        wire.clear()
        turn = fake.turns[0]["turn_id"]
        for verb, extra, check in (
            ("session.snapshot", {}, remote.check_snapshot),
            ("session.events", {"after": 0, "limit": 100, "wait": 0.0}, remote.check_events),
            ("session.history", {"before": turn}, remote.check_history),
        ):
            with pytest.raises(remote.ReadError) as exc:
                await remote.read(p.b, p.a.peer_id, term.grant_id, verb, extra, check=check)
            assert exc.value.code == "no_grant", verb
        assert answers(wire) == [NO_GRANT] * 3  # the calls were made, and each was refused
        everything = b"".join(wire)
        assert MARK.encode() not in everything and b"PROJECTED-REPLY" not in everything
        assert b"an API turn" not in everything


def _frame_answer(**over) -> dict:
    out = {
        "fingerprint": "a" * 32,
        "cols": 10,
        "rows": 3,
        "lines": ["$ ls", "", MASK * 10],
        "settled": True,
    }
    out.update(over)
    return out


def test_a_frame_from_a_peer_is_validated_before_it_is_returned():
    good = _frame_answer()
    assert remote.check_frame(good) is good
    assert remote.check_frame(_frame_answer(lines=[], settled=False))["lines"] == []
    assert remote.check_frame({"settling": True}) == {"settling": True}
    unchanged = {"fingerprint": "a" * 32, "unchanged": True}
    assert remote.check_frame(unchanged, "a" * 32) is unchanged
    assert remote.check_frame(_frame_answer(rows=200, cols=500, lines=["x" * 500] * 200))

    refused = [
        (None, None),
        ([], None),
        ("frame", None),
        ({}, None),
        ({"settling": False}, None),
        ({"settling": True, "lines": ["x"]}, None),
        # `unchanged` only for the fingerprint this instance sent
        (unchanged, None),
        (unchanged, "b" * 32),
        ({"fingerprint": "a" * 32, "unchanged": False}, "a" * 32),
        ({"fingerprint": "a" * 32, "unchanged": True, "lines": ["x"]}, "a" * 32),
        (_frame_answer(fingerprint="A" * 32), None),
        (_frame_answer(fingerprint="a" * 31), None),
        (_frame_answer(fingerprint=None), None),
        ({k: v for k, v in good.items() if k != "settled"}, None),
        ({**good, "cursor": [1, 1]}, None),  # nothing but the listed fields
        ({**good, "raw": "\x1b[2J"}, None),
        (_frame_answer(lines=["a", "b", "c", "d"]), None),  # more lines than rows
        (_frame_answer(lines=["x" * 11]), None),  # a line longer than cols
        (_frame_answer(lines=["a\x1b[31mb"]), None),  # a control character
        (_frame_answer(lines=["a\nb"]), None),
        (_frame_answer(lines=["a\rb"]), None),
        (_frame_answer(lines=["a\x00b"]), None),
        (_frame_answer(lines=["a\x9bb"]), None),  # C1: an 8-bit CSI
        (_frame_answer(lines=["a b"]), None),
        (_frame_answer(lines=["a‮b"]), None),  # a bidirectional override
        (_frame_answer(lines=[7]), None),
        (_frame_answer(lines="$ ls"), None),
        (_frame_answer(lines=None), None),
        (_frame_answer(rows=0), None),
        (_frame_answer(rows=301), None),
        (_frame_answer(rows=True), None),
        (_frame_answer(rows=3.0), None),
        (_frame_answer(cols=0), None),
        (_frame_answer(cols=501), None),
        (_frame_answer(cols="10"), None),
        (_frame_answer(rows=300, cols=500, lines=[]), None),  # a grid no owner renders
        (_frame_answer(settled="yes"), None),
        (_frame_answer(settled=1), None),
    ]
    for doc, have in refused:
        with pytest.raises(remote.Invalid):
            remote.check_frame(doc, have)


@pytest.mark.anyio
async def test_a_frame_read_goes_to_the_owner_and_a_bad_frame_reaches_nobody(tmp_path, monkeypatch):
    r = Receiver(tmp_path)
    row = remote.clean_entry(entry(7, kind="terminal"))
    assert r.fed.store.replace_remote(r.peer.peer_id, [row])
    refreshes: list[str] = []
    monkeypatch.setattr(r.fed, "refresh_remote", refreshes.append)

    async def read(have=None, wait=0.0):
        extra = {"wait": wait, **({"have": have} if have else {})}
        return await remote.read(
            r.fed,
            r.peer.peer_id,
            row["grant_id"],
            "terminal.frame",
            extra,
            check=lambda doc: remote.check_frame(doc, have),
            timeout=remote.events_timeout(wait),
            kind="terminal",
        )

    async def code(**kw) -> str:
        with pytest.raises(remote.ReadError) as exc:
            await read(**kw)
        return exc.value.code

    assert await code() == "peer_offline"  # nothing is answered from the mirror
    good = _frame_answer()
    channel = r.script(good)
    assert await read(wait=20.0) is good
    # The mirrored revision, and the wait plus the margin as the call's own timeout.
    assert channel.calls == [
        ("terminal.frame", {"grant_id": row["grant_id"], "revision": 1, "wait": 20.0}, 30.0)
    ]
    channel = r.script({"fingerprint": "a" * 32, "unchanged": True})
    assert (await read(have="a" * 32))["unchanged"] is True
    assert channel.calls[0][1]["have"] == "a" * 32

    for bad in (
        _frame_answer(lines=["x" * 11]),
        _frame_answer(lines=["\x1b]52;c;c2VjcmV0\x07"]),
        _frame_answer(lines=["a"] * 4),
        {"fingerprint": "b" * 32, "unchanged": True},
        {"settling": True, "lines": ["leak"]},
        ["not", "a", "frame"],
    ):
        r.script(bad)
        assert await code(have="a" * 32) == "bad_answer"
    r.script(ch.CallError("unavailable"))
    assert await code() == "unavailable"
    assert refreshes == []
    r.script(ch.CallError("no_grant"))
    assert await code() == "no_grant"
    assert refreshes == [r.peer.peer_id]  # the mirror may be stale: the list is asked for again


def test_the_frame_route_needs_a_session_and_validates_its_query(auth_cfg, monkeypatch):
    c = _client(auth_cfg)
    fed = c.app.state.federation
    assert c.get(f"/api/federation/remote/{'a' * 32}/{'g' * 22}/frame").status_code == 401
    hdr = _login(c, auth_cfg)
    peer = _trusted_peer(c)
    term, api = remote.clean_entry(entry(1, kind="terminal")), remote.clean_entry(entry(2))
    assert fed.store.replace_remote(peer.peer_id, [term, api])
    listed = {r["grant_id"]: r["kind"] for r in c.get("/api/federation/remote").json()["remote"]}
    assert listed == {term["grant_id"]: "terminal", api["grant_id"]: "api"}

    base = f"/api/federation/remote/{peer.peer_id}"
    frame = f"{base}/{term['grant_id']}/frame"
    r = c.get(frame)  # mirrored, its owner not connected: never an answer from the cache
    assert r.status_code == 503 and r.json()["code"] == "peer_offline"
    assert r.headers["cache-control"] == "no-store"
    assert c.get(f"{frame}?have={'a' * 32}&wait=20").status_code == 503
    for query in ("have=xyz", f"have={'A' * 32}", "have=", "wait=21", "wait=-1", "wait=nan"):
        r = c.get(f"{frame}?{query}")
        assert r.status_code == 422 and r.json()["code"] == "invalid", query
    # The kinds are not crossed here either: 404 without asking anybody.
    assert c.get(f"{base}/{api['grant_id']}/frame").status_code == 404
    assert c.get(f"{base}/{term['grant_id']}/snapshot").status_code == 404
    assert c.get(f"{base}/{term['grant_id']}/events").status_code == 404
    assert c.get(f"{base}/{'z' * 22}/frame").status_code == 404
    assert c.get(f"/api/federation/remote/NOT-A-PEER/{term['grant_id']}/frame").status_code == 404
    assert c.post(frame, headers=hdr).status_code == 405  # a read is not a write


# ── two real instances, a real terminal session ───────────────────────


def _dtach_processes(sock) -> list[str]:
    out = subprocess.run(["pgrep", "-fa", "dtach"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if str(sock) in line]


def _reap_master(sock) -> None:
    """Kill only THIS test's dtach master (matched on its unique socket path) and its child."""
    for line in _dtach_processes(sock):
        pid = int(line.split()[0])
        subprocess.run(["pkill", "-9", "-P", str(pid)], check=False)
        with contextlib.suppress(OSError):
            os.kill(pid, 9)
    sock.unlink(missing_ok=True)


@pytest.mark.skipif(not shutil.which("dtach"), reason="dtach required")
def test_two_real_instances_share_a_real_terminal_session_view_only(
    two_instances,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """Two real apps, paired through their operator APIs, and a real terminal session on the
    owner: a `dtach` master (running `cat` where the shell provider runs `bash`, so the bytes are
    the same on every host) that the production headless reader drains into the real ring.
    `shares.session_state` is the real one: the session exists in the shell provider's own store.

    Grant, read a frame, type on the owner, see the new frame, revoke. Throughout, the owner's
    session gets no input, no resize and no extra `dtach` client."""
    a, b = two_instances
    a_id, b_id = _pair_instances(a, b)
    loop = a.server.servers[0].get_loop()

    def on_a(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(60)

    native = str(uuid.uuid4())
    key = f"shell:{native}"
    prov, _native = engines.parse_key(key)
    prov.on_new_session(native, cwd=str(tmp_path))
    assert shares.session_state(prov, native) == "live"
    sock = ptybridge.socket_path("shell", native)
    sock.parent.mkdir(parents=True, exist_ok=True)
    stream = session_stream.SessionStream("shell", native)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        subprocess.run([ptybridge.DTACH_BIN, "-n", str(sock), "-E", "-z", "/bin/cat"], check=True)
        _wait(lambda: ptybridge.session_exists("shell", native), timeout=60)
        on_a(stream.start())  # the server-owned reader: nobody has this session open
        _wait(lambda: len(_dtach_processes(sock)) == 2, timeout=60)  # the master and that reader

        made = a.post("/grants", {"session_key": key, "peer_id": b_id, "level": "send"})
        assert made.status_code == 422, made.text  # a terminal is view only
        made = a.post("/grants", {"session_key": key, "peer_id": b_id, "level": "view"})
        assert made.status_code == 201, made.text
        grant = made.json()["grant"]
        assert grant["kind"] == "terminal"

        def remote_rows() -> list[dict]:
            return b.http.get("/api/federation/remote", headers=b.read).json()["remote"]

        (row,) = _wait(remote_rows)
        assert row["key"] == f"peer:{a_id}/shell:{native}" and row["kind"] == "terminal"
        base = f"/api/federation/remote/{a_id}/{grant['grant_id']}"
        assert b.http.get(f"{base}/frame").status_code == 401  # beta's own session is required

        def frame(query: str = "") -> dict | None:
            r = b.http.get(f"{base}/frame{query}", headers=b.read, timeout=40)
            return r.json() if r.status_code == 200 and "lines" in r.json() else None

        # Nobody ever opened this session in a browser. The reader recorded the geometry its own
        # PTY has (the default, which dtach relays to the program) when it attached.
        assert termios.tcgetwinsize(stream._master) == (24, 80)
        assert (scrollback._LAST_ROWS[key], scrollback.ring_cols(key)) == (24, 80)
        os.write(stream._master, b"hello from the owner\n")

        # read a frame: beta's operator sees the owner's screen through beta's own API
        typed = ["hello from the owner"] * 2  # the echo of what was typed, and `cat`'s own line

        def shows(lines: list[str], at: int = 0):
            got = frame()
            return got if got and got["lines"][at : at + 2] == lines else None

        first = _wait(lambda: shows(typed), timeout=30)
        assert first["rows"] == 24 and first["cols"] == 80 and first["settled"] is True
        assert set(first["lines"][2:]) == {""}
        # the session verbs of an API session do not serve it
        assert b.http.get(f"{base}/snapshot", headers=b.read).status_code == 404

        # Any number of reads leave the owner's session alone.
        before = (scrollback._TOTALS[key], termios.tcgetwinsize(stream._master))
        for _ in range(5):
            assert frame() == first
        same = b.http.get(f"{base}/frame?have={first['fingerprint']}&wait=1", headers=b.read)
        assert same.json() == {"fingerprint": first["fingerprint"], "unchanged": True}
        # `cat` echoes every byte it is sent, so an unchanged byte count is "no input arrived"
        assert (scrollback._TOTALS[key], termios.tcgetwinsize(stream._master)) == before
        assert len(_dtach_processes(sock)) == 2  # no extra dtach client attached

        # type on the owner: a parked read on beta answers with the new frame
        parked = pool.submit(frame, f"?have={first['fingerprint']}&wait=20")
        _wait(lambda: a.fed.frame_waits.get(b_id) == 1)
        started = time.monotonic()
        os.write(stream._master, b"a second line\n")
        second = parked.result(40)
        assert time.monotonic() - started < 15  # the output woke it, not the end of its 20 s
        assert second["lines"][2] == "a second line"
        assert second["fingerprint"] != first["fingerprint"]
        second = _wait(lambda: shows(["a second line"] * 2, at=2), timeout=30)

        # revoke: a parked read answers 404, and so does everything after it
        held = pool.submit(
            b.http.get,
            f"{base}/frame?have={second['fingerprint']}&wait=20",
            headers=b.read,
            timeout=40,
        )
        _wait(lambda: a.fed.frame_waits.get(b_id) == 1)
        started = time.monotonic()
        assert a.post(f"/grants/{grant['grant_id']}/revoke").status_code == 200
        ended = held.result(40)
        assert ended.status_code == 404 and ended.json()["code"] == "no_grant", ended.text
        assert time.monotonic() - started < 15 and "lines" not in ended.json()
        assert b.http.get(f"{base}/frame", headers=b.read).status_code == 404
        _wait(lambda: remote_rows() == [])
        assert len(_dtach_processes(sock)) == 2  # still the master and its one reader
        assert a.state()["peers"][0]["connected"]  # revoking a grant is not revoking the peer
    finally:
        pool.shutdown(wait=True)
        with contextlib.suppress(Exception):
            on_a(stream.stop())
        _reap_master(sock)
