import json

import httpx
import pytest

from agent_sessions import (
    memory,
    memory_embeddings,
    memory_settings,
    net_vetting,
    prefs,
    template_secrets,
)


def test_key_is_encrypted_and_origin_bound_inside_the_merge():
    key = "sample-only-embeddings-credential"
    public = memory_settings.save(
        {"base_url": "https://one.example/v1", "model": "embed", "api_key": key}
    )
    assert public["api_key_set"] and key not in json.dumps(public)
    block = prefs.read_block(memory_settings.BLOCK)
    assert key not in json.dumps(block) and isinstance(block["key"], dict)
    with pytest.raises(memory.MemoryStoreError, match="key"):
        memory_settings.save({"base_url": "https://two.example/v1"})
    assert memory_settings.get()["base_url"] == "https://one.example/v1"
    memory_settings.save({"base_url": "https://two.example/v1", "api_key": None})
    assert memory_settings.get()["api_key"] == ""


def test_draft_is_not_active_and_does_not_invalidate_generation():
    memory_settings.save({"base_url": "http://localhost:11434/v1", "model": "local"})
    before = memory_settings.stored()
    draft = memory_settings.draft({"model": "other"})
    assert draft["model"] == "other"
    assert memory_settings.stored() == before
    assert "api_key" not in memory_settings.public()


def test_model_only_save_keeps_key_origin_and_does_not_walk_source_projections(monkeypatch):
    key = "sample-only-embeddings-credential"
    memory_settings.save({"base_url": "https://one.example/v1", "model": "first", "api_key": key})
    before = memory_settings.stored()["revision"]
    monkeypatch.setattr(
        memory, "secrets_changing", lambda: pytest.fail("non-secret save scanned notes")
    )
    memory_settings.save({"model": "second", "api_key": ""})
    assert memory_settings.get()["api_key"] == key
    assert memory_settings.stored()["revision"] != before
    with pytest.raises(memory.MemoryStoreError, match="key"):
        memory_settings.save({"base_url": "https://other.example/v1"})
    assert memory_settings.get()["base_url"] == "https://one.example/v1"


def test_non_secret_save_cannot_implicitly_clear_an_unreadable_connection():
    prefs.mutate_block(memory_settings.BLOCK, lambda _: [])
    with pytest.raises(memory.MemoryStoreError, match="explicit key repair"):
        memory_settings.save({"model": "second"})
    assert prefs.read_block(memory_settings.BLOCK) == []


def test_embedding_key_joins_the_secret_inventory_and_retires():
    key = "sample-only-embeddings-credential"
    memory_settings.save({"base_url": "https://one.example/v1", "model": "embed", "api_key": key})
    assert key in template_secrets.redaction_values()
    memory_settings.save({"api_key": None})
    assert key in template_secrets.redaction_values()


def test_embeddings_pin_destination_and_refuse_redirects(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])

    async def vet(url, **kwargs):
        return net_vetting.Pinned(
            "https://127.0.0.1:443/v1/embeddings", "emb.example", "emb.example"
        )

    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.25]}]})

    monkeypatch.setattr(net_vetting, "vet", vet)
    monkeypatch.setattr(memory_embeddings, "_TRANSPORT", httpx.MockTransport(respond))
    cfg = {"base_url": "https://emb.example/v1", "model": "embed", "api_key": "sample-key"}
    assert memory_embeddings.embed(cfg, ["A public example."]) == [[1.0, 0.25]]
    assert seen[0].url.host == "127.0.0.1" and seen[0].headers["host"] == "emb.example"
    monkeypatch.setattr(
        memory_embeddings,
        "_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "https://other.example"})
        ),
    )
    with pytest.raises(memory_embeddings.EmbeddingError, match="unavailable"):
        memory_embeddings.embed(cfg, ["A public example."])


@pytest.mark.parametrize("vector", [[float("nan"), 1], [], [True, 1], [0, 0]])
def test_bad_vectors_are_rejected(monkeypatch, vector):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])
    monkeypatch.setattr(
        memory_embeddings,
        "_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, content=json.dumps({"data": [{"index": 0, "embedding": vector}]})
            ),
        ),
    )
    with pytest.raises(memory_embeddings.EmbeddingError):
        memory_embeddings.embed({"base_url": "http://127.0.0.1/v1", "model": "embed"}, ["Example."])


def test_a_secret_registered_after_connect_is_checked_before_request_body(monkeypatch):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [])
    sent = []

    class Connected(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.headers["Content-Length"]
            monkeypatch.setattr(
                template_secrets, "redaction_values", lambda: ["orchard-harbor-linen"]
            )
            sent.append(await request.aread())
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    monkeypatch.setattr(memory_embeddings, "_TRANSPORT", Connected())
    with pytest.raises(memory_embeddings.EmbeddingError) as error:
        memory_embeddings.embed(
            {"base_url": "http://127.0.0.1/v1", "model": "embed"},
            ["Example marker orchard-harbor-linen."],
        )
    assert error.value.code == "withheld" and not sent


def test_embedding_dimensions_pin_only_the_active_generation():
    memory_settings.save({"base_url": "http://127.0.0.1/v1", "model": "first"})
    old = memory_settings.stored()["revision"]
    assert memory_settings.observe_dimensions(old, 384)
    with pytest.raises(memory.MemoryStoreError, match="dimensions changed"):
        memory_settings.observe_dimensions(old, 768)
    memory_settings.save({"model": "second"})
    new = memory_settings.stored()["revision"]
    assert new != old and not memory_settings.observe_dimensions(old, 384)
    assert memory_settings.observe_dimensions(new, 768)


@pytest.mark.parametrize(
    "url", ["http://[bad", "http://localhost:bad/v1", "http://localhost:70000/v1"]
)
def test_invalid_endpoint_syntax_is_a_validation_error(url):
    with pytest.raises(memory.MemoryStoreError) as error:
        memory_settings.save({"base_url": url, "model": "fixture"})
    assert error.value.status == 422
