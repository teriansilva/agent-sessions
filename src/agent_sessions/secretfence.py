"""The secret-inventory fence (#1501): orders project memory's views against secret writes.

Project memory (#1441) promises that no stored secret value reaches an agent-readable view: a view
is judged against the stored-secret inventory, and a secret write withdraws the value from every
view before it returns. Both halves need ONE ordering — the inventory a render read cannot change
before that render's files are on disk — and nothing else.

Memory used to borrow the authorization fence (`authfence`, via `session_input.mutation_fence`)
for this, because the secret writer already held it. That fence also orders every terminal input
write, every launch and every policy and template edit, so a 37 s memory import held all of them
(#1501). This is the narrower fence memory needs instead: the same `flock` mechanics as
`authfence`, on its own file beside it.

**Who takes it.** Memory, around every render, the page and export builds' inventory read, and an
import; and the one secret writer (`template_vars`), through `memory.secrets_changing()`, across
its store write and the re-check of every view. Nothing else.

**Lock order**, outermost first, one-way by construction::

    secret write:  authfence → template_secrets.registration_fence → variables write lock
                   → THIS → memory scope locks
    import:        projects.locked_index → THIS → memory scope lock
    memory's own:  THIS → memory scope lock

Nothing holding this fence (or a memory scope lock) ever asks for `authfence`, the registration
fence, the variables write lock or `projects.locked_index`, so no order above can invert.

**Bounded, and it fails closed.** Acquisition has a budget; expiry raises `SecretFenceBusy`, which
the memory routes answer as 503 "try again" and a secret write reports as a refusal — never a
render or a write outside the fence. **Crash release is the kernel's**: `flock` is released when
the fd closes, on process death by any signal.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path

from . import sessionlock

#: How long a caller waits before giving up. A memory import can legitimately hold the fence for
#: tens of seconds on a large store, so the budget matches the mutation budget the secret writer
#: had under the authorization fence rather than the terminal writer's 2 s budget.
CONTENTION_BUDGET_S = 10.0

_RETRY_S = 0.002

_NAME = "secrets.lock"


class SecretFenceBusy(RuntimeError):
    """The fence could not be acquired inside the contention budget."""


#: How long a SECRET WRITE waits for this fence. The writer is already inside the authorization
#: fence, so this bounds how long a memory import can hold terminal input up through it (#1501):
#: a quarter of the terminal writer's 2 s budget (`authfence.CONTENTION_BUDGET_S`), leaving
#: headroom for the edit's own re-check and scheduler jitter.
SECRET_WRITE_BUDGET_S = 0.5
#: The refusal a busy secret write answers with (a message, not a secret).
SECRET_WRITE_BUSY = "shared memory is busy (an import is running) — nothing was saved; try again"  # noqa: S105


class SecretWriteBusy(RuntimeError):
    """A secret write could not get this fence inside `SECRET_WRITE_BUDGET_S`. Nothing was
    written; the store routes answer 503 with `SECRET_WRITE_BUSY` and the operator retries."""


def fence_path() -> Path:
    """Beside the authorization fence, in the lock directory every instance shares."""
    return sessionlock.lock_dir() / _NAME


@contextlib.contextmanager
def hold(*, timeout: float = CONTENTION_BUDGET_S):
    """Hold the cross-process secret-inventory fence, or raise `SecretFenceBusy`.

    A fresh fd per acquisition, as in `authfence`: `flock` is per open-file-description, so two
    threads of this process on separate fds are ordered by the kernel exactly like two processes.
    Re-entry on one thread is the caller's to avoid (memory tracks it per thread).
    """
    path = fence_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise SecretFenceBusy(
                        f"the secret-inventory fence was held elsewhere for more than {timeout:g}s"
                    ) from None
                time.sleep(_RETRY_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "CONTENTION_BUDGET_S",
    "SECRET_WRITE_BUDGET_S",
    "SECRET_WRITE_BUSY",
    "SecretFenceBusy",
    "SecretWriteBusy",
    "fence_path",
    "hold",
]
