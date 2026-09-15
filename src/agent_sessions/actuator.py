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

**At-most-once, stated honestly.** The ledger moves any :data:`CLAIMABLE_STATES` member —
``proposed``, ``approved`` and, since #877, ``escalated_low_confidence`` — to ``claimed``
*before* the write, and to a terminal state after. If the process dies in between, nothing on
disk can prove whether the bytes landed, so startup recovery parks it as ``indeterminate``
rather than retrying (double-delivery) or assuming success (silent drop).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from . import (
    engines,
    handoff,
    metadata,
    orchestrator,
    prefs,
    ptybridge,
    scrollback,
    session_input,
)
from . import (
    orchestrator_ledger as ledger,
)

# A viewer who typed or looked recently owns the keyboard; we stay off it.
VIEWER_RECENT_S = 60.0
# The only states an action may be claimed from. Re-exported, NOT re-declared: the authority is
# `orchestrator_ledger`, because `project_for_operator` gates the operator's Approve control on
# this very set and must not be able to disagree with the path that enforces it. Two copies is
# how an Approve button shipped for `escalated`, which `deliver` answers with a 409.
CLAIMABLE_STATES: frozenset[str] = ledger.CLAIMABLE_STATES


def working_keys(registry) -> set[str]:
    """Sessions that are live right now — recent server-owned output, or a viewer attached.

    Shared rather than duplicated: `orchestrator_chat.ask` uses this overlay for eligibility, so
    two callers computing it differently would propose against different views of what is busy.
    Matches either the logical or physical key (a reconciled opencode session registers under its
    placeholder). Best-effort — a registry hiccup yields no overlay rather than failing the turn.
    """
    if registry is None:
        return set()
    keys: set[str] = set()
    with contextlib.suppress(Exception):
        for r in registry.snapshot():
            if r.get("working") or r.get("attached"):
                keys.add(r["id"])
    return keys


# How recent screen-changing output must be to count as "working right now" (#969). The same
# 10 s window as the `working` indicator (`session_stream._WORKING_WINDOW_S`, #156) — a test
# pins the two equal, since importing `session_stream` here would drag in the terminal stack.
BUSY_WINDOW_S = 10.0


def busy_keys(registry, now: float | None = None) -> set[str]:
    """Sessions VISIBLY working right now: output that could change the screen within
    :data:`BUSY_WINDOW_S` (#969). Call on the event loop — `snapshot()` iterates a mutable dict.

    Deliberately not `working_keys`. That overlay also counts an attached browser and a
    window-title blink, so an idle session in an open pane — or a codex session waiting on the
    operator — would read as busy, and those are exactly the sessions a proposal exists for.
    Best-effort: a registry hiccup yields no busy set, which leaves eligibility as it was before
    this filter existed; delivery's own preconditions are untouched either way.
    """
    if registry is None:
        return set()
    now = time.time() if now is None else now
    keys: set[str] = set()
    with contextlib.suppress(Exception):
        for r in registry.snapshot():
            seen = r.get("visible_output_at")
            if isinstance(seen, int | float) and not isinstance(seen, bool):
                if now - seen < BUSY_WINDOW_S:
                    keys.add(r["id"])
    return keys


# What `render()` can actually turn into bytes. Declared HERE, beside the renderer, and shipped
# to the client by the state route — the UI previously kept its own copy that included
# `dispatch`, so it offered Approve on an action every delivery attempt would 409.
RENDERABLE_VERBS: frozenset[str] = frozenset({"continue", "choose", "answer", "relay"})

#: `relay` is OPERATOR-AUTHORED bytes, and #840 §9 is explicit that this makes it a **narrower**
#: authority than the model-authored `answer`, not a wider one — the operator typing their own
#: words is the thing every other verb is a proposal to do on their behalf.
#:
#: It is not a new fence and must not become one. It renders through `render` like everything
#: else, is claimed before the write like everything else, and passes the same
#: `handoff.sanitize_seed` a seed does — so it cannot terminate a bracketed paste early or
#: smuggle raw key input. What distinguishes it is AUTHORSHIP, which the timeline records, so a
#: reader can always tell the operator's words from the model's.
OPERATOR_VERBS: frozenset[str] = frozenset({"relay"})
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
    if verb == "relay":
        # THE OPERATOR'S OWN WORDS (#894). Same rendering and the same sanitiser as `answer` —
        # deliberately, because the payload shape is not what differs between them. What differs
        # is who wrote it, and that is recorded on the action and in the timeline rather than
        # expressed as a looser payload rule.
        text = handoff.sanitize_seed(str(action.get("answer") or ""))
        if not text.strip():
            raise NotDeliverable("relay with no usable text")
        return session_input.bracketed_paste(text)
    raise NotDeliverable(f"verb {verb!r} is not deliverable")


def _viewer_busy(phys_key: str, registry, *, operator_approval: bool = False) -> bool:
    """True when a browser is attached, or was producing output very recently. Best-effort: a
    registry hiccup must not silently *enable* a write, so an error reads as busy.

    ``operator_approval`` (#969) drops ONLY the attached half. The rule keeps the orchestrator off
    the keyboard while the operator is at it — but an explicit approval IS the operator, and since
    #948 P3 it is tapped inside the session's own pane, which is itself an attached viewer, so
    every one was refused. Recent output still refuses (typing echoes, so it covers someone
    typing too), and an error still reads as busy.
    """
    if registry is None:
        return False
    try:
        for row in registry.snapshot():
            if row.get("id") != phys_key:
                continue
            if row.get("attached") and not operator_approval:
                return True
            last = row.get("last_output_at")
            if isinstance(last, int | float) and (time.time() - last) < VIEWER_RECENT_S:
                return True
        return False
    except Exception:
        return True


def screen_matches(phys: str, pre: dict) -> tuple[bool, str]:
    """The screen half of the precondition: does the session still show what the pass judged?

    Shared by delivery (`check_precondition`) and withdrawal (`withdraw_undeliverable`, #969), so
    a proposal is withdrawn for exactly the reason its delivery would be refused, never for a
    near-copy of it. A precondition with no fingerprint has nothing to compare. Blocking.
    """
    want_fp = pre.get("screen_fingerprint")
    if not want_fp:
        return True, ""
    screen = scrollback.live_tail_text(phys, orchestrator.PRECONDITION_CHARS)
    if orchestrator._screen_fingerprint(screen) != want_fp:
        return False, "the session's screen changed since this was proposed"
    want_class = pre.get("prompt_class")
    if want_class and orchestrator._prompt_class(screen) != want_class:
        return False, "the session is at a different kind of prompt now"
    return True, ""


def check_precondition(
    action: dict, *, registry=None, operator_approval: bool = False
) -> tuple[bool, str]:
    """Re-verify everything the proposal assumed. Blocking (ring replay + metadata read).

    Returns ``(ok, reason)``. Deliberately re-derives from live state rather than trusting
    anything cached on the action — a check that reads its own inputs from the record it is
    guarding is not a check. ``operator_approval`` narrows only the viewer check (see
    `_viewer_busy`); everything else is identical for a tap and for an autonomous send.
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
    if _viewer_busy(phys, registry, operator_approval=operator_approval):
        return False, "a viewer is attached or was just active"

    # (4) the screen still is what the pass judged.
    return screen_matches(phys, action.get("precondition") or {})


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


def _mission_membership_authority(rec: dict):
    """`(check, fingerprint)` for any action that names a mission AND a session, or `(None, None)`.

    **An action authorised under one mission must not land in another mission's session** (#903
    review, finding 1). The route checks membership before it appends, but between that check and
    byte one lie a ledger append, a quiet wait, an fd borrow and a lock queue — seconds in which
    the operator can detach the session and a second mission can adopt it. The relay would then
    deliver mission A's words into mission B's work.

    DERIVED FROM THE RECORD, deliberately, exactly as `_supervisor_authority` is: whoever
    delivers the action gets the enforcement without knowing it exists. That is what stopped the
    equivalent supervisor bug being re-introduced through the ordinary approve route, and the
    same reasoning applies to every operator-origin mission action added later.

    The generic mission fence in `_final_guard` does NOT cover this. Its question is "is this
    session's mission being torn down", and a session re-adopted by a healthy second mission is
    barred by nothing — correctly, for that second mission's own writes.

    Fails CLOSED: this is the last check before a real pty, and an unverifiable membership is not
    a membership.
    """
    mission_id = str(rec.get("mission_id") or "")
    session_key = str(rec.get("session_id") or "")
    if not (mission_id and session_key):
        return None, None

    def _state():
        from . import missions

        return missions.session_mission(session_key)

    def _check() -> tuple[bool, str]:
        from . import missions

        try:
            holder = missions.session_mission(session_key)
        except Exception:
            return False, "the mission store could not be read, so authority is unverifiable"
        if holder is None:
            return False, "the session left this mission before the write"
        if holder != mission_id:
            return False, f"the session was adopted by mission {holder} before the write"
        return True, ""

    return _check, _state


def _supervisor_authority(rec: dict):
    """`(check, fingerprint)` for a supervisor-minted action, or `(None, None)`.

    DERIVED FROM THE RECORD, not passed in by a caller, and that is the whole point. An action
    minted by the supervisor carries `source`, `mission_id` and `objective_key`; whoever delivers
    it — this module's auto path, an operator tapping approve on a proposal from ten minutes ago,
    any future path — gets the same enforcement without knowing it exists.

    Handing callbacks to one call site protected one call site. In suggest mode a supervisor
    proposal stays `proposed`, and `POST /api/pulse/actions/{id}/approve` goes through the generic
    `deliver()`, which had no idea the action had mission authority behind it: propose, detach the
    session into another mission, approve, and `continue` landed in the new mission's session
    (#888 review, finding 1).
    """
    if str(rec.get("source") or "") != "supervisor":
        return None, None
    mission_id = str(rec.get("mission_id") or "")
    objective_key = str(rec.get("objective_key") or "")
    session_key = str(rec.get("session_id") or "")
    if not (mission_id and objective_key and session_key):
        return None, None
    # The incarnation the proposal was made against — and it is NOT optional.
    #
    # An earlier version skipped the episode check when the record lacked the field, so actions
    # minted before it existed would not be stranded. That trade is unsafe: dropping an objective
    # and re-adding the same key produces a fresh, unmet, not-stood-down objective, so every other
    # check passes and a stale proposal becomes deliverable against an incarnation it was never
    # minted for. Authority is not a thing to trade for compatibility.
    #
    # The incarnation identity is the supervisor BINDING, carried in the same atomic snapshot as
    # the objective state and compared inside the write fence. An action without one is refused.
    raw_ep = rec.get("objective_episode")
    episode = int(raw_ep) if isinstance(raw_ep, int) and not isinstance(raw_ep, bool) else None
    action_id = str(rec.get("id") or "")

    def _state():
        from . import missions

        # ONE snapshot, and it carries the BINDING. Reading the binding separately is an ABA
        # window — a drop-and-re-add between the two reads shows a live binding beside a
        # recreated objective that never belonged together — and a fingerprint without it cannot
        # see a re-add at all, because every other field comes back identical (#888 review).
        return missions.supervisor_authority(
            mission_id, objective_key, session_key=session_key, action_id=action_id
        )

    def _check() -> tuple[bool, str]:
        from . import missions

        try:
            # ONE atomic read, then one verdict. `supervisor_action_verdict` requires the binding
            # the snapshot carries: an episode NUMBER is not an identity, because a drop deletes
            # the lifecycle rows and a re-added key starts at episode 1 again — so an action
            # minted for the first incarnation compares equal to a different objective that
            # merely reuses the key. The binding is what distinguishes them.
            #
            # The record's own `objective_episode` is checked against the binding rather than
            # trusted: the record is authored by the party being gated.
            state = _state()
            if episode is not None:
                bound = state[4] if len(state) > 4 else None
                if bound is not None and int(episode) != int(bound):
                    return False, (
                        "this supervisor action's recorded episode does not match its binding"
                    )
            return missions.supervisor_action_verdict(mission_id, state)
        except Exception:  # noqa: BLE001
            # Unverifiable authority is not authority — same rule as the fence itself.
            return False, "the mission authority for this action could not be re-read"

    return _check, _state


def _compose_fingerprint(base: Callable[[], object], extra) -> Callable[[], object]:
    """`base` plus a caller's own state, as one value the write fence compares.

    Two fingerprints would need two comparisons at the fence and a rule for disagreement; one
    tuple needs neither. `extra` is read in the same call, under the same lock, so anything it
    covers is as binding as the policy half.
    """
    if extra is None:
        return base

    def _fp() -> object:
        return (base(), extra())

    return _fp


def _authority_fingerprint(session_id: str) -> Callable[[], object]:
    """The policy snapshot PLUS the shared mission state for this session (#871).

    The in-memory policy epoch is process-local, and this app supports several instances over one
    store — so an archive committed by a sibling instance never touches this interpreter's
    counter, and its own epoch reads unchanged right up to byte one. The missions store is the
    only thing both instances agree on, so the fingerprint reads it.

    Kept cheap and, critically, taking NO lock the write fence already holds: it is called from
    inside the registry lock, so everything it touches must be ordered after it — which is the
    order both `policy_transaction` and the archive fence take.
    """

    def _fp() -> object:
        # Imported HERE, not at module scope: `missions` opens sqlite, and this module is on the
        # import path of the input layer it must stay below.
        from . import missions

        barred = bool(session_id) and session_id in missions.sessions_barred_from_automation()
        # THE PER-SESSION OPT-OUT belongs here too. It is checked in `check_precondition`, which
        # runs before the fence — so a sibling instance flipping `orchestrator_excluded` after
        # that check still had its withdrawal land before byte one with nothing to catch it. It is
        # a sidecar read, shared by every instance, which is exactly what this fingerprint is for
        # (#888 review, finding 1).
        excluded = False
        if session_id:
            with contextlib.suppress(Exception):
                excluded = bool(
                    metadata.get(metadata.resolve_key(session_id)).orchestrator_excluded
                )
        return (*_policy_fingerprint(), barred, excluded)

    return _fp


def _settle_waiting(action_id: str, state: str, **fields) -> dict | None:
    """Settle an action that has NOT been claimed yet, atomically.

    Every early return in `deliver()` sits between a `ledger.get()` and the claim, so a blind
    `transition()` here can overwrite a claim another caller landed in that gap — recording
    `expired` or `failed` on top of a delivery that is already underway. CAS from the waiting
    states means the claimant wins and this quietly does nothing.
    """
    return ledger.compare_and_set(action_id, ledger.REJECTABLE_STATES, state, **fields)


def _master_confirmed_dead(phys: str) -> bool:
    """True only on PROOF the session's dtach master is gone: no socket at its path, or a probe
    verdict of ``DEAD`` (the connect was refused).

    Not `scrollback._session_alive`: that is a boolean view for eviction and attach-vs-launch,
    and it reads ``UNKNOWN`` — every connect timed out, which a live master on a starved host
    can do — as "not alive". Withdrawal is irreversible, so ``UNKNOWN``, an unresolvable key and
    any lookup or probe error all keep the decision (#975 review 4846). The same rule
    `ptybridge.unlink_if_stale` applies before its own destructive step.
    """
    try:
        prov, native = engines.parse_key(phys)
        sock = ptybridge.socket_path(prov.engine_id, native)
        if not sock.exists():
            return True
        return sock.is_socket() and ptybridge.probe_master(sock) == ptybridge.DEAD
    except Exception:  # noqa: BLE001 — doubt keeps the decision
        return False


def withdraw_undeliverable(path=None) -> list[str]:
    """Settle as ``stale`` every waiting action whose delivery can no longer succeed. Returns ids.

    A proposal used to stay on every decision surface for its whole TTL after its session moved
    on, offering an Approve `deliver` would refuse (#969). Withdrawal cannot be undone, so it
    takes only conditions that do not clear on their own:

    * **the session is dead** — no registered writer AND a master CONFIRMED gone
      (`_master_confirmed_dead`). A missing writer alone is not death: attach and detach hand the
      writer over (`SessionRegistry.on_attach` stops the headless one before the pane registers
      its own), and a poll landing in that gap must not erase the decision the operator is
      opening the pane to approve. Neither is a probe that timed out. Delivery stays fail-closed
      through the gap without any help from here.
    * **the screen moved** — `screen_matches`, the helper `check_precondition` uses, re-run on
      every read. There is deliberately no skip-replay shortcut: a sound one would have to track
      every input the renderer reads (ring bytes, width, height), and an unsound one keeps
      offering decisions delivery would refuse (#975 review). One replay per live candidate.

    Viewer state is deliberately not a reason: an attached viewer is transient, and that viewer may
    be the operator about to tap. Only :data:`CLAIMABLE_STATES` are candidates — ``claimed``
    belongs to a delivery in flight — and the write is a compare-and-set from those states, so a
    claim that lands first wins and this does nothing. Blocking (ring replay, plus a dtach socket
    probe for a session with no writer); call under ``asyncio.to_thread``.
    """
    moved: list[str] = []
    for rec in ledger.live_actions(path):
        if rec.get("state") not in CLAIMABLE_STATES:
            continue
        try:
            phys = engines.physical_key(str(rec.get("session_id") or ""))
        except Exception:  # noqa: BLE001, S112 — an unresolvable id is delivery's to refuse
            continue
        if not session_input.is_live(phys):
            if not _master_confirmed_dead(phys):
                continue  # a writer handoff, or a master that did not answer in time: keep it
            why = "session is not live"
        else:
            ok, why = screen_matches(phys, rec.get("precondition") or {})
            if ok:
                continue
        if ledger.compare_and_set(rec["id"], CLAIMABLE_STATES, "stale", path, detail=why):
            moved.append(rec["id"])
    return moved


def housekeep_pending(path=None) -> tuple[list[str], list[str]]:
    """Expire overdue actions, then withdraw undeliverable ones: ``(expired, withdrawn)``.

    ONE function for every place that retires waiting actions (#969) — the orchestrator state read
    the pane strip polls, the mission cards' overlay, and the scheduled sweep. Each used to call
    `expire_due` on its own, and a retirement rule added to only one of them would let two
    surfaces disagree about whether a decision is still pending. Blocking.
    """
    return ledger.expire_due(path=path), withdraw_undeliverable(path)


async def deliver(
    action_id: str,
    *,
    registry=None,
    authority=None,
    extra_fingerprint=None,
    operator_approval: bool = False,
) -> dict:
    """Deliver one ledger action. Returns the resulting ledger record.

    The state machine is the safety property, so the ordering matters: ``claimed`` is written
    and fsynced BEFORE any byte reaches the PTY. That is what makes a crash recoverable — the
    record proves a delivery was in flight even though it cannot prove the outcome.

    ``operator_approval`` is set by the approve route and by nothing else (#969). It reaches BOTH
    precondition callbacks below, and it narrows only the viewer check: an attached viewer stops
    counting as someone else at the keyboard, because the tap is the operator.
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

    sup_check, sup_state = _supervisor_authority(rec)
    mem_check, mem_state = _mission_membership_authority(rec)

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
        # …and the action's OWN mission authority, whoever is delivering it. Unlike `authority`
        # this is not a property of how the delivery was triggered — it is a property of the
        # action, so it applies to an operator's approve exactly as it applies to an automatic
        # send. The same state is folded into the in-fence fingerprint below, so a change after
        # this guard is caught too.
        if sup_check is not None:
            ok, why = sup_check()
            if not ok:
                return False, why
        # …and the action's OWN mission MEMBERSHIP, for every action that names both a mission and
        # a session — the relay included (#903 review, finding 1). The route's pre-append check
        # gives the operator an answer; this is the one that is correct, because the detach and
        # the re-adopt can both land in the window it opens.
        if mem_check is not None:
            ok, why = mem_check()
            if not ok:
                return False, why
        # THE MISSION FENCE, and it belongs HERE — in the guard every delivery passes through —
        # rather than in `authority`, which only the automatic path supplies. Placing it there
        # left the ordinary approval route (`POST /api/pulse/actions/{id}/approve` → `deliver()`)
        # with no mission check at all, so an operator tap could still type into a session whose
        # mission was being torn down. Reproduced on the previous head (review on #881).
        #
        # It is not "an extra permission this delivery rests on", which is what `authority` is
        # for. It is a property of the SESSION: an archiving mission has withdrawn it, and no
        # caller — automatic or human — may write to it. A tap is authority to send what the
        # operator approved; it is not authority to send it somewhere that no longer accepts it.
        #
        # Fails CLOSED, unlike the append-time fence: this is the last check before bytes reach a
        # real pty, and "I could not verify this is still authorized" must not deliver.
        sid = str(rec.get("session_id") or "")
        if sid:
            from . import missions

            try:
                barred = missions.sessions_barred_from_automation()
            except Exception:
                return False, "the mission store could not be read, so authority is unverifiable"
            if sid in barred:
                return False, "the session's mission is being archived; it accepts no writes"
        # The screen/viewer contract is "no viewer at the keyboard, and the screen still looks
        # like the one that was proposed against" — as of NOW, not as of setup.
        return check_precondition(rec, registry=registry, operator_approval=operator_approval)

    outcome = await asyncio.to_thread(
        session_input.send_input,
        phys,
        payload,
        # The SAME flag as `_final_guard` above: two callbacks that disagreed about who counts as
        # being at the keyboard would let one approve what the other refuses (#969).
        precondition=lambda: check_precondition(
            rec, registry=registry, operator_approval=operator_approval
        ),
        final_guard=_final_guard,
        # The third domain. `_final_guard` reads policy and then does the screen check, so a
        # flip between those two still slipped through — the guard's verdict is only as fresh
        # as the moment it ran. This is re-read INSIDE the fence, immediately before byte one,
        # so a withdrawal at any point up to the write refuses.
        # A caller may add its OWN state to the thing that is re-read inside the fence. That is
        # the only place an extra authority can be enforced rather than merely consulted: a
        # callback invoked from `_final_guard` runs before the registry and screen work, so a
        # change after it still reaches byte one (#888 review, finding 1).
        policy_fingerprint=_compose_fingerprint(
            _compose_fingerprint(
                _authority_fingerprint(str(rec.get("session_id") or "")),
                extra_fingerprint if extra_fingerprint is not None else sup_state,
            ),
            # Membership rides in the fingerprint as well as in the guard, for the reason every
            # other term does: the guard's verdict is only as fresh as the moment it ran, and the
            # re-adopt can land between it and byte one.
            mem_state,
        ),
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


async def deliver_auto(
    action: dict, *, registry=None, extra_authority=None, extra_fingerprint=None
) -> dict | None:
    """Deliver an action the pass already auto-approved (``yolo``). Returns the record, or
    ``None`` when the tier/ceiling says it must wait for a tap.

    The ceiling is re-read here rather than trusted from the pass: prefs can change between a
    proposal being minted and this running, and the safe direction is to re-ask.

    ``extra_authority`` composes a caller's OWN final check into the same fence, and exists because
    the prefs re-read is only half the question. A supervisor's action also rests on facts about
    the mission — that it still holds this session, that the objective is still unmet — which can
    change in exactly the same window and are invisible here. Giving the caller a seat at this
    fence is the difference between re-authorizing before the claim (where it is a hint) and
    re-authorizing at the write (where it is a guarantee): see `mission_supervisor._nudge_authority`
    (#888 review, findings 1 and 2).

    Composed AND, and the prefs checks run first, so a withdrawn tier short-circuits before any
    extra work. A refusal from either half is a refusal.
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
        if extra_authority is not None:
            return extra_authority()
        return True, ""

    return await deliver(
        action["id"],
        registry=registry,
        authority=_auto_authority,
        extra_fingerprint=extra_fingerprint,
    )


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
