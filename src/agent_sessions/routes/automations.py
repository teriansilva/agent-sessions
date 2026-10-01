"""Automation routes (#1201 Phase 1) — `/api/automations`.

* ``GET    /api/automations``                   — every automation (public shape) + loop status
* ``POST   /api/automations``                   — create (always OFF)
* ``GET    /api/automations/origins``           — ``{key: {automation_id, name, …}}`` for badges
* ``GET    /api/automations/runs/{run_id}``     — one run with its step timeline
* ``GET    /api/automations/{id}``              — one automation
* ``PATCH  /api/automations/{id}``              — edit, fenced by ``revision``; widening an
  approved automation needs ``consent: true`` + the ``scope_digest`` it was shown, else 422 with
  ``widened`` and nothing written
* ``DELETE /api/automations/{id}?revision=N``
* ``POST   /api/automations/{id}/enable``       — ``{revision, consent: true, scope_digest}``
* ``POST   /api/automations/{id}/disable|pause|resume``
* ``POST   /api/automations/{id}/run``          — Run now
* ``GET    /api/automations/{id}/runs``         — history, newest first, ``?limit&offset``

Reads need a session; every write needs the session AND ``csrf_guard`` (which carries the
Origin/Referer check). Every response is ``no-store``: a run record carries instructions. A client
can never write a consent receipt, a pin or a run result — those fields are not in any body this
module accepts, and an unknown field is a 422.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .. import automation_effect_lock as effect_lock
from .. import automation_loop, automation_runner
from .. import automations as model
from .. import automations_store as store

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PREFIX = "/api/automations"
_CONFIG_KEYS = {"name", "trigger", "action", "policy"}
_WORST = ("failed", "skipped", "pending", "ok")
#: Largest request body these routes read. A config is a few KiB; anything past this is refused.
BODY_MAX = 64 * 1024


def _json(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=NO_STORE)


def _err(e: Exception) -> JSONResponse:
    if isinstance(e, model.AutomationError):
        return _json({"detail": str(e), **e.extra}, e.status)
    if isinstance(e, store.StoreError):
        return _json({"detail": str(e)}, e.status)
    if isinstance(e, automation_runner.RunRefused):
        return _json({"detail": e.detail}, e.status)
    if isinstance(e, model.PinsUnavailable):
        return _json({"detail": f"could not check the automation's inputs right now: {e}"}, 503)
    raise e


async def _body(request: Request) -> dict:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_MAX:
        raise model.AutomationError("request body is too large", status=413)
    # READ INCREMENTALLY: without a Content-Length (chunked), `request.body()` would buffer all of
    # it before any size check. Stop at the first chunk past the cap.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > BODY_MAX:
            raise model.AutomationError("request body is too large", status=413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # RecursionError: a deeply nested document is malformed input, not a server fault.
        raise model.AutomationError("request body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise model.AutomationError("request body must be a JSON object")
    return payload


def _revision(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise model.AutomationError("revision is required and is the number you loaded")
    return raw


def _count(raw: str | None, default: int, hi: int) -> int:
    """A non-negative ASCII integer query value (``"²".isdigit()`` is True), bounded."""
    if raw is None or raw == "":
        return default
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        raise model.AutomationError("expected a whole number")
    return min(int(raw), hi)


def _in_flight(got: bool) -> dict:
    """The honest contract: an acknowledged change WITHOUT ``in_flight`` means no later effect."""
    return {"in_flight": not got, "in_flight_detail": "" if got else effect_lock.IN_FLIGHT_DETAIL}


def scope_lines(scope: dict, config: dict | None) -> list[str]:
    """The consent lines for ``scope``, with the template's rendered preview from ``config``."""
    return model.describe(scope, model.message_preview(config))


def scope_digest(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:32]


def _consent(body: dict, scope: dict, receipt: dict | None, config: dict | None = None) -> bool:
    consent = body.get("consent", False)
    if not isinstance(consent, bool):
        raise model.AutomationError("consent must be true or false")
    if consent and body.get("scope_digest") != scope_digest(scope):
        raise model.AutomationError(
            "what this automation would do changed since you read it — review it again",
            status=409,
            scope=scope,
            scope_lines=scope_lines(scope, config),
            scope_digest=scope_digest(scope),
            # What widens NOW against the approved receipt, so the dialog can say it.
            widened=model.widened(receipt, scope),
        )
    return consent


# ---- the public shape ---------------------------------------------------------------------------


def _state(row: dict, now: float, next_run) -> str:
    config = row["config"]
    if config is None:
        return "unreadable"
    if not row["enabled"]:
        return "off"
    if row["needs_reapproval"]:
        return "needs_reapproval"
    expires = config["policy"]["expires_at"]
    if expires is not None and now >= expires:
        return "expired"
    if config["trigger"]["kind"] == "once" and next_run is None:
        return "finished"
    if row["paused"]:
        return "paused"
    if row["consecutive_failures"]:
        return "erroring"
    return "enabled"


def public(row: dict, *, now: float | None = None) -> dict:
    """What a client may read. BLOCKING (reads the store and templates)."""
    ts = time.time() if now is None else now
    config = row["config"]
    tz = model.trigger_tz(config) if config else None
    next_run = None
    scope = None
    if config is not None:
        mark = store.watermark(row["id"])
        # The next UNCLAIMED slot: one in the past is due now (the next tick fires it).
        base = max(float(row["active_since"] or 0), float(mark["fire_at"]) if mark else 0.0)
        nxt = model.next_slot(config["trigger"], base) if row["enabled"] else None
        next_run = {"slot": nxt[0], "at": nxt[1]} if nxt else None
        try:
            scope = model.scope_of(config, model.compute_pins(config))
        except model.PinsUnavailable:
            scope = None  # shown as "could not check"; a read never fails over it
    runs = store.runs_since(row["id"], ts - 14 * 86400)
    counts = {"ok": 0, "failed": 0, "skipped": 0, "pending": 0}
    days: dict[str, str] = {}
    for r in runs:
        cls = r["result_class"]
        counts[cls] = counts.get(cls, 0) + 1
        d = model.local_day(r["created_at"], tz)
        prev = days.get(d)
        if prev is None or _WORST.index(cls) < _WORST.index(prev):
            days[d] = cls
    strip = [
        {"date": (d := model.local_day(ts - i * 86400, tz)), "worst": days.get(d)}
        for i in range(13, -1, -1)
    ]
    decided = counts["ok"] + counts["failed"]
    last = store.last_run(row["id"])
    state = _state(row, ts, next_run)
    return {
        "id": row["id"],
        "name": row["name"],
        "trigger": config["trigger"] if config else None,
        "action": config["action"] if config else None,
        "policy": config["policy"] if config else None,
        "revision": row["revision"],
        "state": state,
        "enabled": row["enabled"],
        "paused": row["paused"],
        "paused_reason": row["paused_reason"],
        "needs_reapproval": row["needs_reapproval"],
        "reapproval_reason": row["reapproval_reason"],
        "consented_at": row["consented_at"],
        "consented_scope": row["consented_scope"],
        # What enabling (or a widening save) would approve NOW — the consent dialog's content.
        "scope": scope,
        "scope_lines": scope_lines(scope, config) if scope else [],
        "scope_digest": scope_digest(scope) if scope else None,
        "pins": row["pins"],
        "consecutive_failures": row["consecutive_failures"],
        "check_note": row.get("check_note", ""),
        "next_run": next_run if state in ("enabled", "erroring") else None,
        "last_run": _run_summary(last) if last else None,
        "stats": {
            **counts,
            "runs": len(runs),
            "success_rate": (counts["ok"] / decided) if decided else None,
        },
        "strip": strip,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_summary(r: dict) -> dict:
    return {
        k: r[k]
        for k in (
            "id",
            "trigger",
            "slot",
            "catch_up",
            "covered",
            "state",
            "outcome",
            "result_class",
            "reason",
            "mission_id",
            "session_key",
            "created_at",
            "finished_at",
        )
    }


def public_run(r: dict) -> dict:
    """A run as a client may read it. ``inputs`` is already masked; the recorded scope is reduced
    to the consented scope (the config it ran under is the same data the automation shows)."""
    out = _run_summary(r)
    out["automation_id"] = r["automation_id"]
    out["fire_at"] = r["fire_at"]
    out["inputs"] = r["inputs"]
    out["scope"] = (r["scope"] or {}).get("consented_scope")
    if "steps" in r:
        out["steps"] = r["steps"]
    return out


# ---- operations (blocking; each runs off the event loop) ----------------------------------------


def _create(body: dict) -> dict:
    now = time.time()
    config = model.validate_config(body, now=now)
    automation_runner.check_save(config)
    row = store.create(config, model.compute_pins(config), now=now)
    return public(row)


def _merged(row: dict, body: dict) -> dict:
    if row["config"] is None:
        raise model.AutomationError(
            "this automation's stored settings can't be read; delete it and create it again",
            status=409,
        )
    raw = {k: body.get(k, row["config"][k]) for k in _CONFIG_KEYS}
    config = model.validate_config(raw, now=time.time() if "trigger" in body else None)
    automation_runner.check_save(config)
    return config


def _patch(aid: str, body: dict) -> dict:
    unknown = sorted(set(body) - _CONFIG_KEYS - {"revision", "consent", "scope_digest"})
    if unknown:
        raise model.AutomationError(f"a change does not take {', '.join(unknown)}")
    rev = _revision(body.get("revision"))
    current = store.get(aid)
    config = _merged(current, body)
    pins = model.compute_pins(config)
    new_scope = model.scope_of(config, pins)

    def fn(row: dict) -> dict:
        updates = {"name": config["name"], "config": config, "pins": pins}
        if not row["enabled"]:
            return updates  # off: nothing runs; enabling asks for consent anyway
        if row["config"] is not None and row["config"]["trigger"] != config["trigger"]:
            # A NEW SCHEDULE COUNTS FROM NOW, exactly like enable and resume: a slot of the new
            # schedule that fell before this edit was never consented and must not fire as a
            # "catch-up" on the next tick.
            updates["active_since"] = time.time()
        widened = model.widened(row["consented_scope"], new_scope)
        if widened and not _consent(body, new_scope, row["consented_scope"], config):
            raise model.AutomationError(
                "this change widens what the automation may do; confirm it again",
                widened=widened,
                scope=new_scope,
                scope_lines=scope_lines(new_scope, config),
                scope_digest=scope_digest(new_scope),
            )
        # The receipt FOLLOWS the current scope — narrowing too — so a later widening of a narrowed
        # configuration needs consent even if an older receipt covered it (#1042).
        updates["consented_scope"] = new_scope
        if widened:
            updates["consented_at"] = time.time()
            updates["needs_reapproval"] = 0
            updates["reapproval_reason"] = ""
        return updates

    with effect_lock.writer(aid) as got:
        row = store.mutate(aid, fn, expect_revision=rev)
    return {**public(row), **_in_flight(got)}


def _enable(aid: str, body: dict) -> dict:
    unknown = sorted(set(body) - {"revision", "consent", "scope_digest"})
    if unknown:
        raise model.AutomationError(f"enable does not take {', '.join(unknown)}")
    rev = _revision(body.get("revision"))
    row = store.get(aid)
    if row["config"] is None:
        raise model.AutomationError("this automation's stored settings can't be read", status=409)
    config = row["config"]
    # Re-validated against the world NOW: a folder that left scope or an engine that lost its
    # unattended start since the save is refused here, not discovered at 03:00.
    model.validate_trigger(config["trigger"], now=time.time())
    automation_runner.check_save(config)
    pins = model.compute_pins(config)
    scope = model.scope_of(config, pins)
    if body.get("consent") is not True:
        raise model.AutomationError(
            "enabling needs your consent to its full scope",
            scope=scope,
            scope_lines=scope_lines(scope, config),
            scope_digest=scope_digest(scope),
        )
    _consent(body, scope, row["consented_scope"], config)
    now = time.time()
    with effect_lock.writer(aid) as got:
        row = store.mutate(
            aid,
            lambda _r: {
                "enabled": 1,
                "paused": 0,
                "paused_reason": "",
                "needs_reapproval": 0,
                "reapproval_reason": "",
                "consented_at": now,
                "consented_scope": scope,
                "pins": pins,
                "active_since": now,
                "consecutive_failures": 0,
            },
            expect_revision=rev,
        )
    return {**public(row), **_in_flight(got)}


SIMPLE_VERBS = ("disable", "pause", "resume")


def _simple(aid: str, verb: str) -> dict:
    if verb not in SIMPLE_VERBS:
        raise ValueError(f"unknown automation verb {verb!r}")  # a bug, never a default
    now = time.time()

    def fn(row: dict) -> dict | None:
        if verb == "disable":
            return {"enabled": 0, "paused": 0, "paused_reason": ""} if row["enabled"] else None
        if not row["enabled"]:
            raise model.AutomationError("this automation is off; enable it first", status=409)
        if verb == "pause":
            return None if row["paused"] else {"paused": 1, "paused_reason": "paused by you"}
        if verb != "resume":
            raise ValueError(f"unknown automation verb {verb!r}")
        if row["needs_reapproval"]:
            raise model.AutomationError(
                "this automation needs your approval again — enable it with consent",
                status=409,
            )
        # Resuming never replays what the pause skipped: slots count from now.
        return {"paused": 0, "paused_reason": "", "consecutive_failures": 0, "active_since": now}

    store.get(aid)  # exists (and well-formed) BEFORE any lock file is created for it
    with effect_lock.writer(aid) as got:
        row = store.mutate(aid, fn)
    return {**public(row), **_in_flight(got)}


def _delete(aid: str, rev: int) -> dict:
    row = store.get(aid)
    with effect_lock.writer(aid) as got:
        store.delete(aid, expect_revision=rev)
    automation_runner.retract(row["failure_episode"])
    return _in_flight(got)


def _unattended_engines() -> list[dict]:
    """Every engine in the roster with whether it may be started UNATTENDED right now, and why not.

    The editor offers only the ``ok`` ones and shows the reason beside the rest; the same
    ``engine_state`` refuses the save and the run, so the offer is never wider than the check."""
    out = []
    for eid in automation_runner.engines.engine_ids():
        try:
            ok, why = automation_runner.engine_state(eid)
        except Exception:  # noqa: BLE001 — an engine that cannot answer is not offered
            ok, why = False, "this agent could not be checked"
        out.append({"id": eid, "ok": ok, "reason": "" if ok else why})
    return out


def _list() -> dict:
    sched = automation_loop.SCHEDULER
    return {
        "automations": [public(r) for r in store.list_all()],
        "engines": _unattended_engines(),
        "loop": {
            "enabled": automation_runner.loop_enabled(),
            "owner": bool(sched and sched.owner),
        },
        "limits": {
            "interval_min_minutes": model.INTERVAL_MIN_MINUTES,
            "max_runs_per_day_max": model.MAX_RUNS_PER_DAY_MAX,
            "max_concurrent_max": model.MAX_CONCURRENT_MAX,
            "pause_after_failures_max": model.PAUSE_AFTER_FAILURES_MAX,
            "triggers": list(model.TRIGGER_KINDS),
            "actions": list(model.ACTION_KINDS),
            "autonomy": list(model.AUTONOMY),
            "timezone": model.host_timezone(),
        },
    }


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    @app.middleware("http")
    async def _automations_are_never_cached(request: Request, call_next):
        path = request.url.path
        if not (path == PREFIX or path.startswith(PREFIX + "/")):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            return _json({"detail": "the automations store failed"}, 500)
        response.headers.update(NO_STORE)
        return response

    async def _run(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except (model.AutomationError, store.StoreError, model.PinsUnavailable) as e:
            return _err(e)

    @app.get(PREFIX)
    async def list_automations(_user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(_list)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.post(PREFIX)
    async def create_automation(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            body = await _body(request)
        except model.AutomationError as e:
            return _err(e)
        out = await _run(_create, body)
        return out if isinstance(out, JSONResponse) else _json(out, 201)

    @app.get(PREFIX + "/origins")
    async def automation_origins(_user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(store.origins)
        return out if isinstance(out, JSONResponse) else _json({"origins": out})

    @app.get(PREFIX + "/runs/{run_id}")
    async def automation_run(run_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(store.get_run, run_id)
        return out if isinstance(out, JSONResponse) else _json(public_run(out))

    @app.get(PREFIX + "/{aid}")
    async def get_automation(aid: str, _user: str = Depends(logged_in)) -> JSONResponse:
        out = await _run(lambda: public(store.get(aid)))
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.patch(PREFIX + "/{aid}")
    async def patch_automation(
        aid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
        except model.AutomationError as e:
            return _err(e)
        out = await _run(_patch, aid, body)
        return out if isinstance(out, JSONResponse) else _json(out)

    @app.delete(PREFIX + "/{aid}")
    async def delete_automation(
        aid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        raw = request.query_params.get("revision")
        if raw is None or not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return _err(model.AutomationError("revision is required"))
        out = await _run(_delete, aid, int(raw))
        return out if isinstance(out, JSONResponse) else _json({"deleted": aid, **out})

    @app.post(PREFIX + "/{aid}/enable")
    async def enable_automation(
        aid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await _body(request)
        except model.AutomationError as e:
            return _err(e)
        out = await _run(_enable, aid, body)
        return out if isinstance(out, JSONResponse) else _json(out)

    def _verb_route(verb: str):
        # The verb is bound in THIS closure, never as a handler parameter: FastAPI exposes every
        # parameter with a default as request input, so `?verb=resume` must have nothing to bind.
        async def _handler(
            aid: str,
            _user: str = Depends(logged_in),
            _csrf: None = Depends(csrf_guard),
        ) -> JSONResponse:
            out = await _run(_simple, aid, verb)
            return out if isinstance(out, JSONResponse) else _json(out)

        return _handler

    for verb in SIMPLE_VERBS:
        app.post(PREFIX + "/{aid}/" + verb, name=f"automation_{verb}")(_verb_route(verb))

    @app.post(PREFIX + "/{aid}/run")
    async def run_automation_now(
        aid: str, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        try:
            run = await automation_runner.run_now(aid, registry=registry)
        except (automation_runner.RunRefused, store.StoreError) as e:
            return _err(e)
        return _json(public_run(run), 202)

    @app.get(PREFIX + "/{aid}/runs")
    async def automation_runs(
        aid: str, request: Request, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        qp = request.query_params

        def _page():
            store.get(aid)  # 404 for an unknown automation, not an empty page
            limit = _count(qp.get("limit"), 50, 200)
            offset = _count(qp.get("offset"), 0, 10**9)
            out = store.list_runs(aid, limit=limit, offset=offset)
            return {"runs": [public_run(r) for r in out["runs"]], "total": out["total"]}

        out = await _run(_page)
        return out if isinstance(out, JSONResponse) else _json(out)
