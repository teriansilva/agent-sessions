"""Cross-store races (#846, Phase 1 of #840).

A mission's timeline joins three stores — the missions DB, the orchestrator ledger and the
metadata sidecar — and none of them share a lock. So the interesting failures are not "does the
query work" but "what does a reader see when the world moves underneath it". The three the
checklist names, plus the concurrency the store's own invariants rest on.

Each test drives the race deterministically (a barrier or a patched call that mutates *inside*
the read) rather than by sleeping and hoping, because a timing-hopeful race test that passes on a
quiet machine is worse than none.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sqlite3
import threading
import time

import pytest

from agent_sessions import missions
from agent_sessions import orchestrator_ledger as ledger

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"
CLAUDE_B = "claude:22222222-2222-2222-2222-222222222222"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "ledger.jsonl"))
    missions.reset_schema_cache_for_test()
    yield tmp_path
    missions.reset_schema_cache_for_test()


def _running(instruction="do the thing"):
    m = missions.create_mission(instruction, cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    return m["id"]


# ---- an action settles WHILE the timeline joins it ---------------------------------


def test_an_action_settling_mid_join_never_renders_a_decision_with_no_content(stores):
    """The join is read-time, so an action can settle — and later be compacted — between the
    mission read and the ledger read. The projection is what makes that safe: it is frozen at
    settlement, so the timeline never depends on the ledger row still existing."""
    mid = _running()
    ledger.append(
        {"id": "act-1", "state": "proposed", "verb": "choose", "rationale": "waiting on 1 or 2"}
    )
    missions.append_event(mid, "approval", action_id="act-1", text="approve?")

    joined: list[dict] = []

    def _read_then_settle():
        row = missions.get_mission(mid)
        # …the operator decides it right here, between the two reads…
        ledger.compare_and_set("act-1", ledger.REJECTABLE_STATES, "rejected", detail="declined")
        joined.append(row)

    _read_then_settle()
    # The first read predates the settlement, so its event has no projection yet — and the
    # AUTHORITATIVE ledger row is still there for it, which is the documented precedence.
    assert ledger.get("act-1")["state"] == "rejected"
    # A re-read carries the frozen projection, which survives compaction.
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"noise-{i}", "state": "observed"})
    ledger.compact()
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-1"][0]
    assert ledger.get("act-1") is None
    assert ev["settlement"]["verb"] == "choose" and ev["settlement"]["state"] == "rejected"


def test_a_settlement_for_an_action_no_mission_references_is_a_no_op(stores):
    """The hook fires on EVERY ledger settlement, most of which belong to no mission at all."""
    _running()
    ledger.append({"id": "orphan", "state": "proposed"})
    assert ledger.transition("orphan", "delivered")["state"] == "delivered"
    assert missions.record_settlement("orphan", {"state": "delivered"}) == 0


def test_an_event_appended_after_its_action_settled_is_reconciled_on_read(stores):
    """The event can be appended AFTER the action settles — the supervisor records the decision
    it just saw, so the settlement hook has already fired and found nothing to write.

    "Best-effort, once" would make that projection permanently absent, and once the ledger
    compacts, the decision renders empty forever. So the read path reconciles it while the ledger
    row still exists: any read before compaction wins."""
    mid = _running()
    ledger.append({"id": "act-late", "state": "proposed", "verb": "nudge"})
    ledger.transition("act-late", "delivered", detail="sent")
    missions.append_event(mid, "approval", action_id="act-late")

    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-late"][0]
    assert ev["settlement"]["verb"] == "nudge"
    assert ev["settlement"]["outcome"] == "sent"

    # …and it survives the compaction it was written to survive.
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"noise-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-late") is None
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-late"][0]
    assert ev["settlement"]["outcome"] == "sent"


def test_a_live_action_is_not_projected_early(stores):
    """The ledger is authoritative while the row exists, and a live action has no final state to
    freeze — projecting one would record an outcome that has not happened."""
    mid = _running()
    ledger.append({"id": "act-live", "state": "proposed", "verb": "choose"})
    missions.append_event(mid, "approval", action_id="act-live")
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-live"][0]
    assert ev["settlement"] is None


def test_a_lost_settlement_write_is_recovered_by_the_next_read(stores, monkeypatch):
    """Finding from review: `_settled` suppresses every failure by design, so one busy moment
    used to lose the projection permanently."""
    mid = _running()
    ledger.append({"id": "act-lost", "state": "proposed", "verb": "continue"})
    missions.append_event(mid, "approval", action_id="act-lost")
    real = missions.record_settlement
    monkeypatch.setattr(
        missions, "record_settlement", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
    )
    ledger.transition("act-lost", "delivered", detail="landed")
    monkeypatch.setattr(missions, "record_settlement", real)
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-lost"][0]
    assert ev["settlement"]["outcome"] == "landed"


# ---- a session is removed / reconciled WHILE it is being adopted ---------------------


def test_two_concurrent_adopts_of_the_same_session_produce_one_winner(stores):
    a, b = _running(), _running()
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def _adopt(mid):
        start.wait(5)
        try:
            missions.adopt(mid, CLAUDE_A)
            outcomes.append("won")
        except missions.SessionHeld:
            outcomes.append("lost")

    ts = [threading.Thread(target=_adopt, args=(m,)) for m in (a, b)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert sorted(outcomes) == ["lost", "won"]


def test_a_detach_racing_an_adopt_from_another_mission(stores):
    """A detach that lands first frees the session; one that lands second finds nothing to do.
    Either order is legal — what must never happen is two missions holding it at once."""
    a, b = _running(), _running()
    missions.adopt(a, CLAUDE_A)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _detach():
        start.wait(5)
        try:
            missions.detach(a, CLAUDE_A)
        except missions.MissionError:
            pass
        except BaseException as e:  # noqa: BLE001 — anything else is the finding
            errors.append(e)

    def _adopt():
        start.wait(5)
        try:
            missions.adopt(b, CLAUDE_A)
        except missions.SessionHeld:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_detach), threading.Thread(target=_adopt)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert errors == []
    holders = [m for m in (a, b) if CLAUDE_A in missions.active_session_keys(m)]
    assert len(holders) <= 1


def test_a_mission_closing_while_a_session_is_being_adopted(stores):
    """The transition releases every active row; the adopt either lands before it (and is
    released with the rest) or after it (and is simply held by a closed mission). Neither leaves
    the partial unique index inconsistent."""
    mid = _running()
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _close():
        start.wait(5)
        try:
            missions.set_state(mid, "running", "done", outcome="done")
        except missions.MissionError:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def _adopt():
        start.wait(5)
        try:
            missions.adopt(mid, CLAUDE_A)
        except missions.MissionError:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_close), threading.Thread(target=_adopt)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert errors == []
    assert missions.get_mission(mid)["state"] == "done"
    # Exactly one row either way, and the index is intact.
    assert len(missions.get_mission(mid)["sessions"]) <= 1


# ---- a mission is closed or reopened WHILE a review proposal is in flight --------------


def test_a_completion_proposal_loses_the_race_to_an_operator_who_already_closed_it(stores):
    """The supervisor decides "this looks done" from a state it read some time ago. Compare-and-
    set is what stops that stale decision landing: a zero rowcount is a lost race, not a retry."""
    mid = _running()
    observed = missions.get_mission(mid)["state"]  # what the supervisor saw
    missions.set_state(mid, "running", "done", outcome="done")  # the operator got there first
    with pytest.raises(missions.MissionError) as e:
        missions.set_state(mid, observed, "review")
    assert e.value.status == 409


def test_a_reopen_racing_a_review_proposal_leaves_one_coherent_state(stores):
    mid = _running()
    missions.set_state(mid, "running", "review")
    start = threading.Barrier(2)
    results: list[str] = []

    def _to_done():
        start.wait(5)
        try:
            missions.set_state(mid, "review", "done", outcome="done")
            results.append("done")
        except missions.MissionError:
            results.append("lost")

    def _to_running():
        start.wait(5)
        try:
            missions.set_state(mid, "review", "running")
            results.append("running")
        except missions.MissionError:
            results.append("lost")

    ts = [threading.Thread(target=_to_done), threading.Thread(target=_to_running)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    # Exactly one winner — never both applied, never neither.
    assert results.count("lost") == 1
    assert missions.get_mission(mid)["state"] in ("done", "running")


def test_an_objective_gate_added_while_the_mission_moves_to_done(stores):
    """ "adding an unmet gate reopens `review`" and "confirm done" both read the state and then
    write it. The reopen happens in the SAME transaction as the insert, so the objective list and
    the lifecycle column can never disagree about which one won."""
    mid = _running()
    missions.set_state(mid, "running", "review")
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def _add_gate():
        start.wait(5)
        try:
            missions.patch_objectives(
                mid, [{"op": "add", "key": "late", "title": "late", "gate": True}]
            )
        except missions.MissionError:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def _confirm():
        start.wait(5)
        try:
            missions.set_state(mid, "review", "done", outcome="done")
        except missions.MissionError:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_add_gate), threading.Thread(target=_confirm)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert errors == []
    row = missions.get_mission(mid)
    if row["state"] == "done":
        # The confirm won; the gate (if it landed) is simply an unmet objective on a done
        # mission, which the completion card will show. It must NOT read as met.
        assert all(o["state"] != "met" for o in row["objectives"])
    else:
        assert row["state"] == "running"


# ---- concurrent writers on the store itself ---------------------------------------------


def test_concurrent_creates_do_not_collide_or_lose_rows(stores):
    n = 12
    start = threading.Barrier(n)
    errors: list[BaseException] = []

    def _make(i):
        start.wait(10)
        try:
            missions.create_mission(f"instruction {i}", cwd="/repo")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=_make, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    assert errors == []
    assert missions.list_missions(limit=missions.LIST_LIMIT_MAX)["total"] == n


def test_a_second_process_holding_the_write_lock_surfaces_rather_than_hanging_forever(stores):
    """`busy_timeout` is the backstop against another PROCESS; this app's own writers serialize
    through one module lock, so the timeout is never load-bearing for us. When it does fire, the
    write raises — it is never swallowed, because a dropped write is indistinguishable from one
    that worked."""
    _running()
    other = sqlite3.connect(str(stores / "missions.db"))
    other.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            con = missions._ready(busy_timeout_ms=200)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("UPDATE missions SET title='x'")
            finally:
                con.close()
    finally:
        other.rollback()
        other.close()


# ---- the projection survives the one thing that can destroy it ---------------------------


def test_compaction_freezes_a_projection_the_settlement_hook_missed(stores, monkeypatch):
    """The hole the read-time backfill could not close.

    `_settled` is best-effort by design, so one busy moment leaves no projection. If compaction
    then wins the race to the ledger row before anybody reads the mission, the verb, rationale and
    outcome are gone for good — reproduced exactly that way in review.

    Compaction is the ONLY thing that ever removes a ledger row, so it is the one moment at which
    "this is about to become unavailable" is knowable. It now freezes first.
    """
    mid = _running()
    ledger.append(
        {
            "id": "act-doomed",
            "state": "proposed",
            "verb": "choose",
            "rationale": "the prompt is waiting on 1 or 2",
        }
    )
    missions.append_event(mid, "approval", action_id="act-doomed", text="approve?")

    # The settlement hook fails once — the busy moment.
    real = missions.record_settlement
    monkeypatch.setattr(
        missions, "record_settlement", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
    )
    ledger.compare_and_set("act-doomed", ledger.REJECTABLE_STATES, "rejected", detail="declined")
    monkeypatch.setattr(missions, "record_settlement", real)
    assert missions.settlements_for(["act-doomed"]) == {}  # nothing frozen yet

    # …and compaction wins the race to the row, with NO mission read in between.
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"noise-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-doomed") is None

    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-doomed"][0]
    assert ev["settlement"]["verb"] == "choose"
    assert ev["settlement"]["state"] == "rejected"
    assert ev["settlement"]["outcome"] == "declined"


def test_compaction_does_not_freeze_a_live_action(stores):
    """A live action has no final state to freeze; compaction keeps its row anyway."""
    mid = _running()
    ledger.append({"id": "act-live2", "state": "proposed", "verb": "continue"})
    missions.append_event(mid, "approval", action_id="act-live2")
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"n-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-live2")["state"] == "proposed"  # live rows are never compacted
    assert missions.settlements_for(["act-live2"]) == {}


def test_a_compaction_hook_failure_never_fails_compaction(stores, monkeypatch):
    monkeypatch.setattr(
        missions, "record_settlements", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
    )
    ledger.append({"id": "act-x", "state": "observed"})
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"m-{i}", "state": "observed"})
    assert ledger.compact() > 0  # the ledger's own job still gets done


def test_the_projection_outlives_the_bounded_feed(stores):
    """Option A, stated as a test: the timeline is a bounded FEED and a decision's content is a
    separate durable fact. Bounding one must not reach the other."""
    mid = _running()
    ledger.append({"id": "act-keep", "state": "proposed", "verb": "nudge"})
    missions.append_event(mid, "approval", action_id="act-keep")
    ledger.transition("act-keep", "delivered", detail="landed")
    assert missions.settlements_for(["act-keep"])["act-keep"]["outcome"] == "landed"

    # Blow the feed far past its hard ceiling with operator edits — the growth that used to take
    # the projection with it.
    missions.patch_objectives(mid, [{"op": "add", "key": "o", "title": "o"}])
    for i in range(missions.MISSION_EVENTS_HARD_MAX + 100):
        missions.patch_objectives(mid, [{"op": "retitle", "key": "o", "title": f"t{i}"}])
    assert missions.event_count(mid) <= missions.MISSION_EVENTS_HARD_MAX
    # The feed is bounded; the decision's content is untouched.
    assert missions.settlements_for(["act-keep"])["act-keep"]["verb"] == "nudge"


def test_a_projection_is_deleted_with_its_mission(stores):
    """`rationale` is bounded model text about the operator's work, so the normalized table must
    not become the one place sensitive text outlives its mission."""
    mid = _running()
    ledger.append({"id": "act-gone", "state": "proposed", "verb": "choose", "rationale": "secret"})
    missions.append_event(mid, "approval", action_id="act-gone")
    ledger.transition("act-gone", "rejected")
    assert missions.settlements_for(["act-gone"])
    missions.delete_mission(mid)
    assert missions.settlements_for(["act-gone"]) == {}


def test_compaction_is_declined_rather_than_destroying_an_unprojected_row(stores, monkeypatch):
    """The projection is a promise, so the row it protects may not be destroyed before it is
    durable. Suppressing the failure and compacting anyway lost the content permanently."""
    mid = _running()
    ledger.append({"id": "act-fenced", "state": "proposed", "verb": "choose", "rationale": "why"})
    missions.append_event(mid, "approval", action_id="act-fenced")
    ledger.transition("act-fenced", "rejected", detail="declined")
    # Wipe the projection so compaction is the only thing standing between the mission and a
    # decision with no content.
    con = missions._ready()
    try:
        con.execute("DELETE FROM mission_settlements")
    finally:
        con.close()

    # Restore the ONE attribute afterwards, not `monkeypatch.undo()` — that would also revert the
    # `stores` fixture's env, pointing the store somewhere else mid-test.
    real = missions.record_settlements
    monkeypatch.setattr(
        missions, "record_settlements", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
    )
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"z-{i}", "state": "observed"})
    assert ledger.compact() == 0, "compaction destroyed a row it could not project"
    assert ledger.get("act-fenced") is not None  # the evidence is still there

    monkeypatch.setattr(missions, "record_settlements", real)
    assert ledger.compact() > 0
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-fenced"][0]
    assert ev["settlement"]["outcome"] == "declined"


def test_the_projected_set_is_the_set_that_is_rewritten(stores):
    """Computing the doomed set outside the lock let a concurrent terminal append change which
    rows `_compact_locked` dropped, leaving a newly-doomed referenced action unprojected. One
    snapshot now feeds both."""
    rows = [{"id": f"a{i}", "state": "observed", "ts": i} for i in range(ledger.HISTORY_MAX + 10)]
    doomed = ledger._doomed(rows, ledger.HISTORY_MAX)
    live = [r for r in rows if r.get("state") in ledger.LIVE_STATES]
    done = sorted(
        (r for r in rows if r.get("state") not in ledger.LIVE_STATES),
        key=lambda r: float(r.get("ts") or 0),
        reverse=True,
    )
    kept = {r["id"] for r in live + done[: ledger.HISTORY_MAX]}
    assert {r["id"] for r in doomed}.isdisjoint(kept)
    assert {r["id"] for r in doomed} | kept == {r["id"] for r in rows}


def test_an_unreferenced_action_is_never_copied_into_the_missions_store(stores):
    """Every settled ledger action used to be copied in, whether a mission pointed at it or not —
    bounded model text with no mission to own it, no path that could name it for collection, and
    no retention window over it."""
    _running()  # a mission exists, but references nothing
    ledger.append({"id": "act-nobody", "state": "proposed", "verb": "nudge", "rationale": "text"})
    ledger.transition("act-nobody", "delivered", detail="sent")
    assert missions.settlements_for(["act-nobody"]) == {}

    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"q-{i}", "state": "observed"})
    ledger.compact()
    assert missions.settlements_for(["act-nobody"]) == {}
    con = missions._ready()
    try:
        assert con.execute("SELECT COUNT(*) FROM mission_settlements").fetchone()[0] == 0
    finally:
        con.close()


def test_a_reference_created_after_the_projection_check_still_survives_compaction(stores):
    """The exact interleaving from review round four.

    Compaction asks which doomed actions a mission references, then rewrites the ledger — a
    check-then-act across two stores. An event committed in between creates a reference to a row
    compaction has already decided nobody points at.

    Fencing the two stores against each other would order their locks in both directions (the
    deadlock the settlement hook documents), so the write ORDER is what fixes it: the projection
    is committed in the SAME transaction as the reference, and therefore exists before the
    reference does.
    """
    mid = _running()
    ledger.append({"id": "act-late-ref", "state": "proposed", "verb": "choose", "rationale": "why"})
    ledger.transition("act-late-ref", "rejected", detail="declined")
    # Nothing references it yet, so nothing is projected — exactly the state compaction checks.
    con = missions._ready()
    try:
        con.execute("DELETE FROM mission_settlements")
    finally:
        con.close()
    assert missions.referenced_action_ids(["act-late-ref"]) == set()

    # The reference arrives AFTER that check would have returned empty…
    missions.append_event(mid, "approval", action_id="act-late-ref")
    # …and it brought its projection with it, atomically.
    assert missions.settlements_for(["act-late-ref"])["act-late-ref"]["outcome"] == "declined"

    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"late-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-late-ref") is None
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-late-ref"][0]
    assert ev["settlement"]["verb"] == "choose"


def test_a_rolled_back_event_leaves_no_orphaned_projection(stores):
    """The other half of making the two writes atomic: if the reference does not commit, neither
    does the projection — so ordering them cannot reintroduce the orphan problem."""
    mid = _running()
    ledger.append({"id": "act-rollback", "state": "proposed", "verb": "nudge"})
    ledger.transition("act-rollback", "delivered", detail="sent")
    missions.set_state(mid, "running", "done", outcome="done")
    missions.begin_archive(mid)  # the lifecycle fence will refuse the append

    with pytest.raises(missions.MissionError):
        missions.append_event(mid, "approval", action_id="act-rollback")
    assert missions.settlements_for(["act-rollback"]) == {}


def test_a_reference_to_a_live_action_projects_nothing_yet(stores):
    """A live row cannot be compacted, so there is nothing to preserve — and projecting a state
    that has not settled would record an outcome that never happened."""
    mid = _running()
    ledger.append({"id": "act-still-live", "state": "proposed", "verb": "continue"})
    missions.append_event(mid, "approval", action_id="act-still-live")
    assert missions.settlements_for(["act-still-live"]) == {}
    ledger.transition("act-still-live", "delivered", detail="ok")
    assert missions.settlements_for(["act-still-live"])["act-still-live"]["outcome"] == "ok"


def test_a_reference_to_an_already_compacted_action_records_that_it_is_gone(stores):
    """The last window of the compaction race, and the decision it forced.

    Compaction can legitimately drop a terminal row before anything referenced it. There are three
    possible answers and only one is both honest and non-destructive: rejecting the write fails an
    operator action over ledger housekeeping; committing silently renders a decision that is blank
    forever; recording that the source was destroyed says the true thing.

    §16 already has the projection for it — *absent from the ledger ⇒ historical, no controls, and
    no outcome asserted*.
    """
    mid = _running()
    ledger.append({"id": "act-vanished", "state": "observed"})
    for i in range(ledger.HISTORY_MAX + 5):
        ledger.append({"id": f"v-{i}", "state": "observed"})
    ledger.compact()
    assert ledger.get("act-vanished") is None  # gone before anything referenced it

    missions.append_event(mid, "approval", action_id="act-vanished")
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-vanished"][0]
    assert ev["settlement"] is not None, "committed a decision with nothing behind it"
    assert ev["settlement"]["state"] == missions.SETTLEMENT_LOST
    assert ev["settlement"]["outcome"] is None  # no outcome is asserted
    assert "compacted before" in ev["settlement"]["rationale"]


def test_a_transiently_unreadable_ledger_is_not_recorded_as_compaction(stores):
    """`read_all` maps every OSError to an empty history — right for a feed, which must degrade
    rather than disappear, and wrong for "does this action still exist?".

    Freezing `source_compacted` on a transient EIO records a permanent answer ("we can never know
    what this decision said") about a row sitting intact on disk — and `INSERT OR IGNORE` then made
    it unrepairable.
    """
    mid = _running()
    ledger.append({"id": "act-eio", "state": "proposed", "verb": "choose", "rationale": "truth"})
    ledger.transition("act-eio", "rejected", detail="declined")
    con = missions._ready()
    try:
        con.execute("DELETE FROM mission_settlements")
    finally:
        con.close()

    path = pathlib.Path(os.environ["AGENT_SESSIONS_ORCHESTRATOR_LEDGER"])
    real_read = pathlib.Path.read_text

    def _eio(self, *a, **k):
        if self == path:
            raise OSError("EIO")
        return real_read(self, *a, **k)

    pathlib.Path.read_text = _eio
    try:
        missions.append_event(mid, "approval", action_id="act-eio")
    finally:
        pathlib.Path.read_text = real_read

    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-eio"][0]
    assert ev["settlement"] is not None
    assert ev["settlement"]["state"] == "rejected", "a transient read froze a false marker"
    assert ev["settlement"]["rationale"] == "truth"


def test_the_lost_marker_is_the_one_settlement_that_can_be_upgraded(stores):
    """It is the "we could not find out" state, so if the truth turns up later it must win.
    Everything else stays write-once."""
    mid = _running()
    # The ledger has never heard of it, so the reference records the marker itself.
    missions.append_event(mid, "approval", action_id="act-late-truth")
    assert (
        missions.settlements_for(["act-late-truth"])["act-late-truth"]["state"]
        == missions.SETTLEMENT_LOST
    )

    ledger.append({"id": "act-late-truth", "state": "proposed", "verb": "nudge"})
    ledger.transition("act-late-truth", "delivered", detail="landed")
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-late-truth"][0]
    assert ev["settlement"]["state"] == "delivered"
    assert ev["settlement"]["outcome"] == "landed"

    # …and a REAL settlement is still immutable.
    missions.record_settlement("act-late-truth", {"state": "failed", "detail": "second"})
    ev = [e for e in missions.get_mission(mid)["events"] if e["action_id"] == "act-late-truth"][0]
    assert ev["settlement"]["outcome"] == "landed"


def test_lookup_tells_absent_from_unreadable(stores):
    ledger.append({"id": "act-here", "state": "observed"})
    assert ledger.lookup("act-here")[0] == "found"
    assert ledger.lookup("act-nowhere")[0] == "absent"

    path = pathlib.Path(os.environ["AGENT_SESSIONS_ORCHESTRATOR_LEDGER"])
    real_read = pathlib.Path.read_text

    def _eio(self, *a, **k):
        if self == path:
            raise OSError("EIO")
        return real_read(self, *a, **k)

    pathlib.Path.read_text = _eio
    try:
        assert ledger.lookup("act-here")[0] == "unreadable"
    finally:
        pathlib.Path.read_text = real_read

    # A ledger that was never written has a genuine absence, not an unreadable one.
    path.unlink()
    assert ledger.lookup("act-here")[0] == "absent"


# --- round 9: the external-effect half of the lease ------------------------------------------


def test_foreign_process_lease_is_not_reclaimed_before_expiry(tmp_path, monkeypatch):
    """A lease from ANOTHER process is not proof that process died.

    The old rule reclaimed any lease whose owner epoch differed from ours, on the premise that a
    foreign epoch meant a *dead* process. With a second app instance serving the same store —
    which this app supports — a foreign epoch means a *live* one, and reclaiming it put two
    workers inside the same irreversible archive.
    """
    db = tmp_path / "m.db"
    m = missions.create_mission("recover", path=db)
    missions.adopt(m["id"], "claude:" + "a" * 32, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    verdict, token = missions.claim_session_teardown(m["id"], "claude:" + "a" * 32, path=db)
    assert verdict == "claimed"

    # Restamp the lease as another process's, fresh — exactly what a sibling instance leaves.
    con = missions._ready(db)
    con.execute(
        "UPDATE mission_sessions SET lease_owner='other-instance', lease_at=?", (time.time(),)
    )
    con.commit()
    con.close()

    assert missions.reopen_stale_leases(m["id"], path=db) == 0
    rows = missions.archive_sessions_for(m["id"], path=db)
    assert rows[0]["archive_state"] == "in_progress"
    # …and the reservation the live worker depends on is still standing.
    assert missions.reservation_of("claude:" + "a" * 32, path=db)["holder"] == f"mission:{m['id']}"

    # Only once it has gone quiet for the full window does it become reclaimable.
    assert (
        missions.reopen_stale_leases(
            m["id"], now=time.time() + missions.LEASE_MAX_AGE_S + 1, path=db
        )
        == 1
    )
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "pending"
    assert token  # the fencing token still exists; it just no longer wins


def test_holding_renews_a_claim_past_its_expiry(tmp_path):
    """A healthy-but-slow worker must not lose its claim to duration.

    Without the heartbeat the expiry was a bet that `cleanup_runtime` + `prov.archive` finish
    inside a fixed window. Asserted on the beat's own observable effect — the reservation's
    timestamp advancing — because a context manager that merely *wakes up* on schedule and renews
    nothing would satisfy any test that only asks whether some later caller is refused: the
    mission-ownership branch refuses that caller anyway, for a different reason entirely.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "b" * 32
    m = missions.create_mission("slow", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, token = missions.claim_session_teardown(m["id"], key, path=db)
    started = missions.reservation_of(key, path=db)["at"]

    async def _run():
        async with missions.holding(key, token, interval=0.02, path=db):
            await asyncio.sleep(0.3)
            return missions.reservation_of(key, path=db)["at"]

    beat_to = asyncio.run(_run())
    assert beat_to > started, "the claim was never renewed while the work ran"

    # …and that renewal is what keeps a rival out: probe as ANOTHER MISSION, which skips the
    # ownership branch, so the only thing that can refuse it is the reservation's own freshness.
    with pytest.raises(missions.SessionBusy):
        missions.reserve_session(key, "mission:msn_" + "0" * 32, now=started + 0.25, path=db)

    missions.settle_session_archive(m["id"], key, "done", token=token, path=db)
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "done"


def test_holding_stops_beating_once_fenced_out(tmp_path):
    """A definitive `False` from a renewal is not a transient failure — stop, don't push on.

    The failure this guards is a fenced-out holder that keeps beating and so keeps extending the
    expiry of the claim that REPLACED it, holding the session open on behalf of a worker that has
    already lost. Asserted on the replacement's timestamp standing still.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "c" * 32
    m = missions.create_mission("fenced", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, token = missions.claim_session_teardown(m["id"], key, path=db)

    async def _run():
        async with missions.holding(key, token, interval=0.02, path=db):
            await asyncio.sleep(0.05)
            missions.reopen_stale_leases(m["id"], now=time.time() + 10_000, path=db)
            _, fresh = missions.claim_session_teardown(m["id"], key, path=db)
            took_at = missions.reservation_of(key, path=db)["at"]
            await asyncio.sleep(0.25)  # many beats of the OLD holder's interval
            return fresh, took_at, missions.reservation_of(key, path=db)["at"]

    fresh, took_at, now_at = asyncio.run(_run())
    assert missions.reservation_of(key, path=db)["token"] == fresh
    assert now_at == took_at, "a fenced-out holder kept renewing the claim that replaced it"
    assert missions.renew_session(key, token, path=db) == missions.SUPERSEDED


def test_a_fenced_worker_publishes_no_outcome_event(tmp_path):
    """The guarded UPDATE refused the stale settlement — but the event was appended anyway.

    That let a worker whose CAS *lost* write `outcome: done` into an append-only timeline: a
    completion claimed by somebody who completed nothing.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "d" * 32
    m = missions.create_mission("stale-publish", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, stale = missions.claim_session_teardown(m["id"], key, path=db)

    missions.reopen_stale_leases(m["id"], now=time.time() + 10_000, path=db)
    verdict, fresh = missions.claim_session_teardown(m["id"], key, path=db)
    assert verdict == "claimed" and fresh != stale

    before = len(missions.get_mission(m["id"], path=db)["events"])
    for outcome in ("done", "failed"):
        missions.settle_session_archive(m["id"], key, outcome, reason="stale", token=stale, path=db)
    after = missions.get_mission(m["id"], path=db)
    assert len(after["events"]) == before, "a fenced-out worker published an outcome"
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "in_progress"
    # …and it did not release the winner's reservation out from under it, either.
    assert missions.reservation_of(key, path=db)["token"] == fresh

    missions.settle_session_archive(m["id"], key, "done", token=fresh, path=db)
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "done"


def test_a_fenced_restore_worker_publishes_no_outcome_event(tmp_path):
    """Same rule on the restore half — it had the same unconditional append."""
    db = tmp_path / "m.db"
    key = "claude:" + "e" * 32
    m = missions.create_mission("stale-restore", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, t = missions.claim_session_teardown(m["id"], key, path=db)
    missions.settle_session_archive(m["id"], key, "done", token=t, path=db)
    missions.finish_archive(m["id"], path=db)

    missions.begin_unarchive(m["id"], path=db)
    _, stale = missions.claim_session_restore(m["id"], key, path=db)
    missions.reopen_stale_leases(m["id"], now=time.time() + 10_000, path=db)
    verdict, fresh = missions.claim_session_restore(m["id"], key, path=db)
    assert verdict == "claimed" and fresh != stale

    before = len(missions.get_mission(m["id"], path=db)["events"])
    for outcome in ("restored", "restore_failed"):  # stale success AND stale failure
        missions.settle_session_restore(m["id"], key, outcome, reason="stale", token=stale, path=db)
    assert len(missions.get_mission(m["id"], path=db)["events"]) == before
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "restoring"
    assert missions.reservation_of(key, path=db)["token"] == fresh

    # …and the winner still settles normally: a successful restore CLEARS the archive state,
    # because the session is back in the live tree and has no archive state to be in.
    missions.settle_session_restore(m["id"], key, "restored", token=fresh, path=db)
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] is None


def test_a_settled_claim_reads_as_released_not_superseded(tmp_path, caplog):
    """Finishing normally is not a takeover, and must not be logged as one.

    Settlement runs INSIDE the claim — it has to, or a rival could take the session between the
    external effect and the record of it — so the very next heartbeat finds its own reservation
    gone. A single boolean made that indistinguishable from being reclaimed, which would have put
    "claim was reclaimed while still working" in the log on every completed teardown: an alarm
    that fires on success is an alarm nobody reads.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "f" * 32
    m = missions.create_mission("settled", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, token = missions.claim_session_teardown(m["id"], key, path=db)

    missions.settle_session_archive(m["id"], key, "done", token=token, path=db)
    assert missions.reservation_of(key, path=db) is None
    assert missions.renew_session(key, token, path=db) == missions.RELEASED

    # …and the beat that runs after a settlement inside the claim says nothing.
    async def _run():
        async with missions.holding(key, token, interval=0.02, path=db):
            await asyncio.sleep(0.12)

    with caplog.at_level("WARNING", logger="agent_sessions.missions"):
        asyncio.run(_run())
    assert not [r for r in caplog.records if "reclaimed" in r.message], caplog.text


def test_holding_beats_through_an_effect_that_blocks_the_event_loop(tmp_path):
    """The production call shape: the protected effect is SYNCHRONOUS.

    `prov.archive` is a file move plus a sidecar write, called inline — it never awaits. A
    heartbeat implemented as an asyncio task shares the loop thread with that effect, so the beat
    stops being scheduled the instant the effect begins: precisely when the claim most needs
    defending it is least able to defend it, and a rival reclaims the reservation mid-move.

    The earlier heartbeat test used `await asyncio.sleep`, which YIELDS to the beat — it passed
    against a task-based beat and so proved nothing about the real path. This one blocks with
    `time.sleep` and no await at all.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "9" * 32
    m = missions.create_mission("blocking", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, token = missions.claim_session_teardown(m["id"], key, path=db)
    started = missions.reservation_of(key, path=db)["at"]

    rival = "mission:msn_" + "1" * 32
    verdicts = {}

    async def _run():
        async with missions.holding(key, token, interval=0.02, path=db):
            time.sleep(0.35)  # BLOCKING, no await — the event loop cannot run anything
            verdicts["at"] = missions.reservation_of(key, path=db)["at"]
            # A rival probing at a moment well past a (shrunken) expiry must still be refused.
            try:
                missions.reserve_session(key, rival, now=started + 0.25, path=db)
                verdicts["rival"] = "took it"
            except missions.SessionBusy:
                verdicts["rival"] = "fenced"

    real = missions.RESERVATION_MAX_AGE_S
    try:
        missions.RESERVATION_MAX_AGE_S = 0.2
        asyncio.run(_run())
    finally:
        missions.RESERVATION_MAX_AGE_S = real

    assert verdicts["at"] > started, "the beat never ran while the effect blocked the loop"
    assert verdicts["rival"] == "fenced", "a rival reclaimed the claim during the effect"
    missions.settle_session_archive(m["id"], key, "done", token=token, path=db)
    assert missions.archive_sessions_for(m["id"], path=db)[0]["archive_state"] == "done"


def test_a_failed_heartbeat_retries_soon_not_a_full_interval_later(tmp_path, monkeypatch):
    """A failing beat must not spend the whole expiry budget waiting to try again.

    `renew_session` can sit through the connection's entire `busy_timeout` and then raise, so
    retrying on the ordinary cadence lets a few consecutive failures carry a LIVE holder past its
    own expiry — while it is still inside the destructive effect. Same shape as the scrub loop:
    the pacing that matters is the one that runs under the condition the retry exists for.

    Asserted on **when** the renewal lands, not on how many calls were made. A call count cannot
    tell the two policies apart — both eventually retry and both eventually succeed; only the
    delay differs. With two failures first, the slow policy needs three whole intervals and the
    fast one needs barely more than the first, so a deadline of two intervals separates them with
    margin in both directions.
    """
    db = tmp_path / "m.db"
    key = "claude:" + "8" * 32
    m = missions.create_mission("flaky-beat", path=db)
    missions.adopt(m["id"], key, path=db)
    missions.begin_archive(m["id"], abandon=True, path=db)
    _, token = missions.claim_session_teardown(m["id"], key, path=db)
    started = missions.reservation_of(key, path=db)["at"]

    real = missions.renew_session
    calls = {"n": 0}
    at: list[float] = []  # when each beat FIRED — the gaps between these are the policy

    def flaky(*a, **kw):
        calls["n"] += 1
        at.append(time.time())
        if calls["n"] <= 2:  # the first two beats fail, as a contended store would
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(missions, "renew_session", flaky)
    monkeypatch.setattr(missions, "RESERVATION_RETRY_S", 0.02)

    interval = 0.5

    async def _run():
        # `time.sleep`, not `await` — the production shape, and it also proves the beat is not on
        # this thread.
        #
        # Runs until FOUR attempts have been observed, not until the claim is first renewed. The
        # fourth is what makes this test mean anything: attempts 1-2 fail, 3 succeeds, and 4 is
        # the next beat AFTER a success — the only gap in the sequence that is an ordinary
        # cadence produced by this same run under this same load.
        async with missions.holding(key, token, interval=interval, path=db):
            t0 = time.time()
            while time.time() - t0 < interval * 12:
                if len(at) >= 4:
                    return True
                time.sleep(0.01)
            return False

    got_four = asyncio.run(_run())
    assert got_four, f"the beat did not produce four attempts (saw {len(at)})"
    assert missions.reservation_of(key, path=db)["at"] > started, "the claim was never renewed"

    # Compared as a RATIO between two gaps THE BEAT ITSELF produced, never against a wall-clock
    # budget and never against startup.
    #
    # The absolute version (`took < interval * 2`) failed in CI at 1.14s against a 1.0s bound:
    # the fast policy needs ~0.54s and the slow one ~1.5s, so the bound sat only 0.46s above the
    # expected value — inside the scheduling jitter a loaded 40-minute suite produces.
    #
    # The first ratio attempt was worse, and wrong in a way that mattered: it used
    # `at[0] - t_start` as the reference cadence. That is startup → the FIRST attempt, and the
    # first attempt FAILS — so it was never a successful-beat cadence at all, only thread-start
    # latency. Inflating it (a slow first attempt) inflates only the denominator, and the buggy
    # `wait = interval` policy passes. Caught in review, reproduced as a false green.
    #
    # Both gaps below are waits INSIDE the beat loop, taken moments apart under identical load:
    #
    #   at[1] - at[0]  gap after a FAILED beat     -> the retry cadence, the behaviour under test
    #   at[3] - at[2]  gap after a SUCCESSFUL beat -> the ordinary cadence, measured not assumed
    #
    # A policy that retried on the ordinary cadence makes these EQUAL; the fix makes the first
    # ~25x smaller. Half is a wide margin between those, and no amount of load can close it,
    # because load stretches both.
    retry_gap = at[1] - at[0]
    cadence_gap = at[3] - at[2]
    assert retry_gap < cadence_gap / 2, (
        f"a failed beat cost a full interval: retried after {retry_gap:.3f}s against this run's "
        f"own successful-beat cadence of {cadence_gap:.3f}s — only the "
        f"retry-on-the-ordinary-cadence policy is slow enough to produce that"
    )


# ---- the sub-agent cap under concurrent approvals (#894) ---------------------------


def test_two_concurrent_SPAWN_approvals_produce_exactly_one_agent(stores):
    """#894. Two approvals racing produce ONE sub-agent, and the loser is told.

    **What makes this true is the reservation, not the count.** The slot a spawn reserves IS the
    `mission_dispatches` row, and that table is keyed by `mission_id` — so the second writer
    cannot create one, whatever any cap arithmetic said a moment earlier. "Count the sub-agents,
    then start one" would be check-then-act; making the reservation the arbiter removes the
    check-then-act rather than timing it better.

    **This test does NOT prove the count's placement**, and saying so matters more than a
    confident sentence would: moving the `COUNT(*)` out of the reservation's transaction leaves
    this test green, because the primary key still refuses the second insert. Verified — the
    mutant passed. The count sits inside `BEGIN IMMEDIATE` to close a different window (an
    `adopt` landing between counting and inserting), and that window is held shut by SQLite's
    write lock rather than by anything this test can observe.

    What this DOES pin: exactly one winner, one durable reservation, and the mission transited
    once.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def _spawn():
        start.wait(5)
        try:
            missions.claim_spawn(
                mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="review it"
            )
            outcomes.append("won")
        except missions.MissionError:
            outcomes.append("lost")

    ts = [threading.Thread(target=_spawn) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)

    assert sorted(outcomes) == ["lost", "won"], outcomes
    # …and the durable record agrees: ONE reservation, not two.
    row = missions.get_dispatch(mid)
    assert row is not None and row["spawn_parent"] == CLAUDE_A
    assert missions.get_mission(mid)["state"] == "dispatching"


def test_RELEASING_a_sub_agent_does_NOT_free_its_slot(stores):
    """A slot is a statement about THIS HOST, and release stops nothing.

    This test previously asserted the opposite — that detaching a child freed its budget — and it
    passed, because the cap counted `mission_sessions` where `removed_at IS NULL`. That was the
    defect, not the contract (review 1, finding 4): `detach` is an ownership operation that
    deliberately does not touch the process, so "released" and "stopped" are different facts and
    only the second one may return capacity. Counting the roster made the bound evadable by
    spawn -> release -> spawn, without limit, while every one of those agents was still running.

    The budget is the `mission_spawns` ledger, closed by evidence only.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)

    # Fill the cap through the CLAIM, which is what reserves ledger rows. Adopting a session with
    # `role="sub"` is roster bookkeeping and deliberately does NOT consume the budget: nothing was
    # launched, so nothing is occupying the host.
    claimed = []
    for i in range(missions.SPAWN_CAP):
        c = missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief=f"child {i}"
        )
        claimed.append(c)
        key = f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-1111-1111-1111-111111111111"
        missions.settle_dispatch(mid, to="running", detail="up", session_key=key)

    assert missions.open_spawn_count(mid) == missions.SPAWN_CAP

    with pytest.raises(missions.MissionError) as at_cap:
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="one too many"
        )
    assert at_cap.value.status == 409

    # RELEASE the first child. The roster lets go; the ledger does not, because nobody has shown
    # the process stopped.
    first_key = "claude:00000000-1111-1111-1111-111111111111"
    missions.detach(mid, first_key)
    assert (
        missions.open_spawn_count(mid) == missions.SPAWN_CAP
    ), "releasing a sub-agent freed its slot while its process may still be running"
    with pytest.raises(missions.MissionError) as still:
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="after the release"
        )
    assert still.value.status == 409

    # Only EVIDENCE returns the slot.
    assert missions.close_spawn(claimed[0]["plan_id"], reason="observed dead") is True
    assert missions.open_spawn_count(mid) == missions.SPAWN_CAP - 1
    again = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the replacement"
    )
    assert again["spawn_parent"] == CLAUDE_A


def test_RE_ADOPTING_a_child_cannot_launder_it_out_of_the_budget(stores):
    """`_adopt_tx` refreshes `role` and `spawned_by` on a session the mission already holds, so
    adopting a live child through the ordinary adopt route rewrote its provenance to NULL and
    dropped it out of a roster-based count without releasing anything (review 1, finding 4, second
    path). The ledger is keyed on the attempt, so the adopt route cannot reach it.

    **Asserted on the ENFORCED cap, not on `open_spawn_count`.** An earlier draft of this test
    checked the helper, which reads the ledger directly — so it stayed green against a mutant that
    put the cap back on the roster, and proved nothing about the thing under test. The claim
    refusing is the behaviour; the count is only how it is reached.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)

    keys = []
    for i in range(missions.SPAWN_CAP):
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief=f"child {i}"
        )
        key = f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-2222-2222-2222-222222222222"
        keys.append(key)
        missions.settle_dispatch(mid, to="running", detail="up", session_key=key)

    # At the cap, as both the ledger and the claim agree.
    with pytest.raises(missions.MissionError):
        missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="full")

    # THE LAUNDERING MOVE: re-adopt every child as a plain primary, erasing `spawned_by`.
    for k in keys:
        missions.adopt(mid, k)

    # The budget must be untouched — the processes are all still running.
    with pytest.raises(missions.MissionError) as e:
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="laundered"
        )
    assert e.value.status == 409
    assert missions.open_spawn_count(mid) == missions.SPAWN_CAP


def test_the_PRIMARY_does_not_consume_a_sub_agent_slot(stores):
    """The mission's own dispatched session is not one of its sub-agents (review 1, finding 5).

    `settle_dispatch` adopts the primary with `spawned_by="dispatch"` — a literal, not a session
    key — so a cap that counted "every non-null `spawned_by`" charged the mission for its own
    agent. With the default cap of 2 that made "a primary plus one child" look like two children;
    at a cap of 1 a normally dispatched mission could never spawn at all, which is the shipped
    default's neighbour and the case an operator would hit first.
    """
    mid = _running()
    # A PRIMARY, adopted exactly as a real dispatch settles one.
    missions.adopt(mid, CLAUDE_A, role="primary", spawned_by="dispatch")
    assert missions.open_spawn_count(mid) == 0, "the mission's own session was charged to the cap"

    # Its first child must be reachable no matter how small the cap is.
    claimed = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the first child", cap=1
    )
    assert claimed["spawn_parent"] == CLAUDE_A


def test_an_UPGRADED_store_back_fills_the_reservation_state(tmp_path, monkeypatch):
    """v23 rows carry no state, and what they are back-filled to decides whether they get reaped.

    The conservative reading is the only safe one: a key means the launch got at least as far as
    minting one (`live`), no key means it never did (`reserved`). Neither is a claim about a
    process — and neither of the two OPEN states is probeable, so an upgraded store cannot have a
    pre-existing row freed on the strength of a socket that was never ours to begin with.
    """
    import sqlite3

    db = tmp_path / "m.db"
    missions._ready(db).close()
    con = sqlite3.connect(db)
    con.execute("DROP TABLE mission_spawns")
    con.execute(
        "CREATE TABLE mission_spawns (plan_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL,"
        " parent_key TEXT NOT NULL, session_key TEXT, started_at REAL NOT NULL,"
        " ended_at REAL, end_reason TEXT)"
    )
    con.executemany(
        "INSERT INTO mission_spawns VALUES (?,?,?,?,?,?,?)",
        [
            ("p1", "m1", "k", None, 1.0, None, None),
            ("p2", "m1", "k", "claude:x", 1.0, None, None),
            ("p3", "m1", "k", "claude:y", 1.0, 2.0, "done"),
        ],
    )
    con.execute("PRAGMA user_version=23")
    con.commit()
    con.close()

    # `_ready` runs the ladder ONCE per path and remembers. Without this the upgrade never runs
    # and the test asserts against the store it built a moment ago — green, and about nothing.
    missions.reset_schema_cache_for_test()
    c2 = missions._ready(db)
    try:
        got = {
            r["plan_id"]: r["state"]
            for r in c2.execute("SELECT plan_id, state FROM mission_spawns")
        }
        assert got == {"p1": "reserved", "p2": "live", "p3": "ended"}, got
    finally:
        c2.close()


def test_a_FRESH_store_has_the_spawn_ledger(tmp_path):
    """A table that exists only inside a migration step is created on UPGRADED stores and missing
    on new ones, and every test on a fresh temp store passes either way.

    That asymmetry actually happened here: `_migrate` stamps `user_version` straight to
    `SCHEMA_VERSION` when the store is new, so the ladder never ran and `mission_spawns` was absent
    on a fresh install while present on an upgraded one. Caught by creating a store and looking,
    which is the only thing that distinguishes the two.
    """
    db = tmp_path / "m.db"
    con = missions._ready(db)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "mission_spawns" in names
        cols = {r[1] for r in con.execute("PRAGMA table_info(mission_spawns)")}
        assert {"plan_id", "mission_id", "parent_key", "session_key", "ended_at"} <= cols
    finally:
        con.close()


def test_a_CHILD_refusal_does_not_rewind_the_running_parent(stores):
    """An ordinary child refusal must end the ATTEMPT, not the mission (review 1, finding 2).

    `settle_dispatch` was written for the mission's own launch, and `to="planned"` is that path's
    honest answer to "nothing started, offer the plan again". Applied to a spawn it did three
    wrong things at once: it moved a mission that was legitimately `running` — with a live roster
    that never stopped — back to `planned`; it republished the CHILD's brief as the mission's own
    proposal; and it dropped `spawn_parent`, so the next DISPATCH would have launched the
    sub-agent's brief as a new primary.

    The parent is untouched and the child's brief stays the child's.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the CHILD brief"
    )
    assert missions.get_mission(mid)["state"] == "dispatching"

    # The refusal a real over-limit brief or a withdrawn policy produces: nothing was spawned.
    missions.settle_dispatch(mid, to="planned", detail="the brief was refused")

    m = missions.get_mission(mid)
    assert m["state"] == "running", f"a child's refusal rewound the parent mission to {m['state']}"
    plan = missions.get_plan(mid)
    assert plan is None or "CHILD brief" not in str(
        plan.get("brief") or ""
    ), "the child's brief was republished as the mission's own proposal"


def test_only_a_PROVEN_DEAD_child_gives_its_slot_back(stores, monkeypatch):
    """`UNKNOWN` is not `DEAD`, and a resource budget is where that distinction pays.

    `probe_master` is tri-state deliberately: on a starved host a live master can be too slow to
    accept within budget, and that answers `UNKNOWN`. Reading it as dead would hand capacity back
    to a process that is still running — which is the fan-out this cap exists to bound. So the
    reaper frees a slot on `DEAD` and on nothing else.
    """
    from agent_sessions import ptybridge
    from agent_sessions.routes import missions as routes

    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:0b0b0b0b-3333-3333-3333-333333333333"
    missions.settle_dispatch(mid, to="running", detail="up", session_key=key)
    assert missions.open_spawn_count(mid) == 1

    # A probe that cannot decide must NOT return the slot.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.UNKNOWN)
    assert routes._reap_dead_spawns(mid) == 0
    assert missions.open_spawn_count(mid) == 1, "an UNKNOWN probe freed a live agent's slot"

    # A probe that raises is not evidence either.
    def _boom(_p):
        raise OSError("probe exploded")

    monkeypatch.setattr(ptybridge, "probe_master", _boom)
    assert routes._reap_dead_spawns(mid) == 0
    assert missions.open_spawn_count(mid) == 1

    # Only DEAD.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.DEAD)
    assert routes._reap_dead_spawns(mid) == 1
    assert missions.open_spawn_count(mid) == 0
    # …and the slot is genuinely reusable.
    again = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the next one"
    )
    assert again["spawn_parent"] == CLAUDE_A


def test_a_NEW_refusal_cannot_discharge_an_OLDER_child_s_obligation(stores):
    """A reservation belongs to the attempt that made it (review 2, finding 1).

    The first version of this discharge closed **every** keyless row on the mission, so an
    unrelated later refusal wrote off an older child that had launched, never reached adoption, and
    was still running. Both missions' budgets went to zero while one of them still held a process.
    A resource ledger that can be cleared by something other than the thing it is accounting for is
    not a ledger.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)

    # ATTEMPT ONE launches and mints a key, but never reaches adoption — the keyless-row case.
    first = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the older child"
    )
    key = "claude:0c0c0c0c-4444-4444-4444-444444444444"
    missions.note_dispatch_session(mid, key, expect_plan=first["plan_id"])
    missions.note_spawn_session(first["plan_id"], key)
    # It fails, and the cleanup finds ANOTHER mission has adopted the child: `spared`. The
    # dispatch record is discharged — somebody answers for that agent — but the process is still
    # on this host, so the slot must stay held. That is what makes the mission free to try again
    # while an older obligation is still open, which is the whole setup for this defect.
    missions.settle_dispatch(
        mid, to="failed", detail="could not prove it stopped", keep_record=True
    )
    missions.clear_dispatch(mid, expect_plan=first["plan_id"], stopped=False)
    assert missions.open_spawn_count(mid) == 1

    # ATTEMPT TWO is refused outright before anything spawns.
    second = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="the newer child"
    )
    assert second["plan_id"] != first["plan_id"]
    missions.settle_dispatch(mid, to="planned", detail="the brief was refused")

    assert (
        missions.open_spawn_count(mid) == 1
    ), "a refused new attempt discharged an older, still-running child's slot"


def test_a_CHILD_failure_does_not_terminalize_the_running_parent(stores):
    """`failed` is the mission's own launch answering for itself; for a child it is not.

    The first pass made only `planned` spawn-aware, so a start-evidence timeout — the single most
    likely child outcome on a real host — took the whole parent mission terminal with it. Adding an
    optional reviewer could end the mission it was meant to help, and remove every running-only
    control with it.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="a reviewer")
    missions.settle_dispatch(mid, to="failed", detail="the session never registered within 90s")
    m = missions.get_mission(mid)
    assert m["state"] == "running", f"a child's failure took the parent to {m['state']}"
    # …and the parent still holds what it held.
    assert CLAUDE_A in missions.active_session_keys(mid)


def test_a_cleaned_up_failure_frees_its_slot_but_a_SPARED_one_does_not(stores):
    """Proved-stopped returns capacity; spared is a transfer, not a discharge (finding 3).

    A launch that failed and was then cleaned up left a keyless reservation forever: the reaper
    skips keyless rows because there is nothing to probe, so repeated cleaned-up failures exhausted
    the cap with no live children anywhere. `spared` must NOT free it — that agent is still on this
    host, another mission answers for it, and its cost did not disappear because the owner changed.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)

    stopped = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="one"
    )
    missions.settle_dispatch(mid, to="failed", detail="did not start", keep_record=True)
    assert missions.open_spawn_count(mid) == 1
    missions.clear_dispatch(mid, expect_plan=stopped["plan_id"], stopped=True)
    assert missions.open_spawn_count(mid) == 0, "a proved-stopped launch kept its slot for ever"

    spared = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="two"
    )
    # A SPARED CHILD HAS A SESSION — that is what "another mission adopted it" means. An earlier
    # draft of this test never minted a key, which models a process that cannot exist: the rule
    # correctly discharged it as never-launched, and the failure was the test's premise, not the
    # code's. Minting the key is what makes this the spared case rather than a cancelled one.
    missions.note_dispatch_session(
        mid, "claude:2a2a2a2a-dddd-dddd-dddd-dddddddddddd", expect_plan=spared["plan_id"]
    )
    missions.settle_dispatch(mid, to="failed", detail="did not start", keep_record=True)
    missions.clear_dispatch(mid, expect_plan=spared["plan_id"], stopped=False)
    assert (
        missions.open_spawn_count(mid) == 1
    ), "a SPARED agent's slot was returned; the process is still running under another owner"


def test_an_AT_CAP_mission_reclaims_its_finished_children_without_being_admitted(
    stores, monkeypatch
):
    """Fill the cap, let the children stop, refresh — the budget must come back (finding 4).

    The reaper's only production caller was `POST /spawn`, and the console disables the control
    that issues it once the count reaches the cap. So a mission whose children had all finished
    normally stayed at the cap for ever: the count never refreshed, because the only thing that
    refreshed it was the thing the count disabled. A deadlock built out of two correct halves.

    **This test does NOT prove the deadlock is fixed, and saying so matters.** It calls the reaper
    directly, so it would pass just as happily against the shipped code where the route never
    reaches that call at the cap. What it pins is narrower and still worth having: that the
    reclaim mechanism works on a full cap and returns the capacity. The route-level proof — fill,
    stop, **GET the detail**, assert the budget came back — is
    `test_AT_CAP_the_detail_route_itself_reclaims_finished_children` in `test_missions_api.py`,
    which drives the read path an operator actually triggers.
    """
    from agent_sessions import ptybridge
    from agent_sessions.routes import missions as routes

    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    for i in range(missions.SPAWN_CAP):
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief=f"child {i}"
        )
        missions.settle_dispatch(
            mid,
            to="running",
            detail="up",
            session_key=f"claude:{i}{i}{i}{i}{i}{i}{i}{i}-5555-5555-5555-555555555555",
        )
    assert missions.open_spawn_count(mid) == missions.SPAWN_CAP
    with pytest.raises(missions.MissionError):
        missions.claim_spawn(
            mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="blocked at cap"
        )

    # The children finish, as children do. Nothing else happens — no operator action, because at
    # the cap there is no control left to press.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.DEAD)

    # The READ path reconciles: this is what the detail route calls when it reports the budget.
    freed = routes._reap_dead_spawns(mid)
    assert freed == missions.SPAWN_CAP, "finished children were not reclaimed"
    assert missions.open_spawn_count(mid) == 0

    # …and the mission can spawn again without anyone having released anything.
    again = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="after they finished"
    )
    assert again["spawn_parent"] == CLAUDE_A


def test_a_LAUNCHING_reservation_is_never_reaped(stores, monkeypatch):
    """The reaper must not free a slot whose agent has not started YET (review 3, finding 1).

    The identity is recorded at `on_key`, which runs BEFORE the spawn — deliberately, so the record
    can never be behind reality. That leaves a window where a key exists and a socket does not, and
    `probe_master` on a missing socket answers DEAD. A concurrent detail GET at the cap, or another
    POST's pre-admission reaper, therefore read "no socket" as "the agent died" and discharged the
    reservation. The launch then succeeded, and adoption could not repair it because adoption only
    updates rows that are still open.

    A state column is what makes this expressible: `launching` is not probeable, so the question is
    never asked in the window where its answer is guaranteed wrong.
    """
    from agent_sessions import ptybridge
    from agent_sessions.routes import missions as routes

    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:0d0d0d0d-7777-7777-7777-777777777777"

    # `on_key`: the key is minted; the process does not exist yet.
    missions.note_spawn_session(c["plan_id"], key)
    assert missions.open_spawn_count(mid) == 1

    # A concurrent reaper runs in exactly that window. Every probe would say DEAD.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.DEAD)
    assert routes._reap_dead_spawns(mid) == 0, "a launching reservation was reaped"
    assert (
        missions.open_spawn_count(mid) == 1
    ), "the slot of an agent that had not started yet was freed"

    # The launch then succeeds, and the accounting is still right.
    missions.settle_dispatch(mid, to="running", detail="up", session_key=key)
    assert missions.open_spawn_count(mid) == 1
    with pytest.raises(missions.MissionError):
        for _ in range(missions.SPAWN_CAP):
            missions.claim_spawn(
                mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="over"
            )


def test_a_SPARED_child_keeps_its_slot_through_RECOVERY(stores):
    """Discharging the dispatch record is not discharging the resource (review 3, finding 3).

    Startup recovery treats `spared` as clean — correctly, because somebody answers for that agent:
    another mission adopted it. But `spared` means the process is still running, under a new owner,
    and settling with the record discharged also freed the originating mission's slot. It could
    then spawn again beside a child it no longer knew about.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:0e0e0e0e-8888-8888-8888-888888888888"
    missions.note_spawn_session(c["plan_id"], key)

    # Recovery's `spared` settlement: the record is over, the process is not.
    missions.settle_dispatch(
        mid,
        to="failed",
        detail="another mission has since adopted that session",
        discharge_resource=False,
    )
    assert (
        missions.open_spawn_count(mid) == 1
    ), "a spared child's slot was returned while its process was still running"


def test_a_CANCELLED_pre_key_launch_gives_its_slot_back(stores):
    """…and the mirror case, which must NOT hold a slot for ever (review 3, finding 4).

    A cancellation before `on_key` leaves a `reserved` row with no key. The reaper skips it by
    design — there is nothing to probe — so without an explicit discharge nothing ever closes it
    and repeating the cancellation exhausts the budget with no agent anywhere.

    The discharge refuses any row that got as far as a key, so it can never free a reservation
    whose process might exist. That refusal is the half that makes this safe, so it is asserted
    here too rather than trusted.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="cancelled")
    assert missions.open_spawn_count(mid) == 1
    # PRODUCTION'S OWN ORDER: settle first — that is what reads `spawn_parent` and keeps the
    # parent `running` — and only then clear the record. Clearing first would delete the row the
    # settlement needs to know this was a spawn at all, and the parent would be rewound to
    # `planned`. An earlier draft of this test did exactly that and failed for that reason.
    missions.settle_dispatch(mid, to="planned", detail="cancelled before the key was minted")
    assert missions.get_mission(mid)["state"] == "running"
    # The settlement ALREADY reconciled it — that is the point of one rule at every exit. There is
    # no separate discharge helper any more, and no second write to fail on its own (review 6,
    # finding 2). A `reserved` row with no key is discharged unconditionally, because nothing can
    # be running behind a row that never named a session.
    assert missions.open_spawn_count(mid) == 0
    assert missions.get_dispatch(mid) is None

    # And it REFUSES a row that minted a key — a process may exist behind that one.
    d = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="launched"
    )
    key2 = "claude:0f0f0f0f-9999-9999-9999-999999999999"
    missions.note_dispatch_session(mid, key2, expect_plan=d["plan_id"])
    # A KEYED row is NOT discharged as "never launched" — a process may exist behind it. With
    # `stopped=False` it keeps its charge and becomes reapable instead.
    missions.clear_dispatch(mid, expect_plan=d["plan_id"], stopped=False)
    assert missions.open_spawn_count(mid) == 1


def test_a_SPARED_child_becomes_reapable_so_its_slot_returns_when_it_dies(stores, monkeypatch):
    """Keeping a spared child's charge is right; keeping it FOR EVER is not (review 4, finding 2).

    `spared` means another mission took the agent over — still running, still on this host — so the
    slot must not come back yet. But the reservation was left `launching`, and `open_spawns`
    deliberately never returns that state, so nothing could ever probe the process to notice it had
    died. The charge was permanent: repeat it and the cap is exhausted with no live children.

    The launch attempt being over is itself the transition. `live` here does not claim this mission
    owns the session — it claims the row is a question worth asking, which is all the reaper needs.
    """
    from agent_sessions import ptybridge
    from agent_sessions.routes import missions as routes

    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:1a1a1a1a-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    missions.note_dispatch_session(mid, key, expect_plan=c["plan_id"])

    # SPARED: the record is discharged, the charge is kept.
    missions.clear_dispatch(mid, expect_plan=c["plan_id"], stopped=False)
    assert missions.open_spawn_count(mid) == 1, "a spared child's slot was returned too early"

    # NO SECOND CALL. `clear_dispatch` made the row reapable in the SAME transaction that deleted
    # the record — that atomicity is the fix for review 5's finding 2, where a crash between two
    # writes left the reservation unreachable AND no dispatch for recovery to repair.

    # Now that the process really dies, the capacity comes back.
    monkeypatch.setattr(ptybridge, "probe_master", lambda _p: ptybridge.DEAD)
    assert routes._reap_dead_spawns(mid) == 1, "a spared child's slot never became reclaimable"
    assert missions.open_spawn_count(mid) == 0


def test_the_identity_writes_are_ONE_transaction(stores):
    """A dispatch naming a session and a reservation that does not is the divergence the record
    exists to make impossible (review 4, carry-forward).

    These used to be two writes — the dispatch stamp, and a best-effort ledger stamp beside it — so
    a crash between them left exactly that split, in the one window where a process may already be
    starting. One transaction now covers both.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:1b1b1b1b-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert missions.note_dispatch_session(mid, key, expect_plan=c["plan_id"]) is True

    # Both records name the same session, from the one call.
    assert missions.get_dispatch(mid)["session_key"] == key
    rows = [r for r in missions.open_spawns(mid)] + [
        r for r in _spawn_rows(mid) if r["plan_id"] == c["plan_id"]
    ]
    assert any(
        r.get("session_key") == key for r in rows
    ), "the dispatch was stamped but the reservation was not"


def _spawn_rows(mission_id):
    """Every reservation, whatever its state — `open_spawns` filters to `live` by design."""
    con = missions._ready()
    try:
        return [
            {"plan_id": r["plan_id"], "session_key": r["session_key"], "state": r["state"]}
            for r in con.execute(
                "SELECT plan_id, session_key, state FROM mission_spawns WHERE mission_id=?",
                (mission_id,),
            )
        ]
    finally:
        con.close()


def test_the_dispatch_DELETE_and_the_resource_transition_are_ONE_commit(stores, monkeypatch):
    """A crash between the two writes must not strand the obligation (review 5, finding 2).

    The transition used to be a second call after `clear_dispatch` had already committed, with its
    exceptions suppressed. A failure there left the reservation in `launching` — which the reaper
    deliberately never returns — **and** no dispatch record for a later recovery pass to find. The
    obligation became unreachable by construction, which is worse than the leak it was fixing.

    Injecting a store failure at the transition must therefore roll the DELETE back too: either
    both land or neither does, so recovery still has something to repair.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:1c1c1c1c-cccc-cccc-cccc-cccccccccccc"
    missions.note_dispatch_session(mid, key, expect_plan=c["plan_id"])
    assert missions.get_dispatch(mid) is not None

    real = missions._ready

    class _Proxy:
        """`sqlite3.Connection.execute` is read-only on an instance, so wrap rather than patch."""

        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a, **kw):
            if "UPDATE mission_spawns SET state='live'" in sql:
                raise sqlite3.OperationalError("injected failure at the transition")
            return self._con.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._con, name)

    def explode_on_transition(path=None):
        return _Proxy(real(path))

    monkeypatch.setattr(missions, "_ready", explode_on_transition)
    with pytest.raises(sqlite3.OperationalError):
        missions.clear_dispatch(mid, expect_plan=c["plan_id"], stopped=False)
    monkeypatch.setattr(missions, "_ready", real)

    # THE DELETE ROLLED BACK WITH IT. Recovery can still find this attempt and finish the job.
    assert (
        missions.get_dispatch(mid) is not None
    ), "the dispatch was deleted while its resource transition failed, so nothing can repair it"
    assert missions.open_spawn_count(mid) == 1


def test_a_PRESENT_session_is_not_proof_of_termination(stores):
    """Recovery that finds the session ALIVE must not discharge its slot (review 7).

    The `present` branch schedules an adoption and stops nothing — but `resource_stopped` defaulted
    to True, and once the moved-state early return began reconciling, that default became a claim
    nobody had made. A mission moving out of `dispatching` while the store lookup was in flight
    therefore took that return, refused the adoption, and freed a live child's slot: no owner, no
    recovery record, no teardown, and the originating mission's budget back to zero.

    Driven through `settle_dispatch` with the flag the adoption path actually carries now, because
    the defect was entirely in what that flag defaulted to.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    c = missions.claim_spawn(mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="child")
    key = "claude:3a3a3a3a-eeee-eeee-eeee-eeeeeeeeeeee"
    missions.note_dispatch_session(mid, key, expect_plan=c["plan_id"])
    assert missions.open_spawn_count(mid) == 1

    # The mission moves out of `dispatching` under the recovery pass — which is what makes the
    # settlement take its early return instead of the main path.
    missions.set_state(mid, "dispatching", "running")
    # …and the settlement takes the early return with the adoption path's own outcome.
    missions.settle_dispatch(
        mid,
        to="failed",
        detail="the session started but there is no record that it received its brief",
        session_key=key,
        discharge_resource=False,
    )

    assert (
        missions.open_spawn_count(mid) == 1
    ), "a live child's slot was freed on a session that was PRESENT, not stopped"


def test_a_spawn_cannot_be_parented_to_a_session_this_mission_does_not_hold(stores):
    """The tree has to be true. A parent key from somewhere else would put an unrelated session's
    id into the roster's provenance, and `spawned_by` is where "whose sub-agent is this" is
    answered afterwards."""
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    with pytest.raises(missions.MissionError) as e:
        missions.claim_spawn(
            mid,
            parent_key="claude:99999999-9999-9999-9999-999999999999",
            engine="claude",
            cwd="/repo",
            brief="review it",
        )
    assert e.value.status == 409
    assert missions.get_dispatch(mid) is None, "a refused spawn left a reservation behind"


# ---- a primary claim landing on an unresolved spawn attempt (#894 review 10) --------


def _spawn_then_replan(mid):
    """The legal sequence that puts a PRIMARY claim on top of an outstanding SPAWN row.

    `claim_spawn` requires `running` and writes the dispatch record; `set_state` may then legally
    take the mission back to `planned`, at which point `claim_plan`'s upsert is reachable with the
    child's row still there. Returns the spawn's claim.
    """
    claim = missions.claim_spawn(
        mid, parent_key=CLAUDE_A, engine="claude", cwd="/repo", brief="review it"
    )
    missions.set_state(mid, "dispatching", "planned")
    missions.put_plan(mid, project_id=None, cwd="/repo", engine="claude", brief="the primary work")
    return claim


def test_a_PRIMARY_claim_never_inherits_the_parentage_of_the_spawn_it_replaces(stores):
    """`claim_plan`'s upsert rewrites only the columns it names (review 10).

    `spawn_parent` was not one of them, so a primary dispatch landing on an outstanding spawn row
    kept the child's parentage — and settlement, which reads exactly that column to tell a child
    from the mission's own launch, then took the CHILD branch: a pre-launch refusal left the
    mission `running` and never restored the approved primary plan, so the operator's plan was
    gone and the mission looked like it was still working.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    _spawn_then_replan(mid)

    plan = missions.get_plan(mid)
    missions.claim_plan(mid, plan["plan_id"])

    row = missions.get_dispatch(mid)
    assert row is not None
    assert not row["spawn_parent"], (
        "the new PRIMARY dispatch inherited the replaced child's parentage, so its settlement "
        "will follow the sub-agent branch"
    )

    # …and the settlement proves it, through the real refusal path rather than by reading a
    # column: a primary refusal returns the MISSION to `planned` and republishes its plan.
    missions.settle_dispatch(mid, to="planned", detail="the brief was rejected")
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_plan(mid) is not None, "the approved primary plan was not restored"


def test_a_KEYLESS_spawn_replaced_by_a_primary_claim_gives_its_SLOT_back(stores):
    """The replaced row is the only reference to its resource obligation (review 10).

    Overwriting it stranded the charge: `open_spawns` selects `live`, the reaper skips `reserved`,
    and no dispatch named that attempt any more — so nothing could ever reclaim the slot, even
    though nothing had launched. Round 9's rule one caller further out: an obligation dropped
    because something else wrote over it is one nobody ever discharges.

    Keyless is the case that can be discharged by construction — a row that never named a session
    cannot have a process behind it.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    claim = _spawn_then_replan(mid)
    assert missions.open_spawn_count(mid) == 1

    plan = missions.get_plan(mid)
    missions.claim_plan(mid, plan["plan_id"])

    assert missions.open_spawn_count(mid) == 0, (
        "the replaced spawn's slot stayed charged with nothing running and no dispatch left to "
        "reference it — unreachable by the reaper and by recovery alike"
    )
    # Discharged, not merely forgotten: the ledger row is closed and names why.
    assert missions.close_spawn(claim["plan_id"], reason="late") is False


def test_a_KEYED_spawn_is_NOT_replaced_by_a_primary_claim(stores):
    """A minted key means an agent may be running, and this row is its only cleanup record.

    There is nowhere to preserve that obligation across the replacement, so the replacement is
    refused rather than the record being dropped — the same direction every other uncertain case
    in this module takes. Settling it first is what unblocks the claim, which the second half
    asserts so the refusal cannot be a dead end.
    """
    mid = _running()
    missions.adopt(mid, CLAUDE_A)
    claim = _spawn_then_replan(mid)
    key = "claude:99999999-9999-4999-8999-999999999999"
    assert missions.note_dispatch_session(mid, key, expect_plan=claim["plan_id"])

    plan = missions.get_plan(mid)
    with pytest.raises(missions.MissionError) as blocked:
        missions.claim_plan(mid, plan["plan_id"])
    assert blocked.value.status == 409
    assert "accounted for" in str(blocked.value)
    # The child's record and charge both survive the refusal — that is the point of refusing.
    assert missions.get_dispatch(mid) is not None
    assert missions.open_spawn_count(mid) == 1

    # …and it is not a dead end: once the attempt is settled, the same claim succeeds.
    missions.clear_dispatch(mid, expect_plan=claim["plan_id"], stopped=True)
    missions.claim_plan(mid, plan["plan_id"])
    row = missions.get_dispatch(mid)
    assert row is not None and not row["spawn_parent"]
    assert missions.open_spawn_count(mid) == 0
