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
import logging
import re
from typing import TYPE_CHECKING

from . import asgi_call
from .ask_library import LIST_MAX, Refused, _secrets, _short

if TYPE_CHECKING:
    from .ask_operations import Operation

_log = logging.getLogger(__name__)

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
    if any(not c.isprintable() and c != "\n" for c in v):
        raise _ops().Refused("a secret holds printable text and line breaks only")
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


# ---- moving a file shared as a secret into the Library (#1786 P4b) --------------------------


def attachment_facts(cid: str, aid: str) -> dict | None:
    """What a store-attachment card shows and binds: the shared file's name and size, while it is
    still in the chat as a SECRET. None when it is gone, taken back or was not shared as one."""
    from . import ask_attachments

    try:
        if not ask_attachments.read_secret(cid, aid):
            return None
        fd, size = ask_attachments.open_blob(cid, aid)
    except ask_attachments.AttachmentRefused:
        return None
    import os

    os.close(fd)
    name, _kind = ask_attachments.read_meta(cid, aid)
    return {"name": name[:120], "size": int(size)}


def _full_record(vid: str) -> dict | None:
    from . import template_vars

    return next((r for r in template_vars._read_strictly_full() if r.get("id") == vid), None)


def _committed_rev(vid: str) -> int | None:
    rec = _full_record(vid)
    if rec is None or rec.get("kind") != "secret_file" or "file" not in rec:
        return None
    return int(rec["file"]["rev"])


def _verify_and_release(cid: str, aid: str, committed: dict, data: bytes) -> str:
    """Verify, then release, with the store's write fences HELD across both (Hermes on #1816):
    the record this move committed is still there at the revision it committed and opens to
    exactly the chat copy's bytes — and nothing can delete or replace it before the chat copy is
    released. ``"released"`` / ``"gone"`` (already released) / ``"retry"`` / ``"mismatch"``."""
    from . import ask_attachments, ask_store, template_vars

    if not ask_store.valid_cid(cid):
        return "gone"
    # Lock order: the conversation's attachments fence FIRST, then the store's fences — the order
    # `ask_orchestrator.begin` already uses (attachments fence held across an append that takes
    # the registration fence). The reverse deadlocked a move against a message being sent.
    with ask_attachments.attachments_fence(cid), template_vars.write_fences_held():
        rec = _full_record(committed["id"])
        if (
            rec is None
            or rec.get("kind") != "secret_file"
            or rec.get("file", {}).get("rev") != committed["rev"]
            or template_vars.file_bytes(rec, checked=True) != data
        ):
            return "mismatch"
        return ask_attachments.release_secret_held(cid, aid)


def _digest_of(data: bytes) -> str:
    """A keyed digest of the moved bytes (never the bytes, never a bare hash)."""
    from . import template_secrets

    return template_secrets.keyed_digest("ask-secret-move", data.hex())


def _intact(committed: dict) -> bool:
    """The record this move committed is still there, at its revision, and its sealed bytes open
    (authenticated) to exactly what was moved — proof of custody without the chat copy."""
    from . import template_vars

    rec = _full_record(committed["id"])
    if rec is None or rec.get("kind") != "secret_file" or "file" not in rec:
        return False
    if rec["file"]["rev"] != committed["rev"]:
        return False
    data = template_vars.file_bytes(rec, checked=True)
    return data is not None and _digest_of(data) == committed["digest"]


async def _note(pid: str | None, cid: str, committed: dict) -> None:
    """Note what the move committed on its own card entry BEFORE the next step, so a stop (or a
    crash) between steps is finished from there, never left half done (`ask_proposals.recover`)."""
    from . import ask_proposals

    if pid:
        await asyncio.to_thread(ask_proposals._update, cid, pid, committed=dict(committed))


async def _move(cid: str, aid: str, f: dict, x: dict, app, operator) -> dict:
    """`_move_steps`, with one guarantee on top: once the move has noted a record it created, an
    unexpected error (the Library briefly unreadable, an in-process call that raised) leaves the
    card `unsettled` — Finish resumes from the note — never a dead `failed`. Before any note it
    is an ordinary failure: nothing was created."""
    noted: dict = {}
    try:
        return await _move_steps(cid, aid, f, x, app, operator, noted)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a move with a noted record must stay finishable
        if not noted.get("committed"):
            raise
        _log.warning("secret move: stopped after its note (%s)", type(e).__name__)
        return {
            "state": _ops().UNSETTLED,
            "outcome": f"Stored in the Library as “{f['secret_name']}”, but the move stopped "
            "before it finished. Confirm again to finish.",
            "committed": noted["committed"],
        }


async def _move_steps(cid: str, aid: str, f: dict, x: dict, app, operator, noted: dict) -> dict:
    """The custody transfer: create → note → upload → note → verify and release under the
    attachments fence and the store's write fences → Done.

    Provenance is the record THIS move created (``committed``: its id, its file revision, a keyed
    digest of the moved bytes), noted on the card's own entry as soon as the record exists and
    again once the upload landed — never a name or matching bytes. A first run refuses any record
    that already has the name. A resumed run (an ``unsettled`` card confirmed again, or a move a
    stop interrupted after it noted its record) continues from that record: it uploads if the
    upload had not landed, and finishes. With the chat copy already released it says Done only if
    the committed record is still there at its revision and opens to the moved bytes. Done appears
    only once the chat copy is released."""
    from . import ask_attachments, vault_files

    ops = _ops()
    name = f["secret_name"]
    shown = f"“{name}”"
    pid = f.get("_pid")
    committed = f.get("_committed") if f.get("_resumed") else None
    if committed is not None:
        noted["committed"] = committed

    def failed(why: str) -> dict:
        return {"state": ops.FAILED, "outcome": why}

    def unsettled(why: str) -> dict:
        return {"state": ops.UNSETTLED, "outcome": why, "committed": committed}

    try:
        data = await asyncio.to_thread(
            ask_attachments.secret_bytes, cid, aid, vault_files.MAX_BYTES
        )
    except ask_attachments.AttachmentRefused as e:
        if committed is not None and committed["rev"] > 0:
            if await asyncio.to_thread(_intact, committed):
                return _moved_done(shown, await asyncio.to_thread(_full_record, committed["id"]))
            return failed(
                f"The chat copy was already released, but the Library's {shown} was changed, "
                "removed or cannot be read. Check it in the Library."
            )
        return failed(f"Not stored: {e.detail}. The file stays in the chat as a secret.")
    digest = await asyncio.to_thread(_digest_of, data)
    if committed is not None and committed["digest"] != digest:
        return failed("The shared file changed since the move began, so nothing moved.")

    async def upload(rec: dict) -> dict | None:
        """Upload the bytes into ``rec``; None on a refusal (the record is taken back)."""
        up = await asgi_call.call(
            app,
            operator,
            "PUT",
            f"/api/variables/{rec['id']}/file",
            None,
            query=f"expected_updated_at={rec['updated_at']!r}",
            form=("file", x.get("name") or "file", data),
        )
        if up.status == 200:
            return None
        gone = await asgi_call.call(
            app,
            operator,
            "DELETE",
            f"/api/variables/{rec['id']}",
            None,
            query=f"expected_updated_at={rec['updated_at']!r}",
        )
        left = (
            ""
            if gone.status == 204
            else f" An empty {shown} was left in the Library; delete it there."
        )
        return failed(
            f"Not stored: {up.detail or 'refused'}. The file stays in the chat as a secret." + left
        )

    body = {
        "name": name,
        "kind": "secret_file",
        "definition": {"value_type": "text", "label": name, "help": f.get("description") or ""},
    }

    async def create() -> dict | None:
        """Create the record under the id the move NOTED first; None on a refusal."""
        r = await asgi_call.call(
            app, operator, "POST", "/api/variables", {**body, "id": committed["id"]}
        )
        return r.body if r.status == 201 and isinstance(r.body, dict) else None

    if committed is None:
        try:
            taken = any(v.get("name") == name for v in await _library(app, operator))
        except Refused:
            return failed("The Library could not be read just now. Nothing was moved.")
        if taken:
            return failed(f"The name {shown} is already used in the Library. Nothing was moved.")
        # The id is chosen and NOTED before the record exists (Hermes on #1816): a stop at any
        # point after this note is finished from it; a stop before it created nothing.
        from . import variable_document

        committed = {"id": variable_document.new_id(), "rev": 0, "digest": digest}
        await _note(pid, cid, committed)
        noted["committed"] = committed
        created = await create()
        if created is None:
            return failed(
                "Not stored: the Library refused it. The file stays in the chat as a secret."
            )
        refused = await upload(created)
        if refused is not None:
            return refused
    elif committed["rev"] == 0:
        # Resumed from the note: create the record if it never landed, upload if that never did.
        row = next(
            (v for v in await _library(app, operator) if v.get("id") == committed["id"]), None
        )
        if row is None:
            if any(v.get("name") == name for v in await _library(app, operator)):
                return failed(
                    f"The name {shown} was taken in the Library meanwhile, so the chat copy was "
                    "kept."
                )
            row = await create()
            if row is None:
                return failed(
                    "Not stored: the Library refused it. The file stays in the chat as a secret."
                )
        if not row.get("set"):
            if row.get("agent_access"):
                # Someone switched agent access on for the still-empty record: filling it now
                # would hand agents the file on the strength of THIS card, which never granted
                # access (Hermes on #1816). The upload below is pinned to this row's revision,
                # so a switch after this check makes it conflict rather than slip through.
                return unsettled(
                    f"Agent access was switched on for the empty {shown} before the file was in "
                    "it, so nothing moved and the chat copy was kept. Turn its access off in the "
                    "Library, then confirm again."
                )
            refused = await upload(row)
            if refused is not None:
                return refused
    if committed["rev"] == 0:
        rev = await asyncio.to_thread(_committed_rev, committed["id"])
        if rev is None:
            return failed(
                f"{shown} could not be read back from the Library, so the chat copy was kept. "
                "Check it in the Library."
            )
        committed = {**committed, "rev": rev}
        await _note(pid, cid, committed)
        noted["committed"] = committed
    # Committed: from here the chat copy stays until the verify has passed under the fences.
    try:
        released = await asyncio.to_thread(_verify_and_release, cid, aid, committed, data)
    except Exception as e:  # noqa: BLE001 — the commit stands; verify and release are retried
        _log.warning("secret move: verify/release failed (%s)", type(e).__name__)
        return unsettled(
            f"Stored in the Library as {shown}, but it could not be checked and the chat copy "
            "deleted yet. Confirm again to finish."
        )
    if released == "mismatch":
        return failed(
            f"The Library's copy of {shown} was changed or removed before the move finished, so "
            "the chat copy was kept. Check it in the Library."
        )
    if released == "retry":
        return unsettled(
            f"Stored in the Library as {shown}, but the chat copy could not be deleted yet. "
            "Confirm again to finish."
        )
    return _moved_done(shown, await asyncio.to_thread(_full_record, committed["id"]) or {})


def _moved_done(shown: str, rec: dict) -> dict:
    access = "on" if rec.get("agent_access") else "off"
    return {
        "state": _ops().DONE,
        "outcome": f"Stored in the Library as {shown}. The chat copy is deleted. Agent access is "
        f"{access}.",
    }


def file_input(v: object) -> dict:
    """The file the operator chose on a create card (#1786's file mode): ``{"name", "data"}``,
    read by the decide route from its multipart body with the Library upload route's own bounds.
    It exists only in that request and the in-process upload; nothing stores it."""
    from . import vault_files

    if not isinstance(v, dict) or set(v) != {"name", "data"}:
        raise _ops().Refused("choose a file on the card")
    name, data = v["name"], v["data"]
    if not isinstance(name, str) or not name or not isinstance(data, bytes) or not data:
        raise _ops().Refused("choose a file on the card")
    if len(data) > vault_files.MAX_BYTES:
        raise _ops().Refused(f"a secret file is at most {vault_files.MAX_BYTES // 1024} KB")
    if len(name) > 255:
        raise _ops().Refused("that file name is too long")
    return {"name": name, "data": data}


async def _create_file(cid: str, target: str, f: dict, x: dict, app, operator) -> dict:
    """Create a secret file from the file chosen on the card: the record, then its upload, both
    through the Library's own routes. An upload the Library refuses takes the empty record back,
    so a refusal leaves nothing behind. Agent access stays off."""
    ops = _ops()
    name, upload = f["secret_name"], f["file"]
    body = {
        "name": name,
        "kind": "secret_file",
        "definition": {"value_type": "text", "label": name, "help": f.get("description") or ""},
    }
    r = await asgi_call.call(app, operator, "POST", "/api/variables", body)
    if r.status != 201 or not isinstance(r.body, dict):
        return {"state": ops.FAILED, "outcome": f"Not stored: {r.detail or 'refused'}. Nothing "
                "changed."}  # fmt: skip
    rec = r.body
    up = await asgi_call.call(
        app,
        operator,
        "PUT",
        f"/api/variables/{rec['id']}/file",
        None,
        query=f"expected_updated_at={rec['updated_at']!r}",
        form=("file", upload["name"], upload["data"]),
    )
    if up.status == 200:
        return {"state": ops.DONE, "outcome": "Stored. Agent access is off."}
    gone = await asgi_call.call(
        app,
        operator,
        "DELETE",
        f"/api/variables/{rec['id']}",
        None,
        query=f"expected_updated_at={rec['updated_at']!r}",
    )
    if gone.status == 204:
        return {"state": ops.FAILED, "outcome": f"Not stored: {up.detail or 'refused'}. Nothing "
                "changed."}  # fmt: skip
    return {
        "state": ops.FAILED,
        "outcome": f"Not stored: {up.detail or 'refused'}. An empty “{name}” was left in the "
        "Library; delete it there.",
    }


def _describe_create_file(f: dict, x: dict) -> str:
    return (
        f"Stores a new secret file “{f['secret_name']}” in the Library. Choose the file below: it "
        "goes straight to the encrypted store, and HQ never sees it. Agents get no access until "
        "you turn that on."
    )


def _describe_move(f: dict, x: dict) -> str:
    return (
        f"Moves the file “{x.get('name') or 'file'}” you shared as a secret into the Library as "
        f"“{f['secret_name']}”. HQ never sees its contents. Once the Library holds it, the copy "
        "in this chat is deleted. Agents get no access until you turn that on."
    )


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
        Operation(
            "secret.create_file",
            GROUP,
            "create_file",
            "Store secret file",
            "Store",
            "reversible",
            "none",
            ("secret_name", "description"),
            # Documents where it starts; `_create_file` runs create → upload.
            lambda t, f, x: ("POST", "/api/variables", None),
            _describe_create_file,
            lambda st, b, x: _ops()._plain(st, b, "Stored."),
            inputs=("file",),
            run=_create_file,
        ),
        Operation(
            "secret.store_attachment",
            GROUP,
            "store_attachment",
            "Store shared file as secret",
            "Store",
            "reversible",
            "attachment",
            ("secret_name", "description"),
            # Documents the route the move ends on; `_move` runs the steps (create, upload,
            # verify, release) through the same Library routes.
            lambda t, f, x: ("PUT", f"/api/variables/{t}/file", None),
            _describe_move,
            lambda st, b, x: _ops()._plain(st, b, "Stored."),
            run=_move,
        ),
    )


async def prepare(
    action: object,
    args: dict,
    *,
    known: dict,
    app,
    operator,
    cid: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    """What `propose_secret` stages: the operation, its target, title and fields. Raises
    ``Refused``. A secret must be one ``list_secrets`` returned in this turn; a shared file one
    of THIS turn's attachments, by its index in the message's list."""
    op = _ops().by_action(GROUP, action) if isinstance(action, str) else None
    if op is None:
        raise Refused(
            "action must be create, access_on, access_off or store_attachment", "bad_arguments"
        )
    if "value" in args:
        raise Refused(
            "refused: HQ never carries a secret's value — the operator types it on the card",
            "never",
        )
    if op.id == "secret.store_attachment":
        return await _prepare_move(op, args, cid, attachments or [], app, operator)
    if op.id == "secret.create" and args.get("kind") == "file":
        op = _ops().CATALOG["secret.create_file"]
    elif args.get("kind") not in (None, "text"):
        raise Refused("kind is text or file", "bad_arguments")
    if op.id in ("secret.create", "secret.create_file"):
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


async def _prepare_move(op, args: dict, cid: str, attachments: list[dict], app, operator):
    from . import vault_files

    idx = args.get("attachment_index")
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(attachments):
        raise Refused(
            "refused: attachment_index is the place of a file shared with THIS message (0 is "
            "the first)",
            "bad_index",
        )
    a = attachments[idx]
    if not a.get("secret"):
        raise Refused(
            "refused: that file was not shared as a secret, so HQ already saw it — ask the "
            "operator to share it again with Secret on",
            "not_secret",
        )
    if int(a.get("size") or 0) > vault_files.MAX_BYTES:
        raise Refused(
            f"refused: a secret file is at most {vault_files.MAX_BYTES // 1024} KB; this one is "
            "larger, so it stays in the chat as a secret",
            "too_large",
        )
    try:
        name = secret_name(args.get("name"))
        fields = {"secret_name": name, "description": description(args.get("description", ""))}
    except _ops().Refused as e:
        raise Refused(str(e), "bad_arguments") from None
    if any(v.get("name") == name for v in await _library(app, operator)):
        raise Refused(f"refused: a secret named {name} already exists", "exists")
    return {"op": op, "target": a["id"], "title": name, "fields": fields}


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
