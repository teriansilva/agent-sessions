"""What each kind contributes to a share bundle (#1714 P1: skills and playbooks).

A kind reads its local item for export, validates and rewrites an incoming item, publishes a new
copy through its store's own new-copy path (NOREPLACE, full validation, read-back) and answers
"what is at this destination now" for crash reconciliation. Nothing here activates, deploys,
launches or sets a default: an import only ever creates an inert new copy.
"""

from __future__ import annotations

import base64
import tomllib

from ..gallery import contract as gallery_contract
from ..gallery import files as skill_files
from ..gallery import skills as skill_library
from ..playbooks import schema, store, tomlw
from . import format as fmt

#: Apply order across kinds (#1714 "Partial failure"): skills (and later variable definitions)
#: first, then templates, then playbooks and specialists, then memory.
LEVEL = {"skill": 0, "playbook": 2}
KINDS = tuple(LEVEL)
#: The Library sections a picker groups by, in order.
LABELS = {"skill": "Skill", "playbook": "Playbook"}


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


# ---- the per-kind table ---------------------------------------------------------------------


def export(kind: str, item_id: str) -> dict:
    """`{title, revision, files, depends_on}` of a local item, read under its store's lock."""
    try:
        if kind == "skill":
            return _skill_export(item_id)
        if kind == "playbook":
            return _playbook_export(item_id)
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
    raise ItemRefused(f"this install can't import {kind} items yet")


def rewrite(kind: str, item_id: str, dest: str, files: dict[str, bytes], skills: dict) -> dict:
    if kind == "playbook":
        try:
            return _rewrite_playbook(item_id, dest, files, skills)
        except (tomlw.TomlWriteError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ItemRefused(f"this playbook can't be renamed here: {exc}") from None
    return dict(files)


def publish(kind: str, dest: str, files: dict[str, bytes]) -> str:
    """Create the new copy (NOREPLACE) and return its published revision."""
    return _skill_publish(dest, files) if kind == "skill" else _playbook_publish(dest, files)


def at(kind: str, dest: str) -> str | None:
    """The revision at `dest` now, `None` when absent, `"unavailable"` when it is not ours."""
    return _skill_at(dest) if kind == "skill" else _playbook_at(dest)


def ids(kind: str) -> dict[str, str | None]:
    """Every id the store holds now (all sources) with its revision: the preview's binding."""
    return _skill_ids() if kind == "skill" else _playbook_ids()


def id_ok(kind: str, value: str) -> bool:
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
    return out
