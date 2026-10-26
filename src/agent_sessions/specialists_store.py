"""`specialists.db` — the specialist pool (#1693, #1613 P3).

* **Location** ``~/.config/agent-sessions/specialists.db``, override
  ``AGENT_SESSIONS_SPECIALISTS_DB``. Created ``0600`` before SQLite can create it under the umask.
* **Versions are immutable.** An edit inserts version N+1 and moves ``current_version``; it never
  rewrites an old row, so anything that pinned ``{id, version}`` (P5/P6 approvals) keeps exactly
  what it approved. Archiving hides a specialist from new use; its versions stay readable.
* **Every write is one IMMEDIATE transaction fenced by ``expect_revision``** (409 on mismatch).
* **The grade is never stored.** ``public`` derives it from ``model_grades`` at read.
* **Versioned by ``PRAGMA user_version``**; a file from a NEWER build is refused, never written.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import model_grades

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
SPECIALISTS_MAX = 100
VERSIONS_MAX = 500
ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")

_SCHEMA = """
CREATE TABLE specialists (
    id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE specialist_versions (
    id TEXT NOT NULL REFERENCES specialists(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (id, version)
)
"""


class StoreError(RuntimeError):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


class StoreUnsupported(StoreError):
    def __init__(self, version: int) -> None:
        super().__init__(
            f"specialists store is at schema {version}, newer than this build "
            f"({SCHEMA_VERSION}); it is left untouched",
            status=503,
        )


class NotFound(StoreError):
    def __init__(self, what: str = "specialist") -> None:
        super().__init__(f"unknown {what}", status=404)


class Conflict(StoreError):
    def __init__(self, message: str = "the specialist changed since you loaded it") -> None:
        super().__init__(message, status=409)


# ---- path + connection --------------------------------------------------------------------------


def db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_SPECIALISTS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "specialists.db"),
        )
    )


_schema_lock = threading.Lock()
_schema_done: set[str] = set()


def reset_schema_cache_for_test() -> None:
    with _schema_lock:
        _schema_done.clear()


def _raw_connect(p: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return con


def _version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def _ensure_schema(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600))
    except FileExistsError:
        pass
    con = _raw_connect(p)
    try:
        # Asked BEFORE anything that writes (WAL mode rewrites the file header): a file from a
        # newer build is refused untouched.
        v = _version(con)
        if v > SCHEMA_VERSION:
            raise StoreUnsupported(v)
        if v == SCHEMA_VERSION:
            return
        created = False
        con.execute("BEGIN IMMEDIATE")
        try:
            v = _version(con)  # re-read under the write lock: a peer may have created it
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            if v == 0:
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                created = True
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        if created:
            con.execute("PRAGMA journal_mode=WAL")  # outside the transaction; our own file only
    finally:
        con.close()


def _connect() -> sqlite3.Connection:
    p = db_path()
    key = str(p)
    if key not in _schema_done or not p.exists():
        with _schema_lock:
            _ensure_schema(p)
            _schema_done.add(key)
    con = _raw_connect(p)
    v = _version(con)
    if v > SCHEMA_VERSION:  # a newer peer migrated it after this process checked
        con.close()
        raise StoreUnsupported(v)
    return con


@contextlib.contextmanager
def _db_errors():
    """A locked, corrupt or unreadable file is a retryable 503 with a reason, never a bare 500."""
    try:
        yield
    except sqlite3.Error as e:
        raise StoreError(f"the specialist store is unavailable: {e}", status=503) from None


@contextlib.contextmanager
def _tx():
    with _db_errors():
        con = _connect()
    try:
        with _db_errors():
            con.execute("BEGIN IMMEDIATE")
            v = _version(con)
        try:
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            with _db_errors():
                yield con
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        else:
            with _db_errors():
                con.execute("COMMIT")
    finally:
        con.close()


@contextlib.contextmanager
def _read():
    with _db_errors():
        con = _connect()
    try:
        with _db_errors():
            yield con
    finally:
        con.close()


# ---- shapes -------------------------------------------------------------------------------------


#: Exactly what `specialists.check_draft` writes; any other key is a damaged row.
_BODY_KEYS = frozenset({"title", "summary", "skills", "engine", "model", "effort", "capacity"})


def _valid_body(body: object) -> bool:
    """A stored version has exactly the shape `specialists.check_draft` writes. Anything else is a
    damaged row (a hand edit, a bad restore): refused here so no reader projects it."""
    if not isinstance(body, dict) or set(body) != _BODY_KEYS:
        return False
    try:  # every response encodes it: a lone surrogate or a NaN would fail each later read
        json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    except (UnicodeEncodeError, TypeError, ValueError):
        return False
    skills = body.get("skills")
    return (
        isinstance(body.get("title"), str)
        and isinstance(body.get("summary"), str)
        and isinstance(body.get("engine"), str)
        and isinstance(body.get("model"), str)
        and (body.get("effort") is None or isinstance(body.get("effort"), str))
        and isinstance(body.get("capacity"), int)
        and not isinstance(body.get("capacity"), bool)
        and isinstance(skills, list)
        and all(
            isinstance(k, dict)
            and set(k) == {"id", "revision"}
            and isinstance(k["id"], str)
            and isinstance(k["revision"], str)
            for k in skills
        )
    )


def _damaged(what: str) -> StoreError:
    return StoreError(f"{what} is damaged", status=500)


def _int(value: object, what: str) -> int:
    """A stored integer column, or `StoreError`: SQLite stores whatever a hand edit wrote."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _damaged(what)
    return value


def _time(value: object, what: str) -> float:
    """A stored timestamp: a finite number. A NaN or text would fail every JSON response."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _damaged(what)
    return float(value)


def _version_row(r: sqlite3.Row) -> dict:
    what = f"specialist {r['id']} v{r['version']}"
    try:
        body = json.loads(r["body"])
    except (TypeError, ValueError):
        body = None
    if not _valid_body(body):
        raise _damaged(what)
    return {"version": _int(r["version"], what), **body, "created_at": _time(r["created_at"], what)}


def _get(con: sqlite3.Connection, sid: str) -> dict:
    r = con.execute("SELECT * FROM specialists WHERE id = ?", (sid,)).fetchone()
    if r is None:
        raise NotFound()
    v = con.execute(
        "SELECT * FROM specialist_versions WHERE id = ? AND version = ?",
        (sid, r["current_version"]),
    ).fetchone()
    if v is None:
        raise StoreError(f"specialist {sid} has no current version", status=500)
    what = f"specialist {sid}"
    out = {
        "id": sid,
        "revision": _int(r["revision"], what),
        "archived": bool(_int(r["archived"], what)),
        "current": _version_row(v),
        "created_at": _time(r["created_at"], what),
        "updated_at": _time(r["updated_at"], what),
    }
    try:  # the last word: this record reaches a JSON response, so it must encode strictly
        json.dumps(out, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _damaged(what) from None
    return out


def _new_id(con: sqlite3.Connection, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "specialist"
    if not base[0].isalnum():
        base = "specialist"
    sid = base
    while con.execute("SELECT 1 FROM specialists WHERE id = ?", (sid,)).fetchone():
        sid = f"{base}-{secrets.token_hex(3)}"
    return sid


# ---- API ----------------------------------------------------------------------------------------


def create(body: dict, *, now: float | None = None) -> dict:
    """A new specialist at version 1 from an already-checked body (`specialists.check_draft`)."""
    ts = time.time() if now is None else now
    with _tx() as con:
        (count,) = con.execute("SELECT COUNT(*) FROM specialists").fetchone()
        if count >= SPECIALISTS_MAX:
            raise StoreError(
                f"the pool holds at most {SPECIALISTS_MAX} specialists, archived ones included",
                status=409,
            )
        sid = _new_id(con, body["title"])
        con.execute(
            "INSERT INTO specialists (id, current_version, revision, archived, created_at, "
            "updated_at) VALUES (?, 1, 1, 0, ?, ?)",
            (sid, ts, ts),
        )
        con.execute(
            "INSERT INTO specialist_versions (id, version, body, created_at) VALUES (?, 1, ?, ?)",
            (sid, json.dumps(body, sort_keys=True), ts),
        )
        return _get(con, sid)


def update(sid: str, body: dict, *, expect_revision: int, now: float | None = None) -> dict:
    """Version N+1 from a checked body; refused (409) unless ``expect_revision`` is current."""
    ts = time.time() if now is None else now
    with _tx() as con:
        cur = _get(con, sid)
        if cur["revision"] != expect_revision:
            raise Conflict()
        if cur["archived"]:
            raise StoreError("unarchive the specialist before editing it", status=409)
        nxt = cur["current"]["version"] + 1
        if nxt > VERSIONS_MAX:
            raise StoreError(f"a specialist keeps at most {VERSIONS_MAX} versions", status=409)
        con.execute(
            "INSERT INTO specialist_versions (id, version, body, created_at) VALUES (?, ?, ?, ?)",
            (sid, nxt, json.dumps(body, sort_keys=True), ts),
        )
        con.execute(
            "UPDATE specialists SET current_version = ?, revision = revision + 1, updated_at = ? "
            "WHERE id = ?",
            (nxt, ts, sid),
        )
        return _get(con, sid)


def set_archived(
    sid: str, archived: bool, *, expect_revision: int, now: float | None = None
) -> dict:
    ts = time.time() if now is None else now
    with _tx() as con:
        cur = _get(con, sid)
        if cur["revision"] != expect_revision:
            raise Conflict()
        if cur["archived"] == archived:
            return cur
        con.execute(
            "UPDATE specialists SET archived = ?, revision = revision + 1, updated_at = ? "
            "WHERE id = ?",
            (int(archived), ts, sid),
        )
        return _get(con, sid)


def get(sid: str) -> dict:
    if not ID_RE.fullmatch(sid or ""):
        raise NotFound()
    with _read() as con:
        return _get(con, sid)


def versions(sid: str) -> list[dict]:
    """Every version of ``sid``, newest first."""
    if not ID_RE.fullmatch(sid or ""):
        raise NotFound()
    with _read() as con:
        _get(con, sid)
        rows = con.execute(
            "SELECT * FROM specialist_versions WHERE id = ? ORDER BY version DESC", (sid,)
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(_version_row(r))
            except StoreError as e:  # a damaged old version never hides the readable ones
                log.warning("specialists: %s", e)
        return out


def version(sid: str, n: int) -> dict:
    """One pinned version — readable whether or not the specialist is archived."""
    if not ID_RE.fullmatch(sid or ""):
        raise NotFound()
    with _read() as con:
        r = con.execute(
            "SELECT * FROM specialist_versions WHERE id = ? AND version = ?", (sid, n)
        ).fetchone()
        if r is None:
            raise NotFound("specialist version")
        return _version_row(r)


def list_all() -> list[dict]:
    if not db_path().exists():
        return []
    with _read() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM specialists ORDER BY created_at, id")]
        out = []
        for i in ids:
            try:
                out.append(_get(con, i))
            except StoreError as e:  # one damaged row never hides the rest of the pool
                log.warning("specialists: skipping %s: %s", i, e)
        return out


def public(
    spec: dict,
    *,
    grades: dict | None = None,
    library: dict[str, str] | None = None,
    live: int = 0,
) -> dict:
    """The API shape: the stored record plus what is derived at read — the grade (from the
    operator's model map), which pinned skills are missing from or changed in the library, and
    how many instances run (S4 fills ``live``; 0 until then)."""
    cur = spec["current"]
    grade = model_grades.grade_of(cur.get("engine", ""), cur.get("model"), grades)
    pinned = cur.get("skills") or []
    # An unreadable library (None) flags nothing: "missing" would be a guess.
    lib = library if library is not None else {s.get("id"): s.get("revision") for s in pinned}
    return {
        **spec,
        "grade": grade,
        "live": live,
        "skills_missing": [s["id"] for s in pinned if s.get("id") not in lib],
        "skills_outdated": [
            s["id"] for s in pinned if s.get("id") in lib and lib[s["id"]] != s.get("revision")
        ],
    }
