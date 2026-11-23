"""The suite's own guarantee: no test can reach the operator's real app stores.

A mid-test `monkeypatch.undo()` once reverted conftest's store pins, and the missions store of
the app running on the CI host was migrated by a branch's schema (#1097). `conftest.py` now has
two independent layers — a session-level env backstop and an audit-hook tripwire — and these
tests pin both, so neither can be removed without a red test.

The tripwire's own probes run against a DISPOSABLE protected root (#1118). Never the real store:
if the hook ever breaks, this file would otherwise be the thing that writes a stray file, an empty
DB and a directory into the operator's live store before failing.
"""

from __future__ import annotations

import os
import pwd
import sqlite3
import sys

import pytest

import conftest

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
