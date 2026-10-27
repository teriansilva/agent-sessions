"""Fetch a feed's current freshness statement from the pinned publisher origin (#1730).

Anonymous and bounded, like the feeds themselves. The pointer selects an immutable generation;
the signed statement, not this transport, grants anything (`freshness.checked`). A statement that
can't be fetched is reported as None: a short cut doesn't need one, and a long cut is then
refused by acceptance, keeping everything accepted earlier.
"""

from __future__ import annotations

import httpx

from . import feed, freshness

BASE = "https://relay.battlelab.superstatus.io/catalogs/freshness/v1"


async def _get(client, url: str, limit: int) -> bytes | None:
    client.cookies.clear()
    # This reader takes identity bytes only, so it must not advertise anything else: the agent
    # feed shares a default client that offers gzip/br (Hermes 7189).
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code != 200:
            return None
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            return None
        body = bytearray()
        async for chunk in response.aiter_raw():
            if len(chunk) > limit - len(body):
                return None
            body.extend(chunk)
        return bytes(body)


async def fetch(client, feed_name: str) -> tuple[bytes, bytes, dict] | None:
    """`(statement bytes, signature, pointer)` for `feed_name`, or None. Never raises for a
    transport or shape problem: the caller's acceptance decides what a missing statement means."""
    if feed_name not in freshness.FEEDS:
        raise ValueError("unknown feed")
    try:
        raw = await _get(client, f"{BASE}/{feed_name}/current.json", 1024)
        if raw is None:
            return None
        selected = freshness.pointer(feed.decode(raw))
        url = f"{BASE}/{feed_name}/releases/{selected['sequence']}-{selected['digest']}"
        data = await _get(client, url + "/statement.json", freshness.MAX_STATEMENT_BYTES)
        signature = await _get(client, url + "/statement.json.sig", 16384)
    except (
        httpx.HTTPError,
        httpx.StreamError,
        TimeoutError,
        feed.FeedError,
        freshness.FreshnessError,
    ):
        return None
    if data is None or signature is None:
        return None
    return data, signature, selected
