"""HQ writes the Library: playbooks, skills and automations (#1611, #1558 P4).

HQ reads the Library the way the Library pages read it (the GET routes, in-process, as the
operator), and proposes a save from the closed catalog in :mod:`ask_operations` — the operator
confirms a card and the SAME route the Library's Save button calls runs. Every guard is the route's
own: the playbook store's validator and revision, the skill store's NOREPLACE publish, the
automation store's revision and consent scope.

**What HQ drafted is shown verbatim, and is exactly what is saved.** A draft (a playbook's whole
tree, a skill's files, an automation's config) is far larger than a card record may hold, so it is
written ONCE to its own file through the store boundary (``ask_store.write_draft``): every text in
it goes through the secret guard WHOLE, before any cut, and a text the guard would change refuses
the draft rather than storing it altered. Its sha256 is part of the card's keyed binding, so a
draft changed on disk after staging makes the tap ``stale``.

**Staging validates without writing.** The model's files go through the store's own validator
before a card exists: an invalid draft returns to the model as a tool error with the validator's
words and never reaches a card.

**Targets are this turn's.** A playbook, skill or automation is named by an id one of this turn's
read tools returned; an automation's project, agent and session come from this turn's lists — the
model never writes a project id, folder path, engine id or session key into a config.

**HQ never widens permission or autonomy** (the #1558 never-list): a config with permission bypass
on, or mission autonomy ``dispatch_auto_choose``, is refused at staging for create, edit and
enable. The operator can still set either in the Library. The one exception is a library secret's
agent access (`ask_secrets`, #1786): the operator approved HQ PROPOSING `secret.access_on` (#1786,
comment 113807) as a card they tap — never voice-confirmed, never automatic, stale when the bound
projects change.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import time

from . import asgi_call, ask_store

#: Rows a list tool returns at most.
LIST_MAX = 60
#: Characters of ONE file a detail tool sends the model, after the whole file was guarded. A file
#: longer than this is never edited by HQ: it could not have read it whole (`_merge`).
FILE_CHARS_MAX = 8_000
#: Characters of all files one detail call sends the model — under HQ's tool-result cap
#: (`ask_orchestrator.TOOL_RESULT_MAX`) with room for the names around them.
DETAIL_CHARS_MAX = 9_000
#: Characters of the authoring vocabulary sent to the model.
SCHEMA_CHARS_MAX = 10_000
#: Lines of a file the card shows before "Show all" — the card's own rule, mirrored for its counts.
PREVIEW_LINES = 12
#: The most files one draft may hold (the skill store's own cap is 64).
DRAFT_FILES_MAX = 64

GROUPS = ("playbook", "skill", "automation")
#: Draft PATHS whose values the SERVER issued — read from a store or route, or generated here —
#: never model text: a revision, a scope digest, an origin pointer, the idempotency id, the routes'
#: rendered scope lines, and the ids in an automation's action (built from this turn's lists). The
#: draft guard skips exactly these paths; everything else, file names included, is guarded whole.
_ACTION_IDS = ("project_id", "session_key", "engine", "folder")
TRUSTED_DRAFT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("revision",),
        ("scope_digest",),
        ("origin",),
        ("operation",),
        # The automation routes' own rendered consent lines ("Permission bypass: off" reads as
        # credential-shaped to the heuristic). The model text they quote — the instruction — is
        # guarded where it lives, in `config` / `body`.
        ("scope_lines",),
        ("widened",),
        *(("config", "action", k) for k in _ACTION_IDS),
        *(("body", "action", k) for k in _ACTION_IDS),
    }
)
#: Autonomy an HQ-authored mission automation may have: `dispatch_auto_choose` answers the agents'
#: menus by itself — a widening of autonomy, which HQ never grants.
HQ_AUTONOMY = ("propose", "dispatch")


class Refused(Exception):
    def __init__(self, message: str, code: str = "bad_arguments"):
        super().__init__(message)
        self.code = code


# ---- guarded text --------------------------------------------------------------------------------


def _guarded(text: str, secrets: list[str]) -> str:
    """One text as the model may see it: the WHOLE text through the boundary, then cut."""
    return ask_store.guard_text(text, max(len(text), 1), secrets=secrets)


def _secrets() -> list[str]:
    from . import template_secrets

    try:
        return template_secrets.redaction_values()
    except template_secrets.RedactionUnavailable as e:
        raise Refused(f"refused: {e}", "not_available") from None


def _files_for_model(files: dict, secrets: list[str], path: str = "") -> dict:
    """A playbook's or skill's files for the model: every text guarded whole, then capped per file
    and in total; a non-text file is named, never sent. ``path`` reads that one file only. A file
    NAME is model-facing too: one the guard would change refuses the whole read (#1611 review)."""
    for name in files:
        if _guarded(str(name), secrets) != name:
            raise Refused(
                "refused: one of its file names contains a secret or looks like a credential; "
                "open it in the Library",
                "credential",
            )
    if path:
        if path not in files:
            raise Refused(f"it has no file {path!r}")
        files = {path: files[path]}
    out: dict[str, str] = {}
    binary: list[str] = []
    cut: list[str] = []
    room = DETAIL_CHARS_MAX
    for path in sorted(files):
        value = files[path]
        if not isinstance(value, str):
            binary.append(path)
            continue
        text = _guarded(value, secrets)
        cap = min(FILE_CHARS_MAX, room)
        if len(text) > cap:
            text = text[:cap]
            cut.append(path)
        out[path] = text
        room -= len(text)
        if room <= 0:
            cut.extend(p for p in sorted(files) if p > path and p not in cut)
            break
    return {"files": out, "binary": binary, "cut": cut}


async def _get(app, operator, path: str, what: str) -> dict:
    r = await asgi_call.call(app, operator, "GET", path, None)
    if r.truncated:
        raise Refused(f"refused: {what} is too large for HQ — open it in the Library", "too_large")
    if r.status == 404:
        raise Refused(f"refused: {what} no longer exists", "not_available")
    if r.status != 200 or not isinstance(r.body, dict):
        raise Refused(f"{what} could not be read just now", "not_available")
    return r.body


# ---- reads ---------------------------------------------------------------------------------------


def _short(text: object, cap: int, secrets: list[str]) -> str:
    """A name for the model: guarded WHOLE, then cut."""
    return _guarded(str(text or ""), secrets)[:cap]


def remember(known: dict, rid: str, seen: dict) -> None:
    """Fold what a read just saw into this turn's record of ``rid``. A list read never erases what a
    detail read recorded; a detail read of the same revision ADDS the files it showed whole, and
    one of another revision starts over (what HQ read before no longer describes it)."""
    prior = known.get(rid) or {}
    if "revision" in seen and prior.get("revision") not in (None, seen["revision"]):
        prior = {}
    merged = {**prior, **{k: v for k, v in seen.items() if k != "whole"}}
    if "whole" in seen or "whole" in prior:
        merged["whole"] = set(prior.get("whole") or ()) | set(seen.get("whole") or ())
    known[rid] = merged


async def list_playbooks(app, operator) -> tuple[list[dict], dict]:
    body = await _get(app, operator, "/api/playbooks", "the playbook list")
    secrets = await asyncio.to_thread(_secrets)
    rows, known = [], {}
    for p in (body.get("playbooks") or [])[:LIST_MAX]:
        if not isinstance(p, dict) or not isinstance(p.get("id"), str):
            continue
        known[p["id"]] = {
            "name": str(p.get("name") or p["id"]),
            "editable": bool(p.get("editable")),
        }
        rows.append(
            {
                "id": p["id"],
                "name": _short(p.get("name"), 120, secrets),
                "domain": _short(p.get("domain"), 60, secrets),
                "source": str(p.get("source") or ""),
                "editable": bool(p.get("editable")),
                "default": bool(p.get("default")),
                "valid": bool(p.get("ok", True)),
            }
        )
    return rows, known


async def playbook_detail(app, operator, pid: str, path: str = "") -> tuple[dict, dict]:
    from .playbooks import store

    with contextlib.suppress(Exception):
        store.playbook_id(pid)
    body = await _get(app, operator, f"/api/playbooks/{pid}", "that playbook")
    secrets = await asyncio.to_thread(_secrets)
    files = body.get("files") if isinstance(body.get("files"), dict) else {}
    shown = await asyncio.to_thread(_files_for_model, files, secrets, path)
    known = {
        "name": str(body.get("name") or pid),
        "editable": bool(body.get("editable")),
        # What HQ has now seen: an edit is bound to THIS revision, and may rewrite only the
        # files HQ read whole (#1611 review).
        "revision": str(body.get("revision") or ""),
        "whole": {p for p in shown["files"] if p not in shown["cut"]},
    }
    return {
        "id": pid,
        "name": _short(body.get("name"), 120, secrets),
        "editable": bool(body.get("editable")),
        "source": str(body.get("source") or ""),
        **shown,
    }, known


async def playbook_schema(app, operator) -> dict:
    body = await _get(app, operator, "/api/playbooks/authoring/schema", "the authoring schema")
    text = json.dumps(body, sort_keys=True)
    if len(text) > SCHEMA_CHARS_MAX:
        body = {"note": "the schema was cut to fit", "schema": text[:SCHEMA_CHARS_MAX]}
    return body


def _skill_name(files: object, sid: str) -> str:
    """A skill's display name: its SKILL.md front matter `name`, else its id."""
    text = files.get("SKILL.md") if isinstance(files, dict) else None
    if isinstance(text, str) and text.startswith("---"):
        for line in text.split("\n")[1:40]:
            if line.strip() == "---":
                break
            k, _, v = line.partition(":")
            if k.strip() == "name" and v.strip():
                return v.strip()
    return sid


async def list_skills(app, operator) -> tuple[list[dict], dict]:
    body = await _get(app, operator, "/api/library/skills", "the skill list")
    secrets = await asyncio.to_thread(_secrets)
    rows, known = [], {}
    for s in (body.get("skills") or [])[:LIST_MAX]:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or s.get("error"):
            continue
        name = _skill_name(s.get("files"), s["id"])
        known[s["id"]] = {"name": name}
        rows.append({"id": s["id"], "name": _short(name, 120, secrets)})
    return rows, known


async def skill_detail(app, operator, sid: str, path: str = "") -> tuple[dict, dict]:
    body = await _get(app, operator, f"/api/library/skills/{sid}", "that skill")
    secrets = await asyncio.to_thread(_secrets)
    files = body.get("files") if isinstance(body.get("files"), dict) else {}
    shown = await asyncio.to_thread(_files_for_model, files, secrets, path)
    name = _skill_name(files, sid)
    return {"id": sid, "name": _short(name, 120, secrets), **shown}, {
        "name": name,
        "revision": str(body.get("revision") or ""),
        "whole": {p for p in shown["files"] if p not in shown["cut"]},
    }


def _automation_row(a: dict, secrets: list[str]) -> dict:
    action = a.get("action") if isinstance(a.get("action"), dict) else {}
    trigger = a.get("trigger") if isinstance(a.get("trigger"), dict) else {}
    return {
        "id": a["id"],
        "name": _short(a.get("name"), 120, secrets),
        "state": str(a.get("state") or ""),
        "enabled": bool(a.get("enabled")),
        "paused": bool(a.get("paused")),
        "trigger": str(trigger.get("kind") or ""),
        "action": str(action.get("kind") or ""),
    }


async def list_automations(app, operator) -> tuple[list[dict], dict]:
    body = await _get(app, operator, "/api/automations", "the automation list")
    secrets = await asyncio.to_thread(_secrets)
    rows, known = [], {}
    for a in (body.get("automations") or [])[:LIST_MAX]:
        if not isinstance(a, dict) or not isinstance(a.get("id"), str):
            continue
        known[a["id"]] = {"name": str(a.get("name") or a["id"]), "revision": a.get("revision")}
        rows.append(_automation_row(a, secrets))
    return rows, known


async def automation_detail(app, operator, aid: str) -> tuple[dict, dict]:
    a = await _get(app, operator, f"/api/automations/{aid}", "that automation")
    secrets = await asyncio.to_thread(_secrets)
    lines = [_guarded(str(x), secrets)[:400] for x in (a.get("scope_lines") or [])[:40]]
    out = {
        **_automation_row({**a, "id": aid}, secrets),
        "trigger": a.get("trigger"),
        "policy": a.get("policy"),
        "scope_lines": lines,
        "needs_reapproval": bool(a.get("needs_reapproval")),
    }
    action = a.get("action") if isinstance(a.get("action"), dict) else {}
    out["action"] = {
        "kind": action.get("kind"),
        **({"autonomy": action.get("autonomy")} if "autonomy" in action else {}),
        **({"bypass": action.get("bypass")} if "bypass" in action else {}),
    }
    # The automation's own texts, guarded whole (a message is the operator's words).
    text = json.dumps(out, sort_keys=True, ensure_ascii=False)
    if _guarded(text, secrets) != text:
        out = {k: out[k] for k in ("id", "name", "state", "enabled", "paused", "scope_lines")}
        out["note"] = "its settings hold text that looks like a credential, so they are not shown"
    return out, {"name": str(a.get("name") or aid), "revision": a.get("revision")}


# ---- drafting ------------------------------------------------------------------------------------


def _text_files(raw: object, what: str) -> dict[str, str]:
    if not isinstance(raw, dict) or len(raw) > DRAFT_FILES_MAX:
        raise Refused(f"{what} must be an object of path → text, at most {DRAFT_FILES_MAX}")
    out: dict[str, str] = {}
    for path, text in raw.items():
        if not isinstance(path, str) or not isinstance(text, str):
            raise Refused(f"{what}: every entry is a path and its full text")
        out[path] = text
    return out


def _removals(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise Refused("remove must be a list of paths")
    return list(raw)


def _merge(current: dict, changes: dict[str, str], remove: list[str], whole: set) -> dict:
    """The tree the edit saves: the CURRENT files (a non-text file carried over as the store wrote
    it), with ``changes`` written over and ``remove`` taken out. An existing file may be rewritten
    only if HQ read it WHOLE this turn (``whole``): a rewrite of a cut file would drop what it
    never saw."""
    unknown = sorted(set(remove) - set(current))
    if unknown:
        raise Refused(f"remove names a file it doesn't have: {unknown[0]}")
    for p in changes:
        if p in current and p not in whole:
            raise Refused(
                f"refused: read {p} whole first (its detail tool, with path); HQ rewrites only "
                "files it has read in full, and a file too long for that is edited in the Library",
                "not_read",
            )
    merged = {p: v for p, v in current.items() if p not in remove}
    merged.update(changes)
    if not merged:
        raise Refused("an edit can't remove every file")
    return merged


def _validate_playbook(files: dict) -> dict:
    """The playbook store's own validator, without writing. Returns the identity it names."""
    from .playbooks import store

    try:
        tree = store.tree_from_files(files)
        pid = store._identity_id(tree)
        pb = store._validate(tree, pid)
    except store.StoreError as e:
        field = getattr(e, "field", "") or ""
        raise Refused(
            f"the playbook is not valid{f' at {field}' if field else ''}: {e}", "invalid"
        ) from None
    return {"id": pid, "name": str(pb["identity"]["name"])}


def _validate_skill(files: dict) -> None:
    from .gallery import contract
    from .gallery import files as gallery_files

    try:
        gallery_files.tree(files)
    except (contract.GalleryError, ValueError) as e:
        raise Refused(f"the skill is not valid: {e}", "invalid") from None


def _skill_id(raw: object) -> str:
    from .playbooks import store

    try:
        return store.playbook_id(raw)
    except Exception:  # noqa: BLE001 — the store's own id rule, in its own words
        raise Refused(
            "new_id must be lowercase letters, digits and dashes", "bad_arguments"
        ) from None


NO_NEWLINE = "\\ No newline at end of file"


def _diff_rows(a: str, b: str) -> list[str]:
    """A unified diff that keeps line ENDINGS: a change of only the final newline still shows,
    as diff marks it (``\\ No newline at end of file``), never as an empty diff."""
    rows: list[str] = []
    out = difflib.unified_diff(a.splitlines(True), b.splitlines(True), lineterm="", n=2)
    for i, row in enumerate(out):
        if i < 2:
            continue  # the ---/+++ header names nothing the card doesn't
        if row.startswith("@@") or row.endswith("\n"):
            rows.append(row.removesuffix("\n"))
        else:
            rows += [row, NO_NEWLINE]
    return rows


def _diff(before: dict, after: dict) -> list[dict]:
    """Per changed file: added / removed / changed, with a unified diff when both sides are text."""
    out = []
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path), after.get(path)
        if a == b:
            continue
        status = "added" if a is None else "removed" if b is None else "changed"
        text = all(v is None or isinstance(v, str) for v in (a, b))
        body = _diff_rows(a or "", b or "") if text else []
        out.append(
            {
                "path": path,
                "status": status,
                "added": sum(1 for x in body if x.startswith("+")),
                "removed": sum(1 for x in body if x.startswith("-")),
                "diff": "\n".join(body),
            }
        )
    return out


# ---- automations: configs built from this turn's lists ------------------------------------------


def _fence(config: dict) -> None:
    """HQ's own fence (#1558 never-list): no permission bypass, no auto-choose autonomy."""
    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    if action.get("bypass") is True:
        raise Refused(
            "refused: HQ never turns permission bypass on — the operator can, in the Library's "
            "automation editor",
            "never",
        )
    if action.get("kind") == "start_mission" and action.get("autonomy") not in HQ_AUTONOMY:
        raise Refused(
            "refused: HQ never lets an automation answer the agents' menus by itself "
            "(dispatch_auto_choose) — the operator can, in the Library",
            "never",
        )


def build_action(args: dict, *, projects, agents, keys, folder_of) -> dict:
    """An automation's ``action`` from indexes and keys this turn returned, never model ids or
    paths. ``folder_of(project)`` resolves a project's default folder server-side."""
    kind = args.get("action_kind")
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        raise Refused("message must be the instruction text, in full")

    def pick(name: str, rows, listed_by: str) -> dict:
        if rows is None:
            raise Refused(f"refused: call {listed_by} in this turn first", "not_listed")
        v = args.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(rows):
            raise Refused(f"refused: {name} must be an index {listed_by} returned in this turn")
        return rows[v]

    if kind == "start_mission":
        autonomy = args.get("autonomy", "propose")
        if autonomy not in HQ_AUTONOMY:
            raise Refused(
                "refused: autonomy is propose or dispatch — HQ never sets dispatch_auto_choose",
                "never",
            )
        project = pick("project_index", projects, "list_projects")
        return {
            "kind": kind,
            "project_id": project["id"],
            "instruction": {"text": message},
            "autonomy": autonomy,
        }
    if kind == "start_session":
        if args.get("bypass") is True:
            raise Refused("refused: HQ never turns permission bypass on", "never")
        project = pick("project_index", projects, "list_projects")
        agent = pick("engine_index", agents, "list_agents")
        folder = folder_of(project)
        if not folder:
            raise Refused(
                "refused: that project has no folder to start a session in", "not_available"
            )
        return {
            "kind": kind,
            "engine": agent["id"],
            "folder": folder,
            "bypass": False,
            "message": {"text": message},
        }
    if kind == "send_to_session":
        key = args.get("key")
        if not isinstance(key, str) or key not in keys:
            raise Refused(
                "refused: use a session key one of your tools returned in this turn", "unknown_key"
            )
        return {"kind": kind, "session_key": key, "message": {"text": message}}
    raise Refused("action_kind must be start_mission, start_session or send_to_session")


def check_config(config: dict) -> dict:
    """A whole automation config through the store's own STRICT validator and save check, no
    write. Returns it as the store would hold it."""
    from . import automation_runner
    from . import automations as model

    try:
        out = model.validate_config(config, now=time.time())
        automation_runner.check_save(out)
    except model.AutomationError as e:
        raise Refused(f"the automation is not valid: {e}", "invalid") from None
    except model.PinsUnavailable as e:
        raise Refused(f"its inputs can't be checked just now: {e}", "not_available") from None
    _fence(out)
    return out


def scope_preview(config: dict) -> tuple[list[str], str]:
    """The consent lines and digest the automation routes would show for ``config``."""
    from . import automations as model
    from .routes import automations as routes

    try:
        pins = model.compute_pins(config)
    except model.PinsUnavailable as e:
        raise Refused(f"its inputs can't be checked just now: {e}", "not_available") from None
    scope = model.scope_of(config, pins)
    return routes.scope_lines(scope, config), routes.scope_digest(scope)


def widening(stored: dict, body: dict) -> tuple[dict, list[str], str, list]:
    """For an edit of a stored automation: the merged config, its scope lines and digest, and
    what widens against the stored consent receipt (empty when nothing does, or it is off)."""
    from . import automations as model
    from .routes import automations as routes

    try:
        config = routes._merged(stored, body)
    except model.AutomationError as e:
        raise Refused(f"the change is not valid: {e}", "invalid") from None
    except model.PinsUnavailable as e:
        raise Refused(f"its inputs can't be checked just now: {e}", "not_available") from None
    _fence(config)
    try:
        pins = model.compute_pins(config)
    except model.PinsUnavailable as e:
        raise Refused(f"its inputs can't be checked just now: {e}", "not_available") from None
    scope = model.scope_of(config, pins)
    widened = model.widened(stored["consented_scope"], scope) if stored["enabled"] else []
    return (
        config,
        routes.scope_lines(scope, config),
        routes.scope_digest(scope),
        list(widened or []),
    )


def card_lines(files: dict) -> list[dict]:
    """The card's file list: path, lines and whether it is text."""
    return [
        {
            "path": p,
            "lines": v.count("\n") + 1 if isinstance(v, str) else 0,
            "text": isinstance(v, str),
        }
        for p, v in sorted(files.items())
    ]


# ---- the catalog entries (#1611) ----------------------------------------------------------------
#
# Every route below is the Library's own, built from the DRAFT the card is bound to (`_content`,
# injected at the tap by `ask_operations` after the draft's sha256 matched the binding) — never
# from model text.


#: The Library editor's own words for a save whose durability flush failed (PlaybookEditor).
NOT_DURABLE = (
    "Saved, but durable storage or recovery could not be confirmed. Inspect it in the Library "
    "before further changes."
)


def _saved(what: str):
    def outcome(status: int, body: object, facts: dict) -> tuple[str, str]:
        from .ask_operations import DONE, FAILED, UNCONFIRMED

        if not 200 <= status < 300:
            return FAILED, ""
        # A store that published but could not flush says so (`durable: false`, as the editor
        # reads it): never reported as a confirmed save (#1611 review).
        flags = ("durable", "state_durable", "recovery_durable")
        if isinstance(body, dict) and any(body.get(k) is False for k in flags):
            return UNCONFIRMED, NOT_DURABLE
        return DONE, f"{what} Open it in the Library."

    return outcome


def _not_saved(status: int, detail: str, facts: dict) -> str:
    if status == 409:
        return f"Not saved: {detail or 'it changed since HQ read it'}. Nothing was written."
    return f"Not saved: {detail or 'refused'}. Nothing was written."


def _content(fields: dict) -> dict:
    c = fields.get("_content")
    if not isinstance(c, dict):
        raise ValueError("a library operation runs only with its bound draft")
    return c


def _files_said(fields: dict) -> str:
    n = int(fields.get("files") or 0)
    return f"{n} file{'s' if n != 1 else ''}"


def library_ops() -> tuple:
    from .ask_operations import Operation

    pb, sk, au = "playbook", "skill", "automation"
    return (
        Operation(
            "playbook.create",
            pb,
            "create",
            "Create playbook",
            "Save playbook",
            "reversible",
            "none",
            ("name", "new_id", "files"),
            lambda t, f, x: ("POST", "/api/playbooks", {"files": _content(f)["files"]}),
            lambda f, x: f"Saves a new playbook, “{f['name']}” ({_files_said(f)}), to your "
            "Library. It doesn't run, deploy or start anything.",
            _saved("Saved to your Library."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "playbook.edit",
            pb,
            "edit",
            "Edit playbook",
            "Save changes",
            "reversible",
            pb,
            ("changed", "added", "removed"),
            lambda t, f, x: (
                "PUT",
                f"/api/playbooks/{t}",
                {"revision": _content(f)["revision"], "files": _content(f)["files"]},
            ),
            lambda f, x: "Saves these changes over the version HQ read "
            f"({f['changed']} file{'s' if f['changed'] != 1 else ''}, +{f['added']} −"
            f"{f['removed']} lines). If it was saved elsewhere since, nothing is written.",
            _saved("Saved."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "playbook.duplicate",
            pb,
            "duplicate",
            "Duplicate playbook",
            "Duplicate",
            "reversible",
            pb,
            ("new_id", "name"),
            lambda t, f, x: (
                "POST",
                f"/api/playbooks/{t}/duplicate",
                {"id": f["new_id"], **({"name": f["name"]} if f["name"] else {})},
            ),
            lambda f, x: f"Copies it to a new local playbook, “{f['name'] or f['new_id']}”. "
            "The original stays as it is.",
            _saved("Duplicated."),
            _not_saved,
        ),
        Operation(
            "skill.create",
            sk,
            "create",
            "Create skill",
            "Save skill",
            "reversible",
            "none",
            ("name", "new_id", "files"),
            lambda t, f, x: (
                "POST",
                "/api/library/skills",
                {"new_id": _content(f)["new_id"], "files": _content(f)["files"]},
            ),
            lambda f, x: f"Saves a new skill, “{f['name']}” ({_files_said(f)}), to your Library. "
            "A skill is an inert copy until a playbook or project uses it.",
            _saved("Saved to your Library."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "skill.edit",
            sk,
            "edit",
            "Edit skill",
            "Save as copy",
            "reversible",
            sk,
            ("new_id", "changed", "added", "removed"),
            lambda t, f, x: (
                "POST",
                f"/api/library/skills/{t}/copies",
                {k: _content(f)[k] for k in ("revision", "origin", "files", "new_id", "operation")},
            ),
            lambda f, x: f"Saves the changed skill as a new copy, “{f['new_id']}” "
            f"(+{f['added']} −{f['removed']} lines). The original stays unchanged.",
            _saved("Saved as a new copy."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "automation.create",
            au,
            "create",
            "Create automation",
            "Save automation",
            "reversible",
            "none",
            ("name",),
            lambda t, f, x: ("POST", "/api/automations", _content(f)["config"]),
            lambda f, x: f"Saves a new automation, “{f['name']}”, switched off: nothing runs "
            "until you enable it.",
            _saved("Saved, switched off."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "automation.edit",
            au,
            "edit",
            "Edit automation",
            "Save changes",
            "reversible",
            au,
            ("enabled",),
            lambda t, f, x: (
                "PATCH",
                f"/api/automations/{t}",
                {"revision": x["revision"], **_content(f)["body"]},
            ),
            lambda f, x: "Saves this change. "
            + (
                "It doesn't widen what the automation may do, so no new consent is needed. It "
                "remains enabled: changes affect its next and later runs."
                if f["enabled"]
                else "It is switched off: nothing runs until you enable it."
            ),
            _saved("Saved."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "automation.widen",
            au,
            "widen",
            "Edit automation",
            "Save and consent",
            "external",
            au,
            ("enabled",),
            lambda t, f, x: (
                "PATCH",
                f"/api/automations/{t}",
                {
                    "revision": x["revision"],
                    **_content(f)["body"],
                    "consent": True,
                    "scope_digest": _content(f)["scope_digest"],
                },
            ),
            lambda f, x: "Saves this change and consents to the new scope below. It remains "
            "enabled: from its next run it works within exactly this scope.",
            _saved("Saved; it runs within the new scope."),
            _not_saved,
            draft=True,
        ),
        Operation(
            "automation.enable",
            au,
            "enable",
            "Enable",
            "Enable",
            "external",
            au,
            (),
            lambda t, f, x: (
                "POST",
                f"/api/automations/{t}/enable",
                {
                    "revision": x["revision"],
                    "consent": True,
                    "scope_digest": _content(f)["scope_digest"],
                },
            ),
            lambda f, x: "Turns it on. From then it starts agents by itself, within exactly the "
            "scope below. Any wider change later asks for your consent again.",
            _saved("Enabled."),
            lambda st, d, x: f"Not enabled: {d or 'refused'}. It is still off.",
            draft=True,
        ),
    )


async def facts(op, target: str, app, operator) -> dict | None:
    """A library target's facts as the card shows them, read the way the Library reads them, or
    None when it is gone. Revisions are bound as bounded ints (a hex revision reads as
    credential-shaped to the store boundary)."""
    from .ask_operations import TITLE_MAX, _digest

    if op.target == "none":
        return {}
    path = {
        "playbook": f"/api/playbooks/{target}",
        "skill": f"/api/library/skills/{target}",
        "automation": f"/api/automations/{target}",
    }[op.target]
    r = await asgi_call.call(app, operator, "GET", path, None)
    if r.status != 200 or not isinstance(r.body, dict):
        return None
    b = r.body
    if op.target == "automation":
        out = {
            "name": str(b.get("name") or "")[:TITLE_MAX],
            "revision": int(b.get("revision") or 0),
            "enabled": bool(b.get("enabled")),
        }
        if b.get("scope_digest"):
            out["scope"] = _digest([str(b["scope_digest"])])
        return out
    name = (
        str(b.get("name") or target)
        if op.target == "playbook"
        else _skill_name(b.get("files"), target)
    )
    return {"name": name[:TITLE_MAX], "revision": _digest([str(b.get("revision") or "")])}


# ---- staging: propose_library's arguments → one catalog entry and its draft ---------------------


def _known(known: dict, group: str, args: dict) -> str:
    tid = args.get("id")
    if not isinstance(tid, str) or tid not in (known.get(group) or {}):
        raise Refused(
            f"refused: use a {group} id list_{group}s or {group}_detail returned in this turn",
            "unknown_target",
        )
    return tid


def _as_read(known: dict, group: str, rid: str, now: object) -> set:
    """The files HQ read whole of ``rid`` this turn — refused unless HQ read it this turn AT the
    revision it has now. An edit is HQ's change to what IT saw: a save made in between (the
    operator's, in the Library) must never be overwritten as if it were HQ's (#1611 review)."""
    seen = (known.get(group) or {}).get(rid) or {}
    if seen.get("revision") in (None, ""):
        raise Refused(
            f"refused: read it with {group}_detail in this turn first, then propose the edit",
            "not_read",
        )
    if seen["revision"] != now:
        raise Refused(
            "refused: it was saved elsewhere after you read it; read it again and redo the edit "
            "on the new version",
            "stale_read",
        )
    return set(seen.get("whole") or ())


def _edit_counts(diff: list[dict]) -> dict:
    return {
        "changed": len(diff),
        "added": sum(d["added"] for d in diff),
        "removed": sum(d["removed"] for d in diff),
    }


async def prepare(
    group: str, action: str, args: dict, *, known: dict, lists: dict, app, operator
) -> dict:
    """What `propose_library` stages: ``{"op", "target", "title", "fields", "draft"}``. Every
    draft is validated by its store WITHOUT writing; every target is one this turn returned.
    ``lists`` holds this turn's ``projects`` / ``agents`` / ``keys``. Raises :class:`Refused`."""
    import uuid

    from .ask_operations import CATALOG

    if group not in GROUPS:
        raise Refused("group must be playbook, skill or automation")
    op = CATALOG.get(f"{group}.{action}")
    if op is None or op.id == "automation.widen":
        names = sorted(
            o.action for o in CATALOG.values() if o.group == group and o.action != "widen"
        )
        raise Refused(f"action must be one of {', '.join(names)}")

    if op.id == "playbook.create":
        files = _text_files(args.get("files"), "files")
        ident = await asyncio.to_thread(_validate_playbook, files)
        draft = {"files": files, "before": {}}
        return _staged(
            op,
            "",
            ident["name"],
            {"name": ident["name"], "new_id": ident["id"], "files": len(files)},
            draft,
        )

    if op.id in ("playbook.edit", "playbook.duplicate"):
        pid = _known(known, "playbook", args)
        body = await _get(app, operator, f"/api/playbooks/{pid}", "that playbook")
        if op.id == "playbook.duplicate":
            new_id = _skill_id(args.get("new_id"))
            name = args.get("name") or ""
            if not isinstance(name, str) or len(name) > 120:
                raise Refused("name must be a short text")
            return _staged(
                op,
                pid,
                str(body.get("name") or pid),
                {"new_id": new_id, "name": name.strip()},
                None,
            )
        if not body.get("editable"):
            raise Refused(
                "refused: that playbook is read-only (bundled or from the gallery) — duplicate it "
                "first, then edit the copy",
                "read_only",
            )
        whole = _as_read(known, "playbook", pid, str(body.get("revision") or ""))
        current = body.get("files") if isinstance(body.get("files"), dict) else {}
        merged = _merge(
            current,
            _text_files(args.get("files") or {}, "files"),
            _removals(args.get("remove")),
            whole,
        )
        ident = await asyncio.to_thread(_validate_playbook, merged)
        if ident["id"] != pid:
            raise Refused("refused: an edit keeps the playbook's identity.id")
        diff = _diff(current, merged)
        if not diff:
            raise Refused("that edit changes nothing")
        draft = {
            "files": merged,
            "before": {d["path"]: current.get(d["path"]) for d in diff},
            "revision": str(body.get("revision") or ""),
        }
        return _staged(op, pid, str(body.get("name") or pid), _edit_counts(diff), draft)

    if op.id == "skill.create":
        new_id = _skill_id(args.get("new_id"))
        files = _text_files(args.get("files"), "files")
        await asyncio.to_thread(_validate_skill, files)
        name = _skill_name(files, new_id)
        draft = {"files": files, "before": {}, "new_id": new_id}
        return _staged(op, "", name, {"name": name, "new_id": new_id, "files": len(files)}, draft)

    if op.id == "skill.edit":
        sid = _known(known, "skill", args)
        new_id = _skill_id(args.get("new_id"))
        if new_id == sid:
            raise Refused("new_id must differ: the edited skill is saved as a new copy")
        body = await _get(app, operator, f"/api/library/skills/{sid}", "that skill")
        whole = _as_read(known, "skill", sid, str(body.get("revision") or ""))
        current = body.get("files") if isinstance(body.get("files"), dict) else {}
        merged = _merge(
            current,
            _text_files(args.get("files") or {}, "files"),
            _removals(args.get("remove")),
            whole,
        )
        await asyncio.to_thread(_validate_skill, merged)
        diff = _diff(current, merged)
        if not diff:
            raise Refused("that edit changes nothing")
        origin = body.get("gallery_origin")
        draft = {
            "files": merged,
            "before": {d["path"]: current.get(d["path"]) for d in diff},
            "revision": str(body.get("revision") or ""),
            "origin": origin.get("pointer") if isinstance(origin, dict) else None,
            "new_id": new_id,
            "operation": uuid.uuid4().hex,
        }
        return _staged(
            op, sid, _skill_name(current, sid), {"new_id": new_id, **_edit_counts(diff)}, draft
        )

    if op.id == "automation.create":
        config = {
            "name": args.get("name"),
            "trigger": args.get("trigger"),
            "action": build_action(args, **lists),
            **({"policy": args["policy"]} if args.get("policy") is not None else {}),
        }
        config = await asyncio.to_thread(check_config, config)
        lines, digest = await asyncio.to_thread(scope_preview, config)
        draft = {"config": config, "scope_lines": lines, "status": "off"}
        return _staged(op, "", config["name"], {"name": config["name"]}, draft)

    aid = _known(known, "automation", args)
    if op.id == "automation.enable":
        a = await _get(app, operator, f"/api/automations/{aid}", "that automation")
        _as_read(known, "automation", aid, a.get("revision"))
        if a.get("enabled") and not a.get("needs_reapproval") and not a.get("paused"):
            raise Refused("it is already enabled")
        stored = {"action": a.get("action") or {}}
        _fence(stored)
        draft = {
            "scope_lines": [str(x) for x in a.get("scope_lines") or []],
            "scope_digest": str(a.get("scope_digest") or ""),
            # The stored action, so the consent card shows the WHOLE instruction, not a preview.
            "config": {"action": a.get("action") or {}},
            "status": "enable",
        }
        return _staged(op, aid, str(a.get("name") or aid), {}, draft)

    # automation.edit — narrowing or off: a plain save; widening an enabled one: the consent card.
    from . import automations_store

    try:
        stored = await asyncio.to_thread(automations_store.get, aid)
    except Exception:  # noqa: BLE001 — gone or unreadable: nothing to edit
        raise Refused("refused: that automation no longer exists", "not_available") from None
    _as_read(known, "automation", aid, stored["revision"])
    body: dict = {}
    for k in ("name", "trigger", "policy"):
        if args.get(k) is not None:
            body[k] = args[k]
    if args.get("action_kind") is not None:
        body["action"] = build_action(args, **lists)
    if not body:
        raise Refused("an edit names what changes: name, trigger, policy or the action")
    config, lines, digest, widened = await asyncio.to_thread(widening, stored, body)
    enabled = bool(stored["enabled"])
    widen = enabled and bool(widened)
    target_op = CATALOG["automation.widen"] if widen else op
    draft = {
        "body": body,
        "config": config,
        "scope_lines": lines,
        "status": "enabled" if enabled else "off",
        **({"scope_digest": digest, "widened": [str(w) for w in widened]} if widen else {}),
    }
    return _staged(target_op, aid, str(stored.get("name") or aid), {"enabled": enabled}, draft)


def _staged(op, target: str, title: str, fields: dict, draft: dict | None) -> dict:
    return {"op": op, "target": target, "title": title, "fields": fields, "draft": draft}


# ---- the card's content (GET /api/ask/proposals/{id}/content) ------------------------------------


def content_view(op_id: str, draft: dict) -> dict:
    """What the card shows of a draft, for the operator's browser only (never the model)."""
    if op_id.startswith(("playbook.", "skill.")):
        files = draft.get("files") or {}
        before = draft.get("before") or {}
        if before:  # an edit: the diff against what HQ read
            return {"kind": "diff", "diff": _diff(before, {p: files.get(p) for p in before})}
        return {
            "kind": "files",
            "files": [
                {"path": p, "text": v if isinstance(v, str) else None}
                for p, v in sorted(files.items())
            ],
        }
    config = draft.get("config") or {}
    action = config.get("action") if isinstance(config.get("action"), dict) else {}
    message = action.get("instruction") or action.get("message") or {}
    changed = sorted((draft.get("body") or {}).keys())
    return {
        "kind": "automation",
        # What the save names it and, for an edit, which settings it changes (#1611 review).
        "name": config.get("name") if isinstance(config.get("name"), str) else None,
        "changed": changed,
        "scope_lines": list(draft.get("scope_lines") or []),
        "instruction": message.get("text") if isinstance(message, dict) else None,
        "status": draft.get("status") or "",
        "widened": list(draft.get("widened") or []),
    }


__all__ = [
    "content_view",
    "prepare",
    "facts",
    "library_ops",
    "GROUPS",
    "Refused",
    "automation_detail",
    "build_action",
    "card_lines",
    "check_config",
    "remember",
    "list_automations",
    "list_playbooks",
    "list_skills",
    "playbook_detail",
    "playbook_schema",
    "scope_preview",
    "skill_detail",
    "widening",
]
