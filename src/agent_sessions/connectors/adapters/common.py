"""What every `versioning` adapter shares (#1826): validation of the parts a path is built from,
the bounds, and the two results whose rules must not differ between forges — the combined CI
state, and a file read as text.

An adapter module (`forgejo`, `github`) exposes the same functions, so `connectors.hq` calls
whichever one the instance's manifest names (`adapters.for_kind`):
``probe``, ``search``, ``read_repo``, ``read_issue``, ``read_ci``, ``list_dir``, ``read_file``.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote

from ...connector_http import ConnectorError

NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BRANCH_MAX = 250
PATH_MAX = 1000
SEGMENT_MAX = 255
SEARCH_LIMIT = 20
COMMENTS_MAX = 50
CHECKS_KEPT = 50
DIR_MAX = 200
FILE_MAX_BYTES = 256 * 1024


def seg(part: str) -> str:
    return quote(part, safe="")


def valid_name(v: object) -> bool:
    return isinstance(v, str) and NAME_RE.fullmatch(v) is not None and not v.endswith(".git")


def valid_path(p: object) -> bool:
    """A repository-relative path: segments, no empty, ``.`` or ``..`` segment, no leading
    slash, no control characters, bounded."""
    if not isinstance(p, str) or len(p) > PATH_MAX:
        return False
    if p == "":
        return True
    if p.startswith("/") or "\\" in p or not p.isprintable():
        return False
    parts = p.split("/")
    return all(s not in ("", ".", "..") and len(s) <= SEGMENT_MAX for s in parts)


def text(v: object, cap: int = 100_000) -> str:
    return v[:cap] if isinstance(v, str) else ""


def count(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def login(user: object) -> str:
    return text(user.get("login"), 100) if isinstance(user, dict) else ""


def sha(v: object) -> str:
    if not isinstance(v, str) or not SHA_RE.fullmatch(v):
        raise ConnectorError("bad_reply", "the forge answered without a commit sha")
    return v


def contents_path(repo_base: str, path: str) -> str:
    """``<repo_base>/contents[/<path>]``, each path segment encoded on its own."""
    if not valid_path(path):
        raise ConnectorError("refused", "not a repository path")
    base = f"{repo_base}/contents"
    return base if path == "" else base + "/" + "/".join(seg(s) for s in path.split("/"))


#: CI severity, worst first: the combined state is the worst of every context's latest state. A
#: state this build does not know counts as `pending` (never green).
STATUS_SEVERITY = ("error", "failure", "warning", "pending", "success")


def severity(state: str) -> int:
    return (
        STATUS_SEVERITY.index(state)
        if state in STATUS_SEVERITY
        else STATUS_SEVERITY.index("pending")
    )


def ci_result(commit: str, latest: dict[str, str], capped: bool) -> dict:
    """The combined state over ALL contexts (keyed by full text), worst first, and the
    ``CHECKS_KEPT`` worst checks. No context at all is the empty state; a capped walk can report a
    failure it saw but never claim ``success`` (``incomplete``)."""
    if latest:
        worst = min(latest.values(), key=severity)
        state = worst if worst in STATUS_SEVERITY else "pending"
    else:
        state = ""
    if capped and state not in ("error", "failure"):
        state = "incomplete"
    ordered = sorted(latest.items(), key=lambda kv: severity(kv[1]))  # stable: worst first
    return {
        "sha": commit,
        "state": state,
        "checks": [{"context": c[:200], "state": st} for c, st in ordered[:CHECKS_KEPT]],
        "checks_total": len(latest),
        "checks_total_capped": capped,
    }


def dir_entries(rows: object, path: str) -> list[dict]:
    """A contents-API directory listing as ``[{name, type, path, size}]``: files and dirs only."""
    if not isinstance(rows, list):
        raise ConnectorError("refused", "that path is not a directory")
    out = []
    for e in rows[:DIR_MAX]:
        if not isinstance(e, dict):
            continue
        name, kind = e.get("name"), e.get("type")
        child = f"{path}{name}" if path == "" or path.endswith("/") else f"{path}/{name}"
        if isinstance(name, str) and kind in ("dir", "file") and valid_path(child):
            out.append({"name": name, "type": kind, "path": child, "size": count(e.get("size"))})
    return out


def file_text(f: object, path: str, commit: str) -> dict:
    """A contents-API file reply as bounded UTF-8 text; binary and oversize files are refused."""
    if not isinstance(f, dict) or f.get("type") != "file":
        raise ConnectorError("refused", "that path is not a file")
    if count(f.get("size")) > FILE_MAX_BYTES or f.get("content") is None:
        raise ConnectorError("too_large", "that file is too large to read here")
    if f.get("encoding") != "base64" or not isinstance(f.get("content"), str):
        raise ConnectorError("bad_reply", "the forge answered the file in an unknown encoding")
    try:
        raw = base64.b64decode(f["content"], validate=False)
    except (binascii.Error, ValueError):
        raise ConnectorError("bad_reply", "the file's content could not be decoded") from None
    if len(raw) > FILE_MAX_BYTES:
        raise ConnectorError("too_large", "that file is too large to read here")
    if b"\x00" in raw:
        raise ConnectorError("binary", "that file is binary; only text is read")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError("binary", "that file is not UTF-8 text; only text is read") from None
    return {"path": path, "sha": commit, "size": len(raw), "text": decoded}
