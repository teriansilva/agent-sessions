"""An instruction becomes a PROPOSAL — project, agent, brief — and nothing launches (#893).

Phase 4 of #840, and the step that turns MISSION CONTROL from a console that watches work into
one that starts it. The half that matters is the one that does not happen here: `/plan` produces
a proposal and stores it, and DISPATCH is a separate, explicitly operator-triggered call.

**The index discipline, for the third time.** #883 established it for probe templates and #892
for question actions, and it is load-bearing here in a way it was not there: a model-authored
`cwd` is a path-traversal bug with an unattended agent on the end of it. So the model
receives NUMBERED LISTS of projects and engines and returns indices into them; the path comes
from the project entity, server-side, exactly as `POST /api/missions` already resolves it. There
is no code path from model text to a launch argument, which is a stronger claim than "we validate
what it sends" and is the only one worth making.

**A suggestion is a proposal, never a silent choice.** The engine comes back with a REASON, the
reason is shown, and the operator can change it or cancel. An agent chosen for the operator
without a stated why is a decision disguised as a default.

**`shell` is never a dispatch target.** Seeding a brief into a bare `bash -l` *runs* it. The gate
is `handoff.seed_start_state`, the same function the handoff picker uses, so the two sets cannot
drift — and it is applied when the LIST is built, so an unseedable engine is not offered rather
than offered and then refused.

**Nothing here is trusted after the await.** The lists are built, the model is asked, and the
indices are resolved against the lists THAT WERE SENT — not against a freshly read set that may
have moved. The plan then records the resolved project id, and `POST /dispatch` resolves the cwd
again at the write boundary, because a project's folder can change between planning and pressing
the button (`stale policy across the await`, #887's family).
"""

from __future__ import annotations

import logging

from . import engines, handoff, missions, projects, prompts, review

log = logging.getLogger(__name__)

#: How many of each the model is shown. A menu nobody reads is a menu that gets a random index.
MAX_PROJECTS = 40
MAX_ENGINES = 12

#: The brief the model writes for the agent. Bounded here as well as in the store, because the
#: store's cap is about what may be persisted and this one is about what may be asked for.
BRIEF_MAX = missions.PLAN_BRIEF_MAX


class PlanError(Exception):
    """A plan could not be produced. Carries an HTTP status for the route."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def project_options() -> list[dict]:
    """The projects a mission may be planned into, in a stable order.

    Archived projects are excluded and a project with no folder is excluded: both would produce a
    plan that cannot be dispatched, and offering a choice that always fails is worse than offering
    fewer choices.
    """
    try:
        loaded = projects.load()
    except Exception:  # noqa: BLE001 — an unreadable projects file is "no options", not a 500
        log.debug("project options unavailable")
        return []
    out: list[dict] = []
    for entity in sorted(loaded.values(), key=lambda e: (e.name.lower(), e.id)):
        if entity.archived:
            continue
        cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
        if not cwd:
            continue
        out.append({"id": entity.id, "name": entity.name, "cwd": cwd})
    return out[:MAX_PROJECTS]


def engine_options() -> list[dict]:
    """The engines that can actually be dispatched into, with the reason for every exclusion.

    Built through `handoff.seed_start_state`, which is the single capability source the handoff
    picker uses. Applying it HERE — when the list is built — is what makes "`shell` is never a
    dispatch target" a property of the offer rather than a refusal after the fact.
    """
    out: list[dict] = []
    for prov in engines.all_providers():
        supported, _why = handoff.seed_start_state(prov, present=prov.is_present())
        if not supported:
            continue
        out.append({"id": prov.engine_id, "label": prov.engine_id})
    return out[:MAX_ENGINES]


def render_options(projects_: list[dict], engines_: list[dict]) -> str:
    """The numbered lists the model selects from.

    Shows the NAME and nothing else that could be mistaken for a target: no path, no id. A model
    that never sees a cwd cannot echo one back, which is the cheapest version of the guarantee.
    """
    lines = ["Projects:"]
    if projects_:
        lines += [f"  {i}. {p['name']}" for i, p in enumerate(projects_)]
    else:
        lines.append("  (none configured)")
    lines.append("Agents:")
    if engines_:
        lines += [f"  {i}. {e['label']}" for i, e in enumerate(engines_)]
    else:
        lines.append("  (none available)")
    return "\n".join(lines)


def _index(value: object, upper: int) -> int | None:
    """A model-supplied index, or None. `bool` is refused before `int`.

    `isinstance(True, int)` is True in Python, so an unguarded check lets `true` select element 1
    — an agent nobody chose. Out of range is None rather than clamped: a clamped index is the
    server inventing a choice and presenting it as the model's.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value < upper:
        return None
    return value


def proposal_from_reply(
    obj: object, projects_: list[dict], engines_: list[dict]
) -> tuple[dict, list[str]]:
    """`(proposal, dropped)` from a model reply. Every refusal DROPS rather than repairs.

    The proposal is always returned — a plan with no project is a legitimate, showable state (it
    is exactly what the picker is for) — and `dropped` names what could not be resolved so the
    card can say why a field is empty instead of looking merely unfilled.
    """
    dropped: list[str] = []
    if not isinstance(obj, dict):
        return {"project": None, "engine": None, "engine_reason": "", "brief": ""}, ["reply"]

    pi = _index(obj.get("project_index"), len(projects_))
    if pi is None and obj.get("project_index") is not None:
        dropped.append("project")
    ei = _index(obj.get("engine_index"), len(engines_))
    if ei is None and obj.get("engine_index") is not None:
        dropped.append("engine")

    raw_brief = obj.get("brief")
    brief = raw_brief.strip()[:BRIEF_MAX] if isinstance(raw_brief, str) else ""
    if not brief:
        dropped.append("brief")
    reason = obj.get("engine_reason")
    reason = reason.strip()[: missions.PLAN_REASON_MAX] if isinstance(reason, str) else ""
    # A suggestion with no reason is a silent choice, which is the thing this feature refuses to
    # make. Keep the engine, say plainly that nothing was given.
    if ei is not None and not reason:
        reason = "no reason was given"
    return (
        {
            "project": projects_[pi] if pi is not None else None,
            "engine": engines_[ei]["id"] if ei is not None else None,
            "engine_reason": reason,
            "brief": brief,
        },
        dropped,
    )


async def propose(mission_id: str, *, path=None) -> dict:
    """Produce and STORE one proposal for this mission. Returns the stored plan row.

    Raises `PlanError` rather than returning a half-answer: unlike a question, a plan the operator
    cannot see is not a state worth writing — there is nothing for the console to render and
    nothing for DISPATCH to consume.
    """
    row = await missions.run_admitted(lambda: missions.get_mission(mission_id, path=path))
    if row is None:
        raise PlanError(f"unknown mission {mission_id}", status=404)
    state = str(row.get("state") or "")
    if state not in missions.PLANNABLE_STATES:
        raise PlanError(f"a mission that is {state} cannot be planned", status=409)

    # BUILT ONCE AND RESOLVED AGAINST THE SAME LISTS. Re-reading them after the model call would
    # resolve an index into a set the model never saw — the "an index is not an identity" family,
    # in the one place where the identity it would name is a filesystem path.
    projects_ = project_options()
    engines_ = engine_options()

    instruction = str(row.get("instruction") or row.get("title") or "")
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("mission_plan")},
                {
                    "role": "user",
                    "content": (
                        f"Instruction:\n{instruction}\n\n{render_options(projects_, engines_)}"
                    ),
                },
            ]
        )
    except review.NotConfiguredError:
        raise PlanError("no AI endpoint is configured, so nothing can be planned", 409) from None
    except Exception as e:  # noqa: BLE001
        log.debug("mission plan for %s failed: %s", mission_id, type(e).__name__)
        raise PlanError(f"the plan could not be produced ({type(e).__name__})") from None

    proposal, dropped = proposal_from_reply(obj, projects_, engines_)
    brief = proposal["brief"] or instruction
    if not brief:
        raise PlanError("the plan has no brief and the mission has no instruction", 422)

    project = proposal["project"]
    stored = await missions.run_admitted(
        lambda: missions.put_plan(
            mission_id,
            project_id=(project or {}).get("id"),
            cwd=(project or {}).get("cwd"),
            engine=proposal["engine"],
            engine_reason=proposal["engine_reason"],
            brief=brief,
            path=path,
        )
    )
    stored["dropped"] = dropped
    stored["project_options"] = projects_
    stored["engine_options"] = engines_
    return stored
