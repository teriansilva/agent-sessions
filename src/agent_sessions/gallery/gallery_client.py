"""Anonymous bounded reads at the fixed public gallery origin; no remote URL authority."""

from __future__ import annotations

import asyncio
import re

import httpx

from ..plugins import process
from . import contract, trust

BASE = "https://relay.battlelab.superstatus.io/catalogs/gallery/v1"
_TRANSPORT = None
UNREACHABLE = "The public gallery can't be reached right now."


class Unreachable(contract.GalleryError):
    """The origin did not answer with a publication (unlike content that fails verification)."""


async def _get(client, url, limit):
    client.cookies.clear()
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise Unreachable(UNREACHABLE)
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            raise contract.GalleryError("Encoded gallery responses are not supported.")
        body = bytearray()
        async for chunk in response.aiter_raw(chunk_size=min(65536, limit + 1)):
            if len(chunk) > limit - len(body):
                raise contract.GalleryError("Gallery response exceeded its limit.")
            body.extend(chunk)
        return bytes(body)


async def _finish(fn, *args):
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await process.drain(task)
        raise


async def refresh():
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=_TRANSPORT,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                selected = contract.decode(await _get(client, BASE + "/current.json", 1024))
                if (
                    set(selected) != {"sequence", "digest"}
                    or type(selected["sequence"]) is not int
                    or not 0 < selected["sequence"] <= 9999999999999999
                    or not isinstance(selected["digest"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", selected["digest"])
                ):
                    raise contract.GalleryError("Invalid gallery descriptor.")
                url = (
                    f"{BASE}/releases/{selected['sequence']}-{selected['digest']}"
                    "/gallery-index.json"
                )
                data = await _get(client, url, contract.MAX_BYTES)
                signature = await _get(client, url + ".sig", 16384)
        return await _finish(trust.accept, data, signature, selected)
    except (httpx.HTTPError, TimeoutError):
        raise Unreachable(UNREACHABLE) from None
