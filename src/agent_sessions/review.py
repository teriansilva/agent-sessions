"""AI session review engine (#356).

One bounded, non-streaming chat completion per review against the user-configured
OpenAI-compatible endpoint (prefs `ai_review` block): the input is the engine's saved
transcript tail (via the engine-agnostic ``transcript`` adapters) plus the live terminal
tail (via the narrow ``scrollback.live_tail_text`` accessor — never the ring globals),
tail-truncated to ``max_input_chars``. The response must be JSON shaped as
``{"summary", "title", "intervention_required", "reason"}``; a server-owned shape guard +
length caps treat the model output strictly as data (no tool calls, no actions).

Fail-soft contract (#356 staleness semantics): ANY failure — endpoint down, timeout, bad
JSON, empty input — raises :class:`ReviewError` and persists NOTHING, so the last good
result (and its ``reviewed_at`` stale age) survives instead of a failure masquerading as a
fresh review. The API key never appears in errors or logs; callers surface ``str(exc)``.

The periodic scheduler lives in ``ai_review_loop`` (#356 Phase 2); this module stays
deliberately scheduler-free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

from . import metadata, prefs, scrollback, transcript

# Output field caps — server-owned, applied AFTER parsing so an over-long model reply is
# truncated rather than rejected (the shape is the contract; the length is hygiene).
SUMMARY_MAX = 200
TITLE_MAX = 120
REASON_MAX = 280

# How much live terminal tail to feed the model (chars, post-ANSI-strip). Bounded
# separately from max_input_chars so a chatty terminal can't crowd out the transcript.
LIVE_TAIL_CHARS = 4000


# Hard request timeout for the review completion call. Sized for SLOW LOCAL MODELS
# (#391): ~13 tok/s generation plus prompt processing on multi-thousand-token
# transcripts means real reviews take 40-90s — 30s aborted every one while the
# gateway logged no error. Env-tunable; the floor keeps a typo from zeroing it.
def _timeout_env(name: str, default: float) -> float:
    try:
        return max(10.0, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def request_timeout(cfg: dict | None = None) -> float:
    """Per-call timeout for the review completion request (#391 follow-up). Resolution:
    the Settings value (prefs ``ai_review.request_timeout``) wins when set, else the
    ``AGENT_SESSIONS_AI_REVIEW_TIMEOUT`` env var, else 120s — floored at 10s everywhere.
    Resolved per call (prefs are read per call by design), so a Settings change applies
    to the next review without a restart."""
    block = cfg if cfg is not None else prefs.get_ai_review()
    pref = block.get("request_timeout")
    if isinstance(pref, int | float) and not isinstance(pref, bool):
        return max(10.0, float(pref))
    return _timeout_env("AGENT_SESSIONS_AI_REVIEW_TIMEOUT", 120.0)


MODELS_TIMEOUT_S = 10.0

# /models proxy cache (#356): tiny TTL so the Settings refresh button stays honest while
# repeated dropdown opens don't hammer the endpoint.
MODELS_CACHE_TTL_S = 60.0
_models_cache: dict[str, tuple[float, list[str]]] = {}

# Test seam: when set, every client this module builds routes through this transport
# (httpx.MockTransport in tests — CI never touches the network).
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class ReviewError(Exception):
    """Fail-soft review failure. The message is operator-safe: it never embeds the API
    key. Review (chat-completion) failures never embed raw endpoint response bodies;
    the /models validation probe is the one exception — it carries a BOUNDED,
    key-redacted extract of the gateway's error text (#382) so Settings can show why
    save-time validation failed."""


class NotConfiguredError(ReviewError):
    """The ai_review endpoint is not configured (missing base URL / API key)."""


def _client(timeout: float) -> httpx.AsyncClient:
    # trust_env=False (Hermes on PR #367): httpx defaults to honoring ambient
    # HTTP_PROXY/HTTPS_PROXY/ALL_PROXY env vars, which would silently route the
    # operator's Bearer API key through whatever proxy the host environment has
    # configured. The AI endpoint is operator-provided and explicit — never proxy it.
    return httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT, trust_env=False)


def _require_config() -> dict:
    cfg = prefs.get_ai_review()
    if not str(cfg["base_url"]).strip() or not cfg["api_key"]:
        raise NotConfiguredError("AI review endpoint is not configured")
    return cfg


def _headers(cfg: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg['api_key']}"}


def _base(cfg: dict) -> str:
    return str(cfg["base_url"]).strip().rstrip("/")


# --- input assembly ------------------------------------------------------------------


def _plain_transcript(key: str) -> str:
    """The engine's saved conversation rendered as plain text (no ANSI). Fail-soft: any
    adapter/parse error → "" (live-tail-only review)."""
    try:
        from . import engines

        prov, native = engines.parse_key(key)
    except Exception:
        return ""
    adapter = transcript.adapter_for(prov.engine_id)
    if adapter is None:
        return ""
    try:
        turns = adapter(native, Path.home())
    except Exception:
        return ""
    lines: list[str] = []
    for t in turns:
        text = (t.text or "").strip()
        if not text:
            continue
        label = t.role if t.kind == "text" else f"{t.role}/{t.kind}"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def gather_input(key: str, max_input_chars: int) -> tuple[str, str]:
    """Build ``(review_input, fingerprint)`` for a session: transcript tail + live tail,
    tail-truncated to ``max_input_chars``. The live tail goes through the bounded
    ANSI-stripped accessor; the transcript through the engine adapters. Raises
    :class:`ReviewError` when there is nothing at all to review (no transcript adapter
    output AND no observed PTY output).

    The fingerprint is a hash of the assembled input — it changes exactly when the
    reviewable content changes, which is the property the Phase-2 scheduler's
    change-detection needs (metadata-only writes / timestamp quirks don't move it).
    """
    try:
        from . import engines

        phys_key = engines.physical_key(key)
    except Exception:
        phys_key = key
    transcript_text = _plain_transcript(key)
    live_text = scrollback.live_tail_text(phys_key, LIVE_TAIL_CHARS)
    if not transcript_text and not live_text:
        raise ReviewError("nothing to review: no transcript and no live terminal output")
    parts: list[str] = []
    if transcript_text:
        parts.append("## Transcript (tail)\n" + transcript_text)
    if live_text:
        parts.append("## Live terminal (tail)\n" + live_text)
    text = "\n\n".join(parts)
    if len(text) > max_input_chars:
        text = text[-max_input_chars:]
    fingerprint = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return text, fingerprint


# --- response parsing ----------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def _extract_json(content: str) -> dict:
    """Tolerant JSON extraction: plain JSON, fenced JSON, or the first JSON object
    embedded in prose. Raises ReviewError when no object can be decoded."""
    s = _FENCE_RE.sub("", content or "").strip()
    for candidate in (s,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    start = s.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(s[start:])
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    raise ReviewError("review response was not valid JSON")


def _shape_guard(obj: dict) -> dict:
    """Server-owned shape guard + length caps: the model's output is DATA, nothing more.
    Missing/garbage fields fail the review (drop, keep the last good result) rather than
    persisting junk."""
    summary = obj.get("summary")
    title = obj.get("title")
    required = obj.get("intervention_required")
    reason = obj.get("reason", "")
    if not isinstance(summary, str) or not summary.strip():
        raise ReviewError("review response missing a usable summary")
    if not isinstance(title, str):
        title = ""
    if not isinstance(required, bool):
        raise ReviewError("review response missing intervention_required")
    if not isinstance(reason, str):
        reason = ""
    summary = " ".join(summary.split())[:SUMMARY_MAX]
    title = " ".join(title.split())[:TITLE_MAX]
    reason = " ".join(reason.split())[:REASON_MAX]
    return {
        "summary": summary,
        "title": title,
        # A clean review CLEARS the badge and its reason (#356): reason only survives
        # alongside required=True.
        "intervention_required": required,
        "reason": reason if required else "",
    }


# --- the review ----------------------------------------------------------------------


async def run_review(key: str) -> dict:
    """Review one session now: assemble input, call the endpoint, guard the shape,
    persist via ``metadata.patch``. Returns the persisted fields (plus the fingerprint).
    Raises NotConfiguredError / ReviewError — never partial writes."""
    cfg = _require_config()
    text, fingerprint = await asyncio.to_thread(gather_input, key, int(cfg["max_input_chars"]))
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg["prompt"]},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "stream": False,
    }
    try:
        async with _client(request_timeout(cfg)) as client:
            r = await client.post(
                _base(cfg) + "/chat/completions", json=body, headers=_headers(cfg)
            )
    except httpx.HTTPError as e:
        # Never echo the exception repr — httpx errors can embed request headers.
        raise ReviewError(f"review endpoint unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ReviewError(f"review endpoint returned HTTP {r.status_code}")
    try:
        payload = r.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ReviewError("review endpoint returned an unexpected response shape") from None
    result = _shape_guard(_extract_json(str(content)))
    # Write against the RESOLVED sidecar key (Hermes on PR #367): for a reconciled
    # opencode session the existing title/sticky/archive sidecar lives under the
    # placeholder physical key — patching the logical key would create a sparse
    # shadowing entry and hide that state from the list read path.
    meta = metadata.patch(
        metadata.resolve_key(key),
        ai_summary=result["summary"],
        ai_title=result["title"],
        intervention_required=result["intervention_required"],
        intervention_reason=result["reason"],
        reviewed_at=time.time(),
        review_fingerprint=fingerprint,
    )
    return {
        "ai_summary": meta.ai_summary,
        "ai_title": meta.ai_title,
        "intervention_required": meta.intervention_required,
        "intervention_reason": meta.intervention_reason,
        "reviewed_at": meta.reviewed_at,
        "review_fingerprint": meta.review_fingerprint,
        "review_excluded": meta.review_excluded,
    }


# --- model discovery -----------------------------------------------------------------

# Bound on the gateway-error extract surfaced to Settings (#382): enough for a full
# LiteLLM/OpenAI auth message, short enough to stay a one-liner in the panel.
GATEWAY_ERROR_MAX = 300


def _gateway_error(r: httpx.Response, cfg: dict) -> str:
    """A bounded extract of the gateway's OWN error text for a failed /models probe
    (#382): Settings shows WHY save-time validation failed (e.g. LiteLLM's 401
    "Virtual Key expected…") instead of a bare status code. Tries the OpenAI-style
    JSON shapes (``error.message`` / ``error`` / ``message`` / ``detail``) before
    falling back to a plain-text snippet. The configured API key is redacted
    defensively in case a gateway echoes the Authorization header back."""
    msg = ""
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        elif isinstance(err, str):
            msg = err
        elif isinstance(payload.get("message"), str):
            msg = payload["message"]
        elif isinstance(payload.get("detail"), str):
            msg = payload["detail"]
    if not msg:
        msg = r.text
    key = str(cfg.get("api_key") or "")
    if key:
        msg = msg.replace(key, "[redacted]")
    msg = " ".join(msg.split())[:GATEWAY_ERROR_MAX]
    base = f"model listing returned HTTP {r.status_code}"
    return f"{base}: {msg}" if msg else base


def _cache_key(cfg: dict) -> str:
    # Key fingerprint, never the key itself, so the cache key is log-safe.
    digest = hashlib.sha256(str(cfg["api_key"]).encode()).hexdigest()[:16]
    return f"{_base(cfg)}|{digest}"


async def list_models(*, force: bool = False) -> list[str]:
    """Proxy ``GET {base_url}/models`` with the stored key (#356): the browser never sees
    the key (and would hit CORS anyway). Small in-memory TTL cache; ``force`` (the UI
    refresh button) bypasses it. Raises NotConfiguredError when unset, ReviewError when
    the endpoint can't serve a list (404 / error / timeout) — the caller falls back to
    free-text model entry. This call doubles as the save-time validation probe (#394):
    a non-200 carries a bounded extract of the gateway's own error text (#382) so the
    Settings panel can show WHY the endpoint/key were rejected."""
    cfg = _require_config()
    ck = _cache_key(cfg)
    now = time.monotonic()
    if not force:
        hit = _models_cache.get(ck)
        if hit and now - hit[0] < MODELS_CACHE_TTL_S:
            return hit[1]
    try:
        async with _client(MODELS_TIMEOUT_S) as client:
            r = await client.get(_base(cfg) + "/models", headers=_headers(cfg))
    except httpx.HTTPError as e:
        raise ReviewError(f"model listing unreachable ({type(e).__name__})") from None
    if r.status_code != 200:
        raise ReviewError(_gateway_error(r, cfg))
    try:
        payload = r.json()
    except ValueError:
        raise ReviewError("model listing returned invalid JSON") from None
    raw = payload.get("data") if isinstance(payload, dict) else None
    if raw is None and isinstance(payload, dict):
        raw = payload.get("models")
    if not isinstance(raw, list):
        raise ReviewError("model listing returned an unexpected shape")
    models: list[str] = []
    for item in raw:
        if isinstance(item, str):
            models.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append(item["id"])
    models = sorted(set(models))
    _models_cache[ck] = (now, models)
    return models
