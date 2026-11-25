"""Run one command under a file-size limit (#2145): a single-threaded exec trampoline.

A skill import fetches a third-party repository with git. Its total size is watched while it
runs (`skill_repo`), and every single file git writes is capped by the kernel too:

    <python> -I -m agent_sessions.fsize_exec --fsize <bytes> -- <argv...>

This fresh process sets `RLIMIT_FSIZE` on itself, restores the default SIGXFSZ disposition (Python
ignores it at startup and an ignored disposition survives exec) and `os.execvp`s the command: same
pid, same pipes, same environment. The limit is in place before git can write anything, and every
helper git starts (`git-remote-https`, `index-pack`) inherits it.

Not `preexec_fn`: the app is multithreaded (`asyncio.to_thread`), and a forked child may deadlock
before `exec` (the reason `native_exec` exists). No shell: the argv is executed exactly as given.
The limit may only be a positive byte count no larger than `MAX`; anything else is refused and the
command never runs.
"""

from __future__ import annotations

import os
import resource
import signal
import sys

MODULE = "agent_sessions.fsize_exec"
#: The largest limit this trampoline sets: it exists to bound, never to lift one.
MAX = 1 << 30
EXIT_REFUSED = 2
EXIT_EXEC_FAILED = 127


class Refused(ValueError):
    pass


def argv(python: str, limit: int, command: list[str]) -> list[str]:
    """The trampoline's literal argv around the command's own (never a shell)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= MAX:
        raise Refused("file size limit out of range")
    if not python.startswith("/") or not command:
        raise Refused("expected an absolute interpreter and a command")
    return [python, "-I", "-m", MODULE, "--fsize", str(limit), "--", *command]


def parse(args: list[str]) -> tuple[int, list[str]]:
    """`--fsize <n> -- <command...>`: exactly that."""
    if len(args) < 4 or args[0] != "--fsize" or args[2] != "--" or not args[1].isdigit():
        raise Refused("usage: --fsize <bytes> -- <command...>")
    limit = int(args[1])
    if not 0 < limit <= MAX:
        raise Refused("file size limit out of range")
    if not args[3]:
        raise Refused("empty command")
    return limit, args[3:]


def main(args: list[str] | None = None) -> int:
    try:
        limit, command = parse(sys.argv[1:] if args is None else args)
        _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        if hard != resource.RLIM_INFINITY and hard < limit:
            limit = hard  # never raised past what this process may already write
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    except (Refused, ValueError, OSError) as exc:
        print(f"fsize_exec refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    for name in ("SIGPIPE", "SIGXFSZ"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, signal.SIG_DFL)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)  # noqa: S606 - a literal argv, no shell
    except OSError as exc:
        print(f"fsize_exec: the command could not start: {exc.strerror}", file=sys.stderr)
    return EXIT_EXEC_FAILED


if __name__ == "__main__":
    sys.exit(main())
