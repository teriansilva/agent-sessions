"""The single non-chat embeddings transport for the shared Memory index (#2036).

Only an operator-configured endpoint is reachable. Resolve once/pin the vetted address, retain
TLS verification, never redirect, refuse compressed/oversize replies, and withhold text against
the current secret inventory immediately before sending. There are no model downloads.
"""

from __future__ import annotations

import asyncio
import json
import math
import socket

import httpx

from . import memory, net_vetting, template_secrets

BATCH_MAX = 16
TEXT_MAX = 8000
RESPONSE_MAX = 4 * 1024 * 1024
DIMENSIONS_MAX = 4096
TIMEOUT_S = 12.0
_TRANSPORT = None


class EmbeddingError(RuntimeError):
    def __init__(self, code: str, detail: str = "embeddings are unavailable"):
        super().__init__(detail)
        self.code = code


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _request(cfg: dict, texts: list[str], *, before_send=None) -> list[list[float]]:
    if not cfg.get("base_url") or not cfg.get("model"):
        raise EmbeddingError("unconfigured")
    if (
        not texts
        or len(texts) > BATCH_MAX
        or any(not isinstance(t, str) or len(t) > TEXT_MAX for t in texts)
    ):
        raise EmbeddingError("too_large")
    url = cfg["base_url"].rstrip("/") + "/embeddings"
    dest = await net_vetting.vet(
        url,
        resolve=_resolve,
        error=EmbeddingError,
        policy=net_vetting.TOKEN_HTTPS_OR_LOOPBACK if cfg.get("api_key") else net_vetting.ANY_HTTP,
    )
    headers = {"Host": dest.host_header, "Accept-Encoding": "identity"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    extensions = {"sni_hostname": dest.sni} if dest.sni else None
    payload = json.dumps(
        {"model": cfg["model"], "input": texts, "encoding_format": "float"}
    ).encode()
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = str(len(payload))

    async def guarded_body():
        # httpx consumes the body after connecting. Recheck here, including after a slow DNS/TLS
        # handshake. Never hold the secret fence over network I/O; bytes already handed to the
        # transport are in flight and cannot be recalled by a later registration.
        with memory._fenced():
            known = template_secrets.redaction_values()
            if any(memory._withheld(t, known) for t in texts):
                raise EmbeddingError("withheld")
            if before_send is not None:
                before_send()
        yield payload

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_S, connect=4),
            trust_env=False,
            follow_redirects=False,
            transport=_TRANSPORT,
        ) as client:
            async with client.stream(
                "POST",
                dest.url,
                headers=headers,
                extensions=extensions,
                content=guarded_body(),
            ) as response:
                if response.status_code != 200:
                    raise EmbeddingError("endpoint_refused")
                if response.headers.get("content-encoding", "identity").lower() not in {
                    "",
                    "identity",
                }:
                    raise EmbeddingError("compressed_response")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > RESPONSE_MAX:
                        raise EmbeddingError("response_too_large")
    except httpx.HTTPError as exc:
        raise EmbeddingError("unreachable") from exc
    try:
        rows = json.loads(data)["data"]
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError
        if any(not isinstance(r, dict) or type(r.get("index")) is not int for r in rows):
            raise ValueError
        ordered = sorted(rows, key=lambda r: r["index"])
        if [r["index"] for r in ordered] != list(range(len(texts))):
            raise ValueError
        vectors = [r["embedding"] for r in ordered]
        dimension = len(vectors[0])
        if not 1 <= dimension <= DIMENSIONS_MAX:
            raise ValueError
        if cfg.get("dimensions") and cfg["dimensions"] != dimension:
            raise ValueError
        for vector in vectors:
            if (
                not isinstance(vector, list)
                or len(vector) != dimension
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                or not any(vector)
            ):
                raise ValueError
        return vectors
    except (ValueError, KeyError, TypeError, IndexError, OverflowError, RecursionError) as exc:
        raise EmbeddingError("invalid_vectors") from exc


def embed(cfg: dict, texts: list[str], *, before_send=None) -> list[list[float]]:
    """Called only off the event loop (index worker, scoped query worker, or draft test worker)."""
    return asyncio.run(_request(cfg, texts, before_send=before_send))
