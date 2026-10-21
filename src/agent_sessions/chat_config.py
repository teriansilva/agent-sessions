"""Endpoint configuration for `chat`-runtime agents (#853 P9a, #1209).

A `chat` agent's manifest names only a wire format (`endpoint.kind`); WHERE it talks — the base
URL, the API key and the model — is the operator's configuration, stored here as
``prefs.chat_agents.<engine_id>`` for legacy agents, or a generation-scoped identity for a
managed installation. Activation selects that scope with its provider; staging never redirects
an active agent. Ordinary engine-ID edits refuse managed scopes; the manager alone edits
never-activated candidates under its worker fence. Nothing a manifest says can point BattleLab
at a server.

**The key is encrypted at rest** with the template-secrets AES-GCM mechanism under its own name
(``chat-agent:<id>``, the AAD — an envelope copied onto another agent does not decrypt there). It is
never returned by any view here except :func:`snapshot`, which exists to hand it to the one
transport (`review._post_chat`) and nothing else.

**The key only goes to the origin it was saved for (#956).** `prefs.key_origin_violation` reads a
plaintext ``api_key``; the stored block holds an envelope instead, so the check runs through an
explicit adapter (:func:`_policy_view`) — never by passing the envelope block to the policy, which
would find no key and wave every URL change through. It runs inside `prefs._mutate`'s lock against
the lock-current block, like `set_ai_review`.

**The API agent may instead use the operator's Settings → AI endpoint (#1305).** With
``source = "ai-settings"`` its block holds NO URL and NO key; :func:`resolve` reads
`prefs.get_ai_review()` once per call, so presence, the public view, the endpoint test, every send
and the edit-approval binding all answer from the same resolution. Nothing is copied: the key only
reaches the origin `set_ai_review` bound it to, and rotating it there rotates it here.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import prefs, prompts, template_secrets

BLOCK = "chat_agents"
PROMPT_ID = "chat_agent"
#: The system prompt when the agent has read tools (#1222) — a second REGISTERED prompt, never a
#: suffix assembled at the call site.
TOOLS_PROMPT_ID = "chat_agent_tools"
#: What the agent may do beyond talking (#1222). `none` is the default and the P9a behaviour;
#: `read` lets it list and read files in the conversation's folder. A permission, not a flag:
#: turning it on sends file contents to the configured endpoint. `write` also allows proposals;
#: the separate operator decision is required for every save.
TOOLS = ("none", "read", "write")
#: Where an agent's endpoint comes from (#1305). `own` is the per-agent endpoint above (#1209);
#: `ai-settings` references Settings → AI and stores no URL or key.
SOURCES = ("own", "ai-settings")
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
    {
        "base_url",
        "api_key",
        "model",
        "context_window",
        "max_output_tokens",
        "request_timeout",
        "tools",
        "source",
    }
)


class ChatConfigError(ValueError):
    """A patch that does not validate, or that the origin policy refuses (→ 422)."""


def _scope(engine_id: str) -> str:
    # Explicit generation scopes are already validated by the manager. Do not acquire the
    # roster lock for them: verification/activation can hold the manager document lock.
    if ":" in engine_id:
        return engine_id
    from .engines import registry

    provider = registry.get(engine_id)
    return getattr(provider, "endpoint_scope", None) or engine_id


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
        "tools": "none",
    }
    if isinstance(raw, dict):
        if raw.get("tools") in TOOLS:
            out["tools"] = raw["tools"]
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
        if raw.get("source") == "ai-settings":
            # Only present when set, so an `own` block (every block before #1305, and every
            # managed generation) coerces byte-identically: its binding and the plugin manager's
            # endpoint fingerprint do not move. A reference never carries a copy, even a stale one
            # in a hand-edited file.
            out.update(source="ai-settings", base_url="", key_envelope=None)
    return out


def may_reference_ai_settings(engine_id: str) -> bool:
    """Whether this agent may use Settings → AI (#1305): its manifest declares
    ``[endpoint] ai_settings``, it is first-party, and it is not a managed generation scope (those
    keep their own endpoint). Asked of the manifest — no engine is named here (#853 P3)."""
    if ":" in engine_id:
        return False
    from .engines import registry
    from .plugins import provenance

    prov = registry.get(engine_id)
    manifest = getattr(prov, "manifest", None)
    endpoint = getattr(manifest, "endpoint", None)
    return bool(
        endpoint is not None
        and endpoint.ai_settings
        and getattr(prov, "trust", None) == provenance.FIRST_PARTY
        and not getattr(prov, "endpoint_scope", None)
    )


def _source(block: dict) -> str:
    return block.get("source", "own")


def _shared(engine_id: str, block: dict, path: Path | None) -> tuple[dict | None, str | None]:
    """ONE read of Settings → AI for an `ai-settings` block: ``({base_url, api_key, model,
    binding}, None)`` or ``(None, reason)``. Configured means URL, key AND model — the agent's
    all-three rule, stricter than Settings → AI's own `configured` (URL + key) — AND a computable
    approval binding, so presence can never promise a turn that `snapshot` would refuse."""
    ai = prefs.get_ai_review(path)
    base, key = str(ai.get("base_url") or "").strip(), str(ai.get("api_key") or "")
    if not base or not key:
        return None, "uses your AI settings, which have no endpoint — configure Settings → AI"
    model = block["model"].strip() or str(ai.get("model") or "").strip()
    if not model:
        return None, "set a model in Settings → AI, or a model override here"
    try:
        digest = template_secrets.keyed_digest(_secret_name(engine_id), key)
    except template_secrets.SecretKeyUnavailable:
        return None, "the template-secrets key file is unusable — repair it to use this agent"
    # The binding a paused edit approval is checked against: the stored reference block plus the
    # RESOLVED endpoint — URL, origin, model, Settings → AI's never-reused endpoint revision and a
    # keyed digest of the key (never the key). Any endpoint change, including a key rotated
    # A → B → A, therefore invalidates a pending approval.
    bound = binding(
        {
            "block": block,
            "base_url": base,
            "origin": prefs.endpoint_origin(base),
            "model": model,
            "revision": ai.get("endpoint_revision") or "",
            "key": digest,
        }
    )
    return {"base_url": base, "api_key": key, "model": model, "binding": bound}, None


def _all(path: Path | None = None) -> dict:
    raw = prefs.read_block(BLOCK, path)
    return raw if isinstance(raw, dict) else {}


def stored(engine_id: str, path: Path | None = None) -> dict:
    """The coerced stored block for one agent (server-side: the envelope, never plaintext)."""
    return _coerce(_all(path).get(_scope(engine_id)))


def binding(block: dict) -> str:
    """Fingerprint stored configuration (including its encrypted envelope, never plaintext)."""
    return hashlib.sha256(json.dumps(block, sort_keys=True).encode()).hexdigest()


def _policy_view(block: dict) -> dict:
    """What `prefs.key_origin_violation` needs to see: the URL, and WHETHER a key is stored."""
    return {"base_url": block["base_url"], "api_key": _KEY_PRESENT if block["key_envelope"] else ""}


def _reason(engine_id: str, block: dict, path: Path | None) -> str | None:
    """Why this block cannot start a conversation, or None. The one rule presence, the card,
    the test and every send share."""
    if _source(block) == "ai-settings":
        return _shared(engine_id, block, path)[1]
    if not (block["base_url"].strip() and block["model"].strip() and block["key_envelope"]):
        return "configure this agent's endpoint"
    return None


def is_configured(engine_id: str, path: Path | None = None) -> bool:
    """URL, model and a key all present — the one answer "can this agent start" asks (#1209).
    In `ai-settings` mode they are the RESOLVED ones (#1305)."""
    return _reason(_scope(engine_id), stored(engine_id, path), path) is None


def public(engine_id: str, path: Path | None = None) -> dict:
    """The view any route may return: never the key, never the envelope."""
    b = stored(engine_id, path)
    reason = _reason(_scope(engine_id), b, path)
    out = {
        "base_url": b["base_url"],
        "model": b["model"],
        "api_key_set": bool(b["key_envelope"]),
        "context_window": b["context_window"],
        "max_output_tokens": b["max_output_tokens"],
        "request_timeout": b["request_timeout"],
        "configured": reason is None,
        "tools": b["tools"],
    }
    if may_reference_ai_settings(_scope(engine_id)):
        # Only the API agent offers the choice (#1305); the resolved endpoint is shown as an
        # origin + model, never a key. `ai_settings_ready` lets the card disable the option.
        shared, _why = _shared(_scope(engine_id), b, path)
        ai = prefs.get_ai_review(path)
        out["source"] = _source(b)
        out["reason"] = reason
        out["ai_settings_ready"] = bool(str(ai.get("base_url") or "").strip() and ai.get("api_key"))
        out["resolved"] = (
            {"origin": prefs.endpoint_origin(shared["base_url"]), "model": shared["model"]}
            if shared is not None and _source(b) == "ai-settings"
            else None
        )
    return out


def prompt_id(block: dict) -> str:
    """The registered system prompt for this configuration (#1222)."""
    if block.get("tools") == "write":
        return "chat_agent_edits"
    return TOOLS_PROMPT_ID if block.get("tools") == "read" else PROMPT_ID


def _permission_enabled(engine_id: str, allowed: tuple[str, ...], path: Path | None) -> bool:
    from .engines import registry

    captured = registry.get(engine_id)
    if getattr(captured, "endpoint_scope", None):
        # An old turn keeps its endpoint, but cannot keep a tool grant withdrawn by activation.
        live = registry.live_provider(engine_id)
        if live is None or stored(live.endpoint_scope or engine_id, path)["tools"] not in allowed:
            return False
    return stored(engine_id, path)["tools"] in allowed


def tools_enabled(engine_id: str, path: Path | None = None) -> bool:
    """Read tools on, answered from the LIVE prefs (#1222). The turn loop asks this before every
    round and every call — never from the snapshot the turn started with, so turning tools off
    mid-turn stops the next call rather than the next turn."""
    return _permission_enabled(engine_id, ("read", "write"), path)


def edits_enabled(engine_id: str) -> bool:
    """Proposals permitted now; every actual save additionally needs an operator decision."""
    return _permission_enabled(engine_id, ("write",), None)


def tool_specs(block: dict) -> list[dict]:
    """The declarations selected by this permission, shared by budgeting and the wire body."""
    from . import chat_tools

    if block.get("tools") == "write":
        from . import chat_edits

        return [*chat_tools.SPECS, chat_edits.SPEC]
    return list(chat_tools.SPECS) if block.get("tools") == "read" else []


def budget_tokens(block: dict, *, system_prompt: str) -> int:
    """The history budget in ESTIMATED tokens: the context window minus the output reserve, the
    effective system prompt, every offered tool declaration and a margin. The configuration
    check and each send use this same calculation (the prompt/permission can change)."""
    prompt_tokens = -(-len(system_prompt) // CHARS_PER_TOKEN)
    declarations = tool_specs(block)
    tools_tokens = -(-len(json.dumps(declarations)) // CHARS_PER_TOKEN) if declarations else 0
    return (
        int(block["context_window"])
        - int(block["max_output_tokens"])
        - prompt_tokens
        - tools_tokens
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
    if "source" in patch:
        if patch["source"] not in SOURCES:
            raise ChatConfigError(f"source must be one of {list(SOURCES)}")
        out["source"] = patch["source"]
    if "tools" in patch:
        if patch["tools"] not in TOOLS:
            raise ChatConfigError(f"tools must be one of {list(TOOLS)}")
        out["tools"] = patch["tools"]
    return out


def set_config(engine_id: str, patch: dict, path: Path | None = None) -> dict:
    """Validate and merge ``patch`` under the prefs lock. Returns the PUBLIC view.

    The origin policy runs against the LOCK-CURRENT block through `_policy_view`; a violation
    aborts before anything is written. The budget is validated against the effective system prompt
    so a config that leaves no room for a conversation is refused with the reason. A managed
    engine ID is read-only here; only the manager supplies an explicit mutable candidate scope."""
    scope = _scope(engine_id)
    if scope != engine_id:
        raise ChatConfigError(
            "this installation is managed; prepare a new candidate to change its endpoint"
        )
    engine_id = scope
    clean = validate_patch(patch)

    def merge(raw: object) -> dict:
        agents = dict(raw) if isinstance(raw, dict) else {}
        cur = _coerce(agents.get(engine_id))
        source = clean.get("source", _source(cur))
        new_key = "api_key" in clean and prefs.is_new_api_key(clean["api_key"])
        if source == "ai-settings":
            if not may_reference_ai_settings(engine_id):
                raise ChatConfigError("only the API agent can use your AI settings")
            if clean.get("base_url") or new_key:
                raise ChatConfigError(
                    "an agent that uses your AI settings stores no URL or key of its own"
                )
        elif _source(cur) == "ai-settings" and not (clean.get("base_url") and new_key):
            # Switching back restores nothing: the URL and key were deleted on the way in.
            raise ChatConfigError("enter a base URL and an API key to use an own endpoint")
        else:
            why = prefs.key_origin_violation(_policy_view(cur), clean)
            if why is not None:
                raise prefs.KeyOriginError(why)
        new = dict(cur)
        new.pop("source", None)
        for k in (
            "base_url",
            "model",
            "context_window",
            "max_output_tokens",
            "request_timeout",
            "tools",
        ):
            if k in clean:
                new[k] = clean[k]
        if "api_key" in clean:
            v = clean["api_key"]
            if v is None:
                new["key_envelope"] = None  # explicit clear
            elif prefs.is_new_api_key(v):
                new["key_envelope"] = template_secrets.encrypt(_secret_name(engine_id), v.strip())
        if source == "ai-settings":
            # Atomic with the switch, under this lock: the own URL and key envelope are DELETED,
            # whatever the patch carried, so a block never holds both a reference and a copy.
            new.update(source="ai-settings", base_url="", key_envelope=None)
            if _source(cur) != "ai-settings" and "model" not in clean:
                # The own model is not carried over as a silent override: "use my AI settings"
                # means its model too, unless this same patch names an override.
                new["model"] = ""
        system_prompt = prompts.effective(prompt_id(new))
        if budget_tokens(new, system_prompt=system_prompt) < BUDGET_MIN_TOKENS:
            raise ChatConfigError(
                "context_window leaves no room for a conversation after the output reserve and "
                "the system prompt and tools — raise it or lower max_output_tokens"
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
    engine_id = _scope(engine_id)
    b = stored(engine_id, path)
    if _source(b) == "ai-settings":
        shared, _why = _shared(engine_id, b, path)
        if shared is None:
            return None
        base_url, key, model = shared["base_url"], shared["api_key"], shared["model"]
        bound = shared["binding"]
    else:
        if not (b["base_url"].strip() and b["model"].strip() and b["key_envelope"]):
            return None
        key = template_secrets.decrypt(_secret_name(engine_id), b["key_envelope"])
        if not key:
            return None
        base_url, model, bound = b["base_url"], b["model"], binding(b)
    return {
        "base_url": base_url,
        "api_key": key,
        "binding": bound,
        "model": model,
        "context_window": b["context_window"],
        "max_output_tokens": b["max_output_tokens"],
        "request_timeout": b["request_timeout"],
        "tools": b["tools"],
    }


def resolved_binding(engine_id: str, path: Path | None = None) -> str:
    """The binding a paused turn or edit approval is checked against NOW (#1260, #1305). For an
    `own` block it is exactly `binding(stored)`; for `ai-settings` it covers the resolved endpoint.
    An unresolvable endpoint yields a value no recorded binding can equal (fail closed)."""
    engine_id = _scope(engine_id)
    b = stored(engine_id, path)
    if _source(b) != "ai-settings":
        return binding(b)
    shared, _why = _shared(engine_id, b, path)
    if shared is not None:
        return shared["binding"]
    return "unresolved:" + os.urandom(16).hex()


def saved_test_draft(engine_id: str, path: Path | None = None) -> tuple[dict | None, str | None]:
    """The connection a Test with NO draft fields uses (#1305): the SAVED source's resolution, so
    after "Use my AI settings" is saved the test exercises exactly what a send would."""
    engine_id = _scope(engine_id)
    b = stored(engine_id, path)
    if _source(b) != "ai-settings":
        return None, "base_url must be an http(s) URL"
    shared, why = _shared(engine_id, b, path)
    if shared is None:
        return None, why
    return {
        "base_url": shared["base_url"],
        "api_key": shared["api_key"],
        "request_timeout": None,
    }, None


def draft_for_test(
    engine_id: str, base_url: str, api_key: object, path: Path | None = None
) -> tuple[dict | None, str | None]:
    """The draft connection an endpoint TEST may use, from ONE snapshot of the stored block:
    ``(cfg, None)`` or ``(None, reason)``. The request's own key wins; otherwise the stored key,
    but only for the origin it was saved for (#956) — a refusal makes no outbound call."""
    engine_id = _scope(engine_id)
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
