"""The bounded multipart ingest behind ``POST /api/files/upload`` (#807).

Kept out of :mod:`agent_sessions.routes.files` because it is the one place in this app that
parses a request body itself, and the reason it does is worth stating in one piece.

**FastAPI's ``UploadFile`` is already too late.** A route parameter declared
``file: UploadFile = File(...)`` is resolved only *after* Starlette has parsed the whole
multipart request. In the pinned Starlette the parser's ``max_part_size`` applies only to
**non-file** parts — a file part is written to a ``SpooledTemporaryFile`` until EOF. So a request
that is chunked, or that lies in ``Content-Length``, can consume unbounded temporary disk before
the route's first statement executes, and a 413 raised there arrives after the damage is done.
(``routes/upload.py`` demonstrates the same dependency: its "stream-read with a hard cap" loop
reads from a file that is already fully spooled.)

So the parser is driven from ``request.stream()`` here:

* every chunk is charged against the batch's reservation **before it is accepted**, so the bound
  never depends on a client-declared size and an under-declaring client gains nothing;
* a hard ingress ceiling aborts the request regardless of parser state, which is what bounds a
  chunked body with no honest length;
* file bytes go straight to the destination descriptor, so nothing is spooled anywhere;
* a refusal mid-stream removes the partial file through its parent descriptor, so no artifact is
  left at the destination.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib

from fastapi import Request
from fastapi.responses import JSONResponse

try:  # the package renamed; the old name still works but emits a PendingDeprecationWarning
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover - older pins
    from multipart.multipart import MultipartParser, parse_options_header

from .. import files, filewrite

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
#: Headroom over the per-file cap for multipart boundaries, part headers and the other fields.
#: The ceiling that actually stops a runaway body; the per-file cap is charged separately.
FRAMING_HEADROOM = 64 * 1024
MAX_REQUEST_BYTES = filewrite.MAX_FILE_BYTES + FRAMING_HEADROOM
#: Non-file fields are tiny. Bounding them separately stops a "field" being used as the payload.
MAX_FIELD_BYTES = 8 * 1024
_FIELDS = ("dir", "relpath", "on_collision", "batch_id")
#: Parser feed size. Small enough that the bytes parked for the executor between slices are a
#: hand-off rather than a buffer, large enough not to dominate a 25 MiB upload with dispatches.
_SLICE = 64 * 1024


def _reap_late_open(fut) -> None:
    """Clean up a destination whose open finished after the request was cancelled.

    Without this, cancelling in the submit-to-assignment window left an empty file at the
    destination: the future still completed, but nothing held its result, so nothing abandoned it.
    """

    def done(f) -> None:
        try:
            dest = f.result()
        except BaseException:
            return
        with contextlib.suppress(Exception):
            dest.abandon()

    fut.add_done_callback(done)


def _check_then_publish(dest: filewrite.Destination) -> None:
    """Refuse a completed gitdir, else publish — in one synchronous step, off the loop."""
    filewrite.refuse_completed_gitdir(dest.dest_dir)
    dest.publish()


def _write_chunk(dest: filewrite.Destination, data: bytes) -> None:
    try:
        dest.write(data)
    except OSError as e:
        if filewrite.disk_full(e):
            raise files.FsError("the disk is full", status=507) from None
        raise files.FsError("could not write the file", status=500) from None


class _Ingest:
    """Parser callbacks plus the accounting they enforce. One instance per request."""

    def __init__(self, owner: str = "") -> None:
        #: The authenticated session this request belongs to — a batch may only be spent by the
        #: session that minted it.
        self.owner = owner
        self.fields: dict[str, bytearray] = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._part_name: str | None = None
        self._part_is_file = False
        self.dest: filewrite.Destination | None = None
        self.batch: filewrite.Batch | None = None
        self.relpath = ""
        self.streamed = 0
        self.slot_taken = False
        self.saw_file = False
        self.pending: list[bytes] = []

    # -- header assembly -----------------------------------------------------

    def on_part_begin(self) -> None:
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._part_name = None
        self._part_is_file = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if bytes(self._header_field).lower() == b"content-disposition":
            _, params = parse_options_header(bytes(self._header_value))
            name = params.get(b"name", b"").decode("utf-8", "replace")
            self._part_name = name
            # A part carrying a filename is the payload; everything else is a field. The client
            # sends the fields FIRST (see `on_part_data`), so the destination is known by then.
            self._part_is_file = b"filename" in params
        self._header_field = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        """The file part starts here — which is the last moment the FIELDS can still be required.

        Everything resolved here is in-memory (a dict lookup, a counter under a lock), so it is
        safe on the event loop. The filesystem work it enables is dispatched by the driver.
        """
        if not self._part_is_file:
            return
        if self.saw_file:
            # ONE file per request, enforced rather than assumed. Without this a second file part
            # streamed into the FIRST part's descriptor — measured: two parts produced a single
            # `first.txt` containing `AAAABBBB`, so a crafted multipart could append content to a
            # file the operator named and never saw. (The batch accounting desynced with it, too:
            # a second file slot was taken against one destination.)
            raise files.FsError("only one file per request", status=422)
        self.saw_file = True
        if not self.field("dir"):
            # The client controls the FormData order and sends the fields first. Refusing here
            # costs at most one chunk — never a whole spooled file, which is the shape this
            # module exists to avoid.
            raise files.FsError("the upload fields must be sent before the file part", status=422)
        self.relpath = self.field("relpath")
        if not self.relpath:
            raise files.FsError("relpath is required", status=422)
        self.batch = (
            filewrite.get_batch(self.field("batch_id"), self.owner)
            if self.field("batch_id")
            else filewrite.implicit_batch()
        )
        self.batch.take_file_slot(self.relpath)
        self.slot_taken = True

    # -- data ----------------------------------------------------------------

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if not chunk:
            return
        if self._part_is_file:
            # Charged BEFORE the bytes are accepted anywhere. This is the load-bearing bound: it
            # never reads a declared size, so `reserved + committed + in-flight <= cap` holds at
            # every instant rather than after a reconciliation that runs too late to refuse.
            self.batch.take_bytes(self.relpath, len(chunk))  # type: ignore[union-attr]
            self.streamed += len(chunk)
            # Parked for the driver to flush through the executor. Bounded by ONE parser slice,
            # because the driver flushes after every slice — this is a hand-off, not a spool.
            self.pending.append(chunk)
        elif self._part_name in _FIELDS:
            buf = self.fields.setdefault(self._part_name, bytearray())
            if len(buf) + len(chunk) > MAX_FIELD_BYTES:
                raise files.FsError("an upload field is too large", status=413)
            buf += chunk

    def field(self, name: str, default: str = "") -> str:
        raw = self.fields.get(name)
        return bytes(raw).decode("utf-8", "replace") if raw is not None else default


def owner_of(request: Request) -> str:
    """A stable id for the authenticated session, used to scope batch ownership.

    The signed session cookie is the app's own notion of "who this is"; hashing it keeps the
    cookie itself out of the batch registry while still separating one session from another.
    """
    # The CSRF token is the practical key: it is present under both auth modes and is already
    # bound to the session cookie. (The cookie itself is named `agent_sessions`, not `session` —
    # reading the wrong name silently fell through to the token anyway, which worked by accident
    # rather than by design.) Hashing keeps the credential out of the batch registry, and a
    # rotation deliberately resets batch ownership rather than carrying it across.
    raw = request.headers.get("x-csrf-token") or request.cookies.get("agent_sessions") or ""
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def ingest(request: Request) -> JSONResponse:
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data":
        raise files.FsError("this route takes a multipart upload", status=415)
    boundary = params.get(b"boundary")
    if not boundary:
        raise files.FsError("the multipart request has no boundary", status=422)

    # A cheap early rejection only. `Content-Length` is client-supplied and absent entirely under
    # chunked transfer, so it is never the enforcement — the per-chunk ceiling below is.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise files.FsError(
            f"that file is larger than the limit "
            f"({filewrite.MAX_FILE_BYTES // (1024 * 1024)} MB)",
            status=413,
        )

    loop = asyncio.get_running_loop()
    st = _Ingest(owner_of(request))
    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": st.on_part_begin,
            "on_header_field": st.on_header_field,
            "on_header_value": st.on_header_value,
            "on_header_end": st.on_header_end,
            "on_headers_finished": st.on_headers_finished,
            "on_part_data": st.on_part_data,
        },
    )

    slot = None
    total = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            # The ceiling that binds regardless of what the parser believes, and the one a
            # chunked body with no honest length runs into. Aborting HERE is what keeps temp
            # disk bounded — there is no temp file to bound, because nothing is spooled.
            if total > MAX_REQUEST_BYTES:
                raise files.FsError(
                    f"that file is larger than the limit "
                    f"({filewrite.MAX_FILE_BYTES // (1024 * 1024)} MB)",
                    status=413,
                )
            # Fed in bounded SLICES so the driver gets control back between them: the parser can
            # hand us headers and payload inside one network chunk, and the destination open is
            # blocking filesystem work that must not run on the event loop.
            for i in range(0, len(chunk), _SLICE):
                parser.write(chunk[i : i + _SLICE])
                if st.saw_file and st.dest is None:
                    # Blocking filesystem work on the panel's OWN pool under its admission slot,
                    # the same rule every other filesystem call in this surface follows.
                    #
                    # The slot is handed to the WORKER via `files.run_slot`, not released by this
                    # task. Releasing it here reported the panel idle while the open was still
                    # running, and — worse — a cancellation between submit and assignment left
                    # `st.dest` unset, so the cleanup below had no descriptor to abandon and an
                    # empty file stayed at the destination. `opened` keeps the result reachable
                    # even if this frame is cancelled before the assignment lands.
                    # No serialisation here any more. Two uploads could each supply a different
                    # missing piece of a gitdir, so this used to take a per-candidate lock — and
                    # that lock was defeated three review rounds running (ownership-blind
                    # rollback, alias-split keys, then a rename between deriving the key and
                    # opening the destination). It is gone because the shape is now
                    # uncompletable at all: `_split_relpath` refuses to create a HEAD file, and
                    # without one no directory becomes a repository. There is no race left to
                    # serialise.
                    slot = files.acquire_slot(st.field("dir"))
                    opened = files.executor().submit(
                        files.run_slot,
                        slot,
                        filewrite.open_destination,
                        st.field("dir"),
                        st.relpath,
                        st.field("on_collision") or "fail",
                    )
                    try:
                        st.dest = await asyncio.wrap_future(opened)
                    except asyncio.CancelledError:
                        # The worker may still be mid-open. Let it finish and clean up after it,
                        # rather than leaving whatever it created behind.
                        _reap_late_open(opened)
                        raise
                    finally:
                        slot = None  # the worker owns it now; `run_slot` releases exactly once
                if st.pending:
                    data, st.pending = b"".join(st.pending), []
                    await loop.run_in_executor(files.executor(), _write_chunk, st.dest, data)
        parser.finalize()
        if st.pending and st.dest is not None:
            data, st.pending = b"".join(st.pending), []
            await loop.run_in_executor(files.executor(), _write_chunk, st.dest, data)
        if st.dest is None:
            raise files.FsError("the upload carried no file", status=422)
        # The fence: an upload that COMPLETED a gitdir shape is refused here, after the bytes
        # landed but before they are visible. Check and publish are ONE executor job — separate
        # jobs left a window, and the thread lock that used to bracket them deadlocked the event
        # loop (it was held across these awaits). The sibling race it was meant to close is
        # handled by the rollback in `Destination.abandon`, which does not need serialisation.
        await loop.run_in_executor(files.executor(), _check_then_publish, st.dest)
        # Landed. A settled batch is retired by `_reap`, so a finished drop stops counting
        # against the session's allowance immediately rather than for the whole idle TTL.
        if st.batch is not None and st.slot_taken:
            st.batch.finish_file(st.relpath, "landed")
    except BaseException as exc:
        # A refused or failed upload leaves NO artifact at the destination and does not poison
        # the batch: the partial file is removed through its parent descriptor, and the
        # reservation is rolled back.
        if st.dest is not None:
            # A 403 here is the gitdir-completion refusal, and only that path may retire a shared
            # empty component — see `Destination.abandon`.
            st.dest.abandon()
        if st.batch is not None and st.slot_taken:
            st.batch.release_file_slot(st.relpath, st.streamed)
            # A name COLLISION is a question, not an outcome: the operator still has to choose
            # skip / keep both / replace, and the client retries this same batch. Settling it here
            # retired a one-file batch immediately, `_reap` deleted it, and the retry came back
            # "unknown or expired" — the collision flow did not work at all. Every other failure
            # is terminal and settles, so a batch of failures still retires.
            # A 409 is only PENDING on the first, default attempt — that is the question the
            # operator still has to answer. A retry already carries their choice, so a 409 there
            # is terminal; and Skip settles through the explicit `skip` mode below rather than by
            # the client going quiet. Treating every 409 as pending left eight Skip drops holding
            # their batches for the full TTL.
            mode = st.field("on_collision") or "fail"
            pending = isinstance(exc, files.FsError) and exc.status == 409 and mode == "fail"
            if pending:
                # Record the question against THIS entry, so the explicit Skip endpoint has
                # something to bind to — and can settle only the entry actually being asked about.
                st.batch.mark_pending_collision(st.relpath)
            else:
                st.batch.finish_file(st.relpath, "failed")
        raise
    finally:
        if slot is not None:
            # Only reachable when the submit itself failed: nobody in a worker can release it.
            slot.release()

    return JSONResponse(
        {
            # Built from the CONTAINED base plus validated components — never echoed back from
            # the client's own `dir` string.
            "path": st.dest.path,
            "name": st.dest.final,
            "relpath": st.relpath,
            "bytes": st.streamed,
            "batch": st.batch.snapshot() if st.batch else None,
        },
        headers=_NO_STORE,
    )
