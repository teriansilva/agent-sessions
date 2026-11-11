"""Mission plans in the Library (#1924, #1613 P7a): reusable, slot-only, inert.

A **mission plan** is #1613 §1's reusable plan: what a mission achieves and in what order — its
objectives, their order (`after`), the project **slots** they need and the procedure each follows
(whose steps name their role needs: skills and a minimum grade). It never names a real project, a
specialist or a value, and NOTHING here plans, dispatches or launches anything: a plan is planning
data until P5 plans a mission from it under plan approval.

Not to be confused with the **dispatch proposal** (`mission_plan.py`, the `mission_plans` table),
which is one mission's proposed session. This module is the Library item.

**The document (format 1, exact keys).** ``{format, title, summary, slots, objectives}``:

* ``slots`` — ``[{id: "p1", label}, …]``, numbered in order, every one used by an objective;
* ``objectives`` — ``[{id, slot, procedure, inputs, after?}]``. ``procedure`` is the plan
  document's procedure snapshot, held to the SAME check (`mission_plan_document.
  check_procedure`: the bundle loader's flow validator, re-run). ``inputs`` are variable NAMES
  only — the values a mission picked (a branch, a pull-request number) are never saved.
  ``after`` follows #1797's rule (earlier objectives only).

**The store** (``mission-plans.db``, ``AGENT_SESSIONS_MISSION_PLANS_DB``) has `specialists_store`'s
shape: immutable versions (P7b pins ``{id, version}``), archive instead of delete (workflow §0),
every write one ``IMMEDIATE`` transaction, a file from a newer build refused untouched.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import mission_plan_document as plan_doc
from . import missions
from .playbooks import schema

log = logging.getLogger(__name__)

FORMAT = 1
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000
PLANS_MAX = 200
TITLE_MAX = 120
SUMMARY_MAX = 2000
LABEL_MAX = 60
MAX_SLOTS = plan_doc.MAX_OBJECTIVES
#: A saved document as canonical JSON. Twenty objectives at the plan document's own 128 KiB bound
#: per procedure would be more; a plan that large is refused rather than stored.
DOCUMENT_MAX = 1024 * 1024
ID_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,47}\Z")
SLOT_ID_RE = re.compile(r"\Ap[1-9][0-9]?\Z")
ORIGIN_KINDS = frozenset({"mission", "file"})

_SCHEMA = """
CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE plan_versions (
    id TEXT NOT NULL REFERENCES plans(id),
    version INTEGER NOT NULL,
    document TEXT NOT NULL,
    digest TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TRIGGER plan_versions_immutable BEFORE UPDATE ON plan_versions
BEGIN SELECT RAISE(ABORT, 'a mission plan version is immutable'); END;
CREATE TRIGGER plan_versions_kept BEFORE DELETE ON plan_versions
BEGIN SELECT RAISE(ABORT, 'a mission plan version is never deleted'); END
"""


class LibraryError(missions.MissionError):
    """A refusal with its HTTP status (`missions.MissionError`, so the routes' `_fail` maps it)."""


class NotFound(LibraryError):
    def __init__(self, what: str = "mission plan") -> None:
        super().__init__(f"unknown {what}", status=404)


class Conflict(LibraryError):
    def __init__(self, message: str = "the mission plan changed since you loaded it") -> None:
        super().__init__(message, status=409)


class StoreUnsupported(LibraryError):
    def __init__(self, version: object) -> None:
        super().__init__(
            f"mission-plans.db is store version {version!r}, newer than this build "
            f"(version {SCHEMA_VERSION}); upgrade BattleLab, nothing was written",
            status=503,
        )


# ---- the document -------------------------------------------------------------------------------


def _err(reason: str) -> plan_doc.PlanError:
    return plan_doc.PlanError(reason)


def digest(doc: dict) -> str:
    return hashlib.sha256(plan_doc.canonical(doc)).hexdigest()


def validate(doc: object) -> dict:
    """The saved-plan document, checked to the letter, or `PlanError` (422).

    Every write passes this — a save from a mission and an import from a file alike — so the
    store never holds a plan the importer would refuse, and the reverse."""
    try:
        size = len(plan_doc.canonical(doc))
    except (TypeError, ValueError, RecursionError):
        raise _err("the mission plan must be plain JSON data, encodable as UTF-8") from None
    if size > DOCUMENT_MAX:
        raise _err(f"the mission plan is larger than {DOCUMENT_MAX} bytes")
    doc = plan_doc.exact_keys(
        doc, {"format", "title", "summary", "slots", "objectives"}, "the mission plan"
    )
    if type(doc["format"]) is not int or doc["format"] != FORMAT:
        raise _err(f"format must be {FORMAT}")
    plan_doc.one_line(doc["title"], "title", max_len=TITLE_MAX)
    summary = doc["summary"]
    if (
        not isinstance(summary, str)
        or len(summary) > SUMMARY_MAX
        or any((ch < " " and ch not in "\n\t") or ch == "\x7f" for ch in summary)
    ):
        raise _err(f"summary must be text of at most {SUMMARY_MAX} characters")
    slots = doc["slots"]
    if not isinstance(slots, list) or not slots or len(slots) > MAX_SLOTS:
        raise _err(f"a mission plan needs 1 to {MAX_SLOTS} project slots")
    for i, slot in enumerate(slots):
        slot = plan_doc.exact_keys(slot, {"id", "label"}, f"slots[{i}]")
        if slot["id"] != f"p{i + 1}":
            raise _err(f"slots[{i}].id must be p{i + 1}: slots are numbered in order")
        plan_doc.one_line(slot["label"], f"slots[{i}].label", max_len=LABEL_MAX)
    slot_ids = {s["id"] for s in slots}
    objectives = doc["objectives"]
    if not isinstance(objectives, list) or not objectives:
        raise _err("a mission plan needs at least one objective")
    if len(objectives) > plan_doc.MAX_OBJECTIVES:
        raise _err(f"a mission plan holds at most {plan_doc.MAX_OBJECTIVES} objectives")
    seen: set[str] = set()
    used: set[str] = set()
    evidence: set[str] = set()
    for i, o in enumerate(objectives):
        what = f"objectives[{i}]"
        keys = {"id", "slot", "procedure", "inputs"}
        if isinstance(o, dict) and "after" in o:
            keys = keys | {"after"}
        o = plan_doc.exact_keys(o, keys, what)
        oid = o["id"]
        if not isinstance(oid, str) or not plan_doc.OBJECTIVE_ID_RE.fullmatch(oid):
            raise _err(f"{what}.id has an invalid shape")
        if oid in seen:
            raise _err(f"duplicate objective id {oid!r}")
        if "after" in o:
            plan_doc.check_after(o["after"], oid, seen, what)
        seen.add(oid)
        if not isinstance(o["slot"], str) or o["slot"] not in slot_ids:
            raise _err(f"{what}.slot names no project slot of this plan")
        used.add(o["slot"])
        flow = plan_doc.check_procedure(o["procedure"], f"{what}.procedure")
        inputs = o["inputs"]
        if (
            not isinstance(inputs, list)
            or len(inputs) > schema.MAX_VARIABLES
            or any(
                not isinstance(n, str) or not schema.VARIABLE_NAME_RE.fullmatch(n) for n in inputs
            )
            or len(set(inputs)) != len(inputs)
        ):
            raise _err(f"{what}.inputs must list distinct variable names")
        for step in plan_doc.project_steps(oid, flow, ""):
            for key in step["evidence"]:
                if key in evidence:
                    raise _err(f"duplicate evidence key {key!r}")
                evidence.add(key)
    if len(evidence) > missions.OBJECTIVES_MAX:
        raise _err(
            f"the plan has {len(evidence)} evidence items; a mission holds at most "
            f"{missions.OBJECTIVES_MAX} objectives"
        )
    unused = [s["id"] for s in slots if s["id"] not in used]
    if unused:
        raise _err(f"project slot {unused[0]} is used by no objective")
    return doc


def derive(doc: dict) -> dict:
    """What a reader shows beside the document, never stored: every step (its objective, title,
    actor and the role need it binds — skills and a minimum grade), and the counts."""
    steps = []
    for o in doc["objectives"]:
        flow = o["procedure"]["flow"]
        for s in plan_doc.project_steps(o["id"], flow, ""):
            steps.append(
                {
                    "id": s["id"],
                    "objective": o["id"],
                    "title": s["title"],
                    "actor": s["actor"],
                    "after": s["after"],
                    "role": s["role"],
                }
            )
    return {
        "steps": steps,
        "counts": {
            "objectives": len(doc["objectives"]),
            "slots": len(doc["slots"]),
            "steps": len(steps),
        },
    }


def _without_defaults(procedure: dict) -> dict:
    """The procedure snapshot with every declaration's `default` cleared (the independent review
    of #1924). In a deployed playbook a reviewed default IS the deployed value — the one a mission
    picked when no project binding overrode it — so keeping it would save an input value under
    another name. The declaration itself (name, type, label, help, pattern, choices) is the
    playbook's schema and stays: the flow is re-validated against exactly what is kept."""
    out = copy.deepcopy(procedure)
    out["variables"] = [{**v, "default": None} for v in out["variables"]]
    return out


def from_revision(document: dict, *, title: object, summary: object, slot_labels: object) -> dict:
    """A saved plan from one mission plan revision (#1924 save-as-plan).

    Each distinct project becomes a slot, numbered by its FIRST APPEARANCE in objective order,
    labelled by the operator (`slot_labels`, one per slot). What is kept: each objective's id, its
    procedure snapshot, its input NAMES and its `after`. What never is: the project ids, every
    input value and number, the step envelopes, and anything that is not in the plan document at
    all (the instruction, the brief, evidence, sessions)."""
    order: list[str] = []
    for o in document["objectives"]:
        if o["project_id"] not in order:
            order.append(o["project_id"])
    if not isinstance(slot_labels, list) or len(slot_labels) != len(order):
        raise _err(f"slot_labels must name each of the plan's {len(order)} project slots")
    slot_of = {pid: f"p{i + 1}" for i, pid in enumerate(order)}
    objectives = []
    for o in document["objectives"]:
        out = {
            "id": o["id"],
            "slot": slot_of[o["project_id"]],
            "procedure": _without_defaults(o["procedure"]),
            "inputs": [row["name"] for row in o["inputs"]],
        }
        if o.get("after"):
            out["after"] = list(o["after"])
        objectives.append(out)
    return validate(
        {
            "format": FORMAT,
            "title": title,
            "summary": "" if summary is None else summary,
            "slots": [{"id": slot_of[pid], "label": slot_labels[i]} for i, pid in enumerate(order)],
            "objectives": objectives,
        }
    )


# ---- the store ----------------------------------------------------------------------------------


def db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_MISSION_PLANS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "mission-plans.db"),
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
    with contextlib.suppress(FileExistsError):
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600))
    con = _raw_connect(p)
    try:
        v = _version(con)  # asked BEFORE anything writes: a newer file is refused untouched
        if v > SCHEMA_VERSION:
            raise StoreUnsupported(v)
        if v == SCHEMA_VERSION:
            return
        con.execute("BEGIN IMMEDIATE")
        try:
            v = _version(con)  # a peer may have created it meanwhile
            if v > SCHEMA_VERSION:
                raise StoreUnsupported(v)
            if v == 0:
                for stmt in re.split(r";\s*\n(?=CREATE)", _SCHEMA):
                    if stmt.strip():
                        con.execute(stmt)
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("PRAGMA journal_mode=WAL")
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
    if v > SCHEMA_VERSION:
        con.close()
        raise StoreUnsupported(v)
    return con


@contextlib.contextmanager
def _db_errors():
    try:
        yield
    except (sqlite3.Error, OSError) as e:
        raise LibraryError(f"the mission plan library is unavailable: {e}", status=503) from None


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


def _origin(origin: object) -> dict:
    if not isinstance(origin, dict) or origin.get("kind") not in ORIGIN_KINDS:
        raise LibraryError("a mission plan's origin is a mission or a file", status=500)
    return origin


def _row(con: sqlite3.Connection, pid: str) -> dict:
    r = con.execute("SELECT * FROM plans WHERE id = ?", (pid,)).fetchone()
    if r is None:
        raise NotFound()
    v = con.execute(
        "SELECT * FROM plan_versions WHERE id = ? AND version = ?", (pid, r["current_version"])
    ).fetchone()
    if v is None:
        raise LibraryError(f"mission plan {pid} is damaged (no current version)", status=503)
    try:
        doc = validate(json.loads(v["document"]))
        origin = json.loads(v["origin"])
        if not isinstance(origin, dict):
            raise ValueError("origin")
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, plan_doc.PlanError):
        raise LibraryError(f"mission plan {pid} is damaged; it can't be read", status=503) from None
    return {
        "id": pid,
        "version": int(r["current_version"]),
        "revision": int(r["revision"]),
        "archived": bool(r["archived"]),
        "created_at": float(r["created_at"]),
        "updated_at": float(r["updated_at"]),
        "digest": v["digest"],
        "origin": origin,
        "document": doc,
    }


def _new_id(con: sqlite3.Connection) -> str:
    """A random id, never one minted from the title (the independent review of #1924 S2): an id
    cut from a title that held a stored secret would carry the secret's START into every place an
    id is shown, out of reach of a scan that looks for whole secrets. A plan's title is text; its
    id names nothing."""
    while True:
        pid = f"plan-{secrets.token_hex(4)}"
        if not con.execute("SELECT 1 FROM plans WHERE id = ?", (pid,)).fetchone():
            return pid


def create(
    document: object, *, origin: dict, pid: str | None = None, now: float | None = None
) -> dict:
    """A new mission plan at version 1. ``pid`` asks for exactly that id (an import's planned
    destination, #1714): an id already taken is refused (409), never reused or renamed here."""
    doc = validate(document)
    origin = _origin(origin)
    ts = time.time() if now is None else now
    if pid is not None and not ID_RE.fullmatch(pid):
        raise LibraryError("not a mission plan id", status=422)
    with _tx() as con:
        (count,) = con.execute("SELECT COUNT(*) FROM plans").fetchone()
        if count >= PLANS_MAX:
            raise LibraryError(
                f"the Library holds at most {PLANS_MAX} mission plans, archived ones included",
                status=409,
            )
        if pid is not None:
            if con.execute("SELECT 1 FROM plans WHERE id = ?", (pid,)).fetchone():
                raise LibraryError(f"a mission plan {pid} already exists", status=409)
        else:
            pid = _new_id(con)
        con.execute(
            "INSERT INTO plans (id, current_version, revision, archived, created_at, updated_at) "
            "VALUES (?, 1, 1, 0, ?, ?)",
            (pid, ts, ts),
        )
        con.execute(
            "INSERT INTO plan_versions (id, version, document, digest, origin, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (pid, json.dumps(doc, sort_keys=True), digest(doc), json.dumps(origin), ts),
        )
        return _row(con, pid)


def set_archived(
    pid: str, archived: object, *, expect_revision: object, now: float | None = None
) -> dict:
    if not isinstance(archived, bool):
        raise LibraryError("archived must be true or false", status=422)
    if type(expect_revision) is not int:
        raise LibraryError("expect_revision is the revision you loaded", status=422)
    if not ID_RE.fullmatch(pid or ""):
        raise NotFound()
    ts = time.time() if now is None else now
    with _tx() as con:
        cur = _row(con, pid)
        if cur["revision"] != expect_revision:
            raise Conflict()
        if cur["archived"] == archived:
            return cur
        con.execute(
            "UPDATE plans SET archived = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
            (int(archived), ts, pid),
        )
        return _row(con, pid)


def get(pid: str) -> dict:
    if not ID_RE.fullmatch(pid or ""):
        raise NotFound()
    with _read() as con:
        return _row(con, pid)


def ids() -> dict[str, str]:
    """Every plan id (archived ones included: an id is taken either way) → its version-1 digest,
    the revision an import binds and reconciles by."""
    if not db_path().exists():
        return {}
    with _read() as con:
        return {
            r["id"]: r["digest"]
            for r in con.execute("SELECT id, digest FROM plan_versions WHERE version = 1")
        }


def at(pid: str) -> str | None:
    """The digest of the version 1 a plan at ``pid`` was created with, `None` when absent."""
    if not db_path().exists():
        return None
    with _read() as con:
        r = con.execute(
            "SELECT digest FROM plan_versions WHERE id = ? AND version = 1", (pid,)
        ).fetchone()
        return None if r is None else r["digest"]


def list_all(*, archived: bool = False) -> list[dict]:
    """Every readable plan (archived ones only when asked), newest first. One damaged row never
    hides the rest of the Library."""
    if not db_path().exists():
        return []
    with _read() as con:
        rows = con.execute("SELECT id FROM plans ORDER BY updated_at DESC, id").fetchall()
        out = []
        for r in rows:
            try:
                plan = _row(con, r["id"])
            except LibraryError as e:
                log.warning("mission plans: skipping %s: %s", r["id"], e)
                continue
            if plan["archived"] and not archived:
                continue
            out.append(plan)
        return out


def public(plan: dict, *, full: bool = False) -> dict:
    """A plan as a route answers it. The origin's kind only: a saving mission's id stays here."""
    doc = plan["document"]
    derived = derive(doc)
    out = {
        "id": plan["id"],
        "version": plan["version"],
        "revision": plan["revision"],
        "archived": plan["archived"],
        "title": doc["title"],
        "summary": doc["summary"],
        "origin_kind": plan["origin"].get("kind"),
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
        "counts": derived["counts"],
    }
    if full:
        out["document"] = doc
        out["steps"] = derived["steps"]
    return out


def save_from_mission(
    mission_id: str, revision: object, *, title: object, summary: object, slot_labels: object
) -> dict:
    """Save one plan revision of a mission as a new mission plan (#1924). The revision is named
    explicitly — revisions are immutable, so the operator saves exactly the one they looked at."""
    if type(revision) is not int or revision < 1:
        raise LibraryError("revision must be the plan revision you are saving", status=422)
    rev = missions.plan_revision(mission_id, revision)
    if rev is None:
        raise LibraryError("this mission has no such plan revision", status=404)
    doc = from_revision(rev["document"], title=title, summary=summary, slot_labels=slot_labels)
    return create(doc, origin={"kind": "mission", "mission_id": mission_id, "revision": revision})
