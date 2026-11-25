"""Record how a native worker's unit ended (#1643), from systemd's own `ExecStopPost=` hook.

The worker runs as a transient `--collect` unit, so once it fails its `Result` is unloaded with
it. systemd hands this hook `$SERVICE_RESULT` / `$EXIT_CODE` / `$EXIT_STATUS` and the unit's
`$INVOCATION_ID`: the one place that says a generation was killed for memory (`oom-kill`) rather
than ending some other way. WHICH limit did the kill is not recorded: no cgroup counter ties one
kill to one limit (`memory.events` counts are cumulative, and a parent's or the host's OOM kills
this unit's processes too), so it is never inferred (Hermes on #1651).

The write is fenced to the pinned invocation: only when `$INVOCATION_ID` equals the
`invocation_id` the runtime recorded for this worker does `exit` land in its lifecycle. Anything
else (no pin yet, a mismatch, a malformed value) writes nothing, and the reader falls back to the
generic "worker ended". The record only explains an ending; it never decides whether a turn's
work was applied, and nothing is ever resent because of it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from . import native_state

_RESULT = re.compile(r"[a-z][a-z-]{0,31}")
_CODE = re.compile(r"[a-z]{1,16}")
_STATUS = re.compile(r"[A-Za-z0-9+-]{1,16}")
_INVOCATION = re.compile(r"[0-9a-f]{32}")


def _value(name: str, pattern: re.Pattern) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and pattern.fullmatch(value) else None


def record(worker_id: str) -> bool:
    """Write `exit` for this worker if the invocation matches its pin. True when written."""
    result = _value("SERVICE_RESULT", _RESULT)
    invocation = _value("INVOCATION_ID", _INVOCATION)
    if result is None or invocation is None:
        return False
    life = native_state.read_lifecycle(worker_id)
    if life.get("invocation_id") != invocation:
        return False
    native_state.update_lifecycle(
        worker_id,
        exit={
            "result": result,
            "code": _value("EXIT_CODE", _CODE),
            "status": _value("EXIT_STATUS", _STATUS),
            "invocation_id": invocation,
            # When it ended: a memory reading the worker took just before is evidence of where
            # it stood, never of which limit did the kill (#1651).
            "at": time.time(),
        },
    )
    return True


def main(argv: list[str] | None = None) -> int:
    from .native_worker import WorkerError, _adopt_environment, _private_file

    parser = argparse.ArgumentParser(prog="agent_sessions.native_exit_record")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    try:
        worker_id = native_state._uuid(args.worker_id, "worker id")
        config = _private_file(state_dir / "workers" / worker_id / "config.json")
        if config.get("worker_id") != worker_id:
            raise WorkerError("worker config belongs to another generation")
        _adopt_environment(config, state_dir)
        record(worker_id)
    except (OSError, ValueError, KeyError, WorkerError, native_state.StateError) as exc:
        print(f"native exit record skipped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
