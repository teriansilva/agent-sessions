"""Durable native operation claims in the existing conversation JSONL (#1278).

This is a strict projection of chat_store, not another message store or executor. The worker
claims an immutable request before writing native stdin. A claim alone means uncertain: a
crash between stdin and its journal receipt cannot prove whether the agent received it.
Replays only observe; even a proved not-sent operation needs a new UUID for another attempt.

Callbacks are never reconstructed from these events. Worker ownership, current source/API
admission, original authority and exact live approval checks remain effect-boundary duties.
All transactions are short read/compare/appends and must never include a native RPC.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import math
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import chat_store, native_ipc

MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_LINE_BYTES = native_ipc.MAX_FRAME_BYTES
HANDOFFS = frozenset({"uncertain", "sent", "not_sent"})


class JournalError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise JournalError("unavailable", "invalid native journal record")


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise JournalError("invalid", "native journal requires canonical UUIDs") from None
    return value


def _integer(value):
    if type(value) is not int or not 0 <= value <= native_ipc.MAX_INTEGER:
        raise JournalError("invalid", "native journal requires a nonnegative revision")
    return value


def _timestamp(value):
    if (
        type(value) not in (float, int)
        or not 0 <= value <= native_ipc.MAX_INTEGER
        or not math.isfinite(value)
    ):
        raise JournalError("unavailable", "invalid native journal timestamp")


def _scope(record):
    _uuid(record.get("worker_id"))
    _uuid(record.get("connection_id"))


def _decode(line):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_):
        raise ValueError

    try:
        if len(line) > MAX_LINE_BYTES:
            raise ValueError
        result = json.loads(line.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
        if type(result) is not dict:
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise JournalError("unavailable", "native journal contains an unreadable record") from None


@dataclass
class Operation:
    operation_id: str
    request: dict
    worker_id: str
    connection_id: str
    recorded_revision: int
    handoff: str = "uncertain"
    ever_sent: bool = False

    def receipt(self):
        return {
            "operation_id": self.operation_id,
            "recorded_revision": self.recorded_revision,
            "handoff": self.handoff,
        }


@dataclass
class Journal:
    header: dict
    revision: int = 1
    operations: dict[str, Operation] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    def page(self, after: int, limit: int = 100) -> dict:
        _integer(after)
        if type(limit) is not int or not 1 <= limit <= native_ipc.MAX_EVENTS:
            raise JournalError("invalid", "invalid native event page size")
        if after > self.revision:
            raise JournalError("conflict", "native event cursor is ahead of this journal")
        start = bisect.bisect_right(self.events, after, key=lambda event: event["cursor"])
        selected = [
            {"cursor": event["cursor"], **event["event"]}
            for event in self.events[start : start + limit]
        ]
        return {
            "revision": self.revision,
            "next_cursor": selected[-1]["cursor"] if len(selected) == limit else self.revision,
            "events": selected,
        }

    def copy(self) -> Journal:
        """Cheap continuation copy: events are immutable once folded; operations are not."""
        return Journal(
            self.header,
            self.revision,
            {k: dataclasses.replace(v) for k, v in self.operations.items()},
            list(self.events),
        )


# Folded prefixes, so a transaction parses only the records appended since the last one. A
# cached prefix is reused only when the current bytes up to its length hash identically, so a
# shorter, replaced or rewritten file misses and is folded in full. Previews of unwritten
# records may be cached too: a different committed record cannot hash the same.
_CACHE: OrderedDict[str, tuple[int, bytes, Journal]] = OrderedDict()
_CACHE_SESSIONS = 64


def _prefix_digest(raw: bytes, length: int) -> bytes:
    # The whole prefix, not a tail sample (Hermes on #1278): a same-length rewrite of an
    # earlier claim must miss. Hashing is far cheaper than re-parsing the JSON it guards.
    return hashlib.blake2b(memoryview(raw)[:length], digest_size=32).digest()


def _cached(raw: bytes, session_id: str) -> tuple[Journal, int] | None:
    hit = _CACHE.get(session_id)
    if hit is None:
        return None
    length, digest, journal = hit
    if length > len(raw) or _prefix_digest(raw, length) != digest:
        return None
    _CACHE.move_to_end(session_id)
    return journal.copy(), length


def _remember(raw: bytes, session_id: str, header: bytes, journal: Journal) -> None:
    _CACHE[session_id] = (len(raw), _prefix_digest(raw, len(raw)), journal)
    _CACHE.move_to_end(session_id)
    while len(_CACHE) > _CACHE_SESSIONS:
        _CACHE.popitem(last=False)


def fold(raw: bytes, session_id: str) -> Journal:
    """Unreadable data stays occupied: never drop a malformed claim and replay its effect."""
    _uuid(session_id)
    if type(raw) is not bytes or len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise JournalError("unavailable", "native journal is incomplete or exceeds its bound")
    first = raw.index(b"\n") + 1
    hit = _cached(raw, session_id)
    if hit is not None:
        journal, offset = hit
        lines = raw[offset:].split(b"\n")[:-1]
        if journal.revision + len(lines) > MAX_RECORDS:
            raise JournalError("unavailable", "native journal record bound exceeded")
        _fold_lines(journal, lines, journal.revision + 1)
        _remember(raw, session_id, raw[:first], journal)
        return journal
    lines = raw.split(b"\n")[:-1]
    if not lines or len(lines) > MAX_RECORDS:
        raise JournalError("unavailable", "native journal record bound exceeded")
    header = _decode(lines[0])
    _fields(header, {"type", "id", "cwd", "created_at", "request", "model"})
    if (
        header["type"] != "session"
        or header["id"] != session_id
        or type(header["cwd"]) is not str
        or not header["cwd"]
        or type(header["request"]) is not dict
        or header["request"].get("runtime") != "api"
        or header["model"] is not None
        and type(header["model"]) is not str
    ):
        raise JournalError("unavailable", "native journal has no matching session header")
    # Source/creation ownership lives in native_ownership; private worker configuration and
    # handshake capabilities never belong in the public conversation header.
    _fields(header["request"], {"runtime", "session_key"})
    _timestamp(header["created_at"])
    try:
        session_key = native_ipc.validate_session_key(header["request"].get("session_key"))
    except native_ipc.IPCError:
        raise JournalError(
            "unavailable", "native journal has no qualified session identity"
        ) from None
    if session_key.partition(":")[2] != session_id:
        raise JournalError("unavailable", "native journal session identity changed")
    journal = Journal(header)
    _fold_lines(journal, lines[1:], 2)
    _remember(raw, session_id, raw[:first], journal)
    return journal


def _fold_lines(journal: Journal, lines: list[bytes], first_cursor: int) -> None:
    for cursor, line in enumerate(lines, first_cursor):
        record = _decode(line)
        _scope(record)
        _timestamp(record.get("ts"))
        kind = record.get("type")
        try:
            if kind == "native_operation":
                _fields(
                    record, {"type", "operation_id", "request", "worker_id", "connection_id", "ts"}
                )
                operation_id = _uuid(record["operation_id"])
                request = native_ipc.normalize_immutable_request(record["request"])
                if operation_id in journal.operations:
                    raise JournalError("unavailable", "duplicate native operation claim")
                journal.operations[operation_id] = Operation(
                    operation_id, request, record["worker_id"], record["connection_id"], cursor
                )
            elif kind == "native_handoff":
                _fields(
                    record, {"type", "operation_id", "handoff", "worker_id", "connection_id", "ts"}
                )
                operation = journal.operations.get(_uuid(record["operation_id"]))
                if operation is None or (operation.worker_id, operation.connection_id) != (
                    record["worker_id"],
                    record["connection_id"],
                ):
                    raise JournalError("unavailable", "native handoff has no matching claim")
                _transition(operation, record["handoff"])
                operation.handoff = record["handoff"]
                operation.ever_sent |= record["handoff"] == "sent"
                operation.recorded_revision = cursor
            elif kind == "native_event":
                _fields(record, {"type", "event", "worker_id", "connection_id", "ts"})
                event = native_ipc.normalize_event(record["event"])
                operation_id = event["data"].get("operation_id")
                if operation_id:
                    operation = journal.operations.get(operation_id)
                    if (
                        operation is None
                        or operation.request["action"] != "submit"
                        or (operation.worker_id, operation.connection_id)
                        != (record["worker_id"], record["connection_id"])
                    ):
                        raise JournalError("unavailable", "native event has no matching operation")
                if event["kind"] == "approval" and (
                    event["data"]["worker_id"],
                    event["data"]["connection_id"],
                ) != (record["worker_id"], record["connection_id"]):
                    raise JournalError("unavailable", "native approval generation changed")
                journal.events.append(
                    {
                        "cursor": cursor,
                        "worker_id": record["worker_id"],
                        "connection_id": record["connection_id"],
                        "event": event,
                    }
                )
            else:
                raise JournalError("unavailable", "unsupported native journal record")
        except native_ipc.IPCError:
            raise JournalError("unavailable", "invalid native journal request or event") from None
        journal.revision = cursor


def _transition(operation, current):
    if type(current) is not str or current not in HANDOFFS:
        raise JournalError("invalid", "invalid native handoff state")
    # A proved non-send is final. Once sent, uncertainty never becomes a claim of non-send.
    if (
        operation.handoff == "not_sent"
        and current != "not_sent"
        or operation.ever_sent
        and current == "not_sent"
    ):
        raise JournalError("conflict", "native handoff evidence cannot be reversed")


def _transaction(root, session_id, update):
    try:
        return chat_store.transaction(root, session_id, update, max_bytes=MAX_BYTES)
    except JournalError:
        raise
    except (OSError, ValueError):
        raise JournalError("unavailable", "native journal is unavailable") from None


def read(root: Path, session_id: str) -> Journal:
    """Observe under a SHARED lock: readers neither serialize each other nor fsync."""
    try:
        raw = chat_store.read_shared(root, session_id, max_bytes=MAX_BYTES)
    except (OSError, ValueError):
        raise JournalError("unavailable", "native journal is unavailable") from None
    return fold(raw, session_id)


def _target(journal, session_key):
    if journal.header["request"]["session_key"] != session_key:
        raise JournalError("conflict", "native journal belongs to another client")


def claim(root: Path, request: dict) -> tuple[bool, dict]:
    """Return (newly_claimed, receipt), fsynced before a caller may write native stdin.

    An exact replay precedes revision comparison and does not authorize another effect.
    Transport request IDs, browser reconnects and the caller's current worker identity do not
    change replay identity. The worker must separately refuse NEW work from an old generation.
    """
    checked = native_ipc.validate_request(request)
    immutable = native_ipc.immutable_request(checked)
    session_id = checked["session_key"].partition(":")[2]
    params = checked["params"]
    operation_id = params["operation_id"]

    def update(raw):
        journal = fold(raw, session_id)
        _target(journal, checked["session_key"])
        previous = journal.operations.get(operation_id)
        if previous is not None:
            if previous.request != immutable:
                raise JournalError(
                    "conflict", "native operation UUID already binds another request"
                )
            return [], (False, previous.receipt())
        if params["expected_revision"] != journal.revision:
            raise JournalError("conflict", "native conversation revision changed")
        record = {
            "type": "native_operation",
            "operation_id": operation_id,
            "request": immutable,
            "worker_id": checked["worker_id"],
            "connection_id": checked["connection_id"],
            "ts": time.time(),
        }
        after = _preview(raw, [record], session_id)
        return [record], (True, after.operations[operation_id].receipt())

    return _transaction(root, session_id, update)


def _preview(raw, records, session_id):
    return fold(raw + chat_store._records_bytes(records), session_id)


def record_handoff(
    root: Path, binding: native_ipc.Binding, operation_id: str, handoff: str
) -> dict:
    _uuid(operation_id)
    session_id = binding.session_key.partition(":")[2]

    def update(raw):
        journal = fold(raw, session_id)
        _target(journal, binding.session_key)
        operation = journal.operations.get(operation_id)
        if operation is None or (operation.worker_id, operation.connection_id) != (
            binding.worker_id,
            binding.connection_id,
        ):
            raise JournalError("conflict", "native handoff requires its original worker connection")
        _transition(operation, handoff)
        if operation.handoff == handoff:
            return [], operation.receipt()
        record = {
            "type": "native_handoff",
            "operation_id": operation_id,
            "handoff": handoff,
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "ts": time.time(),
        }
        after = _preview(raw, [record], session_id)
        return [record], after.operations[operation_id].receipt()

    return _transaction(root, session_id, update)


def append_events(root: Path, binding: native_ipc.Binding, events: list[dict]) -> int:
    """Persist normalized observations; private native 'send' frames cannot enter the journal."""
    if type(events) is not list or not 1 <= len(events) <= native_ipc.MAX_EVENTS:
        raise JournalError("invalid", "invalid native event batch")
    checked = [native_ipc.normalize_event(event) for event in events]
    session_id = binding.session_key.partition(":")[2]
    records = [
        {
            "type": "native_event",
            "worker_id": binding.worker_id,
            "connection_id": binding.connection_id,
            "event": event,
            "ts": time.time(),
        }
        for event in checked
    ]

    def update(raw):
        after = _preview(raw, records, session_id)
        _target(after, binding.session_key)
        return records, after.revision

    return _transaction(root, session_id, update)
