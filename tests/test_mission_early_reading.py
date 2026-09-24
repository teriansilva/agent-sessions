"""The first supervisor reading does not wait a sweep (#1064, Phase 1).

A mission that has just gone `running` is the one the operator is most likely watching, and the
ordinary cadence left it silent for up to `INTERVAL_S` (five minutes). The launch now asks for ONE
early reading. These tests pin the three properties that make that safe to add:

* it is served **when due**, not at the next sweep boundary — the loop is woken, not polled;
* it costs **at most one model call per launch**: an attempt skipped before the model is re-armed
  (a skip is free), a real reading ends it, and the attempts are capped;
* it changes **nothing else** — the sweep cadence is untouched, a disabled supervisor gets no early
  reading, and a pass that never reached a session is not retried.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import mission_supervisor_loop as loop

D = loop.EARLY_READING_DELAY_S


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    loop._early.clear()
    monkeypatch.setattr(loop, "_wake", None)
    monkeypatch.setattr(loop, "_wake_loop", None)
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    yield
    loop._early.clear()


def _pass(*sessions: dict, skipped: str | None = None) -> dict:
    out: dict = {"per_session": list(sessions)}
    if skipped:
        out["skipped"] = skipped
    return out


READ = {"assessment": "on_track"}
UNCHANGED = {
    "assessment": None,
    "skipped_model": "the session has not changed since the last recap",
}
NO_ENDPOINT = {"assessment": None, "skipped_model": "no AI endpoint is configured"}


def _fake_run_pass(monkeypatch, *results):
    calls: list[str] = []
    it = iter(results)

    async def run_pass(mid, registry=None):
        calls.append(mid)
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    return calls


# ---- scheduling ------------------------------------------------------------------------------


def test_a_request_is_due_after_the_delay_and_not_before():
    loop.request_early_pass("msn_a", now=1000.0)
    assert loop.due_early(1000.0 + D - 1) == []
    assert loop.due_early(1000.0 + D) == ["msn_a"]
    assert loop.next_early_due() == 1000.0 + D


def test_a_second_request_restarts_the_schedule_rather_than_stacking():
    loop.request_early_pass("msn_a", now=1000.0)
    loop.request_early_pass("msn_a", now=1010.0)
    assert list(loop._early) == ["msn_a"]
    assert loop.next_early_due() == 1010.0 + D


def test_an_empty_id_is_ignored_and_nothing_raises_without_a_loop():
    loop.request_early_pass("")
    assert loop._early == {}
    loop.request_early_pass("msn_a")  # no running loop, no wake event: recorded, never raised
    assert "msn_a" in loop._early


# ---- one reading, bounded attempts, at most one model call -----------------------------------


def test_a_reading_ends_it_after_one_pass(monkeypatch):
    calls = _fake_run_pass(monkeypatch, _pass(READ))
    loop.request_early_pass("msn_a", now=0.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"msn_a": "read"}
    assert calls == ["msn_a"] and loop._early == {}


def test_an_attempt_skipped_before_the_model_is_re_armed_then_capped(monkeypatch):
    """A skip is free — the fingerprint gate runs before the model — so it may be retried; the cap
    bounds the WAITING, and the early path still makes at most one model call."""
    n = loop.EARLY_READING_ATTEMPTS
    calls = _fake_run_pass(monkeypatch, *[_pass(UNCHANGED)] * n)
    loop.request_early_pass("msn_a", now=0.0)
    t = D
    for i in range(n):
        report = asyncio.run(loop.run_due_early(now=t))
        expected = "gave up" if i == n - 1 else "re-armed"
        assert report == {"msn_a": expected}, (i, report)
        t += D
    assert len(calls) == n and loop._early == {}


def test_a_re_armed_attempt_that_then_reads_stops_there(monkeypatch):
    calls = _fake_run_pass(monkeypatch, _pass(UNCHANGED), _pass(READ))
    loop.request_early_pass("msn_a", now=0.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"msn_a": "re-armed"}
    assert loop.due_early(D + D - 1) == [], "re-armed at the same spacing, not immediately"
    assert asyncio.run(loop.run_due_early(now=2 * D)) == {"msn_a": "read"}
    assert len(calls) == 2 and loop._early == {}


@pytest.mark.parametrize(
    "result",
    [
        _pass(NO_ENDPOINT),  # retrying cannot configure the model
        _pass(skipped="mission is review"),  # the pass never reached a session
        _pass(skipped="the mission holds no session"),
        {"skipped": "unknown mission"},
    ],
)
def test_nothing_time_cannot_cure_is_retried(monkeypatch, result):
    calls = _fake_run_pass(monkeypatch, result)
    loop.request_early_pass("msn_a", now=0.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"msn_a": "gave up"}
    assert len(calls) == 1 and loop._early == {}


def test_a_pass_that_raises_is_an_attempt_and_never_escapes(monkeypatch):
    calls = _fake_run_pass(monkeypatch, RuntimeError("store locked"))
    loop.request_early_pass("msn_a", now=0.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"msn_a": "gave up"}
    assert calls == ["msn_a"]


def test_a_disabled_supervisor_gets_no_early_reading_and_drops_the_requests(monkeypatch):
    calls = _fake_run_pass(monkeypatch)
    monkeypatch.setattr(loop, "_enabled", lambda: False)
    loop.request_early_pass("msn_a", now=0.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"skipped": "disabled"}
    assert calls == [] and loop._early == {}


def test_only_due_missions_run(monkeypatch):
    calls = _fake_run_pass(monkeypatch, _pass(READ))
    loop.request_early_pass("msn_due", now=0.0)
    loop.request_early_pass("msn_later", now=100.0)
    assert asyncio.run(loop.run_due_early(now=D)) == {"msn_due": "read"}
    assert calls == ["msn_due"] and list(loop._early) == ["msn_later"]


# ---- the loop: woken when due, sweep cadence untouched ---------------------------------------


def test_the_loop_serves_an_early_reading_long_before_the_sweep_and_does_not_sweep(monkeypatch):
    """The issue's acceptance test, end to end through `run()`.

    With the sweep interval far away and the early delay short, a request made AFTER the loop has
    gone to sleep must still be served — which is only possible if the request wakes it — and the
    sweep must not have run: the cadence is exactly what it was.
    """
    monkeypatch.setattr(loop, "INTERVAL_S", 3600.0)
    monkeypatch.setattr(loop, "EARLY_READING_DELAY_S", 0.05)

    async def no_reconcile():
        return None

    monkeypatch.setattr(loop, "_reconcile_delivered", no_reconcile)
    swept: list[int] = []

    async def sweep(registry=None):
        swept.append(1)
        return {}

    monkeypatch.setattr(loop, "sweep", sweep)
    read: list[str] = []

    async def run_pass(mid, registry=None):
        read.append(mid)
        return _pass(READ)

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)

    async def drive():
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.05)  # the loop is now asleep on an hour-long timeout
        assert loop._wake is not None, "the loop did not install its wake event"
        loop.request_early_pass("msn_new")
        for _ in range(100):
            if read:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert read == ["msn_new"], "the early reading was not served while the sweep was an hour away"
    assert swept == [], "the sweep ran early — the cadence must be untouched"
    assert loop._wake is None, "the wake event outlived the loop"


def test_switching_supervision_off_mid_batch_stops_the_early_judge_calls(monkeypatch):
    """The early path re-checks the switch before judging, like the sweep's judge phase (#1097)."""
    enabled = {"on": True}
    monkeypatch.setattr(loop, "_enabled", lambda: enabled["on"])

    async def run_pass(mid, registry=None):
        enabled["on"] = False  # the operator switches supervision off while this reading runs
        return _pass(READ)

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    judged: list = []

    async def judge_batch(ids, budget):
        judged.append(list(ids))
        return {}

    monkeypatch.setattr(loop.mission_judge, "judge_batch", judge_batch)
    loop.request_early_pass("msn_a", now=0.0)
    asyncio.run(loop.run_due_early(now=D))
    assert judged == []
