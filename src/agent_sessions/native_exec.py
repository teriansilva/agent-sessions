"""Start a native agent with a raised `oom_score_adj` (#1732): a single-threaded exec trampoline.

An API session's worker, its agent and every tool the agent runs share one unit and one memory
cap. Under `OOMPolicy=continue` an overrun kills ONE process the kernel selects by badness (RSS
plus an `oom_score_adj` share), so the worker must never be the preferred victim. The worker
therefore starts its agent as

    <worker python> -I -m agent_sessions.native_exec --oom-score-adj <n> -- <agent argv...>

This fresh process writes `n` to its own `/proc/self/oom_score_adj` and then `os.execvp`s the
agent: same pid, same pipes, same environment and working directory. The value is in place before
the agent can fork any tool or MCP server, and everything the agent starts inherits it.

Not `preexec_fn`: the worker is multithreaded (`asyncio.to_thread`), and a forked child may
deadlock before `exec`. No host tool (`choom` is not on every install) and no shell: the agent's
argv is executed exactly as given.

`n` may only RAISE the value this process inherited (an unprivileged raise needs no privilege; a
higher value makes the agent subtree easier to kill, never harder). Anything else is refused and
the agent never runs. A failed write of a valid raise is reported on stderr and the agent still
starts: no worse than before #1732, and never a lost session over a ranking hint.
"""

from __future__ import annotations

import os
import re
import signal
import sys

MODULE = "agent_sessions.native_exec"
MAX = 1000  # the kernel's bound for oom_score_adj
EXIT_REFUSED = 2
EXIT_EXEC_FAILED = 127
#: How far above the worker's own value the agent subtree is ranked. Each point adds 0.1% of the
#: unit's memory limit to every agent-side process's badness, so +100 (10% of the cap) outranks a
#: worker of tens of MB, while tools that rank themselves (Chrome's renderers write 300 and up)
#: still go before the agent. Between the agent and its tools, RSS decides.
RAISE = 100
_VALUE = re.compile(r"-?[0-9]{1,4}")
_SELF = "/proc/self/oom_score_adj"


class Refused(ValueError):
    pass


def own_value(path: str = _SELF) -> int | None:
    """This process's `oom_score_adj`, or None when it cannot be read."""
    try:
        with open(path) as fh:
            text = fh.read(16).strip()
    except OSError:
        return None
    return int(text) if _VALUE.fullmatch(text) and -MAX <= int(text) <= MAX else None


def raised(own: int) -> int:
    """The agent's value for a worker at `own`: above it, never past the kernel's bound."""
    return min(MAX, own + RAISE)


def argv(python: str, value: int, command: list[str]) -> list[str]:
    """The trampoline's literal argv around the agent's own (never a shell)."""
    if not isinstance(value, int) or isinstance(value, bool) or not -MAX <= value <= MAX:
        raise Refused("oom_score_adj out of range")
    if not python.startswith("/") or not command:
        raise Refused("expected an absolute interpreter and an agent command")
    return [python, "-I", "-m", MODULE, "--oom-score-adj", str(value), "--", *command]


def parse(args: list[str], own: int | None) -> tuple[int, list[str]]:
    """`--oom-score-adj <n> -- <command...>`: exactly that, with `n` a raise of `own`."""
    if len(args) < 4 or args[0] != "--oom-score-adj" or args[2] != "--":
        raise Refused("usage: --oom-score-adj <n> -- <command...>")
    if _VALUE.fullmatch(args[1]) is None or not -MAX <= int(args[1]) <= MAX:
        raise Refused("oom_score_adj must be an integer in -1000..1000")
    value = int(args[1])
    if own is None or value < own:
        raise Refused("oom_score_adj may only be raised")
    command = args[3:]
    if not command[0]:
        raise Refused("empty agent command")
    return value, command


def main(args: list[str] | None = None) -> int:
    try:
        value, command = parse(sys.argv[1:] if args is None else args, own_value())
    except Refused as exc:
        print(f"native_exec refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        with open(_SELF, "w") as fh:
            fh.write(str(value))
    except OSError as exc:
        print(f"native_exec: oom_score_adj not raised: {exc.strerror}", file=sys.stderr)
    # Python ignores SIGPIPE/SIGXFSZ at startup and an ignored disposition survives exec: restore
    # the defaults the agent had when the worker started it directly (`restore_signals`).
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, signal.SIG_DFL)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)  # noqa: S606 - the agent's literal argv, no shell
    except OSError as exc:
        print(f"native_exec: the agent could not start: {exc.strerror}", file=sys.stderr)
    return EXIT_EXEC_FAILED


if __name__ == "__main__":
    sys.exit(main())
