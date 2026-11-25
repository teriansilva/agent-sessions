"""#1657 P1a (#1660) — AI endpoint providers: the table, the egress gate, thinking levels and model
Auto. (The native Anthropic Messages adapter is a follow-up, verified against the live API.)

The egress gate is tested at EVERY boundary it stands on — the save lock (`set_ai_review` and
`POST /api/prefs`), the transport (`review._post_chat`, through both `complete_json` and
`post_chat_response`), the model list, the draft endpoint test, and the API chat agent's snapshot
of Settings → AI — and each test asserts the strongest thing it can: ZERO outbound requests.

Everything runs against httpx.MockTransport; CI never touches the network.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_sessions import ai_providers, chat_config, prefs, prompts, review
from agent_sessions.main import create_app

OPENAI_KEY = "sk-openai-test-key-000222"
GEMINI_KEY = "AIza-test-key-000444"
CUSTOM_BASE = "https://ai.example.io/v1"
CUSTOM_KEY = "sk-custom-key-000333"
OPENAI_URL = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(review, "_TRANSPORT", None)
    review._thinking_refused.clear()
    review._models_cache.clear()
    ai_providers._listed.clear()
    yield
    review._thinking_refused.clear()
    review._models_cache.clear()
    ai_providers._listed.clear()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def wire(monkeypatch):
    """Every request the transport sends, and a reply function the test can swap."""
    calls: list[httpx.Request] = []
    state: dict = {"reply": lambda req: httpx.Response(500)}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return state["reply"](request)

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    return calls, state


def _sent(req: httpx.Request) -> dict:
    return json.loads(req.content)


def _openai_text(text: str, *, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    )


def _save_openai(**extra) -> dict:
    return prefs.set_ai_review(
        {
            "provider": "openai",
            "api_key": OPENAI_KEY,
            "model_mode": "specific",
            "model": "gpt-4.1",
            "egress_acknowledged": True,
            **extra,
        }
    )


def _system(pid: str = "tail_review") -> dict:
    return {"role": "system", "content": prompts.effective(pid)}


# --- the trust boundary ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "http://127.9.9.9/v1",
        "http://[::1]:8000/v1",
        "http://localhost/v1",
    ],
)
def test_only_literal_loopback_or_localhost_is_this_host(url):
    assert ai_providers.is_loopback(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://172.20.1.5:8000/v1",  # a LAN box is another machine
        "http://10.0.0.5/v1",
        "http://gpu.local/v1",
        "http://ai.internal/v1",
        "http://localhost.example.com/v1",  # a name suffix never waives consent
        "https://api.openai.com/v1",
        "not a url",
    ],
)
def test_everything_else_is_another_machine(url):
    assert not ai_providers.is_loopback(url)


def test_the_ack_is_bound_to_provider_and_origin():
    ack = ai_providers.make_ack("openai", OPENAI_URL, "t")
    assert ai_providers.ack_matches(ack, "openai", "https://API.openai.com:443/other")
    assert not ai_providers.ack_matches(ack, "google", OPENAI_URL)
    assert not ai_providers.ack_matches(ack, "openai", "https://api.openai.com:8443/v1")


# --- legacy blocks: nothing changes --------------------------------------------------------


def test_a_legacy_block_reads_as_custom_with_its_model():
    _write_raw_ai_review({"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, "model": "m-1"})
    full = prefs.get_ai_review()
    assert (full["provider"], full["model_mode"], full["thinking"]) == (
        "custom",
        "specific",
        "auto",
    )
    assert full["egress_ack"] is None and full["schema"] == 1
    # Grandfathered by the one-time migration (#1671): still allowed, now with a legacy ack.
    prefs.migrate_ai_review_egress()
    pub = prefs.public_ai_review()
    assert pub["egress_required"] is True and pub["egress_allowed"] is True
    assert pub["egress_ack"]["source"] == "legacy"


def test_a_legacy_custom_request_is_byte_identical(wire):
    """The pre-#1657 wire, spelled out: Bearer auth, `/chat/completions`, JSON mode and the
    `enable_thinking: false` opt-out — nothing added, nothing renamed."""
    calls, state = wire
    state["reply"] = lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]}
    )
    _write_raw_ai_review({"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, "model": "m-1"})
    msgs = [_system(), {"role": "user", "content": "hi"}]
    assert asyncio.run(review.complete_json(msgs)) == {"ok": True}
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == CUSTOM_BASE + "/chat/completions"
    assert req.headers["authorization"] == f"Bearer {CUSTOM_KEY}"
    assert "x-api-key" not in req.headers
    assert _sent(req) == {
        "model": "m-1",
        "messages": msgs,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }


# --- the save rule ---------------------------------------------------------------------------


def test_a_hosted_provider_cannot_be_saved_without_the_acknowledgement():
    before = prefs.get_ai_review()
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY})
    assert prefs.get_ai_review() == before


def test_the_acknowledgement_is_written_by_the_server_and_bound():
    full = _save_openai()
    assert full["base_url"] == OPENAI_URL  # the table's URL, not the patch's
    ack = full["egress_ack"]
    assert ack["provider"] == "openai" and ack["origin"] == "https://api.openai.com:443"
    assert ack["source"] == "operator"


def test_a_patch_can_never_write_the_ack_itself():
    forged = {
        "provider": "openai",
        "origin": "https://api.openai.com:443",
        "at": "x",
        "source": "operator",
    }
    assert "unknown ai_review fields" in prefs.validate_ai_review_patch({"egress_ack": forged})
    assert prefs.validate_ai_review_patch({"egress_acknowledged": "yes"})


def test_a_hosted_url_is_fixed():
    with pytest.raises(prefs.ProviderUrlError):
        prefs.set_ai_review(
            {
                "provider": "openai",
                "base_url": "https://evil.example/v1",
                "api_key": OPENAI_KEY,
                "egress_acknowledged": True,
            }
        )


def test_switching_provider_needs_the_new_hosts_key_and_a_new_ack():
    _save_openai()
    # The stored key belongs to api.openai.com: moving to Gemini without its key is refused.
    with pytest.raises(prefs.KeyOriginError):
        prefs.set_ai_review({"provider": "google", "egress_acknowledged": True})
    # With the key but no acknowledgement for Google: the OpenAI ack does not carry over.
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"provider": "google", "api_key": GEMINI_KEY})
    assert prefs.get_ai_review()["provider"] == "openai"
    full = prefs.set_ai_review(
        {"provider": "google", "api_key": GEMINI_KEY, "egress_acknowledged": True}
    )
    assert full["egress_ack"]["provider"] == "google"
    assert full["model_mode"] == "auto"  # a newly picked hosted provider starts on Auto


def test_leaving_a_hosted_provider_must_name_the_new_url():
    _save_openai()
    with pytest.raises(prefs.ProviderUrlError):
        prefs.set_ai_review({"provider": "custom", "api_key": CUSTOM_KEY})
    # Consent never carries to another recipient: the remote Custom host needs its own ack.
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"provider": "custom", "base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY})
    full = prefs.set_ai_review(
        {
            "provider": "custom",
            "base_url": CUSTOM_BASE,
            "api_key": CUSTOM_KEY,
            "egress_acknowledged": True,
        }
    )
    assert full["egress_ack"]["provider"] == "custom"


def test_withdrawing_the_ack_is_refused_while_the_provider_stays():
    _save_openai()
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"egress_acknowledged": False})
    assert prefs.get_ai_review()["egress_ack"] is not None


def test_a_key_rotation_on_the_same_provider_keeps_the_ack():
    _save_openai()
    full = prefs.set_ai_review({"api_key": "sk-openai-rotated-999"})
    assert full["egress_ack"]["provider"] == "openai"


# --- the gate at every boundary: zero outbound calls ----------------------------------------


def _hand_edit_drop_ack() -> None:
    """A stored hosted provider whose acknowledgement is gone (a hand edit, a bug elsewhere):
    the send-time gates must hold on their own, without the save rule."""
    path = prefs._default_path()
    doc = json.loads(path.read_text())
    doc["ai_review"]["egress_ack"] = None
    path.write_text(json.dumps(doc))


def test_complete_json_sends_nothing_without_the_ack(wire):
    calls, _ = wire
    _save_openai()
    _hand_edit_drop_ack()
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.complete_json([_system(), {"role": "user", "content": "x"}]))
    assert calls == []


def test_the_transport_refuses_a_config_resolved_elsewhere(wire):
    """`_post_chat` holds a caller's own snapshot to the gate (the API chat agent's path)."""
    calls, _ = wire
    cfg = {
        "provider": "openai",
        "base_url": OPENAI_URL,
        "api_key": OPENAI_KEY,
        "model": "m",
    }
    body = {"model": "m", "messages": [_system(), {"role": "user", "content": "x"}]}
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.post_chat_response(cfg, body))
    assert calls == []


def test_the_model_list_sends_nothing_without_the_ack(wire):
    calls, _ = wire
    _save_openai()
    _hand_edit_drop_ack()
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.list_models(force=True))
    assert calls == []


def test_a_model_list_for_a_config_resolved_elsewhere_sends_nothing(wire):
    """`list_models(cfg=…)` holds its own gate: Auto's first-call listing and the draft test both
    pass a snapshot, and neither may lean on `_require_config` having checked it."""
    calls, _ = wire
    cfg = {"provider": "openai", "base_url": OPENAI_URL, "api_key": OPENAI_KEY}
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.list_models(force=True, cfg=cfg))
    assert calls == []


def test_the_api_chat_agent_on_ai_settings_carries_the_provider_and_its_gate(wire):
    calls, state = wire
    _save_openai()
    chat_config.set_config("apichat", {"source": "ai-settings"})
    snap = chat_config.snapshot("apichat")
    assert snap["provider"] == "openai" and snap["egress_ack"]["provider"] == "openai"
    state["reply"] = lambda req: _openai_text("hello")
    body = {
        "model": snap["model"],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
    }
    r = asyncio.run(review.post_chat_response(snap, body))
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    assert str(calls[0].url) == OPENAI_URL + "/chat/completions"
    _hand_edit_drop_ack()
    snap = chat_config.snapshot("apichat")
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.post_chat_response(snap, body))
    assert len(calls) == 1


def _client(cfg):
    return TestClient(create_app(cfg), base_url="https://testserver")


def _login(c, cfg) -> dict:
    r = c.post(
        "/login",
        data={"username": "marcus", "password": "hunter2"},
        follow_redirects=False,
        headers={"Origin": cfg.origin},
    )
    assert r.status_code == 303
    return {"X-CSRF-Token": c.get("/api/config").json()["csrf"], "Origin": cfg.origin}


def test_the_draft_test_refuses_before_any_request(auth_cfg, fake_jsonl, wire):
    calls, state = wire
    state["reply"] = lambda req: httpx.Response(200, json={"data": [{"id": "gpt-4.1"}]})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    draft = {"provider": "openai", "api_key": OPENAI_KEY}
    r = c.post("/api/ai-review/endpoint/test", json=draft, headers=hdr)
    assert r.status_code == 409 and "acknowledge" in r.json()["detail"]
    assert calls == []
    r = c.post(
        "/api/ai-review/endpoint/test", json={**draft, "egress_acknowledged": True}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["gpt-4.1"]
    assert str(calls[0].url) == OPENAI_URL + "/models"
    assert calls[0].headers["authorization"] == f"Bearer {OPENAI_KEY}"
    # A test never persists anything, the acknowledgement included.
    assert prefs.get_ai_review()["egress_ack"] is None


def test_a_draft_cannot_move_a_hosted_providers_url(auth_cfg, fake_jsonl, wire):
    calls, _ = wire
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/ai-review/endpoint/test",
        json={
            "provider": "openai",
            "base_url": "https://evil.example/v1",
            "api_key": OPENAI_KEY,
            "egress_acknowledged": True,
        },
        headers=hdr,
    )
    assert r.status_code == 422 and calls == []


def test_the_prefs_route_refuses_and_writes_nothing(auth_cfg, fake_jsonl, wire):
    calls, _ = wire
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    before = prefs._default_path().read_bytes() if prefs._default_path().exists() else b""
    r = c.post(
        "/api/prefs",
        json={"ai_review": {"provider": "openai", "api_key": OPENAI_KEY}, "theme": "light"},
        headers=hdr,
    )
    assert r.status_code == 422 and "acknowledge" in r.json()["detail"]
    after = prefs._default_path().read_bytes() if prefs._default_path().exists() else b""
    assert after == before and calls == []
    r = c.post(
        "/api/prefs",
        json={
            "ai_review": {"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True}
        },
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    pub = r.json()["ai_review"]
    assert pub["provider"] == "openai" and pub["egress_allowed"] is True
    assert "api_key" not in pub


def test_the_catalog_is_static_and_secret_free(auth_cfg, fake_jsonl, wire):
    calls, _ = wire
    _save_openai()
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.get("/api/ai-review/providers", headers=hdr)
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["providers"]]
    assert ids[0] == "custom" and {"openai", "google", "openrouter"} <= set(ids)
    # The native Anthropic adapter is its own follow-up (#1660); Claude goes via OpenRouter today.
    assert "anthropic" not in ids
    assert OPENAI_KEY not in r.text and calls == []


# --- thinking ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "level", "interactive", "expected"),
    [
        ("custom", "auto", False, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("custom", "auto", True, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("ollama", "off", True, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("vllm", "high", False, {}),
        ("openai", "auto", False, {"reasoning_effort": "minimal"}),
        ("openai", "auto", True, {}),
        ("openai", "medium", False, {"reasoning_effort": "medium"}),
        ("google", "off", True, {"reasoning_effort": "none"}),
        ("openrouter", "auto", False, {"reasoning": {"enabled": False}}),
        ("openrouter", "low", True, {"reasoning": {"effort": "low"}}),
        ("mistral", "high", True, {}),
        ("xai", "off", False, {}),
    ],
)
def test_each_provider_spells_thinking_its_own_way(provider, level, interactive, expected):
    cfg = {"provider": provider, "thinking": level}
    assert ai_providers.thinking_fields(cfg, interactive=interactive) == expected


def test_a_refused_thinking_field_is_dropped_and_remembered(wire):
    calls, state = wire

    def reply(req):
        if "reasoning_effort" in _sent(req):
            return httpx.Response(400, json={"error": {"message": "unsupported parameter"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        )

    state["reply"] = reply
    prefs.set_ai_review(
        {
            "provider": "openai",
            "api_key": OPENAI_KEY,
            "model_mode": "specific",
            "model": "gpt-4.1",
            "egress_acknowledged": True,
        }
    )
    msgs = [_system(), {"role": "user", "content": "x"}]
    asyncio.run(review.complete_json(msgs))
    asyncio.run(review.complete_json(msgs))
    assert [("reasoning_effort" in _sent(c)) for c in calls] == [True, False, False]


# --- model Auto -----------------------------------------------------------------------------------


def test_auto_follows_the_providers_own_list():
    cfg = {"provider": "openai", "model_mode": "auto", "api_key": "k"}
    fp = ai_providers.fingerprint("k")
    assert ai_providers.auto_model(cfg, fp) == "gpt-5-mini"  # no list yet: first preference
    ai_providers.remember_listing(cfg, fp, ["gpt-4.1-mini", "gpt-5-2026-09-01", "o3"])
    assert ai_providers.auto_model(cfg, fp) == "gpt-5-2026-09-01"  # prefix match on `gpt-5`
    ai_providers.remember_listing(cfg, fp, ["o3", "o4"])
    assert ai_providers.auto_model(cfg, fp) == "o3"  # no preference listed: the first model


def test_auto_lists_once_then_sends_the_resolved_model(wire):
    calls, state = wire

    def reply(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-5-mini-2026-09-01"}]})
        return _openai_text("{}")

    state["reply"] = reply
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    assert prefs.get_ai_review()["model_mode"] == "auto"
    msgs = [_system(), {"role": "user", "content": "x"}]
    asyncio.run(review.complete_json(msgs))
    asyncio.run(review.complete_json(msgs))
    paths = [c.url.path for c in calls]
    assert paths == ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"]
    assert {_sent(c)["model"] for c in calls[1:]} == {"gpt-5-mini-2026-09-01"}


def test_custom_never_resolves_auto():
    cfg = {"provider": "custom", "model_mode": "auto", "model": "m-1"}
    assert not ai_providers.is_auto(cfg)


def test_naming_a_model_chooses_it_over_auto():
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    assert prefs.get_ai_review()["model_mode"] == "auto"
    # The model-only patch today's Settings page sends.
    full = prefs.set_ai_review({"model": "gpt-4.1"})
    assert (full["model_mode"], full["model"]) == ("specific", "gpt-4.1")
    assert review._require_config()["model"] == "gpt-4.1"
    # Switching provider while naming the model keeps that model too.
    full = prefs.set_ai_review(
        {
            "provider": "google",
            "api_key": GEMINI_KEY,
            "model": "gemini-2.5-pro",
            "egress_acknowledged": True,
        }
    )
    assert (full["model_mode"], full["model"]) == ("specific", "gemini-2.5-pro")


def test_an_auto_agents_binding_survives_the_model_list_arriving():
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    chat_config.set_config("apichat", {"source": "ai-settings"})
    before = chat_config.resolved_binding("apichat")
    ai_providers.remember_listing(
        {"provider": "openai"}, ai_providers.fingerprint(OPENAI_KEY), ["gpt-5-mini-x"]
    )
    assert chat_config.snapshot("apichat")["model"] == "gpt-5-mini-x"
    assert chat_config.resolved_binding("apichat") == before


def test_a_failed_listing_is_asked_once_not_before_every_call(wire):
    calls, state = wire

    def reply(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(404, json={"error": {"message": "no"}})
        return _openai_text("{}")

    state["reply"] = reply
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    msgs = [_system(), {"role": "user", "content": "x"}]
    for _ in range(3):
        asyncio.run(review.complete_json(msgs))
    assert [c.url.path for c in calls] == ["/v1/models"] + ["/v1/chat/completions"] * 3
    assert {_sent(c)["model"] for c in calls[1:]} == {"gpt-5-mini"}


SECRET_VALUE = "s3cr3t-value-77aa"


def test_replayed_tool_call_arguments_are_redacted(monkeypatch):
    from agent_sessions import template_secrets

    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET_VALUE])
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "f", "arguments": f'{{"q": "{SECRET_VALUE}"}}'},
                }
            ],
        }
    ]
    out = template_secrets.redact_messages(msgs)
    assert SECRET_VALUE not in json.dumps(out)


# --- P1b (#1671): every non-loopback origin, and the one-time legacy migration ------------------

LEGACY_BASE = "https://legacy.example.io/v1"
LEGACY_KEY = "sk-legacy-key-000555"


def _write_raw_ai_review(block: dict) -> None:
    """A block exactly as an older version stored it — no migration has run on it."""
    path = prefs._default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(path.read_text()) if path.exists() else {}
    doc["ai_review"] = block
    path.write_text(json.dumps(doc))


def _raw_ai_review() -> dict:
    return json.loads(prefs._default_path().read_text())["ai_review"]


def test_a_remote_custom_url_now_needs_the_acknowledgement():
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, "model": "m"})
    full = prefs.set_ai_review(
        {"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, "model": "m", "egress_acknowledged": True}
    )
    assert full["egress_ack"]["provider"] == "custom"
    assert full["egress_ack"]["origin"] == "https://ai.example.io:443"
    assert prefs.public_ai_review()["egress_required"] is True


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1"])
def test_a_loopback_url_needs_nothing(url):
    full = prefs.set_ai_review({"base_url": url, "api_key": "local", "model": "m"})
    assert full["egress_ack"] is None
    pub = prefs.public_ai_review()
    assert pub["egress_required"] is False and pub["egress_allowed"] is True


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(
            {"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"}, id="no-provider"
        ),
        pytest.param(
            {"provider": "custom", "base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"},
            id="p1a-saved-custom",
        ),
        pytest.param(
            {"provider": "vllm", "base_url": LEGACY_BASE, "model": "m"}, id="self-hosted-no-key"
        ),
        pytest.param({"base_url": LEGACY_BASE, "enabled": False}, id="disabled-no-key"),
    ],
)
def test_the_migration_grandfathers_every_legacy_remote_shape(stored):
    _write_raw_ai_review(stored)
    prefs.migrate_ai_review_egress()
    raw = _raw_ai_review()
    assert raw["schema"] == prefs.AI_REVIEW_SCHEMA
    ack = raw["egress_ack"]
    assert ack["source"] == "legacy" and ack["origin"] == "https://legacy.example.io:443"
    assert ack["provider"] == stored.get("provider", "custom")
    assert raw["base_url"] == LEGACY_BASE  # the recipient never changes


def test_the_migration_writes_no_ack_for_loopback_or_hosted():
    _write_raw_ai_review({"base_url": "http://localhost:11434/v1", "api_key": "x"})
    prefs.migrate_ai_review_egress()
    assert _raw_ai_review()["schema"] == 2 and _raw_ai_review().get("egress_ack") is None
    # A hosted row was already consented to under P1a's rules: never grandfathered.
    _write_raw_ai_review({"provider": "openai", "base_url": OPENAI_URL, "api_key": OPENAI_KEY})
    prefs.migrate_ai_review_egress()
    assert _raw_ai_review().get("egress_ack") is None


def test_the_migration_runs_once():
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY})
    prefs.migrate_ai_review_egress()
    first = _raw_ai_review()
    prefs.migrate_ai_review_egress()
    assert _raw_ai_review() == first


def test_a_save_racing_the_migration_never_pairs_the_legacy_ack_with_another_origin():
    """The save applies the migration to the block it read under the lock, so the legacy ack can
    only ever name the legacy origin — moving to another host still needs a new acknowledgement."""
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    with pytest.raises(prefs.EgressConsentError):
        prefs.set_ai_review({"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY})
    assert _raw_ai_review()["base_url"] == LEGACY_BASE
    # An unrelated save on the legacy block migrates it in the same locked write.
    full = prefs.set_ai_review({"enabled": True})
    assert full["egress_ack"]["origin"] == "https://legacy.example.io:443"
    assert full["egress_ack"]["source"] == "legacy" and full["schema"] == 2


def test_a_concurrent_migration_and_save_always_end_consistent():
    import threading

    for _ in range(20):
        _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY})
        errors: list[BaseException] = []

        def save(errors=errors):
            try:
                prefs.set_ai_review(
                    {"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, "egress_acknowledged": True}
                )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [
            threading.Thread(target=prefs.migrate_ai_review_egress),
            threading.Thread(target=save),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        raw = _raw_ai_review()
        assert raw["base_url"] == CUSTOM_BASE
        assert raw["egress_ack"]["origin"] == "https://ai.example.io:443"
        assert raw["egress_ack"]["source"] == "operator"


def test_the_first_call_after_the_upgrade_goes_to_the_same_endpoint(wire):
    """No startup migration ran: the transport migrates lazily and sends to the stored endpoint."""
    calls, state = wire
    state["reply"] = lambda req: _openai_text('{"ok": true}')
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    assert asyncio.run(review.complete_json([_system(), {"role": "user", "content": "x"}])) == {
        "ok": True
    }
    assert str(calls[0].url) == LEGACY_BASE + "/chat/completions"
    assert _raw_ai_review()["egress_ack"]["source"] == "legacy"


def test_the_model_list_migrates_lazily_too(wire):
    calls, state = wire
    state["reply"] = lambda req: httpx.Response(200, json={"data": [{"id": "m"}]})
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    assert asyncio.run(review.list_models(force=True)) == ["m"]
    assert str(calls[0].url) == LEGACY_BASE + "/models"


def test_an_ai_settings_chat_agent_migrates_lazily_and_keeps_its_recipient(wire):
    calls, state = wire
    state["reply"] = lambda req: _openai_text("hello")
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    chat_config.set_config("apichat", {"source": "ai-settings"})
    snap = chat_config.snapshot("apichat")
    assert snap["schema"] == 1  # resolved before any migration ran
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
    r = asyncio.run(review.post_chat_response(snap, body))
    assert r.json()["choices"][0]["message"]["content"] == "hello"
    assert str(calls[0].url) == LEGACY_BASE + "/chat/completions"
    # A schema-1 snapshot pointing ELSEWHERE is not covered by the legacy ack of the stored one.
    elsewhere = {**snap, "base_url": CUSTOM_BASE}
    with pytest.raises(review.EgressRefusedError):
        asyncio.run(review.post_chat_response(elsewhere, body))
    assert len(calls) == 1


def test_the_draft_test_uses_the_legacy_ack_only_for_its_own_origin(auth_cfg, fake_jsonl, wire):
    calls, state = wire
    state["reply"] = lambda req: httpx.Response(200, json={"data": [{"id": "m"}]})
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post("/api/ai-review/endpoint/test", json={"base_url": LEGACY_BASE}, headers=hdr)
    assert r.status_code == 200, r.text
    r = c.post(
        "/api/ai-review/endpoint/test",
        json={"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY},
        headers=hdr,
    )
    assert r.status_code == 409 and "acknowledge" in r.json()["detail"]
    assert len(calls) == 1


def _unwritable(monkeypatch):
    def refuse(*a, **k):
        raise PermissionError("read-only prefs")

    monkeypatch.setattr(prefs, "atomic_write_json", refuse)


def test_an_unwritable_migration_fails_closed_everywhere_then_heals(wire, monkeypatch):
    calls, state = wire
    state["reply"] = lambda req: _openai_text('{"ok": true}')
    _write_raw_ai_review({"base_url": LEGACY_BASE, "api_key": LEGACY_KEY, "model": "m"})
    chat_config.set_config("apichat", {"source": "ai-settings"})
    snap = chat_config.snapshot("apichat")
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    with monkeypatch.context() as m:
        _unwritable(m)
        for attempt in (
            lambda: review.complete_json([_system(), {"role": "user", "content": "x"}]),
            lambda: review.list_models(force=True),
            lambda: review.post_chat_response(snap, body),
        ):
            with pytest.raises(review.EgressRefusedError, match="save Settings"):
                asyncio.run(attempt())
    assert calls == []  # nothing was sent anywhere
    # Writable again: the next call migrates once and goes to the same recipient.
    asyncio.run(review.complete_json([_system(), {"role": "user", "content": "x"}]))
    assert str(calls[0].url) == LEGACY_BASE + "/chat/completions"
    assert _raw_ai_review()["egress_ack"]["origin"] == "https://legacy.example.io:443"


def test_an_unwritable_stamp_of_a_loopback_block_does_not_block_it(wire, monkeypatch):
    calls, state = wire
    state["reply"] = lambda req: _openai_text('{"ok": true}')
    _write_raw_ai_review({"base_url": "http://localhost:11434/v1", "api_key": "x", "model": "m"})
    _unwritable(monkeypatch)
    asyncio.run(review.complete_json([_system(), {"role": "user", "content": "x"}]))
    assert len(calls) == 1


def test_the_chat_agents_own_endpoint_is_exempt(wire):
    calls, state = wire
    state["reply"] = lambda req: _openai_text("own")
    chat_config.set_config(
        "apichat", {"base_url": "https://own.example.io/v1", "api_key": "sk-own-1", "model": "m"}
    )
    snap = chat_config.snapshot("apichat")
    assert snap[ai_providers.OWN_ENDPOINT_EXEMPT] is True
    r = asyncio.run(
        review.post_chat_response(
            snap, {"model": "m", "messages": [{"role": "user", "content": "x"}]}
        )
    )
    assert r.json()["choices"][0]["message"]["content"] == "own"


def test_the_exemption_cannot_be_supplied_from_outside(auth_cfg, fake_jsonl, wire):
    calls, _ = wire
    key = ai_providers.OWN_ENDPOINT_EXEMPT
    assert "unknown" in prefs.validate_ai_review_patch({key: True})
    with pytest.raises(chat_config.ChatConfigError):
        chat_config.validate_patch({key: True})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/ai-review/endpoint/test",
        json={"base_url": CUSTOM_BASE, "api_key": CUSTOM_KEY, key: True},
        headers=hdr,
    )
    assert r.status_code == 422 and calls == []


def test_only_chat_config_sets_the_exemption():
    import pathlib

    src = pathlib.Path(ai_providers.__file__).parent
    users = sorted(
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "OWN_ENDPOINT_EXEMPT" in p.read_text()
    )
    assert users == ["ai_providers.py", "chat_config.py"]


def test_specific_needs_a_model_on_a_hosted_provider():
    """Hermes on #1672: Auto → Specific with no model picked would save an empty model and send
    every request without one."""
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    assert prefs.get_ai_review()["model_mode"] == "auto"
    with pytest.raises(prefs.ModelChoiceError):
        prefs.set_ai_review({"model_mode": "specific", "model": ""})
    with pytest.raises(prefs.ModelChoiceError):
        prefs.set_ai_review({"model_mode": "specific"})
    assert prefs.get_ai_review()["model_mode"] == "auto"
    full = prefs.set_ai_review({"model_mode": "specific", "model": "gpt-4.1"})
    assert (full["model_mode"], full["model"]) == ("specific", "gpt-4.1")


def test_the_prefs_route_refuses_an_empty_specific_model(auth_cfg, fake_jsonl, wire):
    prefs.set_ai_review({"provider": "openai", "api_key": OPENAI_KEY, "egress_acknowledged": True})
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    r = c.post(
        "/api/prefs", json={"ai_review": {"model_mode": "specific", "model": ""}}, headers=hdr
    )
    assert r.status_code == 422 and "pick a model" in r.json()["detail"]
