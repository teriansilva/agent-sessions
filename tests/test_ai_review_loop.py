"""Periodic AI review loop (#356 Phase 2) against a MOCKED endpoint (httpx.MockTransport
— no real network in CI).

The load-bearing guarantees: change-detection runs BEFORE any endpoint call (an unchanged
session never costs a request), excluded/archived sessions are skipped, the prefs
``enabled`` flag is re-read per sweep, the env kill-switch keeps the task from sweeping at
all, a failed review persists nothing (so the session is retried), endpoint calls are
capped + serialized per sweep, and the run loop honors the prefs interval with backoff on
consecutive all-failure sweeps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest

from agent_sessions import ai_review_loop, metadata, prefs, review, webterm

SECRET = "sk-review-secret-31337"  # noqa: S105 — test fixture value
BASE = "https://ai.test/v1"
SID = "claude:11111111-1111-1111-1111-111111111111"  # exists in fake_jsonl
LIVE_ONLY_SID = "claude:99999999-9999-9999-9999-999999999999"  # no jsonl on disk


@pytest.fixture(autouse=True)
def _reset_review(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    review._models_cache.clear()
    yield
    review._models_cache.clear()


@pytest.fixture
def ai_prefs(tmp_home, monkeypatch):
    """Point prefs at tmp and store a configured + ENABLED ai_review block."""
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review(
        {"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "test-model"}
    )
    return tmp_home


def _chat_transport(calls: list, *, status=200, recap_ok=True):
    summary = {
        "summary": "Editing tests",
        "title": "Fix the tests",
        "intervention_required": False,
        "reason": "",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Each review makes TWO calls (#481): the tail summary, then the whole-session recap —
        # answered differently (keyed on the recap prompt) so the loop genuinely persists a recap.
        # ``recap_ok=False`` makes ONLY the recap call return a shape-invalid body, so the summary
        # persists but the recap fails (fail-soft) — exercising the recap retry/backfill path.
        payload = json.loads(request.content)
        is_recap = "returning to a coding-agent session" in payload["messages"][0]["content"]
        if is_recap:
            body = {"recap": "Cloned repo.\nFixed tests."} if recap_ok else {"no_recap_key": True}
        else:
            body = summary
        return httpx.Response(
            status,
            json={"choices": [{"message": {"content": json.dumps(body)}}]},
        )

    return httpx.MockTransport(handler)


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def snapshot(self):
        return list(self._rows)


def _row(key, *, engine="claude"):
    return {"id": key, "engine": engine, "sid": key.split(":", 1)[-1], "attached": False}


def _sweep(reg):
    return asyncio.run(ai_review_loop.sweep(reg))


# ---- sweep: selection + change detection ---------------------------------------------


def test_sweep_reviews_changed_session(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    reviewed, failures = _sweep(_FakeRegistry([_row(SID)]))
    assert reviewed == [SID]
    assert failures == 0
    assert len(calls) == 2  # one summary call + one recap call (#481)
    m = metadata.get(SID)
    assert m.ai_summary == "Editing tests"
    assert m.ai_recap == "Cloned repo.\nFixed tests."
    assert m.reviewed_at is not None
    assert m.review_fingerprint
    assert m.recap_fingerprint


def test_sweep_skips_unchanged_without_calling_endpoint(ai_prefs, fake_jsonl, monkeypatch):
    # The issue's change-detection requirement: across two sweeps with unchanged content the
    # session is reviewed EXACTLY once — the second pass is fingerprint-only, local. One review
    # is two calls (#481): the summary + the recap.
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    reg = _FakeRegistry([_row(SID)])
    assert _sweep(reg) == ([SID], 0)
    assert _sweep(reg) == ([], 0)  # unchanged → skipped before any network I/O
    assert len(calls) == 2


def test_sweep_reviews_again_when_content_changes(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    reg = _FakeRegistry([_row(SID)])
    assert _sweep(reg) == ([SID], 0)
    webterm._buffer_append(SID, b"fresh terminal output moves the fingerprint\r\n")
    assert _sweep(reg) == ([SID], 0)
    assert len(calls) == 4  # two reviews × (summary + recap) (#481)


def test_sweep_retries_recap_when_summary_fresh_but_recap_failed(ai_prefs, fake_jsonl, monkeypatch):
    # #481 (Hermes #482): summary succeeds but the recap call fails → review_fingerprint advances
    # while recap_fingerprint stays empty. The next sweep, with UNCHANGED content, must STILL
    # re-review to retry the recap — it must NOT skip on the summary fingerprint alone (otherwise
    # a failed/absent recap is stranded until the tail changes; same gap as legacy summary-only
    # sessions needing recap backfill).
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls, recap_ok=False))
    reg = _FakeRegistry([_row(SID)])
    assert _sweep(reg) == ([SID], 0)
    m = metadata.get(SID)
    assert m.review_fingerprint  # summary persisted
    assert m.ai_recap == ""  # recap failed → not persisted
    assert not m.recap_fingerprint
    # Unchanged content, but the recap is still stale → re-reviewed, not skipped.
    assert _sweep(reg) == ([SID], 0)


def test_sweep_skips_excluded(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    metadata.patch(SID, review_excluded=True)
    assert _sweep(_FakeRegistry([_row(SID)])) == ([], 0)
    assert calls == []


def test_sweep_skips_archived(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    metadata.patch(SID, archived=True)
    assert _sweep(_FakeRegistry([_row(SID)])) == ([], 0)
    assert calls == []


def test_sweep_skips_unreviewable_session(ai_prefs, fake_jsonl, monkeypatch):
    # No transcript + no live output → gather fails soft, no endpoint call, no crash.
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    reg = _FakeRegistry([_row("claude:88888888-8888-8888-8888-888888888888")])
    assert _sweep(reg) == ([], 0)
    assert calls == []


# ---- sweep: gating -------------------------------------------------------------------


def test_sweep_disabled_flag_makes_no_calls(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    prefs.set_ai_review({"enabled": False})
    assert _sweep(_FakeRegistry([_row(SID)])) == ([], 0)
    assert calls == []
    # Re-enabling takes effect at the very next sweep — no restart, prefs re-read live.
    prefs.set_ai_review({"enabled": True})
    assert _sweep(_FakeRegistry([_row(SID)])) == ([SID], 0)
    assert len(calls) == 2  # summary + recap (#481)


def test_sweep_unconfigured_makes_no_calls(tmp_home, fake_jsonl, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_home / "prefs.json"))
    prefs.set_ai_review({"enabled": True, "base_url": BASE})  # no API key stored
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    assert _sweep(_FakeRegistry([_row(SID)])) == ([], 0)
    assert calls == []


def test_kill_switch_run_never_sweeps(ai_prefs, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_AI_REVIEW_LOOP", "0")
    assert ai_review_loop.loop_enabled() is False
    swept = []

    async def fake_sweep(reg):
        swept.append(reg)
        return [], 0

    monkeypatch.setattr(ai_review_loop, "sweep", fake_sweep)
    # run() returns immediately — the task is effectively never started.
    asyncio.run(ai_review_loop.run(_FakeRegistry([_row(SID)])))
    assert swept == []


def test_kill_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("AGENT_SESSIONS_AI_REVIEW_LOOP", raising=False)
    assert ai_review_loop.loop_enabled() is True


# ---- sweep: failure semantics --------------------------------------------------------


def test_failed_review_persists_nothing_and_is_retried(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls, status=500))
    reg = _FakeRegistry([_row(SID)])
    assert _sweep(reg) == ([], 1)
    m = metadata.get(SID)
    assert m.reviewed_at is None  # failure never masquerades as a fresh review
    assert not m.review_fingerprint  # ...so the session stays "changed"
    assert _sweep(reg) == ([], 1)  # and IS retried next sweep
    assert len(calls) == 2


# ---- sweep: cap + serialization ------------------------------------------------------


def test_sweep_caps_and_spaces_endpoint_calls(ai_prefs, fake_jsonl, monkeypatch):
    # 5 changed sessions, cap 4: exactly SWEEP_CAP SESSIONS reviewed (each = 2 endpoint calls,
    # summary + recap, #481), reviewed strictly one session at a time with CALL_SPACING_S between
    # sessions — a sweep can't stampede the endpoint.
    keys = [f"claude:aaaaaaa{i}-0000-0000-0000-00000000000{i}" for i in range(5)]
    for k in keys:
        webterm._buffer_append(k, f"agent output for {k}\r\n".encode())
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    sleeps = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(ai_review_loop.asyncio, "sleep", recording_sleep)
    reviewed, failures = _sweep(_FakeRegistry([_row(k) for k in keys]))
    assert len(reviewed) == ai_review_loop.SWEEP_CAP
    assert len(calls) == 2 * ai_review_loop.SWEEP_CAP  # each review = summary + recap (#481)
    assert failures == 0
    # Spacing between consecutive calls only (none before the first).
    spacing = [d for d in sleeps if d == ai_review_loop.CALL_SPACING_S]
    assert len(spacing) == ai_review_loop.SWEEP_CAP - 1


# ---- run loop: interval + backoff ----------------------------------------------------


class _StopLoop(Exception):
    pass


def test_run_honors_interval_and_backs_off_on_failures(ai_prefs, monkeypatch):
    prefs.set_ai_review({"interval_minutes": 2})
    timeouts = []

    # The interval is now the timeout on the wake-wait (so a kick can cut it short). With no
    # kick, the wait always times out → a sweep runs; we record the timeout to assert backoff.
    async def fake_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        if asyncio.iscoroutine(awaitable):
            awaitable.close()  # we never actually await the event in this test
        if len(timeouts) >= 4:
            raise _StopLoop
        raise TimeoutError  # interval elapsed, no kick → proceed to sweep

    monkeypatch.setattr(ai_review_loop.asyncio, "wait_for", fake_wait_for)
    outcomes = iter([([], 1), ([], 1), ([SID], 0)])

    async def fake_sweep(reg):
        return next(outcomes)

    monkeypatch.setattr(ai_review_loop, "sweep", fake_sweep)
    with pytest.raises(_StopLoop):
        asyncio.run(ai_review_loop.run(_FakeRegistry([])))
    # 120s prefs interval; ×2 then ×4 after consecutive all-failure sweeps; a successful
    # sweep resets the cadence.
    assert timeouts == [120, 240, 480, 120]


# ---- run loop: early wake on new-session kick (#413) ---------------------------------


def test_request_review_soon_is_noop_before_loop_armed(monkeypatch):
    # Before run() arms the loop (or under the kill-switch), a kick must be a harmless no-op.
    monkeypatch.setattr(ai_review_loop, "_wake", None)
    ai_review_loop.request_review_soon()  # must not raise


def test_kick_wakes_loop_before_the_interval(ai_prefs, monkeypatch):
    # A real (minutes-long) interval means a sweep this quickly can ONLY come from the kick.
    monkeypatch.setattr(ai_review_loop, "KICK_GRACE_S", 0.0)

    async def scenario():
        swept = []
        done = asyncio.Event()

        async def fake_sweep(reg):
            swept.append(reg)
            done.set()
            return [], 0

        monkeypatch.setattr(ai_review_loop, "sweep", fake_sweep)
        task = asyncio.create_task(ai_review_loop.run(_FakeRegistry([_row(SID)])))
        await asyncio.sleep(0.05)  # let run() arm the wake event and start waiting
        assert swept == []  # interval is minutes away — nothing yet
        ai_review_loop.request_review_soon()  # the new-session kick
        await asyncio.wait_for(done.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert len(swept) == 1

    asyncio.run(scenario())


# ---- #611: fair sweep order + the first-send kick ---------------------------------------


def test_never_reviewed_session_is_not_starved_by_noisy_older_ones(
    ai_prefs, fake_jsonl, monkeypatch
):
    """`snapshot()` is insertion-ordered and SWEEP_CAP bounds ATTEMPTS. An agent that is
    actively working changes its frame every sweep, so in insertion order the first SWEEP_CAP
    busy sessions consumed the whole budget forever and anything behind them was starved —
    a brand-new session sat at `(untitled)` indefinitely. Ordering by `reviewed_at` makes the
    cap a fair rotation."""
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))

    noisy = [f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-1111-1111-1111-111111111111" for i in range(1, 6)]
    for i, key in enumerate(noisy):
        webterm._buffer_append(key, f"busy agent {i} output\r\n".encode())
        # Already reviewed a moment ago, and still changing (their frames differ every sweep).
        metadata.patch(key, reviewed_at=1000.0 + i, review_fingerprint="stale")
    assert len(noisy) > ai_review_loop.SWEEP_CAP

    fresh = "claude:99999999-9999-9999-9999-999999999999"
    webterm._buffer_append(fresh, b"the user just sent their first message\r\n")
    assert metadata.get(fresh).reviewed_at is None

    # Registry order puts the never-reviewed session LAST, exactly as insertion order would.
    reviewed, failures = _sweep(_FakeRegistry([_row(k) for k in [*noisy, fresh]]))
    assert failures == 0
    assert fresh in reviewed, "a never-reviewed session must win the first sweep, not starve"
    assert len(reviewed) == ai_review_loop.SWEEP_CAP


def test_sweep_rotates_through_the_stalest_sessions(ai_prefs, fake_jsonl, monkeypatch):
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    keys = [f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-2222-2222-2222-222222222222" for i in range(1, 6)]
    for i, key in enumerate(keys):
        webterm._buffer_append(key, f"output {i}\r\n".encode())
        metadata.patch(key, reviewed_at=2000.0 + i, review_fingerprint="stale")

    # Present them FRESHEST-first, so insertion order is the exact opposite of staleness order.
    reviewed, _ = _sweep(_FakeRegistry([_row(k) for k in reversed(keys)]))
    # The four stalest (lowest reviewed_at) go first — not the four first in the registry.
    assert set(reviewed) == set(keys[: ai_review_loop.SWEEP_CAP])


def test_candidates_orders_never_reviewed_before_stalest(ai_prefs, fake_jsonl):
    a, b, c = (
        "claude:aaaaaaaa-1111-1111-1111-111111111111",
        "claude:bbbbbbbb-1111-1111-1111-111111111111",
        "claude:cccccccc-1111-1111-1111-111111111111",
    )
    metadata.patch(a, reviewed_at=500.0)
    metadata.patch(b, reviewed_at=100.0)
    # c: never reviewed
    order = [k for k, _ in ai_review_loop._candidates(_FakeRegistry([_row(a), _row(b), _row(c)]))]
    assert order == [c, b, a]


def test_first_submit_kicks_the_review_loop_once(monkeypatch):
    kicks = []
    monkeypatch.setattr(ai_review_loop, "request_review_soon", lambda: kicks.append(1))
    key = "claude:dddddddd-1111-1111-1111-111111111111"
    webterm.scrollback._SUBMITTED.discard(key)

    webterm.scrollback.note_user_submit(key)
    webterm.scrollback.note_user_submit(key)
    webterm.scrollback.note_user_submit(key)
    assert len(kicks) == 1  # edge-triggered: later submits ride the interval


def test_submit_detection_distinguishes_typing_from_sending(monkeypatch):
    seen = []
    monkeypatch.setattr(webterm.scrollback, "note_user_submit", lambda k: seen.append(k))
    key = "claude:eeeeeeee-1111-1111-1111-111111111111"

    webterm._note_submit(key, b"deploy the thing")  # typed, not sent
    assert seen == []
    webterm._note_submit(key, b"\r")  # Enter
    assert seen == [key]
    webterm._note_submit(key, b"more\n")  # LF also counts
    assert seen == [key, key]
    webterm._note_submit(None, b"\r")  # no session key
    webterm._note_submit(key, b"")  # nothing typed
    assert seen == [key, key]


def test_first_submit_kick_does_not_re_review_an_unchanged_fingerprint(
    ai_prefs, fake_jsonl, monkeypatch
):
    """Hermes: the extra wake must not double-review what the first-output kick already did.
    The sweep's fingerprint gate is what makes a redundant kick free."""
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _chat_transport(calls))
    webterm._buffer_append(SID, b"agent banner\r\n")

    reviewed, _ = _sweep(_FakeRegistry([_row(SID)]))
    assert reviewed == [SID]
    before = len(calls)

    # A submit kick with no new content: the sweep runs and skips without an endpoint call.
    webterm.scrollback._SUBMITTED.discard(SID)
    webterm.scrollback.note_user_submit(SID)
    reviewed2, _ = _sweep(_FakeRegistry([_row(SID)]))
    assert reviewed2 == []
    assert len(calls) == before


# ---- Hermes on PR #618: a FAILING session must not monopolise the cap either --------------


def _failing_transport(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "endpoint down"})

    return httpx.MockTransport(handler)


def test_failing_never_reviewed_sessions_do_not_starve_the_rest(ai_prefs, fake_jsonl, monkeypatch):
    """A failed review persists nothing (#356), so `reviewed_at` never advances and the same
    never-reviewed sessions sort first on every sweep. With SWEEP_CAP=4 and five failing
    candidates, session 5 would never be attempted. `_LAST_ATTEMPT` breaks the tie."""
    ai_review_loop._LAST_ATTEMPT.clear()
    calls = []
    monkeypatch.setattr(review, "_TRANSPORT", _failing_transport(calls))
    monkeypatch.setattr(ai_review_loop, "CALL_SPACING_S", 0)

    keys = [f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-7777-7777-7777-777777777777" for i in range(1, 6)]
    for i, key in enumerate(keys):
        webterm._buffer_append(key, f"dirty output {i}\r\n".encode())
    assert len(keys) == ai_review_loop.SWEEP_CAP + 1
    reg = _FakeRegistry([_row(k) for k in keys])

    reviewed, failures = _sweep(reg)
    assert reviewed == [] and failures == ai_review_loop.SWEEP_CAP
    first_round = set(ai_review_loop._LAST_ATTEMPT)
    assert len(first_round) == ai_review_loop.SWEEP_CAP
    assert keys[4] not in first_round  # the fifth never got a turn in sweep 1

    # Second sweep: everyone still never-reviewed, but the four already-attempted yield.
    _sweep(reg)
    assert keys[4] in ai_review_loop._LAST_ATTEMPT, "the fifth session must be attempted eventually"


def test_last_attempt_is_pruned_for_sessions_that_leave_the_registry(ai_prefs, fake_jsonl):
    ai_review_loop._LAST_ATTEMPT.clear()
    ai_review_loop._LAST_ATTEMPT["claude:gone-forever"] = 1.0
    ai_review_loop._candidates(_FakeRegistry([_row(SID)]))
    assert "claude:gone-forever" not in ai_review_loop._LAST_ATTEMPT
