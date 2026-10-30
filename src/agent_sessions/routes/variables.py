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

try:  # the package renamed; the old name still works but emits a PendingDeprecationWarning
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover - older pins
    from multipart.multipart import MultipartParser, parse_options_header

from .. import secretfence, session_input, template_secrets, variables, vault_files
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
    except template_secrets.RedactionUnavailable:
        raise _err(503, "the secret inventory could not be read; nothing was saved") from None
    except vault_files.SecretFileUnavailable as exc:
        raise _err(503, str(exc)) from None
    except (store.VariableStoreUnsupported, template_secrets.SecretKeyUnavailable) as exc:
        raise _err(409, str(exc)) from None
    except store.VariableError as exc:
        raise _err(422, str(exc)) from None


#: The whole upload request: the file plus multipart framing. Enforced on every chunk as it
#: arrives, never on the client's declared length alone; nothing is spooled.
SECRET_FILE_REQUEST_MAX = vault_files.MAX_BYTES + 16 * 1024


async def _secret_file_upload(request: Request) -> tuple[str, bytes]:
    """The one ``file`` part of a multipart upload: its file name and its bytes, read with a hard
    ceiling (#1786). The bytes are only ever encrypted — never parsed, logged or echoed."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or not params.get(b"boundary"):
        raise _err(415, "upload the secret file as multipart/form-data")
    too_big = f"a secret file is at most {vault_files.MAX_BYTES // 1024} KiB"
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > SECRET_FILE_REQUEST_MAX:
        raise _err(413, too_big)
    parts: list[dict] = []
    header: dict = {"field": b"", "value": b""}
    ended: list[bool] = []

    def on_part_begin() -> None:
        parts.append({"headers": {}, "data": bytearray()})

    def on_header_field(data: bytes, start: int, end: int) -> None:
        header["field"] += data[start:end]

    def on_header_value(data: bytes, start: int, end: int) -> None:
        header["value"] += data[start:end]

    def on_header_end() -> None:
        parts[-1]["headers"][header["field"].lower()] = header["value"]
        header["field"], header["value"] = b"", b""

    def on_part_data(data: bytes, start: int, end: int) -> None:
        parts[-1]["data"] += data[start:end]

    def on_end() -> None:
        ended.append(True)

    parser = MultipartParser(
        params[b"boundary"],
        {
            "on_part_begin": on_part_begin,
            "on_header_field": on_header_field,
            "on_header_value": on_header_value,
            "on_header_end": on_header_end,
            "on_part_data": on_part_data,
            "on_end": on_end,
        },
    )
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > SECRET_FILE_REQUEST_MAX:
            raise _err(413, too_big)
        try:
            parser.write(chunk)
        except Exception:  # noqa: BLE001 — a malformed body is the client's, never a crash
            raise _err(422, "the upload is not a valid multipart body") from None
    try:
        parser.finalize()
    except Exception:  # noqa: BLE001
        raise _err(422, "the upload is not a valid multipart body") from None
    # Only `on_end` proves the parse reached the CLOSING boundary: a transfer cut mid-body would
    # otherwise be stored as a complete, silently truncated file (Hermes 7394, 3; #1720 round 9).
    if not ended:
        raise _err(422, "the upload ended early — the file was not fully sent")
    files = []
    for part in parts:
        disposition, dparams = parse_options_header(
            part["headers"].get(b"content-disposition", b"")
        )
        if disposition == b"form-data" and dparams.get(b"name") == b"file":
            files.append((dparams.get(b"filename"), bytes(part["data"])))
    if len(files) != 1 or not files[0][0]:
        raise _err(422, "send exactly one file, as the form field 'file'")
    raw_name, data = files[0]
    try:
        name = raw_name.decode("utf-8")
    except UnicodeDecodeError:
        raise _err(422, "the file name is not valid UTF-8") from None
    if not data:
        raise _err(422, "the file is empty")
    if len(data) > vault_files.MAX_BYTES:
        raise _err(413, too_big)
    return name, data


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

    @app.put("/api/variables/{vid}/file")
    async def put_file(
        vid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        expected = _expected(request.query_params.get("expected_updated_at"))
        name, data = await _secret_file_upload(request)
        result = await _run(variables.put_file, vid, data, name, expected)
        return result if isinstance(result, Response) else _json(result)

    @app.put("/api/variables/{vid}/projects")
    async def put_projects(
        vid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ):
        payload = await _json_object(request)
        expected = _expected(payload.pop("expected_updated_at", None))
        if set(payload) != {"project_ids"}:
            raise _err(422, "send project_ids and expected_updated_at")
        result = await _run(variables.set_projects, vid, payload["project_ids"], expected)
        return result if isinstance(result, Response) else _json(result)

    @app.get("/api/variables/{vid}/access-log")
    async def get_access_log(vid: str, _user: str = Depends(logged_in)):
        result = await _run(variables.access_log, vid)
        return result if isinstance(result, Response) else _json({"entries": result})

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
