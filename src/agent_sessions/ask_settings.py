"""What HQ may read of the operator's settings: a closed allowlist, written as code (#1554).

``read_settings(section)`` answers one SECTION from :data:`SECTIONS`; each section is one builder
below that names every field it returns and coerces every value. There is no generic "dump prefs"
and no field-name passthrough: a prefs key not named here is unreachable from HQ.

**Values are gated, not only keys.** Every value is a bool, an int, an agent label
(:func:`agent_labels`: AUTHORED text from the app's own packaged manifests, never an installed
plugin's), or a member of a closed enum.
Anything outside its enum is OMITTED, never echoed. So a credential, an endpoint URL, a path,
a template value or an operator-typed free string (the AI model name) cannot reach the model
through here.

**Never readable, by construction:** the AI key and endpoint URL and model; template variables;
auth configuration, password state and the username; forge credentials; analytics ids; project
roots, folder exclusions and every other filesystem path (``mission_plan``'s rule: a model that
never sees a path cannot echo one).

Changing a setting stays in Settings: HQ points the operator there (``ask_orchestrator``'s prompt).
"""

from __future__ import annotations

import contextlib
import functools
import re
from collections.abc import Callable

#: The sections HQ may read, in the order the tool offers them.
SECTIONS: tuple[str, ...] = ("usage", "agents", "automation", "ai_review", "pulse", "appearance")

_ACCENT_RE = re.compile(r"^#[0-9a-f]{6}$")


class UnknownSection(ValueError):
    """A section outside :data:`SECTIONS`: refused, never repaired."""


def _bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def _int(v: object, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if lo <= v <= hi else None


def _member(v: object, allowed) -> str | None:
    return v if isinstance(v, str) and v in allowed else None


def _clean(d: dict) -> dict:
    """Drop the fields whose value failed its gate: omitted, never echoed."""
    return {k: v for k, v in d.items() if v is not None}


@functools.lru_cache(maxsize=1)
def _packaged_names() -> dict[str, str]:
    """``{engine id: label}`` from the app's OWN first-party manifests, read from the package.

    Written in this repository, so authored text: an installed plugin's display name never reaches
    the model, whatever shape it has — no shape test can tell a segmented key from a name (#1569
    review 6739)."""
    import tomllib

    from .plugins import FIRST_PARTY_DIR

    out: dict[str, str] = {}
    for f in sorted(FIRST_PARTY_DIR.glob("*/plugin.toml")):
        with contextlib.suppress(OSError, ValueError):
            ident = tomllib.loads(f.read_text(encoding="utf-8")).get("identity") or {}
            if isinstance(ident.get("id"), str) and isinstance(ident.get("label"), str):
                out[ident["id"]] = ident["label"]
    return out


def agent_labels() -> dict[str, str]:
    """``{engine id: label}`` for every present provider: what HQ calls each one.

    The packaged first-party label for its id (:func:`_packaged_names`), else ``agent N``. Labels
    are made unique (``Name (2)``, in engine-id order), so two engines never merge their counts or
    limits under one name. Names, never ids, reach the model (consumers name no engine)."""
    from .engines import registry

    packaged = _packaged_names()
    raw: list[tuple[str, str]] = []
    for i, prov in enumerate(sorted(registry.present_providers(), key=lambda p: p.engine_id)):
        raw.append((prov.engine_id, packaged.get(prov.engine_id) or f"agent {i + 1}"))
    out: dict[str, str] = {}
    taken: set[str] = set()
    for engine_id, label in raw:
        unique, n = label, 1
        while unique.casefold() in taken:
            n += 1
            unique = f"{label} ({n})"
        taken.add(unique.casefold())
        out[engine_id] = unique
    return out


#: A usage window's label as this app's own reporters write it: a duration (``_window_label`` in
#: ``agent_usage``: ``5h``, ``90m``, ``week``, ``2 weeks``, ``day``) or kimi's fixed names. Digits
#: and fixed words only.
_DURATION_RE = re.compile(
    r"^(?:window|week|day|month|month \(code\)|[0-9]{1,4}[hm]|[0-9]{1,3} (?:weeks|days))$"
)
#: Claude's ``/usage`` lines (``Current <label>: N% used``): vendor text, so only these exact
#: labels pass, each answered with THIS table's own words.
_KNOWN_WINDOWS = {
    "session": "session",
    "week (all models)": "week (all models)",
    "week (opus)": "week (Opus)",
    "week (opus only)": "week (Opus only)",
    "week (sonnet)": "week (Sonnet)",
    "week (sonnet only)": "week (Sonnet only)",
}


def window_label(value: object) -> str | None:
    """A usage window's label HQ may show the model, or ``None`` (the caller numbers it).

    A CLOSED set, not a shape test: a duration made of digits and fixed words, or one of
    :data:`_KNOWN_WINDOWS`. Vendor text outside it never reaches the model."""
    if not isinstance(value, str):
        return None
    v = " ".join(value.split())
    if _DURATION_RE.match(v):
        return v
    return _KNOWN_WINDOWS.get(v.casefold())


def _agent_names() -> dict[str, str]:
    """:func:`agent_labels` for the present AGENTS only."""
    from .engines import registry

    labels = agent_labels()
    return {
        p.engine_id: labels[p.engine_id]
        for p in registry.present_providers()
        if registry.is_agent(p) and p.engine_id in labels
    }


def _usage() -> dict:
    from . import prefs, usage_loop

    budgets = prefs.get_agent_budgets()
    names = _agent_names()
    limits = {}
    for engine, cfg in (budgets.get("engines") or {}).items():
        if engine in names and isinstance(cfg, dict):
            limit = _int(cfg.get("limit_tokens"), 0, 10**13)
            if limit:
                limits[names[engine]] = limit
    return _clean(
        {
            "alert_threshold_pct": _int(budgets.get("threshold_pct"), 1, 100),
            "alert_notify": _bool(budgets.get("notify")),
            "token_limits": limits,
            "sweep_interval_minutes": _int(usage_loop.INTERVAL_MINUTES, 1, 24 * 60),
            "sweep_enabled": _bool(usage_loop.loop_enabled()),
        }
    )


def _agents() -> dict:
    from . import engines, prefs

    defaults = prefs.get_agent_defaults()
    names = _agent_names()
    offered = sorted(
        names[p.engine_id]
        for p in engines.all_providers()
        if p.engine_id in names and engines.registry.can_start(p)
    )
    default = defaults.get("default_engine")
    return _clean(
        {
            "default_permission_bypass": _bool(defaults.get("bypass")),
            # A display name, or omitted: a stored default that is not a present agent is not news.
            "default_agent": names.get(default) if isinstance(default, str) else None,
            "agents_offered_for_new_sessions": offered,
        }
    )


def _policy(scope: str) -> dict:
    from . import prefs

    p = prefs.get_automation_policy(scope)
    verbs = p.get("allowed_verbs")
    return _clean(
        {
            "enabled": _bool(p.get("enabled")),
            "autonomy": _member(p.get("autonomy"), prefs.ORCH_TIERS),
            "allowed_verbs": sorted(v for v in verbs if v in prefs.ORCH_VERBS)
            if isinstance(verbs, list)
            else None,
        }
    )


def _automation() -> dict:
    from . import prefs

    return _clean(
        {
            "session_assistance": _policy("session"),
            "mission_orchestration": _policy("mission"),
            "unattended_permission_bypass_granted": _bool(prefs.unattended_bypass_granted()),
        }
    )


def _ai_review() -> dict:
    from . import prefs

    pub = prefs.public_ai_review()
    # ONLY these two. Never the model (an operator-typed free string), the URL, the key, the prompt
    # or headers (`ai-key-origin.md`).
    return _clean(
        {
            "enabled": _bool(pub.get("enabled")),
            "endpoint_configured": _bool(pub.get("configured")),
        }
    )


def _pulse() -> dict:
    from . import prefs

    pulse, review = prefs.get_pulse(), prefs.get_session_review()
    return _clean(
        {
            "background_scan": _bool(pulse.get("auto_enabled")),
            "scan_interval_minutes": _int(pulse.get("interval_minutes"), 1, 24 * 60),
            "window_days": _int(pulse.get("window_days"), 1, 365),
            "session_review_notify": _bool(review.get("notify")),
            "session_review_recognise_prompts": _bool(review.get("recognise_prompts")),
        }
    )


def _appearance() -> dict:
    from . import hq_contacts, prefs

    accent = prefs.get_accent()
    return _clean(
        {
            "theme": _member(prefs.get_theme(), prefs.THEMES),
            "accent": accent if isinstance(accent, str) and _ACCENT_RE.match(accent) else None,
            "hq_contact": _member(
                prefs.get_hq_contact(), (*hq_contacts.IDS, hq_contacts.OFF, "random")
            ),
            "terminal_font_size": _int(prefs.get_term_font_size(), 6, 40),
        }
    )


_BUILDERS: dict[str, Callable[[], dict]] = {
    "usage": _usage,
    "agents": _agents,
    "automation": _automation,
    "ai_review": _ai_review,
    "pulse": _pulse,
    "appearance": _appearance,
}


def read_settings(section: object) -> dict:
    """One section, as the allowlist above builds it. Blocking (prefs and the roster read files)."""
    if not isinstance(section, str) or section not in _BUILDERS:
        raise UnknownSection(f"section must be one of {', '.join(SECTIONS)}")
    return {"section": section, **_BUILDERS[section]()}
