"""Release examples must work through the installed loader and ordinary local authoring."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from agent_sessions import fileedit
from agent_sessions.playbooks import loader, store
from agent_sessions.playbooks.errors import PlaybookFormatError
from agent_sessions.playbooks.tree import read_tree


@pytest.fixture(autouse=True)
def isolated_operator_state(tmp_path, monkeypatch):
    """This module is copied outside the checkout for wheel checks, without conftest (#1385)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(home / ".local" / "share" / "agent-sessions"))
    monkeypatch.setenv(store.ENV, str(home / ".config" / "agent-sessions" / "playbooks"))
    monkeypatch.setenv("AGENT_SESSIONS_NO_SERVICE", "1")


@pytest.fixture
def linked_release(tmp_path, monkeypatch):
    """uv's default installation shape, without linking or modifying checkout/cache resources."""
    root = tmp_path / "installed"
    shutil.copytree(loader.BUNDLED_ROOT, root)
    cache = tmp_path / "cache"
    cache.mkdir()
    for i, path in enumerate(sorted(root.rglob("*"))):
        if path.is_file():
            os.link(path, cache / str(i))
            assert path.stat().st_nlink == 2
    monkeypatch.setattr(loader, "BUNDLED_ROOT", root)
    monkeypatch.setattr(loader, "_INSTALLED_ROOT", root)
    return root


@pytest.mark.deploy_shape
@pytest.mark.parametrize("pid", ["forgejo-workflow", "research-brief"])
def test_release_bundle_is_installed_valid_and_inert(pid):
    # No path into the checkout: CI repeats this test from outside the source tree, against a wheel.
    bundle = loader.load_bundle(loader.BUNDLED_ROOT / pid)
    cards = {card["id"]: card for card in loader.list_bundles()}
    assert cards[pid]["ok"] is True
    assert bundle["default_flow"] in bundle["flows"]
    assert bundle["materials"] == [] and bundle["rituals"] == []
    assert not any(bundle["capabilities"].values())
    assert all(variable["kind"] != "secret" for variable in bundle["variables"])
    assert all(connection["credential"] is None for connection in bundle["connections"])
    assert "Saving does not execute" in bundle["readme"]


@pytest.mark.deploy_shape
def test_forgejo_example_keeps_review_identity_authorization_and_merge_revision_explicit():
    bundle = loader.load_bundle(loader.BUNDLED_ROOT / "forgejo-workflow")
    steps = {step["id"]: step for step in bundle["flows"]["issue-to-live"]["steps"]}
    assert steps["review"]["rework"] == {
        "to": "implement",
        "when": "review_approved",
        "max_rounds": 4,
    }
    assert steps["review"]["actor"]["kind"] == "external"
    for sid, key in [("merge", "merge_authorized"), ("deploy", "deployment_authorized")]:
        assert steps[sid]["actor"]["kind"] == "operator"
        item = next(item for item in steps[sid]["checklist"] if item["key"] == key)
        assert item["required"] and item["probe"] == "none"
    revision = next(
        item for item in steps["verify"]["checklist"] if item["probe"] == "http_revision"
    )
    # The probe derives the observed MERGE SHA. A branch-head expectation breaks squash merges.
    assert revision["required"] and revision["probe_args"] == {"url": "{{revision_url}}"}
    assert all(variable["default"] is None for variable in bundle["variables"])


@pytest.mark.parametrize("pid", ["forgejo-workflow", "research-brief"])
def test_bundled_example_duplicates_edits_and_preserves_references_without_mutating_source(pid):
    assert fileedit.install_lease_signal_handler()
    original = store.get_playbook(pid)
    assert original["source"] == "bundled" and not original["editable"]
    with pytest.raises(store.ReadOnly):
        store.update_playbook(pid, original["revision"], original["files"])
    duplicate = store.duplicate_playbook(pid, {"revision": original["revision"]})
    assert duplicate["source"] == "local" and duplicate["editable"]
    path = next(path for path in duplicate["documents"] if path.startswith("flows/"))
    document = duplicate["documents"][path]
    source_path = next(path for path in original["documents"] if path.startswith("flows/"))
    source_ids = {step["id"] for step in original["documents"][source_path]["steps"]}
    ids = {step["id"] for step in document["steps"]}
    assert ids.isdisjoint(source_ids)
    review = next(step for step in document["steps"] if step.get("rework"))
    assert review["rework"]["to"] in ids
    assert all(step_id in ids for step in document["steps"] for step_id in step.get("after", []))
    assert all(
        ref["step"] in ids for step in document["steps"] for ref in step.get("distinct_from", [])
    )
    document["title"] = "My reviewed workflow"
    files = {**duplicate["files"], path: {"toml": document}}
    saved = store.update_playbook(duplicate["id"], duplicate["revision"], files)
    assert saved["documents"][path]["title"] == "My reviewed workflow"
    assert store.get_playbook(pid)["revision"] == original["revision"]
    assert store.list_playbooks()["default"] is None


@pytest.mark.parametrize("pid", ["forgejo-workflow", "research-brief"])
def test_cache_linked_release_can_be_browsed_and_duplicated(linked_release, pid):
    assert loader.load_bundle(linked_release / pid)["identity"]["id"] == pid
    assert all(card["ok"] for card in loader.list_bundles())
    original = store.get_playbook(pid)
    duplicate = store.duplicate_playbook(pid, {"revision": original["revision"]})
    assert duplicate["editable"] and duplicate["source"] == "local"
    assert read_tree(store.local_root() / duplicate["id"]).files
    assert all(
        path.stat().st_nlink == 1
        for path in (store.local_root() / duplicate["id"]).rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize(
    "change", ["changed-bytes", "missing-file", "extra-file", "extra-dir", "symlink"]
)
def test_installed_release_requires_exact_bytes_inventory_and_regular_nodes(linked_release, change):
    root = linked_release / "forgejo-workflow"
    readme = root / "README.md"
    if change == "changed-bytes":
        readme.write_text("different content")
    elif change == "missing-file":
        readme.unlink()
    elif change == "extra-file":
        (root / "extra.md").write_text("unexpected")
    elif change == "extra-dir":
        (root / "empty").mkdir()
    else:
        target = root.parent / "readme-copy"
        readme.rename(target)
        readme.symlink_to(target)
    with pytest.raises(PlaybookFormatError):
        loader.load_bundle(root)
    cards = {card["id"]: card for card in loader.list_bundles()}
    assert cards["forgejo-workflow"]["ok"] is False
    assert cards["research-brief"]["ok"] is True


@pytest.mark.parametrize("source", [store.SOURCE_LOCAL, store.SOURCE_CATALOG, store.SOURCE_BUNDLED])
def test_a_source_label_or_known_bundle_bytes_never_admits_untrusted_hardlinks(
    linked_release, tmp_path, monkeypatch, source
):
    monkeypatch.setattr(loader, "_INSTALLED_ROOT", tmp_path / "other-installation")
    fd = os.open(linked_release, os.O_RDONLY | os.O_DIRECTORY)
    try:
        entry = store._entry_at(fd, "forgejo-workflow", source)
    finally:
        os.close(fd)
    assert "hard link" in entry.error
    with pytest.raises(PlaybookFormatError, match="hard link"):
        loader.load_bundle(linked_release / "forgejo-workflow")


def test_portable_module_preserves_an_inherited_operator_store(tmp_path):
    """Exercise the actual outside-checkout entry point, without repository conftest (#1385)."""
    home = tmp_path / "operator-home"
    operator_store = home / ".config" / "agent-sessions" / "playbooks"
    operator_store.mkdir(parents=True)
    (operator_store / ".sentinel").write_bytes(b"existing operator state\n")
    work = tmp_path / "portable"
    work.mkdir()
    shutil.copyfile(__file__, work / "test_bundled_playbooks.py")

    def snapshot():
        return {
            str(path.relative_to(home)): path.read_bytes() if path.is_file() else None
            for path in home.rglob("*")
        }

    before = snapshot()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_SESSIONS_")
        and key not in {"PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        AGENT_SESSIONS_HOME=str(home / ".local" / "share" / "agent-sessions"),
        AGENT_SESSIONS_PLAYBOOKS_DIR=str(operator_store),
        AGENT_SESSIONS_NO_SERVICE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--noconftest",
            "-k",
            "not portable_module_preserves",
            "test_bundled_playbooks.py",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot() == before
