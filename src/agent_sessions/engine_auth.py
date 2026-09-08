"""Can the agent we are about to launch actually AUTHENTICATE? (#916)

**"Did the process start" and "can it work" are different questions**, and answering the first
while claiming the second is the defect this module's sibling exists to fix, one layer up. The
session registry proves an agent got past its trust and onboarding screens. It proves nothing about
credentials: measured on 2.1.263, a session with **absent** credentials registers in 0.80s and one
with **expired** credentials registers in 1.35s and paints no authentication signal at all. Neither
is distinguishable from a working session by anything the launcher can see afterwards.

So authentication is checked BEFORE anything is spawned. It is knowable in advance, and a failure
deserves "this claude cannot log in" rather than a 90-second readiness timeout blamed on a screen
nobody looked at.

**It is NOT a host-global fact, and an earlier version of this note said it was.** The probe runs
under the LAUNCH's own `cwd` and environment, because credentials, `HOME` and identity can all
differ between two dispatches on one machine — a preflight run under different ones answers a
question about a different process. Caching this answer across dispatches would reintroduce
exactly that error.

## Three answers, and the third is not a flavour of the second

`AUTHENTICATED` / `UNAUTHENTICATED` / `UNKNOWN`. **Only `UNAUTHENTICATED` may tell an operator their
login is broken** — saying that because a probe timed out is its own defect, and sends somebody to
re-authenticate a session that was fine. **`UNKNOWN` refuses the launch too**: an unattended
agent is not something to start on "we could not tell".

## Why the free probe is not enough on its own

`claude -p "/usage"` costs nothing and reports the plan when authenticated. Logged out it does NOT
error — it prints a **well-formed zero report** (`Total cost: $0.0000 …`) and exits 0. A probe that
asked "did it run" would pass a host that cannot authenticate. So only a **recognised positive**
counts, and an inconclusive free probe escalates once to a minimal real prompt, which does say
`Not logged in · Please run /login`. Zero cost in the normal case; a few tokens only when the answer
actually matters.

**The exit code is never consulted.** Both states exit 0 — the same family as `git tag -s` returning
0 for an unsigned tag: success for having run, not for having worked.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading

from . import procgroup

log = logging.getLogger("agent_sessions.engine_auth")


class Probe:
    """One dispatch's probe lifetime. **Per operation, never process-global** (review 2, finding 4).

    Ownership is a single record per process — the handle AND the process-group id captured at
    spawn — because keeping them in two lists let them diverge: `_forget` removed the finished
    usage probe from one and not the other, so `abandon` during the SECOND probe zipped the
    confirmation handle against the FIRST one's group and killed the wrong thing, leaving the
    confirmation's descendant running (review 3, finding 3).

    `spawning()` serializes creation with abandonment (review 3, finding 2). Checking the flag,
    then calling `Popen`, then registering leaves a window where cancellation snapshots an empty
    set and the handle created a moment later is never reclaimed. So the check, the spawn and the
    registration all happen under the SAME lock `abandon()` takes: there is no moment at which a
    live probe process exists and is not owned. `abandon()` blocks across one `Popen`, which is
    the point — it either refuses the spawn or inherits it, never misses it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owned: list[tuple] = []  # (proc, pgid) — one record, never two lists
        self.abandoned = False

    @contextlib.contextmanager
    def spawning(self):
        """Hold the ownership lock across create-and-register. Yields a `register` callable."""
        with self._lock:
            if self.abandoned:
                yield None
                return
            made: list[tuple] = []

            def register(proc, pgid):
                made.append((proc, pgid))

            try:
                yield register
            finally:
                self._owned.extend(made)

    def forget(self, proc) -> None:
        with self._lock:
            self._owned = [r for r in self._owned if r[0] is not proc]

    def abandon(self) -> int:
        """Stop this operation's probes and refuse any it has not started yet."""
        with self._lock:
            self.abandoned = True
            owned = list(self._owned)
            self._owned.clear()
        for proc, pgid in owned:
            _kill_group(proc, pgid)
        return len(owned)


class Refused(Exception):  # noqa: N818 — a refusal, not an error condition
    """The gate would not let a probe spawn: policy withdrawn, or the fence unavailable.

    Distinct from `UNAUTHENTICATED` and from `UNKNOWN`, because it is not an answer about the
    agent at all. Telling an operator their login is broken because orchestration was switched
    off mid-probe would be the same category error this module's three states exist to prevent.
    """


AUTHENTICATED = "authenticated"
UNAUTHENTICATED = "unauthenticated"
UNKNOWN = "unknown"

#: Seconds for each probe. A preflight that outlives the readiness gate it protects is a worse
#: problem than the one it solves.
PROBE_TIMEOUT_S = float(os.environ.get("AGENT_SESSIONS_AUTH_PROBE_TIMEOUT", "45") or 45)

#: A recognised POSITIVE from the free probe. Deliberately several independent phrasings: matching
#: one brittle sentence would turn a wording change into a fleet-wide refusal. Asserted against the
#: real binary by `tests/test_engine_auth.py` so a version bump breaks CI rather than dispatch.
_PLAN_MARKERS = (
    "using your subscription",
    "current session:",
    "current week",
    "resets ",
)

#: An explicit refusal. This is the ONLY thing allowed to produce `UNAUTHENTICATED`.
_REFUSAL_MARKERS = (
    "not logged in",
    "/login",
    "please run /login",
    "invalid api key",
    "authentication_error",
)


def _run(
    argv: list[str],
    cwd: str | None,
    env: dict[str, str],
    probe: Probe,
    gate=None,
) -> tuple[bool, str]:
    """One bounded, noninteractive, CONTAINED probe. `(completed, combined_output)`.

    Literal argv, never a command string — the shell-free guarantee in `CLAUDE.md` covers this
    module for the same reason it covers the launchers. `stdin` is closed so a probe can never sit
    waiting for input.

    **The process group is captured AT SPAWN, not at teardown** (review 2, finding 3). The previous
    version called `os.getpgid(proc.pid)` while cleaning up — and if the leader had already exited
    while a child still held the pipes (which is exactly how `communicate()` reaches its timeout in
    that case) `getpgid` raises, the suppressed exception skipped the group kill, and killing the
    dead leader did nothing. The descendant survived. `start_new_session=True` makes the child its
    own group leader, so the pgid IS the pid and is knowable before anything can exit.

    **`gate` wraps the SPAWN, not the call** (review 3, finding 1). This probe starts a real agent
    process, so it belongs inside the same policy transaction as any other launch — but the
    transaction must not be held across the wait, which is bounded only by `PROBE_TIMEOUT_S`. So
    the caller hands in a context manager that takes its fence, re-authorizes, and yields a
    refusal reason (falsy to proceed); it is entered around create-and-register **only**, and
    exited before `communicate()`. A policy withdrawn while a probe is running still cannot reach
    the launch, because the launch re-enters the same fence for itself.

    The gate is entered OUTSIDE `probe.spawning()`: waiting for a global fence while holding the
    probe's own lock would make `abandon()` — the cancellation path — block for the fence timeout.
    """
    if probe.abandoned:
        return False, ""
    with gate() if gate is not None else contextlib.nullcontext("") as why_not:
        if why_not:
            raise Refused(why_not)
        with probe.spawning() as register:
            if register is None:
                return False, ""
            try:
                proc = subprocess.Popen(  # noqa: S603 — literal argv, no shell
                    argv,
                    cwd=cwd or None,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    # ITS OWN GROUP, so the pgid below is the pid and the kill takes the tree.
                    start_new_session=True,
                )
            except Exception as e:  # noqa: BLE001 — a missing binary is "cannot tell"
                log.debug("auth probe could not run: %s", type(e).__name__)
                return False, ""
            # true because of start_new_session; recorded before anything can exit
            pgid = proc.pid
            register(proc, pgid)
    try:
        out, err = proc.communicate(timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, ""
    except Exception:  # noqa: BLE001
        return False, ""
    finally:
        # THE BOUNDARY IS SETTLED ON EVERY PATH, INCLUDING THE ORDINARY ONE (review 4, finding 1).
        #
        # The kill used to live only in the exception branches, and `forget` ran unconditionally —
        # so a normal completion dropped the ownership record without ever closing the group. That
        # is not a theoretical gap: `communicate()` returns when the PIPES reach EOF, which a probe
        # can produce while leaving work behind. Start a helper in the same group with its standard
        # streams redirected, exit the leader, and the pipes close while the helper runs on. `_run`
        # reported success, the record was forgotten, and a later `abandon()` — after a refusal, or
        # after the whole dispatch was cancelled — found nothing to stop.
        #
        # "The leader exited and its pipes closed" is not "all of this probe's work has ended".
        # Only the group says that, so the group is taken on the way out regardless of how we got
        # here, and the identity is forgotten only after. On the ordinary path the leader is
        # already gone and this costs one failed `killpg`.
        _kill_group(proc, pgid)
        probe.forget(proc)
    # The EXIT CODE IS NOT CONSULTED. Both states exit 0; only the text differs.
    return True, f"{out}\n{err}"


def _kill_group(proc, pgid: int | None = None) -> None:
    """Take the probe's whole process group, then reap it. Best effort, never raises.

    `pgid` is passed in rather than looked up: by the time this runs the leader may be gone, and
    deriving the group from a dead pid is what let a descendant survive.
    """
    if pgid is None:
        with contextlib.suppress(Exception):
            pgid = os.getpgid(proc.pid)
    # THROUGH THE SHARED GUARD (#924). This module invented the bound after a fake `Popen`
    # carrying `pid = 1` turned this line into `kill(-1)` — every process this user owns — seven
    # times in one day. It kept a private copy of the rule; `procgroup` is now the one place it
    # lives, so the four call sites cannot drift apart. The rationale is there in full.
    procgroup.killpg(pgid)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


def _classify(text: str) -> str | None:
    low = text.lower()
    # **REFUSALS ARE READ FIRST**, and the order is the safety property (review 3, non-blocking).
    # The plan markers are broad by necessity — `resets `, `current week` — and a logged-out
    # `/usage` prints a WELL-FORMED ZERO REPORT, which is the same document with zeros in it. So
    # a refusal and a plan marker can appear in one output, and whichever is tested first decides.
    # Testing the positive first hands `AUTHENTICATED` to a host that said `Please run /login`,
    # which is the one direction of this call that fails OPEN: it starts an unattended agent that
    # cannot act. An explicit refusal is never ambiguous; a plan marker is.
    if any(m in low for m in _REFUSAL_MARKERS):
        return UNAUTHENTICATED
    if any(m in low for m in _PLAN_MARKERS):
        return AUTHENTICATED
    return None


def check(
    binary: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    probe: Probe | None = None,
    gate=None,
) -> tuple[str, str]:
    """``(state, detail)`` for the agent binary this dispatch is about to launch.

    `env` and `cwd` are the LAUNCH'S OWN, not the host's: a preflight run under different
    credentials, a different HOME or a different identity answers a question about a different
    process. The caller passes what it is going to spawn with.

    `detail` never carries probe output verbatim — only which branch was taken — so a diagnostic
    cannot leak a token that appeared in an error message.

    `gate` is entered around **each** probe's spawn, not once around this call (review 3, finding
    1): the escalation is a second real agent process, started minutes after the first was
    authorized, and authorizing it by inheritance would be exactly the stale-policy read the fence
    exists to stop. A refusal raises `Refused` rather than returning a state — the caller aborts
    the dispatch, it does not report a verdict about the agent.
    """
    use_env = dict(env if env is not None else os.environ)

    # 1. THE FREE PROBE. A recognised positive is the only thing that ends this early.
    probe = probe if probe is not None else Probe()
    ok, out = _run([binary, "-p", "/usage"], cwd, use_env, probe, gate)
    if not ok:
        return UNKNOWN, "the usage probe did not complete within its timeout"
    verdict = _classify(out)
    if verdict == AUTHENTICATED:
        return AUTHENTICATED, "the agent reported its plan"
    # A logged-out `/usage` prints a well-formed ZERO REPORT and exits 0 — no error, nothing to
    # match. That is why an unrecognised answer escalates instead of concluding anything.
    if verdict == UNAUTHENTICATED:
        return UNAUTHENTICATED, "the agent reported it is not logged in"

    # 2. THE MINIMAL REAL PROMPT, only when the free one could not tell. This is the case that
    #    matters, and it is the one measured to produce an explicit refusal.
    # **ABANDONMENT IS RE-CHECKED HERE, not only at the top** (review 2, finding 2). Killing the
    # first process does not prevent this one: `communicate()` returns NORMALLY once its child
    # dies, with output matching no marker — which is exactly the inconclusive path that escalates.
    # Without this, the confirmation prompt launched after `dispatch()` had returned cancelled.
    if probe.abandoned:
        return UNKNOWN, "the dispatch was abandoned before authentication could be confirmed"
    ok, out = _run([binary, "-p", "Reply with the single word OK."], cwd, use_env, probe, gate)
    if not ok:
        return UNKNOWN, "the confirmation probe did not complete within its timeout"
    verdict = _classify(out)
    if verdict is not None:
        return verdict, (
            "the agent answered"
            if verdict == AUTHENTICATED
            else "the agent reported it is not logged in"
        )
    if re.search(r"\bok\b", out, re.IGNORECASE):
        return AUTHENTICATED, "the agent answered a live prompt"
    return UNKNOWN, "the agent's answer could not be recognised either way"


def may_dispatch(state: str) -> bool:
    """Only a confirmed positive opens the gate. `UNKNOWN` refuses, deliberately."""
    return state == AUTHENTICATED
