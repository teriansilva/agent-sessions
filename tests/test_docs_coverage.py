"""The docs-coverage ledger is checked against the code, never against its own table (#2249).

`docs/docs-coverage.md` maps every part of BattleLab to the manual page that documents it, so HQ
(which answers from the manual) has something to cite. This test derives the parts from the code
and fails on any the ledger doesn't map:

* every navigation entry in ``web/src/components/shell/sections.ts`` — each of ``SECTIONS`` and
  its ``children``, ``HQ_VIEWS`` included;
* every ``####`` capability group in ``docs/hq-coverage.md``'s ledger, which
  ``tests/test_hq_coverage.py`` in turn checks against the routes and prefs.

It also checks the pages: each one a row names exists and is in the VitePress sidebar, and every
published page is in the sidebar. And it fails on a row for an entry, a group or a page that no
longer exists, so the file can't drift the other way.

It guarantees inventory only. Whether a page is right, and whether a change inside an area updated
it, is the review rule in CONTRIBUTING.md ("Docs are part of done").

Every check is a pure function of text, so each also runs on a synthetic input that must fail.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "docs-coverage.md"
HQ_COVERAGE = ROOT / "docs" / "hq-coverage.md"
SECTIONS_TS = ROOT / "web" / "src" / "components" / "shell" / "sections.ts"
SITE = ROOT / "docs" / "site"
CONFIG = SITE / ".vitepress" / "config.ts"

GAP_RE = re.compile(r"gap #[1-9][0-9]*")
PAGE_RE = re.compile(r"[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)*/?")


# ---- the code's side -----------------------------------------------------------------------------


def _strip_line_comments(ts: str) -> str:
    # `//` that starts a comment: at the line start or after whitespace (never `https://`).
    return re.sub(r"(^|\s)//[^\n]*", r"\1", ts, flags=re.M)


def _array_literal(ts: str, name: str) -> str:
    """The text of `export const <name>: … = [ … ];`, brackets balanced."""
    m = re.search(rf"export const {name}\b[^=]*=\s*\[", ts)
    assert m, f"sections.ts has no `{name}` array: has it moved?"
    depth, start = 0, m.end() - 1
    for i in range(start, len(ts)):
        if ts[i] in "[{":
            depth += 1
        elif ts[i] in "]}":
            depth -= 1
            if depth == 0:
                return ts[start : i + 1]
    raise AssertionError(f"`{name}` is not closed")


def _ids_by_depth(literal: str) -> list[tuple[int, str]]:
    """Each `id: "<x>"` in an array literal with its bracket depth: 2 = an element of the array,
    4 = an element of an element's nested array (`children: [ … ]`)."""
    out, depth = [], 0
    for m in re.finditer(r'[\[\]{}]|\bid:\s*"([a-z0-9-]+)"', literal):
        tok = m.group(0)
        if tok in "[{":
            depth += 1
        elif tok in "]}":
            depth -= 1
        else:
            out.append((depth, m.group(1)))
    return out


def nav_entries(ts: str) -> set[str]:
    """`section:<id>` for each of SECTIONS, `sub:<id>` for each child, HQ_VIEWS included."""
    ts = _strip_line_comments(ts)
    views = {f"sub:{i}" for d, i in _ids_by_depth(_array_literal(ts, "HQ_VIEWS")) if d == 2}
    sections = _array_literal(ts, "SECTIONS")
    found: set[str] = set()
    for depth, ident in _ids_by_depth(sections):
        if depth == 2:
            found.add(f"section:{ident}")
        elif depth == 4:
            found.add(f"sub:{ident}")
        else:
            raise AssertionError(f"sections.ts: id {ident!r} at an unexpected depth {depth}")
    if re.search(r"children:\s*HQ_VIEWS\b", sections):
        found |= views
    return found


def hq_groups(md: str) -> set[str]:
    """The `####` headings of hq-coverage.md's `## The ledger` section."""
    m = re.search(r"^## The ledger\n(.*?)(?=^## )", md, re.S | re.M)
    assert m, "hq-coverage.md has no `## The ledger` section: has it moved?"
    return {h.strip() for h in re.findall(r"^#### (.+)$", m.group(1), re.M)}


def sidebar_links(config: str) -> set[str]:
    m = re.search(r"\bsidebar:\s*\[(.*?)\n    \],", config, re.S)
    assert m, "config.ts has no sidebar: has it moved?"
    return set(re.findall(r'link:\s*"(/[^"]*)"', m.group(1)))


def src_excludes(config: str) -> set[str]:
    m = re.search(r"srcExclude:\s*\[(.*?)\]", config, re.S)
    return set(re.findall(r'"([^"]+)"', m.group(1))) if m else set()


def page_file(site: Path, page: str) -> Path:
    return site / (f"{page}index.md" if page.endswith("/") else f"{page}.md")


def page_link(page: str) -> str:
    return f"/{page}"


def _is_redirect_stub(text: str) -> bool:
    fm = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return bool(fm and re.search(r"^search:\s*false\s*$", fm.group(1), re.M))


def published_pages(site: Path, excludes: set[str]) -> dict[str, bool]:
    """Every page VitePress publishes, as a ledger page id → whether it is a redirect stub. The
    home page (`index.md` at the root) is the docs home, not a sidebar entry."""
    out: dict[str, bool] = {}
    for f in sorted(site.rglob("*.md")):
        rel = f.relative_to(site).as_posix()
        if rel.startswith(("node_modules/", ".vitepress/")) or rel in excludes or rel == "index.md":
            continue
        page = rel[: -len("index.md")] if rel.endswith("/index.md") else rel[: -len(".md")]
        out[page] = _is_redirect_stub(f.read_text())
    return out


# ---- the ledger's side ---------------------------------------------------------------------------


def ledger_block(md: str, name: str) -> dict[str, list[str]]:
    m = re.search(rf"```{name}\n(.*?)\n```", md, re.S)
    assert m, f"docs-coverage.md has no {name} block"
    rows: dict[str, list[str]] = {}
    for line in m.group(1).splitlines():
        body = re.split(r"\s{2,}# ", line, maxsplit=1)[0].strip()
        if not body:
            continue
        key, sep, target = (s.strip() for s in body.partition("->"))
        assert sep, f"docs-coverage.md {name}: {line!r} has no `->`"
        assert key not in rows, f"docs-coverage.md {name}: {key!r} is mapped twice"
        rows[key] = [t.strip() for t in target.split(",")]
    return rows


def row_problems(rows: dict[str, list[str]]) -> list[str]:
    """A row is a list of pages, or exactly one `gap #<issue>`; never empty, never both."""
    bad = []
    for key, targets in rows.items():
        gaps = [t for t in targets if t.startswith("gap")]
        if gaps:
            if len(targets) != 1 or not GAP_RE.fullmatch(gaps[0]):
                bad.append(f"{key}: a gap row is exactly `gap #<issue>`, got {targets}")
        elif not targets or not all(PAGE_RE.fullmatch(t) for t in targets):
            bad.append(f"{key}: not a page list: {targets}")
    return bad


def page_problems(
    rows: dict[str, list[str]], site: Path, sidebar: set[str], published: dict[str, bool]
) -> list[str]:
    bad = []
    for key, targets in rows.items():
        for page in targets:
            if GAP_RE.fullmatch(page):
                continue
            if not page_file(site, page).is_file():
                bad.append(f"{key}: page {page!r} does not exist")
            elif published.get(page):
                bad.append(f"{key}: page {page!r} is a redirect stub, name its target")
            elif page_link(page) not in sidebar:
                bad.append(f"{key}: page {page!r} is not in the sidebar")
    return bad


def orphans(published: dict[str, bool], sidebar: set[str]) -> list[str]:
    return sorted(p for p, stub in published.items() if not stub and page_link(p) not in sidebar)


def _diff(live: set[str], mapped: set[str]) -> tuple[list[str], list[str]]:
    return sorted(live - mapped), sorted(mapped - live)


# ---- the real tree -------------------------------------------------------------------------------


def _ledger() -> str:
    return LEDGER.read_text()


def _config() -> str:
    return CONFIG.read_text()


def _published() -> dict[str, bool]:
    return published_pages(SITE, src_excludes(_config()))


def test_every_nav_entry_is_mapped_and_no_mapped_entry_is_gone():
    live = nav_entries(SECTIONS_TS.read_text())
    assert {"section:mission", "sub:dispatches", "sub:map"} <= live, "sections.ts parse broke"
    missing, stale = _diff(live, set(ledger_block(_ledger(), "docs-nav")))
    assert not missing, f"navigation entries the docs ledger doesn't map: {missing}"
    assert not stale, f"docs ledger rows for entries that no longer exist: {stale}"


def test_every_hq_coverage_group_is_mapped_and_no_mapped_group_is_gone():
    live = hq_groups(HQ_COVERAGE.read_text())
    assert len(live) >= 5, "hq-coverage.md groups were not found: has the ledger moved?"
    missing, stale = _diff(live, set(ledger_block(_ledger(), "docs-groups")))
    assert not missing, f"hq-coverage groups the docs ledger doesn't map: {missing}"
    assert not stale, f"docs ledger rows for groups that no longer exist: {stale}"


def test_every_row_is_a_page_list_or_one_gap():
    for name in ("docs-nav", "docs-groups"):
        bad = row_problems(ledger_block(_ledger(), name))
        assert not bad, f"{name}: {bad}"


def test_every_mapped_page_exists_and_is_in_the_sidebar():
    sidebar, published = sidebar_links(_config()), _published()
    assert "/guide/hq" in sidebar, "sidebar parse broke"
    for name in ("docs-nav", "docs-groups"):
        bad = page_problems(ledger_block(_ledger(), name), SITE, sidebar, published)
        assert not bad, f"{name}: {bad}"


def test_every_published_page_is_in_the_sidebar():
    published = _published()
    assert "guide/hq" in published and "reference/" in published, "page walk broke"
    lost = orphans(published, sidebar_links(_config()))
    assert not lost, f"published pages no sidebar entry reaches: {lost}"


# ---- the red cases: each check fails on an input that should fail ------------------------------

_TS = """
export const HQ_VIEWS: Subsection[] = [
  { id: "hq", label: "Conversations", Icon: Crosshair, to: HQ_PATH },
  { id: "missions", label: "Missions", Icon: Crosshair, to: MISSION_PATH },
];
export const SECTIONS: Section[] = [
  // a comment with { braces } and id: "not-an-id"
  { id: "ask", label: "Dashboard", Icon: LayoutDashboard, to: DASHBOARD_PATH },
  { id: "mission", label: "HQ", Icon: Crosshair, to: HQ_PATH, children: HQ_VIEWS },
  {
    id: "library", label: "Library", Icon: Library, to: TEMPLATES_PATH,
    children: [
      { id: "templates", label: "Prompt Templates", Icon: BookMarked, to: TEMPLATES_PATH },
      { id: "new-thing", label: "New thing", Icon: Plug, to: "https://example.test/x" },
    ],
  },
];
"""


def test_red_a_new_child_and_hq_views_are_found_and_reported_missing():
    live = nav_entries(_TS)
    assert live == {
        "section:ask",
        "section:mission",
        "section:library",
        "sub:hq",
        "sub:missions",
        "sub:templates",
        "sub:new-thing",
    }
    mapped = {"section:ask", "section:mission", "section:library", "sub:hq", "sub:templates"}
    mapped |= {"sub:gone"}
    missing, stale = _diff(live, mapped)
    assert missing == ["sub:missions", "sub:new-thing"]
    assert stale == ["sub:gone"]


def test_red_a_new_hq_group_is_reported_missing():
    md = (
        "## The ledger\n\n#### Sessions\n| a |\n#### Brand new\n| b |\n\n"
        "## Never\n#### Not a group\n"
    )
    assert hq_groups(md) == {"Sessions", "Brand new"}
    assert _diff(hq_groups(md), {"Sessions"}) == (["Brand new"], [])


def test_red_malformed_rows_are_reported():
    rows = ledger_block(
        "```docs-nav\n"
        "a  ->  guide/hq\n"
        "b  ->  gap #12, guide/hq\n"
        "c  ->  gap 12\n"
        "d  ->  Guide/HQ.md\n"
        "e  ->  gap #0\n"
        "```",
        "docs-nav",
    )
    bad = row_problems(rows)
    assert [b.split(":")[0] for b in bad] == ["b", "c", "d", "e"]


def test_red_missing_unlisted_and_stub_pages_are_reported(tmp_path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "hq.md").write_text("# HQ\n")
    (tmp_path / "guide" / "hidden.md").write_text("# Not in the sidebar\n")
    (tmp_path / "guide" / "old.md").write_text("---\nsearch: false\n---\n\n# This page moved\n")
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "index.md").write_text("# Reference\n")
    (tmp_path / "index.md").write_text("# Home\n")
    (tmp_path / "NOTES.md").write_text("# excluded\n")
    published = published_pages(tmp_path, {"NOTES.md"})
    assert published == {
        "guide/hq": False,
        "guide/hidden": False,
        "guide/old": True,
        "reference/": False,
    }
    sidebar = {"/guide/hq", "/reference/"}
    rows = {
        "ok": ["guide/hq", "reference/"],
        "gap": ["gap #7"],
        "missing": ["guide/nope"],
        "unlisted": ["guide/hidden"],
        "stub": ["guide/old"],
    }
    bad = page_problems(rows, tmp_path, sidebar, published)
    assert [b.split(":")[0] for b in bad] == ["missing", "unlisted", "stub"]
    assert orphans(published, sidebar) == ["guide/hidden"]
