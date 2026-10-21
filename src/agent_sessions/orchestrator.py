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
from collections.abc import Callable

from . import (
    assessment,
    automation,
    engines,
    metadata,
    mission_fence,
    notifications,
    permission_prompts,
    prefs,
    prompts,
    pulse,
    review,
    screen_menus,
    scrollback,
    session_input,
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
#: How much rendered screen the prompt CLASS is judged from (#1060) — at proposal time and, through
#: `actuator.screen_matches`, again at delivery, so the two can never read different windows and
#: disagree about the same frame. Wider than the fingerprint on purpose: a claude select list with
#: wrapped descriptions runs to ~2 KB, and a 1200-char tail cut off its title, so the real menu that
#: motivated #1060 classified as `open`. The fingerprint still hashes only the last
#: `PRECONDITION_CHARS` of this read, which is the same text a narrower read returned.
PROMPT_SCREEN_CHARS = 8000
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
    # AN ENGINE'S OWN MENU FIRST (#1060). The substring checks below only see the last 400
    # characters, and a claude select list with long descriptions keeps its "1." further up than
    # that — the real menu that motivated #1060 classified as `open`. A menu recognised by its own
    # chrome at the bottom of the screen is a choice whatever the tail's substrings say.
    if screen_menus.recognises(screen):
        return "choice"
    # …AND AN ENGINE'S OWN PERMISSION DIALOG (#1213): a yes/no about one tool call. opencode's has
    # none of the substrings below (no "?", no "1."), so it read as `open` — "quiet" on the mission
    # strip while the agent was in fact blocked on the operator.
    if permission_prompts.recognises(screen):
        return "confirm"
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
        screen = scrollback.live_tail_text(key, PROMPT_SCREEN_CHARS)
    except Exception:
        screen = ""
    return {
        "key": key,
        "screen_fingerprint": _screen_fingerprint(screen),
        "prompt_class": _prompt_class(screen),
        "observed_at": time.time(),
    }


def observed_prompt_for(key: str) -> dict:
    """What the session's screen showed when a decision was ESCALATED (#1060). Blocking.

    An escalation delivers nothing, so it never gets a precondition — and so it carried no fact
    about the screen at all: the card the operator acts on could not tell a session parked at a
    numbered menu from one that simply stopped. This records, for the card to read:

    * ``prompt_class`` — the same classification a precondition carries, from the same window;
    * ``menu`` — the engine's own menu if `screen_menus` recognises one, else ``None``. Display text
      only: labels are the agent's words, cleaned and capped, and never become bytes. What a tap
      would send is re-derived from the LIVE screen at approval time, never from this snapshot.
    * ``permission`` — the engine's own TOOL-PERMISSION dialog (#1213), from the SAME frame, or
      ``None``. Operator-only: `permission_prompts` is never read by an autonomous path. Display
      text plus the dialog's cursor; what a tap sends is re-derived from the live screen too.
    * ``fingerprint`` — the frame's normalised hash, so a card can tell "the same prompt" apart
      from a new one showing the same words.
    """
    try:
        screen = scrollback.live_tail_text(key, PROMPT_SCREEN_CHARS)
    except Exception:
        screen = ""
    engine = screen_menus.engine_of(key)
    cls = _prompt_class(screen)
    return {
        "prompt_class": cls,
        "menu": screen_menus.parse(screen, engine),
        # The coloured re-read only when the text already shows a dialog: every sweep calls this
        # for every session, and a permission dialog always classifies as `confirm`.
        "permission": observed_permission(key) if cls == "confirm" else None,
        "fingerprint": _screen_fingerprint(screen),
        "observed_at": time.time(),
    }


def observed_permission(key: str) -> dict | None:
    """The session's tool-permission dialog (#1213), or None. Blocking.

    Its own read, and ONE read: the dialog's words and its cursor (a colour, for opencode) come
    from the same frame of `scrollback.live_tail_frame`, so they can never describe two different
    moments. Operator-only — never an input to an autonomous decision."""
    try:
        screen, cells = scrollback.live_tail_frame(key, PROMPT_SCREEN_CHARS)
    except Exception:  # noqa: BLE001 — an unreadable ring is "no dialog"
        return None
    return permission_prompts.parse(screen, screen_menus.engine_of(key), cells)


class ScreenUnreadable(Exception):
    """A STRICT screen read failed (#1086 Phase 4, Hermes 5265): unknown, not "the screen moved"."""


def observed_screen(key: str, *, strict: bool = False) -> dict:
    """The live screen as the Ask page reads it (#1086 Phase 3), WITHOUT attaching. Blocking.

    ``prompt_class`` and ``menu`` exactly as :func:`observed_prompt_for`; ``fingerprint`` the same
    normalised hash a precondition carries (so a dismissal can be keyed to "this screen"); and
    ``screen`` the tail itself, control bytes stripped and capped, for display as TEXT only.
    Reading the ring is not attaching a viewer: nothing here can make an Approve refuse (#1049).
    """
    try:
        screen = scrollback.live_tail_text(key, PROMPT_SCREEN_CHARS)
    except Exception as e:
        # The fail-soft read (the list, the details) shows an empty screen. The notification
        # sync must not: an empty screen has a DIFFERENT fingerprint, which a dismissal would read
        # as "the screen moved" and re-announce an unchanged, dismissed session.
        if strict:
            raise ScreenUnreadable(type(e).__name__) from e
        screen = ""
    cls = _prompt_class(screen)
    return {
        "prompt_class": cls,
        "menu": screen_menus.parse(screen, screen_menus.engine_of(key)),
        "permission": observed_permission(key) if cls == "confirm" else None,
        "fingerprint": _screen_fingerprint(screen),
        "screen": _clean_evidence(screen)[-EVIDENCE_SCREEN_CHARS:],
    }


def stale_hours(cfg: dict | None = None) -> float:
    """How long a session may sit idle and still be worth interrupting the operator about.

    Past it, its silence is the answer: nothing is waiting on a nudge, and the session goes
    quiet rather than disappearing — it stays on the Pulse cards and in the sidebar. It was a
    hard-coded 48h; measured on a live store the median session was 30.4h idle when it was
    escalated, so 48h removed 18% of the notification volume where the 24h default removes 52%.
    That number is an operator preference, not a constant (#768).

    Read through `_coerce_orchestrator`, so a hand-edited or out-of-range sidecar value falls
    back to the default rather than to "no window" — the failure mode has to be a window the
    operator did not pick, never no window at all. Pass `cfg` when the caller already re-read
    the config for this pass, so the gate and the tier cannot disagree across the model call.
    """
    c = cfg if cfg is not None else prefs.get_session_assistance()
    return float(c.get("stale_hours") or prefs.ORCH_STALE_HOURS_DEFAULT)


def eligible_cards(
    *,
    now: float | None = None,
    working_keys: set[str] | None = None,
    busy_keys: set[str] | None = None,
    mission_id: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """The sessions the orchestrator may consider, plus a count of what was filtered and why.

    Rides ``pulse.build_cards`` so a proposal and its sidebar row always agree, then applies
    the two gates that must hold BEFORE anything reaches the model:

    * **engine capability** — ``supports_orchestrator_input`` (default-deny). ``shell`` is a
      bare login shell: a "continue" nudge typed into one is a *command*, and its key shape is
      perfectly valid, so no id check can catch it.
    * **per-session opt-out** — ``orchestrator_excluded``.

    ``busy_keys`` (#969) is `actuator.busy_keys` — sessions VISIBLY working right now, snapshotted
    by the caller on the event loop. Not ``working_keys``: that overlay also counts an open browser
    and a window-title blink, and both mark sessions a proposal is FOR.

    Blocking (FS + metadata); call under ``asyncio.to_thread``.
    """
    busy = busy_keys or set()
    cards = pulse.build_cards(window_days=None, now=now, working_keys=working_keys)
    stale_after = stale_hours(prefs.get_automation_policy("mission" if mission_id else "session"))
    actuable = engines.orchestrator_input_engines()
    meta_index = metadata.load()
    # A session with an action already awaiting the operator is not eligible for another
    # proposal. This is what makes progress STRUCTURAL rather than a property of the rotation:
    # with <=DIGEST_MAX cards the offset wraps to 0, so an over-cap pass would otherwise re-send
    # the identical slice and re-record the identical first action forever, while the sessions
    # behind it were never reached. Draining the pending set means each pass necessarily
    # considers sessions the previous ones did not.
    pending_sessions = {
        r.get("session_id")
        for r in ledger.live_actions()
        if r.get("state") in ledger.OPERATOR_PENDING_STATES
    }
    # ROOTS + FOLDER EXCLUSIONS, the terminal's form of the boundary (#1086 Phase 3). A proposal is
    # a proposal to TYPE into a session, so a session the terminal refuses to resume is not
    # eligible either — and that is also what lets every pending decision have a surface: the Ask
    # page lists exactly the in-scope sessions no mission holds. Lazy import: a core module must
    # not import the route layer at load time.
    from . import project_dirs
    from .routes.sessions import _hard_scope_filter

    in_scope = _hard_scope_filter(honour_curation=False)
    # A card with no cwd has nothing to check. The terminal's rule for exactly that case: fail
    # CLOSED where a boundary is configured, open where none is
    # (docs/invariants/session-list-filtering.md, "Two boundary rules").
    boundary = bool(project_dirs.effective_roots() or prefs.get_folder_exclusions())
    skipped = {"engine": 0, "excluded": 0, "pending": 0, "working": 0, "stale": 0, "scope": 0}
    out: list[dict] = []
    try:
        grants = automation.capture_candidates(
            [card["id"] for card in cards if card.get("engine") in actuable], mission_id
        )
    except automation.AuthorityChanged:
        grants = {}
    for card in cards:
        if card.get("engine") not in actuable:
            skipped["engine"] += 1
            continue
        cwd = card.get("cwd")
        if (not isinstance(cwd, str) and boundary) or (
            isinstance(cwd, str) and not in_scope(cwd, card.get("project") or {"kind": ""})
        ):
            skipped["scope"] += 1
            continue
        key = card["id"]
        grant = grants.get(key)
        if grant is None:
            skipped["scope"] += 1
            continue
        phys = grant["physical_key"]
        m = meta_index.get(key) or meta_index.get(phys)
        if m is not None and m.orchestrator_excluded:
            skipped["excluded"] += 1
            continue
        if key in pending_sessions:
            skipped["pending"] += 1
            continue
        # A session repainting its screen right now needs nothing (#969). Measured on the live
        # ledger: `continue` proposed one minute after a session's last transcript write, with its
        # spinner running, and the operator asked why there was anything to approve. The registry
        # names sessions by physical key, so both forms are checked — as `build_cards` does.
        if key in busy or phys in busy:
            skipped["working"] += 1
            continue
        # A session silent for days is not waiting on anyone. `build_cards` is called with
        # `window_days=None`, so without this every session the app has ever seen stays eligible
        # forever and the rotation re-examines week-old work indefinitely — measured at a median
        # 43.9h since last activity across the sessions being notified about, oldest 170h (#763).
        age = _age_hours(card, now if now is not None else time.time())
        if age is not None and age >= stale_after:
            skipped["stale"] += 1
            continue
        out.append({**card, "automation_authority": grant})
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


def _eligible_ids(
    working_keys: set[str] | None, busy_keys: set[str] | None = None, mission_id: str | None = None
) -> list[dict]:
    """Re-derive the eligible set. Used to re-check eligibility AFTER the model call, so a
    session excluded (or an engine made non-actuable) mid-flight is dropped before anything is
    recorded against it — and, given a fresh ``busy_keys``, one that started working (#969)."""
    cards, _ = eligible_cards(working_keys=working_keys, busy_keys=busy_keys, mission_id=mission_id)
    return cards


# Deeper decision context (#1086 Phase 2). Bounded per field and per pass: a richer digest is
# only worth anything if a sweep over forty sessions stays a bounded read.
STATE_LINE_MAX = 300
REASON_MAX = 280
MENU_OPTIONS_MAX = 9
MENU_LABEL_MAX = 120
DEEP_SESSIONS_MAX = 8  # sessions per pass that get the Deep extras (flagged / at a prompt first)
DEEP_TRANSCRIPT_CHARS = 1500
PRIOR_OUTCOMES_MAX = 3
_PRIOR_OUTCOME_STATES = frozenset({"delivered", "rejected"})


def _state_line(recap: str) -> str:
    """The recap's CURRENT-STATE line: its LAST non-empty line, by the recap prompt's contract.

    The digest used to send the recap's first 300 characters, while the recap prompt puts where
    things stand on its last line — so the decision pass read how the session STARTED (#1018).
    """
    lines = [ln.strip() for ln in (recap or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _digest_menu(menu: object) -> dict | None:
    """The screen's parsed menu (`screen_menus`), trimmed for the digest. Agent text, capped."""
    if not isinstance(menu, dict):
        return None
    options = []
    for o in (menu.get("options") or [])[:MENU_OPTIONS_MAX]:
        if isinstance(o, dict) and isinstance(o.get("n"), int):
            options.append({"n": o["n"], "label": _clamp(o.get("label"), MENU_LABEL_MAX)})
    if not options:
        return None
    return {"question": _clamp(menu.get("question"), MENU_LABEL_MAX), "options": options}


def assessment_view(card: dict) -> tuple[dict, str]:
    """``(projection, context_source)`` for a card's assessment — what the digest leads with.

    Shared with the loop's skip-fingerprint (`orchestrator_loop.fingerprint_for`), so a record
    going stale — the session moved, a refresh failed, a newer review came back without one —
    changes what the next pass would be sent AND re-arms that pass (#1020 review finding 4)."""
    view = assessment.project(
        card.get("_ai_assessment"),
        review_fingerprint=str(card.get("_review_fingerprint") or ""),
        last_activity=card.get("last_activity"),
        review_failed_at=card.get("_review_failed_at"),
    )
    if view["status"] == "missing":
        return view, "recap_last_line"
    return view, ("assessment" if view["status"] == "current" else "assessment_stale")


def _digest_entry(card: dict, now: float, extras: dict | None = None) -> dict:
    """The trimmed per-session view the model sees. Bounded fields only, never internal keys,
    never a raw transcript by default — transcripts are pulled per-session as *evidence*, after a
    proposal names one, exactly as ``pulse_chat`` Stage 2 does. ``extras`` (from
    :func:`_digest_extras`) adds what the Session review settings allow: the screen's prompt and
    menu, and at Deep a bounded transcript tail and this session's earlier outcomes.

    **Where the session stands leads, from explicit fields (#1020).** ``current_state``,
    ``blocker``, ``decision_needed`` and the user's ``constraints`` come from the structured
    assessment, each at the assessment's own cap — never re-cut here, so a blocker that survived
    the record survives the digest. ``context_source`` says where they came from and whether they
    are current: ``assessment``, ``assessment_stale`` (the session moved, or a refresh failed,
    after it was written), or ``recap_last_line`` for a session reviewed before assessments
    existed. When a NEWER review came back without an assessment, the record describes an older
    input, so that review's recap line rides along as ``newer_recap_last_line`` — labelled, never
    merged into the record's fields. ``summary`` is the one-line preview ONLY: it no longer falls
    back to the recap's first 300 characters, which was how session histories reached decisions
    as current state.
    """
    project = card.get("project") or {}
    recap = str(card.get("_ai_recap") or "")
    view, source = assessment_view(card)
    entry: dict = {
        "id": card["id"],
        "engine": card.get("engine", ""),
        "title": _clamp(card.get("title"), TITLE_MAX),
        "project": _clamp(project.get("name"), PROJECT_MAX),
        "state": card.get("state", ""),
        "needs_user": bool(card.get("intervention_required")),
    }
    if view["status"] == "missing":
        entry["current_state"] = _clamp(_state_line(recap), STATE_LINE_MAX)
    else:
        entry["current_state"] = view["current_state"]
        for k in ("blocker", "decision_needed", "constraints"):
            if view[k]:
                entry[k] = view[k]
        newer = _state_line(recap)
        if "newer_review_without_assessment" in view["stale_reasons"] and newer:
            entry["newer_recap_last_line"] = _clamp(newer, STATE_LINE_MAX)
    entry["context_source"] = source
    entry["summary"] = _clamp(card.get("ai_summary"), SUMMARY_MAX)
    entry["age_hours"] = round((now - float(card.get("last_activity") or now)) / 3600, 1)
    reason = card.get("intervention_reason")
    if card.get("intervention_required") and isinstance(reason, str) and reason.strip():
        entry["needs_user_reason"] = _clamp(reason, REASON_MAX)
    for k, v in (extras or {}).items():
        if v not in (None, "", [], {}):
            entry[k] = v
    return entry


def _prior_outcomes(latest: dict[str, dict], session_id: str, now: float) -> list[dict]:
    """The last few settled outcomes for this session — what was already done or refused."""
    rows = [
        r
        for r in latest.values()
        if r.get("session_id") == session_id and r.get("state") in _PRIOR_OUTCOME_STATES
    ]
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return [
        {
            "verb": r.get("verb", ""),
            "outcome": r.get("state", ""),
            "age_hours": round((now - float(r.get("ts") or now)) / 3600, 1),
            "rationale": _clamp(r.get("rationale"), RATIONALE_MAX),
        }
        for r in rows[:PRIOR_OUTCOMES_MAX]
    ]


def _digest_extras(cards: list[dict], now: float, review_cfg: dict) -> dict[str, dict]:
    """Per-session extras the Session review settings allow. Blocking (screen + ledger reads).

    * ``recognise_prompts`` — the screen's prompt class and `screen_menus` menu, per session.
    * ``decision_context == "deep"`` — for at most ``DEEP_SESSIONS_MAX`` sessions, the ones that
      are flagged or stopped at a prompt first: a bounded transcript tail and their earlier
      outcomes. Deeper evidence is fetched for the sessions a decision is about, never re-read
      across the whole fleet every pass (#1018).
    Every failure is an absent field, never an error: a pass must not die on one unreadable screen.
    """
    recognise = bool(review_cfg.get("recognise_prompts", True))
    deep = review_cfg.get("decision_context") == "deep"
    out: dict[str, dict] = {c["id"]: {} for c in cards}
    prompts_seen: dict[str, str] = {}
    if recognise:
        for c in cards:
            try:
                observed = observed_prompt_for(mission_fence.physical_of(c["id"]))
            except Exception:  # noqa: BLE001 — an unreadable screen is simply not described
                log.debug("digest: screen unreadable for %s", c["id"], exc_info=True)
                continue
            prompts_seen[c["id"]] = str(observed.get("prompt_class") or "")
            out[c["id"]]["prompt"] = prompts_seen[c["id"]]
            menu = _digest_menu(observed.get("menu"))
            if menu:
                out[c["id"]]["menu"] = menu
    if deep:
        ranked = sorted(
            cards,
            key=lambda c: (
                not c.get("intervention_required"),
                prompts_seen.get(c["id"], "open") == "open",
            ),
        )
        try:
            latest = ledger.latest_by_id()
        except Exception:  # noqa: BLE001
            latest = {}
        for c in ranked[:DEEP_SESSIONS_MAX]:
            try:
                # TRANSCRIPT ONLY: `review.gather_input` mixes in the live screen and the compose
                # draft, which would read the screen even with recognition off and present it as
                # the conversation (review 5180).
                tail = review.transcript_tail(c["id"], DEEP_TRANSCRIPT_CHARS)
                if tail.strip():
                    out[c["id"]]["transcript_tail"] = _clean_evidence(tail)
            except Exception:  # noqa: BLE001 — no transcript is an honest absence
                log.debug("digest: no transcript tail for %s", c["id"], exc_info=True)
            prior = _prior_outcomes(latest, c["id"], now)
            if prior:
                out[c["id"]]["prior_outcomes"] = prior
    return out


def _build_digest(cards: list[dict], now: float) -> dict:
    """The model's input for one pass. Blocking — run it with ``asyncio.to_thread``."""
    extras = _digest_extras(cards, now, prefs.get_session_review())
    return {"sessions": [_digest_entry(c, now, extras.get(c["id"])) for c in cards]}


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
    cfg: dict | None = None,
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
                action["escalation_reason"] = "degraded"
                action.pop("option", None)
        elif verb == "answer":
            text = _clamp(item.get("answer"), ANSWER_MAX)
            if text:
                action["answer"] = text
            else:
                action["verb"] = "escalate"
                action["escalation_reason"] = "degraded"
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
            if not session_input.is_live(mission_fence.physical_of(sid)):
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
            if age is not None and age >= stale_hours(cfg):
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


def auto_choose_context(session_id: str, option: int, seen_menu: object) -> dict | None:
    """What a pass needs to APPROVE a `choose` on its own, read from ONE frame — or None. Blocking.

    Asked only for a `choose` (#1060 Phase 4), and None unless every fact holds: the session is held
    by a mission that opted in (`missions.auto_choose_mission`), the live screen is the engine's own
    menu parsed server-side (`screen_menus.parse` — which refuses a permission dialog), the frame
    classifies as a `choice`, and the option the model picked is on that menu. The returned
    precondition is the fingerprint of THIS frame, so the menu whose label was bound is the screen
    `deliver` requires, byte for byte, before the first byte goes out.

    **The menu the MODEL SAW is the binding, not the one on screen now** (#1185 review 5374,
    finding 1). The model answered the digest built before its call; the screen can change during
    the call. `seen_menu` is that digest's menu for this session, and the live menu must equal it —
    question and every option — or there is no autonomous answer: "2" chosen against "2. Green"
    must never be delivered to a screen whose 2 is now "Purple". Without a seen menu (the digest
    did not describe one) there is nothing the model's number can be bound to, so no context.
    """
    from . import missions

    try:
        mission_id = missions.auto_choose_mission(session_id)
    except Exception:  # noqa: BLE001 — an unreadable store is "no grant", never a guess
        return None
    if not mission_id:
        return None
    phys = mission_fence.physical_of(session_id)
    try:
        screen = scrollback.live_tail_text(phys, PROMPT_SCREEN_CHARS)
    except Exception:  # noqa: BLE001
        return None
    # A belt: a menu `screen_menus.parse` accepts always classifies as `choice` (the classifier asks
    # the parser first), so this cannot refuse what the parse below would accept — it keeps the
    # precondition's recorded class honest if either ever changes.
    if _prompt_class(screen) != "choice":
        return None
    menu = screen_menus.parse(screen, screen_menus.engine_of(session_id))
    if menu is None or not isinstance(seen_menu, dict):
        return None
    if _digest_menu(menu) != seen_menu:
        return None  # the menu changed while the model was answering the one it saw
    picked = next((o for o in seen_menu["options"] if o.get("n") == option), None)
    if picked is None:
        return None
    return {
        "mission_id": mission_id,
        "label": picked["label"],
        # The digest form of the menu, as the model saw it: the write fence compares the live
        # screen against THIS, whole, immediately before byte one.
        "menu": seen_menu,
        "precondition": {
            "key": phys,
            "screen_fingerprint": _screen_fingerprint(screen),
            "prompt_class": "choice",
            "observed_at": time.time(),
        },
    }


def _decide(action: dict, cfg: dict, *, auto_choose: bool = False) -> tuple[str, str | None]:
    """The state a fresh proposal starts in, plus **why** it escalated when it did.

    * ``off`` — everything is a proposal; nothing is ever queued for delivery.
    * ``suggest`` — deliverable verbs queue for a tap (``proposed``).
    * ``yolo`` — a deliverable verb **inside the enforced ceiling** and at or above the
      confidence threshold is ``approved`` (Phase 2 delivers it); anything else falls back to
      the supervised path. Below threshold it is an ESCALATION, which is the whole point of the
      threshold: unsure means ask, never guess — and since #877 the kind of escalation depends on
      the verb, so that "ask" can be answered YES where there is something to run
      (``escalated_low_confidence``) rather than only dismissed (``escalated``).

    ``observe`` and ``escalate`` are decisions rather than deliveries, so they land terminal-ish
    immediately and never consult the ceiling.

    The second element is the ``escalation_reason`` — ``None`` unless the state is one of
    :data:`orchestrator_ledger.ESCALATION_STATES`:

    * ``model`` — the model read the session and chose ``escalate`` itself.
    * ``degraded`` — ``_validate_actions`` rewrote a `choose` with no usable option, or an
      `answer` with no text, into an escalation. It marks the action where it does the rewrite,
      because only that code knows what the model originally asked for; by the time the verb is
      `escalate` the intent is gone.
    * ``confidence`` — the yolo threshold gate below. Since #877 that gate writes ONE OF TWO
      states: ``escalated_low_confidence`` when the verb is deliverable (so the operator can
      approve it), plain ``escalated`` otherwise. The reason string is the same for both —
      the two roads are told apart by STATE, never by re-reading this field.

    **Three paths in, one reporter.** The UI used to append "below threshold" to every escalated
    row (`ActionRow.tsx`), which is false on two of the three paths — and unreachable on the
    third at any tier but `yolo`, since nothing below reads `confidence_min`. The fix is not a
    smarter client: a client cannot know which branch fired. It has to be reported by the
    function that fires it, which is this one. Deliberately NOT a sibling `_escalation_reason()`
    re-deriving the branch from `(action, cfg)` — two readers of the same config reaching for
    the same verdict is precisely how they drift apart.
    """
    verb = action["verb"]
    if verb == "observe":
        return "observed", None
    if verb == "escalate":
        # `_validate_actions` marks its own rewrites; anything unmarked is the model's own call.
        return "escalated", action.get("escalation_reason") or "model"
    # An operator who switched orchestration OFF while the model call was in flight must not
    # find an auto-approved action waiting for them. `enabled` is re-read after the call and
    # fences the approval path here, not just the scheduler.
    if not cfg.get("enabled", False):
        return "proposed", None
    if cfg["autonomy"] != "yolo":
        return "proposed", None
    if verb == "choose" and auto_choose:
        # THE ONE AUTONOMOUS `choose` (#1060 Phase 4): the session's mission opted in and the pass
        # verified a server-parsed menu carrying this option (`auto_choose_context`). Its own floor
        # applies whatever `confidence_min` says, and below it the operator is asked — with the
        # action runnable, so "yes" is still one tap.
        floor = max(float(cfg["confidence_min"]), prefs.ORCH_AUTO_CHOOSE_CONF_LO)
        if action["confidence"] < floor:
            return "escalated_low_confidence", "confidence"
        return "approved", None
    if verb not in set(cfg["allowed_verbs"]):
        return "proposed", None  # outside the v1 ceiling → always a tap
    if action["confidence"] < float(cfg["confidence_min"]):
        # THE ONE PLACE THE VERB IS CONSULTED (#877). A low-confidence action kept its real
        # delivering verb, so "yes" is a meaningful answer to it — unlike the `verb == "escalate"`
        # case above, which has nothing to run. They were the same state, so the operator was
        # shown a runnable `continue`, asked to look at it, and given no way to approve it: the
        # console's button 409'd because `escalated` is not claimable.
        #
        # Deciding it HERE, once, is what keeps `project_for_operator` a pure state → controls
        # table. A projection that took the verb as well would be checkable only by enumerating a
        # product of two variables, at three surfaces, each needing the verb in scope.
        #
        # This does NOT widen what may be delivered without a decision: this branch is reached
        # only where `approved` was not returned, so nothing auto-approves into the new state.
        # The guard is structural — the `return "approved"` below is the sole auto-approval, and
        # it is unreachable from here.
        if verb in DELIVERING_VERBS:
            return "escalated_low_confidence", "confidence"
        # Below the threshold with a verb that cannot be delivered anyway. There is nothing to
        # approve, so it stays the reject-only kind rather than advertising a control that would
        # have nothing to run.
        return "escalated", "confidence"
    return "approved", None


async def run_pass(
    *,
    working_keys: set[str] | None = None,
    busy_keys: Callable[[], set[str]] | None = None,
    now: float | None = None,
    offset: int = 0,
) -> dict:
    """One orchestrator pass: digest → one model call → validated proposals → ledger.

    Returns a report ``{"assessment", "actions": [...], "considered", "skipped"}``. Raises
    :class:`review.NotConfiguredError` when the AI endpoint isn't configured (the route answers
    409) and :class:`review.ReviewError` on an endpoint failure (502) — unlike a Pulse scan
    there is no useful non-LLM fallback for a decision.

    ``busy_keys`` (#969) is a SNAPSHOT FUNCTION, not a set, because it is asked twice: before the
    model call and again after it. Both calls happen here on the event loop, never in a worker.
    """
    review._require_config()  # fail fast before any FS work, like pulse_chat.ask
    cfg = prefs.get_session_assistance()
    now = time.time() if now is None else now

    busy = busy_keys() if busy_keys is not None else set()
    cards, skipped = await asyncio.to_thread(
        eligible_cards, now=now, working_keys=working_keys, busy_keys=busy
    )
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
    payload = await asyncio.to_thread(_build_digest, slice_, now)
    obj = await review.complete_json(
        [
            {"role": "system", "content": prompts.effective("orchestrator_pass")},
            {"role": "user", "content": json.dumps(payload)},
        ]
    )
    assessment, actions = _validate_actions(obj, sent, now=now, cfg=cfg)

    # The endpoint call is the long await in this function, and policy can change across it.
    # Re-read the config and re-derive eligibility BEFORE recording anything: an operator who
    # withdrew agency mid-call must not find an `approved` action waiting for them afterwards.
    cfg = prefs.get_session_assistance()
    # …and with a FRESH busy snapshot (#969): the model call takes seconds to minutes, and a
    # session that started working inside it must not come back with a proposal.
    busy = busy_keys() if busy_keys is not None else set()
    still_eligible = {c["id"] for c in await asyncio.to_thread(_eligible_ids, working_keys, busy)}
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
        state, esc_reason = _decide(action, cfg)
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
            # Where the session was when this was proposed (#1086): the scope boundary is judged
            # against THIS if it later changes — recorded evidence, never a re-scan's absence.
            "cwd": card.get("cwd") or "",
            # The session's own clock at proposal time. The bell uses it to tell "the same
            # unresolved situation, re-proposed" from "something new happened here" (#752).
            "last_activity": card.get("last_activity"),
            **{k: v for k, v in action.items() if k != "session_id"},
        }
        # `_decide` is the only writer of this field on the record. The spread above can carry a
        # `degraded` mark `_validate_actions` left on the action, so clear it first and write back
        # only what was decided — otherwise the record could disagree with its own state.
        rec["authority"] = card["automation_authority"]
        rec.pop("escalation_reason", None)
        if esc_reason:
            rec["escalation_reason"] = esc_reason
        # Only a verb that will actually be delivered needs a precondition to verify later.
        if action["verb"] in DELIVERING_VERBS:
            rec["precondition"] = await asyncio.to_thread(
                precondition_for, rec["authority"]["physical_key"]
            )
        elif action["verb"] == "escalate":
            # …and an escalation says what the screen showed (#1060), so the card can tell a
            # session parked at a menu from one that simply stopped.
            rec["observed_prompt"] = await asyncio.to_thread(
                observed_prompt_for, rec["authority"]["physical_key"]
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


def _barred_sessions() -> set[str]:
    """Sessions no action may be written for, read at the moment of writing.

    An unreadable missions store RAISES, which aborts the append rather than writing actions
    whose authorization could not be checked. Verified rather than assumed: the scheduled loop
    catches `Exception`, logs, and continues without advancing `_last_fingerprint`, so the next
    sweep re-runs the same work — and a store that stays broken backs the loop off exponentially
    (`2**consecutive_failures`, capped) rather than spinning. The manual `Run now` path maps it
    to a 503 naming the store. Guessing instead would mean an action delivered into a mission
    that was being torn down.
    """
    from . import missions

    try:
        return missions.sessions_barred_from_automation()
    except missions.MissionError:
        raise
    except Exception as e:  # noqa: BLE001 — one shape for every store failure
        raise missions.MissionError(
            "the mission store could not be read, so no action's authorization could be "
            "checked; nothing was proposed",
            status=503,
        ) from e


def _persist(records: list[dict], *, gate=None) -> list[dict]:
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
    # The MISSION fence, evaluated inside the ledger lock (#871). A mission that has been
    # abandoned or is being archived has withdrawn its sessions from automation, and the pass
    # reasons about sessions rather than missions — so without this it can mint an action for a
    # session whose mission was torn down moments ago, and under `yolo` deliver it.
    with session_input.mutation_fence():
        kept, dropped = ledger.append_batch_for_free_sessions(
            records,
            gate=gate,
            barred=_barred_sessions,
            record_guard=lambda rec: automation.check(rec)[0],
        )
    if dropped:
        log.info(
            "orchestrator: dropped %d action(s) whose session already had a live one", len(dropped)
        )
    ledger.compact_if_needed()

    # WHICH SESSIONS THIS PASS ANNOUNCES (#1057, #1086 Phase 4): those a mission holds. A session no
    # mission holds is announced by ONE producer, the needs-you episode sync (`needs_you_notify`),
    # which raises one withdrawable notification per episode and retracts it when the session no
    # longer needs the operator. Raising a second row here would announce the same situation
    # twice and leave a row nothing retracts — so for those sessions this pass says nothing, not
    # even under `notify: all`. Read once per pass, and only when there is something to announce.
    surfaces = notifications.mission_surfaces() if kept else None
    for rec in kept:
        notify = str(
            prefs.get_automation_policy(automation.scope_of(rec)).get("notify") or "escalations"
        )
        # `escalated` IS the "I'm not sure, you look" state (see _decide). `all` also covers
        # actions taken autonomously, so a yolo operator still gets a record of what was done.
        # `ESCALATION_STATES`, never `== "escalated"` (#877). A low-confidence row that missed
        # this comparison would never be announced at all — silently, and differently from the
        # six other exact comparisons that used to decide things about this state.
        if not (
            notify == "all"
            or (notify == "escalations" and rec.get("state") in ledger.ESCALATION_STATES)
        ):
            continue
        # `surfaces is None` (membership unreadable) cannot establish that the session is
        # standalone, so it fails toward announcing here, as before.
        if surfaces is not None and str(rec.get("session_id") or "") not in surfaces:
            continue
        # …except where a needs-you EPISODE is already open for it: that notification covers the
        # situation, and a second, URL-tagged one would be the duplicate its retraction cannot
        # reach (Hermes 5239, finding 4). An unreadable episode store still fails toward announcing.
        if surfaces is None and notifications.has_open_episode(str(rec.get("session_id") or "")):
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
                # …and the same set here. Missing THIS one announces the row and then never
                # counts it, which is a different silence in the same feature.
                escalation=rec.get("state") in ledger.ESCALATION_STATES,
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
        text = scrollback.live_tail_text(
            mission_fence.physical_of(session_id), EVIDENCE_SCREEN_CHARS
        )
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
