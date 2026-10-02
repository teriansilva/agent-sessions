"""Load playbook bundles from a bundled source, fail-soft per bundle (#1190).

`load_bundle` validates one bundle directory and raises `PlaybookFormatError`. `list_bundles` is
the listing every later surface reads: one card per entry of the source root, and **one bad
bundle disables only itself** — its card carries `ok: false` and the error, and every other bundle
is still listed. Nothing here writes, fetches, or executes; `requires_status` is a
`shutil.which` presence check by name and nothing more.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import schema
from .errors import PlaybookFormatError
from .tree import Tree, read_tree
from .validate import is_note, validate_tree

log = logging.getLogger(__name__)

#: The in-tree source that ships with the release (#1096 §8's `bundled` source). Empty until the
#: bundled playbooks land (P11, #1200); an absent directory lists as no playbooks.
BUNDLED_ROOT = Path(__file__).resolve().parent / "bundled"


def load_bundle(path: str | os.PathLike) -> dict:
    """The normalized playbook at `path`, or `PlaybookFormatError`."""
    return validate_named(read_tree(path), Path(path).name)


def validate_named(tree: Tree, name: str) -> dict:
    """Validate a snapshot that is (or will be) stored under the directory `name`: the bundle's
    `identity.id` must equal it."""
    pb = validate_tree(tree)
    if pb["identity"]["id"] != name:
        raise PlaybookFormatError(
            f"{schema.MANIFEST_NAME}: identity.id",
            f"{pb['identity']['id']!r} must equal the bundle's directory name {name!r}",
        )
    return pb


def card(pb: dict) -> dict:
    """What a gallery card shows for a valid playbook (#1096 §4): identity, the flow preview with
    each step's actor, and what the playbook ships and requires."""
    ident = pb["identity"]
    return {
        "id": ident["id"],
        "ok": True,
        "error": None,
        "name": ident["name"],
        "publisher": ident["publisher"],
        "version": ident["version"],
        "domain": ident["domain"],
        "summary": ident["summary"],
        "default_flow": pb["default_flow"],
        "flows": [
            {
                "id": f["id"],
                "title": f["title"],
                "steps": [
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "actor": dict(s["actor"]),
                        "after": list(s["after"]),
                        "note": is_note(s),
                    }
                    for s in f["steps"]
                ],
            }
            for f in pb["flows"].values()
        ],
        "ships": {
            "materials": len(pb["materials"]),
            "runbooks": len(pb["runbooks"]),
            "templates": len(pb["templates"]),
            "variables": len(pb["variables"]),
        },
        "connections": [c["kind"] for c in pb["connections"]],
        "requires": {k: list(v) for k, v in pb["requires"].items()},
        "capabilities": sorted(k for k, v in pb["capabilities"].items() if v),
    }


def _error_card(name: str, error: str) -> dict:
    return {"id": name, "ok": False, "error": error}


def list_bundles(root: str | os.PathLike | None = None) -> list[dict]:
    """One card per bundle under `root` (default: `BUNDLED_ROOT`), valid or not, sorted by name."""
    base = Path(root) if root is not None else BUNDLED_ROOT
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("playbook source %s cannot be read: %s", base, e.strerror)
        return []
    try:
        with os.scandir(fd) as it:
            names = sorted(e.name for e in it)
    finally:
        os.close(fd)
    cards: list[dict] = []
    if len(names) > schema.MAX_BUNDLES:
        # Never a silent drop: every entry past the bound gets a card saying why it was not read.
        log.warning("playbook source %s holds more than %d entries", base, schema.MAX_BUNDLES)
        overflow = names[schema.MAX_BUNDLES :]
        names = names[: schema.MAX_BUNDLES]
    else:
        overflow = []
    for name in names:
        try:
            cards.append(card(load_bundle(base / name)))
        except PlaybookFormatError as e:
            cards.append(_error_card(name, str(e)))
        except (OSError, RecursionError, ValueError) as e:  # never let one bundle take the rest
            cards.append(_error_card(name, f"could not be read ({type(e).__name__})"))
    for name in overflow:
        cards.append(
            _error_card(name, f"not read: the source holds more than {schema.MAX_BUNDLES} entries")
        )
    return cards


def requires_status(pb: dict) -> dict:
    """Is each required binary present on PATH? A `shutil.which` lookup by name — nothing is run,
    fetched or installed (#1096 §1: a playbook is not an installer)."""
    return {
        "binaries": {name: shutil.which(name) is not None for name in pb["requires"]["binaries"]}
    }
