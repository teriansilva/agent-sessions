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

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
SPECIALISTS_MAX = 100
VERSIONS_MAX = 500
ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")

#: v2 (#1693 S4): the capacity ledger. One row per reservation; ``state`` is ``starting`` (taken
#: before the session lock and launch), ``live`` (the launch succeeded) or ``released``. At most
#: one reservation per session key is ever active (a partial unique index), so a retried create or
#: a re-attach never takes a second slot. ``owner`` is the token of the ONE launch attempt that
#: may settle it (rotated to nobody's on a re-take); ``attempt`` counts takes; ``rev`` counts EVERY
#: change to the row, so a settlement names the exact state it observed and a newer answer (or
#: launch) always wins over an older one about the same attempt; ``process`` is the process that
#: last took it (pid, start time and pid namespace), so a peer never mistakes its launch for a
#: dead one.
_RESERVATIONS = """
CREATE TABLE specialist_reservations (
    reservation_id TEXT PRIMARY KEY,
    specialist_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('starting', 'live', 'released')),
    owner TEXT NOT NULL,
    process TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    held_reason TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX specialist_reservations_one_per_session
    ON specialist_reservations (session_key) WHERE state != 'released'
"""

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
        if v > 0:
            _backup(p, v)  # BEFORE the first migration of an existing file: a complete copy
        created = False
        con.execute("BEGIN IMMEDIATE")
        try:
            v = _version(con)  # re-read under the write lock: a peer may have created it
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            if v == 0:
                for stmt in (_SCHEMA + ";" + _RESERVATIONS).split(";"):
                    if stmt.strip():
                        con.execute(stmt)
                created = True
            elif v == 1:
                for stmt in _RESERVATIONS.split(";"):  # v1 → v2: the capacity ledger
                    if stmt.strip():
                        con.execute(stmt)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        if created:
            con.execute("PRAGMA journal_mode=WAL")  # outside the transaction; our own file only
    finally:
        con.close()


def _backup(p: Path, version: int) -> Path:
    # A random suffix: two processes migrating in the same second never collide on the name.
    dest = p.with_name(f"{p.name}.bak-v{version}-{int(time.time())}-{secrets.token_hex(3)}")
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    os.close(fd)
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


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
    except (sqlite3.Error, OSError) as e:
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


# ---- capacity reservations (#1693 S4) -----------------------------------------------------------

#: A `starting` reservation touched less than this long ago belongs to a launch still under way:
#: reconcile leaves it alone (its session lock may not be taken yet). Older, it is probed.
STARTING_GRACE_S = 300
_ACTIVE = ("starting", "live")


class CapacityRefused(StoreError):
    """No slot: ``limit`` is ``specialist`` (its capacity) or ``pool`` (the global cap)."""

    def __init__(self, message: str, limit: str) -> None:
        super().__init__(message, status=409)
        self.limit = limit


def _reservation(r: sqlite3.Row, *, owner: bool = False) -> dict:
    out = {
        "attempt": int(r["attempt"]),
        "rev": int(r["rev"]),
        "process": r["process"],
        "reservation_id": r["reservation_id"],
        "specialist_id": r["specialist_id"],
        "version": int(r["version"]),
        "session_key": r["session_key"],
        "state": r["state"],
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
        "held_reason": r["held_reason"],
    }
    if owner:
        out["owner"] = r["owner"]
    return out


def canonical_key(raw: object) -> str:
    """The session key a reservation records: ``engine:native`` as the launch names it (a
    ``new-<uuid>`` placeholder included — its master runs under it), for a kind whose liveness
    reconcile can check (a terminal or an API client). Anything else is refused."""
    from . import engines

    if not isinstance(raw, str) or not raw or len(raw) > 256:
        raise StoreError("invalid session key", status=422)
    try:
        prov, native = engines.parse_key(raw, allow_new_placeholder=True)
    except Exception:  # noqa: BLE001 — an unparseable key can never be checked, so never reserved
        raise StoreError("invalid session key", status=422) from None
    runtime = getattr(getattr(prov, "manifest", None), "runtime", None)
    if runtime not in ("pty", "api"):
        raise StoreError(
            "this kind of session can't hold a specialist slot: its liveness can't be checked",
            status=422,
        )
    return f"{prov.engine_id}:{native}"


ALIVE, GONE, UNKNOWN_PROCESS = "alive", "gone", "unknown"
_PROCESS: tuple[int, str] | None = None


def _pid_namespace() -> str:
    """This process's PID namespace inode, or "" when it can't be read."""
    try:
        link = os.readlink("/proc/self/ns/pid")  # "pid:[4026531836]"
    except OSError:
        return ""
    return "".join(c for c in link if c.isdigit())


def _proc_start(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = raw.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def _process() -> str:
    """This process's identity for a reservation: ``pid:start:pidns:instance``."""
    global _PROCESS
    pid = os.getpid()
    if _PROCESS is None or _PROCESS[0] != pid:
        start, ns = _proc_start(pid) or "?", _pid_namespace() or "?"
        _PROCESS = (pid, f"{pid}:{start}:{ns}:{secrets.token_hex(4)}")
    return _PROCESS[1]


def process_state(token: str) -> str:
    """Is the process that last took a reservation still the one running?

    ``gone`` only on PROOF, and only within OUR pid namespace (the token records its own): no
    such pid (``kill(pid, 0)`` → ESRCH), or the pid now belongs to a process with another start
    time (reused). A peer in another namespace, a pid this process may not see (``hidepid``), an
    unreadable start time or an unparseable token is ``unknown`` — never taken as death, since
    reclaiming a launch under way could double-book its slot."""
    if token == _process():
        return ALIVE
    try:
        pid_s, start, ns, _inst = token.split(":")
        pid = int(pid_s)
    except (AttributeError, ValueError):
        return UNKNOWN_PROCESS
    own_ns = _pid_namespace()
    if pid <= 0 or start == "?" or ns == "?" or not own_ns or ns != own_ns:
        return UNKNOWN_PROCESS  # another namespace's pid says nothing about ours
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return GONE  # no such process in this namespace
    except PermissionError:
        pass  # it exists, owned by someone else
    except OSError:
        return UNKNOWN_PROCESS
    seen = _proc_start(pid)
    if seen is None:
        return UNKNOWN_PROCESS  # it exists; its start time is hidden from us
    return ALIVE if seen == start else GONE  # another start time: the pid was reused


def reserve(
    sid: str, n: int, session_key: str, *, max_instances: int, now: float | None = None
) -> dict:
    """A slot for one session of ``sid`` version ``n``, or `CapacityRefused`.

    ONE transaction counts the specialist's active reservations (EVERY version) against its
    CURRENT capacity and every active reservation against ``max_instances``, and inserts only if
    both hold. The result carries ``created``: True with an ``owner`` token for the attempt that
    took the slot — only it may release on failure (`release(..., owner=)`) — and False for a
    second call on the same session (a double tap, a retry, a resume), which gets the slot that
    session already holds, re-marked ``starting`` so reconcile gives this launch its grace. A
    session holding another specialist's slot is refused."""
    key = canonical_key(session_key)
    ts = time.time() if now is None else now
    with _tx() as con:
        held = con.execute(
            "SELECT * FROM specialist_reservations WHERE session_key = ? AND state != 'released'",
            (key,),
        ).fetchone()
        if held is not None:
            if held["specialist_id"] != sid or int(held["version"]) != n:
                raise Conflict("this session already holds a slot for another specialist")
            # Archived since the slot was taken: no new attempt on it (a relaunch, a retry) —
            # checked here, in the same transaction as the re-take.
            if _get(con, sid)["archived"]:
                raise StoreError("this specialist is archived", status=409)
            # A re-take (a double tap, a retry, a resume) is a NEW attempt on the same slot: back to
            # `starting` with its grace, under this process, its old hold cleared, and the owner
            # token rotated to one nobody holds — so the EARLIER attempt can no longer settle it
            # (its late failure frees nothing), and neither can this one: reconcile does, on proof.
            con.execute(
                "UPDATE specialist_reservations SET state = 'starting', updated_at = ?,"
                " owner = ?, process = ?, held_reason = '', attempt = attempt + 1, rev = rev + 1"
                " WHERE reservation_id = ?",
                (ts, secrets.token_hex(16), _process(), held["reservation_id"]),
            )
            row = con.execute(
                "SELECT * FROM specialist_reservations WHERE reservation_id = ?",
                (held["reservation_id"],),
            ).fetchone()
            return {**_reservation(row), "created": False}
        spec = _get(con, sid)
        if spec["archived"]:
            raise StoreError("this specialist is archived", status=409)
        if (
            con.execute(
                "SELECT 1 FROM specialist_versions WHERE id = ? AND version = ?", (sid, n)
            ).fetchone()
            is None
        ):
            raise NotFound("specialist version")
        capacity = spec["current"]["capacity"]
        (mine,) = con.execute(
            "SELECT COUNT(*) FROM specialist_reservations"
            " WHERE specialist_id = ? AND state != 'released'",
            (sid,),
        ).fetchone()
        if mine >= capacity:
            raise CapacityRefused(
                f"{spec['current']['title']} is at capacity ({mine} of {capacity} running)",
                "specialist",
            )
        (total,) = con.execute(
            "SELECT COUNT(*) FROM specialist_reservations WHERE state != 'released'"
        ).fetchone()
        if total >= max_instances:
            raise CapacityRefused(
                f"the pool is at its limit ({total} of {max_instances} instances running)", "pool"
            )
        rid = "r" + secrets.token_hex(12)
        con.execute(
            "INSERT INTO specialist_reservations (reservation_id, specialist_id, version, "
            "session_key, state, owner, process, attempt, rev, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'starting', ?, ?, 1, 1, ?, ?)",
            (rid, sid, n, key, secrets.token_hex(16), _process(), ts, ts),
        )
        row = con.execute(
            "SELECT * FROM specialist_reservations WHERE reservation_id = ?", (rid,)
        ).fetchone()
        return {**_reservation(row, owner=True), "created": True}


def _cas(reservation_id: str, sets: str, where: str, args: tuple, ts: float) -> bool:
    """One guarded change; every change bumps the row's ``rev`` (and its timestamp)."""
    with _tx() as con:
        cur = con.execute(
            f"UPDATE specialist_reservations SET {sets}, rev = rev + 1, updated_at = ?"  # noqa: S608
            f" WHERE reservation_id = ? AND state != 'released' {where}",
            (ts, reservation_id, *args),
        )
        return cur.rowcount == 1


def activate(reservation_id: str, *, owner: str, now: float | None = None) -> bool:
    """The launch that created this slot succeeded: ``starting`` → ``live``. Only with its owner
    token: an attempt whose slot was re-taken since settles nothing (reconcile will, on proof)."""
    ts = time.time() if now is None else now
    sets = "state = 'live', held_reason = ''"
    return _cas(reservation_id, sets, "AND state = 'starting' AND owner = ?", (owner,), ts)


def mark_live(reservation_id: str, *, seen_rev: int, now: float | None = None) -> bool:
    """Reconcile found its session live: ``live``, any old hold cleared — only if nothing changed
    since reconcile read it (``seen_rev``)."""
    ts = time.time() if now is None else now
    sets = "state = 'live', held_reason = ''"
    return _cas(reservation_id, sets, "AND rev = ?", (seen_rev,), ts)


def release(reservation_id: str, *, owner: str, now: float | None = None) -> bool:
    """A launch that failed releases the slot IT created — and only that: ``owner`` is the token
    `reserve` gave the attempt that took the slot, rotated away by any re-take since, so neither a
    double tap's other attempt nor an earlier attempt's late failure can free it. A compare-and-set
    on the id; releasing twice is a no-op."""
    if not isinstance(owner, str) or not owner:
        raise StoreError("a launch releases only with the token its reservation gave it", 422)
    ts = time.time() if now is None else now
    return _cas(reservation_id, "state = 'released'", "AND owner = ?", (owner,), ts)


def reclaim(reservation_id: str, *, seen_rev: int, now: float | None = None) -> bool:
    """Reconcile proved the session dead: release it — only if nothing changed since reconcile
    read it. A re-take, a launch's `activate`, or a newer reconcile answer (live, held) each move
    ``rev``, so an older DEAD answer can never release what they settled."""
    ts = time.time() if now is None else now
    return _cas(reservation_id, "state = 'released'", "AND rev = ?", (seen_rev,), ts)


def release_held(reservation_id: str, *, seen_rev: int, now: float | None = None) -> bool:
    """The operator frees a slot reconcile could not prove free — only the HELD state they were
    shown (``seen_rev``): any change since (a re-take, a launch going live, a newer answer) moves
    ``rev``, so a running session is never the operator's to take."""
    ts = time.time() if now is None else now
    where = "AND held_reason != '' AND rev = ?"
    return _cas(reservation_id, "state = 'released'", where, (seen_rev,), ts)


def hold(reservation_id: str, reason: str, *, seen_rev: int) -> bool:
    """Record why reconcile could not prove this attempt dead (it stays active) — only if nothing
    changed since reconcile read it. Moves ``rev`` (so an operator release names this hold) but
    NOT the timestamp: holding grants no grace."""
    with _tx() as con:
        cur = con.execute(
            "UPDATE specialist_reservations SET held_reason = ?, rev = rev + 1"
            " WHERE reservation_id = ? AND state != 'released' AND rev = ?",
            (reason[:200], reservation_id, seen_rev),
        )
        return cur.rowcount == 1


def get_reservation(reservation_id: str) -> dict:
    with _read() as con:
        r = con.execute(
            "SELECT * FROM specialist_reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if r is None:
            raise NotFound("reservation")
        return _reservation(r)


def active() -> list[dict]:
    """Every reservation that holds a slot (``starting`` or ``live``)."""
    if not db_path().exists():
        return []
    with _read() as con:
        rows = con.execute(
            "SELECT * FROM specialist_reservations WHERE state != 'released' ORDER BY created_at"
        ).fetchall()
        return [_reservation(r) for r in rows]


def last_reservation(session_keys: list[str]) -> dict | None:
    """The newest reservation, in any state, recorded under any of ``session_keys`` (one session's
    keys: the one a relaunch names and the physical one its master runs under), or None. A
    relaunch of a session started from a specialist re-reserves THAT specialist's version."""
    keys = []
    for k in session_keys:
        with contextlib.suppress(StoreError):
            keys.append(canonical_key(k))
    if not keys or not db_path().exists():
        return None
    marks = ", ".join("?" for _ in keys)
    with _read() as con:
        r = con.execute(
            "SELECT * FROM specialist_reservations"  # noqa: S608 — placeholders only
            f" WHERE session_key IN ({marks}) ORDER BY created_at DESC, rowid DESC LIMIT 1",
            tuple(keys),
        ).fetchone()
        return None if r is None else _reservation(r)


def active_counts() -> tuple[dict[str, int], int]:
    """``({specialist_id: active reservations}, total)``."""
    counts: dict[str, int] = {}
    for r in active():
        counts[r["specialist_id"]] = counts.get(r["specialist_id"], 0) + 1
    return counts, sum(counts.values())
