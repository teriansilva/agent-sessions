"""Ownership stays live after file caches, shared walks and page pins (#1277)."""

from __future__ import annotations

import time
import uuid

import pytest

from agent_sessions import engines, native_discovery, native_ownership
from agent_sessions.engines import base
from agent_sessions.plugins import admission, storage
from agent_sessions.scanner import Session
from test_codex import _write_rollout

NATIVE = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def source(tmp_home, monkeypatch):
    root = tmp_home / "rollouts"
    monkeypatch.setenv("AGENT_SESSIONS_CODEX_SESSIONS_DIR", str(root))
    prov = engines.get("codex")
    for native in (NATIVE, OTHER):
        _write_rollout(root, uuid=native, cwd="/work", first_user="synthetic fixture")
    return prov


def reserve(prov, native=None):
    with storage.locked(admission.LOCK):
        intent = native_ownership.reserve(
            f"fixture-api:{uuid.uuid4()}",
            native_ownership.source_identity(prov),
            operation_id=str(uuid.uuid4()),
            owner_token=str(uuid.uuid4()),
            request={"fixture": True},
        )
        if native is not None:
            intent = native_ownership.bind(
                intent.app_session_key,
                operation_id=intent.operation_id,
                owner_token=intent.owner_token,
                native_id=native,
            )
        return intent


def ids(rows):
    return {row.uuid for row in rows}


def test_file_parse_cache_lookup_checked_and_reconcile_recheck_ownership(source):
    assert ids(source.scan()) == {NATIVE, OTHER}  # populate scancache with unchanged files
    baseline = source.snapshot_session_ids("/work")
    assert baseline == {NATIVE, OTHER}
    assert source.lookup(NATIVE) is not None
    reserve(source, NATIVE)

    assert ids(source.scan()) == {OTHER}
    assert source.lookup(NATIVE) is None
    assert source.lookup(OTHER).uuid == OTHER
    rows, problems = source.scan_checked()
    assert ids(rows) == {OTHER} and not problems
    assert source.snapshot_session_ids("/work") == {OTHER}
    # The raw kind found two new ids. Only the unowned candidate may be reconciled.
    assert source.reconcile_new_session("/work", set()) == OTHER
    assert source.reconcile_new_session("/work", {OTHER}) is None
    assert engines.resolve_session(source.engine_id, NATIVE) is None
    assert engines.resolve_session(source.engine_id, OTHER).uuid == OTHER


@pytest.mark.parametrize("reader", ["warm", "pin", "since", "uncached"])
def test_ownership_added_after_a_walk_is_hidden_without_rewalking(source, monkeypatch, reader):
    walks = []
    rows = source.scan()

    def walk():
        walks.append(True)
        return rows

    monkeypatch.setattr(engines, "scan_all", walk)
    engines.set_scan_cache_ttl(30 if reader != "uncached" else 0)
    arrival = time.monotonic()
    if reader == "pin":
        initial, token = engines.scan_all_pinned("new")

        def read():
            return engines.scan_all_pinned(token)[0]

    elif reader == "since":
        initial = engines.scan_all_since(arrival)

        def read():
            return engines.scan_all_since(arrival)

    else:
        initial = engines.scan_all_cached()
        read = engines.scan_all_cached
    assert ids(initial) == {NATIVE, OTHER}
    reserve(source, NATIVE)
    assert ids(read()) == {OTHER}
    assert len(walks) == (2 if reader == "uncached" else 1)
    assert ids(rows) == {NATIVE, OTHER}, "ownership filtering mutated the cached walk"


def test_pending_preserves_listing_but_never_supplies_a_reconciliation(source):
    reserve(source)
    assert ids(source.scan()) == {NATIVE, OTHER}
    assert source.lookup(NATIVE) is not None
    with pytest.raises(native_ownership.OwnershipError, match="awaiting"):
        source.snapshot_session_ids("/work")
    assert source.reconcile_new_session("/work", {OTHER}) is None
    binder = native_discovery.guard_hook(
        source, "bind_session", lambda: base.Binding(base.BIND_BOUND, native=NATIVE, proof="nonce")
    )
    assert binder().state == base.BIND_UNREADABLE


def test_unreadable_ownership_hides_source_and_reports_incomplete_checked_read(source, monkeypatch):
    rows = source.scan()
    ordinary = Session("shell", OTHER, "/work", 1, "synthetic shell", False)

    def unreadable(_source):
        raise native_ownership.OwnershipError("unavailable", "synthetic unreadable ledger")

    monkeypatch.setattr(native_ownership, "source_snapshot", unreadable)
    assert source.scan() == []
    assert source.lookup(NATIVE) is None
    got, problems = source.scan_checked()
    assert got == [] and "ownership" in " ".join(problems)
    assert native_discovery.filter_cached([*rows, ordinary]) == [ordinary]
    with pytest.raises(native_ownership.OwnershipError):
        source.snapshot_session_ids("/work")
    assert source.reconcile_new_session("/work", {OTHER}) is None


def test_optional_hooks_and_ordinary_console_history_are_preserved(source):
    assert getattr(engines.get("shell"), "reconcile_new_session", None) is None
    assert getattr(source, "bind_session", None) is None
    assert ids(source.scan()) == {NATIVE, OTHER}
    assert source.reconcile_new_session("/work", {OTHER}) == NATIVE
    assert engines.archive_state(source, NATIVE) == "not-archived"
