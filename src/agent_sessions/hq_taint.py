"""A conversation's live taint flag (#2398, epic #2070 B2 §6).

A1 (#2390) records a turn's taint on the turn and on its settled status, and a later turn of the
conversation reads it from there (`ask_orchestrator._history_tainted`). That is enough while one
turn at a time runs and every effect is admitted on the taint as of its claim. B2 adds effects
that land after the claim (a subagent's create and first turn, a steer) and tasks that share a
conversation, so the conversation's taint becomes a LIVE flag, read at every effect admission
(`hq_authority.ActGrant.admits`), never cached at task start.

**Set the moment untrusted text is read.** `mark` is synchronous and touches no disk: the turn's
read tool calls it in the same step it learns the result is untrusted, with no await between the
read and the flag. `persist` then writes the durable flag (an empty file, fsynced with its
directory) BEFORE the result is returned into the turn. So an effect admitted after a sibling's
read could complete sees the flag; one admitted before it is ordered entirely before that read.

**Never cleared.** A conversation that read untrusted text stays tainted, as A1's history rule
already says. The file only adds what a concurrent turn and a restart need to see before the
reading turn settles. Unreadable reads as tainted (fail closed).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import ask_store

log = logging.getLogger(__name__)

#: Conversations this process saw read untrusted text: the in-memory half, set with no I/O.
_LIVE: set[str] = set()


def _dir() -> Path:
    return ask_store.root() / "tainted"


def _path(cid: str) -> Path:
    return _dir() / cid


def mark(cid: str) -> None:
    """The conversation read untrusted text NOW. Synchronous, no I/O: call it in the same step as
    the read, before any await."""
    if ask_store.valid_cid(cid):
        _LIVE.add(cid)


def persist(cid: str) -> bool:
    """Write the durable flag (fsynced). Returns whether it is on disk; the in-memory flag stays
    set either way. Blocking: run it off the loop, before the read's result is returned."""
    if not ask_store.valid_cid(cid):
        return False
    try:
        d = _dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        p = _path(cid)
        if p.exists():
            return True
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    except OSError:
        log.warning("live taint flag not persisted", exc_info=True)
        return False


def tainted(cid: str) -> bool:
    """Whether conversation ``cid`` read untrusted text: this process's flag or the durable one.
    Cheap (a set lookup, else one stat); an unreadable flag reads as tainted."""
    if not cid:
        return False
    if cid in _LIVE:
        return True
    if not ask_store.valid_cid(cid):
        return True
    try:
        return _path(cid).exists()
    except OSError:
        return True


def reset_for_test() -> None:
    _LIVE.clear()
