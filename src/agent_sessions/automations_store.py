"""`automations.db` — the ONE store for automations (#1201 round 3).

Configuration, consent receipts, slot claims, per-automation watermarks, runs and run steps live in
one SQLite file, so "read the consent, claim the slot, record the run" is ONE transaction
(`begin_run`) and there is no second store for that boundary to straddle.

* **Location** ``~/.config/agent-sessions/automations.db``, override
  ``AGENT_SESSIONS_AUTOMATIONS_DB``. Created ``0600`` before SQLite can create it under the umask.
* **Versioned by ``PRAGMA user_version``**, forward migrations only, with a backup copy taken before
  the first migration of an existing file. A file from a NEWER build is refused and never written.
* **Claims are not history.** ``automation_claims`` and ``automation_watermarks`` are never touched
  by retention, which prunes ``automation_runs`` (and their steps) only. Pruning history can never
  make an old slot fire again.
* **A run is written ``dispatching`` BEFORE its effect**, carrying the scope it runs under. A run
  still ``dispatching`` when an owner starts is ``interrupted``: outcome unknown, never replayed.
* **Clients never write receipts or results.** Every column here is written by this module from
  server-derived values; the routes validate a closed set of config fields and nothing else.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from . import automations as model

SCHEMA_VERSION = 5

#: The claim's refusal when the consent receipt no longer equals the config's scope (a receipt from
#: before a scope field existed, or a hand edit). Never run; the due check and Run now both flag it
#: *needs re-approval* so the operator can approve the full scope.
RECEIPT_MISMATCH = (
    "this automation's approval is out of date — the consent now shows everything it does; "
    "review it and approve it again"
)
BUSY_TIMEOUT_MS = 5000

#: Run history bounds (retention prunes runs only).
#: At least the daily cap's maximum, so pruning can never make room under a cap (#1201 review).
RUNS_KEEP_PER_AUTOMATION = 300
#: Runs this recent are never pruned: the daily cap counts them (a local day is ≤ 25 h).
RUNS_PROTECTED_S = 26 * 3600
RUNS_MAX_AGE_S = 90 * 86400
AUTOMATIONS_MAX = 200

#: Outcomes, and the class each one shows as on the success strip (worst of a day wins).
OUTCOME_CLASS = {
    "ok": "ok",
    "started": "pending",  # a mission was dispatched and has not concluded yet
    "review": "pending",  # the mission is waiting for the operator's review
    "failed": "failed",
    "refused": "failed",
    "interrupted": "failed",  # outcome unknown — never shown as a success
    "skipped": "skipped",
    # The operator turned it off, paused or changed it during the run: not a failure.
    "stopped": "skipped",
    # Text was typed into the session but not submitted (#1201 review): shown as a failure, but
    # it pauses the automation at once instead of counting toward pause_after_failures.
    "partial": "failed",
    # Written only by the retired review loop (#2202, removed in #2222), and kept so its old runs
    # still classify: a stale, unknown or duplicate delivery, a revision past the item's review
    # cap, and a verdict that could not reach its session.
    "ignored": "skipped",
    "capped": "skipped",
    "undelivered": "failed",
}
PARTIAL_REASON = "text was typed but not submitted — check the terminal"
#: Outcomes that count toward `pause_after_failures` and open a failure episode. `interrupted` is
#: honest ignorance, not a failure the automation caused, so it does neither.
FAILURE_OUTCOMES = frozenset({"failed", "refused"})
#: Runs that hold a concurrency slot: in flight, or a mission that has not concluded.
ACTIVE_OUTCOMES = frozenset({"started"})

#: The step a mission run records BEFORE `create_mission`, so a crash between the create and the
#: link is reported as "a mission may exist" rather than as nothing.
MISSION_PENDING_STEP = "creating_mission"
MISSION_MAY_EXIST = (
    " A mission may have been created before the stop; it is not linked here because that "
    "could not be recorded."
)

#: The automation queue (#2422). A run that would launch a session while a launch limit is full
#: (or a webhook delivery over its automation's `max_concurrent`) is ``queued``: no effect yet, its
#: claim and its one count toward the daily cap stand, and the owner's drain starts it when a slot
#: frees. It never counts toward `max_concurrent` or `pause_after_failures`.
QUEUE_MAX_WAIT_S = 6 * 3600.0
QUEUE_EXPIRED_REASON = "failed: no free slot for 6 h"
#: Which limit a queued run waits for (`queue_capacity`). ``concurrency``: its own automation's
#: previous run (a webhook delivery over `max_concurrent`).
QUEUE_CAPACITIES = ("session_limit", "pool", "specialist", "concurrency")
QUEUE_CONCURRENCY_DETAIL = "this automation's previous run is still active"

INTERRUPTED_REASON = (
    "interrupted: outcome unknown — the app stopped while this run was starting, so it may or "
    "may not have started; it is never retried"
)

_SCHEMA = """
CREATE TABLE automations (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  config TEXT NOT NULL,
  pins TEXT NOT NULL,
  revision INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  paused INTEGER NOT NULL DEFAULT 0,
  paused_reason TEXT NOT NULL DEFAULT '',
  needs_reapproval INTEGER NOT NULL DEFAULT 0,
  reapproval_reason TEXT NOT NULL DEFAULT '',
  consented_at REAL,
  consented_scope TEXT,
  active_since REAL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  failure_episode TEXT NOT NULL DEFAULT '',
  check_note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE automation_claims (
  automation_id TEXT NOT NULL,
  slot TEXT NOT NULL,
  fire_at REAL,
  claimed_at REAL NOT NULL,
  run_id TEXT NOT NULL,
  PRIMARY KEY (automation_id, slot)
);
CREATE TABLE automation_watermarks (
  automation_id TEXT PRIMARY KEY,
  slot TEXT NOT NULL,
  fire_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE automation_runs (
  id TEXT PRIMARY KEY,
  automation_id TEXT NOT NULL,
  trigger TEXT NOT NULL,
  slot TEXT,
  fire_at REAL,
  catch_up INTEGER NOT NULL DEFAULT 0,
  covered INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  inputs TEXT NOT NULL DEFAULT '{}',
  mission_id TEXT,
  session_key TEXT,
  counts INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  finished_at REAL
);
CREATE INDEX automation_runs_by_automation ON automation_runs(automation_id, created_at);
CREATE TABLE automation_run_steps (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  at REAL NOT NULL,
  step TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE automation_origins (
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  automation_id TEXT NOT NULL,
  automation_name TEXT NOT NULL,
  run_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

#: The review loop's tables (#2202, schema v2). The review loop was retired (#2222); the tables
#: stay so the migration chain runs on every file and `delete()` still cleans an old row's records.
#: A fresh file creates them with the v1 schema and then runs the v1 → v2 step like an existing
#: one, so both paths are the same SQL.
_SCHEMA_V2 = """
CREATE TABLE automation_hook_secrets (
  automation_id TEXT NOT NULL,
  gen INTEGER NOT NULL,
  envelope TEXT NOT NULL,
  created_at REAL NOT NULL,
  retire_at REAL,
  PRIMARY KEY (automation_id, gen)
);
CREATE TABLE automation_hook_stats (
  automation_id TEXT PRIMARY KEY,
  last_at REAL,
  last_event TEXT NOT NULL DEFAULT '',
  last_note TEXT NOT NULL DEFAULT '',
  unsigned_count INTEGER NOT NULL DEFAULT 0,
  unsigned_since REAL
);
CREATE TABLE automation_review_items (
  automation_id TEXT NOT NULL,
  item TEXT NOT NULL,
  reviews INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (automation_id, item)
);
CREATE TABLE automation_review_records (
  id TEXT PRIMARY KEY,
  automation_id TEXT NOT NULL,
  item TEXT NOT NULL,
  revision TEXT NOT NULL,
  round INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  session_key TEXT,
  state TEXT NOT NULL,
  verdict TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  accepted_at REAL
);
CREATE INDEX automation_review_records_by_item ON automation_review_records(automation_id, item);
"""


def _to_v2(con: sqlite3.Connection) -> None:
    for stmt in _SCHEMA_V2.split(";"):
        if stmt.strip():
            con.execute(stmt)


#: Webhooks as a list (#2221 P1, schema v3).
_SCHEMA_V3 = """
CREATE TABLE webhooks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  legacy_automation TEXT UNIQUE,
  last_at REAL,
  last_event TEXT NOT NULL DEFAULT '',
  last_note TEXT NOT NULL DEFAULT '',
  last_shape TEXT,
  unsigned_count INTEGER NOT NULL DEFAULT 0,
  unsigned_since REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE webhook_secrets (
  webhook_id TEXT NOT NULL,
  gen INTEGER NOT NULL,
  envelope TEXT NOT NULL,
  aad TEXT NOT NULL,
  created_at REAL NOT NULL,
  retire_at REAL,
  PRIMARY KEY (webhook_id, gen)
);
"""


def _to_v3(con: sqlite3.Connection) -> None:
    """Webhooks become a list. Each #2202 review loop's own webhook moves in UNCHANGED: same id as
    its automation (`legacy_automation`), same secret envelopes with the AAD they were sealed
    under, same counters. The automation's configuration is not touched. (Since #2222 retired the
    review loop, such an automation loads unreadable and its address answers the uniform 404.)"""
    for stmt in _SCHEMA_V3.split(";"):
        if stmt.strip():
            con.execute(stmt)
    _adopt_legacy_webhooks(con)


def _adopt_legacy_webhooks(con: sqlite3.Connection) -> None:
    """Copy every #2202 review loop's own webhook (secret envelopes with their AAD, counters) into
    the webhook list. Idempotent: an automation that already has its row is left alone, so a
    re-run (or a restart mid-migration, which rolls back anyway) never duplicates or resets."""
    for a in con.execute("SELECT id, name, config, created_at FROM automations").fetchall():
        try:
            trig = (json.loads(a["config"]) or {}).get("trigger") or {}
        except (TypeError, ValueError):
            continue
        if trig.get("kind") != "webhook":
            continue
        aid = a["id"]
        if con.execute("SELECT 1 FROM webhooks WHERE id=?", (aid,)).fetchone():
            continue
        st = con.execute(
            "SELECT * FROM automation_hook_stats WHERE automation_id=?", (aid,)
        ).fetchone()
        con.execute(
            "INSERT INTO webhooks (id, name, revision, legacy_automation, last_at, last_event, "
            "last_note, unsigned_count, unsigned_since, created_at, updated_at) "
            "VALUES (?,?,1,?,?,?,?,?,?,?,?)",
            (
                aid,
                str(a["name"])[:60] or aid,
                aid,
                st["last_at"] if st else None,
                st["last_event"] if st else "",
                st["last_note"] if st else "",
                int(st["unsigned_count"]) if st else 0,
                st["unsigned_since"] if st else None,
                a["created_at"],
                a["created_at"],
            ),
        )
        for sec in con.execute(
            "SELECT * FROM automation_hook_secrets WHERE automation_id=?", (aid,)
        ).fetchall():
            con.execute(
                "INSERT INTO webhook_secrets (webhook_id, gen, envelope, aad, created_at, "
                "retire_at) VALUES (?,?,?,?,?,?)",
                (
                    aid,
                    sec["gen"],
                    sec["envelope"],
                    f"automation-webhook:{aid}:{sec['gen']}",
                    sec["created_at"],
                    sec["retire_at"],
                ),
            )


def _to_v4(con: sqlite3.Connection) -> None:
    """A webhook's relay address for a Home Free box (#2221 P1b)."""
    con.execute("ALTER TABLE webhooks ADD COLUMN relay_public_id TEXT")
    con.execute("ALTER TABLE webhooks ADD COLUMN relay_url TEXT")


def _to_v5(con: sqlite3.Connection) -> None:
    """The automation queue (#2422): which launch limit a ``queued`` run waits for, the
    automation's open "no free slot" episode, and the index the drain and expiry walk."""
    con.execute("ALTER TABLE automation_runs ADD COLUMN queue_capacity TEXT NOT NULL DEFAULT ''")
    con.execute("ALTER TABLE automations ADD COLUMN queue_episode TEXT NOT NULL DEFAULT ''")
    con.execute("CREATE INDEX automation_runs_by_state ON automation_runs(state, created_at, id)")


#: ``{from_version: step}`` — each step brings a file from that version to the next.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _to_v2,
    2: _to_v3,
    3: _to_v4,
    4: _to_v5,
}


class StoreError(RuntimeError):
    def __init__(self, message: str, status: int = 500) -> None:
        super().__init__(message)
        self.status = status


class StoreUnsupported(StoreError):
    """The file was written by a newer build. Refused, never rewritten."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=503)


class NotFound(StoreError):
    def __init__(self, what: str = "automation") -> None:
        super().__init__(f"unknown {what}", status=404)


class Conflict(StoreError):
    def __init__(self, message: str = "the automation changed since you loaded it") -> None:
        super().__init__(message, status=409)


# ---- path + connection --------------------------------------------------------------------------


def db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_AUTOMATIONS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "automations.db"),
        )
    )


def _touch_0600(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        with contextlib.suppress(OSError):
            os.chmod(p, 0o600)
        return
    os.close(fd)


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


def _version_of(p: Path) -> int:
    """``user_version`` read WITHOUT creating or writing anything."""
    if not p.exists() or p.stat().st_size == 0:
        return 0
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0])
    finally:
        con.close()


def _backup(p: Path, version: int) -> Path:
    dest = p.with_name(f"{p.name}.bak-v{version}-{int(time.time())}")
    src = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.close(fd)
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def _migrate(p: Path) -> None:
    version = _version_of(p)
    if version > SCHEMA_VERSION:
        raise StoreUnsupported(
            f"automations store is at schema {version}, newer than this build ({SCHEMA_VERSION}); "
            "it is left untouched"
        )
    if version == SCHEMA_VERSION:
        return
    _touch_0600(p)
    if version > 0:
        # BEFORE the first migration of an existing file, a complete copy beside it.
        _backup(p, version)
    con = _raw_connect(p)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock: a peer may have migrated it since the check above.
            now_v = int(con.execute("PRAGMA user_version").fetchone()[0])
            if now_v > SCHEMA_VERSION:
                raise StoreUnsupported("automations store is newer than this build")
            if now_v == 0:
                for stmt in _SCHEMA.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
                now_v = 1
            for v in range(now_v, SCHEMA_VERSION):
                _MIGRATIONS[v](con)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def _connect() -> sqlite3.Connection:
    p = db_path()
    key = str(p)
    if key not in _schema_done:
        with _schema_lock:
            if key not in _schema_done:
                _migrate(p)
                _schema_done.add(key)
    if not p.exists():  # deleted under us (tests): rebuild once
        with _schema_lock:
            _schema_done.discard(key)
        return _connect()
    return _raw_connect(p)


def _refuse_newer(con: sqlite3.Connection) -> None:
    """A peer running a NEWER build may have migrated the file after this process cached its
    schema check. Asked on every connection — inside the write lock for a write — so this build
    never writes, or misreads, a schema it does not know."""
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise StoreUnsupported(
            f"automations store is at schema {version}, newer than this build ({SCHEMA_VERSION}); "
            "it is left untouched"
        )


@contextlib.contextmanager
def _tx():
    """One IMMEDIATE transaction: every read inside it sees exactly what its writes build on."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            _refuse_newer(con)
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")
    finally:
        con.close()


@contextlib.contextmanager
def _read():
    con = _connect()
    try:
        _refuse_newer(con)  # a clean refusal (503), never a crash on columns it does not know
        yield con
    finally:
        con.close()


# ---- run ownership -------------------------------------------------------------------------------
#
# A `dispatching` run belongs to the process that claimed it. Its token is `pid:start:instance`,
# where `start` is the process start time from /proc (so a recycled pid is not the same owner).
# Any owner's due check recovers `dispatching` runs whose owner is gone — and never touches a run
# whose owner is alive, which is what lets a peer's Run now finish on its own.

_OWNER: tuple[int, str] | None = None


def _proc_start(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 22 (starttime), counted after the parenthesised command name, which may hold spaces.
    fields = raw.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def owner_token() -> str:
    global _OWNER
    pid = os.getpid()
    if _OWNER is None or _OWNER[0] != pid:
        _OWNER = (pid, f"{pid}:{_proc_start(pid) or '?'}:{uuid.uuid4().hex[:8]}")
    return _OWNER[1]


def owner_alive(token: str) -> bool:
    """Is the process that owns a run still the one running? Unparseable ⇒ not alive."""
    try:
        pid_s, start, _inst = token.split(":")
        pid = int(pid_s)
    except ValueError:
        return False
    if token == owner_token():
        return True
    if pid <= 0 or start == "?":
        return False
    return _proc_start(pid) == start


# ---- rows ---------------------------------------------------------------------------------------


def _loads(raw: object, default):
    try:
        v = json.loads(raw) if isinstance(raw, str) else default
    except ValueError:
        return default
    return v if v is not None else default


def _row(r: sqlite3.Row) -> dict:
    """A stored automation, read LENIENTLY: an unreadable config runs nothing (``config: None``)."""
    return {
        "id": r["id"],
        "name": r["name"],
        "config": model.coerce_config(_loads(r["config"], None)),
        "pins": _loads(r["pins"], {}),
        "revision": int(r["revision"]),
        "enabled": bool(r["enabled"]),
        "paused": bool(r["paused"]),
        "paused_reason": r["paused_reason"] or "",
        "needs_reapproval": bool(r["needs_reapproval"]),
        "reapproval_reason": r["reapproval_reason"] or "",
        "consented_at": r["consented_at"],
        "consented_scope": _loads(r["consented_scope"], None),
        "active_since": r["active_since"],
        "consecutive_failures": int(r["consecutive_failures"]),
        "failure_episode": r["failure_episode"] or "",
        "check_note": r["check_note"] or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _run(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "automation_id": r["automation_id"],
        "trigger": r["trigger"],
        "slot": r["slot"],
        "fire_at": r["fire_at"],
        "catch_up": bool(r["catch_up"]),
        "covered": int(r["covered"]),
        "state": r["state"],
        "outcome": r["outcome"],
        "result_class": OUTCOME_CLASS.get(r["outcome"], "pending") if r["outcome"] else "pending",
        "reason": r["reason"],
        "scope": _loads(r["scope"], {}),
        "inputs": _loads(r["inputs"], {}),
        "mission_id": r["mission_id"],
        "session_key": r["session_key"],
        "counts": bool(r["counts"]),
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
        "queue_capacity": r["queue_capacity"] if "queue_capacity" in r.keys() else "",
    }


#: An automation id: ASCII lowercase letters and digits (they are minted as 16 hex characters).
ID_RE = re.compile(r"[a-z0-9]{1,64}", re.ASCII)


def _get(con, aid: str) -> dict:
    if not isinstance(aid, str) or not ID_RE.fullmatch(aid):
        raise NotFound()  # malformed is simply unknown: 404, and never a lock file
    r = con.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
    if r is None:
        raise NotFound()
    return _row(r)


# ---- automations --------------------------------------------------------------------------------


def _check_webhook_ref(con, config: dict | None) -> None:
    """A picked webhook must exist and be pickable — read INSIDE the writing transaction, so a
    concurrent `webhooks.delete` (which checks references inside its own) can never leave an
    automation pointing at nothing (#2221)."""
    trig = (config or {}).get("trigger") or {}
    wid = trig.get("webhook_id") if trig.get("kind") == "webhook" else None
    if not wid:
        return
    r = con.execute("SELECT legacy_automation FROM webhooks WHERE id=?", (wid,)).fetchone()
    if r is None or r["legacy_automation"]:
        raise StoreError("that webhook doesn't exist (or can't be picked)", 422)


def source_of(aid: str, config: dict | None) -> str | None:
    """The webhook a delivery for automation ``aid`` must have come through: the one it picked."""
    trig = (config or {}).get("trigger") or {}
    if trig.get("kind") != "webhook":
        return None
    return trig.get("webhook_id")


def create(config: dict, pins: dict, *, now: float | None = None) -> dict:
    ts = time.time() if now is None else now
    aid = uuid.uuid4().hex[:16]
    with _tx() as con:
        _check_webhook_ref(con, config)
        n = con.execute("SELECT COUNT(*) FROM automations").fetchone()[0]
        if n >= AUTOMATIONS_MAX:
            raise StoreError(f"at most {AUTOMATIONS_MAX} automations", status=409)
        con.execute(
            "INSERT INTO automations (id, name, config, pins, revision, created_at, updated_at) "
            "VALUES (?,?,?,?,1,?,?)",
            (aid, config["name"], json.dumps(config), json.dumps(pins), ts, ts),
        )
        return _get(con, aid)


def get(aid: str) -> dict:
    with _read() as con:
        return _get(con, aid)


def list_all() -> list[dict]:
    with _read() as con:
        return [_row(r) for r in con.execute("SELECT * FROM automations ORDER BY created_at")]


_JSON_COLUMNS = ("config", "pins", "consented_scope")


def mutate(
    aid: str, fn: Callable[[dict], dict | None], *, expect_revision: int | None = None
) -> dict:
    """Read-modify-write one automation under ONE transaction.

    ``fn(row)`` returns the columns to set (``None`` = nothing). A stale ``expect_revision`` is a
    :class:`Conflict`, and nothing is written."""
    with _tx() as con:
        row = _get(con, aid)
        if expect_revision is not None and row["revision"] != expect_revision:
            raise Conflict()
        updates = fn(row)
        if not updates:
            return row
        if "config" in updates:
            _check_webhook_ref(con, updates["config"])
        enc = {
            k: (json.dumps(v) if k in _JSON_COLUMNS and v is not None else v)
            for k, v in updates.items()
        }
        enc["revision"] = row["revision"] + 1
        enc["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in enc)
        con.execute(f"UPDATE automations SET {cols} WHERE id=?", (*enc.values(), aid))  # noqa: S608
        return _get(con, aid)


def delete(aid: str, *, expect_revision: int | None = None) -> None:
    with _tx() as con:
        row = _get(con, aid)
        if expect_revision is not None and row["revision"] != expect_revision:
            raise Conflict()
        active = con.execute(
            "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND state='dispatching'",
            (aid,),
        ).fetchone()[0]
        if active:
            raise StoreError("a run of this automation is starting — try again in a moment", 409)
        # The id is random and never reused, so its claims can go with it; ORIGINS stay, so work it
        # started is still marked as automated after the automation is gone.
        con.execute(
            "DELETE FROM automation_run_steps WHERE run_id IN "
            "(SELECT id FROM automation_runs WHERE automation_id=?)",
            (aid,),
        )
        # A #2202 review loop's OWN webhook (migrated into the list) goes with it, secrets included;
        # a shared webhook it merely picked stays.
        for legacy in con.execute(
            "SELECT id FROM webhooks WHERE legacy_automation=?", (aid,)
        ).fetchall():
            con.execute("DELETE FROM webhook_secrets WHERE webhook_id=?", (legacy["id"],))
            con.execute("DELETE FROM webhooks WHERE id=?", (legacy["id"],))
        for table in (
            "automation_runs",
            "automation_claims",
            "automation_watermarks",
            "automation_hook_secrets",
            "automation_hook_stats",
            "automation_review_items",
            "automation_review_records",
        ):
            con.execute(f"DELETE FROM {table} WHERE automation_id=?", (aid,))  # noqa: S608
        con.execute("DELETE FROM automations WHERE id=?", (aid,))


def watermark(aid: str) -> dict | None:
    with _read() as con:
        r = con.execute(
            "SELECT slot, fire_at FROM automation_watermarks WHERE automation_id=?", (aid,)
        ).fetchone()
        return {"slot": r["slot"], "fire_at": r["fire_at"]} if r else None


# ---- the claim ----------------------------------------------------------------------------------


def _eligibility(
    con, row: dict, *, manual: bool, now: float, queue_concurrency: bool = False
) -> tuple[str, str]:
    """``(verdict, reason)``, read INSIDE the claim transaction. ``verdict`` is ``run``,
    ``skip`` (claim the slot, record a skipped run) or ``no`` (no claim, no record).
    ``queue_concurrency`` (#2422, a webhook delivery that launches a session): over
    ``max_concurrent`` is ``queue`` instead of ``skip``."""
    config = row["config"]
    if config is None:
        return "no", "this automation's stored configuration can't be read"
    if row["consented_at"] is None or row["consented_scope"] is None:
        return "no", "this automation has never been approved"
    if row["needs_reapproval"]:
        return "no", row["reapproval_reason"] or "this automation needs re-approval"
    if not row["enabled"]:
        return "no", "this automation is turned off"
    if not manual and row["paused"]:
        # Run now on a PAUSED automation runs once without resuming; a schedule does not.
        return "no", "this automation is paused"
    if row["consented_scope"] != model.scope_of(config, row["pins"]):
        # The config and the receipt disagree — possible only through a hand edit or a bug. Never
        # run a scope nobody approved.
        return "no", RECEIPT_MISMATCH
    policy = config["policy"]
    if policy["expires_at"] is not None and now >= policy["expires_at"]:
        return "no", "this automation has expired"
    start, end = model.day_bounds(now, model.trigger_tz(config))
    fired = con.execute(
        "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND counts=1 "
        "AND created_at>=? AND created_at<?",
        (row["id"], start, end),
    ).fetchone()[0]
    if fired >= policy["max_runs_per_day"]:
        return "skip", "skipped: daily cap reached"
    active = con.execute(
        # In flight, or a dispatched mission that has not concluded (`ACTIVE_OUTCOMES`).
        "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND "
        "(state='dispatching' OR outcome='started')",
        (row["id"],),
    ).fetchone()[0]
    if active >= policy["max_concurrent"]:
        if queue_concurrency:
            return "queue", QUEUE_CONCURRENCY_DETAIL
        return "skip", "skipped: previous run still active"
    return "run", ""


def _stale_claim(con, row: dict, slot: str, now: float, expect_revision: int) -> str:
    """Why a scheduled claim no longer matches the CURRENT row, or ``""``: a different revision
    than the one the due check read, or a slot that is not the due slot of the current trigger."""
    if row["revision"] != expect_revision:
        return "the automation changed after its due check"
    config = row["config"]
    if config is None:
        return "this automation's stored configuration can't be read"
    mark = con.execute(
        "SELECT fire_at FROM automation_watermarks WHERE automation_id=?", (row["id"],)
    ).fetchone()
    base = max(
        float(row["active_since"] or row["consented_at"] or now),
        float(mark["fire_at"]) if mark else float("-inf"),
    )
    due = model.due_slots(config["trigger"], base, now)
    if not due["count"] or due["last"][0] != slot:
        return "that slot is not due under the current schedule"
    return ""


def begin_run(
    aid: str,
    *,
    trigger: str,
    slot: str,
    fire_at: float | None,
    catch_up: bool = False,
    covered: int = 1,
    now: float | None = None,
    force_skip: str = "",
    expect_revision: int | None = None,
) -> dict:
    """Claim ``(aid, slot)`` and record the run — ONE transaction (#1201 round 2).

    The consent, pause, expiry, cap and concurrency checks are re-read HERE, under the store's write
    lock, and the run is recorded ``dispatching`` with the scope it runs under before any effect. A
    disable or narrowing committed before this transaction therefore stops the run; one committed
    after it is acknowledged after the start record (the barrier, #1042).

    Returns ``{"run": dict | None, "claimed": bool, "reason": str}``. ``claimed`` is False when the
    slot was already claimed (a restart or a peer instance) — nothing is written then."""
    ts = time.time() if now is None else now
    manual = trigger == "manual"
    with _tx() as con:
        row = _get(con, aid)
        if con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?", (aid, slot)
        ).fetchone():
            return {"run": None, "claimed": False, "reason": "this slot has already run"}
        if not manual and expect_revision is not None:  # the scheduler's claim
            stale = _stale_claim(con, row, slot, ts, expect_revision)
            if stale:
                # The scheduler decided on an older row. Claim NOTHING; the next tick re-decides
                # on the current configuration.
                return {"run": None, "claimed": False, "reason": stale, "stale": True}
        verdict, reason = _eligibility(con, row, manual=manual, now=ts)
        if verdict == "run" and force_skip:
            verdict, reason = "skip", force_skip
        if verdict == "no":
            return {"run": None, "claimed": False, "reason": reason}
        run_id = uuid.uuid4().hex
        con.execute(
            "INSERT INTO automation_claims (automation_id, slot, fire_at, claimed_at, run_id) "
            "VALUES (?,?,?,?,?)",
            (aid, slot, fire_at, ts, run_id),
        )
        if not manual and fire_at is not None:
            con.execute(
                "INSERT INTO automation_watermarks (automation_id, slot, fire_at, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(automation_id) DO UPDATE SET "
                "slot=excluded.slot, fire_at=excluded.fire_at, updated_at=excluded.updated_at "
                "WHERE excluded.fire_at >= automation_watermarks.fire_at",
                (aid, slot, fire_at, ts),
            )
        scope = {
            "config": row["config"],
            "pins": row["pins"],
            "consented_scope": row["consented_scope"],
            "consented_at": row["consented_at"],
            "revision": row["revision"],
        }
        run = verdict == "run"
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, slot, fire_at, catch_up, "
            "covered, state, outcome, reason, scope, counts, owner, created_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                aid,
                trigger,
                slot,
                fire_at,
                int(catch_up),
                int(covered),
                "dispatching" if run else "done",
                "" if run else "skipped",
                reason,
                json.dumps(scope),
                1 if run else 0,
                owner_token() if run else "",
                ts,
                None if run else ts,
            ),
        )
        _step(con, run_id, "claimed" if run else "skipped", reason or _claim_detail(slot, covered))
        return {
            "run": _run(
                con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            ),
            "claimed": True,
            "reason": reason,
        }


def _claim_detail(slot: str, covered: int) -> str:
    if covered > 1:
        return f"catch-up run for {covered} missed slots, the latest {slot}"
    return f"slot {slot}"


# ---- run progress -------------------------------------------------------------------------------


def _step(con, run_id: str, step: str, detail: str = "") -> None:
    seq = con.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM automation_run_steps WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    con.execute(
        "INSERT INTO automation_run_steps (run_id, seq, at, step, detail) VALUES (?,?,?,?,?)",
        (run_id, seq, time.time(), step[:64], str(detail)[:1000]),
    )


def add_step(run_id: str, step: str, detail: str = "") -> None:
    with _tx() as con:
        _step(con, run_id, step, detail)


def set_inputs(run_id: str, inputs: dict) -> None:
    """The resolved inputs, as the operator may see them — MASKED text only (`[secret: name]`)."""
    with _tx() as con:
        con.execute("UPDATE automation_runs SET inputs=? WHERE id=?", (json.dumps(inputs), run_id))


def link(
    run_id: str,
    *,
    mission_id: str | None = None,
    session_key: str | None = None,
    origin: bool = True,
) -> None:
    """Attach what a run touched. ``origin`` also marks it as automated work (the origin badge) —
    true for a mission or session the run STARTED, false for a session it only typed into."""
    with _tx() as con:
        r = con.execute(
            "SELECT r.automation_id, a.name FROM automation_runs r JOIN automations a "
            "ON a.id=r.automation_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if r is None:
            return
        marks: list[tuple[str, str]] = []
        if mission_id:
            con.execute("UPDATE automation_runs SET mission_id=? WHERE id=?", (mission_id, run_id))
            marks.append((f"mission:{mission_id}", "mission"))
        if session_key:
            con.execute(
                "UPDATE automation_runs SET session_key=? WHERE id=?", (session_key, run_id)
            )
            marks.append((session_key, "session"))
        if not origin:
            return
        for key, kind in marks:
            con.execute(
                "INSERT OR IGNORE INTO automation_origins "
                "(key, kind, automation_id, automation_name, run_id, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (key, kind, r["automation_id"], r["name"], run_id, time.time()),
            )


def finish_run(run_id: str, outcome: str, reason: str = "", *, now: float | None = None) -> dict:
    """Record a run's outcome and settle the automation's failure accounting. ONE transaction.

    Returns ``{"run", "automation", "episode", "paused", "settled"}``, where ``episode`` is
    ``("open"|"close", key)`` or None, so the caller can notify OUTSIDE the transaction (the
    bell is a different store).

    **Settles ONCE.** Only a run still ``dispatching`` is settled (``WHERE state='dispatching'``):
    a second finish — a retry whose first attempt did land, a parked outcome replayed by a tick —
    neither double-counts a failure nor opens a second episode, and a late finish never overwrites
    a run already recorded ``interrupted``. Such a call returns the run as stored, ``settled:
    False``, with no episode."""
    if outcome not in OUTCOME_CLASS:
        raise ValueError(f"unknown outcome {outcome!r}")
    ts = time.time() if now is None else now
    with _tx() as con:
        r = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound("run")
        cur = con.execute(
            "UPDATE automation_runs SET state='done', outcome=?, reason=?, finished_at=? "
            "WHERE id=? AND state='dispatching'",
            (outcome, reason[:1000], ts, run_id),
        )
        if cur.rowcount != 1:
            return {
                "run": _run(r),
                "automation": None,
                "episode": None,
                "paused": False,
                "settled": False,
            }
        _step(con, run_id, outcome, reason)
        return {**_settle(con, r["automation_id"], run_id, outcome), "settled": True}


def _settle(con, aid: str, run_id: str, outcome: str) -> dict:
    episode = None
    paused = False
    row_r = con.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
    run = _run(con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone())
    if row_r is None:
        return {"run": run, "automation": None, "episode": None, "paused": False, "alert": None}
    row = _row(row_r)
    updates: dict = {}
    alert = None
    if outcome == "partial":
        # Never retried, never counted: paused at once, and announced once (its own key).
        if not row["paused"]:
            updates["paused"] = 1
            updates["paused_reason"] = PARTIAL_REASON
            paused = True
        alert = f"automation:{aid}:{run_id}:partial"
    elif outcome in FAILURE_OUTCOMES:
        n = row["consecutive_failures"] + 1
        updates["consecutive_failures"] = n
        if not row["failure_episode"]:
            # The episode key is server-derived only — never model-authored text (#1086).
            key = f"automation:{aid}:{run_id}"
            updates["failure_episode"] = key
            episode = ("open", key)
        limit = (
            (row["config"] or {})
            .get("policy", {})
            .get("pause_after_failures", model.PAUSE_AFTER_FAILURES_DEFAULT)
        )
        if n >= limit and not row["paused"]:
            updates["paused"] = 1
            updates["paused_reason"] = f"paused after {n} failed runs in a row"
            paused = True
    elif outcome == "ok":
        if row["consecutive_failures"]:
            updates["consecutive_failures"] = 0
        if row["failure_episode"]:
            episode = ("close", row["failure_episode"])
            updates["failure_episode"] = ""
    if updates:
        cols = ", ".join(f"{k}=?" for k in updates)
        con.execute(
            f"UPDATE automations SET {cols}, updated_at=? WHERE id=?",  # noqa: S608
            (*updates.values(), time.time(), aid),
        )
        row = _row(con.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone())
    return {"run": run, "automation": row, "episode": episode, "paused": paused, "alert": alert}


def conclude(run_id: str, outcome: str, reason: str) -> dict | None:
    """Move a ``started``/``review`` mission run to its conclusion. Idempotent."""
    with _tx() as con:
        r = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if r is None or r["outcome"] not in ("started", "review") or r["outcome"] == outcome:
            return None
        con.execute(
            "UPDATE automation_runs SET outcome=?, reason=?, finished_at=? WHERE id=?",
            (outcome, reason[:1000], time.time(), run_id),
        )
        _step(con, run_id, outcome, reason)
        if outcome in ("ok", "failed"):
            return _settle(con, r["automation_id"], run_id, outcome)
        return None


def pending_missions() -> list[dict]:
    with _read() as con:
        return [
            _run(r)
            for r in con.execute(
                "SELECT * FROM automation_runs WHERE outcome IN ('started','review') "
                "AND mission_id IS NOT NULL"
            )
        ]


#: The run steps that settle "close the session when it's done" (#2405): once one is recorded the
#: run's session is never looked at again.
CLOSE_SETTLED_STEPS = ("session_closed", "session_left_open")
#: How long after its run a session is still closed when done. Older ones are left alone.
CLOSE_WINDOW_S = 7 * 24 * 3600.0


def runs_to_close(*, now: float | None = None) -> list[dict]:
    """Runs whose consented scope says "close the session when it's done" (#2405), that started
    their session (`ok`) within `CLOSE_WINDOW_S`, and whose closing is not settled yet. The scope
    is the one the run was admitted under, so a later edit never closes an older run's session."""
    ts = time.time() if now is None else now
    with _read() as con:
        rows = con.execute(
            "SELECT * FROM automation_runs r WHERE r.outcome='ok' AND r.session_key IS NOT NULL "
            "AND r.created_at >= ? AND NOT EXISTS (SELECT 1 FROM automation_run_steps s "
            "WHERE s.run_id=r.id AND s.step IN (?,?))",
            (ts - CLOSE_WINDOW_S, *CLOSE_SETTLED_STEPS),
        ).fetchall()
    return [run for run in map(_run, rows) if close_chosen(run)]


def close_chosen(run: dict) -> bool:
    """Whether ``run`` was admitted under a consent that says "close the session when it's
    done": the CONSENTED scope stored with the run (`begin_run`'s ``consented_scope``), never
    its config."""
    consented = (run.get("scope") or {}).get("consented_scope")
    return isinstance(consented, dict) and consented.get("close_when_done") is True


def has_step(run_id: str, step: str) -> bool:
    with _read() as con:
        return (
            con.execute(
                "SELECT 1 FROM automation_run_steps WHERE run_id=? AND step=? LIMIT 1",
                (run_id, step),
            ).fetchone()
            is not None
        )


def recover_interrupted(*, now: float | None = None) -> list[str]:
    """Every run left ``dispatching`` by an owner that is GONE becomes ``interrupted`` — never
    resumed, never replayed. A run whose owner process is alive is never touched."""
    ts = time.time() if now is None else now
    with _tx() as con:
        rows = con.execute(
            "SELECT id, owner, mission_id FROM automation_runs WHERE state='dispatching'"
        ).fetchall()
        ids = []
        for r in rows:
            if owner_alive(r["owner"] or ""):
                continue
            pending = (
                r["mission_id"] is None
                and con.execute(
                    "SELECT 1 FROM automation_run_steps WHERE run_id=? AND step=?",
                    (r["id"], MISSION_PENDING_STEP),
                ).fetchone()
            )
            reason = INTERRUPTED_REASON + (MISSION_MAY_EXIST if pending else "")
            con.execute(
                "UPDATE automation_runs SET state='done', outcome='interrupted', reason=?, "
                "finished_at=? WHERE id=?",
                (reason, ts, r["id"]),
            )
            _step(con, r["id"], "interrupted", reason)
            ids.append(r["id"])
        return ids


def flag_reapproval(
    aid: str, reason: str, *, observed_revision: int | None, receipt_mismatch: bool = False
) -> dict:
    """Mark an automation *needs re-approval* and pause it — but only if the drift that was
    observed is STILL true: the row still has ``observed_revision`` and its consented pins still
    differ from the inputs as they are NOW. A consent (or an edit) that landed after the
    observation makes this a no-op, so a stale observation can never revoke newer consent."""

    def fn(row: dict) -> dict | None:
        if row["needs_reapproval"] or row["config"] is None:
            return None
        if observed_revision is not None and row["revision"] != observed_revision:
            return None
        if receipt_mismatch:
            # The receipt no longer matches its own config (a receipt from before a scope field
            # was added): still true under the lock, or this is a no-op.
            if row["consented_scope"] == model.scope_of(row["config"], row["pins"]):
                return None
            return {"needs_reapproval": 1, "reapproval_reason": reason, "paused": 1}
        try:
            current = model.compute_pins(row["config"])
        except model.PinsUnavailable:
            return None  # cannot tell now: not a drift
        if not model.pins_drift(row["pins"], current):
            return None
        return {"needs_reapproval": 1, "reapproval_reason": reason, "paused": 1}

    return mutate(aid, fn)


def set_check_note(aid: str, note: str) -> None:
    """Why the last due check could not decide (a transient resolution failure), or ``""``.
    Written WITHOUT a revision bump: it is an observation, not an edit."""
    with _tx() as con:
        con.execute("UPDATE automations SET check_note=? WHERE id=?", (note[:500], aid))


# ---- reads --------------------------------------------------------------------------------------


def list_runs(aid: str, *, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    with _read() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM automation_runs WHERE automation_id=?", (aid,)
        ).fetchone()[0]
        rows = con.execute(
            "SELECT * FROM automation_runs WHERE automation_id=? "
            "ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            (aid, limit, offset),
        ).fetchall()
        return {"runs": [with_queue(con, _run(r)) for r in rows], "total": int(total)}


def runs_since(aid: str, since: float) -> list[dict]:
    with _read() as con:
        return [
            _run(r)
            for r in con.execute(
                "SELECT * FROM automation_runs WHERE automation_id=? AND created_at>=? "
                "ORDER BY created_at",
                (aid, since),
            )
        ]


def last_run(aid: str) -> dict | None:
    with _read() as con:
        r = con.execute(
            "SELECT * FROM automation_runs WHERE automation_id=? ORDER BY created_at DESC LIMIT 1",
            (aid,),
        ).fetchone()
        return with_queue(con, _run(r)) if r else None


def get_run(run_id: str) -> dict:
    with _read() as con:
        r = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound("run")
        out = with_queue(con, _run(r))
        out["steps"] = [
            {"seq": s["seq"], "at": s["at"], "step": s["step"], "detail": s["detail"]}
            for s in con.execute(
                "SELECT * FROM automation_run_steps WHERE run_id=? ORDER BY seq", (run_id,)
            )
        ]
        return out


def origins() -> dict[str, dict]:
    with _read() as con:
        rows = con.execute(
            "SELECT o.key, o.kind, o.automation_id, o.run_id, "
            "COALESCE(a.name, o.automation_name) AS name, a.id IS NOT NULL AS present "
            "FROM automation_origins o LEFT JOIN automations a ON a.id=o.automation_id"
        ).fetchall()
        return {
            r["key"]: {
                "kind": r["kind"],
                "automation_id": r["automation_id"],
                "name": r["name"],
                "run_id": r["run_id"],
                "deleted": not r["present"],
            }
            for r in rows
        }


# ---- the automation queue (#2422) ---------------------------------------------------------------
#
# A queued run has NO effect yet: it was claimed (dedupe, daily cap) and admitted, and stopped at
# the step that would launch a session because a launch limit was full. It keeps its claim and
# its inputs; nothing but this section moves it. Its place in line is derived at read time
# (`(created_at, id)` among queued runs, across every automation), never stored.


def requeue(run_id: str, capacity: str, detail: str, *, now: float | None = None) -> bool:
    """``dispatching`` → ``queued``: the run's launch was refused by a limit BEFORE anything was
    created (the caller holds that proof). Only this process's own run, and only once: a run a
    recovery already recorded ``interrupted`` stays so. Counts nothing toward the pause."""
    if capacity not in QUEUE_CAPACITIES:
        raise ValueError(f"unknown queue capacity {capacity!r}")
    with _tx() as con:
        cur = con.execute(
            "UPDATE automation_runs SET state='queued', owner='', reason=?, queue_capacity=? "
            "WHERE id=? AND state='dispatching' AND owner=?",
            (detail[:1000], capacity, run_id, owner_token()),
        )
        if cur.rowcount != 1:
            return False
        _step(con, run_id, "queued", detail)
        return True


def note_waiting_many(notes: list[tuple[str, str, str]]) -> None:
    """``[(run_id, capacity, detail)]``: the limit each still-queued run waits for NOW, as one
    drain pass found it. ONE write transaction; a row already saying so is left untouched."""
    with _tx() as con:
        con.executemany(
            "UPDATE automation_runs SET reason=?, queue_capacity=? WHERE id=? AND state='queued' "
            "AND (reason!=? OR queue_capacity!=?)",
            [(d[:1000], c, rid, d[:1000], c) for rid, c, d in notes],
        )


def queue_length() -> int:
    with _read() as con:
        return int(
            con.execute("SELECT COUNT(*) FROM automation_runs WHERE state='queued'").fetchone()[0]
        )


def queue_waiting(run: dict) -> bool:
    """Whether a run OLDER than ``run`` waits in the queue: ``run`` then queues behind it and
    never takes a slot that waiter is owed (#2422 rule 4). A newer waiter (a delivery queued
    behind this run's own `max_concurrent`) never holds it back."""
    with _read() as con:
        return (
            con.execute(
                "SELECT 1 FROM automation_runs WHERE state='queued' AND "
                "(created_at < ? OR (created_at = ? AND id < ?)) LIMIT 1",
                (run["created_at"], run["created_at"], run["id"]),
            ).fetchone()
            is not None
        )


def queued_page(after: tuple[float, str] | None = None, limit: int = 50) -> list[dict]:
    """The next ``limit`` queued runs in line order ``(created_at, id)``, after ``after`` (keyset
    paging through the ``(state, created_at, id)`` index)."""
    with _read() as con:
        if after is None:
            rows = con.execute(
                "SELECT * FROM automation_runs WHERE state='queued' "
                "ORDER BY created_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM automation_runs WHERE state='queued' AND "
                "(created_at > ? OR (created_at = ? AND id > ?)) ORDER BY created_at, id LIMIT ?",
                (after[0], after[0], after[1], limit),
            ).fetchall()
        return [_run(r) for r in rows]


def _position(con, created_at: float, run_id: str) -> int:
    (ahead,) = con.execute(
        "SELECT COUNT(*) FROM automation_runs WHERE state='queued' AND "
        "(created_at < ? OR (created_at = ? AND id < ?))",
        (created_at, created_at, run_id),
    ).fetchone()
    return int(ahead) + 1


def with_queue(con, run: dict) -> dict:
    """``run`` with ``queue`` — its place in line (derived now), since when, and what it waits
    for — when it is queued; ``queue: None`` otherwise."""
    if run["state"] != "queued":
        return {**run, "queue": None}
    return {
        **run,
        "queue": {
            "position": _position(con, run["created_at"], run["id"]),
            "since": run["created_at"],
            "capacity": run.get("queue_capacity") or "",
            "detail": run.get("reason") or "",
        },
    }


#: `claim_queued` verdicts.
CLAIM_RUN, CLAIM_GONE, CLAIM_NOT_FIRST, CLAIM_CONCURRENCY, CLAIM_SETTLED = (
    "run",
    "gone",
    "not_first",
    "concurrency",
    "settled",
)


def _queued_consent(
    row: dict | None, run: sqlite3.Row, now: float, receipt_ok: bool | None = None
) -> str:
    """Why a queued run may no longer start (its automation as it is NOW), or ``""``.
    ``receipt_ok``: whether the row's receipt equals its config's scope, when the caller already
    knows (one answer per automation, not per run)."""
    if row is None:
        return "skipped: the automation was deleted while it waited"
    scope = _loads(run["scope"], {})
    if row["config"] is None:
        return "skipped: the automation can't be read any more"
    if not row["enabled"]:
        return "skipped: you turned it off while it waited"
    if row["needs_reapproval"]:
        return "skipped: it needs your approval again"
    if run["trigger"] != "manual" and row["paused"]:
        return "skipped: it was paused while it waited"
    if row["consented_scope"] != scope.get("consented_scope") or row["revision"] != scope.get(
        "revision"
    ):
        return "skipped: you changed it while it waited"
    if receipt_ok is None:
        receipt_ok = row["consented_scope"] == model.scope_of(row["config"], row["pins"])
    if not receipt_ok:
        return "skipped: " + RECEIPT_MISMATCH
    expires = (row["config"].get("policy") or {}).get("expires_at")
    if expires is not None and now >= expires:
        return "skipped: it expired while it waited"
    return ""


def settle_invalid_queued(*, now: float | None = None) -> list[str]:
    """Every queued run whose automation may no longer start it (turned off, deleted, paused,
    edited, needing re-approval, expired) → ``skipped`` with that reason, in ONE transaction,
    WHATEVER the launch limits say (Hermes on #2425): consent is reconciled before the drain
    asks any limit and before expiry, so a run the operator withdrew is never left waiting behind
    a full gate nor later recorded as a capacity failure. Counts nothing toward the pause. The
    walk is bounded by the queued rows (the daily caps bound them); each automation is read once.
    Returns the settled run ids."""
    ts = time.time() if now is None else now
    with _tx() as con:
        rows = con.execute(
            "SELECT * FROM automation_runs WHERE state='queued' ORDER BY created_at, id"
        ).fetchall()
        autos: dict[str, tuple[dict | None, bool]] = {}
        settled = []
        for r in rows:
            aid = r["automation_id"]
            if aid not in autos:
                a = con.execute("SELECT * FROM automations WHERE id=?", (aid,)).fetchone()
                row = _row(a) if a is not None else None
                ok = (
                    row is not None
                    and row["config"] is not None
                    and row["consented_scope"] == model.scope_of(row["config"], row["pins"])
                )
                autos[aid] = (row, ok)
            row, ok = autos[aid]
            why = _queued_consent(row, r, ts, receipt_ok=ok)
            if not why:
                continue
            con.execute(
                "UPDATE automation_runs SET state='done', outcome='skipped', reason=?, "
                "finished_at=? WHERE id=? AND state='queued'",
                (why, ts, r["id"]),
            )
            _step(con, r["id"], "skipped", why)
            settled.append(r["id"])
        return settled


def concurrency_full(aid: str) -> bool:
    """Whether automation ``aid`` already runs as many as its ``max_concurrent`` (an advisory
    read for the drain, so it takes no launch slot for a run that can't start; `claim_queued`
    decides in its own transaction)."""
    with _read() as con:
        a = con.execute("SELECT config FROM automations WHERE id=?", (aid,)).fetchone()
        if a is None:
            return False
        config = model.coerce_config(_loads(a["config"], None))
        if config is None:
            return False
        (active,) = con.execute(
            "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND "
            "(state='dispatching' OR outcome='started')",
            (aid,),
        ).fetchone()
        return active >= config["policy"]["max_concurrent"]


def claim_queued(run_id: str, *, passed: frozenset[str] | set[str] = frozenset(), now=None) -> dict:
    """``queued`` → ``dispatching`` — ONE transaction (#2422 admission rule 3).

    Inside it: the run must still be queued (another drainer did not take it); every OLDER queued
    run must be one the caller's walk found blocked (``passed``), so the run is the oldest
    ELIGIBLE one; consent is re-read (turned off, deleted, paused, edited, needing re-approval or
    expired → ``skipped`` with that reason, which counts toward nothing); and the automation's
    ``max_concurrent`` must have room. Only then is the run ``dispatching`` under this process,
    from where a crash is ``interrupted: outcome unknown`` and never retried. Takes no new claim
    and counts nothing toward the daily cap again: its original claim stands.

    Returns ``{"verdict", "run"}``."""
    ts = time.time() if now is None else now
    with _tx() as con:
        r = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if r is None or r["state"] != "queued":
            return {"verdict": CLAIM_GONE, "run": None}
        older = con.execute(
            "SELECT id FROM automation_runs WHERE state='queued' AND "
            "(created_at < ? OR (created_at = ? AND id < ?))",
            (r["created_at"], r["created_at"], run_id),
        ).fetchall()
        if any(o["id"] not in passed for o in older):
            return {"verdict": CLAIM_NOT_FIRST, "run": None}
        a = con.execute("SELECT * FROM automations WHERE id=?", (r["automation_id"],)).fetchone()
        row = _row(a) if a is not None else None
        why = _queued_consent(row, r, ts)
        if why:
            con.execute(
                "UPDATE automation_runs SET state='done', outcome='skipped', reason=?, "
                "finished_at=? WHERE id=? AND state='queued'",
                (why, ts, run_id),
            )
            _step(con, run_id, "skipped", why)
            got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            return {"verdict": CLAIM_SETTLED, "run": _run(got)}
        (active,) = con.execute(
            "SELECT COUNT(*) FROM automation_runs WHERE automation_id=? AND "
            "(state='dispatching' OR outcome='started')",
            (r["automation_id"],),
        ).fetchone()
        if active >= row["config"]["policy"]["max_concurrent"]:
            return {"verdict": CLAIM_CONCURRENCY, "run": None}
        con.execute(
            "UPDATE automation_runs SET state='dispatching', owner=? WHERE id=? AND state='queued'",
            (owner_token(), run_id),
        )
        _step(con, run_id, "dequeued", f"waited {max(0.0, ts - float(r['created_at'])):.0f} s")
        if a is not None and a["queue_episode"]:
            # A run of this automation starts again: its "no free slot" episode is over.
            con.execute("UPDATE automations SET queue_episode='' WHERE id=?", (r["automation_id"],))
        got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return {"verdict": CLAIM_RUN, "run": _run(got)}


def expire_queued(*, now: float | None = None, max_wait_s: float | None = None) -> list[dict]:
    """Every queued run older than `QUEUE_MAX_WAIT_S` → ``failed: no free slot for 6 h`` (#2422
    rule 7). A separate indexed update, never bounded by the drain, so a long blocked line can't
    stop it. It counts NOTHING toward `pause_after_failures` (a full limit is not the
    automation's fault), and opens at most ONE "no free slot" episode per automation until one of
    its runs starts again. Returns ``[{"automation_id", "name", "runs", "episode"}]`` — ``episode``
    is the bell key to announce, or ``""`` when that automation's episode is already open."""
    ts = time.time() if now is None else now
    cutoff = ts - (QUEUE_MAX_WAIT_S if max_wait_s is None else max_wait_s)
    with _tx() as con:
        rows = con.execute(
            "SELECT id, automation_id FROM automation_runs WHERE state='queued' AND created_at < ? "
            "ORDER BY created_at, id",
            (cutoff,),
        ).fetchall()
        if not rows:
            return []
        by_aid: dict[str, list[str]] = {}
        for r in rows:
            con.execute(
                "UPDATE automation_runs SET state='done', outcome='failed', reason=?, "
                "finished_at=? WHERE id=? AND state='queued'",
                (QUEUE_EXPIRED_REASON, ts, r["id"]),
            )
            _step(con, r["id"], "failed", QUEUE_EXPIRED_REASON)
            by_aid.setdefault(r["automation_id"], []).append(r["id"])
        out = []
        for aid, ids in by_aid.items():
            a = con.execute(
                "SELECT name, queue_episode FROM automations WHERE id=?", (aid,)
            ).fetchone()
            episode = ""
            if a is not None and not a["queue_episode"]:
                episode = f"automation:{aid}:noslot:{ids[0]}"
                con.execute("UPDATE automations SET queue_episode=? WHERE id=?", (episode, aid))
            out.append(
                {
                    "automation_id": aid,
                    "name": a["name"] if a is not None else "automation",
                    "runs": ids,
                    "episode": episode,
                }
            )
        return out


def launched_session_keys() -> set[str]:
    """Every session key an automation LAUNCHED (#2422): ``automation_origins`` rows of kind
    ``session``. Written only by a launch (`link(..., origin=True)`, before anything is spawned)
    and never deleted — not by pruning, not by deleting the automation — so the session-limit
    census can tell an automation's running session from any other after its launch slot is gone
    and across restarts. A session an automation only messaged has no such row.

    **A read that never writes.** The session-limit census asks this from every process that
    launches (a peer instance, a helper process), so it opens the file READ-ONLY and never through
    `_connect`: it never creates, migrates or backs up the store, whatever schema it is at (#2422:
    a process holding a store path it did not mean to have must not upgrade a live store). A file
    that is missing or unreadable, or lacks the table, answers empty (every session then counts as
    ``other``, the conservative side)."""
    p = db_path()
    if not p.exists():
        return set()
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return set()
    try:
        rows = con.execute("SELECT key FROM automation_origins WHERE kind='session'").fetchall()
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    return {r[0] for r in rows}


def launched_among(keys) -> set[str] | None:
    """Which of ``keys`` an automation LAUNCHED (#2437): the ``automation_origins`` rows of kind
    ``session`` among them. The session list asks this once per request with the keys it has in
    scope, so the cost follows the scoped rows and never the table's history, which is never
    pruned.

    Unlike :func:`launched_session_keys`, it tells "none" from "unknown": ``set()`` when the store
    file does not exist (no automation ever ran), ``None`` when it exists but cannot be opened or
    read, or has no ``automation_origins`` table. A caller must not read ``None`` as "none of
    these" — the list then says it could not tell.

    **A read that never writes**, for the same reason as :func:`launched_session_keys`: read-only
    URI, a bounded wait, never ``_connect`` (no create, migrate or backup)."""
    wanted = list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))
    p = db_path()
    if not p.exists():
        return set()
    try:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_origins'"
        ).fetchone()
        if has is None:
            return None
        # ONE bound parameter (a JSON array), never SQL built from the keys. `CROSS JOIN` pins the
        # loop order (SQLite never reorders it): the wanted keys outside, one primary-key probe
        # each inside, so the cost follows the keys asked and never the table's history. A plain
        # `key IN (SELECT value FROM json_each(?))` let the planner scan the whole table instead
        # (measured: 359 ms against 3 ms at 100,000 rows).
        rows = con.execute(
            "SELECT o.key FROM json_each(?) AS j CROSS JOIN automation_origins AS o "
            "ON o.key = j.value WHERE o.kind = 'session'",
            (json.dumps(wanted),),
        ).fetchall()
        out = {r[0] for r in rows}
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


# ---- retention ----------------------------------------------------------------------------------


def prune_runs(
    *, now: float | None = None, keep: int | None = None, max_age_s: float | None = None
) -> int:
    """Drop run HISTORY past its bounds. Claims and watermarks are never touched here, and
    neither is a run still waiting in the automation queue (#2422): it has not happened yet."""
    ts = time.time() if now is None else now
    # Floors, not defaults: whatever a caller asks, the daily cap's evidence is never pruned.
    keep = max(RUNS_KEEP_PER_AUTOMATION if keep is None else keep, model.MAX_RUNS_PER_DAY_MAX)
    max_age = RUNS_MAX_AGE_S if max_age_s is None else max_age_s
    with _tx() as con:
        doomed = [
            r["id"]
            for r in con.execute(
                "SELECT id FROM ("
                "  SELECT id, created_at, state, outcome, ROW_NUMBER() OVER ("
                "    PARTITION BY automation_id ORDER BY created_at DESC) AS rn"
                "  FROM automation_runs)"
                " WHERE state NOT IN ('dispatching','queued')"
                " AND outcome NOT IN ('started','review')"
                " AND (rn>? OR created_at<?) AND created_at<?",
                (keep, ts - max_age, ts - RUNS_PROTECTED_S),
            )
        ]
        for rid in doomed:
            con.execute("DELETE FROM automation_run_steps WHERE run_id=?", (rid,))
            con.execute("DELETE FROM automation_runs WHERE id=?", (rid,))
        return len(doomed)


# ---- the automation's webhook (#2221) ----------------------------------------------------------
#
# An automation listens on the webhook its trigger picked from the list; the secrets and counters
# are the webhook's own (`webhooks.py`). Never a secret here.


def webhook_of(aid: str) -> str | None:
    """The webhook automation ``aid`` listens on (the one its trigger picked), or None when it is
    not a webhook automation (or its config can't be read)."""
    with _read() as con:
        row = _get(con, aid)
    trig = (row["config"] or {}).get("trigger") or {}
    return trig.get("webhook_id") if trig.get("kind") == "webhook" else None


def hook_state(aid: str, *, now: float | None = None) -> dict:
    """The automation's webhook as its panel shows it. Never a secret."""
    from . import webhooks

    wid = webhook_of(aid)
    if not wid:
        return {"secret_set": False, "unsigned_24h": 0, "last_delivery_at": None}
    try:
        pub = webhooks.get(wid, now=now)
    except NotFound:
        return {"secret_set": False, "unsigned_24h": 0, "last_delivery_at": None}
    pub.pop("last_shape", None)
    return {**pub, "webhook_id": wid}


def note_delivery(aid: str, event: str, note: str, *, now: float | None = None) -> None:
    """A signed delivery reached automation ``aid``: noted on its webhook."""
    from . import webhooks

    wid = webhook_of(aid)
    if wid:
        webhooks.note_delivery(wid, event, note, now=now)


def record_delivery(
    aid: str,
    *,
    slot: str,
    outcome: str,
    reason: str,
    inputs: dict,
    source: str | None = None,
    expect_revision: int | None = None,
    now: float | None = None,
) -> dict | None:
    """A signed delivery that becomes a FINISHED run without an effect (``rejected`` → refused,
    ``ignored``) — ONE transaction (Hermes on #2225).

    Inside it: the automation must still listen on ``source`` (the webhook the delivery came
    through), be eligible like any run (`_eligibility`), and ``slot`` must be unclaimed. Only then
    are the claim, the run and its failure accounting (`_settle`) written — together, so a switch
    to another webhook can never charge a failure to the newly approved configuration. Returns the
    run, or None when nothing was recorded."""
    ts = time.time() if now is None else now
    with _tx() as con:
        row = _get(con, aid)
        if expect_revision is not None and row["revision"] != expect_revision:
            # Decided against a configuration that has since changed (#2221 P2): nothing is
            # claimed or counted; the caller decides again against the current one.
            return {"stale_config": True}
        if source is not None and source_of(aid, row["config"]) != source:
            return None  # the automation no longer listens on that webhook: not its delivery
        if con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?", (aid, slot)
        ).fetchone():
            return None
        verdict, _why = _eligibility(con, row, manual=False, now=ts)
        if verdict != "run":
            return None
        run_id = uuid.uuid4().hex
        scope = {
            "config": row["config"],
            "pins": row["pins"],
            "consented_scope": row["consented_scope"],
            "consented_at": row["consented_at"],
            "revision": row["revision"],
        }
        con.execute(
            "INSERT INTO automation_claims (automation_id, slot, fire_at, claimed_at, run_id) "
            "VALUES (?,?,?,?,?)",
            (aid, slot, None, ts, run_id),
        )
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, slot, fire_at, catch_up, "
            "covered, state, outcome, reason, scope, inputs, counts, owner, created_at, "
            "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                aid,
                "webhook",
                slot,
                None,
                0,
                1,
                "done",
                outcome,
                reason[:1000],
                json.dumps(scope),
                json.dumps(inputs),
                0,
                "",
                ts,
                ts,
            ),
        )
        _step(con, run_id, outcome, reason)
        if outcome in FAILURE_OUTCOMES:
            _settle(con, aid, run_id, outcome)
        got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return _run(got)


#: The actions a generic webhook delivery starts through `begin_delivery` (#2221 P2), and the
#: message to HQ (#2221).
DELIVERY_ACTIONS = ("start_session", "send_to_session", "tell_hq", "plugin")


def begin_delivery(
    aid: str,
    *,
    slot: str,
    expect_revision: int,
    inputs: dict,
    source: str,
    now: float | None = None,
) -> dict:
    """A generic webhook delivery's admission (#2221 P2) — ONE transaction, before any effect.

    Bound to the configuration the delivery was matched against (``expect_revision``) and to the
    webhook it came through (``source``): either changed → nothing is claimed and the answer
    carries ``stale_config``, so the caller matches the delivery again. Then it claims ``slot``
    (the body's fingerprint: a redelivered or duplicate body claims nothing twice), re-reads the
    consent / pause / expiry / daily cap / concurrency like every run (`_eligibility`), and records
    the run ``dispatching`` with ``inputs`` (the event label and the typed delivered values). A
    daily-cap or concurrency SKIP records the run WITHOUT consuming the claim, so a redeliver can
    still run. Returns ``{"run", "claimed", "reason"}``."""
    ts = time.time() if now is None else now
    with _tx() as con:
        row = _get(con, aid)
        config = row["config"] or {}
        if (
            row["revision"] != expect_revision
            or (config.get("action") or {}).get("kind") not in DELIVERY_ACTIONS
            or source_of(aid, row["config"]) != source
        ):
            return {
                "run": None,
                "claimed": False,
                "stale_config": True,
                "reason": "the automation changed while the delivery was being checked",
            }
        if con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?", (aid, slot)
        ).fetchone():
            return {"run": None, "claimed": False, "reason": "this delivery was already handled"}
        # A delivery that would launch a session over `max_concurrent` waits in the automation
        # queue instead of being skipped (#2422); every other action keeps its skip.
        launches = (config.get("action") or {}).get("kind") == "start_session"
        verdict, reason = _eligibility(con, row, manual=False, now=ts, queue_concurrency=launches)
        if verdict == "no":
            return {"run": None, "claimed": False, "reason": reason}
        run_id = uuid.uuid4().hex
        scope = {
            "config": row["config"],
            "pins": row["pins"],
            "consented_scope": row["consented_scope"],
            "consented_at": row["consented_at"],
            "revision": row["revision"],
        }
        queued = verdict == "queue"
        run = verdict == "run"
        if run or queued:
            con.execute(
                "INSERT INTO automation_claims (automation_id, slot, fire_at, claimed_at, run_id) "
                "VALUES (?,?,?,?,?)",
                (aid, slot, None, ts, run_id),
            )
        con.execute(
            "INSERT INTO automation_runs (id, automation_id, trigger, slot, fire_at, catch_up, "
            "covered, state, outcome, reason, scope, inputs, counts, owner, created_at, "
            "finished_at, queue_capacity) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                aid,
                "webhook",
                slot,
                None,
                0,
                1,
                "dispatching" if run else "queued" if queued else "done",
                "" if run or queued else "skipped",
                reason,
                json.dumps(scope),
                json.dumps(inputs),
                1 if run or queued else 0,
                owner_token() if run else "",
                ts,
                None if run or queued else ts,
                "concurrency" if queued else "",
            ),
        )
        # Recorded, NOT claimed when skipped: the delivery may come again (redeliver) and then run.
        # A queued one IS claimed and counted once: it runs when its turn comes.
        _step(
            con, run_id, "claimed" if run or queued else "skipped", reason or "a webhook delivery"
        )
        if queued:
            _step(con, run_id, "queued", reason)
        got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return {"run": _run(got), "claimed": run or queued, "reason": reason}
