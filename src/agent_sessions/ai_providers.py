"""Where Settings → AI sends its requests: the provider table, the egress gate, the thinking
levels and model Auto (#1657, P1a #1660).

The one AI endpoint (prefs ``ai_review``) used to be a bare OpenAI-compatible URL + key + model.
It is now a **provider** row from :data:`PROVIDERS`: Custom (today's free URL), the self-hosted
presets, or a hosted vendor whose URL is fixed here. Every row speaks the OpenAI chat-completions
dialect with a Bearer key today; a native Anthropic Messages adapter is its own follow-up, verified
against the live API (#1660; Claude is reachable now through OpenRouter). Everything in this module
is static data and pure functions: no I/O, no HTTP client (``review`` is the one transport, and
the outbound inventory in ``tests/test_prompts_registry.py`` keeps HTTP out of every other module).

**The egress gate** (:func:`egress_violation`). A hosted provider receives session content —
transcripts, terminal tails, drafts, mission and HQ context — so nothing is sent to one, not even
a model-list probe, unless the stored block carries ``egress_ack`` naming exactly that provider and
origin. The ack is server-owned: a patch can only say ``egress_acknowledged: true`` and the server
binds it to the lock-current provider + origin; any provider or origin change clears it. The gate
runs at every boundary — the save lock, ``review._post_chat``, ``review.list_models`` and the
draft endpoint test. P1b extends it to remote Custom / self-hosted origins together with the UI
that can tick the box; until then those rows behave exactly as before (#1660 scope split).

**The trust boundary is this host**: only a literal loopback address or the name ``localhost``
waives consent (:func:`is_loopback`). Nothing is resolved, and no name suffix ever counts.
"""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The provider ids, in picker order.
CUSTOM = "custom"

THINKING_LEVELS = ("auto", "off", "low", "medium", "high")
MODEL_MODES = ("auto", "specific")

# Thinking wire styles. Each maps the five levels to the provider's OWN request field.
_STYLE_CHAT_TEMPLATE = "chat-template"  # self-hosted: today's `enable_thinking: false` opt-out
_STYLE_REASONING_EFFORT = "reasoning-effort"  # OpenAI-style `reasoning_effort`
_STYLE_OPENROUTER = "openrouter"  # `reasoning: {effort | enabled}`
_STYLE_NONE = "none"  # the provider has no thinking setting: nothing is sent


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    #: `custom` | `self-hosted` | `api` (P2 adds `subscription`).
    group: str
    #: The wire dialect. Only `openai-chat` today (the native Anthropic adapter is a follow-up).
    kind: str
    #: The fixed base URL for hosted rows; the preset's suggestion for self-hosted rows; "" for
    #: Custom.
    base_url: str
    #: Whether the operator types the URL (Custom, self-hosted) or the table fixes it (hosted).
    url_editable: bool
    #: How the key travels. Only `bearer` today (`Authorization: Bearer`).
    auth: str
    #: The company the egress warning names ("" for Custom / self-hosted).
    vendor: str
    thinking: str
    #: Auto's model preferences, best first, matched against the provider's own `/models` list
    #: (exact id, then prefix). The first entry is used when no list is available.
    auto_models: tuple[str, ...] = ()
    #: What `off` sends for `reasoning-effort` rows (OpenAI says "minimal", Gemini "none").
    effort_off: str = ""
    key_hint: str = ""

    @property
    def hosted(self) -> bool:
        return self.group == "api"


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        CUSTOM,
        "Custom (OpenAI-compatible)",
        "custom",
        "openai-chat",
        "",
        True,
        "bearer",
        "",
        _STYLE_CHAT_TEMPLATE,
    ),
    Provider(
        "ollama",
        "Ollama",
        "self-hosted",
        "openai-chat",
        "http://localhost:11434/v1",
        True,
        "bearer",
        "",
        _STYLE_CHAT_TEMPLATE,
    ),
    Provider(
        "lmstudio",
        "LM Studio",
        "self-hosted",
        "openai-chat",
        "http://localhost:1234/v1",
        True,
        "bearer",
        "",
        _STYLE_CHAT_TEMPLATE,
    ),
    Provider(
        "llamacpp",
        "llama.cpp",
        "self-hosted",
        "openai-chat",
        "http://localhost:8080/v1",
        True,
        "bearer",
        "",
        _STYLE_CHAT_TEMPLATE,
    ),
    Provider(
        "vllm",
        "vLLM",
        "self-hosted",
        "openai-chat",
        "http://localhost:8000/v1",
        True,
        "bearer",
        "",
        _STYLE_CHAT_TEMPLATE,
    ),
    Provider(
        "openai",
        "OpenAI",
        "api",
        "openai-chat",
        "https://api.openai.com/v1",
        False,
        "bearer",
        "OpenAI",
        _STYLE_REASONING_EFFORT,
        auto_models=("gpt-5-mini", "gpt-5", "gpt-4.1-mini"),
        effort_off="minimal",
        key_hint="sk-…",
    ),
    Provider(
        "google",
        "Google Gemini",
        "api",
        "openai-chat",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        False,
        "bearer",
        "Google",
        _STYLE_REASONING_EFFORT,
        auto_models=("gemini-2.5-flash", "models/gemini-2.5-flash", "gemini-flash"),
        effort_off="none",
        key_hint="AIza…",
    ),
    Provider(
        "openrouter",
        "OpenRouter",
        "api",
        "openai-chat",
        "https://openrouter.ai/api/v1",
        False,
        "bearer",
        "OpenRouter",
        _STYLE_OPENROUTER,
        auto_models=("openrouter/auto",),
        key_hint="sk-or-…",
    ),
    Provider(
        "mistral",
        "Mistral",
        "api",
        "openai-chat",
        "https://api.mistral.ai/v1",
        False,
        "bearer",
        "Mistral AI",
        _STYLE_NONE,
        auto_models=("mistral-medium-latest", "mistral-large-latest", "mistral-small-latest"),
    ),
    Provider(
        "deepseek",
        "DeepSeek",
        "api",
        "openai-chat",
        "https://api.deepseek.com/v1",
        False,
        "bearer",
        "DeepSeek",
        _STYLE_NONE,
        auto_models=("deepseek-chat",),
        key_hint="sk-…",
    ),
    Provider(
        "xai",
        "xAI",
        "api",
        "openai-chat",
        "https://api.x.ai/v1",
        False,
        "bearer",
        "xAI",
        _STYLE_NONE,
        auto_models=("grok-4-fast", "grok-4", "grok-3-mini"),
        key_hint="xai-…",
    ),
)

_BY_ID = {p.id: p for p in PROVIDERS}
PROVIDER_IDS = tuple(_BY_ID)


def get(provider_id: object) -> Provider:
    """The row for an id; an unknown or missing id is Custom — the pre-#1657 behaviour. Callers
    that accept operator input validate the id first (`prefs.validate_ai_review_patch`)."""
    return _BY_ID.get(provider_id if isinstance(provider_id, str) else CUSTOM, _BY_ID[CUSTOM])


def row_for(cfg: dict) -> Provider:
    """The row a resolved endpoint config talks to. A config with no `provider` (the API chat
    agent's own endpoint, a pre-#1657 block) is Custom."""
    return get(cfg.get("provider"))


def thinking_supported(p: Provider) -> bool:
    return p.thinking != _STYLE_NONE


def public_catalog() -> list[dict]:
    """The secret-free table the Settings picker renders (`GET /api/ai-review/providers`)."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "group": p.group,
            "kind": p.kind,
            "base_url": p.base_url,
            "url_editable": p.url_editable,
            "vendor": p.vendor,
            "thinking_supported": thinking_supported(p),
            "auto_model": p.auto_models[0] if p.auto_models else "",
            "key_hint": p.key_hint,
        }
        for p in PROVIDERS
    ]


# --- the egress gate ---------------------------------------------------------------------


def _origin(url: object) -> str | None:
    # Local copy of `prefs.endpoint_origin` (prefs imports this module, not the reverse).
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    defaults = {"http": 80, "https": 443}
    if scheme not in defaults or not host:
        return None
    return f"{scheme}://{host}:{port if port is not None else defaults[scheme]}"


def is_loopback(url: object) -> bool:
    """True only for a literal loopback IP (`127.0.0.0/8`, `::1`) or exactly `localhost`.

    The trust boundary is THIS host. A LAN address, a `.local` / `.internal` name or
    `localhost.example.com` is another machine, and nothing here resolves a name — a suffix or a
    DNS answer must never be what waives consent (#1657 round 2)."""
    if not isinstance(url, str):
        return False
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_ack(provider_id: str, base_url: str, at: str, source: str = "operator") -> dict:
    """The server-owned acknowledgement record, bound to a provider AND an origin."""
    return {"provider": provider_id, "origin": _origin(base_url) or "", "at": at, "source": source}


def ack_matches(ack: object, provider_id: str, base_url: str) -> bool:
    if not isinstance(ack, dict):
        return False
    origin = _origin(base_url)
    return bool(origin) and ack.get("provider") == provider_id and ack.get("origin") == origin


def needs_consent(provider_id: object, base_url: object) -> bool:
    """Whether sending to this provider + URL requires a matching ack. P1a gates the hosted
    rows; Custom and self-hosted keep today's behaviour until P1b (#1660)."""
    p = get(provider_id)
    if not p.hosted:
        return False
    return not is_loopback(base_url)


def egress_violation(cfg: dict) -> str | None:
    """Why nothing may be sent to `cfg`'s endpoint, or None. Pure; evaluated on ONE resolved
    snapshot at every boundary (save lock, transport, model list, draft test)."""
    p = row_for(cfg)
    base = str(cfg.get("base_url") or "")
    if not needs_consent(p.id, base):
        return None
    if ack_matches(cfg.get("egress_ack"), p.id, base):
        return None
    return (
        f"acknowledge first: {p.label} receives the session content AI features send — "
        "nothing is sent to it until you confirm in Settings → AI"
    )


# --- model: Auto -------------------------------------------------------------------------

#: The last model list each hosted endpoint returned, keyed (provider, key fingerprint), so Auto
#: follows a retired model to the provider's current one. Filled by `review.list_models`.
_listed: dict[tuple[str, str], list[str]] = {}


def fingerprint(api_key: object) -> str:
    """A log-safe identity for a key (never the key): which listing Auto may reuse."""
    return hashlib.sha256(str(api_key or "").encode()).hexdigest()[:16]


def is_auto(cfg: dict) -> bool:
    p = row_for(cfg)
    return p.hosted and bool(p.auto_models) and cfg.get("model_mode") == "auto"


def remember_listing(cfg: dict, fingerprint: str, models: list[str]) -> None:
    """Record what the provider listed — or, with an empty list, that listing failed, so Auto
    stops asking before every completion and uses its first preference (a later successful list,
    from Settings or the model dropdown, replaces it)."""
    p = row_for(cfg)
    if p.hosted:
        _listed[(p.id, fingerprint)] = list(models)


def auto_model(cfg: dict, fingerprint: str) -> str:
    """Auto's model: the row's best preference present in the provider's own list (each
    preference by exact id, then by prefix), else the first listed model, else the row's first
    preference."""
    p = row_for(cfg)
    listed = _listed.get((p.id, fingerprint)) or []
    # Preferences in order; each tries its exact id, then its newest prefixed id, before the
    # next preference is considered.
    for want in p.auto_models:
        if want in listed:
            return want
        hit = sorted(m for m in listed if m.startswith(want))
        if hit:
            return hit[-1]
    if listed:
        return listed[0]
    return p.auto_models[0] if p.auto_models else str(cfg.get("model") or "")


def has_listing(cfg: dict, fingerprint: str) -> bool:
    return (row_for(cfg).id, fingerprint) in _listed


# --- thinking ----------------------------------------------------------------------------


def thinking_fields(cfg: dict, *, interactive: bool) -> dict:
    """The request fields for the configured thinking level. An empty dict sends nothing.

    `auto` keeps background passes thinking-OFF (#841: a 22 s review took >305 s with thinking
    on) and gives interactive calls (HQ, API chat) the provider's default. For Custom and the
    self-hosted rows every level that means "off" sends exactly today's opt-out, so a legacy
    block's wire is byte-identical."""
    p = row_for(cfg)
    level = cfg.get("thinking") if cfg.get("thinking") in THINKING_LEVELS else "auto"
    off = level == "off" or (level == "auto" and not interactive)
    if p.thinking == _STYLE_CHAT_TEMPLATE:
        # Today: the opt-out rides on EVERY call, interactive or not.
        return (
            {"chat_template_kwargs": {"enable_thinking": False}} if level in ("auto", "off") else {}
        )
    if p.thinking == _STYLE_REASONING_EFFORT:
        if off:
            return {"reasoning_effort": p.effort_off}
        return {} if level == "auto" else {"reasoning_effort": level}
    if p.thinking == _STYLE_OPENROUTER:
        if off:
            return {"reasoning": {"enabled": False}}
        return {} if level == "auto" else {"reasoning": {"effort": level}}
    return {}


def with_fields(body: dict, fields: dict) -> dict:
    """A COPY of `body` with `fields` merged; `chat_template_kwargs` merges into any existing
    value rather than replacing it (as the pre-#1657 opt-out did)."""
    out = dict(body)
    for k, v in fields.items():
        if k == "chat_template_kwargs" and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def without_fields(body: dict, fields: dict) -> dict:
    """A COPY of `body` with the thinking fields removed again (the ordered degrade, #841)."""
    return {k: v for k, v in body.items() if k not in fields}
