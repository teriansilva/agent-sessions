"""What a mission's sessions are doing RIGHT NOW — observation only, no model call (#1064).

The supervisor's recap is the narrative, and it costs a model call, so it is paced on a wall clock
(`mission_pace`, #1214) even though running missions are swept every 30 s. Between recaps a running
mission used to show nothing at all, and silence reads the same
whether the agent is working, stuck, or waiting. This is the cheap layer under the recap: facts the
app already has, derived into one word per held session.

**Observation, never a decision.** "At a prompt" is a fact about the screen, not a claim that the
operator is needed — the decision card stays the only "needs your call", in agreement with the
bell. And nothing here reads objective state: "producing output" is not "working on goal 2".

**State, never an event.** Nothing is stored and nothing is appended; `mission_events` stays the
audit log. The screen is read only when a session has gone quiet (the only time its prompt class
matters), through the same read-only scrollback path `VIEW SCREEN` uses — no lease, no attach — and
that read is cached briefly so many open tabs cost one read per session per TTL.

**Derived fields only leave the server.** No screen text, no transcript, no command lines.
"""

from __future__ import annotations

import threading
import time

from . import orchestrator, scrollback

#: No visible output for this long reads as QUIET rather than producing. Visible output only —
#: `get_last_visible_output_at` already ignores a title blink (#969).
QUIET_AFTER_S = 20.0
#: A quiet session showing a prompt reads as AT A PROMPT only once it has settled this long, so a
#: question mid-stream is not reported as a wait.
PROMPT_SETTLE_S = 3.0
#: How long one screen classification is reused across requests.
SCREEN_CACHE_TTL_S = 5.0
#: Prompt classes that mean the agent is waiting on input rather than simply silent.
WAITING_CLASSES = frozenset({"choice", "confirm", "question"})

STATUSES = ("producing", "at_prompt", "quiet", "unobserved")

_cache: dict[str, tuple[float, str]] = {}
_cache_lock = threading.Lock()


def derive(
    *,
    now: float,
    last_output_at: float | None,
    prompt_class: str | None,
    recap_at: float | None,
) -> dict:
    """One held session's strip line, from facts alone. Pure, so every branch is a unit test."""
    if last_output_at is None:
        # NOTHING OBSERVED is not QUIET (Phase 3): an evicted buffer, a restart with no viewer, a
        # session whose output never passed through this process — we cannot say it is silent.
        status, since = "unobserved", None
    else:
        since = max(0.0, now - last_output_at)
        if prompt_class in WAITING_CLASSES and since >= PROMPT_SETTLE_S:
            status = "at_prompt"
        elif since < QUIET_AFTER_S:
            status = "producing"
        else:
            status = "quiet"
    return {
        "status": status,
        "seconds_since_output": None if since is None else round(since),
        "prompt_class": prompt_class if status == "at_prompt" else None,
        "recap_age_s": None if recap_at is None else round(max(0.0, now - recap_at)),
        # A recap written BEFORE the latest output is a narrative about an earlier screen, and must
        # never read as current.
        "recap_older_than_output": bool(
            recap_at is not None and last_output_at is not None and recap_at < last_output_at
        ),
    }


def prompt_class_cached(physical_key: str, *, now: float | None = None) -> str:
    """The session's prompt class, reused for `SCREEN_CACHE_TTL_S`. Blocking — call off the loop."""
    t = time.monotonic() if now is None else now
    with _cache_lock:
        hit = _cache.get(physical_key)
        if hit and t - hit[0] < SCREEN_CACHE_TTL_S:
            return hit[1]
    try:
        screen = scrollback.live_tail_text(physical_key, _screen_chars())
    except Exception:  # noqa: BLE001 — an unreadable ring is "open", never an error on the strip
        screen = ""
    cls = orchestrator._prompt_class(screen)
    with _cache_lock:
        _cache[physical_key] = (t, cls)
        if len(_cache) > 512:  # bounded; oldest out
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - 512]:
                _cache.pop(k, None)
    return cls


def rest_episode(physical_key: str, *, now: float | None = None) -> tuple[bool, str | None]:
    """Has this session COME TO REST on its screen? ``(observed, episode)`` — blocking (#1214).

    ``observed`` is False when this process has no visible-output clock for the session (nothing
    passed through a ring since boot, or it was evicted): the screen cannot say, and the caller
    falls back to the engine store. Otherwise ``episode`` is ``None`` while the session is
    producing, and an id naming the output it rests on once it is either

    * at a WAITING prompt (`WAITING_CLASSES`) settled for `PROMPT_SETTLE_S`, or
    * silent for `QUIET_AFTER_S`, whatever the screen shows — an unrecognised dialog is still a
      stop, and the classifier is a hint, not proof, in both directions.

    Both are the SAME episode (the output stamp), so a prompt read at 3 s is not read again when it
    turns quiet at 20 s; new output starts a new one. The screen is read only in the 3–20 s window,
    through the same cached classification the strip uses.
    """
    last = scrollback.get_last_visible_output_at(physical_key)
    if last is None:
        return False, None
    t = time.time() if now is None else now
    since = t - last
    episode = f"screen:{last!r}"
    if since >= QUIET_AFTER_S:
        return True, episode
    if since >= PROMPT_SETTLE_S and prompt_class_cached(physical_key) in WAITING_CLASSES:
        return True, episode
    return True, None


def _screen_chars() -> int:
    """The window the prompt class is judged from — the same one the precondition uses, whichever
    constant this build names it by."""
    return int(getattr(orchestrator, "PROMPT_SCREEN_CHARS", orchestrator.PRECONDITION_CHARS))


def reset_cache_for_test() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "PROMPT_SETTLE_S",
    "QUIET_AFTER_S",
    "SCREEN_CACHE_TTL_S",
    "STATUSES",
    "derive",
    "prompt_class_cached",
    "rest_episode",
]
