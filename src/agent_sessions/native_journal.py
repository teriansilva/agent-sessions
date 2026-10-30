"""Durable native operation claims in the existing conversation JSONL (#1278).

This is a strict projection of chat_store, not another message store or executor. The worker
claims an immutable request before writing native stdin. An immediate claim means uncertain:
a crash between stdin and its journal receipt cannot prove whether the agent received it.
Queued operator turns explicitly remain unsent until a durable transition to uncertain.
Replays only observe; even a proved not-sent operation needs a new UUID for another attempt.

Callbacks are never reconstructed from these events. Worker ownership, current source/API
admission, original authority and exact live approval checks remain effect-boundary duties.
All transactions are short read/compare/appends and must never include a native RPC.
Folded events retain recorded timestamps for response display; public cursors stay record-based.

The journal is a chain of segments (#1733, `native_segments`). Before an append would take the
active segment past `SOFT_BYTES` / `SOFT_RECORDS`, the transaction rolls it over in place: the
successor's header carries only the LIVE control state (unresolved claims and handoffs, the
in-flight turn's start, open approvals, the live generation's background reports, an open
question) — never turn content — and the frozen segment gets an immutable `.ops` sidecar that
answers operation-id replay without folding it again. Transactions fold only the active segment
(plus its carry); `read` folds the whole chain, so every consumer sees one continuous history
with monotonic cursors. A rollover that cannot be written is ``unwritable``: nothing was
appended, so before a claim it is a known refusal.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import sys
import threading
import time
import types
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import chat_store, native_events, native_ipc, native_protocol, native_segments

#: One segment's hard bounds. A session's journal is a chain of segments (#1733), unbounded.
MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 100_000
#: An append that would take the active segment past either soft trigger rolls it over first.
#: Records matter as much as bytes: claude-api journals ~7 records per tool call.
SOFT_BYTES = 56 * 1024 * 1024
SOFT_RECORDS = 90_000
#: Defensive ceiling of what a successor may carry. The carry is only what is unresolved at the
#: rollover (the queue — itself up to 4 MiB of requests —, approvals, one in-flight turn, live
#: background tasks), never history; exceeding this means that assumption broke: fail visibly.
CARRY_MAX_BYTES = 8 * 1024 * 1024
CARRY_MAX_RECORDS = 2_000
#: Pre-claim admission (`prepare`) rolls over this much before a soft trigger, so the claim that
#: follows never has to.
ADMIT_BYTES = 64 * 1024
ADMIT_RECORDS = 16
MAX_LINE_BYTES = native_ipc.MAX_FRAME_BYTES
HANDOFFS = frozenset({"queued", "uncertain", "sent", "not_sent"})
#: Why a message was settled ``not_sent`` (#1538); a record without one reads as ``unexpected``.
REASON_CODES = native_ipc.REASON_CODES
#: Send again's operation ids (#1538) are derived, never minted: every tab, reload and click that
#: resends one not-sent message submits the SAME id, so operation-id idempotency delivers it once.
RETRY_NAMESPACE = uuid.UUID("6b0f7f3e-1538-5a3d-9c1e-5e4d2a7b9c10")


def retry_id(operation_id: str) -> str:
    """The one operation id Send again may use for ``operation_id`` (#1538)."""
    return str(uuid.uuid5(RETRY_NAMESPACE, f"{_uuid(operation_id)}:send-again"))


def _reason(handoff, code) -> bool:
    """A reason is a closed code, on a ``not_sent`` only. The type first: a list or dict is
    unhashable, and an unreadable record must be a `JournalError`, never a TypeError."""
    return handoff == "not_sent" and type(code) is str and code in REASON_CODES


#: A claim's expected revision is no longer the journal's (`claim`).
REVISION_CHANGED = "native conversation revision changed"
MAX_QUEUED = 32
MAX_QUEUE_BYTES = 4 * 1024 * 1024


class JournalError(ValueError):
    """``code``: ``invalid``, ``conflict``, ``busy``, ``unavailable``, or ``unwritable`` (#1733:
    the chain could not be extended and NOTHING was appended — a known refusal before a claim).
    ``cause``: the I/O error class (`native_segments.CAUSES`) when a write failed."""

    def __init__(self, code: str, detail: str, cause: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.cause = cause


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
    ts: float  # Original claim time; later handoffs only advance recorded_revision.
    handoff: str = "uncertain"
    ever_sent: bool = False
    # Where the claim sits in the journal; never advanced. Orders it among the agent's own turns
    # (#1438) by the journal itself, not a wall clock that can step backwards.
    claimed_revision: int = 0
    # Where it left for the agent: the claim itself, or a queued message's first handoff out of
    # the queue. `None` while queued, and forever for one settled `not_sent` from the queue. An
    # async question's answer is the first message dispatched after it (#1463).
    dispatched_revision: int | None = None
    # Claimed as `native_queued` (#1378): an operator message that waited behind a busy turn.
    queued: bool = False
    # Why a `not_sent` was settled (#1538), from `REASON_CODES`; None while it is not `not_sent`.
    reason_code: str | None = None
    # #1733: the cursors of its claim and every handoff (what a successor carries verbatim), the
    # cursor of its latest `turn_started`, and whether its turn ended (completed, refused or
    # steered in) — a sent operation whose turn ended is settled and stays behind.
    records: tuple[int, ...] = ()
    started: int | None = None
    turn_settled: bool = False

    def receipt(self):
        out = {
            "operation_id": self.operation_id,
            "recorded_revision": self.recorded_revision,
            "handoff": self.handoff,
        }
        if self.handoff == "not_sent":
            out["reason_code"] = self.reason_code or "unexpected"
        return out


@dataclass
class _Live:
    """What a rollover must carry that is not an operation (#1733), tracked while folding."""

    # The generation that wrote the newest claim or event: only it can still write.
    writer: str | None = None
    # Open approval requests: (worker, native request id, operation) -> cursor.
    approvals: dict[tuple[str, str, str], int] = field(default_factory=dict)
    # The newest background-reporting generation and its latest report per task / own turn.
    background_worker: str | None = None
    background: dict[str, int] = field(default_factory=dict)

    def copy(self) -> _Live:
        return _Live(
            self.writer, dict(self.approvals), self.background_worker, dict(self.background)
        )


@dataclass
class Journal:
    header: dict
    revision: int = 1
    operations: dict[str, Operation] = field(default_factory=dict)
    events: native_events.Events = field(default_factory=native_events.Events)
    # #1733: the segment this fold ends in and the cursor just before its first record.
    segment: int = 0
    base: int = 0
    live: _Live = field(default_factory=_Live)

    @property
    def lines(self) -> int:
        """Lines in the segment this fold ends in (segment n ≥ 1 opens with its header)."""
        return self.revision - self.base + (1 if self.segment else 0)

    def page(self, after: int, limit: int = 100) -> dict:
        _integer(after)
        if type(limit) is not int or not 1 <= limit <= native_ipc.MAX_EVENTS:
            raise JournalError("invalid", "invalid native event page size")
        if after > self.revision:
            raise JournalError("conflict", "native event cursor is ahead of this journal")
        start = bisect.bisect_right(self.events.cursors, after)
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
            self.events.copy(),
            self.segment,
            self.base,
            self.live.copy(),
        )


# Folded prefixes, so a transaction parses only the records appended since the last one. A
# cached prefix is reused only when the current bytes up to its length hash identically, so a
# shorter, replaced or rewritten file misses and is folded in full. Previews of unwritten
# records may be cached too: a different committed record cannot hash the same.
@dataclass
class _Entry:
    length: int
    digest: bytes
    journal: Journal
    memory_bytes: int
    stamp: tuple | None = None


_CACHE: OrderedDict[tuple[str, int], _Entry] = OrderedDict()
_CACHE_SESSIONS = 64
_CACHE_BYTES = 32 * 1024 * 1024
_CACHE_DISK_BYTES = 128 * 1024 * 1024
_CACHE_LOCK = threading.RLock()


def _size(value, seen=None):
    """Count retained operation graphs, including prompts/attachments, once per object."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(_size(k, seen) + _size(v, seen) for k, v in value.items())
    elif isinstance(value, tuple | list):
        size += sum(_size(v, seen) for v in value)
    elif isinstance(value, Operation):
        size += _size(value.__dict__, seen)
    return size


def _trim():
    while _CACHE and (
        len(_CACHE) > _CACHE_SESSIONS
        or sum(e.memory_bytes for e in _CACHE.values()) > _CACHE_BYTES
        or sum(e.journal.events.disk_bytes for e in _CACHE.values()) > _CACHE_DISK_BYTES
    ):
        _CACHE.popitem(last=False)


def _prefix_hasher(raw: bytes, length: int):
    # The whole prefix, not a tail sample (Hermes on #1278): a same-length rewrite of an
    # earlier claim must miss. Hashing is far cheaper than re-parsing the JSON it guards.
    return hashlib.blake2b(memoryview(raw)[:length], digest_size=32)


def _prefix_digest(raw: bytes, length: int) -> bytes:
    return _prefix_hasher(raw, length).digest()


def _cached(raw: bytes, key) -> tuple[Journal, int, object] | None:
    """The cached fold of a verified prefix of ``raw``, its length, and the hash state after
    exactly that prefix (so the new length's digest extends it rather than hashing it again)."""
    with _CACHE_LOCK:
        _trim()
        hit = _CACHE.get(key)
        if hit is None or hit.length > len(raw):
            return None
        hasher = _prefix_hasher(raw, hit.length)
        if hasher.digest() != hit.digest:
            return None
        _CACHE.move_to_end(key)
        return hit.journal.copy(), hit.length, hasher


def _weight(journal: Journal) -> int:
    return _size(journal.operations) + _size(journal.header) + journal.events.memory_bytes + 1024


def _extended(raw: bytes, prefix=None):
    """``prefix``: ``(length, hash state)`` of a prefix of ``raw`` this call verified. Its state
    extended by the rest of ``raw`` is exactly the full digest: blake2b is a stream hash (#1727:
    a second pass over a 31 MiB journal cost ~60 ms of every observation after an append)."""
    if prefix is None:
        return _prefix_hasher(raw, len(raw))
    length, verified = prefix
    hasher = verified.copy()
    hasher.update(memoryview(raw)[length:])
    return hasher


def _remember(raw: bytes, key, journal: Journal, prefix=None) -> None:
    entry = _Entry(len(raw), _extended(raw, prefix).digest(), journal, _weight(journal))
    with _CACHE_LOCK:
        _CACHE[key] = entry
        _CACHE.move_to_end(key)
        _trim()


def fold(raw: bytes, session_id: str, *, directory: Path | None = None) -> Journal:
    """Fold a single-segment journal (segment 0) from its complete bytes."""
    return _fold_segment(raw, session_id, 0, directory=directory)


def _fold_segment(
    raw: bytes,
    session_id: str,
    index: int,
    *,
    directory: Path | None,
    bounded: bool = True,
    remember: bool = True,
    prior: Callable[[str], dict | None] | None = None,
) -> Journal:
    """Fold one segment: segment 0 from its session header, segment n from its carried state.

    ``bounded=False`` previews unwritten records: the transaction checks the bounds itself, and
    may roll over first (#1733), so a preview never refuses an append that would not happen.
    ``prior(operation_id)``: an operation settled in an EARLIER segment, from its sidecar, for an
    event that still names it (a segment n ≥ 1 does not carry settled operations)."""
    try:
        return _fold_once(raw, session_id, index, directory, bounded, remember, prior)
    except native_events.SpoolFull:
        # A rejected preview may leave unreachable derived bytes; never fail a valid
        # operation because of them. Rebuild once, from the full authoritative source.
        with _CACHE_LOCK:
            _CACHE.pop((session_id, index), None)
        return _fold_once(raw, session_id, index, directory, bounded, remember, prior)


def _fold_once(raw, session_id, index, directory, bounded, remember, prior) -> Journal:
    """Unreadable data stays occupied: never drop a malformed claim and replay its effect."""
    _uuid(session_id)
    if type(raw) is not bytes or bounded and len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise JournalError("unavailable", "native journal is incomplete or exceeds its bound")
    key = (session_id, index)
    hit = _cached(raw, key) if remember else None
    if hit is not None:
        journal, offset, hasher = hit
        lines = raw[offset:].split(b"\n")[:-1]
        if bounded and journal.lines + len(lines) > MAX_RECORDS:
            raise JournalError("unavailable", "native journal record bound exceeded")
        _fold_lines(journal, lines, journal.revision + 1, prior)
        _remember(raw, key, journal, (offset, hasher))
        return journal
    lines = raw.split(b"\n")[:-1]
    if not lines or bounded and len(lines) > MAX_RECORDS:
        raise JournalError("unavailable", "native journal record bound exceeded")
    if index == 0:
        journal = Journal(_session_header(_decode(lines[0]), session_id))
        journal.events = native_events.Events(directory)
        _fold_lines(journal, lines[1:], 2)
    else:
        journal = _open_segment(_segment_header(lines[0], index), session_id, directory)
        _fold_lines(journal, lines[1:], journal.base + 1, prior)
    if remember:
        _remember(raw, key, journal)
    return journal


def _session_header(header, session_id: str) -> dict:
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
    return header


def _segment_header(line: bytes, index: int) -> dict:
    try:
        return native_segments.check_header(
            native_segments.decode(line, native_segments.HEADER_MAX_BYTES), index
        )
    except ValueError:
        raise JournalError("unavailable", "native journal segment header is invalid") from None


def _open_segment(header: dict, session_id: str, directory: Path | None) -> Journal:
    """A segment's fold before its own records: the session, and the carried live state at its
    ORIGINAL cursors (so every claim, handoff and approval folds exactly as it did)."""
    carried = header["carried"]
    _fields(carried, {"session", "records", "last_question", "writer"})
    base = header["base_revision"]
    journal = Journal(_session_header(carried["session"], session_id), segment=header["index"])
    journal.base = base
    journal.events = native_events.Events(directory)
    records = carried["records"]
    if type(records) is not list or len(records) > CARRY_MAX_RECORDS:
        raise JournalError("unavailable", "native journal carry is invalid")
    previous = 0
    for item in records:
        if (
            type(item) is not list
            or len(item) != 2
            or type(item[0]) is not int
            or not previous < item[0] <= base
            or type(item[1]) is not dict
        ):
            raise JournalError("unavailable", "native journal carry is invalid")
        _fold_record(journal, item[1], item[0])
        previous = item[0]
    question = carried["last_question"]
    if question is not None:
        if (
            type(question) is not list
            or len(question) != 2
            or type(question[0]) is not int
            or not 0 < question[0] <= base
            or type(question[1]) is not str
            or not question[1]
        ):
            raise JournalError("unavailable", "native journal carry is invalid")
        journal.events.last_question = (question[0], question[1])
    if carried["writer"] is not None:
        _uuid(carried["writer"])
    journal.live.writer = carried["writer"]
    journal.revision = base
    return journal


def _fold_lines(journal: Journal, lines: list[bytes], first_cursor: int, prior=None) -> None:
    for cursor, line in enumerate(lines, first_cursor):
        _fold_record(journal, _decode(line), cursor, prior)


def _fold_record(journal: Journal, record: dict, cursor: int, prior=None) -> None:
    _scope(record)
    _timestamp(record.get("ts"))
    kind = record.get("type")
    try:
        if kind in {"native_operation", "native_queued"}:
            _fields(record, {"type", "operation_id", "request", "worker_id", "connection_id", "ts"})
            operation_id = _uuid(record["operation_id"])
            request = native_ipc.normalize_immutable_request(record["request"])
            if kind == "native_queued" and request["action"] != "submit":
                raise JournalError("unavailable", "only operator turns can be queued")
            if operation_id in journal.operations:
                raise JournalError("unavailable", "duplicate native operation claim")
            journal.operations[operation_id] = Operation(
                operation_id,
                request,
                record["worker_id"],
                record["connection_id"],
                cursor,
                ts=record["ts"],
                handoff="queued" if kind == "native_queued" else "uncertain",
                claimed_revision=cursor,
                dispatched_revision=None if kind == "native_queued" else cursor,
                queued=kind == "native_queued",
                records=(cursor,),
            )
            journal.live.writer = record["worker_id"]
        elif kind == "native_handoff":
            handoff_fields = {"type", "operation_id", "handoff", "worker_id", "connection_id"}
            _fields(
                record,
                {*handoff_fields, "ts", "reason_code"}
                if "reason_code" in record
                else {*handoff_fields, "ts"},
            )
            reason = record.get("reason_code")
            if "reason_code" in record and not _reason(record["handoff"], reason):
                raise JournalError("unavailable", "invalid native handoff reason")
            operation = journal.operations.get(_uuid(record["operation_id"]))
            if operation is None or (operation.worker_id, operation.connection_id) != (
                record["worker_id"],
                record["connection_id"],
            ):
                raise JournalError("unavailable", "native handoff has no matching claim")
            _transition(operation, record["handoff"])
            if operation.dispatched_revision is None and record["handoff"] in {
                "uncertain",
                "sent",
            }:
                operation.dispatched_revision = cursor
            if record["handoff"] == "not_sent" and operation.handoff != "not_sent":
                operation.reason_code = record.get("reason_code")
            operation.handoff = record["handoff"]
            operation.ever_sent |= record["handoff"] == "sent"
            operation.recorded_revision = cursor
            operation.records = (*operation.records, cursor)
        elif kind == "native_event":
            _fields(record, {"type", "event", "worker_id", "connection_id", "ts"})
            event = native_ipc.normalize_event(record["event"])
            data = event["data"]
            operation_id = data.get("operation_id")
            operation = None
            if operation_id:
                operation = journal.operations.get(operation_id)
                if operation is None and prior is not None:
                    # Settled in an earlier segment and not carried (#1733): the same binding
                    # rule, checked against that segment's validated sidecar.
                    settled = prior(operation_id)
                    if (
                        settled is None
                        or settled["action"] != "submit"
                        or (settled["worker_id"], settled["connection_id"])
                        != (record["worker_id"], record["connection_id"])
                    ):
                        raise JournalError("unavailable", "native event has no matching operation")
                elif (
                    operation is None
                    or operation.request["action"] != "submit"
                    or (operation.worker_id, operation.connection_id)
                    != (record["worker_id"], record["connection_id"])
                ):
                    raise JournalError("unavailable", "native event has no matching operation")
            if event["kind"] == "approval" and (
                data["worker_id"],
                data["connection_id"],
            ) != (record["worker_id"], record["connection_id"]):
                raise JournalError("unavailable", "native approval generation changed")
            journal.events.append(
                {
                    "cursor": cursor,
                    "worker_id": record["worker_id"],
                    "connection_id": record["connection_id"],
                    "ts": record["ts"],
                    "event": event,
                }
            )
            _observe_live(journal.live, operation, record["worker_id"], event, cursor)
        else:
            raise JournalError("unavailable", "unsupported native journal record")
    except native_ipc.IPCError:
        raise JournalError("unavailable", "invalid native journal request or event") from None
    journal.revision = cursor


def _observe_live(live: _Live, operation, worker_id: str, event: dict, cursor: int) -> None:
    """Track what a rollover must carry (#1733). Never changes what the fold accepts."""
    kind, data = event["kind"], event["data"]
    live.writer = worker_id
    if operation is not None:
        if kind == "turn_started":
            operation.started = cursor
        elif kind in {"turn_completed", "input_accepted"} or (
            kind == "error" and data.get("action") in {"turn/start", "turn/steer"}
        ):
            operation.turn_settled = True
            if kind == "turn_completed":
                for key in [k for k in live.approvals if k[2] == operation.operation_id]:
                    live.approvals.pop(key)
    if kind == "approval" and operation is not None:
        live.approvals[(worker_id, data["request_id"], operation.operation_id)] = cursor
    elif kind == "approval_cancelled":
        for key in [k for k in live.approvals if k[0] == worker_id and k[1] == data["request_id"]]:
            live.approvals.pop(key)
    elif kind == "background":
        if live.background_worker != worker_id:
            live.background_worker, live.background = worker_id, {}
        live.background["~state"] = cursor
        if "background_turn" in data:
            live.background["~turn"] = cursor
        task = data.get("task_id")
        if task:
            if data.get("task_state", data["state"]) in native_protocol.TERMINAL_TASK_STATES:
                live.background.pop(f"task:{task}", None)
            else:
                live.background[f"task:{task}"] = cursor


def _transition(operation, current):
    if type(current) is not str or current not in HANDOFFS:
        raise JournalError("invalid", "invalid native handoff state")
    # A proved non-send is final. Once sent, uncertainty never becomes a claim of non-send.
    if (
        current == "queued"
        and operation.handoff != "queued"
        or operation.handoff == "queued"
        and current == "sent"
        or operation.handoff == "not_sent"
        and current != "not_sent"
        or operation.ever_sent
        and current == "not_sent"
    ):
        raise JournalError("conflict", "native handoff evidence cannot be reversed")


# --- the whole chain, for every reader --------------------------------------------------------


@dataclass
class _ChainEntry:
    frozen: tuple
    index: int
    length: int
    digest: bytes
    journal: Journal
    stamp: tuple | None = None


#: Folds of whole chains (#1733), separate from the per-segment transaction cache. The most
#: recently used chain is always kept, however large: evicting it would refold every segment on
#: every read of the busiest session.
_CHAIN: OrderedDict[str, _ChainEntry] = OrderedDict()
_CHAIN_SESSIONS = 8
_CHAIN_DISK_BYTES = 1024 * 1024 * 1024


def _trim_chain() -> None:
    while len(_CHAIN) > 1 and (
        len(_CHAIN) > _CHAIN_SESSIONS
        or sum(e.journal.events.disk_bytes for e in _CHAIN.values()) > _CHAIN_DISK_BYTES
    ):
        _CHAIN.popitem(last=False)


def _guarded(action):
    """Run a journal action, mapping storage failures onto `JournalError`."""
    try:
        return action()
    except JournalError:
        raise
    except native_segments.Unwritable as exc:
        raise JournalError(
            "unwritable", f"the session's record could not be written: {exc.detail}", exc.cause
        ) from None
    except OSError as exc:
        raise JournalError(
            "unavailable", "native journal is unavailable", native_segments.cause_of(exc)
        ) from None
    except ValueError:
        raise JournalError("unavailable", "native journal is unavailable") from None


class _NeedFrozen(Exception):
    """The chain read needs a fold of its frozen segments that is not cached: it is built
    OUTSIDE the journal lock (frozen segments are immutable), then the read is retried."""

    def __init__(self, segments: tuple, previous):
        super().__init__("frozen segments are not folded yet")
        self.segments = segments
        self.previous = previous


#: Folds of a chain's FROZEN segments, keyed by their exact stamps (#1733). Built outside the
#: journal lock; a read under the lock only copies one and folds the active segment on top.
_FROZEN: OrderedDict[tuple, Journal] = OrderedDict()
_FROZEN_SESSIONS = 4


def _prewarm(root: Path, session_id: str) -> list | None:
    """Before taking the journal lock: verify the frozen segments' pins (hashing them, memoized
    by stamp) without holding it. Best effort: the resolution under the lock decides."""
    try:
        if not os.path.lexists(native_segments.segment_path(root, session_id, 1)):
            return None  # never rolled over: nothing frozen to hash
        return native_segments.resolve(root, session_id)
    except (OSError, ValueError):
        return None


def observe(root: Path, session_id: str, use: Callable):
    """``use(view, load)`` under the chain's SHARED lock, after its private-file checks and fsync;
    ``load()`` folds the whole chain (`read`). Appends hold the lock exclusively, so this never
    observes a partial transaction; a writer that died between write and sync must not have its
    bytes observed (and acted on) before they are durable, so it still crosses fsync.

    Frozen segments are never read under the lock (#1733, review of #1771): a fold that needs
    them is built outside it and the observation is retried, so an append never waits behind a
    long chain's refold."""
    _uuid(session_id)

    def run():
        _prewarm(root, session_id)
        for _ in range(4):
            try:
                with native_segments.locked(root, session_id, exclusive=False) as view:
                    st = view.st
                    if st.st_size > MAX_BYTES:
                        raise ValueError("conversation is not a private bounded journal")
                    os.fsync(view.fd)
                    result = use(view, lambda: _load(view, root, session_id))
                    after = os.fstat(view.fd)
                    if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ValueError("conversation changed while reading")
                    return result
            except _NeedFrozen as need:
                _fold_frozen(root, session_id, need.segments, need.previous)
        raise ValueError("the native journal kept rolling over while it was read")

    return _guarded(run)


def read(root: Path, session_id: str) -> Journal:
    """The whole chain's fold. Unchanged observations need no journal byte read.

    This shortcut never runs in a mutation: claims always verify full prefix content.
    ctime detects same-length rewrites even when their previous mtime is restored.
    """
    return observe(root, session_id, lambda view, load: load())


_FIRST: OrderedDict[tuple, tuple[dict, str]] = OrderedDict()


def first_facts(view: native_segments.View, load: Callable) -> tuple[dict, str]:
    """(session header, first operator message) for a discovery row (#1733).

    A journal that never rolled over is folded as before (that fold validates it). In a chain the
    header and first submit are read from the START of the segments — segment 0, or the first
    later one holding a submit when the record rolled over before the first send — never the
    whole history; a found message is cached by segment 0's exact stamp (it never changes)."""
    if len(view.chain) == 1:
        journal = load()
        first = next(
            (
                op.request["params"].get("text") or ""
                for op in journal.operations.values()
                if op.request.get("action") == "submit"
            ),
            "",
        )
        return journal.header, first
    segment = view.chain[0]
    key = (view.session_id, *segment.stamp)
    with _CACHE_LOCK:
        hit = _FIRST.get(key)
    if hit is not None:
        return hit
    header, first = None, None
    for index, part in enumerate(view.chain):
        found_header, first = _scan_first_submit(part, view.session_id, index == 0)
        if index == 0:
            header = found_header
        if first is not None:
            break  # the first message: usually in segment 0, after a pre-send rollover later
    if header is None:
        raise JournalError("unavailable", "native journal has no matching session header")
    if first is None:
        return header, ""  # no message yet: never cached, the next one may be the first
    with _CACHE_LOCK:
        # Once written, the first message never changes: keyed by segment 0's exact stamp.
        _FIRST[key] = (header, first)
        while len(_FIRST) > 1024:
            _FIRST.popitem(last=False)
    return header, first


def _scan_first_submit(segment, session_id: str, first_segment: bool):
    """(session header if segment 0, the first submit's text or None), reading from the start."""
    header, first = None, None
    fd = os.open(segment.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (
            segment.stat.st_dev,
            segment.stat.st_ino,
        ):
            raise ValueError("a native journal segment changed while it was read")
        offset, pending, number = 0, b"", 0
        while first is None:
            chunk = os.pread(fd, 1024 * 1024, offset)
            if not chunk:
                break
            offset += len(chunk)
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for line in lines:
                number += 1
                if number == 1:
                    if first_segment:
                        header = _session_header(_decode(line), session_id)
                    continue  # a successor's header: its carry is not a new message
                if b'"native_operation"' not in line and b'"native_queued"' not in line:
                    continue
                record = _decode(line)
                if record.get("type") not in {"native_operation", "native_queued"}:
                    continue
                try:
                    request = native_ipc.normalize_immutable_request(record["request"])
                except (native_ipc.IPCError, KeyError, TypeError):
                    raise JournalError("unavailable", "invalid native journal request") from None
                if request["action"] == "submit":
                    first = request["params"].get("text") or ""
                    break
    finally:
        os.close(fd)
    return header, first


def _load(view: native_segments.View, root: Path, session_id: str) -> Journal:
    st = view.st
    stamp = (*(s.stamp for s in view.chain[:-1]), native_segments.stamp_of(st))
    if len(view.chain) == 1:
        key = (session_id, 0)
        with _CACHE_LOCK:
            _trim()
            hit = _CACHE.get(key)
            if hit is not None and hit.stamp == stamp:
                _CACHE.move_to_end(key)
                return hit.journal.copy()
        raw = os.pread(view.fd, MAX_BYTES + 1, 0)
        if len(raw) != st.st_size:
            raise ValueError("conversation changed while reading")
        journal = fold(raw, session_id, directory=root)
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None and hit.journal is journal:
                hit.stamp = stamp
        return journal
    return _load_chain(view, root, session_id, stamp)


def _load_chain(view, root: Path, session_id: str, stamp: tuple) -> Journal:
    active = view.active
    with _CACHE_LOCK:
        entry = _CHAIN.get(session_id)
        if entry is not None and entry.stamp == stamp:
            _CHAIN.move_to_end(session_id)
            return entry.journal.copy()
    raw = os.pread(view.fd, MAX_BYTES + 1, 0)
    if len(raw) != view.st.st_size or not raw.endswith(b"\n"):
        raise JournalError("unavailable", "native journal is incomplete or exceeds its bound")
    frozen = tuple(s.stamp for s in view.chain[:-1])
    journal, prefix = None, None
    if (
        entry is not None
        and entry.frozen == frozen
        and entry.index == active.index
        and entry.length <= len(raw)
    ):
        hasher = _prefix_hasher(raw, entry.length)
        if hasher.digest() == entry.digest:
            journal = entry.journal.copy()
            lines = raw[entry.length :].split(b"\n")[:-1]
            if journal.lines + len(lines) > MAX_RECORDS:
                raise JournalError("unavailable", "native journal record bound exceeded")
            _fold_lines(journal, lines, journal.revision + 1)
            prefix = (entry.length, hasher)
    if journal is None:
        with _CACHE_LOCK:
            base = _FROZEN.get((session_id, frozen))
            if base is not None:
                _FROZEN.move_to_end((session_id, frozen))
        if base is None:
            # Not under the lock: the caller folds them outside it and asks again.
            raise _NeedFrozen(tuple(view.chain[:-1]), entry)
        journal = base.copy()
        journal.events.allow(native_events.SPOOL_BYTES * (len(view.chain) + 1))
        journal = _fold_segment_into(journal, active, raw, session_id, root, 0)
    fresh = _ChainEntry(frozen, active.index, len(raw), _extended(raw, prefix).digest(), journal)
    fresh.stamp = stamp
    with _CACHE_LOCK:
        _CHAIN[session_id] = fresh
        _CHAIN.move_to_end(session_id)
        _trim_chain()
    return journal


def _fold_segment_into(journal, segment, raw: bytes, session_id: str, root: Path, limit: int):
    """Fold one segment's OWN records onto the chain so far (segment 0 starts it). A successor's
    carried state repeats what its predecessors already folded, so it is checked, not applied."""
    if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
        raise JournalError("unavailable", "native journal is incomplete or exceeds its bound")
    lines = raw.split(b"\n")[:-1]
    if not lines or len(lines) > MAX_RECORDS:
        raise JournalError("unavailable", "native journal record bound exceeded")
    if journal is None:
        if segment.index:
            raise JournalError("unavailable", "native journal chain has no first segment")
        journal = Journal(_session_header(_decode(lines[0]), session_id))
        journal.events = native_events.Events(root, limit=limit)
        _fold_lines(journal, lines[1:], 2)
        return journal
    header = _segment_header(lines[0], segment.index)
    if (
        header["base_revision"] != journal.revision
        or type(header["carried"]) is not dict
        or header["carried"].get("session") != journal.header
    ):
        raise JournalError("unavailable", "native journal segment does not continue its chain")
    journal.segment, journal.base = segment.index, header["base_revision"]
    _fold_lines(journal, lines[1:], journal.base + 1)
    return journal


def _fold_frozen(root: Path, session_id: str, segments: tuple, previous) -> None:
    """Fold a chain's frozen segments OUTSIDE the journal lock. They are immutable: each is read
    exactly as the stamp observed under the lock (whose resolution verified its pin), so the
    result is what a fold under the lock would give. After a rollover the previous fold of this
    session is EXTENDED — the segment it ended in is now frozen — never refolded from the start."""
    key = tuple(segment.stamp for segment in segments)
    with _CACHE_LOCK:
        if (session_id, key) in _FROZEN:
            return
    limit = native_events.SPOOL_BYTES * (len(segments) + 2)
    journal, start = None, 0
    if (
        previous is not None
        and previous.frozen == key[: len(previous.frozen)]
        and previous.index == len(previous.frozen)
        and previous.index < len(segments)
    ):
        ended = segments[previous.index]
        raw = native_segments.read_file(ended.path, ended.stat, MAX_BYTES)
        if previous.length <= len(raw) and raw.endswith(b"\n"):
            hasher = _prefix_hasher(raw, previous.length)
            if hasher.digest() == previous.digest:
                journal = previous.journal.copy()
                journal.events.allow(limit)
                lines = raw[previous.length :].split(b"\n")[:-1]
                if journal.lines + len(lines) > MAX_RECORDS:
                    raise JournalError("unavailable", "native journal record bound exceeded")
                _fold_lines(journal, lines, journal.revision + 1)
                start = previous.index + 1
    if journal is None:
        start = 0
    for segment in segments[start:]:
        raw = native_segments.read_file(segment.path, segment.stat, MAX_BYTES)
        journal = _fold_segment_into(journal, segment, raw, session_id, root, limit)
    with _CACHE_LOCK:
        _FROZEN[(session_id, key)] = journal
        _FROZEN.move_to_end((session_id, key))
        while len(_FROZEN) > _FROZEN_SESSIONS:
            _FROZEN.popitem(last=False)


# --- transactions: the active segment only ----------------------------------------------------


def _request_digest(request: dict) -> str:
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()


class _Active:
    """The active segment inside an exclusive transaction: its exact bytes and fold."""

    def __init__(self, view: native_segments.View, root: Path, session_id: str):
        self.view, self.root, self.session_id = view, root, session_id
        self.index = view.active.index
        st = view.st
        if st.st_size > MAX_BYTES:
            raise ValueError("conversation is not a private bounded journal")
        raw = os.pread(view.fd, MAX_BYTES + 1, 0)
        if len(raw) != st.st_size:
            raise ValueError("conversation changed while reading")
        self.raw = raw
        self._journal: Journal | None = None
        self._lines: int | None = None

    def journal(self) -> Journal:
        if self._journal is None:
            self._journal = _fold_segment(
                self.raw, self.session_id, self.index, directory=self.root, prior=self.used
            )
            self._lines = self._journal.lines
        return self._journal

    def preview(self, records: list[dict]) -> Journal:
        after = _fold_segment(
            self.raw + chat_store._records_bytes(records),
            self.session_id,
            self.index,
            directory=self.root,
            bounded=False,
            prior=self.used,
        )
        self._lines = after.lines - len(records)
        return after

    @property
    def lines(self) -> int:
        """Lines in the active segment, from whichever fold this transaction already made (a
        fold of the bare segment after a preview would miss the cache and refold it all)."""
        if self._lines is None:
            self._lines = self.raw.count(b"\n")
        return self._lines

    def used(self, operation_id: str) -> dict | None:
        """An operation id settled in a frozen segment (#1733): its sidecar entry, newest first.
        The active fold (carry included) is always asked before this."""
        return _settled_before(self.view, self.root, self.session_id, self.index, operation_id)

    def due(self, extra_bytes: int, extra_records: int) -> bool:
        """Would this append take the segment past a soft trigger? Never for a successor that
        holds no record of its own yet: rolling it over again would carry the same state."""
        return (
            len(self.raw) + extra_bytes > SOFT_BYTES or self.lines + extra_records > SOFT_RECORDS
        ) and (self.index == 0 or self.lines > 1)


def _transaction(root, session_id, update, *, may_roll: bool = True):
    """Read/compare/append under the chain's exclusive lock, rolling over first when the append
    would take the active segment past a soft trigger (#1733). ``update(active)`` returns the
    records to append and its result; it is pure in the active fold, so after a rollover it is
    asked again, of the successor. It never performs an RPC. A replay with no records still
    crosses fsync before its result is returned. ``may_roll=False``: a worker of an older release
    is live and reads only segment 0, so this append never rolls over (the old bound holds)."""

    def run():
        _prewarm_index(root, session_id)
        with native_segments.locked(root, session_id, exclusive=True) as view:
            with _before_append():
                active = _Active(view, root, session_id)
                records, result = update(active)
                data = chat_store._records_bytes(records)
                if records and may_roll and active.due(len(data), len(records)):
                    active = _rollover(view, active)
                    records, result = update(active)
                    data = chat_store._records_bytes(records)
            if not records:
                os.fsync(view.fd)
                return result
            if len(active.raw) + len(data) > MAX_BYTES or active.lines + len(records) > MAX_RECORDS:
                raise JournalError("unavailable", "native journal record bound exceeded")
            native_segments.append(view.fd, data, len(active.raw))
            return result

    return _guarded(run)


@contextlib.contextmanager
def _before_append():
    """Nothing has been appended yet: a full disk here (the fold's spool, a rebuilt sidecar,
    the rollover) is a KNOWN refusal, never a generic unavailable journal (#1733)."""
    try:
        yield
    except OSError as exc:
        if native_segments.cause_of(exc) != "disk_full":
            raise
        raise native_segments.Unwritable(
            "disk_full", "the disk is full; nothing was written"
        ) from None


def _prewarm_index(root: Path, session_id: str) -> None:
    """Before the EXCLUSIVE lock: hash frozen segments and validate their sidecars into the op-id
    index, so the first claim after a start never reads the whole history while appends wait.
    Best effort; under the lock the index is reused only if every frozen stamp is unchanged."""
    chain = _prewarm(root, session_id)
    if not chain or len(chain) < 2:
        return
    view = types.SimpleNamespace(chain=chain, exclusive=False, active=chain[-1])
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, IndexError):
        _frozen_index(view, root, session_id)


def prepare(root: Path, session_id: str) -> None:
    """Pre-claim admission (#1733): before a send launches or claims anything, roll the active
    segment over if the claim could reach a soft trigger. Raises ``unwritable`` (nothing was
    claimed: a known refusal) when that rollover cannot be written, ``unavailable`` when the
    journal is unreadable."""

    def run():
        _prewarm(root, session_id)
        with native_segments.locked(root, session_id, exclusive=True) as view, _before_append():
            active = _Active(view, root, session_id)
            active.journal()
            if active.due(ADMIT_BYTES, ADMIT_RECORDS):
                _rollover(view, active)

    _guarded(run)


def _rollover(view: native_segments.View, active: _Active) -> _Active:
    """Freeze the active segment behind a successor carrying its live state (#1733)."""
    journal = active.journal()
    lines = active.raw.split(b"\n")[:-1]
    offsets, at = [], 0
    for line in lines:
        offsets.append(at)
        at += len(line) + 1
    first = journal.base if active.index else 1  # cursor of line 0
    carried_before: dict[int, dict] = {}
    if active.index:
        for cursor, record in _segment_header(lines[0], active.index)["carried"]["records"]:
            carried_before[cursor] = record

    def record_at(cursor: int) -> dict:
        if cursor in carried_before:
            return carried_before[cursor]
        return _decode(lines[cursor - first])

    def offset_of(cursor: int) -> int:
        # A carried record lives in the header line (offset 0); its own records have their line.
        return 0 if active.index and cursor <= journal.base else offsets[cursor - first]

    carry = _carry(journal, record_at)
    sha256 = hashlib.sha256(active.raw).hexdigest()
    successor = native_segments.encode(
        {
            "type": "segment",
            "index": active.index + 1,
            "base_revision": journal.revision,
            "prev": {"size": len(active.raw), "sha256": sha256},
            "carried": carry,
        }
    )
    if len(successor) > CARRY_MAX_BYTES or len(carry["records"]) > CARRY_MAX_RECORDS:
        raise JournalError(
            "unavailable", "this session's live state is larger than a new segment may carry"
        )
    entries = _sidecar_entries(journal, active.index, offset_of)
    sidecar = _sidecar_bytes(
        active.session_id, active.index, len(active.raw), sha256, journal, entries
    )
    native_segments.rollover(view, active.raw, successor, sidecar)
    # The sidecar just written IS the validated index of the segment just frozen (its entries
    # come from this fold of its exact bytes): seed the caches, so the next lookup never reads
    # the segment back under the lock.
    found = native_segments.read_sidecar(active.root, active.session_id, active.index)
    if found is not None and found[0] == sidecar:
        frozen = view.chain[active.index]
        key = (active.session_id, active.index, frozen.stamp, found[1])
        with _SIDECAR_LOCK:
            _SIDECARS[key] = entries
            _SIDECARS.move_to_end(key)
            while len(_SIDECARS) > _SIDECARS_MAX:
                _SIDECARS.popitem(last=False)
    return _Active(view, active.root, active.session_id)


def _settled(operation: Operation) -> bool:
    """Nothing can write about this operation any more: a proved non-send, a control that was
    sent, or a submit sent whose turn ended. Everything else may still get a handoff or events."""
    if operation.handoff == "not_sent":
        return True
    if operation.request["action"] == "submit":
        return operation.handoff == "sent" and operation.turn_settled
    return operation.handoff == "sent"


def _carry(journal: Journal, record_at: Callable[[int], dict]) -> dict:
    """The live control state a successor needs for every fence (#1733), verbatim at its
    original cursors: what is unresolved NOW, never history or turn content.

    - every queued message (any generation: `settle_worker_ended` still writes their outcome);
    - every operation of the newest generation (the only one that can still write) that is not
      settled: its claim and handoffs; for an in-flight turn also its `turn_started` (the native
      turn id an interrupt or Send now targets) and its open approval requests (ACP permission
      requests included), each undecided and not cancelled;
    - that generation's latest background report, per live task and for its own turn;
    - the newest question, while no message has been dispatched after it (an answer is checked
      against it); the newest generation's id.
    Operations settled earlier answer replay from the frozen segment's sidecar instead."""
    writer = journal.live.writer
    carried = [
        op
        for op in journal.operations.values()
        if op.handoff == "queued" or op.worker_id == writer and not _settled(op)
    ]
    ids = {op.operation_id for op in carried}
    cursors: set[int] = set()
    for op in carried:
        cursors.update(op.records)
        if op.request["action"] == "submit" and op.started is not None:
            cursors.add(op.started)
    decided = {
        (op.request["params"]["approval_worker_id"], op.request["params"]["turn_id"])
        + (op.request["params"]["request_id"],)
        for op in journal.operations.values()
        if op.request["action"] == "decide"
    }
    for (worker, request_id, operation_id), cursor in journal.live.approvals.items():
        if operation_id in ids and (worker, operation_id, request_id) not in decided:
            cursors.add(cursor)
    if writer is not None and journal.live.background_worker == writer:
        cursors.update(journal.live.background.values())
    question = journal.events.last_question
    if question is not None and any(
        op.operation_id not in ids
        and op.request["action"] == "submit"
        and op.handoff != "not_sent"
        and op.dispatched_revision is not None
        and op.dispatched_revision > question[0]
        for op in journal.operations.values()
    ):
        question = None  # answered already: a later answer is refused, as before the rollover
    return {
        "session": journal.header,
        "records": [[cursor, record_at(cursor)] for cursor in sorted(cursors)],
        "last_question": list(question) if question is not None else None,
        "writer": writer,
    }


# --- operation sidecars of frozen segments ----------------------------------------------------
# `<id>.<n>.ops` (#1733): every operation segment n recorded, with the request digest, its final
# receipt and where its claim and last record are. Bound to the exact frozen bytes
# ({index, size, sha256, last_revision}), validated on every load (identity, checksum, and each
# entry against the records at its offsets), and rebuilt from the frozen segment itself when
# missing or wrong: a verifiable cache, never the authority. The authority is the frozen segment,
# pinned by its successor; when THAT does not match, the whole chain is unavailable.

_SIDECARS: OrderedDict[tuple, dict] = OrderedDict()
_SIDECARS_MAX = 64
_SIDECAR_LOCK = threading.Lock()


def _settled_before(view, root: Path, session_id: str, index: int, operation_id: str):
    """``operation_id``'s entry in the newest frozen segment before ``index`` that recorded it.

    From the active segment, through the session's op-id index (`_frozen_index`): a new id — every
    ordinary claim — costs one dict lookup however long the chain is (Hermes on #1771)."""
    if index == view.active.index:
        found = _frozen_index(view, root, session_id).get(operation_id)
        return None if found is None else _sidecar(view, root, session_id, found).get(operation_id)
    for earlier in range(index - 1, -1, -1):  # rebuilding a frozen segment's own sidecar: rare
        entry = _sidecar(view, root, session_id, earlier).get(operation_id)
        if entry is not None:
            return entry
    return None


# Which frozen segment last recorded each operation id, per session, built from VALIDATED
# sidecars once per frozen segment and extended when the chain grows. Keyed by every frozen
# segment's exact stamp (their bytes are what their successors pin), so any change rebuilds it.
_INDEX: OrderedDict[str, tuple[tuple, dict[str, int]]] = OrderedDict()
_INDEX_SESSIONS = 64


def _frozen_index(view, root: Path, session_id: str) -> dict[str, int]:
    frozen = tuple(segment.stamp for segment in view.chain[:-1])
    with _SIDECAR_LOCK:
        hit = _INDEX.get(session_id)
    if hit is not None and hit[0] == frozen:
        with _SIDECAR_LOCK:
            _INDEX.move_to_end(session_id)
        return hit[1]
    if hit is not None and frozen[: len(hit[0])] == hit[0]:
        start, ids = len(hit[0]), dict(hit[1])  # the chain grew: index only the new segments
    else:
        start, ids = 0, {}
    for index in range(start, len(frozen)):
        for operation_id in _sidecar(view, root, session_id, index):
            ids[operation_id] = index  # newest wins
    with _SIDECAR_LOCK:
        _INDEX[session_id] = (frozen, ids)
        _INDEX.move_to_end(session_id)
        while len(_INDEX) > _INDEX_SESSIONS:
            _INDEX.popitem(last=False)
    return ids


_ENTRY = {"digest", "receipt", "claim", "last", "action", "worker_id", "connection_id"}


def _sidecar_entries(journal: Journal, index: int, offset_of: Callable[[int], int]) -> dict:
    entries = {}
    for op in journal.operations.values():
        if not any(cursor > journal.base for cursor in op.records):
            continue  # only carried here, untouched: an older sidecar (or the carry) holds it
        entries[op.operation_id] = {
            "digest": _request_digest(op.request),
            "receipt": op.receipt(),
            "claim": [op.claimed_revision, offset_of(op.claimed_revision)],
            "last": [op.recorded_revision, offset_of(op.recorded_revision)],
            # What a later event naming it must match (the claim record proves all three).
            "action": op.request["action"],
            "worker_id": op.worker_id,
            "connection_id": op.connection_id,
        }
    return entries


def _checksum(entries: dict) -> str:
    body = json.dumps(entries, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _sidecar_bytes(session_id, index, size, sha256, journal: Journal, entries: dict) -> bytes:
    return native_segments.encode(
        {
            "type": "ops",
            "session": session_id,
            "index": index,
            "size": size,
            "sha256": sha256,
            "last_revision": journal.revision,
            "entries": entries,
            "checksum": _checksum(entries),
        }
    )


def _sidecar(view: native_segments.View, root: Path, session_id: str, index: int) -> dict:
    """Frozen segment ``index``'s validated operation index (rebuilt when it is not valid)."""
    segment, successor = view.chain[index], view.chain[index + 1]
    expected = {
        "index": index,
        "size": successor.header["prev"]["size"],
        "sha256": successor.header["prev"]["sha256"],
        "last_revision": successor.header["base_revision"],
    }
    found = native_segments.read_sidecar(root, session_id, index)
    key = (session_id, index, segment.stamp, found[1] if found else None)
    with _SIDECAR_LOCK:
        hit = _SIDECARS.get(key)
        if hit is not None:
            _SIDECARS.move_to_end(key)
            unwritten = key in _UNWRITTEN
    if hit is not None:
        if unwritten and view.exclusive:
            # Rebuilt before the lock (no file write there): replace the bad file now.
            with contextlib.suppress(OSError):
                native_segments.write_sidecar(
                    root, session_id, index, _rebuilt_bytes(session_id, expected, hit)
                )
                with _SIDECAR_LOCK:
                    _UNWRITTEN.discard(key)
        return hit
    entries = None
    if found is not None:
        try:
            entries = _validate_sidecar(found[0], session_id, segment, expected)
        except (ValueError, OSError, KeyError, TypeError, IndexError):
            entries = None
    if entries is None:
        entries = _rebuild_sidecar(view, root, session_id, segment, expected)
        found = native_segments.read_sidecar(root, session_id, index)
        key = (session_id, index, segment.stamp, found[1] if found else None)
        if not view.exclusive:
            with _SIDECAR_LOCK:
                _UNWRITTEN.add(key)
    with _SIDECAR_LOCK:
        _SIDECARS[key] = entries
        _SIDECARS.move_to_end(key)
        while len(_SIDECARS) > _SIDECARS_MAX:
            _SIDECARS.popitem(last=False)
    return entries


#: Sidecars rebuilt without the exclusive lock (the pre-lock warm-up), whose file still has to
#: be replaced by the next exclusive holder.
_UNWRITTEN: set[tuple] = set()


def _rebuilt_bytes(session_id: str, expected: dict, entries: dict) -> bytes:
    return native_segments.encode(
        {
            "type": "ops",
            "session": session_id,
            **expected,
            "entries": entries,
            "checksum": _checksum(entries),
        }
    )


def _rebuild_sidecar(view, root, session_id, segment, expected) -> dict:
    """From the frozen segment itself, whose bytes `resolve` verified against their pin."""
    raw = native_segments.read_file(segment.path, segment.stat, MAX_BYTES)
    journal = _fold_segment(
        raw,
        session_id,
        segment.index,
        directory=root,
        remember=False,
        prior=lambda op: _settled_before(view, root, session_id, segment.index, op),
    )
    if journal.revision != expected["last_revision"]:
        raise ValueError("a frozen native journal segment does not end at its successor's base")
    lines = raw.split(b"\n")[:-1]
    offsets, at = [], 0
    for line in lines:
        offsets.append(at)
        at += len(line) + 1
    first = journal.base if segment.index else 1

    def offset_of(cursor: int) -> int:
        return 0 if segment.index and cursor <= journal.base else offsets[cursor - first]

    entries = _sidecar_entries(journal, segment.index, offset_of)
    if view.exclusive:
        with contextlib.suppress(OSError):
            native_segments.write_sidecar(
                root,
                session_id,
                segment.index,
                _sidecar_bytes(
                    session_id, segment.index, len(raw), expected["sha256"], journal, entries
                ),
            )
    return entries


_OPERATION_RECORDS = frozenset({"native_operation", "native_queued", "native_handoff"})


def _recorded_operations(segment) -> dict[str, dict]:
    """Every operation frozen ``segment`` itself records, read from its pinned bytes: the
    cursor/offset/record of its claim (if claimed there) and of its LAST record there. What a
    sidecar must index exactly — an entry it omits would let a used operation id be claimed
    again (Hermes on #1733), so completeness is checked here, not trusted."""
    raw = native_segments.read_file(segment.path, segment.stat, MAX_BYTES)
    lines = raw.split(b"\n")[:-1]
    first = segment.base if segment.index else 1  # cursor of line 0
    found: dict[str, dict] = {}
    offset = 0
    for number, line in enumerate(lines):
        at, offset = offset, offset + len(line) + 1
        if number == 0 and segment.index:
            continue  # the header: carried copies are not this segment's records
        if (
            b'"native_operation"' not in line
            and b'"native_queued"' not in line
            and (b'"native_handoff"' not in line)
        ):
            continue
        record = _decode(line)
        if record.get("type") not in _OPERATION_RECORDS:
            continue  # the word appeared inside an event's text
        cursor = first + number
        entry = found.setdefault(_uuid(record.get("operation_id")), {"claim": None})
        if record["type"] != "native_handoff":
            entry["claim"] = ([cursor, at], record)
        entry["last"] = ([cursor, at], record)
    return found


def _validate_sidecar(raw: bytes, session_id: str, segment, expected: dict) -> dict:
    """A sidecar is used only when it is exactly the index of its pinned segment: its identity,
    its checksum, and one entry per operation the segment records — claim and last record at the
    cursors and offsets the segment has, the digest of the claimed request, the final receipt."""
    if not raw.endswith(b"\n"):
        raise ValueError("incomplete sidecar")
    doc = native_segments.decode(raw[:-1], native_segments.SIDECAR_MAX_BYTES)
    if set(doc) != {"type", "session", "index", "size", "sha256", "last_revision", "entries"} | {
        "checksum"
    }:
        raise ValueError("invalid sidecar")
    if doc["type"] != "ops" or doc["session"] != session_id:
        raise ValueError("sidecar of another journal")
    if {key: doc[key] for key in expected} != expected:
        raise ValueError("sidecar indexes other bytes")
    entries = doc["entries"]
    if type(entries) is not dict or doc["checksum"] != _checksum(entries):
        raise ValueError("sidecar checksum mismatch")
    recorded = _recorded_operations(segment)
    if set(entries) != set(recorded):
        raise ValueError("sidecar does not index every operation of its segment")
    carried = None
    for operation_id, entry in entries.items():
        if type(entry) is not dict or set(entry) != _ENTRY:
            raise ValueError("invalid sidecar entry")
        truth = recorded[operation_id]
        receipt = entry["receipt"]
        handoff = receipt.get("handoff") if type(receipt) is dict else None
        shape = {"operation_id", "recorded_revision", "handoff"}
        if handoff == "not_sent":
            shape = {*shape, "reason_code"}
        last_at, last = truth["last"]
        if (
            type(receipt) is not dict
            or set(receipt) != shape
            or receipt["operation_id"] != operation_id
            or entry["last"] != last_at
            or receipt["recorded_revision"] != last_at[0]
            or handoff not in HANDOFFS
        ):
            raise ValueError("sidecar entry does not match its segment")
        if truth["claim"] is not None:
            claim_at, claimed = truth["claim"]
            if entry["claim"] != claim_at:
                raise ValueError("sidecar entry does not match its claim")
        else:
            # Claimed earlier and carried in: its claim is in this segment's header.
            claim_at = entry["claim"]
            if (
                type(claim_at) is not list
                or len(claim_at) != 2
                or claim_at[1] != 0
                or not segment.index
                or type(claim_at[0]) is not int
                or not 0 < claim_at[0] <= segment.base
            ):
                raise ValueError("sidecar entry does not match its claim")
            if carried is None:
                header = native_segments.header(segment.path, segment.stat, segment.index)
                carried = {c: r for c, r in header["carried"]["records"]}
            claimed = carried[claim_at[0]]
        if (
            claimed.get("type") not in {"native_operation", "native_queued"}
            or claimed.get("operation_id") != operation_id
            or (claimed.get("worker_id"), claimed.get("connection_id"))
            != (entry["worker_id"], entry["connection_id"])
            or claimed["request"].get("action") != entry["action"]
            or _request_digest(native_ipc.normalize_immutable_request(claimed["request"]))
            != entry["digest"]
        ):
            raise ValueError("sidecar entry does not match its claim")
        if last is claimed or last["type"] != "native_handoff":
            if handoff != ("queued" if claimed["type"] == "native_queued" else "uncertain"):
                raise ValueError("sidecar entry does not match its claim")
        elif last.get("handoff") != handoff or (
            handoff == "not_sent"
            and (last.get("reason_code") or "unexpected") != receipt["reason_code"]
        ):
            raise ValueError("sidecar entry does not match its last record")
    return entries


def withdrawn(journal: Journal) -> set[str]:
    """Queued messages removed before they were sent (#1447), on durable proof of both halves:
    the ``withdraw`` control recorded ``sent`` and the message settled ``not_sent``. A partial
    record proves nothing, so that message is still shown as the journal has it."""
    out = set()
    for op in journal.operations.values():
        if op.request["action"] != "withdraw" or op.handoff != "sent":
            continue
        target = journal.operations.get(op.request["params"]["queued_turn_id"])
        if target is not None and target.handoff == "not_sent":
            out.add(target.operation_id)
    return out


def _target(journal, session_key):
    if journal.header["request"]["session_key"] != session_key:
        raise JournalError("conflict", "native journal belongs to another client")


def _replies(journal: Journal, asked: int, worker_id: str, exclude: str | None = None):
    """Operator messages that reach (or will reach) the agent after the question at ``asked``:
    dispatched after it, or still waiting in the live worker's queue. Codex reads the first of
    them as the answer. One settled ``not_sent`` (refused, or removed before it was sent, #1447)
    never reached it; one left queued by a stopped worker never will."""
    for op in journal.operations.values():
        if op.request["action"] != "submit" or op.operation_id == exclude:
            continue
        if op.handoff == "not_sent":
            continue
        if op.dispatched_revision is not None:
            if op.dispatched_revision > asked:
                yield op
        elif op.handoff == "queued" and op.worker_id == worker_id:
            yield op


def _question_open(journal: Journal, question_id: str, worker_id: str) -> bool:
    """An async question still takes an answer (#1463): it is the newest question the agent
    asked, and no operator message reaches the agent after it ahead of this one.

    Checked inside the claim transaction, so two tabs, a retry and a late lost write serialize:
    exactly one answer is admitted. A queued answer is checked again when it leaves the queue
    (``answer_current``), so a question asked meanwhile is never answered by it.
    """
    asked = journal.events.last_question
    if asked is None or asked[1] != question_id:
        return False
    return next(_replies(journal, asked[0], worker_id), None) is None


def answer_current(journal: Journal, operation_id: str) -> bool:
    """A claimed answer may still be delivered (#1463): its question is still the newest one,
    and no other message reached the agent after it. The worker asks this as a queued answer
    leaves the queue; a ``False`` settles it ``not_sent`` rather than answer a newer question."""
    op = journal.operations.get(operation_id)
    asked = journal.events.last_question
    if op is None or "reply_to" not in op.request["params"]:
        return True
    if asked is None or asked[1] != op.request["params"]["reply_to"]:
        return False
    dispatched = (
        o
        for o in _replies(journal, asked[0], op.worker_id, exclude=operation_id)
        if o.dispatched_revision is not None
    )
    return next(dispatched, None) is None


def claim(root: Path, request: dict, *, queued: bool = False) -> tuple[bool, dict]:
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
    if queued and checked["action"] != "submit":
        raise JournalError("invalid", "only operator turns can be queued")

    def update(active):
        journal = active.journal()
        _target(journal, checked["session_key"])
        previous = journal.operations.get(operation_id)
        if previous is not None:
            if previous.request != immutable:
                raise JournalError(
                    "conflict", "native operation UUID already binds another request"
                )
            return [], (False, previous.receipt())
        # Settled in an earlier segment (#1733): its sidecar answers, exactly as the fold did.
        used = active.used(operation_id)
        if used is not None:
            if used["digest"] != _request_digest(immutable):
                raise JournalError(
                    "conflict", "native operation UUID already binds another request"
                )
            return [], (False, dict(used["receipt"]))
        if params["expected_revision"] != journal.revision:
            raise JournalError("conflict", REVISION_CHANGED)
        if "reply_to" in params and not _question_open(
            journal, params["reply_to"], checked["worker_id"]
        ):
            raise JournalError(
                "conflict", "that question was already answered or a newer one was asked"
            )
        if queued:
            waiting = [
                op.request
                for op in journal.operations.values()
                if op.handoff == "queued" and op.worker_id == checked["worker_id"]
            ]
            size = sum(
                len(json.dumps(item, ensure_ascii=True).encode()) for item in [*waiting, immutable]
            )
            if len(waiting) >= MAX_QUEUED or size > MAX_QUEUE_BYTES:
                raise JournalError(
                    "busy", f"message queue is full (up to {MAX_QUEUED} messages / 4 MiB)"
                )
        record = {
            "type": "native_queued" if queued else "native_operation",
            "operation_id": operation_id,
            "request": immutable,
            "worker_id": checked["worker_id"],
            "connection_id": checked["connection_id"],
            "ts": time.time(),
        }
        after = active.preview([record])
        return [record], (True, after.operations[operation_id].receipt())

    return _transaction(root, session_id, update)


def record_handoff(
    root: Path,
    binding: native_ipc.Binding,
    operation_id: str,
    handoff: str,
    *,
    reason_code: str | None = None,
) -> dict:
    """``reason_code`` (#1538): why a ``not_sent`` was settled, from `REASON_CODES`. A repeated
    ``not_sent`` only observes the first, whatever reason it names."""
    _uuid(operation_id)
    if reason_code is not None and not _reason(handoff, reason_code):
        raise JournalError("invalid", "invalid native handoff reason")
    session_id = binding.session_key.partition(":")[2]

    def update(active):
        journal = active.journal()
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
            **({"reason_code": reason_code} if reason_code is not None else {}),
        }
        after = active.preview([record])
        return [record], after.operations[operation_id].receipt()

    return _transaction(root, session_id, update)


def settle_worker_ended(
    root: Path,
    session_key: str,
    operation_id: str,
    worker_id: str,
    reason_code: str = "worker_ended",
    *,
    may_roll: bool = True,
) -> bool:
    """Record that a message still queued when its worker ended was never sent (#1538).

    The caller saw that worker not live, under the session's lifecycle lock. Idempotent: it
    writes only while the operation is still ``queued`` by that worker, with no handoff at all,
    so a message that left the queue (sent, uncertain, refused) is never touched. The record is
    a proof by itself: a worker fsyncs ``uncertain`` before stdin and ``not_sent`` is final, so
    nothing can write the message after it. It carries the original claim's worker and
    connection, as every handoff does. Returns whether it wrote. ``reason_code``: `worker_ended`,
    or `journal_unwritable` when that worker stopped because the journal could not be written
    (#1733)."""
    _uuid(operation_id)
    if reason_code not in {"worker_ended", "journal_unwritable"}:
        raise JournalError("invalid", "invalid native handoff reason")
    session_id = session_key.partition(":")[2]

    def update(active):
        journal = active.journal()
        _target(journal, session_key)
        operation = journal.operations.get(operation_id)
        if operation is None or operation.handoff != "queued" or operation.worker_id != worker_id:
            return [], False
        record = {
            "type": "native_handoff",
            "operation_id": operation_id,
            "handoff": "not_sent",
            "worker_id": operation.worker_id,
            "connection_id": operation.connection_id,
            "ts": time.time(),
            "reason_code": reason_code,
        }
        active.preview([record])
        return [record], True

    return _transaction(root, session_id, update, may_roll=may_roll)


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

    def update(active):
        after = active.preview(records)
        _target(after, binding.session_key)
        return records, after.revision

    return _transaction(root, session_id, update)
