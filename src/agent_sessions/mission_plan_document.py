"""The mission plan document (#1777, #1613 P4; the store #1578 specified).

A mission's plan is a list of **revisions**. Each revision is one JSON document:

* **objectives** — each names exactly one project and one procedure (#1613 §1 "Objective"): a flow
  from that project's deployed playbook, or a mission-local procedure. The procedure is SNAPSHOT
  here — as authored (`document`), with the declarations it may reference (`variables`), its
  normalized form and its digest — so a later playbook edit changes no mission (#1193 "snapshot
  on apply"), and every write RE-VALIDATES it with the bundle loader's own flow validator
  (`playbooks.validate.flow_snapshot`).
* **steps** — a projection of every objective's flow steps, namespaced by objective
  (`o1.implement`). The step CORE is the playbook flow step (title, role, `after`, independence,
  rework) and is DERIVED from the snapshot, never authored separately: `validate_document`
  recomputes it and refuses a document whose steps say anything else. Only the envelope —
  `reason`, and an `assignment` that stays `null` until P5 assigns a pool specialist — is the
  plan's own.

Nothing here touches a store, a session or the network. `missions` owns persistence (one
transaction with the objective rows); this module is the shape and the derived step state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from . import missions
from .playbooks import PlaybookFormatError, schema, validate

FORMAT = 1
#: Objectives one plan may hold. Each brings its evidence rows, and `missions.OBJECTIVES_MAX`
#: bounds those too; this bounds the document independently of how much evidence a flow has.
MAX_OBJECTIVES = 20
OBJECTIVE_ID_RE = re.compile(r"^o[1-9][0-9]?$")
#: The project entity id shape (`template_vars.PROJECT_ID_RE`'s alphabet, bounded).
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: Where an input's value came from: `deployment`, `project`, `global`, `default`, `missing`, …
INPUT_SOURCE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
#: Who wrote a revision. `playbook`: a deployed procedure applied; `operator`: a mission-local
#: procedure the operator added. #1578 adds `planner` / `lead`; P5 adds HQ.
AUTHOR_KINDS = frozenset({"playbook", "operator"})
#: `authored` only in this slice; #1578 adds `reconstructed` (legacy missions, never runnable).
KINDS = frozenset({"authored"})
SOURCES = frozenset({"playbook", "mission_local"})
REASON_MAX = 200
#: Bound on one procedure's authored document, as stored JSON: a bundle bounds a flow file at
#: `schema.MAX_TOML_BYTES` (64 KiB), and its JSON spelling gets twice that room.
DOCUMENT_MAX = 128 * 1024
#: The minted-key separator. `--` is refused in a playbook item key (`schema.
#: ITEM_KEY_FORBIDDEN_SUBSTRING`) precisely so the store can namespace keys it mints.
KEY_SEPARATOR = "--"

#: Derived step states (never stored). No `working`: that needs per-step sessions (P6).
STEP_STATES = ("waiting", "ready", "done", "failed", "note")


class PlanError(missions.MissionError):
    """A plan document the store refuses. Always a 422 with the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid plan: {reason}", status=422)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def flow_digest(flow: dict) -> str:
    """The digest of a normalized flow, which is what "the procedure this mission applied" means."""
    return "sha256:" + hashlib.sha256(_canonical(flow)).hexdigest()


#: Hex digits of the item key's digest a too-long evidence key ends in.
_KEY_HASH = 12


def evidence_key(oid: str, item_key: str) -> str:
    """`o<k>--<item key>`, bounded to `missions.OBJECTIVE_KEY_MAX` (Hermes on #1781).

    A flow may declare an item key of up to `schema.ITEM_KEY_MAX` (64) characters, the objective
    store's own cap, so the namespace prefix can push it over. Such a key is minted as
    `o<k>--<as much of the item key as fits>-<12 hex of its sha256>`: still the store's key
    grammar, still distinct per item key (the digest is over the WHOLE key), and never longer
    than a build that predates v34 can address after a rollback. The cap is not raised instead
    for exactly that reason. Collisions are refused anyway (`validate_document`)."""
    key = f"{oid}{KEY_SEPARATOR}{item_key}"
    if len(key) <= missions.OBJECTIVE_KEY_MAX:
        return key
    digest = hashlib.sha256(item_key.encode()).hexdigest()[:_KEY_HASH]
    room = missions.OBJECTIVE_KEY_MAX - len(oid) - len(KEY_SEPARATOR) - 1 - _KEY_HASH
    return f"{oid}{KEY_SEPARATOR}{item_key[:room]}-{digest}"


def step_id(oid: str, sid: str) -> str:
    return f"{oid}.{sid}"


def project_steps(oid: str, flow: dict, reason: str) -> list[dict]:
    """The plan steps of one objective, derived from its snapshot flow (the step core)."""
    out = []
    for s in flow["steps"]:
        rework = s.get("rework")
        out.append(
            {
                "id": step_id(oid, s["id"]),
                "objective": oid,
                "procedure_step": s["id"],
                "title": s["title"],
                "actor": s["actor"]["kind"],
                # The role a step BINDS: format 5 only. A format ≤4 step's projected legacy hint
                # is a read-only view and is never promoted into a plan (#1654).
                "role": copy.deepcopy(schema.binding_role(s)),
                "after": [step_id(oid, p) for p in s["after"]],
                "distinct_from": [
                    {"step": step_id(oid, d["step"]), "constraint": d["constraint"]}
                    for d in s["distinct_from"]
                ],
                "rework": (
                    None
                    if rework is None
                    else {
                        **rework,
                        "to": step_id(oid, rework["to"]),
                        "when": evidence_key(oid, rework["when"]),
                    }
                ),
                "evidence": [evidence_key(oid, item["key"]) for item in s["checklist"]],
                "envelope": {"reason": reason, "assignment": None},
            }
        )
    return out


def _one_line(value: object, what: str, *, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{what} is required")
    if len(value) > max_len or any(ch < " " or ch == "\x7f" for ch in value):
        raise PlanError(f"{what} must be one line of at most {max_len} characters")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise PlanError(f"{what} must be encodable as UTF-8") from None
    return value


#: Bound on a CHOSEN input value recorded in a plan (S1d): a branch picked by index.
INPUT_VALUE_MAX = 200


def _input_value_ok(value: object) -> bool:
    """A recorded input value: one bounded, printable, encodable line (a chosen branch)."""
    if not isinstance(value, str) or not value or len(value) > INPUT_VALUE_MAX:
        return False
    if any(ch < " " or ch == "\x7f" for ch in value):
        return False
    try:
        value.encode()
    except UnicodeEncodeError:
        return False
    return True


def _exact_keys(d: object, keys: set[str], what: str) -> dict:
    if not isinstance(d, dict):
        raise PlanError(f"{what} must be an object")
    if set(d) != keys:
        raise PlanError(f"{what} must have exactly {sorted(keys)}")
    return d


def _snapshot(p: dict, what: str) -> dict:
    """The procedure's flow, RE-VALIDATED from what the snapshot stores (Hermes on #1781).

    A digest only proves the caller hashed what it supplied. So the snapshot carries the flow as
    authored (`document`) and the declarations it may reference (`variables`), and the store
    re-runs the bundle loader's own validator over them (`validate.flow_snapshot`): the stored
    normalized `flow` must be exactly that result. A deployed procedure keeps its playbook's
    reviewed format; a mission-local one must be format 5."""
    if len(_canonical(p["document"])) > DOCUMENT_MAX:
        raise PlanError(f"{what}.document is larger than {DOCUMENT_MAX} bytes")
    try:
        flow = validate.flow_snapshot(
            p["document"],
            p["flow_id"],
            p["variables"],
            roles_only=p["source"] == "mission_local",
            manifest_format=p.get("manifest_format"),
        )
    except PlaybookFormatError as e:
        raise PlanError(f"{what} is not a valid flow ({e})") from None
    except RecursionError:
        raise PlanError(f"{what}.document is nested too deeply") from None
    if p["flow"] != flow:
        raise PlanError(f"{what}.flow is not its document's validated form")
    return flow


def validate_document(doc: object) -> dict:
    """The document, checked, or `PlanError`. Every authored revision passes this on write.

    The flow inside each objective was validated by the flow validator when it was snapshot; here
    it is held to its digest, and the steps are held to the flow.
    """
    # PLAIN, ENCODABLE JSON FIRST (the independent review on #1781): a lone surrogate parses
    # from JSON but cannot be encoded to UTF-8, and deep nesting overflows the recursion limit.
    # Either must be this function's refusal, never an exception escaping from a later encode.
    try:
        _canonical(doc)
    except (TypeError, ValueError, RecursionError):
        raise PlanError("the document must be plain JSON data, encodable as UTF-8") from None
    doc = _exact_keys(doc, {"format", "objectives", "steps"}, "the document")
    if type(doc["format"]) is not int or doc["format"] != FORMAT:
        raise PlanError(f"format must be {FORMAT}")
    objectives = doc["objectives"]
    if not isinstance(objectives, list) or not objectives:
        raise PlanError("a plan needs at least one objective")
    if len(objectives) > MAX_OBJECTIVES:
        raise PlanError(f"a plan holds at most {MAX_OBJECTIVES} objectives")
    expected_steps: list[dict] = []
    seen: set[str] = set()
    evidence: set[str] = set()
    for i, o in enumerate(objectives):
        what = f"objectives[{i}]"
        o = _exact_keys(o, {"id", "project_id", "procedure", "inputs"}, what)
        oid = o["id"]
        if not isinstance(oid, str) or not OBJECTIVE_ID_RE.fullmatch(oid):
            raise PlanError(f"{what}.id has an invalid shape")
        if oid in seen:
            raise PlanError(f"duplicate objective id {oid!r}")
        seen.add(oid)
        if not isinstance(o["project_id"], str) or not PROJECT_ID_RE.fullmatch(o["project_id"]):
            raise PlanError(f"{what}.project_id is required: every objective names a project")
        p = o["procedure"]
        if not isinstance(p, dict) or p.get("source") not in SOURCES:
            raise PlanError(f"{what}.procedure.source must be one of {sorted(SOURCES)}")
        keys = {"source", "flow_id", "title", "flow", "digest", "document", "variables"}
        if p["source"] == "playbook":
            keys |= {"playbook_id", "revision", "manifest_format"}
        p = _exact_keys(p, keys, f"{what}.procedure")
        if not isinstance(p["flow_id"], str) or not schema.FILE_ID_RE.fullmatch(p["flow_id"]):
            raise PlanError(f"{what}.procedure.flow_id has an invalid shape")
        _one_line(p["title"], f"{what}.procedure.title", max_len=schema.NAME_MAX)
        if p["source"] == "playbook":
            for k in ("playbook_id", "revision"):
                if not isinstance(p[k], str) or not p[k]:
                    raise PlanError(f"{what}.procedure.{k} is required")
        flow = _snapshot(p, f"{what}.procedure")
        if p["title"] != flow["title"]:
            raise PlanError(f"{what}.procedure.title is not its flow's title")
        if p["digest"] != flow_digest(flow):
            raise PlanError(f"{what}.procedure.digest does not match its flow")
        inputs = o["inputs"]
        if (
            not isinstance(inputs, list)
            or len(inputs) > schema.MAX_VARIABLES
            or any(
                not isinstance(row, dict)
                or set(row)
                not in (
                    {"name", "source"},
                    {"name", "source", "value"},
                    {"name", "source", "value", "number"},
                )
                or ("value" in row and not _input_value_ok(row["value"]))
                or (
                    "number" in row
                    and (type(row["number"]) is not int or not 0 < row["number"] < 10_000_000_000)
                )
                or not isinstance(row["name"], str)
                or not schema.VARIABLE_NAME_RE.fullmatch(row["name"])
                or not isinstance(row["source"], str)
                or not INPUT_SOURCE_RE.fullmatch(row["source"])
                for row in inputs
            )
            or len({row["name"] for row in inputs}) != len(inputs)
        ):
            raise PlanError(f"{what}.inputs must list distinct {{name, source}} rows")
        projected = project_steps(oid, flow, "")
        for s in projected:
            for k in s["evidence"]:
                if k in evidence:
                    raise PlanError(f"duplicate evidence key {k!r}")
                evidence.add(k)
        expected_steps.extend(projected)
    # Every evidence item becomes an objective row, so the plan may not promise more rows than a
    # mission can hold (Hermes on #1781): refused here rather than half-way through a write.
    if len(evidence) > missions.OBJECTIVES_MAX:
        raise PlanError(
            f"the plan has {len(evidence)} evidence items; a mission holds at most "
            f"{missions.OBJECTIVES_MAX} objectives"
        )
    steps = doc["steps"]
    if not isinstance(steps, list) or len(steps) != len(expected_steps):
        raise PlanError("the steps must be exactly the objectives' procedure steps")
    for i, (got, want) in enumerate(zip(steps, expected_steps, strict=True)):
        what = f"steps[{i}]"
        if not isinstance(got, dict):
            raise PlanError(f"{what} must be an object")
        env = _exact_keys(got.get("envelope"), {"reason", "assignment"}, f"{what}.envelope")
        _one_line(env["reason"], f"{what}.envelope.reason", max_len=REASON_MAX)
        if env["assignment"] is not None:
            raise PlanError(f"{what}.envelope.assignment is assigned in P5, never before")
        core = {k: v for k, v in got.items() if k != "envelope"}
        if core != {k: v for k, v in want.items() if k != "envelope"}:
            raise PlanError(f"{what} does not match its procedure step {want['id']!r}")
    return doc


def build(objectives: list[dict], reasons: dict[str, str]) -> dict:
    """A validated document from `objectives`; each objective's steps carry `reasons[oid]`."""
    steps: list[dict] = []
    for o in objectives:
        steps.extend(project_steps(o["id"], o["procedure"]["flow"], reasons[o["id"]]))
    return validate_document({"format": FORMAT, "objectives": objectives, "steps": steps})


def step_states(doc: dict, rows: list[dict]) -> dict[str, str]:
    """Each step's DERIVED state from its evidence rows (#1193: never a second stored truth).

    * `note` — a step with no evidence (a note gates nothing; nothing may wait on it);
    * `failed` — a gating evidence row is `failed`;
    * `done` — its gating rows are satisfied by THE STORE'S OWN PREDICATE (`missions.gate_tally`:
      a `met` row counts only while its latest observation still supports it, a waiver always
      counts), so the plan and the completion gate can never give two answers (#897). A step
      whose remaining evidence gates nothing is done once each row is met or waived;
    * `ready` — every step in its `after` is done, and it is not;
    * `waiting` — a prerequisite is not done.

    A missing evidence row (dropped by the operator) counts as waived: dropping is the operator's
    decision not to require it, which is what waiving is.
    """
    by_key = {r.get("key"): r for r in rows}
    own: dict[str, str] = {}
    for s in doc["steps"]:
        if not s["evidence"]:
            own[s["id"]] = "note"
            continue
        ev = [by_key[k] for k in s["evidence"] if k in by_key]
        gates = [r for r in ev if r.get("gate")]
        if any(r.get("state") == "failed" for r in (gates or ev)):
            own[s["id"]] = "failed"
        elif gates:
            own[s["id"]] = "done" if missions.gate_tally(gates)[1] == 0 else "open"
        else:
            settled = all(r.get("state") in ("met", "waived") for r in ev)
            own[s["id"]] = "done" if settled else "open"
    out: dict[str, str] = {}
    for s in doc["steps"]:
        state = own[s["id"]]
        if state == "open":
            prereqs_done = all(own.get(p) in ("done", "note") for p in s["after"])
            state = "ready" if prereqs_done else "waiting"
        out[s["id"]] = state
    return out
