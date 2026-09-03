"""Start a session with nobody watching, and know whether it actually started (#739).

The orchestrator can watch, decide, act and escalate. It could not **start** anything, and #732's
attempt was closed rather than merged because its launch could not have worked. This is the launch
path, plus the thing #732 had no notion of: **evidence that the agent is really there.**

## Three facts, kept apart

`launched` → `started` → `briefed`, and collapsing any two of them is how a dispatch reports
success for work that is not happening:

* **launched** — the `dtach -n` master is up. Says a *process* exists. Nothing more.
* **started** — the ENGINE'S OWN STORE has a record of the session. This is condition 5, and it is
  the one #840 recorded as evidence rather than speculation: on 2026-08-25 a `claude` launch in a
  fresh checkout produced a live master, a live process, a bound socket — and no transcript,
  indefinitely, because the workspace trust dialog was waiting. Every liveness signal said
  healthy. A fresh per-issue checkout is *by definition* a directory the engine has never seen, so
  that is the default case for dispatch, not an edge case.
* **briefed** — the injector acked a full delivery. A store record says the engine woke up; it does
  not say the agent accepted the brief.

A dispatch that reaches `started` and never reaches `briefed` is a **failure with a reason**, not a
success. That is the whole point: an unattended, permission-bypassed session reported as working
when it is stalled on a modal is a false report the operator's next decision is made on.

**The ORDER of the last two depends on who mints the id, and only the order does.** For a
pinned-id engine (claude, shell) the id is known before the launch, so the store record is
required BEFORE anything is typed — the trust screen above is armed, painted and quiet, so the
readiness gate alone cannot tell it from a running agent. For a mint-own-id engine (codex,
opencode, kimi, antigravity) that order is impossible: the engine writes its id only after its
first turn, so requiring evidence first means the id waits on the first turn, the first turn waits
on the brief and the brief waits on the id. There, reconciliation runs CONCURRENTLY with the
delivery and the store is asked once the id resolves. `ok` requires both facts either way, so the
order changes when each is established and never whether it is.

## Why the screen is not the diagnostic

The same incident: attaching a fresh client and forcing a redraw produced **no repaint**, because
claude holds the alt buffer. So "peek at the screen" is not a reliable check here. Every engine has
its own first-run, onboarding or re-auth screen, in its own config, with its own wording, and an
expired login parks the same way — there is no cross-engine signal for "blocked on a modal". The
one thing every engine *does* have is a store, which is why the evidence is written against that.

## What this module does not do

It does not re-implement the launcher (`ptybridge.launch_argv(detached=True)`), the seed injector
(`headless_seed`, itself a second entry point to `webterm._deliver_seed`) or the single-writer
lock. One of each.

## What it does not cover, and why that is stated rather than implied

**Only engines whose session id is known before the launch.** `claude` qualifies; `codex`,
`opencode`, `kimi` and `antigravity` mint their own id and do not reveal it until after their
first turn — so the store cannot be asked before the brief is typed, and briefing first is the
one thing this module refuses to do. Those engines are refused at the capability fence, before
anything is spawned, with a reason that says so. Establishing a noninteractive bootstrap proof
per engine is real work against the real engines and is its own issue; assuming one would put
the operator's brief in front of a consent screen with nobody watching.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from . import engines, handoff, ptybridge, scopedspawn, sessionlock

log = logging.getLogger(__name__)

#: How long to wait for the engine's own store to show the session. Generous, because a cold
#: engine start is slow and the alternative — declaring failure early — kills a session that was
#: about to work. Bounded, because "no evidence" must eventually BE the answer rather than a wait
#: that never ends.
START_EVIDENCE_TIMEOUT_S = 90.0
START_POLL_S = 1.0

SPAWN_TIMEOUT_S = 20.0


class DispatchError(RuntimeError):
    """A dispatch that could not be attempted. Distinct from one that was attempted and failed."""


@dataclass
class Dispatch:
    """What actually happened, in the three facts above.

    `state` is the honest summary the caller reports: `briefed` is the only success. Anything else
    carries a `reason` — never an empty failure, because "the dispatch failed" with no reason sends
    the operator to read logs for something the code already knew.
    """

    key: str
    engine: str
    native: str
    cwd: str
    launched: bool = False
    started: bool = False
    briefed: bool = False
    reason: str = ""
    events: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        """The furthest point reached. `briefed` now precedes `started` for a mint-own-ID engine,
        so this reports the *highest* fact rather than assuming an order."""
        if self.started and self.briefed:
            return "briefed"
        if self.briefed:
            return "unidentified"
        if self.started:
            return "started"
        if self.launched:
            return "launched"
        return "failed"

    @property
    def ok(self) -> bool:
        """**BOTH `briefed` and `started`.**

        `briefed` alone says bytes reached the agent; `started` says the engine's own store knows
        the session exists — condition 5, and the thing a live process does not prove. Asserted as
        a CONJUNCTION rather than as "the later one", because the order is engine-dependent: a
        mint-own-id engine writes its id only after its first turn, so there the brief necessarily
        comes first.
        """
        return self.briefed and self.started


def _has_store_record(prov, native: str, cwd: str) -> bool:
    """Does the ENGINE'S OWN store know about this session?

    Asked of the provider's scan rather than of a file path, so it is engine-agnostic by
    construction: claude writes a transcript JSONL, the SQLite engines write a row, and this asks
    each of them the question in their own terms. A provider that cannot answer returns nothing,
    and "no evidence" is then the honest result rather than a guess.
    """
    try:
        for row in prov.scan() or []:
            if str(row.get("id") or "").endswith(native) or str(row.get("native") or "") == native:
                return True
    except Exception:  # noqa: BLE001 — an unreadable store is "no evidence", never "yes"
        return False
    return False


async def _await_start_evidence(prov, native: str, cwd: str, *, timeout: float) -> tuple[bool, str]:
    """Condition 5. Returns ``(started, why_not)``.

    Polls the engine's store. Deliberately NOT `ptybridge.probe_master` or `session_input.is_live`:
    both were true throughout the 2026-08-25 incident, for a session that had received nothing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_has_store_record, prov, native, cwd):
            return True, ""
        await asyncio.sleep(START_POLL_S)
    return False, (
        f"the session never appeared in {prov.engine_id}'s own store within {int(timeout)}s — "
        "the process is up but the agent has not started (a first-run or trust prompt is the "
        "usual cause in a folder the engine has not seen before)"
    )


async def _abandon(key: str, engine: str, native: str, out: Dispatch) -> None:
    """Stop a launch that will never be briefed, and say so in the record.

    Reuses `runtime_cleanup.cleanup_runtime` — the same teardown archiving performs — rather than
    signalling a pid here: it terminates the master and the agent's process group, clears the
    scrollback and the owner lease, and unlinks the stale socket under the single-writer lock, so
    a later relaunch is a LAUNCH rather than a BUSY. One teardown, not a second one that drifts.
    """
    from . import runtime_cleanup

    try:
        outcome = await runtime_cleanup.cleanup_runtime(engine, native)
        # A NON-RAISING TEARDOWN IS NOT A STOPPED AGENT (#898 review 4, finding 1). The signal
        # path now answers about the whole process GROUP, and `leaked` means something in it
        # survived SIGKILL — a child that outlived its master. Reporting `abandoned` over that
        # tells the operator the unattended, possibly permission-bypassed agent is gone when it
        # is still running, which is the one thing this record exists to get right.
        if outcome == "leaked":
            log.error("headless dispatch %s: teardown left a live process group", key)
            out.events.append("abandon-leaked")
            out.reason = (
                f"{out.reason} (and a process from the launched session is STILL RUNNING — "
                "it ignored both SIGTERM and SIGKILL)"
            )
        else:
            out.events.append("abandoned")
    except Exception as e:  # noqa: BLE001
        # The dispatch already failed; a failed cleanup is additional bad news, not a replacement
        # for the reason. Appended so an operator can see both.
        log.warning("headless dispatch %s: cleanup failed (%s)", key, type(e).__name__)
        out.events.append(f"abandon-failed:{type(e).__name__}")
        out.reason = f"{out.reason} (and the launched session could not be stopped)"


async def dispatch(
    *,
    engine: str,
    cwd: str,
    brief: str,
    registry,
    bypass: bool = False,
    start_timeout: float | None = None,
) -> Dispatch:
    """Launch `engine` in `cwd` with nobody watching, and deliver `brief`.

    Never raises for an ordinary failure — every outcome is a `Dispatch` whose `state` and `reason`
    say what happened. `DispatchError` is reserved for a request that could not be attempted at
    all (an unknown or ineligible engine, or a missing capability), which is a caller bug rather
    than a launch result.

    **`bypass` DEFAULTS TO FALSE, and that is deliberate.** Every interactive path in this app
    launches with permission bypass, but *unattended* bypass is a different grant: it is an agent
    running with tool prompts suppressed and nobody watching what it does. #739's own security
    section says so — it must stay gated by the autonomy tier and the write fence, and stay
    approval-required until it has been exercised in anger. A default of `True` here would have
    made this function that grant, decided by the module that implements it. The caller opts in
    explicitly, at the layer that knows whether an operator authorised it.

    **`registry` is required**, not optional. A `dtach -n` master has no reader, so nothing drains
    it, nothing observes first paint and no writer is registered — and the seed delivery then
    waits for signals that can never arrive. The dispatcher starts the server-owned reader itself,
    the moment the master is accepting; without a registry there is no way to do that, and a
    launch that cannot be briefed should not happen at all.
    """
    prov = engines.get(engine)
    if prov is None:
        raise DispatchError(f"unknown engine {engine!r}")
    # THE SAME GATE THE HANDOFF PICKER USES, not a second copy. `shell` is refused here because a
    # brief seeded into a bare `bash -l` is EXECUTED, and an engine with no seed support cannot be
    # briefed at all — so dispatching to it would produce a session nobody asked for.
    supported, why = handoff.seed_start_state(prov, present=prov.is_present())
    if not supported:
        raise DispatchError(why or f"{engine} cannot be dispatched to")

    # ONE sanitiser, the handoff picker's. It strips control bytes (an `ESC` could otherwise end
    # the bracketed paste early and smuggle raw key input into the new session) and REFUSES
    # over-cap text rather than truncating it — a brief the author never wrote is worse than no
    # brief. Its refusal is re-raised as a `DispatchError` so this module keeps one error type for
    # "could not be attempted"; the message is the sanitiser's own, not a second wording.
    try:
        cleaned = handoff.sanitize_seed(brief)
    except handoff.HandoffError as e:
        raise DispatchError(str(getattr(e, "detail", None) or e)) from None

    # MINT-OWN-ID ENGINES ARE REFUSED, before the spawn (#898 review 4, finding 2).
    #
    # The safety property this whole module is about is condition 5: the ENGINE'S OWN STORE is
    # what tells a running agent apart from a first-run trust screen, because that screen is
    # armed, painted and quiet and satisfies every signal the readiness gate can see. For a
    # pinned-id engine the id is known before the launch, so the store can be asked BEFORE
    # anything is typed and the property holds.
    #
    # For an engine that mints its own id it cannot: `_reconcile_new_session`'s own contract says
    # the id may not appear "until the first message/output", so requiring evidence first is a
    # deadlock — the id waits on the first turn, the first turn waits on the brief, and the brief
    # waits on the id. Briefing first breaks the deadlock and breaks the property with it: the
    # operator's words, plus a submit, can land on a consent screen with nobody watching.
    #
    # There may well be an engine-specific noninteractive bootstrap proof for each of these — a
    # rollout file, a store row written at startup rather than at first turn — but it is a
    # different fact per engine and it has to be established against the real engine rather than
    # assumed. Until then the honest answer is the one #732 gave: refuse. This is a smaller
    # feature than the issue sketched and it says so, rather than shipping a path whose safety
    # rests on a modal not looking like an agent. Tracked as its own issue.
    if bool(getattr(prov, "new_session_reconciles", False)):
        raise DispatchError(
            f"{engine} does not reveal its session id until after its first turn, so nothing "
            "can tell a live agent from a first-run or consent screen before the brief is "
            "typed. Unattended dispatch is refused for this engine; start it from a terminal."
        )
    native = str(uuid.uuid4())
    key = f"{engine}:{native}"
    out = Dispatch(key=key, engine=engine, native=native, cwd=cwd)

    # THE SINGLE-WRITER LOCK, taken before anything is spawned. A held lock means somebody else
    # owns this session and we do nothing — the arbiter is the flock, never a check-then-act.
    lock = await asyncio.to_thread(sessionlock.acquire, key)
    if lock is None:
        out.reason = "another writer already holds that session"
        return out

    handed_off = False
    may_have_inherited = False
    try:
        # The brief goes into the ONE seed store, so delivery redeems it through the same
        # atomic claim/ack every other seed uses — exactly-once across a retry or a second
        # attempt, without this module knowing how that is done.
        # `commit()` MINTS the target id, and this path already has one — the lock is taken on it
        # and the socket is named after it. So the handle is bound to the id we hold rather than
        # to a second one, through `bind_target`, which is `commit`'s body minus the minting.
        handle = handoff.create_handle(
            source_key="", target_engine=engine, mode="dispatch", seed=cleaned, cwd=cwd
        )
        handoff.bind_target(handle, key)

        launch = prov.new_launch_argv(native, cwd=cwd, bypass=bypass)
        argv = ptybridge.launch_argv(
            engine=engine, session_id=native, launch_argv=launch, detached=True
        )
        # A TRANSIENT SCOPE, exactly as the interactive launch takes (#346 Phase B) — and here it
        # is a containment boundary rather than only a resource one (#898 review 6).
        #
        # A pid snapshot cannot see a process that did not exist when it was taken, so an agent
        # that answers SIGTERM by forking a child and exiting hands that child to init and walks
        # out of a tree-rooted teardown. Cgroup membership is inherited across fork and survives
        # reparenting, so the scope still contains it — which is what lets `terminate_master`
        # PROVE the boundary empty instead of inferring it from a stale list of pids.
        #
        # Falls through unwrapped when scopes are disabled or unavailable (logged inside `wrap`);
        # the teardown then reports the reduced boundary rather than claiming a clean one.
        argv, scope_unit = scopedspawn.wrap(argv, engine=engine, session_id=native)
        if scope_unit is not None:
            log.info("dispatching %s in scope %s", key, scope_unit)
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        # OWNERSHIP STARTS HERE, not at `transfer()` (#898 review 5, finding 2).
        #
        # The moment `create_subprocess_exec` is entered, the lock fd may already have been
        # inherited by a process we cannot see yet — and everything between here and the transfer
        # is awaited: the spawn, `proc.wait()`, the socket poll. A `CancelledError` in that window
        # took the "never handed off" branch and called `lock.release()`, which frees the flock
        # for the whole shared open file description — including the master's copy — while the
        # master keeps running. The next retry then starts a SECOND permission-bypassed agent
        # beside the first, with no single-writer fence between them.
        #
        # So the question the cleanup asks is not "did we finish handing over" but "could a
        # process have inherited this", and the answer becomes yes before the call, not after it.
        may_have_inherited = True
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    # NO terminal, which is the whole reason for `-n`. `-c` fails here with
                    # "Attaching to a session requires a terminal" and then times out looking
                    # like a slow start — #732's actual cause of death.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                    # The lock fd goes to the dtach master, so the flock lives exactly as long as
                    # the agent and survives an app restart.
                    pass_fds=(lock.fd,),
                ),
                timeout=SPAWN_TIMEOUT_S,
            )
        except (TimeoutError, OSError) as e:
            out.reason = f"the launch could not be spawned ({type(e).__name__})"
            return out
        await proc.wait()  # `dtach -n` forks its master and returns immediately
        if proc.returncode:
            out.reason = f"dtach exited {proc.returncode} without creating the session"
            return out

        sock = ptybridge.socket_path(engine, native)
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.05)
        if not sock.exists():
            out.reason = "the master never created its socket"
            return out
        out.launched = True
        out.events.append("launched")
        # The lock now belongs to the master. `transfer()`, never `release()` — `LOCK_UN` frees it
        # for the whole shared open file description, which would unlock the running agent's.
        lock.transfer()
        handed_off = True

        # THE READER, immediately, and before anything waits on output.
        #
        # A `dtach -n` master has nobody attached, so nothing drains it: no scrollback, no
        # first-paint observation, and no registered writer for the seed to borrow. The registry
        # keeps one server-owned `dtach -a` reader per live session and that is the mechanism —
        # but it only sweeps at startup, which is before this socket existed. Without this the
        # delivery below waits 45s for signals that can never arrive and every real dispatch
        # times out (#898 review, finding 1 — invisible to a test that mocks the delivery).
        try:
            await registry.ensure_headless(engine, native)
        except Exception as e:  # noqa: BLE001
            out.reason = f"the session's reader could not be started ({type(e).__name__})"
            return out

        from . import headless_seed

        evidence_timeout = START_EVIDENCE_TIMEOUT_S if start_timeout is None else start_timeout

        try:
            # STORE EVIDENCE FIRST, ALWAYS. Only pinned-id engines reach here (the rest are
            # refused above), so the id is known before the launch and there is nothing to wait
            # for — which is what makes this order possible at all. The store record is the one
            # signal a trust screen cannot fake: it is armed, painted and quiet, so it passes the
            # readiness gate perfectly and is invisible to everything else.
            started, why_not = await _await_start_evidence(
                prov, native, cwd, timeout=evidence_timeout
            )
            if not started:
                out.reason = why_not
                return out
            out.started = True
            out.events.append("started")

            # The readiness gate still protects the delivery — armed + painted + quiet — with the
            # store evidence above as a SECOND fence in front of it, not a replacement for it.
            delivered, why = await headless_seed.deliver(key, key)
            if not delivered:
                out.reason = why or "the brief was not delivered"
                return out
            out.briefed = True
            out.events.append("briefed")
            return out
        except Exception as e:  # noqa: BLE001
            out.reason = f"the dispatch failed ({type(e).__name__})"
            return out
    finally:
        # THREE STATES, not two (#898 review 5, finding 2).
        #
        # * nothing was ever spawned — release the lock, or the session is BUSY for ever with
        #   nothing running;
        # * something MAY have inherited the fd — tear the launch down FIRST and only then let
        #   the lock go, because releasing it while a master still holds a copy frees the flock
        #   for the whole shared open file description and the next retry starts a second
        #   permission-bypassed agent beside the first;
        # * handed off and successful — the master owns everything and this frame touches
        #   nothing.
        #
        # `briefed` and `started` together are the only success, so anything else is torn down:
        # the master and every process it launched, the reader, the ring, the owner lease and the
        # socket, under the single-writer lock that is the split-brain guard.
        #
        # SHIELDED, because this runs on the cancellation path too. An `await` inside a `finally`
        # that is itself unwinding a `CancelledError` is cancelled again at its first suspension
        # point, so an unshielded teardown here would be interrupted precisely when it matters —
        # leaving the agent running and the lock about to be released. Best-effort otherwise, and
        # never allowed to mask the real reason: the operator needs to know why the dispatch
        # failed, not that the cleanup also did.
        if not may_have_inherited:
            with contextlib.suppress(Exception):
                lock.release()
        elif not out.ok:
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.ensure_future(_abandon(key, engine, native, out)))
            if not handed_off:
                # The teardown has stopped whatever inherited it, so the lock is ours to drop —
                # and it must be dropped, or a launch that failed before the transfer leaves the
                # session BUSY with nothing running.
                with contextlib.suppress(Exception):
                    lock.release()
