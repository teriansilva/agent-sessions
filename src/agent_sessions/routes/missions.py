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
design pass). Notably absent, on purpose: ``/plan``, ``/dispatch``, ``/answer`` and the playbook
routes are Phases 3–5. And there is **no new decision endpoint** — approve/reject stay
``/api/pulse/actions/{id}/approve|reject`` (#840 §14).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from .. import (
    actuator,
    aitasks,
    engines,
    gitpanel,
    mission_archive,
    mission_objectives,
    mission_supervisor,
    mission_turn_reconcile,
    missions,
    orchestrator_chat,
    orchestrator_ledger,
    projects,
    review,
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


def _resolve_cwd(project_id: object) -> tuple[str | None, str | None]:
    """Resolve the launch path SERVER-side from a project id. The client never supplies one.

    ``cwd`` is the field where getting it wrong is a path-traversal bug rather than a wrong link,
    which is why #840 §4 makes it the server's to author: the model picks an *index* into a
    server-built list and the entity supplies the path. A route that persisted ``body["cwd"]``
    would hand that same authority to any caller, and the schema's launch ``CHECK`` would then be
    satisfied by a path nobody validated — the gate would be guarding a value the client chose.

    Returns ``(project_id, cwd)``. No project ⇒ ``(None, None)``: a draft is *allowed* to have no
    resolved path, and that is exactly the state in which the console asks which project was
    meant.
    """
    if project_id is None or project_id == "":
        return None, None
    if not isinstance(project_id, str):
        raise missions.MissionError("project_id must be a string", status=422)
    try:
        entity = projects.load().get(project_id)
    except Exception:  # noqa: BLE001 — an unreadable projects file is not a 500 here
        raise missions.MissionError("could not resolve the project", status=503) from None
    if entity is None:
        raise missions.MissionError("unknown project", status=404)
    cwd = entity.default_folder or (entity.folders[0] if entity.folders else "")
    if not cwd:
        raise missions.MissionError("that project has no folder to work in", status=422)
    return entity.id, cwd


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
            return JSONResponse(
                row,
                status_code=201,
                background=BackgroundTask(mission_objectives.propose_for_new_mission, row["id"]),
            )
        except missions.MissionError as e:
            return _fail(e)

    @app.get("/api/missions/{mission_id}")
    async def get_mission_route(
        mission_id: str, request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        qp = request.query_params
        before = qp.get("events_before_seq")
        try:
            missions.validate_id(mission_id)
            row = await missions.run_admitted(
                lambda: missions.safe_get_mission(
                    mission_id,
                    events_limit=_int(qp.get("events_limit"), missions.EVENTS_PAGE_DEFAULT),
                    events_before_seq=_cursor(before),
                )
            )
        except missions.MissionError as e:
            return _fail(e)
        if row is None:
            return JSONResponse({"detail": f"unknown mission {mission_id}"}, status_code=404)
        with contextlib.suppress(Exception):
            flags = await missions.run_admitted(lambda: missions.derive_needs_you([mission_id]))
            row["needs_you"] = bool(flags.get(mission_id, {}).get("needs_you"))
            row["needs_you_why"] = flags.get(mission_id, {}).get("why") or []
        # THE SUPERVISOR'S READING (#885), derived at read time like `needs_you` above and for the
        # same reason: it is a projection of the ledger and the objective store, so caching it
        # would just be a second copy that can disagree with both.
        #
        # Suppressed rather than fatal: a mission's page must still render when the ledger is
        # unreadable. The console shows nothing rather than something wrong, which is the same
        # posture `MissionObjectives` already takes for a probe that could not run.
        with contextlib.suppress(Exception):
            row["supervisor"] = await missions.run_admitted(
                lambda: mission_supervisor.assess(mission_id)
            )
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
            return JSONResponse(
                await missions.run_admitted(
                    # `body.get("role") or "primary"` silently turned a malformed falsy value
                    # (`{}`, `[]`, `0`) into the default instead of rejecting it. Absent means
                    # default; present means it has to be a real string the store recognises.
                    lambda: missions.adopt(
                        mission_id,
                        key,
                        role="primary" if body.get("role") is None else body.get("role"),
                    )
                )
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
            phys = engines.physical_key(key)
            # FENCED. Detaching withdraws this session's authority, and an automatic delivery may
            # be mid-flight: the write fence compares the per-session epoch immediately before
            # byte one, holding the registry lock. Committing the detach inside that same lock is
            # what makes the two orderable — the send either completes first, or waits and then
            # sees the new epoch. Unfenced, the withdrawal could land between the fence's
            # comparison and `os.write()` and the old mission still typed into the session
            # (#888 review, finding 1).
            out = await _fenced_write([phys], lambda: missions.detach(mission_id, key))
            return JSONResponse(out)
        except missions.MissionError as e:
            return _fail(e)

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
            keys = await _held_physical_keys(mission_id)
            return JSONResponse(
                await _fenced_write(
                    keys,
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
            "sessions": m.get("sessions") or [],
            "git": None,
            "git_error": None,
        }
        if cwd:
            try:
                out["git"] = await files_routes.run_bounded(cwd, gitpanel.git_status, cwd)
            except Exception as e:  # noqa: BLE001 — fail CLOSED and name the kind, never the path
                out["git_error"] = type(e).__name__
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
            keys = await _held_physical_keys(mission_id)
            ok = await _fenced_write(
                keys, lambda: missions.stand_down(mission_id, objective_key, episode=episode)
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
        """Operator edits only — ``add`` / ``drop`` / ``retitle`` / ``waive`` / ``reorder``.

        ``state`` / ``met_at`` / ``observed`` are not writable here at all, so an edit can never
        retroactively mark an objective met. ``probe`` and ``probe_args`` ARE accepted on this
        path because it is the operator's — the model path (Phase 3) selects a playbook template
        index and never authors a probe target, which is what keeps an SSRF-with-a-cadence out of
        the design. Validation lives in the store so both paths inherit the same gate.
        """
        try:
            body = await _body(request)
            # Dropping or waiving an objective withdraws the authority behind any in-flight nudge
            # aimed at it, so the edit commits inside the write fence — see `detach_route`.
            keys = await _held_physical_keys(mission_id)
            rows = await _fenced_write(
                keys,
                lambda: missions.patch_objectives(
                    mission_id, body.get("ops") or [], source="operator"
                ),
            )
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse({"objectives": rows})


async def _fenced_write(keys: list[str], fn):
    """Run a store mutation INSIDE the write fence, entirely on a worker thread.

    Both halves matter and they pull in opposite directions.

    The fence must be held across the store write, or the mutation can land between the fence's
    comparison and byte one. But `session_input._lock` is a plain `threading.Lock`, and holding it
    around an `await` on the event loop is a deadlock: request A suspends inside the lock waiting
    for its worker, request B enters the same block on the loop thread and blocks it, and A can
    never resume to release. Hermes reproduced exactly that hang (#888 review, finding 4).

    So the lock and the synchronous mutation go into the SAME callable and that callable runs off
    the loop. The fence still encloses the write; the loop thread never touches the lock.
    """

    def _run():
        with session_input.sessions_transaction(keys):
            return fn()

    return await missions.run_admitted(_run)


async def _held_physical_keys(mission_id: str) -> list[str]:
    """The physical keys of every session this mission currently holds.

    Physical, not app-facing: the write fence is keyed on the pty, which is what `session_input`
    bumps and compares.
    """
    try:
        row = await missions.run_admitted(lambda: missions.get_mission(mission_id))
    except Exception:  # noqa: BLE001 — a fence that cannot enumerate still must not block the edit
        return []
    out: list[str] = []
    for srow in (row or {}).get("sessions") or []:
        if srow.get("removed_at") is not None:
            continue
        key = str(srow.get("session_key") or "")
        if not key:
            continue
        with contextlib.suppress(Exception):
            out.append(engines.physical_key(key))
    return out


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
                result_meta={"intent": result.get("intent"), "delivery_error": delivery_error},
                action_ids=actions,
                # Frozen here, so every later replay returns exactly this.
                action_snapshot=_hydrate_actions(actions),
                assistant_text=answer or "",
                assistant_meta={
                    "turn_id": turn_id,
                    "intent": result.get("intent"),
                    "actions": actions,
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
