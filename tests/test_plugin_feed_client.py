"""Public GitHub reads cannot redirect outside release assets or replace local trust."""

import asyncio

import httpx
import pytest

import test_plugin_feed
from agent_sessions.plugins import feed, feed_client, storage

signer = test_plugin_feed.signer


def selected(data):
    parsed = feed.parse(data)
    return {"sequence": parsed.sequence, "digest": parsed.digest}


def transport(monkeypatch, data, signature, *, metadata=None, fault=None):
    urls = []
    value = {"tag_name": "v0.19.3"} if metadata is None else metadata

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
    base = f"https://github.com/{feed_client.REPOSITORY}/releases/download/v0.19.3/plugin-feed.json"
    assert urls == [feed_client.LATEST, base, base + ".sig"]
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
        limit = [feed_client.MAX_METADATA, feed.MAX_BYTES, 16384][index]
        return httpx.Response(200, content=b"x" * (limit + 1))

    urls = transport(monkeypatch, new, signer(new), fault=fault)
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
    assert {p: p.read_bytes() for p in paths} == before
    assert len(urls) == stage + 1 and all("evil" not in url for url in urls)


@pytest.mark.parametrize(
    "value",
    [
        {"tag_name": "../../evil"},
        {"tag_name": "v1.2.3?bad"},
        {"tag_name": True},
        {"tag_name": "https://evil.test/feed"},
        {},
        [],
    ],
)
def test_invalid_release_tag_refused_before_fetching_assets(signer, monkeypatch, value):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    urls = transport(monkeypatch, data, signer(data), metadata=value)
    with pytest.raises(feed.FeedError, match="invalid version"):
        asyncio.run(feed_client.refresh())
    assert urls == [feed_client.LATEST]


@pytest.mark.parametrize("fault", ["signature", "rollback", "equivocation", "expired", "corrupt"])
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
    sig = b"invalid" if fault == "signature" else signer(data)
    transport(monkeypatch, data, sig)
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


def test_github_asset_redirect_is_bounded_and_anonymous(signer, monkeypatch):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    sig = signer(data)
    urls = []

    def respond(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        url = str(request.url)
        urls.append(url)
        if url == feed_client.LATEST:
            # Metadata-supplied asset URLs are ignored; coordinates come from the fixed repo.
            payload = (
                b'{"tag_name":"v0.19.3","assets":[{"browser_download_url":"https://evil.test"}]}'
            )
            return httpx.Response(200, content=payload)
        if request.url.host == "github.com":
            target = "https://release-assets.githubusercontent.com/file"
            if url.endswith(".sig"):
                target += ".sig"
            return httpx.Response(302, headers={"location": target, "set-cookie": "leak=1"})
        return httpx.Response(
            200, content=sig if url.endswith(".sig") else data, headers={"set-cookie": "leak=2"}
        )

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))
    assert asyncio.run(feed_client.refresh()).sequence == 1
    assert len(urls) == 5 and not any("evil" in url for url in urls)
