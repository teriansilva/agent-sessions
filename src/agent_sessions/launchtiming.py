"""Per-start stage timings (#1505): one INFO line per session start, nothing else.

A start (an API session's create or ``/start``, a terminal LAUNCH) opens a :class:`Recorder` in
its context. Code along the start path marks the end of each stage with :func:`mark` (or times a
block with :func:`stage`); both are no-ops when no start is being recorded, so shared helpers can
mark unconditionally. ``asyncio.to_thread`` and ``asyncio.create_task`` copy the context, so a
stage that runs in a worker thread or a background readiness task lands in the same recorder.

The line carries the engine id, the kind of start and durations only: never a path, a prompt, a
model id or a session id. It is logging, not a contract; nothing reads it back.

    launch timings engine=claude-api op=create total=1834ms stages=model:3,version:1,...,ready:1702
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar

log = logging.getLogger("agent_sessions.launchtiming")
log.setLevel(logging.INFO)

#: A stage name or a label value: short identifiers only, so nothing else can ride in the line.
_SAFE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CURRENT: ContextVar[Recorder | None] = ContextVar("launch_timings", default=None)
_HANDLER_LOCK = threading.Lock()


class Recorder:
    """The stages of one start, in order. Thread-safe: stages may end in worker threads."""

    def __init__(self, engine: str, op: str, started: float | None = None):
        self.engine = engine if _SAFE.fullmatch(engine or "") else "unknown"
        self.op = op if _SAFE.fullmatch(op or "") else "unknown"
        self.started = time.monotonic() if started is None else started
        self._last = self.started
        self._stages: list[tuple[str, float]] = []
        self._lock = threading.Lock()
        self.emitted = False

    def mark(self, stage: str) -> None:
        """End ``stage`` now: its duration is the time since the previous mark."""
        if not _SAFE.fullmatch(stage):
            return
        now = time.monotonic()
        with self._lock:
            self._stages.append((stage, (now - self._last) * 1000.0))
            self._last = now

    def skip(self) -> None:
        """Restart the stage clock without recording (time that belongs to no stage)."""
        with self._lock:
            self._last = time.monotonic()

    def line(self, **extra: str) -> str:
        with self._lock:
            stages = list(self._stages)
            total = (self._last - self.started) * 1000.0
        parts = [f"engine={self.engine}", f"op={self.op}"]
        for key, value in extra.items():
            if _SAFE.fullmatch(key) and _SAFE.fullmatch(str(value)):
                parts.append(f"{key}={value}")
        parts.append(f"total={round(total)}ms")
        parts.append("stages=" + ",".join(f"{name}:{round(ms)}" for name, ms in stages))
        return "launch timings " + " ".join(parts)

    def emit(self, **extra: str) -> None:
        """Log the line once; later calls are no-ops. Never raises into a start."""
        with self._lock:
            if self.emitted:
                return
            self.emitted = True
        with contextlib.suppress(Exception):
            _ensure_handler()
            log.info(self.line(**extra))


def _ensure_handler() -> None:
    # The server runs under uvicorn, whose logging config covers only its own loggers: an app
    # INFO record would reach no handler and be dropped. The operator reads these lines in the
    # journal, so give this one logger a stderr handler when nothing else would take it. Under a
    # configured root (tests, a log config) the record propagates as usual.
    if log.hasHandlers():
        return
    with _HANDLER_LOCK:
        if not log.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
            log.addHandler(handler)


def start(engine: str, op: str, *, started: float | None = None) -> Recorder:
    """Begin recording a start in the current context and return its recorder. ``started`` is a
    ``time.monotonic()`` reading when the start began earlier than this call."""
    recorder = Recorder(engine, op, started)
    _CURRENT.set(recorder)
    return recorder


def current() -> Recorder | None:
    return _CURRENT.get()


def mark(stage: str) -> None:
    recorder = _CURRENT.get()
    if recorder is not None:
        recorder.mark(stage)


def skip() -> None:
    recorder = _CURRENT.get()
    if recorder is not None:
        recorder.skip()


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a block as one stage (the clock restarts at its entry)."""
    skip()
    try:
        yield
    finally:
        mark(name)
