"""Mission routes (#846, Phase 1 of #840) — the console's read/write surface over the store.

Every route is ``logged_in``; every state-changing one adds ``csrf_guard`` (which also carries
the Origin/Referer check). No new auth surface, no unauthenticated endpoint.

Three contracts are worth naming here rather than leaving to be discovered:

* **Filter before paginate.** ``q`` / ``project`` / ``state`` narrow the **full** archived-scoped
  set *before* ``limit``/``offset``, so ``total`` and any "load more" describe the *filtered*
  result — the same rule ``/api/sessions`` follows. Facets are computed over the full scoped set
  *before* the filters, so the dropdowns keep listing every option.
* **Admission before submission.** Every DB call goes through :func:`missions.run_admitted`,
  which takes a slot *before* anything reaches the pool and answers **503** over the bound. A
  bounded pool alone bounds running threads, not the queue, and with a five-second SQLite
  ``busy_timeout`` a queue is experienced as a hang rather than as an answer.
* **Errors never carry mission content.** ``instruction`` / ``brief`` are sensitive operator text,
  so a detail names the mission id and the failure kind and nothing else. The store raises
  :class:`missions.MissionError` already shaped that way; :func:`_fail` just maps the status.

``/context`` (#852) lives here too, and inherits **nothing** from the file panel by being
adjacent: the no-store middleware is gated on the ``/api/files/`` and ``/api/git/`` prefixes, so
this route had to be added to that boundary explicitly or it would have served a git status —
which carries absolute paths — as a cacheable response, on the 401 as well as on success.

``/message`` LANDS HERE (#871, split out of #852 after its lifecycle proved to need its own
design pass), and so do ``/answer`` (#892, Phase 3b) — the operator's reply to a bounded
question the supervisor is waiting on — and ``/plan`` and ``/dispatch`` (#893 Phase 4).

Plan and dispatch are deliberately TWO routes: one spends a model call and writes a proposal, the
other starts an UNATTENDED agent, and a single endpoint that did both would be a button whose
consequences the operator learns about afterwards.

**Unattended, and NOT permission-bypassed** (#904 review 3, finding 6). Every interactive launch
in this app bypasses tool prompts; this one deliberately does not, because unattended bypass is a
different grant — an agent with prompts suppressed and nobody watching what it does — and #898
defaults it off precisely so the decision belongs to a layer that knows whether an operator
authorised it. Nobody has. The cost is real and is stated rather than hidden: a dispatched agent
can stop on a tool or trust prompt with no one present, which reads as a stalled session and is
what the supervisor's follow-through is for. Turning it on is its own change with its own
recorded approval, not a default this route picks.

Still absent, on purpose: the playbook templates are edited through ``PATCH /api/prompts`` and
``POST /api/prefs`` rather than through a mission route, because they are operator config rather
than mission state. And there is **no new decision endpoint** — approve/reject stay
``/api/pulse/actions/{id}/approve|reject`` (#840 §14).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from .. import (
    actuator,
    aitasks,
    engines,
    gitpanel,
    headless_dispatch,
    metadata,
    mission_archive,
    mission_directions,
    mission_dispatch,
    mission_fence,
    mission_objectives,
    mission_plan,
    mission_questions,
    mission_relay_reconcile,
    mission_supervisor,
    mission_turn_reconcile,
    missions,
    orchestrator,
    orchestrator_chat,
    orchestrator_ledger,
    prefs,
    projects,
    ptybridge,
    review,
    scopedspawn,
    session_input,
)
from . import files as files_routes

log = logging.getLogger(__name__)


def _fail(e: missions.MissionError) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=e.status)


async def _body(request: Request) -> dict:
    """The request body as an object, or a 422 — **an unparseable body is never an empty one**.

    Swallowing the parse error and returning ``{}`` made malformed JSON *fail open into the
    defaults*, and on this surface the defaults are the effectful ones: a truncated
    ``{"sessions":false`` on the unarchive route parsed as nothing, defaulted `sessions` to
    ``True``, and restored every session — the opposite of what the caller wrote, at 200.

    An absent body is still legitimately empty (several routes take none), so the two cases are
    distinguished rather than merged: no bytes ⇒ ``{}``; bytes that do not parse ⇒ 422, before any
    mutation.
    """
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise missions.MissionError("request body is not valid JSON", status=422) from None
    if not isinstance(payload, dict):
        raise missions.MissionError("request body must be a JSON object", status=422)
    return payload


def _resolve_cwd(project_id: object, *, index: dict | None = None) -> tuple[str | None, str | None]:
    """Resolve the launch path SERVER-side from a project id. The client never supplies one.

    ``cwd`` is the field where getting it wrong is a path-traversal bug rather than a wrong link,
    which is why #840 §4 makes it the server's to author: the model picks an *index* into a
    server-built list and the entity supplies the path. A route that persisted ``body["cwd"]``
    would hand that same authority to any caller, and the schema's launch ``CHECK`` would then be
    satisfied by a path nobody validated — the gate would be guarding a value the client chose.

    Returns ``(project_id, cwd)``. No project ⇒ ``(None, None)``: a draft is *allowed* to have no
    resolved path, and that is exactly the state in which the console asks which project was
    meant.

    ``index`` is a project store the caller already read — the manual first plan passes the one
    yielded by ``projects.locked_index()`` so the check and its write commit under one lock.
    """
    if project_id is None or project_id == "":
        return None, None
    if not isinstance(project_id, str):
        raise missions.MissionError("project_id must be a string", status=422)
    try:
        entity = (projects.load() if index is None else index).get(project_id)
    except Exception:  # noqa: BLE001 — an unreadable projects file is not a 500 here
        raise missions.MissionError("could not resolve the project", status=503) from None
    if entity is None:
        raise missions.MissionError("unknown project", status=404)
    # ARCHIVED IS NOT DISPATCHABLE. An archived project is one the operator has put away; the
    # planner already excludes them from the list it offers, and re-resolving here without the
    # same rule would let a plan made before the archive still launch into it (#904 review 5).
    if getattr(entity, "archived", False):
        raise missions.MissionError("that project is archived", status=409)
    cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
    if not cwd:
        raise missions.MissionError("that project has no folder to work in", status=422)
    return entity.id, cwd


async def _produce_for_new_mission(mission_id: str) -> None:
    """A new mission's two background producers: objectives and the plan (#883, #967).

    Run CONCURRENTLY, because Begin needs both settled and neither needs the other — in sequence
    the plan would wait out the objectives' model call for no reason. Both never raise; the gather
    still collects exceptions so one cannot cancel the other.
    """
    await asyncio.gather(
        mission_objectives.propose_for_new_mission(mission_id),
        mission_plan.propose_for_new_mission(mission_id),
        return_exceptions=True,
    )


def _session_key(raw: object) -> str:
    """Shape-check a session key before it reaches the store or a provider.

    ``engines.parse_key`` is the single gate the whole app uses — it resolves an id to its
    provider and validates the native shape. Canonicalising here means the store only ever holds
    ``<engine>:<native>``, so a bare back-compat Claude UUID and its qualified form can never
    become two different rows fighting over the partial unique index.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise missions.MissionError("session_key is required", status=422)
    try:
        return engines.canonical_key(raw.strip())
    except engines.EngineError:
        raise missions.MissionError("unknown session id", status=404) from None


async def _settle_relay(mission_id: str, action_id: str, state: str, detail: str) -> None:
    """Stamp a relay record's outcome. Best-effort, and never raises into a response path.

    The record is durable before the bytes, so a failure here leaves a row saying `sending` —
    which `mission_relay_reconcile` resolves on the next read from the ledger. Raising would
    replace a recoverable record with a 500 over a delivery that already happened.
    """
    with contextlib.suppress(Exception):
        await missions.run_admitted(
            lambda: missions.settle_relay_event(
                mission_id, action_id=action_id, state=state, detail=detail
            )
        )


async def _session_exists(key: str) -> bool:
    """Is there a real session behind this key? (#896 review 11, finding 1)

    `_session_key` proves the key is well FORMED, which is a different question — and the gap
    between them was reachable: a syntactically perfect `claude:<uuid>` naming nothing at all was
    adopted, and the mission then reached `running` on the strength of a row it had just written
    about a session that does not exist. `planned -> running` means "work is under way"; a
    fabricated key makes that a claim with nothing behind it, and the supervisor then follows
    through on it.

    TWO independent proofs, either of which is enough, because a session can legitimately be one
    without the other:

    * the ENGINE'S OWN STORE knows it — a transcript, a row — which is #898's condition 5 and the
      evidence that survives a restart;
    * a live master answers for it — a session started moments ago whose store record has not
      landed yet is still a real session to attach to.

    An unreadable store answers "no evidence", never "yes": #898's helper already swallows its own
    errors that way. That direction is the safe one here — a refused adoption is retryable and
    says why, while an accepted one silently underwrites a `running` mission.

    **AND AN ARCHIVED SESSION IS NOT ONE TO ADOPT** (#896 review 15, finding 1). `scanner.scan()`
    returns live **and** archived rows — deliberately, because the sidebar lists both — so "the
    store knows it" was satisfied by a session that has been put away. The mission then reached
    `running` with no live or resumable agent behind it, which is the same claim-with-nothing-
    behind-it the existence check was added to stop, one door along.

    **The archive question is asked FIRST, and it can refuse on its own** (review 16, findings 2
    and 3). Two things were wrong with asking it second:

    * a live writer short-circuited it. `is_live` returned early, and an archived session CAN
      still have one — the archive route suppresses runtime-cleanup failures and archives the
      record anyway — so a teardown that left a writer behind turned an archived record into a
      `running` mission;
    * an unreadable sidecar answered "not archived". `metadata.load()` collapses missing,
      corrupt and unreadable into `{}`, and for the engines whose archive lives ONLY in the
      sidecar the provider row then says `archived=False` — so the gate accepted an archived
      session precisely when it could not tell. `metadata.archive_override_under_lock()` is
      that missing answer, and it takes the WRITER'S OWN FLOCK (review 17, finding 3): `patch()`
      truncates in place before serializing, so every ordinary edit has a window in which the
      file exists and is zero bytes, and a lock-free reader calls that "no override".

    The precedence within the archive question is the app's own — the sidecar override wins where
    it is set, the engine's scan answers otherwise — so this asks `mission_archive` rather than
    growing a second opinion about what archived means. Anything but a definite "not archived"
    refuses, because this is the direction where being wrong underwrites a running mission.
    """
    try:
        prov, native = engines.parse_key(key)
    except Exception:  # noqa: BLE001
        return False

    def _adoptable() -> bool:
        # THE ARCHIVE QUESTION, BEFORE EITHER KIND OF EVIDENCE, and from ONE locked snapshot.
        #
        # FOUR ANSWERS, because the sidecar can say four things and two of them used to arrive
        # as one (review 29). "Nobody recorded an override" and "the operator explicitly set this
        # ACTIVE" both read as `False`, so both fell through to the engine's own store — and for
        # opencode, whose store this app may not write, UNARCHIVE deliberately sets only the
        # sidecar and leaves the native `time_archived` in place. The session moved into the
        # active list and ADOPT went on 404ing against a native flag the operator had already
        # overridden.
        sidecar = metadata.archive_override_under_lock(f"{prov.engine_id}:{native}")
        if sidecar == "unreadable":
            log.warning("refusing to adopt %s: the metadata sidecar could not be read", key)
            return False
        if sidecar == "archived":
            return False
        # …and where the sidecar says nothing, the ENGINE'S own view decides — the same
        # precedence the sidebar and `mission_archive` use, with the override already applied
        # above. **In THREE answers, not two** (#896 review 22).
        #
        # `_effective_archived` collapses "the store has no row for this session" and "the store
        # could not be read" into one `None`, and this gate refused on both. Those are opposite
        # facts: a store that cannot be read must not underwrite an adoption, while a store with
        # no row is the ordinary state of a session started moments ago — some engines persist
        # only after the first turn. The documented second proof, a live writer, was therefore
        # unreachable, and a real live UNTRACKED session could not be adopted at all, potentially
        # for as long as it stayed silent.
        # …and the ENGINE'S OWN ARCHIVE, as a question that can say "I could not tell" (#896
        # reviews 23 and 24). `scan()` cannot answer the negative half — every provider swallows
        # its read failures, so "no archived row" and "could not look" arrive identically, and a
        # gate built on it accepted a live writer while an archived row may have been hidden.
        #
        # …AND ONLY WHERE THE SIDECAR IS SILENT. An explicit `active` is the operator's own
        # answer to this exact question, and the override winning where it is set is the app's
        # precedence everywhere else (the sidebar, `mission_archive`); consulting the engine
        # after it would be a second opinion this gate has no business forming.
        if sidecar == "unset":
            state = engines.archive_state(prov, native)
            if state != "not-archived":
                if state == "unreadable":
                    log.warning("refusing to adopt %s: the engine's store could not be read", key)
                return False
        # …THEN EITHER PROOF IS ENOUGH, because a session can legitimately be one without the
        # other. `scan()` is the right source HERE and its fail-soft answer is fine, because it is
        # used only as POSITIVE evidence: a semantically valid row proves the session exists, and
        # not finding one simply means the live writer has to prove it instead — which is the
        # ordinary state of a session started moments ago, and of every engine that pins its id
        # before it writes anything (review 24, finding 1).
        if headless_dispatch._has_store_record(prov, native, ""):
            return True
        return session_input.is_live(engines.physical_key(key))

    return await asyncio.to_thread(_adoptable)


def _reap_dead_spawns(mission_id: str) -> int:
    """Close the ledger rows of sub-agents whose master is PROVABLY gone. Returns how many.

    The other half of the sub-agent budget (#894 review 1, finding 4). `claim_spawn` reserves a
    slot and only evidence returns it — so without this, a child that finished normally would hold
    its slot for the life of the mission and the resource guard would harden into a permanent
    quota. Release cannot do this job: `detach` is an ownership operation that stops nothing, and
    treating it as a kill would be a worse bug than the one being fixed.

    **Only `DEAD` frees a slot.** `probe_master` is tri-state on purpose: `UNKNOWN` means every
    attempt timed out, which on a starved host is exactly what a live master looks like. Reading
    it as dead would hand back capacity to a process that is still running — the same
    "absence of an answer is not an answer" mistake the store reads elsewhere are careful about.
    Best-effort throughout: a probe that raises leaves its row alone.
    """
    freed = 0
    try:
        rows = missions.open_spawns(mission_id)
    except Exception:  # noqa: BLE001 — an unreadable ledger is not permission to free anything
        log.debug("mission %s: spawn ledger unreadable; freeing nothing", mission_id)
        return 0
    for r in rows:
        key = str(r.get("session_key") or "")
        if not key:
            # Reserved but never launched: the settlement path owns that row, not this one.
            continue
        try:
            engine, native = engines.parse_key(key)
            verdict = ptybridge.probe_master(ptybridge.socket_path(engine.engine_id, native))
        except Exception:  # noqa: BLE001
            log.debug("mission %s: could not probe %s", mission_id, key)
            continue
        if verdict is not ptybridge.DEAD:
            continue
        try:
            if missions.close_spawn(r["plan_id"], reason=f"{key} is no longer running"):
                freed += 1
        except Exception:  # noqa: BLE001
            log.debug("mission %s: could not close spawn %s", mission_id, r["plan_id"])
    return freed


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    @app.get("/api/missions")
    async def list_missions_route(
        request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        qp = request.query_params
        archived = (qp.get("archived") or "").lower() in ("1", "true", "yes")

        def _read():
            out = missions.safe_list_missions(
                q=qp.get("q") or "",
                project_id=qp.get("project") or "",
                state=qp.get("state") or "",
                archived=archived,
                limit=_int(qp.get("limit"), missions.LIST_LIMIT_DEFAULT),
                offset=_int(qp.get("offset"), 0),
            )
            # Derived at read time, never stored — see `missions.derive_needs_you`.
            with contextlib.suppress(Exception):
                flags = missions.derive_needs_you([m["id"] for m in out["missions"]])
                for m in out["missions"]:
                    f = flags.get(m["id"], {})
                    m["needs_you"] = bool(f.get("needs_you"))
                    m["needs_you_why"] = f.get("why") or []
            return out

        try:
            return JSONResponse(await missions.run_admitted(_read))
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions")
    async def create_mission_route(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
            # `cwd` is rejected as route input outright — the same treatment `guard_suffix` gets
            # in the prompt registry, and for the same reason: a field the server must author
            # cannot also be a field the client may send. Refusing it is louder than ignoring it,
            # so a client written against the old shape finds out rather than silently launching
            # somewhere else.
            if "cwd" in body:
                return _fail(
                    missions.MissionError(
                        "cwd is resolved from the project server-side and is not accepted here",
                        status=422,
                    )
                )
            project_id, cwd = _resolve_cwd(body.get("project_id"))
            row = await missions.run_admitted(
                lambda: missions.create_mission(
                    body.get("instruction") or "",
                    title=body.get("title") or "",
                    project_id=project_id,
                    cwd=cwd,
                    playbook_id=body.get("playbook_id"),
                )
            )
            # THE OBJECTIVE PRODUCER'S CALL SITE (#883). A mission is created with an empty
            # checklist and then filled, rather than created-and-filled in one step, and the
            # ordering is the design:
            #
            # * the 201 does not wait on a model call, so a slow or dead endpoint delays nobody
            #   and cannot fail the create;
            # * it is a `BackgroundTask` on the response rather than a bare `create_task`, so the
            #   app owns its lifetime — a detached task can be garbage-collected mid-flight, and
            #   its failures surface as an "exception was never retrieved" warning nobody reads;
            # * `propose_for_new_mission` never raises: every outcome it cannot deliver is a
            #   timeline event on the mission instead.
            #
            # THE PLANNER RIDES THE SAME TASK (#967). Its intent (`plan_state='pending'`) was
            # written by `create_mission` in the transaction above, so this task is the first
            # attempt at discharging it — not the only record that it is owed.
            return JSONResponse(
                row,
                status_code=201,
                background=BackgroundTask(_produce_for_new_mission, row["id"]),
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.get("/api/missions/{mission_id}")
    async def get_mission_route(
        mission_id: str, request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        qp = request.query_params
        before = qp.get("events_before_seq")

        def _read():
            return missions.get_mission(
                mission_id,
                events_limit=_int(qp.get("events_limit"), missions.EVENTS_PAGE_DEFAULT),
                events_before_seq=_cursor(before),
                # ONE SNAPSHOT FOR THE WHOLE ANSWER (#900 review 8, finding 1). The attention
                # projection and the actionable question are read inside the same transaction as
                # the mission row and its timeline — see `get_mission`.
                attention=True,
            )

        try:
            missions.validate_id(mission_id)
            row = await missions.run_admitted(_read)
        except missions.MissionError as e:
            return _fail(e)
        except Exception:  # noqa: BLE001
            # NOT SUPPRESSED, and not a 404 either (#900 review 5, finding 6, carried forward).
            # "we could not look" and "there is nothing there" are different answers, and the
            # console renders its own error for the first rather than a mission that silently
            # lost its question.
            return _fail(missions.MissionError("the mission could not be read", status=503))
        if row is None:
            return JSONResponse({"detail": f"unknown mission {mission_id}"}, status_code=404)
        # A RELAY RECORD THAT OUTLIVED ITS REQUEST is resolved here, from the ledger row under the
        # same action id (#903 review 2, finding 3). The record is written before the bytes, so a
        # process that exits in that window leaves one saying `sending` — a claim about NOW that
        # is wrong by the next minute. Read-time, like `needs_you` and the supervisor reading: a
        # projection refreshed only at boot is wrong for as long as the process has been up.
        with contextlib.suppress(Exception):
            if await asyncio.to_thread(
                mission_relay_reconcile.reconcile, mission_id, row.get("events") or []
            ):
                fresh = await missions.run_admitted(_read)
                if fresh is not None:
                    row = fresh
        # THE SUPERVISOR'S READING (#885), derived at read time like the attention snapshot
        # below and for the same reason: it is a projection of the ledger and the objective
        # store, so caching it would just be a second copy that can disagree with both.
        #
        # Suppressed rather than fatal: a mission's page must still render when the ledger is
        # unreadable. The console shows nothing rather than something wrong, which is the same
        # posture `MissionObjectives` already takes for a probe that could not run.
        with contextlib.suppress(Exception):
            row["supervisor"] = await missions.run_admitted(
                lambda: mission_supervisor.assess(mission_id)
            )
        # THE SUB-AGENT CAP, so the console can refuse a tap that cannot land (#894). It is a
        # SERVER constant — the client may not choose it and does not enforce it; the claim
        # transaction is the only thing that does. Sent because "0 of 2 used" and a disabled
        # button are a better answer than a 409 the operator only discovers by pressing, which is
        # the same reason the plan card computes `ready` instead of letting DISPATCH fail.
        row["spawn_cap"] = missions.SPAWN_CAP
        # THE SAME COUNT THE CLAIM ENFORCES (#894 review 1, finding 5). The console used to
        # derive its own from roster roles while `claim_spawn` counted something else, so
        # the two disagreed by exactly one — "1 of 2 used" over a mission the server would
        # refuse. Published from the ledger so there is one definition, not two that agree
        # by coincidence. A read failure leaves the field absent rather than 0: an unknown
        # budget must not render as an empty one.
        with contextlib.suppress(Exception):
            live = await missions.run_admitted(lambda: missions.open_spawn_count(mission_id))
            # AT THE CAP, RECONCILE BEFORE REPORTING (#894 review 2, finding 4).
            #
            # The only production caller of the reaper was POST `/spawn` — and the console
            # disables the control that issues it once the count reaches the cap. So a mission
            # whose children had all finished normally stayed at the cap for ever: the count never
            # refreshed because the only thing that refreshed it was the thing the count disabled.
            # A deadlock built out of two correct-looking halves.
            #
            # Run ONLY at the cap, so the common path costs nothing and the probes are bounded by
            # SPAWN_CAP rather than by the roster. UNKNOWN still holds its slot — reconciling more
            # often does not make a starved host's silence into evidence.
            if live >= missions.SPAWN_CAP:
                with contextlib.suppress(Exception):
                    if await asyncio.to_thread(_reap_dead_spawns, mission_id):
                        live = await missions.run_admitted(
                            lambda: missions.open_spawn_count(mission_id)
                        )
            row["spawn_live"] = live
        # WHERE A SUB-AGENT WOULD RUN, resolved the same way the spawn route resolves it
        # (#894 review 1, finding 1). The panel has to SHOW the directory it is asking the
        # operator to approve, and the tap has to assert that same value back, or the
        # approval binds to nothing. `null` when the project no longer resolves — the
        # control then has nothing to promise and withholds itself, rather than offering a
        # START that the route would refuse anyway.
        # ELIGIBILITY IS OPTIONAL; THE MISSION READ IS NOT (#894 review 3, finding 5).
        #
        # `_resolve_cwd` RAISES for an unknown, archived or folderless project — deliberately, in
        # the launch path, where refusing is the whole point. Calling it unconditionally here put
        # that refusal in front of an ordinary detail read: a mission whose project was since
        # archived or deleted answered **HTTP 500**, so its history, timeline and roster became
        # unreachable because a control it was never going to be offered could not be computed.
        #
        # The mission is the thing being read. A spawn offer that cannot be made is `null` plus a
        # reason, not an error — and the launch path keeps refusing exactly as strictly as before,
        # because it calls the same resolver on its own.
        row["spawn_cwd"] = None
        row["spawn_unavailable"] = None
        try:
            row["spawn_cwd"] = _resolve_cwd(row.get("project_id"))[1] or None
            if row["spawn_cwd"] is None:
                row["spawn_unavailable"] = "this mission's project has no folder to work in"
        except missions.MissionError as e:
            row["spawn_unavailable"] = str(e)
        except Exception:  # noqa: BLE001 — never let an offer's absence break the read
            row["spawn_unavailable"] = "the project could not be resolved"
        # WHICH AGENT A SUB-AGENT WOULD BE, derived from the session this mission is
        # actually holding (#894 review 1, finding 3).
        #
        # The console used to gate on `mission.engine || mission.plan.engine`, and after a
        # NORMAL create -> plan -> dispatch both are empty: the create route stores no
        # engine, `claim_plan` puts it on the dispatch row and deletes the plan, and a
        # successful settlement deletes the dispatch row. So the control was absent in the
        # one flow it exists for, and only the browser tests' injected `engine: "claude"`
        # hid it. Deriving it from the held session asks the thing that is still true.
        #
        # Re-checked against the capability allowlist rather than trusted from the key: a
        # session adopted from the sidebar can be any engine, including `shell` and the
        # mint-own-id ones the spawn route refuses. Offering one would be a button that
        # can only 409.
        row["spawn_engine"] = None
        try:
            allowed = {str(o.get("id")) for o in mission_plan.engine_options()}
            for sess in row.get("sessions") or []:
                if sess.get("removed_at") is not None:
                    continue
                eng = str(sess.get("engine") or "")
                if not eng:
                    key = str(sess.get("session_key") or "")
                    eng = key.split(":", 1)[0] if ":" in key else ""
                if eng in allowed:
                    row["spawn_engine"] = eng
                    break
        except Exception:  # noqa: BLE001 — an unreadable option list is "no offer", not a 500
            log.debug("mission %s: spawn engine could not be derived", mission_id)
        # THE OPEN TURN comes back inside `get_mission`'s own transaction (#902 review 2,
        # finding 3), so it is never read here. A second read on a second connection was a torn
        # answer in both directions — a claim between them returned a turn with no operator
        # event, a settlement between them returned `turn: null` beside a timeline that did not
        # yet carry the answer — and, worse, its failure was SUPPRESSED, so a store error and a
        # settled turn arrived at the client as the same thing: no field. The client cleared a
        # live "still working" row on it. One snapshot, one field, and a read failure now fails
        # the read rather than quietly meaning "settled".
        #
        # THE ATTENTION FLAG, THE QUESTION AND THE TIMELINE ALL CAME FROM `_read` — one
        # transaction, one answer (#900 review 8, finding 1). There is deliberately no second
        # read here: every version of one produced a page that contradicted itself, and the last
        # one was subtle enough to pass its own regression — the flag and the question agreed
        # with each other while disagreeing with the events array beside them.
        #
        # THE DISPATCH PROPOSAL (#893) is the one thing read separately, and it is a different
        # kind of fact: it is not derived from the mission's own rows, it carries the CURRENT
        # project and engine lists an override needs, and nothing on the page is cross-checked
        # against it. A stale plan is a stale proposal, never a page that contradicts itself.
        with contextlib.suppress(Exception):
            plan = await missions.run_admitted(lambda: missions.get_plan(mission_id))
            if plan is not None:
                plan["project_options"] = mission_plan.picker_project_options()
                plan["engine_options"] = mission_plan.engine_options()
            elif row.get("plan_state") in ("skipped", "failed"):
                # PLAN MANUALLY NEEDS SOMETHING TO CHOOSE FROM (#967 P2b). The lists rode only on
                # a stored plan, so the one mission that has to be planned by hand — no plan, and
                # planning `skipped` or `failed` — had no project or agent to offer. Attached in
                # exactly the states `PATCH /plan` accepts a first plan in, and built on the rules
                # its validation uses, so an offered choice is one the save accepts. The OPERATOR'S
                # list, uncapped: the model's 40-project cap hid a mission's own project (#984).
                row["plan_options"] = {
                    "project_options": mission_plan.picker_project_options(),
                    "engine_options": mission_plan.engine_options(),
                }
            row["plan"] = plan
        return JSONResponse(row)

    @app.post("/api/missions/{mission_id}/adopt")
    async def adopt_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
            key = _session_key(body.get("session_key"))
            # `body.get("role") or "primary"` silently turned a malformed falsy value (`{}`,
            # `[]`, `0`) into the default instead of rejecting it. Absent means default; present
            # means it has to be a real string the store recognises.
            role = "primary" if body.get("role") is None else body.get("role")
            # THE RESERVATION SPANS BOTH HALVES (#896 review 19, finding 2).
            #
            # Eligibility was decided in one moment and the row inserted in another, and the
            # archive routes hold their own reservation only until the provider settles — so
            # `eligible → archive → release → adopt` inserted a session that had been archived
            # in between, and the adopt transaction had nothing left to notice it by. A check is
            # a statement about the past; the reservation is the mutex the sibling routes already
            # take, so taking it here is what makes the two exclude each other.
            #
            # It is a ROW, not a held lock, so it composes with the roster fence below rather
            # than ordering against it: `reserve_session` commits and returns, and nothing here
            # holds the store's write lock across `session_input`'s.
            #
            # Held as `mission:<id>`, which is the holder `_adopt_tx` recognises as its own — so
            # it still refuses a reservation belonging to anybody else, and a session another
            # mission owns is still the store's 409 naming that mission rather than this one.
            token = await missions.run_admitted(
                lambda: missions.reserve_session(key, f"mission:{mission_id}")
            )
            try:
                # …AND RENEWED WHILE IT IS HELD (#896 review 20, finding 1). A reservation is
                # reclaimable after `RESERVATION_MAX_AGE_S` without proof of life, and the scan
                # below is unbounded provider and filesystem work — so a slow engine store turned
                # the mutex into a five-minute bet: an archive could reclaim it, archive the
                # session, release, and this insert would still land. `holding` beats on its own
                # thread for exactly that, and the archive route already takes it. The expiry is
                # a property of the WORK's duration, so both sides of a mutex have to defend
                # against it or only one of them is fenced.
                async with missions.holding(key, token):
                    # EXISTENCE, not just shape (#896 review 11, finding 1). Checked here rather
                    # than in the store, because "is there a session" is a question about the
                    # ENGINES and the store deliberately does not know about them. Inside the
                    # fence, so the answer is still true when the row is written.
                    if not await _session_exists(key):
                        return _fail(
                            missions.MissionError(f"no session {key} exists to adopt", status=404)
                        )

                    def _adopt():
                        # UNDER THE SAME FENCE THE QUESTION TAKES (#900 review 6, finding 1). A
                        # question locks the sessions it knows about, which cannot order it
                        # against an ADOPTION — the session being adopted is by definition not in
                        # that set. Both sides take the roster's own pseudo-key, so "the roster is
                        # changing" and "a question is being committed against it" cannot
                        # interleave.
                        with session_input.sessions_transaction(
                            [mission_fence.roster_key(mission_id), engines.physical_key(key)]
                        ):
                            # THE TOKEN, not just the lock (#896 review 21). The heartbeat above
                            # keeps the claim fresh; this proves it was never lost — the store
                            # refuses unless the reservation is still the exact one taken here.
                            return missions.adopt(mission_id, key, role=role, expect_token=token)

                    return JSONResponse(await missions.run_admitted(_adopt))
            finally:
                with contextlib.suppress(Exception):
                    missions.release_session(key, token)
        except session_input.AuthorityFenceBusy:
            # RETRYABLE, NOT BROKEN (#900 review 7, finding 5). The shared fence being held is
            # ordinary contention — a question is committing against this very roster — and the
            # adoption did NOT happen. Every sibling mutation answers 503 for it; this route
            # caught only `MissionError` and turned the same condition into a 500, which tells
            # the operator the app failed when the honest answer is "in a moment".
            #
            # Kept here as well as in `mission_fence` because this route takes the roster's
            # pseudo-key together with the adopting session's own physical key, rather than the
            # shared helper's set — the adopting session is by definition not in the roster that
            # helper enumerates.
            return JSONResponse(
                {"detail": "the authorization fence is busy; retry"}, status_code=503
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/detach")
    async def detach_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
            key = _session_key(body.get("session_key"))
            # FENCED. Detaching withdraws this session's authority, and an automatic delivery may
            # be mid-flight: the write fence compares the per-session epoch immediately before
            # byte one, holding the registry lock. Committing the detach inside that same lock is
            # what makes the two orderable — the send either completes first, or waits and then
            # sees the new epoch. Unfenced, the withdrawal could land between the fence's
            # comparison and `os.write()` and the old mission still typed into the session
            # (#888 review, finding 1).
            out = await _fenced_write(mission_id, lambda: missions.detach(mission_id, key))
            return JSONResponse(out)
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/plan")
    async def plan_route(
        mission_id: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Produce a dispatch PROPOSAL. Launches nothing (#893, Phase 4 of #840).

        The separation is the feature: an instruction becomes a project, an agent, a brief and a
        stated reason, on screen, editable, before anything runs unattended. A route
        that planned and dispatched in one call would be a button whose consequences the operator
        first learns about afterwards.

        A re-plan supersedes rather than stacks, and `POST` rather than `GET` because it spends
        the operator's money on a model call and writes a durable row.
        """
        try:
            missions.validate_id(mission_id)
            plan = await mission_plan.propose(mission_id)
        except missions.MissionError as e:
            return _fail(e)
        except mission_plan.PlanError as e:
            return JSONResponse({"detail": str(e)}, status_code=e.status)
        return JSONResponse(plan)

    @app.patch("/api/missions/{mission_id}/plan")
    async def edit_plan_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """The operator's edit of the proposal. **A new `plan_id` every time.**

        Editing is not a lesser act than planning: it produces a different proposal, and DISPATCH
        compares the id it was given against the one stored. If an edit reused the id, a dispatch
        approved against the version on screen a minute ago would run the version typed since —
        which is the whole failure the id exists to prevent, arriving through the other door.

        `project_id` is resolved to a cwd SERVER-SIDE, exactly as `POST /api/missions` does. The
        client sends an id; it never sends a path.

        **The manual FIRST plan** (#967). With no `plan_id`, no stored plan, and planning
        `skipped` or `failed` (no AI endpoint, or the planner could not produce one), a body with
        `project_id`, `engine` and `brief` creates the first plan through the same validation and
        settles `ready`. Every other shape keeps its previous answer.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            if body.get("plan_id") is None:
                first = await _first_plan(mission_id, body)
                if first is not None:
                    return first
            # THE PROPOSAL BEING EDITED, named by the client (#904 review 6). This route reads a
            # plan, changes one field and writes the whole row back — so two tabs editing
            # different fields of the same proposal both succeed, and the later write restores
            # its own stale copy of the field the first one changed. Both operators are told the
            # save worked and one of the edits is gone. The id makes the write a compare-and-set
            # instead, and the comparison happens inside `put_plan`'s transaction, not here.
            expect = body.get("plan_id")
            if not isinstance(expect, str) or not expect.strip():
                return _fail(
                    missions.MissionError(
                        "plan_id is required and names the plan you are editing", status=422
                    )
                )
            current = await missions.run_admitted(lambda: missions.get_plan(mission_id))
            if current is None:
                return _fail(missions.MissionError("this mission has no plan", status=404))
            if current["plan_id"] != expect:
                return _fail(
                    missions.MissionError(
                        "the plan changed while you were editing it; read it again", status=409
                    )
                )

            if "project_id" in body:
                project_id, cwd = _resolve_cwd(body.get("project_id"))
            else:
                project_id, cwd = current["project_id"], current["cwd"]

            engine = current["engine"]
            if "engine" in body:
                raw = body.get("engine")
                if raw is not None and not isinstance(raw, str):
                    return _fail(missions.MissionError("engine must be a string", status=422))
                engine = (raw or "").strip() or None
                if engine is not None:
                    # THE SAME GATE THE LIST WAS BUILT WITH. An operator may choose any agent that
                    # could have been offered — and may not choose one that could not, because
                    # `shell` is a login shell and a brief pasted into it EXECUTES.
                    if engine not in {e["id"] for e in mission_plan.engine_options()}:
                        return _fail(
                            missions.MissionError(
                                f"{engine!r} cannot be dispatched into", status=422
                            )
                        )

            brief = current["brief"]
            if "brief" in body:
                raw_brief = body.get("brief")
                if not isinstance(raw_brief, str):
                    return _fail(missions.MissionError("brief must be a string", status=422))
                brief = raw_brief.strip()
                if not brief:
                    return _fail(missions.MissionError("a plan needs a brief", status=422))

            # The operator's own edit carries no model reason — saying "the model chose this" over
            # a value the operator typed would be a false attribution, and an empty reason reads
            # correctly on the card as "you chose this".
            reason = current["engine_reason"] if engine == current["engine"] else ""
            plan = await missions.run_admitted(
                lambda: missions.put_plan(
                    mission_id,
                    project_id=project_id,
                    cwd=cwd,
                    engine=engine,
                    engine_reason=reason,
                    brief=brief,
                    expect_plan_id=expect,
                )
            )
        except missions.MissionError as e:
            return _fail(e)
        plan["project_options"] = mission_plan.picker_project_options()
        plan["engine_options"] = mission_plan.engine_options()
        return JSONResponse(plan)

    async def _first_plan(mission_id: str, body: dict) -> JSONResponse | None:
        """The manual first plan, or None when this request is not one (#967).

        None hands the request back to the edit path, which answers exactly as it did before:
        a missing `plan_id` is still a 422 for a mission that has a plan or is not `skipped` /
        `failed`. The eligibility is checked again inside `put_plan`'s transaction, so a planner
        result that lands in between cannot be silently overwritten by this save — nor this save
        by it.
        """
        row = await missions.run_admitted(lambda: missions.get_mission(mission_id, events_limit=1))
        if row is None:
            return None
        current = await missions.run_admitted(lambda: missions.get_plan(mission_id))
        if current is not None or row.get("plan_state") not in ("skipped", "failed"):
            return None

        raw_project = body.get("project_id")
        if not isinstance(raw_project, str) or not raw_project.strip():
            return _fail(
                missions.MissionError("project_id is required for the first plan", status=422)
            )

        raw_engine = body.get("engine")
        engine = raw_engine.strip() if isinstance(raw_engine, str) else ""
        if not engine:
            return _fail(missions.MissionError("engine is required for the first plan", status=422))
        # THE SAME GATE THE LIST WAS BUILT WITH — `shell` is a login shell, and a brief pasted
        # into it EXECUTES.
        if engine not in {e["id"] for e in mission_plan.engine_options()}:
            return _fail(missions.MissionError(f"{engine!r} cannot be dispatched into", status=422))

        raw_brief = body.get("brief")
        brief = raw_brief.strip() if isinstance(raw_brief, str) else ""
        if not brief:
            return _fail(missions.MissionError("a plan needs a brief", status=422))

        def _resolve_and_write():
            # ONE FENCE FROM THE PROJECT CHECK TO THE COMMIT (#974 review). Resolved on the loop
            # and written in a later worker call, an archive or delete could land between the two
            # and the first plan settled `ready` against a project that was gone. The project
            # store's own flock — the one `projects.update` / `delete` take — is held across both.
            #
            # LOCK ORDER: projects-store flock (outer) -> missions `_write_lock` -> missions.db
            # write lock (inner), the same order as the planner's write boundary.
            try:
                with projects.locked_index() as index:
                    try:
                        project_id, cwd = _resolve_cwd(raw_project, index=index)
                    except missions.MissionError as e:
                        return None, e
                    stored = missions.put_plan(
                        mission_id,
                        project_id=project_id,
                        cwd=cwd,
                        engine=engine,
                        engine_reason="",
                        brief=brief,
                        first_plan=True,
                    )
                    return stored, None
            except OSError:
                return None, missions.MissionError("could not resolve the project", status=503)

        plan, bad_project = await missions.run_admitted(_resolve_and_write)
        if bad_project is not None:
            # SAME VALIDATION AS AN EDIT, one status: every reason a project cannot be planned
            # into is an invalid field of this request. An unreadable store stays a 503.
            e = bad_project
            return _fail(e if e.status >= 500 else missions.MissionError(str(e), status=422))
        plan["project_options"] = mission_plan.picker_project_options()
        plan["engine_options"] = mission_plan.engine_options()
        return JSONResponse(plan)

    @app.post("/api/missions/{mission_id}/dispatch")
    async def dispatch_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Run the plan the operator SAW. The highest-privilege call in the app (#893).

        What it starts is an UNATTENDED agent — see the module note on why it is not also
        permission-bypassed — so every fence this route has is load-bearing:

        * **The plan id is compare-and-set.** The operator dispatches the proposal on their
          screen, not "whatever the mission's current plan is" — a re-plan or an edit mints a new
          id, and a stale one is a 409 that tells them to read it again.
        * **`claim_plan` is the arbiter**, not a check before this call. It moves
          `planned -> dispatching` under its own state predicate in the same transaction that
          consumes the plan, so two taps cannot become two unattended agents.
        * **The cwd is the one the server resolved** from the project entity at plan time and
          re-stamped on the mission by the claim. No client value reaches the launch.
        * **The tier and the master switch are re-read HERE**, immediately before the spawn — not
          at plan time, which is minutes and a model call earlier (`stale policy across the
          await`, #887's family).

        Every outcome resolves the mission forward: `running` on start evidence, `failed` with a
        reason otherwise. A crash between them is recovered by the startup pass, never left as a
        stuck `dispatching`.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            plan_id = body.get("plan_id")
            if not isinstance(plan_id, str) or not plan_id.strip():
                return _fail(
                    missions.MissionError(
                        "plan_id is required and names the plan you are dispatching", status=422
                    )
                )

            # THE POLICY, READ AT THE WRITE BOUNDARY. A plan can sit on screen for as long as the
            # operator likes, and orchestration can be switched off in that time. Reading it at
            # plan time and trusting it here is exactly the shape #887 was about.
            #
            # …AND THE EPOCH IS CAPTURED FIRST (#904 review 4). A read is not a fence: the switch
            # can be flipped after this line and before the spawn, and the agent would start under
            # authority the operator has withdrawn. The epoch captured BEFORE the read is what the
            # launch fence compares immediately before `create_subprocess_exec` — before, because
            # a change landing between the capture and the read would otherwise carry the new
            # epoch and the old reading.
            policy_epoch = session_input.policy_fingerprint()
            cfg = prefs.get_orchestrator()
            if not cfg.get("enabled"):
                return _fail(
                    missions.MissionError(
                        "orchestration is switched off, so nothing may be launched", status=409
                    )
                )
            # …AND THE TIER, which is a different switch and says a stronger thing (#904 review 2,
            # finding 1). `enabled` is "the orchestrator runs at all"; `autonomy` is what it may
            # DO, and the settings page states the `off` contract in the operator's own words:
            # "watch and propose, never send anything". Starting an unattended, permission-shaped
            # agent is the largest thing this app can send, so a route that read only `enabled`
            # was doing the one thing that tier promises it will not.
            if str(cfg.get("autonomy") or "") == prefs.ORCH_TIERS[0]:
                return _fail(
                    missions.MissionError(
                        "autonomy is off — this instance watches and proposes, and may not "
                        "start an agent",
                        status=409,
                    )
                )

            # …and the containment boundary must EXIST before an unattended agent does (#898
            # review 7, the non-blocking note, made blocking here because this is the caller that
            # makes it matter). Without a transient scope the teardown boundary is a pid snapshot,
            # and a target that forks a survivor during SIGTERM walks out of it and is reported as
            # a clean stop. A launch we could not clean up is not one to start unattended.
            if not (scopedspawn.enabled() and scopedspawn.available()):
                return _fail(
                    missions.MissionError(
                        "this host cannot put a session in its own scope, so an unattended agent "
                        "could not be reliably stopped again; dispatch is refused",
                        status=409,
                    )
                )

            # THE PROJECT PATH, RE-RESOLVED NOW (#904 review 5). The plan carries the cwd the
            # entity meant when it was written, and a project can be archived or repointed in the
            # minutes a proposal sits on screen. Launching an unattended agent into the
            # directory a project USED to mean is the `stale policy across the await` family with
            # a filesystem path on the end of it, so the entity is read again here and the answer
            # is what `claim_plan` stamps on the mission.
            current = await missions.run_admitted(lambda: missions.get_plan(mission_id))
            if current is None:
                return _fail(
                    missions.MissionError("this mission has no plan to dispatch", status=409)
                )
            fresh_project, fresh_cwd = _resolve_cwd(current.get("project_id"))
            if not fresh_cwd:
                return _fail(
                    missions.MissionError(
                        "a plan without a project cannot be dispatched", status=422
                    )
                )
            # THE PATH SHOWN IS THE PATH LAUNCHED (#904 review 2, finding 6).
            #
            # Re-resolving is required — a project can be repointed while a proposal sits on
            # screen, and launching where it USED to point is the stale-policy family with a
            # filesystem path on the end. But re-resolving alone means the operator confirms
            # `/old` and the agent starts in `/new`, which is the same defect wearing the fix's
            # clothes. So the client asserts which resolution it showed, and a mismatch refuses.
            #
            # It is a COMPARAND, never a launch argument: `expect_cwd` is compared and discarded,
            # and the path that reaches the spawn is the one this server resolved.
            expect_cwd = body.get("expect_cwd")
            if not isinstance(expect_cwd, str) or not expect_cwd.strip():
                return _fail(
                    missions.MissionError(
                        "expect_cwd is required and names the directory you approved", status=422
                    )
                )
            # WHAT DONE MEANS MUST BE SETTLED, AND MUST BE WHAT WAS SHOWN (#904 review 3,
            # finding 5). A new mission starts with `objectives_state='pending'` — the producer
            # is a background task — so a fast operator could launch before the mission knew what
            # finishing means, and the supervisor would then follow through against a checklist
            # that arrived afterwards. The card shows the set; `expect_objectives` is the digest
            # of what it showed, so "final" and "the same one you read" are one check.
            row = await missions.run_admitted(lambda: missions.get_mission(mission_id))
            if row is None:
                return _fail(missions.MissionError(f"unknown mission {mission_id}", status=404))
            # FINALIZED **AND NOT EMPTY** (#904 review 4, finding 3). `pending` was the only
            # refusal, so a settled-but-empty checklist dispatched — and #893's acceptance
            # invariant is that the mission knows what finishing means BEFORE it starts. An agent
            # running against nothing is one the supervisor cannot follow through on, which is
            # the entire reason the objectives exist.
            #
            # The gate is deliberately NOT `objectives_state == "done"`. Production settles
            # `skipped` on an install with no AI endpoint configured and `failed` when the model
            # call breaks — and in both cases the operator writing the checklist by hand is the
            # intended flow, not a bypass. Keying on the producer's verdict would make DISPATCH
            # permanently unreachable on those installs while proving nothing: what matters is
            # that nobody is still writing the list (`pending`) and that a list exists.
            obj_state = str(row.get("objectives_state") or "")
            if obj_state == "pending":
                return _fail(
                    missions.MissionError(
                        "what done means is still being worked out; try again in a moment",
                        status=409,
                    )
                )
            if not (row.get("objectives") or []):
                return _fail(
                    missions.MissionError(
                        "this mission has no objectives, so it does not know what finishing "
                        "means; add at least one before dispatching",
                        status=409,
                    )
                )
            expect_obj = body.get("expect_objectives")
            if not isinstance(expect_obj, str):
                return _fail(
                    missions.MissionError(
                        "expect_objectives is required and names the checklist you approved",
                        status=422,
                    )
                )
            if expect_obj != missions.objectives_digest(row.get("objectives") or []):
                return _fail(
                    missions.MissionError(
                        "the objectives changed since you read them; read the plan again",
                        status=409,
                    )
                )
            if expect_cwd.strip() != fresh_cwd:
                return _fail(
                    missions.MissionError(
                        "that project now resolves somewhere else; read the plan again",
                        status=409,
                    )
                )
            plan = await missions.run_admitted(
                lambda: missions.claim_plan(
                    mission_id,
                    plan_id,
                    cwd=fresh_cwd,
                    project_id=fresh_project,
                    # RE-COMPARED INSIDE THE CLAIM (#904 review 4, finding 2). The checks above
                    # are a separate transaction and `patch_objectives` is legal in the gap, so
                    # they answer "was this the checklist a moment ago" — the claim answers "is
                    # it the checklist now", which is the one the launch runs against.
                    expect_objectives=expect_obj,
                    require_objectives=True,
                )
            )
        except missions.MissionError as e:
            return _fail(e)

        out = await mission_dispatch.run(
            mission_id,
            plan,
            registry=registry,
            policy_epoch=policy_epoch,
            # …AND AGAIN, INSIDE THE LAUNCH FENCE. The window between this route's resolution and
            # `create_subprocess_exec` is the second half of the same finding: a project moved in
            # it would still be launched. `run` calls this immediately before the spawn, while
            # holding the fence, and refuses on a mismatch.
            verify_cwd=lambda: _resolve_cwd(plan.get("project_id"))[1],
        )
        return JSONResponse(out)

    @app.post("/api/missions/{mission_id}/spawn")
    async def spawn_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Add a bounded, approval-gated SUB-AGENT to a mission already under way (#894, #840 §9).

        **This route is the approval.** There is no queue of pending spawns for something else to
        drain: an operator pressed a button, authenticated and CSRF-gated, and that is the grant.
        Nothing in `AUTO_VERBS_V1` can reach it, and no model can select it by classifying a
        sentence — the same reason `relay` is a route rather than a chat intent.

        **It starts an unattended agent, so it carries every fence `dispatch` carries** — the
        master switch, the autonomy tier re-read HERE rather than at plan time, the containment
        scope, a cwd the SERVER resolved from the project entity, and an engine checked against
        the capability allowlist. A spawn that were cheaper to authorise than a dispatch would be
        a way around the dispatch gate rather than a feature.

        **`bypass=False`, like every mission launch.** #894's text says a spawn "inherits Phase 4's
        posture — permission bypass"; Phase 4 as SHIPPED passes `bypass=False`, and
        `headless_dispatch` says why: unattended bypass is a separate grant that stays
        approval-required until it has been exercised in anger. Inheriting the posture therefore
        means inheriting `False`. Widening it would be introducing a broad-permission grant and
        needs the operator's own recorded sign-off, which is not something this code may assume.

        **The cap is a RESOURCE GUARD, never a security control.** It bounds fan-out — a mission
        that could spawn without limit could fill the host by being approved repeatedly — and
        decides nothing about what a sub-agent may do. That is the tier, the write fence,
        `bypass=False` and the scope, none of which this number touches.

        **Cap and reservation are ONE transaction** (`claim_spawn`), because "count, then start" is
        check-then-act: two approvals both read a count under the cap, both start, and the mission
        ends with one more agent than was ever allowed while each check was true when made.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)

            parent_key = body.get("parent_key")
            if not isinstance(parent_key, str) or not parent_key.strip():
                return _fail(
                    missions.MissionError(
                        "parent_key is required and names the session this sub-agent works "
                        "alongside",
                        status=422,
                    )
                )
            brief = body.get("brief")
            if not isinstance(brief, str) or not brief.strip():
                return _fail(
                    missions.MissionError(
                        "brief is required and is what the sub-agent is being asked to do",
                        status=422,
                    )
                )

            policy_epoch = session_input.policy_fingerprint()
            cfg = prefs.get_orchestrator()
            if not cfg.get("enabled"):
                return _fail(
                    missions.MissionError(
                        "orchestration is switched off, so nothing may be launched", status=409
                    )
                )
            if str(cfg.get("autonomy") or "") == prefs.ORCH_TIERS[0]:
                return _fail(
                    missions.MissionError(
                        "autonomy is off — this instance watches and proposes, and may not "
                        "start an agent",
                        status=409,
                    )
                )
            if not (scopedspawn.enabled() and scopedspawn.available()):
                return _fail(
                    missions.MissionError(
                        "this host cannot put a session in its own scope, so an unattended agent "
                        "could not be reliably stopped again; spawning is refused",
                        status=409,
                    )
                )

            # WHICH DIRECTORY THE OPERATOR APPROVED (#894 review 1, finding 1). Server
            # resolution and operator approval are COMPLEMENTARY, not alternatives, and this
            # route shipped with only the first half.
            #
            # Resolving server-side stops the model or the client naming a path. It does not stop
            # the path moving underneath a control the operator is already looking at: the spawn
            # panel says the child will work alongside a parent in A, the project mapping is
            # edited to B, and START launches in B while the screen still promises A. Re-resolving
            # under the fence does not help — it only pins the value from the moment of the tap
            # onward, and the operator approved something older than that.
            #
            # So the client asserts which resolution it SHOWED and a mismatch refuses, exactly as
            # `dispatch` does. It is a COMPARAND, never a launch argument: compared and discarded,
            # and the path that reaches the spawn is the one this server resolved.
            expect_cwd = body.get("expect_cwd")
            if not isinstance(expect_cwd, str) or not expect_cwd.strip():
                return _fail(
                    missions.MissionError(
                        "expect_cwd is required and names the directory you approved", status=422
                    )
                )

            row = await missions.run_admitted(lambda: missions.get_mission(mission_id))
            if row is None:
                return _fail(missions.MissionError(f"unknown mission {mission_id}", status=404))
            # THE SERVER'S OWN CWD, from the project entity — never a client value, and re-read
            # here rather than taken from whatever the mission was dispatched with.
            fresh_project, fresh_cwd = _resolve_cwd(row.get("project_id"))
            if not fresh_cwd:
                return _fail(
                    missions.MissionError(
                        "this mission has no resolvable project directory, so a sub-agent has "
                        "nowhere to run",
                        status=409,
                    )
                )
            if expect_cwd.strip() != fresh_cwd:
                return _fail(
                    missions.MissionError(
                        "that project now resolves somewhere else; read the panel again",
                        status=409,
                    )
                )

            # THE ENGINE IS CHECKED AGAINST THE CAPABILITY ALLOWLIST, which is what keeps `shell`
            # and every mint-own-id engine out — the same list the plan picker is built from, so
            # there is no second opinion about what may be launched unattended (#899).
            options = {str(o.get("id")) for o in mission_plan.engine_options()}
            want = body.get("engine")
            engine = str(want).strip() if isinstance(want, str) and want.strip() else ""
            if not engine:
                return _fail(
                    missions.MissionError(
                        "engine is required and names which agent to start", status=422
                    )
                )
            if engine not in options:
                return _fail(
                    missions.MissionError(
                        f"{engine} cannot be started unattended; choose one that can", status=422
                    )
                )

            # RETURN THE SLOTS OF CHILDREN THAT HAVE PROVABLY STOPPED, before counting
            # (#894 review 1, finding 4). Capacity is released on EVIDENCE, and this is where the
            # evidence is gathered: `probe_master` is tri-state and only `DEAD` is decisive.
            # `UNKNOWN` — a starved host too slow to accept within budget — deliberately leaves
            # the row open and the slot held, because the cost of holding one too long is a
            # refused spawn and the cost of freeing one too early is an unbounded fan-out of live
            # agents. It runs OUTSIDE the claim: it only ever frees, so a concurrent claim racing
            # it sees either the old count or the smaller one, and both are safe.
            await asyncio.to_thread(_reap_dead_spawns, mission_id)

            claimed = await missions.run_admitted(
                lambda: missions.claim_spawn(
                    mission_id,
                    parent_key=parent_key.strip(),
                    engine=engine,
                    cwd=fresh_cwd,
                    brief=brief,
                    project_id=fresh_project,
                )
            )
        except missions.MissionError as e:
            return _fail(e)

        out = await mission_dispatch.run(
            mission_id,
            claimed,
            registry=registry,
            policy_epoch=policy_epoch,
            verify_cwd=lambda: _resolve_cwd(row.get("project_id"))[1],
        )
        return JSONResponse(out)

    @app.post("/api/missions/{mission_id}/state")
    async def state_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Compare-and-set: the caller states the state it *believes* the mission is in.

        ``from`` is required rather than inferred. Reading the state here and writing it a
        moment later is two moments, and the whole reason the store CASes is that a state read
        before an await cannot be trusted after it — so the client's belief is the comparand and
        a zero rowcount comes back as a 409, never as a silent retry.
        """
        try:
            body = await _body(request)
            # FENCED like detach, and for the same reason: a transition to a TERMINAL state
            # (`done` / `failed` / `abandoned`) releases every session the mission holds, so it
            # withdraws the authority behind any in-flight nudge. Committing it outside the fence
            # let a closed mission's nudge still land (#888 review, finding 3). Non-terminal
            # transitions pay only an epoch bump, which costs a re-proposal at worst.
            return JSONResponse(
                await _fenced_write(
                    mission_id,
                    lambda: missions.set_state(
                        mission_id,
                        str(body.get("from") or ""),
                        str(body.get("to") or ""),
                        outcome=body.get("outcome"),
                        detail=str(body.get("detail") or ""),
                    ),
                )
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/archive")
    async def archive_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Archive the mission **and its sessions** — terminal-state-only.

        A live mission is a **409**, not a prompt: there is no path that leaves a mission
        simultaneously running and archived. ``{"abandon": true}`` is the explicit, confirmed
        two-transition path, and the client only sends it after saying plainly that it will stop
        the agents.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            return JSONResponse(
                await mission_archive.archive_mission(
                    mission_id,
                    # A REAL boolean. `bool("false")` is True, and on the flag that authorises
                    # abandoning a live mission and terminating its agents that is not a type
                    # nit — it is an authorisation bug reachable from any client that
                    # stringifies its JSON.
                    abandon=missions.strict_bool(body.get("abandon"), "abandon", default=False),
                )
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/unarchive")
    async def unarchive_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            restore = missions.strict_bool(body.get("sessions"), "sessions", default=True)
            return JSONResponse(
                await mission_archive.unarchive_mission(mission_id, sessions=restore)
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/turns/{turn_id}/ack")
    async def ack_turn_route(
        mission_id: str,
        turn_id: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Dismiss an AMBIGUOUS turn. Nothing else is dismissible (#902 review, finding 1).

        `indeterminate` is terminal and the server cannot resolve it — that is what the state
        means. Once the console reads its open turn from the store, "I have seen this" has to be
        durable too, or the banner comes back on every reload and the operator can never clear a
        turn nobody can settle for them.

        A 404 rather than a 409 for the not-applicable cases: from the operator's side there is
        no ambiguous turn by that id to dismiss, whether it settled, was already dismissed, or
        never existed.
        """
        try:
            missions.validate_id(mission_id)
            ok = await missions.run_admitted(lambda: missions.ack_turn(mission_id, turn_id))
        except missions.MissionError as e:
            return _fail(e)
        if not ok:
            return JSONResponse(
                {"detail": "there is no unresolved ambiguous turn by that id"}, status_code=404
            )
        return JSONResponse({"turn_id": turn_id, "acked": True})

    @app.post("/api/missions/{mission_id}/message")
    async def message_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """One operator turn: exactly one MODEL EXECUTION per `turn_id`, across crashes and
        retries — and each action it produces is delivered at most once.

        Not "exactly one instruction": a turn may legitimately produce several actions, and the
        at-most-once guarantee is per ACTION. The looser phrasing predates #871's lifecycle pass
        and described a shape the code never had.

        The whole shape exists because this crosses two durable stores — the missions store and
        the orchestrator ledger — and ordering alone does not make that idempotent. See #852.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            raw_msg = body.get("message")
            # The SAME contract the sibling orchestrator route enforces, refused before anything
            # is claimed or persisted. `str(...)` on arbitrary JSON quietly turned a dict into
            # its repr and sent it to the model, and no length bound meant unbounded operator
            # text became an unbounded durable mission event.
            if not isinstance(raw_msg, str):
                return _fail(missions.MissionError("message must be a string", status=422))
            text = raw_msg.strip()
            if not text:
                return _fail(missions.MissionError("message is required", status=422))
            if len(text) > orchestrator_chat.QUERY_MAX:
                return _fail(
                    missions.MissionError(
                        f"message too long (max {orchestrator_chat.QUERY_MAX} chars)", status=422
                    )
                )
            turn_id = str(body.get("turn_id") or "").strip()
            if not turn_id or len(turn_id) > 64:
                return _fail(
                    missions.MissionError(
                        "turn_id is required and must be at most 64 characters", status=422
                    )
                )
            msg_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            # An unknown mission is a 404 BEFORE the model or the store is touched. Without this
            # the `mission_turns` foreign key failed deep inside the claim and surfaced as a 500,
            # which tells the caller "we broke" about a request that was simply wrong.
            if await missions.run_admitted(lambda: missions.get_mission(mission_id)) is None:
                return _fail(missions.MissionError("unknown mission", status=404))

            # The reconciler's SECOND call site (#871). A turn whose actions the TTL already
            # settled stays `in_progress` until somebody writes that conclusion onto it, and an
            # operator sending the next message is exactly when a stale one costs something:
            # without this, `claim_turn` reclaims a turn that was never going to produce
            # anything, and the archive fence stays shut behind it. Opportunistic — it never
            # raises, and it never re-drives an action.
            await asyncio.to_thread(lambda: mission_turn_reconcile.reconcile(mission_id=mission_id))

            # THE TRANSIENT CONDITIONS ARE SETTLED BEFORE ANYTHING DURABLE EXISTS (#871
            # decision 3, corrected). A busy flight and an unconfigured endpoint are conditions
            # of the SYSTEM, not outcomes of the turn — so they must not consume the operator's
            # `turn_id`, and they must not leave a message in a timeline for a request that never
            # reached a model (the recap cap evicts, and no delete restores what eviction drops).
            #
            # Checking them HERE, before the claim, is what makes that possible now that the
            # claim writes the operator event: there is nothing yet to be inconsistent with.
            #
            # The decision as first published said to DELETE the preflight, on the grounds that a
            # check before the call and the check inside it are two moments. The reasoning is
            # right and the conclusion was wrong: a gap between two checks only matters if the
            # two sides of it can disagree, and they no longer can — a failure arriving AFTER the
            # claim settles the turn forward rather than releasing it, so the "released the claim
            # but not the event" state is gone either way. What the early-out buys back is that
            # an ordinary misconfigured install neither burns a turn id nor leaves a message
            # behind for a request that never reached a model.
            # …and they gate ONLY the paths that can reach the model.
            #
            # Putting them ahead of `claim_turn` unconditionally was wrong, and it is the kind of
            # wrong that looks fine until someone's endpoint breaks: `claim_turn` is what detects
            # a REPLAY, so a settled turn could no longer be read back while any flight was
            # running or while the endpoint was unconfigured. Reading a stored answer needs
            # neither — it is a database read — and a `turn_id` reused for different text would
            # have come back 409 instead of the 422 that says what is actually wrong.
            #
            # So peek first. The peek is an optimisation, never the authority: `claim_turn`
            # still decides the verdict, and if the turn is claimed by someone else between the
            # two, it returns TURN_LIVE and this request answers 202. What the peek settles is
            # only whether this request COULD call the model, which is the exact question the
            # transient conditions are about.
            prior = await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id))
            may_call_model = prior is None or (
                prior.get("msg_sha") == msg_sha
                and prior.get("state") == "in_progress"
                and not prior.get("write_reserved_at")
                and float(prior.get("owner_at") or 0) < time.time() - missions.TURN_OWNER_MAX_AGE_S
            )
            if may_call_model:
                if aitasks.is_running("pulse-chat"):
                    return _fail(missions.MissionError("a question is already running", status=409))
                try:
                    await asyncio.to_thread(review._require_config)
                except review.NotConfiguredError:
                    return _fail(missions.MissionError("AI endpoint is not configured", status=409))

            # `text=` writes the operator's message IN the claim transaction (#871 decision 3).
            verdict, row = await missions.run_admitted(
                lambda: missions.claim_turn(mission_id, turn_id, msg_sha, text=text)
            )
            if verdict == missions.TURN_CONFLICT:
                # The same key with different text is a DIFFERENT turn, not a replay. Returning
                # the stored answer would answer a question nobody asked.
                return _fail(
                    missions.MissionError(
                        "turn_id was already used for a different message", status=422
                    )
                )
            if verdict == missions.TURN_LIVE:
                # Someone is running this right now. Say so; do not call the model again.
                # From the TURN'S OWN receipt, not a ledger provenance scan. `reserve_turn_write`
                # records exactly what the turn was about to append, so this is the same list the
                # running frame reports — two paths deriving it differently is how one turn
                # answered with actions once and without them the next time.
                return JSONResponse(
                    _in_progress(
                        turn_id,
                        _stored_action_ids(row),
                        delivery_error=_stored_delivery_error(row),
                    ),
                    status_code=202,
                )
            if verdict == missions.TURN_DONE:
                return JSONResponse(_replay(row))
            if verdict == missions.TURN_RECONCILE:
                # A write MAY have landed. Never re-ask — settle from provenance if the action is
                # there, and `indeterminate` if it is not, so the turn always reaches a terminal
                # state instead of sitting `in_progress` for ever.
                return JSONResponse(await _reconcile(mission_id, turn_id, row))
            # TURN_CLAIMED or TURN_RECOVER — the only paths that call the model.
            return JSONResponse(await _run_turn(mission_id, turn_id, text, row, registry=registry))
        except missions.MissionError as e:
            return _fail(e)

    @app.get("/api/missions/{mission_id}/context")
    async def context_route(mission_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        """Project, cwd, branch, git summary and session roster for one mission.

        **Takes no path.** The cwd is read from the mission row and nowhere else, so there is no
        client-supplied path to traverse with. The file/git work is composed from the existing
        helpers rather than by calling the route handlers, and it runs through the same bounded
        executor those routes use — that bound is part of the safety contract, not a speed knob.

        The no-store envelope is applied by the middleware in `routes/files.py`, whose prefix test
        was widened to cover this path: git status carries absolute paths, and it must not be
        cacheable on success, on 401, or on 500.
        """
        try:
            missions.validate_id(mission_id)
            m = await missions.run_admitted(lambda: missions.get_mission(mission_id))
        except missions.MissionError as e:
            return _fail(e)
        if m is None:
            # A fabricated 200 for a mission that does not exist is worse than an error: the
            # caller renders an empty console for a thing that was never there.
            return _fail(missions.MissionError("unknown mission", status=404))
        cwd = m.get("cwd") or ""
        out: dict = {
            "id": mission_id,
            "project_id": m.get("project_id") or "",
            "cwd": cwd,
            # ACTIVE MEMBERSHIP ONLY (#903 review 3, finding 2).
            #
            # `get_mission` returns the whole roster HISTORY, `removed_at` rows included — which
            # is right for a record and wrong for a control surface. The console renders one live
            # screen-and-relay block per row, so a mission that had detached a session went on
            # offering it: VIEW SCREEN showed the CURRENT output of a session another mission had
            # since adopted, in the old mission's context, and SEND only failed later at the write
            # fence. Filtered at the API boundary rather than in the component, because every
            # consumer of this field wants the same thing and a second one would have to remember.
            "sessions": [s for s in (m.get("sessions") or []) if s.get("removed_at") is None],
            "git": None,
            "git_error": None,
        }
        if cwd:
            try:
                out["git"] = await files_routes.run_bounded(cwd, gitpanel.git_status, cwd)
            except Exception as e:  # noqa: BLE001 — fail CLOSED and name the kind, never the path
                out["git_error"] = type(e).__name__
        return JSONResponse(out)

    @app.post("/api/missions/{mission_id}/relay")
    async def relay_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Type the operator's own words into one of this mission's sessions (#894, #840 §9).

        **This is not a second path to a PTY, and everything about its shape is that sentence.**
        It builds a ledger action and hands it to `actuator.deliver`, which is the one door:
        `is_live` at the write boundary, an atomic claim before any byte, the final guard under
        the write lock, the mission fence, the viewer-busy precondition, and
        `handoff.sanitize_seed` on the payload. Nothing here re-implements any of that, and a
        future change to the fence reaches this automatically because there is no copy of it.

        **The bytes are OPERATOR-AUTHORED, which is a NARROWER authority than the model's
        `answer`, not a wider one.** `relay` is not in `AUTO_VERBS_V1`, so nothing can ever
        deliver one automatically; it exists only because an operator pressed send.

        **It respects the orchestrator master switch and the OFF tier.** That is a deliberate
        choice and worth stating, because the opposite is arguable: an operator typing their own
        words is not the orchestrator acting, and one could reason that the switch should not
        reach it. It does anyway, because the OFF tier's own copy promises the operator that
        *nothing is ever sent* — and a promise with an exception the operator has to know about
        is not the promise they read. Opening the terminal remains available, which is what #840
        keeps deliberately.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            key = _session_key(body.get("session_key"))
            raw = body.get("text")
            if not isinstance(raw, str):
                return _fail(missions.MissionError("text must be a string", status=422))
            text = raw.strip()
            if not text:
                return _fail(missions.MissionError("text is required", status=422))
            if len(text) > actuator.NUDGE_MAX:
                return _fail(
                    missions.MissionError(
                        f"text is longer than {actuator.NUDGE_MAX} characters", status=422
                    )
                )
            # THE MISSION MUST STILL HOLD THIS SESSION. Checked here so a relay aimed at a
            # session the mission released is refused with a reason rather than being delivered
            # to somebody else's work — and checked AGAIN inside the fence by `deliver`'s own
            # mission guard, which is the one that counts. This one exists to give the operator
            # an answer; that one exists to be correct.
            held = await missions.run_admitted(lambda: missions.active_session_keys(mission_id))
            if key not in held:
                return _fail(
                    missions.MissionError("this mission does not hold that session", status=409)
                )
        except missions.MissionError as e:
            return _fail(e)

        action_id = f"relay_{uuid.uuid4().hex}"

        # THE OPERATOR'S RECORD IS WRITTEN BEFORE THE BYTES, and a failure to write it refuses
        # the relay (#903 review, finding 2).
        #
        # The other order suppressed the store failure and still answered `delivered`: the words
        # reached the agent and the transcript never said who sent them, which is precisely the
        # authorship contract this feature exists to keep. Refusing here is safe *because* it is
        # first — nothing has been written to the pty, so the operator can simply send again.
        try:
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "operator_msg",
                    text=text,
                    session_key=key,
                    action_id=action_id,
                    meta={"relay": True, "state": "sending"},
                )
            )
        except Exception:  # noqa: BLE001
            return _fail(
                missions.MissionError(
                    "nothing was sent: the mission's own record of it could not be written",
                    status=502,
                )
            )

        # THE APPEND IS ITS OWN STEP, and its failure is a DIFFERENT fact from a delivery
        # failure (#903 review 3, finding 3). Nothing has been claimed and no byte can have been
        # written, because the thing that writes bytes reads this row to find out what to write.
        # So this failure is definite, and saying so is the whole point of splitting it out.
        try:
            await asyncio.to_thread(
                orchestrator_ledger.append,
                {
                    "id": action_id,
                    "verb": "relay",
                    "session_id": key,
                    # Rendered by `actuator.render`, which sanitises it. Stored under `answer`
                    # because that is the field `render` reads for a paste payload — one field,
                    # one sanitiser, rather than a second spelling to keep in step.
                    "answer": text,
                    # APPROVED on arrival: the operator pressing send IS the approval, exactly as
                    # their tap is for a proposal. There is no model opinion here to weigh.
                    "state": "approved",
                    "confidence": 1.0,
                    "origin": "operator",
                    "mission_id": mission_id,
                },
            )
        except Exception as e:  # noqa: BLE001
            await _settle_relay(
                mission_id,
                action_id,
                "failed",
                f"nothing was sent: the action could not be recorded ({type(e).__name__})",
            )
            return _fail(
                missions.MissionError(f"the relay failed ({type(e).__name__})", status=502)
            )

        try:
            rec = await actuator.deliver(action_id, registry=registry)
        except actuator.NotDeliverable as e:
            # A DEFINITE ZERO-BYTE REFUSAL, so the record is settled here rather than left for
            # the reconciler (#903 review 2, finding 3). `NotDeliverable` is raised before the
            # claim or on a lost claim race; either way nothing of ours reached the pty, and a
            # record still reading `sending` would be a claim about a delivery that is over.
            await _settle_relay(mission_id, action_id, "failed", str(e))
            return _fail(missions.MissionError(str(e), status=409))
        except Exception as e:  # noqa: BLE001
            # AMBIGUOUS, AND SAID SO (#903 review 3, finding 3). Once `deliver` has claimed the
            # action the bytes may already be on the pty — the post-write ledger CAS is inside
            # this call, and it can raise after a successful write. "Zero bytes reached the
            # terminal" is simply not knowable from here, and `failed` asserts it.
            #
            # `indeterminate` is the app's existing word for exactly this, and it is the state
            # startup recovery moves an orphaned claim to. It is also REVISITABLE: if the ledger
            # later carries a terminal row for this action, the read-time reconcile upgrades the
            # record to whatever actually happened. A `failed` would have been final and wrong.
            # THE LEDGER TOO, not just the mission's own record (#903 review 4, finding 2).
            #
            # The action stays `claimed`, and startup recovery deliberately refuses to touch a
            # claim whose owner is still running — which is right, and here it means this process
            # has left a live claim nothing will ever settle: the session reads busy and later
            # actions are refused until a restart. The one process that knows the delivery is
            # over is this one, so it says so.
            #
            # A CAS from `claimed`, never a write: `deliver` may have settled it in the window
            # between the exception and here, and overwriting a real outcome with "we cannot
            # tell" would replace a fact with a shrug.
            note = f"the delivering process could not settle it ({type(e).__name__})"
            try:
                await asyncio.to_thread(
                    orchestrator_ledger.compare_and_set,
                    action_id,
                    frozenset({"claimed"}),
                    "indeterminate",
                    None,
                    detail=note,
                )
            except Exception:  # noqa: BLE001
                # THE COMPENSATING WRITE CAN FAIL FOR THE SAME REASON THE DELIVERY DID (#903
                # review 5, finding 1) — it is the same store. Suppressed, that left the ledger
                # `claimed` under an owner that is still running, which recovery correctly
                # refuses to touch: the session reads busy and later actions are refused until
                # this process restarts, while the timeline says the delivery is terminal.
                #
                # So the obligation is remembered and retried on the read-time reconcile. If the
                # process dies with it outstanding, its owner token dies too and startup recovery
                # takes the row — the two paths cover each other exactly.
                orchestrator_ledger.owe_terminalize(action_id, note)
            await _settle_relay(
                mission_id,
                action_id,
                "indeterminate",
                f"the relay may or may not have landed ({type(e).__name__})",
            )
            # A MACHINE-READABLE OUTCOME, not only a sentence (#903 review 4, finding 3). The
            # client prefixes every rejected mutation with "Not sent —", which is exactly the
            # wrong thing to say about a delivery that may already be in the pty: it invites a
            # retry of bytes the agent might have. `state` is how it tells this apart from the
            # definite zero-byte refusal beside it.
            return JSONResponse(
                {
                    "detail": (
                        f"the relay may or may not have landed ({type(e).__name__}); "
                        "check the session before sending it again"
                    ),
                    "state": "indeterminate",
                    "action_id": action_id,
                },
                status_code=502,
            )

        # …and SETTLED with what actually happened. Best-effort on purpose: the record already
        # exists, so the worst case here is one that still reads `sending` beside a ledger row
        # under the same `action_id` that says how it ended — and `mission_relay_reconcile`
        # resolves exactly that on the next read, which is what makes this recoverable rather
        # than merely reconcilable in principle.
        await _settle_relay(
            mission_id, action_id, str(rec.get("state") or ""), str(rec.get("detail") or "")
        )
        return JSONResponse(
            {
                "action_id": action_id,
                "state": rec.get("state"),
                "detail": rec.get("detail"),
                "session_key": key,
            }
        )

    @app.get("/api/missions/{mission_id}/screen/{session_key:path}")
    async def mission_screen_route(
        mission_id: str, session_key: str, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        """The live screen of a session THIS MISSION HOLDS, checked at request time (#903 review
        3, finding 1).

        `/api/pulse/evidence/{id}` is mission-agnostic and correct for what it is — the console
        asks about a session it named. What it cannot answer is the question a mission's screen
        block is actually asking: *show me what MY agent is doing.* Membership is not a property
        of the page, it is a row that another tab can change: detach the session from mission A,
        adopt it into B, and A's already-open block goes on polling the same key and rendering
        B's live output under A's heading. Nothing on that page is wrong except the thing that
        matters — whose work the operator is reading.

        So the mission is part of the request, the roster is read NOW, and a session the mission
        no longer holds is a 409 rather than a screenful of somebody else's terminal. The client
        removes the block on that answer; the answer is what makes it able to.
        """
        try:
            missions.validate_id(mission_id)
            key = _session_key(session_key)
            held = await missions.run_admitted(lambda: missions.active_session_keys(mission_id))
        except missions.MissionError as e:
            return _fail(e)
        if key not in held:
            return _fail(
                missions.MissionError("this mission no longer holds that session", status=409)
            )
        # Blocking: the ring replay + FS reads must never run on the event loop (#678).
        result = await asyncio.to_thread(orchestrator.evidence_for, key, "screen")
        # …AND CHECKED AGAIN AFTER IT (#903 review 4, finding 1). The first check is about the
        # REQUEST; this one is about the BYTES, and they are not the same moment: reading a live
        # screen takes a ring replay and filesystem work, and a detach-and-re-adopt inside that
        # window means the bytes in hand belong to whoever owns the session now.
        #
        # A re-read rather than a lock: locking a session for the duration of a screen read would
        # order it against deliveries, which is a far heavier promise than this route needs. What
        # it needs is not to HAND OVER content it has no claim to, and a check after the read is
        # exactly that — the content is discarded rather than returned.
        still = await missions.run_admitted(lambda: missions.active_session_keys(mission_id))
        if key not in still:
            return _fail(
                missions.MissionError("this mission no longer holds that session", status=409)
            )
        # Live terminal content, whose whole contract is "what the session shows RIGHT NOW". A
        # cached copy is both a stale-evidence hazard and a data-exposure one — the same headers
        # `/api/pulse/evidence` sets, for the same reason.
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

    @app.post("/api/missions/{mission_id}/answer")
    async def answer_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Answer the mission's open question — an option index, or free text (#892).

        **The action is looked up server-side from the STORED option, by index.** Nothing the
        model wrote is executed: the `label` is display text, and the action name it maps to must
        be in `mission_questions.ACTIONS` or the request is refused. Rejected rather than clamped
        — an action outside the closed set is a question the operator answered and nothing
        happened to, which is worse than an error.

        **Free text is an ANSWER, not an instruction.** It is recorded on the timeline for the
        next supervisor pass to read; it never becomes agent input. That is what keeps this route
        from being a second, unfenced path to a PTY.
        """
        try:
            body = await _body(request)
            missions.validate_id(mission_id)
            seq = body.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                return _fail(
                    missions.MissionError(
                        "seq is required and names the question being answered", status=422
                    )
                )
            idx = body.get("option_index")
            if idx is not None and (isinstance(idx, bool) or not isinstance(idx, int)):
                return _fail(missions.MissionError("option_index must be an integer", status=422))
            raw_text = body.get("text")
            if raw_text is not None and not isinstance(raw_text, str):
                return _fail(missions.MissionError("text must be a string", status=422))
            text = (raw_text or "").strip()
            if len(text) > missions.QUESTION_TEXT_MAX:
                return _fail(
                    missions.MissionError(
                        f"text is longer than {missions.QUESTION_TEXT_MAX} characters", status=422
                    )
                )

            # FENCED, like every other authority change on this route (#900 review 7, finding
            # 13, correcting the note that used to sit here). Answering releases the objective
            # back into the follow-through by advancing its episode, which invalidates an
            # in-flight nudge aimed at the old one — it does NOT withdraw a stand-down the
            # operator set separately, and the store carries that across deliberately.
            #
            # It takes the same protocol the question OPENING takes: fail-closed enumeration,
            # the roster pseudo-key, and a re-read inside the lock.
            out = await _fenced_write(
                mission_id,
                lambda: missions.answer_question(mission_id, int(seq), option_index=idx, text=text),
            )
        except missions.MissionError as e:
            return _fail(e)

        # THE CLOSED SET, enforced after the store has told us which action the chosen option
        # names. A stored option can only have come from `mission_questions`, but this is the
        # boundary that makes that a property rather than a chain of assumptions.
        #
        # The effect itself ran inside the settlement's transaction (#900 review, finding 2), so
        # `applied` is what the store DID, not what this route intended: settling here and acting
        # afterwards left a window where a crash lost the operator's choice for good, because the
        # retry is a 409 once the hold is released.
        action = str(out.get("action") or "")
        if action not in mission_questions.ACTION_NAMES:
            return _fail(missions.MissionError(f"unknown answer action {action!r}", status=422))
        return JSONResponse(out)

    @app.get("/api/missions/{mission_id}/objectives")
    async def objectives_route(mission_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            rows = await missions.run_admitted(lambda: missions.objectives(mission_id))
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse({"objectives": rows})

    @app.post("/api/missions/{mission_id}/objectives/{objective_key}/stand-down")
    async def stand_down_route(
        mission_id: str,
        objective_key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """ "Stop telling me about this one" — for the EPISODE the operator was looking at (#885).

        It silences; it does not settle. The objective stays `pending` and visibly unmet, because
        an operator's annoyance is not evidence about the work — a stand-down that marked
        something met would turn "leave me alone" into a false claim.

        The episode is REQUIRED in the body and is not defaulted to "whatever is current". The
        board the operator tapped was rendered against a particular episode, and if the objective
        has moved since, their tap is about a situation that no longer exists — silencing the new
        episode would suppress a report nobody has seen. A stale tap is a 409, not a no-op, so the
        console can re-render rather than quietly doing nothing.
        """
        try:
            body = await _body(request)
            episode = body.get("episode")
            if not isinstance(episode, int) or isinstance(episode, bool) or episode < 1:
                raise missions.MissionError(
                    "episode is required and must be the one the objective was rendered at",
                    status=422,
                )
            # Fenced for the same reason as detach: a stand-down withdraws the authority behind
            # any in-flight nudge for this objective, on every session the mission holds.
            ok = await _fenced_write(
                mission_id, lambda: missions.stand_down(mission_id, objective_key, episode=episode)
            )
        except missions.MissionError as e:
            return _fail(e)
        if not ok:
            current, _ = await missions.run_admitted(
                lambda: missions.objective_episode(mission_id, objective_key)
            )
            return JSONResponse(
                {
                    "detail": "this objective has moved on since you saw it",
                    "episode": current,
                },
                status_code=409,
            )
        episode_now, stood_down = await missions.run_admitted(
            lambda: missions.objective_episode(mission_id, objective_key)
        )
        return JSONResponse({"episode": episode_now, "stood_down": stood_down})

    @app.patch("/api/missions/{mission_id}/objectives")
    async def patch_objectives_route(
        mission_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Operator edits only — ``add`` / ``drop`` / ``retitle`` / ``waive`` / ``reorder``, and
        the direction ops ``set_direction`` / ``reset_direction`` / ``clear_direction`` (#983).

        ``state`` / ``met_at`` / ``observed`` are not writable here at all, so an edit can never
        retroactively mark an objective met. ``probe`` and ``probe_args`` ARE accepted on this
        path because it is the operator's — the model path (Phase 3) selects a playbook template
        index and never authors a probe target, which is what keeps an SSRF-with-a-cadence out of
        the design. The same holds for a DIRECTION: it is typed into a session, so only this
        authenticated, CSRF-guarded route writes one, and an unknown placeholder is a 422 here.
        Validation lives in the store so both paths inherit the same gate.
        """
        try:
            body = await _body(request)
            # Dropping or waiving an objective withdraws the authority behind any in-flight nudge
            # aimed at it, so the edit commits inside the write fence — see `detach_route`.
            rows = await _fenced_write(
                mission_id,
                lambda: missions.patch_objectives(
                    mission_id, body.get("ops") or [], source="operator"
                ),
            )
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse({"objectives": rows})

    @app.post(DIRECTION_PREVIEW_PATH)
    async def direction_preview_route(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """What a direction would type, filled with EXAMPLE facts (#983 P2). Read-only.

        The editor's preview, from the one renderer rather than a copy of it in the client:
        ``mission_directions.preview`` validates exactly as a save does (so an unknown placeholder
        is the same 422, in the same words, before the operator saves) and renders over a
        synthesized objective carrying example values. It reads no mission, no prefs and no ledger
        and writes nothing, so it can neither disclose a real fact nor change any state. POST only
        because the direction is operator text that does not belong in a URL or an access log;
        it takes ``csrf_guard`` like every other POST here.
        """
        try:
            body = await _body(request)
        except missions.MissionError as e:
            return _fail(e)
        probe = body.get("probe")
        # Only a probe kind the store knows; anything else is "no probe", never echoed back.
        kind = probe if isinstance(probe, str) and probe in missions.PROBE_KINDS else ""
        try:
            out = mission_directions.preview(body.get("direction"), kind)
        except (mission_directions.DirectionError, mission_directions.NotRenderable) as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        return JSONResponse(out)


#: The preview route (#983 P2). A module constant so the route and its tests name one string.
DIRECTION_PREVIEW_PATH = "/api/mission-directions/preview"


async def _fenced_write(mission_id: str, fn):
    """Run a mission mutation INSIDE the write fence. One protocol, shared with the producer.

    THE PROTOCOL IS `mission_fence.fenced_write`, and this is the reason it moved there (#900
    review 7, finding 1). Every mutation on this route file withdraws authority — a terminal
    transition and a detach release sessions, an objective edit or a stand-down retires the thing
    a nudge was aimed at, an answer releases the objective back into the follow-through — and
    each has to be ordered against an in-flight PTY write and against a concurrent ADOPTION.

    The version that lived here got two of the three parts wrong, and the answer path is where
    that showed: `_held_physical_keys` mapped a store read failure to `[]`, so a fence that could
    not see the sessions locked nothing and said it had; and nothing re-read the roster inside the
    lock, so a session adopted between the enumeration and the transaction was never held at all.
    The question producer had both fixes. Sharing one function is what stops them drifting again.
    """
    return await mission_fence.fenced_write(mission_id, fn)


def _cursor(raw: object):
    """A timeline page cursor, or a 422 — **"no cursor" and "bad cursor" are different answers**.

    Falling back to ``None`` collapsed them: a client paging with a corrupt cursor silently got
    page one back instead of being told, which reads as the timeline having reset.
    """
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        raise missions.MissionError("events_before_seq must be an integer", status=422) from None


def _int(raw: object, default):
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def _replay(row: dict | None) -> dict:
    """The stored answer for a settled turn, in the SAME shape a fresh completion returns.

    "Byte-identical replay" is only true if the replay carries the same fields — a caller that
    branches on `intent` or reads `answer` must not have to know whether it was the first to ask.
    """
    row = row or {}
    actions: list = []
    with contextlib.suppress(Exception):
        parsed = json.loads(row.get("action_ids") or "[]")
        if isinstance(parsed, list):
            actions = parsed
    meta: dict = {}
    with contextlib.suppress(Exception):
        meta = json.loads(row.get("result_meta") or "{}") or {}
    # The actions are the SNAPSHOT taken at settlement, not a live re-read. Hydrating from the
    # current ledger made the "identical replay" contract false the moment an action moved on:
    # the same stored turn answered `in_flight_revocable` and later `settled`. This is the same
    # shape Phase 1 uses for decision events — an immutable projection frozen at the moment the
    # record was settled, because a stored answer that changes is not a stored answer.
    snapshot = meta.get("actions")
    return {
        "turn_id": row.get("turn_id"),
        "state": row.get("state") or "done",
        "intent": meta.get("intent"),
        "answer": row.get("result"),
        # Surfaced on EVERY path, first answer and replay alike. Storing it only in `result_meta`
        # meant a failed delivery was recorded and then never shown: the turn read `done`, the
        # action stayed `approved`, and nothing in the response said why. An outcome the operator
        # cannot see is not an outcome that was reported.
        "delivery_error": meta.get("delivery_error"),
        # THE SESSIONS THE ANSWER IS ABOUT (#890). `find` and `history` answer by naming sessions,
        # and an answer naming a session the operator cannot reach is half an answer — the Ask box
        # rendered them and the durable turn has to carry them too, or moving the composer onto
        # this route would quietly lose half of every answer it gives. Frozen at settlement like
        # the actions beside them, for the same reason: a stored answer that changes is not a
        # stored answer.
        "matches": meta.get("matches") if isinstance(meta.get("matches"), list) else [],
        "actions": snapshot if isinstance(snapshot, list) else _hydrate_actions(actions),
    }


def _hydrate_actions(action_ids: list) -> list[dict]:
    """Stored ids → the SAME projected shape every other decision surface returns (#852).

    The mission response is one of the three producers the contract names, and returning bare ids
    left the console with nothing to render controls from — it would have had to re-derive them
    from a state field, which is the drift `project_for_operator` exists to end.

    An id the ledger no longer holds projects as `historical`: no controls, and **no outcome
    asserted**. Storing the id and inventing a state for it would be worse than saying nothing.
    """
    out: list[dict] = []
    latest: dict = {}
    with contextlib.suppress(Exception):
        latest = orchestrator_ledger.latest_by_id()
    for aid in action_ids:
        key = str(aid)
        rec = latest.get(key) or {}
        out.append(
            {
                "id": key,
                **{k: v for k, v in rec.items() if k in ("verb", "session_id", "title")},
                **orchestrator_ledger.project_for_operator(rec.get("state")),
            }
        )
    return out


async def _settle_indeterminate(
    mission_id: str, turn_id: str, fence: str, action_ids: list[str], *, why: str
) -> dict:
    """Terminalize a turn we cannot account for, with both timeline events.

    `indeterminate` is a real terminal outcome, not an error path: it says the instruction may
    have gone out and we will not send a second one to find out. It therefore carries the same
    assistant event every other terminal turn does — a turn settled without one leaves
    `assistant_seq` NULL for ever, and nothing repairs it.
    """
    # The CAS result is deliberately not branched on. Won or lost, the answer the caller gets is
    # what the STORE says: a frame that reports its own intention after losing the race is the
    # lie the fence exists to prevent.
    await missions.run_admitted(
        lambda: missions.settle_turn(
            mission_id,
            turn_id,
            fence,
            state="indeterminate",
            result=why,
            result_meta={"reconciled": True},
            action_ids=action_ids,
            assistant_text="",
            assistant_meta={"turn_id": turn_id, "actions": action_ids, "indeterminate": why},
        )
    )
    return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))


async def _reconcile(mission_id: str, turn_id: str, row: dict | None) -> dict:
    """Settle an orphaned turn whose receipt says a write may already have happened.

    Provenance first, because it is the only evidence the irreversible half occurred. Absent it,
    `indeterminate` — the honest terminal answer: we cannot say whether the instruction went out,
    and we will not send a second one to find out.
    """
    fence = (row or {}).get("fence") or ""
    status, found = await asyncio.to_thread(_actions_for_turn, mission_id, turn_id)
    if status != "ok":
        # The ledger would not read, so there is NO evidence either way. Leave the turn in
        # progress: it stays pinned against compaction, keeps the archive fence shut, and the
        # next attempt reconciles it once the store answers. Settling here would terminalize a
        # turn on a transient I/O error and open that fence — the exact failure the tri-state
        # read above exists to prevent.
        return _in_progress(turn_id, [])
    if found:
        # THE RECEIPT IS THE AUTHORITATIVE EXPECTED SET, not the rows that happen to be present.
        #
        # `append_batch_for_free_sessions` writes and fsyncs one record at a time, so a crash can
        # leave reserved `[a1, a2]` with only `a1` in the ledger. Judging `found` alone then finds
        # one terminal action, concludes the whole turn is done, and silently discards `a2` — a
        # turn reported complete over an action nobody can account for (review on #881).
        #
        # `unresolved_turn_keys` pins exactly these ids against compaction, so a missing one is
        # never "compacted away": it is an append that did not land, which is indistinguishable
        # from one that landed and was lost. That is `indeterminate`, and it is the same rule
        # `mission_turn_reconcile.reconcile()` already applies — stated once, in both places.
        reserved = _stored_action_ids(row) or []
        missing = [a for a in reserved if a not in set(found)]
        if missing:
            return await _settle_indeterminate(
                mission_id,
                turn_id,
                fence,
                reserved,
                why="a reserved action is missing from the ledger",
            )
        # …and the same decision-2 rule as the happy path: an action still live keeps the turn
        # unresolved, which keeps the archive fence shut.
        st2, terminal = await asyncio.to_thread(_all_terminal, found)
        if st2 != "ok" or not terminal:
            # The RECEIPT, like every other in-progress path. `found` is provenance — the right
            # source for deciding whether a write happened, and the wrong one for telling the
            # operator what this turn is waiting on, because the two can differ and then the same
            # open turn describes itself differently depending on which path answered.
            return _in_progress(
                turn_id,
                _stored_action_ids(row) or found,
                delivery_error=_stored_delivery_error(row),
            )
        # The recovered turn gets its assistant event too, in the SAME settlement transaction.
        # Settling without one left a TERMINAL turn with `assistant_seq` NULL and nothing that
        # ever repaired it — the invariant this PR claims ("a settled turn carries both events")
        # was true on the happy path and false on exactly the crash path it exists for.
        # The reply itself died with the writer, so the event records what IS known: which
        # actions the turn produced, and that this was reconciled rather than answered.
        won = await missions.run_admitted(
            lambda: missions.settle_turn(
                mission_id,
                turn_id,
                fence,
                result=None,
                action_ids=found,
                assistant_text="",
                assistant_meta={"turn_id": turn_id, "actions": found, "recovered": True},
            )
        )
        if not won:
            # Somebody else settled first. Report what the STORE says rather than what this frame
            # hoped — a lost CAS that still answers "done" is the lie the fence exists to prevent,
            # one layer up.
            return _replay(
                await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id))
            )
        return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))
    won = await missions.run_admitted(
        lambda: missions.abandon_turn(mission_id, turn_id, fence, turn_id_meta=turn_id)
    )
    if not won:
        return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))
    return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))


def _stored_delivery_error(row: dict | None) -> str | None:
    """The delivery failure recorded on an OPEN turn, so a replay reports it too.

    `settle_turn` was the only thing that persisted `delivery_error`, and since decision 2 keeps
    a failed-delivery turn `in_progress` it never gets there — the failure was shown once and
    lost. `note_turn_delivery_error` writes it; this reads it back.
    """
    try:
        meta = json.loads((row or {}).get("result_meta") or "{}") or {}
    except (TypeError, ValueError):
        return None
    err = meta.get("delivery_error")
    return err if isinstance(err, str) and err else None


def _stored_action_ids(row: dict | None) -> list[str]:
    """The ids a turn RESERVED, from its own receipt. Never a guess, never a scan."""
    try:
        parsed = json.loads((row or {}).get("action_ids") or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in parsed if a] if isinstance(parsed, list) else []


def _in_progress(turn_id: str, actions: list[str], *, delivery_error: str | None = None) -> dict:
    """The one in-progress body, so the paths that return it cannot disagree.

    Three paths answer "still running" — a live owner, a completion held open because its action
    is live, and a recovery in the same position — and they were returning different shapes, so a
    caller comparing two replies saw them differ for no reason the operator could act on. It
    carries the ACTIONS, because "in progress" with nothing to look at tells the operator nothing
    about what is waiting, and the delivery error when there is one: a failure that is recorded
    and never shown is not a failure that was reported.
    """
    body: dict = {"turn_id": turn_id, "state": "in_progress", "actions": _hydrate_actions(actions)}
    if delivery_error:
        body["delivery_error"] = delivery_error
    return body


def _all_terminal(action_ids: list[str]) -> tuple[str, bool]:
    """`(status, all_terminal)` — may this turn settle yet? (#871 decision 2.)

    **A turn is not terminal until its action is**, and until now that rule was enforced only in
    the reconciler. Both completion paths settled a turn while its action was still `proposed` or
    `approved`, and a settled turn drops out of `unresolved_turn_keys`, which OPENS the archive
    fence — so a stale approve could still deliver into an archived mission. The exact failure
    decision 2 exists to prevent, reachable from the ordinary happy path (review on #881).

    Serialized AND checked: an unreadable ledger, or one mid-append, must not read as "no live
    actions" — that is the same fail-open in a third disguise.
    """
    if not action_ids:
        return "ok", True
    try:
        status, latest = orchestrator_ledger.latest_by_id_serialized_checked()
    except Exception:  # noqa: BLE001
        return "unreadable", False
    if status != "ok":
        return "unreadable", False
    for aid in action_ids:
        rec = latest.get(aid)
        if rec is None or str(rec.get("state") or "") in orchestrator_ledger.LIVE_STATES:
            return "ok", False
    return "ok", True


def _actions_for_turn(mission_id: str, turn_id: str) -> tuple[str, list[str]]:
    """`("ok", ids)` / `("unreadable", [])` — ledger action ids stamped with this turn's
    provenance.

    **Tri-state, because the caller settles a turn TERMINAL on "none".** Returning a bare list
    made an unreadable ledger indistinguishable from an empty one: `_read_all_at` maps an
    `OSError` to `[]`, so a transient permission or I/O error read as "nothing was written",
    `_reconcile` abandoned the turn `indeterminate`, and a terminal turn drops out of
    `unresolved_turn_keys` — opening the archive fence that exists to keep it shut. The
    `contextlib.suppress` here made it worse by swallowing the raising cases too (review on
    #881).

    Matched on **both** halves of the key. Two missions may legitimately use the same `turn_id`,
    so matching on the id alone lets mission B's recovery adopt mission A's actions and report an
    instruction it never sent.

    Safe to read as "none were written" ONLY because compaction is pinned while the turn is
    unresolved (`missions.unresolved_turn_keys`) — otherwise a compacted action and one that was
    never written would be the same observation.
    """
    try:
        # SERIALIZED against the ledger writer AND tri-state. Serialized because the gated append
        # holds that lock across gate-then-append, so an unlocked read can see nothing while the
        # write is moments away; tri-state because an unreadable file is not an empty one.
        status, latest = orchestrator_ledger.latest_by_id_serialized_checked()
    except Exception:  # noqa: BLE001
        return "unreadable", []
    if status != "ok":
        return "unreadable", []
    return "ok", [
        str(rec.get("id"))
        for rec in latest.values()
        if str(rec.get("turn_id") or "") == turn_id
        and str(rec.get("mission_id") or "") == mission_id
    ]


async def _run_turn(
    mission_id: str, turn_id: str, text: str, row: dict | None, *, registry=None
) -> dict:
    """Call the model under this turn's fence, then settle. The only path that calls `ask`."""
    fence = (row or {}).get("fence") or ""

    delivery_error: str | None = None

    def _reserve(intended: list[str]) -> bool:
        return missions.reserve_turn_write(mission_id, turn_id, fence, intended)

    # Renewed for as long as the model call runs, so the claim's expiry means the owner STOPPED
    # rather than that the model was slow — the distinction #846 paid several rounds for.
    async with missions.holding_turn(mission_id, turn_id, fence):
        try:
            # The SAME single-flight kind the sibling chat route uses, so a mission turn and a
            # Pulse turn cannot interleave with each other or with the delivery of what either
            # just approved. Without it this route was a second, unfenced path to the actuator.
            async with aitasks.single_flight("pulse-chat", "orchestrate"):
                # Every transient precondition is settled BEFORE the event is written, not just
                # the flight. `ask` checks configuration internally and raises from inside — well
                # after the append — so an unconfigured endpoint left an orphan `operator_msg`
                # behind and the retry appended a second one, each eviction costing a retained
                # recap at the cap. Preflighting here is what makes "written once the transient
                # window has closed" true rather than nearly true.
                # The configuration preflight lives EARLIER, before the claim — see the
                # "transient conditions are settled before anything durable exists" block at the
                # top of this handler. An earlier revision of this comment said the preflight was
                # DELETED, which was decision 3 as first published and is no longer what the code
                # does; leaving it here left one file arguing with itself (review on #881).
                #
                # The correction, briefly: a gap between two checks only matters if the two sides
                # can disagree, and they no longer can — a failure arriving after the claim
                # settles the turn forward rather than releasing it. `ask` remains the single
                # authority on whether the endpoint is usable; the early-out only spares an
                # ordinary misconfigured install from burning a turn id.
                #
                # The operator's message is already in the timeline: it was written inside the
                # claim transaction, so claim-and-event is atomic and there is no orphan to
                # clean up on any path. A `NotConfiguredError` from `ask` now settles the turn
                # FORWARD with an honest failure outcome, like every other failure here.
                result = await orchestrator_chat.ask(
                    text,
                    working_keys=actuator.working_keys(registry),
                    turn_id=turn_id,
                    mission_id=mission_id,
                    reserve_write=_reserve,
                )
                # Inside the single-flight, exactly as the sibling does it.
                if result.get("intent") == "instruct":
                    # Delivery failure is its OWN outcome. Letting it fall into the generic
                    # handler below made it look like a crash after the ledger write, which
                    # settles `done` from the mere presence of the action — reporting success
                    # for an instruction still sitting `approved` and undelivered.
                    try:
                        await actuator.deliver_pass_actions(
                            result.get("actions") or [], registry=registry
                        )
                    except Exception as de:  # noqa: BLE001
                        delivery_error = type(de).__name__
                        log.warning("mission %s: delivery failed (%s)", mission_id, delivery_error)
                    # Re-read from the LEDGER, not from what the helper returned.
                    # `deliver_pass_actions` omits an action another caller already claimed or
                    # settled, so reporting the pre-delivery row would tell the operator a tap is
                    # still needed for something already delivered. The ledger is the authority
                    # on state; the helper only reports what IT did.
                    acts = result.get("actions") or []
                    if acts:
                        latest = await asyncio.to_thread(
                            lambda ids: {i: orchestrator_ledger.get(i) for i in ids},
                            [a["id"] for a in acts if a.get("id")],
                        )
                        result["actions"] = [latest.get(a.get("id")) or a for a in acts]
        except (aitasks.AlreadyRunning, review.NotConfiguredError) as e:
            # The NARROW RACE past the early-outs above: the flight was free and the endpoint was
            # configured when this request checked, and one of them changed before `ask` reached
            # it. Rare by construction, and it is the case #871 decision 3 exists for.
            #
            # SETTLE FORWARD — never release. The operator's message is already in the timeline
            # (it was written inside the claim transaction), so deleting the claim would leave it
            # behind with nothing that owns it: exactly the "released the claim but not the
            # event" state that caused five consecutive fix-caused-the-next-defect rounds on
            # #852. Nothing is released and nothing is deleted; the turn ends with an honest
            # outcome and its message stays where the operator put it.
            #
            # The cost is that this `turn_id` is spent — the right trade here, because it is only
            # spent when the condition changed mid-flight, and the next message carries a new one.
            await missions.run_admitted(lambda: missions.abandon_turn(mission_id, turn_id, fence))
            detail = (
                "a question is already running"
                if isinstance(e, aitasks.AlreadyRunning)
                else "AI endpoint is not configured"
            )
            raise missions.MissionError(detail, status=409) from None
        except Exception as e:  # noqa: BLE001
            # A failure is NOT proof that nothing was written. `_persist` appends and fsyncs
            # before its fallible compaction step, so an exception can arrive after the action is
            # already durable — abandoning then reports `indeterminate` for an instruction the
            # ledger will happily deliver. Ask the receipt first, exactly as recovery does.
            reserved = await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id))
            if (reserved or {}).get("write_reserved_at"):
                return await _reconcile(mission_id, turn_id, {**(reserved or {}), "fence": fence})
            await missions.run_admitted(lambda: missions.abandon_turn(mission_id, turn_id, fence))
            raise missions.MissionError(
                f"the chat backend failed ({type(e).__name__})", status=502
            ) from None

        actions = [str(a.get("id")) for a in (result.get("actions") or []) if a.get("id")]
        # `answer` is what `orchestrator_chat.ask` returns — every branch of it. Reading `reply`
        # meant every successful turn stored and returned `null`, and no test caught it because
        # they all exercised the claim machinery rather than a completed call.
        answer = result.get("answer")

        # DECISION 2, on the happy path — not only in the reconciler. A turn whose action is
        # still `proposed`/`approved` stays IN PROGRESS: settling it would drop it out of
        # `unresolved_turn_keys` and open the archive fence while that action can still be
        # approved and delivered. The state table says exactly this — "claimed, after append |
        # approved | turn terminal? NO | archive 409 | replay: in-progress".
        #
        # It resolves on its own: the TTL expires the action, and the reconciler then carries
        # that conclusion onto the turn. Nothing is re-sent and nothing waits for ever.
        st, terminal = await asyncio.to_thread(_all_terminal, actions)
        if st != "ok" or not terminal:
            if delivery_error:
                # Persisted, because this turn is NOT settling and `settle_turn` was the only
                # thing that stored it — so the failure would be shown once and lost thereafter.
                await missions.run_admitted(
                    lambda: missions.note_turn_delivery_error(
                        mission_id, turn_id, fence, delivery_error
                    )
                )
            # Unowned, because this frame is done and the turn is waiting on its ACTION rather
            # than on a worker. Holding the lease would make it read "someone is running this"
            # for five minutes while nobody is.
            await missions.run_admitted(lambda: missions.park_turn(mission_id, turn_id, fence))
            return _in_progress(turn_id, actions, delivery_error=delivery_error)

        # The assistant event rides IN the settlement transaction, so a settled turn always has
        # its answer in the timeline. There is no "append it afterwards and hope" path left.
        won = await missions.run_admitted(
            lambda: missions.settle_turn(
                mission_id,
                turn_id,
                fence,
                result=answer,
                # `done` means the instruction went out. When delivery raised we cannot say
                # that — the action may be `approved` and untouched, or `claimed` and half
                # delivered — so the turn settles `indeterminate`, which is exactly the state
                # this store already uses for "we will not guess". The action's own ledger state
                # rides in the response beside it, so the operator sees what is actually true.
                state="indeterminate" if delivery_error else "done",
                result_meta={
                    "intent": result.get("intent"),
                    "delivery_error": delivery_error,
                    "matches": result.get("matches") or [],
                },
                action_ids=actions,
                # Frozen here, so every later replay returns exactly this.
                action_snapshot=_hydrate_actions(actions),
                assistant_text=answer or "",
                assistant_meta={
                    "turn_id": turn_id,
                    "intent": result.get("intent"),
                    "actions": actions,
                    # …on the EVENT too, because the thread renders from the timeline rather than
                    # from the response: a reload has only the event to work from.
                    "matches": result.get("matches") or [],
                },
            )
        )

    if not won:
        # This frame was fenced out while the model ran and somebody else has already settled the
        # turn. Returning "done" here would report an outcome this request does not own — so the
        # store answers, not this frame.
        return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))

    # **The response IS the stored record**, on the first request exactly as on a replay. Building
    # two shapes and aligning their fields is how they drifted: one carried `fenced`, the other
    # `replayed`, and the test compared five chosen keys and stayed green. Identical by
    # construction beats identical by inspection.
    return _replay(await missions.run_admitted(lambda: missions.get_turn(mission_id, turn_id)))
