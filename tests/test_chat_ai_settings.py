"""The API agent on the operator's Settings → AI endpoint, by reference (#1305).

`source = "ai-settings"` stores no URL and no key: every read resolves Settings → AI at call time.
These pin the contract the issue's P1 lists — the atomic mode switch (checked in the STORED
JSON, not the public view), one resolution rule shared by presence, the card, Test and send, key
rotation reaching the next turn, and a paused edit approval failing closed when the resolved
endpoint changes.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import httpx
import pytest

import test_chat_agent as chat_api
import test_chat_edits as edits
import test_chat_tools as reads
from agent_sessions import chat_config, chat_runtime, engines, prefs, template_secrets
from agent_sessions.engines import registry

project = reads.project
endpoint = reads.endpoint
anyio_backend = reads.anyio_backend
client = chat_api.client
lease_signal = edits.lease_signal

ENGINE = reads.ENGINE
AI_URL = "https://ai-settings.example.test/v1"
AI_KEY = "sk-ai-settings-0123456789"
OWN_URL = "https://own.example.test/v1"
OWN_KEY = "sk-own-key-0123456789"


def set_ai(**fields) -> None:
    prefs.set_ai_review(fields)


def use_ai_settings(**extra) -> dict:
    return chat_config.set_config(ENGINE, {"source": "ai-settings", **extra})


def raw_block() -> dict:
    """The block exactly as persisted — the contract is about what is STORED."""
    return (prefs.read_block(chat_config.BLOCK) or {}).get(ENGINE) or {}


def auth(request: httpx.Request) -> str:
    return request.headers.get("authorization", "")


# --- the stored shape -----------------------------------------------------------------------------


def test_an_own_block_coerces_exactly_as_before():
    """No `source` key appears on an own block, so its binding — and the plugin manager's endpoint
    fingerprint over `stored()` — is unchanged by this feature."""
    chat_config.set_config(ENGINE, {"base_url": OWN_URL, "api_key": OWN_KEY, "model": "m"})
    assert "source" not in raw_block()
    stored = chat_config.stored(ENGINE)
    assert "source" not in stored
    assert chat_config.resolved_binding(ENGINE) == chat_config.binding(stored)


@pytest.mark.parametrize(
    "patch", [{"source": "ai-settings"}, {"source": "ai-settings", "model": ""}]
)
def test_switching_to_ai_settings_deletes_the_own_url_and_key(patch):
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    chat_config.set_config(ENGINE, {"base_url": OWN_URL, "api_key": OWN_KEY, "model": "m"})
    assert raw_block()["key_envelope"]
    chat_config.set_config(ENGINE, patch)
    block = raw_block()
    assert block["source"] == "ai-settings"
    assert block["base_url"] == "" and block["key_envelope"] is None
    # The own model is not carried over as a silent override.
    assert block["model"] == "" and chat_config.snapshot(ENGINE)["model"] == "ai-model"
    dumped = json.dumps(prefs.read_block(chat_config.BLOCK))
    assert OWN_URL not in dumped and OWN_KEY not in dumped and AI_KEY not in dumped


def test_a_legacy_block_without_source_switches_cleanly():
    """A block written before #1305 (no `source` field) loses its URL and envelope too."""

    def legacy(raw):
        agents = dict(raw or {})
        agents[ENGINE] = {
            "base_url": OWN_URL,
            "model": "m",
            "key_envelope": template_secrets.encrypt(f"chat-agent:{ENGINE}", OWN_KEY),
        }
        return agents

    prefs.mutate_block(chat_config.BLOCK, legacy)
    use_ai_settings()
    assert raw_block()["base_url"] == "" and raw_block()["key_envelope"] is None


def test_a_hand_edited_reference_carrying_a_copy_never_yields_it():
    def both(raw):
        agents = dict(raw or {})
        agents[ENGINE] = {
            "source": "ai-settings",
            "base_url": OWN_URL,
            "model": "",
            "key_envelope": template_secrets.encrypt(f"chat-agent:{ENGINE}", OWN_KEY),
        }
        return agents

    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    prefs.mutate_block(chat_config.BLOCK, both)
    snap = chat_config.snapshot(ENGINE)
    assert snap["base_url"] == AI_URL and snap["api_key"] == AI_KEY


@pytest.mark.parametrize(
    "extra",
    [{"base_url": OWN_URL}, {"api_key": OWN_KEY}, {"base_url": OWN_URL, "api_key": OWN_KEY}],
)
def test_a_reference_patch_carrying_a_url_or_key_is_refused(extra):
    with pytest.raises(chat_config.ChatConfigError, match="stores no URL or key"):
        use_ai_settings(**extra)
    assert raw_block() == {}


def test_the_route_refuses_a_reference_with_a_copy_as_422(client):
    r = client.patch(
        f"/api/agents/{ENGINE}/endpoint", json={"source": "ai-settings", "api_key": OWN_KEY}
    )
    assert r.status_code == 422


def test_switching_back_needs_a_url_and_key_again():
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    use_ai_settings()
    for patch in ({"source": "own"}, {"source": "own", "base_url": OWN_URL}):
        with pytest.raises(chat_config.ChatConfigError, match="enter a base URL and an API key"):
            chat_config.set_config(ENGINE, patch)
    chat_config.set_config(
        ENGINE, {"source": "own", "base_url": OWN_URL, "api_key": OWN_KEY, "model": "m"}
    )
    assert "source" not in raw_block()
    assert chat_config.snapshot(ENGINE)["api_key"] == OWN_KEY


def test_only_the_api_agent_may_reference_ai_settings():
    with pytest.raises(chat_config.ChatConfigError, match="only the API agent"):
        chat_config.set_config("someplugin:gen-1", {"source": "ai-settings"})


# --- one resolution rule --------------------------------------------------------------------------


def test_resolution_cases_agree_across_presence_public_and_snapshot():
    use_ai_settings()
    pub = chat_config.public(ENGINE)
    assert not chat_config.is_configured(ENGINE) and chat_config.snapshot(ENGINE) is None
    assert pub["configured"] is False and "configure Settings → AI" in pub["reason"]
    assert pub["ai_settings_ready"] is False

    set_ai(base_url=AI_URL, api_key=AI_KEY)  # URL + key, no model, no override
    pub = chat_config.public(ENGINE)
    assert not chat_config.is_configured(ENGINE) and chat_config.snapshot(ENGINE) is None
    assert "set a model" in pub["reason"] and pub["ai_settings_ready"] is True

    set_ai(model="ai-model")
    pub = chat_config.public(ENGINE)
    assert chat_config.is_configured(ENGINE) and pub["reason"] is None
    assert pub["resolved"] == {"origin": prefs.endpoint_origin(AI_URL), "model": "ai-model"}
    assert chat_config.snapshot(ENGINE)["model"] == "ai-model"

    use_ai_settings(model="override-model")
    assert chat_config.snapshot(ENGINE)["model"] == "override-model"
    assert chat_config.public(ENGINE)["resolved"]["model"] == "override-model"

    set_ai(model="ai-model-2")  # an override wins over a Settings → AI model change
    assert chat_config.snapshot(ENGINE)["model"] == "override-model"
    use_ai_settings(model="")
    assert chat_config.snapshot(ENGINE)["model"] == "ai-model-2"

    for pub in (chat_config.public(ENGINE),):
        assert AI_KEY not in json.dumps(pub) and pub["api_key_set"] is False
        assert pub["base_url"] == ""


def test_the_picker_follows_ai_settings_live():
    use_ai_settings()
    prov = engines.get(ENGINE)
    assert not registry.can_start(prov)
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    assert registry.can_start(prov)
    prefs.set_ai_review({"base_url": "", "api_key": None})
    assert not registry.can_start(prov)


# --- sending --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_rotated_key_reaches_the_next_turn_and_a_cleared_source_stops_new_sessions(
    endpoint, project
):
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    use_ai_settings()
    sid, _ = await reads.turn(project, "first")
    assert endpoint.requests and auth(endpoint.requests[-1]) == f"Bearer {AI_KEY}"
    assert str(endpoint.requests[-1].url).startswith(AI_URL)

    set_ai(api_key="sk-rotated-0123456789")
    t = str(uuid.uuid4())
    await chat_runtime.send(ENGINE, sid, t, "second")
    task = chat_runtime.running_task(ENGINE, sid)
    if task is not None:
        await task
    assert auth(endpoint.requests[-1]) == "Bearer sk-rotated-0123456789"

    prefs.set_ai_review({"base_url": "", "api_key": None})
    with pytest.raises(chat_runtime.ChatError) as refused:
        await chat_runtime.new_session(ENGINE, str(project))
    assert refused.value.status == 409


# --- the endpoint test ----------------------------------------------------------------------------


def test_an_empty_test_request_tests_the_saved_ai_settings_source(client, endpoint):
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    use_ai_settings()
    endpoint.replies = [httpx.Response(200, json={"data": [{"id": "ai-model"}]})]
    r = client.post(f"/api/agents/{ENGINE}/endpoint/test", json={})
    assert r.status_code == 200, r.text
    assert r.json()["listing"] == "ok"
    assert auth(endpoint.requests[-1]) == f"Bearer {AI_KEY}"
    assert str(endpoint.requests[-1].url).startswith(AI_URL)


def test_an_empty_test_request_reports_the_cards_reason(client, endpoint):
    use_ai_settings()
    r = client.post(f"/api/agents/{ENGINE}/endpoint/test", json={})
    assert r.status_code == 422 and "Settings → AI" in r.json()["detail"]
    assert not endpoint.requests


def test_an_empty_test_request_on_an_own_endpoint_still_needs_a_url(client, endpoint):
    chat_config.set_config(ENGINE, {"base_url": OWN_URL, "api_key": OWN_KEY, "model": "m"})
    r = client.post(f"/api/agents/{ENGINE}/endpoint/test", json={})
    assert r.status_code == 422
    assert not endpoint.requests


# --- paused edit approvals ------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "sk-rotated-0123456789"},
        {"base_url": "https://elsewhere.example.test/v1", "api_key": "sk-elsewhere-0123456"},
        {"model": "another-model"},
        # Rotated away and back (Hermes on #1307): the key digest returns to its old value, the
        # never-reused endpoint revision does not.
        [{"api_key": "sk-rotated-0123456789"}, {"api_key": AI_KEY}],
    ],
)
async def test_a_settings_change_while_an_approval_is_pending_invalidates_it(
    endpoint, project, change
):
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    reads.configure("write")  # the proposal flow configures an own endpoint first …
    use_ai_settings()  # … which this switch deletes, leaving only the reference
    content = (project / "README.md").read_bytes()

    endpoint.replies = [
        reads.calls(("read_file", {"path": "README.md"})),
        reads.calls(
            (
                "propose_edit",
                {
                    "path": "README.md",
                    "content": "clearer wording\n",
                    "base_sha256": hashlib.sha256(content).hexdigest(),
                },
            )
        ),
        reads.answer("finished"),
    ]
    sid, tid = await reads.turn(project)
    view = await reads.view(sid)
    assert view["proposals"][0]["can_approve"] is True

    for step in change if isinstance(change, list) else [change]:
        set_ai(**step)
    view = await reads.view(sid)
    assert view["proposals"][0]["can_approve"] is False
    result = await edits.decide(sid, tid, view, "approve")
    assert result["proposal"]["status"] != "approved"
    assert (project / "README.md").read_bytes() == content


def test_an_unusable_secrets_key_is_not_configured_anywhere(monkeypatch):
    """Presence, the card, the snapshot and the binding agree (Hermes on #1307): a broken
    template-secrets key file must not advertise an agent that cannot start a turn."""
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    use_ai_settings()

    def broken(*_a, **_k):
        raise template_secrets.SecretKeyUnavailable("bad key file")

    monkeypatch.setattr(template_secrets, "keyed_digest", broken)
    pub = chat_config.public(ENGINE)
    assert not chat_config.is_configured(ENGINE)
    assert pub["configured"] is False and "template-secrets key" in pub["reason"]
    assert chat_config.snapshot(ENGINE) is None
    assert chat_config.resolved_binding(ENGINE) != chat_config.resolved_binding(ENGINE)
    assert not registry.can_start(engines.get(ENGINE))


def test_the_endpoint_revision_is_server_owned_and_moves_only_with_the_endpoint():
    set_ai(base_url=AI_URL, api_key=AI_KEY, model="ai-model")
    first = prefs.get_ai_review()["endpoint_revision"]
    assert first
    assert "endpoint_revision" not in prefs.public_ai_review()
    assert prefs.validate_ai_review_patch({"endpoint_revision": "x"})  # refused
    set_ai(interval_minutes=10)  # not an endpoint change: approvals stay valid
    assert prefs.get_ai_review()["endpoint_revision"] == first
    set_ai(api_key="sk-rotated-0123456789")
    second = prefs.get_ai_review()["endpoint_revision"]
    set_ai(api_key=AI_KEY)
    third = prefs.get_ai_review()["endpoint_revision"]
    assert len({first, second, third}) == 3


# --- the capability is the manifest's, and first-party only ---------------------------------------


def _chat_manifest(**endpoint):
    from agent_sessions.plugins.manifest import parse
    from test_plugins import chat_doc

    doc = chat_doc("someagent")
    doc["endpoint"] = {"kind": "openai-chat", **endpoint}
    return parse(doc)


def test_the_flag_parses_and_defaults_off():
    assert _chat_manifest().endpoint.ai_settings is False
    assert _chat_manifest(ai_settings=True).endpoint.ai_settings is True


def test_the_flag_grants_nothing_outside_a_first_party_unmanaged_provider(monkeypatch):
    """A managed copy of the API agent (or any installed manifest) may carry the line; only the
    first-party, unmanaged provider is honoured. Loading never fails on it."""
    from agent_sessions.plugins import provenance

    m = _chat_manifest(ai_settings=True)
    provenance.check_vocabulary(m, provenance.LOCAL)  # loads: the flag is inert there, not refused

    class Prov:
        manifest = m
        trust = provenance.LOCAL
        endpoint_scope = None

    monkeypatch.setattr(registry, "get", lambda _id: Prov())
    assert not chat_config.may_reference_ai_settings("someagent")
    Prov.trust = provenance.FIRST_PARTY
    assert chat_config.may_reference_ai_settings("someagent")
    Prov.endpoint_scope = "someagent:gen-1"
    assert not chat_config.may_reference_ai_settings("someagent")


def test_the_api_agent_is_allowed_because_its_manifest_says_so():
    assert chat_config.may_reference_ai_settings(ENGINE)
    assert not chat_config.may_reference_ai_settings("not-an-engine")
    assert not chat_config.may_reference_ai_settings(f"{ENGINE}:gen-1")
