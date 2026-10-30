"""What each kind contributes to a share bundle (#1714 P1: skills and playbooks; P2: specialists).

A kind reads its local item for export, validates and rewrites an incoming item, publishes a new
copy through its store's own new-copy path (NOREPLACE, full validation, read-back) and answers
"what is at this destination now" for crash reconciliation. Nothing here activates, deploys,
launches or sets a default: an import only ever creates an inert new copy.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tomllib
import unicodedata

from .. import specialists
from .. import specialists_store as specialist_store
from ..gallery import contract as gallery_contract
from ..gallery import files as skill_files
from ..gallery import skills as skill_library
from ..playbooks import schema, store, tomlw
from ..playbooks.schema import SKILL_REF_RE
from . import format as fmt

#: Apply order across kinds (#1714 "Partial failure"): skills (and later variable definitions)
#: first, then templates, then playbooks and specialists, then memory.
LEVEL = {"skill": 0, "playbook": 2, "specialist": 2}
KINDS = tuple(LEVEL)
#: The Library sections a picker groups by, in order.
LABELS = {"skill": "Skill", "playbook": "Playbook", "specialist": "Specialist"}
#: Kinds whose every required skill must travel in the same file: a specialist's pins are what it
#: runs with, never a reference resolved by name here (a playbook's role skills may be).
CARRIES_ALL = frozenset({"specialist"})


class ItemRefused(ValueError):
    """This item cannot be exported or imported; the message is the reason, in words."""


# ---- skills ---------------------------------------------------------------------------------


def _skill_text(files: dict[str, bytes]) -> dict[str, str]:
    try:
        return {path: data.decode("utf-8") for path, data in files.items()}
    except UnicodeDecodeError:
        raise ItemRefused("a skill holds UTF-8 text files only") from None


def _skill_listing() -> list[dict]:
    return skill_library.listing().get("skills", [])


def _skill_export(item_id: str) -> dict:
    current = skill_library.get(item_id)
    files = {path: text.encode("utf-8") for path, text in current["files"].items()}
    origin = current.get("gallery_origin") or {}
    title = origin.get("title") if isinstance(origin.get("title"), str) else item_id
    return {"title": title, "revision": current["revision"], "files": files, "depends_on": []}


def _skill_validate(item_id: str, files: dict[str, bytes]) -> list[str]:
    try:
        store.skill_id(item_id)
    except store.StoreError:
        raise ItemRefused(f"{item_id!r} is not a skill ID") from None
    try:
        skill_files.tree(_skill_text(files))
    except gallery_contract.GalleryError as exc:
        raise ItemRefused(f"not a valid skill here: {exc}") from None
    return []


def _skill_publish(dest: str, files: dict[str, bytes]) -> str:
    return skill_library.save(dest, _skill_text(files))["revision"]


def _skill_at(dest: str) -> str | None:
    try:
        return skill_library.get(dest)["revision"]
    except store.NotFound:
        return None


# ---- playbooks ------------------------------------------------------------------------------


def _role_skills(pb: dict) -> list[str]:
    """The skills a playbook's format-5 roles require, in first-seen order. A format ≤4 step's
    advisory `skills` are not requirements and are not followed."""
    out: list[str] = []
    for flow in pb["flows"].values():
        for step in flow["steps"]:
            role = step.get("role") or {}
            if role.get("legacy_hint") is not None:
                continue
            for sid in role.get("skills", []):
                if sid not in out:
                    out.append(sid)
    return out


def _playbook_entry(item_id: str) -> store.Entry:
    store.playbook_id(item_id)
    with store.root_lock(exclusive=False) as root_fd:
        entry = store._find(store._all_entries(root_fd), item_id)
    if entry.pb is None or entry.tree is None:
        raise ItemRefused(f"the playbook {item_id} is not valid here; fix it before exporting")
    return entry


def _playbook_export(item_id: str) -> dict:
    entry = _playbook_entry(item_id)
    return {
        "title": entry.pb["identity"]["name"],
        "revision": entry.revision,
        "files": dict(entry.tree.files),
        "depends_on": [f"skill:{sid}" for sid in _role_skills(entry.pb)],
    }


def _playbook_validate(item_id: str, files: dict[str, bytes]) -> list[str]:
    try:
        store.playbook_id(item_id)
    except store.StoreError:
        raise ItemRefused(f"{item_id!r} is not a playbook ID") from None
    try:
        pb = store._validate(fmt.tree_of(files), item_id)
    except store.StoreError as exc:
        raise ItemRefused(f"not a valid playbook here: {exc.detail}") from None
    return _role_skills(pb)


def _rewrite_playbook(item_id: str, dest: str, files: dict[str, bytes], skills: dict) -> dict:
    """The playbook under `dest` with every role skill renamed by `skills`. Untouched files keep
    their bytes; a rewritten TOML document is re-emitted (comments are not kept)."""
    out = dict(files)
    if dest != item_id:
        doc = tomllib.loads(files[schema.MANIFEST_NAME].decode("utf-8"))
        doc["identity"]["id"] = dest
        out[schema.MANIFEST_NAME] = tomlw.dumps(doc).encode("utf-8")
    if skills:
        for path, data in files.items():
            if not (path.startswith(schema.FLOWS_DIR + "/") and path.endswith(".toml")):
                continue
            doc = tomllib.loads(data.decode("utf-8"))
            changed = False
            for step in doc.get("steps", []) if isinstance(doc.get("steps"), list) else []:
                role = step.get("role") if isinstance(step, dict) else None
                if isinstance(role, dict) and isinstance(role.get("skills"), list):
                    renamed = [skills.get(s, s) for s in role["skills"]]
                    if renamed != role["skills"]:
                        role["skills"] = renamed
                        changed = True
            if changed:
                out[path] = tomlw.dumps(doc).encode("utf-8")
    try:
        store._validate(fmt.tree_of(out), dest)
    except store.StoreError as exc:
        raise ItemRefused(f"not a valid playbook after renaming: {exc.detail}") from None
    return out


def _playbook_publish(dest: str, files: dict[str, bytes]) -> str:
    payload: dict = {}
    for path, data in files.items():
        try:
            payload[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            payload[path] = {"base64": base64.b64encode(data).decode("ascii")}
    result = store.create_playbook(payload)
    if result.get("id") != dest:
        raise store.Conflict("the playbook was published under another ID")
    return result["revision"]


def _playbook_at(dest: str) -> str | None:
    with store.root_lock(exclusive=False) as root_fd:
        entries = store._all_entries(root_fd)
    for entry in entries:
        if entry.id == dest:
            # A bundled/catalog id, or an unreadable copy, is "other content" — never ours.
            if entry.source != store.SOURCE_LOCAL or entry.revision is None:
                return "unavailable"
            return entry.revision
    return None


def _playbook_ids() -> dict[str, str | None]:
    with store.root_lock(exclusive=False) as root_fd:
        entries = store._all_entries(root_fd)
    return {e.id: e.revision for e in entries}


def _skill_ids() -> dict[str, str | None]:
    return {s["id"]: s.get("revision") for s in _skill_listing()}


# ---- specialists (#1714 P2) -----------------------------------------------------------------

SPECIALIST_FILE = "specialist.json"
SPECIALIST_FORMAT = 1
_SPEC_KEYS = frozenset({"format", "title", "summary", "capacity", "skills", "suggests"})
_SUGGEST_KEYS = frozenset({"engine", "model", "effort"})
SUGGEST_MAX = 128


def _store_errors(exc: specialist_store.StoreError) -> store.StoreError:
    """The specialist store's refusal in the shape the share service handles everywhere."""
    if isinstance(exc, specialist_store.NotFound):
        return store.NotFound(str(exc))
    return store.StoreError(str(exc), status=exc.status)


def _hint(value: object, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > SUGGEST_MAX
        or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value)
    ):
        raise ItemRefused(f"its suggested {field} is not a name")
    return value


def specialist_doc(files: dict[str, bytes]) -> dict:
    """An incoming specialist's document, checked to the letter, or `ItemRefused`. What the file
    may say is the JOB (title, summary, capacity, the skills it pins) plus a SUGGESTION of what
    runs it; nothing about this install's agents, notes, grades or projects."""
    if set(files) != {SPECIALIST_FILE}:
        raise ItemRefused(f"a specialist is one file, {SPECIALIST_FILE}")
    try:
        doc = json.loads(
            files[SPECIALIST_FILE].decode("utf-8"),
            object_pairs_hook=gallery_contract._pairs,
            parse_float=gallery_contract._no_float,
            parse_constant=gallery_contract._no_float,
        )
    except (ValueError, UnicodeError, RecursionError, gallery_contract.GalleryError):
        raise ItemRefused(f"{SPECIALIST_FILE} is not unambiguous JSON") from None
    if not isinstance(doc, dict):
        raise ItemRefused(f"{SPECIALIST_FILE} is not an object")
    fmt_no = doc.get("format")
    if isinstance(fmt_no, int) and not isinstance(fmt_no, bool) and fmt_no > SPECIALIST_FORMAT:
        raise ItemRefused("it was made by a newer BattleLab; update to import it")
    if fmt_no != SPECIALIST_FORMAT or isinstance(fmt_no, bool) or set(doc) != _SPEC_KEYS:
        raise ItemRefused(f"{SPECIALIST_FILE} doesn't have the fields of format 1")
    try:
        title = specialists._text(doc["title"], "title", specialists.TITLE_MAX, required=True)
        summary = specialists._text(
            doc["summary"], "summary", specialists.SUMMARY_MAX, required=False
        )
    except specialists.DraftRefused as exc:
        raise ItemRefused(f"not a valid specialist here: {exc.detail}") from None
    cap = doc["capacity"]
    lo, hi = specialists.CAPACITY_MIN, specialists.CAPACITY_MAX
    if isinstance(cap, bool) or not isinstance(cap, int) or not lo <= cap <= hi:
        raise ItemRefused(f"not a valid specialist here: capacity must be {lo}…{hi}")
    skills = doc["skills"]
    if (
        not isinstance(skills, list)
        or len(skills) > specialists.SKILLS_MAX
        or not all(isinstance(k, str) and SKILL_REF_RE.fullmatch(k) for k in skills)
        or len(set(skills)) != len(skills)
    ):
        raise ItemRefused(
            f"not a valid specialist here: it pins at most {specialists.SKILLS_MAX} distinct "
            "skill IDs"
        )
    hint = doc["suggests"]
    if not isinstance(hint, dict) or set(hint) != _SUGGEST_KEYS:
        raise ItemRefused("its suggestion is not {engine, model, effort}")
    suggests = {f: _hint(hint[f], f) for f in ("engine", "model", "effort")}
    return {
        "title": title,
        "summary": summary,
        "capacity": cap,
        "skills": list(skills),
        "suggests": suggests,
    }


def _specialist_file(doc: dict) -> dict[str, bytes]:
    body = {"format": SPECIALIST_FORMAT, **doc}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return {SPECIALIST_FILE: text.encode("utf-8")}


def _specialist_export(item_id: str) -> dict:
    """The current version of a live specialist, with its pins proved: each pinned skill is read
    from the library and must BE the pinned revision (`pins` is checked by the caller against the
    very bytes it exports). The agent, model and effort travel only as a suggestion."""
    try:
        spec = specialist_store.get(item_id)
    except specialist_store.StoreError as exc:
        raise _store_errors(exc) from None
    if spec["archived"]:
        raise ItemRefused("it is archived; unarchive it to export it")
    cur = spec["current"]
    doc = {
        "title": cur["title"],
        "summary": cur["summary"],
        "capacity": cur["capacity"],
        "skills": [k["id"] for k in cur["skills"]],
        "suggests": {"engine": cur["engine"], "model": cur["model"], "effort": cur["effort"]},
    }
    return {
        "title": cur["title"],
        "revision": f"v{cur['version']}",
        "files": _specialist_file(doc),
        "depends_on": [f"skill:{k['id']}" for k in cur["skills"]],
        "pins": {k["id"]: k["revision"] for k in cur["skills"]},
        "version": cur["version"],
    }


def _specialist_validate(item_id: str, files: dict[str, bytes]) -> list[str]:
    if not specialist_store.ID_RE.fullmatch(item_id):
        raise ItemRefused(f"{item_id!r} is not a specialist ID")
    return specialist_doc(files)["skills"]


def _rewrite_specialist(files: dict[str, bytes], skills: dict) -> dict:
    doc = specialist_doc(files)
    if not skills:
        return dict(files)
    doc["skills"] = [skills.get(s, s) for s in doc["skills"]]
    return _specialist_file(doc)


def body_digest(body: dict) -> str:
    """The digest of a specialist version body: what a crash reconcile compares at the planned
    destination (`at`) against the intent it journaled."""
    data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _specialist_prepare(files: dict[str, bytes], choice: dict | None, pins: dict) -> dict:
    """The version body to create, checked by the specialist's OWN validator (`check_draft`): no
    specialist is created that it would refuse."""
    doc = specialist_doc(files)
    if choice is None:
        raise ItemRefused("no agent was chosen for it")
    draft = {
        "title": doc["title"],
        "summary": doc["summary"],
        "capacity": doc["capacity"],
        "skills": [{"id": s, "revision": pins.get(s, "")} for s in doc["skills"]],
        "engine": choice.get("engine"),
        "model": choice.get("model"),
        "effort": choice.get("effort"),
    }
    try:
        return specialists.check_draft(draft)
    except specialists.DraftRefused as exc:
        raise ItemRefused(exc.detail) from None


def _specialist_publish(dest: str, body: dict) -> str:
    """Create version 1 at `dest`, with every pin proved and the specialist created under ONE
    shared lock of the skills library (the lock its writers take exclusively): no skill can
    change between "the pinned revision is what the library holds" and the create."""
    from .. import skill_bundles

    try:
        with skill_library.locked(exclusive=False) as lib_fd:
            for ref in body["skills"]:
                skill_bundles._snapshot(lib_fd, ref["id"], ref["revision"])
            created = specialist_store.create(body, sid=dest)
    except skill_bundles.BundleRefused:
        raise store.Conflict("a skill it pins changed or can't be read during the import") from None
    except specialist_store.StoreError as exc:
        raise _store_errors(exc) from None
    except skill_bundles._DIR_ERRORS:
        raise store.StoreError("the skills library is busy or unavailable", status=503) from None
    if created["id"] != dest:
        raise store.Conflict("the specialist was created under another ID")
    stored = {k: v for k, v in created["current"].items() if k not in ("version", "created_at")}
    return body_digest(stored)


def _specialist_at(dest: str) -> str | None:
    """The digest of the version 1 a specialist at `dest` was created with, `None` when absent."""
    try:
        first = specialist_store.version(dest, 1)
    except specialist_store.NotFound:
        try:
            specialist_store.get(dest)
        except specialist_store.NotFound:
            return None
        except specialist_store.StoreError as exc:
            raise _store_errors(exc) from None
        return "unavailable"  # it exists without a version 1: never ours
    except specialist_store.StoreError as exc:
        raise _store_errors(exc) from None
    return body_digest({k: v for k, v in first.items() if k not in ("version", "created_at")})


def _specialist_ids() -> dict[str, str | None]:
    # Only which ids are taken shapes a plan; an edit to a specialist changes nothing in it.
    try:
        return {sid: None for sid in specialist_store.ids()}
    except specialist_store.StoreError as exc:
        raise _store_errors(exc) from None


# ---- the per-kind table ---------------------------------------------------------------------


def export(kind: str, item_id: str) -> dict:
    """`{title, revision, files, depends_on}` of a local item, read under its store's lock (a
    specialist adds `pins`: `{skill_id: pinned revision}`, which its export must prove)."""
    try:
        if kind == "skill":
            return _skill_export(item_id)
        if kind == "playbook":
            return _playbook_export(item_id)
        if kind == "specialist":
            return _specialist_export(item_id)
    except ItemRefused:
        raise
    except store.NotFound:
        raise ItemRefused(f"there is no {kind} {item_id} here") from None
    except (store.StoreError, gallery_contract.GalleryError, ValueError, OSError):
        raise ItemRefused(
            f"the {kind} {item_id} can't be read here; fix it before exporting"
        ) from None
    raise ItemRefused(f"{kind} items cannot be exported yet")


def validate(kind: str, item_id: str, files: dict[str, bytes]) -> list[str]:
    """Refuse an incoming item, or return the skill IDs it requires (its content's own word)."""
    if kind == "skill":
        return _skill_validate(item_id, files)
    if kind == "playbook":
        return _playbook_validate(item_id, files)
    if kind == "specialist":
        return _specialist_validate(item_id, files)
    raise ItemRefused(f"this install can't import {kind} items yet")


def rewrite(kind: str, item_id: str, dest: str, files: dict[str, bytes], skills: dict) -> dict:
    if kind == "playbook":
        try:
            return _rewrite_playbook(item_id, dest, files, skills)
        except (tomlw.TomlWriteError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ItemRefused(f"this playbook can't be renamed here: {exc}") from None
    if kind == "specialist":
        return _rewrite_specialist(files, skills)  # its id is not in its content
    return dict(files)


def prepare(
    kind: str, files: dict[str, bytes], *, choice: dict | None = None, pins: dict | None = None
) -> tuple[object, str]:
    """What `publish` writes for this item and the digest `at` will report for it once written.

    A specialist becomes the version body its own validator accepts, with the agent, model and
    effort settled in the preview (`choice`) and its skills pinned at the revisions of the copies
    this import made or reused (`pins`); anything it would refuse is `ItemRefused`."""
    if kind == "specialist":
        body = _specialist_prepare(files, choice, pins or {})
        return body, body_digest(body)
    return files, fmt.tree_digest(files)


def publish(kind: str, dest: str, payload) -> str:
    """Create the new copy (NOREPLACE) and return its published revision."""
    if kind == "skill":
        return _skill_publish(dest, payload)
    if kind == "specialist":
        return _specialist_publish(dest, payload)
    return _playbook_publish(dest, payload)


def at(kind: str, dest: str) -> str | None:
    """The revision at `dest` now, `None` when absent, `"unavailable"` when it is not ours."""
    if kind == "skill":
        return _skill_at(dest)
    if kind == "specialist":
        return _specialist_at(dest)
    return _playbook_at(dest)


def ids(kind: str) -> dict[str, str | None]:
    """Every id the store holds now (all sources) with its revision: the preview's binding."""
    if kind == "skill":
        return _skill_ids()
    if kind == "specialist":
        return _specialist_ids()
    return _playbook_ids()


def id_ok(kind: str, value: str) -> bool:
    if kind == "specialist":
        return isinstance(value, str) and bool(specialist_store.ID_RE.fullmatch(value))
    try:
        store.local_id(kind, value)
    except store.StoreError:
        return False
    return True


def catalog() -> list[dict]:
    """Every exportable local item with its dependencies, for the export picker."""
    out: list[dict] = []
    for skill in _skill_listing():
        out.append(
            {
                "kind": "skill",
                "id": skill["id"],
                "title": (skill.get("gallery_origin") or {}).get("title") or skill["id"],
                "depends_on": [],
                "exportable": not skill.get("error"),
                "reason": skill.get("error"),
                "source": "local",
            }
        )
    held = {s["id"] for s in out if s["exportable"]}
    with store.root_lock(exclusive=False) as root_fd:
        entries = store._all_entries(root_fd)
    for entry in entries:
        if not entry.addressable:
            continue
        ok = entry.pb is not None and entry.tree is not None
        deps = [f"skill:{sid}" for sid in _role_skills(entry.pb) if sid in held] if ok else []
        out.append(
            {
                "kind": "playbook",
                "id": entry.id,
                "title": entry.pb["identity"]["name"] if ok else entry.id,
                "depends_on": deps,
                "exportable": ok,
                "reason": None if ok else entry.error,
                "source": entry.source,
            }
        )
    try:
        pool = specialist_store.list_all()
    except specialist_store.StoreError:
        pool = []  # an unreadable pool lists no specialists; skills and playbooks still export
    listing = _skill_listing()
    listed = {s["id"] for s in listing}
    revisions = {s["id"]: s.get("revision") for s in listing if not s.get("error")}
    for spec in pool:
        if spec["archived"]:
            continue  # archived means hidden from new use
        cur = spec["current"]
        bad = next(
            (k["id"] for k in cur["skills"] if revisions.get(k["id"]) != k["revision"]), None
        )
        why = "changed" if bad in revisions else "unreadable" if bad in listed else "missing"
        out.append(
            {
                "kind": "specialist",
                "id": spec["id"],
                "title": cur["title"],
                "depends_on": [f"skill:{k['id']}" for k in cur["skills"]],
                "exportable": bad is None,
                "reason": None if bad is None else _pin_reason(bad, cur["version"], why),
                "source": "local",
            }
        )
    return out


def _pin_reason(sid: str, version: int, why: str) -> str:
    """Why a specialist can't be exported: its pinned skill `sid` is `missing`, `changed` or
    `unreadable` now. The library keeps only each skill's current tree, so the fix is a re-save."""
    what = {
        "missing": "is no longer in your skills library",
        "unreadable": "can't be read in your skills library",
    }.get(why, f"changed since version {version} was saved")
    return (
        f"Skill {sid} {what}, so the version this specialist pins can't be exported. Open the "
        "specialist and save a new version, then export it."
    )
