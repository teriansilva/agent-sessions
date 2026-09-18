"""Settings → Maintenance (#993): prune BattleLab's own leftovers, and archive old missions.

Manual cache prune and bulk mission archival share their runner with ``opencode_compact``
(increment 2). The opt-in schedule (increment 3) is not enabled here.

**Scope is BattleLab's own caches, never engine history** (operator decision 1 on #993). Every
category is a fixed name, and every path is derived here from the app's own stores — the client
never sends one:

* ``stale_sockets`` — ``dtach`` sockets in the runtime dir whose master is decisively dead. A name
  that does not round-trip through :func:`ptybridge.socket_path` cannot be mapped back to one
  session key, so its lock cannot be the guard, and it is left alone. The unlink itself goes
  through :func:`runtime_cleanup.unlink_stale_socket` — the single-writer-lock guard from the
  2026-06-12 wedge: a HELD lock means a new master generation owns the path.
* ``archived_scrollback`` — the persisted scrollback of archived sessions, through the same
  helper the Scrollback cache card uses (#206).

The ``hooks_void_dirs`` category that this increment originally carried is **deferred**; see the
note above :func:`_prune_archived_scrollback` for why age is not proof that a peer instance is
done with a path in a shared temp dir.

**One runner owns every job.** The single-flight slot belongs to the job TASK, not to the request
that started it: a request awaits the task through ``asyncio.shield``, so a client disconnect
cancels only the request, and the slot is released when the task is done. Blocking work inside a
job goes to ``asyncio.to_thread``; mission archival awaits ``mission_archive.archive_mission`` on
the app loop exactly as ``POST /api/missions/{id}/archive`` does, so its loop-owned fences stay
as they are.

No shell and no subprocess anywhere in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import stat
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import (
    engines,
    metadata,
    mission_archive,
    missions,
    ptybridge,
    runtime_cleanup,
    scrollback,
)

CATEGORIES: tuple[str, ...] = ("stale_sockets", "archived_scrollback")

#: A prune can meet thousands of items; the response names at most this many failures and
#: carries the true total beside them.
FAILED_CAP = 100

#: The copy for a refused concurrent submission. Refused, not queued — the client retries.
BUSY_DETAIL = "Another maintenance job is running — unavailable; retry when maintenance finishes."


# ---- the runner ---------------------------------------------------------------------------


class DiscoveryIncomplete(RuntimeError):
    """Part of a category could not be READ, so its contents are UNKNOWN.

    Raised by a measurement rather than returned as a number, because a partial count is
    indistinguishable from a complete one at the confirmation — and the prune would still delete
    what the count omitted (Hermes on PR #1000). ``problems`` carries one line per failure so a
    prune can report them instead of a clean empty sweep.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems) or "could not be read")
        self.problems = list(problems)


class MaintenanceBusy(RuntimeError):
    """A job is already running. ``info`` names it (``{"job", "started_at"}``)."""

    def __init__(self, info: dict | None) -> None:
        super().__init__(BUSY_DETAIL)
        self.info = info or {}


class Runner:
    """Single-flight owner of every maintenance job (one per app).

    The check-and-claim in :meth:`run` has no ``await`` between the check and the claim, so on
    one event loop it cannot be raced. The claim is released by the job task's done-callback —
    i.e. only once the task has actually finished, however it finished — never by the request.
    """

    def __init__(self) -> None:
        self._job: str | None = None
        self._started_at: float | None = None

    def busy_info(self) -> dict | None:
        if self._job is None:
            return None
        return {"job": self._job, "started_at": self._started_at}

    def start(self, job: str, factory: Callable[[], Awaitable[Any]]) -> asyncio.Future:
        """Claim synchronously and return the owned task (also used by 202/polling jobs)."""
        if self._job is not None:
            raise MaintenanceBusy(self.busy_info())
        self._job = job
        self._started_at = time.time()
        try:
            task = asyncio.ensure_future(factory())
        except BaseException:
            self._release()
            raise
        task.add_done_callback(self._on_done)
        return task

    async def run(self, job: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        task = self.start(job, factory)
        # Shielded: cancelling the caller (a client that went away) must not cancel the job, and
        # the slot stays claimed until `_on_done` runs.
        return await asyncio.shield(task)

    def _on_done(self, task: asyncio.Future) -> None:
        # Mark a failure as retrieved even when its request is gone, so an abandoned job never
        # logs "exception was never retrieved"; the request that is still waiting re-raises it.
        if not task.cancelled():
            task.exception()
        self._release()

    def _release(self) -> None:
        self._job = None
        self._started_at = None


# ---- small seams (tests pin them) ---------------------------------------------------------


def _session_live(session_key: str) -> bool:
    """Does this session have a live ``dtach`` master right now? (physical key, #127)."""
    try:
        engine, _, native = engines.physical_key(session_key).partition(":")
        return ptybridge.session_exists(engine, native)
    except Exception:  # noqa: BLE001 — an unresolvable key has no live terminal we could stop
        return False


class _Report:
    """Accumulates one prune's outcome: removed / freed, skips grouped by reason, failures named."""

    def __init__(self) -> None:
        self.removed = 0
        self.bytes_freed = 0
        self._skipped: dict[tuple[str, str], int] = {}
        self.failed: list[dict] = []
        self.failed_total = 0

    def skip(self, category: str, reason: str, count: int = 1) -> None:
        k = (category, reason)
        self._skipped[k] = self._skipped.get(k, 0) + count

    def fail(self, category: str, item: str, reason: str) -> None:
        self.failed_total += 1
        if len(self.failed) < FAILED_CAP:
            self.failed.append({"category": category, "item": item, "reason": reason})

    def as_dict(self) -> dict:
        return {
            "removed": self.removed,
            "bytes_freed": self.bytes_freed,
            "skipped": [
                {"category": c, "reason": r, "count": n} for (c, r), n in self._skipped.items()
            ],
            "failed": self.failed,
            "failed_total": self.failed_total,
        }


def _os_reason(e: OSError) -> str:
    return f"{type(e).__name__}: {e.strerror or e}"


# ---- stale_sockets ------------------------------------------------------------------------


def _socket_candidates() -> tuple[list[tuple[str, str, Path, int]], list[tuple[str, str]]]:
    """``((engine, native, path, size)…, problems)`` for every round-tripping socket whose master
    is DEAD.

    Only a decisive DEAD verdict qualifies — an UNKNOWN (probe timeouts on a starved host) may be
    a live master (#355). The lock guard at unlink time is still the authority; this only decides
    what is worth asking about.

    ``problems`` is ``(item, reason)`` for whatever could not be read. An unreadable runtime dir
    is not an empty one: reporting it as zero measured a *successful* sweep of nothing, while the
    prune still walks the dir (Hermes on PR #1000).
    """
    out: list[tuple[str, str, Path, int]] = []
    problems: list[tuple[str, str]] = []
    try:
        entries = list(ptybridge.runtime_dir().iterdir())
    except OSError as e:
        return out, [("the runtime dir", _os_reason(e))]
    for p in entries:
        if p.suffix != ".sock":
            continue
        engine, sep, native = p.stem.partition("-")
        if not (sep and engine and native) or engines.get(engine) is None:
            continue
        try:
            if ptybridge.socket_path(engine, native) != p:
                continue  # sanitised / ambiguous — cannot name ONE session, so no lock guards it
        except ptybridge.PtyBridgeError:
            continue
        try:
            st = p.lstat()
        except FileNotFoundError:
            continue  # it vanished mid-scan — genuinely gone, not unreadable
        except OSError as e:
            problems.append((p.name, _os_reason(e)))
            continue
        if not stat.S_ISSOCK(st.st_mode):
            continue
        if ptybridge.probe_master(p) is not ptybridge.DEAD:
            continue
        out.append((engine, native, p, st.st_size))
    return out, problems


def _prune_sockets(report: _Report) -> None:
    candidates, problems = _socket_candidates()
    # A scan that could not read everything is reported as a failure, never as a clean sweep:
    # `candidates` is then a SUBSET, and "removed 0" would read as "there was nothing there".
    for item, reason in problems:
        report.fail("stale_sockets", item, reason)
    for engine, native, path, size in candidates:
        try:
            removed = runtime_cleanup.unlink_stale_socket(engine, native, f"{engine}:{native}")
        except OSError as e:
            report.fail("stale_sockets", path.name, _os_reason(e))
            continue
        if removed:
            report.removed += 1
            report.bytes_freed += size
        elif path.exists():
            report.skip("stale_sockets", "a live session holds its lock")
        # else: it vanished between the scan and the unlink — nothing to report


# NOTE — the `hooks_void_dirs` category is DEFERRED, not forgotten (Hermes on PR #1000).
#
# `gitwrite.hooks_void()` creates one empty 0500 dir per process in the SHARED temp dir and never
# removes it, so the backlog is real (~25k on the author's host). But removing another app
# instance's dir frees its NAME in a directory any local user can write: the peer's `hooks_void()`
# only re-checks `isdir`, so a replacement dir planted under that name would be handed to git as
# `core.hooksPath` — a local hook-injection path, reproduced in review. Age is not proof that a
# peer is done with a path.
#
# Pruning them safely needs cross-process lifetime ownership (a lock held for the dir's life, and
# an owner-only parent directory), which is a change to how `hooks_void` creates them — its own
# issue, not a category here. #993 keeps the backlog untouched until that exists.


# ---- archived_scrollback ------------------------------------------------------------------


def _prune_archived_scrollback(report: _Report) -> None:
    try:
        keys, problems = scrollback.archived_keys_checked()
    except OSError as e:
        report.fail("archived_scrollback", "the archived-session scan", _os_reason(e))
        return
    # Same rule as the socket scan: an engine store that would not open leaves `keys` a SUBSET, so
    # the failure is named rather than reported as "nothing was archived".
    for problem in problems:
        report.fail("archived_scrollback", "the archived-session scan", problem)
    for logical, key in keys:
        # Discovery is a SNAPSHOT, and this runs in a worker thread while ordinary archive and
        # unarchive requests keep being served — so a session can be unarchived between being
        # discovered and being deleted (Hermes on PR #1000, review 4898). Each key is therefore
        # re-checked while HOLDING the sidecar lock every writer takes, and the unlink happens
        # inside that hold: a transition has either already committed (and we see it here) or it
        # waits for us (and by then the cache of a then-archived session is gone, which is the
        # feature). One key per hold — never across the sweep, which would stall every archive,
        # unarchive and rename in the app for its duration.
        #
        # `unset` stays eligible: it means the sidecar has never been told about this session, and
        # every in-app transition writes an explicit value (`archive` → true, `unarchive` → false),
        # so nothing can have unarchived it since discovery. Only `active` and `unreadable` block.
        # BOTH identities, logical first: an aliased OpenCode session records its unarchive under
        # the real id while its cache is keyed by the placeholder, so a fence that knew only the
        # physical key read `unset` and deleted an active session (review 4915/4919, finding 1).
        with metadata.archive_state_held(logical, key) as state:
            if state == "unreadable":
                report.fail("archived_scrollback", key, "its archive state could not be re-checked")
                continue
            if state == "active":
                report.skip("archived_scrollback", "the session was unarchived while pruning")
                continue
            try:
                out = scrollback.clear_scrollback([key])
            except OSError as e:
                report.fail("archived_scrollback", key, _os_reason(e))
                continue
            report.removed += int(out.get("removed", 0))
            report.bytes_freed += int(out.get("bytes_freed", 0))
            # `clear_scrollback` is best-effort by contract: it does not raise for a file it could
            # not delete, and it counts a mirror's bytes only after the unlink lands. Its `failed`
            # list is the only way a refused delete reaches the operator instead of being reported
            # as a clean sweep with bytes that were never freed.
            for f in out.get("failed") or []:
                report.fail(
                    "archived_scrollback",
                    str(f.get("key") or key),
                    str(f.get("reason") or "could not be deleted"),
                )


# ---- public: dry run + prune (blocking; call via to_thread) -------------------------------


def dry_run_caches() -> dict[str, dict]:
    """``{category: {"items", "bytes"}}`` — nothing is changed. A category that cannot be measured
    carries ``error`` rather than a misleading zero."""
    measures: dict[str, Callable[[], tuple[int, int]]] = {
        "stale_sockets": _sockets_measure,
        "archived_scrollback": _scrollback_measure,
    }
    out: dict[str, dict] = {}
    for name in CATEGORIES:
        try:
            items, nbytes = measures[name]()
            out[name] = {"items": items, "bytes": nbytes}
        except Exception as e:  # noqa: BLE001 — one unreadable store must not blank the others
            out[name] = {"items": 0, "bytes": 0, "error": type(e).__name__}
    return out


def _count(rows: list[tuple[str, int]]) -> tuple[int, int]:
    return len(rows), sum(size for _p, size in rows)


# Both measures below raise `DiscoveryIncomplete` the moment any part of the category could not be
# read, rather than returning what they managed to see: `dry_run_caches` turns that into `error`,
# which is the state the card's confirmation guard blocks on.
def _sockets_measure() -> tuple[int, int]:
    rows, problems = _socket_candidates()
    if problems:
        raise DiscoveryIncomplete([f"{item}: {reason}" for item, reason in problems])
    return _count([(str(p), size) for _e, _n, p, size in rows])


def _scrollback_measure() -> tuple[int, int]:
    # `archived_keys_checked` answers in (logical, physical) PAIRS: under the alias layer (#127)
    # the fence must re-check the LOGICAL id — that's where an override is written — while the
    # bytes live under the PHYSICAL one. Measure the physical half, so the preview counts exactly
    # the files `_prune_archived_scrollback` would delete and the two halves cannot disagree.
    pairs, problems = scrollback.archived_keys_checked()
    stats = scrollback.cache_stats_for([physical for _logical, physical in pairs])
    problems = problems + list(stats.get("unreadable") or [])
    if problems:
        raise DiscoveryIncomplete(problems)
    return stats["items"], stats["bytes"]


_PRUNERS: dict[str, Callable[[_Report], None]] = {
    "stale_sockets": _prune_sockets,
    "archived_scrollback": _prune_archived_scrollback,
}


def prune_caches(categories: list[str]) -> dict:
    """Remove the selected categories. Returns ``{removed, bytes_freed, skipped, failed,
    failed_total}``. Unknown names raise ``ValueError`` — the route validates before calling."""
    unknown = [c for c in categories if c not in _PRUNERS]
    if unknown:
        raise ValueError(f"unknown maintenance categories: {unknown}")
    report = _Report()
    for name in CATEGORIES:  # a fixed order, whatever order the client sent
        if name in categories:
            _PRUNERS[name](report)
    return report.as_dict()


# ---- missions -----------------------------------------------------------------------------


async def _roster(mission_id: str) -> list[str]:
    """What a NEW archive would tear down — `begin_archive`'s own eligibility, never the last
    archive's (Hermes on PR #1000): a row an earlier attempt left `skipped` is cleared and
    re-evaluated, so `sessions_governed_by_archive` would preview 0 and then archive one."""
    return await missions.run_admitted(lambda: missions.archive_roster_preview(mission_id))


#: Settlement states that mean the session's transcript is in the archive now.
_SETTLED_ARCHIVED = ("done", "already_archived")


async def _settled(mission_id: str) -> list[dict]:
    return await missions.run_admitted(lambda: missions.archive_sessions_for(mission_id))


async def _live(keys: list[str]) -> list[str]:
    return await asyncio.to_thread(lambda: [k for k in keys if _session_live(k)])


async def missions_dry_run(older_than_days: int) -> dict:
    """What ``archive_old_missions`` would do now: ``{eligible, sessions, live_sessions,
    unresolved}``. Read-only."""
    cands = await missions.run_admitted(
        lambda: missions.archive_candidates(older_than_days * 86400.0)
    )
    sessions = 0
    live = 0
    unresolved: list[str] = []
    for c in cands:
        keys = await _roster(c["id"])
        sessions += len(keys)
        live += len(await _live(keys))
        if c["unresolved"]:
            unresolved.append(c["id"])
    return {
        "eligible": len(cands),
        "sessions": sessions,
        "live_sessions": live,
        "unresolved": unresolved,
    }


async def archive_old_missions(older_than_days: int) -> dict:
    """Archive every terminal-state mission older than N days, one at a time, never abandoning.

    Each goes through :func:`mission_archive.archive_mission` with ``abandon=False`` — its own
    fences decide: a mission that stopped being archivable (an unresolved turn, a concurrent
    archive) answers 409 and is reported under ``skipped``; a session that could not be archived
    does not abort its mission, which archives and is reported under ``failed`` with that session.
    """
    cands = await missions.run_admitted(
        lambda: missions.archive_candidates(older_than_days * 86400.0)
    )
    result: dict[str, Any] = {
        "archived": 0,
        "sessions_archived": 0,
        "terminals_stopped": 0,
        "skipped": [],
        "failed": [],
    }
    for c in cands:
        mid = c["id"]
        try:
            keys = await _roster(mid)
            live_before = await _live(keys)
        except missions.MissionError as e:
            result["failed"].append({"mission_id": mid, "session_key": None, "reason": str(e)})
            continue
        try:
            await mission_archive.archive_mission(mid, abandon=False)
        except missions.MissionError as e:
            bucket = "skipped" if e.status == 409 else "failed"
            entry = {"mission_id": mid, "reason": str(e)}
            if bucket == "failed":
                entry["session_key"] = None
            result[bucket].append(entry)
            continue
        except Exception as e:  # noqa: BLE001 — one mission's crash must not stop the sweep
            result["failed"].append(
                {
                    "mission_id": mid,
                    "session_key": None,
                    "reason": f"archive raised {type(e).__name__}; it resumes on restart",
                }
            )
            continue
        result["archived"] += 1
        # Counted from the sessions this archive ACTUALLY settled, never from the roster it
        # previewed (Hermes on PR #1000): the roster is what we expected to touch, the settlement
        # rows are what happened — `already_archived` counts, `failed` does not, and a row another
        # mission holds settles `skipped` and is nobody's success.
        settled = await _settled(mid)
        result["sessions_archived"] += sum(
            1 for r in settled if r.get("archive_state") in _SETTLED_ARCHIVED
        )
        for r in settled:
            if r.get("archive_state") == "failed":
                result["failed"].append(
                    {
                        "mission_id": mid,
                        "session_key": r.get("session_key"),
                        "reason": r.get("archive_error") or "not archived",
                    }
                )
        still_live = set(await _live(live_before))
        result["terminals_stopped"] += sum(1 for k in live_before if k not in still_live)
    with contextlib.suppress(Exception):
        engines.invalidate_scan_cache()
    return result
