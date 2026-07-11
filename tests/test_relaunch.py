"""Bounded relaunch backstop (#631) — the pure state machine.

Caps a relaunch loop where a launched agent (e.g. a Claude background agent) exits instantly
every time. Time is driven by monkeypatching ``time.monotonic`` so the cooldown/window are
deterministic.
"""

from __future__ import annotations

import pytest

from agent_sessions import relaunch

_KEY = "claude:11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _clean_state():
    relaunch._state.clear()
    yield
    relaunch._state.clear()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(relaunch.time, "monotonic", lambda: now["t"])
    return now


def test_blocks_after_max_consecutive_instant_exits(clock):
    assert relaunch.blocked(_KEY) is False
    for _ in range(relaunch._MAX_INSTANT - 1):
        relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
        clock["t"] += 1.0
        assert relaunch.blocked(_KEY) is False  # not yet at the cap
    relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)  # the Mth instant exit
    assert relaunch.blocked(_KEY) is True


def test_master_still_alive_resets(clock):
    for _ in range(relaunch._MAX_INSTANT):
        relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    assert relaunch.blocked(_KEY) is True
    # A normal detach (agent kept running) clears the count.
    relaunch.note_exit(_KEY, lived_s=0.5, master_alive=True)
    assert relaunch.blocked(_KEY) is False


def test_long_lived_launch_resets(clock):
    for _ in range(relaunch._MAX_INSTANT):
        relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    assert relaunch.blocked(_KEY) is True
    # A launch that ran well past the instant-exit window is healthy → reset.
    relaunch.note_exit(_KEY, lived_s=relaunch._INSTANT_EXIT_S + 1.0, master_alive=False)
    assert relaunch.blocked(_KEY) is False


def test_cooldown_forgets_a_blocked_key(clock):
    for _ in range(relaunch._MAX_INSTANT):
        relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    assert relaunch.blocked(_KEY) is True
    # Much later, a retry gets a fresh start (the key is forgotten).
    clock["t"] += relaunch._RESET_AFTER_S + 1.0
    assert relaunch.blocked(_KEY) is False
    assert _KEY not in relaunch._state


def test_instant_exit_after_cooldown_starts_a_new_count(clock):
    relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    clock["t"] += relaunch._RESET_AFTER_S + 1.0  # window elapsed
    relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    assert relaunch._state[_KEY][0] == 1  # counter restarted, did not accumulate across the gap


def test_reset_clears_a_key(clock):
    for _ in range(relaunch._MAX_INSTANT):
        relaunch.note_exit(_KEY, lived_s=0.5, master_alive=False)
    assert relaunch.blocked(_KEY) is True
    relaunch.reset(_KEY)
    assert relaunch.blocked(_KEY) is False
