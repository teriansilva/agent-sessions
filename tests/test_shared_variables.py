"""Shared definitions/collections preserve the old value and secret boundaries (#1390)."""

import json

import pytest

from agent_sessions import template_vars as store
from agent_sessions import templates, variables

SECRET = "shared-library-secret-123"


def create(name="host", value="one.test", **extra):
    return variables.create({"name": name, "value": value, **extra})


def document():
    return json.loads(store.store_path().read_text())


@pytest.mark.parametrize(
    ("definition", "value", "stored"),
    [
        ({"value_type": "text"}, "hello\nworld", "hello\nworld"),
        ({"value_type": "url"}, "https://example.test", "https://example.test"),
        ({"value_type": "path"}, "~/projects/work", "~/projects/work"),
        ({"value_type": "enum", "choices": ["one", "two"]}, "two", "two"),
        ({"value_type": "int"}, 12, "12"),
        ({"value_type": "bool"}, False, "false"),
    ],
)
def test_typed_value_round_trips_and_legacy_clients_get_canonical_text(
    tmp_home, definition, value, stored
):
    created = create("input", value, definition=definition)
    assert created["value"] == value and type(created["value"]) is type(value)
    [loaded] = variables.library()["variables"]
    assert loaded == created
    assert store.values() == {"input": stored}
    assert document()["variables"][0]["value"] == stored


@pytest.mark.parametrize(
    ("definition", "value"),
    [
        ({"value_type": "url"}, "file:///private"),
        ({"value_type": "path"}, "../escape"),
        ({"value_type": "enum", "choices": ["one"]}, "two"),
        ({"value_type": "int"}, True),
        ({"value_type": "int"}, "12"),
        ({"value_type": "bool"}, 1),
        ({"unknown": "ignored?"}, "hello"),
    ],
)
def test_invalid_shared_values_are_never_written(tmp_home, definition, value):
    with pytest.raises(store.VariableError):
        create("input", value, definition=definition)
    assert not store.store_path().exists()


def test_legacy_patch_cannot_bypass_shared_constraints(tmp_home):
    v = create("count", 12, definition={"value_type": "int"})
    before = store.store_path().read_bytes()
    with pytest.raises(store.VariableError):
        store.update_variable("count", {"value": "true"}, v["updated_at"])
    assert store.store_path().read_bytes() == before
    store.update_variable("count", {"value": "13"}, v["updated_at"])
    assert variables.library()["variables"][0]["value"] == 13


def test_secret_label_change_keeps_id_ciphertext_and_encryption_identity(tmp_home):
    v = create("token", SECRET, kind="secret")
    before = document()["variables"][0]
    changed = variables.update(
        v["id"], {"definition": {"label": "Deployment credential"}}, v["updated_at"]
    )
    after = document()["variables"][0]
    assert changed["id"] == v["id"] and after["secret"] == before["secret"]
    assert changed["name"] == "token"
    assert SECRET not in json.dumps(changed)
    assert "secret" not in changed and "value" not in changed
    assert SECRET not in json.dumps(variables.library())
    with pytest.raises(store.VariableConflict) as conflict:
        variables.update(v["id"], {"value": "replacement-secret"}, v["updated_at"])
    assert SECRET not in json.dumps(conflict.value.current)
    assert "secret" not in conflict.value.current and "value" not in conflict.value.current


@pytest.mark.parametrize(
    "metadata", [{"default": SECRET}, {"example": SECRET}, {"value_type": "url"}]
)
def test_secret_definition_has_no_default_example_or_other_type(tmp_home, metadata):
    with pytest.raises(store.VariableError):
        create("token", SECRET, kind="secret", definition=metadata)
    assert not store.store_path().exists()


def test_collections_overlap_rename_and_delete_without_mutating_values(tmp_home):
    host, token = create(), create("token", SECRET, kind="secret")
    first = variables.save_collection(
        {"name": "Forgejo", "variable_ids": [host["id"], token["id"]]}
    )
    second = variables.save_collection({"name": "Research", "variable_ids": [host["id"]]})
    before = document()["variables"]
    changed = variables.save_collection(
        {"name": "Engineering", "variable_ids": [token["id"]]}, first["id"], first["updated_at"]
    )
    assert changed["id"] == first["id"]
    assert document()["variables"] == before
    variables.delete_collection(changed["id"], changed["updated_at"])
    assert document()["variables"] == before
    assert variables.library()["collections"] == [second]
    assert store.secret_values() == {"token": SECRET}


def test_stale_collection_edit_and_missing_members_leave_store_unchanged(tmp_home):
    host = create()
    collection = variables.save_collection({"name": "Forgejo", "variable_ids": [host["id"]]})
    before = store.store_path().read_bytes()
    with pytest.raises(store.VariableConflict):
        variables.save_collection({"name": "Changed", "variable_ids": []}, collection["id"], 1)
    with pytest.raises(variables.LibraryNotFound):
        variables.save_collection({"name": "Missing", "variable_ids": ["f" * 32]})
    with pytest.raises(store.VariableError):
        variables.save_collection({"name": "Duplicate", "variable_ids": [host["id"], host["id"]]})
    assert store.store_path().read_bytes() == before


def test_variable_deletion_removes_membership_and_invalidates_collection_revision(tmp_home):
    host = create()
    collection = variables.save_collection({"name": "Forgejo", "variable_ids": [host["id"]]})
    variables.delete(host["id"], host["updated_at"])
    current = variables.library()["collections"][0]
    assert current["variable_ids"] == [] and current["updated_at"] > collection["updated_at"]
    assert len(document()["identities"]) == 1  # retired ID is retained
    with pytest.raises(variables.LibraryNotFound):
        variables.update(host["id"], {"value": "resurrected"}, host["updated_at"])


def test_retyping_requires_unused_definition_but_label_change_does_not(tmp_home):
    host = create()
    template = templates.create_template(
        {
            "name": "Use host",
            "body": "Use {{host}}",
            "fields": [{"name": "host", "source": "library"}],
        }
    )
    before = store.store_path().read_bytes()
    with pytest.raises(store.VariableInUse) as refusal:
        variables.update(
            host["id"],
            {"definition": {"value_type": "url"}, "value": "https://example.test"},
            host["updated_at"],
        )
    assert refusal.value.dependants == [{"id": template["id"], "name": "Use host"}]
    assert store.store_path().read_bytes() == before
    changed = variables.update(host["id"], {"definition": {"label": "Server"}}, host["updated_at"])
    assert changed["definition"]["label"] == "Server"
    with pytest.raises(store.VariableInUse):
        variables.delete(changed["id"], changed["updated_at"])


def test_project_reference_blocks_retype_and_collection_membership_grants_no_secret(tmp_home):
    token = create("token", SECRET, kind="secret")
    variables.save_collection({"name": "Forgejo", "variable_ids": [token["id"]]})
    with pytest.raises(store.BindingMissing):
        store.resolver("project-a").secret("token")
    store.bind_project("project-a", [{"name": "token", "kind": "secret", "ref": "global"}])
    assert store.resolver("project-a").secret("token") == SECRET
    with pytest.raises(store.BindingMissing):
        store.resolver("project-b").secret("token")
    with pytest.raises(store.VariableInUse):
        variables.update(
            token["id"], {"definition": {"required": True}, "value": SECRET}, token["updated_at"]
        )


def test_migrate_is_explicit_idempotent_and_read_never_writes(tmp_home):
    path = store.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "variables": [
                    {"name": "host", "value": "one.test", "created_at": 1, "updated_at": 1}
                ],
            }
        )
    )
    before = path.read_bytes()
    view = variables.library()
    assert view["migration_required"] and view["variables"][0]["id"] is None
    assert path.read_bytes() == before
    first = variables.migrate()
    second = variables.migrate()
    assert first == second and first["variables"][0]["id"]
    assert not variables.library()["migration_required"]
    assert len(list(path.parent.glob("*.pre-v4-*"))) == 1


def test_corrupt_collection_inventory_refuses_resolution_and_all_writes(tmp_home):
    host = create()
    broken = document()
    broken["collections"] = [
        {
            "id": "f" * 32,
            "name": "Bad",
            "variable_ids": ["e" * 32],
            "created_at": 1,
            "updated_at": 1,
        }
    ]
    store.store_path().write_text(json.dumps(broken))
    before = store.store_path().read_bytes()
    with pytest.raises(store.ResolutionUnavailable):
        variables.library()
    with pytest.raises(store.ResolutionUnavailable):
        store.resolver("project-a")
    with pytest.raises(store.VariableError):
        variables.update(host["id"], {"value": "changed"}, host["updated_at"])
    assert store.store_path().read_bytes() == before


def test_shared_api_auth_csrf_conflicts_and_secret_responses_are_no_store(auth_cfg, tmp_home):
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    unauthenticated = client.get("/api/variables")
    assert unauthenticated.status_code == 401
    assert "no-store" in unauthenticated.headers["cache-control"]
    login = client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf = client.get("/api/config").json()["csrf"]
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    no_csrf = client.post(
        "/api/variables",
        json={"name": "token", "kind": "secret", "value": SECRET},
        headers={"Origin": auth_cfg.origin},
    )
    assert no_csrf.status_code == 403 and "no-store" in no_csrf.headers["cache-control"]
    assert not store.store_path().exists()
    result = client.post(
        "/api/variables", json={"name": "token", "kind": "secret", "value": SECRET}, headers=headers
    )
    assert result.status_code == 201, result.text
    row = result.json()
    assert SECRET not in result.text and "value" not in row and "secret" not in row
    assert "no-store" in result.headers["cache-control"]
    listing = client.get("/api/variables")
    assert listing.status_code == 200 and SECRET not in listing.text
    conflict = client.patch(
        f"/api/variables/{row['id']}",
        json={"expected_updated_at": 1, "value": "replacement-secret"},
        headers=headers,
    )
    assert conflict.status_code == 409 and "no-store" in conflict.headers["cache-control"]
    assert "current" in conflict.json() and SECRET not in conflict.text
    assert "value" not in conflict.json()["current"]
    collection = client.post(
        "/api/variable-collections",
        json={"name": "Forgejo", "variable_ids": [row["id"]]},
        headers=headers,
    )
    assert collection.status_code == 201, collection.text
    group = collection.json()
    deleted = client.delete(
        f"/api/variable-collections/{group['id']}?expected_updated_at={group['updated_at']}",
        headers=headers,
    )
    assert deleted.status_code == 204 and "no-store" in deleted.headers["cache-control"]
    assert store.secret_values() == {"token": SECRET}
    refused = client.patch(
        f"/api/variables/{row['id']}",
        json={"expected_updated_at": row["updated_at"], "definition": {"default": SECRET}},
        headers=headers,
    )
    assert refused.status_code == 422 and SECRET not in refused.text
    unknown = client.delete(f"/api/variables/{'f' * 32}?expected_updated_at=1", headers=headers)
    assert unknown.status_code == 404 and "no-store" in unknown.headers["cache-control"]


def test_legacy_missing_template_reference_cannot_implicitly_adopt_new_identity(tmp_home):
    template = templates.create_template(
        {"name": "Legacy", "body": "Use {{host}}", "fields": [{"name": "host"}]}
    )
    template["fields"][0]["source"] = "library"
    templates._write(templates.store_path(), [template])
    with pytest.raises(store.VariableInUse):
        create()
    with pytest.raises(store.VariableInUse):
        store.create_variable({"name": "host", "value": "second"})
    assert not store.store_path().exists()


@pytest.mark.parametrize("kind", ["text", "secret"])
def test_definition_can_exist_without_a_global_value_and_legacy_client_can_fill_it(tmp_home, kind):
    created = variables.create({"name": "input", "kind": kind})
    assert created["set"] is False and "value" not in created
    assert variables.library()["variables"][0] == created
    assert store.values() == {} and store.secret_values() == {} and store.secret_state() == {}
    assert store.secret_values_checked() == []
    if kind == "text":
        assert store.resolver("project-a").text("input", "fallback")["value"] == "fallback"
        with pytest.raises(store.BindingMissing):
            store.resolver("project-a").text("input")
    else:
        with pytest.raises(store.BindingMissing):
            store.resolver("project-a").secret("input")
    store.bind_project("project-a", [{"name": "input", "kind": kind, "ref": "global"}])
    with pytest.raises(store.BindingUnusable):
        getattr(store.resolver("project-a"), kind)("input")
    store.update_variable("input", {"value": "new-stored-value"}, created["updated_at"])
    [filled] = variables.library()["variables"]
    assert filled["id"] == created["id"] and filled["set"] is True
    if kind == "text":
        assert store.resolver("project-a").text("input")["value"] == "new-stored-value"
    else:
        assert store.resolver("project-a").secret("input") == "new-stored-value"


def test_clearing_secret_retires_the_value_and_keeps_definition_membership(tmp_home):
    from agent_sessions import template_secrets

    secret = create("token", SECRET, kind="secret")
    collection = variables.save_collection({"name": "Forgejo", "variable_ids": [secret["id"]]})
    cleared = variables.update(secret["id"], {"clear_value": True}, secret["updated_at"])
    assert cleared["id"] == secret["id"] and cleared["set"] is False
    assert "value" not in cleared and "secret" not in cleared
    assert SECRET not in store.store_path().read_text()
    assert variables.library()["collections"] == [collection]
    assert template_secrets.redact_text(SECRET) == "[secret]"
    refilled = variables.update(
        secret["id"], {"value": "replacement-secret"}, cleared["updated_at"]
    )
    assert refilled["set"] is True and "replacement-secret" not in json.dumps(refilled)


def test_explicit_binding_repair_does_not_drop_unreadable_records(tmp_home):
    store.create_variable({"name": "host", "value": "old"})
    store.bind_project("project-a", [{"name": "host", "ref": "global"}])
    path = store.store_path()
    broken = document()
    broken["variables"] = [r for r in broken["variables"] if r["scope"] != "global"]
    path.write_text(json.dumps(broken))
    with pytest.raises(store.ResolutionUnavailable):
        store.resolver("project-a")
    assert store.remove_project_bindings("project-a", ["host"]) == ["host"]
    assert store.project_bindings("project-a") == []
    # A malformed record is not a known dangling target. Even repair leaves it intact.
    broken["variables"].append({"name": "unreadable"})
    path.write_text(json.dumps(broken))
    before = path.read_bytes()
    with pytest.raises(store.VariableError, match="repair it"):
        store.remove_project_bindings("project-a", ["host"])
    assert path.read_bytes() == before


def test_unknown_document_metadata_also_refuses_transport_redaction(tmp_home):
    create()
    path = store.store_path()
    broken = document()
    broken["future_store"] = {}
    path.write_text(json.dumps(broken))
    with pytest.raises(store.VariableError):
        store.secret_values_checked()
