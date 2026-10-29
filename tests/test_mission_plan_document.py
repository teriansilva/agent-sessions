"""The plan document, schema v34 and its rollback (#1777, #1613 P4 slice 1, phase S1a)."""

from __future__ import annotations

import copy
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest

from agent_sessions import cli, missions, playbooks
from agent_sessions import mission_plan_document as plan_doc
from agent_sessions.playbooks import PlaybookFormatError, validate

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks"
SRC = Path(missions.__file__).parent


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "missions.db"
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(db))
    missions.reset_schema_cache_for_test()
    yield db
    missions.reset_schema_cache_for_test()


def _bundles() -> list[Path]:
    roots = [FIXTURES / "forge-workflow", FIXTURES / "inbox-triage"]
    roots += sorted(p for p in playbooks.BUNDLED_ROOT.iterdir() if p.is_dir())
    return roots


def _research() -> tuple[dict, dict]:
    """The bundled research flow (format 5) and its playbook's variables."""
    pb = playbooks.load_bundle(playbooks.BUNDLED_ROOT / "research-brief")
    (fid,) = pb["flows"]
    return pb, pb["flows"][fid]


def _research_doc() -> dict:
    """The research flow AS AUTHORED: what a snapshot stores and the store re-validates."""
    path = playbooks.BUNDLED_ROOT / "research-brief" / "flows" / "research.toml"
    return tomllib.loads(path.read_text())


def _objective(
    oid: str,
    doc: dict | None = None,
    *,
    variables: list[dict] | None = None,
    fid: str = "research",
    project: str = "p-0123456789ab",
    source: str = "playbook",
    manifest_format: int = 5,
):
    doc = _research_doc() if doc is None else doc
    variables = _research()[0]["variables"] if variables is None else variables
    flow = validate.flow_snapshot(
        doc,
        fid,
        variables,
        roles_only=source == "mission_local",
        manifest_format=manifest_format if source == "playbook" else None,
    )
    proc = {
        "source": source,
        "flow_id": fid,
        "title": flow["title"],
        "flow": flow,
        "digest": plan_doc.flow_digest(flow),
        "document": copy.deepcopy(doc),
        "variables": copy.deepcopy(variables),
    }
    if source == "playbook":
        proc |= {
            "playbook_id": "research-brief",
            "revision": "r1",
            "manifest_format": manifest_format,
        }
    return {"id": oid, "project_id": project, "procedure": proc, "inputs": []}


def _doc(*objectives) -> dict:
    return plan_doc.build(
        list(objectives), {o["id"]: "applied research-brief@r1" for o in objectives}
    )


# ---- schema v34 ---------------------------------------------------------------------------------


def _ddl(db: Path, name: str) -> str:
    con = sqlite3.connect(db)
    try:
        raw = con.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
    finally:
        con.close()
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", raw)).strip()


def _columns(db: Path, table: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return [tuple(r[1:]) for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _to_v33(db: Path) -> None:
    """Rewind a store to what a v33 build left: no plan table, no v34 columns."""
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER mission_plan_revisions_append_only")
    con.execute("DROP TABLE mission_plan_revisions")
    for name, _kind in missions.OBJECTIVE_COLUMNS_V34:
        con.execute(f"ALTER TABLE mission_objectives DROP COLUMN {name}")
    con.execute("PRAGMA user_version=33")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()


def test_a_fresh_store_and_an_upgraded_one_get_the_same_v34_schema(store):
    missions.create_mission("fresh", cwd="/tmp")
    fresh = {
        "table": _ddl(store, "mission_plan_revisions"),
        "trigger": _ddl(store, "mission_plan_revisions_append_only"),
        "columns": _columns(store, "mission_objectives"),
    }
    assert [c[0] for c in fresh["columns"][-3:]] == ["project_id", "plan_step", "pending"]
    _to_v33(store)
    missions.create_mission("upgraded", cwd="/tmp")  # forces the migration
    assert _ddl(store, "mission_plan_revisions") == fresh["table"]
    assert _ddl(store, "mission_plan_revisions_append_only") == fresh["trigger"]
    assert _columns(store, "mission_objectives") == fresh["columns"]
    con = sqlite3.connect(store)
    assert con.execute("PRAGMA user_version").fetchone()[0] == missions.SCHEMA_VERSION == 34
    con.close()


def test_the_v34_step_is_idempotent(store):
    missions.create_mission("x", cwd="/tmp")
    con = sqlite3.connect(store)
    missions._migrate_33_to_34(con)  # every column already there: converges, raises nothing
    missions._migrate_33_to_34(con)
    con.close()
    assert [c[0] for c in _columns(store, "mission_objectives")].count("project_id") == 1


def _seed_plan(mid: str, store: Path, doc: dict) -> int:
    con = missions._ready(store)
    try:
        con.execute("BEGIN IMMEDIATE")
        n = missions._append_plan_revision_tx(
            con, mid, doc, author_kind="playbook", reason="applied research-brief@r1", ts=1.0
        )
        con.execute("COMMIT")
    finally:
        con.close()
    return n


def test_a_revision_is_append_only(store):
    m = missions.create_mission("x", cwd="/tmp")
    _pb, flow = _research()
    doc = _doc(_objective("o1"))
    assert _seed_plan(m["id"], store, doc) == 1
    assert _seed_plan(m["id"], store, doc) == 2
    con = sqlite3.connect(store)
    with pytest.raises(sqlite3.IntegrityError, match="never edited"):
        con.execute("UPDATE mission_plan_revisions SET reason='rewritten' WHERE revision=1")
    with pytest.raises(sqlite3.IntegrityError):  # an "overwrite" is a PK collision, refused
        con.execute(
            "INSERT INTO mission_plan_revisions VALUES (?,?,?,?,?,?,?)",
            (m["id"], 1, "authored", "playbook", "again", "{}", 2.0),
        )
    con.close()
    latest = missions.plan_revision(m["id"])
    assert latest["revision"] == 2 and latest["document"] == doc
    assert missions.plan_revision(m["id"], 1)["reason"] == "applied research-brief@r1"
    assert missions.plan_revision(m["id"], 9) is None


def test_an_invalid_revision_is_refused_and_nothing_is_stored(store):
    m = missions.create_mission("x", cwd="/tmp")
    _pb, flow = _research()
    doc = _doc(_objective("o1"))
    doc["steps"][0]["title"] = "a step the procedure does not have"
    with pytest.raises(plan_doc.PlanError, match="does not match its procedure step"):
        _seed_plan(m["id"], store, doc)
    assert missions.plan_revision(m["id"]) is None


def test_plan_reads_are_per_mission_and_unknown_missions_are_404(store):
    a = missions.create_mission("a", cwd="/tmp")
    b = missions.create_mission("b", cwd="/tmp")
    _pb, flow = _research()
    _seed_plan(a["id"], store, _doc(_objective("o1")))
    assert missions.plan_revision(b["id"]) is None
    with pytest.raises(missions.MissionNotFound):
        missions.plan_revision("msn_" + "0" * 32)
    with pytest.raises(missions.MissionError):
        missions.plan_revision(a["id"], 0)


def test_revisions_leave_with_their_mission_and_leave_no_bytes(store):
    m = missions.create_mission("x", cwd="/tmp")
    doc = _research_doc()
    marker = "plan-scrub-marker-7f3a1c"
    doc["title"] = marker
    _seed_plan(m["id"], store, _doc(_objective("o1", doc)))
    missions.set_state(m["id"], "draft", "abandoned")
    missions.delete_mission(m["id"])
    con = sqlite3.connect(store)
    assert con.execute("SELECT COUNT(*) FROM mission_plan_revisions").fetchone()[0] == 0
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    for f in store.parent.glob(store.name + "*"):
        assert marker.encode() not in f.read_bytes(), f"{f.name} still holds the plan's text"


# ---- the document validator ---------------------------------------------------------------------


def test_the_steps_are_the_procedure_projected_with_roles_and_namespaced_ids():
    _pb, flow = _research()
    doc = _doc(_objective("o1"))
    ids = [s["id"] for s in doc["steps"]]
    assert ids == [f"o1.{s['id']}" for s in flow["steps"]]
    draft = next(s for s in doc["steps"] if s["procedure_step"] == "draft")
    assert draft["role"] == {"skills": ["research-with-citations"], "min_grade": "strong"}
    assert draft["after"] == ["o1.scope"]
    assert draft["evidence"][0].startswith("o1--")
    assert draft["envelope"] == {"reason": "applied research-brief@r1", "assignment": None}


def test_a_legacy_step_role_is_never_promoted_into_a_plan():
    pb = playbooks.load_bundle(FIXTURES / "inbox-triage")
    (fid,) = pb["flows"]
    flow = pb["flows"][fid]
    assert flow["steps"][0]["role"]["legacy_hint"]  # the read-only view exists…
    authored = tomllib.loads((FIXTURES / "inbox-triage/flows" / f"{fid}.toml").read_text())
    doc = _doc(
        _objective("o1", authored, variables=pb["variables"], fid=fid, manifest_format=pb["format"])
    )
    assert all(s["role"] is None for s in doc["steps"])  # …and binds nothing


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d["objectives"][0].update(project_id=""), "names a project"),
        (lambda d: d["objectives"][0]["procedure"].update(digest="sha256:00"), "digest"),
        (
            lambda d: d["objectives"].append(copy.deepcopy(d["objectives"][0])),
            "duplicate objective",
        ),
        (lambda d: d["steps"][0]["envelope"].update(assignment="spc_x"), "assigned in P5"),
        (lambda d: d["steps"][0]["envelope"].update(reason="two\nlines"), "one line"),
        (lambda d: d["steps"].pop(), "exactly the objectives' procedure steps"),
        (lambda d: d["steps"][1].update(after=[]), "does not match"),
        (lambda d: d.update(format=2), "format"),
        (lambda d: d["objectives"][0].update(extra=1), "exactly"),
        (lambda d: d["objectives"][0]["procedure"].update(source="hq"), "source"),
    ],
)
def test_the_validator_refuses_a_document_that_disagrees_with_itself(mutate, reason):
    _pb, flow = _research()
    doc = copy.deepcopy(_doc(_objective("o1")))
    mutate(doc)
    with pytest.raises(plan_doc.PlanError, match=reason):
        plan_doc.validate_document(doc)


def _forged(mutate) -> dict:
    """A snapshot tampered with and RE-DIGESTED, so only re-validation can see it."""
    doc = copy.deepcopy(_doc(_objective("o1")))
    proc = doc["objectives"][0]["procedure"]
    mutate(proc)
    proc["digest"] = plan_doc.flow_digest(proc["flow"])
    return doc


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda p: p["flow"]["steps"][1]["checklist"][0].update(
                probe="arbitrary_unvalidated_probe"
            ),
            "not its document's validated form",
        ),
        (
            lambda p: (
                p["document"]["steps"][1]["checklist"][0].update(
                    probe="arbitrary_unvalidated_probe"
                ),
                p["flow"]["steps"][1]["checklist"][0].update(probe="arbitrary_unvalidated_probe"),
            ),
            "not a valid flow",
        ),
        (lambda p: p["variables"][0].update(type="enum"), "not a valid flow"),
        (lambda p: p["variables"][0].update(label=7), "not a valid flow"),
        (lambda p: p["variables"][0].pop("help"), "not normalized"),
        (
            lambda p: p["document"].update(when=__import__("datetime").date(2026, 1, 1)),
            "plain JSON",
        ),
    ],
    ids=[
        "normalized-flow",
        "authored-and-normalized",
        "declaration-type",
        "declaration-shape",
        "declaration-not-normalized",
        "non-json",
    ],
)
def test_a_tampered_snapshot_is_refused_and_never_persisted(store, mutate, reason):
    """Hermes on #1781, finding 1: a digest only proves the caller hashed what it sent."""
    m = missions.create_mission("x", cwd="/tmp")
    doc = _forged(mutate)
    with pytest.raises(plan_doc.PlanError, match=reason):
        plan_doc.validate_document(doc)
    with pytest.raises(plan_doc.PlanError, match=reason):
        _seed_plan(m["id"], store, doc)
    assert missions.plan_revision(m["id"]) is None


def _long_flow(*keys: str) -> dict:
    return {
        "format": 5,
        "title": "Long",
        "steps": [
            {
                "id": "s",
                "title": "s",
                "actor": {"kind": "agent"},
                "checklist": [{"key": k, "title": "t", "probe": "supervisor_judged"} for k in keys],
            }
        ],
    }


def test_the_longest_item_key_a_flow_may_declare_becomes_evidence_end_to_end(store, monkeypatch):
    """Hermes on #1781, finding 2: the flow validator admits a 64-character key, so the minted
    evidence key for it must fit an objective row — and stay addressable after a rollback, where
    a v33 build caps every key it is sent at 64 (the independent review on #1781)."""
    from agent_sessions.playbooks import schema

    assert missions.OBJECTIVE_KEY_MAX == 64  # NOT raised: a v33 build could not address more
    a = "k" * schema.ITEM_KEY_MAX
    b = "k" * (schema.ITEM_KEY_MAX - 1) + "j"  # same long prefix, a different key
    doc = _long_flow(a, b)
    validate.flow_document(doc, "long", [])
    oid = f"o{plan_doc.MAX_OBJECTIVES}"
    built = _doc(_objective(oid, doc, variables=[], fid="long"))
    keys = built["steps"][0]["evidence"]
    assert len(set(keys)) == 2
    assert all(len(k) == missions.OBJECTIVE_KEY_MAX for k in keys)
    assert all(re.fullmatch(r"o20--k+-[0-9a-f]{12}", k) for k in keys)
    assert plan_doc.evidence_key("o1", "short") == "o1--short"  # a key that fits is unchanged

    m = missions.create_mission("x", cwd="/tmp")
    con = missions._ready(store)  # the procedure writer's own insert: the namespace is reserved
    try:
        con.execute("BEGIN IMMEDIATE")
        for k in keys:
            op = {"key": k, "title": "t", "probe": "supervisor_judged", "gate": True}
            missions._op_add(con, m["id"], op, "playbook", 1.0, placement={"plan_step": "o20.s"})
        con.execute("COMMIT")
    finally:
        con.close()
    assert [o["key"] for o in missions.objectives(m["id"])] == keys
    # Rolled back, a v33 build (pinned) can still waive and drop it.
    missions.rollback_schema(33, path=store)
    monkeypatch.setattr(missions, "SCHEMA_VERSION", 33)
    missions.reset_schema_cache_for_test()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": keys[0]}])
    missions.patch_objectives(m["id"], [{"op": "drop", "key": keys[1]}])
    assert [(o["key"], o["state"]) for o in missions.objectives(m["id"])] == [(keys[0], "waived")]


def test_a_document_that_is_not_encodable_json_is_a_plan_error_not_a_crash():
    """The independent review on #1781: a lone surrogate parses from JSON but cannot be encoded,
    and deep nesting overflows recursion. Both must be the validator's own 422."""
    doc = _research_doc()
    doc["title"] = "R \ud800"
    with pytest.raises(plan_doc.PlanError, match="plain JSON"):
        plan_doc.validate_document(
            {"format": 1, "objectives": [{"procedure": {"document": doc}}], "steps": []}
        )
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(plan_doc.PlanError, match="plain JSON"):
        plan_doc.validate_document({"format": 1, "objectives": [deep], "steps": []})
    with pytest.raises(plan_doc.PlanError, match="plain JSON"):
        plan_doc.validate_document({"format": 1, "objectives": [float("nan")], "steps": []})


def test_an_oversized_document_is_refused():
    doc = _research_doc()
    doc["description"] = "x" * (plan_doc.DOCUMENT_MAX + 1)
    o = _objective("o1")
    o["procedure"]["document"] = doc
    with pytest.raises(plan_doc.PlanError, match="larger than"):
        _doc(o)


def test_a_revision_can_be_neither_replaced_nor_deleted_on_its_own(store):
    m = missions.create_mission("x", cwd="/tmp")
    _seed_plan(m["id"], store, _doc(_objective("o1")))
    con = sqlite3.connect(store)
    con.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
        con.execute(
            "INSERT OR REPLACE INTO mission_plan_revisions VALUES (?,?,?,?,?,?,?)",
            (m["id"], 1, "authored", "playbook", "rewritten", "{}", 2.0),
        )
    with pytest.raises(sqlite3.IntegrityError, match="only with its mission"):
        con.execute("DELETE FROM mission_plan_revisions WHERE mission_id=?", (m["id"],))
    con.close()
    assert missions.plan_revision(m["id"])["reason"] == "applied research-brief@r1"
    missions.set_state(m["id"], "draft", "abandoned")
    assert missions.delete_mission(m["id"])  # …and the cascade still takes it
    con = sqlite3.connect(store)
    assert con.execute("SELECT COUNT(*) FROM mission_plan_revisions").fetchone()[0] == 0
    con.close()


def test_step_state_uses_the_stores_completion_predicate():
    """A `met` row whose latest observation no longer supports it is unmet for `gate_tally`, so the
    step is not done either: one question, one answer (#897)."""
    doc = _doc(_objective("o1"))
    step = next(s for s in doc["steps"] if s["evidence"])
    rows = [
        {"key": k, "gate": True, "state": "met", "probe": "git_local", "observed": {"value": False}}
        for k in step["evidence"]
    ]
    assert missions.gate_tally(rows)[1] == len(rows)
    assert plan_doc.step_states(doc, rows)[step["id"]] != "done"
    for r in rows:
        r["observed"] = {"value": True, "observed": True}
    assert missions.gate_tally(rows)[1] == 0
    assert plan_doc.step_states(doc, rows)[step["id"]] == "done"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda o: o.update(project_id="P X"), "names a project"),
        (lambda o: o.update(inputs=[{"name": "a", "source": "x"}] * 2), "distinct"),
        (lambda o: o.update(inputs=[{"name": "Bad Name", "source": "x"}]), "distinct"),
        (lambda o: o.update(inputs=[{"name": "a", "source": "x" * 40}]), "distinct"),
    ],
)
def test_objective_project_and_inputs_are_shape_checked(mutate, reason):
    o = _objective("o1")
    mutate(o)
    with pytest.raises(plan_doc.PlanError, match=reason):
        _doc(o)


def _endpoint(**kw) -> list[dict]:
    raw = [{"name": "endpoint", "type": "url", **kw}]
    return list(validate._variables(validate._Reader({"variables": raw}, "")).values())


_PROBE_FLOW = {
    "format": 5,
    "title": "Probe",
    "steps": [
        {
            "id": "s",
            "title": "s",
            "actor": {"kind": "agent"},
            "checklist": [
                {
                    "key": "up",
                    "title": "up",
                    "probe": "http_status",
                    "probe_args": {"url": "{{endpoint}}"},
                }
            ],
        }
    ],
}


@pytest.mark.parametrize("kw", [{"default": "https://example.invalid/"}], ids=["default"])
def test_a_mission_local_target_variable_must_be_typed_by_the_operator(store, kw):
    """Hermes on #1781 (7390): the bundle's target-variable rule applies to a mission-local
    procedure and to the snapshot write path — no definition may preselect a probe target."""
    with pytest.raises(PlaybookFormatError, match="must be typed by the operator"):
        validate.flow_document(copy.deepcopy(_PROBE_FLOW), "probe", _endpoint(**kw))
    m = missions.create_mission("x", cwd="/tmp")
    good = _objective(
        "o1", copy.deepcopy(_PROBE_FLOW), variables=_endpoint(), fid="probe", source="mission_local"
    )
    forged = copy.deepcopy(good)
    forged["procedure"]["variables"] = _endpoint(**kw)  # the flow itself is unchanged
    doc = plan_doc.build([good], {"o1": "r"})
    doc["objectives"][0] = forged
    with pytest.raises(plan_doc.PlanError, match="must be typed by the operator"):
        _seed_plan(m["id"], store, doc)
    assert missions.plan_revision(m["id"]) is None
    # The control: the same procedure with an operator-typed target variable is accepted.
    assert _seed_plan(m["id"], store, plan_doc.build([good], {"o1": "r"})) == 1


_FORMAT1_PROBE = {
    "format": 1,
    "title": "Probe",
    "steps": [
        {
            "id": "s",
            "title": "s",
            "actor": {"kind": "operator"},
            "checklist": [
                {
                    "key": "up",
                    "title": "up",
                    "probe": "http_status",
                    "probe_args": {"url": "{{endpoint}}"},
                }
            ],
        }
    ],
}


@pytest.mark.parametrize(
    ("manifest", "flow_format", "refused"),
    [(2, 1, False), (1, 2, True), (1, 1, True), (2, 2, False)],
    ids=["manifest2-flow1", "manifest1-flow2", "manifest1-flow1", "manifest2-flow2"],
)
def test_a_deployed_snapshot_keys_the_target_rule_on_its_manifest_format(
    manifest, flow_format, refused
):
    """The independent review on #1781: the bundle loader applies the format-1 target-variable
    rule by the MANIFEST's format, so a deployed snapshot must too — a format 2 manifest's
    confirmed default stays applicable even in a format 1 flow, and the reverse stays refused."""
    doc = {**copy.deepcopy(_FORMAT1_PROBE), "format": flow_format}
    variables = _endpoint(default="https://example.invalid/")
    if refused:
        with pytest.raises(PlaybookFormatError, match="must be typed by the operator"):
            validate.flow_snapshot(
                doc, "probe", variables, roles_only=False, manifest_format=manifest
            )
    else:
        o = _objective("o1", doc, variables=variables, fid="probe", manifest_format=manifest)
        assert _doc(o)["objectives"][0]["procedure"]["manifest_format"] == manifest


@pytest.mark.parametrize("bad", [None, 0, 6, True, "5"])
def test_a_deployed_snapshot_must_name_a_supported_manifest_format(bad):
    o = _objective("o1")
    o["procedure"]["manifest_format"] = bad
    with pytest.raises(plan_doc.PlanError, match="manifest_format"):
        _doc(o)


def test_a_revision_reason_must_be_encodable(store):
    m = missions.create_mission("x", cwd="/tmp")
    con = missions._ready(store)
    try:
        con.execute("BEGIN IMMEDIATE")
        with pytest.raises(plan_doc.PlanError, match="UTF-8"):
            missions._append_plan_revision_tx(
                con,
                m["id"],
                _doc(_objective("o1")),
                author_kind="playbook",
                reason="r \ud800",
                ts=1.0,
            )
        con.execute("ROLLBACK")
    finally:
        con.close()


def test_two_objectives_never_share_an_evidence_key():
    _pb, flow = _research()
    doc = _doc(_objective("o1"), _objective("o2"))
    keys = [k for s in doc["steps"] for k in s["evidence"]]
    assert len(keys) == len(set(keys))


def test_the_objective_bound_is_enforced():
    note = {
        "format": 5,
        "title": "Note",
        "steps": [{"id": "n", "title": "n", "actor": {"kind": "none"}}],
    }
    objs = [_objective(f"o{i}", note) for i in range(1, plan_doc.MAX_OBJECTIVES + 2)]
    with pytest.raises(plan_doc.PlanError, match=f"at most {plan_doc.MAX_OBJECTIVES} objectives"):
        _doc(*objs)


def test_a_plan_never_promises_more_evidence_rows_than_a_mission_holds():
    """Hermes on #1781: eleven research objectives are 55 evidence items; a mission holds 50."""
    per = len(_doc(_objective("o1"))["steps"][0]["evidence"]) or 1
    total = sum(len(s["evidence"]) for s in _doc(_objective("o1"))["steps"])
    n = missions.OBJECTIVES_MAX // total + 1
    assert per and n <= plan_doc.MAX_OBJECTIVES
    with pytest.raises(plan_doc.PlanError, match="evidence items"):
        _doc(*[_objective(f"o{i}") for i in range(1, n + 1)])
    _doc(*[_objective(f"o{i}") for i in range(1, n)])  # one fewer fits


# ---- one flow validator for bundles and mission-local procedures --------------------------------


@pytest.mark.parametrize("root", _bundles(), ids=lambda p: p.name)
def test_the_factored_flow_validator_gives_the_bundle_loader_byte_identical_flows(root):
    pb = playbooks.load_bundle(root)
    variables = {v["name"]: v for v in pb["variables"]}
    for fid, flow in pb["flows"].items():
        doc = tomllib.loads((root / "flows" / f"{fid}.toml").read_text())
        doc = validate._format(doc, fid)
        assert validate._flow_doc(doc, fid, fid, variables, {}) == flow
        if flow["format"] >= 5:
            assert validate.flow_document(doc, fid, pb["variables"]) == flow


def _local(**changes) -> dict:
    root = playbooks.BUNDLED_ROOT / "research-brief"
    doc = tomllib.loads((root / "flows" / "research.toml").read_text())
    for step_id, patch in changes.items():
        step = next(s for s in doc["steps"] if s["id"] == step_id)
        step.update(patch)
    return doc


def _vars() -> list[dict]:
    pb, _flow = _research()
    return pb["variables"]


def test_a_mission_local_procedure_passes_the_same_validator():
    flow = validate.flow_document(_local(), "my-research", _vars())
    assert flow["id"] == "my-research" and flow["format"] == 5


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        (lambda: _local(scope={"after": ["draft"]}), "cycle"),
        (
            lambda: {**_local(), "steps": _local()["steps"] + [_local()["steps"][0]]},
            "duplicate step",
        ),
        (
            lambda: {
                **_local(),
                "steps": [
                    {"id": f"s{i}", "title": "t", "actor": {"kind": "none"}} for i in range(31)
                ],
            },
            "more than 30",
        ),
        (
            lambda: _local(
                draft={
                    "checklist": [
                        {
                            "key": "k",
                            "title": "t",
                            "probe": "http_status",
                            "probe_args": {"url": "https://example.invalid"},
                        }
                    ]
                }
            ),
            "literal probe target",
        ),
        (
            lambda: _local(
                scope={
                    "checklist": [
                        {
                            "key": "k",
                            "title": "t",
                            "probe": "git_local",
                            "probe_args": {"branch": "{{steps.draft.branch}}"},
                        }
                    ]
                }
            ),
            "not an ancestor",
        ),
        (lambda: {**_local(), "format": 4}, "format 5"),
        (lambda: {**_local(), "surprise": True}, "unknown field"),
    ],
    ids=[
        "cycle",
        "duplicate-id",
        "31-steps",
        "literal-target",
        "non-ancestor-ref",
        "format-4",
        "unknown-field",
    ],
)
def test_the_mission_local_refusals(doc, reason):
    with pytest.raises(PlaybookFormatError, match=reason):
        validate.flow_document(doc(), "my-research", _vars())


def test_a_mission_local_procedure_cannot_reference_an_undeclared_variable():
    with pytest.raises(PlaybookFormatError, match="undeclared"):
        validate.flow_document(_local(), "my-research", [])


# ---- the dual read ------------------------------------------------------------------------------


def test_an_objective_reads_its_own_project_else_its_missions(store):
    m = missions.create_mission("x", cwd="/repo", project_id="p-aaaaaaaaaaaa")
    missions.patch_objectives(m["id"], [{"op": "add", "key": "legacy", "title": "t"}])
    con = sqlite3.connect(store)
    con.execute(
        "INSERT INTO mission_objectives (mission_id, key, ord, title, probe, gate, state, source, "
        "incarnation, project_id, plan_step) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            m["id"],
            "o1--own",
            5,
            "own",
            "none",
            1,
            "pending",
            "playbook",
            "inc",
            "p-bbbbbbbbbbbb",
            "o1.scope",
        ),
    )
    con.commit()
    con.close()
    rows = {o["key"]: o for o in missions.objectives(m["id"])}
    assert rows["legacy"]["project_id"] == "p-aaaaaaaaaaaa"  # NULL column: the mission's
    assert rows["legacy"]["plan_step"] is None and rows["legacy"]["pending"] is None
    assert rows["o1--own"]["project_id"] == "p-bbbbbbbbbbbb"  # its own
    got = {o["key"]: o for o in missions.get_mission(m["id"])["objectives"]}
    assert got["legacy"]["project_id"] == "p-aaaaaaaaaaaa"
    assert got["o1--own"]["plan_step"] == "o1.scope"


# ---- rollback -----------------------------------------------------------------------------------


def test_rollback_to_33_keeps_every_byte_and_this_build_converges_again(store, monkeypatch):
    m = missions.create_mission("x", cwd="/repo", project_id="p-aaaaaaaaaaaa")
    _pb, flow = _research()
    doc = _doc(_objective("o1", project="p-aaaaaaaaaaaa"))
    _seed_plan(m["id"], store, doc)
    missions.patch_objectives(m["id"], [{"op": "add", "key": "kept", "title": "kept"}])
    files_before = sorted(p.name for p in store.parent.iterdir())

    assert missions.rollback_schema(33, path=store) == {"from": 34, "to": 33}
    con = sqlite3.connect(store)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 33
    # Nothing dropped: the v34 table, its row and the v34 columns are all still there.
    assert con.execute("SELECT COUNT(*) FROM mission_plan_revisions").fetchone()[0] == 1
    assert {"project_id", "plan_step", "pending"} <= {
        r[1] for r in con.execute("PRAGMA table_info(mission_objectives)")
    }
    con.close()
    # …and no copy was made: a copy would hold mission text `delete_mission` can never scrub.
    assert sorted(p.name for p in store.parent.iterdir()) == files_before

    # THE v33 CODE PATHS against the rolled-back file: this build pinned to schema 33, so it must
    # not re-upgrade, making the reads and named-column writes a v33 build makes.
    monkeypatch.setattr(missions, "SCHEMA_VERSION", 33)
    missions.reset_schema_cache_for_test()
    other = missions.create_mission("made while rolled back", cwd="/repo")
    missions.patch_objectives(
        other["id"],
        [
            {"op": "add", "key": "a", "title": "a", "gate": True},
            {"op": "add", "key": "b", "title": "b"},
        ],
    )
    missions.patch_objectives(other["id"], [{"op": "drop", "key": "b"}])
    assert [o["key"] for o in missions.objectives(other["id"])] == ["a"]
    assert [o["key"] for o in missions.get_mission(m["id"])["objectives"]] == ["kept"]
    missions.set_state(other["id"], "draft", "abandoned")
    assert missions.delete_mission(other["id"])
    con = sqlite3.connect(store)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 33  # still not re-upgraded
    con.close()

    # This build opens it again: the idempotent step re-runs and the data is exactly as it was.
    monkeypatch.setattr(missions, "SCHEMA_VERSION", 34)
    missions.reset_schema_cache_for_test()
    assert missions.plan_revision(m["id"])["document"] == doc
    assert [o["key"] for o in missions.objectives(m["id"])] == ["kept"]
    con = sqlite3.connect(store)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 34
    con.close()
    # …and back once more (v33 → v34 → v33 → v34): the round trip repeats.
    assert missions.rollback_schema(33, path=store) == {"from": 34, "to": 33}
    missions.reset_schema_cache_for_test()
    assert missions.plan_revision(m["id"])["revision"] == 1


def test_rollback_refuses_what_it_cannot_do(store, tmp_path):
    missions.create_mission("x", cwd="/repo")
    with pytest.raises(missions.MissionError, match="cannot roll"):
        missions.rollback_schema(32, path=store)
    missions.rollback_schema(33, path=store)
    with pytest.raises(missions.MissionError, match="nothing to roll back"):
        missions.rollback_schema(33, path=store)
    with pytest.raises(missions.MissionError, match="no missions store"):
        missions.rollback_schema(33, path=tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_the_cli_rolls_back_and_reports(store, capsys):
    missions.create_mission("x", cwd="/repo")
    assert cli.main(["missions-schema", "status", "--db", str(store)]) == 0
    assert "schema 34" in capsys.readouterr().out
    assert cli.main(["missions-schema", "rollback", "--to", "33", "--db", str(store)]) == 0
    out = capsys.readouterr().out
    assert "34 → 33" in out and "Nothing was deleted" in out
    assert cli.main(["missions-schema", "rollback", "--to", "33", "--db", str(store)]) == 2


def test_every_objective_insert_names_its_columns():
    """A v33 build reads a rolled-back store that has three more columns. That is safe only while
    no insert relies on column POSITION — so none may, in any module."""
    offenders = []
    for f in sorted(SRC.rglob("*.py")):
        text = f.read_text()
        for m in re.finditer(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+mission_objectives\b", text):
            if text[m.start() - 1] == "`":  # prose naming the statement, not SQL
                continue
            # SQL is assembled from adjacent string literals: drop quotes and whitespace, then the
            # next thing after the table name must be the column list.
            tail = re.sub(r'["\s]', "", text[m.end() : m.end() + 200])
            if not tail.startswith("("):
                offenders.append(f"{f.name}:{text[: m.start()].count(chr(10)) + 1}")
    assert offenders == []


def test_this_build_still_refuses_a_newer_store(store):
    missions.create_mission("x", cwd="/repo")
    con = sqlite3.connect(store)
    con.execute("PRAGMA user_version=35")
    con.commit()
    con.close()
    missions.reset_schema_cache_for_test()
    with pytest.raises(missions.MissionError, match="newer than this build"):
        missions.objectives("msn_" + "0" * 32)
    with pytest.raises(missions.MissionError, match="newer"):
        missions.rollback_schema(33, path=store)
