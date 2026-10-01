"""Run one claimed automation run through the EXISTING paths (#1201 Phase 1).

There is no second launcher here. Each action goes through the path a manual start uses:

* ``start_mission`` → ``missions.create_mission`` + the background producers the create route runs
  (objectives + plan), then — when the consented autonomy says so — ``routes.missions.
  dispatch_approved``, the manual dispatch route's own body, with every fence it has (master
  switch, tier, containment, cwd and objectives comparands, plan CAS). The mission-orchestration
  tier is never raised here (#1019): ``dispatch`` refuses exactly where the route would.
* ``start_session`` → ``headless_dispatch.dispatch``. The run records the dispatch's own facts —
  launched → started → briefed (→ bound) — never a summary of them. The engine must pass
  ``engines.unattended_start_state`` at save AND here.
* ``send_to_session`` → ``session_input.send_input`` (single writer, ``require_quiet``,
  ``precondition`` + ``final_guard``) for text, ``template_send.send`` for a template (the only
  path a secret travels). Its ``Outcome`` becomes the run outcome.

**Boundary at run time.** Roots + ``folder_exclusions`` (the terminal's hard scope) for the target
folder or session, a present non-retiring engine, a live target for a send. A failed check is a run
REFUSED with its reason — never a fallback to some other target.

**Pinned inputs.** Before any effect the template/checklist the operator approved is compared with
what is stored now; a difference marks the automation *needs re-approval*, pauses it and refuses the
run. Library text values are data and are re-read at every run.

**Secrets.** A mission brief never carries one (refused at save, and the resolved template's field
kinds are checked again immediately before the mission is created). A send renders a secret only
inside ``template_send``; the run records the MASKED text it returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid

from . import automation_effect_lock as effect_lock
from . import automations as model
from . import automations_store as store
from . import (
    engines,
    fsbrowse,
    handoff,
    headless_dispatch,
    missions,
    notifications,
    prefs,
    project_dirs,
    session_input,
    template_send,
    template_vars,
)
from . import templates as tstore

log = logging.getLogger(__name__)

#: How long shutdown waits for runs already started before recording them interrupted.
DRAIN_TIMEOUT_S = 30.0

_TASKS: set[asyncio.Task] = set()
#: Runs whose EFFECT has already happened: ``{run_id: (outcome, reason)}``. Anything that fails
#: afterwards is bookkeeping, and must never turn a delivered run into a `failed` one (#1201).
_DELIVERED: dict[str, tuple[str, str]] = {}


def _delivered(run_id: str, outcome: str, reason: str) -> None:
    _DELIVERED[run_id] = (outcome, reason)


class RunRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def loop_enabled() -> bool:
    """Kill switch. ``AGENT_SESSIONS_AUTOMATION_LOOP=0`` stops every trigger, Run now included."""
    return (os.environ.get("AGENT_SESSIONS_AUTOMATION_LOOP", "1") or "1") != "0"


# ---- boundary checks ----------------------------------------------------------------------------


def scope_ok(cwd: str | None) -> bool:
    """Roots + ``folder_exclusions``, the terminal's hard scope. ``None`` fails closed wherever a
    boundary is configured — the same rule ``template_send`` and the terminal ATTACH use."""
    roots, exclusions = project_dirs.effective_roots(), prefs.get_folder_exclusions()
    if cwd is None:
        return not (roots or exclusions)
    return project_dirs.in_scope(cwd, roots=roots, exclusions=exclusions)


def folder_ok(folder: str) -> tuple[bool, str]:
    if not fsbrowse.is_browsable_dir(folder):
        return False, "that folder does not exist or is outside your home directory"
    if not scope_ok(folder):
        return False, "that folder is outside your project folders"
    return True, ""


def engine_state(engine: str) -> tuple[bool, str]:
    """Whether ``engine`` may be started unattended right now — checked at save AND at run."""
    prov = engines.get(engine)
    if prov is None:
        return False, engines.removed_reason(engine) or f"unknown agent {engine!r}"
    if engines.is_retiring(prov):
        return False, engines.REMOVED_REASON
    try:
        present = bool(prov.is_present())
    except Exception:  # noqa: BLE001 — an engine that cannot answer is not present
        present = False
    ok, why = handoff.seed_start_state(prov, present=present)
    if not ok:
        return False, why or f"{engine} cannot be started with a first message"
    ok, why = engines.unattended_start_state(prov)
    if not ok:
        return False, why or f"{engine} cannot be started unattended"
    return True, ""


def check_save(config: dict) -> None:
    """Save-time checks that need the world (templates, engines, folders). Raises
    ``AutomationError``; blocking — run off the event loop."""
    model.check_template(config)
    action = config["action"]
    if action["kind"] == "start_session":
        ok, why = engine_state(action["engine"])
        if not ok:
            raise model.AutomationError(why)
        ok, why = folder_ok(action["folder"])
        if not ok:
            raise model.AutomationError(why)
    elif action["kind"] == "send_to_session":
        try:
            prov, native = engines.parse_key(action["session_key"])
        except Exception:
            raise model.AutomationError("not a session id this app can address") from None
        # The boundary is checked here too WHEN the target can be resolved now; a session that is
        # not running yet is checked at run time, where the send refuses it just the same.
        try:
            row = engines.resolve_session(prov.engine_id, native)
        except Exception:  # noqa: BLE001 — unresolvable now: the run-time check decides
            row = None
        cwd = getattr(row, "cwd", None) if row is not None else None
        if cwd and not scope_ok(cwd):
            raise model.AutomationError("that session is outside your project folders")
    elif action["kind"] == "start_mission":
        from .routes import missions as mroutes

        try:
            _pid, cwd = mroutes._resolve_cwd(action["project_id"])
        except missions.MissionError as e:
            raise model.AutomationError(str(e), status=e.status) from None
        if not scope_ok(cwd):
            raise model.AutomationError("that project's folder is outside your project folders")


# ---- rendering ----------------------------------------------------------------------------------


def _render_plain(config: dict, pins: dict) -> tuple[str, dict]:
    """``(text, inputs)`` for a mission or new-session message, which may NEVER carry a secret.

    Re-reads the template and the library at run time, and re-checks every field kind here — the
    last moment before the text goes anywhere (#1201 round 2)."""
    msg = model.message_of(config["action"])
    if "text" in msg:
        return msg["text"], {"message": msg["text"]}
    try:
        t = tstore.get_template(msg["template_id"])
    except tstore.TemplateNotFound:
        raise RunRefused(409, "the template was deleted") from None
    if (pins.get("template") or {}).get("digest") != model.template_digest(t):
        raise RunRefused(409, "the template was edited since you approved this automation")
    secret_names = set(template_vars.secret_state())
    for f in t["fields"]:
        if f["kind"] == "secret" or (f["source"] == "library" and f["name"] in secret_names):
            raise RunRefused(
                409, "the template now has a secret, which this action can never carry"
            )
    try:
        rendered = template_send.render(
            t, msg["values"], library=template_vars.values(), secrets={}, secret_state={}
        )
    except (tstore.TemplateError, template_send.SendRefused) as e:
        raise RunRefused(409, str(getattr(e, "detail", None) or e)) from None
    return rendered.text, {"message": rendered.masked, "template_id": t["id"]}


# ---- authority ----------------------------------------------------------------------------------

#: Effects of ONE automation are serialised: under `concurrency: allow` overlapping runs WAIT for
#: each other's effect (up to `RUNNER_WAIT_S`) rather than act in parallel; one that waited too long
#: is skipped — never a failure, never a notification.
LOCK_BUSY = "skipped: previous run still in its effect"
SEND_BUSY = "skipped: another send to that session is in progress"
SEND_LOCK_UNAVAILABLE = "skipped: " + template_send.SEND_LOCK_UNAVAILABLE


def authority(run: dict) -> tuple[bool, str, str]:
    """The FINAL authority read: ``(ok, why, kind)``. Called under the effect lock, right before
    the effect, and again inside every launch fence and write guard.

    ``kind`` says WHO stopped the run, because only an operator's own action is ``stopped``:

    * ``stopped`` — turned off, or any write by the operator since the claim (every writer bumps
      the revision): not a failure, no notification;
    * ``halted`` — the automation stopped itself (auto-paused after another run's failures, or
      flagged for re-approval) or expired while this run waited: ``skipped``;
    * ``unreadable`` — the store could not answer: ``refused``.
    """
    try:
        row = store.get(run["automation_id"])
    except Exception:  # noqa: BLE001 — unreadable authority never permits an effect
        return False, "the automation could not be re-read", "unreadable"
    scope = run.get("scope") or {}
    if not row["enabled"]:
        return False, "stopped: you turned it off during the run", "stopped"
    expires = ((row["config"] or {}).get("policy") or {}).get("expires_at")
    if expires is not None and time.time() >= expires:
        return False, "skipped: expired while waiting", "halted"
    if row["needs_reapproval"]:
        return False, "skipped: it needs your approval again", "halted"
    if row["consented_scope"] != scope.get("consented_scope") or row["revision"] != scope.get(
        "revision"
    ):
        if run["trigger"] != "manual" and row["paused"]:
            return False, "stopped: you paused it during the run", "stopped"
        return False, "stopped: you changed it during the run", "stopped"
    if run["trigger"] != "manual" and row["paused"]:
        # Paused WITHOUT an operator write since the claim: the automation's own auto-pause.
        return False, "skipped: it was paused after failed runs", "halted"
    return True, "", ""


def _still_authorized(run: dict) -> tuple[bool, str]:
    ok, why, _kind = authority(run)
    return ok, why


def _authority_outcome(run: dict) -> tuple[str, str] | None:
    ok, why, kind = authority(run)
    if ok:
        return None
    return {"stopped": "stopped", "halted": "skipped"}.get(kind, "refused"), why


#: Runs whose own drift check flagged re-approval: the drift refused them, not an operator stop.
_FLAGGED: set[str] = set()


def _flag(run: dict, why: str) -> None:
    _FLAGGED.add(run["id"])
    with contextlib.suppress(Exception):
        store.flag_reapproval(
            run["automation_id"], why, observed_revision=(run.get("scope") or {}).get("revision")
        )


def _identity_drift(pinned: dict, path: str | None) -> str:
    """Why ``path`` is no longer the approved directory (name, realpath, device and inode)."""
    if not path or not pinned.get("real"):
        return "the working folder changed since you approved this automation"
    try:
        now = model.folder_identity(path)
    except model.PinsUnavailable as e:
        return str(e)
    if now != pinned:
        return "the working folder changed since you approved this automation"
    return ""


def _real_still_pinned(pinned: dict) -> str:
    """At the launch boundary: the pinned REAL path is still exactly that directory — no symlink in
    its place, same device and inode. (A same-UID swap-and-restore around the launcher's own
    directory open is the documented residual.)"""
    real = pinned.get("real") or ""
    try:
        st = os.stat(real)
    except OSError:
        return "the working folder is gone"
    if os.path.realpath(real) != real or (st.st_dev, st.st_ino) != (
        pinned.get("dev"),
        pinned.get("ino"),
    ):
        return "the working folder changed while the launch was being authorised"
    return ""


# ---- actions ------------------------------------------------------------------------------------


def _playbook_for(action: dict, pins: dict) -> str | None:
    """The checklist the mission is created with: EXACTLY the pinned one (a default resolved at
    consent is passed by id, so a later change of the default cannot swap it)."""
    if action.get("checklist_id") == missions.PLAYBOOK_DECLINED:
        return missions.PLAYBOOK_DECLINED
    ck = pins.get("checklist") or {}
    return ck.get("id") or missions.PLAYBOOK_DECLINED


def _checklist_drift(mid: str, pins: dict) -> str:
    """After the mission is created: its checklist still resolves to the approved one."""
    ck = pins.get("checklist")
    if not ck or ck.get("digest") == "none":
        return ""
    status, templates = missions.templates_for_mission(mid)
    book = next(
        (p for p in prefs.get_mission_playbooks()["playbooks"] if p["id"] == ck["id"]), None
    )
    if (
        status != "ok"
        or book is None
        or model._digest(book) != ck["digest"]
        or templates != book["objectives"]
    ):
        return "the checklist changed while the mission was being created; it was not dispatched"
    return ""


def _mission_fence(run: dict, pinned: dict, project_id: str | None):
    """The automation's check INSIDE the dispatch fence (with the fence's own realpath compare)."""
    from .routes import missions as mroutes

    def check() -> str | None:
        ok, why = _still_authorized(run)
        if not ok:
            return why
        try:
            now = mroutes._resolve_cwd(project_id)[1]
        except missions.MissionError as e:
            return str(e)
        if not now or os.path.realpath(now) != pinned.get("real"):
            return "the project's folder changed while the launch was being authorised"
        return _real_still_pinned(pinned) or None

    return check


async def _start_mission(run: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
    from .routes import missions as mroutes

    rid = run["id"]
    action = config["action"]
    pinned = pins.get("cwd") or {}
    # THE EFFECT LOCK is held from the final authority read through create → plan → dispatch, so
    # an operator's disable/pause/edit either lands first (and this refuses) or reports in_flight.
    async with effect_lock.runner(run["automation_id"]) as held:
        if not held:
            return "skipped", LOCK_BUSY
        stop = await asyncio.to_thread(_authority_outcome, run)
        if stop:
            return stop
        try:
            project_id, cwd = await asyncio.to_thread(mroutes._resolve_cwd, action["project_id"])
        except missions.MissionError as e:
            return "refused", str(e)
        drift = await asyncio.to_thread(_identity_drift, pinned, cwd)
        if drift:
            await asyncio.to_thread(_flag, run, drift)
            return "refused", f"{drift} — it needs your approval again"
        if not await asyncio.to_thread(scope_ok, cwd):
            return "refused", "the project's folder is outside your project folders"
        try:
            text, inputs = await asyncio.to_thread(_render_plain, config, pins)
        except RunRefused as e:
            return "refused", e.detail
        await asyncio.to_thread(store.set_inputs, rid, inputs)
        # THE INTENT IS RECORDED BEFORE THE MISSION EXISTS: a crash between the create and the
        # link below leaves a run that says a mission may exist, never one that invents or omits it.
        await asyncio.to_thread(store.add_step, rid, store.MISSION_PENDING_STEP, project_id or "")
        row = await missions.run_admitted(
            lambda: missions.create_mission(
                text,
                title=config["name"],
                project_id=project_id,
                cwd=cwd,
                playbook_id=_playbook_for(action, pins),
            )
        )
        mid = row["id"]
        await asyncio.to_thread(store.link, rid, mission_id=mid)
        await asyncio.to_thread(store.add_step, rid, "mission_created", mid)
        # The create route's own background producers, awaited here because the run needs them.
        await mroutes._produce_for_new_mission(mid)
        plan = await missions.run_admitted(lambda: missions.get_plan(mid))
        mission = await missions.run_admitted(lambda: missions.get_mission(mid))
        objectives = (mission or {}).get("objectives") or []
        await asyncio.to_thread(
            store.add_step,
            rid,
            "planned" if plan else "not_planned",
            f"engine {plan.get('engine')}, {len(objectives)} objectives"
            if plan
            else f"planning: {(mission or {}).get('plan_state') or 'unknown'}",
        )
        drift = await missions.run_admitted(lambda: _checklist_drift(mid, pins))
        if drift:
            return "refused", drift
        if action["autonomy"] == "propose":
            if plan is None:
                return "failed", "the mission was created but no plan was produced"
            _delivered(rid, "ok", "mission planned — waiting for you to dispatch it")
            return _DELIVERED[rid]
        if plan is None:
            return "failed", "the mission was created but no plan was produced, so nothing started"
        # …AND AGAIN before the spawn: planning takes a model call, and a change that landed while
        # it ran must stop the launch (the fence below checks once more).
        stop = await asyncio.to_thread(_authority_outcome, run)
        if stop:
            return stop
        try:
            fresh = (await asyncio.to_thread(mroutes._resolve_cwd, plan.get("project_id")))[1]
        except missions.MissionError as e:
            return "refused", str(e)
        drift = await asyncio.to_thread(_identity_drift, pinned, fresh)
        if drift:
            await asyncio.to_thread(_flag, run, drift)
            return "refused", f"{drift} — it needs your approval again"
        resp = await mroutes.dispatch_approved(
            mid,
            {
                "plan_id": plan["plan_id"],
                # The PINNED folder is the comparand, so the route's own fence refuses a project
                # that moved in the minutes the planner took.
                "expect_cwd": pinned.get("path") or "",
                "expect_objectives": missions.objectives_digest(objectives),
            },
            registry=registry,
            # Automated missions never launch permission-bypassed (Phase 1).
            bypass_ceiling=False,
            # Launch in the pinned REAL path, compared inside the fence.
            pinned_real_cwd=pinned.get("real"),
            authorize=_mission_fence(run, pinned, plan.get("project_id")),
        )
        try:
            data = json.loads(bytes(resp.body))
        except ValueError:
            data = {}
        if resp.status_code != 200:
            return "refused", str(data.get("detail") or f"dispatch refused ({resp.status_code})")
        if data.get("state") == "running":
            _delivered(rid, "started", "mission dispatched and running")
        if data.get("session_key"):
            await asyncio.to_thread(store.link, rid, session_key=str(data["session_key"]))
        if data.get("state") != "running":
            outcome = "refused" if data.get("outcome") == "refused" else "failed"
            return outcome, str(data.get("reason") or "the dispatch did not start")
        await asyncio.to_thread(
            store.add_step, rid, "dispatched", str(data.get("session_key") or "")
        )
        if action["autonomy"] == "dispatch_auto_choose":
            # A GRANT, so it is an effect like any other: still under the lock, after a fresh
            # authority read. The mission already dispatched keeps running either way.
            stop = await asyncio.to_thread(_authority_outcome, run)
            if stop:
                await asyncio.to_thread(
                    store.add_step,
                    rid,
                    "auto_choose_skipped",
                    f"auto-choose not granted: {stop[1].removeprefix('stopped: ')}",
                )
            else:
                try:
                    await missions.run_admitted(lambda: missions.set_auto_choose(mid, True))
                    await asyncio.to_thread(
                        store.add_step, rid, "auto_choose", "menu answers opted in"
                    )
                except missions.MissionError as e:
                    await asyncio.to_thread(store.add_step, rid, "auto_choose_refused", str(e))
    return "started", "mission dispatched and running"


def session_cwd_drift(pinned: dict, folder: str) -> str:
    """Why the session folder no longer resolves to the approved directory, or ``""``."""
    if _identity_drift(pinned, folder):
        return "the session folder changed since you approved this automation"
    return ""


async def _start_session(run: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
    rid = run["id"]
    action = config["action"]
    pinned = pins.get("cwd") or {}
    async with effect_lock.runner(run["automation_id"]) as held:
        if not held:
            return "skipped", LOCK_BUSY
        stop = await asyncio.to_thread(_authority_outcome, run)
        if stop:
            return stop
        ok, why = await asyncio.to_thread(engine_state, action["engine"])
        if not ok:
            return "refused", why
        drift = await asyncio.to_thread(session_cwd_drift, pinned, action["folder"])
        if drift:
            await asyncio.to_thread(_flag, run, drift)
            return "refused", f"{drift} — it needs your approval again"
        real = pinned["real"]
        ok, why = await asyncio.to_thread(folder_ok, real)
        if not ok:
            return "refused", why
        try:
            text, inputs = await asyncio.to_thread(_render_plain, config, pins)
        except RunRefused as e:
            return "refused", e.detail
        await asyncio.to_thread(store.set_inputs, rid, inputs)
        if registry is None:
            return "refused", "the session runner isn't available in this process"

        def on_key(key: str) -> None:
            # BEFORE anything is spawned: a record naming a session that never came to be is
            # honest, one missing a session that did is not.
            store.link(rid, session_key=key)

        def authorize(_epoch) -> str | None:
            # INSIDE the launch fence, immediately before the spawn: still authorized, and the
            # approved directory is still exactly itself and inside the boundary. The launch uses
            # the pinned REAL path, so a repointed symlink cannot redirect it.
            ok, why = _still_authorized(run)
            if not ok:
                return why
            moved = _real_still_pinned(pinned)
            if moved:
                return moved
            ok, why = folder_ok(real)
            return None if ok else why

        try:
            out = await headless_dispatch.dispatch(
                engine=action["engine"],
                cwd=real,
                brief=text,
                registry=registry,
                bypass=bool(action["bypass"]),
                on_key=on_key,
                authorize=authorize,
            )
        except headless_dispatch.DispatchError as e:
            return "refused", str(e)
    if out.ok:
        _delivered(rid, "ok", "session started and briefed")
    for fact in ("launched", "started", "briefed"):
        if getattr(out, fact):
            await asyncio.to_thread(store.add_step, rid, fact, out.key)
    if out.bound is True:
        await asyncio.to_thread(store.add_step, rid, "bound", out.bound_key)
        await asyncio.to_thread(store.link, rid, session_key=out.bound_key)
    if out.ok:
        return _DELIVERED[rid]
    reason = out.reason or f"the session reached {out.state}"
    return ("failed" if out.launched else "refused"), reason


_SEND_OUTCOME = {
    "delivered": ("ok", "delivered"),
    "not_live": ("refused", "the session isn't running — nothing was started instead"),
    "stale": ("refused", "something changed before sending"),
    "refused": ("refused", "the session was busy"),
    "failed": ("failed", "nothing reached the session"),
    "timeout": ("failed", "the write timed out"),
    # Part of the paste may be in front of the agent: typed, not submitted.
    "aborted": ("partial", store.PARTIAL_REASON),
}


def _send_text(run: dict, phys: str, cwd: str | None, text: str) -> tuple[str, str]:
    def guard() -> tuple[bool, str]:
        if not scope_ok(cwd):
            return False, "the session is outside your project folders"
        return _still_authorized(run)

    # ONE write (the paste and its Enter together), but it still takes the session's send lock so
    # it can never land between another sender's clear / paste / Enter.
    try:
        with template_send.session_send_lock(phys):
            out = session_input.send_input(
                phys,
                session_input.bracketed_paste(text),
                precondition=guard,
                final_guard=guard,
                require_quiet=True,
            )
    except template_send.SendRefused as e:
        if e.busy:
            return "skipped", SEND_BUSY
        if e.lock_unavailable:
            return "skipped", SEND_LOCK_UNAVAILABLE
        raise
    outcome, words = _SEND_OUTCOME.get(out.state, ("failed", out.state))
    if outcome in ("ok", "partial") or not out.detail:
        return outcome, words
    return outcome, f"{words}: {out.detail}"


def _masked_template(template_id: str, values: dict) -> str:
    """The message as the operator may see it — secrets as ``[secret: name]`` — computed WITHOUT
    reading any secret, so it can be recorded before a byte is sent."""
    t = tstore.get_template(template_id)
    names = [f["name"] for f in t["fields"] if f["kind"] == "secret"]
    return template_send.render(
        t,
        values,
        library=template_vars.values(),
        secrets={n: template_send.mask(n) for n in names},
        secret_state=dict.fromkeys(names, "ok"),
    ).masked


def _send(run: dict, config: dict, pins: dict) -> tuple[str, str]:
    """BLOCKING — the whole send, off the event loop, under the effect lock."""
    with effect_lock.runner_sync(run["automation_id"]) as held:
        if not held:
            return "skipped", LOCK_BUSY
        return _send_locked(run, config, pins)


def _send_locked(run: dict, config: dict, pins: dict) -> tuple[str, str]:
    rid = run["id"]
    action = config["action"]
    key = action["session_key"]
    msg = action["message"]
    stop = _authority_outcome(run)
    if stop:
        return stop
    try:
        phys, cwd = template_send.resolve_target(key)
    except template_send.SendRefused as e:
        return "refused", e.detail
    # Linked for the run record, never as an ORIGIN: typing into a session did not start it.
    store.link(rid, session_key=key, origin=False)
    if not session_input.is_live(phys):
        return "refused", "the session isn't running — nothing was started instead"
    if "text" in msg:
        store.set_inputs(rid, {"message": msg["text"]})  # recorded BEFORE the bytes
        outcome, reason = _send_text(run, phys, cwd, msg["text"])
        if outcome == "ok":
            _delivered(rid, outcome, reason)
        return outcome, reason
    tpin = pins.get("template") or {}
    try:
        masked = _masked_template(msg["template_id"], msg["values"])
    except tstore.TemplateNotFound:
        return "refused", "the template was deleted"
    except (tstore.TemplateError, template_send.SendRefused) as e:
        return "refused", str(getattr(e, "detail", None) or e)
    # MASKED — `[secret: name]` — is all a run record ever holds, and it is recorded BEFORE the
    # delivery so nothing after the bytes can fail the run.
    store.set_inputs(rid, {"message": masked, "template_id": msg["template_id"]})
    stop = _authority_outcome(run)
    if stop:
        return stop
    try:
        template_send.send(
            msg["template_id"],
            {
                "session": key,
                "values": msg["values"],
                "expected_updated_at": tpin.get("updated_at"),
            },
            # Defence in depth: the automation's authority, re-checked as EVERY write's guards.
            extra_guard=lambda: _still_authorized(run),
        )
    except template_send.SendRefused as e:
        if e.busy:
            return "skipped", SEND_BUSY
        if e.lock_unavailable:
            return "skipped", SEND_LOCK_UNAVAILABLE
        if getattr(e, "partial", False):
            return "partial", store.PARTIAL_REASON
        return ("failed" if e.status >= 500 else "refused"), e.detail
    except tstore.TemplateNotFound:
        return "refused", "the template was deleted"
    except tstore.TemplateError as e:
        return "refused", str(e)
    _delivered(rid, "ok", "delivered")
    return "ok", "delivered"


async def _send_to_session(run: dict, config: dict, pins: dict, *, registry) -> tuple[str, str]:
    return await asyncio.to_thread(_send, run, config, pins)


_ACTIONS = {
    "start_mission": _start_mission,
    "start_session": _start_session,
    "send_to_session": _send_to_session,
}


# ---- execution ----------------------------------------------------------------------------------


async def _execute(run: dict, *, registry) -> tuple[str, str]:
    scope = run.get("scope") or {}
    config, pins = scope.get("config"), scope.get("pins") or {}
    if not config:
        return "refused", "this run's approved scope could not be read"
    # THE APPROVED INPUTS, compared before any effect (#1201 round 2).
    try:
        current = await asyncio.to_thread(model.compute_pins, config)
    except model.PinsUnavailable as e:
        # Could not RESOLVE the inputs now: skipped with its reason and noted, never a
        # re-approval and never a failure — the next run checks again.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(store.set_check_note, run["automation_id"], f"not checked: {e}")
        return "skipped", f"skipped: could not check the approved inputs right now ({e})"
    drift = model.pins_drift(pins, current)
    if drift:
        await asyncio.to_thread(_flag, run, drift)
        return "refused", f"{drift} — it needs your approval again"
    return await _ACTIONS[config["action"]["kind"]](run, config, pins, registry=registry)


async def execute(run: dict, *, registry) -> dict:
    """Run a claimed ``dispatching`` run to an outcome. Never raises for an ordinary failure."""
    rid = run["id"]
    try:
        outcome, reason = await _execute(run, registry=registry)
    except asyncio.CancelledError:
        # Shutdown reached a run mid-effect: whatever it started keeps running on its own, and the
        # record says honestly that the outcome is unknown. Never retried.
        done = _DELIVERED.pop(rid, None)
        outcome, reason = done or ("interrupted", store.INTERRUPTED_REASON)
        with contextlib.suppress(Exception):
            await asyncio.shield(asyncio.to_thread(store.finish_run, rid, outcome, reason))
        raise
    except Exception as e:  # noqa: BLE001 — a crash in one run must not take the loop down
        done = _DELIVERED.get(rid)
        if done is not None:
            # THE EFFECT HAPPENED. What failed is recording its details; reporting `failed` here
            # would count a delivery toward pause-after-failures and invite a retry that repeats it.
            log.warning("automation run %s: delivered, bookkeeping failed", rid, exc_info=True)
            outcome, reason = done[0], f"{done[1]}; recording details failed: {type(e).__name__}"
        elif isinstance(e, missions.MissionError):
            outcome, reason = "refused", str(e)
        else:
            log.warning("automation run %s failed", rid, exc_info=True)
            outcome, reason = "failed", f"the run failed ({type(e).__name__})"
    if outcome in ("refused", "failed") and rid not in _DELIVERED and rid not in _FLAGGED:
        # THE OPERATOR STOPPED IT: a refusal caused by their own disable/pause/edit is `stopped`,
        # never a failure that counts toward pausing or raises "Automation failed".
        with contextlib.suppress(Exception):
            stop = await asyncio.to_thread(_authority_outcome, run)
            if stop and stop[0] == "stopped":
                outcome, reason = stop
    _DELIVERED.pop(rid, None)
    _FLAGGED.discard(rid)
    res = await finish(rid, outcome, reason)
    if res is not None:
        await asyncio.to_thread(notify, res)
    return res or {"run": {"id": rid, "outcome": outcome, "reason": reason}}


#: Retry schedule for recording an outcome (a busy or locked store is transient).
FINISH_BACKOFF_S = (0.05, 0.2, 0.5, 1.0, 2.0)
#: Outcomes that could not be recorded yet: ``{run_id: (outcome, reason)}``. Retried every tick
#: until they land, so a live owner never leaves its own run `dispatching` for ever.
_UNSETTLED: dict[str, tuple[str, str]] = {}


async def finish(rid: str, outcome: str, reason: str) -> dict | None:
    for delay in (*FINISH_BACKOFF_S, None):
        try:
            res = await asyncio.to_thread(store.finish_run, rid, outcome, reason)
        except Exception:  # noqa: BLE001 — retried, then parked for the next tick
            if delay is None:
                log.warning("automation run %s: outcome not recorded yet", rid, exc_info=True)
                _UNSETTLED[rid] = (outcome, reason)
                return None
            await asyncio.sleep(delay)
            continue
        _UNSETTLED.pop(rid, None)
        if res.get("settled") is False:
            # An earlier attempt COMMITTED and only its acknowledgement was lost. The store did the
            # accounting once; what may be missing is the announcement that should follow it.
            return _lost_ack(res, rid)
        return res
    return None  # pragma: no cover


async def retry_unsettled() -> None:
    for rid, (outcome, reason) in list(_UNSETTLED.items()):
        try:
            res = await asyncio.to_thread(store.finish_run, rid, outcome, reason)
        except Exception:  # noqa: BLE001 — next tick
            log.debug("automation run %s: outcome still not recorded", rid, exc_info=True)
            continue
        _UNSETTLED.pop(rid, None)
        if res.get("settled") is False:
            res = _lost_ack(res, rid)
        await asyncio.to_thread(notify, res)


def spawn(run: dict, *, registry) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(
        execute(run, registry=registry), name=f"automation-run:{run['id']}"
    )
    _TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.warning("automation run %s ended with %s", run["id"], type(t.exception()).__name__)

    task.add_done_callback(_done)
    return task


async def drain(timeout: float = DRAIN_TIMEOUT_S) -> None:
    """Shutdown: wait for runs already started; cancel (→ interrupted) what outlives ``timeout``."""
    pending = [t for t in _TASKS if not t.done()]
    if not pending:
        return
    _done, still = await asyncio.wait(pending, timeout=timeout)
    for t in still:
        t.cancel()
    for t in still:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


async def run_now(aid: str, *, registry) -> dict:
    """Run once, now. Refused for a never-approved automation; on a paused one it runs ONCE without
    resuming, after the same consent, expiry and daily-cap checks as every other trigger."""
    if not loop_enabled():
        raise RunRefused(409, "automations are switched off on this server")
    res = await asyncio.to_thread(
        store.begin_run, aid, trigger="manual", slot=f"manual:{uuid.uuid4().hex}", fire_at=None
    )
    if not res["claimed"]:
        if res["reason"] == store.RECEIPT_MISMATCH:
            # Flag it now, not only on the next tick: the refusal and the "Review and approve"
            # the list offers must say the same thing (#1252 review).
            with contextlib.suppress(Exception):
                row = await asyncio.to_thread(store.get, aid)
                await asyncio.to_thread(
                    store.flag_reapproval,
                    aid,
                    store.RECEIPT_MISMATCH,
                    observed_revision=row["revision"],
                    receipt_mismatch=True,
                )
        raise RunRefused(409, res["reason"])
    run = res["run"]
    if run["state"] == "dispatching":
        spawn(run, registry=registry)
    return run


# ---- notifications ------------------------------------------------------------------------------


def _in_bell(key: str) -> bool:
    try:
        return any(
            r.get("action_id") == key for r in notifications.listing().get("notifications") or []
        )
    except Exception:  # noqa: BLE001 — unknown ⇒ announce (fail toward telling the operator)
        return False


def _lost_ack(res: dict, rid: str) -> dict:
    """The announcement a committed-but-unacknowledged settle still owes, and nothing more."""
    run = res.get("run") or {}
    try:
        auto = store.get(run.get("automation_id") or "")
    except Exception:  # noqa: BLE001
        return res
    out = {**res, "automation": auto}
    key = f"automation:{auto['id']}:{rid}"
    if auto["failure_episode"] == key and not _in_bell(key):
        out["episode"] = ("open", key)
    if run.get("outcome") == "partial" and not _in_bell(f"{key}:partial"):
        out["alert"] = f"{key}:partial"
    return out


def _announce(title: str, reason: str, key: str) -> None:
    rec = notifications.add(
        title=title, project="", session_id="", engine="", reason=reason, action_id=key
    )
    if rec and notifications.list_subscriptions():
        with contextlib.suppress(Exception):
            notifications.fanout(rec)


def notify(result: dict) -> None:
    """One bell entry per failure EPISODE; retracted when the automation recovers.

    The episode key is ``automation:<id>:<first failed run id>`` — server-derived, no model text —
    and it is persisted with the automation in the same transaction that opened it, so a restart or
    a second failure never announces the same episode twice. Best-effort: the bell is another store.
    """
    auto = result.get("automation") or {}
    run = result.get("run") or {}
    name = auto.get("name") or "automation"
    alert = result.get("alert")
    if alert:
        # A PARTIAL send: one notification of its own. The automation is already paused.
        try:
            _announce(f"Automation typed but did not submit: {name}", store.PARTIAL_REASON, alert)
        except Exception:  # noqa: BLE001
            log.warning("automation notification for %s failed", alert, exc_info=True)
    ep = result.get("episode")
    if not ep:
        return
    kind, key = ep
    try:
        if kind == "open":
            _announce(
                f"Automation failed: {name}",
                f"{run.get('outcome')}: {run.get('reason') or ''}",
                key,
            )
        else:
            notifications.dismiss_for_action(key)
    except Exception:  # noqa: BLE001 — the run is recorded; the bell must not undo that
        log.warning("automation notification for %s failed", key, exc_info=True)


def retract(key: str) -> None:
    if key:
        with contextlib.suppress(Exception):
            notifications.dismiss_for_action(key)
