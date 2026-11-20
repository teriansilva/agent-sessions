"""#2228: a locked `federation.db` answers a typed busy response, never an unhandled 500.

Production (release b565752, 2026-11-20 10:20:50): `GET /api/federation/grants` failed with
`sqlite3.OperationalError: database is locked` at the store's version probe, as a 500. Here a
second, real SQLite connection holds the file (`BEGIN EXCLUSIVE`, which blocks readers in the
store's rollback-journal mode) while the route and the store are asked; a `BEGIN IMMEDIATE`
(reserved) lock, which still lets readers in, is the negative control.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent_sessions.federation import identity, store
from agent_sessions.routes import federation as fed_routes

SESSION = "claude-api:3a2b81b1-c409-49f8-be56-122fb6c6e917"
SAFE = "the sharing store is busy; try again"


def _client(cfg):
    from agent_sessions.main import create_app

    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg):
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303


def _db():
    return identity.state_dir() / store.DB_FILE


class _Holder:
    """Another connection to the store's file, holding a lock until released."""

    def __init__(self, mode: str) -> None:
        self.con = sqlite3.connect(
            str(_db()), timeout=0.1, isolation_level=None, check_same_thread=False
        )
        self.con.execute(f"BEGIN {mode}")

    def release(self) -> None:
        self.con.execute("ROLLBACK")
        self.con.close()


@pytest.fixture
def ready(auth_cfg, monkeypatch):
    """A signed-in client whose store exists (the first read created it), with a short request
    deadline and connection wait so a locked file answers quickly."""
    monkeypatch.setattr(fed_routes, "GRANTS_READ_DEADLINE_S", 0.4)
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 300)
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    with store.Store(identity.state_dir())._connect():
        pass  # a read creates nothing: the store's own schema path makes the file
    assert _db().exists()
    assert c.get(f"/api/federation/grants?session={SESSION}").status_code == 200
    return c


# ---- the HTTP contract -------------------------------------------------------------------------


def test_a_locked_store_answers_busy_not_500_and_recovers(ready):
    holder = _Holder("EXCLUSIVE")
    try:
        t0 = time.monotonic()
        r = ready.get(f"/api/federation/grants?session={SESSION}")
        elapsed = time.monotonic() - t0
    finally:
        holder.release()
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": SAFE, "code": "busy"}
    assert "federation" not in r.json()["detail"] and ".db" not in r.json()["detail"]
    assert elapsed < 2.0, f"the request waited {elapsed:.1f}s past its deadline"
    # Once the lock is released, the same read answers.
    again = ready.get(f"/api/federation/grants?session={SESSION}")
    assert again.status_code == 200 and again.json()["grants"] == []


def test_a_reserved_lock_still_lets_the_list_be_read(ready):
    """The negative control: a writer that has only RESERVED the file blocks no reader."""
    holder = _Holder("IMMEDIATE")
    try:
        r = ready.get(f"/api/federation/grants?session={SESSION}")
    finally:
        holder.release()
    assert r.status_code == 200, r.text


def test_the_whole_list_shares_one_deadline(ready, monkeypatch):
    """The handler makes several store reads (`listing`, `list_peers`, `note_counts`,
    `generation`). Its first read uses up most of the budget, then the file is locked: the next
    read answers busy with what is LEFT, never a fresh wait per read (independent review of
    #2228: the previous test measured only one wait)."""
    from agent_sessions.federation import grants

    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 5000)  # a fresh wait would be 5 s
    monkeypatch.setattr(fed_routes, "GRANTS_READ_DEADLINE_S", 0.6)
    real = grants.listing
    holder: list = []

    def slow_listing(st, key):
        rows = real(st, key)
        time.sleep(0.45)  # most of the budget, spent
        holder.append(_Holder("EXCLUSIVE"))  # then the file is locked for the next reads
        return rows

    monkeypatch.setattr(grants, "listing", slow_listing)
    t0 = time.monotonic()
    try:
        r = ready.get(f"/api/federation/grants?session={SESSION}")
    finally:
        for h in holder:
            h.release()
    elapsed = time.monotonic() - t0
    assert r.status_code == 503 and r.json()["code"] == "busy", r.text
    assert elapsed < 0.95, f"{elapsed:.2f}s: the later reads did not share the deadline"


def test_the_deadline_is_shared_across_store_reads(ready):
    st = _fresh_store()
    st.list_peers()
    with store.request_deadline(0.5):
        time.sleep(0.4)  # spent by earlier work in the same request
        holder = _Holder("EXCLUSIVE")
        t0 = time.monotonic()
        try:
            with pytest.raises(store.StoreBusy):
                st.list_peers()
        finally:
            holder.release()
    assert time.monotonic() - t0 < 0.35


def test_the_delivery_fence_busy_keeps_its_own_answer():
    """`store.Busy` (a grant's delivery fence) is not the file lock: its message is unchanged and
    it is not a `StoreBusy`."""
    fence_busy = store.Busy()
    assert not isinstance(fence_busy, store.StoreBusy)
    assert "delivery" in str(fence_busy) and str(store.StoreBusy()) == SAFE


def test_the_write_side_wrappers_answer_busy_too():
    """Independent review of #2228: creating, changing or revoking a grant (`grants._code`) and
    approving or rejecting a suggestion, notes (`inputs.store_code`) answer `busy` on a locked
    file, like the read; every other store failure keeps `store`."""
    from agent_sessions.federation import grants, inputs

    assert grants._code(store.StoreBusy()) == "busy" and grants._code(store.Busy()) == "busy"
    assert grants._code(store.StoreError("x")) == "store"
    assert inputs.store_code(store.StoreBusy()) == "busy"
    assert inputs.store_code(store.StoreError("x")) == "store"


# ---- the store: both probes, the read itself, and nothing else ----------------------------------


def _fresh_store() -> store.Store:
    return store.Store(identity.state_dir())


def test_the_cold_schema_probe_answers_busy(ready):
    holder = _Holder("EXCLUSIVE")
    try:
        cold = _fresh_store()  # its schema has not been checked yet
        with store.request_deadline(0.3), pytest.raises(store.StoreBusy):
            cold.list_peers()
    finally:
        holder.release()
    assert _fresh_store().list_peers() == []


def test_the_warm_version_probe_answers_busy(ready):
    warm = _fresh_store()
    assert warm.list_peers() == []  # schema checked once
    holder = _Holder("EXCLUSIVE")
    try:
        with store.request_deadline(0.3), pytest.raises(store.StoreBusy):
            warm.list_peers()
    finally:
        holder.release()
    assert warm.list_peers() == []


def test_a_lock_taken_during_the_read_answers_busy_within_the_deadline(ready):
    """`busy_timeout` is per statement: the caller's block gets what is LEFT of the deadline, not
    a fresh wait (independent review of #2228: 0.95 s on a 0.5 s deadline before)."""
    st = _fresh_store()
    st.list_peers()
    holder = None
    t0 = time.monotonic()
    try:
        with store.request_deadline(0.5), pytest.raises(store.StoreBusy):
            with st._connect() as con:
                holder = _Holder("EXCLUSIVE")  # after the version probe, before the query
                con.execute("SELECT * FROM peers").fetchall()
    finally:
        if holder is not None:
            holder.release()
    assert time.monotonic() - t0 < 0.8


def test_any_other_operational_error_is_left_as_it_is(ready):
    st = _fresh_store()
    with pytest.raises(sqlite3.OperationalError) as err:
        with st._connect() as con:
            con.execute("SELECT * FROM no_such_table")
    assert not isinstance(err.value, store.StoreBusy)
    assert not store._is_busy(sqlite3.OperationalError("disk I/O error"))
    assert store._is_busy(sqlite3.OperationalError("database is locked"))
    # SQLITE_LOCKED is a same-connection conflict (a code bug), never a retryable busy answer.
    assert not store._is_busy(sqlite3.OperationalError("database table is locked"))


def test_a_newer_schema_is_still_refused_untouched(ready):
    con = sqlite3.connect(str(_db()), isolation_level=None)
    try:
        con.execute(f"PRAGMA user_version={store.SCHEMA_VERSION + 1}")
    finally:
        con.close()
    with pytest.raises(store.StoreUnsupported):
        _fresh_store().list_peers()


def test_the_deadline_nests_and_expires(ready):
    st = _fresh_store()
    with store.request_deadline(5.0):
        with store.request_deadline(0.0), pytest.raises(store.StoreBusy):
            st.list_peers()  # the inner, earlier deadline wins
        assert st.list_peers() == []  # the outer one is restored
    assert store._remaining_s() is None


# ---- the structured view keeps its UNKNOWN fallback ---------------------------------------------


def test_peer_authors_on_a_locked_store_raise_for_the_unknown_fallback(ready):
    """`routes/structured.py` turns any failure of this read into `authors = None` (UNKNOWN),
    never an empty author map: the store must RAISE on a locked file, not answer `{}`."""
    st = _fresh_store()
    st.list_peers()
    holder = _Holder("EXCLUSIVE")
    try:
        with store.request_deadline(0.3), pytest.raises(store.StoreBusy):
            st.peer_authors(SESSION)
    finally:
        holder.release()


# ---- naming the holder: content-free timing, a waiter never named as the holder ------------------


def _slow_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "federation store:" in r.getMessage()]


def test_a_long_write_transaction_is_logged_as_its_holder(ready, monkeypatch, caplog):
    monkeypatch.setattr(store, "SLOW_HOLD_S", 0.05)
    st = _fresh_store()

    def rename_everything():  # stands in for a store method holding its write too long
        with st._write() as con:
            con.execute("SELECT 1").fetchone()
            time.sleep(0.1)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        rename_everything()
    (line,) = _slow_lines(caplog)
    assert "rename_everything held a write transaction" in line
    assert str(_db()) not in line and "SELECT" not in line


def test_a_read_transaction_is_timed_from_its_lock_not_its_wait(ready, monkeypatch, caplog):
    monkeypatch.setattr(store, "SLOW_HOLD_S", 0.05)
    st = _fresh_store()

    def note_page():
        with st._read() as con:
            con.execute("SELECT 1").fetchone()
            time.sleep(0.1)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        note_page()
    (line,) = _slow_lines(caplog)
    assert "note_page held a read transaction" in line


def test_a_blocked_reader_is_logged_as_waiting_never_as_the_holder(ready, monkeypatch, caplog):
    """Independent review of #2228: a reader that waited 1.5 s and gave up was logged as having
    "held the database" — the very misdiagnosis the timing exists to prevent."""
    monkeypatch.setattr(store, "SLOW_HOLD_S", 0.05)
    st = _fresh_store()
    st.list_peers()
    holder = _Holder("EXCLUSIVE")
    try:
        with caplog.at_level(logging.WARNING, logger=store.__name__):
            with store.request_deadline(0.3), pytest.raises(store.StoreBusy):
                st.list_peers()
    finally:
        holder.release()
    lines = _slow_lines(caplog)
    assert lines and all("held" not in line for line in lines), lines
    assert any("list_peers waited for the database" in line for line in lines), lines


def test_a_commit_waits_only_what_is_left_of_the_deadline(ready, monkeypatch):
    """`busy_timeout` is per statement (independent review of #2228): a write that spent most of
    its budget, whose COMMIT then waits for a reader's shared lock, gives up with what is LEFT —
    never a fresh wait per statement (probe, BEGIN IMMEDIATE, COMMIT)."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 5000)  # only the deadline may bound the wait
    st = _fresh_store()
    st.list_peers()
    reader = None
    t0 = time.monotonic()
    try:
        with store.request_deadline(0.6), pytest.raises(store.StoreBusy):
            with st._write() as con:
                con.execute("CREATE TABLE probe_2228 (a)")  # a real write: COMMIT needs the file
                time.sleep(0.4)
                reader = sqlite3.connect(str(_db()), isolation_level=None, check_same_thread=False)
                reader.execute("BEGIN")
                reader.execute(
                    "SELECT * FROM peers"
                ).fetchall()  # a shared lock the COMMIT waits on
    finally:
        if reader is not None:
            reader.execute("ROLLBACK")
            reader.close()
    assert time.monotonic() - t0 < 0.85
    assert st.list_peers() == []  # the store answers again
    con = sqlite3.connect(str(_db()))
    try:  # the write was rolled back, not half-committed
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='probe_2228'").fetchone()
    finally:
        con.close()


def test_a_write_past_its_deadline_still_commits_when_nothing_holds_the_file(ready):
    """Independent review of #2228: a spent budget waits 1 ms, it does not refuse — a write whose
    work ran past the deadline commits when the file is free."""
    st = _fresh_store()
    st.list_peers()
    with store.request_deadline(0.05):
        with st._write() as con:
            time.sleep(0.1)  # past the deadline, no contention
            con.execute("CREATE TABLE probe_free (a)")
    con = sqlite3.connect(str(_db()))
    try:
        assert con.execute("SELECT 1 FROM sqlite_master WHERE name='probe_free'").fetchone()
    finally:
        con.close()


def _v4_store(directory) -> store.Store:
    """A real store file at schema 4 (the steps a v4 build ran), in its own directory."""
    directory.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(directory / store.DB_FILE), isolation_level=None)
    try:
        for statements in (store._SCHEMA_V1, store._SCHEMA_V2, store._SCHEMA_V3, store._SCHEMA_V4):
            for stmt in statements:
                con.execute(stmt)
        con.execute("PRAGMA user_version=4")
    finally:
        con.close()
    return store.Store(directory)


def test_the_cold_migration_backup_answers_busy_within_the_deadline(tmp_path, monkeypatch):
    """Hermes on #2236: on an older store the cold path takes a BACKUP before migrating, and
    SQLite's backup retries a busy source with no end of its own. A writer that locks the file
    after the cold version probe must not hold the request past its deadline; nothing partial is
    left behind, and the migration completes once the file is free."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 5000)  # only the deadline may bound the wait
    directory = tmp_path / "fed"
    st = _v4_store(directory)
    db = directory / store.DB_FILE
    holders: list[sqlite3.Connection] = []
    real_backup = store._backup

    def locked_after_the_probe(p, version):
        h = sqlite3.connect(str(db), timeout=0.1, isolation_level=None, check_same_thread=False)
        h.execute("BEGIN EXCLUSIVE")  # a writer, between the cold probe and the backup
        holders.append(h)
        return real_backup(p, version)

    monkeypatch.setattr(store, "_backup", locked_after_the_probe)
    t0 = time.monotonic()
    try:
        with store.request_deadline(0.15), pytest.raises(store.StoreBusy):
            st.list_peers()
        elapsed = time.monotonic() - t0
        assert not list(directory.glob(f"{store.DB_FILE}.bak-*")), "a partial backup was left"
    finally:
        for h in holders:
            h.execute("ROLLBACK")
            h.close()
    assert elapsed < 0.6, f"{elapsed:.2f}s: the backup waited past the request deadline"
    monkeypatch.setattr(store, "_backup", real_backup)
    assert st.list_peers() == []  # the file is free: the migration completes
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    finally:
        con.close()
    assert len(list(directory.glob(f"{store.DB_FILE}.bak-v4-*"))) == 1


def test_the_migration_backup_finishes_under_steady_writes(tmp_path):
    """Independent review of #2228: a stepwise backup (`pages=64`) restarts on every write by
    another connection and, with no deadline, never finished under a writer inserting every
    2 ms. The whole-file backup completes, and the copy is sound."""
    import threading

    directory = tmp_path / "fed"
    st = _v4_store(directory)
    db = directory / store.DB_FILE
    con = sqlite3.connect(str(db), isolation_level=None)
    try:
        con.execute("CREATE TABLE ballast (b BLOB)")
        con.executemany("INSERT INTO ballast VALUES (?)", [(b"x" * 4000,) for _ in range(750)])
    finally:
        con.close()
    stop = threading.Event()

    def writer():
        w = sqlite3.connect(str(db), isolation_level=None, timeout=5)
        try:
            while not stop.is_set():
                w.execute("INSERT INTO ballast VALUES (x'00')")
                time.sleep(0.002)
        finally:
            w.close()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        done = threading.Event()
        out: list = []

        def migrate():
            out.append(st.list_peers())
            done.set()

        threading.Thread(target=migrate, daemon=True).start()
        assert done.wait(10), "the migration backup never finished under steady writes"
    finally:
        stop.set()
        t.join(5)
    assert out == [[]]
    (bak,) = list(directory.glob(f"{store.DB_FILE}.bak-v4-*"))
    check = sqlite3.connect(f"file:{bak}?mode=ro", uri=True)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
