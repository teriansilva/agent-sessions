"""The installed manual, searchable (#2249 P2, `docs/invariants/hq-manual.md`).

HQ answers "how do I…?" from the manual this release shipped: ``docs/site`` in the release's own
source tree, the same pages docs.battlelabos.com publishes, at the version that is running.

- **The root is pinned at import**, which is app startup, and never re-resolved. On an install
  it is the interpreter's own release (``realpath(sys.prefix).parent / "src"``: the venv is
  ``<release>/venv``), so an update or rollback that switches ``current`` never changes what a
  running process reads. A source checkout falls back to this package's repo; then
  ``AGENT_SESSIONS_DOCS_ROOT``. No root means "manual not installed", never an error.
- **Transclusions are materialized**, the way VitePress publishes them, but only from
  :data:`INCLUDES`, only a regular file inside the pinned root, and never through a symlink.
  Anything else is dropped and logged.
- **Everything leaves by an id this module minted**: a chunk of at most :data:`CHUNK_MAX`
  characters, cut at a paragraph (an oversize paragraph at a sentence, else hard). The caller
  decides which ids a model may name; nothing here turns model input into a path.
- **The index is in memory** (SQLite FTS5, BM25), built on first use and rebuilt when the tree's
  files change. The query is words, each quoted, so FTS syntax in it is just text.

Network: none. Writes: none.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SITE_REL = "docs/site"
#: The sidebar's top-level folders: the pages the manual publishes and links.
FOLDERS = ("start/", "guide/", "reference/", "security/", "internals/", "contributing/")
#: VitePress' ``srcExclude`` (``docs/site/.vitepress/config.ts``); a test pins the two together.
EXCLUDE = frozenset({"SECURITY-NOTES.md"})
#: The files a page may transclude, relative to the root. A test pins this to exactly the
#: ``@include`` directives under ``docs/site``, both ways: a new one is added here on purpose.
INCLUDES = frozenset(
    {
        "CONTRIBUTING.md",
        "INSTALL.md",
        "SECURITY.md",
        "docs/home-free-handshake.md",
        "docs/infrastructure.md",
        "docs/reference.md",
        "docs/session-handling.md",
    }
)
PUBLIC = "https://docs.battlelabos.com"
CHUNK_MAX = 6000
EXCERPT_MAX = 300
SEARCH_MAX = 5
QUERY_MAX = 200
#: One page or transcluded file read at most this much; the manual's largest is far below it.
FILE_MAX = 1_000_000
_TERMS_MAX = 16
_STOP = frozenset(
    "a an and are be can do does for from how i in is it me my of on or the this to what when "
    "where which who why with you your".split()
)


@dataclass(frozen=True)
class Chunk:
    id: str
    page: str
    title: str
    heading: str
    anchor: str
    text: str
    part: int
    parts: int
    #: The next chunk in reading order within the page: this section's next part, else the next
    #: section's first. ``None`` at the page's end.
    next: str | None

    @property
    def link(self) -> str:
        return f"{PUBLIC}/{self.page}" + (f"#{self.anchor}" if self.anchor else "")


# ---- the root ------------------------------------------------------------------------------------


def _has_site(root: Path) -> bool:
    return (root / SITE_REL).is_dir()


def resolve_root(prefix: str | None = None, env: str | None = None) -> Path | None:
    """Where this process' manual lives, resolved now. ``prefix``/``env`` are for tests."""
    prefix = sys.prefix if prefix is None else prefix
    env = os.environ.get("AGENT_SESSIONS_DOCS_ROOT", "") if env is None else env
    candidates = [
        Path(os.path.realpath(prefix)).parent / "src",  # an install: <release>/venv → <release>/src
        Path(os.path.realpath(__file__)).parents[2],  # a source checkout: <repo>/src/agent_sessions
    ]
    if env:
        candidates.append(Path(os.path.realpath(env)))
    for c in candidates:
        if _has_site(c):
            return c
    return None


#: Pinned at import, i.e. at app startup — never on the first lookup (see the module docstring).
_ROOT: Path | None = resolve_root()


def root() -> Path | None:
    return _ROOT


# ---- markdown → sections → chunks ----------------------------------------------------------------

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_INCLUDE = re.compile(r"<!--\s*@include:\s*([^\s>]+?)\s*-->")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^(#{1,3})\s+(.*?)\s*$")
_CUSTOM_ANCHOR = re.compile(r"\s*\{#([A-Za-z0-9_-]+)\}\s*$")
_CONTAINER = re.compile(r"^:::+\s*(\w+)?\s*(.*)$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_SPECIAL = re.compile(r"[\s~`!@#$%^&*()\-_+=\[\]{}|\\;:\"'“”‘’<>,.?/]+")


class _Fences:
    """CommonMark fences: an opening run of 3+ backticks or tildes is closed only by a run of the
    same character, at least as long, with nothing after it. So a ```` block can hold ``` ones."""

    def __init__(self) -> None:
        self.open: str | None = None

    def step(self, line: str) -> bool:
        """Whether ``line`` is code (a fence line or inside one), advancing the state."""
        m = _FENCE.match(line)
        if self.open is None:
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                self.open = m.group(1)
                return True
            return False
        run = m.group(1) if m else ""
        if run and run[0] == self.open[0] and len(run) >= len(self.open) and not m.group(2).strip():
            self.open = None
        return True


def _strip_tags(text: str) -> str:
    """HTML/Vue tags out, except inside inline code (`` `agent-sessions <subcommand>` ``)."""
    parts = re.split(r"(`+[^`]*`+)", text)
    return "".join(part if i % 2 else _TAG.sub("", part) for i, part in enumerate(parts))


def slugify(text: str) -> str:
    """VitePress' heading slug (`@mdit-vue/shared` ``slugify``) over the heading's plain text."""
    s = unicodedata.normalize("NFKD", text)
    s = re.sub(r"[̀-ͯ]", "", s)
    s = re.sub(r"[\u0000-\u001f]", "", s)
    s = _SPECIAL.sub("-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = re.sub(r"^(\d)", r"_\1", s)
    return s.lower()


def _plain_inline(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = _strip_tags(text)
    return re.sub(r"[*_`]", "", text).strip()


def _frontmatter(text: str) -> tuple[str, str]:
    m = _FRONTMATTER.match(text)
    return (m.group(1), text[m.end() :]) if m else ("", text)


def is_stub(text: str) -> bool:
    """A ``search: false`` page: a "this page moved" redirect, not content."""
    fm, _ = _frontmatter(text)
    return bool(re.search(r"^search:\s*false\s*$", fm, re.M))


def _read_regular(path: Path) -> str | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > FILE_MAX:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _include(root: Path, page_dir: Path, target: str, page: str) -> str:
    """The text a directive transcludes, or "" when it is not allowed (logged, never followed)."""
    if "#" in target or "{" in target:  # regions and line ranges: unused here, so not supported
        log.warning("docs: %s: unsupported include %r dropped", page, target)
        return ""
    lexical = Path(os.path.normpath(page_dir / target))
    real = Path(os.path.realpath(lexical))
    # realpath == normpath ⇔ no symlink anywhere on the way (the root itself is already real).
    if real != lexical or not real.is_relative_to(root):
        log.warning("docs: %s: include %r leaves the root or uses a symlink, dropped", page, target)
        return ""
    rel = real.relative_to(root).as_posix()
    if rel not in INCLUDES:
        log.warning("docs: %s: include %r is not in INCLUDES, dropped", page, rel)
        return ""
    body = _read_regular(real)
    if body is None:
        log.warning("docs: %s: include %r is not a readable regular file, dropped", page, rel)
        return ""
    # One level: a directive inside a transcluded file is not followed.
    return _INCLUDE.sub("", _frontmatter(body)[1])


def _clean_lines(lines: list[str]) -> list[str]:
    """Outside code fences: VitePress containers become their title, HTML/Vue tags go."""
    out, fences = [], _Fences()
    for line in lines:
        if not fences.step(line):
            c = _CONTAINER.match(line.strip())
            if c:
                line = c.group(2)
            line = _strip_tags(line)
        out.append(line)
    return out


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated, a fenced block kept whole."""
    paras, cur, fences = [], [], _Fences()
    for line in text.split("\n"):
        code = fences.step(line)
        if not code and not line.strip():
            if cur:
                paras.append("\n".join(cur).strip())
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append("\n".join(cur).strip())
    return [p for p in paras if p]


def _split_oversize(para: str) -> list[str]:
    if len(para) <= CHUNK_MAX:
        return [para]
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", para):
        while len(sentence) > CHUNK_MAX:  # no sentence boundary to use: hard cut
            if cur:
                out.append(cur)
                cur = ""
            out.append(sentence[:CHUNK_MAX])
            sentence = sentence[CHUNK_MAX:]
        if cur and len(cur) + 1 + len(sentence) > CHUNK_MAX:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}" if cur else sentence
    if cur:
        out.append(cur)
    return out


def _pack(text: str) -> list[str]:
    parts, cur = [], ""
    for para in _paragraphs(text):
        for piece in _split_oversize(para):
            if cur and len(cur) + 2 + len(piece) > CHUNK_MAX:
                parts.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts


def parse_page(root: Path, page: str, path: Path, raw: str) -> list[Chunk]:
    """One page's chunks, in reading order."""
    _, body = _frontmatter(raw)
    body = _INCLUDE.sub(lambda m: _include(root, path.parent, m.group(1), page), body)
    body = _COMMENT.sub("", body)
    lines = _clean_lines(body.split("\n"))
    title = page
    sections: list[tuple[str, str, list[str]]] = []  # (heading, anchor, lines)
    used: dict[str, int] = {}
    cur: tuple[str, str, list[str]] = ("", "", [])
    fences = _Fences()
    for line in lines:
        m = None if fences.step(line) else _HEADING.match(line)
        if not m:
            cur[2].append(line)
            continue
        raw_heading = m.group(2)
        custom = _CUSTOM_ANCHOR.search(raw_heading)
        heading = _plain_inline(_CUSTOM_ANCHOR.sub("", raw_heading))
        anchor = custom.group(1) if custom else slugify(heading)
        if not custom and anchor in used:  # markdown-it-anchor's duplicate rule: -1, -2, …
            used[anchor] += 1
            anchor = f"{anchor}-{used[anchor]}"
        used.setdefault(anchor, 0)
        if len(m.group(1)) == 1:
            if title == page:
                title = heading or page
            cur[2].append("")
            continue
        sections.append(cur)
        cur = (heading, anchor, [])
    sections.append(cur)
    chunks: list[Chunk] = []
    for heading, anchor, body_lines in sections:
        parts = _pack("\n".join(body_lines))
        for n, text in enumerate(parts, 1):
            chunks.append(
                Chunk(
                    id=f"{page}#{anchor}:{n}",
                    page=page,
                    title=title,
                    heading=heading or title,
                    anchor=anchor,
                    text=text,
                    part=n,
                    parts=len(parts),
                    next=None,
                )
            )
    return [
        Chunk(**{**c.__dict__, "next": chunks[i + 1].id if i + 1 < len(chunks) else None})
        for i, c in enumerate(chunks)
    ]


def pages(root: Path) -> list[tuple[str, Path]]:
    """The published pages under the root's manual, as (page id, file); stubs excluded."""
    site = root / SITE_REL
    out = []
    for f in sorted(site.rglob("*.md")):
        rel = f.relative_to(site).as_posix()
        if rel in EXCLUDE or not rel.startswith(FOLDERS):
            continue
        # A page reached through a symlink (the file or a folder on the way) is not the manual's.
        if Path(os.path.realpath(f)) != Path(os.path.normpath(f)):
            continue
        page = rel[: -len("index.md")] if rel.endswith("/index.md") else rel[: -len(".md")]
        out.append((page, f))
    return out


def build(root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for page, f in pages(root):
        raw = _read_regular(f)
        if raw is None or is_stub(raw):
            continue
        chunks.extend(parse_page(root, page, f, raw))
    return chunks


# ---- the index -----------------------------------------------------------------------------------


def _fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    files = [f for _, f in pages(root)] + [root / rel for rel in sorted(INCLUDES)]
    for f in files:
        try:
            st = os.lstat(f)
        except OSError:
            continue
        h.update(f"{f}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return h.hexdigest()


class Index:
    def __init__(self, root: Path):
        self.root = root
        self.fingerprint = _fingerprint(root)
        self.chunks = {c.id: c for c in build(root)}
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(id UNINDEXED, title, heading, body, "
            "tokenize='porter unicode61 remove_diacritics 2')"
        )
        self.db.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?)",
            [(c.id, c.title, c.heading, c.text) for c in self.chunks.values()],
        )

    def search(self, terms: list[str], limit: int) -> list[tuple[Chunk, str]]:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        rows = self.db.execute(
            "SELECT id, snippet(chunks, 3, '', '', '…', 48) FROM chunks WHERE chunks MATCH ? "
            "ORDER BY bm25(chunks, 0.0, 4.0, 6.0, 1.0) LIMIT ?",
            (match, limit),
        ).fetchall()
        return [(self.chunks[i], snip) for i, snip in rows if i in self.chunks]


_LOCK = threading.Lock()
_INDEX: Index | None = None


def _index() -> Index | None:
    global _INDEX
    r = root()
    if r is None:
        return None
    with _LOCK:
        if _INDEX is None or _INDEX.root != r or _INDEX.fingerprint != _fingerprint(r):
            _INDEX = Index(r)
        return _INDEX


def terms(query: str) -> list[str]:
    words = [w for w in re.findall(r"\w+", query.lower()) if w not in _STOP]
    return list(dict.fromkeys(words))[:_TERMS_MAX]


def search(query: str, limit: int = SEARCH_MAX) -> list[dict] | None:
    """The best chunks for ``query``; ``None`` when no manual is installed. Blocking."""
    idx = _index()
    if idx is None:
        return None
    words = terms(query)
    if not words:
        return []
    with _LOCK:
        found = idx.search(words, limit)
    return [
        {
            "id": c.id,
            "page": c.page,
            "page_title": c.title,
            "heading": c.heading,
            "excerpt": " ".join(snip.split())[:EXCERPT_MAX],
            "link": c.link,
        }
        for c, snip in found
    ]


def read(chunk_id: str) -> Chunk | None:
    """One chunk by an id this index minted; ``None`` for any other string. Blocking."""
    idx = _index()
    return None if idx is None else idx.chunks.get(chunk_id)
