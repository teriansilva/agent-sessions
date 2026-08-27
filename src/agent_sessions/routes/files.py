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

from .. import files, filewrite, gitpanel, gitwrite
from . import fileupload

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


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
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

    @app.post("/api/files/upload/batch")
    async def files_upload_batch(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Mint a reservation from an immutable manifest (#807). ADMISSION only: an over-budget
        # folder drop fails here, before a single byte moves, which is the whole point of asking
        # for a manifest. It is a client claim, so it never bounds the stream — `Batch.take_bytes`
        # does, per chunk.
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422, detail="a JSON object is required", headers=_NO_STORE
            )
        try:
            payload = filewrite.create_batch(body.get("files"), fileupload.owner_of(request))
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        return _json(payload)

    @app.post("/api/files/upload/skip")
    async def files_upload_skip(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # The operator chose Skip on a collision. Without this the client simply went quiet, the
        # manifest entry stayed pending, and the batch held its slot for the full idle TTL —
        # eight Skip drops locked the ninth out. A skip is a terminal outcome and says so.
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422, detail="a JSON object is required", headers=_NO_STORE
            )
        try:
            batch = filewrite.get_batch(body.get("batch_id"), fileupload.owner_of(request))
            # Bound to the ENTRY the operator answered. The relpath used to be accepted and
            # ignored, so one Skip could settle a batch whose sibling was still streaming.
            relpath = body.get("relpath")
            batch.skip_file(relpath if isinstance(relpath, str) else "")
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        return _json({"skipped": True, "batch": batch.snapshot()})

    @app.post("/api/files/upload")
    async def files_upload(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        """One file into the directory the panel is showing (#807).

        Deliberately **not** ``file: UploadFile = File(...)``. FastAPI resolves that only after
        Starlette has parsed the entire multipart request, and in the pinned Starlette a file part
        is spooled to a ``SpooledTemporaryFile`` until EOF — so a chunked or false-length request
        writes unbounded bytes to temporary disk before the route's first line runs, and the 413
        arrives after the damage. The existing ``/api/upload`` route has exactly that shape.

        So the parser is driven from ``request.stream()`` here, bytes are counted **as they
        arrive**, and file bytes go straight to a descriptor. Nothing is ever spooled, and the
        refusal happens mid-stream rather than after it.
        """
        try:
            return await fileupload.ingest(request)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=500, detail="the upload failed", headers=_NO_STORE
            ) from None

    @app.get("/api/git/branches")
    async def git_branches(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # A READ, so it stays on the read path's sanitized gitdir like every other read.
        raw = request.query_params.get("path")
        try:
            payload = await _run(raw or "", gitpanel.git_branches, raw)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not list the branches", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.get("/api/git/push-target")
    async def git_push_target(request: Request, _user: str = Depends(logged_in)) -> JSONResponse:
        # The dry preflight (#806 Phase 3): which remote a push WOULD go to, resolved server-side
        # so the control can render `PUSH -> origin` before the operator commits to it. A GET
        # because it is a read — it resolves and reports, and changes nothing. Ambiguity comes
        # back as `ok:false` + candidates rather than an error, because the control has to RENDER
        # the refusal; the POST is where that same ambiguity is actually enforced.
        raw = request.query_params.get("path")
        if not raw or not raw.strip():
            raise HTTPException(status_code=422, detail="path is required", headers=_NO_STORE)
        remote = request.query_params.get("remote")
        try:
            payload = await _run(raw, gitwrite.push_target, raw, remote)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="could not resolve the push target", headers=_NO_STORE
            ) from None
        return _json(payload)

    async def _write(request: Request, fn, *keys):
        """Shared body for every git WRITE route (#806).

        Three properties, none of them incidental:

        * The verb is POST and `csrf_guard` is a dependency on each route — these are the first
          state-changing routes in this surface, and a write reachable by GET would be reachable by
          an image tag.
        * The work runs off-loop on the file panel's OWN pool under its admission slot, exactly
          like the reads, so a slow network fetch cannot stall the event loop or outrun the budget.
        * `FsError` carries its own status (409 for a refusal, 423 for a busy repository, 422 for a
          rejected name), so a refusal reaches the client as the reason it actually is rather than
          as a generic failure. That distinction IS the feature — see the issue's refusal states.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422, detail="a JSON object is required", headers=_NO_STORE
            )
        path = body.get("path")
        if not isinstance(path, str) or not path.strip():
            raise HTTPException(status_code=422, detail="path is required", headers=_NO_STORE)
        # A key may name ALTERNATIVE spellings, first present wins — see `/api/git/switch`.
        args = [
            next((body[k] for k in spec.split("|") if body.get(k) is not None), None)
            for spec in keys
        ]
        try:
            payload = await _run(path, fn, path, *args)
        except files.FsError as e:
            raise HTTPException(status_code=e.status, detail=str(e), headers=_NO_STORE) from None
        except Exception:
            raise HTTPException(
                status_code=500, detail="the git operation failed", headers=_NO_STORE
            ) from None
        return _json(payload)

    @app.post("/api/git/fetch")
    async def git_fetch(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        return await _write(request, gitwrite.git_fetch, "remote")

    @app.post("/api/git/pull")
    async def git_pull(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Fast-forward only. A diverged branch is a 409 with the numbers, never an attempted merge.
        return await _write(request, gitwrite.git_pull)

    @app.post("/api/git/switch")
    async def git_switch(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Refuses a dirty tree: `git switch` silently carries uncommitted work across (measured).
        # #806 documents the start point as `from`; the first implementation read only `start`, so
        # a caller following the published contract silently created from HEAD instead. Both
        # spellings are accepted and normalised, because breaking either would be worse than
        # carrying one alias.
        return await _write(
            request, gitwrite.git_switch, "branch", "create", "from|start", "expect"
        )

    @app.post("/api/git/branch/delete")
    async def git_branch_delete(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # `-d` only — an unmerged branch is refused, and no force variant exists to reach for.
        return await _write(request, gitwrite.git_branch_delete, "branch")

    @app.post("/api/git/stage")
    async def git_stage(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Whole files only, in either direction. Staging runs the repo's own clean filter — an
        # accepted, documented residual (gitwrite.git_stage), not an oversight.
        return await _write(request, gitwrite.git_stage, "paths", "staged", "expect")

    @app.post("/api/git/discard")
    async def git_discard(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # The one destructive route. Every path must appear in a status the server re-reads
        # inside the call, so a tampered request cannot widen the blast radius past the
        # confirmation the operator actually saw.
        return await _write(request, gitwrite.git_discard, "paths", "expect")

    @app.post("/api/git/commit")
    async def git_commit(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # No amend, no --no-verify (hooks are already neutralized), author from the operator's
        # own git identity.
        return await _write(request, gitwrite.git_commit, "message", "expect")

    @app.post("/api/git/push")
    async def git_push(
        request: Request, _user: str = Depends(logged_in), _csrf: None = Depends(csrf_guard)
    ) -> JSONResponse:
        # Current branch to a server-resolved target; never --force, never a client refspec.
        # `expect` carries the target the panel DISPLAYED and is REQUIRED: a config change
        # between the preflight and the click is a refusal rather than a silent redirect, and an
        # omitted binding is a 422 rather than a quiet fallback to re-resolving. Optional would
        # mean any stale client keeps the behaviour the binding exists to remove.
        return await _write(request, gitwrite.git_push, "remote", "expect")

    @app.get("/api/files/capabilities")
    async def files_capabilities(_user: str = Depends(logged_in)) -> JSONResponse:
        # The panel asks once and disables itself with the stated reason when the platform can't
        # support the containment contract — fail closed, never a quiet downgrade.
        caps = files.capabilities()
        return _json({"ok": caps.ok, "reason": caps.reason})
