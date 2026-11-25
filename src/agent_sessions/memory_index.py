"""One rebuildable Memory retrieval engine: SQLite FTS5/BM25 + configured Lance ANN (#2036).

SQLite notebooks remain authoritative. This derived SQLite database owns searchable chunks,
scope filters, receipts and purge work; Lance stores vectors only. Every result is resolved from
the source before it leaves. Ordinary reads never create/migrate stores or walk the corpus.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import hashlib
import importlib.util
import logging
import os
import platform
import re
import sqlite3
import threading
import time
import warnings
from datetime import timedelta
from pathlib import Path

from . import memory, memory_embeddings, memory_settings, projects, template_secrets
from . import memory_service as service

log = logging.getLogger(__name__)
BATCH_NOTES = 64
INTERVAL_S = 15.0
PURGE_DEADLINE_S = 24 * 3600
FORMAT = "3"
_DB_LOCK = threading.Lock()
_DB_PID = os.getpid()
_DB_CACHE = None
# Keep ordinary question grammar from giving almost every note a lexical vote in RRF. Negation,
# domain terms and identifiers remain searchable. Non-English text still uses unicode61.
_STOP_WORDS = frozenset(
    "a an the and or of to in on at by for with from as is are was were be been being "
    "i me my we our you your it its this that these those do does did can could may "
    "would should will what which who when where why how if then than into while".split()
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scopes(
 scope TEXT PRIMARY KEY,cursor TEXT NOT NULL DEFAULT '',base_seq INTEGER NOT NULL DEFAULT 0,
 watermark INTEGER NOT NULL DEFAULT 0,complete INTEGER NOT NULL DEFAULT 0,
 notes INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,slug TEXT NOT NULL,revision INTEGER NOT NULL,
 generation INTEGER NOT NULL,content_digest TEXT NOT NULL,chunk INTEGER NOT NULL,
 offset INTEGER NOT NULL,text TEXT NOT NULL,chunk_text TEXT NOT NULL,scope_token TEXT NOT NULL,
 dense_generation TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS document_scope ON documents(scope,slug);
CREATE VIEW IF NOT EXISTS search_content AS
 SELECT rowid AS rowid,scope_token,text FROM documents WHERE chunk=0;
CREATE VIRTUAL TABLE IF NOT EXISTS terms USING fts5(
 scope_token,text,content='search_content',content_rowid='rowid',tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS vocabulary USING fts5vocab(terms,'row');
CREATE TRIGGER IF NOT EXISTS document_insert AFTER INSERT ON documents WHEN NEW.chunk=0 BEGIN
 INSERT INTO terms(rowid,scope_token,text) VALUES(NEW.rowid,NEW.scope_token,NEW.text);
END;
CREATE TRIGGER IF NOT EXISTS document_delete AFTER DELETE ON documents WHEN OLD.chunk=0 BEGIN
 INSERT INTO terms(terms,rowid,scope_token,text)
 VALUES('delete',OLD.rowid,OLD.scope_token,OLD.text);
END;
CREATE TRIGGER IF NOT EXISTS document_update AFTER UPDATE OF text ON documents
 WHEN NEW.chunk=0 BEGIN
 INSERT INTO terms(terms,rowid,scope_token,text)
 VALUES('delete',OLD.rowid,OLD.scope_token,OLD.text);
 INSERT INTO terms(rowid,scope_token,text) VALUES(NEW.rowid,NEW.scope_token,NEW.text);
END;
CREATE TABLE IF NOT EXISTS vector_deletes(
 scope TEXT NOT NULL,slug TEXT NOT NULL,PRIMARY KEY(scope,slug));
"""


def root() -> Path:
    return memory.root() / ".search"


def _prepare():
    memory._ensure_root()
    with memory._dirfd(memory.root()) as fd:
        os.close(memory._open_dir(".search", fd, create=True))
    for name in ("writer.lock", "gc.lock"):
        os.close(
            os.open(root() / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        )


def _meta(conn, key, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def _set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))


def _connect(*, write=False):
    path = root() / "state.db"
    if write:
        _prepare()
        with contextlib.suppress(FileExistsError):
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
        conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    else:
        if not path.is_file():
            raise memory.MemoryStoreError("memory search is indexing", 503)
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None
        )
    conn.row_factory = sqlite3.Row
    try:
        # Share read-only index pages through the OS cache instead of copying each broad match
        # through a connection's tiny page cache. GC's exclusive lease orders VACUUM/removal.
        conn.execute("PRAGMA mmap_size=268435456")
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        version = _meta(conn, "format") if exists else ""
        if (write and version not in {"", "1", "2", FORMAT}) or (not write and version != FORMAT):
            raise memory.MemoryStoreError(
                "memory index is rebuilding or needs a newer release", 503
            )
        if write:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        return conn
    except BaseException:
        conn.close()
        raise


def status():
    try:
        with contextlib.closing(_connect()) as conn:
            purge = float(_meta(conn, "purge_pending", "0") or "0")
            cfg = memory_settings.stored()
            state = _meta(conn, "state", "indexing")
            if state in {"ready", "keyword-only"}:
                if not cfg["base_url"] or not cfg["model"] or not vector_support()["available"]:
                    state = "keyword-only"
                elif _meta(conn, "dense_revision") != cfg["revision"]:
                    state = "indexing"
            return {
                "state": state,
                "error": _meta(conn, "error"),
                "notes": conn.execute("SELECT COALESCE(SUM(notes),0) FROM scopes").fetchone()[0],
                "source_backlog": _meta(conn, "source_backlog", "0") == "1",
                "pending_notebooks": conn.execute(
                    "SELECT COUNT(*) FROM scopes WHERE complete=0"
                ).fetchone()[0],
                "updated": float(_meta(conn, "updated", "0")),
                "purge_pending": bool(purge),
                "purge_age_seconds": max(0, time.time() - purge) if purge else 0,
                "purge_overdue": bool(purge and time.time() - purge > PURGE_DEADLINE_S),
                "purge_error": _meta(conn, "purge_error"),
            }
    except (sqlite3.Error, memory.MemoryStoreError, ValueError):
        return {"state": "unavailable", "error": "not_ready", "notes": 0}


def available_scopes():
    try:
        with contextlib.closing(_connect()) as conn:
            return [r[0] for r in conn.execute("SELECT scope FROM scopes")]
    except (sqlite3.Error, memory.MemoryStoreError):
        return []


def vector_support() -> dict:
    if importlib.util.find_spec("lancedb") is None:
        return {"available": False, "reason": "native_unavailable"}
    if platform.system() == "Darwin" and platform.machine() == "x86_64":
        return {"available": False, "reason": "unqualified_platform"}
    if platform.system() == "Linux" and platform.machine() == "x86_64":
        from importlib.metadata import PackageNotFoundError, version

        try:
            version("lancedb-compat")
            try:
                version("lancedb")
            except PackageNotFoundError:
                pass
            else:
                # The distributions overwrite the same import namespace; metadata alone cannot
                # tell which native binary won. Refuse before an older CPU can hit SIGILL.
                return {"available": False, "reason": "conflicting_native_packages"}
            required = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3", "pni"}
        except PackageNotFoundError:
            required = {"avx2", "fma", "f16c"}
        try:
            flags = Path("/proc/cpuinfo").read_text()
        except OSError:
            return {"available": False, "reason": "cpu_unverified"}
        if not required <= set(flags.split()):
            return {"available": False, "reason": "cpu_unsupported"}
    return {"available": True, "reason": ""}


def _reset_database():
    global _DB_CACHE
    with _DB_LOCK:
        cached, _DB_CACHE = _DB_CACHE, None
        if cached is not None:
            cached[1].close()


def _database(*, create=False):
    global _DB_CACHE, _DB_LOCK, _DB_PID
    if not vector_support()["available"]:
        raise memory_embeddings.EmbeddingError("native_unavailable")
    os.environ.setdefault("LANCE_CPU_THREADS", "2")
    os.environ.setdefault("RAYON_NUM_THREADS", "2")
    try:
        with memory._dirfd(root()) as parent:
            fd = memory._open_dir("lance", parent, create=create)
            try:
                identity = os.fstat(fd)
            finally:
                os.close(fd)
    except FileNotFoundError:
        raise memory_embeddings.EmbeddingError("index_missing") from None
    pid = os.getpid()
    if _DB_PID != pid:
        _DB_LOCK = threading.Lock()
        _DB_PID = pid
    key = (pid, str(root().resolve()), identity.st_dev, identity.st_ino)
    with _DB_LOCK:
        if _DB_CACHE is None or _DB_CACHE[0] != key:
            if _DB_CACHE is not None:
                _DB_CACHE[1].close()
            import lancedb

            # Reuse the native session's index caches. Zero consistency interval still checks
            # every table read for commits by another app instance; source hydration is separate.
            _DB_CACHE = (
                key,
                lancedb.connect(root() / "lance", read_consistency_interval=timedelta(0)),
            )
        return _DB_CACHE[1]


def _table_names(db):
    names, page = [], None
    while True:
        result = db.list_tables(page_token=page)
        names.extend(result.tables)
        page = result.page_token
        if not page:
            return names


def _table(db, name, *, dimensions=0, create=False):
    try:
        return db.open_table(name)
    except (FileNotFoundError, ValueError):
        if not create:
            raise
    import pyarrow as pa

    fields = [pa.field(k, pa.string()) for k in ("id", "scope", "slug", "content_digest")]
    fields += [pa.field(k, pa.int64()) for k in ("revision", "generation", "chunk", "offset")]
    fields += [pa.field("vector", pa.list_(pa.float32(), dimensions))]
    return db.create_table(name, schema=pa.schema(fields))


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _scope_filter(scopes):
    return "scope IN (" + ",".join(_literal(s) for s in scopes) + ")"


def _scope_token(scope):
    return "b" + hashlib.sha256(scope.encode()).hexdigest()[:16]


def _chunks(note):
    text, at, rows = note["body"], 0, []
    for chunk in range(32):
        end = min(len(text), at + 2200)
        if end < len(text):
            boundary = text.rfind("\n\n", at + 800, end)
            if boundary > at:
                end = boundary + 2
        rows.append(
            {
                "id": hashlib.sha256(
                    f"{note['scope']}\0{note['slug']}\0{chunk}".encode()
                ).hexdigest(),
                "scope": note["scope"],
                "slug": note["slug"],
                "revision": note["revision"],
                "generation": note["generation"],
                "content_digest": note["content_digest"],
                "chunk": chunk,
                "offset": at,
                # One lexical document per note; embeddings retain bounded overlapping chunks.
                "text": note["name"] + "\n" + text if chunk == 0 else "",
                "chunk_text": note["name"] + "\n" + text[at:end],
                "scope_token": _scope_token(note["scope"]),
            }
        )
        if end == len(text):
            break
        at = max(at + 1, end - 240)
    return rows


def _catalog():
    known = projects.load()
    return [
        s
        for s in memory._list_scopes()
        if (memory.project_of(s) == memory.HQ_SCOPE or memory.project_of(s) in known)
        and memory._exists(s)
    ]


def _source_batch(conn, scope):
    conn.execute("INSERT OR IGNORE INTO scopes(scope) VALUES(?)", (scope,))
    state = dict(conn.execute("SELECT * FROM scopes WHERE scope=?", (scope,)).fetchone())
    with memory._scope_lock(scope):
        if not memory._exists(scope):
            return [], state
        with contextlib.closing(memory._connect(scope)) as source:
            if memory._meta(source, "clearing", "0") == "1":
                return [], state
            floor = source.execute("SELECT COALESCE(MIN(seq),0) FROM memory_changes").fetchone()[0]
            ack = state["watermark"] if state["complete"] else state["base_seq"]
            if (state["complete"] or state["cursor"]) and ack < floor - 1:
                state.update(cursor="", base_seq=0, watermark=0, complete=0, _reset=True)
            if not state["complete"]:
                if not state["cursor"]:
                    state["base_seq"] = source.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM memory_changes"
                    ).fetchone()[0]
                slugs = [
                    r[0]
                    for r in source.execute(
                        "SELECT slug FROM notes WHERE slug>? ORDER BY slug LIMIT ?",
                        (state["cursor"], BATCH_NOTES),
                    )
                ]
                if len(slugs) < BATCH_NOTES:
                    state.update(complete=1, watermark=state["base_seq"])
                if slugs:
                    state["cursor"] = slugs[-1]
            else:
                rows = source.execute(
                    "SELECT seq,slug,at FROM memory_changes WHERE seq>? ORDER BY seq LIMIT ?",
                    (state["watermark"], BATCH_NOTES),
                ).fetchall()
                slugs = list(dict.fromkeys(r[1] for r in rows))
                if rows:
                    state["watermark"] = rows[-1][0]
                    state["_changed_at"] = min(r[2] for r in rows)
            state["notes"] = source.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            state["updated"] = time.time()
            if state["complete"]:
                # A final short backfill page may finish while an earlier note has changed
                # behind its cursor. Drain those known events before reporting caught up.
                state["_backlog"] = (
                    source.execute(
                        "SELECT 1 FROM memory_changes WHERE seq>? LIMIT 1", (state["watermark"],)
                    ).fetchone()
                    is not None
                )
    return slugs, state


def _purge_requested(conn, at=None):
    old = float(_meta(conn, "purge_pending", "0") or "0")
    _set(conn, "purge_pending", min(old, at or time.time()) if old else at or time.time())


def _remove(conn, scope, slugs=None, *, at=None):
    if slugs == []:
        return
    sql = "scope=?"
    args = [scope]
    if slugs is not None:
        sql += " AND slug IN (" + ",".join("?" for _ in slugs) + ")"
        args.extend(slugs)
    if conn.execute("SELECT 1 FROM documents WHERE " + sql + " LIMIT 1", args).fetchone() is None:  # noqa: S608
        return
    with memory._txn(conn):
        _purge_requested(conn, at)
        if (root() / "lance").exists():
            conn.executemany(
                "INSERT OR IGNORE INTO vector_deletes VALUES(?,?)",
                [(scope, s) for s in slugs] if slugs is not None else [(scope, "")],
            )
        conn.execute("DELETE FROM documents WHERE " + sql, args)  # noqa: S608 — placeholders only


@contextlib.contextmanager
def _read_lease():
    try:
        fd = os.open(root() / "gc.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise memory.MemoryStoreError("memory search is unavailable or rebuilding", 503) from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise memory.MemoryStoreError("memory search is busy; retry shortly", 503) from None
        yield
    finally:
        os.close(fd)


def _drop_vectors():
    """Only the app-owned derived directory, under the writer + exclusive read leases."""
    if not (root() / "lance").exists():
        return
    _reset_database()
    with memory._dirfd(root()) as parent:
        fd = memory._open_dir("lance", parent, create=False)
        failed = []
        try:
            memory._scrub_dir(fd, failed)
        finally:
            os.close(fd)
        if failed:
            raise OSError("derived vector cleanup incomplete")
        os.rmdir("lance", dir_fd=parent)


def _initialize(conn):
    if _meta(conn, "format") == FORMAT:
        return True
    fd = os.open(root() / "gc.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _purge_requested(conn)
        _drop_vectors()
        conn.execute("DROP VIEW IF EXISTS search_content")
        for name in ("vocabulary", "terms", "documents", "scopes", "vector_deletes"):
            conn.execute("DROP TABLE IF EXISTS " + name)  # noqa: S608 — closed literal table list
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO terms(terms,rank) VALUES('rank','bm25(0.0,1.0)')")
        _set(conn, "format", FORMAT)
        _set(conn, "state", "indexing")
        return True
    finally:
        os.close(fd)


def _sync_sources(conn):
    scopes = _catalog()
    backlog = False
    failed = False
    for row in conn.execute("SELECT scope FROM scopes").fetchall():
        if row[0] not in scopes:
            _remove(conn, row[0])
            conn.execute("DELETE FROM scopes WHERE scope=?", (row[0],))
    for scope in scopes:
        slugs, state = _source_batch(conn, scope)
        if state.pop("_reset", False):
            _remove(conn, scope)
        backlog |= (
            state.pop("_backlog", False) or not state["complete"] or len(slugs) >= BATCH_NOTES
        )
        if slugs:
            with memory._fenced():
                try:
                    notes = service.read_many(service.operator(), scope, slugs)
                except template_secrets.RedactionUnavailable:
                    _remove(conn, scope, slugs, at=state.get("_changed_at"))
                    failed = True
                    continue  # other notebooks' pending deletions must still be withdrawn
                existing = {
                    row["slug"]: row
                    for row in conn.execute(
                        "SELECT slug,revision,generation,content_digest FROM documents "  # noqa: S608
                        "WHERE scope=? AND chunk=0 AND slug IN ("
                        + ",".join("?" for _ in slugs)
                        + ")",
                        [scope, *slugs],
                    )
                }
                unchanged = {
                    note["slug"]
                    for note in notes
                    if note["slug"] in existing
                    and all(
                        note[k] == existing[note["slug"]][k]
                        for k in ("revision", "generation", "content_digest")
                    )
                }
                # Inventory rechecks emit events for every note. Preserve eligible unchanged
                # vectors; only changed or now-withheld sources need purging and re-embedding.
                _remove(
                    conn,
                    scope,
                    [s for s in slugs if s not in unchanged],
                    at=state.get("_changed_at"),
                )
                notes = [note for note in notes if note["slug"] not in unchanged]
                rows = [row for note in notes for row in _chunks(note)]
                with memory._txn(conn):
                    conn.executemany(
                        "INSERT INTO documents(id,scope,slug,revision,generation,content_digest,"
                        "chunk,offset,text,chunk_text,scope_token) "
                        "VALUES(:id,:scope,:slug,:revision,"
                        ":generation,:content_digest,:chunk,:offset,:text,:chunk_text,:scope_token)"
                        " ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,"
                        "generation=excluded.generation,content_digest=excluded.content_digest,"
                        "text=excluded.text,chunk_text=excluded.chunk_text,"
                        "offset=excluded.offset,dense_generation=''",
                        rows,
                    )
        conn.execute(
            "UPDATE scopes SET cursor=?,base_seq=?,watermark=?,complete=?,notes=?,updated=? "
            "WHERE scope=?",
            (
                state["cursor"],
                state["base_seq"],
                state["watermark"],
                state["complete"],
                state["notes"],
                state["updated"],
                scope,
            ),
        )
    if failed:
        raise memory.MemoryStoreError("memory source safety check unavailable", 503)
    return backlog


def _drain_deletes(conn):
    jobs = conn.execute("SELECT scope,slug FROM vector_deletes LIMIT 1024").fetchall()
    if not jobs:
        return True
    if not (root() / "lance").exists():
        conn.execute("DELETE FROM vector_deletes")
        return True
    if not vector_support()["available"]:
        return False  # _collect_versions can safely remove the derived cache without the library
    db = _database()
    tables = [_table(db, name) for name in _table_names(db) if name.startswith("dense_")]
    grouped = {}
    for scope, slug in jobs:
        grouped.setdefault(scope, []).append(slug)
    for scope, slugs in grouped.items():
        for start in range(0, len(slugs), BATCH_NOTES):
            batch = slugs[start : start + BATCH_NOTES]
            condition = "scope=" + _literal(scope)
            if "" not in batch:
                condition += " AND slug IN (" + ",".join(map(_literal, batch)) + ")"
            for table in tables:
                table.delete(condition)
            with memory._txn(conn):
                conn.executemany(
                    "DELETE FROM vector_deletes WHERE scope=? AND slug=?",
                    [(scope, slug) for slug in batch],
                )
    return len(jobs) < 1024


def _collect_versions(conn):
    fd = os.open(root() / "gc.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _set(conn, "purge_error", "readers_busy")
            return False
        if (root() / "lance").exists():
            try:
                cfg = memory_settings.stored()
                disabled = not cfg["base_url"] or not cfg["model"]
            except memory.MemoryStoreError:
                cfg, disabled = None, False
            if disabled or not vector_support()["available"]:
                _drop_vectors()
                conn.execute("UPDATE documents SET dense_generation=''")
                conn.execute("DELETE FROM vector_deletes")
            else:
                db = _database()
                active = "dense_" + cfg["revision"] if cfg is not None else None
                for name in _table_names(db):
                    if not name.startswith("dense_"):
                        continue
                    if active is not None and name != active:
                        db.drop_table(name)
                        continue
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore",
                            message=r"optimize\(cleanup_older_than=0\).*",
                            category=UserWarning,
                        )
                        table = _table(db, name)
                        table.optimize(
                            cleanup_older_than=timedelta(0),
                            delete_unverified=True,
                            compaction_options={
                                "materialize_deletions": True,
                                "materialize_deletions_threshold": 0.0,
                            },
                        )
                        stats = table.stats()
                        deleted = (
                            stats.get("num_deleted_rows")
                            if isinstance(stats, dict)
                            else stats.num_deleted_rows
                        )
                        if deleted != 0 or len(table.list_versions()) != 1:
                            _set(conn, "purge_error", "vector_cleanup_unverified")
                            return False
        if conn.execute("SELECT 1 FROM vector_deletes LIMIT 1").fetchone():
            return False
        conn.execute("INSERT INTO terms(terms) VALUES('optimize')")
        # FTS optimize removes delete-keys/obsolete postings. Core secure_delete zeros their
        # released pages and checkpoint/TRUNCATE removes old WAL page images. A whole-file
        # VACUUM while live rows remain adds an O(index-size) copy to every small update.
        if conn.execute("SELECT 1 FROM documents LIMIT 1").fetchone() is None:
            conn.execute("VACUUM")
        if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            _set(conn, "purge_error", "checkpoint_busy")
            return False
        _reset_database()
        _set(conn, "purge_pending", "")
        _set(conn, "purge_error", "")
        return True
    finally:
        os.close(fd)


def _current_sources(rows, generation):
    if memory_settings.stored()["revision"] != generation:
        raise memory_embeddings.EmbeddingError("configuration_changed")
    by_scope = {}
    for row in rows:
        by_scope.setdefault(row["scope"], set()).add(row["slug"])
    current = {}
    for scope, slugs in by_scope.items():
        for note in service.read_many(service.operator(), scope, list(slugs)):
            current[(scope, note["slug"])] = note
    for row in rows:
        note = current.get((row["scope"], row["slug"]))
        if note is None or any(
            row[k] != note[k] for k in ("revision", "generation", "content_digest")
        ):
            raise memory_embeddings.EmbeddingError("source_changed")


def _put(table, rows):
    if not rows:
        return
    import pyarrow as pa

    table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
        pa.Table.from_pylist(rows, schema=table.schema)
    )


def _maintain_dense(conn, dense, generation):
    from lancedb.index import BTree, IvfHnswFlat

    count = dense.count_rows()
    indices = dense.list_indices()
    if count and not any(i.name == "memory_id" for i in indices):
        dense.create_index("id", config=BTree(), name="memory_id")
    indexed = int(_meta(conn, "ann_rows_" + generation, "0"))
    if count >= 256 and (
        not any(i.index_type.upper().startswith("IVF") for i in indices)
        or count >= max(4096, indexed * 4)
    ):
        dense.create_index(
            "vector",
            config=IvfHnswFlat(
                distance_type="cosine",
                num_partitions=max(1, min(64, count // 256)),
                m=16,
                ef_construction=100,
            ),
        )
        dense.create_index("scope", config=BTree())
        _set(conn, "ann_rows_" + generation, count)
    maintained = int(_meta(conn, "dense_maintained_" + generation, "0"))
    if count - maintained >= 512:
        dense.optimize(cleanup_older_than=timedelta(days=7))
        _set(conn, "dense_maintained_" + generation, count)


def _dense_pass(conn):
    cfg = memory_settings.get()
    if not cfg["base_url"] or not cfg["model"]:
        if (root() / "lance").exists():
            _purge_requested(conn)
        return False, False
    if not vector_support()["available"]:
        _set(conn, "error", "native_unavailable")
        return False, False
    generation = cfg["revision"]
    db = _database(create=True)
    name = "dense_" + generation
    names = _table_names(db)
    if name not in names:
        conn.execute(
            "UPDATE documents SET dense_generation='' WHERE dense_generation=?", (generation,)
        )
    if any(n.startswith("dense_") and n != name for n in names):
        _purge_requested(conn)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM documents WHERE dense_generation<>? LIMIT ?",
            (generation, memory_embeddings.BATCH_MAX),
        )
    ]
    if not rows:
        if name in names:
            _maintain_dense(conn, _table(db, name), generation)
        _set(conn, "dense_revision", generation)
        return True, False
    try:
        vectors = memory_embeddings.embed(
            cfg,
            [r["chunk_text"] for r in rows],
            before_send=lambda: _current_sources(rows, generation),
        )
    except memory_embeddings.EmbeddingError:
        _set(conn, "error", "embeddings_unavailable")
        return False, False
    if not memory_settings.observe_dimensions(generation, len(vectors[0])):
        return False, True
    dense = _table(db, name, dimensions=len(vectors[0]), create=True)
    with memory._fenced():
        _current_sources(rows, generation)
        accepted = [
            {
                k: r[k]
                for k in (
                    "id",
                    "scope",
                    "slug",
                    "revision",
                    "generation",
                    "content_digest",
                    "chunk",
                    "offset",
                )
            }
            | {"vector": v}
            for r, v in zip(rows, vectors, strict=True)
        ]
        _put(dense, accepted)
        conn.executemany(
            "UPDATE documents SET dense_generation=? WHERE id=?",
            [(generation, r["id"]) for r in rows],
        )
    _maintain_dense(conn, dense, generation)
    _set(conn, "dense_revision", generation)
    return True, len(rows) == memory_embeddings.BATCH_MAX


def sync(*, poll_sources=True):
    _prepare()
    lock = os.open(root() / "writer.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    conn = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        conn = _connect(write=True)
        if not _initialize(conn):
            return True
        _set(conn, "error", "")
        more = _sync_sources(conn) if poll_sources else False
        if poll_sources:
            _set(conn, "source_backlog", int(more))
        deletes_done = _drain_deletes(conn)
        ready, dense_more = _dense_pass(conn) if deletes_done else (False, False)
        more |= dense_more or not deletes_done
        if _meta(conn, "purge_pending"):
            more |= not _collect_versions(conn)
        _set(conn, "state", "indexing" if more else "ready" if ready else "keyword-only")
        _set(conn, "updated", time.time())
        return more
    except Exception as exc:
        if conn is not None:
            _set(conn, "error", "index_failed")
            _set(conn, "state", "degraded")
            if _meta(conn, "purge_pending"):
                with contextlib.suppress(Exception):
                    _drain_deletes(conn)
                    _collect_versions(conn)
        log.warning("memory index pass failed (%s)", type(exc).__name__)
        raise
    finally:
        if conn is not None:
            purge = float(_meta(conn, "purge_pending", "0") or "0")
            if (
                purge
                and time.time() - purge > PURGE_DEADLINE_S
                and _meta(conn, "purge_signalled") != str(purge)
            ):
                log.error("Memory storage purge is overdue; cleanup remains incomplete")
                _set(conn, "purge_signalled", purge)
            conn.close()
        os.close(lock)


def _query_words(query):
    tokens = re.findall(r"\w+", query, re.UNICODE)
    return [w for w in tokens if w.lower() not in _STOP_WORDS][:16] or tokens[:16]


def _lexical(conn, query, scopes, count):
    words = _query_words(query)
    if not words:
        return []
    expression = "text:(" + " OR ".join('"' + word + '"' for word in words) + ")"
    indexed = {r[0] for r in conn.execute("SELECT scope FROM scopes")}
    if set(scopes) != indexed:
        expression += " AND scope_token:(" + " OR ".join(_scope_token(s) for s in scopes) + ")"
    placeholders = ",".join("?" for _ in scopes)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT d.* FROM terms JOIN documents d ON d.rowid=terms.rowid "  # noqa: S608
            "WHERE terms MATCH ? AND d.scope IN (" + placeholders + ") ORDER BY terms.rank LIMIT ?",
            [expression, *scopes, count],
        )
    ]


def _hydrate(principal, candidates, limit, query=""):
    ordered = []
    seen = set()
    for _, row in sorted(candidates.values(), key=lambda pair: -pair[0]):
        key = (row["scope"], row["slug"])
        if key not in seen:
            seen.add(key)
            ordered.append(row)
    ordered = ordered[:64]
    hits, stale = [], False
    with memory._fenced():
        for start in range(0, len(ordered), limit):
            batch = ordered[start : start + limit]
            grouped = {}
            for row in batch:
                grouped.setdefault(row["scope"], []).append(row["slug"])
            current = {}
            for scope, slugs in grouped.items():
                try:
                    notes = service.read_many(principal, scope, slugs)
                except memory.MemoryStoreError as exc:
                    if exc.status not in {403, 404, 409}:
                        raise
                    notes = []
                current.update({(scope, n["slug"]): n for n in notes})
            for row in batch:
                note = current.get((row["scope"], row["slug"]))
                if note is None or any(
                    note[k] != row[k] for k in ("revision", "generation", "content_digest")
                ):
                    stale = True
                    continue
                hits.append(
                    {
                        k: note[k]
                        for k in (
                            "scope",
                            "slug",
                            "name",
                            "type",
                            "revision",
                            "generation",
                            "content_digest",
                            "updated",
                            "source",
                            "notebook",
                        )
                    }
                    | {
                        "excerpt": _excerpt(note["body"], query, row["offset"]),
                        "href": "/library/memory?project="
                        + note["scope"]
                        + "&note="
                        + note["slug"],
                    }
                )
                if len(hits) == limit:
                    return hits, stale
    return hits, stale


def _excerpt(body, query, offset):
    words = _query_words(query)
    if words:
        match = re.search(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", body, re.I)
        if match:
            offset = max(0, match.start() - 80)
    return body[offset : offset + service.EXCERPT_MAX]


def _keyword_candidates(query, scopes, count):
    with contextlib.closing(_connect()) as conn:
        return _lexical(conn, query, scopes, count)


def _rankings(query, scopes, count, health):
    """Under the caller's read lease; independent lexical/ANN work shares no SQLite connection."""
    cfg = memory_settings.get()
    configured = bool(cfg["base_url"] and cfg["model"])
    native = configured and vector_support()["available"]
    if configured and not native:
        health["state"] = "keyword-only"
    parallel = (
        concurrent.futures.ThreadPoolExecutor(max_workers=1) if native else contextlib.nullcontext()
    )
    with parallel as pool:
        lexical = pool.submit(_keyword_candidates, query, scopes, count) if pool else None
        rankings = []
        if native:
            try:
                db = _database()
                name = "dense_" + cfg["revision"]
                if name in _table_names(db):

                    def current_connection():
                        if memory_settings.stored()["revision"] != cfg["revision"]:
                            raise memory_embeddings.EmbeddingError("connection_changed")

                    vector = memory_embeddings.embed(cfg, [query], before_send=current_connection)[
                        0
                    ]
                    rankings.append(
                        _table(db, name)
                        .search(vector)
                        .distance_type("cosine")
                        .where(_scope_filter(scopes), prefilter=True)
                        .nprobes(64)
                        .ef(256)
                        .limit(count)
                        .to_list()
                    )
                elif health["notes"]:
                    health["state"] = "indexing"
            except Exception:  # noqa: BLE001 — valid keywords remain; no hidden scanner
                if health["state"] != "indexing":
                    health["state"] = "keyword-only"
        rankings.insert(
            0, lexical.result() if lexical else _keyword_candidates(query, scopes, count)
        )
        return rankings


def search(principal, query, scopes, limit):
    with _read_lease():
        health = status()
        if health["state"] == "unavailable":
            raise memory.MemoryStoreError("memory search is unavailable or rebuilding", 503)
        if not scopes:
            return {
                "hits": [],
                "state": health["state"],
                "complete": health["state"] not in {"indexing", "degraded"}
                and not health.get("source_backlog", False),
            }
        try:
            rankings = _rankings(query, scopes, min(64, limit * 4), health)
            candidates = {}
            for ranking in rankings:
                seen = set()
                for row in ranking:
                    key = (row["scope"], row["slug"])
                    if key in seen:
                        continue
                    seen.add(key)
                    score, old = candidates.get(key, (0, row))
                    candidates[key] = (score + 1 / (60 + len(seen)), old)
            hits, stale = _hydrate(principal, candidates, limit, query)
            if stale:
                health["state"] = "indexing"
            return {
                "hits": hits,
                "state": health["state"],
                "complete": health["state"] not in {"indexing", "degraded"}
                and not health.get("source_backlog", False),
            }
        except memory.MemoryStoreError:
            raise
        except Exception as exc:
            log.warning("memory search failed (%s)", type(exc).__name__)
            raise memory.MemoryStoreError("memory search is unavailable", 503) from None


async def run():
    if os.environ.get("AGENT_SESSIONS_MEMORY_LOOP", "1") == "0":
        return
    delay, next_poll, backlog = 0.0, 0.0, True
    while True:
        await asyncio.sleep(delay)
        try:
            poll = backlog or time.monotonic() >= next_poll
            more = await asyncio.to_thread(sync, poll_sources=poll)
            health = await asyncio.to_thread(status)
            backlog = health.get("source_backlog", False)
            if poll:
                next_poll = time.monotonic() + INTERVAL_S
            delay = 0.1 if more else INTERVAL_S
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = min(300, max(INTERVAL_S, delay * 2))
