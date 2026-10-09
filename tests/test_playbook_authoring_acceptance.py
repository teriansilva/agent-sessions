"""P3 saves the requested actor; P2 alone decides the deployed assignment (#1192)."""

from dataclasses import replace

import pytest

from agent_sessions import engines, projects
from agent_sessions.playbooks import apply, lifecycle, review, store
from agent_sessions.plugins import load_first_party
from test_playbook_acceptance import no_effects as _no_effects
from test_playbook_lifecycle_binding import record
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup
no_effects = _no_effects


@pytest.mark.parametrize("state", ["available", "missing", "retiring", "model-missing", "api"])
def test_save_reload_review_apply_preserves_actor_and_uses_server_assignment(
    setup, no_effects, monkeypatch, state
):
    folder, inputs = setup
    runtime = "api" if state == "api" else "pty"
    provider = next(
        p
        for p in load_first_party().providers.values()
        if engines.is_agent(p) and p.manifest.runtime == runtime
    )
    roster = replace(
        engines.registry.current(),
        by_id={} if state == "missing" else {provider.engine_id: provider},
    )
    monkeypatch.setattr(engines.registry, "current", lambda: roster)
    monkeypatch.setattr(engines.registry, "can_start", lambda p: state != "retiring")
    model = "unavailable-model-for-test" if state == "model-missing" else "default"
    requested = {"engine": provider.engine_id, "model": model}
    contents = files()
    contents["flows/check.toml"] = contents["flows/check.toml"].replace(
        'actor = {kind = "operator"}',
        f'actor = {{kind = "agent", engine = "{provider.engine_id}", model = "{model}"}}',
    )
    saved = store.update_playbook("review-demo", inputs["revision"], contents)
    reloaded = store.get_playbook("review-demo")
    assert reloaded["revision"] == saved["revision"]
    actor = reloaded["documents"]["flows/check.toml"]["steps"][0]["actor"]
    assert actor == {"kind": "agent", **requested}

    # An unrelated editor save must preserve even an unresolved actor's original bytes.
    reloaded["files"]["README.md"] = "Edited instructions.\n"
    saved = store.update_playbook("review-demo", reloaded["revision"], reloaded["files"])
    assert saved["files"]["flows/check.toml"] == contents["flows/check.toml"]
    entity = projects.create(
        "Authoring acceptance", folders=[str(folder)], default_folder=str(folder)
    )
    inputs = {**inputs, "revision": saved["revision"], "project_id": entity.id}
    plan = review.build("review-demo", inputs, key=KEY)
    [assignment] = plan.public["assignments"]
    assert assignment["requested"] == requested
    assert assignment["assignment"] == (requested if state == "available" else None)
    assert bool(assignment["reason"]) == (state != "available")
    receipt = review.confirm(plan, plan.public["digest"], [], key=KEY)["receipt"]
    bound = lifecycle.bind(entity.id, "review-demo", inputs, receipt, "bind-authoring-one", key=KEY)
    result = apply.apply(entity.id, "apply-authoring-one", bound["receipt"], key=KEY)
    assert result["state"] == "applied"
    assert record(entity.id)["review_facts"]["assignments"] == [assignment]
    deployed = (folder / "docs" / "playbook.md").read_text()
    if state == "available":
        assert f"agent `{provider.engine_id}`" in deployed
        assert "requested model `default`" in deployed
    else:
        assert "Actor: agent, unassigned" in deployed
        assert "requested model" not in deployed
    assert (
        store.get_playbook("review-demo")["files"]["flows/check.toml"]
        == contents["flows/check.toml"]
    )
