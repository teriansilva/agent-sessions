"""The readiness gate for a session nobody is watching (#739).

The gate is the risk, not the write. `webterm._deliver_seed` already owns the claim/ack protocol
and the bounded serialized write, and this module reuses it verbatim — what is new is deciding
*when*, with no viewer to observe first paint.

**`DECSET 2004` is not readiness.** At least one engine arms bracketed paste in its preamble and
then discards stdin through a long cold start (measured: 91 bytes over 90 seconds on a cold codex).
Gating on 2004 alone pastes the brief into the void and reports success — an unseeded session that
looks seeded, which is the worst available outcome. So: armed AND painted AND quiet, and on
timeout nothing is written and the seed stays pending.

**"Painted" is per engine (#966).** Claude's whole READY startup is smaller than the byte rule's
threshold, so claude is judged on its screen. The claude tests below feed REAL captures
(`tests/fixtures/claude_startup_*`, provenance beside them) through `scrollback._buffer_append` at
their recorded read boundaries — the path production takes — never by assigning `_BUFFERS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from agent_sessions import headless_seed, scrollback, session_input, vtscreen

KEY = "claude:11111111-1111-1111-1111-111111111111"
#: The byte rule's own tests run on a codex key: since #966 claude has a rule of its own.
BYTES_KEY = "codex:22222222-2222-2222-2222-222222222222"
FIXTURES = Path(__file__).parent / "fixtures"
REAL = os.environ.get("AGENT_SESSIONS_TEST_REAL_CLAUDE") == "1"

ALT_ENTER = b"\x1b[?1049h"
ARM = b"\x1b[?2004h"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    session_input.reset()
    scrollback._BUFFERS.clear()
    yield
    session_input.reset()
    scrollback._BUFFERS.clear()


def _paint(key: str, n: int) -> None:
    scrollback._BUFFERS[key] = bytearray(b"x" * n)


def _capture(size: str) -> tuple[bytes, list[bytes], int, int]:
    """A recorded claude startup: ``(bytes, the reads as recorded, rows, cols)``."""
    data = (FIXTURES / f"claude_startup_{size}.bin").read_bytes()
    reads = json.loads((FIXTURES / f"claude_startup_{size}.chunks.json").read_text())
    parts = [data[r["offset"] : r["offset"] + r["length"]] for r in reads]
    assert b"".join(parts) == data, "the chunk map no longer covers the capture"
    cols, rows = (int(n) for n in size.split("x"))
    return data, parts, rows, cols


def _at_size(monkeypatch, key: str, rows: int, cols: int) -> None:
    """The geometry the headless reader attached at (what `session_stream` sizes its pty from)."""
    monkeypatch.setitem(scrollback._LAST_ROWS, key, rows)
    monkeypatch.setitem(scrollback._LAST_COLS, key, cols)


def _feed(key: str, parts: list[bytes]) -> None:
    for part in parts:
        scrollback._buffer_append(key, part)


def _split(data: bytes, cuts) -> list[bytes]:
    edges = [0, *sorted(set(cuts)), len(data)]
    return [data[a:b] for a, b in itertools.pairwise(edges) if b > a]


def _interior(data: bytes, seq: bytes) -> list[int]:
    """Every cut position strictly inside every occurrence of ``seq``."""
    cuts, i = [], data.find(seq)
    while i != -1:
        cuts.extend(range(i + 1, i + len(seq)))
        i = data.find(seq, i + 1)
    return cuts


def _fast_gate(monkeypatch, quiet: float = 0.05) -> None:
    monkeypatch.setattr(headless_seed, "QUIET_S", quiet)
    monkeypatch.setattr(headless_seed, "POLL_S", 0.01)


def _write_stamp(key: str, content: str) -> None:
    """A `.ready` sidecar as an earlier process left it (pre-#966 wrote ``"1"``)."""
    scrollback._SCROLLBACK_DIR.mkdir(parents=True, exist_ok=True)
    scrollback._ready_path(key).write_text(content)


def _forget(key: str) -> None:
    """What a restart (or an eviction) loses: every in-memory trace; the sidecars stay."""
    scrollback._drop_buffer(key)
    scrollback._READY.discard(key)
    scrollback._READY_SOURCE.pop(key, None)
    scrollback._LOADED_FROM_DISK.discard(key)


@pytest.mark.anyio
async def test_ARMED_ALONE_is_not_readiness(monkeypatch):
    """The measured failure this gate exists for: 2004 set, nothing painted."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    monkeypatch.setattr(scrollback, "first_paint_seen", lambda k: False)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False
    assert "first-paint" in why


@pytest.mark.anyio
async def test_PAINTED_ALONE_is_not_readiness(monkeypatch):
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: False)
    _paint(BYTES_KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False
    assert "bracketed-paste" in why


@pytest.mark.anyio
async def test_all_three_together_ARE_readiness(monkeypatch):
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(BYTES_KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=2.0)
    assert ready is True and why == ""


@pytest.mark.anyio
async def test_a_screen_mutation_IN_FLIGHT_is_not_quiet(monkeypatch):
    """The seqlock's odd interval means an append is mid-flight; a quiet reading taken then is not
    quiet at all. `screen_is_stable` is the same guard the write fence uses."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(BYTES_KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(session_input, "screen_is_stable", lambda k: False)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False and "quiet" in why


@pytest.mark.anyio
async def test_the_reason_NAMES_which_signal_never_came_true(monkeypatch):
    """ "The brief was never delivered" is useless without which of the three failed."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: False)
    monkeypatch.setattr(scrollback, "first_paint_seen", lambda k: False)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False
    assert "bracketed-paste" in why and "first-paint" in why


@pytest.mark.anyio
async def test_a_timeout_writes_NOTHING_and_leaves_the_seed_pending(monkeypatch):
    """Fail SAFE. An unseeded session with a warning beats bytes pasted into the void."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: False)
    borrowed = []
    monkeypatch.setattr(session_input, "borrow_writer", lambda k: borrowed.append(k) or None)
    delivered, why = await headless_seed.deliver(KEY, KEY, timeout=0.3)
    assert delivered is False and why
    # The writer is never even borrowed — nothing was written, so the claim is untouched and the
    # next attach still finds the seed.
    assert borrowed == []


@pytest.mark.anyio
async def test_no_owner_is_reported_as_such_rather_than_as_a_failed_write(monkeypatch):
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(BYTES_KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(headless_seed, "SETTLE_S", 0.0)
    monkeypatch.setattr(session_input, "borrow_writer", lambda k: None)
    delivered, why = await headless_seed.deliver(BYTES_KEY, BYTES_KEY, timeout=2.0)
    assert delivered is False
    assert "nothing owns" in why


@pytest.mark.anyio
async def test_a_DEAD_writer_is_told_apart_from_an_absent_one(monkeypatch):
    """`borrow_writer` raises for a registration whose fd is already closed. That is a BROKEN
    owner, not no owner, and the two send an operator to different places."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(BYTES_KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(headless_seed, "SETTLE_S", 0.0)

    def boom(k):
        raise OSError(9, "bad fd")

    monkeypatch.setattr(session_input, "borrow_writer", boom)
    delivered, why = await headless_seed.deliver(BYTES_KEY, BYTES_KEY, timeout=2.0)
    assert delivered is False and "writer is dead" in why


def test_the_gate_uses_WEBTERM_S_OWN_constants():
    """Two copies of a readiness threshold is two answers to "is it ready", and the interesting
    failure is always the one where they disagree."""
    from agent_sessions import webterm

    assert headless_seed.FIRST_PAINT_BYTES == webterm._SEED_FIRST_PAINT_BYTES
    assert headless_seed.QUIET_S == webterm._SEED_QUIET_S
    assert headless_seed.READY_TIMEOUT_S == webterm._SEED_READY_TIMEOUT_S
    assert headless_seed.SETTLE_S == webterm._SEED_SETTLE_S


# ---------------------------------------------------------------------------
# #966 — claude's READY screen is smaller than the byte rule's threshold.
# ---------------------------------------------------------------------------


async def _a_real_boot_opens_the_gate(monkeypatch, size: str) -> None:
    data, parts, rows, cols = _capture(size)
    _at_size(monkeypatch, KEY, rows, cols)
    _feed(KEY, parts)
    # The bug, stated: the whole ready screen is under the byte threshold, so no byte count opens.
    assert len(data) < headless_seed.FIRST_PAINT_BYTES
    assert scrollback.has_mode(KEY, 2004)
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(KEY, timeout=2.0)
    assert (ready, why) == (True, "")
    assert headless_seed._painted(KEY) is True
    assert scrollback.first_paint_source(KEY) == "screen:claude"


@pytest.mark.anyio
async def test_a_real_claude_alt_screen_boot_at_80x24_IS_painted(monkeypatch):
    await _a_real_boot_opens_the_gate(monkeypatch, "80x24")


@pytest.mark.anyio
async def test_a_real_claude_alt_screen_boot_at_120x40_IS_painted(monkeypatch):
    """A second geometry: a threshold that only fits 80×24 fails one of the two."""
    await _a_real_boot_opens_the_gate(monkeypatch, "120x40")


@pytest.mark.parametrize("size", ["80x24", "120x40"])
@pytest.mark.parametrize(
    "how", ["one-byte-reads", "all-cuts-inside-2004h-and-1049h", "each-single-cut-inside"]
)
def test_the_real_boot_is_painted_however_its_escapes_are_split(monkeypatch, size, how):
    """Neither signal may depend on where the reads fell: a `?2004h` or `?1049h` split across
    reads must arm and switch exactly as one that arrived whole."""
    data, _parts, rows, cols = _capture(size)
    _at_size(monkeypatch, KEY, rows, cols)
    inside = _interior(data, ARM) + _interior(data, ALT_ENTER)
    assert inside, "the capture lost its escape sequences"
    if how == "one-byte-reads":
        splits = [[data[i : i + 1] for i in range(len(data))]]
    elif how == "all-cuts-inside-2004h-and-1049h":
        splits = [_split(data, inside)]
    else:
        splits = [_split(data, [cut]) for cut in inside]
    for parts in splits:
        scrollback.clear_scrollback([KEY])
        _feed(KEY, parts)
        assert bytes(scrollback._BUFFERS[KEY]) == data
        assert scrollback.has_mode(KEY, 2004)
        assert headless_seed._painted(KEY) is True


#: A cold codex first-run preamble: bracketed paste armed, terminal queries, and nothing drawn —
#: the #711/#722 shape (91 B, then stdin discarded through a long cold start).
_QUERIES = b"\x1b[?2004h\x1b[>7u\x1b[?1004h\x1b[6n\x1b[?u\x1b[c\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
COLD_CODEX_PREAMBLE = _QUERIES + b"\x1b[?25l" * ((91 - len(_QUERIES)) // 6)
COLD_CODEX_PREAMBLE += b"\r" * (91 - len(COLD_CODEX_PREAMBLE))


@pytest.mark.anyio
async def test_a_cold_codex_preamble_is_NOT_painted(monkeypatch):
    assert len(COLD_CODEX_PREAMBLE) == 91
    scrollback._buffer_append(BYTES_KEY, COLD_CODEX_PREAMBLE)
    assert scrollback.has_mode(BYTES_KEY, 2004)
    assert headless_seed._painted(BYTES_KEY) is False
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False and "first-paint" in why


def test_the_claude_rule_does_not_apply_to_OTHER_engines(monkeypatch):
    """The rule is per engine: claude's own painted screen under a codex key is still 1435 B."""
    _data, parts, rows, cols = _capture("80x24")
    _at_size(monkeypatch, BYTES_KEY, rows, cols)
    _feed(BYTES_KEY, parts)
    assert headless_seed._painted(BYTES_KEY) is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ring",
    [ALT_ENTER, ALT_ENTER + ARM + b"\x1b[H\x1b[2J"],
    ids=["alt-screen-entered", "alt-screen-entered-armed-and-cleared"],
)
async def test_a_blank_claude_alt_screen_is_NOT_painted(monkeypatch, ring):
    """A predicate that only checks the alt-screen switch must fail this."""
    scrollback._buffer_append(KEY, ring)
    assert headless_seed._painted(KEY) is False
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)  # isolate the paint signal
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False and "first-paint" in why


@pytest.mark.anyio
async def test_the_armed_and_quiet_61_B_BEFORE_claude_paints_is_NOT_painted(monkeypatch):
    """The real false-ready window: at 120×40 the capture sits armed and quiet for ~3 s at 61 B,
    with nothing drawn and the alternate screen not yet entered. A gate on armed + quiet alone
    types the brief into it."""
    _data, parts, rows, cols = _capture("120x40")
    _at_size(monkeypatch, KEY, rows, cols)
    prefix = list(itertools.takewhile(lambda n: n <= 61, itertools.accumulate(map(len, parts))))
    _feed(KEY, parts[: len(prefix)])
    assert len(scrollback._BUFFERS[KEY]) == 61
    assert scrollback.has_mode(KEY, 2004)
    assert headless_seed._painted(KEY) is False
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.5)
    assert ready is False
    assert why.startswith("the session never became ready (first-paint never true")


@pytest.mark.parametrize(
    "ring",
    [
        pytest.param("flipped", id="the-real-screen-drawn-on-the-PRIMARY-screen"),
        pytest.param("left", id="claude-LEFT-the-alt-screen"),
        pytest.param("text-then-blank-alt", id="primary-text-then-a-BLANK-alt-screen"),
    ],
)
def test_rows_that_are_not_on_the_alt_screen_are_NOT_a_claude_paint(monkeypatch, ring):
    """`vtscreen.render` does not model the alternate screen — it skips every `CSI ? … h/l`, so
    primary text survives `?1049h` and alt text survives `?1049l`. The switch is therefore read
    from the bytes, and rows are counted only from the switch onward."""
    data, _parts, rows, cols = _capture("80x24")
    if ring == "flipped":
        raw = data.replace(ALT_ENTER, b"\x1b[?1049l")
    elif ring == "left":
        raw = data + b"\x1b[?1049l"
    else:
        raw = b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\n" + ALT_ENTER
    rendered = vtscreen.render(raw, rows, cols)
    assert sum(1 for line in rendered.split("\n") if line.strip()) >= 5, "the rows are there"
    _at_size(monkeypatch, KEY, rows, cols)
    scrollback._buffer_append(KEY, raw)
    assert headless_seed._painted(KEY) is False


# ---- stamp provenance ------------------------------------------------------------------------


@pytest.mark.parametrize("stamp", ["legacy-sidecar", "bytes-sidecar", "byte-rule-in-process"])
def test_a_BYTE_RULE_or_LEGACY_stamp_does_not_paint_a_blank_claude_screen(stamp):
    """The shared durable flag must not be a side door. A stamp that only counted bytes — whether
    this process wrote it or a pre-#966 process left `"1"` behind — says nothing about claude."""
    if stamp == "legacy-sidecar":
        _write_stamp(KEY, "1")
    elif stamp == "bytes-sidecar":
        _write_stamp(KEY, "bytes")
    else:
        scrollback.note_first_paint(KEY)  # the byte rule's own call
    scrollback._buffer_append(KEY, ALT_ENTER + ARM)
    assert scrollback.first_paint_seen(KEY) is True  # the stamp IS there…
    assert headless_seed._painted(KEY) is False  # …and does not count for claude
    # …and a failed check neither upgrades nor erases it.
    assert scrollback.first_paint_source(KEY) in {"legacy", "bytes"}


def test_an_ATTACH_stamp_paints_a_claude_key():
    """The browser path measured its own run at the browser's real size; that is trusted."""
    scrollback.note_first_paint(KEY, source="attach")
    assert headless_seed._painted(KEY) is True


def test_a_LEGACY_stamp_still_paints_a_CODEX_key():
    """Unchanged for every other engine: any stamp counts, including one from before #966."""
    _write_stamp(BYTES_KEY, "1")
    assert headless_seed._painted(BYTES_KEY) is True
    assert scrollback.first_paint_source(BYTES_KEY) == "legacy"


def test_the_stamp_SOURCE_survives_a_restart():
    scrollback.note_first_paint(KEY, source="screen:claude")
    assert scrollback._ready_path(KEY).read_text() == "screen:claude"
    _forget(KEY)
    assert KEY not in scrollback._READY and KEY not in scrollback._READY_SOURCE
    assert scrollback.first_paint_source(KEY) == "screen:claude"  # rehydrated from the sidecar
    assert headless_seed._painted(KEY) is True  # and trusted, with nothing in the ring


@pytest.mark.parametrize("content", ["1", "", "a future source"])
def test_a_pre_966_or_unrecognised_sidecar_hydrates_as_LEGACY(content):
    _write_stamp(KEY, content)
    assert scrollback.first_paint_seen(KEY) is True
    assert scrollback.first_paint_source(KEY) == "legacy"


def test_a_MEASURED_stamp_upgrades_a_weak_one_and_is_never_downgraded():
    scrollback.note_first_paint(KEY)
    assert scrollback.first_paint_source(KEY) == "bytes"
    scrollback.note_first_paint(KEY, source="attach")
    assert scrollback.first_paint_source(KEY) == "attach"
    assert scrollback._ready_path(KEY).read_text() == "attach"
    scrollback.note_first_paint(KEY, source="bytes")
    assert scrollback.first_paint_source(KEY) == "attach"
    # A new process whose byte rule stamps BEFORE anything hydrated this key.
    _forget(KEY)
    scrollback.note_first_paint(KEY, source="bytes")
    assert scrollback.first_paint_source(KEY) == "attach"
    assert scrollback._ready_path(KEY).read_text() == "attach"


def test_a_LEGACY_claude_stamp_is_re_evaluated_and_upgraded_by_a_real_paint(monkeypatch):
    _write_stamp(KEY, "1")
    _data, parts, rows, cols = _capture("80x24")
    _at_size(monkeypatch, KEY, rows, cols)
    _feed(KEY, parts)
    assert headless_seed._painted(KEY) is True
    assert scrollback.first_paint_source(KEY) == "screen:claude"
    assert scrollback._ready_path(KEY).read_text() == "screen:claude"


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        scrollback.note_first_paint(KEY, source="guess")


@pytest.mark.anyio
async def test_browser_attach_then_headless_only_a_MEASURED_stamp_opens_the_claude_gate(
    monkeypatch,
):
    """The handover order the issue names. An earlier stamp written under the old byte rule (a
    pre-#966 attach left `"1"`) does not open the headless claude gate on a screen that is not
    painted; an `"attach"` stamp does. The real attach path's tag is pinned in `test_handoff`."""
    _write_stamp(KEY, "1")
    scrollback._buffer_append(KEY, ARM + ALT_ENTER + b"\x1b[H\x1b[2J")
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False
    assert why.endswith("(first-paint never true: alt screen active, 0 of 4 rows at 80×24)")

    scrollback.note_first_paint(KEY, source="attach")
    ready, why = await headless_seed.wait_ready(KEY, timeout=2.0)
    assert (ready, why) == (True, "")


# ---- the failure reason says what was observed ----------------------------------------------


@pytest.mark.anyio
async def test_the_BYTE_RULE_failure_names_the_bytes_and_the_size(monkeypatch):
    _at_size(monkeypatch, BYTES_KEY, 24, 80)
    scrollback._buffer_append(BYTES_KEY, ARM + b"x" * (1432 - len(ARM)))
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(BYTES_KEY, timeout=0.3)
    assert ready is False
    assert why == (
        "the session never became ready (first-paint never true: 1432 B of 2048 B at 80×24)"
    )


@pytest.mark.anyio
async def test_the_CLAUDE_failure_names_the_screen_the_rows_and_the_size(monkeypatch):
    _at_size(monkeypatch, KEY, 40, 120)
    scrollback._buffer_append(KEY, ARM + ALT_ENTER + b"\x1b[H\x1b[2Jone\r\ntwo\r\nthree")
    _fast_gate(monkeypatch)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False
    assert why == (
        "the session never became ready "
        "(first-paint never true: alt screen active, 3 of 4 rows at 120×40)"
    )


@pytest.mark.anyio
async def test_the_reason_lists_every_missing_signal_BEFORE_what_the_paint_rule_saw(monkeypatch):
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.1)
    assert ready is False
    assert why == (
        "the session never became ready (bracketed-paste, first-paint, quiet never true: "
        "alt screen not active, 0 of 4 rows at 80×24)"
    )


# ---- opt-in: the real binary -----------------------------------------------------------------


def _claude_trusts(cwd: Path) -> bool:
    """Read-only: has the operator accepted claude's workspace-trust dialog for ``cwd``? An
    untrusted folder shows that dialog instead of the prompt — not the screen this test pins."""
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return False
    project = (cfg.get("projects") or {}).get(str(cwd)) or {}
    return bool(project.get("hasTrustDialogAccepted"))


def _reap(native: str) -> None:
    """SIGTERM, then SIGKILL, every process group holding ``native`` in its argv, plus their
    descendants' groups. Never pgid ≤ 1 (killpg(1) is kill(-1)), never this test's own group."""

    def cmdline(pid: int) -> bytes:
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return b""

    def ppid(pid: int) -> int | None:
        try:
            return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return None

    pids = [int(d) for d in os.listdir("/proc") if d.isdigit() and int(d) != os.getpid()]
    tree = {p for p in pids if native.encode() in cmdline(p)}
    children: dict[int, list[int]] = {}
    for p in pids:
        parent = ppid(p)
        if parent is not None:
            children.setdefault(parent, []).append(p)
    stack = list(tree)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    groups = set()
    for p in tree:
        with contextlib.suppress(ProcessLookupError):
            groups.add(os.getpgid(p))
    groups = {g for g in groups if g > 1 and g != os.getpgrp()}
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        for g in groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(g, sig)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and any(Path(f"/proc/{p}").exists() for p in tree):
            time.sleep(0.1)


@pytest.mark.skipif(not REAL, reason="set AGENT_SESSIONS_TEST_REAL_CLAUDE=1 to boot the binary")
@pytest.mark.anyio
async def test_a_REAL_claude_boot_opens_the_headless_gate_without_typing(monkeypatch):
    """The version-coupling guard for the claude rule: a release that changes the startup screen
    must fail this test instead of a mission.

    The production path: `ClaudeProvider.new_launch_argv(bypass=False)` wrapped by
    `ptybridge.launch_argv(detached=True)` (a `dtach -n` master), then the `SessionStream` reader
    at 80×24. Nothing is ever written to the session — the gate is only observed.
    """
    from agent_sessions import ptybridge, session_stream
    from agent_sessions.engines.claude import ClaudeProvider

    repo = Path(__file__).resolve().parents[1]
    cwd = Path(os.environ.get("AGENT_SESSIONS_TEST_REAL_CLAUDE_CWD") or repo)
    if not _claude_trusts(cwd):
        pytest.skip(f"claude has not trusted {cwd}; set AGENT_SESSIONS_TEST_REAL_CLAUDE_CWD")
    native = str(uuid.uuid4())
    key = f"claude:{native}"
    launch = ClaudeProvider().new_launch_argv(native, cwd=str(cwd), bypass=False)
    if not (os.path.isabs(launch[0]) and os.access(launch[0], os.X_OK)):
        pytest.skip(f"no claude binary at {launch[0]!r}")
    if not shutil.which(ptybridge.DTACH_BIN):
        pytest.skip("dtach is not installed")
    # A short runtime dir: AF_UNIX socket paths are capped at 108 bytes and pytest's are long.
    runtime = tempfile.mkdtemp(prefix="as966-")
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", runtime)
    argv = ptybridge.launch_argv(
        engine="claude", session_id=native, launch_argv=launch, detached=True
    )
    sock = ptybridge.socket_path("claude", native)
    # The service's environment has no CLAUDE* names; a suite run from inside claude does.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    _at_size(monkeypatch, key, 24, 80)
    stream = session_stream.SessionStream("claude", native)
    try:
        subprocess.run(  # noqa: S603 — literal argv from the production builders
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + 5
        while not sock.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert sock.exists(), "the dtach master never created its socket"
        await stream.start()
        assert not stream.ended.is_set(), "the reader could not attach"
        ready, why = await headless_seed.wait_ready(key, timeout=20.0)
        ring = bytes(scrollback._BUFFERS.get(key) or b"")
        assert ready, f"{why} — {len(ring)} B observed"
        assert scrollback.first_paint_source(key) == "screen:claude"
    finally:
        await stream.stop()
        _reap(native)
        with contextlib.suppress(OSError):
            sock.unlink()
        shutil.rmtree(runtime, ignore_errors=True)
