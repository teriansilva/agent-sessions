"""Authenticated agent catalog/review/install/sign-in/verify/activation APIs (#1259).

All mutations require the existing CSRF + origin guard. Requests select fixed operations, never
commands, probe results, download URLs or signing keys. The separate sign-in socket has no
session/capture hooks and checks cookie, origin and forced-password state before accepting.
"""

from __future__ import annotations

import asyncio
import functools
import json

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from .. import __version__, agent_catalog, agent_catalog_refresh, chat_config, prefs
from ..auth import origin_matches, session_uid
from ..engines import registry
from ..plugins import feed, jobs, manager, provenance, signin, storage
from .chat import test_endpoint_draft

MAX_BODY = feed.MAX_BYTES


async def _body(request: Request, *, required: set[str], optional: set[str] = frozenset()) -> dict:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY:
            raise HTTPException(413, "plugin request is too large")
    try:
        value = json.loads(data, object_pairs_hook=feed._pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "invalid plugin request") from None
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
    ):
        raise HTTPException(422, "unexpected or missing plugin request fields")
    return value


async def _call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
    except (manager.ManagerError, feed.FeedError, storage.StateError) as exc:
        raise HTTPException(409, str(exc)) from None
    except (chat_config.ChatConfigError, prefs.KeyOriginError) as exc:
        raise HTTPException(422, str(exc)) from None
    except provenance.ProvenanceError:
        raise HTTPException(422, "plugin provenance validation refused this request") from None
    except (ValueError, OSError, KeyError, TypeError):
        raise HTTPException(409, "plugin state is unavailable; no operation was inferred") from None


async def _job(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except (manager.ManagerError, feed.FeedError, storage.StateError) as exc:
        raise HTTPException(409, str(exc)) from None
    except provenance.ProvenanceError:
        raise HTTPException(422, "plugin provenance validation refused this request") from None
    except (ValueError, OSError, KeyError, TypeError):
        raise HTTPException(409, "plugin operation is unavailable") from None


def _operation(item: dict) -> dict:
    return {
        **{
            key: item[key]
            for key in ("id", "plugin_id", "kind", "state", "created_at", "updated_at", "error")
        },
        "generation_id": item["id"]
        if item["kind"] == "install"
        else item["request"].get("generation_id"),
        # A reload during staging still needs the server's exact reviewed candidate. This is
        # the same closed manifest/recipe view already returned by review(), never a credential.
        "review": item.get("review"),
        "review_digest": item.get("review_digest") or (item.get("review") or {}).get("digest"),
    }


def _generation(gen: dict) -> dict:
    reviewed = gen["review"]
    return {
        "id": gen["id"],
        "review": reviewed,
        "verification": gen["verification"],
        "required_checks": manager.required_checks(manager.provider(reviewed["plugin_id"], gen)),
    }


def _list() -> dict:
    catalog = agent_catalog.current()
    feed_view = {
        "state": "unavailable" if catalog.error else "ready",
        "sequence": catalog.sequence,
        "expires_at": catalog.expires_at,
        "digest": catalog.digest,
        "bundled_digest": catalog.bundled_digest,
        "source": catalog.source,
        "release_version": __version__,
        "stale": catalog.stale,
        "error": catalog.error,
        "definitions_url": agent_catalog.SOURCE_URL,
        "history_url": agent_catalog.HISTORY_URL,
        "updates_url": "https://github.com/teriansilva/agent-sessions/releases",
    }
    try:
        feed_view["refresh"] = agent_catalog_refresh.status()
    except (ValueError, OSError):
        feed_view["refresh"] = None
        feed_view["error"] = (
            "Catalog refresh settings are unavailable; restore them to check for updates."
        )
    entries = [
        {
            "manifest": feed.decode(c.entry.manifest_bytes),
            "digest": c.entry.digest,
            "source": c.source,
            "installable": c.source is not None,
            "reason": c.reason,
            "included": c.included,
        }
        for c in catalog.choices
    ]
    doc = manager.snapshot()
    rows = []
    for plugin_id, row in doc["plugins"].items():
        try:
            manager._validate_row(plugin_id, row)
            rows.append(
                {
                    "id": plugin_id,
                    **{k: row[k] for k in ("active", "candidate", "enabled")},
                    "generations": [_generation(g) for g in row["generations"].values()],
                }
            )
        except (ValueError, OSError, KeyError, TypeError):
            rows.append(
                {"id": plugin_id, "error": "This agent's installation state is unavailable."}
            )
    return {
        "feed": feed_view,
        "catalog": entries,
        "plugins": rows,
        "operations": [_operation(o) for o in doc["operations"].values()],
        "roster_generation": registry.current().generation,
        "roster_revision": doc.get("roster_revision"),
    }


def _chat_candidate(plugin_id: str, generation_id: str) -> None:
    gen = manager.generation(plugin_id, generation_id)
    if manager.provider(plugin_id, gen).manifest.runtime != "chat":
        raise manager.ManagerError("this candidate has no API endpoint")


def register(app: FastAPI, *, logged_in, csrf_guard, cfg, must_change) -> None:
    service = jobs.Service()
    app.state.plugin_jobs = service
    read = [Depends(logged_in)]
    write = [Depends(logged_in), Depends(csrf_guard)]

    @app.get("/api/agents/catalog", dependencies=read)
    @app.get("/api/plugins", dependencies=read)
    async def plugin_list():
        return await _call(_list)

    @app.post("/api/agents/catalog/refresh", dependencies=write)
    @app.post("/api/plugins/feed/refresh", dependencies=write)
    async def plugin_refresh(request: Request):
        await _body(request, required=set())
        await _job(agent_catalog_refresh.refresh)
        return await _call(_list)

    @app.patch("/api/agents/catalog/preferences", dependencies=write)
    async def catalog_preferences(request: Request):
        body = await _body(request, required={"automatic"})
        if type(body["automatic"]) is not bool:
            raise HTTPException(422, "automatic must be true or false")
        await _call(agent_catalog_refresh.configure, body["automatic"])
        return await _call(_list)

    @app.post("/api/plugins/review", dependencies=write)
    async def plugin_review(request: Request):
        body = await _body(request, required=set(), optional={"plugin_id", "local", "adopted_path"})
        return await _call(manager.review, **body)

    @app.post("/api/plugins/install", dependencies=write)
    async def plugin_install(request: Request):
        body = await _body(
            request,
            required={"request_id", "review_id", "digest"},
            optional={"confirm_local", "confirm_adopted"},
        )
        return JSONResponse(_operation(await _job(service.install, **body)), status_code=202)

    @app.get("/api/plugins/operations/{operation_id}", dependencies=read)
    async def plugin_operation(operation_id: str):
        return _operation(await _call(manager.operation, operation_id))

    @app.post("/api/plugins/operations/{operation_id}/cancel", dependencies=write)
    async def plugin_cancel(operation_id: str, request: Request):
        await _body(request, required=set())
        return _operation(await _call(manager.cancel_ready, operation_id))

    @app.post("/api/plugins/recover", dependencies=write)
    async def plugin_recover(request: Request):
        await _body(request, required=set())
        await _call(manager.recover)
        return await _call(_list)

    @app.post("/api/plugins/signin", dependencies=write)
    async def plugin_signin(request: Request):
        body = await _body(request, required={"request_id", "plugin_id", "generation_id"})
        return _operation(await _job(service.signin, **body))

    @app.post("/api/plugins/verify", dependencies=write)
    async def plugin_verify(request: Request):
        body = await _body(
            request, required={"request_id", "plugin_id", "generation_id", "confirm_effects"}
        )
        return JSONResponse(_operation(await _job(service.verify, **body)), status_code=202)

    @app.post("/api/plugins/activate", dependencies=write)
    async def plugin_activate(request: Request):
        body = await _body(request, required={"request_id", "plugin_id", "generation_id"})
        return _operation(await _call(manager.activate, **body))

    @app.post("/api/plugins/disable", dependencies=write)
    async def plugin_disable(request: Request):
        body = await _body(
            request, required={"request_id", "plugin_id", "expected_active", "expected_revision"}
        )
        return _operation(await _call(manager.deactivate, **body))

    @app.post("/api/plugins/remove", dependencies=write)
    async def plugin_remove(request: Request):
        body = await _body(
            request, required={"request_id", "plugin_id", "expected_active", "expected_revision"}
        )
        return _operation(await _call(manager.deactivate, **body, remove=True))

    @app.post("/api/plugins/reload", dependencies=write)
    async def plugin_reload(request: Request):
        await _body(request, required=set())
        await _call(registry.request_reload)
        return {"generation": registry.capture().generation}

    @app.get("/api/plugins/{plugin_id}/generations/{generation_id}/endpoint", dependencies=read)
    async def candidate_endpoint(plugin_id: str, generation_id: str):
        await _call(_chat_candidate, plugin_id, generation_id)
        return chat_config.public(manager.endpoint_scope(plugin_id, generation_id))

    @app.patch("/api/plugins/{plugin_id}/generations/{generation_id}/endpoint", dependencies=write)
    async def candidate_endpoint_set(plugin_id: str, generation_id: str, request: Request):
        await _call(_chat_candidate, plugin_id, generation_id)
        body = await _body(request, required=set(), optional=set(chat_config._FIELDS))
        try:
            return await _call(manager.set_endpoint, plugin_id, generation_id, body)
        except (chat_config.ChatConfigError, prefs.KeyOriginError) as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post(
        "/api/plugins/{plugin_id}/generations/{generation_id}/endpoint/test", dependencies=write
    )
    async def candidate_endpoint_test(plugin_id: str, generation_id: str, request: Request):
        await _call(_chat_candidate, plugin_id, generation_id)
        body = await _body(request, required={"base_url"}, optional={"api_key"})
        return await test_endpoint_draft(manager.endpoint_scope(plugin_id, generation_id), body)

    @app.websocket("/ws/plugins/signin/{operation_id}")
    async def plugin_signin_socket(ws: WebSocket, operation_id: str):
        if session_uid(cfg, ws) != cfg.username or not origin_matches(cfg, ws) or must_change["v"]:
            await ws.close(code=4403)
            return
        await ws.accept()
        await signin.serve(ws, operation_id)
