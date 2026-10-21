"""The one bound that keeps a group signal from becoming a host outage (#924).

`os.killpg(1, sig)` is `kill(-1, sig)`. A fake `Popen` carrying `pid = 1` reached a cleanup path
and killed every process this user owned — seven times in one morning. These tests exist so the
guard cannot be removed as redundant, and so "refuse everything" cannot pass for a fix.

`os.killpg` is stubbed throughout: a test that could reach the kernel with a bad id is the very
accident being guarded against.
"""

from __future__ import annotations

import os
import signal

import pytest

from agent_sessions import procgroup


@pytest.fixture
def sent(monkeypatch):
    """Record what would have reached the kernel, without anything reaching it."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    return calls


@pytest.mark.parametrize(
    ("pgid", "why"),
    [
        (1, "killpg(1) is kill(-1) — every process this user owns"),
        (0, "killpg(0) is the caller's OWN group, which includes this process"),
        (-1, "a negative id is not a group we could have created"),
        (None, "an absent id is not a licence to signal something"),
        (True, "isinstance(True, int) is True in Python — the prefs validator's trap"),
        ("1", "a string is not an id"),
        (object(), "a Mock-shaped object is exactly what a test fake supplies"),
    ],
)
def test_a_CATASTROPHIC_or_unusable_group_id_never_reaches_the_kernel(sent, pgid, why):
    assert procgroup.killpg(pgid) is False, why
    assert sent == [], f"killpg reached the kernel with {pgid!r} — {why}"


def test_a_LEGITIMATE_group_is_still_signalled(sent):
    """The other half, so the guard cannot be 'refuse everything'.

    Without this, deleting the body of `killpg` would pass every test above.
    """
    assert procgroup.killpg(2**20, signal.SIGTERM) is True
    assert sent == [(2**20, signal.SIGTERM)]


def test_the_default_signal_is_SIGKILL(sent):
    procgroup.killpg(4242)
    assert sent == [(4242, signal.SIGKILL)]


def test_a_group_that_is_already_gone_is_not_an_error(monkeypatch):
    """The normal case: the probe exited first. `ProcessLookupError` IS the answer we wanted."""

    def gone(pgid, sig):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(os, "killpg", gone)
    assert procgroup.killpg(4242) is False  # not delivered…
    # …and it did not raise, which is what every caller depends on.


def test_a_PERMISSION_error_is_reported_as_not_delivered(monkeypatch):
    def denied(pgid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", denied)
    assert procgroup.killpg(4242) is False


def test_MIN_GROUP_is_two_and_the_boundary_is_inclusive(sent):
    """Pinned as a value, because the whole defect is an off-by-one in the dangerous direction."""
    assert procgroup.MIN_GROUP == 2
    assert procgroup.killpg(procgroup.MIN_GROUP - 1) is False
    assert procgroup.killpg(procgroup.MIN_GROUP) is True
    assert sent == [(2, signal.SIGKILL)]
