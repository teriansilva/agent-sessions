"""Ephemeral vendor PTYs for installation sign-in and verification (#1259).

The fixed systemd user service owns the whole process cgroup, including descendants which call
setsid. It has a host-enforced lifetime and is stopped on success, failure and disconnect. There
is no uncontained fallback. Vendor bytes go only through this in-memory PTY, never the ordinary
terminal/session/scrollback/AI stack or a logger. This is lifecycle containment, not a sandbox:
the explicitly reviewed vendor program still runs as the operator and can use its own stores.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import struct
import subprocess
import termios
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import opencode_admission

MAX_OUTPUT = 1024 * 1024
MAX_INPUT = 256 * 1024
SIGNIN_SECONDS = 600
PROBE_SECONDS = 90


class ProcessError(ValueError):
    pass


class CleanupError(ProcessError):
    pass


async def drain(task):
    """Finish owned spawn/teardown work even when the requesting socket disappears."""
    task = asyncio.ensure_future(task)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def probe_marker(operation_id: str, purpose: str) -> str:
    unit_name(operation_id)  # same canonical, server-minted UUID as the owned service
    if purpose not in ("new", "resume"):
        raise ProcessError("invalid verification purpose")
    return "BATTLELAB_PROBE_" + operation_id.replace("-", "")[:16] + "_" + purpose.upper()


def probe_message(operation_id: str, purpose: str) -> str:
    return f"Reply exactly {probe_marker(operation_id, purpose)}. Do not use tools."


def _probe_argv(prov, purpose: str, native_id: str, operation_id: str) -> list[str]:
    m = prov.manifest
    if purpose not in ("new", "resume") or not m.can(purpose):
        raise ProcessError("this verification operation is unavailable")
    resume = purpose == "resume"
    if resume or not prov.new_session_reconciles:
        if not prov.id_pattern.fullmatch(native_id):
            raise ProcessError("invalid verification session identity")
    message = probe_message(operation_id, purpose)
    ep = prov.entrypoint_path()
    # These closed, version-tested modes are implementation-owned. A recipe cannot add flags,
    # a prompt, automatic trust answers or permission bypass to a verification operation.
    match m.probe_kind:
        case "print-pinned":
            return [
                ep,
                "--print",
                "--tools",
                "",
                "--disable-slash-commands",
                "--resume" if resume else "--session-id",
                native_id,
                message,
            ]
        case "exec-readonly":
            return [
                ep,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--json",
                *(["resume", native_id, "--json"] if resume else []),
                message,
            ]
        case "run-session":
            return [
                ep,
                "run",
                "--pure",
                "--format",
                "json",
                *(["--session", native_id] if resume else []),
                message,
            ]
        case "prompt-pinned":
            return [
                ep,
                "--approval-mode",
                "default",
                "--resume" if resume else "--session-id",
                native_id,
                "--prompt",
                message,
            ]
        case "prompt-session":
            return [ep, "--prompt", message, *(["--session", native_id] if resume else [])]
        case "print-conversation":
            return [
                ep,
                "--print",
                message,
                "--disable-slash-commands",
                "--print-timeout",
                "60s",
                *(["--conversation", native_id] if resume else []),
            ]
    raise ProcessError("this verification mode is unavailable")


def _argv(
    prov, purpose: str, cwd: Path, native_id: str | None, operation_id: str | None = None
) -> list[str]:
    m = prov.manifest
    if m.runtime != "pty":
        raise ProcessError("this agent has no terminal process")
    if purpose == "version":
        if m.binary.version_flag is None:
            raise ProcessError("this manifest has no reviewed version flag")
        return [prov.entrypoint_path(), m.binary.version_flag]
    if purpose == "signin":
        if m.signin_kind == "cli-subcommand" and m.signin_subcommand is not None:
            return [prov.entrypoint_path(), m.signin_subcommand]
        if m.signin_kind == "auth-login":
            return [prov.entrypoint_path(), "auth", "login"]
        if m.signin_kind == "interactive":
            return [prov.entrypoint_path()]
        raise ProcessError("this manifest has no sign-in command")
    if native_id is None:
        raise ProcessError("a verification session identity is required")
    if m.probe_kind != "terminal":
        if operation_id is None:
            raise ProcessError("a verification operation identity is required")
        return _probe_argv(prov, purpose, native_id, operation_id)
    # Verification never grants permission bypass. A vendor trust/permission prompt that needs
    # an operator leaves this bounded check unsuccessful; a test message is not blanket consent.
    if purpose == "new" and m.can("new"):
        return prov.new_launch_argv(native_id, cwd=str(cwd), bypass=False)
    if purpose == "resume" and m.can("resume"):
        return prov.launch_argv(native_id, cwd=str(cwd), bypass=False)
    raise ProcessError("this verification operation is unavailable")


def _wrapped(
    argv: list[str], cwd: Path, unit: str, seconds: int, *, probe_kind: str | None = None
) -> list[str]:
    # Clear the user manager's environment inside the service. The vendor gets its own HOME and
    # PATH, never BattleLab's cookie/key/database environment. These values contain no credentials.
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }
    if probe_kind == "run-session":
        # This CLI defaults to allowing tools. Its own newly minted primary agent makes the
        # deny rule last, without merging a project's earlier per-tool grants under that name.
        agent = "battlelab-probe-" + uuid.uuid4().hex
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {
                "permission": {"*": "deny"},
                "default_agent": agent,
                "agent": {agent: {"mode": "primary", "permission": {"*": "deny"}}},
            }
        )
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--description=BattleLab temporary agent operation",
        "--wait",
        "--collect",
        "--pty",
        "--service-type=exec",
        "--expand-environment=no",
        f"--unit={unit}",
        f"--working-directory={cwd}",
        f"--property=RuntimeMaxSec={seconds}",
        "--property=TimeoutStopSec=2",
        "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes",
        "--property=TasksMax=128",
        "--property=MemoryMax=1G",
        "--",
        "/usr/bin/env",
        "-i",
        *(f"{key}={value}" for key, value in environment.items()),
        *argv,
    ]


def _stop(unit: str) -> bool:
    # No vendor bytes or exception text is logged. The unit name is locally minted, never input.
    try:
        result = subprocess.run(  # noqa: S603 — fixed systemctl command and server-minted unit
            ["/usr/bin/systemctl", "--user", "stop", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode in (0, 5)  # stopped, or unit already collected/not loaded
    except (OSError, subprocess.TimeoutExpired):
        return False


def unit_name(operation_id: str) -> str:
    if str(uuid.UUID(operation_id)) != operation_id:
        raise ProcessError("invalid temporary operation identity")
    return f"battlelab-plugin-{operation_id.replace('-', '')}.service"


def stop_operation(operation_id: str) -> bool:
    return _stop(unit_name(operation_id))


@dataclass
class Pty:
    proc: asyncio.subprocess.Process
    fd: int
    output: int = 0
    input: int = 0

    def resize(self, rows: int, cols: int) -> None:
        if (
            type(rows) is not int
            or type(cols) is not int
            or not (2 <= rows <= 200 and 10 <= cols <= 300)
        ):
            raise ProcessError("invalid terminal size")
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def _ready(self, write=False) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        add = loop.add_writer if write else loop.add_reader
        remove = loop.remove_writer if write else loop.remove_reader
        add(self.fd, lambda: None if ready.done() else ready.set_result(None))
        try:
            await ready
        finally:
            remove(self.fd)

    async def read(self) -> bytes:
        while True:
            try:
                data = os.read(self.fd, 16384)
                self.output += len(data)
                if self.output > MAX_OUTPUT:
                    raise ProcessError("temporary terminal exceeded its output limit")
                return data
            except BlockingIOError:
                await self._ready()
            except OSError as exc:
                if exc.errno == errno.EIO:
                    return b""
                raise

    async def write(self, data: bytes) -> None:
        self.input += len(data)
        if self.input > MAX_INPUT:
            raise ProcessError("temporary terminal exceeded its input limit")
        pending = memoryview(data)
        while pending:
            try:
                pending = pending[os.write(self.fd, pending[:16384]) :]
            except BlockingIOError:
                await self._ready(write=True)


@contextlib.asynccontextmanager
async def spawn(
    prov,
    purpose: str,
    *,
    cwd: Path,
    native_id: str | None = None,
    operation_id: str | None = None,
):
    seconds = SIGNIN_SECONDS if purpose == "signin" else PROBE_SECONDS
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ProcessError("the temporary workspace is unavailable")
    operation_id = operation_id or str(uuid.uuid4())
    unit = unit_name(operation_id)
    master, slave = os.openpty()
    os.set_blocking(master, False)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    guard = None
    launch_guard = None
    proc = None
    try:
        # Candidate providers are not live yet. Ask THEIR manifest and take the same stable
        # per-engine maintenance lock that live launches and compaction use.
        if prov.manifest.launch and prov.manifest.launch.admission == "sqlite-store-shared":
            acquiring = asyncio.create_task(
                asyncio.to_thread(
                    opencode_admission.acquire,
                    prov.engine_id,
                    exclusive=False,
                    database=prov.store_path("db"),
                )
            )
            try:
                guard = await asyncio.shield(acquiring)
            except asyncio.CancelledError:
                guard = await drain(acquiring)
                raise
            if guard is None:
                raise ProcessError("agent maintenance is busy; retry after it finishes")
        argv = _wrapped(
            _argv(prov, purpose, cwd, native_id, operation_id),
            cwd,
            unit,
            seconds,
            probe_kind=prov.manifest.probe_kind if purpose in ("new", "resume") else None,
        )
        from . import admission

        # jobs/signin own the worker fence for the operation's entire lifetime. Order its
        # actual spawn with native creation and roster changes, then release after handoff.
        launch_guard = await admission.acquire_candidate_async(
            prov, purpose, operation_id, native_id
        )
        if launch_guard.reason:
            # The helper raises on refusal today; the spawn site must not depend on that.
            reason = launch_guard.reason
            launch_guard.release()
            launch_guard = None
            raise admission.Refused(reason)
        creating = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
                cwd=str(cwd),
                env={
                    k: os.environ[k]
                    for k in ("PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
                    if k in os.environ
                },
            )
        )
        try:
            proc = await asyncio.shield(creating)
        except BaseException:
            proc = await drain(creating)
            raise
        finally:
            launch_guard.release()
            launch_guard = None
        os.close(slave)
        slave = -1
        async with asyncio.timeout(seconds):
            yield Pty(proc, master)
    finally:
        if launch_guard is not None:
            launch_guard.release()
        stopped = True if proc is None else await drain(asyncio.to_thread(_stop, unit))
        if proc is not None:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await drain(proc.wait())
        if guard is not None:
            guard.release()
        os.close(master)
        if slave != -1:
            os.close(slave)
        if not stopped:
            raise CleanupError("temporary process cleanup is pending; retry recovery")
