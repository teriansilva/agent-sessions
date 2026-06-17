"""Pulse background scan loop + prefs block (#441 Phase 3).

The load-bearing guarantees mirror the sibling loops (autosort/ai-review): the prefs
``auto_enabled`` flag gates every sweep (re-read live), a sweep skips when a manual scan holds
the single-flight, change-detection makes an unchanged in-window set a no-op (no scan, no LLM),
the env kill-switch keeps the task from sweeping at all, and the run loop honors the prefs
interval with backoff on consecutive crashed sweeps. The prefs block defaults/validation and
its bounds-in-sync with pulse.py are pinned too.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pytest

from agent_sessions import aitasks, metadata, prefs, pulse, pulse_loop


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str = "first message"
    archived: bool = False

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


@pytest.fixture(autouse=True)
def _reset_activity():
    aitasks.reset()
    yield
    aitasks.reset()


@pytest.fixture
def prefs_at_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_PULSE_CACHE", str(tmp_path / "pulse-cache.json"))
    return tmp_path


def _setup(monkeypatch, sessions, meta=None):
    monkeypatch.setattr(pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse.metadata, "load", lambda *a, **k: meta or {})
    monkeypatch.setattr(pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse.projects, "load", lambda *a, **k: {})


def _sweep(reg=None):
    return asyncio.run(pulse_loop.sweep(reg))


# ---- sweep: gating -------------------------------------------------------------------


def test_sweep_disabled_is_noop(prefs_at_tmp, monkeypatch):
    # auto_enabled defaults False → never builds cards, never scans.
    scanned = {"n": 0}

    async def _fake_scan(**kw):
        scanned["n"] += 1
        return {}

    monkeypatch.setattr(pulse, "run_scan", _fake_scan)
    assert _sweep()["skipped"] == "disabled"
    assert scanned["n"] == 0


def test_sweep_skips_when_scan_already_running(prefs_at_tmp, monkeypatch):
    prefs.set_pulse({"auto_enabled": True})

    async def scenario():
        async with aitasks.single_flight("pulse-scan", "manual"):
            return await pulse_loop.sweep()

    assert asyncio.run(scenario())["skipped"] == "locked"


def test_sweep_skips_when_unchanged(prefs_at_tmp, monkeypatch):
    # Populate the cache, then a second sweep over the SAME session set is a no-op: the input
    # fingerprint matches, so no scan (and at any depth, no endpoint call) happens.
    prefs.set_pulse({"auto_enabled": True})
    now = time.time()
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(review_fingerprint="fp")},
    )
    asyncio.run(pulse.run_scan())  # writes the cache (fast)
    calls = {"n": 0}
    real = pulse.run_scan

    async def counting(**kw):
        calls["n"] += 1
        return await real(**kw)

    monkeypatch.setattr(pulse, "run_scan", counting)
    assert _sweep()["skipped"] == "unchanged"
    assert calls["n"] == 0


def test_sweep_scans_when_enabled_and_changed(prefs_at_tmp, monkeypatch):
    # No cache yet → the change-detection short-circuit can't fire → a real scan runs.
    prefs.set_pulse({"auto_enabled": True})
    now = time.time()
    _setup(
        monkeypatch,
        [FakeSession("claude", "u", "/a", now - 100)],
        {"claude:u": metadata.SessionMeta(review_fingerprint="fp")},
    )
    report = _sweep()
    assert report["scanned"] is True
    assert report["cards"] == 1
    assert pulse.load_cache() is not None


# ---- run loop: kill-switch + interval/backoff ----------------------------------------


def test_kill_switch_run_never_sweeps(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PULSE_LOOP", "0")
    assert pulse_loop.loop_enabled() is False
    swept = []

    async def fake_sweep(reg):
        swept.append(reg)
        return {}

    monkeypatch.setattr(pulse_loop, "sweep", fake_sweep)
    asyncio.run(pulse_loop.run(None))  # returns immediately — never sweeps
    assert swept == []


def test_kill_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_PULSE_LOOP", raising=False)
    assert pulse_loop.loop_enabled() is True


class _StopLoop(Exception):
    pass


def test_run_honors_interval_and_backs_off(prefs_at_tmp, monkeypatch):
    prefs.set_pulse({"auto_enabled": True, "interval_minutes": 10})
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 4:
            raise _StopLoop

    monkeypatch.setattr(pulse_loop.asyncio, "sleep", fake_sleep)
    outcomes = iter([_StopLoop, _StopLoop, {"scanned": True, "cards": 0}])

    async def fake_sweep(reg):
        o = next(outcomes)
        if o is _StopLoop:
            raise RuntimeError("boom")
        return o

    monkeypatch.setattr(pulse_loop, "sweep", fake_sweep)
    with pytest.raises(_StopLoop):
        asyncio.run(pulse_loop.run(None))
    # 600s interval; ×2 then ×4 after consecutive crashed sweeps; a success resets the cadence.
    assert sleeps == [600, 1200, 2400, 600]


# ---- prefs block ---------------------------------------------------------------------


def test_pulse_defaults_and_validation(prefs_at_tmp):
    assert prefs.get_pulse() == {
        "auto_enabled": False,
        "interval_minutes": 30,
        "window_days": 3,
        "scan_depth": "fast",
    }
    assert (
        prefs.validate_pulse_patch(
            {"auto_enabled": True, "interval_minutes": 15, "window_days": 7, "scan_depth": "slow"}
        )
        is None
    )
    assert prefs.validate_pulse_patch({"auto_enabled": "yes"}) is not None
    assert prefs.validate_pulse_patch({"interval_minutes": 1}) is not None  # below floor
    assert prefs.validate_pulse_patch({"window_days": 99}) is not None  # above ceiling
    assert prefs.validate_pulse_patch({"scan_depth": "turbo"}) is not None  # unknown depth
    assert prefs.validate_pulse_patch({"bogus": 1}) is not None  # unknown key
    prefs.set_pulse({"auto_enabled": True, "scan_depth": "medium"})
    got = prefs.get_pulse()
    assert got["auto_enabled"] is True and got["scan_depth"] == "medium"
    assert got["window_days"] == 3  # untouched keys preserved


def test_public_pulse_reports_endpoint_readiness(prefs_at_tmp):
    assert prefs.public_pulse()["configured"] is False
    prefs.set_ai_review(
        {"enabled": True, "base_url": "https://ai.test/v1", "api_key": "k", "model": "m"}
    )
    pub = prefs.public_pulse()
    assert pub["configured"] is True  # mirrors the reused ai_review endpoint
    assert "api_key" not in pub


def test_pulse_bounds_constants_in_sync():
    # prefs.py keeps its own PULSE_* bounds (no import of pulse.py → no cycle); they MUST match
    # pulse.py's window/depth source of truth — this guard fails if one drifts.
    assert prefs.PULSE_WINDOW_MIN == pulse.WINDOW_DAYS_MIN
    assert prefs.PULSE_WINDOW_MAX == pulse.WINDOW_DAYS_MAX
    assert prefs.PULSE_DEPTHS == pulse.SCAN_DEPTHS
    assert prefs.PULSE_DEFAULT_DEPTH == pulse.DEFAULT_DEPTH
    assert prefs.get_pulse()["window_days"] == pulse.WINDOW_DAYS_DEFAULT
