"""Agent account homes (#1820): the overlay, its health check and the launch environment.

Everything here runs against temporary homes. The proof against the real CLIs is the gated
harness in ``test_agent_accounts_p0.py``.
"""

from __future__ import annotations

import ast
import copy
import os
import tomllib
from pathlib import Path

import pytest

from agent_sessions import agent_accounts
from agent_sessions.plugins import FIRST_PARTY_DIR, kinds, manifest

KINDS = sorted(kinds.ACCOUNT_KINDS)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """(recipe, account home, default home) for a small recipe, under a pinned accounts dir."""
    monkeypatch.setenv(agent_accounts.ROOT_ENV, str(tmp_path / "accounts"))
    rec = agent_accounts.Recipe(
        kind="test",
        home_env="AGENT_HOME",
        default_home="~/.agent",
        private=("login.json",),
        shared_dirs=("sessions", "skills"),
        shared_files=("settings.json",),
        clear_env=("AGENT_API_KEY",),
        login=("login",),
        status=("login", "status"),
    )
    shared = tmp_path / "default"
    shared.mkdir()
    (shared / "sessions").mkdir()
    return rec, agent_accounts.home("agent", "second"), shared


def _doc(engine: str) -> dict:
    return tomllib.loads((FIRST_PARTY_DIR / engine / "plugin.toml").read_text())


# --- the recipes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_every_recipe_is_well_formed(kind):
    rec = agent_accounts.recipe(kind)
    assert rec is not None
    assert rec.default_home.startswith("~/")
    assert rec.private and rec.login and rec.status
    names = [*rec.private, *rec.shared_dirs, *rec.shared_files]
    assert len(names) == len(set(names)), "an entry is private or shared, never both"
    for name in names:
        assert "/" not in name and name not in (".", ".."), name
    # A launch must not be able to lose its own home variable to the scrub list.
    assert rec.home_env not in rec.clear_env


def test_an_unknown_kind_has_no_recipe():
    assert agent_accounts.recipe("none") is None
    assert agent_accounts.recipe("made-up") is None


def test_no_first_party_agent_declares_accounts_before_its_proof():
    """#1820 P0: an engine's manifest names a kind only once the recipe is proven against the
    real CLI. Until a results table says so, every first-party manifest stays on "none"."""
    for path in sorted(FIRST_PARTY_DIR.glob("*/plugin.toml")):
        assert manifest.load_file(path).accounts_kind == "none", path.parent.name


# --- the manifest field --------------------------------------------------------------------------


def test_a_manifest_selects_a_kind_and_nothing_else():
    doc = _doc("claude")
    doc["accounts"] = {"kind": "claude-config-dir"}
    assert manifest.parse(copy.deepcopy(doc)).accounts_kind == "claude-config-dir"

    doc["accounts"] = {"kind": "claude-config-dir", "home_env": "LD_PRELOAD"}
    with pytest.raises(manifest.ManifestError, match="unknown field"):
        manifest.parse(copy.deepcopy(doc))

    doc["accounts"] = {"kind": "my-own-home"}
    with pytest.raises(manifest.ManifestError, match="accounts.kind"):
        manifest.parse(copy.deepcopy(doc))


@pytest.mark.parametrize("engine", ["claude-api", "shell"])
def test_only_a_terminal_agent_may_hold_logins(engine):
    doc = _doc(engine)
    doc["accounts"] = {"kind": "claude-config-dir"}
    with pytest.raises(manifest.ManifestError, match="accounts"):
        manifest.parse(doc)


# --- paths ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "..", "a/b", "A", "-a", "a" * 33, ".hidden", None, 3])
def test_an_id_is_one_safe_path_segment(bad, homes):
    with pytest.raises(agent_accounts.AccountError):
        agent_accounts.home("agent", bad)
    with pytest.raises(agent_accounts.AccountError):
        agent_accounts.home(bad, "second")


def test_the_default_home_follows_the_environment(homes, tmp_path, monkeypatch):
    rec, _, _ = homes
    monkeypatch.setenv("HOME", str(tmp_path))
    assert agent_accounts.default_home(rec, {}) == tmp_path / ".agent"
    assert agent_accounts.default_home(rec, {"AGENT_HOME": "/srv/agent"}) == Path("/srv/agent")


# --- build ---------------------------------------------------------------------------------------


def test_build_lays_the_account_over_the_default_home(homes):
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)

    assert os.stat(account).st_mode & 0o777 == 0o700
    for name in ("sessions", "skills", "settings.json"):
        assert os.readlink(account / name) == str(shared / name)
    # A directory the default home lacked is created there, empty: a CLI cannot mkdir through a
    # dangling link. A missing file stays missing, and a write through the link creates it.
    assert (shared / "skills").is_dir() and not list((shared / "skills").iterdir())
    assert not (shared / "settings.json").exists()
    assert not (account / "login.json").exists()
    assert agent_accounts.problems(rec, account, shared) == []

    # What a session writes through the account lands in the one shared store.
    (account / "sessions" / "s1.jsonl").write_text("{}")
    (account / "settings.json").write_text("{}")
    assert (shared / "sessions" / "s1.jsonl").is_file()
    assert (shared / "settings.json").is_file()


def test_build_is_idempotent_and_leaves_the_login_alone(homes):
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (account / "login.json").write_text("the vendor wrote this")
    before = os.stat(account / "login.json")
    agent_accounts.build(rec, account, shared)
    after = os.stat(account / "login.json")
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_build_refuses_a_second_copy_instead_of_replacing_it(homes):
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (account / "sessions").unlink()
    (account / "sessions").mkdir()
    (account / "sessions" / "only-here.jsonl").write_text("{}")
    with pytest.raises(agent_accounts.AccountError, match="sessions: a separate copy"):
        agent_accounts.build(rec, account, shared)
    assert (account / "sessions" / "only-here.jsonl").is_file()


def test_build_needs_a_default_home(homes, tmp_path):
    rec, account, _ = homes
    with pytest.raises(agent_accounts.AccountError, match="default home does not exist"):
        agent_accounts.build(rec, account, tmp_path / "never-ran")
    assert not account.exists()


def test_build_refuses_a_file_where_a_shared_directory_belongs(homes):
    rec, account, shared = homes
    (shared / "skills").write_text("not a directory")
    with pytest.raises(agent_accounts.AccountError, match="skills: not a directory"):
        agent_accounts.build(rec, account, shared)


def test_a_default_home_entry_may_itself_be_a_link(homes, tmp_path):
    rec, account, shared = homes
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (shared / "skills").symlink_to(elsewhere)
    agent_accounts.build(rec, account, shared)
    assert agent_accounts.problems(rec, account, shared) == []


# --- the health check ----------------------------------------------------------------------------


def test_a_replaced_shared_entry_refuses_the_account(homes):
    """A CLI that rewrites a file by rename leaves a regular file where the link was: from then
    on there are two copies, and the account must not launch."""
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (account / "settings.json").unlink()
    (account / "settings.json").write_text("{}")
    assert agent_accounts.problems(rec, account, shared) == [
        "settings.json: a separate copy exists in this account's home"
    ]


def test_a_relinked_or_missing_shared_entry_refuses_the_account(homes, tmp_path):
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (account / "sessions").unlink()
    (account / "sessions").symlink_to(tmp_path)
    (account / "skills").unlink()
    assert agent_accounts.problems(rec, account, shared) == [
        "sessions: the shared link points somewhere else",
        "skills: the shared link is missing",
    ]


def test_a_shared_directory_removed_from_the_default_home_refuses_the_account(homes):
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (shared / "skills").rmdir()
    assert agent_accounts.problems(rec, account, shared) == [
        "skills: the shared directory is missing from the default home"
    ]


def test_a_login_entry_that_became_a_link_refuses_the_account(homes):
    """A linked login would hand this account whichever login the link points at."""
    rec, account, shared = homes
    agent_accounts.build(rec, account, shared)
    (shared / "login.json").write_text("the default login")
    (account / "login.json").symlink_to(shared / "login.json")
    assert agent_accounts.problems(rec, account, shared) == [
        "login.json: the login entry is not a regular file"
    ]


def test_a_missing_or_exposed_home_refuses_the_account(homes):
    rec, account, shared = homes
    assert agent_accounts.problems(rec, account, shared) == ["the account's home is missing"]
    agent_accounts.build(rec, account, shared)
    os.chmod(account, 0o750)
    assert agent_accounts.problems(rec, account, shared) == [
        "the account's home is readable by other users"
    ]


# --- the launch environment ----------------------------------------------------------------------


def test_launch_env_sets_the_home_and_drops_outranking_credentials(homes):
    rec, account, _ = homes
    base = {"PATH": "/usr/bin", "AGENT_API_KEY": "sk-inherited", "AGENT_HOME": "/old", "X": "1"}
    env = agent_accounts.launch_env(rec, account, base)
    assert env == {"PATH": "/usr/bin", "AGENT_HOME": str(account), "X": "1"}
    assert base["AGENT_API_KEY"] == "sk-inherited", "the caller's environment is not edited"


# --- what the module may never do ----------------------------------------------------------------

#: Calls that read, copy, move or delete a file. An account home holds a login BattleLab must
#: never read (#1820 Decisions), and a home is never repaired by removing something.
_FORBIDDEN_CALLS = frozenset(
    {
        "open",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rename",
        "replace",
        "unlink",
        "remove",
        "rmdir",
        "rmtree",
        "listdir",
        "scandir",
        "iterdir",
        "glob",
        "rglob",
        "walk",
    }
)


def test_the_module_never_reads_copies_or_removes_anything():
    tree = ast.parse(Path(agent_accounts.__file__).read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            called.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
    assert called & _FORBIDDEN_CALLS == set()
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    imported = {alias.name for node in imports for alias in node.names}
    assert "shutil" not in imported and "subprocess" not in imported
