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

Notably absent, on purpose: ``/message`` is **#871** (split out of #852 after its lifecycle proved
to need its own design pass); ``/plan``, ``/dispatch``, ``/answer`` and the playbook routes are
Phases 3–5. And there is **no new decision endpoint** — approve/reject stay
``/api/pulse/actions/{id}/approve|reject`` (#840 §14).
"""

from __future__ import annotations

import contextlib
import json
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from .. import (
    engines,
    gitpanel,
    mission_archive,
    mission_objectives,
    missions,
    projects,
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
            return JSONResponse(
                await missions.run_admitted(lambda: missions.detach(mission_id, key))
            )
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
            return JSONResponse(
                await missions.run_admitted(
                    lambda: missions.set_state(
                        mission_id,
                        str(body.get("from") or ""),
                        str(body.get("to") or ""),
                        outcome=body.get("outcome"),
                        detail=str(body.get("detail") or ""),
                    )
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
            rows = await missions.run_admitted(
                lambda: missions.patch_objectives(
                    mission_id, body.get("ops") or [], source="operator"
                )
            )
        except missions.MissionError as e:
            return _fail(e)
        return JSONResponse({"objectives": rows})


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
