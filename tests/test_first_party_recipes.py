"""Pinned release recipes select closed platform/login/probe behavior, never free argv."""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_sessions.plugins import feed, manager, manifest, process

RECIPES = Path(__file__).parents[1] / "release" / "recipes" / "linux-x64"


@pytest.mark.parametrize("path", sorted(RECIPES.glob("*.json")), ids=lambda p: p.stem)
def test_release_recipe_is_accepted_as_signed_data(path):
    entry = feed.entry(json.loads(path.read_text()), signed=True)
    assert entry.manifest.install.platform == "linux-x64"
    assert entry.manifest.binary.version_flag == "--version"
    assert entry.manifest.probe_kind != "terminal"
    assert {"new", "resume", "transcript"} <= set(entry.manifest.verify)


@pytest.mark.parametrize("path", sorted(RECIPES.glob("*.json")), ids=lambda p: p.stem)
def test_recipe_probe_and_signin_are_fixed_and_resume_validates_identity(path, tmp_path):
    m = feed.entry(json.loads(path.read_text()), signed=True).manifest
    prov = SimpleNamespace(
        manifest=m,
        entrypoint_path=lambda: "/fixture/vendor",
        id_pattern=m.session_id.pattern,
        new_session_reconciles=m.session_id.mint == "adopt",
    )
    native = "ses_0123456789abcdefghijklmnop" if m.probe_kind == "run-session" else str(uuid4())
    # The conversation-store kind is authoritative; never pass a path/option as a session id.
    if not prov.id_pattern.fullmatch(native):
        native = "session_" + str(uuid4())
    assert prov.id_pattern.fullmatch(native)
    op = str(uuid4())
    for purpose in ("new", "resume"):
        argv = process._argv(prov, purpose, tmp_path, native, op)
        assert argv[0] == "/fixture/vendor"
        assert process.probe_message(op, purpose) in argv
        assert not {
            "-y",
            "--yolo",
            "--skip-trust",
            "--dangerously-skip-permissions",
            "--dangerously-bypass-approvals-and-sandbox",
        }.intersection(argv)
        if purpose == "resume":
            assert native in argv
    with pytest.raises(process.ProcessError, match="session identity"):
        process._argv(prov, "resume", tmp_path, "--arbitrary-option", op)
    with pytest.raises((ValueError, process.ProcessError)):
        process._argv(prov, "new", tmp_path, native, "not-a-server-uuid")
    signin = process._argv(prov, "signin", tmp_path, None)
    assert signin[1:] in ([], ["login"], ["auth", "login"])


@pytest.mark.parametrize("system,machine", [("Linux", "aarch64"), ("Darwin", "x86_64")])
def test_pinned_platform_refuses_other_hosts(system, machine, monkeypatch):
    import platform

    entry = feed.entry(json.loads((RECIPES / "codex.json").read_text()), signed=True)
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    with pytest.raises(manager.ManagerError, match="requires linux-x64"):
        manager._check_platform(entry)


@pytest.mark.parametrize(
    "block",
    [
        {"probe": {"kind": "exec-readonly", "argv": ["--yolo"]}},
        {"probe": {"kind": "free-command"}},
        {"signin": {"kind": "auth-login", "subcommand": "anything"}},
        {"signin": {"kind": "interactive", "argv": ["--yolo"]}},
    ],
)
def test_closed_probe_and_signin_vocabulary_refuses_added_authority(block):
    recipe = json.loads((RECIPES / "codex.json").read_text())
    recipe["manifest"].update(block)
    with pytest.raises(manifest.ManifestError):
        feed.entry(recipe, signed=True)


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/@/codex/bin/codex",
        "node_modules/@scope/../codex",
        "@scope/codex",
        "node_modules/@scope/@nested/codex",
        "node_modules/@scope/./codex",
        "node_modules/@scope/codex\n",
    ],
)
def test_npm_scope_exception_cannot_expand_other_path_grammar(path):
    with pytest.raises(manifest.ManifestError):
        manifest.relative_path(path, "install.entrypoint", npm_entrypoint=True)


def test_store_and_tarball_paths_still_refuse_npm_scopes():
    with pytest.raises(manifest.ManifestError):
        manifest.relative_path("node_modules/@scope/codex", "store.path")


def test_run_session_probe_does_not_inherit_default_allow_or_reuse_an_agent(tmp_path):
    names = set()
    for _ in range(2):
        argv = process._wrapped(
            ["/fixture/vendor"], tmp_path, "fixture.service", 90, probe_kind="run-session"
        )
        inline = next(x for x in argv if x.startswith("OPENCODE_CONFIG_CONTENT="))
        cfg = json.loads(inline.split("=", 1)[1])
        agent = cfg["default_agent"]
        names.add(agent)
        assert cfg["permission"] == {"*": "deny"}
        assert cfg["agent"] == {agent: {"mode": "primary", "permission": {"*": "deny"}}}
    assert len(names) == 2
