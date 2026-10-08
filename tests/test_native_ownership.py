"""Durable ownership precedes native handoff and permanently guards all console aliases."""

from __future__ import annotations

import dataclasses
import json
import multiprocessing
import os
import sqlite3
import stat
import time
import tomllib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sessions import metadata, ptybridge, sessionlock
from agent_sessions import native_ownership as ownership
from agent_sessions.engines import registry
from agent_sessions.plugins import admission, parse, storage


def uid():
    return str(uuid.uuid4())


def provider(path, *, engine="codex", alias=None, pattern=None):
    fixture = (
        Path(__file__).parent.parent
        / "src/agent_sessions/plugins/first_party"
        / engine
        / "plugin.toml"
    )
    doc = tomllib.loads(fixture.read_text())
    if alias:
        doc["identity"]["id"] = alias
        doc["binary"]["aliases"] = [doc["binary"]["name"]]
    if pattern:
        doc["session_id"]["pattern"] = pattern
    manifest = parse(doc)
    return SimpleNamespace(manifest=manifest, engine_id=manifest.id, store_root=lambda: path)


def roster(monkeypatch, *providers, retiring=()):
    monkeypatch.setattr(registry, "_PROVIDERS", list(providers))
    monkeypatch.setattr(registry, "_BY_ID", {p.engine_id: p for p in providers})
    monkeypatch.setattr(registry, "_RETIRING", {p.engine_id: p for p in retiring})
    monkeypatch.setattr(registry, "RETIREMENT_PROBLEMS", [])
    monkeypatch.setattr(registry, "LOAD_PROBLEMS", {})


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "private-state"
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(home))
    roster(monkeypatch, provider(tmp_path / "native-store"))
    return home


@pytest.fixture
def source(tmp_path):
    return ownership.source_identity(provider(tmp_path / "native-store"))


def reservation(source, **overrides):
    args = dict(
        app_session_key=f"native-api:{uid()}",
        source=source,
        operation_id=uid(),
        owner_token=uid(),
        request={"cwd": "/workspace", "model": "configured", "execution_binding": "original"},
    )
    args.update(overrides)
    return args


def bind(args, native):
    with storage.locked(admission.LOCK):
        return ownership.bind(
            args["app_session_key"],
            operation_id=args["operation_id"],
            owner_token=args["owner_token"],
            native_id=native,
        )


def rejected(code, call, *args, **kwargs):
    with pytest.raises(ownership.OwnershipError) as error:
        call(*args, **kwargs)
    assert error.value.code == code


def paths(state):
    return state / "native-ownership.initialized.json", state / "native-ownership/ownership.db"


def test_fresh_reads_do_not_initialize_ledger(state, source):
    assert ownership.source_snapshot(source) == ownership.SourceOwnership()
    assert ownership.lookup(f"native-api:{uid()}") is None
    assert all(not p.exists() for p in paths(state))


def test_identity_is_canonical_store_shape_and_not_alias(state, tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    link = tmp_path / "alias-store"
    link.symlink_to(store, target_is_directory=True)
    first = ownership.source_identity(provider(store))
    roster(monkeypatch, provider(store), provider(link, alias="alternate"))
    alias = ownership.source_identity(provider(link, alias="alternate"))
    assert alias == first
    # A broader alias pattern cannot evade reverse uniqueness of the native source.
    broad = ownership.source_identity(provider(link, pattern=r"^[a-z0-9\-]{1,80}$"))
    assert broad.key == first.key and broad.id_pattern != first.id_pattern
    claude = ownership.source_identity(provider(store, engine="claude"))
    assert claude.key != first.key
    native = uid()
    args = reservation(first)
    ownership.reserve(**args)
    bind(args, native)
    assert ownership.source_snapshot(broad).bound_native_ids == frozenset({native})
    rejected("owned", ownership.check_console, provider(link, alias="alternate"), native)
    rejected("conflict", ownership.reserve, **dict(args, source=broad))
    second = reservation(broad)
    ownership.reserve(**second)
    rejected("conflict", bind, second, native)


@pytest.mark.parametrize("engine", ["shell", "kimi", "gemini"])  # opencode is a source (#1312)
def test_unrelated_provider_does_not_open_ledger(state, tmp_path, monkeypatch, engine):
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported providers must not open the ownership ledger")

    monkeypatch.setattr(ownership, "source_snapshot", unexpected)
    prov = provider(tmp_path / engine, engine=engine)
    assert ownership.source_identity(prov) is None
    ownership.check_console(prov, uid())
    assert not state.exists()


def test_storeless_provider_does_not_open_ledger(state, tmp_path):
    prov = provider(tmp_path / "unused")
    prov.manifest = dataclasses.replace(prov.manifest, store=None)
    assert ownership.source_identity(prov) is None
    ownership.check_console(prov, uid())
    assert not state.exists()


@pytest.mark.parametrize("value", [None, "relative/path"])
def test_invalid_source_path_fails_closed(state, value):
    rejected("unavailable", ownership.source_identity, provider(value))


def test_broken_provider_and_symlink_loop_fail_closed(state, tmp_path):
    prov = provider(tmp_path)
    del prov.store_root
    rejected("unavailable", ownership.source_identity, prov)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    rejected("unavailable", ownership.source_identity, provider(loop))


def test_pending_then_bound_and_exact_replay_survive_reopen(state, source):
    args = reservation(source)
    first = ownership.reserve(**args)
    assert first.state == "pending" and first.native_id is None
    args["request"]["model"] = "changed"
    assert first.request["model"] == "configured"
    args["request"]["model"] = "configured"
    assert ownership.reserve(**args) == first
    assert ownership.lookup(first.app_session_key) == first
    prov = provider(source.canonical_path)
    rejected("pending", ownership.check_console, prov)
    rejected("pending", ownership.check_console, prov, f"new-{uid()}")
    ownership.check_console(prov, uid())  # resuming a concrete existing history stays open
    native = uid()
    bound = bind(args, native)
    assert bound.state == "bound" and bound.bound_at is not None
    assert bind(args, native) == ownership.reserve(**args) == bound
    assert ownership.lookup(first.app_session_key) == bound
    rejected("owned", ownership.check_console, prov, native)
    ownership.check_console(prov, uid())
    ownership.check_console(prov)
    assert ownership.source_snapshot(source) == ownership.SourceOwnership(frozenset({native}))
    for path in (*paths(state), state / "native-ownership.lock"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1


@pytest.mark.parametrize("changed", ["app", "operation", "owner", "cwd", "model", "source"])
def test_reservation_identity_is_immutable(state, source, tmp_path, changed):
    args = reservation(source)
    first = ownership.reserve(**args)
    replay = dict(args, request=dict(args["request"]))
    if changed == "app":
        replay["app_session_key"] = f"other-api:{uid()}"
    elif changed == "operation":
        replay["operation_id"] = uid()
    elif changed == "owner":
        replay["owner_token"] = uid()
    elif changed == "source":
        replay["source"] = ownership.source_identity(provider(tmp_path / "other"))
    else:
        replay["request"][changed] = "different"
    rejected("conflict", ownership.reserve, **replay)
    assert ownership.lookup(first.app_session_key) == first


def test_canonical_request_replay_does_not_coerce_json_types(state, source):
    args = reservation(source, request={"n": 1, "nested": {"b": 2, "a": [None, False]}})
    ownership.reserve(**args)
    ownership.reserve(**dict(args, request={"nested": {"a": [None, False], "b": 2}, "n": 1}))
    for value in (True, 1.0):
        rejected(
            "conflict", ownership.reserve, **dict(args, request=dict(args["request"], n=value))
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {1: "coerced"},
        {"nested": {1: "coerced"}},
        {"tuple": (1, 2)},
        {"n": float("nan")},
        {"n": float("inf")},
        {"large": "x" * 16384},
        [],
    ],
)
def test_invalid_metadata_is_rejected_before_initialization(state, source, metadata):
    rejected("invalid", ownership.reserve, **reservation(source, request=metadata))
    assert all(not p.exists() for p in paths(state))


def test_binding_requires_original_owner_and_captured_native_shape(state, source):
    args = reservation(source)
    original = ownership.reserve(**args)
    rejected("conflict", bind, dict(args, owner_token=uid()), uid())
    rejected("conflict", bind, dict(args, operation_id=uid()), uid())
    for native in ("../../bad", "not-a-uuid", "-option", uid() + "\n"):
        rejected("invalid", bind, args, native)
    assert ownership.lookup(original.app_session_key) == original
    native = uid()
    bound = bind(args, native)
    rejected("conflict", bind, args, uid())
    assert ownership.lookup(original.app_session_key) == bound


def test_same_native_history_cannot_bind_to_two_app_sessions(state, source):
    one, two = reservation(source), reservation(source)
    ownership.reserve(**one)
    ownership.reserve(**two)
    native = uid()
    bind(one, native)
    rejected("conflict", bind, two, native)
    assert ownership.lookup(two["app_session_key"]).state == "pending"
    assert ownership.source_snapshot(source) == ownership.SourceOwnership(frozenset({native}), True)


@pytest.mark.parametrize("retiring", [False, True])
def test_binding_refuses_an_alias_console_writer_and_releases_its_guards(
    state, source, monkeypatch, retiring
):
    primary = provider(source.canonical_path)
    alias = provider(source.canonical_path, alias="alternate")
    roster(
        monkeypatch,
        primary,
        *((alias,) if not retiring else ()),
        retiring=(alias,) if retiring else (),
    )
    args = reservation(source)
    pending = ownership.reserve(**args)
    native = uid()
    key = f"alternate:{native}"
    with sessionlock.acquire(key):
        rejected("busy", bind, args, native)
        assert ownership.lookup(args["app_session_key"]) == pending
    assert bind(args, native).native_id == native
    # Binding exclusion has committed; ownership is permanent, but no runtime is claimed.
    for engine in (primary.engine_id, alias.engine_id):
        guard = sessionlock.acquire(f"{engine}:{native}")
        assert guard is not None
        guard.release()


def test_binding_refuses_fresh_console_before_socket_or_transcript_exists(state, source):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    physical = f"codex:new-{uid()}"
    assert not list(ptybridge.runtime_dir().iterdir())
    with sessionlock.acquire(physical):
        with pytest.raises(ownership.OwnershipError, match="unresolved console creation") as error:
            bind(args, native)
        assert error.value.code == "busy"
        assert ownership.lookup(args["app_session_key"]).state == "pending"
    assert bind(args, native).native_id == native


@pytest.mark.parametrize("physical", ["native", "placeholder"])
@pytest.mark.parametrize("verdict", [ptybridge.ALIVE, ptybridge.UNKNOWN])
def test_binding_requires_dead_console_socket_even_without_held_writer_lock(
    state, source, monkeypatch, physical, verdict
):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    socket = ptybridge.socket_path("codex", native if physical == "native" else f"new-{uid()}")
    socket.touch()
    monkeypatch.setattr(
        ptybridge, "probe_master", lambda path: verdict if path == socket else ptybridge.DEAD
    )
    rejected("busy", bind, args, native)
    assert ownership.lookup(args["app_session_key"]).state == "pending"
    monkeypatch.setattr(ptybridge, "probe_master", lambda path: ptybridge.DEAD)
    assert bind(args, native).native_id == native


def test_binding_checks_every_physical_alias_of_native_history(state, source):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    first, second = f"codex:new-{uid()}", f"codex:new-{uid()}"
    metadata.set_alias(first, f"codex:{native}")
    metadata.set_alias(second, f"codex:{native}")
    with sessionlock.acquire(second):
        rejected("busy", bind, args, native)
    assert bind(args, native).native_id == native


def test_unrelated_reconciled_console_writer_does_not_block_binding(state, source):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    physical = f"codex:new-{uid()}"
    metadata.set_alias(physical, f"codex:{uid()}")
    with sessionlock.acquire(physical):
        assert bind(args, native).native_id == native


def _hold_plugin_worker(ready, release):
    with storage.locked("worker", wait=0):
        ready.set()
        assert release.wait(10)


def test_candidate_worker_in_another_process_blocks_binding_until_cleanup(state, source):
    args = reservation(source)
    pending = ownership.reserve(**args)
    native = uid()
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    worker = context.Process(target=_hold_plugin_worker, args=(ready, release))
    worker.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ownership.OwnershipError, match="plugin worker admission") as error:
            bind(args, native)
        assert error.value.code == "busy"
        assert ownership.lookup(args["app_session_key"]) == pending
        assert ownership.source_snapshot(source) == ownership.SourceOwnership(pending=True)
    finally:
        release.set()
        worker.join(10)
        if worker.is_alive():
            worker.kill()
            worker.join(10)
    assert worker.exitcode == 0
    assert bind(args, native).native_id == native


@pytest.mark.parametrize("damage", ["source-absent", "retirement", "roster", "metadata", "runtime"])
def test_unreadable_console_ownership_cannot_complete_a_pending_binding(
    state, source, monkeypatch, damage
):
    args = reservation(source)
    pending = ownership.reserve(**args)
    if damage == "source-absent":
        roster(monkeypatch)
    elif damage == "retirement":
        monkeypatch.setattr(registry, "RETIREMENT_PROBLEMS", ["unreadable retired manifest"])
    elif damage == "roster":
        monkeypatch.setattr(registry, "LOAD_PROBLEMS", {"manager": "unreadable"})
    elif damage == "metadata":
        path = metadata._default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid JSON")
    else:

        def unavailable():
            raise OSError("cannot enumerate console runtime")

        monkeypatch.setattr(ptybridge, "runtime_dir", unavailable)
    rejected("unavailable", bind, args, uid())
    assert ownership.lookup(args["app_session_key"]) == pending


def test_bound_replay_survives_source_removal_and_new_pending_intents(state, source, monkeypatch):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    original = bind(args, native)
    ownership.reserve(**reservation(source))
    roster(monkeypatch)
    assert bind(args, native) == original
    prov = provider(source.canonical_path)
    ownership.check_console(prov, uid(), allow_pending=True)
    rejected("owned", ownership.check_console, prov, native, allow_pending=True)
    rejected("owned", ownership.check_console, prov, native)
    rejected("pending", ownership.check_console, prov, f"new-{uid()}")


def test_database_capacity_refuses_growth_without_losing_existing_ownership(
    state, source, monkeypatch
):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    original = bind(args, native)
    db = paths(state)[1]
    capacity = db.stat().st_size
    monkeypatch.setattr(ownership, "MAX_DATABASE_BYTES", capacity)
    rejected(
        "unavailable", ownership.reserve, **reservation(source, request={"metadata": "x" * 12000})
    )
    assert db.stat().st_size <= capacity
    assert ownership.lookup(original.app_session_key) == original
    assert ownership.source_snapshot(source) == ownership.SourceOwnership(frozenset({native}))


def test_interrupted_initialization_never_recreates_empty_ledger(state, source, monkeypatch):
    original = ownership._connect

    def interrupted(db):
        assert paths(state)[0].is_file(), "sentinel must precede database initialization"
        raise OSError("crash after the durable sentinel")

    monkeypatch.setattr(ownership, "_connect", interrupted)
    args = reservation(source)
    rejected("unavailable", ownership.reserve, **args)
    marker = paths(state)[0].read_bytes()
    monkeypatch.setattr(ownership, "_connect", original)
    rejected("unavailable", ownership.source_snapshot, source)
    rejected("unavailable", ownership.reserve, **args)
    assert paths(state)[0].read_bytes() == marker


@pytest.mark.parametrize(
    "damage",
    [
        "db-missing",
        "marker-missing",
        "db-corrupt",
        "marker-corrupt",
        "generation",
        "version",
        "marker-uuid",
        "row",
    ],
)
def test_initialized_damage_fails_closed_and_is_not_repaired(state, source, damage):
    args = reservation(source)
    ownership.reserve(**args)
    bind(args, uid())
    marker, db = paths(state)
    if damage == "db-missing":
        db.unlink()
    elif damage == "marker-missing":
        marker.unlink()
    elif damage == "db-corrupt":
        db.write_bytes(b"not sqlite")
    elif damage == "marker-corrupt":
        marker.write_text("invalid json")
    elif damage == "generation":
        marker.write_text(json.dumps({"version": 1, "ledger_id": uid()}))
    elif damage == "marker-uuid":
        marker.write_text(json.dumps({"version": 1, "ledger_id": "bad"}))
    else:
        with sqlite3.connect(db) as con:
            if damage == "version":
                con.execute("PRAGMA user_version=999")
            else:
                con.execute("UPDATE ownership SET request_json='[]'")
    before = [(p.exists(), p.read_bytes() if p.exists() else None) for p in (marker, db)]
    rejected("unavailable", ownership.source_snapshot, source)
    rejected("unavailable", ownership.check_console, provider(source.canonical_path), uid())
    rejected("unavailable", ownership.reserve, **args)
    assert before == [(p.exists(), p.read_bytes() if p.exists() else None) for p in (marker, db)]


@pytest.mark.parametrize("file", ["db", "marker", "lock", "journal"])
@pytest.mark.parametrize("damage", ["public", "symlink", "hardlink"])
def test_unsafe_files_refuse_source_access(state, source, tmp_path, file, damage):
    ownership.reserve(**reservation(source))
    marker, db = paths(state)
    target = {
        "db": db,
        "marker": marker,
        "lock": state / "native-ownership.lock",
        "journal": db.with_name(db.name + "-journal"),
    }[file]
    if file == "journal":
        target.touch(mode=0o600)
    if damage == "public":
        target.chmod(0o644)
    elif damage == "symlink":
        saved = tmp_path / "saved"
        target.rename(saved)
        target.symlink_to(saved)
    else:
        os.link(target, tmp_path / "second-link")
    if file == "lock":
        # Reads are lock-free SQLite transactions; the ledger lock guards writes only.
        rejected("unavailable", ownership.reserve, **reservation(source))
    else:
        rejected("unavailable", ownership.source_snapshot, source)


def _reserve_worker(args, gate, queue):
    gate.wait(10)
    try:
        queue.put(("ok", ownership.reserve(**args).app_session_key))
    except ownership.OwnershipError as error:
        queue.put((error.code, None))


def _bind_worker(args, native, gate, queue):
    gate.wait(10)
    try:
        queue.put(("ok", bind(args, native).app_session_key))
    except ownership.OwnershipError as error:
        queue.put((error.code, None))


def race(worker, calls):
    context = multiprocessing.get_context("fork")
    gate, queue = context.Event(), context.Queue()
    workers = [context.Process(target=worker, args=(*call, gate, queue)) for call in calls]
    try:
        for process in workers:
            process.start()
        gate.set()
        results = [queue.get(timeout=10) for _ in workers]
        for process in workers:
            process.join(10)
            assert process.exitcode == 0
        return results
    finally:
        for process in workers:
            if process.is_alive():
                process.kill()
                process.join(10)
        queue.close()


def test_cross_process_same_intent_is_published_once(state, source):
    args = reservation(source)
    results = race(_reserve_worker, [(args,), (args,)])
    assert results == [("ok", args["app_session_key"])] * 2
    with sqlite3.connect(paths(state)[1]) as con:
        assert con.execute("SELECT count(*) FROM ownership").fetchone()[0] == 1


def test_cross_process_conflicting_intent_has_one_winner(state, source):
    args = reservation(source)
    other = dict(args, request={"cwd": "/changed"})
    results = race(_reserve_worker, [(args,), (other,)])
    assert sorted(code for code, _ in results) == ["conflict", "ok"]
    assert ownership.lookup(args["app_session_key"]).request in [args["request"], other["request"]]


def test_cross_process_reverse_binding_has_one_winner(state, source):
    one, two = reservation(source), reservation(source)
    ownership.reserve(**one)
    ownership.reserve(**two)
    native = uid()
    results = race(_bind_worker, [(one, native), (two, native)])
    assert sorted(code for code, _ in results) == ["conflict", "ok"]
    assert ownership.source_snapshot(source) == ownership.SourceOwnership(frozenset({native}), True)


def test_a_held_ledger_lock_does_not_block_or_hide_discovery_reads(state, source):
    """#1277 review: every read took the ledger's exclusive lock (2 s timeout), so one slow
    holder made discovery fail closed and hide every source session. Reads are lock-free."""
    from agent_sessions.plugins import storage

    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    bind(args, native)
    with storage.locked("native-ownership"):
        started = time.monotonic()
        assert ownership.source_snapshot(source).bound_native_ids == {native}
        assert ownership.lookup(args["app_session_key"]).native_id == native
        assert time.monotonic() - started < 1


@pytest.mark.parametrize("loss", ["absent", "empty", "null-aliases", "recreated"])
def test_a_lost_alias_never_lets_binding_overlap_a_live_placeholder_writer(state, source, loss):
    """Hermes on #1277: index presence was taken as completeness. The frame no longer trusts
    the index at all: an unattributed live placeholder writer refuses binding."""
    args = reservation(source)
    ownership.reserve(**args)
    native, physical = uid(), f"codex:new-{uid()}"
    metadata.set_alias(physical, f"codex:{native}")
    index = metadata._default_path()
    with sessionlock.acquire(physical):
        rejected("busy", bind, args, native)
        if loss == "absent":
            index.unlink()
        elif loss == "empty":
            index.write_text("")
        elif loss == "null-aliases":
            index.write_text(json.dumps({"__aliases__": None}))
        else:
            index.unlink()
            metadata.set_alias(f"codex:new-{uid()}", f"codex:{uid()}")  # unrelated rewrite
        rejected("busy", bind, args, native)
        assert ownership.lookup(args["app_session_key"]).state == "pending"
    assert bind(args, native).native_id == native


def test_only_a_placeholder_runtime_can_be_aliased(state, source):
    prov = provider(source.canonical_path)
    with pytest.raises(admission.Refused, match="placeholder"):
        admission.publish_alias(prov, f"codex:{uid()}", f"codex:{uid()}")
    assert not metadata.load_checked()[1]  # refused before anything was written


def test_a_stale_concrete_runtime_alias_to_the_history_fails_closed(state, source):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    metadata.set_alias(f"codex:{uid()}", f"codex:{native}")  # written outside publication
    rejected("unavailable", bind, args, native)


def test_an_unrelated_concrete_console_never_blocks_binding(state, source):
    args = reservation(source)
    ownership.reserve(**args)
    native = uid()
    with sessionlock.acquire(f"codex:{uid()}"):  # another console, its own history
        assert bind(args, native).native_id == native


def test_discharge_releases_only_an_unbound_creation_for_its_original_owner(state, source):
    """#1278: the one recovery path for a reservation; a bound history is permanent."""
    pending = reservation(source)
    ownership.reserve(**pending)
    prov = provider(source.canonical_path)
    rejected("pending", ownership.check_console, prov, f"new-{uid()}")
    with storage.locked(admission.LOCK):
        rejected(
            "conflict",
            ownership.discharge,
            pending["app_session_key"],
            operation_id=pending["operation_id"],
            owner_token=uid(),
        )
        assert ownership.discharge(
            pending["app_session_key"],
            operation_id=pending["operation_id"],
            owner_token=pending["owner_token"],
        )
        assert not ownership.discharge(
            pending["app_session_key"],
            operation_id=pending["operation_id"],
            owner_token=pending["owner_token"],
        )
    assert ownership.lookup(pending["app_session_key"]) is None
    ownership.check_console(prov, f"new-{uid()}")
    bound = reservation(source)
    ownership.reserve(**bound)
    native = uid()
    bind(bound, native)
    with storage.locked(admission.LOCK):
        rejected(
            "owned",
            ownership.discharge,
            bound["app_session_key"],
            operation_id=bound["operation_id"],
            owner_token=bound["owner_token"],
        )
    rejected("owned", ownership.check_console, prov, native)
