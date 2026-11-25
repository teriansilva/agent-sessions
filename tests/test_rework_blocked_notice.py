"""#2035 Phase 3: a rework round held back by an `unknown` run is surfaced, not left waiting.

A review asks for changes, but a step the reopen would reset still has an `unknown` run (never
settled automatically). The round waits and nothing is consumed; ONE pending-round notice per
blocking run (mission + edge + that run) tells the operator, deduplicated across passes, retries
and restarts, and retracted (closed on devices) when the run settles or the edge stops mattering.
"""

# ruff: noqa: F811 — imported pytest fixtures
from __future__ import annotations

import pytest

from agent_sessions import mission_rework, missions, notifications
from test_mission_rework import (
    SRC,
    TO,
    _at_review,
    _fresh_runner,  # noqa: F401 — the autouse runner reset
    _fresh_when_read,  # noqa: F401 — the autouse fresh `when` read
    _review_asked_for_changes,
    _run,
    anyio_backend,  # noqa: F401 — the anyio backend fixture
)


@pytest.fixture
def pushes(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(notifications, "push", lambda rec, *a, **k: sent.append(rec["action_id"]))
    return sent


def _blocked_rows(mid: str) -> list[str]:
    return notifications.actions_with_prefix(f"{mission_rework.BLOCKED}{mid}:")


def _blocked(state: str = "unknown"):
    """A review that asks for changes while the target step (in the reset span) has an open run
    of its own in `state`."""
    mid, doc = _at_review()
    _run(mid, TO, 2, state)
    _review_asked_for_changes(mid, doc)
    return mid, doc, mission_rework.blocked_key(mid, SRC, TO, 2)


@pytest.mark.anyio
async def test_a_round_behind_an_unknown_run_consumes_nothing_and_says_so(pushes):
    mid, doc, key = _blocked()
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [], "a round was consumed to get a record"
    assert _blocked_rows(mid) == [key]
    assert pushes == [key]
    row = next(r for r in notifications.listing()["notifications"] if r.get("action_id") == key)
    assert TO in row["reason"] and "unknown" in row["reason"]
    assert row.get("tag") == notifications.mission_episode_tag(key)


@pytest.mark.anyio
async def test_the_notice_is_deduplicated_across_passes_and_a_restart(pushes):
    mid, doc, key = _blocked()
    for _ in range(3):
        await mission_rework.sweep(mid)
    mission_rework.notify_blocked_sync(mid)  # a retry
    import agent_sessions.mission_runner as runner

    runner.reset_for_test()  # a restart forgets everything in memory; the store remembers
    await mission_rework.sweep(mid)
    assert _blocked_rows(mid) == [key] and pushes == [key]


@pytest.mark.anyio
async def test_it_is_retracted_on_devices_when_the_run_settles_and_the_round_then_proceeds(pushes):
    mid, doc, key = _blocked()
    await mission_rework.sweep(mid)
    missions.finish_step_run(mid, TO, 2, to="stopped", detail="settled", expect="unknown")
    assert await mission_rework.sweep(mid) == 1, "the unblocked round was not consumed"
    assert _blocked_rows(mid) == []
    assert notifications.mission_episode_tag(key) in notifications.owed_close_tags()


def test_it_is_retracted_when_the_edge_stops_mattering(pushes):
    """The review passed since: no round is owed, so nothing waits, and the routes' retraction
    (`retract_sync`) takes the notice down without a pass."""
    mid, doc, key = _blocked()
    mission_rework.notify_blocked_sync(mid)
    assert _blocked_rows(mid) == [key]
    missions.observe_objective(
        mid,
        _when_key(doc),
        observed=True,
        value=True,
        detail="approved",
        extra={"state": "APPROVED", "sha": "head2"},
    )
    assert mission_rework.retract_sync(mid) >= 1
    assert _blocked_rows(mid) == []


def _when_key(doc: dict) -> str:
    return next(s for s in doc["steps"] if s["id"] == SRC)["rework"]["when"]


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["claimed", "running"])
async def test_a_run_that_settles_by_itself_raises_no_notice(pushes, state):
    mid, doc, _ = _blocked(state)
    assert await mission_rework.sweep(mid) == 0
    assert missions.reworks(mid) == [] and _blocked_rows(mid) == [] and pushes == []


@pytest.mark.anyio
async def test_another_blocking_run_is_a_new_notice(pushes):
    """The identity is the blocking run: once that run settled and another went unknown, the
    operator is told again, about the new one."""
    mid, doc, key = _blocked()
    await mission_rework.sweep(mid)
    missions.finish_step_run(mid, TO, 2, to="stopped", detail="settled", expect="unknown")
    _run(mid, "o1.documents", 2, "unknown")
    mission_rework.notify_blocked_sync(mid)
    other = mission_rework.blocked_key(mid, SRC, "o1.documents", 2)
    assert _blocked_rows(mid) == [other] and pushes == [key, other]


# ---- independent review of #2035 P3: a blip never takes down a notice that still holds ----------


@pytest.mark.anyio
async def test_an_unknown_read_of_the_review_never_loses_the_notice(pushes):
    """One forge blip (an unknown read of the `when` row) while the same run still holds the round:
    the notice stays, and when the request is read again nothing is lost or pushed twice."""
    mid, doc, key = _blocked()
    await mission_rework.sweep(mid)
    missions.observe_objective(
        mid, _when_key(doc), observed=False, value=False, detail="forge down"
    )
    await mission_rework.sweep(mid)
    assert mission_rework.retract_sync(mid) == 0
    assert _blocked_rows(mid) == [key], "a blip took the notice down"
    _review_asked_for_changes(mid, doc)
    await mission_rework.sweep(mid)
    assert _blocked_rows(mid) == [key] and pushes == [key] and missions.reworks(mid) == []


@pytest.mark.anyio
async def test_a_pause_and_resume_never_loses_the_notice(pushes):
    mid, doc, key = _blocked()
    await mission_rework.sweep(mid)
    _set_mission_state(mid, "review")
    await mission_rework.sweep(mid)
    assert mission_rework.retract_sync(mid) == 0 and _blocked_rows(mid) == [key]
    _set_mission_state(mid, "running")
    await mission_rework.sweep(mid)
    assert _blocked_rows(mid) == [key] and pushes == [key]


def test_a_finished_mission_takes_the_notice_down(pushes):
    mid, doc, key = _blocked()
    mission_rework.notify_blocked_sync(mid)
    _set_mission_state(mid, "abandoned")
    assert mission_rework.retract_sync(mid) >= 1 and _blocked_rows(mid) == []


@pytest.mark.anyio
async def test_the_named_blocker_is_stable_and_prefers_the_unknown_run(pushes):
    """Two open runs in the span: the `unknown` one is named, the same one every pass (a flip
    would retract one notice and raise another)."""
    mid, doc = _at_review()
    _run(mid, TO, 2, "running")
    _run(mid, "o1.documents", 2, "unknown")
    _review_asked_for_changes(mid, doc)
    for _ in range(3):
        await mission_rework.sweep(mid)
    key = mission_rework.blocked_key(mid, SRC, "o1.documents", 2)
    assert _blocked_rows(mid) == [key] and pushes == [key]


def _set_mission_state(mid: str, state: str) -> None:
    con = missions._ready(None)
    try:
        con.execute("UPDATE missions SET state=? WHERE id=?", (state, mid))
        con.commit()
    finally:
        con.close()
