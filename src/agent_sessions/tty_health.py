"""Detect and repair a session PTY that has fallen out of raw mode (#804).

A TUI agent puts its PTY into raw mode so every keystroke reaches it immediately. If that
PTY reverts to the kernel default — canonical + echo — the line discipline swallows input
into a line buffer and echoes control bytes in caret notation, so the agent receives
*nothing* while the operator watches ``^[[B^[[B^[[B…`` pile up on screen. The session stays
live, attached and writable by every measure the app has; it simply cannot be typed into.

Observed on 2026-08-06: all three running ``codex`` sessions were in this state at once,
while every ``claude`` and ``kimi`` PTY was fine and a freshly launched ``codex`` came up
raw. So the PTY is *lost*, not never-set, and the app is not what loses it — nothing here
calls ``tcsetattr``; the only termios use in the codebase is a ``TIOCSWINSZ`` window-size
ioctl. Whatever upstream drops raw mode, the session is recoverable, and this module is how
the app recovers it instead of a human running ``stty`` against ``/dev/pts/N`` on the host.

**Cooked is not always broken**, and that is the whole difficulty. Two states look identical
in the termios flags and must never be touched:

* ``shell`` (#636) is a bare ``bash -l``. Between commands it is cooked *by design* —
  repairing it would break the operator's own line editing. It opts out via
  ``expects_raw_tty``, and the flag is **default-deny**, so the next agentless engine added
  to the registry is excluded automatically rather than by remembering to exclude it.
* A TUI that shells out to a pager or ``$EDITOR`` on its own tty is cooked *correctly* for
  as long as that child runs. The foreground process group says so: when the terminal's
  foreground pgrp is not the engine's own, the terminal belongs to that child and we leave
  it alone.

What is left after both gates is a TUI that owns its terminal and is not reading it — which
is the fault. It is confirmed by a second read a moment later (:data:`_CONFIRM_DELAY_S`) so a
TUI mid-toggle is never raced, then repaired by :func:`_restore_input_raw`.

**Detection keys on ``ICANON``/``ECHO`` alone, and the repair writes input flags only.** Both
narrowings come from the same measurement: healthy engines do *not* agree on a single termios
profile. codex runs exactly ``cfmakeraw``; claude runs ``cfmakeraw`` **plus ``OPOST``**. So
"deviates from ``cfmakeraw``" is not a usable definition of broken — it would condemn every
healthy claude session — and "apply ``cfmakeraw``" is not a safe repair, because it would strip
a flag claude keeps. What every healthy engine *does* agree on is the input side, and that is
the only side that decides whether a keystroke reaches the agent. So the invariant enforced
here is narrow and precise: **the line discipline must not be consuming input.**

**Blocking.** Every entry point here scans ``/proc`` and may sleep for the confirm read.
Callers on the event loop must go through ``asyncio.to_thread`` — a blocking scan on the loop
freezes every session this process serves (the #678 failure mode).
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import termios
import time
from dataclasses import dataclass

from . import engines, reaper

log = logging.getLogger("agent_sessions.tty_health")

# Linux allocates UNIX98 pty slaves (/dev/pts/N) majors 136-143. Anything else is not a
# session PTY and is never opened, let alone written to.
_PTS_MAJOR_MIN = 136
_PTS_MAJOR_MAX = 143

# The two flags that decide whether keystrokes reach the agent at all: ICANON buffers input
# until a newline, ECHO makes the line discipline paint it back. Either one set on a TUI's
# own terminal means the agent is not reading raw keystrokes.
_COOKED_MASK = termios.ICANON | termios.ECHO

# Gap between the first cooked reading and the confirming one. Long enough that a TUI briefly
# restoring its terminal mid-redraw is not mistaken for a stuck one, short enough to stay
# invisible on the attach path (this runs once per attach, not per output chunk).
_CONFIRM_DELAY_S = 0.15

HEALTHY = "healthy"
STUCK = "stuck"
COOKED_BY_CHILD = "cooked_by_child"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    """What was found, and what (if anything) was done about it."""

    status: str
    detail: str = ""
    device: str | None = None
    repaired: bool = False
    rdev: int | None = None  # device number of `device`, for a later cheap re-check

    @property
    def ok(self) -> bool:
        """True when the agent can receive keystrokes — either it always could, or we fixed it."""
        return self.status in (HEALTHY, COOKED_BY_CHILD, NOT_APPLICABLE) or self.repaired


@dataclass(frozen=True)
class _Stat:
    """The three fields of ``/proc/<pid>/stat`` this module needs."""

    pgrp: int  # field 5 — the process's own process group
    tty_nr: int  # field 7 — its CONTROLLING terminal, as a device number
    tpgid: int  # field 8 — the FOREGROUND process group of that terminal


def _parse_stat(raw: str) -> _Stat | None:
    """Pull fields 5, 7 and 8 out of a ``/proc/<pid>/stat`` line.

    Parsed from after the LAST ``)`` because field 2 is ``comm`` in parentheses and may itself
    contain spaces and parens — ``(codex (worker))`` is a legal comm, and splitting the line on
    whitespace would shift every field after it.
    """
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 1 :].split()
    # fields[0] is state (field 3), so field N is fields[N - 3].
    if len(fields) < 6:
        return None
    try:
        return _Stat(pgrp=int(fields[2]), tty_nr=int(fields[4]), tpgid=int(fields[5]))
    except ValueError:
        return None


def _proc_stat(pid: int) -> _Stat | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            return _parse_stat(fh.read())
    except OSError:
        return None


def _pts_path(tty_nr: int) -> str | None:
    """``/dev/pts/N`` for a ``tty_nr``, or ``None`` if it isn't a UNIX98 pty slave.

    The kernel packs the device number with the minor split either side of the major
    (``mkdev``): low 8 bits of the minor, then the major, then the minor's high bits.
    """
    if tty_nr <= 0:
        return None
    major = (tty_nr >> 8) & 0xFFF
    minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
    if not _PTS_MAJOR_MIN <= major <= _PTS_MAJOR_MAX:
        return None
    return f"/dev/pts/{minor}"


def _direct_children(master_pid: int) -> list[int]:
    """Every direct child of ``master_pid``.

    ``/proc/<pid>/task/<pid>/children`` when the kernel exposes it (CONFIG_PROC_CHILDREN),
    otherwise a ``PPid`` scan. Returns them all — deciding what to do with more than one is
    :func:`_engine_root_pid`'s job, not this one's.
    """
    try:
        with open(f"/proc/{master_pid}/task/{master_pid}/children", encoding="ascii") as fh:
            kids = [int(k) for k in fh.read().split()]
        if kids:
            return kids
    except (OSError, ValueError):
        pass
    out: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        if int(line.split()[1]) == master_pid:
                            out.append(int(name))
                        break
        except (OSError, ValueError, IndexError):
            continue
    return out


def _engine_root_pid(master_pid: int) -> int | None:
    """The ``dtach`` master's single direct child — the engine process that owns the PTY.

    **Exactly one, or nothing.** dtach forks exactly one program, so a master with two direct
    children is a topology this module does not understand — a fork mid-exec, a wrapper that
    kept a sibling alive, something unanticipated. Picking the first one `/proc` happens to list
    would authorize a `tcsetattr` against whichever PID won a race, which is precisely the
    "ambiguous ⇒ write nothing" boundary this module promises. So ambiguity resolves to ``None``
    and the caller reports ``unknown`` rather than guessing.
    """
    kids = _direct_children(master_pid)
    return kids[0] if len(kids) == 1 else None


def _open_pts(path: str, expect_rdev: int) -> int | None:
    """Open a resolved PTY slave for termios work, or ``None`` if it isn't what we resolved.

    ``O_NOCTTY`` is not optional: without it this process could acquire the agent's terminal
    as its own controlling tty. ``O_NONBLOCK`` keeps the open from stalling on a PTY with no
    reader. The ``fstat`` afterwards is the open-then-verify half — the path was derived from
    ``/proc`` and could have been recycled between resolving and opening, so the thing we
    actually hold must still be the character device we resolved, or we close it untouched.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISCHR(st.st_mode) or st.st_rdev != expect_rdev:
            os.close(fd)
            return None
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    return fd


def _pgrps(root_pid: int) -> tuple[int, int] | None:
    """``(the terminal's foreground pgrp, the engine's own pgrp)``, or ``None`` if unreadable.

    Equal means the engine itself is what the terminal is talking to; different means a child
    command took the foreground and the line discipline is that child's to configure.

    Read from ``/proc``, deliberately **not** ``tcgetpgrp``. The obvious implementation —
    ``os.tcgetpgrp(fd)`` on the opened slave — cannot work here and fails closed in a way that
    is easy to miss: Linux's ``TIOCGPGRP`` returns ``ENOTTY`` for a pty *slave* that is not the
    calling process's own controlling terminal, which this one never is (we open ``O_NOCTTY``,
    by design). It was written that way first and the live end-to-end check caught it. ``tpgid``
    is the same number without the ioctl, and it comes from the read we already do.
    """
    st = _proc_stat(root_pid)
    if st is None:
        return None
    return st.tpgid, st.pgrp


def _restore_input_raw(fd: int) -> None:
    """Restore the INPUT side of raw mode, and discard what the cooked terminal was holding.

    **Output processing is deliberately not touched.** ``cfmakeraw(3)`` would also clear
    ``OPOST``, and a healthy PTY was measured with ``OPOST`` *set*: codex runs exactly
    ``cfmakeraw``, but claude runs ``cfmakeraw`` **plus** ``OPOST`` — the only flag on which two
    healthy engines disagree. Blanket-applying ``cfmakeraw`` would therefore strip a flag claude
    deliberately keeps, on every repair, and a detector keyed on the full profile would call
    every healthy claude session broken. ``OPOST`` governs how output is post-processed and has
    nothing to do with whether a keystroke reaches the agent, so a repair of *input* has no
    business writing it. Everything below is a flag both engines were measured to agree on.

    ``TCSAFLUSH``, not ``TCSANOW`` — and this is a safety property, not a detail. Bytes queued
    while the line discipline was canonical are stale: arrows the operator pressed at a terminal
    that was visibly ignoring them, half-finished escape sequences, orphaned paste wrappers.
    Switching with ``TCSANOW`` releases the whole backlog into the TUI as one burst the instant
    it starts reading again, which can drive a menu or confirm a dialog nobody chose. Retyping a
    keystroke is free; an unintended one is not. So the queue is dropped with the repair, in the
    same atomic call rather than a separate ``tcflush`` a concurrent write could slip past.
    """
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    iflag &= ~(
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB)
    cflag |= termios.CS8
    cc = list(cc)
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])


def _cooked_flags(lflag: int) -> str:
    """The offending flags, named — so a repair log line says what it actually changed."""
    names = []
    if lflag & termios.ICANON:
        names.append("ICANON")
    if lflag & termios.ECHO:
        names.append("ECHO")
    return "|".join(names) or "none"


def _resolve(key: str) -> tuple[str, int, int] | None:
    """``(device, rdev, engine_root_pid)`` for a session key, or ``None`` if unresolvable.

    Re-done from the socket path on every call and never cached: a session can be reaped and
    relaunched between checks, and a stale device number would point the repair at whatever
    reused that pty. Unresolvable means we write nothing.
    """
    try:
        prov, native = engines.parse_key(key, allow_new_placeholder=True)
    except Exception:
        return None
    master_pid = reaper._find_master_pid(prov.engine_id, native)
    if master_pid is None:
        return None
    root = _engine_root_pid(master_pid)
    if root is None:
        return None
    st = _proc_stat(root)
    if st is None:
        return None
    device = _pts_path(st.tty_nr)
    if device is None:
        return None
    return device, st.tty_nr, root


def _expects_raw(key: str) -> bool:
    try:
        prov, _ = engines.parse_key(key, allow_new_placeholder=True)
    except Exception:
        return False
    return engines.expects_raw_tty(prov)


def inspect(key: str, *, repair: bool = False) -> Verdict:
    """Classify ``key``'s PTY, and repair it when ``repair`` and it is genuinely stuck.

    BLOCKING — scans ``/proc`` and sleeps for the confirm read. Never call from the loop.
    """
    if not _expects_raw(key):
        return Verdict(NOT_APPLICABLE, "engine does not run a raw-mode TUI")

    resolved = _resolve(key)
    if resolved is None:
        return Verdict(UNKNOWN, "no live master, or its PTY could not be resolved")
    device, rdev, root = resolved

    fd = _open_pts(device, rdev)
    if fd is None:
        return Verdict(
            UNKNOWN, f"{device} could not be opened as the resolved PTY", device, rdev=rdev
        )
    try:
        try:
            lflag = termios.tcgetattr(fd)[3]
        except OSError as e:
            return Verdict(UNKNOWN, f"tcgetattr failed ({e.strerror})", device, rdev=rdev)
        if not lflag & _COOKED_MASK:
            return Verdict(HEALTHY, "raw", device, rdev=rdev)

        # Cooked — but whose terminal is it right now? A child command in the foreground owns
        # the line discipline and is entitled to a cooked one.
        pgrps = _pgrps(root)
        if pgrps is None:
            return Verdict(UNKNOWN, "foreground process group unreadable", device, rdev=rdev)
        fg, own = pgrps
        if fg <= 0:
            # No foreground process group at all — nothing is reading this terminal and we
            # cannot say the engine should be. Report, don't write.
            return Verdict(UNKNOWN, "terminal has no foreground process group", device, rdev=rdev)
        if fg != own:
            return Verdict(
                COOKED_BY_CHILD,
                f"foreground pgrp {fg} is not the engine's {own}",
                device,
                rdev=rdev,
            )

        if not repair:
            return Verdict(
                STUCK, f"cooked ({_cooked_flags(lflag)}) with no child in front", device, rdev=rdev
            )

        # Confirm before writing: a TUI restoring its terminal for one redraw is not stuck.
        time.sleep(_CONFIRM_DELAY_S)

        # EVERY precondition is re-established here, at the write boundary — not just the flags.
        # The delay is a window, and an authorization computed before it says nothing about the
        # instant we actually write: a pager can take the foreground inside 150 ms, and a session
        # can be reaped and relaunched so that this pts number now belongs to somebody else. The
        # open fd pins the file *description*, so our bytes cannot be redirected — but that is a
        # different guarantee from "the thing on the other end is still the session we judged".
        # Re-resolving is cheap on a path that runs once per attach, and the failure it prevents
        # is writing termios into an innocent terminal.
        if _resolve(key) != resolved:
            return Verdict(
                UNKNOWN, "session identity changed during the confirm read", device, rdev=rdev
            )
        pgrps = _pgrps(root)
        if pgrps is None:
            return Verdict(
                UNKNOWN, "foreground process group unreadable at the write boundary", device
            )
        fg, own = pgrps
        if fg <= 0:
            return Verdict(UNKNOWN, "terminal lost its foreground process group", device, rdev=rdev)
        if fg != own:
            return Verdict(
                COOKED_BY_CHILD,
                f"a child (pgrp {fg}) took the foreground during the confirm read",
                device,
                rdev=rdev,
            )
        try:
            lflag2 = termios.tcgetattr(fd)[3]
        except OSError as e:
            return Verdict(UNKNOWN, f"confirm tcgetattr failed ({e.strerror})", device, rdev=rdev)
        if not lflag2 & _COOKED_MASK:
            return Verdict(HEALTHY, "went raw again during the confirm read", device, rdev=rdev)

        try:
            _restore_input_raw(fd)
        except OSError as e:
            return Verdict(STUCK, f"repair failed ({e.strerror})", device, rdev=rdev)
        log.warning(
            "tty_health: %s PTY %s was consuming input (%s set); cleared it, "
            "restored raw input flags, discarded the queued backlog",
            key,
            device,
            _cooked_flags(lflag2),
        )
        return Verdict(
            STUCK, f"repaired from {_cooked_flags(lflag2)}", device, repaired=True, rdev=rdev
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def still_raw(device: str, rdev: int) -> bool | None:
    """Is this exact PTY still not consuming input? ``None`` if that can't be established.

    The **cheap** counterpart to :func:`inspect`: one open, one ``fstat``, one ``tcgetattr``.
    No ``/proc`` scan, no confirm sleep, no repair — because the one caller holds a lock that
    the browser's own input path contends for, and doing I/O-heavy work there would stall
    every keystroke a viewer types.

    That is the whole point of splitting it out. A verdict from :func:`ensure_raw` describes
    the terminal at the moment it ran; if an unbounded wait follows before the write, the
    verdict is a statement about the past. This re-asks the only question that still matters,
    cheaply enough to ask it with a lock held.
    """
    fd = _open_pts(device, rdev)
    if fd is None:
        return None
    try:
        return not termios.tcgetattr(fd)[3] & _COOKED_MASK
    except OSError:
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def ensure_raw(key: str) -> Verdict:
    """Repair ``key``'s PTY if it is stuck out of raw mode. BLOCKING — see module docstring.

    Best-effort by construction: anything unresolvable, unopenable or ambiguous returns a
    verdict and writes nothing. Never raises — a health check must not be able to fail an
    attach or a delivery.
    """
    try:
        return inspect(key, repair=True)
    except Exception as e:  # pragma: no cover — defence in depth around a best-effort probe
        log.debug("tty_health: check failed for %s: %s", key, e)
        return Verdict(UNKNOWN, f"check raised {type(e).__name__}")
