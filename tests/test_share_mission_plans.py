"""Share bundles carry mission plans (#1924, #1613 P7a): a Library mission plan travels as its
saved document only, its own text scanned at export; the import validates it with the Library's
own validator and creates an inert version 1 — a new copy, never an overwrite."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_sessions import mission_plan_document as plan_doc
from agent_sessions import mission_plan_library as library
from agent_sessions import missions, variables
from agent_sessions.playbooks import loader, validate
from agent_sessions.sharebundle import format as fmt
from agent_sessions.sharebundle import kinds, plan_kinds, service

SECRET = "s3cr3t-Value-for-the-deploy-0042"

FLOW = {
    "format": 5,
    "title": "Check it",
    "steps": [
        {
            "id": "check",
            "title": "Check",
            "actor": {"kind": "agent"},
            "checklist": [{"key": "done", "title": "Done", "probe": "supervisor_judged"}],
        },
        {
            "id": "sign",
            "title": "Sign off",
            "actor": {"kind": "operator"},
            "after": ["check"],
            "checklist": [{"key": "ok", "title": "Signed", "probe": "none"}],
        },
    ],
}


def _procedure(flow_id: str = "check-it", flow_doc: dict | None = None) -> dict:
    document = copy.deepcopy(flow_doc or FLOW)
    flow = validate.flow_snapshot(document, flow_id, [], roles_only=True, manifest_format=None)
    return {
        "source": "mission_local",
        "flow_id": flow_id,
        "title": flow["title"],
        "flow": flow,
        "digest": plan_doc.flow_digest(flow),
        "document": document,
        "variables": [],
    }


def _doc(title: str = "Weekly check", **over) -> dict:
    doc = {
        "format": 1,
        "title": title,
        "summary": "Check, then sign off, in two places.",
        "slots": [{"id": "p1", "label": "Service"}, {"id": "p2", "label": "Docs"}],
        "objectives": [
            {"id": "o1", "slot": "p1", "procedure": _procedure(), "inputs": []},
            {
                "id": "o2",
                "slot": "p2",
                "procedure": _procedure("check-docs"),
                "inputs": [],
                "after": ["o1"],
            },
        ],
    }
    doc.update(over)
    return library.validate(doc)


def _install(root: Path, monkeypatch) -> Path:
    """One BattleLab install: its own app state, Library stores and variables."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(root / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(root / "playbooks"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATES", str(root / "cfg" / "templates.json"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_VARS", str(root / "cfg" / "vars.json"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_SECRETS_KEY", str(root / "cfg" / "secrets.key"))
    monkeypatch.setenv("AGENT_SESSIONS_MISSION_PLANS_DB", str(root / "cfg" / "mission-plans.db"))
    library.reset_schema_cache_for_test()
    (root / "home").mkdir(exist_ok=True)
    empty = root / "bundled"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(loader, "BUNDLED_ROOT", empty)
    return root


@pytest.fixture
def install(tmp_path, monkeypatch):
    return _install(tmp_path / "one", monkeypatch)


def _second(tmp_path, monkeypatch) -> Path:
    return _install(tmp_path / "two", monkeypatch)


def _export(*items: tuple[str, str]) -> bytes:
    _, data = service.export([{"kind": k, "id": i} for k, i in items])
    return data


def _plan(preview: dict) -> dict:
    return {i["key"]: i for i in preview["items"]}


def _keys(preview: dict) -> list[str]:
    return [i["key"] for i in preview["items"] if i["status"] not in ("refused", "reused")]


def _saved(doc: dict | None = None, **origin) -> str:
    return library.create(
        doc or _doc(), origin={"kind": "mission", "mission_id": "msn_x", "revision": 3, **origin}
    )["id"]


def _file(pid: str, doc: dict) -> bytes:
    """A bundle carrying `doc` as it is — what a hostile or a newer install could send."""
    text = json.dumps(doc).encode()
    return fmt.write(
        [
            {
                "kind": "mission-plan",
                "id": pid,
                "title": "x",
                "files": {"mission-plan.json": text},
                "depends_on": [],
            }
        ],
        created_at="2026-11-10T00:00:00Z",
        app_version="test",
    )


# ---- export -------------------------------------------------------------------------------------


def test_a_plan_exports_as_its_document_only(install):
    pid = _saved()
    data = _export(("mission-plan", pid))
    bundle = fmt.read(data)
    item = bundle.item(f"mission-plan:{pid}")
    assert [i.key for i in bundle.items] == [f"mission-plan:{pid}"]
    assert set(item.files) == {"mission-plan.json"} and item.depends_on == ()
    assert json.loads(item.files["mission-plan.json"]) == _doc()
    assert b"msn_x" not in data  # the saving mission (the local origin) never leaves


def test_an_archived_plan_is_not_offered_or_exported(install):
    pid = _saved()
    library.set_archived(pid, True, expect_revision=1)
    assert not [r for r in kinds.catalog() if r["kind"] == "mission-plan"]
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert err.value.status == 422 and "archived" in err.value.detail


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"summary": f"Use {SECRET} now"}, "plan summary"),
        (
            {"slots": [{"id": "p1", "label": SECRET[:40]}, {"id": "p2", "label": "Docs"}]},
            "plan slot label",
        ),
    ],
)
def test_a_plan_holding_a_stored_secret_is_never_exported(install, over, field):
    variables.create({"name": "deploy_token", "kind": "secret", "definition": {}, "value": SECRET})
    pid = _saved(_doc(**over))
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert err.value.status == 422
    assert f"its {field} holds one of your stored secrets" in err.value.detail
    assert SECRET not in err.value.detail
    row = next(r for r in kinds.catalog() if r["kind"] == "mission-plan")
    assert row["exportable"] is False and SECRET not in json.dumps(row)


PASSPHRASE = "correct horse battery staple deploy phrase"


def test_a_title_holding_a_secret_refuses_the_export_and_is_never_shown(install):
    variables.create({"name": "phrase", "kind": "secret", "definition": {}, "value": PASSPHRASE})
    pid = _saved(_doc(title=f"Run {PASSPHRASE}"))
    assert pid.startswith("plan-")  # the id names nothing: it is never minted from the title
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert "its plan title holds one of your stored secrets" in err.value.detail
    assert "save or import it again" in err.value.detail
    text = json.dumps(service.catalog()) + json.dumps(service.imports_view())
    assert "horse" not in text.lower()
    row = next(r for r in kinds.catalog() if r["kind"] == "mission-plan")
    assert row["exportable"] is False and row["title"] == pid


def test_an_imported_plan_whose_id_holds_a_secret_is_withheld_everywhere(install):
    """A file's plan id is the sender's choice: case-folded, it is judged as an identifier."""
    pid = "x-correct-horse-battery-staple-deploy-phrase"
    preview = service.preview(_file(pid, _doc()))
    service.confirm(preview["token"], _keys(preview))
    variables.create(
        {"name": "phrase", "kind": "secret", "definition": {}, "value": PASSPHRASE.upper()}
    )
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert "(its ID withheld)" in err.value.detail and "horse" not in err.value.detail
    text = json.dumps(service.catalog()) + json.dumps(service.imports_view())
    assert "horse" not in text.lower()
    row = next(r for r in kinds.catalog() if r["kind"] == "mission-plan")
    assert row["id"].startswith("~withheld-") and row["title"] == "Mission plan (ID withheld)"


def test_a_credential_shaped_summary_is_refused(install):
    pid = _saved(_doc(summary="token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"))
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert "looks like a credential" in err.value.detail


def test_with_no_secret_inventory_no_plan_exports_or_shows_its_text(install, monkeypatch):
    from agent_sessions import template_secrets

    pid = _saved()

    def unavailable():
        raise template_secrets.RedactionUnavailable("no")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    with pytest.raises(service.ShareError):
        _export(("mission-plan", pid))
    rows = [r for r in kinds.catalog() if r["kind"] == "mission-plan"]
    assert [r["title"] for r in rows] == ["Mission plan (unavailable)"]
    assert "Weekly check" not in json.dumps(service.catalog())


# ---- import -------------------------------------------------------------------------------------


def test_round_trip_creates_an_inert_new_copy(tmp_path, monkeypatch, install):
    pid = _saved()
    data = _export(("mission-plan", pid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    item = _plan(preview)[f"mission-plan:{pid}"]
    assert item["status"] == "new" and item["dest"] == pid
    assert item["mission-plan"] == {"objectives": 2, "slots": 2, "steps": 4}
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}
    copy_ = library.get(pid)
    assert copy_["document"] == _doc() and copy_["version"] == 1
    assert copy_["origin"] == {"kind": "file"}
    assert service.imports()[f"mission-plan:{pid}"]["bundle"] == preview["bundle"]["digest"]
    # Inert: the import wrote the Library and nothing else — no mission, nothing launched.
    assert missions.list_missions()["missions"] == []
    again = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in again["items"]} == {"created"}
    assert [p["id"] for p in library.list_all()] == [pid]  # a retry never makes a second copy


def test_an_import_beside_the_original_is_renamed_never_overwritten(install):
    pid = _saved()
    data = _export(("mission-plan", pid))
    preview = service.preview(data)
    item = _plan(preview)[f"mission-plan:{pid}"]
    assert item["dest"] == f"{pid}-2"
    service.confirm(preview["token"], _keys(preview))
    assert library.get(pid)["origin"]["kind"] == "mission"  # the original is untouched
    assert library.get(f"{pid}-2")["origin"] == {"kind": "file"}


def _broken(how: str) -> dict:
    d = json.loads(json.dumps(_doc()))
    if how == "unknown key":
        d["projects"] = ["p-real"]
    elif how == "newer format":
        d["format"] = 2
    elif how == "unused slot":
        d["slots"].append({"id": "p3", "label": "Spare"})
    elif how == "bad flow":
        d["objectives"][0]["procedure"]["document"]["steps"][0]["actor"] = {"kind": "robot"}
    elif how == "digest":
        d["objectives"][0]["procedure"]["digest"] = "0" * 64
    elif how == "forward after":
        d["objectives"][0]["after"] = ["o2"]
    elif how == "values":
        d["objectives"][0]["inputs"] = [{"name": "repo", "value": "org/x"}]
    elif how == "unknown slot":
        d["objectives"][0]["slot"] = "p9"
    return d


@pytest.mark.parametrize(
    "how",
    [
        "unknown key",
        "newer format",
        "unused slot",
        "bad flow",
        "digest",
        "forward after",
        "values",
        "unknown slot",
        "oversize",
    ],
)
def test_a_hostile_file_is_refused_in_the_preview_and_its_text_withheld(install, how, monkeypatch):
    data = _file("evil", _broken(how))
    if how == "oversize":  # the valid document, one byte over the bound
        monkeypatch.setattr(library, "DOCUMENT_MAX", len(plan_doc.canonical(_doc())) - 1)
    preview = service.preview(data)
    item = _plan(preview)["mission-plan:evil"]
    assert item["status"] == "refused"
    if how == "newer format":
        assert "newer BattleLab" in item["reason"]
    if how == "oversize":
        assert "larger than" in item["reason"]
    assert all(f.get("withheld") for f in item["files"])
    assert library.list_all(archived=True) == []


def test_a_file_that_is_not_one_json_document_is_refused(install):
    for files in (
        {"mission-plan.json": b"{not json"},
        {"mission-plan.json": json.dumps(_doc()).encode()[:-1] + b', "title": "Twice"}'},
        {"mission-plan.json": json.dumps(_doc()).encode(), "extra.txt": b"x"},
    ):
        data = fmt.write(
            [{"kind": "mission-plan", "id": "x", "title": "x", "files": files, "depends_on": []}],
            created_at="2026-11-10T00:00:00Z",
            app_version="test",
        )
        assert _plan(service.preview(data))["mission-plan:x"]["status"] == "refused"


def test_a_crash_after_creation_reconciles_to_created_by_digest(tmp_path, monkeypatch, install):
    pid = _saved()
    data = _export(("mission-plan", pid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = service._record_outcome

    class Crash(BaseException):
        pass

    def crash(journal, key, **outcome):
        if key.startswith("mission-plan:"):
            raise Crash()
        return real(journal, key, **outcome)

    monkeypatch.setattr(service, "_record_outcome", crash)
    with pytest.raises(Crash):
        service.confirm(preview["token"], _keys(preview))
    assert list(library.ids()) == [pid]
    monkeypatch.setattr(service, "_record_outcome", real)
    monkeypatch.setattr(kinds, "publish", lambda *a: pytest.fail("created twice"))
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in result["items"]} == {"created"}


def test_a_plan_already_at_the_destination_is_never_adopted(tmp_path, monkeypatch, install):
    pid = _saved()
    data = _export(("mission-plan", pid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = plan_kinds.publish

    def other_first(dest, doc):  # someone else takes the id between plan and publish
        library.create(_doc(title="Someone else's"), origin={"kind": "file"}, pid=dest)
        return real(dest, doc)

    monkeypatch.setattr(plan_kinds, "publish", other_first)
    result = service.confirm(preview["token"], _keys(preview))
    row = next(i for i in result["items"] if i["key"] == f"mission-plan:{pid}")
    assert row["status"] == "failed" and "nothing was overwritten" in row["reason"]
    assert row["retry"] is False
    assert library.get(pid)["document"]["title"] == "Someone else's"


def test_a_file_without_plans_never_binds_the_plan_library(install, monkeypatch):
    """Bound only when carried: a skills-and-playbooks import never depends on this store."""
    monkeypatch.setattr(plan_kinds, "ids", lambda: pytest.fail("the plan library was read"))
    from agent_sessions import fileedit
    from agent_sessions.gallery import library as skills

    assert fileedit.install_lease_signal_handler()

    skills.create_skill("tdd", {"SKILL.md": "# tdd\n\nDo it.\n"})
    data = _export(("skill", "tdd"))
    service.preview(data)


def test_an_import_mark_whose_plan_id_holds_a_secret_is_withheld(install):
    variables.create({"name": "deploy_token", "kind": "secret", "definition": {}, "value": SECRET})
    pid = SECRET.lower()[:40].strip("-")
    preview = service.preview(_file(pid, _doc()))
    service.confirm(preview["token"], _keys(preview))
    assert f"mission-plan:{pid}" in service.imports()
    shown = json.dumps(service.imports_view())
    assert SECRET.lower()[:20] not in shown.lower() and "mission-plan:~withheld-" in shown


def test_publish_never_takes_a_taken_id(install):
    pid = _saved()
    with pytest.raises(kinds.Terminal):
        plan_kinds.publish(pid, _doc(title="Mine"))
    assert library.get(pid)["document"] == _doc()


def test_an_archived_plan_whose_id_holds_a_secret_is_refused_without_naming_it(install):
    """The scan runs before the archived refusal, which would otherwise quote the ID."""
    pid = "x-correct-horse-battery-staple-deploy-phrase"
    preview = service.preview(_file(pid, _doc()))
    service.confirm(preview["token"], _keys(preview))
    library.set_archived(pid, True, expect_revision=1)
    variables.create({"name": "phrase", "kind": "secret", "definition": {}, "value": PASSPHRASE})
    with pytest.raises(service.ShareError) as err:
        _export(("mission-plan", pid))
    assert "horse" not in err.value.detail and "(its ID withheld)" in err.value.detail


def test_a_full_library_fails_an_import_for_good(install, monkeypatch):
    """Plans are archived, never deleted: a full Library is not "confirm again"."""
    monkeypatch.setattr(library, "PLANS_MAX", 1)
    _saved()
    data = _file("plan-0000beef", _doc(title="Another"))
    preview = service.preview(data)
    result = service.confirm(preview["token"], _keys(preview))
    row = next(i for i in result["items"] if i["key"] == "mission-plan:plan-0000beef")
    assert row["status"] == "failed" and row["retry"] is False
