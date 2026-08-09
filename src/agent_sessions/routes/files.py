"""File-panel routes (#783): bounded, read-only directory listing + file read.

Both are GET and read-only, so they take ``logged_in`` but not ``csrf_guard`` (that guard exists
for state-changing verbs). All filesystem work is dispatched to the file panel's OWN
thread pool (:func:`agent_sessions.files.executor`) — this app has already been bitten by
synchronous probes on the event loop making typing sluggish across *every* session, so off-loop
is an invariant here, not a preference. Deliberately not ``asyncio.to_thread``: that shares the
interpreter's default pool, which would make the admission budget count one thing while a larger,
shared pool executed the work.

**Every response carries ``Cache-Control: no-store``, success and error alike.** The read route
returns file bytes (``.env``, key material); the list route returns absolute paths, which are
sensitive on their own. Being a GET is not a reason to let a browser or an intermediary keep
either.
"""

from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import files, gitpanel

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_NO_STORE)


async def _run(key: str, fn, *args):
    """Admit, then run off the loop on the panel's OWN pool, with the slot owned by the worker.

    Three properties, each of which the obvious version gets wrong:

    * ``acquire`` happens HERE, above the dispatch. Admitting inside the worker bounds what
      *runs* while a flood queues behind it.
    * The work runs on :func:`files.executor`, **not** ``asyncio.to_thread``. ``to_thread``
      dispatches to the interpreter's default shared pool, so the budget would be counting one
      thing while a different, larger pool executed the work — and an unrelated flood on that
      shared pool could starve the panel without ever raising ``FilesBusy``. Admission is only an
      honest bound when the budget and the pool are the same thing.
    * Release ownership is decided by whether the worker actually STARTED. If it did, the worker
      owns it (releasing here would hand the slot back mid-flight). If the request was cancelled
      while the callable was still queued, the worker never runs and only this frame can release
      it — without that branch, a client disconnect during the submit→start window leaks the slot
      permanently, and eight of them kill the panel until restart.

    ``acquire`` raises :class:`files.FilesBusy`, an ``FsError``, so the caller's existing error
    mapping turns it into a 503 rather than a 500.
    """
    slot = files.acquire_slot(key)
    try:
        cf = files.executor().submit(files.run_slot, slot, fn, *args)
    except BaseException:
        slot.release()  # never submitted: nobody in a worker can ever release it
        raise
    try:
        return await asyncio.wrap_future(cf)
    except BaseException:
        # `cancelled()` is True ONLY when the callable never began — the queued work was dropped,
        # so no worker will ever reach `run_slot`'s finally and the slot would leak for the life
        # of the process. When the worker did start, this must NOT release: the thread is still
        # running and owns it. `Slot.release` is exactly-once, so the two paths cannot both fire.
        if cf.cancelled():
            slot.release()
        raise


def register(app: FastAPI, *, logged_in) -> None:
    @app.middleware("http")
    async def _file_routes_are_never_cached(request: Request, call_next):
        """Apply the no-store policy at the OUTERMOST boundary of these routes.

        Covers `/api/files/` and `/api/git/` alike: a diff carries file bytes and a status carries
        absolute paths, so neither may be cached. Two escapes had to be closed, in this order:

        * `Depends(logged_in)` raises its own 401 *before* any handler runs, so header code
          inside the handlers could never execute — an auth failure that still names the
          requested path went out cacheable.
        * An exception escaping the endpoint — a `capabilities()` failure, or a response body
          that will not encode — unwinds past this middleware and is rendered by Starlette's
          outer error middleware, which knows nothing about this policy. So exceptions are
          caught and rendered HERE, where the headers can still be attached.
        """
        if not (
            request.url.path.startswith("/api/files/") or request.url.path.startswith("/api/git/")
        ):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse(
                {"detail": "the file service failed"}, status_code=500, headers=_NO_STORE
            )
        response.headers.update(_NO_STORE)
        return response

    @app.get("/api/files/list")
    async def files_list(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # One directory under $HOME. `total` is an integer iff `complete`; a scan stopped by the
        # entry cap or the wall-clock budget reports complete:false / total:null instead of a
        # count it never finished.
        raw = request.query_params.get("path")
        try:
            payload = await _run(raw or "", files.list_dir, raw)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            # Belt and braces: an unhandled error would otherwise be rendered by the default
            # handler WITHOUT no-store, and its body can carry the path that caused it.
            raise HTTPException(
                status_code=500, detail="could not read the folder", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.get("/api/files/read")
    async def files_read(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # One regular file, capped while reading. Binary returns metadata only.
        path = request.query_params.get("path")
        if not path or not path.strip():
            raise HTTPException(status_code=422, detail="path is required", headers=_NO_STORE)
        try:
            payload = await _run(path, files.read_file, path)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not read the file", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.get("/api/git/status")
    async def git_status(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # Repository state for the panel's current root. `repo: null` is a normal 200 — "not a
        # repository" is a state, not a failure, and so are unborn/detached/no-upstream.
        raw = request.query_params.get("path")
        try:
            payload = await _run(raw or "", gitpanel.git_status, raw)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not read the repository", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.get("/api/git/diff")
    async def git_diff(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # Assembled from `cat-file` blobs plus the descriptor-verified worktree read — `git diff`
        # is never invoked, because no flag stops a repo-configured `filter.*` clean driver.
        path = request.query_params.get("path")
        if not path or not path.strip():
            raise HTTPException(status_code=422, detail="path is required", headers=_NO_STORE)
        staged = request.query_params.get("staged") in ("1", "true", "yes")
        try:
            payload = await _run(path, gitpanel.git_diff_kw, path, staged)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not build the diff", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.get("/api/files/capabilities")
    async def files_capabilities(_user: str = Depends(logged_in)) -> JSONResponse:
        # The panel asks once and disables itself with the stated reason when the platform can't
        # support the containment contract — fail closed, never a quiet downgrade.
        caps = files.capabilities()
        return _json({"ok": caps.ok, "reason": caps.reason})
