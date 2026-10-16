"""Disposable disk-backed event payloads; only cursor/operation indexes stay on the heap.

The authoritative JSONL is never replaced. A spool contains already validated observations,
has no pathname, and closes when the last snapshot releases it. Copies share immutable byte
ranges, while keeping separate indexes, so a failed preview cannot change another snapshot.
"""

from __future__ import annotations

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


class _Spool:
    def __init__(self, directory: Path | None):
        self.file = tempfile.TemporaryFile(dir=directory)
        self.size = 0
        self.lock = threading.Lock()

    def append(self, data: bytes) -> int:
        with self.lock:
            # Failed previews can leave unreachable bytes. Bound them too; a fresh fold
            # fits because the source journal itself is capped at 64 MiB.
            if self.size + len(data) > 128 * 1024 * 1024:
                raise SpoolFull
            offset = self.size
            view = memoryview(data)
            while view:
                written = os.pwrite(self.file.fileno(), view, self.size)
                if written <= 0:
                    raise OSError("event spool write made no progress")
                self.size += written
                view = view[written:]
            return offset


class Events(Sequence):
    def __init__(self, directory: Path | None = None):
        self._spool = _Spool(directory)
        self.cursors = array("Q")
        self._offsets = array("Q")
        self._lengths = array("I")
        self._operations: dict[str | None, array] = {}
        self._agent_turns: set[str] = set()
        self._global = array("Q")

    def __len__(self):
        return len(self.cursors)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        raw = os.pread(self._spool.file.fileno(), self._lengths[index], self._offsets[index])
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
        # These affect session state even when their turn is outside the display window.
        if event["kind"] in {
            "session",
            "model",
            "background",
            "approval_cancelled",
            "turn_completed",
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
            + sys.getsizeof(self._spool.file.raw)
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
