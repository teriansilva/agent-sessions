"""Resolve relay records that were written before the bytes and never settled (#894).

The relay writes its `operator_msg` FIRST and settles it afterwards, because the other order
suppressed the store failure and still answered `delivered` — the words reached the agent and the
transcript never said who sent them. That ordering is right, and it has a cost: a process that
exits between the write and the settlement leaves a record reading `sending` for ever.

**`sending` is a claim about NOW, and a record that keeps making it is lying by the next minute.**
So it is reconciled at read time, from the one thing that does know.

## Only POSITIVE evidence settles a record (#903 review 3, findings 2 and 3)

The first version concluded "nothing was sent" from the ABSENCE of a ledger row after a grace
window. Two measured failures killed that idea rather than shrinking the window:

* **a ledger append waits on an unbounded writer flock.** No timer can bound it, so absence never
  becomes proof by waiting — a late append landed after the record had already been stamped
  `failed`, and the operator was told nothing was sent about a delivery that then happened;
* **compaction legitimately removes a terminal row** once its settlement has been frozen into
  `mission_settlements`. A delivered relay whose row had been compacted away was overwritten to
  `failed` — the app destroying its own record of a success.

So absence is not a verdict here, and this module has no clock in it at all. Exactly two things
settle a record, and both are somebody else's durable statement of what happened:

1. **a frozen settlement** for the same `action_id` — the projection that is written before a row
   may be destroyed, and therefore outlives it. Preferred over everything, because it is the only
   evidence that survives compaction. Its `source_compacted` marker is explicitly NOT evidence:
   that value is the store saying the row was gone before anyone could read it, which is the
   absence of a fact rather than one;
2. **a terminal ledger row** — `delivered`, `failed`, `expired`, `rejected`, `indeterminate`.

Anything else — an absent row, a live row, an unreadable ledger — is left exactly as it is. A
record that keeps saying `sending` when nobody can say otherwise is uncomfortable and true; the
alternative is a comfortable lie in whichever direction the guess went.

**`indeterminate` is revisited, `delivered` and `failed` are not.** An ambiguous outcome is a
statement that nobody could tell yet, so a later terminal ledger row is allowed to resolve it. A
definite one is finished.
"""

from __future__ import annotations

import contextlib
import logging

from . import missions, orchestrator_ledger

log = logging.getLogger(__name__)

#: How many unsettled records one pass will resolve. A bound rather than a cursor: these are rare
#: (they need a crash in a sub-second window), and an unbounded scan on a hot read path is a worse
#: failure than a record that settles on the next read.
MAX_PER_PASS = 20

#: The record states a pass may still change — the store's own set, not a second copy. `sending`
#: is unfinished; `indeterminate` is a statement that nobody could tell YET, which a later
#: terminal row is allowed to resolve. Everything else is a definite outcome and is never
#: revisited, and `settle_relay_event` enforces that at the write as well.
OPEN_STATES = missions.RELAY_OPEN_STATES


def pending_relays(events: list[dict]) -> list[str]:
    """Every relay record still open to being resolved, newest first."""
    out: list[str] = []
    for e in events:
        if e.get("kind") != "operator_msg":
            continue
        meta = e.get("meta") or {}
        if not isinstance(meta, dict) or not meta.get("relay"):
            continue
        if str(meta.get("state") or "") not in OPEN_STATES:
            continue
        action_id = str(e.get("action_id") or "")
        if action_id:
            out.append(action_id)
        if len(out) >= MAX_PER_PASS:
            break
    return out


def _frozen(action_ids: list[str], *, path=None) -> dict[str, dict]:
    """The settlements that outlive their ledger rows. Unreadable is an empty answer, not a
    verdict — every caller below treats "nothing here" as "no evidence", never as "it failed"."""
    if not action_ids:
        return {}
    try:
        return missions.settlements_for(action_ids, path=path)
    except Exception:  # noqa: BLE001
        log.debug("relay reconcile could not read the settlements")
        return {}


def reconcile(mission_id: str, events: list[dict], *, path=None) -> int:
    """Settle what somebody else's durable record can answer for. Returns how many records moved.

    Never raises: this runs inside a read, and a mission's page must render whatever the ledger is
    doing. A record it cannot resolve is left as it is, which is the honest answer for a delivery
    whose fate is genuinely unknown.
    """
    # ANY OWED TERMINALIZATION FIRST (#903 review 5, finding 1). A delivering process that could
    # not release its own claim left the ledger `claimed` under a live owner, which recovery is
    # right to refuse — so the retry belongs on a cadence that already exists, and this is it:
    # it runs whenever anybody looks at a mission, and it is already the place unfinished relay
    # business is resolved. Discharging it here is also what lets the loop below find a terminal
    # row for a record that is still `sending`.
    with contextlib.suppress(Exception):
        orchestrator_ledger.discharge_owed(path)

    ids = pending_relays(events)
    if not ids:
        return 0
    frozen = _frozen(ids, path=path)
    moved = 0
    for action_id in ids:
        settled = frozen.get(action_id)
        if settled is not None and str(settled.get("state") or "") == missions.SETTLEMENT_LOST:
            # NOT EVIDENCE, DELIBERATELY. `source_compacted` is the store's own words for "the
            # ledger row was gone before anything could read it" — a marker that a fact is
            # missing, not a fact. Reading it as an outcome would settle every relay whose row
            # has not appeared yet with the one state that means nobody knows.
            settled = None
        if settled is not None:
            # THE PROJECTION OUTLIVES THE ROW. Preferred over the ledger precisely because it is
            # what remains after compaction has removed the row it was frozen from.
            state = str(settled.get("state") or "")
            detail = str(settled.get("outcome") or settled.get("rationale") or "")
        else:
            # TRI-STATE, not `get()`. `get()` maps every I/O error to "no such action", which
            # would turn a transiently unreadable ledger into a claim about a delivery.
            try:
                status, rec = orchestrator_ledger.lookup(action_id)
            except Exception:  # noqa: BLE001
                log.debug("relay reconcile could not read the ledger for %s", action_id)
                return moved
            if status != "found" or rec is None:
                # ABSENT or UNREADABLE. Neither is evidence: an append can still be blocked on the
                # ledger's writer lock, and a terminal row can have been compacted away. We could
                # not look is not the same as there is nothing there.
                continue
            state = str(rec.get("state") or "")
            if state not in orchestrator_ledger.TERMINAL_STATES:
                continue  # genuinely in flight; `sending` is true
            detail = str(rec.get("detail") or "")
        if not state:
            continue
        try:
            if missions.settle_relay_event(
                mission_id, action_id=action_id, state=state, detail=detail, path=path
            ):
                moved += 1
        except Exception:  # noqa: BLE001
            log.debug("relay reconcile could not settle %s", action_id)
    return moved
