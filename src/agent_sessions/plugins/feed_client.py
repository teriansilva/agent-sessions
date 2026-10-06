"""Anonymous, bounded reads of the relay's immutable signed catalog (#1283).

The descriptor selects only a sequence and digest at a fixed origin, never a URL or key.
Installed trust roots and the existing durable feed acceptance remain authoritative.
"""

from __future__ import annotations

import asyncio
import re

import httpx

from . import feed, process

BASE = "https://relay.battlelab.superstatus.io/catalogs/agents/v1"
CURRENT = BASE + "/current.json"
MAX_DESCRIPTOR = 1024
DEADLINE = 30
_TRANSPORT = None  # deterministic test seam, never a runtime configuration


def descriptor(data: bytes) -> dict:
    try:
        value = feed.decode(data)
        if (
            len(data) > MAX_DESCRIPTOR
            or set(value) != {"sequence", "digest"}
            or type(value["sequence"]) is not int
            or not 0 < value["sequence"] <= 9999999999999999
            or not isinstance(value["digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["digest"])
        ):
            raise ValueError
        return value
    except ValueError:
        raise feed.FeedError("the catalog descriptor is invalid") from None


async def _get(client, url: str, limit: int) -> bytes:
    # No response may redirect, including to another path on the same origin.
    async with client.stream("GET", url) as response:
        if 300 <= response.status_code < 400:
            raise feed.FeedError("catalog redirects are not allowed")
        if response.status_code != 200:
            raise feed.FeedError("the first-party catalog is unavailable")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise feed.FeedError("catalog response exceeded its limit")
        return bytes(body)


def _accept(data: bytes, signature: bytes, selected: dict) -> feed.Feed:
    # Verify before trusting signed metadata, then bind both immutable resources to the
    # descriptor. Acceptance re-verifies under its own existing durable high-water protocol.
    feed.verify(data, signature)
    parsed = feed.parse(data)
    if selected != {"sequence": parsed.sequence, "digest": parsed.digest}:
        raise feed.FeedError("signed catalog does not match its descriptor")
    try:
        return feed.accept(data, signature)
    except ValueError as exc:
        raise feed.FeedError(str(exc)) from None


async def refresh() -> feed.Feed:
    try:
        async with asyncio.timeout(DEADLINE):
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False, transport=_TRANSPORT
            ) as client:
                selected = descriptor(await _get(client, CURRENT, MAX_DESCRIPTOR))
                base = (
                    f"{BASE}/releases/{selected['sequence']}-{selected['digest']}/plugin-feed.json"
                )
                # Discard unsolicited cookies between requests: all three reads are anonymous.
                client.cookies.clear()
                data = await _get(client, base, feed.MAX_BYTES)
                client.cookies.clear()
                signature = await _get(client, base + ".sig", 16 * 1024)
        accepting = asyncio.create_task(asyncio.to_thread(_accept, data, signature, selected))
        try:
            return await asyncio.shield(accepting)
        except asyncio.CancelledError:
            await process.drain(accepting)
            raise
    except (httpx.HTTPError, TimeoutError):
        raise feed.FeedError(
            "catalog transport failed or timed out; the last accepted catalog was retained"
        ) from None
