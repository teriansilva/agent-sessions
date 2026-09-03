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

import contextlib
import errno
import os
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

#: Mode of the uploads directory and of every file in it. Owner-only, both.
DIR_MODE = 0o700
FILE_MODE = 0o600
#: Bound on the same-second filename-collision retry. Each attempt is a distinct ``O_EXCL``
#: create, so this can only be reached by something pathological; it exists so the loop has a
#: terminating case that isn't "spin forever holding a request".
MAX_NAME_ATTEMPTS = 1000

#: The stored-basename shape — exactly what this route writes: ``<YYYYmmdd-HHMMSS>[-<n>]-<safe>``
#: with ``safe`` already reduced to ``[A-Za-z0-9._-]`` and capped at 80. One path component of
#: this shape is the only thing that can name an upload; a hand-placed ``manual.png`` is not an
#: upload and is never served or referenced (Hermes on #906: the earlier charset-only pattern
#: accepted any simple basename while claiming to be "the stored shape").
STORED_RE = re.compile(r"\d{8}-\d{6}(?:-\d+)?-[A-Za-z0-9._-]{1,80}")
#: The components below ``$HOME`` the uploads folder lives at — bound one at a time, never
#: followed (``open_uploads_dir``).
_UPLOAD_PARTS = (".agent-sessions", "uploads")
#: What ``GET /api/uploads/{stored}`` will serve, keyed by suffix. Anything else is 404: the route
#: is a picture viewer for the templates gallery, not a file reader. ``templates.IMAGE_SUFFIXES``
#: is the same set, so what a template may reference and what the gallery can show never drift.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
#: An upload can be a screenshot of a dashboard or a config with a token in it; a template is
#: verbatim instructions. Neither may sit in a browser cache after sign-out, so every response
#: on both surfaces — success and error alike — carries the same policy ``routes/files.py``
#: applies to file bytes. ``private`` alone is not that policy: it only stops SHARED caches.
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
#: Read the served image in slices of this size from the validated descriptor.
_READ_CHUNK = 64 * 1024


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


def open_uploads_dir() -> int:
    """A directory descriptor on the uploads folder, bound WITHOUT following a symlink.

    ``uploads_dir().resolve()`` followed whatever ``~/.agent-sessions/uploads`` pointed at, so a
    same-UID process could replace the folder itself with a symlink and redirect the read-back
    into any directory it liked — the final-component ``O_NOFOLLOW`` never saw it (Hermes on
    #906, reproduced). ``$HOME`` is the trusted root; each component below it is opened relative
    to the previous descriptor with ``O_DIRECTORY | O_NOFOLLOW``, so what this returns is the
    real folder or an ``OSError`` (``ELOOP`` for a symlink at either level). A symlinked uploads
    folder is therefore NOT supported — it reads back as 404 and refuses uploads, by design;
    binding a mutable namespace by re-resolving it per request is the hole, not a feature.
    The write route, the read-back and the template validator all go through here, so "an
    upload" means one thing. The caller owns the descriptor.
    """
    fd = os.open(str(Path.home()), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in _UPLOAD_PARTS:
            nfd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_upload(stored: str) -> tuple[int, os.stat_result]:
    """Open the upload named ``stored`` read-only through the bound folder and ``fstat`` it.

    One predicate for "an upload the gallery can read back", shared by the read-back route and
    ``templates.validate``: the folder bound by ``open_uploads_dir``, the entry opened
    ``O_NOFOLLOW`` relative to it, a regular file. Raises ``OSError`` — ``ELOOP`` for a symlink
    at the entry or at the folder, ``ENOENT`` when absent, ``EISDIR`` for anything that is not a
    regular file. The caller owns the descriptor on success.
    """
    dir_fd = open_uploads_dir()
    try:
        fd = os.open(stored, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OSError(errno.EISDIR, "not a regular file", stored)
    return fd, st


def _gone() -> HTTPException:
    """The one answer the read-back route gives for every refusal, so it enumerates nothing."""
    return HTTPException(status_code=404, detail="no such upload", headers=NO_STORE)


def _iter_fd(fh, remaining: int) -> Iterator[bytes]:
    """Stream exactly ``remaining`` bytes from an already-validated descriptor, then close it.

    The descriptor is pinned to the inode that passed the checks, so a file swapped under the
    directory entry after the open changes nothing here. ``remaining`` is the size ``fstat``
    reported on that same descriptor, which is what ``Content-Length`` promised.
    """
    with fh:
        while remaining > 0:
            chunk = fh.read(min(_READ_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def register(app: FastAPI, *, logged_in, csrf_guard) -> None:
    @app.middleware("http")
    async def _uploads_are_never_cached(request: Request, call_next):
        """``NO_STORE`` at the OUTERMOST boundary of ``/api/uploads/`` and the ``/api/upload``
        POST — whose response names an absolute host path — (the ``routes/files.py``
        shape, for the same two reasons): ``Depends(logged_in)`` raises its 401 before any
        handler code runs, and an exception escaping the endpoint is rendered by Starlette's
        outer middleware, which knows nothing about this policy."""
        path = request.url.path
        if not (path.startswith("/api/uploads/") or path == "/api/upload"):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception:
            return JSONResponse({"detail": "no such upload"}, status_code=500, headers=NO_STORE)
        response.headers.update(NO_STORE)
        return response

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
        # Bind the folder the same way the read-back does — by descriptor, never through a
        # symlink — so what this writes is by construction what that can serve.
        try:
            dir_fd = open_uploads_dir()
        except OSError:
            raise HTTPException(
                status_code=500,
                detail="the uploads folder is not a plain directory (a symlink is refused)",
            ) from None
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
        basename = ""
        # The directory descriptor stays open through the write AND the cleanup. A failed
        # upload used to be dropped with `dest.unlink()` — a fresh pathname lookup — after the
        # descriptor was closed, so a same-UID process that renamed the bound folder and left a
        # symlink at its old name could make the cleanup delete the same basename somewhere else
        # while the partial file stayed behind in the real folder (Hermes on #906, round 3).
        # `os.unlink(basename, dir_fd=…)` removes the entry from the directory that RECEIVED the
        # upload, whatever the pathname points at by then.
        try:
            for n in range(MAX_NAME_ATTEMPTS):
                basename = f"{stamp}-{safe}" if n == 0 else f"{stamp}-{n}-{safe}"
                try:
                    fd = os.open(
                        basename,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        FILE_MODE,
                        dir_fd=dir_fd,
                    )
                    break
                except FileExistsError:
                    continue
            if fd is None:
                raise HTTPException(status_code=500, detail="could not allocate an upload filename")

            def discard() -> None:
                with contextlib.suppress(OSError):
                    os.unlink(basename, dir_fd=dir_fd)

            # Split in two so the fd is closed exactly once on every path: before `fdopen`
            # succeeds we own the raw descriptor, after it the file object does.
            try:
                os.fchmod(fd, FILE_MODE)
                fh = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                discard()
                raise
            try:
                with fh:
                    for chunk in chunks:
                        fh.write(chunk)
            except BaseException:
                # A half-written upload is worse than none: the composer would hand an agent a
                # path to a truncated file. Drop it and let the error surface.
                discard()
                raise
        finally:
            os.close(dir_fd)
        dest = dest_dir / basename
        # `name` is the sanitized ORIGINAL filename and names no file on disk; `stored` is the
        # basename actually written, which is what `GET /api/uploads/{stored}` reads back
        # (#905 P1 — Hermes caught that a client keying on `name` would 404).
        return JSONResponse({"path": str(dest), "name": safe, "stored": dest.name})

    @app.get("/api/uploads/{stored}")
    async def read_upload(stored: str, _user: str = Depends(logged_in)) -> StreamingResponse:
        """Serve one image back out of the uploads folder, for the templates gallery (#905).

        The composer previews a pasted ``File`` through ``data:``/``blob:`` URLs and never needs
        the stored bytes again; a template stores only the path, so its thumbnail has to come
        from here. Three refusals, each 404 so the route says nothing about what else exists:

        * the key is not one path component of the stored shape (``STORED_RE``; ``.`` and
          ``..`` refused by name);
        * the suffix is not in ``IMAGE_TYPES`` — a text or PDF upload is not served, ever;
        * the entry is not a regular file directly inside the bound uploads folder.

        **The open IS the containment check, and the bytes come from that same descriptor.**
        The first cut resolved the path, checked it, and then handed ``FileResponse`` the
        pathname — which ``stat``s and ``open``s it again, so a same-UID process (an agent
        working in the shared uploads folder) could swap the checked file for a symlink pointing
        outside between the check and the open (Hermes on #906, reproduced). Now the file is
        opened ONCE, relative to a folder descriptor bound with ``O_NOFOLLOW`` at every
        component below ``$HOME`` (``open_uploads_dir`` — the second round found the folder
        itself could be swapped for a symlink), with ``O_NOFOLLOW`` on the name — a symlink at
        the entry fails the open whatever it points at, inside or out — ``fstat`` on the
        descriptor decides "regular file", and the response streams from that descriptor. No
        path is resolved anywhere, so there is nothing for a rename to race.

        The content type comes from the suffix allowlist (never sniffed from the bytes), and the
        response is ``inline`` + ``nosniff`` + the ``NO_STORE`` policy every response on this
        surface carries.
        """
        if stored in (".", "..") or not STORED_RE.fullmatch(stored):
            raise _gone()
        media_type = IMAGE_TYPES.get(Path(stored).suffix.lower())
        if media_type is None:
            raise _gone()
        # The folder is bound by descriptor with NOFOLLOW at every component below $HOME, and
        # the entry is opened NOFOLLOW relative to it (`open_upload`). No path is resolved.
        try:
            fd, st = open_upload(stored)
        except OSError:
            raise _gone() from None
        fh = os.fdopen(fd, "rb")
        return StreamingResponse(
            _iter_fd(fh, st.st_size),
            media_type=media_type,
            headers={
                **NO_STORE,
                "Content-Length": str(st.st_size),
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            },
        )
