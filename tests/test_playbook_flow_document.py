"""The deployed flow reference is complete, bounded and has no execution authority."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from agent_sessions.playbooks import binding, flow_document, loader, materials, schema

FIXTURES = Path(__file__).parent / "fixtures/playbooks"


@pytest.fixture
def workflow():
    bundle = loader.load_bundle(FIXTURES / "forge-workflow")
    supplied = [
        {"name": name, "kind": kind, "value": value}
        for name, kind, value in [
            ("repo_name", "text", "demo"),
            ("repo", "text", "example/demo"),
            ("forge_url", "text", "https://forge.example.com"),
            ("forge_token", "secret", "test-only-secret"),
            ("deploy_url", "text", "https://example.com/health"),
        ]
    ]
    resolved = binding.resolve(bundle, None, supplied)
    assignments = [
        {
            "step": f"{fid}:{step['id']}",
            "requested": {"engine": "fixture-agent", "model": "fixture-model"},
            "assignment": {"engine": "fixture-agent", "model": "fixture-model"},
            "reason": None,
        }
        for fid, flow in bundle["flows"].items()
        for step in flow["steps"]
        if step["actor"]["kind"] == "agent"
    ]
    return bundle, resolved.text, assignments, binding.targets(bundle, resolved)


def content(workflow):
    result = flow_document.render(*workflow)
    assert result.path == "docs/playbook.md" and result.disposition == "managed"
    return result.data.decode("utf-8")


def test_document_carries_the_complete_flow_assignments_evidence_and_bail_conditions(workflow):
    before = copy.deepcopy(workflow)
    text = content(workflow)
    for step in workflow[0]["flows"]["dev"]["steps"]:
        assert f"### {step['id']}:" in text
        for item in step["checklist"]:
            assert f"`{item['key']}`" in text
    assert "requested model `fixture-model`" in text
    assert "runtime must verify the actual model" in text
    assert "Actor: external — Reviewer bot." in text
    assert "Actor: operator." in text
    assert "Actor: none; this step dispatches no agent." in text
    assert "Prerequisites (all required): `implement`." in text
    assert "Independent `session` required from step `implement`." in text
    assert "(optional, `http_revision`)" in text
    assert "Observed outputs: `pr_number`, `head_branch`, `head_sha`." in text
    assert "at most 5 rounds on this edge" in text
    assert "After the limit, ask the operator" in text
    assert "Repeated evidence consumes no additional round" in text
    assert "This is a note; it is never a prerequisite." in text
    assert "a check fails twice with the same cause" in text
    assert "test-only-secret" not in text
    assert workflow == before  # No caller-owned input is changed during rendering.


def test_an_alias_request_is_rendered_as_requested_beside_its_resolution(workflow):
    bundle, values, assignments, targets = workflow
    assignments = copy.deepcopy(assignments)
    assignments[0]["requested"]["model"] = "fast"
    text = flow_document.render(bundle, values, assignments, targets).data.decode("utf-8")
    assert "requested model `fast`, resolved to `fixture-model`." in text
    assert "requested model `fixture-model`." in text  # Unaliased rows add no resolution.


def test_bail_conditions_carry_their_runbook_trigger(workflow):
    bundle, values, assignments, targets = workflow
    bundle = copy.deepcopy(bundle)
    audit = bundle["runbooks"]["audit"]
    bundle["runbooks"]["release"] = {
        **copy.deepcopy(audit),
        "id": "release",
        "title": "Release",
        "trigger": "operator",
        "bail": ["the operator withdraws the release"],
    }
    text = flow_document.render(bundle, values, assignments, targets).data.decode("utf-8")
    ritual, operator = text.split("## Bail conditions: Release")
    assert "trigger: `ritual`" in ritual and "trigger: `operator`" not in ritual
    assert "a check fails twice with the same cause" in ritual
    assert "trigger: `operator`" in operator and "withdraws the release" in operator


def test_observed_outputs_remain_pending_and_rendered_arguments_are_not_authority(workflow):
    workflow[3][0]["requires_confirmation"] = True
    text = content(workflow)
    assert "Awaiting observed outputs from the current step episodes:" in text
    assert "{{steps.implement.head_branch}}" in text
    assert '"repo": "example/demo"' in text
    assert "This default-derived target requires individual confirmation." in text
    assert "Completing an agent turn does not complete a step." in text


@pytest.mark.parametrize("missing", [True, False])
def test_unavailable_or_unresolved_assignment_never_falls_back_to_the_bundle_actor(
    workflow, missing
):
    bundle, values, assignments, targets = workflow
    if missing:
        assignments = []
    else:
        assignments = [
            {**row, "assignment": None, "reason": "model no longer offered"} for row in assignments
        ]
    text = content((bundle, values, assignments, targets))
    assert "Actor: agent, unassigned" in text
    assert "requested model" not in text
    assert ("assignment not resolved" if missing else "model no longer offered") in text


def test_non_development_workflows_use_the_same_document_path():
    bundle = loader.load_bundle(FIXTURES / "inbox-triage")
    values = binding.resolve(
        bundle, None, [{"name": "inbox_name", "kind": "text", "value": "Shared inbox"}]
    )
    text = content((bundle, values.text, [], binding.targets(bundle, values)))
    assert "Read everything new in Shared inbox." in text
    assert "Independent `engine` required from step `read`." in text
    assert "### confirm: Operator confirms" in text
    assert "forge_" not in text


def test_values_are_expanded_once_and_markdown_cannot_close_reference_blocks(workflow):
    bundle, values, _, _ = workflow
    values["repo"] = "{{another_variable}}\n`````\n# untrusted heading"
    bundle["identity"]["name"] = "<script>bad()</script> [link](https://example.com)"
    text = content(workflow)
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert r"\[link\]" in text
    assert "{{another_variable}}" in text  # Inserted text is not interpreted as another token.
    assert "``````text\nWrite an issue" in text
    assert "# untrusted heading with the required sections.\n``````\n" in text


@pytest.mark.parametrize(
    "field",
    ["name", "flow_title", "description", "step_title", "brief", "external", "item", "bail"],
)
def test_secret_tokens_are_refused_in_every_rendered_free_text_field(workflow, field):
    bundle = workflow[0]
    flow = bundle["flows"]["dev"]
    token = "{{forge_token}}"
    if field == "name":
        bundle["identity"]["name"] = token
    elif field == "flow_title":
        flow["title"] = token
    elif field == "description":
        flow["description"] = token
    elif field == "step_title":
        flow["steps"][0]["title"] = token
    elif field == "brief":
        flow["steps"][0]["brief"] = token
    elif field == "external":
        flow["steps"][2]["actor"]["label"] = token
    elif field == "item":
        flow["steps"][0]["checklist"][0]["title"] = token
    else:
        bundle["runbooks"]["audit"]["bail"] = [token]
    with pytest.raises(materials.MaterialError, match="not a declared text variable"):
        content(workflow)


def test_a_missing_rendered_value_or_checklist_target_refuses(workflow):
    bundle, values, assignments, targets = workflow
    with pytest.raises(materials.MaterialError, match="needs a resolved text value"):
        flow_document.render(bundle, {}, assignments, targets)
    with pytest.raises(materials.MaterialError, match="checklist target was not resolved"):
        flow_document.render(bundle, values, assignments, [])


def test_document_bytes_are_deterministic_and_bounded(workflow, monkeypatch):
    assert flow_document.render(*workflow) == flow_document.render(*copy.deepcopy(workflow))
    monkeypatch.setattr(schema, "MAX_FILE_BYTES", 30)
    with pytest.raises(materials.MaterialError, match="file size limit"):
        flow_document.render(*workflow)


def test_document_size_limit_stops_before_rendering_later_steps(workflow, monkeypatch):
    workflow[0]["flows"]["dev"]["steps"][-1]["brief"] = "{{forge_token}}"
    monkeypatch.setattr(schema, "MAX_FILE_BYTES", 30)
    # The oversized header refuses before the deliberately invalid later step is expanded.
    with pytest.raises(materials.MaterialError, match="file size limit"):
        flow_document.render(*workflow)


def test_no_flows_adds_no_generated_file(workflow):
    workflow[0]["flows"] = {}
    document = flow_document.render(*workflow)
    assert document is None
    declared = [materials.Material("note", "seed", data=b"content")]
    assert flow_document.include(declared, document) == declared


@pytest.mark.parametrize("path", ["docs", "docs/playbook.md", "docs/playbook.md/child"])
def test_generated_reference_refuses_declared_path_collisions(workflow, path):
    document = flow_document.render(*workflow)
    with pytest.raises(materials.MaterialError, match="reserved for the generated flow"):
        flow_document.include([materials.Material(path, "managed", data=b"original")], document)


@pytest.mark.parametrize("limit", ["MAX_TOTAL_BYTES", "MAX_MATERIALS"])
def test_generated_reference_counts_towards_deployment_bounds(workflow, monkeypatch, limit):
    document = flow_document.render(*workflow)
    monkeypatch.setattr(schema, limit, 1)
    with pytest.raises(materials.MaterialError, match="exceed the bundle limit"):
        flow_document.include([materials.Material("note", "seed", data=b"content")], document)


def test_generated_reference_does_not_modify_declared_materials(workflow):
    document = flow_document.render(*workflow)
    declared = [materials.Material("note", "seed", data=b"original")]
    assert flow_document.include(declared, document) == [*declared, document]
    assert len(declared) == 1
