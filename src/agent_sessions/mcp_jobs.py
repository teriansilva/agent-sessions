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
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .mcp_registry import Conflict, NotFound, RegistryError

log = logging.getLogger(__name__)

KINDS = ("review", "install")
KEEP = 64  # finished operations remembered (newest), for a client that polls late
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

    def view(self) -> dict:
        return {
            "id": self.id,
            "server": self.server,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


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
    if kind not in KINDS:
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


async def _run(op: Operation, fn: Callable[[], dict]) -> None:
    try:
        op.result = await asyncio.to_thread(fn)
        op.state = "done"
    except RegistryError as e:  # InstallError included: words for the operator
        op.error, op.state = str(e), "failed"
    except Exception:  # noqa: BLE001 — never lose the operation's end
        log.warning("MCP %s of %s failed unexpectedly", op.kind, op.server, exc_info=True)
        op.error, op.state = f"the {op.kind} failed unexpectedly", "failed"
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
