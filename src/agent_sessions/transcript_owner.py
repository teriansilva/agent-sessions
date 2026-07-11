"""Transcript-ownership probe (#631) — is a LIVE process holding a session's transcript?

Claude Code writes a ``<uuid>.jsonl`` transcript not only for the sessions this app launches,
but also for its **background agents** (``claude daemon run`` → a ``bg-pty-host`` →
``claude --session-id <new> --fork-session --resume <parent>.jsonl`` fork). Those forks show up
in ``~/.claude/projects`` and so appear in the sidebar as ordinary sessions, but this app owns
neither their lifecycle nor their transcript: there is no ``dtach`` master. Archiving one would
``shutil.move`` its still-open JSONL out from under the running fork (the file then diverges
between the live + archive trees), and resuming one relaunch-loops (``claude --resume`` prints
"currently running as a background agent" and exits instantly).

This probe answers "does a live process own ``<uuid>.jsonl``" from ``/proc`` alone — no shell,
no subprocess (the repo's shell-free guarantee) — so archive/launch can refuse instead of
corrupting the file or looping. It is called ONLY where this app's own master is known to be
gone: archive runs it AFTER ``runtime_cleanup`` killed our master; launch runs it only on the
LAUNCH branch, where no master exists. A match is therefore necessarily a process we do NOT
manage (a background agent, or some other external claude) — never our own live session.

Fail-OPEN: any probe error returns ``False`` (allow) and logs. A false negative only restores
the prior behaviour; a false positive would wrongly block a legitimate archive/launch, which is
worse.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("agent_sessions.transcript_owner")


def _cmdline_owns(args: list[str], uuid: str, jsonl_name: str) -> bool:
    """True if a ``claude`` argv references this session's transcript: ``--session-id <uuid>``
    or ``--resume <…/<uuid>.jsonl>`` — the PATH form a background-agent fork uses to resume its
    parent, NOT a bare ``--resume <uuid>``, which is exactly how THIS app resumes (so the probe
    never self-matches an app-launched claude). Matches the uuid EXACTLY."""
    if not args:
        return False
    # Only a claude process counts (honour "a claude argv") — guards against a wild flag match
    # from some unrelated program that happens to carry a ``--resume``/``--session-id`` token.
    if not any(os.path.basename(a) == "claude" for a in args if a):
        return False
    for flag, value in zip(args, args[1:], strict=False):
        if flag == "--session-id" and value == uuid:
            return True
        if flag == "--resume" and value and os.path.basename(value) == jsonl_name:
            return True
    return False


def _fd_target_owns(target: str, jsonl_name: str) -> bool:
    """True if an open-fd symlink points at this session's ``<uuid>.jsonl`` — matched by exact
    basename (the uuid is globally unique, so the basename is an exact identity even across the
    live/archive trees). A file moved out from under a still-open fd reads back as
    ``<path> (deleted)``; strip that so a transcript archived from under a live fork still
    matches."""
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    return os.path.basename(target) == jsonl_name


def transcript_is_owned(uuid: str, *, proc_root: str = "/proc") -> bool:
    """Whether a live process owns the ``<uuid>.jsonl`` transcript — it holds the file open, or
    a ``claude`` argv references it. Pure ``/proc`` reads; no shell. Fail-open: returns ``False``
    on any probe error (and logs)."""
    jsonl_name = f"{uuid}.jsonl"
    try:
        pids = [n for n in os.listdir(proc_root) if n.isdigit()]
    except OSError:
        log.warning("transcript probe: cannot list %s — allowing (fail-open)", proc_root)
        return False
    for pid in pids:
        base = f"{proc_root}/{pid}"
        # (1) argv match — a single cheap read per process.
        try:
            with open(f"{base}/cmdline", "rb") as fh:
                args = [a for a in fh.read().decode("utf-8", "replace").split("\0") if a]
            if _cmdline_owns(args, uuid, jsonl_name):
                log.debug("transcript %s owned by pid %s (argv)", jsonl_name, pid)
                return True
        except OSError:
            pass  # process exited between listdir and open, or not ours — try its fds anyway
        # (2) open-fd match — scan this pid's descriptors for the transcript file.
        try:
            fd_dir = f"{base}/fd"
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue  # fd closed mid-scan
                if _fd_target_owns(target, jsonl_name):
                    log.debug("transcript %s owned by pid %s (open fd)", jsonl_name, pid)
                    return True
        except OSError:
            continue  # /proc/<pid>/fd gone or not readable (other user) — skip this process
    return False
