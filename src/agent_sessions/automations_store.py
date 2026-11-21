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

SCHEMA_VERSION = 4

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
    # A review loop's deliveries (#2202): a verdict or delivery that is stale, unknown or a
    # duplicate, and a revision past the item's lifetime review cap. Neither is a failure.
    "ignored": "skipped",
    "capped": "skipped",
    # A verdict that could not reach the session that opened the item (no session record, a
    # terminal session, a busy or ended one). Shown as a failure and announced once on its own,
    # but not counted toward pause_after_failures: the review itself worked.
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

#: The review loop's tables (#2202, schema v2). A fresh file creates them with the v1 schema and
#: then runs the v1 → v2 step like an existing one, so both paths are the same SQL.
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
    under, same counters. The automation's configuration is not touched, so nothing needs
    re-approval and its address `/hooks/a/{id}` keeps routing to it alone."""
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


#: ``{from_version: step}`` — each step brings a file from that version to the next.
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _to_v2, 2: _to_v3, 3: _to_v4}


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
    """The webhook a delivery for automation ``aid`` must have come through: the picked one, or
    — for a #2202 review loop in its old form — its own (same id as the automation)."""
    trig = (config or {}).get("trigger") or {}
    if trig.get("kind") != "webhook":
        return None
    return trig.get("webhook_id") or aid


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


def _eligibility(con, row: dict, *, manual: bool, now: float) -> tuple[str, str]:
    """``(verdict, reason)``, read INSIDE the claim transaction. ``verdict`` is ``run``,
    ``skip`` (claim the slot, record a skipped run) or ``no`` (no claim, no record)."""
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
    elif outcome == "undelivered":
        alert = f"automation:{aid}:{run_id}:undelivered"
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
        return {"runs": [_run(r) for r in rows], "total": int(total)}


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
        return _run(r) if r else None


def get_run(run_id: str) -> dict:
    with _read() as con:
        r = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        if r is None:
            raise NotFound("run")
        out = _run(r)
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


# ---- retention ----------------------------------------------------------------------------------


def prune_runs(
    *, now: float | None = None, keep: int | None = None, max_age_s: float | None = None
) -> int:
    """Drop run HISTORY past its bounds. Claims and watermarks are never touched here."""
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
                " WHERE state!='dispatching' AND outcome NOT IN ('started','review')"
                " AND (rn>? OR created_at<?) AND created_at<?",
                (keep, ts - max_age, ts - RUNS_PROTECTED_S),
            )
        ]
        for rid in doomed:
            con.execute("DELETE FROM automation_run_steps WHERE run_id=?", (rid,))
            con.execute("DELETE FROM automation_runs WHERE id=?", (rid,))
        return len(doomed)


# ---- the review loop's webhook (#2202) -----------------------------------------------------------
#
# Read `docs/invariants/forge-review-loop.md`. The webhook secret is stored ONLY as an AES-GCM
# envelope (`template_secrets.encrypt`, AAD = ``automation-webhook:<id>:<gen>``); its plaintext is
# returned once, by the call that made it, and decrypted again only to verify a delivery.

#: How long a rotated-out secret keeps verifying deliveries, so the forge can be updated.
SECRET_OVERLAP_S = 24 * 3600
#: The unsigned-delivery counter's window.
UNSIGNED_WINDOW_S = 24 * 3600


def webhook_of(aid: str) -> str | None:
    """The webhook automation ``aid`` listens on: its own legacy one (#2202), or the one its
    trigger picked (#2221). None when it is not a webhook automation."""
    from . import webhooks

    legacy = webhooks.legacy_for(aid)
    if legacy:
        return legacy
    with _read() as con:
        row = _get(con, aid)
    trig = (row["config"] or {}).get("trigger") or {}
    return trig.get("webhook_id") if trig.get("kind") == "webhook" else None


def new_hook_secret(aid: str, *, now: float | None = None) -> str:
    """Rotate the secret of automation ``aid``'s OWN webhook (a #2202 review loop's) and return it
    — the only time it is shown. A webhook picked from the list is shared, so it is rotated on its
    own page (`webhooks.rotate`), never through one of its automations."""
    from . import webhooks

    with _read() as con:
        row = _get(con, aid)
    if (row["config"] or {}).get("trigger", {}).get("kind") != "webhook":
        raise StoreError("only a webhook automation has a webhook secret", 409)
    wid = webhooks.legacy_for(aid)
    if wid is None:
        raise StoreError("this automation uses a shared webhook: rotate it on its page", 409)
    return webhooks.rotate(wid, now=now)


def hook_secrets(aid: str, *, now: float | None = None) -> list[str]:
    from . import webhooks

    wid = webhook_of(aid)
    return webhooks.secrets(wid, now=now) if wid else []


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
DELIVERY_ACTIONS = ("start_session", "send_to_session", "tell_hq")


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
        verdict, reason = _eligibility(con, row, manual=False, now=ts)
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
        run = verdict == "run"
        if run:
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
                "dispatching" if run else "done",
                "" if run else "skipped",
                reason,
                json.dumps(scope),
                json.dumps(inputs),
                1 if run else 0,
                owner_token() if run else "",
                ts,
                None if run else ts,
            ),
        )
        # Recorded, NOT claimed when skipped: the delivery may come again (redeliver) and then run.
        _step(con, run_id, "claimed" if run else "skipped", reason or "a webhook delivery")
        got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return {"run": _run(got), "claimed": run, "reason": reason}


def begin_review(
    aid: str,
    *,
    slot: str,
    repo: str,
    item: str,
    revision: str,
    expect_revision: int,
    inputs: dict,
    source: str | None = None,
    now: float | None = None,
) -> dict:
    """The review leg's dispatch admission (#2202) — ONE transaction, before any launch.

    **Bound to the configuration the delivery was classified against**: ``expect_revision`` is the
    automation revision the caller read before its forge check. If the automation changed since
    (another repo, leg or cap, even with fresh consent), nothing is claimed and the answer carries
    ``stale_config`` so the caller classifies the delivery again against the current one. The cap
    is read from THIS transaction's configuration, never passed in.

    Then it claims ``slot``, re-reads the consent / pause / expiry / daily cap / concurrency like
    every run (`_eligibility`), and:

    * the item's lifetime review count (keyed by repository AND item) is below the cap → the count
      is incremented, older open records of the item are superseded, a ``dispatched`` review record
      is written and the run is recorded ``dispatching`` with that record in its inputs;
    * at the cap → the run is recorded ``capped`` (finished, not counted) and nothing launches.

    A daily-cap or concurrency SKIP records the run WITHOUT consuming the claim, so the forge's
    redeliver can still deliver it. Returns ``{"run", "claimed", "reason", "record", "round"}``."""
    from .review_loop import item_key

    ts = time.time() if now is None else now
    with _tx() as con:
        row = _get(con, aid)
        config = row["config"] or {}
        action = config.get("action") or {}
        if (
            row["revision"] != expect_revision
            or action.get("kind") != "forge_review_loop"
            or str(action.get("repo", "")).lower() != repo.lower()
            or (source is not None and source_of(aid, row["config"]) != source)
        ):
            return {
                "run": None,
                "claimed": False,
                "stale_config": True,
                "reason": "the automation changed while the delivery was being checked",
            }
        kind = item.partition("#")[0]
        cap = int((action.get("max_rounds") or {}).get(kind) or 0)
        key = item_key(repo, item)
        if con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?", (aid, slot)
        ).fetchone():
            return {"run": None, "claimed": False, "reason": "this revision was already reviewed"}
        verdict, reason = _eligibility(con, row, manual=False, now=ts)
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

        def insert(state: str, outcome: str, why: str, counts: int, run_inputs: dict) -> dict:
            con.execute(
                "INSERT INTO automation_runs (id, automation_id, trigger, slot, fire_at, "
                "catch_up, covered, state, outcome, reason, scope, inputs, counts, owner, "
                "created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    aid,
                    "webhook",
                    slot,
                    None,
                    0,
                    1,
                    state,
                    outcome,
                    why,
                    json.dumps(scope),
                    json.dumps(run_inputs),
                    counts,
                    owner_token() if state == "dispatching" else "",
                    ts,
                    None if state == "dispatching" else ts,
                ),
            )
            _step(con, run_id, outcome or "claimed", why or f"{item} at {revision}")
            got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
            return _run(got)

        if verdict == "skip":
            # Recorded, NOT claimed: the delivery may come again (redeliver) and then run.
            run = insert("done", "skipped", reason, 0, inputs)
            return {"run": run, "claimed": False, "reason": reason}
        con.execute(
            "INSERT INTO automation_claims (automation_id, slot, fire_at, claimed_at, run_id) "
            "VALUES (?,?,?,?,?)",
            (aid, slot, None, ts, run_id),
        )
        done = con.execute(
            "SELECT reviews FROM automation_review_items WHERE automation_id=? AND item=?",
            (aid, key),
        ).fetchone()
        reviews = int(done["reviews"]) if done else 0
        if reviews >= cap:
            why = f"{item} has had {reviews} of {cap} reviews; no review started for {revision}"
            run = insert("done", "capped", why, 0, {**inputs, "reviews": reviews, "cap": cap})
            return {"run": run, "claimed": True, "reason": why, "capped": True}
        n = reviews + 1
        con.execute(
            "INSERT INTO automation_review_items (automation_id, item, reviews, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(automation_id, item) DO UPDATE SET "
            "reviews=excluded.reviews, updated_at=excluded.updated_at",
            (aid, key, n, ts),
        )
        con.execute(
            "UPDATE automation_review_records SET state='superseded' WHERE automation_id=? AND "
            "item=? AND state='dispatched'",
            (aid, key),
        )
        record = uuid.uuid4().hex[:24]
        con.execute(
            "INSERT INTO automation_review_records (id, automation_id, item, revision, round, "
            "run_id, session_key, state, created_at) VALUES (?,?,?,?,?,?,NULL,'dispatched',?)",
            (record, aid, key, revision, n, run_id, ts),
        )
        run = insert(
            "dispatching",
            "",
            "",
            1,
            {**inputs, "record": record, "round": n, "cap": cap},
        )
        return {"run": run, "claimed": True, "reason": "", "record": record, "round": n}


def set_review_session(record: str, key: str) -> None:
    with _tx() as con:
        con.execute("UPDATE automation_review_records SET session_key=? WHERE id=?", (key, record))


def review_records(aid: str, item: str | None = None) -> list[dict]:
    """``item`` is the durable key (`review_loop.item_key`), or None for every record."""
    with _read() as con:
        if item is None:
            rows = con.execute(
                "SELECT * FROM automation_review_records WHERE automation_id=? ORDER BY "
                "created_at",
                (aid,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM automation_review_records WHERE automation_id=? AND item=? "
                "ORDER BY created_at",
                (aid, item),
            ).fetchall()
    return [dict(r) for r in rows]


def review_count(aid: str, item: str) -> int:
    with _read() as con:
        r = con.execute(
            "SELECT reviews FROM automation_review_items WHERE automation_id=? AND item=?",
            (aid, item),
        ).fetchone()
    return int(r["reviews"]) if r else 0


def accept_verdict(
    aid: str,
    *,
    slot: str,
    repo: str,
    expect_revision: int,
    record: str,
    item: str,
    revision: str,
    verdict: str,
    current: str,
    inputs: dict,
    source: str | None = None,
    now: float | None = None,
) -> dict:
    """The return leg's acceptance (#2202 PR 2) — ONE transaction, before any send.

    A verdict counts only when ``record`` is a review record THIS automation dispatched for
    ``item``, its revision is the marker's ``revision`` AND the forge's ``current`` one, and it is
    still ``dispatched`` (never superseded, never already answered). Then the record becomes
    ``verdict_accepted`` and the run is recorded: ``dispatching`` when the verdict is to be
    ``deliver``-ed to the originating session, else finished ``ok``. Anything else is a finished
    ``ignored`` run with its reason. ``slot`` (one per record) makes a second verdict for the same
    record — even under another signed body — record nothing at all.

    Bound, like `begin_review`, to the automation revision the delivery was classified against
    (``expect_revision``): a change since answers ``stale_config`` and claims nothing. Whether the
    verdict is delivered comes from THIS transaction's configuration. The record is looked up by the
    repository-qualified item key.

    Returns ``{"run", "claimed", "reason", "accepted"}``."""
    from .review_loop import item_key

    ts = time.time() if now is None else now
    with _tx() as con:
        row = _get(con, aid)
        action = (row["config"] or {}).get("action") or {}
        if (
            row["revision"] != expect_revision
            or action.get("kind") != "forge_review_loop"
            or str(action.get("repo", "")).lower() != repo.lower()
            or (source is not None and source_of(aid, row["config"]) != source)
        ):
            return {
                "run": None,
                "claimed": False,
                "stale_config": True,
                "reason": "the automation changed while the delivery was being checked",
            }
        deliver = bool(action.get("return_to_origin"))
        key = item_key(repo, item)
        if con.execute(
            "SELECT 1 FROM automation_claims WHERE automation_id=? AND slot=?", (aid, slot)
        ).fetchone():
            return {"run": None, "claimed": False, "reason": "this review already has a verdict"}
        verdict_ok, reason = _eligibility(con, row, manual=False, now=ts)
        if verdict_ok == "no":
            return {"run": None, "claimed": False, "reason": reason}
        if verdict_ok == "skip":
            # The consented limits (daily cap, concurrency) hold for returns too: recorded as
            # skipped, NOT claimed and NOT accepted, so a redelivery can return it once eligible.
            sid = uuid.uuid4().hex
            con.execute(
                "INSERT INTO automation_runs (id, automation_id, trigger, slot, fire_at, catch_up, "
                "covered, state, outcome, reason, scope, inputs, counts, owner, created_at, "
                "finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    aid,
                    "webhook",
                    slot,
                    None,
                    0,
                    1,
                    "done",
                    "skipped",
                    reason,
                    json.dumps({"consented_scope": row["consented_scope"]}),
                    json.dumps({**inputs, "leg": "return", "record": record}),
                    0,
                    "",
                    ts,
                    ts,
                ),
            )
            _step(con, sid, "skipped", reason)
            got = con.execute("SELECT * FROM automation_runs WHERE id=?", (sid,)).fetchone()
            return {"run": _run(got), "claimed": False, "reason": reason, "accepted": False}
        rec = con.execute(
            "SELECT * FROM automation_review_records WHERE id=? AND automation_id=?",
            (record, aid),
        ).fetchone()
        why = ""
        if rec is None or rec["item"] != key:
            why = f"ignored: {item} has no review {record} from this automation"
        elif rec["state"] == "verdict_accepted":
            why = f"ignored: review {record} already has a verdict ({rec['verdict']})"
        elif rec["state"] != "dispatched":
            why = f"ignored: stale verdict — review {record} was superseded by a newer revision"
        elif rec["revision"] != revision:
            why = (
                f"ignored: the verdict names {revision}, but review {record} was for "
                f"{rec['revision']}"
            )
        elif current != revision:
            why = (
                f"ignored: stale verdict — it is for {item} at {revision}, but the forge says the "
                f"current revision is {current}"
            )
        run_id = uuid.uuid4().hex
        scope = {
            "config": row["config"],
            "pins": row["pins"],
            "consented_scope": row["consented_scope"],
            "consented_at": row["consented_at"],
            "revision": row["revision"],
        }
        accepted = not why
        if accepted:
            con.execute(
                "UPDATE automation_review_records SET state='verdict_accepted', verdict=?, "
                "accepted_at=? WHERE id=?",
                (verdict, ts, record),
            )
            run_inputs = {
                **inputs,
                "leg": "return",
                "record": record,
                "round": int(rec["round"]),
                "reviewer_session": rec["session_key"],
            }
        else:
            run_inputs = {**inputs, "leg": "return", "record": record}
        dispatching = accepted and deliver
        outcome = "" if dispatching else ("ok" if accepted else "ignored")
        if accepted and not deliver:
            why = f"{verdict} on {item} at {revision}: recorded (this loop returns no verdicts)"
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
                "dispatching" if dispatching else "done",
                outcome,
                why,
                json.dumps(scope),
                json.dumps(run_inputs),
                1 if dispatching else 0,
                owner_token() if dispatching else "",
                ts,
                None if dispatching else ts,
            ),
        )
        _step(con, run_id, "claimed" if dispatching else outcome, why or f"{verdict} accepted")
        got = con.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        return {"run": _run(got), "claimed": True, "reason": why, "accepted": accepted}
