"""New template references and variable deletion serialize under the authority fence."""

from __future__ import annotations

import json
import multiprocessing

import pytest
from fastapi.testclient import TestClient

from agent_sessions import template_vars as variables
from agent_sessions import templates
from agent_sessions.main import create_app


def _template(kind="text"):
    return {
        "name": "Use shared input",
        "body": "Use {{shared}}",
        "fields": [{"name": "shared", "source": "library", "kind": kind}],
    }


@pytest.mark.parametrize("kind", ["text", "secret"])
def test_new_reference_requires_an_existing_global_of_the_same_kind(tmp_home, kind):
    with pytest.raises(templates.TemplateError, match="shared.*library"):
        templates.create_template(_template(kind))
    assert not templates.store_path().exists()
    other = "secret" if kind == "text" else "text"
    variables.create_variable({"name": "shared", "kind": other, "value": "fixture-value"})
    with pytest.raises(templates.TemplateError, match="shared.*library"):
        templates.create_template(_template(kind))
    assert not templates.store_path().exists()


def test_project_value_does_not_authorize_a_global_template_reference(tmp_home):
    variables.bind_project("project-a", [{"name": "shared", "value": "local"}])
    with pytest.raises(templates.TemplateError, match="shared.*library"):
        templates.create_template(_template())


def test_update_checks_new_references_without_overwriting_the_existing_template(tmp_home):
    old = templates.create_template({"name": "Original", "body": "Original body"})
    before = templates.store_path().read_bytes()
    with pytest.raises(templates.TemplateError, match="shared.*library"):
        templates.update_template(old["id"], _template(), old["updated_at"])
    assert templates.store_path().read_bytes() == before


@pytest.mark.parametrize("method", ["post", "patch"])
def test_route_distinguishes_missing_binding_from_unreadable_inventory(auth_cfg, tmp_home, method):
    old = templates.create_template({"name": "Original", "body": "Original body"})
    before = templates.store_path().read_bytes()
    with TestClient(create_app(auth_cfg), base_url="https://testserver") as client:
        response = client.post(
            "/login",
            data={"username": "marcus", "password": "hunter2"},
            headers={"Origin": auth_cfg.origin},
            follow_redirects=False,
        )
        assert response.status_code == 303
        headers = {
            "Origin": auth_cfg.origin,
            "X-CSRF-Token": client.get("/api/config").json()["csrf"],
        }
        path = "/api/templates" if method == "post" else f"/api/templates/{old['id']}"
        payload = _template()
        if method == "patch":
            payload["expected_updated_at"] = old["updated_at"]
        for content, status in [('{"version":999}', 503), ('{"version":3,"variables":[]}', 422)]:
            variables.store_path().write_text(content)
            response = getattr(client, method)(path, headers=headers, json=payload)
            assert response.status_code == status, response.text
            assert response.headers["cache-control"] == "no-store"
            assert templates.store_path().read_bytes() == before


@pytest.mark.parametrize("raw", ["{", '{"version":999}', '{"version":3,"variables":[{}]}'])
def test_unreadable_variable_inventory_refuses_reference_creation(tmp_home, raw):
    path = variables.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    with pytest.raises(templates.TemplateInventoryIncomplete):
        templates.create_template(_template())
    assert path.read_text() == raw
    assert not templates.store_path().exists()


def test_legacy_unresolved_reference_is_retained_but_cannot_be_copied_as_a_new_reference(tmp_home):
    variables.create_variable({"name": "shared", "value": "old"})
    old = templates.create_template(_template())
    # Simulate a legacy store whose variable was removed out of band. Reads must remain useful.
    variables.store_path().write_text(json.dumps({"version": 3, "variables": []}))
    assert templates.get_template(old["id"])["fields"] == old["fields"]
    updated = templates.update_template(
        old["id"], {**_template(), "name": "Repair later"}, old["updated_at"]
    )
    assert updated["fields"] == old["fields"]
    with pytest.raises(templates.TemplateError, match="shared.*library"):
        templates.create_template(_template())
    # Explicitly remove the unresolved binding; no variable inventory is needed for removal.
    variables.store_path().write_text("{")
    repaired = templates.update_template(
        old["id"], {"name": "Repaired", "body": "No binding"}, updated["updated_at"]
    )
    assert repaired["fields"] == []


def _race_worker(action, revision, ready, release, result, pause):
    """Spawned processes share only the isolated store paths and the real filesystem fence."""
    if pause:
        module, attr = (
            (templates, "_write") if action == "create" else (variables, "atomic_write_json")
        )
        original = getattr(module, attr)

        def paused(*args, **kwargs):
            ready.set()
            if not release.wait(15):
                raise RuntimeError("test did not release the first writer")
            return original(*args, **kwargs)

        setattr(module, attr, paused)
    else:
        ready.set()
    try:
        if action == "create":
            templates.create_template(_template())
        else:
            variables.delete_variable("shared", revision)
        result.put("saved")
    except Exception as error:
        result.put(type(error).__name__)


@pytest.mark.parametrize("first", ["create", "delete"])
def test_both_process_orderings_refuse_exactly_one_conflicting_writer(tmp_home, first):
    variable = variables.create_variable({"name": "shared", "value": "fixture"})
    context = multiprocessing.get_context("spawn")
    ready, second_ready, release = context.Event(), context.Event(), context.Event()
    results = [context.Queue(), context.Queue()]
    second = "delete" if first == "create" else "create"
    processes = [
        context.Process(
            target=_race_worker,
            args=(first, variable["updated_at"], ready, release, results[0], True),
        ),
        context.Process(
            target=_race_worker,
            args=(second, variable["updated_at"], second_ready, release, results[1], False),
        ),
    ]
    try:
        processes[0].start()
        assert ready.wait(10)
        processes[1].start()
        assert second_ready.wait(10)
        release.set()
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert results[0].get(timeout=1) == "saved"
        assert results[1].get(timeout=1) == (
            "VariableInUse" if first == "create" else "TemplateError"
        )
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
