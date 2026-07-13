"""Bounded relaunch backstop (#631).

The terminal route relaunches a session's agent whenever its ``dtach`` master is gone and the
single-writer lock is free (``sessions.open_action`` → ``LAUNCH``). If the launched agent EXITS
INSTANTLY every time — e.g. a Claude background agent, whose ``claude --resume`` prints
"currently running as a background agent" and quits — the master dies at once, the browser's
close-code retry reconnects, and the route relaunches, forever (measured 17 attaches in 3
minutes, one scrollback line appended each cycle).

This backstop caps that loop: after a few consecutive instant exits for one physical session
key, the route stops relaunching and closes on a TERMINAL close code so the client's retry loop
ends. It is INDEPENDENT of the transcript-ownership probe — a safety net for ANY instant-exit
loop, not only background agents, and it deliberately does not touch the ``termSocket`` close-code
taxonomy: ``4409`` (BUSY) and ``4502`` (transient start) stay retryable; only this backstop makes
the route emit an already-terminal code, and only after repeated instant exits.

State is per physical session key, in-memory (the loop is a single-instance, event-loop
phenomenon). A launch that lives past the instant-exit window, or whose master is still alive
when the viewer leaves (a normal detach), RESETS the key. A key with no instant exit for the
cooldown is forgotten, so a much-later retry always gets a fresh chance.
"""

from __future__ import annotations

import time

# A LAUNCH whose master is gone within this many seconds of starting is an "instant exit".
_INSTANT_EXIT_S = 8.0
# This many consecutive instant exits ⇒ block further relaunches (close terminal instead).
_MAX_INSTANT = 3
# Forget a key after this long with no instant exit, so a later retry starts clean.
_RESET_AFTER_S = 60.0

# phys_key → (consecutive_instant_exit_count, last_instant_exit_monotonic)
_state: dict[str, tuple[int, float]] = {}


def blocked(key: str) -> bool:
    """True if ``key`` has hit the consecutive-instant-exit cap and must not be relaunched.

    A key whose last instant exit is older than the cooldown is forgotten here (a later retry
    is a fresh start), so ``blocked`` never wedges a key permanently."""
    entry = _state.get(key)
    if entry is None:
        return False
    count, last = entry
    if time.monotonic() - last > _RESET_AFTER_S:
        _state.pop(key, None)  # cooldown elapsed → fresh start
        return False
    return count >= _MAX_INSTANT


def note_exit(key: str, lived_s: float, *, master_alive: bool) -> None:
    """Record how a LAUNCH ended. A still-alive master (a normal detach — the agent keeps
    running, the viewer just left) or a launch that lived past the instant-exit window RESETS
    the key; an instant master exit increments its consecutive count."""
    if master_alive or lived_s >= _INSTANT_EXIT_S:
        _state.pop(key, None)
        return
    now = time.monotonic()
    prev = _state.get(key)
    count = prev[0] + 1 if prev is not None and now - prev[1] <= _RESET_AFTER_S else 1
    _state[key] = (count, now)


def reset(key: str) -> None:
    """Forget a key (explicit clear / test seam)."""
    _state.pop(key, None)
