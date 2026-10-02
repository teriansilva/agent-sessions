"""First-party feed fetches are anonymous, bounded and unable to select another authority."""

import asyncio

import httpx
import pytest

import test_plugin_feed
from agent_sessions.plugins import feed, feed_client, storage

signer = test_plugin_feed.signer


def test_signed_public_release_feed_accepts_only_after_all_checks(signer, monkeypatch):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    signature = signer(data)
    urls = []

    def respond(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        urls.append(str(request.url))
        if str(request.url) == feed_client.LATEST:
            # Untrusted release metadata cannot provide any download address.
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.2.3",
                    "assets": [{"browser_download_url": "https://evil.test/feed"}],
                },
            )
        if str(request.url).endswith(".sig"):
            return httpx.Response(200, content=signature)
        return httpx.Response(200, content=data)

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))
    assert asyncio.run(feed_client.refresh()).sequence == 1
    assert len(urls) == 3 and all("evil.test" not in url for url in urls)
    assert feed.current().digest == feed.parse(data).digest


@pytest.mark.parametrize("fault", ["foreign", "unsigned", "huge", "tag", "stale"])
def test_failed_refresh_retains_last_accepted_feed(signer, monkeypatch, fault):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document(2))
    feed.accept(data, signer(data))
    before = (storage.root() / "feed.json").read_bytes()
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if str(request.url) == feed_client.LATEST:
            tag = "../../elsewhere" if fault == "tag" else "v1.2.3"
            return httpx.Response(200, json={"tag_name": tag})
        if fault == "foreign":
            return httpx.Response(302, headers={"location": "https://evil.test/feed"})
        if fault == "huge":
            return httpx.Response(200, content=b"x" * (feed.MAX_BYTES + 1))
        new = feed.canonical(test_plugin_feed.document(1 if fault == "stale" else 3))
        if str(request.url).endswith(".sig"):
            return httpx.Response(200, content=b"invalid" if fault == "unsigned" else signer(new))
        return httpx.Response(200, content=new)

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(respond))
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
    assert (storage.root() / "feed.json").read_bytes() == before
    assert not any("evil.test" in url for url in calls)


def test_total_deadline_includes_headers_and_empty_responses(monkeypatch):
    monkeypatch.setattr(feed_client, "DEADLINE", 0.03)

    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(slow))
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
