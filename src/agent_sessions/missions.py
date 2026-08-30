"""The mission record — MISSION CONTROL's durable store (#846, Phase 1 of #840).

A **mission** is an instruction, a lifecycle state, an ordered objective list, 1..N owned
sessions and an append-only event stream. Everything the console does in later phases is a
client of this module, so the invariants below are the deliverable — not the row count.

**Why SQLite rather than the ledger.** ``orchestrator_ledger`` is an append-only JSONL of
*actions*, reduced on read. That shape is right for actions and wrong for missions: a mission is
long-lived, is queried by state/project/recency, accumulates an unbounded event stream, and needs
a durable compare-and-set on one lifecycle column. Reducing a growing JSONL on every poll to
answer "list my missions" is the wrong trade. This is the app's first *write* use of ``sqlite3``;
``transcript.py`` / ``opencode.py`` / ``antigravity.py`` are the read-only precedents.

**Off the event loop, on a bounded pool — not** ``asyncio.to_thread``. With
``busy_timeout=5000`` a contended write parks a thread for five seconds, and ``to_thread``
dispatches to ``run_in_executor(None, …)``: the interpreter's *default* pool, unbounded queue,
shared with every other blocking call in the process. A burst of five-second waits there starves
the file panel, ``owner``'s flock waits and the orchestrator's own off-loop work. So this module
owns its pool, exactly as :mod:`agent_sessions.files` does, and — because a bounded pool bounds
*running threads, not the queue* — admission is counted **above** the executor and equals the pool
size. Over the bound is a deterministic :class:`MissionsBusy` (503), never a queue the caller
experiences as a hang.

**Writes serialize** through one module lock, so SQLite's ``busy_timeout`` is a backstop against a
*second process*, not against this app's own concurrency.

**Reads fail soft; writes fail loudly.** A locked or corrupt DB degrades the console (empty rail
plus a stated reason) exactly as opencode's reader degrades the sidebar. A *write* that fails is
reported and leaves the mission in its prior state — a silently dropped adopt or archive is
indistinguishable from one that worked.

**Sensitive operator data.** ``instruction``, ``brief`` and recap text are verbatim operator text
and bounded model text: an operator can type a token into an instruction. They are never written
to a log line, never put in an error message, deleted (not tombstoned) with the mission, and
bounded by the retention pass. The file is 0600 **from creation** — a widened-then-narrowed
window is still a window.

**Nothing here is a transcript.** Evidence is referenced by kind and re-fetched live, exactly as
the ledger already does, so the store never becomes a place transcript content can leak from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- identity + bounds

log = logging.getLogger(__name__)

MISSION_ID_RE = re.compile(r"^msn_[0-9a-f]{32}$")

#: Bumped whenever the schema changes; ``PRAGMA user_version`` carries it in the file.
SCHEMA_VERSION = 8

TITLE_MAX = 200
INSTRUCTION_MAX = 8000
BRIEF_MAX = 8000
EVENT_TEXT_MAX = 4000
EVENT_META_MAX = 4000
OBJECTIVE_TITLE_MAX = 200
OBSERVED_MAX = 500
OBJECTIVE_KEY_MAX = 64
PROBE_ARGS_MAX = 2000
#: Bound on one `GET /api/missions/{id}` timeline page.
EVENTS_PAGE_MAX = 200
EVENTS_PAGE_DEFAULT = 100
#: Bound on one `/api/missions` page.
LIST_LIMIT_MAX = 200
LIST_LIMIT_DEFAULT = 50
#: Objectives are an operator-sized list, not a data feed.
OBJECTIVES_MAX = 50

#: Events kept per mission. Trimming drops the oldest *droppable* kinds first (see
#: :data:`EVENT_KINDS_PRESERVED`) — never a decision, a state change or an objective edit, or
#: the growth would erase the recap stream the operator actually reads.
MISSION_EVENTS_MAX = 500
#: The ceiling that makes the cap a *cap*. Preserved kinds are protected from the soft cap above,
#: but "protected" cannot mean "unbounded": an operator retitling one objective in a loop grows
#: the timeline forever. Past this, preserved rows are dropped too.
#:
#: That is only safe because the settlement projection does **not** live on the event row (see
#: ``mission_settlements``): the timeline is a bounded *feed*, and a decision's content is a
#: separate durable fact that bounding the feed cannot reach.
MISSION_EVENTS_HARD_MAX = 2000
#: Closed missions older than this are pruned by :func:`retention_pass`.
MISSION_RETENTION_DAYS = 90
#: Rows deleted per retention pass — bounded so a long-idle install cannot stall one request.
RETENTION_BATCH = 200

#: Pool size. Small on purpose: writes serialize anyway, and this store is polled, not streamed.
MISSIONS_DB_WORKERS = 4
#: Admission equals the pool, so nothing ever queues behind a five-second wait.
MISSIONS_DB_MAX_INFLIGHT = MISSIONS_DB_WORKERS

_BUSY_TIMEOUT_MS = 5000
#: The ledger settlement hook runs inside somebody else's transition; it may never park a thread
#: for five seconds on our account, and it is best-effort, so it gets its own short wait.
_SETTLEMENT_BUSY_TIMEOUT_MS = 1000


# ---------------------------------------------------------------- the state machine

STATES: frozenset[str] = frozenset(
    {"draft", "planned", "dispatching", "running", "review", "done", "failed", "abandoned"}
)
#: States in which ``cwd`` may still be NULL. This MIRRORS the schema ``CHECK`` and must stay in
#: step with it — a draft exists before its project is resolved, which is exactly when the console
#: needs to ask which project was meant.
CWD_OPTIONAL_STATES: frozenset[str] = frozenset({"draft", "planned", "abandoned"})
#: A mission here is closed. ``abandoned`` alone is un-reopenable (see :data:`_ALLOWED`).
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "abandoned"})

#: The legal transition graph, as data rather than as scattered ``if``s. #840 names the happy
#: path and the two off-ramps but not the graph, and its §14 adds "reopen"; this is the whole
#: answer in one readable table.
_ALLOWED: dict[str, frozenset[str]] = {
    "draft": frozenset({"planned", "abandoned"}),
    "planned": frozenset({"dispatching", "draft", "abandoned"}),
    "dispatching": frozenset({"running", "failed", "abandoned"}),
    "running": frozenset({"review", "done", "failed", "abandoned"}),
    "review": frozenset({"running", "done", "failed", "abandoned"}),
    "done": frozenset({"running"}),
    "failed": frozenset({"running"}),
    # Terminal in the strong sense: abandoning is the operator saying "not this". Reopening it
    # would resurrect a mission whose sessions were already released and possibly archived.
    "abandoned": frozenset(),
}

OUTCOMES: frozenset[str] = frozenset({"done", "abandoned", "failed"})

# ---------------------------------------------------------------- events + objectives

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "operator_msg",
        "assistant_msg",
        "plan",
        "dispatched",
        "recap",
        "action",
        "approval",
        "subagent",
        "state",
        "error",
        "completion",
        "objective",
        "question",
        "answer",
        "probe",
        # Beyond #840's literal list: adopt/detach and archive are operator-visible changes that
        # are not actions, and the timeline has to be able to say them.
        "session",
        "archive",
    }
)

#: Kinds the SOFT cap may never drop — the mission's own history, as opposed to the recap stream
#: that actually grows. The hard ceiling above may still drop them; what it cannot reach is a
#: decision's content, which lives in ``mission_settlements`` rather than on the row.
EVENT_KINDS_PRESERVED: frozenset[str] = frozenset(
    {
        "action",
        "approval",
        "state",
        "objective",
        "completion",
        "plan",
        "dispatched",
        "question",
        "answer",
        "session",
        "archive",
    }
)

#: The closed set of ways an objective can be settled. Phase 1 has no probe *runner* — it owns
#: the vocabulary so Phase 3 inherits a validated gate instead of inventing one.
PROBE_KINDS: frozenset[str] = frozenset(
    {
        "none",
        "git_local",
        "forge_pr",
        "forge_checks",
        "forge_review",
        "forge_merged",
        "forge_run",
        "http_status",
        "http_revision",
        "agent_judged",
    }
)
#: "the agent believes it wrote tests" is not evidence that it did, so it may never gate alone.
NON_GATING_PROBES: frozenset[str] = frozenset({"agent_judged"})

OBJECTIVE_STATES: frozenset[str] = frozenset({"pending", "active", "met", "failed", "waived"})
OBJECTIVE_SOURCES: frozenset[str] = frozenset({"playbook", "model", "operator"})
#: Objective states that do NOT count as satisfied for the "adding a gate reopens `review`" rule.
_UNMET_OBJECTIVE_STATES: frozenset[str] = frozenset({"pending", "active", "failed"})

SESSION_ROLES: frozenset[str] = frozenset({"primary", "sub"})
#: ``skipped`` = the session was released and is now held by another OPEN mission, so tearing
#: it down would kill that mission's agent. Named rather than silently omitted.
ARCHIVE_STATES: frozenset[str] = frozenset(
    {
        "pending",
        "in_progress",
        "done",
        "already_archived",
        "failed",
        "skipped",
        "restoring",
        "restore_failed",
    }
)
#: `in_progress` is a **lease**: exactly one worker may run a session's external teardown.
#: `begin_archive` hands the same pending rows to a retry, and `prov.archive` is not idempotent
#: in general, so without a lease a concurrent (or retried) request performs the destructive
#: effect twice. A lease is re-opened only once it has gone :data:`LEASE_MAX_AGE_S` without a
#: heartbeat — never because it belongs to another process. "Another process" is not "a dead
#: process": this app supports more than one instance over the same store (its single-writer
#: session lock is explicitly cross-instance), so an owner-epoch test cannot tell a crashed worker
#: from a live sibling, and reclaiming on one put two workers inside the same irreversible effect.


# ---------------------------------------------------------------- errors


class MissionError(RuntimeError):
    """A refusal with the HTTP status the route layer passes through.

    The message is operator-facing and **never** carries mission content — it names the mission
    id and the failure kind, because ``instruction`` / ``brief`` are sensitive.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class MissionsBusy(MissionError):
    """Admission bound saturated. 503 — an honest "try again", never a silent queue."""

    def __init__(self, message: str = "the missions store is busy; try again") -> None:
        super().__init__(message, status=503)


class MissionNotFound(MissionError):
    def __init__(self, mission_id: str) -> None:
        super().__init__(f"unknown mission {mission_id}", status=404)


class SessionHeld(MissionError):
    """A session is already owned by another OPEN mission. 409, naming the holder."""

    def __init__(self, session_key: str, holder: str) -> None:
        super().__init__(f"session {session_key} is already held by mission {holder}", status=409)
        self.holder = holder


# ---------------------------------------------------------------- path + connection


def _db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_MISSIONS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "missions.db"),
        )
    )


def _retention_days() -> int:
    raw = os.environ.get("AGENT_SESSIONS_MISSIONS_RETENTION_DAYS")
    if raw is None:
        return MISSION_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MISSION_RETENTION_DAYS


def _touch_0600(p: Path) -> None:
    """Create the file 0600 **before** sqlite3 can create it under the process umask.

    ``sqlite3.connect`` creates a fresh DB with 0644 on a default umask, and a chmod afterwards
    leaves a window in which the operator's instructions were world-readable. Opening with
    ``O_CREAT|O_EXCL`` at 0600 first closes it; an existing file is left alone.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _connect(path: Path | None = None, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS):
    p = path or _db_path()
    _touch_0600(p)
    con = sqlite3.connect(str(p), timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    con.execute("PRAGMA foreign_keys=ON")
    # Deleting a mission has to actually remove the bytes, not just unlink the row: `instruction`
    # and `brief` are verbatim operator text and may hold a token. Without this, a freed page
    # keeps its old content until something reuses it.
    #
    # **Set explicitly because the default is a build option, not a promise.** Debian/Ubuntu
    # compile libsqlite3 with `SQLITE_SECURE_DELETE=1`, so on this host — and therefore in CI,
    # which runs on it — the property held by accident and no test could see it missing. On a
    # build without it the plaintext survives every delete. A security property that depends on
    # which libsqlite3 happens to be linked is not a property.
    con.execute("PRAGMA secure_delete=ON")
    return con


_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  instruction   TEXT NOT NULL,
  brief         TEXT,
  project_id    TEXT,
  cwd           TEXT,
  engine        TEXT,
  engine_source TEXT,
  state         TEXT NOT NULL,
  playbook_id   TEXT,
  created_at    REAL,
  updated_at    REAL,
  closed_at     REAL,
  archived_at   REAL,
  archiving_at  REAL,
  unarchiving_at REAL,
  -- What the operator asked for, stored WITH the claim. Recovery that has to guess whether the
  -- sessions were meant to be restored is not recovering the operation, it is performing a
  -- different one — `sessions=False` crashed and came back with the sessions unarchived anyway.
  unarchive_sessions INTEGER,
  -- The OWNER of the in-flight archive/unarchive. Per-session leases stop two workers tearing
  -- down the same session; they do not stop a second caller walking in, seeing an empty pending
  -- list (because the first worker already leased every row) and finalising the operation while
  -- that worker is still inside `cleanup_runtime`. The operation needs an owner too.
  op_token      TEXT,
  outcome       TEXT,
  -- A draft may exist before its project is resolved (that is the whole point of asking), but
  -- nothing may LAUNCH without a server-resolved cwd. Enforced here, not in a comment.
  CHECK (state IN ('draft','planned','abandoned') OR cwd IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS mission_sessions (
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  session_key   TEXT NOT NULL,
  role          TEXT NOT NULL,
  spawned_by    TEXT,
  added_at      REAL,
  removed_at    REAL,
  -- WHY the session left the roster, which archive has to be able to tell apart. Reaching a
  -- terminal state releases ownership ('closed') and those sessions are still the mission's to
  -- reap; an operator DETACHING one ('detached') is the operator saying "this is not part of
  -- this mission any more", and archiving must not cross that line. Both set `removed_at`, so
  -- the timestamp alone cannot distinguish them — and archiving the roster by timestamp
  -- terminated work the operator had deliberately removed.
  release_reason TEXT,
  archive_state TEXT,
  archive_error TEXT,
  -- WHICH PROCESS holds the lease. Recovery reopens a lease on the premise that it can only
  -- belong to a worker that died — true at boot, and false the moment the app is also serving
  -- requests, which it is: recovery runs as a background task while routes are live. Stamping
  -- the process identity makes the premise checkable instead of assumed.
  lease_owner   TEXT,
  -- WHEN the lease was taken. Ownership alone is not enough: the write that releases a lease can
  -- itself fail, and a lease stamped with the CURRENT process is one recovery refuses to touch —
  -- so a transient failure at exactly the wrong moment stranded the row until a restart.
  lease_at      REAL,
  -- The fencing token of the reservation this lease holds. Settlement must present it, so a
  -- worker whose lease was reclaimed cannot settle the operation that replaced it.
  lease_token   TEXT,
  PRIMARY KEY (mission_id, session_key)
);

CREATE TABLE IF NOT EXISTS mission_objectives (
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  title      TEXT NOT NULL,
  probe      TEXT NOT NULL,
  probe_args TEXT,
  gate       INTEGER NOT NULL,
  state      TEXT NOT NULL,
  met_at     REAL,
  observed   TEXT,
  source     TEXT NOT NULL,
  PRIMARY KEY (mission_id, key)
);

CREATE TABLE IF NOT EXISTS mission_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  at          REAL NOT NULL,
  kind        TEXT NOT NULL,
  session_key TEXT,
  action_id   TEXT,
  text        TEXT,
  meta        TEXT
);

-- The immutable settlement projection (#840 §2), in its OWN table rather than a column on the
-- event.
--
-- It lived on the event first, and that made two contracts fight: the timeline is a *bounded
-- feed* (something has to cap it, or one operator retitling an objective in a loop grows it
-- forever), while the projection is a *durability promise* (an old mission must still render its
-- decisions after the ledger has compacted the row away). With the projection stored on the row,
-- bounding the feed necessarily deleted the promise — ordering projection-bearing rows last only
-- delayed it.
--
-- Keyed on `action_id` and owned by no mission, so trimming the feed cannot touch it. Written
-- ONCE, at settlement (`INSERT OR IGNORE`), and never updated. Orphans are collected when the
-- last event referencing them goes, because `rationale` is bounded model text about the
-- operator's work and is deleted with the mission like everything else here.
-- WHO currently has the right to mutate a session's PROVIDER state — move its transcript, or
-- terminate the process group behind it. One row per session key, so the mission teardown path and
-- the sibling session routes exclude each other instead of merely checking each other.
--
-- A read-only guard cannot close this: it answers about the past, and the caller then acts. The
-- reservation IS the act. `token` is a fencing token — a reclaimed reservation mints a new one, so
-- a worker whose lease expired can no longer settle the operation that replaced it.
CREATE TABLE IF NOT EXISTS session_reservations (
  session_key TEXT PRIMARY KEY,
  token       TEXT NOT NULL,
  holder      TEXT NOT NULL,
  at          REAL NOT NULL
);

-- Durable obligations that outlive one call. Today: `scrub_pending`, set when a WAL truncate
-- came back busy so the plaintext of a deleted mission is still on disk. Without it the advertised
-- "retry" was a no-op — the rows are already gone, so the next delete returns early and never
-- reaches the scrub.
CREATE TABLE IF NOT EXISTS store_flags (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS mission_settlements (
  action_id TEXT PRIMARY KEY,
  verb      TEXT,
  state     TEXT,
  rationale TEXT,
  outcome   TEXT,
  at        REAL
);

-- One session may be held by at most one OPEN mission. "Check then insert" cannot be made
-- race-safe in application code, so the database is the arbiter and the loser gets a 409.
CREATE UNIQUE INDEX IF NOT EXISTS mission_sessions_active
  ON mission_sessions(session_key) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS mission_objectives_by_mission
  ON mission_objectives(mission_id, ord);
CREATE INDEX IF NOT EXISTS mission_events_by_mission
  ON mission_events(mission_id, seq);
CREATE INDEX IF NOT EXISTS missions_by_state
  ON missions(state, archived_at, updated_at DESC);
-- The rail's own query is scope + recency with NO state predicate, so `missions_by_state` (which
-- leads with `state`) cannot serve it. This one can.
CREATE INDEX IF NOT EXISTS missions_by_archived
  ON missions(archived_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS mission_events_by_action
  ON mission_events(action_id) WHERE action_id IS NOT NULL;
"""


def _migrate(con) -> int:
    """Bring the file to :data:`SCHEMA_VERSION`. Explicit and tested, never implicit.

    Version 0 is "no file / empty file"; the create is the migration. Every later version adds an
    ``if version < N`` step below, applied **in ascending order**, so an install upgraded in place
    walks the same path a fresh one would arrive at — and each step is a code path with a test
    rather than a hope.
    """
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        # A newer app wrote this file. Refuse rather than corrupt it — reads fail soft above.
        raise MissionError(
            f"missions store is at schema {version}, newer than this build ({SCHEMA_VERSION})",
            status=500,
        )
    if version == SCHEMA_VERSION:
        return version
    if version < 1:
        con.executescript(_SCHEMA)
    else:
        # Ascending, one step per version, each guarded so a partially-upgraded file converges.
        if version < 2:
            _migrate_1_to_2(con)
        if version < 3:
            _migrate_2_to_3(con)
        if version < 4:
            _migrate_3_to_4(con)
        if version < 5:
            _migrate_4_to_5(con)
        if version < 6:
            _migrate_5_to_6(con)
        if version < 7:
            _migrate_6_to_7(con)
        if version < 8:
            _migrate_7_to_8(con)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return SCHEMA_VERSION


def _migrate_1_to_2(con) -> None:
    """v2 makes UNARCHIVE durable the way archive already was.

    A crash between "the provider sessions were restored" and "the record says un-archived" used
    to leave live sessions under a mission still recorded archived, and boot recovery only looked
    at ``archiving_at`` — so the torn state was invisible. The claim stamp is what makes it
    findable.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "unarchiving_at" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN unarchiving_at REAL")


def _migrate_2_to_3(con) -> None:  # noqa: D401 — see the body; two changes ship as one step
    """v3 moves the settlement projection off the event row into its own table.

    Storing it on the event made two contracts fight: the timeline is a **bounded feed** and the
    projection is a **durability promise**, so bounding the feed necessarily deleted the promise.
    Normalising it settles that — see the ``mission_settlements`` comment in the schema.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_settlements ("
        "  action_id TEXT PRIMARY KEY, verb TEXT, state TEXT,"
        "  rationale TEXT, outcome TEXT, at REAL)"
    )
    mcols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "unarchive_sessions" not in mcols:
        # The unarchive claim now remembers the mode it was made for.
        con.execute("ALTER TABLE missions ADD COLUMN unarchive_sessions INTEGER")
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_events)")}
    if "settlement" not in cols:
        return
    for row in con.execute(
        "SELECT action_id, settlement FROM mission_events "
        "WHERE action_id IS NOT NULL AND settlement IS NOT NULL"
    ).fetchall():
        proj = _loads(row["settlement"]) or {}
        con.execute(
            "INSERT OR IGNORE INTO mission_settlements "
            "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
            (
                row["action_id"],
                proj.get("verb"),
                proj.get("state"),
                proj.get("rationale"),
                proj.get("outcome"),
                proj.get("at"),
            ),
        )
    con.execute("ALTER TABLE mission_events DROP COLUMN settlement")


def _migrate_3_to_4(con) -> None:
    """v4 gives the archive/unarchive OPERATION an owner, and the store a durable obligation slot.

    Per-session leases were not enough: a second caller saw an empty pending list — because the
    first worker had already leased every row — and finalised the operation while that worker was
    still inside ``cleanup_runtime``. And a failed scrub had no way to be retried, because the
    rows it should have scrubbed were already deleted.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "op_token" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN op_token TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS store_flags (key TEXT PRIMARY KEY, value TEXT)")


def _migrate_5_to_6(con) -> None:
    """v6 records which process holds a session lease.

    Boot recovery reopens leases on the premise that any it finds must belong to a crashed
    worker. That is true at boot and false while the app is serving — and it serves immediately,
    with recovery running behind it. Without an owner, recovery could reset a lease a live request
    was holding and then claim the same session, so two workers ran the same destructive effect.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_owner" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_owner TEXT")


def _migrate_6_to_7(con) -> None:
    """v7 stamps WHEN a lease was taken, so one can expire.

    v6 made recovery refuse a lease owned by the current process, which is right — a live worker's
    lease must not be reset. But the write that RELEASES a lease can fail too, and then nothing in
    that process's lifetime could reclaim it. Age is the property that does not depend on another
    write succeeding.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_at" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_at REAL")


def _migrate_7_to_8(con) -> None:
    """v8 turns the read-only archive guard into a real reservation with a fencing token.

    A guard answers about the past and the caller then acts, so a mission could adopt a session
    between the check and the provider call. And an expired lease reclaimed by a new worker left
    the old worker still able to settle it, so the stale result could win and the external effect
    could run twice.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS session_reservations ("
        "  session_key TEXT PRIMARY KEY, token TEXT NOT NULL,"
        "  holder TEXT NOT NULL, at REAL NOT NULL)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_token" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_token TEXT")


def _flag_get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM store_flags WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _flag_set(con, key: str, value: str | None) -> None:
    if value is None:
        con.execute("DELETE FROM store_flags WHERE key=?", (key,))
    else:
        con.execute(
            "INSERT INTO store_flags (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _migrate_4_to_5(con) -> None:
    """v5 records WHY a session left the roster, so archive can respect an explicit detach.

    Terminal-state release and operator detach both set ``removed_at``, so archiving "the whole
    roster" reaped sessions the operator had deliberately removed from the mission. Existing rows
    default to ``closed``: before this column there was no detach distinction to preserve, and
    ``closed`` is the behaviour they already had.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "release_reason" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN release_reason TEXT")
        con.execute(
            "UPDATE mission_sessions SET release_reason='closed' WHERE removed_at IS NOT NULL"
        )


_schema_lock = threading.Lock()
_schema_done: set[str] = set()


def _ready(path: Path | None = None, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS):
    """A connection with the schema present. Cheap after the first call per path."""
    p = path or _db_path()
    con = _connect(p, busy_timeout_ms=busy_timeout_ms)
    key = str(p)
    if key in _schema_done:
        return con
    with _schema_lock:
        _migrate(con)
        _schema_done.add(key)
    return con


def reset_schema_cache_for_test() -> None:
    """Forget which paths have been migrated (tests point the env at a fresh tmp file)."""
    with _schema_lock:
        _schema_done.clear()


# ---------------------------------------------------------------- pool + admission

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
#: Every write core runs under this, so the app never contends with itself on the DB lock.
_write_lock = threading.RLock()

_budget_lock = threading.Lock()
_inflight = 0


def executor() -> ThreadPoolExecutor:
    """The module's own pool, sized to exactly :data:`MISSIONS_DB_WORKERS`.

    Deliberately not ``asyncio.to_thread``'s default pool — see the module docstring. Sized to
    equal the admission bound so admission and execution coincide and nothing ever queues.
    """
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=MISSIONS_DB_WORKERS, thread_name_prefix="missions-db"
            )
        return _EXECUTOR


def shutdown_executor_for_test() -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = None


def inflight_for_test() -> int:
    with _budget_lock:
        return _inflight


def acquire() -> None:
    """Take a slot or raise :class:`MissionsBusy`. Called by the ROUTE, **before** submission.

    Admission has to happen above the executor: submitting first bounds what *runs* while a flood
    piles up in the queue behind it, and with a five-second ``busy_timeout`` per contended write
    that queue is experienced as a hang rather than as an answer.
    """
    global _inflight
    with _budget_lock:
        if _inflight >= MISSIONS_DB_MAX_INFLIGHT:
            raise MissionsBusy()
        _inflight += 1


def _release() -> None:
    global _inflight
    with _budget_lock:
        if _inflight > 0:
            _inflight -= 1


class Slot:
    """One admitted request's slot, releasable **exactly once** by whoever ends up owning it.

    Ownership is genuinely ambiguous, and both halves have been measured wrong before (see
    :class:`agent_sessions.files.Slot`, where this shape comes from):

    * If the worker starts, the worker owns it — releasing from the request task hands the slot
      back while the thread is still running.
    * If the request is cancelled *before* the worker starts, the queued callable is dropped and
      the worker never runs, so nothing in the worker can release and the slot leaks for the life
      of the process.

    Neither owner can be chosen up front, so both try and the lock makes the second a no-op.
    """

    __slots__ = ("_lock", "_done")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False

    def release(self) -> bool:
        """Release if nobody has yet. Returns True iff THIS call did it."""
        with self._lock:
            if self._done:
                return False
            self._done = True
        _release()
        return True


def _run_slot(slot: Slot, fn: Callable[[], Any]) -> Any:
    """Worker entry point: run ``fn`` and release the slot when the callable actually exits."""
    try:
        return fn()
    finally:
        slot.release()


async def run_admitted(fn: Callable[[], Any]) -> Any:
    """Admit, run ``fn`` off the loop on the module pool, release **exactly once**.

    The single entry point every async caller uses, and the release logic is
    :func:`agent_sessions.routes.files._run`'s, verbatim in shape, because the obvious version is
    wrong in both directions:

    * a plain ``finally: slot.release()`` around the await hands the slot back **while the worker
      is still running** — occupancy drops to zero with the thread blocked, and the bound then
      admits more work than it names;
    * releasing only in the worker leaks the slot forever when the request is cancelled while the
      callable is still queued, because that callable is dropped and no worker ever runs it.

    ``concurrent.futures.Future.cancelled()`` is the discriminator: it is True **only** when the
    callable never began. So the worker owns the slot whenever it started, and this frame owns it
    only when it did not — and ``Slot.release`` is exactly-once, so the two can never both fire.

    ``acquire()`` raises :class:`MissionsBusy` *before* anything is submitted, which is what makes
    the bound a refusal rather than a queue.
    """
    acquire()
    slot = Slot()
    try:
        cf = executor().submit(_run_slot, slot, fn)
    except BaseException:
        slot.release()  # never submitted: nobody in a worker can ever release it
        raise
    try:
        return await asyncio.wrap_future(cf)
    except BaseException:
        if cf.cancelled():
            slot.release()
        raise


# ---------------------------------------------------------------- helpers


#: Archive states in which a session key is RESERVED against adoption.
#:
#: `in_progress` alone was not enough. `settle_session_archive` moves the row to `done` and
#: releases it *before* the mission's archive finishes — and at that point the provider session
#: really is archived, so adopting it hands the next mission a session whose transcript has been
#: moved out from under it. The reservation therefore lasts until an explicit restore puts the
#: session back, not until the teardown step happens to settle.
RESERVED_ARCHIVE_STATES: frozenset[str] = frozenset(
    {"pending", "in_progress", "done", "already_archived", "restoring", "restore_failed"}
)
#: Built once at import — the only interpolation is a `?` run sized by a module constant.
_RESERVED_ORDER: tuple[str, ...] = tuple(sorted(RESERVED_ARCHIVE_STATES))
_RESERVED_SQL = (  # noqa: S608
    "SELECT mission_id, archive_state FROM mission_sessions "
    "WHERE session_key=? AND archive_state IN ({m}) LIMIT 1"
).format(m=",".join("?" * len(_RESERVED_ORDER)))
_RESERVED_WHY = {
    "pending": "queued for teardown",
    "in_progress": "being torn down",
    "done": "archived",
    "already_archived": "archived",
    "restoring": "being restored",
    # Still archived — the restore did not land. Reserved for the same reason `done` is: adopting
    # it would hand the next mission a session whose transcript is not there.
    "restore_failed": "archived (its restore failed)",
}


#: Identity of THIS process, minted once at import. A lease stamped with it belongs to a worker
#: that is, by definition, still running — so recovery must not reopen it. A lease stamped with
#: anything else (or nothing) belonged to a process that is gone.
PROCESS_EPOCH: str = uuid.uuid4().hex

#: How long a session lease may be held before it is treated as abandoned, whoever owns it.
#: A teardown is `cleanup_runtime` plus a file move — seconds. Five minutes is unambiguous, and
#: it is the only reclamation path that does not depend on some *other* write succeeding at the
#: moment things are already going wrong.
LEASE_MAX_AGE_S = 300


def _new_op_token() -> str:
    """Identity of one archive/unarchive attempt, so `finish` can prove it owns what it closes."""
    return uuid.uuid4().hex


def new_id() -> str:
    return f"msn_{uuid.uuid4().hex}"


def validate_id(mission_id: object) -> str:
    if not isinstance(mission_id, str) or not MISSION_ID_RE.match(mission_id):
        raise MissionError("bad mission id", status=404)
    return mission_id


def _require_str(value: object, field: str) -> str:
    """A string, or a 422 — checked BEFORE any set-membership test.

    ``value in frozenset`` raises ``TypeError: unhashable type`` for a dict or a list, which
    escapes the route as a 500. A malformed request body is a client error, and it says so.
    """
    if not isinstance(value, str):
        raise MissionError(f"{field} must be a string", status=422)
    return value


def strict_bool(value: object, field: str, *, default: bool | None = None) -> bool:
    """A real JSON boolean, or a 422 — never Python truthiness.

    ``bool(value)`` on request JSON makes the string ``"false"`` mean **true**, which on a
    destructive flag is not a type nit: it is an authorisation bug. Every boolean that crosses
    the route boundary comes through here.
    """
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise MissionError(f"{field} must be true or false", status=422)
    return value


def _cap(value: object, limit: int) -> str:
    return (value if isinstance(value, str) else "").strip()[:limit]


def _cap_or_none(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = _cap(value, limit)
    return text or None


def _json_or_none(value: object, limit: int, *, field: str = "value") -> str | None:
    """Serialize to bounded JSON, or **refuse**.

    Truncating at an arbitrary character is the wrong failure: an oversized but otherwise valid
    object becomes invalid JSON on disk and reads back as ``None``, so the caller is told the
    write succeeded and the data is silently gone. Over the bound is an error the caller sees.
    """
    if value is None:
        return None
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        raise MissionError(f"{field} is not serialisable", status=422) from e
    if len(blob) > limit:
        raise MissionError(f"{field} is too large (max {limit} serialised chars)", status=422)
    return blob


def _loads(blob: object) -> Any:
    if not isinstance(blob, str) or not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _row_to_mission(row) -> dict:
    return dict(row)


def _event_row(row) -> dict:
    d = dict(row)
    d["meta"] = _loads(d.get("meta"))
    d["settlement"] = None  # filled by the join in `get_mission`
    return d


def _objective_row(row) -> dict:
    d = dict(row)
    d["gate"] = bool(d.get("gate"))
    d["probe_args"] = _loads(d.get("probe_args"))
    return d


# ---------------------------------------------------------------- event append (in-tx)


#: How many ids a server-built event meta may name before it summarises instead. A mission's
#: roster is operator-sized, but "operator-sized" is not a bound, and an event meta that grows
#: with it would make an ordinary archive fail on a size check.
META_LIST_MAX = 20


def _bounded_meta(meta: object) -> object:
    """Bound a server-built event meta so it cannot outgrow :data:`EVENT_META_MAX`.

    Only the list-valued fields grow (session rosters, applied ops); each is truncated to
    :data:`META_LIST_MAX` with an explicit ``…_total`` count beside it, so a summarised meta reads
    as a summary rather than as the whole story.
    """
    if not isinstance(meta, dict):
        return meta
    out: dict = {}
    for k, v in meta.items():
        if isinstance(v, list) and len(v) > META_LIST_MAX:
            out[k] = v[:META_LIST_MAX]
            out[f"{k}_total"] = len(v)
        else:
            out[k] = v
    return out


def _append_event(
    con,
    mission_id: str,
    kind: str,
    *,
    at: float | None = None,
    session_key: str | None = None,
    action_id: str | None = None,
    text: str | None = None,
    meta: object = None,
) -> int:
    if kind not in EVENT_KINDS:
        raise MissionError(f"unknown event kind {kind!r}")
    cur = con.execute(
        "INSERT INTO mission_events (mission_id, at, kind, session_key, action_id, text, meta) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            mission_id,
            time.time() if at is None else at,
            kind,
            session_key,
            action_id,
            _cap_or_none(text, EVENT_TEXT_MAX),
            _json_or_none(_bounded_meta(meta), EVENT_META_MAX, field="event meta"),
        ),
    )
    _trim_events(con, mission_id)
    return int(cur.lastrowid)


#: Built once at import from :data:`EVENT_KINDS_PRESERVED` — no per-call SQL construction, and
#: the kind list is a module constant rather than anything a request can reach.
_PRESERVED_ORDER: tuple[str, ...] = tuple(sorted(EVENT_KINDS_PRESERVED))
# The only interpolation is a `?` run sized by a module constant; every value is bound.
_TRIM_SQL = (  # noqa: S608
    "DELETE FROM mission_events WHERE seq IN ("
    "  SELECT seq FROM mission_events"
    "  WHERE mission_id=? AND kind NOT IN ({placeholders})"
    "  ORDER BY seq ASC LIMIT ?)"
).format(placeholders=",".join("?" * len(_PRESERVED_ORDER)))


def append_event(
    mission_id: str,
    kind: str,
    *,
    session_key: str | None = None,
    action_id: str | None = None,
    text: str | None = None,
    meta: object = None,
    now: float | None = None,
    path: Path | None = None,
) -> int:
    """Append one timeline event. Returns its ``seq``.

    The public door for events that are not part of another operator-visible change — recaps,
    questions, probes. Anything that IS part of one (a transition, an adopt, an objective edit)
    appends inside that change's own transaction instead, so the timeline can never disagree with
    the state it describes.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # The same lifecycle fence every other mutation gets. This is the public producer
            # later phases write recaps and probe results through, and "every other mutation is
            # fenced" has to include it — otherwise the supervisor keeps appending to a mission
            # whose agents are being torn down, or to one that is already archived.
            _fence_busy(con, mission_id)
            seq = _append_event(
                con,
                mission_id,
                kind,
                at=now,
                session_key=session_key,
                action_id=action_id,
                text=text,
                meta=meta,
            )
            if action_id:
                # ATOMIC with the reference it protects — see the docstring. A reference to a
                # settled action can never exist without the projection that outlives it.
                _project_reference_in_tx(con, action_id)
            con.execute(
                "UPDATE missions SET updated_at=? WHERE id=?",
                (time.time() if now is None else now, mission_id),
            )
            con.execute("COMMIT")
            return seq
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def _project_reference_in_tx(con, action_id: str) -> None:
    """Freeze a settled action's projection **in the same transaction as the reference to it**.

    Compaction asks which doomed actions a mission references and then rewrites the ledger — a
    check-then-act across two stores. An event committed in between creates a reference to a row
    compaction has already decided nobody points at. Fencing the two stores against each other
    would order their locks in both directions, which is the deadlock the settlement hook
    documents.

    Making the two writes **atomic** fixes it without any fence: the reference and its projection
    commit together, so "a reference exists" implies "a projection exists" no matter what
    compaction does next, and a rolled-back event leaves no orphaned projection either. Called
    *after* the event insert so the row is visible to the reference check.

    An action that is still LIVE needs nothing — compaction never removes a live row, and by the
    time it settles the reference is already there for the reference-scoped pass to find.

    **And when the source is already gone**, this records that fact rather than committing a
    reference with nothing behind it. Compaction can legitimately have dropped a terminal row
    before anything referenced it, and there are only three things to do about it: reject the
    operator's write because of ledger housekeeping, commit a decision that renders blank forever,
    or say plainly that the record was destroyed before this reference existed. The third is the
    only one that is both honest and non-destructive, and §16 already has a projection for it —
    *absent from the ledger ⇒ historical, no controls, and no outcome asserted*.
    """
    try:
        from . import orchestrator_ledger

        status, rec = orchestrator_ledger.lookup(action_id)
        terminal = orchestrator_ledger.TERMINAL_STATES
    except Exception:  # noqa: BLE001 — an unreadable ledger is not this write's problem
        return
    if status == "unreadable":
        # **Absent and unreadable are different answers**, and only one of them is permanent.
        # Freezing `source_compacted` on a transient EIO would record "we can never know what this
        # decision said" about a row sitting intact on disk. Leave it unprojected instead: the
        # reference stays retryable by the read-time backfill, and compaction cannot destroy the
        # row in the meantime because it declines when it cannot read the ledger either.
        return
    if rec is None:
        con.execute(
            "INSERT OR IGNORE INTO mission_settlements "
            "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
            (
                action_id,
                None,
                SETTLEMENT_LOST,
                "the ledger record was compacted before anything referenced it",
                None,
                time.time(),
            ),
        )
        return
    if rec.get("state") not in terminal:
        return
    con.execute(
        "INSERT OR IGNORE INTO mission_settlements "
        "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
        (
            action_id,
            _cap(rec.get("verb"), 40) or None,
            _cap(rec.get("state"), 40) or None,
            _cap(rec.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
            _cap(rec.get("outcome") or rec.get("detail"), SETTLEMENT_OUTCOME_MAX) or None,
            float(rec.get("ts") or time.time()),
        ),
    )


def event_count(mission_id: str, *, path: Path | None = None) -> int:
    con = _ready(path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
            ).fetchone()[0]
        )
    finally:
        con.close()


def _trim_events(con, mission_id: str) -> int:
    """Hold the per-mission event cap. Two bounds, in order.

    The **soft cap** drops only droppable kinds — recaps and probe noise — so the ordinary case
    never loses a decision, a state change or an objective edit.

    The **hard ceiling** (:func:`_trim_hard`) is what makes "capped" true rather than aspirational,
    because "preserved" would otherwise mean unbounded.
    """
    total = con.execute(
        "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
    ).fetchone()[0]
    over = int(total) - MISSION_EVENTS_MAX
    if over <= 0:
        return 0
    dropped = con.execute(_TRIM_SQL, (mission_id, *_PRESERVED_ORDER, over)).rowcount or 0
    dropped += _trim_hard(con, mission_id)
    return dropped


def _trim_hard(con, mission_id: str) -> int:
    """The hard ceiling that makes the cap a *cap*.

    The soft cap protects decision / state / objective rows, but "protected" cannot mean
    "unbounded": an operator retitling one objective in a loop grew the timeline past the stated
    cap (measured at 521 against 500). Past this ceiling the protected kinds go too.

    That is safe **only because the settlement projection no longer lives on the row.** When it
    did, bounding the feed deleted the durability promise, and ordering projection-bearing rows
    last merely delayed it. With projections normalized into ``mission_settlements``, the timeline
    is a bounded *feed* and the decision's content is a separate, durable fact.

    Drop order is stated rather than incidental: the high-volume operator-generated kinds
    (``objective``, ``session``) go before the lifecycle ones, so what a reader loses first is the
    edit noise rather than the shape of what happened.
    """
    total = int(
        con.execute(
            "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
        ).fetchone()[0]
    )
    over = total - MISSION_EVENTS_HARD_MAX
    if over <= 0:
        return 0
    doomed = con.execute(
        "SELECT seq, action_id FROM mission_events WHERE mission_id=? "
        "ORDER BY CASE kind WHEN 'objective' THEN 0 WHEN 'session' THEN 0 ELSE 1 END ASC, "
        "seq ASC LIMIT ?",
        (mission_id, over),
    ).fetchall()
    if not doomed:
        return 0
    marks = ",".join("?" * len(doomed))
    con.execute(
        f"DELETE FROM mission_events WHERE seq IN ({marks})",  # noqa: S608
        tuple(r["seq"] for r in doomed),
    )
    _gc_settlements(con, [r["action_id"] for r in doomed if r["action_id"]])
    return len(doomed)


# ---------------------------------------------------------------- create / read


def create_mission(
    instruction: str,
    *,
    title: str = "",
    project_id: str | None = None,
    cwd: str | None = None,
    engine: str | None = None,
    engine_source: str | None = None,
    playbook_id: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Create a ``draft`` from an instruction. One transaction: the row and its first event.

    ``cwd`` stays NULL when the project is not resolved yet — that is the state in which the
    console asks which project was meant, and the schema ``CHECK`` is what stops it launching.
    """
    text = _cap(instruction, INSTRUCTION_MAX)
    if not text:
        raise MissionError("instruction is required", status=422)
    ts = time.time() if now is None else now
    mission_id = new_id()
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO missions (id, title, instruction, brief, project_id, cwd, engine,"
                " engine_source, state, playbook_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'draft',?,?,?)",
                (
                    mission_id,
                    _cap(title, TITLE_MAX) or text[:TITLE_MAX],
                    text,
                    None,
                    _cap_or_none(project_id, 200),
                    _cap_or_none(cwd, 4096),
                    _cap_or_none(engine, 40),
                    _cap_or_none(engine_source, 20),
                    _cap_or_none(playbook_id, 200),
                    ts,
                    ts,
                ),
            )
            _append_event(con, mission_id, "operator_msg", at=ts, text=text)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


def get_mission(
    mission_id: str,
    *,
    events_limit: int = EVENTS_PAGE_DEFAULT,
    events_before_seq: int | None = None,
    path: Path | None = None,
) -> dict | None:
    """One mission with its roster, objectives and a bounded timeline page.

    The timeline pages **newest-first on ``seq``**, not on an offset: ``seq`` is monotonic and
    a concurrent append therefore cannot shift a page under the reader.
    """
    validate_id(mission_id)
    limit = max(1, min(EVENTS_PAGE_MAX, int(events_limit or EVENTS_PAGE_DEFAULT)))
    con = _ready(path)
    try:
        # ONE snapshot for the whole response. In autocommit each SELECT sees its own snapshot,
        # so a legal transition committing between them returned `state="running"` beside a
        # `done` event and a released session — a mission whose parts disagree, which is exactly
        # what the timeline exists to prevent. `BEGIN DEFERRED` starts the read transaction on
        # the first SELECT and holds it across all four; WAL means it blocks no writer.
        con.execute("BEGIN DEFERRED")
        row = con.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            con.execute("COMMIT")
            return None
        if events_before_seq is None:
            events = con.execute(
                "SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT ?",
                (mission_id, limit),
            ).fetchall()
        else:
            events = con.execute(
                "SELECT * FROM mission_events WHERE mission_id=? AND seq<? "
                "ORDER BY seq DESC LIMIT ?",
                (mission_id, int(events_before_seq), limit),
            ).fetchall()
        sessions = con.execute(
            "SELECT * FROM mission_sessions WHERE mission_id=? ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        objectives = con.execute(
            "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
            (mission_id,),
        ).fetchall()
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    mission = _row_to_mission(row)
    mission["sessions"] = [dict(s) for s in sessions]
    mission["objectives"] = [_objective_row(o) for o in objectives]
    mission["events"] = [_event_row(e) for e in events]
    _attach_settlements(mission["events"], path=path)
    # Anything neither compaction nor the settlement hook froze is reconciled here while the
    # ledger row still exists, then joined again so the response carries it.
    if _backfill_settlements(mission["events"], path=path):
        _attach_settlements(mission["events"], path=path)
    mission["events_next_seq"] = mission["events"][-1]["seq"] if len(events) == limit else None
    return mission


# Two constants rather than one query with a parameterised scope: `(archived_at IS NOT NULL) = ?`
# reads neatly and is not sargable — it defeats `missions_by_archived` and turns every list into a
# full scan. No dynamic SQL either way.
_LIST_LIVE_SQL = "SELECT * FROM missions WHERE archived_at IS NULL ORDER BY updated_at DESC"
_LIST_ARCHIVED_SQL = "SELECT * FROM missions WHERE archived_at IS NOT NULL ORDER BY updated_at DESC"


def _attach_settlements(events: list[dict], *, path: Path | None = None) -> None:
    """Join each decision event to its frozen projection.

    A plain read-time join now, because the projection lives in its own table: nothing has to be
    written during a read to make an old decision render.
    """
    ids = [e["action_id"] for e in events if e["action_id"]]
    if not ids:
        return
    with contextlib.suppress(Exception):  # a read degrades, never fails
        found = settlements_for(ids, path=path)
        for e in events:
            row = found.get(e["action_id"])
            if row is not None:
                e["settlement"] = {
                    "verb": row["verb"],
                    "state": row["state"],
                    "rationale": row["rationale"],
                    "outcome": row["outcome"],
                    "at": row["at"],
                }


def _backfill_settlements(events: list[dict], *, path: Path | None = None) -> int:
    """Fill in any projection that was never frozen, while the ledger row still exists.

    Three things now write a projection, in decreasing order of how much they can be relied on:

    1. :func:`orchestrator_ledger.compact` freezes everything about to fall out of the tail. That
       is the **durable** one — compaction is the only thing that ever removes a ledger row, so it
       is the one moment at which "this is about to become unavailable" is knowable.
    2. ``_settled`` freezes each action as it settles. Best-effort by design: a missions failure
       must never fail or undo a settled ledger transition.
    3. This, on read, for anything the other two missed while the ledger row is still there.

    (1) is what makes the promise hold; (2) and (3) are the cheap paths that mean it is almost
    never needed. Best-effort in its turn — this runs on a read path.
    """
    pending = [
        e["action_id"]
        for e in events
        if e["action_id"]
        and (
            e["settlement"] is None
            # A marker is a placeholder, not an answer — retry it while the ledger row may exist.
            or (e["settlement"] or {}).get("state") == SETTLEMENT_LOST
        )
    ]
    if not pending:
        return 0
    filled = 0
    try:
        from . import orchestrator_ledger

        latest = orchestrator_ledger.latest_by_id()
        for action_id in pending:
            rec = latest.get(action_id)
            if rec is None or rec.get("state") not in orchestrator_ledger.TERMINAL_STATES:
                continue  # still live: the ledger IS the answer, no projection needed yet
            filled += record_settlement(action_id, rec, path=path)
    except Exception:  # noqa: BLE001 — a reconciliation hiccup must never fail a read
        return filled
    return filled


def list_missions(
    *,
    q: str = "",
    project_id: str = "",
    state: str = "",
    archived: bool = False,
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = 0,
    path: Path | None = None,
) -> dict:
    """Missions newest-updated-first, plus facets and a filtered ``total``.

    Filters apply to the **full** archived-scoped set **before** ``limit``/``offset``, so ``total``
    and any "load more" describe the *filtered* result — the same rule ``/api/sessions`` follows.
    Facets are computed over the full scoped set **before** the filters, so the dropdowns keep
    listing every option regardless of the current filter.
    """
    lim = max(1, min(LIST_LIMIT_MAX, int(limit or LIST_LIMIT_DEFAULT)))
    off = max(0, int(offset or 0))
    con = _ready(path)
    try:
        scoped = con.execute(_LIST_ARCHIVED_SQL if archived else _LIST_LIVE_SQL).fetchall()
        # `session_keys`, NOT the `sessions` roster. A list row is not a detail row, and the
        # console proved why: it iterated `m.sessions` on a list row, which has never had one,
        # so any non-empty production list threw instead of rendering the rail.
        #
        # Keys rather than a bare count because the console needs BOTH questions answered and
        # they are the same fact: how many sessions a mission holds (the rail's line), and which
        # sessions are held at all (so a session tracked by ANOTHER mission is not offered as
        # untracked). A count alone would answer the first and silently get the second wrong.
        # One query over an indexed partial set, never an N+1.
        keys: dict[str, list[str]] = {}
        for r in con.execute(
            "SELECT mission_id, session_key FROM mission_sessions WHERE removed_at IS NULL"
        ).fetchall():
            keys.setdefault(r["mission_id"], []).append(r["session_key"])
    finally:
        con.close()
    rows = [_row_to_mission(r) for r in scoped]
    for r in rows:
        r["session_keys"] = keys.get(r["id"], [])
    facets = {
        "projects": sorted({r["project_id"] for r in rows if r.get("project_id")}),
        "states": sorted({r["state"] for r in rows if r.get("state")}),
    }
    needle = (q or "").strip().lower()
    filtered = [
        r
        for r in rows
        if (not needle or needle in (r.get("title") or "").lower())
        and (not project_id or r.get("project_id") == project_id)
        and (not state or r.get("state") == state)
    ]
    return {
        "missions": filtered[off : off + lim],
        "total": len(filtered),
        "limit": lim,
        "offset": off,
        "facets": facets,
    }


def delete_mission(mission_id: str, *, path: Path | None = None) -> bool:
    """Delete a mission and everything it owns — a row delete, never a tombstone.

    ``instruction`` / ``brief`` / recaps are sensitive operator text, so "deleted" has to mean
    the bytes are gone, not that a flag was flipped. ``ON DELETE CASCADE`` plus
    ``foreign_keys=ON`` takes the children.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Never delete a mission mid-operation. Retention already refuses this; the same
            # hazard applies here — the row carries the archive/unarchive journal recovery works
            # from, and the roster naming the agents it was about to reap.
            row = con.execute(
                "SELECT archiving_at, archived_at, unarchiving_at FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is not None and (
                (row["archiving_at"] is not None and row["archived_at"] is None)
                or row["unarchiving_at"] is not None
            ):
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: an archive operation is in flight", status=409
                )
            # Capture the action ids this mission's events reference BEFORE the cascade takes
            # them, so the orphaned projections can be collected afterwards — `rationale` is
            # bounded model text about the operator's work and is deleted with the mission like
            # everything else here.
            referenced = [
                r["action_id"]
                for r in con.execute(
                    "SELECT DISTINCT action_id FROM mission_events "
                    "WHERE mission_id=? AND action_id IS NOT NULL",
                    (mission_id,),
                ).fetchall()
            ]
            cur = con.execute("DELETE FROM missions WHERE id=?", (mission_id,))
            deleted = bool(cur.rowcount)
            _gc_settlements(con, referenced)
            con.execute("COMMIT")
            if not deleted:
                # Nothing deleted here, but an EARLIER delete may still owe a scrub — and this is
                # exactly the call that used to return before reaching it, making the advertised
                # retry a no-op.
                if _flag_get(con, SCRUB_PENDING) is not None and not _scrub(con):
                    raise ScrubFailed("an earlier deletion")
                return False
            if not _scrub(con):
                # The rows ARE gone — that transaction committed — but the bytes are not provably
                # off disk, and this function's whole contract is physical deletion. Say so.
                raise ScrubFailed(mission_id)
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


class ScrubFailed(MissionError):
    """The rows are gone but the bytes were not provably scrubbed. 503 — say so, never imply it."""

    def __init__(self, mission_ids: str) -> None:
        super().__init__(
            f"rows deleted for {mission_ids}, but the store could not be scrubbed "
            f"(a reader is pinning the log); retry",
            status=503,
        )


#: How long to keep trying to truncate the log before admitting the scrub did not happen. Short:
#: the only thing that can block it is another connection holding a read snapshot, and those are
#: request-scoped.
_SCRUB_ATTEMPTS = 20
_SCRUB_PAUSE_S = 0.05


SCRUB_PENDING = "scrub_pending"


def _scrub(con) -> bool:
    """Push the delete all the way to disk, across every file the store owns. Returns success.

    ``secure_delete`` zeroes freed content in the **main database**. It says nothing about the
    write-ahead log, which still holds the pre-delete page images — and while any connection holds
    a read snapshot the log cannot be truncated at all. So a checkpoint that comes back **busy**
    means the plaintext is still on disk, and treating that as success is a claim of physical
    deletion the code did not make good on.

    **A failure leaves a durable obligation**, because "retry" was otherwise a lie: the rows are
    already deleted, so the next call returns early and never reaches the scrub at all. The flag
    is what gives a later delete, retention pass or boot something to act on.
    """
    ok = False
    # **The loop is the waiting strategy, so the checkpoint must REPORT busy rather than block.**
    # The budget below reads as 20 attempts 50ms apart — about a second. That was never the budget
    # that ran: the connection carries a 5s `busy_timeout`, and `wal_checkpoint` honours it, so
    # every attempt sat through five seconds and a one-second budget became a hundred-second one.
    # The pacing here was dead code. Dropping the timeout for the checkpoint gives the loop its
    # own pacing back and gives a briefly-contended log twenty chances instead of one long wait.
    with contextlib.suppress(sqlite3.Error):
        con.execute("PRAGMA busy_timeout=0")
    try:
        for attempt in range(_SCRUB_ATTEMPTS):
            try:
                row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error:
                break
            if row is None or int(row[0]) == 0:
                ok = True
                break
            if attempt + 1 < _SCRUB_ATTEMPTS:
                time.sleep(_SCRUB_PAUSE_S)
    finally:
        # Restored BEFORE the flag write below, which is an ordinary contended write and does
        # want to wait rather than fail fast.
        with contextlib.suppress(sqlite3.Error):
            con.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_MS)}")
    with contextlib.suppress(sqlite3.Error):
        _flag_set(con, SCRUB_PENDING, None if ok else "1")
    return ok


def scrub_if_pending(*, path: Path | None = None) -> bool | None:
    """Discharge an outstanding scrub. ``None`` when there was nothing owed.

    Called wherever a scrub could next succeed — a later delete, the retention pass, and boot —
    so an obligation created by a busy reader is actually collected rather than merely announced.
    """
    with _write_lock:
        con = _ready(path)
        try:
            if _flag_get(con, SCRUB_PENDING) is None:
                return None
            return _scrub(con)
        finally:
            con.close()


# ---------------------------------------------------------------- lifecycle


def _fence_busy(con, mission_id: str) -> None:
    """Refuse an ordinary mutation on a mission that is archived or mid-operation.

    Called INSIDE the transaction rather than before it so it cannot be raced: an adopt that
    slipped past a pre-check would attach a session to a mission whose teardown loop is already
    killing process groups, and a detach would clear the very ``removed_at`` bookkeeping the
    archive is part-way through writing.

    **Archived counts too.** Fencing only the in-flight window left an archived mission fully
    mutable: ``done → running`` succeeded with ``archived_at`` still set, producing a mission
    that reads *running* and appears only in the archived scope — invisible on the rail while
    claiming to be live. Unarchive is the required predecessor, and it is the one mutation that
    is allowed to act on an archived record (it does not call this).
    """
    row = con.execute(
        "SELECT archiving_at, archived_at, unarchiving_at FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    if row is None:
        raise MissionNotFound(mission_id)
    if row["archiving_at"] is not None and row["archived_at"] is None:
        raise MissionError(f"mission {mission_id}: archive in progress", status=409)
    if row["unarchiving_at"] is not None:
        raise MissionError(f"mission {mission_id}: unarchive in progress", status=409)
    if row["archived_at"] is not None:
        raise MissionError(f"mission {mission_id} is archived; unarchive it first", status=409)


def set_state(
    mission_id: str,
    from_state: str,
    to_state: str,
    *,
    outcome: str | None = None,
    detail: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Compare-and-set the lifecycle column, with its ``state`` event, in one transaction.

    ``UPDATE … WHERE id=? AND state=?``: **a zero rowcount is a lost race, not a retry.** A state
    read before an ``await`` cannot be trusted after it — the lesson the ledger's
    ``compare_and_set`` already carries.

    Reaching a terminal state **releases** every session the mission holds (``removed_at`` set);
    the rows stay, so the roster keeps its history. **Reopening re-acquires nothing** — in the
    meantime another mission may legitimately hold those sessions, and silently taking them back
    would either steal one or fail the reopen for a reason unrelated to reopening. Re-attaching is
    the ordinary adopt path, which already answers 409 naming the holder.
    """
    validate_id(mission_id)
    # Type first, then membership. `x in frozenset` raises `TypeError: unhashable type` for a
    # dict or list, which escapes as a 500 — a malformed request body is a 422, always.
    to_state = _require_str(to_state, "to")
    from_state = _require_str(from_state, "from")
    if to_state not in STATES:
        raise MissionError(f"unknown state {to_state!r}", status=422)
    if from_state not in STATES:
        raise MissionError(f"unknown state {from_state!r}", status=422)
    if to_state not in _ALLOWED.get(from_state, frozenset()):
        raise MissionError(f"{from_state} cannot become {to_state}", status=409)
    if outcome is not None:
        outcome = _require_str(outcome, "outcome")
        if outcome not in OUTCOMES:
            raise MissionError(f"unknown outcome {outcome!r}", status=422)
        # An outcome is the *reason a mission closed*, so it has to agree with the state it is
        # recorded against. Validating it against the global set alone let through records like
        # `state=planned, outcome=failed` and `state=done, outcome=abandoned` — a timeline that
        # contradicts itself, which is precisely what the timeline exists to prevent.
        if to_state not in TERMINAL_STATES:
            raise MissionError(
                f"outcome is only meaningful on a terminal state, not {to_state}", status=422
            )
        if outcome != to_state:
            raise MissionError(f"outcome {outcome!r} does not match state {to_state!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute(
                "SELECT state, cwd FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            # The schema CHECK exempts draft/planned/abandoned only, so a cwd-less mission
            # cannot become `failed` without the write blowing up at the database. Refuse it
            # here with a reason rather than letting an IntegrityError surface as a 500 — and
            # keep the CHECK as written: a plan that never launched was not `failed`, it was
            # `abandoned`, which IS available.
            if to_state not in CWD_OPTIONAL_STATES and row["cwd"] is None:
                raise MissionError(
                    f"mission {mission_id}: cannot become {to_state} without a resolved cwd "
                    f"(abandon it instead)",
                    status=409,
                )
            terminal = to_state in TERMINAL_STATES
            cur = con.execute(
                # `outcome` is CLEARED on a non-terminal transition rather than carried forward.
                # `COALESCE(?, outcome)` kept it, so reopening a finished mission produced
                # `state='running', outcome='done'` — a record that says both "in flight" and
                # "finished, successfully". An outcome is the reason a mission CLOSED; a mission
                # that is open has not got one.
                "UPDATE missions SET state=?, updated_at=?, "
                "outcome=CASE WHEN ? THEN ? ELSE NULL END, "
                "closed_at=CASE WHEN ? THEN ? ELSE NULL END WHERE id=? AND state=?",
                (
                    to_state,
                    ts,
                    1 if terminal else 0,
                    outcome,
                    1 if terminal else 0,
                    ts,
                    mission_id,
                    from_state,
                ),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is no longer {from_state}", status=409)
            released: list[str] = []
            if to_state in TERMINAL_STATES:
                held = con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND removed_at IS NULL",
                    (mission_id,),
                ).fetchall()
                released = [h["session_key"] for h in held]
                con.execute(
                    "UPDATE mission_sessions SET removed_at=?, release_reason='closed' "
                    "WHERE mission_id=? AND removed_at IS NULL",
                    (ts, mission_id),
                )
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                text=_cap(detail, EVENT_TEXT_MAX) or None,
                meta={"from": from_state, "to": to_state, "released": released},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


# ---------------------------------------------------------------- session ownership


def adopt(
    mission_id: str,
    session_key: str,
    *,
    role: str = "primary",
    spawned_by: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Attach a session to a mission. One transaction: the row and its ``session`` event.

    Exclusivity is the **database's** job. "Check whether anything holds this, then insert" is not
    race-safe — two concurrent adopts both pass the check — so the partial unique index
    ``mission_sessions_active`` is the arbiter and the loser catches ``IntegrityError`` and gets a
    409 naming the holder.

    Re-adopting into the **same** mission is an UPDATE, not an INSERT: the composite primary key
    means the historical row is still there after a detach, so a fresh insert would collide with
    the mission's own history rather than with another mission's claim.
    """
    validate_id(mission_id)
    role = _require_str(role, "role")
    if role not in SESSION_ROLES:
        raise MissionError(f"unknown role {role!r}", status=422)
    key = (session_key or "").strip()
    if not key:
        raise MissionError("session_key is required", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _adopt_tx(con, mission_id, key, role, spawned_by, ts, path)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    # Read AFTER the lock is released and the connection is closed. `get_mission` opens its own
    # connection and may write a settlement backfill; issuing that while this thread still holds
    # the write lock and an open connection is a knot worth not tying.
    return get_mission(mission_id, path=path) or {}


def _adopt_tx(con, mission_id: str, key: str, role: str, spawned_by, ts: float, path) -> None:
    """The adopt transaction. Caller owns BEGIN/COMMIT so the read can happen outside the lock."""
    _fence_busy(con, mission_id)
    # A leased teardown is a RESERVATION on the session key, and adoption has to honour it.
    # Reaching a terminal state already set `removed_at`, so the partial unique index does not
    # stop this insert — and the archive worker that holds the lease is about to call
    # `cleanup_runtime` and `prov.archive` on that very session. Adopting it here would hand a
    # live session to this mission moments before another mission's worker kills it. The lease is
    # released the instant the teardown settles, so this window is exactly as long as the
    # destructive effect.
    busy = con.execute(_RESERVED_SQL, (key, *_RESERVED_ORDER)).fetchone()
    if busy is not None:
        raise MissionError(
            f"session {key} is {_RESERVED_WHY[busy['archive_state']]} "
            f"by mission {busy['mission_id']}",
            status=409,
        )
    # A session the SIBLING ROUTES are mid-archiving is equally off limits — the reservation is
    # the mutex, and adoption is a mutation of who may touch the provider state. Checking only
    # the mission-side archive states left the other direction open: a mission could adopt a
    # session while `POST /api/sessions/{id}/archive` was inside `cleanup_runtime` for it.
    held = con.execute(
        "SELECT holder, at FROM session_reservations WHERE session_key=?", (key,)
    ).fetchone()
    if held is not None and float(held["at"] or 0) >= ts - RESERVATION_MAX_AGE_S:
        if not str(held["holder"]).startswith(f"mission:{mission_id}"):
            raise MissionError(f"session {key} is being changed by {held['holder']}", status=409)
    mine = con.execute(
        "SELECT removed_at FROM mission_sessions WHERE mission_id=? AND session_key=?",
        (mission_id, key),
    ).fetchone()
    if mine is not None and mine["removed_at"] is None:
        # Already ours: idempotent. The role is refreshed (a primary can be re-declared a sub)
        # but `added_at` is not — the roster's order is history, and re-adopting a session you
        # already hold is not re-joining it.
        con.execute(
            "UPDATE mission_sessions SET role=?, spawned_by=? WHERE mission_id=? AND session_key=?",
            (role, spawned_by, mission_id, key),
        )
        return
    try:
        if mine is not None:
            con.execute(
                "UPDATE mission_sessions SET removed_at=NULL, release_reason=NULL, added_at=?, "
                "role=?, spawned_by=?, archive_state=NULL, archive_error=NULL "
                "WHERE mission_id=? AND session_key=?",
                (ts, role, spawned_by, mission_id, key),
            )
        else:
            con.execute(
                "INSERT INTO mission_sessions "
                "(mission_id, session_key, role, spawned_by, added_at) VALUES (?,?,?,?,?)",
                (mission_id, key, role, spawned_by, ts),
            )
    except sqlite3.IntegrityError:
        # The partial unique index refused it, so somebody else holds it. Name them — from THIS
        # connection, after rolling back, rather than opening a second one under the same lock.
        con.execute("ROLLBACK")
        holder = con.execute(
            "SELECT mission_id FROM mission_sessions "
            "WHERE session_key=? AND removed_at IS NULL LIMIT 1",
            (key,),
        ).fetchone()
        raise SessionHeld(key, holder["mission_id"] if holder else "another mission") from None
    _append_event(con, mission_id, "session", at=ts, session_key=key, meta={"adopted": role})
    con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))


def _holder_of(session_key: str, *, path: Path | None = None) -> str | None:
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT mission_id FROM mission_sessions "
            "WHERE session_key=? AND removed_at IS NULL LIMIT 1",
            (session_key,),
        ).fetchone()
        return row["mission_id"] if row else None
    finally:
        con.close()


class SessionBusy(MissionError):
    """Somebody else holds the right to mutate this session's provider state. 409."""

    def __init__(self, session_key: str, why: str) -> None:
        super().__init__(f"session {session_key}: {why}", status=409)


class OwnershipUnknown(MissionError):
    """The store that records ownership could not be read. 503 — and nothing is destroyed.

    Presentation reads fail soft here; **destruction fails closed**. Terminating a process group
    or moving a transcript on the strength of "we could not check" is the one outcome the
    reservation exists to prevent, and an earlier version of this guard had it backwards.
    """

    def __init__(self) -> None:
        super().__init__("could not verify session ownership; not archiving. Try again", status=503)


#: How long a reservation may be held **without proof of life** before a later caller may take
#: it. This is not a duration cap on the work — a holder that is still running renews (below), so
#: reaching this age means the holder stopped beating, which is the only thing an expiry can
#: honestly detect. A fixed cap with no renewal was the bug: an unusually slow but perfectly
#: healthy `prov.archive` handed the same session to a second worker while the first was inside it.
RESERVATION_MAX_AGE_S = 300

#: How often a holder proves it is alive. Strictly shorter than the expiry, by enough that several
#: beats can be lost to load before a healthy holder is declared dead.
RESERVATION_RENEW_S = 60.0

#: How soon a *failed* beat tries again. A failure must not cost a full interval: the expiry is a
#: fixed budget, and `renew_session` can spend the connection's whole `busy_timeout` before
#: raising, so retrying on the ordinary cadence lets a handful of consecutive failures run a LIVE
#: holder past its own expiry — while it is still inside the destructive effect. Same shape as the
#: scrub loop's budget: the pacing that matters is the one that runs under the condition the
#: retry exists for, not the one on the happy path.
RESERVATION_RETRY_S = 5.0


def beat_wait(renewed: bool, interval: float) -> float:
    """How long a heartbeat waits before its next attempt. The retry policy, as a pure function.

    Extracted so it can be asserted **directly** rather than inferred from elapsed wall-clock. A
    timing-based test of this is only as reliable as the machine running it: the policies differ by
    a fraction of a second, and a loaded CI runner erases that difference — which is precisely the
    "passes on a quiet machine" failure this module's tests warn against elsewhere.
    """
    return interval if renewed else min(interval, RESERVATION_RETRY_S)


#: What a heartbeat learned. ``released`` and ``superseded`` both stop the beat, and collapsing
#: them into one boolean is what made a successful teardown log as a hostile takeover: settlement
#: happens INSIDE the claim, so the very next beat finds its own reservation gone.
RENEWED, RELEASED, SUPERSEDED = "renewed", "released", "superseded"


def renew_session(
    session_key: str,
    token: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> str:
    """Prove the holder of ``token`` is still alive. One of :data:`RENEWED` / :data:`RELEASED` /
    :data:`SUPERSEDED`.

    Renews the reservation **and** the mission-side lease in one transaction, because they are one
    claim: letting the lease age out while the reservation stayed fresh would leave a session no
    worker could re-claim and no worker was working.

    Gated on the token, not the key — a holder that has already been reclaimed must not be able to
    push the expiry of the claim that replaced it.

    **Three answers, not two.** "Did not renew" has two causes that call for opposite reactions:
    the claim was *released* (normally, by this worker's own settlement, which runs while the beat
    is still armed) or it was *superseded* (somebody reclaimed it — a real anomaly). A single
    ``False`` made the ordinary success path indistinguishable from a takeover, which would have
    put a false warning in the log on every completed teardown.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            n = con.execute(
                "UPDATE session_reservations SET at=? WHERE session_key=? AND token=?",
                (ts, session_key, token),
            ).rowcount
            if not n:
                # Distinguish HERE, inside the same transaction, so the answer cannot be
                # invalidated by whatever happens between a failed update and a second look.
                held = con.execute(
                    "SELECT 1 FROM session_reservations WHERE session_key=?", (session_key,)
                ).fetchone()
                con.execute("COMMIT")
                return SUPERSEDED if held is not None else RELEASED
            con.execute(
                "UPDATE mission_sessions SET lease_at=? WHERE session_key=? AND lease_token=?",
                (ts, session_key, token),
            )
            con.execute("COMMIT")
            return RENEWED
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


@contextlib.asynccontextmanager
async def holding(
    session_key: str,
    token: str | None,
    *,
    interval: float = RESERVATION_RENEW_S,
    path: Path | None = None,
) -> AsyncIterator[None]:
    """Keep a claim alive for as long as the external effect inside actually runs.

    Every destructive path here spans calls this process does not control — ``cleanup_runtime``
    waiting on a process group, ``prov.archive`` moving a file on a filesystem that may be under
    load. Without this, the expiry is a bet that those finish inside a fixed window, and losing the
    bet means two workers inside the same irreversible operation.

    **The beat runs on its own thread, not as an asyncio task, and that is the whole point.** The
    protected effect is *synchronous* — `prov.archive` is a file move plus a sidecar write, called
    inline. A task-based heartbeat shares the event-loop thread with it, so the moment the effect
    starts the beat stops being scheduled: exactly when the claim most needs defending, it is least
    able to defend it, and a rival can reclaim the reservation mid-move. A thread beats whether or
    not the loop is blocked, and it keeps that guarantee for call sites written later, which an
    "always dispatch the effect off-loop" rule would depend on every future caller remembering.

    The beat also calls the store **directly** rather than through :func:`run_admitted`. Admission
    exists to refuse work under load; a heartbeat is the one caller that must not be refused, since
    being refused is indistinguishable in its consequences from having died. It is one small write
    per active claim per interval, so it cannot be the thing that overwhelms the pool.

    A beat that fails transiently (store busy) is retried on the next tick. :data:`SUPERSEDED` is
    an anomaly and is logged; :data:`RELEASED` is the ordinary end of the work — settlement runs
    inside the claim — and is silent.
    """
    if not token:
        yield
        return
    stop = threading.Event()

    def _beat() -> None:
        wait = interval
        while not stop.wait(wait):
            try:
                verdict = renew_session(session_key, token, path=path)
            except Exception as exc:  # transient — retry SOON, not on the ordinary cadence
                log.debug("mission: heartbeat on %s deferred (%s)", session_key, type(exc).__name__)
                wait = beat_wait(False, interval)
                continue
            wait = beat_wait(True, interval)
            if verdict == SUPERSEDED:
                log.warning("mission: claim on %s was reclaimed while still working", session_key)
                return
            if verdict == RELEASED:
                return

    beat = threading.Thread(target=_beat, name="mission-heartbeat", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        # Bounded: a wedged beat must not hold the request open. It is a daemon thread, so a
        # thread that outlives this join cannot keep the interpreter alive either.
        beat.join(timeout=5.0)


def reserve_session(
    session_key: str,
    holder: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> str:
    """Take the exclusive right to mutate this session's provider state. Returns a fencing token.

    **This replaces a read-only guard, and the difference is the whole point.** A guard answers a
    question about the past and the caller then acts, so a mission could adopt a session — or a
    mission teardown could begin — in the window between. The reservation *is* the act: one row,
    one transaction, and whoever loses gets a 409 naming the holder.

    Mission ownership is checked **inside the same transaction**, so "no mission is using this"
    cannot go stale between the check and the reservation either.

    The token is a **fencing token**. A reservation reclaimed after expiry mints a new one, so the
    previous holder can no longer settle the operation that replaced it — otherwise a stale worker
    finishing late would clear a fresh worker's lease and let the external effect run twice.

    Raises :class:`SessionBusy` when somebody holds it, and :class:`OwnershipUnknown` when the
    store cannot be read — never silence.
    """
    ts = time.time() if now is None else now
    cutoff = ts - RESERVATION_MAX_AGE_S
    token = uuid.uuid4().hex
    try:
        with _write_lock:
            con = _ready(path)
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT holder, at FROM session_reservations WHERE session_key=?",
                    (session_key,),
                ).fetchone()
                if row is not None and float(row["at"] or 0) >= cutoff:
                    con.execute("ROLLBACK")
                    raise SessionBusy(session_key, f"{row['holder']} is changing it")
                # Mission ownership, in the SAME transaction as the reservation.
                if not holder.startswith("mission:"):
                    owner = con.execute(
                        "SELECT mission_id, removed_at, archive_state FROM mission_sessions "
                        "WHERE session_key=? AND (removed_at IS NULL OR archive_state IS NOT NULL)"
                        " ORDER BY removed_at IS NOT NULL, added_at DESC LIMIT 1",
                        (session_key,),
                    ).fetchone()
                    if owner is not None:
                        why = None
                        if owner["archive_state"] in RESERVED_ARCHIVE_STATES:
                            why = (
                                f"mission {owner['mission_id']} is archiving it "
                                f"({_RESERVED_WHY.get(owner['archive_state'])})"
                            )
                        elif owner["removed_at"] is None:
                            why = f"mission {owner['mission_id']} is using it"
                        if why:
                            con.execute("ROLLBACK")
                            raise SessionBusy(session_key, why)
                con.execute(
                    "INSERT INTO session_reservations (session_key, token, holder, at) "
                    "VALUES (?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
                    "token=excluded.token, holder=excluded.holder, at=excluded.at",
                    (session_key, token, holder, ts),
                )
                con.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
    except MissionError:
        raise
    except Exception as e:  # noqa: BLE001 — an unprovable ownership must not authorise destruction
        raise OwnershipUnknown from e
    return token


def release_session(session_key: str, token: str, *, path: Path | None = None) -> bool:
    """Give the reservation back. Only the holder of ``token`` can — a stale worker cannot free
    a reservation that has already been reclaimed by somebody else."""
    with contextlib.suppress(Exception):
        with _write_lock:
            con = _ready(path)
            try:
                return bool(
                    con.execute(
                        "DELETE FROM session_reservations WHERE session_key=? AND token=?",
                        (session_key, token),
                    ).rowcount
                )
            finally:
                con.close()
    return False


def reservation_of(session_key: str, *, path: Path | None = None) -> dict | None:
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT * FROM session_reservations WHERE session_key=?", (session_key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def holder_of(session_key: str, *, path: Path | None = None) -> str | None:
    """Which OPEN mission holds ``session_key``, or ``None``."""
    return _holder_of(session_key, path=path)


def detach(
    mission_id: str,
    session_key: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Release a session explicitly. The row stays; only ``removed_at`` is stamped."""
    validate_id(mission_id)
    key = (session_key or "").strip()
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            cur = con.execute(
                # 'detached' is the operator saying this is not part of the mission any more.
                # Archive reads this to know it must not cross that line.
                "UPDATE mission_sessions SET removed_at=?, release_reason='detached' "
                "WHERE mission_id=? AND session_key=? AND removed_at IS NULL",
                (ts, mission_id, key),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} does not hold {key}", status=404)
            _append_event(
                con, mission_id, "session", at=ts, session_key=key, meta={"detached": True}
            )
            con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


def active_session_keys(mission_id: str, *, path: Path | None = None) -> list[str]:
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key FROM mission_sessions WHERE mission_id=? AND removed_at IS NULL "
            "ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        return [r["session_key"] for r in rows]
    finally:
        con.close()


def all_active_memberships(*, path: Path | None = None) -> dict[str, str]:
    """``session_key -> mission_id`` for every open membership. One query for the rail."""
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key, mission_id FROM mission_sessions WHERE removed_at IS NULL"
        ).fetchall()
        return {r["session_key"]: r["mission_id"] for r in rows}
    finally:
        con.close()


# ---------------------------------------------------------------- objectives


def objectives(mission_id: str, *, path: Path | None = None) -> list[dict]:
    validate_id(mission_id)
    con = _ready(path)
    try:
        # An unknown mission is a 404, not an empty list: "this mission has no objectives" and
        # "this mission does not exist" are different answers and the client acts on them
        # differently.
        if con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
            raise MissionNotFound(mission_id)
        rows = con.execute(
            "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
            (mission_id,),
        ).fetchall()
        return [_objective_row(r) for r in rows]
    finally:
        con.close()


def _validate_probe(probe: object, probe_args: object, gate: bool) -> tuple[str, str | None]:
    kind = probe if isinstance(probe, str) else ""
    if kind not in PROBE_KINDS:
        raise MissionError(f"unknown probe {probe!r}", status=422)
    if gate and kind in NON_GATING_PROBES:
        # "the agent believes it wrote tests" is not evidence that it did.
        raise MissionError(f"probe {kind} may not be a gate", status=422)
    if probe_args is not None and not isinstance(probe_args, dict):
        raise MissionError("probe_args must be an object", status=422)
    return kind, _json_or_none(probe_args, PROBE_ARGS_MAX)


def patch_objectives(
    mission_id: str,
    ops: list[dict],
    *,
    source: str = "operator",
    now: float | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Apply operator edits — ``add`` / ``drop`` / ``retitle`` / ``waive`` / ``reorder``.

    Three rules make edits honest, and each one is a test:

    * **An edit never retroactively marks an objective met.** ``state`` / ``met_at`` / ``observed``
      are not writable on this path at all; the only state an operator may set is ``waived``,
      which is a decision not to require the objective, not a claim that it holds.
    * **Adding an unmet gating objective to a mission in ``review`` moves it back to ``running``**
      — in the same transaction as the insert, so the list and the state can never disagree.
    * ``agent_judged`` may not gate, rejected at write time.
    """
    validate_id(mission_id)
    source = _require_str(source, "source")
    if source not in OBJECTIVE_SOURCES:
        raise MissionError(f"unknown source {source!r}", status=422)
    if not isinstance(ops, list) or not ops:
        raise MissionError("ops (a non-empty list) is required", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            state_row = con.execute(
                "SELECT state FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if state_row is None:
                raise MissionNotFound(mission_id)
            added_unmet_gate = False
            applied: list[dict] = []
            for op in ops:
                if not isinstance(op, dict):
                    raise MissionError("each op must be an object", status=422)
                kind = op.get("op")
                _check_op_fields(op)
                if kind == "add":
                    added_unmet_gate |= _op_add(con, mission_id, op, source, ts)
                elif kind == "drop":
                    _op_drop(con, mission_id, op)
                elif kind == "retitle":
                    _op_retitle(con, mission_id, op)
                elif kind == "waive":
                    _op_waive(con, mission_id, op, ts)
                elif kind == "reorder":
                    _op_reorder(con, mission_id, op)
                else:
                    raise MissionError(f"unknown objective op {kind!r}", status=422)
                applied.append({"op": kind, "key": op.get("key")})
            reopened = False
            if added_unmet_gate and state_row["state"] == "review":
                cur = con.execute(
                    "UPDATE missions SET state='running', updated_at=?, closed_at=NULL "
                    "WHERE id=? AND state='review'",
                    (ts, mission_id),
                )
                reopened = bool(cur.rowcount)
                if reopened:
                    _append_event(
                        con,
                        mission_id,
                        "state",
                        at=ts,
                        meta={
                            "from": "review",
                            "to": "running",
                            "why": "an unmet gating objective was added",
                        },
                    )
            _append_event(
                con,
                mission_id,
                "objective",
                at=ts,
                meta={"by": source, "ops": applied, "reopened": reopened},
            )
            con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return objectives(mission_id, path=path)


#: What an operator edit may name. Everything else is refused rather than silently dropped —
#: `state` / `met_at` / `observed` in particular, so "an edit never marks an objective met" is an
#: answer the caller GETS rather than a field that quietly did nothing.
_OP_FIELDS: dict[str, frozenset[str]] = {
    "add": frozenset({"op", "key", "title", "gate", "probe", "probe_args"}),
    "drop": frozenset({"op", "key"}),
    "retitle": frozenset({"op", "key", "title"}),
    "waive": frozenset({"op", "key"}),
    "reorder": frozenset({"op", "keys"}),
}
#: Named separately so the refusal can say *why* rather than "unknown field".
_SETTLED_BY_OBSERVATION = frozenset({"state", "met_at", "observed"})


def _check_op_fields(op: dict) -> None:
    kind = op.get("op")
    allowed = _OP_FIELDS.get(kind, frozenset())
    extra = set(op) - allowed
    settled = extra & _SETTLED_BY_OBSERVATION
    if settled:
        raise MissionError(
            f"{', '.join(sorted(settled))} is set from a server observation, never from an edit",
            status=422,
        )
    if extra:
        raise MissionError(f"unknown field(s) for {kind}: {', '.join(sorted(extra))}", status=422)


def _op_add(con, mission_id: str, op: dict, source: str, ts: float) -> bool:
    """Insert one objective. Returns True iff it is an unmet **gate** (which can reopen review)."""
    count = con.execute(
        "SELECT COUNT(*) FROM mission_objectives WHERE mission_id=?", (mission_id,)
    ).fetchone()[0]
    if int(count) >= OBJECTIVES_MAX:
        raise MissionError(f"a mission may hold at most {OBJECTIVES_MAX} objectives", status=422)
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    if not key or not re.match(r"^[a-z0-9][a-z0-9_-]*$", key):
        raise MissionError("objective key must be a lowercase slug", status=422)
    title = _cap(op.get("title"), OBJECTIVE_TITLE_MAX)
    if not title:
        raise MissionError("objective title is required", status=422)
    gate = strict_bool(op.get("gate"), "gate", default=False)
    probe, args = _validate_probe(op.get("probe", "none"), op.get("probe_args"), gate)
    nxt = con.execute(
        "SELECT COALESCE(MAX(ord), -1) + 1 FROM mission_objectives WHERE mission_id=?",
        (mission_id,),
    ).fetchone()[0]
    try:
        con.execute(
            "INSERT INTO mission_objectives "
            "(mission_id, key, ord, title, probe, probe_args, gate, state, source) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?)",
            (mission_id, key, int(nxt), title, probe, args, 1 if gate else 0, source),
        )
    except sqlite3.IntegrityError:
        raise MissionError(f"objective {key} already exists", status=409) from None
    return gate


def _op_drop(con, mission_id: str, op: dict) -> None:
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    cur = con.execute(
        "DELETE FROM mission_objectives WHERE mission_id=? AND key=?", (mission_id, key)
    )
    if not cur.rowcount:
        raise MissionError(f"unknown objective {key}", status=404)


def _op_retitle(con, mission_id: str, op: dict) -> None:
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    title = _cap(op.get("title"), OBJECTIVE_TITLE_MAX)
    if not title:
        raise MissionError("objective title is required", status=422)
    cur = con.execute(
        "UPDATE mission_objectives SET title=? WHERE mission_id=? AND key=?",
        (title, mission_id, key),
    )
    if not cur.rowcount:
        raise MissionError(f"unknown objective {key}", status=404)


def _op_waive(con, mission_id: str, op: dict, ts: float) -> None:
    """Waive = "I have decided this is not required", which is NOT "this holds".

    ``met_at`` and ``observed`` stay untouched precisely so a waived objective can never be read
    back as one a probe settled.
    """
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    cur = con.execute(
        "UPDATE mission_objectives SET state='waived' WHERE mission_id=? AND key=? "
        "AND state != 'met'",
        (mission_id, key),
    )
    if not cur.rowcount:
        row = con.execute(
            "SELECT state FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, key),
        ).fetchone()
        if row is None:
            raise MissionError(f"unknown objective {key}", status=404)
        raise MissionError(f"objective {key} is already met", status=409)


def _op_reorder(con, mission_id: str, op: dict) -> None:
    keys = op.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise MissionError("reorder needs keys (a list of strings)", status=422)
    have = {
        r["key"]
        for r in con.execute(
            "SELECT key FROM mission_objectives WHERE mission_id=?", (mission_id,)
        ).fetchall()
    }
    # Set equality alone is not "exactly once": ['a','b','b'] on {'a','b'} passes it and leaves a
    # gapped order. Cardinality is the half that catches a duplicate.
    if len(keys) != len(set(keys)) or set(keys) != have:
        raise MissionError("reorder must list every objective exactly once", status=422)
    # Two passes through a disjoint range: `ord` has no UNIQUE constraint today, but a one-pass
    # renumber would still transiently collide if one ever gains it.
    for i, key in enumerate(keys):
        con.execute(
            "UPDATE mission_objectives SET ord=? WHERE mission_id=? AND key=?",
            (i + 10_000, mission_id, key),
        )
    for i, key in enumerate(keys):
        con.execute(
            "UPDATE mission_objectives SET ord=? WHERE mission_id=? AND key=?",
            (i, mission_id, key),
        )


def unmet_gates(mission_id: str, *, path: Path | None = None) -> list[str]:
    """Gating objectives that do not hold. ``likely_done`` may not even be proposed while this
    is non-empty (Phase 5) — stated here so the rule lives with the data it reads."""
    return [
        o["key"]
        for o in objectives(mission_id, path=path)
        if o["gate"] and o["state"] in _UNMET_OBJECTIVE_STATES
    ]


# ---------------------------------------------------------------- settlement projection


#: Bounds on the projection. It is a *summary* of a ledger row, not a copy of it.
#: The projection's ``state`` when the ledger row was already gone when the reference was made.
#: Deliberately not a ledger state — it is the honest answer to "what did this decision say?" when
#: the answer no longer exists, and it maps onto §16's "absent from the ledger ⇒ historical, no
#: controls, no outcome asserted" rather than rendering an empty decision forever.
SETTLEMENT_LOST = "source_compacted"

SETTLEMENT_RATIONALE_MAX = 600
SETTLEMENT_OUTCOME_MAX = 300


def record_settlement(action_id: str, record: dict, *, path: Path | None = None) -> int:
    """Freeze a bounded projection of a settled ledger action. Returns rows written (0 or 1).

    **Why this exists.** The ledger compacts terminal actions down to a globally bounded tail
    (``HISTORY_MAX``), which is right for a feed and wrong for a mission that outlives it: a
    six-week-old mission would keep its ``approval`` event while the row carrying the verb,
    rationale and outcome had already been compacted away, and the timeline would render a
    decision with no content.

    **Written once, never updated** — ``INSERT OR IGNORE`` on the ``action_id`` primary key. The
    ledger stays authoritative while its row exists; this is what keeps the mission readable after
    it does not.

    **In its own table, owned by no mission.** On the event row it was hostage to the event cap:
    bounding the timeline deleted the very durability promise the cap was protecting. Here the
    feed can be bounded freely.

    Called from ``orchestrator_ledger._settled`` (per-settlement) and from
    ``orchestrator_ledger.compact`` (for everything about to fall out of the tail), under that
    call site's two documented rules: after the durable append and **outside** the ledger lock,
    and best-effort. Hence the short busy timeout — this runs inside somebody else's transition
    and may not park a thread for five seconds on our account.
    """
    if not isinstance(action_id, str) or not action_id:
        return 0
    with _write_lock:
        con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
        try:
            # ONLY for an action a mission actually references. The hook fires on every ledger
            # settlement in the app, most of which belong to no mission at all — copying those in
            # put bounded model text (`rationale`, `outcome`) into this store with no mission to
            # own it, no path that could ever name it for collection, and no retention window
            # over it. Referenced-only means an orphan cannot be created in the first place.
            if not _is_referenced(con, action_id):
                return 0
            # Written once — except over a `source_compacted` marker, which is precisely the
            # "we could not find out" state. If the truth turns up later it must win; anything
            # else stays immutable.
            con.execute(
                "DELETE FROM mission_settlements WHERE action_id=? AND state=?",
                (action_id, SETTLEMENT_LOST),
            )
            return (
                con.execute(
                    "INSERT OR IGNORE INTO mission_settlements "
                    "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
                    (
                        action_id,
                        _cap(record.get("verb"), 40) or None,
                        _cap(record.get("state"), 40) or None,
                        _cap(record.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
                        _cap(record.get("outcome") or record.get("detail"), SETTLEMENT_OUTCOME_MAX)
                        or None,
                        float(record.get("ts") or time.time()),
                    ),
                ).rowcount
                or 0
            )
        finally:
            con.close()


def _is_referenced(con, action_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM mission_events WHERE action_id=? LIMIT 1", (action_id,)
        ).fetchone()
        is not None
    )


def referenced_action_ids(action_ids: list[str], *, path: Path | None = None) -> set[str]:
    """Which of these actions a mission timeline points at.

    The ledger asks this before compacting, so it projects exactly the rows a mission would
    otherwise be left unable to render — and nothing else.
    """
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return set()
    # The SHORT wait, like every other step on this path: the ledger calls this while holding its
    # own lock, so a five-second block here would stall every orchestrator append behind it.
    con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
    try:
        sql = (  # noqa: S608
            "SELECT DISTINCT action_id FROM mission_events WHERE action_id IN ({m})"
        ).format(m=",".join("?" * len(ids)))
        return {r["action_id"] for r in con.execute(sql, tuple(ids)).fetchall()}
    finally:
        con.close()


def record_settlements(records: list[dict], *, path: Path | None = None) -> int:
    """Freeze many projections in one hold — the pre-compaction pass.

    The ledger is about to drop these rows, which is the one moment at which "this is about to
    become unavailable" is knowable. Doing it per-row through :func:`record_settlement` would take
    and drop the lock once per action for no reason.
    """
    rows = [r for r in records if isinstance(r.get("id"), str) and r["id"]]
    if not rows:
        return 0
    with _write_lock:
        con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
        try:
            con.execute("BEGIN IMMEDIATE")
            written = 0
            for r in rows:
                if not _is_referenced(con, r["id"]):
                    continue  # never create an orphan — see `record_settlement`
                # Replace a `source_compacted` marker, exactly as the single-row writer does.
                # Without this the bulk pass counted the marker as a projection and compaction
                # went on to delete the row that could still have told the truth.
                con.execute(
                    "DELETE FROM mission_settlements WHERE action_id=? AND state=?",
                    (r["id"], SETTLEMENT_LOST),
                )
                written += (
                    con.execute(
                        "INSERT OR IGNORE INTO mission_settlements "
                        "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
                        (
                            r["id"],
                            _cap(r.get("verb"), 40) or None,
                            _cap(r.get("state"), 40) or None,
                            _cap(r.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
                            _cap(r.get("outcome") or r.get("detail"), SETTLEMENT_OUTCOME_MAX)
                            or None,
                            float(r.get("ts") or time.time()),
                        ),
                    ).rowcount
                    or 0
                )
            con.execute("COMMIT")
            return written
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settlements_for(action_ids: list[str], *, path: Path | None = None) -> dict[str, dict]:
    """The frozen projections for these actions, by id."""
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return {}
    con = _ready(path)
    try:
        sql = (  # noqa: S608
            "SELECT * FROM mission_settlements WHERE action_id IN ({placeholders})"
        ).format(placeholders=",".join("?" * len(ids)))
        return {r["action_id"]: dict(r) for r in con.execute(sql, tuple(ids)).fetchall()}
    finally:
        con.close()


def _gc_settlements(con, action_ids: list[str]) -> int:
    """Drop projections nothing references any more.

    ``rationale`` is bounded model text about the operator's work, so it is deleted with the
    mission like everything else here — the normalized table must not become the one place
    sensitive text outlives its mission. Scoped to the ids just orphaned, so this never becomes a
    table scan on an ordinary append.
    """
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    sql = (
        f"DELETE FROM mission_settlements WHERE action_id IN ({marks}) AND action_id NOT IN ("  # noqa: S608
        "  SELECT action_id FROM mission_events WHERE action_id IS NOT NULL)"
    )
    return con.execute(sql, tuple(ids)).rowcount or 0


# ---------------------------------------------------------------- derived attention


def derive_needs_you(mission_ids: list[str], *, path: Path | None = None) -> dict[str, dict]:
    """Compute the attention flag per mission — **at read time, never stored**.

    Storing it would create a second source of truth that goes stale exactly when it matters:
    the operator decides an action in the bell, and the rail keeps saying "needs you" because
    nothing told it. Three terms, ORed:

    1. a live ledger action in ``OPERATOR_PENDING_STATES`` on one of the mission's ACTIVE
       sessions — the ledger is what states that an action is pending, never the model;
    2. ``intervention_required`` on one of those sessions, read from the metadata sidecar, which
       is where ``pulse.build_cards`` reads the card's own flag from (identical source, and no
       ``scan_all()`` on a list request);
    3. an **open question** — a ``question`` event with no later ``answer`` on the mission. It
       has no producer until Phase 3 and is included now so Phase 3 need not reopen this.

    Best-effort per term: a ledger or sidecar hiccup degrades the flag, it never fails the list.
    """
    from . import metadata, orchestrator_ledger

    out: dict[str, dict] = {m: {"needs_you": False, "why": []} for m in mission_ids}
    if not mission_ids:
        return out

    con = _ready(path)
    try:
        placeholders = ",".join("?" for _ in mission_ids)
        rows = con.execute(
            # noqa justification: `placeholders` is a run of `?` sized by len(mission_ids); every
            # id is BOUND, never interpolated. Same for the grouped query below.
            f"SELECT mission_id, session_key FROM mission_sessions "  # noqa: S608
            f"WHERE mission_id IN ({placeholders}) AND removed_at IS NULL",
            tuple(mission_ids),
        ).fetchall()
        open_q = con.execute(
            f"SELECT mission_id, MAX(CASE WHEN kind='question' THEN seq END) AS q, "  # noqa: S608
            f"MAX(CASE WHEN kind='answer' THEN seq END) AS a "
            f"FROM mission_events WHERE mission_id IN ({placeholders}) GROUP BY mission_id",
            tuple(mission_ids),
        ).fetchall()
    finally:
        con.close()

    by_mission: dict[str, list[str]] = {}
    for r in rows:
        by_mission.setdefault(r["mission_id"], []).append(r["session_key"])

    pending: set[str] = set()
    try:
        for a in orchestrator_ledger.live_actions():
            if a.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES:
                sid = str(a.get("session_id") or "")
                if sid:
                    pending.add(sid)
    except Exception:  # noqa: BLE001 — a ledger hiccup degrades the flag, never the list
        pending = set()

    try:
        meta_index = metadata.load()
    except Exception:  # noqa: BLE001 — same rule for the sidecar
        meta_index = {}

    for mid in mission_ids:
        why = out[mid]["why"]
        for key in by_mission.get(mid, []):
            if key in pending:
                why.append("decision")
                break
        for key in by_mission.get(mid, []):
            m = meta_index.get(key)
            if m is not None and getattr(m, "intervention_required", False):
                why.append("intervention")
                break
        out[mid]["needs_you"] = bool(why)

    for r in open_q:
        q, a = r["q"], r["a"]
        if q is not None and (a is None or a < q):
            out[r["mission_id"]]["why"].append("question")
            out[r["mission_id"]]["needs_you"] = True
    return out


# ---------------------------------------------------------------- archive (durable, 3-step)


def begin_archive(
    mission_id: str,
    *,
    abandon: bool = False,
    resume: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Step 1 — one transaction, no awaits inside it. Returns the sessions to tear down.

    **Terminal-state-only.** ``POST …/archive`` on a live mission is a **409**, not a prompt:
    there is no path that leaves a mission simultaneously running and archived. A live mission is
    archived by *abandoning it first*, in one explicitly confirmed step — ``{"abandon": true}``
    performs ``→ abandoned`` and stamps ``archiving_at`` in the same transaction. Two distinct
    transitions, one deliberate request, rather than one endpoint that sometimes means "tidy up"
    and sometimes means "kill my work".
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archived_at"] is not None:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is already archived", status=409)
            if row["archiving_at"] is not None:
                # An archive is already in flight and it has an OWNER. Handing a second caller
                # the worklist looked idempotent and was not: once the first worker leased every
                # row, the second saw an empty pending list, walked straight to
                # `finish_archive`, and marked the mission archived while that worker was still
                # inside `cleanup_runtime` killing the session.
                if not resume:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id}: archive already in progress", status=409
                    )
                # Boot recovery may take the MISSION-level claim over. That is safe without
                # proving the previous owner died, because it is not what guards the destructive
                # effect: each session's teardown lease is, and `claim_session_teardown` still
                # answers "taken" for every session whose lease is being kept alive. Taking this
                # over gets recovery as far as the per-session gate and no further.
                token = _new_op_token()
                con.execute("UPDATE missions SET op_token=? WHERE id=?", (token, mission_id))
                # Recovery re-drives FAILED sessions too, not just the ones left mid-flight. A
                # worker that dies mid-teardown settles its own lease as `failed` (so it is never
                # stranded), and that failure may have come *after* the provider effect landed —
                # which only a retry can discover, and which `_archive_idempotent` makes safe by
                # answering `already_archived` rather than repeating the effect. Scoped to the
                # resume path: an ordinary request never reaches this branch.
                con.execute(
                    "UPDATE mission_sessions SET archive_state='pending', archive_error=NULL "
                    "WHERE mission_id=? AND archive_state='failed'",
                    (mission_id,),
                )
                pending = _pending_sessions(con, mission_id)
                con.execute("COMMIT")
                return {
                    "mission_id": mission_id,
                    "sessions": pending,
                    "resumed": True,
                    "op_token": token,
                }
            if row["state"] not in TERMINAL_STATES:
                if not abandon:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id} is {row['state']}; archive requires a terminal "
                        f"state or an explicit abandon",
                        status=409,
                    )
                cur = con.execute(
                    "UPDATE missions SET state='abandoned', outcome='abandoned', updated_at=?, "
                    "closed_at=? WHERE id=? AND state=?",
                    (ts, ts, mission_id, row["state"]),
                )
                if not cur.rowcount:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id} is no longer {row['state']}", status=409
                    )
                _append_event(
                    con,
                    mission_id,
                    "state",
                    at=ts,
                    meta={"from": row["state"], "to": "abandoned", "why": "archive"},
                )
            # Re-evaluate the skip decision from scratch on every attempt. A row marked
            # `skipped` because another mission held the key must not STAY skipped once that
            # holder releases it — otherwise a re-archive after an unarchive silently omits a
            # session that is now safe to reap.
            con.execute(
                "UPDATE mission_sessions SET archive_state=NULL, archive_error=NULL "
                "WHERE mission_id=? AND archive_state='skipped'",
                (mission_id,),
            )
            # Target the mission's whole ROSTER, not just what it still actively holds.
            # Reaching a terminal state already released ownership (`removed_at` set), so keying
            # the teardown on `removed_at IS NULL` would archive the record of a `done` mission
            # and quietly leave every one of its agents running — the exact runtime cost #840 §11
            # exists to reclaim.
            #
            # The one exception is the session somebody else now holds: it was released and
            # legitimately re-adopted, and tearing it down would kill another mission's agent.
            # Those are marked `skipped` rather than silently omitted, so `finish_archive` can
            # say what it did not touch instead of implying a clean sweep.
            con.execute(
                "UPDATE mission_sessions SET archive_state='skipped', archive_error=NULL "
                "WHERE mission_id=? AND COALESCE(release_reason,'closed') != 'detached' "
                "AND session_key IN ("
                "  SELECT session_key FROM mission_sessions"
                "  WHERE removed_at IS NULL AND mission_id != ?)",
                (mission_id, mission_id),
            )
            # Everything on the roster EXCEPT what the operator explicitly detached. Reaching a
            # terminal state releases ownership and those sessions are still the mission's to
            # reap; a detach is the operator removing the session from the mission, and both set
            # `removed_at` — so without the reason, archiving the roster terminated work that had
            # been deliberately taken out of it.
            con.execute(
                "UPDATE mission_sessions SET archive_state='pending', archive_error=NULL "
                "WHERE mission_id=? AND COALESCE(archive_state,'') != 'skipped' "
                "AND COALESCE(release_reason,'closed') != 'detached'",
                (mission_id,),
            )
            token = _new_op_token()
            con.execute(
                "UPDATE missions SET archiving_at=?, op_token=? WHERE id=?",
                (ts, token, mission_id),
            )
            pending = _pending_sessions(con, mission_id)
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={"phase": "begin", "sessions": [p["session_key"] for p in pending]},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "sessions": pending,
        "resumed": False,
        "op_token": token,
    }


def _archive_outcome(con, mission_id: str) -> dict:
    """What this archive actually achieved, read from the roster rather than assumed."""
    return {
        "not_archived": [
            dict(r)
            for r in con.execute(
                "SELECT session_key, archive_error FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='failed'",
                (mission_id,),
            ).fetchall()
        ],
        "skipped": [
            r["session_key"]
            for r in con.execute(
                "SELECT session_key FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='skipped'",
                (mission_id,),
            ).fetchall()
        ],
    }


def _pending_sessions(con, mission_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT session_key, role FROM mission_sessions "
        "WHERE mission_id=? AND archive_state='pending' ORDER BY added_at ASC",
        (mission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def claim_session_teardown(
    mission_id: str, session_key: str, *, now: float | None = None, path: Path | None = None
) -> tuple[str, str | None]:
    """Take the exclusive lease on one session's external teardown. Returns what to do.

    Three things have to be decided together, under one transaction, or the destructive effect
    runs when it should not:

    * ``"claimed"`` — this worker owns the teardown. ``pending → in_progress`` is a CAS, so a
      concurrent request (or a retry of the same one, which ``begin_archive`` hands the *same*
      pending rows) loses and does nothing. ``prov.archive`` is not idempotent in general, so
      without this the external effect runs twice.
    * ``"skipped"`` — **another open mission holds this session now.** The roster snapshot taken
      at ``begin_archive`` is stale by the time teardown runs: reaching a terminal state released
      the key, and the partial unique index only guards ``removed_at IS NULL``, so mission B can
      legitimately adopt it in between. Tearing it down then kills a live agent belonging to a
      different mission. Re-checked HERE, at the write boundary, not at plan time.
    * ``"taken"`` — somebody else already holds the lease; do nothing.

    Returns ``(verdict, token)``. The token is the **fencing token** and the worker must present it
    at settlement: a lease reclaimed after expiry mints a new one, so a worker that finishes late
    cannot settle the operation that replaced it.
    """
    ts = time.time() if now is None else now
    # The reservation is the mutex the sibling session routes also take, so the two paths exclude
    # each other rather than merely checking each other. Taken FIRST: if somebody else is changing
    # this session's provider state, there is nothing to claim.
    try:
        token = reserve_session(session_key, f"mission:{mission_id}", now=now, path=path)
    except SessionBusy:
        return "taken", None
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archive_state FROM mission_sessions WHERE mission_id=? AND session_key=?",
                (mission_id, session_key),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "gone", None
            if row["archive_state"] != "pending":
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "taken", None
            holder = con.execute(
                "SELECT mission_id FROM mission_sessions "
                "WHERE session_key=? AND removed_at IS NULL AND mission_id != ? LIMIT 1",
                (session_key, mission_id),
            ).fetchone()
            if holder is not None:
                con.execute(
                    "UPDATE mission_sessions SET archive_state='skipped', archive_error=? "
                    "WHERE mission_id=? AND session_key=?",
                    (f"held by mission {holder['mission_id']}", mission_id, session_key),
                )
                _append_event(
                    con,
                    mission_id,
                    "archive",
                    at=ts,
                    session_key=session_key,
                    meta={"phase": "session", "outcome": "skipped", "holder": holder["mission_id"]},
                )
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "skipped", None
            con.execute(
                "UPDATE mission_sessions SET archive_state='in_progress', lease_owner=?, "
                "lease_at=?, lease_token=? WHERE mission_id=? AND session_key=? "
                "AND archive_state='pending'",
                (PROCESS_EPOCH, ts, token, mission_id, session_key),
            )
            con.execute("COMMIT")
            return "claimed", token
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def reopen_stale_leases(
    mission_id: str, *, now: float | None = None, path: Path | None = None
) -> int:
    """Reopen leases held by a process that is gone. Returns how many.

    Covers both kinds — a teardown lease (``in_progress``) goes back to ``pending``, a restore
    lease (``restoring``) back to ``done`` — because missing the restore half made recovery skip
    the session, invoke no provider call, and then report the mission unarchived.

    **Expiry is the only proof of death, and it is now worth something.** Two earlier premises
    were wrong in the same way — each assumed a fact it had not established:

    * *"any lease found here belongs to a crashed worker"* — false the moment recovery moved from
      boot to a background task running beside live requests.
    * *"a lease not stamped with our own :data:`PROCESS_EPOCH` outlived its process"* — false
      whenever a second app instance is serving the same store, which this app supports (the
      single-writer lock is explicitly cross-instance). A foreign epoch means *another* process,
      not a *dead* one, and reclaiming it immediately put two live workers inside the same
      irreversible external effect. A fencing token cannot fix that: it makes the loser's database
      write fail, and says nothing about the process it already terminated or the file it moved.

    So a lease is reclaimed only once it has gone :data:`LEASE_MAX_AGE_S` without a heartbeat.
    That is a real signal rather than a timing assumption, because a live holder renews (see
    :func:`holding`) — an expired lease means the holder stopped beating, in any process. The
    owner stamp stays, but it is now diagnostic, not a licence to reclaim.
    """
    cutoff = (time.time() if now is None else now) - LEASE_MAX_AGE_S
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Exactly which sessions are being reclaimed, decided ONCE and reused below. The
            # reservation is half of the same claim, so it must be released for these rows and
            # ONLY these: a blanket delete would strip the mutex from a sibling session whose
            # worker is still beating, and an independent age check on the reservation would give
            # one claim two clocks — the lease reopening while the mutex it needs stayed held.
            dead = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions WHERE mission_id=? "
                    "AND archive_state IN ('in_progress','restoring') "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).fetchall()
            ]
            n = (
                con.execute(
                    "UPDATE mission_sessions SET archive_state='pending', lease_owner=NULL, "
                    "lease_at=NULL, lease_token=NULL "
                    "WHERE mission_id=? AND archive_state='in_progress' "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).rowcount
                or 0
            )
            n += (
                con.execute(
                    "UPDATE mission_sessions SET archive_state='done', lease_owner=NULL, "
                    "lease_at=NULL, lease_token=NULL "
                    "WHERE mission_id=? AND archive_state='restoring' "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).rowcount
                or 0
            )
            # Reclaiming a lease reclaims its RESERVATION too — they are one claim, stamped
            # together and renewed together, so the lease's expiry is proof for both.
            for key in dead:  # one statement per key: no SQL assembled from a Python expression
                con.execute(
                    "DELETE FROM session_reservations WHERE holder=? AND session_key=?",
                    (f"mission:{mission_id}", key),
                )
            con.execute("COMMIT")
            return n
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def next_lease_expiry(*, path: Path | None = None) -> float | None:
    """When the earliest still-held lease becomes reclaimable, or ``None`` if none is held.

    Recovery needs this because reclamation is now expiry-only: after a crash, a lease left by the
    dead process is untouchable until it ages out, so a retry loop with a delay shorter than
    :data:`LEASE_MAX_AGE_S` would burn its whole budget re-reading a worklist it cannot act on and
    then report the mission unrecoverable. Waiting the *remaining* time is the difference between
    patient and stuck.
    """
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT MIN(lease_at) AS t FROM mission_sessions "
            "WHERE lease_at IS NOT NULL AND archive_state IN ('in_progress','restoring')"
        ).fetchone()
    finally:
        con.close()
    return None if row is None or row["t"] is None else float(row["t"]) + LEASE_MAX_AGE_S


def settle_session_archive(
    mission_id: str,
    session_key: str,
    outcome: str,
    *,
    reason: str = "",
    token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """Step 2's settlement — one tiny transaction per session, outside any teardown.

    **Requires the fencing token the claim minted.** A lease reclaimed after expiry mints a new
    token, so a worker that finishes late cannot settle the operation that replaced it — otherwise
    the stale result wins, the fresh lease is cleared, and the external effect can run twice.
    A token that no longer matches settles nothing.
    """
    if outcome not in ARCHIVE_STATES:
        raise MissionError(f"unknown archive outcome {outcome!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            won = con.execute(
                "UPDATE mission_sessions SET archive_state=?, archive_error=?, lease_owner=NULL, "
                "lease_at=NULL, lease_token=NULL, "
                "removed_at=CASE WHEN ? THEN COALESCE(removed_at, ?) ELSE removed_at END, "
                "release_reason=CASE WHEN ? THEN COALESCE(release_reason,'archived') "
                "  ELSE release_reason END "
                "WHERE mission_id=? AND session_key=? "
                "AND archive_state IN ('pending','in_progress') "
                "AND (? IS NULL OR lease_token IS NULL OR lease_token = ?)",
                (
                    outcome,
                    _cap_or_none(reason, 300),
                    1 if outcome != "failed" else 0,
                    ts,
                    1 if outcome != "failed" else 0,
                    mission_id,
                    session_key,
                    token,
                    token,
                ),
            ).rowcount
            # **Only the winner publishes.** The guarded UPDATE already refuses a stale worker's
            # settlement, but appending the event regardless let a worker whose CAS LOST write
            # `outcome: done` into an append-only timeline — a completion claimed by somebody who
            # completed nothing. The event and the release belong to whoever actually won.
            if not won:
                con.execute("COMMIT")
                return
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "session", "outcome": outcome, "reason": _cap(reason, 300)},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    if token and won:
        release_session(session_key, token, path=path)


def finish_archive(
    mission_id: str,
    *,
    op_token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Step 3 — stamp ``archived_at``, release anything still held, and name what did not archive.

    **Refuses unless it owns the operation and no lease is still open.** Both guards exist because
    the same reproduction breaks without either: a second caller that did not start this archive
    used to walk in, find no ``pending`` rows (the first worker had leased them all), mark the
    mission archived and then unarchive it — while the first worker was still inside
    ``cleanup_runtime`` killing a session, which it then settled into a now-live mission.

    So finalisation is conditional on *this* being the attempt that began, and on every session
    having actually settled. An open lease is not a failure to report — it is a reason not to
    finish yet.

    **Honest about partial failure**, once it does finish: the mission archives and the record
    names the session that survived, rather than reporting a clean sweep.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archiving_at, archived_at, op_token FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archiving_at"] is None and row["archived_at"] is not None:
                # Already finished; idempotent for a retry. Report the REAL outcome rather than
                # an empty one — a second call must not turn a partial archive into a clean sweep.
                done = _archive_outcome(con, mission_id)
                con.execute("COMMIT")
                return {"mission_id": mission_id, "archived": True, **done}
            if op_token is not None and row["op_token"] != op_token:
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: this is not the archive attempt in flight", status=409
                )
            open_leases = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state IN ('pending','in_progress')",
                    (mission_id,),
                ).fetchall()
            ]
            if open_leases:
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: {len(open_leases)} session teardown(s) still open",
                    status=409,
                )
            outcome = _archive_outcome(con, mission_id)
            failures, skipped = outcome["not_archived"], outcome["skipped"]
            con.execute(
                "UPDATE mission_sessions SET removed_at=?, release_reason='archived' "
                "WHERE mission_id=? AND removed_at IS NULL",
                (ts, mission_id),
            )
            con.execute(
                "UPDATE missions SET archived_at=?, archiving_at=NULL, op_token=NULL, "
                "updated_at=? WHERE id=?",
                (ts, ts, mission_id),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={
                    "phase": "finish",
                    "not_archived": [f["session_key"] for f in failures],
                    "skipped": skipped,
                },
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "archived": True,
        "not_archived": failures,
        "skipped": skipped,
    }


def begin_unarchive(
    mission_id: str,
    *,
    sessions: bool = True,
    resume: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Claim the unarchive **before** any external effect. One transaction.

    Unarchive used to restore every provider session first and clear ``archived_at`` afterwards.
    A crash between those two left live, unarchived sessions under a mission still recorded as
    archived — and boot recovery only looked at ``archiving_at``, so the torn state was invisible
    and permanent. Worse, a repeated or concurrent request mutated the providers and only *then*
    discovered it had lost, so the losing caller still moved real files.

    Two things the first version got wrong, both fixed here:

    * **The claim is exclusive.** A second *request* is refused (409) rather than handed
      ``resumed=True`` and allowed to walk the same provider restores in parallel — which is how
      one caller reported success while the other reported a provider failure for the same
      session. Only :func:`resume=True`, which boot recovery passes, may take over a claim —
      safe for the reason `begin_archive` gives: the mission-level claim is not the gate on the
      destructive effect, the per-session lease is, and that one is taken over only on a proven
      expiry.
    * **The claim carries the mode.** ``sessions`` is stored with it, so recovery finishes *this*
      operation rather than a differently-shaped one. Without it, a crash from ``sessions=False``
      came back with the sessions unarchived anyway.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archived_at, archiving_at, unarchiving_at, unarchive_sessions "
                "FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archiving_at"] is not None and row["archived_at"] is None:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id}: archive in progress", status=409)
            if row["unarchiving_at"] is not None:
                if not resume:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id}: unarchive already in progress", status=409
                    )
                token = _new_op_token()
                con.execute("UPDATE missions SET op_token=? WHERE id=?", (token, mission_id))
                con.execute("COMMIT")
                return {
                    "mission_id": mission_id,
                    "resumed": True,
                    # The mode the CLAIM was made with, not the one this caller asked for.
                    "sessions": bool(row["unarchive_sessions"]),
                    "op_token": token,
                    "retry_only": row["archived_at"] is None,
                }
            outstanding = con.execute(
                "SELECT COUNT(*) FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='restore_failed'",
                (mission_id,),
            ).fetchone()[0]
            if row["archived_at"] is None and not outstanding:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is not archived", status=409)
            # A mission that is already back but still owes restores may claim again, to retry
            # exactly those. Without this the partial-failure contract stranded them: the
            # mission-level gate is what lets a restore be attempted, and clearing it on the way
            # out meant "retry" answered "not archived" forever. Fencing the mission until every
            # session came back is the other trap — one un-restorable session would park the
            # record for the life of the process.
            retry_only = row["archived_at"] is None
            token = _new_op_token()
            cur = con.execute(
                "UPDATE missions SET unarchiving_at=?, unarchive_sessions=?, op_token=? "
                "WHERE id=? AND unarchiving_at IS NULL",
                (ts, 1 if sessions else 0, token, mission_id),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id}: unarchive already claimed", status=409)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "resumed": False,
        "sessions": sessions,
        "op_token": token,
        "retry_only": retry_only,
    }


def claim_session_restore(
    mission_id: str, session_key: str, *, now: float | None = None, path: Path | None = None
) -> tuple[str, str | None]:
    """Lease one session's provider restore, mirroring :func:`claim_session_teardown`.

    Restores are external effects too, and the reproduction that motivated this had two callers
    running ``prov.unarchive`` on the same session — one reporting success, the other a provider
    failure for work that had already happened.
    """
    ts = time.time() if now is None else now
    try:
        token = reserve_session(session_key, f"mission:{mission_id}", now=now, path=path)
    except SessionBusy:
        return "taken", None
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archive_state FROM mission_sessions WHERE mission_id=? AND session_key=?",
                (mission_id, session_key),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "gone", None
            # `restore_failed` is eligible on purpose: that is what a retry pass picks up, and
            # it is the reason the row stays reserved rather than being released as "done with".
            if row["archive_state"] not in ("done", "already_archived", "restore_failed"):
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "taken", None
            # Ownership is re-checked HERE too, not just on the teardown side: restoring a
            # session another mission has since taken would move the provider files underneath
            # a live holder.
            holder = con.execute(
                "SELECT mission_id FROM mission_sessions "
                "WHERE session_key=? AND removed_at IS NULL AND mission_id != ? LIMIT 1",
                (session_key, mission_id),
            ).fetchone()
            if holder is not None:
                con.execute(
                    "UPDATE mission_sessions SET archive_state='skipped', archive_error=? "
                    "WHERE mission_id=? AND session_key=?",
                    (f"held by mission {holder['mission_id']}", mission_id, session_key),
                )
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "skipped", None
            con.execute(
                "UPDATE mission_sessions SET archive_state='restoring', lease_owner=?, "
                "lease_at=?, lease_token=? WHERE mission_id=? AND session_key=?",
                (PROCESS_EPOCH, ts, token, mission_id, session_key),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "restore", "outcome": "claimed"},
            )
            con.execute("COMMIT")
            return "claimed", token
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settle_session_restore(
    mission_id: str,
    session_key: str,
    outcome: str,
    *,
    reason: str = "",
    token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """Settle one restore. Requires the claim's fencing token, for the reason in
    :func:`settle_session_archive`: a worker that finishes late must not settle the operation
    that replaced it."""
    if outcome not in ("restored", "restore_failed"):
        raise MissionError(f"unknown restore outcome {outcome!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            won = con.execute(
                "UPDATE mission_sessions SET archive_state=?, archive_error=?, lease_owner=NULL, "
                "lease_at=NULL, lease_token=NULL "
                "WHERE mission_id=? AND session_key=? AND archive_state='restoring' "
                "AND (? IS NULL OR lease_token IS NULL OR lease_token = ?)",
                (
                    None if outcome == "restored" else "restore_failed",
                    None if outcome == "restored" else _cap_or_none(reason, 300),
                    mission_id,
                    session_key,
                    token,
                    token,
                ),
            ).rowcount
            if not won:  # a fenced-out worker publishes nothing — see `settle_session_archive`
                con.execute("COMMIT")
                return
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "restore", "outcome": outcome, "reason": _cap(reason, 300)},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    if token and won:
        release_session(session_key, token, path=path)


def finish_unarchive(
    mission_id: str, *, now: float | None = None, path: Path | None = None
) -> dict:
    """Clear the archive stamps and report what did not come back.

    **The explicit partial-failure contract.** A session that will not restore does not hold the
    mission hostage: the mission un-archives and the roster names the session, exactly as archive
    is honest about a session it could not reap. Keeping the whole mission fenced on one
    un-restorable session is the *other* failure this phase already fixed once — a transient
    error must never park a record for the life of the process. Those rows keep
    ``archive_state='restore_failed'``, so a later unarchive retries precisely them.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            open_leases = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state='restoring'",
                    (mission_id,),
                ).fetchall()
            ]
            if open_leases:
                # An open restore lease is not a failure to record — it is a reason not to finish.
                # Converting it to `restore_failed` and finishing anyway is how recovery came to
                # report a mission unarchived after invoking no provider restore at all.
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: {len(open_leases)} session restore(s) still open",
                    status=409,
                )
            not_restored = [
                dict(r)
                for r in con.execute(
                    "SELECT session_key, archive_error FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state='restore_failed'",
                    (mission_id,),
                ).fetchall()
            ]
            # `archived_at` is cleared only on the pass that was actually un-archiving. A
            # retry-only pass (the mission is already back, and is finishing outstanding restores)
            # must leave the rest of the record alone.
            con.execute(
                "UPDATE missions SET archived_at=NULL, archiving_at=NULL, unarchiving_at=NULL, "
                "unarchive_sessions=NULL, op_token=NULL, updated_at=? WHERE id=?",
                (ts, mission_id),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={
                    "phase": "unarchive",
                    "not_restored": [r["session_key"] for r in not_restored],
                },
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"mission_id": mission_id, "archived": False, "not_restored": not_restored}


def pending_unarchives(*, path: Path | None = None) -> list[str]:
    """Unarchives claimed and never finished — the other half of the boot worklist."""
    con = _ready(path)
    try:
        return [
            r["id"]
            for r in con.execute(
                "SELECT id FROM missions WHERE unarchiving_at IS NOT NULL ORDER BY unarchiving_at"
            ).fetchall()
        ]
    finally:
        con.close()


def pending_archives(*, path: Path | None = None) -> list[str]:
    """Missions whose archive began and never finished — the boot reconciliation worklist."""
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT id FROM missions WHERE archiving_at IS NOT NULL AND archived_at IS NULL "
            "ORDER BY archiving_at ASC"
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        con.close()


def archive_sessions_for(mission_id: str, *, path: Path | None = None) -> list[dict]:
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key, role, archive_state, archive_error, lease_owner "
            "FROM mission_sessions WHERE mission_id=? ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------- retention


#: The predicate that decides what retention may delete. Repeated verbatim at BOTH the selection
#: and the mutation, because a mission can enter an operation between the two — and deleting one
#: mid-archive destroys the journal that recovery needs, along with the roster naming the agents
#: it was about to reap.
class _NoVictims(Exception):
    """Nothing old enough to prune — but an outstanding scrub may still be owed."""


_RETENTION_ELIGIBLE = (
    "closed_at IS NOT NULL AND closed_at < ? " "AND archiving_at IS NULL AND unarchiving_at IS NULL"
)


def retention_pass(
    *, now: float | None = None, days: int | None = None, path: Path | None = None
) -> int:
    """Delete closed missions older than the retention window. Bounded; returns rows deleted.

    A **row delete**, not a tombstone: the rows carry the operator's verbatim instruction, so
    "deleted" has to mean the bytes are gone. Bounded per pass so a long-idle install cannot turn
    one request into a table scan.

    **Selection and deletion happen in one transaction**, with the eligibility predicate repeated
    on the DELETE. Selecting outside it and deleting by captured id was a check-then-act: a
    mission could be reopened, or an archive could begin on it, between the two — and the delete
    would still take it, destroying the operation journal that boot recovery needs.

    Raises :class:`ScrubFailed` when the rows are gone but the log could not be truncated: this
    deletes sensitive text, so it reports what it actually achieved.
    """
    window = _retention_days() if days is None else max(1, int(days))
    cutoff = (time.time() if now is None else now) - window * 86400
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            victims = [
                r["id"]
                for r in con.execute(
                    f"SELECT id FROM missions WHERE {_RETENTION_ELIGIBLE} "  # noqa: S608
                    "ORDER BY closed_at ASC LIMIT ?",
                    (cutoff, RETENTION_BATCH),
                ).fetchall()
            ]
            if not victims:
                con.execute("COMMIT")
                deleted = 0
                referenced = []
                raise _NoVictims
            referenced = [
                r["action_id"]
                for r in con.execute(
                    "SELECT DISTINCT action_id FROM mission_events "  # noqa: S608
                    f"WHERE action_id IS NOT NULL AND mission_id IN ({_marks(victims)})",
                    tuple(victims),
                ).fetchall()
            ]
            deleted = (
                con.execute(
                    f"DELETE FROM missions WHERE id IN ({_marks(victims)}) "  # noqa: S608
                    f"AND {_RETENTION_ELIGIBLE}",
                    (*victims, cutoff),
                ).rowcount
                or 0
            )
            _gc_settlements(con, referenced)
            con.execute("COMMIT")
        except _NoVictims:
            pass
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        try:
            # Scrub when this pass deleted something OR when an earlier one left the obligation.
            owed = deleted or _flag_get(con, SCRUB_PENDING) is not None
            if owed and not _scrub(con):
                raise ScrubFailed(f"{deleted} retired mission(s)" if deleted else "an earlier one")
        finally:
            con.close()
    return deleted


def _marks(items) -> str:
    """A `?` run sized by a list — every value is still bound."""
    return ",".join("?" * len(items))


# ---------------------------------------------------------------- fail-soft read wrappers


def safe_list_missions(**kw) -> dict:
    """:func:`list_missions`, degraded rather than fatal.

    A locked or corrupt store empties the rail and **says why**, exactly as opencode's reader
    degrades the sidebar — it never takes the console down. Writes deliberately do NOT get this
    treatment: a silently dropped adopt or archive is indistinguishable from one that worked.
    """
    try:
        out = list_missions(**kw)
        out["store_error"] = None
        return out
    except (sqlite3.Error, MissionError, OSError) as e:
        return {
            "missions": [],
            "total": 0,
            "limit": kw.get("limit") or LIST_LIMIT_DEFAULT,
            "offset": kw.get("offset") or 0,
            "facets": {"projects": [], "states": []},
            "store_error": _store_reason(e),
        }


def safe_get_mission(mission_id: str, **kw) -> dict | None:
    try:
        return get_mission(mission_id, **kw)
    except (sqlite3.Error, OSError):
        return None


def _store_reason(exc: BaseException) -> str:
    """A stated reason that never carries mission content.

    ``sqlite3`` messages are about the *file* ("database is locked", "database disk image is
    malformed"), never about a row, so they are safe to surface. Anything else is reported by
    type only.
    """
    if isinstance(exc, sqlite3.Error):
        return f"missions store unavailable: {exc}"
    if isinstance(exc, MissionError):
        return str(exc)
    return f"missions store unavailable: {type(exc).__name__}"


__all__ = [
    "MissionError",
    "MissionNotFound",
    "MissionsBusy",
    "ScrubFailed",
    "SessionHeld",
    "STATES",
    "TERMINAL_STATES",
    "CWD_OPTIONAL_STATES",
    "EVENT_KINDS",
    "PROBE_KINDS",
    "LEASE_MAX_AGE_S",
    "PROCESS_EPOCH",
    "RESERVED_ARCHIVE_STATES",
    "OBJECTIVE_STATES",
    "SCHEMA_VERSION",
    "SETTLEMENT_LOST",
    "acquire",
    "active_session_keys",
    "adopt",
    "append_event",
    "all_active_memberships",
    "OwnershipUnknown",
    "RESERVATION_MAX_AGE_S",
    "SessionBusy",
    "release_session",
    "reservation_of",
    "reserve_session",
    "archive_sessions_for",
    "begin_archive",
    "begin_unarchive",
    "create_mission",
    "delete_mission",
    "derive_needs_you",
    "detach",
    "event_count",
    "executor",
    "claim_session_restore",
    "claim_session_teardown",
    "finish_archive",
    "finish_unarchive",
    "get_mission",
    "holder_of",
    "inflight_for_test",
    "list_missions",
    "new_id",
    "objectives",
    "patch_objectives",
    "pending_archives",
    "pending_unarchives",
    "record_settlement",
    "record_settlements",
    "referenced_action_ids",
    "settlements_for",
    "reopen_stale_leases",
    "reset_schema_cache_for_test",
    "retention_pass",
    "run_admitted",
    "safe_get_mission",
    "scrub_if_pending",
    "safe_list_missions",
    "set_state",
    "settle_session_archive",
    "settle_session_restore",
    "strict_bool",
    "shutdown_executor_for_test",
    "unmet_gates",
    "validate_id",
]
