"""Review and install of a project MCP server as background operations (#2163 P1b).

A review resolves a package's closure and an install downloads it: minutes on a busy host, far
longer than a proxy or the Home Free relay keeps one request open. So both run here, in this
process, and the route answers at once with an operation the client polls.

The client names each operation (``request_id``, a UUID like `/api/plugins`): repeating a POST
with the same id returns the same operation instead of starting a second one, so a retried or
duplicated request never runs a tool twice. One operation per server at a time; an operation
for another id while one runs is refused. Operations live in memory: after an app restart an id
is unknown and the client starts again (a review writes a new lock; an install of an already
installed review is a no-op, `mcp_install.install`).

**A link's resolution is an operation with no server** (#2353, kind ``resolve``): resolving never
creates a server record to obtain a key. It is keyed by its ``request_id`` alone and bound to its
link: the same id with a different link is refused (409), never answered with the earlier result.
A finished resolution holds ``{id, source, origin}`` server-side (`bound`); `create` takes it by
id (`resolution`), once, while it is fresh (`RESOLUTION_TTL_S`). After expiry, trimming or a
restart the id is unknown and the client resolves the link again: provenance is never
reconstructed from client fields. A running resolution can be stopped (`stop`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .mcp_registry import Conflict, NotFound, RegistryError

log = logging.getLogger(__name__)

KINDS = ("review", "install", "resolve")
KEEP = 64  # finished operations remembered (newest), for a client that polls late
#: How long a finished resolution can be registered (`resolution`); then: resolve the link again.
RESOLUTION_TTL_S = 6 * 3600
GONE = (
    "this link's resolution is no longer held (it expired, or BattleLab restarted); resolve the "
    "link again"
)
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


@dataclass
class Operation:
    id: str
    server: str
    kind: str
    state: str = "running"  # running | done | failed
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: dict | None = None
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    # ---- a link's resolution (kind ``resolve``, #2353) only
    #: The link this id is bound to: the same id with another link is refused.
    link: str = ""
    #: The server id a registration from this resolution takes (its review is staged under it).
    reserved: str = ""
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    #: What has been read so far, for the page's progress rows.
    progress: dict = field(default_factory=dict)
    #: What was read before a refusal (the repository, its commit, the manifests found).
    details: dict | None = None
    #: The server-held evidence ``{id, source, origin}``: never shown, never taken from a client.
    bound: dict | None = field(default=None, repr=False)
    consumed: bool = False

    def report(self, step: str, **facts: object) -> None:
        self.progress = {**self.progress, **facts, "step": step}
        self.updated_at = time.time()

    def view(self) -> dict:
        out = {
            "id": self.id,
            "server": self.server or None,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }
        if self.kind == "resolve":
            out["progress"] = self.progress
            out["details"] = self.details
        return out


_ops: dict[str, Operation] = {}


def request_id(value: object) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise RegistryError("request_id must be a UUID")
    return value


def get(op_id: object) -> Operation:
    op = _ops.get(request_id(op_id))
    if op is None:
        raise NotFound("no such operation (it may predate an app restart; start again)")
    return op


def start(sid: str, kind: str, op_id: object, fn: Callable[[], dict]) -> Operation:
    """Start ``fn`` (blocking; run in a thread) as operation ``op_id`` for server ``sid``, or
    return the operation already started under that id. Must run on the event loop."""
    if kind not in KINDS or kind == "resolve":
        raise ValueError(kind)
    op_id = request_id(op_id)
    existing = _ops.get(op_id)
    if existing is not None:
        if (existing.server, existing.kind) != (sid, kind):
            raise Conflict("that request_id belongs to another operation")
        return existing
    if any(o.server == sid and o.state == "running" for o in _ops.values()):
        raise Conflict("this server is being reviewed or installed")
    op = Operation(id=op_id, server=sid, kind=kind)
    _ops[op_id] = op
    op.task = asyncio.get_running_loop().create_task(_run(op, fn))
    _trim()
    return op


def start_resolve(op_id: object, link: str, fn: Callable[[Operation], dict]) -> Operation:
    """Start resolving ``link`` as operation ``op_id`` (no server), or return the resolution
    already started under that id for the SAME link. Must run on the event loop."""
    op_id = request_id(op_id)
    existing = _ops.get(op_id)
    if existing is not None:
        if existing.kind != "resolve" or existing.link != link:
            raise Conflict(
                "that request_id belongs to another operation or link; resolve with a new one"
            )
        return existing
    op = Operation(id=op_id, server="", kind="resolve", link=link, reserved=secrets.token_hex(8))
    _ops[op_id] = op
    op.task = asyncio.get_running_loop().create_task(_run(op, lambda: fn(op)))
    _trim()
    return op


def stop(op_id: object) -> Operation:
    """Stop a running resolution: its fetch is killed and it fails as ``stopped``."""
    op = get(op_id)
    if op.kind != "resolve":
        raise RegistryError("only reading a link can be stopped")
    op.cancel.set()
    return op


def resolution(op_id: object) -> dict:
    """The server-held ``{id, source, origin}`` of a finished, fresh, unused resolution. Raises
    `NotFound` (resolve again) when it is unknown, expired or from before a restart."""
    if not isinstance(op_id, str) or not _UUID.fullmatch(op_id):
        raise RegistryError("resolution_id must be the id of a link's resolution")
    op = _ops.get(op_id)
    if op is None or op.kind != "resolve":
        raise NotFound(GONE)
    if op.state == "running":
        raise Conflict("the link is still being read")
    if op.state != "done" or op.bound is None:
        raise Conflict("that link was refused; nothing can be registered from it")
    if time.time() - op.updated_at > RESOLUTION_TTL_S:
        raise NotFound(GONE)
    if op.consumed:
        raise Conflict("a server was already registered from this resolution; resolve again")
    return dict(op.bound)


def consume(op_id: str) -> None:
    op = _ops.get(op_id)
    if op is not None:
        op.consumed = True


async def _run(op: Operation, fn: Callable[[], dict]) -> None:
    try:
        op.result = await asyncio.to_thread(fn)
        op.state = "done"
    except RegistryError as e:  # InstallError included: words for the operator
        op.error, op.state = str(e), "failed"
        op.details = getattr(e, "facts", None)
    except Exception:  # noqa: BLE001 — never lose the operation's end
        log.warning("MCP %s of %s failed unexpectedly", op.kind, op.server or "-", exc_info=True)
        what = "reading the link" if op.kind == "resolve" else f"the {op.kind}"
        op.error, op.state = f"{what} failed unexpectedly", "failed"
    finally:
        op.updated_at = time.time()
        op.task = None


def _trim() -> None:
    finished = sorted(
        (o for o in _ops.values() if o.state != "running"), key=lambda o: o.updated_at
    )
    for o in finished[: max(0, len(finished) - KEEP)]:
        _ops.pop(o.id, None)


def _reset_for_tests() -> None:
    _ops.clear()
