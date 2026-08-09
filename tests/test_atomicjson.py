"""Crash-safety of the shared JSON store helper and its callers (#728).

The two defects these pin are both *windows*, not steady states, so every test here forces the
window open deliberately rather than hoping to land in it:

* a write that fails partway used to leave ``prefs.json`` truncated or empty — and it carries
  the operator's AI-review API key, so that is credential loss, not a lost UI preference;
* an unlocked reader inside that window parsed the empty file and returned **defaults**, so a
  read concurrent with any save could report the AI review disabled and unconfigured.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from agent_sessions import atomicjson, prefs
from agent_sessions import orchestrator_ledger as ledger


def _temps(d) -> list[str]:
    """Leftover temp files in ``d``. The name is unique per write, so this globs the suffix."""
    return sorted(f.name for f in d.iterdir() if f.name.endswith(atomicjson.TMP_SUFFIX))


# --- the helper itself ---------------------------------------------------------------------


def test_write_is_atomic_and_owner_only(tmp_path):
    p = tmp_path / "store.json"
    atomicjson.atomic_write_json(p, {"b": 2, "a": 1})
    assert json.loads(p.read_text()) == {"a": 1, "b": 2}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_previous_document_intact(tmp_path):
    """The core guarantee. The old shape truncated first, so this scenario emptied the file."""
    p = tmp_path / "store.json"
    atomicjson.atomic_write_json(p, {"api_key": "sk-secret", "theme": "light"})

    real_write = os.write
    calls = {"n": 0}

    def exploding_write(fd, data):
        calls["n"] += 1
        # Let the first chunk through, then fail — the shape of a disk filling up mid-save.
        if calls["n"] > 1:
            raise OSError("no space left on device")
        return real_write(fd, data[:1])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomicjson.os, "write", exploding_write)
        with pytest.raises(OSError):
            atomicjson.atomic_write_json(p, {"api_key": "sk-secret", "theme": "dark"})

    assert json.loads(p.read_text()) == {"api_key": "sk-secret", "theme": "light"}
    # …and no half-written temp is left lying next to it.
    assert _temps(tmp_path) == []


def test_an_unencodable_document_never_reaches_the_file(tmp_path):
    """Serialising before touching disk is what makes this a no-op rather than data loss."""
    p = tmp_path / "store.json"
    atomicjson.atomic_write_json(p, {"keep": "me"})
    with pytest.raises(TypeError):
        atomicjson.atomic_write_json(p, {"bad": object()})
    assert json.loads(p.read_text()) == {"keep": "me"}


def test_the_secret_is_never_on_disk_world_readable_even_briefly(tmp_path):
    """The window, not the end state (Hermes on #811).

    A fixed `<name>.tmp` opened with `O_CREAT` inherits a stale file's mode, and chmod-ing after
    the payload lands is too late — the API key has already been written into a 0644 file. So the
    mode is asserted DURING the write: every byte of the document must go into a 0600 inode.
    """
    p = tmp_path / "store.json"
    # A stale, world-readable leftover at the name the old implementation would have reused.
    stale = tmp_path / ("store.json" + atomicjson.TMP_SUFFIX)
    stale.write_text("{}")
    os.chmod(stale, 0o644)

    real_write = os.write
    modes: list[int] = []

    def watching_write(fd, data):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_write(fd, data)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomicjson.os, "write", watching_write)
        atomicjson.atomic_write_json(p, {"api_key": "sk-secret"})

    assert modes, "the document was never written"
    assert set(modes) == {0o600}, f"the secret was written into a {oct(max(modes))} file"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    # The stale file was not adopted, and is not what got published.
    assert stale.read_text() == "{}"


def test_concurrent_writers_do_not_race_on_one_temp_inode(tmp_path):
    """A fixed temp name is shared state: one writer's `os.replace` moved the file out from
    under the other, which raised `FileNotFoundError` — and the caller that reported success had
    not written the document that ended up on disk. Unique temps remove the shared inode."""
    p = tmp_path / "store.json"
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(n: int):
        try:
            start.wait(5)
            for _ in range(25):
                atomicjson.atomic_write_json(p, {"writer": n})
        except BaseException as e:  # noqa: BLE001 - collected and asserted on below
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == [], f"concurrent writers raced: {errors!r}"
    # Whoever wrote last, the published document is one complete document — never a mixture.
    assert json.loads(p.read_text()).keys() == {"writer"}
    assert _temps(tmp_path) == [], "temp files leaked"


def test_a_failed_directory_fsync_is_not_reported_as_a_successful_save(tmp_path):
    """Swallowing it reports a crash-durable save that isn't one, and the caller cannot tell."""
    p = tmp_path / "store.json"
    atomicjson.atomic_write_json(p, {"v": 1})
    with pytest.MonkeyPatch.context() as mp:

        def boom(_d):
            raise OSError("directory fsync refused")

        mp.setattr(atomicjson, "fsync_dir", boom)
        with pytest.raises(OSError, match="directory fsync refused"):
            atomicjson.atomic_write_json(p, {"v": 2})


def test_the_write_lock_is_a_sidecar_not_the_document(tmp_path):
    """``flock`` is per-inode and ``os.replace`` installs a new one, so a lock held on the
    document stops excluding anyone the moment the first atomic write lands."""
    p = tmp_path / "store.json"
    with atomicjson.json_write_lock(p):
        atomicjson.atomic_write_json(p, {"a": 1})
    assert (tmp_path / ("store.json" + atomicjson.LOCK_SUFFIX)).exists()


def test_the_write_lock_actually_excludes_a_second_writer(tmp_path):
    p = tmp_path / "store.json"
    order: list[str] = []
    inside = threading.Event()
    release = threading.Event()

    def first():
        with atomicjson.json_write_lock(p):
            order.append("first-in")
            inside.set()
            release.wait(5)
            order.append("first-out")

    def second():
        inside.wait(5)
        with atomicjson.json_write_lock(p):
            order.append("second-in")

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    t2.start()
    # Give `second` time to block on the lock, then let `first` finish.
    time.sleep(0.1)
    release.set()
    t1.join(5)
    t2.join(5)
    assert order == ["first-in", "first-out", "second-in"]


# --- prefs.json ----------------------------------------------------------------------------


def test_a_failed_pref_save_does_not_erase_the_api_key(tmp_path):
    """The concrete loss this issue is about: `_set` used to truncate before rewriting."""
    p = tmp_path / "prefs.json"
    prefs.set_ai_review({"enabled": True, "api_key": "sk-live-key"}, p)

    real_write = os.write
    calls = {"n": 0}

    def exploding_write(fd, data):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("no space left on device")
        return real_write(fd, data[:1])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomicjson.os, "write", exploding_write)
        with pytest.raises(OSError):
            prefs.set_theme("light", p)

    assert prefs.get_ai_review(p)["api_key"] == "sk-live-key"


def test_a_concurrent_reader_never_observes_defaults(tmp_path):
    """A reader landing in the old truncate window parsed an empty file and reported the
    DEFAULT theme — an answer the caller then acts on. Atomic replace removes the window."""
    p = tmp_path / "prefs.json"
    prefs.set_theme("light", p)

    stop = threading.Event()
    seen: list[str] = []

    def reader():
        while not stop.is_set():
            seen.append(prefs.get_theme(p))

    t = threading.Thread(target=reader)
    t.start()
    try:
        for _ in range(300):
            prefs.set_theme("light", p)
    finally:
        stop.set()
        t.join(5)

    assert seen, "reader never ran"
    # `dark` is the default — observing it means a read fell into a write window.
    assert set(seen) == {"light"}, f"reader saw {sorted(set(seen))} — a torn read returned defaults"


# --- orchestrator ledger -------------------------------------------------------------------


def test_ledger_creation_syncs_the_parent_directory(tmp_path, monkeypatch):
    """``fsync(fd)`` makes the bytes durable; the directory entry naming them is a separate
    change, and without this the first record can be absent after power loss."""
    p = tmp_path / "led.jsonl"
    synced: list[str] = []
    monkeypatch.setattr(ledger, "fsync_dir", lambda d: synced.append(str(d)))

    ledger.append({"id": "a1", "state": "proposed"}, p)
    assert synced == [str(tmp_path)], "creation did not sync the directory"

    synced.clear()
    ledger.append({"id": "a2", "state": "proposed"}, p)
    assert synced == [], "an append into an existing name should not re-sync the directory"


def test_ledger_compaction_syncs_the_rename(tmp_path, monkeypatch):
    p = tmp_path / "led.jsonl"
    ledger.append({"id": "a1", "state": "delivered"}, p)
    ledger.append({"id": "a2", "state": "proposed"}, p)

    synced: list[str] = []
    monkeypatch.setattr(ledger, "fsync_dir", lambda d: synced.append(str(d)))
    kept = ledger.compact(p)
    assert kept == 2
    assert synced == [str(tmp_path)], "the compaction rename was not made durable"
