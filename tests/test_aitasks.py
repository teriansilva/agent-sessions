"""Shared AI-task registry + per-kind single-flight (#441 Phase 1).

Guarantees pinned here: single-flight admits at most one task of a kind (so two Pulse scans
never overlap), `track` is non-exclusive (the existing review/auto-sort observability never
gains a 409), different kinds run independently, and a raising task still deregisters + records
a failed last-run.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import aitasks


@pytest.fixture(autouse=True)
def _reset():
    aitasks.reset()
    yield
    aitasks.reset()


def test_snapshot_starts_empty():
    assert aitasks.snapshot() == {"running": [], "last": {}}


def test_single_flight_blocks_same_kind():
    async def scenario():
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def hold():
            async with aitasks.single_flight("pulse-scan", "bg"):
                acquired.set()
                await release.wait()

        t = asyncio.create_task(hold())
        await acquired.wait()
        assert aitasks.is_running("pulse-scan")
        snap = aitasks.snapshot()
        assert [r["kind"] for r in snap["running"]] == ["pulse-scan"]
        assert snap["running"][0]["detail"] == "bg"

        # A second same-kind acquire is refused — the manual-scan 409 path.
        with pytest.raises(aitasks.AlreadyRunning):
            async with aitasks.single_flight("pulse-scan"):
                pass

        release.set()
        await t
        assert not aitasks.is_running("pulse-scan")
        assert aitasks.snapshot()["last"]["pulse-scan"]["ok"] is True

    asyncio.run(scenario())


def test_track_is_non_exclusive():
    async def scenario():
        up_a = asyncio.Event()
        up_b = asyncio.Event()
        release = asyncio.Event()

        async def one(ev):
            async with aitasks.track("ai-review", "sweep"):
                ev.set()
                await release.wait()

        t1 = asyncio.create_task(one(up_a))
        t2 = asyncio.create_task(one(up_b))
        await up_a.wait()
        await up_b.wait()
        running = aitasks.snapshot()["running"]
        assert len(running) == 2
        assert all(r["kind"] == "ai-review" for r in running)
        release.set()
        await asyncio.gather(t1, t2)
        assert not aitasks.is_running("ai-review")

    asyncio.run(scenario())


def test_different_kinds_run_concurrently():
    async def scenario():
        held = asyncio.Event()
        release = asyncio.Event()

        async def hold_pulse():
            async with aitasks.single_flight("pulse-scan"):
                held.set()
                await release.wait()

        t = asyncio.create_task(hold_pulse())
        await held.wait()
        # A different kind acquires fine while pulse-scan is in flight.
        async with aitasks.single_flight("auto-sort"):
            assert aitasks.is_running("auto-sort")
            assert aitasks.is_running("pulse-scan")
        release.set()
        await t

    asyncio.run(scenario())


def test_cleanup_and_failed_last_run_on_exception():
    async def scenario():
        with pytest.raises(ValueError):
            async with aitasks.track("pulse-scan", "boom"):
                raise ValueError("nope")
        assert not aitasks.is_running("pulse-scan")
        last = aitasks.snapshot()["last"]["pulse-scan"]
        assert last["ok"] is False
        assert last["detail"] == "boom"

    asyncio.run(scenario())


@pytest.mark.anyio
async def test_a_NON_EXCLUSIVE_track_never_consults_single_flight_state(monkeypatch):
    """Ordinary tracking short-circuits, and it always did.

    Hoisting the `is_running` call out of the `exclusive` branch — while fixing an unrelated
    arity problem — changed the contract for every non-exclusive task at once (review on #881).
    """
    called = []
    monkeypatch.setattr(aitasks, "is_running", lambda *a, **k: called.append(a) or False)
    async with aitasks.track("ordinary", "detail"):
        pass
    assert called == [], "a non-exclusive track consulted single-flight state"


@pytest.mark.anyio
async def test_a_LEGACY_one_argument_stub_still_works(monkeypatch):
    """The arity fix, asserted from the caller's side.

    A stub written against `is_running(kind)` — the shape this had for its whole life — must keep
    working for an unscoped exclusive task, or the break resurfaces somewhere unrelated.
    """
    seen = []

    def one_arg_stub(kind):  # deliberately NOT accepting a scope
        seen.append(kind)
        return False

    monkeypatch.setattr(aitasks, "is_running", one_arg_stub)
    async with aitasks.single_flight("legacy"):
        pass
    assert seen == ["legacy"]


def test_a_cancelled_run_is_not_recorded_as_a_failure():
    """#1171: a streamed Ask is cancelled whenever the operator starts a new conversation or
    leaves. That is a deliberate stop, not a failure: the activity panel must not say "failed",
    and "failing since" must not count it. It still leaves the running set."""
    import asyncio

    aitasks.reset()

    async def go():
        async def ok_run():
            async with aitasks.single_flight("pulse-chat", "ask"):
                pass

        await ok_run()
        before = dict(aitasks.snapshot()["last"]["pulse-chat"])

        async def hung():
            async with aitasks.single_flight("pulse-chat", "ask"):
                await asyncio.Event().wait()

        t = asyncio.ensure_future(hung())
        await asyncio.sleep(0)
        assert aitasks.is_running("pulse-chat")
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert not aitasks.is_running("pulse-chat")  # the gate is free
        return before, aitasks.snapshot()["last"]["pulse-chat"]

    before, after = asyncio.run(go())
    assert after == before  # untouched: still the last real (successful) run
    assert after["ok"] is True and after["consecutive_failures"] == 0
    aitasks.reset()


def test_a_real_failure_is_still_recorded():
    import asyncio

    aitasks.reset()

    async def go():
        with pytest.raises(RuntimeError):
            async with aitasks.single_flight("pulse-chat", "ask"):
                raise RuntimeError("endpoint down")

    asyncio.run(go())
    last = aitasks.snapshot()["last"]["pulse-chat"]
    assert last["ok"] is False and last["consecutive_failures"] == 1
    assert "endpoint down" in (last["error"] or "")
    aitasks.reset()
