"""Pulse routes (#441 Phase 2): the cached recent-work overview + manual scan.

* ``GET  /api/pulse`` — the cached overview artifact, served instantly; it NEVER triggers a
  scan. Returns the "never scanned" empty overview (at the configured window/depth) before the
  first scan (or on a cache miss).
* ``POST /api/pulse/scan`` — run one scan now and return the fresh artifact. Uses the configured
  ``pulse`` window/depth (#441 Phase 3), overridable per-request by an optional JSON body
  ``{"depth": …, "window_days": …}`` (the page's depth control). The single ``409`` case is
  "a Pulse scan is already running" (single-flight, #441 Phase 1) — its body carries the live
  AI-activity snapshot so the UI shows the running scan, not an error. An **unconfigured AI
  gateway never 409s here**: depth ≥ medium degrades to ``fast`` curation and returns **200**
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

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import (
    actuator,
    aitasks,
    engines,
    metadata,
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
)

# How many ledger rows the activity feed carries. Bounded so a long-lived install's
# history can't make the Pulse page payload grow without limit.
FEED_LIMIT = 100


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
    live: dict[str, dict] = {}
    with contextlib.suppress(Exception):
        # Retire overdue proposals FIRST. `live_actions` filters on persisted state and never
        # looks at `expires_at`, and the expiry sweep lived only in the sibling orchestrator
        # endpoint — which is fetched independently, so a card could offer Approve/Reject for a
        # proposal that had already timed out.
        orchestrator_ledger.expire_due()
        for a in orchestrator_ledger.live_actions():
            # `live_actions` includes `claimed`, which is an action already being delivered —
            # neither rejectable nor waiting on the operator. Overlaying one puts Approve/Reject
            # on a card for bytes that are already going out, and a non-polling page keeps them
            # there. The sibling `_pending_and_feed` already drew this line; both now read the
            # same set so they cannot drift apart again.
            if a.get("state") not in orchestrator_ledger.OPERATOR_PENDING_STATES:
                continue
            sid = str(a.get("session_id") or "")
            # newest-first, so the first row seen per session is the current one
            if sid and sid not in live:
                live[sid] = a
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

    seen: set[str] = set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        seen.add(str(c.get("id") or ""))
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
    # The same applies to a SETTLED action whose session has no card, and it is not a rare
    # corner: measured live, 16 sessions were inside the 3-day Pulse window while the feed
    # reached 100 — so 84 sessions had history and no card. Dropping the Activity block without
    # carrying those across would not have merged the two lists, it would have deleted one.
    for sid, a in list(live.items()) + list(history.items()):
        if sid in seen:
            continue
        seen.add(sid)
        settled = sid not in live
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
                # A settled action is history, not an errand — banding it `needs_you` would
                # put week-old expired escalations at the top of the list.
                "state": "idle" if settled else "needs_you",
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
                **({"last_action": a} if settled else {"pending_action": a}),
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


def register(app: FastAPI, *, logged_in, csrf_guard, registry=None) -> None:
    def _working_keys() -> set[str]:
        # Live "in flight" overlay: a session is live if its server-owned stream has recent
        # output (working) or a viewer is attached. Match either the logical or physical key
        # (a reconciled opencode session registers under its placeholder). Best-effort — a
        # registry hiccup must never fail the scan, it just yields no live overlay.
        if registry is None:
            return set()
        keys: set[str] = set()
        with contextlib.suppress(Exception):
            for r in registry.snapshot():
                if r.get("working") or r.get("attached"):
                    keys.add(r["id"])
        return keys

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
                {"detail": "a Pulse scan is already running", **aitasks.snapshot()},
                status_code=409,
            )
        # The same live overlay `GET /api/pulse` applies. A scan writes the CACHE, which has no
        # business holding ledger state — but the response the client swaps in must still carry
        # the pending actions, or running a scan silently strips every Approve/Dismiss control
        # from the page while the ledger still says they are pending.
        return JSONResponse(await asyncio.to_thread(_attach_pending, artifact))

    @app.post("/api/pulse/ask")
    async def ask_pulse(
        request: Request,
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
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
        try:
            # Separate kind from "pulse-scan" ON PURPOSE: an ask never blocks a scan (or
            # vice-versa); only concurrent ASKS serialize.
            async with aitasks.single_flight("pulse-chat", "ask"):
                result = await pulse_chat.ask(query, history, working_keys=_working_keys())
        except aitasks.AlreadyRunning:
            return JSONResponse(
                {"detail": "a question is already running", **aitasks.snapshot()},
                status_code=409,
            )
        except review.NotConfiguredError:
            # Contrast with /scan (which degrades to 200/fast): a chat has no non-LLM
            # fallback, so an unconfigured endpoint surfaces as a 409 the UI pre-gates on.
            return JSONResponse(
                {"detail": "AI endpoint is not configured", "configured": False},
                status_code=409,
            )
        except review.ReviewError as e:
            return JSONResponse({"detail": str(e)}, status_code=502)
        return JSONResponse(await asyncio.to_thread(_with_pending, result))

    # --- orchestrator (#726 Phase 1) ---------------------------------------------------
    # Pulse gains agency. These join the `/api/pulse/*` family on purpose rather than opening
    # an `/api/orchestrator/*` namespace: the operator-facing name is Pulse, and the existing
    # `/^\/api/` service-worker denylist entry already covers everything here.

    @app.get("/api/pulse/orchestrator")
    async def get_orchestrator_state(_: str = Depends(logged_in)) -> JSONResponse:
        """Cached state: config, pending actions, and the activity feed. NEVER runs a pass —
        same contract as `GET /api/pulse` (cache-only, instant)."""
        cfg = prefs.public_orchestrator()
        expired = await asyncio.to_thread(orchestrator_ledger.expire_due)
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
        pending = [r for r in live if r.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES]
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
                report = await orchestrator.run_pass(working_keys=_working_keys())
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
        _user: str = Depends(logged_in),
        _csrf: None = Depends(csrf_guard),
    ) -> JSONResponse:
        """Approve one action and deliver it — compare-and-execute (#726 Phase 2).

        The precondition is re-verified INSIDE the delivery, immediately before the first byte,
        not here: a check that runs at approve time and a write that happens milliseconds later
        are two different moments, and `choose 1` into a screen that moved is exactly the
        failure this design exists to stop. A moved screen comes back `409 stale`.
        """
        try:
            rec = await actuator.deliver(action_id, registry=registry)
        except actuator.NotDeliverable as e:
            return JSONResponse({"detail": str(e)}, status_code=409)
        if rec.get("state") in ("stale", "expired"):
            return JSONResponse(
                {"detail": rec.get("detail") or "the session moved on", **rec}, status_code=409
            )
        # Delivering it retires the alert too. Rejecting already did this; approving did not, so
        # a successfully delivered escalation stayed in the bell and still needed the second
        # manual dismissal this change exists to remove. Strictly after a terminal delivery —
        # the 409 stale/expired path above returns first, and `NotDeliverable` never reaches
        # here, so a failed approval can never destroy the operator's only pointer to an action
        # that is still live.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(notifications.dismiss_for_action, action_id)
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
            # Deciding it here retires the alert too. The bell and the ledger are separate
            # stores, so without this the operator dealt with the escalation and then had to
            # dismiss it a second time, in a second place. Strictly after a successful CAS —
            # the 404 and 409 paths below must never destroy an alert for an action that is
            # still live or already delivered.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(notifications.dismiss_for_action, action_id)
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
        with session_input.session_transaction(phys):
            if desired is None:
                desired = not metadata.get(mkey).orchestrator_excluded
            m = metadata.patch(mkey, orchestrator_excluded=desired)
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
