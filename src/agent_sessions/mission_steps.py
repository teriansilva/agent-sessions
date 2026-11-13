"""One API session per agent step: the ONE claim-and-launch seam (#1581 S1, #1613 D15).

Every step launch, whoever asks for it, goes through `start_step`, and `start_step` is the only
caller of `missions.claim_step`. Today the operator's tap is the only production caller
(`Authority("operator")`, the rule `operating-model.md` §6 keeps in force). #1951 slice B passes
`admit=mission_approval.admit` with `Authority("approval")` (or `"consent"`): it is asked off the
loop BEFORE the claim, and the approval it names is bound into the claim's transaction and
re-read inside the write fence at the create and again at the brief.

The order, and why each step is where it is:

1. **The fences every unattended launch carries** (orchestration on, autonomy not `off`, a host
   that can scope a session): read here, at the seam, so no caller can skip them.
2. **The launch facts, blocking, off the loop:** the latest plan revision (it must be the one the
   caller saw), the step and its objective, the assigned specialist VERSION (it must still exist
   and not be archived), the client (`mission_structured.admits`), the project's directory, and
   the server-written brief. Nothing here is trusted later: the claim re-checks the revision and
   the readiness in its own transaction, and the fence re-checks the run.
3. **The admission** (slice B), when given.
4. **The claim** (`missions.claim_step`): the run row, the dispatch row and `→ dispatching`,
   one transaction.
5. **The launch** (`mission_dispatch.run`), the same path the lead's dispatch and a spawn take:
   the launch fence, *alive is not started*, the shielded conclusion, settlement under the
   adoption fence and startup recovery. Its `extra_authorize` is `missions.step_launch_verdict`,
   asked INSIDE the write fence before the create and before the brief, so a plan edit, a Stop or
   a revoked approval after the claim refuses the effect.

A step session is created with no permission bypass (#1613 D8), from its specialist version
(engine, model, effort and skills, with the capacity reserved per worker generation, #1693 S4b).
Read `docs/invariants/mission-steps.md` before changing this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from . import missions, model_grades, prefs, projects, scopedspawn, session_input

log = logging.getLogger(__name__)

#: The longest step instruction (`step_prompts.compose`) a brief carries; the whole brief must
#: still fit the seed cap (`handoff.sanitize_seed`).
INSTRUCTION_MAX = 6000
#: Checklist lines a brief lists (a step holds at most `schema` limits; this bounds the text).
CHECKLIST_MAX = 12
#: The qualification codes that let an approved alternate start instead of the pick (D6): its
#: client can't hold the role, or its usage is spent. Anything else is the plan's to fix.
SWITCH_CODES = frozenset({"engine_not_mission_ready", "usage_exhausted"})


class StepRefused(missions.StepRefused):
    """A launch refused BEFORE the claim (nothing was written). Same shape as the store's."""


@dataclass(frozen=True)
class Authority:
    """Who asked for the launch. `operator` (the tap) needs no admission; `approval` and
    `consent` are #1951 slice B's, and need `admit`."""

    kind: str = "operator"
    consent_id: str | None = None


@dataclass(frozen=True)
class Use:
    """Which specialist of the assignment launches: the pick, or an approved alternate (D6)."""

    kind: str = "primary"
    index: int | None = None
    reason: str = ""

    @classmethod
    def alternate(cls, index: int, reason: str) -> Use:
        return cls("alternate", index, reason)


OPERATOR = Authority()
PRIMARY = Use()
#: `mission_approval.admit`'s shape: ``(mission_id, step_id, boundary, *, consent_id)`` →
#: ``{ok, reasons, approval_id, new}``.
Admit = Callable[..., dict]


# ---- the launch facts (blocking) ----------------------------------------------------------------


def resolve_dir(project_id: object) -> tuple[str, str]:
    """The project's id and its working directory's REAL path, SERVER-side, by the rule the
    dispatch route uses: an unknown, archived or folderless project refuses. Blocking."""
    if not isinstance(project_id, str) or not project_id:
        raise StepRefused("project_unavailable", "this objective names no project")
    try:
        entity = projects.load().get(project_id)
    except Exception:  # noqa: BLE001 — an unreadable projects file refuses, never guesses
        raise StepRefused(
            "project_unavailable", "could not read the projects", status=503
        ) from None
    if entity is None:
        raise StepRefused("project_unavailable", "this objective's project no longer exists")
    if getattr(entity, "archived", False):
        raise StepRefused("project_unavailable", "this objective's project is archived")
    cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
    if not cwd:
        raise StepRefused("project_unavailable", "this objective's project has no folder")
    # THE RESOLVED FOLDER: the shared-folder rule compares these, so a trailing slash or a
    # symlinked second project naming the same directory is the same folder. The launch runs
    # there too, and the fence re-resolves it the same way.
    return entity.id, os.path.realpath(cwd)


def _version(pin: dict) -> dict:
    from . import specialists_store as store

    try:
        v = store.version(pin["id"], int(pin["version"]))
    except store.NotFound:
        raise StepRefused(
            "specialist_unavailable",
            f"{pin['id']} v{pin['version']} no longer exists",
        ) from None
    except store.StoreError as exc:
        raise StepRefused("specialist_unavailable", str(exc), status=503) from None
    try:
        archived = bool(store.get(pin["id"]).get("archived"))
    except store.StoreError:
        archived = True  # an unreadable specialist is not launchable
    if archived:
        raise StepRefused("specialist_unavailable", f"{pin['id']} is archived")
    return v


def _launch_spec(v: dict) -> dict:
    model = v.get("model")
    return {
        "engine": v["engine"],
        "model": None if model in (None, model_grades.DEFAULT_MODEL) else model,
        "effort": v.get("effort"),
    }


def compose_brief(mission: dict, objective: dict, step: dict) -> str:
    """The step session's brief: server-written, nothing in it from a model.

    The mission's title and instruction, the objective's procedure and the step, the step's own
    instruction (`step_prompts.compose`: the retained brief, then its prompts, with the objective's
    input values and the snapshot's defaults; a missing value refuses), and its checklist."""
    from .playbooks import step_prompts
    from .plugins.manifest import ManifestError

    proc = objective["procedure"]
    flow_step = next((s for s in proc["flow"]["steps"] if s["id"] == step["procedure_step"]), None)
    if flow_step is None:  # the plan validator makes this impossible; fail closed anyway
        raise StepRefused("unknown_step", f"step {step['id']} is not in its procedure")
    declared = {v["name"]: v for v in proc.get("variables") or [] if isinstance(v, dict)}
    values: dict = {
        name: v["default"] for name, v in declared.items() if v.get("default") is not None
    }
    for row in objective.get("inputs") or []:
        if "value" in row:
            values[row["name"]] = row["value"]
    instruction = ""
    if flow_step.get("brief") or flow_step.get("prompts"):
        try:
            instruction = step_prompts.compose(flow_step, declared, values, limit=INSTRUCTION_MAX)
        except ManifestError as exc:
            raise StepRefused(
                "brief_refused", f"step {step['id']}'s instruction can't be written: {exc}"
            ) from None
    title = str(mission.get("title") or "").strip()
    lines = [
        f"You are working ONE step of the mission “{title}”.",
        "",
        f"Objective {objective['id']}: {proc['title']}",
        f"Your step: {flow_step['title']} ({step['id']})",
    ]
    goal = str(mission.get("instruction") or "").strip()
    if goal:
        lines += ["", "The mission, in the operator's words:", goal[:2000]]
    if instruction:
        lines += ["", "How to do this step:", instruction]
    checks = [c for c in flow_step.get("checklist") or [] if isinstance(c, dict)]
    if checks:
        lines += ["", "Done means (an independent judge checks each):"]
        lines += [f"- {c.get('title') or c.get('key')}" for c in checks[:CHECKLIST_MAX]]
    lines += [
        "",
        "Work on this step only. When it is done, say what you did and stop; other steps run "
        "in their own sessions.",
    ]
    return "\n".join(lines)


def launch_facts(mission_id: str, step_id: str, *, revision: int, use: Use = PRIMARY) -> dict:
    """Everything the launch needs, read now, or `StepRefused`. Blocking (store, projects,
    specialists, a client's readiness): call it off the loop."""
    mission = missions.get_mission(mission_id, events_limit=1)
    if mission is None:
        raise missions.MissionError(f"unknown mission {mission_id}", status=404)
    latest = missions.plan_revision(mission_id)
    if latest is None:
        raise StepRefused("no_plan", "this mission has no plan yet: apply a procedure first")
    if latest["revision"] != revision:
        raise StepRefused(
            "revision_superseded",
            "the plan changed since you read it; read it again",
            latest_revision=latest["revision"],
        )
    doc = latest["document"]
    step = next((s for s in doc["steps"] if s["id"] == step_id), None)
    if step is None:
        raise StepRefused("unknown_step", f"step {step_id} is not in this plan", status=404)
    if step["actor"] != "agent":
        raise StepRefused("not_agent", f"step {step_id} is the operator's; it launches no agent")
    assignment = step["envelope"]["assignment"]
    if assignment is None:
        raise StepRefused("unassigned", f"step {step_id} has no specialist assigned")
    objective = next(o for o in doc["objectives"] if o["id"] == step["objective"])
    from . import mission_team as team

    # THE TEAM'S ONE PREDICATE, at every start (P5's `qualify`, Hermes on #1581): a grade, model,
    # skill or client change after the assignment refuses an operator's start too, not only an
    # approval-bound one. The context reads the clients and their model lists: off the loop.
    ctx = team.load_context(doc)

    def verdict_of(pin: dict, *, own: bool) -> dict:
        v = team.version_of(ctx, pin)
        if v is None:
            raise StepRefused(
                "specialist_unavailable", f"{pin['id']} v{pin['version']} no longer exists"
            )
        eff = team.own_effective(v) if own else team.effective_of(assignment, v)
        return team.qualify(step, v, eff, ctx)

    def refuse(code: str, verdict: dict, hint: str = "") -> StepRefused:
        text = "; ".join(r["text"] for r in verdict["reasons"]) or "it does not qualify"
        return StepRefused(code, f"step {step_id}: {text}{hint}", reasons=verdict["reasons"])

    if use.kind == "primary":
        if assignment.get("override") is not None:
            # D-1581-9: a per-mission model override changes what the specialist version runs on,
            # and the create binds the version exactly (`specialist_launch.check_api`). Strictly
            # safer to refuse than to launch without the specialist's skills and capacity.
            raise StepRefused(
                "override_unsupported",
                f"step {step_id} has a per-mission model override, which can't start from its "
                "specialist yet; clear the override to start it",
            )
        pin = assignment["specialist"]
        verdict = verdict_of(pin, own=False)
        if not verdict["ok"]:
            switch = {r["code"] for r in verdict["reasons"]} & SWITCH_CODES
            hint = (
                " (an approved alternate may start instead)"
                if switch and assignment["alternates"]
                else ""
            )
            raise refuse("not_qualified", verdict, hint)
        use_reason = ""
    elif use.kind == "alternate":
        alts = assignment["alternates"]
        if use.index is None or not 0 <= use.index < len(alts):
            raise StepRefused("unknown_alternate", f"step {step_id} has no such alternate")
        # D6: an alternate starts only when the pick's client can't (unavailable or out of
        # quota). A full pool is a reason to wait, never to switch.
        pick = verdict_of(assignment["specialist"], own=False)
        switch = [r for r in pick["reasons"] if r["code"] in SWITCH_CODES]
        if pick["ok"] or not switch:
            raise StepRefused(
                "alternate_not_needed",
                f"{assignment['specialist']['id']} is not blocked by its client or its usage, "
                "so its alternate does not start",
            )
        use_reason = switch[0]["text"]
        pin = alts[use.index]["specialist"]
        verdict = verdict_of(pin, own=True)
        if not verdict["ok"]:
            raise refuse("not_qualified", verdict)
    else:
        raise ValueError(f"unknown use {use.kind!r}")
    version = _version(pin)
    spec = _launch_spec(version)
    project_id, cwd = resolve_dir(objective["project_id"])
    return {
        "mission": mission,
        "revision": latest["revision"],
        "step": step,
        "objective": objective,
        "specialist": (pin["id"], int(pin["version"])),
        "title": str(version.get("title") or pin["id"]),
        "use_reason": use_reason,
        **spec,
        "project_id": project_id,
        "cwd": cwd,
        "brief": compose_brief(mission, objective, step),
    }


# ---- the seam -----------------------------------------------------------------------------------


def _launch_fences() -> None:
    """The fences every unattended launch carries (`routes.missions.dispatch_approved`), read at
    the seam so no caller can skip them."""
    cfg = prefs.get_mission_orchestration()
    if not cfg.get("enabled"):
        raise StepRefused(
            "orchestration_off", "orchestration is switched off, so nothing may be launched"
        )
    if str(cfg.get("autonomy") or "") == prefs.ORCH_TIERS[0]:
        raise StepRefused(
            "autonomy_off",
            "autonomy is off — this instance watches and proposes, and may not start an agent",
        )
    if not (scopedspawn.enabled() and scopedspawn.available()):
        raise StepRefused(
            "no_scope",
            "this host cannot put a session in its own scope, so an agent could not be reliably "
            "stopped again; starting a step is refused",
        )


def _grade_now(engine: str, model: str | None) -> str | None:
    """The model's grade in the operator's map right now (`specialists.md`: never cached)."""
    return model_grades.grade_of(engine, model, model_grades.get())


def fence_verdict(plan_id: str) -> str | None:
    """THE check inside the write fence, before the create and again before the brief: None, or
    why the effect is refused. The store's half (`missions.step_launch_verdict`: the run is still
    `claimed`, the revision still the latest, a bound approval or consent still in force), then the
    facts outside the store that can change in the window since the preflight: the specialist
    version still exists and is not archived, is still the CURRENT one when an approval is bound
    (D-P6-1), and its model still meets the step's minimum grade. Cheap reads only (no client is
    asked). Fails closed."""
    from . import specialists_store as store

    why = missions.step_launch_verdict(plan_id)
    if why is not None:
        return why
    try:
        run = missions.step_run_by_plan(plan_id)
        sid, n = run["specialist_id"], int(run["specialist_version"])
        version = store.version(sid, n)
        current = store.get(sid)
        latest = missions.plan_revision(run["mission_id"])
        step = next(x for x in latest["document"]["steps"] if x["id"] == run["step_id"])
        need = (step.get("role") or {}).get("min_grade")
        got = _grade_now(version["engine"], version.get("model")) if need else None
    except Exception:  # noqa: BLE001 — an unreadable fact is not permission
        return "what this step starts as could not be re-read, so nothing was started"
    if current.get("archived"):
        return f"{sid} was archived while the step was being started, so nothing was started"
    if (
        run["authority"] != "operator"
        and int((current.get("current") or {}).get("version") or 0) != n
    ):
        return f"{sid} changed since the plan was approved, so nothing was started"
    if need and model_grades.rank(got) < model_grades.rank(need):
        return (
            f"{sid}'s model no longer meets the step's minimum grade ({need}), "
            "so nothing was started"
        )
    return None


async def start_step(
    mission_id: str,
    step_id: str,
    *,
    revision: int,
    authority: Authority = OPERATOR,
    admit: Admit | None = None,
    use: Use = PRIMARY,
    expect_cwd: str | None = None,
    registry=None,
) -> dict:
    """Claim and launch one episode of ``step_id``. THE seam (module note). Raises
    `missions.StepRefused` (or `MissionError`) when nothing was written; otherwise returns the
    launch's outcome and the run as it settled: ``{outcome, reason, state, session_key, run}``.

    ``expect_cwd`` is the operator's comparand: the directory the view showed. ``admit`` is
    required for an `approval` / `consent` authority and refused for the operator's."""
    from . import mission_dispatch

    missions.validate_id(mission_id)
    missions.validate_step_id(step_id)
    if authority.kind not in missions.STEP_AUTHORITIES:
        raise ValueError(f"unknown authority {authority.kind!r}")
    if (authority.kind == "operator") != (admit is None):
        raise ValueError("an operator launch takes no admission; any other authority needs one")
    policy_epoch = session_input.policy_fingerprint()
    _launch_fences()
    await reconcile(mission_id)
    facts = await asyncio.to_thread(launch_facts, mission_id, step_id, revision=revision, use=use)
    if expect_cwd is not None and expect_cwd != facts["cwd"]:
        raise StepRefused(
            "cwd_moved", "that project now resolves somewhere else; read the steps again"
        )
    approval_id: str | None = None
    if admit is not None:
        boundary = "consent" if authority.kind == "consent" else "launch"
        verdict = await asyncio.to_thread(
            lambda: admit(mission_id, step_id, boundary, consent_id=authority.consent_id)
            if authority.consent_id
            else admit(mission_id, step_id, boundary)
        )
        if not verdict.get("ok"):
            # Recorded by the admission itself, once per (step, what changed): nothing claimed.
            raise StepRefused(
                "admission_refused",
                "; ".join(r.get("text", "") for r in verdict.get("reasons") or [])
                or "the launch was not admitted",
                reasons=verdict.get("reasons") or [],
                new=bool(verdict.get("new")),
            )
        approval_id = verdict.get("approval_id")

    def claim() -> dict:
        return missions.claim_step(
            mission_id,
            step_id,
            revision=facts["revision"],
            engine=facts["engine"],
            model=facts["model"],
            effort=facts["effort"],
            specialist=facts["specialist"],
            use=use.kind,
            alternate_index=use.index,
            use_reason=facts["use_reason"] or use.reason,
            project_id=facts["project_id"],
            cwd=facts["cwd"],
            brief=facts["brief"],
            authority=authority.kind,
            approval_id=approval_id,
            consent_id=authority.consent_id if authority.kind == "consent" else None,
        )

    async def claim_and_launch() -> tuple[str, dict]:
        plan = await missions.run_admitted(claim)
        plan_id = plan["plan_id"]
        out = await mission_dispatch.run(
            mission_id,
            plan,
            registry=registry,
            policy_epoch=policy_epoch,
            verify_cwd=lambda: resolve_dir(facts["project_id"])[1],
            extra_authorize=lambda: fence_verdict(plan_id),
        )
        return plan_id, out

    # THE CLAIM AND ITS LAUNCH ARE ONE PROTECTED TASK (Hermes on #2001). A request cancelled while
    # the claim's worker is in flight cancels only its await: the transaction can still commit,
    # and a claim whose launch never runs is a keyless `claimed` run and a `dispatching` mission
    # owned by a live process, which recovery rightly skips, holding a slot and a folder for the
    # life of the process. So the claim and `mission_dispatch.run` (which settles every outcome,
    # a cancellation included) run as one task the request cannot reach: shielded, joined to
    # completion on cancellation, and only then is the cancellation re-raised.
    task = asyncio.ensure_future(claim_and_launch())
    try:
        plan_id, out = await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        raise
    run = await missions.run_admitted(lambda: missions.step_run_by_plan(plan_id))
    return {
        "outcome": out.get("outcome"),
        "reason": out.get("reason") or "",
        "state": out.get("state"),
        "session_key": out.get("session_key"),
        "run": public_run(run) if run else None,
    }


_NOT_OURS = {
    "detached": "its session was detached from the mission, so it was not stopped",
    "elsewhere": "its session is held by another mission, so it was not stopped",
    "member": "the mission holds its session as another member, so it was not stopped",
}


async def stop_step(mission_id: str, step_id: str, episode: int) -> dict:
    """Stop a step's session (the operator's Stop; it only reduces an effect, so it needs no
    admission). Always through `mission_fence.fenced_step_stop`, which picks what to do from the
    session's MEMBERSHIP, never from the run's state: `stopped` only when the worker was PROVED
    gone (this mission's step member, or an orphan nobody holds); a session the operator detached
    or that is held as another member or by another mission is never stopped (`failed` with why);
    an unproved stop leaves `unknown`, which keeps the slot and the folder. A settled `unknown`
    run also discharges its kept dispatch record, in the same fence. Returns the run as it now
    is."""
    from . import mission_fence

    missions.validate_id(mission_id)
    missions.validate_step_id(step_id)
    run = await missions.run_admitted(lambda: missions.step_run(mission_id, step_id, episode))
    if run is None:
        raise missions.MissionError("no such step run", status=404)
    if run["state"] == "claimed":
        raise StepRefused("launch_in_flight", "this step is still being started; try again")
    if run["state"] not in ("running", "unknown"):
        raise StepRefused("not_open", f"this step run is {run['state']}; nothing to stop")
    was = run["state"]

    def settle(outcome: str) -> None:
        if outcome == "stopped":
            to, detail = "stopped", "the operator stopped it"
        elif outcome in _NOT_OURS:
            to, detail = "failed", _NOT_OURS[outcome]
        elif was == "running":
            to, detail = "unknown", "the session could not be proved stopped"
        else:
            return  # still unknown: the slot, the folder and the record stay
        if (
            missions.finish_step_run(mission_id, step_id, episode, to=to, detail=detail, expect=was)
            and was == "unknown"
        ):
            # The kept dispatch record goes with it: the mission can launch again now.
            missions.clear_dispatch(mission_id, expect_plan=run["plan_id"], stopped=to == "stopped")

    key = run.get("session_key")
    if key:
        await mission_fence.fenced_step_stop(mission_id, key, settle)
    else:
        await missions.run_admitted(lambda: settle("stopped"))  # nothing was ever started
    settled = await missions.run_admitted(lambda: missions.step_run(mission_id, step_id, episode))
    return public_run(settled)


# ---- reconcile, read ----------------------------------------------------------------------------


def _settled_runs(mission_id: str) -> list[tuple[dict, str]]:
    """The `running` runs whose step the evidence now says is `done` or `failed`. Blocking."""
    from . import mission_plan_document as plan_doc

    runs = [r for r in missions.step_runs(mission_id) if r["state"] == "running"]
    if not runs:
        return []
    latest = missions.plan_revision(mission_id)
    if latest is None:
        return []
    states = plan_doc.step_states(latest["document"], missions.objectives(mission_id))
    return [
        (r, states[r["step_id"]]) for r in runs if states.get(r["step_id"]) in ("done", "failed")
    ]


def _idle(key: str) -> bool:
    """Is ``key``'s session between turns, with no background task running? Blocking: asked
    INSIDE the fence (`fenced_step_stop(idle=…)`), where no guarded turn can be admitted. False
    while a turn or task runs (evidence can land mid-turn, Hermes on #1581) and when it can't be
    read: the next pass looks again."""
    from . import structured_runtime as runtime

    try:
        snap = asyncio.run(runtime.snapshot(key))
    except Exception:  # noqa: BLE001 — could not look: not idle
        return False
    return not (snap.get("active_turn") or (snap.get("native") or {}).get("background_active"))


async def reconcile(mission_id: str) -> int:
    """Settle `running` runs whose step the evidence now says is `done` or `failed` (D-1581-6).

    Derived state stays the authority; this records it, with one thread row. The step's session
    is stopped once it is IDLE, under the adoption fence, and the run settles in that same fence
    only on a PROVED stop (or when the operator detached the session, or another mission holds
    it: nothing is stopped then). So the concurrency slot and the folder come back only when
    nothing of that step still runs; a session mid-turn is left for the next pass. Settling also
    releases the session from the roster, so nothing the mission automates sends into it again.
    Never raises (the supervisor calls it every pass)."""
    from . import mission_fence

    try:
        todo = await missions.run_admitted(lambda: _settled_runs(mission_id))
    except Exception:  # noqa: BLE001 — retried by the next pass
        log.debug("mission %s: step reconcile could not read", mission_id, exc_info=True)
        return 0
    n = 0
    for run, derived in todo:
        try:
            why = "its evidence is met" if derived == "done" else "its evidence failed"
            settled: list[bool] = []

            def settle(outcome: str, r=run, d=derived, w=why, out=settled) -> None:
                if outcome in ("leaked", "busy"):
                    return  # still working, or not proved: the slot and the folder stay held
                detail = w if outcome == "stopped" else f"{w}; {_NOT_OURS[outcome]}"
                out.append(
                    missions.finish_step_run(
                        mission_id,
                        r["step_id"],
                        r["episode"],
                        to=d,
                        detail=detail,
                        expect="running",
                    )
                    is not None
                )

            key = run.get("session_key")
            if not key:
                await missions.run_admitted(lambda s=settle: s("stopped"))
            else:
                await mission_fence.fenced_step_stop(
                    mission_id, key, settle, idle=lambda k=key: _idle(k)
                )
            n += 1 if any(settled) else 0
        except Exception:  # noqa: BLE001 — one run must not stop the others
            log.debug(
                "mission %s: step %s not reconciled", mission_id, run["step_id"], exc_info=True
            )
    return n


def run_of_session(session_key: str) -> dict | None:
    """``{mission_id, step_id, episode, state}`` of the step run a session belongs to, or None.
    The lookup a gate settlement (#1951 slice B) uses to find the step a tool request is for."""
    run = missions.step_run_of_session(session_key)
    if run is None:
        return None
    return {k: run[k] for k in ("mission_id", "step_id", "episode", "state")}


_PUBLIC = (
    "step_id",
    "episode",
    "revision",
    "state",
    "session_key",
    "engine",
    "model",
    "effort",
    "use_kind",
    "alternate_index",
    "use_reason",
    "project_id",
    "cwd",
    "authority",
    "detail",
    "created_at",
    "updated_at",
    "ended_at",
)


def public_run(run: dict) -> dict:
    out = {k: run.get(k) for k in _PUBLIC}
    out["specialist"] = {"id": run["specialist_id"], "version": int(run["specialist_version"])}
    return out


def _why_not(mission: dict, step: dict, derived: str, runs: list[dict], open_n: int) -> str | None:
    if step["actor"] != "agent":
        return "the operator's step"
    if step["envelope"]["assignment"] is None:
        return "no specialist assigned"
    if any(r["state"] in missions.OPEN_STEP_STATES for r in runs):
        return "already running"
    if derived != "ready":
        return {"waiting": "waiting for an earlier step", "done": "done", "failed": "failed"}.get(
            derived, derived
        )
    if str(mission.get("state") or "") not in missions.STEP_LAUNCH_STATES:
        return f"the mission is {mission.get('state')}"
    if open_n >= missions.STEP_CONCURRENCY:
        return f"{open_n} of {missions.STEP_CONCURRENCY} steps are running"
    return None


def view_sync(mission_id: str) -> dict:
    """The steps of the latest plan revision, their derived state, their runs, and whether each
    can start now (or why not), as far as a cheap read can tell: readiness, the cap, a held or
    shared folder, orchestration. The specialist's qualification (which may ask a client) is the
    start's preflight, so `can_start` is a hint the start re-checks, never a promise. Blocking;
    no reconcile here (a read has no side effects)."""
    from . import mission_plan_document as plan_doc

    mission = missions.get_mission(mission_id, events_limit=1)
    if mission is None:
        raise missions.MissionError(f"unknown mission {mission_id}", status=404)
    latest = missions.plan_revision(mission_id)
    runs = missions.step_runs(mission_id)
    if latest is None:
        return {"mission_id": mission_id, "revision": None, "steps": [], "concurrency": None}
    doc = latest["document"]
    states = plan_doc.step_states(doc, missions.objectives(mission_id))
    open_n = sum(1 for r in runs if r["state"] in missions.OPEN_STEP_STATES)
    by_obj = {o["id"]: o for o in doc["objectives"]}
    folders = missions.open_step_folders()
    cfg = prefs.get_mission_orchestration()
    switched_off = not cfg.get("enabled") or str(cfg.get("autonomy") or "") == prefs.ORCH_TIERS[0]
    steps = []
    for s in doc["steps"]:
        mine = [r for r in runs if r["step_id"] == s["id"]]
        derived = states.get(s["id"], "waiting")
        cwd = None
        if s["actor"] == "agent":
            try:
                cwd = resolve_dir(by_obj[s["objective"]]["project_id"])[1]
            except StepRefused:
                cwd = None
        why = _why_not(mission, s, derived, mine, open_n)
        if why is None and cwd is None:
            why = "the project has no folder"
        if why is None and cwd in folders:
            other = folders[cwd]
            why = (
                f"{other['step_id']} is working in the same folder"
                if other["mission_id"] == mission_id
                else f"a step of another mission is working in the same folder ({cwd})"
            )
        if why is None and switched_off:
            why = "orchestration is off or its autonomy is off"
        steps.append(
            {
                "id": s["id"],
                "title": s["title"],
                "actor": s["actor"],
                "objective": s["objective"],
                "after": list(s["after"]),
                "derived": derived,
                "assignment": s["envelope"]["assignment"],
                "cwd": cwd,
                "can_start": why is None,
                "why_not": why,
                "runs": [public_run(r) for r in mine],
            }
        )
    return {
        "mission_id": mission_id,
        "mission_state": mission.get("state"),
        "revision": latest["revision"],
        "concurrency": {"open": open_n, "cap": missions.STEP_CONCURRENCY},
        "steps": steps,
    }


__all__ = [
    "OPERATOR",
    "PRIMARY",
    "Admit",
    "Authority",
    "StepRefused",
    "Use",
    "compose_brief",
    "launch_facts",
    "public_run",
    "fence_verdict",
    "reconcile",
    "resolve_dir",
    "run_of_session",
    "start_step",
    "stop_step",
    "view_sync",
]
