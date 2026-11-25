"""One definition of "a PID that cannot be live", for stale-lock fixtures (#1210).

The stale-lock tests used to write the literal ``999999`` and call it dead. On a host with
``kernel.pid_max`` 4194304 that is an ordinary PID the busy shared runner hands out, so when a
real process happened to hold it the lock correctly read as live and the tests flaked.

A value ABOVE ``pid_max`` can never be allocated, so it is dead whatever the host is running.
``/proc/sys/kernel/pid_max`` is the source of truth; ``PID_MAX_LIMIT`` (the kernel's hard
ceiling on 64-bit) is the fallback if it cannot be read, and ``PID_MAX_LIMIT + 1`` is above it.
"""

from __future__ import annotations

from pathlib import Path

# The kernel's hard ceiling for kernel.pid_max on 64-bit Linux (PID_MAX_LIMIT). No PID at or
# above this can ever be allocated, so fall back to it if the host's value cannot be read.
PID_MAX_LIMIT = 4194304


def pid_max() -> int:
    """The host's allocatable PID ceiling, or the kernel's hard ceiling if it is unreadable."""
    try:
        return int(Path("/proc/sys/kernel/pid_max").read_text())
    except (OSError, ValueError):
        return PID_MAX_LIMIT


def never_live_pid() -> int:
    """A PID the kernel can never hand out: one past the host's ``pid_max``."""
    return pid_max() + 1
