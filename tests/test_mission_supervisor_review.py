"""Regressions for the twelve defects found reviewing PR #888 (#885).

Each test is named for the failure it reproduces, and each was written by first confirming it goes
RED against the code as reviewed. Where a defect had an existing test that passed anyway, the note
says why that test could not see it — usually because it drove the helper directly instead of the
production path, which is the difference between proving a helper works and proving it is used.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_supervisor as sup
from agent_sessions import mission_supervisor_loop as loop
from agent_sessions import missions
from agent_sessions import orchestrator_ledger as ledger

SESSION = "claude:11111111-1111-1111-1111-111111111111"
OTHER = "claude:22222222-2222-2222-2222-222222222222"
KEY = "checks_green"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    missions.reset_schema_cache_for_test()
    return missions.create_mission("ship it", cwd="/tmp")["id"]


def _running(store, *sessions):
    missions.set_state(store, "draft", "planned")
    missions.set_state(store, "planned", "dispatching")
    missions.set_state(store, "dispatching", "running")
    for s in sessions or (SESSION,):
        missions.adopt(store, s)


def _objective(mid, key, *, gate=True):
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": key,
                "title": key.replace("_", " "),
                "probe": "forge_pr",
                "gate": gate,
                "source": "playbook",
            }
        ],
    )


# =================================================================================== finding 3
def test_WAIVING_an_objective_starts_a_new_episode(store):
    """`bump_episode` had no production caller at all.

    The existing episode tests called it by hand, so they proved the helper and not the contract —
    an objective could transition while keeping the episode its earlier nudges were charged to.
    """
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    ledger.append({"id": "a1", "state": "delivered", "verb": "continue", "session_id": SESSION})
    assert sup.budget_state(store, KEY)["spent"] == 1

    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])

    episode, _ = missions.objective_episode(store, KEY)
    assert episode == 2, "the objective transitioned but the episode did not advance"
    assert sup.budget_state(store, KEY)["spent"] == 0, "the new episode inherited the old spend"


def test_a_transition_also_ENDS_a_stand_down(store):
    """A stand-down silences one episode. If the episode never advances it silences forever."""
    _objective(store, KEY)
    assert missions.stand_down(store, KEY, episode=1) is True
    assert sup.may_nudge(store, KEY)[0] is False

    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])
    _, stood_down = missions.objective_episode(store, KEY)
    assert stood_down is False, "the operator's silence outlived the episode it was for"


# =================================================================================== finding 4
def test_DROPPING_an_objective_forgets_its_supervisor_lifecycle(store):
    """Re-adding the same key must not inherit the dead objective's episode, silence or spend.

    The lifecycle tables key on `(mission_id, objective_key)` and the FK cascade only follows
    `missions(id)`, so nothing reached them when the objective itself went away.
    """
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    ledger.append({"id": "a1", "state": "delivered", "verb": "continue", "session_id": SESSION})
    missions.stand_down(store, KEY, episode=1)

    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])
    _objective(store, KEY)

    episode, stood_down = missions.objective_episode(store, KEY)
    assert (episode, stood_down) == (1, False), "a fresh objective arrived already silenced"
    assert sup.budget_state(store, KEY)["spent"] == 0, "a fresh objective arrived with no budget"
    assert sup.may_nudge(store, KEY)[0] is True


# =================================================================================== finding 5
def test_stand_down_REFUSES_a_fabricated_episode(store):
    """The episode guard sat only on the DO UPDATE branch, so the INSERT branch took any number.

    `episode=99` on a fresh objective was written and BECAME current, silencing every real episode
    up to it — reachable by one authenticated malformed request.
    """
    _objective(store, KEY)
    assert missions.stand_down(store, KEY, episode=99) is False
    episode, stood_down = missions.objective_episode(store, KEY)
    assert (episode, stood_down) == (1, False)
    assert sup.may_nudge(store, KEY)[0] is True, "a fabricated episode silenced a real objective"


def test_stand_down_REFUSES_an_unknown_objective(store):
    """Upserting on a key that does not exist leaves a row waiting to silence it when it arrives."""
    assert missions.stand_down(store, "never_added", episode=1) is False
    _objective(store, "never_added")
    assert sup.may_nudge(store, "never_added")[0] is True


def test_stand_down_still_WORKS_on_the_current_episode(store):
    """The guard must not have made the feature unusable — the honest half of the fix."""
    _objective(store, KEY)
    assert missions.stand_down(store, KEY, episode=1) is True
    assert sup.may_nudge(store, KEY)[0] is False


# =================================================================================== finding 6
@pytest.mark.anyio
async def test_a_KNOWN_refusal_does_not_charge_the_budget(store, monkeypatch):
    """`_persist` returning nothing is DEFINITE: the ledger did not take the action.

    Keeping the binding made the next read see an id with no ledger row, treat it as indeterminate
    — charged and terminal — and end automatic attempts for the whole episode over a write the code
    knows never happened.
    """
    _running(store)
    _objective(store, KEY)
    monkeypatch.setattr(sup, "may_nudge", lambda *a, **k: (True, ""))
    from agent_sessions import orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: [])

    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is False

    b = sup.budget_state(store, KEY)
    assert b["spent"] == 0, "a write that provably never happened was charged"
    assert b["indeterminate"] is False, "a KNOWN outcome was recorded as unknown"


# =================================================================================== finding 7
def test_a_LIVE_action_is_not_TERMINAL(store):
    """An ordinary pending approval must not become a once-per-episode terminal escalation."""
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": SESSION})

    o = next(o for o in sup.assess(store)["objectives"] if o["key"] == KEY)
    assert o["may_nudge"] is False
    assert o["terminal"] is False, "a live action was treated as the end of the episode"


def test_an_UNREADABLE_ledger_is_not_TERMINAL(store, monkeypatch):
    """It is a fact about the file, not about the objective."""
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    monkeypatch.setattr(ledger, "latest_by_id_checked", lambda: ("error", {}))
    o = next(o for o in sup.assess(store)["objectives"] if o["key"] == KEY)
    assert o["unreadable"] is True and o["terminal"] is False


def test_an_EXHAUSTED_budget_IS_terminal(store):
    """The other side of the same rule — the fix must not stop real escalations."""
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        aid = f"a{i}"
        missions.record_supervisor_action(
            store, session_key=SESSION, objective_key=KEY, episode=1, action_id=aid
        )
        ledger.append({"id": aid, "state": "delivered", "verb": "continue", "session_id": SESSION})
    o = next(o for o in sup.assess(store)["objectives"] if o["key"] == KEY)
    assert o["terminal"] is True


@pytest.mark.anyio
async def test_the_pass_does_not_ESCALATE_a_merely_live_action(store, monkeypatch):
    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    ledger.append({"id": "a1", "state": "approved", "verb": "continue", "session_id": SESSION})
    monkeypatch.setattr(sup, "consider", _no_model)

    out = await sup.run_pass(store)
    assert out.get("escalated") is None, "a pending approval was terminally escalated"


# =================================================================================== finding 8
@pytest.mark.anyio
async def test_an_objective_WAIVED_while_the_model_ran_is_not_nudged(store, monkeypatch):
    """The proposal was authorized at assessment time and delivered without re-reading it.

    Everything `nudge` checked (budget, episode, the actuator's session fence) stays true when the
    OBJECTIVE changes, so a waive inside the model/delivery window still typed `continue`.
    """
    _running(store)
    _objective(store, KEY)
    from agent_sessions import orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    persisted: list = []
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: persisted.extend(recs) or recs)

    # The operator settles it after the proposal was formed, before the write.
    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])

    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is False
    assert "waived" in res["why"]
    assert persisted == [], "a stale proposal was written after the objective was settled"


@pytest.mark.anyio
async def test_an_objective_DROPPED_while_the_model_ran_is_not_nudged(store, monkeypatch):
    _running(store)
    _objective(store, KEY)
    from agent_sessions import orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    persisted: list = []
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: persisted.extend(recs) or recs)
    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])

    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is False and persisted == []


# =================================================================================== finding 1
async def _no_model(mission_id, session_key, *, path=None):
    return {"skipped": "no model in this test", "assessment": None}


@pytest.mark.anyio
async def test_a_DETACHED_session_is_never_nudged(store, monkeypatch):
    """`get_mission` returns the full historical roster, including released sessions.

    Taking `sessions[0]` unfiltered meant a mission could send autonomous input into a session it
    no longer owns — one that may since have been adopted by a different mission.
    """
    _running(store, SESSION, OTHER)
    _objective(store, KEY)
    missions.detach(store, SESSION)
    monkeypatch.setattr(sup, "consider", _no_model)
    seen: list = []
    monkeypatch.setattr(sup, "_still_authorized", lambda *a, **k: (True, ""))

    async def _spy(mid, *, session_key, objective_key, why, registry=None, path=None):
        seen.append(session_key)
        return {"sent": False}

    monkeypatch.setattr(sup, "nudge", _spy)
    out = await sup.run_pass(store)
    assert SESSION not in out["sessions"], "a released session stayed on the supervised roster"
    assert out["sessions"] == [OTHER]


@pytest.mark.anyio
async def test_EVERY_held_session_is_supervised_not_just_the_first(store, monkeypatch):
    """A mission with two live agents had exactly one of them looked at."""
    _running(store, SESSION, OTHER)
    _objective(store, KEY)
    looked: list = []

    async def _spy(mission_id, session_key, *, path=None):
        looked.append(session_key)
        return {"skipped": "x", "assessment": None}

    monkeypatch.setattr(sup, "consider", _spy)
    await sup.run_pass(store)
    assert looked == [SESSION, OTHER], f"only {looked} was supervised"


# =================================================================================== finding 2
@pytest.mark.anyio
async def test_the_pass_STOPS_when_the_mission_needs_the_operator(store, monkeypatch):
    """Typing `continue` into a mission that is waiting on a human talks over the question."""
    _running(store)
    _objective(store, KEY)
    # PRODUCER-FAITHFUL: `derive_needs_you` returns `why`, not `needs_you_why` (that is the name
    # of the field on the mission ROW). The old stub used the wrong key and passed anyway, because
    # the gate only read `needs_you` — #892 made the reasons load-bearing and surfaced it.
    #
    # `decision` rather than `question`: a pending decision is about a SESSION, which is what a
    # nudge would type into, so it stops the pass. An open question stands its own objective down
    # instead — see the case below.
    monkeypatch.setattr(
        missions,
        "derive_needs_you",
        lambda ids, **k: {store: {"needs_you": True, "why": ["decision"]}},
    )
    asked: list = []

    async def _spy(mission_id, session_key, *, path=None):
        asked.append(session_key)
        return {"skipped": "x", "assessment": None}

    monkeypatch.setattr(sup, "consider", _spy)
    out = await sup.run_pass(store)
    assert out["skipped"] == "the mission needs the operator"
    assert asked == [], "the model was asked about a mission already waiting on a human"


@pytest.mark.anyio
async def test_an_open_QUESTION_does_not_stop_the_whole_mission(store, monkeypatch):
    """#892: a question is about ONE objective, and `open_question` stands that one down.

    A pending decision and an intervention are about a session — the thing a nudge types into — so
    they stop the pass. Stopping the whole mission for a question would stall follow-through on
    every other objective because one of them is waiting on an answer.
    """
    _running(store)
    _objective(store, KEY)
    monkeypatch.setattr(
        missions,
        "derive_needs_you",
        lambda ids, **k: {store: {"needs_you": True, "why": ["question"]}},
    )
    asked: list = []

    async def _spy(mission_id, session_key, *, path=None):
        asked.append(session_key)
        return {"skipped": "x", "assessment": None}

    monkeypatch.setattr(sup, "consider", _spy)
    out = await sup.run_pass(store)
    assert "skipped" not in out, out
    assert asked, "the other objectives must keep being followed through"


@pytest.mark.anyio
async def test_a_question_ALONGSIDE_a_decision_still_stops_the_pass(store, monkeypatch):
    """The decision is what stops it; the question neither adds to nor cancels that."""
    _running(store)
    _objective(store, KEY)
    monkeypatch.setattr(
        missions,
        "derive_needs_you",
        lambda ids, **k: {store: {"needs_you": True, "why": ["question", "decision"]}},
    )
    monkeypatch.setattr(sup, "consider", lambda *a, **k: None)
    out = await sup.run_pass(store)
    assert out["skipped"] == "the mission needs the operator"
    assert out["needs_you_why"] == ["decision"]


# =================================================================================== finding 12
@pytest.mark.anyio
async def test_the_completion_PROPOSAL_is_an_artifact_not_just_a_state_flip(store, monkeypatch):
    """`Closes #885` promises a proposal listing each objective and the fact that settled it."""
    _running(store)
    _objective(store, "pr_open")
    _objective(store, "checks", gate=False)
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    missions.patch_objectives(store, [{"op": "waive", "key": "checks"}])
    monkeypatch.setattr(sup, "consider", _no_model)

    out = await sup.run_pass(store)
    assert out.get("likely_done") is True

    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "completion"]
    assert events, "the mission moved to review with no proposal to review"
    meta = events[-1]["meta"]
    assert {o["key"] for o in meta["objectives"]} == {"pr_open", "checks"}
    assert all(o["settled_by"] for o in meta["objectives"]), "no reason was given per objective"
    # A waiver is not a verification, and the proposal must not blur them.
    assert "not verified" in meta["objectives"][0]["settled_by"]
    assert "nothing has been closed" in (events[-1]["text"] or "").lower()


@pytest.mark.anyio
async def test_the_completion_proposal_is_posted_ONCE(store, monkeypatch):
    """The CAS is the idempotency — a second pass must not repost it."""
    _running(store)
    _objective(store, "pr_open")
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    monkeypatch.setattr(sup, "consider", _no_model)

    await sup.run_pass(store)
    await sup.run_pass(store)
    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "completion"]
    assert len(events) == 1, f"the proposal was posted {len(events)} times"


# =================================================================================== finding 9
@pytest.mark.anyio
async def test_the_sweep_ROTATES_so_no_mission_starves(tmp_path, monkeypatch):
    """A fixed prefix of a newest-first list never reaches the tail.

    With more than `MISSIONS_PER_SWEEP` eligible missions the old sweep visited the same page
    forever; `review` missions in particular were never reached at all.
    """
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    ids = [missions.create_mission(f"m{i}", cwd="/tmp")["id"] for i in range(7)]
    for mid in ids:
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        missions.set_state(mid, "dispatching", "running")

    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 3)
    monkeypatch.setattr(loop, "_cursor", None, raising=False)
    seen: list[str] = []

    async def _spy(mid, registry=None, path=None):
        seen.append(mid)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)

    for _ in range(3):
        await loop.sweep()

    assert len(set(seen)) == len(
        ids
    ), f"after three sweeps of 3 over 7 missions only {len(set(seen))} were ever visited"


# =================================================================================== finding 10
@pytest.mark.anyio
async def test_the_STALL_detector_is_actually_consulted(store, monkeypatch):
    """It was defined, documented and tested — and never called by any production path."""
    _running(store)
    _objective(store, KEY)
    monkeypatch.setattr(sup, "consider", _no_model)
    monkeypatch.setattr(
        sup, "session_is_stalled", lambda *a, **k: (True, "nothing since dispatch", 0)
    )

    out = await sup.run_pass(store)
    per = out["per_session"][0]
    assert (
        per.get("stalled") == "nothing since dispatch"
    ), "the pass reported no stall for a session the detector calls stuck"


# ==============================================================================================
# Round 2 of the #888 review — nine further defects.
# ==============================================================================================


# =================================================================================== finding 1
@pytest.mark.anyio
async def test_a_session_ADOPTED_ELSEWHERE_mid_pass_is_not_written_to(store, tmp_path, monkeypatch):
    """The roster snapshot is taken at the top of the pass; ownership can change under it.

    Filtering `removed_at` at snapshot time is not enough — the probe detached the session and
    adopted it into another mission during `precondition_for`, and the old mission still persisted
    a `continue` for it. Ownership is now re-read at the append fence.
    """
    _running(store)
    _objective(store, KEY)
    other = missions.create_mission("the new owner", cwd="/tmp")["id"]
    from agent_sessions import orchestrator

    def _steal(*a, **k):
        # Exactly the window Hermes drove: ownership moves while the precondition is captured.
        missions.detach(store, SESSION)
        missions.adopt(other, SESSION)
        return None

    monkeypatch.setattr(orchestrator, "precondition_for", _steal)
    persisted: list = []
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: persisted.extend(recs) or recs)

    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is False
    assert other in res["why"], res["why"]
    assert persisted == [], "a mission wrote into a session another mission now owns"


def test_the_authority_predicate_refuses_a_session_this_mission_never_held(store):
    _objective(store, KEY)
    ok, why = sup._still_authorized(store, KEY, session_key=SESSION)
    assert ok is False and "left this mission" in why


# =================================================================================== finding 2
def test_the_FINAL_fence_authority_is_DERIVED_from_the_action_record(store):
    """Enforcement must not depend on the caller remembering to pass a callback.

    It used to: `nudge` handed `extra_authority` to its own `deliver_auto` call, which protected
    that one call. In suggest mode the action stays `proposed`, and an operator tapping approve
    later goes through the generic `deliver()` — which knew nothing about the mission. Propose,
    detach the session into another mission, approve, and `continue` landed in the new mission's
    session. The authority now comes off the RECORD, so every path enforces it.
    """
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    rec = {
        "id": "a1",
        "source": "supervisor",
        "mission_id": store,
        "objective_key": KEY,
        "objective_episode": 1,
        "session_id": SESSION,
        "verb": "continue",
    }
    check, state = actuator._supervisor_authority(rec)
    assert check is not None and state is not None, "a supervisor action carried no authority"
    assert check()[0] is True

    # The exact bypass: the session moves to another mission after the proposal was minted.
    other = missions.create_mission("the new owner", cwd="/tmp")["id"]
    missions.detach(store, SESSION)
    missions.adopt(other, SESSION)

    ok, why = check()
    assert ok is False and other in why, why


def test_a_NON_supervisor_action_carries_no_mission_authority(store):
    """The control: deriving from the record must not impose mission checks on ordinary actions."""
    from agent_sessions import actuator

    assert actuator._supervisor_authority(
        {"id": "a1", "source": "orchestrator", "session_id": SESSION, "verb": "continue"}
    ) == (None, None)
    # …nor on a supervisor action missing the fields the check needs.
    assert actuator._supervisor_authority(
        {"id": "a2", "source": "supervisor", "session_id": SESSION}
    ) == (None, None)


def test_an_UNREADABLE_mission_store_refuses_the_delivery(store):
    """Unverifiable authority is not authority — the same rule the fence itself uses."""
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    check, _ = actuator._supervisor_authority(
        {
            "id": "a1",
            "source": "supervisor",
            "mission_id": store,
            "objective_key": KEY,
            "objective_episode": 1,
            "session_id": SESSION,
            "verb": "continue",
        }
    )
    import agent_sessions.missions as M

    real = M.supervisor_authority
    M.supervisor_authority = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store down"))
    try:
        ok, why = check()
    finally:
        M.supervisor_authority = real
    assert ok is False and "could not be re-read" in why


# =================================================================================== finding 3
def test_the_budget_cannot_be_OVERSPENT_by_two_overlapping_passes(store):
    """`may_nudge` then append is a check-then-act; the binding is now the reservation.

    Drives the real vulnerable ordering: both passes read a budget with one unit left, then both
    try to reserve. Exactly one may win, or a 3-nudge budget delivers four.
    """
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET - 1):
        aid = f"d{i}"
        missions.record_supervisor_action(
            store, session_key=SESSION, objective_key=KEY, episode=1, action_id=aid
        )
        ledger.append({"id": aid, "state": "delivered", "verb": "continue", "session_id": SESSION})

    assert sup.may_nudge(store, KEY)[0] is True, "the fixture must leave exactly one unit"
    a = missions.record_supervisor_action(
        store,
        session_key=SESSION,
        objective_key=KEY,
        episode=1,
        action_id="race-a",
        max_per_episode=sup.NUDGE_BUDGET,
    )
    b = missions.record_supervisor_action(
        store,
        session_key=SESSION,
        objective_key=KEY,
        episode=1,
        action_id="race-b",
        max_per_episode=sup.NUDGE_BUDGET,
    )
    assert [a, b] == [True, False], "both overlapping passes reserved the last unit"
    ids = missions.supervisor_action_ids(store, KEY, 1)
    assert (
        len(ids) == sup.NUDGE_BUDGET
    ), f"{len(ids)} actions minted against a budget of {sup.NUDGE_BUDGET}"


def test_the_reservation_still_allows_the_budget_it_grants(store):
    """The control: the cap must not be off by one in the strict direction."""
    _objective(store, KEY)
    got = [
        missions.record_supervisor_action(
            store,
            session_key=SESSION,
            objective_key=KEY,
            episode=1,
            action_id=f"a{i}",
            max_per_episode=sup.NUDGE_BUDGET,
        )
        for i in range(sup.NUDGE_BUDGET)
    ]
    assert got == [True] * sup.NUDGE_BUDGET


# =================================================================================== finding 4
def test_two_sessions_do_not_SHARE_one_checkpoint(store):
    """One row per mission made two sessions fight over it.

    A writes fingerprint A, B overwrites with B, and A — unchanged — is charged another model call
    next sweep. In the other direction two sessions with the SAME fingerprint suppress the second
    one's first recap entirely.
    """
    missions.advance_checkpoint(store, session_key=SESSION, input_fp="fp-A", recap_text="A")
    missions.advance_checkpoint(store, session_key=OTHER, input_fp="fp-B", recap_text="B")

    assert missions.supervisor_checkpoint(store, session_key=SESSION)["input_fp"] == "fp-A"
    assert missions.supervisor_checkpoint(store, session_key=OTHER)["input_fp"] == "fp-B"


def test_a_SECOND_session_is_not_suppressed_by_an_identical_fingerprint(store):
    """The other direction of the same defect — and the one that loses a recap silently."""
    missions.advance_checkpoint(store, session_key=SESSION, input_fp="same", recap_text="A")
    assert (
        missions.supervisor_checkpoint(store, session_key=OTHER)["input_fp"] is None
    ), "a second session inherited the first's checkpoint and would skip its own first recap"


# =================================================================================== finding 5
@pytest.mark.anyio
async def test_a_gate_added_MID_PASS_stops_the_completion(store, monkeypatch):
    """Assess, flip, append were three operations with two windows between them.

    A gate added between the assessment and the flip still moved the mission to `review`, carrying
    a proposal listing only the old checklist.
    """
    _running(store)
    _objective(store, "pr_open")
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])
    monkeypatch.setattr(sup, "consider", _no_model)

    real = missions.propose_completion

    def _add_a_gate_first(mid, **kw):
        _objective(mid, "late_gate")  # unmet, gating — arrives inside the window
        return real(mid, **kw)

    monkeypatch.setattr(missions, "propose_completion", _add_a_gate_first)
    out = await sup.run_pass(store)

    assert (
        missions.get_mission(store)["state"] == "running"
    ), "a mission with an unmet gate was moved to review"
    assert out.get("proposed_review") is not True
    assert not [e for e in missions.get_mission(store)["events"] if e["kind"] == "completion"]


def test_the_completion_transition_and_its_proposal_are_ONE_transaction(store):
    """An append that fails must not leave a mission in `review` with nothing to review — and the
    CAS would already have won, so no later pass could ever repair it."""
    _running(store)
    _objective(store, "pr_open")
    missions.patch_objectives(store, [{"op": "waive", "key": "pr_open"}])

    import agent_sessions.missions as M

    real_append = M._append_event

    def _fail_on_completion(con, mid, kind, **kw):
        if kind == "completion":
            raise RuntimeError("the append failed")
        return real_append(con, mid, kind, **kw)

    M._append_event = _fail_on_completion
    try:
        with pytest.raises(RuntimeError):
            missions.propose_completion(store, from_state="running", render=lambda rows: ("t", {}))
    finally:
        M._append_event = real_append

    assert (
        missions.get_mission(store)["state"] == "running"
    ), "the state moved even though its proposal never landed"


# =================================================================================== finding 6
@pytest.mark.anyio
async def test_NEEDS_APPROVAL_makes_the_mission_need_the_operator(store, monkeypatch):
    """Nulling the proposal is only half the sentence.

    It left no pending action, no question, no escalation — so `needs_you` stayed false and the
    operator was never told a decision was waiting.
    """
    _running(store)
    _objective(store, KEY)

    async def _needs_approval(mission_id, session_key, *, path=None):
        return {"assessment": "needs_approval", "nudge": {"objective_key": KEY, "why": "poke"}}

    monkeypatch.setattr(sup, "consider", _needs_approval)
    sent: list = []
    monkeypatch.setattr(sup, "nudge", lambda *a, **k: sent.append(k) or {"sent": True})

    out = await sup.run_pass(store)
    assert sent == [], "a decision only the operator can make became bytes"
    assert out.get("escalated"), "needs_approval produced no durable artifact at all"
    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "escalation"]
    assert events, "nothing durable recorded the waiting decision"


# =================================================================================== finding 7
@pytest.mark.anyio
async def test_a_STALLED_session_is_not_nudged_and_escalates_instead(store, monkeypatch):
    """The detector was called and its result went nowhere.

    A session that has written nothing since dispatch is sitting at something that eats keystrokes,
    so `continue` goes into a wall and the budget drains against it. The operator is the only one
    who can clear that, so it escalates rather than nudging.
    """
    _running(store)
    _objective(store, KEY)

    async def _wants_a_nudge(mission_id, session_key, *, path=None):
        return {"assessment": "on_track", "nudge": {"objective_key": KEY, "why": "poke"}}

    monkeypatch.setattr(sup, "consider", _wants_a_nudge)
    monkeypatch.setattr(
        sup, "session_is_stalled", lambda *a, **k: (True, "nothing since dispatch", 0)
    )
    sent: list = []
    monkeypatch.setattr(sup, "nudge", lambda *a, **k: sent.append(k) or {"sent": True})

    out = await sup.run_pass(store)
    assert sent == [], "a stalled session was nudged into a wall"
    assert out.get("escalated"), "a stalled session produced no operator-facing artifact"
    assert "written nothing" in out["escalated"]["reason"]


# =================================================================================== finding 8
@pytest.mark.anyio
async def test_the_sweep_REACHES_every_eligible_mission_with_no_ceiling(tmp_path, monkeypatch):
    """The worklist used to be a rebuilt PREFIX of a list page, so it always had a boundary.

    First it was one page of a newest-first list; then that list paged to `WORKLIST_MAX`, which
    moved the boundary without removing it — rows past it were excluded from every reconstructed
    ring, and logging the truncation supervises nothing. It is a keyset cursor over the whole
    eligible set now, so this walks more missions than one sweep can hold and asserts every one is
    reached.
    """
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 3)
    monkeypatch.setattr(loop, "_cursor", None, raising=False)

    ids = []
    for i in range(11):
        mid = missions.create_mission(f"m{i}", cwd="/tmp")["id"]
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        missions.set_state(mid, "dispatching", "running")
        ids.append(mid)

    seen: list[str] = []

    async def _spy(mid, registry=None, path=None):
        seen.append(mid)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)
    for _ in range(4):
        await loop.sweep()

    assert set(seen) == set(
        ids
    ), f"{len(set(ids) - set(seen))} eligible missions were never reachable"


@pytest.mark.anyio
async def test_the_worklist_cursor_WRAPS_rather_than_stalling(tmp_path, monkeypatch):
    """A cursor sitting past the last id must start the next revolution, not do nothing."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 10)

    mid = missions.create_mission("only one", cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    monkeypatch.setattr(loop, "_cursor", "zzz-past-the-end", raising=False)

    seen: list[str] = []

    async def _spy(m, registry=None, path=None):
        seen.append(m)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)
    await loop.sweep()
    assert seen == [mid], "a cursor past the end stalled the sweep instead of wrapping"


def test_the_worklist_EXCLUDES_ineligible_states(tmp_path, monkeypatch):
    """The control: removing the ceiling must not widen WHAT is eligible."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    draft = missions.create_mission("still a draft", cwd="/tmp")["id"]
    live = missions.create_mission("running", cwd="/tmp")["id"]
    for a, b in (("draft", "planned"), ("planned", "dispatching"), ("dispatching", "running")):
        missions.set_state(live, a, b)

    got = missions.supervisor_worklist(states=loop.ELIGIBLE_STATES, limit=50)
    assert live in got and draft not in got


# =================================================================================== finding 9
def test_one_exhausted_objective_escalates_ONCE_not_once_per_session(store):
    """The uniqueness key included `session_key` while the budget it reports is objective-level.

    With the pass now visiting every held session, one exhausted objective announced itself twice.
    """
    _running(store, SESSION, OTHER)
    _objective(store, KEY)
    first = missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent"
    )
    second = missions.escalate_once(
        store, session_key=OTHER, objective_key=KEY, episode=1, reason="spent"
    )
    assert [first, second] == [True, False], "one objective escalated once per session"


def test_a_NEW_episode_may_escalate_again(store):
    """The control: objective-level arbitration must not silence the next episode."""
    _running(store)
    _objective(store, KEY)
    assert missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent"
    )
    # The episode must actually advance — naming an episode the objective is not on is refused.
    missions.bump_episode(store, KEY)
    assert missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=2, reason="spent again"
    )


# ==============================================================================================
# Round 3 of the #888 review — six further defects.
# ==============================================================================================


# =============================================================================== r3, finding 1
def test_the_supervisor_authority_is_IN_the_fingerprint_the_write_fence_re_reads(store):
    """`extra_authority` runs from `_final_guard`, which is BEFORE the registry and screen work.

    A callback there is a check with a window after it; only what the fence re-reads inside the
    lock is binding. So the same authority tuple is folded into the fingerprint — and it must
    actually CHANGE when ownership or the objective changes, or folding it in proves nothing.
    """
    _running(store)
    _objective(store, KEY)
    fp = lambda: sup._authority_state(store, KEY, session_key=SESSION)  # noqa: E731

    before = fp()
    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])
    assert fp() != before, "waiving the objective did not move the fingerprint"

    _objective(store, "second")
    before2 = sup._authority_state(store, "second", session_key=SESSION)
    missions.detach(store, SESSION)
    assert (
        sup._authority_state(store, "second", session_key=SESSION) != before2
    ), "losing the session did not move the fingerprint"


def test_the_composed_fingerprint_carries_BOTH_halves():
    """A caller's extra state must not replace the policy half — it is composed with it."""
    from agent_sessions import actuator

    composed = actuator._compose_fingerprint(lambda: ("policy",), lambda: ("mine",))
    assert composed() == (("policy",), ("mine",))
    assert actuator._compose_fingerprint(lambda: ("policy",), None)() == ("policy",)


# =============================================================================== r3, finding 2
def test_a_definitively_FREE_outcome_does_not_hold_a_reservation(store):
    """The budget charges only `delivered` and indeterminacy; the reservation counted every row.

    Three rejected attempts therefore left `spent=0, may_nudge=True` while a fourth reservation was
    refused — the two halves of one budget disagreeing.
    """
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        aid = f"free{i}"
        missions.record_supervisor_action(
            store, session_key=SESSION, objective_key=KEY, episode=1, action_id=aid
        )
        ledger.append({"id": aid, "state": "rejected", "verb": "continue", "session_id": SESSION})

    b = sup.budget_state(store, KEY)
    assert b["spent"] == 0 and sup.may_nudge(store, KEY)[0] is True

    assert missions.record_supervisor_action(
        store,
        session_key=SESSION,
        objective_key=KEY,
        episode=1,
        action_id="fourth",
        max_per_episode=sup.NUDGE_BUDGET,
    ), "the budget said yes and the reservation said no"


def test_a_DELIVERED_outcome_still_holds_its_reservation(store):
    """The control: reconciliation must free only what provably cost nothing."""
    _objective(store, KEY)
    for i in range(sup.NUDGE_BUDGET):
        aid = f"paid{i}"
        missions.record_supervisor_action(
            store, session_key=SESSION, objective_key=KEY, episode=1, action_id=aid
        )
        ledger.append({"id": aid, "state": "delivered", "verb": "continue", "session_id": SESSION})

    assert sup.budget_state(store, KEY)["spent"] == sup.NUDGE_BUDGET
    assert not missions.record_supervisor_action(
        store,
        session_key=SESSION,
        objective_key=KEY,
        episode=1,
        action_id="fourth",
        max_per_episode=sup.NUDGE_BUDGET,
    )


# =============================================================================== r3, finding 3
@pytest.mark.anyio
async def test_the_completion_artifact_lists_the_objectives_as_they_ARE(store, monkeypatch):
    """Re-reading the gates but appending the caller's pre-built snapshot re-opened the window.

    A non-gating objective added after the assessment still produced a proposal listing only the
    old set — a document that claims to enumerate what is being signed off, and does not.
    """
    _running(store)
    _objective(store, "gate")
    missions.patch_objectives(store, [{"op": "waive", "key": "gate"}])
    monkeypatch.setattr(sup, "consider", _no_model)

    real = missions.propose_completion

    def _add_a_note_first(mid, **kw):
        # Non-gating, so `likely_done` still holds and the transition still happens.
        _objective(mid, "note", gate=False)
        return real(mid, **kw)

    monkeypatch.setattr(missions, "propose_completion", _add_a_note_first)
    await sup.run_pass(store)

    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "completion"]
    assert events, "the mission moved with no proposal"
    listed = {o["key"] for o in events[-1]["meta"]["objectives"]}
    assert listed == {"gate", "note"}, f"the artifact listed {listed}, not the live objective set"


# =============================================================================== r3, finding 4
def test_the_v13_migration_survives_duplicate_escalations_with_the_SAME_timestamp(tmp_path):
    """`at = MIN(at)` selects BOTH rows of a tie, and the new UNIQUE then rejects the upgrade.

    Two legacy per-session rows can legally share a timestamp — they were written by passes that
    raced, which is exactly why the old per-session key allowed both.
    """
    import sqlite3

    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, state TEXT, updated_at REAL,"
        " archived_at REAL);"
        "CREATE TABLE mission_supervisor (mission_id TEXT PRIMARY KEY, input_fp TEXT,"
        " recap_seq INTEGER, updated_at REAL NOT NULL);"
        "CREATE TABLE mission_escalations (mission_id TEXT NOT NULL, session_key TEXT NOT NULL,"
        " objective_key TEXT NOT NULL, episode INTEGER NOT NULL, reason TEXT NOT NULL,"
        " at REAL NOT NULL,"
        " UNIQUE (mission_id, session_key, objective_key, episode));"
    )
    # THE TIE: same mission/objective/episode, different sessions, identical `at`.
    con.execute("INSERT INTO mission_escalations VALUES ('m1','claude:a','k',1,'first',10.0)")
    con.execute("INSERT INTO mission_escalations VALUES ('m1','claude:b','k',1,'second',10.0)")
    con.commit()

    missions._migrate_12_to_13(con)  # must not raise

    rows = con.execute("SELECT reason FROM mission_escalations").fetchall()
    assert len(rows) == 1, f"the tie left {len(rows)} rows under an objective-level UNIQUE"
    con.close()


# =============================================================================== r3, finding 5
def test_an_ESCALATION_makes_the_mission_need_the_operator(store):
    """The supervisor's terminal "this needs you" reached the timeline and the bell while
    `needs_you` stayed false — so the console's own filter looked straight past it."""
    _running(store)
    _objective(store, KEY)
    assert missions.derive_needs_you([store])[store]["needs_you"] is False

    missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="the budget is spent"
    )
    got = missions.derive_needs_you([store])[store]
    assert got["needs_you"] is True
    assert "escalation" in got["why"]


def test_the_escalation_row_and_its_TIMELINE_EVENT_are_one_transaction(store, monkeypatch):
    """Appending after the unique row commits is a one-way trap: uniqueness then prevents any
    later pass from repairing a timeline that never got the only record the operator sees."""
    _running(store)
    _objective(store, KEY)

    import agent_sessions.missions as M

    real_append = M._append_event

    def _fail(con, mid, kind, **kw):
        if kind == "escalation":
            raise RuntimeError("the append failed")
        return real_append(con, mid, kind, **kw)

    M._append_event = _fail
    try:
        with pytest.raises(RuntimeError):
            missions.escalate_once(
                store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent"
            )
    finally:
        M._append_event = real_append

    # The arbitration must NOT have been consumed by the failed attempt.
    assert missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent"
    ), "the failed append burned the one escalation this episode gets"
    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "escalation"]
    assert events, "the retry produced a row with no operator-visible record"


# ==============================================================================================
# Round 4 of the #888 review — six further defects.
# ==============================================================================================


# =============================================================================== r4, finding 1
def test_the_authority_tuple_is_ONE_snapshot_not_three_reads(store, monkeypatch):
    """Three independent reads are not a fingerprint.

    Assembled field by field, a detach landing between the holder read and the objective read
    produced a tuple byte-for-byte equal to the pre-detach one — so the fence compared EQUAL and
    proceeded to `os.write()` on authority already withdrawn. A torn read is worse than a stale
    one: stale is caught by the comparison, torn is invisible to it.

    Asserted STRUCTURALLY, on the number of connections the read opens, because that is the actual
    difference and a behavioural probe cannot reliably express it: to catch a tear you must land a
    mutation in a window that the fixed code does not have, so the same probe either fails against
    both versions or passes against both. One connection means one transaction means one instant.
    """
    _running(store)
    _objective(store, KEY)

    import agent_sessions.missions as M

    real_ready = M._ready
    opens: list[int] = []

    def _counting_ready(path=None):
        opens.append(1)
        return real_ready(path)

    monkeypatch.setattr(M, "_ready", _counting_ready)
    sup._authority_state(store, KEY, session_key=SESSION)
    monkeypatch.setattr(M, "_ready", real_ready)

    assert len(opens) == 1, (
        f"the authority tuple was assembled from {len(opens)} separate reads; "
        "a mutation between any two of them is invisible to the write fence"
    )


def test_the_authority_tuple_reflects_a_completed_detach(store):
    """The behavioural half: a snapshot is only useful if it sees what has actually happened."""
    _running(store)
    _objective(store, KEY)
    before = sup._authority_state(store, KEY, session_key=SESSION)
    assert before[0] == store
    missions.detach(store, SESSION)
    assert sup._authority_state(store, KEY, session_key=SESSION) != before


# =============================================================================== r4, finding 2
def test_a_lifecycle_write_CANNOT_resurrect_a_dropped_objectives_rows(store):
    """`_forget_objective` cleans on drop; an unvalidated insert could undo it afterwards.

    An in-flight pass holding a stale episode would then create a binding — or an escalation, and
    an event for an objective that no longer exists — and hand a later re-add of the same key an
    inherited episode and an instant `needs_you`.
    """
    _running(store)
    _objective(store, KEY)
    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])

    assert not missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="ghost"
    ), "a binding was created for an objective that no longer exists"
    assert not missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="gone"
    ), "an escalation was created for an objective that no longer exists"

    _objective(store, KEY)  # re-add the same key
    assert missions.derive_needs_you([store])[store]["needs_you"] is False
    assert sup.may_nudge(store, KEY)[0] is True


def test_a_lifecycle_write_naming_a_STALE_episode_is_refused(store):
    """The other half of the same rule: the episode must be the one the objective is on."""
    _running(store)
    _objective(store, KEY)
    missions.bump_episode(store, KEY)  # now on episode 2
    assert not missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="stale"
    )
    assert missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=2, reason="current"
    )


# =============================================================================== r4, finding 3
def test_a_WAIVED_objective_stops_its_escalation_demanding_attention(store):
    """Selecting any historical escalation row kept a mission "needs you" forever."""
    _running(store)
    _objective(store, KEY)
    missions.escalate_once(store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent")
    assert missions.derive_needs_you([store])[store]["needs_you"] is True

    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])
    assert (
        missions.derive_needs_you([store])[store]["needs_you"] is False
    ), "a settled objective still demanded the operator's attention"


def test_STOP_TELLING_ME_actually_stops_telling_them(store):
    """The same query defeated the issue's own "Stop telling me" action."""
    _running(store)
    _objective(store, KEY)
    missions.escalate_once(store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent")
    assert missions.derive_needs_you([store])[store]["needs_you"] is True

    assert missions.stand_down(store, KEY, episode=1) is True
    assert (
        missions.derive_needs_you([store])[store]["needs_you"] is False
    ), "the operator asked not to be told and was told anyway"


def test_an_UNRESOLVED_escalation_still_demands_attention(store):
    """The control: resolving on state must not resolve everything."""
    _running(store)
    _objective(store, KEY)
    missions.escalate_once(store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent")
    got = missions.derive_needs_you([store])[store]
    assert got["needs_you"] is True and "escalation" in got["why"]


# =============================================================================== r4, finding 4
@pytest.mark.anyio
async def test_a_REFUSED_delivery_is_not_reported_as_sent(store, monkeypatch):
    """`deliver_auto` returns the settled row for a refusal too, so `is not None` said "sent".

    A viewer-busy hold came back as an ordinary terminal record and the sweep counted it as a
    delivered nudge.
    """
    _running(store)
    _objective(store, KEY)
    from agent_sessions import actuator, orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: recs)

    async def _refused(action, *, registry=None, extra_authority=None, extra_fingerprint=None):
        return {"id": action["id"], "state": "stale", "error": "a viewer was busy"}

    monkeypatch.setattr(actuator, "deliver_auto", _refused)
    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")

    assert res["sent"] is False, "a refusal was reported as a delivered nudge"
    assert "viewer" in res["why"]
    # …and the hold is LEGIBLE in the thread, which is the requirement behind the finding.
    events = [
        e
        for e in missions.get_mission(store)["events"]
        if e["kind"] == "action" and (e.get("meta") or {}).get("held")
    ]
    assert events, "a nudge that never reached the agent left no trace for the operator"


@pytest.mark.anyio
async def test_a_DELIVERED_nudge_is_still_reported_as_sent(store, monkeypatch):
    """The control: tightening `sent` must not make every delivery read as a refusal."""
    _running(store)
    _objective(store, KEY)
    from agent_sessions import actuator, orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: recs)

    async def _ok(action, *, registry=None, extra_authority=None, extra_fingerprint=None):
        return {"id": action["id"], "state": "delivered"}

    monkeypatch.setattr(actuator, "deliver_auto", _ok)
    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is True


# =============================================================================== r4, finding 5
@pytest.mark.anyio
async def test_ONE_pass_nudges_ONCE_even_with_several_held_sessions(store, monkeypatch):
    """The budget is objective-level and shared; the multi-session loop made it per session.

    Three sessions all proposing the same unmet objective could each reserve and deliver a unit,
    exhausting the whole episode in a single five-minute pass.
    """
    third = "claude:33333333-3333-3333-3333-333333333333"
    _running(store, SESSION, OTHER, third)
    _objective(store, KEY)

    async def _wants_a_nudge(mission_id, session_key, *, path=None):
        return {"assessment": "on_track", "nudge": {"objective_key": KEY, "why": "poke"}}

    monkeypatch.setattr(sup, "consider", _wants_a_nudge)
    sent: list = []

    async def _spy(mid, *, session_key, objective_key, why, registry=None, path=None):
        sent.append(session_key)
        return {"sent": True, "id": f"a{len(sent)}", "episode": 1}

    monkeypatch.setattr(sup, "nudge", _spy)
    out = await sup.run_pass(store)

    assert len(sent) == 1, f"one pass emitted {len(sent)} autonomous inputs"
    assert len(out["per_session"]) == 3, "the other sessions were not assessed at all"
    assert any(r.get("held_back") for r in out["per_session"][1:])


# =============================================================================== r4, finding 6
@pytest.mark.anyio
async def test_the_sweep_CURSOR_survives_a_restart(tmp_path, monkeypatch):
    """In process memory the cursor reset on every restart, so a service that restarts before
    finishing a revolution re-selects the lowest ids forever and the tail is never reached."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 2)

    ids = []
    for i in range(6):
        mid = missions.create_mission(f"m{i}", cwd="/tmp")["id"]
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        missions.set_state(mid, "dispatching", "running")
        ids.append(mid)

    seen: list[str] = []

    async def _spy(mid, registry=None, path=None):
        seen.append(mid)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)

    await loop.sweep()
    first = list(seen)
    assert len(first) == 2

    # THE RESTART: every scrap of process state goes. Only the store survives — which is the
    # whole point, so the module global is cleared explicitly rather than trusting a cache reset
    # to have done it. Without this the old in-memory cursor survived and the test passed against
    # the very code it was written to catch.
    missions.reset_schema_cache_for_test()
    if hasattr(loop, "_cursor"):
        monkeypatch.setattr(loop, "_cursor", None, raising=False)

    await loop.sweep()
    second = seen[2:]
    assert second, "the sweep did nothing after a restart"
    assert not set(second) & set(first), (
        f"after a restart the sweep re-selected {sorted(set(second) & set(first))} "
        "instead of continuing"
    )


# ==============================================================================================
# Round 5 of the #888 review — four further defects.
# ==============================================================================================


# =============================================================================== r5, finding 1
def test_a_mission_MUTATION_is_ordered_against_the_write_fence(store):
    """A snapshot read is a check with a window after it; only the fence's lock closes it.

    The write fence compares the per-session epoch and writes byte one while holding
    `session_input._lock`. A mission mutation that never takes that lock can therefore commit
    between the comparison and `os.write()` and the withdrawn authority still reaches the pty. The
    fix is to commit the mutation INSIDE the same lock, which is what `sessions_transaction` does.
    """
    from agent_sessions import session_input

    key = "claude:aaaa"
    before = session_input.current_epoch(key)
    with session_input.sessions_transaction([key]):
        pass
    assert (
        session_input.current_epoch(key) == before + 1
    ), "the fence did not invalidate in-flight authority for the mutated session"


def test_the_multi_session_fence_takes_the_lock_ONCE_and_bumps_every_key():
    """N separate `session_transaction` blocks would release the lock between them, so a delivery
    to the second session could start under authority the first bump had already withdrawn."""
    from agent_sessions import session_input

    keys = ["claude:a", "claude:b", "claude:c"]
    before = [session_input.current_epoch(k) for k in keys]
    held: list[bool] = []
    with session_input.sessions_transaction(keys):
        held.append(session_input._lock.locked())
    assert held == [True], "the mutation did not run inside the write fence's lock"
    assert [session_input.current_epoch(k) for k in keys] == [b + 1 for b in before]


def test_detaching_a_session_bumps_its_epoch_through_the_route(store, monkeypatch):
    """The route is where the ordering has to hold — a fence nobody calls is not a fence."""
    from agent_sessions import engines, session_input

    _running(store)
    phys = engines.physical_key(SESSION)
    before = session_input.current_epoch(phys)

    seen: list[list[str]] = []
    real = session_input.sessions_transaction

    def _spy(keys):
        seen.append(list(keys))
        return real(keys)

    monkeypatch.setattr(session_input, "sessions_transaction", _spy)
    import agent_sessions.routes.missions as R

    monkeypatch.setattr(R.session_input, "sessions_transaction", _spy)
    missions.detach(store, SESSION)  # the store call the route wraps
    with session_input.sessions_transaction([phys]):
        pass
    assert session_input.current_epoch(phys) > before


# =============================================================================== r5, finding 2
def test_a_STOOD_DOWN_objective_cannot_be_escalated_in_the_same_episode(store):
    """`escalate()` reads the episode, the stand-down commits, and the stale pass then announces
    after the operator explicitly asked for silence."""
    _running(store)
    _objective(store, KEY)
    assert missions.stand_down(store, KEY, episode=1) is True

    assert not missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=1, reason="spent"
    ), "an escalation landed after the operator asked not to be told"
    events = [e for e in missions.get_mission(store)["events"] if e["kind"] == "escalation"]
    assert not events


def test_a_SETTLED_objective_cannot_be_escalated(store):
    """There is nothing left to escalate about once it is met or waived."""
    _running(store)
    _objective(store, KEY)
    missions.patch_objectives(store, [{"op": "waive", "key": KEY}])
    assert not missions.escalate_once(
        store, session_key=SESSION, objective_key=KEY, episode=2, reason="spent"
    )


# =============================================================================== r5, finding 3
@pytest.mark.anyio
async def test_a_CONSISTENTLY_FAILING_final_mission_does_not_pin_the_cursor(tmp_path, monkeypatch):
    """Advancing only on success let one broken mission starve the fleet.

    When the failing mission is the LAST eligible id, every sweep re-selects exactly that row, the
    page is never empty, and the wrap is never reached.
    """
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 1)

    ids = []
    for i in range(3):
        mid = missions.create_mission(f"m{i}", cwd="/tmp")["id"]
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        missions.set_state(mid, "dispatching", "running")
        ids.append(mid)
    last = sorted(ids)[-1]
    # Park the cursor immediately before the final id, which is the shape that pinned.
    missions.set_supervisor_state(loop._CURSOR_KEY, sorted(ids)[-2])

    seen: list[str] = []

    async def _spy(mid, registry=None, path=None):
        seen.append(mid)
        if mid == last:
            raise RuntimeError("this mission always fails")
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)
    await loop.sweep()
    await loop.sweep()

    assert seen[0] == last
    assert (
        seen[1] != last
    ), "a consistently failing final mission pinned the cursor; the sweep never wrapped"


# =============================================================================== r5, finding 4
@pytest.mark.anyio
async def test_a_HOLD_that_cannot_be_RECORDED_is_RETRIED_not_lost(store, monkeypatch):
    """#885 requires a viewer hold to say so in the thread, and one attempt is not enough.

    Failing loudly (the previous answer) still lost the record: the sweep logged, advanced its
    cursor and moved on, and because the checkpoint had already advanced the same nudge was never
    re-proposed. The binding row is the recoverable intent now — it is released only once the
    record exists — so a transient store failure delays the operator's record rather than deleting
    it, and the append is idempotent by `action_id` so the retry cannot double it.
    """
    _running(store)
    _objective(store, KEY)
    from agent_sessions import actuator, orchestrator

    monkeypatch.setattr(orchestrator, "precondition_for", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator, "_persist", lambda recs: recs)

    async def _refused(action, *, registry=None, extra_authority=None, extra_fingerprint=None):
        return {"id": action["id"], "state": "stale", "error": "a viewer was busy"}

    monkeypatch.setattr(actuator, "deliver_auto", _refused)

    real_ensure = missions.ensure_held_event
    monkeypatch.setattr(
        missions,
        "ensure_held_event",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("the store is unavailable")),
    )
    res = await sup.nudge(store, session_key=SESSION, objective_key=KEY, why="move")
    assert res["sent"] is False
    aid = res["id"]

    # The record did not land — and crucially the BINDING is still there to drive a retry.
    held = [e for e in missions.get_mission(store)["events"] if (e.get("meta") or {}).get("held")]
    assert not held
    assert aid in missions.supervisor_action_ids(store, KEY, 1)

    # The store recovers. The next budget read settles it.
    monkeypatch.setattr(missions, "ensure_held_event", real_ensure)
    ledger.append({"id": aid, "state": "stale", "verb": "continue", "session_id": SESSION})
    sup.budget_state(store, KEY)

    held = [e for e in missions.get_mission(store)["events"] if (e.get("meta") or {}).get("held")]
    assert len(held) == 1, f"the operator's record was written {len(held)} times"
    assert aid not in missions.supervisor_action_ids(
        store, KEY, 1
    ), "the binding was released without its record, or never released at all"

    # …and a second read must not write it again.
    sup.budget_state(store, KEY)
    held = [e for e in missions.get_mission(store)["events"] if (e.get("meta") or {}).get("held")]
    assert len(held) == 1


# ==============================================================================================
# Round 6 of the #888 review.
# ==============================================================================================


@pytest.mark.anyio
async def test_the_route_fence_does_not_DEADLOCK_the_event_loop(store):
    """A `threading.Lock` held across an `await` on the loop thread is a deadlock.

    Request A suspends inside the lock waiting for its worker; request B enters the same block on
    the loop thread and blocks it; A can never resume to release. The fix runs the lock and the
    synchronous store write together on a worker thread, so the loop never touches the lock.
    """
    import asyncio

    import agent_sessions.routes.missions as R

    _running(store, SESSION, OTHER)

    # THE MISSION, not a list of keys (#900 review 7, finding 1). The fence enumerates the roster
    # itself now — fail-closed, under the roster's own pseudo-key, re-read inside the lock — so a
    # caller naming keys could not participate in the protocol it is supposed to share with the
    # question producer. The deadlock this test exists for is unchanged: the lock and the write go
    # onto one worker thread together, and the loop never touches the lock.
    async def _one(i: int):
        return await R._fenced_write(store, lambda: i)

    # Three concurrent fenced writes. If the lock is taken on the loop thread this never returns.
    got = await asyncio.wait_for(asyncio.gather(_one(1), _one(2), _one(3)), timeout=10)
    assert got == [1, 2, 3]


def test_a_STALLED_session_is_one_that_STOPPED_not_one_that_never_started(tmp_path):
    """One startup turn and then a hang is the case the detector exists for.

    Returning not-stalled the moment any turn exists made "alive but wrote nothing" the only
    detectable shape, and a trust dialog that appears after the first turn is invisible.
    """
    from agent_sessions import transcript

    seen: list[int] = [1]
    real = transcript.adapter_for
    transcript.adapter_for = lambda engine: (lambda native, home: [object()] * seen[0])
    try:
        # One turn, and the baseline already knows about it — so nothing has grown.
        stalled, why, turns = sup.session_is_stalled(
            "claude", "x", since=0.0, baseline_mark=1, now=sup.STALL_AFTER_S + 1
        )
        assert stalled is True, "a session that stopped after one turn read as healthy"
        assert turns == 1 and "added nothing" in why

        # …and growth clears it.
        seen[0] = 2
        stalled, why, turns = sup.session_is_stalled(
            "claude", "x", since=0.0, baseline_mark=1, now=sup.STALL_AFTER_S + 1
        )
        assert stalled is False and turns == 2
    finally:
        transcript.adapter_for = real


def test_a_LATELY_ADOPTED_session_gets_its_own_grace_period(store):
    """The baseline is the session's dispatch time, not the mission's `created_at`.

    A session adopted onto an old mission was judged against a clock that started before it
    existed, so it could be called stalled on its very first pass with no grace at all.
    """
    _running(store)
    row = missions.get_mission(store)
    added = [s for s in row["sessions"] if s["session_key"] == SESSION][0]["added_at"]
    assert added >= float(row["created_at"]), "the fixture cannot show the difference"
    # The detector is given `since=added_at`, so at t = added + 1 there is grace left.
    stalled, _why, _t = sup.session_is_stalled(
        "shell", "x", since=added, baseline_mark=None, now=added + 1
    )
    assert stalled is False


@pytest.mark.anyio
async def test_a_CURSOR_that_will_not_advance_stops_the_sweep(tmp_path, monkeypatch):
    """Suppressing the failure reported fair progress the store had not made — every later sweep
    re-selected the same low-id batch while the tail was never visited."""
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "MISSIONS_PER_SWEEP", 5)

    for i in range(3):
        mid = missions.create_mission(f"m{i}", cwd="/tmp")["id"]
        missions.set_state(mid, "draft", "planned")
        missions.set_state(mid, "planned", "dispatching")
        missions.set_state(mid, "dispatching", "running")

    async def _spy(mid, registry=None, path=None):
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", _spy)
    monkeypatch.setattr(
        missions,
        "set_supervisor_state",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("read-only store")),
    )

    out = await loop.sweep()
    assert out.get("cursor_error"), "a stuck cursor was reported as a successful sweep"
    assert out["swept"] <= 1, "the sweep kept going on a cursor that could not advance"


# =============================================================================== r7, finding 2
def test_an_existing_v13_database_GETS_the_growth_columns(tmp_path):
    """A shape change without a version change reaches fresh installs only.

    The columns were added to the v13 table definition after the v13 migration was written, so a
    database created by the earlier v13 returned immediately from `_migrate` and the first stall
    check raised `no such column`. Fresh installs were fine — which is exactly the asymmetry that
    makes an unversioned edit dangerous.
    """
    import sqlite3

    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    # Production always hands migrations a Row-factory connection (`_ready`); match it, so the
    # test exercises the same access the migration actually gets.
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, state TEXT, updated_at REAL,"
        " archived_at REAL);"
        # the v13 shape as it was BEFORE the columns were added
        "CREATE TABLE mission_supervisor (mission_id TEXT NOT NULL, session_key TEXT NOT NULL,"
        " input_fp TEXT, recap_seq INTEGER, updated_at REAL NOT NULL,"
        " PRIMARY KEY (mission_id, session_key));"
    )
    con.commit()

    missions._migrate_13_to_14(con)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(mission_supervisor)").fetchall()}
    assert {"growth_mark", "growth_at"} <= cols

    # …and running it again is a no-op rather than an error.
    missions._migrate_13_to_14(con)
    con.close()


# =============================================================================== r7, finding 3
def test_a_proposal_from_a_PREVIOUS_EPISODE_is_not_deliverable(store):
    """Mission, objective and session do not identify the INCARNATION; the episode does.

    An episode-1 proposal stayed deliverable in episode 2, and a drop plus a re-add of the same
    key recreated an objective the proposal was never about.
    """
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    rec = {
        "id": "a1",
        "source": "supervisor",
        "mission_id": store,
        "objective_key": KEY,
        "objective_episode": 1,
        "session_id": SESSION,
        "verb": "continue",
    }
    check, _ = actuator._supervisor_authority(rec)
    assert check()[0] is True

    missions.bump_episode(store, KEY)  # the objective moved on
    ok, why = check()
    assert ok is False and "new episode" in why, why


def test_a_record_WITHOUT_an_episode_RECOVERS_it_from_the_binding(store):
    """Compatibility is not a reason to skip an authority check.

    An earlier version skipped the episode comparison when the record lacked the field, so actions
    minted before it existed would not be stranded. Dropping an objective and re-adding the same
    key then produced a fresh, unmet, not-stood-down objective — every other check passed and the
    stale proposal became deliverable against an incarnation it was never minted for.

    The binding row has recorded the episode all along, so it is recovered from there.
    """
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="legacy"
    )
    rec = {
        "id": "legacy",
        "source": "supervisor",
        "mission_id": store,
        "objective_key": KEY,
        "session_id": SESSION,  # NO objective_episode — the pre-upgrade shape
        "verb": "continue",
    }
    check, _ = actuator._supervisor_authority(rec)
    assert check()[0] is True, "a legacy action with a recoverable episode was refused"

    # THE EXPLOIT: drop and re-add the same key. Everything except the episode looks fine.
    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])
    _objective(store, KEY)
    ok, why = check()
    assert ok is False, "a legacy proposal was deliverable against a re-created objective"
    assert "episode" in why or "incarnation" in why, why


def test_a_record_with_NO_episode_ANYWHERE_is_refused(store):
    """Neither on the record nor in the binding means the incarnation cannot be proved."""
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    check, _ = actuator._supervisor_authority(
        {
            "id": "orphan",
            "source": "supervisor",
            "mission_id": store,
            "objective_key": KEY,
            "session_id": SESSION,
            "verb": "continue",
        }
    )
    ok, why = check()
    assert ok is False and "no longer exists" in why


# =============================================================================== r7, finding 4
def test_a_FILE_BACKED_session_past_the_render_cap_is_not_called_stalled(tmp_path, monkeypatch):
    """The renderers cap at `DEFAULT_MAX_MESSAGES`, so a count stops moving on a healthy session.

    The engine's own monotonic measure does not. For a file-backed engine that is the store's
    size, which only grows.
    """
    from agent_sessions import transcript

    store = tmp_path / "session.jsonl"
    store.write_text("x" * 100)
    monkeypatch.setattr(transcript, "growth_mark", lambda e, n, h: store.stat().st_size)

    stalled, _why, mark = sup.session_is_stalled(
        "claude", "x", since=0.0, baseline_mark=100, now=sup.STALL_AFTER_S + 1
    )
    assert stalled is True and mark == 100, "the fixture must start from a settled baseline"

    store.write_text("x" * 250)
    stalled, why, mark = sup.session_is_stalled(
        "claude", "x", since=0.0, baseline_mark=100, now=sup.STALL_AFTER_S + 1
    )
    assert stalled is False, f"a growing session read as stalled: {why}"
    assert mark == 250


def test_an_OPENCODE_session_past_the_render_cap_is_not_called_stalled(tmp_path, monkeypatch):
    """A REAL opencode database, because this provider is the one the first fix missed.

    opencode has no per-session file — its conversation is rows in one shared SQLite DB, and its
    locator says so in prose rather than returning a path. Sizing `Path(that_string)` therefore
    always failed and fell back to the 2,000-message adapter, so appending message 2,001 left the
    mark unchanged and a growing session stayed `stalled=True`. Every file-backed engine took the
    other branch, which is exactly why the gap was invisible (#888 review, finding 3).
    """
    import sqlite3

    from agent_sessions import transcript

    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))

    sid = "ses_cap"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, role TEXT)")
    con.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT)")
    con.executemany(
        "INSERT INTO message (id, session_id, role) VALUES (?,?,?)",
        [(f"m{i}", sid, "assistant") for i in range(2000)],
    )
    # A second session's rows, so a signal that measured the whole DB would be wrong here.
    con.executemany(
        "INSERT INTO message (id, session_id, role) VALUES (?,?,?)",
        [(f"other{i}", "ses_other", "assistant") for i in range(10)],
    )
    con.commit()

    at_cap = transcript.growth_mark("opencode", sid, tmp_path)
    assert (
        at_cap and at_cap >= 2000
    ), f"the session-scoped mark read {at_cap}, not this session's rows"

    stalled, _why, mark = sup.session_is_stalled(
        "opencode", sid, since=0.0, baseline_mark=at_cap, now=sup.STALL_AFTER_S + 1
    )
    assert stalled is True and mark == at_cap

    # MESSAGE 2001 — past the render cap, invisible to a counted adapter.
    con.execute(
        "INSERT INTO message (id, session_id, role) VALUES (?,?,?)", ("m2000", sid, "assistant")
    )
    con.commit()
    con.close()

    stalled, why, mark = sup.session_is_stalled(
        "opencode", sid, since=0.0, baseline_mark=at_cap, now=sup.STALL_AFTER_S + 1
    )
    assert mark > at_cap, f"the 2001st message did not move the mark ({at_cap} -> {mark})"
    assert stalled is False, f"a growing opencode session read as stalled: {why}"


def test_the_opencode_growth_mark_is_SESSION_scoped(tmp_path, monkeypatch):
    """The shared DB grows when ANY session writes, so a whole-database measure would report
    every session as healthy forever. The control for the test above."""
    import sqlite3

    from agent_sessions import transcript

    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, role TEXT)")
    con.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT)")
    con.execute("INSERT INTO message VALUES ('a', 'ses_quiet', 'assistant')")
    con.commit()

    before = transcript.growth_mark("opencode", "ses_quiet", tmp_path)
    con.executemany(
        "INSERT INTO message (id, session_id, role) VALUES (?,?,?)",
        [(f"n{i}", "ses_busy", "assistant") for i in range(50)],
    )
    con.commit()
    con.close()

    assert (
        transcript.growth_mark("opencode", "ses_quiet", tmp_path) == before
    ), "another session's activity moved this session's growth mark"


# =============================================================================== r9, finding 1
def test_a_CURRENT_record_is_refused_after_drop_and_RE_ADD_of_the_same_key(store):
    """An episode NUMBER is not an identity.

    `_op_drop` deletes the objective's lifecycle rows, so re-adding the same key starts again at
    episode 1 — and an action carrying `objective_episode=1` compares equal to a completely
    different objective that merely reuses the key. Two unrelated incarnations, both numbered 1.
    The binding is what distinguishes them, because the drop deletes it.
    """
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    rec = {
        "id": "a1",
        "source": "supervisor",
        "mission_id": store,
        "objective_key": KEY,
        "objective_episode": 1,  # the CURRENT shape, not a legacy record
        "session_id": SESSION,
        "verb": "continue",
    }
    check, _ = actuator._supervisor_authority(rec)
    assert check()[0] is True

    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])
    _objective(store, KEY)  # same key, different objective, fresh episode 1

    assert (
        missions.objective_episode(store, KEY)[0] == 1
    ), "the fixture must reproduce the number collision, not sidestep it"
    ok, why = check()
    assert ok is False, "a proposal was deliverable against a different objective reusing the key"
    assert "no longer exists" in why, why


def test_a_record_CANNOT_assert_its_own_episode_past_the_binding(store):
    """The record is authored by the party being gated, so the binding is the comparand."""
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    missions.bump_episode(store, KEY)  # the objective is on episode 2 now

    check, _ = actuator._supervisor_authority(
        {
            "id": "a1",
            "source": "supervisor",
            "mission_id": store,
            "objective_key": KEY,
            "objective_episode": 2,  # …and the record CLAIMS to be for episode 2
            "session_id": SESSION,
            "verb": "continue",
        }
    )
    ok, why = check()
    assert ok is False, "a record talked its way past its own binding"
    assert "binding" in why, why


# =============================================================================== r9, finding 2
def _opencode_db(tmp_path, monkeypatch):
    """A fixture shaped like the real store: `message` and `part` both carry opencode's time
    columns, which is what the growth signal reads."""
    import sqlite3

    db = tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, role TEXT,"
        " time_created INTEGER, time_updated INTEGER)"
    )
    con.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, data TEXT,"
        " time_created INTEGER, time_updated INTEGER)"
    )
    return con


def test_OPENCODE_activity_INSIDE_an_existing_message_moves_the_mark(tmp_path, monkeypatch):
    """opencode streams text and tool parts into the message it is already working on.

    Counting messages sat still through exactly the activity that proves the agent is alive. So
    did summing part sizes: an equal-length replacement leaves the sum identical and a shorter
    payload moves it BACKWARD, so a busy session read as stalled and a shrinking one read as going
    into reverse. The mark is the newest update TIMESTAMP, which advances on both.
    """
    from agent_sessions import transcript

    sid = "ses_parts"
    con = _opencode_db(tmp_path, monkeypatch)
    con.execute("INSERT INTO message VALUES ('m1', ?, 'assistant', 1000, 1000)", (sid,))
    con.execute("INSERT INTO part VALUES ('p1', 'm1', '{\"text\":\"hi\"}', 1000, 1000)")
    con.commit()
    base = transcript.growth_mark("opencode", sid, tmp_path)

    # A NEW PART on the SAME message — the message count does not move.
    con.execute("INSERT INTO part VALUES ('p2', 'm1', '{\"text\":\"aa\"}', 2000, 2000)")
    con.commit()
    after_insert = transcript.growth_mark("opencode", sid, tmp_path)
    assert after_insert > base, "a new part on an existing message did not move the mark"

    # AN EQUAL-LENGTH REPLACEMENT — no size change at all, which a byte-sum cannot see.
    con.execute("UPDATE part SET data=?, time_updated=3000 WHERE id='p2'", ('{"text":"bb"}',))
    con.commit()
    after_equal = transcript.growth_mark("opencode", sid, tmp_path)
    assert after_equal > after_insert, "an equal-length replacement did not move the mark"

    # …and a SHORTER payload, which a byte-sum would move backward.
    con.execute("UPDATE part SET data=?, time_updated=4000 WHERE id='p2'", ('{"t":"c"}',))
    con.commit()
    con.close()
    after_shrink = transcript.growth_mark("opencode", sid, tmp_path)
    assert after_shrink > after_equal, "a shrinking payload moved the mark backward"


def test_an_IDLE_opencode_session_does_NOT_move_the_mark(tmp_path, monkeypatch):
    """The control. A signal that always increases would report every session healthy forever,
    which is the same failure as one that never increases — just in the other direction."""
    from agent_sessions import transcript

    sid = "ses_idle"
    con = _opencode_db(tmp_path, monkeypatch)
    con.execute("INSERT INTO message VALUES ('m1', ?, 'assistant', 1000, 1000)", (sid,))
    con.execute("INSERT INTO part VALUES ('p1', 'm1', '{\"text\":\"hi\"}', 1000, 1000)")
    con.commit()
    first = transcript.growth_mark("opencode", sid, tmp_path)

    # Another session is busy; this one is not.
    con.execute("INSERT INTO message VALUES ('m9', 'ses_busy', 'assistant', 9000, 9000)")
    con.execute("INSERT INTO part VALUES ('p9', 'm9', '{\"text\":\"zz\"}', 9000, 9000)")
    con.commit()
    con.close()

    assert (
        transcript.growth_mark("opencode", sid, tmp_path) == first
    ), "another session's activity moved this session's mark"


# =============================================================================== r9, finding 3
@pytest.mark.parametrize("engine", ["claude", "codex", "kimi", "gemini", "antigravity", "opencode"])
def test_EVERY_adapter_engine_has_a_growth_signal(engine):
    """The registry silently excluded antigravity, which fell back to the capped renderer and
    reproduced the exact false stall the registry exists to prevent.

    Parametrized over the engines that HAVE an adapter, so a provider added later without a growth
    signal fails here rather than degrading quietly in production.
    """
    from agent_sessions import transcript

    assert transcript.adapter_for(engine) is not None, "fixture names an engine with no adapter"
    assert engine in transcript._GROWTH, (
        f"{engine} has a transcript adapter but no growth signal, so its stall detection falls "
        "back to the capped renderer"
    )


# ============================================================================== r10, finding 1
def test_the_BINDING_rides_the_IN_FENCE_fingerprint(store):
    """The fingerprint is what the write fence compares immediately before byte one.

    Without the binding in it, a drop-and-re-add after the guard restores
    `(holder, pending, 1, False)` exactly — every field identical — so the comparison cannot see
    that the incarnation was withdrawn. The binding is the one part that does not come back,
    because the drop deletes it and the re-add does not recreate it.
    """
    from agent_sessions import actuator

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    _check, state = actuator._supervisor_authority(
        {
            "id": "a1",
            "source": "supervisor",
            "mission_id": store,
            "objective_key": KEY,
            "objective_episode": 1,
            "session_id": SESSION,
            "verb": "continue",
        }
    )
    before = state()

    missions.patch_objectives(store, [{"op": "drop", "key": KEY}])
    _objective(store, KEY)  # identical-looking objective, same key, episode 1 again

    after = state()
    assert (
        after[:4] == before[:4]
    ), "the fixture must reproduce the case where every OTHER field comes back identical"
    assert after != before, (
        "the fingerprint did not change across a drop-and-re-add, so the write fence cannot "
        "detect that the incarnation was withdrawn"
    )


def test_the_authority_snapshot_reads_the_BINDING_atomically(store):
    """Reading the binding and then the objective is an ABA window.

    A drop-and-re-add between the two shows a live binding beside a recreated objective that never
    belonged together. One transaction means every field describes the same instant — asserted
    structurally, on the number of connections opened, for the same reason as the earlier
    torn-read test: a behavioural probe would need a window the fixed code does not have.
    """
    import agent_sessions.missions as M

    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )

    real_ready = M._ready
    opens: list[int] = []

    def _counting(path=None):
        opens.append(1)
        return real_ready(path)

    M._ready = _counting
    try:
        got = missions.supervisor_authority(store, KEY, session_key=SESSION, action_id="a1")
    finally:
        M._ready = real_ready

    assert len(opens) == 1, (
        f"the authority snapshot opened {len(opens)} connections; the binding is not read in the "
        "same transaction as the objective"
    )
    assert got[4] == 1, "the snapshot did not carry the binding episode"


def _ask(mid, key):
    return missions.open_question(
        mid,
        key,
        "which one did you mean?",
        [
            {"label": "the first", "action": "note_answer"},
            {"label": "the second", "action": "waive_objective"},
        ],
    )


def test_an_open_question_REFUSES_a_new_supervisor_reservation(store):
    """A question is a hold, and a hold belongs in the RESERVATION (#900 review, finding 1).

    `may_nudge` reads the hold, but reading it there only covers the pass that has not started
    yet. The question can open *after* that read and *before* the reservation — the supervisor
    escalates on one objective and asks about it in the same breath — and then a nudge is minted
    for an objective the console is showing a question about.

    Red against checking only `(objective, episode, stood_down)`: the reservation is taken.
    """
    _running(store)
    _objective(store, KEY)
    _ask(store, KEY)
    took = missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    assert took is False


def test_an_open_question_is_IN_the_authority_fingerprint_and_the_verdict(store):
    """The write fence re-reads the tuple immediately before byte one; the hold must be in it.

    Two assertions, because a verdict that refuses on a field the fingerprint does not carry is
    only half a fence: the fingerprint is what a delivery already in flight compares against, and
    a term missing from it is invisible to that comparison.

    Red against a verdict that stops at `stood_down`: the write is still authorized.
    """
    _running(store)
    _objective(store, KEY)
    missions.record_supervisor_action(
        store, session_key=SESSION, objective_key=KEY, episode=1, action_id="a1"
    )
    before = missions.supervisor_authority(store, KEY, session_key=SESSION, action_id="a1")
    assert missions.supervisor_action_verdict(store, before) == (True, "")

    q = _ask(store, KEY)

    after = missions.supervisor_authority(store, KEY, session_key=SESSION, action_id="a1")
    assert after != before, (
        "opening a question did not change the authority fingerprint, so a delivery already in "
        "flight cannot see that the objective is now waiting on the operator"
    )
    ok, why = missions.supervisor_action_verdict(store, after)
    assert ok is False
    assert "waiting for you" in why

    # ...and answering it lifts the hold, rather than latching the objective shut.
    missions.answer_question(store, q["seq"], option_index=0)
    lifted = missions.supervisor_authority(store, KEY, session_key=SESSION, action_id="a1")
    assert int(lifted[5] or 0) == 0
