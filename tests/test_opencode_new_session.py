"""opencode new-session via launch-then-reconcile (#127).

opencode can't pin a new-session id, so the ws/dtach bridge launches under a
client-minted ``new-<uuid>`` placeholder and reconciles to opencode's real ``ses_…``
id by diffing ``opencode.db`` (read-only). The placeholder→real alias is persisted in
the metadata sidecar and resolved at every identity surface (socket / lock / buffer /
metadata key / list de-dupe). These tests lock that — the highest-risk path in the repo.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from agent_sessions import engines, main, metadata, ptybridge, sessionlock, sessions

_PLACEHOLDER = "new-11111111-1111-1111-1111-111111111111"
_PLACEHOLDER_KEY = f"opencode:{_PLACEHOLDER}"
_REAL = "ses_realreal0000000000000000"
_REAL_KEY = f"opencode:{_REAL}"
_CWD = "/home/user/claude"


def _seed_db(tmp_home: Path, monkeypatch, rows: list[tuple]) -> Path:
    """Write a minimal opencode.db with the given (id, parent_id, directory) rows."""
    db = tmp_home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, title TEXT, "
        "time_created INTEGER, time_updated INTEGER, time_archived INTEGER)"
    )
    con.executemany(
        "INSERT INTO session "
        "(id, parent_id, directory, title, time_created, time_updated, time_archived) "
        "VALUES (?,?,?,?,?,?,?)",
        [(sid, parent, directory, "t", 1, 1, None) for (sid, parent, directory) in rows],
    )
    con.commit()
    con.close()
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(db))
    return db


def _add_rows(db: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO session "
        "(id, parent_id, directory, title, time_created, time_updated, time_archived) "
        "VALUES (?,?,?,?,?,?,?)",
        [(sid, parent, directory, "t", 1, 1, None) for (sid, parent, directory) in rows],
    )
    con.commit()
    con.close()


# ---- placeholder validation ---------------------------------------------------


def test_placeholder_accepted_only_on_new_path():
    # The new-<uuid> placeholder is a valid id ONLY with allow_new_placeholder (the ws
    # new=1 launch). On the resume/attach path it must be rejected (a placeholder can
    # never attach to or resume an arbitrary session).
    prov, native = engines.parse_key(_PLACEHOLDER_KEY, allow_new_placeholder=True)
    assert prov.engine_id == "opencode" and native == _PLACEHOLDER
    with pytest.raises(engines.EngineError):
        engines.parse_key(_PLACEHOLDER_KEY)  # default: no placeholder allowed


def test_placeholder_only_for_opencode():
    # The placeholder is accepted only by reconciling engines (opencode, codex — see test_codex).
    # A non-reconciling engine like claude (it pins its own id) must not accept it.
    with pytest.raises(engines.EngineError):
        engines.parse_key(f"claude:{_PLACEHOLDER}", allow_new_placeholder=True)


def test_is_opencode_new_placeholder():
    assert engines.is_opencode_new_placeholder(_PLACEHOLDER_KEY) is True
    assert engines.is_opencode_new_placeholder(_REAL_KEY) is False
    assert engines.is_opencode_new_placeholder("claude:new-x") is False
    assert engines.is_opencode_new_placeholder("opencode:not-new") is False


# ---- snapshot / diff attribution ----------------------------------------------


def test_snapshot_is_cwd_scoped(tmp_home, monkeypatch):
    _seed_db(
        tmp_home,
        monkeypatch,
        [
            ("ses_old1aaaaaaaaaaaaaaaaaaaaaa", None, _CWD),
            ("ses_elsewhere00000000000000000", None, "/other/dir"),
        ],
    )
    snap = engines.OpenCodeProvider().snapshot_session_ids(_CWD)
    assert snap == {"ses_old1aaaaaaaaaaaaaaaaaaaaaa"}  # only this cwd


def test_reconcile_finds_single_new_id(tmp_home, monkeypatch):
    db = _seed_db(tmp_home, monkeypatch, [("ses_old1aaaaaaaaaaaaaaaaaaaaaa", None, _CWD)])
    prov = engines.OpenCodeProvider()
    snap = prov.snapshot_session_ids(_CWD)
    assert prov.reconcile_new_session(_CWD, snap) is None  # opencode hasn't written yet
    _add_rows(db, [(_REAL, None, _CWD)])
    assert prov.reconcile_new_session(_CWD, snap) == _REAL  # the one new id is ours


def test_reconcile_ignores_other_cwd_and_forks(tmp_home, monkeypatch):
    db = _seed_db(tmp_home, monkeypatch, [("ses_old1aaaaaaaaaaaaaaaaaaaaaa", None, _CWD)])
    prov = engines.OpenCodeProvider()
    snap = prov.snapshot_session_ids(_CWD)
    # A new session in a DIFFERENT cwd + a fork (parent_id set) of our cwd → neither is
    # attributed to us; reconcile still reports "not yet".
    _add_rows(
        db,
        [
            ("ses_otherdir0000000000000000000", None, "/other/dir"),
            ("ses_fork000000000000000000000000", "ses_old1aaaaaaaaaaaaaaaaaaaaaa", _CWD),
        ],
    )
    assert prov.reconcile_new_session(_CWD, snap) is None


def test_reconcile_ambiguous_returns_list_no_guess(tmp_home, monkeypatch):
    db = _seed_db(tmp_home, monkeypatch, [("ses_old1aaaaaaaaaaaaaaaaaaaaaa", None, _CWD)])
    prov = engines.OpenCodeProvider()
    snap = prov.snapshot_session_ids(_CWD)
    # Two new same-cwd sessions in the poll window → ambiguous; must NOT guess.
    _add_rows(db, [(_REAL, None, _CWD), ("ses_secondnew0000000000000000", None, _CWD)])
    result = prov.reconcile_new_session(_CWD, snap)
    assert isinstance(result, list) and len(result) == 2  # caller fails safe


def test_reconcile_fail_soft_missing_db(tmp_home, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_OPENCODE_DB", str(tmp_home / "nope.db"))
    prov = engines.OpenCodeProvider()
    assert prov.snapshot_session_ids(_CWD) == set()  # absent DB = valid empty baseline
    assert prov.reconcile_new_session(_CWD, set()) is None


def test_snapshot_returns_none_on_read_failure_not_empty(tmp_home, monkeypatch):
    # The baseline snapshot MUST distinguish "read failed" from "empty". If a transient
    # sqlite error degraded the baseline to an empty set while the cwd already had a row, the
    # next poll would see that pre-existing row as "new" and misattribute it — a wrong attach,
    # exactly what #127 must never do. On read failure the snapshot reports None so the caller
    # skips reconciliation (a missing DB *file* is still a valid empty baseline, tested above).
    _seed_db(tmp_home, monkeypatch, [("ses_existing00000000000000000", None, _CWD)])
    prov = engines.OpenCodeProvider()
    # Healthy baseline includes the existing row…
    assert prov.snapshot_session_ids(_CWD) == {"ses_existing00000000000000000"}
    # …and the hazard is real: an EMPTY baseline would misattribute that pre-existing row.
    assert prov.reconcile_new_session(_CWD, set()) == "ses_existing00000000000000000"

    # The guard: a failed read reports None (not set()), so the caller skips reconcile.
    def boom(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(engines.OpenCodeProvider, "_query_rows", boom)
    assert prov.snapshot_session_ids(_CWD) is None


# ---- converge only after the alias is durable (finding #1) --------------------


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, s: str) -> None:
        self.sent.append(s)


class _FakeProv:
    """A provider stub that resolves to one real id on the first reconcile poll."""

    engine_id = "opencode"

    def __init__(self, real: str) -> None:
        self._real = real
        self.calls = 0

    def reconcile_new_session(self, cwd, snapshot):
        self.calls += 1
        return self._real


def test_reconcile_does_not_converge_when_alias_persist_fails(tmp_home, monkeypatch):
    # If persisting the placeholder→real alias fails (full disk, permissions, …) we must NOT
    # send the {t:"id"} converge frame: the browser URL would become ses_… with no alias on
    # disk, so a reload/reattach by the real id could not find the placeholder socket/lock and
    # might launch a SECOND writer for the same opencode session. Stay on the placeholder.
    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(metadata, "set_alias", boom)
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    ws, prov = _FakeWS(), _FakeProv(_REAL)
    asyncio.run(main._reconcile_new_session(ws, prov, _PLACEHOLDER, _CWD, set()))
    assert ws.sent == []  # never converged
    assert prov.calls >= 1  # but it did try to reconcile


def test_reconcile_converges_after_alias_persist_succeeds(tmp_home, monkeypatch):
    # The happy path: alias persisted (real→placeholder resolvable on a later attach), THEN
    # the client is converged exactly once.
    monkeypatch.setattr(main, "_RECONCILE_INTERVAL_S", 0)
    ws, prov = _FakeWS(), _FakeProv(_REAL)
    asyncio.run(main._reconcile_new_session(ws, prov, _PLACEHOLDER, _CWD, set()))
    assert ws.sent == [json.dumps({"t": "id", "sid": _REAL_KEY})]
    assert metadata.load_aliases() == {_PLACEHOLDER_KEY: _REAL_KEY}


# ---- alias persistence + resolution at every identity surface -----------------


def test_set_alias_persists_and_loads(tmp_home):
    metadata.set_alias(_PLACEHOLDER_KEY, _REAL_KEY)
    assert metadata.load_aliases() == {_PLACEHOLDER_KEY: _REAL_KEY}


def test_alias_not_a_session_row(tmp_home):
    # The alias map must never leak into the session list (load() skips it).
    metadata.set_alias(_PLACEHOLDER_KEY, _REAL_KEY)
    metadata.patch("opencode:ses_keep0000000000000000000", title="real")
    rows = metadata.load()
    assert "__aliases__" not in rows
    assert _PLACEHOLDER_KEY not in rows
    assert "opencode:ses_keep0000000000000000000" in rows


def test_physical_key_resolves_real_to_placeholder(tmp_home):
    # The live socket/lock/buffer are keyed by the placeholder; an attach by the REAL id
    # must resolve back to it (the inverse of the stored placeholder→real map).
    metadata.set_alias(_PLACEHOLDER_KEY, _REAL_KEY)
    assert engines.physical_key(_REAL_KEY) == _PLACEHOLDER_KEY
    # Non-aliased / unrelated keys pass through unchanged.
    other = "opencode:ses_unrelated00000000000"
    assert engines.physical_key(other) == other
    assert engines.physical_key(_PLACEHOLDER_KEY) == _PLACEHOLDER_KEY


def test_alias_resolution_across_socket_lock_metadata(tmp_home, monkeypatch):
    # Prove the SAME physical key drives socket, lock, and metadata after reconcile, so a
    # real-id attach lands on the placeholder's resources (no partial swap → no #64 ghost).
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_home / "pty"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_home / "locks"))
    metadata.set_alias(_PLACEHOLDER_KEY, _REAL_KEY)

    phys = engines.physical_key(_REAL_KEY)  # what the ws route derives for a real attach
    assert phys == _PLACEHOLDER_KEY
    _engine, _, phys_native = phys.partition(":")

    # socket: keyed by the placeholder native id
    sock = ptybridge.socket_path("opencode", phys_native)
    assert _PLACEHOLDER.replace("-", "-") in sock.name  # placeholder, not the real ses_

    # lock: keyed by the placeholder full key
    lp = sessionlock.lock_path(phys)
    assert "new-" in lp.name and _REAL not in lp.name

    # metadata: a title set on the placeholder is fetched via the physical key
    metadata.patch(_PLACEHOLDER_KEY, title="set-before-converge")
    aliases = metadata.load_aliases()
    meta_index = metadata.load()
    resolved = meta_index.get(_REAL_KEY) or meta_index.get(engines.physical_key(_REAL_KEY, aliases))
    assert resolved is not None and resolved.title == "set-before-converge"


def test_alias_survives_simulated_restart(tmp_home, monkeypatch):
    # The alias lives in the on-disk sidecar, so a *fresh* read (new "app instance") still
    # resolves real→placeholder — the restart-survival path. The socket/lock stay under the
    # placeholder across the restart; this is what lets a real-id attach find them again.
    metadata.set_alias(_PLACEHOLDER_KEY, _REAL_KEY)
    # Simulate a new process: load aliases from scratch (no in-memory state carried over).
    reloaded = metadata.load_aliases()
    assert reloaded == {_PLACEHOLDER_KEY: _REAL_KEY}
    assert engines.physical_key(_REAL_KEY, reloaded) == _PLACEHOLDER_KEY


def test_open_action_uses_physical_key_for_lock(tmp_home, monkeypatch):
    # After reconcile, opening by the REAL id must take the lock under the PLACEHOLDER key
    # (single-writer must be one key across the alias). The ws route resolves to the
    # physical native before calling open_action; assert open_action on the placeholder
    # native locks the placeholder key.
    monkeypatch.setenv("AGENT_SESSIONS_RUNTIME_DIR", str(tmp_home / "pty"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_home / "locks"))
    action, lock = sessions.open_action("opencode", _PLACEHOLDER)
    assert action == sessions.LAUNCH and lock is not None
    assert sessionlock.is_locked(_PLACEHOLDER_KEY) is True  # held under the placeholder
    lock.release()
