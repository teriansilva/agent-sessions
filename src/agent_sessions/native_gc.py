"""Settings → Maintenance → "Ended agent worker records" (#2415): the ``native_worker_state`` prune.

An API session's worker generation leaves ``<native state>/workers/<worker id>/`` behind for good
(``native_state``): its lifecycle, and its config until the generation is proved gone. Nothing
reclaims a generation that has ended and that nothing names any more, so they pile up (#2413).
This module removes exactly those, and nothing else.

**What qualifies** (every fact must hold, and the deciding ones are re-read at removal time):

* **unreferenced** — the id appears nowhere in any session record. The referenced set is every
  UUID-shaped string anywhere in every record, not only ``workers`` and ``current_worker``: the
  launch and start views name a generation too (``launch.gen``, ``start.gen``) and are read back
  through its lifecycle (``native_runtime`` reads ``phase == "ready"`` for a ``starting`` gen), so a
  narrower set would delete a dir something still asks about. The set is built ONCE per pass, and
  ONE unreadable record skips the whole category: a subset of the records is not proof of anything.
* **ended** — under the worker's own ``lifecycle.lock`` (the sidecar lock ``update_lifecycle``
  takes), the lifecycle says ``phase == "gone"`` and there is no ``config.json``. Only ``gone`` is
  cgroup-proved, and the runtime forgets the config exactly when it proves ``gone``.
* **no history uses it** — a gone generation's lifecycle is also where the transcript reads WHY it
  ended (``native_runtime._ended_reason``: an OOM kill, an unwritable record). A dir whose
  lifecycle carries any of the facts that function reads (a superset test, so never narrower) is
  kept, and removing a dir never changes what any session shows.
* **its unit is over** — systemd answers ``not-found`` for its unit, or the unit is inactive with
  an empty or absent cgroup, from at most one unit query per dir that passed the file checks
  (batched on the real host). A failed, partial or unknown answer keeps the dir; unknown never
  authorises.

**Why "unreferenced" stays true through removal.** Worker ids are minted fresh
(``WorkerIdentity.mint()``, a uuid4) at launch under the session lifecycle lock, and ``config.json``
plus the ``intent`` lifecycle are written BEFORE the record names the id. A ``gone`` id is never
written back into a record and never gets a new config; peer app instances mint their own ids the
same way. So a reference can only appear for an id that also has a config, which the under-lock
re-check refuses. The referenced set is additionally rebuilt after discovery (and the unit probes)
and before the first removal, so the window between the two is as short as one pass of reads.

**Removal** is rename-then-delete under the worker's lock: ``workers/<id>`` becomes
``workers/.gc-<id>``, a non-UUID name that every reader ignores, and is then removed
descriptor-relative (``O_NOFOLLOW``, an owner check on every entry, no symlink followed). Bytes are
counted per file actually unlinked. A partial removal reports the bytes it freed and a failure, and
leaves ``.gc-<id>`` for the next pass to finish; it never claims the whole dir's size.

**Bounded cost** (the Phase 1 budget): the native state dir is verified ONCE per pass and every read
after that is descriptor-relative, so a pass costs one open per file rather than a checked walk of
every path component. Unit queries are batched on the real host (one ``systemctl show`` answers
up to :data:`_PROBE_BATCH` units, so never more than one query per file-eligible dir) on a small
pool, and the pass's own Python work is paced to a duty cycle (:class:`_Pacer`) so it cannot
starve the app's event loop of the GIL it shares with it.

Blocking throughout: the maintenance routes call it through ``asyncio.to_thread``. No shell, no
subprocess here; systemd is asked through ``native_runtime.HOST``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import native_containment, native_runtime, native_state
from .plugins import storage

CATEGORY = "native_worker_state"

#: The prefix a dir is renamed to before it is deleted. Not a UUID, so no reader resolves it.
GC_PREFIX = ".gc-"

#: Bounded: a lifecycle is a few hundred bytes; anything larger is not one of ours.
_MAX_LIFECYCLE_BYTES = native_state.MAX_RECORD_BYTES
#: How long removal waits for a worker's lifecycle lock before leaving the dir for the next pass.
_LOCK_WAIT = 2.0
#: A worker dir is flat today; a deeper tree is not ours to walk.
_MAX_DEPTH = 4
#: Concurrent unit queries. Each is a short-lived read-only ``systemctl show``.
_PROBE_WORKERS = 4
#: Units per query on the real host (one ``systemctl show`` answers many; see ``_probe_batch``).
_PROBE_BATCH = 64
#: The share of wall time the pass's own loops may run before resting (see :class:`_Pacer`).
_DUTY = 0.5
#: How much CPU the pass spends before it rests, so a rest is never shorter than a scheduler tick.
_SLICE = 0.01

_DASHED = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")

# Skip reasons, as the operator reads them (grouped by reason in the prune result).
SKIP_PROBE = "systemd could not be asked about its worker, so it was kept"
SKIP_UNIT = "its worker's systemd unit is still loaded"
SKIP_REFERENCED = "a session started naming it while pruning"
SKIP_CHANGED = "its worker record changed while pruning"
SKIP_BUSY = "its worker record was busy"
SKIP_UNREADABLE = "its worker record could not be read, so it was kept"


class RecordsUnreadable(RuntimeError):
    """The session records could not all be read, so no dir can be proved unreferenced."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems) or "the session records could not be read")
        self.problems = list(problems)


@dataclass
class Outcome:
    """One prune pass, folded into ``maintenance._Report`` by the caller."""

    removed: int = 0
    bytes_freed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)
    #: Unit queries this pass made (the budget: at most one per file-eligible dir).
    unit_queries: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


# ---- test seams -------------------------------------------------------------------------------


def _after_discovery() -> None:
    """Between discovery (and the unit probes) and the second reference read. Tests put a barrier
    here to make a reference or a config appear after the dir was found eligible."""


def _before_remove(worker_id: str) -> None:
    """Under the worker's lock, after the re-check and before the rename (tests only)."""


def _unlink_at(name: str, dfd: int) -> None:
    """One file's unlink (a seam: tests make one fail to prove partial accounting)."""
    os.unlink(name, dir_fd=dfd)


# ---- pacing -----------------------------------------------------------------------------------


class _Pacer:
    """Rest after every :data:`_SLICE` of this thread's CPU time, so its own Python work is at
    most :data:`_DUTY` of the CPU it gets. The app's event loop shares this process's GIL with the
    pass's thread; without a rest, a pass's few CPU-seconds are a few seconds of slower requests.

    CPU time, not wall time: under contention a wall clock counts the wait for the GIL as work and
    rests for it too, which slowed the pass several-fold without protecting anyone."""

    def __init__(self) -> None:
        self._since = time.thread_time()

    def tick(self) -> None:
        busy = time.thread_time() - self._since
        if busy >= _SLICE and 0 < _DUTY < 1:
            time.sleep(busy * (1 - _DUTY) / _DUTY)
            self._since = time.thread_time()


# ---- descriptor-relative reads ----------------------------------------------------------------


def _reason(e: OSError) -> str:
    return f"{type(e).__name__}: {e.strerror or e}"


def _open_dir_at(parent_fd: int, name: str) -> int:
    fd = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
    )
    st = os.fstat(fd)
    if st.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError(errno.EPERM, "owned by another user")
    return fd


def _read_json_at(dfd: int, name: str, limit: int = _MAX_LIFECYCLE_BYTES) -> dict | None:
    """``storage.read``'s checks (regular, ours, private, one link, bounded, a JSON object) on a
    file opened relative to an already verified directory. None when it does not exist."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink > 1
            or st.st_mode & 0o077
        ):
            raise native_state.StateError(f"{name} is not private")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise native_state.StateError(f"{name} is too large")
    try:
        doc = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise native_state.StateError(f"{name} is unreadable") from None
    if not isinstance(doc, dict):
        raise native_state.StateError(f"{name} is unreadable")
    return doc


@contextlib.contextmanager
def _state_dirs() -> Iterator[tuple[int | None, int | None]]:
    """``(native state dir fd, workers dir fd)`` for one pass, either None when absent.

    The chain is verified once (owner, not writable by others, no symlinks: ``storage.directory``)
    and never re-resolved by path afterwards."""
    root = native_state.root()
    try:
        os.lstat(root)
    except FileNotFoundError:
        yield None, None
        return
    try:
        with storage.directory(root) as fd:
            root_fd = os.dup(fd)
    except (OSError, ValueError) as e:
        raise RecordsUnreadable([f"the native state directory: {e}"]) from None
    workers_fd = None
    try:
        try:
            workers_fd = _open_dir_at(root_fd, "workers")
        except FileNotFoundError:
            workers_fd = None
        yield root_fd, workers_fd
    finally:
        if workers_fd is not None:
            os.close(workers_fd)
        os.close(root_fd)


# ---- the referenced set -----------------------------------------------------------------------


def _ids_in(text: str, out: set[str]) -> None:
    for m in _DASHED.findall(text):
        out.add(m.lower())
    for m in _HEX32.findall(text):  # a unit name carries the id without its dashes
        out.add(str(uuid.UUID(m)))


def _referenced(root_fd: int | None, pacer: _Pacer) -> set[str]:
    """Every UUID any session record names, anywhere in it. Raises :class:`RecordsUnreadable` if
    ANY record (or the listing) could not be read: a subset of the records proves nothing."""
    if root_fd is None:
        return set()
    try:
        names = [n for n in os.listdir(root_fd) if n.endswith(".json")]
    except OSError as e:
        raise RecordsUnreadable([f"the native state directory: {_reason(e)}"]) from None
    out: set[str] = set()
    problems: list[str] = []
    for name in names:
        pacer.tick()
        try:
            record = _read_json_at(root_fd, name, native_state.MAX_RECORD_BYTES)
        except (OSError, native_state.StateError) as e:
            problems.append(f"session record {name}: {e}")
            continue
        if record is None:
            continue  # it went away between the listing and the read
        # Re-serialised, so an id is found whatever escaping the file used.
        _ids_in(json.dumps(record), out)
    if problems:
        raise RecordsUnreadable(problems)
    return out


def referenced_ids() -> set[str]:
    """The referenced set on its own (diagnostics and tests)."""
    with _state_dirs() as (root_fd, _workers_fd):
        return _referenced(root_fd, _Pacer())


# ---- discovery --------------------------------------------------------------------------------


def _worker_id(name: str) -> str | None:
    """A canonical uuid4 directory name, else None (not a worker dir: never touched)."""
    try:
        return native_containment.WorkerIdentity(name).worker_id
    except native_containment.ContainmentError:
        return None


def _listing(workers_fd: int | None) -> tuple[list[str], list[str]]:
    """``(worker ids, leftover .gc- names)`` of plain directories under ``workers/``.

    Symlinks and anything that is not a directory are ignored, never followed."""
    if workers_fd is None:
        return [], []
    workers: list[str] = []
    leftovers: list[str] = []
    for name in os.listdir(workers_fd):
        if name.startswith(GC_PREFIX):
            wid, leftover = _worker_id(name[len(GC_PREFIX) :]), True
        else:
            wid, leftover = _worker_id(name), False
        if wid is None:
            continue
        try:
            st = os.stat(name, dir_fd=workers_fd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        (leftovers if leftover else workers).append(name)
    return workers, leftovers


def _may_explain_an_ending(life: dict) -> bool:
    """Could ``native_runtime._ended_reason`` explain this generation's ending from its lifecycle?

    A SUPERSET of that function's non-None cases (it also needs matching invocation ids, which
    only narrows it), so a dir it would explain is always kept. Pinned against the function."""
    if life.get("journal_failure") is not None:
        return True
    ended = life.get("exit")
    if isinstance(ended, dict) and ended.get("result") == "oom-kill":
        return True
    agent = life.get("agent_exit")
    return isinstance(agent, dict) and agent.get("reason") == "sigkill_during_oom"


def _ended_facts(dfd: int) -> tuple[dict, bool]:
    """``(lifecycle, has config)`` read relative to the worker dir. Raises when unreadable."""
    life = _read_json_at(dfd, "lifecycle.json") or {}
    try:
        os.stat("config.json", dir_fd=dfd, follow_symlinks=False)
        has_config = True
    except FileNotFoundError:
        has_config = False
    return life, has_config


def _file_eligible(life: dict, has_config: bool) -> bool:
    return life.get("phase") == "gone" and not has_config and not _may_explain_an_ending(life)


def _probe_one(worker_id: str):
    try:
        return native_runtime.HOST.show(native_containment.WorkerIdentity(worker_id))
    except Exception:  # noqa: BLE001 — a probe that raised answered nothing
        return None


def _probe_batch(worker_ids: list[str]) -> list:
    """ONE ``systemctl --user show`` for many units (the real host only): the manager answers
    one property block per unit, in order, each validated by ``parse_show`` exactly as a single
    query is. Anything short of a complete, matching answer is None for every unit in it."""
    workers = [native_containment.WorkerIdentity(w) for w in worker_ids]
    argv = native_containment.show_argv(workers[0])[:-1] + [w.unit for w in workers]
    try:
        done = native_runtime.HOST.run(argv)
    except Exception:  # noqa: BLE001 — no answer is no proof
        return [None] * len(workers)
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return [None] * len(workers)
    blocks = done.stdout.strip("\n").split("\n\n")
    if len(blocks) != len(workers):
        return [None] * len(workers)
    out = []
    for worker, block in zip(workers, blocks, strict=True):
        try:
            out.append(native_containment.parse_show(worker, block + "\n"))
        except native_containment.ContainmentError:
            out.append(None)
    return out


def _batched_host() -> bool:
    """Is ``HOST`` the real systemd host (whose ``show`` we may batch through its ``run``)? A
    substituted host (tests, the scripted-native fake) is asked one unit at a time."""
    host = native_runtime.HOST
    return isinstance(host, native_runtime.Host) and type(host).show is native_runtime.Host.show


def _observe(worker_ids: list[str]) -> tuple[list, int]:
    """``(observation or None per id, unit queries made)`` — never more than one query per id."""
    if not worker_ids:
        return [], 0
    with ThreadPoolExecutor(max_workers=_PROBE_WORKERS) as pool:
        if _batched_host():
            chunks = [
                worker_ids[i : i + _PROBE_BATCH] for i in range(0, len(worker_ids), _PROBE_BATCH)
            ]
            answers = list(pool.map(_probe_batch, chunks))
            return [o for chunk in answers for o in chunk], len(chunks)
        return list(pool.map(_probe_one, worker_ids)), len(worker_ids)


def _unit_over(observation, life: dict) -> tuple[bool, str | None]:
    """``(over, skip reason)`` from the unit's observation plus, if it is still loaded, a cgroup
    read. A failed probe is never proof."""
    if observation is None:
        return False, SKIP_PROBE
    if observation.load_state == "not-found":
        if observation.active_state == "inactive" and observation.main_pid == 0:
            return True, None
        return False, SKIP_PROBE
    if observation.active_state not in {"inactive", "failed"} or observation.main_pid != 0:
        return False, SKIP_UNIT
    group = observation.control_group or life.get("control_group")
    if not isinstance(group, str) or not group:
        return False, SKIP_PROBE
    try:
        cgroup = native_runtime.HOST.cgroup(group)
    except (native_containment.ContainmentError, OSError, ValueError):
        return False, SKIP_PROBE
    if cgroup.state in {"absent", "empty"}:
        return True, None
    return False, SKIP_UNIT if cgroup.state == "populated" else SKIP_PROBE


@dataclass
class _Scan:
    eligible: list[str]
    leftovers: list[str]
    skipped: dict[str, int]
    unit_queries: int


def _scan(workers_fd: int | None, referenced: set[str], pacer: _Pacer) -> _Scan:
    workers, leftovers = _listing(workers_fd)
    file_ok: list[tuple[str, dict]] = []
    skipped: dict[str, int] = {}
    for wid in workers:
        if wid in referenced:
            continue
        pacer.tick()
        try:
            dfd = _open_dir_at(workers_fd, wid)
        except FileNotFoundError:
            continue  # it vanished since the listing: nothing to prove or report
        except OSError:
            skipped[SKIP_UNREADABLE] = skipped.get(SKIP_UNREADABLE, 0) + 1
            continue
        try:
            life, has_config = _ended_facts(dfd)
        except (OSError, native_state.StateError):
            # Not proved ended, so kept — and reported, so the result never reads as complete.
            skipped[SKIP_UNREADABLE] = skipped.get(SKIP_UNREADABLE, 0) + 1
            continue
        finally:
            os.close(dfd)
        if _file_eligible(life, has_config):
            file_ok.append((wid, life))
    eligible: list[str] = []
    observations, queries = _observe([wid for wid, _life in file_ok])
    for (wid, life), observation in zip(file_ok, observations, strict=True):
        over, why = _unit_over(observation, life)
        if over:
            eligible.append(wid)
        else:
            skipped[why or SKIP_PROBE] = skipped.get(why or SKIP_PROBE, 0) + 1
    return _Scan(eligible, leftovers, skipped, queries)


# ---- dry run ----------------------------------------------------------------------------------


def _tree_bytes_at(dfd: int, depth: int = 0) -> int:
    total = 0
    for name in os.listdir(dfd):
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode):
            total += st.st_size
        elif depth < _MAX_DEPTH:
            child = _open_dir_at(dfd, name)
            try:
                total += _tree_bytes_at(child, depth + 1)
            finally:
                os.close(child)
    return total


def measure() -> tuple[int, int, int]:
    """``(items, bytes, unit_queries)`` a prune would remove now. Nothing is changed.

    Raises :class:`RecordsUnreadable` when the records could not all be read (the caller reports
    the category as unmeasured, which the confirmation blocks on)."""
    pacer = _Pacer()
    with _state_dirs() as (root_fd, workers_fd):
        scan = _scan(workers_fd, _referenced(root_fd, pacer), pacer)
        nbytes = 0
        for name in [*scan.eligible, *scan.leftovers]:
            pacer.tick()
            try:
                dfd = _open_dir_at(workers_fd, name)
            except FileNotFoundError:
                continue
            try:
                nbytes += _tree_bytes_at(dfd)
            finally:
                os.close(dfd)
    return len(scan.eligible) + len(scan.leftovers), nbytes, scan.unit_queries


# ---- removal ----------------------------------------------------------------------------------


class _Partial(Exception):
    def __init__(self, freed: int, reason: str) -> None:
        super().__init__(reason)
        self.freed = freed
        self.reason = reason


def _clear_at(dfd: int, depth: int = 0) -> tuple[int, list[str]]:
    """Unlink everything inside the open directory ``dfd``. Returns ``(bytes freed, problems)``;
    bytes count only files whose unlink landed. Symlinks are unlinked, never followed."""
    freed = 0
    problems: list[str] = []
    for name in os.listdir(dfd):
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(f"{name}: {_reason(e)}")
            continue
        if st.st_uid != os.geteuid():
            problems.append(f"{name}: owned by another user")
            continue
        if stat.S_ISDIR(st.st_mode):
            if depth >= _MAX_DEPTH:
                problems.append(f"{name}: nested too deep")
                continue
            try:
                child = _open_dir_at(dfd, name)
            except OSError as e:
                problems.append(f"{name}: {_reason(e)}")
                continue
            try:
                sub_freed, sub_problems = _clear_at(child, depth + 1)
            finally:
                os.close(child)
            freed += sub_freed
            problems.extend(f"{name}/{p}" for p in sub_problems)
            if not sub_problems:
                try:
                    os.rmdir(name, dir_fd=dfd)
                except OSError as e:
                    problems.append(f"{name}: {_reason(e)}")
            continue
        try:
            _unlink_at(name, dfd)
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(f"{name}: {_reason(e)}")
            continue
        freed += st.st_size
    return freed, problems


def _delete_renamed(workers_fd: int, gc_name: str) -> int:
    """Remove ``workers/<gc_name>`` completely; returns bytes freed or raises :class:`_Partial`."""
    try:
        dfd = _open_dir_at(workers_fd, gc_name)
    except FileNotFoundError:
        return 0
    except OSError as e:
        raise _Partial(0, _reason(e)) from None
    try:
        freed, problems = _clear_at(dfd)
    finally:
        os.close(dfd)
    if problems:
        raise _Partial(freed, "; ".join(problems[:3]))
    try:
        os.rmdir(gc_name, dir_fd=workers_fd)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise _Partial(freed, _reason(e)) from None
    return freed


@contextlib.contextmanager
def _worker_locked(dfd: int) -> Iterator[bool]:
    """Hold the worker's ``lifecycle.lock`` (the sidecar ``update_lifecycle`` takes). Yields False
    if it stayed busy for :data:`_LOCK_WAIT`."""
    fd = os.open(
        "lifecycle.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dfd,
    )
    try:
        deadline = time.monotonic() + _LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        yield True
    finally:
        os.close(fd)


def _remove_one(workers_fd: int, worker_id: str, out: Outcome) -> None:
    try:
        dfd = _open_dir_at(workers_fd, worker_id)
    except FileNotFoundError:
        return  # gone already (another instance's pass) — nothing to report
    except OSError as e:
        out.failed.append((worker_id, _reason(e)))
        return
    try:
        with _worker_locked(dfd) as held:
            if not held:
                out.skip(SKIP_BUSY)
                return
            # The deciding facts, re-read under the lock every lifecycle writer takes.
            try:
                life, has_config = _ended_facts(dfd)
            except (OSError, native_state.StateError):
                out.skip(SKIP_CHANGED)
                return
            if not _file_eligible(life, has_config):
                out.skip(SKIP_CHANGED)
                return
            _before_remove(worker_id)
            gc_name = GC_PREFIX + worker_id
            try:
                os.rename(worker_id, gc_name, src_dir_fd=workers_fd, dst_dir_fd=workers_fd)
            except OSError as e:
                out.failed.append((worker_id, _reason(e)))
                return
            # What was renamed must be what was checked (same inode as the open descriptor).
            try:
                renamed = os.stat(gc_name, dir_fd=workers_fd, follow_symlinks=False)
            except OSError as e:
                out.failed.append((worker_id, _reason(e)))
                return
            here = os.fstat(dfd)
            if (renamed.st_dev, renamed.st_ino) != (here.st_dev, here.st_ino):
                out.failed.append((worker_id, "the directory was replaced while pruning"))
                return
            try:
                out.bytes_freed += _delete_renamed(workers_fd, gc_name)
            except _Partial as p:
                out.bytes_freed += p.freed
                out.failed.append(
                    (worker_id, f"partly removed ({p.freed} bytes freed): {p.reason}")
                )
                return
            out.removed += 1
    except OSError as e:
        out.failed.append((worker_id, _reason(e)))
    finally:
        os.close(dfd)


def prune() -> Outcome:
    """Remove every qualifying dir, plus any ``.gc-`` leftover of an earlier partial pass.

    Raises :class:`RecordsUnreadable` (nothing removed) when the session records could not all be
    read, before discovery or at the re-read just before removal."""
    out = Outcome()
    pacer = _Pacer()
    with _state_dirs() as (root_fd, workers_fd):
        scan = _scan(workers_fd, _referenced(root_fd, pacer), pacer)
        out.unit_queries = scan.unit_queries
        for reason, n in scan.skipped.items():
            out.skipped[reason] = out.skipped.get(reason, 0) + n
        _after_discovery()
        if workers_fd is None or (not scan.eligible and not scan.leftovers):
            return out
        # Rebuilt once more after the (slow) unit probes, so a reference written meanwhile is seen.
        referenced = _referenced(root_fd, pacer)
        for name in scan.leftovers:
            pacer.tick()
            try:
                out.bytes_freed += _delete_renamed(workers_fd, name)
                out.removed += 1
            except _Partial as p:
                out.bytes_freed += p.freed
                out.failed.append(
                    (name[len(GC_PREFIX) :], f"partly removed ({p.freed} bytes freed): {p.reason}")
                )
        for wid in scan.eligible:
            pacer.tick()
            if wid in referenced:
                out.skip(SKIP_REFERENCED)
                continue
            _remove_one(workers_fd, wid, out)
    return out
