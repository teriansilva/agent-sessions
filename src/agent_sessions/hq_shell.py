"""HQ's own shell, files and scripts (#2404, epic #2070 B3). Invariant: `hq-shell.md`.

HQ runs commands, reads, lists and writes files, and runs the scripts it keeps in its workspace,
as an agent session does: with the operator's whole user authority (D9, #2070 comment 141397: no
private filesystem view). What this module guarantees instead of a view:

**Every command runs in its own contained unit, never in the app process (D8).** One transient
systemd user unit per command (``battlelab-hq-shell-<id>.service``, a server-minted name never
reused), launched with a literal argv through ``systemd-run --user --pipe --wait``. The user
manager spawns the process, so it inherits no descriptor from the app (stdin is ``/dev/null``,
stdout and stderr the one pipe ``--pipe`` hands over). ``env -i`` and a whitelist are its whole
environment: no ``AGENT_SESSIONS_*``, no agent or provider keys, no tokens, no ``SSH_AUTH_SOCK``,
no bus or runtime directory. The unit carries memory, task, CPU and time limits
(``RuntimeMaxSec`` bounds it even if the app dies).

**No shell layer in the app** (CLAUDE.md, ``pr-validate.yml``). The command HQ wrote is saved as a
script file in the workspace (``runs/<id>.sh``, 0600) and the unit runs bash on that FILE: no
command string is ever an argument, so systemd's ``$`` and ``%`` expansion of command words never
sees it, and the app side is ``create_subprocess_exec`` with literal argv lists only.

**Stop is the unit's.** Stop, Stop all, a card's Stop, a timeout and shutdown run
``systemctl --user stop -- <that unit>``, which kills the unit's whole control group
(``KillMode=control-group``), daemonised children included. Nothing is killed by name or by a
recorded pid.

**Output is bounded and guarded.** At most :data:`OUTPUT_HEAD` + :data:`OUTPUT_TAIL` bytes are
kept (the rest is counted and dropped); escape sequences are removed; known secrets are redacted
and every credential-shaped line is withheld with the line after it (:func:`guard_output`) before
the output reaches the model, the card or the store.

**The card** (proposal kind ``command``) follows the message card's shape: a binding digest
over what runs, ``pending → deciding`` written durably before the unit starts, the act's grant
asked again immediately before the launch, ``interrupted`` after a restart and never re-run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import ask_store, native_containment, template_secrets

log = logging.getLogger(__name__)

KIND = "command"
CLASS = "runs_commands"
PENDING, DECIDING = "pending", "deciding"
DONE, FAILED, TIMED_OUT, STOPPED = "done", "failed", "timed_out", "stopped"
STALE, DISMISSED, INTERRUPTED = "stale", "dismissed", "interrupted"
STATES = frozenset(
    {PENDING, DECIDING, DONE, FAILED, TIMED_OUT, STOPPED, STALE, DISMISSED, INTERRUPTED}
)
ACTIONS = frozenset({"run", "write"})

INTERRUPTED_REASON = (
    "BattleLab stopped while this ran. It may have run, or run in part; check the folder. It is "
    "not run again."
)
INTERRUPTED_REASON_WRITE = (
    "BattleLab stopped while this file was written. Check the file; it is not written again."
)

#: A command, as HQ wrote it.
COMMAND_MAX = 8_000
TIMEOUT_DEFAULT_S = 120
TIMEOUT_MAX_S = 600
#: The unit's own limits (D8). Constants for now (no new setting, #2404 out of scope).
MEMORY_MAX_MIB = 4096
TASKS_MAX = 512
CPU_QUOTA = "200%"
#: The aggregate budget every HQ command shares (#2404, Hermes on the issue): all command units
#: run in one slice whose limits bound them together, however many run at once (D11 keeps the
#: number of commands unbounded). Set on the slice before a launch; a launch fails closed when the
#: budget can't be set.
SLICE = "battlelab-hq.slice"
SLICE_MEMORY_MAX_MIB = 8192
SLICE_TASKS_MAX = 2048
SLICE_CPU_QUOTA = "400%"
#: A kept line longer than this is withheld whole, never checked in parts.
LINE_MAX = 16_384
#: How much longer than the command's timeout the unit itself may live (the app stops it first).
UNIT_GRACE_S = 15
#: What the app keeps of a command's merged output: the first and the last bytes.
OUTPUT_HEAD = 16 * 1024
OUTPUT_TAIL = 48 * 1024
#: What of the guarded output is stored on the card and given to the model, in characters.
OUTPUT_KEEP = 8_000
OUTPUT_KEEP_HEAD = 2_000
#: What of it a tool result carries (a result is at most `ask_orchestrator.TOOL_RESULT_MAX`
#: characters as JSON, escapes included).
MODEL_OUTPUT_MAX = 6_000
#: The file tools' bounds.
FILE_READ_LINES = 400
FILE_READ_CHARS = 9_000
FILE_HASH_MAX = 1024 * 1024
#: The longest physical line `file_read` reads; a longer one is withheld, never materialized.
FILE_LINE_MAX = 16_384
FILE_WRITE_MAX = 64_000
LIST_MAX = 200
CARD_PREVIEW_MAX = 4_000

WITHHELD_LINE = "[line withheld: it looked like a credential]"
OUTPUT_UNAVAILABLE = "[output unavailable: it could not be checked for secrets]"

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
ENV = "/usr/bin/env"
BASH = "/bin/bash"
_UNIT_PREFIX = "battlelab-hq-shell-"
_RUN_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

#: Commands (and writes) in flight in this process: ``{proposal id: task}``.
_TASKS: dict[str, asyncio.Task] = {}
#: Their units: ``{proposal id: (cid, turn_id, unit)}`` while the unit may run.
_UNITS: dict[str, tuple[str, str, str]] = {}
#: The commands (never a file write: a write is not stopped half way) whose run task exists:
#: ``{proposal id: (cid, turn_id)}``, so a Stop before the unit started still stops them.
_OWNERS: dict[str, tuple[str, str]] = {}
#: Proposal ids whose stop was asked.
_STOP_ASKED: set[str] = set()


class Refused(Exception):
    def __init__(self, message: str, code: str = "bad_arguments"):
        super().__init__(message)
        self.code = code


class Revoked(Exception):
    """The act's grant refused at its last check, immediately before the launch: nothing ran."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---- where HQ works -------------------------------------------------------------------------


def _state_home() -> Path:
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


def workspace() -> Path:
    """HQ's own workspace (epic §10): ``<state>/hq/workspace``, with ``scripts/`` HQ keeps and
    ``runs/`` for the command files. Created 0700 on first use. Blocking."""
    root = _state_home() / "hq" / "workspace"
    for d in (root, root / "scripts", root / "runs"):
        d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(d, 0o700)
    return root


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def allowed_roots() -> list[str]:
    """Where HQ's folders and files may be: its workspace, and the discovery roots the Commander
    set (Settings → Projects); with no roots set, the Commander's home. Real paths."""
    from . import project_dirs

    roots = [_real(workspace())]
    configured = project_dirs.effective_roots()
    roots += configured if configured else [_real(Path.home())]
    return list(dict.fromkeys(roots))


def private_dirs() -> list[str]:
    """Folders the file tools refuse (defence in depth; the shell itself can reach them, D9): the
    app's own state, config and runtime folders, and the credential folders. HQ's workspace, which
    sits inside the app's state folder, is carved out by :func:`resolve`."""
    home = Path.home()
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    dirs = [
        _state_home(),
        home / ".config" / "agent-sessions",
        ask_store.root(),
        Path(rt) / "agent-sessions",
        # The app's control sockets (dtach masters and the rest), without creating the folder.
        Path(os.environ.get("AGENT_SESSIONS_RUNTIME_DIR") or home / ".agent-sessions"),
        home / ".agent-sessions",
        home / ".ssh",
        home / ".gnupg",
    ]
    return [_real(d) for d in dirs]


def _clean_text(value: object, name: str, cap: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"{name} must be a non-empty string")
    if len(value) > cap:
        raise Refused(f"{name} is longer than {cap} characters")
    if "\x00" in value:
        raise Refused(f"{name} must not contain NUL")
    return value


def resolve(path: object, base: str | None = None, *, folder: bool = False) -> str:
    """A path HQ named, resolved for the file tools and the shell's folder: ``~`` expanded, a
    relative path taken from ``base`` (HQ's workspace by default), symlinks resolved. It must be
    inside :func:`allowed_roots` and outside :func:`private_dirs` (HQ's workspace excepted).
    Raises :class:`Refused`. Blocking."""
    raw = _clean_text(path, "path", ask_store.PATH_MAX)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise Refused("path must not contain control characters")
    expanded = os.path.expanduser(raw.strip())
    if not os.path.isabs(expanded):
        expanded = os.path.join(base or str(workspace()), expanded)
    real = _real(expanded)
    ws = _real(workspace())
    if not _within(real, ws):
        if any(_within(real, d) for d in private_dirs()):
            raise Refused(
                "that path is in BattleLab's own or a credential folder; the file tools stay out "
                "of it",
                "private",
            )
        if not any(_within(real, r) for r in allowed_roots()):
            raise Refused(
                "that path is outside HQ's workspace and the project roots in Settings",
                "outside_roots",
            )
    if folder and not os.path.isdir(real):
        raise Refused("that folder does not exist", "not_found")
    return real


def in_workspace(path: str) -> bool:
    """Whether ``path`` resolves inside HQ's workspace NOW (symlinks followed). Blocking."""
    try:
        return _within(_real(path), _real(workspace()))
    except Exception:  # noqa: BLE001 — unknown: not the workspace (the stronger class)
        return False


def command_class(row: dict) -> str:
    """The class of a command card's effect NOW (#2404): a write that resolves inside HQ's
    workspace is *reversible*; a write anywhere else and every command is *runs commands*."""
    if row.get("action") == "write" and isinstance(row.get("path"), str):
        return "reversible" if in_workspace(row["path"]) else CLASS
    return CLASS


def _systemd_word(value: str, what: str) -> str:
    """A path the unit's argv or properties carry: no specifier, no control character."""
    if "%" in value or "$" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Refused(f"{what} has a character the contained worker can't take (% or $)")
    return value


# ---- availability and the launch -------------------------------------------------------------


def _runtime_dir() -> str:
    return f"/run/user/{os.getuid()}"


def available() -> tuple[bool, str]:
    """Whether a command can be contained on this host (D8): ``systemd-run``, ``systemctl``,
    ``env`` and ``bash`` present and the user manager's bus reachable. ``(False, why)``
    otherwise; nothing then falls back to running in the app."""
    for exe in (SYSTEMD_RUN, SYSTEMCTL, ENV, BASH):
        if not os.access(exe, os.X_OK):
            return False, f"{exe} is missing, so commands can't be contained on this host"
    bus = Path(_runtime_dir()) / "bus"
    try:
        if not stat.S_ISSOCK(bus.stat().st_mode):
            raise OSError
    except OSError:
        return False, "no systemd user manager is reachable, so commands can't be contained here"
    return True, ""


def unit_name(run_id: str) -> str:
    if not _RUN_ID_RE.match(run_id):
        raise ValueError("malformed run id")
    return f"{_UNIT_PREFIX}{run_id}.service"


def command_env() -> list[str]:
    """The command's whole environment: a whitelist. Nothing of the app's environment, nothing
    of the user manager's (``env -i``)."""
    home = str(Path.home())
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or Path(home).name
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        user = Path(home).name
    return [
        f"HOME={home}",
        f"PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG=C.UTF-8",
        f"USER={user}",
        f"LOGNAME={user}",
        f"SHELL={BASH}",
        "TERM=dumb",
        "PAGER=cat",
        "GIT_PAGER=cat",
        "GIT_TERMINAL_PROMPT=0",
        f"HQ_WORKSPACE={workspace()}",
    ]


def inner_argv(script: str) -> list[str]:
    """What the unit executes: ``env -i <whitelist> bash --noprofile --norc <script file>``. A
    literal list; the command itself is the FILE's content, never an argument."""
    return [ENV, "-i", *command_env(), BASH, "--noprofile", "--norc", script]


def unit_argv(run_id: str, *, cwd: str, script: str, timeout: int) -> list[str]:
    """The one contained launch (D8). Every word is server-owned and checked for systemd's
    specifiers; ``$`` is doubled (`native_containment.literal_command`)."""
    unit = unit_name(run_id)
    cwd = _systemd_word(cwd, "the folder")
    script = _systemd_word(script, "the workspace path")
    for word in command_env():
        _systemd_word(word.split("=", 1)[1], "the environment")
    return [
        SYSTEMD_RUN,
        "--user",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        "--service-type=exec",
        "--description=BattleLab HQ command",
        f"--unit={unit}",
        f"--slice={SLICE}",
        f"--working-directory={cwd}",
        f"--property=RuntimeMaxSec={int(timeout) + UNIT_GRACE_S}",
        f"--property=MemoryMax={MEMORY_MAX_MIB}M",
        f"--property=MemoryHigh={MEMORY_MAX_MIB * 9 // 10}M",
        f"--property=TasksMax={TASKS_MAX}",
        f"--property=CPUQuota={CPU_QUOTA}",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=TimeoutStopSec=10",
        "--property=NoNewPrivileges=yes",
        "--property=Delegate=no",
        "--",
        *native_containment.literal_command(inner_argv(script)),
    ]


def client_env() -> dict[str, str]:
    """The environment of ``systemd-run`` / ``systemctl`` themselves: enough to reach the user
    manager, nothing of the app's. (The unit's own environment is :func:`command_env`.)"""
    rt = _runtime_dir()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": rt,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={rt}/bus",
    }


def slice_argv() -> list[str]:
    """The shared budget on :data:`SLICE` (runtime only: gone after a reboot, set again on use)."""
    return [
        SYSTEMCTL,
        "--user",
        "set-property",
        "--runtime",
        "--",
        SLICE,
        f"MemoryMax={SLICE_MEMORY_MAX_MIB}M",
        f"MemoryHigh={SLICE_MEMORY_MAX_MIB * 9 // 10}M",
        f"TasksMax={SLICE_TASKS_MAX}",
        f"CPUQuota={SLICE_CPU_QUOTA}",
    ]


_SLICE_READY = {"ok": False}


async def _prepare_slice() -> None:
    """Set the shared budget once per process. Raises when it can't be set (fail closed)."""
    if _SLICE_READY["ok"]:
        return
    proc = await asyncio.create_subprocess_exec(
        *slice_argv(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=client_env(),
        close_fds=True,
    )
    if await asyncio.wait_for(proc.wait(), 20) != 0:
        raise OSError("the shared budget for HQ's commands could not be set")
    _SLICE_READY["ok"] = True


async def _spawn(argv: list[str]):
    """Default launcher: ``systemd-run`` as a literal argv, no shell, no inherited descriptor.
    The shared budget's slice is prepared before it (`PREPARE`, in `execute`), so the grant's
    last check sits after every await that prepares the launch."""
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=client_env(),
        close_fds=True,
        start_new_session=True,
    )


async def _stop_unit(unit: str) -> None:
    """Stop one unit HQ started: its whole control group. Never by name of a process."""
    if not unit.startswith(_UNIT_PREFIX):
        raise ValueError("not an HQ command unit")
    proc = await asyncio.create_subprocess_exec(
        SYSTEMCTL,
        "--user",
        "stop",
        "--",
        unit,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=client_env(),
        close_fds=True,
    )
    try:
        await asyncio.wait_for(proc.wait(), 20)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


#: Test seams: tests inject a launcher that never starts a real systemd unit.
LAUNCH: Callable[[list[str]], Awaitable] = _spawn
PREPARE: Callable[[], Awaitable[None]] = _prepare_slice
STOP_UNIT: Callable[[str], Awaitable[None]] = _stop_unit


# ---- output ----------------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_LOGICAL_RE = re.compile(r"(?<!\\)\n")


def _plain(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = _ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or not (ord(c) < 32 or ord(c) == 127))


def guard_output(text: str, cap: int = OUTPUT_KEEP) -> str:
    """Command output (or a file's text) through the store boundary, line by line: every known
    secret redacted, every credential-shaped logical line (backslash continuations joined)
    withheld together with the line after it, then cut to ``cap`` characters keeping the head and
    the tail. Redaction unavailable: a content-free marker. Blocking."""
    from . import template_suggest

    try:
        text = template_secrets.redact_text(text, template_secrets.redaction_values())
    except Exception:  # noqa: BLE001 — fail closed
        return OUTPUT_UNAVAILABLE
    out: list[str] = []
    prev = False
    for line in _LOGICAL_RE.split(text):
        if len(line) > LINE_MAX:
            out.append(f"[a {len(line)}-character line withheld: too long to check]")
            prev = True  # and the line after it, as for a credential-shaped one
            continue
        hit = template_suggest.credential_shaped(ask_store._mask_ids(line))
        if hit or prev:
            if not out or out[-1] != WITHHELD_LINE:
                out.append(WITHHELD_LINE)
        else:
            out.append(line)
        prev = hit
    joined = "\n".join(out)
    if len(joined) <= cap:
        return joined
    head = joined[:OUTPUT_KEEP_HEAD]
    tail = joined[-(cap - OUTPUT_KEEP_HEAD - 60) :]
    cut = len(joined) - len(head) - len(tail)
    return f"{head}\n[… {cut} characters cut …]\n{tail}"


class _Bounded:
    """The first :data:`OUTPUT_HEAD` and last :data:`OUTPUT_TAIL` bytes, and the total."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = OUTPUT_HEAD - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > OUTPUT_TAIL:
                del self.tail[: len(self.tail) - OUTPUT_TAIL]

    def text(self) -> str:
        """The kept bytes as text. When bytes were dropped, the line the head window cuts and the
        line the tail window starts inside are dropped too: a fragment of a line (of a secret) is
        never kept, so redaction always sees whole lines."""
        head, tail = bytes(self.head), bytes(self.tail)
        if self.total - len(head) - len(tail) <= 0:
            return _plain(head + tail)
        nl = head.rfind(b"\n")
        head = head[: nl + 1] if nl >= 0 else b""
        nl = tail.find(b"\n")
        tail = tail[nl + 1 :] if nl >= 0 else b""
        dropped = self.total - len(head) - len(tail)
        return _plain(head) + f"[… {dropped} bytes dropped, whole lines only …]\n" + _plain(tail)


# ---- running one command -----------------------------------------------------------------------


def _script(run_id: str, command: str) -> str:
    """The command as a file in the workspace (0600, ``O_EXCL``): bash runs THIS, never a string."""
    path = workspace() / "runs" / f"{run_id}.sh"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, command.encode("utf-8") + b"\n")
    finally:
        os.close(fd)
    return str(path)


async def execute(
    run_id: str,
    *,
    command: str,
    cwd: str,
    timeout: int,
    owner: tuple[str, str],
    admit: Callable[[], str] | None = None,
) -> dict:
    """Run ``command`` once in its own unit and wait for it, bounded. Returns ``{exit_code,
    output (guarded), truncated, bytes, ms, timed_out, stopped}``. A cancellation (Stop, a
    shutdown) stops the unit before it propagates. ``owner``: ``(cid, turn_id)``, for Stop.

    ``admit`` (an act's grant, `hq_acts.Act.admits`) is asked AFTER every await that prepares
    the launch (the script file, the shared slice), synchronously, immediately before the unit's
    start request: a Commander who lowered the Authority setting or signed out meanwhile gets
    :class:`Revoked` and nothing runs (Hermes on #2408)."""
    unit = unit_name(run_id)
    script = await asyncio.to_thread(_script, run_id, command)
    try:
        argv = unit_argv(run_id, cwd=cwd, script=script, timeout=timeout)
        await PREPARE()
        why = admit() if admit is not None else ""
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(script)
        raise
    if why:
        with contextlib.suppress(OSError):
            os.unlink(script)
        raise Revoked(why)
    began = time.monotonic()
    buf = _Bounded()
    timed_out = False
    proc = None
    _UNITS[run_id] = (*owner, unit)
    launching = asyncio.ensure_future(LAUNCH(argv))
    try:
        proc = await asyncio.shield(launching)

        async def pump_and_wait() -> None:
            while chunk := await proc.stdout.read(65536):
                buf.add(chunk)
            await proc.wait()

        try:
            await asyncio.wait_for(pump_and_wait(), timeout)
        except TimeoutError:
            timed_out = True
            await _halt(unit, proc)
        rc = proc.returncode
    except asyncio.CancelledError:
        # A Stop while the unit was being created: the launch is awaited (shielded) so its client
        # is known, then the unit is stopped, the client ended, and the unit stopped again, so a
        # unit created late can't survive the Stop. RuntimeMaxSec bounds it in any case.
        with contextlib.suppress(Exception):
            if proc is None:
                proc = await asyncio.shield(launching)
        with contextlib.suppress(Exception):
            await asyncio.shield(_halt(unit, proc))
        raise
    finally:
        _UNITS.pop(run_id, None)
        with contextlib.suppress(OSError):
            os.unlink(script)
    stopped = run_id in _STOP_ASKED
    _STOP_ASKED.discard(run_id)
    output = await asyncio.to_thread(guard_output, buf.text())
    return {
        "exit_code": rc if isinstance(rc, int) else None,
        "output": output,
        "truncated": buf.total > OUTPUT_HEAD + OUTPUT_TAIL or len(output) >= OUTPUT_KEEP,
        "bytes": buf.total,
        "ms": int((time.monotonic() - began) * 1000),
        "timed_out": timed_out,
        "stopped": stopped,
    }


async def _halt(unit: str, proc) -> None:
    """Stop the unit (its whole control group), end the local ``systemd-run`` client, and stop
    the unit once more: a start request the client sent just before it ended is stopped too."""
    try:
        await STOP_UNIT(unit)
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        with contextlib.suppress(Exception):
            await asyncio.sleep(0.3)
            await STOP_UNIT(unit)


def _ask_stop(run_id: str) -> bool:
    """Stop one command: its run task is cancelled, which stops its unit (`execute`) or, before
    the launch, means it never starts. Synchronous."""
    task = _TASKS.get(run_id)
    if run_id not in _UNITS and (task is None or task.done() or run_id not in _OWNERS):
        return False
    _STOP_ASKED.add(run_id)
    if task is not None and not task.done():
        task.cancel()
    return True


def _running() -> dict[str, tuple[str, str]]:
    """The commands of this process that may still run: ``{pid: (cid, turn_id)}``."""
    out = dict(_OWNERS)
    out.update({pid: (c, t) for pid, (c, t, _u) in list(_UNITS.items())})
    return out


def stop_turn(cid: str, turn_id: str) -> int:
    """The Commander stopped a turn (#2076): every command that turn started stops. Synchronous,
    no await (it runs inside `ask_orchestrator.request_stop`). Returns how many."""
    ids = [pid for pid, (c, t) in _running().items() if c == cid and t == turn_id]
    return sum(_ask_stop(pid) for pid in ids)


def stop_all() -> int:
    """Stop all (#2103): every command HQ is running. Synchronous."""
    return sum(_ask_stop(pid) for pid in list(_running()))


def stop_card(pid: str) -> bool:
    """A running card's own Stop (the Commander tapped it). Synchronous."""
    return _ask_stop(pid)


# ---- files -------------------------------------------------------------------------------------


def _sha256_file(path: str) -> str | None:
    """The file's sha256 when it is at most 1 MiB, else None. Blocking."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            size += len(chunk)
            if size > FILE_HASH_MAX:
                return None
            h.update(chunk)
    return h.hexdigest()


def read_file(path: object, *, offset: object = None, limit: object = None) -> dict:
    """``file_read``: up to :data:`FILE_READ_LINES` lines from ``offset`` (1-based), guarded, with
    the file's sha256 (what a ``file_write`` of it binds to). Blocking."""
    real = resolve(path)
    if not os.path.isfile(real):
        raise Refused("that is not a file", "not_found")
    start = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0 else 1
    count = (
        min(limit, FILE_READ_LINES)
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else FILE_READ_LINES
    )
    with open(real, "rb") as f:
        sniff = f.read(8192)
    if b"\x00" in sniff:
        raise Refused("that file is binary; HQ reads text files only", "binary")
    lines: list[str] = []
    chars = 0
    more = False
    eof = False
    n = 0
    # Bytes, a bounded line at a time (Hermes on #2408): a physical line longer than
    # FILE_LINE_MAX is never materialized (its rest is skipped in bounded pieces and the line is
    # withheld), and reading stops once the page is full.
    with open(real, "rb") as f:
        while True:
            raw = f.readline(FILE_LINE_MAX + 1)
            if not raw:
                eof = True
                break
            # A chunk that already ends with its newline is the whole line, even at exactly
            # FILE_LINE_MAX + 1 bytes (Hermes on #2408): draining then would eat the next line.
            long = len(raw) > FILE_LINE_MAX and not raw.endswith(b"\n")
            if long:
                while (rest := f.readline(65536)) and not rest.endswith(b"\n"):
                    pass
            n += 1
            if n < start:
                continue
            if len(lines) >= count or chars >= FILE_READ_CHARS:
                more = True
                break
            text = (
                f"[a line of more than {FILE_LINE_MAX} bytes withheld: too long to read]"
                if long
                else raw.decode("utf-8", errors="replace").rstrip("\r\n")
            )
            lines.append(text)
            chars += len(text)
    text = guard_output("\n".join(lines), FILE_READ_CHARS)
    return {
        "path": real,
        "size": os.path.getsize(real),
        "sha256": _sha256_file(real) or "",
        "from_line": start,
        "lines": len(lines),
        # Counted only when this read reached the end; a page that stopped early says `more`.
        **({"total_lines": n} if eof else {}),
        "more": more,
        "untrusted_content": text,
    }


def list_dir(path: object) -> dict:
    """``file_list``: the folder's entries (name, type, size), folders first, at most
    :data:`LIST_MAX`. Names pass the guard. Blocking."""
    real = resolve(path, folder=True)
    rows: list[tuple[int, str, str, int]] = []
    with os.scandir(real) as it:
        for e in it:
            try:
                kind = (
                    "dir"
                    if e.is_dir(follow_symlinks=False)
                    else ("link" if e.is_symlink() else "file")
                )
                size = e.stat(follow_symlinks=False).st_size if kind == "file" else 0
            except OSError:
                kind, size = "unknown", 0
            rows.append((0 if kind == "dir" else 1, e.name, kind, size))
    rows.sort()
    names = guard_output("\n".join(r[1] for r in rows[:LIST_MAX]), 10**6).split("\n")
    entries = [
        {"name": n, "type": r[2], **({"size": r[3]} if r[2] == "file" else {})}
        for n, r in zip(names, rows[:LIST_MAX], strict=False)
    ]
    return {"path": real, "entries": entries, "count": len(rows), "more": len(rows) > LIST_MAX}


def _write(row: dict, admit: Callable[[], str]) -> dict:
    """A write card's effect: a NEW file created ``O_EXCL`` (refused if one appeared), or an
    existing file replaced through the Files editor's own guarded save
    (`fileedit.save_bytes`, ``docs/invariants/file-edit.md``): bound to the sha256 HQ staged it
    against, displaced into the recovery store, never overwritten. ``admit`` is asked right
    before the effect (and by the save before displacement and installation). Blocking."""
    from . import fileedit

    path = row["path"]
    data = row["content"].encode("utf-8")
    real = resolve(path)
    if real != path:
        return {"state": STALE, "reason": "the path resolves elsewhere now; nothing was written"}
    base = row.get("base_sha") or ""
    if not base:
        why = admit()
        if why:
            return {"state": PENDING, "reason": why}
        parent, name = os.path.split(real)
        try:
            dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            return {"state": STALE, "reason": "its folder is gone; nothing was written"}
        try:
            try:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=dfd,
                )
            except FileExistsError:
                return {"state": STALE, "reason": "a file of that name appeared; nothing written"}
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return {"state": DONE, "bytes": len(data)}
    root = next((r for r in allowed_roots() if _within(real, r)), None)
    if root is None:
        return {"state": STALE, "reason": "the file is outside the roots now; nothing written"}

    refused: dict = {}

    def policy(_p: str) -> None:
        why = admit()
        if why:
            refused["why"] = why
            raise fileedit.SaveRefused(why, reason="authority")

    try:
        fileedit.save_bytes(real, data, base, root=root, admit=policy)
    except fileedit.FsError as e:
        if refused:
            return {"state": PENDING, "reason": refused["why"]}
        reason = getattr(e, "reason", "") or ""
        if reason == "changed" or "changed" in str(e) or "version" in str(e):
            return {"state": STALE, "reason": "the file changed since HQ read it; nothing written"}
        return {"state": FAILED, "reason": f"{e}"[:300]}
    return {"state": DONE, "bytes": len(data)}


# ---- the card -----------------------------------------------------------------------------------


def binding(row: dict) -> str:
    """The card ↔ entry binding over exactly what runs (keyed, so a sidecar edit can't forge it)."""
    canon = json.dumps(
        [
            row.get("action"),
            row.get("command") or "",
            row.get("path") or "",
            row.get("cwd") or "",
            int(row.get("timeout") or 0),
            hashlib.sha256((row.get("content") or "").encode()).hexdigest(),
            row.get("base_sha") or "",
        ],
        ensure_ascii=False,
    )
    return template_secrets.keyed_digest("hq-command", canon)


def interrupted_reason(row: dict) -> str:
    return INTERRUPTED_REASON_WRITE if row.get("action") == "write" else INTERRUPTED_REASON


def view(row: dict) -> dict:
    state = row.get("state")
    out = {
        "state": state,
        "action": row.get("action") or "run",
        "cwd": row.get("cwd") or "",
        "timeout": row.get("timeout") or 0,
        "reason": row.get("reason") or "",
        "can_run": state == PENDING,
        "can_dismiss": state == PENDING,
        "can_stop": state == DECIDING and row.get("action") != "write",
        **({"decided_by": "hq"} if row.get("decided_by") == "hq" and state != PENDING else {}),
    }
    if row.get("action") == "write":
        content = row.get("content") or ""
        out.update(
            path=row.get("path") or "",
            new=not row.get("base_sha"),
            size=len(content.encode()),
            lines=content.count("\n") + (0 if content.endswith("\n") or not content else 1),
            preview=content[:CARD_PREVIEW_MAX],
            preview_cut=len(content) > CARD_PREVIEW_MAX,
            workspace=in_workspace(row.get("path") or ""),
        )
    else:
        out["command"] = row.get("command") or ""
    for k in ("exit_code", "output", "truncated", "ms", "bytes"):
        if k in row and row[k] is not None:
            out[k] = row[k]
    return out


def model_result(row: dict) -> dict:
    """What HQ's model is told of a settled command card: the facts and the guarded output."""
    out: dict = {"state": row.get("state")}
    for k in ("exit_code", "truncated", "ms", "reason"):
        if row.get(k) not in (None, ""):
            out[k] = row[k]
    if row.get("action") != "write":
        text = row.get("output") or ""
        if len(text) > MODEL_OUTPUT_MAX:
            head = text[:1_500]
            tail = text[-(MODEL_OUTPUT_MAX - 1_560) :]
            text = f"{head}\n[… {len(text) - len(head) - len(tail)} characters cut …]\n{tail}"
            out["truncated"] = True
        out["untrusted_output"] = text
    return out


def _timeout(value: object) -> int:
    if value is None:
        return TIMEOUT_DEFAULT_S
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= TIMEOUT_MAX_S:
        raise Refused(f"timeout_s must be a whole number of seconds from 1 to {TIMEOUT_MAX_S}")
    return value


def _unchanged_by_guard(text: str, what: str) -> None:
    """A secret or a credential-shaped text is refused, never redacted into something else: a
    command (or file) must reach its secrets by reference (`battlelab-secrets`), never inline."""
    try:
        same = ask_store.guard_text(text, len(text)) == text
    except ask_store.StoreRefused:
        raise Refused(f"{what} could not be checked for secrets", "unavailable") from None
    if not same:
        raise Refused(
            f"refused: the {what} holds a secret or something that looks like a credential. "
            "Use a secret by reference (the Commander grants it; the command reads the file the "
            "secrets helper names), never its value inline",
            "credential",
        )


def prepare_run(args: dict) -> dict:
    """Validate a ``shell_run`` call into a sidecar entry's fields. Blocking."""
    ok, why = available()
    if not ok:
        raise Refused(f"refused: HQ's shell is unavailable on this host: {why}", "unavailable")
    command = _clean_text(args.get("command"), "command", COMMAND_MAX)
    _unchanged_by_guard(command, "command")
    cwd = resolve(args.get("cwd") or str(workspace()), folder=True)
    _systemd_word(cwd, "the folder")
    return {
        "action": "run",
        "command": command,
        "cwd": cwd,
        "timeout": _timeout(args.get("timeout_s")),
    }


def prepare_write(args: dict) -> dict:
    """Validate a ``file_write`` call: the path, the whole new content, and the sha256 of the
    file HQ read (``expected_sha256``; required when the file exists). Blocking."""
    content = args.get("content")
    if not isinstance(content, str):
        raise Refused("content must be the file's whole new text")
    if len(content) > FILE_WRITE_MAX:
        raise Refused(f"content is longer than {FILE_WRITE_MAX} characters", "too_long")
    if "\x00" in content:
        raise Refused("content must be text")
    if content:
        _unchanged_by_guard(content, "file content")
    real = resolve(args.get("path"))
    if os.path.isdir(real):
        raise Refused("that path is a folder")
    if not os.path.isdir(os.path.dirname(real)):
        raise Refused("its folder does not exist; create it with shell_run first", "not_found")
    base = ""
    if os.path.lexists(real):
        if not os.path.isfile(real) or os.path.islink(real):
            raise Refused("that path is not a regular file")
        current = _sha256_file(real)
        if current is None:
            raise Refused("that file is larger than 1 MiB; HQ does not replace it", "too_long")
        expected = args.get("expected_sha256")
        if expected != current:
            raise Refused(
                "the file exists: read it with file_read first and pass its sha256 as "
                "expected_sha256 (it changed, or you did not read it)",
                "stale",
            )
        base = current
    return {
        "action": "write",
        "path": real,
        "cwd": os.path.dirname(real),
        "timeout": 0,
        "content": content,
        "base_sha": base,
    }


async def stage(*, cid: str, turn_id: str, fields: dict, append_card) -> dict:
    """Stage one command card: the conversation's card record, then the sidecar entry."""
    from . import ask_proposals

    pid = uuid.uuid4().hex
    bind = await asyncio.to_thread(binding, fields)
    card: dict = {
        "type": "proposal",
        "turn_id": turn_id,
        "action_id": pid,
        "kind": KIND,
        "binding": bind,
        "action": fields["action"],
        "cwd": fields["cwd"],
        "timeout": fields["timeout"],
    }
    if fields["action"] == "write":
        card["path"] = fields["path"]
        card["text"] = f"{'Create' if not fields['base_sha'] else 'Replace'} {fields['path']}"
    else:
        card["text"] = fields["command"]
    try:
        await append_card(cid, card)
    except Exception:
        raise Refused(
            "refused: the card could not be recorded; nothing will run", "not_recorded"
        ) from None
    entry = {"id": pid, "turn_id": turn_id, "kind": KIND, "state": PENDING, "ts": time.time(),
             "binding": bind, **fields}  # fmt: skip
    try:
        await asyncio.to_thread(ask_proposals.add, cid, entry)
    except Exception:
        raise Refused(
            "refused: the card could not be recorded; nothing will run", "not_recorded"
        ) from None
    return {"id": pid, **fields}


def _card_binding(cid: str, pid: str) -> str | None:
    for r in ask_store.read(cid):
        if r.get("type") == "proposal" and r.get("action_id") == pid and r.get("kind") == KIND:
            return r.get("binding")
    return None


def precheck(cid: str, row: dict) -> str | None:
    """Why this entry may not run NOW, or None. Blocking."""
    expect = binding(row)
    if row.get("binding") != expect or _card_binding(cid, row["id"]) != expect:
        return "the card changed since it was proposed; nothing ran"
    try:
        if row.get("action") == "write":
            real = resolve(row.get("path"))
            if real != row.get("path"):
                return "the path resolves elsewhere now; nothing was written"
            base = row.get("base_sha") or ""
            if base:
                if not os.path.isfile(real) or _sha256_file(real) != base:
                    return "the file changed since HQ read it; nothing was written"
            elif os.path.lexists(real):
                return "a file of that name exists now; nothing was written"
        else:
            ok, why = available()
            if not ok:
                return f"HQ's shell is unavailable here: {why}; nothing ran"
            if resolve(row.get("cwd"), folder=True) != row.get("cwd"):
                return "the folder resolves elsewhere now; nothing ran"
            _unchanged_by_guard(row.get("command") or "", "command")
    except Refused as e:
        return f"{e}; nothing ran"
    return None


ACT_OP_RUN, ACT_LABEL_RUN = "shell.run", "Run a command"
ACT_OP_WRITE, ACT_LABEL_WRITE = "file.write", "Write a file"


def _act_names(row: dict) -> tuple[str, str, str]:
    if row.get("action") == "write":
        return ACT_OP_WRITE, ACT_LABEL_WRITE, str(row.get("path") or "")
    return ACT_OP_RUN, ACT_LABEL_RUN, str(row.get("command") or "")[:200]


async def decide_locked(cid: str, row: dict, fd: int, decision: str) -> tuple[int, dict]:
    """Decide a command entry (Run / Dismiss). OWNS ``fd`` (the decide lock) from the call on."""
    from . import ask_proposals

    handed = False
    try:
        if row.get("state") != PENDING:
            return 200, view(row)
        if decision == "dismiss":
            done = await asyncio.to_thread(
                ask_proposals._update, cid, row["id"], state=DISMISSED, settled_at=time.time()
            )
            return 200, view(done or row)
        if decision != "confirm":
            raise ask_proposals.ProposalError(422, "a command is confirmed or dismissed")
        status, out, task = await _run_locked(cid, row, fd)
        handed = task is not None
        return status, out
    finally:
        if not handed:
            os.close(fd)


def _back_to_pending(cid: str, pid: str, reason: str) -> dict | None:
    from . import ask_proposals

    return ask_proposals._update(
        cid, pid, state=PENDING, decided_at=None, decided_by=None, authority_rev=None,
        reason=reason,
    )  # fmt: skip


async def _run_locked(cid: str, row: dict, fd: int, act=None):
    """A pending command's run, under the decide lock ``fd``, shared by the tap and HQ's act.
    Returns ``(status, view, task)``; the task owns ``fd`` when there is one. ``act``
    (`hq_acts.Act`): the grant is asked before the ``deciding`` write (raises ``hq_acts.Waits``:
    nothing written, nothing run), the write records ``decided_by: "hq"``, the claim is logged
    (fail closed), and the task asks the grant again immediately before the launch."""
    from . import ask_proposals, hq_acts

    pid = row["id"]
    why = await asyncio.to_thread(precheck, cid, row)
    if why:
        done = await asyncio.to_thread(
            ask_proposals._update, cid, pid, state=STALE, reason=why, settled_at=time.time()
        )
        return 200, view(done or row), None
    by_hq: dict = {}
    if act is not None:
        why = await asyncio.to_thread(act.admits, row)
        if why:
            raise hq_acts.Waits(why)
        by_hq = {"decided_by": "hq", "authority_rev": act.revision}
    # DURABLE BEFORE ANYTHING RUNS: from here a crash leaves `deciding`, which the next read
    # settles as `interrupted` and never runs again.
    done = await asyncio.to_thread(
        ask_proposals._update, cid, pid, state=DECIDING, decided_at=time.time(), reason="", **by_hq
    )
    if done is None:
        if act is not None:
            raise hq_acts.Waits("the card is no longer tracked")
        raise ask_proposals.ProposalError(404, "no such proposal")
    op, label, title = _act_names(row)
    if act is not None and not await asyncio.to_thread(
        act.claimed, done, cid, op=op, label=label, title=title
    ):
        await asyncio.to_thread(_back_to_pending, cid, pid, "")
        raise hq_acts.Waits("the acts log could not be written, so HQ did not run it")
    if row.get("action") != "write":
        _OWNERS[pid] = (cid, str(row.get("turn_id") or ""))
    task = asyncio.create_task(_run_task(cid, dict(row), fd, act=act))
    _TASKS[pid] = task
    return 202, view(done), task


async def act_run(cid: str, row: dict, fd: int, act) -> asyncio.Task | dict:
    """HQ runs a command card itself (#2404): the card's own run (`_run_locked`). Returns the
    task (it owns ``fd``), or the entry settled stale. Raises ``hq_acts.Waits``."""
    from . import ask_proposals

    _status, out, task = await _run_locked(cid, row, fd, act=act)
    if task is None:
        return await asyncio.to_thread(ask_proposals._row, cid, row["id"]) or out
    return task


async def _run_task(cid: str, row: dict, fd: int, act=None) -> None:
    """Run once and settle, whatever happens. Owns ``fd``."""
    from . import ask_proposals

    pid = row["id"]
    settle: dict = {}
    started = False

    def admit() -> str:
        return act.admits(row) if act is not None else ""

    try:
        if row.get("action") == "write":
            started = True
            settle = await asyncio.to_thread(_write, row, admit)
            return
        why = await asyncio.to_thread(precheck, cid, row)
        if why:
            settle = {"state": STALE, "reason": why}
            return
        # An early check (cheap); the binding one is inside `execute`, after every await that
        # prepares the launch and immediately before the unit's start request.
        why = await asyncio.to_thread(admit)
        if why:
            settle = {"state": PENDING, "reason": why}
            return
        started = True
        try:
            result = await execute(
                pid,
                command=row["command"],
                cwd=row["cwd"],
                timeout=int(row["timeout"]),
                owner=(cid, str(row.get("turn_id") or "")),
                admit=admit if act is not None else None,
            )
        except Revoked as e:
            settle = {"state": PENDING, "reason": e.reason}  # nothing ran
            return
        state = (
            STOPPED if result["stopped"]
            else TIMED_OUT if result["timed_out"]
            else DONE if result["exit_code"] == 0
            else FAILED
        )  # fmt: skip
        reason = {
            STOPPED: "stopped by the Commander",
            TIMED_OUT: f"it ran past its {row['timeout']} s timeout and was stopped",
            FAILED: f"it exited with code {result['exit_code']}",
        }.get(state, "")
        settle = {"state": state, "reason": reason, **{k: result[k] for k in
                  ("exit_code", "output", "truncated", "ms", "bytes")}}  # fmt: skip
    except asyncio.CancelledError:
        if pid in _STOP_ASKED:
            # A Stop is not a shutdown: the unit is stopped, the card settles, and the
            # cancellation ends here.
            _STOP_ASKED.discard(pid)
            me = asyncio.current_task()
            if me is not None:
                me.uncancel()
            settle = {"state": STOPPED, "reason": "stopped by the Commander"}
            return
        settle = {}  # shutdown: `deciding` reads interrupted, never re-run
        raise
    except Exception:  # noqa: BLE001 — every run ends settled
        log.exception("hq command %s failed", pid)
        settle = {
            "state": FAILED,
            "reason": "it ended unexpectedly; it is not run again"
            if started
            else "it could not start; nothing ran",
        }
    finally:
        try:
            if settle.get("state") == PENDING:
                await asyncio.to_thread(_back_to_pending, cid, pid, settle.get("reason") or "")
            elif settle:
                await asyncio.to_thread(
                    ask_proposals._update, cid, pid, settled_at=time.time(), **settle
                )
            if settle and act is not None:
                op, label, title = _act_names(row)
                await asyncio.to_thread(
                    act.settled, pid, cid, op=op, label=label, title=title,
                    state=str(settle.get("state") or ""), outcome=str(settle.get("reason") or ""),
                )  # fmt: skip
        finally:
            os.close(fd)
            _TASKS.pop(pid, None)
            _OWNERS.pop(pid, None)
            _STOP_ASKED.discard(pid)


async def drain(timeout: float = 10.0) -> None:
    """Shutdown: every running command's unit is stopped (its task's cancellation does it); the
    entries stay ``deciding`` and read ``interrupted``. Never re-run."""
    tasks = list(_TASKS.values())
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
