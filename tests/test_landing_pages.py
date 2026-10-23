"""battlelabos.com's sub-pages in NIGHTJAR (#1597, epic #1531 Phase 12b).

- ONE palette: landing/nightjar.css is generated from web/src/tokens.css, and the primitives
  source carries no colour of its own.
- The legal wording does not change with a restyle: the text in [data-legal-text] equals the text
  of the pre-NIGHTJAR panel, frozen in tests/fixtures/landing/.
- Every sub-page links the shared stylesheet and the site footer; the classification line is gone.
- deploy-landing.yml syncs and byte-verifies *.css, or the stylesheet would 404 live.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
LANDING = REPO / "landing"
SUBPAGES = ["legal.html", "privacy.html", "404.html"]

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from landing_text import legal_text  # noqa: E402


def test_the_generated_stylesheet_is_current():
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "gen-landing-css"), "--check"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_the_stylesheet_carries_the_apps_dark_tokens_verbatim():
    tokens = (REPO / "web" / "src" / "tokens.css").read_text()
    root = re.search(r"^:root \{\n.*?^\}\n", tokens, flags=re.S | re.M).group(0)
    assert root in (LANDING / "nightjar.css").read_text()


def test_the_primitives_carry_no_colour_of_their_own():
    css = (REPO / "scripts" / "landing-primitives.css").read_text()
    code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", code), "a hex colour: use a token"
    assert not re.findall(r"\b(?:rgba?|hsla?)\(", code), "a literal colour function: use a token"


@pytest.mark.parametrize("page", ["legal", "privacy"])
def test_the_legal_wording_is_unchanged(page):
    want = (REPO / "tests" / "fixtures" / "landing" / f"{page}.txt").read_text().strip()
    got = legal_text((LANDING / f"{page}.html").read_text())
    assert got == want


@pytest.mark.parametrize("page", SUBPAGES)
def test_every_sub_page_uses_the_shared_stylesheet_and_the_site_footer(page):
    html = (LANDING / page).read_text()
    assert '<link rel="stylesheet" href="/nightjar.css" />' in html
    assert "<style" not in html, "one stylesheet: no inline copy of the palette"
    assert "UNCLASSIFIED" not in html, "the classification line is gone (Phase 0 decision 4)"
    foot = re.search(r'<footer class="sfoot">.*?</footer>', html, flags=re.S)
    assert foot, "the site footer"
    for href in ("/legal.html", "/privacy.html", "https://docs.battlelabos.com/", "/llms.txt"):
        assert f'href="{href}"' in foot.group(0), href


def test_the_404_is_not_indexed_and_links_every_way_out():
    html = (LANDING / "404.html").read_text()
    assert '<meta name="robots" content="noindex" />' in html
    for href in ('href="/"', 'href="https://docs.battlelabos.com/"', 'href="/connect"'):
        assert href in html


def test_the_privacy_page_keeps_every_consent_hook():
    html = (LANDING / "privacy.html").read_text()
    # No website id on the tag: the privacy page never loads a tracker.
    assert '<script defer src="/consent.js"></script>' in html
    assert 'id="consent-settings"' in html and "data-consent-state" in html
    assert 'data-consent="denied"' in html and 'data-consent="granted"' in html


def test_deploy_landing_syncs_and_verifies_the_stylesheet():
    wf = (REPO / ".forgejo" / "workflows" / "deploy-landing.yml").read_text()
    assert '- "landing/*.css"' in wf, "the path filter"
    assert wf.count("landing/*.css landing/*.webp") == 2, "the sync loop and the byte-verify loop"
