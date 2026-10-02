"""Answer a session's numbered menu from the console (#1060 Phase 3).

An escalation raised at an engine's own menu carries that menu, parsed server-side by
`screen_menus` (`observed_prompt.menu`). The card draws one button per option. A tap lands here,
and this module turns it into an ordinary operator-approved `choose` delivered through
`actuator.deliver`, the same door every other byte to a PTY goes through: `is_live` at the write
boundary, an atomic claim before any byte, the viewer check, the mission fence, and the screen
precondition re-verified immediately before the first byte.

**What a tap may send is decided by the SERVER'S parse of the LIVE screen, never by the card.**
The request names an option number and the label the operator saw. Both must match the menu the
escalation recorded, and then both must match the menu parsed from the screen as it is now. A
prompt that moved, was answered in the terminal, or renumbered its options is refused with
nothing sent. The label is compared as the binding between what the operator read and what the
digit means. It is never sent: the payload is the digit alone.

**The precondition is the fingerprint of that exact frame.** It is not a second read, so the
screen whose labels were verified is the screen `deliver` requires, byte for byte, before the
first byte goes out.

**Digit only, for an engine whose captured rendering proves a digit submits** (claude: verified
in a real session on 2026-09-23, recorded on #1060). The `\\r` the model-authored `choose` sends
would otherwise arrive after the answer, in the agent's next prompt. Other engines have no menu
parser, so they never reach here.

**Ordering is fail-closed**, as in `/relay`. The escalation is closed FIRST, by compare-and-set,
so two taps (or a tap and a dismiss) cannot both win. The `choose` is appended only after that.
Every zero-byte refusal after the append settles the `choose` too, so an `approved` record can
never be left behind for a later delivery nobody asked for.

**Closed is not answered until a byte is typed** (#1082 review). Every outcome that sent nothing —
a refusal at the write boundary, a session no longer live, a write that failed before its first
byte, a `choose` that could not be recorded — REOPENS the escalation, so the operator's question is
still there to answer. Success is `delivered` and nothing else. A partial write is the one outcome
that keeps it closed: bytes may be on the PTY, and inviting a second keypress is the worse error.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

from . import (
    actuator,
    automation,
    engines,
    mission_fence,
    orchestrator,
    permission_holds,
    permission_prompts,
    screen_menus,
    scrollback,
)
from . import orchestrator_ledger as ledger


def digit_submits(engine: str) -> bool:
    """Does this engine's captured menu rendering prove the digit alone submits? The manifest's
    `terminal.menu_digit_submits` (#853 P3)."""
    term = engines.terminal_of(engine)
    return term is not None and term.menu_digit_submits


#: What the escalation becomes once answered: terminal, with a reason that names the answer.
ANSWERED_OUTCOME = "answered_by_operator"


class Refused(Exception):
    """Nothing was sent. ``status`` is the HTTP status the route should answer with."""

    def __init__(self, detail: str, status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class Indeterminate(Exception):
    """The delivery may or may not have reached the PTY. Never retry blindly."""

    def __init__(self, detail: str, action_id: str):
        super().__init__(detail)
        self.detail = detail
        self.action_id = action_id


def _option(menu: object, n: int) -> dict | None:
    if not isinstance(menu, dict):
        return None
    for o in menu.get("options") or []:
        if isinstance(o, dict) and o.get("n") == n:
            return o
    return None


def _live_frame(session_id: str) -> tuple[str, dict | None]:
    """The screen now, and the engine's menu parsed from it. Blocking (ring replay)."""
    phys = mission_fence.physical_of(session_id)
    try:
        screen = scrollback.live_tail_text(phys, orchestrator.PROMPT_SCREEN_CHARS)
    except Exception:  # noqa: BLE001 — an unreadable ring is "no menu", which refuses
        screen = ""
    return screen, screen_menus.parse(screen, screen_menus.engine_of(session_id))


def prepare(escalation_id: str, option: object, label: object) -> tuple[dict, dict]:
    """Validate a tap against the recorded AND the live menu. Blocking.

    Returns ``(escalation, choose_record)`` with the record NOT yet written, or raises
    :class:`Refused`. Nothing is written here.
    """
    if not isinstance(option, int) or isinstance(option, bool):
        raise Refused("option must be a whole number", 422)
    if not (orchestrator.OPTION_MIN <= option <= orchestrator.OPTION_MAX):
        raise Refused("option is out of range", 422)
    if not isinstance(label, str) or not label.strip() or len(label) > screen_menus.LABEL_MAX:
        raise Refused("label must be the option text the card showed", 422)

    status, esc = ledger.lookup(escalation_id)
    if status == "unreadable":
        raise Refused("nothing was sent: the action ledger could not be read", 503)
    if status != "found" or esc is None:
        raise Refused("unknown decision", 404)
    if esc.get("verb") != "escalate" or esc.get("state") not in ledger.ESCALATION_STATES:
        raise Refused(f"nothing was sent: this decision is already {esc.get('state') or 'settled'}")
    expires = esc.get("expires_at")
    if isinstance(expires, int | float) and time.time() > expires:
        raise Refused("nothing was sent: this decision has expired")
    valid, why = automation.check(esc)
    if not valid:
        raise Refused(f"nothing was sent: {why}")

    observed = esc.get("observed_prompt") or {}
    if isinstance(observed.get("permission"), dict):
        return _prepare_permission(escalation_id, esc, observed["permission"], option, label)

    recorded = _option(observed.get("menu"), option)
    if recorded is None or recorded.get("label") != label:
        raise Refused("nothing was sent: that option was not on the menu this card showed")

    sid = str(esc.get("session_id") or "")
    screen, live = _live_frame(sid)
    now = _option(live, option)
    if live is None or now is None:
        raise Refused(
            "nothing was sent: the session is no longer at that menu — open it to see what it "
            "is showing now"
        )
    if now.get("label") != label:
        raise Refused(
            f"nothing was sent: option {option} now reads {now.get('label')!r}, not {label!r}"
        )

    engine = screen_menus.engine_of(sid)
    rec = {
        "id": f"choose_{uuid.uuid4().hex}",
        "verb": "choose",
        "option": option,
        # What the operator chose, for the timeline and the card. Never rendered into bytes.
        "label": label,
        "session_id": sid,
        "state": "approved",
        "confidence": 1.0,
        "origin": "operator",
        "answers": escalation_id,
        "authority": esc["authority"],
        "expires_at": time.time() + 120,
        # THE FRAME WHOSE LABELS WERE JUST VERIFIED, not a second read of the screen.
        "precondition": {
            "key": esc["authority"]["physical_key"],
            "screen_fingerprint": orchestrator._screen_fingerprint(screen),
            "prompt_class": orchestrator._prompt_class(screen),
            "observed_at": time.time(),
        },
    }
    if digit_submits(engine):
        rec["submit"] = "digit"
    for k in ("mission_id", "engine", "title", "project", "project_id"):
        if esc.get(k) is not None:
            rec[k] = esc[k]
    return esc, rec


def _live_permission(session_id: str) -> tuple[str, dict | None]:
    """The screen now, and the permission dialog parsed from it — text and colours from ONE read,
    so the cursor and the words describe the same frame. Blocking."""
    phys = mission_fence.physical_of(session_id)
    try:
        screen, cells = scrollback.live_tail_frame(phys, orchestrator.PROMPT_SCREEN_CHARS)
    except Exception:  # noqa: BLE001 — an unreadable ring is "no dialog", which refuses
        screen, cells = "", None
    return screen, permission_prompts.parse(screen, screen_menus.engine_of(session_id), cells)


def _prepare_permission(
    escalation_id: str, esc: dict, recorded: dict, option: int, label: str
) -> tuple[dict, dict]:
    """A tap on a TOOL-PERMISSION dialog (#1213). Same contract as a menu, stricter binding.

    The label alone is not enough here: "Allow once" reads the same over ``git log`` and over
    ``rm -rf``. So the LIVE dialog must equal the RECORDED one field for field — heading, tool
    line, command, question, every option — and the choose record pins a digest of it INCLUDING
    the cursor, re-checked inside the first-byte fence (`actuator.screen_matches`). The keys are
    built later by `actuator.render` from the kind's recipe, this option and that cursor. Blocking.
    """
    rec_opt = _option(recorded, option)
    if rec_opt is None or rec_opt.get("label") != label:
        raise Refused("nothing was sent: that option was not on the prompt this card showed")
    sid = str(esc.get("session_id") or "")
    screen, live = _live_permission(sid)
    if live is None:
        raise Refused(
            "nothing was sent: the session is no longer showing this permission prompt — open "
            "it to see what it shows now"
        )
    if not permission_prompts.same_prompt(live, recorded):  # type: ignore[arg-type]
        raise Refused(
            "nothing was sent: the session is showing a different permission prompt now (the "
            "command or its options changed) — open it to see what it asks"
        )
    cursor = permission_prompts.selected_of(live)
    if cursor is None:
        raise Refused("nothing was sent: the prompt's selection could not be read")
    # AN EARLIER ANSWER TO THIS VERY DIALOG MAY HAVE LANDED (#1218 review): its hold is released
    # only on positive evidence (`permission_holds`), which is looked for first.
    permission_holds.reconcile(sid)
    try:
        held = permission_holds.open_holds(sid, str(live.get("identity") or ""))
    except permission_holds.HoldsUnreadable:
        raise Refused("nothing was sent: the permission holds could not be read", 503) from None
    if held:
        raise Refused(
            "nothing was sent: an earlier answer to this same prompt may already have reached the "
            "session — check the session and answer it there"
        )
    engine = screen_menus.engine_of(sid)
    rec = {
        "id": f"choose_{uuid.uuid4().hex}",
        "verb": "choose",
        "option": option,
        # What the operator chose, for the timeline and the card. Never rendered into bytes.
        "label": label,
        "session_id": sid,
        "state": "approved",
        "confidence": 1.0,
        "origin": "operator",
        "answers": escalation_id,
        "authority": esc["authority"],
        "ts": time.time(),
        "expires_at": time.time() + 120,
        "submit": "permission",
        # The recipe's inputs — the kind that read the dialog, where its cursor was, how many
        # options it has. `actuator.render` builds the keys from these and the option; the
        # precondition below refuses if any of them is no longer true at the write.
        "permission": {
            "parser": live["parser"],
            "identity": live.get("identity"),
            "from_selected": cursor,
            "count": len(live["options"]),
            "title": live.get("title"),
            "detail": live.get("detail"),
        },
        "precondition": {
            "key": esc["authority"]["physical_key"],
            "screen_fingerprint": orchestrator._screen_fingerprint(screen),
            "prompt_class": orchestrator._prompt_class(screen),
            "permission_digest": permission_prompts.digest(live),
            "engine": engine,
            "observed_at": time.time(),
        },
    }
    for k in ("mission_id", "engine", "title", "project", "project_id"):
        if esc.get(k) is not None:
            rec[k] = esc[k]
    return esc, rec


def _audit_permission(rec: dict, outcome: str) -> None:
    """The mission timeline's record of a permission answer (#1213): what was chosen, on which
    prompt, and whether it certainly reached the session. Best-effort — the ledger is the durable
    record; a mission that has closed meanwhile simply keeps no line."""
    mid = rec.get("mission_id")
    if rec.get("submit") != "permission" or not isinstance(mid, str) or not mid:
        return
    perm = rec.get("permission") or {}
    what = str(perm.get("title") or "a permission prompt")
    said = {
        "delivered": "sent",
        "indeterminate": (
            "may or may not have reached the session — check it before answering again"
        ),
    }.get(outcome, outcome)
    who = rec.get("engine") or "the agent"
    text = f"You answered {who}'s permission prompt ({what}): {rec.get('label')} — {said}"
    with contextlib.suppress(Exception):
        from . import missions

        missions.append_event(
            mid,
            "action",
            session_key=str(rec.get("session_id") or "") or None,
            text=text[:500],
            meta={
                "prompt_answered": True,
                "option": rec.get("option"),
                "label": rec.get("label"),
                "outcome": outcome,
                "answers": rec.get("answers"),
                "choose_id": rec.get("id"),
            },
        )


def _settle_unsent(action_id: str, detail: str) -> None:
    """A zero-byte refusal after the append: the `choose` must never stay deliverable."""
    ledger.compare_and_set(
        action_id, frozenset({"approved", "proposed"}), "failed", None, detail=detail
    )


#: The writer's own verdicts (`session_input.Outcome.state`) that mean NO byte reached the PTY.
#: `aborted` is a partial write, and anything unrecognised is treated as possibly sent.
ZERO_BYTE_OUTCOMES: frozenset[str] = frozenset({"not_live", "refused", "stale", "failed"})


def _reopen(escalation_id: str, action_id: str, prev_state: str, why: str) -> None:
    """Undo the close: nothing was typed, so the operator's decision is still open (#1082 review).

    Only an escalation that THIS answer closed is reopened (`answered_by == action_id`). A terminal
    `rejected` never moves by any other path, so the guard is belt-and-braces rather than a race.
    The answer's fields are blanked, not left beside a live state they no longer describe.
    """
    cur = ledger.get(escalation_id)
    if not cur or cur.get("state") != "rejected" or cur.get("answered_by") != action_id:
        return
    reopened = ledger.compare_and_set(
        escalation_id,
        frozenset({"rejected"}),
        prev_state,
        None,
        outcome="",
        answered_by="",
        detail=f"your answer was not sent: {why}",
    )
    # …and its bell row, which the close retired: the decision is open again, so it is an alert
    # again. Best-effort, as every notifications write beside the ledger is.
    if reopened is not None:
        with contextlib.suppress(Exception):
            from . import notifications

            notifications.unretire_for_action(escalation_id)


def _zero_byte(action_id: str) -> bool:
    """Did this `choose` certainly put NO byte on the PTY? Asked of the LEDGER, never inferred
    from what `deliver` returned (#1082 review 5022).

    `deliver` settles by a compare-and-set from `claimed`; when another settler moves the claim
    first (the mission-archive sweep, `recover_claimed`, `discharge_owed`) it returns the
    PRE-CLAIM snapshot — `approved`, no `outcome` — after the byte was written. So:

    * never claimed ⇒ settled before any write was attempted ⇒ zero bytes;
    * claimed ⇒ zero bytes ONLY when the writer itself said so (`outcome` in the zero-byte set);
      anything else — another settler's `indeterminate`, a missing outcome — may have been sent.

    "Was it claimed" is `claim_owner` on the MERGED record, which `ledger.claim` stamps and every
    later event carries forward — so it survives a compaction that rewrites the history into one
    row, where a scan for a `claimed` event would find none (#1082 review, compaction note). An
    unreadable ledger FAILS CLOSED: not provably zero-byte, so indeterminate. Blocking.
    """
    status, cur = ledger.lookup(action_id)
    if status != "found" or cur is None:
        return False
    if not cur.get("claim_owner"):
        return True
    return cur.get("outcome") in ZERO_BYTE_OUTCOMES


async def answer(escalation_id: str, option: object, label: object, *, registry) -> dict:
    """Validate, close the escalation, write the `choose`, deliver it.

    Returns the settled escalation with the delivered `choose` under ``choice``. Raises
    :class:`Refused` when nothing was sent — and then the escalation is OPEN again, whatever step
    refused — or :class:`Indeterminate` when bytes may have been sent (the escalation stays
    answered: retrying a keypress that may have landed is the one thing not to invite).
    """
    esc, rec = await asyncio.to_thread(prepare, escalation_id, option, label)
    action_id = rec["id"]
    prev_state = str(esc.get("state") or "escalated")

    closed = await asyncio.to_thread(
        ledger.compare_and_set,
        escalation_id,
        ledger.ESCALATION_STATES,
        "rejected",
        None,
        outcome=ANSWERED_OUTCOME,
        answered_by=action_id,
        detail=f"you chose {rec['option']}. {rec['label']}",
    )
    if closed is None:
        cur = await asyncio.to_thread(ledger.get, escalation_id)
        raise Refused(
            "nothing was sent: this decision is already " f"{(cur or {}).get('state') or 'settled'}"
        )

    permission = rec.get("submit") == "permission"

    async def refuse(why: str, status: int = 409) -> Refused:
        # Every caller of this is a ZERO-BYTE outcome, so a permission hold taken for this answer
        # is released on the writer's own evidence: nothing was typed.
        if permission:
            await asyncio.to_thread(permission_holds.release, action_id, f"nothing sent: {why}")
        await asyncio.to_thread(_reopen, escalation_id, action_id, prev_state, why)
        return Refused(f"nothing was sent: {why}", status)

    if permission:
        # THE HOLD BEFORE THE CLAIM (#1218 review 5405): from here until positive evidence, this
        # dialog is neither answered nor offered again. No hold, no keys.
        try:
            await asyncio.to_thread(
                permission_holds.take,
                action_id,
                session_key=str(rec.get("session_id") or ""),
                identity=str((rec.get("permission") or {}).get("identity") or ""),
                mission_id=rec.get("mission_id"),
            )
        except permission_holds.HoldsUnreadable as e:
            await asyncio.to_thread(_reopen, escalation_id, action_id, prev_state, str(e))
            raise Refused("nothing was sent: the permission hold could not be recorded", 503) from e

    try:
        await asyncio.to_thread(automation.append_operator_action, rec)
    except Exception as e:  # noqa: BLE001 — nothing claimed, nothing written
        raise await refuse(f"the action could not be recorded ({type(e).__name__})", 502) from e

    try:
        out = await actuator.deliver(action_id, registry=registry, operator_approval=True)
    except actuator.NotDeliverable as e:
        await asyncio.to_thread(_settle_unsent, action_id, str(e))
        raise await refuse(str(e)) from e
    except Exception as e:  # noqa: BLE001
        # Same reasoning as `/relay`: once claimed, bytes may be on the PTY. Say so; never
        # rewrite a real outcome (a CAS from `claimed` only), and owe the release if that fails.
        note = f"the delivering process could not settle it ({type(e).__name__})"
        try:
            moved = await asyncio.to_thread(
                ledger.compare_and_set,
                action_id,
                frozenset({"claimed"}),
                "indeterminate",
                None,
                detail=note,
            )
            if moved is None:
                cur = await asyncio.to_thread(ledger.get, action_id)
                if (cur or {}).get("state") in ("approved", "proposed"):
                    # Never claimed: no byte can have been written.
                    await asyncio.to_thread(_settle_unsent, action_id, note)
                    raise await refuse(note, 502) from e
        except Refused:
            raise
        except Exception:  # noqa: BLE001
            ledger.owe_terminalize(action_id, note)
        await asyncio.to_thread(_audit_permission, rec, "indeterminate")
        raise Indeterminate(
            f"the choice may or may not have landed ({type(e).__name__}); check the session "
            "before choosing again",
            action_id,
        ) from e

    # SUCCESS IS `delivered` AND NOTHING ELSE (#1082 review, finding 2). A `failed` settle is
    # zero-byte when the writer said so, and a partial write is not a refusal.
    if out.get("state") != "delivered":
        cur = await asyncio.to_thread(ledger.get, action_id) or out
        detail = str(cur.get("detail") or cur.get("state") or "the session moved on")
        if await asyncio.to_thread(_zero_byte, action_id):
            # A record `deliver` left waiting (it returned without settling) must not stay
            # deliverable either.
            await asyncio.to_thread(_settle_unsent, action_id, detail)
            raise await refuse(detail)
        await asyncio.to_thread(_audit_permission, rec, "indeterminate")
        raise Indeterminate(
            f"the choice may or may not have landed ({detail}); check the session before "
            "choosing again",
            action_id,
        )
    # THE DECISION THE CARD SHOWED, settled — with the delivered choice beside it. The card
    # resolves rows by id, and the row the operator tapped is the escalation, not the new choose.
    if permission:
        await asyncio.to_thread(permission_holds.release, action_id, "delivered in full")
    await asyncio.to_thread(_audit_permission, rec, "delivered")
    settled = await asyncio.to_thread(ledger.get, escalation_id) or closed
    return {**settled, "choice": out}
