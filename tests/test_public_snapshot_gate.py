"""The pre-publish denylist gate (`scripts/check-public-snapshot`).

Two surfaces now go public: the GitHub mirror (a filtered `git archive` snapshot) and the docs
site's *generated HTML* (#828/#829). Both are scanned by the same `scan_tree` implementation
inside the script, because the alternative — a second copy of the regex in the docs workflow —
rots, and the rotted copy is always the one guarding the newer surface.

These tests exercise the `scan-tree` entry point directly with planted tokens, so a refactor that
loosens the scan fails here rather than at publish time.

Note: no denied token appears *literally* in this file. The gate scans `tests/` too (it is not
export-ignored), so a literal would make this test file the leak it is testing for. Every fixture
token is assembled at runtime from fragments that individually match nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "check-public-snapshot"

# The gate script is `export-ignore`d, so it is absent from the public snapshot; a user running
# the test suite from that snapshot has nothing to test.
pytestmark = pytest.mark.skipif(
    not GATE.exists(), reason="check-public-snapshot is not part of the public snapshot"
)

# Assembled so the literal never appears in this file. `infrabot` on its own is not denied; the
# hyphenated host is.
DENIED = "mb-" + "infrabot"
# Intentionally public — the advertised install host, present in the README today.
ALLOWED = "battlelab.superstatus.io"


def scan(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GATE), "scan-tree", str(tree)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )


def test_clean_tree_passes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>BattleLab docs</h1>\n")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text(":root{--accent:#ffb000}\n")
    r = scan(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


@pytest.mark.parametrize(
    "token", [DENIED, "Ca" + "yoo", "post" + "pilot", "ro-" + "infrastructure"]
)
def test_denied_token_in_contents_fails(tmp_path: Path, token: str) -> None:
    """The whole point: a generated page that names an internal host must not ship."""
    (tmp_path / "index.html").write_text(f"<p>built on {token}</p>\n")
    r = scan(tmp_path)
    assert r.returncode == 1
    assert "LEAK" in r.stderr


@pytest.mark.parametrize(
    "token", [DENIED, "Ca" + "yoo", "post" + "pilot", "ro-" + "infrastructure"]
)
def test_denied_token_in_pathname_fails(tmp_path: Path, token: str) -> None:
    """A file whose NAME carries the token leaks it via the URL even if its bytes are clean."""
    (tmp_path / f"{token}.html").write_text("<p>clean body</p>\n")
    r = scan(tmp_path)
    assert r.returncode == 1
    assert "pathnames" in r.stderr


def test_allowed_token_alone_passes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(f"<code>curl -fsSL https://{ALLOWED}/install.sh</code>\n")
    assert scan(tmp_path).returncode == 0


def test_allowed_token_cannot_shield_a_denied_one_on_the_same_line(tmp_path: Path) -> None:
    """Masking, not line-dropping.

    The allowlist removes the exact allowed substrings and re-scans what is left; a line-based
    filter would let an allowed token act as a free pass for anything sharing its line.
    """
    (tmp_path / "index.html").write_text(f"<p>{ALLOWED} is served from {DENIED}</p>\n")
    r = scan(tmp_path)
    assert r.returncode == 1
    assert "LEAK" in r.stderr


def test_denied_token_inside_a_binary_asset_fails(tmp_path: Path) -> None:
    """Binary assets are scanned in text mode on purpose — a font or image can carry a string."""
    (tmp_path / "logo.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00" + DENIED.encode() + b"\x00\xff"
    )
    assert scan(tmp_path).returncode == 1


def test_a_scan_that_cannot_run_is_not_a_pass(tmp_path: Path) -> None:
    """The fail-open trap: a broken scanner must never certify a tree as clean.

    `grep` exits 0 for matches, 1 for none and >1 for an error. Ending the pipeline in `|| true`
    reads as "tolerate no matches" but swallows every failure — an unreadable file, a rejected
    regex, a missing binary — and all of them produce empty output that then gets reported as
    "no leaks found". Here `grep` is replaced with a shim that always exits 2; the scan must
    refuse to certify rather than print OK.
    """
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "index.html").write_text("<h1>clean</h1>\n")
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "grep").write_text("#!/bin/sh\nexit 2\n")
    (shim / "grep").chmod(0o755)

    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    r = subprocess.run(
        [str(GATE), "scan-tree", str(tmp_path / "tree")],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode != 0, "a scan that could not run must not exit 0"
    assert "OK" not in r.stdout
    assert "FAILED" in r.stderr


def test_missing_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """Fail closed: a typo'd path must never read as 'nothing found, ship it'."""
    r = scan(tmp_path / "does-not-exist")
    assert r.returncode == 2


def test_snapshot_caller_still_passes_on_head() -> None:
    """The pre-existing caller keeps working after the refactor (this is what CI runs)."""
    r = subprocess.run([str(GATE), "HEAD"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
