# ruff: noqa: F811 — the native runtime fixtures are imported, then requested by name
"""API clients offer the models their own CLI reports, and nothing else (#1313).

Driven against `fake_native.py` standing in for `codex app-server` / `claude` stream-JSON over
real stdio, through the same `host` fixture the native runtime tests use.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_sessions import model_choice, native_models
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from agent_sessions.plugins import kinds
from agent_sessions.routes import structured as structured_routes
from test_native_runtime import ENGINES, anyio_backend, host, project  # noqa: F401

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(native_models, "_CACHE", {})
    monkeypatch.setattr(native_models, "_LOCKS", {})


def _ids(listing):
    return [m["id"] for m in listing.models]


def test_every_api_adapter_decides_where_its_model_list_comes_from():
    assert set(native_models._DISCOVER) == set(kinds.API_KINDS)


def test_codex_lists_every_page_skips_hidden_and_keeps_efforts_and_default(host):
    listing = native_models.models(registry.get(ENGINES["codex"]))
    assert listing.status == "ok", listing.reason
    assert _ids(listing) == ["fake-large", "fake-small"]
    large = listing.models[0]
    assert large["is_default"] is True and large["efforts"] == ["low", "high"]
    assert large["label"] == "Fake Large"


def test_claude_lists_initialize_models_without_default_or_malformed_ids(host):
    listing = native_models.models(registry.get(ENGINES["claude"]))
    assert listing.status == "ok", listing.reason
    assert _ids(listing) == ["fake-opus"]
    assert listing.models[0]["efforts"] == ["low", "max"]


def test_the_list_is_cached_per_binary_and_refreshed_when_it_changes(host, monkeypatch):
    prov = registry.get(ENGINES["codex"])
    calls = []
    real = native_models._DISCOVER["codex-app-server"]
    monkeypatch.setitem(
        native_models._DISCOVER, "codex-app-server", lambda b: calls.append(b) or real(b)
    )
    native_models.models(prov)
    native_models.models(prov)
    assert len(calls) == 1
    binary = calls[0]
    with open(binary, "a") as fh:  # a new CLI build is a new identity
        fh.write("\n# upgraded\n")
    native_models.models(prov)
    assert len(calls) == 2


def test_a_cli_that_cannot_list_is_unavailable_with_a_reason(host, monkeypatch):
    def broken(binary):
        raise native_models.ProbeError("the agent did not answer in time")

    monkeypatch.setitem(native_models._DISCOVER, "codex-app-server", broken)
    listing = native_models.models(registry.get(ENGINES["codex"]))
    assert listing.status == "unavailable"
    assert listing.reason == "the agent did not answer in time"


def test_a_cli_that_hangs_is_cut_off_at_the_deadline(host, tmp_path, monkeypatch):
    import sys

    hang = tmp_path / "hang"
    hang.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    hang.chmod(0o755)
    monkeypatch.setattr(native_models, "TIMEOUT_S", 0.5)
    with pytest.raises(native_models.ProbeError, match="did not answer in time"):
        native_models._codex(str(hang))


def test_select_api_accepts_only_listed_ids_and_never_substitutes(host, monkeypatch):
    prov = registry.get(ENGINES["codex"])
    assert model_choice.select_api(prov, None) is None
    assert model_choice.select_api(prov, "default") is None
    assert model_choice.select_api(prov, "fake-small") == "fake-small"
    for bad, code in (("fake-hidden", "not_offered"), ("gpt-5", "not_offered"), ("-x", "invalid")):
        with pytest.raises(model_choice.ModelRefused) as refused:
            model_choice.select_api(prov, bad)
        assert refused.value.code == code
    monkeypatch.setitem(
        native_models._DISCOVER,
        "codex-app-server",
        lambda b: (_ for _ in ()).throw(native_models.ProbeError("offline")),
    )
    monkeypatch.setattr(native_models, "_CACHE", {})
    assert model_choice.select_api(prov, "default") is None  # default needs no list
    with pytest.raises(model_choice.ModelRefused) as refused:
        model_choice.select_api(prov, "fake-small")
    assert refused.value.code == "unavailable" and "offline" in refused.value.detail


@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_create_launches_the_chosen_model_and_refuses_an_unlisted_one(host, project, source):
    engine = ENGINES[source]
    model = "fake-small" if source == "codex" else "fake-opus"
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(
            engine, str(project), operation_id=str(uuid.uuid4()), model="not-listed"
        )
    assert refused.value.status == 422 and not host.launches  # nothing was created

    created = await runtime.create_session(
        engine, str(project), operation_id=str(uuid.uuid4()), model=model
    )
    assert created["model_requested"] == model
    frames = [json.loads(x) for x in (project / "native-frames.jsonl").read_text().splitlines()]
    if source == "codex":
        start = next(f["frame"] for f in frames if f["frame"].get("method") == "thread/start")
        assert start["params"]["model"] == model
    else:
        assert any("--model" in f["argv"] and model in f["argv"] for f in frames)


def test_the_models_route_answers_for_api_clients_only(host, monkeypatch):
    app = FastAPI()
    structured_routes.register(app, logged_in=lambda: "admin", csrf_guard=lambda: None)
    client = TestClient(app)
    body = client.get(f"/api/structured/clients/{ENGINES['codex']}/models").json()
    assert body["status"] == "ok" and [m["id"] for m in body["models"]] == [
        "fake-large",
        "fake-small",
    ]
    assert client.get("/api/structured/clients/codex/models").status_code == 404
    assert client.get("/api/structured/clients/nope/models").status_code == 404


@pytest.mark.parametrize("source", ["codex", "claude"])
def test_a_malformed_reply_is_tolerated_never_a_crash(host, tmp_home, source):
    # The probe runs in HOME; the fake reads this marker there (#1313, Hermes).
    (tmp_home / "malformed-models").write_text("")
    listing = native_models.models(registry.get(ENGINES[source]))
    assert listing.status == "ok", listing.reason
    assert [(m["id"], m["efforts"]) for m in listing.models] == [("fake-odd", [])]


def test_an_unforeseen_reply_shape_is_unavailable_through_the_route_and_create(
    host, project, monkeypatch
):
    def odd(binary):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setitem(native_models._DISCOVER, "codex-app-server", odd)
    app = FastAPI()
    structured_routes.register(app, logged_in=lambda: "admin", csrf_guard=lambda: None)
    body = TestClient(app).get(f"/api/structured/clients/{ENGINES['codex']}/models").json()
    assert body == {
        "status": "unavailable",
        "models": [],
        "reason": "the agent's answer was not understood",
    }
    with pytest.raises(model_choice.ModelRefused) as refused:
        model_choice.select_api(registry.get(ENGINES["codex"]), "fake-small")
    assert refused.value.code == "unavailable"
