"""Instructions every present engine reads (#1096 §11, #1191), derived from the roster.

A bundle's instruction material is its single root FILE material whose name is a manifest
instruction file (`kinds.INSTRUCTION_FILES`). Deploy writes that material once and adds a typed
instruction alias to it for every instruction file a PRESENT engine reads that the bundle does
not already declare. No engine is named here: the names come from each manifest's
`instructions.files` (#1189). A bundle with no, or more than one, root instruction file gets no
derived aliases; its declared materials stay exactly as written.

The derived aliases are ordinary `instruction-alias` materials, so review shows them, their
digest covers them, and apply/remove treat them exactly like declared ones (inode-proven
create, ownership, removal). Status reports an engine whose instruction files this deployment
does not hold, for example one installed after apply; re-apply is the fix.
"""

from __future__ import annotations

from .. import engines
from ..fsbrowse import FsError
from ..plugins import kinds
from . import destination, materials, mutation_plan, schema


def present() -> dict[str, list[str]]:
    """Each present agent engine's instruction files, sorted (the review's presence rule)."""
    roster = engines.registry.current()
    out: dict[str, list[str]] = {}
    for provider in roster.providers:
        if not engines.is_agent(provider) or not engines.registry.can_start(provider):
            continue
        files = sorted(set(getattr(provider.manifest, "instructions", ()) or ()))
        if files:
            out[provider.engine_id] = files
    return dict(sorted(out.items()))


def material(bundle: dict) -> str | None:
    """The bundle's instruction material, or None when it has none or more than one."""
    found = [
        m["path"]
        for m in bundle["materials"]
        if m["kind"] == schema.MATERIAL_FILE
        and m["path"] in kinds.INSTRUCTION_FILES
        and m["disposition"] == "managed"
    ]
    return found[0] if len(found) == 1 else None


def include(
    rendered: list[materials.Material], bundle: dict, engines_present: dict[str, list[str]]
) -> list[materials.Material]:
    """Add an alias to the instruction material for each present engine's undeclared file."""
    target = material(bundle)
    if target is None:
        return rendered
    declared = {m.path for m in rendered}
    wanted = sorted({name for files in engines_present.values() for name in files} - declared)
    derived = [materials.Material(name, "managed", target=target) for name in wanted]
    if len(rendered) + len(derived) > schema.MAX_MATERIALS:
        raise materials.MaterialError(
            f"adding instruction aliases for the present engines ({', '.join(wanted)}) would "
            f"exceed the {schema.MAX_MATERIALS}-material limit ({len(rendered)} already); "
            "remove a material from the playbook or declare these instruction files in it"
        )
    return [*rendered, *derived]


def _valid_source(path: str, owned: dict, node, deployment: str | None) -> bool:
    """The instruction material is still the deployment's: the planner's own ownership rules."""
    try:
        if owned["kind"] == "region":
            if node.kind != "file" or not isinstance(deployment, str):
                return False
            mutation_plan._region(path, deployment, node.data, owned, None)
        else:
            mutation_plan._owned(node, owned)
        return True
    except (materials.MaterialError, KeyError, TypeError, ValueError):
        return False


def _covers(name: str, files: dict, node, source: str) -> bool:
    """A link covers its name only if it targets the source AND, when this deployment owns it as
    a managed alias, it is still that exact alias (inode included). A pre-existing reference
    alias is the operator's and only needs to target the source."""
    if node.kind != "symlink" or node.target != source:
        return False
    owned = files.get(name)
    if (
        owned is not None
        and owned.get("kind") == "symlink"
        and owned.get("disposition") == "managed"
    ):
        try:
            mutation_plan._owned(node, owned)
        except materials.MaterialError:
            return False
    return True


def missing(record: dict, engines_present: dict[str, list[str]]) -> list[str]:
    """Present engines whose instruction files are not VERIFIED on disk for this deployment.

    A name counts only when the live destination holds the deployment's instruction material at
    it, or an instruction alias that really targets that material. A recorded reference, an
    absent file or an unrelated operator file does not. An unreadable destination reports every
    present engine (fail closed). Only meaningful with a managed root instruction material.
    """
    files = record.get("files", {})
    sources = [
        path
        for path, owned in files.items()
        if path in kinds.INSTRUCTION_FILES
        and owned.get("kind") in ("file", "region")
        and owned.get("disposition") == "managed"
    ]
    if len(sources) != 1:
        return []
    source = sources[0]
    names = sorted({name for files_ in engines_present.values() for name in files_} | {source})
    try:
        live = destination.snapshot(destination.Folder(**record["destination"]), names)
    except (FsError, OSError, KeyError, TypeError):
        return sorted(engines_present)
    if not _valid_source(source, files[source], live[source], record.get("id")):
        return sorted(engines_present)  # a replaced or deleted source covers nobody
    covered = {name for name in names if name == source or _covers(name, files, live[name], source)}
    return sorted(engine for engine, wanted in engines_present.items() if set(wanted) - covered)
