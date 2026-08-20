"""Upload route (agent-sessions#265): save a pasted/dropped file to the shared
uploads dir so an agent session can read it by path. Moved verbatim from
``main.create_app``.

**Modes are part of the contract, not cosmetics (#612 Phase 4).** An upload is whatever the
operator pasted into the composer — a screenshot of a dashboard, a log fragment, a config file
with a token in it. Before this, the directory was created with a bare ``mkdir`` and the file
written with ``write_bytes``, so both landed at whatever the process umask happened to be:
0755 / 0644 under the common default, i.e. **readable by every other local account** on a host
that is explicitly multi-account (other services use separate accounts). The store
is single-admin by design; its files should be too.

So the dir is pinned to ``0700`` and every file to ``0600``, and both are argued below at the
place they are enforced rather than left to a reader to infer.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

#: Mode of the uploads directory and of every file in it. Owner-only, both.
DIR_MODE = 0o700
FILE_MODE = 0o600
#: Bound on the same-second filename-collision retry. Each attempt is a distinct ``O_EXCL``
#: create, so this can only be reached by something pathological; it exists so the loop has a
#: terminating case that isn't "spin forever holding a request".
MAX_NAME_ATTEMPTS = 1000


def uploads_dir() -> Path:
    """The shared dir every upload lands in (``~/.agent-sessions/uploads/``).

    Exposed so other routes (e.g. compose-draft persistence, #477) can validate that a
    stored attachment path lives inside this namespace rather than re-deriving the path.
    """
    return Path.home() / ".agent-sessions" / "uploads"


def ensure_uploads_dir() -> Path:
    """The uploads dir, existing and at ``DIR_MODE`` — **created or corrected**, before any write.

    ``mkdir(mode=...)`` is not enough on its own for either half:

    * On creation the mode is masked by the umask, so it can end up *tighter* than asked but the
      call says nothing about what it actually got.
    * With ``exist_ok=True`` the mode argument is **ignored entirely** for a directory that is
      already there — which is every call after the first. An install that created this dir at
      0755 under an older build (every install before this change) would keep it at 0755 forever.

    So the ``chmod`` is unconditional and is what makes the guarantee true for existing installs
    rather than only for fresh ones. It runs *before* the file is created, so there is no window
    where a 0600 file sits in a world-traversable directory.
    """
    d = uploads_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(DIR_MODE)
    return d


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.post("/api/upload")
    async def upload_context(
        file: UploadFile = File(...),
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Save a pasted/dropped image or file so a Claude/opencode session can read
        # it by path (the web terminal can't carry image paste itself). Lands in a
        # shared ~/.agent-sessions/uploads/ — never in a project working tree.
        # Stream-read with a hard cap so a huge upload can't exhaust memory.
        max_bytes = 25 * 1024 * 1024
        size, chunks = 0, []
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(status_code=413, detail="file too large (max 25 MB)")
            chunks.append(chunk)
        if not size:
            raise HTTPException(status_code=422, detail="empty upload")
        # Sanitise to a bare, safe basename — no path separators, no traversal.
        raw_name = Path(file.filename or "upload").name
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:80] or "upload"
        dest_dir = ensure_uploads_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")

        # `O_CREAT | O_EXCL` is doing two jobs, and the second one is a bug fix rather than
        # style. The old shape was `while dest.exists(): ...` then `write_bytes`, which is a
        # TOCTOU: two uploads of the same basename in the same second both saw the name free and
        # the second silently overwrote the first. `O_EXCL` makes the check and the claim one
        # atomic operation, so the loser gets `FileExistsError` and takes the next name.
        #
        # The mode argument is masked by the umask, so it can only ever produce something
        # *tighter* than 0600 — never looser. `fchmod` on the descriptor then pins it exactly,
        # BEFORE any byte of the upload is written. That ordering is the whole point and is the
        # same one `atomicjson.atomic_write_json` argues at length: a chmod after the write
        # closes a window that has already passed.
        fd = None
        for n in range(MAX_NAME_ATTEMPTS):
            dest = dest_dir / (f"{stamp}-{safe}" if n == 0 else f"{stamp}-{n}-{safe}")
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
                break
            except FileExistsError:
                continue
        if fd is None:
            raise HTTPException(status_code=500, detail="could not allocate an upload filename")
        # Split in two so the fd is closed exactly once on every path: before `fdopen` succeeds
        # we own the raw descriptor, after it the file object does.
        try:
            os.fchmod(fd, FILE_MODE)
            fh = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            dest.unlink(missing_ok=True)
            raise
        try:
            with fh:
                for chunk in chunks:
                    fh.write(chunk)
        except BaseException:
            # A half-written upload is worse than none: the composer would hand an agent a path
            # to a truncated file. Drop it and let the error surface.
            dest.unlink(missing_ok=True)
            raise
        return JSONResponse({"path": str(dest), "name": safe})
