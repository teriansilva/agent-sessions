"""A mission-held session parked at a TOOL-PERMISSION dialog gets a decision card (#1213).

The mission supervisor used to escalate this as "<objective>: the agent is waiting on a decision
only you can make" — a timeline line naming nothing, with nothing to press. This module is the
supervisor's other half of the fix:

* :func:`observe` reads the dialog off the live screen (`orchestrator.observed_prompt_for`, one
  frame: text, colours, the parsed dialog and its fingerprint);
* :func:`sync_card` keeps exactly ONE ledger escalation per session describing the dialog that is
  showing NOW. It is an ordinary `escalate` record with `observed_prompt.permission`, so it rides
  the session's card, appears in the mission's decisions, is announced once by `_persist`'s own
  bell path, and is answered through the existing `/choose` (`menu_answer`) — no second path to a
  PTY. A card for a dialog that is no longer showing (answered in the terminal, replaced by the
  next prompt) is retired, so the operator never taps a stale one; the next dialog then gets its
  own card on the same pass.

The ledger's one-live-action-per-session rule arbitrates: if something else is already live on the
session, no card is written and the caller falls back to the timeline escalation + bell.

Nothing here decides anything or sends a byte. The card is a question for the operator.
"""

from __future__ import annotations

import contextlib
import time
import uuid

from . import automation, orchestrator, permission_holds, permission_prompts, prefs, screen_menus
from . import orchestrator_ledger as ledger

#: The `origin` this module stamps, so it only ever retires ITS OWN cards.
ORIGIN = "mission_supervisor_permission"
#: The `escalation_reason` — tells the card why it is asking.
REASON = "permission"


def observe(physical_key: str) -> dict:
    """``orchestrator.observed_prompt_for`` for the session. Blocking."""
    return orchestrator.observed_prompt_for(physical_key)


def _is_ours(rec: dict) -> bool:
    return rec.get("origin") == ORIGIN and rec.get("verb") == "escalate"


def _live_for(session_key: str) -> list[dict]:
    return [r for r in ledger.live_actions() if str(r.get("session_id") or "") == session_key]


def retire_stale(session_key: str, permission: dict | None) -> list[str]:
    """Retire this module's live cards for the session that no longer describe what is showing.
    Returns the retired ids. Compare-and-set from an escalation state, so a card the operator is
    answering right now (closed by `menu_answer` first) is never touched. Blocking."""
    retired: list[str] = []
    for rec in _live_for(session_key):
        if not _is_ours(rec) or rec.get("state") not in ledger.ESCALATION_STATES:
            continue
        shown = (rec.get("observed_prompt") or {}).get("permission")
        if permission is not None and permission_prompts.same_prompt(shown, permission):
            continue
        moved = ledger.compare_and_set(
            rec["id"],
            ledger.ESCALATION_STATES,
            "expired",
            None,
            detail="the session is no longer showing this permission prompt",
        )
        if moved is not None:
            retired.append(rec["id"])
            # The bell row is best-effort beside the ledger.
            with contextlib.suppress(Exception):
                from . import notifications

                notifications.retire_for_actions([rec["id"]])
    return retired


def has_card(session_key: str, permission: dict) -> bool:
    """Is a live card of ours already showing THIS dialog? Blocking."""
    return any(
        _is_ours(r)
        and r.get("state") in ledger.ESCALATION_STATES
        and permission_prompts.same_prompt(
            (r.get("observed_prompt") or {}).get("permission"),
            permission,  # type: ignore[arg-type]
        )
        for r in _live_for(session_key)
    )


def build_card(mission_id: str, session_key: str, row: dict, observed: dict) -> dict:
    """The ledger escalation describing ``observed``'s dialog. Blocking (authority read)."""
    perm = observed["permission"]
    now = time.time()
    cfg = prefs.get_automation_policy("mission")
    ttl = int(cfg.get("proposal_ttl_minutes") or 30) * 60
    return {
        "id": uuid.uuid4().hex,
        "verb": "escalate",
        "state": "escalated",
        "ts": now,
        "expires_at": now + ttl,
        "tier": cfg.get("autonomy") or "",
        "session_id": session_key,
        "mission_id": mission_id,
        "engine": screen_menus.engine_of(session_key),
        "title": str(row.get("title") or "")[:200],
        "project": str((row.get("project") or {}).get("name") or "")[:200],
        "project_id": str((row.get("project") or {}).get("id") or ""),
        "confidence": 1.0,
        "rationale": permission_prompts.summary(perm)[:400],
        "escalation_reason": REASON,
        "origin": ORIGIN,
        "authority": automation.capture(session_key, mission_id),
        "observed_prompt": observed,
    }


def sync_card(mission_id: str, session_key: str, row: dict, observed: dict) -> dict:
    """Make the ledger match the screen for this session. Blocking.

    Returns ``{"retired": [...], "card": <record> | None, "existing": bool}``: ``card`` is the
    record written on THIS call (announced by `_persist`), ``existing`` that a live card already
    shows this dialog. Neither means the caller must announce it itself.
    """
    perm = observed.get("permission") if isinstance(observed, dict) else None
    out: dict = {"retired": retire_stale(session_key, perm), "card": None, "existing": False}
    out["reconciled"] = permission_holds.reconcile(session_key)
    if not isinstance(perm, dict):
        return out
    if has_card(session_key, perm):
        out["existing"] = True
        return out
    # AN EARLIER ANSWER TO THIS VERY DIALOG MAY HAVE LANDED (#1218 review): offering it again under
    # a new card would invite the second keypress an uncertain delivery must never get. No card;
    # the caller still escalates in words, and the operator settles it in the session. An
    # unreadable ledger is the same answer: nothing can be shown to be settled.
    try:
        held = permission_holds.open_holds(session_key, str(perm.get("identity") or ""))
    except permission_holds.HoldsUnreadable:
        out["unsettled"] = "unreadable"
        return out
    if held:
        out["unsettled"] = True
        return out
    try:
        rec = build_card(mission_id, session_key, row, observed)
    except automation.AuthorityChanged:
        return out
    kept = orchestrator._persist([rec])
    if kept:
        out["card"] = kept[0]
    return out


# --- an API session's pending requests (#1900) ------------------------------------------------
#
# An API session has no dialog on a screen; its pending native requests (approvals and agent
# questions) are a SET, keyed by request id, and a turn doing ten edits can hold several at once.
# So an API member gets ONE card per SESSION, never one per request: it says how many wait, lists
# a few, and sends the operator to the session's own pane, where each request is decided against
# its digest. The card is written (and announced, by `_persist`'s bell path) when the set goes from
# empty to non-empty; while the set changes the same card is updated in place, with no second
# announcement; it is retired when the set empties. A card the operator dismissed is not raised
# again for the same requests, only when a new one arrives (escalate once per stop).

#: The `escalation_reason` of an API session's card.
API_REASON = "api_pending"
#: How long an API card lives between passes. The card mirrors state and is retired when the set
#: empties, so its life is not the proposal TTL: a short TTL would expire it between passes (a
#: review mission is swept every 300 s) and the next pass would announce the same requests again.
API_CARD_TTL_S = 3600
#: What an API card's own verb-free decision is: nothing to approve here, so dismiss only.
_OPERATOR_CLOSED = frozenset({"rejected", "dismissed"})


def api_fingerprint(items: list[dict]) -> list[str]:
    """The pending set's content fingerprint: its sorted request ids, as a list (an id may contain
    any character, so a joined string could not be split back)."""
    return sorted(str(i["request_id"]) for i in items)


def api_summary(items: list[dict], title: str) -> str:
    """The card's words: how many approvals and questions wait, never what they are."""
    asks = sum(1 for i in items if i.get("question"))
    approvals = len(items) - asks
    parts = [
        f"{n} {word if n == 1 else word + 's'}"
        for n, word in ((approvals, "approval"), (asks, "question"))
        if n
    ]
    where = f" in {title}" if title else ""
    return f"{' and '.join(parts)} waiting{where}. Decide each one in its conversation."


def _api_cards(session_key: str) -> list[dict]:
    """Every card of ours for this API session, newest last. Blocking."""
    rows = [
        r
        for r in ledger.latest_by_id().values()
        if _is_ours(r)
        and str(r.get("session_id") or "") == session_key
        and r.get("escalation_reason") == API_REASON
    ]
    return sorted(rows, key=lambda r: float(r.get("ts") or 0))


def _api_payload(items: list[dict], pane: str) -> dict:
    from . import mission_observe

    return {
        "count": len(items),
        "items": items[: mission_observe.SHOWN_ITEMS],
        "fp": api_fingerprint(items),
        "pane": pane,
    }


def sync_api_card(
    mission_id: str, session_key: str, row: dict, items: list[dict], *, pane: str
) -> dict:
    """Make the ledger match an API session's pending set. Blocking.

    Returns ``{"retired": [...], "card": <record written now> | None, "existing": bool,
    "updated": bool}``. ``items`` is `mission_observe.pending_items` of the snapshot.
    """
    out: dict = {"retired": [], "card": None, "existing": False, "updated": False}
    cards = _api_cards(session_key)
    live = [r for r in cards if r.get("state") in ledger.ESCALATION_STATES]
    if not items:
        for rec in live:
            moved = ledger.compare_and_set(
                rec["id"],
                ledger.ESCALATION_STATES,
                "expired",
                None,
                detail="no approval or question is waiting in the session any more",
            )
            if moved is not None:
                out["retired"].append(rec["id"])
                with contextlib.suppress(Exception):
                    from . import notifications

                    notifications.retire_for_actions([rec["id"]])
        return out
    fp = api_fingerprint(items)
    cfg = prefs.get_automation_policy("mission")
    ttl = max(int(cfg.get("proposal_ttl_minutes") or 30) * 60, API_CARD_TTL_S)
    title = str(row.get("title") or "")[:200]
    if live:
        # ONE CARD: the newest live one is kept current; any older one (a race) is retired.
        keep, extra = live[-1], live[:-1]
        for rec in extra:
            if ledger.compare_and_set(
                rec["id"], ledger.ESCALATION_STATES, "expired", None, detail="superseded"
            ):
                out["retired"].append(rec["id"])
        out["existing"] = True
        now = time.time()
        # Kept alive while anything waits, and updated in place, never re-announced.
        if (keep.get("api_pending") or {}).get("fp") != fp or float(
            keep.get("expires_at") or 0
        ) - now < ttl / 2:
            moved = ledger.compare_and_set(
                keep["id"],
                ledger.ESCALATION_STATES,
                str(keep.get("state") or "escalated"),
                None,
                expect_ts=keep.get("ts"),
                api_pending=_api_payload(items, pane),
                rationale=api_summary(items, title)[:400],
                expires_at=now + ttl,
            )
            out["updated"] = moved is not None
        return out
    # ONCE PER STOP: a card the operator dismissed is not raised again for the same requests.
    last = cards[-1] if cards else None
    if last is not None and last.get("state") in _OPERATOR_CLOSED:
        fp = (last.get("api_pending") or {}).get("fp")
        seen = {str(x) for x in fp} if isinstance(fp, list) else set()
        if {str(i["request_id"]) for i in items} <= seen:
            out["dismissed"] = True
            return out
    try:
        authority = automation.capture(session_key, mission_id)
    except automation.AuthorityChanged:
        return out
    now = time.time()
    rec = {
        "id": uuid.uuid4().hex,
        "verb": "escalate",
        "state": "escalated",
        "ts": now,
        "expires_at": now + ttl,
        "tier": cfg.get("autonomy") or "",
        "session_id": session_key,
        "mission_id": mission_id,
        "engine": session_key.partition(":")[0],
        "title": title,
        "project": str((row.get("project") or {}).get("name") or "")[:200],
        "project_id": str((row.get("project") or {}).get("id") or ""),
        "confidence": 1.0,
        "rationale": api_summary(items, title)[:400],
        "escalation_reason": API_REASON,
        "origin": ORIGIN,
        "authority": authority,
        "api_pending": _api_payload(items, pane),
        # Nothing on this card is deliverable: the operator decides in the pane.
        "evidence": "none",
    }
    kept = orchestrator._persist([rec])
    if kept:
        out["card"] = kept[0]
    return out


__all__ = [
    "API_CARD_TTL_S",
    "API_REASON",
    "ORIGIN",
    "REASON",
    "api_fingerprint",
    "api_summary",
    "build_card",
    "has_card",
    "observe",
    "retire_stale",
    "sync_api_card",
    "sync_card",
]
