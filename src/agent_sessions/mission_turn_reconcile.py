"""Settle a mission turn once the ledger actions it reserved have gone terminal.

**The named owner of the terminal-action → turn transition (#871).** Decision 2 says a turn is
not terminal until its action is, which closes the hole where archive's fence opened while an
`approved` action was still deliverable. But it created a second question, which the review
asked and the issue had not answered: `expire_due()` terminalizes the ACTION, and nothing
terminalized the TURN. Without an owner for that step the action settles, the turn stays
`in_progress` for ever, and decision 1's fence 409s the archive indefinitely — a deadlock
reached by the two mitigations rather than despite them, which is the same shape as the bug
decision 2 was corrected for.

**Bounded work at named call sites, not a daemon.** There is no background loop here: the
archive path reconciles before it fences (so an archive heals itself rather than refusing for
ever) and the turn read reconciles before it answers (so an operator looking at the mission is
the other trigger). Both are the moments where a stale `in_progress` actually costs something,
which is the whole justification for touching it at all.

**It never re-drives anything.** Reconciliation only records what already happened: it reads the
ledger, and where every reserved action is terminal it writes that conclusion onto the turn. A
live action is left alone — the TTL sweep owns settling those, and a reconciler that settled
them would be a second policy for the same transition.
"""

from __future__ import annotations

import logging

from . import missions
from . import orchestrator_ledger as ledger

log = logging.getLogger(__name__)

#: The outcome recorded on a turn whose owner died before it reserved a ledger write. Nothing was
#: appended, so nothing was delivered — the turn produced no instruction at all.
NO_WRITE = "the turn's owner stopped before anything was sent; nothing was delivered"

#: …and once every reserved action is terminal, the turn carries THEIR outcome rather than a
#: generic one. `indeterminate` is contagious on purpose: a turn that may have delivered must not
#: report that it did not.
_INDETERMINATE = "indeterminate"


def _projected(ids: list[str], latest: dict[str, dict]) -> list[dict]:
    """The SAME shape `routes/missions._hydrate_actions` produces.

    The frozen snapshot is what every later replay returns, and it must not depend on WHICH path
    settled the turn. This module stored raw ledger records while the route stored projected
    ones, so the same turn answered with `projection`/`can_approve` when the route settled it and
    without them when the reconciler did — one contract, two derivations, which is the drift the
    projection exists to end.
    """
    out = []
    for aid in ids:
        rec = latest.get(aid) or {}
        out.append(
            {
                "id": aid,
                **{k: v for k, v in rec.items() if k in ("verb", "session_id", "title")},
                **ledger.project_for_operator(rec.get("state")),
            }
        )
    return out


def _turn_outcome(states: list[str]) -> tuple[str, str]:
    """`(turn_state, result)` for a set of terminal action states.

    `indeterminate` wins over everything: it is the honest answer for a write that may or may not
    have reached the PTY, and a turn that summarised it as "done" or "expired" would be asserting
    something nobody knows. That is the same rule `recover_claimed` follows, applied one level up.
    """
    if _INDETERMINATE in states:
        return _INDETERMINATE, "delivery is indeterminate; the action may or may not have been sent"
    seen = sorted(set(states))
    if all(s == "delivered" for s in states):
        return "done", "delivered"
    # PARTIAL is its own answer. Collapsing it into "settled without delivery" produced a durable
    # result that contradicted its own state list — `settled without delivery: delivered, expired`
    # says nothing was sent while naming the thing that was (review on #881). It matters to the
    # operator too: something DID reach the agent, so the turn is not a clean no-op they can
    # simply repeat.
    if any(s == "delivered" for s in states):
        return "done", "partially delivered: " + ", ".join(seen)
    return "done", "settled without delivery: " + ", ".join(seen)


def reconcile(*, mission_id: str | None = None) -> int:
    """Settle every reconcilable orphaned turn. Returns how many were settled.

    Reads the ledger FIRST and the missions store second, which is the lock order #862 fixed
    (LEDGER → MISSIONS). `settle_turn` takes the turn's own fence, so a turn reclaimed by a live
    owner between the worklist read and the write is refused rather than overwritten — the same
    protection an ordinary settle gets, for the same reason.
    """
    try:
        orphans = missions.orphaned_turns(mission_id=mission_id)
    except Exception:  # noqa: BLE001 — reconciliation is opportunistic; never fail the caller
        log.debug("turn reconcile: worklist unavailable", exc_info=True)
        return 0
    if not orphans:
        return 0

    # TRI-STATE, and `latest_by_id()` cannot express it. `_read_all_at` turns an `OSError` into
    # `[]`, so an unreadable ledger is indistinguishable from an empty one — and here "empty"
    # means every reserved action is missing, which settles turns `indeterminate`, which is
    # terminal, which drops them out of `unresolved_turn_keys` and OPENS THE ARCHIVE FENCE.
    #
    # A transient permission or I/O error would therefore have let an archive through, which is
    # precisely what this function's comment claimed it prevented. It did not: the try/except
    # below only ever fired if `latest_by_id` RAISED, and production never raises here. The
    # regression that "covered" it monkeypatched the raise, so it exercised the handler and never
    # the real door (review on #881).
    #
    # SERIALIZED as well as checked, which the first fix got wrong: `latest_by_id_checked`
    # gives tri-state without the writer lock, so a writer that has committed its receipt and
    # not yet appended is observed as an empty ledger — the reconciler settles the turn
    # `indeterminate`, and the append then lands an `approved` action on a turn that is
    # already terminal. I added `latest_by_id_serialized_checked` for exactly this and then
    # called the weaker reader.
    try:
        status, latest = ledger.latest_by_id_serialized_checked()
    except Exception:  # noqa: BLE001
        log.debug("turn reconcile: ledger read failed", exc_info=True)
        return 0
    if status != "ok":
        log.debug("turn reconcile: ledger unreadable — settling nothing")
        return 0

    settled = 0
    for t in orphans:
        ids = t["action_ids"]
        # DECISION 4: `None` is "no snapshot was taken", `[]` is "one was taken and it was
        # empty". They are different facts and the store must not conflate them, so this is set
        # per branch below rather than defaulted to a list.
        snapshot: list[dict] | None = None
        if not t["reserved"]:
            # Died before the ledger was ever touched. There is nothing to wait for and nothing
            # that could have been delivered.
            state, result = _INDETERMINATE, NO_WRITE
        elif not ids:
            # Reserved, but named no actions — the model asked for nothing. A snapshot WAS taken
            # here and it is legitimately empty, which is exactly the `[]` case.
            state, result = "done", "no action was proposed"
            snapshot = []
        else:
            recs = [latest.get(a) for a in ids]
            if any(r is None for r in recs):
                # An action the turn reserved is not in the ledger. `unresolved_turn_keys` pins
                # exactly these against compaction, so absence here is not "compacted away" — it
                # is an append that never landed, which is indistinguishable from one that landed
                # and was lost. Indeterminate, never "nothing happened".
                # Snapshot stays `None`: a partial snapshot presented as THE snapshot would be
                # a more confident lie than admitting none was taken.
                state, result = _INDETERMINATE, "a reserved action is missing from the ledger"
            else:
                states = [str((r or {}).get("state") or "") for r in recs]
                if any(s not in ledger.TERMINAL_STATES for s in states):
                    continue  # still live — the TTL sweep owns this one
                state, result = _turn_outcome(states)
                snapshot = _projected(ids, latest)
        try:
            ok = missions.settle_turn(
                t["mission_id"],
                t["turn_id"],
                t["fence"],
                state=state,
                result=result,
                result_meta={"reconciled": True},
                action_ids=ids,
                action_snapshot=snapshot,
                # BOTH timeline events, in the settlement transaction — the same invariant the
                # route's recovery path carries. This is the SECOND production settlement path
                # (archive reconciliation calls it), and it settled turns to a terminal state
                # with `assistant_seq` NULL and nothing that ever repaired them: #871 requires
                # every terminal turn to carry both events, and it was true on one path and
                # false on the other (review on #881).
                #
                # The reply itself died with the writer, so the event records what IS known —
                # which actions the turn produced and that this was reconciled, not answered.
                assistant_text="",
                assistant_meta={
                    "turn_id": t["turn_id"],
                    "actions": ids,
                    "reconciled": True,
                    "outcome": state,
                },
            )
        except Exception:  # noqa: BLE001
            log.debug("turn reconcile: settle failed", exc_info=True)
            continue
        if ok:
            settled += 1
            log.info(
                "turn reconcile: mission %s turn %s -> %s", t["mission_id"], t["turn_id"], state
            )
    return settled
