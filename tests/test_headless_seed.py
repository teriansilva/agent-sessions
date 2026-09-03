"""The readiness gate for a session nobody is watching (#739).

The gate is the risk, not the write. `webterm._deliver_seed` already owns the claim/ack protocol
and the bounded serialized write, and this module reuses it verbatim — what is new is deciding
*when*, with no viewer to observe first paint.

**`DECSET 2004` is not readiness.** At least one engine arms bracketed paste in its preamble and
then discards stdin through a long cold start (measured: 91 bytes over 90 seconds on a cold codex).
Gating on 2004 alone pastes the brief into the void and reports success — an unseeded session that
looks seeded, which is the worst available outcome. So: armed AND painted AND quiet, and on
timeout nothing is written and the seed stays pending.
"""

from __future__ import annotations

import pytest

from agent_sessions import headless_seed, scrollback, session_input

KEY = "claude:11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    session_input.reset()
    scrollback._BUFFERS.clear()
    yield
    session_input.reset()
    scrollback._BUFFERS.clear()


def _paint(key: str, n: int) -> None:
    scrollback._BUFFERS[key] = bytearray(b"x" * n)


@pytest.mark.anyio
async def test_ARMED_ALONE_is_not_readiness(monkeypatch):
    """The measured failure this gate exists for: 2004 set, nothing painted."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    monkeypatch.setattr(scrollback, "first_paint_seen", lambda k: False)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False
    assert "first-paint" in why


@pytest.mark.anyio
async def test_PAINTED_ALONE_is_not_readiness(monkeypatch):
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: False)
    _paint(KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False
    assert "bracketed-paste" in why


@pytest.mark.anyio
async def test_all_three_together_ARE_readiness(monkeypatch):
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    ready, why = await headless_seed.wait_ready(KEY, timeout=2.0)
    assert ready is True and why == ""


@pytest.mark.anyio
async def test_a_screen_mutation_IN_FLIGHT_is_not_quiet(monkeypatch):
    """The seqlock's odd interval means an append is mid-flight; a quiet reading taken then is not
    quiet at all. `screen_is_stable` is the same guard the write fence uses."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(session_input, "screen_is_stable", lambda k: False)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
    assert ready is False and "quiet" in why


@pytest.mark.anyio
async def test_the_reason_NAMES_which_signal_never_came_true(monkeypatch):
    """ "The brief was never delivered" is useless without which of the three failed."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: False)
    monkeypatch.setattr(scrollback, "first_paint_seen", lambda k: False)
    ready, why = await headless_seed.wait_ready(KEY, timeout=0.3)
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
    _paint(KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(headless_seed, "SETTLE_S", 0.0)
    monkeypatch.setattr(session_input, "borrow_writer", lambda k: None)
    delivered, why = await headless_seed.deliver(KEY, KEY, timeout=2.0)
    assert delivered is False
    assert "nothing owns" in why


@pytest.mark.anyio
async def test_a_DEAD_writer_is_told_apart_from_an_absent_one(monkeypatch):
    """`borrow_writer` raises for a registration whose fd is already closed. That is a BROKEN
    owner, not no owner, and the two send an operator to different places."""
    monkeypatch.setattr(scrollback, "has_mode", lambda k, m: True)
    _paint(KEY, headless_seed.FIRST_PAINT_BYTES + 1)
    monkeypatch.setattr(headless_seed, "QUIET_S", 0.0)
    monkeypatch.setattr(headless_seed, "SETTLE_S", 0.0)

    def boom(k):
        raise OSError(9, "bad fd")

    monkeypatch.setattr(session_input, "borrow_writer", boom)
    delivered, why = await headless_seed.deliver(KEY, KEY, timeout=2.0)
    assert delivered is False and "writer is dead" in why


def test_the_gate_uses_WEBTERM_S_OWN_constants():
    """Two copies of a readiness threshold is two answers to "is it ready", and the interesting
    failure is always the one where they disagree."""
    from agent_sessions import webterm

    assert headless_seed.FIRST_PAINT_BYTES == webterm._SEED_FIRST_PAINT_BYTES
    assert headless_seed.QUIET_S == webterm._SEED_QUIET_S
    assert headless_seed.READY_TIMEOUT_S == webterm._SEED_READY_TIMEOUT_S
    assert headless_seed.SETTLE_S == webterm._SEED_SETTLE_S
