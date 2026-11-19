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

**HQ is the only planner (#2152).** The one-agent planner is retired, so HQ always plans with a
flow:

* ``mission.create_local`` — no deployed procedure fits (or the project has none), so HQ WROTE a
  format-5 flow for this mission. The flow is the card's draft (``Operation.draft``, #1611's
  mechanism): stored server-side, bound by its sha256, shown on the card (every step, its role's
  skills and minimum grade, the operator steps), and reaching the runner only after it hashed
  the same at the tap. Staging runs `mission_procedures.check_local` (the apply's own resolution,
  writing nothing) and checks every role's skill is in the Library; the card binds the check's
  digest. The tap checks both AGAIN before anything is created (stale, nothing written), creates
  the draft, then applies the flow through ``POST …/procedure {mission_local}``, which runs
  `apply_local` a third time.
* ``mission.plan`` — plan an EXISTING draft (D4): the operator's Plan with HQ binds the
  conversation to it. One route, ``POST …/procedure`` with ``if_unplanned``, deployed
  (`{index, listing}`) or mission-local; it creates nothing, keeps the goal and history, and is
  refused unless the mission is a draft with no plan, at staging, at the tap and inside the
  store's transaction (`missions.apply_procedure(require_unplanned=…)`).
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
#: The `procedure` field of a draft that applies none (a card staged before #2152: HQ now writes a
#: flow when the project has no deployed playbook).
NO_PROCEDURE = -1
#: The largest `propose_mission` call HQ may make (its flow rides in the arguments).
FLOW_ARGS_MAX = 32 * 1024


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


def _describe_local(f: dict, facts: dict) -> str:
    return (
        f"Creates a draft mission: \"{f['instruction']}\". Applies the flow HQ wrote for it, "
        "since no deployed procedure fits: the plan gets its steps and roles. Nothing runs until "
        "you approve the plan."
    )


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


async def _create(cid: str, target: str, fields: dict, facts: dict, app, operator):
    """``POST /api/missions`` as the operator: ``(mission_id, title, None)``, or
    ``(None, "", outcome)`` when the create did not (provably) happen. The new id is noted on the
    entry at once, so a stop during what follows (the entry then reads `interrupted`) still names
    the draft that exists."""
    from . import asgi_call, ask_proposals, missions
    from .ask_operations import FAILED, UNCONFIRMED, UNCONFIRMED_SAID

    method, path, body = _body(target, fields, facts)
    r = await asgi_call.call(app, operator, method, path, body)
    if r.truncated and 200 <= r.status < 300:
        return None, "", {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID}
    if not 200 <= r.status < 300 or not isinstance(r.body, dict):
        return None, "", {"state": FAILED, "outcome": _failure(r.status, r.detail, facts)}
    mid = r.body.get("id")
    if not (isinstance(mid, str) and missions.MISSION_ID_RE.match(mid)):
        return None, "", {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID}
    pid = fields.get("_pid")
    if isinstance(pid, str) and pid:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ask_proposals._update, cid, pid, mission_id=mid)
    return mid, _title(r.body), None


async def _apply(app, operator, mid: str, body: dict, title: str, proc: str) -> dict:
    """``POST /api/missions/{mid}/procedure`` as the operator, after the create: every outcome
    names the draft that already exists (never "nothing was written", never a second draft)."""
    from . import asgi_call
    from .ask_operations import DONE, PARTLY_DONE, UNCONFIRMED, UNCONFIRMED_SAID

    try:
        p = await asgi_call.call(app, operator, "POST", f"/api/missions/{mid}/procedure", body)
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


async def _run(cid: str, target: str, fields: dict, facts: dict, app, operator) -> dict:
    """Create the draft, then apply its procedure: two routes, in order, as the operator. Never
    retried or resumed: an interrupted run is reported (`interrupted`), never replayed."""
    mid, title, settled = await _create(cid, target, fields, facts, app, operator)
    if settled is not None:
        return settled
    index = _procedure(fields)
    if index == NO_PROCEDURE:
        # A card staged before #2152 (HQ now writes a flow when there is no deployment).
        state, said = _outcome(201, {"title": title}, facts)
        return {"state": state, "outcome": said, "mission_id": mid}
    proc = facts.get("procedure") or "the procedure"
    return await _apply(
        app, operator, mid, {"index": index, "listing": facts.get("listing") or ""}, title, proc
    )


# ---- HQ's own flow, and planning an existing draft (#2152) -------------------------------------


def _skills_in(flow: dict) -> list[str]:
    out: set[str] = set()
    for step in flow.get("steps") or []:
        role = step.get("role") or {}
        out.update(x for x in role.get("skills") or [] if isinstance(x, str))
    return sorted(out)


def check_flow(project_id: str, flow_id: object, doc: object) -> dict:
    """HQ's flow for ``project_id``, checked WITHOUT writing (D6): `mission_procedures.check_local`
    (the apply's own resolution) plus every role's skill in the Library (D3). Returns
    `{flow, digest, skills}`; raises `ask_operations.Refused` with the reason. BLOCKING."""
    from . import mission_procedures, missions, specialists
    from .ask_operations import Refused

    try:
        checked = mission_procedures.check_local(project_id, flow_id, doc)
    except missions.MissionError as e:
        code = "bad_arguments" if e.status == 422 else "not_available"
        raise Refused(f"refused: {e}. Nothing was written", code) from None
    skills = _skills_in(checked["flow"])
    try:
        library = specialists.library_revisions()
    except Exception:  # noqa: BLE001 — an unreadable library proves nothing exists
        raise Refused(
            "refused: the skills library can't be read just now. Nothing was written",
            "not_available",
        ) from None
    # NOTHING REACHES AN AGENT THAT THE CARD DOES NOT SHOW (#2152 review): a step's `prompts` are
    # composed into its instruction (`mission_steps.compose_brief`), so a flow HQ writes may not
    # carry them; its instruction is the step's `brief`, which the card shows whole.
    prompted = [str(st.get("id")) for st in checked["flow"]["steps"] if st.get("prompts")]
    if prompted:
        raise Refused(
            "refused: a flow you write gives each step its instruction in its brief, which the "
            f"card shows in full; step prompts are not accepted (step {', '.join(prompted)}). "
            "Nothing was written",
            "bad_arguments",
        )
    missing = [x for x in skills if x not in library]
    if missing:
        raise Refused(
            f"refused: the Library has no skill {', '.join(missing)}: a role names only skills "
            "list_skills returns. Nothing was written",
            "unknown_skill",
        )
    return {**checked, "skills": ",".join(skills)}


def _steps_view(flow: dict) -> list[dict]:
    """What the card shows of a flow, from the validated flow: each step, who does it, an agent
    step's role (skills and MINIMUM grade), and EVERYTHING of it that reaches the agent's
    instruction (`mission_steps.compose_brief`), VERBATIM and never cut: its title, its brief, its
    prompts (a deployed procedure's; HQ's own flow may carry none, `check_flow`) and its
    checklist's titles. Also the operator-relevant constraints: its memory mode, its outputs and
    the steps it must not share a specialist with."""
    from .mission_team import GRADE_LABEL

    titles = {str(st.get("id")): str(st.get("title") or "") for st in flow.get("steps") or []}
    out = []
    for st in flow.get("steps") or []:
        kind = str((st.get("actor") or {}).get("kind") or "")
        role = st.get("role") or {}
        out.append(
            {
                "id": str(st.get("id") or ""),
                "title": str(st.get("title") or ""),
                "brief": str(st.get("brief") or ""),
                "prompts": [
                    str(p.get("body") or "") for p in st.get("prompts") or [] if isinstance(p, dict)
                ],
                "checklist": [
                    str(c.get("title") or c.get("key") or "")
                    for c in st.get("checklist") or []
                    if isinstance(c, dict)
                ],
                "actor": kind,
                "skills": [str(x) for x in role.get("skills") or []] if kind == "agent" else [],
                "min_grade": GRADE_LABEL.get(str(role.get("min_grade") or ""), "")
                if kind == "agent"
                else "",
                "memory": str(st.get("memory") or "none"),
                "outputs": [str(o) for o in st.get("outputs") or []],
                "distinct_from": [
                    titles.get(str(d.get("step")), str(d.get("step")))
                    for d in st.get("distinct_from") or []
                    if isinstance(d, dict)
                ],
                "rework": (st.get("rework") or {}).get("max_rounds"),
            }
        )
    return out


def _flow_args(args: dict) -> tuple[str, object]:
    from .ask_operations import Refused

    fid, doc = args.get("flow_id"), args.get("flow")
    if not isinstance(fid, str) or not fid:
        raise Refused("refused: a flow needs its flow_id (lowercase letters, digits, -)")
    if not isinstance(doc, dict):
        raise Refused("refused: flow is the format 5 flow document, an object")
    return fid, doc


async def local_proposal(project_id: str, args: dict) -> tuple[dict, dict]:
    """The fields and the draft of a card whose flow HQ wrote: staged only once the flow checks
    clean (an invalid flow, an unusable deployment or binding, a default-derived target, or a
    skill missing from the Library is HQ's refusal, never a card)."""
    fid, doc = _flow_args(args)
    checked = await asyncio.to_thread(check_flow, project_id, fid, doc)
    draft = {
        "source": "mission_local",
        "flow_id": fid,
        "flow": doc,
        "title": str(checked["flow"].get("title") or fid),
        "steps": _steps_view(checked["flow"]),
    }
    return {
        "procedure": NO_PROCEDURE,
        "local": checked["digest"],
        "skills": checked["skills"],
    }, draft


async def deployed_draft(project_id: str, index: int, listing_digest: str) -> dict:
    """The draft a `mission.plan` card SHOWS for a deployed procedure (its steps and roles), from
    the listing HQ's pick was validated against (another one is a refusal, never a card)."""
    from . import mission_procedures, missions
    from .ask_operations import Refused

    try:
        got = await asyncio.to_thread(mission_procedures.procedure_flow, project_id, index)
    except missions.MissionError as e:
        raise Refused(f"refused: {e}. Nothing was written", "not_available") from None
    if got["listing"] != listing_digest:
        raise Refused(
            "refused: the project's procedures changed while the card was prepared; read them "
            "again with list_procedures. Nothing was written",
            "stale",
        )
    return {
        "source": "playbook",
        "flow_id": got["flow_id"],
        "title": str(got["flow"].get("title") or got["flow_id"]),
        "playbook": str(got["playbook"])[:PROCEDURE_TITLE_MAX],
        "steps": _steps_view(got["flow"]),
    }


def content_view(draft: dict) -> dict:
    """What a mission card shows of its draft, for the operator's browser only."""
    steps = draft.get("steps") if isinstance(draft.get("steps"), list) else []
    return {
        "kind": "flow",
        "source": "playbook" if draft.get("source") == "playbook" else "mission_local",
        "title": str(draft.get("title") or ""),
        "playbook": str(draft.get("playbook") or "") or None,
        "steps": [s for s in steps if isinstance(s, dict)],
    }


async def _recheck(project_id: str, fields: dict) -> str:
    """At the tap (D6): HQ's flow checked AGAIN before anything is written. ``""`` when it still
    resolves to exactly what the card bound, else why not."""
    from .ask_operations import Refused

    content = fields.get("_content") or {}
    try:
        now = await asyncio.to_thread(
            check_flow, project_id, content.get("flow_id"), content.get("flow")
        )
    except Refused as e:
        return str(e).removeprefix("refused: ").removesuffix(". Nothing was written")
    if now["digest"] != fields.get("local") or now["skills"] != fields.get("skills"):
        return "the project's bindings, deployment or skills changed"
    return ""


async def _run_local(cid: str, target: str, fields: dict, facts: dict, app, operator) -> dict:
    """``mission.create_local``: the flow re-checked, the draft created, the flow applied."""
    from .ask_operations import STALE

    why = await _recheck(target, fields)
    if why:
        return {"state": STALE, "reason": f"{why} since this was proposed; nothing ran"}
    mid, title, settled = await _create(cid, target, fields, facts, app, operator)
    if settled is not None:
        return settled
    content = fields["_content"]
    body = {"mission_local": {"flow_id": content["flow_id"], "flow": content["flow"]}}
    return await _apply(app, operator, mid, body, title, str(content.get("title") or ""))


def _mission_facts(mid: str) -> dict | None:
    """A mission as a `mission.plan` card binds it, or None when it is gone. BLOCKING."""
    from . import missions

    m = missions.get_mission(mid, events_limit=1)
    if m is None:
        return None
    rev = missions.latest_plan_revision(mid)
    return {
        "title": str(m.get("title") or ""),
        "state": str(m.get("state") or ""),
        "archived": m.get("archived_at") is not None or m.get("archiving_at") is not None,
        "planned": rev is not None or missions.get_plan(mid) is not None,
        "project_id": str(m.get("project_id") or ""),
    }


async def plan_target(mid: str) -> tuple[str, str]:
    """``(project_id, title)`` of a mission HQ may plan (D4): a draft, not archived, in a project,
    with no plan revision and no legacy plan. Raises `ask_operations.Refused` with the reason."""
    from . import missions
    from .ask_operations import Refused

    m = await missions.run_admitted(lambda: _mission_facts(mid))
    why = (
        "that mission no longer exists"
        if m is None
        else "that mission is archived"
        if m["archived"]
        else f"that mission is {m['state']}: only a draft is planned"
        if m["state"] != "draft"
        else "that mission already has a plan: the operator reviews its Team and Plan approval"
        if m["planned"]
        else "that mission has no project: the operator chooses one first"
        if not m["project_id"]
        else ""
    )
    if why or m is None:
        raise Refused(f"refused: {why}. Nothing was written", "not_available")
    return m["project_id"], m["title"]


async def _plan_facts(mid: str, fields: dict) -> dict | None:
    from . import ask_operations, missions

    m = await missions.run_admitted(lambda: _mission_facts(mid))
    if m is None or m["archived"]:
        return None
    out = {
        "title": m["title"][: ask_operations.TITLE_MAX],
        "archived": False,
        "state": m["state"],
        "planned": m["planned"],
    }
    index = _procedure(fields)
    if index == NO_PROCEDURE:
        return out  # HQ's own flow: re-checked by the runner (`_recheck`)
    try:
        now = await listing(m["project_id"])
    except missions.MissionError:
        now = {"listing": "", "procedures": []}
    rows = now.get("procedures") or []
    row = rows[index] if 0 <= index < len(rows) else None
    out["listing"] = str(now.get("listing") or "")
    out["flow"] = str(row["flow_id"]) if row else ""
    out["procedure"] = str(row["title"])[:PROCEDURE_TITLE_MAX] if row else ""
    return out


def _plan_describe(f: dict, facts: dict) -> str:
    from .ask_operations import SUMMARY_MAX

    tail = (
        " Its goal and history stay, and no second mission is created. Nothing runs until you "
        "approve the plan."
    )
    if _procedure(f) == NO_PROCEDURE:
        what = "Applies the flow HQ wrote for it: the plan gets its steps and roles."
    else:
        frame = 'Applies the playbook procedure "{}": the plan gets its steps and roles.'
        room = SUMMARY_MAX - len(tail) - len(frame.format("")) - 60
        title = str(facts.get("procedure") or "")
        if len(title) > room:
            title = title[: max(room - 1, 1)] + "…"
        what = frame.format(title)
    return f"Plans this draft mission. {what}{tail}"


def _plan_outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
    from .ask_operations import DONE, FAILED

    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    return DONE, (
        f"Planned: the procedure is plan revision {body.get('revision')}. Its goal and history "
        "are kept. Nothing runs until you approve the plan."
    )


def _plan_failure(status: int, detail: str, facts: dict) -> str:
    return f"Not planned: {detail or 'refused'}. Nothing was written."


def _plan_body(target: str, f: dict, facts: dict) -> tuple[str, str, dict]:
    return "POST", f"/api/missions/{target}/procedure", {}


async def _run_plan(cid: str, target: str, fields: dict, facts: dict, app, operator) -> dict:
    """``mission.plan``: ONE route on the existing draft, ``if_unplanned`` so the store refuses
    inside its transaction when a plan landed after the card was staged. Creates nothing."""
    from . import asgi_call, missions
    from .ask_operations import FAILED, STALE, UNCONFIRMED, UNCONFIRMED_SAID

    index = _procedure(fields)
    try:
        scope = await missions.run_admitted(lambda: missions.procedure_scope(target))
    except missions.MissionError:
        return {
            "state": STALE,
            "reason": "it no longer exists since this was proposed; nothing ran",
        }
    # The project, as at staging (#2152 review): gone, archived or without a folder is no plan.
    gone = await refusal(str(scope.get("project_id") or ""), app, operator)
    if gone:
        return {"state": STALE, "reason": f"{gone} since this was proposed; nothing ran"}
    if index == NO_PROCEDURE:
        why = await _recheck(str(scope.get("project_id") or ""), fields)
        if why:
            return {"state": STALE, "reason": f"{why} since this was proposed; nothing ran"}
        content = fields["_content"]
        body: dict = {"mission_local": {"flow_id": content["flow_id"], "flow": content["flow"]}}
    else:
        body = {"index": index, "listing": facts.get("listing") or ""}
    body["if_unplanned"] = True
    try:
        r = await asgi_call.call(app, operator, "POST", f"/api/missions/{target}/procedure", body)
    except Exception as e:  # noqa: BLE001 — it may have landed: say so, never "nothing"
        return {
            "state": FAILED,
            "outcome": f"Planning stopped unexpectedly ({type(e).__name__}); it may have been "
            "applied. Check the mission.",
            "mission_id": target,
        }
    if r.truncated and 200 <= r.status < 300:
        return {"state": UNCONFIRMED, "outcome": UNCONFIRMED_SAID, "mission_id": target}
    state, said = _plan_outcome(r.status, r.body, facts)
    if state == FAILED:
        said = _plan_failure(r.status, r.detail, facts)
    return {"state": state, "outcome": said, "mission_id": target}


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
        Operation(
            "mission.create_local",
            GROUP,
            "create_local",
            "Create a draft mission",
            "Create draft",
            "reversible",
            "project",
            ("instruction",),
            _body,
            _describe_local,
            _outcome,
            _failure,
            draft=True,
            run=_run_local,
            follow_up=True,
        ),
        Operation(
            "mission.plan",
            GROUP,
            "plan",
            "Plan this mission",
            "Apply to this mission",
            "reversible",
            "mission",
            (),
            _plan_body,
            _plan_describe,
            _plan_outcome,
            _plan_failure,
            draft=True,
            run=_run_plan,
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


async def facts(op_id: str, target: str, fields: dict, app, operator) -> dict | None:
    """The project the draft goes into, as the operator sees it (its name and archive state), and
    the procedure the card applies: the listing digest it was chosen from, its flow id and title.
    ``None`` (nothing staged) for a project that is gone or archived. A listing that moved shows
    as a different digest, so `ask_operations.changed` makes the card stale."""
    from . import ask_operations, missions

    if op_id == "mission.plan":
        return await _plan_facts(target, fields)
    p = await _row(target, app, operator)
    if p is None or p.get("archived"):
        return None
    out = {"name": str(p.get("name") or "")[: ask_operations.TITLE_MAX], "archived": False}
    index = _procedure(fields)
    if "procedure" not in fields or op_id == "mission.create_local":
        # A card staged before procedures were applied binds the project only; HQ's own flow is
        # re-checked by its runner (`_recheck`).
        return out
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


# ---- Plan with HQ: a conversation bound to one draft (#2152, D4) --------------------------------


def bound_mission(records: list[dict], turn_id: str) -> str:
    """The draft this conversation was opened for: the `mission` of the newest user record up to
    and including ``turn_id`` that carries one (the message route validated it when it was sent;
    the store kept it shape-checked). ``""`` when none did. A later Plan with HQ on another
    mission, in the same conversation, binds that one instead."""
    from . import missions

    bound = ""
    for rec in records:
        if rec.get("type") != "user":
            continue
        mid = rec.get("mission")
        if isinstance(mid, str) and missions.MISSION_ID_RE.match(mid):
            bound = mid
        if rec.get("turn_id") == turn_id:
            break
    return bound


async def still_plannable(mid: str) -> bool:
    """Is the bound draft still one to plan: a draft, not archived, without a plan? Once it is
    planned (or gone) the binding stops steering the conversation: no note, no focus (#2152
    review). Read fresh, every turn."""
    from . import missions

    try:
        m = await missions.run_admitted(lambda: _mission_facts(mid))
    except missions.MissionError:
        return False
    return bool(m and not m["archived"] and m["state"] == "draft" and not m["planned"])


def plan_note(mid: str) -> str:
    """What HQ's own request carries after the Commander's words when the conversation is bound to
    a draft: its id (usable by this turn's tools) and what to do with it."""
    if not mid:
        return ""
    return (
        f"\n\n[plan_mission — the Commander opened this from Plan with HQ on the draft mission "
        f"{mid}. Plan THAT mission: read it with mission_status, then call propose_mission with "
        "mission_id and a fitting procedure_index, or a flow you write when none fits. Never "
        "create a second mission for it.]"
    )


async def bindable(mid: object) -> str:
    """Why a message may not be bound to ``mid``, or ``""`` (the message route's check): a
    mission id that exists. Whether it can still be PLANNED is `mission.plan`'s own check."""
    from . import missions

    if not isinstance(mid, str) or not missions.MISSION_ID_RE.match(mid):
        return "mission must be a mission id"
    try:
        m = await missions.run_admitted(lambda: missions.get_mission(mid, events_limit=1))
    except missions.MissionError:
        return "the mission could not be read just now"
    return "" if m is not None else "no such mission"
