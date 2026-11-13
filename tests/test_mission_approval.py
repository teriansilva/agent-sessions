"""Approving the enumerated plan is the tap (#1951, #1613 P6 slice A).

The pins, their digest, the ONE re-check (P5's `qualify` on the effective model) with every
closed refusal code, the boundary function `admit` that records a refusal once, the approval
record (immutable, CAS, `operation_id`, revoke) and standing consent (inert; proven through a
harness that calls the same boundary). The scenario is #1950's synthetic application mission
(`mission_team_fixture`): same flow, roles and pool as the operator's, no personal content.
"""

from __future__ import annotations

import copy
import inspect
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import mission_approval as appr
from agent_sessions import mission_team as team
from agent_sessions import missions
from agent_sessions.main import create_app
from mission_team_fixture import ENGINE, GRADES, client, context, document, pool, store_plan

PID = "p-synthetic01"
EQUIPMENT = [
    {"kind": "memory", "id": PID, "name": "project memory", "scope": "project"},
    {"kind": "secret", "id": "v-gh01", "name": "GITHUB_TOKEN", "scope": "project"},
    {"kind": "secret_file", "id": "v-cv01", "name": "cv-master.pdf", "scope": "global"},
]
AGENT_STEPS = [
    "o1.posting",
    "o1.cv",
    "o1.cover_letter",
    "o1.documents",
    "o1.review",
    "o1.pull_request",
    "o1.record",
]


def facts(**over) -> appr.Facts:
    entry = {"usable": True, "equipment": copy.deepcopy(EQUIPMENT)}
    entry.update(over)
    return appr.Facts(projects={PID: entry})


def assembled(ctx=None) -> dict:
    doc = document()
    ctx = ctx or context()
    out = team.assemble(doc, team.assignments_of(doc), ctx)
    for s in doc["steps"]:
        if s["id"] in out["assignments"]:
            s["envelope"]["assignment"] = out["assignments"][s["id"]]
    return doc


def approved(doc=None, ctx=None, f=None, revision=1) -> dict:
    doc = doc or assembled()
    p = appr.pins(doc, revision, ctx or context(), f or facts())
    return {"id": "apr-" + "0" * 20, "pins": p, "digest": appr.digest(p), "revoked_at": None}


def codes(check) -> list[str]:
    return [r["code"] for r in check["reasons"]]


def bump(specs, sid, **cur):
    """`sid` saved again: a new current version with `cur` changed (versions are immutable)."""
    for s in specs:
        if s["id"] == sid:
            old = copy.deepcopy(s["current"])
            s["current"] = {**s["current"], "version": s["current"]["version"] + 1, **cur}
            return old
    raise KeyError(sid)


def with_old(ctx, sid, old):
    """`ctx` able to read the pinned version `old` of `sid` (as `team.with_pinned` does)."""
    return team.Context(**{**ctx.__dict__, "pinned": {(sid, old["version"]): old}})


# ---- the pins ------------------------------------------------------------------------------------


def test_the_approval_pins_the_acceptance_team_at_its_versions_and_models():
    p = approved()["pins"]
    got = {s["id"]: (s["specialist"]["id"], s["effective"]["model"]) for s in p["steps"]}
    assert got == {
        "o1.posting": ("career-archivist", "sonnet"),
        "o1.cv": ("cv-writer", "sonnet"),
        "o1.cover_letter": ("cv-writer", "sonnet"),
        "o1.documents": ("career-archivist", "sonnet"),
        "o1.review": ("application-reviewer", "opus"),
        "o1.pull_request": ("career-archivist", "sonnet"),
        "o1.record": ("career-archivist", "sonnet"),
    }
    assert all(s["specialist"]["version"] == 1 for s in p["steps"])
    assert p["revision"] == 1 and p["equipment"] == {PID: EQUIPMENT}
    assert p["objectives"][0]["allowed"] == {
        "actors": ["agent", "operator"],
        "probes": ["none", "supervisor_judged"],
    }
    assert p["rework"] == [
        {
            "from": "o1.review",
            "to": "o1.cv",
            "max_rounds": 3,
            "steps": ["o1.cv", "o1.documents", "o1.review"],
        }
    ]


def test_no_grade_and_no_value_is_ever_in_the_pins():
    blob = str(approved()["pins"])
    for word in ("grade", "strong", "frontier", "fast", "secret_value", "marker"):
        assert word not in blob


def test_the_digest_is_stable_and_binds_every_pin():
    a, b = approved(), approved()
    assert a["digest"] == b["digest"] and appr.DIGEST_RE.fullmatch(a["digest"])
    changed = approved(f=facts(equipment=EQUIPMENT[:2]))
    assert changed["digest"] != a["digest"]


def test_an_unassigned_agent_step_cannot_be_approved():
    with pytest.raises(appr.ApprovalRefused) as e:
        appr.pins(document(), 1, context(), facts())
    assert e.value.code == "unassigned" and "o1.cv" in e.value.text


def test_unreadable_equipment_cannot_be_approved():
    with pytest.raises(appr.ApprovalRefused) as e:
        appr.pins(assembled(), 1, context(), facts(equipment=None))
    assert e.value.code == "equipment_changed"


def test_the_card_says_the_bounds_and_what_is_not_covered():
    doc = assembled()
    out = appr.lines(approved(doc)["pins"], doc, context())
    rows = {r["key"]: r for r in out["groups"][0]["rows"]}
    assert rows["o1.cv"]["bound"] == "up to 1 + 3"
    assert rows["o1.posting"]["bound"] == "1 session"
    assert rows["o1.review"]["text"].startswith(
        "application-reviewer v1 · claude-api · opus · Senior (needs Senior)"
    )
    assert out["bounds"].startswith("Up to 16 sessions in all")
    assert "approve, submit" in out["not_covered"]
    assert "HQ never approves a gate" in out["not_covered"]


# ---- the re-check: one case per refusal code -----------------------------------------------------


def test_a_fresh_approval_rechecks_clean():
    doc = assembled()
    assert appr.recheck(approved(doc), doc, 1, context(), facts()) == {"ok": True, "reasons": []}


def test_stale_binding_a_new_specialist_version_refuses():
    """REGRESSION 1 (#1951 acceptance 3): `cv-writer` saved again after approval (D-P6-1)."""
    doc = assembled()
    a = approved(doc)
    specs = pool()
    old = bump(specs, "cv-writer", summary="edited")
    ctx = with_old(context(specs=specs), "cv-writer", old)
    check = appr.recheck(a, doc, 1, ctx, facts(), step="o1.cv")
    assert codes(check) == ["specialist_version_changed"]
    r = check["reasons"][0]
    assert (r["old"], r["new"], r["text"]) == ("v1", "v2", "cv-writer changed from v1 to v2")
    # The step that never used cv-writer still launches.
    assert appr.recheck(a, doc, 1, ctx, facts(), step="o1.posting")["ok"]


def test_stale_binding_an_archived_specialist_refuses():
    doc = assembled()
    a = approved(doc)
    specs = pool()
    next(s for s in specs if s["id"] == "cv-writer")["archived"] = True
    check = appr.recheck(a, doc, 1, context(specs=specs), facts(), step="o1.cv")
    assert codes(check) == ["specialist_archived"]


def test_a_pinned_model_that_moved_refuses():
    doc = assembled()
    a = approved(doc)
    a["pins"]["steps"][1]["effective"]["model"] = "haiku"
    check = appr.recheck(a, doc, 1, context(), facts(), step="o1.cv")
    assert codes(check) == ["model_changed"]


def test_a_model_the_client_no_longer_offers_refuses():
    doc = assembled()
    a = approved(doc)
    ctx = context(clients={ENGINE: client(models={"opus": ("low", "high")})})
    check = appr.recheck(a, doc, 1, ctx, facts(), step="o1.cv")
    assert codes(check) == ["model_unavailable"]
    ctx = context(clients={ENGINE: client(ready=False, why="not ready")})
    assert "model_unavailable" in codes(appr.recheck(a, doc, 1, ctx, facts(), step="o1.cv"))


def test_grade_drift_refuses_and_remapping_back_launches_the_same_approval():
    """REGRESSION 2 (#1951 acceptance 4): sonnet remapped strong → fast after approval."""
    doc = assembled()
    a = approved(doc)
    drifted = copy.deepcopy(GRADES)
    drifted[ENGINE]["sonnet"] = "fast"
    check = appr.recheck(a, doc, 1, context(grades=drifted), facts(), step="o1.cv")
    assert codes(check) == ["grade_below_min"]
    r = check["reasons"][0]
    assert (r["old"], r["new"]) == ("strong", "fast")
    assert r["text"] == "sonnet is Intern, below Specialist, which cv-writing needs"
    # The grade was never pinned: mapped back, the SAME approval launches again.
    assert appr.recheck(a, doc, 1, context(), facts(), step="o1.cv")["ok"]


def test_an_unmapped_effective_model_refuses_as_ungraded():
    doc = assembled()
    a = approved(doc)
    ungraded = copy.deepcopy(GRADES)
    del ungraded[ENGINE]["opus"]
    check = appr.recheck(a, doc, 1, context(grades=ungraded), facts(), step="o1.review")
    assert codes(check) == ["ungraded"]


def test_a_skill_that_changed_in_the_library_refuses():
    doc = assembled()
    a = approved(doc)
    lib = {"career-archive": "r1", "cv-writing": "r2", "application-review": "r1"}
    check = appr.recheck(a, doc, 1, context(library=lib), facts(), step="o1.cv")
    assert codes(check) == ["skill_pin_stale"]


def test_a_newer_plan_revision_refuses():
    doc = assembled()
    check = appr.recheck(approved(doc), doc, 2, context(), facts(), step="o1.cv")
    assert codes(check) == ["plan_revision_superseded"]
    assert (check["reasons"][0]["old"], check["reasons"][0]["new"]) == (1, 2)


def test_an_unavailable_project_refuses():
    doc = assembled()
    check = appr.recheck(approved(doc), doc, 1, context(), facts(usable=False), step="o1.cv")
    assert codes(check) == ["project_unavailable"]


@pytest.mark.parametrize(
    "change",
    [
        "added",
        "removed",
        "access_off",
        "rebound_global",
        "kind_changed",
        "renamed",
        "unreadable",
    ],
)
def test_any_change_to_what_is_offered_refuses_as_equipment_changed(change):
    doc = assembled()
    a = approved(doc)
    now = copy.deepcopy(EQUIPMENT)
    if change == "added":
        now.append({"kind": "secret", "id": "v-st01", "name": "STRIPE_KEY", "scope": "project"})
    elif change in ("removed", "access_off"):
        now = now[:2]  # agent access off: the broker no longer offers it
    elif change == "rebound_global":
        now[1]["scope"] = "global"
    elif change == "kind_changed":
        now[1]["kind"] = "secret_file"
    elif change == "renamed":
        now[1]["name"] = "GH_TOKEN"
    else:
        now = None
    check = appr.recheck(a, doc, 1, context(), facts(equipment=now), step="o1.cv")
    assert codes(check) == ["equipment_changed"]
    if change == "added":
        assert check["reasons"][0]["text"].endswith("added secret STRIPE_KEY (this project)")


def test_a_rotated_secret_value_is_not_a_change():
    """D-P6-5: the pin is what is offered, never the value or its revision."""
    doc = assembled()
    a = approved(doc)
    # A rotation changes nothing `equipment_of` returns: same kind, id, name and scope.
    assert appr.recheck(a, doc, 1, context(), facts(), step="o1.cv")["ok"]


def _deployed(doc):
    """`doc` as if its procedure came from a deployed playbook at revision 7d3675e8…"""
    proc = doc["objectives"][0]["procedure"]
    proc.update(source="playbook", playbook_id="synthetic-application", revision="7d3675e8" * 5)
    return doc


def _dep(**over):
    d = {
        "reason": None,
        "playbook_id": "synthetic-application",
        "revision": "7d3675e8" * 5,
        "id": "dep-1",
        "generation": 4,
    }
    d.update(over)
    return d


def test_a_redeployed_playbook_refuses():
    doc = _deployed(assembled())
    a = approved(doc, f=facts(deployment=_dep()))
    assert appr.recheck(a, doc, 1, context(), facts(deployment=_dep()), step="o1.cv")["ok"]
    moved = facts(deployment=_dep(revision="a91f02c4" * 5, generation=5))
    check = appr.recheck(a, doc, 1, context(), moved, step="o1.cv")
    assert codes(check) == ["playbook_revision_changed", "deployment_changed"]


def test_a_redeployment_at_the_same_revision_refuses_as_deployment_changed():
    doc = _deployed(assembled())
    a = approved(doc, f=facts(deployment=_dep()))
    check = appr.recheck(a, doc, 1, context(), facts(deployment=_dep(generation=5)))
    assert codes(check) == ["deployment_changed"]
    unsettled = facts(deployment=_dep(reason="the project's deployment is not settled"))
    assert codes(appr.recheck(a, doc, 1, context(), unsettled)) == ["deployment_changed"]


def test_a_revoked_approval_refuses():
    doc = assembled()
    a = {**approved(doc), "revoked_at": 5.0, "revoked_reason": "revoked"}
    assert codes(appr.recheck(a, doc, 1, context(), facts())) == ["approval_revoked"]


def test_every_code_is_in_the_closed_set_and_qualify_codes_map_into_it():
    assert len(set(appr.CODES)) == 14
    assert set(appr._QUALIFY_MAP) == set(team.CODES) - {"not_distinct"}
    assert set(appr._QUALIFY_MAP.values()) <= set(appr.CODES)


def test_recheck_uses_p5s_one_predicate():
    """#1950/#1951: approval and launch reuse the SAME `qualify()`; no second copy."""
    src = inspect.getsource(appr)
    assert "team.qualify(" in src
    assert "def qualify" not in src and "model_grades.rank" not in src


def test_a_gate_on_an_operator_step_checks_its_objective_and_project():
    doc = assembled()
    a = approved(doc)
    assert appr.recheck(a, doc, 1, context(), facts(), step="o1.approve")["ok"]
    check = appr.recheck(a, doc, 1, context(), facts(usable=False), step="o1.approve")
    assert codes(check) == ["project_unavailable"]


def test_an_agent_step_that_binds_no_role_cannot_be_approved():
    """A format ≤ 4 agent step has no role, so nothing can be pinned on it (independent review)."""
    doc = assembled()
    legacy = next(s for s in doc["steps"] if s["id"] == "o1.posting")
    legacy["role"], legacy["envelope"]["assignment"] = None, None
    with pytest.raises(appr.ApprovalRefused) as e:
        appr.pins(doc, 1, context(), facts())
    assert e.value.code == "unassigned" and "o1.posting binds no role" in e.value.text


def test_an_agent_step_the_approval_does_not_enumerate_is_refused():
    doc = assembled()
    a = approved(doc)
    a["pins"]["steps"] = [s for s in a["pins"]["steps"] if s["id"] != "o1.posting"]
    check = appr.recheck(a, doc, 1, context(), facts(), step="o1.posting")
    assert codes(check) == ["approval_revoked"]
    assert "does not cover agent step o1.posting" in check["reasons"][0]["text"]


def test_the_refusal_key_ignores_wording_when_old_and_new_say_what_changed():
    a = approved()
    one = [
        {
            "code": "specialist_version_changed",
            "text": "x",
            "step": "o1.cv",
            "old": "v1",
            "new": "v2",
        }
    ]
    two = [{**one[0], "text": "y"}]
    assert appr.change_digest(a, "o1.cv", one) == appr.change_digest(a, "o1.cv", two)
    three = [{**one[0], "new": "v3"}]
    assert appr.change_digest(a, "o1.cv", one) != appr.change_digest(a, "o1.cv", three)


def test_equipment_is_what_the_real_store_offers_never_the_value(tmp_home):
    """D-P6-5 against the real variables store: a rotated value changes nothing; turning agent
    access off removes the item; no value appears in the row."""
    from agent_sessions import template_vars, variables

    row = variables.create({"name": "forgejo_token", "kind": "secret", "value": "value-one-12345"})
    row = variables.update(row["id"], {"agent_access": True}, row["updated_at"])
    template_vars.bind_project(
        "atlas", [{"name": "forgejo_token", "kind": "secret", "ref": "global"}]
    )
    before = appr.equipment_of("atlas")
    assert before == [
        {"kind": "secret", "id": row["id"], "name": "forgejo_token", "scope": "global"}
    ]
    row = variables.update(row["id"], {"value": "value-two-67890"}, row["updated_at"])
    assert appr.equipment_of("atlas") == before
    assert "value-" not in str(before)
    variables.update(row["id"], {"agent_access": False}, row["updated_at"])
    assert appr.equipment_of("atlas") == []
    assert appr.equipment_of("beta") == []


# ---- standing consent (pure) ---------------------------------------------------------------------


def _consent(**over):
    c = {"id": "con-" + "1" * 20, "approval_id": "apr-" + "0" * 20, "expires_at": 1000.0}
    c["revoked_at"] = None
    c.update(over)
    return c


def test_consent_verdicts():
    a = approved()
    ok = {"ok": True, "reasons": []}
    assert appr.consent_verdict(_consent(), a, ok, now=10)["state"] == "active"
    assert appr.consent_verdict(None, a, ok, now=10)["state"] == "absent"
    assert appr.consent_verdict(_consent(revoked_at=5.0), a, ok, now=10)["state"] == "revoked"
    expired = appr.consent_verdict(_consent(), a, ok, now=1000)
    assert expired["state"] == "expired" and codes(expired) == ["approval_expired"]
    stale = {"ok": False, "reasons": [{"code": "specialist_version_changed", "text": "x"}]}
    void = appr.consent_verdict(_consent(), a, stale, now=10)
    assert void["state"] == "void" and codes(void) == ["specialist_version_changed"]
    other = appr.consent_verdict(_consent(approval_id="apr-" + "9" * 20), a, ok, now=10)
    assert other["state"] == "void"


def test_the_consent_scope_is_server_text_and_its_digest_binds_the_approval():
    a = approved()
    s = appr.consent_scope(a, "Apply")
    assert s["lines"][0].startswith("Launch plan revision 1 of “Apply”, exactly as approved")
    assert "voids this consent" in s["lines"][-1]
    b = {**a, "id": "apr-" + "9" * 20}
    assert appr.consent_scope(b, "Apply")["digest"] != s["digest"]


@pytest.mark.parametrize("days", [0, 31, 7.0, "7", True])
def test_the_consent_expiry_is_whole_days_up_to_thirty(days):
    body = {
        "approval_id": "apr-" + "0" * 20,
        "scope_digest": "sha256:" + "a" * 64,
        "days": days,
        "operation_id": str(uuid.uuid4()),
    }
    with pytest.raises(appr.ApprovalRefused) as e:
        appr.parse_consent(body)
    assert e.value.status == 422


# ---- the store and the routes --------------------------------------------------------------------


@pytest.fixture
def app(auth_cfg, monkeypatch):
    missions.reset_schema_cache_for_test()
    state = {"ctx": context(), "facts": facts()}
    monkeypatch.setattr(team, "snapshot", lambda: state["ctx"])
    monkeypatch.setattr(appr, "read_facts", lambda doc: state["facts"])
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
    store_plan(mid, assembled())
    yield {"c": c, "hdr": hdr, "mid": mid, "state": state, "cfg": auth_cfg}
    missions.reset_schema_cache_for_test()


def _card(app):
    r = app["c"].get(f"/api/missions/{app['mid']}/plan/approval")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def _approve(app, **over):
    card = _card(app)
    body = {"revision": card["revision"], "digest": card["card"]["digest"]}
    body["operation_id"] = str(uuid.uuid4())
    body.update(over)
    return app["c"].post(f"/api/missions/{app['mid']}/plan/approve", json=body, headers=app["hdr"])


def test_the_card_offers_the_latest_revision_with_its_digest(app):
    card = _card(app)
    assert card["revision"] == 1 and card["approval"] is None and card["consent"] is None
    assert card["card"]["ok"] and appr.DIGEST_RE.fullmatch(card["card"]["digest"])
    assert card["card"]["agent_steps"] == 7
    assert card["consent_days"] == {"default": 7, "max": 30}


def test_approve_writes_one_immutable_record(app):
    r = _approve(app)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revision"] == 1 and not out["replayed"]
    card = _card(app)
    assert card["approval"]["state"] == "in_force" and card["approval"]["id"] == out["id"]
    assert card["consent_offer"] is not None
    con = missions._ready(None)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE mission_plan_approvals SET digest='x' WHERE id=?", (out["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE mission_plan_approvals SET revision=2 WHERE id=?", (out["id"],))
    finally:
        con.close()


def test_a_stale_digest_is_a_409_and_writes_nothing(app):
    r = _approve(app, digest="sha256:" + "0" * 64)
    assert r.status_code == 409 and r.json()["code"] == "digest_changed"
    assert missions.approval(app["mid"]) is None


def test_a_repeated_operation_id_returns_the_original(app):
    op = str(uuid.uuid4())
    first = _approve(app, operation_id=op).json()
    again = _approve(app, operation_id=op)
    assert again.status_code == 200 and again.json()["replayed"]
    assert again.json()["id"] == first["id"]
    other = _approve(app, operation_id=op, revision=2)
    assert other.status_code == 409


def test_approving_a_plan_that_no_longer_qualifies_is_refused_with_reasons(app):
    drifted = copy.deepcopy(GRADES)
    drifted[ENGINE]["sonnet"] = "fast"
    app["state"]["ctx"] = context(grades=drifted)
    card = _card(app)
    assert not card["card"]["ok"] and "grade_below_min" in codes(card["card"])
    r = _approve(app)
    assert r.status_code == 409 and r.json()["code"] == "grade_below_min"
    assert missions.approval(app["mid"]) is None


def test_approve_against_a_concurrent_plan_edit_has_one_winner(app):
    """The store's CAS: the approve was built from revision 1; a plan edit wrote 2 first."""
    card = _card(app)
    p = appr.pins(missions.plan_revision(app["mid"])["document"], 1, context(), facts())
    store_plan(app["mid"], assembled())
    with pytest.raises(missions.MissionError) as e:
        missions.commit_approval(
            app["mid"],
            revision=1,
            digest=card["card"]["digest"],
            pins=p,
            operation_id=str(uuid.uuid4()),
            request_digest="d",
        )
    assert e.value.status == 409 and e.value.latest_revision == 2
    assert missions.approval(app["mid"]) is None


def test_two_approvals_of_one_revision_leave_exactly_one(app):
    assert _approve(app).status_code == 200
    second = _approve(app)
    assert second.status_code == 409
    con = missions._ready(None)
    try:
        n = con.execute("SELECT COUNT(*) FROM mission_plan_approvals").fetchone()[0]
    finally:
        con.close()
    assert n == 1


def test_a_new_revision_needs_a_new_approval_and_supersedes_the_old(app):
    first = _approve(app).json()
    store_plan(app["mid"], assembled())
    card = _card(app)
    assert card["approval"]["state"] == "stale"
    assert codes(card["approval"]) == ["plan_revision_superseded"]
    second = _approve(app).json()
    assert second["revision"] == 2 and second["id"] != first["id"]
    old = sqlite3.connect(missions._db_path())
    try:
        row = old.execute(
            "SELECT revoked_reason FROM mission_plan_approvals WHERE id=?", (first["id"],)
        ).fetchone()
    finally:
        old.close()
    assert row == ("superseded",)


def test_the_approval_in_force_is_found_even_if_the_clock_stepped_back(app):
    first = _approve(app).json()
    store_plan(app["mid"], assembled())
    card = _card(app)
    p = appr.pins(missions.plan_revision(app["mid"])["document"], 2, context(), facts())
    second = missions.commit_approval(
        app["mid"],
        revision=2,
        digest=card["card"]["digest"],
        pins=p,
        operation_id=str(uuid.uuid4()),
        request_digest="d",
        now=first["created_at"] - 3600,  # an earlier wall-clock time than the superseded one
    )
    assert missions.approval(app["mid"])["id"] == second["id"]


def test_revoke_is_once_and_the_card_says_so(app):
    aid = _approve(app).json()["id"]
    url = f"/api/missions/{app['mid']}/plan/approval/revoke"
    r = app["c"].post(url, json={"approval_id": aid}, headers=app["hdr"])
    assert r.status_code == 200
    assert _card(app)["approval"]["state"] == "revoked"
    again = app["c"].post(url, json={"approval_id": aid}, headers=app["hdr"])
    assert again.status_code == 409


def test_every_write_route_needs_csrf_and_origin(app):
    base = f"/api/missions/{app['mid']}/plan"
    for path in ("/approve", "/approval/revoke", "/consent", "/consent/revoke"):
        r = app["c"].post(base + path, json={})
        assert r.status_code in (401, 403), path
        bad = {**app["hdr"], "Origin": "https://evil.example"}
        r = app["c"].post(base + path, json={}, headers=bad)
        assert r.status_code in (401, 403), path


def test_a_body_with_an_extra_key_is_refused(app):
    r = _approve(app, model="opus")
    assert r.status_code == 422


def test_the_approval_route_reflects_a_stale_binding_live(app):
    """REGRESSION 1 on the card: the approval re-checks on every read, nothing stored."""
    _approve(app)
    specs = pool()
    old = bump(specs, "cv-writer", summary="edited")
    store_old = {("cv-writer", 1): old}
    app["state"]["ctx"] = team.Context(**{**context(specs=specs).__dict__, "pinned": store_old})
    card = _card(app)
    assert card["approval"]["state"] == "stale"
    assert {r["step"] for r in card["approval"]["reasons"]} == {"o1.cv", "o1.cover_letter"}
    assert card["consent_offer"] is None


# ---- the boundary: `admit` -----------------------------------------------------------------------


@pytest.fixture
def live(app, monkeypatch):
    monkeypatch.setattr(appr, "context_for", lambda doc: app["state"]["ctx"])
    return app


def test_admit_with_no_approval_refuses(live):
    out = appr.admit(live["mid"], "o1.cv", "launch")
    assert not out["ok"] and codes(out) == ["approval_revoked"]


def test_admit_launches_a_clean_step(live):
    _approve(live)
    out = appr.admit(live["mid"], "o1.cv", "launch")
    assert out["ok"] and missions.refusals(live["mid"]) == []


def test_a_stale_binding_is_refused_at_launch_and_asks_once(live):
    """REGRESSION 1 at the boundary: one refusal per (step, what changed); a retry asks nothing
    new, a different change asks again."""
    _approve(live)
    specs = pool()
    old = bump(specs, "cv-writer", summary="edited")
    live["state"]["ctx"] = with_old(context(specs=specs), "cv-writer", old)
    first = appr.admit(live["mid"], "o1.cv", "launch")
    assert not first["ok"] and first["new"]
    assert codes(first) == ["specialist_version_changed"]
    again = appr.admit(live["mid"], "o1.cv", "launch")
    assert not again["ok"] and not again["new"]
    assert len(missions.refusals(live["mid"])) == 1
    bump(specs, "cv-writer", summary="edited again")  # v3: something else changed
    third = appr.admit(live["mid"], "o1.cv", "launch")
    assert third["new"] and third["reasons"][0]["new"] == "v3"
    assert len(missions.refusals(live["mid"])) == 2


def test_grade_drift_is_refused_at_launch_and_the_remap_back_admits(live):
    """REGRESSION 2 at the boundary."""
    _approve(live)
    drifted = copy.deepcopy(GRADES)
    drifted[ENGINE]["sonnet"] = "fast"
    live["state"]["ctx"] = context(grades=drifted)
    out = appr.admit(live["mid"], "o1.cv", "launch")
    assert codes(out) == ["grade_below_min"]
    live["state"]["ctx"] = context()
    assert appr.admit(live["mid"], "o1.cv", "launch")["ok"]


def test_a_gate_settlement_rechecks_like_a_launch(live):
    _approve(live)
    live["state"]["facts"] = facts(equipment=EQUIPMENT[:1])
    out = appr.admit(live["mid"], "o1.approve", "gate")
    assert codes(out) == ["equipment_changed"]
    assert missions.refusals(live["mid"])[0]["boundary"] == "gate"


def test_admit_refuses_an_unknown_boundary(live):
    with pytest.raises(ValueError):
        appr.admit(live["mid"], "o1.cv", "read")


# ---- standing consent: the harness trigger calls the same boundary ------------------------------


def _consent_post(app, days=7, **over):
    card = _card(app)
    offer = card["consent_offer"]
    body = {
        "approval_id": offer["approval_id"],
        "scope_digest": offer["digest"],
        "days": days,
        "operation_id": str(uuid.uuid4()),
    }
    body.update(over)
    return app["c"].post(f"/api/missions/{app['mid']}/plan/consent", json=body, headers=app["hdr"])


def harness_trigger(mission_id, consent_id, now=None):
    """What #1901 / P7b will wire: an automation launching a step under standing consent."""
    return appr.admit(mission_id, "o1.posting", "consent", consent_id=consent_id, now=now)


def test_with_a_valid_consent_a_launch_proceeds(live):
    _approve(live)
    r = _consent_post(live)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert harness_trigger(live["mid"], cid)["ok"]
    assert _card(live)["consent"]["state"] == "active"


def test_with_no_consent_a_launch_is_refused(live):
    _approve(live)
    out = harness_trigger(live["mid"], None)
    assert not out["ok"] and codes(out) == ["approval_revoked"]


def test_an_expired_consent_refuses(live):
    _approve(live)
    c = _consent_post(live, days=1).json()
    out = harness_trigger(live["mid"], c["id"], now=c["expires_at"] + 1)
    assert codes(out) == ["approval_expired"]


def test_a_revoked_consent_refuses(live):
    _approve(live)
    cid = _consent_post(live).json()["id"]
    url = f"/api/missions/{live['mid']}/plan/consent/revoke"
    assert live["c"].post(url, json={"consent_id": cid}, headers=live["hdr"]).status_code == 200
    assert codes(harness_trigger(live["mid"], cid)) == ["approval_revoked"]
    assert _card(live)["consent"]["state"] == "revoked"


@pytest.mark.parametrize("change", ["plan", "specialist", "equipment", "playbook_project"])
def test_any_change_voids_the_consent_even_for_an_untouched_step(live, change):
    _approve(live)
    cid = _consent_post(live).json()["id"]
    if change == "plan":
        store_plan(live["mid"], assembled())
    elif change == "specialist":
        specs = pool()
        old = bump(specs, "application-reviewer", summary="edited")
        live["state"]["ctx"] = with_old(context(specs=specs), "application-reviewer", old)
    elif change == "equipment":
        live["state"]["facts"] = facts(equipment=EQUIPMENT[1:])
    else:
        live["state"]["facts"] = facts(usable=False)
    out = harness_trigger(live["mid"], cid)  # o1.posting itself is unchanged
    assert not out["ok"]
    assert _card(live)["consent"]["state"] == "void"


def test_revoking_the_approval_revokes_its_consent(live):
    aid = _approve(live).json()["id"]
    cid = _consent_post(live).json()["id"]
    url = f"/api/missions/{live['mid']}/plan/approval/revoke"
    live["c"].post(url, json={"approval_id": aid}, headers=live["hdr"])
    assert missions.consent(live["mid"], cid)["revoked_reason"] == "approval_revoked"
    assert not harness_trigger(live["mid"], cid)["ok"]


def test_the_maximum_expiry_is_enforced_by_the_route(live):
    _approve(live)
    assert _consent_post(live, days=31).status_code == 422
    assert _consent_post(live, days=30).status_code == 200


def test_a_stale_scope_digest_is_a_409(live):
    _approve(live)
    r = _consent_post(live, scope_digest="sha256:" + "0" * 64)
    assert r.status_code == 409 and missions.consent(live["mid"]) is None


def test_approving_never_creates_standing_consent(live):
    _approve(live)
    assert missions.consent(live["mid"]) is None
    assert _card(live)["consent"] is None


def test_no_consent_on_a_stale_approval(live):
    _approve(live)
    live["state"]["facts"] = facts(usable=False)
    assert _card(live)["consent_offer"] is None


# ---- HQ: no approve, no consent ------------------------------------------------------------------


def test_hq_has_no_tool_that_approves_or_consents():
    from agent_sessions import ask_operations, ask_orchestrator

    names = " ".join(ask_orchestrator.TOOLS)
    for word in ("approve", "consent", "revoke"):
        assert word not in names
    for mod in (ask_orchestrator, ask_operations):
        src = inspect.getsource(mod)
        assert "plan/approve" not in src and "plan/consent" not in src


# ---- schema --------------------------------------------------------------------------------------


def test_the_schema_rolls_back_to_38(app):
    db = missions._db_path()
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION >= 39
    con.close()
    assert missions.rollback_schema(38, path=db) == {"from": missions.SCHEMA_VERSION, "to": 38}
    missions.reset_schema_cache_for_test()
    assert missions.plan_revision(app["mid"])["revision"] == 1  # converges, nothing dropped


def test_approvals_consents_and_refusals_leave_with_their_mission(live):
    _approve(live)
    _consent_post(live)
    live["state"]["facts"] = facts(usable=False)
    appr.admit(live["mid"], "o1.cv", "launch")
    assert missions.delete_mission(live["mid"])
    con = missions._ready(None)
    try:
        for table in (
            "mission_plan_approvals",
            "mission_standing_consents",
            "mission_approval_refusals",
        ):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table  # noqa: S608
    finally:
        con.close()
