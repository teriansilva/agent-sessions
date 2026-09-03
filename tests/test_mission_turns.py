"""The `/message` turn claim (#852, Phase 2a of #840).

`/message` spans two durable stores — the missions store and the orchestrator ledger — so
"exactly one MODEL EXECUTION per `turn_id`, and at-most-once delivery per ACTION" cannot come
from ordering alone. (A turn may produce several actions; the earlier "exactly one instruction
per turn" phrasing predates #871's lifecycle pass and described a shape the code never had.)
These tests drive the
windows deterministically (a barrier, or a patched call that crashes at a chosen point) rather
than by sleeping and hoping, because a timing-hopeful race test that passes on a quiet machine
is worse than none.

The single assertion every one of them makes: **exactly one MODEL EXECUTION per `turn_id`,
and at-most-once delivery per ACTION**.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agent_sessions import missions
from agent_sessions import orchestrator_ledger as ledger


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    return db


def _mission(db):
    return missions.create_mission("do the thing", path=db)["id"]


def test_two_simultaneous_requests_with_one_turn_id_yield_one_claim(store):
    """The INSERT is the claim, so the duplicate loses at the database rather than at a check.

    A check-then-insert lets both callers observe "no prior turn" and both reach the model. That
    is the failure this table exists to prevent, and it is not observable by inspection — only by
    driving both at once.
    """
    mid = _mission(store)
    verdicts: list[str] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def go():
        barrier.wait(timeout=5)
        v, _ = missions.claim_turn(mid, "t1", "sha", path=store)
        with lock:
            verdicts.append(v)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(verdicts) == [missions.TURN_CLAIMED, missions.TURN_LIVE], verdicts


def test_a_replay_while_the_owner_is_live_calls_nothing(store):
    """`in_progress` with a beating owner is an in-progress answer, never a second attempt."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha", path=store)
    assert missions.claim_turn(mid, "t1", "sha", path=store)[0] == missions.TURN_LIVE


def test_the_same_turn_id_with_different_text_is_a_conflict_not_a_replay(store):
    """A used key on new content is a different turn. Replaying the stored answer would answer a
    question nobody asked."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha-a", path=store)
    assert missions.claim_turn(mid, "t1", "sha-b", path=store)[0] == missions.TURN_CONFLICT


def test_a_crash_after_the_claim_but_before_the_model_recovers_and_re_enters_once(store):
    """No receipt means nothing was ever appended — the ONLY state that may call the model again.

    Without the receipt this window is indistinguishable from the post-append one, and treating
    them alike either strands the turn or duplicates the instruction.
    """
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha", path=store)  # …and the process dies here
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    verdict, row = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    assert verdict == missions.TURN_RECOVER
    assert row["write_reserved_at"] is None


def test_a_crash_after_the_receipt_never_re_enters_the_model(store):
    """The receipt is a ONE-WAY no-reask barrier.

    A write may have landed and we cannot prove otherwise, so the safe reading is "do not send a
    second instruction" — an unanswered turn is visible in the thread, a duplicate nudge to a
    live agent is not.
    """
    mid = _mission(store)
    _, row = missions.claim_turn(mid, "t1", "sha", path=store)
    assert missions.reserve_turn_write(mid, "t1", row["fence"], path=store) is True
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    verdict, _ = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    assert verdict == missions.TURN_RECONCILE, "a receipted turn must never be re-asked"


def test_a_reclaimed_owner_cannot_reserve_a_write_or_settle(store):
    """The fence guards the WRITE, not only the settlement.

    Validating a fence and then appending is check-then-write: the owner passes, is reclaimed,
    and appends anyway — landing a second instruction no later fence can withdraw. Reserving
    first means the reclaimed owner fails *before* the irreversible half.
    """
    mid = _mission(store)
    _, first = missions.claim_turn(mid, "t1", "sha", path=store)
    stale = first["fence"]
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    _, second = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    fresh = second["fence"]
    assert fresh != stale

    assert missions.reserve_turn_write(mid, "t1", stale, path=store) is False
    assert missions.settle_turn(mid, "t1", stale, result="stale answer", path=store) is False
    assert missions.reserve_turn_write(mid, "t1", fresh, path=store) is True
    assert missions.settle_turn(mid, "t1", fresh, result="real", action_ids=["a1"], path=store)


def test_an_unresolvable_turn_settles_indeterminate_rather_than_hanging(store):
    """Receipt set, no action ever written: terminal, not stuck.

    This is the one window the receipt cannot resolve — committed, then died before the append.
    There is no provenance to replay and re-asking is forbidden, so without an explicit terminal
    state the turn sits `in_progress` for ever, holding a compaction pin and answering every
    replay with "still running" about a request that no longer exists.
    """
    mid = _mission(store)
    _, row = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.reserve_turn_write(mid, "t1", row["fence"], path=store)
    assert missions.abandon_turn(mid, "t1", row["fence"], path=store) is True

    verdict, settled = missions.claim_turn(mid, "t1", "sha", path=store)
    assert verdict == missions.TURN_DONE
    assert settled["state"] == "indeterminate"
    assert missions.unresolved_turn_keys(path=store) == set(), "a settled turn must free its pin"


def test_an_unresolved_turn_pins_its_action_against_compaction(store, tmp_path, monkeypatch):
    """ "Readable and absent" may only mean "never written" if compaction cannot have removed it.

    Otherwise a compacted action and one that never existed are the same observation with
    opposite safe responses, and recovery re-asks after a real instruction was compacted away.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    mid = _mission(store)
    missions.claim_turn(mid, "keep-me", "sha", path=store)

    ledger.append(
        {
            "id": "act-pinned",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:a",
            "turn_id": "keep-me",
            # `mission_id` too: the pin is on the FULL key, exactly as production stamps it.
            "mission_id": mid,
            "ts": 1.0,
        }
    )
    for i in range(5):
        ledger.append(
            {
                "id": f"act-{i}",
                "state": "delivered",
                "verb": "continue",
                "session_id": "claude:b",
                "ts": 100.0 + i,
            }
        )

    ledger.compact(history_max=1)
    surviving = set(ledger.latest_by_id())
    assert "act-pinned" in surviving, "an unresolved turn's action was compacted away"


# --- round 1 of #862 review -------------------------------------------------------------------


def test_provenance_is_mission_qualified(store):
    """The key is `(mission_id, turn_id)`, and half a key is not a key.

    Two missions may legitimately use the same turn id. Matching provenance on the id alone lets
    mission B's recovery adopt mission A's actions and report an instruction it never sent.
    """
    from agent_sessions.routes import missions as mroutes

    a = _mission(store)
    b = _mission(store)
    ledger.append(
        {
            "id": "act-A",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:x",
            "turn_id": "shared",
            "mission_id": a,
        }
    )
    # `("ok", ids)` — tri-state since #881: an unreadable ledger must not read as "no actions",
    # because the caller settles a turn TERMINAL on that answer.
    assert mroutes._actions_for_turn(a, "shared") == ("ok", ["act-A"])
    assert mroutes._actions_for_turn(b, "shared") == ("ok", []), "B must not adopt A's action"


def test_the_receipt_records_which_actions_were_about_to_be_written(store):
    """Deterministic identity, so recovery looks for exactly those rather than inferring."""
    mid = _mission(store)
    _, row = missions.claim_turn(mid, "t1", "sha", path=store)
    assert missions.reserve_turn_write(mid, "t1", row["fence"], ["act-1", "act-2"], path=store)
    rec = missions.get_turn(mid, "t1", path=store)
    assert rec["action_ids"] == '["act-1", "act-2"]'


def test_a_settled_turn_reports_the_stores_answer_not_the_losing_frames(store):
    """A lost CAS that still answers "done" is the lie the fence exists to prevent, one layer up."""
    mid = _mission(store)
    _, first = missions.claim_turn(mid, "t1", "sha", path=store)
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    _, second = missions.claim_turn(mid, "t1", "sha", now=future, path=store)

    # The replacement settles first.
    assert missions.abandon_turn(mid, "t1", second["fence"], path=store) is True
    # The original comes back and loses — and must be TOLD it lost.
    assert missions.settle_turn(mid, "t1", first["fence"], result="mine", path=store) is False
    assert missions.get_turn(mid, "t1", path=store)["state"] == "indeterminate"


# --- round 2 of #862 review --------------------------------------------------------------------


def test_a_reclaimed_writer_cannot_append_after_recovery_settled_the_turn(
    store, tmp_path, monkeypatch
):
    """The reserve→append race, driven at the exact point it opens.

    A passes the gate's *moment*, B reclaims and settles the turn terminal, then A tries to write.
    Reserving before the append and appending afterwards leaves that gap open — so the reservation
    is evaluated INSIDE the ledger's own lock, and a fenced-out writer's batch is refused whole.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    mid = _mission(store)
    _, a = missions.claim_turn(mid, "t1", "sha", path=store)

    # B reclaims the orphaned turn and settles it terminal.
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    _, b = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    assert b["fence"] != a["fence"]
    assert missions.abandon_turn(mid, "t1", b["fence"], path=store) is True

    # A now resumes and tries to write under its stale fence — as the gate, in one lock hold.
    kept, dropped = ledger.append_batch_for_free_sessions(
        [
            {
                "id": "act-late",
                "state": "approved",
                "verb": "continue",
                "session_id": "claude:z",
                "turn_id": "t1",
                "mission_id": mid,
            }
        ],
        gate=lambda: missions.reserve_turn_write(mid, "t1", a["fence"], ["act-late"], path=store),
    )
    assert kept == [], "a fenced-out writer appended after the turn was settled"
    assert len(dropped) == 1
    assert "act-late" not in ledger.latest_by_id(), "the action reached the ledger anyway"


def test_the_winning_writer_still_appends(store, tmp_path, monkeypatch):
    """The gate must refuse the loser without also refusing the legitimate writer."""
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    mid = _mission(store)
    _, a = missions.claim_turn(mid, "t1", "sha", path=store)
    kept, dropped = ledger.append_batch_for_free_sessions(
        [
            {
                "id": "act-ok",
                "state": "approved",
                "verb": "continue",
                "session_id": "claude:z",
                "turn_id": "t1",
                "mission_id": mid,
            }
        ],
        gate=lambda: missions.reserve_turn_write(mid, "t1", a["fence"], ["act-ok"], path=store),
    )
    assert [r["id"] for r in kept] == ["act-ok"] and dropped == []


def test_a_turns_timeline_events_are_written_exactly_once(store):
    """Recovery re-enters the turn, so an unconditional append writes the operator's single
    message twice. The check and the write share one transaction — possible only because the
    events and the turn live in the same database."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha", path=store)
    # `create_mission` appends its own `operator_msg`, so measure the DELTA this turn adds rather
    # than the absolute count — otherwise the test passes or fails on an unrelated event.
    base = [e["kind"] for e in missions.get_mission(mid, path=store)["events"]].count(
        "operator_msg"
    )

    first = missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="hi", path=store)
    again = missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="hi", path=store)
    assert first is not None and again == first, "a replay must return the existing seq"

    kinds = [e["kind"] for e in missions.get_mission(mid, path=store)["events"]]
    assert kinds.count("operator_msg") == base + 1

    # …and the assistant slot is independent of the operator one.
    missions.append_turn_event(mid, "t1", "assistant", "assistant_msg", text="ok", path=store)
    missions.append_turn_event(mid, "t1", "assistant", "assistant_msg", text="ok", path=store)
    kinds = [e["kind"] for e in missions.get_mission(mid, path=store)["events"]]
    assert kinds.count("assistant_msg") == 1


def test_a_prior_v9_database_upgrades_rather_than_failing_every_settlement(tmp_path, monkeypatch):
    """`result_meta` went into the v9 CREATE TABLE without a version bump, so a database made by
    the previous build was accepted as current, never migrated, and failed EVERY settlement with
    `no such column`. A fresh install would have looked perfect."""
    import sqlite3 as sq

    db = tmp_path / "old.db"
    con = sq.connect(db)
    con.executescript(
        "CREATE TABLE missions (id TEXT PRIMARY KEY);"
        "CREATE TABLE mission_turns ("
        "  mission_id TEXT NOT NULL, turn_id TEXT NOT NULL, msg_sha TEXT NOT NULL,"
        "  state TEXT NOT NULL, owner TEXT, owner_at REAL, fence TEXT NOT NULL,"
        "  write_reserved_at REAL, result TEXT, action_ids TEXT, created_at REAL NOT NULL,"
        "  settled_at REAL, PRIMARY KEY (mission_id, turn_id));"
        # A real v9 file also carries `mission_objectives` — it has been in the v1 schema since
        # the store existed. Present here because the walk from v9 crosses migrations that ALTER
        # it, and a fixture that omits a table the era genuinely had tests a database that never
        # existed.
        "CREATE TABLE mission_objectives ("
        "  mission_id TEXT NOT NULL, key TEXT NOT NULL, ord INTEGER NOT NULL,"
        "  title TEXT NOT NULL, probe TEXT NOT NULL, probe_args TEXT, gate INTEGER NOT NULL,"
        "  state TEXT NOT NULL, met_at REAL, observed TEXT, source TEXT NOT NULL,"
        "  PRIMARY KEY (mission_id, key));"
        "PRAGMA user_version=9;"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()

    c = missions._ready(db)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(mission_turns)")}
        assert int(c.execute("PRAGMA user_version").fetchone()[0]) == missions.SCHEMA_VERSION
    finally:
        c.close()
    assert {"result_meta", "operator_seq", "assistant_seq"} <= cols


def test_a_settled_turn_always_carries_both_timeline_events(store):
    """Settlement and the assistant event commit together, or neither does.

    Appending afterwards and suppressing the failure produced a **terminal** turn whose answer
    was missing from the timeline — and no later path repaired it, because a replay returns the
    stored response and looks perfectly healthy. `append_event`'s own docstring states the rule
    this broke: an event that is part of an operator-visible change belongs inside that change's
    transaction, or the timeline can disagree with the state it describes.
    """
    mid = _mission(store)
    verdict, row = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.append_turn_event(
        mid, "t1", "operator", "operator_msg", text="please do it", path=store
    )
    assert verdict == missions.TURN_CLAIMED

    # The operator's message is already there — committed with the claim, not after it.
    evs = missions.get_mission(mid, path=store)["events"]
    assert any(e["kind"] == "operator_msg" and e["text"] == "please do it" for e in evs)

    assert missions.settle_turn(
        mid,
        "t1",
        row["fence"],
        result="all done",
        action_ids=["a1"],
        assistant_text="all done",
        assistant_meta={"turn_id": "t1"},
        path=store,
    )
    rec = missions.get_turn(mid, "t1", path=store)
    assert rec["state"] == "done"
    assert rec["operator_seq"] is not None and rec["assistant_seq"] is not None

    evs = missions.get_mission(mid, path=store)["events"]
    assert [e["text"] for e in evs if e["kind"] == "assistant_msg"] == ["all done"]


def test_a_stale_fence_writes_neither_the_settlement_nor_its_event(store):
    """The event must not escape the CAS it rides with."""
    mid = _mission(store)
    _, first = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="hi", path=store)
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    missions.claim_turn(mid, "t1", "sha", now=future, path=store)

    assert (
        missions.settle_turn(
            mid, "t1", first["fence"], result="stale", assistant_text="stale answer", path=store
        )
        is False
    )
    evs = missions.get_mission(mid, path=store)["events"]
    assert not [e for e in evs if e["kind"] == "assistant_msg"], "a fenced-out answer was recorded"


def test_every_terminal_turn_carries_both_events_including_the_recovered_ones(
    store, tmp_path, monkeypatch
):
    """The invariant, checked on the crash paths it exists for rather than only the happy one.

    Reconciliation settled without an assistant event, so a turn recovered after the ledger append
    became terminal with `assistant_seq` NULL and nothing ever repaired it. The claim "a settled
    turn carries both events" was true where it was easy and false where it mattered.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions.routes import missions as mroutes

    # --- recovered WITH provenance: the writer appended, then died before settling -------------
    mid = _mission(store)
    _, a = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="do it", path=store)
    missions.reserve_turn_write(mid, "t1", a["fence"], ["act-1"], path=store)
    ledger.append(
        {
            "id": "act-1",
            # TERMINAL, deliberately. This test is about a SETTLED turn carrying both events, and
            # since decision 2 is enforced on every path a turn whose action is still `approved`
            # correctly stays `in_progress` — it would never reach the assertion. The neighbouring
            # test covers that case; this one needs the action to be finished.
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:z",
            "turn_id": "t1",
            "mission_id": mid,
        }
    )
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    verdict, row = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    assert verdict == missions.TURN_RECONCILE
    asyncio.run(mroutes._reconcile(mid, "t1", row))

    rec = missions.get_turn(mid, "t1", path=store)
    assert rec["state"] == "done"
    assert rec["assistant_seq"] is not None, "a recovered turn settled with no assistant event"

    # --- recovered WITHOUT provenance: indeterminate is terminal too ---------------------------
    mid2 = _mission(store)
    _, b = missions.claim_turn(mid2, "t2", "sha", path=store)
    missions.append_turn_event(mid2, "t2", "operator", "operator_msg", text="do it", path=store)
    missions.reserve_turn_write(mid2, "t2", b["fence"], ["never-written"], path=store)
    verdict2, row2 = missions.claim_turn(mid2, "t2", "sha", now=future, path=store)
    assert verdict2 == missions.TURN_RECONCILE
    asyncio.run(mroutes._reconcile(mid2, "t2", row2))

    rec2 = missions.get_turn(mid2, "t2", path=store)
    assert rec2["state"] == "indeterminate"
    assert rec2["assistant_seq"] is not None, "an abandoned turn settled with no assistant event"


def test_a_pin_does_not_reach_across_missions(store, tmp_path, monkeypatch):
    """`turn_id` is client-generated and may legitimately repeat across missions.

    Pinning on it alone let one long-running turn in mission A retain mission B's unrelated
    terminal history — defeating the ledger's global bound and holding sensitive action records
    nothing was waiting for.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    a = _mission(store)
    b = _mission(store)
    missions.claim_turn(a, "same-turn", "sha", path=store)  # unresolved in A only

    ledger.append(
        {
            "id": "act-A",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:a",
            "turn_id": "same-turn",
            "mission_id": a,
            "ts": 1.0,
        }
    )
    ledger.append(
        {
            "id": "act-B",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:b",
            "turn_id": "same-turn",
            "mission_id": b,
            "ts": 2.0,
        }
    )

    ledger.compact(history_max=0)
    surviving = set(ledger.latest_by_id())
    assert "act-A" in surviving, "A's unresolved turn must keep its own action"
    assert "act-B" not in surviving, "A's turn pinned B's unrelated history"


# ==================================================================================================
# The lifecycle decisions (#871). Each was settled on the issue BEFORE any code, because #852's
# ten rounds established that patching these per-symptom does not converge — five consecutive
# rounds there were each caused by the previous round's fix.
# ==================================================================================================


def test_archive_refuses_while_a_turn_is_unresolved(store):
    """DECISION 1. A turn past its ledger append may already have put bytes on a live PTY, and
    there is no un-sending them — so archive waits rather than cancelling. A 409, not a prompt,
    which is what #840 already says archive does to a live mission."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha-hello", path=store)

    with pytest.raises(missions.MissionError) as e:
        missions.begin_archive(mid, path=store)
    assert e.value.status == 409
    assert "unresolved turn" in str(e.value)
    assert "t1" in str(e.value)


def test_archive_proceeds_once_the_turn_settles(store):
    """The other half, and the one that makes the fence a wait rather than a wall."""
    mid = _mission(store)
    _, row = missions.claim_turn(mid, "t1", "sha-hello", path=store)
    missions.settle_turn(mid, "t1", row["fence"], result="ok", path=store)
    begun = missions.begin_archive(mid, abandon=True, path=store)
    assert begun["mission_id"] == mid


def test_abandon_is_the_one_path_that_archives_past_an_unresolved_turn(store):
    """`{"abandon": true}` is the explicit, confirmed override — and it is exactly why decision 5
    exists: it is the only way to reach an archived mission that still holds live actions."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha-hello", path=store)
    begun = missions.begin_archive(mid, abandon=True, path=store)
    assert begun["mission_id"] == mid


def test_abandon_terminalizes_the_missions_live_actions(store, tmp_path, monkeypatch):
    """DECISION 5. `{"abandon": true}` is the ONE path that archives past a live mission, so it is
    the only place "archived mission, still-deliverable approval" is reachable — the security case
    #871's Risks table names.

    Terminating the agents is not the guard on its own: a delivery can race the teardown, and a
    session relaunched afterwards is a live PTY again. So the actions are settled, and this
    asserts the ledger — not the teardown — is what makes them undeliverable.
    """
    from agent_sessions import mission_archive

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "l.jsonl"))
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})
    ledger.append({"id": "a2", "state": "proposed", "verb": "continue", "session_id": "claude:s2"})
    # …and one on a session this mission does NOT hold, which must be left alone.
    ledger.append({"id": "a3", "state": "approved", "verb": "continue", "session_id": "claude:s9"})

    settled = mission_archive._settle_live_actions(["claude:s1", "claude:s2"])
    assert settled == 2

    states = {k: v.get("state") for k, v in ledger.latest_by_id().items()}
    assert states["a1"] == "expired"
    assert states["a2"] == "expired"
    # Another mission's decision is not collateral damage.
    assert states["a3"] == "approved"


def test_settling_a_missions_actions_leaves_already_terminal_ones_alone(
    store, tmp_path, monkeypatch
):
    """A settled action is history and must not be rewritten — its recorded outcome is what the
    operator was told, and overwriting it would change the past to tidy up the present."""
    from agent_sessions import mission_archive

    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "l.jsonl"))
    ledger.append({"id": "a1", "state": "delivered", "verb": "continue", "session_id": "claude:s1"})
    assert mission_archive._settle_live_actions(["claude:s1"]) == 0
    assert ledger.latest_by_id()["a1"]["state"] == "delivered"


# ==============================================================================================
# THE STATE TABLE (#871), driven as a table — one case per row.
#
# The four lifecycle decisions were answered separately and then contradicted each other: the
# archive fence (1) plus "recovery settles the turn" (2) together opened the fence while the
# action was still `approved` and deliverable. The table is the artifact that made the
# interaction visible, so the tests are written FROM it, one row at a time, rather than as
# scattered assertions — which is also how a later reader checks the code against the contract
# instead of against the prose around it.
#
# | Turn stage           | Action state | Recovery does      | Terminal? | Archive | Snapshot |
# | claimed, before ask  | none         | nothing to recover | yes       | allowed | null     |
# | claimed, after append| approved     | nothing            | NO        | 409     | null     |
# | claimed, delivering  | claimed      | nothing (indet.)   | NO        | 409     | null     |
# | settled              | terminal     | —                  | yes       | allowed | snapshot |
# ==============================================================================================

STALE = missions.TURN_OWNER_MAX_AGE_S + 60


def _orphaned_turn(db, mid, *, reserved, action_ids=()):
    """A turn whose owner has stopped heartbeating — the only kind recovery may touch.

    Ages `owner_at` past `TURN_OWNER_MAX_AGE_S` rather than sleeping: a live owner renews its
    lease for as long as the model call runs, so "orphaned" is a fact about the clock and the
    test should state it directly.
    """
    verdict, _ = missions.claim_turn(mid, "t1", "sha1", path=db)
    assert verdict == missions.TURN_CLAIMED
    row = missions.get_turn(mid, "t1", path=db)
    if reserved:
        assert missions.reserve_turn_write(mid, "t1", row["fence"], list(action_ids), path=db)
    import sqlite3

    con = sqlite3.connect(db)
    con.execute(
        "UPDATE mission_turns SET owner_at=? WHERE mission_id=? AND turn_id='t1'",
        (time.time() - STALE, mid),
    )
    con.commit()
    con.close()
    return row["fence"]


@pytest.fixture
def ledger_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "l.jsonl"))
    return tmp_path / "l.jsonl"


@pytest.mark.parametrize(
    ("action_state", "settles", "turn_state"),
    [
        # Row 2: an approved action is LIVE and still deliverable, so the turn stays unresolved
        # and the archive fence stays shut. This is the row decision 2 was corrected for.
        ("approved", False, None),
        # Row 3: delivering. Nobody knows whether the bytes landed; the turn cannot summarise it.
        ("claimed", False, None),
        # Row 4, one case per terminal outcome. `indeterminate` is contagious: a turn that MAY
        # have delivered must never report that it did not.
        ("delivered", True, "done"),
        ("expired", True, "done"),
        ("rejected", True, "done"),
        ("failed", True, "done"),
        ("indeterminate", True, "indeterminate"),
    ],
)
def test_the_turn_is_not_terminal_until_its_action_is(
    store, ledger_file, action_state, settles, turn_state
):
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=True, action_ids=["a1"])
    ledger.append(
        {"id": "a1", "state": action_state, "verb": "continue", "session_id": "claude:s1"}
    )

    n = mission_turn_reconcile.reconcile(mission_id=mid)
    row = missions.get_turn(mid, "t1", path=store)

    if not settles:
        assert n == 0
        assert row["state"] == "in_progress", action_state
        # …and decision 1's fence is therefore still shut, which is the point of leaving it open.
        with pytest.raises(missions.MissionError) as e:
            missions.begin_archive(mid, path=store)
        assert e.value.status == 409
    else:
        assert n == 1
        assert row["state"] == turn_state, action_state


def test_row_one_a_turn_that_died_before_touching_the_ledger_is_indeterminate(store, ledger_file):
    """Row 1. Nothing was reserved, so nothing was appended and nothing could have been sent.

    `indeterminate` rather than `done`: the turn produced no answer at all, and calling that
    "done" would put a successful-looking record where there is no result.
    """
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=False)
    assert mission_turn_reconcile.reconcile(mission_id=mid) == 1
    row = missions.get_turn(mid, "t1", path=store)
    assert row["state"] == "indeterminate"
    assert "nothing was delivered" in (row["result"] or "")


def test_a_reserved_action_missing_from_the_ledger_is_indeterminate_not_absent(store, ledger_file):
    """The append that may or may not have landed.

    `unresolved_turn_keys` pins exactly these ids against compaction, so an absent record is not
    "compacted away" — it is an append that never committed, which cannot be told apart from one
    that committed and was lost. Reading absence as "nothing happened" is the same false
    certainty `indeterminate` exists to refuse.
    """
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=True, action_ids=["ghost"])
    assert mission_turn_reconcile.reconcile(mission_id=mid) == 1
    assert missions.get_turn(mid, "t1", path=store)["state"] == "indeterminate"


def test_a_live_owner_is_never_reconciled(store, ledger_file):
    """The guard that makes the reconciler safe to call from a request path: a turn whose owner
    is still heartbeating is mid-flight, and settling it would land a conclusion on a model call
    that is about to return its own."""
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    verdict, _ = missions.claim_turn(mid, "t1", "sha1", path=store)
    assert verdict == missions.TURN_CLAIMED
    assert mission_turn_reconcile.reconcile(mission_id=mid) == 0
    assert missions.get_turn(mid, "t1", path=store)["state"] == "in_progress"


def test_an_unreadable_ledger_settles_nothing(store, ledger_file, monkeypatch):
    """THE REAL DOOR, not a monkeypatched one.

    The first version of this test patched `latest_by_id` to RAISE, and passed — while
    production could not reach that handler at all: `_read_all_at` maps an `OSError` to `[]`, so
    a genuinely unreadable ledger returned an EMPTY mapping, the reconciler read that as "the
    reserved action is missing", and settled the turn `indeterminate`. Terminal turns drop out
    of `unresolved_turn_keys`, so a transient I/O error would have opened the archive fence —
    precisely the failure the test's own docstring said it prevented (review on #881).

    So this makes the file actually unreadable and asserts the production path.
    """
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=True, action_ids=["a1"])
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})

    # A file that EXISTS and will not open — which is what `_read_all_at` silently flattened.
    ledger_file.chmod(0o000)
    try:
        assert mission_turn_reconcile.reconcile(mission_id=mid) == 0
        assert missions.get_turn(mid, "t1", path=store)["state"] == "in_progress"
        # …and the fence it guards is still shut.
        with pytest.raises(missions.MissionError) as e:
            missions.begin_archive(mid, path=store)
        assert e.value.status == 409
    finally:
        ledger_file.chmod(0o600)


def test_an_unreadable_ledger_leaves_a_RECOVERING_turn_in_progress(store, ledger_file):
    """The second call site, and the same swallow. `_actions_for_turn` returned a bare list
    inside `contextlib.suppress`, so an unreadable ledger looked like "no actions were written"
    and `_reconcile` abandoned the turn `indeterminate` — terminalizing it on no evidence."""
    from agent_sessions.routes import missions as mroutes

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=True, action_ids=["a1"])
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})

    ledger_file.chmod(0o000)
    try:
        status, found = mroutes._actions_for_turn(mid, "t1")
        assert status == "unreadable", "an unreadable ledger reported as 'no actions'"
        assert found == []
    finally:
        ledger_file.chmod(0o600)

    # …and readable again, the same call reports what is actually there.
    status, found = mroutes._actions_for_turn(mid, "t1")
    assert status == "ok"


def test_the_ttl_expiry_unblocks_an_archive_that_was_fenced(store, ledger_file):
    """The DEADLOCK the reconciler exists to prevent, end to end.

    Decision 1 fences archive on an unresolved turn; decision 2 keeps the turn unresolved until
    its action settles. Without a named owner for "the action went terminal, so the turn is too",
    the TTL sweep expires the action and the archive 409s FOR EVER — a deadlock reached by the
    two mitigations rather than despite them.
    """
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=True, action_ids=["a1"])
    ledger.append(
        {
            "id": "a1",
            "state": "approved",
            "verb": "continue",
            "session_id": "claude:s1",
            "expires_at": time.time() - 1,
        }
    )
    # `abandoned`, not `done`: a plan with no resolved cwd never launched, so the store refuses
    # `done` for it. Either is terminal, which is all this archive needs — and using the one the
    # fixture can actually reach keeps the test about the turn fence rather than about cwd.
    missions.set_state(mid, "draft", "abandoned", path=store)

    # Fenced while the action is live…
    with pytest.raises(missions.MissionError) as e:
        missions.begin_archive(mid, path=store)
    assert e.value.status == 409

    # …the TTL sweep settles the action…
    ledger.expire_due()
    assert ledger.latest_by_id()["a1"]["state"] == "expired"

    # …the reconciler carries that onto the turn…
    assert mission_turn_reconcile.reconcile(mission_id=mid) == 1
    # …and the archive is available again, with no re-send anywhere in the story.
    missions.begin_archive(mid, path=store)


# ---------------------------------------------------------------- decision 3: one authority


def _operator_msgs(db, mid, text):
    """How many operator messages carrying exactly this text are in the timeline.

    Counting the KIND would be wrong: `create_mission` already writes one carrying the mission's
    own instruction, so a bare count of `operator_msg` starts at one and a duplicate turn event
    is invisible inside it.
    """
    return [
        e
        for e in missions.get_mission(mid, path=db)["events"]
        if e["kind"] == "operator_msg" and e.get("text") == text
    ]


def test_the_claim_and_the_operators_message_are_one_transaction(store):
    """DECISION 3. Claim-and-event is atomic, so there is no state where one exists without the
    other — which is what removes the release-the-claim-but-not-the-event bug rather than making
    it rarer. #852 spent five consecutive rounds patching that state from alternating sides."""
    mid = _mission(store)
    verdict, _ = missions.claim_turn(mid, "t1", "sha1", text="ship it", path=store)
    assert verdict == missions.TURN_CLAIMED
    assert len(_operator_msgs(store, mid, "ship it")) == 1
    row = missions.get_turn(mid, "t1", path=store)
    assert row["operator_seq"] is not None


def test_a_replay_does_not_append_the_message_twice(store):
    """The other half: only a fresh claim writes it. A retry of the same turn_id is answering the
    same question, and the timeline already carries it."""
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha1", text="ship it", path=store)
    verdict, _ = missions.claim_turn(mid, "t1", "sha1", text="ship it", path=store)
    assert verdict == missions.TURN_LIVE
    assert len(_operator_msgs(store, mid, "ship it")) == 1


def test_a_refused_claim_writes_no_message_at_all(store):
    """Atomic in the failing direction too. `_fence_busy` refuses inside the transaction, so an
    archived mission does not gain an operator message from a turn it rejected — the mission's
    timeline is sensitive text, and a rejected request must not add to it."""
    mid = _mission(store)
    missions.set_state(mid, "draft", "abandoned", path=store)
    missions.begin_archive(mid, path=store)
    with pytest.raises(missions.MissionError):
        missions.claim_turn(mid, "t1", "sha1", text="ship it", path=store)
    assert _operator_msgs(store, mid, "ship it") == []


def test_the_transient_conditions_are_checked_BEFORE_the_claim():
    """DECISION 3, corrected — and this asserts the ORDER, which is the whole of it.

    The claim now writes the operator's message, so anything that can fail must either fail
    before the claim (nothing exists yet) or settle the turn forward (the turn owns what it
    wrote). What must never come back is the third option — releasing the claim and leaving the
    message behind — which is the state that produced five consecutive fix-caused-the-next-defect
    rounds on #852.

    The first version of this decision deleted the configuration preflight outright, reasoning
    that a check before the call and the check inside it are two moments. The reasoning holds and
    the conclusion did not: a gap between two checks only matters if the two sides can disagree,
    and with the late failure settling forward they cannot. Checking first is what stops an
    ordinary misconfigured install from burning a turn id.
    """
    import inspect

    from agent_sessions.routes import missions as routes

    src = inspect.getsource(routes)
    claim = src.index("missions.claim_turn(")
    assert 0 < src.index('aitasks.is_running("pulse-chat")') < claim
    assert 0 < src.index("review._require_config") < claim
    # …and the failure path settles rather than releasing. `release_turn` deletes an unreserved
    # claim, which is precisely what would orphan the message.
    assert "release_turn" not in src


# ---------------------------------------------------------------- decision 4: null is not []


def test_no_snapshot_and_an_empty_snapshot_are_stored_differently(store):
    """DECISION 4. `None` means no snapshot was taken; `[]` means one was and there were no
    actions. `action_snapshot or []` mapped both to `[]`, so a reader could not tell "we never
    looked" from "we looked and found nothing"."""
    import json as _json

    mid = _mission(store)

    missions.claim_turn(mid, "t1", "sha1", path=store)
    f1 = missions.get_turn(mid, "t1", path=store)["fence"]
    assert missions.settle_turn(mid, "t1", f1, action_snapshot=None, path=store)

    missions.claim_turn(mid, "t2", "sha2", path=store)
    f2 = missions.get_turn(mid, "t2", path=store)["fence"]
    assert missions.settle_turn(mid, "t2", f2, action_snapshot=[], path=store)

    none_meta = _json.loads(missions.get_turn(mid, "t1", path=store)["result_meta"])
    empty_meta = _json.loads(missions.get_turn(mid, "t2", path=store)["result_meta"])
    assert none_meta["actions"] is None
    assert empty_meta["actions"] == []


def test_the_replay_distinguishes_them_too(store):
    """The contract has to survive the read, not just the write: `null` falls back to hydrating
    from the ledger (the snapshot was never taken, so the live record is the best answer), while
    `[]` is returned as itself — settled, and there were no actions."""
    from agent_sessions.routes import missions as routes

    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha1", path=store)
    f1 = missions.get_turn(mid, "t1", path=store)["fence"]
    missions.settle_turn(mid, "t1", f1, action_snapshot=[], path=store)
    assert routes._replay(missions.get_turn(mid, "t1", path=store))["actions"] == []

    missions.claim_turn(mid, "t2", "sha2", path=store)
    f2 = missions.get_turn(mid, "t2", path=store)["fence"]
    missions.settle_turn(mid, "t2", f2, action_snapshot=None, action_ids=[], path=store)
    # No snapshot → hydrated, which for no ids is also empty — but it got there by LOOKING,
    # which is the distinction the store now preserves.
    assert routes._replay(missions.get_turn(mid, "t2", path=store))["actions"] == []


def test_a_reconciled_turn_that_took_no_snapshot_stores_null(store, ledger_file):
    """The recovery row of the state table. Nothing was reserved, so nothing was ever looked at,
    and the stored snapshot says so instead of claiming an empty one."""
    import json as _json

    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _orphaned_turn(store, mid, reserved=False)
    assert mission_turn_reconcile.reconcile(mission_id=mid) == 1
    meta = _json.loads(missions.get_turn(mid, "t1", path=store)["result_meta"])
    assert meta["actions"] is None


# ---------------------------------------------------------------- decision 5: the abandon race
#
# `{"abandon": true}` is the ONE path that archives past a live mission, so it is the only place
# "archived mission, still-deliverable approval" can be reached — the security case #871's Risks
# table names. The regression the review asked to write first: recovery → abandon racing a stale
# approve, with BOTH winners covered, because "the approve loses" has to be true whichever order
# the two actually land in.


def _abandon_settle(keys):
    from agent_sessions import mission_archive

    return mission_archive._settle_live_actions(keys)


def test_abandon_first_the_stale_approve_finds_nothing_to_claim(store, ledger_file):
    """Winner: abandon. The approve arrives afterwards and must not reach a PTY.

    Asserted on the LEDGER rather than on the teardown, deliberately: terminating the agents is
    not the guard on its own — a delivery can race the teardown, and a session relaunched after
    it is a live PTY again. What makes the approve lose is that its action is no longer in a
    claimable state, which no relaunch can undo.
    """
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})
    assert _abandon_settle(["claude:s1"]) == 1
    assert ledger.latest_by_id()["a1"]["state"] == "expired"
    # The actuator's own claim — the step immediately before the first byte — now fails.
    assert ledger.claim("a1", ledger.CLAIMABLE_STATES) is None


def test_approve_first_abandon_does_not_rewrite_a_delivery_in_flight(store, ledger_file):
    """Winner: approve. The bytes may already be gone, so abandon records `indeterminate`.

    Settling this to `expired` would assert a delivery did NOT happen when nobody knows — the one
    thing the ledger's design refuses to do, and the reason `recover_claimed` exists.
    """
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})
    # The approve wins the race to the claim…
    assert ledger.claim("a1", ledger.CLAIMABLE_STATES) is not None
    assert ledger.latest_by_id()["a1"]["state"] == "claimed"
    # …and abandon then records what is true rather than what is tidy.
    assert _abandon_settle(["claude:s1"]) == 1
    assert ledger.latest_by_id()["a1"]["state"] == "indeterminate"


def test_abandon_settles_only_the_missions_own_sessions(store, ledger_file):
    """A blast-radius assertion. Abandon terminalizes what the MISSION holds; another mission's
    pending decision is not collateral damage."""
    ledger.append(
        {"id": "mine", "state": "approved", "verb": "continue", "session_id": "claude:s1"}
    )
    ledger.append(
        {"id": "theirs", "state": "approved", "verb": "continue", "session_id": "claude:s9"}
    )
    assert _abandon_settle(["claude:s1"]) == 1
    states = {k: v["state"] for k, v in ledger.latest_by_id().items()}
    assert states["mine"] == "expired"
    assert states["theirs"] == "approved"


def test_a_relaunched_session_does_not_revive_an_abandoned_decision(store, ledger_file):
    """The case the review named explicitly. Teardown kills the agent; a relaunch brings a live
    PTY back. If the guard were the teardown, the decision would become deliverable again the
    moment the operator reopened the session. It is the ledger, so it does not."""
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})
    _abandon_settle(["claude:s1"])
    # …session relaunched, live again, and the decision is still unclaimable.
    assert ledger.claim("a1", ledger.CLAIMABLE_STATES) is None
    assert ledger.latest_by_id()["a1"]["state"] == "expired"


# ---------------------------------------------------------------- decision 2, ENFORCED EVERYWHERE
#
# The rule was implemented only in the reconciler. Both completion paths settled a turn while its
# action was still live, and a settled turn drops out of `unresolved_turn_keys` — so the ordinary
# happy path opened the archive fence that decision 1 exists to keep shut (review on #881).


@pytest.mark.parametrize("state", sorted(ledger.LIVE_STATES))
def test_a_turn_may_not_settle_while_its_action_is_LIVE(store, ledger_file, state):
    from agent_sessions.routes import missions as mroutes

    ledger.append({"id": "a1", "state": state, "verb": "continue", "session_id": "claude:s1"})
    st, terminal = mroutes._all_terminal(["a1"])
    assert st == "ok"
    assert terminal is False, f"{state} is live and must hold the turn open"


@pytest.mark.parametrize("state", sorted(ledger.TERMINAL_STATES))
def test_a_terminal_action_lets_the_turn_settle(store, ledger_file, state):
    from agent_sessions.routes import missions as mroutes

    ledger.append({"id": "a1", "state": state, "verb": "continue", "session_id": "claude:s1"})
    assert mroutes._all_terminal(["a1"]) == ("ok", True)


def test_an_action_MISSING_from_the_ledger_holds_the_turn_open(store, ledger_file):
    """Absence is not "terminal". `unresolved_turn_keys` pins these against compaction, so a
    missing record is an append that has not landed — and settling on it would open the fence
    just before the action appears."""
    from agent_sessions.routes import missions as mroutes

    assert mroutes._all_terminal(["never-written"]) == ("ok", False)


def test_an_unreadable_ledger_holds_the_turn_open(store, ledger_file):
    from agent_sessions.routes import missions as mroutes

    ledger.append({"id": "a1", "state": "delivered", "verb": "continue", "session_id": "claude:s1"})
    ledger_file.chmod(0o000)
    try:
        st, terminal = mroutes._all_terminal(["a1"])
        assert st == "unreadable"
        assert terminal is False
    finally:
        ledger_file.chmod(0o600)


def test_a_reservation_is_REFUSED_on_an_archived_mission(store):
    """BLOCKER 2. `{"abandon": true}` commits the archive and THEN sweeps the ledger once. A model
    call already in flight reaches the reservation after that sweep — and the turn fence says
    nothing about the mission, so the append landed a fresh action on a mission that had just
    been abandoned, after the sweep meant to settle everything.

    The reservation is the linearization point for the append, so it is where the mission fence
    has to be asked.
    """
    mid = _mission(store)
    missions.claim_turn(mid, "t1", "sha1", path=store)
    fence = missions.get_turn(mid, "t1", path=store)["fence"]

    missions.set_state(mid, "draft", "abandoned", path=store)
    missions.begin_archive(mid, abandon=True, path=store)

    with pytest.raises(missions.MissionError) as e:
        missions.reserve_turn_write(mid, "t1", fence, ["late"], path=store)
    assert e.value.status in (409, 423), e.value.status
    assert missions.get_turn(mid, "t1", path=store)["write_reserved_at"] is None


def test_abandon_REFUSES_rather_than_settling_nothing_on_an_unreadable_ledger(store, ledger_file):
    """BLOCKER 3. The sweep used the fail-soft reader, so an unreadable ledger read as "no live
    actions" and abandon reported success having installed no fence at all — while a
    pre-existing approved action stayed deliverable into the mission it had just archived."""
    from agent_sessions import mission_archive

    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": "claude:s1"})
    ledger_file.chmod(0o000)
    try:
        with pytest.raises(missions.MissionError) as e:
            mission_archive._settle_live_actions(["claude:s1"])
        assert e.value.status == 503
    finally:
        ledger_file.chmod(0o600)
    # …and the action is untouched, so a retry can still settle it.
    assert ledger.latest_by_id()["a1"]["state"] == "approved"


# ---- the four lifecycle holes the second review found (#871 / review on #881) --------------


def test_a_PARTIAL_batch_is_indeterminate_not_done(store, tmp_path, monkeypatch):
    """The receipt is the expected set; the rows that happen to be present are not.

    `append_batch_for_free_sessions` writes and fsyncs one record at a time, so a crash can leave
    reserved `[a1, a2]` with only `a1` in the ledger. Judging the rows FOUND then finds one
    terminal action, calls the turn done, and silently discards `a2` — a turn reported complete
    over an action nobody can account for.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions.routes import missions as mroutes

    mid = _mission(store)
    _, a = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="do it", path=store)
    missions.reserve_turn_write(mid, "t1", a["fence"], ["a1", "a2"], path=store)
    # Only the FIRST of the two landed.
    ledger.append(
        {
            "id": "a1",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:z",
            "turn_id": "t1",
            "mission_id": mid,
        }
    )
    future = time.time() + missions.TURN_OWNER_MAX_AGE_S + 1
    verdict, row = missions.claim_turn(mid, "t1", "sha", now=future, path=store)
    assert verdict == missions.TURN_RECONCILE
    asyncio.run(mroutes._reconcile(mid, "t1", row))

    rec = missions.get_turn(mid, "t1", path=store)
    assert (
        rec["state"] == "indeterminate"
    ), "a turn with an unaccounted-for reserved action was reported done"
    assert rec["assistant_seq"] is not None, "an indeterminate turn settled with no event"


def test_the_BACKGROUND_reconciler_also_writes_both_events(store, tmp_path, monkeypatch):
    """The second production settlement path, driven directly.

    The route's `_reconcile` and `mission_turn_reconcile.reconcile()` are two different callers of
    `settle_turn`, and only the first was covered. Archive reconciliation calls the second, which
    settled turns terminal with `assistant_seq` NULL.
    """
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    from agent_sessions import mission_turn_reconcile

    mid = _mission(store)
    _, a = missions.claim_turn(mid, "t1", "sha", path=store)
    missions.append_turn_event(mid, "t1", "operator", "operator_msg", text="do it", path=store)
    missions.reserve_turn_write(mid, "t1", a["fence"], ["act-1"], path=store)
    ledger.append(
        {
            "id": "act-1",
            "state": "delivered",
            "verb": "continue",
            "session_id": "claude:z",
            "turn_id": "t1",
            "mission_id": mid,
        }
    )
    # The turn looks orphaned once its owner lease is older than the max age.
    monkeypatch.setattr(missions, "TURN_OWNER_MAX_AGE_S", -1)
    settled = mission_turn_reconcile.reconcile(mission_id=mid)
    assert settled == 1, "the background reconciler settled nothing"

    rec = missions.get_turn(mid, "t1", path=store)
    assert rec["state"] == "done"
    assert (
        rec["assistant_seq"] is not None
    ), "the background reconciler settled a terminal turn with no assistant event"


@pytest.mark.parametrize(
    ("states", "expect_word"),
    [
        (["delivered"], "delivered"),
        (["delivered", "delivered"], "delivered"),
        (["expired"], "settled without delivery"),
        (["expired", "rejected"], "settled without delivery"),
        (["delivered", "expired"], "partially delivered"),
        (["delivered", "failed"], "partially delivered"),
        (["delivered", "rejected"], "partially delivered"),
    ],
)
def test_a_MIXED_batch_is_not_reported_as_undelivered(states, expect_word):
    """A durable result must not contradict its own state list.

    `settled without delivery: delivered, expired` says nothing was sent while naming the thing
    that was. It matters to the operator too: something DID reach the agent, so the turn is not a
    clean no-op they can simply repeat (review on #881).
    """
    from agent_sessions.mission_turn_reconcile import _turn_outcome

    state, result = _turn_outcome(states)
    assert state == "done"
    assert result.startswith(expect_word), (states, result)
    if "delivered" in states and any(s != "delivered" for s in states):
        assert "without delivery" not in result, result
