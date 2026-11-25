"""A bounded non-secret pre-state for retrying an already accepted deployment operation.

An apply changes its own destination snapshot. Its retry recomputes source, bindings, roster,
project and root-policy facts with the original pre-state, then requires the original HMAC.
This is not permission to skip current files: the writer must separately reconcile every live
entry against that accepted before/after plan under the project and effect fences.
"""

from __future__ import annotations

import base64
import hmac
import stat

from .. import filewrite
from ..fsbrowse import FsError
from . import destination, review, schema, store
from .tree import check_segment


def freeze(plan: review.Plan) -> dict:
    """Keep reviewed material bytes, never input secret values or even secret envelopes."""
    if plan.fingerprint["inputs"]["bindings"]:
        raise store.Conflict("bind the reviewed inputs before starting a deployment operation")
    return {
        "version": 1,
        "digest": plan.public["digest"],
        "prestate": {
            path: {
                "kind": node.kind,
                "identity": list(node.identity),
                "data": base64.b64encode(node.data).decode() if node.data is not None else None,
                "target": node.target,
            }
            for path, node in plan.nodes.items()
        },
    }


def _nodes(raw: object) -> tuple[str, dict[str, destination.Node]]:
    try:
        if not isinstance(raw, dict) or set(raw) != {"version", "digest", "prestate"}:
            raise ValueError
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ValueError
        digest = store.revision(raw["digest"])
        snapshot = raw["prestate"]
        if (
            not isinstance(snapshot, dict)
            or len(snapshot) > schema.MAX_MATERIALS * filewrite.MAX_DEPTH
        ):
            raise ValueError
        nodes, size = {}, 0
        for path, row in snapshot.items():
            if len(path) > schema.PATH_MAX:
                raise ValueError
            if not isinstance(row, dict) or set(row) != {"kind", "identity", "data", "target"}:
                raise ValueError
            kind, identity = row["kind"], row["identity"]
            if kind not in {"absent", "directory", "file", "symlink"}:
                raise ValueError
            # HEAD is forbidden as a file, but is legal as an intermediate directory. Validate
            # its original role rather than treating every parent snapshot as a leaf write.
            checked_path = (
                path + "/snapshot"
                if kind in {"directory", "absent"} and path.split("/")[-1] == "HEAD"
                else path
            )
            filewrite.validate_relpath(checked_path)
            for segment in path.split("/"):
                check_segment(segment, path)
            if not isinstance(identity, list) or any(type(i) is not int for i in identity):
                raise ValueError
            if len(identity) != (0 if kind == "absent" else 9):
                raise ValueError
            if kind != "absent":
                if any(identity[i] < 0 for i in (0, 1, 2, 3, 4, 7, 8)):
                    raise ValueError
                valid = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "symlink": stat.S_ISLNK}
                if not valid[kind](identity[2]) or identity[3] < 1:
                    raise ValueError
            data = None
            if kind == "file":
                if not isinstance(row["data"], str) or len(row["data"]) > 4 * (
                    (schema.MAX_FILE_BYTES + 2) // 3
                ):
                    raise ValueError
                data = base64.b64decode(row["data"], validate=True)
                size += len(data)
                if (
                    len(data) != identity[4]
                    or identity[3] != 1
                    or len(data) > schema.MAX_FILE_BYTES
                    or size > schema.MAX_TOTAL_BYTES
                ):
                    raise ValueError
            elif row["data"] is not None:
                raise ValueError
            target = row["target"]
            if kind == "symlink":
                if (
                    not isinstance(target, str)
                    or not target
                    or "\x00" in target
                    or len(target) > 4096
                ):
                    raise ValueError
            elif target is not None:
                raise ValueError
            nodes[path] = destination.Node(kind, tuple(identity), data, target)
        return digest, nodes
    except (ValueError, TypeError, KeyError, FsError, store.StoreError):
        raise store.Conflict("the accepted operation's pre-state is damaged") from None


def resume(
    playbook_id: str,
    inputs: dict,
    basis: dict,
    frozen: dict,
    *,
    key: str,
    variables: list[dict] | None = None,
) -> review.Plan:
    """Recompute the accepted plan; no filesystem or operation-state mutation occurs here."""
    digest, nodes = _nodes(frozen)
    try:
        plan = review.build(
            playbook_id, inputs, key=key, _record=basis, _variables=variables, _prestate=nodes
        )
    except KeyError:
        raise store.Conflict("the accepted operation's pre-state is incomplete") from None
    if not hmac.compare_digest(digest, plan.public["digest"]):
        raise store.Conflict("the accepted operation's inputs changed; no new effect was admitted")
    return plan
