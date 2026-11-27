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

**What can act in this version** is :data:`ACTABLE` (A1: reversible; A1b, #2391: starts or
sends; A1c-destructive, #2396: destructive, RISKY). A stored ``act`` for another class is the
Commander's own configuration, kept and shown, and changes nothing until the child of #2070 that
adds its class here ships.

**The effective class** is the action's class when it runs (:func:`effective_class`): the catalog
table for an operation, except where the action's own facts decide it (an automation run now is
*external* when its approved scope reaches outside, *widens* when it starts a bypass session, and
*external* when its scope can't be read); a session start is *starts*, or *widens* when the tap
would turn permission bypass on; a connector message is *starts*; a drive send or adopt (#2397) is
*starts*, or *external* in a session that skips permission prompts. It is derived at staging, at the
claim and at the effect boundary, so a change in between can only make the act ask.

**D4, after untrusted text** (#2391): once a turn or its conversation read agent- or
outside-authored text (A1's recorded taint), a class in :data:`TAINT_GATED` asks first unless the
Commander switched that class's ``untrusted`` switch on (``{"untrusted": {"starts": true}}``, off
by default, RISKY). A missing, malformed or non-boolean value reads as off. Screen and reversible
act in a tainted turn (epic §1).

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
#: The classes HQ can act on in this version (#2070 A1, A1b #2391, A1c-destructive #2396). The
#: other RISKY classes (external, widens) follow in their own children.
ACTABLE = frozenset({"reversible", "starts", "destructive"})
#: The classes that ask first after HQ read untrusted text unless their switch is on (D4, #2391).
#: Screen and reversible act in a tainted turn (epic §1); every later class is gated.
TAINT_GATED = frozenset(CLASSES) - {"reversible"}
#: The classes whose "also after untrusted text" switch exists in this version: every gated class
#: that can act (#2391 starts, #2396 destructive); external and widens add theirs with their acting.
UNTRUSTED_SWITCHES = ("starts", "destructive")
#: Why a gated class asks first in a tainted conversation (D4), said on the card and to HQ.
TAINT_REASON = "HQ read text from a session or connector in this conversation, so this asks first"
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
    # driving an API session HQ drives (#2397): a turn, a stop, a queued turn taken back, and
    # taking over driving. A send or an adopt into a session that skips permission prompts is
    # *external* (`effective_class`: classed with what that session may do).
    "session.send": "starts",
    "session.interrupt": "starts",
    "session.withdraw": "starts",
    "session.adopt": "starts",
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
    # starts its approved action once now; its scope decides (`effective_class`, #2391)
    "automation.run_now": "starts",
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
    # types into a session the mission holds; the route keeps the mission's own guards (#2391)
    "mission.relay": "starts",
    # project memory
    "memory.edit": "starts",  # text the project's agents read
    "memory.delete": "destructive",
    # MCP servers
    "mcp.create": "widens",
    "mcp.install": "external",
    "mcp.check": "external",
    "mcp.enable": "widens",
}


#: Every proposal kind's class (#2391), pinned against `ask_store.PROPOSAL_KINDS`. An operation's
#: is its catalog entry's (`CLASS_OF`); a nudge (`send_nudge`) keeps its own authority (#1960) and
#: never takes this path. The kinds' dynamic rule is `effective_class`.
KIND_CLASS: dict[str, str | None] = {
    "session": "starts",  # widens when the tap would turn permission bypass on
    "message": "starts",  # a connector message the Commander allowed the recipient of
    "operation": None,  # the catalog entry's class
    "nudge": None,  # its own authority (#1960), not this setting
}
#: An automation's approved action kinds that stay inside BattleLab when run now (#2391). Anything
#: else (a plugin action: a webhook call, a push, a share), an unknown kind, or a scope that can't
#: be read is external.
_INSIDE_ACTIONS = frozenset({"start_session", "start_mission", "send_to_session", "tell_hq"})
#: Drive entries whose class the session's permission mode decides (#2397): *starts*, or
#: *external* in a session that skips permission prompts (the card's `bypass` fact, re-read at the
#: claim and at the boundary); a missing fact reads as the stronger class.
BYPASS_EXTERNAL = frozenset({"session.send", "session.adopt"})


def automation_reach(scope: object) -> str:
    """The class an automation's run now takes from its approved scope (`GET /api/automations/{id}`
    `scope`): *starts* for an action inside BattleLab, *widens* for a session it starts with
    permission bypass, *external* for anything else and for a missing or unreadable scope."""
    if not isinstance(scope, dict):
        return "external"
    action = scope.get("action")
    if not isinstance(action, str) or action not in _INSIDE_ACTIONS:
        return "external"
    if action == "start_session" and scope.get("bypass") is not False:
        return "widens"
    return "starts"


def effective_class(row: dict, facts: dict | None = None) -> str:
    """The class of what ``row`` (a sidecar entry) would do NOW (#2391, epic §1's effective-action
    rule). ``facts`` are an operation's facts as read at this boundary (its staged facts when
    omitted). Fails toward the stronger class: anything unrecognised is *widens*."""
    kind = row.get("kind")
    if kind == "operation":
        op_id = str(row.get("op") or "")
        if op_id == "automation.run_now":
            basis = facts if facts is not None else row.get("facts")
            reach = basis.get("reach") if isinstance(basis, dict) else None
            return reach if reach in ("starts", "external", "widens") else "external"
        if op_id in BYPASS_EXTERNAL:
            basis = facts if facts is not None else row.get("facts")
            bypass = basis.get("bypass") if isinstance(basis, dict) else None
            return class_of(op_id) if bypass is False else "external"
        return class_of(op_id)
    if kind == "session":
        # A start the tap would make with permission bypass on (the card's starting value) is
        # widens: HQ never starts it without a card (A1c). An API card never bypasses (#2014).
        if row.get("runtime") != "api" and row.get("bypass_default") is not False:
            return "widens"
        return "starts"
    if kind == "message":
        return "starts"
    return "widens"


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


def _coerce_untrusted(raw: object) -> dict[str, bool]:
    """The D4 switches (#2391). Fail closed: only a stored ``true`` is on; a missing block, a
    missing class, ``"true"``, ``1`` or any other value is off."""
    stored = raw.get("untrusted") if isinstance(raw, dict) else None
    return {c: isinstance(stored, dict) and stored.get(c) is True for c in UNTRUSTED_SWITCHES}


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
    #: The D4 switches then (#2391): ``(class, on)``. Off unless stored ``true``.
    untrusted: tuple[tuple[str, bool], ...] = ()

    def setting(self, klass: str) -> str:
        return dict(self.classes).get(klass, ASK)

    def after_untrusted(self, klass: str) -> bool:
        return dict(self.untrusted).get(klass) is True

    @property
    def preset(self) -> str:
        return preset_of(dict(self.classes))


def snapshot(path: Path | None = None) -> Snapshot:
    """The block as stored now. An unreadable prefs file reads as Ask first. Blocking."""
    try:
        raw = prefs.read_block(KEY, path)
        classes, revision = _coerce(raw)
        untrusted = _coerce_untrusted(raw)
    except Exception:  # noqa: BLE001 — fail closed: nothing acts
        classes, revision = {c: ASK for c in CLASSES}, -1
        untrusted = {c: False for c in UNTRUSTED_SWITCHES}
    return Snapshot(revision, tuple(classes.items()), tuple(untrusted.items()))


def acts(snap: Snapshot, klass: str) -> bool:
    """Whether ``klass`` acts under ``snap`` in this version."""
    return klass in ACTABLE and snap.setting(klass) == ACT and snap.revision >= 0


def taint_blocks(snap: Snapshot, klass: str, tainted: bool) -> bool:
    """D4 (#2391): whether a tainted turn keeps ``klass`` on a card under ``snap``."""
    return bool(tainted) and klass in TAINT_GATED and not snap.after_untrusted(klass)


def view(path: Path | None = None) -> dict:
    snap = snapshot(path)
    classes = dict(snap.classes)
    return {
        "classes": classes,
        "preset": preset_of(classes),
        "revision": max(snap.revision, 0),
        "actable": [c for c in CLASSES if c in ACTABLE],
        "risky": [c for c in CLASSES if c in RISKY],
        # D4 (#2391): the "also after untrusted text" switches, and which classes have one.
        "untrusted": dict(snap.untrusted),
        "untrusted_classes": list(UNTRUSTED_SWITCHES),
    }


def update(body: object, path: Path | None = None) -> dict:
    """Apply the Commander's write: ``{"revision", "preset"}``, ``{"revision", "classes"}`` or
    ``{"revision", "untrusted": {<class>: bool}}`` (D4, #2391), compare-and-set on the revision
    under the prefs lock. A preset sets the classes only; it never turns a D4 switch on or off.
    Raises :class:`AuthorityError`."""
    if not isinstance(body, dict) or set(body) - {"revision", "preset", "classes", "untrusted"}:
        raise AuthorityError(422, "send revision with preset, classes or untrusted")
    rev = body.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        raise AuthorityError(422, "revision must be the revision you read")
    if sum(k in body for k in ("preset", "classes", "untrusted")) != 1:
        raise AuthorityError(422, "send one of preset, classes or untrusted")
    switches: dict[str, bool] = {}
    patch: dict[str, str] = {}
    if "untrusted" in body:
        raw = body["untrusted"]
        if (
            not isinstance(raw, dict)
            or not raw
            or set(raw) - set(UNTRUSTED_SWITCHES)
            or any(not isinstance(v, bool) for v in raw.values())
        ):
            raise AuthorityError(
                422, "untrusted maps " + ", ".join(UNTRUSTED_SWITCHES) + " to true or false"
            )
        switches = dict(raw)
    elif "preset" in body:
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
        untrusted = _coerce_untrusted(cur)
        classes.update(patch)
        untrusted.update(switches)
        return {"classes": classes, "untrusted": untrusted, "revision": revision + 1}

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

    def admits(self, klass: str, tainted: bool = False) -> str:
        """Empty when ``klass`` may act now, else why not (said to HQ; the card waits). ``tainted``:
        the turn read untrusted text (D4, #2391). Blocking but cheap: one small prefs read, so it
        can sit right before the effect with no await."""
        if not acts(self.snapshot, klass):
            return "the Commander's Authority setting asks first for this"
        if taint_blocks(self.snapshot, klass, tainted):
            return TAINT_REASON
        now = snapshot()
        if now.revision != self.snapshot.revision or not acts(now, klass):
            return "the Commander changed the Authority setting during this question"
        if taint_blocks(now, klass, tainted):
            return TAINT_REASON
        if logout_epoch() != self.epoch:
            return "the Commander signed out after this question began"
        try:
            live = bool(self.live())
        except Exception:  # noqa: BLE001 — fail closed
            live = False
        if not live:
            return "the call this was asked on has ended"
        return ""
