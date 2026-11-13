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
  `stale` (facts bind the revision) or the route's own 409.
* **No approve, start or launch.** These four operations are the only team writes HQ has.
"""

from __future__ import annotations

import asyncio
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
    return "POST", f"/api/missions/{target}/plan/ops", body


def _describe(f: dict, facts: dict) -> str:
    rev = f["expected_revision"]
    if f["op"] == "assemble":
        what = "Picks a specialist for every agent step that has none, the cheapest that qualifies"
    elif f["op"] == "assign":
        what = f"Puts {f['shown']} on step {f['step']}"
    elif f["op"] == "set_override":
        what = f"Runs step {f['step']} on {f['shown']}, for this mission only"
    else:
        what = f"Puts step {f['step']} back on its specialist's own model"
    return (
        f"{what} (plan revision {rev}). It writes a new plan revision and launches nothing; if "
        "the plan changed since HQ read it, nothing is written."
    )


def _outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED

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
        )
        for op in ("assemble", "assign", "set_override", "clear_override")
    )


async def facts(target: str, app, operator) -> dict | None:
    """The card's facts, read as the operator: the mission's title and its plan revision. A plan
    that moves before the tap makes the card `stale` (`ask_operations.changed`: `revision`)."""
    from . import asgi_call

    r = await asgi_call.call(app, operator, "GET", f"/api/missions/{target}/plan/team", None)
    if r.status != 200 or not isinstance(r.body, dict) or r.body.get("revision") is None:
        return None
    m = await asgi_call.call(app, operator, "GET", f"/api/missions/{target}", None)
    title = ""
    if m.status == 200 and isinstance(m.body, dict):
        mission = m.body.get("mission") if isinstance(m.body.get("mission"), dict) else m.body
        title = str(mission.get("title") or "")[:120]
    return {"title": title or target, "revision": int(r.body["revision"])}


def operation_id(nonce: str) -> str:
    return str(uuid.uuid5(_NS, nonce))


async def dry_run(mission_id: str, op: dict) -> None:
    """Run the operation against the revision it names, writing nothing: `TeamRefused` (with its
    code and server text) when the route would refuse it. Off the loop."""
    from . import mission_team, missions

    def check() -> None:
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
        mission_team.apply_op(doc, parsed, ctx, mission_id=mission_id, revision=latest["revision"])

    await asyncio.to_thread(check)
