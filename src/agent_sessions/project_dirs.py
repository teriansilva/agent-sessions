"""Scoped project-directory creation (#335 Phase 3).

A BOUNDED filesystem write surface for the new-session UI: create a directory ONLY directly
beneath an operator-configured base root (``AGENT_SESSIONS_PROJECT_ROOTS``, an ``os.pathsep``-
separated list). The feature is OFF by default — with no roots configured the endpoint does not
function, so the write surface simply doesn't exist on a default install.

This module is the security boundary; treat every change here as such. Containment is enforced by
``os.path.realpath`` (which resolves ``..`` AND symlinks) plus a strict single-component name
check, so a crafted ``root``/``name`` can never escape a configured root.
"""

from __future__ import annotations

import os

_MAX_NAME = 255


class ProjectDirError(Exception):
    """A create-project-dir request was rejected. ``status`` is the HTTP code the route maps to:
    404 (feature disabled), 403 (root not allowed / path escape), 422 (bad name), 400 (FS error)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def project_roots() -> list[str]:
    """The configured base dirs under which new project folders may be created — each
    ``expanduser``-ed, ``realpath``-resolved, de-duplicated, and kept only if it is an existing
    directory. An empty list means the feature is OFF (no roots configured)."""
    raw = os.environ.get("AGENT_SESSIONS_PROJECT_ROOTS", "") or ""
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(os.pathsep):
        p = part.strip()
        if not p:
            continue
        real = os.path.realpath(os.path.expanduser(p))
        if real in seen:
            continue
        if os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


def _valid_name(name: str) -> bool:
    # A SINGLE path component only: no separators (so it can't be a sub-path), not "."/"..",
    # no control chars, bounded length. Combined with the realpath-containment check below this
    # makes traversal/symlink escape impossible.
    nm = name.strip()
    if not nm or nm in (".", "..") or len(nm) > _MAX_NAME:
        return False
    if "/" in nm or "\\" in nm or os.sep in nm or (os.altsep and os.altsep in nm):
        return False
    return all(ord(c) >= 32 for c in nm)


def create_project_dir(root: str, name: str) -> str:
    """Idempotently create directory ``name`` directly under the configured base ``root`` and
    return its absolute path (``mkdir -p`` semantics — an existing dir under the root is a success
    that just selects it). Raises ``ProjectDirError`` (with a mapped ``status``) when:
      * no roots are configured (feature off) — 404,
      * ``root`` is not one of the configured roots, or the resolved target escapes it — 403,
      * ``name`` is empty / "."/".." / contains a separator or control char / too long — 422,
      * the filesystem op fails — 400.
    """
    roots = project_roots()
    if not roots:
        raise ProjectDirError(
            "folder creation is disabled (no AGENT_SESSIONS_PROJECT_ROOTS configured)", status=404
        )
    base = os.path.realpath(os.path.expanduser(root))
    if base not in roots:
        raise ProjectDirError("root is not an allowed project root", status=403)
    if not _valid_name(name):
        raise ProjectDirError("invalid folder name", status=422)
    target = os.path.realpath(os.path.join(base, name.strip()))
    # Strict containment: target must be base/<child>, never base itself nor anything outside it.
    if target == base or not target.startswith(base + os.sep):
        raise ProjectDirError("folder path escapes the project root", status=403)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ProjectDirError(f"could not create the folder: {e}", status=400) from None
    return target
