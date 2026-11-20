"""#2035 Phase 2: the rework `when` row is read FRESH, within a budget, outside the global lock.

The cached observation only nominates a candidate. Before a round is consumed the review is asked
again (a probe row through the probe pass's own fenced path; a judged row by gathering the
output), before `session_input._lock`, within `mission_rework.WHEN_READ_TIMEOUT_S`. Inside the
fence only the store is read: the read's age, the approval it began under, and the observation's
identity. Each test names the acceptance item it pins.
"""

# ruff: noqa: F811 — imported pytest fixtures
from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest

from agent_sessions import (
    forge,
    mission_fence,
    mission_judge,
    mission_probes,
    mission_rework,
    missions,
    session_input,
)
from test_mission_rework import (
    KEY,
    SRC,
    TO,
    _Fence,
    _fresh_runner,  # noqa: F401 — the autouse runner reset
    _fresh_when_read,  # noqa: F401 — the autouse fresh `when` read
    _mission,
    _review_asked_for_changes,
    _run,
    _running,
    _set_obs,
    _set_rows,
    _states,
    _when,
    anyio_backend,  # noqa: F401 — the anyio backend fixture
)


def _approve(mid: str) -> dict:
    """An approval in force on the latest revision, written through the store (these tests need
    the approval's identity, not its pins: the claim path that reads pins is not exercised)."""
    latest = missions.plan_revision(mid)
    return missions.commit_approval(
        mid,
        revision=latest["revision"],
        digest="sha256:" + "0" * 64,
        pins={},
        operation_id=str(uuid.uuid4()),
        request_digest="r",
    )


#: The real fresh read, before the autouse fixture replaces it with a forge that repeats itself.
REAL_READ_WHEN = mission_rework._read_when
#: The real idle read the fence makes (it reads `_snapshot`, which a test controls).
REAL_IDLE_SNAPSHOT = mission_rework._idle_snapshot


def _at_review_with_session(*, approved: bool = False) -> tuple[str, dict]:
    """The review runs in a session (the fenced stop path), its target done before it; with
    `approved`, under an approval committed before dispatch (`_approval(mid)` names it)."""
    mid, doc = _mission(met=("o1.intake",), state="planned")
    if approved:
        _approve(mid)
    _set_rows(mid, doc, ("o1.posting", TO, "o1.cover_letter", "o1.documents"), "met")
    _run(mid, TO, 1, "done")
    _run(mid, SRC, 1, session_key=KEY)
    _running(mid)
    return mid, doc


def _approval(mid: str) -> dict:
    got = missions.approval(mid)
    assert got is not None and got["revoked_at"] is None
    return got


def _src_run(mid: str) -> dict:
    return next(r for r in missions.step_runs(mid) if r["step_id"] == SRC and r["episode"] == 1)


def _fenced(monkeypatch, idle=lambda key: True) -> _Fence:
    """The fence, with `idle(key)` a hook run inside it before its one runtime read."""
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    monkeypatch.setattr(
        mission_rework, "_idle_snapshot", lambda key: REAL_IDLE_SNAPSHOT(key) if idle(key) else None
    )
    return fence


# ---- "a cached 'changes requested' since approved consumes nothing" ----------------------------


@pytest.mark.anyio
async def test_a_cached_request_the_forge_has_since_approved_consumes_nothing(monkeypatch):
    mid, doc = _at_review_with_session()
    fence = _fenced(monkeypatch)
    _review_asked_for_changes(mid, doc)  # what the last pass cached

    def approved_now(m, key):
        missions.observe_objective(
            m,
            key,
            observed=True,
            value=True,
            detail="approved",
            extra={"state": "APPROVED", "sha": "head2"},
            read_at=time.time(),
        )

    monkeypatch.setattr(mission_rework, "_read_when", approved_now)
    assert mission_rework.candidates_sync(mid, judge=False), "the cache nominates it"
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.calls == 0, "nothing stopped, nothing consumed"
    assert _src_run(mid)["state"] == "running"


@pytest.mark.anyio
async def test_a_fresh_read_that_records_nothing_consumes_nothing(monkeypatch):
    """The forge did not answer (the fresh read recorded nothing): the cached request is not
    re-used as if it had been read now."""
    mid, doc = _at_review_with_session()
    fence = _fenced(monkeypatch)
    _review_asked_for_changes(mid, doc)
    monkeypatch.setattr(mission_rework, "_read_when", lambda m, key: None)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.calls == 0


@pytest.mark.anyio
async def test_the_same_request_read_fresh_consumes_one_round(monkeypatch):
    mid, doc = _at_review_with_session()
    fence = _fenced(monkeypatch)
    _review_asked_for_changes(mid, doc)
    assert await mission_rework.sweep(mid) == 1
    assert fence.stopped == 1 and _states(mid, doc)[TO] == "ready"


# ---- "a timed-out read consumes nothing, including when it completes later" --------------------


@pytest.mark.anyio
async def test_a_timed_out_read_consumes_nothing_even_when_it_answers_later(monkeypatch):
    """The read does answer — "changes requested", the same request — but only after the budget:
    the sweep has already returned without waiting for it, nothing is stopped or consumed, and the
    late answer (an observation, recorded) is acted on by nobody."""
    mid, doc = _at_review_with_session()
    fence = _fenced(monkeypatch)
    _review_asked_for_changes(mid, doc)
    monkeypatch.setattr(mission_rework, "WHEN_READ_TIMEOUT_S", 0.2)
    finished = threading.Event()
    real = mission_rework._read_when  # the autouse forge, which repeats the request

    def slow(m, key):
        time.sleep(0.8)  # well past the budget, and it completes
        real(m, key)
        finished.set()

    monkeypatch.setattr(mission_rework, "_read_when", slow)
    t0 = time.monotonic()
    assert await mission_rework.sweep(mid) == 0
    assert time.monotonic() - t0 < 0.7, "the sweep waited for the slow read"
    assert not finished.is_set()
    assert await asyncio.to_thread(finished.wait, 10), "the late read never completed"
    await asyncio.sleep(0.2)  # anything that would act on the late answer has had its chance
    assert missions.reworks(mid) == [] and fence.calls == 0
    assert _src_run(mid)["state"] == "running"


@pytest.mark.anyio
async def test_a_judged_reads_gather_is_bounded_too(monkeypatch):
    mid, doc = _at_review_with_session()
    fence = _fenced(monkeypatch)
    no = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "fingerprint": "fp",
        "evidence": [{"source": "transcript:review", "quote": "the cv lists no project"}],
    }
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": no})
    monkeypatch.setattr(mission_rework, "WHEN_READ_TIMEOUT_S", 0.2)
    release = threading.Event()

    def slow_gather(mid_, cwd):
        release.wait(10)
        return object()

    monkeypatch.setattr(mission_judge, "gather", slow_gather)
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "fp")
    monkeypatch.setattr(mission_judge, "_counts", lambda source, inp: True)
    try:
        assert await mission_rework.sweep(mid) == 0
    finally:
        release.set()
    assert missions.reworks(mid) == [] and fence.calls == 0


# ---- "approval or row/episode replacement between the read and the lock" -----------------------


@pytest.mark.anyio
async def test_an_approval_revoked_between_the_read_and_the_lock_consumes_nothing(monkeypatch):
    mid, doc = _at_review_with_session(approved=True)
    first = _approval(mid)
    _review_asked_for_changes(mid, doc)

    def revoke_then_idle(key):
        missions.revoke_approval(mid, first["id"])
        return True

    fence = _fenced(monkeypatch, idle=revoke_then_idle)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


@pytest.mark.anyio
async def test_an_approval_revoked_during_the_read_consumes_nothing(monkeypatch):
    """Bound BEFORE the read: an approval that ends while the forge is asked is caught, though the
    fresh answer itself is the same request."""
    mid, doc = _at_review_with_session(approved=True)
    first = _approval(mid)
    _review_asked_for_changes(mid, doc)
    fence = _fenced(monkeypatch)
    real = mission_rework._read_when

    def read_while_revoked(m, key):
        missions.revoke_approval(m, first["id"])
        real(m, key)

    monkeypatch.setattr(mission_rework, "_read_when", read_while_revoked)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


def test_the_consume_binds_the_approval_in_its_own_transaction():
    """The store's own re-check: a consume bound to an approval no longer in force is nothing."""
    mid, doc = _at_review_with_session(approved=True)
    first = _approval(mid)
    _review_asked_for_changes(mid, doc)
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    oid = missions.rework_observation(row, float(_src_run(mid)["created_at"]))
    missions.revoke_approval(mid, first["id"])
    args = {"edge_from": SRC, "source_episode": 1, "observation_id": oid}
    assert missions.rework_decision(mid, expect_approval=first["id"], **args) is None
    assert missions.consume_rework(mid, expect_approval=first["id"], **args) is None
    assert missions.reworks(mid) == []
    # Bound to the truth (none in force), the same round consumes.
    assert missions.consume_rework(mid, expect_approval=None, **args)["new"]


@pytest.mark.anyio
async def test_a_row_replaced_between_the_read_and_the_lock_consumes_nothing(monkeypatch):
    mid, doc = _at_review_with_session()
    _review_asked_for_changes(mid, doc, review="7001")

    def new_review_then_idle(key):
        _review_asked_for_changes(mid, doc, review="7002")  # another request, another identity
        return True

    fence = _fenced(monkeypatch, idle=new_review_then_idle)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


@pytest.mark.anyio
async def test_an_episode_settled_between_the_read_and_the_lock_consumes_nothing(monkeypatch):
    mid, doc = _at_review_with_session()
    _review_asked_for_changes(mid, doc)

    def settled_then_idle(key):
        missions.finish_step_run(mid, SRC, 1, to="stopped", detail="x", expect="running")
        return True

    fence = _fenced(monkeypatch, idle=settled_then_idle)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


@pytest.mark.anyio
async def test_a_read_older_than_its_age_bound_at_the_fence_consumes_nothing(monkeypatch):
    mid, doc = _at_review_with_session()
    _review_asked_for_changes(mid, doc)
    monkeypatch.setattr(mission_rework, "WHEN_READ_MAX_AGE_S", -1.0)
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


# ---- "another session acquires the global lock while a slow read is pending" -------------------


@pytest.mark.anyio
async def test_the_global_lock_is_free_while_the_read_is_pending(monkeypatch):
    mid, doc = _at_review_with_session()
    _review_asked_for_changes(mid, doc)
    fence = _fenced(monkeypatch)
    pending, release = threading.Event(), threading.Event()
    real = mission_rework._read_when

    def slow(m, key):
        pending.set()
        assert release.wait(10)
        real(m, key)

    monkeypatch.setattr(mission_rework, "_read_when", slow)
    task = asyncio.ensure_future(mission_rework.sweep(mid))
    assert await asyncio.to_thread(pending.wait, 10)

    def another_session() -> bool:
        if not session_input._lock.acquire(timeout=2):
            return False
        session_input._lock.release()
        return True

    try:
        assert await asyncio.to_thread(another_session), "the read held the global lock"
    finally:
        release.set()
    assert await task == 1 and fence.stopped == 1


@pytest.mark.anyio
async def test_no_gather_runs_under_the_global_lock(monkeypatch):
    """The judge's gather (the slow read) is never called while the session lock is held: the
    fence's re-check, run here under the lock as the real fence runs it, reads only the store."""
    mid, doc = _at_review_with_session()
    no = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "fingerprint": "fp",
        "evidence": [{"source": "transcript:review", "quote": "the cv lists no project"}],
    }
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": no})
    held: list[bool] = []

    def gather(mid_, cwd):
        held.append(session_input._lock.locked())
        return object()

    real_valid = mission_rework._still_valid

    def valid_under_the_lock(m, cand):
        with session_input._lock:
            return real_valid(m, cand)

    monkeypatch.setattr(mission_judge, "gather", gather)
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "fp")
    monkeypatch.setattr(mission_judge, "_counts", lambda source, inp: True)
    monkeypatch.setattr(mission_rework, "_still_valid", valid_under_the_lock)
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 1 and fence.stopped == 1
    assert held and not any(held), held


# ---- the real fresh read: the probe pass's own path -------------------------------------------


@pytest.mark.anyio
async def test_the_real_fresh_read_asks_the_probe_and_records_its_start(monkeypatch):
    mid, doc = _at_review_with_session()
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET probe='forge_review', probe_args=NULL "
            "WHERE mission_id=? AND key=?",
            (mid, _when(doc)),
        )
        con.commit()
    finally:
        con.close()
    _review_asked_for_changes(mid, doc, review="7100")  # the cache, from a minute ago
    asked: list[str] = []

    def probe(m, o, **_):
        asked.append(o["key"])
        return forge.Fact.seen(
            False, "changes were requested", state="REQUEST_CHANGES", reviews="7100", sha="head1"
        )

    monkeypatch.setattr(mission_probes, "probe_one", probe)
    monkeypatch.setattr(mission_rework, "_read_when", REAL_READ_WHEN)
    fence = _fenced(monkeypatch)
    before = time.time()
    assert await mission_rework.sweep(mid) == 1
    assert asked == [_when(doc)], "the fresh read did not ask the probe"
    assert fence.stopped == 1
    (rnd,) = missions.reworks(mid)
    assert rnd["round"] == 1
    # The consumed observation was the FRESH one: its read began in this sweep.
    assert rnd["observation_id"] and len(asked) == 1
    row = next(o for o in missions.objectives(mid) if o["key"] == _when(doc))
    assert row["observed"] is None or float(row["observed"].get("read_at") or before) >= before


# ---- Hermes on #2230: the judged read is bound to the reviewer's output generation --------------


def _judged_review(mid, doc):
    no = {
        "met": False,
        "confidence": 0.05,
        "threshold": 0.9,
        "fingerprint": "fp",
        "evidence": [{"source": "transcript:review", "quote": "the cv lists no project"}],
    }
    _set_obs(mid, doc, {"at": time.time(), "detail": "not met", "value": False, "judged": no})


class _Reviewer:
    """The reviewer's runtime as `structured_runtime.snapshot` reports it: settled turns, and
    whether a turn runs. A test moves it (a corrected answer, a turn starting)."""

    def __init__(self) -> None:
        self.turns = [{"turn_id": "t1", "status": "done"}]
        self.active = None
        self.readable = True

    def snapshot(self, key):
        if key != KEY:
            return {"turns": list(self.others.get(key, [])), "omitted_turns": 0}
        if not self.readable:
            raise RuntimeError("no runtime")
        return {"turns": list(self.turns), "omitted_turns": 0, "active_turn": self.active}


@pytest.fixture
def reviewer(monkeypatch):
    r = _Reviewer()
    r.others = {}
    r.keys = [KEY]
    monkeypatch.setattr(mission_rework, "_snapshot", r.snapshot)

    async def on_the_loop(key):
        r.loops.append(asyncio.get_running_loop())
        return r.snapshot(key)

    r.loops = []
    monkeypatch.setattr(mission_rework, "_snapshot_async", on_the_loop)
    monkeypatch.setattr(mission_rework, "_gathered_keys", lambda m: list(r.keys))
    monkeypatch.setattr(mission_judge, "gather", lambda mid_, cwd: object())
    monkeypatch.setattr(mission_judge, "row_fingerprint", lambda inp, row: "fp")
    monkeypatch.setattr(mission_judge, "_counts", lambda source, inp: True)
    return r


@pytest.mark.anyio
async def test_a_reviewer_that_answers_again_before_the_fence_spends_no_round(
    monkeypatch, reviewer
):
    """Hermes on #2230: the gather saw the reviewer's old, confidently not-met output; the reviewer
    then settles a corrected turn and goes idle before the fence. The stored verdict is unchanged,
    but it no longer describes the output: nothing is stopped or consumed."""
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)

    def answers_then_idle(key):
        reviewer.turns.append({"turn_id": "t2", "status": "done"})  # the corrected answer
        return True

    fence = _fenced(monkeypatch, idle=answers_then_idle)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


@pytest.mark.anyio
async def test_a_reviewer_still_in_its_turn_at_the_gather_spends_no_round(monkeypatch, reviewer):
    """A gather made while the reviewer's turn runs, the turn settling before the fence: the
    generation moved (the move to idle), so the read is discarded and the next pass reads again."""
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    reviewer.active = "t2"

    def settles_then_idle(key):
        reviewer.active = None
        reviewer.turns.append({"turn_id": "t2", "status": "done"})
        return True

    fence = _fenced(monkeypatch, idle=settles_then_idle)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and fence.stopped == 0


@pytest.mark.anyio
async def test_an_unmoved_reviewer_spends_its_round(monkeypatch, reviewer):
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 1 and fence.stopped == 1


@pytest.mark.anyio
async def test_a_reviewer_whose_output_cannot_be_read_spends_no_round(monkeypatch, reviewer):
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    reviewer.readable = False
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 0 and fence.calls == 0


OTHER = "claude-api:0b0b0b0b-2222-4222-8222-0b0b0b0b0b0b"


@pytest.mark.anyio
async def test_another_gathered_session_moving_during_the_gather_spends_no_round(
    monkeypatch, reviewer
):
    """Independent review of #2230: a gathered session's turn settles while the gather reads it.
    The generation is read again after the gather (outside the lock) and the read is discarded."""
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    reviewer.keys = [KEY, OTHER]
    reviewer.others[OTHER] = [{"turn_id": "o1", "status": "done"}]

    def gather_while_other_answers(mid_, cwd):
        reviewer.others[OTHER] = reviewer.others[OTHER] + [{"turn_id": "o2", "status": "done"}]
        return object()

    monkeypatch.setattr(mission_judge, "gather", gather_while_other_answers)
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 0 and fence.calls == 0


@pytest.mark.anyio
async def test_a_different_set_of_sessions_after_the_gather_spends_no_round(monkeypatch, reviewer):
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)

    def gather_while_keys_drift(mid_, cwd):
        reviewer.keys = [KEY, OTHER]
        return object()

    monkeypatch.setattr(mission_judge, "gather", gather_while_keys_drift)
    fence = _fenced(monkeypatch)
    assert await mission_rework.sweep(mid) == 0 and fence.calls == 0


@pytest.mark.anyio
async def test_the_fence_makes_one_runtime_read_under_the_lock(monkeypatch, reviewer):
    """Independent review of #2230: a chat snapshot waits on that session's own asyncio lock, so
    the fence reads nothing new under the global lock — ONE snapshot (the reviewer's idle read),
    whatever the number of gathered sessions."""
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    reviewer.keys = [KEY, OTHER]
    under_lock: list[str] = []
    real = reviewer.snapshot

    def counted(key):
        if session_input._lock.locked():
            under_lock.append(key)
        return real(key)

    monkeypatch.setattr(mission_rework, "_snapshot", counted)
    real_ready = mission_rework._fence_ready

    def ready_under_the_lock(m, key, cand):
        with session_input._lock:  # what the real fence holds around `idle()`
            return real_ready(m, key, cand)

    monkeypatch.setattr(mission_rework, "_fence_ready", ready_under_the_lock)
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    assert await mission_rework.sweep(mid) == 1 and fence.stopped == 1
    assert under_lock == [KEY], under_lock


@pytest.mark.anyio
async def test_the_generation_is_read_on_the_main_loop_never_a_second_one(monkeypatch, reviewer):
    """Independent review of #2230: a chat snapshot takes that session's `asyncio.Lock`, which
    belongs to the main loop. Every generation read outside the fence is awaited on THAT loop
    (here: the test's), never through `asyncio.run` in a worker thread."""
    mid, doc = _at_review_with_session()
    _judged_review(mid, doc)
    reviewer.keys = [KEY, OTHER]

    def no_second_loop(key):
        raise AssertionError("a generation snapshot was driven through a worker-thread loop")

    monkeypatch.setattr(mission_rework, "_snapshot", no_second_loop)
    monkeypatch.setattr(mission_rework, "_idle_snapshot", lambda key: {})  # the fence's own read
    fence = _Fence()
    monkeypatch.setattr(mission_fence, "fenced_step_stop", fence)
    loop = asyncio.get_running_loop()
    await mission_rework.sweep(mid)
    assert reviewer.loops and all(x is loop for x in reviewer.loops), reviewer.loops
