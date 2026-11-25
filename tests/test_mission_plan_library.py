"""Mission plans in the Library (#1924, #1613 P7a): save-as-plan, the format and the store.

Real deployments drive the saves (the procedure demo through the real lifecycle, as #1777's
tests do), so a saved plan holds exactly the procedure snapshot a mission applied.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3

import pytest

from agent_sessions import mission_plan_library as library
from agent_sessions import mission_procedures, missions
from agent_sessions.gallery import contract as gallery_contract
from test_mission_cross_project_apply import _apply_in, _deploy_beta
from test_mission_inputs import FakeForge, _pr
from test_mission_procedures import _client, _deploy, _files, _login
from test_mission_procedures import setup as _setup

setup = _setup


@pytest.fixture
def deployed(setup, tmp_path, monkeypatch):
    folder, _body = setup
    yield _deploy(folder, tmp_path, monkeypatch, _files())
    missions.reset_schema_cache_for_test()


def _apply(mid: str, flow_id: str = "dev", **kw) -> dict:
    listing = mission_procedures.listing(mid)
    index = next(p["index"] for p in listing["procedures"] if p["flow_id"] == flow_id)
    return mission_procedures.apply_listed(mid, index, listing["listing"], **kw)


def _saved(mid: str, revision: int | None = None, labels=None, **kw) -> dict:
    rev = revision or missions.plan_revision(mid)["revision"]
    found = missions.plan_revision(mid, rev)
    n = len({o["project_id"] for o in found["document"]["objectives"]}) if found else 1
    plan = library.save_from_mission(
        mid,
        rev,
        title=kw.get("title", "Ship it"),
        summary=kw.get("summary", ""),
        slot_labels=labels if labels is not None else [f"Slot {i + 1}" for i in range(n)],
    )
    return library.get(plan["id"])


# ---- save-as-plan -------------------------------------------------------------------------------


def test_a_saved_plan_keeps_the_procedure_and_names_no_project_or_value(deployed, monkeypatch):
    """A mission with a project name, a folder, a picked branch, a pull-request number and an
    instruction: none of them reaches the saved document."""
    fake = FakeForge([_pr(11, "feature/secret-branch", title="Add a")])
    monkeypatch.setattr(mission_procedures, "_forge_client", lambda: fake)
    mid = deployed["mission"]
    listing = mission_procedures.listing(mid)
    dev = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "dev")
    offered = mission_procedures.input_choices(mid, dev, listing["listing"])
    pick = next(c["index"] for c in offered["inputs"]["work_branch"] if c.get("number") == 11)
    mission_procedures.apply_listed(
        mid,
        dev,
        listing["listing"],
        inputs={"work_branch": {"choice": pick}},
        choices=offered["choices"],
    )
    plan = _saved(mid, labels=["Code"])
    doc = plan["document"]
    text = json.dumps(doc)
    project = deployed["project"]
    for leaked in (
        project.id,
        str(deployed["folder"]),
        "feature/secret-branch",
        "org/app",  # the deployment's bound repo value
        '"number"',
        "ship it",  # the mission's instruction
        mid,
    ):
        assert leaked not in text, leaked
    assert doc["slots"] == [{"id": "p1", "label": "Code"}]
    o = doc["objectives"][0]
    assert o["slot"] == "p1" and o["inputs"] == ["repo", "work_branch"]
    rev = missions.plan_revision(mid)["document"]
    assert o["procedure"] == rev["objectives"][0]["procedure"]
    out = library.public(plan, full=True)
    assert out["origin_kind"] == "mission" and "origin" not in out
    assert out["counts"] == {"objectives": 1, "slots": 1, "steps": 4}
    roles = {s["id"]: s["role"] for s in out["steps"]}
    assert roles["o1.plan"] == {"skills": ["forgejo-pr-flow"], "min_grade": "strong"}


def test_slots_follow_first_appearance_and_order_is_kept(setup, tmp_path, monkeypatch):
    folder, _body = setup
    base = _deploy(folder, tmp_path, monkeypatch, _files())
    try:
        mid = base["mission"]
        beta = _deploy_beta(tmp_path)
        _apply_in(mid, beta.id)  # o1 in Beta: the first project to appear
        _apply_in(mid, base["project"].id)  # o2 in the mission's own project
        listing = mission_procedures.listing(mid)
        notes = next(p["index"] for p in listing["procedures"] if p["flow_id"] == "notes")
        mission_procedures.apply_listed(mid, notes, listing["listing"], after=["o1", "o2"])
        with pytest.raises(missions.MissionError) as e:
            _saved(mid, labels=["Only one"])
        assert e.value.status == 422 and "each of the plan's 2 project slots" in str(e.value)
        doc = _saved(mid, labels=["Second repo", "Main repo"])["document"]
        assert [(o["id"], o["slot"]) for o in doc["objectives"]] == [
            ("o1", "p1"),
            ("o2", "p2"),
            ("o3", "p2"),
        ]
        assert doc["slots"][0]["label"] == "Second repo"
        assert doc["objectives"][2]["after"] == ["o1", "o2"]
        assert "after" not in doc["objectives"][0]
    finally:
        missions.reset_schema_cache_for_test()


def test_an_earlier_revision_is_saved_exactly_and_an_absent_one_is_a_404(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes")
    assert len(_saved(mid, revision=1)["document"]["objectives"]) == 1
    assert len(_saved(mid, revision=2)["document"]["objectives"]) == 2
    with pytest.raises(missions.MissionError) as e:
        _saved(mid, revision=3)
    assert e.value.status == 404
    for bad in (0, "1", True, None):
        with pytest.raises(missions.MissionError) as e:
            library.save_from_mission(mid, bad, title="x", summary="", slot_labels=["a"])
        assert e.value.status == 422
    unknown = "msn_" + "0" * 32
    with pytest.raises(missions.MissionError) as e:
        library.save_from_mission(unknown, 1, title="x", summary="", slot_labels=["a"])
    assert e.value.status == 404


def test_saving_changes_nothing_in_the_mission(deployed):
    """Inert: the save reads the mission and writes only the Library."""
    mid = deployed["mission"]
    _apply(mid, "dev")
    con = sqlite3.connect(missions._db_path())

    def snapshot():
        return [
            con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("missions", "mission_plan_revisions", "mission_objectives", "mission_plans")
        ] + [con.execute("SELECT state, updated_at FROM missions WHERE id=?", (mid,)).fetchone()]

    before = snapshot()
    _saved(mid, labels=["Code"])
    assert snapshot() == before
    con.close()


# ---- the format ---------------------------------------------------------------------------------


@pytest.fixture
def valid(deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    _apply(mid, "notes")
    return library.from_revision(
        missions.plan_revision(mid)["document"], title="Ship it", summary="", slot_labels=["Code"]
    )


def _break(doc: dict, how: str) -> dict:
    d = copy.deepcopy(doc)
    if how == "extra key":
        d["projects"] = ["p-1"]
    elif how == "format 2":
        d["format"] = 2
    elif how == "empty title":
        d["title"] = " "
    elif how == "two-line title":
        d["title"] = "a\nb"
    elif how == "long summary":
        d["summary"] = "x" * (library.SUMMARY_MAX + 1)
    elif how == "slot numbering":
        d["slots"][0]["id"] = "p2"
        for o in d["objectives"]:
            o["slot"] = "p2"
    elif how == "slot key":
        d["slots"][0]["project_id"] = "p-1"
    elif how == "unused slot":
        d["slots"].append({"id": "p2", "label": "Spare"})
    elif how == "unknown slot":
        d["objectives"][0]["slot"] = "p9"
    elif how == "forward after":
        d["objectives"][0]["after"] = ["o2"]
    elif how == "empty after":
        d["objectives"][1]["after"] = []
    elif how == "duplicate id":
        d["objectives"][1]["id"] = "o1"
    elif how == "bad digest":
        d["objectives"][0]["procedure"]["digest"] = "0" * 64
    elif how == "edited flow":
        d["objectives"][0]["procedure"]["flow"]["title"] = "Something else"
    elif how == "edited document":
        d["objectives"][0]["procedure"]["document"]["title"] = "Something else"
    elif how == "input values":
        d["objectives"][0]["inputs"] = [{"name": "repo", "value": "org/app"}]
    elif how == "duplicate input":
        d["objectives"][0]["inputs"] = ["repo", "repo"]
    elif how == "project id on objective":
        d["objectives"][0]["project_id"] = "p-1"
    elif how == "no objectives":
        d["objectives"] = []
    return d


@pytest.mark.parametrize(
    "how",
    [
        "extra key",
        "format 2",
        "empty title",
        "two-line title",
        "long summary",
        "slot numbering",
        "slot key",
        "unused slot",
        "unknown slot",
        "forward after",
        "empty after",
        "duplicate id",
        "bad digest",
        "edited flow",
        "edited document",
        "input values",
        "duplicate input",
        "project id on objective",
        "no objectives",
    ],
)
def test_the_validator_refuses(valid, how):
    library.validate(copy.deepcopy(valid))  # the unbroken document passes
    with pytest.raises(missions.MissionError) as e:
        library.validate(_break(valid, how))
    assert e.value.status == 422


def test_an_oversized_document_is_refused(valid, monkeypatch):
    monkeypatch.setattr(library, "DOCUMENT_MAX", 1000)
    with pytest.raises(missions.MissionError) as e:
        library.validate(valid)
    assert "larger than" in str(e.value)


def test_the_store_never_holds_a_plan_the_validator_refuses(valid):
    with pytest.raises(missions.MissionError):
        library.create(_break(valid, "unused slot"), origin={"kind": "file", "bundle": "x"})
    assert library.list_all(archived=True) == []


# ---- the store ----------------------------------------------------------------------------------


def test_ids_are_random_never_minted_from_the_title_and_never_reused(valid):
    a = library.create(valid, origin={"kind": "file", "bundle": "a"})
    b = library.create(valid, origin={"kind": "file", "bundle": "b"})
    for p in (a, b):
        assert re.fullmatch(r"plan-[0-9a-f]{8}", p["id"]) and "ship" not in p["id"]
    assert a["id"] != b["id"]
    with pytest.raises(missions.MissionError) as e:
        library.create(valid, origin={"kind": "file", "bundle": "c"}, pid=a["id"])
    assert e.value.status == 409
    c = library.create(valid, origin={"kind": "file", "bundle": "c"}, pid="chosen")
    assert c["id"] == "chosen" and c["version"] == 1
    assert library.ids() == {i: library.digest(valid) for i in (a["id"], b["id"], "chosen")}
    assert library.at("chosen") == library.digest(valid) and library.at("nope") is None


def test_archive_is_fenced_and_hides_the_plan_from_the_list(valid):
    plan = library.create(valid, origin={"kind": "file", "bundle": "a"})
    with pytest.raises(missions.MissionError) as e:
        library.set_archived(plan["id"], True, expect_revision=plan["revision"] + 1)
    assert e.value.status == 409
    for bad_archived, bad_rev in ((1, plan["revision"]), (True, "1")):
        with pytest.raises(missions.MissionError) as e:
            library.set_archived(plan["id"], bad_archived, expect_revision=bad_rev)
        assert e.value.status == 422
    done = library.set_archived(plan["id"], True, expect_revision=plan["revision"])
    assert done["archived"] and done["revision"] == plan["revision"] + 1
    assert library.list_all() == []
    assert [p["id"] for p in library.list_all(archived=True)] == [plan["id"]]
    back = library.set_archived(plan["id"], False, expect_revision=done["revision"])
    assert not back["archived"] and library.get(plan["id"])["document"] == valid


def test_a_version_is_never_rewritten_or_deleted(valid):
    plan = library.create(valid, origin={"kind": "file", "bundle": "a"})
    con = sqlite3.connect(library.db_path())
    for stmt in (
        "UPDATE plan_versions SET document = '{}' WHERE id = ?",
        "DELETE FROM plan_versions WHERE id = ?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(stmt, (plan["id"],))
    con.close()
    assert library.get(plan["id"])["document"] == valid


@pytest.mark.parametrize("warm", [False, True])
def test_a_store_from_a_newer_build_is_refused_and_left_untouched(valid, warm):
    """Cold: the schema check refuses it. Warm (this process already checked the file, and a newer
    build migrated it since): every connection asks again."""
    library.create(valid, origin={"kind": "file", "bundle": "a"})
    con = sqlite3.connect(library.db_path())
    con.execute("PRAGMA user_version=2")
    con.close()
    if not warm:
        library.reset_schema_cache_for_test()
    before = library.db_path().read_bytes()
    for call in (
        lambda: library.list_all(),
        lambda: library.create(valid, origin={"kind": "file", "bundle": "b"}),
    ):
        with pytest.raises(missions.MissionError) as e:
            call()
        assert e.value.status == 503 and "newer than this build" in str(e.value)
    assert library.db_path().read_bytes() == before


def test_the_library_is_bounded(valid, monkeypatch):
    monkeypatch.setattr(library, "PLANS_MAX", 2)
    library.create(valid, origin={"kind": "file", "bundle": "a"})
    library.create(valid, origin={"kind": "file", "bundle": "b"})
    with pytest.raises(missions.MissionError) as e:
        library.create(valid, origin={"kind": "file", "bundle": "c"})
    assert e.value.status == 409


def test_the_store_file_is_private(valid):
    library.create(valid, origin={"kind": "file", "bundle": "a"})
    assert library.db_path().stat().st_mode & 0o777 == 0o600


def test_the_v1_gallery_feed_still_takes_only_playbooks_and_skills():
    """#1729/D16: a mission plan never enters the v1 feed; P7a leaves its contract as it was."""
    with pytest.raises(gallery_contract.GalleryError):
        gallery_contract.content(
            {"format": 1, "kind": "mission-plan"}
            | dict.fromkeys(
                "id version title author publisher summary source requirements files".split(), "x"
            )
        )


# ---- the routes ---------------------------------------------------------------------------------


def test_the_routes(auth_cfg, deployed):
    mid = deployed["mission"]
    _apply(mid, "dev")
    c = _client(auth_cfg)
    assert c.get("/api/mission-plans").status_code == 401
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    url = f"/api/missions/{mid}/plan/save-as-plan"
    body = {"revision": 1, "title": "Ship it", "summary": "", "slot_labels": ["Code"]}
    assert c.post(url, json=body).status_code in (401, 403)
    for bad in ({**body, "project_id": "x"}, {**body, "slot_labels": []}, [1]):
        assert c.post(url, json=bad, headers=hdr).status_code == 422, bad
    r = c.post(url, json=body, headers=hdr)
    assert r.status_code == 201 and r.json()["version"] == 1
    pid = r.json()["id"]
    assert r.headers.get("cache-control") == "no-store"
    listed = c.get("/api/mission-plans").json()["plans"]
    assert [p["id"] for p in listed] == [pid] and "document" not in listed[0]
    one = c.get(f"/api/mission-plans/{pid}").json()
    assert one["document"]["title"] == "Ship it" and len(one["steps"]) == 4
    assert c.get("/api/mission-plans/nope").status_code == 404
    arch = f"/api/mission-plans/{pid}/archive"
    assert (
        c.post(arch, json={"archived": True, "expect_revision": 9}, headers=hdr).status_code == 409
    )
    r = c.post(arch, json={"archived": True, "expect_revision": 1}, headers=hdr)
    assert r.status_code == 200 and r.json()["archived"] is True
    assert c.get("/api/mission-plans").json()["plans"] == []
    assert len(c.get("/api/mission-plans?archived=1").json()["plans"]) == 1


def test_no_mission_plan_route_starts_anything(auth_cfg):
    """Inert by construction: the only routes this issue adds read, archive or save."""
    from agent_sessions.main import create_app

    app = create_app(auth_cfg)
    mine = sorted(
        (sorted(r.methods)[0], r.path)
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/mission-plans")
        or getattr(r, "path", "").endswith("/save-as-plan")
    )
    assert mine == [
        ("GET", "/api/mission-plans"),
        ("GET", "/api/mission-plans/{pid}"),
        ("POST", "/api/mission-plans/{pid}/archive"),
        ("POST", "/api/missions/{mission_id}/plan/save-as-plan"),
    ]


# ---- the independent review of S1 ---------------------------------------------------------------


@pytest.fixture
def deployed_with_default(setup, tmp_path, monkeypatch):
    from test_mission_procedures import HEALTH, MANIFEST

    folder, _body = setup
    contents = _files()
    contents["playbook.toml"] = MANIFEST.replace(
        "[[materials]]",
        '[[variables]]\nname = "health_url"\ntype = "url"\n'
        'default = "https://bundle-chosen.example/health"\n[[materials]]',
    )
    contents["flows/health.toml"] = HEALTH
    yield _deploy(folder, tmp_path, monkeypatch, contents)
    missions.reset_schema_cache_for_test()


def test_a_playbook_default_is_never_saved(deployed_with_default):
    """A reviewed default IS a deployed value: cleared from every declaration the plan keeps,
    whether the procedure uses it or not, and the kept procedure still validates."""
    mid = deployed_with_default["mission"]
    _apply(mid, "dev")
    doc = _saved(mid, labels=["Code"])["document"]
    assert "bundle-chosen.example" not in json.dumps(doc)
    declared = doc["objectives"][0]["procedure"]["variables"]
    assert declared and all(v["default"] is None for v in declared)
    assert {v["name"] for v in declared} >= {"repo", "work_branch", "health_url"}
    library.validate(doc)


@pytest.mark.parametrize("field", ["slot", "source"])
def test_an_unhashable_value_is_a_422_not_a_crash(valid, field):
    d = copy.deepcopy(valid)
    if field == "slot":
        d["objectives"][0]["slot"] = ["p1"]
    else:
        d["objectives"][0]["procedure"]["source"] = ["playbook"]
    with pytest.raises(missions.MissionError) as e:
        library.validate(d)
    assert e.value.status == 422


def test_a_damaged_row_never_hides_the_rest(valid):
    a = library.create(valid, origin={"kind": "file"})
    b = library.create(valid, origin={"kind": "file"})
    con = sqlite3.connect(library.db_path())
    con.execute(
        "DROP TRIGGER plan_versions_immutable"
    )  # a damaged file, not a write this app makes
    con.execute("UPDATE plan_versions SET origin = '[1]' WHERE id = ?", (a["id"],))
    con.execute(
        "UPDATE plan_versions SET document = ? WHERE id = ?",
        (json.dumps({**valid, "slots": [{"id": "p1", "label": ["x"]}]}), b["id"]),
    )
    con.commit()
    con.close()
    c = library.create(valid, origin={"kind": "file"})
    assert [p["id"] for p in library.list_all()] == [c["id"]]
    with pytest.raises(missions.MissionError) as e:
        library.get(a["id"])
    assert e.value.status == 503


@pytest.mark.parametrize(
    "how",
    ["empty label", "two-line label", "long label", "control in summary", "too much evidence"],
)
def test_more_refusals(valid, how, monkeypatch):
    d = copy.deepcopy(valid)
    if how == "empty label":
        d["slots"][0]["label"] = ""
    elif how == "two-line label":
        d["slots"][0]["label"] = "a\nb"
    elif how == "long label":
        d["slots"][0]["label"] = "x" * (library.LABEL_MAX + 1)
    elif how == "control in summary":
        d["summary"] = "fine\x1b[2Jnot fine"
    else:
        monkeypatch.setattr(missions, "OBJECTIVES_MAX", 2)
    with pytest.raises(missions.MissionError) as e:
        library.validate(d)
    assert e.value.status == 422


def test_a_summary_may_have_lines(valid):
    library.validate({**valid, "summary": "one\n\ttwo"})


def test_an_archive_that_changes_nothing_keeps_the_revision(valid):
    plan = library.create(valid, origin={"kind": "file"})
    same = library.set_archived(plan["id"], False, expect_revision=plan["revision"])
    assert same["revision"] == plan["revision"]


def test_the_save_reads_the_mission_through_its_admission(auth_cfg, deployed, monkeypatch):
    mid = deployed["mission"]
    _apply(mid, "dev")
    seen = []
    real = missions.run_admitted

    async def counted(fn):
        seen.append(fn)
        return await real(fn)

    monkeypatch.setattr(missions, "run_admitted", counted)
    c = _client(auth_cfg)
    hdr = {"X-CSRF-Token": _login(c, auth_cfg), "Origin": auth_cfg.origin}
    body = {"revision": 1, "title": "Ship it", "slot_labels": ["Code"]}
    r = c.post(f"/api/missions/{mid}/plan/save-as-plan", json=body, headers=hdr)
    assert r.status_code == 201 and len(seen) == 1
    big = {**body, "summary": "x" * (40 * 1024)}
    r = c.post(f"/api/missions/{mid}/plan/save-as-plan", json=big, headers=hdr)
    assert r.status_code == 413
    pid = library.list_all()[0]["id"]
    for path in ("/api/mission-plans", f"/api/mission-plans/{pid}"):
        assert c.get(path).headers.get("cache-control") == "no-store"
    r = c.post(
        f"/api/mission-plans/{pid}/archive",
        json={"archived": True, "expect_revision": 1},
        headers=hdr,
    )
    assert r.headers.get("cache-control") == "no-store"
