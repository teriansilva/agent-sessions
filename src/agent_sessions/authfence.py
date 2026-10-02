"""The cross-process authorization fence (#887).

`session_input`'s write fence closes the window between "authority verified" and "byte one"
*within one process*: the mutation paths take `_lock`, the writer holds it across the compare and
the first chunk, so the two are ordered. That is the whole of what a `threading.Lock` can do.

This app supports **several instances over one store**. A sibling instance never takes this
interpreter's lock, so it can commit an ownership or objective withdrawal in exactly that window
and the compare — which happened microseconds earlier — still reads equal. A shared *read* cannot
close it either: reading and comparing is not mutual exclusion, so the sibling can always land
between the read and the write, however late the read happens. Only a fence both sides take can
order them, and the only thing two instances share is the filesystem.

So: one `flock` file, in the lock directory the session locks already use, taken by

  * every mutation that withdraws a session's authorization (`session_input.sessions_transaction`,
    which the mission routes commit inside), and
  * the writer, around the shared-store re-verification **and the first chunk only**.

**Authorization commits at byte one.** Once the first chunk is written the delivery is irrevocable;
later chunks complete an already-started write and are not re-authorized. That is a deliberate
limit, not an oversight — a half-delivered instruction is worse for the operator than a whole one,
and a PTY offers no way to un-write. The guarantee is "no byte reaches the PTY under a withdrawn
authorization", never "every `os.write` stays authorized".

**Why this is affordable.** The fence is taken only once the fd is already known writable, so what
it covers is a re-read plus one non-blocking `os.write` of at most `WRITE_CHUNK` bytes — not the
payload, and not a wait on a full PTY. A slow or blocked terminal therefore does not hold it: the
writer releases, waits outside the fence, and re-takes it when the fd is ready again.

**Bounded, and it fails closed.** Acquisition has a contention budget; expiry raises `FenceBusy`,
which the writer turns into a refusal the caller settles and the orchestrator re-proposes. An
unbounded wait would convert a stuck sibling into a hung delivery.

**Crash release is the kernel's, not ours.** `flock` is released when the fd closes, which happens
on process death by any signal — so a crash while holding the fence cannot wedge the fleet. That
is asserted by a test rather than assumed.

**Lock order**, one-way by construction and stated once, here:

    session_input._lock  →  authfence (this file)  →  session_input._screen_lock

Both sides take them in that order: the writer holds `_lock`, takes this, then `_screen_lock` for
the byte; a mutation holds `_lock` and takes this. Nothing takes them in the other direction, and
nothing holding this fence ever asks for `_lock`.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
from pathlib import Path

from . import sessionlock

#: How long a caller will wait for the fence before giving up. Bounded on purpose: a sibling that
#: is stuck must not turn every delivery here into a hang. Generous relative to what the fence
#: covers (a store read plus one non-blocking chunk write, i.e. sub-millisecond in the ordinary
#: case), so expiry means something is genuinely wrong rather than merely busy.
CONTENTION_BUDGET_S = 2.0

#: Poll interval while waiting. `flock` has no timed acquire, so a bounded wait is a non-blocking
#: attempt in a loop; small enough that ordinary contention costs microseconds, large enough not to
#: spin a core.
_RETRY_S = 0.002

_NAME = "authorization.lock"


class FenceBusy(RuntimeError):
    """The fence could not be acquired inside the contention budget."""


def fence_path() -> Path:
    """The one fence file, beside the per-session locks.

    Reusing `sessionlock.lock_dir()` rather than inventing a second namespace: operators already
    point `AGENT_SESSIONS_LOCK_DIR` at a directory shared by every instance, and a fence in a
    different place would silently not be shared by the instances it is meant to order.
    """
    return sessionlock.lock_dir() / _NAME


@contextlib.contextmanager
def hold(*, timeout: float = CONTENTION_BUDGET_S):
    """Hold the cross-process authorization fence, or raise `FenceBusy`.

    A fresh fd per acquisition, deliberately: `flock` is per open-file-description, so two threads
    sharing one fd would both "succeed" and the fence would not order them. In-process threads are
    already serialized by `_lock` above this, and a new fd keeps the cross-process semantics exact
    rather than relying on that.
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
                    raise FenceBusy(
                        f"the authorization fence was held elsewhere for more than {timeout:g}s"
                    ) from None
                time.sleep(_RETRY_S)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = ["CONTENTION_BUDGET_S", "FenceBusy", "fence_path", "hold"]
