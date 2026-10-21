"""Authenticated shared variable/collection CRUD (#1390), without delivery or execution.

Every mutation runs off the event loop under the store's authority fence. Secret inputs
are write-only; public rows and conflicts use the store's masked shapes. Every response,
including dependency failures and authentication refusals, is marked no-store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .. import secretfence, session_input, template_secrets, variables
from .. import template_vars as store
from .templates import _conflict, _err, _expected, _json, _json_object
from .upload import NO_STORE


async def _run(fn: Callable, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except store.VariableConflict as exc:
        return _conflict(exc, "variable or collection")
    except variables.LibraryNotFound:
        raise _err(404, "unknown variable or collection") from None
    except store.VariableInUse as exc:
        return JSONResponse(
            {"detail": str(exc), "dependants": exc.dependants, "projects": exc.projects},
            status_code=409,
            headers=NO_STORE,
        )
    except (store.ResolutionUnavailable, store.VariableRefsUnknown) as exc:
        raise _err(503, str(exc)) from None
    except (store.VariableStoreUnsupported, template_secrets.SecretKeyUnavailable) as exc:
        raise _err(409, str(exc)) from None
    except store.VariableError as exc:
        raise _err(422, str(exc)) from None


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.middleware("http")
    async def _variables_are_never_cached(request: Request, call_next):
        path = request.url.path
        if not any(
            path == root or path.startswith(root + "/")
            for root in ("/api/variables", "/api/variable-collections")
        ):
            return await call_next(request)
        try:
            response = await call_next(request)
        except session_input.AuthorityFenceBusy:
            return _json_error("busy sending into a session — nothing was saved; try again", 503)
        except secretfence.SecretWriteBusy:
            # A memory import holds the secret-inventory fence (#1501): refused, nothing written.
            return _json_error(secretfence.SECRET_WRITE_BUSY, 503)
        except Exception:
            return _json_error("the variables store failed", 500)
        response.headers.update(NO_STORE)
        return response

    @app.get("/api/variables")
    async def get_library(_user: str = Depends(logged_in)):
        return _json(await _run(variables.library))

    @app.post("/api/variables/migrate")
    async def migrate(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        if await _json_object(request):
            raise _err(422, "migration accepts an empty object")
        result = await _run(variables.migrate)
        return result if isinstance(result, Response) else _json(result)

    @app.post("/api/variables", status_code=201)
    async def create(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        result = await _run(variables.create, await _json_object(request))
        return result if isinstance(result, Response) else _json(result, 201)

    @app.patch("/api/variables/{vid}")
    async def update(
        vid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        expected = _expected(payload.pop("expected_updated_at", None))
        result = await _run(variables.update, vid, payload, expected)
        return result if isinstance(result, Response) else _json(result)

    @app.delete("/api/variables/{vid}", status_code=204)
    async def delete(
        vid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        result = await _run(
            variables.delete, vid, _expected(request.query_params.get("expected_updated_at"))
        )
        return (
            result if isinstance(result, Response) else Response(status_code=204, headers=NO_STORE)
        )

    @app.post("/api/variable-collections", status_code=201)
    async def create_collection(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ):
        result = await _run(variables.save_collection, await _json_object(request))
        return result if isinstance(result, Response) else _json(result, 201)

    @app.patch("/api/variable-collections/{cid}")
    async def update_collection(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        expected = _expected(payload.pop("expected_updated_at", None))
        result = await _run(variables.save_collection, payload, cid, expected)
        return result if isinstance(result, Response) else _json(result)

    @app.delete("/api/variable-collections/{cid}", status_code=204)
    async def delete_collection(
        cid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        result = await _run(
            variables.delete_collection,
            cid,
            _expected(request.query_params.get("expected_updated_at")),
        )
        return (
            result if isinstance(result, Response) else Response(status_code=204, headers=NO_STORE)
        )


def _json_error(detail: str, code: int) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=code, headers=NO_STORE)
