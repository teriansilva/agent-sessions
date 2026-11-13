"""A mission's team (#1950, #1613 P5): who fills each agent step, and why.

HQ plans every mission with the Commander (D2). For each agent step's ROLE (required skills by
exact id plus an optional minimum grade, `schema.binding_role`) the assembler picks one specialist
from the pool: the cheapest that qualifies. It records why, and who else qualified.

* **One predicate.** `qualify` is the only place that decides whether a specialist (or a model
  override) may fill a step. Every exclusion lives in it, each with a closed `code` and
  server-authored text. Model prose never appears in a reason. P6 calls the same function at
  approval, at launch and at every gate.
* **Pure over a `Context`.** `snapshot()` reads the pool, the grade map, the skills library, each
  client's facts (mission-ready, tool channel, its own model list) and known usage ONCE. Every
  other function here is pure over that snapshot: the same inputs give the same team.
* **The grade is derived, never stored.** `model_grades.grade_of` is asked on every call. Nothing
  here writes a grade into an assignment, so a remap in Settings re-grades a plan on its next read.
* **Cheapest (D-P5-1)** is the lexicographic order: lowest effective grade rank, then the most
  free capacity headroom (capacity minus the steps THIS plan already gave the specialist; live
  occupancy is never read), then the specialist id.
* **`distinct_from` is solved jointly**: plan order with the constraints propagated, then a
  bounded exhaustive search. Past `ASSEMBLY_NODE_BUDGET` the assembler refuses
  (`assembly_too_large`) and nothing is written. A step with no feasible specialist stays
  unassigned with its blocking codes: a constraint is never relaxed to fill a role.
* **Capacity never refuses a plan.** A specialist's peak concurrent instances (the width of the
  dependency order restricted to its steps) above its capacity is a `will_queue` note.

Nothing here touches a store, a session or the network except `snapshot()`, which only reads.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace

from . import model_grades

#: The closed refusal codes (#1950 §1). A reason's `code` is always one of these.
CODES: tuple[str, ...] = (
    "skill_missing",
    "skill_pin_stale",
    "engine_cannot_load_skills",
    "engine_not_mission_ready",
    "model_not_offered",
    "grade_below_min",
    "ungraded",
    "specialist_archived",
    "usage_exhausted",
    "not_distinct",
)
#: The codes an assignment's `notes` may carry: facts, never refusals.
NOTE_CODES: tuple[str, ...] = ("usage_unknown", "will_queue")
#: The safety net on the joint search (#1950 §2): nodes visited before it refuses.
ASSEMBLY_NODE_BUDGET = 20_000
#: Alternates recorded per pick (D6).
ALTERNATES_MAX = 3
#: Entries one step's override list may hold.
OVERRIDE_CHOICES_MAX = 200
#: The grade names the SPA shows (`web/src/components/specialists/grades.ts` `GRADE_LABEL`).
GRADE_LABEL = {"fast": "Intern", "strong": "Specialist", "frontier": "Senior"}
#: The `distinct_from` constraints a role step may carry (`schema.DISTINCT_CONSTRAINTS_ROLE`).
#: `session` (format ≤ 4) is always true across steps and is not a team constraint.
TEAM_CONSTRAINTS = ("specialist", "engine", "model")
DEFAULT_MODEL = model_grades.DEFAULT_MODEL
REASON_MAX = 200


class TeamRefused(Exception):
    """An operation the team rules refuse. `status` is the HTTP answer; `reasons` the codes."""

    def __init__(self, code: str, text: str, *, status: int = 422, reasons=None, extra=None):
        super().__init__(text)
        self.code = code
        self.text = text
        self.status = status
        self.reasons = list(reasons or [{"code": code, "text": text}])
        self.extra = dict(extra or {})


@dataclass(frozen=True)
class Client:
    """One agent's facts at snapshot time, as `qualify` needs them."""

    engine: str
    label: str
    #: The manifest has a `tools.mcp` channel (skills ride it).
    can_load_skills: bool
    #: `mission_structured.admits`: a mission-ready API client that can start here.
    mission_ready: bool
    not_ready_reason: str | None = None
    #: The client's own model list: `{model_id: (effort, …)}` in its order; None = unavailable.
    models: dict[str, tuple[str, ...]] | None = None
    #: Known remaining usage: `ok`, `unknown` or `exhausted` (`agent_usage`, never reconstructed).
    usage: str = "unknown"


@dataclass(frozen=True)
class Context:
    """Everything `qualify` and the assembler read, read once (`snapshot`)."""

    #: `model_grades.get()`.
    grades: dict
    #: engine id → `Client`.
    clients: dict
    #: `{skill_id: current_revision}`, or None when the library is unreadable (flags nothing).
    library: dict | None
    #: Specialist records (`specialists_store.list_all()` shape), archived ones included.
    pool: list = field(default_factory=list)
    #: `specialist_capacity.max_instances()`.
    pool_max: int = 6
    #: `(id, version)` → a pinned version that is no longer current (`with_pinned`).
    pinned: dict = field(default_factory=dict)


# ---- qualification -------------------------------------------------------------------------------


def label(grade: str | None) -> str:
    return GRADE_LABEL.get(grade or "", "Ungraded")


def grade_of(ctx: Context, engine: str, model: str | None) -> str | None:
    return model_grades.grade_of(engine, model, ctx.grades)


def own_effective(version: dict) -> dict:
    """A specialist version's preselected `{engine, model, effort}`."""
    return {
        "engine": version.get("engine"),
        "model": version.get("model"),
        "effort": version.get("effort"),
    }


def _role_name(step: dict) -> str:
    role = step.get("role") or {}
    skills = role.get("skills") or []
    return ", ".join(skills) if skills else "this step"


def _r(code: str, text: str) -> dict:
    return {"code": code, "text": text[:REASON_MAX]}


def qualify(step: dict, version: dict, effective: dict | None, ctx: Context) -> dict:
    """May `version` fill `step` on `effective` (the override, else its own choice)?

    `{ok, reasons[]}`; every reason is `{code, text}` with a code from `CODES`. Every check runs
    (no short-circuit), so an unassigned step can say everything that blocked each candidate.
    `version` is `{id, version, archived, title, skills, engine, model, effort, capacity}`.
    `not_distinct` is a set-level check: `distinct_reasons` adds it."""
    eff = effective if effective is not None else own_effective(version)
    engine, model, effort = eff.get("engine") or "", eff.get("model"), eff.get("effort")
    role = step.get("role") or {}
    reasons: list[dict] = []
    name = version.get("id", "this specialist")
    if version.get("archived"):
        reasons.append(_r("specialist_archived", f"{name} is archived: it can't be assigned"))
    pinned = [s.get("id") for s in version.get("skills") or [] if isinstance(s, dict)]
    lacking = [s for s in role.get("skills") or [] if s not in pinned]
    if lacking:
        reasons.append(_r("skill_missing", f"{name} has no {', '.join(lacking)} skill"))
    if ctx.library is not None:
        stale = [
            s.get("id")
            for s in version.get("skills") or []
            if isinstance(s, dict) and ctx.library.get(s.get("id")) != s.get("revision")
        ]
        if stale:
            reasons.append(
                _r(
                    "skill_pin_stale",
                    f"{name}'s skill {', '.join(stale)} is missing or changed in the library: "
                    "save the specialist again",
                )
            )
    client = ctx.clients.get(engine)
    if client is None:
        reasons.append(
            _r("engine_not_mission_ready", f"{engine or 'the agent'} is unknown or removed")
        )
    else:
        if not client.mission_ready:
            why = client.not_ready_reason or "not mission-ready"
            reasons.append(
                _r("engine_not_mission_ready", f"{client.label} cannot hold a mission role: {why}")
            )
        if pinned and not client.can_load_skills:
            reasons.append(
                _r(
                    "engine_cannot_load_skills",
                    f"{client.label} cannot load skills: it has no tool channel",
                )
            )
        if not client.mission_ready:
            pass  # the client itself is refused; its model list is never asked
        elif model and model != DEFAULT_MODEL:
            if client.models is None:
                reasons.append(
                    _r("model_not_offered", f"{client.label}'s model list is unavailable")
                )
            elif model not in client.models:
                reasons.append(_r("model_not_offered", f"{client.label} does not offer {model}"))
            elif effort is not None and effort not in client.models[model]:
                reasons.append(
                    _r("model_not_offered", f"{client.label}: {model} offers no effort {effort}")
                )
        elif effort is not None:
            reasons.append(_r("model_not_offered", f"{client.label}: an effort needs a model"))
        if client.usage == "exhausted":
            reasons.append(
                _r("usage_exhausted", f"{client.label} has used up its known remaining usage")
            )
    need = role.get("min_grade")
    if need:
        got = grade_of(ctx, engine, model)
        what = model if model and model != DEFAULT_MODEL else "the agent's default model"
        if got is None:
            reasons.append(
                _r(
                    "ungraded",
                    f"{what} is ungraded on {engine}; an ungraded model meets no minimum grade "
                    "(grade it in Settings → Agents)",
                )
            )
        elif model_grades.rank(got) < model_grades.rank(need):
            reasons.append(
                _r(
                    "grade_below_min",
                    f"{what} is {label(got)}, below {label(need)}, which {_role_name(step)} needs",
                )
            )
    return {"ok": not reasons, "reasons": reasons}


# ---- the plan's shape ----------------------------------------------------------------------------


def assignable(step: dict) -> bool:
    """Only an AGENT step with a binding role gets a specialist. Operator, external and note steps
    get none, and a format ≤ 4 agent step's legacy hint is never promoted (#1654)."""
    return step.get("actor") == "agent" and step.get("role") is not None


def _versions(ctx: Context) -> dict[str, dict]:
    """specialist id → its CURRENT version as `qualify` reads it."""
    out = {}
    for spec in ctx.pool:
        cur = spec.get("current") or {}
        out[spec["id"]] = {**cur, "id": spec["id"], "archived": bool(spec.get("archived"))}
    return out


def version_of(ctx: Context, pinned: dict, versions: dict | None = None) -> dict | None:
    """The version an assignment pins, as `qualify` reads it — or None when it is gone.

    The plan pins `{id, version}`. The pool holds each specialist's current version; a pinned
    version that is not current is read through `ctx.pinned` (filled by `snapshot`)."""
    sid, n = pinned.get("id"), pinned.get("version")
    cur = (versions or _versions(ctx)).get(sid)
    if cur is not None and cur.get("version") == n:
        return cur
    old = ctx_pinned(ctx).get((sid, n))
    if old is None:
        return None
    archived = cur["archived"] if cur is not None else True
    return {**old, "id": sid, "archived": archived}


def ctx_pinned(ctx: Context) -> dict:
    return ctx.pinned


def effective_of(assignment: dict, version: dict | None) -> dict:
    ovr = assignment.get("override")
    if ovr is not None:
        return dict(ovr)
    return own_effective(version or {})


def order_relation(doc: dict) -> dict[str, set[str]]:
    """step id → every step that must finish before it (the transitive `after`). Objective
    `after` (#1797) counts: every step of an objective waits for the objectives it comes after.
    Rework edges are ignored: reopened episodes run one at a time."""
    steps = doc["steps"]
    by_obj: dict[str, list[str]] = {}
    for s in steps:
        by_obj.setdefault(s["objective"], []).append(s["id"])
    obj_after = {o["id"]: list(o.get("after") or []) for o in doc["objectives"]}
    direct: dict[str, set[str]] = {}
    for s in steps:
        preds = set(s.get("after") or [])
        for a in obj_after.get(s["objective"], []):
            preds |= set(by_obj.get(a, []))
        direct[s["id"]] = preds
    closed: dict[str, set[str]] = {}

    def walk(sid: str, seen: frozenset) -> set[str]:
        if sid in closed:
            return closed[sid]
        out: set[str] = set()
        for p in direct.get(sid, ()):  # the plan validator guarantees a DAG
            if p in seen:
                continue
            out.add(p)
            out |= walk(p, seen | {p})
        closed[sid] = out
        return out

    for s in steps:
        walk(s["id"], frozenset({s["id"]}))
    return closed


def peak(ids: list[str], before: dict[str, set[str]]) -> int:
    """The largest set of `ids` pairwise unordered by `before` (an antichain): the width of the
    dependency order restricted to them. Dilworth: width = n − a maximum matching in the
    comparability bipartite graph. A fixed algorithm, independent of any scheduler."""
    ids = sorted(set(ids))
    if not ids:
        return 0
    succ = {a: [b for b in ids if a in before.get(b, ())] for a in ids}
    match: dict[str, str] = {}

    def augment(a: str, seen: set[str]) -> bool:
        for b in succ[a]:
            if b in seen:
                continue
            seen.add(b)
            if b not in match or augment(match[b], seen):
                match[b] = a
                return True
        return False

    matched = sum(1 for a in ids if augment(a, set()))
    return len(ids) - matched


def queue_text(capacity: int, n: int) -> str:
    verb = "runs" if capacity == 1 else "run"
    return f"will queue: {capacity} of {n} {verb} first"


# ---- distinctness --------------------------------------------------------------------------------


def _dims(assignment: dict, versions: dict, ctx: Context) -> dict | None:
    if assignment is None:
        return None
    v = version_of(ctx, assignment["specialist"], versions)
    eff = effective_of(assignment, v)
    return {
        "specialist": assignment["specialist"]["id"],
        "engine": eff.get("engine"),
        "model": eff.get("model"),
    }


def distinct_reasons(
    sid: str, doc: dict, assignments: dict, ctx: Context, versions: dict | None = None
) -> list[dict]:
    """`not_distinct` reasons for step `sid` under `assignments` (step id → assignment), checked
    in BOTH directions: the step's own `distinct_from`, and every step whose `distinct_from` names
    it. Only assigned pairs are compared."""
    versions = versions or _versions(ctx)
    mine = _dims(assignments.get(sid), versions, ctx)
    if mine is None:
        return []
    by_id = {s["id"]: s for s in doc["steps"]}
    pairs: list[tuple[str, str]] = []
    for d in by_id[sid].get("distinct_from") or []:
        pairs.append((d["step"], d["constraint"]))
    for s in doc["steps"]:
        for d in s.get("distinct_from") or []:
            if d["step"] == sid:
                pairs.append((s["id"], d["constraint"]))
    out: list[dict] = []
    for other, constraint in sorted(set(pairs)):
        if constraint not in TEAM_CONSTRAINTS:
            continue
        theirs = _dims(assignments.get(other), versions, ctx)
        if theirs is None or theirs[constraint] != mine[constraint]:
            continue
        what = {"specialist": "specialist", "engine": "agent", "model": "model"}[constraint]
        out.append(
            _r(
                "not_distinct",
                f"same {what} as step {other} ({mine[constraint]}), which must differ",
            )
        )
    return out


# ---- the assembler -------------------------------------------------------------------------------


def _cost(version: dict, ctx: Context, used: dict[str, int]) -> tuple:
    eff = own_effective(version)
    rank = model_grades.rank(grade_of(ctx, eff["engine"] or "", eff["model"]))
    headroom = int(version.get("capacity") or 0) - used.get(version["id"], 0)
    return (rank, -headroom, version["id"])


def _qualified(step: dict, versions: dict, ctx: Context) -> list[dict]:
    """The specialists that qualify for `step` on their own choice, archived ones never."""
    return [
        v for v in versions.values() if not v.get("archived") and qualify(step, v, None, ctx)["ok"]
    ]


def _conflicts(sid: str, spec: dict, doc_index: dict, chosen: dict, versions, ctx) -> bool:
    """Would `spec` on step `sid` (on its own choice) break a distinct pair with a step already
    in `chosen` (step id → version)?"""
    mine = {"specialist": spec["id"], **own_effective(spec)}
    for other, constraint in doc_index.get(sid, ()):  # both directions, precomputed
        theirs_v = chosen.get(other)
        if theirs_v is None:
            continue
        theirs = theirs_v["_dims"]
        if theirs[constraint] == mine[constraint]:
            return True
    return False


def _pairs_index(doc: dict) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, set] = {}
    for s in doc["steps"]:
        for d in s.get("distinct_from") or []:
            if d["constraint"] not in TEAM_CONSTRAINTS:
                continue
            out.setdefault(s["id"], set()).add((d["step"], d["constraint"]))
            out.setdefault(d["step"], set()).add((s["id"], d["constraint"]))
    return {k: sorted(v) for k, v in out.items()}


def assemble(doc: dict, assignments: dict, ctx: Context) -> dict:
    """Propose a team for every UNASSIGNED assignable step; assigned steps stay as they are (and
    constrain the rest). Returns `{assignments, unassigned}`: the new assignment for each step it
    filled, and the blocking reasons of each it could not. Raises `TeamRefused`
    (`assembly_too_large`) past the node budget: never a partial or order-dependent solve."""
    versions = _versions(ctx)
    pairs = _pairs_index(doc)
    steps = [s for s in doc["steps"] if assignable(s)]
    todo = [s for s in steps if assignments.get(s["id"]) is None]
    if not todo:
        raise TeamRefused("nothing_to_assemble", "every agent step already has a specialist")
    fixed: dict[str, dict] = {}
    used: dict[str, int] = {}
    for s in steps:
        a = assignments.get(s["id"])
        if a is None:
            continue
        v = version_of(ctx, a["specialist"], versions)
        dims = _dims(a, versions, ctx)
        fixed[s["id"]] = {**(v or {}), "id": a["specialist"]["id"], "_dims": dims}
        used[a["specialist"]["id"]] = used.get(a["specialist"]["id"], 0) + 1
    quals = {s["id"]: _qualified(s, versions, ctx) for s in todo}
    fillable = sum(1 for s in todo if quals[s["id"]])
    best: dict = {"n": -1, "pick": {}}
    nodes = 0

    def _open(s: dict, chosen: dict) -> bool:
        return any(not _conflicts(s["id"], v, pairs, chosen, versions, ctx) for v in quals[s["id"]])

    def dfs(i: int, chosen: dict, used: dict, n: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > ASSEMBLY_NODE_BUDGET:
            raise TeamRefused(
                "assembly_too_large",
                f"this plan needs more than {ASSEMBLY_NODE_BUDGET} search steps to assemble; "
                "nothing was written. Assign some steps by hand, then assemble the rest",
            )
        # The bound counts only the remaining steps that still have a candidate compatible with
        # what is already chosen (forward checking): a step a constraint has already blocked can
        # never be filled below this node, so it must not keep alive a search that cannot beat
        # the best found (independent review: one blocked step exhausted the budget otherwise).
        if n + sum(1 for s in todo[i:] if _open(s, chosen)) <= best["n"]:
            return False  # cannot beat the best found: pruned
        if i == len(todo):
            best["n"], best["pick"] = n, {k: v for k, v in chosen.items() if k not in fixed}
            return n == fillable  # nothing can be better: stop
        s = todo[i]
        cands = sorted(quals[s["id"]], key=lambda v: _cost(v, ctx, used))
        for v in cands:
            if _conflicts(s["id"], v, pairs, chosen, versions, ctx):
                continue
            chosen[s["id"]] = {**v, "_dims": {"specialist": v["id"], **own_effective(v)}}
            used[v["id"]] = used.get(v["id"], 0) + 1
            done = dfs(i + 1, chosen, used, n + 1)
            used[v["id"]] -= 1
            del chosen[s["id"]]
            if done:
                return True
        return dfs(i + 1, chosen, used, n)

    dfs(0, dict(fixed), dict(used), 0)
    picks = best["pick"]
    # The reasons and alternates are worded against the FINAL team.
    final: dict[str, dict] = dict(assignments)
    for sid, v in picks.items():
        final[sid] = {"specialist": {"id": v["id"], "version": v["version"]}, "override": None}
    out: dict[str, dict] = {}
    counts = _counts(final)
    for s in todo:
        v = picks.get(s["id"])
        if v is None:
            continue
        out[s["id"]] = _describe(s, v, doc, final, ctx, versions, counts, by="assembled")
    unassigned = {
        s["id"]: blocking(s, doc, final, ctx, versions) for s in todo if s["id"] not in picks
    }
    return {"assignments": out, "unassigned": unassigned}


def _counts(assignments: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in assignments.values():
        if a is not None:
            out[a["specialist"]["id"]] = out.get(a["specialist"]["id"], 0) + 1
    return out


def _grade_clause(step: dict, version: dict, ctx: Context, eff: dict | None = None) -> str:
    e = eff or own_effective(version)
    need = (step.get("role") or {}).get("min_grade")
    got = label(grade_of(ctx, e.get("engine") or "", e.get("model")))
    return f"{got} ≥ {label(need)}" if need else f"{got}; any grade"


def _feasible_others(
    step: dict, chosen_id: str, doc: dict, final: dict, ctx: Context, versions: dict
) -> list[dict]:
    """Every OTHER specialist that qualifies for `step` and keeps every distinct pair with the
    rest of the final team."""
    out = []
    for v in _qualified(step, versions, ctx):
        if v["id"] == chosen_id:
            continue
        trial = dict(final)
        trial[step["id"]] = {
            "specialist": {"id": v["id"], "version": v["version"]},
            "override": None,
        }
        if distinct_reasons(step["id"], doc, trial, ctx, versions):
            continue
        out.append(v)
    return out


def _describe(
    step: dict,
    v: dict,
    doc: dict,
    final: dict,
    ctx: Context,
    versions: dict,
    counts: dict,
    *,
    by: str,
) -> dict:
    """The stored assignment for a pick: reason, up to 3 alternates, notes."""
    role = _role_name(step)
    others = _feasible_others(step, v["id"], doc, final, ctx, versions)
    base = {k: n for k, n in counts.items()}
    base[v["id"]] = base.get(v["id"], 1) - 1  # headroom as it stood before this pick
    ranked = sorted(others, key=lambda o: _cost(o, ctx, base))
    clause = _grade_clause(step, v, ctx)
    if by == "assembled":
        if not others:
            reason = f"only specialist with {role}; {clause}"
        else:
            reason = f"cheapest of {len(others) + 1} that qualify for {role}; {clause}"
    else:
        reason = f"chosen from {len(others) + 1} that qualify for {role}; {clause}"
    distinct = [
        d["step"] for d in step.get("distinct_from") or [] if d["constraint"] in TEAM_CONSTRAINTS
    ]
    if distinct:
        reason += f"; distinct from {', '.join(distinct)}"
    mine = _cost(v, ctx, base)
    alternates = []
    for o in ranked[:ALTERNATES_MAX]:
        theirs = _cost(o, ctx, base)
        if theirs[0] > mine[0]:
            why = "a higher grade than needed"
        elif theirs[0] < mine[0]:
            why = "a lower grade"
        elif theirs[1] > mine[1]:
            why = "less free capacity in this plan"
        elif theirs[1] < mine[1]:
            why = "more free capacity in this plan"
        else:
            why = "same grade and free capacity, ordered after by id"
        alternates.append(
            {
                "specialist": {"id": o["id"], "version": o["version"]},
                "reason": f"also qualifies ({_grade_clause(step, o, ctx)}); {why}"[:REASON_MAX],
            }
        )
    a = {
        "specialist": {"id": v["id"], "version": v["version"]},
        "override": None,
        "reason": reason[:REASON_MAX],
        "alternates": alternates,
        "notes": [],
    }
    a["notes"] = notes_for(step["id"], a, doc, final, ctx, versions)
    return a


def notes_for(
    sid: str, assignment: dict, doc: dict, final: dict, ctx: Context, versions: dict | None = None
) -> list[str]:
    """The closed notes for one assignment against the final team: `usage_unknown` when its
    effective client's remaining usage is unknown, `will_queue` when its specialist's peak in this
    plan exceeds its capacity."""
    versions = versions or _versions(ctx)
    v = version_of(ctx, assignment["specialist"], versions)
    eff = effective_of(assignment, v)
    notes = []
    client = ctx.clients.get(eff.get("engine"))
    if client is not None and client.usage == "unknown":
        notes.append("usage_unknown")
    mine = [
        k for k, a in final.items() if a and a["specialist"]["id"] == assignment["specialist"]["id"]
    ]
    if v is not None and peak(mine, order_relation(doc)) > int(v.get("capacity") or 0):
        notes.append("will_queue")
    return notes


def blocking(
    step: dict, doc: dict, final: dict, ctx: Context, versions: dict | None = None
) -> dict:
    """Why `step` has no specialist: a one-line summary plus each candidate's codes."""
    versions = versions or _versions(ctx)
    role = step.get("role") or {}
    need = role.get("min_grade")
    holders = sorted(
        {a["specialist"]["id"] for k, a in final.items() if a and k in _distinct_steps(step)}
    )
    parts = []
    if need:
        parts.append(label(need))
    if role.get("skills"):
        parts.append(_role_name(step))
    summary = "no qualifying specialist"
    if parts:
        summary += ": needs " + " + ".join(parts)
    if holders:
        summary += ", distinct from " + ", ".join(holders)
    candidates = []
    for v in sorted(versions.values(), key=lambda x: x["id"]):
        if v.get("archived") and not any(
            s.get("id") in (role.get("skills") or []) for s in v.get("skills") or []
        ):
            continue  # an archived specialist without the skill is not worth a line
        verdict = qualify(step, v, None, ctx)
        reasons = list(verdict["reasons"])
        if verdict["ok"]:
            trial = dict(final)
            trial[step["id"]] = {
                "specialist": {"id": v["id"], "version": v["version"]},
                "override": None,
            }
            reasons = distinct_reasons(step["id"], doc, trial, ctx, versions)
        if reasons:
            candidates.append({"specialist": v["id"], "reasons": reasons})
    return {"summary": summary[:REASON_MAX], "candidates": candidates[:20]}


def _distinct_steps(step: dict) -> set[str]:
    return {
        d["step"] for d in step.get("distinct_from") or [] if d["constraint"] in TEAM_CONSTRAINTS
    }


# ---- choices offered by index --------------------------------------------------------------------


def _digest(kind: str, mission_id: str, revision: int, sid: str, rows: list) -> str:
    raw = json.dumps(
        [kind, mission_id, revision, sid, rows], sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:32]


def candidates(
    step: dict, doc: dict, assignments: dict, ctx: Context, *, mission_id: str, revision: int
) -> dict:
    """The specialists offered for `step`, cheapest first, each with its verdict (qualify plus
    distinctness against the rest of the plan). Archived ones are not offered. `listing` binds the
    list to this mission, revision and step: an `assign` carrying another listing is refused."""
    versions = _versions(ctx)
    others = {k: a for k, a in assignments.items() if k != step["id"]}
    used = _counts(others)
    live = [v for v in versions.values() if not v.get("archived")]
    rows = []
    for v in sorted(live, key=lambda x: _cost(x, ctx, used)):
        verdict = qualify(step, v, None, ctx)
        reasons = list(verdict["reasons"])
        trial = dict(others)
        trial[step["id"]] = {
            "specialist": {"id": v["id"], "version": v["version"]},
            "override": None,
        }
        reasons += distinct_reasons(step["id"], doc, trial, ctx, versions)
        eff = own_effective(v)
        rows.append(
            {
                "specialist": {"id": v["id"], "version": v["version"]},
                "title": v.get("title") or v["id"],
                "engine": eff["engine"],
                "model": eff["model"],
                "effort": eff["effort"],
                "grade": grade_of(ctx, eff["engine"] or "", eff["model"]),
                "ok": not reasons,
                "reasons": reasons,
            }
        )
    ident = [[r["specialist"]["id"], r["specialist"]["version"], r["ok"]] for r in rows]
    for i, r in enumerate(rows):
        r["index"] = i
    return {"choices": rows, "listing": _digest("assign", mission_id, revision, step["id"], ident)}


def override_choices(
    step: dict, doc: dict, assignments: dict, ctx: Context, *, mission_id: str, revision: int
) -> dict:
    """The `{engine, model, effort}` entries offered for `step`'s override, each with its verdict.

    For each agent client (engine id order): each model in the client's OWN list, at the model's
    own effort and at each effort it lists. `default` is never offered: it is not one model and is
    never graded. Empty when the step has no specialist (an override needs one)."""
    a = assignments.get(step["id"])
    if a is None:
        return {"choices": [], "listing": _digest("override", mission_id, revision, step["id"], [])}
    versions = _versions(ctx)
    v = version_of(ctx, a["specialist"], versions) or {
        **a["specialist"],
        "archived": True,
        "skills": [],
    }
    rows: list[dict] = []
    for engine in sorted(ctx.clients):
        client = ctx.clients[engine]
        for model, efforts in (client.models or {}).items():
            for effort in (None, *efforts):
                if len(rows) >= OVERRIDE_CHOICES_MAX:
                    break
                eff = {"engine": engine, "model": model, "effort": effort}
                verdict = qualify(step, v, eff, ctx)
                reasons = list(verdict["reasons"])
                trial = dict(assignments)
                trial[step["id"]] = {**a, "override": eff}
                reasons += distinct_reasons(step["id"], doc, trial, ctx, versions)
                rows.append(
                    {
                        **eff,
                        "label": client.label,
                        "grade": grade_of(ctx, engine, model),
                        "ok": not reasons,
                        "reasons": reasons,
                    }
                )
    for i, r in enumerate(rows):
        r["index"] = i
    ident = [[r["engine"], r["model"], r["effort"], r["ok"]] for r in rows]
    return {
        "choices": rows,
        "listing": _digest("override", mission_id, revision, step["id"], ident),
    }


# ---- the plan view: re-derived on every read -----------------------------------------------------


def assignments_of(doc: dict) -> dict[str, dict | None]:
    return {s["id"]: s["envelope"]["assignment"] for s in doc["steps"]}


def view(doc: dict, ctx: Context) -> dict:
    """The team as it stands NOW: each step's stored assignment plus what is derived at read —
    the effective model and its grade, the current verdict ("no longer qualifies" when it fails),
    the notes and their wording, and why an unassigned step has no specialist. Never cached."""
    versions = _versions(ctx)
    assignments = assignments_of(doc)
    before = order_relation(doc)
    steps_out = []
    agent_ids = []
    for s in doc["steps"]:
        row = {
            "id": s["id"],
            "objective": s["objective"],
            "title": s["title"],
            "actor": s["actor"],
            "role": s.get("role"),
            "distinct_from": [
                d for d in s.get("distinct_from") or [] if d["constraint"] in TEAM_CONSTRAINTS
            ],
            "assignable": assignable(s),
            "assignment": None,
        }
        a = assignments.get(s["id"])
        if assignable(s):
            agent_ids.append(s["id"])
            if a is None:
                row["unassigned"] = blocking(s, doc, assignments, ctx, versions)
            else:
                v = version_of(ctx, a["specialist"], versions)
                eff = effective_of(a, v)
                if v is None:
                    verdict = {
                        "ok": False,
                        "reasons": [
                            _r(
                                "specialist_archived",
                                f"{a['specialist']['id']} v{a['specialist']['version']} is gone",
                            )
                        ],
                    }
                else:
                    verdict = qualify(s, v, eff, ctx)
                reasons = list(verdict["reasons"]) + distinct_reasons(
                    s["id"], doc, assignments, ctx, versions
                )
                notes = notes_for(s["id"], a, doc, assignments, ctx, versions)
                mine = [
                    k
                    for k, x in assignments.items()
                    if x and x["specialist"]["id"] == a["specialist"]["id"]
                ]
                n = peak(mine, before)
                cap = int((v or {}).get("capacity") or 0)
                row["assignment"] = a
                row["derived"] = {
                    "title": (v or {}).get("title") or a["specialist"]["id"],
                    "effective": eff,
                    "grade": grade_of(ctx, eff.get("engine") or "", eff.get("model")),
                    "ok": not reasons,
                    "reasons": reasons,
                    "notes": [
                        {
                            "code": c,
                            "text": queue_text(cap, n)
                            if c == "will_queue"
                            else "remaining usage unknown; the launch cap still applies",
                        }
                        for c in notes
                    ],
                }
        steps_out.append(row)
    plan_peak = peak(agent_ids, before)
    return {
        "steps": steps_out,
        "peak": plan_peak,
        "pool_max": ctx.pool_max,
        "pool_note": (queue_text(ctx.pool_max, plan_peak) if plan_peak > ctx.pool_max else None),
        "pool_size": sum(1 for v in versions.values() if not v.get("archived")),
    }


# ---- the context ---------------------------------------------------------------------------------


def _usage_states() -> dict[str, str]:
    """engine → `ok` | `exhausted`; anything not measured, stale or failed is absent (unknown)."""
    from . import agent_usage

    out: dict[str, str] = {}
    try:
        rows = agent_usage.snapshot()
    except Exception:  # noqa: BLE001 — unreadable usage is unknown, never a refusal
        return out
    for row in rows:
        if row.get("source") not in ("plan", "tokens", "manual") or row.get("stale"):
            continue
        pct = row.get("used_pct")
        if isinstance(pct, bool) or not isinstance(pct, int | float):
            continue
        out[row["engine"]] = "exhausted" if pct >= agent_usage.EXHAUST_PCT else "ok"
    return out


def _client(prov, usage: dict[str, str]) -> Client:
    from . import mission_structured, native_models

    m = prov.manifest
    eid = prov.engine_id
    label_ = getattr(getattr(m, "identity", None), "label", None) or eid
    if m.runtime != "api":
        return Client(
            eid,
            label_,
            m.tools_mcp is not None,
            False,
            "only a mission-ready API client can hold a mission role",
        )
    try:
        ok, why = mission_structured.admits(prov)
    except Exception:  # noqa: BLE001 — a client that can't answer is not admitted
        ok, why = False, "it could not be asked"
    # The client's own list, asked ONLY of a client that can hold a mission role: a read of the
    # team never spawns a CLI whose every entry would be refused anyway.
    models = None
    if ok:
        try:
            listing = native_models.models(prov)
            if listing.status == "ok":
                models = {mm["id"]: tuple(mm.get("efforts") or ()) for mm in listing.models}
        except Exception:  # noqa: BLE001 — an unreadable list offers nothing
            models = None
    # A native API client's usage is its console agent's (`api.source`): the same account.
    family = getattr(getattr(m, "api", None), "source", None) or None
    state = usage.get(eid) or (usage.get(family) if family else None) or "unknown"
    return Client(eid, label_, m.tools_mcp is not None, bool(ok), why, models, state)


def snapshot() -> Context:
    """Read everything the team rules need, once. Blocking (a client may be asked for its model
    list, its readiness probed): call it OFF the event loop."""
    from . import engines, specialist_capacity, specialists
    from . import specialists_store as store

    pool = store.list_all()
    try:
        library = specialists.library_revisions()
    except Exception:  # noqa: BLE001 — an unreadable library flags nothing
        library = None
    usage = _usage_states()
    clients: dict[str, Client] = {}
    for prov in engines.all_providers():
        m = getattr(prov, "manifest", None)
        if m is None or m.identity.kind != "agent":
            continue
        if m.runtime == "api" or any(
            (s.get("current") or {}).get("engine") == prov.engine_id for s in pool
        ):
            clients[prov.engine_id] = _client(prov, usage)
    return Context(
        grades=model_grades.get(),
        clients=clients,
        library=library,
        pool=pool,
        pool_max=specialist_capacity.max_instances(),
    )


def with_pinned(ctx: Context, doc: dict) -> Context:
    """`ctx` able to read every version `doc` pins that is no longer current. Blocking."""
    from . import specialists_store as store

    versions = _versions(ctx)
    pinned: dict = {}
    for a in assignments_of(doc).values():
        if a is None:
            continue
        sid, n = a["specialist"]["id"], a["specialist"]["version"]
        cur = versions.get(sid)
        if cur is not None and cur.get("version") == n:
            continue
        try:
            pinned[(sid, n)] = store.version(sid, n)
        except Exception:  # noqa: BLE001, S112 — a gone version reads as gone
            continue
    return replace(ctx, pinned=pinned)


# ---- operations (#1950 §5): the same function for both authors -----------------------------------

#: The team operations P5 adds to #1579's `POST /api/missions/{id}/plan/ops`.
OPS = ("assemble", "assign", "set_override", "clear_override")
#: Who asked. `hq` is a provenance label for a change HQ proposed and the operator confirmed on a
#: card; it carries no authority beyond the operator's (D8: no bypass on the planner).
AUTHORS = ("operator", "hq")
_OP_KEYS = {
    "assemble": set(),
    "assign": {"step", "index", "listing"},
    "set_override": {"step", "index", "listing"},
    "clear_override": {"step"},
}
_COMMON = {"op", "operation_id", "expected_revision", "reason"}
_UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
STEP_ID_RE = re.compile(r"\Ao[1-9][0-9]?\.[a-z][a-z0-9_-]{0,31}\Z")
_LISTING_RE = re.compile(r"\Asha256:[0-9a-f]{32}\Z")


def _bad(text: str, status: int = 422) -> TeamRefused:
    return TeamRefused("invalid_request", text, status=status)


def parse_op(body: object) -> dict:
    """The operation, checked to the letter, or `TeamRefused` (422). An index is the ONLY way to
    name a specialist or a model: a string where an index belongs, an extra key or a missing one
    is refused, so no model-written text becomes an engine, model or effort (#883/#893)."""
    if not isinstance(body, dict):
        raise _bad("the operation must be an object")
    op = body.get("op")
    if op not in OPS:
        raise _bad(f"op must be one of {', '.join(OPS)}")
    keys = _COMMON | _OP_KEYS[op]
    extra = set(body) - keys - {"author"}
    missing = keys - set(body)
    if extra or missing:
        raise _bad(f"{op} takes exactly {', '.join(sorted(keys))} (and optionally author)")
    author = body.get("author", "operator")
    if author not in AUTHORS:
        raise _bad("author must be operator or hq")
    if not isinstance(body["operation_id"], str) or not _UUID_RE.fullmatch(body["operation_id"]):
        raise _bad("operation_id must be a lowercase UUID")
    rev = body["expected_revision"]
    if type(rev) is not int or not 0 < rev < 1_000_000_000:
        raise _bad("expected_revision must be the plan revision you read")
    reason = body["reason"]
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason) > REASON_MAX
        or any(ch < " " or ch == "\x7f" for ch in reason)
    ):
        raise _bad(f"reason must be one line of at most {REASON_MAX} characters")
    try:
        reason.encode()
    except UnicodeEncodeError:
        raise _bad("reason must be encodable as UTF-8") from None
    out = {
        "op": op,
        "operation_id": body["operation_id"],
        "expected_revision": rev,
        "reason": reason,
        "author": author,
    }
    if "step" in keys:
        if not isinstance(body["step"], str) or not STEP_ID_RE.fullmatch(body["step"]):
            raise _bad("step must be a plan step id")
        out["step"] = body["step"]
    if "index" in keys:
        idx = body["index"]
        if type(idx) is not int or not 0 <= idx < 10_000:
            raise _bad("index must be a position in the list the server offered")
        if not isinstance(body["listing"], str) or not _LISTING_RE.fullmatch(body["listing"]):
            raise _bad("listing must be the digest of the list the server offered")
        out["index"], out["listing"] = idx, body["listing"]
    return out


def request_digest(op: dict) -> str:
    """What an `operation_id` is bound to besides its mission and author: the request itself."""
    core = {k: v for k, v in op.items() if k not in ("operation_id", "author")}
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _step(doc: dict, sid: str) -> dict:
    for s in doc["steps"]:
        if s["id"] == sid:
            return s
    raise TeamRefused("unknown_step", f"this plan has no step {sid}")


def _offered(listing: dict, op: dict) -> dict:
    if listing["listing"] != op["listing"]:
        raise TeamRefused(
            "listing_changed",
            "the list changed since it was read (or belongs to another mission or step); "
            "read it again",
            status=409,
        )
    rows = listing["choices"]
    if not 0 <= op["index"] < len(rows):
        raise TeamRefused("not_offered", "that index was not offered")
    row = rows[op["index"]]
    if not row["ok"]:
        first = row["reasons"][0]
        raise TeamRefused(first["code"], first["text"], reasons=row["reasons"])
    return row


def apply_op(doc: dict, op: dict, ctx: Context, *, mission_id: str, revision: int) -> dict:
    """The document `op` turns `doc` into, or `TeamRefused` with its code and text — nothing is
    written on a refusal. Pure over `ctx`: the route and HQ's card reach this one function."""
    new = json.loads(json.dumps(doc))
    assignments = assignments_of(new)
    versions = _versions(ctx)
    changed: dict[str, dict | None] = {}
    if op["op"] == "assemble":
        result = assemble(new, assignments, ctx)
        if not result["assignments"]:
            first = next(iter(result["unassigned"].values()), None)
            text = first["summary"] if first else "no step could be assigned"
            raise TeamRefused(
                "nothing_assignable",
                f"no step could be assigned ({text}); nothing was written",
                extra={"unassigned": result["unassigned"]},
            )
        changed = dict(result["assignments"])
    else:
        step = _step(new, op["step"])
        if not assignable(step):
            raise TeamRefused(
                "not_assignable",
                f"step {step['id']} is not an agent step with a role: it gets no specialist",
            )
        current = assignments.get(step["id"])
        if op["op"] == "assign":
            row = _offered(
                candidates(step, new, assignments, ctx, mission_id=mission_id, revision=revision),
                op,
            )
            v = versions[row["specialist"]["id"]]
            trial = dict(assignments)
            trial[step["id"]] = {"specialist": row["specialist"], "override": None}
            changed[step["id"]] = _describe(
                step, v, new, trial, ctx, versions, _counts(trial), by="hand"
            )
        elif current is None:
            raise TeamRefused(
                "unassigned", f"step {step['id']} has no specialist: assign one first"
            )
        elif op["op"] == "set_override":
            row = _offered(
                override_choices(
                    step, new, assignments, ctx, mission_id=mission_id, revision=revision
                ),
                op,
            )
            eff = {"engine": row["engine"], "model": row["model"], "effort": row["effort"]}
            v = version_of(ctx, current["specialist"], versions) or {}
            own = own_effective(v)
            a = {**current, "override": eff}
            a["reason"] = (
                f"override for this mission: {eff['model']} on {eff['engine']}, "
                f"{_grade_clause(step, v, ctx, eff)}; {current['specialist']['id']} itself "
                f"runs on {own.get('model')}"
            )[:REASON_MAX]
            changed[step["id"]] = a
        else:  # clear_override
            if current.get("override") is None:
                raise TeamRefused("no_override", f"step {step['id']} has no override to clear")
            v = version_of(ctx, current["specialist"], versions)
            a = {**current, "override": None}
            # Re-checked like every other edit (#1950 §1: `not_distinct` "again after every
            # edit"): back on its own model, the step must still qualify and keep its pairs.
            trial = dict(assignments)
            trial[step["id"]] = a
            reasons = (
                qualify(step, v, None, ctx)["reasons"]
                if v is not None
                else [_r("specialist_archived", f"{current['specialist']['id']} is gone")]
            ) + distinct_reasons(step["id"], new, trial, ctx, versions)
            if reasons:
                raise TeamRefused(reasons[0]["code"], reasons[0]["text"], reasons=reasons)
            a["reason"] = f"override cleared; runs as saved: {_grade_clause(step, v, ctx)}"[
                :REASON_MAX
            ]
            changed[step["id"]] = a
    final = dict(assignments)
    final.update(changed)
    for sid, a in changed.items():
        a["notes"] = notes_for(sid, a, new, final, ctx, versions)
    for s in new["steps"]:
        if s["id"] in changed:
            s["envelope"]["assignment"] = changed[s["id"]]
    return new


def load_context(doc: dict | None) -> Context:
    """`snapshot()` able to read every version `doc` pins. Blocking: off the event loop."""
    ctx = snapshot()
    return with_pinned(ctx, doc) if doc is not None else ctx
