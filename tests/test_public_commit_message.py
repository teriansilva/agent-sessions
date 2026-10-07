"""`scripts/public-commit-message`: the mirror commit's public message (#1326).

Driven offline: a throwaway repo supplies the commit, `--pr-json` stands in for the forge's
`/pulls/<n>` response, and the real `check-public-snapshot` is the gate — so a denylist change
is exercised here too, not mocked away.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# Denylisted tokens, assembled at runtime: this file is part of the public snapshot, whose gate
# scans for exactly these strings.
INTERNAL_HOST = "mb-" + "infrabot"
INTERNAL_REPO = "infrastructure" + "-docs"

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/public-commit-message"

BODY = """## Summary
- The panel moves onto the type scale.
- A linked note (see [design](https://example.org/design)) is dropped whole.
- Fixes the overlap from #1201 (#1202 follow-up).
  - nested detail stays indented
- After Hermes' issue review the spec also covers the armed row.

Closes #1316.

Before | after:
![desktop](https://forge.example/attachments/abc)

## Security impact
None: presentation only — SECURITY-SECTION-MARKER.

## Test plan
- [x] TEST-PLAN-MARKER

## Session
<!-- agent-session-pr: engine=claude id=abc -->
Checkout: `~/agentwork/agent-sessions/x`
"""


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "f").write_text("x\n")

    def commit(subject: str) -> str:
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(r),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@e",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                subject,
            ],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()

    return r, commit


def _run(repo_dir: Path, commit: str, pr: dict | None, tmp_path: Path):
    args = [str(SCRIPT), commit]
    if pr is not None:
        f = tmp_path / "pr.json"
        f.write_text(json.dumps(pr))
        args += ["--pr-json", str(f)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    return subprocess.run(args, cwd=repo_dir, capture_output=True, text=True, env=env)


def _pr(commit: str, **kw) -> dict:
    return {
        "merged": True,
        "merge_commit_sha": commit,
        "title": "feat(web): a panel",
        "body": BODY,
        **kw,
    }


def test_title_and_scrubbed_summary(repo, tmp_path):
    r, commit = repo
    c = commit("feat(web): a panel (#1318)")
    out = _run(r, c, _pr(c), tmp_path)
    assert out.returncode == 0, out.stderr
    msg = out.stdout
    assert msg.startswith(
        "feat(web): a panel\n\n- The panel moves onto the type scale.\n- Fixes the overlap from."
    )
    assert "  - nested detail stays indented" in msg
    # only the Summary section, and none of its private residue
    for leak in (
        "SECURITY-SECTION-MARKER",
        "TEST-PLAN-MARKER",
        "agentwork",
        "agent-session",
        "Hermes",
        "Closes",
        "#13",
        "#12",
        "http",
        "attachments",
        "Before | after",
        "linked note",
        "example",
    ):
        assert leak not in msg, leak


def test_denylisted_summary_falls_back_to_the_title(repo, tmp_path):
    r, commit = repo
    c = commit("feat(web): a panel (#1318)")
    pr = _pr(c, body=f"## Summary\n- deployed to {INTERNAL_HOST} for a check\n")
    out = _run(r, c, pr, tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "feat(web): a panel\n"


def test_denylisted_title_falls_back_to_the_generic_message(repo, tmp_path):
    r, commit = repo
    c = commit(f"chore: sync from {INTERNAL_REPO}@5085d9f (#1322)")
    out = _run(r, c, _pr(c, title=f"chore: sync from {INTERNAL_REPO}@5085d9f", body=""), tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "publish: snapshot of main\n"


def test_a_pr_not_merged_as_this_commit_is_ignored(repo, tmp_path):
    """`(#N)` may name an issue, or a PR merged as another commit — its body is not ours."""
    r, commit = repo
    c = commit("feat(app): thing (#1191) (#1306)")
    out = _run(r, c, _pr("0" * 40, title="something else"), tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "feat(app): thing\n"


def test_no_forge_access_degrades_to_the_subject(repo, tmp_path):
    r, commit = repo
    c = commit("fix(web): x (#7)")
    out = _run(r, c, None, tmp_path)  # no GITHUB_* env: the fetch cannot happen
    assert out.returncode == 0, out.stderr
    assert out.stdout == "fix(web): x\n"
    assert "could not read PR #7" in out.stderr


def test_a_direct_push_uses_its_subject(repo, tmp_path):
    r, commit = repo
    c = commit("fix(release): avoid changelog SIGPIPE")
    out = _run(r, c, None, tmp_path)
    assert out.stdout == "fix(release): avoid changelog SIGPIPE\n"


# ---- titles get the same scrub (Hermes on #1326) ----


# Hermes on #1327: locations that are not http(s), and a link whose target holds parentheses.
# Synthetic, and each one must not reach any output — title, subject fallback or Summary.
PRIVATE_LOCATIONS = (
    "ftp://vault.example.invalid/private/report",
    "www.vault.example.invalid/private/report",
    "[details](https://vault.example.invalid/path_(v1)?key=example-sensitive-value)",
    "<https://vault.example.invalid/x>",
    "ssh://git@vault.example.invalid:2222/r.git",
    "ops@vault.corp.lan",
    "10.20.30.40",
    "vault.corp.lan",
    # round 2: IPv6 in every form, scheme-relative, relative links, reference definitions,
    # absolute paths, and look-alike characters
    "//[fd00::1]/private/report",
    "fd00::1",
    "2001:db8:0:0:0:0:0:1",
    "[report]: /private/report",
    '<a href="/private/report">report</a>',
    "/srv/private/report",
    "vault\uff0ecorp\uff1a8080",
)


@pytest.mark.parametrize("loc", PRIVATE_LOCATIONS)
def test_a_pr_title_with_a_location_falls_back_to_the_generic_message(repo, tmp_path, loc):
    r, commit = repo
    c = commit("feat(web): x (#9)")
    out = _run(r, c, _pr(c, title=f"feat(web): see {loc}", body="## Summary\n- fine\n"), tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "publish: snapshot of main\n"


@pytest.mark.parametrize("loc", PRIVATE_LOCATIONS)
def test_the_subject_fallback_with_a_location_is_dropped_when_the_api_fails(repo, tmp_path, loc):
    r, commit = repo
    c = commit(f"fix(web): per {loc} (#7)")
    out = _run(r, c, None, tmp_path)  # no forge access
    assert out.returncode == 0, out.stderr
    assert out.stdout == "publish: snapshot of main\n"


@pytest.mark.parametrize("loc", PRIVATE_LOCATIONS)
def test_a_summary_line_with_a_location_is_dropped_whole(repo, tmp_path, loc):
    r, commit = repo
    c = commit("feat(web): x (#9)")
    body = f"## Summary\n- kept line\n- details at {loc} for the run\n- also kept\n"
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert out.stdout == "feat(web): x\n\n- kept line\n- also kept\n"


def test_ordinary_summary_prose_survives_the_frame(repo, tmp_path):
    """The frame is strict; pin that the prose real PRs use still gets through."""
    r, commit = repo
    c = commit("feat(web): x (#9)")
    lines = [
        "- `Open` fills `--accent`/`--on-accent` on hover; ✕ sits apart; ≥44px at ≤800px.",
        "- Two-tier header: `NOTIFICATIONS ● N unread`, then `Clear all? Yes / Cancel`.",
        "  - journal its inode, then publish; the record keeps only name → inode.",
    ]
    body = "## Summary\n" + "\n".join(lines) + "\n"
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert out.stdout == "feat(web): x\n\n" + "\n".join(lines) + "\n"


def test_file_names_are_not_mistaken_for_hosts(repo, tmp_path):
    r, commit = repo
    c = commit("feat(web): x (#9)")
    body = "## Summary\n- `web/src/app/origins.test.ts`, `docs/design.md` and `install.sh`\n"
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert "origins.test.ts" in out.stdout and "install.sh" in out.stdout


def test_the_subject_fallback_is_tidied_when_the_api_fails(repo, tmp_path):
    r, commit = repo
    c = commit("fix(web): a fix (#7) <!-- marker -->")
    out = _run(r, c, None, tmp_path)
    assert out.stdout == "fix(web): a fix\n"


def test_a_title_carrying_chatter_or_an_image_falls_back_to_the_generic_message(repo, tmp_path):
    r, commit = repo
    for title in ("fix: apply Hermes round 3", "feat: shot ![x](https://a.example/attachments/1)"):
        c = commit(title + " (#5)")
        out = _run(r, c, _pr(c, title=title, body=""), tmp_path)
        assert out.stdout == "publish: snapshot of main\n", title


# ---- only the body's own Summary section (Hermes on #1327, round 3) ----

SENTINEL = "PRIVATE-PLAN-SENTINEL"
FENCE = "`" * 3


@pytest.mark.parametrize(
    "body",
    [
        # a fenced example quoting a Summary under another section
        f"## Test plan\n{FENCE}md\n## Summary\n- {SENTINEL}\n{FENCE}\n",
        f"## Summary\n- kept\n\n## Test plan\n{FENCE}\n## Summary\n- {SENTINEL}\n{FENCE}\n",
        # round 4: section ends at any indent — spaces, a tab, a sub-heading
        f"## Summary\n- kept\n\n   ## Test plan\n- {SENTINEL}\n",
        f"## Summary\n- kept\n\t## Test plan\n- {SENTINEL}\n",
        f"## Summary\n- kept\n### Test plan\n- {SENTINEL}\n",
        # a second Summary-like line anywhere, or a fence before the Summary
        f"## Summary\n- {SENTINEL}\n\n## Notes\n  ## summary\n",
        f"{FENCE}\nx\n{FENCE}\n## Summary\n- {SENTINEL}\n",
        # round 5: anything before the heading — an HTML comment hiding it, a preamble
        f"<!--\n## Summary\n- {SENTINEL}\n-->\n## Test plan\n- t\n",
        f"intro line\n\n## Summary\n- {SENTINEL}\n",
        # only the template's exact heading
        f"### Summary\n- {SENTINEL}\n",
        f"## Summary of it\n- {SENTINEL}\n",
        # section ends this walk does not follow: setext and HTML headings, a fence inside
        f"## Summary\n- kept\n\nTest plan\n---------\n- {SENTINEL}\n",
        f"## Summary\n- kept\n<h2>Test plan</h2>\n- {SENTINEL}\n",
        f"## Summary\n- kept\n{FENCE}\n- {SENTINEL}\n{FENCE}\n",
        # an unterminated fence
        f"{FENCE}\n## Summary\n- {SENTINEL}\n",
    ],
)
def test_text_outside_the_real_summary_never_reaches_the_message(repo, tmp_path, body):
    r, commit = repo
    c = commit("feat(web): x (#9)")
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert out.returncode == 0, out.stderr
    assert SENTINEL not in out.stdout
    assert out.stdout.startswith("feat(web): x\n")


def test_anything_but_the_template_shape_publishes_the_title_alone(repo, tmp_path):
    """A quoted Summary before the real one no longer gets a second look: two Summary-like
    lines are ambiguous, and ambiguity costs the summary, never the boundary."""
    r, commit = repo
    c = commit("feat(web): x (#9)")
    body = f"{FENCE}\n## Summary\n- {SENTINEL}\n{FENCE}\n## Summary\n- the real one\n"
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert out.stdout == "feat(web): x\n"


def test_the_template_shape_publishes_its_summary(repo, tmp_path):
    r, commit = repo
    c = commit("feat(web): x (#9)")
    body = (
        "## Summary\n- one\n  - nested\n- two\n\nCloses #4.\n\n"
        "## Security impact\nnone\n\n## Test plan\n- [x] t\n"
    )
    out = _run(r, c, _pr(c, title="feat(web): x", body=body), tmp_path)
    assert out.stdout == "feat(web): x\n\n- one\n  - nested\n- two\n"
