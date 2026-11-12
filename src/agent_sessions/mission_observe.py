"""What a mission-held session is doing, whatever its transport (#1900, #1273 P3d).

Mission supervision read the terminal for everything but stall: the Now strip's output clock and
prompt class, the permission dialog, the VIEW SCREEN block. An API session has no terminal, so
every one of those read as empty, and a pending native approval reached nobody. This is the one
place that knows the difference. Callers ask it; none of them branches on the transport itself.

* :func:`facts` is the observation for one session key. For an API session it reads the
  structured snapshot: its state, the running turn, the newest turn's time and the pending
  requests. For a console session it reads the output clock, exactly as the strip did.
* :func:`now_line` turns that observation into the strip's line, in the strip's existing status
  vocabulary, so the client keeps one renderer.
* :func:`pending_items` is the bounded, display-only view of a pending request set: the request
  id, the tool and one short label. It never carries the request's payload or digest; deciding
  stays in the session's own pane, where the digest binding lives.

Observation only. Nothing here decides, writes, or sends a byte. An unreadable snapshot is
"unobserved", never "idle": a session that can't be read must not look free.
"""

from __future__ import annotations

import asyncio
import re

from . import mission_now, scrollback

#: The snapshot states that mean the session's own conversation is working.
BUSY_STATES = frozenset({"queued", "running", "delivering", "awaiting_approval"})
#: At most this many pending requests are itemised on a card or strip; the count is always exact.
SHOWN_ITEMS = 3
#: A label is one line of at most this many characters.
LABEL_CHARS = 120
#: The question tool, by the name Claude reports (`native_questions.ASK_TOOL`).
_ASK_TOOL = "AskUserQuestion"
#: The payload keys a label may come from, in order. Never the content being written.
_LABEL_KEYS = ("file_path", "notebook_path", "path", "command", "url", "pattern")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def is_api(session_key: str) -> bool:
    """Is ``session_key`` an API session? Never raises (an unresolvable key is not)."""
    from . import mission_structured

    return mission_structured.is_api_key(session_key)


def pane_path(session_key: str) -> str:
    """The structured pane's route for a session (`/s/<engine>/<id>`)."""
    engine, _, native = session_key.partition(":")
    return f"/s/{engine}/{native}"


def _one_line(value: object) -> str:
    text = _CONTROL.sub(" ", str(value or "")).strip()
    return text if len(text) <= LABEL_CHARS else text[: LABEL_CHARS - 1] + "…"


def _label(kind: str, payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if kind == _ASK_TOOL:
        questions = payload.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            return _one_line(questions[0].get("question"))
        return ""
    for key in _LABEL_KEYS:
        if isinstance(payload.get(key), str) and payload[key].strip():
            return _one_line(payload[key])
    return ""


def pending_items(requests: list[dict]) -> list[dict]:
    """``[{request_id, kind, label, question}]`` for a snapshot's pending requests. Bounded and
    display-only: no payload, no digest, no choices."""
    out = []
    for r in requests or ():
        if not isinstance(r, dict) or not r.get("request_id"):
            continue
        kind = str(r.get("kind") or "tool")
        out.append(
            {
                "request_id": str(r["request_id"]),
                "kind": "Question" if kind == _ASK_TOOL else _one_line(kind)[:40],
                "label": _label(kind, r.get("payload")),
                "question": kind == _ASK_TOOL,
            }
        )
    return out


def _turn_time(snap: dict) -> float | None:
    """The newest operator turn's latest time (its reply if it has one), or None."""
    for turn in reversed(snap.get("turns") or ()):
        if turn.get("kind") == "agent":
            continue
        for key in ("reply_ts", "ts"):
            t = turn.get(key)
            if isinstance(t, int | float) and not isinstance(t, bool):
                return float(t)
        return None
    return None


def api_facts(snap: dict) -> dict:
    """The observation of one API session from its structured snapshot. Pure."""
    pending = pending_items(list(snap.get("pending_requests") or ()))
    state = str(snap.get("state") or "")
    return {
        "api": True,
        "readable": True,
        "state": state,
        "busy": state in BUSY_STATES or bool(snap.get("active_turn")),
        "pending": pending,
        "last_turn_at": _turn_time(snap),
        "revision": snap.get("revision"),
    }


async def facts(session_key: str, *, physical_key: str | None = None) -> dict:
    """The observation of one held session. Never raises.

    API: ``{api, readable, state, busy, pending, last_turn_at, revision}`` from the snapshot (an
    unreadable one is ``readable: False`` with nothing pending, never idle). Console:
    ``{api: False, last_output_at}`` from the output clock of ``physical_key`` (default: the key).
    """
    if await asyncio.to_thread(is_api, session_key):
        # Imported here, not at module level: `routes.missions` imports this module, and the
        # structured runtime imports the native stack, which reaches back into `routes`.
        from . import structured_runtime as runtime

        try:
            snap = await runtime.snapshot(session_key)
        except Exception:  # noqa: BLE001 — unreadable is "unobserved", never "idle"
            return {
                "api": True,
                "readable": False,
                "state": "",
                "busy": False,
                "pending": [],
                "last_turn_at": None,
                "revision": None,
            }
        return api_facts(snap)
    return {
        "api": False,
        "last_output_at": scrollback.get_last_visible_output_at(physical_key or session_key),
    }


def now_line(obs: dict, *, now: float, recap_at: float | None, prompt_class: str | None) -> dict:
    """The strip's line for an observation, in `mission_now.STATUSES`. Pure.

    A console session keeps `mission_now.derive` exactly. An API session maps its structured
    state onto the same four words: working → ``producing``; requests waiting → ``at_prompt``
    with ``prompt_class`` "approval"; settled → ``quiet``; unreadable → ``unobserved``. Its line
    also says it is an API session, how many requests wait, and where its pane is.
    """
    if not obs.get("api"):
        return mission_now.derive(
            now=now,
            last_output_at=obs.get("last_output_at"),
            prompt_class=prompt_class,
            recap_at=recap_at,
        )
    last = obs.get("last_turn_at")
    pending = obs.get("pending") or []
    if not obs.get("readable"):
        status, cls = "unobserved", None
    elif pending:
        status, cls = "at_prompt", "approval"
    elif obs.get("busy"):
        status, cls = "producing", None
    else:
        status, cls = "quiet", None
    return {
        "status": status,
        "seconds_since_output": None if last is None else round(max(0.0, now - last)),
        "prompt_class": cls,
        "recap_age_s": None if recap_at is None else round(max(0.0, now - recap_at)),
        "recap_older_than_output": bool(
            recap_at is not None and last is not None and recap_at < last
        ),
        "api": True,
        "pending_count": len(pending),
    }


__all__ = [
    "BUSY_STATES",
    "SHOWN_ITEMS",
    "api_facts",
    "facts",
    "is_api",
    "now_line",
    "pane_path",
    "pending_items",
]
