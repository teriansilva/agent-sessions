# ruff: noqa: F811 — the native runtime fixtures are imported, then requested by name
"""API-client model lists are served from cache and refreshed behind it (#1503).

A fake clock drives `native_models._clock`; a fake `discover` stands in for the CLI and records
the thread every probe ran on, so "the caller never waited on a probe" is an observed fact, not
an inference from timing. The providers are the real roster's, resolved through the `host`
fixture the native runtime tests use.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest

from agent_sessions import model_choice, models_loop, native_models, prefs
from agent_sessions import structured_runtime as runtime
from agent_sessions.engines import registry
from test_native_runtime import ENGINES, anyio_backend, host, project  # noqa: F401

pytestmark = pytest.mark.anyio

KIND = "codex-app-server"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _model(mid: str, efforts=()) -> dict:
    return {
        "id": mid,
        "label": mid,
        "description": None,
        "efforts": list(efforts),
        "is_default": False,
    }


class FakeCLI:
    """A `discover` whose answer, failure and blocking are set by the test."""

    def __init__(self, models=None) -> None:
        self.models = models if models is not None else [_model("fake-large", ["low", "high"])]
        self.error: str | None = None
        self.threads: list[int] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def __call__(self, binary: str) -> list[dict]:
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the probe"
        if self.error:
            raise native_models.ProbeError(self.error)
        return [dict(m) for m in self.models]

    @property
    def calls(self) -> int:
        return len(self.threads)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(native_models, "_CACHE", {})
    monkeypatch.setattr(native_models, "_LOCKS", {})
    monkeypatch.setattr(native_models, "_REFRESHER", native_models._Refresher())
    clock = Clock()
    monkeypatch.setattr(native_models, "_clock", clock)
    yield clock
    native_models._REFRESHER.stop(5)


@pytest.fixture
def cli(host, monkeypatch):
    fake = FakeCLI()
    monkeypatch.setitem(native_models._DISCOVER, KIND, fake)
    return fake


@pytest.fixture
def clock(_isolated):
    return _isolated


def _prov():
    return registry.get(ENGINES["codex"])


def _idle():
    assert native_models._REFRESHER.wait_idle(10), "the refresher never went idle"


def _ids(listing) -> list[str]:
    return [m["id"] for m in listing.models]


# --- server: stale-while-revalidate ----------------------------------------------------------


def test_a_fresh_entry_is_served_from_cache(cli, clock):
    first = native_models.models(_prov())
    assert first.status == "ok" and not first.stale and cli.calls == 1
    clock.advance(native_models.TTL_S - 1)
    again = native_models.models(_prov())
    assert _ids(again) == ["fake-large"] and not again.stale and cli.calls == 1
    assert again.fetched_age_s == pytest.approx(native_models.TTL_S - 1)


def test_an_expired_entry_returns_at_once_and_is_refreshed_off_the_callers_thread(cli, clock):
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    cli.models = [_model("fake-large"), _model("fake-new")]
    me = threading.get_ident()
    stale = native_models.models(_prov())
    assert stale.stale and _ids(stale) == ["fake-large"]  # the old list, at once
    _idle()
    assert cli.calls == 2 and cli.threads[1] != me  # the refresh ran on the refresher thread
    fresh = native_models.models(_prov())
    assert not fresh.stale and _ids(fresh) == ["fake-large", "fake-new"]
    assert cli.calls == 2


def test_n_concurrent_callers_of_a_stale_list_start_exactly_one_refresh(cli, clock):
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    cli.gate = threading.Event()
    cli.entered.clear()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(native_models.models(_prov())))
        for _ in range(16)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(5)
    assert len(results) == 16 and all(r.stale for r in results)
    assert cli.entered.wait(5)
    cli.gate.set()
    _idle()
    assert cli.calls == 2  # the first read + ONE refresh


def test_n_concurrent_cold_callers_join_one_probe(cli):
    cli.gate = threading.Event()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(native_models.models(_prov())))
        for _ in range(8)
    ]
    for th in threads:
        th.start()
    assert cli.entered.wait(5)
    time.sleep(0.05)  # every caller is now queued behind the one probe
    cli.gate.set()
    for th in threads:
        th.join(5)
    assert cli.calls == 1 and len(results) == 8 and all(r.status == "ok" for r in results)


def test_a_stale_list_is_returned_lock_free_while_its_refresh_is_blocked(cli, clock):
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    cli.gate = threading.Event()
    cli.entered.clear()
    native_models.models(_prov())  # starts the refresh, which now blocks inside the probe
    assert cli.entered.wait(5)
    assert cli.threads[-1] != threading.get_ident()  # blocked on the refresher, not on us
    try:
        started = time.monotonic()
        listing = native_models.models(_prov())
        elapsed = time.monotonic() - started
        assert listing.stale and _ids(listing) == ["fake-large"]
        assert elapsed < 0.05, elapsed
    finally:
        cli.gate.set()
    _idle()


def test_a_failed_refresh_keeps_the_last_good_list_and_says_why(cli, clock):
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    cli.error = "the agent did not answer in time"
    native_models.models(_prov())
    _idle()
    kept = native_models.models(_prov())
    assert kept.status == "ok" and _ids(kept) == ["fake-large"] and kept.stale
    assert kept.refresh_error == "the agent did not answer in time"
    body = kept.as_dict()
    assert body["refresh_error"] == "the agent did not answer in time"
    assert body["stale"] is True and body["fetched_age_s"] == int(native_models.TTL_S + 1)
    assert cli.calls == 2
    # A broken CLI is not re-spawned per request…
    native_models.models(_prov())
    _idle()
    assert cli.calls == 2
    # …but is asked again once the failure has aged, and a success clears the error.
    clock.advance(native_models.FAILURE_TTL_S)
    cli.error = None
    native_models.models(_prov())
    _idle()
    healed = native_models.models(_prov())
    assert cli.calls == 3 and healed.refresh_error is None and not healed.stale
    assert "refresh_error" not in healed.as_dict()


def test_a_changed_binary_identity_misses_and_probes(cli, clock):
    native_models.models(_prov())
    binary = native_models._target(_prov()).binary
    with open(binary, "a") as fh:  # a CLI upgrade is a new identity
        fh.write("\n# upgraded\n")
    cli.models = [_model("fake-next")]
    me = threading.get_ident()
    listing = native_models.models(_prov())
    assert _ids(listing) == ["fake-next"] and not listing.stale
    assert cli.calls == 2 and cli.threads[1] == me  # a true miss probes synchronously


def test_the_route_carries_the_cache_fields(cli, clock):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_sessions.routes import structured as structured_routes

    app = FastAPI()
    structured_routes.register(app, logged_in=lambda: "admin", csrf_guard=lambda: None)
    client = TestClient(app)
    url = f"/api/structured/clients/{ENGINES['codex']}/models"
    body = client.get(url).json()
    assert body["stale"] is False and body["fetched_age_s"] == 0 and "refresh_error" not in body
    clock.advance(native_models.TTL_S + 120)
    body = client.get(url).json()
    assert body["stale"] is True and body["fetched_age_s"] == int(native_models.TTL_S + 120)


# --- create / Settings save: one snapshot, one fresh probe for an absent id -------------------


def _stale_list_without_new_model(cli, clock):
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    native_models._REFRESHER.stop(5)  # keep the background refresh out of the way


def test_an_id_absent_from_a_stale_list_is_accepted_after_one_fresh_probe(cli, clock):
    _stale_list_without_new_model(cli, clock)
    cli.models = [_model("fake-large"), _model("fake-new", ["max"])]
    me = threading.get_ident()
    assert model_choice.select_api_choice(_prov(), "fake-new", "max") == ("fake-new", "max")
    assert cli.calls == 2 and cli.threads[1] == me


def test_an_id_absent_from_a_stale_list_is_refused_when_the_fresh_probe_fails(cli, clock):
    _stale_list_without_new_model(cli, clock)
    cli.error = "the agent did not answer in time"
    with pytest.raises(model_choice.ModelRefused) as refused:
        model_choice.select_api_choice(_prov(), "fake-new", None)
    assert refused.value.code == "unavailable"
    assert "the agent did not answer in time" in refused.value.detail
    # …and everyone else still gets the last good list.
    assert _ids(native_models.models(_prov())) == ["fake-large"]


def test_an_id_absent_from_the_fresh_list_too_is_not_offered(cli, clock):
    _stale_list_without_new_model(cli, clock)
    with pytest.raises(model_choice.ModelRefused) as refused:
        model_choice.select_api_choice(_prov(), "fake-gone", None)
    assert refused.value.code == "not_offered" and cli.calls == 2


def test_a_choice_present_in_the_stale_list_needs_no_probe(cli, clock):
    """The named relaxation (#1503): same binary, choice in its last good list → accepted."""
    _stale_list_without_new_model(cli, clock)
    assert model_choice.select_api_choice(_prov(), "fake-large", "high") == ("fake-large", "high")
    assert cli.calls == 1


def test_an_effort_absent_from_a_stale_list_is_re_asked_too(cli, clock):
    _stale_list_without_new_model(cli, clock)
    cli.models = [_model("fake-large", ["low", "high", "max"])]
    assert model_choice.select_api_choice(_prov(), "fake-large", "max") == ("fake-large", "max")
    assert cli.calls == 2


def test_a_changed_binary_never_reuses_the_old_list(cli, clock):
    native_models.models(_prov())  # the old binary listed fake-large
    binary = native_models._target(_prov()).binary
    with open(binary, "a") as fh:
        fh.write("\n# upgraded\n")
    cli.models = [_model("fake-next")]
    with pytest.raises(model_choice.ModelRefused) as refused:
        model_choice.select_api_choice(_prov(), "fake-large", None)
    assert refused.value.code == "not_offered"


def test_model_and_effort_are_checked_against_one_snapshot(host, monkeypatch):
    """A refresh landing between the model check and the effort check cannot split them."""
    with_level = native_models.ModelList("ok", (_model("fake-large", ["high"]),))
    without = native_models.ModelList("ok", (_model("fake-large", []),))
    served = iter([with_level, without, without])
    reads = []
    monkeypatch.setattr(native_models, "models", lambda prov: reads.append(1) or next(served))
    assert model_choice.select_api_choice(_prov(), "fake-large", "high") == ("fake-large", "high")
    assert len(reads) == 1


async def test_create_validates_against_one_snapshot_while_a_refresh_lands(host, project, cli):
    """create ∥ refresh: the refresh swaps the list mid-create, the create keeps its snapshot."""
    native_models.models(_prov())
    real_snapshot = native_models.snapshot_for

    def snapshot_then_refresh(prov, model, effort=None):
        listing = real_snapshot(prov, model, effort)
        cli.models = [_model("fake-large", [])]  # the next list drops the level…
        native_models._record(
            native_models._target(prov).key, native_models._probe(native_models._target(prov))
        )  # …and lands now, before the effort is checked
        return listing

    import unittest.mock

    with unittest.mock.patch.object(native_models, "snapshot_for", snapshot_then_refresh):
        created = await runtime.create_session(
            ENGINES["codex"],
            str(project),
            operation_id=str(uuid.uuid4()),
            model="fake-large",
            effort="high",
        )
    assert created["model_requested"] == "fake-large"


async def test_create_takes_an_id_only_the_fresh_list_offers(host, project, clock, monkeypatch):
    """End to end through the fake CLI: the stale list lacks fake-small; one probe finds it."""
    stale_cli = FakeCLI([_model("fake-large")])
    monkeypatch.setitem(native_models._DISCOVER, KIND, stale_cli)
    native_models.models(_prov())
    clock.advance(native_models.TTL_S + 1)
    native_models._REFRESHER.stop(5)
    monkeypatch.setitem(native_models._DISCOVER, KIND, native_models._codex)  # the real probe
    created = await runtime.create_session(
        ENGINES["codex"], str(project), operation_id=str(uuid.uuid4()), model="fake-small"
    )
    assert created["model_requested"] == "fake-small"


def test_settings_save_uses_the_same_snapshot_rule(cli, clock, monkeypatch):
    from agent_sessions import engines

    monkeypatch.setattr(engines, "get", lambda eid: _prov() if eid == ENGINES["codex"] else None)
    _stale_list_without_new_model(cli, clock)
    cli.models = [_model("fake-large"), _model("fake-new", ["max"])]
    patch = {ENGINES["codex"]: {"model": "fake-new", "effort": "max"}}
    assert prefs.validate_session_defaults_patch(patch) is None
    assert cli.calls == 2  # one fresh probe, for the id the stale list lacked
    # Present in the (now fresh) list: no probe; absent from a fresh list: refused, no probe.
    assert (
        prefs.validate_session_defaults_patch(
            {ENGINES["codex"]: {"model": "fake-large", "effort": None}}
        )
        is None
    )
    assert "is not offered" in prefs.validate_session_defaults_patch(
        {ENGINES["codex"]: {"model": "fake-gone", "effort": None}}
    )
    assert cli.calls == 2
    # Settings checks model and effort against one read, as create does.
    reads = []
    real = native_models.models
    monkeypatch.setattr(native_models, "models", lambda prov: reads.append(1) or real(prov))
    prefs.validate_session_defaults_patch(patch)
    assert len(reads) == 1


# --- lifespan: warm-up, refresh loop, bounded shutdown ---------------------------------------


async def test_the_warm_up_probes_each_available_client_once_off_the_event_loop(
    host, clock, monkeypatch
):
    loop_thread = threading.get_ident()
    fakes = {kind: FakeCLI() for kind in ("codex-app-server", "claude-stream-json")}
    for kind, fake in fakes.items():
        monkeypatch.setitem(native_models._DISCOVER, kind, fake)
    opencode = FakeCLI()

    def opencode_discover(binary, *, source):
        return opencode(binary)

    opencode_discover.needs_source = True  # type: ignore[attr-defined]
    monkeypatch.setitem(native_models._DISCOVER, "opencode-acp", opencode_discover)
    # opencode's source CLI is unavailable: it is skipped, never probed.
    monkeypatch.setattr(
        registry,
        "api_unavailable_reason",
        lambda prov: "opencode is not installed" if prov.engine_id == ENGINES["opencode"] else None,
    )
    monkeypatch.setenv("AGENT_SESSIONS_MODELS_LOOP", "1")
    monkeypatch.setattr(models_loop, "INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr(models_loop, "INTERVAL_S", 60.0)
    task = asyncio.create_task(models_loop.run())
    try:
        for _ in range(200):
            if all(f.calls for f in fakes.values()) and native_models._REFRESHER.wait_idle(0):
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [f.calls for f in fakes.values()] == [1, 1] and opencode.calls == 0
    assert all(t != loop_thread for f in fakes.values() for t in f.threads)
    # A second pass finds both fresh and queues nothing…
    assert await asyncio.to_thread(models_loop.warm_once) == []
    # …and one that runs as a list nears TTL_S renews it before it expires.
    clock.advance(native_models.TTL_S - models_loop.INTERVAL_S)
    queued = await asyncio.to_thread(models_loop.warm_once)
    assert sorted(queued) == sorted([ENGINES["codex"], ENGINES["claude"]])
    await asyncio.to_thread(_idle)
    assert [f.calls for f in fakes.values()] == [2, 2]


def test_the_warm_up_is_off_under_its_kill_switch(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MODELS_LOOP", "0")
    called = []
    monkeypatch.setattr(models_loop, "warm_once", lambda: called.append(1) or [])
    asyncio.run(models_loop.run())
    assert not called


def test_shutdown_with_a_probe_in_flight_is_bounded(cli, monkeypatch):
    cli.gate = threading.Event()  # never released: the probe outlives the shutdown's wait
    assert native_models.schedule(_prov())
    assert cli.entered.wait(5)
    started = time.monotonic()
    assert native_models.shutdown(timeout=0.3) is False  # gave up waiting, did not hang
    assert time.monotonic() - started < 1.0
    assert not native_models.schedule(_prov())  # nothing is queued after shutdown
    cli.gate.set()


def test_the_lifespan_drains_a_probe_in_flight_within_the_probe_timeout(
    host, auth_cfg, monkeypatch
):
    """A slow probe — one that only ends at its deadline — holds teardown no longer than that."""
    from fastapi.testclient import TestClient

    from agent_sessions.main import create_app

    monkeypatch.setattr(native_models, "TIMEOUT_S", 1.0)
    entered = threading.Event()

    def slow(binary):
        entered.set()
        time.sleep(native_models.TIMEOUT_S)  # the probe's own deadline ends it
        raise native_models.ProbeError("the agent did not answer in time")

    monkeypatch.setitem(native_models._DISCOVER, KIND, slow)
    with TestClient(create_app(auth_cfg), base_url="https://testserver"):
        assert native_models.schedule(_prov())
        assert entered.wait(5)
        started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < native_models.TIMEOUT_S + 6.0, elapsed
    assert native_models._REFRESHER.wait_idle(0)  # the probe finished; no thread is left


def test_a_failed_warm_up_is_retried_at_its_failure_ttl_with_no_visitor(cli, clock, monkeypatch):
    """#1503 PR review: a CLI that failed at startup and then recovered is re-probed by the loop
    once `FAILURE_TTL_S` has passed — not a whole TTL later, and with nobody on the page."""
    monkeypatch.setattr(models_loop, "clients", lambda: [_prov()])
    cli.error = "the agent did not answer in time"
    assert models_loop.warm_once() == [ENGINES["codex"]]
    _idle()
    key = native_models._target(_prov()).key
    assert native_models._CACHE[key].listing.status == "unavailable"
    cli.error = None  # the CLI recovers; no request comes in
    clock.advance(native_models.FAILURE_TTL_S - 1)
    assert models_loop.warm_once() == []  # not before the failure TTL…
    clock.advance(1)
    assert models_loop.warm_once() == [ENGINES["codex"]]  # …but at it
    _idle()
    assert native_models._CACHE[key].listing.status == "ok" and cli.calls == 2
    assert models_loop.INTERVAL_S <= native_models.FAILURE_TTL_S


def test_a_failed_refresh_of_a_good_list_is_retried_at_its_failure_ttl(cli, clock, monkeypatch):
    monkeypatch.setattr(models_loop, "clients", lambda: [_prov()])
    native_models.models(_prov())
    clock.advance(native_models.TTL_S)
    cli.error = "the agent did not answer in time"
    assert models_loop.warm_once() == [ENGINES["codex"]]
    _idle()
    key = native_models._target(_prov()).key
    assert native_models._CACHE[key].refresh_error
    cli.error = None
    clock.advance(native_models.FAILURE_TTL_S)
    assert models_loop.warm_once() == [ENGINES["codex"]]
    _idle()
    assert native_models._CACHE[key].refresh_error is None and cli.calls == 3


def test_a_probe_a_timed_out_drain_left_behind_never_lands_after_a_restart(cli, monkeypatch):
    """#1503 PR review: `stop` gave up on a probe, `start` installed a new refresher — the old
    probe's late answer is dropped, and only the new refresher's lands."""
    old_gate, new_gate = threading.Event(), threading.Event()
    gates = iter([old_gate, new_gate])
    answers = iter([[_model("from-old")], [_model("from-new")]])
    entered = []

    def gated(binary):
        gate = next(gates)
        models = next(answers)
        entered.append(gate)
        assert gate.wait(10)
        return models

    monkeypatch.setitem(native_models._DISCOVER, KIND, gated)
    assert native_models.schedule(_prov())
    for _ in range(200):
        if entered:
            break
        time.sleep(0.01)
    old = native_models._REFRESHER
    assert native_models.shutdown(timeout=0.1) is False  # the old worker is still probing
    native_models.start()
    assert native_models._REFRESHER is not old
    assert native_models.schedule(_prov())  # the new refresher queues its own probe
    key = native_models._target(_prov()).key
    old_gate.set()  # the old probe answers late…
    assert old._thread is None or old.wait_idle(5)
    for _ in range(200):
        if len(entered) == 2:
            break
        time.sleep(0.01)
    assert key not in native_models._CACHE  # …and is dropped, not recorded
    new_gate.set()
    _idle()
    assert _ids(native_models._CACHE[key].listing) == ["from-new"]
