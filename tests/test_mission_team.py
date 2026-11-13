"""HQ plans the team (#1950, #1613 P5): `qualify()`, the assembler, the envelope and the ops.

The acceptance scenario is the operator's own application project as a SYNTHETIC fixture
(`mission_team_fixture`: same flow, roles and pool, no personal content). Pure functions are
tested over a hand-built `Context`; the route tests run the real store and app, with only the
context snapshot (the pool, grades and client facts) replaced by the same fixture.
"""

from __future__ import annotations

import copy
import json
import random
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import mission_plan_document as plan_doc
from agent_sessions import mission_team as team
from agent_sessions import missions
from agent_sessions.main import create_app
from mission_team_fixture import (
    ENGINE,
    FLOW,
    client,
    context,
    document,
    pool,
    spec,
    store_plan,
)

AGENT_STEPS = ["o1.posting", "o1.cv", "o1.cover_letter", "o1.documents", "o1.review"]
AGENT_STEPS += ["o1.pull_request", "o1.record"]


def _team(doc, ctx, existing=None):
    out = team.assemble(doc, existing or team.assignments_of(doc), ctx)
    return {k: v["specialist"]["id"] for k, v in out["assignments"].items()}, out


def _with(doc, assignments):
    new = copy.deepcopy(doc)
    for s in new["steps"]:
        if s["id"] in assignments:
            s["envelope"]["assignment"] = assignments[s["id"]]
    return new


def _assembled(ctx=None):
    doc = document()
    ctx = ctx or context()
    _, out = _team(doc, ctx)
    return _with(doc, out["assignments"]), ctx


def _op(op, **kw):
    body = {
        "op": op,
        "operation_id": str(uuid.uuid4()),
        "expected_revision": 1,
        "reason": "test",
        **kw,
    }
    return team.parse_op(body)


def _index_of(rows, **match):
    return next(r["index"] for r in rows if all(r[k] == v for k, v in match.items()))


# ---- the acceptance scenario ---------------------------------------------------------------------


def test_the_application_team_is_exactly_the_one_the_operator_expects():
    picked, out = _team(document(), context())
    assert picked == {
        "o1.posting": "career-archivist",
        "o1.cv": "cv-writer",
        "o1.cover_letter": "cv-writer",
        "o1.documents": "career-archivist",
        "o1.review": "application-reviewer",
        "o1.pull_request": "career-archivist",
        "o1.record": "career-archivist",
    }
    assert out["unassigned"] == {}
    a = out["assignments"]
    assert a["o1.posting"]["reason"] == "only specialist with career-archive; Specialist ≥ Intern"
    assert a["o1.cv"]["reason"] == "only specialist with cv-writing; Specialist ≥ Specialist"
    assert a["o1.review"]["reason"].startswith(
        "only specialist with application-review; Senior ≥ Senior; distinct from o1.cv"
    )
    # Operator, external and note steps get no specialist.
    assert "o1.intake" not in a and "o1.approve" not in a and "o1.submit" not in a


def test_the_assembled_document_is_one_the_store_accepts():
    doc, _ = _assembled()
    plan_doc.validate_document(doc)
    for a in team.assignments_of(doc).values():
        if a is not None:
            assert '"grade"' not in json.dumps(a), "a grade is derived at read, never stored"


# ---- the override matrix (the downgrade regression), pure ----------------------------------------


def _override(doc, ctx, step, model, effort=None):
    lists = team.override_choices(
        team._step(doc, step), doc, team.assignments_of(doc), ctx, mission_id="m", revision=1
    )
    idx = _index_of(lists["choices"], engine=ENGINE, model=model, effort=effort)
    op = _op("set_override", step=step, index=idx, listing=lists["listing"])
    return team.apply_op(doc, op, ctx, mission_id="m", revision=1)


def test_the_review_on_sonnet_is_refused_as_a_downgrade():
    """THE DOWNGRADE REGRESSION (#1950 acceptance): strong < frontier."""
    doc, ctx = _assembled()
    with pytest.raises(team.TeamRefused) as e:
        _override(doc, ctx, "o1.review", "sonnet")
    assert e.value.code == "grade_below_min"
    assert e.value.text == "sonnet is Specialist, below Senior, which application-review needs"


def test_the_cv_on_haiku_is_refused():
    doc, ctx = _assembled()
    with pytest.raises(team.TeamRefused) as e:
        _override(doc, ctx, "o1.cv", "haiku")
    assert e.value.code == "grade_below_min"
    assert "Intern, below Specialist" in e.value.text


def test_a_career_archive_step_on_haiku_is_accepted_and_the_reason_says_so():
    doc, ctx = _assembled()
    new = _override(doc, ctx, "o1.record", "haiku")
    a = team.assignments_of(new)["o1.record"]
    assert a["override"] == {"engine": ENGINE, "model": "haiku", "effort": None}
    assert "Intern ≥ Intern" in a["reason"]
    assert a["specialist"] == {"id": "career-archivist", "version": 1}
    plan_doc.validate_document(new)


def test_the_review_with_opus_ungraded_is_refused():
    doc, _ = _assembled()
    ctx = context(grades={ENGINE: {"sonnet": "strong", "haiku": "fast"}})
    with pytest.raises(team.TeamRefused) as e:
        _override(doc, ctx, "o1.review", "opus", "high")
    assert e.value.code == "ungraded"


def test_a_remap_after_a_write_reaches_the_view_and_not_the_stored_envelope():
    doc, ctx = _assembled()
    new = _override(doc, ctx, "o1.cv", "sonnet", "high")
    stored = json.dumps(new, sort_keys=True)
    ok = {s["id"]: s for s in team.view(new, ctx)["steps"]}["o1.cv"]["derived"]
    assert ok["ok"] and ok["grade"] == "strong"
    remapped = context(grades={ENGINE: {"opus": "frontier", "sonnet": "fast", "haiku": "fast"}})
    row = {s["id"]: s for s in team.view(new, remapped)["steps"]}["o1.cv"]["derived"]
    assert row["ok"] is False and row["grade"] == "fast"
    assert row["reasons"][0]["code"] == "grade_below_min"
    assert json.dumps(new, sort_keys=True) == stored, "the read never rewrites the revision"


# ---- negative assembly ---------------------------------------------------------------------------


def test_an_archived_reviewer_leaves_the_review_unassigned_never_filled_by_the_cv_writer():
    specs = pool(
        application_reviewer=spec(
            "application-reviewer", "opus", ["application-review"], archived=True
        )
    )
    # Give cv-writer the review skill on opus too, so ONLY the distinct constraint stops it.
    specs = [s for s in specs if s["id"] != "cv-writer"] + [
        spec("cv-writer", "opus", ["cv-writing", "application-review"], capacity=2)
    ]
    picked, out = _team(document(), context(specs=specs))
    assert "o1.review" not in picked
    assert picked["o1.cv"] == picked["o1.cover_letter"] == "cv-writer"
    why = out["unassigned"]["o1.review"]
    assert why["summary"] == (
        "no qualifying specialist: needs Senior + application-review, distinct from cv-writer"
    )
    codes = {c["specialist"]: [r["code"] for r in c["reasons"]] for c in why["candidates"]}
    assert codes["application-reviewer"] == ["specialist_archived"]
    assert codes["cv-writer"] == ["not_distinct", "not_distinct"]


def test_capacity_one_with_two_parallel_steps_assigns_both_and_says_it_will_queue():
    specs = pool(cv_writer=spec("cv-writer", "sonnet", ["cv-writing"], capacity=1))
    doc = document()
    _, out = _team(doc, context(specs=specs))
    a = out["assignments"]
    assert a["o1.cv"]["specialist"]["id"] == a["o1.cover_letter"]["specialist"]["id"]
    assert "will_queue" in a["o1.cv"]["notes"] and "will_queue" in a["o1.cover_letter"]["notes"]
    view = team.view(_with(doc, a), context(specs=specs))
    notes = {s["id"]: s.get("derived", {}).get("notes") for s in view["steps"]}
    assert {"code": "will_queue", "text": "will queue: 1 of 2 runs first"} in notes["o1.cv"]


# ---- every qualify() code ------------------------------------------------------------------------


def _verdict(ctx, sid="o1.cv", **spec_over):
    doc = document()
    s = team._step(doc, sid)
    base = spec("x", spec_over.pop("model", "sonnet"), spec_over.pop("skills", ["cv-writing"]))
    v = {**base["current"], "id": "x", "archived": spec_over.pop("archived", False)}
    v.update(spec_over)
    return [r["code"] for r in team.qualify(s, v, None, ctx)["reasons"]]


@pytest.mark.parametrize(
    "ctx_kw,spec_kw,code",
    [
        ({}, {"skills": ["career-archive"]}, "skill_missing"),
        ({"library": {"cv-writing": "r2"}}, {}, "skill_pin_stale"),
        ({"library": {}}, {}, "skill_pin_stale"),
        ({"clients": {ENGINE: client(skills=False)}}, {}, "engine_cannot_load_skills"),
        (
            {"clients": {ENGINE: client(ready=False, why="not ready")}},
            {},
            "engine_not_mission_ready",
        ),
        ({"clients": {}}, {}, "engine_not_mission_ready"),
        ({}, {"model": "sonnet-9"}, "model_not_offered"),
        ({"clients": {ENGINE: client(models=None)}}, {}, "model_not_offered"),
        ({}, {"effort": "max"}, "model_not_offered"),
        ({}, {"model": "haiku"}, "grade_below_min"),
        ({"grades": {}}, {}, "ungraded"),
        ({}, {"model": "default"}, "ungraded"),
        ({}, {"archived": True}, "specialist_archived"),
        ({"clients": {ENGINE: client(usage="exhausted")}}, {}, "usage_exhausted"),
    ],
)
def test_each_refusal_code(ctx_kw, spec_kw, code):
    assert code in _verdict(context(**ctx_kw), **spec_kw)


def test_unknown_usage_is_a_note_never_a_refusal():
    ctx = context(clients={ENGINE: client(usage="unknown")})
    assert _verdict(ctx) == []
    _, out = _team(document(), ctx)
    assert all("usage_unknown" in a["notes"] for a in out["assignments"].values())


def test_every_reason_code_is_in_the_closed_set():
    for ctx_kw in ({"grades": {}}, {"clients": {}}, {"library": {}}):
        for code in _verdict(context(**ctx_kw), skills=[], archived=True, model="zz"):
            assert code in team.CODES


# ---- distinct_from, solved jointly ---------------------------------------------------------------

TWO = """format = 5
title = "Two"

[[steps]]
id = "a"
title = "A"
actor = { kind = "agent" }
role = { skills = ["s"] }
[[steps.checklist]]
key = "a_ok"
title = "a"
probe = "supervisor_judged"

[[steps]]
id = "b"
title = "B"
actor = { kind = "agent" }
role = { skills = ["t"] }
distinct_from = [{ step = "a", constraint = "CONSTRAINT" }]
[[steps.checklist]]
key = "b_ok"
title = "b"
probe = "supervisor_judged"
"""


def test_greedy_failure_is_rescued_by_the_joint_search():
    """`x` is cheaper for A, but only `x` can do B, which must differ from A: plan order alone
    would give A to `x` and leave B empty. The joint search gives A to `y`."""
    doc = document(TWO.replace("CONSTRAINT", "specialist"))
    specs = [
        spec("x", "haiku", ["s", "t"]),
        spec("y", "sonnet", ["s"]),
    ]
    picked, out = _team(doc, context(specs=specs, library={"s": "r1", "t": "r1"}))
    assert picked == {"o1.a": "y", "o1.b": "x"}
    assert out["unassigned"] == {}


def test_an_unsatisfiable_constraint_leaves_the_step_unassigned():
    doc = document(TWO.replace("CONSTRAINT", "specialist"))
    specs = [spec("x", "haiku", ["s", "t"])]
    picked, out = _team(doc, context(specs=specs, library={"s": "r1", "t": "r1"}))
    assert picked == {"o1.a": "x"}
    assert [r["code"] for r in out["unassigned"]["o1.b"]["candidates"][0]["reasons"]] == [
        "not_distinct"
    ]


def test_an_override_that_makes_a_model_distinct_pair_equal_is_not_distinct():
    doc = document(TWO.replace("CONSTRAINT", "model"))
    ctx = context(
        specs=[spec("x", "haiku", ["s"]), spec("y", "sonnet", ["t"])],
        library={"s": "r1", "t": "r1"},
    )
    _, out = _team(doc, ctx)
    doc = _with(doc, out["assignments"])
    with pytest.raises(team.TeamRefused) as e:
        _override(doc, ctx, "o1.b", "haiku")
    assert e.value.code == "not_distinct"


# ---- the cheapest order --------------------------------------------------------------------------


def test_the_lowest_grade_that_qualifies_wins():
    specs = pool() + [spec("intern", "haiku", ["career-archive"], capacity=8)]
    picked, out = _team(document(), context(specs=specs))
    assert picked["o1.posting"] == "intern"
    alts = out["assignments"]["o1.posting"]["alternates"]
    assert [x["specialist"]["id"] for x in alts] == ["career-archivist"]
    assert alts[0]["reason"].endswith("a higher grade than needed")


def test_more_headroom_then_the_id_breaks_ties():
    specs = [
        spec("b-wide", "sonnet", ["s"], capacity=3),
        spec("a-narrow", "sonnet", ["s"], capacity=1),
        spec("c-wide", "sonnet", ["s"], capacity=3),
    ]
    doc = document(
        TWO.replace('role = { skills = ["t"] }', 'role = { skills = ["s"] }').replace(
            'distinct_from = [{ step = "a", constraint = "CONSTRAINT" }]\n', ""
        )
    )
    picked, out = _team(doc, context(specs=specs, library={"s": "r1"}))
    # b-wide and c-wide tie on grade and headroom; b < c by id. After A takes b-wide, its
    # headroom is 2 and c-wide's still 3, so B goes to c-wide.
    assert picked == {"o1.a": "b-wide", "o1.b": "c-wide"}
    alts = out["assignments"]["o1.a"]["alternates"]
    assert [a["specialist"]["id"] for a in alts] == ["c-wide", "a-narrow"]


def test_a_shuffled_pool_gives_the_same_team():
    specs = pool() + [
        spec("intern", "haiku", ["career-archive"], capacity=1),
        spec("senior-writer", "opus", ["cv-writing", "application-review"], capacity=2),
        spec("writer-2", "sonnet", ["cv-writing"], capacity=2),
    ]
    doc = document()
    first = team.assemble(doc, team.assignments_of(doc), context(specs=specs))
    rng = random.Random(1950)
    for _ in range(12):
        shuffled = specs[:]
        rng.shuffle(shuffled)
        again = team.assemble(doc, team.assignments_of(doc), context(specs=shuffled))
        assert again == first


# ---- capacity: the antichain metric --------------------------------------------------------------


@pytest.mark.parametrize(
    "edges,width",
    [
        ({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}, 2),  # diamond
        ({"a": [], "b": ["a"], "c": ["b"], "d": ["c"]}, 1),  # chain
        ({"a": [], "b": ["a"], "c": ["a"], "d": ["a"]}, 3),  # fan-out
        ({"a": [], "b": [], "c": ["a"], "d": ["b"]}, 2),  # two chains
    ],
)
def test_peak_is_the_width_of_the_dependency_order(edges, width):
    def closure(n):
        out = set()
        for p in edges[n]:
            out |= {p} | closure(p)
        return out

    before = {n: closure(n) for n in edges}
    assert team.peak(list(edges), before) == width


def test_objective_order_counts_and_rework_edges_do_not():
    doc = document(objectives=2)
    doc["objectives"][1]["after"] = ["o1"]
    before = team.order_relation(doc)
    assert "o1.record" in before["o2.posting"]  # every step of o2 waits for all of o1
    # The review's rework edge (→ cv) never makes cv come after the review.
    assert "o1.review" not in before["o1.cv"]
    assert team.peak(["o1.cv", "o1.cover_letter", "o2.cv"], before) == 2


def test_the_pool_limit_is_a_note_never_a_refusal():
    doc, _ = _assembled()
    view = team.view(doc, context(pool_max=1))
    assert view["peak"] == 2 and view["pool_note"] == "will queue: 1 of 2 runs first"


# ---- the node budget -----------------------------------------------------------------------------


def test_past_the_node_budget_the_assembler_refuses_and_solves_nothing(monkeypatch):
    monkeypatch.setattr(team, "ASSEMBLY_NODE_BUDGET", 3)
    with pytest.raises(team.TeamRefused) as e:
        team.assemble(document(), team.assignments_of(document()), context())
    assert e.value.code == "assembly_too_large"


# ---- the envelope --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(grade="strong"),
        lambda a: a.update(specialist={"id": "X Y", "version": 1}),
        lambda a: a.update(specialist={"id": "x", "version": 0}),
        lambda a: a.update(override={"engine": ENGINE, "model": "default", "effort": None}),
        lambda a: a.update(override={"engine": ENGINE, "model": "opus"}),
        lambda a: a.update(reason="two\nlines"),
        lambda a: a.update(notes=["cheap"]),
        lambda a: a.update(notes=["will_queue", "will_queue"]),
        lambda a: a.update(alternates=[{"specialist": a["specialist"], "reason": "self"}]),
        lambda a: a.update(
            alternates=[
                {"specialist": {"id": f"s{i}", "version": 1}, "reason": "r"} for i in range(4)
            ]
        ),
    ],
)
def test_the_validator_refuses_a_malformed_assignment(mutate):
    doc, _ = _assembled()
    a = doc["steps"][2]["envelope"]["assignment"]
    mutate(a)
    with pytest.raises(plan_doc.PlanError):
        plan_doc.validate_document(doc)


def test_an_operator_step_is_never_assigned():
    doc, _ = _assembled()
    doc["steps"][0]["envelope"]["assignment"] = copy.deepcopy(
        doc["steps"][1]["envelope"]["assignment"]
    )
    with pytest.raises(plan_doc.PlanError, match="only an agent step"):
        plan_doc.validate_document(doc)


# ---- the operation parser: index discipline ------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"op": "set_override", "step": "o1.cv", "index": "sonnet", "listing": "sha256:" + "0" * 32},
        {"op": "set_override", "step": "o1.cv", "model": "sonnet"},
        {"op": "assign", "step": "o1.cv", "index": 0, "listing": "x"},
        {"op": "assign", "step": "../etc", "index": 0, "listing": "sha256:" + "0" * 32},
        {"op": "assemble", "engine": ENGINE},
        {"op": "approve"},
        {"op": "assemble", "author": "lead"},
    ],
)
def test_model_written_text_never_becomes_a_target(body):
    full = {"operation_id": str(uuid.uuid4()), "expected_revision": 1, "reason": "r", **body}
    with pytest.raises(team.TeamRefused) as e:
        team.parse_op(full)
    assert e.value.status == 422


# ---- the store and the route ---------------------------------------------------------------------


@pytest.fixture
def app(auth_cfg, monkeypatch):
    missions.reset_schema_cache_for_test()
    state = {"ctx": context()}
    monkeypatch.setattr(team, "snapshot", lambda: state["ctx"])
    c = TestClient(create_app(auth_cfg), base_url="https://testserver")
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": auth_cfg.origin},
    )
    assert r.status_code == 303
    csrf = c.get("/api/config").json()["csrf"]
    hdr = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    mid = missions.create_mission("apply for the job", title="Apply")["id"]
    store_plan(mid, document())
    yield {"c": c, "hdr": hdr, "mid": mid, "state": state, "cfg": auth_cfg}
    missions.reset_schema_cache_for_test()


def _post(app, mid=None, **body):
    full = {"operation_id": str(uuid.uuid4()), "reason": "plan the team", **body}
    return app["c"].post(
        f"/api/missions/{mid or app['mid']}/plan/ops", json=full, headers=app["hdr"]
    )


def _revisions(mid):
    con = missions._ready(None)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM mission_plan_revisions WHERE mission_id=?", (mid,)
        ).fetchone()[0]
    finally:
        con.close()


def _choices(app, step, mid=None):
    r = app["c"].get(f"/api/missions/{mid or app['mid']}/plan/team/choices", params={"step": step})
    assert r.status_code == 200, r.text
    return r.json()


def test_assemble_through_the_route_writes_one_revision(app):
    r = _post(app, op="assemble", expected_revision=1)
    assert r.status_code == 200, r.text
    assert r.json() == {"revision": 2, "replayed": False}
    view = app["c"].get(f"/api/missions/{app['mid']}/plan/team").json()
    assert view["revision"] == 2
    rows = {s["id"]: s for s in view["team"]["steps"]}
    assert rows["o1.review"]["assignment"]["specialist"]["id"] == "application-reviewer"
    assert rows["o1.review"]["derived"]["ok"] is True


@pytest.mark.parametrize("author", ["operator", "hq"])
@pytest.mark.parametrize(
    "step,model,effort,code",
    [
        ("o1.review", "sonnet", None, "grade_below_min"),  # THE downgrade regression
        ("o1.cv", "haiku", None, "grade_below_min"),
        ("o1.record", "haiku", None, None),  # accepted: fast ≥ fast
    ],
)
def test_the_override_matrix_from_both_authors(app, author, step, model, effort, code):
    assert _post(app, op="assemble", expected_revision=1).status_code == 200
    lists = _choices(app, step)["overrides"]
    idx = _index_of(lists["choices"], engine=ENGINE, model=model, effort=effort)
    r = _post(
        app,
        op="set_override",
        step=step,
        index=idx,
        listing=lists["listing"],
        expected_revision=2,
        author=author,
    )
    if code is None:
        assert r.status_code == 200, r.text
        assert _revisions(app["mid"]) == 3
        rev = missions.plan_revision(app["mid"])
        assert rev["author_kind"] == author
    else:
        assert r.status_code == 422
        assert r.json()["code"] == code
        assert _revisions(app["mid"]) == 2, "a refused override writes no revision"


def test_ungraded_and_a_later_remap_through_the_route(app):
    assert _post(app, op="assemble", expected_revision=1).status_code == 200
    app["state"]["ctx"] = context(grades={ENGINE: {"sonnet": "strong", "haiku": "fast"}})
    lists = _choices(app, "o1.review")["overrides"]
    idx = _index_of(lists["choices"], engine=ENGINE, model="opus", effort=None)
    r = _post(
        app,
        op="set_override",
        step="o1.review",
        index=idx,
        listing=lists["listing"],
        expected_revision=2,
    )
    assert r.status_code == 422 and r.json()["code"] == "ungraded"
    # Sonnet re-mapped to fast after the team was written: the view says so, nothing stored moves.
    stored = missions.plan_revision(app["mid"])["document"]
    app["state"]["ctx"] = context(grades={ENGINE: {"opus": "frontier", "sonnet": "fast"}})
    view = app["c"].get(f"/api/missions/{app['mid']}/plan/team").json()
    cv = {s["id"]: s for s in view["team"]["steps"]}["o1.cv"]["derived"]
    assert cv["ok"] is False and cv["reasons"][0]["code"] == "grade_below_min"
    assert missions.plan_revision(app["mid"])["document"] == stored


def test_an_index_that_was_not_offered_is_a_422(app):
    _post(app, op="assemble", expected_revision=1)
    lists = _choices(app, "o1.record")["overrides"]
    r = _post(
        app,
        op="set_override",
        step="o1.record",
        index=len(lists["choices"]),
        listing=lists["listing"],
        expected_revision=2,
    )
    assert r.status_code == 422 and r.json()["code"] == "not_offered"
    assert _revisions(app["mid"]) == 2


def test_a_listing_from_another_mission_is_refused(app):
    other = missions.create_mission("another", title="Other")["id"]
    store_plan(other, document())
    _post(app, op="assemble", expected_revision=1)
    _post(app, mid=other, op="assemble", expected_revision=1)
    theirs = _choices(app, "o1.record", mid=other)["overrides"]
    r = _post(
        app,
        op="set_override",
        step="o1.record",
        index=0,
        listing=theirs["listing"],
        expected_revision=2,
    )
    assert r.status_code == 409 and r.json()["code"] == "listing_changed"
    r = _post(app, op="clear_override", step="o9.nope", expected_revision=2)
    assert r.status_code == 422


def test_a_stale_revision_is_409_with_the_latest(app):
    assert _post(app, op="assemble", expected_revision=1).status_code == 200
    r = _post(app, op="assemble", expected_revision=1)
    assert r.status_code == 409 and r.json()["latest_revision"] == 2


def test_a_repeated_operation_id_replays_and_a_reused_one_conflicts(app):
    oid = str(uuid.uuid4())
    first = _post(app, op="assemble", expected_revision=1, operation_id=oid)
    again = _post(app, op="assemble", expected_revision=1, operation_id=oid)
    assert first.json() == {"revision": 2, "replayed": False}
    assert again.status_code == 200 and again.json() == {"revision": 2, "replayed": True}
    assert _revisions(app["mid"]) == 2
    # Same id, another payload / author / mission: a conflict, never a replay.
    assert _post(app, op="assemble", expected_revision=2, operation_id=oid).status_code == 409
    r = _post(app, op="assemble", expected_revision=1, operation_id=oid, author="hq")
    assert r.status_code == 409
    other = missions.create_mission("another", title="Other")["id"]
    store_plan(other, document())
    r = _post(app, mid=other, op="assemble", expected_revision=1, operation_id=oid)
    assert r.status_code == 409


def test_assign_by_index_and_a_refused_candidate_is_refused(app):
    lists = _choices(app, "o1.review")["candidates"]
    ids = [c["specialist"]["id"] for c in lists["choices"]]
    assert (
        ids == sorted(ids, key=lambda i: {"career-archivist": 1, "cv-writer": 1}.get(i, 0)) or ids
    )
    bad = _index_of(lists["choices"], ok=False)
    r = _post(
        app, op="assign", step="o1.review", index=bad, listing=lists["listing"], expected_revision=1
    )
    assert r.status_code == 422 and _revisions(app["mid"]) == 1
    good = next(
        c["index"] for c in lists["choices"] if c["specialist"]["id"] == "application-reviewer"
    )
    r = _post(
        app,
        op="assign",
        step="o1.review",
        index=good,
        listing=lists["listing"],
        expected_revision=1,
    )
    assert r.status_code == 200, r.text
    a = missions.plan_revision(app["mid"])["document"]["steps"][5]["envelope"]["assignment"]
    assert a["reason"].startswith("chosen from 1 that qualify")


def test_a_dispatched_mission_takes_no_team_change(app):
    con = missions._ready(None)
    con.execute("UPDATE missions SET state='running', cwd='/tmp' WHERE id=?", (app["mid"],))
    con.commit()
    con.close()
    r = _post(app, op="assemble", expected_revision=1)
    assert r.status_code == 409 and _revisions(app["mid"]) == 1


def test_the_routes_need_login_and_csrf(app):
    fresh = TestClient(create_app(app["cfg"]), base_url="https://testserver")
    mid = app["mid"]
    assert fresh.get(f"/api/missions/{mid}/plan/team").status_code == 401
    assert fresh.get(f"/api/missions/{mid}/plan/team/choices?step=o1.cv").status_code == 401
    body = {
        "op": "assemble",
        "operation_id": str(uuid.uuid4()),
        "expected_revision": 1,
        "reason": "r",
    }
    assert fresh.post(f"/api/missions/{mid}/plan/ops", json=body).status_code in (401, 403)
    no_csrf = app["c"].post(
        f"/api/missions/{mid}/plan/ops", json=body, headers={"Origin": app["cfg"].origin}
    )
    assert no_csrf.status_code in (401, 403)
    assert _revisions(mid) == 1


def test_past_the_budget_the_route_writes_nothing(app, monkeypatch):
    monkeypatch.setattr(team, "ASSEMBLY_NODE_BUDGET", 2)
    r = _post(app, op="assemble", expected_revision=1)
    assert r.status_code == 422 and r.json()["code"] == "assembly_too_large"
    assert _revisions(app["mid"]) == 1


def test_a_restart_mid_planning_keeps_every_committed_revision(app):
    _post(app, op="assemble", expected_revision=1)
    lists = _choices(app, "o1.record")["overrides"]
    idx = _index_of(lists["choices"], engine=ENGINE, model="haiku", effort=None)
    _post(
        app,
        op="set_override",
        step="o1.record",
        index=idx,
        listing=lists["listing"],
        expected_revision=2,
    )
    missions.reset_schema_cache_for_test()
    db = missions._db_path()
    con = sqlite3.connect(db)
    revs = [
        r[0]
        for r in con.execute(
            "SELECT revision FROM mission_plan_revisions WHERE mission_id=? ORDER BY revision",
            (app["mid"],),
        )
    ]
    ops = con.execute("SELECT COUNT(*) FROM mission_plan_ops").fetchone()[0]
    con.close()
    assert revs == [1, 2, 3] and ops == 2
    assert missions.plan_revision(app["mid"])["revision"] == 3


def test_applying_another_procedure_keeps_the_team(app, monkeypatch):
    _post(app, op="assemble", expected_revision=1)
    before = missions.plan_revision(app["mid"])["document"]
    con = missions._ready(None)
    con.execute("UPDATE missions SET project_id='p-synthetic01' WHERE id=?", (app["mid"],))
    con.commit()
    con.close()
    from mission_team_fixture import procedure

    two = FLOW.replace('title = "Application"', 'title = "Second application"')
    monkeypatch.setattr(missions, "_same_inputs", lambda *a, **k: False)
    out = missions.apply_procedure(
        app["mid"],
        project_id="p-synthetic01",
        procedure=procedure(two, "second"),
        inputs=[],
        items=[
            {
                "step": s["id"],
                "key": i["key"],
                "title": i["title"],
                "probe": i["probe"],
                "probe_args": None,
                "gate": False,
            }
            for s in procedure(two, "second")["flow"]["steps"]
            for i in s["checklist"]
        ],
        reason="a second one",
    )
    doc = missions.plan_revision(app["mid"], out["revision"])["document"]
    for s in before["steps"]:
        got = next(x for x in doc["steps"] if x["id"] == s["id"])
        assert got["envelope"]["assignment"] == s["envelope"]["assignment"]
    assert all(s["envelope"]["assignment"] is None for s in doc["steps"] if s["objective"] == "o2")


def test_the_schema_is_v38_and_rolls_back_to_37(app):
    db = missions._db_path()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 38
    con.close()
    assert missions.rollback_schema(37, path=db) == {"from": 38, "to": 37}
    missions.reset_schema_cache_for_test()
    assert missions.plan_revision(app["mid"])["revision"] == 1  # converges, nothing dropped


def test_two_writers_from_one_revision_exactly_one_wins(app):
    """The store's own compare-and-set (#1579's concurrent-edit rule): two operations built from
    revision 1 race past the route's early check; the second commit is a 409, writes nothing."""
    doc = missions.plan_revision(app["mid"])["document"]
    ctx = context()
    first = team.apply_op(doc, _op("assemble"), ctx, mission_id=app["mid"], revision=1)
    second = team.apply_op(doc, _op("assemble"), ctx, mission_id=app["mid"], revision=1)
    kw = {"author_kind": "operator", "request_digest": "d", "expected_revision": 1, "reason": "r"}
    out = missions.commit_plan_op(app["mid"], first, operation_id=str(uuid.uuid4()), **kw)
    assert out == {"revision": 2, "replayed": False}
    with pytest.raises(missions.MissionError) as e:
        missions.commit_plan_op(app["mid"], second, operation_id=str(uuid.uuid4()), **kw)
    assert e.value.status == 409 and e.value.latest_revision == 2
    assert _revisions(app["mid"]) == 2


# ---- independent review of #1985: the three blocking findings ---------------------------------


def test_clearing_an_override_is_re_checked_like_every_edit():
    """`not_distinct` again after every edit: clearing b's override would put b back on opus, the
    model step a (distinct by model) now runs on — refused, nothing changed."""
    doc = document(TWO.replace("CONSTRAINT", "model"))
    ctx = context(
        specs=[spec("x", "sonnet", ["s"]), spec("y", "opus", ["t"])],
        library={"s": "r1", "t": "r1"},
    )
    _, out = _team(doc, ctx)
    doc = _with(doc, out["assignments"])
    doc = _override(doc, ctx, "o1.b", "haiku")
    doc = _override(doc, ctx, "o1.a", "opus")
    with pytest.raises(team.TeamRefused) as e:
        team.apply_op(doc, _op("clear_override", step="o1.b"), ctx, mission_id="m", revision=1)
    assert e.value.code == "not_distinct"
    # And a clear onto a model now below the step's grade is refused the same way.
    doc2, ctx2 = _assembled()
    doc2 = _override(doc2, ctx2, "o1.cv", "opus")
    remapped = context(grades={ENGINE: {"opus": "frontier", "sonnet": "fast", "haiku": "fast"}})
    with pytest.raises(team.TeamRefused) as e:
        team.apply_op(
            doc2, _op("clear_override", step="o1.cv"), remapped, mission_id="m", revision=1
        )
    assert e.value.code == "grade_below_min"


def test_a_native_api_clients_usage_is_its_console_agents(monkeypatch):
    """claude-api reports no usage of its own; the console agent its `api.source` names does."""
    from types import SimpleNamespace

    from agent_sessions import mission_structured, native_models

    monkeypatch.setattr(mission_structured, "admits", lambda prov: (True, None))
    monkeypatch.setattr(
        native_models, "models", lambda prov: native_models.ModelList("ok", ({"id": "opus"},))
    )
    prov = SimpleNamespace(
        engine_id="claude-api",
        manifest=SimpleNamespace(
            runtime="api",
            tools_mcp="claude-mcp-config-flag",
            identity=SimpleNamespace(label="Claude — API"),
            api=SimpleNamespace(source="claude"),
        ),
    )
    assert team._client(prov, {"claude": "exhausted"}).usage == "exhausted"
    assert team._client(prov, {"claude": "ok"}).usage == "ok"
    assert team._client(prov, {}).usage == "unknown"


def _many(n: int) -> str:
    steps = []
    for i in range(n):
        steps.append(
            f'[[steps]]\nid = "a{i}"\ntitle = "A{i}"\nactor = {{ kind = "agent" }}\n'
            f'role = {{ skills = ["s"] }}\n[[steps.checklist]]\nkey = "a{i}_ok"\ntitle = "a"\n'
            'probe = "supervisor_judged"\n'
        )
    steps.append(
        '[[steps]]\nid = "x"\ntitle = "X"\nactor = { kind = "agent" }\n'
        'role = { skills = ["s"] }\ndistinct_from = [{ step = "a0", constraint = "engine" }]\n'
        '[[steps.checklist]]\nkey = "x_ok"\ntitle = "x"\nprobe = "supervisor_judged"\n'
    )
    return 'format = 5\ntitle = "Many"\n\n' + "\n".join(steps)


def test_one_blocked_step_never_exhausts_the_budget_on_an_ordinary_plan():
    """Twelve interchangeable steps plus one that must differ from a0 by AGENT, in a one-agent
    pool: x stays unassigned with its code; the plan is never refused as too large."""
    doc = document(_many(12))
    specs = [spec(f"s{i}", "sonnet", ["s"], capacity=8) for i in range(3)]
    picked, out = _team(doc, context(specs=specs, library={"s": "r1"}))
    assert len(picked) == 12 and "o1.x" not in picked
    codes = {r["code"] for c in out["unassigned"]["o1.x"]["candidates"] for r in c["reasons"]}
    assert codes == {"not_distinct"}
