"""How many tokens HQ is using (#1531, the NIGHTJAR telemetry footer).

Two numbers, recorded from the provider's own ``usage`` on every HQ model call:

- ``ctx_tokens``: the prompt size of HQ's most recent call — "how big HQ's context is right now";
- ``today_tokens``: the total (prompt + completion) of every HQ call since local midnight.

**Numbers only.** No prompt, answer, model name or session reaches this file; it holds two
integers, the day they belong to, whether either was estimated, and when. It is fed from ONE place,
``review._post_chat`` (the only transport, docs/invariants/prompt-registry.md), from the response
that call already received, so it adds no outbound request.

When a provider omits ``usage``, the count is estimated at four characters per token and marked
``estimated`` (the footer shows ``≈``). A broken or unreadable file never breaks a model call:
recording is best-effort and the reader starts from zero.

Stored at ``AGENT_SESSIONS_HQ_USAGE`` (default ``~/.config/agent-sessions/hq-usage.json``).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any

from .atomicjson import atomic_write_json, json_write_lock

_LOCK = threading.Lock()
#: Characters per token for the estimate when a provider reports no usage.
CHARS_PER_TOKEN = 4


#: Set by HQ's own pipeline around its model calls (#1531). The transport counts a call as HQ's
#: from THIS, never from the prompt text it carries: two registry prompts may hold identical
#: text, and a text match would count another feature's call as HQ (Hermes on #1560). Async
#: context carries it from the HQ code down to ``review._post_chat`` (and into ``to_thread``).
_HQ_CALL: ContextVar[bool] = ContextVar("hq_call", default=False)


@contextlib.contextmanager
def hq_call() -> Iterator[None]:
    """Mark the model calls made inside as HQ's, for the footer's token count."""
    token = _HQ_CALL.set(True)
    try:
        yield
    finally:
        _HQ_CALL.reset(token)


def in_hq_call() -> bool:
    return _HQ_CALL.get()


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_HQ_USAGE",
            str(Path.home() / ".config" / "agent-sessions" / "hq-usage.json"),
        )
    )


def _today() -> str:
    return date.today().isoformat()


def _empty() -> dict[str, Any]:
    return {
        "day": _today(),
        "today_tokens": 0,
        "today_estimated": False,
        "ctx_tokens": None,
        "ctx_estimated": False,
        "at": None,
    }


def _read() -> dict[str, Any]:
    """The stored counter, or an empty one. A missing, unreadable or malformed file is zero —
    a telemetry readout must never fail the page it sits on."""
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(doc, dict):
        return _empty()
    out = _empty()
    if isinstance(doc.get("day"), str):
        out["day"] = doc["day"]
    for k in ("today_tokens", "ctx_tokens"):
        v = doc.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[k] = v
    for k in ("today_estimated", "ctx_estimated"):
        out[k] = doc.get(k) is True
    if isinstance(doc.get("at"), int | float) and not isinstance(doc.get("at"), bool):
        out["at"] = float(doc["at"])
    return out


def snapshot() -> dict[str, Any]:
    """What the footer shows. A counter from an earlier day reads as zero today (the context
    size of the last call is kept: it is still the size HQ last worked with)."""
    doc = _read()
    if doc["day"] != _today():
        doc["today_tokens"] = 0
        doc["today_estimated"] = False
        doc["day"] = _today()
    return {
        "ctx_tokens": doc["ctx_tokens"],
        "ctx_estimated": doc["ctx_estimated"],
        "today_tokens": doc["today_tokens"],
        "today_estimated": doc["today_estimated"],
        "at": doc["at"],
    }


def _text_chars(content: object) -> int:
    """Characters of a message ``content``: a string, or a list of content parts."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part["text"])
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return 0


def _tool_call_chars(calls: object) -> int:
    """Characters of ``tool_calls``: each function's name and its (JSON string) arguments."""
    if not isinstance(calls, list):
        return 0
    n = 0
    for call in calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            for k in ("name", "arguments"):
                v = fn.get(k)
                n += len(v) if isinstance(v, str) else len(json.dumps(v)) if v is not None else 0
    return n


def _request_chars(messages: list[dict], tools: object) -> int:
    """What the prompt carries: every message's text, the tool calls an assistant turn made, and
    the tool DEFINITIONS sent with the request — HQ's orchestrator sends schemas on every round,
    and they cost prompt tokens like any text (Hermes on #1560)."""
    n = 0
    for m in messages:
        if isinstance(m, dict):
            n += _text_chars(m.get("content")) + _tool_call_chars(m.get("tool_calls"))
    if tools:
        n += len(json.dumps(tools, separators=(",", ":")))
    return n


def _reply_chars(response_json: object) -> int:
    """What the answer carries: its text and any tool calls (a tool-only reply has no text)."""
    n = 0
    if isinstance(response_json, dict):
        for ch in response_json.get("choices") or []:
            msg = ch.get("message") if isinstance(ch, dict) else None
            if isinstance(msg, dict):
                n += _text_chars(msg.get("content")) + _tool_call_chars(msg.get("tool_calls"))
    return n


def _tokens(chars: int) -> int:
    return -(-chars // CHARS_PER_TOKEN)


def _count(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def usage_from(
    response_json: object, messages: list[dict], tools: object = None
) -> tuple[int, int, bool]:
    """(prompt_tokens, total_tokens, estimated) for one chat-completions response.

    Exact only when the provider says so in full. A provider that reports the prompt but
    neither the total nor the completion has its answer ESTIMATED, and the count is marked
    estimated — never a prompt-only total presented as exact (Hermes on #1560). With no usage
    at all, both sides are estimated from what was sent and received, tool traffic included."""
    usage = response_json.get("usage") if isinstance(response_json, dict) else None
    if isinstance(usage, dict):
        p = _count(usage.get("prompt_tokens"))
        if p is not None:
            t, c = _count(usage.get("total_tokens")), _count(usage.get("completion_tokens"))
            if t is not None and t >= p:
                return p, t, False
            if c is not None:
                return p, p + c, False
            return p, p + _tokens(_reply_chars(response_json)), True
    p = _tokens(_request_chars(messages, tools))
    return p, p + _tokens(_reply_chars(response_json)), True


def record(prompt_tokens: int, total_tokens: int, estimated: bool) -> None:
    """Add one HQ call. Best-effort: a write failure is swallowed (a model call never fails
    because its telemetry could not be stored).

    The whole read → add → atomic write runs under the CROSS-PROCESS sidecar lock
    (``json_write_lock``), not only the in-process one: two server processes adding 19 tokens
    each to an empty counter must leave 38, never 19 (Hermes on #1560)."""
    try:
        path = _path()
        with _LOCK, json_write_lock(path):
            doc = _read()
            if doc["day"] != _today():
                kept = {"ctx_tokens": doc["ctx_tokens"], "ctx_estimated": doc["ctx_estimated"]}
                doc = {**_empty(), **kept}
            doc["today_tokens"] = int(doc["today_tokens"]) + max(0, int(total_tokens))
            doc["today_estimated"] = bool(doc["today_estimated"] or estimated)
            doc["ctx_tokens"] = max(0, int(prompt_tokens))
            doc["ctx_estimated"] = bool(estimated)
            doc["at"] = time.time()
            atomic_write_json(path, doc)
    except OSError:
        pass
