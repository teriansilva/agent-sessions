"""Scoped project-directory creation (#335 Phase 3) + root-scoped discovery (#465).

A BOUNDED filesystem write surface for the new-session UI: create a directory ONLY directly
beneath an operator-configured base root. The roots are now a settable PREF (``project_roots``),
falling back to the env ``AGENT_SESSIONS_PROJECT_ROOTS`` (an ``os.pathsep``-separated list) when
the pref is empty (#465). The feature is OFF by default — with no roots configured the create
endpoint does not function, so the write surface simply doesn't exist on a default install.

This module is the security boundary; treat every change here as such. Containment is enforced by
``os.path.realpath`` (which resolves ``..`` AND symlinks) plus a strict single-component name
check, so a crafted ``root``/``name`` can never escape a configured root. The SAME effective-roots
definition (``effective_roots``) is the single source of truth for both the mkdir boundary and the
discovery scoping (#465), and ``path_within`` is the shared boundary-aware path test.

Import direction (#465): this module imports ``prefs`` (for the settable
roots/exclusions); ``prefs`` must NOT import this module, so there is no cycle.
"""

from __future__ import annotations

import os

from . import prefs

_MAX_NAME = 255


class ProjectDirError(Exception):
    """A create-project-dir request was rejected. ``status`` is the HTTP code the route maps to:
    404 (feature disabled), 403 (root not allowed / path escape), 422 (bad name), 400 (FS error)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def path_within(cwd: str, base: str) -> bool:
    """Boundary-aware, **filesystem-free** containment test (#465): is ``cwd`` equal to ``base``
    or nested under it? Both are ``normpath``-normalized lexically (never a ``stat`` — the cwds we
    test may point at already-deleted dirs), then compared as ``cwd == base`` or
    ``cwd.startswith(base + os.sep)`` so ``/a`` contains ``/a/b`` but never ``/a-foo``. An empty
    ``cwd`` or ``base`` → ``False``."""
    if not cwd or not base:
        return False
    c = os.path.normpath(cwd)
    b = os.path.normpath(base)
    return c == b or c.startswith(b.rstrip(os.sep) + os.sep)


def _normalize_roots(raw: list[str]) -> list[str]:
    """Normalize a raw list of root dirs: each ``expanduser``-ed, ``realpath``-resolved,
    de-duplicated, and kept only if it is an existing directory (#465). Shared by the prefs roots
    and the env fallback so both go through the SAME existing-dir + realpath filter."""
    out: list[str] = []
    seen: set[str] = set()
    for part in raw:
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


def effective_roots() -> list[str]:
    """The effective base dirs (#465): the ``project_roots`` PREF if the operator has SET one
    (normalized), else the env ``AGENT_SESSIONS_PROJECT_ROOTS`` (back-compat seed for
    ops-provisioned installs). An empty result means the feature is OFF (no roots → today's
    unscoped behaviour).

    Branch on the RAW pref presence, NOT on the normalized result: a non-empty pref whose dirs
    are stale/missing normalizes to ``[]`` but must still WIN over the env (Hermes #467) — else a
    narrow/broken pref would silently widen scope back to the env roots. Only an *unset* pref
    (empty raw list) falls through to the env."""
    raw_pref = prefs.get_project_roots()
    if raw_pref:
        return _normalize_roots(raw_pref)
    raw = os.environ.get("AGENT_SESSIONS_PROJECT_ROOTS", "") or ""
    return _normalize_roots(raw.split(os.pathsep))


def project_roots() -> list[str]:
    """The configured base dirs under which new project folders may be created — the merged
    (prefs-or-env) effective roots (#465). Kept as a named function so the mkdir containment
    boundary (``create_project_dir``) and discovery scoping share ONE source of truth. An empty
    list means the feature is OFF (no roots configured)."""
    return effective_roots()


def in_scope(cwd: str, *, roots: list[str], exclusions: list[str]) -> bool:
    """Whether ``cwd`` is in the discoverable scope (#465): under some root (or no roots at all,
    i.e. the feature off) AND not under any exclusion prefix. Pure + boundary-aware
    (via ``path_within``);
    the existing ``scanner.is_ephemeral_cwd`` ``~/.cache/act`` filter is applied separately where it
    already is."""
    return (not roots or any(path_within(cwd, r) for r in roots)) and not any(
        path_within(cwd, e) for e in exclusions
    )


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
