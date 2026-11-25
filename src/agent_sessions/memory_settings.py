"""The shared Memory embeddings connection: encrypted key, origin-bound saves (#2036).

Keyword search needs no endpoint. Testing a draft neither activates it nor invalidates the
working index; only a save issues a new embedding configuration generation.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from . import memory, prefs, template_secrets

BLOCK = "memory_search"
KEY_NAME = "battlelab-memory-embeddings"
DEFAULTS = {"base_url": "", "model": "", "key": None, "revision": "", "dimensions": 0}


def stored() -> dict:
    raw = prefs.read_block(BLOCK)
    if raw is None:
        return dict(DEFAULTS)
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise memory.MemoryStoreError("memory connection is unreadable", 503)
    block = {**DEFAULTS, **raw}
    if (
        any(not isinstance(block[k], str) for k in ("base_url", "model", "revision"))
        or type(block["dimensions"]) is not int
        or not 0 <= block["dimensions"] <= 4096
        or (block["revision"] and not re.fullmatch(r"[0-9a-f]{32}", block["revision"]))
    ):
        raise memory.MemoryStoreError("memory connection is unreadable", 503)
    return block


def _key(block: dict) -> str:
    envelope = block.get("key")
    if envelope is None:
        return ""
    if not template_secrets.valid_envelope(envelope):
        raise memory.MemoryStoreError("memory key needs re-entry", 503)
    value = template_secrets.decrypt_checked(KEY_NAME, envelope)
    if value is None:
        raise memory.MemoryStoreError("memory key needs re-entry", 503)
    return value


def get() -> dict:
    block = stored()
    return {**{k: v for k, v in block.items() if k != "key"}, "api_key": _key(block)}


def public() -> dict:
    from . import memory_index

    block = stored()
    needs_reentry = False
    try:
        _key(block)
    except (memory.MemoryStoreError, template_secrets.SecretKeyUnavailable):
        needs_reentry = True
    return {
        "base_url": block["base_url"],
        "model": block["model"],
        "api_key_set": block["key"] is not None,
        "needs_reentry": needs_reentry,
        "configured": bool(block["base_url"] and block["model"] and not needs_reentry),
        "dimensions": block["dimensions"],
        "vectors": memory_index.vector_support(),
    }


def signature() -> str:
    # Part of the secret-inventory signature, including ciphertext to detect a key rotation.
    return hashlib.sha256(json.dumps(stored(), sort_keys=True).encode()).hexdigest()


def secret_values_checked() -> list[str]:
    value = _key(stored())
    return [value] if value else []


def _patch(block: dict, patch: object) -> dict:
    if not isinstance(patch, dict) or set(patch) - {"base_url", "model", "api_key"}:
        raise memory.MemoryStoreError("send base_url, model and optional api_key", 422)
    plain = {**block, "api_key": "stored" if block.get("key") is not None else ""}
    reason = prefs.key_origin_violation(plain, patch)
    if reason:
        raise memory.MemoryStoreError(reason, 422)
    out = dict(block)
    for name, cap in (("base_url", 2000), ("model", 200)):
        if name in patch:
            v = patch[name]
            if not isinstance(v, str) or len(v) > cap or any(ord(c) < 32 for c in v):
                raise memory.MemoryStoreError(f"invalid {name}", 422)
            out[name] = v.strip().rstrip("/") if name == "base_url" else v.strip()
    if out["base_url"]:
        try:
            u = urlsplit(out["base_url"])
            if u.port is not None and not 1 <= u.port <= 65535:
                raise ValueError
        except ValueError:
            raise memory.MemoryStoreError("invalid embeddings endpoint", 422) from None
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise memory.MemoryStoreError("invalid embeddings endpoint", 422)
    key = patch.get("api_key", "")
    if key is not None and (not isinstance(key, str) or len(key) > prefs.AI_REVIEW_KEY_MAX):
        raise memory.MemoryStoreError("invalid api_key", 422)
    if isinstance(key, str) and any(not 32 <= ord(c) <= 126 for c in key):
        raise memory.MemoryStoreError("the API key must use printable ASCII", 422)
    if key is None:
        out["key"] = None
    elif prefs.is_new_api_key(key):
        out["key"] = template_secrets.encrypt(KEY_NAME, key.strip())
    if any(out[k] != block[k] for k in ("base_url", "model", "key")):
        out["revision"] = uuid.uuid4().hex
        out["dimensions"] = 0
    return out


def draft(patch: dict) -> dict:
    # One snapshot. A draft key from another origin must be supplied or cleared explicitly.
    block = _patch(stored(), patch)
    return {**{k: v for k, v in block.items() if k != "key"}, "api_key": _key(block)}


def save(patch: dict) -> dict:
    def merge(raw):
        if raw is not None and not isinstance(raw, dict) and not changes_key:
            raise memory.MemoryStoreError("memory connection needs an explicit key repair", 503)
        current = {**DEFAULTS, **raw} if isinstance(raw, dict) else dict(DEFAULTS)
        return _patch(current, patch)

    changes_key = (
        isinstance(patch, dict)
        and "api_key" in patch
        and (patch["api_key"] is None or prefs.is_new_api_key(patch["api_key"]))
    )
    # A model/endpoint-only save changes the vector generation, not the secret set. Avoid
    # re-reading every Markdown projection under the global secret fence in that case.
    changing = memory.secrets_changing() if changes_key else contextlib.nullcontext()
    with template_secrets.registration_fence(), changing:
        try:
            old = secret_values_checked() if changes_key else []
        except (memory.MemoryStoreError, template_secrets.SecretKeyUnavailable):
            old = []  # an explicit replacement/clear can repair an unreadable envelope
        prefs.mutate_block(BLOCK, merge)
        if changes_key:
            template_secrets.retire(old)
    return public()


def observe_dimensions(revision: str, dimensions: int) -> bool:
    """Pin the first validated response to its saved generation, never to an unsaved draft."""
    if type(dimensions) is not int or not 1 <= dimensions <= 4096:
        raise memory.MemoryStoreError("invalid embedding dimensions", 503)
    accepted = False

    def merge(raw):
        nonlocal accepted
        block = {**DEFAULTS, **raw} if isinstance(raw, dict) else dict(DEFAULTS)
        if block["revision"] != revision:
            return block
        if block["dimensions"] not in {0, dimensions}:
            raise memory.MemoryStoreError(
                "embedding dimensions changed; save the connection again", 503
            )
        block["dimensions"] = dimensions
        accepted = True
        return block

    prefs.mutate_block(BLOCK, merge)
    return accepted
