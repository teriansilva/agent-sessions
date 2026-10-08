"""Fetch only the public first-party release feed, anonymously and with total bounds (#1259).

Release metadata selects a closed tag, never a URL, authority or trust key. All three responses
are bounded inside one deadline. The signed feed verifier, not HTTPS metadata, grants authority.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx

from . import feed, process

REPOSITORY = "teriansilva/agent-sessions"
LATEST = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_METADATA = 256 * 1024
DEADLINE = 30
_TRANSPORT = None  # deterministic test seam, never a runtime configuration


def _url(url: str, *, initial: str, asset: bool) -> str:
    if url == initial:
        return url
    value = urlsplit(url)
    if (
        not asset
        or value.scheme != "https"
        or value.hostname != "release-assets.githubusercontent.com"
        or value.port not in (None, 443)
        or value.username is not None
        or value.password is not None
        or value.fragment
        or "\\" in url
    ):
        raise feed.FeedError("catalog redirect is not allowed")
    return url


async def _get(client, url: str, limit: int, *, asset: bool = False) -> bytes:
    initial = url
    for _ in range(4):
        _url(url, initial=initial, asset=asset)
        client.cookies.clear()
        async with client.stream("GET", url) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = str(response.url.join(response.headers.get("location", "")))
                continue
            if response.status_code != 200:
                raise feed.FeedError("the first-party catalog is unavailable")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise feed.FeedError("catalog response exceeded its limit")
            return bytes(body)
    raise feed.FeedError("too many catalog redirects")


async def refresh() -> feed.Feed:
    try:
        async with asyncio.timeout(DEADLINE):
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False, transport=_TRANSPORT
            ) as client:
                import json

                metadata = json.loads(
                    await _get(client, LATEST, MAX_METADATA), object_pairs_hook=feed._pairs
                )
                tag = metadata.get("tag_name") if isinstance(metadata, dict) else None
                if not isinstance(tag, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
                    raise feed.FeedError("the catalog release has an invalid version")
                base = f"https://github.com/{REPOSITORY}/releases/download/{tag}/plugin-feed.json"
                data = await _get(client, base, feed.MAX_BYTES, asset=True)
                signature = await _get(client, base + ".sig", 16 * 1024, asset=True)
        # Acceptance owns its lock and finite SSH verifier; no background writes outlive it.
        accepting = asyncio.create_task(asyncio.to_thread(feed.accept, data, signature))
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
