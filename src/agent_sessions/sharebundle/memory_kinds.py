"""Project memory in a share bundle (#1714 P4).

Two kinds, both containers of notes:

* **`memory`** — one project's notes. The item's ID in the file is always `project`: the
  project's name and ID never travel (the importing install chooses the project).
* **`notebook`** — the notes a specialist keeps in that project (`memory.specialist_scope`). Its
  ID in the file is the specialist's, and it `depends_on` that specialist, which must travel in the
  same file: it is imported into the notebook of the specialist this import creates, never matched
  by name against this install. Choosing a notebook in the export picker includes its specialist.

Each note is one file, `notes/<n>.json`: `{format: 1, name, description, type, body}`. A note's
local slug never travels (it was minted from the name the note had when it was written, which
may be gone): the importing install names it from its name, beside whatever is there.

**What leaves is the read view.** A note the read view leaves out (it holds a stored secret in
any field it shows) or the withhold check refuses now (a credential shape) is left out, and so is
every quarantine item. On top of that, the share scan of the other kinds (`template_kinds.scan`:
stored secrets, credential-value shapes, and identifiers case-folded) runs on what travels: the
name and each `[[link]]` are identifiers (a slug is the name lowercased), the description, type
and body are text. Only counts are reported. With no readable secret inventory, nothing exports.

**What enters goes through memory's own write path** (`memory.share_import_note`): one note at a
time, judged under the secret-inventory fence by the withhold check an agent's note gets, never
over an existing note, into a project whose memory is on (an import never turns it on).
"""

from __future__ import annotations

import json
import re
import sqlite3

from .. import memory
from ..gallery import contract as gallery_contract
from . import template_kinds
from .template_kinds import Refused

NOTE_DIR = "notes"
NOTE_FORMAT = 1
NOTE_RE = re.compile(r"notes/[0-9]{4}\.json")
PROJECT_ID = "project"
KINDS = ("memory", "notebook")
_NOTE_KEYS = frozenset({"format", *memory.SHARE_FIELDS})
_LINK_RE = re.compile(r"\[\[([^\]\n]{1,64})\]\]")
#: Every note's `name` and `[[link]]` targets are identifiers: a slug is minted from the name
#: lowercased, so a secret is looked for in that folded form too (`template_kinds._IDENTIFIERS`).
NAME_WHAT = "note name"
LINK_WHAT = "note link"


def note_file(n: int) -> str:
    return f"{NOTE_DIR}/{n:04d}.json"


def _note_bytes(note: dict) -> bytes:
    doc = {"format": NOTE_FORMAT, **note}
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _texts(note: dict) -> list[tuple[str, str]]:
    out = [(NAME_WHAT, note["name"]), ("description", note["description"])]
    out += [("type", note["type"]), ("body", note["body"])]
    out += [(LINK_WHAT, m) for m in _LINK_RE.findall(note["body"])]
    return out


def scope_of(kind: str, item_id: str) -> str:
    """The local scope an export request names: a project ID, or `<project>~<specialist>`."""
    if kind == "notebook" and memory.SPECIALIST_SEP not in item_id:
        raise Refused("a notebook is named <project>~<specialist>")
    if kind == "memory" and memory.SPECIALIST_SEP in item_id:
        raise Refused("project memory is named by its project")
    try:
        return memory._scope_id(item_id)
    except memory.MemoryStoreError:
        raise Refused(f"there is no {kind} {item_id} here") from None


def id_ok(kind: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        scope_of(kind, value)
    except Refused:
        return False
    return True


def leaving(scope: str, secrets: list[str]) -> dict:
    """The notes of `scope` that may leave, as `{"notes": [...], "withheld", "quarantined"}`:
    the read view's rule (`memory.share_notes`) and then the share scan on what travels."""
    try:
        found = memory.share_notes(scope, secrets)
    except memory.MemoryStoreError:
        raise Refused("its project isn't here") from None
    except (sqlite3.Error, OSError):
        raise Refused("its memory store can't be read here; nothing of it is exported") from None
    kept = []
    for note in found["notes"]:
        try:
            template_kinds.scan(_texts(note), secrets)
        except Refused:
            found["withheld"] += 1
            continue
        kept.append(note)
    found["notes"] = kept
    return found


def export(kind: str, item_id: str, secrets: list[str]) -> dict:
    """`{title, revision, files, depends_on, bundle_id, withheld, quarantined}` for a project's
    memory (`bundle_id` "project") or a specialist's notebook in it (`bundle_id` its ID)."""
    scope = scope_of(kind, item_id)
    found = leaving(scope, secrets)
    if not found["notes"]:
        raise Refused(
            "none of its notes can leave this install"
            + (f" ({found['withheld']} held back by the secret check)" if found["withheld"] else "")
        )
    files = {note_file(n): _note_bytes(note) for n, note in enumerate(found["notes"], 1)}
    if kind == "notebook":
        sid = scope.partition(memory.SPECIALIST_SEP)[2]
        title, bundle_id, deps = "Specialist notes", sid, [f"specialist:{sid}"]
    else:
        title, bundle_id, deps = "Project memory", PROJECT_ID, []
    return {
        "title": title,
        "revision": "",
        "files": files,
        "depends_on": deps,
        "bundle_id": bundle_id,
        "withheld": found["withheld"],
        "quarantined": found["quarantined"],
    }


def notes_of(files: dict[str, bytes]) -> list[tuple[str, dict | None, str]]:
    """Each note file as `(path, note, problem)`, in file order: a note `memory` would hold as
    it is, else `None` and why not. A file that is not `notes/<n>.json` refuses the whole item."""
    bad = next((p for p in sorted(files) if not NOTE_RE.fullmatch(p)), None)
    if bad is not None:
        raise Refused("a memory item holds notes/<n>.json files only")
    if len(files) > memory.SCOPE_MAX_NOTES:
        raise Refused(f"a project holds at most {memory.SCOPE_MAX_NOTES} notes")
    out: list[tuple[str, dict | None, str]] = []
    for path in sorted(files):
        try:
            doc = json.loads(
                files[path].decode("utf-8"),
                object_pairs_hook=gallery_contract._pairs,
                parse_float=gallery_contract._no_float,
                parse_constant=gallery_contract._no_float,
            )
        except (ValueError, UnicodeError, RecursionError, gallery_contract.GalleryError):
            out.append((path, None, "it is not unambiguous JSON"))
            continue
        if not isinstance(doc, dict) or set(doc) != _NOTE_KEYS:
            out.append((path, None, "it doesn't have the fields of a note"))
            continue
        fmt_no = doc.pop("format")
        if type(fmt_no) is int and fmt_no > NOTE_FORMAT:
            out.append((path, None, "it was made by a newer BattleLab; update to import it"))
            continue
        if type(fmt_no) is not int or fmt_no != NOTE_FORMAT:
            out.append((path, None, "it doesn't have the fields of a note"))
            continue
        problem = memory.share_note_problem(doc)
        out.append((path, None if problem else doc, problem))
    return out


def validate(kind: str, item_id: str, files: dict[str, bytes]) -> list[str]:
    """Refuse a memory item whose shape is wrong (each bad NOTE is refused on its own, in the
    view); return the specialist a notebook needs."""
    if kind == "memory" and item_id != PROJECT_ID:
        raise Refused(f"project memory travels as {PROJECT_ID!r}")
    notes_of(files)
    if kind == "notebook":
        if not memory._SPECIALIST_RE.fullmatch(item_id):
            raise Refused(f"{item_id!r} is not a specialist ID")
        return [item_id]
    return []


def view(files: dict[str, bytes]) -> dict:
    """What the preview shows of a memory item: each note with what its import would do here now
    (`new`, `held` with the withhold check's reason, or `refused` with why), judged against this
    install's inventory as of the request. A held or refused note's own text is withheld."""
    rows = notes_of(files)
    try:
        reasons = memory.share_judge([n for _p, n, _w in rows if n is not None])
    except memory.MemoryStoreError as exc:
        raise Refused(str(exc)) from None
    except RuntimeError:  # the secret-inventory fence is busy (a secret write, an import)
        raise Refused("the secret check is busy right now; preview the file again") from None
    verdicts = iter(reasons)
    notes = []
    for path, note, problem in rows:
        if note is None:
            notes.append({"file": path, "verdict": "refused", "reason": problem})
            continue
        why = next(verdicts)
        if why:
            notes.append({"file": path, "verdict": "held", "reason": why})
            continue
        notes.append(
            {
                "file": path,
                "verdict": "new",
                "reason": None,
                "name": note["name"],
                "type": note["type"],
                "description": note["description"],
            }
        )
    counts = {v: sum(1 for n in notes if n["verdict"] == v) for v in ("new", "held", "refused")}
    return {"notes": notes, "counts": counts}


def withheld_files(view_doc: dict | None) -> set[str]:
    """The files of a memory item whose text the preview must not show (held or refused notes)."""
    if not view_doc:
        return set()
    return {n["file"] for n in view_doc.get("notes", []) if n.get("verdict") != "new"}


def catalog() -> list[dict]:
    """A row per project with memory, and per notebook of a live specialist in it, each with how
    many notes would travel and how many are held back. Rows are local: they name the project,
    which no file ever does. When the secret check can't run, no memory is exportable."""
    from .. import projects
    from .. import specialists_store as pool

    try:
        secrets = template_kinds.secret_inventory()
        why_all = None
    except Refused as exc:
        secrets, why_all = [], str(exc)
    try:
        live = {s["id"]: s["current"]["title"] for s in pool.list_all() if not s.get("archived")}
    except Exception:  # noqa: BLE001 — an unreadable pool lists no notebooks
        live = {}
    out: list[dict] = []
    for entity in sorted(projects.load().values(), key=lambda p: p.name.lower()):
        if entity.archived or not memory._exists(entity.id):
            continue
        scopes = [("memory", entity.id, f"{entity.name}", [])]
        for scope in memory._specialist_scopes(entity.id):
            sid = scope.partition(memory.SPECIALIST_SEP)[2]
            if sid in live and memory._exists(scope):
                scopes.append(("notebook", scope, f"{live[sid]} — notes in {entity.name}", [sid]))
        for kind, scope, title, deps in scopes:
            row: dict = {
                "kind": kind,
                "id": scope,
                "title": title,
                "depends_on": [f"specialist:{d}" for d in deps],
                "exportable": False,
                "reason": why_all,
                "source": "local",
                "project": {"id": entity.id, "name": entity.name},
                "memory": None,
            }
            if why_all is None:
                try:
                    found = leaving(scope, secrets)
                except Refused as exc:
                    row["reason"] = str(exc)
                except Exception:  # noqa: BLE001 — one broken store never breaks the picker
                    row["reason"] = "Its memory store can't be read here."
                else:
                    row["memory"] = {
                        "notes": len(found["notes"]),
                        "withheld": found["withheld"],
                        "quarantined": found["quarantined"],
                    }
                    row["exportable"] = bool(found["notes"])
                    if not found["notes"]:
                        row["reason"] = "No note of it can leave this install."
            out.append(row)
    return out


# ---- import (called by `service` under the share lock) -------------------------------------------


def project_problem(pid: object) -> str:
    """Why notes can't be imported into project `pid` now, or `""`. An import never turns memory
    on, never picks a project by itself, and never writes into one that is archived."""
    if not isinstance(pid, str) or not memory.valid_id(pid) or memory.SPECIALIST_SEP in pid:
        return "choose the project to import the notes into"
    try:
        entity = memory._project(pid)
    except memory.MemoryStoreError:
        return "that project isn't here any more"
    if entity.archived:
        return f"{entity.name} is archived"
    try:
        on = memory.enabled(pid)
    except (sqlite3.Error, OSError, memory.MemoryStoreError):
        return f"the memory of {entity.name} can't be read right now; nothing was written"
    if not on:
        return (
            f"memory is off for {entity.name}. Turn it on in Library → Memory to import notes "
            "into it; an import never turns it on"
        )
    return ""


def scope_for(kind: str, project: str, specialist: str | None) -> str:
    """Where a container's notes go: the chosen project, or the notebook there of the specialist
    this import created (its destination ID)."""
    if kind == "notebook":
        return memory.specialist_scope(project, specialist)
    return memory._scope_id(project)


def writer(bundle_digest: str) -> str:
    return memory.share_writer(bundle_digest)


def digest(note: dict) -> str:
    return memory.share_digest(note)


def import_note(scope: str, note: dict, *, writer: str, intent) -> dict:
    return memory.share_import_note(scope, note, writer=writer, intent=intent)


def note_at(scope: str, slug: str) -> str | None:
    return memory.share_note_at(scope, slug)


def note_removed(scope: str, digest: str) -> bool:
    return memory.share_note_removed(scope, digest)


def publish(scope: str) -> None:
    memory.publish(scope)


RETRY = memory.SHARE_RETRY

#: What a note's import can raise without a verdict on the note: settled by what its slug holds.
IMPORT_ERRORS = (OSError, sqlite3.Error, memory.MemoryStoreError, RuntimeError, ValueError)
