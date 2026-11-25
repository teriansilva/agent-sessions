"""The installed manual's corpus and index (#2249 P2, `docs/invariants/hq-manual.md`)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agent_sessions import docs_index

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "docs" / "site"


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def manual(tmp_path, monkeypatch):
    """A small release tree: `<tmp>/src` holds `docs/site` and the files pages transclude."""
    root = (tmp_path / "src").resolve()
    _write(
        root,
        "docs/site/guide/home-free.md",
        "---\ntitle: Home Free\n---\n\n# Home Free\n\nReach BattleLab from anywhere.\n\n"
        "## Pairing\n\nOpen **Settings → Home Free** and scan the pairing code with your phone.\n\n"
        "::: tip Keep it private\nThe code works once.\n:::\n\n"
        "## Pairing\n\nA second section with the same heading.\n\n"
        "### Custom anchor {#relay-key}\n\nThe relay never sees your key.\n",
    )
    _write(
        root,
        "docs/site/guide/playbooks.md",
        "# Playbooks\n\n## Flows\n\nA playbook flow is a list of steps, each with a role.\n\n"
        '```bash\n# not a heading\nbattlelab flows list\n```\n\n<Badge text="beta" />\n',
    )
    _write(
        root,
        "docs/site/reference/index.md",
        "---\nlinkBase: /docs/\n---\n\n<!--@include: ../../reference.md-->\n",
    )
    _write(
        root,
        "docs/reference.md",
        "# Reference\n\n## Environment\n\nAGENT_SESSIONS_HOME sets where state lives.\n",
    )
    _write(
        root,
        "docs/site/guide/old.md",
        "---\nsearch: false\n---\n\n# This page moved\n\nPairing moved.\n",
    )
    _write(root, "docs/site/SECURITY-NOTES.md", "# Notes\n\nPairing advisory.\n")
    _write(root, "docs/site/index.md", "# Home\n\nPairing hero text.\n")
    monkeypatch.setattr(docs_index, "_ROOT", root)
    monkeypatch.setattr(docs_index, "_INDEX", None)
    return root


# ---- the root ------------------------------------------------------------------------------------


def _release(base: Path, name: str, marker: str) -> Path:
    rel = base / name
    _write(rel, "src/docs/site/guide/x.md", f"# X\n\n## Marker\n\n{marker}\n")
    (rel / "venv").mkdir()
    return rel


def test_the_root_is_the_interpreters_own_release_even_after_current_switches(
    tmp_path, monkeypatch
):
    one = _release(tmp_path, "rel-1", "alphaword")
    two = _release(tmp_path, "rel-2", "betaword")
    current = tmp_path / "current"
    current.symlink_to(one)
    pinned = docs_index.resolve_root(prefix=str(current / "venv"), env="")
    assert pinned == (one / "src").resolve()
    # An update switches `current` BEFORE the first docs lookup: the pin does not follow it.
    current.unlink()
    current.symlink_to(two)
    monkeypatch.setattr(docs_index, "_ROOT", pinned)
    monkeypatch.setattr(docs_index, "_INDEX", None)
    assert docs_index.search("alphaword")
    assert docs_index.search("betaword") == []


def test_a_source_checkout_falls_back_to_this_repo_and_nothing_means_not_installed(
    tmp_path, monkeypatch
):
    # No release beside the interpreter: the package's own repo (this checkout) is the manual.
    assert docs_index.resolve_root(prefix=str(tmp_path / "venv"), env="") == REPO.resolve()
    monkeypatch.setattr(docs_index, "_ROOT", None)
    monkeypatch.setattr(docs_index, "_INDEX", None)
    assert docs_index.search("pairing") is None
    assert docs_index.read("guide/hq#:1") is None


def test_the_env_override_is_used_only_when_nothing_else_has_a_manual(tmp_path, monkeypatch):
    other = _release(tmp_path, "elsewhere", "w")
    monkeypatch.setattr(docs_index, "__file__", str(tmp_path / "a" / "b" / "c.py"))
    got = docs_index.resolve_root(prefix=str(tmp_path / "venv"), env=str(other / "src"))
    assert got == (other / "src").resolve()
    assert docs_index.resolve_root(prefix=str(tmp_path / "venv"), env="") is None


# ---- the corpus ----------------------------------------------------------------------------------


def test_published_pages_only_no_stub_no_exclude_no_home(manual):
    assert [p for p, _ in docs_index.pages(manual)] == [
        "guide/home-free",
        "guide/old",  # walked, then skipped at build as a stub
        "guide/playbooks",
        "reference/",
    ]
    pages = {c.page for c in docs_index.build(manual)}
    assert pages == {"guide/home-free", "guide/playbooks", "reference/"}


def test_a_transcluded_wrapper_is_searchable_and_readable_as_its_targets_text(manual):
    hits = docs_index.search("AGENT_SESSIONS_HOME environment")
    assert hits and hits[0]["page"] == "reference/"
    assert hits[0]["link"] == "https://docs.battlelabos.com/reference/#environment"
    assert "sets where state lives" in docs_index.read(hits[0]["id"]).text


@pytest.mark.parametrize(
    "target, why",
    [
        ("../../../../outside.md", "leaves the root"),
        ("../../notlisted.md", "not in INCLUDES"),
        ("../../reference.md#region", "region"),
    ],
)
def test_an_include_outside_the_allowlist_or_the_root_is_dropped(manual, target, why):
    _write(manual.parent, "outside.md", "SECRETOUTSIDE\n")
    _write(manual, "docs/notlisted.md", "SECRETUNLISTED\n")
    _write(manual, "docs/site/guide/wrap.md", f"# Wrap\n\n<!--@include: {target}-->\n\nkept\n")
    text = " ".join(c.text for c in docs_index.build(manual) if c.page == "guide/wrap")
    assert "SECRET" not in text and "kept" in text and "Reference" not in text, why


def test_an_include_through_a_symlink_is_dropped(manual, tmp_path):
    secret = _write(tmp_path, "elsewhere/secret.md", "SECRETLINKED\n")
    (manual / "docs" / "reference.md").unlink()
    os.symlink(secret, manual / "docs" / "reference.md")  # an allowed NAME, a foreign file
    assert not any("SECRETLINKED" in c.text for c in docs_index.build(manual))


def test_a_page_reached_through_a_symlinked_folder_is_not_the_manuals(manual, tmp_path):
    _write(tmp_path, "foreign/evil.md", "# Evil\n\nSECRETFOLDER\n")
    os.symlink(tmp_path / "foreign", manual / "docs" / "site" / "guide" / "linked")
    assert not any("SECRETFOLDER" in c.text for c in docs_index.build(manual))


def test_includes_are_pinned_to_the_directives_in_docs_site_both_ways():
    found = set()
    for f in SITE.rglob("*.md"):
        if "node_modules" in f.parts:
            continue
        for target in re.findall(r"<!--\s*@include:\s*([^\s>]+?)\s*-->", f.read_text()):
            found.add(Path(os.path.normpath(f.parent / target)).relative_to(REPO).as_posix())
    assert found == set(docs_index.INCLUDES)


def test_exclude_and_folders_match_the_vitepress_config():
    config = (SITE / ".vitepress" / "config.ts").read_text()
    excl = re.search(r"srcExclude:\s*\[(.*?)\]", config, re.S).group(1)
    assert set(re.findall(r'"([^"]+)"', excl)) == set(docs_index.EXCLUDE)
    sidebar = re.search(r"\bsidebar:\s*\[(.*?)\n    \],", config, re.S).group(1)
    folders = {
        link.strip("/").split("/")[0] + "/" for link in re.findall(r'link:\s*"(/[^"]*)"', sidebar)
    }
    assert folders == set(docs_index.FOLDERS)


def test_the_real_manual_builds_and_every_wrapper_has_its_targets_text():
    chunks = docs_index.build(REPO.resolve())
    by_page: dict[str, str] = {}
    for c in chunks:
        by_page[c.page] = by_page.get(c.page, "") + c.text
    for page in (
        "reference/",
        "security/",
        "contributing/",
        "start/install",
        "security/relay-crypto",
    ):
        assert len(by_page.get(page, "")) > 500, page
    assert "Docs are part of done" in by_page["contributing/"]
    assert all(len(c.text) <= docs_index.CHUNK_MAX for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)


# ---- markdown → sections → chunks ----------------------------------------------------------------


def test_anchors_follow_vitepress_duplicates_and_custom_ids(manual):
    chunks = [c for c in docs_index.build(manual) if c.page == "guide/home-free"]
    assert [(c.heading, c.anchor) for c in chunks] == [
        ("Home Free", ""),
        ("Pairing", "pairing"),
        ("Pairing", "pairing-1"),
        ("Custom anchor", "relay-key"),
    ]
    assert chunks[0].title == "Home Free"
    assert chunks[3].link == "https://docs.battlelabos.com/guide/home-free#relay-key"


def test_containers_tags_and_frontmatter_are_stripped_code_is_kept(manual):
    text = "\n".join(c.text for c in docs_index.build(manual))
    assert ":::" not in text and "title: Home Free" not in text and "<Badge" not in text
    assert "Keep it private" in text and "The code works once." in text
    assert "# not a heading" in text and "battlelab flows list" in text  # fenced, not a section
    # The fenced `# not a heading` opened no section; an empty preamble makes no chunk.
    assert {c.heading for c in docs_index.build(manual) if c.page == "guide/playbooks"} == {"Flows"}


def test_an_oversize_paragraph_is_split_and_a_long_section_reads_in_order(manual):
    sentence = "Pairing step explained in detail. "
    long_para = sentence * 400  # ~13,600 chars, one paragraph
    no_breaks = "z" * 7000  # no sentence boundary: hard cut
    _write(
        manual,
        "docs/site/guide/long.md",
        f"# Long\n\n## Big\n\n{long_para}\n\n{no_breaks}\n\n## After\n\ntail\n",
    )
    chunks = [c for c in docs_index.build(manual) if c.page == "guide/long"]
    big = [c for c in chunks if c.heading == "Big"]
    assert len(big) >= 4 and all(len(c.text) <= docs_index.CHUNK_MAX for c in chunks)
    assert [c.part for c in big] == list(range(1, len(big) + 1))
    assert all(c.parts == len(big) for c in big)
    # `next` walks the whole section before the next one, and ends at the page's end.
    walk = [chunks[0]]
    while walk[-1].next:
        walk.append(next(c for c in chunks if c.id == walk[-1].next))
    assert walk == chunks and walk[-1].heading == "After" and walk[-1].next is None
    assert "".join(c.text for c in big).count("z") == 7000


# ---- search --------------------------------------------------------------------------------------


def test_search_ranks_the_right_section_with_a_bounded_excerpt(manual):
    hits = docs_index.search("how do I pair home free with my phone")
    assert hits[0]["page"] == "guide/home-free" and hits[0]["heading"] == "Pairing"
    assert all(len(h["excerpt"]) <= docs_index.EXCERPT_MAX for h in hits)
    assert len(hits) <= docs_index.SEARCH_MAX
    assert not any(h["page"] in ("guide/old", "SECURITY-NOTES", "") for h in hits)


@pytest.mark.parametrize(
    "query", ['"', "NEAR(pairing", "pair*", "title:pairing", "AND OR NOT", "", "   ", "the a of"]
)
def test_malformed_fts_input_never_raises(manual, query):
    assert isinstance(docs_index.search(query), list)


def test_an_unknown_id_reads_nothing(manual):
    for bad in ("", "guide/home-free", "../../etc/passwd", "guide/home-free#pairing:99", "x" * 500):
        assert docs_index.read(bad) is None


def test_the_index_rebuilds_when_the_tree_changes(manual):
    assert docs_index.search("zeppelin") == []
    p = manual / "docs" / "site" / "guide" / "playbooks.md"
    p.write_text(p.read_text() + "\n## Zeppelin\n\nA zeppelin section.\n")
    os.utime(p, ns=(1, 1))  # a different mtime even on a coarse clock
    assert docs_index.search("zeppelin")[0]["heading"] == "Zeppelin"


def test_a_longer_fence_holds_shorter_ones_and_no_heading_inside_opens_a_section(manual):
    # Found on the real manual (guide/web-research): a ```` block that shows a ``` block whose
    # first line is `## HQ summary`. Toggling on any fence line made that a section.
    _write(
        manual,
        "docs/site/guide/nested.md",
        "# Nested\n\n## Outside\n\nbefore\n\n````markdown\n## Not a section\n\n```\n## Nor this\n"
        "```\n\nstill code\n````\n\nafter\n\n~~~\n## Tilde code\n```\n~~~\n\n## Real\n\nend\n",
    )
    chunks = [c for c in docs_index.build(manual) if c.page == "guide/nested"]
    assert [c.heading for c in chunks] == ["Outside", "Real"]
    assert "## Not a section" in chunks[0].text and "still code" in chunks[0].text
    assert "## Tilde code" in chunks[0].text and "after" in chunks[0].text


def test_tags_go_but_angle_brackets_in_inline_code_stay(manual):
    _write(
        manual,
        "docs/site/guide/cli.md",
        "# CLI\n\n## CLI — `agent-sessions <subcommand>`\n\nRun `agent-sessions <subcommand>` "
        '<span class="x">now</span>.\n',
    )
    (c,) = [c for c in docs_index.build(manual) if c.page == "guide/cli"]
    # VitePress' own id for this heading, read off a real build of the manual.
    assert c.anchor == "cli-—-agent-sessions-subcommand"
    assert c.text == "Run `agent-sessions <subcommand>` now."
