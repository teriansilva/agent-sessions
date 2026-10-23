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
STATES = frozenset(
    {PENDING, DECIDING, DONE, PARTLY_DONE, FAILED, STALE, DISMISSED, INTERRUPTED, UNCONFIRMED}
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


#: Inputs only the operator supplies, on the card, with the confirm.
INPUT_GATES: dict[str, Callable[[object], object]] = {"folder": _folder_input}


GATES: dict[str, Callable[[object], object]] = {
    "title": _title,
    "tag": _tag,
    "color": _color,
    "name": _name,
    "days": _days,
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
    group: str  # "session" | "project" | "playbook" | "skill" | "automation"
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
                {"name": f["name"], "folders": [f["folder"]], "default_folder": f["folder"]},
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
                _folder_patch(k, x, add=f["folder"]),
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

    return (*_ops(), *library_ops())


CATALOG: dict[str, Operation] = {op.id: op for op in _all_ops()}


def by_action(group: str, action: str) -> Operation | None:
    return next((o for o in CATALOG.values() if o.group == group and o.action == action), None)


def actions(group: str) -> list[str]:
    return sorted(o.action for o in CATALOG.values() if o.group == group)


# ---- facts, binding -----------------------------------------------------------------------------


async def facts_for(op: Operation, target: str, fields: dict, app, operator) -> dict | None:
    """The target's facts as the card shows them, or None when the target is gone."""
    if op.target == "session":
        facts = await _session_facts(app, operator, target)
        if facts is not None and op.id == "session.move" and fields.get("project_id"):
            # The card names its destination: bind it, so a project renamed or removed before
            # the tap is `stale`, never a move to a project the card did not show (review 6742).
            facts["dest"] = await _project_name(app, operator, fields["project_id"])
            if not facts["dest"]:
                return None  # a destination already gone stages nothing
        return facts
    if op.target == "project":
        return await _project_facts(app, operator, target)
    if op.id == "sessions.archive_older":
        return await asyncio.to_thread(_older_facts, int(fields["days"]))
    if op.group in ("playbook", "skill", "automation"):
        from . import ask_library

        return await ask_library.facts(op, target, app, operator)
    return {}


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
    if "enabled" in before and before["enabled"] != now.get("enabled"):
        return "it was switched on" if now.get("enabled") else "it was switched off"
    if "revision" in before and before["revision"] != now.get("revision"):
        return "it was saved elsewhere after HQ read it"
    if "scope" in before and before["scope"] != now.get("scope"):
        return "what it would do changed"
    return ""


def gate_fields(op: Operation, raw: dict) -> dict:
    """The operation's typed fields, each through its gate. Fields it doesn't take are refused."""
    out: dict = {}
    for name in op.fields:
        if name in ("project_id", "project_name"):
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


def view(row: dict) -> dict:
    """What the card may show and offer — nothing the decide route would refuse."""
    state = row.get("state")
    out = {
        "state": state,
        "summary": row.get("outcome") or "",
        "reason": row.get("reason") or "",
        "can_confirm": state == PENDING,
        "can_dismiss": state == PENDING,
    }
    if state == PENDING and row.get("op") in _PICK_FROM_PROJECT:
        out["choices"] = {"folder": _folder_choices(row)}
    op = CATALOG.get(row.get("op") or "")
    if op is not None and op.draft:
        # The card fetches what it shows from the content route (#1611), never from this poll.
        out["content"] = True
    if (
        op is not None
        and op.group in ("playbook", "skill", "automation")
        and state in (DONE, UNCONFIRMED)
    ):
        out["library"] = {"kind": op.group, "id": _saved_id(op, row)}
    return out


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
    facts = await facts_for(op, target, fields, app, operator)
    if facts is None:
        raise Refused("refused: that is no longer available", "not_available")
    shown = facts.get("title") or facts.get("name") or title
    pid = uuid.uuid4().hex
    sha = ""
    if op.draft:
        if not isinstance(draft, dict):
            raise Refused("refused: this save needs its draft", "bad_arguments")
        try:
            from .ask_library import TRUSTED_DRAFT_PATHS

            sha = await asyncio.to_thread(
                lambda: ask_store.write_draft(cid, pid, draft, trusted=TRUSTED_DRAFT_PATHS)
            )
        except ask_store.Withheld:
            raise Refused(
                "refused: the draft contains a secret or looks like a credential", "credential"
            ) from None
        except ask_store.StoreRefused as e:
            raise Refused(f"refused: {e}", "not_recorded") from None
    bind = await asyncio.to_thread(binding, op.id, target, fields, facts, sha)
    summary = op.describe(fields, facts)
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
                "verb": op.verb,
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
        "state": PENDING,
        "ts": time.time(),
        "op": op.id,
        "target": target,
        "binding": bind,
        "fields": fields,
        "facts": facts,
        "title": shown,
    }
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
    return {"summary": summary, "target": shown}


# ---- the card's content (#1611) -----------------------------------------------------------------


def content(pid: str) -> tuple[int, dict]:
    """What a library card shows of its draft, for the operator's browser — never the model.
    Served only while the draft still hashes to what the card is bound to: a draft changed on
    disk is a 409, never shown altered. Blocking."""
    from . import ask_library, ask_proposals

    cid = ask_proposals.locate(pid)
    row = ask_proposals._row(cid, pid) if cid else None
    op = CATALOG.get((row or {}).get("op") or "")
    if row is None or op is None or not op.draft:
        return 404, {"detail": "no such card content"}
    try:
        draft, sha = ask_store.read_draft(cid, pid)
    except (OSError, ValueError):
        return 404, {"detail": "this card's content is no longer kept"}
    expected = binding(op.id, row["target"], row["fields"], row["facts"], sha)
    if not (_card_binding(cid, pid) == row.get("binding") == expected):
        return 409, {"detail": "this card's content changed after it was proposed"}
    return 200, ask_library.content_view(op.id, draft)


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
    if not isinstance(raw, dict) or set(raw) - set(op.inputs):
        raise ask_proposals.ProposalError(422, "this card takes no such input")
    out = {}
    for name in op.inputs:
        if name not in raw:
            raise ask_proposals.ProposalError(422, f"choose a {name} on the card first")
        try:
            out[name] = INPUT_GATES[name](raw[name])
        except Refused as e:
            raise ask_proposals.ProposalError(422, str(e)) from None
    return out


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
            return 200, view(row or {})
        extras = gate_inputs(op, inputs)  # before `deciding`: a bad input leaves it pending
        if content is not None:
            extras["_content"] = content
        row = await asyncio.to_thread(
            ask_proposals._update, cid, pid, state=DECIDING, decided_at=time.time()
        )
        if row is None:
            raise ask_proposals.ProposalError(404, "no such proposal")
        _TASKS[pid] = asyncio.create_task(_run(cid, dict(row), op, fd, operator, app, extras))
        handed = True
        return 202, view(row)
    finally:
        if not handed:
            os.close(fd)


async def _run(
    cid: str, row: dict, op: Operation, fd: int, operator, app, extras: dict | None = None
) -> None:
    """Run the operation and write its outcome — whatever happened. Owns ``fd``."""
    import os

    from . import ask_proposals

    pid = row["id"]
    outcome: dict
    try:
        try:
            now = await facts_for(op, row["target"], row["fields"], app, operator)
            why = changed(row["facts"], now)
            if why:
                outcome = {"state": STALE, "reason": f"{why} since this was proposed; nothing ran"}
            else:
                fields = {**row["fields"], **(extras or {})}
                try:
                    # Off the loop: a folder change reads the project store (its flock, #974).
                    method, path, body = await asyncio.to_thread(
                        op.route, row["target"], fields, now or {}
                    )
                except _Moved as e:
                    outcome = {
                        "state": STALE,
                        "reason": f"{e} since this was proposed; nothing ran",
                    }
                else:
                    resp = await asgi_call.call(app, operator, method, path, body)
                    if resp.truncated and 200 <= resp.status < 300:
                        state, said = UNCONFIRMED, UNCONFIRMED_SAID
                    else:
                        state, said = op.outcome(resp.status, resp.body, row["facts"])
                    if state == FAILED:
                        said = op.failure(resp.status, resp.detail, row["facts"])
                    outcome = {"state": state, "outcome": said[:SUMMARY_MAX]}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a decision must always settle
            log.warning("ask operation failed (%s)", type(e).__name__)
            outcome = {
                "state": FAILED,
                "outcome": "It stopped unexpectedly. It may have run; check the target. It is "
                "not retried.",
            }
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                ask_proposals._update, cid, pid, settled_at=time.time(), **outcome
            )
    except asyncio.CancelledError:
        # Shutdown: the entry stays `deciding` and reads `interrupted` once this lock is gone.
        raise
    finally:
        os.close(fd)
        _TASKS.pop(pid, None)
