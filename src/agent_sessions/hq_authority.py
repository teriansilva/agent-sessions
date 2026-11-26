"""HQ's authority: what HQ does on its own and what it asks first (#2390, epic #2070 §1, A1).

One prefs block, ``hq_authority``: ``{"classes": {<class>: "ask" | "act"}, "revision": n}``,
written only by the Commander through ``PUT /api/hq/authority`` (login, CSRF, Origin, and never
from HQ's own in-process calls: ``asgi_call`` marks those, and the route refuses them). It is read
only at the write boundary, never from model output: a turn snapshots it when it starts
(:func:`snapshot`) and the act path re-reads it immediately before the claim and again immediately
before the route call (:func:`admits`).

**Classes.** Every catalog operation has exactly one class (:data:`CLASS_OF`, server-authored and
pinned against ``ask_operations.CATALOG``): the class of its EFFECTIVE action, the strongest of
what it sets in motion, so it may be stronger than the card's own ``risk`` stamp. Screen (#2102)
always acts and is not stored. An absent block, an unknown class or an unknown value reads as
``ask`` (fail closed); an upgrade therefore keeps Ask first (D1).

**What can act in this version** is :data:`ACTABLE` (A1: reversible only). A stored ``act`` for
another class is the Commander's own configuration, kept and shown, and changes nothing until the
child of #2070 that adds its class here ships.

**Revocation.** A logout bumps an in-memory epoch (:func:`logged_out`, a logout hook): sessions
are stateless, so a cookie stays valid after a sign-out and the epoch is what refuses an act a
turn begun before it would still make. Nothing here survives a restart, and neither does a turn.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import prefs

KEY = "hq_authority"
ASK, ACT = "ask", "act"
#: The stored classes, in the order the Settings page shows them. Screen is not stored.
CLASSES = ("reversible", "starts", "destructive", "external", "widens")
#: The classes labelled RISKY in Settings (D2): they may be set to act, with that label.
RISKY = frozenset({"destructive", "external", "widens"})
#: The classes HQ can act on in this version (#2070 A1). A1b adds "starts", A1c the RISKY ones.
ACTABLE = frozenset({"reversible"})
#: Presets set the classes together; any other mix reads as "custom".
PRESETS: dict[str, dict[str, str]] = {
    "ask_first": {c: ASK for c in CLASSES},
    "standard": {c: (ACT if c == "reversible" else ASK) for c in CLASSES},
    "full": {c: ACT for c in CLASSES},
}

#: Every catalog operation's class (the effective action, epic §1). Ambiguous entries take the
#: stronger class, so in A1 they stay cards. Pinned against the catalog in the tests.
CLASS_OF: dict[str, str] = {
    # sessions
    "session.rename": "reversible",
    "session.tag": "reversible",
    "session.color": "reversible",
    "session.favorite": "reversible",
    "session.unfavorite": "reversible",
    "session.move": "reversible",
    "session.unarchive": "reversible",
    "session.review_exclude": "reversible",
    "session.orchestration_exclude": "reversible",
    "session.archive": "destructive",  # stops its agent
    "session.close": "destructive",  # stops its agent / worker
    "sessions.archive_older": "destructive",
    "session.review": "external",  # its transcript goes to the AI endpoint
    "session.review_include": "external",  # …and keeps going there
    "session.orchestration_include": "widens",  # mission control may act on it again
    # projects
    "project.rename": "reversible",
    "project.color": "reversible",
    "project.unarchive": "reversible",
    "project.default": "reversible",
    "project.create": "reversible",
    "project.add_folder": "reversible",
    "project.remove_folder": "reversible",
    "project.default_folder": "reversible",
    "project.archive": "destructive",  # stops its sessions' agents
    "project.delete": "destructive",
    "projects.auto_sort": "external",
    # the Library
    "playbook.create": "reversible",
    "playbook.duplicate": "reversible",
    "playbook.edit": "destructive",  # saves over the version it read
    "skill.create": "reversible",
    "skill.import": "reversible",
    "skill.edit": "reversible",  # saves a new copy
    "automation.create": "reversible",  # saved switched off
    "automation.edit": "starts",  # changes what an enabled automation's next runs tell agents
    "automation.widen": "widens",
    "automation.enable": "widens",  # runs agents unattended
    "specialist.create": "reversible",
    "specialist.edit": "reversible",  # a new version
    "specialist.archive": "reversible",
    "specialist.unarchive": "reversible",
    "secret.create": "reversible",
    "secret.create_project": "reversible",
    "secret.create_file": "reversible",
    "secret.access_off": "reversible",
    "secret.access_on": "widens",
    "secret.bind": "widens",
    "secret.store_attachment": "destructive",  # removes the chat copy
    # missions (drafts; approving a plan stays the Commander's own)
    "team.assemble": "reversible",
    "team.assign": "reversible",
    "team.set_override": "reversible",
    "team.clear_override": "reversible",
    "mission.create": "reversible",
    "mission.create_local": "reversible",
    "mission.plan": "reversible",
    # project memory
    "memory.edit": "starts",  # text the project's agents read
    "memory.delete": "destructive",
    # MCP servers
    "mcp.create": "widens",
    "mcp.install": "external",
    "mcp.check": "external",
    "mcp.enable": "widens",
}


class AuthorityError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def class_of(op_id: str) -> str:
    """The class of a catalog operation; an unknown one is ``widens`` (never acts)."""
    return CLASS_OF.get(op_id, "widens")


def _coerce(raw: object) -> tuple[dict[str, str], int]:
    classes = {c: ASK for c in CLASSES}
    revision = 0
    if isinstance(raw, dict):
        stored = raw.get("classes")
        if isinstance(stored, dict):
            for c in CLASSES:
                if stored.get(c) == ACT:
                    classes[c] = ACT
        rev = raw.get("revision")
        if isinstance(rev, int) and not isinstance(rev, bool) and rev >= 0:
            revision = rev
    return classes, revision


def preset_of(classes: dict[str, str]) -> str:
    for name, values in PRESETS.items():
        if all(classes.get(c) == v for c, v in values.items()):
            return name
    return "custom"


@dataclass(frozen=True)
class Snapshot:
    """The authority a turn began under: its revision and the classes then."""

    revision: int
    classes: tuple[tuple[str, str], ...]

    def setting(self, klass: str) -> str:
        return dict(self.classes).get(klass, ASK)

    @property
    def preset(self) -> str:
        return preset_of(dict(self.classes))


def snapshot(path: Path | None = None) -> Snapshot:
    """The block as stored now. An unreadable prefs file reads as Ask first. Blocking."""
    try:
        classes, revision = _coerce(prefs.read_block(KEY, path))
    except Exception:  # noqa: BLE001 — fail closed: nothing acts
        classes, revision = {c: ASK for c in CLASSES}, -1
    return Snapshot(revision, tuple(classes.items()))


def acts(snap: Snapshot, klass: str) -> bool:
    """Whether ``klass`` acts under ``snap`` in this version."""
    return klass in ACTABLE and snap.setting(klass) == ACT and snap.revision >= 0


def view(path: Path | None = None) -> dict:
    snap = snapshot(path)
    classes = dict(snap.classes)
    return {
        "classes": classes,
        "preset": preset_of(classes),
        "revision": max(snap.revision, 0),
        "actable": [c for c in CLASSES if c in ACTABLE],
        "risky": [c for c in CLASSES if c in RISKY],
    }


def update(body: object, path: Path | None = None) -> dict:
    """Apply the Commander's write: ``{"revision", "preset"}`` or ``{"revision", "classes"}``,
    compare-and-set on the revision under the prefs lock. Raises :class:`AuthorityError`."""
    if not isinstance(body, dict) or set(body) - {"revision", "preset", "classes"}:
        raise AuthorityError(422, "send revision with preset or classes")
    rev = body.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise AuthorityError(422, "revision must be the revision you read")
    if ("preset" in body) == ("classes" in body):
        raise AuthorityError(422, "send either preset or classes")
    if "preset" in body:
        if not isinstance(body["preset"], str) or body["preset"] not in PRESETS:
            raise AuthorityError(422, "preset must be ask_first, standard or full")
        patch = dict(PRESETS[body["preset"]])
    else:
        raw = body["classes"]
        if (
            not isinstance(raw, dict)
            or not raw
            or set(raw) - set(CLASSES)
            or any(v not in (ASK, ACT) for v in raw.values())
        ):
            raise AuthorityError(422, "classes maps " + ", ".join(CLASSES) + " to ask or act")
        patch = dict(raw)
    stale = {"v": False}

    def merge(cur):
        classes, revision = _coerce(cur)
        if revision != rev:
            stale["v"] = True
            return cur
        classes.update(patch)
        return {"classes": classes, "revision": revision + 1}

    prefs.mutate_block(KEY, merge, path)
    if stale["v"]:
        raise AuthorityError(409, "authority was changed elsewhere; reload and choose again")
    return view(path)


# ---- revocation ---------------------------------------------------------------------------------

_LOGOUT_EPOCH = {"n": 0}


def logout_epoch() -> int:
    return _LOGOUT_EPOCH["n"]


async def logged_out() -> None:
    """A logout hook (`routes/auth.py`): every turn begun before it acts no more."""
    _LOGOUT_EPOCH["n"] += 1


def _always() -> bool:
    return True


@dataclass(frozen=True)
class ActGrant:
    """What an act is admitted under (#2390): the authority the turn began under, the logout
    epoch then, and whether the channel it was asked on is still live (a call's own check; a
    typed turn has none). Held in memory only, by the turn. :meth:`admits` is the one check, run
    at the claim and again immediately before the effect; a later effect (A1b's background sends)
    reuses the same grant and check."""

    snapshot: Snapshot
    epoch: int
    live: Callable[[], bool] = field(default=_always)

    @classmethod
    def now(cls, live: Callable[[], bool] | None = None) -> ActGrant:
        return cls(snapshot(), logout_epoch(), live or _always)

    def admits(self, klass: str) -> str:
        """Empty when ``klass`` may act now, else why not (said to HQ; the card waits). Blocking but
        cheap: one small prefs read, so it can sit right before the effect with no await."""
        if not acts(self.snapshot, klass):
            return "the Commander's Authority setting asks first for this"
        now = snapshot()
        if now.revision != self.snapshot.revision or not acts(now, klass):
            return "the Commander changed the Authority setting during this question"
        if logout_epoch() != self.epoch:
            return "the Commander signed out after this question began"
        try:
            live = bool(self.live())
        except Exception:  # noqa: BLE001 — fail closed
            live = False
        if not live:
            return "the call this was asked on has ended"
        return ""
