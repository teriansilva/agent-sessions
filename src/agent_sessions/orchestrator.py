"""Pulse orchestrator — the decision layer (#726 Phase 1).

Pulse observes; this decides. One bounded ``review.complete_json`` call per pass turns the
curated session set into a list of *proposals*: for each session that needs something, a verb
from a closed set, a confidence, and a rationale. **Phase 1 writes nothing to any PTY** — the
proposals land in the ledger and render on the page as "would send". Phase 2 adds delivery.

The safety posture is the whole design, so it is worth stating plainly:

* **The model never authors terminal bytes.** It names a *verb*; the server renders the
  keystrokes. ``continue`` sends an operator-owned nudge template the model cannot influence;
  ``choose`` sends a validated digit. Same discipline as ``autosort`` (``{project_id,
  confidence}``) and ``handoff`` (model output re-rendered by us, never emitted verbatim).
* **Session content is untrusted input.** Every transcript and screen the model sees is
  *output from the agents being watched* — an agent can print anything, including text shaped
  like an instruction. So an id is only usable if it appears in the slice actually sent this
  pass (never the whole catalog), every id is shape-checked through ``engines.parse_key``, and
  every free-text field is length-capped and rendered as plain text by the UI.
* **Two gates decide who can even be named.** Non-actuable engines (``shell`` — an agentless
  ``bash -l`` where a nudge would *execute*) and per-session ``orchestrator_excluded`` opt-outs
  are filtered out **before** the digest is built. An id the model never sees is an id it
  cannot name; Phase 2 re-checks both at the write boundary anyway.
* **A proposal is a claim about a screen.** Each one binds a precondition — physical key,
  screen fingerprint, prompt class, expiry — captured at pass time, so Phase 2's approve path
  can verify the screen still holds before delivering. ``choose 1`` against a *different*
  prompt is the failure mode this exists to stop.

An unconfigured endpoint is not an error here: :func:`run_pass` raises
:class:`review.NotConfiguredError` so the route can answer honestly, and the loop treats it as
a no-op rather than a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
import uuid

from . import (
    engines,
    metadata,
    notifications,
    prefs,
    pulse,
    review,
    scrollback,
)
from . import (
    orchestrator_ledger as ledger,
)

log = logging.getLogger("agent_sessions.orchestrator")

# --- bounds (server-owned; the model's output is DATA) ---------------------------------
DIGEST_MAX = 40  # sessions offered to the model in one pass
TITLE_MAX = 80
SUMMARY_MAX = 300
PROJECT_MAX = 40
ASSESSMENT_MAX = 600
RATIONALE_MAX = 200
ANSWER_MAX = 800
OPTION_MIN, OPTION_MAX = 1, 20

EVIDENCE_KINDS: tuple[str, ...] = ("screen", "transcript_tail", "recap", "none")
# How much rendered screen feeds the precondition fingerprint. Small on purpose: the
# fingerprint should track "is this still the same prompt", not "did a spinner tick".
PRECONDITION_CHARS = 1200
EVIDENCE_SCREEN_CHARS = 2000
EVIDENCE_TRANSCRIPT_CHARS = 4000
EVIDENCE_RECAP_CHARS = 1500

# Verbs that put bytes on a session's stdin. `observe`/`escalate` are decisions, not
# deliveries, so they are never gated on the actuation capability.
DELIVERING_VERBS: frozenset[str] = frozenset({"continue", "choose", "answer"})


def _clamp(value: object, cap: int) -> str:
    """Model output is DATA: collapse whitespace, cap the length, empty on junk."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:cap]


# Control bytes (keeping \n and \t) are stripped from evidence before it leaves the server.
# ``live_tail_text`` already renders a clean grid, but ``gather_input`` carries transcript
# content straight from an engine's store — and that is agent output, i.e. untrusted. The UI
# renders it as plain text, so this is defence in depth rather than the only guard.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean_evidence(text: str) -> str:
    return _CTRL_RE.sub("", text)


def _prompt_class(screen: str) -> str:
    """A coarse label for what the session's screen is *asking*, used as part of the
    precondition. Deliberately coarse: it must survive a spinner frame or a re-render, and only
    change when the nature of the prompt does — otherwise every proposal would be stale by the
    time the operator looked at it."""
    tail = screen[-400:].lower()
    if any(t in tail for t in ("(y/n)", "[y/n]", "yes/no", "do you want to proceed")):
        return "confirm"
    if any(t in tail for t in ("1)", "1.", "❯ 1", "select an option", "choose")):
        return "choice"
    if tail.rstrip().endswith("?"):
        return "question"
    return "open"


def _screen_fingerprint(screen: str) -> str:
    """Hash of the *normalised* screen tail. Whitespace runs collapse so a cursor-parked
    repaint doesn't read as a change; the prompt class rides alongside it in the precondition."""
    norm = " ".join(screen[-PRECONDITION_CHARS:].split())
    return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:32]


def precondition_for(key: str) -> dict:
    """Capture what the pass believed about this session's screen. Blocking (ring replay) —
    call under ``asyncio.to_thread``."""
    try:
        screen = scrollback.live_tail_text(key, PRECONDITION_CHARS)
    except Exception:
        screen = ""
    return {
        "key": key,
        "screen_fingerprint": _screen_fingerprint(screen),
        "prompt_class": _prompt_class(screen),
        "observed_at": time.time(),
    }


def eligible_cards(
    *, now: float | None = None, working_keys: set[str] | None = None
) -> tuple[list[dict], dict[str, int]]:
    """The sessions the orchestrator may consider, plus a count of what was filtered and why.

    Rides ``pulse.build_cards`` so a proposal and its sidebar row always agree, then applies
    the two gates that must hold BEFORE anything reaches the model:

    * **engine capability** — ``supports_orchestrator_input`` (default-deny). ``shell`` is a
      bare login shell: a "continue" nudge typed into one is a *command*, and its key shape is
      perfectly valid, so no id check can catch it.
    * **per-session opt-out** — ``orchestrator_excluded``.

    Blocking (FS + metadata); call under ``asyncio.to_thread``.
    """
    cards = pulse.build_cards(window_days=None, now=now, working_keys=working_keys)
    actuable = engines.orchestrator_input_engines()
    meta_index = metadata.load()
    aliases = metadata.load_aliases()
    # A session with an action already awaiting the operator is not eligible for another
    # proposal. This is what makes progress STRUCTURAL rather than a property of the rotation:
    # with <=DIGEST_MAX cards the offset wraps to 0, so an over-cap pass would otherwise re-send
    # the identical slice and re-record the identical first action forever, while the sessions
    # behind it were never reached. Draining the pending set means each pass necessarily
    # considers sessions the previous ones did not.
    pending_sessions = {
        r.get("session_id")
        for r in ledger.live_actions()
        if r.get("state") in ("proposed", "approved", "escalated")
    }
    skipped = {"engine": 0, "excluded": 0, "pending": 0}
    out: list[dict] = []
    for card in cards:
        if card.get("engine") not in actuable:
            skipped["engine"] += 1
            continue
        key = card["id"]
        m = meta_index.get(key) or meta_index.get(engines.physical_key(key, aliases))
        if m is not None and m.orchestrator_excluded:
            skipped["excluded"] += 1
            continue
        if key in pending_sessions:
            skipped["pending"] += 1
            continue
        out.append(card)
    return out, skipped


def _last_action_at() -> dict[str, float]:
    """Newest ledger timestamp per session. Feeds the over-cap fairness ordering — a session
    with no history sorts first (0.0), so unseen work always outranks repeat work."""
    out: dict[str, float] = {}
    for rec in ledger.latest_by_id().values():
        sid = rec.get("session_id")
        if isinstance(sid, str):
            out[sid] = max(out.get(sid, 0.0), float(rec.get("ts") or 0))
    return out


def _eligible_ids(working_keys: set[str] | None) -> list[dict]:
    """Re-derive the eligible set. Used to re-check eligibility AFTER the model call, so a
    session excluded (or an engine made non-actuable) mid-flight is dropped before anything is
    recorded against it."""
    cards, _ = eligible_cards(working_keys=working_keys)
    return cards


def _digest_entry(card: dict, now: float) -> dict:
    """The trimmed per-session view the model sees. Bounded fields only, never internal keys,
    never a raw transcript — transcripts are pulled per-session as *evidence*, after a
    proposal names one, exactly as ``pulse_chat`` Stage 2 does."""
    project = card.get("project") or {}
    summary = str(card.get("_ai_recap") or card.get("ai_summary") or "")[:SUMMARY_MAX]
    return {
        "id": card["id"],
        "engine": card.get("engine", ""),
        "title": _clamp(card.get("title"), TITLE_MAX),
        "project": _clamp(project.get("name"), PROJECT_MAX),
        "state": card.get("state", ""),
        "needs_user": bool(card.get("intervention_required")),
        "summary": summary,
        "age_hours": round((now - float(card.get("last_activity") or now)) / 3600, 1),
    }


def _validate_actions(obj: dict, sent: dict[str, dict]) -> tuple[str, list[dict]]:
    """Narrow a model reply to ``(assessment, [action, …])``.

    Anti-hallucination, mirroring ``pulse_chat._validate_matches``: an id must appear in the
    slice **actually sent this pass** and must survive ``engines.parse_key``; unknowns are
    dropped, duplicates collapsed. A ``choose`` without a usable option number, or an ``answer``
    without text, degrades to ``escalate`` rather than being invented into something
    deliverable — the operator sees the session, which is the honest outcome.
    """
    assessment = _clamp(obj.get("assessment"), ASSESSMENT_MAX)
    raw = obj.get("actions")
    if not isinstance(raw, list):
        return assessment, []
    seen: set[str] = set()
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = item.get("session_id")
        if not isinstance(sid, str) or sid not in sent or sid in seen:
            continue
        try:
            engines.parse_key(sid)
        except Exception:  # noqa: S112 — a bad-shape id is data, not an event to log
            continue
        verb = item.get("verb")
        if not isinstance(verb, str) or verb not in prefs.ORCH_VERBS:
            continue
        # `json.loads` accepts NaN / Infinity, and `max(0, min(1, nan))` returns 1.0 — so a
        # non-finite confidence would clamp to MAXIMUM confidence and auto-approve under yolo.
        # Non-finite means "no usable confidence", which is 0.0, not 1.0.
        conf = item.get("confidence")
        confidence = 0.0
        if isinstance(conf, int | float) and not isinstance(conf, bool) and math.isfinite(conf):
            confidence = max(0.0, min(1.0, float(conf)))
        evidence = item.get("evidence")
        evidence = evidence if isinstance(evidence, str) and evidence in EVIDENCE_KINDS else "none"
        action: dict = {
            "session_id": sid,
            "verb": verb,
            "confidence": round(confidence, 3),
            "rationale": _clamp(item.get("rationale"), RATIONALE_MAX),
            "evidence": evidence,
        }
        if verb == "choose":
            opt = item.get("option")
            if (
                isinstance(opt, int)
                and not isinstance(opt, bool)
                and OPTION_MIN <= opt <= OPTION_MAX
            ):
                action["option"] = opt
            else:
                action["verb"] = "escalate"  # no usable option → let the operator look
                action.pop("option", None)
        elif verb == "answer":
            text = _clamp(item.get("answer"), ANSWER_MAX)
            if text:
                action["answer"] = text
            else:
                action["verb"] = "escalate"
        seen.add(sid)
        out.append(action)
    return assessment, out


def _decide(action: dict, cfg: dict) -> str:
    """The state a fresh proposal starts in, given the operator's tier and threshold.

    * ``off`` — everything is a proposal; nothing is ever queued for delivery.
    * ``suggest`` — deliverable verbs queue for a tap (``proposed``).
    * ``yolo`` — a deliverable verb **inside the enforced ceiling** and at or above the
      confidence threshold is ``approved`` (Phase 2 delivers it); anything else falls back to
      the supervised path. Below threshold it is ``escalated``, which is the whole point of the
      threshold: unsure means ask, never guess.

    ``observe`` and ``escalate`` are decisions rather than deliveries, so they land terminal-ish
    immediately and never consult the ceiling.
    """
    verb = action["verb"]
    if verb == "observe":
        return "observed"
    if verb == "escalate":
        return "escalated"
    # An operator who switched orchestration OFF while the model call was in flight must not
    # find an auto-approved action waiting for them. `enabled` is re-read after the call and
    # fences the approval path here, not just the scheduler.
    if not cfg.get("enabled", False):
        return "proposed"
    if cfg["autonomy"] != "yolo":
        return "proposed"
    if verb not in set(cfg["allowed_verbs"]):
        return "proposed"  # outside the v1 ceiling → always a tap
    if action["confidence"] < float(cfg["confidence_min"]):
        return "escalated"
    return "approved"


async def run_pass(
    *,
    working_keys: set[str] | None = None,
    now: float | None = None,
    offset: int = 0,
) -> dict:
    """One orchestrator pass: digest → one model call → validated proposals → ledger.

    Returns a report ``{"assessment", "actions": [...], "considered", "skipped"}``. Raises
    :class:`review.NotConfiguredError` when the AI endpoint isn't configured (the route answers
    409) and :class:`review.ReviewError` on an endpoint failure (502) — unlike a Pulse scan
    there is no useful non-LLM fallback for a decision.
    """
    review._require_config()  # fail fast before any FS work, like pulse_chat.ask
    cfg = prefs.get_orchestrator()
    now = time.time() if now is None else now

    cards, skipped = await asyncio.to_thread(eligible_cards, now=now, working_keys=working_keys)
    if not cards:
        return {
            "assessment": "No sessions to manage right now.",
            "actions": [],
            "considered": 0,
            "skipped": skipped,
        }

    # Rotate the window. Taking `cards[:DIGEST_MAX]` every pass meant a >cap world re-sent the
    # SAME 40 sessions forever — burning a paid call per sweep while cards 41+ were never once
    # shown to the model. `offset` walks the eligible set so consecutive passes cover it.
    total = len(cards)
    start = (offset or 0) % total if total else 0
    slice_ = (cards + cards)[start : start + DIGEST_MAX] if total > DIGEST_MAX else cards
    sent = {c["id"]: c for c in slice_}
    payload = {"sessions": [_digest_entry(c, now) for c in slice_]}
    obj = await review.complete_json(
        [
            {"role": "system", "content": str(cfg["prompt"])},
            {"role": "user", "content": json.dumps(payload)},
        ]
    )
    assessment, actions = _validate_actions(obj, sent)

    # The endpoint call is the long await in this function, and policy can change across it.
    # Re-read the config and re-derive eligibility BEFORE recording anything: an operator who
    # withdrew agency mid-call must not find an `approved` action waiting for them afterwards.
    cfg = prefs.get_orchestrator()
    still_eligible = {c["id"] for c in await asyncio.to_thread(_eligible_ids, working_keys)}
    dropped = [a for a in actions if a["session_id"] not in still_eligible]
    actions = [a for a in actions if a["session_id"] in still_eligible]
    cap = int(cfg["max_actions_per_pass"])
    over_cap = max(0, len(actions) - cap)
    if over_cap:
        # Fairness, not truncation order. Rotating the CARD slice does not guarantee progress:
        # a model that returns its actions in a stable order re-proposes the same session
        # first no matter which order it was shown them in, so `actions[:cap]` would record
        # that one forever while the rest starved. Ordering by "least recently acted on"
        # makes progress a property of the ledger rather than of model behaviour — the session
        # just acted on sorts last next time, so every session is reached in bounded passes.
        last_seen = _last_action_at()
        actions.sort(key=lambda a: last_seen.get(a["session_id"], 0.0))
    actions = actions[:cap]

    ttl_s = int(cfg["proposal_ttl_minutes"]) * 60
    recorded: list[dict] = []
    for action in actions:
        card = sent[action["session_id"]]
        state = _decide(action, cfg)
        rec: dict = {
            "id": uuid.uuid4().hex,
            "state": state,
            "ts": now,
            "expires_at": now + ttl_s,
            "tier": cfg["autonomy"],
            # Identity, so the feed and every notification can name the project and deep-link
            # the session without re-resolving anything.
            "session_id": action["session_id"],
            "engine": card.get("engine", ""),
            "title": _clamp(card.get("title"), TITLE_MAX),
            "project": _clamp((card.get("project") or {}).get("name"), PROJECT_MAX),
            "project_id": (card.get("project") or {}).get("id") or "",
            **{k: v for k, v in action.items() if k != "session_id"},
        }
        # Only a verb that will actually be delivered needs a precondition to verify later.
        if action["verb"] in DELIVERING_VERBS:
            rec["precondition"] = await asyncio.to_thread(
                precondition_for, engines.physical_key(action["session_id"])
            )
        recorded.append(rec)

    # (7) Ledger writes fsync, and compaction can rewrite the whole file. Doing that inline
    # stalls every HTTP/WS client this process serves — the #678 lesson, which this module
    # otherwise preaches. Batch it into ONE worker-thread hop.
    if recorded:
        recorded = await asyncio.to_thread(_persist, recorded)
    return {
        "assessment": assessment,
        "actions": recorded,
        "considered": len(slice_),
        # What the pass actually consumed — the loop scopes its change-detection fingerprint to
        # THIS, not the whole eligible world, or cards beyond the digest cap would be recorded
        # as "seen" and never reconsidered (starvation).
        "consumed_ids": [c["id"] for c in slice_],
        # Remaining work of EITHER kind: sessions the digest couldn't fit, or model actions
        # sliced off by the per-pass cap. Both mean "there is more to do", and the loop must
        # not record the world as fully seen while either is true.
        "truncated": len(cards) > len(slice_) or over_cap > 0,
        "next_offset": (start + len(slice_)) % total if total else 0,
        "over_cap": over_cap,
        "dropped_ineligible": len(dropped),
        "skipped": skipped,
    }


def _persist(records: list[dict]) -> list[dict]:
    """Write a pass's records, raise notifications, and compact if needed. Returns what was
    actually written.

    Blocking — call under ``to_thread``.

    The append is a CHECK-AND-APPEND under one ledger lock, not a plain append. "At most one
    live action per session" cannot be enforced by deciding eligibility and then writing: the
    scheduled pass and the chat run under different single-flights, so both can see a session
    as free and both append. Two live actions for one session can both reach the actuator, and
    if the first write has not yet changed the screen the second precondition passes too —
    duplicate input into a real session.

    Records dropped by that check are returned to the caller as "not written", so a response
    can never claim to have queued something the ledger refused.

    Notifications are raised HERE, after the ledger append, because the ledger is the durable
    record: notifying before it would announce something that might not exist, and notifying
    from the caller would mean every call site had to remember to. An escalation the operator
    is never told about is the one failure this whole feature exists to remove.

    Crucially they are raised for KEPT records only. A dropped one was never persisted, so
    notifying about it would announce exactly the thing that does not exist — the same rule,
    applied to the case where the ledger refuses the slot.
    """
    kept, dropped = ledger.append_batch_for_free_sessions(records)
    if dropped:
        log.info(
            "orchestrator: dropped %d action(s) whose session already had a live one", len(dropped)
        )
    ledger.compact_if_needed()

    notify = str(prefs.get_orchestrator().get("notify") or "escalations")
    for rec in kept:
        # `escalated` IS the "I'm not sure, you look" state (see _decide). `all` also covers
        # actions taken autonomously, so a yolo operator still gets a record of what was done.
        if not (notify == "all" or (notify == "escalations" and rec.get("state") == "escalated")):
            continue
        with contextlib.suppress(Exception):
            # Best-effort by design: a notification store or push failure must never lose the
            # ledger write that already succeeded, nor break the pass.
            note = notifications.add(
                title=str(rec.get("title") or "A session needs you"),
                project=str(rec.get("project") or ""),
                reason=str(rec.get("rationale") or ""),
                session_id=str(rec.get("session_id") or ""),
                engine=str(rec.get("engine") or ""),
                action_id=str(rec.get("id") or ""),
            )
            notifications.fanout(note)
    return kept


def evidence_for(session_id: str, kind: str) -> dict:
    """Server-pulled evidence for one session. Blocking — call under ``asyncio.to_thread``.

    The model names only the *kind*; every byte here comes from the real session, fetched at
    render time. That asymmetry is the anti-hallucination rule: a model that can quote a screen
    can invent one, and invented evidence launders a hallucination into something that looks
    verified. Nothing here is ever persisted into the ledger.
    """
    kind = kind if kind in EVIDENCE_KINDS else "none"
    if kind == "none":
        return {"kind": "none", "text": "", "available": False}
    if kind == "screen":
        text = scrollback.live_tail_text(engines.physical_key(session_id), EVIDENCE_SCREEN_CHARS)
    elif kind == "recap":
        # Resolve first: for a reconciled opencode/codex session the sidecar still lives under
        # the PLACEHOLDER physical key, so a direct get() reports a real recap as unavailable.
        # `pulse.build_cards` already resolves this way; evidence must agree with the card.
        m = metadata.get(metadata.resolve_key(session_id))
        text = (m.ai_recap or "")[:EVIDENCE_RECAP_CHARS]
    else:
        try:
            text, _ = review.gather_input(session_id, EVIDENCE_TRANSCRIPT_CHARS)
        except review.ReviewError:
            text = ""
    return {"kind": kind, "text": _clean_evidence(text), "available": bool(text.strip())}
