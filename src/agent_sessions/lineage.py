"""Session lineage (#2325): which session started which.

One strict JSON document (``lineage.json``; ``AGENT_SESSIONS_LINEAGE`` overrides the path):

.. code-block:: text

    links:        { child_key: { parent: key, source, at } }
    bindings:     { scope_unit: { key, at } }
    attribution:  { session_key: { state, tries, next_at } }

The rules this module owns (``docs/invariants/session-lineage.md``):

* **Proven links only.** A link is written by the code path that started the child (``launch``),
  by the process attributor from exact evidence (``process``), by an engine's own parent link
  (``native``) or by the operator (``operator``). Nothing is linked on a guess.
* **Precedence.** ``operator`` > ``launch`` > ``native`` > ``process``. A lower source never
  replaces a higher one. Self-links and cycles are refused on write.
* **Strict and fail-soft.** A document that exists and cannot be read raises
  :class:`LineageUnreadable` and is never rewritten from such a read. Display readers treat that
  as "lineage unknown": every session renders as a main session, never "no sessions".
* **Exact scope identity.** ``scopedspawn.unit_name`` truncates the session id, so a scope name can
  never be turned back into a key. The LAUNCH path records ``bindings[unit] = full key``; the
  attributor looks a unit up by its exact name only.
* **Main session or child is decided against a row set** (:func:`project`): a row is a child only
  when its recorded parent is in the same set. A child whose parent is archived, gone or out of
  scope is an orphan and renders as a main session.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock

SOURCES = ("process", "native", "launch", "operator")
_RANK = {s: i for i, s in enumerate(SOURCES)}

#: Links kept before the oldest are dropped. A link is a few dozen bytes.
MAX_LINKS = 20_000
#: Scope bindings kept; a scope lives as long as its master, so old ones are dead weight.
MAX_BINDINGS = 4_000
#: Attribution records kept.
MAX_ATTRIBUTION = 20_000
#: The serialized document's ceiling. A write that would exceed it is refused, never truncated.
MAX_BYTES = 8 * 1024 * 1024

#: Attribution outcomes. ``linked`` and ``none`` are final; ``unknown`` is retried.
STATES = ("linked", "none", "unknown", "none-final", "conflict", "unavailable")
FINAL_STATES = frozenset({"linked", "none", "none-final", "conflict", "unavailable"})
#: Seconds after the n-th ``unknown`` before the next try. After the last, ``none-final``.
RETRY_BACKOFF_S = (10.0, 30.0, 120.0, 600.0)
MAX_TRIES = len(RETRY_BACKOFF_S) + 1

#: Row state, worst first: what a rolled-up LED shows.
ROLLUP_ORDER = ("attention", "working", "draft", "idle")


class LineageUnreadable(Exception):
    """``lineage.json`` exists but could not be read or is not the document this module writes."""


class LineageError(Exception):
    """A refused write; ``status`` is the HTTP status a route would answer with."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_LINEAGE",
            str(Path.home() / ".config" / "agent-sessions" / "lineage.json"),
        )
    )


def _empty() -> dict:
    return {"version": 1, "links": {}, "bindings": {}, "attribution": {}}


def _num(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _check(doc: object) -> dict:
    if not isinstance(doc, dict):
        raise LineageUnreadable("not a mapping")
    out = _empty()
    for name in ("links", "bindings", "attribution"):
        block = doc.get(name, {})
        if not isinstance(block, dict):
            raise LineageUnreadable(f"{name} is not a mapping")
        out[name] = block
    for child, link in out["links"].items():
        if (
            not isinstance(child, str)
            or not isinstance(link, dict)
            or not isinstance(link.get("parent"), str)
            or link.get("source") not in SOURCES
            or not _num(link.get("at"))
        ):
            raise LineageUnreadable("a link is malformed")
    for unit, b in out["bindings"].items():
        if (
            not isinstance(unit, str)
            or not isinstance(b, dict)
            or not isinstance(b.get("key"), str)
        ):
            raise LineageUnreadable("a binding is malformed")
    for key, a in out["attribution"].items():
        if (
            not isinstance(key, str)
            or not isinstance(a, dict)
            or a.get("state") not in STATES
            or not isinstance(a.get("tries"), int)
            or not _num(a.get("next_at"))
        ):
            raise LineageUnreadable("an attribution record is malformed")
    return out


def read_checked(path: Path | None = None) -> dict:
    """The stored document. Missing = empty; unreadable = :class:`LineageUnreadable`."""
    path = path or _path()
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return _empty()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as e:
        raise LineageUnreadable(type(e).__name__) from e
    return _check(doc)


class _Skip:
    def __init__(self, result: object = None) -> None:
        self.result = result


def _trim(block: dict, cap: int, at: Callable[[dict], float]) -> None:
    if len(block) <= cap:
        return
    for k in sorted(block, key=lambda k: at(block[k]))[: len(block) - cap]:
        del block[k]


def _mutate(fn: Callable[[dict], object], path: Path | None = None):
    """Read-modify-write under the store's lock. An unreadable document raises before ``fn``."""
    path = path or _path()
    with json_write_lock(path):
        doc = read_checked(path)
        result = fn(doc)
        if isinstance(result, _Skip):
            return result.result
        _trim(doc["links"], MAX_LINKS, lambda v: v["at"])
        _trim(doc["bindings"], MAX_BINDINGS, lambda v: v.get("at", 0) if _num(v.get("at")) else 0)
        _trim(doc["attribution"], MAX_ATTRIBUTION, lambda v: v["next_at"])
        if len(json.dumps(doc, sort_keys=True).encode("utf-8")) > MAX_BYTES:
            raise LineageError("the lineage store is full", status=507)
        atomic_write_json(path, doc)
        _invalidate()
        return result


# --- links -------------------------------------------------------------------------------------


def _ancestors(links: Mapping, key: str) -> Iterable[str]:
    seen: set[str] = set()
    cur = links.get(key)
    while cur is not None:
        p = cur["parent"]
        if p in seen:
            return
        seen.add(p)
        yield p
        cur = links.get(p)


def link(child: str, parent: str, source: str, *, now: float | None = None) -> bool:
    """Record ``child`` → ``parent``. ``False`` when an equal-or-higher source already holds a
    link for ``child`` (that link stands). Raises :class:`LineageError` on a self-link or a
    cycle."""
    if source not in SOURCES:
        raise LineageError(f"unknown lineage source {source!r}")
    if not child or not parent or not isinstance(child, str) or not isinstance(parent, str):
        raise LineageError("both keys are required")
    if child == parent:
        raise LineageError("a session cannot be its own parent")
    ts = time.time() if now is None else now

    def apply(doc: dict):
        links = doc["links"]
        held = links.get(child)
        if held is not None:
            if held["parent"] == parent and held["source"] == source:
                return _Skip(True)
            if _RANK[held["source"]] > _RANK[source]:
                return _Skip(False)
        if child in set(_ancestors(links, parent)) or parent == child:
            raise LineageError("that link would make a cycle")
        links[child] = {"parent": parent, "source": source, "at": ts}
        return True

    return _mutate(apply)


def unlink(child: str) -> None:
    """Drop ``child``'s link (the operator's "make main session" in a later phase)."""

    def apply(doc: dict):
        if doc["links"].pop(child, None) is None:
            return _Skip()
        return None

    _mutate(apply)


# --- scope bindings ----------------------------------------------------------------------------


def bind_scope(unit: str, key: str, *, now: float | None = None) -> None:
    """Remember that ``unit`` (a scope the LAUNCH path just created) holds session ``key``.
    Best-effort: a store that cannot be written loses this one piece of evidence, nothing else."""
    if not unit or not key:
        return
    ts = time.time() if now is None else now

    def apply(doc: dict):
        doc["bindings"][unit] = {"key": key, "at": ts}

    try:
        _mutate(apply)
    except (LineageUnreadable, LineageError, OSError):
        return


def scope_key(unit: str, doc: Mapping | None = None) -> str | None:
    """The full key bound to exactly this unit name, or ``None``. Never a prefix match."""
    try:
        d = doc if doc is not None else read_checked()
    except LineageUnreadable:
        return None
    b = d["bindings"].get(unit)
    return b["key"] if b else None


# --- attribution bookkeeping -------------------------------------------------------------------


def attribution_due(keys: Iterable[str], *, now: float | None = None) -> list[str]:
    """The keys of ``keys`` that have no link, no final attribution, and whose retry is due."""
    ts = time.time() if now is None else now
    try:
        doc = read_checked()
    except LineageUnreadable:
        return []
    out = []
    for k in keys:
        if k in doc["links"]:
            continue
        a = doc["attribution"].get(k)
        if a is None or (a["state"] not in FINAL_STATES and a["next_at"] <= ts):
            out.append(k)
    return out


def _apply_attribution(doc: dict, key: str, outcome: str, parent: str | None, ts: float) -> str:
    prev = doc["attribution"].get(key) or {"tries": 0}
    tries = int(prev.get("tries", 0)) + 1
    state = outcome
    if outcome == "linked":
        if not parent or parent == key:
            state = "conflict"
        elif key in doc["links"]:
            state = "linked"  # a link already stands (equal or higher source)
        elif key in set(_ancestors(doc["links"], parent)):
            state = "conflict"
        else:
            doc["links"][key] = {"parent": parent, "source": "process", "at": ts}
    next_at = ts
    if state == "unknown":
        if tries >= MAX_TRIES:
            state = "none-final"
        else:
            next_at = ts + RETRY_BACKOFF_S[tries - 1]
    doc["attribution"][key] = {"state": state, "tries": tries, "next_at": next_at}
    return state


def record_attributions(
    results: Iterable[tuple[str, str, str | None]], *, now: float | None = None
) -> list[str]:
    """Store attribution attempts ``(key, outcome, parent)`` in ONE write; returns the states.

    ``linked`` also writes the ``process`` link, subject to precedence and cycle refusal (a refused
    link is stored as ``conflict``). ``unknown`` schedules the next try on :data:`RETRY_BACKOFF_S`
    and settles as ``none-final`` after :data:`MAX_TRIES` attempts."""
    items = list(results)
    for _k, outcome, _p in items:
        if outcome not in STATES:
            raise LineageError(f"unknown attribution outcome {outcome!r}")
    if not items:
        return []
    ts = time.time() if now is None else now
    return _mutate(lambda doc: [_apply_attribution(doc, k, o, p, ts) for k, o, p in items])


def record_attribution(
    key: str, outcome: str, *, parent: str | None = None, now: float | None = None
) -> str:
    """One attempt; see :func:`record_attributions`."""
    return record_attributions([(key, outcome, parent)], now=now)[0]


# --- the cached index --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: tuple[tuple, dict] | None = None


def _invalidate() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
    except FileNotFoundError:
        return (str(path), None)
    return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)


def links_snapshot() -> dict | None:
    """``{child: {parent, source, at}}`` cached per ledger generation (path + mtime + size + inode),
    or ``None`` when the ledger is unreadable (lineage unknown)."""
    global _cache
    path = _path()
    stamp = _stamp(path)
    with _cache_lock:
        if _cache is not None and _cache[0] == stamp:
            return _cache[1]
    try:
        links = read_checked(path)["links"]
    except LineageUnreadable:
        return None
    with _cache_lock:
        _cache = (stamp, links)
    return links


# --- the projection ----------------------------------------------------------------------------


def row_state(row: Mapping) -> str:
    """The state a row's own LED shows, in :data:`ROLLUP_ORDER` vocabulary (mirrors the SPA's
    ``sessionStatus``: attention > working > draft > idle)."""
    if row.get("intervention_required") and not row.get("review_excluded"):
        return "attention"
    if row.get("working"):
        return "working"
    if row.get("has_draft"):
        return "draft"
    return "idle"


def _worst(a: str, b: str) -> str:
    return a if ROLLUP_ORDER.index(a) <= ROLLUP_ORDER.index(b) else b


#: How many attention keys a main row previews; the full list is the attention route's.
ATTENTION_PREVIEW = 10


@dataclass
class Projection:
    """Lineage over one row set. ``mains`` keeps the input order."""

    mains: list[dict]
    children: dict[str, list[dict]] = field(default_factory=dict)
    by_id: dict[str, dict] = field(default_factory=dict)
    known: bool = True

    def descendants(self, key: str) -> list[dict]:
        out: list[dict] = []
        seen = {key}
        stack = list(reversed(self.children.get(key, [])))
        while stack:
            r = stack.pop()
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
            stack.extend(reversed(self.children.get(r["id"], [])))
        return out

    def attention(self, key: str) -> list[tuple[dict, list[dict]]]:
        """Every descendant of ``key`` that needs the operator, any depth, with its path (the rows
        from ``key`` down to the item's parent), in tree order."""
        out: list[tuple[dict, list[dict]]] = []
        root = self.by_id.get(key)
        if root is None:
            return out
        seen = {key}
        stack: list[tuple[dict, list[dict]]] = [
            (c, [root]) for c in reversed(self.children.get(key, []))
        ]
        while stack:
            r, path = stack.pop()
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            if row_state(r) == "attention":
                out.append((r, path))
            stack.extend((c, [*path, r]) for c in reversed(self.children.get(r["id"], [])))
        return out


def project(rows: list[dict], *, links: Mapping | None = None) -> Projection:
    """Split ``rows`` into main sessions and children and annotate every row in place.

    A row is a child when its recorded parent is another row of ``rows``. Annotations:

    * every row: ``child_count`` (direct children), ``descendant_count``, ``rollup`` (worst state
      over all descendants, or ``None`` without any), ``attention_count`` (descendants needing the
      operator, any depth) and ``attention`` (the first :data:`ATTENTION_PREVIEW` of their keys);
    * a child: ``parent_key`` and ``lineage_source``;
    * an orphan (recorded parent not in ``rows``): ``orphan`` = ``True``.

    One pass over rows that the caller already built; no I/O apart from the cached ledger read.
    An unreadable ledger leaves every row a main session (``Projection.known`` is ``False``).
    """
    if links is None:
        links = links_snapshot()
    known = links is not None
    links = links or {}
    by_id = {r["id"]: r for r in rows}
    children: dict[str, list[dict]] = {}
    mains: list[dict] = []
    for r in rows:
        link_ = links.get(r["id"])
        parent = link_["parent"] if link_ else None
        if parent and parent in by_id and parent != r["id"]:
            r["parent_key"] = parent
            r["lineage_source"] = link_["source"]
            children.setdefault(parent, []).append(r)
        else:
            if link_:
                r["orphan"] = True
            mains.append(r)
    for lst in children.values():
        lst.sort(
            key=lambda c: (
                links[c["id"]]["at"],
                c.get("created_at") or 0,
                c["id"],
            )
        )
    proj = Projection(mains=mains, children=children, by_id=by_id, known=known)
    # A cycle in the data (refused on write, but a hand edit could make one) would leave its members
    # neither main nor reachable: promote them to main sessions rather than hide them.
    reachable: set[str] = set()
    for m in mains:
        reachable.add(m["id"])
        reachable.update(d["id"] for d in proj.descendants(m["id"]))
    for r in rows:
        if r["id"] not in reachable:
            r.pop("parent_key", None)
            r.pop("lineage_source", None)
            r["orphan"] = True
            mains.append(r)
            reachable.add(r["id"])
            reachable.update(d["id"] for d in proj.descendants(r["id"]))
    for parent, lst in list(children.items()):
        children[parent] = [c for c in lst if "parent_key" in c]
    # ONE bottom-up pass (#2325 review): every row's aggregates come from its direct children's,
    # so the work is O(rows) whatever the depth. Full descendant and path enumeration is only for
    # the one key a lazy route asks about (`Projection.descendants` / `attention`).
    #
    # Every row starts with empty aggregates, so a row the walk below never reaches still carries
    # all of them: the walk visits each ID once, and a second row with the same id (a provider
    # that lists one session twice) would otherwise have none. The filtered list reads
    # `attention_count` on every row, and one row without it failed every search with a 500.
    for r in rows:
        r["child_count"] = 0
        r["descendant_count"] = 0
        r["rollup"] = None
        r["attention_count"] = 0
        r["attention"] = []
    state = {r["id"]: row_state(r) for r in rows}
    order: list[dict] = []  # pre-order over the forest; reversed, children come before parents
    seen: set[str] = set()
    stack = list(reversed(mains))
    while stack:
        r = stack.pop()
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        order.append(r)
        stack.extend(reversed(children.get(r["id"], [])))
    for r in reversed(order):
        kids = children.get(r["id"], [])
        desc = 0
        roll: str | None = None
        att_n = 0
        preview: list[str] = []
        for c in kids:
            desc += 1 + c["descendant_count"]
            for st in (state[c["id"]], c["rollup"]):
                if st is not None:
                    roll = st if roll is None else _worst(roll, st)
            if state[c["id"]] == "attention":
                att_n += 1
                if len(preview) < ATTENTION_PREVIEW:
                    preview.append(c["id"])
            att_n += c["attention_count"]
            if len(preview) < ATTENTION_PREVIEW:
                preview.extend(c["attention"][: ATTENTION_PREVIEW - len(preview)])
        r["child_count"] = len(kids)
        r["descendant_count"] = desc
        r["rollup"] = roll
        r["attention_count"] = att_n
        r["attention"] = preview
    return proj


def child_keys_of(present: set[str], *, links: Mapping | None = None) -> set[str]:
    """Which of ``present`` are children (their recorded parent is also in ``present``). For the
    consumers that hold a key set rather than rows: the Pulse catalog, the AI review loop. An
    unreadable ledger answers the empty set — everything stays a main session."""
    if links is None:
        links = links_snapshot()
    if not links:
        return set()
    return {
        k
        for k in present
        if (lk := links.get(k)) is not None and lk["parent"] in present and lk["parent"] != k
    }
