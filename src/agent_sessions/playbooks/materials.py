"""Pure material rendering and managed-region edits for deployment review (#1191).

Inputs are the P1-validated bundle snapshot and resolved TEXT variables. There is no filesystem,
secret resolution, probe, agent dispatch or mutation here. The lifecycle owns destination checks,
the review digest and the descriptor-relative writes of these exact bytes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..template_send import FIELD_TOKEN_RE
from . import schema
from .tree import Tree


class MaterialError(ValueError):
    """A material cannot be rendered or safely changed; return to deployment review."""


@dataclass(frozen=True)
class Material:
    path: str
    disposition: str
    data: bytes | None = None
    target: str | None = None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Both token kinds in ONE pass, so a substituted value is never parsed again as a token.
_TOKENS = re.compile(f"{FIELD_TOKEN_RE.pattern}|{schema.SECRET_PATH_RE.pattern}")


def secret_names(bundle: dict, tree: Tree) -> list[str]:
    """The secret variables whose reference FILE the template materials name, sorted."""
    names = set()
    for material in bundle["materials"]:
        if material["kind"] == schema.MATERIAL_ALIAS or not material["template"]:
            continue
        text = tree.files[f"{schema.MATERIALS_DIR}/{material['path']}"].decode("utf-8")
        names.update(m.group(1) for m in schema.SECRET_PATH_RE.finditer(text))
    return sorted(names)


def render(
    bundle: dict, tree: Tree, values: dict[str, str], secret_paths: dict[str, str] | None = None
) -> list[Material]:
    """Render literal tokens once; preserve verbatim and binary materials byte for byte.

    The caller supplies only resolved text, and for `{{secret_path:<name>}}` the reference file's
    PATH (never a secret value). Defensive checks also refuse a secret/unknown token or an
    unresolved/non-text value rather than carrying it into a workspace document. Values
    themselves are never parsed a second time as templates. Expansion retains the bundle bounds.
    """
    fields = {field["name"]: field for field in bundle["variables"]}
    paths = secret_paths or {}
    out: list[Material] = []
    total = 0
    for material in bundle["materials"]:
        path = material["path"]
        disposition = material["disposition"]
        if material["kind"] == schema.MATERIAL_ALIAS:
            out.append(Material(path, disposition, target=material["target"]))
            continue
        data = tree.files[f"{schema.MATERIALS_DIR}/{path}"]
        if material["template"]:
            text = data.decode("utf-8")

            def one(match: re.Match, path: str = path) -> str:
                name = match.group(1)
                if name is None:
                    secret = match.group(2)
                    if secret not in fields or fields[secret]["kind"] != "secret":
                        raise MaterialError(f"{path}: {secret} is not a declared secret variable")
                    if not isinstance(paths.get(secret), str):
                        raise MaterialError(f"{path}: {secret} has no secret reference file")
                    return paths[secret]
                if name not in fields or fields[name]["kind"] != "text":
                    raise MaterialError(f"{path}: {name} is not a declared text variable")
                if not isinstance(values.get(name), str):
                    raise MaterialError(f"{path}: {name} needs a resolved text value")
                return values[name]

            data = _TOKENS.sub(one, text).encode("utf-8")
        total += len(data)
        if len(data) > schema.MAX_FILE_BYTES or total > schema.MAX_TOTAL_BYTES:
            raise MaterialError(f"{path}: rendered materials exceed the bundle size limit")
        out.append(Material(path, disposition, data=data))
    return out


_REGION_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _markers(region_id: str) -> tuple[bytes, bytes]:
    if not isinstance(region_id, str) or not _REGION_ID.fullmatch(region_id):
        raise MaterialError("invalid managed region identity")
    return (
        f"<!-- BattleLab playbook {region_id} begin -->".encode(),
        f"<!-- BattleLab playbook {region_id} end -->".encode(),
    )


def region_block(region_id: str, body: bytes) -> tuple[bytes, str]:
    """The exact block and interior digest recorded by apply; markers cannot occur in its body."""
    begin, end = _markers(region_id)
    if begin in body or end in body:
        raise MaterialError("the material contains its managed region markers")
    interior = b"\n" + body + (b"" if body.endswith(b"\n") else b"\n")
    return begin + interior + end, digest(interior)


def append_region(current: bytes, region_id: str, body: bytes) -> tuple[bytes, str]:
    """Append a new region without adopting existing markers or replacing operator content."""
    begin, end = _markers(region_id)
    if begin in current or end in current:
        raise MaterialError("the destination already contains managed region markers")
    block, written = region_block(region_id, body)
    separator = b"\n" if current and not current.endswith(b"\n") else b""
    return current + separator + block + b"\n", written


def change_region(
    current: bytes, region_id: str, expected: str, body: bytes | None
) -> tuple[bytes, str | None]:
    """Replace/remove one unchanged region, preserving every byte outside its two markers.

    Missing, duplicate or reordered markers and edits inside the region always refuse. A matching
    whole-file digest is insufficient: the record must identify what this deployment wrote.
    """
    begin, end = _markers(region_id)
    if current.count(begin) != 1 or current.count(end) != 1:
        raise MaterialError("both managed region markers must occur exactly once")
    start = current.index(begin)
    finish = current.index(end)
    interior = start + len(begin)
    if finish < interior:
        raise MaterialError("managed region markers are out of order")
    if digest(current[interior:finish]) != expected:
        raise MaterialError("the managed region has operator edits")
    replacement, written = (b"", None) if body is None else region_block(region_id, body)
    return current[:start] + replacement + current[finish + len(end) :], written


def require_adoption(current: bytes | None, rendered: bytes, *, regular_file: bool) -> str:
    """A reference becomes managed only by adopting identical bytes from a regular file."""
    if not regular_file or current is None:
        raise MaterialError("adoption requires an existing regular file")
    if current != rendered:
        raise MaterialError("the reference file differs from the rendered material")
    return digest(rendered)
