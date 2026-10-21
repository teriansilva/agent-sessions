"""Signalling a process GROUP, with the one bound that makes it survivable (#924).

**`os.killpg(1, sig)` is `kill(-1, sig)`: every process the invoking user owns.** The supervisor,
every `dtach`'d agent, the user manager, its ssh sessions. There is no confirmation, nothing is
logged, and the host does not come back on its own.

That is not hypothetical here. A fake `Popen` in a test carried `pid = 1` — because nothing had
made it carry anything else — reached a cleanup path, and did exactly this **seven times in one
morning**. It was identified only because the runner unit's `NRestarts` counter matched the
count, and because CI logs truncated mid-run with no failure line. `last -x reboot` showed no
reboot: it was never a reboot, only every user process dying at once.

**The bound lives here, in code, and not in the fakes.** Both, in fact — the fakes now use
impossible pids — but a convention in test code is not a control: the next stub is written by
someone who has not read this, `Mock().pid` is not an int at all, and the blast radius is the
whole machine. Defence in depth is warranted precisely because the failure is unrecoverable and
silent.

Nothing legitimate is lost. Every group this app signals belongs to a process it spawned with
`start_new_session=True`, so the group id IS that child's pid — never 0, never 1.
"""

from __future__ import annotations

import contextlib
import os
import signal

#: The smallest id that can name a group we created. 0 means "the caller's own group" and 1 is
#: init's, which `killpg` widens to *everything*. Neither can ever be a probe we launched.
MIN_GROUP = 2


def killpg(pgid: object, sig: int = signal.SIGKILL) -> bool:
    """Signal a process group. `True` if the signal was delivered, `False` if it was refused.

    Refuses anything that is not a plain `int` at or above `MIN_GROUP` — which covers `None`, a
    `Mock`, a string, and the two catastrophic ids. `bool` is excluded explicitly because
    `isinstance(True, int)` is `True` in Python, the same trap the prefs validator documents.

    Never raises: a group that has already exited is the normal case, not an error.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid < MIN_GROUP:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)
        return True
    return False
