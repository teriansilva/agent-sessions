"""The steps of a mission's plan, one API session per agent step (#1581 S1, #1613 D15).

* ``GET  /api/missions/{id}/steps``                       — each step, its runs, can it start now
* ``POST /api/missions/{id}/steps/{step}/start``          — ``{revision, expect_cwd, alternate?}``
* ``POST /api/missions/{id}/steps/{step}/stop``           — ``{episode}``

The operator's own routes: a session for reads, the session AND ``csrf_guard`` (which carries the
Origin/Referer check) for writes, exact-key bounded bodies, ``no-store``. Start is the operator's
tap (`mission_steps.Authority("operator")`); every fence lives in `mission_steps.start_step`, the
one seam #1951 slice B also launches through. Stop only reduces an effect.
"""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import mission_steps, missions

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
BODY_MAX = 4 * 1024
START_KEYS = frozenset({"revision", "expect_cwd", "alternate"})
STOP_KEYS = frozenset({"episode"})


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _fail(e: missions.MissionError) -> JSONResponse:
    body: dict = {"detail": str(e)}
    if isinstance(e, missions.StepRefused):
        body["code"] = e.code
        body.update({k: v for k, v in e.extra.items() if k != "detail"})
    return _json(body, e.status)


async def _body(request: Request, keys: frozenset[str]) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise missions.MissionError("request body is too large", status=413)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise missions.MissionError("request body is too large", status=413)
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise missions.MissionError("request body is not valid JSON", status=422) from None
    if not isinstance(payload, dict):
        raise missions.MissionError("request body must be a JSON object", status=422)
    extra = sorted(set(payload) - keys)
    if extra:
        raise missions.MissionError(f"unknown field {extra[0]!r}", status=422)
    return payload


def _int(value: object, what: str, *, upper: int = 999_999) -> int:
    if type(value) is not int or not 1 <= value <= upper:
        raise missions.MissionError(f"{what} must be a positive whole number", status=422)
    return value


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    @app.get("/api/missions/{mission_id}/steps")
    async def mission_steps_view(mission_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        try:
            missions.validate_id(mission_id)
            # A read: no reconcile here (it stops sessions). The supervisor's pass and every start
            # reconcile; this shows the store as it is.
            return _json(await missions.run_admitted(lambda: mission_steps.view_sync(mission_id)))
        except missions.MissionError as e:
            return _fail(e)

    @app.post("/api/missions/{mission_id}/steps/{step_id}/start")
    async def mission_step_start(
        mission_id: str,
        step_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            missions.validate_id(mission_id)
            missions.validate_step_id(step_id)
            body = await _body(request, START_KEYS)
            revision = _int(body.get("revision"), "revision")
            expect_cwd = body.get("expect_cwd")
            if not isinstance(expect_cwd, str) or not expect_cwd.strip():
                raise missions.MissionError(
                    "expect_cwd is required and names the folder you approved", status=422
                )
            alt = body.get("alternate")
            if alt is None:
                use = mission_steps.PRIMARY
            elif type(alt) is int and 0 <= alt < 3:
                use = mission_steps.Use.alternate(alt, "")
            else:
                raise missions.MissionError("alternate is an alternate's index", status=422)
            out = await mission_steps.start_step(
                mission_id,
                step_id,
                revision=revision,
                use=use,
                expect_cwd=expect_cwd.strip(),
                registry=registry,
            )
        except missions.MissionError as e:
            return _fail(e)
        return _json(out)

    @app.post("/api/missions/{mission_id}/steps/{step_id}/stop")
    async def mission_step_stop(
        mission_id: str,
        step_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            missions.validate_id(mission_id)
            missions.validate_step_id(step_id)
            body = await _body(request, STOP_KEYS)
            episode = _int(body.get("episode"), "episode")
            run = await mission_steps.stop_step(mission_id, step_id, episode)
        except missions.MissionError as e:
            return _fail(e)
        return _json({"run": run})
