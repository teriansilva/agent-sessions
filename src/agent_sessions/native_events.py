"""Disposable disk-backed event payloads; only cursor/operation indexes stay on the heap.

The authoritative JSONL is never replaced. A spool contains already validated observations,
has no pathname, and closes when the last snapshot releases it. Copies share immutable byte
ranges, while keeping separate indexes, so a failed preview cannot change another snapshot.
"""

from __future__ import annotations

import errno
import heapq
import json
import os
import sys
import tempfile
import threading
from array import array
from collections.abc import Sequence
from pathlib import Path


class SpoolFull(Exception):
    """Discard the derived cache and rebuild from authoritative bytes."""


#: One segment's spool bound: a fresh fold fits, because a segment is capped at 64 MiB. A fold of
#: a whole chain of segments (#1733) gets this much per segment.
SPOOL_BYTES = 128 * 1024 * 1024


_FULL = (errno.ENOSPC, errno.EDQUOT)


class _Spool:
    """The payload store: an anonymous file on the journal's filesystem, or — when that
    filesystem is FULL — memory (#1733). A full disk must not make the session unreadable: its
    snapshot still has to show what happened, and a send must still reach the refusal that
    says the record could not be written, not a generic failure of a derived cache."""

    def __init__(self, directory: Path | None, limit: int = SPOOL_BYTES):
        self.size = 0
        self.limit = limit
        self.lock = threading.Lock()
        self.memory: bytearray | None = None
        try:
            self.file = tempfile.TemporaryFile(dir=directory)
        except OSError as exc:
            if exc.errno not in _FULL:
                raise
            self.file = None
            self.memory = bytearray()

    def _to_memory(self) -> None:
        held = bytearray(os.pread(self.file.fileno(), self.size, 0)) if self.size else bytearray()
        if len(held) != self.size:
            raise OSError("event spool is incomplete")
        self.file.close()
        self.file, self.memory = None, held

    def append(self, data: bytes) -> int:
        with self.lock:
            # Failed previews can leave unreachable bytes. Bound them too; a fresh fold
            # fits because the source journal itself is capped at 64 MiB per segment.
            if self.size + len(data) > self.limit:
                raise SpoolFull
            offset = self.size
            if self.memory is None:
                try:
                    view = memoryview(data)
                    while view:
                        written = os.pwrite(
                            self.file.fileno(), view, self.size + (len(data) - len(view))
                        )
                        if written <= 0:
                            raise OSError("event spool write made no progress")
                        view = view[written:]
                except OSError as exc:
                    if exc.errno not in _FULL:
                        raise
                    self._to_memory()  # only the complete records before this one are kept
            if self.memory is not None:
                del self.memory[offset:]
                self.memory += data
            self.size = offset + len(data)
            return offset

    def read(self, length: int, offset: int) -> bytes:
        with self.lock:  # never across a switch to memory, which closes the file
            if self.memory is not None:
                return bytes(self.memory[offset : offset + length])
            return os.pread(self.file.fileno(), length, offset)


class Events(Sequence):
    def __init__(self, directory: Path | None = None, *, limit: int = SPOOL_BYTES):
        self._spool = _Spool(directory, limit)
        self.cursors = array("Q")
        self._offsets = array("Q")
        self._lengths = array("I")
        self._operations: dict[str | None, array] = {}
        self._agent_turns: set[str] = set()
        self._global = array("Q")
        # (cursor, item id) of the newest settled question the agent asked (#1463): what an
        # answer's `reply_to` must still name when it is claimed.
        self.last_question: tuple[int, str] | None = None

    def __len__(self):
        return len(self.cursors)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        raw = self._spool.read(self._lengths[index], self._offsets[index])
        if len(raw) != self._lengths[index]:
            raise OSError("event spool is incomplete")
        return json.loads(raw)

    def __eq__(self, other):
        return (
            isinstance(other, Sequence)
            and len(self) == len(other)
            and all(a == b for a, b in zip(self, other, strict=True))
        )

    def copy(self):
        result = object.__new__(Events)
        result._spool = self._spool
        result.cursors = self.cursors[:]
        result._offsets = self._offsets[:]
        result._lengths = self._lengths[:]
        result._operations = {key: indexes[:] for key, indexes in self._operations.items()}
        result._agent_turns = set(self._agent_turns)
        result._global = self._global[:]
        result.last_question = self.last_question
        return result

    def append(self, item: dict):
        # Keep UTF-8 compact: escaping every non-ASCII character could make a valid
        # 64 MiB journal exceed the spool bound. Preserve any escaped legacy surrogate
        # too; json.loads(bytes) decodes with surrogatepass, so cached values stay exact.
        raw = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", "surrogatepass"
        )
        offset = self._spool.append(raw)
        index = len(self)
        self.cursors.append(item["cursor"])
        self._offsets.append(offset)
        self._lengths.append(len(raw))
        event = item["event"]
        if (
            event["kind"] == "text"
            and event["data"].get("questions")
            and event["data"].get("item_id")
        ):
            self.last_question = (item["cursor"], event["data"]["item_id"])
        # These affect session state even when their turn is outside the display window.
        if event["kind"] in {
            "session",
            "model",
            "background",
            "approval_cancelled",
            "turn_completed",
            "memory_pressure",
            # #1732: rare, bounded, and a session-level one belongs to no turn; the projection
            # attributes a turn-level one by its field, whatever window it reads.
            "memory_oom_kill",
        }:
            self._global.append(index)
        else:
            # An own turn's output (#1438) is indexed by its agent turn, so one bounded read
            # serves either kind. Only the index is shared: the projection attributes by FIELD,
            # so an operation id equal to an agent turn's never moves output between them.
            data = event["data"]
            operation = data.get("operation_id") or data.get("agent_turn")
            self._operations.setdefault(operation, array("Q")).append(index)
            if "agent_turn" in data and (
                event["kind"] == "tool"
                or (
                    event["kind"] == "text"
                    and not data.get("partial")
                    and bool(data.get("text", "").strip())
                )
            ):
                self._agent_turns.add(data["agent_turn"])

    def allow(self, limit: int) -> None:
        """Raise the shared spool's bound (a chain fold extended by more segments, #1733)."""
        with self._spool.lock:
            self._spool.limit = max(self._spool.limit, limit)

    def recorded(self, agent_turn: str) -> bool:
        """Did own turn ``agent_turn`` (#1438) record attributable, displayable output (a tool or
        non-blank settled text)? Scoped to ``agent_turn`` so a colliding ``operation_id`` or an
        empty text block never counts."""
        return agent_turn in self._agent_turns

    def for_operations(self, operations: set[str]):
        indexes = [self._global]
        indexes.extend(self._operations[key] for key in operations if key in self._operations)
        return (self[index] for index in heapq.merge(*indexes))

    @property
    def memory_bytes(self):
        return (
            sys.getsizeof(self)
            + sys.getsizeof(self.__dict__)
            + sys.getsizeof(self._spool)
            + sys.getsizeof(self._spool.__dict__)
            + sys.getsizeof(self._spool.file)
            + (sys.getsizeof(self._spool.file.raw) if self._spool.file is not None else 0)
            + sys.getsizeof(self._spool.lock)
            + sum(
                sys.getsizeof(a) for a in (self.cursors, self._offsets, self._lengths, self._global)
            )
            + sys.getsizeof(self._operations)
            + sum(
                sys.getsizeof(key) + sys.getsizeof(value) for key, value in self._operations.items()
            )
            + sys.getsizeof(self._agent_turns)
            + sum(sys.getsizeof(key) for key in self._agent_turns)
        )

    @property
    def disk_bytes(self):
        return self._spool.size
