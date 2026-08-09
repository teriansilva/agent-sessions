"""HOME-sandboxed filesystem browse + create for the folder picker (#448).

A BOUNDED read+create surface rooted at ``$HOME``: list the immediate subdirectories of a path,
and create a directory under a parent — both contained to ``$HOME`` via ``os.path.realpath`` (which
resolves ``..`` AND symlinks). This module is the security boundary for the new-session / Settings
folder picker; treat every change here as such. It is SEPARATE from the operator-root-gated
``project_dirs`` write surface (#335): that one is off-by-default and bounded to configured roots;
this one is rooted at the single-admin user's home (the app already launches agents with permission
bypass in arbitrary cwds, so a home-rooted picker fits the trust model — but it never escapes home).
No shell, ever.
"""

from __future__ import annotations

import os

_MAX_NAME = 255


class FsError(Exception):
    """A browse/create request was rejected. ``status`` is the HTTP code the route maps to:
    403 (path escapes home), 404 (not a directory), 422 (bad name), 400 (filesystem error)."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def home_root() -> str:
    """The realpath-resolved home directory the picker is bounded to. ``AGENT_SESSIONS_FS_ROOT``
    overrides it (tests; an operator who wants a different root)."""
    return os.path.realpath(os.path.expanduser(os.environ.get("AGENT_SESSIONS_FS_ROOT") or "~"))


def contained_path(path: str | None) -> str:
    """Realpath of ``path`` (default: home). Must BE home or live under it, else 403. Resolving
    with realpath collapses ``..`` and symlinks, so a crafted path can't escape the root.

    Public since #783: the file panel reuses this exact boundary rather than drawing a second
    one. Note what it is and isn't — it is a *cheap filter*, not a proof. ``realpath`` then
    ``open`` is check-then-use, and the panel returns file BYTES, so :mod:`agent_sessions.files`
    layers open-then-verify (``O_NOFOLLOW`` acquisition + ``fstat`` + ``/proc/self/fd``
    re-check) on top. Callers that only need a name (the folder picker) are fine with this
    alone; callers that read content are not.
    """
    root = home_root()
    real = os.path.realpath(os.path.expanduser(path)) if path and path.strip() else root
    if real != root and not real.startswith(root + os.sep):
        raise FsError("path escapes the home root", status=403)
    return real


# Back-compat alias for the pre-#783 private name (still used inside this module).
_contained = contained_path


def list_dirs(path: str | None = None) -> tuple[str, list[dict]]:
    """``(resolved_path, immediate subdirectories)`` under ``$HOME``. Each subdir is
    ``{"name", "path"}``; dotfiles are skipped; entries sorted case-insensitively. Symlinked
    directories ARE listed (browsing convenience), but selecting/expanding one re-runs
    :func:`_contained` so it can't escape home. 403 on escape, 404 if not a directory."""
    real = _contained(path or "")
    if not os.path.isdir(real):
        raise FsError("not a directory", status=404)
    try:
        entries = sorted(os.scandir(real), key=lambda e: e.name.lower())
    except OSError as e:
        raise FsError(f"could not read the directory: {e}", status=400) from None
    out: list[dict] = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            is_dir = e.is_dir()  # follows symlinks; selection is re-contained on use
        except OSError:
            continue
        if is_dir:
            out.append({"name": e.name, "path": os.path.join(real, e.name)})
    return real, out


def is_browsable_dir(path: str | None) -> bool:
    """``True`` if ``path`` is an existing directory contained under ``$HOME`` — the set the
    folder picker can browse to, and therefore the set the new-session launch must accept (#457).

    Reuses the same realpath containment as :func:`list_dirs` / :func:`make_dir` (so a ``..`` or
    symlink that escapes home is rejected), then requires the target to be a real directory. A
    pure predicate: it never raises and never escapes home. ``None`` / empty / a path that escapes
    home / a non-existent path / a non-directory all return ``False``.
    """
    if not path or not path.strip():
        return False
    try:
        real = _contained(path)
    except FsError:
        return False
    return os.path.isdir(real)


def _valid_name(name: str) -> bool:
    # A SINGLE path component: no separators, not "."/"..", no control chars, bounded length.
    nm = name.strip()
    if not nm or nm in (".", "..") or len(nm) > _MAX_NAME:
        return False
    if "/" in nm or "\\" in nm or os.sep in nm or (os.altsep and os.altsep in nm):
        return False
    return all(ord(c) >= 32 for c in nm)


def make_dir(parent: str, name: str) -> str:
    """Create directory ``name`` directly under ``parent`` (``mkdir -p`` semantics) and return its
    absolute path. ``parent`` must be contained under ``$HOME`` and ``name`` a single component;
    the realpath of the target must stay strictly under ``parent`` (rejects a symlinked
    ``parent/name`` that points outside). 403 escape / 404 bad parent / 422 bad name / 400 fs."""
    base = _contained(parent or "")
    if not os.path.isdir(base):
        raise FsError("parent is not a directory", status=404)
    if not _valid_name(name):
        raise FsError("invalid folder name", status=422)
    target = os.path.realpath(os.path.join(base, name.strip()))
    if not target.startswith(base + os.sep):
        raise FsError("folder path escapes the parent", status=403)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise FsError(f"could not create the folder: {e}", status=400) from None
    return target


__all__ = ["FsError", "contained_path", "home_root", "is_browsable_dir", "list_dirs", "make_dir"]
