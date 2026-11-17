"""HQ shows it on screen (#2102): the catalog `show` picks from, and the directive it resolves to.

`show` changes what ONE browser displays, the one that asked, and nothing else: no platform state,
no route, nothing outside the app. The model names a target from a closed catalog; this module
resolves it to a **directive**, a closed discriminated union with one member per operation:

    {"op": "open", "target": {"type", "id"}, "where": "full" | "map"}
    {"op": "go", "section"}
    {"op": "map_focus" | "map_minimize" | "map_close", "key"}
    {"op": "map_close_all" | "map_arrange"}

No path, no URL and no free text can be expressed: the web maps a directive to a route through
its own builders (`web/src/app/screenDirective.ts`, pinned against `SECTIONS` here), and an id is
accepted only when THIS turn's tools returned it. The directive is stored as a turn record (what
was ASKED for, never what happened: the outcome is the asking device's own and is not reported
back) and delivered once, to the stream or the call socket that asked.

Deliberately pure: this module imports nothing of the app. It has no route to call and no store
to write; the orchestrator writes the turn record through `ask_store.append` like every record
(`tests/test_ask_screen.py` pins the imports).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass

#: `show` calls a turn may make ("open these four sessions in the map" is one ask).
MAX_SHOWS_PER_TURN = 8

#: What `show` can put on screen (the tool's `what`).
WHATS = ("session", "mission", "artifact", "conversation", "section", "map")
#: The things an `open` directive names by id.
TARGET_TYPES = ("session", "mission", "artifact", "conversation")
#: Full screen, or (a session only) a window in the map.
WHERES = ("full", "map")
#: The map workspace's own commands (the tool's `action`, with `what: "map"`).
MAP_ACTIONS = ("focus", "minimize", "close", "close_all", "arrange")
#: …of which the ones that name a session's window.
MAP_KEY_ACTIONS = frozenset({"focus", "minimize", "close"})
#: Every directive member.
OPS = (
    "open",
    "go",
    "map_focus",
    "map_minimize",
    "map_close",
    "map_close_all",
    "map_arrange",
)

#: The app's sections outside Settings. Names, never paths: the web owns the route of each
#: (`SCREEN_SECTIONS` in `web/src/app/screenDirective.ts`; a test pins the two against each other).
APP_SECTIONS = (
    "dashboard",
    "hq",
    "missions",
    "dispatches",
    "sessions",
    "map",
    "templates",
    "variables",
    "memory",
    "artifacts",
    "automations",
    "skills",
    "specialists",
    "playbooks",
    "mission_plans",
    "gallery_playbooks",
    "gallery_skills",
    "settings",
)
#: The Settings pages, by the id the web's own list gives each (`web/src/routes/settingsTabs.ts`,
#: pinned by the same test). A section name is `settings.<id>`.
SETTINGS_PAGES = (
    "profile",
    "session-defaults",
    "projects",
    "appearance",
    "terminal-appearance",
    "api-appearance",
    "ai-endpoint",
    "ai-session-review",
    "ai-auto-sort",
    "ai-hq",
    "ai-prompts",
    "ai-activity",
    "agents",
    "agents-defaults",
    "security",
    "sharing",
    "peers",
    "artifacts",
    "updates",
    "analytics",
    "system",
    "resources",
    "maintenance",
    "about",
)
SETTINGS_PREFIX = "settings."
#: Every section `show` can go to.
SECTIONS = APP_SECTIONS + tuple(SETTINGS_PREFIX + page for page in SETTINGS_PAGES)

# The id shapes, restated so this module stays import-free; `tests/test_ask_screen.py` pins each
# against its owner (`ask_store`, `missions`, `artifact_store`).
SESSION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}:[A-Za-z0-9._:@-]{1,200}$")
MISSION_ID_RE = re.compile(r"^msn_[0-9a-f]{32}$")
ARTIFACT_ID_RE = re.compile(r"^[1-9][0-9]{3}(?:-[1-9][0-9]{3}){3}$")
CONVERSATION_ID_RE = re.compile(r"^ask_[0-9a-f]{32}$")
DIRECTIVE_ID_RE = re.compile(r"^sd_[0-9a-f]{16}$")
_TARGET_ID_RES = {
    "session": SESSION_KEY_RE,
    "mission": MISSION_ID_RE,
    "artifact": ARTIFACT_ID_RE,
    "conversation": CONVERSATION_ID_RE,
}

#: The arguments `show` declares. Anything else (a `path`, a `url`) refuses the call.
ARGUMENTS = frozenset({"what", "key", "mission_id", "artifact_id", "section", "where", "action"})


class Refused(Exception):
    """`show` named something it may not: the message is the model's, the code is stored."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Known:
    """The ids this turn may show: what its tools returned, and the conversation it runs in."""

    keys: Collection[str]
    mission_ids: Collection[str]
    artifact_ids: Collection[str]
    conversation: str


def new_id() -> str:
    """A directive's id: what the asking device applies at most once."""
    return f"sd_{uuid.uuid4().hex[:16]}"


def _known(args: dict, name: str, known: Collection[str], pattern: re.Pattern, what: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or value not in known or not pattern.match(value):
        raise Refused(
            f"refused: {name} must be {what} one of your tools returned in this turn",
            f"unknown_{what.split()[-1]}",
        )
    return value


def _session(args: dict, known: Known) -> str:
    key = args.get("key")
    if not isinstance(key, str) or key not in known.keys or not SESSION_KEY_RE.match(key):
        raise Refused(
            "refused: use a session key one of your tools returned in this turn, or one listed "
            "under known_sessions in the Commander's message",
            "unknown_key",
        )
    return key


def _only(args: dict, *allowed: str) -> None:
    extra = sorted(k for k in args if k not in ("what", *allowed))
    if extra:
        raise Refused(
            f"refused: {', '.join(extra)} does not apply to what: {args.get('what')!r}",
            "bad_arguments",
        )


def resolve(args: dict, known: Known) -> dict:
    """The directive `show`'s arguments name, without its id. Raises `Refused`: a target this
    turn's tools did not return, a path or a URL, an unknown section, `where: "map"` on anything
    but a session."""
    if not isinstance(args, dict):
        raise Refused("arguments are not a JSON object", "bad_arguments")
    unknown = sorted(str(k) for k in args if k not in ARGUMENTS)
    if unknown:
        # A path or a URL has no argument to travel in: the catalog is the whole vocabulary.
        raise Refused(
            f"refused: show takes no {', '.join(unknown)}; it opens a target by name or id, "
            "never a path or a URL",
            "bad_arguments",
        )
    what = args.get("what")
    if what not in WHATS:
        raise Refused(f"what must be one of {', '.join(WHATS)}", "bad_arguments")
    where = args.get("where", "full")
    if where not in WHERES:
        raise Refused("where must be full or map", "bad_arguments")
    if where == "map" and what != "session":
        raise Refused("refused: only a session has a window in the map", "bad_arguments")
    if what == "session":
        _only(args, "key", "where")
        return {
            "op": "open",
            "target": {"type": "session", "id": _session(args, known)},
            "where": where,
        }
    if what == "mission":
        _only(args, "mission_id", "where")
        mid = _known(args, "mission_id", known.mission_ids, MISSION_ID_RE, "a mission")
        return {"op": "open", "target": {"type": "mission", "id": mid}, "where": "full"}
    if what == "artifact":
        _only(args, "artifact_id", "where")
        aid = _known(args, "artifact_id", known.artifact_ids, ARTIFACT_ID_RE, "an artifact")
        return {"op": "open", "target": {"type": "artifact", "id": aid}, "where": "full"}
    if what == "conversation":
        # The one conversation a turn knows by id is its own: its page, from the panel or a call.
        _only(args, "where")
        if not CONVERSATION_ID_RE.match(known.conversation):
            raise Refused("refused: this conversation has no page", "unknown_conversation")
        return {
            "op": "open",
            "target": {"type": "conversation", "id": known.conversation},
            "where": "full",
        }
    if what == "section":
        _only(args, "section", "where")
        section = args.get("section")
        if not isinstance(section, str) or section not in SECTIONS:
            raise Refused(
                "refused: section must be one of the names in the tool's list, never a path or "
                "a URL",
                "unknown_section",
            )
        return {"op": "go", "section": section}
    _only(args, "action", "key", "where")
    action = args.get("action")
    if action not in MAP_ACTIONS:
        raise Refused(f"action must be one of {', '.join(MAP_ACTIONS)}", "bad_arguments")
    if action in MAP_KEY_ACTIONS:
        return {"op": f"map_{action}", "key": _session(args, known)}
    if "key" in args:
        raise Refused(f"refused: {action} takes no key", "bad_arguments")
    return {"op": f"map_{action}"}


def admit(rec: dict) -> dict:
    """A directive as it may be stored or sent: its closed fields and nothing else. Raises
    ValueError for one no caller should produce (the store boundary's rule: a bad structural
    field raises, it is never repaired)."""
    did, op = rec.get("id"), rec.get("op")
    if not (isinstance(did, str) and DIRECTIVE_ID_RE.match(did)):
        raise ValueError("malformed directive id")
    if op not in OPS:
        raise ValueError("unknown screen operation")
    out: dict = {"id": did, "op": op}
    if op == "open":
        target = rec.get("target")
        kind = target.get("type") if isinstance(target, dict) else None
        tid = target.get("id") if isinstance(target, dict) else None
        if kind not in TARGET_TYPES or not (
            isinstance(tid, str) and _TARGET_ID_RES[kind].match(tid)
        ):
            raise ValueError("malformed screen target")
        where = rec.get("where")
        if where not in WHERES or (where == "map" and kind != "session"):
            raise ValueError("malformed screen placement")
        out["target"] = {"type": kind, "id": tid}
        out["where"] = where
    elif op == "go":
        section = rec.get("section")
        if not isinstance(section, str) or section not in SECTIONS:
            raise ValueError("unknown section")
        out["section"] = section
    elif op in ("map_focus", "map_minimize", "map_close"):
        key = rec.get("key")
        if not (isinstance(key, str) and SESSION_KEY_RE.match(key)):
            raise ValueError("malformed session key")
        out["key"] = key
    return out


def session_of(directive: dict) -> str | None:
    """The session a directive names, if it names one."""
    if directive.get("op") == "open":
        target = directive["target"]
        return target["id"] if target["type"] == "session" else None
    return directive.get("key")


def spec_properties() -> dict:
    """`show`'s declared arguments, for its tool spec: the closed sets as enums."""
    return {
        "what": {"type": "string", "enum": list(WHATS)},
        "key": {
            "type": "string",
            "description": "a session key a tool returned (what: session; or map with focus, "
            "minimize or close)",
        },
        "mission_id": {"type": "string", "description": "a mission id a tool returned"},
        "artifact_id": {
            "type": "string",
            "description": "an artifact id artifacts_find returned in this turn",
        },
        "section": {"type": "string", "enum": list(SECTIONS)},
        "where": {
            "type": "string",
            "enum": list(WHERES),
            "description": "a session only: full screen (the default) or a window in the map",
        },
        "action": {"type": "string", "enum": list(MAP_ACTIONS)},
    }
