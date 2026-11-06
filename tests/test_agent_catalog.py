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


def versioned(version, agent_id="fixture"):
    raw = test_plugin_feed.entry()
    raw["manifest"]["identity"]["version"] = version
    raw["manifest"]["identity"]["id"] = agent_id
    raw["manifest"]["binary"]["name"] = agent_id
    raw["manifest"]["install"]["entrypoint"] = f"node_modules/fixture/bin/{agent_id}"
    return raw


def bundle_with(path, *raws):
    path.write_bytes(feed.canonical({"format": 1, "agents": list(raws)}))


def choice(agent_id="fixture"):
    return next(c for c in catalog.current().choices if c.entry.manifest.id == agent_id)


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize(
    ("ours", "theirs", "winner"),
    [
        ("1.1.0", "1.0.0", "bundled"),  # strictly newer bundle wins (#1861)
        ("1.0.10", "1.0.9", "bundled"),  # numeric, not lexical
        ("1.0.0", "1.0.0", "remote"),  # tie keeps the signed cut
        ("1.0.0", "1.1.0", "remote"),  # the cut is newer
        ("1.1.0", "1", "remote"),  # unparseable remote
        ("1", "1.0.0", "remote"),  # unparseable bundle
        ("1.1.0-beta", "1.0.0", "remote"),  # only exact X.Y.Z is ordered
    ],
)
def test_the_newer_recipe_wins_per_agent(
    bundle, signer, monkeypatch, ours, theirs, winner, expired
):
    mine = versioned(ours)
    if ours == theirs:
        mine["manifest"]["identity"]["label"] = "Bundled definition"  # same version, other digest
    bundle_with(bundle, mine)
    accept(signer, entries=[versioned(theirs)])
    if expired:
        monkeypatch.setattr(feed.time, "time", lambda: NOW + 86401)
    chosen = choice()
    expected = ours if winner == "bundled" else theirs
    assert chosen.entry.manifest.identity.version == expected
    if winner == "bundled":
        assert chosen.source == "bundled" and chosen.reason is None
        assert chosen.entry.digest == catalog.bundled()[0][0].digest
    else:
        # Today's rule, unchanged: signed while fresh, uninstallable once expired (the bundle
        # never carries the remote recipe's exact digest in these cases).
        assert chosen.source == (None if expired else "signed")
    assert len([c for c in catalog.current().choices if c.entry.manifest.id == "fixture"]) == 1


@pytest.mark.parametrize("expired", [False, True])
def test_remote_only_entries_keep_todays_rule(bundle, signer, monkeypatch, expired):
    bundle_with(bundle, versioned("9.0.0"))
    accept(signer, entries=[versioned("9.0.0"), versioned("1.0.0", "other")])
    if expired:
        monkeypatch.setattr(feed.time, "time", lambda: NOW + 86401)
    other = choice("other")
    assert other.entry.manifest.identity.version == "1.0.0"
    assert other.source == (None if expired else "signed")


@pytest.mark.parametrize("expired", [False, True])
def test_a_newer_bundle_cannot_undo_a_remote_removal(bundle, signer, monkeypatch, expired):
    bundle_with(bundle, versioned("9.0.0"))
    accept(signer, entries=[versioned("1.0.0", "other")])
    if expired:
        monkeypatch.setattr(feed.time, "time", lambda: NOW + 86401)
    removed = choice()
    assert removed.source is None and "not offered" in removed.reason
    with pytest.raises(ValueError, match="not offered"):
        manager.review(plugin_id="fixture")


def test_damaged_evidence_stays_unavailable_with_a_newer_bundle(bundle, signer):
    bundle_with(bundle, versioned("9.0.0"))
    accept(signer, entries=[versioned("1.0.0")])
    (storage.root() / "feed.json").write_bytes(b"damaged")
    value = catalog.current()
    assert value.source == "unavailable"
    assert value.choices and all(c.source is None for c in value.choices)
    with pytest.raises(ValueError, match="Restore"):
        manager.review(plugin_id="fixture")


def test_a_bundled_winner_installs_on_release_authority_until_a_newer_cut_arrives(bundle, signer):
    bundle_with(bundle, versioned("1.1.0"))
    accept(signer, entries=[versioned("1.0.0")])
    review = manager.review(plugin_id="fixture")
    assert review["source"] == "bundled" and review["sequence"] is None
    assert manager._entry(review, fresh=True).digest == catalog.bundled()[0][0].digest
    # A newer signed cut pins an even newer version: the bundled review is stale, and a fresh
    # review follows the cut.
    accept(signer, 2, entries=[versioned("1.2.0")])
    with pytest.raises(ValueError, match="review this installation again"):
        manager._entry(review, fresh=True)
    replacement = manager.review(plugin_id="fixture")
    assert replacement["source"] == "signed" and replacement["sequence"] == 2


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


def test_a_failed_check_retries_after_an_hour_and_a_success_waits_a_day(bundle, monkeypatch):
    # #1604: a failure (e.g. GitHub's shared rate limit) no longer parks the daily check for 24 h.
    calls = []
    outcome = {"fail": True}

    async def fetch():
        calls.append(int(feed.time.time()))
        if outcome["fail"]:
            raise feed.FeedError("offline")

    monkeypatch.setattr(refresh.feed_client, "refresh", fetch)
    clock = lambda at: monkeypatch.setattr(feed.time, "time", lambda: NOW + at)  # noqa: E731
    with pytest.raises(feed.FeedError):
        asyncio.run(refresh.refresh(automatic=True))
    clock(refresh.RETRY - 1)
    assert asyncio.run(refresh.refresh(automatic=True)) is None  # still inside the retry floor
    outcome["fail"] = False
    clock(refresh.RETRY)
    asyncio.run(refresh.refresh(automatic=True))
    assert calls == [NOW, NOW + refresh.RETRY] and refresh.status()["error"] is None
    clock(refresh.RETRY + refresh.INTERVAL - 1)
    assert asyncio.run(refresh.refresh(automatic=True)) is None  # success keeps the daily cadence
    clock(refresh.RETRY + refresh.INTERVAL)
    asyncio.run(refresh.refresh(automatic=True))
    assert len(calls) == 3
    outcome["fail"] = True
    clock(refresh.RETRY + refresh.INTERVAL + 1)
    with pytest.raises(feed.FeedError):
        asyncio.run(refresh.refresh())  # a manual check ignores both windows
    assert len(calls) == 4
    assert refresh.status()["error"] == (
        "The update could not be verified or fetched. Previous catalog retained."
    )


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


def test_no_bundled_agent_pins_a_version_below_its_api_clients_native_floor():
    """Ratchet (#1852): a catalog bump can never ship a console agent that its own API client
    refuses (`native_runtime.readiness`). Enabling such a copy is refused at activation, so the
    catalog's offer would be a dead end."""
    from agent_sessions import native_runtime

    raw = {}
    for entry in catalog.bundled()[0]:
        doc = feed.decode(entry.manifest_bytes)
        raw[doc["identity"]["id"]] = doc
    checked = 0
    for client in raw.values():
        api = client.get("api") or {}
        floor = native_runtime._FLOORS.get(api.get("kind"))
        if floor is None:
            continue
        source = raw[api["source"]]
        pinned = (source.get("install") or {}).get("version") or source["identity"]["version"]
        version = native_runtime.parse_version(pinned)
        assert version is not None and version >= floor, (
            f"{api['source']} {pinned} is below {client['identity']['id']}'s native floor "
            f"{'.'.join(map(str, floor))}"
        )
        checked += 1
    assert checked >= 3
