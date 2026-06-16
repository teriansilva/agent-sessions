"""Pulse — AI-curated recent-work overview scan engine + cache (#441 Phase 2).

Reads the recent sessions (a rolling window, default 3 days), curates them into a ranked,
grouped overview the user can jump straight back into, and caches the result so the page
loads instantly. This ``fast`` slice does **no** LLM work — it ranks/flags purely from the
per-session AI-review summaries already on the sidecar (#356). Synthesis depths
(``medium`` / ``slow``) layer on in #441 Phase 4; the artifact already carries
``scan_depth`` / ``banner`` / ``synthesis_skipped`` so the shape is stable.

Curation rides the exact same per-session resolution the sidebar uses (``engines.scan_all``
→ metadata sidecar via the alias layer → ``projects.resolve``), so a Pulse card and its
sidebar row always agree.

Cache (``pulse-cache.json``, next to ``prefs.json``):

* Written atomically (temp file + ``os.replace``, ``0600``) — a single-flight scan is the
  only writer, so no flock is needed.
* ``cache_version`` guards the artifact shape: a mismatch is a cache **miss** (the stale
  artifact is ignored, never mis-rendered against newer card/banner code).
* ``input_fingerprint`` (sha256 over the in-window session set) is what a future background
  loop (#441 Phase 3) compares to skip a no-op scan; a manual "Scan now" always runs (at
  ``fast`` it is free anyway).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from . import engines, metadata, projects

# Bump when the artifact shape (cards / banner / top-level fields) changes incompatibly —
# `load_cache` treats any other version as a miss so an old shape never renders wrong.
CACHE_VERSION = 1

WINDOW_DAYS_DEFAULT = 3
WINDOW_DAYS_MIN = 1
WINDOW_DAYS_MAX = 30

SCAN_DEPTHS: tuple[str, ...] = ("fast", "medium", "slow")
DEFAULT_DEPTH = "fast"

# Within the window, a session touched more recently than this is "recently_active"; older
# (but still in-window) is "idle". Live sessions are "in_flight" regardless.
RECENT_ACTIVE_S = 24 * 3600

# Sort priority per state (lower = nearer the top); within a state, newest activity first.
_STATE_ORDER = {"needs_you": 0, "in_flight": 1, "recently_active": 2, "idle": 3}


def _cache_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_PULSE_CACHE",
            str(Path.home() / ".config" / "agent-sessions" / "pulse-cache.json"),
        )
    )


def coerce_window_days(value: object) -> int:
    """Narrow any input to a window in ``[MIN, MAX]`` days, falling back to the default."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(WINDOW_DAYS_MIN, min(WINDOW_DAYS_MAX, value))
    return WINDOW_DAYS_DEFAULT


def coerce_depth(value: object) -> str:
    """Narrow any input to a known scan depth, falling back to the default."""
    return value if isinstance(value, str) and value in SCAN_DEPTHS else DEFAULT_DEPTH


def _classify(m: metadata.SessionMeta, last_mtime: float, live: bool, now: float) -> str:
    if m.intervention_required:
        return "needs_you"
    if live:
        return "in_flight"
    if now - last_mtime <= RECENT_ACTIVE_S:
        return "recently_active"
    return "idle"


def build_cards(
    *, window_days: int, now: float | None = None, working_keys: set[str] | None = None
) -> list[dict]:
    """Curate the in-window, non-archived sessions into ranked cards. Pure FS + metadata, no
    network — safe to run under ``asyncio.to_thread``.

    A card is dropped when the session is effectively archived (sidecar override wins over the
    native state, mirroring the sidebar) or review-excluded, or its last activity is older than
    the window. ``working_keys`` (logical or physical session keys currently live) marks cards
    ``live`` → state ``in_flight``.
    """
    now = time.time() if now is None else now
    working = working_keys or set()
    cutoff = now - window_days * 86400
    meta_index = metadata.load()
    aliases = metadata.load_aliases()
    project_index = projects.load()

    cards: list[dict] = []
    for s in engines.scan_all():
        key = engines.session_key(s)
        phys = engines.physical_key(key, aliases)
        m = meta_index.get(key) or meta_index.get(phys) or metadata.SessionMeta()
        archived = m.archived if m.archived is not None else s.archived
        if archived or m.review_excluded:
            continue
        if s.last_mtime < cutoff:
            continue
        live = key in working or phys in working
        cards.append(
            {
                "id": key,
                "engine": s.engine,
                "title": metadata.display_title(m, s.first_user_message),
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
                # Internal: feeds the input fingerprint; stripped from the public artifact.
                "_review_fingerprint": m.review_fingerprint,
            }
        )
    cards.sort(key=lambda c: (_STATE_ORDER[c["state"]], -c["last_activity"]))
    return cards


def _fingerprint(cards: list[dict], window_days: int, depth: str) -> str:
    """sha256 over the in-window session set + scan params. Stable across scans when nothing
    relevant changed (so #441 Phase 3's loop can skip a no-op scan); changes when a session's
    activity or its review result changes."""
    payload = json.dumps(
        {
            "window_days": window_days,
            "depth": depth,
            "sessions": sorted(
                [c["id"], c["last_activity"], c["_review_fingerprint"]] for c in cards
            ),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _artifact(
    cards: list[dict],
    *,
    window_days: int,
    depth: str,
    banner: str | None,
    synthesis_skipped: bool,
    now: float,
) -> dict:
    fingerprint = _fingerprint(cards, window_days, depth)
    public_cards = [{k: v for k, v in c.items() if not k.startswith("_")} for c in cards]
    return {
        "cache_version": CACHE_VERSION,
        "generated_at": now,
        "window_days": window_days,
        "scan_depth": depth,
        "input_fingerprint": fingerprint,
        "synthesis_skipped": synthesis_skipped,
        "banner": banner,
        "cards": public_cards,
    }


def empty_overview(window_days: int = WINDOW_DAYS_DEFAULT, depth: str = DEFAULT_DEPTH) -> dict:
    """The "never scanned" artifact `GET /api/pulse` returns before the first scan (or on a
    cache miss). Same shape as a real artifact, with no cards and a null ``generated_at``."""
    return {
        "cache_version": CACHE_VERSION,
        "generated_at": None,
        "window_days": coerce_window_days(window_days),
        "scan_depth": coerce_depth(depth),
        "input_fingerprint": None,
        "synthesis_skipped": False,
        "banner": None,
        "cards": [],
    }


def _write_cache(artifact: dict, path: Path | None = None) -> None:
    path = path or _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as fh:
        json.dump(artifact, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_cache(path: Path | None = None) -> dict | None:
    """The cached artifact, or ``None`` on a missing / unreadable / version-mismatched cache
    (a mismatch is a deliberate miss so an old shape is never rendered against newer code)."""
    path = path or _cache_path()
    if not path.exists():
        return None
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("cache_version") != CACHE_VERSION:
        return None
    return raw


async def run_scan(
    *,
    window_days: int = WINDOW_DAYS_DEFAULT,
    depth: str = DEFAULT_DEPTH,
    working_keys: set[str] | None = None,
    now: float | None = None,
) -> dict:
    """Run one Pulse scan and write the cache, returning the fresh artifact.

    The ``fast`` slice (this phase) is curation only — no endpoint call, so an unconfigured
    AI gateway is irrelevant here. ``medium`` / ``slow`` synthesis lands in #441 Phase 4; for
    now any depth produces the ``fast`` curation with ``banner=None`` and
    ``synthesis_skipped=False`` (nothing was requested to skip). The caller owns the
    single-flight guard (so two scans never overlap) and supplies ``working_keys``.
    """
    window_days = coerce_window_days(window_days)
    depth = coerce_depth(depth)
    now = time.time() if now is None else now
    cards = await asyncio.to_thread(
        build_cards, window_days=window_days, now=now, working_keys=working_keys
    )
    artifact = _artifact(
        cards,
        window_days=window_days,
        depth=depth,
        banner=None,
        synthesis_skipped=False,
        now=now,
    )
    _write_cache(artifact)
    return artifact
