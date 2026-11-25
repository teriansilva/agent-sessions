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
    # #1946: even a COLD row reads the header and first message, never the event history.
    assert writes == [], "the cold scan folded the journal events"
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
    # #1946: a malformed LATER record no longer hides the row (discovery reads only the header and
    # first message, as a chain's has since #1733), but the rewrite still invalidates the memo, and
    # opening the conversation still refuses the journal.
    assert store.lookup(sid).cwd == "/work/new"
    with pytest.raises(native_journal.JournalError):
        native_journal.read(root, sid)


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
    # The session's stable lock and, while it is the active segment, segment 0's own (#1733).
    assert locks == [chat_store.fcntl.LOCK_SH, chat_store.fcntl.LOCK_SH]
    assert len(syncs) == 1


def test_maximum_length_prompts_obey_the_aggregate_byte_budget_without_truncation(
    tmp_path, monkeypatch
):
    root = tmp_path / "conversations"
    # Wide Unicode occupies four bytes per character in Python. Twelve valid maximum-length
    # submits exceed an 8 MiB budget, despite being far below the entry cap. Pinned here rather
    # than read off the default, which #1976 raised so a full walk fits.
    monkeypatch.setattr(scancache, "MAX_BYTES", 8 * 1024 * 1024)
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


def grow(root, binding, records=200):
    """History after the first message: what a discovery row must never replay."""
    pressure = {"kind": "memory_pressure", "data": {"level": "normal"}}
    for _ in range(records // 20):
        native_journal.append_events(root, binding, [pressure] * 20)


def _no_fold(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("a discovery row folded the journal's history")

    monkeypatch.setattr(native_journal, "fold", refuse)
    monkeypatch.setattr(native_journal, "_fold_segment", refuse)
    native_journal._CACHE.clear()
    scancache.clear()


def test_a_cold_single_segment_row_reads_only_the_header_and_first_message(tmp_path, monkeypatch):
    """#1946: the row of a journal that never rolled over is two facts, not a replay of its
    history — a cold walk of every API session cost about a minute on prod."""
    root = tmp_path / "conversations"
    sid, binding = conversation(root, cwd="/work/first", text="The first prompt")
    grow(root, binding)
    quiet(root, sid)
    _no_fold(monkeypatch)
    reads = []
    pread = native_journal.os.pread
    monkeypatch.setattr(
        native_journal.os,
        "pread",
        lambda fd, n, offset: reads.append(n) or pread(fd, n, offset),
    )
    row = kind(root).lookup(sid)
    assert (row.cwd, row.first_user_message) == ("/work/first", "The first prompt")
    assert row.created_at
    size = (root / f"{sid}.jsonl").stat().st_size
    assert sum(reads) < size + 1024 * 1024  # one bounded chunk + the tail byte, never a fold


def test_a_cold_single_segment_row_with_no_message_yet_is_empty(tmp_path, monkeypatch):
    root = tmp_path / "conversations"
    sid, _ = conversation(root, text=None)
    _no_fold(monkeypatch)
    assert kind(root).lookup(sid).first_user_message == ""


def test_a_single_segment_row_matches_the_fold(tmp_path):
    """The row's facts are the fold's: the header, and the first claimed submit's text."""
    root = tmp_path / "conversations"
    sid, binding = conversation(root, cwd="/work/x", text="Opening line")
    grow(root, binding)
    quiet(root, sid)
    journal = native_journal.read(root, sid)
    first = next(
        op.request["params"]["text"]
        for op in journal.operations.values()
        if op.request["action"] == "submit"
    )
    row = kind(root).lookup(sid)
    assert (row.cwd, row.first_user_message) == (journal.header["cwd"], first)


def test_a_queued_first_message_is_the_rows_first_message(tmp_path, monkeypatch):
    """A first message QUEUED behind a busy turn (`native_queued`) is the row's first message, as
    it is the fold's (#1946 review): the discovery reader must not see only claimed submits."""
    root = tmp_path / "conversations"
    sid, binding = conversation(root, text=None)
    native_journal.claim(
        root,
        {
            **binding.envelope("request"),
            "request_id": str(uuid.uuid4()),
            "action": "submit",
            "params": {
                "operation_id": str(uuid.uuid4()),
                "expected_revision": 1,
                "text": "Queued first prompt",
                "context": {},
            },
        },
        queued=True,
    )
    grow(root, binding)
    quiet(root, sid)
    journal = native_journal.read(root, sid)
    (only,) = journal.operations.values()
    assert only.handoff == "queued"
    _no_fold(monkeypatch)
    row = kind(root).lookup(sid)
    assert row.first_user_message == only.request["params"]["text"] == "Queued first prompt"
