"""Shared pytest fixtures.

Tests don't touch the real Claude Code session history or the real sidecar —
every fixture sets up isolated paths.
"""

from __future__ import annotations

import os
import pwd
import re
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

# --- No test may reach the operator's real stores ------------------------------------------------
#
# Measured, not hypothetical: a mid-test `monkeypatch.undo()` also undid the autouse pins below, so
# `_db_path()` fell back to `Path.home()/.config/agent-sessions/missions.db` — the LIVE store of the
# app running on the CI host — and a schema-30 branch migrated it in place (#1097), breaking the
# running service. Two layers stop that class of leak, independently of whether the per-test pins
# survive:
#
# 1. **Backstop.** Every store path the app reads from the environment is pinned at process start
#    to a session sandbox by writing `os.environ` directly. A `monkeypatch` restores what it saw,
#    so an `undo()` (or a fixture that forgot one) lands in the sandbox, never `$HOME`. Done at
#    conftest IMPORT, before `agent_sessions` is imported, because some modules capture a path at
#    import (`scrollback._SCROLLBACK_DIR`); `pytest_configure` re-asserts it. Engine transcript
#    stores (`~/.claude`, `~/.codex`, …) are deliberately not pinned: tests point `$HOME` at a tmp
#    dir and rely on those following it.
# 2. **Tripwire.** An audit hook refuses any open / sqlite connect / mkdir / rename / unlink under
#    the REAL app store directories, resolved from the passwd entry (never `$HOME`, which tests
#    move). It raises at the call site AND is reported by an autouse fixture, so a leak fails the
#    test even when the app code swallows the exception as a fail-soft read.

# The real home, from the passwd database — `$HOME` is monkeypatched all over the suite.
_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir

# Store env var → sub-path under the sandbox (mirroring the production default layout). Values
# derived from `AGENT_SESSIONS_HOME` (the env file, `2fa.json`, plugins, update progress) are
# covered by pinning that one and are NOT pinned separately: tests that move the app home expect
# them to follow it.
STORE_ENV_PATHS: dict[str, str] = {
    "AGENT_SESSIONS_MISSIONS_DB": ".config/agent-sessions/missions.db",
    "AGENT_SESSIONS_PREFS": ".config/agent-sessions/prefs.json",
    "AGENT_SESSIONS_METADATA": ".config/agent-sessions/metadata.json",
    "AGENT_SESSIONS_PROJECTS": ".config/agent-sessions/projects.json",
    "AGENT_SESSIONS_NOTIFICATIONS": ".config/agent-sessions/notifications.json",
    "AGENT_SESSIONS_PUSH_SUBS": ".config/agent-sessions/push-subscriptions.json",
    "AGENT_SESSIONS_ORCHESTRATOR_LEDGER": ".config/agent-sessions/orchestrator-ledger.jsonl",
    "AGENT_SESSIONS_AGENT_USAGE": ".config/agent-sessions/agent-usage.json",
    "AGENT_SESSIONS_TEMPLATES": ".config/agent-sessions/templates.json",
    "AGENT_SESSIONS_TEMPLATE_VARS": ".config/agent-sessions/template-variables.json",
    "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY": ".config/agent-sessions/template-secrets.key",
    "AGENT_SESSIONS_PULSE_CACHE": ".config/agent-sessions/pulse-cache.json",
    "AGENT_SESSIONS_WORK_RECAP": ".config/agent-sessions/work-recap.json",  # #1086
    "AGENT_SESSIONS_VAPID_KEYS": ".config/agent-sessions/vapid.json",
    "AGENT_SESSIONS_HOME": ".local/share/agent-sessions",
    "AGENT_SESSIONS_OPENCODE_DB": ".local/share/opencode/opencode.db",
    "AGENT_SESSIONS_RUNTIME_DIR": "rt",  # short: AF_UNIX sockets live here (see below)
    "AGENT_SESSIONS_LOCK_DIR": ".agent-sessions/locks",
    "AGENT_SESSIONS_EDIT_RECOVERY": ".agent-sessions/edit-recovery",
    "AGENT_SESSIONS_SCROLLBACK_DIR": ".agent-sessions/scrollback",
}

# The app's own state directories in the REAL home — and in the `$HOME` the run was started with,
# when that differs (a developer pointing the whole run at a sandbox home gets the same guarantee
# for it). The tripwire guards these. Tests that point `$HOME` at a tmp dir are unaffected.
PROTECTED_DIRS: tuple[str, ...] = tuple(
    dict.fromkeys(
        os.path.join(home, sub)
        for home in (_REAL_HOME, os.environ.get("HOME") or _REAL_HOME)
        for sub in (".config/agent-sessions", ".local/share/agent-sessions", ".agent-sessions")
    )
)

_STATE_MODULE = "_agent_sessions_test_isolation"  # one per process, even if conftest re-imports


def _state() -> types.ModuleType:
    mod = sys.modules.get(_STATE_MODULE)
    if mod is None:
        mod = types.ModuleType(_STATE_MODULE)
        mod.sandbox = None
        mod.violations = []
        mod.reported = 0
        mod.outside = []
        mod.hook_installed = False
        sys.modules[_STATE_MODULE] = mod
    return mod


def _pin_store_env() -> str:
    """Point every store env var at the process's session sandbox (idempotent)."""
    st = _state()
    if st.sandbox is None:
        st.sandbox = tempfile.mkdtemp(prefix="blsess-")
    for var, sub in STORE_ENV_PATHS.items():
        os.environ[var] = os.path.join(st.sandbox, sub)
    return st.sandbox


def _under_protected(raw: object) -> str | None:
    if isinstance(raw, int) or raw is None:
        return None  # a descriptor, or nothing: not a path we can judge
    try:
        path = os.fsdecode(raw)
    except TypeError:
        return None
    if path.startswith("file:"):  # sqlite URI form
        path = path[5:].split("?", 1)[0]
    if not path or path == ":memory:":
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    path = os.path.normpath(path)
    for root in PROTECTED_DIRS:
        if path == root or path.startswith(root + os.sep):
            return path
    return None


class RealStoreTouched(RuntimeError):
    """Deliberately NOT an OSError, so a fail-soft `except OSError` in the app cannot eat it."""


# Audit event → indexes of its path arguments. A relative path with a `dir_fd` is not judged
# (it cannot be resolved without a syscall); every store in the app is opened by absolute path.
_AUDITED: dict[str, tuple[int, ...]] = {
    "open": (0,),
    "sqlite3.connect": (0,),
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.link": (0, 1),
    "os.symlink": (1,),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.utime": (0,),
    "shutil.rmtree": (0,),
}


def _audit(event: str, args: tuple) -> None:
    idx = _AUDITED.get(event)
    if idx is None:
        return
    for i in idx:
        if i < len(args):
            hit = _under_protected(args[i])
            if hit is not None:
                _state().violations.append(f"{event} {hit}")
                raise RealStoreTouched(
                    f"test isolation breach: {event} on the operator's real store {hit!r} — "
                    "a store path fell back to the real $HOME (a mid-test monkeypatch.undo()?)"
                )


def _install_tripwire() -> None:
    st = _state()
    if not st.hook_installed:  # audit hooks cannot be removed: install exactly once per process
        sys.addaudithook(_audit)
        st.hook_installed = True


_pin_store_env()
_install_tripwire()


def pytest_configure(config) -> None:
    # Re-assert at session start (xdist workers run this too; each worker is its own process).
    _pin_store_env()
    _install_tripwire()


def pytest_unconfigure(config) -> None:
    st = _state()
    if st.sandbox is not None:
        shutil.rmtree(st.sandbox, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus) -> None:
    st = _state()
    stray = st.outside + st.violations[st.reported :]
    if stray:  # touched outside any test (import, collection, a session fixture)
        sys.stderr.write("\n".join(["real-store isolation breaches:", *stray]) + "\n")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _real_store_tripwire():
    """Fail the test that touched a real store, even if the app swallowed the exception."""
    st = _state()
    st.outside.extend(st.violations[st.reported :])  # between tests: owned by no test
    start = st.reported = len(st.violations)
    yield
    hits = st.violations[start:]
    st.reported = len(st.violations)
    if hits:
        pytest.fail("touched the operator's real store: " + "; ".join(hits), pytrace=False)


from agent_sessions import auth, privatedir  # noqa: E402  (after the env pins, deliberately)
from agent_sessions.auth import AuthConfig, hash_password  # noqa: E402

# The production work factor, captured at conftest import — i.e. before ANY fixture (in
# particular _fast_pbkdf2 below) can patch the module constant. The dedicated guard test
# (test_password.py::test_production_kdf_iteration_count_is_not_silently_downgraded) pins
# this against the shipped 600k.
_PROD_PBKDF2_ITERS = auth._PBKDF2_ITERS


@pytest.fixture(scope="session")
def prod_pbkdf2_iters() -> int:
    return _PROD_PBKDF2_ITERS


@pytest.fixture(scope="session", autouse=True)
def _fast_pbkdf2():
    """Shrink the PBKDF2 work factor to 1,000 iterations for the test session (#699).

    At the production 600k (``auth._PBKDF2_ITERS``) every ``hash_password``/``verify_password``
    costs ~0.4–0.6 s — and the auth/2FA tests hash and verify *in-test* constantly (a single 2FA
    enrollment mints a full recovery-code set; recovery login scans every stored hash), which
    dominated the suite's wall clock. The patch is safe because the encoded hash is
    self-describing (``pbkdf2_sha256$<iters>$<salt>$<key>``) and ``verify_password`` derives with
    the iteration count parsed FROM the string, never the module constant — so test-minted
    ``$1000$`` hashes verify at 1,000 rounds while parsing, scheme rejection, the real PBKDF2
    derivation, and the constant-time compare all still execute unchanged. Production is
    structurally out of reach: only ``hash_password`` reads the constant at mint time, every real
    mint site (install.sh, change-password, recovery codes) runs in the server process, and
    ``tests/`` is never packaged into the wheel.

    The assertion below fails the WHOLE suite the moment the production work factor is
    weakened at the source; the dedicated #395 guard test in test_password.py additionally
    pins the exact value and round-trips one real 600k hash per suite."""
    assert auth._PBKDF2_ITERS >= 600_000, (
        f"production PBKDF2 work factor weakened: auth._PBKDF2_ITERS "
        f"is {auth._PBKDF2_ITERS}, expected >= 600_000"
    )
    orig = auth._PBKDF2_ITERS
    auth._PBKDF2_ITERS = 1_000
    yield
    auth._PBKDF2_ITERS = orig


@pytest.fixture(autouse=True)
def _isolate_scrollback(tmp_path, monkeypatch) -> None:
    """Point the persisted-scrollback cache (#206) at a per-test tmp dir and reset the
    in-memory ring state, so ``_buffer_append`` never writes to the real ``$HOME`` and no
    cache bytes leak between tests (``_SCROLLBACK_DIR`` is captured at import from
    ``Path.home()``, so setting ``$HOME`` later is not enough)."""
    from agent_sessions import webterm

    monkeypatch.setattr(webterm.scrollback, "_SCROLLBACK_DIR", tmp_path / "scrollback-cache")
    for d in (
        webterm._BUFFERS,
        webterm._TOTALS,
        webterm._LAST_OUTPUT_AT,
        webterm.scrollback._LAST_VISIBLE_OUTPUT_AT,
        webterm._SUPPRESS_OUTPUT_UNTIL,
        webterm.scrollback._MODES,
        webterm.scrollback._MODE_CARRY,
        webterm.scrollback._READY,
        webterm.scrollback._READY_SOURCE,
        webterm.scrollback._SANITIZE_CARRY,
    ):
        d.clear()
    webterm._LOADED_FROM_DISK.clear()


@pytest.fixture(autouse=True)
def _no_usage_analytics(monkeypatch) -> None:
    """The suite never reports usage (#1009). Every config fetch would otherwise be a candidate
    report, so the server-wide switch is off for every test; the analytics tests turn it back on
    for themselves and swap in an ``httpx.MockTransport``, so none of them reaches the network."""
    from agent_sessions import analytics

    monkeypatch.setenv("AGENT_SESSIONS_ANALYTICS", "0")
    monkeypatch.setattr(analytics, "_TRANSPORT", None)
    monkeypatch.setattr(analytics, "_in_flight", False)
    monkeypatch.setattr(analytics, "_last_attempt", None)


@pytest.fixture(autouse=True)
def _isolate_prefs(tmp_path, monkeypatch) -> None:
    """Point operator prefs (#465) at a per-test tmp file so a LIVE config on the machine running
    the suite can't leak in. ``prefs.get_project_roots()`` — read FIRST by
    ``project_dirs.effective_roots`` — otherwise returns the operator's real project roots (e.g. a
    runner whose owner has set ``/home/<user>`` as a root), and ``test_project_dirs``'s
    'no roots' / env-only cases fail against the live roots instead of the test's. ``_default_path``
    reads ``AGENT_SESSIONS_PREFS`` per call, so the env override is enough — no file is created, so
    ``_load`` sees an empty prefs and roots fall back to ``AGENT_SESSIONS_PROJECT_ROOTS``.

    Points at the CANONICAL ``~/.config/agent-sessions/prefs.json`` sub-path under the tmp dir so it
    coincides with what ``tmp_home`` already uses for METADATA/PROJECTS (and with the real default
    when ``$HOME`` is the tmp dir) — so a test that writes a legacy prefs file via the default path
    and reads it back through ``create_app`` (e.g. the migration test) still lines up."""
    monkeypatch.setenv(
        "AGENT_SESSIONS_PREFS", str(tmp_path / ".config" / "agent-sessions" / "prefs.json")
    )
    # The instruction-template library (#905) sits beside prefs.json and reads its override per
    # call the same way, so the same env pin keeps a live library out of the suite.
    monkeypatch.setenv(
        "AGENT_SESSIONS_TEMPLATES",
        str(tmp_path / ".config" / "agent-sessions" / "templates.json"),
    )
    # …and so does its variables library (#1090).
    monkeypatch.setenv(
        "AGENT_SESSIONS_TEMPLATE_VARS",
        str(tmp_path / ".config" / "agent-sessions" / "template-variables.json"),
    )
    # …and the key its secret variables are encrypted under (#1090 Phase 2): a test must never
    # create or read the operator's real key file.
    monkeypatch.setenv(
        "AGENT_SESSIONS_TEMPLATE_SECRETS_KEY",
        str(tmp_path / ".config" / "agent-sessions" / "template-secrets.key"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_TEMPLATE_SUGGESTIONS",
        str(tmp_path / ".config" / "agent-sessions" / "template-suggestions.json"),
    )
    # The session sidecar (#1054's leak: authfence, maintenance-prune, kimi and nudge-harness
    # tests wrote the operator's real metadata.json), the projects store, the pulse cache and the
    # VAPID identity all default under the real `$HOME` too, and `tmp_home` is opt-in. Same
    # canonical sub-path `tmp_home` uses, so a test that opts in still lines up.
    for var, name in (
        ("AGENT_SESSIONS_METADATA", "metadata.json"),
        ("AGENT_SESSIONS_PROJECTS", "projects.json"),
        ("AGENT_SESSIONS_PULSE_CACHE", "pulse-cache.json"),
        ("AGENT_SESSIONS_VAPID_KEYS", "vapid.json"),
    ):
        monkeypatch.setenv(var, str(tmp_path / ".config" / "agent-sessions" / name))
    from agent_sessions import template_secrets

    template_secrets._reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_opencode_db(tmp_path, monkeypatch) -> None:
    """Maintenance now measures this store too; no test may open the operator's database.

    The explicit opencode_db fixture overrides this with its populated disposable store.
    """
    monkeypatch.setenv(
        "AGENT_SESSIONS_OPENCODE_DB", str(tmp_path / ".local/share/opencode/opencode.db")
    )


@pytest.fixture(autouse=True)
def _isolate_lock_dir(tmp_path, monkeypatch) -> None:
    """Point the shared lock directory (#910) — the per-session `dtach` locks AND the cross-process
    authorization fence (`authfence.hold`, a `flock` on `locks/authorization.lock`) — at a per-test
    tmp dir.

    Measured, not hypothetical: ``sessionlock.lock_dir()`` defaults to ``~/.agent-sessions/locks``
    and ``tmp_home`` is opt-in, so every test that did not request it took the fence in the REAL
    directory — the one the live ``agent-sessions`` service on the same host and every concurrent
    CI job on the runner also use. Whenever another process held it, a test's
    ``session_transaction`` waited on the 10 s mutation budget and either timed out
    (``AuthorityFenceBusy … held elsewhere for more than 10s``) or outlasted its own bound
    ("never completed after the fence was released"). That was the whole "flaky under load"
    story for the ``*_is_ordered_against_*`` tests: load was when two jobs overlapped. The fence
    keeps its exact cross-process semantics — it is still a real ``flock`` — on a file that is the
    test's own. Autouse so no test can reach the production fence by forgetting to override."""
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _isolate_runtime_dir(tmp_path, monkeypatch) -> None:
    """Point the app's runtime dir at a per-test tmp dir, and declare that dir the trust root.

    **Both halves are required**, which is why #1006 spells it out. ``AGENT_SESSIONS_HOME`` does
    not reach ``ptybridge.runtime_dir()``: that helper reads ``AGENT_SESSIONS_RUNTIME_DIR`` and
    otherwise falls back to ``Path.home()/".agent-sessions"/"pty"``. So a test that pinned only the
    app home would create ``hooks-void`` in the operator's **real** runtime dir — on this host, the
    one the live ``agent-sessions`` service and every concurrent CI job also use. Autouse so no
    test can reach it by forgetting, exactly like the lock dir and the edit-recovery store above.

    The second half is the ``/tmp`` exception. :mod:`agent_sessions.privatedir` refuses a subtree
    anchored in shared temp, and pytest's ``tmp_path`` is precisely that
    (``/tmp/pytest-of-<user>/...``), so without a declared test root every git-write test would be
    refused. It is injected **here, in the harness**, and has no production spelling — no
    environment variable, no route, no pref, no config key (pinned by
    ``tests/test_privatedir.py``). A path outside the declared root is still judged by the
    production rules, which is what lets the default-home positive test assert the real policy.

    ``AGENT_SESSIONS_HOME`` is pinned alongside it at the same sub-path ``tmp_home`` would produce,
    so the two agree wherever a test uses both.

    **The runtime dir deliberately does NOT live under ``tmp_path``, and that is a hard kernel
    limit rather than a preference.** ``AF_UNIX`` caps ``sun_path`` at 108 bytes, and pytest's
    ``tmp_path`` already spends ~90 of them on a long test name. MEASURED: pointing the runtime dir
    at ``tmp_path`` made the socket for ``test_teardown_reaps_the_master_not_just_the_client``
    **146 bytes**, so ``dtach`` could not bind it and the test failed with "the dtach master never
    came up" — a failure with no mention of paths anywhere in it. A short ``mkdtemp`` keeps the
    same socket at ~65 bytes. Both that directory and ``tmp_path`` are declared trust roots,
    because the suite verifies paths under each."""
    runtime = tempfile.mkdtemp(prefix="blrt-")  # short on purpose — see the AF_UNIX note above
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", runtime)
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path / ".local" / "share" / "agent-sessions"))
    privatedir.set_policy_for_test(privatedir.TrustPolicy(test_roots=(str(tmp_path), runtime)))
    yield
    privatedir.set_policy_for_test(None)
    shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_edit_recovery(tmp_path, monkeypatch) -> None:
    """Point the editor's recovery store (#950) at a per-test tmp dir.

    ``fileedit.recovery_dir()`` defaults to ``~/.agent-sessions/edit-recovery`` — on the CI runner,
    the live app's own store — and it is created by a READ too (the same-filesystem check needs
    it), not just by a save. Autouse so no test can reach the real store by forgetting."""
    monkeypatch.setenv("AGENT_SESSIONS_EDIT_RECOVERY", str(tmp_path / "edit-recovery"))


@pytest.fixture(autouse=True)
def _isolate_agent_usage(tmp_path, monkeypatch) -> None:
    """Point the per-agent usage store (#839) at a per-test tmp file.

    Same reason as prefs and the bell: on a machine with a live install the real store holds this
    operator's actual plan percentages, and a test asserting "nothing reported yet" would pass or
    fail depending on whose laptop ran it. Autouse so no test can reach the real file by
    forgetting to override the env var."""
    monkeypatch.setenv(
        "AGENT_SESSIONS_AGENT_USAGE",
        str(tmp_path / ".config" / "agent-sessions" / "agent-usage.json"),
    )


@pytest.fixture(autouse=True)
def _isolate_notifications(tmp_path, monkeypatch) -> None:
    """Point the notification bell and the push-subscription store at per-test tmp files.

    Measured, not hypothetical: running the orchestrator suite on a machine with a live install
    wrote 15 rows into the operator's real ``~/.config/agent-sessions/notifications.json`` —
    `first` / `first message` from the session fixtures, project `/a`, session ids like
    ``claude:cccccccc-…`` — which then rendered in their bell alongside real escalations. Any
    test that reaches ``notifications.add`` does it, and the orchestrator ones do so readily
    (``notify: escalations`` announces on every ``escalated`` record, which is exactly what
    those tests produce).

    ``tmp_home`` fixes ``$HOME`` but is **opt-in**, and ``_store_path`` resolves ``Path.home()``
    per call — so every test that does not request ``tmp_home`` writes to the real store. The
    other stores (prefs, metadata, projects) already have their env overrides pinned; these two
    were simply missed.

    ``AGENT_SESSIONS_PUSH_SUBS`` is the load-bearing half. ``add`` fans out to every stored
    subscription when push is configured, so an unisolated run does not merely write a file —
    it can send a real Web Push to the operator's actual devices from a test fixture.

    ``AGENT_SESSIONS_ORCHESTRATOR_LEDGER`` was missed for the same reason and bites in the
    opposite direction: :func:`notifications.listing` reconciles against the ledger and retires
    any row whose action has already settled, so an unisolated test *reads* operator state and
    lets it decide the test's outcome. Measured on this host: the real ledger held three
    ``action_id: "act-A"`` records in the terminal ``delivered`` state — a synthetic id that
    only a test could have written — which silently retired the row
    ``test_re_linking_does_not_make_a_read_row_look_new`` had just created, leaving ``listing``
    empty and the test failing with ``IndexError`` on every run, locally and in CI alike. Two
    sibling files already pinned this env var per-file; putting it here makes the guarantee
    structural instead of something each new test file has to remember."""
    monkeypatch.setenv(
        "AGENT_SESSIONS_NOTIFICATIONS",
        str(tmp_path / ".config" / "agent-sessions" / "notifications.json"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_PUSH_SUBS",
        str(tmp_path / ".config" / "agent-sessions" / "push-subscriptions.json"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_ORCHESTRATOR_LEDGER",
        str(tmp_path / ".config" / "agent-sessions" / "orchestrator-ledger.jsonl"),
    )
    # #1086: the RECENT WORK summary cache. A test that generates one must never overwrite the
    # operator's, and a stale real cache must never decide what a test reads.
    monkeypatch.setenv(
        "AGENT_SESSIONS_WORK_RECAP",
        str(tmp_path / ".config" / "agent-sessions" / "work-recap.json"),
    )


@pytest.fixture(autouse=True)
def _isolate_missions_db(tmp_path, monkeypatch) -> None:
    """Point the missions store (#846) at a per-test tmp file, never the operator's real one.

    Same rule as prefs / metadata / notifications: ``_db_path`` reads the env per call, so the
    override is enough and no file is created until something writes. The schema-migration cache
    is keyed by path and is process-global, so it is cleared here too — otherwise a second test
    reusing a path string would skip the migration for a file that no longer exists."""
    from agent_sessions import missions

    monkeypatch.setenv(
        "AGENT_SESSIONS_MISSIONS_DB",
        str(tmp_path / ".config" / "agent-sessions" / "missions.db"),
    )
    missions.reset_schema_cache_for_test()
    yield
    missions.reset_schema_cache_for_test()


@pytest.fixture(autouse=True)
def _isolate_scan_cache() -> None:
    """Disable + reset the ``/api/sessions`` scan snapshot cache (#561) for every test.

    The cache memoises ``engines.scan_all()`` for a short TTL keyed on ``Path.home()``. With it
    live, a test that mutates the tree (archive moves a JSONL) and re-queries within the TTL would
    read the pre-mutation snapshot — and many tests monkeypatch ``engines.scan_all`` then call the
    route twice expecting each call to re-run the patch. Setting the TTL to 0 makes every request
    re-walk (identical to pre-#561 behaviour); the dedicated cache tests opt back in with an
    explicit ``set_scan_cache_ttl``. Cleared on the way in and out so no snapshot leaks across
    tests (distinct ``$HOME``s already isolate the key, but a shared 0-key entry could otherwise
    survive a test that raised the TTL).

    The per-file walk memo (#1048) is cleared alongside it, for the same reason and one more: it is
    keyed on ``(path, dev, ino, size, mtime_ns)``, and a test that writes a transcript, reads it,
    and rewrites it inside one filesystem timestamp tick would otherwise be served the first
    version. Production cannot hit that — an agent's writes are not nanosecond-adjacent — but a
    test's are, so the fixture removes the question rather than relying on clock resolution."""
    from agent_sessions import engines, scancache

    engines.set_scan_cache_ttl(0.0)
    engines.invalidate_scan_cache()
    scancache.clear()
    yield
    engines.set_scan_cache_ttl(0.0)
    engines.invalidate_scan_cache()
    scancache.clear()


@pytest.fixture(autouse=True)
def _reset_engine_entrypoints() -> None:
    """Forget every live provider's resolved entrypoint before each test (#853 §2b).

    `PluginProvider` caches its resolution keyed on the env override and the install record —
    not on $HOME, which is constant in production. Tests move $HOME (`tmp_home`, `no_engine_bin`),
    so without this an entrypoint resolved under the operator's real `~/.local/bin` by an earlier
    test would keep launching in a later one: the pass/fail of a launch test would depend on
    test order and on what this host has installed.
    """
    from agent_sessions import engines

    for p in engines.all_providers():
        if hasattr(p, "_cached"):
            p._cached = None


@pytest.fixture
def engine_bin(tmp_path, monkeypatch):
    """#853 P2: point engines' manifest `binary.env_var` at a provenance-acceptable fake agent.

    argv[0] resolves only from that env var or the manifest's `search_paths` under $HOME, never
    PATH and never `engines.base.*_BIN`. `engine_bin("claude", "codex")` wires the named engines
    (all seven when none is named) to one operator-owned `0755` file and returns argv[0] exactly as
    the launcher will see it — the real, symlink-free path.
    """
    from agent_sessions import engines

    b = tmp_path / "engine-bin" / "agent"

    def make(*engine_ids: str) -> str:
        if not b.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            # 0755 regardless of the runner's umask: provenance refuses a directory another user
            # could write, and whether a 0775 one counts depends on the host's group layout.
            b.parent.chmod(0o755)
            b.write_bytes(b"#!/bin/true\n")
            b.chmod(0o755)
        for e in engine_ids or [p.engine_id for p in engines.all_providers()]:
            monkeypatch.setenv(engines.get(e).manifest.binary.env_var, str(b))
        return os.path.realpath(b)

    return make


@pytest.fixture
def no_engine_bin(tmp_path, monkeypatch):
    """#853 P2: make every engine's entrypoint unlaunchable — the replacement for the pre-P2 idiom
    of patching `base.*_BIN` to a bare name.

    The agent engines get no env override and a $HOME whose `search_paths` hold nothing, so their
    `launch_argv` raises `EngineError("<engine>: no binary found")`. `shell` searches the system
    `/bin` / `/usr/bin`, which a test cannot empty, so its override names a file that does not
    exist and provenance refuses it (`EngineError("shell: refusing to launch — …")`). A $HOME
    already under this test's tmp dir (e.g. `tmp_home`) is kept — it is empty of binaries.
    """
    from agent_sessions import engines

    for p in engines.all_providers():
        monkeypatch.delenv(p.manifest.binary.env_var, raising=False)
    monkeypatch.setenv(engines.get("shell").manifest.binary.env_var, str(tmp_path / "no-bash"))
    home = Path(os.environ.get("HOME", "/"))
    if not home.is_relative_to(tmp_path):
        home = tmp_path / "no-engine-home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def tmp_home(tmp_path, monkeypatch) -> Path:
    """Pretend the user's ``$HOME`` is an empty tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_SESSIONS_METADATA",
        str(tmp_path / ".config" / "agent-sessions" / "metadata.json"),
    )
    monkeypatch.setenv(
        "AGENT_SESSIONS_PROJECTS",
        str(tmp_path / ".config" / "agent-sessions" / "projects.json"),
    )
    return tmp_path


@pytest.fixture
def fake_jsonl(tmp_home) -> Path:
    """Lay down a couple of Claude Code-shaped JSONLs under tmp_home/.claude/projects/."""
    projects = tmp_home / ".claude" / "projects"
    proj1 = projects / "-home-user-claude-repo-a"
    proj2 = projects / "-tmp-other"
    proj1.mkdir(parents=True)
    proj2.mkdir(parents=True)
    (proj1 / "11111111-1111-1111-1111-111111111111.jsonl").write_text(
        '{"type":"user","message":{"content":"first message on repo-a"}}\n'
    )
    (proj1 / "22222222-2222-2222-2222-222222222222.jsonl").write_text(
        '{"type":"user","message":{"content":[{"type":"text","text":"second"}]}}\n'
    )
    (proj2 / "33333333-3333-3333-3333-333333333333.jsonl").write_text(
        '{"type":"user","message":{"content":"hello tmp"}}\n'
    )
    # A dotted-path project: the dir name is lossy (demoapp.io and
    # demoapp/io both encode to ...-demoapp-io), but the JSONL carries the
    # real cwd. The scanner must prefer the JSONL cwd over the decode.
    proj3 = projects / "-home-user-claude-demoapp-io"
    proj3.mkdir(parents=True)
    (proj3 / "55555555-5555-5555-5555-555555555555.jsonl").write_text(
        '{"type":"user","cwd":"/home/user/claude/demoapp.io",'
        '"message":{"content":"dotted path session"}}\n'
    )
    # An archived one — same shape, different root.
    archive = tmp_home / ".claude" / "projects-archive" / "-home-user-claude-old"
    archive.mkdir(parents=True)
    (archive / "44444444-4444-4444-4444-444444444444.jsonl").write_text(
        '{"type":"user","message":{"content":"archived session"}}\n'
    )
    return tmp_home


@pytest.fixture(scope="session")
def password_hash() -> str:
    """The encoded ``hunter2`` hash, computed ONCE per test session (#395). ``hash_password``
    runs production-strength PBKDF2 (600k iterations — ~0.6 s on a free core, multiple seconds
    under load); the function-scoped ``auth_cfg`` re-ran it on every construction across ~16
    modules, so the suite paid that KDF cost dozens of times and page-thrashed on a loaded CI
    runner (#393). Fixtures only need a *valid* hash, not a fresh one — the KDF scheme itself is
    covered by the dedicated auth/password unit tests. Byte-format-identical, zero API change."""
    return hash_password("hunter2")


@pytest.fixture
def auth_cfg(tmp_path, monkeypatch, password_hash) -> AuthConfig:
    monkeypatch.setenv("AGENT_SESSIONS_USERNAME", "marcus")
    monkeypatch.setenv("AGENT_SESSIONS_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("AGENT_SESSIONS_SECRET_KEY", "x" * 64)
    monkeypatch.setenv("AGENT_SESSIONS_ORIGIN", "https://your-domain.example")
    # Isolate the 2FA store (#116) to a tmp path — otherwise twofactor.default_path()
    # resolves under the real HOME and the auth/login tests read the operator's real,
    # possibly-enabled 2fa.json, making the suite outcome host-dependent (Hermes #140).
    monkeypatch.setenv("AGENT_SESSIONS_2FA_FILE", str(tmp_path / "2fa.json"))
    return AuthConfig.from_env()


# opencode session ids in the fixture (≥1 top-level, 1 archived, 1 fork to skip,
# 1 ephemeral CI session to filter).
OC_TOP = "ses_aaaaaaaaaaaaaaaaaaaaaaaa"
OC_ARCHIVED = "ses_bbbbbbbbbbbbbbbbbbbbbbbb"
OC_FORK = "ses_ffffffffffffffffffffffff"
OC_ACT = "ses_cccccccccccccccccccccccc"
# An ephemeral CI workdir (nektos/act), recorded under a HOME that differs from
# the test's tmp_home — proving the ``.cache``/``act`` component match catches it
# regardless of the runtime cache env (#452).
OC_ACT_DIR = "/home/ci-runner/.cache/act/deadbeef0001/hostexecutor"


@pytest.fixture
def opencode_db(tmp_home, monkeypatch) -> Path:
    """A minimal opencode SQLite DB (only the columns OpenCodeProvider reads).

    Two top-level sessions (one archived) + one fork (``parent_id`` set, must be
    skipped) + one ephemeral CI session (``OC_ACT``, an ``~/.cache/act`` workdir
    that must be filtered, #452). ``time_updated`` is epoch **milliseconds**, like
    the real DB.
    """
    db = tmp_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    con.executemany(
        "INSERT INTO session "
        "(id, parent_id, directory, title, time_created, time_updated, time_archived) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            # time_created < time_updated (ms), like the real DB (#506).
            (OC_TOP, None, "/home/user/claude", "OC top one", 1777400000000, 1777460564154, None),
            (
                OC_ARCHIVED,
                None,
                "/tmp/other",
                "OC archived",
                1777200000000,
                1777300000000,
                1777400000000,
            ),
            (
                OC_FORK,
                OC_TOP,
                "/home/user/claude",
                "OC fork skip",
                1777460564000,
                1777460564999,
                None,
            ),
            (OC_ACT, None, OC_ACT_DIR, "OC ephemeral CI", 1777460565000, 1777460565000, None),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    return db


def pytest_collection_modifyitems(config, items):
    """Second, independent arm on the real-agent gate (#801) — a marker is not a gate.

    `tests/test_nudge_submit_real.py` launches real agent CLIs and spends real tokens. It was
    described as "opt-in twice over", but registering a `real_agent` marker in `pyproject.toml`
    only *names* it — pytest still collects and runs it. Review demonstrated the gap: a plain
    `pytest tests/test_nudge_submit_real.py` selected all 24 cases, and they skipped only
    because `AGENT_SESSIONS_REAL_AGENT` happened to be unset. Inherit that variable — from a
    shell that ran the matrix earlier, or a CI environment — and an ordinary run starts
    launching agents.

    So the second arm is enforced here: a `real_agent` test runs only when the `-m` expression
    *explicitly names it*. That composes with the environment gate in `nudge_harness` (both
    must hold) and, deliberately, does not fight the `-m "not e2e_install"` selections CI
    already uses — those do not mention `real_agent`, so they correctly deselect it.
    """
    # Match the IDENTIFIER, not a substring: `-m "not real_agent_extra"` contains the text
    # "real_agent" while selecting the real-agent cells, so a substring check let a routine run
    # launch the token-spending matrix whenever the env var happened to be inherited (review on
    # #858). Word boundaries around `_` do not work either — `\b` sees `_` as a word character —
    # so the lookaround is explicit.
    if re.search(r"(?<![0-9A-Za-z_])real_agent(?![0-9A-Za-z_])", config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(
        reason="real-agent tests need an explicit `-m real_agent` (they launch agents and spend "
        "tokens); AGENT_SESSIONS_REAL_AGENT=1 is required in addition"
    )
    for item in items:
        if "real_agent" in item.keywords:
            item.add_marker(skip)


class _EverySession(set):
    """A membership set that contains every key — "some mission holds every session"."""

    def __contains__(self, key: object) -> bool:
        return True


@pytest.fixture
def every_session_held(monkeypatch) -> None:
    """Give every decision a surface, for tests of OTHER axes of the bell (#1057).

    Since #1049 a decision is only counted and pushed when a mission holds its session
    (`notifications.decision_surfaces`). Tests written about the ledger projection, retirement,
    dedupe or push plumbing predate that and use bare session ids; this keeps them testing what
    they were written for. The surface rule itself is pinned in `test_decision_surfaces.py`,
    which does NOT use this fixture.
    """
    from agent_sessions import notifications

    monkeypatch.setattr(notifications, "decision_surfaces", lambda: _EverySession())


@pytest.fixture(autouse=True)
def _isolate_plugin_dirs(tmp_path, monkeypatch) -> None:
    """No test reads or writes the operator's real plugin or plugin-state directories (#853).

    The live providers read install/confirmation records from `plugin_state_home()`, which would
    otherwise be `~/.local/share/agent-sessions/plugin-state` on the host running the suite."""
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "_plugins"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "_plugin-state"))
    # The live providers fixed their state dir when the registry was imported (at collection,
    # before this fixture ran), so point each of them here too — otherwise a record the host
    # happens to hold would decide a test's outcome (independent review of PR #1115).
    from agent_sessions import engines

    for p in engines.all_providers():
        if hasattr(p, "state_dir"):
            monkeypatch.setattr(p, "state_dir", tmp_path / "_plugin-state")


@pytest.fixture(autouse=True)
def _no_host_engine_binaries(tmp_path, monkeypatch) -> None:
    """No test may launch through the HOST's real agent binaries (#853 P2).

    Every engine's `binary.env_var` points at a path that does not exist, so a test that forgot
    `engine_bin` fails the same way here as on a CI runner — instead of passing locally because
    `~/.local/bin/claude` happens to resolve (twice a false green on PR #1115). `engine_bin` and
    `no_engine_bin` override this per test."""
    from agent_sessions import engines

    for p in engines.all_providers():
        var = getattr(getattr(getattr(p, "manifest", None), "binary", None), "env_var", None)
        if var:
            monkeypatch.setenv(var, str(tmp_path / "_no-host-binary" / var.lower()))
