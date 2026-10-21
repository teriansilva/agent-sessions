"""A mission with NO gating objectives is not finished — it is unmeasurable (#1063).

The rule every completion surface asks is *"are there unmet gates?"*, and over an **empty gate
set** the answer is no — vacuously. A mission created with no playbook gets the notes-only
objective set (`probe: "none"`, `gate: 0`), so it satisfied that rule from the moment it started:
observed in the wild as `msn_f311fccf`, which flipped `running -> review` 80 seconds after
dispatch, `why: "every gate is met"`, while both of its objectives were pending and its agent was
still mid-turn.

Four sites asked it, which is why the answer now lives in one predicate rather than in four
places that already drifted once (`missions.unmet_gate_count`: *"One function so the three
callers cannot drift, which is how they drifted in the first place"*). Each test below names its
site, and each fails against the unfixed code — the point of the file is that none of them can
pass for the wrong reason.

The two directions that must NOT change are pinned here too: a real gate still completes, and a
**waived** gate is still settled. A waiver is the operator saying it was not required, and that
is not the same fact as there being nothing to require.
"""

from __future__ import annotations

import pytest

from agent_sessions import mission_questions as mq
from agent_sessions import mission_supervisor as sup
from agent_sessions import missions


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


def _running(instruction: str = "review all open bugs and bring them to viable state") -> str:
    m = missions.create_mission(instruction, cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    return m["id"]


def _notes(mid: str, *titles: str) -> None:
    """Exactly what instantiation produces with no playbook: model notes, nothing checkable.

    `probe: "none"` and `gate: False` are structural for a note — it checks nothing and gates
    nothing — so this is the real shape, not a contrived one.
    """
    missions.instantiate_objectives(
        mid,
        [
            {
                "key": f"note_{i + 1}",
                "title": t,
                "probe": "none",
                "gate": False,
                "source": "model",
            }
            for i, t in enumerate(titles)
        ],
    )


def _gate(mid: str, key: str = "merged", *, met: bool = False, waived: bool = False) -> None:
    missions.patch_objectives(
        mid,
        [
            {
                "op": "add",
                "key": key,
                "title": key,
                "probe": "forge_merged",
                "probe_args": None,
                "gate": True,
            }
        ],
    )
    if met:
        missions.observe_objective(mid, key, observed=True, value=True, detail="merged")
    if waived:
        missions.patch_objectives(mid, [{"op": "waive", "key": key}])


def _propose(mid: str):
    return missions.propose_completion(mid, from_state="running", render=lambda rows: ("t", {}))


def _completions(mid: str) -> list[dict]:
    return [e for e in missions.get_mission(mid)["events"] if e.get("kind") == "completion"]


# ---- site 1: the transaction that flips the state -------------------------------------------


def test_a_notes_only_mission_does_not_transition_to_review(store):
    """The observed bug, at its own call site.

    Unfixed, `propose_completion` counts unmet GATES, finds none because there are none, and
    commits — so this asserts the mission is still where the operator left it.
    """
    mid = _running()
    _notes(mid, "Review all open bugs", "Bring each to a viable state")

    assert _propose(mid) is False, "a mission with nothing to check proposed completion"
    assert missions.get_mission(mid)["state"] == "running"
    assert _completions(mid) == [], "a completion artifact was posted over an empty gate set"


# ---- site 2: the assessment the board and the pass both read --------------------------------


def test_a_notes_only_mission_is_never_likely_done(store):
    """`likely_done` drives two surfaces at once.

    `MissionSupervisorBoard` renders "Every gate is met — this mission looks finished" straight
    off this field, and `run_pass` takes its proposal branch on it. One field, so one fix — but
    it has to be asserted here, because a Python-side fix with the banner still reading a stale
    truth would be a half-fix.
    """
    mid = _running()
    _notes(mid, "Review all open bugs", "Bring each to a viable state")

    a = sup.assess(mid)
    assert a["likely_done"] is False, "nothing is checkable, so nothing is settled"
    assert a["unmet_gates"] == 0, "premise: the vacuity is real — there are no unmet gates"
    assert a["gates"] == 0, "the board must be able to tell *no gates* from *0 unmet*"


def test_the_two_counts_are_reported_separately_so_the_board_can_tell_them_apart(store):
    """`0 unmet` is the same number whether every gate passed or none exists.

    Without the second count the board cannot distinguish them, which is the whole reason the
    banner was able to congratulate an unmeasured mission.
    """
    mid = _running()
    _gate(mid, "merged", met=True)

    a = sup.assess(mid)
    assert (a["gates"], a["unmet_gates"]) == (1, 0)
    assert a["likely_done"] is True, "a real, met gate still completes"


# ---- site 3: the operator's own close answer ------------------------------------------------


def test_close_mission_refuses_on_a_notes_only_mission(store):
    """Answered, recorded, and honestly without effect.

    The refusal shape is the one the path already uses for its other two — the operator said what
    they think and the store says what happened — so this asserts the answer is not silently
    swallowed either.
    """
    mid = _running()
    _notes(mid, "Review all open bugs")
    q = missions.open_question(
        mid,
        "note_1",
        "Is this finished?",
        [
            {"label": "Yes, close it", "action": "close_mission"},
            {"label": "No, keep going", "action": "note_answer"},
        ],
    )

    out = missions.answer_question(mid, q["seq"], option_index=0)

    assert out["action"] == "close_mission", "the answer itself is still recorded"
    assert out["applied_ok"] is False, "a mission with nothing to check was closed"
    assert "not proposed" in (out.get("applied") or ""), out.get("applied")
    assert missions.get_mission(mid)["state"] == "running"


# ---- site 4: the option list the card is built from -----------------------------------------


def test_close_mission_is_not_OFFERED_when_nothing_gates(store):
    """One option fewer beats an option that finishes an unfinished mission.

    `offered_actions` dropped `close_mission` only when a gate was unmet, on the reasoning that
    the supervisor completes a mission before it can ever ask — true of every mission WITH gates,
    and false of one with none. The model picks an index into this list, so an action absent from
    it cannot be selected at all.
    """
    mid = _running()
    _notes(mid, "Review all open bugs")

    offered = [name for name, _ in mq.offered_actions_for(missions.objectives(mid))]
    assert "close_mission" not in offered, offered
    # …and the rest of the closed set is untouched: this narrows one option, it does not gut the
    # card.
    assert "note_answer" in offered and "waive_objective" in offered


def test_close_mission_IS_offered_once_a_real_gate_is_met(store):
    mid = _running()
    _gate(mid, "merged", met=True)

    offered = [name for name, _ in mq.offered_actions_for(missions.objectives(mid))]
    assert "close_mission" in offered, offered


# ---- the two directions that must not change ------------------------------------------------


def test_it_does_not_over_block_a_mission_that_has_a_real_gate(store):
    """The same mission, once something IS checkable, completes exactly as before.

    Ordered as one story on one mission: notes-only refuses, then a met gate is added and the
    identical call succeeds. A pair of separate fixtures could both pass while the rule keyed on
    something other than the gate set.
    """
    mid = _running()
    _notes(mid, "Review all open bugs")
    assert _propose(mid) is False, "premise: refused while nothing gates"

    _gate(mid, "merged", met=True)

    assert _propose(mid) is True
    assert missions.get_mission(mid)["state"] == "review"
    assert len(_completions(mid)) == 1


def test_a_gate_set_that_is_entirely_WAIVED_still_settles(store):
    """A waiver is the operator's decision that an objective was not required.

    That is not the same fact as having nothing to require, and the new rule must not collide
    with the existing exemption: the gate set is non-empty, every member is settled, so the
    mission completes.
    """
    mid = _running()
    _gate(mid, "merged", waived=True)

    a = sup.assess(mid)
    assert (a["gates"], a["unmet_gates"]) == (1, 0)
    assert a["likely_done"] is True

    assert _propose(mid) is True
    assert missions.get_mission(mid)["state"] == "review"


def test_a_mission_with_no_objectives_at_all_is_still_refused(store):
    """The pre-existing guard, kept.

    `not rows` and "no gates" are different facts, and both must refuse.
    """
    mid = _running()

    assert _propose(mid) is False
    assert sup.assess(mid)["likely_done"] is False


# ---- the predicate itself --------------------------------------------------------------------


def test_the_store_predicate_answers_for_every_site(store):
    """One function, so a fifth caller cannot reintroduce the same hole.

    Exercised directly on row shapes rather than through a mission, because the callers pass both
    `sqlite3.Row` and plain dicts and the tally has to read either.
    """
    notes = [{"gate": 0, "state": "pending"}, {"gate": 0, "state": "pending"}]
    assert missions.gate_tally(notes) == (0, 0)
    assert missions.gates_settled(notes) is False

    unmet = [{"gate": 1, "state": "pending"}]
    assert missions.gate_tally(unmet) == (1, 1)
    assert missions.gates_settled(unmet) is False

    waived = [{"gate": 1, "state": "waived"}]
    assert missions.gate_tally(waived) == (1, 0)
    assert missions.gates_settled(waived) is True

    # `unmet_gate_count` keeps its signature and its meaning — existing callers read it.
    assert missions.unmet_gate_count(unmet) == 1
    assert missions.unmet_gate_count(notes) == 0


# ---- Phase 2: the heading is derived, and cannot contradict its own list ----------------------


def _propose_rendered(mid: str):
    """Through the REAL renderer — `_propose` stubs it, which is right for the state-machine
    tests above and useless for these: the wording is the thing under test."""
    return missions.propose_completion(mid, from_state="running", render=sup._render_completion)


def _completion_head(mid: str) -> str:
    return _completions(mid)[0]["text"].splitlines()[0]


def test_the_heading_states_what_was_actually_checked(store):
    """It used to be a constant, emitted before the function had looked at anything.

    A heading that cannot disagree with the list beneath it is a decoration, and the operator
    reads it as the verdict — which is how "This looks finished" came to sit above two objectives
    both printed `pending`.
    """
    mid = _running()
    _gate(mid, "merged", met=True)

    assert _propose_rendered(mid) is True
    head = _completion_head(mid)
    assert head.startswith("Every gate is met (1 gate)."), head
    assert "This looks finished" in head


def test_an_outstanding_non_gating_goal_is_NAMED_not_folded_into_finished(store):
    """A goal that does not gate still did not happen, and the proposal says so.

    This is the shape the reported mission would have had with one real gate: the operator should
    be able to see that something is still open even though it did not block completion.
    """
    mid = _running()
    _gate(mid, "merged", met=True)
    _notes(mid, "Re-review every issue for file conflicts")

    assert _propose_rendered(mid) is True
    head = _completion_head(mid)
    assert "1 goal is still open" in head, head
    assert "they do not gate completion" in head, head


def test_the_artifact_carries_the_numbers_its_heading_was_built_from(store):
    """So a surface rendering the card never re-derives them and never disagrees with the text."""
    mid = _running()
    _gate(mid, "merged", met=True)
    _notes(mid, "Tidy up")

    assert _propose_rendered(mid) is True
    meta = _completions(mid)[0]["meta"]
    assert (meta["gates"], meta["unmet_gates"], meta["outstanding_goals"]) == (1, 0, 1)


def test_the_heading_can_no_longer_produce_the_sentence_that_caused_this_issue(store):
    """The renderer is unreachable with an empty gate set — and says the honest thing anyway.

    `propose_completion` refuses before rendering, so this drives the function directly: a later
    caller that forgets the guard must not be able to emit "Every gate is met" over nothing.
    """
    text = sup._completion_text(
        [{"key": "note_1", "title": "Review the bugs", "gate": False, "state": "pending"}],
        gates=0,
        unmet=0,
        outstanding=1,
    )
    head = text.splitlines()[0]
    assert "Every gate is met" not in head, head
    assert "cannot be confirmed finished" in head, head


def test_an_unmet_gate_heading_does_not_claim_completion(store):
    """Also unreachable today, and also must not lie if it is reached."""
    text = sup._completion_text(
        [{"key": "merged", "title": "It is merged", "gate": True, "state": "pending"}],
        gates=2,
        unmet=1,
        outstanding=0,
    )
    head = text.splitlines()[0]
    assert head == "1 of 2 gates is still unmet — this is not finished.", head
