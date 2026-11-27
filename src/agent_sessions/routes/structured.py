"""Structured client routes (#1278). All `logged_in`; every mutation also `csrf_guard`.

These expose the transport-neutral facade (``structured_runtime``) for any client whose adapter
implements the operation — HTTP chat or a contained native worker. Request bodies carry only
operator input and caller-chosen UUIDs: no execution authority, worker capability, argv or
mission correlation is ever read from a browser (mission callers use the facade server-side).

* ``GET  /api/structured/clients/{engine}`` — implemented operations and live readiness.
* ``GET  /api/structured/clients/{engine}/models`` — the models the client's CLI reports (#1313).
* ``POST /api/structured/sessions {engine, cwd, operation_id, model?, bypass?, specialist?}`` —
  create; an exact repeat of ``operation_id`` returns the same session (a ``bypass`` one waits for
  ``start``). ``specialist`` is ``{id, version}`` (#1693): every generation re-reserves it.
  It answers once the worker is launched, not ready (#1505): the snapshot's ``launch`` view
  (``{gen, state, reason, since}``) turns ``ready`` or ``failed`` by itself.
* ``POST /api/structured/sessions/{key}/start`` — launch a skip-permissions creation (#1339);
  its create reserved and launched nothing. Idempotent. Answers at launch too (#1505).
* ``GET  /api/structured/sessions/{key}`` — bounded snapshot (turns, exact pending requests).
* ``GET  /api/structured/sessions/{key}/events?after=&limit=&wait=`` — durable journal cursor page;
  ``wait`` (≤ 3 s) holds a read with nothing new until the journal moves (#1727).
* ``GET  /api/structured/sessions/{key}/history?before=&limit=`` — the turns just older than one
  turn, for scrolling up past the snapshot's window (#1504); native clients only.
* ``POST /api/structured/sessions/{key}/turns {operation_id, text, expected_revision?,
  reply_to?}`` — ``reply_to``: the async question this message answers (#1463), admitted only
  while that question is still open.
* ``POST /api/structured/sessions/{key}/decisions {decision_id, turn_id, request_id, decision,
  grant?, expected_revision?}`` — one exact pending request: approve / reject / cancel, or
  ``always`` with the id of one proposed standing grant the snapshot offers (#1339).
* ``POST /api/structured/sessions/{key}/interrupt {operation_id, turn_id}``
* ``POST /api/structured/sessions/{key}/send-now {operation_id, turn_id, queued_turn_id}`` —
  steer or explicitly cancel the observed response and send the selected queued input next.
* ``POST /api/structured/sessions/{key}/withdraw {operation_id, queued_turn_id}`` — remove one
  still-queued message before it is sent (#1447).
* ``POST /api/structured/sessions/{key}/stop`` — close the worker; reports proved containment.
* ``POST /api/sessions/{key}/driver {who: "hq", generation}`` — the Commander hands an API session
  back to HQ (#2397): a compare-and-set on the driver's generation, refused in-process.
* ``POST /api/sessions/{key}/driver/adopt {generation}`` — HQ's ``session.adopt`` (#2397):
  in-process only, with its drive binding.

HQ's drive requests (#2397, `session_driver`): an IN-PROCESS ``/turns``, ``/interrupt`` or
``/withdraw`` carries the card's binding in its scope (`asgi_call.DRIVE`), never in its body, and
is admitted by it inside the write fence; one without a binding is refused, so the catalog's drive
entries are HQ's only way in. The Commander's own ``/turns`` (a request that is not in-process)
on a session HQ drives takes it over first, inside the same fence.
* ``GET  /api/structured/sessions/{key}/containment`` — live / gone / unknown.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import (
    asgi_call,
    engines,
    native_ipc,
    presence,
    session_driver,
    session_input,
    share_api,
    shares,
    structured_runtime,
)

log = logging.getLogger("agent_sessions.routes.structured")


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


async def _json(request: Request, allowed: set[str], required: set[str]) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be an object")
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown fields: {unknown}")
    missing = sorted(required - set(body))
    if missing:
        raise HTTPException(status_code=422, detail=f"missing fields: {missing}")
    return body


def _revision(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise HTTPException(status_code=422, detail="expected_revision must be an integer")
    return value


def _specialist(raw: object) -> tuple[str, int] | None:
    from .. import specialist_launch

    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"id", "version"}:
        raise HTTPException(status_code=422, detail="specialist must be {id, version}")
    try:
        parsed = specialist_launch.parse(raw["id"], raw["version"])
    except specialist_launch.Refused as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from None
    if parsed is None:  # an empty `{id, version}` is not "no specialist"
        raise HTTPException(status_code=422, detail="specialist must be {id, version}")
    return parsed


async def _until_gone(receive) -> None:
    """Returns when the client hangs up (#1727). Not `Request.is_disconnected()`: under the app's
    `@app.middleware("http")` layers its pre-cancelled read loses the disconnect message every
    time, so a waiting read would hold its slot to the end of its wait. A plain read does not."""
    while (await receive())["type"] != "http.disconnect":
        pass


async def _run(coroutine):
    try:
        return await coroutine
    except structured_runtime.StructuredError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


def _peer_authors(app: FastAPI, session_key: str) -> dict:
    """The turns a paired instance caused in this session (#2057). An app built without
    federation has none; a store that cannot be read raises, and attribution is then UNKNOWN."""
    federation = getattr(app.state, "federation", None)
    return {} if federation is None else federation.store.peer_authors(session_key)


def _decisions(app: FastAPI, session_key: str) -> list[dict]:
    """Who settled this session's newest requests (#2059): the operator, or a paired instance.
    An app built without federation has none."""
    federation = getattr(app.state, "federation", None)
    if federation is None:
        return []
    return share_api.decision_views(federation.store.decision_authors(session_key))


async def _settled(app: FastAPI, session_key: str, turn_id: str, request_id: str) -> None:
    """The operator's decision was accepted: record who settled the request, for the paired
    instances that can see it (#2059). The federation service reads the session's own journal
    for it and never raises: attribution is a record, not a gate."""
    federation = getattr(app.state, "federation", None)
    if federation is not None:
        await federation.owner_decided(session_key, turn_id, request_id)


def _canonical(key: str) -> str:
    """``engine:native`` as the driver store keys it; an unparseable key is left as it is (the
    runtime then answers 404)."""
    try:
        prov, native = engines.parse_key(key)
    except Exception:  # noqa: BLE001
        return key
    return f"{prov.engine_id}:{native}"


def _drive_binding(request: Request, verb: str) -> dict | None:
    """An HQ drive request's binding (#2397), or None for the Commander's own request. An
    in-process request without one is refused: HQ reaches a session's turns only through the
    catalog's drive entries, so there is no side door around their class."""
    if not asgi_call.in_process(request.scope):
        return None
    binding = session_driver.binding_of(request.scope.get(asgi_call.DRIVE))
    if binding is None or binding["verb"] != verb:
        raise HTTPException(
            status_code=403, detail="HQ reaches an API session only through a drive request"
        )
    return binding


def _refused(detail: str, *, grant: bool) -> JSONResponse:
    """A drive request refused before anything was claimed. ``grant``: the act's own grant refused
    it (the card returns to the Commander, `ask_operations._route_once`); else the driver moved
    (a closed reason the catalog reads as stale)."""
    if grant:
        return JSONResponse({"detail": detail, "act_refused": True}, status_code=409)
    return JSONResponse(
        {"detail": f"not sent: {detail}", "reason": session_driver.REFUSED_REASON},
        status_code=409,
    )


def _notice_take_over(key: str, rec: dict) -> None:
    """Say it in the conversation HQ drove the session from (#2397): an authored update, never
    model text. Best-effort: the take-over itself is already committed. Blocking."""
    from .. import ask_follow, ask_store

    cid = rec.get("cid")
    if not isinstance(cid, str) or not ask_store.valid_cid(cid):
        return
    try:
        ask_follow.notice(cid, key, kind="taken_over", title=rec.get("title") or "")
    except Exception:  # noqa: BLE001 — a notice never undoes the take-over
        log.warning("take-over notice not recorded", exc_info=True)


def driver_view(key: str) -> dict:
    """Who drives API session ``key``, as its header shows it (#2397): HQ (and from which
    conversation), the Commander, or a mission; whether Hand back is offered. Blocking."""
    from .. import ask_store, missions

    v = session_driver.view(key)
    out = {k: v.get(k) for k in ("who", "generation", "since")}
    if v.get("who") == session_driver.MISSION:
        row = {}
        try:
            row = missions.active_membership_rows().get(key) or {}
        except Exception:  # noqa: BLE001 — the chip then names no mission
            row = {}
        out["mission"] = {"id": v.get("mission_id"), "title": str(row.get("title") or "")[:120]}
    cid = v.get("cid")
    conversation = None
    if isinstance(cid, str) and ask_store.valid_cid(cid) and ask_store.exists(cid):
        title = ""
        try:
            title = ask_store.summary(cid, ask_store._path(cid), 0.0)["title"]
        except Exception:  # noqa: BLE001 — named without its title
            title = ""
        conversation = {"id": cid, "title": title}
    if conversation is not None:
        out["conversation"] = conversation
    # Hand back is offered only where it can return the session somewhere: a conversation HQ
    # drove it from that still exists. A session HQ never drove says "Ask HQ to take it over".
    out["can_hand_back"] = v.get("who") == session_driver.COMMANDER and conversation is not None
    return out


async def _take_over(key: str) -> None:
    """The Commander typed in ``key`` (#2397): if HQ drives it, it is theirs now. Committed inside
    the write fence BEFORE their turn is submitted, so an HQ send waiting for its admission is
    refused there, and one admitted but not yet claimed is refused by the worker."""
    try:
        rec = await asyncio.to_thread(session_driver.take_over, key)
    except session_input.AuthorityFenceBusy:
        raise HTTPException(
            status_code=503, detail="this session is busy right now; send again in a moment"
        ) from None
    if rec is not None:
        await asyncio.to_thread(_notice_take_over, key, rec)


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    async def _drive_failed(key: str, drive: dict, refused: dict, exc, act: bool) -> JSONResponse:
        """A drive turn or interrupt the runtime refused: by the act's grant (back to the
        Commander), by the driver check at admission, or by the worker at its claim (the tokens
        were replaced by a take-over or an adoption: re-read why)."""
        if refused["grant"]:
            return _refused(refused["grant"], grant=True)
        if refused["why"]:
            return _refused(refused["why"], grant=False)
        if getattr(exc, "reason", None) == native_ipc.AUTHORITY_WITHDRAWN:
            _ok, why = await asyncio.to_thread(session_driver.check, key, drive)
            return _refused(why or "its authority was withdrawn before it was claimed", grant=False)
        if act and exc.status == 409:
            # The act's class at the boundary (#2397): an unattended send is refused in a session
            # that skips permission prompts, and that is a session whose send is *external*.
            try:
                bypass = await asyncio.to_thread(session_driver.bypass_now, key)
            except Exception:  # noqa: BLE001 — the runtime's own refusal stands
                bypass = False
            if bypass:
                return _refused("this session skips permission prompts now", grant=False)
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    async def _drive_send(key: str, body: dict, drive: dict, request: Request) -> JSONResponse:
        """HQ's ``session.send`` (#2397): one turn, its id derived from the card, admitted by the
        card's binding inside the write fence and re-checked at the worker's claim."""
        if any(body.get(k) is not None for k in ("attachments", "reply_to", "expected_revision")):
            raise HTTPException(status_code=422, detail="a drive send is its text only")
        if body["operation_id"] != session_driver.send_operation_id(drive["pid"]):
            raise HTTPException(status_code=422, detail="a drive send uses its card's turn id")
        act_admit = request.scope.get(asgi_call.ACT_ADMIT)
        guard, refused = session_driver.guard(
            key, drive, act_admit=act_admit, attended=act_admit is None
        )
        try:
            out = await structured_runtime.submit_turn(
                key,
                operation_id=body["operation_id"],
                text=body["text"],
                context={"reservation_id": drive["pid"]},
                execution_admission=guard,
            )
        except structured_runtime.StructuredError as exc:
            return await _drive_failed(key, drive, refused, exc, act_admit is not None)
        return JSONResponse(out, status_code=202 if out.get("state") == "running" else 200)

    async def _drive_interrupt(key: str, body: dict, drive: dict, request: Request) -> JSONResponse:
        if body["operation_id"] != session_driver.interrupt_operation_id(drive["pid"]):
            raise HTTPException(status_code=422, detail="a drive interrupt uses its card's id")
        act_admit = request.scope.get(asgi_call.ACT_ADMIT)
        guard, refused = session_driver.guard(
            key, drive, act_admit=act_admit, attended=act_admit is None
        )
        try:
            out = await structured_runtime.interrupt(
                key,
                operation_id=body["operation_id"],
                turn_id=body["turn_id"],
                execution_admission=guard,
            )
        except structured_runtime.StructuredError as exc:
            return await _drive_failed(key, drive, refused, exc, False)
        return JSONResponse(out, status_code=202)

    async def _drive_withdraw(key: str, body: dict, drive: dict) -> JSONResponse:
        """HQ withdraws a queued turn HQ itself sent (#2397). It adds nothing to the session, so
        the driver check at the route suffices (the invariant says so); the turn must be one of
        HQ's drive sends."""
        if body["operation_id"] != session_driver.withdraw_operation_id(drive["pid"]):
            raise HTTPException(status_code=422, detail="a drive withdraw uses its card's id")
        snap = await _run(structured_runtime.snapshot(key))
        turn = next(
            (t for t in snap.get("turns") or [] if t.get("turn_id") == body["queued_turn_id"]),
            None,
        )
        rid = ((turn or {}).get("context") or {}).get("reservation_id")
        if (
            turn is None
            or not isinstance(rid, str)
            or (body["queued_turn_id"] != session_driver.send_operation_id(rid))
        ):
            raise HTTPException(status_code=422, detail="HQ withdraws only a message HQ sent")
        ok, why = await asyncio.to_thread(session_driver.check, key, drive)
        if not ok:
            return _refused(why, grant=False)
        return JSONResponse(await _run(structured_runtime.withdraw(key, **body)), status_code=202)

    @app.post("/api/sessions/{sid}/driver")
    async def session_hand_back(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """The Commander hands an API session back to HQ (#2397). Never HQ itself."""
        from .. import ask_store, mission_structured

        key = sid
        if asgi_call.in_process(request.scope):
            raise HTTPException(status_code=403, detail="only the Commander hands a session back")
        body = await _json(request, {"who", "generation"}, {"who", "generation"})
        if body["who"] != session_driver.HQ:
            raise HTTPException(status_code=422, detail="who must be hq")
        gen = body["generation"]
        if type(gen) is not int or gen < 0:
            raise HTTPException(status_code=422, detail="generation must be the one you read")
        canon = _canonical(key)
        if not await asyncio.to_thread(mission_structured.is_api_key, canon):
            raise HTTPException(status_code=422, detail="HQ drives API sessions only")
        rec = await asyncio.to_thread(session_driver.record, canon)
        cid = (rec or {}).get("cid")
        if cid and not await asyncio.to_thread(ask_store.exists, cid):
            raise HTTPException(
                status_code=422,
                detail="the conversation HQ drove it from is gone: ask HQ to take it over",
            )
        try:
            await asyncio.to_thread(session_driver.hand_back, canon, gen)
        except session_driver.DriverError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from None
        except session_input.AuthorityFenceBusy:
            raise HTTPException(
                status_code=503, detail="this session is busy right now; try again in a moment"
            ) from None
        return JSONResponse(await asyncio.to_thread(driver_view, canon))

    @app.post("/api/sessions/{sid}/driver/adopt")
    async def session_adopt(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """HQ's ``session.adopt`` (#2397): in-process only, with its card's binding. A CAS on the
        driver's generation inside the write fence; the act's grant and (for an act) the
        session's permission mode are asked there too."""
        from .. import ask_follow, mission_structured

        key = sid
        drive = _drive_binding(request, "adopt")
        if drive is None:
            raise HTTPException(
                status_code=403, detail="HQ takes a session over only through its card"
            )
        body = await _json(request, {"generation"}, {"generation"})
        if body["generation"] != drive["generation"]:
            raise HTTPException(status_code=422, detail="the generation is the card's")
        canon = _canonical(key)
        if not await asyncio.to_thread(mission_structured.is_api_key, canon):
            raise HTTPException(status_code=422, detail="HQ drives API sessions only")
        act_admit = request.scope.get(asgi_call.ACT_ADMIT)

        def admit() -> str:
            why = act_admit() if act_admit is not None else ""
            if not why and act_admit is not None and session_driver.bypass_now(canon):
                # An act's adopt of a session that skips permission prompts is *external*.
                raise session_driver.DriverError(409, "this session skips permission prompts now")
            return why

        try:
            await asyncio.to_thread(
                lambda: session_driver.adopt(
                    canon,
                    cid=drive["cid"],
                    generation=drive["generation"],
                    title=str(drive.get("title") or ""),
                    admit=admit,
                )
            )
        except session_driver.GrantRefused as exc:
            return _refused(exc.detail, grant=True)
        except session_driver.DriverError as exc:
            return _refused(exc.detail, grant=False)
        except session_input.AuthorityFenceBusy:
            raise HTTPException(
                status_code=503, detail="this session is busy right now; try again in a moment"
            ) from None
        try:
            await asyncio.to_thread(
                lambda: ask_follow.follow(
                    drive["cid"], canon, title=str(drive.get("title") or ""), via="adopt",
                    reason=drive["pid"],
                )
            )  # fmt: skip
        except Exception:  # noqa: BLE001 — following is a convenience; the adoption stands
            log.warning("adopted session not followed", exc_info=True)
        return JSONResponse(await asyncio.to_thread(driver_view, canon))

    @app.get("/api/structured/clients/{engine}")
    async def structured_client(engine: str, _user: str = Depends(logged_in)) -> JSONResponse:
        import asyncio

        descriptor = await asyncio.to_thread(structured_runtime.describe, engine)
        return JSONResponse(descriptor.as_dict())

    @app.get("/api/structured/clients/{engine}/models")
    async def structured_models(engine: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # What the client's own CLI says it can run (#1313); `unavailable` offers `default` only.
        import asyncio

        from .. import engines, native_models

        prov = engines.get(engine)
        if prov is None or getattr(prov.manifest, "runtime", None) != "api":
            raise HTTPException(status_code=404, detail="no such API client")
        listing = await asyncio.to_thread(native_models.models, prov)
        return JSONResponse(listing.as_dict())

    @app.post("/api/structured/sessions")
    async def structured_create(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        body = await _json(
            request,
            {"engine", "cwd", "operation_id", "model", "effort", "bypass", "specialist", "choice"},
            {"engine", "cwd", "operation_id"},
        )
        if not isinstance(body["engine"], str):
            raise HTTPException(status_code=422, detail="engine must be a string")
        # API first (#1728): a create of the AUTOMATIC default the form showed is refused, with the
        # new default, once its family's default moved; an explicit choice is honoured as before.
        from .. import engine_choice

        stale = await engine_choice.refuse_stale(
            body["engine"], body.get("choice"), body["operation_id"]
        )
        if stale is not None:
            return stale
        if not isinstance(body.get("bypass", False), bool):
            raise HTTPException(status_code=422, detail="bypass must be a boolean")
        if body.get("effort") is not None and not isinstance(body["effort"], str):
            raise HTTPException(status_code=422, detail="effort must be a string")
        # Started from a specialist (#1693 S4b): `{id, version}`, re-validated where it launches.
        specialist = _specialist(body.get("specialist"))
        out = await _run(
            structured_runtime.create_session(
                body["engine"],
                body["cwd"],
                operation_id=body["operation_id"],
                model=body.get("model"),
                effort=body.get("effort"),
                bypass=body.get("bypass", False),
                **({"specialist": specialist} if specialist is not None else {}),
                # The page opens on launch, not on ready (#1505): the snapshot's `launch` view
                # reports readiness. Only this route and `/start` below pass False.
                wait_ready=False,
            )
        )
        return JSONResponse(out, status_code=201)

    @app.post("/api/structured/sessions/{key}/start")
    async def structured_start(
        key: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # The second phase of a skip-permissions creation (#1339): the SPA calls it only after it
        # has received the create response, so a lost response can never launch unobserved.
        # It answers at launch (#1505); a refusal before any worker exists is recorded in the
        # snapshot's `launch` view too, so a lost answer still has a truthful state to read.
        out = await _run(structured_runtime.start_session(key, wait_ready=False))
        # A handoff's brief bound to this session (#2141) is sent once this launch is ready.
        from .. import handoff_api

        handoff_api.after_start(out.get("session_key") or key)
        return JSONResponse(out)

    async def _presence(key: str) -> dict:
        """Who has this session open (#1931). Reading the operator's own pane counts the
        operator in; `shared` says whether any live link reaches it at all."""
        presence.default().touch(key, presence.OPERATOR)
        out = presence.default().view(key)
        try:
            out["shared"] = bool(await asyncio.to_thread(shares.store().list_active, key))
        except Exception:  # the share store must never take the operator's own view down
            log.warning("structured view: share links unavailable", exc_info=True)
            out["shared"] = False
        return out

    async def _attributed(view: dict, key: str) -> dict:
        # A visitor's turns (#1404 P2) carry their self-chosen name in the operator's view too,
        # and a turn a paired instance caused (#2057) carries that peer's name.
        session_key = view.get("session_key") or key

        def read() -> dict:
            return share_api.with_peer_authors(
                shares.store().authors(session_key),
                _peer_authors(app, session_key),
            )

        try:
            authors = await asyncio.to_thread(read)
        except Exception:  # the share store must never take the operator's own view down…
            log.warning("structured view: visitor attribution unavailable", exc_info=True)
            authors = None  # …but then every turn is UNKNOWN, never labelled the operator's
        if "pending_requests" in view:  # a snapshot, not a history page
            try:
                decisions = await asyncio.to_thread(_decisions, app, session_key)
            except Exception:  # unreadable is not "nobody": the field is left out
                log.warning("structured view: decision attribution unavailable", exc_info=True)
                decisions = []
            if decisions:
                view["decisions"] = decisions
        return share_api.overlay_authors(view, authors)

    @app.get("/api/structured/sessions/{key}")
    async def structured_snapshot(key: str, _user: str = Depends(logged_in)) -> JSONResponse:
        # The operator's own view: a running turn's message in progress shows too (#1727).
        snap = await _run(structured_runtime.snapshot(key, live_text=True))
        view = await _attributed(snap, key)
        view["presence"] = await _presence(view.get("session_key") or key)
        # Who drives it (#2397): HQ, the Commander, or a mission; and which turns HQ sent.
        view["driver"] = await asyncio.to_thread(driver_view, view.get("session_key") or key)
        for turn in view.get("turns") or []:
            if session_driver.sent_by_hq(turn):
                turn["from_hq"] = True
        return JSONResponse(view)

    @app.get("/api/structured/sessions/{key}/history")
    async def structured_history(
        key: str, before: str, limit: int = 50, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        # Older turns get the snapshot's attribution, failing closed the same way (#1504).
        # The operator's own pages keep a stopped turn's streamed words, as its snapshot (#1727).
        page = await _run(
            structured_runtime.history(key, before=before, limit=limit, live_text=True)
        )
        page = await _attributed(page, key)
        for turn in page.get("turns") or []:
            if session_driver.sent_by_hq(turn):
                turn["from_hq"] = True  # #2397
        return JSONResponse(page)

    @app.post("/api/structured/sessions/{key}/skip-permissions")
    async def structured_enable_bypass(
        key: str,
        request: Request,
        user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(request, {"operation_id"}, {"operation_id"})
        return JSONResponse(
            await _run(
                structured_runtime.enable_bypass(key, operation_id=body["operation_id"], user=user)
            )
        )

    @app.get("/api/structured/sessions/{key}/events")
    async def structured_events(
        key: str,
        request: Request,
        after: int = 0,
        limit: int = 100,
        wait: float = 0,
        _user: str = Depends(logged_in),
    ) -> JSONResponse:
        presence.default().touch(key, presence.OPERATOR)  # #1931: the pane is open
        # `wait` (#1727): hold a read with nothing past `after` until the journal moves, at most
        # `EVENTS_WAIT_MAX` (3 s) whatever is asked. Operator route only: the public share route
        # never waits and never carries `data`.
        watch = asyncio.ensure_future(_until_gone(request.receive)) if wait > 0 else None
        try:
            page = await _run(
                structured_runtime.wait_events(
                    key,
                    after=after,
                    limit=limit,
                    wait=wait,
                    disconnected=watch.done if watch is not None else None,
                )
            )
        finally:
            if watch is not None:
                watch.cancel()
        return JSONResponse(page)

    @app.get("/api/structured/sessions/{key}/containment")
    async def structured_probe(key: str, _user: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await _run(structured_runtime.probe(key)))

    @app.post("/api/structured/sessions/{key}/turns")
    async def structured_submit(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(
            request,
            {"operation_id", "text", "expected_revision", "attachments", "reply_to"},
            {"operation_id", "text"},
        )
        canon = _canonical(key)
        drive = _drive_binding(request, "send")
        if drive is not None:
            return await _drive_send(canon, body, drive, request)
        # The Commander typed (#2397): a session HQ drives is theirs from now on.
        await _take_over(canon)
        try:
            out = await structured_runtime.submit_turn(
                key,
                operation_id=body["operation_id"],
                text=body["text"],
                expected_revision=_revision(body.get("expected_revision")),
                # Upload names (#1332 Phase 3); the facade admits each before anything durable.
                attachments=body.get("attachments"),
                reply_to=body.get("reply_to"),
            )
        except structured_runtime.StructuredError as exc:
            if exc.remedy is None and exc.reason != native_ipc.JOURNAL_UNWRITABLE:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            # A known refusal before any claim (#1733): the pane keeps the draft and offers a NEW
            # send, never a Retry of an uncertain one. Or one that names its action (#1725): the
            # pane offers that action, through the route that already exists. Closed values,
            # never free text.
            return JSONResponse(
                {
                    "detail": exc.detail,
                    **({"reason": exc.reason} if exc.reason else {}),
                    **({"remedy": exc.remedy} if exc.remedy else {}),
                },
                status_code=exc.status,
            )
        return JSONResponse(out, status_code=202 if out.get("state") == "running" else 200)

    @app.post("/api/structured/sessions/{key}/decisions")
    async def structured_decide(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"decision_id", "turn_id", "request_id", "decision"}
        body = await _json(request, fields | {"expected_revision", "grant", "answers"}, fields)
        for name in ("turn_id", "request_id", "decision"):
            if not isinstance(body[name], str):
                raise HTTPException(status_code=422, detail=f"{name} must be a string")
        if "grant" in body and not isinstance(body["grant"], str):
            raise HTTPException(status_code=422, detail="grant must be a string")
        # An agent question's answer (#1463): only with `answer`; its shape and the offered
        # options are checked by the runtime and again by the worker.
        if ("answers" in body) != (body["decision"] == "answer"):
            raise HTTPException(
                status_code=422, detail="answers go with an answer decision, and only with one"
            )
        if "answers" in body and not isinstance(body["answers"], list):
            raise HTTPException(status_code=422, detail="answers must be a list")
        out = await _run(
            structured_runtime.decide(
                key,
                turn_id=body["turn_id"],
                request_id=body["request_id"],
                decision_id=body["decision_id"],
                decision=body["decision"],
                user=_user,
                expected_revision=_revision(body.get("expected_revision")),
                grant=body.get("grant"),
                answers=body.get("answers"),
            )
        )
        await _settled(app, out.get("session_key") or key, body["turn_id"], body["request_id"])
        return JSONResponse(out)

    @app.post("/api/structured/sessions/{key}/send-now")
    async def structured_send_now(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"operation_id", "turn_id", "queued_turn_id"}
        body = await _json(request, fields | {"stop_all"}, fields)
        if asgi_call.in_process(request.scope):
            # Not a drive verb (#2397): it interrupts the running turn to deliver a queue, and has
            # no admission seam for HQ's binding.
            raise HTTPException(status_code=403, detail="HQ does not use Send now")
        try:
            result = await structured_runtime.send_now(key, **body)
        except structured_runtime.StructuredError as exc:
            if exc.reason is None:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            # A closed reason the pane acts on (#1644): Wait or Stop all and send, or confirm
            # again. Never free text.
            return JSONResponse(
                {"detail": exc.detail, "reason": exc.reason}, status_code=exc.status
            )
        return JSONResponse(result, status_code=202)

    @app.post("/api/structured/sessions/{key}/withdraw")
    async def structured_withdraw(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        fields = {"operation_id", "queued_turn_id"}
        body = await _json(request, fields, fields)
        drive = _drive_binding(request, "withdraw")
        if drive is not None:
            return await _drive_withdraw(_canonical(key), body, drive)
        return JSONResponse(await _run(structured_runtime.withdraw(key, **body)), status_code=202)

    @app.post("/api/structured/sessions/{key}/interrupt")
    async def structured_interrupt(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        body = await _json(request, {"operation_id", "turn_id"}, {"operation_id", "turn_id"})
        drive = _drive_binding(request, "interrupt")
        if drive is not None:
            return await _drive_interrupt(_canonical(key), body, drive, request)
        return JSONResponse(
            await _run(
                structured_runtime.interrupt(
                    key, operation_id=body["operation_id"], turn_id=body["turn_id"]
                )
            ),
            status_code=202,
        )

    @app.post("/api/structured/sessions/{key}/stop")
    async def structured_stop(
        key: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # The operator's Stop (#1724) names the background work it was confirmed for: the
        # generation and digest of `native.stop_inventory`. An empty body (an older page)
        # confirms that nothing runs. Either way the worker rechecks before anything stops.
        # An exact Stop also ends anything those jobs start while it runs (the dialog says so; the
        # settlement lists such processes as started during the stop).
        # Or `{worker, everything: true}`: Stop everything of exactly that generation, the
        # explicitly disclosed second kind of confirmation (#1724).
        body = (
            await _json(request, {"worker", "digest", "everything"}, set())
            if await request.body()
            else {}
        )
        confirmed = (
            set(body) == {"worker", "digest"}
            and isinstance(body["digest"], str)
            and _DIGEST.fullmatch(body["digest"])
        ) or (set(body) == {"worker", "everything"} and body["everything"] is True)
        if body and not (
            confirmed and isinstance(body["worker"], str) and _UUID.fullmatch(body["worker"])
        ):
            raise HTTPException(
                status_code=422,
                detail="a Stop confirms a worker and a digest, or everything of a worker",
            )
        try:
            result = await structured_runtime.operator_stop(
                key,
                worker=body.get("worker"),
                digest=body.get("digest"),
                everything=body.get("everything") is True,
            )
        except structured_runtime.StructuredError as exc:
            if exc.reason is None:
                raise HTTPException(status_code=exc.status, detail=exc.detail) from None
            # Refused before anything stopped (#1724): the work running now, to confirm again.
            return JSONResponse(
                {"detail": exc.detail, "reason": exc.reason, "inventory": exc.inventory},
                status_code=exc.status,
            )
        return JSONResponse(result)
