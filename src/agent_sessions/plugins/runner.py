"""Bounded transport for the fixed system SSH feed signer/verifier (#1259).

This is not a vendor CLI executor or a sandbox for arbitrary processes. Only ssh-keygen's
sign/verify argv shapes are admitted. Timeout/output teardown kills its original process group;
it does not claim containment of a hostile program which detaches into another session.
No argv, input or output is logged. Sign-in and vendor probes require their own containment.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class StepError(ValueError):
    pass


@dataclass(frozen=True)
class Result:
    code: int
    output: bytes


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    data: bytes = b"",
    timeout: float = 30,
    max_output: int = 65536,
) -> Result:
    verify = (
        len(argv) == 11
        and argv[:4] == ["/usr/bin/ssh-keygen", "-Y", "verify", "-f"]
        and argv[5] == "-I"
        and argv[7] == "-n"
        and argv[9] == "-s"
    )
    sign = (
        len(argv) == 7
        and argv[:4] == ["/usr/bin/ssh-keygen", "-Y", "sign", "-f"]
        and argv[5] == "-n"
    )
    if not (verify or sign) or not os.path.isabs(argv[4]):
        raise StepError("only the fixed system SSH feed signer/verifier is admitted")
    if verify and not os.path.isabs(argv[10]):
        raise StepError("the signature path must be absolute")
    if any(not isinstance(value, str) or "\x00" in value for value in argv):
        raise StepError("invalid SSH verification argument")
    if timeout <= 0 or max_output <= 0:
        raise StepError("invalid step limits")
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(  # noqa: S603 — fixed system SSH sign/verify argv validated above
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    output = bytearray()
    pending = memoryview(data)
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(proc.stdout.fileno(), False)
            selector.register(proc.stdout, selectors.EVENT_READ)
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE)
            else:
                proc.stdin.close()
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise StepError("the step exceeded its time limit")
                for key, mask in selector.select(min(left, 0.2)):
                    if mask & selectors.EVENT_WRITE:
                        try:
                            count = os.write(key.fd, pending[:65536])
                            pending = pending[count:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    if mask & selectors.EVENT_READ:
                        chunk = os.read(key.fd, min(65536, max_output - len(output) + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > max_output:
                                raise StepError("the step exceeded its output limit")
            try:
                # Leave the child unreaped until group teardown below: its pid cannot be reused.
                while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    if time.monotonic() >= deadline:
                        raise StepError("the step exceeded its time limit")
                    time.sleep(0.02)
            except ChildProcessError:
                raise StepError("the step's process state was lost") from None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()
        proc.stdin.close()
    return Result(proc.returncode, bytes(output))
