"""The process floor of an API worker (#1724): what runs in its own unit that the agent started.

An idle worker exits, and its unit's `KillMode=control-group` takes every process in the
group with it. A client reports some background work itself (Claude's tasks, Codex's
terminals); much of it nobody reports (`cmd &`, `setsid`, a dev server started through an MCP
tool). So the worker also observes its own cgroup:

* At readiness it records a **baseline**: every process then in the group, as a
  ``(pid, start time)`` identity: the worker, the native CLI and whatever the CLI started to get
  ready (MCP servers). The start time (``/proc/<pid>/stat`` field 22) guards against pid reuse.
* Any later process in the group (or a descendant cgroup) whose identity is not in that baseline
  is **agent-spawned work**, whatever its parent: a grandchild started through an MCP server
  counts too. While one lives, the worker is busy.
* The scan only reads (``cgroup.procs`` and ``/proc/<pid>/stat``); it never signals. A process
  that vanishes mid-scan is gone. Any other failure, a partial read, or more than
  ``MAX_PROCESSES`` processes is ``unknown``, which is never proof of idle.

Only the pid, its start time and the executable name (``comm``) leave this module: never argv
or the environment, which can hold secrets. The guarantee covers observable same-cgroup
processes only; a job handed to another service manager (``systemd-run --user``) or another host
has left the group and is not seen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC = Path("/proc")
#: A scan that finds more processes than this is ``unknown``, never a partial answer. Above the
#: largest API task budget (`resource_limits`: 16384 tasks), so a real unit never reaches it.
MAX_PROCESSES = 16384
#: Descendant cgroups walked below the unit's own (#1732 may put tools in their own).
MAX_GROUPS = 256
_MAX_READ = 1 << 20

OK, UNKNOWN, UNCONTAINED = "ok", "unknown", "uncontained"


@dataclass(frozen=True, order=True)
class Process:
    start: int  # clock ticks since boot: (pid, start) is the identity
    pid: int
    comm: str = field(compare=False)
    ppid: int = field(default=0, compare=False)  # its parent at scan time
    state: str = field(default="", compare=False)  # `/proc/<pid>/stat` state (`T` = stopped)

    @property
    def identity(self) -> tuple[int, int]:
        return (self.pid, self.start)


@dataclass(frozen=True)
class Scan:
    """``ok`` with every live process of the group, ``unknown`` (unreadable), or ``uncontained``:
    the worker is not in a unit of its own at all, which only a substituted service manager
    (a test harness) produces — `native_containment` never launches a worker outside its unit."""

    state: str
    processes: frozenset[Process] = frozenset()
    #: When it was taken, in the clock ticks since boot process start times use: read BEFORE the
    #: group, so a live process missing from this scan started after it (#1724, Stop).
    observed: int | None = None


class ScanError(Exception):
    pass


def _read(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(_MAX_READ + 1)
    if len(data) > _MAX_READ:
        raise ScanError("oversized")
    return data.decode("utf-8", "replace")


def parse_stat(text: str) -> tuple[str, str, int, int]:
    """``(comm, state, ppid, start)`` of one ``/proc/<pid>/stat``. ``comm`` sits in parentheses
    and may itself hold spaces or parentheses, so the fields are counted after the LAST ``)``."""
    left, right = text.find("("), text.rfind(")")
    if left < 0 or right < left:
        raise ScanError("malformed stat")
    rest = text[right + 2 :].split()
    if len(rest) < 20 or not rest[1].isdigit() or not rest[19].isdigit():
        raise ScanError("malformed stat")
    return text[left + 1 : right][:16], rest[0], int(rest[1]), int(rest[19])


def read_process(pid: int, proc: Path = PROC) -> Process | None:
    """One process, or None when it is gone (or a zombie: it runs nothing any more)."""
    try:
        comm, state, ppid, start = parse_stat(_read(proc / str(pid) / "stat"))
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError) as exc:
        raise ScanError(type(exc).__name__) from None
    if state in {"Z", "X"}:
        return None
    return Process(start, pid, comm, ppid, state)


def _groups(top: Path) -> list[Path]:
    """The group and its descendant groups, bounded. A walk that cannot finish is unknown."""
    out, pending = [], [top]
    while pending:
        group = pending.pop()
        out.append(group)
        if len(out) > MAX_GROUPS:
            raise ScanError("too many cgroups")
        try:
            with os.scandir(group) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except FileNotFoundError:
            if group is top:
                raise ScanError("no such cgroup") from None
            # A descendant removed mid-walk held nothing any more.
            out.pop()
        except OSError as exc:
            raise ScanError(type(exc).__name__) from None
    return out


def scan(group: str, *, root: Path = CGROUP_ROOT, proc: Path = PROC) -> Scan:
    """Every live process in ``group`` (a cgroup v2 path such as ``/user.slice/…/x.service``)
    and its descendant groups. Never raises: a failure is ``unknown``."""
    try:
        if not group.startswith("/") or ".." in group.split("/"):
            raise ScanError("invalid cgroup path")
        observed = now_ticks(proc)
        pids: set[int] = set()
        for path in _groups(root / group.lstrip("/")):
            try:
                text = _read(path / "cgroup.procs")
            except FileNotFoundError:
                if path == root / group.lstrip("/"):
                    raise ScanError("no cgroup.procs") from None
                continue  # a descendant removed after the walk
            except OSError as exc:
                raise ScanError(type(exc).__name__) from None
            for line in text.split():
                if not line.isdigit():
                    raise ScanError("malformed cgroup.procs")
                pids.add(int(line))
                if len(pids) > MAX_PROCESSES:
                    raise ScanError("too many processes")
        found = (read_process(pid, proc) for pid in sorted(pids))
        return Scan(OK, frozenset(p for p in found if p is not None), observed)
    except ScanError:
        return Scan(UNKNOWN)


def now_ticks(proc: Path = PROC) -> int:
    """Clock ticks since boot now (``/proc/uptime``), the unit of a process's start time."""
    try:
        uptime = float(_read(proc / "uptime").split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ScanError, ValueError, IndexError):
        raise ScanError("no uptime") from None
    return int(uptime * ticks)


def members(group: str, *, root: Path = CGROUP_ROOT) -> set[int]:
    """The pids in ``group`` and its descendant groups (``cgroup.procs`` only, no ``/proc``).
    Raises `ScanError` when that cannot be read: membership is never guessed."""
    if not group.startswith("/") or ".." in group.split("/"):
        raise ScanError("invalid cgroup path")
    pids: set[int] = set()
    for path in _groups(root / group.lstrip("/")):
        try:
            text = _read(path / "cgroup.procs")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ScanError(type(exc).__name__) from None
        pids.update(int(line) for line in text.split() if line.isdigit())
    return pids


def own_group(proc_cgroup: str) -> str | None:
    """The cgroup v2 path in ``/proc/self/cgroup`` (``0::/…``), or None if there is none."""
    for line in proc_cgroup.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "" and path.startswith("/"):
            return path.rstrip("/") or "/"
    return None


def observe(unit: str, pinned: str | None, *, proc: Path = PROC, root: Path = CGROUP_ROOT) -> Scan:
    """This worker's own group, scanned only when it is the unit's AND the group the runtime
    pinned for it. In its unit but unpinned or mismatched: ``unknown``. Not in a unit of its own
    at all (the leaf is not ``unit``): ``uncontained``, a test harness's substituted manager."""
    try:
        group = own_group(_read(proc / "self" / "cgroup"))
    except (OSError, ScanError):
        return Scan(UNKNOWN)
    if group is None:
        return Scan(UNKNOWN)
    if group.rsplit("/", 1)[-1] != unit:
        return Scan(UNCONTAINED)
    if pinned != group:
        return Scan(UNKNOWN)
    return scan(group, root=root, proc=proc)


@dataclass(frozen=True)
class Floor:
    """The process floor against a baseline: the agent's processes, or why they are unknown."""

    state: str
    processes: tuple[Process, ...] = ()
    observed: int | None = None  # the scan's time, in ticks since boot (`Scan.observed`)

    @property
    def busy(self) -> bool:
        return self.state == UNKNOWN or bool(self.processes)


def floor(observed: Scan, baseline: frozenset[tuple[int, int]] | None) -> Floor:
    if observed.state != OK:
        return Floor(observed.state)
    if baseline is None:
        return Floor(UNKNOWN)  # no baseline yet: nothing can be told apart
    return Floor(
        OK,
        tuple(sorted(p for p in observed.processes if p.identity not in baseline)),
        observed.observed,
    )


_BOOT: float | None = None


def started_at(process: Process, proc: Path = PROC) -> int | None:
    """When a process started, as epoch seconds (boot time + its start ticks), or None."""
    global _BOOT
    boot = _BOOT if proc == PROC else None
    try:
        if boot is None:
            for line in _read(proc / "stat").splitlines():
                if line.startswith("btime "):
                    boot = float(line.split()[1])
            if proc == PROC:
                _BOOT = boot
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ScanError, ValueError):
        return None
    if boot is None or ticks <= 0:
        return None
    return int(boot + process.start / ticks)
