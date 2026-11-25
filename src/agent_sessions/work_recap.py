"""RECENT WORK — the chronological summary above Ask (#1086).

What the operator did across sessions in the recent-work window (1–3 days), oldest first. It is
STRUCTURED, not a paragraph: every entry is tied to one session, so the page can filter by agent and
project and expand an entry into that session's own recap without another model call.

Two sources, and the page says which it is showing:

* ``ai`` — one bounded completion over the window's per-session recaps (`prompts.effective(
  "pulse_recap")`). Its output is model text built from AGENT text, so it is treated as data:
  every entry is validated against the INPUT set (the key must be one of the sessions sent, the
  time must lie inside the window), its sentence is cleaned and capped, and anything invalid is
  DROPPED, never repaired. The page renders it as plain text.
* ``local`` — no endpoint, or nothing valid came back: one entry per session from what the app
  already stores (its last activity and the current-state line of its recap). Never an error.

The artifact is cached (``work-recap.json``) under the window and an input fingerprint, so an
unchanged window costs nothing. Reading never calls the model: `read` serves the cache — marked
``stale`` when the sessions moved on since — or local entries, and only `generate` (the refresh
route, single-flight) spends a completion.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

from . import atomicjson, prompts, pulse, review

CACHE_VERSION = 1
SESSIONS_MAX = 40  # sessions offered to the model, most recent first
SESSION_RECAP_MAX = 1500  # per-session recap sent to the model (and shown on ▸)
INPUT_MAX = 24_000  # total recap characters sent in one call
ENTRY_TEXT_MAX = 240  # one entry's sentence, after cleaning
ENTRIES_MAX = 60
# A model's clock is not ours: an entry time may sit this far past `now` and still be kept.
FUTURE_SLACK_S = 120

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def _cache_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_WORK_RECAP",
            str(Path.home() / ".config" / "agent-sessions" / "work-recap.json"),
        )
    )


def _clean(text: object, cap: int) -> str:
    if not isinstance(text, str):
        return ""
    return _SPACE.sub(" ", _CTRL.sub(" ", text)).strip()[:cap]


def _num(value: object) -> float | None:
    """A FINITE number, or ``None``. A model's ``NaN`` survives JSON parsing and every window
    comparison against it is false — so it must be refused here, not by a comparison later."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            v = float(value)
        except OverflowError:  # a valid JSON integer can exceed float range (review 5044)
            return None
        return v if math.isfinite(v) else None
    return None


def inputs_from(cards: list[dict]) -> list[dict]:
    """The sessions a recap is written from, most recent first, bounded twice (count, chars)."""
    ordered = sorted(cards, key=lambda c: _num(c.get("last_activity")) or 0.0, reverse=True)
    out: list[dict] = []
    budget = INPUT_MAX
    for c in ordered[:SESSIONS_MAX]:
        recap = str(c.get("_ai_recap") or c.get("ai_summary") or "")[:SESSION_RECAP_MAX]
        if budget - len(recap) < 0:
            break
        budget -= len(recap)
        p = c.get("project") or {}
        out.append(
            {
                "key": str(c.get("id") or ""),
                "engine": str(c.get("engine") or ""),
                "title": str(c.get("title") or ""),
                "project": {"id": str(p.get("id") or ""), "name": str(p.get("name") or "")},
                "last_activity": _num(c.get("last_activity")) or 0.0,
                "recap": recap,
            }
        )
    return [i for i in out if i["key"]]


def fingerprint(inputs: list[dict], window_days: int) -> str:
    payload = json.dumps(
        {
            "window_days": window_days,
            "sessions": sorted([i["key"], i["last_activity"], i["recap"]] for i in inputs),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _state_line(recap: str) -> str:
    """The recap's CURRENT-STATE line — its LAST non-empty line, by the recap prompt's own contract.

    Not the first: the recap is chronological and puts where things stand at the end.
    """
    lines = [ln.strip() for ln in recap.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def local_entries(inputs: list[dict]) -> list[dict]:
    """One entry per session from stored facts only — no model, never empty for a non-empty set."""
    entries = [
        {
            "session_key": i["key"],
            "ts": i["last_activity"],
            "text": _clean(_state_line(i["recap"]) or i["title"] or "Active", ENTRY_TEXT_MAX),
        }
        for i in inputs
    ]
    entries.sort(key=lambda e: e["ts"])
    return entries


def validate(obj: object, inputs: list[dict], *, window_start: float, now: float) -> list[dict]:
    """The model's entries that survive the input set and the window. Drops, never repairs."""
    keys = {i["key"] for i in inputs}
    raw = obj.get("entries") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for e in raw[: ENTRIES_MAX * 2]:
        if not isinstance(e, dict):
            continue
        key = e.get("session_key")
        ts = _num(e.get("ts"))
        text = _clean(e.get("text"), ENTRY_TEXT_MAX)
        if not isinstance(key, str) or key not in keys or not text:
            continue
        if ts is None or ts < window_start or ts > now + FUTURE_SLACK_S:
            continue
        out.append({"session_key": key, "ts": ts, "text": text})
    out.sort(key=lambda e: e["ts"])
    return out[-ENTRIES_MAX:]


def _join(entries: list[dict], inputs: list[dict]) -> list[dict]:
    """Entries with the session facts the page filters and expands on. An entry whose session has
    left the window is dropped: the page must never show a row it cannot place."""
    by_key = {i["key"]: i for i in inputs}
    out = []
    for e in entries:
        i = by_key.get(e.get("session_key"))
        if i is None:
            continue
        out.append(
            {
                **e,
                "engine": i["engine"],
                "title": i["title"],
                "project": i["project"],
                "session_recap": i["recap"],
            }
        )
    return out


def _in_window(entries: list[dict], *, window_days: int, now: float) -> list[dict]:
    """Entries whose time is still inside the ROLLING window. The window moves with the clock, so
    an entry that was valid when written ages out of it, and a cached summary must not show it."""
    start = now - window_days * 86400
    return [e for e in entries if start <= e["ts"] <= now + FUTURE_SLACK_S]


def _sane_entries(raw: object) -> list[dict]:
    """Cached entries re-checked on LOAD: a file is input too (hand-edited, or written by an older
    build), and one non-finite time in it would make every response fail to serialise."""
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        key, ts = e.get("session_key"), _num(e.get("ts"))
        text = _clean(e.get("text"), ENTRY_TEXT_MAX)
        if isinstance(key, str) and key and ts is not None and text:
            out.append({"session_key": key, "ts": ts, "text": text})
    return out


def _load() -> dict | None:
    path = _cache_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("cache_version") != CACHE_VERSION:
        return None
    stored = raw.get("entries")
    raw["entries"] = _sane_entries(stored)
    # An artifact that needed cleaning on load is DIRTY: served sanitised, but never trusted as an
    # unchanged cache hit, so the next refresh replaces the file (review 5044's note).
    raw["_dirty"] = not isinstance(stored, list) or len(raw["entries"]) != len(stored)
    return raw


def _cached_ai(window_days: int, now: float) -> tuple[dict, list[dict], bool] | None:
    """``(cache, entries still in the window, whether it is out of date)`` for an ``ai`` artifact
    of this window — or ``None`` when there is none, or nothing in it is still inside the window.
    Out of date = an entry aged out of the rolling window, or the file needed cleaning on load."""
    cached = _load()
    if not cached or cached.get("window_days") != window_days or cached.get("source") != "ai":
        return None
    kept = _in_window(cached["entries"], window_days=window_days, now=now)
    if not kept:
        return None
    return cached, kept, bool(cached.get("_dirty")) or len(kept) != len(cached["entries"])


def _payload(
    entries: list[dict],
    inputs: list[dict],
    *,
    window_days: int,
    source: str,
    generated_at: float | None,
    stale: bool,
    configured: bool,
    error: str | None = None,
) -> dict:
    return {
        "window_days": window_days,
        "source": source,
        "generated_at": generated_at,
        "stale": stale,
        "configured": configured,
        "error": error,
        "entries": _join(entries, inputs),
    }


def read(
    cards: list[dict], *, window_days: int, configured: bool, now: float | None = None
) -> dict:
    """What the page shows now. Never calls the model; blocking (run it off the event loop).

    A cached summary is served only for its own window, only with the entries still INSIDE the
    rolling window, and is ``stale`` when the sessions changed OR an entry aged out — either way a
    refresh would write something different.
    """
    now = time.time() if now is None else now
    inputs = inputs_from(cards)
    fp = fingerprint(inputs, window_days)
    hit = _cached_ai(window_days, now)
    if hit is not None:
        cached, kept, aged_out = hit
        return _payload(
            kept,
            inputs,
            window_days=window_days,
            source="ai",
            generated_at=_num(cached.get("generated_at")),
            stale=aged_out or cached.get("input_fingerprint") != fp,
            configured=configured,
        )
    return _payload(
        local_entries(inputs),
        inputs,
        window_days=window_days,
        source="local",
        generated_at=None,
        # Local entries are always current; "stale" means a refresh would produce something newer,
        # which is only true when an endpoint could write it.
        stale=configured and bool(inputs),
        configured=configured,
    )


async def generate(cards: list[dict], *, window_days: int, now: float | None = None) -> dict:
    """Write a fresh ``ai`` recap for the window (one completion) and return what `read` would.

    Unchanged inputs are a cache hit with no call. No endpoint, or no valid entry back, is the
    ``local`` payload with the reason in ``error`` — the previous ``ai`` artifact is kept, never
    overwritten with a worse one.
    """
    now = time.time() if now is None else now
    inputs = inputs_from(cards)
    fp = fingerprint(inputs, window_days)
    hit = _cached_ai(window_days, now)
    # A hit is the SAME inputs with nothing aged out of the rolling window. Anything else is a
    # miss: serving a summary that still shows an expired entry is what `stale` exists to prevent.
    if hit is not None and not hit[2] and hit[0].get("input_fingerprint") == fp:
        return read(cards, window_days=window_days, configured=True, now=now)
    if not inputs:
        return read(cards, window_days=window_days, configured=True, now=now)
    window_start = now - window_days * 86400
    user = {
        "window": {"from": window_start, "to": now, "days": window_days},
        "sessions": [
            {
                "key": i["key"],
                "title": i["title"],
                "project": i["project"]["name"],
                "agent": i["engine"],
                "last_activity": i["last_activity"],
                "recap": i["recap"],
            }
            for i in inputs
        ],
    }
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("pulse_recap")},
                {"role": "user", "content": json.dumps(user)},
            ]
        )
    except review.NotConfiguredError:
        return read(cards, window_days=window_days, configured=False, now=now)
    except review.ReviewError as e:
        out = read(cards, window_days=window_days, configured=True, now=now)
        out["error"] = _clean(str(e), 200) or "the AI endpoint did not answer"
        return out
    entries = validate(obj, inputs, window_start=window_start, now=now)
    if not entries:
        out = read(cards, window_days=window_days, configured=True, now=now)
        out["error"] = "the AI endpoint returned no usable entries"
        return out
    atomicjson.atomic_write_json(
        _cache_path(),
        {
            "cache_version": CACHE_VERSION,
            "window_days": window_days,
            "input_fingerprint": fp,
            "generated_at": now,
            "source": "ai",
            "entries": entries,
        },
    )
    return _payload(
        entries,
        inputs,
        window_days=window_days,
        source="ai",
        generated_at=now,
        stale=False,
        configured=True,
    )


def cards_for(window_days: int) -> list[dict]:
    """The in-window cards WITH the internal recap field `inputs_from` reads. Blocking."""
    return pulse.build_cards(window_days=window_days)
