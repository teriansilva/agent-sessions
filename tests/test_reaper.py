"""Idle-session reaper (#279): selection logic + safety posture.

The load-bearing guarantee is that the reaper only ever targets STALE sessions (detached + idle past
the TTL) and NEVER an active one (attached, or recently-active), is opt-in, and defaults to dry-run
(observes, kills nothing). These pin that without spawning real PTYs.
"""

from __future__ import annotations

import asyncio

from agent_sessions import reaper


def _row(key, *, attached=False, last_output_at=None, engine="claude", sid=None):
    return {
        "id": key,
        "engine": engine,
        "sid": sid or key.split(":", 1)[-1],
        "attached": attached,
        "last_output_at": last_output_at,
    }


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", raising=False)
    assert reaper.enabled() is False
    assert reaper.idle_ttl() == 0


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_DRY_RUN", raising=False)
    assert reaper.dry_run() is True
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    assert reaper.dry_run() is False


def test_is_stale_only_detached_and_idle():
    now = 1000.0
    ttl = 600
    assert reaper.is_stale(True, 0.0, now, ttl) is False  # attached → never stale, ever
    assert reaper.is_stale(False, now - 10, now, ttl) is False  # recently active → not stale
    assert reaper.is_stale(False, None, now, ttl) is False  # no signal at all → don't risk it
    assert reaper.is_stale(False, now - 999, now, ttl) is True  # detached + idle past TTL → STALE
    assert reaper.is_stale(False, now - ttl, now, ttl) is True  # exactly at threshold (>=)


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def snapshot(self):
        return list(self._rows)


def test_sweep_selects_only_stale_and_honors_exempt(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "600")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")  # observe only
    monkeypatch.setenv("AGENT_SESSIONS_REAP_EXEMPT", "claude:pinned")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])  # hermetic: no transcript mtimes
    now = 10_000.0
    reg = _FakeRegistry(
        [
            _row("claude:active", attached=True, last_output_at=0.0),  # attached → skip
            _row("claude:fresh", last_output_at=now - 5),  # recent → skip
            _row("claude:pinned", last_output_at=now - 99_999),  # exempt → skip
            _row("claude:stale1", last_output_at=now - 5000),  # STALE
            _row("claude:stale2", last_output_at=now - 700),  # STALE
            _row("claude:noidle", last_output_at=None),  # no signal → skip
        ]
    )

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert set(selected) == {"claude:stale1", "claude:stale2"}


def test_sweep_kills_nothing_in_dry_run(monkeypatch):
    # Dry-run must not invoke the teardown path at all (no PID lookup, no kill, no mirror teardown).
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    calls = {"find": 0, "signal": 0}
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: calls.__setitem__("find", 1))
    monkeypatch.setattr(reaper, "_signal_tree", lambda p, s: calls.__setitem__("signal", 1))
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stale", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert selected == ["claude:stale"]  # still SELECTED + logged...
    assert calls == {"find": 0, "signal": 0}  # ...but nothing was torn down


def test_transcript_mtime_is_the_restart_proof_activity_floor(monkeypatch):
    # The accumulation case: a session silent since before this process started has last_output_at
    # None, but its transcript mtime is old → it IS stale and gets reaped. Conversely, an OLD
    # last_output_at but a RECENT transcript mtime (active again post-restart) is NOT stale.
    from agent_sessions.scanner import Session

    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "600")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "1")
    now = 100_000.0
    scanned = [
        Session("claude", "silentold", "/x", now - 5000, "hi", False),  # idle 5000s → stale
        Session("claude", "activenow", "/x", now - 5, "hi", False),  # active 5s ago → NOT stale
    ]
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: scanned)
    reg = _FakeRegistry(
        [
            _row("claude:silentold", last_output_at=None),  # never observed live this process
            _row("claude:activenow", last_output_at=now - 9000),  # stale by live signal alone...
        ]
    )

    async def go():
        return await reaper.sweep(reg, now=now)

    # silentold reaped via mtime; activenow saved because its transcript mtime is recent.
    assert asyncio.run(go()) == ["claude:silentold"]


def test_sweep_disabled_returns_empty(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", raising=False)
    reg = _FakeRegistry([_row("claude:x", last_output_at=0.0)])

    async def go():
        return await reaper.sweep(reg, now=1.0)

    assert asyncio.run(go()) == []


def test_real_reap_signals_tree_and_frees_mirror(monkeypatch):
    # Flag off dry-run: the stale session's master PID is looked up, its process group SIGTERM'd,
    # and the VT mirror freed. History (transcript/ring) is untouched — the teardown calls neither
    # clear_scrollback nor _drop_buffer. Exits on SIGTERM (no SIGKILL escalation).
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    monkeypatch.setattr(reaper, "_REAP_GRACE_S", 0)  # don't actually sleep in the test
    sigs = []
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: 4242)
    monkeypatch.setattr(reaper, "_signal_tree", lambda pid, sig: sigs.append((pid, sig)))
    monkeypatch.setattr(reaper, "_alive", lambda pid: False)  # died on SIGTERM
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stale", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    selected = asyncio.run(go())
    assert selected == ["claude:stale"]
    assert sigs == [(4242, reaper.signal.SIGTERM)]  # SIGTERM only — it exited


def test_real_reap_escalates_to_sigkill_when_surviving(monkeypatch):
    # A master that ignores SIGTERM (stays alive past the grace) is escalated to SIGKILL so a reap
    # always frees the session rather than re-logging the same survivor every sweep.
    monkeypatch.setenv("AGENT_SESSIONS_REAP_IDLE_SECONDS", "100")
    monkeypatch.setenv("AGENT_SESSIONS_REAP_DRY_RUN", "0")
    monkeypatch.setattr(reaper.engines, "scan_all", lambda: [])
    monkeypatch.setattr(reaper, "_REAP_GRACE_S", 0)
    sigs = []
    monkeypatch.setattr(reaper, "_find_master_pid", lambda e, s: 99)
    monkeypatch.setattr(reaper, "_signal_tree", lambda pid, sig: sigs.append((pid, sig)))
    monkeypatch.setattr(reaper, "_alive", lambda pid: True)  # survived SIGTERM
    now = 5000.0
    reg = _FakeRegistry([_row("claude:stubborn", last_output_at=now - 4000)])

    async def go():
        return await reaper.sweep(reg, now=now)

    asyncio.run(go())
    assert sigs == [(99, reaper.signal.SIGTERM), (99, reaper.signal.SIGKILL)]
