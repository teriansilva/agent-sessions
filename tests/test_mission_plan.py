"""The dispatch proposal (#893, Phase 4 of #840).

One property runs through this file and it is the reason the phase is gated the way it is: **no
model-authored value becomes a launch argument.** The model picks an INDEX into a server-built
list; the cwd comes from the project entity, server-side. That is a stronger claim than "we
validate what it sends", and it is the only one worth making when the thing on the end of the
launch is an agent running UNATTENDED, in a real working directory, with nobody watching it.
It is deliberately NOT permission-bypassed: `mission_dispatch.run` passes `bypass=False`,
because unattended bypass is a broad privilege nobody has approved (#904 review 3, finding 6).

The other half is that `/plan` launches nothing. It produces a proposal, stores it, and stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import pytest

from agent_sessions import mission_plan, missions

#: A real engine-qualified key. `engines.parse_key` validates the native shape, so a made-up
#: `claude:zzz` makes the teardown path raise instead of decide — and a test asserting on the
#: teardown then passes for the wrong reason.
UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    missions.reset_schema_cache_for_test()
    yield tmp_path
    missions.reset_schema_cache_for_test()


# NEUTRAL FIXTURE NAMES. The public-snapshot gate scans the whole tree for internal project and
# host names, and a test fixture is as public as any other file — quoting a real one here failed
# `pr-validate` on the first push.
PROJECTS = [
    {"id": "prj_a", "name": "the-app", "cwd": "/repo/the-app"},
    {"id": "prj_b", "name": "the-docs", "cwd": "/repo/the-docs"},
]
ENGINES = [{"id": "claude", "label": "claude"}, {"id": "codex", "label": "codex"}]


# ---- the index discipline ---------------------------------------------------------------


def test_the_model_NEVER_SEES_a_path_so_it_cannot_echo_one_back():
    """The cheapest half of the guarantee: a list with no paths in it.

    `render_options` is the entire text the model is given about projects. If a cwd appeared
    there, a model could put it in `brief` — and a brief is pasted verbatim into a real agent.
    """
    text = mission_plan.render_options(PROJECTS, ENGINES)
    assert "the-app" in text and "the-docs" in text
    for p in PROJECTS:
        assert p["cwd"] not in text
        assert p["id"] not in text


def test_a_MODEL_AUTHORED_PATH_never_becomes_the_plans_cwd():
    """The load-bearing one. A reply carrying a literal path — the shape a prompt injection would
    take — resolves to nothing: there is no field for it, and `project_index` is an index."""
    proposal, dropped = mission_plan.proposal_from_reply(
        {
            "project_index": "/etc",
            "cwd": "/etc",
            "project": "/etc",
            "engine_index": 0,
            "engine_reason": "it is a python repo",
            "brief": "do the thing",
        },
        PROJECTS,
        ENGINES,
    )
    assert proposal["project"] is None
    assert "project" in dropped
    # …and nothing anywhere in the proposal carries the path it tried to supply.
    assert "/etc" not in repr(proposal)


@pytest.mark.parametrize("bad", [True, False, "0", 1.0, None, -1, 2, 99])
def test_an_index_that_is_not_a_real_index_selects_NOTHING(bad):
    """`isinstance(True, int)` is True in Python, so an unguarded check lets `true` select
    element 1 — an agent nobody chose. Out of range DROPS rather than clamps: a clamped index is
    the server inventing a choice and presenting it as the model's."""
    proposal, _ = mission_plan.proposal_from_reply(
        {"project_index": bad, "engine_index": bad, "brief": "x"}, PROJECTS, ENGINES
    )
    assert proposal["project"] is None
    assert proposal["engine"] is None


def test_a_VALID_index_resolves_to_the_entity_and_its_server_side_cwd():
    proposal, dropped = mission_plan.proposal_from_reply(
        {
            "project_index": 1,
            "engine_index": 1,
            "engine_reason": "it is a docs repo",
            "brief": "update the guide",
        },
        PROJECTS,
        ENGINES,
    )
    assert proposal["project"]["id"] == "prj_b"
    assert proposal["project"]["cwd"] == "/repo/the-docs"
    assert proposal["engine"] == "codex"
    assert dropped == []


def test_a_SUGGESTION_WITHOUT_A_REASON_says_so_rather_than_arriving_silently():
    """ "A suggestion is a proposal, never a silent choice." An agent chosen for the operator with
    no stated why is a decision disguised as a default."""
    proposal, _ = mission_plan.proposal_from_reply(
        {"engine_index": 0, "brief": "x"}, PROJECTS, ENGINES
    )
    assert proposal["engine"] == "claude"
    assert proposal["engine_reason"] == "no reason was given"


def test_NULL_is_a_real_answer_and_is_not_reported_as_a_refusal():
    """The instruction may simply not say which project. That is what the picker is for, and it
    is different from a reply the server had to throw away."""
    proposal, dropped = mission_plan.proposal_from_reply(
        {"project_index": None, "engine_index": None, "brief": "x"}, PROJECTS, ENGINES
    )
    assert proposal["project"] is None and proposal["engine"] is None
    assert dropped == []


# ---- what is offered --------------------------------------------------------------------


def test_SHELL_is_never_offered_as_a_dispatch_target(monkeypatch):
    """Seeding a brief into a bare `bash -l` RUNS it.

    The gate is applied when the LIST is built, so an unseedable engine is not offered rather
    than offered and then refused — an operator cannot pick what was never on the menu, and the
    model cannot index to it.
    """
    from agent_sessions import engines

    class P:
        def __init__(self, eid, seed):
            self.engine_id = eid
            self.supports_seed_start = seed

        def is_present(self):
            return True

    monkeypatch.setattr(
        engines,
        "all_providers",
        lambda: [P("claude", True), P("shell", True), P("gemini", False)],
    )
    ids = {e["id"] for e in mission_plan.engine_options()}
    assert ids == {"claude"}
    assert "shell" not in ids, "a brief pasted into a login shell executes"


def test_an_UNINSTALLED_engine_is_not_offered(monkeypatch):
    from agent_sessions import engines

    class P:
        engine_id = "codex"
        supports_seed_start = True

        def is_present(self):
            return False

    monkeypatch.setattr(engines, "all_providers", lambda: [P()])
    assert mission_plan.engine_options() == []


def test_a_project_with_NO_FOLDER_is_not_offered(monkeypatch, tmp_path):
    """Offering a choice that can never be dispatched is worse than offering fewer choices."""
    from agent_sessions import projects

    class E:
        def __init__(self, pid, folders, default=""):
            self.id = pid
            self.name = pid
            self.folders = folders
            self.default_folder = default
            self.archived = False

    monkeypatch.setattr(
        projects,
        "load",
        lambda: {
            "a": E("a", (), ""),
            "b": E("b", ("/repo",), "/repo"),
        },
    )
    assert [p["id"] for p in mission_plan.project_options()] == ["b"]


# ---- the stored proposal ----------------------------------------------------------------


def _mission(state="draft"):
    m = missions.create_mission("ship it", cwd="/repo")
    if state != "draft":
        missions.set_state(m["id"], "draft", "planned")
    return m["id"]


def test_a_RE_PLAN_supersedes_rather_than_stacking(store):
    """Two live plans for one mission is a state the operator cannot act on coherently, and the
    newer one is the one on their screen."""
    mid = _mission()
    first = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="one")
    second = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="codex", brief="two")
    assert first["plan_id"] != second["plan_id"]
    current = missions.get_plan(mid)
    assert current["plan_id"] == second["plan_id"] and current["brief"] == "two"


def test_DISPATCHING_A_SUPERSEDED_PLAN_is_refused(store):
    """An index is not an identity and neither is "the mission's current plan".

    A model call sits between `/plan` and the button, and the operator dispatches the plan they
    SAW. Red against a dispatch that reads whatever is stored now.
    """
    mid = _mission("planned")
    stale = missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="one")
    missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="two")
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, stale["plan_id"])
    assert e.value.status == 409
    assert "replaced" in str(e.value)
    # …and nothing moved: the mission is still planned, and the plan is still there to dispatch.
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_plan(mid) is not None


def test_ONE_WINNER_when_two_taps_race_the_same_plan(store):
    """Two approvals both reading a matching plan and both transitioning is two unattended
    agents against one mission."""
    mid = _mission("planned")
    plan = missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="go")
    first = missions.claim_plan(mid, plan["plan_id"])
    assert first["engine"] == "claude"
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"])
    assert e.value.status == 409
    assert missions.get_mission(mid)["state"] == "dispatching"


def test_a_mission_that_LEFT_PLANNED_cannot_be_dispatched_from_its_old_plan(store):
    """The state predicate, on its own — the second-tap test above passes without it, because the
    claim consumes the plan and the retry fails on the missing row instead.

    This is the case the predicate is actually for: the plan is stored while the mission is
    `planned`, the mission moves on by ANOTHER path (an abandon, a dispatch already in flight),
    and then the button is pressed. `WHERE state='planned'` is what makes the transition itself
    the arbiter rather than a check made before it.

    Red against an UPDATE with no state predicate: the mission is dragged back to `dispatching`
    from wherever it had got to.
    """
    mid = _mission("planned")
    plan = missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="go")
    # It leaves `planned` by a route that is not this one.
    missions.set_state(mid, "planned", "dispatching")

    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"])
    assert e.value.status == 409
    assert "not planned" in str(e.value)
    # …and the plan is still there, because nothing consumed it.
    assert missions.get_plan(mid) is not None


def test_a_CLAIMED_plan_is_consumed(store):
    """A proposal that survives its own dispatch is a button the operator can press again."""
    mid = _mission("planned")
    plan = missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="go")
    missions.claim_plan(mid, plan["plan_id"])
    assert missions.get_plan(mid) is None


def test_a_plan_with_NO_PROJECT_or_NO_AGENT_cannot_be_dispatched(store):
    """Both are legitimate states to STORE — that is what the picker is for — and neither is a
    state to launch from. The refusal names which one is missing."""
    mid = _mission("planned")
    p1 = missions.put_plan(mid, project_id=None, cwd=None, engine="claude", brief="go")
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, p1["plan_id"])
    assert e.value.status == 422 and "project" in str(e.value)

    p2 = missions.put_plan(mid, project_id="p", cwd="/repo", engine=None, brief="go")
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, p2["plan_id"])
    assert e.value.status == 422 and "agent" in str(e.value)


def test_a_RUNNING_mission_cannot_be_PLANNED(store):
    """A plan is a proposal to START work. Re-planning a running mission is a proposal to launch
    it twice, and the store refuses rather than leaving a button whose meaning depends on when
    it is pressed."""
    mid = _mission("planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    with pytest.raises(missions.MissionError) as e:
        missions.put_plan(mid, project_id="p", cwd="/repo", engine="claude", brief="go")
    assert e.value.status == 409


def test_the_plan_is_on_the_TIMELINE_as_its_own_kind(store):
    """`plan` is a preserved event kind. A proposal the operator can no longer find is a decision
    with no record of what was proposed."""
    mid = _mission()
    plan = missions.put_plan(
        mid, project_id="prj_a", cwd="/repo", engine="claude", engine_reason="why", brief="go"
    )
    events = missions.get_mission(mid)["events"]
    row = next(e for e in events if e["kind"] == "plan")
    assert row["text"] == "go"
    assert row["meta"]["plan_id"] == plan["plan_id"]
    assert row["meta"]["engine"] == "claude" and row["meta"]["engine_reason"] == "why"


# ---- the whole pass ---------------------------------------------------------------------


def test_PROPOSE_stores_the_proposal_and_launches_NOTHING(store, monkeypatch):
    """The separation is the feature. `/plan` spends a model call and writes a row; the agent
    starts when the operator presses a different button."""
    from agent_sessions import review

    monkeypatch.setattr(mission_plan, "project_options", lambda: PROJECTS)
    monkeypatch.setattr(mission_plan, "engine_options", lambda: ENGINES)

    async def reply(messages, **kw):
        return {
            "project_index": 0,
            "engine_index": 0,
            "engine_reason": "it is a python repo",
            "brief": "open a PR that does the thing",
        }

    monkeypatch.setattr(review, "complete_json", reply)
    mid = _mission()
    plan = asyncio.run(mission_plan.propose(mid))

    assert plan["cwd"] == "/repo/the-app", "the cwd came from the entity, not the model"
    assert plan["engine"] == "claude"
    assert plan["brief"] == "open a PR that does the thing"
    # NOTHING STARTED — and that is not the same as nothing HAPPENED. A plan is what `planned`
    # means, so the mission is now dispatchable; the agent starts when a different button is
    # pressed (#904 review 1). Leaving it in `draft` made the plan un-dispatchable, because
    # `claim_plan` only claims from `planned`.
    assert missions.get_mission(mid)["state"] == "planned"
    assert plan["mission_state"] == "planned"
    # …and the proposal is durable, with the options the card needs to offer an override.
    assert missions.get_plan(mid)["plan_id"] == plan["plan_id"]
    assert plan["project_options"] == PROJECTS and plan["engine_options"] == ENGINES


def test_PROPOSE_resolves_indices_against_the_lists_THE_MODEL_SAW(store, monkeypatch):
    """ "An index is not an identity", in the one place where the identity it names is a
    filesystem path. A list re-read after the model call resolves an index into a set the model
    never saw — so a project added mid-call would silently retarget the plan.

    Red against a `propose` that rebuilds the options after the await.
    """
    from agent_sessions import review

    live = [dict(p) for p in PROJECTS]
    monkeypatch.setattr(mission_plan, "project_options", lambda: [dict(p) for p in live])
    monkeypatch.setattr(mission_plan, "engine_options", lambda: ENGINES)

    async def reply(messages, **kw):
        # A project is added at the FRONT while the model is thinking — every later index shifts.
        live.insert(0, {"id": "prj_new", "name": "brand new", "cwd": "/repo/new"})
        return {"project_index": 0, "engine_index": 0, "brief": "go"}

    monkeypatch.setattr(review, "complete_json", reply)
    plan = asyncio.run(mission_plan.propose(_mission()))
    assert plan["project_id"] == "prj_a", "the index resolved against a list the model never saw"
    assert plan["cwd"] == "/repo/the-app"


def test_PROPOSE_falls_back_to_the_INSTRUCTION_when_no_brief_comes_back(store, monkeypatch):
    """A plan with no brief cannot be dispatched, and the operator's own words are a better
    default than an empty box."""
    from agent_sessions import review

    monkeypatch.setattr(mission_plan, "project_options", lambda: PROJECTS)
    monkeypatch.setattr(mission_plan, "engine_options", lambda: ENGINES)
    monkeypatch.setattr(review, "complete_json", lambda *a, **k: _async({"brief": ""}))
    plan = asyncio.run(mission_plan.propose(_mission()))
    assert plan["brief"] == "ship it"
    assert "brief" in plan["dropped"]


async def _async(value):
    return value


def test_an_UNCONFIGURED_endpoint_is_a_409_rather_than_an_empty_plan(store, monkeypatch):
    """Unlike a question, a plan the operator cannot see is not a state worth writing: there is
    nothing for the console to render and nothing for DISPATCH to consume."""
    from agent_sessions import review

    async def boom(*a, **k):
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(review, "complete_json", boom)
    mid = _mission()
    with pytest.raises(mission_plan.PlanError) as e:
        asyncio.run(mission_plan.propose(mid))
    assert e.value.status == 409
    # THE SAME mission — a second `_mission()` here would create a different one and the
    # assertion would hold against any implementation at all.
    assert missions.get_plan(mid) is None


def test_a_mission_with_NO_PROJECT_acquires_one_when_its_plan_is_dispatched(store):
    """The picker's whole reason to exist is a draft that does not yet say where to work.

    The schema's launch CHECK makes a cwd-less `dispatching` unrepresentable, so without this the
    mission the picker was built for is the one mission that can never be dispatched — it fails
    with an `IntegrityError` from inside the transition, which is not a state anyone can act on.

    Dispatching is the honest moment for it: the plan is where the project was CHOSEN, and the
    mission takes the value the SERVER resolved from the entity, never a client's.

    Red against a transition that moves the state and leaves the mission's own cwd null.
    """
    m = missions.create_mission("ship it")  # no project — a legitimate draft
    assert m.get("cwd") is None
    missions.set_state(m["id"], "draft", "planned")
    plan = missions.put_plan(
        m["id"], project_id="prj_a", cwd="/repo/the-app", engine="claude", brief="go"
    )

    missions.claim_plan(m["id"], plan["plan_id"])
    row = missions.get_mission(m["id"])
    assert row["state"] == "dispatching"
    assert row["cwd"] == "/repo/the-app"
    assert row["project_id"] == "prj_a"


# ---- dispatch: the highest-privilege call in the app ------------------------------------


class _Out:
    """A `headless_dispatch.Dispatch` stand-in. The three facts, kept apart."""

    def __init__(self, *, started=True, briefed=True, reason="", key="claude:aaa", launched=True):
        self.started = started
        self.briefed = briefed
        self.reason = reason
        self.key = key
        # `launched` is the FIRST of the three facts and the one the cleanup path reads: a master
        # exists, whatever the other two say. A stand-in without it let the cleanup branch pass
        # by raising instead of by deciding.
        self.launched = launched

    @property
    def ok(self):
        return self.started and self.briefed

    @property
    def state(self):
        if self.started and self.briefed:
            return "briefed"
        if self.briefed:
            return "unidentified"
        if self.started:
            return "started"
        return "failed"


def _planned(store, **over):
    mid = _mission("planned")
    plan = missions.put_plan(
        mid,
        project_id="prj_a",
        cwd="/repo",
        engine="claude",
        brief="go",
        **over,
    )
    return mid, plan


def test_ALIVE_IS_NOT_STARTED_a_launch_with_no_store_record_lands_FAILED(store, monkeypatch):
    """#840's condition 5, at the mission boundary.

    A dispatched agent sitting on a trust dialog is alive by every process-level measure and has
    received nothing — measured on 2026-08-25: a live master, a live process, a bound socket, and
    no transcript at all. Reporting that as `running` is the worst outcome available here, because
    the supervisor then follows through on work that is not happening.

    Red against settling on `briefed` alone, or on "the launcher did not raise".
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def fake(**kw):
        return _Out(started=False, briefed=True, reason="the engine's store has no such session")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] != "running"
    assert "store has no such session" in out["reason"]
    row = missions.get_mission(mid)
    assert row["state"] == "failed", "a session that never started was reported as running"
    # …and the mission holds nothing, because nothing it can supervise exists.
    assert missions.active_session_keys(mid) == []


def test_a_launch_that_could_not_be_ATTEMPTED_leaves_the_mission_RETRYABLE(store, monkeypatch):
    """`dispatching` is transient. A mission left in it is the crash outcome #840 names, and a
    refusal — an ineligible engine, a brief the sanitiser would not take — is not a crash.

    …and it is not a FAILURE either (#904 review 2, finding 7). Nothing was spawned, so nothing
    about the mission went wrong; what failed is the dispatch. `failed` consumed the proposal —
    the claim deletes it — into a state only `running` leads out of, so the operator could
    neither re-plan nor retry a refusal whose whole point was that nothing had changed.

    Red against a refusal that settles `failed`.
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    assert missions.get_plan(mid) is None, "the claim consumes the plan"

    async def refuse(**kw):
        raise mission_dispatch.headless_dispatch.DispatchError("shell cannot be dispatched to")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", refuse)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "planned"
    assert "shell" in out["reason"]
    assert missions.get_mission(mid)["state"] == "planned"
    # THE PROPOSAL IS BACK, under its own id — so the card on screen still matches and the next
    # tap of DISPATCH works rather than 409-ing on an id nothing holds.
    back = missions.get_plan(mid)
    assert back is not None and back["plan_id"] == plan["plan_id"]
    assert back["brief"] == plan["brief"] and back["engine"] == plan["engine"]
    # …and the dispatch record is gone: that dispatch is over.
    assert missions.get_dispatch(mid) is None


def test_an_UNEXPECTED_error_still_settles_the_mission(store, monkeypatch):
    """The same rule for the case nobody predicted: whatever happens, the mission leaves
    `dispatching`, because a stuck transient state is worse than a named failure."""
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def boom(**kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", boom)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert out["state"] == "failed"
    assert missions.get_mission(mid)["state"] == "failed"


def test_a_STARTED_AND_BRIEFED_launch_is_adopted_before_the_mission_reports_running(
    store, monkeypatch
):
    """A `running` mission with no session is a board promising follow-through it cannot perform.

    The order matters: adopt, then report. Red against reporting `running` and adopting after.
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    seen: list[str] = []

    async def fake(**kw):
        seen.append(f"{kw['engine']}|{kw['cwd']}|{kw['brief']}|bypass={kw['bypass']}")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "running" and out["session_key"] == "claude:aaa"
    row = missions.get_mission(mid)
    assert row["state"] == "running"
    assert missions.active_session_keys(mid) == ["claude:aaa"]
    # The launch used the SERVER's resolved cwd and the stored brief...
    assert seen == ["claude|/repo|go|bypass=False"]


def test_UNATTENDED_BYPASS_IS_NOT_INHERITED_from_the_operators_approval(store, monkeypatch):
    """An operator approving a PLAN approved the work, not the removal of every tool prompt from
    an agent nobody is watching. #898 defaults it off so the decision belongs to the layer that
    knows whether somebody authorised it, and the answer here is no."""
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    got: list[bool] = []

    async def fake(**kw):
        got.append(kw["bypass"])
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert got == [False]


def test_a_session_that_could_not_be_ADOPTED_is_not_reported_as_running(store, monkeypatch):
    """It started and it was briefed, and this mission does not hold it — another adopted it in
    the window, or this one was archived. Reporting `running` over a session the mission does not
    own is the membership lie the whole ownership fence exists to prevent."""
    from agent_sessions import mission_dispatch

    other = missions.create_mission("someone else", cwd="/repo")
    missions.set_state(other["id"], "draft", "planned")
    missions.set_state(other["id"], "planned", "dispatching")
    missions.set_state(other["id"], "dispatching", "running")
    missions.adopt(other["id"], "claude:aaa")

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def fake(**kw):
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    assert missions.get_mission(mid)["state"] == "failed"
    assert missions.active_session_keys(mid) == []


# ==============================================================================================
# #904 review 2 — the dispatch is DURABLE, and a crash is recoverable
# ==============================================================================================


def test_STORING_a_plan_is_what_makes_a_mission_dispatchable(store):
    """#904 review 1, and the reason it survived every test in the file.

    `claim_plan` claims from `planned`. `put_plan` left the mission in `draft`. So the one path an
    operator actually takes — create, plan, press DISPATCH — answered `409: mission is draft, not
    planned`, while every dispatch test here moved the state by hand first and passed.

    Red against a `put_plan` that stores the row and leaves the state alone.
    """
    mid = _mission()  # a fresh DRAFT, exactly as `POST /api/missions` leaves it
    assert missions.get_mission(mid)["state"] == "draft"
    plan = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="go")
    assert missions.get_mission(mid)["state"] == "planned"
    # …and the very next call the operator makes now works, with nothing in between.
    claimed = missions.claim_plan(mid, plan["plan_id"])
    assert claimed["plan_id"] == plan["plan_id"]
    assert missions.get_mission(mid)["state"] == "dispatching"


def test_a_RE_PLAN_of_a_planned_mission_leaves_it_planned(store):
    """The transition is `draft -> planned` and nothing else. A second plan must not try to move
    a state that is already right — `_ALLOWED` has no `planned -> planned`."""
    mid = _mission()
    missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="one")
    second = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="two")
    assert missions.get_mission(mid)["state"] == "planned"
    assert second["mission_state"] == "planned"


def test_two_TABS_editing_one_plan_do_not_silently_lose_an_edit(store):
    """#904 review 6. An edit reads the plan, changes one field and writes the WHOLE row back.

    Two tabs holding plan A: one changes the brief, the other the engine. Both are told the save
    worked, and the later write restores its own stale copy of the field the first one changed —
    an acknowledged edit, gone, with nothing on either screen to say so.

    Red against a `put_plan` that ignores `expect_plan_id`.
    """
    mid = _mission()
    a = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="one")

    # Tab 1 edits the brief and wins.
    missions.put_plan(
        mid,
        project_id="prj_a",
        cwd="/repo",
        engine="claude",
        brief="one, but better",
        expect_plan_id=a["plan_id"],
    )
    # Tab 2 is still holding A, and its write carries A's stale brief.
    with pytest.raises(missions.MissionError) as e:
        missions.put_plan(
            mid,
            project_id="prj_a",
            cwd="/repo",
            engine="codex",
            brief="one",
            expect_plan_id=a["plan_id"],
        )
    assert "changed while you were editing" in str(e.value)
    assert missions.get_plan(mid)["brief"] == "one, but better"


def test_the_CLAIM_takes_the_cwd_the_caller_resolved_NOW(store):
    """#904 review 5. A plan can sit on screen while its project is repointed, and the stored cwd
    is what that project USED to mean. Launching an unattended agent into the old
    directory is `stale policy across the await` with a filesystem path on the end of it."""
    mid = _mission()
    plan = missions.put_plan(mid, project_id="prj_a", cwd="/repo/old", engine="claude", brief="go")
    claimed = missions.claim_plan(mid, plan["plan_id"], cwd="/repo/new", project_id="prj_a")
    assert claimed["cwd"] == "/repo/new"
    # …and it is what the MISSION now carries, which is what the launcher reads.
    assert missions.get_mission(mid)["cwd"] == "/repo/new"


def test_the_CLAIM_writes_a_durable_record_of_the_launch_it_is_about_to_make(store):
    """#904 review 2. `dispatching` is a promise a running process makes, and the plan row is
    consumed by the claim — so without this there is nothing left on disk that says a launch was
    ever attempted, and the mission is `dispatching` for ever."""
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"])
    rec = missions.get_dispatch(mid)
    assert rec is not None
    assert rec["plan_id"] == plan["plan_id"] and rec["engine"] == "claude"
    # NULL until the key is minted — which is the difference recovery reads as "nothing was
    # spawned" versus "something may have been".
    assert rec["session_key"] is None
    assert [r["mission_id"] for r in missions.unsettled_dispatches()] == [mid]


def test_the_key_is_recorded_BEFORE_the_launch_not_after_it(store, monkeypatch):
    """The record may name a session that never came to be; it may never miss one that did.

    Red against a launcher that stamps the key after the spawn: the assertion is that `on_key`
    has already been honoured by the time the dispatch function is even entered.
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    seen: list[str | None] = []

    async def fake(**kw):
        kw["on_key"]("claude:11111111-2222-3333-4444-555555555555")
        # WHAT THE STORE HOLDS AT THIS MOMENT — i.e. before anything else in the launch runs.
        seen.append(missions.get_dispatch(mid)["session_key"])
        return _Out(key="claude:11111111-2222-3333-4444-555555555555")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert seen == ["claude:11111111-2222-3333-4444-555555555555"]


def test_a_dispatch_that_was_SETTLED_refuses_to_record_a_key(store, monkeypatch):
    """`note_dispatch_session` returning False is the launcher's signal that this dispatch has an
    owner no more — starting an agent for it would produce exactly the orphan the record exists
    to prevent."""
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    # Somebody else settles it — the operator abandoned the mission from another tab.
    missions.settle_dispatch(mid, to="abandoned", detail="operator changed their mind")
    assert (
        missions.note_dispatch_session(mid, "claude:11111111-2222-3333-4444-555555555555") is False
    )

    calls: list[str] = []

    async def fake(**kw):
        with pytest.raises(missions.MissionError):
            kw["on_key"]("claude:11111111-2222-3333-4444-555555555555")
        calls.append("refused")
        return _Out(key="claude:11111111-2222-3333-4444-555555555555")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
    assert calls == ["refused"]


def test_an_ABANDON_during_the_launch_does_not_produce_a_running_report(store, monkeypatch):
    """#904 review 3, and the worst outcome this module can produce.

    The launch takes tens of seconds; abandoning takes one tap. Adopting first and transitioning
    after made the two halves separable: the terminal transition released the roster, the late
    adopt re-attached a live unattended agent to a mission that was already closed, and
    the failed `dispatching -> running` CAS was SUPPRESSED while the API answered `running`.

    Red against a settlement whose adopt and transition are two statements.
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    stopped: list[str] = []

    async def fake(**kw):
        kw["on_key"]("claude:11111111-2222-3333-4444-555555555555")
        # THE OPERATOR ABANDONS IT, while the launch is in flight. Exactly the interleaving.
        missions.set_state(mid, "dispatching", "abandoned", outcome="abandoned")
        return _Out(key="claude:11111111-2222-3333-4444-555555555555")

    async def stop(engine, native, **kw):
        stopped.append(f"{engine}:{native}")
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    from agent_sessions import runtime_cleanup

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stop)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # 1. The API does not say `running` for a mission that is abandoned.
    assert out["state"] == "abandoned"
    assert missions.get_mission(mid)["state"] == "abandoned"
    # 2. NOTHING was adopted into the closed mission.
    assert missions.active_session_keys(mid) == []
    # 3. …and the agent that had already started is not left running with nobody owning it.
    assert stopped == ["claude:11111111-2222-3333-4444-555555555555"]
    # 4. …and the record is gone, because the teardown PROVED the boundary empty. A `leaked`
    #    one would keep it — that is its own test.
    assert missions.get_dispatch(mid) is None


# ==============================================================================================
# #904 review 2 — the startup recovery pass, at each crash boundary
# ==============================================================================================


#: A pid that cannot be allocated (above `/proc/sys/kernel/pid_max` on any Linux), so its lease
#: is provably dead for the whole life of the test.
DEAD_OWNER = "4194305:1"


def _crashed(store, *, key=None, owner=DEAD_OWNER):
    """A mission left `dispatching` by a process that died, with its durable record intact.

    The OWNER is what makes it a crash rather than a live dispatch (#904 review 2, finding 3):
    recovery acts only on a lease it can prove is dead, and a record written by this very test
    process is indistinguishable from a sibling instance that is launching right now.
    """
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=owner)
    if key:
        missions.note_dispatch_session(mid, key)
    return mid


def test_recovery_fails_a_dispatch_that_died_BEFORE_the_key_was_minted(store):
    """PRE-SPAWN. The record exists because the claim committed; the key does not, because nothing
    was ever minted. Nothing ran, and the mission must not stay `dispatching` for ever."""
    from agent_sessions import mission_dispatch_recover

    mid = _crashed(store)
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    row = missions.get_mission(mid)
    assert row["state"] == "failed"
    assert "stopped before the agent was started" in _last_state_event(mid)
    assert missions.get_dispatch(mid) is None
    assert missions.active_session_keys(mid) == []


def test_recovery_ADOPTS_a_session_the_engine_store_knows_and_still_does_not_say_running(
    store, monkeypatch
):
    """POST-SPAWN, POST-STORE-RECORD. A session exists, so it is attached — the operator has to be
    able to reach it. It is NOT reported as running: the third fact, that the brief was delivered
    and acked, is not recoverable from disk, and `running` over a session that may be sitting on a
    consent screen is exactly the false report #898 refused to make."""
    from agent_sessions import headless_dispatch, mission_dispatch_recover

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    monkeypatch.setattr(headless_dispatch, "store_record_state", lambda *a, **k: "found")

    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert missions.get_mission(mid)["state"] == "failed"
    # ADOPTED — the agent is reachable rather than orphaned.
    assert missions.active_session_keys(mid) == [key]
    assert "no record that it received its brief" in _last_state_event(mid)


def test_a_REFUSED_recovery_adoption_still_reconciles_the_unowned_child(store, monkeypatch):
    """A child nobody adopted must not be left running with its recovery record gone (review 8).

    The mission moves out of `dispatching` between the store lookup and the settlement, so the
    settlement refuses the adoption and drops the dispatch. The ACCOUNTING for that child was
    already right — its reservation stays charged and reapable — but a charge is not a cleanup
    obligation: the reaper only ever observes death, it never stops an orphan. So the child kept
    running and the next pass found nothing to repair.

    Driven through the production `recover_once` with the concurrent move, because the previous
    version of this test called `settle_dispatch` directly with a hard-coded flag and therefore
    exercised neither the changed default nor the missing follow-through.
    """
    from agent_sessions import headless_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)

    # The session IS present — and looking moves the mission out of `dispatching`, which is the
    # race: settlement will then take its early return and refuse the adoption.
    def present(*_a, **_kw):
        if missions.get_mission(mid)["state"] == "dispatching":
            missions.set_state(mid, "dispatching", "failed")
        return "found"

    monkeypatch.setattr(headless_dispatch, "store_record_state", present)

    stopped: list[str] = []

    async def stop(engine, native, **kw):
        stopped.append(f"{engine}:{native}")
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stop)

    asyncio.run(mission_dispatch_recover.recover_once())

    assert missions.active_session_keys(mid) == [], "the child was never adopted"
    assert stopped == [key], (
        "an unadopted, possibly-live child was abandoned: no owner, no teardown, and its "
        "recovery record deleted"
    )


def _refusing_recovery(monkeypatch, mid, answers):
    """A recovery whose adoption is refused mid-pass, with `_stop` answering from `answers`.

    The concurrent move is what makes the settlement refuse: the store lookup itself takes the
    mission out of `dispatching`, which is the real race and the only way to reach the exit under
    test. Returns the list teardown calls are recorded into.
    """
    from agent_sessions import headless_dispatch, runtime_cleanup

    def present(*_a, **_kw):
        if missions.get_mission(mid)["state"] == "dispatching":
            missions.set_state(mid, "dispatching", "failed")
        return "found"

    monkeypatch.setattr(headless_dispatch, "store_record_state", present)
    calls: list[str] = []

    async def stop(engine, native, **kw):
        calls.append(f"{engine}:{native}")
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stop)
    return calls


@pytest.mark.parametrize(
    ("first", "why"),
    [
        ("leaked", "a teardown that proved nothing"),
        (RuntimeError("the fence blew up"), "a teardown that raised"),
    ],
    ids=["leaked", "exception"],
)
def test_a_FAILED_teardown_KEEPS_the_recovery_record_for_the_next_pass(
    store, monkeypatch, first, why
):
    """An obligation dropped on a failed attempt is one nobody ever discharges (review 9).

    The settlement's mission-moved exit deleted the dispatch, so the teardown that follows it ran
    with its own retry record already gone. `leaked` then logged "leaving the obligation for the
    next pass" and left nothing behind for the next pass to find — and a reapable resource charge
    is not a cleanup queue, because the reaper only ever observes death; it never stops an orphan.

    So the record now survives a refused adoption, and only a PROVED answer takes it away. Both
    failure shapes are covered: `leaked`, and a teardown that raises — the second reaches a
    different `except` and was the one with no coverage at all.

    Driven through the production `recover_once` twice, which is the assertion that matters: the
    first pass must retain, and the second must find the record and discharge it. A test that
    only asserts "teardown was called once" passes against the version that forgets.
    """
    from agent_sessions import mission_dispatch_recover

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    calls = _refusing_recovery(monkeypatch, mid, [first, "stopped"])

    # PASS ONE: the adoption is refused, the teardown fails, and the record must SURVIVE.
    asyncio.run(mission_dispatch_recover.recover_once())
    assert calls == [key], "the unowned child was not torn down at all"
    assert missions.get_dispatch(mid) is not None, (
        f"{why} deleted the only durable trace of a child nobody adopted and nobody stopped — "
        "the next pass has nothing left to retry"
    )
    assert missions.active_session_keys(mid) == [], "the child was never adopted"

    # PASS TWO: the retained record is found again and the teardown now succeeds.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert calls == [key, key], "the retained obligation was never retried"
    assert missions.get_dispatch(mid) is None, "a proved stop did not discharge the record"


def _crashed_spawn(store, key):
    """A crashed SPAWN attempt: a real `claim_spawn` reservation, keyed, owned by a dead process.

    The sibling tests use `_crashed`, which is a PRIMARY dispatch and therefore has no ledger row
    at all — so they can assert what happens to the dispatch record but nothing about the resource
    charge, which is the half these findings keep being about (review 10, non-blocking note).
    """
    m = missions.create_mission("do the thing", cwd="/repo")
    mid = m["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    parent = "claude:11111111-1111-4111-8111-111111111111"
    missions.adopt(mid, parent)
    claim = missions.claim_spawn(
        mid,
        parent_key=parent,
        engine="claude",
        cwd="/repo",
        brief="review it",
        owner=DEAD_OWNER,
    )
    missions.note_dispatch_session(mid, key, expect_plan=claim["plan_id"])
    return mid, claim


def test_a_SPARED_child_KEEPS_ITS_CHARGE_while_another_mission_runs_it(store, monkeypatch):
    """The canonical spawn-shaped version of the test below (review 10, non-blocking note).

    `spared` discharges the dispatch RECORD — somebody answers for the agent — but must not return
    the SLOT, because nothing stopped: the process is still on this host under a new owner. A
    mission that got its capacity back here could spawn again beside a child it no longer knows
    about, which is the bound being evadable rather than enforced.
    """
    from agent_sessions import mission_dispatch_recover

    key = "claude:77777777-7777-4777-8777-777777777777"
    mid, _claim = _crashed_spawn(store, key)
    assert missions.open_spawn_count(mid) == 1
    _refusing_recovery(monkeypatch, mid, ["spared"])

    asyncio.run(mission_dispatch_recover.recover_once())

    assert missions.get_dispatch(mid) is None, "a verified owner did not discharge the record"
    assert missions.open_spawn_count(mid) == 1, (
        "a spared child's slot came back while its process is still running under another "
        "mission — the cap is evadable by spawn -> lose it -> spawn"
    )


def test_a_SPARED_child_discharges_the_record_but_keeps_its_slot_charged(store, monkeypatch):
    """`spared` is the second answer that ends the obligation — and it is not `stopped`.

    Another mission adopted the child, so somebody answers for it and the record goes; but the
    process is still on this host, so the slot stays charged and becomes reapable rather than
    being handed straight back to a mission that could then spawn beside a child it no longer
    knows about.
    """
    from agent_sessions import mission_dispatch_recover

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    calls = _refusing_recovery(monkeypatch, mid, ["spared"])

    asyncio.run(mission_dispatch_recover.recover_once())
    assert calls == [key]
    assert missions.get_dispatch(mid) is None, "a verified owner did not discharge the record"
    # …and the timeline says what actually happened. `spared` discharging the record does NOT
    # mean the agent stopped, and writing that it did was its own defect (#904 review 13).
    assert missions.active_session_keys(mid) == [], "the child was adopted by this mission"


def test_recovery_STOPS_a_launch_the_engine_store_never_saw(store, monkeypatch):
    """POST-KEY, PRE-START. An id and no agent behind it: whatever runtime footprint exists is
    torn down rather than left as a process nobody owns."""
    from agent_sessions import headless_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    monkeypatch.setattr(headless_dispatch, "store_record_state", lambda *a, **k: "absent")
    stopped: list[str] = []

    async def stop(engine, native, **kw):
        stopped.append(f"{engine}:{native}")
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stop)
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert missions.get_mission(mid)["state"] == "failed"
    assert stopped == [key]
    assert missions.active_session_keys(mid) == []


def test_recovery_does_not_touch_a_dispatch_that_already_settled(store):
    """POST-ADOPT. The record is the intent, not the outcome — a mission that reached `running`
    while the process was alive has no record left, and one that reached it afterwards is not
    `dispatching` any more."""
    from agent_sessions import mission_dispatch_recover

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    missions.settle_dispatch(mid, to="running", detail="dispatched", session_key=key)
    assert missions.get_dispatch(mid) is None
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert missions.get_mission(mid)["state"] == "running"
    assert missions.active_session_keys(mid) == [key]


def _last_state_event(mission_id: str) -> str:
    """The newest `state` event's text. `get_mission` returns events NEWEST FIRST, so `[0]` is
    the last thing that happened — `[-1]` is the mission being created."""
    row = missions.get_mission(mission_id)
    events = [e for e in (row.get("events") or []) if e.get("kind") == "state"]
    return str(events[0].get("text") or "") if events else ""


# ==============================================================================================
# #904 review 4 — the launch is ORDERED against a policy withdrawal, not merely checked before it
# ==============================================================================================


def test_a_DISABLE_that_commits_after_the_check_still_stops_the_spawn(store, monkeypatch):
    """The route reads the master switch, then awaits a database claim and a process spawn. A
    read is not a fence: the switch can be flipped in that window and the unattended agent starts
    under authority the operator has withdrawn.

    `policy_transaction` holds `session_input`'s two locks across the persist and bumps the epoch
    on the way out — the same ordering `_write_all` gets. This asserts the LAUNCH takes it too.

    Red against a dispatch that passes no `authorize` (or one that compares nothing): the
    withdrawal lands mid-flight and the spawn happens anyway.
    """
    from agent_sessions import mission_dispatch, prefs, session_input

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    epoch = session_input.current_policy_epoch()
    spawned: list[str] = []

    async def fake(**kw):
        # THE INTERLEAVING, at the only moment that matters: after the route read the policy and
        # before the process exists. `set_orchestrator` goes through `policy_transaction`, so this
        # is the real withdrawal path rather than a stand-in for it.
        prefs.set_orchestrator({"enabled": False})
        why = kw["authorize"](session_input.current_policy_epoch())
        if why:
            return _Out(started=False, briefed=False, reason=why, launched=False)
        spawned.append("agent")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object(), policy_epoch=epoch))

    assert spawned == [], "an agent started under policy the operator had withdrawn"
    assert "policy changed" in out["reason"]
    # AND IT IS RETRYABLE. Nothing was spawned, so this is a dispatch that did not happen — the
    # plan goes back and the operator can switch orchestration on and press again (#904 review 2,
    # finding 7). `failed` consumed the proposal into a state only `running` leads out of.
    assert missions.get_mission(mid)["state"] == "planned"
    back = missions.get_plan(mid)
    assert back is not None and back["plan_id"] == plan["plan_id"]


def test_the_launch_fence_hands_out_the_POLICY_it_is_HOLDING(store):
    """The comparand cannot be fetched by the caller inside the fence — `policy_fingerprint`
    takes the same non-reentrant lock — so the fence yields it. This pins that it is the live
    value and not a stale copy captured on the way in.

    And that it is a digest of the POLICY ITSELF (#904 review 3, finding 3): a counter beside it
    was a second record that could fail, and its failure mode was fail-OPEN — the withdrawal
    persisted, the counter write did not, and a launch comparing an unchanged integer proceeded.
    """
    from agent_sessions import prefs, session_input

    prefs.set_orchestrator({"enabled": True})
    before = session_input.policy_fingerprint()
    assert before is not None
    with session_input.launch_fence() as inside:
        assert inside == before
    prefs.set_orchestrator({"enabled": False})
    with session_input.launch_fence() as after:
        assert after is not None and after != before


def test_a_HELD_launch_fence_EXPIRES_rather_than_blocking_for_ever(store):
    """#904 review 8, finding 2, the half that makes the caller's guarantee affordable.

    A spawn runs the fence and the `Popen` together on a worker thread, and a thread cannot be
    cancelled — so whatever the worker does, the caller has to wait for it before it may release
    the single-writer lock or close the directory handle. That is only safe if the worker has a
    deadline, and `_lock` is a plain `threading.Lock` with none: a launch queued behind a stuck
    holder would sit here for ever, and the frame that must join it with it.

    Refusing to be ordered is an answer this module already knows how to report, so the expiry is
    `AuthorityFenceBusy` — the same refusal a busy file fence raises — and the caller's existing
    "could not be ordered against a policy change; try again" covers it.

    Asked with a timeout rather than by hanging: a test that reproduces a hang by hanging is a
    test that hangs.
    """
    import threading

    from agent_sessions import session_input

    held = threading.Event()
    release = threading.Event()

    def _hold():
        with session_input.launch_fence():
            held.set()
            release.wait(timeout=10)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(timeout=5), "the fence was never taken"
    try:
        with pytest.raises(session_input.AuthorityFenceBusy):
            with session_input.launch_fence(timeout=0.05):
                pass
    finally:
        release.set()
        t.join(timeout=5)

    # …and the lock is not left held by the failed acquisition, which would wedge every later
    # launch on a fence nobody owns.
    with session_input.launch_fence(timeout=5) as epoch:
        assert epoch is not None


def test_an_UNREADABLE_policy_refuses_the_launch_rather_than_authorising_it(store, monkeypatch):
    """`None` is "could not read", and an unreadable authority record is the absence of a check
    rather than permission (#904 review 3, finding 3)."""
    from agent_sessions import mission_dispatch, prefs, session_input

    def boom():
        raise OSError("prefs could not be read")

    monkeypatch.setattr(prefs, "get_orchestrator", boom)
    assert session_input.policy_fingerprint() is None

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    spawned: list[str] = []

    async def fake(**kw):
        with session_input.launch_fence() as inside:
            why = kw["authorize"](inside)
        if why:
            return _Out(started=False, briefed=False, reason=why, launched=False)
        spawned.append("agent")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(
        mission_dispatch.run(mid, claimed, registry=object(), policy_epoch="anything")
    )
    assert spawned == []
    assert "policy changed" in out["reason"]


def test_recovery_LEAVES_a_dispatch_whose_owner_is_still_running(store):
    """#904 review 2, finding 3. `dispatching` says a launch is somewhere between claimed and
    settled; it does not say whether anybody is still doing it.

    Startup runs this pass as an UN-AWAITED task, so a request that is actively launching — in
    this instance, or in a sibling over the same store — was snapshotted as crashed: the mission
    was moved to `failed` and its session torn down while the launcher was still working.

    Red against a pass that treats every `dispatching` row as orphaned.
    """
    from agent_sessions import mission_dispatch_recover

    # THIS process's own lease, which is exactly what a live sibling's looks like from here.
    mid = _crashed(store, owner=missions.process_owner())
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert missions.get_mission(mid)["state"] == "dispatching"
    assert missions.get_dispatch(mid) is not None


def test_recovery_LEAVES_a_dispatch_whose_owner_it_cannot_IDENTIFY(store):
    """`unknown` is not `gone`. An unparseable lease is not evidence that the launcher died, and
    acting on a live dispatch is the harmful direction."""
    from agent_sessions import mission_dispatch_recover

    mid = _crashed(store, owner="not-a-lease")
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert missions.get_mission(mid)["state"] == "dispatching"


def test_an_UNREADABLE_provider_store_is_not_proof_the_session_is_absent(store, monkeypatch):
    """#904 review 2, finding 4, against the PRODUCTION helper rather than a patched one.

    Recovery's `None` branch existed and could never be reached: `_has_store_record` swallowed
    every provider-scan exception and returned `False`, so a transiently corrupt or locked store
    read as "that session never existed" — and a possibly live agent was torn down and its
    mission settled failed. The only test that covered it patched the helper to raise, which
    asserts the caller's handling of an answer the helper could not produce.

    Red against a boolean-only reader: this raises from `prov.scan()`, which is where a real
    store failure surfaces.
    """
    from agent_sessions import engines, headless_dispatch, mission_dispatch_recover

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)

    prov, _ = engines.parse_key(key)

    def boom():
        raise OSError("the provider store is locked")

    monkeypatch.setattr(type(prov), "scan", lambda self: boom())
    assert headless_dispatch.store_record_state(prov, "x") == "unreadable"

    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert missions.get_mission(mid)["state"] == "dispatching"
    assert [r["mission_id"] for r in missions.unsettled_dispatches()] == [mid]


def test_a_SIBLING_INSTANCE_withdrawing_the_policy_stops_this_launch(store, monkeypatch):
    """#904 review 2, finding 2, and the reason a process-local counter was never a fence.

    `policy_transaction` bumped only THIS interpreter's integer, so a sibling instance over the
    same lock dir could commit the disable, release the file fence, and leave our captured value
    untouched — the launch fence then acquired a lock that told it nothing and authorized the
    spawn. The counter has to live where both processes can see it.

    A real `fork`, so nothing in memory is shared: an in-process stand-in would bump the very
    integer whose blindness is the defect, and pass against it.

    Red against the process-local comparand.
    """
    from agent_sessions import prefs, session_input

    # ON, so the child's withdrawal is a real CHANGE rather than a re-write of the default.
    prefs.set_orchestrator({"enabled": True})
    captured = session_input.policy_fingerprint()
    lock_dir = os.environ.get("AGENT_SESSIONS_LOCK_DIR", "")
    home = os.environ.get("AGENT_SESSIONS_HOME", "")

    pid = os.fork()
    if pid == 0:  # child — a DIFFERENT process withdrawing the policy
        code = 1
        try:
            os.environ["AGENT_SESSIONS_LOCK_DIR"] = lock_dir
            os.environ["AGENT_SESSIONS_HOME"] = home
            from agent_sessions import prefs as p2

            p2.set_orchestrator({"enabled": False})
            code = 0
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, "the sibling failed to withdraw the policy"

    # AND THE LAUNCH REFUSES. Asserted through `run`'s own `authorize`, driven with the value a
    # real `launch_fence` yields — not by comparing two integers, which is what made the
    # process-local version look fine.
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    spawned: list[str] = []

    async def fake(**kw):
        with session_input.launch_fence() as inside:
            why = kw["authorize"](inside)
        if why:
            return _Out(started=False, briefed=False, reason=why, launched=False)
        spawned.append("agent")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object(), policy_epoch=captured))
    assert spawned == [], "a sibling's withdrawal was invisible to the launch fence"
    assert "policy changed" in out["reason"]


def test_a_project_that_moves_AFTER_the_route_resolved_it_still_stops_the_spawn(store, monkeypatch):
    """#904 review 2, finding 6, second half. The route's re-resolution closes the window between
    the card and the request; this closes the one between the request and `create_subprocess_exec`.

    A project repointed in that window would still be launched — the approved path and the
    resolved path agreed when they were compared, and then the world moved. `authorize` runs
    inside the launch fence, immediately before the spawn, so it is the last thing that can ask.

    Red against a `run` that verifies nothing at the fence.
    """
    from agent_sessions import mission_dispatch, session_input

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    spawned: list[str] = []

    async def fake(**kw):
        with session_input.launch_fence() as inside:
            why = kw["authorize"](inside)
        if why:
            return _Out(started=False, briefed=False, reason=why, launched=False)
        spawned.append("agent")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(
        mission_dispatch.run(
            mid,
            claimed,
            registry=object(),
            # THE PROJECT HAS MOVED by the time the fence asks — which is the only moment this
            # can be observed, because the route's own comparison already passed.
            verify_cwd=lambda: "/repo/moved-since-the-route-looked",
        )
    )
    assert spawned == [], "an agent started in a directory nobody approved"
    assert "that project moved" in out["reason"]
    # …and it is retryable: nothing was spawned, so the plan comes back.
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_plan(mid) is not None


def test_an_UNRESOLVABLE_project_at_the_fence_refuses_rather_than_launching(store, monkeypatch):
    """The same check, when the answer is an exception. A project store that will not read is not
    permission to launch where it used to point."""
    from agent_sessions import mission_dispatch, session_input

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    spawned: list[str] = []

    def boom():
        raise OSError("the projects file could not be read")

    async def fake(**kw):
        with session_input.launch_fence() as inside:
            why = kw["authorize"](inside)
        if why:
            return _Out(started=False, briefed=False, reason=why, launched=False)
        spawned.append("agent")
        return _Out()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object(), verify_cwd=boom))
    assert spawned == []
    assert "that project moved" in out["reason"]


def test_a_LEAKED_teardown_keeps_the_durable_record(store, monkeypatch):
    """#904 review 3, finding 1. `cleanup_runtime` answers `leaked` when something in the
    session's process group survived SIGKILL — and both paths discarded that.

    Settling then deleted `mission_dispatches`, so the only durable trace of an unattended agent
    that is very possibly still running was gone: no operator-visible obligation, and nothing for
    a later pass to find.

    Red against a teardown whose outcome is ignored.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def fake(**kw):
        kw["on_key"](f"claude:{UUID}")
        # The mission is abandoned under us, so the settlement refuses and the session is torn
        # down — which is the path that used to discard the outcome.
        missions.set_state(mid, "dispatching", "abandoned", outcome="abandoned")
        return _Out(key=f"claude:{UUID}")

    async def leaks(engine, native, **kw):
        return "leaked"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaks)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # THE RECORD SURVIVES, naming the session nobody has proved is gone.
    rec = missions.get_dispatch(mid)
    assert rec is not None and rec["session_key"] == f"claude:{UUID}"


def test_a_CANCELLED_request_does_not_strand_the_mission(store, monkeypatch):
    """#904 review 3, finding 2. `CancelledError` is a `BaseException`, so it walked past the
    handler and out of `run` — leaving the mission `dispatching` with a lease whose process is
    still very much alive, which is exactly the row recovery is right to refuse. Nothing moved it
    for the life of the process.

    Red against a `run` that catches only `Exception`.
    """
    from agent_sessions import mission_dispatch, mission_dispatch_recover

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def cancelled(**kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # 1. The mission is not left `dispatching`.
    assert missions.get_mission(mid)["state"] == "failed"
    # 2. …and the record is KEPT: a cancellation can land after the spawn, so "nothing started"
    #    is not something this path can claim, and the record is the trace of what might be out
    #    there. Recovery leaves it alone while this process lives, which is now harmless because
    #    the mission has already been settled.
    assert missions.get_dispatch(mid) is not None
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0


def test_a_RETAINED_cleanup_obligation_is_RETRIED_until_the_boundary_is_EMPTY(store, monkeypatch):
    """#904 review 4, finding 1 — the whole point of keeping the record.

    Every path that retains a `mission_dispatches` row moves the mission to `failed`/`abandoned`
    FIRST, and `unsettled_dispatches()` selected only rows whose mission was still `dispatching`.
    So the row that was kept precisely because a possibly-live unattended agent could not be
    proved stopped was the one row no later pass ever looked at. It sat there for ever.

    Red against a selection filtered on the mission's lifecycle: `_stop` is never called and the
    record is never cleared.
    """
    from agent_sessions import mission_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def fake(**kw):
        kw["on_key"](key)
        missions.set_state(mid, "dispatching", "abandoned", outcome="abandoned")
        return _Out(key=key)

    outcomes = ["leaked", "leaked", "stopped"]
    stopped: list[str] = []

    async def teardown(engine, native, **kw):
        stopped.append(native)
        return outcomes.pop(0) if outcomes else "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # The request path kept the obligation, and the mission is already settled — which is exactly
    # the shape the old selection could not see.
    assert missions.get_mission(mid)["state"] == "abandoned"
    assert missions.get_dispatch(mid) is not None
    assert len(stopped) == 1

    # PASS 1 — the row is FOUND (this is the regression), the teardown is retried, and it leaks
    # again, so the obligation stays.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert len(stopped) == 2, "the retained obligation was never retried"
    assert missions.get_dispatch(mid) is not None, "an obligation dropped on a failed attempt"
    # …and no owner-lease check gates this branch: the owner is THIS process and very much alive,
    # which is right for a `dispatching` row and meaningless for a settled one.
    assert missions.owner_is_live(missions.get_dispatch(mid)["owner"]) is True

    # PASS 2 — the teardown finally proves the boundary empty, and only now is the row dropped.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert len(stopped) == 3
    assert missions.get_dispatch(mid) is None
    # The mission itself was never re-settled: it is somebody else's decision, already made.
    assert missions.get_mission(mid)["state"] == "abandoned"


def test_recovery_NEVER_STOPS_a_session_the_mission_still_OWNS(store, monkeypatch):
    """The window a lifecycle-only test walks straight into, and the reason `held` exists.

    A successful dispatch settles `running` with `keep_record=True` — the record has to survive a
    REFUSED settlement — and `clear_dispatch()` discharges it on the next line. Between those two
    the mission is `running` with its record still on disk, and a crash there leaves it for good.

    `running` is not `dispatching`, so selecting obligations by lifecycle alone called that a
    retained cleanup obligation and stopped the live agent the mission had just started. An orphan
    is a session NOBODY owns; that is a membership question.

    Red against a branch keyed on `state != "dispatching"`.
    """
    from agent_sessions import mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, key)
    # THE SUCCESSFUL SETTLEMENT, with its record deliberately kept …
    verdict = missions.settle_dispatch(
        mid, to="running", detail="dispatched", session_key=key, keep_record=True
    )
    assert verdict.get("settled")
    # … and the crash lands before `clear_dispatch()`.
    assert missions.get_dispatch(mid) is not None
    assert missions.active_session_keys(mid) == [key]

    stopped: list[str] = []

    async def teardown(engine, native, **kw):
        stopped.append(native)
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    moved = asyncio.run(mission_dispatch_recover.recover_once())

    # THE AGENT IS UNTOUCHED, and the mission still holds it.
    assert stopped == [], "recovery stopped the live agent of a dispatch that had SUCCEEDED"
    assert missions.get_mission(mid)["state"] == "running"
    assert missions.active_session_keys(mid) == [key]
    # …and the stale bookkeeping is discharged, so the row does not sit there for ever.
    assert moved == 1
    assert missions.get_dispatch(mid) is None


def test_recovery_NEVER_STOPS_a_session_ANOTHER_MISSION_owns(store, monkeypatch):
    """#904 review 5, finding 1. An orphan is a session NOBODY owns — not one THIS mission has
    stopped owning.

    A failed dispatch can retain its record without holding the session, and once a second mission
    legitimately adopts that key, "my mission does not hold it" is true while "it is an orphan" is
    false. Tearing it down then trades an orphan for somebody else's live agent.

    Red against a `held` computed for the dispatch row's own mission.
    """
    from agent_sessions import mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    # MISSION A: a dead-owner dispatch that kept its record and does NOT hold the session.
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, key)
    missions.settle_dispatch(mid, to="failed", detail="stubbed", keep_record=True)
    assert missions.get_dispatch(mid) is not None
    assert missions.active_session_keys(mid) == []

    # MISSION B adopts it, legitimately.
    other = _mission("planned")
    missions.adopt(other, key)
    assert missions.holder_of(key) == other

    stopped: list[str] = []

    async def teardown(engine, native, **kw):
        stopped.append(native)
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    moved = asyncio.run(mission_dispatch_recover.recover_once())

    assert stopped == [], "recovery tore down another mission's live agent"
    assert missions.holder_of(key) == other
    # …and A's obligation is discharged: the session has somebody to answer for it.
    assert moved == 1
    assert missions.get_dispatch(mid) is None


def test_an_ADOPTION_that_lands_MID_TEARDOWN_spares_the_session(store, monkeypatch):
    """#904 review 5, finding 1, the other half: the ownership read is a SNAPSHOT.

    An adoption can commit after `unsettled_dispatches()` answers and before the signal lands, so
    "nobody owns it" being true a moment ago is not a licence to kill. `cleanup_runtime`'s
    `spare_if` guard is re-asked immediately before each signal, which is where the window
    actually closes — and a spared session is a DISCHARGED obligation, because it now has a
    mission to answer for it.

    Red against a teardown that passes no guard: the adopting mission's agent is killed.
    """
    from agent_sessions import mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, key)
    missions.settle_dispatch(mid, to="failed", detail="stubbed", keep_record=True)
    other = _mission("planned")

    killed: list[str] = []

    async def teardown(engine, native, *, spare_if=None, **kw):
        # THE ADOPTION LANDS HERE — after the snapshot said "nobody owns it", before the signal.
        missions.adopt(other, key)
        if spare_if is not None and not spare_if():
            return "spared"
        killed.append(native)
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    moved = asyncio.run(mission_dispatch_recover.recover_once())

    assert killed == [], "the session was killed after another mission had adopted it"
    assert missions.holder_of(key) == other
    assert moved == 1 and missions.get_dispatch(mid) is None


def test_an_ADOPTION_cannot_INTERLEAVE_the_teardown_at_all(store, monkeypatch):
    """#904 review 6, finding 1. `spare_if` narrowed the window; it did not close it.

    `terminate_master` evaluates the predicate, then reads the cgroup and the process tree from
    `/proc`, and only then signals. An adoption committing inside THAT — after the last guard,
    before the kill — still kills the new owner's agent, and the previous regression could not see
    it because it adopted before the predicate ran.

    So the test adopts where the defect actually lives: from inside the teardown, after every
    guard the old design had. The fence makes that impossible rather than unlikely — the adoption
    blocks until the teardown is finished, which is why this asserts on ORDER rather than on
    timing.

    Red against a teardown that is not mutually exclusive with adoption.
    """
    import threading

    from agent_sessions import mission_dispatch_recover, runtime_cleanup
    from agent_sessions.mission_dispatch import fenced_adopt as _fenced_adopt

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, key)
    missions.settle_dispatch(mid, to="failed", detail="stubbed", keep_record=True)
    other = _mission("planned")

    order: list[str] = []
    adopted = threading.Event()

    def _adopt_from_another_process():
        # A REAL FENCED ADOPTER (#904 review 7, finding 1). The earlier version of this test
        # wrapped the adopter in `sessions_transaction()` ITSELF, so it proved only that a caller
        # which already takes the lock serialises — the very thing the door did not do. It goes
        # through `_fenced_adopt` instead, so removing the fence there makes this test fail rather
        # than making it lie.
        #
        # `POST /adopt` no longer calls this helper — since #896 it takes the same two locks
        # inline, around a reservation-checked insert. What is under test here is the TEARDOWN's
        # side of the race, so what matters is that the adopter is genuinely fenced, not which of
        # the two fenced doors it came through.
        order.append("adopt-begin")
        _fenced_adopt(other, key)
        order.append("adopt")
        adopted.set()

    async def teardown(engine, native, *, spare_if=None, **kw):
        # PAST EVERY GUARD the old design had: the predicate has been asked and answered.
        assert spare_if is None or spare_if()
        t = threading.Thread(target=_adopt_from_another_process, daemon=True)
        t.start()
        # Give the adopter a real chance to win the race if nothing is stopping it.
        adopted.wait(timeout=0.5)
        order.append("signal")
        t.join(timeout=5)
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    asyncio.run(mission_dispatch_recover.recover_once())
    # Wait for the adopter to COMMIT, bounded — and fail saying so. 5 s ran out on a starved runner
    # (#1107): `adopt` was then missing from `order` (a ValueError, not a finding), and the
    # still-running daemon thread went on to lock the NEXT test's store ("database is locked").
    assert adopted.wait(timeout=60), f"the adopter never committed after the teardown: {order}"

    # THE ORDER IS THE PROPERTY. The adoption may BEGIN inside the window — nothing stops a
    # request arriving — but it cannot COMMIT until the teardown has released the fence, so the
    # signal is always first. Without it the adopter commits inside the window and the agent it
    # has just taken responsibility for is killed.
    assert order.index("signal") < order.index("adopt"), order
    assert missions.holder_of(key) == other


def test_a_SUCCESSFUL_DISPATCH_cannot_interleave_a_teardown_of_the_session_it_adopts(
    store, monkeypatch
):
    """#904 review 8, finding 1. The fence covered every path EXCEPT the one that adopts.

    `fenced_adopt` is the route's door and it was fenced; `_settle` was fenced; and the successful
    dispatch — the one line in the codebase whose whole job is to record that this mission now owns
    the agent it has just started — went straight to `missions.settle_dispatch`. So the guarantee
    read as complete while the single most consequential adoption in the feature was outside it.

    The previous regression could not see that, because it drove `fenced_adopt` directly: it
    proved the fence works, not that the production path takes it. This one drives the real
    `mission_dispatch.run()` — stubbing only the launcher — so deleting the fence from that call
    site makes it FAIL rather than making it lie.

    Red against a successful path that calls `missions.settle_dispatch` directly: the adoption
    commits inside the teardown window, and recovery signals the agent this mission has just
    taken responsibility for.
    """
    import threading

    from agent_sessions import mission_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"

    # A RETAINED OBLIGATION on that session: a teardown that could not prove the boundary empty.
    # This is the row recovery acts on, and it takes the session's fence to do it.
    stale, stale_plan = _planned(store)
    missions.claim_plan(stale, stale_plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(stale, key)
    missions.settle_dispatch(stale, to="failed", detail="stubbed", keep_record=True)

    # …and a REAL DISPATCH of the same session, mid-flight. Its owner is this process, so recovery
    # leaves its row alone — the two rows meet at the session, not at the mission.
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    order: list[str] = []
    adopted = threading.Event()

    async def launcher(**kw):
        # #898's three facts, all true: the master exists, the store knows the session, the brief
        # was acked. This is the success the mission is about to record ownership of.
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    def _dispatch_from_another_request():
        order.append("dispatch-begin")
        asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
        order.append("adopt")
        adopted.set()

    async def teardown(engine, native, *, spare_if=None, **kw):
        # PAST EVERY GUARD the narrower designs had: the predicate has been asked and answered.
        assert spare_if is None or spare_if()
        t = threading.Thread(target=_dispatch_from_another_request, daemon=True)
        t.start()
        # Give the dispatch a real chance to win the race if nothing is stopping it.
        adopted.wait(timeout=0.5)
        order.append("signal")
        t.join(timeout=5)
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    asyncio.run(mission_dispatch_recover.recover_once())
    # Wait for the adopter to COMMIT, bounded — and fail saying so. 5 s ran out on a starved runner
    # (#1107): `adopt` was then missing from `order` (a ValueError, not a finding), and the
    # still-running daemon thread went on to lock the NEXT test's store ("database is locked").
    assert adopted.wait(timeout=60), f"the adopter never committed after the teardown: {order}"

    # THE ORDER IS THE PROPERTY, exactly as for the route's adoption: the launch may happen inside
    # the window — nothing stops an agent starting — but the settlement that ADOPTS cannot commit
    # until the teardown has released the fence.
    assert order.index("signal") < order.index("adopt"), order
    assert missions.holder_of(key) == mid
    assert missions.get_mission(mid)["state"] == "running"


def test_RECOVERYS_OWN_ADOPTION_goes_through_the_fence(store, monkeypatch):
    """#904 review 8, finding 1, the second unfenced adoption: recovery's own.

    A crashed dispatch whose session the engine's store still knows is ADOPTED — that is the whole
    point of the middle case — and that adoption went straight to `missions.settle_dispatch` too.
    It is the pass most likely to be racing a teardown of the very same key: its own, one row
    later, or a sibling instance's.

    Red against a recovery settlement that does not take the session's fence.
    """
    import threading

    from agent_sessions import mission_dispatch_recover, session_input
    from agent_sessions.engines import physical_key

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"], owner=DEAD_OWNER)
    missions.note_dispatch_session(mid, key)

    order: list[str] = []
    holder = threading.Event()
    release = threading.Event()

    def _hold_the_fence():
        with session_input.sessions_transaction([physical_key(key)]):
            order.append("held")
            holder.set()
            release.wait(timeout=5)
            order.append("released")

    async def present(k, cwd):
        return True

    monkeypatch.setattr(mission_dispatch_recover, "_has_session", present)

    t = threading.Thread(target=_hold_the_fence, daemon=True)
    t.start()
    assert holder.wait(timeout=5), "the fence was never taken"

    done = threading.Event()

    def _recover():
        asyncio.run(mission_dispatch_recover.recover_once())
        order.append("adopt")
        done.set()

    r = threading.Thread(target=_recover, daemon=True)
    r.start()
    # If the adoption is unfenced it commits while the fence is held by somebody else — which is
    # precisely the teardown window it must not commit inside.
    assert not done.wait(timeout=0.5), "recovery adopted while the session's fence was held"
    release.set()
    assert done.wait(timeout=10), "recovery never finished"
    t.join(timeout=5)
    r.join(timeout=5)

    assert order.index("released") < order.index("adopt"), order
    assert missions.holder_of(key) == mid


def test_the_APPROVED_checklist_cannot_change_between_the_CLAIM_and_the_SPAWN(store):
    """#904 review 5, finding 2. `claim_plan` compares the digest inside its own transaction, and
    that transaction ends before the spawn — so an edit landing in the window between them started
    an unattended agent against a checklist other than the one the operator approved, with the
    comparison having passed.

    A digest cannot fence a window it has already left. The STATE can: `dispatching` is short,
    bounded by the launch, and settled by recovery if the process dies.

    Red against a `patch_objectives` that is legal while a mission is `dispatching`.
    """
    mid, plan = _planned(store)
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    approved = missions.objectives_digest(missions.get_mission(mid)["objectives"])
    missions.claim_plan(mid, plan["plan_id"], expect_objectives=approved, require_objectives=True)
    assert missions.get_mission(mid)["state"] == "dispatching"

    # THE EDIT THAT USED TO LAND. Every op, because the refusal is about the window rather than
    # about which field is being changed.
    for op in (
        {"op": "retitle", "key": "pr", "title": "Something else entirely"},
        {"op": "add", "key": "green", "title": "Checks are green", "gate": True},
        {"op": "drop", "key": "pr"},
        {"op": "waive", "key": "pr"},
    ):
        with pytest.raises(missions.MissionError) as e:
            missions.patch_objectives(mid, [op])
        assert e.value.status == 409, op
        assert "being dispatched" in str(e.value), op

    # …and the checklist the launch will run against is still the one that was approved.
    assert missions.objectives_digest(missions.get_mission(mid)["objectives"]) == approved

    # …and once the dispatch settles, edits are ordinary again.
    missions.settle_dispatch(mid, to="running", detail="dispatched", session_key=f"claude:{UUID}")
    missions.patch_objectives(
        mid, [{"op": "retitle", "key": "pr", "title": "A PR is open, with notes"}]
    )


def test_a_RAISING_teardown_is_an_obligation_too_and_survives_a_CANCELLATION(store, monkeypatch):
    """#904 review 4, finding 1, on the cancellation path and with a teardown that RAISES.

    A cleanup that throws is not a cleanup that succeeded, and `CancelledError` reaches `run`
    while a spawn may already have happened — so the record has to survive both, and a later pass
    has to pick it up even though the mission has already been settled `failed`.
    """
    from agent_sessions import mission_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def cancelled(**kw):
        kw["on_key"](key)
        raise asyncio.CancelledError()

    calls: list[str] = []

    async def raises(engine, native, **kw):
        calls.append(native)
        if len(calls) == 1:
            raise OSError("the process table could not be read")
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", cancelled)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", raises)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert missions.get_mission(mid)["state"] == "failed"
    rec = missions.get_dispatch(mid)
    assert rec is not None and rec["session_key"] == key

    # PASS 1 — the row is selected even though the mission is already `failed` (the regression),
    # the teardown RAISES, and a cleanup that threw is not a cleanup that succeeded: the
    # obligation stays.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 0
    assert len(calls) == 1, "the retained obligation was never retried"
    assert missions.get_dispatch(mid) is not None

    # PASS 2 — it answers this time, and only now is the row dropped.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert len(calls) == 2
    assert missions.get_dispatch(mid) is None
    assert missions.get_mission(mid)["state"] == "failed"


def test_a_STARTUP_recovery_whose_teardown_LEAKS_keeps_the_row_for_the_next_pass(
    store, monkeypatch
):
    """#904 review 4, finding 1, on the startup path — the third of the three Hermes named.

    Recovery's own teardown can leak, and the mission it settles is `failed`, so the row it keeps
    is immediately in the state the old selection could not see: its own next pass would have
    skipped the obligation it had just created.
    """
    from agent_sessions import headless_dispatch, mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    # A DEAD owner, so the `dispatching` branch acts on the row at all.
    mid = _crashed(store, key=key)

    outcomes = ["leaked", "stopped"]
    stopped: list[str] = []

    async def teardown(engine, native, **kw):
        stopped.append(native)
        return outcomes.pop(0) if outcomes else "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    monkeypatch.setattr(headless_dispatch, "store_record_state", lambda *a, **k: "absent")

    # PASS 1 — settles the mission, cannot prove the boundary empty, keeps the obligation.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert missions.get_mission(mid)["state"] == "failed"
    assert missions.get_dispatch(mid) is not None
    assert len(stopped) == 1

    # PASS 2 — the row is still selected even though the mission is `failed`, and clears.
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1
    assert len(stopped) == 2
    assert missions.get_dispatch(mid) is None


def test_an_OBJECTIVE_EDIT_that_lands_before_the_CLAIM_refuses_the_dispatch(store, monkeypatch):
    """#904 review 4, finding 2 — the comparand has to be inside the claim's transaction.

    The route read the objectives and compared the digest, then claimed the plan in a SEPARATE
    transaction, and `patch_objectives` is legal in the gap. So a concurrent edit could replace
    checklist A with B after A had been approved and the launch still ran against B.

    Red against a `claim_plan` that does not compare: the edit is injected in the one interval
    that used to be unguarded — inside the claim's own admission, immediately before it commits.
    """
    mid, plan = _planned(store)
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    approved = missions.objectives_digest(missions.get_mission(mid)["objectives"])

    # THE EDIT LANDS IN THE GAP. Patched onto the store's own transaction opener, so it commits
    # after the route's read and before the claim's SELECT — the real interleaving, not a
    # rewritten call order in the test.
    real_ready = missions._ready
    fired: list[int] = []

    def ready_then_edit(path=None):
        con = real_ready(path)
        if not fired:
            fired.append(1)
            missions.patch_objectives(
                mid, [{"op": "add", "key": "green", "title": "Checks are green", "gate": True}]
            )
        return con

    monkeypatch.setattr(missions, "_ready", ready_then_edit)
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"], expect_objectives=approved)
    # NOT `monkeypatch.undo()` — that would also revert the `store` fixture's DB env var and
    # point the assertions below at the real missions store. The hook is a one-shot anyway.
    assert e.value.status == 409
    assert "objectives changed" in str(e.value)
    # NOTHING WAS CONSUMED. The plan is still there, so the operator can read the new checklist
    # and approve THAT one — a refused dispatch must not eat the proposal.
    assert missions.get_mission(mid)["state"] == "planned"
    assert missions.get_plan(mid) is not None


def test_a_dispatch_with_NO_OBJECTIVES_is_refused_by_the_CLAIM_itself(store):
    """#904 review 4, finding 3, at the store rather than only at the route.

    #893's acceptance invariant is that the mission knows what finishing means BEFORE it starts.
    An empty checklist is not a slow one — it is a mission with nothing for the supervisor to
    follow through on, and it reached the spawn.
    """
    mid, plan = _planned(store)
    assert missions.get_mission(mid)["objectives"] == []
    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, plan["plan_id"], require_objectives=True)
    assert e.value.status == 409
    assert "no objectives" in str(e.value)
    assert missions.get_mission(mid)["state"] == "planned"
    # …and with one, the same call claims.
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    claimed = missions.claim_plan(mid, plan["plan_id"], require_objectives=True)
    assert claimed["cwd"] == "/repo"


def test_DISPATCH_refuses_while_the_objectives_are_still_being_worked_out(store):
    """#904 review 3, finding 5, at the store's own predicate — the route reads the same field."""
    mid, plan = _planned(store)
    row = missions.get_mission(mid)
    assert row["objectives_state"] in (None, "pending", "done")
    # The DIGEST is over key/title/gate, so a state change is not a different checklist.
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    a = missions.objectives_digest(missions.get_mission(mid)["objectives"])
    missions.patch_objectives(mid, [{"op": "waive", "key": "pr"}])
    b = missions.objectives_digest(missions.get_mission(mid)["objectives"])
    assert a == b, "a settlement is progress, not a different checklist"
    # …but adding one IS.
    missions.patch_objectives(
        mid, [{"op": "add", "key": "green", "title": "Checks are green", "gate": True}]
    )
    assert missions.objectives_digest(missions.get_mission(mid)["objectives"]) != a
    # …and so is a change to what the operator is actually approving: the TITLE and whether it
    # GATES. A digest over the keys alone would call these the same checklist.
    rows = [{"key": "pr", "title": "A PR is open", "gate": True}]
    assert missions.objectives_digest(rows) != missions.objectives_digest(
        [{"key": "pr", "title": "A PR is open", "gate": False}]
    )
    assert missions.objectives_digest(rows) != missions.objectives_digest(
        [{"key": "pr", "title": "Something else entirely", "gate": True}]
    )


def test_a_SUCCESSFUL_DISPATCH_cannot_commit_inside_a_mission_WRITE_FENCE(store, monkeypatch):
    """#904 review 9, finding 1. Locking the session is not most of the protocol; it is a
    different, smaller one.

    `fenced_settle` took only `adoption_fence(physical_key)`, which orders a settlement against a
    TEARDOWN of that session and against nothing else. Phase 3b's fence enumerates the mission's
    roster, takes the roster pseudo-key, and writes — and a session being adopted is by definition
    not in the set it enumerated. So a dispatch could add one to the roster from inside somebody
    else's held fence: the question commits against a roster that no longer describes the mission,
    with the newly adopted session neither ordered against nor invalidated.

    The lock set is now `[roster, session]` on both sides — and the assertion that matters is NOT
    the ordering. `sessions_transaction` is one global mutex, so a settlement holding any key at
    all already serializes against the fence; what the key set decides is whose EPOCH moves on the
    way out. A roster that gained a session without its epoch moving is a roster whose readers
    cannot tell, which is precisely "the mutation proceeds without invalidating the newly adopted
    session". So this asserts the invalidation, and the order alongside it.

    Red against `fenced_settle` locking only the physical key: the order still holds and the
    roster's epoch does not move.
    """
    import threading

    from agent_sessions import mission_dispatch, mission_fence

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    order: list[str] = []
    settled = threading.Event()

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)

    def _dispatch_from_another_request():
        order.append("dispatch-begin")
        asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))
        order.append("adopt")
        settled.set()

    def _the_write():
        # INSIDE THE FENCE, past the re-read: this is where a question commits, and the roster it
        # was computed against is the one it is holding.
        t = threading.Thread(target=_dispatch_from_another_request, daemon=True)
        t.start()
        settled.wait(timeout=0.5)  # a real chance to win the race if nothing stops it
        order.append("write")
        t.join(timeout=10)
        return "written"

    from agent_sessions import session_input

    before = session_input.current_epoch(mission_fence.roster_key(mid))
    assert asyncio.run(mission_fence.fenced_write(mid, _the_write)) == "written"
    assert settled.wait(timeout=10), "the dispatch never settled"

    assert order.index("write") < order.index("adopt"), order
    # THE INVALIDATION. The adoption changed which sessions this mission holds, so anything
    # authorised against the old roster has to be able to see that it moved.
    assert session_input.current_epoch(mission_fence.roster_key(mid)) > before + 1, (
        "the successful dispatch adopted a session without moving the roster's epoch, so a "
        "delivery authorised against the old roster is not invalidated"
    )
    assert missions.holder_of(key) == mid
    assert missions.get_mission(mid)["state"] == "running"


def test_a_REQUEST_TIME_teardown_cannot_kill_a_session_somebody_just_adopted(store, monkeypatch):
    """#904 review 9, finding 2. The recovery pass was fixed and the request paths were not.

    `_abandon_session` relied on `spare_if`, which is a check before the signal: the predicate
    runs, `/proc` is read, and only then does the signal go out. An adoption inside THAT window
    is enough to kill the new owner's agent, and an exact-head probe produced exactly that —
    `['guard', 'adopt', 'signal']`.

    So this drives the adoption where the defect lives: from inside the teardown, past every
    guard the old design had, through the production door `POST /adopt` uses.

    Red against a teardown that is not mutually exclusive with adoption.
    """
    import threading

    from agent_sessions import mission_dispatch, runtime_cleanup
    from agent_sessions.mission_dispatch import fenced_adopt as _fenced_adopt

    key = f"claude:{UUID}"
    other = _mission("planned")

    order: list[str] = []
    adopted = threading.Event()

    def _adopt_from_another_request():
        order.append("adopt-begin")
        _fenced_adopt(other, key)
        order.append("adopt")
        adopted.set()

    async def teardown(engine, native, *, spare_if=None, **kw):
        assert spare_if is None or spare_if()
        order.append("guard")
        t = threading.Thread(target=_adopt_from_another_request, daemon=True)
        t.start()
        adopted.wait(timeout=0.5)
        order.append("signal")
        t.join(timeout=10)
        return "stopped"

    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    asyncio.run(mission_dispatch._abandon_session(key))
    assert adopted.wait(timeout=60), f"the adopter never committed after the teardown: {order}"

    assert order.index("signal") < order.index("adopt"), order
    assert missions.holder_of(key) == other


def test_the_SETTLEMENT_does_not_block_the_EVENT_LOOP(store):
    """#904 review 9, finding 4. `run()` called the fenced settlement synchronously.

    `fenced_settle` takes `session_input`'s lock and then writes to the store, and both block. On
    the app's loop that stalls every other request for as long as somebody else holds the fence —
    measured at 2.0s against a held one, and unbounded if the holder is. The merged mission-fence
    contract says the lock and the store write go onto a worker; this was the one caller still
    ignoring it.

    **Asked by the HOLDER, not by a stopwatch.** Counting ticks or timing the call is a flake on a
    loaded box. The question is exact and has an exact answer: while another thread holds the
    fence, can the event loop still run a callback? So the holder waits for the loop to say so,
    and releases either way — the test is hang-safe in both directions and answers the property
    rather than a proxy for it.

    Red against a synchronous `fenced_settle` on the loop: nothing on the loop runs until the
    fence is released, so the holder times out having seen nothing.
    """
    import threading

    from agent_sessions import mission_dispatch, session_input
    from agent_sessions.engines import physical_key

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    missions.claim_plan(mid, plan["plan_id"])
    missions.note_dispatch_session(mid, key)

    held = threading.Event()
    loop_ran = threading.Event()
    observed: list[str] = []

    def _hold():
        with session_input.sessions_transaction([physical_key(key)]):
            held.set()
            if loop_ran.wait(timeout=2.0):
                observed.append("the loop ran while the fence was held")

    async def _drive():
        t = threading.Thread(target=_hold, daemon=True)
        t.start()
        assert held.wait(timeout=5), "the fence was never taken"
        # SCHEDULED BEFORE THE SETTLEMENT and due almost immediately. A loop that is still
        # turning runs it while the settlement waits on the fence; a blocked one cannot.
        asyncio.get_running_loop().call_later(0.05, loop_ran.set)
        verdict = await asyncio.wait_for(
            mission_dispatch.settle_offloop(
                mid, to="running", detail="x", session_key=key, keep_record=True
            ),
            timeout=20,
        )
        t.join(timeout=10)
        return verdict

    verdict = asyncio.run(_drive())

    assert observed == ["the loop ran while the fence was held"], observed
    assert verdict.get("settled") is True, verdict
    assert missions.holder_of(key) == mid


def test_the_OBJECTIVES_DIGEST_is_a_cross_language_contract(store):
    """#904 review 10, finding 3, and the reason it is a shared fixture.

    The client sends this digest, the server recomputes it, and DISPATCH refuses when they differ
    — so a disagreement is not a wrong answer, it is a mission that cannot be started. Two
    hand-written encodings are exactly the thing that drifts, so one file carries the rows AND the
    digest, and `web/src/lib/digest.test.ts` asserts against the same list.

    The collision itself: the old encoding joined fields with U+001F and rows with U+001E while a
    TITLE may contain either, so a one-row checklist whose title spelled them serialized
    identically to a different two-row one — and the second DISPATCH tap could then pass the
    server's compare-and-set for a checklist the first tap never showed.

    Red against the delimiter encoding: the two cases below produce the same digest.
    """
    import json
    from pathlib import Path

    cases = json.loads(
        (Path(__file__).parent / "fixtures" / "objectives_digest_cases.json").read_text()
    )["cases"]
    assert len(cases) >= 8, "the fixture is the contract; a shrunken one proves less"
    for c in cases:
        assert missions.objectives_digest(c["rows"]) == c["digest"], c["why"]

    by_why = {c["why"]: c for c in cases}
    collide_a = next(c for w, c in by_why.items() if "THE COLLISION" in w)
    collide_b = next(c for w, c in by_why.items() if "used to serialize identically" in w)
    assert collide_a["digest"] != collide_b["digest"]

    # …and the property the fixture is a witness for, stated directly: a title may contain the
    # old separators and must still not be able to impersonate a different set.
    us, rs = chr(0x1F), chr(0x1E)
    one = [{"key": "a", "title": f"x{us}1{rs}z{us}z", "gate": True}]
    two = [
        {"key": "a", "title": "x", "gate": True},
        {"key": "z", "title": "z", "gate": True},
    ]
    # SOLVED, not guessed: under the old encoding these two hash the same string. A pair that
    # merely differs would pass against the defect and prove nothing.
    assert missions.objectives_digest(one) != missions.objectives_digest(two)

    # A REORDER is the same approval — the digest is about the SET, not the order it arrived in.
    ordered = [
        {"key": "aa", "title": "A PR is open", "gate": False},
        {"key": "zz", "title": "Merged", "gate": True},
    ]
    assert missions.objectives_digest(ordered) == missions.objectives_digest(ordered[::-1])


def test_a_CONTROL_CHARACTER_in_an_objective_title_is_refused(store):
    """The belt-and-braces half of #904 review 10, finding 3.

    The digest no longer depends on these being absent — it is length-prefixed — but an objective
    title is one line the operator reads on a card, and a C0 control in it renders as nothing and
    exists only to be confusing. Refused at the write boundary, so it cannot get into the store
    in the first place.
    """
    mid = _mission("planned")
    with pytest.raises(missions.MissionError) as e:
        missions.patch_objectives(
            mid,
            [{"op": "add", "key": "pr", "title": f"A PR{chr(0x1F)} is open", "gate": True}],
        )
    assert e.value.status == 422
    assert "control characters" in str(e.value)
    # …and the ordinary title still lands, so this refuses the character rather than the field.
    missions.patch_objectives(
        mid, [{"op": "add", "key": "pr", "title": "A PR is open", "gate": True}]
    )
    assert [o["key"] for o in missions.get_mission(mid)["objectives"]] == ["pr"]


def test_a_REFUSED_ADOPTION_keeps_the_record_of_an_agent_it_could_not_stop(store, monkeypatch):
    """#904 review 10, finding 1. Two mistakes in one branch, and together they erase the evidence.

    A refused adoption settled the mission WITHOUT `keep_record`, which deletes
    `mission_dispatches` — and then tore the session down and ignored the answer. So a launch that
    succeeded, an adoption another holder refused, and a teardown that answered `leaked` left a
    live unattended agent with NOTHING on disk naming it: the mission says `failed`, the record is
    gone, and recovery has nothing to look at.

    The failed-launch branch above already had this right, and for the stated reason — the
    settlement can refuse, and only afterwards does the caller learn whether the session could be
    stopped — so the record is kept through the settlement and dropped only on proof.

    Red against `keep_record` omitted, or against a `clear_dispatch` that does not read the
    teardown's answer.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    # A SESSION-ROUTE RESERVATION refuses the adoption, and — unlike another mission OWNING the
    # key — leaves nobody to answer for the session. That is the case where the record matters:
    # the teardown is ours to do, and it is the teardown that fails.
    missions.reserve_session(key, "session-route")
    assert missions.holder_of(key) is None, "the premise: nobody owns it, so nobody answers for it"

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    async def leaked(engine, native, *, spare_if=None, **kw):
        return "leaked"  # something survived SIGKILL — the agent may still be running

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaked)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    assert missions.get_mission(mid)["state"] == "failed"
    # THE RECORD SURVIVES, because the teardown could not prove the boundary empty. It is the only
    # thing that remembers a possibly-live unattended agent.
    assert (
        missions.get_dispatch(mid) is not None
    ), "the only durable trace of an agent that survived SIGKILL was deleted"


def test_a_PROVED_TEARDOWN_after_a_refused_adoption_discharges_the_record(store, monkeypatch):
    """The other half: `keep_record` must not become "keep it for ever".

    An obligation nobody can discharge is as useless as one nobody kept — every later recovery
    pass would try to stop a session that is already gone. The record goes when, and only when,
    the teardown proves the boundary empty.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    missions.reserve_session(key, "session-route")  # refuses the adoption; nobody owns the session

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    async def stopped(engine, native, *, spare_if=None, **kw):
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stopped)
    asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert missions.get_mission(mid)["state"] == "failed"
    # SPARED counts as discharged too — the session has an owner to answer for it.
    assert missions.get_dispatch(mid) is None


def test_a_CANCELLATION_while_RECORDING_the_dispatch_does_not_strand_the_mission(
    store, monkeypatch
):
    """#904 review 10, finding 2. The launcher's cancellation handler stopped at the spawn.

    A cancellation landing on the SETTLEMENT escaped without terminalizing anything, while
    `asyncio.to_thread` kept its worker running. If that worker then raised — a busy fence, an
    adoption another holder refused — the mission stayed `dispatching` with a LIVE owner lease,
    which is exactly the row `mission_dispatch_recover` is right to skip. Stranded for the life of
    the process, beside a running agent.

    So the settlement is shielded and reclaimed like the spawn, and whichever way it lands the
    mission is terminal with a durable obligation.

    Red against a cancellation that escapes the settlement: the mission stays `dispatching`.
    """
    import threading

    from agent_sessions import mission_dispatch

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    other = _mission("planned")
    missions.adopt(other, key)  # so the settlement's adoption is REFUSED when it finally runs

    settling = threading.Event()

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    real = mission_dispatch.fenced_settle

    def _slow_settle(*a, **kw):
        settling.set()
        time.sleep(0.3)  # the request goes away while this is in flight
        return real(*a, **kw)

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _slow_settle)

    async def _drive():
        task = asyncio.ensure_future(mission_dispatch.run(mid, claimed, registry=object()))
        await asyncio.get_running_loop().run_in_executor(None, settling.wait, 5)
        assert settling.is_set(), "the settlement never started, so nothing was cancelled in it"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    asyncio.run(_drive())

    row = missions.get_mission(mid)
    assert row["state"] != "dispatching", (
        "a cancelled request left the mission dispatching with a live owner lease — "
        "recovery skips exactly that row"
    )
    assert row["state"] == "failed"
    # …AND THE SESSION IS ACCOUNTED FOR. Here the settlement was refused because `other` holds the
    # key, so the teardown SPARES it — a spared session has a mission to answer for it, which
    # discharges the obligation. The record going is the proof, not its absence being ignored.
    assert missions.holder_of(key) == other
    assert missions.get_dispatch(mid) is None


def test_ANY_settlement_failure_leaves_a_terminal_mission_and_a_teardown(store, monkeypatch):
    """#904 review 11, finding 1. The handler caught `MissionError` and nothing else.

    A refused adoption was reconciled fully; a locked store or a filesystem error propagated out
    of `run()` with the mission still `dispatching` and THIS process's owner lease on the durable
    row — which is exactly the row startup recovery is right to skip. So an ordinary sqlite hiccup
    left a live unattended agent unattended for the life of the process.

    The distinction never mattered: whatever the reason the settlement did not land, the agent is
    running and this mission does not own it.

    Red against `except missions.MissionError`.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    stopped: list[str] = []

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    real = mission_dispatch.fenced_settle

    def _boom(mission_id_, **kw):
        # ONLY THE ADOPTING WRITE FAILS. Breaking every settlement would break the reconciliation
        # too and the test would pass for the wrong reason — it is the ADOPTION that could not be
        # recorded, and the terminal transition that must still land.
        if kw.get("session_key"):
            raise OSError("the mission store could not be written")
        return real(mission_id_, **kw)

    async def teardown(engine, native, *, spare_if=None, **kw):
        stopped.append(native)
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _boom)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    assert (
        missions.get_mission(mid)["state"] != "dispatching"
    ), "an ordinary settlement error left the mission dispatching with a live owner lease"
    # …AND THE AGENT WAS STOPPED. Nobody owns it, so leaving it running is the orphan this whole
    # path exists to prevent.
    assert stopped == [UUID]
    # …and the obligation is discharged, because the teardown PROVED the boundary empty.
    assert missions.get_dispatch(mid) is None


def test_a_CANCELLED_settlement_that_DID_NOT_SETTLE_still_tears_the_session_down(
    store, monkeypatch
):
    """#904 review 11, finding 2. "No exception" is not "the mission owns it".

    A settlement can return normally and still not settle — the operator abandons the mission
    while the worker waits on the fence, so `settle_dispatch` answers `{settled: false}`. The
    cancellation branch checked only that the worker had not raised, called that ownership, and
    re-raised without reaching the teardown. Nobody owned the session and nobody stopped it, and
    the retained row is only looked at again by the boot-time pass.

    Red against a cancellation branch that inspects `settling.exception()` and not the verdict.
    """
    import threading

    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    settling = threading.Event()
    stopped: list[str] = []

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    real = mission_dispatch.fenced_settle

    def _abandon_then_settle(*a, **kw):
        settling.set()
        time.sleep(0.3)
        # THE MISSION MOVES OUT FROM UNDER IT while the settlement waits. `settle_dispatch` is
        # governed by a state predicate, so the call below then returns NORMALLY and reports that
        # it did not settle — no exception anywhere, which is the case the old check read as
        # ownership. (`begin_archive` would RAISE instead, and a raising settlement is already
        # reconciled — a test built on it passes against the defect.)
        missions.settle_dispatch(mid, to="failed", detail="somebody else got there first")
        return real(*a, **kw)

    async def teardown(engine, native, *, spare_if=None, **kw):
        stopped.append(native)
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _abandon_then_settle)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)

    async def _drive():
        task = asyncio.ensure_future(mission_dispatch.run(mid, claimed, registry=object()))
        await asyncio.get_running_loop().run_in_executor(None, settling.wait, 5)
        assert settling.is_set(), "the settlement never started"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    asyncio.run(_drive())

    assert missions.get_mission(mid)["state"] != "dispatching"
    # THE AGENT IS STOPPED. The settlement returned without raising and without adopting, so
    # nobody owns this session — which is the case the old check read as ownership.
    assert stopped == [UUID], "a settlement that did not adopt was treated as ownership"
    assert missions.active_session_keys(mid) == []


def test_a_REPEATED_write_failure_keeps_the_row_recovery_needs(store, monkeypatch):
    """#904 review 12, finding 1. The record was discharged on the teardown's answer alone.

    `_settle` turns a store failure into `{settled: false, state: "dispatching"}` rather than
    raising — deliberately, because an exception there would replace a wrong state with the same
    wrong state plus a 500. But the reconciliation ignored that verdict: if the teardown then
    proved the boundary empty it deleted `mission_dispatches` anyway, so a second write failure
    left the mission `dispatching` FOR EVER with nothing on disk for recovery to find, while the
    API cheerfully reported `failed`.

    Red against a discharge that reads only the teardown: the row goes and the mission is stuck.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    def _every_write_fails(*a, **kw):
        raise OSError("the mission store could not be written")

    async def stopped(engine, native, *, spare_if=None, **kw):
        return "stopped"  # the process boundary IS provably empty

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _every_write_fails)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stopped)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # THE ROW SURVIVES, because the mission is still `dispatching` and that row is the only thing
    # a later pass can find it by.
    assert (
        missions.get_dispatch(mid) is not None
    ), "the only row recovery could have found was deleted while the mission stayed dispatching"
    assert missions.get_mission(mid)["state"] == "dispatching"
    # …AND THE ANSWER IS TRUTHFUL. Reporting `failed` over a mission the store never moved is the
    # false report the whole settlement path exists to avoid.
    assert out["state"] == "dispatching", out


def test_a_LEAKED_teardown_is_never_reported_as_STOPPED(store, monkeypatch):
    """#904 review 12, finding 2. The timeline claimed proof it did not have.

    The cancellation path wrote "the session … was stopped" as the settlement's reason and only
    afterwards tried to stop it. A `leaked` cleanup — something in the process group survived
    SIGKILL — therefore left the operator reading that an unattended agent was gone while it was
    still running. The durable record was correctly retained; the sentence beside it was false.

    Red against a reason that asserts the teardown's outcome before the teardown has run.
    """
    import threading

    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    settling = threading.Event()

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)

    real = mission_dispatch.fenced_settle

    def _slow(*a, **kw):
        settling.set()
        time.sleep(0.3)
        # …and the mission moves out from under it, so the settlement returns without adopting and
        # the reconciliation — the thing under test — actually runs.
        # `keep_record=True`, because THIS settlement is not the one under test: dropping the row
        # here would remove the obligation before the reconciliation could be asked about it.
        missions.settle_dispatch(
            mid, to="failed", detail="somebody else got there first", keep_record=True
        )
        return real(*a, **kw)

    async def leaked(engine, native, *, spare_if=None, **kw):
        return "leaked"  # it ignored SIGTERM and SIGKILL

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _slow)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaked)

    async def _drive():
        task = asyncio.ensure_future(mission_dispatch.run(mid, claimed, registry=object()))
        await asyncio.get_running_loop().run_in_executor(None, settling.wait, 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    asyncio.run(_drive())

    said = " ".join(str(e.get("text") or "") for e in missions.get_mission(mid)["events"]).lower()
    assert (
        "was stopped" not in said
    ), "the timeline told the operator a possibly-live unattended agent had been stopped"
    assert "could not be proved stopped" in said
    # …and the obligation is kept, because nothing proved the boundary empty.
    assert missions.get_dispatch(mid) is not None


def test_an_ATTEMPTED_launch_whose_SETTLEMENT_also_failed_keeps_its_recovery_row(
    store, monkeypatch
):
    """#904 review 13, finding 1. The same mistake, in the branch that had not been folded in.

    Review 12 fixed the discharge for the paths that go through `_orphaned_after_launch`. The
    ATTEMPTED-AND-FAILED branch — a live process with nothing in the engine's store, the case
    #840's condition 5 exists to catch — still carried its own copy: it settled, ignored the
    verdict, and deleted `mission_dispatches` on the teardown's answer alone.

    So when the terminal write ALSO failed the mission stayed `dispatching` for ever with its only
    recovery row gone, and the response named the launcher's furthest-point diagnostic as the
    mission's state. Two false facts from one branch, both acted on by the operator.

    Red against a discharge that reads only the teardown, and against returning `out.state`.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        # LAUNCHED, and neither started nor briefed: the master exists, the engine's store does
        # not know the session, so `ok` is false and `launched` is true.
        return _Out(
            key=key, started=False, briefed=False, reason="the engine's store has no such session"
        )

    def _every_write_fails(*a, **kw):
        raise OSError("the mission store could not be written")

    async def stopped(engine, native, *, spare_if=None, **kw):
        return "stopped"  # the boundary IS provably empty; only the WRITE failed

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _every_write_fails)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", stopped)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert (
        missions.get_dispatch(mid) is not None
    ), "the only row recovery could have found was deleted while the mission stayed dispatching"
    assert missions.get_mission(mid)["state"] == "dispatching"
    assert out["state"] == "dispatching", out
    # …and the launcher's own reason still rides back, because it names the missing fact.
    assert "store has no such session" in out["reason"]


def test_a_MISSION_THAT_MOVED_never_claims_a_LEAKED_session_was_stopped(store, monkeypatch):
    """#904 review 13, finding 2. The other unfolded sibling, asserting proof it did not have.

    A settlement that returns without adopting means the operator abandoned the mission while the
    launch was in flight. The agent is running and nobody owns it, so it is torn down — but this
    branch wrote "so the session that had started was stopped" into the response whatever the
    teardown answered. With a `leaked` cleanup the log correctly said an unattended agent may
    still be running while the response, which is what the operator's next decision is made on,
    said it was gone. It also returned `session_key: None`, hiding the one thing they could go and
    look at.

    Red against a reason that is written before the teardown, and against dropping the key.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)  # started AND briefed: the SUCCESSFUL path

    real = mission_dispatch.fenced_settle

    def _moved(*a, **kw):
        # The mission goes terminal underneath the settlement, so `settle_dispatch` adopts
        # nothing and reports what the mission actually became. `keep_record=True` because THIS
        # settlement is not the one under test — dropping the row here would remove the
        # obligation before the reconciliation could be asked about it.
        missions.settle_dispatch(
            mid, to="abandoned", detail="the operator abandoned it", keep_record=True
        )
        return real(*a, **kw)

    async def leaked(engine, native, *, spare_if=None, **kw):
        return "leaked"  # it ignored SIGTERM and SIGKILL

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _moved)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", leaked)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert (
        "was stopped" not in out["reason"]
    ), "the response told the operator a possibly-live unattended agent had been stopped"
    assert "could NOT be proved stopped" in out["reason"], out
    assert out["session_key"] == key, "the leaked session was not named to the operator"
    # …the mission's real state, and the obligation, both survive.
    assert out["state"] == "abandoned", out
    assert missions.get_dispatch(mid) is not None


def test_a_SPARED_teardown_is_never_reported_as_a_STOP(store, monkeypatch):
    """#904 review 13, second round. The third outcome, collapsed into the first.

    `fenced_teardown` answers `spared` when another mission has adopted the session — so it is
    RUNNING, owned, and accounted for. That reached the reconciliation as the same `True` as
    `stopped`, and the reconciliation reads the boolean as "the agent is gone": the timeline said
    the session had been stopped, `meta.stopped` said `true`, the dispatch response repeated the
    claim, and `session_key` was blanked — hiding the one thing the operator would need in order
    to go and look at the agent that is still running.

    Discharging the obligation is the ONE thing `spared` and `stopped` share, and that part was
    right: somebody answers for the agent either way, so the durable record goes. Everything the
    operator is TOLD about it differs.

    Red against a boolean teardown outcome: the mission's own timeline claims the stop.
    """
    from agent_sessions import mission_dispatch, runtime_cleanup

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])
    other = _mission("planned")

    async def launcher(**kw):
        on_key = kw.get("on_key")
        if on_key is not None:
            on_key(key)
        return _Out(key=key)  # started AND briefed: the SUCCESSFUL path

    real = mission_dispatch.fenced_settle

    def _moved(*a, **kw):
        # The operator abandons THIS mission while the launch is in flight, and another mission
        # takes the session over — a REAL holder, not a stubbed outcome, so the fence reaches its
        # `spared` branch by the route production reaches it.
        missions.settle_dispatch(
            mid, to="abandoned", detail="the operator abandoned it", keep_record=True
        )
        missions.adopt(other, key)
        return real(*a, **kw)

    killed: list[str] = []

    async def teardown(engine, native, *, spare_if=None, **kw):
        killed.append(native)
        return "stopped"

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", launcher)
    monkeypatch.setattr(mission_dispatch, "fenced_settle", _moved)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    # The session was never signalled, and it still belongs to the mission that adopted it.
    assert killed == [], "a session another mission owns was signalled"
    assert missions.holder_of(key) == other

    # THE RESPONSE THE OPERATOR'S NEXT DECISION IS MADE ON.
    assert "was stopped" not in out["reason"], out
    assert "another mission" in out["reason"], out
    assert out["session_key"] == key, "the running session was not named to the operator"

    # …AND THE DURABLE TIMELINE, which is what a later reader has.
    ev = [e for e in missions.get_mission(mid)["events"] if e.get("kind") == "session"]
    assert ev, "the reconciliation recorded nothing about the session it did not stop"
    assert "was stopped" not in str(ev[-1].get("text") or ""), ev[-1]
    assert "another mission" in str(ev[-1].get("text") or ""), ev[-1]

    # The obligation IS discharged — somebody answers for the agent — so the record goes.
    assert missions.get_dispatch(mid) is None


def test_RECOVERY_never_calls_a_session_another_mission_adopted_NEVER_STARTED(store, monkeypatch):
    """#904 review 14. The same collapse, in the sibling that reports rather than responds.

    `_stop` reports three outcomes now, and the startup pass reduced them to two the line after
    receiving them, then wrote "which never started" over all of them. `spared` means another
    mission adopted the session between this pass's store probe and the fenced teardown — so it
    EXISTS and is RUNNING under that mission. The pass then settled the original mission with that
    false line as the only durable trace and discharged the record, so nothing anywhere recorded
    that the session had been handed on rather than never begun.

    Driven through the production ordering: the engine store answers ABSENT, the adoption lands
    between that answer and the fence, and the fence spares the session on its own ownership read
    — no stubbed outcome anywhere.

    Red against `outcome != "leaked"` deciding the sentence.
    """
    from agent_sessions import mission_dispatch_recover, runtime_cleanup

    key = f"claude:{UUID}"
    mid = _crashed(store, key=key)
    other = _mission("planned")

    async def absent_then_adopted(k, cwd):
        # THE STORE HAS NO ROW … and the adoption lands right here, before the teardown.
        missions.adopt(other, str(k))
        return False

    killed: list[str] = []

    async def teardown(engine, native, *, spare_if=None, **kw):
        killed.append(native)
        return "stopped"

    monkeypatch.setattr(mission_dispatch_recover, "_has_session", absent_then_adopted)
    monkeypatch.setattr(runtime_cleanup, "cleanup_runtime", teardown)
    assert asyncio.run(mission_dispatch_recover.recover_once()) == 1

    assert killed == [], "a session another mission owns was signalled"
    assert missions.holder_of(key) == other
    said = _last_state_event(mid)
    assert "never started" not in said, said
    assert "another mission" in said, said
    # …and the obligation is discharged, because that other mission now answers for the session.
    assert missions.get_dispatch(mid) is None


def test_a_SUPERSEDED_dispatch_cannot_stamp_adopt_or_CLEAR_the_plan_that_replaced_it(
    store, monkeypatch
):
    """#904 review 15. Every write after the claim named the MISSION, and none named the ATTEMPT.

    `dispatching -> planned` is a legal retreat, so this is not a hypothetical interleave:

      1. attempt A is claimed and its launch is in flight;
      2. the mission retreats to `planned` — A is now a dispatch nobody is waiting for;
      3. the operator proposes plan B, approves it, and the claim overwrites the dispatch row
         through `ON CONFLICT(mission_id)`. The mission is `dispatching` again, for B;
      4. A resumes.

    A's stamp then found a row (same mission), A's settlement found `dispatching` (same mission),
    and the state predicate answers "is a dispatch in flight" — never "is it MINE". So A wrote its
    session key onto B's row, adopted A's session under B's approval, moved the mission to
    `running`, and deleted B's record: an agent the operator never approved, running under an
    approval it silently consumed.

    `plan_id` is minted per plan and already rides on the row, so it is the attempt's identity.
    Every stamp, settle and clear names it, and a superseded attempt touches NOTHING.

    Red against mutations keyed on `mission_id` alone: A stamps, A adopts, and B's record is gone.
    """
    key_a = f"claude:{UUID}"
    key_b = "claude:99999999-9999-9999-9999-999999999999"

    # 1. ATTEMPT A, claimed.
    mid, plan_a = _planned(store)
    missions.claim_plan(mid, plan_a["plan_id"])
    assert missions.get_mission(mid)["state"] == "dispatching"

    # 2. THE LEGAL RETREAT — A is still in flight, and the mission goes back.
    missions.settle_dispatch(
        mid, to="planned", detail="the operator went back", expect_plan=plan_a["plan_id"]
    )
    assert missions.get_mission(mid)["state"] == "planned"

    # 3. PLAN B, proposed and approved. Its claim overwrites the row A is still holding.
    plan_b = missions.put_plan(
        mid, project_id="prj_a", cwd="/repo", engine="claude", brief="something else"
    )
    assert plan_b["plan_id"] != plan_a["plan_id"]
    missions.claim_plan(mid, plan_b["plan_id"])
    assert missions.get_mission(mid)["state"] == "dispatching"
    assert missions.get_dispatch(mid)["plan_id"] == plan_b["plan_id"]

    # 4. A RESUMES. It may not stamp …
    assert not missions.note_dispatch_session(
        mid, key_a, expect_plan=plan_a["plan_id"]
    ), "a superseded attempt stamped its session key onto the row that replaced it"
    # …and B's row is untouched.
    assert missions.get_dispatch(mid)["session_key"] is None

    # … it may not settle, adopt, or consume B's approval …
    verdict = missions.settle_dispatch(
        mid,
        to="running",
        detail="A finally finished",
        session_key=key_a,
        expect_plan=plan_a["plan_id"],
    )
    assert verdict.get("settled") is False, verdict
    assert verdict.get("adopted") is False, verdict
    assert verdict.get("stale") is True, verdict
    assert missions.get_mission(mid)["state"] == "dispatching", "A settled B's dispatch"
    assert missions.active_session_keys(mid) == [], "A's session was adopted under B's approval"

    # … and it may not discharge B's record.
    assert not missions.clear_dispatch(mid, expect_plan=plan_a["plan_id"])
    row = missions.get_dispatch(mid)
    assert row is not None, "A deleted the record of the attempt that replaced it"
    assert row["plan_id"] == plan_b["plan_id"]

    # B, meanwhile, is entirely unaffected and settles normally.
    assert missions.note_dispatch_session(mid, key_b, expect_plan=plan_b["plan_id"])
    ok = missions.settle_dispatch(
        mid,
        to="running",
        detail="B dispatched",
        session_key=key_b,
        expect_plan=plan_b["plan_id"],
    )
    assert ok.get("settled") and ok.get("adopted"), ok
    assert missions.active_session_keys(mid) == [key_b]


def test_a_SUPERSEDED_attempt_cannot_SPAWN_under_the_approval_that_replaced_it(store, monkeypatch):
    """#904 review 16. The CAS closed the window after the spawn; this is the window before it.

    `on_key` runs BEFORE the launch fence, and deliberately: a record of an intention is the only
    thing a crashed dispatch can be reconciled against, so it has to be written before anything
    can exist. That leaves a real gap — the attempt stamps while it is current, then waits for the
    fence, and the mission may legally retreat to `planned`, be re-planned, approved and claimed
    inside that wait.

    Every later write is now a CAS, so the settlement catches it. But "catches it" means the agent
    has ALREADY started and already been handed a brief nobody approved, and is then torn down —
    which is the outcome the whole authorize-under-the-fence step exists to prevent. Policy and
    cwd were rechecked there for exactly this reason; the attempt's own identity was not.

    Production's callback order, not a summary of it: `on_key` first, the replacement inside the
    wait, then `authorize` under a REAL launch fence — the last gate before `_popen`.

    Red against an `authorize` that checks only policy and cwd: it returns `None` and the agent
    spawns.
    """
    from agent_sessions import mission_dispatch, session_input

    key_a = f"claude:{UUID}"
    mid, plan_a = _planned(store)
    claimed_a = missions.claim_plan(mid, plan_a["plan_id"])

    spawned: list[str] = []
    stamped: list[bool] = []

    async def fake(**kw):
        # 1. A STAMPS, while it is still the current attempt — before the fence, as production
        #    does it, because the record must precede anything that could exist.
        stamped.append(True)
        kw["on_key"](key_a)

        # 2. …AND THE MISSION LEGALLY MOVES ON while A waits for the fence: back to `planned`,
        #    a new proposal, the operator's approval, and B's claim overwriting A's row.
        missions.settle_dispatch(
            mid, to="planned", detail="the operator went back", expect_plan=plan_a["plan_id"]
        )
        plan_b = missions.put_plan(
            mid, project_id="prj_a", cwd="/repo", engine="claude", brief="something else"
        )
        missions.claim_plan(mid, plan_b["plan_id"])
        assert missions.get_dispatch(mid)["plan_id"] == plan_b["plan_id"]

        # 3. A REACHES THE LAST GATE. A real fence, so `authorize` is handed the value production
        #    hands it and the policy half genuinely passes — leaving the identity as the only
        #    thing that can refuse.
        with session_input.launch_fence() as inside:
            why = kw["authorize"](inside)
        if why:
            return _Out(started=False, briefed=False, launched=False, reason=why)
        spawned.append("agent")
        return _Out(key=key_a)

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", fake)
    out = asyncio.run(mission_dispatch.run(mid, claimed_a, registry=object()))

    assert stamped == [True], "the test never reached the stamp it is about"
    assert (
        spawned == []
    ), "a superseded attempt started an agent under the approval that replaced it"
    assert "superseded" in out["reason"], out
    # …and B is untouched: its record still stands, and nothing was adopted under it.
    row = missions.get_dispatch(mid)
    assert row is not None and row["session_key"] is None
    assert missions.active_session_keys(mid) == []


def test_an_EXCEPTION_after_the_KEY_WAS_MINTED_keeps_the_record_that_names_it(store, monkeypatch):
    """#904 review 18, finding 1. "We cannot say what happened" is when the record matters MOST.

    `on_key` exists for one reason, and its own comment states it: a record that names a session
    which never came to be is recoverable, one that misses a session which DID is not. The generic
    launcher-exception handler then did exactly the thing that comment rules out — it settled with
    the default `keep_record=False` and reported `session_key: None`, so an `OSError` landing
    after the spawn (a full disk on a log write, a failing `proc.wait()`) deleted the durable row
    and left an agent running with nothing on disk naming it.

    The frame cannot tell whether a process exists; that is the whole point. What it CAN do is
    keep the row and name the key, so the startup pass — which probes the engine's own store —
    has something to reconcile from.

    Red against a handler that discards the minted key: the row goes and the answer says `None`.
    """
    from agent_sessions import mission_dispatch

    key = f"claude:{UUID}"
    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def raises_after_stamping(**kw):
        # The key is minted and stamped — a master may exist by now — and THEN it fails.
        kw["on_key"](key)
        raise OSError("no space left on device")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", raises_after_stamping)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    assert out["session_key"] == key, "the answer did not name the session it may have started"
    row = missions.get_dispatch(mid)
    assert row is not None, "the only row naming a possibly-live agent was deleted"
    assert row["session_key"] == key
    assert missions.get_mission(mid)["state"] == "failed"


def test_an_exception_BEFORE_any_key_was_minted_still_discharges_the_record(store, monkeypatch):
    """The other half, so the rule above is a DISTINCTION and not just "always keep it".

    Nothing was minted, so nothing can be running, and a record that names no session is an
    obligation nobody can ever discharge — the pass would carry it for ever.
    """
    from agent_sessions import mission_dispatch

    mid, plan = _planned(store)
    claimed = missions.claim_plan(mid, plan["plan_id"])

    async def raises_before_stamping(**kw):
        raise OSError("the launcher fell over before anything was minted")

    monkeypatch.setattr(mission_dispatch.headless_dispatch, "dispatch", raises_before_stamping)
    out = asyncio.run(mission_dispatch.run(mid, claimed, registry=object()))

    assert out["state"] == "failed"
    assert out["session_key"] is None
    assert missions.get_dispatch(mid) is None, "a record naming no session was kept for ever"
