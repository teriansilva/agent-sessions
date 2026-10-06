"""Pure, ownership-bound deployment mutations; no destination writes or secret reads (#1191).

A plan contains exact before/after bytes for review and the ownership to record only after its
writer settles. Seeds survive updates and removal. References never write. Existing operator
text can receive a managed region; an existing binary or unsupported comment format refuses.
"""

from __future__ import annotations

import base64
import difflib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .. import filewrite
from . import destination, materials, schema, store
from .tree import check_segment


@dataclass(frozen=True)
class Change:
    path: str
    action: str
    before: destination.Node
    after: destination.Node
    ownership: dict | None


def ownership(raw: object) -> dict[str, dict]:
    """Persisted ownership is input too; never derive a mutation from a malformed record."""
    if not isinstance(raw, dict) or len(raw) > schema.MAX_MATERIALS:
        raise store.Conflict("the deployment ownership record is damaged")
    for path, record in raw.items():
        try:
            for segment in filewrite.validate_relpath(path):
                check_segment(segment, path)
            if not isinstance(record, dict):
                raise ValueError
            kind, disposition = record.get("kind"), record.get("disposition")
            expected = {"kind", "disposition"}
            if kind in {"file", "region"} and disposition == "managed":
                expected.add("digest")
                store.revision(record.get("digest"))
                if kind == "region" and "separator" in record:
                    store.revision(record["separator"])
                    expected.add("separator")
            elif kind == "symlink" and disposition == "managed":
                expected.add("target")
                target = record.get("target")
                if (
                    not isinstance(target, str)
                    or "/" in target
                    or "/" in path
                    or not (target.endswith(".md") and path.endswith(".md"))
                ):
                    raise ValueError
                check_segment(target, path)
            elif kind not in {"reference", "seed"} or disposition != kind:
                raise ValueError
            if kind in {"file", "symlink"} and disposition == "managed" and "inode" in record:
                # The exact inode the apply installed; regions are keyed by content instead,
                # since operators legitimately rewrite the file around them.
                inode = record["inode"]
                if (
                    not isinstance(inode, list)
                    or len(inode) != 2
                    or not all(type(i) is int for i in inode)
                ):
                    raise ValueError
                expected.add("inode")
            if set(record) != expected:
                raise ValueError
        except (ValueError, TypeError, store.StoreError):
            raise store.Conflict("the deployment ownership record is damaged") from None
    return raw


#: An owned entry the operator deleted is their decision; an update never silently undoes it.
_DELETED = "the operator deleted this managed material; remove the deployment to re-create it"


def _file(data: bytes) -> destination.Node:
    return destination.Node("file", data=data)


def _region_style(path: str) -> tuple[bytes, bytes]:
    extension = PurePosixPath(path).suffix.lower()
    if extension in (".md", ".markdown", ".html", ".htm"):
        return b"<!-- ", b" -->"
    if extension in (".yml", ".yaml", ".toml", ".sh", ".py", ".txt"):
        return b"# ", b""
    raise materials.MaterialError("this file format cannot contain a managed region")


def _markers(path: str, deployment: str) -> tuple[bytes, bytes]:
    # Keep the existing validated region identity, with the destination's own comment syntax.
    begin, end = materials._markers(deployment)
    prefix, suffix = _region_style(path)
    return prefix + begin[5:-4] + suffix, prefix + end[5:-4] + suffix


def _block(path: str, deployment: str, body: bytes) -> tuple[bytes, str]:
    body.decode("utf-8")
    begin, end = _markers(path, deployment)
    if begin in body or end in body:
        raise materials.MaterialError("the material contains its managed region markers")
    interior = b"\n" + body + (b"" if body.endswith(b"\n") else b"\n")
    return begin + interior + end, materials.digest(interior)


def _region(path: str, deployment: str, current: bytes, record: dict, body: bytes | None):
    begin, end = _markers(path, deployment)
    if current.count(begin) != 1 or current.count(end) != 1:
        raise materials.MaterialError("both managed region markers must occur exactly once")
    start, finish = current.index(begin), current.index(end)
    interior = start + len(begin)
    if finish < interior or materials.digest(current[interior:finish]) != record["digest"]:
        raise materials.MaterialError("the managed region has operator edits")
    replacement, digest = (b"", None) if body is None else _block(path, deployment, body)
    head, tail = current[:start], current[finish + len(end) :]
    if body is None:
        # Take back exactly what insertion added: the newline after the block, and the one
        # before it when the operator's original text had no final newline.
        if tail.startswith(b"\n"):
            tail = tail[1:]
        original = record.get("separator")
        if original and head.endswith(b"\n") and materials.digest(head[:-1]) == original:
            head = head[:-1]
    return head + replacement + tail, digest


def _owned(current: destination.Node, record: dict) -> None:
    kind = record["kind"]
    if kind == "file":
        good = current.kind == "file" and materials.digest(current.data) == record["digest"]
    elif kind == "symlink":
        good = current.kind == "symlink" and current.target == record["target"]
    else:
        good = False
    if good and "inode" in record and list(current.identity[:2]) != record["inode"]:
        good = False  # same bytes, different inode: someone else's file now holds the name
    if not good:
        raise materials.MaterialError("the managed material has operator edits")


def _undo(path: str, current: destination.Node, record: dict | None, deployment: str):
    if record is None or record["disposition"] in ("seed", "reference"):
        return current
    if current.kind == "absent":
        return current
    if record["kind"] == "region":
        if current.kind != "file":
            raise materials.MaterialError("the managed region is no longer a regular file")
        data, _ = _region(path, deployment, current.data, record, None)
        return _file(data)
    _owned(current, record)
    return destination.Node("absent")


def _next(material, current, record, deployment):
    path, disposition = material.path, material.disposition
    # Demotion drops ownership without modifying the existing bytes. An update can no longer
    # claim an operator's seed or reference unless reference-to-managed adoption succeeds.
    if disposition == "reference":
        return current, {"kind": "reference", "disposition": disposition}
    if disposition == "seed":
        # Written once: deleting a seed is also a project edit, not a request to recreate it.
        after = (
            _file(material.data)
            if current.kind == "absent" and not (record and record["disposition"] == "seed")
            else current
        )
        return after, {"kind": "seed", "disposition": disposition}
    if material.target is not None:
        after = destination.Node("symlink", target=material.target)
        if current.kind == "absent":
            if record and record["disposition"] == "managed":
                raise materials.MaterialError(_DELETED)
            return after, {"kind": "symlink", "disposition": disposition, "target": after.target}
        if record and record.get("kind") == "symlink":
            _owned(current, record)
            return after, {"kind": "symlink", "disposition": disposition, "target": after.target}
        if current.kind == "symlink" and current.target == material.target:
            # An already-present matching alias is useful but is not ours to remove later.
            return current, {"kind": "reference", "disposition": "reference"}
        raise materials.MaterialError("the instruction alias is already occupied")
    data = material.data
    assert data is not None
    if record and record["kind"] == "symlink":
        # An owned alias that is gone or replaced is ownership loss, never an operator file to
        # overlay; an intact one cannot silently change kind in place either.
        _owned(current, record)
        raise materials.MaterialError(
            "an instruction alias cannot become a file in place; remove the deployment first"
        )
    if record and record["disposition"] == "reference":
        digest = materials.require_adoption(current.data, data, regular_file=current.kind == "file")
        return current, {"kind": "file", "disposition": disposition, "digest": digest}
    if record and record["disposition"] == "seed":
        raise materials.MaterialError("a seed belongs to the project and cannot become managed")
    if current.kind == "absent":
        if record and record["kind"] in ("file", "region"):
            raise materials.MaterialError(_DELETED)
        return _file(data), {
            "kind": "file",
            "disposition": disposition,
            "digest": materials.digest(data),
        }
    if record and record["kind"] == "file":
        _owned(current, record)
        return _file(data), {
            "kind": "file",
            "disposition": disposition,
            "digest": materials.digest(data),
        }
    if current.kind != "file":
        raise materials.MaterialError("a managed material needs a regular file")
    if b"\0" in current.data:
        raise materials.MaterialError("a managed region cannot be added to a binary file")
    if record and record["kind"] == "region":
        changed, digest = _region(path, deployment, current.data, record, data)
        separated = record.get("separator", False)
    else:
        current.data.decode("utf-8")
        begin, end = _markers(path, deployment)
        if begin in current.data or end in current.data:
            raise materials.MaterialError("the destination already contains managed region markers")
        block, digest = _block(path, deployment, data)
        separated = (
            materials.digest(current.data)
            if current.data and not current.data.endswith(b"\n")
            else False
        )
        changed = current.data + (b"\n" if separated else b"") + block + b"\n"
    owned = {"kind": "region", "disposition": disposition, "digest": digest}
    if separated:
        # Insertion added a newline BEFORE the block. Record the original text it followed, so
        # removal takes that newline back only while that text is still exactly what preceded it.
        owned["separator"] = separated
    return _file(changed), owned


def _same(a: destination.Node, b: destination.Node) -> bool:
    return (a.kind, a.data, a.target) == (b.kind, b.data, b.target)


def build(
    rendered: list[materials.Material],
    nodes: dict[str, destination.Node],
    owned: dict[str, dict],
    deployment: str,
) -> tuple[list[Change], list[dict]]:
    """Return exact candidate changes and reviewable conflicts, without applying any of them."""
    wanted = {m.path: m for m in rendered}
    changes, conflicts = [], []
    total = 0
    for path in sorted(set(wanted) | set(owned)):
        current = nodes[path]
        material, previous = wanted.get(path), owned.get(path)
        try:
            if material is None:
                after, ownership = _undo(path, current, previous, deployment), None
            else:
                after, ownership = _next(material, current, previous, deployment)
            size = len(after.data or b"")
            total += size
            if size > schema.MAX_FILE_BYTES or total > schema.MAX_TOTAL_BYTES:
                raise materials.MaterialError(
                    "the resulting materials exceed the bundle size limit"
                )
            action = (
                "keep"
                if _same(current, after)
                else (
                    "remove"
                    if after.kind == "absent"
                    else "create"
                    if current.kind == "absent"
                    else "replace"
                )
            )
            changes.append(Change(path, action, current, after, ownership))
        except (materials.MaterialError, UnicodeDecodeError) as error:
            reason = (
                "existing and rendered regions must be UTF-8 text"
                if isinstance(error, UnicodeDecodeError)
                else str(error)
            )
            conflicts.append(
                {
                    "path": path,
                    "reason": reason,
                    "before": content(current),
                    "proposed": content(_file(material.data))
                    if material and material.data is not None
                    else None,
                }
            )
    return changes, conflicts


def content(node: destination.Node) -> dict:
    out = {"kind": node.kind}
    if node.kind == "symlink":
        out["target"] = node.target
    elif node.kind == "file":
        data = node.data
        out["digest"] = materials.digest(data)
        try:
            out["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            out["base64"] = base64.b64encode(data).decode("ascii")
    return out


def public(change: Change) -> dict:
    before, after = content(change.before), content(change.after)
    diff = None
    if "base64" not in before and "base64" not in after:
        diff = "".join(
            difflib.unified_diff(
                before.get("text", "").splitlines(keepends=True),
                after.get("text", "").splitlines(keepends=True),
                "a/" + change.path,
                "b/" + change.path,
            )
        )
    return {
        "path": change.path,
        "action": change.action,
        "before": before,
        "after": after,
        "diff": diff,
    }
