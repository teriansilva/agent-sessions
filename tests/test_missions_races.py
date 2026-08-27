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

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= 2:  # the first two beats fail, as a contended store would
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **kw)

    monkeypatch.setattr(missions, "renew_session", flaky)
    monkeypatch.setattr(missions, "RESERVATION_RETRY_S", 0.02)

    interval = 0.5

    async def _run():
        # `time.sleep`, not `await` — the production shape, and it also proves the beat is not on
        # this thread. The first beat is always a full interval away whatever the policy; what is
        # being measured is what the two failures AFTER it cost.
        async with missions.holding(key, token, interval=interval, path=db):
            t0 = time.time()
            while time.time() - t0 < interval * 6:
                if missions.reservation_of(key, path=db)["at"] > started:
                    return time.time() - t0
                time.sleep(0.01)
            return None

    took = asyncio.run(_run())
    assert took is not None, "the claim was never renewed at all"
    assert calls["n"] >= 3, "the beat did not retry after failing"
    # Slow policy needs 3 intervals (1.5s); fast needs ~1 (0.5s) plus two 20ms retries.
    assert took < interval * 2, (
        f"a failed beat cost a full interval: renewed after {took:.2f}s, "
        f"which only the retry-on-the-ordinary-cadence policy is slow enough to produce"
    )
