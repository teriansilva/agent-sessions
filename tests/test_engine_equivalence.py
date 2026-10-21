"""#853 P2 — the seven-engine equivalence matrix.

The GOLDEN table below was recorded from the provider classes' own `launch_argv` /
`new_launch_argv` and capability attributes on `main` @ 2ab9871, immediately before P2 moved that
data into the first-party manifests. It is the contract the manifest-built roster must keep: any
drift in a flag, an order or a capability shows up here as a diff against what shipped.

`{bin}` is the provenance-checked entrypoint and `{cwd}` the launch directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_sessions import engines

UUID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"
PLACEHOLDER = f"new-{UUID}"
NATIVE = {
    "claude": UUID,
    "opencode": "ses_AbC123",
    "codex": UUID,
    "gemini": UUID,
    "antigravity": UUID,
    "kimi": f"session_{UUID}",
    "shell": UUID,
}
# (supports_new, supports_orchestrator_input, expects_raw_tty, supports_seed_start,
#  new_session_reconciles)
GOLDEN = {
    ("antigravity", "caps"): (True, True, True, False, True),
    ("antigravity", "new", False): ["{bin}"],
    ("antigravity", "new", True): ["{bin}", "--dangerously-skip-permissions"],
    ("antigravity", "resume", False): [
        "{bin}",
        "--conversation",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
    ],
    ("antigravity", "resume", True): [
        "{bin}",
        "--conversation",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
        "--dangerously-skip-permissions",
    ],
    ("claude", "caps"): (True, True, True, True, False),
    ("claude", "new", False): ["{bin}", "--session-id", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("claude", "new", True): [
        "{bin}",
        "--session-id",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
        "--dangerously-skip-permissions",
    ],
    ("claude", "resume", False): ["{bin}", "--resume", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("claude", "resume", True): [
        "{bin}",
        "--resume",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
        "--dangerously-skip-permissions",
    ],
    ("codex", "caps"): (True, True, True, True, True),
    ("codex", "new", False): ["{bin}", "--cd", "{cwd}"],
    ("codex", "new", True): [
        "{bin}",
        "--cd",
        "{cwd}",
        "--dangerously-bypass-approvals-and-sandbox",
    ],
    ("codex", "resume", False): ["{bin}", "resume", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("codex", "resume", True): ["{bin}", "resume", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("gemini", "caps"): (True, True, True, False, False),
    ("gemini", "new", False): ["{bin}", "--session-id", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("gemini", "new", True): [
        "{bin}",
        "--session-id",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
        "--yolo",
        "--skip-trust",
    ],
    ("gemini", "resume", False): ["{bin}", "--resume", "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("gemini", "resume", True): [
        "{bin}",
        "--resume",
        "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b",
        "--yolo",
        "--skip-trust",
    ],
    ("kimi", "caps"): (True, True, True, True, True),
    ("kimi", "new", False): ["{bin}"],
    ("kimi", "new", True): ["{bin}", "-y"],
    ("kimi", "resume", False): ["{bin}", "-S", "session_0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"],
    ("kimi", "resume", True): ["{bin}", "-S", "session_0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b", "-y"],
    ("opencode", "caps"): (True, True, True, True, True),
    ("opencode", "new", False): ["{bin}", "{cwd}"],
    ("opencode", "new", True): ["{bin}", "{cwd}"],
    ("opencode", "resume", False): ["{bin}", "{cwd}", "--session", "ses_AbC123"],
    ("opencode", "resume", True): ["{bin}", "{cwd}", "--session", "ses_AbC123"],
    ("shell", "caps"): (True, False, False, False, False),
    ("shell", "new", False): ["{bin}", "-l"],
    ("shell", "new", True): ["{bin}", "-l"],
    ("shell", "resume", False): ["{bin}", "-l"],
    ("shell", "resume", True): ["{bin}", "-l"],
}

# The seven P2 engines in their historic order, plus the API agent (#1209, display order 80).
ORDER = [
    "claude",
    "claude-api",
    "opencode",
    "opencode-api",
    "codex",
    "codex-api",
    "gemini",
    "antigravity",
    "kimi",
    "apichat",
    "shell",
]
#: The seven terminal engines P2 converted — what "shipped" means in the equivalence tables below.
#: The API agent (#1209) and the native API clients (#1311) run no terminal argv of their own.
SEVEN = [e for e in ORDER if e not in ("apichat", "codex-api", "claude-api", "opencode-api")]


def test_roster_is_the_seven_in_their_historic_order():
    assert [p.engine_id for p in engines.all_providers()] == ORDER


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    b = tmp_path / "bin" / "agent"
    b.parent.mkdir()
    b.parent.chmod(0o755)  # independent of the runner's umask / group layout
    b.write_bytes(b"#!/bin/true\n")
    b.chmod(0o755)
    for p in engines.all_providers():
        if p.manifest.binary is not None:  # a `chat` engine (#1209) runs no binary
            monkeypatch.setenv(p.manifest.binary.env_var, str(b))
    return b


def _fill(argv, b, cwd):
    return [x.replace("{bin}", str(b)).replace("{cwd}", cwd) for x in argv]


@pytest.mark.parametrize("engine", SEVEN)
@pytest.mark.parametrize("bypass", [True, False])
def test_argv_matches_what_shipped(engine, bypass, fake_bin, tmp_path):
    prov = engines.get(engine)
    cwd = str(tmp_path / "proj")
    native = NATIVE[engine]
    new_id = PLACEHOLDER if prov.new_session_reconciles else native
    assert prov.launch_argv(native, cwd=cwd, bypass=bypass) == _fill(
        GOLDEN[(engine, "resume", bypass)], fake_bin, cwd
    )
    assert prov.new_launch_argv(new_id, cwd=cwd, bypass=bypass) == _fill(
        GOLDEN[(engine, "new", bypass)], fake_bin, cwd
    )


@pytest.mark.parametrize("engine", SEVEN)
def test_capabilities_match_what_shipped(engine):
    p = engines.get(engine)
    assert (
        p.supports_new,
        p.supports_orchestrator_input,
        p.expects_raw_tty,
        p.supports_seed_start,
        p.new_session_reconciles,
    ) == GOLDEN[(engine, "caps")]


@pytest.mark.parametrize("engine", SEVEN)
def test_ids_are_accepted_and_refused_as_before(engine):
    p = engines.get(engine)
    assert engines.parse_key(f"{engine}:{NATIVE[engine]}") == (p, NATIVE[engine])
    for bad in ("../etc", "--yolo", "", "x" * 200, f"{NATIVE[engine]}\n"):
        with pytest.raises(engines.EngineError):
            engines.parse_key(f"{engine}:{bad}")
    reconciles = GOLDEN[(engine, "caps")][4]
    if reconciles:
        assert (
            engines.parse_key(f"{engine}:{PLACEHOLDER}", allow_new_placeholder=True)[1]
            == PLACEHOLDER
        )
    with pytest.raises(engines.EngineError):
        engines.parse_key(f"{engine}:{PLACEHOLDER}")


def test_bare_ids_still_mean_claude():
    assert engines.parse_key(UUID)[0].engine_id == "claude"


@pytest.mark.parametrize("engine", SEVEN)
def test_every_kind_hook_is_reached_through_the_provider(engine):
    import inspect

    from agent_sessions.plugins.provider import KIND_HOOKS

    p = engines.get(engine)
    for hook in KIND_HOOKS:
        k = getattr(p.kind, hook, None)
        if callable(k):
            assert inspect.unwrap(getattr(p, hook)) == k, hook
        else:
            assert getattr(p, hook, None) is None, hook
    assert p.kind.owner is p


def test_the_live_roster_reads_in_tree_manifests_only(tmp_path, monkeypatch):
    # A valid, non-colliding local manifest in the plugins dir must never become a live engine.
    import json
    import tomllib

    from agent_sessions.engines import registry
    from agent_sessions.plugins import FIRST_PARTY_DIR

    d = tomllib.loads((FIRST_PARTY_DIR / "gemini" / "plugin.toml").read_text())
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    d["store"]["layout"] = "gemini-tmp"
    local = tmp_path / "plugins" / "myagent"
    local.mkdir(parents=True)
    (local / "plugin.json").write_text(json.dumps(d))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))
    roster = registry._build_roster()
    assert [p.engine_id for p in roster] == ORDER


def test_a_manifest_naming_the_wrong_kind_is_refused():
    from agent_sessions.engines.codex import CodexProvider

    with pytest.raises(ValueError, match="not for engine"):
        engines.get("claude").__class__(
            engines.get("claude").manifest, trust="first-party", root=Path("/nonexistent")
        ).attach_kind(CodexProvider())


@pytest.mark.deploy_shape
def test_the_installed_package_carries_all_seven_manifests():
    """Runs again in pr-validate's wheel pass against site-packages (#704): the roster is built
    from package data, so a wheel missing the manifests would start with no engines."""
    import agent_sessions
    from agent_sessions.plugins import FIRST_PARTY_DIR, load_first_party

    assert FIRST_PARTY_DIR.is_relative_to(Path(agent_sessions.__file__).parent)
    loaded = load_first_party()
    assert loaded.problems == {}
    assert list(loaded.providers) == ORDER


def test_an_absent_record_never_blocks_a_launch(tmp_path, monkeypatch, fake_bin):
    """No record grants nothing, so nothing is verified: a state dir whose ANCESTOR fails the
    ownership walk (a CI runner with another primary group) must not stop an in-tree engine."""
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    try:
        monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(shared / "state"))
        prov = engines.get("gemini")
        monkeypatch.setattr(prov, "state_dir", shared / "state")
        prov._cached = None
        assert prov.launch_argv(UUID, cwd=str(tmp_path), bypass=False)[0] == str(fake_bin)
        # …while a record that DOES exist there is walked and refused.
        (shared / "state").mkdir()
        (shared / "state" / "gemini.json").write_text("{}")
        prov._cached = None
        with pytest.raises(engines.EngineError, match="writable by other users"):
            prov.launch_argv(UUID, cwd=str(tmp_path), bypass=False)
    finally:
        shared.chmod(0o755)


@pytest.mark.parametrize(
    "first", ["agent_sessions.plugins", "agent_sessions.plugins.provider", "agent_sessions.engines"]
)
def test_the_packages_import_in_any_order(first):
    """The registry is built from `plugins`, so `plugins` must never import the `engines` package
    at module load — importing it first used to be a circular ImportError."""
    import subprocess
    import sys

    code = (
        f"import {first}; from agent_sessions import engines; print(len(engines.all_providers()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == str(len(ORDER))


def test_the_ui_and_the_launcher_ask_the_same_question(tmp_path, monkeypatch):
    """An engine whose binary is only on PATH is not offered: `/api/engines`,
    `new_session_engines` and the handoff check read `launchable_bin`, which is the launcher's
    own provenance answer — offering what the launcher then refuses was a 4500 with no reason."""
    on_path = tmp_path / "pathdir" / "gemini"
    on_path.parent.mkdir()
    on_path.parent.chmod(0o755)
    on_path.write_bytes(b"#!/bin/true\n")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{on_path.parent}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENT_SESSIONS_GEMINI_BIN", raising=False)
    prov = engines.get("gemini")
    prov._cached = None
    assert engines.launchable_bin(prov) is None
    with pytest.raises(engines.EngineError):
        prov.new_launch_argv(UUID, cwd=str(tmp_path), bypass=False)
    monkeypatch.setenv("AGENT_SESSIONS_GEMINI_BIN", str(on_path))
    assert engines.launchable_bin(prov) == str(on_path)
