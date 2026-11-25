"""Share bundles carry templates and variable definitions (#1714 P3): a template travels with its
reference images and the library variables its fields use; a variable travels as its definition
(and a text variable's value) — a secret's value never leaves the install in any form, and it
arrives empty. Imports are inert new copies, renamed on conflict with every reference rewritten."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sessions import template_secrets, template_vars, templates, variables
from agent_sessions.gallery import library
from agent_sessions.playbooks import loader
from agent_sessions.routes.upload import ensure_uploads_dir, store_upload
from agent_sessions.sharebundle import format as fmt
from agent_sessions.sharebundle import kinds, service, state, template_kinds

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24 + b"one picture"
GIF = b"GIF89a" + b"\x01" * 20
SECRET = "s3cr3t-Value-for-the-deploy-0042"


def _install(root: Path, monkeypatch) -> Path:
    """One BattleLab install: its own home (uploads), app state, template and variable stores."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(root / "app"))
    monkeypatch.setenv("AGENT_SESSIONS_PLAYBOOKS_DIR", str(root / "playbooks"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATES", str(root / "cfg" / "templates.json"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_VARS", str(root / "cfg" / "vars.json"))
    monkeypatch.setenv("AGENT_SESSIONS_TEMPLATE_SECRETS_KEY", str(root / "cfg" / "secrets.key"))
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


def _image(data: bytes = PNG, name: str = "shot.png") -> dict:
    path, _ = store_upload([data], name)
    return {"name": name, "path": str(path)}


def _variable(name: str, kind: str = "text", value=None, **definition) -> dict:
    payload: dict = {"name": name, "kind": kind, "definition": definition}
    if value is not None:
        payload["value"] = value
    return variables.create(payload)


def _template(name: str = "Deploy note", **over) -> dict:
    payload = {
        "name": name,
        "description": "Tell the agent where to deploy.",
        "tags": ["deploy"],
        "body": "Deploy to {{api_url}} with {{deploy_token}}. Ticket {{ticket}}.",
        "fields": [
            {"name": "api_url", "label": "API URL", "source": "library"},
            {"name": "deploy_token", "label": "Token", "source": "library", "kind": "secret"},
            {"name": "ticket", "label": "Ticket", "default": "OPS-1"},
        ],
        "images": [_image()],
        **over,
    }
    return templates.create_template(payload)


def _library() -> None:
    _variable("api_url", value="https://api.example.test", label="API URL", help="Where to deploy")
    _variable("deploy_token", kind="secret", value=SECRET, label="Deploy token")


def _export(*items: tuple[str, str]) -> bytes:
    _, data = service.export([{"kind": k, "id": i} for k, i in items])
    return data


def _plan(preview: dict) -> dict:
    return {i["key"]: i for i in preview["items"]}


def _keys(preview: dict) -> list[str]:
    return [i["key"] for i in preview["items"] if i["status"] not in ("refused", "reused")]


def _vars() -> dict:
    return {v["name"]: v for v in variables.library()["variables"]}


# ---- export ---------------------------------------------------------------------------------


def test_a_template_exports_with_its_images_and_variables_and_no_secret_value(install):
    _library()
    _variable("unrelated", value="nothing to do with it")
    tid = _template()["id"]
    data = _export(("template", tid))
    bundle = fmt.read(data)
    assert [i.key for i in bundle.items] == [
        "variable:api_url",
        "variable:deploy_token",
        f"template:{tid}",
    ]
    tpl = bundle.item(f"template:{tid}")
    assert sorted(tpl.depends_on) == ["variable:api_url", "variable:deploy_token"]
    assert sorted(tpl.files) == ["images/1.png", "template.json"]
    assert tpl.files["images/1.png"] == PNG
    doc = json.loads(tpl.files["template.json"])
    assert set(doc) == {"format", "name", "description", "tags", "body", "fields", "images"}
    assert doc["images"] == [{"name": "shot.png", "file": "images/1.png"}]
    secret = json.loads(bundle.item("variable:deploy_token").files["variable.json"])
    assert secret == {
        "format": 1,
        "kind": "secret",
        "definition": {
            "label": "Deploy token",
            "help": "",
            "required": False,
            "value_type": "text",
        },
        "value": None,
    }
    text = json.loads(bundle.item("variable:api_url").files["variable.json"])
    assert text["value"] == "https://api.example.test"
    # The secret never leaves in any form: not its value, not its envelope.
    rec = next(r for r in template_vars._read_strictly_full() if r["name"] == "deploy_token")
    for needle in (SECRET, rec["secret"]["ct"], rec["secret"]["nonce"]):
        assert needle.encode() not in data
    # No usage counters, timestamps, project bindings or agent access travel.
    for word in (b"used_count", b"updated_at", b"agent_access", b"project_id"):
        assert word not in data


def test_a_secret_file_and_an_unset_text_travel_as_definitions_only(install):
    variables.create({"name": "kubeconfig", "kind": "secret_file", "definition": {}})
    variables.create({"name": "region", "kind": "text", "definition": {"default": "eu-1"}})
    bundle = fmt.read(_export(("variable", "kubeconfig"), ("variable", "region")))
    for key in ("variable:kubeconfig", "variable:region"):
        assert json.loads(bundle.item(key).files["variable.json"])["value"] is None
    region = json.loads(bundle.item("variable:region").files["variable.json"])
    assert region["definition"]["default"] == "eu-1"


@pytest.mark.parametrize(
    ("where", "over", "field"),
    [
        ("body", {"body": f"Use {SECRET} now"}, "body"),
        ("default", {"fields": [{"name": "t", "default": SECRET}]}, "field t default"),
        ("label", {"fields": [{"name": "t", "label": SECRET[:40]}]}, "field t label"),
        ("description", {"description": f"key {SECRET}"}, "description"),
    ],
)
def test_a_template_holding_a_stored_secret_is_never_exported(install, where, over, field):
    _variable("deploy_token", kind="secret", value=SECRET)
    tid = templates.create_template({"name": "Leaky", "body": "Hello", **over})["id"]
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert err.value.status == 422
    assert f"its {field} holds one of your stored secrets" in err.value.detail
    assert SECRET not in err.value.detail
    row = next(r for r in kinds.catalog() if r["kind"] == "template")
    assert row["exportable"] is False and SECRET not in row["reason"]


def test_credential_shaped_text_and_defaults_are_refused_on_export(install):
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    _variable("gh", value=token)
    variables.create({"name": "region", "kind": "text", "definition": {"default": token}})
    for name, what in (("gh", "its value looks like a credential"), ("region", "its default")):
        with pytest.raises(service.ShareError) as err:
            _export(("variable", name))
        assert what in err.value.detail and token not in err.value.detail
    rows = {r["id"]: r for r in kinds.catalog() if r["kind"] == "variable"}
    assert rows["gh"]["exportable"] is False and rows["region"]["exportable"] is False
    tid = templates.create_template(
        {"name": "Uses gh", "body": "{{gh}}", "fields": [{"name": "gh", "source": "library"}]}
    )["id"]
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert err.value.detail.startswith(f"Variable gh (needed by template {tid}):")


def test_the_scan_fails_closed_when_the_secret_store_cannot_be_read(install, monkeypatch):
    _library()
    tid = _template()["id"]

    def unavailable():
        raise template_secrets.RedactionUnavailable("no")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert "the secret check can't run" in err.value.detail
    rows = [r for r in kinds.catalog() if r["kind"] in ("template", "variable")]
    assert rows and not any(r["exportable"] for r in rows)
    assert all(r["depends_on"] == [] for r in rows)


def test_a_template_with_a_dangling_or_missing_image_refuses_its_export(install):
    _library()
    tid = _template()["id"]
    image = Path(templates.get_template(tid)["images"][0]["path"])
    image.write_bytes(b"not a png at all")
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert "its image 1 is not the picture its name says" in err.value.detail
    image.unlink()
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert "its image 1 is missing or can't be read" in err.value.detail


# ---- import ---------------------------------------------------------------------------------


def test_a_round_trip_recreates_the_template_and_definitions_and_a_secret_arrives_empty(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    source = templates.get_template(tid)
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    plan = _plan(preview)
    assert all(i["status"] == "new" for i in preview["items"])
    assert plan["variable:deploy_token"]["variable"] == {
        "kind": "secret",
        "value_type": "text",
        "arrives_empty": True,
    }
    assert plan["variable:api_url"]["variable"]["arrives_empty"] is False
    assert plan[f"template:{tid}"]["template"] == {
        "fields": 3,
        "images": 1,
        "variables": ["api_url", "deploy_token"],
    }
    result = service.confirm(preview["token"], _keys(preview))
    assert {i["key"]: i["status"] for i in result["items"]} == {k: "created" for k in plan}
    copy = templates.get_template(tid)
    for key in ("name", "description", "tags", "body", "fields"):
        assert copy[key] == source[key]
    assert copy["used_count"] == 0
    stored = Path(copy["images"][0]["path"])
    assert stored.parent == ensure_uploads_dir() and stored.read_bytes() == PNG
    lib = _vars()
    assert lib["api_url"]["value"] == "https://api.example.test"
    assert lib["deploy_token"]["set"] is False and "value" not in lib["deploy_token"]
    # Inert: offered to no agent, bound to no project.
    assert lib["deploy_token"]["agent_access"] is False
    assert lib["deploy_token"]["projects"] == []
    marks = service.imports()
    assert {f"template:{tid}", "variable:api_url", "variable:deploy_token"} <= set(marks)


def test_conflicts_are_renamed_and_the_template_follows_its_renamed_variables(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    _variable("api_url", value="https://mine.test")
    _variable("deploy_token_2", value="already here")
    mine = templates.create_template({"name": "Deploy note", "body": "mine"})
    assert mine["id"] == tid
    preview = service.preview(data)
    plan = _plan(preview)
    assert plan["variable:api_url"]["dest"] == "api_url_2"
    assert plan["variable:deploy_token"]["dest"] == "deploy_token"
    assert plan[f"template:{tid}"]["dest"] == f"{tid}-2"
    assert plan[f"template:{tid}"]["uses"] == [{"from": "api_url", "to": "api_url_2"}]
    service.confirm(preview["token"], _keys(preview))
    copy = templates.get_template(f"{tid}-2")
    assert "{{api_url_2}}" in copy["body"] and "{{api_url}}" not in copy["body"]
    assert [f["name"] for f in copy["fields"]] == ["api_url_2", "deploy_token", "ticket"]
    # Nothing existing was touched.
    assert templates.get_template(tid)["body"] == "mine"
    assert _vars()["api_url"]["value"] == "https://mine.test"
    assert _vars()["api_url_2"]["value"] == "https://api.example.test"


def test_a_renamed_variable_never_takes_a_name_a_template_or_binding_already_uses(
    tmp_path, install, monkeypatch
):
    _variable("host", value="a.test")
    tid = templates.create_template(
        {
            "name": "Host",
            "body": "{{host}} {{host_2}}",
            "fields": [{"name": "host", "source": "library"}, {"name": "host_2"}],
        }
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    _variable("host", value="mine.test")
    _variable("host_3", value="x")
    # A local (legacy) template names `host_4` as a library field without a variable: the
    # library would refuse a new `host_4`, so the plan never takes it.
    real = templates.library_references_checked
    monkeypatch.setattr(
        templates,
        "library_references_checked",
        lambda name: [{"id": "legacy", "name": "Legacy"}] if name == "host_4" else real(name),
    )
    preview = service.preview(data)
    plan = _plan(preview)
    # Not host_2 (the template's own field), host_3 (held) or host_4 (a template names it).
    assert plan["variable:host"]["dest"] == "host_5"
    service.confirm(preview["token"], _keys(preview))
    copy = templates.get_template(plan[f"template:{tid}"]["dest"])
    assert copy["body"] == "{{host_5}} {{host_2}}"


def test_deselecting_or_refusing_a_variable_refuses_its_template(tmp_path, install, monkeypatch):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    result = service.confirm(preview["token"], ["variable:deploy_token", f"template:{tid}"])
    out = {i["key"]: i for i in result["items"]}
    assert out[f"template:{tid}"]["status"] == "refused"
    assert "needs variable api_url, which isn't being imported" in out[f"template:{tid}"]["reason"]
    assert templates.list_templates() == []


def _rebuild(data: bytes, key: str, path: str, body: bytes | None) -> bytes:
    """The same bundle with one file of one item replaced (or removed), re-sealed."""
    bundle = fmt.read(data)
    items = []
    for item in bundle.items:
        files = dict(item.files)
        if item.key == key:
            if body is None:
                files.pop(path)
            else:
                files[path] = body
        items.append(
            {
                "kind": item.kind,
                "id": item.id,
                "title": item.title,
                "files": files,
                "depends_on": list(item.depends_on),
            }
        )
    return fmt.write(items, created_at="2026-10-30T00:00:00Z", app_version="test")


def test_hostile_template_and_variable_items_are_refused_in_the_preview(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    key = f"template:{tid}"
    secret_doc = json.loads(fmt.read(data).item("variable:deploy_token").files["variable.json"])
    cases = {
        "a secret with a value": (
            "variable:deploy_token",
            "variable.json",
            json.dumps({**secret_doc, "value": "smuggled-in-value"}).encode(),
            "a secret travels without its value",
        ),
        "an image that isn't one": (key, "images/1.png", b"<svg onload=alert(1)>", "picture"),
        "an undeclared extra file": (key, "notes.txt", b"hi", "isn't one of its images"),
        "a float in the JSON": (
            "variable:api_url",
            "variable.json",
            b'{"format": 1.0, "kind": "text", "definition": {}, "value": null}',
            "unambiguous JSON",
        ),
    }
    _second(tmp_path, monkeypatch)
    for name, (item, path, body, why) in cases.items():
        preview = service.preview(_rebuild(data, item, path, body))
        plan = _plan(preview)
        assert plan[item]["status"] == "refused", name
        assert why in plan[item]["reason"], (name, plan[item]["reason"])
        if item != key:
            assert plan[key]["status"] == "refused", name
    assert templates.list_templates() == [] and _vars() == {}


def test_a_template_needs_every_variable_in_the_file_and_of_its_kind(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    bundle = fmt.read(data)
    tpl = bundle.item(f"template:{tid}")
    alone = fmt.write(
        [
            {
                "kind": "template",
                "id": tid,
                "title": tpl.title,
                "files": dict(tpl.files),
                "depends_on": [],
            }
        ],
        created_at="2026-10-30T00:00:00Z",
        app_version="test",
    )
    text_token = json.dumps(
        {"format": 1, "kind": "text", "definition": {}, "value": "plain"}
    ).encode()
    swapped = _rebuild(data, "variable:deploy_token", "variable.json", text_token)
    _second(tmp_path, monkeypatch)
    _variable("api_url", value="local")
    _variable("deploy_token", kind="secret", value=SECRET)
    plan = _plan(service.preview(alone))
    assert (
        "it uses variable api_url, which this file doesn't carry"
        in plan[f"template:{tid}"]["reason"]
    )
    plan = _plan(service.preview(swapped))
    assert plan[f"template:{tid}"]["status"] == "refused"
    assert "needs a secret variable" in plan[f"template:{tid}"]["reason"]


def test_a_failed_variable_skips_its_template_and_a_retry_never_duplicates(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = variables.create
    calls = []

    def flaky(payload):
        calls.append(payload["name"])
        if payload["name"] == "deploy_token" and calls.count("deploy_token") == 1:
            raise OSError(5, "EIO")
        return real(payload)

    monkeypatch.setattr(variables, "create", flaky)
    first = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert first["variable:api_url"]["status"] == "created"
    assert first["variable:deploy_token"]["status"] == "failed"
    assert first["variable:deploy_token"]["retry"] is True
    assert first[f"template:{tid}"]["status"] == "skipped"
    assert templates.list_templates() == []
    second = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert {k: v["status"] for k, v in second.items()} == {k: "created" for k in second}
    third = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in third["items"]} == {"created"}
    assert calls == ["api_url", "deploy_token", "deploy_token"]
    assert len(templates.list_templates()) == 1 and sorted(_vars()) == ["api_url", "deploy_token"]


def test_a_crash_after_publication_reconciles_by_destination_and_digest(
    tmp_path, install, monkeypatch
):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = service._record_outcome

    def crash(journal, key, **outcome):
        if key == f"template:{tid}" and outcome.get("outcome") == "created":
            raise RuntimeError("power cut")
        return real(journal, key, **outcome)

    monkeypatch.setattr(service, "_record_outcome", crash)
    with pytest.raises(RuntimeError):
        service.confirm(preview["token"], _keys(preview))
    monkeypatch.setattr(service, "_record_outcome", real)
    result = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    assert result[f"template:{tid}"]["status"] == "created"
    assert len(templates.list_templates()) == 1
    journal = json.loads((Path(state.root()) / f"journal-{preview['token']}.json").read_text())
    assert journal["entries"][f"template:{tid}"]["recovered"] is True


def test_a_variable_created_here_after_the_preview_forces_a_new_preview(
    tmp_path, install, monkeypatch
):
    variables.create({"name": "region", "kind": "text", "definition": {}, "value": "eu"})
    data = _export(("variable", "region"))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    with pytest.raises(service.ShareError) as err:
        variables.create({"name": "region", "kind": "text", "definition": {}, "value": "eu"})
        service.confirm(preview["token"], _keys(preview))
    assert err.value.status == 409 and err.value.extra["reason"] == "changed"


def test_a_failed_template_create_removes_the_images_it_wrote(tmp_path, install, monkeypatch):
    tid = templates.create_template(
        {"name": "Pics", "body": "look", "images": [_image(GIF, "a.gif")]}
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)

    def refuse(payload, *, tid=None):
        raise templates.TemplateError("too many templates (max 200)")

    monkeypatch.setattr(templates, "create_template", refuse)
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "failed"
    assert list(ensure_uploads_dir().iterdir()) == []


def test_a_skills_import_never_reads_the_template_or_variable_stores(
    tmp_path, install, monkeypatch
):
    library.create_skill("tdd", {"SKILL.md": "# tdd\n\nTest first.\n"})
    data = _export(("skill", "tdd"))
    _second(tmp_path, monkeypatch)
    template_vars.store_path().parent.mkdir(parents=True, exist_ok=True)
    template_vars.store_path().write_text('{"version": 99}')
    templates.store_path().write_text('{"version": 99}')
    preview = service.preview(data)
    assert set(
        json.loads((Path(state.root()) / f"preview-{preview['token']}.json").read_text())[
            "bindings"
        ]
    ) == {"skill", "playbook"}
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "created"


def test_the_template_digest_is_what_the_store_reads_back(install):
    _library()
    tid = _template()["id"]
    bundle = fmt.read(_export(("template", tid)))
    files = bundle.item(f"template:{tid}").files
    _payload, digest = template_kinds.template_prepare(tid, files)
    assert template_kinds.template_at(tid) == digest


def test_a_name_the_library_refuses_at_confirm_is_settled_for_good(tmp_path, install, monkeypatch):
    variables.create({"name": "region", "kind": "text", "definition": {}, "value": "eu"})
    data = _export(("variable", "region"))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    # A deployment now retains a project binding to `region` (no store the preview bound moved):
    # the library refuses a new global of that name, so the same plan can never succeed.
    real = template_vars._project_dependants
    monkeypatch.setattr(
        template_vars,
        "_project_dependants",
        lambda records, name: ["acme"] if name == "region" else real(records, name),
    )
    item = service.confirm(preview["token"], _keys(preview))["items"][0]
    assert item["status"] == "failed" and item["retry"] is False
    assert "Preview the file again" in item["reason"]
    assert _vars() == {}


def test_the_routes_carry_templates_and_no_response_holds_a_secret(auth_cfg, install):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    _library()
    tid = _template()["id"]
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert (
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        ).status_code
        == 303
    )
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": client.get("/api/config").json()["csrf"]}
    catalog = client.get("/api/share/catalog")
    rows = {(i["kind"], i["id"]): i for i in catalog.json()["items"]}
    assert rows[("template", tid)]["depends_on"] == ["variable:api_url", "variable:deploy_token"]
    assert rows[("variable", "deploy_token")]["note"].startswith("Definition only")
    assert rows[("variable", "deploy_token")]["secret"] is True
    assert rows[("variable", "api_url")]["secret"] is False
    exported = client.post(
        "/api/share/export", json={"items": [{"kind": "template", "id": tid}]}, headers=headers
    )
    assert exported.status_code == 200, exported.text
    templates.delete_template(tid, templates.get_template(tid)["updated_at"])
    preview = client.post("/api/share/import/preview", content=exported.content, headers=headers)
    assert preview.status_code == 200, preview.text
    keys = [i["key"] for i in preview.json()["items"] if i["status"] != "refused"]
    plan = {i["key"]: i for i in preview.json()["items"]}
    assert plan["variable:api_url"]["dest"] == "api_url_2"
    assert plan[f"template:{tid}"]["dest"] == tid
    done = client.post(
        "/api/share/import",
        json={"token": preview.json()["token"], "selection": keys},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    assert {i["status"] for i in done.json()["items"]} == {"created"}
    for response in (catalog, exported, preview, done):
        assert SECRET.encode() not in response.content


# ---- review round 1 (independent review of #1814) ---------------------------------------------


def test_a_secret_in_a_template_id_or_a_variable_name_never_leaves(install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})  # typed-once values stay in this test
    secret = "zq8w-deployhook-77xk-prod-0042aa"
    _variable("hook", kind="secret", value=secret)
    tid = templates.create_template({"name": f"Call {secret}", "body": "Call it"})["id"]
    rec = templates.get_template(tid)
    templates.update_template(tid, {"name": "Call hook", "body": "Call it"}, rec["updated_at"])
    assert secret in tid
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert "its ID holds one of your stored secrets" in err.value.detail
    assert "duplicate the template under another name" in err.value.detail
    assert secret not in err.value.detail
    # A variable's name is judged on export too: a secret typed once at a send (redacted for
    # 24 h) can match a name the library accepted before.
    _variable("deployhook", value="plain")
    template_secrets.remember(["deployhook"])
    with pytest.raises(service.ShareError) as err:
        _export(("variable", "deployhook"))
    assert "its variable name holds one of your stored secrets" in err.value.detail
    assert "create it again under another name" in err.value.detail


def test_a_rename_never_captures_a_literal_slot_in_a_body(tmp_path, install, monkeypatch):
    _variable("host", value="a.test")
    tid = templates.create_template(
        {
            "name": "Host",
            "body": "send {{host}} but keep the literal {{host_2}}",
            "fields": [{"name": "host", "source": "library"}],
        }
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    _variable("host", value="mine.test")
    preview = service.preview(data)
    assert _plan(preview)["variable:host"]["dest"] == "host_3"
    service.confirm(preview["token"], _keys(preview))
    assert templates.get_template(tid)["body"] == "send {{host_3}} but keep the literal {{host_2}}"


@pytest.mark.parametrize("where", ["variable", "template"])
def test_a_busy_fence_at_publish_is_a_retriable_failure_never_a_500(
    tmp_path, install, monkeypatch, where
):
    from agent_sessions import session_input

    _variable("host", value="a.test")
    tid = templates.create_template(
        {"name": "Host", "body": "{{host}}", "fields": [{"name": "host", "source": "library"}]}
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    target = variables if where == "variable" else templates
    name = "create" if where == "variable" else "create_template"
    real = getattr(target, name)
    calls = []

    def busy(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise session_input.AuthorityFenceBusy("busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, busy)
    first = {i["key"]: i for i in service.confirm(preview["token"], _keys(preview))["items"]}
    key = "variable:host" if where == "variable" else f"template:{tid}"
    assert first[key]["status"] == "failed" and first[key]["retry"] is True
    assert list(ensure_uploads_dir().iterdir()) == [] if ensure_uploads_dir().exists() else True
    second = service.confirm(preview["token"], _keys(preview))
    assert {i["status"] for i in second["items"]} == {"created"}


def test_a_numeric_default_equal_to_a_stored_secret_is_refused(install):
    _variable("pin", kind="secret", value="73519284")
    variables.create(
        {"name": "port", "kind": "text", "definition": {"value_type": "int", "default": 73519284}}
    )
    with pytest.raises(service.ShareError) as err:
        _export(("variable", "port"))
    assert "its default holds one of your stored secrets" in err.value.detail


def test_images_stay_when_the_template_was_written_before_a_late_failure(
    tmp_path, install, monkeypatch
):
    tid = templates.create_template(
        {"name": "Pics", "body": "look", "images": [_image(GIF, "a.gif")]}
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = templates.create_template

    def late(payload, *, tid=None):
        real(payload, tid=tid)
        raise OSError(5, "EIO after the replace")

    monkeypatch.setattr(templates, "create_template", late)
    result = service.confirm(preview["token"], _keys(preview))
    assert result["items"][0]["status"] == "created"
    copy = templates.get_template(tid)
    assert Path(copy["images"][0]["path"]).read_bytes() == GIF


def test_images_go_when_someone_else_took_the_destination(tmp_path, install, monkeypatch):
    tid = templates.create_template(
        {"name": "Pics", "body": "look", "images": [_image(GIF, "a.gif")]}
    )["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    real = templates.create_template

    def raced(payload, *, tid=None):
        # Another writer takes the ID between the occupant check and the create.
        real({"name": "Theirs", "body": "theirs"}, tid=tid)
        return real(payload, tid=tid)

    monkeypatch.setattr(templates, "create_template", raced)
    item = service.confirm(preview["token"], _keys(preview))["items"][0]
    assert item["status"] == "failed" and "was taken" in item["reason"]
    assert templates.get_template(tid)["name"] == "Theirs"
    assert list(ensure_uploads_dir().iterdir()) == []


# ---- review round 2 (Hermes on #1814, 7470) ----------------------------------------------------


def _client(auth_cfg):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    assert (
        client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        ).status_code
        == 303
    )
    csrf = client.get("/api/config").json()["csrf"]
    return client, {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}


def test_an_export_refusal_never_echoes_a_secret_id_or_name(auth_cfg, install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})
    secret = "zq8w-deployhook-77xk-prod-0042aa"
    _variable("hook", kind="secret", value=secret)
    tid = templates.create_template({"name": f"Call {secret}", "body": "Call it"})["id"]
    rec = templates.get_template(tid)
    templates.update_template(tid, {"name": "Call hook", "body": "Call it"}, rec["updated_at"])
    _variable("deployhook", value="plain")
    template_secrets.remember(["deployhook"])
    client, headers = _client(auth_cfg)
    for kind, item in (("template", tid), ("variable", "deployhook")):
        response = client.post(
            "/api/share/export", json={"items": [{"kind": kind, "id": item}]}, headers=headers
        )
        assert response.status_code == 422
        assert "withheld" in response.json()["detail"]
        needle = secret if kind == "template" else "deployhook"
        assert needle.encode() not in response.content
    catalog = client.get("/api/share/catalog")
    assert catalog.status_code == 200
    assert secret.encode() not in catalog.content and b"deployhook" not in catalog.content
    rows = [r for r in catalog.json()["items"] if r["id"].startswith("~withheld-")]
    assert len(rows) == 2 and not any(r["exportable"] for r in rows)


def test_a_refused_item_shows_no_file_text_in_the_preview(auth_cfg, tmp_path, install, monkeypatch):
    _library()
    tid = _template()["id"]
    data = _export(("template", tid))
    secret_doc = json.loads(fmt.read(data).item("variable:deploy_token").files["variable.json"])
    smuggled = "smuggled-in-value-0042"
    hostile = _rebuild(
        data,
        "variable:deploy_token",
        "variable.json",
        json.dumps({**secret_doc, "value": smuggled}).encode(),
    )
    _second(tmp_path, monkeypatch)
    client, headers = _client(auth_cfg)
    response = client.post("/api/share/import/preview", content=hostile, headers=headers)
    assert response.status_code == 200, response.text
    plan = {i["key"]: i for i in response.json()["items"]}
    assert plan["variable:deploy_token"]["status"] == "refused"
    assert all("text" not in f for f in plan["variable:deploy_token"]["files"])
    assert all(f.get("withheld") for f in plan["variable:deploy_token"]["files"])
    assert smuggled.encode() not in response.content


def test_a_secret_in_a_field_name_never_leaves(auth_cfg, install):
    tid = templates.create_template(
        {
            "name": "Note",
            "body": "hello",
            "fields": [{"name": "xk7pq9wz_deploy", "label": "Ticket"}],
        }
    )["id"]
    _variable("hook", kind="secret", value="xk7pq9wz_deploy", label="Hook")
    with pytest.raises(service.ShareError) as err:
        _export(("template", tid))
    assert "one of its field names holds one of your stored secrets" in err.value.detail
    assert "rename that field" in err.value.detail
    assert "xk7pq9wz_deploy" not in err.value.detail
    client, headers = _client(auth_cfg)
    catalog = client.get("/api/share/catalog")
    assert b"xk7pq9wz_deploy" not in catalog.content
    row = next(r for r in catalog.json()["items"] if r["kind"] == "template")
    assert row["exportable"] is False and row["depends_on"] == []


def test_a_refused_template_row_never_names_a_secret_bearing_variable(
    auth_cfg, install, monkeypatch
):
    monkeypatch.setattr(template_secrets, "_typed", {})
    _variable("deployhook2", value="plain")
    templates.create_template(
        {
            "name": "Uses it",
            "body": "{{deployhook2}}",
            "fields": [{"name": "deployhook2", "source": "library"}],
        }
    )
    template_secrets.remember(["deployhook2"])  # typed once at a send: redacted for 24 h
    client, _headers = _client(auth_cfg)
    catalog = client.get("/api/share/catalog")
    assert b"deployhook2" not in catalog.content
    row = next(r for r in catalog.json()["items"] if r["kind"] == "template")
    assert row["exportable"] is False and row["depends_on"] == []


# ---- review round 3 (Hermes on #1814, 7472) ----------------------------------------------------


def test_a_refused_template_title_or_image_name_never_echoes_a_secret(auth_cfg, install):
    secret = "AbCd-Deploy-Key-77xQ-0042"
    _variable("hook", kind="secret", value=secret)
    # Minted from a clean name, then renamed: only the title holds the secret, the ID doesn't.
    tid = templates.create_template({"name": "Call hook", "body": "Call it"})["id"]
    rec = templates.get_template(tid)
    templates.update_template(tid, {"name": f"Call {secret}", "body": "Call it"}, rec["updated_at"])
    pic = templates.create_template(
        {"name": "Pics", "body": "look", "images": [_image(GIF, f"{secret}.gif")]}
    )
    Path(pic["images"][0]["path"]).unlink()  # a read failure must not quote the name first
    client, headers = _client(auth_cfg)
    for item in (tid, pic["id"]):
        response = client.post(
            "/api/share/export", json={"items": [{"kind": "template", "id": item}]}, headers=headers
        )
        assert response.status_code == 422
        assert "holds one of your stored secrets" in response.json()["detail"]
        assert secret.encode() not in response.content
    catalog = client.get("/api/share/catalog")
    assert secret.encode() not in catalog.content
    rows = {r["id"]: r for r in catalog.json()["items"] if r["kind"] == "template"}
    assert rows[tid]["title"] == tid and rows[tid]["exportable"] is False


# ---- review round 5 (independent review of #1814) ----------------------------------------------


def test_with_no_secret_inventory_the_picker_shows_no_item_text(install, monkeypatch):
    _variable("zq8w_hook_var", value="plain")
    templates.create_template({"name": "Call Zq8wDeployHook77xk", "body": "x"})

    def unavailable():
        raise template_secrets.RedactionUnavailable("no")

    monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
    text = json.dumps(service.catalog())
    assert "Zq8wDeployHook77xk".lower() not in text.lower() and "zq8w_hook_var" not in text
    rows = [r for r in service.catalog()["items"] if r["kind"] in ("template", "variable")]
    assert {r["title"] for r in rows} == {"Template (unavailable)", "Variable (unavailable)"}
    assert all(r["id"].startswith("~withheld-") and r["reason"] for r in rows)


def test_an_import_mark_never_names_an_id_that_now_holds_a_secret(
    auth_cfg, tmp_path, install, monkeypatch
):
    monkeypatch.setattr(template_secrets, "_typed", {})
    tid = templates.create_template({"name": "zz8qwdeployhookk", "body": "x"})["id"]
    data = _export(("template", tid))
    _second(tmp_path, monkeypatch)
    preview = service.preview(data)
    service.confirm(preview["token"], _keys(preview))
    template_secrets.remember([tid])  # a secret stored after the import matches its ID
    digest = fmt.read(data).digest
    for view in (service.catalog()["imports"], service.imports_view()):
        assert tid not in json.dumps(view)
        assert [v["bundle"] for v in view.values()] == [digest]  # the dialog still counts it
    client, _headers = _client(auth_cfg)
    response = client.get("/api/share/imports")
    assert response.status_code == 200 and tid.encode() not in response.content


# ---- review round 6 (independent review of #1814) ----------------------------------------------


# ---- review round 7 (independent review of #1814) ----------------------------------------------


def _secret_file(name: str, data: bytes) -> None:
    rec = variables.create({"name": name, "kind": "secret_file", "definition": {}})
    variables.put_file(rec["id"], data, f"{name}.txt", rec["updated_at"])


def test_ordinary_names_are_not_refused_for_starting_like_a_secret(install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})
    _variable("pw", kind="secret", value="Password-2026-Winter!")
    _secret_file(
        "kubeconfig", b"apiVersion: v1\ncertificate-authority-data: LS0tLS1CRUdJTiBDRVJU\n"
    )
    reset = templates.create_template({"name": "Reset password", "body": "hi"})["id"]
    renew = templates.create_template(
        {
            "name": "Renew certificate",
            "body": "{{certificate}}",
            "fields": [{"name": "certificate"}],
        }
    )["id"]
    for tid in (reset, renew):
        assert fmt.read(_export(("template", tid))).item(f"template:{tid}")


def test_a_large_secret_file_keeps_the_picker_small(install, monkeypatch):
    import tracemalloc

    monkeypatch.setattr(template_secrets, "_typed", {})
    lines = [f"line-{n:05d}-{'Ab9' * 10}-{n * 7919:08x}" for n in range(2500)]
    _secret_file("big", "\n".join(lines).encode()[:200_000])
    for n in range(20):
        templates.create_template({"name": f"Template {n} " + "w" * 40, "body": "hi"})
    tracemalloc.start()
    try:
        rows = kinds.catalog()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len([r for r in rows if r["kind"] == "template"]) == 20
    assert peak < 64 * 1024 * 1024


# ---- review round 8 (independent review of #1814) ----------------------------------------------

K8S_SECRET = (
    b"apiVersion: v1\nkind: Secret\nmetadata:\n  name: registry-creds\ntype: Opaque\n"
    b"stringData:\n  password: Xk7pQ9wZ-registry-0042\n"
)


def test_a_secret_files_ordinary_lines_never_block_ordinary_names(install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})
    _secret_file("regcreds", K8S_SECRET)
    ok = [
        templates.create_template({"name": "Inspect metadata", "body": "hi"})["id"],
        templates.create_template(
            {"name": "Fields", "body": "{{metadata}}", "fields": [{"name": "metadata"}]}
        )["id"],
        templates.create_template(
            {"name": "Tagged", "body": "hi", "tags": ["metadata", "stringdata"]}
        )["id"],
        templates.create_template({"name": "Fix the secret type opaque issue", "body": "hi"})["id"],
    ]
    for tid in ok:
        assert fmt.read(_export(("template", tid))).item(f"template:{tid}")


# ---- review round 9 (independent review of #1814) ----------------------------------------------


# ---- review round 4 (Hermes on #1814, 7476) ----------------------------------------------------


def test_a_typed_secret_files_fragments_never_block_ordinary_words(install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})
    template_secrets.register_typed([K8S_SECRET.decode()])  # a secret file typed at a send
    tid = templates.create_template(
        {"name": "Tagged", "body": "hi", "tags": ["stringdata", "metadata"]}
    )["id"]
    assert fmt.read(_export(("template", tid))).item(f"template:{tid}")


# ---- Hermes on #1814, 7477 and 7479: derived identifiers ----------------------------------------


@pytest.fixture(autouse=True)
def _fresh_typed(monkeypatch):
    """Typed-once values live in process memory: each test starts with none."""
    monkeypatch.setattr(template_secrets, "_typed", {})
    monkeypatch.setattr(template_secrets, "_typed_whole", {})


def _store(kind: str, secret: str) -> None:
    if kind == "variable":
        _variable("hook", kind="secret", value=secret)
    elif kind == "file":
        _secret_file("creds", f"deployer\n{secret}\n".encode())  # a short line of two
    elif kind == "file-one":
        _secret_file("token", f"{secret}\n".encode())  # `echo token > file`
    else:
        template_secrets.register_typed([secret])


@pytest.mark.parametrize(
    ("secret_kind", "secret", "name"),
    [
        ("variable", "Zq8wDeployHook77xk", "Call {S}"),
        ("file", "Ab12cdEF34gh", "Call {S}"),
        ("file-one", "Zq8wDeployHook77xk", "Call {S}"),
        ("typed", "Zq8wDeployHook77xk", "Call {S}"),
        ("variable", "Rq5tNb8w-2026-0042-7719-Zz", "y" * 29 + " {S}"),  # a cut slug
    ],
)
def test_a_local_id_derived_from_a_secret_never_reaches_a_response(
    auth_cfg, install, secret_kind, secret, name
):
    tid = templates.create_template({"name": name.format(S=secret), "body": "hi"})["id"]
    rec = templates.get_template(tid)
    templates.update_template(tid, {"name": "Call hook", "body": "hi"}, rec["updated_at"])
    _store(secret_kind, secret)
    client, headers = _client(auth_cfg)
    exported = client.post(
        "/api/share/export", json={"items": [{"kind": "template", "id": tid}]}, headers=headers
    )
    assert exported.status_code == 422 and "withheld" in exported.json()["detail"]
    catalog = client.get("/api/share/catalog")
    for response in (exported, catalog):
        assert tid.encode() not in response.content
        assert secret.lower()[:8].encode() not in response.content.lower()


def test_a_variable_named_after_a_secret_in_another_case_is_refused(auth_cfg, install):
    _variable("zq8wdeployhook77xk", value="plain")
    _variable("hook", kind="secret", value="Zq8wDeployHook77xk")
    with pytest.raises(service.ShareError) as err:
        _export(("variable", "zq8wdeployhook77xk"))
    assert "withheld" in err.value.detail and "zq8w" not in err.value.detail
    client, _headers = _client(auth_cfg)
    assert b"zq8w" not in client.get("/api/share/catalog").content


@pytest.mark.parametrize("order", ["file-first", "token-first"])
def test_a_typed_token_equal_to_a_typed_files_line_keeps_its_protection(install, order):
    token = "Zq8wDeployHook77xk"
    tid = templates.create_template({"name": f"Call {token}", "body": "hi"})["id"]
    rec = templates.get_template(tid)
    templates.update_template(tid, {"name": "Call hook", "body": "hi"}, rec["updated_at"])
    both = [[f"apiVersion: v1\nkind: List\n{token}\n"], [token]]
    for values in both if order == "file-first" else both[::-1]:
        template_secrets.register_typed(values)
    with pytest.raises(service.ShareError):
        _export(("template", tid))


def test_two_templates_of_one_name_travel_under_distinct_ids(install):
    first = templates.create_template({"name": "Same", "body": "a"})["id"]
    second = templates.create_template({"name": "Same", "body": "b"})["id"]
    assert first != second
    bundle = fmt.read(_export(("template", first), ("template", second)))
    assert sorted(i.key for i in bundle.items) == ["template:same", "template:same-2"]


def test_an_unnamed_image_travels_as_image_n_never_its_stored_name(install):
    path, _ = store_upload([PNG], "Zq8w!Hook@77xk.png")  # sanitized to Zq8w_Hook_77xk.png
    tid = templates.create_template(
        {"name": "Pics", "body": "look", "images": [{"name": "", "path": str(path)}]}
    )["id"]
    assert templates.get_template(tid)["images"][0]["name"] == path.name
    data = _export(("template", tid))
    doc = json.loads(fmt.read(data).item(f"template:{tid}").files["template.json"])
    assert doc["images"] == [{"name": "image 1", "file": "images/1.png"}]
    assert b"zq8w" not in data.lower()


def test_the_picker_judges_the_image_names_that_travel(install):
    path, _ = store_upload([PNG], "Kq7vHook9wPx.png")
    tid = templates.create_template(
        {"name": "Pics", "body": "look", "images": [{"name": "", "path": str(path)}]}
    )["id"]
    _variable(
        "hook", kind="secret", value=Path(templates.get_template(tid)["images"][0]["path"]).name
    )
    row = next(r for r in kinds.catalog() if r["kind"] == "template")
    assert row["exportable"] is True and row["id"] == tid
    assert fmt.read(_export(("template", tid))).item(f"template:{tid}")


# ---- #1868: a short key line's value is folded like a credential line ---------------------------


def test_a_key_lines_credential_value_is_matched_case_folded_in_an_identifier(install, monkeypatch):
    monkeypatch.setattr(template_secrets, "_typed", {})
    _secret_file("deploy_env", b"NODE_ENV=production\nDB_PASSWORD=Hunter2Deploy\n")
    leaky = templates.create_template({"name": "HUNTER2DEPLOY notes", "body": "hi"})["id"]
    with pytest.raises(service.ShareError) as err:
        _export(("template", leaky))
    assert "holds one of your stored secrets" in err.value.detail
    assert "Hunter2Deploy" not in err.value.detail and "hunter2deploy" not in err.value.detail
    # A letters-only value is no entry of its own (`template_secrets._value_entry`): an ordinary
    # name that holds it stays exportable.
    ok = templates.create_template({"name": "Production checklist", "body": "hi"})["id"]
    assert fmt.read(_export(("template", ok))).item(f"template:{ok}")
