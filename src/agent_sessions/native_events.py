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

from . import native_protocol


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


def heartbeat(event: dict) -> bool:
    """A `background` record that neither starts nor ends a turn the agent runs for itself
    (#1427): a task's progress or an activity / status report. An idle agent's running subagent
    writes one every few seconds, so a long session holds tens of thousands (#1805). They are kept
    out of the session-wide index: what a snapshot needs of them is folded at append instead."""
    return event["kind"] == "background" and "background_turn" not in event["data"]


def observe_task(tasks: dict, ended: set[str], data: dict, ts: float) -> None:
    """Merge one worker's task observation (#1461), independently of execution state.

    Terminal ids stay ended for this generation, so a late sparse/progress frame cannot revive
    them. Journal order chooses observations; their timestamps are labels, never ordering keys.
    """
    task_id = data["task_id"]
    state = data.get("task_state", data["state"])
    if state in native_protocol.TERMINAL_TASK_STATES:
        ended.add(task_id)
        tasks.pop(task_id, None)
        return
    if task_id in ended:
        return
    if task_id not in tasks:
        if len(tasks) >= native_protocol.MAX_PENDING:
            return
        tasks[task_id] = {
            "task_id": task_id,
            "state": state or "unknown",
            "started_at": ts,
            "updated_at": ts,
        }
    task = tasks[task_id]
    task["updated_at"] = ts
    if state:
        task["state"] = state
    for field in native_protocol.TASK_METADATA_LIMITS:
        if data.get(field):
            task[field] = data[field]


#: The counter index's bounds (#1726): distinct names kept per map; the rest are `other`.
MAX_COUNTED_TOOLS = 256
MAX_COUNTED_SERVERS = 64
MAX_COUNTED_SKILLS = 256
#: How many recent calls' skill uses the counter remembers, to count each one once.
SKILLED_SEEN = 1024


class Counts:
    """How many calls this conversation made, per built-in tool, per MCP server and per skill
    used (#1726). Derived from the authoritative journal events alone, like the spool beside it,
    and disposable: every fold rebuilds it, so it is never a second source of truth. It reads
    every `tool` event of the journal, never the snapshot's windowed and truncated rows.

    Only an event marked ``call`` (the codec's "this call starts here") is a call, so a call
    observed many times counts once. An MCP call is counted under its server and not as a
    built-in tool; one whose server could not be attributed (``mcp_server: ""``) is
    ``unattributed``. A skill counts only from a REPORTED use (``skill``), never from its being
    available — on whichever event of the call reports it (opencode names the skill on a later
    update than the call's start), once per call item. Each map keeps at most its bound of
    distinct names; calls past it are ``other``."""

    __slots__ = ("tools", "servers", "skills", "other", "unattributed", "_skilled")

    def __init__(self) -> None:
        self.tools: dict[str, int] = {}
        self.servers: dict[str, int] = {}
        self.skills: dict[str, int] = {}
        self.other = {"tools": 0, "servers": 0, "skills": 0}
        self.unattributed = 0
        # (turn, item) of the recent calls whose skill use was counted: one use per call, however
        # many observations repeat it. Bounded: a call's reports arrive close together.
        self._skilled: dict[tuple, None] = {}

    def copy(self) -> Counts:
        result = Counts()
        result.tools, result.servers, result.skills = (
            dict(self.tools),
            dict(self.servers),
            dict(self.skills),
        )
        result.other, result.unattributed = dict(self.other), self.unattributed
        result._skilled = dict(self._skilled)
        return result

    def _bump(self, key: str, counts: dict[str, int], name: str, limit: int) -> None:
        if name in counts:
            counts[name] += 1
        elif len(counts) < limit:
            counts[name] = 1
        else:
            self.other[key] += 1

    def observe(self, data: dict) -> None:
        if data.get("skill"):
            key = (data.get("operation_id") or data.get("agent_turn"), data.get("item_id"))
            if key not in self._skilled:
                if len(self._skilled) >= SKILLED_SEEN:
                    self._skilled.pop(next(iter(self._skilled)))
                self._skilled[key] = None
                self._bump("skills", self.skills, data["skill"], MAX_COUNTED_SKILLS)
        if data.get("call") is not True:
            return
        if "mcp_server" in data:
            if data["mcp_server"]:
                self._bump("servers", self.servers, data["mcp_server"], MAX_COUNTED_SERVERS)
            else:
                self.unattributed += 1
        elif data.get("tool_name") or data.get("tool"):
            # The tool's own name where the protocol only reports a kind (ACP's `other`).
            name = data.get("tool_name") or data["tool"]
            self._bump("tools", self.tools, name, MAX_COUNTED_TOOLS)

    def view(self) -> dict:
        def rows(counts: dict[str, int]) -> list[dict]:
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return [{"name": name, "calls": calls} for name, calls in ranked]

        return {
            "tools": rows(self.tools),
            "tools_other": self.other["tools"],
            "servers": rows(self.servers),
            "servers_other": self.other["servers"],
            "unattributed": self.unattributed,
            "skills": rows(self.skills),
            "skills_other": self.other["skills"],
        }


class Events(Sequence):
    def __init__(self, directory: Path | None = None, *, limit: int = SPOOL_BYTES):
        self._spool = _Spool(directory, limit)
        self.cursors = array("Q")
        self._offsets = array("Q")
        self._lengths = array("I")
        self._operations: dict[str | None, array] = {}
        self._agent_turns: set[str] = set()
        self._global = array("Q")
        # Folded from every `background` record at append (#1805), per worker: the newest plain
        # heartbeat's (cursor, active), and the task inventory with that worker's ended ids.
        self._beats: dict[str, tuple[int, bool]] = {}
        self._tasks: dict[str, tuple[dict[str, dict], set[str]]] = {}
        # (cursor, item id) of the newest settled question the agent asked (#1463): what an
        # answer's `reply_to` must still name when it is claimed.
        self.last_question: tuple[int, str] | None = None
        # #1726: the newest plan checklist, the newest plan document (each an index), the newest
        # environment report with the worker that made it, and the call counts. The plan is the
        # conversation's, whatever page of history is shown; the environment its generation's.
        self.plan_entries: int | None = None
        self.plan_document: int | None = None
        self.plan_stale: int | None = None
        self.environment: tuple[int, str] | None = None
        # The newest `wakeup` (#1927) and the worker that reported it: read by index only.
        self.wakeup: tuple[int, str] | None = None
        self.counts = Counts()

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
        result._beats = dict(self._beats)
        result._tasks = {
            worker: ({task_id: dict(task) for task_id, task in tasks.items()}, set(ended))
            for worker, (tasks, ended) in self._tasks.items()
        }
        result.last_question = self.last_question
        result.plan_entries = self.plan_entries
        result.plan_document = self.plan_document
        result.plan_stale = self.plan_stale
        result.environment = self.environment
        result.wakeup = self.wakeup
        result.counts = self.counts.copy()
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
        if event["kind"] == "tool":
            self.counts.observe(event["data"])
        if event["kind"] == "wakeup":
            self.wakeup = (index, item["worker_id"])
            return
        if event["kind"] in {"plan", "environment"}:
            # Read by index only (`plan()`, `environment_of()`): a long session's many plan
            # updates are never re-read by every snapshot.
            if event["kind"] == "environment":
                self.environment = (index, item["worker_id"])
            else:
                if "entries" in event["data"]:
                    self.plan_entries = index
                if "document" in event["data"]:
                    self.plan_document = index
                if event["data"].get("stale"):
                    self.plan_stale = index
            return
        if event["kind"] == "background" and event["data"].get("task_id"):
            tasks, ended = self._tasks.setdefault(item["worker_id"], ({}, set()))
            observe_task(tasks, ended, event["data"], item["ts"])
        if heartbeat(event):
            # Never indexed (#1805): a snapshot reads only the newest one per worker, from here,
            # and its tasks from the fold above. A full read still walks every record.
            self._beats[item["worker_id"]] = (item["cursor"], bool(event["data"].get("active")))
        # These affect session state even when their turn is outside the display window.
        elif event["kind"] in {
            "session",
            "model",
            "background",
            "approval_cancelled",
            "turn_completed",
            "memory_pressure",
            # The background inventory and its settlement (#1724), per generation.
            "background_inventory",
            "background_settled",
            "stop_aborted",
            "stop_paused",
            "stop_resumed",
            # #1732: rare, bounded, and a session-level one belongs to no turn; the projection
            # attributes a turn-level one by its field, whatever window it reads.
            "memory_oom_kill",
        } or (event["kind"] == "approval" and "background_task" in event["data"]):
            # A background task's request (#1725) belongs to no turn: it is pending whichever
            # turns the window shows.
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

    def plan(self, live_worker: str | None = None) -> dict | None:
        """The conversation's current plan (#1726): the newest checklist (with its explanation)
        and the newest proposed document, each with when it was recorded, or None.

        ``stale`` (review on #1811): the checklist may be out of date — its client said so
        later, or a later generation is live and has not re-confirmed it."""
        out: dict = {}
        if self.plan_entries is not None:
            item = self[self.plan_entries]
            data = item["event"]["data"]
            out["entries"] = data["entries"]
            if data.get("explanation"):
                out["explanation"] = data["explanation"]
            out["entries_at"] = item.get("ts")
            out["stale"] = (
                self.plan_stale is not None and self.plan_stale > self.plan_entries
            ) or (live_worker is not None and item["worker_id"] != live_worker)
        if self.plan_document is not None:
            item = self[self.plan_document]
            out["document"] = item["event"]["data"]["document"]
            out["document_at"] = item.get("ts")
        return out or None

    def environment_of(self, worker_id: str | None) -> dict | None:
        """What ``worker_id``'s client last reported it works with, or None: a report by an
        earlier generation ended with it."""
        if worker_id is None or self.environment is None or self.environment[1] != worker_id:
            return None
        return dict(self[self.environment[0]]["event"]["data"])

    def wakeup_of(self, worker_id: str | None) -> dict | None:
        """``worker_id``'s pending ScheduleWakeup (#1927) — when it is due and why — or None: it
        fired, was cancelled, or an earlier generation reported it (its timer ended with it).
        A wake that never fired stays here until the next report; the pane hides it past due."""
        if worker_id is None or self.wakeup is None or self.wakeup[1] != worker_id:
            return None
        data = self[self.wakeup[0]]["event"]["data"]
        if data.get("state") != "scheduled":
            return None
        return {key: data[key] for key in ("at", "reason") if key in data}

    def heartbeat_of(self, worker_id: str) -> tuple[int, bool] | None:
        """(cursor, active) of ``worker_id``'s newest plain heartbeat, if it wrote one."""
        return self._beats.get(worker_id)

    def tasks_of(self, worker_id: str) -> list[dict]:
        """``worker_id``'s tasks still running, in the order first seen; copies."""
        tasks, _ = self._tasks.get(worker_id, ({}, set()))
        return [dict(task) for task in tasks.values()]

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
            + sys.getsizeof(self._beats)
            + sum(sys.getsizeof(key) + sys.getsizeof(value) for key, value in self._beats.items())
            + sys.getsizeof(self._tasks)
            + sum(
                sys.getsizeof(worker)
                + sys.getsizeof(tasks)
                + sum(
                    sys.getsizeof(task) + sum(sys.getsizeof(v) for v in task.values())
                    for task in tasks.values()
                )
                + sys.getsizeof(ended)
                + sum(sys.getsizeof(task_id) for task_id in ended)
                for worker, (tasks, ended) in self._tasks.items()
            )
        )

    @property
    def disk_bytes(self):
        return self._spool.size
