"""Pulse cards — the recent-work curation needs-you, the recap, HQ and the Missions view share.

``build_cards`` reads the recent sessions (a rolling window, 1–3 days) and curates them into
ranked cards. Pure FS + metadata, no network and no model call. It rides the exact same
per-session resolution the sidebar uses (``engines.scan_all`` → metadata sidecar via the alias
layer → ``projects.resolve``), so a card and its sidebar row always agree.

``GET /api/pulse`` serves :func:`overview`, built live per request (#2179). There used to be a
background scan (``pulse_loop``), a ``fast``/``slow`` depth with a per-session model-written
line, and a ``pulse-cache.json`` the GET read. The only thing anyone saw of it was the Missions
shelf's session titles, which the live cards carry, so the scan, its model calls and its cache
were retired. A ``pulse-cache.json`` an older release wrote is left on disk and never read.
"""

from __future__ import annotations

import time

from . import engines, metadata, prefs, projects

# 1–3 days, default 1 (#1086) — the same numbers as `prefs.PULSE_WINDOW_*`, which own the rule.
WINDOW_DAYS_DEFAULT = 1
WINDOW_DAYS_MIN = 1
WINDOW_DAYS_MAX = 3

# Within the window, a session touched more recently than this is "recently_active"; older
# (but still in-window) is "idle". Live sessions are "in_flight" regardless.
RECENT_ACTIVE_S = 24 * 3600

# Sort priority per state (lower = nearer the top); within a state, newest activity first.
_STATE_ORDER = {"needs_you": 0, "in_flight": 1, "recently_active": 2, "idle": 3}


def coerce_window_days(value: object) -> int:
    """Narrow any input to a window in ``[MIN, MAX]`` days — `prefs.coerce_pulse_window_days`,
    the one read rule (#1086), so a card read and the stored pref can never disagree."""
    return prefs.coerce_pulse_window_days(value)


def _classify(m: metadata.SessionMeta, last_mtime: float, live: bool, now: float) -> str:
    # ``last_mtime`` is the last real-activity time (#525 — newest conversation-record timestamp,
    # not the raw file mtime), so "recently_active" reflects genuine turns, not an idle re-open.
    if m.intervention_required:
        return "needs_you"
    if live:
        return "in_flight"
    if now - last_mtime <= RECENT_ACTIVE_S:
        return "recently_active"
    return "idle"


class CardsIncomplete(Exception):
    """A STRICT card read could not establish its whole input (#1086 Phase 4, Hermes 5231): a
    provider's store, or the metadata sidecar, could not be read completely. Unknown is not
    "no session needs you" — a caller that would retract something on the answer must not."""


def checked_scan() -> list:
    """One complete engine walk for a strict card read, or :class:`CardsIncomplete`."""
    rows, problems = engines.scan_all_checked()
    if problems:
        raise CardsIncomplete("; ".join(problems))
    return rows


def build_cards(
    *,
    window_days: int | None,
    now: float | None = None,
    working_keys: set[str] | None = None,
    strict: bool = False,
    scanned: list | None = None,
    checked: tuple | None = None,
) -> list[dict]:
    """Curate the in-window, non-archived sessions into ranked cards. Pure FS + metadata, no
    network — safe to run under ``asyncio.to_thread``.

    A card is dropped when the session is effectively archived (sidecar override wins over the
    native state, mirroring the sidebar) or review-excluded, or its last activity is older than
    the window. ``working_keys`` (logical or physical session keys currently live) marks cards
    ``live`` → state ``in_flight``.

    ``window_days=None`` disables the recency cutoff (full history): HQ's find catalog
    (``pulse_chat.build_catalog``, #522), the orchestrator's eligible set and the session details
    read use it. ``GET /api/pulse`` (:func:`overview`) always passes a concrete window.

    ``strict`` (#1086 Phase 4) is for a caller that RETRACTS on the answer — the needs-you
    notification sync. Its inputs are read so that an incomplete read raises
    :class:`CardsIncomplete` instead of silently dropping a session or clearing its flags: the
    metadata under the writers' lock (`metadata.load_checked`, which also sees through the
    zero-byte window of an in-place rewrite) and a checked engine walk (``scanned``, from
    :func:`checked_scan`, so two calls in one decision share one walk). Every other caller keeps
    the fail-soft read. ``checked``: a `metadata.load_checked` snapshot the caller already holds,
    so the cards are curated through the SAME metadata and aliases the caller joins with (#1882).
    """
    now = time.time() if now is None else now
    working = working_keys or set()
    cutoff = None if window_days is None else now - window_days * 86400
    if strict:
        try:
            meta_index, aliases, _overrides = (
                checked if checked is not None else metadata.load_checked()
            )
        except metadata.MetadataUnreadable as e:
            raise CardsIncomplete(f"session metadata: {e}") from e
        sessions = scanned if scanned is not None else checked_scan()
    else:
        meta_index = metadata.load()
        aliases = metadata.load_aliases()
        sessions = scanned if scanned is not None else engines.scan_all()
    project_index = projects.load()
    # The card title is the one the operator sees (notifications, Ask), so it follows the same
    # "keep session titles" choice as the list (#1510).
    use_ai_title = prefs.renames_sessions()

    cards: list[dict] = []
    for s in sessions:
        key = engines.session_key(s)
        phys = engines.physical_key(key, aliases)
        m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
        archived = m.archived if m.archived is not None else s.archived
        if archived or m.review_excluded:
            continue
        if cutoff is not None and s.last_mtime < cutoff:
            continue
        live = key in working or phys in working
        cards.append(
            {
                "id": key,
                "engine": s.engine,
                "title": metadata.display_title(m, s.first_user_message, use_ai_title=use_ai_title),
                "cwd": s.cwd,
                "project": projects.resolve(
                    s.cwd, m.project_id, project_index, alias=m.project_alias
                ).as_dict(),
                "last_activity": s.last_mtime,
                "ai_summary": m.ai_summary,
                "intervention_required": m.intervention_required,
                "intervention_reason": m.intervention_reason,
                "reviewed_at": m.reviewed_at,
                "live": live,
                "state": _classify(m, s.last_mtime, live, now),
                # Internal fields (`_`-prefixed) never leave the server: `public_cards` strips
                # them. The orchestrator's skip-fingerprint reads the two fingerprints; the
                # decision pass, the recap and HQ's find read the recap (#611).
                "_review_fingerprint": m.review_fingerprint,
                "_ai_recap": m.ai_recap,
                "_recap_fingerprint": m.recap_fingerprint,
                # Internal (#1020): the structured assessment and the failed-refresh stamp the
                # decision pass projects it with (`assessment.project`).
                "_ai_assessment": m.ai_assessment,
                "_review_failed_at": m.review_failed_at,
            }
        )
    cards.sort(key=lambda c: (_STATE_ORDER[c["state"]], -c["last_activity"]))
    return cards


def public_cards(cards: list[dict]) -> list[dict]:
    """The cards as they may leave the server: every ``_``-prefixed (internal) field stripped.
    The recap, the assessment and the fingerprints are server inputs, never response fields."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in cards]


def overview(
    *,
    window_days: int,
    working_keys: set[str] | None = None,
    now: float | None = None,
) -> dict:
    """What ``GET /api/pulse`` serves (#2179): the in-window cards, built now, public fields only.

    Blocking (an engine walk + the metadata read): call it under ``asyncio.to_thread``. The
    route lays the ledger overlay on top (``routes.pulse._attach_pending``).
    """
    now = time.time() if now is None else now
    window_days = coerce_window_days(window_days)
    cards = build_cards(window_days=window_days, now=now, working_keys=working_keys)
    return {"generated_at": now, "window_days": window_days, "cards": public_cards(cards)}
