"""The suite's own guarantee: no test can reach the operator's real app stores.

A mid-test `monkeypatch.undo()` once reverted conftest's store pins, and the missions store of
the app running on the CI host was migrated by a branch's schema (#1097). `conftest.py` now has
two independent layers — a session-level env backstop and an audit-hook tripwire — and these
tests pin both, so neither can be removed without a red test.
"""

from __future__ import annotations

import os
import pwd
import sqlite3
import sys

import pytest

_REAL_HOME = pwd.getpwuid(os.getuid()).pw_dir
_REAL_STORE = os.path.join(_REAL_HOME, ".config", "agent-sessions")


def _isolation():
    return sys.modules["_agent_sessions_test_isolation"]


def test_the_tripwire_refuses_the_real_store_before_the_syscall():
    st = _isolation()
    before = len(st.violations)
    probe = os.path.join(_REAL_STORE, "tripwire-probe-never-created.db")
    try:
        with pytest.raises(RuntimeError, match="test isolation breach"):
            open(probe, "w")  # noqa: SIM115 — the hook raises before a descriptor exists
        with pytest.raises(RuntimeError, match="test isolation breach"):
            sqlite3.connect(probe)
        with pytest.raises(RuntimeError, match="test isolation breach"):
            os.makedirs(os.path.join(_REAL_STORE, "tripwire-probe-dir"))
        assert not os.path.exists(probe), "the hook must refuse before the file is created"
        assert len(st.violations) == before + 3, "every refusal is recorded for the reporter"
    finally:
        del st.violations[before:]  # this test's own, deliberate breaches


def test_an_undo_falls_back_to_the_sandbox_never_the_real_home(monkeypatch):
    """What `monkeypatch.undo()` restores is the session pin, so the fallback is harmless."""
    from agent_sessions import discover, metadata, missions, prefs

    monkeypatch.undo()  # the exact mistake this backstop exists for
    sandbox = _isolation().sandbox
    for path in (
        missions._db_path(),
        prefs._default_path(),
        metadata._default_path(),
        discover.default_env_path(),
    ):
        assert str(path).startswith(sandbox + os.sep), path
        assert not str(path).startswith(_REAL_HOME + os.sep + "."), path
