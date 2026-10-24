"""Direct prompts retain source copies without changing completion evidence or executing work."""

from __future__ import annotations

import copy
import shutil
import tomllib
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agent_sessions import chat_store, native_ipc, native_protocol, templates
from agent_sessions.main import create_app
from agent_sessions.playbooks import authoring, loader, schema, step_prompts, store, tomlw
from agent_sessions.playbooks.errors import PlaybookFormatError
from agent_sessions.plugins.manifest import ManifestError, _Reader

FIXTURE = Path(__file__).parent / "fixtures/playbooks/forge-workflow"


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "forge-workflow"
    shutil.copytree(FIXTURE, path)
    return path


def document(path):
    return tomllib.loads(path.read_text())


def write(path, value):
    path.write_text(tomlw.dumps(value))


def definitions(bundle):
    return {v["name"]: v for v in loader.load_bundle(bundle)["variables"]}


def manual(**over):
    return {"id": "prompt-one", "title": "Instructions", "body": "Inspect {{repo_name}}.", **over}


def attach(bundle, prompts, *, manifest_version=4, flow_version=4):
    manifest = document(bundle / "playbook.toml")
    manifest["format"] = manifest_version
    write(bundle / "playbook.toml", manifest)
    flow = document(bundle / "flows/dev.toml")
    flow["format"] = flow_version
    flow["steps"][0]["prompts"] = prompts
    write(bundle / "flows/dev.toml", flow)


def source(**over):
    return {
        "id": "source-template",
        "name": "Inspect",
        "body": "Inspect {{repository}}.",
        "fields": [{"name": "repository", "source": "library"}],
        **over,
    }


def test_legacy_bundle_stays_byte_for_byte_unchanged_and_has_no_added_prompt_keys(bundle):
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    loaded = loader.load_bundle(bundle)
    assert all("prompts" not in s for s in loaded["flows"]["dev"]["steps"])
    assert before == {p: p.read_bytes() for p in before}


def test_direct_prompts_preserve_completion_rules_outputs_and_rework(bundle):
    old = loader.load_bundle(bundle)["flows"]["dev"]
    attach(bundle, [manual(), manual(id="prompt-two", body="Review every required rule.")])
    new = loader.load_bundle(bundle)["flows"]["dev"]
    assert new["steps"][0].pop("prompts") == [
        manual(),
        manual(id="prompt-two", body="Review every required rule."),
    ]
    # Opting into prompts bumps the flow's format (#1654 exposes it); everything else is equal.
    assert (old.pop("format"), new.pop("format")) == (1, 4)
    assert old == new


@pytest.mark.parametrize("manifest,flow", [(1, 4), (2, 4), (3, 4), (4, 1), (4, 2), (4, 3)])
def test_prompts_require_explicit_bundle_and_flow_opt_in(bundle, manifest, flow):
    attach(bundle, [manual()], manifest_version=manifest, flow_version=flow)
    with pytest.raises(PlaybookFormatError, match="format 4"):
        loader.load_bundle(bundle)


def test_old_build_refuses_new_format_before_interpreting_instructions(bundle, monkeypatch):
    attach(bundle, [manual()])
    monkeypatch.setattr(schema, "FORMAT", 3)
    monkeypatch.setattr(schema, "SUPPORTED_FORMATS", frozenset({1, 2, 3}))
    with pytest.raises(PlaybookFormatError, match="newer BattleLab"):
        loader.load_bundle(bundle)


@pytest.mark.parametrize(
    "body",
    [
        "Use {{forge_token}}",
        "Use {{missing}}",
        "{{steps.plan.head_sha}}",
        "{{secret_path:forge_token}}",
    ],
)
def test_prompt_text_never_interpolates_secrets_unknown_inputs_or_evidence(bundle, body):
    attach(bundle, [manual(body=body)])
    with pytest.raises(PlaybookFormatError):
        loader.load_bundle(bundle)


@pytest.mark.parametrize(
    "prompts",
    [
        [manual(), manual()],
        [manual(unknown=True)],
        [manual(id="bad id")],
        [manual(body="bad\x1btext")],
        [manual(id=f"prompt-{i}") for i in range(schema.MAX_PROMPTS_PER_STEP + 1)],
    ],
)
def test_prompt_identity_shape_count_and_unknown_fields_are_strict(bundle, prompts):
    attach(bundle, prompts)
    with pytest.raises(PlaybookFormatError):
        loader.load_bundle(bundle)


def test_two_imports_have_distinct_ids_and_never_read_the_variable_store(bundle, monkeypatch):
    from agent_sessions import template_vars

    for name in ("values", "secret_values", "resolver"):
        monkeypatch.setattr(template_vars, name, Mock(side_effect=AssertionError("value read")))
    variables = definitions(bundle)
    one = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    two = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    assert one["id"] != two["id"]
    assert one["body"] == "Inspect {{repo_name}}."
    assert one["source"] == two["source"]
    assert one["source"]["template"]["body"] == "Inspect {{repository}}."
    attach(bundle, [one, two])
    assert loader.load_bundle(bundle)["flows"]["dev"]["steps"][0]["prompts"] == [one, two]


def test_source_update_and_deletion_leave_customized_retained_copy_intact(bundle):
    variables = definitions(bundle)
    prior = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    prior["body"] += " Preserve my local instruction."
    before = copy.deepcopy(prior)
    same = step_prompts.source_comparison(prior, source(used_count=20), variables)
    assert same == {"state": "current", "retained": prior, "candidate": None}
    comparison = step_prompts.source_comparison(
        prior, source(body="New {{repository}}."), variables
    )
    assert comparison["state"] == "changed"
    assert comparison["retained"] == prior
    assert comparison["candidate"]["body"] == "New {{repo_name}}."
    assert comparison["candidate"]["id"] == prior["id"]
    assert step_prompts.source_comparison(prior, None, variables)["candidate"] is None
    assert prior == before


def test_changed_source_fields_need_explicit_remapping(bundle):
    variables = definitions(bundle)
    prior = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    changed = source(body="New {{target}}", fields=[{"name": "target"}])
    comparison = step_prompts.source_comparison(prior, changed, variables)
    assert comparison["state"] == "changed" and comparison["candidate"] is None
    assert "map exactly" in comparison["reason"]


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"repository": "missing"},
        {"repository": "forge_token"},
        {"repository": "local-id-123"},
        {"repository": "repo_name", "extra": "repo"},
    ],
)
def test_source_binding_is_explicit_symbolic_and_nonsecret(bundle, mapping):
    with pytest.raises(ManifestError):
        step_prompts.retain_template(source(), mapping, definitions(bundle))


def test_mapped_title_tokens_cannot_push_a_valid_source_past_the_title_cap(bundle):
    alias = "a" * 32
    variables = definitions(bundle)
    variables[alias] = {**variables["repo_name"], "name": alias}
    name = "{{x}}" * 20  # a valid 100-character template name
    template = source(name=name, body="Use {{x}}.", fields=[{"name": "x", "source": "library"}])
    prompt = step_prompts.retain_template(template, {"x": alias}, variables)
    assert prompt["title"] == "x" * 20
    assert prompt["body"] == "Use {{" + alias + "}}."
    assert prompt["source"]["template"]["name"] == name
    prior = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    old = {**prompt["source"]["template"], "body": "Old {{x}}."}
    prior["source"] = {**prompt["source"], "template": old}
    prior["source"]["revision"] = step_prompts.revision(prior["source"]["template"])
    comparison = step_prompts.source_comparison(prior, template, variables)
    assert comparison["state"] == "changed"
    assert comparison["candidate"]["title"] == "x" * 20


def test_secret_template_refused_without_resolving_its_value(bundle):
    with pytest.raises(ManifestError, match="secret cannot"):
        step_prompts.retain_template(
            source(fields=[{"name": "repository", "kind": "secret"}]),
            {"repository": "repo_name"},
            definitions(bundle),
        )


def test_source_digest_cannot_disagree_with_its_retained_definition(bundle):
    variables = definitions(bundle)
    prior = step_prompts.retain_template(source(), {"repository": "repo_name"}, variables)
    prior["source"]["template"]["body"] = "Unexpected {{repository}}"
    with pytest.raises(ManifestError, match="does not match"):
        step_prompts.read(_Reader(prior, "prompt"), variables)


def test_portable_store_roundtrip_and_duplicate_keep_sources_but_refresh_prompt_ids(
    bundle, tmp_home
):
    prompt = step_prompts.retain_template(
        source(), {"repository": "repo_name"}, definitions(bundle)
    )
    attach(bundle, [prompt])
    manifest = document(bundle / "playbook.toml")
    manifest["identity"]["id"] = "retained-prompts"
    write(bundle / "playbook.toml", manifest)
    files = {str(p.relative_to(bundle)): p.read_text() for p in bundle.rglob("*") if p.is_file()}
    saved = store.create_playbook(files)
    restored = store.get_playbook(saved["id"])
    assert restored["files"] == saved["files"]
    duplicate = store.duplicate_playbook(saved["id"], {"revision": saved["revision"]})
    flow = next(
        value for path, value in duplicate["documents"].items() if path.startswith("flows/")
    )
    copied = flow["steps"][0]["prompts"][0]
    assert copied["id"] != prompt["id"] and copied["source"] == prompt["source"]
    assert copied["body"] == prompt["body"]
    assert store.get_playbook(saved["id"])["revision"] == saved["revision"]


def test_composition_is_verbatim_ordered_typed_and_bounded_before_return(bundle):
    variables = definitions(bundle)
    step = {
        "brief": "Keep this brief.\n",
        "prompts": [manual(body="First {{repo_name}}."), manual(id="second", body="  Last.  ")],
    }
    expected = "Keep this brief.\n\n\nFirst demo.\n\n  Last.  "
    assert (
        step_prompts.compose(step, variables, {"repo_name": "demo"}, limit=len(expected))
        == expected
    )
    with pytest.raises(ManifestError, match=r"prompts\[1\].body.*exceeds"):
        step_prompts.compose(step, variables, {"repo_name": "demo"}, limit=len(expected) - 1)
    with pytest.raises(ManifestError, match="no resolved value"):
        step_prompts.compose(step, variables, {}, limit=1000)
    # The expansion is measured before allocation, rather than building repeated huge values.
    variables["repo_name"]["pattern"] = None
    step["prompts"][0]["body"] = "{{repo_name}}" * 5000
    with pytest.raises(ManifestError, match="exceeds"):
        step_prompts.compose(step, variables, {"repo_name": "x" * 200_000}, limit=200_000)
    assert schema.MAX_INITIAL_INSTRUCTION == min(
        chat_store.TEXT_MAX, native_ipc.MAX_TEXT, native_protocol.MAX_INPUT_TEXT
    )


def test_import_and_compare_routes_are_authenticated_nostore_and_leave_drafts_unsaved(
    auth_cfg, bundle
):
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    endpoint = "/api/playbooks/authoring/prompts/import"
    assert client.post(endpoint, json={}).status_code in (401, 403)
    client.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    csrf = client.get("/api/config").json()["csrf"]
    headers = {"Origin": auth_cfg.origin, "X-CSRF-Token": csrf}
    template = templates.create_template(
        {"name": "Read", "body": "Read {{repository}}.", "fields": [{"name": "repository"}]}
    )
    variables = document(bundle / "playbook.toml")["variables"]
    body = {
        "template_id": template["id"],
        "expected_updated_at": template["updated_at"],
        "variables": variables,
        "bindings": {"repository": "repo_name"},
    }
    assert client.post(endpoint, json=body, headers={"Origin": auth_cfg.origin}).status_code == 403
    response = client.post(endpoint, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    prompt = response.json()["prompt"]
    assert not store.local_root().exists()
    body["expected_updated_at"] = 0
    assert client.post(endpoint, json=body, headers=headers).status_code == 409
    templates.delete_template(template["id"], template["updated_at"])
    comparison = client.post(
        "/api/playbooks/authoring/prompts/compare",
        json={"variables": variables, "prompt": prompt},
        headers=headers,
    )
    assert comparison.status_code == 200 and comparison.json()["state"] == "deleted"
    assert comparison.json()["retained"] == prompt
    assert not store.local_root().exists()
    templates.store_path().write_text("{damaged")
    with pytest.raises(store.StoreError, match="could not be read in full") as error:
        authoring.compare_template_prompt({"variables": variables, "prompt": prompt})
    assert error.value.status == 503


def test_required_source_field_is_not_weakened_by_optional_bundle_input(bundle):
    variables = definitions(bundle)
    variables["repo_name"].update(required=False, pattern=None)
    prompt = step_prompts.retain_template(
        source(fields=[{"name": "repository", "required": True}]),
        {"repository": "repo_name"},
        variables,
    )
    prompt["body"] = "A customized body still retains its required source input."
    with pytest.raises(ManifestError, match="requires a value"):
        step_prompts.compose({"prompts": [prompt]}, variables, {"repo_name": ""}, limit=1000)
    with pytest.raises(ManifestError, match="empty"):
        step_prompts.compose({"brief": "  "}, variables, {}, limit=1000)


def test_composition_keeps_native_types_and_rejects_bool_as_int(bundle):
    variables = definitions(bundle)
    variables["count"] = {
        "name": "count",
        "kind": "text",
        "type": "int",
        "required": True,
        "pattern": None,
        "choices": [],
    }
    variables["enabled"] = {
        "name": "enabled",
        "kind": "text",
        "type": "bool",
        "required": True,
        "pattern": None,
        "choices": [],
    }
    step = {"brief": "Count {{count}}, enabled {{enabled}}."}
    assert (
        step_prompts.compose(step, variables, {"count": 0, "enabled": False}, limit=100)
        == "Count 0, enabled false."
    )
    with pytest.raises(ManifestError, match="not a boolean"):
        step_prompts.compose(step, variables, {"count": True, "enabled": False}, limit=100)
