"""First-class project entities (#361): the sidecar store + THE shared resolver.

A *project* is an organizational grouping — pure app-side metadata, decoupled from
the working directory a session launches in. A *folder* (cwd) stays the launch
location. Entities live in ``~/.config/agent-sessions/projects.json``
(``AGENT_SESSIONS_PROJECTS`` override), same conventions as ``metadata.json``:
exclusive ``flock`` for read-modify-write, fail-soft reads, rewrite-in-place under
the lock. Engine stores are never touched — the opencode/codex read-only guarantee
holds.

Assignment resolution is centralized in :func:`resolve` so every row-shaping /
facet / filter / archive-membership path agrees (same lesson as #335's shared
visibility resolver):

1. explicit per-session ``project_id`` (sidecar metadata) — wins always; a dangling
   id (deleted project) falls through, never errors;
2. boundary-aware longest-prefix match of the session cwd against adopted folders
   (``/a/b`` matches a project owning ``/a``, ``/a-foo`` doesn't; the most specific
   owning folder wins);
3. fallback: an implicit *folder ref* — exactly the pre-#361 grouping.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

# Same file-locking discipline as the metadata sidecar — shared helpers, not a copy,
# so the two stores can't drift on lock/rewrite semantics.
from .metadata import _exclusive, _rewrite_in_place, validate_color

log = logging.getLogger(__name__)

# Reserved top-level flag: the one-shot project_alias → entity migration ran (#361).
_MIGRATED_KEY = "alias_migration_done"

# The synthetic "Default" project (#445): a SURFACE-ONLY catch-all for sessions whose cwd no
# project has adopted (the ``kind=="folder"`` fallback rows). It is NOT a stored entity and is
# never returned by :func:`resolve` — `resolve` still yields a folder ref for unadopted cwds so
# the visibility/curation rules (`_visible`, `prefs.project_visible`) are unchanged. Default is
# materialized only in the project-facing surfaces (`/api/sessions` facets + filter, the overview
# graph, the sidebar dropdown), where folders are presented as a sub-property of projects. The
# ``__``-prefix keeps it disjoint from generated ids (``p-<hex>``) and from folder-conflict checks.
DEFAULT_PROJECT_ID = "__default__"
DEFAULT_PROJECT_NAME = "Default"


class ProjectError(Exception):
    """Validation / conflict error with an HTTP status the route layer passes through."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    color: str = ""
    folders: tuple[str, ...] = ()
    archived: bool = False
    created_at: float = 0.0
    # The project's DEFAULT launch folder (#448): where new sessions start unless overridden.
    # Always one of ``folders`` (auto-adopted on create/update). Required for NEW projects at the
    # API; legacy entities may have "" and fall back to ``folders[0]`` on read (see ``_from_raw``).
    default_folder: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "folders": list(self.folders),
            "default_folder": self.default_folder,
            "archived": self.archived,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Ref:
    """What a session resolves to: a project entity or the implicit folder group."""

    kind: str  # "project" | "folder"
    id: str  # project id, or the cwd for a folder ref
    name: str
    color: str = ""

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "id": self.id, "name": self.name}
        if self.kind == "project":
            d["color"] = self.color
        return d


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_PROJECTS",
            str(Path.home() / ".config" / "agent-sessions" / "projects.json"),
        )
    )


def _normalize_folder(folder: object) -> str:
    """Canonical adopted-folder form: absolute path, no trailing slash (except ``/``).

    No realpath — adopted folders may not exist on this host (a project can adopt a
    folder whose sessions came from another machine's scan)."""
    if not isinstance(folder, str) or not folder.strip():
        raise ProjectError("folders must be non-empty strings", status=422)
    f = folder.strip()
    if not f.startswith("/"):
        raise ProjectError(f"folder must be an absolute path: {f!r}", status=422)
    while len(f) > 1 and f.endswith("/"):
        f = f[:-1]
    return f


def _validate_color(color: object) -> str:
    """Write-path validator (#571). Delegates to the shared ``metadata.validate_color``
    so the project-color rule and the per-session-color rule can never drift apart —
    the rule lives in exactly one place (``metadata.validate_color``).

    A ``ValueError`` from the shared validator becomes a ``ProjectError`` here (the
    existing exception the route layer translates into HTTP responses).
    """
    try:
        return validate_color(color)
    except ValueError as e:
        raise ProjectError(str(e), status=422) from None


def _validate_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProjectError("name required", status=422)
    return name.strip()[:120]


def _nested(a: str, b: str) -> bool:
    """True when one path is a boundary-aware ancestor of the other (or equal)."""
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _check_folder_conflicts(folders: list[str], data: dict, self_id: str | None) -> None:
    """A folder belongs to at most one project — enforced on write (#361).

    Conflicts 409 on an exact duplicate AND on a folder nested under (or above)
    another project's folder: two projects must never both own a point on one
    folder path, or which one "wins" would depend on resolver depth instead of
    explicit user intent. Releasing a folder (PATCH with it removed) frees it.
    The same project MAY own nested folders — resolution inside it is moot.
    """
    for fid, raw in data.items():
        if fid == self_id or fid.startswith("__") or not isinstance(raw, dict):
            continue
        for theirs in raw.get("folders") or []:
            if not isinstance(theirs, str):
                continue
            for ours in folders:
                if _nested(ours, theirs):
                    raise ProjectError(
                        f"folder {ours!r} conflicts with {theirs!r} "
                        f"already adopted by project {fid}",
                        status=409,
                    )


def _from_raw(pid: str, raw: dict) -> Project:
    folders = tuple(
        sorted({_safe_folder(f) for f in raw.get("folders") or [] if isinstance(f, str)} - {""})
    )
    # Default launch folder (#448): the stored value if it's still one of the folders, else a
    # deterministic read-time fallback to the first (sorted) folder so legacy projects with folders
    # but no recorded default still launch sensibly. Folderless legacy projects stay "" (the
    # Settings UI prompts to set one; new-session disables launching into them).
    stored_default = _safe_folder(raw.get("default_folder") or "")
    default_folder = (
        stored_default if stored_default in folders else (folders[0] if folders else "")
    )
    return Project(
        id=pid,
        name=str(raw.get("name", "") or ""),
        # Read-path color normalization (#571): any stored value that isn't a valid
        # ``#rgb``/``#rrggbb`` hex degrades to ``""`` (no color) instead of poisoning the
        # sidebar. Mirrors the discipline ``validate_color`` brings to the session sidecar
        # and to the write path — hand-edited ``projects.json`` can never 500 the SPA.
        color=validate_color(raw.get("color", ""), fail_soft=True),
        folders=folders,
        default_folder=default_folder,
        archived=bool(raw.get("archived", False)),
        created_at=(
            float(raw["created_at"])
            if isinstance(raw.get("created_at"), int | float)
            and not isinstance(raw.get("created_at"), bool)
            else 0.0
        ),
    )


def _safe_folder(folder: str) -> str:
    """Fail-soft normalization for READS: never raise on hand-edited data."""
    f = folder.strip()
    if not f.startswith("/"):
        return ""
    while len(f) > 1 and f.endswith("/"):
        f = f[:-1]
    return f


def load(path: Path | None = None) -> dict[str, Project]:
    """Read the store; missing/corrupt files yield ``{}`` (fail-soft, like metadata)."""
    path = path or _default_path()
    if not path.exists():
        return {}
    try:
        with path.open() as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        return {}
    out: dict[str, Project] = {}
    for pid, val in projects.items():
        if isinstance(pid, str) and isinstance(val, dict):
            out[pid] = _from_raw(pid, val)
    return out


def _read_locked(fh) -> dict:
    try:
        text = fh.read()
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    data["version"] = 1
    return data


def _new_id(existing: dict) -> str:
    while True:
        pid = f"p-{secrets.token_hex(4)}"
        if pid not in existing:
            return pid


def create(
    name: str,
    *,
    color: object = None,
    folders: object = None,
    default_folder: object = None,
    path: Path | None = None,
) -> Project:
    """Create an entity. Creating "from a folder" is just ``folders=[cwd]``.

    ``default_folder`` (#448) is the project's default launch folder; when given it is
    auto-adopted into ``folders``. It's optional at this layer (legacy/back-compat), but the
    UI requires one on create (the folder picker)."""
    name = _validate_name(name)
    color_v = _validate_color(color)
    folder_list = [_normalize_folder(f) for f in (folders or [])]
    if len(set(folder_list)) != len(folder_list):
        raise ProjectError("duplicate folder in request", status=422)
    default_v = ""
    if default_folder is not None and str(default_folder).strip():
        default_v = _normalize_folder(default_folder)
        if default_v not in folder_list:
            folder_list.append(default_v)  # the default folder is always an adopted folder (#448)
    path = path or _default_path()
    with _exclusive(path) as fh:
        data = _read_locked(fh)
        _check_folder_conflicts(folder_list, data["projects"], None)
        pid = _new_id(data["projects"])
        entity = {
            "name": name,
            "color": color_v,
            "folders": sorted(folder_list),
            "default_folder": default_v,
            "archived": False,
            "created_at": time.time(),
        }
        data["projects"][pid] = entity
        _rewrite_in_place(fh, data)
        return _from_raw(pid, entity)


def update(
    pid: str,
    *,
    name: object = None,
    color: object = None,
    folders: object = None,
    default_folder: object = None,
    archived: object = None,
    path: Path | None = None,
) -> Project:
    """Patch an entity: rename / recolor / adopt+release folders / set the default launch folder /
    set the archive flag.

    ``None`` means "leave unchanged" for every field (clear color with ``""``). The default folder
    (#448) is reconciled with the (possibly patched) folder set: setting one auto-adopts it; a
    ``folders`` patch that would drop the project's current explicit default folder is rejected
    unless the same request sets a new default (the default must always be one of the folders)."""
    path = path or _default_path()
    with _exclusive(path) as fh:
        data = _read_locked(fh)
        raw = data["projects"].get(pid)
        if not isinstance(raw, dict):
            raise ProjectError("unknown project", status=404)
        if name is not None:
            raw["name"] = _validate_name(name)
        if color is not None:
            raw["color"] = _validate_color(color)

        # Reconcile folders + default_folder together (#448) so "the default is always one of the
        # folders" holds. new_default: None = leave, "" = clear, else the normalized path.
        new_default: str | None = None
        if default_folder is not None:
            new_default = _normalize_folder(default_folder) if str(default_folder).strip() else ""
        if folders is not None:
            if not isinstance(folders, list):
                raise ProjectError("folders must be a list", status=422)
            folder_list = [_normalize_folder(f) for f in folders]
        else:
            folder_list = [
                f
                for f in (_safe_folder(x) for x in raw.get("folders") or [] if isinstance(x, str))
                if f
            ]
        cur_default = _safe_folder(raw.get("default_folder") or "")
        eff_default = new_default if new_default is not None else cur_default
        if eff_default and eff_default not in folder_list:
            if new_default is not None:
                folder_list.append(eff_default)  # setting a default adopts it
            else:
                raise ProjectError(
                    f"cannot release {eff_default!r}: it is the project's default folder — "
                    "set a different default_folder in the same request",
                    status=409,
                )
        if folders is not None or new_default is not None:
            if len(set(folder_list)) != len(folder_list):
                raise ProjectError("duplicate folder in request", status=422)
            _check_folder_conflicts(folder_list, data["projects"], pid)
            raw["folders"] = sorted(folder_list)
        if new_default is not None:
            raw["default_folder"] = eff_default

        if archived is not None:
            if not isinstance(archived, bool):
                raise ProjectError("archived must be a boolean", status=422)
            raw["archived"] = archived
        data["projects"][pid] = raw
        _rewrite_in_place(fh, data)
        return _from_raw(pid, raw)


def delete(pid: str, path: Path | None = None) -> None:
    """Remove the entity only. Member sessions keep their (now dangling) ``project_id``
    and revert to folder grouping on the next resolve — session files are never touched."""
    path = path or _default_path()
    with _exclusive(path) as fh:
        data = _read_locked(fh)
        if pid not in data["projects"]:
            raise ProjectError("unknown project", status=404)
        del data["projects"][pid]
        _rewrite_in_place(fh, data)


# ---- resolution ----------------------------------------------------------------------


def owning_project(cwd: str, projects: dict[str, Project]) -> Project | None:
    """The project whose adopted folder is the most specific boundary-aware prefix of
    ``cwd`` (same semantics as ``web/src/lib/projectTree.ts``), or ``None``."""
    if not cwd:
        return None
    best: Project | None = None
    best_len = -1
    for p in projects.values():
        for f in p.folders:
            if (cwd == f or cwd.startswith(f.rstrip("/") + "/")) and len(f) > best_len:
                best, best_len = p, len(f)
    return best


def resolve(cwd: str, project_id: str, projects: dict[str, Project], *, alias: str = "") -> Ref:
    """THE assignment resolver (#361) — see the module docstring for the precedence.

    ``alias`` is the legacy per-session ``project_alias`` (#148 predecessor), kept ONE
    release as a read fallback for sessions the one-shot migration never saw: it only
    renames the implicit folder ref, it never creates identity.
    """
    if project_id:
        p = projects.get(project_id)
        if p is not None:
            return Ref(kind="project", id=p.id, name=p.name, color=p.color)
        # dangling id (deleted project): fall through, never an error
    p = owning_project(cwd, projects)
    if p is not None:
        return Ref(kind="project", id=p.id, name=p.name, color=p.color)
    # Folder-ref name: the legacy alias when present, else the full cwd — exactly the
    # pre-#361 `project_alias or cwd` string, so zero-entity rendering is unchanged
    # (clients shorten paths themselves, e.g. shortCwd/displayProjectName).
    return Ref(kind="folder", id=cwd, name=alias or cwd)


# ---- one-shot project_alias migration (#361) ------------------------------------------


def ensure_alias_migration(
    entries: list[tuple[str, str, str]],
    *,
    path: Path | None = None,
    metadata_path: Path | None = None,
) -> None:
    """Convert legacy per-session ``project_alias`` renames into project entities, once.

    ``entries`` is ``(session_key, cwd, alias)`` for every scanned session with a
    non-empty alias (the sidecar doesn't know cwds — the caller pairs them from the
    scan). Distinct ``(cwd → alias)`` pairs collapse into ONE entity adopting that
    folder, so today's "renamed projects" survive as visible entities. Guarded by a
    flag in the store, checked and set under the store's exclusive lock; before the
    migrated ``project_alias`` fields are stripped from ``metadata.json`` (the write
    path is retired), the file is backed up once (same precedent as the bare-UUID key
    migration). A one-shot summary is logged so rename-survival is observable.
    """
    path = path or _default_path()
    with _exclusive(path) as fh:
        data = _read_locked(fh)
        if data.get(_MIGRATED_KEY):
            return
        pairs: dict[tuple[str, str], list[str]] = {}
        for key, cwd, alias in entries:
            alias = (alias or "").strip()
            if not alias or not cwd:
                continue
            pairs.setdefault((_safe_folder(cwd) or cwd, alias), []).append(key)
        created: list[tuple[str, str]] = []
        skipped: list[tuple[str, str]] = []
        migrated_keys: list[str] = []
        for (cwd, alias), keys in sorted(pairs.items()):
            try:
                _check_folder_conflicts([cwd], data["projects"], None)
            except ProjectError:
                skipped.append((cwd, alias))  # folder already owned — entity wins
                continue
            pid = _new_id(data["projects"])
            data["projects"][pid] = {
                "name": alias,
                "color": "",
                "folders": [cwd],
                "archived": False,
                "created_at": time.time(),
            }
            created.append((cwd, alias))
            migrated_keys.extend(keys)
        data[_MIGRATED_KEY] = True
        _rewrite_in_place(fh, data)
    if migrated_keys:
        _strip_aliases(migrated_keys, metadata_path)
    if pairs:
        log.info(
            "project_alias migration: %d alias pair(s) found, %d entity(ies) created: %s%s",
            len(pairs),
            len(created),
            "; ".join(f"{alias!r} ← {cwd}" for cwd, alias in created) or "none",
            f"; skipped (folder already adopted): {skipped}" if skipped else "",
        )


def _strip_aliases(keys: list[str], metadata_path: Path | None) -> None:
    """Retire the migrated ``project_alias`` fields from the metadata sidecar, after a
    one-time backup so a botched migration is reversible. Unmigrated aliases (sessions
    not visible to the scan that triggered the migration) are left for the read
    fallback in :func:`resolve`."""
    from . import metadata

    mpath = metadata_path or metadata._default_path()
    if not mpath.exists():
        return
    with _exclusive(mpath) as fh:
        try:
            text = fh.read()
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        bak = mpath.with_name(mpath.name + ".pre-projects.bak")
        if not bak.exists():
            bak.write_text(text)
        changed = False
        for key in keys:
            entry = data.get(key)
            if isinstance(entry, dict) and entry.get("project_alias"):
                entry["project_alias"] = ""
                changed = True
        if changed:
            _rewrite_in_place(fh, data)


__all__ = [
    "DEFAULT_PROJECT_ID",
    "DEFAULT_PROJECT_NAME",
    "Project",
    "ProjectError",
    "Ref",
    "load",
    "create",
    "update",
    "delete",
    "owning_project",
    "resolve",
    "ensure_alias_migration",
]
