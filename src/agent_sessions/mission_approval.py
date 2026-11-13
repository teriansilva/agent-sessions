"""Approving the enumerated plan is the tap (#1951, #1613 P6 D3; slice A).

The Commander approves ONE plan revision once. The approval PINS what it enumerates and stores
their digest; every launch and every gate re-checks every pin against current state, and a stale
binding or a grade drift is refused with its reason. Nothing is ever remapped.

* **`pins(doc, revision, ctx, facts)`** — what an approval of `doc` enumerates: per objective its
  project, procedure (and for a deployed one the playbook revision and the deployment's identity
  and generation) and its allowed actions; per agent step its specialist `{id, version}`, its
  effective `{engine, model, effort}` and its approved alternates; per project the equipment
  OFFERED, as `(kind, id, name, scope)` — never a value (D-P6-5); per rework edge `max_rounds`.
  **The grade is never pinned**: it is derived at read (`specialists.md`), and that is what makes
  grade drift refusable.
* **`recheck(approval, doc, revision, ctx, facts, step=None)`** — the ONE re-check: the
  approval's pins against the state now, through P5's own `mission_team.qualify` on the effective
  model (never a second copy of the predicate). The result is `ok` or reasons from the closed
  `CODES`, each with the old and new value where there is one.
* **`admit(mission_id, step, boundary)`** — the boundary every launch, gate and standing-consent
  launch calls. It reads the state fresh, re-checks, and on a refusal records it ONCE per (step,
  what changed) (`missions.record_refusal`). Slice A ships the function and its callers in the
  standing-consent harness; #1581's step claim and the gate settlements call it in slice B.
* **Standing consent** (`consent_scope`, `consent_verdict`) — the Commander's bounded, expiring,
  revocable consent for an automation to launch from an approval. Inert until #1901 / P7b wire a
  consumer: nothing here fires anything.

Only `read_facts` and `context_for` read stores (blocking: call them off the event loop); every
other function here is pure over what they read.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

from . import mission_team as team
from . import model_grades

#: The closed refusal codes (#1951 §2). A reason's `code` is always one of these.
CODES: tuple[str, ...] = (
    "plan_revision_superseded",
    "specialist_version_changed",
    "specialist_archived",
    "model_changed",
    "model_unavailable",
    "grade_below_min",
    "ungraded",
    "skill_pin_stale",
    "playbook_revision_changed",
    "deployment_changed",
    "project_unavailable",
    "equipment_changed",
    "approval_revoked",
    "approval_expired",
)
#: Where a re-check runs: a step launch (#1581's claim, a rework reopen, an alternate), the
#: operator's settlement of a gate or floor item, a standing-consent launch, and a plain read.
BOUNDARIES = ("launch", "gate", "consent", "read")
#: `qualify`'s codes, as the re-check names them. A pinned version is immutable and the plan
#: revision is pinned, so `skill_missing` and `not_distinct` can only follow a change the other
#: codes already name; they are mapped rather than invented.
_QUALIFY_MAP = {
    "grade_below_min": "grade_below_min",
    "ungraded": "ungraded",
    "skill_pin_stale": "skill_pin_stale",
    "skill_missing": "skill_pin_stale",
    "specialist_archived": "specialist_archived",
    "model_not_offered": "model_unavailable",
    "engine_not_mission_ready": "model_unavailable",
    "engine_cannot_load_skills": "model_unavailable",
    "usage_exhausted": "model_unavailable",
}
#: Standing consent's expiry (D-P6-4, reversible): the default and the hard maximum, in days.
CONSENT_DAYS_DEFAULT = 7
CONSENT_DAYS_MAX = 30
PINS_FORMAT = 1
TEXT_MAX = 240
DIGEST_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
APPROVAL_ID_RE = re.compile(r"\Aapr-[0-9a-f]{20}\Z")
CONSENT_ID_RE = re.compile(r"\Acon-[0-9a-f]{20}\Z")


class ApprovalRefused(Exception):
    """The plan can't be approved (or consented to) as asked. `status` is the HTTP answer."""

    def __init__(self, code: str, text: str, *, status: int = 409, reasons=None, extra=None):
        super().__init__(text)
        self.code = code
        self.text = text
        self.status = status
        self.reasons = list(reasons or [{"code": code, "text": text}])
        self.extra = dict(extra or {})


@dataclass(frozen=True)
class Facts:
    """The non-pool state a re-check compares against, read once (`read_facts`).

    `projects`: project id → `{usable: bool, equipment: list | None, deployment: dict | None}`.
    `equipment` None means the variables store could not be read (fail closed: a change).
    `deployment` is `{reason, playbook_id, revision, id, generation}`; None when not asked."""

    projects: dict = field(default_factory=dict)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _r(code: str, text: str, *, step: str | None = None, old=None, new=None) -> dict:
    out: dict = {"code": code, "text": text[:TEXT_MAX]}
    if step is not None:
        out["step"] = step
    if old is not None or new is not None:
        out["old"], out["new"] = old, new
    return out


# ---- reading the state ---------------------------------------------------------------------------


def equipment_of(pid: str) -> list[dict] | None:
    """What project `pid` OFFERS its agents now, as `(kind, id, name, scope)` rows sorted, or None
    when the variables store can't be read. The secrets broker's own offer (`_offered`), so the
    pin and what a grant could materialize can never disagree; plus the project's memory scope
    while its memory is on. Never a value, a revision or a marker (D-P6-5)."""
    from . import memory, secrets_broker
    from . import template_vars as store

    try:
        offered, _refused = secrets_broker._offered(pid)
    except secrets_broker.Refused:
        return None
    items = []
    for name, holder in offered.items():
        # The holder is the GLOBAL record when the project binds it by `ref: global`.
        scope = "global" if store._is_global(holder) else "project"
        items.append({"kind": holder["kind"], "id": holder["id"], "name": name, "scope": scope})
    try:
        if memory.valid_id(pid) and memory.enabled(pid):
            items.append(
                {"kind": "memory", "id": pid, "name": "project memory", "scope": "project"}
            )
    except Exception:  # noqa: BLE001 — unreadable memory state is unknown equipment
        return None
    return sorted(items, key=lambda i: (i["kind"], i["id"], i["name"], i["scope"]))


def _deployment_facts(pid: str) -> dict:
    from . import mission_procedures

    record, _bundle, reason = mission_procedures._deployment(pid)
    out = {"reason": reason, "playbook_id": None, "revision": None, "id": None, "generation": None}
    if record is not None:
        out["playbook_id"] = record.get("playbook_id")
        out["revision"] = (record.get("applied_paths") or {}).get("revision")
        out["id"] = record.get("id")
        out["generation"] = record.get("generation")
    return out


def read_facts(doc: dict) -> Facts:
    """Read, once, the project facts every objective of `doc` needs. Blocking."""
    from . import missions

    projects: dict = {}
    for o in doc["objectives"]:
        pid = o["project_id"]
        entry = projects.setdefault(
            pid,
            {"usable": missions._project_usable(pid), "equipment": equipment_of(pid)},
        )
        if o["procedure"]["source"] == "playbook" and "deployment" not in entry:
            try:
                entry["deployment"] = _deployment_facts(pid)
            except Exception:  # noqa: BLE001 — an unreadable deployment is a change
                # A FIXED text: a refusal is deduplicated on its reasons (`change_digest`).
                entry["deployment"] = {"reason": "the project's deployment cannot be read"}
    return Facts(projects=projects)


def context_for(doc: dict) -> team.Context:
    """P5's own context, able to read every version `doc` pins. Blocking."""
    return team.load_context(doc)


# ---- the pins ------------------------------------------------------------------------------------


def _rework_span(doc: dict, src: str, to: str) -> list[str]:
    """The steps a rework edge `src → to` may run again: `to`, `src`, and every step between them
    (after `to`, before `src`), in plan order."""
    before = team.order_relation(doc)
    return [
        s["id"]
        for s in doc["steps"]
        if s["id"] in (src, to) or (to in before.get(s["id"], ()) and s["id"] in before[src])
    ]


def pins(doc: dict, revision: int, ctx: team.Context, facts: Facts) -> dict:
    """What approving `doc` at `revision` enumerates, or `ApprovalRefused` when it can't be
    enumerated (an unassigned agent step, a missing version, unreadable equipment or deployment).
    Never a grade, a secret value or the plan's free text."""
    versions = team._versions(ctx)
    objectives = []
    for o in doc["objectives"]:
        proc = o["procedure"]
        flow = proc["flow"]
        ppin: dict = {
            "source": proc["source"],
            "flow_id": proc["flow_id"],
            "digest": proc["digest"],
        }
        if proc["source"] == "playbook":
            dep = (facts.projects.get(o["project_id"]) or {}).get("deployment") or {}
            if dep.get("reason"):
                raise ApprovalRefused("deployment_changed", f"objective {o['id']}: {dep['reason']}")
            ppin["playbook_id"] = proc["playbook_id"]
            ppin["revision"] = proc["revision"]
            ppin["deployment"] = [dep.get("id"), dep.get("generation")]
        objectives.append(
            {
                "id": o["id"],
                "project": o["project_id"],
                "procedure": ppin,
                "allowed": {
                    "actors": sorted({s["actor"]["kind"] for s in flow["steps"]}),
                    "probes": sorted(
                        {i["probe"] for s in flow["steps"] for i in s.get("checklist") or []}
                    ),
                },
            }
        )
    steps = []
    unassigned = []
    for s in doc["steps"]:
        if s["actor"] == "agent" and not team.assignable(s):
            # A format ≤ 4 agent step binds no role (#1654), so no specialist can be pinned on it:
            # an approval never covers a step it can't enumerate ("and nothing else").
            raise ApprovalRefused(
                "unassigned",
                f"agent step {s['id']} binds no role (its playbook predates format 5), so it "
                "can't be approved: no specialist can be pinned on it",
            )
        if not team.assignable(s):
            continue
        a = s["envelope"]["assignment"]
        if a is None:
            unassigned.append(s["id"])
            continue
        v = team.version_of(ctx, a["specialist"], versions)
        if v is None:
            raise ApprovalRefused(
                "specialist_archived",
                f"step {s['id']}: {a['specialist']['id']} v{a['specialist']['version']} is gone",
            )
        steps.append(
            {
                "id": s["id"],
                "objective": s["objective"],
                "specialist": dict(a["specialist"]),
                "effective": team.effective_of(a, v),
                "override": a.get("override") is not None,
                "alternates": [dict(x["specialist"]) for x in a.get("alternates") or []],
            }
        )
    if unassigned:
        raise ApprovalRefused(
            "unassigned",
            f"every agent step needs a specialist before approval: {', '.join(unassigned)}",
        )
    equipment = {}
    for pid in sorted({o["project_id"] for o in doc["objectives"]}):
        items = (facts.projects.get(pid) or {}).get("equipment")
        if items is None:
            raise ApprovalRefused(
                "equipment_changed", f"project {pid}'s equipment can't be read; try again"
            )
        equipment[pid] = items
    rework = [
        {
            "from": s["id"],
            "to": s["rework"]["to"],
            "max_rounds": s["rework"]["max_rounds"],
            "steps": _rework_span(doc, s["id"], s["rework"]["to"]),
        }
        for s in doc["steps"]
        if s.get("rework")
    ]
    return {
        "format": PINS_FORMAT,
        "revision": revision,
        "objectives": objectives,
        "steps": steps,
        "equipment": equipment,
        "rework": rework,
    }


# ---- the re-check --------------------------------------------------------------------------------


def _item_text(i: dict) -> str:
    kind = {"secret": "secret", "secret_file": "secret file", "memory": ""}.get(
        i["kind"], i["kind"]
    )
    where = "global, bound here" if i["scope"] == "global" else "this project"
    if i["kind"] == "memory":
        return "project memory"
    return f"{kind} {i['name']} ({where})"


def _equipment_reasons(pid: str, pinned: list[dict], now: list[dict] | None) -> list[dict]:
    if now is None:
        return [_r("equipment_changed", f"project {pid}'s equipment can't be read")]
    if now == pinned:
        return []
    key = lambda i: (i["kind"], i["id"], i["name"], i["scope"])  # noqa: E731
    was, has = {key(i): i for i in pinned}, {key(i): i for i in now}
    added = [_item_text(has[k]) for k in sorted(set(has) - set(was))]
    removed = [_item_text(was[k]) for k in sorted(set(was) - set(has))]
    parts = ([f"added {', '.join(added)}"] if added else []) + (
        [f"removed {', '.join(removed)}"] if removed else []
    )
    return [
        _r(
            "equipment_changed",
            f"project {pid}'s equipment changed: {'; '.join(parts)}",
            old=[_item_text(i) for i in pinned],
            new=[_item_text(i) for i in now],
        )
    ]


def _objective_reasons(opin: dict, facts: Facts) -> list[dict]:
    pid = opin["project"]
    fact = facts.projects.get(pid) or {}
    out: list[dict] = []
    if not fact.get("usable"):
        out.append(_r("project_unavailable", f"project {pid} is archived, gone or unreadable"))
    proc = opin["procedure"]
    if proc["source"] == "playbook":
        dep = fact.get("deployment") or {"reason": "the deployment was not read"}
        if dep.get("reason"):
            out.append(_r("deployment_changed", f"project {pid}: {dep['reason']}"))
        else:
            if (
                dep.get("playbook_id") != proc["playbook_id"]
                or dep.get("revision") != proc["revision"]
            ):
                out.append(
                    _r(
                        "playbook_revision_changed",
                        f"playbook re-deployed: {proc['revision'][:8]} → "
                        f"{str(dep.get('revision'))[:8]}",
                        old=proc["revision"],
                        new=dep.get("revision"),
                    )
                )
            if [dep.get("id"), dep.get("generation")] != proc["deployment"]:
                out.append(
                    _r(
                        "deployment_changed",
                        f"project {pid}'s deployment changed (generation "
                        f"{proc['deployment'][1]} → {dep.get('generation')})",
                        old=proc["deployment"],
                        new=[dep.get("id"), dep.get("generation")],
                    )
                )
    return out


def _step_reasons(spin: dict, doc: dict, ctx: team.Context) -> list[dict]:
    sid = spin["id"]
    pinned = spin["specialist"]
    name = pinned["id"]
    versions = team._versions(ctx)
    cur = versions.get(name)
    out: list[dict] = []
    if cur is None or cur.get("archived"):
        out.append(_r("specialist_archived", f"{name} is archived", step=sid))
    elif cur.get("version") != pinned["version"]:
        # D-P6-1: a newer current version is stale, even though the pinned one stays launchable.
        out.append(
            _r(
                "specialist_version_changed",
                f"{name} changed from v{pinned['version']} to v{cur.get('version')}",
                step=sid,
                old=f"v{pinned['version']}",
                new=f"v{cur.get('version')}",
            )
        )
    step = next((s for s in doc["steps"] if s["id"] == sid), None)
    a = (step or {}).get("envelope", {}).get("assignment")
    v = team.version_of(ctx, pinned, versions)
    if step is None or a is None or v is None:
        if not any(r["code"] == "specialist_archived" for r in out):
            out.append(_r("specialist_archived", f"{name} v{pinned['version']} is gone", step=sid))
        return out
    eff = team.effective_of(a, v)
    if eff != spin["effective"]:
        out.append(
            _r(
                "model_changed",
                f"the model changed from {spin['effective'].get('model')} to {eff.get('model')}",
                step=sid,
                old=spin["effective"],
                new=eff,
            )
        )
    # P5's ONE predicate, on the effective model (#1950: shared by plan write, approval, launch).
    verdict = team.qualify(step, v, eff, ctx)
    for reason in verdict["reasons"]:
        code = _QUALIFY_MAP.get(reason["code"], "model_unavailable")
        if code == "specialist_archived" and any(r["code"] == code for r in out):
            continue
        extra = {}
        if code in ("grade_below_min", "ungraded"):
            extra = {
                "old": (step.get("role") or {}).get("min_grade"),
                "new": team.grade_of(ctx, eff.get("engine") or "", eff.get("model")),
            }
        out.append(_r(code, reason["text"], step=sid, **extra))
    return out


def recheck(
    approval: dict,
    doc: dict,
    revision: int,
    ctx: team.Context,
    facts: Facts,
    *,
    step: str | None = None,
) -> dict:
    """Re-resolve every pin of `approval` against the state now (`doc` is the LATEST plan at
    `revision`). `step` narrows the step checks to one step (its objective and project are always
    checked); None checks the whole plan. `{ok, reasons[]}`; codes from `CODES`."""
    p = approval["pins"]
    reasons: list[dict] = []
    if approval.get("revoked_at") is not None:
        why = approval.get("revoked_reason") or "revoked"
        reasons.append(_r("approval_revoked", f"this approval was {why}"))
    if revision != p["revision"]:
        reasons.append(
            _r(
                "plan_revision_superseded",
                f"the plan moved from revision {p['revision']} to {revision}",
                old=p["revision"],
                new=revision,
            )
        )
        return {"ok": False, "reasons": reasons}
    if step is None:
        steps = list(p["steps"])
        oids = {o["id"] for o in p["objectives"]}
    else:
        here = next((s for s in doc["steps"] if s["id"] == step), None)
        if here is None:
            reasons.append(_r("plan_revision_superseded", f"step {step} is not in this plan"))
            return {"ok": False, "reasons": reasons}
        steps = [s for s in p["steps"] if s["id"] == step]
        oids = {here["objective"]}
        if here["actor"] == "agent" and not steps:
            # Fail closed: an agent step the approval did not enumerate is never covered by it.
            reasons.append(
                _r("approval_revoked", f"this approval does not cover agent step {step}", step=step)
            )
    for opin in p["objectives"]:
        if opin["id"] in oids:
            reasons.extend(_objective_reasons(opin, facts))
    for pid in sorted({o["project"] for o in p["objectives"] if o["id"] in oids}):
        reasons.extend(
            _equipment_reasons(
                pid, p["equipment"].get(pid, []), (facts.projects.get(pid) or {}).get("equipment")
            )
        )
    for spin in steps:
        reasons.extend(_step_reasons(spin, doc, ctx))
    return {"ok": not reasons, "reasons": reasons}


def change_digest(approval: dict, step: str, reasons: list[dict]) -> str:
    """What a refusal is deduplicated on: the approval, the step and exactly what changed."""
    core = [
        [r["code"], r.get("step"), r.get("old"), r.get("new")]
        + ([] if r.get("old") is not None or r.get("new") is not None else [r["text"]])
        for r in reasons
    ]
    return digest([approval["digest"], step, sorted(core, key=json.dumps)])


# ---- the card: the pins in plain words -----------------------------------------------------------


def _session_bounds(p: dict) -> dict[str, int]:
    """Step id → the most sessions it may run under the approval (1 + rounds per rework edge)."""
    out = {s["id"]: 1 for s in p["steps"]}
    for edge in p["rework"]:
        for sid in edge["steps"]:
            if sid in out:
                out[sid] += edge["max_rounds"]
    return out


def lines(p: dict, doc: dict, ctx: team.Context) -> dict:
    """The approval card's server-written lines: per objective the procedure, each agent step's
    specialist, effective model and grade (derived NOW, never pinned) with its bound, the
    equipment, the allowed actions and the rework bounds; then the bounds and what is not
    covered. The client renders these and recomputes nothing."""
    by_step = {s["id"]: s for s in doc["steps"]}
    bounds = _session_bounds(p)
    groups = []
    for o in p["objectives"]:
        proc = o["procedure"]
        rows = []
        if proc["source"] == "playbook":
            rows.append(
                {
                    "key": "procedure",
                    "text": f"{proc['flow_id']} from playbook {proc['playbook_id']} at revision "
                    f"{proc['revision'][:8]}, deployment generation {proc['deployment'][1]}",
                }
            )
        else:
            rows.append(
                {"key": "procedure", "text": f"{proc['flow_id']} (mission-local procedure)"}
            )
        for s in p["steps"]:
            if s["objective"] != o["id"]:
                continue
            eff = s["effective"]
            grade = team.grade_of(ctx, eff.get("engine") or "", eff.get("model"))
            need = ((by_step.get(s["id"]) or {}).get("role") or {}).get("min_grade")
            model = eff.get("model") or model_grades.DEFAULT_MODEL
            text = (
                f"{s['specialist']['id']} v{s['specialist']['version']} · {eff.get('engine')} · "
                f"{model}{' (override)' if s['override'] else ''} · {team.label(grade)}"
            )
            if need:
                text += f" (needs {team.label(need)})"
            alts = ", ".join(f"{a['id']} v{a['version']}" for a in s["alternates"])
            text += f"; alternates: {alts}" if alts else ""
            n = bounds[s["id"]]
            rows.append(
                {
                    "key": s["id"],
                    "text": text,
                    "bound": "1 session" if n == 1 else f"up to 1 + {n - 1}",
                }
            )
        equip = p["equipment"].get(o["project"], [])
        rows.append(
            {
                "key": "equipment",
                "text": (
                    f"{o['project']}: {', '.join(_item_text(i) for i in equip)}. "
                    "Nothing from another project."
                    if equip
                    else f"{o['project']}: none"
                ),
                "bound": f"{len(equip)} item{'s' if len(equip) != 1 else ''}",
            }
        )
        actors = " and ".join(o["allowed"]["actors"])
        probes = " and ".join(o["allowed"]["probes"]) or "none"
        rows.append({"key": "allowed", "text": f"{actors} steps; probes {probes}"})
        for edge in p["rework"]:
            if edge["from"].split(".")[0] != o["id"]:
                continue
            names = ", ".join(e.split(".", 1)[1] for e in edge["steps"])
            rows.append(
                {
                    "key": "rework",
                    "text": f"{edge['from'].split('.', 1)[1]} → {edge['to'].split('.', 1)[1]}, "
                    f"at most {edge['max_rounds']} rounds: {names} may each run up to "
                    f"1 + {edge['max_rounds']} times",
                }
            )
        groups.append({"objective": o["id"], "project": o["project"], "rows": rows})
    total = sum(bounds.values())
    peak = team.peak([s["id"] for s in p["steps"]], team.order_relation(doc))
    gates = [
        s["procedure_step"]
        for s in doc["steps"]
        if s["actor"] == "operator" and s["objective"] in {o["id"] for o in p["objectives"]}
    ]
    not_covered = (
        "The floor (anything irreversible, outward, money or credentials) still asks you, every "
        "time. "
        + (
            f"The playbook's own gates ({', '.join(gates)}) still wait for you. "
            if gates
            else "Operator steps still wait for you. "
        )
        + "HQ never approves a gate, a tool request or a floor item."
    )
    return {
        "groups": groups,
        "bounds": f"Up to {total} sessions in all, up to {min(peak, ctx.pool_max)} at once "
        f"(pool limit {ctx.pool_max}). One session per step run.",
        "not_covered": not_covered,
        "agent_steps": len(p["steps"]),
    }


# ---- standing consent (inert until #1901 / P7b) --------------------------------------------------


def consent_scope(approval: dict, title: str) -> dict:
    """The server's scope lines for a standing consent on `approval`, and their digest. The
    expiry is chosen beside them (1…`CONSENT_DAYS_MAX` days) and stored on the record."""
    p = approval["pins"]
    n_items = sum(len(v) for v in p["equipment"].values())
    projects = ", ".join(sorted(p["equipment"]))
    scope_lines = [
        f"Launch plan revision {p['revision']} of “{title}”, exactly as approved "
        f"(digest {approval['digest'][7:15]}…).",
        f"Only the {len(p['steps'])} agent steps listed, on their pinned specialists and models.",
        f"Only {projects}'s {n_items} equipment item{'s' if n_items != 1 else ''}.",
        "The floor and the playbook's gates still ask you on every run.",
        "Any change to the plan, playbook, specialists or equipment voids this consent.",
    ]
    scope = {"approval_id": approval["id"], "approval_digest": approval["digest"]}
    scope["lines"] = scope_lines
    return {**scope, "digest": digest(scope)}


def consent_verdict(
    consent: dict | None,
    approval: dict | None,
    check: dict | None,
    *,
    now: float | None = None,
) -> dict:
    """Does `consent` cover a launch now? `check` is the approval's `recheck` (None when there is
    no approval). Absent, revoked, expired or resting on another approval refuses; any change the
    re-check finds voids it (#1951 §5)."""
    ts = time.time() if now is None else now
    if consent is None:
        return {
            "ok": False,
            "state": "absent",
            "reasons": [_r("approval_revoked", "no standing consent covers this launch")],
        }
    if consent.get("revoked_at") is not None:
        return {
            "ok": False,
            "state": "revoked",
            "reasons": [_r("approval_revoked", "this standing consent was revoked")],
        }
    if ts >= consent["expires_at"]:
        return {
            "ok": False,
            "state": "expired",
            "reasons": [_r("approval_expired", "this standing consent has expired")],
        }
    if approval is None or approval["id"] != consent["approval_id"]:
        return {
            "ok": False,
            "state": "void",
            "reasons": [_r("approval_revoked", "the approval this consent rests on is gone")],
        }
    if check is None or not check["ok"]:
        return {"ok": False, "state": "void", "reasons": list((check or {}).get("reasons") or [])}
    return {"ok": True, "state": "active", "reasons": []}


# ---- the read: the card, the approval in force, the consent -------------------------------------


def _approval_state(approval: dict, check: dict) -> str:
    if approval.get("revoked_at") is not None:
        return "superseded" if approval.get("revoked_reason") == "superseded" else "revoked"
    return "in_force" if check["ok"] else "stale"


def view(
    mission_id: str,
    title: str,
    latest: dict | None,
    approval: dict | None,
    consent: dict | None,
    ctx: team.Context | None,
    facts: Facts | None,
    *,
    now: float | None = None,
) -> dict:
    """Everything the approval card shows, derived on this read and never stored: what approving
    the latest revision would pin (or why it can't be approved yet), the approval in force with
    its re-check, and the standing consent with its verdict."""
    out: dict = {"mission_id": mission_id, "revision": None, "card": None, "approval": None}
    out["consent"] = None
    out["consent_offer"] = None
    out["consent_days"] = {"default": CONSENT_DAYS_DEFAULT, "max": CONSENT_DAYS_MAX}
    if latest is None or ctx is None or facts is None:
        return out
    doc, revision = latest["document"], latest["revision"]
    out["revision"] = revision
    try:
        p = pins(doc, revision, ctx, facts)
    except ApprovalRefused as e:
        out["card"] = {"ok": False, "code": e.code, "reasons": e.reasons, "digest": None}
    else:
        fresh = {"pins": p, "digest": digest(p), "revoked_at": None}
        check = recheck(fresh, doc, revision, ctx, facts)
        out["card"] = {
            "ok": check["ok"],
            "reasons": check["reasons"],
            "digest": fresh["digest"],
            **lines(p, doc, ctx),
        }
    check = None
    if approval is not None:
        check = recheck(approval, doc, revision, ctx, facts)
        out["approval"] = {
            "id": approval["id"],
            "revision": approval["revision"],
            "digest": approval["digest"],
            "created_at": approval["created_at"],
            "revoked_at": approval["revoked_at"],
            "revoked_reason": approval["revoked_reason"],
            "state": _approval_state(approval, check),
            "reasons": check["reasons"],
            "agent_steps": len(approval["pins"]["steps"]),
        }
        live = approval.get("revoked_at") is None
        if live and check["ok"] and approval["revision"] == revision:
            out["approval"].update(lines(approval["pins"], doc, ctx))
            out["consent_offer"] = consent_scope(approval, title)
    if consent is not None:
        live_approval = approval if approval and approval.get("revoked_at") is None else None
        verdict = consent_verdict(consent, live_approval, check, now=now)
        out["consent"] = {
            "id": consent["id"],
            "approval_id": consent["approval_id"],
            "lines": consent["scope"].get("lines") or [],
            "scope_digest": consent["scope_digest"],
            "expires_at": consent["expires_at"],
            "created_at": consent["created_at"],
            "revoked_at": consent["revoked_at"],
            "state": verdict["state"],
            "reasons": verdict["reasons"],
        }
    return out


# ---- request bodies: exact keys, server-built values only ---------------------------------------


def _bad(text: str) -> ApprovalRefused:
    return ApprovalRefused("invalid_request", text, status=422)


def _exact(body: object, keys: set[str], what: str) -> dict:
    if not isinstance(body, dict) or set(body) != keys:
        raise _bad(f"{what} takes exactly {', '.join(sorted(keys))}")
    return body


def _op_id(raw: object) -> str:
    if not isinstance(raw, str) or not UUID_RE.fullmatch(raw):
        raise _bad("operation_id must be a lowercase UUID")
    return raw


def parse_approve(body: object) -> dict:
    """`{revision, digest, operation_id}`: the revision and digest the card SHOWED."""
    b = _exact(body, {"revision", "digest", "operation_id"}, "approve")
    if type(b["revision"]) is not int or not 0 < b["revision"] < 1_000_000_000:
        raise _bad("revision must be the plan revision the card showed")
    if not isinstance(b["digest"], str) or not DIGEST_RE.fullmatch(b["digest"]):
        raise _bad("digest must be the digest the card showed")
    return {
        "revision": b["revision"],
        "digest": b["digest"],
        "operation_id": _op_id(b["operation_id"]),
    }


def parse_revoke(body: object, key: str, pattern: re.Pattern) -> str:
    b = _exact(body, {key}, "revoke")
    if not isinstance(b[key], str) or not pattern.fullmatch(b[key]):
        raise _bad(f"{key} must name one")
    return b[key]


def parse_consent(body: object) -> dict:
    """`{approval_id, scope_digest, days, operation_id}`: the scope the card showed, an expiry in
    whole days (1…`CONSENT_DAYS_MAX`)."""
    b = _exact(body, {"approval_id", "scope_digest", "days", "operation_id"}, "consent")
    if not isinstance(b["approval_id"], str) or not APPROVAL_ID_RE.fullmatch(b["approval_id"]):
        raise _bad("approval_id must name the approval in force")
    if not isinstance(b["scope_digest"], str) or not DIGEST_RE.fullmatch(b["scope_digest"]):
        raise _bad("scope_digest must be the digest the card showed")
    if type(b["days"]) is not int or not 1 <= b["days"] <= CONSENT_DAYS_MAX:
        raise _bad(f"days must be a whole number from 1 to {CONSENT_DAYS_MAX}")
    return {
        "approval_id": b["approval_id"],
        "scope_digest": b["scope_digest"],
        "days": b["days"],
        "operation_id": _op_id(b["operation_id"]),
    }


# ---- the boundary --------------------------------------------------------------------------------


def admit(
    mission_id: str,
    step: str,
    boundary: str,
    *,
    consent_id: str | None = None,
    now: float | None = None,
) -> dict:
    """THE boundary check (#1951 §2): may `step` of `mission_id` launch (or its gate take effect)
    under the approval in force? Reads the state fresh, re-checks every pin, and on a refusal
    records it once per (step, what changed). Blocking. Returns `{ok, reasons, approval_id,
    new}`; `new` says whether this refusal was not recorded before (the one decision).

    A `consent` boundary must also name a standing consent that still covers the launch."""
    from . import missions

    if boundary not in BOUNDARIES or boundary == "read":
        raise ValueError(f"unknown boundary {boundary!r}")
    approval = missions.approval(mission_id)
    if approval is None or approval.get("revoked_at") is not None:
        text = "no plan approval is in force" if approval is None else "the approval was revoked"
        return {
            "ok": False,
            "reasons": [_r("approval_revoked", text, step=step)],
            "approval_id": None if approval is None else approval["id"],
            "new": False,
        }
    latest = missions.plan_revision(mission_id)
    doc, revision = latest["document"], latest["revision"]
    ctx = context_for(doc)
    facts = read_facts(doc)
    check = recheck(approval, doc, revision, ctx, facts, step=step)
    reasons = list(check["reasons"])
    if boundary == "consent":
        c = missions.consent(mission_id, consent_id) if consent_id else None
        # The consent is checked against the WHOLE plan: any change voids it (#1951 §5).
        full = recheck(approval, doc, revision, ctx, facts)
        verdict = consent_verdict(c, approval, full, now=now)
        if not verdict["ok"]:
            seen = {(r["code"], r.get("step"), r["text"]) for r in reasons}
            reasons += [
                r for r in verdict["reasons"] if (r["code"], r.get("step"), r["text"]) not in seen
            ]
    if not reasons:
        return {"ok": True, "reasons": [], "approval_id": approval["id"], "new": False}
    new = missions.record_refusal(
        mission_id,
        approval_id=approval["id"],
        step=step,
        change_digest=change_digest(approval, step, reasons),
        boundary=boundary,
        reasons=reasons,
        now=now,
    )
    return {"ok": False, "reasons": reasons, "approval_id": approval["id"], "new": new}


__all__ = [
    "BOUNDARIES",
    "CONSENT_DAYS_DEFAULT",
    "CONSENT_DAYS_MAX",
    "CODES",
    "ApprovalRefused",
    "Facts",
    "admit",
    "consent_scope",
    "consent_verdict",
    "digest",
    "lines",
    "pins",
    "read_facts",
    "recheck",
    "view",
]
