"""Verify runs the declared checks on an applied deployment; it changes and probes nothing."""

import os

import pytest

from agent_sessions import mission_probes, projects, template_vars
from agent_sessions.playbooks import apply, instructions, lifecycle, review, store, verify
from test_playbook_review import KEY, files
from test_playbook_review import setup as _review_setup

setup = _review_setup
ALL = '["variables", "materials", "binaries", "instructions", "connections", "capabilities"]'


@pytest.fixture
def applied(setup, monkeypatch):
    folder, body = setup
    contents = files()
    contents["playbook.toml"] = (
        contents["playbook.toml"]
        .replace("format = 2\n", f"format = 2\nverify = {ALL}\n", 1)
        .replace('path = "RULES.md"', 'path = "CLAUDE.md"')
        + '\n[requires]\nbinaries = ["sh"]\n'
    )
    contents["template/CLAUDE.md"] = contents.pop("template/RULES.md")
    tree = store.tree_from_files(contents)
    (store.local_root() / "review-demo" / "template" / "RULES.md").unlink()  # renamed above
    for relative, data in tree.files.items():
        path = store.local_root() / "review-demo" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(instructions, "present", lambda: {"alpha": ["AGENTS.md"]})
    entity = projects.create("Verify", folders=[str(folder)], default_folder=str(folder))
    inputs = {
        **body,
        "revision": tree.digest(),
        "bindings": [{"name": "token", "kind": "secret", "value": "verify-test-secret"}],
        "project_id": entity.id,
    }
    plan = review.build("review-demo", inputs, key=KEY)
    receipt = review.confirm(plan, plan.public["digest"], ["connection:service"], key=KEY)
    bound = lifecycle.bind(
        entity.id, "review-demo", inputs, receipt["receipt"], "bind-op-10000", key=KEY
    )
    apply.apply(entity.id, "apply-op-10000", bound["receipt"], key=KEY)
    return entity, folder


def test_a_healthy_deployment_verifies_and_reports_each_declared_check(applied, monkeypatch):
    entity, _ = applied
    monkeypatch.setattr(
        mission_probes, "_probe_http", lambda *a, **k: pytest.fail("verify must never probe")
    )
    result = verify.verify(entity.id)
    assert result["ok"] is True and set(result["checks"]) == {
        "variables",
        "materials",
        "binaries",
        "instructions",
        "connections",
        "capabilities",
    }
    assert result["checks"]["connections"]["probed"] is False
    assert [t["id"] for t in result["checks"]["connections"]["targets"]] == ["connection:service"]
    assert "verify-test-secret" not in repr(result)


def test_material_drift_is_listed_by_path(applied):
    entity, folder = applied
    (folder / "CLAUDE.md").write_text("an operator edit\n")
    result = verify.verify(entity.id)
    assert result["ok"] is False and result["checks"]["materials"]["drift"] == ["CLAUDE.md"]
    assert result["checks"]["instructions"]["missing"] == ["alpha"]  # its source is no longer ours


def test_a_missing_binding_and_a_missing_binary_are_reported(applied, monkeypatch):
    entity, _ = applied

    def drop(records):
        records[:] = [r for r in records if not (r["project_id"] == entity.id)]

    template_vars._mutate(drop)
    monkeypatch.setattr("agent_sessions.playbooks.loader.shutil.which", lambda name: None)
    result = verify.verify(entity.id)
    assert result["checks"]["variables"]["problems"] == {"token": "missing"}
    assert result["checks"]["binaries"]["missing"] == ["sh"]


def test_only_declared_checks_run_and_nothing_applied_refuses(setup):
    folder, body = setup
    entity = projects.create("Empty", folders=[str(folder)], default_folder=str(folder))
    with pytest.raises(store.Conflict, match="no applied deployment"):
        verify.verify(entity.id)


def test_verify_changes_nothing(applied):
    entity, folder = applied
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in folder.iterdir()}
    verify.verify(entity.id)
    assert {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in folder.iterdir()} == before
    assert os.readlink(folder / "AGENTS.md") == "CLAUDE.md"


def test_the_verify_route_needs_a_session_and_is_never_cached(applied, auth_cfg):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    entity, _ = applied
    c = TestClient(create_app(replace(auth_cfg, secret_key=KEY)), base_url=auth_cfg.origin)
    url = f"/api/projects/{entity.id}/playbook/verify"
    assert c.get(url).status_code == 401
    c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        headers={"Origin": auth_cfg.origin},
        follow_redirects=False,
    )
    r = c.get(url)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.headers["cache-control"] == "no-store"


def _edit_source(transform):
    root = store.local_root() / "review-demo"
    toml = root / "playbook.toml"
    toml.write_text(transform(toml.read_text()))


def test_verify_runs_the_applied_checks_even_after_the_source_drops_one(applied):
    entity, folder = applied
    # The operator edits the playbook so it no longer declares `materials`, without re-applying.
    _edit_source(lambda t: t.replace(ALL, '["variables"]'))
    (folder / "CLAUDE.md").write_text("an operator edit\n")
    result = verify.verify(entity.id)
    assert result["update_available"] is True
    assert "materials" in result["checks"] and result["checks"]["materials"]["drift"] == [
        "CLAUDE.md"
    ]
    assert result["ok"] is False


def test_a_pre_snapshot_deployment_refuses_once_the_source_changed(applied):
    from agent_sessions.playbooks import deployment_state

    entity, _ = applied
    with deployment_state.locked(entity.id) as locked:
        rec = locked.read()
        rec.pop("verify_facts")  # applied before the snapshot was recorded
        locked.write(rec)
    assert verify.verify(entity.id)["ok"] is True  # unchanged source may stand in
    _edit_source(lambda t: t.replace(ALL, '["variables"]'))
    with pytest.raises(store.Conflict, match="re-apply"):
        verify.verify(entity.id)


@pytest.mark.parametrize("damage", ["invalid", "missing"])
def test_the_applied_checks_run_when_the_source_is_invalid_or_gone(applied, damage):
    import shutil

    entity, folder = applied
    root = store.local_root() / "review-demo"
    if damage == "invalid":
        (root / "playbook.toml").write_text("this is not = valid [toml\n")
    else:
        shutil.rmtree(root)
    (folder / "CLAUDE.md").write_text("an operator edit\n")
    result = verify.verify(entity.id)
    assert result["update_available"] is None  # unknown, never guessed
    assert result["checks"]["materials"]["drift"] == ["CLAUDE.md"] and result["ok"] is False
