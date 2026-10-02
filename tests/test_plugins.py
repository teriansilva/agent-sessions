"""#853 P1 — the plugin manifest contract: schema, closed value space, executable provenance.

Four blocks:

1. the validator — unknown fields, contract/migration, the id-pattern grammar, the closed flag /
   kind / authority vocabularies, default-deny capabilities;
2. argv parity — each of the seven fixture manifests under `tests/fixtures/plugins/` assembles the
   SAME launch/new argv as today's provider class (P2 turns this into the deletion gate);
3. the §2b negative matrix — every case refused before a probe could run;
4. the loader — fail-soft per plugin.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tomllib
from pathlib import Path

import pytest

from agent_sessions.engines.base import EngineError
from agent_sessions.plugins import (
    ManifestError,
    PluginProvider,
    kinds,
    load_all,
    parse,
    provenance,
)
from agent_sessions.plugins import manifest as manifest_mod

FIXTURES = Path(__file__).parent.parent / "src" / "agent_sessions" / "plugins" / "first_party"
SEVEN = ("claude", "opencode", "codex", "gemini", "antigravity", "kimi", "shell")
UUID = "0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"
NATIVE = {
    "claude": UUID,
    "opencode": "ses_AbC123",
    "codex": UUID,
    "gemini": UUID,
    "antigravity": UUID,
    "kimi": f"session_{UUID}",
    "shell": UUID,
}
PLACEHOLDER = "new-0190a3b2-1c2d-7e3f-8a9b-0c1d2e3f4a5b"


def doc(engine: str = "gemini") -> dict:
    return tomllib.loads((FIXTURES / engine / "plugin.toml").read_text())


def exe(path: Path, body: bytes = b"#!/bin/true\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o755)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- 1. the validator -----------------------------------------------------------------------------


@pytest.mark.parametrize("engine", SEVEN)
def test_every_fixture_manifest_validates(engine):
    m = parse(doc(engine))
    assert m.id == engine


def test_capabilities_are_default_deny():
    d = doc()
    del d["capabilities"]
    d["launch"].pop("new")
    m = parse(d)
    for c in kinds.CAPABILITIES:
        assert m.can(c) is False
    p = PluginProvider(m, trust=provenance.FIRST_PARTY, root=Path("/nonexistent"))
    assert (
        p.supports_orchestrator_input,
        p.expects_raw_tty,
        p.supports_seed_start,
        p.supports_new,
    ) == (
        False,
        False,
        False,
        False,
    )


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda d: d.update(hooks={"pre": "x"}), "hooks"),
        (lambda d: d["binary"].update(argv=["gemini", "--x"]), "binary.argv"),
        (lambda d: d["launch"]["resume"].update(value="{id}"), "launch.resume.value"),
        (lambda d: d["identity"].update(adapter="mod:fn"), "identity.adapter"),
        (lambda d: d["usage"].update(argv=["gemini", "/usage"]), "usage.argv"),
    ],
)
def test_unknown_fields_are_rejected_at_every_level(mutate, field):
    d = doc()
    mutate(d)
    with pytest.raises(ManifestError) as e:
        parse(d)
    assert e.value.field == field


@pytest.mark.parametrize(
    "contract, needle", [(None, "required"), (0, "not a valid"), (2, "newer BattleLab")]
)
def test_contract_outside_this_build_is_refused(contract, needle):
    d = doc()
    if contract is None:
        del d["contract"]
    else:
        d["contract"] = contract
    with pytest.raises(ManifestError, match=needle):
        parse(d)


def test_older_contract_is_migrated_forward(monkeypatch):
    # Simulate the day contract 2 exists: a contract-1 document is rewritten, then validated.
    monkeypatch.setattr(kinds, "CONTRACT_CURRENT", 2)
    seen = []

    def one_to_two(d):
        seen.append(d["contract"])
        return d

    monkeypatch.setitem(manifest_mod.MIGRATIONS, 1, one_to_two)
    m = parse(doc())
    assert seen == [1] and m.contract == 2


@pytest.mark.parametrize(
    "pattern",
    [
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        r"^ses_[A-Za-z0-9]+$",
        r"^session_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ],
)
def test_id_grammar_accepts_the_shapes_in_use(pattern):
    manifest_mod.compile_id_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"^.*$",  # the canonical sloppy pattern
        r"^[a-z].*$",
        r"[a-z]+",  # unanchored
        r"^[a-z]+",  # missing $
        r"^(a|b)+$",  # groups / alternation
        r"^a*$",
        r"^a?$",
        r"^\d+$",
        r"^[^/]+$",  # negated class — would admit '/' and shell metacharacters
        r"^a+$",  # + on a literal
        r"^a{0}$",
        r"^[a]{0,3}[b]$",
        r"^[a-z/]+$",
        r"^$",
    ],
)
def test_id_grammar_rejects_everything_else(pattern):
    with pytest.raises(ManifestError):
        manifest_mod.compile_id_pattern(pattern)


def test_native_ids_are_capped_and_never_look_like_options():
    d = doc()
    d["session_id"]["pattern"] = r"^[a-z\-]+$"
    m = parse(d)
    assert m.session_id.accepts("abc")
    assert not m.session_id.accepts("--yolo")
    assert not m.session_id.accepts("a" * 129)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["launch"]["resume"].update(flag="--exec"),
        lambda d: d["launch"]["resume"].update(kind="argv"),
        lambda d: d["launch"].update(bypass=["--allow-everything"]),
        lambda d: d["launch"].update(base_args=["-c"]),
        lambda d: d["launch"].__setitem__("resume", {"kind": "subcommand", "subcommand": "exec"}),
        lambda d: d["binary"].update(version_flag="--eval"),
        lambda d: d["usage"].update(source="plan", kind="curl-probe"),
        lambda d: d["usage"].update(access="curl-probe"),
        lambda d: d.update(verify=["binary", "run-this"]),
        lambda d: d.update(signin={"kind": "cli-subcommand", "subcommand": "rm"}),
        lambda d: d["display"].update(accent="#ff0000"),
        lambda d: d["store"].update(layout="python-module"),
    ],
)
def test_the_value_space_is_closed(mutate):
    d = doc()
    mutate(d)
    with pytest.raises(ManifestError):
        parse(d)


@pytest.mark.parametrize(
    "path",
    [
        "relative/bin",
        "~/../etc",
        "/usr/*/bin",
        "~/.local//bin",
        "~user/bin",
        "/opt/./x",
        "/opt/a b",
    ],
)
def test_paths_are_anchored_and_literal(path):
    d = doc()
    d["binary"]["search_paths"] = [path]
    with pytest.raises(ManifestError, match="search_paths"):
        parse(d)


def test_store_paths_stay_under_their_root():
    d = doc("opencode")
    d["store"]["paths"]["db"] = "../../.ssh/id_rsa"
    with pytest.raises(ManifestError, match="store.paths.db"):
        parse(d)
    d = doc("opencode")
    d["store"]["paths"]["secrets"] = "x"
    with pytest.raises(ManifestError, match="store.paths.secrets"):
        parse(d)


def _install(**over):
    base = {
        "kind": "npm-prefix",
        "authority": "registry.npmjs.org",
        "package": "@google/gemini-cli",
        "version": "0.9.0",
        "digest": "sha256:" + "a" * 64,
        "entrypoint": "node_modules/.bin/gemini",
    }
    base.update(over)
    return base


def test_install_block_validates_authority_digest_and_entrypoint():
    d = doc()
    d["install"] = _install()
    assert parse(d).install.authority == "registry.npmjs.org"
    for bad in (
        {"authority": "evil.example"},
        {"authority": "github.com"},  # allowed for tarball, not npm
        {"digest": "md5:abc"},
        {"entrypoint": "../../bin/sh"},
        {"entrypoint": "node_modules/.bin/bash"},  # must end in binary.name
        {"kind": "curl-pipe"},
    ):
        d = doc()
        d["install"] = _install(**bad)
        with pytest.raises(ManifestError):
            parse(d)


@pytest.mark.parametrize("cap", sorted(kinds.AGENT_ONLY_CAPABILITIES))
def test_a_terminal_plugin_cannot_claim_agent_capabilities(cap):
    d = doc("shell")
    d["capabilities"][cap] = True
    if cap == "owns_transcript":
        d["transcript"] = {"kind": "claude-jsonl"}
    with pytest.raises(ManifestError, match="terminal plugin"):
        parse(d)


def test_binary_name_must_be_the_id_or_a_declared_alias():
    d = doc()
    d["binary"]["name"] = "other"
    with pytest.raises(ManifestError, match="binary.name"):
        parse(d)


# --- 2. argv parity with today's provider classes -------------------------------------------------

# The per-engine argv parity against the provider classes lived here in P1. Since P2 the classes
# no longer carry argv at all, so the contract is the GOLDEN table in test_engine_equivalence.py,
# recorded from the classes immediately before the data moved.


@pytest.fixture
def fake_bin(tmp_path):
    return exe(tmp_path / "bin" / "agent")


def test_adopt_mint_takes_only_the_placeholder(fake_bin, tmp_path):
    m = parse(doc("codex"))
    p = PluginProvider(
        m, trust=provenance.FIRST_PARTY, root=tmp_path, env={m.binary.env_var: str(fake_bin)}
    )
    with pytest.raises(EngineError):
        p.new_launch_argv(UUID, cwd="/tmp", bypass=False)
    with pytest.raises(EngineError):
        p.launch_argv("--dangerously-bypass-approvals-and-sandbox", cwd="/tmp", bypass=False)


def test_cwd_must_be_absolute(fake_bin, tmp_path):
    m = parse(doc("opencode"))
    p = PluginProvider(
        m, trust=provenance.FIRST_PARTY, root=tmp_path, env={m.binary.env_var: str(fake_bin)}
    )
    with pytest.raises(EngineError, match="absolute"):
        p.launch_argv("ses_x", cwd="--help", bypass=False)


# --- 3. §2b — the negative matrix -----------------------------------------------------------------


def local(m_doc: dict, tmp_path: Path, *, env=None, name="myagent") -> PluginProvider:
    d = copy.deepcopy(m_doc)
    d["identity"]["id"] = name
    d["binary"]["name"] = name
    d["binary"]["aliases"] = []
    d["binary"]["env_var"] = f"AGENT_SESSIONS_{name.upper().replace('-', '')}_BIN"
    m = parse(d)
    provenance.check_vocabulary(m, provenance.LOCAL)
    return PluginProvider(
        m,
        trust=provenance.LOCAL,
        root=tmp_path / "plugins" / name,
        env=env or {},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )


def record_path(p: PluginProvider) -> Path:
    return p.state_dir / f"{p.engine_id}.json"


def write_record(p: PluginProvider, **rec) -> None:
    """An install / confirmation record where BattleLab keeps them — app state, never the plugin
    tree — bound to this provider's manifest digest unless the test says otherwise."""
    rec.setdefault("manifest_sha256", p.manifest.digest)
    p.state_dir.mkdir(parents=True, exist_ok=True)
    record_path(p).write_text(json.dumps(rec))


@pytest.mark.parametrize("name", ["bash", "sudo", "python3", "env", "node20", "perl5", "python3"])
def test_matrix_forbidden_entrypoint_names(name, tmp_path):
    with pytest.raises(provenance.ProvenanceError, match="shell, interpreter or privilege"):
        local(doc(), tmp_path, name=name)


def test_matrix_first_party_shell_is_exempt_by_provenance_not_name(tmp_path):
    m = parse(doc("shell"))
    provenance.check_vocabulary(m, provenance.FIRST_PARTY)  # no raise
    with pytest.raises(provenance.ProvenanceError):
        provenance.check_vocabulary(m, provenance.LOCAL)


def test_matrix_alias_resolving_to_a_shell_is_refused(tmp_path):
    shell = shutil.which("bash")
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    p = local(d, tmp_path)
    (tmp_path / "bin").mkdir()
    os.symlink(shell, tmp_path / "bin" / "myagent")
    with pytest.raises(EngineError, match="shell, interpreter or privilege"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_matrix_no_path_lookup_so_path_cannot_shadow(tmp_path, monkeypatch):
    shadow = exe(tmp_path / "shadow" / "gemini")
    monkeypatch.setenv("PATH", f"{shadow.parent}:{os.environ.get('PATH', '')}")
    m = parse(doc())
    p = PluginProvider(
        m,
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "plugins" / "gemini",
        env={},
        home=tmp_path / "home",
    )
    with pytest.raises(EngineError, match="no binary found"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_matrix_search_path_into_a_world_writable_dir(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/drop"]
    target = exe(tmp_path / "drop" / "gemini")
    (tmp_path / "drop").chmod(0o777)
    try:
        m = parse(d)
        p = PluginProvider(
            m,
            trust=provenance.FIRST_PARTY,
            root=tmp_path / "plugins" / "gemini",
            env={},
            home=tmp_path,
        )
        with pytest.raises(EngineError, match="writable by other users"):
            p.launch_argv(UUID, cwd="/tmp", bypass=False)
    finally:
        (tmp_path / "drop").chmod(0o755)
    assert target.exists()


def test_matrix_entrypoint_parent_writable_by_another_user(tmp_path, monkeypatch):
    # A group-writable ancestor whose group is SHARED (other members) — simulated, since a test
    # cannot chgrp to a group with other users. The private-group case is the next test.
    monkeypatch.setattr(provenance, "_group_is_private", lambda gid: False)
    d = doc()
    d["binary"]["search_paths"] = ["~/shared/bin"]
    exe(tmp_path / "shared" / "bin" / "gemini")
    (tmp_path / "shared").chmod(0o775)  # group-writable ancestor
    try:
        p = PluginProvider(
            parse(d),
            trust=provenance.FIRST_PARTY,
            root=tmp_path / "p",
            env={},
            home=tmp_path,
            state_dir=tmp_path / "state",
        )
        with pytest.raises(EngineError, match="writable by other users"):
            p.launch_argv(UUID, cwd="/tmp", bypass=False)
    finally:
        (tmp_path / "shared").chmod(0o755)


def test_matrix_writable_entrypoint_file(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    exe(tmp_path / "bin" / "gemini").chmod(0o777)
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(EngineError, match="writable by other users"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_matrix_env_override_outside_the_root_flips_managed_to_adopted(tmp_path):
    d = doc()
    d["install"] = _install(entrypoint="bin/myagent")
    root = tmp_path / "plugins" / "myagent"
    installed = exe(root / "bin" / "myagent", b"#!/bin/true\n#managed\n")
    elsewhere = exe(tmp_path / "elsewhere" / "myagent")
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    m = parse(d)
    rec = dict(install_entrypoint="bin/myagent", install_sha256=sha(installed))
    write_record(
        PluginProvider(m, trust=provenance.LOCAL, root=root, state_dir=tmp_path / "state"), **rec
    )
    # Without the override: managed, launched from the plugin root.
    p = PluginProvider(
        m, trust=provenance.LOCAL, root=root, env={}, home=tmp_path, state_dir=tmp_path / "state"
    )
    assert p.entrypoint().state == provenance.MANAGED
    # With it: adopted — and, for a plugin-supplied manifest, refused until confirmed.
    p = PluginProvider(
        m,
        trust=provenance.LOCAL,
        root=root,
        env={m.binary.env_var: str(elsewhere)},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(EngineError, match="needs the operator's confirmation"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)
    # A first-party manifest is flipped too — recorded as adopted with the reason, never as managed.
    p = PluginProvider(
        m,
        trust=provenance.FIRST_PARTY,
        root=root,
        env={m.binary.env_var: str(elsewhere)},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    ep = p.entrypoint()
    assert ep.state == provenance.ADOPTED and "outside the plugin root" in ep.note


def test_matrix_symlink_retargeted_after_validation(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    real_a = exe(tmp_path / "versions" / "a" / "myagent", b"#!/bin/true\n#a\n")
    real_b = exe(tmp_path / "versions" / "b" / "myagent", b"#!/bin/true\n#b\n")
    (tmp_path / "bin").mkdir()
    link = tmp_path / "bin" / "myagent"
    os.symlink(real_a, link)
    p = local(d, tmp_path)
    write_record(p, confirmed_path=str(real_a), confirmed_sha256=sha(real_a))

    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(real_a)
    link.unlink()
    os.symlink(real_b, link)  # retarget after validation
    # argv[0] is the path that was validated, never the symlink — the retarget cannot swap bytes in.
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(real_a)
    # And replacing the validated file itself is caught by the stat/digest re-check.
    real_a.unlink()
    exe(real_a, b"#!/bin/true\n#swapped\n")
    with pytest.raises(EngineError, match="no longer matches|confirmation"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_managed_digest_is_rechecked_at_every_exec(tmp_path):
    d = doc()
    d["install"] = _install(entrypoint="bin/myagent")
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    root = tmp_path / "plugins" / "myagent"
    ep = exe(root / "bin" / "myagent", b"#!/bin/true\n#v1\n")
    p = PluginProvider(
        parse(d),
        trust=provenance.LOCAL,
        root=root,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    write_record(p, install_entrypoint="bin/myagent", install_sha256=sha(ep))
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(ep)
    ep.write_bytes(b"#!/bin/true\n#tampered\n")  # same inode, new bytes
    with pytest.raises(EngineError, match="no longer matches"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_managed_entrypoint_reached_through_a_symlink_is_refused(tmp_path):
    d = doc()
    d["install"] = _install(entrypoint="bin/myagent")
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    root = tmp_path / "plugins" / "myagent"
    outside = exe(tmp_path / "outside" / "myagent")
    (root / "bin").mkdir(parents=True)
    os.symlink(outside, root / "bin" / "myagent")
    p = PluginProvider(
        parse(d),
        trust=provenance.LOCAL,
        root=root,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    write_record(p, install_entrypoint="bin/myagent", install_sha256=sha(outside))
    with pytest.raises(EngineError, match="symlink"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_confirmed_adopted_binary_runs_until_it_changes(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    target = exe(tmp_path / "bin" / "myagent", b"#!/bin/true\n#v1\n")
    p = local(d, tmp_path)
    with pytest.raises(EngineError, match="confirmation"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)
    write_record(p, confirmed_path=str(target), confirmed_sha256=sha(target))
    assert p.entrypoint().state == provenance.ADOPTED
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)
    target.write_bytes(b"#!/bin/true\n#v2 - a vendor update needs confirming again\n")
    with pytest.raises(EngineError):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_first_party_adopted_binary_follows_a_vendor_update(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    v1 = exe(tmp_path / "versions" / "1" / "gemini")
    v2 = exe(tmp_path / "versions" / "2" / "gemini")
    (tmp_path / "bin").mkdir()
    os.symlink(v1, tmp_path / "bin" / "gemini")
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(v1)
    # v1 stays on disk, as vendor installers keep old versions: the provider must still follow
    # the retargeted link, not keep launching the stale cached path.
    (tmp_path / "bin" / "gemini").unlink()
    os.symlink(v2, tmp_path / "bin" / "gemini")
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(v2)


def test_a_record_someone_else_could_write_is_refused(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    exe(tmp_path / "bin" / "myagent")
    p = local(d, tmp_path)
    write_record(p, confirmed_path="x", confirmed_sha256="y")
    record_path(p).chmod(0o666)
    with pytest.raises(EngineError, match="only the operator can write"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


# --- 4. the loader --------------------------------------------------------------------------------


def _write(dirpath: Path, d: dict) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "plugin.json").write_text(json.dumps(d))


def test_loader_is_fail_soft_per_plugin(tmp_path):
    fp = tmp_path / "first_party"
    loc = tmp_path / "local"
    for e in ("claude", "gemini"):
        shutil.copytree(FIXTURES / e, fp / e)
    bad = doc()
    bad["launch"]["resume"]["flag"] = "--exec"
    bad["identity"]["id"] = "broken"
    bad["binary"]["name"] = "broken"
    _write(loc / "broken", bad)
    mismatch = doc()
    mismatch["identity"]["id"] = "other"
    mismatch["binary"]["name"] = "other"
    _write(loc / "renamed", mismatch)
    _write(loc / "gemini", doc())  # shadows an in-tree plugin
    r = load_all(
        first_party_dir=fp, local_dir=loc, env={}, home=tmp_path, state_dir=tmp_path / "state"
    )
    assert sorted(r.providers) == ["claude", "gemini"]
    assert r.providers["gemini"].trust == provenance.FIRST_PARTY
    assert "launch.resume.flag" in r.problems["local:broken"]
    assert "does not match its directory" in r.problems["local:renamed"]
    assert "reserved for an in-tree engine" in r.problems["local:gemini"]


def test_loader_refuses_a_local_bare_id_claim_and_tamperable_manifests(tmp_path):
    loc = tmp_path / "local"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    d["session_id"]["legacy_bare_id"] = True
    _write(loc / "myagent", d)
    d2 = copy.deepcopy(d)
    d2["session_id"]["legacy_bare_id"] = False
    d2["identity"]["id"] = d2["binary"]["name"] = "open"
    d2["binary"]["env_var"] = "AGENT_SESSIONS_OPEN_BIN"
    _write(loc / "open", d2)
    (loc / "open" / "plugin.json").chmod(0o666)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert r.providers == {}
    assert "bare ids" in r.problems["local:myagent"]
    assert "writable only by the operator" in r.problems["local:open"]


def test_loader_refuses_a_forbidden_local_entrypoint(tmp_path):
    loc = tmp_path / "local"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "sudo"
    d["binary"]["env_var"] = "AGENT_SESSIONS_SUDO_BIN"
    _write(loc / "sudo", d)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert "sudo" not in r.providers and "privilege" in r.problems["local:sudo"]


def test_a_local_manifest_cannot_impersonate_an_in_tree_engine(tmp_path):
    # A local `shell` (the one engine exempt from the entrypoint vocabulary) must not inherit the
    # in-tree exemption, replace the in-tree manifest, or load at all.
    fp = tmp_path / "first_party"
    shutil.copytree(FIXTURES / "shell", fp / "shell")
    loc = tmp_path / "local"
    _write(loc / "shell", doc("shell"))
    r = load_all(
        first_party_dir=fp, local_dir=loc, env={}, home=tmp_path, state_dir=tmp_path / "state"
    )
    assert r.providers["shell"].trust == provenance.FIRST_PARTY
    assert r.providers["shell"].manifest.source.startswith("first-party:")
    assert "local:shell" in r.problems
    # With no in-tree shell at all, the local copy is refused on the vocabulary, not exempted.
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert "shell" not in r.providers
    assert "shell, interpreter or privilege" in r.problems["local:shell"]


def test_symlinked_local_sources_are_refused(tmp_path):
    loc = tmp_path / "local"
    elsewhere = tmp_path / "elsewhere"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    _write(elsewhere / "myagent", d)
    loc.mkdir()
    os.symlink(elsewhere / "myagent", loc / "myagent")  # symlinked plugin directory
    (loc / "other").mkdir()
    d2 = copy.deepcopy(d)
    d2["identity"]["id"] = d2["binary"]["name"] = "other"
    d2["binary"]["env_var"] = "AGENT_SESSIONS_OTHER_BIN"
    _write(elsewhere / "other", d2)
    os.symlink(elsewhere / "other" / "plugin.json", loc / "other" / "plugin.json")  # symlinked file
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert r.providers == {}
    assert "plugin directory" in r.problems["local:myagent"]
    assert "not a symlink" in r.problems["local:other"]


def test_trust_is_never_read_from_the_manifest(tmp_path):
    loc = tmp_path / "local"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    d["identity"]["publisher"] = "battlelab"
    d["identity"]["trust"] = "first-party"
    _write(loc / "myagent", d)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert "unknown field" in r.problems["local:myagent"]
    del d["identity"]["trust"]
    _write(loc / "myagent", d)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert r.providers["myagent"].trust == provenance.LOCAL


def test_a_script_entrypoint_is_the_file_that_is_attested(tmp_path):
    # The supported wrapper form: an `env`-shebang script (npm's bin shims look like this). Its
    # bytes are what is hashed; the interpreter `env` finds is outside the boundary (see the
    # provenance docstring), which is why argv[0] is the script and never the interpreter.
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    script = exe(tmp_path / "bin" / "myagent", b"#!/usr/bin/env node\nrequire('./cli.js')\n")
    p = local(d, tmp_path)
    write_record(p, confirmed_path=str(script), confirmed_sha256=sha(script))
    argv = p.launch_argv(UUID, cwd="/tmp", bypass=False)
    assert argv[0] == str(script)
    assert p.entrypoint().sha256 == sha(script)


def test_plugins_home_honours_the_app_home(tmp_path):
    from agent_sessions.plugins import plugins_home

    assert plugins_home({"AGENT_SESSIONS_HOME": str(tmp_path)}) == tmp_path / "plugins"
    assert plugins_home({"AGENT_SESSIONS_PLUGINS_DIR": str(tmp_path / "x")}) == tmp_path / "x"


def test_archive_is_a_sidecar_toggle(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agent_sessions.plugins.provider._metadata.patch", lambda k, **kw: calls.append((k, kw))
    )
    p = PluginProvider(parse(doc()), trust=provenance.FIRST_PARTY, root=tmp_path)
    p.archive(UUID)
    p.unarchive(UUID)
    assert calls == [
        (f"gemini:{UUID}", {"archived": True}),
        (f"gemini:{UUID}", {"archived": False}),
    ]
    with pytest.raises(EngineError):
        p.archive("../../etc/passwd")


def test_no_shell_layer_in_the_plugin_package():
    src = Path(manifest_mod.__file__).parent
    for f in src.glob("*.py"):
        text = f.read_text()
        # P5 admits the fixed SSH transport and the contained ephemeral vendor PTY.
        # Schema/provider/storage code
        # still spawns nothing; its argv refusal and limits are exercised in test_plugin_feed.
        if f.name not in ("runner.py", "process.py"):
            assert "subprocess" not in text, f
        assert "shell=" not in text, f
        assert "os.system" not in text, f


def test_a_private_group_writable_dir_is_the_operators_own(tmp_path, monkeypatch):
    # umask 002 on a user-private-group host: ~/.local/bin is 775 and nobody else is in the group.
    monkeypatch.setattr(provenance, "_group_is_private", lambda gid: True)
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    target = exe(tmp_path / "bin" / "gemini")
    (tmp_path / "bin").chmod(0o775)
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)


def test_stat_mode_helpers_ignore_sticky_root_ancestors(tmp_path):
    # tmp_path lives under a root-owned sticky /tmp on most hosts; resolution must still work there.
    target = exe(tmp_path / "bin" / "gemini")
    st = os.stat(tmp_path.anchor)
    assert stat.S_ISDIR(st.st_mode)
    provenance._check_dirs(str(target))


# --- Hermes on PR #1112 ---------------------------------------------------------------------------


def _acl(*entries: tuple[int, int, int]) -> bytes:
    out = (2).to_bytes(4, "little")
    for tag, perm, qid in entries:
        out += tag.to_bytes(2, "little") + perm.to_bytes(2, "little") + qid.to_bytes(4, "little")
    return out


_ANY = 0xFFFFFFFF


def _set_acl(path: Path, *, named_user_perm: int, uid: int = 65534) -> None:
    """A REAL access ACL (the bytes setfacl writes): owner rwx, a named user, group r-x,
    mask rwx, other r-x. Skips where the filesystem has no ACL support."""
    blob = _acl(
        (0x01, 7, _ANY),
        (0x02, named_user_perm, uid),
        (0x04, 5, _ANY),
        (0x10, 7, _ANY),
        (0x20, 5, _ANY),
    )
    try:
        os.setxattr(path, "system.posix_acl_access", blob)
    except OSError as e:
        pytest.skip(f"no POSIX ACL support here: {e}")


def test_acl_named_user_writer_on_the_entrypoint_is_refused(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    target = exe(tmp_path / "bin" / "myagent")
    p = local(d, tmp_path)
    write_record(p, confirmed_path=str(target), confirmed_sha256=sha(target))
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)
    _set_acl(target, named_user_perm=7)
    p2 = local(d, tmp_path)
    with pytest.raises(EngineError, match="writable by other users"):
        p2.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_acl_named_user_writer_on_an_ancestor_is_refused(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/shared/bin"]
    exe(tmp_path / "shared" / "bin" / "gemini")
    _set_acl(tmp_path / "shared", named_user_perm=7)
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(EngineError, match="writable by other users"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_acl_named_user_reader_only_is_fine(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    target = exe(tmp_path / "bin" / "gemini")
    _set_acl(target, named_user_perm=5)
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)


def test_acl_writer_on_a_manifest_or_record_is_refused(tmp_path):
    loc = tmp_path / "local"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    _write(loc / "myagent", d)
    _set_acl(loc / "myagent" / "plugin.json", named_user_perm=6)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert "writable only by the operator" in r.problems["local:myagent"]

    d2 = doc()
    d2["binary"]["search_paths"] = ["~/bin"]
    exe(tmp_path / "bin" / "myagent")
    p = local(d2, tmp_path)
    write_record(p, confirmed_path="x", confirmed_sha256="y")
    _set_acl(record_path(p), named_user_perm=6)
    with pytest.raises(EngineError, match="only the operator can write"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


def test_malformed_acl_fails_closed():
    st = os.stat("/")
    assert provenance._acl_grants_others_write(b"\x00", st)
    assert provenance._acl_grants_others_write(_acl((0x01, 7, _ANY))[:-1], st)


def test_a_private_group_that_gains_a_member_stops_being_trusted(tmp_path, monkeypatch):
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid())
    members: list[str] = []

    class G:
        @property
        def gr_mem(self):
            return list(members)

    monkeypatch.setattr(grp, "getgrgid", lambda gid: G())
    monkeypatch.setattr(pwd, "getpwall", lambda: [me])
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    target = exe(tmp_path / "bin" / "gemini")
    (tmp_path / "bin").chmod(0o775)
    os.chown(tmp_path / "bin", -1, os.getgid())
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)
    members.append("mallory")  # the group becomes shared between two consecutive checks
    with pytest.raises(EngineError, match="writable by other users"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)


@pytest.mark.parametrize(
    "pattern",
    [
        "^" + "[a]+" * 12 + "b$",  # the reported catastrophic-backtracking shape
        "^[a-z]+[a-z0-9]+$",  # overlapping variable runs
        "^[a-z]{1,40}[a-z]{1,40}$",  # bounded but still ambiguous
        "^[a-f]+a$",
        "^[z-a]+$",  # reversed range: used to raise re.error out of the loader
        "^[0-z]+$",  # cross-category range: silently admits ; [ ^ …
        "^[A-z]+$",
    ],
)
def test_id_grammar_refuses_ambiguous_and_malformed_repetition(pattern):
    with pytest.raises(ManifestError):
        manifest_mod.compile_id_pattern(pattern)


def test_accepted_patterns_match_in_linear_time():
    import time

    worst = [
        r"^[a-z]+[0-9]+[A-Z]+$",
        r"^ses_[A-Za-z0-9]+$",
        r"^[a-z]{1,40}-[0-9]{1,40}$",
    ]
    for p in worst:
        rx = manifest_mod.compile_id_pattern(p)
        for probe in ("a" * 127 + "!", "a" * 64 + "1" * 63 + "!", "ses_" + "A" * 123 + "-"):
            t = time.perf_counter()
            rx.fullmatch(probe)
            assert time.perf_counter() - t < 0.05, (p, probe)


def test_one_malformed_manifest_never_stops_the_roster(tmp_path):
    loc = tmp_path / "local"
    good = doc()
    good["identity"]["id"] = good["binary"]["name"] = "good"
    good["binary"]["env_var"] = "AGENT_SESSIONS_GOOD_BIN"
    _write(loc / "good", good)
    rev = copy.deepcopy(good)
    rev["identity"]["id"] = rev["binary"]["name"] = "rev"
    rev["binary"]["env_var"] = "AGENT_SESSIONS_REV_BIN"
    rev["session_id"]["pattern"] = "^[z-a]+$"
    _write(loc / "rev", rev)
    (loc / "deep").mkdir()
    (loc / "deep" / "plugin.json").write_text("[" * 20000 + "]" * 20000)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert list(r.providers) == ["good"]
    assert "session_id.pattern" in r.problems["local:rev"]
    assert "local:deep" in r.problems


def test_an_unanticipated_error_is_still_one_plugins_problem(tmp_path, monkeypatch):
    import agent_sessions.plugins as plugins_pkg

    loc = tmp_path / "local"
    for name in ("alpha", "beta"):
        d = doc()
        d["identity"]["id"] = d["binary"]["name"] = name
        d["binary"]["env_var"] = f"AGENT_SESSIONS_{name.upper()}_BIN"
        _write(loc / name, d)
    real = plugins_pkg._load_local_manifest

    def flaky(p, **kw):
        if p.parent.name == "alpha":
            raise TypeError("a bug the validator did not anticipate")
        return real(p, **kw)

    monkeypatch.setattr(plugins_pkg, "_load_local_manifest", flaky)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert list(r.providers) == ["beta"]
    assert r.problems["local:alpha"] == "could not be loaded (TypeError)"


@pytest.mark.parametrize(
    "pattern",
    [
        "^" + "[a]+[b]{0,1}" * 10 + "c$",  # nullable + form (Hermes review 5133)
        "^" + "[a]{1,20}[b]{0,1}" * 5 + "c$",  # nullable bounded form
        "^[a]{0,1}$",
    ],
)
def test_nullable_atoms_are_refused(pattern):
    with pytest.raises(ManifestError, match="at least one character"):
        manifest_mod.compile_id_pattern(pattern)


def test_every_accepted_pattern_from_a_fuzzed_grammar_matches_fast():
    """Random patterns over the grammar's own atoms; whatever it ACCEPTS must match adversarial
    128-character inputs quickly. A rule that admits an ambiguous shape turns this red."""
    import random
    import time

    rng = random.Random(853)
    atoms = ["[a]", "[b]", "[ab]", "[a-c]", "[0-9]", "x", "-"]
    quants = ["", "+", "{2}", "{1,3}", "{1,9}"]
    accepted = 0
    for _ in range(3000):
        body = "".join(rng.choice(atoms) + rng.choice(quants) for _ in range(rng.randint(1, 10)))
        try:
            rx = manifest_mod.compile_id_pattern(f"^{body}$")
        except ManifestError:
            continue
        accepted += 1
        for probe in ("a" * 127 + "!", "ab" * 63 + "!!", "a" * 64 + "b" * 63 + "c"):
            t = time.perf_counter()
            rx.fullmatch(probe)
            assert time.perf_counter() - t < 0.05, (body, probe)
    assert accepted > 100


def test_a_symlinked_ancestor_swapped_after_the_check_changes_nothing(tmp_path, monkeypatch):
    """Hermes review 5133: the plugins dir reached through a symlink in a shared directory,
    retargeted between validation and parsing. The manifest is now parsed from the descriptor
    that was checked, so the swap cannot inject anything."""
    import agent_sessions.plugins as plugins_pkg

    real_dir = tmp_path / "real"
    evil_dir = tmp_path / "evil"
    for base, label in ((real_dir, "Original"), (evil_dir, "INJECTED")):
        d = doc()
        d["identity"]["id"] = d["binary"]["name"] = "myagent"
        d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
        d["identity"]["label"] = label
        _write(base / "myagent", d)
    (evil_dir / "myagent" / "plugin.json").chmod(0o666)
    link = tmp_path / "plugins"
    os.symlink(real_dir, link)

    orig = provenance.open_verified

    def swap_after_check(path, **kw):
        out = orig(path, **kw)
        link.unlink()
        os.symlink(evil_dir, link)  # the retarget lands after the check, before any parse
        return out

    monkeypatch.setattr(plugins_pkg.provenance, "open_verified", swap_after_check)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=link,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert r.providers["myagent"].manifest.identity.label == "Original"


def test_a_component_swapped_to_a_symlink_mid_walk_is_refused(tmp_path, monkeypatch):
    target = exe(tmp_path / "a" / "b" / "gemini")
    elsewhere = exe(tmp_path / "x" / "b" / "gemini")
    canonical = str(target)
    monkeypatch.setattr(provenance.os.path, "realpath", lambda p: canonical)
    import shutil as _sh

    _sh.rmtree(tmp_path / "a")
    os.symlink(tmp_path / "x", tmp_path / "a")  # "a" is now a symlink the walk must not follow
    with pytest.raises(provenance.ProvenanceError, match="cannot open"):
        provenance.open_verified(canonical)
    assert elsewhere.exists()


def test_a_fifo_is_refused_without_hanging(tmp_path):
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    (tmp_path / "bin").mkdir()
    os.mkfifo(tmp_path / "bin" / "gemini", 0o755)
    p = PluginProvider(
        parse(d),
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(EngineError, match="not a regular file"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)
    state = tmp_path / "state"
    state.mkdir()
    os.mkfifo(state / "x.json", 0o600)
    with pytest.raises(provenance.ProvenanceError, match="not a regular file"):
        from agent_sessions.plugins import read_record

        read_record(state, "x")


def test_a_local_plugin_root_is_the_directory_it_was_loaded_from(tmp_path):
    loc = tmp_path / "elsewhere"
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    _write(loc / "myagent", d)
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert r.providers["myagent"].root == loc / "myagent"


def test_a_swap_between_canonicalisation_and_the_walk_is_refused(tmp_path, monkeypatch):
    """Hermes review 5136: `slot` is replaced by a symlink to a trusted directory AFTER the path
    was canonicalised. Walking a second resolution would check only the trusted target and hand
    back the shared path; walking the exact canonical path visits the shared, world-writable
    ancestor (and finds the new symlink) and refuses."""
    shared = tmp_path / "shared"
    exe(shared / "slot" / "gemini", b"#!/bin/true\n#shared\n")
    trusted = tmp_path / "trusted"
    exe(trusted / "gemini", b"#!/bin/true\n#trusted\n")
    shared.chmod(0o777)
    cand = str(shared / "slot" / "gemini")
    real_realpath = os.path.realpath
    swapped = []

    def realpath_then_swap(p, *a, **kw):
        out = real_realpath(p, *a, **kw)
        if p == cand and not swapped:
            shutil.rmtree(shared / "slot")
            os.symlink(trusted, shared / "slot")
            swapped.append(True)
        return out

    monkeypatch.setattr(provenance.os.path, "realpath", realpath_then_swap)
    m = parse(doc())
    p = PluginProvider(
        m,
        trust=provenance.FIRST_PARTY,
        root=tmp_path / "p",
        env={m.binary.env_var: cand},
        home=tmp_path,
    )
    try:
        with pytest.raises(EngineError, match="writable by other users|cannot open"):
            p.launch_argv(UUID, cwd="/tmp", bypass=False)
        assert swapped
    finally:
        shared.chmod(0o755)


def test_a_cached_managed_entrypoint_that_becomes_a_symlink_or_loses_its_parent_is_refused(
    tmp_path,
):
    d = doc()
    d["install"] = _install(entrypoint="bin/myagent")
    d["identity"]["id"] = d["binary"]["name"] = "myagent"
    d["binary"]["env_var"] = "AGENT_SESSIONS_MYAGENT_BIN"
    root = tmp_path / "plugins" / "myagent"
    body = b"#!/bin/true\n#managed\n"
    ep = exe(root / "bin" / "myagent", body)
    p = PluginProvider(
        parse(d),
        trust=provenance.LOCAL,
        root=root,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    write_record(p, install_entrypoint="bin/myagent", install_sha256=sha(ep))
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(ep)

    # Same bytes, now reached through a symlink to a file outside the root.
    outside = exe(tmp_path / "outside" / "myagent", body)
    ep.unlink()
    os.symlink(outside, ep)
    with pytest.raises(EngineError):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)

    # Put the real file back, then make its directory world-writable.
    ep.unlink()
    exe(ep, body)
    p2 = PluginProvider(
        parse(d),
        trust=provenance.LOCAL,
        root=root,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    assert p2.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(ep)
    (root / "bin").chmod(0o777)
    try:
        with pytest.raises(EngineError, match="writable by other users"):
            p2.launch_argv(UUID, cwd="/tmp", bypass=False)
    finally:
        (root / "bin").chmod(0o755)


def test_an_executable_path_is_never_resolved_a_second_time(tmp_path):
    target = exe(tmp_path / "real" / "gemini")
    os.symlink(tmp_path / "real", tmp_path / "link")
    with pytest.raises(provenance.ProvenanceError, match="cannot open"):
        provenance.open_verified(str(tmp_path / "link" / "gemini"), canonicalize=False)
    fd, _ = provenance.open_verified(str(target), canonicalize=False)
    os.close(fd)
    with pytest.raises(provenance.ProvenanceError, match="normalised"):
        provenance.open_verified(
            str(tmp_path / "real" / ".." / "real" / "gemini"), canonicalize=False
        )


# --- independent review of PR #1112 ---------------------------------------------------------------


def _local_doc(name: str = "myagent") -> dict:
    d = doc()
    d["identity"]["id"] = d["binary"]["name"] = name
    d["binary"]["env_var"] = f"AGENT_SESSIONS_{name.upper()}_BIN"
    d["binary"]["search_paths"] = ["~/bin"]
    return d


def test_a_bundle_cannot_confirm_itself(tmp_path):
    """A record shipped INSIDE the plugin directory is not a record: records live in app state."""
    loc = tmp_path / "local"
    target = exe(tmp_path / "bin" / "myagent")
    _write(loc / "myagent", _local_doc())
    (loc / "myagent" / "record.json").write_text(
        json.dumps({"confirmed_path": str(target), "confirmed_sha256": sha(target)})
    )
    r = load_all(
        first_party_dir=tmp_path / "none",
        local_dir=loc,
        env={},
        home=tmp_path,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(EngineError, match="confirmation"):
        r.providers["myagent"].launch_argv(UUID, cwd="/tmp", bypass=False)


def test_a_state_dir_inside_the_plugins_dir_is_never_trusted(tmp_path):
    loc = tmp_path / "local"
    target = exe(tmp_path / "bin" / "myagent")
    _write(loc / "myagent", _local_doc())
    r0 = load_all(first_party_dir=tmp_path / "none", local_dir=loc, env={}, home=tmp_path)
    digest = r0.providers["myagent"].manifest.digest
    state = loc / "state"  # a bundle could ship this directory
    state.mkdir()
    (state / "myagent.json").write_text(
        json.dumps(
            {
                "confirmed_path": str(target),
                "confirmed_sha256": sha(target),
                "manifest_sha256": digest,
            }
        )
    )
    r = load_all(
        first_party_dir=tmp_path / "none", local_dir=loc, env={}, home=tmp_path, state_dir=state
    )
    assert r.providers["myagent"].state_dir is None
    with pytest.raises(EngineError, match="confirmation"):
        r.providers["myagent"].launch_argv(UUID, cwd="/tmp", bypass=False)


def test_a_record_for_a_different_manifest_confirms_nothing(tmp_path):
    target = exe(tmp_path / "bin" / "myagent")
    p = local(_local_doc(), tmp_path)
    write_record(
        p, confirmed_path=str(target), confirmed_sha256=sha(target), manifest_sha256="0" * 64
    )
    with pytest.raises(EngineError, match="different manifest"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)
    write_record(p, confirmed_path=str(target), confirmed_sha256=sha(target))
    assert p.launch_argv(UUID, cwd="/tmp", bypass=False)[0] == str(target)


@pytest.mark.parametrize("in_tree", ["absent", "broken"])
def test_a_local_plugin_can_never_take_an_in_tree_id(tmp_path, in_tree):
    fp = tmp_path / "first_party"
    if in_tree == "broken":
        (fp / "claude").mkdir(parents=True)
        (fp / "claude" / "plugin.json").write_text("{}")
    loc = tmp_path / "local"
    _write(loc / "claude", _local_doc("claude"))
    r = load_all(first_party_dir=fp, local_dir=loc, env={}, home=tmp_path)
    assert "claude" not in r.providers
    assert "reserved" in r.problems["local:claude"]


def test_an_unreadable_plugins_dir_is_a_problem_not_a_crash(tmp_path):
    loc = tmp_path / "local"
    loc.mkdir()
    loc.chmod(0o000)
    try:
        r = load_all(first_party_dir=tmp_path / "none", local_dir=loc, env={}, home=tmp_path)
    finally:
        loc.chmod(0o755)
    if os.geteuid() == 0:
        pytest.skip("root reads a 000 directory")
    assert r.providers == {} and any("cannot read" in v for v in r.problems.values())


def test_an_unresolvable_plugins_home_is_a_problem_not_a_crash(tmp_path):
    r = load_all(
        first_party_dir=tmp_path / "none",
        env={"AGENT_SESSIONS_PLUGINS_DIR": "~no-such-user-853/plugins"},
        home=tmp_path,
    )
    assert "plugins" in r.problems


def test_a_missing_passwd_entry_fails_closed_not_with_keyerror(tmp_path, monkeypatch):
    import pwd

    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    d = doc()
    d["binary"]["search_paths"] = ["~/bin"]
    exe(tmp_path / "bin" / "gemini")
    (tmp_path / "bin").chmod(0o775)
    p = PluginProvider(
        parse(d), trust=provenance.FIRST_PARTY, root=tmp_path / "p", env={}, home=tmp_path
    )
    with pytest.raises(EngineError, match="writable by other users"):
        p.launch_argv(UUID, cwd="/tmp", bypass=False)
    assert p.is_present() is False


def test_the_compiled_id_pattern_refuses_a_trailing_newline_with_match_too():
    m = parse(doc())
    assert m.session_id.pattern.match(UUID)
    assert not m.session_id.pattern.match(UUID + "\n")


@pytest.mark.parametrize(
    "name", ["tclsh8.6", "wish8.6", "ksh93", "zsh5", "bash5", "busybox.static"]
)
def test_versioned_interpreter_names_are_forbidden(name):
    assert kinds.is_forbidden_entrypoint(name)


@pytest.mark.parametrize("name", ["gemini", "claude", "codex", "shellcheck-free", "geminicli"])
def test_ordinary_agent_names_are_not_caught_by_the_suffix_rule(name):
    assert not kinds.is_forbidden_entrypoint(name)


# --- #853 P3: runtime, per-path env overrides, npm-global discovery -------------------------------


def test_runtime_absent_means_pty_for_every_first_party_manifest():
    for e in SEVEN:
        assert parse(doc(e)).runtime == "pty", e


def test_runtime_pty_can_be_stated_explicitly():
    d = doc()
    d["runtime"] = {"kind": "pty"}
    assert parse(d).runtime == "pty"


@pytest.mark.parametrize("kind", ["api", "http", "", "PTY", "Chat", 1, None])
def test_a_runtime_this_build_cannot_run_is_refused_not_half_run(kind):
    """A runtime outside the closed set is refused with a reason — never loaded as if it were a
    terminal engine. (`chat` joined the set in #1209; the rest of the vocabulary stays closed.)"""
    d = doc()
    d["runtime"] = {"kind": kind}
    with pytest.raises(ManifestError, match="runtime.kind"):
        parse(d)


def test_an_unknown_runtime_field_is_refused():
    d = doc()
    d["runtime"] = {"kind": "pty", "endpoint": "https://x"}
    with pytest.raises(ManifestError):
        parse(d)


def test_a_manifest_with_an_unrunnable_runtime_fails_soft_with_its_reason(tmp_path):
    """The loader surfaces the refusal for that plugin and keeps the others (the API/UI path)."""
    local = tmp_path / "plugins"
    (local / "zeta").mkdir(parents=True)
    d = doc()
    d["identity"]["id"] = "zeta"
    d["binary"].update(name="zeta", env_var="AGENT_SESSIONS_ZETA_BIN")
    d["runtime"] = {"kind": "api"}
    (local / "zeta" / "plugin.json").write_text(json.dumps(d))
    got = load_all(local_dir=local, state_dir=tmp_path / "state")
    assert "zeta" not in got.providers and "needs a newer BattleLab" in str(got.problems)
    assert "claude" in got.providers


def test_path_env_must_name_a_declared_path_and_a_valid_variable():
    d = doc("opencode")
    d["store"]["path_env"] = {"secrets": "AGENT_SESSIONS_X"}
    with pytest.raises(ManifestError, match="store.path_env.secrets"):
        parse(d)
    d = doc("opencode")
    d["store"]["path_env"] = {"db": "LD_PRELOAD"}
    with pytest.raises(ManifestError):
        parse(d)


def test_search_npm_global_is_a_boolean():
    d = doc("codex")
    assert parse(d).binary.search_npm_global is True
    assert parse(doc("claude")).binary.search_npm_global is False
    d["binary"]["search_npm_global"] = "yes"
    with pytest.raises(ManifestError):
        parse(d)


# --- #853 P9a (#1209): the `chat` runtime -------------------------------------------------------


def chat_doc(engine_id: str = "apichat") -> dict:
    """A minimal, valid `chat` manifest: no binary, no launch, an endpoint wire format only."""
    return {
        "contract": 1,
        "identity": {"id": engine_id, "label": "API agent", "publisher": "t", "version": "1"},
        "runtime": {"kind": "chat"},
        "endpoint": {"kind": "openai-chat"},
        "session_id": {
            "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            "mint": "pinned",
        },
        "store": {
            "root": "~/.local/share/agent-sessions/chat",
            "layout": "battlelab-chat",
            "read_only": False,
        },
        "capabilities": {"resume": True, "new": True, "archive": True},
        "transcript": {"kind": "battlelab-chat"},
        "usage": {"source": "tokens", "kind": "chat-response-tokens"},
        "display": {"name": "API agent", "badge": "api", "accent": "blue"},
    }


def test_a_chat_manifest_parses_with_no_binary_and_no_launch():
    m = parse(chat_doc())
    assert m.runtime == "chat"
    assert m.binary is None and m.launch is None
    assert m.endpoint is not None and m.endpoint.kind == "openai-chat"
    assert m.store is not None and m.store.layout == "battlelab-chat"
    assert m.transcript_kind == "battlelab-chat"
    assert (m.usage.source, m.usage.kind) == ("tokens", "chat-response-tokens")


@pytest.mark.parametrize("block", kinds.PTY_ONLY_BLOCKS)
def test_a_chat_manifest_refuses_every_block_that_describes_a_process(block):
    d = chat_doc()
    d[block] = doc()[block] if block in doc() else {"kind": "none"}
    if block == "verify":
        d[block] = ["binary"]
    with pytest.raises(ManifestError, match=f"^{block}: is forbidden for runtime 'chat'"):
        parse(d)


def test_a_chat_manifest_needs_an_endpoint_of_a_known_wire_format():
    d = chat_doc()
    del d["endpoint"]
    with pytest.raises(ManifestError, match="endpoint"):
        parse(d)
    d = chat_doc()
    d["endpoint"] = {"kind": "anthropic-messages"}
    with pytest.raises(ManifestError, match="endpoint.kind"):
        parse(d)


@pytest.mark.parametrize("field", ["url", "base_url", "authority", "api_key", "model"])
def test_a_manifest_can_never_name_the_endpoint_authority_or_key(field):
    """The URL, key and model are the operator's configuration. A manifest naming any of them is
    refused by the unknown-field policy — not an allowlist the manifest could populate."""
    d = chat_doc()
    d["endpoint"][field] = "https://attacker.example/v1"
    with pytest.raises(ManifestError, match=f"endpoint.{field}"):
        parse(d)


def test_an_endpoint_block_on_a_pty_manifest_is_refused():
    d = doc()
    d["endpoint"] = {"kind": "openai-chat"}
    with pytest.raises(ManifestError, match="only allowed for runtime 'chat'"):
        parse(d)


@pytest.mark.parametrize("cap", sorted(kinds.PTY_ONLY_CAPABILITIES))
def test_a_chat_manifest_refuses_capabilities_that_presume_a_terminal(cap):
    d = chat_doc()
    d["capabilities"][cap] = True
    with pytest.raises(ManifestError, match=f"capabilities.{cap}"):
        parse(d)


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda d: d.pop("store"), "store"),
        (lambda d: d["store"].update(layout="shell-records"), "store.layout"),
        (lambda d: d["store"].update(read_only=True), "store.read_only"),
        (lambda d: d["transcript"].update(kind="claude-jsonl"), "transcript.kind"),
    ],
)
def test_a_chat_manifest_must_keep_its_conversations_in_its_own_store(mutate, field):
    d = chat_doc()
    mutate(d)
    with pytest.raises(ManifestError, match=field):
        parse(d)


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda d: d["store"].update(layout="battlelab-chat"), "store.layout"),
        (lambda d: d.__setitem__("transcript", {"kind": "battlelab-chat"}), "transcript.kind"),
        (
            lambda d: d.__setitem__("usage", {"source": "tokens", "kind": "chat-response-tokens"}),
            "usage.kind",
        ),
    ],
)
def test_the_chat_kinds_are_refused_on_a_pty_manifest(mutate, field):
    d = doc()
    mutate(d)
    with pytest.raises(ManifestError, match=field):
        parse(d)


def test_pty_manifests_still_require_binary_and_launch():
    for block in ("binary", "launch"):
        d = doc()
        del d[block]
        with pytest.raises(ManifestError, match=block):
            parse(d)


def test_a_chat_manifest_loads_and_refuses_to_launch(tmp_path):
    """Through the real loader (local trust, the strictest path): no problem is recorded, and the
    provider refuses anything that would execute — with the reason, not an AttributeError."""
    local = tmp_path / "plugins"
    (local / "localchat").mkdir(parents=True)
    f = local / "localchat" / "plugin.json"
    f.write_text(json.dumps(chat_doc("localchat")))
    f.chmod(0o600)
    got = load_all(local_dir=local, state_dir=tmp_path / "state", home=tmp_path)
    assert not {k: v for k, v in got.problems.items() if "localchat" in k}, got.problems
    p = got.providers["localchat"]
    assert p.entrypoint() is None
    assert not (p.supports_orchestrator_input or p.expects_raw_tty or p.supports_seed_start)
    for call in (
        lambda: p.launch_argv(UUID, cwd="/tmp", bypass=False),
        lambda: p.new_launch_argv(UUID, cwd="/tmp", bypass=False),
    ):
        with pytest.raises(EngineError, match="runs no process"):
            call()
