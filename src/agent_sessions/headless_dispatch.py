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

**Who mints the id changes how the facts are established, never whether they are.** For a
pinned-id engine (claude, shell) the id is known before the launch, so the store record is
required BEFORE anything is typed — the trust screen above is armed, painted and quiet, so the
readiness gate alone cannot tell it from a running agent. An engine that mints its own id (codex,
opencode, kimi, antigravity) writes that id only after its first turn, so its store cannot be asked
for it before the brief. There the question is split (#989): start evidence is asked of the
LAUNCH — the provider's own check, still before any byte is typed — and a fourth fact, **bound**
(which session the launch became), is established after the brief on a proof that the session
found received this attempt's paste. `ok` requires every fact the engine has.

## Why the screen is not the diagnostic

The same incident: attaching a fresh client and forcing a redraw produced **no repaint**, because
claude holds the alt buffer. So "peek at the screen" is not a reliable check here. Every engine has
its own first-run, onboarding or re-auth screen, in its own config, with its own wording, and an
expired login parks the same way — there is no cross-engine signal for "blocked on a modal". The
one thing every engine *does* have is a store, which is why the evidence is written against that.

## OpenCode maintenance admission (#1040)

An OpenCode dispatch takes shared non-inherited admission before auth probes or process creation,
retains it through actual child handoff, and drains abandoned workers before releasing it. A
compaction holding exclusive admission produces a retryable `maintenance` refusal. Executor
Futures are retained directly so cancellation of Tasks during shutdown cannot conceal a live thread.

## What this module does not do

It does not re-implement the launcher (`ptybridge.launch_argv(detached=True)`), the seed injector
(`headless_seed`, itself a second entry point to `webterm._deliver_seed`) or the single-writer
lock. One of each.

## What it does not cover, and why that is stated rather than implied

**A late-id engine that has not declared how to answer those questions.** The preflight, the start
evidence and the binding proof are facts about each engine, measured against the real CLI rather
than assumed, and they live on its provider (`engines/base.py`). An engine that has not declared
every one of them is refused at the capability fence (`engines.unattended_start_state`), before
anything is spawned, with a reason naming what is missing. As of #989 that is every late-id
engine: the contract exists and none has passed its measurement yet. Assuming an answer would put
the operator's brief in front of a consent screen with nobody watching.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from . import (
    engine_auth,
    engines,
    handoff,
    launch_binding,
    opencode_admission,
    ptybridge,
    scopedspawn,
    session_input,
    sessionlock,
    start_evidence,
)
from .engines import base as engine_base

log = logging.getLogger(__name__)

#: How long to wait for the engine's own store to show the session. Generous, because a cold
#: engine start is slow and the alternative — declaring failure early — kills a session that was
#: about to work. Bounded, because "no evidence" must eventually BE the answer rather than a wait
#: that never ends.
START_EVIDENCE_TIMEOUT_S = 90.0
START_POLL_S = 1.0

#: How long a late-id launch waits, from the delivery's acknowledgement, for its real session id to
#: be bound with a proof (#989). The same budget as start evidence and for the same reason: a cold
#: engine writes its first turn slowly, and "never bound" must eventually be the answer.
BIND_TIMEOUT_S = START_EVIDENCE_TIMEOUT_S

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
    refusal: str = ""
    events: list[str] = field(default_factory=list)
    #: WHAT WAS TYPED (#966), from the seed store's claim/ack record once the launch is over:
    #: `not_attempted`, `zero_write`, `partial`, `delivered` or `unknown` (see
    #: `handoff.retire_seed`). `unknown` until something establishes otherwise.
    seed_outcome: str = "unknown"
    #: The teardown's own word for a failed launch: `stopped` (the boundary was proved empty),
    #: `spared`, `leaked` or `error`. Empty when no teardown ran.
    teardown: str = ""
    #: THE THIRD FACT, which session this launch became (#989). `None` for a pinned-id engine: its
    #: id is known before the launch, so `key` is the session and there is nothing to bind.
    #: `False` until a late-id launch binds its real id with a proof, then `True`.
    bound: bool | None = None
    #: The engine-qualified REAL key a late-id launch bound, e.g. `kimi:session_<uuid>`. `key`
    #: stays the physical placeholder the master, lock and ring live under.
    bound_key: str = ""
    #: Which proof bound it (`nonce` / `linkage`). Empty until bound.
    bound_proof: str = ""
    #: The attempt nonce a late-id launch delivered as its brief's last line. Empty otherwise.
    nonce: str = ""

    @property
    def adopt_key(self) -> str:
        """What a mission adopts: the bound real key, or `key` for a pinned-id engine."""
        return self.bound_key or self.key

    @property
    def state(self) -> str:
        """The furthest point reached. `briefed` now precedes `started` for a mint-own-ID engine,
        so this reports the *highest* fact rather than assuming an order."""
        if self.started and self.briefed and self.bound is False:
            return "unbound"
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

        **And BOUND, for a late-id launch** (#989). A brief that reached an agent whose session
        nobody can name is not a dispatch anyone can follow through on — and without this, the
        caller adopts the placeholder, which no public path can resolve. `bound is None` is a
        pinned-id launch, whose key is the session.
        """
        return self.briefed and self.started and self.bound is not False


def _row_ids(row) -> tuple[str, str]:
    """`(id, native)` from a scan row, whatever shape the provider returns.

    **Every provider's `scan()` returns `scanner.Session`, a dataclass** — so the original
    `row.get("id")` raised `AttributeError` on the first row, the broad `except` below swallowed
    it, and this function answered "no evidence" for every session that has ever existed (#896
    review 11, finding 1, found while wiring the adopt-time existence check to it).

    It was invisible because the tests stub `prov.scan()` with dicts: a door production cannot
    reach. Dicts are still accepted — a provider is free to return them — but the dataclass is
    the shape that actually arrives.
    """
    if isinstance(row, dict):
        return str(row.get("id") or ""), str(row.get("native") or row.get("uuid") or "")
    return str(getattr(row, "id", "") or ""), str(getattr(row, "uuid", "") or "")


def store_record_state(prov, native: str) -> str:
    """`"found"` / `"absent"` / `"unreadable"` — the THREE answers (#904 review 2, finding 4).

    `unknown` is not `absent`, and collapsing them here is what made a caller's careful tri-state
    a fiction: recovery intended an exception to mean "we could not look", but the helper below
    swallowed every provider-scan error and returned `False`, so a transiently corrupt or locked
    provider store read as "that session never existed" — and a possibly live agent was torn down
    and its mission settled failed on the strength of it.

    The only test that covered it monkeypatched this function to raise, which asserts the
    caller's handling of an answer this function could not produce.
    """
    try:
        rows = prov.scan() or []
    except Exception:  # noqa: BLE001
        log.debug("could not read the store of %s", getattr(prov, "engine_id", "?"))
        return "unreadable"
    for row in rows:
        rid, ruuid = _row_ids(row)
        if ruuid == native or (rid and rid.endswith(native)):
            return "found"
    return "absent"


def _has_store_record(prov, native: str, cwd: str) -> bool:
    """Does the ENGINE'S OWN store know about this session?

    Asked of the provider's scan rather than of a file path, so it is engine-agnostic by
    construction: claude writes a transcript JSONL, the SQLite engines write a row, and this asks
    each of them the question in their own terms.

    **The boolean form, for callers that genuinely have only two branches** — the start-evidence
    poll, where "not yet" and "cannot tell" both mean "keep waiting". A caller that must not
    conclude absence from a failed read wants `store_record_state`.
    """
    return store_record_state(prov, native) == "found"


#: Start-evidence adapters, by engine id (#916). An engine with no entry here falls back to the
#: transcript store — which is correct for any engine whose store record predates the first turn,
#: and is why this is a registry rather than a rewrite of `_has_store_record`.
#:
#: **`ClaudeProvider.scan()` is deliberately NOT widened into a registry reader.** `scan()` answers
#: "what sessions exist" for the sidebar, the lookup route and every other consumer; redefining
#: transcript existence to mean "a process started" would change that answer everywhere for the
#: benefit of one caller. Two sources, two questions, one boundary.
_START_EVIDENCE = {"claude": start_evidence.claude_start_state}


async def _await_start_evidence(
    prov, native: str, cwd: str, *, timeout: float, launch=None
) -> tuple[bool, str]:
    """Condition 5. Returns ``(started, why_not)``.

    Polls for evidence the agent STARTED. Deliberately NOT `ptybridge.probe_master` or
    `session_input.is_live`: both were true throughout the 2026-08-25 incident, for a session that
    had received nothing.

    **For `claude` the transcript store cannot answer this** (#916). It writes its JSONL on the
    first turn, and the first turn is the brief this gate is withholding — so waiting on it
    deadlocked and every dispatch timed out. The adapter reads the session registry instead, which
    the engine writes at startup and which a trust screen does not produce. Engines with no adapter
    keep the old behaviour unchanged.

    **The timeout reason names what was OBSERVED.** It used to assert a first-run prompt as "the
    usual cause", which was a guess the code had not checked — and was wrong in the case that
    actually mattered, where a dispatch into an already-trusted folder failed identically and was
    told the same story.
    """
    if launch is not None:
        # A LATE-ID LAUNCH IS ASKED BY ITS LAUNCH, never by an id it does not have yet (#989). The
        # provider answers in the same three words; anything else — or a raise — is `unreadable`,
        # which keeps polling and never counts as a start.
        def adapter(_native: str, _cwd: str) -> tuple[str, str]:
            try:
                state, detail = prov.start_evidence(launch)
            except Exception as e:  # noqa: BLE001
                return start_evidence.UNREADABLE, f"the start check raised ({type(e).__name__})"
            if state not in (
                start_evidence.FOUND,
                start_evidence.ABSENT,
                start_evidence.UNREADABLE,
            ):
                return start_evidence.UNREADABLE, "the start check gave no recognisable answer"
            return state, str(detail or "")

    else:
        adapter = _START_EVIDENCE.get(prov.engine_id)
    deadline = time.monotonic() + timeout
    last_state, last_detail = "", ""
    while time.monotonic() < deadline:
        if adapter is not None:
            state, detail = await asyncio.to_thread(adapter, native, cwd)
            last_state, last_detail = state, detail
            if state == start_evidence.FOUND:
                return True, ""
        elif await asyncio.to_thread(_has_store_record, prov, native, cwd):
            return True, ""
        await asyncio.sleep(START_POLL_S)
    if adapter is None:
        return False, (
            f"the session never appeared in {prov.engine_id}'s own store within {int(timeout)}s — "
            "the process is up but the agent has not started"
        )
    # UNREADABLE is not ABSENT, and the operator is told which one happened. "We could not look"
    # sends somebody to check a permission or a path; "we looked and it was not there" sends them
    # to the screen. Reporting both as the same sentence is what made this defect cost a day.
    if last_state == start_evidence.UNREADABLE:
        return False, (
            f"could not tell whether the agent started within {int(timeout)}s — "
            f"{last_detail}. This is not a report that it failed to start."
        )
    return False, (
        f"the agent did not register a live session within {int(timeout)}s: {last_detail}. "
        "The process is up, so it is most likely holding a screen that takes input first — "
        "a trust, onboarding or re-authentication prompt."
    )


def _admission_key(engine: str, cwd: str) -> str:
    """The flock name that serialises unattended late-id launches of one engine in one folder.

    Keyed on the RESOLVED folder (review comment 72377 finding 2): a symlinked checkout and its
    target are one folder with one store, so two spellings of it must meet on one flock rather than
    each admitting a launch the other's snapshot diff could then mistake for its own.
    """
    try:
        folder = os.path.realpath(cwd)
    except (OSError, ValueError):
        folder = cwd
    return f"unattended-admit-{engine}-{uuid.uuid5(uuid.NAMESPACE_URL, 'file://' + folder)}"


async def _await_binding(
    prov, launch: engine_base.LaunchContext, *, timeout: float
) -> tuple[engine_base.Binding | None, str]:
    """Which session did this late-id launch become? Returns ``(binding, why_not)`` (#989).

    Polls `prov.bind_session(launch)` until the deadline. It publishes nothing and adopts nothing:
    discovery is side-effect-free, so a dispatch that fails or is cancelled here leaves no trace.

    **The dispatcher checks the answer rather than trusting it.** `bound` is accepted only with a
    known proof and an id this engine can actually have; anything else is treated as `unreadable`,
    because a provider that says "bound" without saying how is not an answer about this launch.
    `ambiguous` fails at once — waiting cannot make two proven candidates into one. `pending` and
    `unreadable` keep polling, and the one standing at the deadline decides the sentence: "never
    appeared" is a claim about the engine, "could not tell" is a claim about our reading of it.
    """
    engine = launch.engine
    deadline = time.monotonic() + timeout
    last = engine_base.Binding(engine_base.BIND_PENDING)
    # Whether ANY call has come back with an answer. A call cut off by the deadline, or one that
    # answers after it, says nothing new: when an earlier call answered, THAT answer stands (so a
    # launch whose binder kept saying `pending` still "never appeared"). Only when no call ever
    # answered is the binder itself the reason.
    answered = False
    while time.monotonic() < deadline:
        # THE DEADLINE BOUNDS EACH CALL, NOT ONLY THE LOOP (#994 review 1, finding 4). A binder that
        # stalls used to hold the dispatch — and the folder's admission flock — for as long as it
        # took, and its answer was then accepted however late it came. The call now gets what is
        # left of the budget. A worker thread cannot be cancelled, so a call that overruns keeps
        # running after we stop waiting; that is why `bind_session` must be a bounded,
        # side-effect-free read (`engines/base.py`) — abandoning it leaves nothing behind.
        remaining = deadline - time.monotonic()
        try:
            got = await asyncio.wait_for(
                asyncio.to_thread(prov.bind_session, launch), timeout=max(remaining, 0.0)
            )
        except TimeoutError:
            if not answered:
                last = engine_base.Binding(
                    engine_base.BIND_UNREADABLE,
                    detail="the binder did not answer within the binding budget",
                )
            break
        except Exception as e:  # noqa: BLE001 — a binder that raises cannot answer this poll
            got = engine_base.Binding(
                engine_base.BIND_UNREADABLE, detail=f"the binder raised ({type(e).__name__})"
            )
        if time.monotonic() > deadline:
            # A LATE ANSWER IS NOT AN ANSWER. Whatever it says — `bound` included — the budget the
            # operator's launch was given has passed, and binding past it would adopt a session the
            # failure path has already been entitled to tear down. The answer standing from the
            # last call that came back in time decides the sentence.
            if not answered:
                last = engine_base.Binding(
                    engine_base.BIND_UNREADABLE,
                    detail="the binder answered only after the binding budget; not accepted",
                )
            break
        answered = True
        if not isinstance(got, engine_base.Binding):
            got = engine_base.Binding(
                engine_base.BIND_UNREADABLE, detail="the binder gave no recognisable answer"
            )
        if got.state == engine_base.BIND_BOUND:
            if got.proof not in engine_base.BIND_PROOFS:
                got = engine_base.Binding(
                    engine_base.BIND_UNREADABLE,
                    detail="a binding arrived without a proof, and was not accepted",
                )
            elif not prov.id_pattern.match(str(got.native or "")):
                got = engine_base.Binding(
                    engine_base.BIND_UNREADABLE,
                    detail="a binding named an id this engine cannot have, and was not accepted",
                )
            else:
                return got, ""
        if got.state == engine_base.BIND_AMBIGUOUS:
            return None, (
                f"more than one {engine} session in this folder carries this launch's proof "
                f"({got.detail or 'ambiguous'}); none was picked"
            )
        if got.state not in (engine_base.BIND_PENDING, engine_base.BIND_UNREADABLE):
            got = engine_base.Binding(
                engine_base.BIND_UNREADABLE, detail=f"the binder answered {got.state!r}"
            )
        last = got
        await asyncio.sleep(START_POLL_S)
    if last.state == engine_base.BIND_UNREADABLE:
        return None, (
            f"could not tell which {engine} session this launch became within {int(timeout)}s — "
            f"{last.detail or 'the answer could not be read'}. This is not a report that it did "
            "not start."
        )
    return None, (
        f"the {engine} session id never appeared within {int(timeout)}s of the brief being "
        "delivered"
    )


async def _abandon(key: str, out: Dispatch) -> None:
    """Stop a launch that will never be briefed, and say so in the record.

    Reuses `runtime_cleanup.cleanup_runtime` — the same teardown archiving performs — rather than
    signalling a pid here: it terminates the master and the agent's process group, clears the
    scrollback and the owner lease, and unlinks the stale socket under the single-writer lock, so
    a later relaunch is a LAUNCH rather than a BUSY. One teardown, not a second one that drifts.

    **Through the mission fence, because this session can be ADOPTED while it is being stopped**
    (#904 review 9, finding 2). This runs on the failure paths of a launch a mission asked for,
    and between the launcher giving up and the signal going out that mission — or another — can
    commit an adoption of the very key here. Ownership is therefore asked and answered INSIDE the
    session's own fence, which is the lock an adoption itself takes, rather than before it. The
    module fence is imported lazily: this is the launcher, and it must not grow a load-time
    dependency on the mission store.
    """
    from . import mission_fence

    try:
        outcome = await mission_fence.fenced_teardown(key)
        # A NON-RAISING TEARDOWN IS NOT A STOPPED AGENT (#898 review 4, finding 1). The signal
        # path now answers about the whole process GROUP, and `leaked` means something in it
        # survived SIGKILL — a child that outlived its master. Reporting `abandoned` over that
        # tells the operator the unattended, possibly permission-bypassed agent is gone when it
        # is still running, which is the one thing this record exists to get right.
        # The word `mission_fence.abandon` would give (#966): only a non-spared, non-leaked
        # teardown proved the boundary empty.
        out.teardown = outcome if outcome in ("leaked", "spared") else "stopped"
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
        out.teardown = "error"
        out.events.append(f"abandon-failed:{type(e).__name__}")
        out.reason = f"{out.reason} (and the launched session could not be stopped)"


#: The spawn itself, as ONE name (#904 review 7, finding 2). A test that wants to stand in for
#: `dtach` patches THIS, not `subprocess.Popen` — patching the stdlib module object replaces it
#: for every other caller in the process, which is how a stubbed launch turned into unrelated
#: failures somewhere else entirely.
_popen = subprocess.Popen


async def dispatch(
    *,
    engine: str,
    cwd: str,
    brief: str,
    registry,
    bypass: bool = False,
    start_timeout: float | None = None,
    on_key: Callable[[str], None] | None = None,
    authorize: Callable[[int], str | None] | None = None,
    nonce: str | None = None,
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

    **`on_key` sees the session key before anything is spawned.** A caller that has to be able to
    reconcile a crash needs the id written down while the launch is still only an intention — a
    record that can name a session which never came to be is recoverable, one that can miss a
    session which did is not. It runs before the master exists, and a caller that refuses (by
    raising) stops the launch.

    **`authorize` runs INSIDE the launch fence, immediately before `create_subprocess_exec`.** It
    is handed the policy epoch as observed under the fence and returns a refusal reason or None.
    That ordering is the point and a later check is not equivalent: a policy withdrawal either
    completes before the fence is entered — in which case the epoch has moved and the caller
    refuses — or waits for the spawn it could not have prevented anyway. See
    `session_input.launch_fence`.

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

    # WHO MINTS THE ID DECIDES THE PATH (#989), and a late-id engine must bring its own answers.
    #
    # The safety property this whole module is about is condition 5: something the engine writes
    # is what tells a running agent apart from a first-run trust screen, because that screen is
    # armed, painted and quiet and satisfies every signal the readiness gate can see. For a
    # pinned-id engine the id is known before the launch, so the store can be asked BEFORE
    # anything is typed and the property holds.
    #
    # For an engine that mints its own id, asking the store for THE ID before the brief is a
    # deadlock — the id waits on the first turn, the first turn waits on the brief, the brief on
    # the id — and #898 refused them outright. The way out keeps the property: start evidence is
    # asked of the LAUNCH (a provider capability, still before any byte is typed) and the id is
    # bound AFTER the brief, by a proof that the session found is the one this attempt pasted into.
    # Every one of those answers is engine-specific and measured against the real engine, so an
    # engine that has not declared them all is refused here, before anything is spawned, with a
    # reason naming what is missing (`engines.unattended_start_state`).
    late_id = bool(getattr(prov, "new_session_reconciles", False))
    unattended, why_not = engines.unattended_start_state(prov)
    if not unattended:
        raise DispatchError(why_not or f"{engine} cannot be dispatched unattended")
    attempt_nonce = ""
    if late_id:
        # THE NONCE RIDES INSIDE THE SANITISED TEXT, so the cap covers it too: a brief with no room
        # left for its nonce line is refused rather than delivered without the one line that says
        # which session it became. `nonce` is the caller's when it recorded one before the spawn.
        attempt_nonce = nonce or launch_binding.mint_nonce()
        try:
            cleaned = handoff.sanitize_seed(launch_binding.envelope(cleaned, attempt_nonce))
        except ValueError:
            raise DispatchError("the dispatch carries a malformed attempt nonce") from None
        except handoff.HandoffError as e:
            raise DispatchError(
                "the brief leaves no room for this attempt's identifying line "
                f"({getattr(e, 'detail', None) or e})"
            ) from None
    # The executable the launcher will actually exec, not whatever `PATH` resolves — a probe of a
    # different binary answers a question about a different process.
    try:
        probe_bin = str(prov.new_launch_argv("preflight", cwd=cwd, bypass=False)[0])
    except Exception:  # noqa: BLE001
        probe_bin = engine

    # A LATE-ID ENGINE LAUNCHES UNDER A PLACEHOLDER it never sees (#989): its `new_launch_argv`
    # ignores the id and starts a fresh session, so the id only keys the socket, the lock and the
    # ring — for the life of the master. The real id is bound after the brief, into `bound_key`.
    native = f"new-{uuid.uuid4()}" if late_id else str(uuid.uuid4())
    key = f"{engine}:{native}"
    out = Dispatch(key=key, engine=engine, native=native, cwd=cwd, nonce=attempt_nonce)
    if late_id:
        out.bound = False

    # BEFORE THE LOCK, BEFORE THE SPAWN. Nothing exists yet, so a caller that records this and
    # then dies has a record of an intention — which is precisely what it can reconcile against
    # the engine's own store. Recording it after the spawn would leave the one window that cannot
    # be reconciled: a live agent nobody wrote down.
    if on_key is not None:
        try:
            on_key(key)
        except Exception as e:  # noqa: BLE001
            out.reason = f"the dispatch could not be recorded ({type(e).__name__})"
            return out

    # THE SINGLE-WRITER LOCK, taken before anything is spawned. A held lock means somebody else
    # owns this session and we do nothing — the arbiter is the flock, never a check-then-act.
    lock = await asyncio.to_thread(sessionlock.acquire, key)
    if lock is None:
        out.reason = "another writer already holds that session"
        return out

    handed_off = False
    may_have_inherited = False
    # The spawn worker and its give-up flag, declared out here because the `finally` owns them:
    # a thread cannot be cancelled, so the only way this frame can promise "nothing was launched"
    # is to still be holding the thread that could launch it.
    spawn: asyncio.Future | None = None
    abandoned = threading.Event()
    # The auth worker is owned by the `finally` for the SAME reason the spawn worker is (#916
    # review 3, finding 2): it holds `spawn_cwd` — a `/proc/self/fd/N` path — and may have a live
    # agent process of its own. A frame that returns while that thread runs is a frame that closes
    # the descriptor under it and leaves the probe unreaped.
    auth_task: asyncio.Future | None = None
    auth_probe = engine_auth.Probe()
    maintenance_admission: opencode_admission.Admission | None = None
    # BOUND BEFORE THE TRY, because the `finally` reads it (#904 review 8). It used to be
    # declared partway down the block, so anything that raised before that line — an engine whose
    # launch binary is not an absolute path, a handle the seed store refuses — reached the
    # cleanup with the name unbound. The `UnboundLocalError` then replaced the real reason AND
    # aborted the rest of the teardown, so the single-writer lock was never released and the
    # session was BUSY for the life of the process. A cleanup that can itself fail is not one.
    dirfd: int | None = None
    spawn_cwd = cwd
    # A late-id launch's admission flock and its launch context (#989), bound here for the same
    # reason `dirfd` is: the `finally` releases the flock on every path, including the refusals that
    # return before either is assigned.
    admission: sessionlock.SessionLock | None = None
    admission_task: asyncio.Future | None = None
    launch_ctx: engine_base.LaunchContext | None = None

    async def _reclaim() -> None:
        """Wait out the spawn worker, and reap whatever it produced after we stopped wanting it."""
        late, _why = await spawn
        if late is not None and late.returncode is None:
            # `dtach -n` forks its master and exits, so this returns at once. A launch nobody
            # waited on would otherwise leave a zombie behind for the life of the process.
            await asyncio.to_thread(late.wait)

    try:
        try:
            maintenance_admission = await opencode_admission.for_launch(engine)
        except opencode_admission.Unavailable as e:
            out.reason, out.refusal = str(e), "maintenance"
            return out
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

        # AN ENGINE WHOSE PERMISSIONS ARE CONFIG, NOT ARGV, SAYS HOW `bypass` IS HONOURED (#1050).
        # opencode's `new_launch_argv` cannot express `bypass=False` — its default policy is
        # allow-all — so a late-id engine declares `unattended_launch`, which returns the argv AND
        # the environment that enforce it. A `ValueError` there means the enforcement could not be
        # guaranteed, and nothing is spawned.
        launch_env: dict[str, str] = {}
        try:
            unattended_launch = getattr(prov, "unattended_launch", None)
            if unattended_launch is not None:
                launch, launch_env = unattended_launch(
                    native, cwd=cwd, bypass=bypass, env=dict(os.environ)
                )
            else:
                launch = prov.new_launch_argv(native, cwd=cwd, bypass=bypass)
            argv = ptybridge.launch_argv(
                engine=engine, session_id=native, launch_argv=launch, detached=True
            )
        except ValueError as e:
            raise DispatchError(str(e)) from None
        except ptybridge.PtyBridgeError as e:
            # NOTHING HAS BEEN SPAWNED, so this is a dispatch that did not happen rather than a
            # mission that failed (#904 review 2, finding 7) — the same shape the seed store's
            # refusal already takes a few lines above. `ptybridge` refuses a launch binary that
            # is not an absolute path, which is a real refusal on an install where the engine is
            # not where the provider expects it, and the operator can fix it and press again.
            raise DispatchError(str(e)) from None
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
        env.update(launch_env)
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
        # THE DIRECTORY THE OPERATOR APPROVED, AS A HANDLE (#904 review 3, finding 4).
        #
        # Every check on the path is a check on a NAME, and a name is re-resolved by whoever
        # follows it: the route compares, `authorize` compares again inside the fence, and the
        # kernel resolves `cwd=` after all of that. A project repointed in the last window still
        # launches somewhere nobody approved.
        #
        # An open descriptor is not a name. It refers to the inode that was there when the
        # approved path was resolved, and no later rename, symlink swap or config edit moves it.
        # `/proc/self/fd/N` in the CHILD resolves through the inherited fd, so the spawn happens
        # in exactly the directory this process opened.
        #
        # Falls back to the path where `/proc` is unavailable, which is not Linux — the same
        # place `dtach`, the reaper and the scope wrapper already assume they are.
        try:
            dirfd = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY)
            proc_path = f"/proc/self/fd/{dirfd}"
            if os.path.isdir(proc_path):
                spawn_cwd = proc_path
            else:  # pragma: no cover - non-Linux
                os.close(dirfd)
                dirfd = None
        except OSError:
            out.reason = f"the working directory {cwd} could not be opened"
            return out

        # CAN IT AUTHENTICATE? **Under the same ownership and cwd as the launch it gates**
        # (#916 review 2, finding 1).
        #
        # This probe LAUNCHES A REAL AGENT — `claude -p`, possibly metered. I twice placed it
        # earlier in this function and reasoned about it as a cheap read; it is not, and each time
        # it ran ahead of a fence that exists to stop launches. It now sits where a launch belongs:
        #
        #   * AFTER `on_key`, so a superseded dispatch has already refused and nothing is probed
        #     for an attempt that is over;
        #   * INSIDE the single-writer lock, so this session key is owned before any process runs
        #     under it. Holding the lock across the probe costs nothing — the key is a uuid minted
        #     moments ago and nobody else can want it;
        #   * with the PINNED cwd, `/proc/self/fd/N`, the same descriptor the spawn will use, so
        #     the probe cannot run somewhere the approved path no longer points;
        #   * behind `authorize` under the launch fence, so a withdrawn policy stops it. The fence
        #     is taken briefly for the decision and released — it is a global policy gate and must
        #     not be held across a 45s subprocess — and the spawn below re-authorizes under it
        #     again, so a policy withdrawn during the probe still cannot launch.
        @contextlib.contextmanager
        def _probe_gate():
            """The policy transaction ONE probe spawn happens inside — held across the decision
            and the process creation, released before the wait.

            The previous version took the fence in the coroutine, read `authorize`, released it,
            and only then scheduled the probes. Two things were wrong with that and the review
            named both. The spawns landed OUTSIDE the transaction that was supposed to gate them,
            so a policy withdrawn in the gap still got a real agent started; and the fence is a
            `threading.Lock`, so acquiring it on the loop blocks every other task — measured at a
            50 ms heartbeat running 401 ms late.

            Both go away by handing the fence to the worker as a context manager. `to_thread`
            takes the blocking acquisition off the loop, and `_run` enters this around
            `Popen` alone, so the probe is created under the same authority that approved it and
            the 45 s wait happens with the fence free.
            """
            with session_input.launch_fence(timeout=SPAWN_TIMEOUT_S) as epoch:
                yield authorize(epoch) if authorize is not None else ""

        if late_id:
            # THE PROVIDER'S OWN PREFLIGHT (#989), under exactly the same ownership: this worker,
            # this probe (so `abandon` reaches whatever it spawns), and this gate — the launch
            # fence around each spawn and never across a read or a wait (#921).
            auth_task = asyncio.get_running_loop().run_in_executor(
                None,
                contextvars.copy_context().run,
                partial(
                    prov.unattended_preflight, cwd=spawn_cwd, probe=auth_probe, gate=_probe_gate
                ),
            )
        else:
            auth_task = asyncio.get_running_loop().run_in_executor(
                None,
                contextvars.copy_context().run,
                partial(
                    engine_auth.check, probe_bin, cwd=spawn_cwd, probe=auth_probe, gate=_probe_gate
                ),
            )
        try:
            # SHIELDED, and joined in the `finally` — the idiom the spawn below uses, for the
            # identical reason: a thread cannot be cancelled, so the only way this frame can
            # promise "no probe is running in that directory" is to still be holding the thread.
            auth_state, auth_why = await asyncio.shield(auth_task)
        except engine_auth.Refused as e:
            out.reason = str(e)
            return out
        except session_input.AuthorityFenceBusy:
            out.reason = "the launch could not be ordered against a policy change; try again"
            return out
        except Exception as e:  # noqa: BLE001
            # A PREFLIGHT THAT CRASHES IS A REFUSAL, NOT A RAISE. `dispatch()`'s contract is to
            # return an outcome carrying a reason; the enclosing `try` has only a `finally`, so
            # an exception escaping here leaves the caller with no `DispatchOut` at all — and the
            # caller is a settlement that has to write SOMETHING about this attempt. `authorize`
            # is operator-supplied and reads a project store, so "it threw" is reachable.
            #
            # `CancelledError` is a `BaseException` and is deliberately not caught: cancellation
            # must keep propagating to the `finally` that joins the worker.
            out.reason = f"the launch could not be authorized ({type(e).__name__})"
            return out
        if late_id:
            # ONLY `ok` OPENS THE GATE, exactly as only `AUTHENTICATED` does below: `unknown` is not
            # permission, and it must not be reported as a refusal the engine never made.
            if auth_state != engine_base.PREFLIGHT_OK:
                out.reason = (
                    f"{engine} refused an unattended start on this host ({auth_why})"
                    if auth_state == engine_base.PREFLIGHT_REFUSED
                    else f"could not confirm that {engine} can start unattended ({auth_why}); "
                    "refusing to start an unattended agent on an unknown"
                )
                return out
        elif not engine_auth.may_dispatch(auth_state):
            out.reason = (
                f"{engine} cannot authenticate on this host, so an unattended agent would take "
                f"the brief and be unable to act on it ({auth_why})"
                if auth_state == engine_auth.UNAUTHENTICATED
                else f"could not confirm that {engine} can authenticate ({auth_why}); refusing "
                "to start an unattended agent on an unknown"
            )
            return out

        if late_id:
            # ONE UNATTENDED LAUNCH PER (ENGINE, FOLDER) (#989). Two of them diffing the same store
            # could each find the other's new id; the nonce already refuses to bind a session that
            # did not receive this attempt's paste, so this is a second fence rather than the only
            # one — and it deliberately does NOT claim to cover interactive launches, which the
            # proof alone excludes. A flock, so it holds across app instances like the writer lock.
            # THE ACQUISITION IS OWNED, like the spawn and auth workers (#994 review 2, finding 2).
            # The flock is handed across an await, and a cancellation after the worker takes it
            # but before `admission` is bound left a raw fd nothing would ever release — every
            # later unattended launch in this folder refused, with nothing running. The task is
            # kept, and the `finally` joins it and releases whatever it acquired.
            admission_task = asyncio.ensure_future(
                asyncio.to_thread(sessionlock.acquire, _admission_key(engine, cwd))
            )
            admission = await asyncio.shield(admission_task)
            if admission is None:
                out.reason = (
                    f"another unattended {engine} launch is starting in this folder; "
                    "try again once it has started"
                )
                return out
            # THE BASELINE, taken AFTER the preflight — whose own probe may leave a session behind
            # in this folder — and BEFORE the spawn. A read that failed is not an empty folder:
            # every id already there would then look new, so nothing is launched on it.
            try:
                snapshot = await asyncio.to_thread(prov.snapshot_session_ids, cwd)
            except Exception:  # noqa: BLE001 — a store that raises has told us it cannot answer
                snapshot = None
            if snapshot is None:
                out.reason = (
                    f"{engine}'s own store could not be read before the launch, so the session "
                    "this launch becomes could not be told apart from one that already existed"
                )
                return out
            launch_ctx = engine_base.LaunchContext(
                engine=engine,
                key=key,
                native=native,
                cwd=cwd,
                nonce=attempt_nonce,
                snapshot=frozenset(str(s) for s in snapshot),
                launched_at=time.time(),
            )

        may_have_inherited = True
        try:
            # THE LAUNCH FENCE, AND IT RUNS OFF THE EVENT LOOP (#904 review 7, finding 2).
            #
            # `authorize` compares the policy fingerprint it is handed against the one the caller
            # captured, under the same two locks `policy_transaction` holds across a withdrawal —
            # so "switch orchestration off" and "start an unattended agent" cannot interleave
            # (#904 review 4). The fence has to be held across the spawn itself: a check released
            # before it would be a check again, and checks are what this fence exists to stop
            # being the whole answer.
            #
            # The first version held it across `await create_subprocess_exec(...)`, which is the
            # #888 deadlock in a new place: `session_input._lock` is a `threading.Lock`, the
            # viewer attach path calls `bump_epoch()` synchronously ON THE LOOP, and an attach
            # arriving while the spawn was suspended blocked the loop — so the spawn could never
            # complete and the lock was never released. Neither side can make progress.
            #
            # So the lock and the spawn go onto ONE worker thread together, the idiom
            # `_fenced_write` uses, and the spawn becomes a plain `Popen`. That is not a
            # workaround for the lock: `dtach -n` forks its master and returns immediately, so
            # there was never anything to await except asyncio's own subprocess-watcher setup —
            # which is precisely the machinery that needs the loop this was blocking.
            #
            # Still a literal argv list, never a command string, so the shell-free guarantee and
            # its `pr-validate` grep are untouched.
            def _fenced_spawn():
                # BOUNDED, so the caller's worker is bounded (#904 review 8, finding 2). A thread
                # cannot be cancelled, so whatever this does, the caller is going to have to wait
                # for it — which is only safe if "it" has a deadline.
                with session_input.launch_fence(timeout=SPAWN_TIMEOUT_S) as policy_epoch:
                    if authorize is not None:
                        why = authorize(policy_epoch)
                        if why:
                            return None, why
                    # …AND ABANDONED IS A REFUSAL. The caller sets this when it has given up, and
                    # the check is inside the fence, immediately before the spawn: the common
                    # shape of a late worker is one still queued for the fence when the deadline
                    # passes, and it must not start an agent for a dispatch already reported as
                    # having launched nothing. Losing this race is survivable — the caller waits
                    # for this thread before tearing down — but not starting is much better than
                    # starting and killing.
                    if abandoned.is_set():
                        return None, "the launch was abandoned before anything was spawned"
                    return (
                        _popen(  # noqa: S603 — literal argv, no shell
                            argv,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            cwd=spawn_cwd,
                            env=env,
                            start_new_session=True,
                            close_fds=True,
                            # The lock fd goes to the dtach master, so the flock lives exactly as
                            # long as the agent and survives an app restart. The DIRECTORY fd
                            # rides along because `cwd=/proc/self/fd/N` is resolved by the child,
                            # which needs to have inherited it — `close_fds=True` would otherwise
                            # take it.
                            pass_fds=((lock.fd,) if dirfd is None else (lock.fd, dirfd)),
                        ),
                        "",
                    )

            # THE WORKER STAYS OURS UNTIL IT HAS DEFINITIVELY FINISHED (#904 review 8, finding
            # 2). `wait_for(to_thread(...))` reads like a bounded spawn and is not one: cancelling
            # the await cancels nothing on the thread. On a timeout — or on a cancelled request —
            # the coroutine returned "nothing was launched", the cleanup below released the
            # single-writer lock and closed the directory handle, and the worker then went on to
            # `_popen()` a permission-bypassed agent nobody was ever told about, outside the
            # teardown that would have stopped it.
            #
            # So the task is SHIELDED from the deadline: the wait ends, the worker does not, and
            # `finally` joins it before it touches the lock or the descriptor. Every path out of
            # this frame therefore leaves the thread finished, and a spawn that won the race is
            # covered by the same teardown as any other failed launch.
            spawn = asyncio.get_running_loop().run_in_executor(
                None, contextvars.copy_context().run, _fenced_spawn
            )
            proc, refused = await asyncio.wait_for(asyncio.shield(spawn), timeout=SPAWN_TIMEOUT_S)
            if proc is None:
                out.reason = refused
                return out
        except session_input.AuthorityFenceBusy:
            out.reason = "the launch could not be ordered against a policy change; try again"
            return out
        except (TimeoutError, OSError) as e:
            out.reason = f"the launch could not be spawned ({type(e).__name__})"
            return out
        # The child has the directory it needs; ours has done its job.
        if dirfd is not None:
            with contextlib.suppress(OSError):
                os.close(dirfd)
            dirfd = None
        # `dtach -n` forks its master and returns immediately, so this is a formality — but
        # it is a BLOCKING wait now, and blocking calls do not belong on the loop.
        await asyncio.to_thread(proc.wait)
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
        if maintenance_admission is not None:
            maintenance_admission.release()
            maintenance_admission = None
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
            # START EVIDENCE FIRST, ALWAYS. For a pinned-id engine the id is known before the
            # launch, so the store can be asked directly. For a late-id engine it is asked of the
            # LAUNCH (#989) — the provider's own start check, still before any byte is typed. Either
            # way it is the one signal a trust screen cannot fake: that screen is armed, painted
            # and quiet, so it passes the readiness gate perfectly and is invisible to all else.
            started, why_not = await _await_start_evidence(
                prov, native, cwd, timeout=evidence_timeout, launch=launch_ctx
            )
            if not started:
                out.reason = why_not
                return out
            out.started = True
            out.events.append("started")

            # The readiness gate still protects the delivery — armed + painted + quiet — with the
            # start evidence above as a SECOND fence in front of it, not a replacement for it.
            delivered, why = await headless_seed.deliver(key, key)
            if not delivered:
                out.reason = why or "the brief was not delivered"
                return out
            out.briefed = True
            out.events.append("briefed")

            if launch_ctx is not None:
                # WHICH SESSION IT BECAME (#989), bound AFTER the brief because that is when a
                # late-id engine writes its id — and bound only on a proof that the session found
                # received THIS paste. Nothing is published here: the caller adopts first, and the
                # alias is a projection of that commit. A launch that never binds is a failure with
                # a reason, and the `finally` below tears it down like any other.
                binding, why_unbound = await _await_binding(
                    prov,
                    launch_ctx,
                    # ITS OWN BUDGET (#994 review 1, finding 4). `start_timeout` still overrides it,
                    # as it overrides every wait in this function, so a test states one budget.
                    timeout=(BIND_TIMEOUT_S if start_timeout is None else start_timeout),
                )
                if binding is None:
                    out.reason = why_unbound
                    return out
                out.bound = True
                out.bound_key = f"{engine}:{binding.native}"
                out.bound_proof = binding.proof
                out.events.append("bound")
            return out
        except Exception as e:  # noqa: BLE001
            out.reason = f"the dispatch failed ({type(e).__name__})"
            return out
    finally:
        # Maintenance admission must cover every worker that can still create OpenCode,
        # including auth probes. Repeated cancellation cannot shorten their actual lifetime.
        if maintenance_admission is not None:
            abandoned.set()
            pending = []
            if auth_task is not None:
                pending.extend(
                    [
                        asyncio.get_running_loop().run_in_executor(None, auth_probe.abandon),
                        auth_task,
                    ]
                )
            if spawn is not None:
                pending.append(spawn)
            for task in pending:
                while not task.done():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await asyncio.shield(task)
            maintenance_admission.release()
        # FIRST, RECLAIM THE SPAWN WORKER (#904 review 8, finding 2). Nothing below may run while
        # a thread might still be inside `_popen` with these descriptors: closing `dirfd` would
        # hand the child a recycled fd, and releasing the lock would let the next attempt start a
        # second permission-bypassed agent beside the one this thread is about to create.
        #
        # `abandoned` first, so a worker still queued for the fence refuses instead of spawning;
        # then the join, which is bounded because the fence acquisition is. Shielded for the same
        # reason the teardown below is: this runs on the cancellation path, where an unshielded
        # await is cancelled at its first suspension point — exactly when the guarantee matters.
        if spawn is not None:
            abandoned.set()
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.ensure_future(_reclaim()))
        # THEN THE AUTH WORKER, on the same terms and before the same descriptor close. `abandon`
        # first so a probe still queued for the fence refuses instead of spawning and a running
        # one is killed by group; then the join, because `abandon` returns as soon as it has
        # signalled — the thread is still inside `communicate()` reaping what it was handed.
        if auth_task is not None:
            # …AND ABANDONMENT IS BLOCKING WORK, SO IT LEAVES THE LOOP TOO (review 4, finding 2).
            #
            # `abandon()` takes the ownership lock that the worker holds across `Popen`, then
            # signals a process group and waits on it. Called straight from this coroutine it
            # stalled every other task for as long as a spawn took — measured at a 30 ms heartbeat
            # arriving 599 ms late with a 600 ms `Popen`. Moving the launch fence off the loop did
            # not cover this: it is a second blocking acquisition, on the teardown side.
            #
            # The serialization is the point and is kept — it is what stops a cancellation missing
            # a process created a moment later — so the lock is not weakened; the WAIT for it just
            # happens somewhere it does not hold the loop. Shielded and joined, because this runs
            # on the cancellation path and a half-finished teardown is the thing being prevented.
            with contextlib.suppress(Exception):
                await asyncio.shield(asyncio.ensure_future(asyncio.to_thread(auth_probe.abandon)))
            with contextlib.suppress(Exception):
                await asyncio.shield(auth_task)
        # The directory handle, on every path out. Ours is only needed until the child has been
        # spawned with it inherited; a return between the open and that point would otherwise
        # leak a descriptor per refused dispatch.
        if dirfd is not None:
            with contextlib.suppress(OSError):
                os.close(dirfd)
            dirfd = None
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
                await asyncio.shield(asyncio.ensure_future(_abandon(key, out)))
            if not handed_off:
                # The teardown has stopped whatever inherited it, so the lock is ours to drop —
                # and it must be dropped, or a launch that failed before the transfer leaves the
                # session BUSY with nothing running.
                with contextlib.suppress(Exception):
                    lock.release()
        # WHAT WAS TYPED, asked of the seed store AFTER the teardown (#966). The claim/ack record is
        # the only witness to whether bytes reached the PTY, and reading it last means no writer of
        # this launch is left to change the answer. Retiring the seed in the same step means a
        # later attach cannot type the brief into a session this launch has given up on. A read
        # that fails leaves `unknown`, never a guess.
        if out.ok:
            out.seed_outcome = "delivered"
        else:
            with contextlib.suppress(Exception):
                out.seed_outcome = handoff.retire_seed(key)
        # THE ADMISSION FLOCK LAST (#989), after the teardown: releasing it while a failed launch
        # is still being stopped would let the next unattended launch in this folder snapshot a
        # store this one's agent may still be writing to.
        if admission is None and admission_task is not None:
            # A cancelled acquisition may still have taken the lock on its worker. Join it —
            # shielded and repeated, because this runs while a cancellation is unwinding — and
            # release what it got, so the folder is never left refusing launches.
            while not admission_task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(admission_task)
            if not admission_task.cancelled() and admission_task.exception() is None:
                admission = admission_task.result()
        if admission is not None:
            with contextlib.suppress(Exception):
                admission.release()
