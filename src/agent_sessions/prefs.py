"""App preferences — small, user-facing settings the UI persists server-side.

Backed by ``~/.config/agent-sessions/prefs.json`` (override: ``AGENT_SESSIONS_PREFS``).
Deliberately a *separate* file from the session metadata sidecar (metadata.py) and the
env file (boot config / secrets): this is per-app UI state, not session data or secrets.

Single-admin app → a flat ``{"theme": …}`` document, no per-user keying. Concurrent
writers serialize on ``fcntl.flock`` (same approach as metadata.py). Reads tolerate a
missing/empty/corrupt file by returning defaults.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

# Mirror of web/src/theme/themes.ts THEME_IDS. Kept in sync by
# tests/test_prefs.py (server) + the SPA registry test (client).
# `royal` is retired (#211): coerce_theme maps it (any unknown value) → DEFAULT_THEME = dark,
# so a persisted legacy `royal` migrates cleanly instead of stranding on an invalid theme.
THEMES: tuple[str, ...] = ("dark", "light")
DEFAULT_THEME = "dark"

# Compose box default state on load. "auto" keeps the device heuristic (expanded on touch,
# collapsed to the bar on desktop); "open"/"collapsed" force it the same on every device.
COMPOSE_DEFAULTS: tuple[str, ...] = ("auto", "open", "collapsed")
DEFAULT_COMPOSE = "auto"

# Brand accent (#211 Phase 2): a #rrggbb hex driving --accent (and, via color-mix in
# index.css, the derived accent-soft/glow + CTA tokens) plus the xterm cursor. User-
# customizable; the preset palette lives client-side (web/src/theme/accent.ts). Default
# is phosphor-amber — keep in sync with accent.ts DEFAULT_ACCENT.
DEFAULT_ACCENT = "#ffb000"
_HEX6_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX3_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_PREFS",
            str(Path.home() / ".config" / "agent-sessions" / "prefs.json"),
        )
    )


def coerce_theme(value: object) -> str:
    """Narrow any input to a known theme id, falling back to the default."""
    return value if isinstance(value, str) and value in THEMES else DEFAULT_THEME


def coerce_compose_default(value: object) -> str:
    """Narrow any input to a known compose-default mode, falling back to the default."""
    return value if isinstance(value, str) and value in COMPOSE_DEFAULTS else DEFAULT_COMPOSE


def coerce_accent(value: object) -> str:
    """Narrow any input to a normalized lowercase ``#rrggbb`` accent, falling back to the
    default. Accepts ``#rgb`` shorthand (expanded) and a missing leading ``#``; anything
    else (non-string, wrong length, non-hex) → DEFAULT_ACCENT. Applied on read AND write so
    a malformed persisted value can never strand the UI on an invalid accent."""
    if not isinstance(value, str):
        return DEFAULT_ACCENT
    s = value.strip()
    m6 = _HEX6_RE.match(s)
    if m6:
        return "#" + m6.group(1).lower()
    m3 = _HEX3_RE.match(s)
    if m3:
        return "#" + "".join(c * 2 for c in m3.group(1).lower())
    return DEFAULT_ACCENT


def is_valid_accent(value: object) -> bool:
    """True iff ``value`` is a hex colour we accept. The write endpoint uses this to reject
    garbage with a 422 (same contract as theme) rather than silently coercing a bad
    payload to the default on write."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    return bool(_HEX6_RE.match(s)) or bool(_HEX3_RE.match(s))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def coerce_str_list(value: object, cap: int = 2000) -> list[str]:
    """Narrow any input to a bounded list of unique strings (drops non-strings/dupes).
    Used for the overview's expanded list (#144) + the project hide/include lists."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in value:
        if isinstance(v, str) and v not in seen:
            seen.add(v)
            out.append(v)
            if len(out) >= cap:
                break
    return out


def coerce_str_map(
    value: object, cap: int = 500, key_max: int = 4096, val_max: int = 80
) -> dict[str, str]:
    """Narrow any input to a bounded {str: str} map for custom project names (#148).
    Non-string keys/values are dropped; keys over key_max are dropped; values are trimmed
    and capped at val_max; an empty (after-trim) value drops the entry (clears the name).
    Applied on BOTH write and read so a malformed persisted map can't crash the app."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or len(k) > key_max:
            continue
        name = v.strip()[:val_max]
        if not name:
            continue
        out[k] = name
        if len(out) >= cap:
            break
    return out


def _set(key: str, value: object, path: Path | None = None):
    """Persist a single pref key. Read-modify-write under an exclusive flock so a concurrent
    writer (or a different key) can't clobber the rest of the document.

    The file is explicitly chmod'd to 0600 on every write (#356): prefs.json now carries a
    secret (the AI-review API key), and the historical create path inherited the process
    umask — so a pre-existing world-readable file stays readable forever unless we assert
    the tight mode ourselves. Owner-only is correct for every other pref too."""
    path = path or _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    with path.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            try:
                data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
            except json.JSONDecodeError:
                data = {}
            data[key] = value
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return value


def get_theme(path: Path | None = None) -> str:
    """The persisted theme, or the default when unset/unreadable/invalid."""
    return coerce_theme(_load(path or _default_path()).get("theme"))


def set_theme(theme: str, path: Path | None = None) -> str:
    """Persist a theme (invalid input → default). Preserves other keys (e.g. accent)."""
    return _set("theme", coerce_theme(theme), path)


def get_compose_default(path: Path | None = None) -> str:
    """The persisted compose-default mode (auto|open|collapsed), or the default when unset."""
    return coerce_compose_default(_load(path or _default_path()).get("compose_default"))


def set_compose_default(mode: str, path: Path | None = None) -> str:
    """Persist the compose-default mode (invalid input → default). Preserves other keys."""
    return _set("compose_default", coerce_compose_default(mode), path)


def get_vt_scrollback(path: Path | None = None) -> bool | None:
    """The VT-scrollback toggle (#329): ``True``/``False`` when the user has set it,
    or ``None`` when unset — so the caller falls back to the ``AGENT_SESSIONS_VT_SCROLLBACK`` env
    default instead of forcing a value."""
    v = _load(path or _default_path()).get("vt_scrollback")
    return v if isinstance(v, bool) else None


def set_vt_scrollback(value: bool, path: Path | None = None) -> bool:
    """Persist the VT-scrollback toggle. Preserves other keys."""
    return _set("vt_scrollback", bool(value), path)


def get_accent(path: Path | None = None) -> str:
    """The persisted brand accent (#rrggbb), or the default when unset/unreadable/invalid."""
    return coerce_accent(_load(path or _default_path()).get("accent"))


def set_accent(accent: str, path: Path | None = None) -> str:
    """Persist the brand accent, normalized to lowercase #rrggbb (invalid input → default).
    Preserves other keys (e.g. theme)."""
    return _set("accent", coerce_accent(accent), path)


def get_overview_expanded(path: Path | None = None) -> list[str]:
    """Project cwds whose overview cluster is expanded (default: none → collapsed) (#144)."""
    return coerce_str_list(_load(path or _default_path()).get("overview_expanded"))


def set_overview_expanded(cwds: object, path: Path | None = None) -> list[str]:
    """Persist the expanded-cluster cwds. Preserves other keys."""
    return _set("overview_expanded", coerce_str_list(cwds), path)


def get_projects_hidden(path: Path | None = None) -> list[str]:
    """Project cwds globally hidden from the UI (#174). Hide is broader than the retired
    `overview_excluded` (#144): an unchecked project also disappears from the sidebar list,
    the project filter dropdown, and the new-session picker — not just the overview map.

    The legacy `overview_excluded` read-fallback is gone (#357 Phase 2): a one-time
    union-merge into `projects_hidden` runs at app startup instead (see
    `migrate_overview_excluded`), so an old on-disk file still keeps every hide."""
    return coerce_str_list(_load(path or _default_path()).get("projects_hidden"))


def set_projects_hidden(cwds: object, path: Path | None = None) -> list[str]:
    """Persist the hidden-project cwds (#174). Preserves other keys."""
    return _set("projects_hidden", coerce_str_list(cwds), path)


def migrate_overview_excluded(path: Path | None = None) -> list[str] | None:
    """One-time migration retiring the legacy `overview_excluded` key (#357 Phase 2).

    When the legacy key is on disk: union-merge it into `projects_hidden` (existing
    `projects_hidden` entries first, then any legacy hides not already present — no
    hidden project lost, #174 precedence preserved for duplicates), write the normalized
    form once, and drop the legacy key. When it is absent — the steady state after the
    first run — this is a pure no-op: nothing is written, so re-runs are idempotent.

    Returns the merged list when a migration happened, else ``None``. Runs at app
    startup (main.create_app); a missing/corrupt file is tolerated like every read."""
    path = path or _default_path()
    if not path.exists():
        return None
    with path.open("r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = json.load(fh)
                if not isinstance(data, dict):
                    return None
            except json.JSONDecodeError:
                return None
            if "overview_excluded" not in data:
                return None  # already migrated (or never legacy) → never rewrite
            merged = coerce_str_list(data.get("projects_hidden"))
            seen = set(merged)
            for cwd in coerce_str_list(data.pop("overview_excluded")):
                if cwd not in seen:
                    seen.add(cwd)
                    merged.append(cwd)
            data["projects_hidden"] = merged
            fh.seek(0)
            fh.truncate()
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
            return merged
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# Project-visibility mode (#335). "all" = the legacy denylist (`projects_hidden`): every project
# shows unless explicitly hidden — the DEFAULT, so upgrades / fresh installs stay unchanged.
# "included"
# = a curated allowlist: ONLY cwds in `projects_included` show, and a new/unlisted directory never
# auto-appears. The lists are mode-EXCLUSIVE (Hermes #335): `all` consults only `projects_hidden`,
# `included` consults only `projects_included` — never the confusing intersection of both.
PROJECT_MODES: tuple[str, ...] = ("all", "included")
DEFAULT_PROJECT_MODE = "all"


def coerce_project_mode(value: object) -> str:
    """Narrow any input to a known project-visibility mode, falling back to the default."""
    return value if isinstance(value, str) and value in PROJECT_MODES else DEFAULT_PROJECT_MODE


def get_projects_mode(path: Path | None = None) -> str:
    """The project-visibility mode (all|included); default `all` (legacy denylist)."""
    return coerce_project_mode(_load(path or _default_path()).get("projects_mode"))


def set_projects_mode(mode: str, path: Path | None = None) -> str:
    """Persist the project-visibility mode (invalid input → default). Preserves other keys."""
    return _set("projects_mode", coerce_project_mode(mode), path)


def get_projects_included(path: Path | None = None) -> list[str]:
    """The curated allowlist of project cwds shown in `included` mode (#335). Ignored in `all`
    mode. Normalized on read."""
    return coerce_str_list(_load(path or _default_path()).get("projects_included"))


def set_projects_included(cwds: object, path: Path | None = None) -> list[str]:
    """Persist the included-project allowlist (#335). Preserves other keys."""
    return _set("projects_included", coerce_str_list(cwds), path)


def add_project_included(cwd: str, path: Path | None = None) -> list[str]:
    """Idempotently add one cwd to the include-list (#335). Used by auto-include-on-accepted-launch;
    the caller only invokes it in `included` mode, so it never grows the list in `all` mode."""
    cur = get_projects_included(path)
    if cwd and cwd not in cur:
        cur.append(cwd)
        return set_projects_included(cur, path)
    return cur


def project_visible(cwd: str, *, mode: str, hidden: set[str], included: set[str]) -> bool:
    """Whether a project ``cwd`` is visible, given the resolved mode + the two sets (#335). The
    single source of truth threaded through /api/sessions (list + facets), /api/projects (picker),
    and the overview, so the four surfaces can't drift. Pure + mode-EXCLUSIVE: `included` shows only
    allowlisted cwds (a new/unlisted dir stays hidden); any other mode (`all`) hides only
    denylisted cwds."""
    if mode == "included":
        return cwd in included
    return cwd not in hidden


def get_default_project(path: Path | None = None) -> str:
    """The preferred new-session start directory (#335 Phase 2), or "" when unset. The picker
    pre-selects it ONLY when it is still a pickable project (validated client-side on read); a
    stale value (dir gone) silently falls back to the picker's first option — never an error."""
    v = _load(path or _default_path()).get("default_project")
    return v if isinstance(v, str) else ""


def set_default_project(cwd: object, path: Path | None = None) -> str:
    """Persist the preferred new-session cwd (or "" to clear). Preserves other keys."""
    return _set("default_project", cwd if isinstance(cwd, str) else "", path)


def get_project_names(path: Path | None = None) -> dict[str, str]:
    """Per-cwd custom display names for projects (#148). Normalized on read."""
    return coerce_str_map(_load(path or _default_path()).get("project_names"))


def set_project_names(names: object, path: Path | None = None) -> dict[str, str]:
    """Persist the custom project-name map (normalized; empty names drop entries)."""
    return _set("project_names", coerce_str_map(names), path)


# --- AI session review (#356) --------------------------------------------------------
# One nested `ai_review` block: the OpenAI-compatible endpoint config + the review prompt.
# The API key lives here too (prefs.json is chmod 0600 — see `_set`), but it is WRITE-ONLY
# through the HTTP surface: `public_ai_review()` (what /api/config returns) replaces it with
# `api_key_set`, and a POST carrying the mask sentinel / an empty string preserves the stored
# value — only a non-empty new value replaces it, an explicit JSON null clears it.

# What a client sees in the key field when a key is stored; round-tripping it back means
# "unchanged". Deliberately not a plausible key shape.
AI_REVIEW_KEY_MASK = "********"

DEFAULT_AI_REVIEW_PROMPT = (
    "You monitor coding-agent terminal sessions. From the transcript tail and live terminal "
    'output, return strict JSON: {"summary": one line (max 100 chars) of what the session is '
    'doing, "title": short imperative title, "intervention_required": true only if the '
    'agent is blocked on the user (permission prompt, question, fatal error), "reason": one '
    "short line when true}. Be conservative about intervention_required. Output only the JSON "
    "object, no markdown."
)

# Server-owned bounds (#356): every write is validated against these (422 on violation),
# so a malformed/abusive block can never be persisted via the API.
AI_REVIEW_BASE_URL_MAX = 1000
AI_REVIEW_KEY_MAX = 4096
AI_REVIEW_MODEL_MAX = 200
AI_REVIEW_PROMPT_MAX = 8000
AI_REVIEW_INTERVAL_MIN = 1
AI_REVIEW_INTERVAL_MAX = 24 * 60
AI_REVIEW_INPUT_CHARS_MIN = 1_000
AI_REVIEW_INPUT_CHARS_MAX = 200_000
# Per-request review timeout in seconds (#391 follow-up): operator-settable from the UI.
# None = unset → review.py falls back to AGENT_SESSIONS_AI_REVIEW_TIMEOUT, then 120s.
AI_REVIEW_TIMEOUT_MIN = 10
AI_REVIEW_TIMEOUT_MAX = 600

_AI_REVIEW_DEFAULTS: dict[str, object] = {
    "enabled": False,
    "base_url": "",
    "api_key": "",
    "model": "",
    "interval_minutes": 5,
    "prompt": DEFAULT_AI_REVIEW_PROMPT,
    "max_input_chars": 24_000,
    "request_timeout": None,
}


def _valid_base_url(value: object) -> bool:
    """A syntactically sane OpenAI-compatible base URL: http(s), has a host, bounded.
    Empty is allowed (unconfigured)."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if s == "":
        return True
    if len(s) > AI_REVIEW_BASE_URL_MAX:
        return False
    try:
        parts = urlsplit(s)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def get_ai_review(path: Path | None = None) -> dict:
    """The full stored `ai_review` block (INCLUDING the API key) with defaults applied and
    every field coerced to its type. Server-side use only — HTTP surfaces must go through
    `public_ai_review()` so the key never leaves the process."""
    raw = _load(path or _default_path()).get("ai_review")
    out = dict(_AI_REVIEW_DEFAULTS)
    if isinstance(raw, dict):
        for k in ("base_url", "api_key", "model", "prompt"):
            if isinstance(raw.get(k), str):
                out[k] = raw[k]
        if isinstance(raw.get("enabled"), bool):
            out["enabled"] = raw["enabled"]
        for k, lo, hi in (
            ("interval_minutes", AI_REVIEW_INTERVAL_MIN, AI_REVIEW_INTERVAL_MAX),
            ("max_input_chars", AI_REVIEW_INPUT_CHARS_MIN, AI_REVIEW_INPUT_CHARS_MAX),
        ):
            v = raw.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
        t = raw.get("request_timeout")
        if (
            isinstance(t, int | float)
            and not isinstance(t, bool)
            and AI_REVIEW_TIMEOUT_MIN <= t <= AI_REVIEW_TIMEOUT_MAX
        ):
            out["request_timeout"] = t
    if not str(out["prompt"]).strip():
        out["prompt"] = DEFAULT_AI_REVIEW_PROMPT  # empty prompt can never strand reviews
    return out


def public_ai_review(path: Path | None = None) -> dict:
    """The client-safe view of the block: the key is replaced by `api_key_set`, plus
    `configured` (endpoint usable for proxy calls) and the default prompt for the
    reset-to-default control. This is what /api/config and POST /api/prefs echo."""
    full = get_ai_review(path)
    pub = {k: v for k, v in full.items() if k != "api_key"}
    pub["api_key_set"] = bool(full["api_key"])
    pub["configured"] = bool(str(full["base_url"]).strip() and full["api_key"])
    pub["default_prompt"] = DEFAULT_AI_REVIEW_PROMPT
    return pub


def validate_ai_review_patch(patch: object) -> str | None:
    """Server-side schema validation for a partial `ai_review` write (#356): returns a
    human-readable error (→ 422) or None when acceptable. Unknown keys are rejected so a
    typo'd field can't silently no-op; the api_key accepts the mask/empty (preserve) and
    null (clear) sentinels."""
    if not isinstance(patch, dict):
        return "ai_review must be an object"
    unknown = set(patch) - set(_AI_REVIEW_DEFAULTS)
    if unknown:
        return f"unknown ai_review fields: {sorted(unknown)}"
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        return "ai_review.enabled must be a boolean"
    if "base_url" in patch and not _valid_base_url(patch["base_url"]):
        return "ai_review.base_url must be an http(s) URL"
    if "api_key" in patch:
        v = patch["api_key"]
        if v is not None and not isinstance(v, str):
            return "ai_review.api_key must be a string or null"
        if isinstance(v, str) and len(v) > AI_REVIEW_KEY_MAX:
            return "ai_review.api_key is too long"
    if "model" in patch and not (
        isinstance(patch["model"], str) and len(patch["model"]) <= AI_REVIEW_MODEL_MAX
    ):
        return "ai_review.model must be a string of bounded length"
    if "prompt" in patch and not (
        isinstance(patch["prompt"], str) and len(patch["prompt"]) <= AI_REVIEW_PROMPT_MAX
    ):
        return "ai_review.prompt must be a string of bounded length"
    for k, lo, hi in (
        ("interval_minutes", AI_REVIEW_INTERVAL_MIN, AI_REVIEW_INTERVAL_MAX),
        ("max_input_chars", AI_REVIEW_INPUT_CHARS_MIN, AI_REVIEW_INPUT_CHARS_MAX),
    ):
        if k in patch:
            v = patch[k]
            if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                return f"ai_review.{k} must be an integer between {lo} and {hi}"
    if "request_timeout" in patch:
        v = patch["request_timeout"]
        # None = explicit unset (fall back to env/default). NaN/inf fail the range check.
        if v is not None and (
            not isinstance(v, int | float)
            or isinstance(v, bool)
            or not (AI_REVIEW_TIMEOUT_MIN <= v <= AI_REVIEW_TIMEOUT_MAX)
        ):
            return (
                "ai_review.request_timeout must be a number of seconds between "
                f"{AI_REVIEW_TIMEOUT_MIN} and {AI_REVIEW_TIMEOUT_MAX}, or null to unset"
            )
    return None


def set_ai_review(patch: dict, path: Path | None = None) -> dict:
    """Merge a VALIDATED partial block into the stored one (masked-sentinel key handling)
    and persist. Returns the new full block (server-side view, including the key)."""
    cur = get_ai_review(path)
    new = dict(cur)
    for k in (
        "enabled",
        "base_url",
        "model",
        "prompt",
        "interval_minutes",
        "max_input_chars",
        "request_timeout",  # None passes through = unset (env/default applies)
    ):
        if k in patch:
            new[k] = patch[k].strip() if isinstance(patch[k], str) else patch[k]
    if not str(new["prompt"]).strip():
        new["prompt"] = DEFAULT_AI_REVIEW_PROMPT
    if "api_key" in patch:
        v = patch["api_key"]
        if v is None:
            new["api_key"] = ""  # explicit clear
        elif isinstance(v, str) and v.strip() not in ("", AI_REVIEW_KEY_MASK):
            new["api_key"] = v.strip()  # only a real new value replaces the stored key
    _set("ai_review", new, path)
    return new
