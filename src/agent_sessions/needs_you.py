"""NEEDS YOU for sessions no mission holds — the Ask page's list (#1086).

One read answers the whole section: which sessions need the operator, what KIND of stop each one
is at, and which pending decision (if any) can be settled from the row. It is a pure join over
facts the app already keeps, so nothing here decides anything:

* **the session cards** — `pulse.build_cards`, the same curation the sidebar and the scan use
  (archived and review-excluded sessions are already gone, the window already applied);
* **the pending decisions** — the orchestrator ledger's live actions, PROJECTED exactly as every
  other decision surface receives them (`routes/pulse._operator_projection`). The controls a row
  offers come from that projection and nowhere else;
* **the mission memberships** — a session a mission holds is left out. Its decisions belong to the
  mission console (#1018's ownership rule), and listing it twice would give one decision two places
  to be settled;
* **the live screen** — `orchestrator.observed_prompt_for`, for the KIND. The kind is read off the
  screen, never taken from a model: a label that lies still gets pressed (`MissionQuestionCard`).

A session needs you when its review says so (`intervention_required`) OR it holds a decision the
operator can still act on. Rows sort by when that started, newest first. Facets are computed over
every needs-you row BEFORE the agent/project filters, so the dropdowns keep listing every option
(the `/api/sessions` rule).

**An unreadable membership store is not "no missions".** If it cannot be read, a mission-held
session could be listed here and settled from the wrong surface, so the read fails
(`MembershipUnavailable`) and the page says it could not read sessions — never an empty list that
may be wrong.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable

# Rows returned at most. The list is a worklist of things waiting on one person; past this the
# operator has a different problem, and the screen read below is per row.
ROWS_MAX = 50

# The ledger fields a row may carry. Whitelisted rather than passed through: a record also holds
# preconditions and fingerprints the page has no use for, and this read is polled.
_ACTION_FIELDS = (
    "id",
    "verb",
    "state",
    "projection",
    "can_approve",
    "can_reject",
    "confidence",
    "rationale",
    "escalation_reason",
    "option",
    "answer",
    "ts",
    "expires_at",
    "render_status",
)

# `orchestrator._prompt_class` → the kind a row names. A parsed menu always wins (`choice`).
_KIND_BY_CLASS = {
    "confirm": "approval",
    "choice": "choice",
    "question": "question",
    "open": "needs_inspection",
}
KINDS = ("choice", "approval", "question", "needs_inspection")


class Unavailable(Exception):
    """A store this list is built from could not be read, so the list cannot be trusted."""


class MembershipUnavailable(Unavailable):
    """The mission-membership store could not be read, so ownership cannot be established."""


class LedgerUnavailable(Unavailable):
    """The action ledger could not be read: a decision-only session would silently vanish."""


class ReadIncomplete(Unavailable):
    """A STRICT read (the notification sync's) could not establish its whole input: a session
    store, the metadata sidecar or the dismissal record could not be read completely."""


def _num(value: object) -> float | None:
    """A FINITE number, or ``None``: ``NaN``/``inf`` are not times, and `JSONResponse` refuses
    them."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            v = float(value)
        except OverflowError:  # a valid JSON integer can exceed float range (review 5044)
            return None
        return v if math.isfinite(v) else None
    return None


def _actionable(action: dict, now: float) -> bool:
    """Controls the projection offers AND not past its own deadline.

    The route runs the ledger's housekeeping first; this is the read-side half of the same rule, so
    a sweep that could not run never lets an expired proposal be advertised as approvable.
    """
    if not (action.get("can_approve") or action.get("can_reject")):
        return False
    expires = _num(action.get("expires_at"))
    return expires is None or expires > now


def _first_present(*values: float | None) -> float:
    """The first value that EXISTS. Not an ``or`` chain: ``0.0`` is a time, not an absence."""
    for v in values:
        if v is not None:
            return v
    return 0.0


def _pick_action(actions: list[dict], now: float) -> dict | None:
    """The one decision a row offers: the NEWEST one the operator can still act on."""
    live = [a for a in actions if _actionable(a, now)]
    if not live:
        return None
    return max(live, key=lambda a: _num(a.get("ts")) or 0.0)


def _public_action(action: dict) -> dict:
    out = {k: action[k] for k in _ACTION_FIELDS if k in action}
    # The menu an escalation RECORDED is the one `/choose` checks a tap against
    # (`menu_answer`), so it is the one the row must show — never a fresher parse.
    observed = action.get("observed_prompt")
    if isinstance(observed, dict) and isinstance(observed.get("menu"), dict):
        out["menu"] = observed["menu"]
    return out


def _kind(observed: dict | None) -> tuple[str, dict | None]:
    if not isinstance(observed, dict):
        return "needs_inspection", None
    menu = observed.get("menu") if isinstance(observed.get("menu"), dict) else None
    if menu is not None:
        return "choice", menu
    return _KIND_BY_CLASS.get(str(observed.get("prompt_class") or ""), "needs_inspection"), None


def _safe_observe(observe: Callable[[dict], dict | None], row: dict) -> dict | None:
    try:
        return observe(row)
    except Exception:  # noqa: BLE001 — an unreadable screen is `needs_inspection`, not an error
        return None


def _project(card: dict) -> dict:
    p = card.get("project") or {}
    return {"id": str(p.get("id") or ""), "name": str(p.get("name") or "")}


def build(
    cards: Iterable[dict],
    pending: Iterable[dict],
    held: set[str] | None,
    *,
    observe: Callable[[dict], dict | None],
    engine: str | None = None,
    project: str | None = None,
    now: float | None = None,
    suppressed: dict[str, str] | None = None,
    strict: bool = False,
) -> dict:
    """The NEEDS YOU payload. Pure apart from ``observe`` (a screen read per candidate row).

    ``held`` is the set of session keys a mission holds, or ``None`` when that store could not be
    read — which raises :class:`MembershipUnavailable` rather than guessing.
    """
    if held is None:
        raise MembershipUnavailable
    now = time.time() if now is None else now
    by_session: dict[str, list[dict]] = {}
    for a in pending:
        sid = str(a.get("session_id") or "")
        if sid:
            by_session.setdefault(sid, []).append(a)

    rows: list[dict] = []
    for card in cards:
        sid = str(card.get("id") or "")
        if not sid or sid in held:
            continue
        action = _pick_action(by_session.get(sid, []), now)
        flagged = bool(card.get("intervention_required"))
        if not flagged and action is None:
            continue
        # WHEN it started needing you: the decision's own time when there is one, else the review
        # that raised the flag, else the session's last activity.
        since = _first_present(
            _num(action.get("ts")) if action else None,
            _num(card.get("reviewed_at")),
            _num(card.get("last_activity")),
        )
        reason = str(card.get("intervention_reason") or "") or (
            str(action.get("escalation_reason") or action.get("rationale") or "") if action else ""
        )
        rows.append(
            {
                "id": sid,
                "engine": str(card.get("engine") or ""),
                "title": str(card.get("title") or ""),
                "project": _project(card),
                "last_activity": _num(card.get("last_activity")),
                "since": since,
                "reason": reason,
                "summary": str(card.get("ai_summary") or ""),
                "flagged": flagged,
                "action": _public_action(action) if action else None,
            }
        )

    # DISMISSED rows (#1086 Phase 3) stay hidden while the session still shows the screen the
    # operator dismissed; the moment it moves on, it needs them again. Only the dismissed rows are
    # read here, and the read is reused below — never a second screen read for one row.
    observed_cache: dict[str, dict | None] = {}
    if suppressed:
        kept = []
        for r in rows:
            fp = suppressed.get(r["id"])
            if fp is None:
                kept.append(r)
                continue
            if strict:
                # A dismissal is judged on the screen; a screen that could not be read is
                # UNKNOWN, never "moved" (Hermes 5265, finding 5).
                try:
                    observed_cache[r["id"]] = observe(r)
                except Exception as e:  # noqa: BLE001 — any failure is "could not read"
                    raise ReadIncomplete(f"screen of a dismissed session: {e}") from e
            else:
                observed_cache[r["id"]] = _safe_observe(observe, r)
            current = (observed_cache[r["id"]] or {}).get("fingerprint")
            if current != fp:
                kept.append(r)
        rows = kept
    rows.sort(key=lambda r: r["since"], reverse=True)
    facets = {
        "engines": sorted({r["engine"] for r in rows if r["engine"]}),
        "projects": sorted(
            (
                {"id": pid, "name": name}
                for pid, name in {
                    (r["project"]["id"], r["project"]["name"]) for r in rows if r["project"]["id"]
                }
            ),
            key=lambda p: (p["name"].lower(), p["id"]),
        ),
    }
    total_unfiltered = len(rows)
    # Membership, not display (#1086 review 5184): every session that needs you, BEFORE the list's
    # agent/project filter and its row cap, so an Ask answer can mark a session the list is
    # currently filtering out, and the pinned count is never a filtered subset passed off as all.
    all_ids = [r["id"] for r in rows]
    if engine:
        rows = [r for r in rows if r["engine"] == engine]
    if project:
        rows = [r for r in rows if r["project"]["id"] == project]
    total = len(rows)
    rows = rows[:ROWS_MAX]
    # The screen is read only for rows that will be SHOWN — the read is per row and bounded.
    for r in rows:
        observed = (
            observed_cache[r["id"]] if r["id"] in observed_cache else _safe_observe(observe, r)
        )
        r["kind"], live_menu = _kind(observed)
        # A menu for display: the recorded one when the decision has one (what `/choose` checks),
        # else what the screen shows now. Labels are agent text, cleaned and capped by the parser.
        recorded = (r["action"] or {}).get("menu")
        r["menu"] = recorded if isinstance(recorded, dict) else live_menu
    return {
        "rows": rows,
        "total": total,
        "total_unfiltered": total_unfiltered,
        "needs_you_ids": all_ids,
        "truncated": total > len(rows),
        "facets": facets,
    }
