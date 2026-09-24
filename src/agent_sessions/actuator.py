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
RENDERABLE_VERBS: frozenset[str] = frozenset(
    {"continue", "choose", "answer", "relay", prefs.DRAFT_DIRECTION_VERB}
)

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


class RenderStale(NotDeliverable):
    """A supervisor nudge whose text or provenance is no longer what was proposed (#983).

    Its own class because the outcome differs: the action was fine when proposed and the world
    moved, so it settles ``stale`` like a moved screen, not ``failed``.
    """


def default_nudge_text(cfg: dict) -> str:
    """The operator's global nudge, as `continue` types it. ONE expression for every caller.

    `mission_directions.render` uses it for an objective with no direction, so the fallback a
    supervisor proposal binds is byte-identical to what an ordinary `continue` sends.
    """
    return str(cfg.get("nudge_template") or prefs.DEFAULT_ORCH_NUDGE)[:NUDGE_MAX]


def _is_supervisor_nudge(action: dict) -> bool:
    return action.get("verb") == "continue" and str(action.get("source") or "") == "supervisor"


def _is_draft(action: dict) -> bool:
    """An AI-drafted direction (#983 P3): model text, typed only on the operator's approval."""
    return action.get("verb") == prefs.DRAFT_DIRECTION_VERB


def draft_auto_allowed(action: dict, cfg: dict) -> bool:
    """May THIS AI-drafted direction be typed with nobody reading it, under THIS policy? (#983 P4)

    The operator's approved grant, expressed once and asked everywhere: the opt-in is on, the tier
    is `yolo`, the master switch is on, the verb is in the allowed set, and the model's own
    reported confidence is at or above the operator's threshold. Every conjunct is independent —
    the threshold is an EXTRA gate, never a replacement for the switch or the verb ceiling.

    Two deliberate belts. The confidence must be a real number (`isinstance(True, int)` is True in
    Python, so a boolean is refused on type), and the threshold is floored at
    :data:`prefs.ORCH_AI_DIRECTION_CONF_LO` whatever the config says — a corrupted or hand-edited
    block cannot lower the bound below the one the operator actually approved.

    **This is not the whole fence.** It answers only "does policy permit this kind of send"; the
    objective's own authority, the episode's AI-text budget, the screen, the viewer and the archive
    fence are all asked separately, and all of them are re-asked inside the write fence.
    """
    if not _is_draft(action):
        return False
    if cfg.get("auto_ai_directions") is not True:
        return False
    if not cfg.get("enabled") or cfg.get("autonomy") != "yolo":
        return False
    if prefs.DRAFT_DIRECTION_VERB not in set(cfg.get("allowed_verbs") or ()):
        return False
    conf = action.get("confidence")
    # A REAL confidence, IN RANGE, or nothing. `isinstance(True, int)` is True in Python, so a
    # boolean is refused on type; `nan` and `±inf` fail this comparison on their own; and a value
    # ABOVE 1.0 is not a confidence either — a forged or hand-edited row must not be able to clear
    # the operator's threshold by overshooting it.
    if not isinstance(conf, int | float) or isinstance(conf, bool) or not (0.0 <= conf <= 1.0):
        return False
    floor = cfg.get("ai_direction_confidence_min")
    if not isinstance(floor, int | float) or isinstance(floor, bool):
        floor = prefs.ORCH_AI_DIRECTION_CONF_DEFAULT
    return float(conf) >= max(float(floor), prefs.ORCH_AI_DIRECTION_CONF_LO)


def _reserve_auto_slot(rec: dict) -> bool:
    """Take this objective episode's ONE autonomous AI-written send for `rec`. Blocking.

    Idempotent for an action that already holds it, exclusive against any other. Called from
    :func:`deliver` for every draft that is NOT an operator tap, so the bound belongs to the write
    boundary rather than to whichever caller happened to ask — the first version reserved in
    `mission_supervisor._maybe_auto_send`, which left a caller that reached `deliver` by another
    route free of it, and its send uncounted (#983 P4 review).

    **Fails closed.** A draft that names no mission, objective or episode has nothing to bound, and
    an unreadable store is not a free slot.
    """
    from . import missions

    mission_id = str(rec.get("mission_id") or "")
    objective_key = str(rec.get("objective_key") or "")
    raw = rec.get("objective_episode")
    episode = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
    if not (mission_id and objective_key and episode is not None):
        return False
    try:
        return missions.reserve_ai_direction(
            mission_id,
            objective_key=objective_key,
            episode=episode,
            action_id=str(rec.get("id") or ""),
            expect_incarnation=str(rec.get("objective_incarnation") or "") or None,
        )
    except Exception:  # noqa: BLE001 — an unverifiable allowance is not an allowance
        return False


def draft_text(action: dict) -> str:
    """The exact text an AI-drafted direction types: its stored draft, sanitized like `answer`.

    Raises :class:`NotDeliverable` for a draft with nothing typeable. The stored draft was sanitized
    when it was proposed, so this is the text the card showed.
    """
    try:
        return handoff.sanitize_seed(str(action.get("draft") or ""))
    except handoff.HandoffError:
        raise NotDeliverable("an AI-drafted direction with no usable text") from None


def supervisor_render(action: dict, cfg: dict, *, resolve_target: bool = True) -> dict:
    """Render a supervisor `continue` AGAIN and require it to be the one that was proposed (#983).

    The proposal persisted ``render = {text, source, facts, provenance, digest}``. This reads the
    objective as it is now, renders through the same function, and raises :class:`RenderStale`
    unless both the text and the provenance are unchanged — the default-nudge fallback included,
    so an edit to the global template stales a proposal exactly as a direction edit does.
    Blocking (one missions-store read).
    """
    from . import mission_directions, missions

    mission_id = str(action.get("mission_id") or "")
    objective_key = str(action.get("objective_key") or "")
    if not (mission_id and objective_key):
        raise RenderStale("this supervisor nudge names no objective")
    try:
        snapshot = missions.objective_snapshot(mission_id, objective_key)
    except Exception:  # noqa: BLE001 — unverifiable text is not verified text
        raise RenderStale(
            "the objective could not be re-read, so the text is unverifiable"
        ) from None
    try:
        fresh = mission_directions.render(snapshot, cfg)
    except mission_directions.NotRenderable as e:
        raise RenderStale(f"this nudge can no longer be filled: {e}") from None
    ok, why = mission_directions.matches(action.get("render"), fresh)
    if not ok:
        raise RenderStale(why)
    # …and the facts must come from the authority configured NOW, not merely the one the row was
    # bound to (#983 review): a forge-settings save or a moved checkout stales them at once rather
    # than at the next re-probe. `resolve_target=False` is the in-fence form (no `git`).
    try:
        ok, why = mission_directions.current_authority(
            snapshot, fresh, resolve_target=resolve_target
        )
    except Exception:  # noqa: BLE001 — unverifiable authority is not authority
        ok, why = False, "the authority behind this nudge's facts could not be re-read"
    if not ok:
        raise RenderStale(why)
    return fresh


#: What the decision row says when the verdict itself could not be reached. Fails closed: a row
#: that cannot say the text is still true offers no Send.
RENDER_STATUS_UNCHECKED = "whether this nudge's text is still true could not be checked"


def render_status(action: dict, cfg: dict) -> dict | None:
    """Would delivery still type this supervisor nudge's text? ``{sendable, reason}`` (#983 P2).

    ``None`` for anything that is not a supervisor ``continue`` carrying a persisted ``render``.

    A READ-ONLY PROJECTION for the decision row, and it is not a second opinion: it calls
    :func:`supervisor_render`, the very function :func:`render` calls before :func:`deliver`
    claims anything, and reports that function's verdict in its own words. Projection and delivery
    therefore cannot disagree about whether the text or its provenance moved — there is one
    comparison and both ask it. What this does NOT do is anything delivery does after that point:
    no claim, no fence, no settlement and no ledger write, so a proposal the row shows as not
    sendable is still settled only by an operator's Dismiss or by delivery's own refusal.

    The rest of delivery's gates (orchestration switched off, a moved screen, a viewer at the
    keyboard) are not text facts and are not projected here; they keep refusing at approve time.

    **NO GIT, ON PURPOSE.** This runs for every pending supervisor nudge on every `/api/pulse` and
    `/api/pulse/orchestrator` read, and every open client polls those. So it asks the in-fence form,
    ``resolve_target=False``: the text, the render identity (objective, facts, probe binding and
    arguments, the mission's `cwd` and `merge_sha`) and the forge-settings revision, all read from
    the store. It never touches the checkout. Resolving the target would read its `.git` (remote
    and refs) on every poll, and run `git status` for an objective with no configured branch. A
    blocking probe per poll is how typing went sluggish app-wide before. What that form cannot see
    is the agent-controlled half of the target: a
    checkout HEAD, branch or remote the agent moved with its own `git`. The projection is advisory,
    and Approve still runs the full check (:func:`render` → :func:`supervisor_render` with target
    resolution) before anything is claimed, so such a nudge reads as sendable here and settles
    `stale`, with its reason, when tapped. Blocking (store reads only); producers still call it off
    the event loop.
    """
    if not (_is_supervisor_nudge(action) and isinstance(action.get("render"), dict)):
        return None
    try:
        supervisor_render(action, cfg, resolve_target=False)
    except RenderStale as e:
        return {"sendable": False, "reason": str(e)}
    except Exception:  # noqa: BLE001 — an unanswerable question is not a yes
        return {"sendable": False, "reason": RENDER_STATUS_UNCHECKED}
    return {"sendable": True, "reason": ""}


def render(action: dict, cfg: dict) -> bytes:
    """The bytes for one action. Raises :class:`NotDeliverable` for anything else.

    Every branch here is server-authored except ``answer``, which is sanitised and only ever
    reached behind an explicit approval.
    """
    verb = action.get("verb")
    if verb == "continue":
        if _is_supervisor_nudge(action):
            # Operator text plus the objective's own checked facts, re-rendered and compared with
            # what was proposed. The model's `why` is not an input to any of it.
            text = supervisor_render(action, cfg)["text"]
        else:
            text = default_nudge_text(cfg)
        return session_input.bracketed_paste(text)
    if verb == "choose":
        opt = action.get("option")
        if not isinstance(opt, int) or isinstance(opt, bool):
            raise NotDeliverable("choose without a validated option")
        if not (orchestrator.OPTION_MIN <= opt <= orchestrator.OPTION_MAX):
            raise NotDeliverable("choose option out of range")
        # THE DIGIT ALONE for an operator's answer from the console to an engine whose menu is
        # proven to submit on the digit (#1060 Phase 3, `menu_answer.digit_submits`): the `\r`
        # would land after the answer, in the agent's next prompt. Gated on `origin` too, so a
        # model-built record can never opt into it.
        if action.get("submit") == "digit" and action.get("origin") == "operator":
            return f"{opt}".encode()
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
    if _is_draft(action):
        # AN AI-DRAFTED DIRECTION (#983 P3): model prose, rendered exactly like `answer` (sanitized,
        # one bracketed paste). What keeps it safe is not the payload rule but WHO may deliver it:
        # `deliver` refuses it unless the operator approved it.
        return session_input.bracketed_paste(draft_text(action))
    raise NotDeliverable(f"verb {verb!r} is not deliverable")


def _viewer_busy(phys_key: str, registry, *, operator_approval: bool = False) -> bool:
    """True when a browser is attached, or was producing output very recently. Best-effort: a
    registry hiccup must not silently *enable* a write, so an error reads as busy.

    ``operator_approval`` (#969) drops ONLY the attached half. The rule keeps the orchestrator off
    the keyboard while the operator is at it — but an explicit approval IS the operator. The
    attached pane is most often the operator's own: the mission console offers **Open session**
    beside Approve, and the bell and push deep-link every decision to the session, so an operator
    who looked before deciding leaves a viewer attached (the pane's own decision strip, #948 P3,
    was removed in #1049). Refusing that tap as "someone else at the keyboard" would refuse the
    operator for having read the screen. Recent output still refuses (typing echoes, so it covers
    someone typing too), and an error still reads as busy.

    This does NOT cover the screen half: a viewer attaching at another width makes the agent
    repaint, and `screen_matches` then refuses the approval as stale. That is #973's, pinned as an
    expected failure in `tests/test_console_approve_after_viewing.py`.
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
    # THE SAME WINDOW `precondition_for` judged from (#1060): a narrower read here could cut a
    # menu's title that the proposal saw, classify the unchanged frame differently, and refuse a
    # delivery as "a different kind of prompt now" when nothing moved.
    screen = scrollback.live_tail_text(phys, orchestrator.PROMPT_SCREEN_CHARS)
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
        # THE AUTONOMOUS-AI-DIRECTION GRANT (#983 P4), and this is where its withdrawal actually
        # bites. `_final_guard` asks `draft_auto_allowed` too, but a guard's verdict is only as
        # fresh as the moment it ran — the operator can clear the toggle, or leave yolo, in the
        # gap between the guard returning and byte one. This tuple is re-read inside that gap,
        # under the registry lock, so a withdrawal anywhere up to the write refuses the payload.
        cfg.get("auto_ai_directions") is True,
        float(cfg.get("ai_direction_confidence_min") or 0),
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
    # THE INCARNATION, for an action that was bound to one (#983 P3). A draft MUST carry it. The
    # binding already refuses a drop-and-re-add, but a binding for the NEW objective can be reserved
    # under the old action's key (a re-add landing between a draft's snapshot and its reservation).
    # The incarnation is the identity that cannot come back.
    want_incarnation = rec.get("objective_incarnation")
    needs_incarnation = _is_draft(rec) or want_incarnation is not None

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
            if needs_incarnation:
                have = state[6] if len(state) > 6 else None
                if not (isinstance(want_incarnation, str) and want_incarnation) or (
                    have != want_incarnation
                ):
                    return False, (
                        "the objective this was written for was removed and re-created, so it "
                        "no longer applies"
                    )
            return missions.supervisor_action_verdict(mission_id, state)
        except Exception:  # noqa: BLE001
            # Unverifiable authority is not authority — same rule as the fence itself.
            return False, "the mission authority for this action could not be re-read"

    return _check, _state


def _draft_direction_authority(rec: dict, operator_approval: bool):
    """`(check, fingerprint)` for an AUTONOMOUS AI-drafted direction, or `(None, None)` (#983 P4).

    **"Operator text always wins" has to win at the write fence, not only at the guard.** Until
    this existed, `has_direction` was asked once — in `mission_supervisor._maybe_auto_send`'s
    authority callback — and nothing re-read it before byte one. The fallback fingerprint is the
    supervisor state tuple (state, incarnation, episode, binding), which does not carry the
    direction, and the render fingerprint covers a `continue` rather than a draft. So a
    `set_direction` committed by a SIBLING INSTANCE after the guard ran still lost the race: this
    process's policy epoch never moves for a write another process made, and the AI's words landed
    in a session the operator had just written their own direction for.

    DERIVED FROM THE RECORD, at the common autonomous-draft boundary, so every path that can type a
    draft without a tap inherits it — including a direct `deliver_auto` call that passes no
    callbacks of its own. Fails CLOSED: an objective that cannot be re-read is not an objective
    without a direction.
    """
    if not _is_draft(rec) or operator_approval:
        return None, None
    mission_id = str(rec.get("mission_id") or "")
    objective_key = str(rec.get("objective_key") or "")
    if not (mission_id and objective_key):
        return None, None

    def _snapshot():
        from . import missions

        return missions.objective_snapshot(mission_id, objective_key)

    def _state() -> object:
        from . import mission_directions

        try:
            snap = _snapshot()
        except Exception:  # noqa: BLE001
            return object()  # unreadable compares equal to nothing, so the fence refuses
        if snap is None:
            return object()
        return (
            bool(mission_directions.has_direction(snap)),
            str(snap.get("incarnation") or ""),
            int(snap.get("episode") or 0),
        )

    def _check() -> tuple[bool, str]:
        from . import mission_directions

        try:
            snap = _snapshot()
        except Exception:  # noqa: BLE001
            return False, "the objective could not be re-read before the write"
        if snap is None:
            return False, "the objective was dropped before the write"
        if mission_directions.has_direction(snap):
            return False, "the objective gained your own direction before the write"
        return True, ""

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


def _render_authority(rec: dict):
    """`(check, fingerprint)` for a supervisor `continue`, or `(None, None)` (#983).

    DERIVED FROM THE RECORD, like `_supervisor_authority`, so the approve route and the automatic
    path both get it without asking. `check` is the guard's verdict; `fingerprint` is what the
    fence re-reads immediately before byte one. The fingerprint is the persisted digest while the
    re-render still matches the proposal, and a fresh unequal sentinel otherwise — so a mismatch at
    either end of the fence can never compare equal, even if the state later changes back.
    """
    if not _is_supervisor_nudge(rec):
        return None, None

    def _check() -> tuple[bool, str]:
        try:
            supervisor_render(rec, prefs.get_orchestrator())
        except NotDeliverable as e:
            return False, str(e)
        return True, ""

    def _state() -> object:
        try:
            # Read INSIDE the fence, so no `git`: the forge revision is compared here, and the
            # resolved checkout target in `_check` above (see `current_authority`). Probe writes and
            # forge saves take the same fence (`session_input.fact_transaction`), so none of what
            # this reads can commit between it and byte one.
            supervisor_render(rec, prefs.get_orchestrator(), resolve_target=False)
        except NotDeliverable:
            return object()
        return str((rec.get("render") or {}).get("digest") or "")

    return _check, _state


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
      opening the pane to look at before approving. Neither is a probe that timed out. Delivery
      stays fail-closed through the gap without any help from here.
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
    evidence = None  # computed once, only if a standalone candidate exists
    held: set[str] | None = None
    for rec in ledger.live_actions(path):
        if rec.get("state") not in ledger.OPERATOR_PENDING_STATES:
            continue
        # NO SURFACE LEFT (#1086 review 5184). A standalone decision is settled on the Ask page,
        # which lists only sessions that are in scope, not archived and not review-excluded. One
        # whose session stopped being listed after it was proposed can no longer be acted on
        # anywhere — and would still count in the badge. Withdrawn, with the reason — but ONLY on
        # facts the OPERATOR RECORDED (review 5188): the sidecar's `archived` / `review_excluded`
        # flags, or the action's own proposal-time `cwd` now outside roots + `folder_exclusions`.
        # Never on a scan's absence: scans fail soft, and one transient read error must not
        # durably withdraw a valid decision. Unreadable anything → no evidence → keep it. A held
        # session's decision belongs to its mission console; an unreadable membership store
        # withdraws nothing. Unarchiving later means a NEW pass may propose again.
        if not rec.get("mission_id"):
            if held is None:
                held = _held_sessions()
            sid = str(rec.get("session_id") or "")
            if held is not None and sid not in held:
                if evidence is None:
                    evidence = _surface_evidence()
                if evidence is not None and _no_surface(rec, *evidence):
                    # Every state that COUNTS (escalations included), never `claimed`.
                    if ledger.compare_and_set(
                        rec["id"],
                        ledger.OPERATOR_PENDING_STATES - {"claimed"},
                        "stale",
                        path,
                        detail=NO_SURFACE_DETAIL,
                    ):
                        moved.append(rec["id"])
                    continue
        # The live-screen rules below are about DELIVERY, so only claimable actions are theirs.
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


NO_SURFACE_DETAIL = "its session is no longer listed (archived, excluded or out of scope)"


def _held_sessions() -> set[str] | None:
    """Sessions an open mission holds, or ``None`` when the store cannot be read."""
    try:
        from . import missions

        return set(missions.all_active_memberships())
    except Exception:  # noqa: BLE001
        return None


def _surface_evidence():
    """``(metadata index, aliases, in_scope)`` for :func:`_no_surface`, or ``None``. Blocking.

    All three fail toward "no evidence": `metadata.load` answers ``{}`` for an unreadable sidecar
    (no flags → nothing withdrawn), and a prefs read that fails returns no exclusions."""
    try:
        from .routes.sessions import _hard_scope_filter

        return metadata.load(), metadata.load_aliases(), _hard_scope_filter(honour_curation=False)
    except Exception:  # noqa: BLE001
        return None


def _no_surface(rec: dict, meta_index: dict, aliases: dict, in_scope) -> bool:
    """True only on RECORDED evidence that the Ask page cannot list this action's session."""
    sid = str(rec.get("session_id") or "")
    try:
        phys = engines.physical_key(sid, aliases)
    except Exception:  # noqa: BLE001
        phys = sid
    m = meta_index.get(sid) or meta_index.get(phys)
    if m is not None and (m.archived is True or m.review_excluded):
        return True
    cwd = rec.get("cwd")
    return isinstance(cwd, str) and bool(cwd) and not in_scope(cwd, {"kind": ""})


def housekeep_pending(path=None) -> tuple[list[str], list[str]]:
    """Expire overdue actions, then withdraw undeliverable ones: ``(expired, withdrawn)``.

    ONE function for every place that retires waiting actions (#969) — the orchestrator state read
    (`GET /api/pulse/orchestrator`), the mission cards' overlay, and the scheduled sweep. Each
    used to call `expire_due` on its own, and a retirement rule added to only one of them would
    let two surfaces disagree about whether a decision is still pending. Blocking.
    """
    return ledger.expire_due(path=path), withdraw_undeliverable(path)


async def deliver(
    action_id: str,
    *,
    registry=None,
    authority=None,
    extra_fingerprint=None,
    operator_approval: bool = False,
    edit: dict | None = None,
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
    # An operator's EDIT (#1086): never written to the pending proposal, only carried here — the
    # bytes are rendered from this view and the claim below writes the same fields atomically.
    if edit is not None and not operator_approval:
        raise NotDeliverable("an edit is delivered only on the operator's own approval")
    read_ts = rec.get("ts")
    view = {**rec, **edit} if edit else rec

    exp = rec.get("expires_at")
    if isinstance(exp, int | float) and time.time() >= exp:
        return _settle_waiting(action_id, "expired") or rec

    cfg = prefs.get_orchestrator()
    # APPROVE-ONLY, UNLESS THE OPERATOR OPTED IN (#983 P3, widened in P4). An AI-drafted direction
    # is model-authored text, so the operator's tap on the approve route is normally the only
    # authority to type it. The one exception is the grant they gave explicitly: with
    # `auto_ai_directions` on, in yolo, at or above their threshold, the automatic path may send it
    # — and `deliver_auto` has already asked the same question before it got here.
    #
    # Refused before anything is settled, so a proposal nobody may auto-send stays exactly where it
    # was for the operator to decide.
    if _is_draft(rec) and not operator_approval and not draft_auto_allowed(rec, cfg):
        raise NotDeliverable(
            "an AI-drafted direction is sent only when you approve it, unless you have turned "
            "on AI-written directions"
        )
    if _is_draft(rec) and not operator_approval:
        # THE EPISODE'S ONE AUTONOMOUS SEND, TAKEN HERE (#983 P4 review). At the write boundary,
        # so the bound is a property of delivering an unreviewed direction rather than of the one
        # caller that remembers to ask for it. Idempotent for an action that reserved it upstream;
        # an episode whose slot another action holds settles `stale` and types nothing.
        if not await asyncio.to_thread(_reserve_auto_slot, rec):
            return (
                _settle_waiting(
                    action_id,
                    "stale",
                    detail="this objective episode's one AI-written direction is already spent",
                )
                or rec
            )
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
        # Off the loop: a supervisor nudge re-reads its objective from the missions store.
        payload = await asyncio.to_thread(render, view, cfg)
    except RenderStale as e:
        return _settle_waiting(action_id, "stale", detail=str(e)) or rec
    except NotDeliverable as e:
        return _settle_waiting(action_id, "failed", detail=str(e)) or rec

    phys = engines.physical_key(rec["session_id"])
    if not session_input.is_live(phys):
        return _settle_waiting(action_id, "failed", detail="session is not live") or rec

    # Claim BEFORE writing, and ATOMICALLY. A read-then-write across two lock holds lets two
    # callers both see `proposed` and both write — a duplicate `choose` answers a prompt twice.
    # …and against the REVISION that was read and rendered (#1086 review 5184): a record that moved
    # since — another claim, another edit, a state change — makes this view stale, so it types
    # nothing. The edit's fields ride the claim itself.
    if ledger.claim(action_id, CLAIMABLE_STATES, expect_ts=read_ts, **(edit or {})) is None:
        raise NotDeliverable("another caller claimed or changed this action first")

    sup_check, sup_state = _supervisor_authority(rec)
    mem_check, mem_state = _mission_membership_authority(rec)
    txt_check, txt_state = _render_authority(rec)
    dir_check, dir_state = _draft_direction_authority(rec, operator_approval)

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
        # …and a supervisor nudge's TEXT, rendered again from the objective as it is now (#983). An
        # edited direction, an edited global nudge, a moved head or a new observation since the
        # proposal all refuse here. The same comparison rides the in-fence fingerprint below, so a
        # change after this line is caught before byte one too.
        if txt_check is not None:
            ok, why = txt_check()
            if not ok:
                return False, why
        # …and, for an autonomous AI-drafted direction, that the operator has not meanwhile written
        # their OWN direction for this objective (#983 P4 review). The same fact rides the in-fence
        # fingerprint below, so a sibling instance committing one after this line still refuses.
        if dir_check is not None:
            ok, why = dir_check()
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
                _compose_fingerprint(
                    _compose_fingerprint(
                        _authority_fingerprint(str(rec.get("session_id") or "")),
                        extra_fingerprint if extra_fingerprint is not None else sup_state,
                    ),
                    # Membership rides in the fingerprint as well as in the guard, for the
                    # reason every other term does: the guard's verdict is only as fresh as
                    # the moment it ran, and the re-adopt can land between it and byte one.
                    mem_state,
                ),
                # …and so does a supervisor nudge's render digest (#983): a direction,
                # template, head or observation change after `_final_guard` evaluated is
                # refused before byte one.
                txt_state,
            ),
            # …and an autonomous draft's OPERATOR-DIRECTION eligibility (#983 P4 review). Read
            # from the shared store, the only thing a sibling instance and this one agree on:
            # its `set_direction` never moves this process's epoch, so nothing else sees it.
            dir_state,
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
    # THE DELIVERED SNAPSHOT (#983). A supervisor nudge types only a text equal to its persisted
    # `render`, so that record already is what was typed; the settlement names it explicitly, and
    # nothing later rewrites either. Later direction or template edits change neither.
    # HOW IT WAS SENT, recorded ON THE ACTION at delivery (#983 P4). Everything downstream reads
    # this recorded fact and never the live policy: the one-per-episode AI-text budget, the thread's
    # label, and the bell. The tier and the toggle can both change a second after the bytes land,
    # so a send classified by re-reading them would be reclassified by the operator's next tap —
    # and the budget that limits autonomous sends would reset itself.
    #
    # DERIVED FROM WHAT MADE IT DELIVERABLE, not from how it happened to be triggered.
    #
    # For a DRAFT there are exactly two grounds, and they are mutually exclusive: the operator's
    # tap, or `draft_auto_allowed` — the guard above admits nothing else. So anything that is not a
    # tap got here on the grant and is autonomous, whoever called us. Keying this on `authority`
    # instead would mislabel a future caller that delivered a draft without passing one (a retry, a
    # sweep, a route added later): the thread row would read as operator-sent and the AI-text
    # budget would go uncharged, which is the one accounting error that hands an episode a second
    # unreviewed write. No such caller exists today; this makes the record correct if one appears.
    #
    # For every other verb `authority` remains the honest signal — it is supplied by `deliver_auto`
    # and by nothing else.
    if _is_draft(rec):
        sent_by = "operator" if operator_approval else "auto"
    else:
        sent_by = "auto" if authority is not None else "operator"
    snapshot = rec.get("render") if (state == "delivered" and _is_supervisor_nudge(rec)) else None
    if state == "delivered" and _is_draft(rec):
        # …and a delivered DRAFT keeps the text that was typed the same way (#983 P3): the stored
        # draft, sanitized, which is what `render` turned into the payload. P4 splits the source in
        # two, because "the AI wrote this and you sent it" and "the AI wrote this and it was sent
        # with nobody reading it" are different things to tell an operator.
        with contextlib.suppress(NotDeliverable):
            snapshot = {
                "text": draft_text(rec),
                "source": "ai_auto" if sent_by == "auto" else "ai_draft",
                "digest": None,
            }
    delivered: dict = {}
    if state == "delivered":
        delivered["sent_by"] = sent_by
    if isinstance(snapshot, dict):
        delivered["delivered_text"] = snapshot.get("text")
        delivered["delivered_digest"] = snapshot.get("digest")
    # CAS strictly from `claimed`: we hold the claim, so any other state means something
    # else settled this action while we were writing and its verdict must stand.
    settled = ledger.compare_and_set(
        action_id,
        frozenset({"claimed"}),
        state,
        detail=outcome.detail,
        outcome=outcome.state,
        **delivered,
    )
    if isinstance(snapshot, dict) and settled is not None:
        # The thread's record of what was typed. Best-effort HERE because the settled ledger row
        # above is the durable record and a store hiccup must not undo a delivery — but not lost:
        # `reconcile_delivered_nudges` writes any missing one from that ledger row, on every
        # supervisor sweep and at boot (#983 review).
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_record_delivered_nudge, rec, snapshot)
        # …and the bell, for an AI-written direction that was sent with nobody reading it (#983 P4).
        # Same posture and the same repair path as the thread row above: best-effort here, restored
        # from the settled ledger row by the sweep, and idempotent on the action id so the repair
        # cannot announce it twice. Driven by the SETTLED row, so it announces what was recorded.
        if sent_by == "auto" and _is_draft(rec):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_announce_auto_direction, settled)
    return settled or rec


def _auto_direction_title(rec: dict) -> str:
    """What the bell says about an autonomously sent AI-written direction (#983 P4).

    Server-authored, and deliberately NOT the draft itself: the text is model prose and the bell
    carries no session content. The thread row holds the words; this says an unreviewed send
    happened, and which objective it was about, so the operator knows where to look.
    """
    key = str(rec.get("objective_key") or "").strip()
    return (
        f"An AI-written direction was sent for “{key}”"
        if key
        else "An AI-written direction was sent"
    )


def _ensure_receipt(action_id: str, mission_id: str = "") -> bool:
    """Make this announcement's durable receipt exist. True only if it now does (#983 P4).

    The write is idempotent, so an identity that already has a receipt answers True without a
    second row — which is exactly right for the caller's question, "is it safe to forget this".
    Fails CLOSED: an unwritable store answers False and the identity is kept.
    """
    from . import missions

    try:
        missions.record_auto_announcement(action_id, mission_id)
        return True
    except Exception:  # noqa: BLE001 — an unverifiable receipt is not a durable one
        return False


def _announce_auto_direction(rec: dict, path=None) -> None:
    """Announce ONE autonomously sent AI-written direction. Idempotent on the action id (#983 P4).

    This mode types text nobody read into a permission-bypassed agent, so every send is announced —
    it is the operator's only live signal that it happened. Its own announcing class, never
    `escalation`: an escalation means "you need to decide something", and this is the opposite
    report ("this was done on your behalf"), so enrolling it in the decision badge would say the
    wrong thing. Blocking; call off the loop.
    """
    from . import missions, notifications

    if not _is_draft(rec) or str(rec.get("sent_by") or "") != "auto":
        return
    action_id = str(rec.get("id") or "")
    mission_id = str(rec.get("mission_id") or "")
    if not action_id:
        return
    # THE DURABLE RECEIPT IS THE AUTHORITY; THE TOMBSTONE COVERS THE WINDOW IT CANNOT.
    #
    # `notifications.add` reads both inside its own lock, writes the row and the tombstone in one
    # document, and runs `after` there too — so this call is the whole announcement, with nothing
    # for a caller to sequence or forget. `recorded` is the unbounded record the compaction pin
    # also reads; the tombstone only has to bridge the gap before it lands, which is why its
    # eviction is not load-bearing. See `notifications.add` and `_remember`.
    notifications.add(
        title=_auto_direction_title(rec),
        project=str(rec.get("project") or ""),
        reason="sent automatically by mission control, without review",
        session_id=str(rec.get("session_id") or ""),
        engine=str(rec.get("engine") or ""),
        action_id=action_id,
        auto_direction=True,
        recorded=lambda: missions.auto_announcement_recorded(action_id),
        after=lambda: missions.record_auto_announcement(action_id, mission_id),
        # CONVERGE-THEN-RECLAIM. The bell asks this before dropping an identity from its dedupe
        # list, and keeps the identity when it answers False — so nothing is ever reclaimed whose
        # receipt is not durable. The mission is carried for the action being announced now; an
        # older candidate is written without one, which costs nothing, because that column is
        # provenance and every lookup keys on the action id alone.
        record=lambda aid: _ensure_receipt(aid, mission_id if aid == action_id else ""),
        path=path,
    )


def _record_delivered_nudge(rec: dict, snapshot: dict, *, at: float | None = None) -> bool:
    """Put a delivered supervisor nudge on its mission's thread, with the text that was typed.

    Stage `delivered`, so a `held` event already written for the same action (a Suggest proposal
    records one) cannot suppress it, nor it the held one.
    """
    from . import missions

    return missions.ensure_delivered_nudge_event(
        rec,
        text=str(snapshot.get("text") or ""),
        source=snapshot.get("source"),
        digest=snapshot.get("digest"),
        # The two facts that make the row say "AI-written · sent automatically" (#983 P4). `ai_auto`
        # is minted from the RECORDED `sent_by`, so the repair path reconstructs the same row.
        auto=snapshot.get("source") == "ai_auto",
        confidence=rec.get("confidence") if _is_draft(rec) else None,
        at=at,
    )


def reconcile_delivered_nudges() -> int:
    """Write the missing thread record of every DELIVERED supervisor nudge. Returns how many.

    The delivery settles the ledger first and writes the thread event after it, best-effort, so a
    store failure between the two left the operator's thread without the text that was typed —
    for ever (#983 review). This repairs that from the one durable record of what was typed: the
    ledger row's `delivered_text`, written by the settling compare-and-set.

    **It never types and never renders.** Its only input is the settled ledger row, so there is no
    path from here to a PTY or to the objective's current state. Idempotent: the event write is
    deduplicated on `(mission, action, stage='delivered')` inside its own transaction, so running
    it twice — or on two instances at once — writes one record. Blocking; call off the loop.
    """
    from . import missions

    status, latest = ledger.latest_by_id_checked()
    if status != "ok":
        return 0
    # The same record-level reconciliation compaction runs over the rows it is about to delete.
    written = int(missions.reconcile_delivered_records(latest.values())["written"])
    # …AND THE BELL, from the same rows on the same sweep (#983 P4). The announcement is written
    # after the settlement and is best-effort, so a failure in that gap would otherwise leave an
    # unreviewed send with no live signal at all — which is the one thing this mode owes the
    # operator. THE SAME repair path, deliberately: a second one would be a second place for the
    # rule to drift. It never types and never re-sends — its only input is a row already settled
    # `delivered` — and `notifications.add` is idempotent on the action id within this class, so
    # running it on every sweep announces nothing twice.
    for r in latest.values():
        if r.get("state") == "delivered" and _is_draft(r) and str(r.get("sent_by") or "") == "auto":
            with contextlib.suppress(Exception):
                _announce_auto_direction(r)
    return written


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
    # AN AI-DRAFTED DIRECTION IS REFUSED UNLESS THE OPERATOR OPTED IN (#983 P3, widened in P4), and
    # it is asked as its own question rather than left to the ceiling below. The ceiling is a SET,
    # and a set is the kind of thing a later edit widens by accident; `draft_auto_allowed` is the
    # whole approved grant in one expression — off by default, yolo only, master switch on, verb in
    # the allowed set, at or above the operator's threshold. With the pref off this is exactly the
    # P3 refusal: no tier, no confidence and no hand-edited prefs file can send model prose.
    if _is_draft(action) and not draft_auto_allowed(action, cfg):
        return None
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
        if _is_draft(action) and not draft_auto_allowed(action, live):
            return False, "autonomous AI-written directions were switched off before the write"
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
