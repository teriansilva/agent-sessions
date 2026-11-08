"""Pulse routes (#441 Phase 2): the cached recent-work overview + manual scan.

* ``GET  /api/pulse`` — the cached overview artifact, served instantly; it NEVER triggers a
  scan. Returns the "never scanned" empty overview (at the configured window/depth) before the
  first scan (or on a cache miss).
* ``POST /api/pulse/scan`` — run one scan now and return the fresh artifact. Uses the configured
  ``pulse`` window/depth (#441 Phase 3), overridable per-request by an optional JSON body
  ``{"depth": …, "window_days": …}`` (the page's depth control). The single ``409`` case is
  "a mission control scan is already running" (single-flight, #441 Phase 1) — its body carries
  the live AI-activity snapshot so the UI shows the running scan, not an error. An **unconfigured AI
  gateway never 409s here**: a ``slow`` scan degrades to ``fast`` curation and returns **200**
  with ``synthesis_skipped: true`` (the page always works).
* ``POST /api/pulse/ask`` (#522) — one natural-language question over past sessions
  (``pulse_chat.ask``). Its own single-flight kind ``pulse-chat`` (an ask never blocks a
  scan, or vice-versa; concurrent asks 409 with the activity snapshot). Deliberate contrast
  with ``/scan``: an **unconfigured endpoint is a 409** (``configured: false``) and an
  endpoint failure a **502** — a chat has no useful non-LLM fallback, so it surfaces the
  condition instead of returning an empty "answer". The UI pre-gates on ``configured``;
  these are backstops.

The shared ``GET /api/ai/activity`` surface lives in ``routes/system.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import (
    actuator,
    agent_reports,
    aitasks,
    ask_store,
    engines,
    handoff,
    menu_answer,
    metadata,
    missions,
    needs_you,
    needs_you_dismissals,
    notifications,
    orchestrator,
    orchestrator_chat,
    orchestrator_ledger,
    prefs,
    pulse,
    pulse_chat,
    review,
    session_input,
    webpush,
    work_recap,
)
from .sessions import _hard_scope_filter

log = logging.getLogger(__name__)

# How many ledger rows the activity feed carries. Bounded so a long-lived install's
# history can't make the Pulse page payload grow without limit.
FEED_LIMIT = 100


#: Longest operator-edited message an approve may carry (#1086 Phase 3).
EDIT_TEXT_MAX = 4000
#: The decisions whose TEXT the operator may edit before approving: the model's `answer` and the
#: standalone `continue` nudge. A choice is a digit, and a mission's actions are checked against
#: their objectives (`txt_check`), so neither is editable here.
EDITABLE_VERBS = frozenset({"answer", "continue"})


def _suggested_text(action: dict) -> str:
    """What approving this text decision UNEDITED would type — the same text `actuator.render`
    produces for it. "" when an `answer` carries nothing usable."""
    if action.get("verb") == "answer":
        try:
            return handoff.sanitize_seed(str(action.get("answer") or ""))
        except handoff.HandoffError:
            return ""
    return actuator.default_nudge_text(prefs.get_session_assistance())


def _operator_edit(action_id: str, text: object) -> tuple[dict | None, tuple[int, str] | None]:
    """The EDIT an approve carries — ``(fields, None)`` — or a refusal ``(None, (status, reason))``.

    Edited text is the operator's, not the model's, so the delivered action becomes a `relay`, the
    verb for operator-authored bytes (`actuator.OPERATOR_VERBS`): same renderer, same
    `sanitize_seed`, same claim-before-write and the action's OWN fence (fingerprint + prompt
    class). The record keeps what was SUGGESTED and what was SENT side by side.

    **Nothing is written here** (#1086 review 5184). An earlier cut rewrote the pending proposal
    in place, and two approvals could then interleave — A's claim sending B's text, or an unedited
    approve sending the original under a record that said the edit was sent. The fields are handed
    to `actuator.deliver`, which renders from them and writes them in the SAME atomic claim that
    decides who delivers, against the revision it read. ``(None, None)`` = no edit (identical text).
    """
    if not isinstance(text, str):
        return None, (422, "text must be a string")
    if len(text) > EDIT_TEXT_MAX:
        return None, (422, f"text must be at most {EDIT_TEXT_MAX} characters")
    try:
        sent = handoff.sanitize_seed(text)
    except handoff.HandoffError:  # empty once sanitised — the same rule a seed obeys
        return None, (422, "text is empty")
    if not sent.strip():
        return None, (422, "text is empty")
    status, cur = orchestrator_ledger.lookup(action_id)
    if status != "found" or cur is None:
        return None, ((404 if status == "absent" else 503), "unknown action")
    if cur.get("verb") not in EDITABLE_VERBS or cur.get("mission_id"):
        return None, (422, "this decision's text cannot be edited here")
    if cur.get("state") not in orchestrator_ledger.CLAIMABLE_STATES:
        return None, (409, "this decision can no longer be approved")
    suggested = _suggested_text(cur)
    if sent == suggested:
        return None, None
    return {
        "verb": "relay",
        "answer": sent,
        "origin": "operator",
        "operator_edited": True,
        "suggested_verb": cur.get("verb"),
        "suggested_text": suggested,
        "sent_text": sent,
    }, None


def _retire_decided(action_id: str) -> int:
    """Clear the bell rows for an action the operator has just decided in Pulse.

    ``escalations_only=False`` here and nowhere else. Automatic settlement is careful to leave
    `notify: all` informational notices alone — they are the operator's only record of what was
    done autonomously. But a deliberate Approve or Reject is the operator saying they are done
    with this action, so whatever was raised for it goes; leaving a row behind is exactly the
    second dismissal, in a second place, that #757 set out to remove.

    Still a retire rather than a delete: the row stays as the #760 "already told you" memo, so a
    decided-but-unchanged situation is not re-announced on the next pass (#800).
    """
    return notifications.retire_for_actions([action_id], escalations_only=False)


def _orchestrator_cfg() -> dict:
    """The orchestrator block for a projection, or `{}` when it cannot be read (never raises)."""
    try:
        return prefs.get_session_assistance()
    except Exception:  # noqa: BLE001 — a projection must not take the read down
        return {}


def _operator_projection(a: dict, cfg: dict, titles: dict[str, dict]) -> dict:
    """ONE pending action as every decision surface receives it. Never raises.

    `project_for_operator` decides the controls from the state. For a supervisor nudge that carries
    the exact text it will type (#983), two read-only facts ride beside it:

    * ``render_status`` — `actuator.render_status`, which asks the delivery path's own comparison
      whether that text and its provenance still hold. When it says no, ``can_approve`` is withdrawn
      here, so no surface offers a Send that `deliver` would answer with a stale 409;
    * ``objective_title`` — the objective's current title, so the row can name what it is about.
      Display only: a title is never an input to what is typed.

    `titles` caches one objectives read per mission for the length of one producer call.
    """
    from .. import automation

    out = {**a, **orchestrator_ledger.project_for_operator(a.get("state"))}
    try:
        cfg = prefs.get_automation_policy(automation.scope_of(a))
    except automation.AuthorityChanged:
        cfg = {"enabled": False, "autonomy": "off"}
        out["can_approve"] = False
    # STORE-ONLY, NEVER THE CHECKOUT'S GIT: this runs per pending nudge on every poll of both
    # producers, so `render_status` uses delivery's check without target resolution (no `.git`
    # reads, no `git status`). A checkout HEAD the agent moved with its own git is not visible here;
    # Approve runs the full check and refuses it as stale.
    status = actuator.render_status(a, cfg)
    if status is not None:
        out["render_status"] = status
        if not status["sendable"]:
            out["can_approve"] = False
    elif a.get("verb") != prefs.DRAFT_DIRECTION_VERB:
        return out
    # …and an AI-DRAFTED DIRECTION (#983 P3) is named by its objective the same way. Its text is the
    # stored `draft`, which never re-renders, so there is no status to project.
    mid = str(a.get("mission_id") or "")
    key = str(a.get("objective_key") or "")
    if mid and key:
        if mid not in titles:
            try:
                titles[mid] = {str(o.get("key")): o.get("title") for o in missions.objectives(mid)}
            except Exception:  # noqa: BLE001 — no title is an honest answer; the key still shows
                titles[mid] = {}
        title = titles[mid].get(key)
        if isinstance(title, str) and title.strip():
            out["objective_title"] = title
    return out


def _attach_pending(overview: dict) -> dict:
    """Give each card the live orchestrator action on its session, if any.

    The queue used to be a SECOND list beside the cards. Measured against the live stores, it
    was a strict subset — every action's session already appeared in "Needs you", and nothing
    was exclusive to it — so the operator read the same session twice, in two visual languages,
    with two different affordances. Merging them needs the action ON the card, and the card
    comes from the pulse cache while the action lives in the ledger.

    Server-side and read-only, for the same reason `_with_pending` is: the ledger states what is
    pending, never the model. Only LIVE states count — a delivered, expired or rejected action
    is history, not an errand.
    """
    cards = overview.get("cards")
    if not isinstance(cards, list):
        # An overview with no cards at all still has to surface live actions — that is exactly
        # the "no Pulse cache yet" case where they would otherwise be unreachable.
        cards = []
        overview["cards"] = cards
    # Which action ids the bell has ALREADY raised (#852/#840 §16). This cannot be computed from
    # ledger state: with `notify: "none"` nothing ever reaches the bell, so an escalated action's
    # only surface is the card, and demoting it unconditionally would bury the one decision with
    # nowhere else to appear. Read ONCE here rather than per card. The join is self-healing —
    # clear a bell row without deciding it and the card goes back to needing you.
    announced: set[str] = set()
    with contextlib.suppress(Exception):
        for row in notifications.listing().get("notifications") or []:
            aid = str(row.get("action_id") or "")
            if aid:
                announced.add(aid)

    live: dict[str, dict] = {}
    cfg = _orchestrator_cfg()
    titles: dict[str, dict] = {}
    with contextlib.suppress(Exception):
        # Retire overdue proposals FIRST. `live_actions` filters on persisted state and never
        # looks at `expires_at`, and the expiry sweep lived only in the sibling orchestrator
        # endpoint — which is fetched independently, so a card could offer Approve/Reject for a
        # proposal that had already timed out. Withdrawal of what can no longer be delivered rides
        # with it, through the one helper every retiring surface shares (#969).
        actuator.housekeep_pending()
        for a in orchestrator_ledger.live_actions():
            # `live_actions` includes `claimed`, which is an action already being delivered —
            # neither rejectable nor waiting on the operator. Overlaying one puts Approve/Reject
            # on a card for bytes that are already going out, and a non-polling page keeps them
            # there. The sibling `_pending_and_feed` already drew this line; both now read the
            # same set so they cannot drift apart again.
            # WHICH actions overlay a card is unchanged (`OPERATOR_PENDING_STATES`) — `claimed`
            # stays off this surface, as #777 decided, and the sibling decision list reads the
            # same set so the two cannot drift. What changes is WHAT MAY BE TAPPED once one is
            # here: that is the shared projection below, never this filter. The two questions
            # were conflated before, which is how `approved` ended up offering Approve.
            if a.get("state") not in orchestrator_ledger.OPERATOR_PENDING_STATES:
                continue
            sid = str(a.get("session_id") or "")
            # newest-first, so the first row seen per session is the current one.
            # PROJECTED HERE, once, so both producers below consume the same object. Doing it in
            # the card branch alone left the synthesized branch storing the raw ledger row — the
            # same "one contract, two derivations" drift the projection exists to end, reproduced
            # inside the very function that introduced it.
            if sid and sid not in live:
                live[sid] = {
                    **_operator_projection(a, cfg, titles),
                    "announced": str(a.get("id") or "") in announced,
                }
    # Settled history, one row per session (`feed_by_session`, #775). The Activity block used to
    # render this as a SECOND list of near-identical boxes directly above the cards — different
    # things (what the orchestrator did vs what your sessions are) that looked the same and sat
    # adjacent, so the page read as duplication (#777). It rides the card now.
    history: dict[str, dict] = {}
    with contextlib.suppress(Exception):
        for a in orchestrator_ledger.feed_by_session(FEED_LIMIT):
            sid = str(a.get("session_id") or "")
            if sid and sid not in live:
                history[sid] = a

    # WHICH MISSION HOLDS EACH SESSION — stamped here, on the card, because ownership is a
    # server fact and the client cannot derive it.
    #
    # The console's first version computed it by scanning the mission rows it happened to have
    # in memory. That list is PAGED (100 a page, server cap 200), so a session held by mission
    # 101 read as "held by nobody" and was offered ADOPT — a mutation the server then correctly
    # refused with 409. Ownership that depends on how far the operator has scrolled is not
    # ownership; one query answers it for every mission at once, loaded or not.
    #
    # Tri-state, not two: `None` means NO mission holds this session, and a card with no
    # `mission_id` key at all means the membership store could not be read. Those are different
    # answers and the client acts on them differently — the first offers ADOPT, the second
    # offers nothing, because advertising a control the backend will refuse is the defect this
    # whole stamp exists to remove. Absence is the honest encoding of "unreadable": there is no
    # sentinel string that cannot also be a real mission id.
    owner: dict[str, str] = {}
    memberships_known = True
    try:
        owner = missions.all_active_memberships()
    except Exception:
        memberships_known = False

    seen: set[str] = set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        seen.add(cid)
        # `mission_id` is set (id or None) only when the membership store answered; see above.
        c.pop("mission_id", None)
        if memberships_known:
            c["mission_id"] = owner.get(cid)
        # Strip BEFORE consulting the ledger, and unconditionally. The cache is written by a
        # scan and outlives the actions it saw, so a stale `pending_action` would otherwise
        # survive precisely when the ledger holds nothing live — the case where a card would
        # show decision controls for an action that no longer exists.
        c.pop("pending_action", None)
        a = live.get(str(c.get("id") or ""))
        if a:
            c["pending_action"] = a
            # A proposal awaiting the operator IS something that needs them, whether or not AI
            # review independently flagged the session. Without this a card could carry Approve
            # buttons while sitting under "Idle".
            #
            # Keep what the band WAS, so the client can put it back. Settling an action from a
            # card removes the controls immediately (the reconciling GET may fail), but without
            # this the session stays under "Needs you" until some later fetch succeeds — the
            # band outliving the reason for it.
            c["state_without_action"] = c.get("state")
            # Only an ACTIONABLE decision means the operator is needed. An `approved` action is
            # in flight (reject-only) and a `claimed` one cannot be touched at all, so banding
            # either as "needs you" asks for a decision that has already been made.
            if a["projection"] == orchestrator_ledger.ACTIONABLE:
                c["state"] = "needs_you"
        else:
            # No live action — show what the orchestrator last DID here instead. Never both:
            # a card with decision controls is about a choice you still have, and a settled
            # summary next to it would read as a second, contradictory status.
            c.pop("last_action", None)
            h = history.get(str(c.get("id") or ""))
            if h:
                c["last_action"] = h

    # An action with NO card would be unreachable now that the standalone queue is gone, and
    # that is not a hypothetical: `eligible_cards` builds with `window_days=None`, so the
    # orchestrator can act on a session outside Pulse's cached window — or before any scan has
    # produced a cache at all. The measured "every action's session already has a card" was
    # true of one moment, not an invariant, and the queue used to be the thing covering the gap.
    #
    # So synthesize a card from the action's own identity fields. It is the same information the
    # queue row carried, in the one place the operator now looks.
    #
    # LIVE actions only. #777 also synthesized for settled ones, so that removing the Activity
    # block would not lose the history of sessions outside the Pulse window. It did not lose
    # it — it turned it into cards, and the page went from 21 sessions to **102 cards**, with a
    # filter row full of scratch directories because a synthesized card carries whatever
    # `project` string its action stored (`/tmp/claude-1000/…`) rather than a real project ref.
    # A wall of 102 boxes is a worse answer than the duplication it replaced (#787).
    #
    # The line that matters is not "card or no card", it is **waiting on you or not**: something
    # awaiting the operator has to be reachable whatever its age, and history does not. So a
    # settled action rides the card of a session that has one (`last_action`, above) and
    # otherwise stays in the ledger, where it is bounded by the Pulse window like everything
    # else on this page.
    for sid, a in live.items():
        if sid in seen:
            continue
        seen.add(sid)
        project = str(a.get("project") or "")
        cards.append(
            {
                "id": sid,
                "engine": str(a.get("engine") or ""),
                "title": str(a.get("title") or sid),
                "cwd": "",
                "project": {
                    "kind": "project" if a.get("project_id") else "folder",
                    "id": str(a.get("project_id") or ""),
                    "name": project,
                },
                "state": "needs_you",
                # This card exists ONLY because the action does. Settle it and there is nothing
                # left to show, so the client drops the card rather than leaving an empty
                # phantom under "Needs you" with no title, no summary and no controls.
                "synthesized_for_action": True,
                "live": False,
                "last_activity": a.get("ts"),
                "intervention_required": False,
                "intervention_reason": "",
                "ai_summary": "",
                "synthesis": "",
                "pending_action": a,
                **({"mission_id": owner.get(sid)} if memberships_known else {}),
            }
        )
    return overview


def _with_pending(result: dict) -> dict:
    """Annotate each Ask match with the live orchestrator action on that session, if any.

    Server-supplied, never model-asserted — the same asymmetry `evidence_for` is built on: the
    model names a session, the server states the facts about it. A model asked "does this
    session need me?" can answer yes about one that needs nothing, and a false "something is
    waiting for you" is worse than silence: it sends the operator in to find nothing and teaches
    them to stop trusting the flag.

    Only LIVE states count. An expired, delivered or rejected action is history, not an errand.
    """
    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result
    live: dict[str, dict] = {}
    with contextlib.suppress(Exception):
        for a in orchestrator_ledger.live_actions():
            sid = str(a.get("session_id") or "")
            # `live_actions` is newest-first, so the first row seen per session is the current
            # one; later rows are older and must not overwrite it.
            if sid and sid not in live:
                live[sid] = a
    for m in matches:
        if not isinstance(m, dict):
            continue
        # Overwrite unconditionally: whatever the model may have put here is discarded.
        m.pop("pending", None)
        a = live.get(str(m.get("id") or ""))
        if a:
            m["pending"] = {
                "action_id": str(a.get("id") or ""),
                "state": str(a.get("state") or ""),
                "verb": str(a.get("verb") or ""),
            }
    return result


def pending_checked() -> list[dict]:
    """The operator-pending actions, PROJECTED, from ONE checked ledger snapshot.

    `_pending_and_feed` reads through `live_actions`, which turns an unreadable ledger into
    "no actions" — right for a feed, wrong for a worklist, where it silently drops every
    decision-only session. So this reads `latest_by_id_checked` once and derives the rows from
    that same snapshot: a separate health probe followed by the fail-soft read would race.
    """
    status, latest = orchestrator_ledger.latest_by_id_checked()
    if status != "ok":
        raise needs_you.LedgerUnavailable
    cfg = _orchestrator_cfg()
    titles: dict[str, dict] = {}
    return [
        _operator_projection(r, cfg, titles)
        for r in latest.values()
        if r.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES
    ]


def in_scope_cards(cards: list[dict]) -> list[dict]:
    """Roots + ``folder_exclusions`` — the TERMINAL's form of the boundary (#867).

    The Ask page lists these sessions and (#1086 Phase 3) acts on them, so a session the
    terminal would refuse to resume is neither named nor summarised here. The strict form
    (`honour_curation=False`) is the one resume uses; the list's looser form would let an
    adopted out-of-root session through to a surface that can type into it.
    """
    in_scope = _hard_scope_filter(honour_curation=False)
    return [
        c
        for c in cards
        if isinstance(c.get("cwd"), str) and in_scope(c["cwd"], c.get("project") or {"kind": ""})
    ]


def build_needs_you(
    wd: int, *, engine: str | None = None, project: str | None = None, strict: bool = False
) -> dict:
    """The NEEDS YOU payload for window ``wd`` — the route's read AND the notification sync's
    (#1086 Phase 4), so a notification can never be raised for a session the list would not show.
    Blocking; raises `needs_you.MembershipUnavailable` / `LedgerUnavailable`.

    ``strict`` (the notification sync, which RETRACTS on the answer — Hermes 5231): one checked
    engine walk shared by both card reads, metadata read under the writers' lock, and a checked
    dismissal read. Any of them incomplete raises `needs_you.ReadIncomplete`, never a list that
    silently lost a session or a flag. The list route stays fail-soft."""
    # ONE metadata snapshot for the strict read (#1882, Hermes on #1884): the cards are curated
    # through it AND reports are joined and judged through its aliases, so the two can never
    # disagree. Unreadable or malformed fails the read.
    snapshot: tuple | None = None
    if strict:
        try:
            snapshot = metadata.load_checked(strict_aliases=True)
        except metadata.MetadataUnreadable as e:
            raise needs_you.ReadIncomplete(f"session metadata: {e}") from e
    try:
        scanned = pulse.checked_scan() if strict else None
        # The fail-soft call is exactly the pre-#1086-P4 one; only the strict read adds arguments.
        cards = (
            pulse.build_cards(window_days=wd, strict=True, scanned=scanned, checked=snapshot)
            if strict
            else pulse.build_cards(window_days=wd)
        )
        dismissed = (
            needs_you_dismissals.suppressed_detail_checked()
            if strict
            else needs_you_dismissals.suppressed_detail()
        )
    except (pulse.CardsIncomplete, needs_you_dismissals.DismissalsUnreadable) as e:
        raise needs_you.ReadIncomplete(str(e)) from e
    # The same retirement every other decision read applies first (#969): an expired or
    # undeliverable proposal must not be offered as approvable here either.
    try:
        actuator.housekeep_pending()
    except Exception:  # noqa: BLE001 — `needs_you` also refuses a past-deadline action
        log.debug("needs-you: housekeeping failed", exc_info=True)
    pending = pending_checked()
    # The agents' own reports (#1882), keyed by PHYSICAL session. The strict read raises on an
    # unreadable store: unknown is not "nobody needs you".
    try:
        current_reports = agent_reports.current_checked() if strict else agent_reports.current_map()
    except agent_reports.StoreUnreadable as e:
        raise needs_you.ReadIncomplete(f"agent reports: {e}") from e
    # Reports are keyed by PHYSICAL session (the key the launch filed under), cards by the id the
    # engine knows them by — which can differ once an engine that mints its own id reconciles a
    # placeholder. So the two sides are compared as RESOLVED physical keys (#1882, Hermes on #1884),
    # through ONE alias snapshot used for card selection, report attachment and answerability. The
    # strict read takes it under the writers' lock and FAILS on an unreadable map: "no aliases"
    # would detach a placeholder-keyed report from its card, and the sync would retract it.
    aliases = snapshot[1] if snapshot is not None else metadata.load_aliases()
    reporting = {
        phys
        for phys, r in current_reports.items()
        if r.get("state") in agent_reports.NEEDS_YOU_STATES
    }
    # A LIVE DECISION IS LISTED WHATEVER THE WINDOW (#1086 Phase 3). The window scopes the
    # review flag's "needs you"; a decision the operator can still act on must never
    # vanish from the one surface that acts on it because the session is older than it.
    # The same holds for an agent's report asking for the Commander (#1882).
    in_window = {c["id"] for c in cards}
    wanted = {str(a.get("session_id") or "") for a in pending} - in_window
    wanted_phys = reporting - {engines.physical_key(c["id"], aliases) for c in cards}
    if wanted or wanted_phys:
        try:
            full = (
                pulse.build_cards(window_days=None, strict=True, scanned=scanned, checked=snapshot)
                if strict
                else pulse.build_cards(window_days=None)
            )
        except pulse.CardsIncomplete as e:
            raise needs_you.ReadIncomplete(str(e)) from e
        cards += [
            c
            for c in full
            if c["id"] not in in_window
            and (c["id"] in wanted or engines.physical_key(c["id"], aliases) in wanted_phys)
        ]
    cards = in_scope_cards(cards)
    try:
        held: set[str] | None = set(missions.all_active_memberships())
    except Exception:  # noqa: BLE001 — unreadable ownership is its own answer
        held = None
    out = needs_you.build(
        cards,
        pending,
        held,
        # The strict read raises on an unreadable screen; the fail-soft call is unchanged.
        observe=(
            (lambda row: orchestrator.observed_screen(engines.physical_key(row["id"]), strict=True))
            if strict
            else (lambda row: orchestrator.observed_screen(engines.physical_key(row["id"])))
        ),
        engine=engine,
        project=project,
        suppressed={k: v["fingerprint"] for k, v in dismissed.items()},
        strict=strict,
        reports=_reports_by_card(cards, current_reports, aliases),
        suppressed_reports={k: v["report_id"] for k, v in dismissed.items()},
        suppressed_actions={k: v["action_ids"] for k, v in dismissed.items()},
    )
    for row in out["rows"]:
        if row.get("report") is not None:
            try:
                stored = current_reports.get(engines.physical_key(row["id"], aliases))
            except Exception:  # noqa: BLE001 — unresolvable: the ledger half still answers
                stored = None
            row["report_answer"] = report_answerable(row, stored, aliases)
    out["window_days"] = wd
    return out


def _reports_by_card(
    cards: list[dict], current: dict[str, dict], aliases: dict[str, str] | None = None
) -> dict[str, dict]:
    """``card id -> its session's current report`` (public fields), through the physical key
    resolved with the caller's alias snapshot (``None``: read it, fail-soft)."""
    if not current:
        return {}
    out: dict[str, dict] = {}
    for c in cards:
        try:
            phys = engines.physical_key(c["id"], aliases)
        except Exception:  # noqa: BLE001, S112 — an unresolvable key has no report
            continue
        rep = current.get(phys)
        if rep is not None:
            out[c["id"]] = agent_reports.public(rep)
    return out


#: Why a report's options are disabled on its card (#1882). Fixed server text.
REPORT_ANSWER_OFF = (
    "Answering from here needs the orchestrator on (Settings → Mission control). Open the "
    "session to answer it."
)
REPORT_ANSWER_NOT_ACTUABLE = (
    "This agent doesn't take typed answers from BattleLab. Open the session to answer it."
)
REPORT_ANSWER_EXCLUDED = (
    "This session is excluded from the orchestrator. Open the session to answer it."
)
#: One answer per report (#1882): an answer holds the report's reservation
#: (`agent_reports.mark_answered`) or was claimed and did not settle as a clean refusal, so its
#: bytes may be on the screen.
REPORT_ANSWER_SENT = (
    "An answer to this was already sent or may have been — open the session to check."
)
#: The same fact as a refused tap's detail.
REPORT_ANSWER_SENT_DETAIL = agent_reports.ANSWER_SPENT
#: A refused tap's machine code (#1882): ``stale`` — nothing was typed by this tap, nor can an
#: earlier answer have been; ``spent`` — an answer to this report was already sent or may have
#: been. The client may say "Nothing was sent" ONLY for ``stale``.
REFUSED_STALE = "stale"
REFUSED_SPENT = "spent"
#: …and ``unknown``: whether an earlier answer was sent can't be read. Fails closed.
REFUSED_UNKNOWN = "unknown"
#: …and ``uncertain``: no record still held can show whether an answer went (the report aged out
#: of history, or is not one this server knows). Never said as "nothing was sent".
REFUSED_UNCERTAIN = "uncertain"
REPORT_ANSWER_UNCERTAIN = (
    "an answer to this report may already have been sent; its record is no longer held"
)
REPORT_ANSWER_UNKNOWN = (
    "The decision ledger can't be read, so an earlier answer can't be ruled out — open the "
    "session to check."
)


def _report_spent(report_id: str, stored: dict | None) -> bool:
    """Either source says an answer holds the report. Raises
    `orchestrator_ledger.LedgerUnreadable` when the ledger can't say."""
    if not report_id:
        return False
    mine = stored is not None and stored.get("id") == report_id
    if mine and agent_reports.answer_mark(stored):
        return True
    if orchestrator_ledger.report_answer_spent(report_id):
        return True
    # An ordinary answer or relay delivered to the session since the report was filed answered
    # it, whether or not its asynchronous end has committed yet (Hermes on #1884).
    return bool(
        mine
        and stored.get("phys_key")
        and orchestrator_ledger.answered_after(str(stored["phys_key"]), float(stored["at"]))
    )


def report_answerable(
    row: dict, stored: dict | None = None, aliases: dict[str, str] | None = None
) -> dict:
    """Can an option tap on this row's report be DELIVERED? ``{"available", "reason"}``.

    The same gates `actuator.deliver` applies before it types: the master switch and a tier that
    is not `off` (an operator's tap is consent to THIS action, not an exemption from the switch),
    an actuable engine and a session not opted out — and a report no earlier answer to was
    already sent, or may have been (one answer per report). A card is never offered as
    actionable when every tap would be refused. Never raises; unknown reads as unavailable."""
    try:
        report_id = str((row.get("report") or {}).get("id") or "")
        try:
            if _report_spent(report_id, stored):
                return {"available": False, "reason": REPORT_ANSWER_SENT}
        except orchestrator_ledger.LedgerUnreadable:
            return {"available": False, "reason": REPORT_ANSWER_UNKNOWN}
        cfg = prefs.get_automation_policy("session")
        if not cfg.get("enabled") or cfg.get("autonomy") == "off":
            return {"available": False, "reason": REPORT_ANSWER_OFF}
        prov, _native = engines.parse_key(str(row.get("id") or ""))
        if not engines.supports_orchestrator_input(prov):
            return {"available": False, "reason": REPORT_ANSWER_NOT_ACTUABLE}
        phys = engines.physical_key(str(row["id"]), aliases)
        idx = metadata.load()
        meta = idx.get(str(row["id"])) or idx.get(phys)
        if meta is not None and meta.orchestrator_excluded:
            return {"available": False, "reason": REPORT_ANSWER_EXCLUDED}
    except Exception:  # noqa: BLE001 — an unanswerable question is not a yes
        return {"available": False, "reason": REPORT_ANSWER_OFF}
    return {"available": True, "reason": ""}


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    _pending_checked = pending_checked
    _in_scope_cards = in_scope_cards

    def _working_keys() -> set[str]:
        # One implementation, in `actuator`, because `/api/missions/{id}/message` needs the same
        # overlay and two callers computing "what is busy" differently would propose against
        # different views of the world.
        return actuator.working_keys(registry)

    def _standalone_card(session_id: str) -> tuple[dict | None, tuple[int, str] | None]:
        """The card for a session the Ask page may act on, or ``(None, (status, reason))``.

        The same gates the NEEDS YOU list applies — a key that parses, a session that exists, the
        roots + exclusions boundary, and NOT held by a mission (decided in its console) — so these
        routes can never reach a session the list would not show. Blocking.
        """
        try:
            engines.parse_key(session_id)
        except Exception:  # noqa: BLE001 — a malformed id is simply not a session
            return None, (404, "unknown session")
        card = next((c for c in pulse.build_cards(window_days=None) if c["id"] == session_id), None)
        if card is None or not _in_scope_cards([card]):
            return None, (404, "unknown session")
        try:
            held = set(missions.all_active_memberships())
        except Exception:  # noqa: BLE001
            return None, (503, "mission membership could not be read")
        if session_id in held:
            return None, (409, "this session is decided in its mission")
        return card, None

    @app.get("/api/pulse/needs-you/{session_id:path}/details")
    async def needs_you_details(session_id: str, _user: str = Depends(logged_in)) -> JSONResponse:
        """What the Ask page's details modal shows (#1086 Phase 3). READ-ONLY, and no viewer is
        attached: the screen comes from the ring, so opening details can never make Approve refuse
        (#1049). Agent text (last words, screen, menu labels) is data, rendered as text."""

        def _read() -> tuple[int, dict]:
            card, err = _standalone_card(session_id)
            if err is not None:
                return err[0], {"detail": err[1]}
            observed = orchestrator.observed_screen(engines.physical_key(session_id))
            try:
                pending = [a for a in _pending_checked() if a.get("session_id") == session_id]
            except needs_you.LedgerUnavailable:
                return 503, {"detail": "the action ledger could not be read"}
            action = needs_you._pick_action(pending, time.time())
            public = needs_you._public_action(action) if action else None
            if (
                public is not None
                and action.get("verb") in EDITABLE_VERBS
                and not action.get("mission_id")
            ):
                public["editable"] = True
                public["suggested_text"] = _suggested_text(action)
            recorded = (public or {}).get("menu")
            return 200, {
                "id": session_id,
                "title": str(card.get("title") or ""),
                "engine": str(card.get("engine") or ""),
                "project": needs_you._project(card),
                "reason": str(card.get("intervention_reason") or ""),
                "last_words": review.last_words(session_id),
                "screen": observed["screen"],
                "prompt_class": observed["prompt_class"],
                "menu": recorded if isinstance(recorded, dict) else observed["menu"],
                "action": public,
            }

        status, body = await asyncio.to_thread(_read)
        return JSONResponse(body, status_code=status)

    @app.post("/api/pulse/needs-you/{session_id:path}/dismiss")
    async def needs_you_dismiss(
        session_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """DISMISS a NEEDS YOU row (#1086 Phase 3): hide it until the session's screen changes, and
        reject its pending decision when the body names one. The fingerprint is read HERE, from the
        live screen — a client can say which session, never which screen.

        The dismissal covers only what the operator SAW (#1882, Hermes on #1884): the body names
        the decisions (``shown_action_ids``) and the agent's report (``shown_report_id``) the row
        displayed. The server records a shown decision only while it is still pending and the
        shown report only while it is still current; anything that arrived after the operator
        looked stays visible. Omitted lists record nothing, so an older client can never hide
        more than the screen."""
        try:
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return JSONResponse({"detail": "the body must be JSON"}, status_code=422)
        allowed = {"action_id", "shown_action_ids", "shown_report_id"}
        if not isinstance(body, dict) or set(body) - allowed:
            return JSONResponse(
                {"detail": "the body may only name action_id, shown_action_ids, shown_report_id"},
                status_code=422,
            )
        action_id = body.get("action_id")
        if action_id is not None and not isinstance(action_id, str):
            return JSONResponse({"detail": "action_id must be a string"}, status_code=422)
        shown_ids = body.get("shown_action_ids", [])
        if (
            not isinstance(shown_ids, list)
            or len(shown_ids) > 20
            or not all(isinstance(a, str) and 0 < len(a) <= 64 for a in shown_ids)
        ):
            return JSONResponse(
                {"detail": "shown_action_ids must be a list of action ids"}, status_code=422
            )
        shown_report_id = body.get("shown_report_id")
        if shown_report_id is not None and (
            not isinstance(shown_report_id, str) or not 0 < len(shown_report_id) <= 64
        ):
            return JSONResponse({"detail": "shown_report_id must be a string"}, status_code=422)

        def _apply() -> tuple[int, dict]:
            card, err = _standalone_card(session_id)
            if err is not None:
                return err[0], {"detail": err[1]}
            rejected = None
            if action_id:
                status, cur = orchestrator_ledger.lookup(action_id)
                if status != "found" or cur is None or cur.get("session_id") != session_id:
                    return 404, {"detail": "unknown action for this session"}
                rejected = orchestrator_ledger.compare_and_set(
                    action_id, orchestrator_ledger.REJECTABLE_STATES, "rejected"
                )
                if rejected is not None:
                    with contextlib.suppress(Exception):
                        _retire_decided(action_id)
            phys = engines.physical_key(session_id)
            fp = orchestrator.observed_screen(phys)["fingerprint"]
            # The report the row SHOWED (#1882), recorded only while it is still the current one:
            # a newer report on the same screen is not dismissed. Unreadable reads as none, which
            # can only leave a later report visible.
            current = agent_reports.current(phys)
            report = (
                str(current["id"])
                if current and shown_report_id and current["id"] == shown_report_id
                else None
            )
            # …and the decisions it SHOWED that are still pending — never one that arrived after
            # the operator looked. Unreadable reads as none, the same direction.
            shown = set(shown_ids)
            carried = [
                str(a["id"])
                for a in orchestrator_ledger.live_actions()
                if a.get("session_id") == session_id
                and a.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES
                and a["id"] in shown
            ]
            needs_you_dismissals.dismiss(session_id, fp, report_id=report, action_ids=carried)
            return 200, {"dismissed": True, "rejected": rejected is not None}

        status, out = await asyncio.to_thread(_apply)
        return JSONResponse(out, status_code=status)

    # ---- an agent's own HQ report (#1882) ---------------------------------------------------

    @app.post("/api/needs-you/{session_id:path}/report/answer")
    async def report_answer(
        session_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Answer an agent's `needs_decision` report with one of ITS options (#1882).

        The tap mints a ledger `answer` action (``source: "report"``) whose text is
        server-rendered — ``Decision: <option>``, normalized by `handoff.sanitize_seed` as delivery
        normalizes it — bound to the report's id, its launch generation and the screen as it is
        now, and approves it in the same request: the tap IS the approval. It is delivered by the
        approve path (`_deliver_approved` → `actuator.deliver`), which re-checks the report after
        the claim and again under the write lock, so a report that moved on types nothing. The
        body names the report and the option NUMBER; the words come from the stored report."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict) or set(body) != {"report_id", "option"}:
            return JSONResponse({"detail": "the body names report_id and option"}, status_code=422)
        report_id, option = body["report_id"], body["option"]
        if not isinstance(report_id, str) or not report_id or len(report_id) > 64:
            return JSONResponse({"detail": "report_id must be a string"}, status_code=422)
        if isinstance(option, bool) or not isinstance(option, int):
            return JSONResponse({"detail": "option must be a number"}, status_code=422)

        def stale(detail: str) -> tuple[int, dict]:
            return 409, {"detail": detail, "code": REFUSED_STALE}

        def _gone_reply(phys: str) -> tuple[int, dict]:
            """The report is no longer current. "Nothing was sent" (``stale``) only on POSITIVE
            evidence from a record still held (#1882, Hermes on #1884): the report's own ended row,
            read strictly, showing it was never answered — no answer reservation, not ended by an
            answer, no ordinary delivery after it — and a strictly read ledger with no claimed
            answer for it. A report id found nowhere (aged out of history, or never ours) is
            ``uncertain``, never ``stale``; an unreadable store is ``unknown``."""
            try:
                cur = agent_reports.current_checked().get(phys)
                rows = agent_reports.history_checked(phys)
            except agent_reports.StoreUnreadable:
                return unknown_reply
            if cur is not None and cur.get("id") == report_id:
                # Current but not a decision: nothing to choose — unless something holds it.
                if agent_reports.answer_mark(cur):
                    return spent_reply
                return stale("that report is not a decision")
            ended = next((r for r in rows if r["id"] == report_id), None)
            if ended is None:
                return uncertain_reply
            verdict = agent_reports.answered_or_maybe(ended)
            if verdict == "spent":
                return spent_reply
            if verdict == "uncertain":
                return uncertain_reply
            try:
                if orchestrator_ledger.report_answer_spent(report_id):
                    return spent_reply
            except orchestrator_ledger.LedgerUnreadable:
                return unknown_reply
            return stale("the agent's report is no longer current")

        spent_reply = (409, {"detail": REPORT_ANSWER_SENT_DETAIL, "code": REFUSED_SPENT})
        unknown_reply = (503, {"detail": agent_reports.ANSWER_UNKNOWN, "code": REFUSED_UNKNOWN})
        uncertain_reply = (409, {"detail": REPORT_ANSWER_UNCERTAIN, "code": REFUSED_UNCERTAIN})

        def _mint() -> tuple[int, dict]:
            card, err = _standalone_card(session_id)
            if err is not None:
                return err[0], {"detail": err[1]}
            phys = engines.physical_key(session_id)
            try:
                rep = agent_reports.current_checked().get(phys)
            except agent_reports.StoreUnreadable:
                return 503, {"detail": "the agent-reports store could not be read"}
            if rep is None or rep.get("id") != report_id or rep.get("state") != "needs_decision":
                return _gone_reply(phys)
            options = agent_reports.public(rep).get("options") or []
            if not 1 <= option <= len(options):
                return 422, {"detail": "no such option on this report"}
            available = report_answerable({"id": session_id, "report": {"id": rep["id"]}}, rep)
            if available["reason"] == REPORT_ANSWER_SENT:
                return spent_reply
            if available["reason"] == REPORT_ANSWER_UNKNOWN:
                return unknown_reply
            if not available["available"]:
                return stale(available["reason"])
            try:
                text = handoff.sanitize_seed(f"Decision: {options[option - 1]}")
            except handoff.HandoffError:
                return 422, {"detail": "that option has no typeable text"}
            if not session_input.is_live(phys):
                return stale("the session is not running, so nothing can be typed")
            from .. import automation, template_secrets

            try:
                grant = automation.capture_candidates([session_id], None).get(session_id)
            except Exception:  # noqa: BLE001 — unreadable ownership authorizes nothing
                grant = None
            if grant is None:
                return 503, {"detail": "who owns this session could not be confirmed"}
            cfg = prefs.get_automation_policy("session")
            now = time.time()
            project = card.get("project") or {}
            rec = {
                "id": uuid.uuid4().hex,
                "state": "proposed",
                "ts": now,
                "expires_at": now + int(cfg["proposal_ttl_minutes"]) * 60,
                "tier": cfg["autonomy"],
                "source": "report",
                "report_id": rep["id"],
                "report_generation": str(rep.get("generation") or ""),
                "session_id": session_id,
                "engine": str(card.get("engine") or ""),
                "title": str(card.get("title") or "")[: pulse_chat.TITLE_MAX],
                "project": str(project.get("name") or "")[: pulse_chat.PROJECT_MAX],
                "project_id": project.get("id") or "",
                "verb": "answer",
                "answer": text,
                "confidence": 1.0,
                "rationale": "Your answer to the agent's question",
                "evidence": "none",
                "authority": grant,
                "precondition": orchestrator.precondition_for(phys),
            }

            # ONE ANSWER PER REPORT, reserved on the REPORT record before the action exists
            # (atomic under the fence and the store's lock, so two taps reserve it once, and
            # durable whatever the ledger compacts). Not inside the ledger lock: the reservation
            # takes the write fence, and nothing may take the fence under a lock a delivery's
            # write window could want.
            try:
                reserved = agent_reports.mark_answered(phys, rep["id"], rec["id"])
            except agent_reports.StoreUnreadable:
                return 503, {"detail": "the agent-reports store could not be read"}
            except agent_reports.Busy:
                return 503, {"detail": "a delivery is in progress; try again"}
            if reserved == "spent":
                return spent_reply
            if reserved != "ok":
                return _gone_reply(phys)

            spent = unknown = False

            def clean(r: dict) -> bool:
                nonlocal spent, unknown
                # …and the ledger's half, inside its lock: an answer claimed for this report.
                try:
                    if orchestrator_ledger.report_answer_spent(
                        r["report_id"], exclude=r["id"]
                    ) or orchestrator_ledger.answered_after(phys, float(rep["at"])):
                        spent = True
                        return False
                except orchestrator_ledger.LedgerUnreadable:
                    unknown = True
                    return False
                # The secret check at the write, inside the ledger lock (as HQ's proposals do).
                try:
                    return ask_store.guard_text(r["answer"], 4000) == r["answer"]
                except Exception:  # noqa: BLE001 — redaction that cannot run authorizes nothing
                    return False

            try:
                with template_secrets.registration_fence():
                    kept, _dropped = orchestrator_ledger.append_batch_for_free_sessions(
                        [rec], record_guard=clean
                    )
            except BaseException:
                agent_reports.clear_answer_quietly(phys, rep["id"], rec["id"])
                raise
            if not kept:
                # Never minted: give the reservation back (best-effort; a failure leaves the
                # report refusing, never double-typing).
                agent_reports.clear_answer_quietly(phys, rep["id"], rec["id"])
                if spent:
                    return spent_reply
                if unknown:
                    return unknown_reply
                return stale("that session already has a decision waiting")
            return 200, {"action_id": rec["id"]}

        status, out = await asyncio.to_thread(_mint)
        if status != 200:
            return JSONResponse(out, status_code=status)
        resp = await _deliver_approved(out["action_id"])
        if resp.status_code == 200:
            # WHAT HAPPENED, said explicitly (Hermes on #1884): the approve path answers 200 for
            # an attempted write too. `delivered` only when the PTY took every byte; `uncertain`
            # when a write was attempted and its outcome is unknown or partial (failed after the
            # claim, indeterminate); `stale` when it failed before the claim (nothing typed).
            rec = json.loads(resp.body)
            state = rec.get("state")
            if state == "delivered":
                outcome = "delivered"
            elif state == "indeterminate" or (state == "failed" and rec.get("claim_owner")):
                outcome = "uncertain"
            else:
                outcome = "stale"
            return JSONResponse({**rec, "outcome": outcome})
        if resp.status_code != 409:
            return resp
        # Which refusal: only THIS action settling `stale`/`expired` on a ground other than the
        # one-answer rule proves nothing was typed. Anything else (another caller had the action,
        # another answer holds the report) may have been sent.
        body = json.loads(resp.body)
        clean_refusal = body.get("state") in ("stale", "expired") and body.get("detail") not in (
            REPORT_ANSWER_SENT_DETAIL,
            agent_reports.ANSWER_UNKNOWN,
        )
        if body.get("detail") == agent_reports.ANSWER_UNKNOWN:
            body["code"] = REFUSED_UNKNOWN
        else:
            body["code"] = REFUSED_STALE if clean_refusal else REFUSED_SPENT
        return JSONResponse(body, status_code=409)

    @app.post("/api/needs-you/{session_id:path}/report/dismiss")
    async def report_dismiss(
        session_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """DISMISS an agent's report (#1882): it ends — into history — and nothing is typed. The
        body may name the ``report_id`` the card showed; a newer report is then left alone."""
        try:
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return JSONResponse({"detail": "the body must be JSON"}, status_code=422)
        if not isinstance(body, dict) or set(body) - {"report_id"}:
            return JSONResponse({"detail": "the body may only name report_id"}, status_code=422)
        report_id = body.get("report_id")
        if report_id is not None and not isinstance(report_id, str):
            return JSONResponse({"detail": "report_id must be a string"}, status_code=422)

        def _apply() -> tuple[int, dict]:
            _card, err = _standalone_card(session_id)
            if err is not None:
                return err[0], {"detail": err[1]}
            try:
                ended = agent_reports.end(
                    engines.physical_key(session_id), "dismissed", report_id=report_id or None
                )
            except agent_reports.StoreUnreadable:
                return 503, {"detail": "the agent-reports store could not be read"}
            except agent_reports.Busy:
                return 503, {"detail": "a delivery is in progress; try again"}
            return 200, {"dismissed": ended is not None}

        status, out = await asyncio.to_thread(_apply)
        return JSONResponse(out, status_code=status)

    @app.get("/api/pulse/recap")
    async def recent_work(
        window_days: int | None = None, _user: str = Depends(logged_in)
    ) -> JSONResponse:
        """RECENT WORK above Ask (#1086) — never calls the model. See `work_recap.read`."""
        wd = pulse.coerce_window_days(
            window_days if window_days is not None else prefs.get_pulse()["window_days"]
        )
        configured = bool(prefs.public_ai_review()["configured"])

        def _read() -> dict:
            cards = _in_scope_cards(work_recap.cards_for(wd))
            return work_recap.read(cards, window_days=wd, configured=configured)

        return JSONResponse(await asyncio.to_thread(_read))

    @app.post("/api/pulse/recap")
    async def refresh_recent_work(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Write a fresh RECENT WORK summary (one completion; a no-op when nothing changed).

        Single-flight under its own kind, so it never blocks a scan, an Ask or a pass. The body
        may name ``window_days`` (coerced like every read); anything else in it is ignored.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — an empty or non-JSON body means "the stored window"
            body = None
        raw = body.get("window_days") if isinstance(body, dict) else None
        wd = pulse.coerce_window_days(raw if raw is not None else prefs.get_pulse()["window_days"])
        try:
            async with aitasks.single_flight("work-recap", f"{wd}d"):
                cards = await asyncio.to_thread(lambda: _in_scope_cards(work_recap.cards_for(wd)))
                out = await work_recap.generate(cards, window_days=wd)
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "a recent-work summary is already being written"}, status_code=409
            )
        return JSONResponse(out)

    @app.get("/api/pulse/needs-you")
    async def needs_you_feed(
        window_days: int | None = None,
        engine: str | None = None,
        project: str | None = None,
        _user: str = Depends(logged_in),
    ) -> JSONResponse:
        """The Ask page's NEEDS YOU list (#1086) — sessions no mission holds that need the
        operator, newest first, with the one decision each row can settle. See `needs_you`.

        ``window_days`` defaults to the stored pref and is COERCED like every read of it (1–3).
        The filters are plain equality on the engine id and the project id; facets ride over the
        unfiltered set. It decides nothing. Its one write is the ledger housekeeping every
        decision read runs first (`actuator.housekeep_pending`: expire overdue actions, withdraw
        undeliverable ones, #969), so an expired proposal is never offered here. It runs off the
        event loop — it scans, reads the ledger and a live screen per shown row (#678).
        """
        wd = pulse.coerce_window_days(
            window_days if window_days is not None else prefs.get_pulse()["window_days"]
        )
        engine_f = (engine or "").strip()[:64] or None
        project_f = (project or "").strip()[:200] or None

        def _build() -> dict:
            return build_needs_you(wd, engine=engine_f, project=project_f)

        try:
            return JSONResponse(await asyncio.to_thread(_build))
        except needs_you.MembershipUnavailable:
            return JSONResponse(
                {"detail": "mission membership could not be read, so ownership is unknown"},
                status_code=503,
            )
        except needs_you.LedgerUnavailable:
            return JSONResponse(
                {"detail": "the action ledger could not be read, so pending decisions are unknown"},
                status_code=503,
            )

    @app.get("/api/pulse")
    async def get_pulse(_: str = Depends(logged_in)) -> JSONResponse:
        cached = pulse.load_cache()
        if cached is not None:
            return JSONResponse(await asyncio.to_thread(_attach_pending, cached))
        cfg = prefs.get_pulse()
        # The cache MISS branch needs the overlay just as much — more, in fact: before the first
        # scan there are no cards at all, so a live action has nothing to attach to and would be
        # unreachable. My earlier regression mocked an empty cached artifact rather than a miss,
        # so it never exercised this path.
        empty = pulse.empty_overview(cfg["window_days"], cfg["scan_depth"])
        return JSONResponse(await asyncio.to_thread(_attach_pending, empty))

    @app.post("/api/pulse/scan")
    async def scan_pulse(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        # Configured window/depth, overridable by an optional body (the page depth control).
        # A bad/absent body falls back to prefs — the scan is never blocked on a parse error.
        cfg = prefs.get_pulse()
        window_days, depth = cfg["window_days"], cfg["scan_depth"]
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                if "depth" in body:
                    depth = pulse.coerce_depth(body["depth"])
                if "window_days" in body:
                    window_days = pulse.coerce_window_days(body["window_days"])
        working = _working_keys()
        try:
            async with aitasks.single_flight("pulse-scan", "manual"):
                artifact = await pulse.run_scan(
                    window_days=window_days, depth=depth, working_keys=working
                )
        except aitasks.AlreadyRunning:
            # The only 409: another Pulse scan holds the single-flight. Hand back the live
            # activity so the page renders "scan already running", not a broken state.
            return JSONResponse(
                {"detail": "a mission control scan is already running", **aitasks.snapshot()},
                status_code=409,
            )
        # The same live overlay `GET /api/pulse` applies. A scan writes the CACHE, which has no
        # business holding ledger state — but the response the client swaps in must still carry
        # the pending actions, or running a scan silently strips every Approve/Dismiss control
        # from the page while the ledger still says they are pending.
        return JSONResponse(await asyncio.to_thread(_attach_pending, artifact))

    async def _ask_body(request: Request) -> tuple[str, object] | JSONResponse:
        """The ask's body, or the 422 that refuses it. Shared by both ask routes so their bounds
        cannot drift apart."""
        # Hand-rolled body parsing (like /scan): the bounds are the contract (#522) —
        # a missing/empty/oversized query is a 422 with a plain detail.
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str) or not query.strip():
            return JSONResponse({"detail": "query (string) is required"}, status_code=422)
        query = query.strip()
        if len(query) > pulse_chat.QUERY_MAX:
            return JSONResponse(
                {"detail": f"query too long (max {pulse_chat.QUERY_MAX} chars)"},
                status_code=422,
            )
        history = body.get("history") if isinstance(body, dict) else None
        return query, history

    def _ask_busy() -> JSONResponse:
        return JSONResponse(
            {"detail": "a question is already running", **aitasks.snapshot()},
            status_code=409,
        )

    def _ask_unconfigured() -> JSONResponse:
        # Contrast with /scan (which degrades to 200/fast): a chat has no non-LLM
        # fallback, so an unconfigured endpoint surfaces as a 409 the UI pre-gates on.
        return JSONResponse(
            {"detail": "AI endpoint is not configured", "configured": False},
            status_code=409,
        )

    @app.post("/api/pulse/ask")
    async def ask_pulse(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        parsed = await _ask_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        query, history = parsed
        try:
            # Separate kind from "pulse-scan" ON PURPOSE: an ask never blocks a scan (or
            # vice-versa); only concurrent ASKS serialize.
            async with aitasks.single_flight("pulse-chat", "ask"):
                result = await pulse_chat.ask(query, history, working_keys=_working_keys())
        except aitasks.AlreadyRunning:
            return _ask_busy()
        except review.NotConfiguredError:
            return _ask_unconfigured()
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return JSONResponse(await asyncio.to_thread(_with_pending, result))

    @app.post("/api/pulse/ask/stream", response_model=None)
    async def ask_pulse_stream(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse | StreamingResponse:
        """The same ask, as NDJSON events while it runs (#1171) — see ``pulse_chat.ask_events``.

        Everything that can be refused before any work is still an HTTP status: 422 for the body,
        409 when unconfigured or another question is running. Once the stream has started, a
        failure arrives as ONE final ``{"type": "error", "status", "detail"}`` line, since the
        200 is already on the wire.

        The single-flight lives INSIDE the generator, so its lifetime is exactly the stream's: a
        client that goes away cancels the generator and the ``finally`` releases it, and a stream
        that never starts never took it. The check here is what keeps the ordinary busy case a
        409; the one inside settles the race between two streams that both passed it.
        """
        parsed = await _ask_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed
        query, history = parsed
        try:
            review._require_config()
        except review.NotConfiguredError:
            return _ask_unconfigured()
        if aitasks.is_running("pulse-chat"):
            return _ask_busy()
        working = _working_keys()

        async def events():
            try:
                async with aitasks.single_flight("pulse-chat", "ask"):
                    async for ev in pulse_chat.ask_events(query, history, working_keys=working):
                        if ev["type"] == "answer":
                            ev = await asyncio.to_thread(_with_pending, ev)
                        yield json.dumps(ev) + "\n"
            except aitasks.AlreadyRunning:
                yield (
                    json.dumps(
                        {"type": "error", "status": 409, "detail": "a question is already running"}
                    )
                    + "\n"
                )
            except review.NotConfiguredError:
                yield (
                    json.dumps(
                        {"type": "error", "status": 409, "detail": "AI endpoint is not configured"}
                    )
                    + "\n"
                )
            except review.ReviewError as e:
                yield json.dumps({"type": "error", "status": 502, "detail": str(e)}) + "\n"

        return StreamingResponse(
            events(),
            media_type="application/x-ndjson",
            # Nothing between here and the browser may hold the lines back.
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- orchestrator (#726 Phase 1) ---------------------------------------------------
    # Pulse gains agency. These join the `/api/pulse/*` family on purpose rather than opening
    # an `/api/orchestrator/*` namespace: the operator-facing name is Pulse, and the existing
    # `/^\/api/` service-worker denylist entry already covers everything here.

    @app.get("/api/pulse/orchestrator")
    async def get_orchestrator_state(_: str = Depends(logged_in)) -> JSONResponse:
        """Cached state: config, pending actions, and the activity feed. NEVER runs a pass —
        same contract as `GET /api/pulse` (cache-only, instant)."""
        cfg = prefs.public_orchestrator()
        # Retiring rides with the read: a proposal whose session moved on leaves here on the next
        # poll, not at its TTL (#969). One helper for every retiring site. The session pane's
        # decision strip used to be this route's polling consumer until #1049 removed it; the
        # remaining consumer is `Orchestrator.tsx` (autonomy + health). The mission console's
        # decision rows ride the cards of `GET /api/pulse` instead.
        expired, _withdrawn = await asyncio.to_thread(actuator.housekeep_pending)
        pending, feed = await asyncio.to_thread(_pending_and_feed)
        return JSONResponse(
            {
                "config": cfg,
                "pending": pending,
                "feed": feed,
                "expired_now": len(expired),
                # The verbs the actuator can actually RENDER and deliver. Shipped rather than
                # duplicated client-side: the UI had its own hardcoded set that included
                # `dispatch`, which `render()` does not implement, so Approve was offered on
                # an action the server would always 409. One owner for the set, no drift.
                "delivering_verbs": sorted(actuator.RENDERABLE_VERBS),
                **aitasks.snapshot(),
            }
        )

    def _pending_and_feed() -> tuple[list[dict], list[dict]]:
        """`pending` (needs the operator) and `feed` (history) must be DISJOINT.

        The UI renders both lists, so a row appearing in each is shown twice — the same action
        under "Needs a decision" and again in the activity feed. Filtering the feed here rather
        than deduplicating in the client keeps the contract in one place; the previous shape
        only looked right because the e2e helper defaulted `feed` to empty, which hid it.
        """
        live = orchestrator_ledger.live_actions()
        # PROJECTED, the same merge `/api/pulse` applies to a card's `pending_action`. The session
        # pane's strip was this list's control surface from #948 P3 until #1049 removed it; the
        # projection still matters for every surface that renders one, because a raw
        # ledger row sent `ActionRow` to its legacy state guess: a low-confidence escalation with a
        # real `continue` verb offered Dismiss and no Approve, and an `approved` row offered an
        # Approve the server treats as a no-op (#959 review 4805, finding 1). The fields are derived
        # booleans on an authenticated read — which controls to offer, never what the action is.
        # …and a supervisor nudge's `render_status` / `objective_title` (#983 P2), through the same
        # helper `_attach_pending` uses, so this list and the mission console's rows agree.
        cfg = _orchestrator_cfg()
        titles: dict[str, dict] = {}
        pending = [
            _operator_projection(r, cfg, titles)
            for r in live
            if r.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES
        ]
        pending_ids = {r.get("id") for r in pending}
        # ONE row per session (#774) — see `orchestrator_ledger.feed_by_session`, which
        # collapses across the COMPLETE action set so `FEED_LIMIT` bounds sessions rather than
        # actions. Excluding the pending ids there rather than after keeps the pending/feed
        # disjointness contract: a pending action must never become somebody's visible latest.
        return pending, orchestrator_ledger.feed_by_session(FEED_LIMIT, exclude=set(pending_ids))

    @app.post("/api/pulse/orchestrate")
    async def run_orchestrator(
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Run one pass now. Its own single-flight kind so a pass never blocks a Pulse scan or
        an Ask (or vice-versa); only concurrent passes serialize.

        Deliberately contrasts with `/scan`, matching `/ask`: an unconfigured endpoint is a
        **409** and an endpoint failure a **502**. A scan degrades to fast curation because the
        page must still render; a *decision* has no useful non-LLM fallback, so it says so
        rather than returning an empty action list that reads as "nothing needs you".
        """
        try:
            async with aitasks.single_flight("orchestrator", "manual"):
                report = await orchestrator.run_pass(
                    working_keys=_working_keys(),
                    busy_keys=lambda: actuator.busy_keys(registry),
                )
                # A manual pass in `yolo` must deliver what it approved too — otherwise
                # "Run now" behaves differently from the scheduled sweep for no stated reason.
                await actuator.deliver_pass_actions(report["actions"], registry=registry)
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "an orchestrator pass is already running", **aitasks.snapshot()},
                status_code=409,
            )
        except review.NotConfiguredError:
            return JSONResponse(
                {"detail": "AI endpoint is not configured", "configured": False},
                status_code=409,
            )
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        except missions.MissionError as e:
            # The mission fence could not be evaluated (#871). A pass that cannot check whether a
            # session's mission was torn down proposes nothing, and says which store failed —
            # `Run now` returning a 500 would read as a bug in the pass rather than as a store
            # that is unavailable.
            return JSONResponse({"detail": str(e)}, status_code=getattr(e, "status", 503))
        pending, feed = await asyncio.to_thread(_pending_and_feed)
        # Carry the health record on the manual path too. `Run now` is what the operator is
        # told to click to force recovery, so it is exactly the request that clears a degraded
        # state — and a response that omits the record leaves the warning on screen after the
        # very pass that fixed it (#772). Read AFTER the `single_flight` block, so `aitasks`
        # has already written this run's outcome in its `finally`.
        return JSONResponse({**report, "pending": pending, "feed": feed, **aitasks.snapshot()})

    @app.post("/api/pulse/actions/{action_id}/approve")
    async def approve_action(
        action_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Approve one action and deliver it — compare-and-execute (#726 Phase 2).

        The precondition is re-verified INSIDE the delivery, immediately before the first byte,
        not here: a check that runs at approve time and a write that happens milliseconds later
        are two different moments, and `choose 1` into a screen that moved is exactly the
        failure this design exists to stop. A moved screen comes back `409 stale`.

        This is the ONE place `operator_approval` is set (#969): every approval surface posts
        here — since #1049 that is the mission console's `ActionRow` — and the operator's tap is
        what lets an attached viewer (usually the pane the operator opened to look before
        deciding) not count as someone else typing. It does not stop a repaint for a new width
        from moving the screen and refusing the approval as stale; that is #973.
        """
        # OPTIONAL edited text (#1086 Phase 3). A body-less approve is exactly what it always was.
        try:
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else None
        except ValueError:
            return JSONResponse({"detail": "the body must be JSON"}, status_code=422)
        edit = None
        if body is not None:
            if not isinstance(body, dict) or set(body) - {"text"}:
                return JSONResponse({"detail": "the body may only carry text"}, status_code=422)
            if "text" in body:
                edit, refused = await asyncio.to_thread(_operator_edit, action_id, body["text"])
                if refused is not None:
                    return JSONResponse({"detail": refused[1]}, status_code=refused[0])
        return await _deliver_approved(action_id, edit)

    async def _deliver_approved(action_id: str, edit: dict | None = None) -> JSONResponse:
        """THE approve path: the operator's tap delivers ``action_id`` through `actuator.deliver`
        (``operator_approval=True``) and retires its alert. Shared by the approve route and a
        report option's tap (#1882), so there is one way an approval reaches a session."""
        try:
            rec = await actuator.deliver(
                action_id, registry=registry, operator_approval=True, edit=edit
            )
        except actuator.NotDeliverable as e:
            return JSONResponse({"detail": str(e)}, status_code=409)
        if rec.get("state") in ("stale", "expired"):
            return JSONResponse(
                {"detail": rec.get("detail") or "the session moved on", **rec}, status_code=409
            )
        # Delivering it retires the alert too. Strictly after a terminal delivery — the 409
        # stale/expired path above returns first, and `NotDeliverable` never reaches here, so a
        # failed approval can never clear the operator's only pointer to an action still live.
        #
        # RETIRE, never delete (#800). This used to call `dismiss_for_action`, which physically
        # removed the row — and the row is also the "already told you" memo that stops one
        # unresolved situation being announced every TTL (#760). Deleting it on a manual decision
        # therefore restarted that loop for exactly the actions the operator had dealt with.
        # Belt-and-braces with the ledger's own settlement hook: `deliver` normally settles
        # through a CAS that retires this already, but the route must not depend on which
        # internal path produced the record it is about to return.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_retire_decided, action_id)
        return JSONResponse(rec)

    @app.post("/api/pulse/actions/{action_id}/choose")
    async def choose_from_menu(
        action_id: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Answer an escalated menu with one of its options (#1060 Phase 3). See `menu_answer`.

        The body names the option NUMBER and the LABEL the card showed; both must match the menu
        the escalation recorded and the menu parsed from the live screen now. The payload is the
        digit alone and is delivered through `actuator.deliver`, like an approval: the operator's
        tap is the approval, so an attached viewer does not refuse it (`operator_approval`).
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"detail": "a JSON object is required"}, status_code=422)
        try:
            rec = await menu_answer.answer(
                action_id, body.get("option"), body.get("label"), registry=registry
            )
        except menu_answer.Refused as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)
        except menu_answer.Indeterminate as e:
            return JSONResponse(
                {"detail": e.detail, "state": "indeterminate", "action_id": e.action_id},
                status_code=502,
            )
        # The escalation is answered: retire its alert as an approval retires a proposal's.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_retire_decided, action_id)
        return JSONResponse(rec)

    @app.post("/api/pulse/actions/{action_id}/reject")
    async def reject_action(
        action_id: str,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Decline an action. Terminal — the ledger keeps it as history, and the next pass is
        free to propose something else for that session.

        Compare-and-swap, not a blind write. A plain `transition(..., "rejected")` accepted ANY
        current state, which broke in two directions: a stale tab could overwrite `delivered`
        with `rejected`, so the feed claimed nothing was sent when it had been; and a reject
        racing an in-flight delivery produced `claimed -> rejected -> delivered`, returning 200
        "rejected" while the bytes were already on their way to the PTY. Rejection is only
        meaningful while the action is still WAITING, so that is the only thing it may move.
        """
        rec = await asyncio.to_thread(
            orchestrator_ledger.compare_and_set,
            action_id,
            orchestrator_ledger.REJECTABLE_STATES,
            "rejected",
        )
        if rec is not None:
            # Deciding it here retires the alert too — the operator must not have to dismiss it
            # a second time, in a second place. Strictly after a successful CAS: the 404 and 409
            # paths below must never clear an alert for an action still live or already
            # delivered. Retire rather than delete, for the #760 reason spelled out on the
            # approve path above.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_retire_decided, action_id)
            return JSONResponse(rec)
        # Distinguish "never existed" from "too late" — the operator needs to know which.
        cur = await asyncio.to_thread(orchestrator_ledger.get, action_id)
        if cur is None:
            return JSONResponse({"detail": "unknown action"}, status_code=404)
        return JSONResponse(
            {
                "detail": (
                    "that action is already being delivered"
                    if cur.get("state") == "claimed"
                    else f"that action is already {cur.get('state')}"
                ),
                **cur,
            },
            status_code=409,
        )

    @app.post("/api/pulse/chat")
    async def orchestrator_chat_route(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """The Pulse chat that can act (#726 Phase 4) — retrieval, instructions, and "what did
        you do". Its own single-flight kind so a chat turn never blocks a scheduled pass.

        An instruction here produces PROPOSALS through the same verb path a scheduled pass
        uses; it is not a privileged write channel. Two paths to a PTY would mean two sets of
        guards, and the newer one would be the weaker.
        """
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str) or not query.strip():
            return JSONResponse({"detail": "query (string) is required"}, status_code=422)
        query = query.strip()
        if len(query) > orchestrator_chat.QUERY_MAX:
            return JSONResponse(
                {"detail": f"query too long (max {orchestrator_chat.QUERY_MAX} chars)"},
                status_code=422,
            )
        history = body.get("history") if isinstance(body, dict) else None
        try:
            async with aitasks.single_flight("pulse-chat", "orchestrate"):
                result = await orchestrator_chat.ask(query, history, working_keys=_working_keys())
                # A chat instruction under `yolo` produces `approved` records exactly as a pass
                # does, so it must DELIVER them exactly as a pass does. Without this the "chat
                # that can act" hands back an approved action that nothing ever picks up: the
                # scheduled and manual sweeps only deliver the records their own `run_pass()`
                # produced, and the chat's live action makes that session ineligible for them —
                # so it sits until a manual tap or expiry. That is the inert-`yolo` condition
                # `deliver_pass_actions` exists to remove for the other two entry points.
                #
                # Inside the single-flight, so a concurrent pass cannot interleave with the
                # delivery of what this turn just approved.
                # ONLY an instruction dispatches. `ask()` overloads `actions`: for `instruct`
                # it holds what this turn created, but for `history` it holds recent LEDGER
                # ROWS shown for audit. Dispatching unconditionally meant a read-only question
                # ("what did you do?") could hand an old `approved` row to the actuator and,
                # under `yolo`, type it into the session. A question must never cause a write.
                if result.get("intent") == "instruct":
                    await actuator.deliver_pass_actions(
                        result.get("actions") or [], registry=registry
                    )
                # Re-read each action from the LEDGER, not from what the helper returned.
                # `deliver_pass_actions` deliberately omits an action another caller already
                # claimed or settled (`deliver_auto` returns None, or `deliver` raises
                # NotDeliverable), and this route persists the record BEFORE awaiting delivery
                # while approve/delivery callers are not fenced by the chat single-flight. So a
                # racing winner can settle the action while its id is absent from the helper's
                # list — and reporting the pre-delivery row would tell the operator a tap is
                # still needed for something already delivered.
                #
                # The ledger is the authority on state; the helper only reports what IT did.
                actions = result.get("actions") or []
                if result.get("intent") == "instruct" and actions:
                    latest = await asyncio.to_thread(
                        lambda ids: {i: orchestrator_ledger.get(i) for i in ids},
                        [a["id"] for a in actions if a.get("id")],
                    )
                    result["actions"] = [latest.get(a.get("id")) or a for a in actions]
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "a question is already running", **aitasks.snapshot()},
                status_code=409,
            )
        except review.NotConfiguredError:
            return JSONResponse(
                {"detail": "AI endpoint is not configured", "configured": False},
                status_code=409,
            )
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return JSONResponse(result)

    # --- notifications + Web Push (#726 Phase 3) --------------------------------------
    # In-app first: the bell always works. Push is the extra that wakes the operator when the
    # tab is closed, and its absence must never mean an escalation goes unheard.

    @app.get("/api/pulse/notifications")
    async def get_notifications(_: str = Depends(logged_in)) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(notifications.listing))

    @app.post("/api/pulse/notifications/read")
    async def mark_notifications_read(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        ids: list[str] | None = None
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("ids"), list):
                ids = [i for i in body["ids"] if isinstance(i, str)]
        n = await asyncio.to_thread(notifications.mark_read, ids)
        return JSONResponse({"marked": n, **await asyncio.to_thread(notifications.listing)})

    @app.post("/api/pulse/notifications/clear-settled")
    async def clear_settled_notifications(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Hide the settled rows the client displayed. Returns how many left the projection.

        A **hide**, never a delete, and it may only touch rows already settled. Removing them
        would destroy the #760 "already told you" memo and restart the re-announce loop for
        exactly the actions the operator has just dealt with — #800's bug through the front door.

        **`ids` is required: it is the snapshot the operator was looking at.** Clearing "the
        settled window" as recomputed at POST time silently swallows anything that settled
        between the GET that drew the list and the click — hiding a decision the operator never
        saw, permanently, since hidden is what keeps a row out of every later projection. The
        window is bounded (newest 10 / 24h), so it turns over on its own; this is not a rare
        interleaving.

        Required rather than optional-with-a-fallback because nothing calls this yet — the bell's
        client lands with the console (#840 Phase 2). An unsnapshotted branch kept "for
        compatibility" would be a live footgun with no user to justify it; better it never
        exists.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is a 400, not a 500
            body = None
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return JSONResponse(
                {"detail": "ids must be the list of settled notification ids you displayed"},
                status_code=400,
            )
        n = await asyncio.to_thread(notifications.clear_settled, ids)
        return JSONResponse({"cleared": n})

    @app.post("/api/pulse/notifications/dismiss")
    async def dismiss_notifications(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Remove rows from the bell — the given ids, or every one when none are named.

        A DELETE of the operator's own alerts, not a second read-flag: "mark read" answers
        *have I seen this*, which is a different question from *is this still on my list*. The
        bell had no answer to the second one at all, so a saturated ring could only be emptied
        by waiting for 200 newer alerts to evict it.
        """
        body: object = None
        with contextlib.suppress(Exception):
            body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"detail": "expected an object"}, status_code=422)

        ids: list[str] | None
        if body.get("all") is True:
            ids = None
        elif isinstance(body.get("ids"), list) and all(isinstance(i, str) for i in body["ids"]):
            ids = list(body["ids"])
        else:
            # Fails CLOSED, unlike `/read` above. That route coerces a missing or malformed body
            # to `None` meaning "every row", which is harmless for a read-flag and a footgun for
            # a delete: `{"ids": "n1"}` — a plausible client typo — would empty the whole bell.
            # Deleting everything has to be asked for in as many words.
            return JSONResponse({"detail": 'send {"ids": [...]} or {"all": true}'}, status_code=422)

        n = await asyncio.to_thread(notifications.dismiss, ids)
        return JSONResponse({"dismissed": n, **await asyncio.to_thread(notifications.listing)})

    @app.get("/api/pulse/push/key")
    async def get_push_key(_: str = Depends(logged_in)) -> JSONResponse:
        """The VAPID PUBLIC key. The private half never leaves the server."""
        return JSONResponse(
            {
                "public_key": await asyncio.to_thread(webpush.public_key),
                "subscriptions": await asyncio.to_thread(notifications.list_subscriptions),
            }
        )

    @app.post("/api/pulse/push/subscribe")
    async def push_subscribe(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"detail": "invalid JSON"}, status_code=422)
        sub = body.get("subscription") if isinstance(body, dict) else None
        try:
            public = await asyncio.to_thread(notifications.subscribe, sub or {})
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=422)
        # The echo is the PUBLIC view: an opaque id and the endpoint's ORIGIN. The endpoint
        # itself is a per-device capability and never travels back to a client.
        return JSONResponse(public)

    @app.post("/api/pulse/push/unsubscribe")
    async def push_unsubscribe(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        sub_id = ""
        with contextlib.suppress(Exception):
            body = await request.json()
            if isinstance(body, dict):
                sub_id = str(body.get("id") or "")
        removed = await asyncio.to_thread(notifications.unsubscribe, sub_id)
        return JSONResponse(
            {
                "removed": removed,
                "subscriptions": await asyncio.to_thread(notifications.list_subscriptions),
            }
        )

    @app.post("/api/sessions/{sid}/orchestrator-exclude")
    async def toggle_orchestrator_exclude(
        sid: str,
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Withdraw (or restore) the orchestrator's agency over ONE session (#726).

        A dedicated toggle mirroring `POST /api/sessions/{sid}/review-exclude` rather than a
        `PATCH …/metadata` write: that route is project_id-only by contract (it 422s without
        one), and widening it would change a shared surface for an unrelated concern.

        This is NOT `review_excluded`. An unmanaged session stays listed, stays summarised,
        stays flagged needs-you — it only stops being something the orchestrator may act on.
        """
        try:
            key = engines.canonical_key(sid)
        except engines.EngineError:
            raise HTTPException(status_code=404, detail="unknown session") from None
        # Optional body {"excluded": bool}; absent/invalid → toggle the stored state.
        desired: bool | None = None
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("excluded"), bool):
                desired = body["excluded"]
        # Write against the RESOLVED sidecar key, like review-exclude: for a reconciled
        # opencode session the sidecar lives under the placeholder physical key.
        mkey = metadata.resolve_key(key)
        # Under the write fence (#726): `check_precondition` reads this field in the final
        # guard, which runs BEFORE the write lock is taken, so an opt-out landing in that
        # window used to be invisible to the fence and the session still received input.
        # Transacting the read-modify-write here means an in-flight send either finishes
        # first or sees the bumped session epoch and refuses. Keyed on the PHYSICAL session
        # key, which is what the fence compares.
        phys = engines.physical_key(key)

        # OFF THE LOOP. The fence is a cross-process `flock` with a mutation budget measured in
        # seconds, and `session_transaction` polls it synchronously — entering that on the event
        # loop stalls every other request for the whole budget while a sibling holds it. The
        # mission routes already moved this exact work to a worker; this path is the same class
        # and had been missed (#888 review, finding 2).
        def _apply() -> object:
            with session_input.session_transaction(phys):
                want = not metadata.get(mkey).orchestrator_excluded if desired is None else desired
                return metadata.patch(mkey, orchestrator_excluded=want)

        try:
            m = await asyncio.to_thread(_apply)
        except session_input.AuthorityFenceBusy:
            # Deliberate and retryable: the withdrawal was NOT applied, and saying so is better
            # than applying it without the ordering that makes it mean anything.
            raise HTTPException(
                status_code=503,
                detail="the authorization fence is busy; retry",
            ) from None
        return JSONResponse({"id": key, "orchestrator_excluded": m.orchestrator_excluded})

    @app.get("/api/pulse/evidence/{session_id:path}")
    async def get_evidence(
        session_id: str,
        request: Request,
        _user: str = Depends(logged_in),
    ) -> JSONResponse:
        """Server-pulled evidence for one session: the live screen, a transcript tail, or the
        recap. The model only ever names a *kind*; every byte here comes from the real session,
        fetched now — a model that can quote a screen can invent one.

        Never cached and never persisted into the ledger, so the operator always reads the
        current screen rather than a frozen one.
        """
        try:
            engines.parse_key(session_id)
        except Exception:
            return JSONResponse({"detail": "unknown session id"}, status_code=404)
        kind = request.query_params.get("kind", "screen")
        if kind not in orchestrator.EVIDENCE_KINDS:
            return JSONResponse(
                {"detail": f"kind must be one of {list(orchestrator.EVIDENCE_KINDS)}"},
                status_code=422,
            )
        # Blocking: the ring replay + FS reads must never run on the event loop (#678).
        result = await asyncio.to_thread(orchestrator.evidence_for, session_id, kind)
        # This response carries live terminal / transcript content, and its whole contract is
        # "what the session shows RIGHT NOW". A cached copy is both a stale-evidence hazard
        # (approving against a screen that has moved) and a data-exposure one (session content
        # sitting in a disk cache). Deny caching explicitly rather than relying on defaults.
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )
