"""HQ's confirmed operations: a closed catalog of the UI's own routes (#1558 P1, #1559).

HQ (``ask_orchestrator``) PROPOSES an operation from :data:`CATALOG`; the operator CONFIRMS it on a
card; a server task then runs the SAME route the UI's button calls, in-process and as the
operator (:mod:`asgi_call`). Every guard is therefore the route's own.

**What the card showed is what runs.** Staging reads the target's facts the way the UI reads them
(the GET routes, as the operator) and binds operation, target, fields and facts into a keyed
digest stored on BOTH the card record and the sidecar entry. At the tap the entry must match its
card, and the facts are read again: a project renamed, or one with a different session count
since the card was shown, is ``stale`` — nothing runs. Method, path and body are never stored;
they are rebuilt here from the catalog, so an edited sidecar cannot redirect a tap.

**Outcomes are the route's own facts.** ``done`` for a clean 2xx; ``partly_done`` when the route
reports per-item failures (a project archive's ``counts.failed``, archive-older's ``skipped``);
``failed`` with the route's own ``detail`` — and never a blanket "nothing changed" where the route
may have acted before refusing (a session archive tears the runtime down first).

**Every operation is a server task**, tracked and drained on shutdown, so a long bulk archive never
holds a request. A ``deciding`` entry whose decide lock is free was abandoned by a stopped server:
``interrupted``, reported, never replayed (``ask_proposals.recover``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import asgi_call, ask_store

log = logging.getLogger(__name__)

KIND = "operation"
PENDING, DECIDING = "pending", "deciding"
DONE, PARTLY_DONE, FAILED = "done", "partly_done", "failed"
STALE, DISMISSED, INTERRUPTED = "stale", "dismissed", "interrupted"
#: The route answered 2xx but its result was too large to read back: it ran, what it did is
#: unknown — never reported as a failure (#1559 review).
UNCONFIRMED = "unconfirmed"
#: A move that committed but did not finish (#1786: the secret is in the Library, the chat copy
#: is not yet released). The ONE state confirmed again: an operation with its own idempotent
#: runner (`Operation.run`) picks up where it stopped.
UNSETTLED = "unsettled"
#: The card ended with NOTHING changed and nothing left to run. Two ways in: the route's own store
#: refused it at confirm (#1938: archiving built-in staff, a full pool; never used where the route
#: may have acted before refusing, which stays ``failed``), or it was staged ALREADY SETTLED (#2145:
#: an import whose files the Library can't store; it shows what was read and every reason, and
#: offers no Save and no Dismiss).
REFUSED = "refused"
STATES = frozenset(
    {
        PENDING,
        DECIDING,
        DONE,
        PARTLY_DONE,
        FAILED,
        STALE,
        DISMISSED,
        INTERRUPTED,
        UNCONFIRMED,
        UNSETTLED,
        REFUSED,
    }
)
UNCONFIRMED_SAID = (
    "It went through, but its result was too large to read back here, so HQ can't say what "
    "changed. Check the target."
)
RISKS = frozenset({"reversible", "destructive", "external"})

INTERRUPTED_REASON = (
    "BattleLab stopped while this ran. It may have run; check the target. It is not retried."
)
#: Text caps, all within the store boundary's.
TITLE_MAX = 120
TAG_MAX = 32
SUMMARY_MAX = 400
DAYS_MAX = 3650

#: Operations in flight in this process: ``{proposal id: task}``.
_TASKS: dict[str, asyncio.Task] = {}


class Refused(Exception):
    """A proposal the catalog will not stage; ``code`` is the tool error code."""

    def __init__(self, message: str, code: str = "bad_arguments"):
        super().__init__(message)
        self.code = code


# ---- field gates --------------------------------------------------------------------------------


def _one_line(value: object, name: str, cap: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name} must be a string")
    v = value.strip()
    if not v and not allow_empty:
        raise Refused(f"{name} must not be empty")
    if len(v) > cap:
        raise Refused(f"{name} is longer than {cap} characters")
    if any(c in v for c in "\r\n\t\x00"):
        raise Refused(f"{name} must be one line")
    return v


def _title(v: object) -> str:
    return _one_line(v, "title", TITLE_MAX)


def _tag(v: object) -> str:
    return _one_line(v, "tag", TAG_MAX, allow_empty=True)


def _color(v: object) -> str:
    from . import metadata

    raw = _one_line(v, "color", 7, allow_empty=True)
    try:
        return metadata.validate_color(raw)
    except ValueError as e:
        raise Refused(str(e)) from None


def _name(v: object) -> str:
    from . import projects

    raw = _one_line(v, "name", TITLE_MAX)
    try:
        return projects._validate_name(raw)
    except projects.ProjectError as e:
        raise Refused(str(e)) from None


def _days(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= DAYS_MAX:
        raise Refused(f"days must be a whole number from 1 to {DAYS_MAX}")
    return v


def _folder_input(v: object) -> str:
    """A folder the OPERATOR picked on the card (never model output): an absolute, normalised
    path. Where it may be is the routes' own rule (the picker is bounded to the home folder)."""
    import os

    if not isinstance(v, str) or not v.startswith("/") or len(v) > 4096:
        raise Refused("folder must be an absolute path")
    if any(c in v for c in "\x00\r\n") or ".." in v.split("/"):
        raise Refused("folder is not a plain path")
    return os.path.normpath(v)


def _existing_input(v: object) -> bool:
    """With a folder: ``true`` = the folder must still EXIST (a real directory, not a symlink) when
    it is saved — what a call's spoken suggestion promised (#1692). It only narrows."""
    if v is not True:
        raise Refused("existing must be true")
    return True


def _secret_value_input(v: object) -> object:
    from .ask_secrets import value_input

    return value_input(v)


def _secret_name(v: object) -> object:
    from .ask_secrets import secret_name

    return secret_name(v)


def _secret_file_input(v: object) -> object:
    from .ask_secrets import file_input

    return file_input(v)


def _description(v: object) -> object:
    from .ask_secrets import description

    return description(v)


#: Inputs only the operator supplies, on the card, with the confirm. ``value`` is a secret's
#: value (#1786 P4): it exists only in the tap request and the in-process route call.
INPUT_GATES: dict[str, Callable[[object], object]] = {
    "folder": _folder_input,
    "value": _secret_value_input,
    "file": _secret_file_input,
}
#: Optional with an input (``existing`` with ``folder``): never required, never alone — and only
#: on the operations whose route acts on it (`_if_dir`): anywhere else it would be accepted and
#: silently ignored (review of #1692).
OPTIONAL_INPUTS: dict[str, tuple[str, Callable[[object], object]]] = {
    "existing": ("folder", _existing_input)
}
_OPTIONAL_INPUT_OPS: dict[str, frozenset[str]] = {
    "existing": frozenset({"project.create", "project.add_folder"})
}


def _instruction(v: object) -> str:
    from . import ask_mission

    return ask_mission.instruction(v)


def _relay_text(v: object) -> str:
    from . import ask_mission

    return ask_mission.relay_text(v)


GATES: dict[str, Callable[[object], object]] = {
    "title": _title,
    "tag": _tag,
    "color": _color,
    "name": _name,
    "days": _days,
    "secret_name": _secret_name,
    "description": _description,
    "instruction": _instruction,
    "text": _relay_text,
}


# ---- facts: what the card shows, read the way the UI reads it ----------------------------------


async def _session_facts(app, operator: asgi_call.Operator, key: str) -> dict | None:
    from . import engines
    from .engines import registry

    r = await asgi_call.call(app, operator, "GET", f"/api/sessions/{key}", None)
    if r.status != 200 or not isinstance(r.body, dict):
        return None
    row = r.body
    runtime = "terminal"
    with contextlib.suppress(Exception):
        prov, _native = engines.parse_key(key)
        runtime = str(getattr(registry.manifest_of(prov), "runtime", "") or "terminal")
    project = row.get("project") if isinstance(row.get("project"), dict) else {}
    return {
        "title": str(row.get("title") or "")[:TITLE_MAX],
        "archived": bool(row.get("archived")),
        "runtime": runtime,
        "project": str(project.get("name") or "")[:TITLE_MAX],
    }


async def _project_name(app, operator: asgi_call.Operator, pid: str) -> str:
    """A project's name as the UI lists it, or "" when it is gone."""
    r = await asgi_call.call(
        app, operator, "GET", "/api/projects", None, query="include_archived=1"
    )
    rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
    for p in rows or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return str(p.get("name") or "")[:TITLE_MAX]
    return ""


def _digest(items) -> int:
    """A set's identity as a bounded int (a fact is a str, int or bool, and a hex string would
    read as credential-shaped): the card binds WHICH members, not only how many (#1559 review)."""
    # A JSON array, not a joined string: a path may hold a newline, and ["/a", "/b\n/c"] must not
    # read as ["/a\n/b", "/c"] (#1571 review 6742).
    h = hashlib.sha256(json.dumps(sorted(items), ensure_ascii=False).encode()).hexdigest()
    return int(h, 16) % 10**12


def _member_keys(pid: str) -> list[str]:
    """The keys a project archive / unarchive will walk — the route's own resolver (a FRESH,
    unscoped scan), never the scoped, cached ``session_count`` the sidebar shows."""
    from . import projects
    from .routes.sessions import project_members

    return [key for _s, key, _m in project_members(pid, projects.load())]


async def _project_facts(app, operator: asgi_call.Operator, pid: str) -> dict | None:
    r = await asgi_call.call(
        app, operator, "GET", "/api/projects", None, query="include_archived=1"
    )
    rows = (r.body or {}).get("projects") if isinstance(r.body, dict) else None
    for p in rows or []:
        if isinstance(p, dict) and p.get("id") == pid:
            members = await asyncio.to_thread(_member_keys, pid)
            folders = [f for f in p.get("folders") or [] if isinstance(f, str)]
            return {
                "name": str(p.get("name") or "")[:TITLE_MAX],
                "archived": bool(p.get("archived")),
                "sessions": len(members),
                "members": _digest(members),
                "color": str(p.get("color") or ""),
                "folders": len(folders),
                "folder_set": _digest(folders),
            }
    return None


def _older_facts(days: int) -> dict:
    """What archive-older would sweep NOW: the route's own candidates (a fresh scan), counted
    and bound by identity."""
    from . import engines
    from .routes.sessions import archive_older_candidates

    keys = [engines.session_key(s) for s in archive_older_candidates(time.time() - days * 86400.0)]
    return {"matched": len(keys), "members": _digest(keys)}


# ---- outcomes -----------------------------------------------------------------------------------


def _plain(status: int, body: object, said: str) -> tuple[str, str]:
    return (DONE, said) if 200 <= status < 300 else (FAILED, "")


def _bulk_project(status: int, body: object, verb: str) -> tuple[str, str]:
    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    counts = body.get("counts") if isinstance(body.get("counts"), dict) else {}
    failed = int(counts.get("failed") or 0)
    members = [m for m in body.get("sessions") or [] if isinstance(m, dict)]
    total = len(members)
    if failed:
        reasons = sorted(
            {str(m.get("reason") or "")[:80] for m in members if m.get("result") == "failed"} - {""}
        )
        why = f": {'; '.join(reasons[:3])}" if reasons else ""
        return PARTLY_DONE, f"{total - failed} of {total} sessions {verb}; {failed} not{why}."
    return DONE, f"Project {verb}, with its {total} session{'s' if total != 1 else ''}."


def _older(status: int, body: object) -> tuple[str, str]:
    if not 200 <= status < 300 or not isinstance(body, dict):
        return FAILED, ""
    archived, skipped = int(body.get("archived") or 0), int(body.get("skipped") or 0)
    if skipped:
        # Counts only: the route reports how many it skipped, not which — never guessed here.
        return (
            PARTLY_DONE,
            f"{archived} archived; {skipped} could not be. Open Sessions to see which.",
        )
    return DONE, f"{archived} session{'s' if archived != 1 else ''} archived."


# ---- the catalog --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    id: str
    group: str  # session, project, playbook, skill, automation, specialist, secret, team, …
    action: str  # the propose tool's `action` value
    label: str  # the card's Action row
    verb: str  # the card's button
    risk: str
    target: str  # "session" | "project" | "playbook" | "skill" | "automation" | "none"
    fields: tuple[str, ...]
    #: (target, fields, facts) → (method, path, body)
    route: Callable[[str, dict, dict], tuple[str, str, dict | None]]
    #: (fields, facts) → the card's summary sentence
    describe: Callable[[dict, dict], str]
    #: (status, body, facts) → (state, the outcome sentence)
    outcome: Callable[[int, object, dict], tuple[str, str]]
    #: (status, detail, facts) → the failed sentence — honest about what may already have happened
    failure: Callable[[int, str, dict], str] = field(
        default=lambda status, detail, facts: f"{detail or 'It did not go through'}. Nothing "
        "changed."
    )
    #: What only the operator supplies, on the card, with the confirm (e.g. a picked folder).
    inputs: tuple[str, ...] = ()
    #: The operation saves a DRAFT HQ wrote (#1611): its content lives in its own file
    #: (`ask_store.write_draft`), bound to the card by its sha256, and reaches the route as
    #: `fields["_content"]` only after that sha256 matched again at the tap.
    draft: bool = False
    #: Several route calls with their own order (#1786's custody-transfer move), instead of
    #: ``route``: ``async (cid, target, fields, facts, app, operator) → {"state", "outcome"}``.
    #: Idempotent: an ``unsettled`` card is confirmed again and the runner resumes.
    run: Callable | None = None
    #: A ``done`` card starts ONE continuation turn in its conversation (#1950: HQ continues after
    #: the draft-mission card, `ask_orchestrator.continue_after_card`). Off for every other card.
    follow_up: bool = False
    #: ``route`` builds the request from the facts the card was STAGED with (what it showed and
    #: bound), not the ones re-read at the tap (#2129: a team card sends the team digest it bound,
    #: and a card staged without one sends none). The re-read still decides ``stale`` first.
    staged_facts: bool = False
    #: The card's button when it names something of THIS card (#1938: "Save as version 4"):
    #: ``(fields, facts) → the verb``. ``verb`` is the catalog's own word otherwise.
    verb_of: Callable[[dict, dict], str] | None = None
    #: Why a card whose facts moved before the tap is out of date, as the card says it (#1938):
    #: ``(why, row) → the reason``. The catalog's generic sentence otherwise.
    stale: Callable[[str, dict], str] | None = None


class _Moved(Exception):
    """The target changed between the facts check and building the request: stale, nothing ran."""


def _folder_patch(pid: str, facts: dict, *, add: str = "", remove: str = "", default: str = ""):
    """The PATCH body for a folder change, read at the tap (the UI's chips send the whole list).

    The list must still be the set the card bound (``folder_set``); a folder picked from the
    project's own must still be one of them (a default the project no longer has would be
    re-adopted by the store); and ``if_folders`` makes the route refuse (409) if the set moves
    before its lock — never a lost concurrent change."""
    from . import projects

    entity = projects.load().get(pid)
    if entity is None:
        raise _Moved("it no longer exists")
    current = list(entity.folders)
    if _digest(current) != facts.get("folder_set"):
        raise _Moved("its folders changed")
    picked = remove or default
    if picked and picked not in current:
        raise _Moved("that folder is no longer one of its folders")
    body: dict = {"if_folders": current}
    if add or remove:
        grown = current + ([add] if add and add not in current else [])
        body["folders"] = [f for f in grown if f != remove]
    if default:
        body["default_folder"] = default
    return body


def _if_dir(body: dict, fields: dict) -> dict:
    """A folder confirmed as EXISTING (a call's suggestion, #1692) must still be a real directory
    when the store saves it — checked under the store's lock (``projects._expect_dir``)."""
    return {**body, "if_dir": fields["folder"]} if fields.get("existing") else body


def _may_have_stopped(status: int, detail: str, facts: dict) -> str:
    # The archive routes tear the runtime down BEFORE a later refusal (routes/sessions.py).
    return f"Not archived: {detail or 'refused'}. Its agent may already have been stopped."


def _close_route(key: str, f: dict, facts: dict) -> tuple[str, str, dict | None]:
    if facts.get("runtime") == "api":
        return "POST", f"/api/structured/sessions/{key}/stop", None
    return "POST", f"/api/sessions/{key}/archive", None


def _ops() -> tuple[Operation, ...]:
    s, p = "session", "project"
    return (
        Operation(
            "session.archive",
            s,
            "archive",
            "Archive",
            "Archive",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/archive", None),
            lambda f, x: "Stops its agent and moves it to the archive. You can unarchive it "
            "later; its history is kept.",
            lambda st, b, x: _plain(st, b, "Archived."),
            _may_have_stopped,
        ),
        Operation(
            "session.unarchive",
            s,
            "unarchive",
            "Unarchive",
            "Unarchive",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/unarchive", None),
            lambda f, x: "Brings it back from the archive. Opening it relaunches from its "
            "history.",
            lambda st, b, x: _plain(st, b, "Unarchived."),
        ),
        Operation(
            "session.close",
            s,
            "close",
            "Close",
            "Close",
            "reversible",
            s,
            (),
            _close_route,
            lambda f, x: "Stops its API worker; the conversation is kept."
            if x.get("runtime") == "api"
            else "Stops its agent and archives it (a terminal session closes by archiving).",
            lambda st, b, x: _plain(
                st, b, "Worker stopped." if x.get("runtime") == "api" else "Archived."
            ),
            lambda st, d, x: f"Not stopped: {d or 'refused'}."
            if x.get("runtime") == "api"
            else _may_have_stopped(st, d, x),
        ),
        Operation(
            "session.rename",
            s,
            "rename",
            "Rename",
            "Rename",
            "reversible",
            s,
            ("title",),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/rename", {"title": f["title"]}),
            lambda f, x: f"Renames it to “{f['title']}”.",
            lambda st, b, x: _plain(st, b, "Renamed."),
        ),
        Operation(
            "session.tag",
            s,
            "tag",
            "Tag",
            "Set tag",
            "reversible",
            s,
            ("tag",),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/tag", {"tag": f["tag"]}),
            lambda f, x: f"Sets its tag to “{f['tag']}”." if f["tag"] else "Clears its tag.",
            lambda st, b, x: _plain(st, b, "Tag set."),
        ),
        Operation(
            "session.color",
            s,
            "color",
            "Colour",
            "Set colour",
            "reversible",
            s,
            ("color",),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/color", {"color": f["color"]}),
            lambda f, x: f"Sets its colour to {f['color']}."
            if f["color"]
            else "Clears its " "colour.",
            lambda st, b, x: _plain(st, b, "Colour set."),
        ),
        Operation(
            "session.favorite",
            s,
            "favorite",
            "Favourite",
            "Favourite",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/favorite", None),
            lambda f, x: "Pins it to the top of your sessions.",
            lambda st, b, x: _plain(st, b, "Favourited."),
        ),
        Operation(
            "session.unfavorite",
            s,
            "unfavorite",
            "Unfavourite",
            "Unfavourite",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/unfavorite", None),
            lambda f, x: "Unpins it.",
            lambda st, b, x: _plain(st, b, "Unfavourited."),
        ),
        Operation(
            "session.move",
            s,
            "move",
            "Move to project",
            "Move",
            "reversible",
            s,
            ("project_id", "project_name"),
            lambda k, f, x: (
                "PATCH",
                f"/api/sessions/{k}/metadata",
                {"project_id": f["project_id"] or None},
            ),
            lambda f, x: f"Moves it to {x.get('dest') or f['project_name']}."
            if f["project_id"]
            else "Takes it " "out of its project (back to folder grouping).",
            lambda st, b, x: _plain(st, b, "Moved."),
        ),
        Operation(
            "session.review",
            s,
            "review",
            "Review now",
            "Review now",
            "external",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/review", None),
            lambda f, x: "Sends its recent transcript to your AI endpoint for a new summary and "
            "title.",
            lambda st, b, x: _plain(st, b, "Reviewed."),
        ),
        Operation(
            "session.review_exclude",
            s,
            "exclude_review",
            "Exclude from AI review",
            "Exclude",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/review-exclude", {"excluded": True}),
            lambda f, x: "AI review skips it from now on.",
            lambda st, b, x: _plain(st, b, "Excluded from AI review."),
        ),
        Operation(
            "session.review_include",
            s,
            "include_review",
            "Include in AI review",
            "Include",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/review-exclude", {"excluded": False}),
            lambda f, x: "AI review covers it again.",
            lambda st, b, x: _plain(st, b, "Included in AI review."),
        ),
        Operation(
            "session.orchestration_exclude",
            s,
            "exclude_orchestration",
            "Stop mission control managing it",
            "Exclude",
            "reversible",
            s,
            (),
            lambda k, f, x: ("POST", f"/api/sessions/{k}/orchestrator-exclude", {"excluded": True}),
            lambda f, x: "Mission control stops proposing anything for it.",
            lambda st, b, x: _plain(st, b, "Excluded from mission control."),
        ),
        Operation(
            "session.orchestration_include",
            s,
            "include_orchestration",
            "Let mission control manage it",
            "Include",
            "reversible",
            s,
            (),
            lambda k, f, x: (
                "POST",
                f"/api/sessions/{k}/orchestrator-exclude",
                {"excluded": False},
            ),
            lambda f, x: "Mission control may propose actions for it again.",
            lambda st, b, x: _plain(st, b, "Included in mission control."),
        ),
        Operation(
            "sessions.archive_older",
            s,
            "archive_older",
            "Archive older sessions",
            "Archive",
            "reversible",
            "none",
            ("days",),
            lambda k, f, x: ("POST", "/api/sessions/archive-older", {"hours": f["days"] * 24}),
            lambda f, x: f"Archives every active session idle for more than {f['days']} "
            f"day{'s' if f['days'] != 1 else ''} ({x.get('matched', 0)} now). Each can be "
            "unarchived.",
            lambda st, b, x: _older(st, b),
            lambda st, d, x: f"Not archived: {d or 'refused'}. Some agents may already have been "
            "stopped.",
        ),
        Operation(
            "project.rename",
            p,
            "rename",
            "Rename project",
            "Rename",
            "reversible",
            p,
            ("name",),
            lambda k, f, x: ("PATCH", f"/api/projects/{k}", {"name": f["name"]}),
            lambda f, x: f"Renames {x.get('name', 'it')} to “{f['name']}”.",
            lambda st, b, x: _plain(st, b, "Renamed."),
        ),
        Operation(
            "project.color",
            p,
            "color",
            "Project colour",
            "Set colour",
            "reversible",
            p,
            ("color",),
            lambda k, f, x: ("PATCH", f"/api/projects/{k}", {"color": f["color"]}),
            lambda f, x: f"Sets its colour to {f['color']}."
            if f["color"]
            else "Clears its " "colour.",
            lambda st, b, x: _plain(st, b, "Colour set."),
        ),
        Operation(
            "project.archive",
            p,
            "archive",
            "Archive project",
            "Archive",
            "reversible",
            p,
            (),
            lambda k, f, x: ("POST", f"/api/projects/{k}/archive", None),
            lambda f, x: f"Archives the project and its {x.get('sessions', 0)} session"
            f"{'s' if x.get('sessions', 0) != 1 else ''}, stopping their agents. Unarchive "
            "brings them back.",
            lambda st, b, x: _bulk_project(st, b, "archived"),
            lambda st, d, x: f"Not archived: {d or 'refused'}.",
        ),
        Operation(
            "project.unarchive",
            p,
            "unarchive",
            "Unarchive project",
            "Unarchive",
            "reversible",
            p,
            (),
            lambda k, f, x: ("POST", f"/api/projects/{k}/unarchive", None),
            lambda f, x: f"Brings the project and its {x.get('sessions', 0)} session"
            f"{'s' if x.get('sessions', 0) != 1 else ''} back from the archive.",
            lambda st, b, x: _bulk_project(st, b, "unarchived"),
        ),
        Operation(
            "project.delete",
            p,
            "delete",
            "Delete project",
            "Delete project",
            "destructive",
            p,
            (),
            lambda k, f, x: ("DELETE", f"/api/projects/{k}", None),
            lambda f, x: f"Removes the project and its shared memory. Its {x.get('sessions', 0)} "
            f"session{'s are' if x.get('sessions', 0) != 1 else ' is'} kept and go back to folder "
            "grouping. Its shared memory, name, colour and launch defaults are lost.",
            lambda st, b, x: _plain(st, b, "Project deleted. Its sessions were kept."),
            # `projects.delete` removes the memory FIRST: a later failure may leave it gone.
            lambda st, d, x: f"Not deleted: {d or 'refused'}. Its shared memory may already have "
            "been removed.",
        ),
        Operation(
            "project.default",
            p,
            "make_default",
            "Default for new sessions",
            "Make default",
            "reversible",
            p,
            (),
            lambda k, f, x: ("POST", "/api/prefs", {"default_project_id": k}),
            lambda f, x: f"New sessions start in {x.get('name', 'this project')} unless you pick "
            "another.",
            lambda st, b, x: _plain(st, b, "Default project set."),
        ),
        Operation(
            "project.create",
            p,
            "create",
            "Create project",
            "Create project",
            "reversible",
            "none",
            ("name",),
            lambda k, f, x: (
                "POST",
                "/api/projects",
                _if_dir(
                    {"name": f["name"], "folders": [f["folder"]], "default_folder": f["folder"]}, f
                ),
            ),
            lambda f, x: f"Creates the project “{f['name']}” in the folder you choose below.",
            lambda st, b, x: _plain(st, b, "Project created."),
            inputs=("folder",),
        ),
        Operation(
            "project.add_folder",
            p,
            "add_folder",
            "Add a folder",
            "Add folder",
            "reversible",
            p,
            (),
            lambda k, f, x: (
                "PATCH",
                f"/api/projects/{k}",
                _if_dir(_folder_patch(k, x, add=f["folder"]), f),
            ),
            lambda f, x: f"Adds the folder you choose below to {x.get('name', 'the project')}; its "
            "sessions group under the project.",
            lambda st, b, x: _plain(st, b, "Folder added."),
            inputs=("folder",),
        ),
        Operation(
            "project.remove_folder",
            p,
            "remove_folder",
            "Release a folder",
            "Release folder",
            "reversible",
            p,
            (),
            lambda k, f, x: (
                "PATCH",
                f"/api/projects/{k}",
                _folder_patch(k, x, remove=f["folder"]),
            ),
            lambda f, x: "Releases the folder you choose below from "
            f"{x.get('name', 'the project')}; its sessions go back to folder grouping.",
            lambda st, b, x: _plain(st, b, "Folder released."),
            inputs=("folder",),
        ),
        Operation(
            "project.default_folder",
            p,
            "default_folder",
            "Default launch folder",
            "Set default folder",
            "reversible",
            p,
            (),
            lambda k, f, x: (
                "PATCH",
                f"/api/projects/{k}",
                _folder_patch(k, x, default=f["folder"]),
            ),
            lambda f, x: f"New sessions in {x.get('name', 'the project')} start in the folder you "
            "choose below.",
            lambda st, b, x: _plain(st, b, "Default folder set."),
            inputs=("folder",),
        ),
        Operation(
            "projects.auto_sort",
            p,
            "auto_sort",
            "Sort sessions into projects",
            "Sort now",
            "external",
            "none",
            (),
            lambda k, f, x: ("POST", "/api/projects/auto-sort", None),
            lambda f, x: "Your AI endpoint assigns unassigned sessions to projects, by the "
            "auto-sort settings.",
            lambda st, b, x: _plain(st, b, "Sorted."),
        ),
    )


def _all_ops() -> tuple[Operation, ...]:
    from .ask_library import library_ops
    from .ask_mcp import mcp_ops
    from .ask_memory import operations as memory_ops
    from .ask_mission import mission_ops
    from .ask_secrets import secret_ops
    from .ask_specialists import specialist_ops
    from .ask_team import team_ops

    return (
        *_ops(),
        *library_ops(),
        *specialist_ops(),
        *secret_ops(),
        *team_ops(),
        *mission_ops(),
        *memory_ops(),
        *mcp_ops(),
    )


CATALOG: dict[str, Operation] = {op.id: op for op in _all_ops()}


def by_action(group: str, action: str) -> Operation | None:
    return next((o for o in CATALOG.values() if o.group == group and o.action == action), None)


def actions(group: str) -> list[str]:
    return sorted(o.action for o in CATALOG.values() if o.group == group)


# ---- facts, binding -----------------------------------------------------------------------------


async def facts_for(
    op: Operation, target: str, fields: dict, app, operator, *, cid: str = ""
) -> dict | None:
    """The target's facts as the card shows them, or None when the target is gone. ``cid`` is the
    conversation the card is in (a shared file's facts, #1786)."""
    if op.target == "session":
        facts = await _session_facts(app, operator, target)
        if facts is not None and op.id == "session.move" and fields.get("project_id"):
            # The card names its destination: bind it, so a project renamed or removed before
            # the tap is `stale`, never a move to a project the card did not show (review 6742).
            facts["dest"] = await _project_name(app, operator, fields["project_id"])
            if not facts["dest"]:
                return None  # a destination already gone stages nothing
        return facts
    if op.group == "mission":  # a draft mission's project (#1950), or the mission HQ plans (#2152)
        from . import ask_mission

        return await ask_mission.facts(op.id, target, fields, app, operator)
    if op.group == "mcp":  # a project MCP server (#2163 P2)
        from . import ask_mcp

        return await ask_mcp.facts(op, target, fields, app, operator)
    if op.id == "secret.create_project":  # a project's own secret: the project it names
        from . import ask_secrets

        return await ask_secrets.project_facts(fields, app, operator)
    if op.group == "memory":
        from . import ask_memory

        return await ask_memory.facts(target, app, operator)
    if op.target == "project":
        return await _project_facts(app, operator, target)
    if op.id == "sessions.archive_older":
        return await asyncio.to_thread(_older_facts, int(fields["days"]))
    if op.group in ("playbook", "skill", "automation"):
        from . import ask_library

        return await ask_library.facts(op, target, app, operator)
    if op.group == "specialist":  # a specialist's revision, version and archive state (#1938)
        from . import ask_specialists

        return await ask_specialists.facts(op, target, app, operator)
    if op.target == "secret":
        from . import ask_secrets

        return await ask_secrets.facts(op, target, app, operator)
    if op.group == "team":  # a mission's team (#1950): title, plan revision, the bound team and
        # its rows (#2126, #2130)
        from . import ask_team

        return await ask_team.facts(target, fields, app, operator)
    if op.target == "attachment":
        from . import ask_secrets

        return await asyncio.to_thread(ask_secrets.attachment_facts, cid, target)
    return {}


def lift_display(facts: dict | None) -> tuple[dict | None, dict | None]:
    """`(facts, display)`: an operation's display-only payload (#2130: an `assemble` card's team
    rows) split off its facts. It is stored on the entry beside them, never in them, so it never
    binds, is never compared at the tap (`changed`) and is never sent with the request."""
    if not isinstance(facts, dict) or "display" not in facts:
        return facts, None
    facts = dict(facts)
    display = facts.pop("display")
    return facts, display if isinstance(display, dict) else None


def binding(op_id: str, target: str, fields: dict, facts: dict, draft: str = "") -> str:
    """The keyed digest that ties a card to its entry — and, for a library save, to the sha256 of
    the draft it saves (#1611). Fails closed (StoreRefused)."""
    from . import template_secrets

    bound: dict = {"op": op_id, "target": target, "fields": fields, "facts": facts}
    if draft:
        bound["draft"] = draft
    canon = json.dumps(bound, sort_keys=True, ensure_ascii=False)
    try:
        return template_secrets.keyed_digest("ask-operation", canon)
    except template_secrets.SecretKeyUnavailable as e:
        raise ask_store.StoreRefused(f"{e} — nothing was staged") from None


def changed(before: dict, now: dict | None) -> str:
    """Why the facts the card showed no longer hold, or ""."""
    if now is None:
        return "it no longer exists"
    for k, label in (
        ("name", "it was renamed"),
        ("title", "it was renamed"),
        ("archived", "its archive state changed"),
        ("runtime", "its agent changed"),
    ):
        if k in before and before.get(k) != now.get(k):
            return label
    if "sessions" in before and before["sessions"] != now.get("sessions"):
        return f"it now has {now.get('sessions')} sessions, not the {before['sessions']} shown"
    if "matched" in before and before["matched"] != now.get("matched"):
        return f"{now.get('matched')} sessions match now, not the {before['matched']} shown"
    # Same count, different members: the card previewed other sessions than it would act on.
    if "dest" in before and before["dest"] != now.get("dest"):
        return "the destination project changed" if now.get("dest") else "its destination is gone"
    if "members" in before and before["members"] != now.get("members"):
        return "the sessions it covers changed"
    if "folders" in before and before["folders"] != now.get("folders"):
        return "its folders changed"
    if "folder_set" in before and before["folder_set"] != now.get("folder_set"):
        return "its folders changed"
    # A library target (#1611): saved elsewhere, switched on or off, or its scope moved.
    # Switching on or off also moves the revision: name the more specific change first.
    # A secret (#1786): its agent access, or the projects whose agents it would reach.
    if "agent_access" in before and before["agent_access"] != now.get("agent_access"):
        return "its agent access was switched elsewhere"
    if "project_set" in before and before["project_set"] != now.get("project_set"):
        return "the projects that can use it changed"
    if "enabled" in before and before["enabled"] != now.get("enabled"):
        return "it was switched on" if now.get("enabled") else "it was switched off"
    # An assemble card's picks (#2126): the team it would write now is not the one it showed.
    if "revision" in before and before["revision"] != now.get("revision"):
        return "it was saved elsewhere after HQ read it"
    if "team" in before and before["team"] != now.get("team"):
        return "the team it would assemble changed"
    # A draft mission's procedure (#1950): the deployed playbook's listing it was chosen from.
    if ("listing" in before and before["listing"] != now.get("listing")) or (
        "flow" in before and before["flow"] != now.get("flow")
    ):
        return "the project's deployed playbook changed"
    # The existing draft a `mission.plan` card plans (#2152): still a draft, still without a plan.
    if "planned" in before and before["planned"] != now.get("planned"):
        return "it was planned elsewhere"
    if "state" in before and before["state"] != now.get("state"):
        return f"it is {now.get('state')} now"
    # An MCP server's install card (#2163): the source the package review was run for.
    if "source_rev" in before and before["source_rev"] != now.get("source_rev"):
        return "its source changed"
    # A project's own credential (#2163): the project set that name elsewhere before the tap.
    if "holds" in before and before["holds"] != now.get("holds"):
        return "the project set that secret elsewhere"
    if "scope" in before and before["scope"] != now.get("scope"):
        return "what it would do changed"
    if "held_now" in before and before["held_now"] != now.get("held_now"):
        return "the mission no longer holds that session"
    if "reach" in before and before["reach"] != now.get("reach"):
        return "what it would do changed"  # run now's class moved (#2391)
    return ""


def gate_fields(op: Operation, raw: dict) -> dict:
    """The operation's typed fields, each through its gate. Fields it doesn't take are refused."""
    out: dict = {}
    for name in op.fields:
        if name in ("project_id", "project_name", "procedure", "session_key"):
            continue  # resolved by the tool from this turn's list, never model text
        if name not in raw:
            raise Refused(f"{op.action} needs {name}")
        out[name] = GATES[name](raw[name])
    return out


# ---- view ---------------------------------------------------------------------------------------


#: Operations whose folder the operator picks from the PROJECT's own folders, not the filesystem.
_PICK_FROM_PROJECT = frozenset({"project.remove_folder", "project.default_folder"})


def _folder_choices(row: dict) -> list[str]:
    """The project's folders, for the card's own picker (the operator's browser only — this
    view never reaches the model). Empty when the project is gone or unreadable."""
    from . import projects

    with contextlib.suppress(Exception):
        return list(projects.load()[row["target"]].folders)
    return []


#: Operations whose browsed-for folder a call may SUGGEST from the project's name (#1681, the
#: operator's decision): an EXISTING folder named after the project under the configured project
#: root. A new folder is never made by voice (the operator's option A); it is picked on screen.
_SUGGEST_FROM_NAME = frozenset({"project.create", "project.add_folder"})


def folder_slug(name: object) -> str:
    """A project name as one folder name: lowercase, runs of anything but letters, digits, dots
    and dashes become one dash ("Web App" → "web-app"). ``""`` when nothing is left."""
    import re

    if not isinstance(name, str):
        return ""
    return re.sub(r"[^a-z0-9.-]+", "-", name.strip().lower()).strip("-.")


def suggested_folder(pid: str) -> dict | None:
    """The folder a call suggests for a PENDING create-project or add-folder card: the first
    configured project root (`project_dirs`) plus the project's name as one folder name — only if
    that folder EXISTS already (a real directory, not a symlink). ``None`` with no root
    configured, a name that makes no folder name, no such folder, or (adding a folder) one the
    project already has. Server-side only: the model never writes the path. Blocking."""
    import os

    from . import ask_proposals, prefs, project_dirs, projects

    cid = ask_proposals.locate(pid)
    row = ask_proposals._row(cid, pid) if cid else None
    if row is None or row.get("state") != PENDING or row.get("op") not in _SUGGEST_FROM_NAME:
        return None
    roots = project_dirs.project_roots()
    if not roots:
        return None
    current: list[str] = []
    if row["op"] == "project.create":
        name = (row.get("fields") or {}).get("name")
    else:
        project = projects.load().get(str(row.get("target") or ""))
        if project is None:
            return None
        name, current = project.name, list(project.folders)
    slug = folder_slug(name)
    if not slug or not project_dirs._valid_name(slug):
        return None
    folder = os.path.join(roots[0], slug)
    try:
        folder = _folder_input(folder)
    except Refused:
        return None
    if folder in current or os.path.islink(folder) or not os.path.isdir(folder):
        return None  # already its own; a link; or not there — a new folder is picked on screen
    # A folder another project owns — exactly, nested or above — the store refuses (#361), so it
    # is never suggested; nor one under an excluded prefix (#1692 review).
    target = str(row.get("target") or "")
    for oid, other in projects.load().items():
        if oid != target and any(projects._nested(folder, f) for f in other.folders):
            return None
    if any(project_dirs.path_within(folder, e) for e in prefs.get_folder_exclusions()):
        return None
    return {"root": roots[0], "name": slug, "folder": folder}


def folder_choices(pid: str) -> list[str]:
    """The folders a PENDING pick-from-project card offers (its own picker's list), for the
    operator's eyes and ears only — the card, a call's read-back — never the model. Blocking."""
    from . import ask_proposals

    cid = ask_proposals.locate(pid)
    row = ask_proposals._row(cid, pid) if cid else None
    if row is None or row.get("state") != PENDING or row.get("op") not in _PICK_FROM_PROJECT:
        return []
    return _folder_choices(row)


def shown_display(row: dict) -> dict | None:
    """The card's stored display as it may be SHOWN NOW (#2130, Hermes on #2226): re-admitted at
    READ time against the secrets registered now (`ask_store._admit_display`, the store boundary's
    own check), so a secret registered after staging is withheld on the very next read, not at
    the conversation's next write. None when there is none, when it must be withheld, or when the
    secret set cannot be established (fail closed: the rows are not shown)."""
    display = row.get("display")
    if not isinstance(display, dict):
        return None
    from . import ask_store, template_secrets

    try:
        secrets = template_secrets.redaction_values()
        return ask_store._admit_display(display, secrets, fresh=False)
    except (template_secrets.RedactionUnavailable, ask_store.StoreRefused, ValueError):
        return None


def team_hidden(row: dict, shown: object = ...) -> bool:
    """An `assemble` card whose team it can no longer show (#2130): it binds a team digest
    (staged after #2126), names no picks in its sentence (staged after #2130) and its rows are
    withheld (`shown_display`: a secret registered since, on this read or a stored withholding).
    Such a card would confirm a team the operator cannot see, so it offers no Confirm and its tap
    settles it stale. A card staged before #2126 (no digest) or #2130 (its stored sentence names
    the picks) is unaffected. `shown`: the caller's `shown_display(row)`, when it has it."""
    facts = row.get("facts") or {}
    if not (
        row.get("op") == "team.assemble"
        and isinstance(facts.get("team"), str)
        and not facts.get("picks")
    ):
        return False
    return (shown_display(row) if shown is ... else shown) is None


TEAM_HIDDEN = "its team can no longer be shown on the card; ask HQ to propose it again"


def view(row: dict) -> dict:
    """What the card may show and offer — nothing the decide route would refuse."""
    state = row.get("state")
    shown = shown_display(row)  # re-admitted on THIS read (Hermes on #2226)
    hidden = team_hidden(row, shown)
    out = {
        "state": state,
        "summary": row.get("outcome") or "",
        "reason": row.get("reason") or "",
        "can_confirm": (state == PENDING or _resumable(row)) and not hidden,
        "can_dismiss": state == PENDING,
    }
    if state == PENDING and hidden:
        out["reason"] = TEAM_HIDDEN  # why Confirm is gone, before anyone taps
    if row.get("decided_by") == "hq" and state != PENDING:
        out["decided_by"] = "hq"  # HQ ran it on the Commander's Authority setting (#2390)
    if state == PENDING and row.get("op") in _PICK_FROM_PROJECT:
        out["choices"] = {"folder": _folder_choices(row)}
    op = CATALOG.get(row.get("op") or "")
    if shown is not None and isinstance(shown.get("team"), list):
        # An `assemble` card's team as it was STAGED (#2130), whatever the card's state: a stale
        # card keeps these rows and is not confirmable; today's team is never shown here. Only
        # what passes the store boundary NOW is shown.
        out["team"] = shown["team"]
        out["team_revision"] = shown.get("revision")
    if op is not None and op.draft:
        # The card fetches what it shows from the content route (#1611), never from this poll.
        out["content"] = True
    if (
        op is not None
        and op.group in ("playbook", "skill", "automation")
        and state in (DONE, UNCONFIRMED)
    ):
        out["library"] = {"kind": op.group, "id": _saved_id(op, row)}
    if op is not None and op.group == "specialist" and state in (DONE, UNCONFIRMED):
        # The specialist a card changed; a create's new id is the store's, so its link opens the
        # pool (#1938).
        out["library"] = {"kind": "specialist", "id": str(row.get("target") or "")}
    if op is not None and op.group == "secret" and state in (DONE, UNCONFIRMED):
        out["library"] = {"kind": "secret", "id": str(row.get("target") or "")}
    mission = _mission_of(op, row)
    if mission:
        # The way into the mission a card names (#1950): its id is the server's own, never model
        # text, and the client builds the link (`missionLink`).
        out["mission"] = {"id": mission}
    return out


def _mission_of(op: Operation | None, row: dict) -> str:
    """The mission a card names: a team card's target, or the draft a mission card created (the
    id its route returned, kept on the entry). ``""`` when there is none (yet)."""
    from . import missions

    if op is None:
        return ""
    if op.group == "team":
        mid = row.get("target")
    elif op.group == "mission":
        # The draft a create made, or the existing draft `mission.plan` plans (#2152).
        mid = row.get("mission_id") or (row.get("target") if op.id == "mission.plan" else None)
    else:
        return ""
    return mid if isinstance(mid, str) and missions.MISSION_ID_RE.match(mid) else ""


#: What an interrupted move that had noted its commit says (#1786): it resumes, never replays.
RESUME_AFTER_STOP = (
    "BattleLab stopped while this ran. What it stored is noted; confirm again to finish from "
    "there."
)


def resumes_after_a_stop(row: dict) -> bool:
    """A ``deciding`` operation abandoned by a stop that can be finished, not reported: its own
    runner resumes (`Operation.run`) and it had noted what it committed (#1786's move)."""
    op = CATALOG.get(row.get("op") or "")
    return op is not None and op.run is not None and isinstance(row.get("committed"), dict)


def _resumable(row: dict) -> bool:
    """An ``unsettled`` move whose operation can resume it (confirmed again, never dismissed)."""
    op = CATALOG.get(row.get("op") or "")
    return row.get("state") == UNSETTLED and op is not None and op.run is not None


def _saved_id(op: Operation, row: dict) -> str:
    """What a done library save names in the Library: the new id, or the target it changed."""
    fields = row.get("fields") or {}
    return str(fields.get("new_id") or row.get("target") or fields.get("saved_id") or "")


def running(pid: str) -> bool:
    return pid in _TASKS


async def drain(timeout: float = 30.0) -> None:
    pending = set(_TASKS.values())
    if not pending:
        return
    _done, late = await asyncio.wait(pending, timeout=timeout)
    for t in late:
        t.cancel()
    if late:
        await asyncio.wait(late, timeout=5)


# ---- staging ------------------------------------------------------------------------------------


async def stage(
    *,
    cid: str,
    turn_id: str,
    op: Operation,
    target: str,
    title: str,
    fields: dict,
    app,
    operator: asgi_call.Operator,
    append_card: Callable,
    draft: dict | None = None,
    expected: dict | None = None,
    refused: str = "",
) -> dict:
    """Stage one operation: facts read as the operator, the draft (a library save, #1611) written
    through the store boundary, the card written FIRST, the entry SECOND (a card whose entry never
    landed reads "no longer tracked" and offers nothing). Returns the card's summary for the
    model. Raises :class:`Refused`."""
    import uuid

    from . import ask_proposals

    for k, v in fields.items():
        guarded = (
            v
            if not isinstance(v, str)
            else await asyncio.to_thread(ask_store.guard_text, v, SUMMARY_MAX)
        )
        if guarded != v:
            raise Refused(
                f"refused: the {k} contains a secret or looks like a credential", "credential"
            )
    facts, display = lift_display(await facts_for(op, target, fields, app, operator, cid=cid))
    if facts is None:
        raise Refused("refused: that is no longer available", "not_available")
    if expected is not None and changed(expected, facts):
        if op.group == "team":  # the team HQ quoted is not the one the card would bind (#2126)
            raise Refused(
                "refused: the plan or its picks changed while the card was prepared; read the "
                "plan again. Nothing was written",
                "stale",
            )
        if op.group == "mission":  # the procedure HQ validated is not the one the card would show
            raise Refused(
                "refused: the project's procedures or the mission changed while the card was "
                "prepared; read them again with list_procedures. Nothing was written",
                "stale",
            )
        if op.group == "specialist":  # saved elsewhere between the read the card came from and
            # the card itself (#1938 review): binding the newer revision would let the tap write
            # the older replacement over that save.
            raise Refused(
                "refused: the specialist was saved, archived or unarchived elsewhere while the "
                "card was prepared; read it again with specialist_detail and propose again. "
                "Nothing was written",
                "stale",
            )
        if op.group == "mcp":  # the package HQ reviewed is not the one the card would install
            raise Refused(
                "refused: the MCP server's source changed while the card was prepared; read it "
                "again with list_mcp_servers. Nothing was installed",
                "stale",
            )
        if op.group == "automation":  # the automation whose config and scope the card previews
            # is not the one a later read would bind (#2260): the card binds the revision its
            # draft came from, so a save in between stages nothing.
            raise Refused(
                "refused: the automation was saved elsewhere while the card was prepared; read it "
                "again with automation_detail and propose again. Nothing was written",
                "stale",
            )
        raise Refused("refused: that note changed after HQ read it; search again", "not_available")
    shown = facts.get("title") or facts.get("name") or title
    pid = uuid.uuid4().hex
    sha = ""
    if op.draft:
        if not isinstance(draft, dict):
            raise Refused("refused: this save needs its draft", "bad_arguments")
        try:
            from .ask_library import TRUSTED_DRAFT_PATHS
            from .ask_specialists import TRUSTED_DRAFT_PATHS as SPECIALIST_PATHS

            # A specialist's draft has its own server-resolved paths (#1938); no other draft has
            # a `body` or `before` holding them, so each group keeps exactly its own.
            trusted = SPECIALIST_PATHS if op.group == "specialist" else TRUSTED_DRAFT_PATHS
            sha = await asyncio.to_thread(
                lambda: ask_store.write_draft(cid, pid, draft, trusted=trusted)
            )
        except ask_store.Withheld:
            raise Refused(
                "refused: the draft contains a secret or looks like a credential", "credential"
            ) from None
        except ask_store.StoreRefused as e:
            raise Refused(f"refused: {e}", "not_recorded") from None
    bind = await asyncio.to_thread(binding, op.id, target, fields, facts, sha)
    summary = op.describe(fields, facts)
    # What the card says goes back to the MODEL too (`ask_orchestrator._stage`): a sentence the
    # store boundary would change — a stored secret's value inside a fact such as a project name —
    # is refused, never staged or returned (Hermes on #1813).
    if len(summary) > SUMMARY_MAX:
        # The card stores at most SUMMARY_MAX characters: a sentence that long would be cut, and
        # a cut access card could hide a project the tap widens access to (#1813 review).
        raise Refused(
            "refused: this card would be too long to show in full — the operator can do it in "
            "the Library",
            "too_long",
        )
    for said in (summary, str(shown)):
        if await asyncio.to_thread(ask_store.guard_text, said, max(len(said), 1)) != said:
            raise Refused(
                "refused: the card would show a secret or something that looks like a "
                "credential",
                "credential",
            )
    try:
        await append_card(
            cid,
            {
                "type": "proposal",
                "turn_id": turn_id,
                "action_id": pid,
                "kind": KIND,
                "op": op.id,
                "risk": op.risk,
                "binding": bind,
                "title": shown,
                "label": op.label,
                "verb": op.verb_of(fields, facts) if op.verb_of else op.verb,
                "text": summary,
                "needs": list(op.inputs),
            },
        )
    except Exception:
        raise Refused(
            "refused: the proposal could not be recorded; nothing will run", "not_recorded"
        ) from None
    entry = {
        "id": pid,
        "turn_id": turn_id,
        "kind": KIND,
        "state": REFUSED if refused else PENDING,
        **({"outcome": refused, "settled_at": time.time()} if refused else {}),
        "ts": time.time(),
        "op": op.id,
        "target": target,
        "binding": bind,
        "fields": fields,
        "facts": facts,
        "title": shown,
    }
    if display is not None:
        entry["display"] = display
    try:
        await asyncio.to_thread(ask_proposals.add, cid, entry)
    except ask_store.Withheld:
        raise Refused(
            "refused: it contains a secret or looks like a credential", "credential"
        ) from None
    except Exception:
        raise Refused(
            "refused: the proposal could not be recorded; nothing will run", "not_recorded"
        ) from None
    return {"summary": summary, "target": shown, "id": pid}


# ---- the card's content (#1611) -----------------------------------------------------------------


def bound_draft(pid: str) -> tuple[int, dict, str]:
    """A library card's draft, ONLY while it still hashes to what the card is bound to: the
    status, the draft (or the refusal's ``detail``), and the operation's id. Blocking. For the
    operator's eyes and ears (the card, a call's read-back) — never the model."""
    from . import ask_proposals

    cid = ask_proposals.locate(pid)
    row = ask_proposals._row(cid, pid) if cid else None
    op = CATALOG.get((row or {}).get("op") or "")
    if row is None or op is None or not op.draft:
        return 404, {"detail": "no such card content"}, ""
    try:
        draft, sha = ask_store.read_draft(cid, pid)
    except (OSError, ValueError):
        return 404, {"detail": "this card's content is no longer kept"}, op.id
    expected = binding(op.id, row["target"], row["fields"], row["facts"], sha)
    if not (_card_binding(cid, pid) == row.get("binding") == expected):
        return 409, {"detail": "this card's content changed after it was proposed"}, op.id
    return 200, draft, op.id


def content(pid: str) -> tuple[int, dict]:
    """What a library card shows of its draft, for the operator's browser — never the model.
    Served only while the draft still hashes to what the card is bound to: a draft changed on
    disk is a 409, never shown altered. Blocking."""
    from . import ask_library

    status, draft, op_id = bound_draft(pid)
    if status != 200:
        return status, draft
    if op_id.startswith("mcp."):  # a server a create card registers (#2163 P2)
        from . import ask_mcp

        return 200, ask_mcp.content_view(draft)
    if op_id == "memory.edit":
        return 200, {
            "kind": "diff",
            "diff": ask_library._diff(draft.get("before", {}), draft.get("files", {})),
        }
    if op_id.startswith("mission."):  # the flow a mission card applies (#2152)
        from . import ask_mission

        return 200, ask_mission.content_view(draft)
    if op_id.startswith("specialist."):  # the specialist in full, or an edit's two versions
        from . import ask_specialists

        return 200, ask_specialists.content_view(draft)
    shown = ask_library.content_view(op_id, draft)
    if op_id.startswith("playbook.") and draft.get("relies_on"):
        # The specialists a playbook card says it relies on (#1938), with their state NOW: read
        # from the store and this conversation's cards on every read, never from staging.
        from . import ask_proposals, ask_specialists

        cid = ask_proposals.locate(pid)
        shown["relies"] = ask_specialists.relies(cid, pid, draft["relies_on"]) if cid else []
    return 200, shown


# ---- the tap ------------------------------------------------------------------------------------


def _card_binding(cid: str, pid: str) -> str | None:
    for r in ask_store.read(cid):
        if r.get("type") == "proposal" and r.get("action_id") == pid and r.get("kind") == KIND:
            return r.get("binding")
    return None


def gate_inputs(op: Operation, raw: object) -> dict:
    """The operator's card inputs for ``op`` (a picked folder), each through its gate. Raises
    ``ask_proposals.ProposalError`` (422) — this is the operator's request, not the model's."""
    from . import ask_proposals

    raw = raw or {}
    optional = {
        n
        for n, (with_, _) in OPTIONAL_INPUTS.items()
        if with_ in op.inputs and op.id in _OPTIONAL_INPUT_OPS[n]
    }
    if not isinstance(raw, dict) or set(raw) - set(op.inputs) - optional:
        raise ask_proposals.ProposalError(422, "this card takes no such input")
    out = {}
    for name in op.inputs:
        if name not in raw:
            raise ask_proposals.ProposalError(422, f"choose a {name} on the card first")
        try:
            out[name] = INPUT_GATES[name](raw[name])
        except Refused as e:
            raise ask_proposals.ProposalError(422, str(e)) from None
    for name in optional & set(raw):
        try:
            out[name] = OPTIONAL_INPUTS[name][1](raw[name])
        except Refused as e:
            raise ask_proposals.ProposalError(422, str(e)) from None
    return out


async def _prepare(cid: str, row: dict, inputs: object = None):
    """What a confirm checks and gathers before ``deciding``, shared by the tap and HQ's act
    (#2390): the entry must match its card and a recomputed binding (a Library draft re-read and
    re-hashed, #1611), the operator's inputs pass their gates, and the runner's extras are built.
    Returns ``(op, extras, tapped)``, or the entry settled ``stale`` (a dict) when it no longer
    matches. Raises ``ProposalError`` for a bad input (the entry stays pending)."""
    from . import ask_proposals

    pid = row["id"]
    op = CATALOG.get(row.get("op") or "")
    card = await asyncio.to_thread(_card_binding, cid, pid)
    content, sha = None, ""
    if op is not None and op.draft:
        # The draft this card shows, re-read and re-hashed: changed on disk → the binding
        # below cannot match, and nothing runs (#1611).
        with contextlib.suppress(OSError, ValueError):
            content, sha = await asyncio.to_thread(ask_store.read_draft, cid, pid)
    expected = (
        await asyncio.to_thread(binding, op.id, row["target"], row["fields"], row["facts"], sha)
        if op and (content is not None or not op.draft)
        else None
    )
    if op is None or card is None or not (card == row.get("binding") == expected):
        row = await asyncio.to_thread(
            ask_proposals._update,
            cid,
            pid,
            state=STALE,
            reason="this card no longer matches what was proposed; nothing ran",
            settled_at=time.time(),
        )
        return row or {}
    extras = gate_inputs(op, inputs)  # before `deciding`: a bad input leaves it pending
    if _resumable(row):
        # An unfinished move confirmed again (#1786): its runner resumes where it stopped,
        # from what it committed, and facts that its own first steps changed (the chat copy
        # released) are expected.
        extras["_resumed"] = True
        if isinstance(row.get("committed"), dict):
            extras["_committed"] = dict(row["committed"])
    if op.follow_up:
        # Where the card's turn stood when THIS tap was decided (#2050): read now, never when
        # the card settles, so a Stop during the run does not turn an earlier confirm into
        # "after the Stop", nor let it restart a turn the Commander stopped.
        from . import ask_orchestrator

        tapped = ask_orchestrator.tap_state(cid, row.get("turn_id"))
    else:
        tapped = None
    if op.run is not None:
        # A runner notes its progress on its own entry as it goes (#1786: what it committed).
        extras["_pid"] = row["id"]
    if content is not None:
        extras["_content"] = content
    return op, extras, tapped


async def decide_locked(
    cid: str,
    row: dict,
    fd: int,
    decision: str,
    *,
    operator: asgi_call.Operator,
    app,
    inputs: object = None,
) -> tuple[int, dict]:
    """Decide one pending operation. The caller holds the conversation's decide lock (``fd``);
    from here this function owns it — it is closed when the decision settles."""
    import os

    from . import ask_proposals

    pid = row["id"]
    handed = False
    try:
        if decision == "dismiss":
            row = await asyncio.to_thread(
                ask_proposals._update, cid, pid, state=DISMISSED, settled_at=time.time()
            )
            return 200, view(row or {})
        if decision != "confirm":
            raise ask_proposals.ProposalError(422, "an operation is confirmed or dismissed")
        prepared = await _prepare(cid, row, inputs)
        if isinstance(prepared, dict):  # settled stale: nothing runs
            return 200, view(prepared)
        op, extras, tapped = prepared
        row = await asyncio.to_thread(
            ask_proposals._update, cid, pid, state=DECIDING, decided_at=time.time(), reason=""
        )
        if row is None:
            raise ask_proposals.ProposalError(404, "no such proposal")
        _TASKS[pid] = asyncio.create_task(
            _run(cid, dict(row), op, fd, operator, app, extras, tapped=tapped)
        )
        handed = True
        return 202, view(row)
    finally:
        if not handed:
            os.close(fd)


#: An act refused before it claimed anything says why; the card stays pending for the tap.
ACT_BUSY = "another card of this conversation is being decided right now"

#: Per-effect deadlines of an act (#2391), chosen BEFORE they were measured (the PR reports the
#: observed values). Past its deadline the act keeps running in the background and settles on its
#: card; the turn says it is still going, never waits longer.
#: How long an act waits for the conversation's decide lock (a card being decided, or an earlier
#: act's launch still holding it). Past it, the card waits for the Commander.
ACT_LOCK_WAIT_S = 10.0
#: How long the turn waits for an operation's route (an automation's run now, a mission relay).
ACT_ROUTE_WAIT_S = 30.0


def running_any(pid: str) -> bool:
    """Whether a task of this process still runs proposal ``pid``, of any kind."""
    from . import ask_messages, ask_proposals

    return pid in _TASKS or ask_proposals.running(pid) or pid in ask_messages._TASKS


async def _act_lock(cid: str, wait: float) -> int | None:
    """The conversation's decide lock, tried until ``wait`` seconds passed (0: once)."""
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        fd = await asyncio.to_thread(ask_store.try_decide_lock, cid)
        if fd is not None or time.monotonic() >= deadline:
            return fd
        await asyncio.sleep(0.05)


async def act(
    cid: str,
    pid: str,
    *,
    operator,
    app,
    grant,
    record: dict,
    wait: float | None = None,
    lock_wait: float = 0.0,
) -> tuple[str, dict]:
    """HQ runs one staged card itself, on the Commander's Authority setting (#2390, #2391): the
    tap's own claim and runner, minus the wait. ``grant`` is the turn's
    ``hq_authority.ActGrant``; ``record`` the acts-log fields of this turn (``tainted`` among them,
    D4). An operation runs through the catalog's route (`_run`); a session start through the
    session card's launch (`ask_proposals.act_start`, an API card only, bypass off); a connector
    message through the message card's send (`ask_messages.act_send`). Each kind re-checks the
    grant, on the entry's EFFECTIVE class derived again, immediately before each of its effects.

    The claim is the tap's: the conversation's decide lock (waited for up to ``lock_wait``), the
    entry still ``pending``, the kind's own preparation and re-checks, and the grant asked right
    before the durable ``pending → deciding`` write, which also records ``decided_by: "hq"`` and
    the authority revision. A restart mid-act leaves ``deciding``, which
    ``ask_proposals.recover`` settles ``interrupted``: never replayed.

    Returns ``("ran", entry)`` once it settled (the card's outcome is the kind's own),
    ``("running", entry)`` when it was claimed and still runs after ``wait`` seconds (it settles
    on its card), or ``("waits", {"reason"})`` / ``("decided", entry)`` / ``("stale", entry)``
    when it did not run and the card stays as it is."""
    import os

    from . import ask_messages, ask_proposals, hq_acts

    carried = hq_acts.Act(grant, dict(record))
    fd = await _act_lock(cid, lock_wait)
    if fd is None:
        return "waits", {"reason": ACT_BUSY}
    handed = False
    task: asyncio.Task | None = None
    try:
        row = await asyncio.to_thread(ask_proposals._row, cid, pid)
        if row is None:
            return "waits", {"reason": "the card is no longer tracked"}
        if row.get("state") != PENDING:
            return "decided", row  # the Commander (or another act) decided it first
        why = carried.admits(row)
        if why:
            return "waits", {"reason": why}
        kind = row.get("kind")
        try:
            if kind == "session":
                task = await ask_proposals.act_start(cid, row, fd, carried)
            elif kind == "message":
                task = await ask_messages.act_send(cid, row, fd, carried)
            elif kind == KIND:
                task = await _act_operation(cid, row, fd, carried, operator, app)
            else:
                return "waits", {"reason": "this one is always confirmed on its card"}
        except hq_acts.Waits as e:
            return "waits", {"reason": e.reason}
        if isinstance(task, dict):  # settled stale by the kind's own re-check: nothing ran
            return "stale", task
        handed = True
    finally:
        if not handed:
            os.close(fd)
    if wait is None:
        wait = ACT_ROUTE_WAIT_S
    # The tool waits for the outcome, up to the deadline; a cancelled turn (shutdown) leaves the
    # task to its kind's `drain`, and a slow one keeps running and settles on its card.
    done, _ = await asyncio.wait({task}, timeout=max(0.0, wait))
    row = await asyncio.to_thread(ask_proposals._row, cid, pid)
    if row is None:
        return "waits", {"reason": "the card is no longer tracked"}
    if not done:
        return "running", row
    if row.get("state") == PENDING:
        return "waits", {"reason": row.get("reason") or "it was not admitted"}
    return "ran", row


#: Operations whose class their facts decide (#2391): re-read at the claim, not only at the route.
_DYNAMIC_CLASS = frozenset({"automation.run_now"})


def card_only(row: dict, facts: dict | None = None) -> str:
    """Why this operation is always confirmed on its card even where its class acts (#2391), or
    "": a relay into a terminal session (HQ types into no terminal on its own, #1613 D17)."""
    basis = facts if facts is not None else row.get("facts")
    if row.get("op") == "mission.relay" and not (isinstance(basis, dict) and basis.get("api")):
        return "HQ types into an API session only; a terminal is always relayed on its card"
    return ""


async def _act_operation(cid: str, row: dict, fd: int, carried, operator, app):
    """An operation's act (#2390): the tap's preparation and claim; returns the run's task (it
    owns ``fd`` from then), or the entry settled ``stale``. Raises ``hq_acts.Waits``."""
    from . import ask_proposals, hq_acts

    pid = row["id"]
    prepared = await _prepare(cid, row)
    if isinstance(prepared, dict):
        return prepared
    op, extras, tapped = prepared
    if op.run is not None or op.inputs or op.follow_up:
        raise hq_acts.Waits("this one is always confirmed on its card")

    def admit(facts: dict | None = None) -> str:
        # The class of what runs NOW (#2391): an automation's scope re-read at the route.
        return card_only(row, facts) or carried.admits(row, facts)

    now = None
    if op.id in _DYNAMIC_CLASS:
        # Its class at the claim is its class NOW (#2391): a scope that moved since staging, or
        # one that can not be read, keeps it on its card.
        now, _ = lift_display(
            await facts_for(op, row["target"], row["fields"], app, operator, cid=cid)
        )
    why = admit(now)  # again after the awaits: the last check before the claim
    if why:
        raise hq_acts.Waits(why)
    rev = carried.revision
    claimed = await asyncio.to_thread(
        ask_proposals._update,
        cid,
        pid,
        state=DECIDING,
        decided_at=time.time(),
        decided_by="hq",
        authority_rev=rev,
        reason="",
    )
    if claimed is None:
        raise hq_acts.Waits("the card is no longer tracked")
    # The claim's own log line, before the route runs: a crash from here on is resolved against
    # the sidecar when the log is read (`hq_acts`), never replayed.
    if not await asyncio.to_thread(
        carried.claimed, claimed, cid, op=op.id, label=op.label, title=claimed.get("title") or ""
    ):
        # Fail closed: an act the log cannot record does not run. The card waits.
        await asyncio.to_thread(
            ask_proposals._update,
            cid,
            pid,
            state=PENDING,
            decided_at=None,
            decided_by=None,
            authority_rev=None,
        )
        raise hq_acts.Waits("the acts log could not be written, so HQ did not run it")
    task = asyncio.create_task(
        _run(cid, dict(claimed), op, fd, operator, app, extras, tapped=tapped, admit=admit,
             act=carried)
    )  # fmt: skip
    _TASKS[pid] = task
    return task


async def _route_once(
    op: Operation, row: dict, fields: dict, now, app, operator, admit: Callable | None = None
) -> dict:
    """The catalog's one route call for ``op``, and the outcome it settles to. ``admit`` (an act
    of HQ's, #2390) is asked once more right before the call, with no await in between, on the
    facts just re-read (#2391: the effective class of what runs now): "" lets it run; a reason
    returns the card to ``pending`` with nothing run."""
    try:
        # Off the loop: a folder change reads the project store (its flock, #974).
        basis = row["facts"] if op.staged_facts else now
        method, path, body = await asyncio.to_thread(op.route, row["target"], fields, basis or {})
    except _Moved as e:
        return {"state": STALE, "reason": f"{e} since this was proposed; nothing ran"}
    carried = None
    if admit is not None:
        why = admit(now)  # the class of what runs now, from the facts just re-read (#2391)
        if why:
            return {"state": PENDING, "reason": why}

        def carried() -> str:
            # Carried into the route (#2391): a route whose effect lands after its own awaits
            # asks the grant again at that effect's final fence (the relay's delivery).
            return admit(now)

    resp = await asgi_call.call(app, operator, method, path, body, act_admit=carried)
    if admit is not None and isinstance(resp.body, dict) and resp.body.get("act_refused") is True:
        # The route's own effect boundary refused the act's grant before any effect: nothing
        # ran, and the card is the Commander's again.
        return {"state": PENDING, "reason": str(resp.detail or resp.body.get("detail") or "")}
    if resp.truncated and 200 <= resp.status < 300:
        state, said = UNCONFIRMED, UNCONFIRMED_SAID
    else:
        state, said = op.outcome(resp.status, resp.body, row["facts"])
    if state == FAILED:
        said = op.failure(resp.status, resp.detail, row["facts"])
    if state == STALE:  # the route refused what moved (#2129): said like the re-read's `stale`
        return {"state": STALE, "reason": said[:SUMMARY_MAX]}
    return {"state": state, "outcome": said[:SUMMARY_MAX]}


async def _run(
    cid: str,
    row: dict,
    op: Operation,
    fd: int,
    operator,
    app,
    extras: dict | None = None,
    *,
    tapped: str | None = None,
    admit: Callable | None = None,
    act=None,
) -> None:
    """Run the operation and write its outcome — whatever happened. Owns ``fd``. ``admit`` and
    ``act`` (`hq_acts.Act`): an act of HQ's (#2390), re-admitted right before the route and logged
    when it settles."""
    import os

    from . import ask_proposals

    pid = row["id"]
    outcome: dict
    try:
        try:
            now, _ = lift_display(
                await facts_for(op, row["target"], row["fields"], app, operator, cid=cid)
            )
            why = changed(row["facts"], now)
            if why and (extras or {}).get("_resumed"):
                why, now = "", row["facts"]
            if not why and team_hidden(row):
                # Never confirm a team the card cannot show (#2130).
                outcome = {"state": STALE, "reason": f"{TEAM_HIDDEN}; nothing ran"}
            elif why:
                said = (
                    op.stale(why, row)
                    if op.stale
                    else f"{why} since this was proposed; nothing ran"
                )
                outcome = {"state": STALE, "reason": said}
            else:
                fields = {**row["fields"], **(extras or {})}
                if op.run is not None:
                    # Its own ordered steps (#1786's move); it settles itself.
                    outcome = await op.run(cid, row["target"], fields, now or {}, app, operator)
                    outcome["outcome"] = str(outcome.get("outcome") or "")[:SUMMARY_MAX]
                else:
                    outcome = await _route_once(op, row, fields, now, app, operator, admit)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a decision must always settle
            log.warning("ask operation failed (%s)", type(e).__name__)
            outcome = {
                "state": FAILED,
                "outcome": "It stopped unexpectedly. It may have run; check the target. It is "
                "not retried.",
            }
        if op.follow_up and outcome.get("state") == DONE:
            # HQ continues (#1950): started BEFORE the card reads done, so a client that sees the
            # card settle also sees the new turn and keeps reading. Never raises.
            from . import ask_orchestrator

            await ask_orchestrator.continue_after_card(
                cid, {**row, **outcome}, op, operator=operator, app=app, tapped=tapped
            )
        if outcome.get("state") == PENDING:
            # Refused at the effect (#2390): nothing ran, and the card waits for the tap again.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    ask_proposals._update,
                    cid,
                    pid,
                    state=PENDING,
                    decided_at=None,
                    decided_by=None,
                    authority_rev=None,
                    reason=str(outcome.get("reason") or "")[:SUMMARY_MAX],
                )
        else:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    ask_proposals._update, cid, pid, settled_at=time.time(), **outcome
                )
        if act is not None:
            await asyncio.to_thread(
                act.settled, pid, cid, op=op.id, label=op.label, title=row.get("title") or "",
                state=str(outcome.get("state") or ""),
                outcome=str(outcome.get("outcome") or outcome.get("reason") or ""),
            )  # fmt: skip
    except asyncio.CancelledError:
        # Shutdown: the entry stays `deciding` and reads `interrupted` once this lock is gone.
        raise
    finally:
        os.close(fd)
        _TASKS.pop(pid, None)
