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
    session_input,
)
from . import (
    orchestrator_ledger as ledger,
)

log = logging.getLogger("agent_sessions.orchestrator")

# --- bounds (server-owned; the model's output is DATA) ---------------------------------
DIGEST_MAX = 40  # sessions offered to the model in one pass
# Beyond this, a session's silence is the answer: nothing is waiting on a nudge. Deliberately
# generous — it exists to stop a `continue` landing in work that finished last week (#755), not
# to second-guess a session someone stepped away from for an afternoon.
STALE_DELIVER_HOURS = 48.0
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
    skipped = {"engine": 0, "excluded": 0, "pending": 0, "stale": 0}
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
        # A session silent for days is not waiting on anyone. `build_cards` is called with
        # `window_days=None`, so without this every session the app has ever seen stays eligible
        # forever and the rotation re-examines week-old work indefinitely — measured at a median
        # 43.9h since last activity across the sessions being notified about, oldest 170h (#763).
        age = _age_hours(card, now if now is not None else time.time())
        if age is not None and age >= STALE_DELIVER_HOURS:
            skipped["stale"] += 1
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


# A rationale that opens by quoting the title back. Measured on the live store: 4 of 200 began
# literally `Title says '…' — …`, and 31 of 200 contained their own title somewhere.
_ECHO_LEAD = re.compile(
    r"^\s*(?:the\s+)?title\s+(?:says|reads|is)\s*[:\-\u2014]?\s*", re.IGNORECASE
)
_ECHO_SEP = re.compile(r"^\s*[\-\u2014:]\s*")
# Openers mapped to the closer that actually pairs with them.
_QUOTE_PAIRS = {"'": "'", '"': '"', "\u2018": "\u2019", "\u201c": "\u201d"}


def _strip_echo_prefix(rationale: str, title: str) -> tuple[str, bool]:
    """Remove a leading `Title says '<title>' — ` when the quoted span IS the title.

    Explicit steps rather than one pattern, because three successive regex versions each got the
    same thing wrong in a new way: non-greedy stopped at an apostrophe inside the title, greedy
    ran on to a later quote in the real sentence, and an optional closer let the title match as
    a PREFIX of a longer phrase — `'Build is blocked by CI'` against title `Build is blocked`
    left `By CI' — …`. Every rejection below has a name, which is the point.
    """
    m = _ECHO_LEAD.match(rationale)
    if not m:
        return rationale, False
    rest = rationale[m.end() :]
    closer = _QUOTE_PAIRS.get(rest[:1])
    if closer:
        body = rest[1:]
        if body[: len(title)].lower() != title.lower():
            return rationale, False  # quoted something other than the title
        after = body[len(title) :]
        if not after.startswith(closer):
            return rationale, False  # the title is only a PREFIX of the quoted span
        after = after[1:]
    else:
        if rest[: len(title)].lower() != title.lower():
            return rationale, False
        after = rest[len(title) :]
        # Unquoted needs an explicit separator, or `Title says Build is blocked by CI — …`
        # would be truncated to `by CI — …` on title `Build is blocked`.
        if not _ECHO_SEP.match(after):
            return rationale, False
    return _ECHO_SEP.sub("", after, count=1), True


# A word that carries its own internal capital is deliberately cased — `iOS`, `eBay`, `macOS`.
_ALL_LOWER_LEAD = re.compile(r"^[a-z]+(?![A-Za-z])")


def _degabble(rationale: str, title: str) -> str:
    """Strip a `Title says '<title>' — ` preamble so what is left is the part that says something.

    #753: the rationale is the one line answering *why does this need me*, and some of it just
    echoed the title printed directly above it. Removing the preamble turns
    `Title says 'X' — needs user decision on re-queue.` into `Needs user decision on re-queue.`

    Deliberately ONLY the preamble form. Removing a title quoted mid-sentence scored far better
    on the obvious metric — "does the reason still contain its title", 26 -> 2 against 26 -> 22 —
    and produced worse text, because in those rows the title IS the opening clause:

        Awaiting user decision on PR #20 merge path after Hermes approval
        -> "after Hermes approval"

    A redundant sentence is readable; a fragment is not. The metric rewarded shredding, so it
    was the wrong metric, and those rows are a PROMPT problem rather than something subtraction
    can fix.

    Subtractive in the strict sense: the retained suffix is handed back byte-for-byte apart
    from the leading separator that joined it to the preamble. It is NOT re-spaced, and its
    casing is repaired only when the leading word is unambiguously lowercase — capitalising
    unconditionally turned `iOS deployment…` into `IOS deployment…` and `eBay…` into `EBay…`.
    """
    if not title:
        return rationale
    out, had_prefix = _strip_echo_prefix(rationale, title)
    if not had_prefix:
        return rationale  # nothing was an echo — hand back exactly what we got
    out = out.strip()
    if len(out) < 12:
        return rationale  # nothing meaningful survived — keep what we had
    if _ALL_LOWER_LEAD.match(out):
        out = out[:1].upper() + out[1:]
    return out


def _age_hours(card: dict, now: float) -> float | None:
    """Hours since a session last did anything, from EITHER shape this is handed.

    `run_pass` and `orchestrator_chat.ask` build their `sent` map from raw cards, which carry
    `last_activity`; `age_hours` exists only on the trimmed `_digest_entry` copy sent to the
    model. Reading `age_hours` alone therefore found `None` on every production call and the
    staleness gate never fired — and a test that builds its own `sent` with `age_hours` already
    present cannot see that, because no real caller passes that shape.
    """
    age = card.get("age_hours")
    if isinstance(age, int | float) and not isinstance(age, bool):
        return float(age)
    last = card.get("last_activity")
    if isinstance(last, int | float) and not isinstance(last, bool):
        return max(0.0, (now - float(last)) / 3600)
    return None


def _validate_actions(
    obj: dict,
    sent: dict[str, dict],
    *,
    now: float | None = None,
    dropped: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Narrow a model reply to ``(assessment, [action, …])``.

    Anti-hallucination, mirroring ``pulse_chat._validate_matches``: an id must appear in the
    slice **actually sent this pass** and must survive ``engines.parse_key``; unknowns are
    dropped, duplicates collapsed. A ``choose`` without a usable option number, or an ``answer``
    without text, degrades to ``escalate`` rather than being invented into something
    deliverable — the operator sees the session, which is the honest outcome.

    ``dropped``, when given, collects ``{"session_id", "verb", "reason"}`` for each action this
    function REMOVES. A caller that reports back to a human needs it: silently returning fewer
    actions is indistinguishable from the model having proposed nothing, and `orchestrator_chat`
    used that distinction to decide whether its "On it." needed correcting. Without it, asking
    the chat to nudge a dead session answered "Nudged it." over an empty action list.
    """
    now = time.time() if now is None else now
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
            # The CLAMPED title — the exact string `_digest_entry` put in front of the model.
            # Comparing against the raw card title meant a >TITLE_MAX title could be echoed
            # perfectly and never recognised, because the model never saw the long form.
            "rationale": _clamp(
                _degabble(
                    str(item.get("rationale") or ""),
                    _clamp(sent[sid].get("title"), TITLE_MAX),
                ),
                RATIONALE_MAX,
            ),
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
        # A delivering verb on a session that has been silent for days is a nudge nobody is
        # waiting for. #755: `continue` was proposed at confidence 0.8 on a session whose work
        # finished six days earlier — the model had `age_hours` in front of it and used it for
        # nothing. Degrading to `escalate` keeps the session in front of the operator without
        # typing into it, which is the same rule the confidence threshold already encodes:
        # unsure means ask, never guess.
        if action["verb"] in DELIVERING_VERBS:
            # A delivering verb needs somewhere to type. The actuator refuses on exactly this
            # predicate — `session_input.is_live(physical_key)`, actuator.py — and settles the
            # action `failed` with "session is not live". That is where `yolo` was dying: of the
            # 38 actions it auto-approved, 7 failed there and only 5 ever delivered (#766).
            # Proposing a nudge for a session with no writable PTY is a guaranteed failure, and
            # not a decision the operator can act on either, so it is dropped.
            #
            # Ask the registry, NOT the card. `card["live"]` means "an agent is working or a
            # browser is attached" — a headless-but-live session, which is the archetypal
            # `continue` target, has `live: False` while being perfectly writable. Gating on it
            # would block precisely the case this is meant to enable.
            if not session_input.is_live(engines.physical_key(sid)):
                if dropped is not None:
                    dropped.append(
                        {"session_id": sid, "verb": action["verb"], "reason": "not_live"}
                    )
                seen.add(sid)
                continue
            # Defence in depth only: `eligible_cards` drops anything past this same bound
            # before the model is ever called, so on both production paths nothing this old
            # reaches here. It stays for a caller that assembles `sent` itself.
            age = _age_hours(sent[sid], now)
            if age is not None and age >= STALE_DELIVER_HOURS:
                # DROP it. #756 degraded this to `escalate` to stop a nudge landing in work that
                # finished last week — the verb reasoning was right and the notification
                # consequence was not. `notify: escalations` raises an alert only for
                # `escalated`, while a `proposed` delivering verb is silent, so that change
                # turned a silent proposal into a recurring alert about a stale session (#763).
                # Dropping stops the delivery just as firmly, and quietly. The session is still
                # on the Pulse cards and in the sidebar; only the unsolicited interruption goes.
                if dropped is not None:
                    dropped.append({"session_id": sid, "verb": action["verb"], "reason": "stale"})
                seen.add(sid)
                continue
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
    assessment, actions = _validate_actions(obj, sent, now=now)

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
            # The session's own clock at proposal time. The bell uses it to tell "the same
            # unresolved situation, re-proposed" from "something new happened here" (#752).
            "last_activity": card.get("last_activity"),
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
                escalation=rec.get("state") == "escalated",
                activity_at=rec.get("last_activity"),
            )
            # `None` means an equivalent alert is already sitting in the bell — the operator has
            # been told. Re-proposing is correct (the situation IS still unresolved); re-alerting
            # about it every TTL is not, and a push is the one channel that can wake someone.
            if note is not None:
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
