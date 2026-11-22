"""#2273: a stalled liveness commit no longer locks every federation reader out.

Production (release 6a74ba2, 2026-11-22 06:01–06:06): ``touch_seen`` (one ``UPDATE peers SET
last_seen_at``, written on every keepalive ping) held a write transaction for up to 55 s while
host storage stalled, and in rollback-journal mode every reader (``list_peers``,
``live_grants_of``, the grants list) waited and answered busy. A real second connection holding
an EXCLUSIVE lock stands in for the stalled commit here: it is what a committing writer holds,
and in rollback mode it blocks readers. It does not inject an fsync fault; what it shows is reader
isolation. A RESERVED holder, which never blocks readers, is the negative control.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_sessions.federation import noise, store

SESSION = "claude-api:3a2b81b1-c409-49f8-be56-122fb6c6e917"
WAL_KEY = "AGENT_SESSIONS_FEDERATION_WAL"  # the operator's documented key, spelled out


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    """A lock wait that ends in a fraction of a second, so a blocked read fails fast."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 300)
    monkeypatch.delenv(WAL_KEY, raising=False)
    # Each test is a fresh process as far as the store's per-process memory of a failed switch
    # goes; a test that means "the next process" clears it itself.
    monkeypatch.setattr(store, "_JOURNAL_FAILED", set())


def _rollback_mode(monkeypatch) -> None:
    monkeypatch.setenv(WAL_KEY, "0")


def _mode(db) -> str:
    con = sqlite3.connect(str(db))
    try:
        return str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        con.close()


def _peer(st: store.Store, name: str = "box") -> store.Peer:
    return st.trust(noise.public_bytes(X25519PrivateKey.generate()), name=name, endpoints=[])


def _populated(directory) -> tuple[store.Store, store.Peer]:
    st = store.Store(directory)
    peer = _peer(st)
    st.create_grant(
        session_key=SESSION,
        peer_id=peer.peer_id,
        level="view",
        expires_at=time.time() + 3600,
        kind=store.KIND_API,
    )
    return st, peer


class _Holder:
    """Another connection to the store's file, holding a lock until released."""

    def __init__(self, db, mode: str) -> None:
        self.con = sqlite3.connect(
            str(db), timeout=0.1, isolation_level=None, check_same_thread=False
        )
        self.con.execute(f"BEGIN {mode}")
        if mode == "IMMEDIATE":  # a writer with an uncommitted change: RESERVED, not more
            self.con.execute("UPDATE peers SET last_seen_at = 1")

    def release(self) -> None:
        self.con.execute("ROLLBACK")
        self.con.close()


def _crashed_writer(db, rows: list[str]) -> None:
    """Commit ``rows`` as peer names in another process that then dies without closing, so the
    commits stay in ``-wal``, never checkpointed into the main file."""
    code = (
        "import os, sqlite3, sys\n"
        "con = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "con.execute('PRAGMA wal_autocheckpoint=0')\n"
        "for n in sys.argv[2:]:\n"
        '    con.execute("INSERT INTO peers (peer_id, public_key, name, state, endpoints,'
        " revision, created_at, updated_at) VALUES (?, ?, ?, 'trusted', '[]', 1, 0, 0)\","
        " (n, n.encode(), n))\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code, str(db), *rows], check=True, timeout=30)
    assert (db.parent / (db.name + "-wal")).stat().st_size > 0


# ---- read isolation ---------------------------------------------------------------------------


def test_readers_read_committed_rows_while_a_writer_holds_the_commit_lock(tmp_path):
    st, peer = _populated(tmp_path / "fed")
    holder = _Holder(st.db_path, "EXCLUSIVE")
    try:
        # WHILE the lock is held, not after: the committed rows, within the request budget.
        started = time.monotonic()
        with store.request_deadline(1.0):
            assert [p.peer_id for p in st.list_peers()] == [peer.peer_id]
            assert [g.peer_id for g in st.list_grants(session_key=SESSION)] == [peer.peer_id]
        assert time.monotonic() - started < 1.0
    finally:
        holder.release()


def test_in_rollback_mode_the_same_lock_still_blocks_readers(tmp_path, monkeypatch):
    """What the operator's ``=0`` switch keeps: the old behaviour, answered busy (#2236)."""
    _rollback_mode(monkeypatch)
    st, _ = _populated(tmp_path / "fed")
    assert _mode(st.db_path) == "delete"
    holder = _Holder(st.db_path, "EXCLUSIVE")
    try:
        with pytest.raises(store.StoreBusy):
            st.list_peers()
    finally:
        holder.release()


@pytest.mark.parametrize("wal", [True, False])
def test_a_reserved_writer_never_blocked_readers_in_either_mode(tmp_path, monkeypatch, wal):
    if not wal:
        _rollback_mode(monkeypatch)
    st, peer = _populated(tmp_path / "fed")
    holder = _Holder(st.db_path, "IMMEDIATE")
    try:
        assert [p.peer_id for p in st.list_peers()] == [peer.peer_id]
    finally:
        holder.release()


# ---- the journal mode -------------------------------------------------------------------------


def test_a_new_store_is_wal_private_and_durable_on_every_connection(tmp_path):
    assert store.WAL_KEY == WAL_KEY
    st, _ = _populated(tmp_path / "fed")
    assert _mode(st.db_path) == "wal"
    holder = _Holder(st.db_path, "IMMEDIATE")  # keeps -wal and -shm on disk to look at
    try:
        for suffix in ("", "-wal", "-shm"):
            path = st.db_path.with_name(st.db_path.name + suffix)
            assert oct(path.stat().st_mode & 0o777) == "0o600", suffix
    finally:
        holder.release()


def test_every_later_connection_is_made_durable_whatever_the_default(tmp_path, monkeypatch):
    """This SQLite's compiled default is already FULL, so the default is forced down first: what
    reports FULL afterwards is the store's own setting, on read and write connections alike."""
    st, _ = _populated(tmp_path / "fed")
    real = store._raw_connect

    def weak(p):
        con = real(p)
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA journal_size_limit=-1")
        return con

    monkeypatch.setattr(store, "_raw_connect", weak)
    with st._connect() as con:
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert con.execute("PRAGMA journal_size_limit").fetchone()[0] == store.WAL_SIZE_LIMIT_BYTES
    with st._write() as con:
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "setconfig"), reason="Python 3.11 has no setconfig"
)
def test_closing_the_last_connection_never_checkpoints(tmp_path):
    """The independent review of #2273: closing the LAST connection to a WAL file checkpoints it
    under an EXCLUSIVE lock (two fsyncs), which would lock readers out exactly as the stalled
    commit did. Store connections never checkpoint on close: the WAL outlives them."""
    st, peer = _populated(tmp_path / "fed")
    wal = st.db_path.with_name(st.db_path.name + "-wal")
    st.touch_seen(peer.peer_id)  # a write, and its connection closed: no other connection open
    assert wal.exists() and wal.stat().st_size > 0
    with st._connect() as con:
        assert con.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is True
    # A reader opened afterwards still reads what the WAL holds.
    assert st.get(peer.peer_id).last_seen_at is not None


def test_the_wal_stays_bounded_over_many_writes_with_no_other_connection_open(
    tmp_path, monkeypatch
):
    """Independent review, round 2: with no connection kept open between operations, each new one
    rebuilt the WAL index and forgot what was checkpointed, so the WAL never restarted and grew
    with every write (24.7 MB after 6000). The process's idle anchor keeps the index; the WAL
    restarts after each autocheckpoint and is trimmed to the limit. Scaled down here."""
    monkeypatch.setattr(store, "WAL_AUTOCHECKPOINT_PAGES", 20)
    monkeypatch.setattr(store, "WAL_SIZE_LIMIT_BYTES", 32 * 1024)
    st, peer = _populated(tmp_path / "fed")
    wal = st.db_path.with_name(st.db_path.name + "-wal")
    for n in range(400):
        with st._write() as con:
            con.execute("UPDATE peers SET last_seen_at = ? WHERE peer_id = ?", (n, peer.peer_id))
    # The limit plus at most one checkpoint interval of frames (a page and its frame header each).
    bound = 32 * 1024 + (20 + 5) * (4096 + 24)
    assert wal.stat().st_size <= bound
    assert st.get(peer.peer_id).last_seen_at == 399


def test_an_existing_current_schema_file_converts_and_keeps_every_row(tmp_path, monkeypatch):
    directory = tmp_path / "fed"
    _rollback_mode(monkeypatch)
    old, peer = _populated(directory)
    assert _mode(old.db_path) == "delete"
    monkeypatch.delenv(WAL_KEY)
    st = store.Store(directory)  # the next process: its cold path, at schema 5 already
    assert [p.peer_id for p in st.list_peers()] == [peer.peer_id]
    assert _mode(st.db_path) == "wal"
    assert [g.peer_id for g in st.list_grants(session_key=SESSION)] == [peer.peer_id]


def test_a_newer_schema_is_refused_and_its_journal_left_alone(tmp_path, monkeypatch):
    directory = tmp_path / "fed"
    _rollback_mode(monkeypatch)
    old, _ = _populated(directory)
    con = sqlite3.connect(str(old.db_path))
    con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    con.close()
    monkeypatch.delenv(WAL_KEY)
    with pytest.raises(store.StoreUnsupported):
        store.Store(directory).list_peers()
    assert _mode(old.db_path) == "delete"


def test_reading_a_store_that_was_never_used_still_creates_nothing(tmp_path):
    st = store.Store(tmp_path / "fed")
    assert st.list_grants() == []
    assert not (tmp_path / "fed").exists()


def test_a_switch_that_cannot_get_its_lock_keeps_the_mode_and_the_next_start_retries(
    tmp_path, monkeypatch, caplog
):
    directory = tmp_path / "fed"
    _rollback_mode(monkeypatch)
    old, peer = _populated(directory)
    monkeypatch.delenv(WAL_KEY)
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 5000)  # only the switch's budget may bound it
    monkeypatch.setattr(store, "JOURNAL_SWITCH_BUSY_MS", 1000)
    reader = sqlite3.connect(str(old.db_path), isolation_level=None, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM peers").fetchall()  # an open read: the switch can't proceed
    try:
        caplog.set_level(logging.WARNING, logger=store.__name__)
        st = store.Store(directory)
        started = time.monotonic()
        assert [p.peer_id for p in st.list_peers()] == [peer.peer_id]  # served, in the old mode
        assert time.monotonic() - started < 2.5  # the switch's 1 s, never the store's 5 s
        assert _mode(old.db_path) == "delete"
        said = [r.getMessage() for r in caplog.records if "journal" in r.getMessage()]
        assert said == [said[0]] and "kept the delete journal (SQLITE_BUSY" in said[0]  # one line
        # Another Store over the same file in the same process does not wait again.
        started = time.monotonic()
        store.Store(directory).list_peers()
        assert time.monotonic() - started < 0.5
    finally:
        reader.execute("ROLLBACK")
        reader.close()
    store.Store(directory).list_peers()  # still this process: no retry
    assert _mode(old.db_path) == "delete"
    monkeypatch.setattr(store, "_JOURNAL_FAILED", set())  # the next process
    store.Store(directory).list_peers()
    assert _mode(old.db_path) == "wal"


def test_a_switch_that_fails_for_another_reason_keeps_the_working_mode(
    tmp_path, monkeypatch, caplog
):
    """A file that worked in rollback mode keeps working if WAL can't be had there (a filesystem
    without shared memory, say): the switch failing is logged, never fatal."""
    directory = tmp_path / "fed"
    _rollback_mode(monkeypatch)
    _, peer = _populated(directory)
    monkeypatch.delenv(WAL_KEY)

    class NoWal(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, *args)

    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=NoWal, **k))
    caplog.set_level(logging.WARNING, logger=store.__name__)
    st = store.Store(directory)
    assert [p.peer_id for p in st.list_peers()] == [peer.peer_id]
    assert _mode(st.db_path) == "delete"
    assert any(
        "kept the delete journal (OperationalError)" in r.getMessage() for r in caplog.records
    )


# ---- backup, restore, rollback ----------------------------------------------------------------


def _v4_wal_store(directory) -> sqlite3.Connection:
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / store.DB_FILE
    con = sqlite3.connect(str(db), isolation_level=None)
    for statements in (store._SCHEMA_V1, store._SCHEMA_V2, store._SCHEMA_V3, store._SCHEMA_V4):
        for stmt in statements:
            con.execute(stmt)
    con.execute("PRAGMA user_version=4")
    assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    con.close()
    return db


def test_the_migration_backup_holds_rows_that_were_only_in_the_wal(tmp_path):
    directory = tmp_path / "fed"
    db = _v4_wal_store(directory)
    _crashed_writer(db, ["only-in-the-wal"])
    store.Store(directory).list_peers()  # the cold migration: backup first, then v5
    (bak,) = directory.glob(f"{store.DB_FILE}.bak-v4-*")
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy2(bak, alone / "copy.db")  # on its own: no -wal beside it
    con = sqlite3.connect(str(alone / "copy.db"))
    try:
        names = [r[0] for r in con.execute("SELECT name FROM peers")]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        con.close()
    assert names == ["only-in-the-wal"]


def test_restoring_a_backup_is_moving_the_three_files_aside_and_the_copy_in(tmp_path):
    """The documented procedure (docs/invariants/federation.md), with the service stopped."""
    directory = tmp_path / "fed"
    db = _v4_wal_store(directory)
    con = sqlite3.connect(str(db), isolation_level=None)
    assert con.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"  # an older file
    con.execute(
        "INSERT INTO peers (peer_id, public_key, name, state, endpoints, revision, created_at,"
        " updated_at) VALUES ('kept', x'01', 'kept', 'trusted', '[]', 1, 0, 0)"
    )
    con.close()
    st = store.Store(directory)
    _peer(st, "after-the-backup")
    (bak,) = directory.glob(f"{store.DB_FILE}.bak-v4-*")
    assert open(bak, "rb").read(20)[18:20] == b"\x01\x01"  # a rollback-mode copy
    assert _mode(db) == "wal"
    store.close_anchors()  # the service is stopped: no connection of it is open any more
    aside = tmp_path / "aside"
    aside.mkdir()
    for suffix in ("", "-wal", "-shm"):
        path = db.with_name(db.name + suffix)
        if path.exists():
            path.rename(aside / path.name)
    shutil.copy2(bak, db)
    assert _mode(db) == "delete"
    restored = store.Store(directory)
    assert [p.name for p in restored.list_peers()] == ["kept"]
    assert _mode(db) == "wal"  # converted by the restored store's first start


def test_the_operator_switch_converts_back_and_folds_the_wal_in(tmp_path, monkeypatch):
    directory = tmp_path / "fed"
    st, peer = _populated(directory)
    _crashed_writer(st.db_path, ["uncheckpointed"])
    _rollback_mode(monkeypatch)
    names = {p.name for p in store.Store(directory).list_peers()}  # the next process
    assert names == {peer.name, "uncheckpointed"}
    assert _mode(st.db_path) == "delete"
    assert not st.db_path.with_name(st.db_path.name + "-wal").exists()


def test_the_operator_switch_is_read_from_the_install_env_file(tmp_path, monkeypatch):
    env = tmp_path / "install.env"
    env.write_text(f"{WAL_KEY}=0\n")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(env))
    st, _ = _populated(tmp_path / "fed")
    assert _mode(st.db_path) == "delete"
    env.write_text(f"{WAL_KEY}=1\n")  # any other value means WAL
    store.Store(tmp_path / "fed").list_peers()
    assert _mode(st.db_path) == "wal"


def test_an_older_build_reads_and_writes_a_converted_store(tmp_path):
    """An older build sets no journal pragma: SQLite keeps the file's WAL mode for it."""
    st, peer = _populated(tmp_path / "fed")
    con = sqlite3.connect(str(st.db_path))
    try:
        assert [r[0] for r in con.execute("SELECT peer_id FROM peers")] == [peer.peer_id]
        con.execute("UPDATE peers SET name = 'renamed' WHERE peer_id = ?", (peer.peer_id,))
        con.commit()
    finally:
        con.close()
    assert st.get(peer.peer_id).name == "renamed"


# ---- touch_seen -------------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _seen(st, peer_id):
    return st.get(peer_id).last_seen_at


def _touchable(tmp_path):
    st = store.Store(tmp_path / "fed")
    peer = _peer(st)
    clock = _Clock()
    st._touch_clock = clock
    return st, peer, clock


def test_touch_writes_at_most_once_per_interval(tmp_path):
    st, peer, clock = _touchable(tmp_path)
    assert st.touch_seen(peer.peer_id) is True
    first = _seen(st, peer.peer_id)
    clock.t += store.TOUCH_EVERY_S - 1
    assert st.touch_seen(peer.peer_id) is False
    assert _seen(st, peer.peer_id) == first
    clock.t += 1
    assert st.touch_seen(peer.peer_id) is True
    assert _seen(st, peer.peer_id) > first


def test_a_busy_touch_is_dropped_backs_off_and_keeps_its_last_success(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(store, "TOUCH_DEADLINE_S", 0.2)
    st, peer, clock = _touchable(tmp_path)
    assert st.touch_seen(peer.peer_id) is True
    clock.t += store.TOUCH_EVERY_S
    holder = _Holder(st.db_path, "EXCLUSIVE")  # a writer: busy for another writer in WAL too
    try:
        caplog.set_level(logging.WARNING, logger=store.__name__)
        started = time.monotonic()
        assert st.touch_seen(peer.peer_id) is False  # dropped, never raised
        assert time.monotonic() - started < 1.0  # its own lock budget
        assert any("touch_seen met a busy file" in r.getMessage() for r in caplog.records)
    finally:
        holder.release()
    clock.t += store.TOUCH_RETRY_S - 1
    assert st.touch_seen(peer.peer_id) is False  # backing off, not retrying every ping
    clock.t += 1
    assert st.touch_seen(peer.peer_id) is True  # due: the busy try never counted as a write


def test_a_reconnect_writes_at_once_whatever_the_clocks_say(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "TOUCH_DEADLINE_S", 0.2)
    st, peer, clock = _touchable(tmp_path)
    assert st.touch_seen(peer.peer_id) is True
    clock.t += 1
    assert st.touch_seen(peer.peer_id, reconnect=True) is True  # inside the interval
    clock.t += store.TOUCH_EVERY_S
    holder = _Holder(st.db_path, "EXCLUSIVE")
    try:
        assert st.touch_seen(peer.peer_id) is False
    finally:
        holder.release()
    assert st.touch_seen(peer.peer_id, reconnect=True) is True  # inside the backoff


def test_a_touch_that_fails_for_any_other_reason_still_raises(tmp_path, monkeypatch):
    st, peer, _ = _touchable(tmp_path)

    def broken():
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(st, "_write", broken)
    with pytest.raises(sqlite3.DatabaseError):
        st.touch_seen(peer.peer_id)


def test_a_replaced_file_gets_a_fresh_anchor(tmp_path):
    """An anchor follows the FILE: one replaced under the process (another inode) is anchored
    again by the next cold path, and the old anchor is closed."""
    directory = tmp_path / "fed"
    st, _ = _populated(directory)
    key = os.path.realpath(st.db_path)
    first_ino, first = store._ANCHORS[key]
    assert first_ino == os.stat(st.db_path).st_ino
    replacement = tmp_path / "replacement.db"
    con = sqlite3.connect(str(st.db_path))
    con.execute(f"VACUUM INTO '{replacement}'")  # a complete copy: a new file, a new inode
    con.close()
    for suffix in ("-wal", "-shm"):  # the documented way: never the main file on its own
        side = st.db_path.with_name(st.db_path.name + suffix)
        if side.exists():
            side.rename(tmp_path / side.name)
    os.replace(replacement, st.db_path)
    assert [p.name for p in store.Store(directory).list_peers()] == ["box"]
    ino, now = store._ANCHORS[key]
    assert ino == os.stat(st.db_path).st_ino != first_ino
    assert now is not first
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")  # closed


def test_an_anchor_never_creates_a_file_and_keeps_to_the_request_budget(tmp_path, monkeypatch):
    missing = tmp_path / store.DB_FILE
    real_stat = os.stat
    seen = real_stat(tmp_path)

    def stat(path, *args, **kwargs):  # the file was there when looked at, gone when opened
        return (
            seen
            if os.fspath(path) == os.path.realpath(missing)
            else real_stat(path, *args, **kwargs)
        )

    with monkeypatch.context() as m:  # scoped: never undo the conftest pins with it
        m.setattr(os, "stat", stat)
        store._anchor(missing, "wal")
    assert not missing.exists()
    assert os.path.realpath(missing) not in store._ANCHORS
    st, _ = _populated(tmp_path / "fed")
    store.close_anchors()
    with store.request_deadline(0.0):  # spent: no anchor attempt waits at all
        store._anchor(st.db_path, "wal")
    assert os.path.realpath(st.db_path) not in store._ANCHORS
    store._anchor(st.db_path, "wal")
    assert os.path.realpath(st.db_path) in store._ANCHORS
