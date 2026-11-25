"""Session clusters (#1997): each project's sessions grouped by topic.

One strict JSON document (``clusters.json``; ``AGENT_SESSIONS_CLUSTERS`` overrides the path):

.. code-block:: text

    clusters:    { tc_id: { project_id, ai_title, user_title|null, summary,
                            source: "ai"|"operator", created_at, updated_at } }
    membership:  { session_key: { cluster: tc_id, placed: bool } }
    sweeps:      { project_id: { fingerprint, at, error|null } }

The rules this module owns (``docs/invariants/session-clusters.md``):

* **Titles.** The shown title is ``user_title`` when set, else ``ai_title``. A cluster is *kept*
  iff ``user_title`` is set. The sweep writes ``ai_title`` and ``summary`` only.
* **Placed.** A membership with ``placed`` was put there by the operator (or a confirmed HQ
  card). The sweep never moves or removes it.
* **Protected.** A cluster the operator created, kept, or placed a session into is never
  dissolved by the sweep, whatever its size. Every other cluster needs two live members.
* **Strict and fail-soft.** A document that exists and cannot be read raises
  :class:`ClustersUnreadable`; it is never rewritten from such a read. Readers that only display
  treat that as "unknown", never as "no clusters".
* **A sweep commits against the document it planned from.** The model answers off the lock;
  :func:`commit_sweep` re-reads under the lock and discards the plan when the project's clusters
  or memberships changed meanwhile, so an operator's write always wins.

A session's group is its mission when a mission holds it (:func:`effective_cluster`): the stored
membership is then ignored, not deleted, so a placed session returns to its cluster afterwards.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import projects
from .atomicjson import atomic_write_json, json_write_lock

TITLE_MAX = 60
SUMMARY_MAX = 200
#: Clusters per project. An operator cluster beyond it is refused; AI ones beyond it are dropped.
MAX_CLUSTERS_PER_PROJECT = 12
#: Live members an unprotected cluster needs to exist.
MIN_MEMBERS = 2
#: Memberships kept before placed entries for sessions nothing lists any more are pruned.
MAX_MEMBERSHIPS = 5000
#: The serialized document's ceiling. A write that would exceed it is refused, never truncated.
MAX_BYTES = 2 * 1024 * 1024
ERROR_MAX = 300

_ID_RE = re.compile(r"^tc_[0-9a-f]{12}$")
_SOURCES = ("ai", "operator")


class ClustersUnreadable(Exception):
    """``clusters.json`` exists but could not be read or is not the document this module writes."""


class ClustersError(Exception):
    """A refused operation; ``status`` is the HTTP status the routes answer with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Live:
    """What the sweep and the routes know about one listed, non-archived session."""

    project_id: str
    #: A mission holds it: the mission is its group and a stored membership is ignored.
    mission: bool = False
    #: The sweep may group it (it has an AI title or summary and is not excluded from AI review).
    candidate: bool = False


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_CLUSTERS",
            str(Path.home() / ".config" / "agent-sessions" / "clusters.json"),
        )
    )


def valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def new_id() -> str:
    return "tc_" + secrets.token_hex(6)


def project_id_of(ref: Mapping) -> str:
    """The project a session row belongs to, as the list's project facet names it: the entity's
    id, or the synthetic Default project for a folder no project has adopted."""
    if ref.get("kind") == "project":
        return str(ref.get("id") or "")
    return projects.DEFAULT_PROJECT_ID


def clean_text(value: object, limit: int) -> str:
    """One line of plain text: control characters removed, whitespace collapsed, cut to ``limit``.
    Model output and operator input both pass through here before they are stored."""
    if not isinstance(value, str):
        return ""
    # Cut first: a hostile megabyte must not be walked character by character more than once.
    text = value[: limit * 8]
    text = "".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or ch.isprintable())
    return " ".join(text.split())[:limit].strip()


def operator_title(value: object) -> str:
    """An operator-typed title, or a 422. Over-long input is refused rather than cut: the
    operator should see exactly the title they saved."""
    if not isinstance(value, str):
        raise ClustersError("title must be text")
    title = clean_text(value, TITLE_MAX * 4)
    if not title:
        raise ClustersError("title must not be empty")
    if len(title) > TITLE_MAX:
        raise ClustersError(f"title must be at most {TITLE_MAX} characters")
    return title


def shown_title(cluster: Mapping) -> str:
    return cluster.get("user_title") or cluster.get("ai_title") or ""


def kept(cluster: Mapping) -> bool:
    return bool(cluster.get("user_title"))


# --- the document ------------------------------------------------------------------------------


def _empty() -> dict:
    return {"version": 1, "clusters": {}, "membership": {}, "sweeps": {}}


def _check(doc: object) -> dict:
    """The document, or :class:`ClustersUnreadable`. Every field a reader relies on is checked, so
    a hand-edited or foreign file is unknown rather than half-believed."""
    if not isinstance(doc, dict):
        raise ClustersUnreadable("not a mapping")
    out = _empty()
    for name in ("clusters", "membership", "sweeps"):
        block = doc.get(name, {})
        if not isinstance(block, dict):
            raise ClustersUnreadable(f"{name} is not a mapping")
        out[name] = block
    for cid, c in out["clusters"].items():
        if (
            not valid_id(cid)
            or not isinstance(c, dict)
            or not isinstance(c.get("project_id"), str)
            or not c["project_id"]
            or not isinstance(c.get("ai_title"), str)
            or not (c.get("user_title") is None or isinstance(c.get("user_title"), str))
            or not isinstance(c.get("summary"), str)
            or c.get("source") not in _SOURCES
        ):
            raise ClustersUnreadable("a cluster is malformed")
    for key, m in out["membership"].items():
        if (
            not isinstance(key, str)
            or not isinstance(m, dict)
            or not isinstance(m.get("cluster"), str)
            or not isinstance(m.get("placed"), bool)
        ):
            raise ClustersUnreadable("a membership is malformed")
    for pid, s in out["sweeps"].items():
        if not isinstance(pid, str) or not isinstance(s, dict):
            raise ClustersUnreadable("a sweep record is malformed")
    return out


def read_checked(path: Path | None = None) -> dict:
    """The stored document. A missing file is an empty one; one that exists and cannot be read or
    parsed raises :class:`ClustersUnreadable`. Lock-free: writes are atomic replaces."""
    path = path or _path()
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as e:
        raise ClustersUnreadable(type(e).__name__) from e
    return _check(doc)


class _Skip:
    """What a mutation returns to leave the document as it is."""

    def __init__(self, result: object = None) -> None:
        self.result = result


def _mutate(fn: Callable[[dict], object], path: Path | None = None):
    """Read-modify-write under the store's lock. An unreadable document raises before ``fn`` runs,
    so it is never replaced by a guess. ``fn`` returning a :class:`_Skip` writes nothing."""
    path = path or _path()
    with json_write_lock(path):
        doc = read_checked(path)
        result = fn(doc)
        if isinstance(result, _Skip):
            return result.result
        if len(json.dumps(doc, sort_keys=True).encode("utf-8")) > MAX_BYTES:
            raise ClustersError("the cluster store is full", status=507)
        atomic_write_json(path, doc)
        return result


def _cluster(doc: dict, cid: str) -> dict:
    c = doc["clusters"].get(cid) if valid_id(cid) else None
    if c is None:
        raise ClustersError("that cluster isn't here", status=404)
    return c


def _members(doc: dict, cid: str) -> list[str]:
    return [k for k, m in doc["membership"].items() if m["cluster"] == cid]


def _project_clusters(doc: dict, project_id: str) -> dict[str, dict]:
    return {cid: c for cid, c in doc["clusters"].items() if c["project_id"] == project_id}


def protected(doc: dict, cid: str) -> bool:
    """Whether the sweep may never dissolve this cluster: the operator created it, kept its title,
    or placed a session into it."""
    c = doc["clusters"][cid]
    return (
        c["source"] == "operator"
        or kept(c)
        or any(m["cluster"] == cid and m["placed"] for m in doc["membership"].values())
    )


def basis(doc: dict, project_id: str) -> str:
    """A digest of everything in one project's slice that an operator write can change: its
    clusters (titles, kept, source) and their memberships. A sweep plans against one basis and
    may only commit against the same one. Sweep records are not part of it."""
    own = _project_clusters(doc, project_id)
    payload = {
        "clusters": {
            cid: [c["ai_title"], c["user_title"], c["summary"], c["source"]]
            for cid, c in sorted(own.items())
        },
        "membership": sorted(
            [k, m["cluster"], m["placed"]]
            for k, m in doc["membership"].items()
            if m["cluster"] in own
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


# --- reading for display -----------------------------------------------------------------------


def effective_cluster(
    doc: dict, keys: Iterable[str], project_id: str, *, mission: bool
) -> str | None:
    """The cluster id a session is shown in, or ``None``. THE one place mission precedence and
    the project check live, for the list, the map and the agent tool alike.

    ``keys`` is the session's logical key and its physical one (a membership may be recorded
    under either). A mission-held session has no cluster: the mission is its group. A membership
    pointing at another project's cluster (the session moved; the next sweep removes it) or at a
    cluster that no longer exists is ignored."""
    if mission:
        return None
    for key in keys:
        m = doc["membership"].get(key)
        if m is None:
            continue
        c = doc["clusters"].get(m["cluster"])
        if c is not None and c["project_id"] == project_id:
            return m["cluster"]
        return None
    return None


def cluster_ref(doc: dict, cid: str) -> dict:
    c = doc["clusters"][cid]
    return {"id": cid, "title": shown_title(c), "kept": kept(c)}


def describe(doc: dict, cid: str, count: int) -> dict:
    """One cluster as ``GET /api/clusters`` lists it."""
    c = doc["clusters"][cid]
    return {
        "id": cid,
        "project_id": c["project_id"],
        "title": shown_title(c),
        "ai_title": c["ai_title"],
        "kept": kept(c),
        "source": c["source"],
        "summary": c["summary"],
        "count": count,
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }


def sweep_record(doc: dict, project_id: str) -> dict | None:
    s = doc["sweeps"].get(project_id)
    if s is None:
        return None
    at = s.get("at")
    err = s.get("error")
    return {
        "at": at if isinstance(at, int | float) else None,
        "error": err if isinstance(err, str) and err else None,
    }


# --- operator writes ---------------------------------------------------------------------------


def create(project_id: str, title: object, *, now: float | None = None) -> dict:
    """An operator cluster: kept title, survives empty."""
    name = operator_title(title)
    if not isinstance(project_id, str) or not project_id.strip():
        raise ClustersError("project_id is required")
    now = time.time() if now is None else now

    def fn(doc: dict) -> dict:
        if len(_project_clusters(doc, project_id)) >= MAX_CLUSTERS_PER_PROJECT:
            raise ClustersError(
                f"a project has at most {MAX_CLUSTERS_PER_PROJECT} clusters", status=409
            )
        cid = new_id()
        doc["clusters"][cid] = {
            "project_id": project_id,
            "ai_title": name,
            "user_title": name,
            "summary": "",
            "source": "operator",
            "created_at": now,
            "updated_at": now,
        }
        return describe(doc, cid, 0)

    return _mutate(fn)


def rename(cid: str, title: object, *, now: float | None = None) -> dict:
    """Set the operator's title. The cluster is kept from here on."""
    name = operator_title(title)
    now = time.time() if now is None else now

    def fn(doc: dict) -> dict:
        c = _cluster(doc, cid)
        c["user_title"] = name
        c["updated_at"] = now
        return cluster_ref(doc, cid)

    return _mutate(fn)


def set_keep(cid: str, keep: bool, *, now: float | None = None) -> dict:
    """``keep=True`` keeps the title shown now; ``False`` hands it back to HQ (Let HQ rename)."""
    now = time.time() if now is None else now

    def fn(doc: dict) -> dict:
        c = _cluster(doc, cid)
        c["user_title"] = shown_title(c) if keep else None
        c["updated_at"] = now
        return cluster_ref(doc, cid)

    return _mutate(fn)


def merge(cid: str, into: str, *, now: float | None = None) -> dict:
    """Move ``cid``'s members into ``into`` (same project) and remove ``cid``. Placed stays
    placed. A kept title on ``into`` wins; otherwise ``cid``'s kept title carries over."""
    now = time.time() if now is None else now

    def fn(doc: dict) -> dict:
        src = _cluster(doc, cid)
        dst = _cluster(doc, into)
        if cid == into:
            raise ClustersError("a cluster cannot be merged into itself")
        if src["project_id"] != dst["project_id"]:
            raise ClustersError("clusters of different projects cannot be merged", status=409)
        for m in doc["membership"].values():
            if m["cluster"] == cid:
                m["cluster"] = into
        if not kept(dst) and kept(src):
            dst["user_title"] = src["user_title"]
        if src["source"] == "operator":
            dst["source"] = "operator"
        dst["updated_at"] = now
        del doc["clusters"][cid]
        return cluster_ref(doc, into)

    return _mutate(fn)


def dissolve(cid: str) -> None:
    """Remove the cluster and free its members, unplaced. No tombstone: a later sweep may group
    them again."""

    def fn(doc: dict) -> None:
        _cluster(doc, cid)
        for key in _members(doc, cid):
            del doc["membership"][key]
        del doc["clusters"][cid]

    _mutate(fn)


def place(session_key: str, cid: str, session_project_id: str) -> dict:
    """Put a session into a cluster of ITS project, to stay."""

    def fn(doc: dict) -> dict:
        c = _cluster(doc, cid)
        if c["project_id"] != session_project_id:
            raise ClustersError("that cluster belongs to another project", status=409)
        doc["membership"][session_key] = {"cluster": cid, "placed": True}
        return cluster_ref(doc, cid)

    return _mutate(fn)


def unplace(keys: Iterable[str]) -> None:
    """Back to HQ's choice: the session stays where it is, but the sweep may move it again."""
    keys = list(keys)

    def fn(doc: dict):
        changed = False
        for key in keys:
            m = doc["membership"].get(key)
            if m is not None and m["placed"]:
                m["placed"] = False
                changed = True
        return None if changed else _Skip()

    _mutate(fn)


# --- the sweep's writes ------------------------------------------------------------------------


def _is_live(live: Mapping[str, Live], key: str, project_id: str) -> bool:
    fact = live.get(key)
    return fact is not None and fact.project_id == project_id and not fact.mission


def _reconcile(doc: dict, live: Mapping[str, Live], archived: frozenset[str] | set[str]) -> bool:
    """Drop memberships that no longer describe anything, on positive evidence only.

    * the session is archived, or it now belongs to another project (placed or not: a placement
      is into one project's cluster);
    * an unplaced membership whose session nothing lists any more (the sweep would regroup it);
    * a membership of a cluster that no longer exists.

    A PLACED membership whose session is merely missing from this scan is kept: one unreadable
    engine store must not undo the operator's act. Those are pruned only past
    :data:`MAX_MEMBERSHIPS`."""
    changed = False
    for key in list(doc["membership"]):
        m = doc["membership"][key]
        c = doc["clusters"].get(m["cluster"])
        fact = live.get(key)
        gone = (
            c is None
            or key in archived
            or (fact is not None and fact.project_id != c["project_id"])
            or (fact is None and not m["placed"])
        )
        if gone:
            del doc["membership"][key]
            changed = True
    if len(doc["membership"]) > MAX_MEMBERSHIPS:
        for key in [k for k in doc["membership"] if k not in live]:
            del doc["membership"][key]
            changed = True
    return changed


def _dissolve_small(doc: dict, project_id: str, live: Mapping[str, Live]) -> bool:
    """Remove every unprotected cluster of the project with fewer than two live members."""
    changed = False
    for cid in list(_project_clusters(doc, project_id)):
        if protected(doc, cid):
            continue
        members = _members(doc, cid)
        if sum(1 for k in members if _is_live(live, k, project_id)) < MIN_MEMBERS:
            for k in members:
                del doc["membership"][k]
            del doc["clusters"][cid]
            changed = True
    return changed


def reconcile(live: Mapping[str, Live], archived: Iterable[str] = ()) -> bool:
    """One housekeeping pass with no model call: stale memberships go, and clusters the sweep owns
    that fell below two live members are dissolved. Returns whether anything was written."""
    gone = frozenset(archived)

    def fn(doc: dict):
        changed = _reconcile(doc, live, gone)
        for pid in {c["project_id"] for c in doc["clusters"].values()}:
            changed = _dissolve_small(doc, pid, live) or changed
        return True if changed else _Skip(False)

    return bool(_mutate(fn))


def record_sweep(
    project_id: str, *, fingerprint: str, error: str | None, now: float | None = None
) -> None:
    """Record a sweep that wrote no clusters: an error, or an unchanged result."""
    now = time.time() if now is None else now

    def fn(doc: dict) -> None:
        doc["sweeps"][project_id] = {
            "fingerprint": fingerprint,
            "at": now,
            "error": clean_text(error, ERROR_MAX) or None if error else None,
        }

    _mutate(fn)


def forget_sweeps(project_ids: Iterable[str] | None = None) -> None:
    """Clear stored fingerprints so the next scheduled pass sweeps again (all projects when
    ``project_ids`` is ``None``). The last time and error stay visible."""
    wanted = None if project_ids is None else set(project_ids)

    def fn(doc: dict):
        changed = False
        for pid, s in doc["sweeps"].items():
            if (wanted is None or pid in wanted) and s.get("fingerprint"):
                s["fingerprint"] = ""
                changed = True
        return None if changed else _Skip()

    _mutate(fn)


def commit_sweep(
    project_id: str,
    *,
    planned_basis: str,
    plan: Mapping,
    input_keys: Iterable[str],
    live: Mapping[str, Live],
    archived: Iterable[str] = (),
    fingerprint: Callable[[dict], str],
    now: float | None = None,
) -> str:
    """Apply a validated plan for one project, or refuse it as stale.

    ``plan`` is ``{"existing": {tc_id: {"title", "summary", "members": [key…]}},
    "new": [{"title", "summary", "members": [key…]}]}`` — session KEYS, already mapped back from
    the aliases the model saw. ``input_keys`` are the sessions the model was asked about; ``live``
    is read again AFTER the model answered.

    Under the lock the document is re-read. If the project's :func:`basis` is not the one the
    plan was made from — the operator renamed, placed, merged, dissolved or created meanwhile, or
    another sweep committed — nothing is written and ``"stale"`` is returned. Otherwise:

    * a placed membership is never touched;
    * an unplaced membership of a session the model was asked about is replaced by the plan's
      answer; one it was not asked about (beyond the input cap) stays;
    * a plan member that is no longer a live candidate of this project is dropped;
    * a cluster the plan does not name loses its unplaced, asked-about members;
    * unprotected clusters under two live members go, then unprotected clusters beyond the
      project's cap, newest plan entries first.

    ``fingerprint(doc)`` is called on the result so the stored fingerprint describes what was
    written. Returns ``"committed"`` or ``"stale"``."""
    now = time.time() if now is None else now
    asked = set(input_keys)
    gone = frozenset(archived)

    def eligible(key: str) -> bool:
        fact = live.get(key)
        return (
            key in asked
            and fact is not None
            and fact.project_id == project_id
            and fact.candidate
            and not fact.mission
        )

    def fn(doc: dict):
        if basis(doc, project_id) != planned_basis:
            return _Skip("stale")
        _reconcile(doc, live, gone)
        own = _project_clusters(doc, project_id)
        membership = doc["membership"]
        # The model re-decides every unplaced session it was asked about.
        for key in [
            k
            for k, m in membership.items()
            if m["cluster"] in own and not m["placed"] and k in asked
        ]:
            del membership[key]
        taken = {k for k, m in membership.items() if m["placed"]}

        def assign(cid: str, members: Iterable[str]) -> None:
            for key in members:
                if key in taken or not eligible(key):
                    continue
                # A leftover unplaced membership elsewhere (never asked about) is not overwritten.
                if key in membership:
                    continue
                membership[key] = {"cluster": cid, "placed": False}
                taken.add(key)

        for cid, entry in plan.get("existing", {}).items():
            c = own.get(cid)
            if c is None:
                continue
            title = clean_text(entry.get("title"), TITLE_MAX)
            if title:
                c["ai_title"] = title
            c["summary"] = clean_text(entry.get("summary"), SUMMARY_MAX)
            c["updated_at"] = now
            assign(cid, entry.get("members", ()))
        created: list[str] = []
        for entry in plan.get("new", ()):
            title = clean_text(entry.get("title"), TITLE_MAX)
            if not title:
                continue
            cid = new_id()
            doc["clusters"][cid] = {
                "project_id": project_id,
                "ai_title": title,
                "user_title": None,
                "summary": clean_text(entry.get("summary"), SUMMARY_MAX),
                "source": "ai",
                "created_at": now,
                "updated_at": now,
            }
            created.append(cid)
            assign(cid, entry.get("members", ()))
        _dissolve_small(doc, project_id, live)
        # The cap: protected clusters and the ones that already existed stay; the newest AI
        # clusters go first.
        over = len(_project_clusters(doc, project_id)) - MAX_CLUSTERS_PER_PROJECT
        for cid in reversed(created):
            if over <= 0:
                break
            if cid in doc["clusters"] and not protected(doc, cid):
                for k in _members(doc, cid):
                    del membership[k]
                del doc["clusters"][cid]
                over -= 1
        doc["sweeps"][project_id] = {"fingerprint": fingerprint(doc), "at": now, "error": None}
        return "committed"

    return _mutate(fn)
