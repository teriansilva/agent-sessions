"""Fetch only the public first-party release feed, anonymously and with total bounds (#1259).

The latest release's redirect selects a closed tag, never a URL, authority or trust key. All three
responses are bounded inside one deadline. The signed feed verifier, not HTTPS metadata, grants
authority. The tag comes from github.com's `releases/latest` redirect, not the REST API, whose
anonymous 60-per-hour budget is shared by everything behind the operator's address (#1604).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx

from . import feed, freshness, freshness_client, process

REPOSITORY = "teriansilva/agent-sessions"
LATEST = f"https://github.com/{REPOSITORY}/releases/latest"
_TAG = re.compile(
    rf"https://github\.com/{re.escape(REPOSITORY)}/releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)"
)
_REDIRECTS = (301, 302, 303, 307, 308)
DEADLINE = 30
_TRANSPORT = None  # deterministic test seam, never a runtime configuration


def _url(url: str, *, initial: str) -> str:
    if url == initial:
        return url
    value = urlsplit(url)
    if (
        value.scheme != "https"
        or value.hostname != "release-assets.githubusercontent.com"
        or value.port not in (None, 443)
        or value.username is not None
        or value.password is not None
        or value.fragment
        or "\\" in url
    ):
        raise feed.FeedError("catalog redirect is not allowed")
    return url


class Unavailable(feed.FeedError):
    """GitHub answered a non-success status. `status` lets the catalog mirror (#2082 P3) tell an
    absent asset (404) from an outage; the client treats both alike."""

    def __init__(self, status: int, step: str):
        # Fixed text plus an integer: response bodies and headers never reach the operator.
        super().__init__(f"GitHub answered HTTP {status} for the catalog's {step}")
        self.status = status


def _unavailable(status: int, step: str) -> feed.FeedError:
    return Unavailable(status, step)


def feed_url(tag: str) -> str:
    """The constructed (never response-supplied) feed asset URL of a release tag."""
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/plugin-feed.json"


def open_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=10, follow_redirects=False, trust_env=False, transport=_TRANSPORT
    )


async def latest_tag(client) -> str:
    """The latest release's tag, read from the `Location` of one never-followed redirect."""
    client.cookies.clear()
    async with client.stream("GET", LATEST) as response:
        if response.status_code in _REDIRECTS:
            target = response.headers.get("location", "")
        elif response.status_code == 200:
            target = ""
        else:
            raise _unavailable(response.status_code, "latest-release lookup")
    match = _TAG.fullmatch(target)
    if match is None:
        raise feed.FeedError("the catalog release has an invalid version")
    return match.group(1)


async def read_asset(client, url: str, limit: int, *, step: str) -> bytes:
    initial = url
    for _ in range(4):
        _url(url, initial=initial)
        client.cookies.clear()
        async with client.stream("GET", url) as response:
            if response.status_code in _REDIRECTS:
                url = str(response.url.join(response.headers.get("location", "")))
                continue
            if response.status_code != 200:
                raise _unavailable(response.status_code, step)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise feed.FeedError("catalog response exceeded its limit")
            return bytes(body)
    raise feed.FeedError("too many catalog redirects")


async def fetch_pair(client) -> tuple[str, bytes, bytes]:
    """`(tag, feed bytes, signature)`: exactly the pair a client resolves. The catalog mirror's
    readback (#2082 P3) calls this same function, so it checks what hosts will read."""
    tag = await latest_tag(client)
    base = feed_url(tag)
    data = await read_asset(client, base, feed.MAX_BYTES, step="feed")
    signature = await read_asset(client, base + ".sig", 16 * 1024, step="feed signature")
    return tag, data, signature


async def refresh() -> feed.Feed:
    try:
        async with asyncio.timeout(DEADLINE):
            async with open_client() as client:
                _, data, signature = await fetch_pair(client)
                # A long cut's freshness statement (#1730), from the pinned publisher origin. A
                # short cut needs none, so none is fetched: the legacy path is unchanged.
                statement = None
                try:
                    long = freshness.needs_statement(feed.decode(data))
                except feed.FeedError:
                    long = False  # acceptance refuses it on its own terms
                if long:
                    statement = await freshness_client.fetch(client, "agents")
        # Acceptance owns its lock and finite SSH verifier; no background writes outlive it.
        accepting = asyncio.create_task(
            asyncio.to_thread(feed.accept, data, signature, statement=statement)
        )
        try:
            return await asyncio.shield(accepting)
        except asyncio.CancelledError:
            await process.drain(accepting)
            raise
    except (httpx.HTTPError, TimeoutError):
        raise feed.FeedError(
            "catalog transport failed or timed out; the last accepted catalog was retained"
        ) from None
    except feed.FeedError:
        raise
    except (ValueError, OSError):
        raise feed.FeedError(
            "catalog data or saved trust evidence is invalid; previous state retained"
        ) from None
