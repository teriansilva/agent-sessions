"""Shared AI-task activity registry + per-kind single-flight (#441 Phase 1).

A tiny in-process surface two things ride on:

* **Single-flight** — :func:`single_flight` lets at most ONE task of a given ``kind`` run
  at a time. A second attempt while one is in flight raises :class:`AlreadyRunning`, so a
  manual Pulse scan can never overlap a background Pulse scan (the issue's "two scans never
  overlap" guarantee). It is intentionally **per-process**: the app is single-admin /
  single-instance and the background loops are already per-instance, so cross-process
  locking is out of scope.
* **Observability** — every AI task (Pulse scans, AI-review sweeps, auto-sort sweeps, and
  the on-demand reviews/sorts) runs inside :func:`track` / :func:`single_flight`, so
  :func:`snapshot` can report what is running right now plus the last run per kind. That is
  the data behind the Settings "AI activity" panel, and the home for the platform's other
  AI features as they land (project sorting, …).

No lock is needed for the check-and-register: asyncio is single-threaded and the register
step never awaits between the "is this kind already running?" test and the insert, so it is
atomic against other coroutines. Cleanup runs in a ``finally``, so a task that raises still
deregisters and still records its (failed) last-run.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import count

__all__ = ["AlreadyRunning", "track", "single_flight", "snapshot", "is_running", "reset"]


class AlreadyRunning(Exception):
    """A single-flight task of this ``kind`` is already in flight (maps to HTTP 409)."""

    def __init__(self, kind: str):
        super().__init__(f"{kind} is already running")
        self.kind = kind


@dataclass
class _Running:
    token: int
    kind: str
    detail: str
    started_at: float


# Process-global: the app is single-instance, so a module-level registry is the whole story.
_running: dict[int, _Running] = {}
_last: dict[str, dict] = {}
_ids = count(1)


def is_running(kind: str) -> bool:
    """True iff a task of ``kind`` is currently tracked as running."""
    return any(r.kind == kind for r in _running.values())


ERROR_MAX = 200


def _clamp_error(e: BaseException) -> str:
    """A one-line, bounded rendering of a failure: `ReviewError: endpoint returned HTTP 500`."""
    msg = " ".join(str(e).split())[:ERROR_MAX]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


@asynccontextmanager
async def track(kind: str, detail: str = "", *, exclusive: bool = False):
    """Run the wrapped block as a tracked AI task of ``kind``.

    With ``exclusive=True`` (see :func:`single_flight`) it raises :class:`AlreadyRunning`
    when another task of the same kind is already in flight — the check-and-register is
    atomic because nothing awaits between them. The block is visible to :func:`snapshot`
    for its whole life and is deregistered in a ``finally``; the per-kind last-run
    (``ok`` / ``finished_at`` / ``duration_s``) is recorded on exit whether the block
    returned or raised.
    """
    if exclusive and is_running(kind):
        raise AlreadyRunning(kind)
    token = next(_ids)
    started = time.time()
    _running[token] = _Running(token, kind, detail, started)
    ok = True
    err: str | None = None
    try:
        yield
    except BaseException as e:  # noqa: BLE001 — recorded, then re-raised unchanged
        ok = False
        # WHY it failed, not just that it did. The orchestrator's endpoint went down for 11
        # hours and the only trace was a journal traceback: the operator saw an empty page and
        # concluded the feature was broken (#772). A run that failed has to be able to say so.
        # Clamped, and rendered as plain text by the client — this string is a remote
        # endpoint's response body, not ours.
        err = _clamp_error(e)
        raise
    finally:
        _running.pop(token, None)
        prev = _last.get(kind) or {}
        finished = time.time()
        _last[kind] = {
            "finished_at": finished,
            "ok": ok,
            "detail": detail,
            "duration_s": round(finished - started, 3),
            # None once it has succeeded — a stale error next to `ok: true` reads as a fault
            # that is still happening.
            "error": None if ok else err,
            # A single failure is a blip; a run of them is an outage. The client needs the
            # count to tell those apart without inventing a rule of its own.
            "consecutive_failures": 0 if ok else int(prev.get("consecutive_failures") or 0) + 1,
            # Carried across failures, so "failing since" is answerable at a glance rather
            # than by digging for the last successful pass.
            "last_ok": finished if ok else prev.get("last_ok"),
        }


def single_flight(kind: str, detail: str = ""):
    """``track(kind, exclusive=True)`` — at most one task of ``kind`` runs at a time."""
    return track(kind, detail, exclusive=True)


def snapshot() -> dict:
    """Live AI-task state for ``GET /api/ai/activity`` and the Settings panel: every running
    task (kind, detail, started_at) sorted oldest-first, plus the last-run summary per kind."""
    running = sorted(
        (
            {"kind": r.kind, "detail": r.detail, "started_at": r.started_at}
            for r in _running.values()
        ),
        key=lambda r: r["started_at"],
    )
    return {"running": running, "last": {k: dict(v) for k, v in _last.items()}}


def reset() -> None:
    """Drop all state. Test hook only — the registry is process-global."""
    _running.clear()
    _last.clear()
