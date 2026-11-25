"""Observed-idle OpenCode compaction, fenced against BattleLab launches (#993, #1040).

Only VACUUM and its separately reported checkpoint write to the provider's database. The path
comes from server configuration, never a request. External launchers are not fenced: SQLite's
own transaction/locking guarantees still apply. An unreadable process scan is a refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path

from . import discover, maintenance, opencode_admission

PROC = Path("/proc")
SCAN_SECONDS = 2.0
SQL_TIMEOUT = 0.5


def targets() -> list[str]:
    """Every engine this maintenance kind can compact (#853 P3)."""
    return opencode_admission.maintained_engines()


def _target(engine: str | None = None):
    """The provider whose store is compacted: ``engine`` when it selects this kind, else — when
    no engine is named — the default target. An engine that does not select it is None, never
    silently another engine's store."""
    from . import engines

    eid = engine if engine is not None else opencode_admission.maintained_engine()
    return engines.get(eid) if eid in targets() else None


def database_path(engine: str | None = None) -> Path:
    prov = _target(engine)
    db = prov.store_path("db") if prov is not None else None
    if db is None:
        raise FileNotFoundError("no engine selects store maintenance")
    return Path(db).expanduser().resolve()


def _size(path: Path, *, optional: bool = False) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        if optional:
            return 0
        raise


def sqlite_temp_dir() -> Path:
    """SQLite's Unix temp-file search order, without mutating its process-global setting.

    https://sqlite.org/tempfiles.html#temporary_file_storage_locations . This app never sets
    sqlite3_temp_directory/PRAGMA temp_store_directory; changing it is unsafe across threads.
    """
    for name in (
        os.environ.get("SQLITE_TMPDIR"),
        os.environ.get("TMPDIR"),
        "/var/tmp",  # noqa: S108 — inspect SQLite's documented search path; no file creation
        "/usr/tmp",
        "/tmp",  # noqa: S108 — inspect SQLite's documented search path; no file creation
        ".",
    ):
        if name and Path(name).is_dir() and os.access(name, os.W_OK | os.X_OK):
            return Path(name).resolve()
    raise OSError("no writable SQLite temporary directory")


def holders(path: Path, engine: str | None = None) -> dict:
    """Observe DB/WAL/SHM descriptors and OpenCode launch command lines; unknown stays busy.

    Compare inode identities, including aliases, and do not omit our own process: a concurrent
    BattleLab reader is a holder too. This runs before opening our measurement connection.
    No UID filter can prove a process is unable to hold the file, so inaccessible processes
    remain unknown, including on hosts whose /proc permissions prevent a complete scan.
    """
    found: set[int] = set()
    unknown: set[int] = set()
    deadline = time.monotonic() + SCAN_SECONDS
    targets = set()
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            st = candidate.stat()
            targets.add((st.st_dev, st.st_ino))
        except FileNotFoundError:
            continue
    prov = _target(engine)
    binary = (
        (discover.resolve(prov.engine_id) or prov.manifest.binary.name)
        if prov is not None and prov.manifest.binary is not None
        else ""
    )
    binaries = {os.fsencode(binary), os.fsencode(os.path.realpath(binary))} if binary else set()
    try:
        processes = list(PROC.iterdir())
    except OSError:
        return {"pids": [], "unknown": True, "unknown_processes": 0, "scan_incomplete": True}
    incomplete = False
    for proc in processes:
        if not proc.name.isdecimal():
            continue
        if time.monotonic() >= deadline:
            incomplete = True
            break
        pid = int(proc.name)
        try:
            if binaries.intersection((proc / "cmdline").read_bytes().split(b"\0")):
                found.add(pid)
            for fd in (proc / "fd").iterdir():
                if time.monotonic() >= deadline:
                    incomplete = True
                    break
                try:
                    st = fd.stat()
                except FileNotFoundError:
                    continue
                if (st.st_dev, st.st_ino) in targets:
                    found.add(pid)
                    break
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            unknown.add(pid)
    return {
        "pids": sorted(found),
        "unknown": bool(unknown) or incomplete,
        "unknown_processes": len(unknown),
        "scan_incomplete": incomplete,
    }


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    con = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=SQL_TIMEOUT)
    con.execute("PRAGMA busy_timeout=500")
    return con


def measure(
    path: Path | None = None, *, engine: str | None = None, admission_held: bool = False
) -> dict:
    """Report every independently measurable blocker. Never promote missing data to zero."""
    out: dict = {
        "available": False,
        "db_bytes": None,
        "wal_bytes": None,
        "reclaimable_bytes": None,
        "holders": None,
        "disk": None,
        "blockers": [],
    }

    def block(code: str, detail: str) -> None:
        out["blockers"].append({"code": code, "detail": detail})

    try:
        path = path or database_path(engine)
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            block("database", "The database must be a regular file owned by this user.")
            return out
        out["db_bytes"] = st.st_size
    except FileNotFoundError:
        block("missing", "No OpenCode database exists on this host.")
        return out
    except OSError:
        block("database", "The OpenCode database could not be inspected.")
        return out

    if not admission_held:
        try:
            gate = opencode_admission.acquire(engine=engine, exclusive=True)
            if gate is None:
                block("admission", "An OpenCode launch or compaction is in progress.")
            else:
                gate.release()
        except OSError:
            block("admission_unknown", "OpenCode launch admission could not be checked.")
    try:
        out["holders"] = holders(path, engine)
        if out["holders"]["pids"]:
            block("held", "Processes are using the OpenCode database or launching OpenCode.")
        if out["holders"]["unknown"]:
            block(
                "holders_unknown", "Some processes could not be inspected; idle state is unknown."
            )
    except OSError:
        block("holders_unknown", "Database holders could not be inspected; idle state is unknown.")
    try:
        with contextlib.closing(_connect(path, write=False)) as con:
            con.execute("PRAGMA query_only=ON")
            if con.execute("PRAGMA journal_mode").fetchone()[0].lower() in ("off", "memory"):
                block("journal", "The database does not use a durable journal mode.")
            pages = con.execute("PRAGMA freelist_count").fetchone()[0]
            size = con.execute("PRAGMA page_size").fetchone()[0]
            out["reclaimable_bytes"] = pages * size
        if out["reclaimable_bytes"] == 0:
            block("nothing", "There are no free database pages to reclaim.")
    except (OSError, sqlite3.Error):
        block("measurement", "Reclaimable database space could not be measured.")
    try:
        wal = _size(Path(str(path) + "-wal"), optional=True)
        out["wal_bytes"] = wal
        temp = sqlite_temp_dir()
        shared = path.stat().st_dev == temp.stat().st_dev
        total = out["db_bytes"] + wal
        db_need, temp_need = (3 * total, 0) if shared else (2 * total, out["db_bytes"])
        db_free = shutil.disk_usage(path.parent).free
        temp_free = db_free if shared else shutil.disk_usage(temp).free
        out["disk"] = {
            "shared_filesystem": shared,
            "database_required": db_need,
            "database_free": db_free,
            "temp_required": temp_need,
            "temp_free": temp_free,
        }
        if db_free < db_need:
            block("database_space", "There is not enough free space on the database filesystem.")
        if temp_free < temp_need:
            block(
                "temp_space", "There is not enough free space on the SQLite temporary filesystem."
            )
    except OSError:
        block(
            "disk_unknown", "The database/WAL size or available disk space could not be measured."
        )
    out["available"] = not out["blockers"]
    return out


class Worker:
    """Thread-owned mutation and connection lifetime, with a safe cross-thread interrupt."""

    def __init__(self, engine: str | None = None):
        #: The engine whose store this worker compacts — carried through admission, holders,
        #: measurement and the VACUUM itself, so every step is about the same database.
        self.engine = engine
        self.stop = threading.Event()
        self._mutex = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def interrupt(self) -> None:
        self.stop.set()
        # SQLite permits interrupt from another thread, but never concurrently with close.
        # https://sqlite.org/c3ref/interrupt.html
        with self._mutex:
            if self._connection is not None:
                self._connection.interrupt()

    def run(self, ready, phase) -> dict:
        result: dict = {
            "state": "refused",
            "vacuum": "not_started",
            "checkpoint": "not_started",
            "checkpoint_result": None,
            "bytes_freed": None,
            "blockers": [],
        }
        gate = None
        con = None
        try:
            gate = opencode_admission.acquire(engine=self.engine, exclusive=True)
            if gate is None:
                result["blockers"] = [
                    {
                        "code": "admission",
                        "detail": "An OpenCode launch or compaction is in progress.",
                    }
                ]
                return result
            path = database_path(self.engine)
            info = measure(path, engine=self.engine, admission_held=True)
            result["blockers"] = info["blockers"]
            if not info["available"]:
                return result
            if self.stop.is_set():
                result["state"] = "interrupted"
                return result
            con = _connect(path, write=True)
            # Retain the file's journal mode; refuse modes without a durable rollback/WAL.
            if con.execute("PRAGMA journal_mode").fetchone()[0].lower() in ("off", "memory"):
                result["blockers"] = [
                    {
                        "code": "journal",
                        "detail": "The database does not use a durable journal mode.",
                    }
                ]
                return result
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA temp_store=FILE")
            con.set_progress_handler(lambda: int(self.stop.is_set()), 1000)
            with self._mutex:
                self._connection = con
            if self.stop.is_set():
                result["state"] = "interrupted"
                return result
            ready(True)
            phase("vacuum")
            result["state"] = "running"
            try:
                con.execute("VACUUM")
            except sqlite3.Error:
                result.update(
                    state="interrupted" if self.stop.is_set() else "failed", vacuum="rolled_back"
                )
                return result
            result["vacuum"] = "done"
            phase("checkpoint")
            try:
                if self.stop.is_set():
                    raise sqlite3.OperationalError("shutdown")
                checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                result["checkpoint_result"] = list(checkpoint)
                result["checkpoint"] = "deferred" if checkpoint[0] else "done"
            except sqlite3.Error:
                result["checkpoint"] = "failed"
            result["state"] = "done"
            # The checkpoint's outcome is independent of VACUUM's committed result.
            with self._mutex:
                self._connection = None
                con.close()
                con = None
            try:
                after = _size(path) + _size(Path(str(path) + "-wal"), optional=True)
                result["bytes_freed"] = max(0, info["db_bytes"] + info["wal_bytes"] - after)
            except OSError:
                pass  # a committed compaction with unknown byte accounting stays committed
            return result
        except (OSError, sqlite3.Error):
            result["blockers"] = [
                {
                    "code": "unavailable",
                    "detail": "The database or maintenance admission could not be opened.",
                }
            ]
            return result
        finally:
            # Closing the connection belongs to this worker; interrupt takes the same mutex.
            try:
                with self._mutex:
                    self._connection = None
                    if con is not None:
                        con.close()
            finally:
                if gate is not None:
                    gate.release()


class Service:
    """One retained job result per app; the existing runner owns the actual job task."""

    def __init__(self, runner: maintenance.Runner):
        self.runner = runner
        self.job: dict | None = None
        self.worker: Worker | None = None
        self.task: asyncio.Task | None = None
        self.closing = False

    def snapshot(self) -> dict | None:
        return copy.deepcopy(self.job)

    async def start(self, engine: str | None = None) -> tuple[bool, dict]:
        if self.closing:
            raise maintenance.MaintenanceBusy({"job": "shutdown", "started_at": None})
        # Claim before publishing/replacing a job, without yielding between the two.
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        target = engine if engine is not None else opencode_admission.maintained_engine()
        worker = Worker(target)
        job = {
            "id": uuid.uuid4().hex,
            "engine": target,
            "state": "checking",
            "started_at": time.time(),
            "finished_at": None,
            "result": None,
        }

        def accepted(value: bool) -> None:
            def publish() -> None:
                if not ready.done():
                    ready.set_result(value)

            loop.call_soon_threadsafe(publish)

        def phase(value: str) -> None:
            loop.call_soon_threadsafe(job.update, {"state": value})

        async def run() -> dict:
            # Keep the executor Future directly: loop shutdown cancels all Tasks, including
            # a to_thread wrapper, before its OS thread exits. Shield this Future instead.
            task = loop.run_in_executor(None, worker.run, accepted, phase)
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._interrupt(worker)
                while not task.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(task)
                try:
                    result = task.result()
                except Exception:
                    result = self._failed_result()
            except Exception:
                result = self._failed_result()
            job.update(state=result["state"], result=result, finished_at=time.time())
            if not ready.done():
                ready.set_result(False)
            return result

        task = self.runner.start("opencode_compact", run)
        self.job, self.worker, self.task = job, worker, task
        ok = await asyncio.shield(ready)
        return ok, self.snapshot()

    @staticmethod
    def _failed_result() -> dict:
        return {
            "state": "failed",
            "vacuum": "unknown",
            "checkpoint": "not_started",
            "checkpoint_result": None,
            "bytes_freed": None,
            "blockers": [
                {"code": "failed", "detail": "The compaction outcome could not be established."}
            ],
        }

    @staticmethod
    async def _interrupt(worker: Worker) -> None:
        # interrupt shares a mutex with close(), which can perform SQLite I/O. Acquiring
        # that mutex on the event loop could freeze every request during a slow close.
        worker.stop.set()
        pending = asyncio.get_running_loop().run_in_executor(None, worker.interrupt)
        while not pending.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(pending)
        with contextlib.suppress(Exception):
            pending.result()

    async def shutdown(self) -> None:
        self.closing = True
        if self.worker is not None:
            await self._interrupt(self.worker)
        if self.task is not None:
            while not self.task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(self.task)
