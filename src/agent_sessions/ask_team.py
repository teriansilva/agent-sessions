"""HQ plans the team with the Commander (#1950, #1613 P5): HQ's team cards.

HQ reads a mission's team (`read_plan`) and the server's lists for one step (`read_pool`), and
proposes `assemble`, `assign`, `set_override` or `clear_override`. Its writes are operator
approvals (D8: no bypass on the planner):

* **Checked when staged.** Every proposal runs the very same `mission_team.apply_op` against the
  plan revision HQ read, before any card exists. A proposal that fails `qualify()` (the Sonnet
  review, for one) is refused right there with its code and server text, and that refusal is HQ's
  tool result: it never becomes a card.
* **Confirmed on a card.** One that passes is an `ask_operations` card. The operator's tap runs
  `POST /api/missions/{id}/plan/ops` AS the operator (`asgi_call`), with `author: "hq"`, a
  provenance label only. The route checks everything again; a plan that moved since HQ read it is
  `stale` (facts bind the revision) or the route's own 409. An `assemble` sends the team its card
  bound (`expected_team`, #2129) and the route commits only that team.
* **No approve, start or launch.** These four operations are the only team writes HQ has.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

#: The card group. Never voice-confirmed (`voice_calls._voice_operation`): a team card's
#: meaning is its step and model, shown on screen.
GROUP = "team"
#: A card's `operation_id` is derived from its fields' own nonce, so a re-run replays.
_NS = uuid.UUID("6b0d1f4e-1950-4a5e-9c1d-7e5a3c2b1950")

_WHAT = {
    "assemble": "Assemble the team",
    "assign": "Assign a specialist",
    "set_override": "Change a step's model",
    "clear_override": "Clear a step's model override",
}
_VERB = {
    "assemble": "Assemble",
    "assign": "Assign",
    "set_override": "Use this model",
    "clear_override": "Clear override",
}
#: How much of an `assemble` card's sentence its picks may take (#2126): `step → specialist` for
#: as many as fit, the rest counted — the sentence stays under `ask_operations.SUMMARY_MAX`.
TEAM_SHOWN_CHARS = 120
_MORE_RESERVE = 16  # room for " and N more"
#: The fields of a team operation its dry run takes (`mission_team.parse_op` refuses the rest).
_PROBE_KEYS = ("op", "operation_id", "expected_revision", "reason", "step", "index", "listing")


def _body(target: str, f: dict, facts: dict) -> tuple[str, str, dict]:
    body = {
        "op": f["op"],
        "operation_id": f["operation_id"],
        "expected_revision": f["expected_revision"],
        "reason": f["reason"],
        "author": "hq",
    }
    if f["op"] != "assemble":
        body["step"] = f["step"]
    if f["op"] in ("assign", "set_override"):
        body["index"], body["listing"] = f["index"], f["listing"]
    # The team the card bound (#2129): `facts` are the card's STAGED facts (`staged_facts`), so
    # a card staged without a digest (before #2126) sends none and confirms as it did.
    if f["op"] == "assemble" and isinstance(facts.get("team"), str):
        body["expected_team"] = facts["team"]
    return "POST", f"/api/missions/{target}/plan/ops", body


def _team_clause(team: dict | None) -> str:
    """The picks an `assemble` card names (#2126): `step → specialist` for each step the server's
    dry run fills, as many as fit, the rest counted; then what stays and what it cannot fill."""
    if not team:
        return ""
    tail = []
    if team["kept"]:
        tail.append(f"{len(team['kept'])} already assigned stay")
    if team["unassigned"]:
        tail.append(f"{len(team['unassigned'])} cannot be filled")
    tail_text = f" ({'; '.join(tail)})" if tail else ""
    # The picks and the tail share one budget, so the clause stays bounded however many steps.
    # The "and N more" suffix is reserved too, so the whole clause is at most TEAM_SHOWN_CHARS + 2.
    budget = TEAM_SHOWN_CHARS - len(tail_text) - _MORE_RESERVE
    picks = [f"{p['step']} → {p['specialist']}" for p in team["assembled"]]
    shown: list[str] = []
    used = 0
    for pick in picks:
        if not shown and len(pick) > budget:
            shown.append(pick[: budget - 1] + "…")  # the first pick is cut, never over budget
            break
        if shown and used + len(pick) > budget:
            break
        shown.append(pick)
        used += len(pick) + 2
    more = len(picks) - len(shown)
    text = ", ".join(shown) + (f" and {more} more" if more else "")
    return f": {text}{tail_text}"


def _describe(f: dict, facts: dict) -> str:
    rev = f["expected_revision"]
    if f["op"] == "assemble":
        what = "Picks the cheapest qualifying specialist for every agent step that has none"
        what += str(facts.get("picks") or "")
    elif f["op"] == "assign":
        what = f"Puts {f['shown']} on step {f['step']}"
    elif f["op"] == "set_override":
        what = f"Runs step {f['step']} on {f['shown']}, for this mission only"
    else:
        what = f"Puts step {f['step']} back on its specialist's own model"
    moved = "the plan or these picks" if f["op"] == "assemble" else "the plan"
    return (
        f"{what} (plan revision {rev}). It writes a new plan revision and launches nothing; if "
        f"{moved} changed since HQ read it, nothing is written."
    )


#: The route's refusal of an `assemble` whose team is not the card's (#2129): the card is `stale`.
TEAM_CHANGED = "team_changed"


def _outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED, STALE

    if status == 409 and isinstance(body, dict) and body.get("code") == TEAM_CHANGED:
        # Only this code (#2129): another 409 (a moved revision, a reused id) stays `failed`.
        return STALE, "the team it would assemble changed since this was proposed; nothing ran"
    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    return DONE, f"Saved as plan revision {body.get('revision')}."


def _failure(status: int, detail: str, facts: dict) -> str:
    return f"Not changed: {detail or 'refused'}. Nothing was written."


def team_ops() -> tuple:
    from .ask_operations import Operation

    return tuple(
        Operation(
            f"team.{op}",
            GROUP,
            op,
            _WHAT[op],
            _VERB[op],
            "reversible",
            "mission",
            ("op", "operation_id", "expected_revision", "reason", "step", "index", "listing"),
            _body,
            _describe,
            _outcome,
            _failure,
            staged_facts=True,
        )
        for op in ("assemble", "assign", "set_override", "clear_override")
    )


async def facts(target: str, fields: dict, app, operator) -> dict | None:
    """The card's facts, read as the operator: the mission's title and its plan revision, and for
    `assemble` the team it writes (#2126). A plan that moves before the tap makes the card `stale`
    (`ask_operations.changed`: `revision`); so does a team that would come out differently
    (`team`): a plan revision does not freeze the pool, so the tap runs the dry run again over the
    context as it stands, and a pick gone or changed means nothing is written. A change inside
    the confirm request itself, after this check, is the route's to catch (#2129): the tap sends
    this digest as `expected_team` and the route commits only that team, else 409 `team_changed`
    and the card is `stale`. That holds for a card whose facts bound a digest; one staged before
    #2126 sends none, and the operator's own Team card never sends one. A dry run the route would
    now refuse leaves no team."""
    from . import asgi_call, mission_team

    r = await asgi_call.call(app, operator, "GET", f"/api/missions/{target}/plan/team", None)
    if r.status != 200 or not isinstance(r.body, dict) or r.body.get("revision") is None:
        return None
    m = await asgi_call.call(app, operator, "GET", f"/api/missions/{target}", None)
    title = ""
    if m.status == 200 and isinstance(m.body, dict):
        mission = m.body.get("mission") if isinstance(m.body.get("mission"), dict) else m.body
        title = str(mission.get("title") or "")[:120]
    out: dict = {"title": title or target, "revision": int(r.body["revision"])}
    if fields.get("op") == "assemble":
        # Facts hold scalars (`ask_store._admit_values`): the team as its digest (what binds)
        # and the picks as the card's clause (what shows).
        try:
            team = await dry_run(target, {k: fields[k] for k in _PROBE_KEYS[:4]})
        except mission_team.TeamRefused:
            out["team"] = None
        else:
            out["team"], out["picks"] = team_digest(team), _team_clause(team)
    return out


def team_digest(team: dict) -> str:
    """One team as a short, comparable string (#2126): the same picks give the same digest."""
    canon = json.dumps(team, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(canon).hexdigest()[:16]


def written_team(before: dict, after: dict, ctx) -> str:
    """The digest of the team `after` writes over `before` (#2129): THE one comparison, made the
    same way for the card (`facts`, through `dry_run`) and for the route, which checks an
    `assemble`'s `expected_team` against the exact document it is about to commit."""
    return team_digest(_preview(before, after, ctx))


def operation_id(nonce: str) -> str:
    return str(uuid.uuid5(_NS, nonce))


def _preview(before: dict, after: dict, ctx) -> dict:
    """The team the operation writes (#2126), `after` against `before`: the steps it fills
    (`assembled`: the specialist and version it pins and the effective engine · model), the steps
    that keep their specialist (`kept`) and the agent steps left without one (`unassigned`, with
    the server's own summary). Identifiers and server text only — the card binds it (`facts`) and
    HQ quotes it, so what HQ says is what the tap writes, never its own reading of `read_pool`."""
    from . import mission_team

    was = mission_team.assignments_of(before)
    assembled: list[dict] = []
    kept: list[dict] = []
    unassigned: list[dict] = []
    for row in mission_team.view(after, ctx)["steps"]:
        sid = row["id"]
        a = row.get("assignment")
        if a is None:
            if row.get("unassigned"):
                why = str(row["unassigned"].get("summary") or "no specialist qualifies")
                unassigned.append({"step": sid, "why": why[: mission_team.REASON_MAX]})
            continue
        eff = (row.get("derived") or {}).get("effective") or {}
        entry = {
            "step": sid,
            "specialist": a["specialist"]["id"],
            "version": a["specialist"]["version"],
            "model": f"{eff.get('engine')} · {eff.get('model') or 'default'}",
        }
        (kept if was.get(sid) is not None else assembled).append(entry)
    return {"assembled": assembled, "kept": kept, "unassigned": unassigned}


def _dry_run(mission_id: str, op: dict) -> dict:
    """`dry_run`, blocking."""
    from . import mission_team, missions

    latest = missions.plan_revision(mission_id)
    if latest is None:
        raise mission_team.TeamRefused(
            "no_plan", "this mission has no plan yet: apply a procedure first", status=409
        )
    if latest["revision"] != op["expected_revision"]:
        raise mission_team.TeamRefused(
            "stale",
            f"the plan is at revision {latest['revision']} now: read it again",
            status=409,
        )
    parsed = mission_team.parse_op(op)
    doc = latest["document"]
    ctx = mission_team.load_context(doc)
    new = mission_team.apply_op(
        doc, parsed, ctx, mission_id=mission_id, revision=latest["revision"]
    )
    return _preview(doc, new, ctx)


async def dry_run(mission_id: str, op: dict) -> dict:
    """Run the operation against the revision it names, writing nothing: `TeamRefused` (with its
    code and server text) when the route would refuse it; otherwise the team it would write
    (`_preview`). Off the loop."""
    return await asyncio.to_thread(_dry_run, mission_id, op)
