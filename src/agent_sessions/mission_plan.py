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

**A new mission plans itself, durably (#967).** Planning is an INTENT recorded on the mission
(`plan_state`), not a button press:

* `POST /api/missions` stamps `plan_state='pending'` at generation 1 in the create transaction and
  schedules :func:`propose_for_new_mission`. Plan again (:func:`propose`) takes a new generation.
* Every attempt runs under `aitasks.single_flight("mission-plan", scope=mission_id)`.
* Every settlement — a plan (`ready`), `failed`, `skipped` — is a compare-and-set on
  `plan_state='pending'` AND the attempt's generation, so a late planner never writes over a newer
  attempt or over the operator's own save. A plan and its `ready` are one transaction.
* :func:`recover_pending` resumes `pending` intents at boot. It settles `ready` without a model
  call only when the stored plan's generation IS the pending one; otherwise it runs the planner
  again for that generation.

**What that guarantees, and what it does not.** `single_flight` is process-local (`aitasks.py`).
With the generation fence it guarantees AT MOST ONE CONCURRENT ATTEMPT per mission in a process
and NO LOST OR OVERWRITTEN RESULT. It does NOT guarantee exactly one model call across crashes: a
process that dies mid-call leaves the attempt `pending`, and recovery calls the model again.

**The operator's chosen project is enforced in code, not in the prompt (#967).** When the mission
has a `project_id`, it is resolved from the project store (never from the capped option list),
shown to the model as the only, fixed project, and kept whatever the model replies — a reply that
names another is ignored and recorded. If that project is archived or removed before the write,
the attempt settles `failed` and no plan is written.
"""

from __future__ import annotations

import asyncio
import logging

from . import aitasks, engines, handoff, missions, projects, prompts, review

log = logging.getLogger(__name__)

#: How many of each the model is shown. A menu nobody reads is a menu that gets a random index.
MAX_PROJECTS = 40
MAX_ENGINES = 12

#: The brief the model writes for the agent. Bounded here as well as in the store, because the
#: store's cap is about what may be persisted and this one is about what may be asked for.
BRIEF_MAX = missions.PLAN_BRIEF_MAX

#: The activity kind. STABLE, with the mission as the single-flight SCOPE — a per-mission kind
#: would grow `aitasks._last` by one entry per mission (#883 review).
KIND = "mission-plan"

#: The one sentence both the skip path and the not-configured model error settle with.
NOT_CONFIGURED = "no AI endpoint is configured, so nothing can be planned"

#: How many times recovery reads its worklist before giving up on the pass, and the first backoff
#: (doubled per retry). Short: this runs at boot, and a busy store usually clears in milliseconds.
WORKLIST_ATTEMPTS = 3
WORKLIST_BACKOFF_S = 0.2


class PlanError(Exception):
    """A plan could not be produced. Carries an HTTP status for the route.

    `outcome` says what happened to the attempt: `failed`, `skipped`, `superseded` (a newer
    attempt or an operator's save owns the plan), `busy` (another attempt is running) or `refused`
    (nothing was attempted).
    """

    def __init__(self, message: str, status: int = 502, *, outcome: str = "failed") -> None:
        super().__init__(message)
        self.status = status
        self.outcome = outcome


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


def resolve_chosen_project(
    project_id: str, *, index: dict | None = None
) -> tuple[dict | None, str]:
    """`(project, "")` for the mission's chosen project, or `(None, reason)` (#967).

    Resolved from the PROJECT STORE by id — not from :func:`project_options`, whose list is capped
    at :data:`MAX_PROJECTS`, so a chosen project that sorts past the cap is still the one used.
    The rules match the option list's: archived and folderless projects cannot be planned into.

    `index` is a store already read by the caller — the write boundary passes the one yielded by
    `projects.locked_index()`, so the check and the plan commit happen under the same lock.
    """
    try:
        entity = (projects.load() if index is None else index).get(project_id)
    except Exception:  # noqa: BLE001 — unreadable is a reason, not a crash
        return None, "the chosen project could not be read"
    if entity is None:
        return None, "the chosen project no longer exists"
    if entity.archived:
        return None, "the chosen project is archived"
    cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
    if not cwd:
        return None, "the chosen project has no folder to work in"
    return {"id": entity.id, "name": entity.name, "cwd": cwd}, ""


def picker_project_options() -> list[dict]:
    """Every project the OPERATOR may pick for a plan: Plan manually's first plan, and a plan edit.

    Not :func:`project_options`. That list is what the MODEL is shown, and it is capped at
    :data:`MAX_PROJECTS` because a menu nobody reads is a menu that gets a random index. The
    operator is not choosing an index: they pick from their own projects, which the projects
    routes already list in full, on an authenticated route. Offering the capped list here meant
    a mission whose project sorts past the cap could not be planned by hand, or shown in an
    edit, in the project it was created in, while `PATCH /plan` resolves that project by id and
    would accept it (#967, the review on #984).

    ONE RULE, ONE SNAPSHOT. A project is offered exactly when :func:`resolve_chosen_project`
    resolves it against a single read of the store: it exists, is not archived, and has a folder
    to work in, which are the rules `PATCH /plan` validates with. So the mission's own project,
    whenever it can be planned into at all, is in this list by construction rather than by a
    special case. Same order as the model's list.
    """
    try:
        loaded = projects.load()
    except Exception:  # noqa: BLE001 — an unreadable projects file is "no options", not a 500
        log.debug("picker project options unavailable")
        return []
    out: list[dict] = []
    for entity in sorted(loaded.values(), key=lambda e: (e.name.lower(), e.id)):
        project, _why = resolve_chosen_project(entity.id, index=loaded)
        if project is not None:
            out.append(project)
    return out


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


def render_options(
    projects_: list[dict], engines_: list[dict], *, fixed_project: bool = False
) -> str:
    """The numbered lists the model selects from.

    Shows the NAME and nothing else that could be mistaken for a target: no path, no id. A model
    that never sees a cwd cannot echo one back, which is the cheapest version of the guarantee.

    `fixed_project` says the one listed project was chosen by the operator (#967). That is a hint
    to the model only; the server keeps the chosen project whatever the reply says.
    """
    if fixed_project:
        lines = ["Projects (chosen by the operator and fixed; reply with project_index 0):"]
    else:
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


async def _settle(
    mission_id: str, generation: int, state: str, detail: str, *, path=None
) -> bool | None:
    """Close one attempt as `failed`/`skipped`, fenced by its generation. Never raises.

    Returns what `settle_plan` said: True when THIS settlement landed, False when the generation
    fence discarded it (a newer attempt or the operator's save owns the mission now), and None when
    it could not be written at all. A settlement that cannot be written leaves the attempt
    `pending`, which is the state recovery exists for — so swallowing here loses nothing.

    **The answer is returned, not dropped** (#974 review, carried into #967 P2b). Callers used to
    report the state they ASKED for, so a discarded skip counted as recovered and a discarded
    failure as failed. :func:`_outcome` turns the answer into the outcome that actually happened.
    """
    try:
        return bool(
            await missions.run_admitted(
                lambda: missions.settle_plan(
                    mission_id, generation, state, detail=detail, path=path
                )
            )
        )
    except Exception:  # noqa: BLE001
        log.debug("mission %s: could not settle planning attempt %s", mission_id, generation)
        return None


def _outcome(landed: bool | None, state: str) -> str:
    """The outcome of a settlement that asked for `state`, given what `_settle` returned.

    `state` only when it landed. A discarded one is `superseded`: this attempt settled nothing,
    and the mission belongs to whoever took the newer generation. One that could not be written is
    `error`: the attempt is still `pending`, and recovery must not count it as done.
    """
    if landed is True:
        return state
    if landed is False:
        return "superseded"
    return "error"


async def _attempt(mission_id: str, generation: int, *, path=None) -> dict:
    """Run the planner for ONE attempt and settle it. The caller holds the single-flight.

    Returns the stored plan, or raises `PlanError` after settling the attempt (or after the fence
    discarded the result).
    """
    row = await missions.run_admitted(
        lambda: missions.get_mission(mission_id, events_limit=1, path=path)
    )
    if row is None:
        raise PlanError(f"unknown mission {mission_id}", status=404, outcome="refused")
    state = str(row.get("state") or "")
    if state not in missions.PLANNABLE_STATES:
        why = f"a mission that is {state} cannot be planned"
        landed = await _settle(mission_id, generation, "failed", why, path=path)
        raise PlanError(why, status=409, outcome=_outcome(landed, "failed"))
    if row.get("archived_at") is not None or row.get("archiving_at") is not None:
        why = "the mission is archived"
        landed = await _settle(mission_id, generation, "failed", why, path=path)
        raise PlanError(why, status=409, outcome=_outcome(landed, "failed"))

    # THE OPERATOR'S PROJECT, resolved from the store before anything is asked. A project that is
    # already gone fails the attempt here rather than after a model call nobody can use.
    chosen_id = row.get("project_id")
    fixed: dict | None = None
    if chosen_id:
        fixed, why = resolve_chosen_project(str(chosen_id))
        if fixed is None:
            landed = await _settle(mission_id, generation, "failed", why, path=path)
            raise PlanError(why, status=409, outcome=_outcome(landed, "failed"))

    # BUILT ONCE AND RESOLVED AGAINST THE SAME LISTS. Re-reading them after the model call would
    # resolve an index into a set the model never saw — the "an index is not an identity" family,
    # in the one place where the identity it would name is a filesystem path.
    projects_ = [fixed] if fixed is not None else project_options()
    engines_ = engine_options()

    instruction = str(row.get("instruction") or row.get("title") or "")
    options = render_options(projects_, engines_, fixed_project=fixed is not None)
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("mission_plan")},
                {"role": "user", "content": f"Instruction:\n{instruction}\n\n{options}"},
            ]
        )
    except review.NotConfiguredError:
        landed = await _settle(mission_id, generation, "skipped", NOT_CONFIGURED, path=path)
        raise PlanError(NOT_CONFIGURED, 409, outcome=_outcome(landed, "skipped")) from None
    except Exception as e:  # noqa: BLE001
        log.debug("mission plan for %s failed: %s", mission_id, type(e).__name__)
        landed = await _settle(
            mission_id,
            generation,
            "failed",
            f"the model call failed: {aitasks.clamp_error(e)}",
            path=path,
        )
        raise PlanError(
            f"the plan could not be produced ({type(e).__name__})",
            outcome=_outcome(landed, "failed"),
        ) from None

    proposal, dropped = proposal_from_reply(obj, projects_, engines_)
    note: str | None = None
    if fixed is not None:
        # THE MODEL'S PROJECT IS IGNORED. The only project it was shown is the chosen one, so any
        # non-null index that did not resolve to it is a reply naming something else — recorded,
        # and never persisted.
        raw = obj.get("project_index") if isinstance(obj, dict) else None
        if raw is not None and proposal["project"] is None:
            note = (
                "the planner named a different project; the project chosen for this mission "
                "was kept"
            )
        dropped = [d for d in dropped if d != "project"]

    brief = proposal["brief"] or instruction
    if not brief:
        why = "the plan has no brief and the mission has no instruction"
        landed = await _settle(mission_id, generation, "failed", why, path=path)
        raise PlanError(why, 422, outcome=_outcome(landed, "failed"))

    def _store(target: dict | None) -> dict:
        return missions.put_plan(
            mission_id,
            project_id=(target or {}).get("id"),
            cwd=(target or {}).get("cwd"),
            engine=proposal["engine"],
            engine_reason=proposal["engine_reason"],
            brief=brief,
            generation=generation,
            planner_note=note,
            path=path,
        )

    def _write():
        if fixed is None:
            return _store(proposal["project"]), ""
        # THE WRITE BOUNDARY, FENCED (#974 review). Re-reading the chosen project just before
        # `put_plan` was a check, not an exclusion: project archive and delete serialize on the
        # project store's own flock and never wait for the missions store, so one could commit
        # after the re-read and before the plan's COMMIT, and the plan settled `ready` against a
        # project that no longer existed as read. Holding that same flock from the final read
        # through `put_plan`'s COMMIT makes archive/delete wait for the plan, or makes the plan
        # see them and settle `failed`.
        #
        # LOCK ORDER: projects-store flock (outer) -> missions `_write_lock` -> missions.db write
        # lock (inner). No path takes them the other way: project mutations take only the flock,
        # and mission code reads projects through the lockless `load()`. Held on the admission
        # worker thread, never across the model call or an await.
        with projects.locked_index() as index:
            target, gone = resolve_chosen_project(fixed["id"], index=index)
            if target is None:
                return None, gone
            return _store(target), ""

    try:
        stored, gone = await missions.run_admitted(_write)
    except missions.PlanSuperseded as e:
        raise PlanError(str(e), 409, outcome="superseded") from None
    except missions.MissionError as e:
        why, status = str(e), e.status
        landed = await _settle(mission_id, generation, "failed", why, path=path)
        raise PlanError(why, status, outcome=_outcome(landed, "failed")) from None
    if stored is None:
        landed = await _settle(mission_id, generation, "failed", gone, path=path)
        raise PlanError(gone, 409, outcome=_outcome(landed, "failed"))

    stored["dropped"] = dropped
    stored["project_options"] = projects_
    stored["engine_options"] = engines_
    return stored


async def _run_generation(mission_id: str, generation: int, *, path=None) -> dict:
    """:func:`_attempt`, with an unexpected error settling the attempt `failed` too."""
    try:
        return await _attempt(mission_id, generation, path=path)
    except PlanError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("mission %s: planning attempt %s failed: %s", mission_id, generation, e)
        landed = await _settle(
            mission_id,
            generation,
            "failed",
            f"planning failed: {aitasks.clamp_error(e)}",
            path=path,
        )
        raise PlanError(
            f"the plan could not be produced ({type(e).__name__})",
            outcome=_outcome(landed, "failed"),
        ) from None


async def propose(mission_id: str, *, path=None) -> dict:
    """PLAN AGAIN: start a new attempt, run it, and return the stored plan (synchronous).

    Raises `PlanError` rather than returning a half-answer: unlike a question, a plan the operator
    cannot see is not a state worth writing — there is nothing for the console to render and
    nothing for DISPATCH to consume.

    **Single-flighted per mission, and the generation is taken INSIDE the flight** (#967). A
    second request while an attempt is running is a 409 that changes nothing; bumping first
    would retire the attempt that is actually running.
    """
    row = await missions.run_admitted(
        lambda: missions.get_mission(mission_id, events_limit=1, path=path)
    )
    if row is None:
        raise PlanError(f"unknown mission {mission_id}", status=404, outcome="refused")
    state = str(row.get("state") or "")
    if state not in missions.PLANNABLE_STATES:
        raise PlanError(f"a mission that is {state} cannot be planned", 409, outcome="refused")

    try:
        async with aitasks.single_flight(KIND, detail=mission_id, scope=mission_id):
            try:
                generation = await missions.run_admitted(
                    lambda: missions.begin_planning(mission_id, path=path)
                )
            except missions.MissionError as e:
                raise PlanError(str(e), e.status, outcome="refused") from None
            return await _run_generation(mission_id, generation, path=path)
    except aitasks.AlreadyRunning:
        raise PlanError(
            "a plan is already being prepared for this mission", 409, outcome="busy"
        ) from None


async def _resume(mission_id: str, *, path=None) -> dict:
    """Discharge the mission's PENDING attempt, whichever generation it is. Inside the flight."""
    intent = await missions.run_admitted(lambda: missions.plan_intent(mission_id, path=path))
    if intent is None:
        return {"plan_state": "gone"}
    if intent["plan_state"] != "pending":
        # Settled already — by the attempt itself, by an operator's save, or by an earlier pass.
        # Reported as its own outcome: THIS call settled nothing, and a caller counting outcomes
        # must not read someone else's `ready` or `failed` as its own.
        return {"plan_state": "not_pending", "settled": intent["plan_state"]}
    generation = intent["plan_generation"]

    # RECOVERY IS BOUND TO THE GENERATION, NOT TO "A PLAN ROW EXISTS". A Plan again is pending
    # while the previous plan is still stored; only a stored plan OF THIS ATTEMPT may settle it
    # without asking the model again.
    if intent["stored_generation"] == generation:
        settled = await missions.run_admitted(
            lambda: missions.settle_plan_from_stored(mission_id, generation, path=path)
        )
        return {"plan_state": "ready" if settled else "unchanged", "model_called": False}

    # Not configured is an unmade choice, not a fault: settled `skipped` for this attempt, and
    # without a red task counter — nothing raises through the flight.
    try:
        review._require_config()
    except review.NotConfiguredError:
        landed = await _settle(mission_id, generation, "skipped", NOT_CONFIGURED, path=path)
        return {"plan_state": _outcome(landed, "skipped")}

    plan = await _run_generation(mission_id, generation, path=path)
    return {"plan_state": "ready", "plan_id": plan["plan_id"]}


async def propose_for_new_mission(mission_id: str, *, path=None) -> dict:
    """The lifecycle call site: plan a NEWLY CREATED mission, or resume a pending attempt (#967).

    Mirrors `mission_objectives.propose_for_new_mission`:

    * **A mission is created whether or not this succeeds.** It runs after the 201, and every
      outcome that is not a plan is a settlement plus a `planning` event on the mission.
    * **Never raises.** It is a `BackgroundTask` and the recovery worker; neither has anyone to
      raise to.
    * **Single-flighted per mission.** If another attempt holds the flight it owns the outcome,
      and settling here would close an intent that is still being worked.
    """
    try:
        async with aitasks.single_flight(KIND, detail=mission_id, scope=mission_id):
            return await _resume(mission_id, path=path)
    except aitasks.AlreadyRunning:
        return {"plan_state": "already_running"}
    except PlanError as e:
        return {"plan_state": e.outcome, "detail": str(e)}
    except Exception as e:  # noqa: BLE001 — recorded on the mission where possible, then swallowed
        log.warning("mission %s: planning failed: %s", mission_id, type(e).__name__)
        return {"plan_state": "error"}


async def recover_pending(*, older_than: float = 0.0, limit: int = 20) -> dict:
    """Finish planning attempts that a crash or restart interrupted (#967).

    The planner runs as a `BackgroundTask`, which lives only in the process that served the
    request; the intent is stamped with the mission, and this is the caller that discharges it.
    Same forward-paging cursor as `mission_objectives.recover_pending`, for the same reasons: each
    row is visited at most once per pass, and nothing is skipped.

    For each `pending` mission it resumes THAT generation: `ready` without a model call only when
    the stored plan was produced by it, otherwise the planner runs again (or the attempt settles
    `skipped` / `failed` when it cannot). Because single-flight is process-local, a crash mid-call
    means this calls the model a second time; the generation fence is what keeps that from ever
    losing or overwriting a result.

    **The report says what happened, per mission** (#974 review). `propose_for_new_mission` never
    raises — it turns every failure into a returned outcome — so counting "returned" as
    "recovered" reported swallowed errors as successes. Each mission lands in exactly one bucket:

    * `recovered` — this pass's own settlement LANDED as `ready` or `skipped`;
    * `failed` — this pass's `failed` settlement landed, a settlement could not be written (the
      attempt is still `pending`), or the call hit an unexpected error;
    * `already_running` — another attempt in this process holds the single-flight and owns it;
    * `unchanged` — nothing for this pass to settle: settled elsewhere, gone, or SUPERSEDED — the
      generation fence discarded this pass's settlement because a newer attempt or the operator's
      own save owns the mission. A discarded settlement is not an outcome of this pass (#967 P2b).

    **A worklist read is retried** :data:`WORKLIST_ATTEMPTS` times with a short backoff before the
    pass gives up, and giving up is reported as `worklist_error` rather than as an empty pass. One
    busy-database blip at boot used to end recovery until the next restart.
    """
    out: dict = {"recovered": [], "failed": [], "already_running": [], "unchanged": []}
    cursor: tuple[float, str] | None = None
    while True:
        rows = None
        for attempt in range(WORKLIST_ATTEMPTS):
            try:
                rows = await missions.run_admitted(
                    lambda c=cursor: missions.missions_awaiting_plan(
                        older_than=older_than, limit=limit, after=c
                    )
                )
                break
            except Exception as e:  # noqa: BLE001 — recovery is opportunistic; never fail boot
                log.debug("plan recovery: worklist read %d failed", attempt + 1, exc_info=True)
                if attempt + 1 >= WORKLIST_ATTEMPTS:
                    out["worklist_error"] = aitasks.clamp_error(e)
                    return out
                await asyncio.sleep(WORKLIST_BACKOFF_S * (2**attempt))
        if not rows:
            return out
        for at, mid in rows:
            cursor = (at, mid)
            try:
                result = await propose_for_new_mission(mid)
            except Exception as e:  # noqa: BLE001 — one stuck mission must not block the others
                log.warning("mission %s: plan recovery failed: %s", mid, e)
                result = {"plan_state": "error"}
            out[_recovery_bucket(result)].append(mid)


def _recovery_bucket(result: dict) -> str:
    """Which report bucket one `propose_for_new_mission` outcome belongs in."""
    state = str((result or {}).get("plan_state") or "")
    if state in ("ready", "skipped"):
        return "recovered"
    if state in ("failed", "error"):
        return "failed"
    if state in ("already_running", "busy"):
        return "already_running"
    return "unchanged"
