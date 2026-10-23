"""Settings → AI → HQ calls (#1397): the `hq_calls` prefs block.

Speech is configured like the HQ model: STT and TTS are each an OpenAI-compatible endpoint, a base
URL, an optional token and a model. The token rules are the AI key's (`docs/invariants/
ai-key-origin.md`), applied to each side on its own:

- a stored token is only ever sent to the ORIGIN it was saved for; a patch that moves a side's URL
  to another origin must carry that origin's token or clear it (`api_key: null`). Blank,
  whitespace and the mask mean "keep the stored token", so none of them authorizes a move;
- the check runs inside `prefs.mutate_block`'s lock against the lock-current block;
- tokens never leave the server: the public view says `api_key_set`.

Unlike the AI endpoint, a token is OPTIONAL: a self-hosted speech server usually needs none.
"""

from __future__ import annotations

from pathlib import Path

from . import prefs

BLOCK = "hq_calls"
SIDES = ("stt", "tts")
VOICE_GROUPS = ("woman", "man")
TURN_TAKING = ("quick", "natural", "patient")
MIC_MODES = ("open", "push")
ANSWERS = ("brief", "detailed")

MODEL_MAX = prefs.AI_REVIEW_MODEL_MAX
VOICE_MAX = 120
KEY_MAX = prefs.AI_REVIEW_KEY_MAX
MASK = prefs.AI_REVIEW_KEY_MASK

_SIDE_DEFAULTS = {"base_url": "", "api_key": "", "model": ""}
_DEFAULTS: dict = {
    "stt": dict(_SIDE_DEFAULTS),
    "tts": dict(_SIDE_DEFAULTS),
    "voices": {"woman": "", "man": ""},
    "turn_taking": "natural",
    "interrupt": True,
    "mic": "open",
    "answers": "brief",
    "captions": True,
}


class SettingsError(ValueError):
    """A patch that may not be saved (→ 422)."""


def _coerce_side(raw: object) -> dict:
    out = dict(_SIDE_DEFAULTS)
    if isinstance(raw, dict):
        for k in out:
            if isinstance(raw.get(k), str):
                out[k] = raw[k]
    return out


def coerce(raw: object) -> dict:
    out = {
        "stt": _coerce_side(None),
        "tts": _coerce_side(None),
        "voices": dict(_DEFAULTS["voices"]),
        **{k: v for k, v in _DEFAULTS.items() if k not in ("stt", "tts", "voices")},
    }
    if not isinstance(raw, dict):
        return out
    for side in SIDES:
        out[side] = _coerce_side(raw.get(side))
    if isinstance(raw.get("voices"), dict):
        for g in VOICE_GROUPS:
            v = raw["voices"].get(g)
            if isinstance(v, str) and len(v) <= VOICE_MAX:
                out["voices"][g] = v
    for k, allowed in (("turn_taking", TURN_TAKING), ("mic", MIC_MODES), ("answers", ANSWERS)):
        if raw.get(k) in allowed:
            out[k] = raw[k]
    for k in ("interrupt", "captions"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    return out


def get(path: Path | None = None) -> dict:
    """The full block, tokens INCLUDED. Server-side only."""
    return coerce(prefs.read_block(BLOCK, path))


def side_configured(side: dict) -> bool:
    return bool(str(side.get("base_url") or "").strip())


def configured(block: dict) -> bool:
    """Calls are offered once both endpoints and both voices are set. A token is optional."""
    return all(side_configured(block[s]) for s in SIDES) and all(
        block["voices"][g].strip() for g in VOICE_GROUPS
    )


def public(path: Path | None = None) -> dict:
    block = get(path)
    out = {k: v for k, v in block.items() if k not in SIDES}
    for s in SIDES:
        side = block[s]
        out[s] = {
            "base_url": side["base_url"],
            "model": side["model"],
            "api_key_set": bool(side["api_key"]),
        }
    out["configured"] = configured(block)
    return out


def _validate_side(name: str, patch: object) -> None:
    if not isinstance(patch, dict):
        raise SettingsError(f"{name} must be an object")
    unknown = set(patch) - set(_SIDE_DEFAULTS)
    if unknown:
        raise SettingsError(f"unknown {name} fields: {sorted(unknown)}")
    if "base_url" in patch and not prefs.is_valid_ai_base_url(patch["base_url"]):
        raise SettingsError(f"{name}.base_url must be an http(s) URL")
    if "api_key" in patch:
        v = patch["api_key"]
        if v is not None and not isinstance(v, str):
            raise SettingsError(f"{name}.api_key must be a string or null")
        if isinstance(v, str) and len(v) > KEY_MAX:
            raise SettingsError(f"{name}.api_key is too long")
    if "model" in patch and not (
        isinstance(patch["model"], str) and len(patch["model"]) <= MODEL_MAX
    ):
        raise SettingsError(f"{name}.model must be a string of bounded length")


def validate(patch: object) -> None:
    if not isinstance(patch, dict):
        raise SettingsError("the settings must be an object")
    unknown = set(patch) - set(_DEFAULTS)
    if unknown:
        raise SettingsError(f"unknown fields: {sorted(unknown)}")
    for s in SIDES:
        if s in patch:
            _validate_side(s, patch[s])
    if "voices" in patch:
        v = patch["voices"]
        if not isinstance(v, dict) or set(v) - set(VOICE_GROUPS):
            raise SettingsError("voices must be {woman, man}")
        for g, name in v.items():
            if not isinstance(name, str) or len(name) > VOICE_MAX:
                raise SettingsError(f"voices.{g} must be a short string")
    for k, allowed in (("turn_taking", TURN_TAKING), ("mic", MIC_MODES), ("answers", ANSWERS)):
        if k in patch and patch[k] not in allowed:
            raise SettingsError(f"{k} must be one of {list(allowed)}")
    for k in ("interrupt", "captions"):
        if k in patch and not isinstance(patch[k], bool):
            raise SettingsError(f"{k} must be a boolean")


def token_origin_violation(name: str, stored: dict, patch: dict) -> str | None:
    """The AI key's rule (`prefs.key_origin_violation`), for one speech side."""
    why = prefs.key_origin_violation(stored, patch)
    return f"{name}: {why}" if why else None


def _merge_side(cur: dict, patch: dict) -> dict:
    new = dict(cur)
    for k in ("base_url", "model"):
        if k in patch:
            new[k] = patch[k].strip()
    if "api_key" in patch:
        v = patch["api_key"]
        if v is None:
            new["api_key"] = ""
        elif prefs.is_new_api_key(v):
            new["api_key"] = v.strip()
    return new


def save(patch: dict, path: Path | None = None) -> dict:
    """Merge a validated patch under the prefs lock; returns the new full block."""
    validate(patch)

    def merge(raw: object) -> dict:
        cur = coerce(raw)
        for s in SIDES:
            if s in patch:
                why = token_origin_violation(s, cur[s], patch[s])
                if why is not None:
                    raise SettingsError(why)
        new = dict(cur)
        for s in SIDES:
            if s in patch:
                new[s] = _merge_side(cur[s], patch[s])
        if "voices" in patch:
            new["voices"] = {**cur["voices"], **{g: v.strip() for g, v in patch["voices"].items()}}
        for k in ("turn_taking", "mic", "answers", "interrupt", "captions"):
            if k in patch:
                new[k] = patch[k]
        return new

    return prefs.mutate_block(BLOCK, merge, path)


def resolve_test_token(stored: dict, base_url: str, api_key: object) -> tuple[str, str | None]:
    """The token a DRAFT test may send, from one snapshot: `(token, None)` or `("", reason)`.

    A new token in the request wins. Otherwise the stored token, but only for its own origin; for
    another origin the test runs WITHOUT a token (a self-hosted server may need none) — never with
    the stored one."""
    if prefs.is_new_api_key(api_key):
        return str(api_key).strip(), None
    stored_key = str(stored.get("api_key") or "")
    if not stored_key:
        return "", None
    if prefs.endpoint_origin(base_url) == prefs.endpoint_origin(stored.get("base_url")):
        return stored_key, None
    return "", None
