"""Shared artifact repositories (#2347 P1): named collections of artifact revisions, hosted here.

A **repository** is a named collection one BattleLab hosts. Each **item** in it is a sequence of
immutable **item revisions**, and each item revision is a manifest ``path → {sha256, size, type}``
exactly like an artifact revision's. The rules (pinned in ``docs/invariants/shared-repos.md``):

* **Rows in the artifact store, bytes in its blob store.** The tables live in ``artifacts.db``
  (`artifact_store.DDL`, schema 2) and every write goes through `artifact_store._tx`, under the
  same process write lock as a publish and garbage collection.
* **No byte copy.** Adding an artifact revision to a repository copies its MANIFEST: the item
  revision names the same blobs. A blob stays on disk while any artifact revision or repository
  item revision names it (`artifact_store._manifests`), and the host-wide `TOTAL_BYTES_MAX`
  counts every blob once, whoever holds it.
* **A quota per repository** (`QUOTA_DEFAULT`, 2 GB) counts the distinct blobs its revisions name.
  It is checked inside the transaction that adds a revision, after the old ones are pruned: over
  quota refuses ``quota`` and the transaction rolls back, so nothing is stored.
* **An agent publishes through the one judge.** ``artifact_publish(repository=…)`` is
  `artifact_store.publish` (the boundary, caps, hidden and key files, the secret scan), which
  calls `add_in_tx` inside its own commit. The agent's artifact and its item revision are one
  transaction.
* **Removing an item or deleting a repository releases its references**; garbage collection then
  removes every blob nothing names any more, at once, as an artifact delete does.

Peers (read, contribute) are later phases; nothing here talks to another instance.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time

from . import artifact_store, mission_shelf
from .artifact_store import ArtifactRefused

#: A repository's default quota: the distinct bytes its revisions may name.
QUOTA_DEFAULT = 2 * 2**30
#: A repository name is a short lowercase slug (it names a folder in a later phase's fetch):
#: letters, digits, ``.``, ``_`` and ``-``, starting and ending with a letter or digit.
NAME_MAX = artifact_store.REPO_REF_MAX
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
DESCRIPTION_MAX = artifact_store.WHY_MAX
REVISIONS_MAX = artifact_store.REVISIONS_MAX
PAGE_MAX = artifact_store.PAGE_MAX
OPERATOR = "operator"

#: What the operator's page shows for each refusal the repository routes can answer.
WORDS: dict[str, str] = {
    "bad_request": "That request is not valid.",
    "repository_name": (
        "A repository name is 1–64 lowercase letters, digits, dots, dashes or underscores,"
        " starting and ending with a letter or digit."
    ),
    "repository_exists": "A repository with that name already exists.",
    "too_long": "The description is at most 300 characters.",
    "unknown_repository": "There is no such repository.",
    "unknown_item": "There is no such item in this repository.",
    "unknown_artifact": "There is no such artifact, or that revision was pruned.",
    "kind_mismatch": "A new revision keeps its item's kind: a site stays a site, a file a file.",
    "quota": "That would put the repository over its quota. Nothing was added.",
    "store_full": "BattleLab's artifact store is full.",
}


def _refuse(code: str, status: int = 422) -> ArtifactRefused:
    return ArtifactRefused(code, status=status)


# ---------------------------------------------------------------- validation


def clean_name(value: object) -> str:
    if not isinstance(value, str):
        raise _refuse("repository_name")
    name = value.strip()
    if not NAME_RE.fullmatch(name) or artifact_store.ID_RE.fullmatch(name):
        # Never an id's shape either: `lookup` takes a name or an id.
        raise _refuse("repository_name")
    return name


def clean_description(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _refuse("bad_request")
    text = mission_shelf.clean_line(value, DESCRIPTION_MAX)
    if len(text) > DESCRIPTION_MAX:
        raise _refuse("too_long")
    return text


def _repo_id(value: object) -> str:
    if not isinstance(value, str) or not artifact_store.ID_RE.fullmatch(value):
        raise _refuse("unknown_repository", 404)
    return value


def _item_id(value: object) -> str:
    if not isinstance(value, str) or not artifact_store.ID_RE.fullmatch(value):
        raise _refuse("unknown_item", 404)
    return value


# ---------------------------------------------------------------- reading


def _repo_bytes(con: sqlite3.Connection, repo_id: str) -> int:
    """The distinct bytes one repository's revisions name (its quota's measure)."""
    seen: dict[str, int] = {}
    for r in con.execute(
        "SELECT r.manifest FROM repo_item_revisions r JOIN repo_items i ON i.id = r.item_id"
        " WHERE i.repo_id=?",
        (repo_id,),
    ):
        for meta in json.loads(r["manifest"]).values():
            seen[meta["sha256"]] = meta["size"]
    return sum(seen.values())


def _view(con: sqlite3.Connection, row: sqlite3.Row) -> dict:
    count = con.execute("SELECT count(*) FROM repo_items WHERE repo_id=?", (row["id"],)).fetchone()
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "quota_bytes": row["quota_bytes"],
        "bytes": _repo_bytes(con, row["id"]),
        "items": count[0],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_repos() -> list[dict]:
    """Every repository hosted here, by name. A read never creates the store."""
    if artifact_store._absent():
        return []
    with artifact_store._con() as con:
        rows = con.execute("SELECT * FROM repositories ORDER BY name").fetchall()
        return [_view(con, r) for r in rows]


def get(repo_id: str) -> dict | None:
    rid = _repo_id(repo_id)
    if artifact_store._absent():
        return None
    with artifact_store._con() as con:
        row = con.execute("SELECT * FROM repositories WHERE id=?", (rid,)).fetchone()
        return None if row is None else _view(con, row)


def lookup(ref: str) -> dict:
    """The repository an agent named, by its id or its name. Raises ``unknown_repository``."""
    if artifact_store._absent():
        raise _refuse("unknown_repository", 404)
    key = "id" if artifact_store.ID_RE.fullmatch(ref) else "name"
    with artifact_store._con() as con:
        row = con.execute(
            f"SELECT * FROM repositories WHERE {key}=?",  # noqa: S608 — one of two fixed columns
            (ref if key == "id" else ref.lower(),),
        ).fetchone()
        if row is None:
            raise _refuse("unknown_repository", 404)
        return _view(con, row)


def _added_by(value: str) -> dict:
    if value == OPERATOR:
        return {"kind": "operator"}
    return {"kind": "session", "session_key": value}


def item_view(row) -> dict:
    """One item as the operator's page sees it, with its latest revision's size and entry."""
    return {
        "id": row["id"],
        "repository_id": row["repo_id"],
        "title": row["title"],
        "why": row["why"],
        "kind": row["kind"],
        "revision": row["latest"],
        "added_by": _added_by(row["added_by"]),
        "source_artifact": row["source_artifact"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "files": row["files"],
        "bytes": row["bytes"],
        "entry": row["entry"],
        # The operator's link to the latest revision, served sandboxed like an artifact.
        "url": f"{artifact_store.base_url()}/artifacts/i/{row['id']}/",
    }


_ITEM_SELECT = (
    "SELECT i.*, r.files, r.bytes, r.entry FROM repo_items i LEFT JOIN repo_item_revisions r"
    " ON r.item_id = i.id AND r.n = i.latest"
)


def items(repo_id: str, *, cursor: str | None = None, limit: int = PAGE_MAX) -> dict:
    """One page of a repository's items, newest first: ``{items, total, next_cursor}``."""
    rid = _repo_id(repo_id)
    offset = 0
    if cursor:
        try:
            offset = max(0, int(cursor))
        except ValueError:
            raise _refuse("bad_request") from None
    limit = max(1, min(PAGE_MAX, int(limit)))
    if artifact_store._absent():
        raise _refuse("unknown_repository", 404)
    with artifact_store._con() as con:
        if con.execute("SELECT 1 FROM repositories WHERE id=?", (rid,)).fetchone() is None:
            raise _refuse("unknown_repository", 404)
        total = con.execute("SELECT count(*) FROM repo_items WHERE repo_id=?", (rid,)).fetchone()[0]
        rows = con.execute(
            f"{_ITEM_SELECT} WHERE i.repo_id=? ORDER BY i.updated_at DESC, i.id LIMIT ? OFFSET ?",
            (rid, limit, offset),
        ).fetchall()
    nxt = offset + len(rows)
    return {
        "items": [item_view(r) for r in rows],
        "total": total,
        "next_cursor": str(nxt) if nxt < total else None,
    }


# ---------------------------------------------------------------- writing


def create(name: object, description: object = None) -> dict:
    nm, desc = clean_name(name), clean_description(description)
    now = time.time()
    with artifact_store._tx() as con:
        if con.execute("SELECT 1 FROM repositories WHERE name=?", (nm,)).fetchone():
            raise _refuse("repository_exists", 409)
        rid = artifact_store.new_id()
        con.execute(
            "INSERT INTO repositories (id, name, description, quota_bytes, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (rid, nm, desc, QUOTA_DEFAULT, now, now),
        )
        return _view(con, con.execute("SELECT * FROM repositories WHERE id=?", (rid,)).fetchone())


def update(repo_id: str, *, name: object = None, description: object = None) -> dict:
    """Rename a repository or change its description (either may be left out)."""
    rid = _repo_id(repo_id)
    nm = None if name is None else clean_name(name)
    desc = None if description is None else clean_description(description)
    with artifact_store._tx() as con:
        row = con.execute("SELECT * FROM repositories WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise _refuse("unknown_repository", 404)
        if nm is not None and nm != row["name"]:
            if con.execute("SELECT 1 FROM repositories WHERE name=?", (nm,)).fetchone():
                raise _refuse("repository_exists", 409)
        con.execute(
            "UPDATE repositories SET name=?, description=?, updated_at=? WHERE id=?",
            (nm or row["name"], row["description"] if desc is None else desc, time.time(), rid),
        )
        return _view(con, con.execute("SELECT * FROM repositories WHERE id=?", (rid,)).fetchone())


def delete(repo_id: str) -> bool:
    """Delete a repository, every item and revision in it; then collect the blobs nothing names."""
    rid = _repo_id(repo_id)
    if artifact_store._absent():
        return False
    with artifact_store._tx() as con:
        gone = con.execute("DELETE FROM repositories WHERE id=?", (rid,)).rowcount > 0
    if gone:
        artifact_store._collect_garbage()
    return gone


def remove_item(repo_id: str, item_id: str) -> bool:
    """Remove one item and every revision of it; then collect the blobs nothing names."""
    rid, iid = _repo_id(repo_id), _item_id(item_id)
    if artifact_store._absent():
        return False
    with artifact_store._tx() as con:
        gone = (
            con.execute("DELETE FROM repo_items WHERE id=? AND repo_id=?", (iid, rid)).rowcount > 0
        )
    if gone:
        artifact_store._collect_garbage()
    return gone


def add_in_tx(
    con: sqlite3.Connection,
    repo_id: str,
    *,
    source: tuple[str, int],
    title: str,
    why: str,
    kind: str,
    manifest: dict[str, dict],
    entry: str | None,
    added_by: str,
    now: float,
    item_id: str | None = None,
    new_item: bool = False,
) -> dict:
    """Add one revision to a repository INSIDE the caller's write transaction: a new revision of
    ``item_id`` when given; with ``new_item`` always a new item; else a new revision of the item
    ``source``'s artifact already feeds in this repository (an agent publishing that artifact
    again), or a new item when there is none. The manifest is copied; the blobs
    are not. Over the quota (measured after pruning) raises ``quota``, which rolls the caller's
    transaction back. Returns ``{"placed": {...}, "pruned": bool}``."""
    repo = con.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone()
    if repo is None:
        raise _refuse("unknown_repository", 404)
    aid, rev = source
    if item_id is not None:
        item = con.execute(
            "SELECT * FROM repo_items WHERE id=? AND repo_id=?", (item_id, repo_id)
        ).fetchone()
        if item is None:
            raise _refuse("unknown_item", 404)
    elif new_item:
        item = None
    else:
        item = con.execute(
            "SELECT * FROM repo_items WHERE repo_id=? AND source_artifact=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (repo_id, aid),
        ).fetchone()
    if item is not None and item["kind"] != kind:
        raise _refuse("kind_mismatch")
    files = len(manifest)
    total = sum(m["size"] for m in manifest.values())
    if item is None:
        iid, n = artifact_store.new_id(), 1
        con.execute(
            "INSERT INTO repo_items (id, repo_id, title, why, kind, added_by, source_artifact,"
            " created_at, updated_at, latest) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, repo_id, title, why, kind, added_by, aid, now, now, n),
        )
    else:
        iid, n = item["id"], item["latest"] + 1
        con.execute(
            "UPDATE repo_items SET title=?, why=?, source_artifact=?, updated_at=?, latest=?"
            " WHERE id=?",
            (title, why or item["why"], aid, now, n, iid),
        )
    con.execute(
        "INSERT INTO repo_item_revisions (item_id, n, created_at, files, bytes, entry, manifest,"
        " added_by, source_artifact, source_rev) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            iid,
            n,
            now,
            files,
            total,
            entry,
            json.dumps(manifest, sort_keys=True),
            added_by,
            aid,
            rev,
        ),
    )
    pruned = (
        con.execute(
            "DELETE FROM repo_item_revisions WHERE item_id=? AND n <= ?", (iid, n - REVISIONS_MAX)
        ).rowcount
        > 0
    )
    if _repo_bytes(con, repo_id) > repo["quota_bytes"]:
        raise _refuse("quota")
    con.execute("UPDATE repositories SET updated_at=? WHERE id=?", (now, repo_id))
    return {
        "placed": {
            "id": repo_id,
            "name": repo["name"],
            "item_id": iid,
            "revision": n,
            "new_item": item is None,
        },
        "pruned": pruned,
    }


def add_artifact(
    repo_id: str,
    artifact_id: object,
    *,
    revision: object = None,
    item_id: object = None,
    added_by: str = OPERATOR,
) -> dict:
    """The operator adds an EXISTING artifact revision (the latest, or ``revision``) to a
    repository: a new item, or a new revision of ``item_id``. Its manifest is copied, never its
    bytes. Returns the item."""
    rid = _repo_id(repo_id)
    aid = artifact_store.validate_id(artifact_id)
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
    ):
        raise _refuse("bad_request")
    iid = None if item_id is None else _item_id(item_id)
    with artifact_store._tx() as con:
        art = con.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        if art is None:
            raise _refuse("unknown_artifact", 404)
        n = art["latest"] if revision is None else revision
        rev = con.execute(
            "SELECT * FROM artifact_revisions WHERE artifact_id=? AND n=?", (aid, n)
        ).fetchone()
        if rev is None:
            raise _refuse("unknown_artifact", 404)
        # The operator's add is always a NEW item unless an item is named: adding the same
        # artifact twice is two items, as the operator asked.
        placed = add_in_tx(
            con,
            rid,
            source=(aid, n),
            title=art["title"],
            why=art["why"],
            kind=art["kind"],
            manifest=json.loads(rev["manifest"]),
            entry=rev["entry"],
            added_by=added_by,
            now=time.time(),
            item_id=iid,
            new_item=iid is None,
        )
        row = con.execute(f"{_ITEM_SELECT} WHERE i.id=?", (placed["placed"]["item_id"],)).fetchone()
        out = item_view(row)
    if placed["pruned"]:
        artifact_store._collect_garbage()
    return out


# ---------------------------------------------------------------- serving


def _item_revision(iid: str, revision: int | None):
    if not artifact_store.ID_RE.fullmatch(iid or "") or artifact_store._absent():
        raise artifact_store.NotServed()
    with artifact_store._con() as con:
        item = con.execute("SELECT * FROM repo_items WHERE id=?", (iid,)).fetchone()
        if item is None:
            raise artifact_store.NotServed()
        n = item["latest"] if revision is None else revision
        rev = con.execute(
            "SELECT * FROM repo_item_revisions WHERE item_id=? AND n=?", (iid, n)
        ).fetchone()
    if rev is None:
        raise artifact_store.NotServed()
    return item, rev


def resolve(iid: str, rel: str, *, revision: int | None = None) -> artifact_store.Served:
    """The file ``rel`` names in an item's revision (latest by default), by manifest lookup only,
    exactly as an artifact's. Raises `artifact_store.NotServed` / `artifact_store.Redirect`."""
    _item, rev = _item_revision(iid, revision)
    return artifact_store.resolve_in(rev["entry"], json.loads(rev["manifest"]), rel)


def listing(iid: str, *, revision: int | None = None) -> list[dict]:
    _item, rev = _item_revision(iid, revision)
    return [{"path": p, "size": m["size"]} for p, m in sorted(json.loads(rev["manifest"]).items())]


def item_meta(iid: str, revision: int | None = None) -> dict | None:
    """What the operator's viewer page shows of an item revision: title, kind and time."""
    try:
        item, rev = _item_revision(iid, revision)
    except artifact_store.NotServed:
        return None
    return {"title": item["title"], "kind": item["kind"], "updated_at": rev["created_at"]}
