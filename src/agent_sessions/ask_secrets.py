"""HQ and library secrets (#1786 P4): list them, store one, switch agent access — never a value.

HQ (a model) never sees, holds or forwards a secret's value. ``list_secrets`` reads what the
Variables page lists (``GET /api/variables``: names, kinds, whether a value is stored, agent
access, bound projects; the route never returns a value) as the asking operator. The three
operations are proposals the operator confirms on a card:

- ``secret.create`` stores a new text secret. Its value is NOT a model argument: the operator
  types it on the card (a write-only field, the ``value`` card input), and it travels only in the
  tap request and the in-process route call (`ask_operations._run` merges card inputs in memory).
  It is never staged, bound, stored in an Ask record or sidecar, logged or returned. A create
  never turns agent access on.
- ``secret.access_on`` WIDENS access: once confirmed, agents in the projects that bind the secret
  can request a private copy (`secrets_broker`). The operator approved HQ proposing it on #1786
  (comment 113807) as an exception to the never-list (`ask_library`), on these terms: it is only
  ever a card the operator taps, never confirmed by voice (`voice_calls._voice_operation`), never
  at any autonomy level; the card names the bound projects, and a project set that changed
  before the tap makes it ``stale``.
- ``secret.access_off`` narrows it again.

Ids are the 32-hex library ids, and only one ``list_secrets`` returned in THIS turn may be named
(`ask_orchestrator._Turn.secrets`).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from . import asgi_call
from .ask_library import LIST_MAX, Refused, _secrets, _short

if TYPE_CHECKING:
    from .ask_operations import Operation

# `ask_operations` builds its CATALOG from `secret_ops()` at import, so it is imported here only
# inside the functions that need it (never at module level: that would be a cycle).


def _ops():
    from . import ask_operations

    return ask_operations


def _digest(items) -> int:
    return _ops()._digest(items)


GROUP = "secret"
_KINDS = ("secret", "secret_file")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
#: The longest value the card accepts: the store's own cap (`template_vars.VALUE_MAX`).
VALUE_MAX = 2_000
DESCRIPTION_MAX = 200


# ---- gates ---------------------------------------------------------------------------------------


def secret_name(v: object) -> str:
    """A new secret's name, the store's own rule (lowercase, digits, underscores)."""
    if not isinstance(v, str) or not _NAME_RE.match(v):
        raise _ops().Refused(
            "secret_name: lowercase letters, numbers and underscores, starting with a letter "
            "(up to 32)"
        )
    return v


def description(v: object) -> str:
    if not isinstance(v, str) or "\n" in v or "\r" in v or "\0" in v:
        raise _ops().Refused("description is one line of text")
    return v.strip()[:DESCRIPTION_MAX]


def value_input(v: object) -> str:
    """The value the operator typed on the create card, checked by the store's own rules BEFORE
    the tap is accepted (a 422 leaves the card pending to type again; a refusal from the store
    would settle it `failed`, independent review of #1813). The words never repeat the value."""
    from .template_secrets import SECRET_MIN

    if not isinstance(v, str) or not v or len(v) > VALUE_MAX or "\0" in v:
        raise _ops().Refused(
            f"type the secret on the card: up to {VALUE_MAX} characters, not empty"
        )
    if v != v.strip():
        raise _ops().Refused("a secret cannot start or end with a space or a new line")
    if len(v) < SECRET_MIN:
        raise _ops().Refused(f"a secret is at least {SECRET_MIN} characters")
    return v


# ---- reads ---------------------------------------------------------------------------------------


async def _library(app, operator) -> list[dict]:
    r = await asgi_call.call(app, operator, "GET", "/api/variables", None)
    if r.status != 200 or not isinstance(r.body, dict) or r.truncated:
        raise Refused("the secret list could not be read just now", "not_available")
    return [
        v
        for v in r.body.get("variables") or []
        if isinstance(v, dict) and v.get("kind") in _KINDS and isinstance(v.get("id"), str)
    ]


async def _project_names(app, operator) -> dict[str, str]:
    r = await asgi_call.call(
        app, operator, "GET", "/api/projects", None, query="include_archived=1"
    )
    rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
    return {
        str(p["id"]): str(p.get("name") or p["id"])
        for p in rows or []
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    }


def _bound(v: dict) -> list[str]:
    return sorted(p for p in v.get("projects") or [] if isinstance(p, str))


async def list_secrets(app, operator) -> tuple[list[dict], dict]:
    """The library's secrets as the model may see them: never a value, never a path."""
    rows_in = await _library(app, operator)
    names = await _project_names(app, operator)
    secrets = await asyncio.to_thread(_secrets)
    rows, known = [], {}
    for v in rows_in[:LIST_MAX]:
        definition = v.get("definition") if isinstance(v.get("definition"), dict) else {}
        file = v.get("file") if isinstance(v.get("file"), dict) else None
        row = {
            "id": v["id"],
            "name": _short(v.get("name"), 64, secrets),
            "kind": "file" if v["kind"] == "secret_file" else "text",
            "stored": bool(v.get("set")),
            "needs_reentry": bool(v.get("needs_reentry")),
            "agent_access": bool(v.get("agent_access")),
            "projects": [_short(names.get(p, p), 120, secrets) for p in _bound(v)],
            "description": _short(definition.get("help") or "", DESCRIPTION_MAX, secrets),
        }
        if file is not None:
            row["file_name"] = _short(file.get("name"), 120, secrets)
        rows.append(row)
        known[v["id"]] = {"name": str(v.get("name") or "")}
    return rows, known


async def facts(op: Operation, target: str, app, operator) -> dict | None:
    """What an access card shows and binds: the secret's name, access, bound projects (named and
    as a set) and revision. None when the secret is gone."""
    for v in await _library(app, operator):
        if v["id"] != target:
            continue
        names = await _project_names(app, operator)
        bound = _bound(v)
        # Every recipient, never cut (Hermes on #1813): a card that cannot name them all is
        # refused at staging (`prepare`), so the tap never widens access to an unnamed project.
        shown = ", ".join(names.get(p, p) for p in bound)
        return {
            "name": str(v.get("name") or ""),
            "agent_access": bool(v.get("agent_access")),
            # Named only where the card names them (access on); the SET binds every card.
            **({"projects": shown} if op.id == "secret.access_on" else {}),
            "project_set": _digest(bound),
            "revision": _digest([repr(v.get("updated_at"))]),
        }
    return None


def _current(target: str, facts: dict) -> float:
    """The secret's revision as the route needs it, read at the tap: still the one the card's
    facts were read at, bound to the same projects, or the card is stale (`_Moved`) — never a
    PATCH at a newer revision or for projects the card did not name. (A binding a playbook
    deployment adds moves the project set, not the secret's revision.)"""
    from . import variables

    for v in variables.library()["variables"]:
        if v.get("id") == target:
            if _digest([repr(v.get("updated_at"))]) != facts.get("revision"):
                raise _ops()._Moved("it was saved elsewhere")
            if _digest(_bound(v)) != facts.get("project_set"):
                raise _ops()._Moved("the projects that can use it changed")
            return v["updated_at"]
    raise _ops()._Moved("it no longer exists")


# ---- operations ----------------------------------------------------------------------------------


def _create_route(target: str, f: dict, x: dict):
    body = {
        "name": f["secret_name"],
        "kind": "secret",
        "definition": {
            "value_type": "text",
            "label": f["secret_name"],
            "help": f.get("description") or "",
        },
        "value": f["value"],  # the card input, merged at the tap; agent access stays off
    }
    return "POST", "/api/variables", body


def _access_route(on: bool):
    def route(target: str, f: dict, x: dict):
        return (
            "PATCH",
            f"/api/variables/{target}",
            {"agent_access": on, "expected_updated_at": _current(target, x)},
        )

    return route


def _describe_create(f: dict, x: dict) -> str:
    return (
        f"Stores a new secret “{f['secret_name']}” in the Library. Type its value below: it goes "
        "straight to the encrypted store, and HQ never sees it. Agents get no access until you "
        "turn that on."
    )


def _describe_on(f: dict, x: dict) -> str:
    name = x.get("name") or "this secret"
    if not x.get("projects"):
        return (
            f"Turns agent access on for “{name}”. No project binds it yet, so no agent can "
            "request it until you add one in the Library."
        )
    return (
        f"Lets agents in {x['projects']} request a private copy of “{name}” through the secrets "
        "tool. An agent can read that copy and send it anywhere. Turning access off later "
        "deletes BattleLab's copies but cannot recall what an agent already read."
    )


def _describe_off(f: dict, x: dict) -> str:
    return (
        f"Refuses every later agent request for “{x.get('name') or 'this secret'}” and deletes "
        "the copies BattleLab placed. It cannot recall a copy an agent already read."
    )


def _created(status: int, body: object, x: dict) -> tuple[str, str]:
    if 200 <= status < 300:
        return _ops().DONE, "Stored. Agent access is off."
    return _ops()._plain(status, body, "")


def secret_ops() -> tuple[Operation, ...]:
    Operation = _ops().Operation  # noqa: N806 — the dataclass, imported late (see above)
    return (
        Operation(
            "secret.create",
            GROUP,
            "create",
            "Store secret",
            "Store",
            "reversible",
            "none",
            ("secret_name", "description"),
            _create_route,
            _describe_create,
            _created,
            lambda st, d, x: f"Not stored: {d or 'refused'}. Nothing changed.",
            inputs=("value",),
        ),
        Operation(
            "secret.access_on",
            GROUP,
            "access_on",
            "Turn agent access on",
            "Turn on",
            "external",
            GROUP,
            (),
            _access_route(True),
            _describe_on,
            lambda st, b, x: _ops()._plain(st, b, "Agent access is on."),
            lambda st, d, x: f"Not turned on: {d or 'refused'}. Access is still off.",
        ),
        Operation(
            "secret.access_off",
            GROUP,
            "access_off",
            "Turn agent access off",
            "Turn off",
            "reversible",
            GROUP,
            (),
            _access_route(False),
            _describe_off,
            lambda st, b, x: _ops()._plain(st, b, "Agent access is off."),
            lambda st, d, x: f"Not turned off: {d or 'refused'}. Access is still on.",
        ),
    )


async def prepare(action: object, args: dict, *, known: dict, app, operator) -> dict:
    """What `propose_secret` stages: the operation, its target, title and fields. Raises
    ``Refused``. A target must be one ``list_secrets`` returned in this turn."""
    op = _ops().by_action(GROUP, action) if isinstance(action, str) else None
    if op is None:
        raise Refused("action must be create, access_on or access_off", "bad_arguments")
    if "value" in args:
        raise Refused(
            "refused: HQ never carries a secret's value — the operator types it on the card",
            "never",
        )
    if op.id == "secret.create":
        try:
            name = secret_name(args.get("name"))
            fields = {"secret_name": name, "description": description(args.get("description", ""))}
        except _ops().Refused as e:
            raise Refused(str(e), "bad_arguments") from None
        if any(v.get("name") == name for v in await _library(app, operator)):
            raise Refused(f"refused: a secret named {name} already exists", "exists")
        return {"op": op, "target": "", "title": name, "fields": fields}
    tid = args.get("id")
    if not isinstance(tid, str) or not _ID_RE.match(tid) or tid not in known:
        raise Refused(
            "refused: use a secret id list_secrets returned in this turn", "unknown_target"
        )
    if op.id == "secret.access_on":
        now = await facts(op, tid, app, operator)
        if now is not None and len(_describe_on({}, now)) > _ops().SUMMARY_MAX:
            raise Refused(
                "refused: this secret is bound to more projects than one card can name — the "
                "operator can turn its access on in the Library",
                "too_many_projects",
            )
    return {"op": op, "target": tid, "title": known[tid].get("name") or tid, "fields": {}}


__all__ = [
    "GROUP",
    "description",
    "facts",
    "list_secrets",
    "prepare",
    "secret_name",
    "secret_ops",
    "value_input",
]
