"""How OFTEN the supervisor may spend, on a wall clock (#1214).

The supervisor loop looks at a running mission every 30 s and wakes it within seconds when one of
its sessions comes to rest (`mission_supervisor_loop`). Looking is cheap; the model is not. This is
the one place every path — the fast sweep, the early reading (#1064), the prompt wake — asks before
it reads a session, calls the model about it, retries a question, or runs the probes. Because the
bounds are per unit of TIME rather than per pass, making passes ten times as frequent does not make
them ten times as expensive.

The rules, each pinned by `tests/test_mission_pace.py`:

* **A session is READ** (its input gathered, and the fingerprint compared) when it has never been
  read, when `READ_INTERVAL_S` has passed since its last read, or when it has entered a REST EPISODE
  that has not been read yet. A continuously working session is therefore read exactly as often as
  the old five-minute sweep read it; a session that stops is read once, promptly, and then not again
  until it moves.
* **The model is CALLED** only after the existing fingerprint gate says the input moved, and at most
  `MODEL_CALLS_PER_WINDOW` times per session in any sliding `WINDOW_S`, whichever path asks.
* **An owed question is retried** at most once per (mission, objective) per `ASK_RETRY_S`.
* **The probes run** at most once per mission per `PROBE_INTERVAL_S` — their external load is what
  it was before the cadence changed.

IN MEMORY, deliberately. A restart forgets the clocks, which costs at most one extra READ per
session — and that read is still fingerprint-gated against the durable checkpoint, so an unchanged
session still makes no model call. Callers that pass no `Pace` (tests, tooling, anything outside the
loop) get today's unthrottled behaviour.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

#: How long a session that has NOT come to rest waits between reads — the old sweep interval, so a
#: working agent costs what it always cost.
READ_INTERVAL_S = 300.0
#: The sliding window the per-session model cap is counted over.
WINDOW_S = 300.0
#: Model calls one session may cause in any `WINDOW_S`, across every path.
MODEL_CALLS_PER_WINDOW = 3
#: How long a question that did not land waits before it is attempted again.
ASK_RETRY_S = 300.0
#: How long a mission's probes wait between runs.
PROBE_INTERVAL_S = 300.0

#: Bound on the in-memory maps. Keys are (mission, session) pairs of live missions, so this is only
#: ever reached by a leak; the oldest entries go first.
_MAX_KEYS = 4096


class Pace:
    """The supervisor's wall-clock spend gates. One instance per loop; thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        #: (mission, session) -> (last read at, rest episode that read covered)
        self._reads: dict[tuple[str, str], tuple[float, str | None]] = {}
        #: (mission, session) -> model call times inside the window
        self._calls: dict[tuple[str, str], deque[float]] = {}
        #: (mission, objective) -> last ask attempt
        self._asks: dict[tuple[str, str], float] = {}
        #: mission -> last probe run
        self._probes: dict[str, float] = {}
        #: session -> (engine store growth mark, when it was first seen at that mark)
        self._marks: dict[str, tuple[int, float]] = {}

    def now(self) -> float:
        return self._clock()

    # ---- reads -----------------------------------------------------------------------------

    def read_due(self, mission_id: str, session_key: str, rest: str | None) -> tuple[bool, str]:
        """May this session be read now? ``(due, why_not)``. Does not record anything.

        A session whose model budget is spent is not read at all: the read would only find input
        the model may not be shown, and whatever made it due stays PENDING (nothing is recorded),
        so the first pass after the window frees a call reads the latest screen.
        """
        t = self._clock()
        left = self._calls_left(mission_id, session_key, t)
        if left <= 0:
            return False, "model budget spent for this window; the reading stays pending"
        with self._lock:
            last = self._reads.get((mission_id, session_key))
        if last is None:
            return True, ""
        at, episode = last
        if t - at >= READ_INTERVAL_S:
            return True, ""
        if rest is not None and rest != episode:
            return True, ""
        if rest is not None:
            return False, "this rest was already read"
        return False, f"read {int(t - at)}s ago; the session is still working"

    def note_read(
        self, mission_id: str, session_key: str, rest: str | None
    ) -> tuple[float, str | None] | None:
        """Record a read as CONSUMED. Returns the previous record, for `restore_read`."""
        with self._lock:
            prev = self._reads.get((mission_id, session_key))
            self._reads[(mission_id, session_key)] = (self._clock(), rest)
            _trim(self._reads)
            return prev

    def restore_read(
        self, mission_id: str, session_key: str, prev: tuple[float, str | None] | None
    ) -> None:
        """Un-consume a read that produced no reading — refused a model call, or the call failed —
        so what made it due (a rest episode, the interval) is still pending on the next pass."""
        with self._lock:
            if prev is None:
                self._reads.pop((mission_id, session_key), None)
            else:
                self._reads[(mission_id, session_key)] = prev

    # ---- model calls -----------------------------------------------------------------------

    def take_model_call(self, mission_id: str, session_key: str) -> tuple[bool, str]:
        """Charge one model call to this session's window, or refuse. ``(allowed, why_not)``."""
        t = self._clock()
        with self._lock:
            q = self._calls.setdefault((mission_id, session_key), deque())
            while q and t - q[0] >= WINDOW_S:
                q.popleft()
            if len(q) >= MODEL_CALLS_PER_WINDOW:
                wait = int(WINDOW_S - (t - q[0])) + 1
                return (
                    False,
                    f"model budget spent: {len(q)} calls in {int(WINDOW_S)}s (next in {wait}s)",
                )
            q.append(t)
            _trim(self._calls)
            return True, ""

    def _calls_left(self, mission_id: str, session_key: str, t: float) -> int:
        with self._lock:
            q = self._calls.get((mission_id, session_key)) or deque()
            used = sum(1 for c in q if t - c < WINDOW_S)
        return MODEL_CALLS_PER_WINDOW - used

    def model_calls_in_window(self, mission_id: str, session_key: str) -> int:
        t = self._clock()
        with self._lock:
            q = self._calls.get((mission_id, session_key)) or deque()
            return sum(1 for c in q if t - c < WINDOW_S)

    # ---- asks and probes -------------------------------------------------------------------

    def take_ask(self, mission_id: str, objective_key: str) -> bool:
        """Record an ask attempt if one is due; False when the last one is too recent."""
        t = self._clock()
        with self._lock:
            last = self._asks.get((mission_id, objective_key))
            if last is not None and t - last < ASK_RETRY_S:
                return False
            self._asks[(mission_id, objective_key)] = t
            _trim(self._asks)
            return True

    def take_probes(self, mission_id: str) -> bool:
        """Record a probe run if one is due; False when the last one is too recent."""
        t = self._clock()
        with self._lock:
            last = self._probes.get(mission_id)
            if last is not None and t - last < PROBE_INTERVAL_S:
                return False
            self._probes[mission_id] = t
            _trim(self._probes)
            return True

    # ---- rest, for a session no screen clock observes ----------------------------------------

    def store_rest(self, session_key: str, mark: int, quiet_s: float) -> str | None:
        """The rest episode of a session judged by its engine STORE alone, or ``None``.

        For a session whose output this process does not observe (no ring clock), "at rest" is its
        store not having grown for `quiet_s`. The episode is the mark it rests on, so the same
        resting store is one episode however often it is asked about, and growth starts a new one.
        """
        t = self._clock()
        with self._lock:
            prev = self._marks.get(session_key)
            if prev is None or prev[0] != mark:
                self._marks[session_key] = (mark, t)
                _trim(self._marks)
                return None
            return f"store:{mark}" if t - prev[1] >= quiet_s else None


def _trim(d: dict) -> None:
    """Keep a map bounded — oldest insertion first. Called under the lock."""
    while len(d) > _MAX_KEYS:
        d.pop(next(iter(d)))


__all__ = [
    "ASK_RETRY_S",
    "MODEL_CALLS_PER_WINDOW",
    "PROBE_INTERVAL_S",
    "READ_INTERVAL_S",
    "WINDOW_S",
    "Pace",
]
