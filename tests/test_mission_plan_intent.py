"""A new mission plans itself, durably, fenced by generation, and keeps the chosen project (#967).

P2a of #967, the server half. The contracts pinned here:

* **Intent is durable with creation.** `plan_state='pending'` is written by the transaction that
  creates the mission, so a crash before the background planner runs loses nothing.
* **The plan and its settlement are one write.** A plan never exists beside `pending`.
* **Every settlement is fenced by the attempt's generation** — a plan, `failed` and `skipped`
  alike. A late planner never writes over a newer attempt or over the operator's own save.
* **Recovery is bound to the generation, not to "a plan row exists".** A Plan again interrupted
  by a restart is never reported as finished with the previous plan.
* **The chosen project is enforced in code.** A reply naming another is ignored; a project gone
  by the write fails the attempt and writes no plan.
* **A skipped or failed mission can be planned by hand**, through the existing `PATCH /plan`.
* **An edit records `plan_edit`, never the brief.**
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import threading
import time

import pytest

from agent_sessions import aitasks, mission_plan, missions, projects, prompts, review

ENGINES = [{"id": "claude", "label": "claude"}, {"id": "codex", "label": "codex"}]
REPLY = {"project_index": 0, "engine_index": 0, "engine_reason": "it fits", "brief": "go"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_PROJECTS", str(tmp_path / "projects.json"))
    missions.reset_schema_cache_for_test()
    aitasks.reset()
    # The engine list is a property of the host; the planner's handling of it is what is tested.
    monkeypatch.setattr(mission_plan, "engine_options", lambda: ENGINES)
    yield tmp_path
    aitasks.reset()
    missions.reset_schema_cache_for_test()


@pytest.fixture
def api(store, auth_cfg, tmp_home, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    # `tmp_home` moves HOME; the projects file stays where `store` put it.
    monkeypatch.setenv("AGENT_SESSIONS_PROJECTS", str(store / "projects.json"))
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    hdr = {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    return c, hdr, _project(store, "the-app")


def _project(root, name):
    folder = root / "repos" / name
    folder.mkdir(parents=True, exist_ok=True)
    return projects.create(name, folders=[str(folder)], default_folder=str(folder))


def _configured(monkeypatch):
    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})


def _not_configured(monkeypatch):
    def boom():
        raise review.NotConfiguredError("no endpoint")

    monkeypatch.setattr(review, "_require_config", boom)


class _Model:
    """The model, counting PLANNING calls only — told apart by the system prompt they send.

    The objectives producer shares `complete_json` and runs beside the planner on create; it is
    answered with an empty checklist and not counted.
    """

    def __init__(self, reply=None, *, raises=None, during=None):
        self.reply = reply if reply is not None else REPLY
        self.raises = raises
        self.during = during
        self.calls = 0
        self.messages: list[list[dict]] = []

    async def __call__(self, messages, **_kw):
        if messages[0]["content"] != prompts.effective("mission_plan"):
            return {"objectives": []}
        self.calls += 1
        self.messages.append(messages)
        if self.during is not None:
            out = self.during()
            if asyncio.iscoroutine(out):
                await out
        if self.raises is not None:
            raise self.raises
        return dict(self.reply)


def _model(monkeypatch, **kw) -> _Model:
    m = _Model(**kw)
    monkeypatch.setattr(review, "complete_json", m)
    return m


def _mission(**kw) -> str:
    return missions.create_mission("ship it", **({"cwd": "/repo"} | kw))["id"]


def _events(mission_id, kind):
    rows = missions.get_mission(mission_id, events_limit=200)["events"]
    return [e for e in rows if e["kind"] == kind]


def _pending():
    return [mid for _, mid in missions.missions_awaiting_plan(limit=500)]


# ---- intent -------------------------------------------------------------------------------


def test_CREATE_records_the_planning_intent_in_the_SAME_transaction(store):
    """Nothing but the create has run: no background task, no planner. The intent is already
    durable, so a crash right here is found by recovery rather than lost."""
    mid = _mission()
    row = missions.get_mission(mid)
    assert row["plan_state"] == "pending"
    assert row["plan_generation"] == 1
    assert _pending() == [mid]


def test_POST_missions_starts_EXACTLY_ONE_planning_run(api, monkeypatch):
    """Through the route, because the defect would be in the wiring: a planner nobody calls, or
    one called twice. The spy reads the row BEFORE the planner runs — `pending` there is the
    intent committed by the create, not something the task wrote."""
    c, hdr, proj = api
    _configured(monkeypatch)
    seen: list[str] = []
    real = mission_plan.propose_for_new_mission

    async def spy(mission_id, **kw):
        seen.append(missions.get_mission(mission_id)["plan_state"])
        return await real(mission_id, **kw)

    monkeypatch.setattr(mission_plan, "propose_for_new_mission", spy)
    model = _model(monkeypatch)

    r = c.post("/api/missions", json={"instruction": "ship it", "project_id": proj.id}, headers=hdr)
    assert r.status_code == 201, r.text
    assert r.json()["plan_state"] == "pending", "the create response carries the intent"
    assert seen == ["pending"], "the planner was not scheduled exactly once after the commit"
    assert model.calls == 1

    detail = c.get(f"/api/missions/{r.json()['id']}", headers=hdr).json()
    assert (detail["state"], detail["plan_state"]) == ("planned", "ready")
    assert missions.get_plan(detail["id"])["project_id"] == proj.id


def test_a_SECOND_concurrent_plan_request_is_REFUSED_and_never_runs_the_model_twice(
    store, monkeypatch
):
    """Per-mission single-flight, with the generation taken INSIDE it: the refused request
    changes nothing, so it cannot retire the attempt that is actually running."""
    _configured(monkeypatch)
    mid = _mission()

    async def drive():
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def during():
            entered.set()
            await gate.wait()

        model = _model(monkeypatch, during=during)
        first = asyncio.create_task(mission_plan.propose(mid))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(mission_plan.PlanError) as e:
            await asyncio.wait_for(mission_plan.propose(mid), 5)
        assert (e.value.status, e.value.outcome) == (409, "busy")
        # The create-time producer and recovery defer to the owner rather than joining it.
        joined = await asyncio.wait_for(mission_plan.propose_for_new_mission(mid), 5)
        assert joined == {"plan_state": "already_running"}
        generation_during = missions.get_mission(mid)["plan_generation"]
        gate.set()
        return model, await first, generation_during

    model, plan, generation_during = asyncio.run(drive())
    assert model.calls == 1
    assert generation_during == 2, "the refused request took a generation"
    assert plan["generation"] == 2
    assert missions.get_mission(mid)["plan_state"] == "ready"


# ---- settlement ---------------------------------------------------------------------------


def test_NO_ENDPOINT_settles_SKIPPED_with_an_event(store, monkeypatch):
    _not_configured(monkeypatch)
    model = _model(monkeypatch, raises=AssertionError("the model was asked"))
    mid = _mission()

    asyncio.run(mission_plan.propose_for_new_mission(mid))

    row = missions.get_mission(mid)
    assert row["plan_state"] == "skipped"
    assert "no AI endpoint" in row["plan_detail"]
    assert model.calls == 0
    (ev,) = _events(mid, "planning")
    assert ev["meta"]["outcome"] == "skipped" and ev["meta"]["generation"] == 1
    assert _pending() == [], "an unconfigured install left the intent pending"


def test_a_MODEL_FAILURE_settles_FAILED_with_the_error_text_and_an_event(store, monkeypatch):
    _configured(monkeypatch)
    _model(monkeypatch, raises=RuntimeError("endpoint returned HTTP 500"))
    mid = _mission()

    asyncio.run(mission_plan.propose_for_new_mission(mid))

    row = missions.get_mission(mid)
    assert row["plan_state"] == "failed"
    assert "HTTP 500" in row["plan_detail"]
    (ev,) = _events(mid, "planning")
    assert ev["meta"]["outcome"] == "failed" and "HTTP 500" in ev["text"]
    assert missions.get_plan(mid) is None
    assert row["state"] == "draft"
    assert _pending() == []


def test_the_plan_and_its_READY_settlement_are_ONE_write(store, monkeypatch):
    """If anything in the plan write fails, neither the plan nor `ready` is left behind.

    Red against a plan committed first and settled in a second write: the failure below lands
    between them, and a plan then sits beside `pending`.
    """
    mid = _mission()
    real = missions._append_event

    def failing(con, mission_id, kind, **kw):
        if kind == "plan":
            raise sqlite3.OperationalError("disk I/O error")
        return real(con, mission_id, kind, **kw)

    monkeypatch.setattr(missions, "_append_event", failing)
    with pytest.raises(sqlite3.OperationalError):
        missions.put_plan(
            mid, project_id=None, cwd="/repo", engine="claude", brief="go", generation=1
        )
    monkeypatch.setattr(missions, "_append_event", real)

    assert missions.get_plan(mid) is None, "a plan survived the settlement that failed"
    assert missions.get_mission(mid)["plan_state"] == "pending"


# ---- the generation fence -----------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["failed", "skipped"])
def test_a_STALE_attempts_FAILED_or_SKIPPED_settlement_is_DISCARDED(store, outcome):
    """Fenced like a successful plan. Red against a CAS on `pending` alone: attempt 1's outcome
    would close attempt 2's intent while attempt 2 is still running."""
    mid = _mission()
    assert missions.begin_planning(mid) == 2

    assert missions.settle_plan(mid, 1, outcome, detail="too late") is False
    row = missions.get_mission(mid)
    assert (row["plan_state"], row["plan_generation"]) == ("pending", 2)
    (ev,) = _events(mid, "planning")
    assert ev["meta"]["outcome"] == "discarded"
    assert (ev["meta"]["discarded"], ev["meta"]["generation"]) == (outcome, 1)

    # …and the CURRENT attempt still settles.
    assert missions.settle_plan(mid, 2, outcome, detail="now") is True
    assert missions.get_mission(mid)["plan_state"] == outcome


def test_a_STALE_attempts_PLAN_is_DISCARDED_and_not_written(store):
    mid = _mission()
    missions.begin_planning(mid)
    with pytest.raises(missions.PlanSuperseded):
        missions.put_plan(
            mid, project_id=None, cwd="/repo", engine="claude", brief="late", generation=1
        )
    assert missions.get_plan(mid) is None
    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"], row["plan_generation"]) == ("draft", "pending", 2)
    (ev,) = _events(mid, "planning")
    assert ev["meta"]["outcome"] == "discarded" and ev["meta"]["discarded"] == "ready"


@pytest.mark.parametrize("outcome", ["ready", "failed", "skipped"])
def test_a_planner_OVERTAKEN_mid_call_changes_nothing_it_no_longer_owns(
    store, monkeypatch, outcome
):
    """The same fence driven through the production planner: a newer attempt starts while the
    model is thinking (another process's Plan again), and whatever the old attempt comes back
    with — a plan, a failure, no endpoint — is discarded with an event."""
    _configured(monkeypatch)
    mid = _mission()
    raises = {
        "ready": None,
        "failed": RuntimeError("endpoint returned HTTP 500"),
        "skipped": review.NotConfiguredError("no endpoint"),
    }[outcome]
    _model(monkeypatch, raises=raises, during=lambda: missions.begin_planning(mid))

    asyncio.run(mission_plan.propose_for_new_mission(mid))

    row = missions.get_mission(mid)
    assert (row["plan_state"], row["plan_generation"]) == ("pending", 2)
    assert missions.get_plan(mid) is None
    assert [e["meta"]["outcome"] for e in _events(mid, "planning")] == ["discarded"]


def test_a_LATE_planner_result_after_a_MANUAL_first_plan_is_DISCARDED(store, monkeypatch):
    """The operator's own save takes a new generation, so a planner still on its way loses."""
    _configured(monkeypatch)
    mid = _mission()

    def operator_plans_by_hand():
        # Planning is given up on (e.g. by another process), and the operator saves a first plan.
        missions.settle_plan(mid, 1, "failed", detail="gave up")
        missions.put_plan(
            mid, project_id=None, cwd="/repo", engine="codex", brief="my own plan", first_plan=True
        )

    _model(monkeypatch, reply=REPLY | {"brief": "the model's plan"}, during=operator_plans_by_hand)
    out = asyncio.run(mission_plan.propose_for_new_mission(mid))

    assert out["plan_state"] == "superseded"
    plan = missions.get_plan(mid)
    assert (plan["brief"], plan["engine"]) == ("my own plan", "codex")
    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"]) == ("planned", "ready")
    assert "discarded" in [e["meta"]["outcome"] for e in _events(mid, "planning")]


# ---- recovery -----------------------------------------------------------------------------


def _restart():
    """What a restart leaves: nothing in flight in this process, the store as it was."""
    aitasks.reset()
    missions.reset_schema_cache_for_test()


def test_RECOVERY_resumes_a_PENDING_plan_after_a_restart(store, monkeypatch):
    _configured(monkeypatch)
    mid = _mission()  # created; the background planner never ran — the crash
    _restart()
    model = _model(monkeypatch)

    out = asyncio.run(mission_plan.recover_pending())

    assert out["recovered"] == [mid]
    assert model.calls == 1
    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"]) == ("planned", "ready")
    assert missions.get_plan(mid)["generation"] == 1
    assert _pending() == []


def test_RECOVERY_settles_READY_from_a_stored_plan_of_the_PENDING_generation_with_NO_model_call(
    store, monkeypatch
):
    """`put_plan` makes this unreachable, so it is forced by hand: attempt 1 is pending and its
    plan is already stored. Recovery must finish it without asking the model again."""
    mid = _mission()
    con = sqlite3.connect(missions._db_path())
    con.execute(
        "INSERT INTO mission_plans (mission_id, plan_id, project_id, cwd, engine, engine_reason,"
        " brief, created_at, generation) VALUES (?,?,?,?,?,?,?,?,?)",
        (mid, "pln_stored", None, "/repo", "claude", "", "stored", time.time(), 1),
    )
    con.commit()
    con.close()
    _restart()
    _configured(monkeypatch)
    model = _model(monkeypatch, raises=AssertionError("recovery asked the model again"))

    asyncio.run(mission_plan.recover_pending())

    assert model.calls == 0
    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"]) == ("planned", "ready")
    assert missions.get_plan(mid)["plan_id"] == "pln_stored"


@pytest.mark.parametrize("variant", ["model", "no_endpoint"])
def test_a_REPLAN_interrupted_by_a_restart_is_NOT_settled_with_the_OLD_plan(
    store, monkeypatch, variant
):
    """Plan A exists (attempt 1), Plan again starts (attempt 2), the app restarts before B.

    Red against recovery that settles on "a plan row exists": it reports A as B's result and
    never asks the model."""
    mid = _mission()
    a = missions.put_plan(
        mid, project_id=None, cwd="/repo", engine="claude", brief="A", generation=1
    )
    assert a["generation"] == 1
    assert missions.begin_planning(mid) == 2
    _restart()

    if variant == "model":
        _configured(monkeypatch)
        model = _model(monkeypatch, reply={"engine_index": 1, "engine_reason": "r", "brief": "B"})
        asyncio.run(mission_plan.recover_pending())
        assert model.calls == 1
        plan = missions.get_plan(mid)
        assert (plan["brief"], plan["generation"]) == ("B", 2)
        assert plan["plan_id"] != a["plan_id"]
        assert missions.get_mission(mid)["plan_state"] == "ready"
    else:
        _not_configured(monkeypatch)
        model = _model(monkeypatch, raises=AssertionError("no endpoint, no call"))
        asyncio.run(mission_plan.recover_pending())
        row = missions.get_mission(mid)
        assert (row["plan_state"], row["plan_generation"]) == ("skipped", 2)
        # A is still stored, and nothing reports it as attempt 2's result.
        assert missions.get_plan(mid)["plan_id"] == a["plan_id"]
        (ev,) = _events(mid, "planning")
        assert (ev["meta"]["outcome"], ev["meta"]["generation"]) == ("skipped", 2)


# ---- the operator's chosen project --------------------------------------------------------


def test_the_CHOSEN_project_is_KEPT_and_a_reply_naming_ANOTHER_is_ignored(store, monkeypatch):
    """Red against the planner persisting whatever project the model picked."""
    alpha = _project(store, "alpha")
    _project(store, "beta")
    mid = _mission(project_id=alpha.id, cwd=alpha.default_folder)
    _configured(monkeypatch)
    model = _model(monkeypatch, reply=REPLY | {"project_index": 1})

    plan = asyncio.run(mission_plan.propose(mid))

    assert (plan["project_id"], plan["cwd"]) == (alpha.id, alpha.default_folder)
    assert missions.get_plan(mid)["project_id"] == alpha.id
    (note,) = _events(mid, "planning")
    assert note["meta"]["outcome"] == "project_conflict"
    # The model was shown the chosen project and no other.
    user = model.messages[0][1]["content"]
    assert "alpha" in user and "beta" not in user


def test_a_chosen_project_OUTSIDE_the_40_option_cap_is_still_kept(store, monkeypatch):
    made = [_project(store, f"p{i:02d}") for i in range(mission_plan.MAX_PROJECTS + 1)]
    chosen = made[-1]
    assert chosen.id not in {p["id"] for p in mission_plan.project_options()}
    mid = _mission(project_id=chosen.id, cwd=chosen.default_folder)
    _configured(monkeypatch)
    _model(monkeypatch, reply=REPLY | {"project_index": None})

    plan = asyncio.run(mission_plan.propose(mid))

    assert plan["project_id"] == chosen.id
    assert plan["cwd"] == chosen.default_folder


@pytest.mark.parametrize(
    ("change", "reason"),
    [("archive", "archived"), ("remove", "no longer exists")],
)
def test_a_chosen_project_GONE_during_the_call_FAILS_and_writes_no_plan(
    store, monkeypatch, change, reason
):
    alpha = _project(store, "alpha")
    mid = _mission(project_id=alpha.id, cwd=alpha.default_folder)
    _configured(monkeypatch)

    def gone():
        if change == "archive":
            projects.update(alpha.id, archived=True)
        else:
            projects.delete(alpha.id)

    _model(monkeypatch, during=gone)
    with pytest.raises(mission_plan.PlanError) as e:
        asyncio.run(mission_plan.propose(mid))
    assert reason in str(e.value)

    assert missions.get_plan(mid) is None
    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"]) == ("draft", "failed")
    assert reason in row["plan_detail"]


# ---- the manual first plan ----------------------------------------------------------------


def test_NO_ENDPOINT_then_PLAN_MANUALLY_saves_the_first_plan(api, monkeypatch):
    c, hdr, proj = api
    _not_configured(monkeypatch)
    m = c.post("/api/missions", json={"instruction": "ship it"}, headers=hdr).json()
    url = f"/api/missions/{m['id']}/plan"
    detail = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert (detail["state"], detail["plan_state"]) == ("draft", "skipped")

    for bad in (
        {"project_id": "prj_nope", "engine": "claude", "brief": "go"},
        {"project_id": "/etc", "engine": "claude", "brief": "go"},
        {"project_id": proj.id, "engine": "shell", "brief": "go"},
        {"project_id": proj.id, "brief": "go"},
        {"project_id": proj.id, "engine": "claude", "brief": "  "},
    ):
        r = c.patch(url, json=bad, headers=hdr)
        assert r.status_code == 422, (bad, r.text)
    assert missions.get_plan(m["id"]) is None

    r = c.patch(
        url, json={"project_id": proj.id, "engine": "claude", "brief": "do it"}, headers=hdr
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    assert (plan["project_id"], plan["cwd"], plan["engine"]) == (
        proj.id,
        proj.default_folder,
        "claude",
    )
    detail = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert (detail["state"], detail["plan_state"]) == ("planned", "ready")

    # With a plan stored, the no-`plan_id` shape is the old 422 again.
    again = c.patch(
        url, json={"project_id": proj.id, "engine": "claude", "brief": "x"}, headers=hdr
    )
    assert again.status_code == 422, again.text


def test_PLAN_MANUALLY_offers_and_saves_a_chosen_project_PAST_the_models_40_project_cap(
    api, store, monkeypatch
):
    """The operator's picker is not the model's capped list (#967, the review on #984).

    More eligible projects than `MAX_PROJECTS`, and the mission's own project sorting LAST. The
    no-endpoint recovery path offered the first 40 only, so Plan manually could not select the
    project the mission was created in, and a plan edit could not show it, although `PATCH /plan`
    resolves that project by id. Red against the capped list, on both surfaces.
    """
    c, hdr, _proj = api
    _not_configured(monkeypatch)
    for i in range(mission_plan.MAX_PROJECTS + 1):
        _project(store, f"acme-{i:02d}")
    last = _project(store, "zzz-last-sorting")
    # …and one the save would refuse: an archived project is not offered either.
    archived = _project(store, "acme-archived")
    projects.update(archived.id, archived=True)

    # The MODEL'S list keeps its cap, and the project is past it: that is the case under test.
    model_ids = {p["id"] for p in mission_plan.project_options()}
    assert len(model_ids) == mission_plan.MAX_PROJECTS
    assert last.id not in model_ids

    m = c.post("/api/missions", json={"instruction": "ship it", "project_id": last.id}, headers=hdr)
    assert m.status_code in (200, 201), m.text
    mid = m.json()["id"]
    detail = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert (detail["plan_state"], detail["project_id"]) == ("skipped", last.id)
    assert detail["plan"] is None
    offered = [p["id"] for p in detail["plan_options"]["project_options"]]
    assert last.id in offered, f"the mission's own project was not offered ({len(offered)} were)"
    assert len(offered) > mission_plan.MAX_PROJECTS
    assert archived.id not in offered, "an archived project was offered"

    # …it saves as the first plan, through the same validation as before…
    r = c.patch(
        f"/api/missions/{mid}/plan",
        json={"project_id": last.id, "engine": "claude", "brief": "by hand"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    assert (plan["project_id"], plan["cwd"]) == (last.id, last.default_folder)

    # …and the EDIT picker can show it: on the save's response and on the next read.
    assert last.id in {p["id"] for p in plan["project_options"]}
    detail = c.get(f"/api/missions/{mid}", headers=hdr).json()
    assert (detail["state"], detail["plan_state"]) == ("planned", "ready")
    assert detail["plan"]["project_id"] == last.id
    assert last.id in {p["id"] for p in detail["plan"]["project_options"]}


def test_a_first_plan_body_is_REFUSED_while_planning_is_PENDING(api):
    """Other states keep their previous answer: without a `plan_id` that is a 422."""
    c, hdr, proj = api
    mid = _mission()  # pending, no background task
    r = c.patch(
        f"/api/missions/{mid}/plan",
        json={"project_id": proj.id, "engine": "claude", "brief": "go"},
        headers=hdr,
    )
    assert r.status_code == 422, r.text
    assert missions.get_plan(mid) is None


# ---- edits --------------------------------------------------------------------------------


def test_an_EDIT_writes_ONE_plan_edit_event_with_the_changed_fields_and_NO_brief(store):
    mid = _mission()
    a = missions.put_plan(mid, project_id="prj_a", cwd="/repo", engine="claude", brief="secret one")
    b = missions.put_plan(
        mid,
        project_id="prj_a",
        cwd="/repo",
        engine="codex",
        brief="secret two",
        expect_plan_id=a["plan_id"],
    )

    assert len(_events(mid, "plan")) == 1, "an edit appended another full plan event"
    (edit,) = _events(mid, "plan_edit")
    assert edit["meta"] == {"plan_id": b["plan_id"], "changed": ["engine", "brief"]}
    assert not edit["text"]
    assert "secret" not in json.dumps(edit)
    # The edit is the operator's save: it takes the plan, and a newer generation with it.
    row = missions.get_mission(mid)
    assert (row["plan_state"], row["plan_generation"]) == ("ready", b["generation"])


# ---- dispatch waits for a pending attempt -------------------------------------------------


def _planned_then_replanning():
    """Plan A is stored (attempt 1, `ready`, `planned`); Plan again then starts attempt 2."""
    mid = _mission()
    a = missions.put_plan(
        mid, project_id=None, cwd="/repo", engine="claude", brief="A", generation=1
    )
    assert missions.begin_planning(mid) == 2
    return mid, a


def test_DISPATCH_is_REFUSED_while_a_plan_again_is_PENDING_and_changes_nothing(store):
    """Red against a claim that ignores `plan_state`: plan A launches after the operator asked
    for a new plan, and a refused launch would put A back under attempt 2's generation, where
    recovery reads it as attempt 2's result."""
    mid, a = _planned_then_replanning()

    with pytest.raises(missions.MissionError) as e:
        missions.claim_plan(mid, a["plan_id"])
    assert e.value.status == 409
    assert "still being prepared" in str(e.value)

    row = missions.get_mission(mid)
    assert (row["state"], row["plan_state"], row["plan_generation"]) == ("planned", "pending", 2)
    assert missions.get_plan(mid)["plan_id"] == a["plan_id"], "the claim consumed the plan"
    assert missions.get_dispatch(mid) is None


@pytest.mark.parametrize("outcome", ["ready", "failed", "skipped"])
def test_DISPATCH_is_ADMITTED_again_once_the_attempt_SETTLES(store, outcome):
    mid, a = _planned_then_replanning()
    if outcome == "ready":
        stored = missions.put_plan(
            mid, project_id=None, cwd="/repo", engine="codex", brief="B", generation=2
        )
    else:
        assert missions.settle_plan(mid, 2, outcome, detail="gave up") is True
        stored = a

    claimed = missions.claim_plan(mid, stored["plan_id"])
    assert claimed["plan_id"] == stored["plan_id"]
    assert missions.get_mission(mid)["state"] == "dispatching"


# ---- a project mutation cannot land between the final check and the plan commit (#974) ---------


#: Every barrier waits at most this long, so a regression that deadlocks fails instead of hanging.
BARRIER_S = 10.0


def _project_store_lock_is_held() -> bool:
    """Is the project store's flock held right now? A non-blocking probe on a fresh descriptor.

    `flock` is per open file description, so this probe contends with a holder in this same
    process exactly as another instance would.
    """
    fd = os.open(os.environ["AGENT_SESSIONS_PROJECTS"], os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class _MutationInTheWindow:
    """Launch a project archive or delete EXACTLY in the window the review found.

    `missions.put_plan` is wrapped, so the mutation starts after the final project resolution and
    before the plan's commit. The wrapper probes the store lock BEFORE the mutation exists, so the
    probe can only see a fence the code under test is holding:

    * no fence: nothing stops the mutation, so the wrapper waits (bounded) for it to LAND and only
      then lets the plan commit — the bad interleaving, forced deterministically;
    * a fence: the mutation cannot land until the fence is released, which is after the commit.

    `order` records what committed first. No sleeps: every wait is an event with a timeout.
    """

    def __init__(self, monkeypatch, project_id: str, change: str) -> None:
        self.order: list[str] = []
        self.errors: list[BaseException] = []
        self.thread: threading.Thread | None = None
        entering = threading.Event()
        landed = threading.Event()
        real = missions.put_plan

        def mutate() -> None:
            entering.set()
            try:
                if change == "archive":
                    projects.update(project_id, archived=True)
                else:
                    projects.delete(project_id)
            except BaseException as e:  # noqa: BLE001 — reported by `join`
                self.errors.append(e)
            finally:
                self.order.append("mutation")
                landed.set()

        def wrapped(*a, **kw):
            fenced = _project_store_lock_is_held()
            self.thread = threading.Thread(target=mutate, name="project-mutation", daemon=True)
            self.thread.start()
            assert entering.wait(BARRIER_S), "the mutation thread never started"
            if not fenced:
                assert landed.wait(BARRIER_S), "an unfenced mutation did not land"
            out = real(*a, **kw)
            self.order.append("plan")
            return out

        monkeypatch.setattr(missions, "put_plan", wrapped)

    def join(self) -> None:
        assert self.thread is not None, "the window was never reached"
        self.thread.join(BARRIER_S)
        assert not self.thread.is_alive(), "the project mutation never finished: a deadlock"
        assert self.errors == [], self.errors


def _assert_never_ready_against_a_gone_project(window, mission_id, project_id, *, landed: bool):
    """Either the mutation waited and the plan landed before it, or the plan was refused."""
    row = missions.get_mission(mission_id)
    stored = missions.get_plan(mission_id)
    if landed:
        assert window.order == ["plan", "mutation"], (
            f"the project changed after the final check and the plan committed against it anyway "
            f"(order {window.order})"
        )
        assert row["plan_state"] == "ready"
        assert stored is not None and stored["project_id"] == project_id
    else:
        assert stored is None, "a refused plan still wrote a row"
        assert row["plan_state"] != "ready"


@pytest.mark.parametrize("change", ["archive", "delete"])
def test_the_PLANNER_never_commits_a_ready_plan_against_a_project_ARCHIVED_or_DELETED_in_the_window(
    store, monkeypatch, change
):
    """#974 review, the blocking finding: the re-read before `put_plan` was a check, not an
    exclusion, so an archive or delete landing after it still got a `ready` plan.

    Red on 5031ed0 (and with the fence removed): the mutation lands first and the plan commits.
    """
    alpha = _project(store, "alpha")
    mid = _mission(project_id=alpha.id, cwd=alpha.default_folder)
    _configured(monkeypatch)
    _model(monkeypatch)
    window = _MutationInTheWindow(monkeypatch, alpha.id, change)

    async def drive():
        return await asyncio.wait_for(mission_plan.propose(mid), BARRIER_S * 2)

    try:
        asyncio.run(drive())
        landed = True
    except mission_plan.PlanError:
        landed = False
    window.join()
    _assert_never_ready_against_a_gone_project(window, mid, alpha.id, landed=landed)
    if not landed:
        assert missions.get_mission(mid)["plan_state"] == "failed"


@pytest.mark.parametrize("change", ["archive", "delete"])
def test_a_MANUAL_first_plan_never_commits_against_a_project_ARCHIVED_or_DELETED_in_the_window(
    api, monkeypatch, change
):
    """The same window in `PATCH /plan`'s first-plan path: `_resolve_cwd` then `put_plan`.

    Red on 5031ed0 (and with the fence removed): 200, `ready`, and a plan naming a project that
    was archived or deleted before it committed.
    """
    c, hdr, proj = api
    _not_configured(monkeypatch)
    m = c.post("/api/missions", json={"instruction": "ship it"}, headers=hdr).json()
    assert missions.get_mission(m["id"])["plan_state"] == "skipped"
    window = _MutationInTheWindow(monkeypatch, proj.id, change)

    r = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"project_id": proj.id, "engine": "claude", "brief": "do it"},
        headers=hdr,
    )
    window.join()
    assert r.status_code in (200, 409, 422), r.text
    _assert_never_ready_against_a_gone_project(
        window, m["id"], proj.id, landed=r.status_code == 200
    )


def test_NO_route_takes_the_project_store_lock_ON_THE_EVENT_LOOP(api, monkeypatch):
    """The plan write now holds the project store's flock through a SQLite COMMIT, which can wait
    seconds under contention. A route taking that flock ON THE LOOP would freeze every terminal
    WebSocket for as long (#678's shape). So every acquisition is recorded with whether the
    thread taking it is running an event loop.

    Red against project routes that call `projects.create` / `update` / `delete` inline in an
    `async def`.
    """
    import contextlib

    c, hdr, proj = api
    real = projects._exclusive
    seen: list[tuple[str, bool]] = []

    @contextlib.contextmanager
    def spy(path):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((threading.current_thread().name, on_loop))
        with real(path) as fh:
            yield fh

    monkeypatch.setattr(projects, "_exclusive", spy)
    _not_configured(monkeypatch)

    folder = proj.default_folder + "-two"
    os.makedirs(folder, exist_ok=True)
    made = c.post("/api/projects", json={"name": "two", "folders": [folder]}, headers=hdr)
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    assert c.patch(f"/api/projects/{pid}", json={"name": "renamed"}, headers=hdr).status_code == 200
    assert c.post(f"/api/projects/{pid}/archive", headers=hdr).status_code == 200
    assert c.post(f"/api/projects/{pid}/unarchive", headers=hdr).status_code == 200
    assert c.delete(f"/api/projects/{pid}", headers=hdr).status_code == 200
    assert c.get("/api/sessions", headers=hdr).status_code == 200

    m = c.post("/api/missions", json={"instruction": "ship it"}, headers=hdr).json()
    r = c.patch(
        f"/api/missions/{m['id']}/plan",
        json={"project_id": proj.id, "engine": "claude", "brief": "do it"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text

    assert len(seen) >= 6, seen
    on_loop = [name for name, loop in seen if loop]
    assert on_loop == [], f"the project store lock was taken on the event loop: {seen}"


# ---- recovery reports what happened ------------------------------------------------------------


def test_RECOVERY_reports_each_mission_by_its_OUTCOME_not_by_having_returned(store, monkeypatch):
    """`propose_for_new_mission` never raises, so "it returned" was counted as "recovered" even
    when it had swallowed an error or deferred to a running attempt (#974 review).

    Red against the old report: the broken and busy missions came back as recovered.
    """
    _configured(monkeypatch)
    _model(monkeypatch)
    ok, broken, busy = _mission(), _mission(), _mission()
    real_resume = mission_plan._resume

    async def resume(mission_id, **kw):
        if mission_id == broken:
            raise RuntimeError("the store went away mid-resume")
        return await real_resume(mission_id, **kw)

    monkeypatch.setattr(mission_plan, "_resume", resume)

    async def drive():
        async with aitasks.single_flight(mission_plan.KIND, scope=busy):
            return await mission_plan.recover_pending()

    out = asyncio.run(drive())
    assert out.get("recovered") == [ok], out
    assert out.get("failed") == [broken], out
    assert out.get("already_running") == [busy], out
    assert missions.get_mission(ok)["plan_state"] == "ready"
    assert missions.get_mission(busy)["plan_state"] == "pending", "a deferred attempt was settled"


def test_RECOVERY_retries_a_TRANSIENT_worklist_read_and_reports_giving_up(store, monkeypatch):
    """One busy-database blip at boot used to end the pass with work still pending (#974 review).

    Red against a pass that returns on the first worklist error.
    """
    _configured(monkeypatch)
    _model(monkeypatch)
    mid = _mission()
    monkeypatch.setattr(mission_plan, "WORKLIST_BACKOFF_S", 0.0, raising=False)
    real = missions.missions_awaiting_plan
    calls = {"n": 0, "fail_first": 2}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] <= calls["fail_first"]:
            raise sqlite3.OperationalError("database is locked")
        return real(**kw)

    monkeypatch.setattr(missions, "missions_awaiting_plan", flaky)
    out = asyncio.run(mission_plan.recover_pending())
    assert out.get("recovered") == [mid], out
    assert calls["n"] >= 3, "the failed reads were not retried"
    assert "worklist_error" not in out
    assert missions.get_mission(mid)["plan_state"] == "ready"

    # …and a store that never answers ends the pass after a BOUNDED number of reads, saying so.
    other = _mission()
    calls.update(n=0, fail_first=10**6)
    out = asyncio.run(mission_plan.recover_pending())
    assert calls["n"] == 3, calls
    assert "database is locked" in out.get("worklist_error", ""), out
    assert missions.get_mission(other)["plan_state"] == "pending"


def _supersede_on_settle(monkeypatch):
    """Make the FIRST `settle_plan` lose the generation fence for real: a newer attempt takes the
    mission (another process's Plan again) just before the settlement commits, and the real
    `settle_plan` then discards it with its own `discarded` event. Not a stub returning False."""
    real = missions.settle_plan
    fired = {"n": 0}

    def settle(mission_id, generation, state, **kw):
        if not fired["n"]:
            fired["n"] += 1
            con = sqlite3.connect(missions._db_path())
            con.execute(
                "UPDATE missions SET plan_generation = plan_generation + 1 WHERE id=?",
                (mission_id,),
            )
            con.commit()
            con.close()
        return real(mission_id, generation, state, **kw)

    monkeypatch.setattr(missions, "settle_plan", settle)
    return fired


def _unwritable_settle(monkeypatch):
    def settle(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(missions, "settle_plan", settle)


@pytest.mark.parametrize(
    ("case", "bucket", "plan_state"),
    [
        # The control: a skip that LANDED is this pass's outcome.
        ("landed_skip", "recovered", "skipped"),
        # Red against `_settle` dropping `settle_plan`'s bool: the no-endpoint branch reported
        # `skipped`, so a settlement the fence DISCARDED was counted as recovered.
        ("discarded_skip", "unchanged", "pending"),
        # …and the failure path the same way: a discarded `failed` is not this pass's failure.
        ("discarded_failure", "unchanged", "pending"),
        # A settlement that could not be WRITTEN leaves the intent pending. Not recovered.
        ("unwritable_skip", "failed", "pending"),
    ],
)
def test_RECOVERY_reports_a_settlement_by_whether_it_LANDED(
    store, monkeypatch, case, bucket, plan_state
):
    """`recovered` only when this pass's settlement actually landed (#974 review, #967 P2b)."""
    mid = _mission()
    _restart()
    if case == "discarded_failure":
        _configured(monkeypatch)
        model = _model(monkeypatch, raises=RuntimeError("endpoint returned HTTP 500"))
    else:
        _not_configured(monkeypatch)
        model = _model(monkeypatch, raises=AssertionError("no endpoint, no call"))
    if case.startswith("discarded"):
        fired = _supersede_on_settle(monkeypatch)
    elif case == "unwritable_skip":
        _unwritable_settle(monkeypatch)

    out = asyncio.run(mission_plan.recover_pending())

    buckets = {k: v for k, v in out.items() if isinstance(v, list) and v}
    assert buckets == {bucket: [mid]}, out
    row = missions.get_mission(mid)
    assert row["plan_state"] == plan_state
    assert missions.get_plan(mid) is None
    if case == "discarded_failure":
        assert model.calls == 1
    if case.startswith("discarded"):
        assert fired["n"] == 1
        (ev,) = _events(mid, "planning")
        assert ev["meta"]["outcome"] == "discarded", ev
        assert row["plan_generation"] == 2


# ---- plan manually: the options it chooses from -------------------------------------------------


def test_the_DETAIL_of_an_UNPLANNED_mission_carries_the_options_PLAN_MANUALLY_chooses_from(
    api, monkeypatch
):
    """With no plan the lists rode nowhere, so the card had nothing to offer (#967 P2b).

    Red against the detail route before the change: `plan_options` is absent for a skipped
    mission. Offered exactly where `PATCH /plan` accepts a first plan, and from the builders that
    validate it — so every offered choice is one the save accepts.
    """
    c, hdr, proj = api
    _not_configured(monkeypatch)
    m = c.post("/api/missions", json={"instruction": "ship it"}, headers=hdr).json()
    detail = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert (detail["plan_state"], detail["plan"]) == ("skipped", None)
    opts = detail["plan_options"]
    assert [p["id"] for p in opts["project_options"]] == [proj.id]
    assert [e["id"] for e in opts["engine_options"]] == [e["id"] for e in ENGINES]

    # …the first plan saved from them is accepted, and the lists move onto the plan.
    choice = {
        "project_id": opts["project_options"][0]["id"],
        "engine": opts["engine_options"][0]["id"],
        "brief": "by hand",
    }
    r = c.patch(f"/api/missions/{m['id']}/plan", json=choice, headers=hdr)
    assert r.status_code == 200, r.text
    detail = c.get(f"/api/missions/{m['id']}", headers=hdr).json()
    assert "plan_options" not in detail
    assert detail["plan"]["project_options"] and detail["plan"]["engine_options"]

    # A PENDING mission is not offered them: a first plan is refused there.
    pending = _mission()
    detail = c.get(f"/api/missions/{pending}", headers=hdr).json()
    assert detail["plan_state"] == "pending"
    assert "plan_options" not in detail


# ---- the upgrade --------------------------------------------------------------------------


def test_the_v25_UPGRADE_backfills_READY_or_SKIPPED_and_queues_NOTHING(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    with_plan = _mission()
    missions.put_plan(with_plan, project_id=None, cwd="/repo", engine="claude", brief="go")
    without = _mission()
    fresh_cols = {
        t: [r[1] for r in sqlite3.connect(db).execute(f"PRAGMA table_info({t})")]
        for t in ("missions", "mission_plans")
    }

    con = sqlite3.connect(db)
    # A REAL v24 store: the v25 plan columns go, and so does every `missions` column a LATER
    # migration adds (v31's `auto_choose`, #1060) — a v24 file cannot have them, and leaving one in
    # would put it ahead of the plan columns the upgrade appends.
    con.execute("DROP TRIGGER IF EXISTS automation_owner_menu")
    for col in ("plan_state", "plan_generation", "plan_at", "plan_detail", "auto_choose"):
        con.execute(f"ALTER TABLE missions DROP COLUMN {col}")
    con.execute("ALTER TABLE mission_plans DROP COLUMN generation")
    con.execute("PRAGMA user_version=24")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()

    assert missions.get_mission(with_plan)["plan_state"] == "ready"
    assert missions.get_mission(without)["plan_state"] == "skipped"
    assert _pending() == [], "the upgrade queued historical missions for planning"
    up = sqlite3.connect(db)
    assert up.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION
    # A fresh install and an upgraded one agree on the columns and their order.
    for t, cols in fresh_cols.items():
        assert [r[1] for r in up.execute(f"PRAGMA table_info({t})")] == cols
    up.close()
    assert _pending() == [] and _mission() in _pending()
