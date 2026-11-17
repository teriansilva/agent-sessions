"""HQ proposes a DRAFT mission (#1950): the card that makes "plan me a mission" work in one chat.

HQ picks the project (by the index `list_projects` returned this turn), writes the instruction and,
when the project has a deployed playbook, names the procedure to apply (by the index
`list_procedures` returned this turn, or the only ready one). The card is ``mission.create``, an
ordered runner (`Operation.run`) of two routes, each AS the operator (`asgi_call`):

1. ``POST /api/missions``, the very route the Missions rail's "New mission" form posts to. The
   server creates a **draft** (its `cwd` resolved from the project, never sent).
2. ``POST /api/missions/{id}/procedure {index, listing}``: the project's deployed procedure becomes
   the plan's first objective, so the plan revision has the flow's steps and roles (#1777). The
   procedure is named only by its index and the listing digest; no input is ever sent, so every
   probe target is the deployment's reviewed one (`mission_procedures`: a choosable branch takes
   its deployed value, an upstream output waits as a pending gate, D-P4-3).

* **A draft starts nothing.** Approve, Start and launch stay the operator's, in the console
  (**Begin**). This operation and the team cards are the only mission writes HQ has.
* **Checked when staged.** The project must exist, not be archived and have a folder, and a named
  procedure must be ready; else the proposal is refused right there and HQ gets the refusal.
* **Bound.** The project's name and archive state, the listing digest and the procedure's flow id
  are the card's facts: a project renamed or archived, or a playbook redeployed, before the tap
  makes the card ``stale`` and nothing is created. A listing that moves between the two routes is
  the apply route's own 409: the card is ``partly_done`` and says the procedure was not applied.
* **HQ continues** (`follow_up`): a ``done`` card starts one continuation turn in the conversation
  (`ask_orchestrator.continue_after_card`), which knows the new mission's id from this runner.
"""

from __future__ import annotations

import asyncio
import contextlib

GROUP = "mission"
#: What the card can show whole: the summary sentence is capped (`ask_operations.SUMMARY_MAX`) and
#: a card that would be cut is refused, so the instruction is bounded well inside that.
INSTRUCTION_MAX = 240
#: The procedure title a card binds (the summary may cut it further, `_describe`).
PROCEDURE_TITLE_MAX = 80
#: The `procedure` field of a draft that applies none (the project has no deployed playbook).
NO_PROCEDURE = -1


def instruction(v: object) -> str:
    from .ask_operations import Refused

    if not isinstance(v, str):
        raise Refused("instruction must be a string")
    text = " ".join(v.split())
    if not text:
        raise Refused("instruction must not be empty")
    if len(text) > INSTRUCTION_MAX:
        raise Refused(
            f"instruction is longer than {INSTRUCTION_MAX} characters: shorten it; the operator "
            "adds detail in the mission itself"
        )
    return text


def _procedure(f: dict) -> int:
    """The procedure index a card applies, or NO_PROCEDURE (also for a card staged before #1950's
    second round, which carried no such field)."""
    v = f.get("procedure", NO_PROCEDURE)
    return v if type(v) is int and v >= 0 else NO_PROCEDURE


def _body(target: str, f: dict, facts: dict) -> tuple[str, str, dict]:
    # `project_id` is the server's own id for a project HQ named by index; `cwd` is never sent.
    return "POST", "/api/missions", {"instruction": f["instruction"], "project_id": target}


def _describe(f: dict, facts: dict) -> str:
    from .ask_operations import SUMMARY_MAX

    head = f"Creates a draft mission: \"{f['instruction']}\". "
    tail = " Nothing runs until you press Begin."
    if _procedure(f) != NO_PROCEDURE and facts.get("procedure"):
        # The title is cut (never the instruction) to keep the whole sentence on the card: a
        # summary past SUMMARY_MAX would be refused at staging.
        frame = 'Applies the playbook procedure "{}": the plan gets its steps and roles.'
        room = SUMMARY_MAX - len(head) - len(tail) - len(frame.format(""))
        title = str(facts["procedure"])
        if len(title) > room:
            title = title[: max(room - 1, 1)] + "…"
        what = frame.format(title)
    else:
        what = "The project has no deployed playbook, so no procedure is applied."
    return f"{head}{what}{tail}"


def _title(body: object) -> str:
    return str((body or {}).get("title") or "")[:120] if isinstance(body, dict) else ""


def _outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    """The create's own outcome (the route's 2xx), used when no procedure follows."""
    from .ask_operations import DONE, FAILED

    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    return DONE, (
        f"Draft mission created ({_title(body)}) with no procedure: the project has no deployed "
        "playbook. Nothing runs until you press Begin."
    )


def _failure(status: int, detail: str, facts: dict) -> str:
    return f"Not created: {detail or 'refused'}. Nothing was written."


async def _run(cid: str, target: str, fields: dict, facts: dict, app, operator) -> dict:
    """Create the draft, then apply its procedure: two routes, in order, as the operator. Never
    retried or resumed: an interrupted run is reported (`interrupted`), never replayed."""
    from . import asgi_call, ask_proposals, missions
    from .ask_operations import DONE, FAILED, PARTLY_DONE, UNCONFIRMED, UNCONFIRMED_SAID

    method, path, body = _body(target, fields, facts)
    r = await asgi_call.call(app, operator, method, path, body)
    if r.truncated and 200 <= r.status < 300:
        return {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID}
    if not 200 <= r.status < 300 or not isinstance(r.body, dict):
        return {"state": FAILED, "outcome": _failure(r.status, r.detail, facts)}
    mid = r.body.get("id")
    if not (isinstance(mid, str) and missions.MISSION_ID_RE.match(mid)):
        return {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID}
    pid = fields.get("_pid")
    if isinstance(pid, str) and pid:
        # Noted on the entry at once: a stop during the apply (the entry then reads
        # `interrupted`) still names the draft that exists.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ask_proposals._update, cid, pid, mission_id=mid)
    title = _title(r.body)
    index = _procedure(fields)
    if index == NO_PROCEDURE:
        state, said = _outcome(r.status, r.body, facts)
        return {"state": state, "outcome": said, "mission_id": mid}
    proc = facts.get("procedure") or "the procedure"
    try:
        p = await asgi_call.call(
            app,
            operator,
            "POST",
            f"/api/missions/{mid}/procedure",
            {"index": index, "listing": facts.get("listing") or ""},
        )
    except Exception as e:  # noqa: BLE001 — the draft exists: the card must still name it
        return {
            "state": PARTLY_DONE,
            "outcome": f'Draft mission created ({title}), but applying the procedure "{proc}" '
            f"stopped unexpectedly ({type(e).__name__}); it may not be applied. Check the "
            "mission. Nothing runs until you press Begin.",
            "mission_id": mid,
        }
    if 200 <= p.status < 300 and isinstance(p.body, dict) and not p.truncated:
        rev = p.body.get("revision")
        return {
            "state": DONE,
            "outcome": f'Draft mission created ({title}) with the procedure "{proc}" as plan '
            f"revision {rev}. Nothing runs until you press Begin.",
            "mission_id": mid,
        }
    if 200 <= p.status < 300:
        return {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID, "mission_id": mid}
    return {
        "state": PARTLY_DONE,
        "outcome": f'Draft mission created ({title}), but the procedure "{proc}" was not '
        f"applied: {p.detail or 'refused'}. Nothing runs until you press Begin.",
        "mission_id": mid,
    }


def mission_ops() -> tuple:
    from .ask_operations import Operation

    return (
        Operation(
            "mission.create",
            GROUP,
            "create",
            "Create a draft mission",
            "Create draft",
            "reversible",
            "project",
            ("instruction", "procedure"),
            _body,
            _describe,
            _outcome,
            _failure,
            run=_run,
            follow_up=True,
        ),
    )


async def _row(target: str, app, operator) -> dict | None:
    from . import asgi_call

    r = await asgi_call.call(
        app, operator, "GET", "/api/projects", None, query="include_archived=1"
    )
    rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
    return next((p for p in rows or [] if isinstance(p, dict) and p.get("id") == target), None)


async def refusal(target: str, app, operator) -> str:
    """Why the route would refuse a draft in this project, or "" (the dry run at staging): the
    project is gone, archived, or has no folder for the draft's path (`_resolve_cwd`)."""
    p = await _row(target, app, operator)
    if p is None:
        return "that project is no longer available"
    if p.get("archived"):
        return "that project is archived"
    if not [f for f in p.get("folders") or [] if isinstance(f, str) and f]:
        return "that project has no folder to work in: the operator adds one in the project"
    return ""


async def listing(project_id: str) -> dict:
    """The project's deployed procedures, by index, with the listing digest (off the loop)."""
    from . import mission_procedures

    return await asyncio.to_thread(mission_procedures.project_listing, project_id)


async def facts(target: str, fields: dict, app, operator) -> dict | None:
    """The project the draft goes into, as the operator sees it (its name and archive state), and
    the procedure the card applies: the listing digest it was chosen from, its flow id and title.
    ``None`` (nothing staged) for a project that is gone or archived. A listing that moved shows
    as a different digest, so `ask_operations.changed` makes the card stale."""
    from . import ask_operations, missions

    p = await _row(target, app, operator)
    if p is None or p.get("archived"):
        return None
    out = {"name": str(p.get("name") or "")[: ask_operations.TITLE_MAX], "archived": False}
    index = _procedure(fields)
    if "procedure" not in fields:
        return out  # a card staged before procedures were applied binds the project only
    try:
        now = await listing(target)
    except missions.MissionError:
        now = {"listing": "", "procedures": []}
    out["listing"] = str(now.get("listing") or "")
    rows = now.get("procedures") or []
    row = rows[index] if 0 <= index < len(rows) else None
    out["flow"] = str(row["flow_id"]) if row else ""
    out["procedure"] = str(row["title"])[:PROCEDURE_TITLE_MAX] if row else ""
    return out
