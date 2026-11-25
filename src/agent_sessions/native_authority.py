"""The caller's execution authority, carried across the process boundary to a native worker (#1897).

A native API session's turn is claimed by its WORKER process (`native_journal.claim`), while
the authority it is sent under (a mission's hold on the session, the mission policy) lives in
the app's in-process write fence (`session_input`), which may never be held across an `await`.
So the app cannot check authority in the same critical section as the claim. This module is
the bridge: a durable **authority token** per session, plus one for the policy, that

* the app reads when it admits a guarded turn (`admit`, called inside the write fence by
  `session_input.admit_native`, together with the caller's own domain check);
* the app withdraws (`bump_session` from every `session_transaction` / `sessions_transaction`,
  `bump_policy` from every `policy_transaction`) inside the same fence, BEFORE the withdrawal's
  own store write commits;
* the worker compares, under this module's flock, in the same critical section as the claim
  (`held`) and again at a queued message's delayed handoff.

A withdrawal therefore either commits before the claim (the turn is refused, nothing is sent)
or after it (the turn was admitted first). Tokens are random, never counters: a record that is
lost and recreated can never match a token handed out before.

**Fail closed.** A missing, unreadable or malformed record refuses a guarded turn. A withdrawal
removes the record; if it can't (a busy lock, an I/O error) it raises and the caller's withdrawal
is aborted, never committed unmirrored. Unguarded turns never read any of this.

Layout, under the native state root: ``authority/<session uuid>.json`` and
``authority/policy.json``, each ``{"token": <uuid4>}``; one lock, ``locks/authority.lock``.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import authfence, native_state

log = logging.getLogger(__name__)

#: Bounded: an app-side withdrawal runs inside the global write fence, so it must never wait long.
#: A worker holds the lock only to compare and claim (or up to one queued message's durable
#: `uncertain` record). The same budget as the cross-process fence's contention, so "busy means
#: something is wrong" is one number. Worst case held across a session write: the authfence's
#: own acquisition (`session_input.MUTATION_FENCE_BUDGET_S`) plus this.
LOCK_WAIT = authfence.CONTENTION_BUDGET_S
_POLICY = "policy"


class AuthorityError(RuntimeError):
    """A guarded turn whose authority is not proved current. Nothing may be claimed or sent."""


def _dir() -> Path:
    return native_state.root() / "authority"


def _path(name: str) -> Path:
    if name != _POLICY:
        name = native_state._uuid(name, "session id")
    return _dir() / f"{name}.json"


@contextlib.contextmanager
def _locked(wait: float = LOCK_WAIT) -> Iterator[None]:
    directory = native_state.root() / "locks"
    native_state.ensure_dir(directory)
    fd = os.open(
        directory / "authority.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_nlink != 1
            or st.st_mode & 0o077
        ):
            raise native_state.StateError("native authority lock is not private")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise native_state.LockBusy("the native authority lock is busy") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _read(name: str) -> str | None:
    """The record's token, or None when there is none. Raises when it can't be trusted."""
    doc = native_state.read(_path(name))
    if doc is None:
        return None
    token = doc.get("token") if isinstance(doc, dict) and set(doc) == {"token"} else None
    try:
        return str(uuid.UUID(token)) if isinstance(token, str) else _malformed()
    except ValueError:
        return _malformed()


def _malformed():
    raise native_state.StateError("native authority record is malformed")


def _write(name: str) -> str:
    token = str(uuid.uuid4())
    native_state.write(_path(name), {"token": token})
    return token


def admit(session_id: str) -> dict:
    """The tokens a guarded turn is admitted under: ``{"session": …, "policy": …}``.

    Call ONLY inside `session_input`'s write fence (see `session_input.admit_native`), after the
    caller's domain check passed: that is what orders it against `bump_session`/`bump_policy`.
    A record that does not exist yet is created here, so the first guarded turn has one.
    """
    with _locked():
        session = _current_or_new(session_id)
        policy = _current_or_new(_POLICY)
    return {"session": session, "policy": policy}


def _current_or_new(name: str) -> str:
    """The record's token; a missing or malformed record gets a fresh one. Replacing a malformed
    record withdraws nothing a worker could still match (it already refuses it), and keeps one
    damaged file from refusing every later admission forever."""
    try:
        token = _read(name)
    except native_state.StateError:
        log.warning("native authority record %s was unreadable; issuing a new token", name)
        token = None
    return token or _write(name)


def _bump(name: str) -> None:
    """Withdraw every token issued for ``name`` by removing its record (the next admission
    creates a new one, with a token never issued before).

    Called BEFORE the withdrawal's own store write commits (`session_input`), so a worker that
    compares after this refuses, and one comparing before it was admitted before the withdrawal.
    Taking the lock waits out a worker that is between its comparison and its claim. Any failure
    raises `StateError`: the caller then aborts its withdrawal, never commits it unmirrored.
    """
    try:
        path = _path(name)
    except native_state.StateError:
        # No state root: no record can exist, so no guarded turn could ever have been admitted.
        return
    try:
        if not path.exists():
            # Cheap for the many sessions never guarded. A record cannot appear meanwhile:
            # admission runs under the same write fence the caller holds.
            return
        with _locked():
            path.unlink(missing_ok=True)
    except native_state.StateError:
        raise
    except (OSError, ValueError) as exc:  # `storage.StateError` is a ValueError
        raise native_state.StateError("native authority could not be withdrawn") from exc


def bump_session(session_id: str) -> None:
    """Withdraw every token issued for ``session_id``. A session never guarded has no record."""
    _bump(session_id)


def bump_policy() -> None:
    """Withdraw every token issued under the current policy (one record for every scope)."""
    _bump(_POLICY)


def _matches(session_id: str, tokens: dict) -> bool:
    try:
        return (
            _read(session_id) == tokens.get("session")
            and _read(_POLICY) == tokens.get("policy")
            and tokens.get("session") is not None
            and tokens.get("policy") is not None
        )
    except native_state.StateError:
        return False


@contextlib.contextmanager
def held(session_id: str, tokens: dict, *, wait: float = LOCK_WAIT) -> Iterator[None]:
    """The worker's side: hold the authority lock while ``tokens`` are current, else raise.

    The caller claims (or hands off) INSIDE this block, so no withdrawal can commit between the
    comparison and the effect.
    """
    with _locked(wait):
        if not _matches(session_id, tokens):
            raise AuthorityError("the authority this turn was admitted under was withdrawn")
        yield


def current(session_id: str, tokens: dict) -> bool:
    """A read-only observation, for tests and diagnostics; never a substitute for `held`."""
    with _locked():
        return _matches(session_id, tokens)
