"""Endpoint configuration for `chat`-runtime agents (#853 P9a, #1209).

A `chat` agent's manifest names only a wire format (`endpoint.kind`); WHERE it talks — the base
URL, the API key and the model — is the operator's configuration, stored here as
``prefs.chat_agents.<engine_id>``. Nothing a manifest says can point BattleLab at a server.

**The key is encrypted at rest** with the template-secrets AES-GCM mechanism under its own name
(``chat-agent:<id>``, the AAD — an envelope copied onto another agent does not decrypt there). It is
never returned by any view here except :func:`snapshot`, which exists to hand it to the one
transport (`review._post_chat`) and nothing else.

**The key only goes to the origin it was saved for (#956).** `prefs.key_origin_violation` reads a
plaintext ``api_key``; the stored block holds an envelope instead, so the check runs through an
explicit adapter (:func:`_policy_view`) — never by passing the envelope block to the policy, which
would find no key and wave every URL change through. It runs inside `prefs._mutate`'s lock against
the lock-current block, like `set_ai_review`.
"""

from __future__ import annotations

from pathlib import Path

from . import prefs, prompts, template_secrets

BLOCK = "chat_agents"
PROMPT_ID = "chat_agent"
#: A non-empty stand-in for "a key is stored" — the origin policy only asks whether one exists.
_KEY_PRESENT = "<stored>"

MODEL_MAX = 200
CONTEXT_MIN, CONTEXT_MAX, CONTEXT_DEFAULT = 2_048, 10_000_000, 32_768
OUTPUT_MIN, OUTPUT_MAX, OUTPUT_DEFAULT = 64, 1_000_000, 4_096
TIMEOUT_MIN, TIMEOUT_MAX = 10, 600
#: Chars per token for the history budget. An ESTIMATE: it undercounts non-English text and code.
CHARS_PER_TOKEN = 4
BUDGET_MARGIN_TOKENS = 256
#: The smallest history budget a config may leave. Below it the agent could not hold a turn.
BUDGET_MIN_TOKENS = 1_024

_FIELDS = frozenset(
    {"base_url", "api_key", "model", "context_window", "max_output_tokens", "request_timeout"}
)


class ChatConfigError(ValueError):
    """A patch that does not validate, or that the origin policy refuses (→ 422)."""


def _secret_name(engine_id: str) -> str:
    return f"chat-agent:{engine_id}"


def _coerce(raw: object) -> dict:
    out = {
        "base_url": "",
        "model": "",
        "key_envelope": None,
        "context_window": CONTEXT_DEFAULT,
        "max_output_tokens": OUTPUT_DEFAULT,
        "request_timeout": None,
    }
    if isinstance(raw, dict):
        for k in ("base_url", "model"):
            if isinstance(raw.get(k), str):
                out[k] = raw[k]
        if template_secrets.valid_envelope(raw.get("key_envelope")):
            out["key_envelope"] = raw["key_envelope"]
        for k, lo, hi in (
            ("context_window", CONTEXT_MIN, CONTEXT_MAX),
            ("max_output_tokens", OUTPUT_MIN, OUTPUT_MAX),
            ("request_timeout", TIMEOUT_MIN, TIMEOUT_MAX),
        ):
            v = raw.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
    return out


def _all(path: Path | None = None) -> dict:
    raw = prefs.read_block(BLOCK, path)
    return raw if isinstance(raw, dict) else {}


def stored(engine_id: str, path: Path | None = None) -> dict:
    """The coerced stored block for one agent (server-side: the envelope, never plaintext)."""
    return _coerce(_all(path).get(engine_id))


def _policy_view(block: dict) -> dict:
    """What `prefs.key_origin_violation` needs to see: the URL, and WHETHER a key is stored."""
    return {"base_url": block["base_url"], "api_key": _KEY_PRESENT if block["key_envelope"] else ""}


def is_configured(engine_id: str, path: Path | None = None) -> bool:
    """URL, model and a key all present — the one answer "can this agent start" asks (#1209)."""
    b = stored(engine_id, path)
    return bool(b["base_url"].strip() and b["model"].strip() and b["key_envelope"])


def public(engine_id: str, path: Path | None = None) -> dict:
    """The view any route may return: never the key, never the envelope."""
    b = stored(engine_id, path)
    return {
        "base_url": b["base_url"],
        "model": b["model"],
        "api_key_set": bool(b["key_envelope"]),
        "context_window": b["context_window"],
        "max_output_tokens": b["max_output_tokens"],
        "request_timeout": b["request_timeout"],
        "configured": is_configured(engine_id, path),
    }


def budget_tokens(block: dict, *, system_prompt: str) -> int:
    """The history budget in ESTIMATED tokens: the context window minus the output reserve, the
    effective system prompt and a margin. Re-evaluated at every send (the prompt can change)."""
    prompt_tokens = -(-len(system_prompt) // CHARS_PER_TOKEN)
    return (
        int(block["context_window"])
        - int(block["max_output_tokens"])
        - prompt_tokens
        - BUDGET_MARGIN_TOKENS
    )


def validate_patch(patch: object) -> dict:
    """Type/bounds validation, no I/O. Returns the cleaned patch; raises `ChatConfigError`."""
    if not isinstance(patch, dict):
        raise ChatConfigError("body must be an object")
    unknown = sorted(set(patch) - _FIELDS)
    if unknown:
        raise ChatConfigError(f"unknown fields: {unknown}")
    out: dict = {}
    if "base_url" in patch:
        v = patch["base_url"]
        if not isinstance(v, str) or (v.strip() and not prefs.is_valid_ai_base_url(v)):
            raise ChatConfigError("base_url must be an http(s) URL")
        out["base_url"] = v.strip()
    if "api_key" in patch:
        v = patch["api_key"]
        if v is not None and not isinstance(v, str):
            raise ChatConfigError("api_key must be a string or null")
        if isinstance(v, str) and len(v) > prefs.AI_REVIEW_KEY_MAX:
            raise ChatConfigError("api_key is too long")
        out["api_key"] = v
    if "model" in patch:
        v = patch["model"]
        if not isinstance(v, str) or len(v) > MODEL_MAX or any(ord(c) < 0x20 for c in v):
            raise ChatConfigError(f"model must be a string of at most {MODEL_MAX} characters")
        out["model"] = v.strip()
    for k, lo, hi in (
        ("context_window", CONTEXT_MIN, CONTEXT_MAX),
        ("max_output_tokens", OUTPUT_MIN, OUTPUT_MAX),
        ("request_timeout", TIMEOUT_MIN, TIMEOUT_MAX),
    ):
        if k in patch:
            v = patch[k]
            if k == "request_timeout" and v is None:
                out[k] = None
                continue
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ChatConfigError(f"{k} must be an integer in [{lo}, {hi}]")
            out[k] = v
    return out


def set_config(engine_id: str, patch: dict, path: Path | None = None) -> dict:
    """Validate and merge ``patch`` under the prefs lock. Returns the PUBLIC view.

    The origin policy runs against the LOCK-CURRENT block through `_policy_view`; a violation
    aborts before anything is written. The budget is validated against the effective system prompt
    so a config that leaves no room for a conversation is refused with the reason."""
    clean = validate_patch(patch)
    system_prompt = prompts.effective(PROMPT_ID)

    def merge(raw: object) -> dict:
        agents = dict(raw) if isinstance(raw, dict) else {}
        cur = _coerce(agents.get(engine_id))
        why = prefs.key_origin_violation(_policy_view(cur), clean)
        if why is not None:
            raise prefs.KeyOriginError(why)
        new = dict(cur)
        for k in ("base_url", "model", "context_window", "max_output_tokens", "request_timeout"):
            if k in clean:
                new[k] = clean[k]
        if "api_key" in clean:
            v = clean["api_key"]
            if v is None:
                new["key_envelope"] = None  # explicit clear
            elif prefs.is_new_api_key(v):
                new["key_envelope"] = template_secrets.encrypt(_secret_name(engine_id), v.strip())
        if budget_tokens(new, system_prompt=system_prompt) < BUDGET_MIN_TOKENS:
            raise ChatConfigError(
                "context_window leaves no room for a conversation after the output reserve and "
                "the system prompt — raise it or lower max_output_tokens"
            )
        agents[engine_id] = new
        return agents

    prefs.mutate_block(BLOCK, merge, path)
    return public(engine_id, path)


def snapshot(engine_id: str, path: Path | None = None) -> dict | None:
    """ONE consistent read of URL + decrypted key + model + limits, for one model request or one
    test. None when not configured or the key cannot be decrypted (it needs re-entry). The only
    function that returns the plaintext key; its caller hands it to `review._post_chat` and
    nothing else."""
    b = stored(engine_id, path)
    if not (b["base_url"].strip() and b["model"].strip() and b["key_envelope"]):
        return None
    key = template_secrets.decrypt(_secret_name(engine_id), b["key_envelope"])
    if not key:
        return None
    return {
        "base_url": b["base_url"],
        "api_key": key,
        "model": b["model"],
        "context_window": b["context_window"],
        "max_output_tokens": b["max_output_tokens"],
        "request_timeout": b["request_timeout"],
    }


def draft_for_test(
    engine_id: str, base_url: str, api_key: object, path: Path | None = None
) -> tuple[dict | None, str | None]:
    """The draft connection an endpoint TEST may use, from ONE snapshot of the stored block:
    ``(cfg, None)`` or ``(None, reason)``. The request's own key wins; otherwise the stored key,
    but only for the origin it was saved for (#956) — a refusal makes no outbound call."""
    b = stored(engine_id, path)
    if prefs.is_new_api_key(api_key):
        key = str(api_key).strip()
    else:
        if not b["key_envelope"]:
            return None, "an API key is required to test an endpoint"
        new_origin = prefs.endpoint_origin(base_url)
        if new_origin is None or new_origin != prefs.endpoint_origin(b["base_url"]):
            return None, (
                f"enter the API key for {new_origin or base_url.strip()} — the stored key is only "
                "sent to the endpoint it was saved for"
            )
        key = template_secrets.decrypt(_secret_name(engine_id), b["key_envelope"])
        if not key:
            return None, "the stored API key cannot be read — enter it again"
    return {"base_url": base_url.strip(), "api_key": key, "request_timeout": None}, None
