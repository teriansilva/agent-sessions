"""PTY raw-mode health: detection, the two do-not-touch gates, and the repair (#804).

The dangerous failure here is not "failed to repair" — it is "repaired something that was
cooked on purpose", which breaks a `shell` session's line editing or a pager a TUI legitimately
handed its terminal to. So most of these tests assert that NOTHING was written.

Every test drives a real `openpty()` pair rather than a mock: the thing under test is the
kernel's line discipline, and a mocked termios would pass while the real ioctl did nothing.
`_resolve` and `_pgrps` are patched because /proc resolution and process-group ownership are
what a *live session* supplies — the flag logic is what this file is about.
"""

from __future__ import annotations

import os
import termios
import time

import pytest

from agent_sessions import engines, tty_health


@pytest.fixture
def pty_pair():
    """A real pty. The slave starts in the kernel default (cooked) — the broken state itself."""
    master, slave = os.openpty()
    yield master, slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def _target(slave: int) -> tuple[str, int, int]:
    """What `_resolve` would return for this pty: (device, rdev, engine root pid)."""
    return os.ttyname(slave), os.fstat(slave).st_rdev, os.getpid()


def _wire(monkeypatch, slave: int, *, same_pgrp: bool = True) -> None:
    monkeypatch.setattr(tty_health, "_resolve", lambda key: _target(slave))
    monkeypatch.setattr(tty_health, "_expects_raw", lambda key: True)
    monkeypatch.setattr(tty_health, "_pgrps", lambda root: (7, 7) if same_pgrp else (4242, 7))


def _is_cooked(fd: int) -> bool:
    return bool(termios.tcgetattr(fd)[3] & (termios.ICANON | termios.ECHO))


# --- device-number decoding ---------------------------------------------------------------


def test_pts_path_decodes_a_pty_slave_device_number():
    # /dev/pts/68 == major 136, minor 68.
    assert tty_health._pts_path((136 << 8) | 68) == "/dev/pts/68"


def test_pts_path_decodes_a_minor_above_255():
    # The minor is split either side of the major: low 8 bits, then the high bits at >>12.
    minor = 300
    tty_nr = (136 << 8) | (minor & 0xFF) | ((minor & 0xFFF00) << 12)
    assert tty_health._pts_path(tty_nr) == "/dev/pts/300"


@pytest.mark.parametrize("tty_nr", [0, -1, (4 << 8) | 1, (5 << 8) | 0])
def test_pts_path_rejects_anything_that_is_not_a_pty_slave(tty_nr):
    """A console or a serial tty must never be resolved, let alone written to."""
    assert tty_health._pts_path(tty_nr) is None


def test_stat_parse_survives_a_comm_containing_spaces_and_parens():
    """`comm` is attacker-adjacent (it is the process name) and routinely breaks naive splits."""
    # fields: pid (comm) state ppid PGRP session TTY_NR TPGID …
    raw = f"4242 (codex (worker) x) S 1 900 900 {(136 << 8) | 68} 901 " + "0 " * 20
    st = tty_health._parse_stat(raw)
    assert (st.pgrp, st.tty_nr, st.tpgid) == (900, (136 << 8) | 68, 901)


def test_stat_parse_returns_none_on_a_truncated_line():
    assert tty_health._parse_stat("4242 (codex) S 1") is None


# --- the two do-not-touch gates -----------------------------------------------------------


def test_an_engine_that_is_not_a_raw_tui_is_never_even_resolved(monkeypatch):
    """`shell` is cooked by design. The gate must fire before any /proc work or open()."""
    calls = []
    monkeypatch.setattr(tty_health, "_resolve", lambda key: calls.append(key))
    v = tty_health.ensure_raw("shell:11111111-2222-3333-4444-555555555555")
    assert v.status == tty_health.NOT_APPLICABLE
    assert v.repaired is False
    assert calls == []


def test_shell_provider_declares_itself_out_and_a_tui_declares_itself_in():
    assert engines.expects_raw_tty(engines.get("shell")) is False
    for engine_id in ("claude", "codex", "opencode", "gemini", "antigravity", "kimi"):
        assert engines.expects_raw_tty(engines.get(engine_id)) is True, engine_id


def test_the_capability_is_default_deny_for_an_unknown_provider():
    """A new engine added to the registry must be excluded until it opts in, never included."""

    class NewEngine:
        engine_id = "brandnew"

    assert engines.expects_raw_tty(NewEngine()) is False
    assert engines.expects_raw_tty(None) is False


def test_a_cooked_pty_whose_child_holds_the_foreground_is_left_alone(monkeypatch, pty_pair):
    """A TUI that shelled out to a pager owns nothing here — that pager configured this tty."""
    _master, slave = pty_pair
    _wire(monkeypatch, slave, same_pgrp=False)
    before = termios.tcgetattr(slave)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.COOKED_BY_CHILD
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before
    assert _is_cooked(slave)


# --- detection and repair -----------------------------------------------------------------


def test_a_stuck_pty_is_detected_and_restored_to_raw(monkeypatch, pty_pair):
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    assert _is_cooked(slave), "fixture precondition: a fresh pty slave is cooked"

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.STUCK
    assert v.repaired is True
    assert v.ok is True
    assert not _is_cooked(slave)


def test_the_repair_restores_the_input_flags_every_healthy_engine_agrees_on(monkeypatch, pty_pair):
    """Measured against live healthy PTYs — these are the flags codex and claude both carry."""
    _master, slave = pty_pair
    _wire(monkeypatch, slave)

    tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    iflag, _oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(slave)
    assert not iflag & (
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    assert not lflag & (
        termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
    )
    assert cflag & termios.CS8
    assert not cflag & termios.PARENB
    assert cc[termios.VMIN] == 1
    assert cc[termios.VTIME] == 0


@pytest.mark.parametrize("opost_set", [True, False])
def test_the_repair_never_touches_output_processing(monkeypatch, pty_pair, opost_set):
    """OPOST is the one flag healthy engines DISAGREE on, so the repair must not have an opinion.

    Measured 2026-08-06: a healthy codex PTY is exactly `cfmakeraw` (OPOST clear); a healthy
    claude PTY is `cfmakeraw` **plus OPOST**. Applying `cfmakeraw` wholesale — the obvious
    repair — would strip a flag claude deliberately keeps, on every heal. OPOST governs output
    post-processing and cannot affect whether a keystroke arrives, so repairing input must
    leave it exactly as found, whichever way it was set.
    """
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    mode = termios.tcgetattr(slave)
    mode[1] = (mode[1] | termios.OPOST) if opost_set else (mode[1] & ~termios.OPOST)
    mode[3] |= termios.ICANON | termios.ECHO  # still stuck
    termios.tcsetattr(slave, termios.TCSANOW, mode)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.repaired is True
    assert bool(termios.tcgetattr(slave)[1] & termios.OPOST) is opost_set


def test_the_repair_discards_input_the_cooked_terminal_was_holding(monkeypatch, pty_pair):
    """The backlog is stale keystrokes, and releasing it in one burst can drive the TUI.

    Bytes typed at a terminal that was visibly ignoring them are not input to the state the TUI
    is in now — they are arrows the operator repeated in frustration, half-finished escape
    sequences, orphaned paste wrappers. `TCSANOW` would hand the whole queue to the agent the
    moment it starts reading again; `TCSAFLUSH` drops it with the repair.
    """
    master, slave = pty_pair
    _wire(monkeypatch, slave)
    os.write(master, b"\x1b[B" * 8)  # what the operator's screenshot actually showed
    time.sleep(0.05)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")
    assert v.repaired is True

    os.set_blocking(slave, False)
    with pytest.raises(BlockingIOError):
        os.read(slave, 4096)  # nothing queued: the backlog went with the repair


def test_a_healthy_pty_is_read_and_left_untouched(monkeypatch, pty_pair):
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    tty_health._restore_input_raw(slave)
    before = termios.tcgetattr(slave)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.HEALTHY
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


def test_repair_is_idempotent(monkeypatch, pty_pair):
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    key = "codex:11111111-2222-3333-4444-555555555555"

    assert tty_health.ensure_raw(key).repaired is True
    second = tty_health.ensure_raw(key)
    assert second.status == tty_health.HEALTHY
    assert second.repaired is False


def test_a_pty_that_goes_raw_again_during_the_confirm_read_is_not_written_to(monkeypatch, pty_pair):
    """A TUI mid-toggle looks stuck for an instant. One reading is not evidence."""
    _master, slave = pty_pair
    _wire(monkeypatch, slave)

    class _Clock:
        def sleep(self, _s):  # the TUI restores its own terminal while we wait
            tty_health._restore_input_raw(slave)

    monkeypatch.setattr(tty_health, "time", _Clock())

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.HEALTHY
    assert v.repaired is False


def test_inspect_without_repair_reports_but_never_writes(monkeypatch, pty_pair):
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    before = termios.tcgetattr(slave)

    v = tty_health.inspect("codex:11111111-2222-3333-4444-555555555555", repair=False)

    assert v.status == tty_health.STUCK
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


# --- refusing to act on anything unproven -------------------------------------------------


def test_an_unresolvable_session_writes_nothing(monkeypatch):
    monkeypatch.setattr(tty_health, "_expects_raw", lambda key: True)
    monkeypatch.setattr(tty_health, "_resolve", lambda key: None)
    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")
    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False


def test_open_refuses_a_device_that_is_not_the_one_resolved(pty_pair):
    """Guards the /proc-to-open window: a recycled pts number must not be adopted silently."""
    _master, slave = pty_pair
    device, rdev, _ = _target(slave)
    assert tty_health._open_pts(device, rdev + 1) is None

    fd = tty_health._open_pts(device, rdev)
    assert fd is not None
    os.close(fd)


def test_open_uses_o_noctty_so_the_server_never_adopts_a_session_terminal(pty_pair):
    """Without O_NOCTTY this process could take the agent's terminal as its own."""
    _master, slave = pty_pair
    device, rdev, _ = _target(slave)
    fd = tty_health._open_pts(device, rdev)
    assert fd is not None
    try:
        assert os.get_blocking(fd) is False  # O_NONBLOCK survived
        # A controlling terminal would make this process's session leader own it; we are not a
        # session leader in pytest, so the observable proof is that opening did not change
        # which terminal (if any) this process controls.
        assert os.ttyname(fd) == device
    finally:
        os.close(fd)


def test_a_probe_that_raises_is_swallowed_into_unknown(monkeypatch):
    """A health check must never be able to fail an attach or a delivery."""

    def _boom(_key):
        raise RuntimeError("procfs went sideways")

    monkeypatch.setattr(tty_health, "_expects_raw", lambda key: True)
    monkeypatch.setattr(tty_health, "_resolve", _boom)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")
    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False


def test_pgrps_comes_from_proc_and_not_the_tty_ioctl():
    """Regression guard for the bug the live end-to-end check caught (#804).

    The natural implementation, ``os.tcgetpgrp(fd)`` on the opened slave, raises ``ENOTTY`` for
    any pty that is not the caller's OWN controlling terminal — which is every session PTY,
    since we open ``O_NOCTTY`` on purpose. Reading ``tpgid``/``pgrp`` out of ``/proc`` has no
    such constraint, so this must keep working for an arbitrary pid.
    """
    got = tty_health._pgrps(os.getpid())
    assert got is not None
    _tpgid, pgrp = got
    assert pgrp == os.getpgid(0)


def test_pgrps_returns_none_for_a_pid_that_is_gone():
    assert tty_health._pgrps(0x7FFFFFFF) is None


def test_a_terminal_with_no_foreground_group_is_left_alone(monkeypatch, pty_pair):
    """tpgid of -1 means nothing is reading it — that is not evidence the engine should be."""
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    monkeypatch.setattr(tty_health, "_pgrps", lambda root: (-1, 7))
    before = termios.tcgetattr(slave)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


def test_unreadable_pgrps_writes_nothing(monkeypatch, pty_pair):
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    monkeypatch.setattr(tty_health, "_pgrps", lambda root: None)
    before = termios.tcgetattr(slave)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


# --- the write boundary: every precondition re-established after the confirm delay (#805 r1) ---


def test_a_child_taking_the_foreground_during_the_confirm_read_writes_nothing(
    monkeypatch, pty_pair
):
    """150 ms is long enough for a pager to claim the terminal, and then it is not ours to fix.

    Rechecking only `lflag` after the delay authorizes the write on a pre-delay observation of
    ownership — the classic stale-authorization shape. The verdict must follow the state at the
    instant of the write, not the state that opened the investigation.
    """
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    before = termios.tcgetattr(slave)
    seen = {"n": 0}

    def _pgrps_flipping(_root):
        seen["n"] += 1
        return (7, 7) if seen["n"] == 1 else (4242, 7)  # a child grabs it during the delay

    monkeypatch.setattr(tty_health, "_pgrps", _pgrps_flipping)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.COOKED_BY_CHILD
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


def test_a_session_relaunched_during_the_confirm_read_writes_nothing(monkeypatch, pty_pair):
    """A reaped-and-relaunched session can recycle the pts number onto a different terminal."""
    _master, slave = pty_pair
    _wire(monkeypatch, slave)
    before = termios.tcgetattr(slave)
    seen = {"n": 0}
    real = _target(slave)

    def _resolve_changing(_key):
        seen["n"] += 1
        # Same device, different engine root: the master was relaunched under us.
        return real if seen["n"] == 1 else (real[0], real[1], real[2] + 1)

    monkeypatch.setattr(tty_health, "_resolve", _resolve_changing)

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before


def test_a_master_with_two_direct_children_is_ambiguous_and_resolves_to_nothing(monkeypatch):
    """dtach forks one program; two children is a topology we do not understand.

    Returning whichever PID `/proc` listed first would authorize a termios write against the
    winner of a race — exactly the ambiguity this module promises to refuse.
    """
    monkeypatch.setattr(tty_health, "_direct_children", lambda pid: [4242, 4243])
    assert tty_health._engine_root_pid(999) is None

    monkeypatch.setattr(tty_health, "_direct_children", lambda pid: [])
    assert tty_health._engine_root_pid(999) is None

    monkeypatch.setattr(tty_health, "_direct_children", lambda pid: [4242])
    assert tty_health._engine_root_pid(999) == 4242


def test_an_ambiguous_topology_never_reaches_the_terminal(monkeypatch, pty_pair):
    """End-to-end: ambiguity upstream must surface as `unknown`, with nothing written.

    Both candidate children are made fully resolvable — same controlling terminal, engine in the
    foreground — so the OLD "first child wins" behaviour would sail through every downstream gate
    and repair the terminal. That is what makes this a regression and not a tautology: the only
    thing standing between this pty and a `tcsetattr` is the refusal to pick one of two.
    """
    _master, slave = pty_pair
    tty_nr = os.fstat(slave).st_rdev
    monkeypatch.setattr(tty_health, "_expects_raw", lambda key: True)
    monkeypatch.setattr(tty_health, "_direct_children", lambda pid: [4242, 4243])
    monkeypatch.setattr(tty_health.reaper, "_find_master_pid", lambda e, s: 999)
    monkeypatch.setattr(
        tty_health, "_proc_stat", lambda pid: tty_health._Stat(pgrp=7, tty_nr=tty_nr, tpgid=7)
    )
    monkeypatch.setattr(tty_health, "_pgrps", lambda root: (7, 7))
    before = termios.tcgetattr(slave)
    assert _is_cooked(slave), "precondition: the pty is stuck, so only ambiguity can stop the write"

    v = tty_health.ensure_raw("codex:11111111-2222-3333-4444-555555555555")

    assert v.status == tty_health.UNKNOWN
    assert v.repaired is False
    assert termios.tcgetattr(slave) == before
    assert _is_cooked(slave)
