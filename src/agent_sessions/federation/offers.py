"""What a peer may answer of a pending request (#1246 §7, #2059).

**A peer gives one-time answers only.** Which decision kinds settle exactly one request is the
runtime's own classification (``structured_runtime.decision_effect``); this module names
EFFECTS, never kinds. A standing grant (it is written into the owner's agent settings and
widens what the agent may do without asking), a stop of the turn, and any kind the runtime does
not know are the owner's.

:func:`offer` adds to an already projected and redacted snapshot what a ``send`` peer may give
for each pending request: the offered one-time kinds and, for an agent question, its options
BY INDEX. A choice that carries a payload (a standing grant) is never listed, and a background
task's request lists nothing.

**An offer names the exact request it was made for.** Every request :func:`offer` lists choices
for carries :func:`token`: the grant, the session, the turn, the request id and the payload
digest, hashed. A request id alone names nothing (native request ids repeat from turn to turn
and from connection to connection), so ``decision.answer`` sends the token back and the owner
compares it with the request that is pending NOW.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

from .. import native_questions
from .. import structured_runtime as runtime

#: The effects of a decision that settle exactly ONE request and nothing else
#: (``structured_runtime.DECISION_EFFECTS``): the only ones a peer may give.
PEER_EFFECTS = frozenset({"once", "decline", "answer"})

assert PEER_EFFECTS.isdisjoint(runtime.WIDENING_EFFECTS)  # noqa: S101 — a constant relation
assert PEER_EFFECTS <= set(runtime.DECISION_EFFECTS.values())  # noqa: S101


_TOKEN_DOMAIN = b"agent-sessions/federation/decision-offer/v1\x00"
TOKEN_LEN = 32
TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{32}\Z")


def valid_token(value: object) -> bool:
    """Whether ``value`` has the shape of :func:`token`. A shape, never a proof."""
    return isinstance(value, str) and TOKEN_RE.fullmatch(value) is not None


def token(grant_id: str, session_key: str, request: dict) -> str | None:
    """The name of ONE exact pending request under one grant: a domain-separated SHA-256 over
    the grant id, the session key, the request's turn, its id and its payload digest, as 32
    url-safe characters. ``None`` for a request without a turn or an id (nothing names it).

    **It identifies; it authorizes nothing.** It is not a secret and not a credential: the
    grant and its revision are the authority, checked on every call. Its one use is equality:
    the owner recomputes it from the request that is pending (or from a stored record's turn,
    request and digest) and compares."""
    turn_id, request_id = request.get("turn_id"), request.get("request_id")
    if not isinstance(turn_id, str) or not isinstance(request_id, str):
        return None
    digest = request.get("payload_digest") or ""
    if not isinstance(digest, str):
        return None
    doc = json.dumps([grant_id, session_key, turn_id, request_id, digest], separators=(",", ":"))
    raw = hashlib.sha256(_TOKEN_DOMAIN + doc.encode()).digest()
    return base64.urlsafe_b64encode(raw).decode()[:TOKEN_LEN]


def peer_may(decision: object) -> bool:
    """Whether a peer may give this decision kind: only what the RUNTIME classifies as settling
    one request. A standing grant, a stop, and any kind the runtime does not know are the
    owner's."""
    return runtime.decision_effect(decision) in PEER_EFFECTS


def questions(request: dict) -> list[dict] | None:
    """The questions of a pending agent question, exactly as the runtime validates them, or
    ``None`` when this request is not one that can be answered by picking options."""
    payload = request.get("payload")
    if request.get("kind") != native_questions.ASK_TOOL or not isinstance(payload, dict):
        return None
    return native_questions.claude_questions(payload.get("input"))


def offer(view: dict, snap: dict, redact, grant_id: str, session_key: str) -> None:
    """Add to a PROJECTED snapshot what a ``send`` peer may answer of each pending request, and
    the :func:`token` that names it. In place. The projection maps the runtime's requests one
    to one and in order, which is what pairs a projected request with the one it came from;
    anything else offers nothing. A request nothing names (no turn) offers nothing either."""
    raw, shown = snap.get("pending_requests") or [], view.get("pending_requests") or []
    if len(raw) != len(shown):
        return
    for request, out in zip(raw, shown, strict=True):
        if request.get("background_task") or out.get("request_id") != request.get("request_id"):
            continue
        named = token(grant_id, session_key, request)
        if named is None:
            continue
        choices = [c for c in request.get("choices") or [] if isinstance(c, str) and peer_may(c)]
        asked = questions(request)
        if asked is None:
            choices = [c for c in choices if runtime.decision_effect(c) != "answer"]
        else:
            out["questions"] = [
                {
                    "question": redact(q["question"]),
                    "multi": bool(q.get("multiSelect", False)),
                    "options": [redact(o["label"]) for o in q["options"]],
                }
                for q in asked
            ]
        out["choices"] = choices
        out["offer"] = named
