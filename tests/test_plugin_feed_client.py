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


TAG_URL = f"https://github.com/{feed_client.REPOSITORY}/releases/tag/v0.19.3"


def transport(monkeypatch, data, signature, *, location=TAG_URL, fault=None):
    urls = []

    def respond(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        url = str(request.url)
        urls.append(url)
        index = len(urls) - 1
        if fault:
            response = fault(index, request)
            if response is not None:
                return response
        cookie = {"set-cookie": "leak=secret; Path=/"}
        if index == 0:
            headers = cookie if location is None else {**cookie, "location": location}
            return httpx.Response(302, headers=headers, content=b"<html>body is never read")
        return httpx.Response(200, content=signature if index == 2 else data, headers=cookie)

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
        if index == 0:  # the lookup reads no body, so its "large" failure is a refused target
            return httpx.Response(302, headers={"location": TAG_URL + "x" * 10_000})
        limit = [None, feed.MAX_BYTES, 16384][index]
        return httpx.Response(200, content=b"x" * (limit + 1))

    urls = transport(monkeypatch, new, signer(new), fault=fault)
    with pytest.raises(feed.FeedError):
        asyncio.run(feed_client.refresh())
    assert {p: p.read_bytes() for p in paths} == before
    assert len(urls) == stage + 1 and all("evil" not in url for url in urls)


RELEASES = f"https://github.com/{feed_client.REPOSITORY}/releases"


@pytest.mark.parametrize(
    "location",
    [
        None,  # a 302 without a target
        "",
        f"{RELEASES}",  # no release published yet
        f"{RELEASES}/tag/../../evil",
        f"{RELEASES}/tag/v1.2.3?bad",
        f"{RELEASES}/tag/v1.2.3#bad",
        f"{RELEASES}/tag/v1.2.3/",
        f"{RELEASES}/tag/v1.2",
        f"{RELEASES}/tag/1.2.3",
        f"{RELEASES}/tag/v1.2.3-rc1",
        "/teriansilva/agent-sessions/releases/tag/v1.2.3",  # relative
        "http://github.com/teriansilva/agent-sessions/releases/tag/v1.2.3",
        "https://evil.test/teriansilva/agent-sessions/releases/tag/v1.2.3",
        "https://github.com.evil.test/teriansilva/agent-sessions/releases/tag/v1.2.3",
        "https://github.com/someone/agent-sessions/releases/tag/v1.2.3",
        "https://github.com/teriansilva/agent-sessions/releases/download/v1.2.3/plugin-feed.json",
    ],
)
def test_invalid_release_tag_refused_before_fetching_assets(signer, monkeypatch, location):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    urls = transport(monkeypatch, data, signer(data), location=location)
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
            # Body-supplied asset URLs are never read; coordinates come from the fixed repo.
            payload = b'<a href="https://evil.test/plugin-feed.json">plugin-feed.json</a>'
            return httpx.Response(302, headers={"location": TAG_URL}, content=payload)
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


def test_latest_release_lookup_does_not_use_the_rate_limited_api():
    # #1604: the anonymous REST API allows 60 requests an hour per address, shared by every
    # tool behind it; the web redirect names the same tag without spending that budget.
    assert feed_client.LATEST == f"https://github.com/{feed_client.REPOSITORY}/releases/latest"
    assert "api.github.com" not in feed_client.LATEST


def test_latest_release_lookup_never_follows_its_redirect(signer, monkeypatch):
    monkeypatch.setattr(feed.time, "time", lambda: test_plugin_feed.NOW)
    data = feed.canonical(test_plugin_feed.document())
    urls = transport(monkeypatch, data, signer(data))
    asyncio.run(feed_client.refresh())
    assert TAG_URL not in urls and len(urls) == 3


def test_a_200_lookup_is_an_invalid_version_not_a_body_read(signer, monkeypatch):
    def fault(index, request):
        if index == 0:
            return httpx.Response(200, content=b'{"tag_name":"v0.19.3"}')
        return None

    data = feed.canonical(test_plugin_feed.document())
    urls = transport(monkeypatch, data, signer(data), fault=fault)
    with pytest.raises(feed.FeedError, match="invalid version"):
        asyncio.run(feed_client.refresh())
    assert urls == [feed_client.LATEST]


@pytest.mark.parametrize(
    ("stage", "status", "step"),
    [
        (0, 403, "latest-release lookup"),
        (0, 429, "latest-release lookup"),
        (1, 404, "feed"),
        (2, 500, "feed signature"),
    ],
)
def test_http_failure_names_the_step_and_status_but_no_response_content(
    signer, monkeypatch, stage, status, step
):
    def fault(index, request):
        if index == stage:
            return httpx.Response(status, content=b"<script>leak</script> rate limit exceeded")
        return None

    data = feed.canonical(test_plugin_feed.document())
    transport(monkeypatch, data, signer(data), fault=fault)
    with pytest.raises(feed.FeedError) as caught:
        asyncio.run(feed_client.refresh())
    assert str(caught.value) == f"GitHub answered HTTP {status} for the catalog's {step}"
