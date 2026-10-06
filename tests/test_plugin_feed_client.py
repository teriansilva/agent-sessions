"""Relay transport cannot redirect, replace trust, roll back or repair accepted evidence."""

import asyncio

import httpx
import pytest

import test_plugin_feed
from agent_sessions.plugins import feed, feed_client, storage

signer = test_plugin_feed.signer


def selected(data):
    parsed = feed.parse(data)
    return {"sequence": parsed.sequence, "digest": parsed.digest}


def transport(monkeypatch, data, signature, *, descriptor=None, fault=None):
    urls = []
    value = selected(data) if descriptor is None else descriptor

    def respond(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        url = str(request.url)
        urls.append(url)
        index = len(urls) - 1
        if fault:
            response = fault(index, request)
            if response is not None:
                return response
        content = feed.canonical(value) if index == 0 else signature if index == 2 else data
        return httpx.Response(200, content=content, headers={"set-cookie": "leak=secret; Path=/"})

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))
    return urls


@pytest.mark.parametrize("previous", [False, True])
def test_fresh_and_legacy_cache_accept_same_verified_cut(signer, monkeypatch, previous):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    sig = signer(data)
    if previous:
        feed.accept(data, sig)  # identical format/high-water evidence written by the old client
    urls = transport(monkeypatch, data, sig)
    assert asyncio.run(feed_client.refresh()).sequence == 1
    value = selected(data)
    base = f"{feed_client.BASE}/releases/1-{value['digest']}/plugin-feed.json"
    assert urls == [feed_client.CURRENT, base, base + ".sig"]
    assert feed.current().digest == value["digest"]


@pytest.mark.parametrize("stage", range(3))
@pytest.mark.parametrize("failure", ["redirect", "large", "http", "timeout"])
def test_transport_failure_at_each_stage_retains_all_evidence(signer, monkeypatch, stage, failure):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    old = feed.canonical(test_plugin_feed.document(1))
    feed.accept(old, signer(old))
    paths = [p for p in storage.root().iterdir() if p.is_file()]
    before = {p: p.read_bytes() for p in paths}
    new = feed.canonical(test_plugin_feed.document(2))

    def fault(index, request):
        if index != stage:
            return None
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.test/catalog"})
        if failure == "http":
            return httpx.Response(500)
        if failure == "timeout":
            raise httpx.ReadTimeout("test", request=request)
        limit = [feed_client.MAX_DESCRIPTOR, feed.MAX_BYTES, 16384][index]
        return httpx.Response(200, content=b"x" * (limit + 1))

    urls = transport(monkeypatch, new, signer(new), fault=fault)
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
    assert {p: p.read_bytes() for p in paths} == before
    assert len(urls) == stage + 1 and all("evil" not in url for url in urls)


@pytest.mark.parametrize(
    "value",
    [
        {"sequence": True, "digest": "a" * 64},
        {"sequence": 0, "digest": "a" * 64},
        {"sequence": 10**16, "digest": "a" * 64},
        {"sequence": 1, "digest": "A" * 64},
        {"sequence": 1, "digest": "../feed"},
        {"sequence": 1, "digest": "a" * 64, "url": "https://evil.test"},
        {"sequence": 1},
    ],
)
def test_descriptor_shape_refused_before_fetching_assets(signer, monkeypatch, value):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    urls = transport(monkeypatch, data, signer(data), descriptor=value)
    with pytest.raises(feed.FeedError, match="descriptor is invalid"):
        asyncio.run(feed_client.refresh())
    assert urls == [feed_client.CURRENT]


@pytest.mark.parametrize("raw", [b'{"sequence":1,"sequence":2}', b"{}\n", b"[]", b"NaN"])
def test_descriptor_ambiguous_json_refused(raw):
    with pytest.raises(feed.FeedError, match="descriptor is invalid"):
        feed_client.descriptor(raw)


@pytest.mark.parametrize(
    "fault", ["signature", "digest", "sequence", "rollback", "equivocation", "expired", "corrupt"]
)
def test_signed_identity_and_existing_acceptance_stay_authoritative(signer, monkeypatch, fault):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    old = feed.canonical(test_plugin_feed.document(2))
    feed.accept(old, signer(old))
    if fault == "corrupt":
        (storage.root() / "feed.json").write_bytes(b"corrupt")
    before = {p: p.read_bytes() for p in storage.root().iterdir() if p.is_file()}
    doc = test_plugin_feed.document(
        1 if fault == "rollback" else 2 if fault == "equivocation" else 3
    )
    if fault == "equivocation":
        doc["expires_at"] -= 1
    data = feed.canonical(doc)
    value = selected(data)
    if fault == "digest":
        value["digest"] = "a" * 64
    if fault == "sequence":
        value["sequence"] += 1
    sig = b"invalid" if fault == "signature" else signer(data)
    transport(monkeypatch, data, sig, descriptor=value)
    if fault == "expired":
        monkeypatch.setattr(feed.time, "time", lambda: doc["expires_at"] + 1)
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
    assert {p: p.read_bytes() for p in before} == before


def test_total_deadline_includes_headers_and_empty_responses(monkeypatch):
    monkeypatch.setattr(feed_client, "DEADLINE", 0.03)

    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(slow))
    with pytest.raises(feed.FeedError, match="timed out"):
        asyncio.run(feed_client.refresh())
