"""One scoped Memory service for HQ, agents and the operator (#2036).

SQLite notebooks remain the authority. Search is a derived index; every hit is resolved here
under the secret fence and notebook lock. A principal is constructed by the trusted ingress
(operator route or launch broker), never deserialized from a model's request. File/inbox clients
are compatibility projections/producers of these same notebooks, not another note store.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

from . import memory, projects, template_secrets

NOTEBOOKS_PAGE = 25
MAX_HITS = 10
QUERY_MAX = 200
EXCERPT_MAX = 400


_HQ_SOURCE = re.compile(r"ask_[0-9a-f]{32}/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def _source_label(source: str) -> str:
    # Only structural provenance, never note/quote text. Stored-secret checks still see the
    # original bytes. These server-issued identifiers otherwise resemble random credentials.
    return "HQ conversation" if _HQ_SOURCE.fullmatch(source) else source


def _note_problem(note, known) -> bool:
    text = memory._note_text(note)
    if known and template_secrets.redact_text(text, known) != text:
        return True
    fields = dict(note)
    fields["source_text"] = "\n".join(
        _source_label(line) for line in fields.get("source_text", "").splitlines()
    )
    return bool(memory._withheld(memory._note_text(fields), []))


@dataclass(frozen=True)
class Principal:
    scopes: tuple[str, ...] = ()
    operator: bool = False


def worker(scopes: list[str] | tuple[str, ...]) -> Principal:
    checked = tuple(dict.fromkeys(memory._scope_id(s) for s in scopes))
    if not checked or len(checked) > 2 or memory.HQ_SCOPE in checked:
        raise memory.MemoryStoreError("not a worker's notebook", 403)
    owners = {memory.project_of(s) for s in checked}
    if len(owners) != 1:
        raise memory.MemoryStoreError("not a worker's notebook", 403)
    return Principal(checked)


def operator() -> Principal:
    return Principal(operator=True)


def authorize(principal: Principal, scope: str) -> str:
    scope = memory._scope_id(scope)
    if not principal.operator and scope not in principal.scopes:
        raise memory.MemoryStoreError("not an offered notebook", 403)
    memory._project(memory.project_of(scope))
    return scope


def notebooks(
    principal: Principal, *, archived: bool = False, offset: int = 0, limit: int | None = None
) -> list[dict]:
    """Metadata only, without reading note bodies or building the operator's complete views."""
    entities = projects.load()
    scopes = [memory.HQ_SCOPE] if principal.operator else []
    stored_scopes = memory._list_scopes() if principal.operator else []
    if principal.operator:
        for entity in sorted(entities.values(), key=lambda e: (e.name.casefold(), e.id)):
            if entity.archived and not archived:
                continue
            scopes.extend(
                [
                    entity.id,
                    *[s for s in stored_scopes if s.startswith(entity.id + memory.SPECIALIST_SEP)],
                ]
            )
    else:
        scopes = list(principal.scopes)
    out = []
    with memory._fenced():
        known = template_secrets.redaction_values()
    for scope in scopes[offset : offset + limit if limit is not None else None]:
        owner = memory.project_of(scope)
        entity = memory._project(owner) if owner == memory.HQ_SCOPE else entities.get(owner)
        if entity is None:
            continue
        row = {
            "id": scope,
            "name": memory._safe_name(entity.name, known),
            "project": None if owner == memory.HQ_SCOPE else owner,
            "specialist": scope.partition(memory.SPECIALIST_SEP)[2] or None,
            "archived": bool(entity.archived),
            "enabled": scope == memory.HQ_SCOPE,
            "notes": 0,
        }
        if memory._exists(scope):
            with readonly(scope) as conn:
                row["notes"] = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                if scope != memory.HQ_SCOPE:
                    parent = owner if row["specialist"] else scope
                    with readonly(parent) as parent_conn:
                        row["enabled"] = memory._enabled(parent_conn)
        out.append(row)
    return out


@contextlib.contextmanager
def readonly(scope: str):
    """A query never creates/migrates a store or changes its WAL/checkpoint policy."""
    conn = sqlite3.connect(
        (memory._scope(scope) / "state.db").resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        timeout=5,
    )
    conn.row_factory = sqlite3.Row
    try:
        memory._check_source_format(conn)
        yield conn
    finally:
        conn.close()


def revision(conn: sqlite3.Connection, slug: str) -> int:
    try:
        row = conn.execute("SELECT revision FROM memory_revisions WHERE slug=?", (slug,)).fetchone()
    except sqlite3.OperationalError:
        return 0  # legacy source, before its additive schema upgrade
    return row[0] if row else 0


def digest(note: dict) -> str:
    # Operator edits historically retain the intake digest. Hash the fields actually being read.
    return hashlib.sha256(memory._note_text(note).encode()).hexdigest()


def source_origin(cid: str, tid: str, quote: str) -> str:
    """One operator assertion, shared by explicit remember and automatic capture."""
    return f"hq:{cid}:{tid}:fact:" + hashlib.sha256(quote.encode()).hexdigest()[:24]


def _notebook_info(conn, scope, known):
    owner = memory.project_of(scope)
    entity = memory._project(owner)
    specialist = scope.partition(memory.SPECIALIST_SEP)[2]
    enabled = scope == memory.HQ_SCOPE or memory._enabled(conn)
    if specialist:
        with readonly(owner) as parent:
            enabled = memory._enabled(parent)
    return {
        "id": scope,
        "kind": "hq" if scope == memory.HQ_SCOPE else "specialist" if specialist else "project",
        "name": memory._safe_name(entity.name, known),
        "specialist": specialist or None,
        "enabled": enabled,
        "archived": bool(entity.archived),
    }


def _resolve_note(conn, scope, row, known, expected=None, *, notebook=None):
    note = dict(row)
    slug = note["slug"]
    if _note_problem(note, known):
        raise memory.MemoryStoreError("note withheld", 404)
    note.update(
        scope=scope,
        revision=note["revision"],
        generation=memory._generation(conn),
        content_digest=digest(note),
    )
    note["notebook"] = notebook if notebook is not None else _notebook_info(conn, scope, known)
    if expected is not None and any(
        expected.get(k) != note[k] for k in ("revision", "generation", "content_digest")
    ):
        raise memory.MemoryStoreError("note changed; search again", 409)
    note.pop("digest", None)
    note.pop("token", None)
    note.pop("source_text", None)
    note.pop("hq_source", None)
    note["source"] = None
    try:
        source = conn.execute(
            "SELECT kind, source, quote FROM memory_sources"
            " WHERE slug=? AND deleted=0 ORDER BY rowid DESC LIMIT 1",
            (slug,),
        ).fetchone()
    except sqlite3.OperationalError:
        source = None
    if source is not None:
        if (
            known and template_secrets.redact_text("\n".join(source), known) != "\n".join(source)
        ) or memory._withheld(
            source["kind"] + "\n" + _source_label(source["source"]) + "\n" + source["quote"],
            [],
        ):
            raise memory.MemoryStoreError("note withheld", 404)
        if scope == memory.HQ_SCOPE:
            from . import ask_store

            cid = source["source"].partition("/")[0]
            if ask_store.valid_cid(cid) and not ask_store.exists(cid):
                raise memory.MemoryStoreError("note's source was deleted", 404)
        note["source"] = dict(source)
    return note


def read(principal: Principal, scope: str, slug: str, *, expected: dict | None = None) -> dict:
    scope = authorize(principal, scope)
    if not isinstance(slug, str) or not memory._SLUG_RE.fullmatch(slug):
        raise memory.MemoryStoreError("not a note", 404)
    with memory._fenced(), memory._scope_lock(scope):
        if not memory._exists(scope):
            raise memory.MemoryStoreError("not a note", 404)
        known = template_secrets.redaction_values()
        with readonly(scope) as conn:
            if memory._meta(conn, "clearing", "0") == "1":
                raise memory.MemoryStoreError("memory is being cleared", 409)
            rows = memory._note_rows(conn, slug)
            if not rows:
                raise memory.MemoryStoreError("not a note", 404)
            return _resolve_note(conn, scope, rows[0], known, expected)


def read_many(principal: Principal, scope: str, slugs: list[str]) -> list[dict]:
    """A bounded index-worker snapshot through the SAME resolver, using one source connection."""
    scope = authorize(principal, scope)
    if len(slugs) > 64 or any(
        not isinstance(s, str) or not memory._SLUG_RE.fullmatch(s) for s in slugs
    ):
        raise memory.MemoryStoreError("invalid note batch", 422)
    with memory._fenced(), memory._scope_lock(scope):
        if not memory._exists(scope):
            return []
        known = template_secrets.redaction_values()
        with readonly(scope) as conn:
            if memory._meta(conn, "clearing", "0") == "1":
                return []
            notebook = _notebook_info(conn, scope, known)
            notes = []
            for slug in slugs:
                rows = memory._note_rows(conn, slug)
                if not rows:
                    continue
                try:
                    notes.append(_resolve_note(conn, scope, rows[0], known, notebook=notebook))
                except memory.MemoryStoreError as exc:
                    if exc.status != 404:
                        raise
            return notes


def submit(
    principal: Principal,
    scope: str,
    *,
    title: str,
    body: str,
    origin: str,
    kind: str = "finding",
    source: str = "",
    quote: str = "",
    source_kind: str | None = None,
    expected_generation: int | None = None,
    capture_revision: int | None = None,
) -> dict:
    """Admit a small note and source receipt in the existing notebook transaction.

    The broker/model cannot turn a notebook on. Every producer uses the same `_commit_one`
    withhold/quarantine/size/cap/receipt rules; a replayed source cannot resurrect a forgotten note.
    """
    scope = authorize(principal, scope)
    source_kind = source_kind or ("operator" if principal.operator else "agent")
    if not isinstance(source_kind, str) or source_kind not in {
        "explicit",
        "capture",
        "operator",
        "agent",
        "import",
    }:
        raise memory.MemoryStoreError("invalid provenance", 422)
    if (
        not isinstance(kind, str)
        or kind not in memory.TYPES
        or not all(isinstance(v, str) for v in (title, body, source, quote))
    ):
        raise memory.MemoryStoreError("invalid note", 422)
    if not isinstance(origin, str) or not 1 <= len(origin) <= 256:
        raise memory.MemoryStoreError("invalid note source", 422)
    title = memory._one_line(title, 120)
    text = f"---\nname: {title}\ntype: {kind}\n---\n\n{body}"
    if (
        not title
        or len(text.encode()) > memory.ENTRY_MAX_BYTES
        or len(quote) > 2000
        or len(source) > 300
    ):
        raise memory.MemoryStoreError("note or source too large", 422)
    text = memory._CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    note = memory.parse_note(text, "note.md")
    if note is None:
        raise memory.MemoryStoreError("a note needs a body", 422)
    owner = memory.project_of(scope)
    outer = contextlib.nullcontext() if owner == memory.HQ_SCOPE else projects.locked_index()
    with outer, memory._fenced(), memory._decision_locks(scope):
        authorize(principal, scope)
        if not memory._exists(scope):
            if scope != memory.HQ_SCOPE:
                raise memory.MemoryStoreError("memory is off", 409)
            memory._create_scope(scope)
        conn = memory._connect(scope)
        try:
            generation = memory._generation(conn)
            if (
                not memory._on(scope, conn)
                or memory._meta(conn, "clearing", "0") == "1"
                or (expected_generation is not None and expected_generation != generation)
            ):
                raise memory.MemoryStoreError("memory changed or is off", 409)
            if capture_revision is not None and (
                memory._meta(conn, "capture_enabled", "0") != "1"
                or int(memory._meta(conn, "capture_revision", "0")) != capture_revision
            ):
                raise memory.MemoryStoreError("capture changed", 409)
            previous = conn.execute(
                "SELECT * FROM memory_sources WHERE origin=?", (origin,)
            ).fetchone()
            if (
                scope == memory.HQ_SCOPE
                and conn.execute(
                    "SELECT 1 FROM memory_blocked_sources WHERE source=?", (source,)
                ).fetchone()
            ):
                return {"state": "forgotten"}
            if previous is not None:
                return {
                    "state": "forgotten" if previous["deleted"] else "saved",
                    "slug": previous["slug"],
                }
            if (
                scope == memory.HQ_SCOPE
                and conn.execute("SELECT 1 FROM notes WHERE slug=?", (note["slug"],)).fetchone()
            ):
                return {
                    "state": "exists",
                    "slug": note["slug"],
                    "note": "An existing HQ note needs a confirmed correction.",
                }
            known = template_secrets.redaction_values()
            why = memory._withheld(text + "\n" + _source_label(source) + "\n" + quote, known)
            if known and template_secrets.redact_text(source, known) != source:
                why = "contains a stored secret value"
            c = memory._Candidate(
                token="m" + secrets.token_hex(16),
                filename="note.md",
                size=len(text.encode()),
                digest=hashlib.sha256((origin + "\0" + text).encode()).hexdigest(),
                text=text,
                note=note,
                outcome="quarantine" if why else "note",
                reason=why,
            )
            with memory._txn(conn):
                outcome = memory._commit_one(conn, c, time.time())
                if outcome == "notes":
                    conn.execute(
                        "INSERT INTO memory_sources(origin,slug,kind,source,quote,generation)"
                        " VALUES(?,?,?,?,?,?)",
                        (origin, note["slug"], source_kind, source, quote, generation),
                    )
            memory._render(scope, conn)
            if outcome != "notes":
                return {"state": "quarantined" if outcome == "quarantined" else "refused"}
            return {
                "state": "saved",
                "scope": scope,
                "slug": note["slug"],
                "revision": revision(conn, note["slug"]),
            }
        finally:
            conn.close()


def capture_settings() -> dict:
    if not memory._exists(memory.HQ_SCOPE):
        return {"enabled": False, "revision": 0, "generation": 0}
    with readonly(memory.HQ_SCOPE) as conn:
        return {
            "enabled": memory._meta(conn, "capture_enabled", "0") == "1",
            "revision": int(memory._meta(conn, "capture_revision", "0")),
            "generation": memory._generation(conn),
        }


def set_capture(enabled: bool) -> dict:
    if not isinstance(enabled, bool):
        raise memory.MemoryStoreError("enabled must be boolean", 422)
    with memory._scope_lock(memory.HQ_SCOPE):
        if not memory._exists(memory.HQ_SCOPE):
            memory._create_scope(memory.HQ_SCOPE)
        conn = memory._connect(memory.HQ_SCOPE)
        try:
            with memory._txn(conn):
                memory._set_meta(conn, "capture_enabled", "1" if enabled else "0")
                current = int(memory._meta(conn, "capture_revision", "0"))
                memory._set_meta(conn, "capture_revision", str(current + 1))
        finally:
            conn.close()
    return capture_settings()


def search(
    principal: Principal, query: str, *, scopes: list[str] | None = None, limit: int = 8
) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > QUERY_MAX:
        raise memory.MemoryStoreError("query must be 1–200 characters", 422)
    if type(limit) is not int or not 1 <= limit <= MAX_HITS:
        raise memory.MemoryStoreError("invalid result limit", 422)
    from . import memory_index

    entities = projects.load()
    if scopes is not None:
        selected = scopes
    elif not principal.operator:
        selected = list(principal.scopes)
    else:
        # Discovery for a search uses index metadata and the current project catalog, not N
        # notebook/count reads. A deleted or archived project is excluded before ranking.
        selected = [
            s
            for s in memory_index.available_scopes()
            if s == memory.HQ_SCOPE
            or (memory.project_of(s) in entities and not entities[memory.project_of(s)].archived)
        ]
    # Validate the whole selection against ONE current catalog, before ranking. Re-loading the
    # catalog per scope made global queries quadratic in the number of projects. Hydration
    # independently re-authorizes each selected source under its notebook lock.
    checked = []
    for raw in selected:
        scope = memory._scope_id(raw)
        if not principal.operator and scope not in principal.scopes:
            raise memory.MemoryStoreError("not an offered notebook", 403)
        owner = memory.project_of(scope)
        if owner != memory.HQ_SCOPE and owner not in entities:
            raise memory.MemoryStoreError("unknown project", 404)
        checked.append(scope)
    selected = list(dict.fromkeys(checked))
    with memory._fenced():
        if memory._withheld(query, template_secrets.redaction_values()):
            raise memory.MemoryStoreError("query withheld", 422)
    return memory_index.search(principal, query, selected, limit)
