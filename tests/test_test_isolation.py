"""The suite's own guarantee: no test can reach the operator's real app stores.

A mid-test `monkeypatch.undo()` once reverted conftest's store pins, and the missions store of
the app running on the CI host was migrated by a branch's schema (#1097). `conftest.py` now has
two independent layers — a session-level env backstop and an audit-hook tripwire — and these
tests pin both, so neither can be removed without a red test.

#2423 added two more for the child processes a test starts — the session's `$HOME` is the sandbox,
and the tripwire also judges every process start's environment — plus `conftest.child_env()`.

The tripwire's own probes run against a DISPOSABLE protected root (#1118). Never the real store:
if the hook ever breaks, this file would otherwise be the thing that writes a stray file, an empty
DB and a directory into the operator's live store before failing.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys

import pytest

import conftest
from conftest import STORE_ENV_PATHS, child_env

_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
_REAL_STORE = os.path.join(_REAL_HOME, ".config", "agent-sessions")


def _isolation():
    return sys.modules["_agent_sessions_test_isolation"]


def _protected_root(tmp_path, monkeypatch):
    """A throwaway directory the tripwire treats as protected for this test.

    Patching `PROTECTED_DIRS` is the existing seam `_under_protected` reads at call time, so the
    probe never reaches the operator's store — even when the audit hook is broken and the probe's
    `open`/`sqlite3.connect`/`makedirs` really run.
    """
    root = tmp_path / "protected-store"
    root.mkdir()
    monkeypatch.setattr(conftest, "PROTECTED_DIRS", (str(root),))
    return root


def test_the_tripwire_refuses_a_protected_store_before_the_syscall(tmp_path, monkeypatch):
    """The hook is installed and refuses every mutating call before it reaches the disk."""
    root = _protected_root(tmp_path, monkeypatch)
    st = _isolation()
    before = len(st.violations)
    probe = str(root / "tripwire-probe-never-created.db")
    try:
        with pytest.raises(RuntimeError, match="test isolation breach"):
            open(probe, "w")  # noqa: SIM115 — the hook raises before a descriptor exists
        with pytest.raises(RuntimeError, match="test isolation breach"):
            sqlite3.connect(probe)
        with pytest.raises(RuntimeError, match="test isolation breach"):
            os.makedirs(str(root / "tripwire-probe-dir"))
        assert not os.path.exists(probe), "the hook must refuse before the file is created"
        assert len(st.violations) == before + 3, "every refusal is recorded for the reporter"
    finally:
        del st.violations[before:]  # this test's own, deliberate breaches


def test_every_audited_event_refuses_a_disposable_protected_path(tmp_path, monkeypatch):
    """Direct `_audit` coverage of the event map, without any syscall on a real store."""
    root = _protected_root(tmp_path, monkeypatch)
    st = _isolation()
    before = len(st.violations)
    allowed = str(tmp_path / "allowed")
    try:
        for event, indices in conftest._AUDITED.items():
            for index in indices:
                args = [allowed] * (max(indices) + 1)
                args[index] = str(root / "victim")
                with pytest.raises(RuntimeError, match="test isolation breach"):
                    conftest._audit(event, tuple(args))
        assert len(st.violations) == before + sum(
            len(indices) for indices in conftest._AUDITED.values()
        )
    finally:
        del st.violations[before:]


def test_the_real_store_is_still_a_protected_root():
    """The disposable seam must not have displaced the real store from the constants."""
    assert _REAL_STORE in conftest.PROTECTED_DIRS
    # Pure string matching: `_under_protected` returns the path without any syscall here.
    assert conftest._under_protected(os.path.join(_REAL_STORE, "prefs.json")) is not None


def test_an_ordinary_allowed_path_is_not_a_hit(tmp_path, monkeypatch):
    root = _protected_root(tmp_path, monkeypatch)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    assert conftest._under_protected(str(allowed / "x.db")) is None
    assert root.exists()  # the fixture is a real directory; the allowed path is beside it


def test_an_undo_falls_back_to_the_sandbox_never_the_real_home(monkeypatch):
    """What `monkeypatch.undo()` restores is the session pin, so the fallback is harmless."""
    from agent_sessions import discover, memory, metadata, missions, prefs

    monkeypatch.undo()  # the exact mistake this backstop exists for
    sandbox = _isolation().sandbox
    for path in (
        missions._db_path(),
        prefs._default_path(),
        metadata._default_path(),
        discover.default_env_path(),
        memory.root(),
    ):
        assert str(path).startswith(sandbox + os.sep), path
        assert not str(path).startswith(_REAL_HOME + os.sep + "."), path


# ---- child processes (#2423) ---------------------------------------------------------------------
#
# A test child that dropped every store pin while `$HOME` stayed real migrated the LIVE
# `automations.db` (2026-11-27): the tripwire above lives in the pytest process only. The probes
# below that need a protected home use a DISPOSABLE stand-in for it, like `_protected_root`.

_PIN = "AGENT_SESSIONS_AUTOMATIONS_DB"


def _operator_home(tmp_path, monkeypatch):
    """A throwaway stand-in for the operator's home: its app dirs protected, it the real home."""
    home = tmp_path / "operator-home"
    home.mkdir()
    roots = tuple(
        str(home / sub)
        for sub in (".config/agent-sessions", ".local/share/agent-sessions", ".agent-sessions")
    )
    monkeypatch.setattr(conftest, "PROTECTED_DIRS", roots)
    monkeypatch.setattr(conftest, "REAL_HOMES", frozenset({os.path.realpath(home)}))
    return home


def test_the_session_home_is_the_sandbox_and_the_started_home_stays_protected(monkeypatch):
    """`$HOME` is this process's sandbox root, so a store whose pin is gone defaults to exactly
    the path conftest pinned it at."""
    from agent_sessions import automations_store, missions, prefs

    sandbox = _isolation().sandbox
    assert os.environ["HOME"] == sandbox
    assert os.environ["XDG_CONFIG_HOME"] == os.path.join(sandbox, ".config")
    for var, default in (
        ("AGENT_SESSIONS_AUTOMATIONS_DB", automations_store.db_path),
        ("AGENT_SESSIONS_MISSIONS_DB", missions._db_path),
        ("AGENT_SESSIONS_PREFS", prefs._default_path),
    ):
        monkeypatch.delenv(var)
        assert str(default()) == os.path.join(sandbox, STORE_ENV_PATHS[var]), var
    started = os.environ["BATTLELAB_TEST_STARTED_HOME"]
    assert started != sandbox  # an xdist worker inherits the controller's sandbox HOME, not this
    assert os.path.join(started, ".config", "agent-sessions") in conftest.PROTECTED_DIRS
    assert os.path.realpath(_REAL_HOME) in conftest.REAL_HOMES


def test_spawn_breach_judges_the_destination_of_every_route(tmp_path, monkeypatch):
    """The matrix: each route into the (stand-in) operator's store is refused; safe ones pass."""
    real = _operator_home(tmp_path, monkeypatch)
    safe = tmp_path / "test-home"
    (tmp_path / "real-link").symlink_to(real)
    refused = {
        "no HOME": child_env(drop=("HOME",)),
        "empty HOME": child_env(HOME=""),
        "the real HOME": child_env(HOME=real),
        "the real HOME through a symlink": child_env(HOME=tmp_path / "real-link"),
        "a real XDG_CONFIG_HOME": child_env(XDG_CONFIG_HOME=real / ".config"),
        "a real XDG_DATA_HOME": child_env(XDG_DATA_HOME=real / ".local" / "share"),
        "a pin replaced by a real path": child_env(
            **{_PIN: real / ".config" / "agent-sessions" / "automations.db"}
        ),
        "bytes keys and values": {b"HOME": os.fsencode(real)},
    }
    for case, env in refused.items():
        assert conftest.spawn_breach(env) is not None, case
    allowed = {
        "the session env": child_env(),
        "a test's own HOME": child_env(HOME=safe),
        "a pin moved to a tmp path": child_env(**{_PIN: tmp_path / "mine.db"}),
        "every pin dropped under a tmp HOME": child_env(drop=STORE_ENV_PATHS, HOME=safe),
    }
    for case, env in allowed.items():
        assert conftest.spawn_breach(env) is None, case
    # A dropped pin is judged by where its default lands under that env's HOME.
    monkeypatch.setattr(
        conftest, "PROTECTED_DIRS", (*conftest.PROTECTED_DIRS, str(safe / ".config/agent-sessions"))
    )
    assert conftest.spawn_breach(child_env(drop=(_PIN,), HOME=safe)) is not None
    assert conftest.spawn_breach(child_env(HOME=safe)) is None  # pinned: the default is unused


def test_a_child_without_home_is_refused_only_when_it_can_run_the_app(tmp_path, monkeypatch):
    """No `$HOME` means the passwd home. That is refused for a program that can run the app's code;
    the app's own hardened launches of fixed system tools (`ssh-keygen` with only PATH and LANG)
    pass, though a store they are explicitly pointed at is still judged."""
    real = _operator_home(tmp_path, monkeypatch)
    bare = {"PATH": "/usr/bin:/bin", "LANG": "C"}
    beside = os.path.join(os.path.dirname(sys.executable), "pytest")
    runs_the_app = (sys.executable, "python3", "/usr/bin/python3.12", "/bin/sh", "bash", beside)
    for program in (*runs_the_app, None):
        assert conftest.spawn_breach(bare, program) is not None, program
    for program in ("/usr/bin/ssh-keygen", "/usr/bin/systemd-run", "/usr/bin/git", b"/bin/true"):
        assert conftest.spawn_breach(bare, program) is None, program
    pinned_real = {**bare, _PIN: str(real / ".config" / "agent-sessions" / "automations.db")}
    assert conftest.spawn_breach(pinned_real, "/usr/bin/ssh-keygen") is not None
    with pytest.raises(conftest.RealStoreTouched, match="has no HOME"):
        subprocess.run([sys.executable, "-c", "pass"], env=bare, check=False)
    st = _isolation()
    assert st.violations and "has no HOME" in st.violations[-1]
    del st.violations[-1:]  # this test's own, deliberate breach
    assert subprocess.run(["/bin/true"], env=bare, check=False).returncode == 0


def test_an_inherited_env_is_judged_as_it_is_at_the_spawn(tmp_path, monkeypatch):
    """`env=None` means `os.environ` as it is when the child starts, not as conftest left it."""
    real = _operator_home(tmp_path, monkeypatch)
    assert conftest.spawn_breach(None) is None
    with monkeypatch.context() as m:
        m.setenv(_PIN, str(real / ".config" / "agent-sessions" / "automations.db"))
        assert conftest.spawn_breach(None) is not None
    with monkeypatch.context() as m:
        m.setenv("HOME", str(real))
        assert conftest.spawn_breach(None) is not None
    with monkeypatch.context() as m:
        m.delenv("HOME")
        assert conftest.spawn_breach(None) is not None
    assert conftest.spawn_breach(None) is None


def test_real_process_starts_are_refused_before_the_child_runs(tmp_path, monkeypatch):
    """`subprocess.Popen`, `os.posix_spawn`, an inherited launch and `os.exec` all refuse, and
    each refusal is recorded, so one the app swallows still fails the test that made it."""
    real = _operator_home(tmp_path, monkeypatch)
    ran = tmp_path / "the-child-ran"
    argv = ["/bin/sh", "-c", f'touch "{ran}"']
    bad = child_env(HOME=real)
    st = _isolation()
    before = len(st.violations)
    try:
        with pytest.raises(conftest.RealStoreTouched, match="subprocess.Popen refused"):
            subprocess.run(argv, env=bad, check=False)
        with pytest.raises(conftest.RealStoreTouched, match="os.posix_spawn refused"):
            os.posix_spawn(argv[0], argv, bad)
        with monkeypatch.context() as m:
            m.setenv("HOME", str(real))
            with pytest.raises(conftest.RealStoreTouched):
                subprocess.run(argv, check=False)  # env=None: the child would inherit it
        assert len(st.violations) == before + 3, "every refusal is recorded for the reporter"
        pid = os.fork()
        if pid == 0:  # the exec* family audits in the process that execs
            try:
                os.execve(argv[0], argv, bad)
            except conftest.RealStoreTouched:
                os._exit(97)
            finally:
                os._exit(98)
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 97
        conftest._collect_forked()
        assert len(st.violations) == before + 4, "the forked child's refusal reaches this process"
        assert "os.exec" in st.violations[-1]
        assert not ran.exists(), "no refused child may have run"
        subprocess.run(argv, env=child_env(), check=True)  # and a sandboxed one still runs
        assert ran.exists()
    finally:
        conftest._collect_forked()
        del st.violations[before:]  # this test's own, deliberate breaches


def test_a_relative_route_is_judged_where_the_child_runs(tmp_path, monkeypatch):
    """`HOME=.` or a relative pin means the child's `cwd`, not pytest's: a child started inside
    the (stand-in) operator's home with either is refused, and the same env elsewhere runs."""
    real = _operator_home(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    relative_home = child_env(drop=STORE_ENV_PATHS, HOME=".")
    relative_pin = child_env(**{_PIN: ".config/agent-sessions/automations.db"})
    relative_xdg = child_env(XDG_CONFIG_HOME=".config")
    for env in (relative_home, relative_pin, relative_xdg):
        assert conftest.spawn_breach(env, "/bin/true", real) is not None
        assert conftest.spawn_breach(env, "/bin/true", elsewhere) is None
    with monkeypatch.context() as m:
        m.chdir(real)  # no `cwd`: the child runs where this process is
        assert conftest.spawn_breach(relative_home, "/bin/true") is not None
    st = _isolation()
    before = len(st.violations)
    try:
        for env in (relative_home, relative_pin):
            with pytest.raises(conftest.RealStoreTouched, match="subprocess.Popen refused"):
                subprocess.run([sys.executable, "-c", "pass"], env=env, cwd=real, check=False)
        assert len(st.violations) == before + 2
    finally:
        del st.violations[before:]  # this test's own, deliberate breaches
    for env in (relative_home, relative_pin):
        subprocess.run(["/bin/true"], env=env, cwd=elsewhere, check=True)


_SWALLOWED_EXEC = """
import os

def test_ignores_the_status():
    env = {**os.environ, "HOME": os.environ["BATTLELAB_TEST_STARTED_HOME"]}
    os.spawnve(os.P_WAIT, "/bin/true", ["/bin/true"], env)  # refused in the fork; status ignored
"""


def test_a_forked_refusal_fails_the_test_even_when_its_status_is_ignored(tmp_path):
    """`os.spawn*` refuses in the forked child, which only returns 127 to the caller. A test that
    ignores that status still fails: the fork's record reaches the owning test's reporter."""
    started = tmp_path / "operator-home"  # the nested run's stand-in operator home
    started.mkdir()
    work = tmp_path / "run"
    work.mkdir()
    shutil.copyfile(conftest.__file__, work / "conftest.py")
    (work / "test_swallowed.py").write_text(_SWALLOWED_EXEC)
    env = child_env(
        drop=("BATTLELAB_TEST_STARTED_HOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"), HOME=started
    )
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist"]
    result = subprocess.run(
        [*argv, str(work)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    # The reporter fails the item in its teardown, so pytest counts it as an error.
    assert "1 error" in out and "touched the operator's real store: os.exec" in out, out
    assert not (started / ".config").exists()


_CANARY = r"""
import json
from agent_sessions import automations_store, missions, prefs

automations_store._connect().close()  # opens AND migrates, through its default path
missions._connect().close()
prefs.read_block("canary")
print(json.dumps([str(automations_store.db_path()), str(missions._db_path()),
                  str(prefs._default_path())]))
"""


@pytest.mark.parametrize("launch", ["child_env", "hand-built", "through a shell"])
def test_a_child_that_lost_its_pins_resolves_the_sandbox_never_a_real_store(launch):
    """The canary: a child with NO store pins opens three stores through their defaults, and
    every path it resolved is inside the session sandbox, outside every protected root.

    Red (#2423) is shown with a disposable stand-in home (`HOME=<scratch> pytest …`) and the
    sandbox `$HOME` reverted: the tripwire refuses the launch, and with the tripwire reverted too
    the child resolves `<scratch>/.config/agent-sessions`, a protected root. Never the real home:
    the first assertion stops before any child starts when `$HOME` is the passwd entry."""
    home = os.environ.get("HOME", "")
    assert home and os.path.realpath(home) != os.path.realpath(
        _REAL_HOME
    ), "the session $HOME is not a sandbox: refusing to start a child that would open real stores"
    argv = [sys.executable, "-c", _CANARY]
    if launch == "hand-built":  # the shape of the old filters, minus the pins only
        env = {k: v for k, v in os.environ.items() if k not in STORE_ENV_PATHS}
    else:
        env = child_env(drop=STORE_ENV_PATHS)
    if launch == "through a shell":  # a grandchild: sh, then Python
        argv = ["/bin/sh", "-c", 'exec "$0" -c "$1"', sys.executable, _CANARY]
    store = os.path.join(home, ".config", "agent-sessions")
    existed = set(os.listdir(store)) if os.path.isdir(store) else set()
    out = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    paths = json.loads(out.stdout.strip().splitlines()[-1])
    sandbox = os.path.realpath(home)
    try:
        for path in paths:
            assert conftest._under_protected(path) is None, path
            assert os.path.realpath(path).startswith(sandbox + os.sep), path
        assert all(os.path.exists(p) for p in paths[:2]), "the child really opened the stores"
    finally:  # the session sandbox is shared by this worker's later tests: leave it as found
        for name in set(os.listdir(store)) - existed:
            os.remove(os.path.join(store, name))


_WORKER_PROBE = """
import json, os
import conftest

def test_probe():
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(os.environ["PROBE_OUT"], worker + ".json"), "w") as f:
        json.dump({"home": os.environ["HOME"],
                   "started": os.environ["BATTLELAB_TEST_STARTED_HOME"],
                   "protected": list(conftest.PROTECTED_DIRS),
                   "real_homes": sorted(conftest.REAL_HOMES)}, f)
"""


def test_every_xdist_worker_protects_the_started_home_not_the_controllers_sandbox(tmp_path):
    """A real two-worker run started with a stand-in operator home: each worker's `$HOME` is its
    own sandbox, yet it protects the STARTED home (and the passwd one), never a sandbox."""
    started = tmp_path / "operator-home"
    started.mkdir()
    work, out = tmp_path / "run", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    shutil.copyfile(conftest.__file__, work / "conftest.py")
    (work / "test_probe.py").write_text(_WORKER_PROBE)
    env = child_env(
        drop=("BATTLELAB_TEST_STARTED_HOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"),
        HOME=started,
        PROBE_OUT=out,
    )
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2"]
    result = subprocess.run(
        [*argv, "--dist", "each", str(work)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=170,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    reports = {p.stem: json.loads(p.read_text()) for p in out.glob("*.json")}
    assert set(reports) == {"gw0", "gw1"}
    for worker, r in reports.items():
        assert r["started"] == str(started), worker
        assert r["home"] != str(started) and not r["home"].startswith(str(started)), worker
        assert str(started / ".config" / "agent-sessions") in r["protected"], worker
        assert os.path.join(_REAL_HOME, ".config", "agent-sessions") in r["protected"], worker
        assert os.path.realpath(started) in r["real_homes"], worker
    assert reports["gw0"]["home"] != reports["gw1"]["home"]  # a sandbox per worker
