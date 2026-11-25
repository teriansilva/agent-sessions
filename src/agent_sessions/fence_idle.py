"""Is a step session idle, as the adoption fence may ask it (#2232).

`mission_fence.fenced_step_stop` asks `idle()` INSIDE the fence, holding `session_input._lock`, on
a worker thread. Nothing that can wait on another party may run there: a session's runtime
snapshot takes, for an API (chat) session, that session's `asyncio.Lock` (`chat_runtime._lock`),
which belongs to the MAIN loop — driven from the worker's own loop (`asyncio.run`) it can hang the
fence under the global lock and leave the chat lock bound to a dead loop — and a native session's
journal read waits on the journal's `flock` while a writer appends or rolls it over.

So the question is asked in two halves:

* :func:`observe` — BEFORE the fence, on the main loop, as ONE operation under ONE deadline
  (:data:`OBSERVE_TIMEOUT_S`): resolve the session's marker reader (:func:`marker_reader`; it may
  read the roster), read the marker FIRST, then take the snapshot (the full view a caller may
  need, e.g. rework's reviewer generation). Marker first, so anything that moves after it —
  even between the two reads — makes the fence's marker differ. The per-key slot is reserved
  SYNCHRONOUSLY, before the first await, so two concurrent callers never both start one: at
  most ONE outstanding observation per session key. That operation runs as its own task that is
  never cancelled, so the slot is freed only when all its underlying work (worker I/O included)
  has finished; while it runs, a new observation is refused (not idle), and a late result is
  discarded — nothing waits for it.
* :func:`validate` — INSIDE the fence: the observation said idle, is younger than
  :data:`OBSERVE_MAX_AGE_S`, and its reader's marker read NOW is the same. The reader only
  STATS files and reads in-process state — no `flock`, no journal parse, no settling, no
  `asyncio`. Anything missing, unreadable or different reads NOT idle. The fence's own ownership
  and proved-stop checks are unchanged.

The marker, per adapter (the consistency contract): every append, rollover or rewrite of the
session's persisted record changes it, so a turn that starts, settles or is added, a background
task's records and a reconcile all read as "moved".

* chat (`openai-chat`): the conversation log file's stat stamp (device, inode, size, mtime, ctime)
  and whether a turn runs in this process (`chat_runtime.running_keys`).
* native API clients: the active journal segment's stat stamp (`native_segments.active_stamp`:
  its index and stat — the same lock-free probe the events wake uses; a rollover moves the
  index).
* both: the session's physical key (`mission_fence.physical_of`), so a replaced session differs.
* anything else (a terminal session): no reader — not idle through this path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: How long the whole pre-fence observation (marker reader, marker, snapshot) may take, in seconds.
OBSERVE_TIMEOUT_S = 5.0
#: How old an observation may be when the fence checks it, in seconds.
OBSERVE_MAX_AGE_S = 30.0

#: Per session key, the observation still running from an earlier call.
_OUTSTANDING: dict[str, asyncio.Task] = {}


@dataclass(frozen=True)
class Observation:
    key: str
    snapshot: dict
    marker: tuple
    idle: bool
    at: float
    reader: Callable[[], tuple | None]


def _stamp(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def marker_reader(key: str) -> Callable[[], tuple | None] | None:
    """A function that reads the session's marker by STAT and in-process state only (see the
    module docstring), resolved here — OUTSIDE the fence, since resolving may read the roster —
    or None when the session has no such marker. Blocking."""
    from . import chat_runtime, chat_store, mission_fence, native_runtime, structured_runtime

    engine, native, adapter, _ = structured_runtime._observation_target(key, "snapshot")
    if isinstance(adapter, structured_runtime._ChatAdapter):
        path = chat_store._path(chat_runtime._root(chat_runtime._chat_provider(engine)), native)
        running_key = chat_runtime._key(engine, native)

        def chat_marker() -> tuple | None:
            try:
                st = os.stat(path, follow_symlinks=False)
                physical = mission_fence.physical_of(key)
            except Exception:  # noqa: BLE001 — unreadable is never idle
                return None
            return ("chat", physical, _stamp(st), running_key in chat_runtime.running_keys())

        return chat_marker
    if isinstance(adapter, structured_runtime._NativeAdapter):
        stamp = native_runtime._journal_stamp_sync(engine, native)

        def native_marker() -> tuple | None:
            try:
                got = stamp()  # `native_segments.active_stamp`: a stat, no lock
                physical = mission_fence.physical_of(key)
            except Exception:  # noqa: BLE001
                return None
            return None if got is None else ("native", physical, got)

        return native_marker
    return None


def _idle(snap: dict) -> bool:
    return not (snap.get("active_turn") or (snap.get("native") or {}).get("background_active"))


async def _snapshot(key: str) -> dict:
    from . import structured_runtime

    return await structured_runtime.snapshot(key)


async def _observe_now(key: str) -> Observation | None:
    reader = await asyncio.to_thread(marker_reader, key)
    if reader is None:
        return None
    marker = await asyncio.to_thread(reader)  # FIRST: anything after it moves the marker
    if marker is None:
        return None
    snap = await _snapshot(key)
    return Observation(
        key=key,
        snapshot=snap,
        marker=marker,
        idle=_idle(snap),
        at=time.monotonic(),
        reader=reader,
    )


async def observe(key: str) -> Observation | None:
    """The pre-fence observation, on the MAIN loop (see the module docstring); None when it
    could not be had in time, an earlier observation of this key still runs, or anything
    failed."""
    previous = _OUTSTANDING.get(key)
    if previous is not None and not previous.done():
        return None  # one outstanding observation per key: the next pass asks again
    # Reserved before the first await: a concurrent caller sees it and refuses.
    task = asyncio.ensure_future(_observe_now(key))
    _OUTSTANDING[key] = task

    def _release(t: asyncio.Task, k: str = key) -> None:
        if _OUTSTANDING.get(k) is t:
            del _OUTSTANDING[k]
        if not t.cancelled():
            t.exception()  # a late failure is retrieved, never logged as unhandled

    task.add_done_callback(_release)
    done, _ = await asyncio.wait({task}, timeout=OBSERVE_TIMEOUT_S)
    if not done or task.cancelled() or task.exception() is not None:
        return None  # NOT cancelled: the slot stays taken until the work really finishes
    return task.result()


def validate(obs: Observation | None) -> dict | None:
    """Asked INSIDE the fence: the observed snapshot when the session is still idle as observed,
    else None. Stat and in-process reads only (see the module docstring)."""
    if obs is None or not obs.idle:
        return None
    if time.monotonic() - obs.at > OBSERVE_MAX_AGE_S:
        return None
    try:
        now = obs.reader()
    except Exception:  # noqa: BLE001 — unreadable is never idle
        return None
    if now is None or now != obs.marker:
        return None
    return obs.snapshot
