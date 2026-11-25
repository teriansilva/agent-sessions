"""The installed release decides a queued message's admission (#2165).

API-session workers outlive app updates. A worker re-admits each queued message (and each Send
now) at delayed handoff by rebuilding the roster (`native_worker.admit_queued`, #1538). With its
OWN code, an older worker would read a manifest field a newer release committed as invalid (the
manifest parser rejects unknown fields on purpose) and refuse every queued message as
`agent_removed`. So a worker that is not the installed release asks the installed one:

* **Not installed** (its package is not under ``<home>/releases/<id>/``: dev, tests): in-process.
* **Installed, same release** (``<home>/current`` resolves to its own release): in-process.
* **Installed, another release**: this module, run by that release's interpreter as a short-lived
  child, decides. ``current`` is resolved ONCE, and the argv is built from the resolved release
  directory, never through the mutable symlink.
* **Installed, but ``current`` is missing, dangling or not a release directory**: refused. Never
  the stale parser.

The protocol is versioned and exact. stdin: ``{"v": 1, "session_key", "adapter", "config"}``.
stdout: exactly ``{"v": 1, "ok": true}`` (admitted) or ``{"v": 1, "refused": <code>}`` with a code
from `CODES`, with exit 0. Anything else refuses as the existing `agent_changed`: no new reason
code, since `native_ipc.REASON_CODES` is a closed set that older readers enforce. The child
persists nothing and takes no storage lock (`plugins.storage.persist_nothing`): the parent already
holds the lifecycle and ``launch`` locks across the check. The wait is an asyncio subprocess with
a deadline that includes the kill and the reap, a capped stdout and a discarded stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROTOCOL = 1
MODULE = "agent_sessions.native_admit"
#: The refusals the child may answer with; the subset of `native_ipc.REASON_CODES` admission makes.
CODES = frozenset({"agent_removed", "agent_changed", "source_store_changed"})
#: What "could not be checked" refuses as: readable by every release (contract 4 of #2165).
UNVERIFIED = "agent_changed"
#: The whole check, start to reap. The answer must arrive by `DEADLINE - REAP_RESERVE`.
DEADLINE = 5.0
REAP_RESERVE = 0.5
MAX_OUTPUT = 4096
MAX_REQUEST = 1024 * 1024

IN_PROCESS = "in_process"
DELEGATE = "delegate"
BROKEN = "broken"


def _encode(doc: dict) -> bytes:
    return json.dumps(doc).encode()


#: The ONLY answers a parent accepts, byte for byte: code None means admitted.
ANSWERS: dict[bytes, str | None] = {
    _encode({"v": PROTOCOL, "ok": True}): None,
    **{_encode({"v": PROTOCOL, "refused": code}): code for code in sorted(CODES)},
}


class Unverified(Exception):
    """The installed release could not decide. ``cause`` is a fixed phrase, safe to log: never
    the config, the child's output or its stderr."""

    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class Plan:
    """Where one admission is decided. ``release`` is the resolved installed release directory
    (`DELEGATE`); ``cause`` says why a `BROKEN` plan refuses."""

    mode: str
    release: Path | None = None
    cause: str = ""

    def argv(self) -> list[str]:
        """The child's literal argv, from the RESOLVED release directory (never `current`)."""
        assert self.mode == DELEGATE and self.release is not None
        return [str(self.release / "venv" / "bin" / "python"), "-I", "-m", MODULE]


def install_home() -> Path:
    """The install root (``install.sh``'s ``$PREFIX``), as `update._home` names it."""
    return Path(
        os.environ.get("AGENT_SESSIONS_HOME") or "~/.local/share/agent-sessions"
    ).expanduser()


def _package_file() -> Path:
    import agent_sessions

    return Path(agent_sessions.__file__)


def plan(home: Path | None = None) -> Plan:
    """Decide where this worker's admissions run (contract 1 of #2165). Resolves ``current``
    exactly once; the returned plan carries the resolved directory."""
    package = _package_file().resolve()
    root = Path(os.path.realpath(install_home() if home is None else home))
    releases = Path(os.path.realpath(root / "releases"))
    if not package.is_relative_to(releases) or len(package.relative_to(releases).parts) < 2:
        return Plan(IN_PROCESS)
    own = releases / package.relative_to(releases).parts[0]
    try:
        installed = (root / "current").resolve(strict=True)
    except (OSError, RuntimeError):
        return Plan(BROKEN, cause="the installed release pointer is missing or dangling")
    if installed.parent != releases or not installed.is_dir():
        return Plan(BROKEN, cause="the installed release pointer names no release directory")
    if installed == own:
        return Plan(IN_PROCESS)
    return Plan(DELEGATE, release=installed)


async def ask(where: Plan, session_key: str, adapter: str, config: dict) -> str | None:
    """Run the installed release's check. None: admitted; a code from `CODES`: refused by it.
    Raises `Unverified` for anything else. Cancellation kills and reaps the child first."""
    if where.mode == BROKEN:
        raise Unverified(where.cause)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEADLINE
    payload = _encode(
        {"v": PROTOCOL, "session_key": session_key, "adapter": adapter, "config": config}
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *where.argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd="/",
        )
    except OSError as exc:
        cause = f"the installed release's check did not start ({type(exc).__name__})"
        raise Unverified(cause) from None
    try:
        try:
            async with asyncio.timeout_at(deadline - REAP_RESERVE):
                out, code = await _exchange(proc, payload)
        except TimeoutError:
            raise Unverified("the installed release's check timed out") from None
    finally:
        await _reap(proc, deadline)
    if code != 0:
        raise Unverified(f"the installed release's check exited {code}")
    if out not in ANSWERS:
        raise Unverified("the installed release's check answered outside the protocol")
    return ANSWERS[out]


async def _exchange(proc, payload: bytes) -> tuple[bytes, int]:
    assert proc.stdin is not None and proc.stdout is not None
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        proc.stdin.write(payload)
        await proc.stdin.drain()
    proc.stdin.close()
    out = bytearray()
    while chunk := await proc.stdout.read(MAX_OUTPUT + 1 - len(out)):
        out += chunk
        if len(out) > MAX_OUTPUT:
            raise Unverified("the installed release's check answered too much")
    return bytes(out), await proc.wait()


async def _reap(proc, deadline: float) -> None:
    """Kill a child still running and wait for it, within what is left of the deadline (and
    at least the reap reserve, so a cancelled check still reaps)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    if proc.returncode is not None:
        return
    remaining = max(deadline - asyncio.get_running_loop().time(), REAP_RESERVE)
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        # A child that outlives this (uninterruptible sleep) is reaped by asyncio's child
        # watcher later; the admission has already been refused.
        await asyncio.wait_for(proc.wait(), remaining)


# --- the child: the installed release's side ------------------------------------------------------


def _request(raw: bytes) -> tuple[str, str, dict] | None:
    if len(raw) > MAX_REQUEST:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if (
        not isinstance(doc, dict)
        or set(doc) != {"v", "session_key", "adapter", "config"}
        or type(doc["v"]) is not int
        or doc["v"] != PROTOCOL
        or not isinstance(doc["session_key"], str)
        or not isinstance(doc["adapter"], str)
        or not isinstance(doc["config"], dict)
    ):
        return None
    return doc["session_key"], doc["adapter"], doc["config"]


def main() -> int:
    """Answer one admission check. Nothing on stdout but one protocol answer; every failure is
    a non-zero exit with no output, which the parent refuses."""
    # Before ANY roster code is imported: the registry builds a roster at import time.
    from .plugins import storage

    storage.persist_nothing()
    request = _request(sys.stdin.buffer.read(MAX_REQUEST + 1))
    if request is None:
        return 2
    session_key, adapter, config = request
    from .engines import registry
    from .native_worker import AdmissionRefused, admit_queued

    try:
        admit_queued(registry.admission_snapshot(), session_key, adapter, config)
    except AdmissionRefused as exc:
        if exc.code not in CODES:
            return 3
        answer = {"v": PROTOCOL, "refused": exc.code}
    except Exception:  # noqa: BLE001 — a config shape or state it can't read: no answer
        return 3
    else:
        answer = {"v": PROTOCOL, "ok": True}
    sys.stdout.buffer.write(_encode(answer))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
