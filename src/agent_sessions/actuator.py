"""Delivering an orchestrator action into a live session (#726 Phase 2).

This is the module that turns a *decision* into *bytes*, and it is deliberately the narrowest
thing that can do so. The model never reaches here with text it authored freely:

* ``continue`` sends the operator's ``nudge_template`` — the model chose *whether*, never
  *what*. That asymmetry is the entire reason ``continue`` is the one verb allowed to run
  without a tap.
* ``choose`` sends a server-rendered digit + CR, from an integer already bounds-checked at
  proposal time. No model text reaches the PTY at all.
* ``answer`` sends model prose — but only ever after an explicit human approval, through
  ``handoff.sanitize_seed`` (control bytes stripped, capped), framed as one bracketed paste so
  an embedded ``ESC`` cannot terminate the paste early and smuggle raw key input.

**Compare-and-execute.** A proposal is a claim about a screen the operator saw. By the time
anyone approves it the agent may have moved on, and delivering ``choose 1`` into a *different*
prompt is the failure this whole design exists to prevent. So every precondition is re-verified
inside :func:`deliver`, immediately before the first byte — never at queue time, never by the
caller. Four things are checked, and all of them can change between proposal and delivery:

1. the engine is still orchestrator-actuable (``supports_orchestrator_input``),
2. the session is still managed (not ``orchestrator_excluded``),
3. the screen still matches the fingerprint + prompt class the pass observed,
4. no viewer is attached or recently active — the operator and the orchestrator must never
   type at the same time.

**At-most-once, stated honestly.** The ledger moves ``proposed|approved → claimed`` *before*
the write and to a terminal state after. If the process dies in between, nothing on disk can
prove whether the bytes landed, so startup recovery parks it as ``indeterminate`` rather than
retrying (double-delivery) or assuming success (silent drop).
"""

from __future__ import annotations

import asyncio
import time

from . import (
    engines,
    handoff,
    metadata,
    orchestrator,
    prefs,
    scrollback,
    session_input,
)
from . import (
    orchestrator_ledger as ledger,
)

# A viewer who typed or looked recently owns the keyboard; we stay off it.
VIEWER_RECENT_S = 60.0
# The only states an action may be claimed from.
CLAIMABLE_STATES: frozenset[str] = frozenset({"proposed", "approved"})

# What `render()` can actually turn into bytes. Declared HERE, beside the renderer, and shipped
# to the client by the state route — the UI previously kept its own copy that included
# `dispatch`, so it offered Approve on an action every delivery attempt would 409.
RENDERABLE_VERBS: frozenset[str] = frozenset({"continue", "choose", "answer"})
NUDGE_MAX = 2000
# Pause between consecutive autonomous deliveries in one pass.
DELIVERY_SPACING_S = 1.0


class NotDeliverable(Exception):
    """The action cannot be delivered at all (unknown, wrong state, unsupported verb)."""


def render(action: dict, cfg: dict) -> bytes:
    """The bytes for one action. Raises :class:`NotDeliverable` for anything else.

    Every branch here is server-authored except ``answer``, which is sanitised and only ever
    reached behind an explicit approval.
    """
    verb = action.get("verb")
    if verb == "continue":
        text = str(cfg.get("nudge_template") or prefs.DEFAULT_ORCH_NUDGE)[:NUDGE_MAX]
        return session_input.bracketed_paste(text)
    if verb == "choose":
        opt = action.get("option")
        if not isinstance(opt, int) or isinstance(opt, bool):
            raise NotDeliverable("choose without a validated option")
        if not (orchestrator.OPTION_MIN <= opt <= orchestrator.OPTION_MAX):
            raise NotDeliverable("choose option out of range")
        # A digit and a carriage return. No paste framing, no model text — a numbered prompt
        # wants a keypress, and the narrower the payload the smaller the blast radius.
        return f"{opt}\r".encode()
    if verb == "answer":
        text = handoff.sanitize_seed(str(action.get("answer") or ""))
        if not text.strip():
            raise NotDeliverable("answer with no usable text")
        return session_input.bracketed_paste(text)
    raise NotDeliverable(f"verb {verb!r} is not deliverable")


def _viewer_busy(phys_key: str, registry) -> bool:
    """True when a browser is attached, or was producing output very recently. Best-effort: a
    registry hiccup must not silently *enable* a write, so an error reads as busy."""
    if registry is None:
        return False
    try:
        for row in registry.snapshot():
            if row.get("id") != phys_key:
                continue
            if row.get("attached"):
                return True
            last = row.get("last_output_at")
            if isinstance(last, int | float) and (time.time() - last) < VIEWER_RECENT_S:
                return True
        return False
    except Exception:
        return True


def check_precondition(action: dict, *, registry=None) -> tuple[bool, str]:
    """Re-verify everything the proposal assumed. Blocking (ring replay + metadata read).

    Returns ``(ok, reason)``. Deliberately re-derives from live state rather than trusting
    anything cached on the action — a check that reads its own inputs from the record it is
    guarding is not a check.
    """
    sid = action.get("session_id") or ""
    try:
        prov, _native = engines.parse_key(sid)
    except Exception:
        return False, "session id no longer resolves"

    # (1) engine capability — re-checked at the write boundary, not merely filtered upstream.
    # `shell` is an agentless bash: a nudge typed into one is a command.
    if not engines.supports_orchestrator_input(prov):
        return False, f"engine {getattr(prov, 'engine_id', '?')} is not orchestrator-actuable"

    # (2) the operator may have withdrawn agency AFTER this was proposed.
    mkey = metadata.resolve_key(sid)
    if metadata.get(mkey).orchestrator_excluded:
        return False, "session is no longer managed by the orchestrator"

    phys = engines.physical_key(sid)

    # (3) nobody else is at the keyboard.
    if _viewer_busy(phys, registry):
        return False, "a viewer is attached or was just active"

    # (4) the screen still is what the pass judged.
    pre = action.get("precondition") or {}
    want_fp = pre.get("screen_fingerprint")
    if want_fp:
        screen = scrollback.live_tail_text(phys, orchestrator.PRECONDITION_CHARS)
        if orchestrator._screen_fingerprint(screen) != want_fp:
            return False, "the session's screen changed since this was proposed"
        want_class = pre.get("prompt_class")
        if want_class and orchestrator._prompt_class(screen) != want_class:
            return False, "the session is at a different kind of prompt now"
    return True, ""


def _policy_fingerprint() -> tuple:
    """A cheap, comparable snapshot of every policy value a write depends on.

    Compared inside the write fence, so any change between authorization and byte one refuses.
    A tuple rather than the dict itself because it must be hashable/comparable and stable —
    and narrow, so an unrelated preference edit does not spuriously cancel a delivery.
    """
    cfg = prefs.get_orchestrator()
    return (
        bool(cfg.get("enabled")),
        str(cfg.get("autonomy")),
        tuple(sorted(cfg.get("allowed_verbs") or ())),
        float(cfg.get("confidence_min") or 0),
    )


def _settle_waiting(action_id: str, state: str, **fields) -> dict | None:
    """Settle an action that has NOT been claimed yet, atomically.

    Every early return in `deliver()` sits between a `ledger.get()` and the claim, so a blind
    `transition()` here can overwrite a claim another caller landed in that gap — recording
    `expired` or `failed` on top of a delivery that is already underway. CAS from the waiting
    states means the claimant wins and this quietly does nothing.
    """
    return ledger.compare_and_set(action_id, ledger.REJECTABLE_STATES, state, **fields)


async def deliver(action_id: str, *, registry=None, authority=None) -> dict:
    """Deliver one ledger action. Returns the resulting ledger record.

    The state machine is the safety property, so the ordering matters: ``claimed`` is written
    and fsynced BEFORE any byte reaches the PTY. That is what makes a crash recoverable — the
    record proves a delivery was in flight even though it cannot prove the outcome.
    """
    rec = ledger.get(action_id)
    if rec is None:
        raise NotDeliverable("unknown action")
    if rec.get("state") not in CLAIMABLE_STATES:
        raise NotDeliverable(f"action is {rec.get('state')}, not deliverable")

    exp = rec.get("expires_at")
    if isinstance(exp, int | float) and time.time() >= exp:
        return _settle_waiting(action_id, "expired") or rec

    cfg = prefs.get_orchestrator()
    # The master switch fences EVERY write, not just autonomous ones. This read used to feed
    # `render` only, so a proposal sitting in a stale tab could still be approved after the
    # operator switched orchestration off — directly contradicting the OFF tier's own copy,
    # which promises nothing is ever sent. An operator's tap is consent to THIS action, not a
    # standing exemption from the switch they just flipped.
    if not cfg.get("enabled"):
        return _settle_waiting(action_id, "stale", detail="orchestration is switched off") or rec
    if cfg.get("autonomy") == "off":
        return _settle_waiting(action_id, "stale", detail="autonomy is set to off") or rec

    try:
        payload = render(rec, cfg)
    except NotDeliverable as e:
        return _settle_waiting(action_id, "failed", detail=str(e)) or rec

    phys = engines.physical_key(rec["session_id"])
    if not session_input.is_live(phys):
        return _settle_waiting(action_id, "failed", detail="session is not live") or rec

    # Claim BEFORE writing, and ATOMICALLY. A read-then-write across two lock holds lets two
    # callers both see `proposed` and both write — a duplicate `choose` answers a prompt twice.
    if ledger.claim(action_id, CLAIMABLE_STATES) is None:
        raise NotDeliverable("another caller claimed this action first")

    def _final_guard() -> tuple[bool, str]:
        """Evaluated UNDER the write lock, immediately before the first byte.

        Everything above this ran before the quiet wait, the fd borrow and the lock queue —
        seconds during which the operator can switch orchestration off and a browser can
        attach and start typing. Re-asking here is the only way those actions actually win;
        checked earlier, they lose to a verdict formed before they happened.
        """
        live = prefs.get_orchestrator()
        if not live.get("enabled"):
            return False, "orchestration was switched off before the write"
        if live.get("autonomy") == "off":
            return False, "autonomy was set to off before the write"
        # `authority` carries whatever EXTRA permission this particular delivery rests on.
        # An automatic delivery is authorised by the yolo tier plus the verb ceiling plus the
        # confidence threshold — none of which the checks above re-examine, so without this a
        # yolo->suggest switch mid-wait still types the payload. A manual approval has no
        # extra authority to re-check: the operator's tap is the authority, and it stays valid
        # in suggest, which is why this is a parameter rather than a blanket yolo requirement.
        if authority is not None:
            ok, why = authority(live)
            if not ok:
                return False, why
        # The screen/viewer contract is "no viewer at the keyboard, and the screen still looks
        # like the one that was proposed against" — as of NOW, not as of setup.
        return check_precondition(rec, registry=registry)

    outcome = await asyncio.to_thread(
        session_input.send_input,
        phys,
        payload,
        precondition=lambda: check_precondition(rec, registry=registry),
        final_guard=_final_guard,
        # The third domain. `_final_guard` reads policy and then does the screen check, so a
        # flip between those two still slipped through — the guard's verdict is only as fresh
        # as the moment it ran. This is re-read INSIDE the fence, immediately before byte one,
        # so a withdrawal at any point up to the write refuses.
        policy_fingerprint=_policy_fingerprint,
    )
    state = {
        "delivered": "delivered",
        "stale": "stale",
        "aborted": "failed",
        "refused": "stale",
        "not_live": "failed",
        "failed": "failed",
    }.get(outcome.state, "failed")
    # CAS strictly from `claimed`: we hold the claim, so any other state means something
    # else settled this action while we were writing and its verdict must stand.
    return (
        ledger.compare_and_set(
            action_id,
            frozenset({"claimed"}),
            state,
            detail=outcome.detail,
            outcome=outcome.state,
        )
        or rec
    )


async def deliver_auto(action: dict, *, registry=None) -> dict | None:
    """Deliver an action the pass already auto-approved (``yolo``). Returns the record, or
    ``None`` when the tier/ceiling says it must wait for a tap.

    The ceiling is re-read here rather than trusted from the pass: prefs can change between a
    proposal being minted and this running, and the safe direction is to re-ask.
    """
    cfg = prefs.get_orchestrator()
    # `enabled` is the master switch and belongs in this gate too. Checking only the tier
    # meant a disabled orchestrator still delivered anything a pass had already approved —
    # switching it off has to stop writes, not just stop new proposals.
    if not cfg.get("enabled"):
        return None
    if cfg["autonomy"] != "yolo":
        return None
    if action.get("verb") not in set(cfg["allowed_verbs"]):
        return None
    if float(action.get("confidence") or 0) < float(cfg["confidence_min"]):
        return None

    def _auto_authority(live: dict) -> tuple[bool, str]:
        """Re-assert, at the write boundary, everything that made this AUTOMATIC.

        The checks above ran before the claim, the quiet wait and the lock queue. An operator
        who drops out of yolo, narrows `allowed_verbs`, or raises `confidence_min` in that
        window has withdrawn the authority this delivery rests on, and it must not proceed on
        the strength of a tier they have left.
        """
        if live.get("autonomy") != "yolo":
            return False, "autonomy left yolo before the write"
        if action.get("verb") not in set(live["allowed_verbs"]):
            return False, "the verb left the allowed set before the write"
        if float(action.get("confidence") or 0) < float(live["confidence_min"]):
            return False, "the confidence threshold was raised above this action before the write"
        return True, ""

    return await deliver(action["id"], registry=registry, authority=_auto_authority)


async def deliver_pass_actions(records: list[dict], *, registry=None) -> list[dict]:
    """Deliver the actions a pass already auto-approved.

    Without this the `yolo` tier is inert: `_decide` records `approved`, and then nothing
    delivers it — the operator is told the orchestrator acts on its own while it sits waiting
    for a tap it was never supposed to need. Wired into BOTH the scheduled loop and the manual
    pass, since either can produce approvals.

    Serialized with spacing, matching the endpoint-call posture: a burst of nudges landing at
    once across several sessions is its own kind of alarming.
    """
    out: list[dict] = []
    for rec in records:
        if rec.get("state") != "approved":
            continue
        try:
            # deliver_auto, NOT deliver: policy is re-read at the WRITE boundary, per action.
            # A pass can persist and then deliver over many seconds (DELIVERY_SPACING_S between
            # each), and an operator who switches orchestration off — or drops out of yolo —
            # mid-batch must not have the remaining actions typed into their sessions on the
            # strength of a decision the pass made before they changed their mind.
            res = await deliver_auto(rec, registry=registry)
            if res is None:
                continue  # live policy withdrew it; nothing was written
            out.append(res)
        except NotDeliverable:
            continue  # already claimed, expired, or no longer deliverable — never fatal
        await asyncio.sleep(DELIVERY_SPACING_S)
    return out
