"""Holds on permission dialogs whose answer may have reached the agent (#1213, #1218 review 5405).

An operator's answer to an agent's tool-permission dialog can end in a state where nobody knows
whether the keys landed: a partial write, a delivering process that died, a claim nobody settled.
A second answer to the SAME dialog must then never be invited — the first may be sitting in the
agent's input, and "Allow always" twice is not "Allow always" once.

So every answer takes a HOLD on its dialog (session + the dialog's lossless identity) **before it
is claimed**, in the missions store (`permission_holds`, owned by no mission and outside the action
ledger — neither ledger compaction nor mission retention can drop one). While a hold is open, the
dialog is neither answered (`menu_answer`) nor offered on a new card (`mission_permission`).

A hold is released ONLY on positive evidence:

* at answer time, by the writer's own verdict — ``delivered`` (the keys went out in full to the
  dialog verified at byte one) or a zero-byte refusal (nothing was typed);
* later, by :func:`reconcile`: a trusted frame (`scrollback.live_tail_frame` — fail-closed, see
  `vtscreen.render_cells`) showing a DIFFERENT complete permission dialog, or the engine's own
  transcript having grown since the hold was taken (the agent acted).

Nothing else releases one: not time, not an empty or half-drawn screen, not a screen this app
cannot parse. An unreadable store is never "nothing is held" — callers refuse.
"""

from __future__ import annotations

from pathlib import Path

from . import engines, missions, permission_prompts, screen_menus, transcript


class HoldsUnreadable(Exception):
    """The holds could not be read or written, so nothing may be answered or offered."""


def growth_mark(session_key: str) -> int | None:
    """The engine's own monotonic transcript mark for the session, or None when unmeasurable."""
    try:
        provider, native = engines.parse_key(session_key)
        return transcript.growth_mark(provider.engine_id, native, Path.home())
    except Exception:  # noqa: BLE001 — unmeasurable is "no evidence", never an error
        return None


def open_holds(session_key: str, identity: str | None = None) -> list[dict]:
    """The session's unreleased holds (for one dialog when ``identity`` is given). Blocking.
    Raises :class:`HoldsUnreadable` on any store error."""
    try:
        return missions.permission_holds_open(session_key, identity)
    except Exception as e:  # noqa: BLE001
        raise HoldsUnreadable(type(e).__name__) from e


def take(choose_id: str, *, session_key: str, identity: str, mission_id: str | None) -> None:
    """Record the hold BEFORE the answer is claimed. Raises :class:`HoldsUnreadable`; the caller
    must then send nothing."""
    try:
        missions.permission_hold_add(
            choose_id,
            session_key=session_key,
            identity=identity,
            mission_id=mission_id,
            growth_mark=growth_mark(session_key),
        )
    except Exception as e:  # noqa: BLE001
        raise HoldsUnreadable(type(e).__name__) from e


def release(choose_id: str, by: str) -> bool:
    """Release one hold on the writer's own evidence. Best-effort: a hold that stays open only
    ever refuses more, never less."""
    try:
        return missions.permission_hold_release(choose_id, by)
    except Exception:  # noqa: BLE001
        return False


def reconcile(session_key: str) -> list[str]:
    """Release the session's holds that POSITIVE evidence shows are resolved. Returns the released
    answer ids. Blocking; never raises.

    Evidence, and only this: a trusted frame showing a different COMPLETE permission dialog, or the
    engine's transcript grown past the mark taken with the hold. An empty, untrusted, half-drawn
    or unrecognised screen proves nothing and releases nothing.
    """
    try:
        holds = missions.permission_holds_open(session_key)
    except Exception:  # noqa: BLE001
        return []
    if not holds:
        return []
    from . import mission_fence, orchestrator, scrollback

    showing = ""
    try:
        text, cells = scrollback.live_tail_frame(
            mission_fence.physical_of(session_key), orchestrator.PROMPT_SCREEN_CHARS
        )
        if text.strip() and cells is not None:
            live = permission_prompts._parse_any(text, screen_menus.engine_of(session_key), cells)
            showing = str((live or {}).get("identity") or "")
    except Exception:  # noqa: BLE001
        showing = ""
    mark = growth_mark(session_key)
    released: list[str] = []
    for h in holds:
        why = ""
        if showing and showing != h.get("identity"):
            why = "a different permission dialog is showing"
        elif mark is not None and h.get("growth_mark") is not None and mark > int(h["growth_mark"]):
            why = "the agent's transcript advanced after the answer"
        if why and release(str(h["choose_id"]), why):
            released.append(str(h["choose_id"]))
    return released


__all__ = ["HoldsUnreadable", "growth_mark", "open_holds", "reconcile", "release", "take"]
