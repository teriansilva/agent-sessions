# ruff: noqa: F811 — the native runtime fixtures are imported, then requested by name
"""API clients offer the models their own CLI reports, and nothing else (#1313).

Driven against `fake_native.py` standing in for `codex app-server` / `claude` stream-JSON over
real stdio, through the same `host` fixture the native runtime tests use.
"""

from __future__ import annotations

import asyncio
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
from test_native_runtime import (  # noqa: F401
    ENGINES,
    anyio_backend,
    frames,
    host,
    project,
    settle,
)

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


def test_opencode_lists_its_models_under_shared_store_admission(host, tmp_home, monkeypatch):
    """`opencode models` is an opencode process on the operator's database: it runs only while it
    holds the store's SHARED admission, so it can never overlap a compaction (#1312)."""
    from agent_sessions import opencode_admission

    prov = registry.get("opencode-api")
    listing = native_models.models(prov)
    assert listing.status == "ok", listing.reason
    # `--verbose` (#1407): each model's `variants` are its levels; a malformed block costs that
    # model its label and levels, never its id or the rest of the list.
    assert [(m["id"], m["label"], m["efforts"]) for m in listing.models] == [
        ("local/fake-coder", "Fake Coder", ["low", "high"]),
        ("openai/fake-gpt", "Fake GPT", []),
        ("local/fake-broken", "local/fake-broken", []),
    ]
    frames = [json.loads(x) for x in (tmp_home / "native-frames.jsonl").read_text().splitlines()]
    assert {"models": True, "password": True} in [f["frame"] for f in frames]

    monkeypatch.setattr(native_models, "_CACHE", {})
    source = registry.get("opencode")
    held = opencode_admission.acquire(
        source.engine_id, exclusive=True, database=source.store_path("db")
    )
    assert held is not None
    try:
        busy = native_models.models(prov)
    finally:
        held.release()
    assert busy.status == "unavailable" and "maintenance" in busy.reason


def test_opencode_verbose_parsing_is_bounded_and_fail_soft():
    """`opencode models --verbose` as 1.18.35 prints it (#1407): `default` is never a level, a
    plain id-per-line answer still parses, and an oversized or unterminated block costs only its
    own model's levels."""
    big = "  " + "x" * (native_models._OPENCODE_BLOCK_MAX + 10)
    text = "\n".join(
        [
            "a/one",
            "{",
            '  "name": "One",',
            '  "variants": {"default": {}, "low": {}, "Bad Level": {}}',
            "}",
            "b/plain",
            "c/huge",
            "{",
            big,
            "}",
            "d/after",
            "{",
            '  "variants": {"high": {}}',
            "}",
            "e/unterminated",
            "{",
            '  "variants": {"low": {}}',
        ]
    )
    got = [(m["id"], m["label"], m["efforts"]) for m in native_models._opencode_models(text)]
    assert got == [
        ("a/one", "One", ["low"]),
        ("b/plain", "b/plain", []),
        ("c/huge", "c/huge", []),
        ("d/after", "d/after", ["high"]),
        ("e/unterminated", "e/unterminated", []),
    ]
    many = "\n".join(f"p/m{i}" for i in range(native_models.MAX_MODELS + 5))
    assert len(native_models._opencode_models(many)) == native_models.MAX_MODELS


def test_select_api_effort_needs_a_model_and_one_of_its_levels(host):
    prov = registry.get(ENGINES["codex"])
    listing = native_models.models(prov)
    model = next(m for m in listing.models if m["efforts"])
    level = model["efforts"][0]
    assert model_choice.select_api_effort(prov, model["id"], None) is None
    assert model_choice.select_api_effort(prov, model["id"], level) == level
    for mid, bad, code in (
        (None, level, "needs_model"),
        (model["id"], "not-a-level", "not_offered"),
        (model["id"], "Bad Level", "invalid"),
    ):
        with pytest.raises(model_choice.ModelRefused) as refused:
            model_choice.select_api_effort(prov, mid, bad)
        assert refused.value.code == code


# (source, model, level) — each fake client's own listed model and one of ITS levels (#1407).
_EFFORT_CASES = [
    ("codex", "fake-large", "high"),
    ("claude", "fake-opus", "max"),
    ("opencode", "local/fake-coder", "high"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("source, model, level", _EFFORT_CASES)
async def test_the_requested_effort_reaches_the_client_on_every_generation(
    host, project, monkeypatch, source, model, level
):
    """#1407: the level is part of the immutable create request and every generation sends it —
    codex on EVERY `turn/start`, claude in every generation's argv, opencode as a confirmed
    `effort` option after the model — including the successor that resumes after an idle exit."""
    from agent_sessions import native_runtime, native_state

    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 0.5)
    engine = ENGINES[source]
    created = await runtime.create_session(
        engine, str(project), operation_id=str(uuid.uuid4()), model=model, effort=level
    )
    key = created["session_key"]
    assert created["model_requested"] == model and created["effort_requested"] == level
    first = str(uuid.uuid4())
    await runtime.submit_turn(key, operation_id=first, text="one")
    snap, _ = await settle(key, first)
    worker = snap["native"]["worker"]
    for _ in range(200):
        if native_state.read_lifecycle(worker).get("phase") == "exited":
            break
        await asyncio.sleep(0.05)
    monkeypatch.setattr(native_runtime, "WORKER_IDLE_TIMEOUT", 60.0)
    second = str(uuid.uuid4())
    await runtime.submit_turn(key, operation_id=second, text="two")
    snap, turn = await settle(key, second)
    assert turn["state"] == "completed" and snap["native"]["worker"] != worker
    assert snap["effort_requested"] == level

    seen = frames(project)
    if source == "codex":
        starts = [f["frame"] for f in seen if f["frame"].get("method") == "turn/start"]
        assert len(starts) == 2 and all(s["params"].get("effort") == level for s in starts)
    elif source == "claude":
        launches = {f["pid"]: f["argv"] for f in seen if "--print" in f["argv"]}
        # At least the creating and the resumed generation; on a loaded runner the creation's own
        # worker can idle out (0.5 s) before its first turn and add one more (#1337). EVERY
        # generation must carry the level, however many there are.
        assert len(launches) >= 2
        for argv in launches.values():
            assert argv[argv.index("--effort") + 1] == level
    else:
        sets = [
            f["frame"]["params"]
            for f in seen
            if f["frame"].get("method") == "session/set_config_option"
        ]
        efforts = [p for p in sets if p["configId"] == "effort"]
        # Every generation (at least create + resume; an early idle-out adds one, #1337) sets it.
        assert len(efforts) >= 2 and all(p["value"] == level for p in efforts)
        for p in efforts:  # always after this generation's model
            before = sets[: sets.index(p)]
            assert before and before[-1] == {**p, "configId": "model", "value": model}


@pytest.mark.anyio
async def test_a_create_without_effort_sends_none_and_records_none(host, project):
    """No level chosen = today's request, byte for byte: nothing recorded, nothing sent."""
    engine = ENGINES["codex"]
    created = await runtime.create_session(
        engine, str(project), operation_id=str(uuid.uuid4()), model="fake-large"
    )
    assert created["effort_requested"] is None
    turn = str(uuid.uuid4())
    await runtime.submit_turn(created["session_key"], operation_id=turn, text="one")
    await settle(created["session_key"], turn)
    start = next(f["frame"] for f in frames(project) if f["frame"].get("method") == "turn/start")
    assert "effort" not in start["params"]


@pytest.mark.anyio
async def test_an_unlisted_or_modelless_effort_is_refused_before_anything_exists(host, project):
    engine = ENGINES["codex"]
    for model, level in (("fake-large", "max"), (None, "high"), ("fake-small", "low")):
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.create_session(
                engine, str(project), operation_id=str(uuid.uuid4()), model=model, effort=level
            )
        assert refused.value.status == 422, (model, level)
    assert not host.launches


@pytest.mark.anyio
async def test_a_replay_with_another_effort_conflicts_instead_of_flipping_it(host, project):
    engine = ENGINES["codex"]
    op = str(uuid.uuid4())
    await runtime.create_session(
        engine, str(project), operation_id=op, model="fake-large", effort="low"
    )
    with pytest.raises(runtime.StructuredError) as conflict:
        await runtime.create_session(
            engine, str(project), operation_id=op, model="fake-large", effort="high"
        )
    assert conflict.value.status == 409


@pytest.mark.anyio
async def test_opencode_refuses_a_level_the_agent_does_not_confirm(host, project, tmp_home):
    """The echo is the proof (`native-runtime.md`): an `effort` the agent does not report back
    fails the generation's start — never a session silently on another level."""
    control = tmp_home / ".fake-opencode"
    control.mkdir(exist_ok=True)
    (control / "effort-ignored").write_text("1")
    with pytest.raises(runtime.StructuredError):
        await runtime.create_session(
            ENGINES["opencode"],
            str(project),
            operation_id=str(uuid.uuid4()),
            model="local/fake-coder",
            effort="high",  # the agent starts on `low` and ignores the switch
        )


@pytest.mark.anyio
async def test_the_chat_runtime_takes_no_effort(host, project):
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.create_session(
            "apichat", str(project), operation_id=str(uuid.uuid4()), effort="high"
        )
    assert refused.value.status in (404, 409, 422)


def test_an_unterminated_opencode_block_resumes_at_the_next_model():
    """#1407 PR review: a malformed block must not swallow the model after it."""
    text = "\n".join(
        [
            "p/broken",
            "{",
            '  "variants": {"low": {}',
            "p/good",
            "{",
            '  "variants": {"high": {}}',
            "}",
        ]
    )
    got = [(m["id"], m["efforts"]) for m in native_models._opencode_models(text)]
    assert got == [("p/broken", []), ("p/good", ["high"])]
