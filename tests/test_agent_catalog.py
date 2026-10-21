"""Release authority never resets accepted remote trust, including after expiry or a crash."""

import asyncio
import json
import runpy
import threading
from pathlib import Path

import pytest

import test_plugin_feed
from agent_sessions import agent_catalog as catalog
from agent_sessions import agent_catalog_refresh as refresh
from agent_sessions.plugins import feed, manager, storage

signer = test_plugin_feed.signer
NOW = test_plugin_feed.NOW


@pytest.fixture
def bundle(signer, tmp_path, monkeypatch):
    path = tmp_path / "bundle.json"
    path.write_bytes(feed.canonical({"format": 1, "agents": [test_plugin_feed.entry()]}))
    monkeypatch.setattr(catalog, "BUNDLED_FILE", path)
    monkeypatch.setattr(feed.time, "time", lambda: NOW)
    return path


def accept(signer, sequence=1, entries=None):
    doc = test_plugin_feed.document(sequence)
    if entries is not None:
        doc["plugins"] = entries
    data = feed.canonical(doc)
    return feed.accept(data, signer(data), now=NOW)


@pytest.mark.deploy_shape
def test_installed_release_contains_valid_catalog_and_native_api_dependencies():
    entries, digest = catalog.bundled()
    assert len(digest) == 64
    agents = {e.manifest.id: e for e in entries}
    assert len(agents) >= 9
    native = [e for e in entries if e.manifest.runtime == "api"]
    assert len(native) >= 3
    for entry in native:
        raw = feed.decode(entry.manifest_bytes)
        assert raw["api"]["source"] in agents
        assert not entry.artifacts


def test_release_bundle_matches_public_definitions_exactly():
    script = Path(__file__).parents[1] / "scripts/build-agent-catalog"
    assert runpy.run_path(str(script))["build"]() == catalog.BUNDLED_FILE.read_bytes()


def test_first_boot_review_uses_release_authority_without_remote_state(bundle):
    review = manager.review(plugin_id="fixture")
    assert review["source"] == "bundled" and review["sequence"] is None
    assert manager._entry(review, fresh=True).digest == review["recipe_digest"]
    assert not (storage.root() / "feed.json").exists()


def test_remote_authority_expiry_requires_review_again_and_exact_bundle_match(
    bundle, signer, monkeypatch
):
    accept(signer)
    review = manager.review(plugin_id="fixture")
    assert review["source"] == "signed"
    monkeypatch.setattr(feed.time, "time", lambda: NOW + 86401)
    assert catalog.current().stale
    with pytest.raises(ValueError, match="review this installation again"):
        manager._entry(review, fresh=True)
    replacement = manager.review(plugin_id="fixture")
    assert replacement["source"] == "bundled"
    assert manager._entry(replacement, fresh=True).digest == review["recipe_digest"]
    # A release update with a different recipe does not override the highest accepted remote cut.
    value = json.loads(bundle.read_bytes())
    value["agents"][0]["manifest"]["identity"]["label"] = "Changed definition"
    bundle.write_bytes(feed.canonical(value))
    with pytest.raises(ValueError, match="expired"):
        manager.review(plugin_id="fixture")
    with pytest.raises(ValueError, match="review this installation again"):
        manager._entry(replacement, fresh=True)


def test_remote_removal_cannot_be_undone_by_a_bundled_recipe(bundle, signer, monkeypatch):
    review = manager.review(plugin_id="fixture")
    accept(signer, entries=[])
    for now in (NOW, NOW + 86401):
        monkeypatch.setattr(feed.time, "time", lambda now=now: now)
        with pytest.raises(ValueError, match="not offered"):
            manager.review(plugin_id="fixture")
        with pytest.raises(ValueError, match="review this installation again"):
            manager._entry(review, fresh=True)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "floor", "rollback"])
def test_established_state_damage_never_becomes_first_boot(bundle, signer, damage):
    accept(signer)
    old = (storage.root() / "feed.json").read_bytes()
    accept(signer, 2)
    path = storage.root() / "feed.json"
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"damaged")
    elif damage == "floor":
        (storage.root() / "feed-floor.json").write_text('{"sequence":true}')
    else:
        path.write_bytes(old)
    before = {p: p.read_bytes() for p in storage.root().iterdir()}
    value = catalog.current()
    assert value.source == "unavailable" and value.error
    assert value.choices and all(c.source is None for c in value.choices)
    with pytest.raises(ValueError, match="Restore"):
        manager.review(plugin_id="fixture")
    assert {p: p.read_bytes() for p in before} == before


def test_relay_era_intact_record_backfills_floor_without_changing_accepted_bytes(bundle, signer):
    accept(signer, 4)
    floor = storage.root() / "feed-floor.json"
    floor.unlink()
    prior = (storage.root() / "feed.json").read_bytes()
    assert catalog.current().sequence == 4
    assert json.loads(floor.read_bytes())["sequence"] == 4
    assert (storage.root() / "feed.json").read_bytes() == prior


def test_interrupted_acceptance_keeps_floor_and_allows_exact_retry(bundle, signer, monkeypatch):
    accept(signer)
    original = storage.write

    def fail(path, value, **kwargs):
        if path.name == "feed.json":
            raise OSError("injected interruption")
        original(path, value, **kwargs)

    monkeypatch.setattr(storage, "write", fail)
    with pytest.raises(OSError):
        accept(signer, 2)
    assert catalog.current().source == "unavailable"
    monkeypatch.setattr(storage, "write", original)
    with pytest.raises(ValueError, match="backwards"):
        accept(signer)
    assert accept(signer, 2).sequence == 2
    assert catalog.current().source == "remote"


def test_missing_accepted_feed_cannot_be_repaired_by_refresh(bundle, signer):
    accept(signer)
    (storage.root() / "feed.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        accept(signer, 2)
    assert not (storage.root() / "feed.json").exists()


def test_daily_checks_rate_limit_failures_across_restart_and_preserve_opt_out(bundle, monkeypatch):
    calls = []

    async def fail():
        calls.append(1)
        raise feed.FeedError("offline")

    monkeypatch.setattr(refresh.feed_client, "refresh", fail)
    with pytest.raises(feed.FeedError):
        asyncio.run(refresh.refresh(automatic=True))
    assert refresh.status()["last_attempt"] == NOW
    assert refresh.status()["last_success"] is None
    assert refresh.status()["error"]
    assert asyncio.run(refresh.refresh(automatic=True)) is None
    refresh.configure(False)
    monkeypatch.setattr(feed.time, "time", lambda: NOW + 2 * 86400)
    assert asyncio.run(refresh.refresh(automatic=True)) is None
    assert len(calls) == 1
    with pytest.raises(feed.FeedError):
        asyncio.run(refresh.refresh())  # explicit check works even after opting out
    assert len(calls) == 2 and refresh.status()["automatic"] is False


def test_manual_and_background_attempts_share_fence_and_keep_concurrent_preferences(
    bundle, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    async def fetch():
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return "verified"

    monkeypatch.setattr(refresh.feed_client, "refresh", fetch)

    async def race():
        task = asyncio.create_task(refresh.refresh())
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            with pytest.raises(storage.StateError, match="busy"):
                await refresh.refresh(automatic=True)
            refresh.configure(False)
        finally:
            release.set()
        assert await task == "verified"

    asyncio.run(race())
    value = refresh.status()
    assert value["automatic"] is False and value["last_success"] == NOW and value["error"] is None


def test_corrupt_refresh_settings_refuse_network_and_remain_untouched(bundle, monkeypatch):
    with storage.locked(refresh.SETTINGS) as path:
        storage.write(path, {"automatic": "wrong"})
    before = path.read_bytes()

    async def never():
        pytest.fail("corrupt settings reached network")

    monkeypatch.setattr(refresh.feed_client, "refresh", never)
    with pytest.raises(ValueError):
        asyncio.run(refresh.refresh())
    with pytest.raises(ValueError):
        refresh.configure(True)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", [None, feed.FeedError("offline"), OSError("unavailable")])
def test_refresh_cancellation_drains_worker_and_preserves_cancellation(
    bundle, monkeypatch, failure
):
    entered, release = threading.Event(), threading.Event()

    async def fetch():
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        if failure is not None:
            raise failure
        return "verified"

    monkeypatch.setattr(refresh.feed_client, "refresh", fetch)

    async def cancel():
        task = asyncio.create_task(refresh.refresh())
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(storage.StateError, match="busy"):
                await refresh.refresh()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    value = refresh.status()
    assert value["last_attempt"] == NOW
    assert bool(value["error"]) == (failure is not None)
    assert value["last_success"] == (NOW if failure is None else None)
    with storage.locked("agent-catalog-refresh", wait=0):
        pass  # ownership only releases once the cancelled worker has finished writing
