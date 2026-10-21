"""API discovery must not churn the bounded conversation projection cache (#1539)."""

from __future__ import annotations

import os
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_sessions import chat_store, native_events, native_ipc, native_journal, scancache


def kind(root, engine="fixture-api"):
    store = chat_store.ChatStoreKind()
    store.owner = SimpleNamespace(
        engine_id=engine,
        manifest=SimpleNamespace(runtime="api"),
        store_root=lambda: root,
    )
    return store


def conversation(root, *, sid=None, cwd="/work", text="Synthetic prompt"):
    sid = sid or str(uuid.uuid4())
    binding = native_ipc.Binding(
        session_key=f"fixture-api:{sid}",
        worker_id=str(uuid.uuid4()),
        connection_id=str(uuid.uuid4()),
        adapter="codex-app-server",
    )
    chat_store.create(
        root, sid, cwd=cwd, request={"runtime": "api", "session_key": binding.session_key}
    )
    if text is not None:
        submit(root, binding, text)
    quiet(root, sid)
    return sid, binding


def quiet(root, sid):
    # The existing per-file memo deliberately refuses racily-clean files.
    stamp = time.time() - 10
    os.utime(root / f"{sid}.jsonl", (stamp, stamp))


def submit(root, binding, text):
    operation = str(uuid.uuid4())
    native_journal.claim(
        root,
        {
            **binding.envelope("request"),
            "request_id": str(uuid.uuid4()),
            "action": "submit",
            "params": {
                "operation_id": operation,
                "expected_revision": 1,
                "text": text,
                "context": {},
            },
        },
    )
    native_journal.append_events(
        root,
        binding,
        [
            {
                "kind": "text",
                "data": {
                    "operation_id": operation,
                    "native_turn_id": "turn",
                    "item_id": "item",
                    "text": "Synthetic reply",
                    "partial": False,
                    "truncated": False,
                },
            }
        ],
    )


def test_unchanged_roster_larger_than_projection_cache_does_not_rebuild_events(
    tmp_path, monkeypatch
):
    root = tmp_path / "conversations"
    monkeypatch.setattr(native_journal, "_CACHE_SESSIONS", 2)
    expected = {conversation(root)[0] for _ in range(3)}
    native_journal._CACHE.clear()
    writes = []
    append = native_events._Spool.append

    def observed(spool, data):
        writes.append(len(data))
        return append(spool, data)

    monkeypatch.setattr(native_events._Spool, "append", observed)
    store = kind(root)
    assert {r.uuid for r in store.scan()} == expected
    assert writes, "the cold scan must actually validate and fold the journal events"
    writes.clear()
    assert {r.uuid for r in store.scan()} == expected
    assert writes == [], "listing unchanged histories rebuilt their event projections"
    for sid in expected:
        assert store.lookup(sid).first_user_message == "Synthetic prompt"
    assert writes == [], "a scoped lookup must share the validated discovery row"


def test_first_submit_invalidates_an_empty_discovery_row(tmp_path):
    root = tmp_path / "conversations"
    sid, binding = conversation(root, text=None)
    store = kind(root)
    assert store.lookup(sid).first_user_message == ""
    submit(root, binding, "First actual prompt")
    quiet(root, sid)
    assert store.lookup(sid).first_user_message == "First actual prompt"


def test_same_length_rewrite_with_restored_mtime_invalidates_discovery(tmp_path):
    root = tmp_path / "conversations"
    sid, _ = conversation(root, cwd="/work/old")
    path = root / f"{sid}.jsonl"
    store = kind(root)
    assert store.lookup(sid).cwd == "/work/old"
    before = path.stat()
    path.write_bytes(path.read_bytes().replace(b"/work/old", b"/work/new"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert store.lookup(sid).cwd == "/work/new"
    path.write_bytes(path.read_bytes().replace(b'"native_event"', b'"broken_event"'))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.lookup(sid) is None, "a cached row hid a newly malformed journal"


@pytest.mark.parametrize("damage", ["permissions", "symlink", "hardlink", "torn"])
def test_warm_discovery_still_enforces_private_complete_journals(tmp_path, damage):
    root = tmp_path / "conversations"
    sid, _ = conversation(root)
    path = root / f"{sid}.jsonl"
    store = kind(root)
    assert store.lookup(sid) is not None
    if damage == "permissions":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "moved.jsonl"
        path.rename(target)
        path.symlink_to(target)
    elif damage == "hardlink":
        os.link(path, tmp_path / "linked.jsonl")
    else:
        with path.open("ab") as stream:
            stream.write(b'{"type":')
    assert store.lookup(sid) is None
    assert store.scan() == []


def test_same_id_in_different_store_roots_never_shares_a_row(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    sid, _ = conversation(first, cwd="/first")
    conversation(second, sid=sid, cwd="/other")
    assert kind(first).lookup(sid).cwd == "/first"
    assert kind(second).lookup(sid).cwd == "/other"


def test_path_replacement_invalidates_a_warm_discovery_row(tmp_path):
    first, replacement = tmp_path / "first", tmp_path / "replacement"
    sid, _ = conversation(first, cwd="/first")
    conversation(replacement, sid=sid, cwd="/other")
    store = kind(first)
    assert store.lookup(sid).cwd == "/first"
    path = first / f"{sid}.jsonl"
    before = path.stat()
    os.replace(replacement / path.name, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert store.lookup(sid).cwd == "/other"


def test_cache_hit_still_takes_the_shared_lock_and_syncs(tmp_path, monkeypatch):
    root = tmp_path / "conversations"
    sid, _ = conversation(root)
    store = kind(root)
    assert store.lookup(sid) is not None
    locks, syncs = [], []
    flock, fsync = chat_store.fcntl.flock, chat_store.os.fsync

    def locked(fd, operation):
        locks.append(operation)
        return flock(fd, operation)

    def synced(fd):
        syncs.append(fd)
        return fsync(fd)

    def no_fold(*args, **kwargs):
        pytest.fail("an unchanged cached row replayed its journal")

    monkeypatch.setattr(chat_store.fcntl, "flock", locked)
    monkeypatch.setattr(chat_store.os, "fsync", synced)
    monkeypatch.setattr(native_journal, "fold", no_fold)
    assert store.lookup(sid) is not None
    assert locks == [chat_store.fcntl.LOCK_SH]
    assert len(syncs) == 1


def test_maximum_length_prompts_obey_the_aggregate_byte_budget_without_truncation(tmp_path):
    root = tmp_path / "conversations"
    # Wide Unicode occupies four bytes per character in Python. Twelve valid maximum-length
    # submits exceed the default 8 MiB budget, despite being far below the 4,096-entry cap.
    prompt = "\U0001f680" * native_ipc.MAX_TEXT
    ids = [conversation(root, text=prompt)[0] for _ in range(12)]
    store = kind(root)
    assert len(store.scan()) == len(ids)
    assert 0 < scancache.stats()["entries"] < len(ids)
    retained_prompts = sum(
        sys.getsizeof(row.first_user_message) for row, _ in scancache._entries.values()
    )
    assert retained_prompts <= scancache.MAX_BYTES
    assert scancache._bytes <= scancache.MAX_BYTES
    # Eviction affects caching only: callers still get the COMPLETE prompt for search.
    assert store.lookup(ids[0]).first_user_message == prompt
    assert store.lookup(ids[-1]).first_user_message == prompt
