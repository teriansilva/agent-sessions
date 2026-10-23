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

import copy
import hashlib
import threading
from pathlib import Path

from . import hq_contacts, prefs

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
    # P3 (#1623): per-contact overrides, the call language, and what the TTS server advertised.
    "contacts": {},
    "language": "auto",
    "tts_styles": [],
    "tts_languages": [],
    "tts_voices": [],
    # Does the STT endpoint report the language it heard? True / False once TESTED; None until
    # then. Unknown fails spoken confirmation closed; only a tested False means "assume English".
    "stt_detects_language": None,
    # Did the TTS server accept a `language` field in the conformance probe? Only then is one sent.
    "tts_accepts_language": False,
}
SPEED_MIN, SPEED_MAX = 0.75, 1.5
STYLE_MAX = 40
LANGUAGE_VOICES_MAX = 12  # a voice per language, per contact (#1397 P3)
LANG_RE = __import__("re").compile(r"^[a-z]{2}\Z")  # `$` would also match "de\n"
#: What a side's server advertised or the conformance probe proved — true of THAT server, with
#: THAT credential, only (#1635 reviews). A save may make a NEW claim only as far as this process
#: observed it of the endpoint being saved (`observe`, from the test and discovery routes); a claim
#: the settings already hold for an unchanged endpoint stands (a restart never downgrades it). A
#: side whose URL, model or token changes loses the claims the save does not make.
_PROBED = {
    "stt": ("stt_detects_language",),
    "tts": ("tts_accepts_language", "tts_styles", "tts_languages", "tts_voices"),
}
#: (side, base_url, model, token digest) → what the server itself observed there. In memory:
#: after a restart a new claim needs a new test, and a stored one stays until its endpoint changes.
_OBSERVED: dict[tuple[str, str, str, str], dict] = {}
_OBSERVED_MAX = 32
_OBSERVED_LOCK = threading.Lock()


def _ep_key(side: str, base_url: str, model: str, api_key: str) -> tuple[str, str, str, str]:
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:16]
    return (side, (base_url or "").strip(), (model or "").strip(), digest)


def observe(side: str, base_url: str, model: str, api_key: str, **facts) -> None:
    """Record what the server itself saw of an endpoint, as reached with `api_key` (keys from
    `_PROBED[side]`). The token is kept only as a digest, only in memory."""
    key = _ep_key(side, base_url, model, api_key)
    clean = {k: v for k, v in facts.items() if k in _PROBED[side]}
    with _OBSERVED_LOCK:
        entry = _OBSERVED.pop(key, {})
        entry.update(clean)
        _OBSERVED[key] = entry  # newest last
        while len(_OBSERVED) > _OBSERVED_MAX:
            _OBSERVED.pop(next(iter(_OBSERVED)))


def forget_observations() -> None:
    with _OBSERVED_LOCK:
        _OBSERVED.clear()


def _within_evidence(side: str, ep: dict, k: str, claim: object, held: object) -> object:
    """A claim cut down to its evidence: what was observed of `ep`, or what the settings already
    hold for it (`held`; the default when the endpoint changed).

    - `stt_detects_language`: True or False only as observed or held — False relaxes the
      spoken-confirmation gate ("assume English"), so it needs evidence too; else None (unknown).
    - `tts_accepts_language`: True only with evidence; False always stands.
    - lists: only the entries observed or held."""
    with _OBSERVED_LOCK:
        key = _ep_key(side, ep.get("base_url", ""), ep.get("model", ""), ep.get("api_key", ""))
        seen = _OBSERVED.get(key, {}).get(k)
    if k == "stt_detects_language":
        return claim if isinstance(claim, bool) and claim in (seen, held) else None
    if isinstance(claim, bool):
        return claim and (seen is True or held is True)
    have = [*(seen if isinstance(seen, list) else []), *(held if isinstance(held, list) else [])]
    return [x for x in claim if x in have]


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
        # A deep copy: the defaults hold a dict and lists, and a shared one would carry one
        # read's contents into the next.
        **copy.deepcopy({k: v for k, v in _DEFAULTS.items() if k not in ("stt", "tts", "voices")}),
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
    for k in ("interrupt", "captions", "stt_detects_language", "tts_accepts_language"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    if raw.get("stt_detects_language") is None:
        out["stt_detects_language"] = None  # untested (an older False stays a tested False)
    if raw.get("language") == "auto" or (
        isinstance(raw.get("language"), str) and LANG_RE.match(raw["language"])
    ):
        out["language"] = raw["language"]
    for k in ("tts_styles", "tts_languages", "tts_voices"):
        v = raw.get(k)
        if isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, str) and len(x) <= STYLE_MAX][:200]
    contacts = raw.get("contacts")
    if isinstance(contacts, dict):
        for cid, prof in contacts.items():
            if cid in hq_contacts.IDS and isinstance(prof, dict):
                clean = _coerce_profile(prof)
                if clean:
                    out["contacts"][cid] = clean
    return out


def _coerce_profile(prof: dict) -> dict:
    out: dict = {}
    if isinstance(prof.get("voice"), str) and 0 < len(prof["voice"]) <= VOICE_MAX:
        out["voice"] = prof["voice"]
    if isinstance(prof.get("style"), str) and 0 < len(prof["style"]) <= STYLE_MAX:
        out["style"] = prof["style"]
    sp = prof.get("speed")
    if isinstance(sp, int | float) and not isinstance(sp, bool) and SPEED_MIN <= sp <= SPEED_MAX:
        out["speed"] = float(sp)
    lv = prof.get("language_voices")
    if isinstance(lv, dict):
        clean = {
            k: v for k, v in list(lv.items())[:LANGUAGE_VOICES_MAX]
            if isinstance(k, str) and LANG_RE.match(k) and isinstance(v, str)
            and 0 < len(v) <= VOICE_MAX
        }  # fmt: skip
        if clean:
            out["language_voices"] = clean
    return out


def profile(block: dict, contact: str) -> dict:
    """The voice a contact speaks with on a call: its override, else its defaults
    (`hq_contacts`), with the voice falling back to its group's (#1397 P3)."""
    try:
        c = hq_contacts.get(contact)
        group, style, speed = c.voice_group or "woman", c.call_style, c.call_speed
    except KeyError:
        group, style, speed = "woman", "neutral", 1.0
    over = block.get("contacts", {}).get(contact, {})
    group_voice = block["voices"].get(group) or block["voices"]["woman"]
    voice = over.get("voice") or group_voice
    known = block.get("tts_voices") or []
    if known and voice not in known:
        voice = group_voice  # the saved voice left the server: the group's voice until re-picked
    style = over.get("style", style)
    # A voice per language: used while the call is in that language; one that left the server
    # is dropped (the contact's own voice speaks instead).
    saved = over.get("language_voices", {})
    by_lang = {lang: v for lang, v in saved.items() if not known or v in known}
    return {
        "voice": voice,
        "language_voices": by_lang,
        # Shown, not silently lost: the languages whose saved voice left the server.
        "language_voices_missing": sorted(set(saved) - set(by_lang)),
        # Sent only to a server that advertised it: a strict server never sees an unknown field.
        "style": style if style in block.get("tts_styles", []) else None,
        "speed": over.get("speed", speed),
    }


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
    # Each General's own defaults (#1397 P3), so Settings can show an untouched row as it sounds.
    out["contact_defaults"] = {
        c.id: {"group": c.voice_group or "woman", "style": c.call_style, "speed": c.call_speed}
        for c in hq_contacts.CONTACTS
    }
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
    for k in ("interrupt", "captions", "stt_detects_language", "tts_accepts_language"):
        if k in patch and not isinstance(patch[k], bool):
            if k == "stt_detects_language" and patch[k] is None:
                continue  # back to untested
            raise SettingsError(f"{k} must be a boolean")
    if "language" in patch and not (
        patch["language"] == "auto"
        or (isinstance(patch["language"], str) and LANG_RE.match(patch["language"]))
    ):
        raise SettingsError("language must be auto or a two-letter code")
    for k in ("tts_styles", "tts_languages", "tts_voices"):
        if k in patch and not (
            isinstance(patch[k], list)
            and all(isinstance(x, str) and 0 < len(x) <= STYLE_MAX for x in patch[k])
            and len(patch[k]) <= 200
        ):
            raise SettingsError(f"{k} must be a list of short strings")
    if "contacts" in patch:
        c = patch["contacts"]
        if not isinstance(c, dict) or set(c) - set(hq_contacts.IDS):
            raise SettingsError("contacts must map HQ contact ids to profiles")
        for cid, prof in c.items():
            if prof is None:
                continue  # reset to defaults
            if not isinstance(prof, dict) or set(prof) - {
                "voice",
                "style",
                "speed",
                "language_voices",
            }:
                raise SettingsError(
                    f"contacts.{cid} must be {{voice, style, speed, language_voices}}"
                )
            lv = prof.get("language_voices")
            if lv is not None and not (
                isinstance(lv, dict)
                and len(lv) <= LANGUAGE_VOICES_MAX
                and all(
                    isinstance(k, str) and LANG_RE.match(k) and isinstance(v, str)
                    and 0 < len(v) <= VOICE_MAX
                    for k, v in lv.items()
                )
            ):  # fmt: skip
                raise SettingsError(
                    f"contacts.{cid}.language_voices must map two-letter codes to voices"
                )
            if "speed" in prof and (
                not isinstance(prof["speed"], int | float)
                or isinstance(prof["speed"], bool)
                or not SPEED_MIN <= prof["speed"] <= SPEED_MAX
            ):
                raise SettingsError(f"contacts.{cid}.speed must be {SPEED_MIN}–{SPEED_MAX}")
            for k in ("voice", "style"):
                if k in prof and not (
                    isinstance(prof[k], str)
                    and len(prof[k]) <= (VOICE_MAX if k == "voice" else STYLE_MAX)
                ):
                    raise SettingsError(f"contacts.{cid}.{k} must be a short string")


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
        for k in (
            "turn_taking", "mic", "answers", "interrupt", "captions", "language", "tts_styles",
            "tts_languages", "tts_voices", "stt_detects_language", "tts_accepts_language",
        ):  # fmt: skip
            if k in patch:
                new[k] = patch[k]
        for s in SIDES:
            moved = any(new[s].get(k) != cur[s].get(k) for k in ("base_url", "model", "api_key"))
            for k in _PROBED[s]:
                held = copy.deepcopy(_DEFAULTS[k]) if moved else cur[k]
                if k in patch:
                    new[k] = _within_evidence(s, new[s], k, patch[k], held)
                else:
                    new[k] = held
        if "contacts" in patch:
            merged = dict(cur.get("contacts", {}))
            for cid, prof in patch["contacts"].items():
                if prof is None:
                    merged.pop(cid, None)
                else:
                    clean = _coerce_profile(prof)
                    if clean:
                        merged[cid] = clean
                    else:
                        merged.pop(cid, None)
            new["contacts"] = merged
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
