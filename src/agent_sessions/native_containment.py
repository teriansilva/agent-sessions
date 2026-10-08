"""Pure commands and observations for native worker containment (#1278).

The runtime journal must allocate a fresh server-minted worker UUID for every generation,
persist startup intent BEFORE launch, and never launch that generation again after an ambiguous
handoff. Names are never reused, including after collection. That invariant closes the systemd
show/stop race: systemctl has no stop-by-InvocationID operation. The caller serializes recovery,
reads fresh properties immediately before stop, and never restarts/replaces the named service.

These helpers do not execute commands, verify filesystem provenance, allocate journal records,
or grant agent permissions. A caller must verify the exact retained release interpreter and
private state directory before launching. Secrets belong in private worker state/descriptors,
never command arguments or systemd properties. The fixed worker starts its native child itself.

``start_closed`` is stronger than recording startup intent: the caller has permanently closed
that generation's launch gate AND drained any outstanding launcher. Only then may a matching
terminal/collected unit and an empty/absent previously captured cgroup prove it gone. Socket
absence, protocol EOF and a successful stop command are never process-lifetime evidence.
"""

from __future__ import annotations

import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from . import resource_limits

MAX_SHOW_BYTES = 16 * 1024
MAX_CGROUP_BYTES = 4096
_INVOCATION = re.compile(r"[0-9a-f]{32}")
_STATES = frozenset(
    {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance"}
)
_TERMINAL = frozenset({"inactive", "failed"})
_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "InvocationID",
    "ControlGroup",
    "MainPID",
    "Result",
)


class ContainmentError(ValueError):
    pass


def _path(value: str) -> str:
    # systemd performs its own specifier expansion even with shell-free argv. Do not let a
    # percent-containing server path select a different executable, home or state directory.
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or len(value.encode()) > 4096
        or posixpath.normpath(value) != value
        or "%" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ContainmentError("expected a normalized absolute path without specifiers")
    return value


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str

    def __post_init__(self) -> None:
        try:
            value = uuid.UUID(self.worker_id)
            if value.version != 4 or str(value) != self.worker_id:
                raise ValueError
        except (TypeError, AttributeError, ValueError):
            raise ContainmentError(
                "worker identity must be a canonical server-minted UUID4"
            ) from None

    @classmethod
    def mint(cls) -> WorkerIdentity:
        """Mint identity only; the caller must reserve it durably before building a launch."""
        return cls(str(uuid.uuid4()))

    @property
    def unit(self) -> str:
        return f"battlelab-native-{self.worker_id.replace('-', '')}.service"


def _cgroup(path: str, worker: WorkerIdentity | None = None) -> str:
    path = _path(path)
    if path == "/" or (worker is not None and posixpath.basename(path) != worker.unit):
        raise ContainmentError("control group does not identify the worker service")
    return path


@dataclass(frozen=True)
class InvocationIdentity:
    worker: WorkerIdentity
    invocation_id: str
    control_group: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.worker, WorkerIdentity)
            or not isinstance(self.invocation_id, str)
            or _INVOCATION.fullmatch(self.invocation_id) is None
            or self.invocation_id == "0" * 32
        ):
            raise ContainmentError("expected an exact systemd invocation identity")
        _cgroup(self.control_group, self.worker)


@dataclass(frozen=True)
class UnitObservation:
    unit: str
    load_state: str
    active_state: str
    sub_state: str
    invocation_id: str | None
    control_group: str | None
    main_pid: int
    result: str


@dataclass(frozen=True)
class CgroupObservation:
    control_group: str
    state: Literal["populated", "empty", "absent", "unknown"]

    def __post_init__(self) -> None:
        _cgroup(self.control_group)
        if self.state not in {"populated", "empty", "absent", "unknown"}:
            raise ContainmentError("invalid control group observation")


def launch_argv(
    worker: WorkerIdentity,
    *,
    python: str,
    state_dir: str,
    home: str,
    runtime_dir: str | None = None,
    tasks_max: int = resource_limits.DEFAULTS["api_tasks"],
) -> list[str]:
    """Build the one contained service command; no scope/passthrough fallback exists.

    All inputs are server-owned. The caller keeps a release-retention lease through the
    service's lifetime. A private worker config supplies reviewed source/authentication data;
    no native protocol bytes, credentials, prompts or permission flags are accepted here.
    """
    if not isinstance(worker, WorkerIdentity):
        raise ContainmentError("a reserved worker identity is required")
    if not resource_limits.valid("api_tasks", tasks_max):
        raise ContainmentError("invalid API worker task limit")
    environment = [
        f"HOME={_path(home)}",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
    ]
    if runtime_dir is not None:
        runtime_dir = _path(runtime_dir)
        if re.fullmatch(r"/run/user/[0-9]{1,10}", runtime_dir) is None:
            raise ContainmentError("expected the local user manager's runtime directory")
        environment.extend(
            [
                f"XDG_RUNTIME_DIR={runtime_dir}",
                f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus",
            ]
        )
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--service-type=exec",
        "--expand-environment=no",
        "--description=BattleLab native agent runtime",
        f"--unit={worker.unit}",
        f"--working-directory={_path(state_dir)}",
        "--property=ExitType=main",
        "--property=KillMode=control-group",
        "--property=Restart=no",
        "--property=RemainAfterExit=no",
        "--property=SendSIGKILL=yes",
        "--property=TimeoutStartSec=30",
        "--property=TimeoutStopSec=15",
        "--property=RuntimeMaxSec=86400",
        f"--property=TasksMax={tasks_max}",
        "--property=MemoryMax=8G",
        "--property=OOMPolicy=kill",
        "--property=Delegate=no",
        "--property=NoNewPrivileges=yes",
        "--property=StandardInput=null",
        "--property=StandardOutput=null",
        "--property=StandardError=null",
        "--",
        "/usr/bin/env",
        "-i",
        *environment,
        _path(python),
        "-I",
        "-m",
        "agent_sessions.native_worker",
        "--worker-id",
        worker.worker_id,
        "--state-dir",
        state_dir,
    ]


def show_argv(worker: WorkerIdentity) -> list[str]:
    return [
        "/usr/bin/systemctl",
        "--user",
        "show",
        "--no-pager",
        "--property=" + ",".join(_PROPERTIES),
        "--",
        worker.unit,
    ]


def parse_show(worker: WorkerIdentity, text: str, *, returncode: int = 0) -> UnitObservation:
    """Parse complete requested properties; a failed/partial manager query is unknown."""
    if returncode != 0 or not isinstance(text, str) or len(text.encode()) > MAX_SHOW_BYTES:
        raise ContainmentError("systemd unit observation is unavailable")
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in _PROPERTIES or key in values:
            raise ContainmentError("systemd unit observation is malformed")
        values[key] = value
    if set(values) != set(_PROPERTIES) or values["Id"] != worker.unit:
        raise ContainmentError("systemd unit observation has another or incomplete identity")
    if values["LoadState"] not in {"loaded", "not-found"} or values["ActiveState"] not in _STATES:
        raise ContainmentError("systemd unit state is unavailable")
    if any(re.fullmatch(r"[a-z0-9-]{0,64}", values[key]) is None for key in ("SubState", "Result")):
        raise ContainmentError("systemd unit state is malformed")
    invocation = values["InvocationID"] or None
    if invocation is not None and (
        _INVOCATION.fullmatch(invocation) is None or invocation == "0" * 32
    ):
        raise ContainmentError("systemd invocation identity is malformed")
    group = _cgroup(values["ControlGroup"], worker) if values["ControlGroup"] else None
    if re.fullmatch(r"[0-9]{1,10}", values["MainPID"]) is None:
        raise ContainmentError("systemd main process identity is malformed")
    return UnitObservation(
        worker.unit,
        values["LoadState"],
        values["ActiveState"],
        values["SubState"],
        invocation,
        group,
        int(values["MainPID"]),
        values["Result"],
    )


def capture(worker: WorkerIdentity, observation: UnitObservation) -> InvocationIdentity:
    """Capture and durably pin this result before sending any native agent work."""
    if observation.unit != worker.unit or observation.load_state != "loaded":
        raise ContainmentError("the reserved worker service is not loaded")
    return InvocationIdentity(worker, observation.invocation_id, observation.control_group)


def parse_cgroup_events(control_group: str, text: str) -> CgroupObservation:
    """Use cgroup v2's recursive population flag, not just the parent's cgroup.procs."""
    if not isinstance(text, str) or len(text.encode()) > MAX_CGROUP_BYTES:
        raise ContainmentError("control group population is unavailable")
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if (
            len(parts) != 2
            or parts[0] in values
            or re.fullmatch(r"[a-z_]{1,64}", parts[0]) is None
            or re.fullmatch(r"[0-9]{1,20}", parts[1]) is None
        ):
            raise ContainmentError("control group population is malformed")
        values[parts[0]] = parts[1]
    if values.get("populated") not in {"0", "1"}:
        raise ContainmentError("control group has no definite recursive population")
    return CgroupObservation(control_group, "populated" if values["populated"] == "1" else "empty")


def _matches(identity: InvocationIdentity, observation: UnitObservation) -> bool:
    return (
        observation.unit == identity.worker.unit
        and observation.load_state == "loaded"
        and observation.invocation_id == identity.invocation_id
        and observation.control_group == identity.control_group
    )


def classify(
    identity: InvocationIdentity,
    observation: UnitObservation | None,
    cgroup: CgroupObservation | None,
    *,
    start_closed: bool,
) -> Literal["live", "gone", "unknown"]:
    """Classify retained identity; failures and incomplete/contradictory evidence stay unknown.

    A caller may construct ``CgroupObservation(path, 'absent')`` only after a verified read
    proves that exact previously captured cgroup is absent; unreadability is ``unknown``.
    ``start_closed`` must come from the runtime's durable gate and drained launch protocol.
    """
    if (
        observation is None
        or cgroup is None
        or type(start_closed) is not bool
        or observation.unit != identity.worker.unit
        or cgroup.control_group != identity.control_group
    ):
        return "unknown"
    collected = (
        observation.load_state == "not-found"
        and observation.invocation_id is None
        and observation.control_group is None
        and observation.main_pid == 0
        and observation.active_state == "inactive"
    )
    if not _matches(identity, observation) and not collected:
        return "unknown"
    if cgroup.state == "populated":
        return "live"
    if cgroup.state not in {"empty", "absent"}:
        return "unknown"
    if start_closed and observation.active_state in _TERMINAL and observation.main_pid == 0:
        return "gone"
    return "unknown"


def stop_argv(identity: InvocationIdentity, observation: UnitObservation) -> list[str]:
    """Require fresh exact invocation before stop; success still needs classify(...)=gone.

    The caller holds its generation recovery fence. Neither it nor any cooperating process
    may reuse this unit name between observation and execution (see module invariant).
    """
    if not _matches(identity, observation):
        raise ContainmentError("refusing to stop an unverified worker invocation")
    return ["/usr/bin/systemctl", "--user", "stop", "--", identity.worker.unit]
