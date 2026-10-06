"""Format 2 never turns a bundle default into implicit probe authority (#1191)."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from agent_sessions import (
    engines,
    fileedit,
    missions,
    model_choice,
    prefs,
    projects,
    template_secrets,
    template_vars,
)
from agent_sessions.main import create_app
from agent_sessions.playbooks import binding, loader, review, schema, store, tree
from agent_sessions.playbooks.errors import PlaybookFormatError

KEY = "review-tests-only-signing-key-32-characters"


def files(version=2):
    return {
        "playbook.toml": f"""format = {version}
[identity]
id = "review-demo"
name = "Review demo"
publisher = "Example"
version = "1.0.0"
domain = "operations"
summary = "Review two endpoints."
[[variables]]
name = "endpoint"
type = "url"
default = "https://example.com/health"
[[variables]]
name = "status"
type = "int"
default = 200
[[variables]]
name = "token"
kind = "secret"
[[connections]]
name = "service"
kind = "http-endpoint"
url = "{{{{endpoint}}}}"
verify = "status"
[[materials]]
path = "RULES.md"
disposition = "managed"
template = true
""",
        "template/RULES.md": "Check {{endpoint}}.\n",
        "flows/check.toml": """format = 1
title = "Check"
[[steps]]
id = "inspect"
title = "Inspect"
actor = {kind = "operator"}
[[steps.checklist]]
key = "healthy"
title = "Healthy"
probe = "http_status"
probe_args = {url = "{{endpoint}}", expect_status = "{{status}}"}
[[steps.checklist]]
key = "healthy_again"
title = "Healthy again"
probe = "http_status"
probe_args = {url = "{{endpoint}}"}
""",
    }


@pytest.fixture
def setup(tmp_home, tmp_path, monkeypatch):
    assert fileedit.install_lease_signal_handler()
    monkeypatch.setattr(loader, "BUNDLED_ROOT", tmp_path / "no-bundled")
    monkeypatch.setattr(prefs, "get_project_roots", lambda: [str(tmp_path)])
    folder = tmp_path / "destination"
    folder.mkdir()
    # Seed reader fixtures directly: authoring publication/durability has its own suite and the
    # explicit load/save regression below. These tests exercise actual descriptor reads.
    source = store.local_root() / "review-demo"
    contents = store.tree_from_files(files())
    loader.validate_named(contents, "review-demo")
    for relative, data in contents.files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder, {"destination": str(folder), "revision": contents.digest()}


def build(body):
    return review.build("review-demo", body, key=KEY)


def receipt(plan, targets=None):
    return review.confirm(plan, plan.public["digest"], targets or [], key=KEY)["receipt"]


def test_format_one_stays_strict_and_version_two_does_not_upgrade_subdocuments():
    with pytest.raises(PlaybookFormatError, match="target variable takes no default"):
        loader.validate_named(store.tree_from_files(files(1)), "review-demo")
    bundle = loader.validate_named(store.tree_from_files(files(2)), "review-demo")
    assert bundle["format"] == 2
    original = files(1)
    original["playbook.toml"] = original["playbook.toml"].replace(
        'default = "https://example.com/health"', 'example = "https://example.com/health"'
    )
    assert loader.validate_named(store.tree_from_files(original), "review-demo")["format"] == 1


def test_an_older_reader_refuses_format_two_before_reading_targets(monkeypatch):
    monkeypatch.setattr(schema, "FORMAT", 1)
    with pytest.raises(PlaybookFormatError, match="needs a newer BattleLab"):
        loader.validate_named(store.tree_from_files(files()), "review-demo")


def test_defaults_are_rendered_but_all_targets_start_unarmed(setup):
    folder, body = setup
    before = tree.read_tree(store.local_root() / "review-demo").digest()
    plan = build(body)
    assert plan.public["materials"][0]["after"]["text"] == "Check https://example.com/health.\n"
    assert len(plan.public["targets"]) == 3
    assert all(t["requires_confirmation"] and not t["armed"] for t in plan.public["targets"])
    assert all(t["default_variables"] == ["endpoint"] for t in plan.public["targets"])
    assert review.accept(plan, receipt(plan), key=KEY) == []
    assert list(folder.iterdir()) == []
    assert not template_vars.store_path().exists()
    assert tree.read_tree(store.local_root() / "review-demo").digest() == before


def test_confirmation_is_individual_even_for_targets_with_identical_urls(setup):
    _, body = setup
    plan = build(body)
    target = plan.public["targets"][0]["id"]
    token = receipt(plan, [target])
    armed = review.accept(build(body), token, key=KEY)
    assert [t["id"] for t in armed] == [target]
    assert armed[0]["armed"]
    assert not any(t["armed"] for t in plan.public["targets"])


def test_generated_flow_reference_is_reviewed_and_its_prestate_invalidates_confirmation(setup):
    folder, body = setup
    plan = build(body)
    row = next(m for m in plan.public["materials"] if m["path"] == "docs/playbook.md")
    assert row["disposition"] == "managed" and row["before"]["kind"] == "absent"
    assert "Actor: operator." in row["after"]["text"]
    assert "individual confirmation" in row["after"]["text"]
    assert not (folder / "docs").exists()
    token = receipt(plan)
    (folder / "docs").mkdir()
    (folder / "docs/playbook.md").write_text("operator-authored reference")
    with pytest.raises(store.Conflict, match="review changed"):
        review.accept(build(body), token, key=KEY)


def test_explicit_text_is_operator_input_even_when_equal_to_the_default(setup):
    _, body = setup
    body["bindings"] = [{"name": "endpoint", "kind": "text", "value": "https://example.com/health"}]
    plan = build(body)
    assert plan.public["variables"][0]["source"] == "input"
    assert all(not t["requires_confirmation"] for t in plan.public["targets"])
    assert len(review.accept(plan, receipt(plan), key=KEY)) == 3


@pytest.mark.parametrize(
    "change",
    ["value", "source", "file", "file_inode", "folder_inode", "assignment", "policy", "bundle"],
)
def test_any_reviewed_change_invalidates_confirmations(setup, change, tmp_path):
    folder, body = setup
    (folder / "RULES.md").write_text("operator content")
    plan = build(body)
    token = receipt(plan, [t["id"] for t in plan.public["targets"]])
    if change == "value":
        body["bindings"] = [
            {"name": "endpoint", "kind": "text", "value": "https://example.com/other"}
        ]
    elif change == "source":
        template_vars.create_variable({"name": "endpoint", "value": "https://example.com/health"})
    elif change == "file":
        (folder / "RULES.md").write_text("operator changed content")
    elif change == "file_inode":
        (folder / "RULES.md").rename(folder / "old")
        (folder / "RULES.md").write_text("operator content")
    elif change == "folder_inode":
        folder.rename(tmp_path / "old-folder")
        folder.mkdir()
        (folder / "RULES.md").write_text("operator content")
    elif change == "assignment":
        # Unknown/non-agent assignments are refused, not ignored and blessed by an old receipt.
        body["assignments"] = {"check:inspect": {"engine": "new-agent", "model": "default"}}
    elif change == "policy":
        prefs.set_folder_exclusions([str(tmp_path / "excluded")])
    else:
        changed = files()
        changed["template/RULES.md"] += "New material\n"
        body["revision"] = store.update_playbook("review-demo", body["revision"], changed)[
            "revision"
        ]
    with pytest.raises(store.StoreError):
        review.accept(build(body), token, key=KEY)


def test_a_library_revision_change_invalidates_review_even_if_value_is_unchanged(setup):
    _, body = setup
    var = template_vars.create_variable({"name": "endpoint", "value": "https://example.com/health"})
    plan = build(body)
    token = receipt(plan)
    template_vars.update_variable(
        "endpoint", {"value": "https://example.com/health"}, var["updated_at"]
    )
    with pytest.raises(store.Conflict, match="review changed"):
        review.accept(build(body), token, key=KEY)


def test_secret_inputs_are_never_returned_saved_or_unkeyedly_hashed(setup):
    folder, body = setup
    secret = "unsaved-token-value"
    body["bindings"] = [{"name": "token", "kind": "secret", "value": secret}]
    plan = build(body)
    result = review.confirm(plan, plan.public["digest"], [], key=KEY)
    assert secret not in json.dumps(plan.public)
    assert secret not in json.dumps(result)
    assert secret not in repr(plan)
    assert not template_vars.store_path().exists()
    assert list(folder.iterdir()) == []
    changed = copy.deepcopy(body)
    changed["bindings"][0]["value"] = "different-token-value"
    assert build(changed).public["digest"] != plan.public["digest"]
    assert (
        review.build("review-demo", body, key=KEY + "rotated").public["digest"]
        != plan.public["digest"]
    )


def test_secrets_do_not_fall_back_to_global_without_an_explicit_choice(setup):
    _, body = setup
    template_vars.create_variable(
        {"name": "token", "kind": "secret", "value": "global-token-value"}
    )
    plan = build(body)
    token = next(v for v in plan.public["variables"] if v["name"] == "token")
    assert token["state"] == "missing"
    body["bindings"] = [{"name": "token", "kind": "secret", "ref": "global"}]
    token = next(v for v in build(body).public["variables"] if v["name"] == "token")
    assert token["state"] == "ok" and token["source"] == "global" and token["explicit_global"]
    assert "value" not in token and "secret" not in token


def test_a_project_secret_needing_reentry_never_falls_back(setup, monkeypatch):
    folder, body = setup
    project = projects.create("Deploy", folders=[str(folder)], default_folder=str(folder))
    body["project_id"] = project.id
    template_vars.create_variable(
        {"name": "token", "kind": "secret", "value": "global-token-value"}
    )
    template_vars.bind_project(
        project.id, [{"name": "token", "kind": "secret", "value": "project-token-value"}]
    )
    monkeypatch.setattr(
        template_vars, "_decrypt", lambda r: None if r["project_id"] else "global-token-value"
    )
    with pytest.raises(store.StoreError, match="needs re-entry"):
        build(body)


@pytest.mark.parametrize(
    "targets", [["unknown"], [42], "all", ["connection:service", "connection:service"]]
)
def test_confirm_refuses_unknown_duplicate_and_malformed_target_ids(setup, targets):
    _, body = setup
    plan = build(body)
    with pytest.raises(store.StoreError):
        review.confirm(plan, plan.public["digest"], targets, key=KEY)


def test_receipt_expiry_tampering_and_key_rotation_refuse(setup, monkeypatch):
    _, body = setup
    plan = build(body)
    token = receipt(plan, ["connection:service"])
    for invalid, key in [(token + "x", KEY), (token, KEY + "new")]:
        with pytest.raises(store.Conflict, match="invalid"):
            review.accept(plan, invalid, key=key)
    monkeypatch.setattr(review, "REVIEW_TTL", -1)
    with pytest.raises(store.Conflict, match="expired"):
        review.accept(plan, token, key=KEY)


def test_signed_receipt_has_only_digest_and_target_ids(setup):
    _, body = setup
    plan = build(body)
    payload = URLSafeTimedSerializer(KEY, salt=review._SALT).loads(
        receipt(plan, ["connection:service"])
    )
    assert payload == {"digest": plan.public["digest"], "targets": ["connection:service"]}


def test_review_and_confirmation_routes_require_auth_csrf_origin_and_recompute(setup, auth_cfg):
    _, body = setup
    client = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    url = "/api/playbooks/review-demo/review"
    assert client.post(url, json=body).status_code == 401
    assert client.post(url + "/confirm", json={}).status_code == 401
    login = client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    assert login.status_code == 303
    csrf = client.get("/api/config").json()["csrf"]
    assert client.post(url, json=body).status_code == 403
    headers = {"X-CSRF-Token": csrf, "Origin": auth_cfg.origin}
    wrong_origin = {**headers, "Origin": "https://other.example"}
    assert client.post(url, json=body, headers=wrong_origin).status_code == 403
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    confirmation = {
        "inputs": body,
        "digest": response.json()["digest"],
        "targets": ["connection:service"],
    }
    assert client.post(url + "/confirm", json=confirmation, headers=headers).status_code == 200
    assert client.post(url + "/confirm", json=confirmation).status_code == 403
    assert client.post(url + "/confirm", json=confirmation, headers=wrong_origin).status_code == 403
    template_vars.create_variable({"name": "endpoint", "value": "https://example.com/new"})
    refused = client.post(url + "/confirm", json=confirmation, headers=headers)
    assert refused.status_code == 409
    assert refused.headers["cache-control"] == "no-store"


def test_review_route_refuses_a_non_utf8_link_target_with_a_controlled_409(setup, auth_cfg):
    folder, body = setup
    os.symlink(b"\xff", bytes(folder / "RULES.md"))
    client = TestClient(create_app(auth_cfg), base_url=auth_cfg.origin)
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    headers = {"X-CSRF-Token": client.get("/api/config").json()["csrf"], "Origin": auth_cfg.origin}
    response = client.post("/api/playbooks/review-demo/review", json=body, headers=headers)
    assert response.status_code == 409, response.text
    assert "not valid UTF-8" in response.json()["detail"]


@pytest.mark.parametrize("value", ["true", "1.5", "200.0", "+200", "099", "600"])
def test_concrete_probe_arguments_reuse_mission_validation(value):
    bundle = loader.validate_named(store.tree_from_files(files()), "review-demo")
    with pytest.raises((store.StoreError, PlaybookFormatError, missions.MissionError)):
        resolved = binding.resolve(
            bundle, None, [{"name": "status", "kind": "text", "value": value}]
        )
        binding.targets(bundle, resolved)


@pytest.mark.parametrize("version", [1, 2])
def test_enum_target_choices_are_only_allowed_by_explicit_format_two(version):
    contents = files(version)
    contents["playbook.toml"] += """
[[variables]]
name = "branch"
type = "enum"
choices = ["main", "release"]
default = "main"
"""
    contents["flows/check.toml"] += """
[[steps.checklist]]
key = "merged"
title = "Merged"
probe = "forge_merged"
probe_args = {branch = "{{branch}}"}
"""
    if version == 1:
        with pytest.raises(PlaybookFormatError, match="target variable takes no"):
            loader.validate_named(store.tree_from_files(contents), "review-demo")
        return
    bundle = loader.validate_named(store.tree_from_files(contents), "review-demo")
    target = binding.targets(bundle, binding.resolve(bundle, None, []))[2]
    assert target["args"] == {"branch": "main"}
    assert target["requires_confirmation"] and not target["armed"]


def test_declared_optional_connection_credential_keeps_target_unresolved():
    contents = files()
    contents["playbook.toml"] = contents["playbook.toml"].replace(
        'verify = "status"', 'verify = "status"\ncredential = "token"'
    )
    bundle = loader.validate_named(store.tree_from_files(contents), "review-demo")
    target = binding.targets(bundle, binding.resolve(bundle, None, []))[-1]
    assert target["pending"] == {"credential": "token"}
    plan = review.Plan({"digest": "a" * 64, "targets": [target]}, {})
    assert review.accept(plan, receipt(plan, [target["id"]]), key=KEY) == []


def test_step_outputs_stay_symbolic_and_can_never_be_armed_by_confirmation():
    bundle = loader.load_bundle(Path(__file__).parent / "fixtures/playbooks/forge-workflow")
    raw = [
        {"name": name, "kind": kind, "value": value}
        for name, kind, value in [
            ("repo_name", "text", "demo"),
            ("repo", "text", "example/demo"),
            ("forge_url", "text", "https://forge.example.com"),
            ("forge_token", "secret", "test-only-token"),
            ("deploy_url", "text", "https://example.com/health"),
        ]
    ]
    targets = binding.targets(bundle, binding.resolve(bundle, None, raw))
    pending = [t for t in targets if t["pending"]]
    assert pending and all(not t["armed"] for t in pending)
    plan = review.Plan({"digest": "a" * 64, "targets": targets}, {})
    armed = review.accept(plan, receipt(plan, [t["id"] for t in targets]), key=KEY)
    assert not {t["id"] for t in pending} & {t["id"] for t in armed}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        3,
        {},
        [None],
        [{"name": "unknown", "value": "value"}],
        [{"name": "endpoint", "kind": "secret", "value": "test-only-secret"}],
    ],
)
def test_binding_input_shape_is_strict(raw):
    bundle = loader.validate_named(store.tree_from_files(files()), "review-demo")
    with pytest.raises((store.StoreError, template_vars.VariableError)):
        binding.resolve(bundle, None, raw)


def test_project_values_are_scoped_and_preproject_preview_reads_only_globals(monkeypatch):
    bundle = loader.validate_named(store.tree_from_files(files()), "review-demo")
    records = [
        {
            "name": "endpoint",
            "kind": "text",
            "scope": scope,
            "project_id": pid,
            "value": value,
            "updated_at": revision,
            "created_at": 1,
        }
        for scope, pid, value, revision in [
            ("global", None, "https://example.com/global", 1),
            ("project", "p-aaaaaaaa", "https://example.com/first", 2),
            ("project", "p-bbbbbbbb", "https://example.com/second", 3),
        ]
    ]
    monkeypatch.setattr(template_vars, "_read_strictly", lambda: records)
    for pid, suffix in [(None, "global"), ("p-aaaaaaaa", "first"), ("p-bbbbbbbb", "second")]:
        result = binding.resolve(bundle, pid, [])
        assert result.text["endpoint"] == f"https://example.com/{suffix}"
        assert all(r["project_id"] in (None, pid) for r in result.fingerprint["dependencies"])


def test_bound_text_keeps_the_library_multiline_and_length_contract():
    contents = files()
    contents["playbook.toml"] += '\n[[variables]]\nname = "instructions"\n'
    bundle = loader.validate_named(store.tree_from_files(contents), "review-demo")
    value = "a" * 600 + "\nA second line\twith a tab"
    result = binding.resolve(
        bundle, None, [{"name": "instructions", "kind": "text", "value": value}]
    )
    assert result.text["instructions"] == value


@pytest.mark.parametrize(
    "name,kind,value",
    [("endpoint", "text", "https://example.com/health"), ("token", "secret", "test-only-token")],
)
def test_saved_global_references_remain_explicit_on_later_reviews(tmp_home, name, kind, value):
    bundle = loader.validate_named(store.tree_from_files(files()), "review-demo")
    pid = "p-aaaaaaaa"
    # Fixture publication is separate from the reader under test. The scoped-store suite covers
    # bind durability; here both text and encrypted secrets go through the real strict reader.
    path = template_vars.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    global_row = {
        "name": name,
        "kind": kind,
        "scope": "global",
        "project_id": None,
        "created_at": 1,
        "updated_at": 1,
    }
    if kind == "secret":
        key = template_secrets.key_path()
        key.write_bytes(bytes(range(template_secrets.KEY_BYTES)))
        key.chmod(0o600)
        global_row["secret"] = template_secrets.encrypt_scoped("global", None, name, value)
    else:
        global_row["value"] = value
    project_row = {
        "name": name,
        "kind": kind,
        "scope": "project",
        "project_id": pid,
        "ref": "global",
        "created_at": 2,
        "updated_at": 2,
    }
    path.write_text(
        json.dumps({"version": template_vars.STORE_VERSION, "variables": [global_row, project_row]})
    )
    saved = binding.resolve(bundle, pid, [])
    row = next(v for v in saved.public if v["name"] == name)
    assert row["source"] == "global" and row["explicit_global"]

    # A new own-value input replaces the recorded choice even if the bytes are identical.
    changed = binding.resolve(bundle, pid, [{"name": name, "kind": kind, "value": value}])
    row = next(v for v in changed.public if v["name"] == name)
    assert row["source"] == "input" and not row["explicit_global"]
    assert saved.fingerprint != changed.fingerprint
    if kind == "secret":
        assert value not in json.dumps(saved.public + changed.public)

    # Reviewing before project creation cannot borrow that project's explicit choice.
    preview = binding.resolve(bundle, None, [])
    row = next(v for v in preview.public if v["name"] == name)
    if kind == "secret":
        assert row["state"] == "missing"
    else:
        assert row["source"] == "global" and not row["explicit_global"]


def test_agent_assignments_follow_the_live_roster_without_substituting_models(monkeypatch):
    from agent_sessions.plugins import load_first_party

    bundle = loader.validate_named(store.tree_from_files(files()), "review-demo")
    provider = next(p for p in load_first_party().providers.values() if engines.is_agent(p))
    captured = engines.registry.current()
    roster = replace(captured, by_id={provider.engine_id: provider})
    monkeypatch.setattr(engines.registry, "current", lambda: roster)
    step = bundle["flows"]["check"]["steps"][0]
    step["actor"] = {"kind": "agent", "engine": provider.engine_id, "model": "default"}
    monkeypatch.setattr(engines.registry, "can_start", lambda p: True)
    rows, facts = review._assignments(bundle, {})
    assert rows[0]["assignment"] == {"engine": provider.engine_id, "model": "default"}
    assert facts["engines"][provider.engine_id]["manifest"] == provider.manifest.digest

    def missing_model(*args, **kwargs):
        raise model_choice.ModelRefused("unavailable", "model no longer offered")

    monkeypatch.setattr(model_choice, "select", missing_model)
    rows, _ = review._assignments(bundle, {})
    assert rows[0]["assignment"] is None and "model" in rows[0]["reason"]
    current = engines.registry.current()
    monkeypatch.setattr(engines.registry, "current", lambda: replace(current, by_id={}))
    rows, _ = review._assignments(bundle, {})
    assert rows[0]["assignment"] is None and rows[0]["reason"] == "agent unavailable"


def test_assignment_changes_and_roster_generation_invalidate_review(setup, monkeypatch):
    _, body = setup
    contents = files()
    contents["flows/check.toml"] = contents["flows/check.toml"].replace(
        'actor = {kind = "operator"}', 'actor = {kind = "agent", engine = "missing-agent"}'
    )
    body["revision"] = store.update_playbook("review-demo", body["revision"], contents)["revision"]
    plan = build(body)
    token = receipt(plan)
    body["assignments"] = {"check:inspect": None}
    with pytest.raises(store.Conflict, match="review changed"):
        review.accept(build(body), token, key=KEY)
    body.pop("assignments")
    current = engines.registry.current()
    monkeypatch.setattr(
        engines.registry, "current", lambda: replace(current, generation=current.generation + 1)
    )
    with pytest.raises(store.Conflict, match="review changed"):
        review.accept(build(body), token, key=KEY)


def test_load_save_review_and_confirmation_have_no_probe_or_dispatch_effects(setup, monkeypatch):
    from agent_sessions import headless_dispatch, mission_probes

    _, body = setup

    def forbidden(*args, **kwargs):
        pytest.fail("review/authoring must not run a probe or dispatch an agent")

    monkeypatch.setattr(mission_probes, "_probe_http", forbidden)
    monkeypatch.setattr(headless_dispatch, "dispatch", forbidden)
    loader.load_bundle(store.local_root() / "review-demo")
    changed = files()
    changed["template/RULES.md"] += "Updated instructions\n"
    body["revision"] = store.update_playbook("review-demo", body["revision"], changed)["revision"]
    plan = build(body)
    assert review.accept(plan, receipt(plan, [t["id"] for t in plan.public["targets"]]), key=KEY)


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "../project"),
        ("project_id", []),
        ("revision", None),
        ("destination", None),
        ("destination", "relative"),
        ("bindings", {}),
        ("assignments", []),
        ("unexpected", True),
    ],
)
def test_malformed_review_inputs_are_validation_errors(setup, field, value):
    _, body = setup
    body[field] = value
    with pytest.raises(store.StoreError) as refused:
        build(body)
    assert refused.value.status == 422


@pytest.mark.parametrize("change", ["archive", "folder"])
def test_project_archive_and_destination_reassignment_invalidate_review(setup, tmp_path, change):
    folder, body = setup
    project = projects.create("Deploy", folders=[str(folder)], default_folder=str(folder))
    body["project_id"] = project.id
    plan = build(body)
    token = receipt(plan)
    if change == "archive":
        projects.update(project.id, archived=True)
    else:
        other = tmp_path / "new-destination"
        other.mkdir()
        projects.update(project.id, folders=[str(other)], default_folder=str(other))
    with pytest.raises(store.StoreError):
        review.accept(build(body), token, key=KEY)
