"""The two CI helpers of #1244: `pr_scope.py` (which backend checks a PR needs) and
`sibling_watch.py` (stop a long check when a sibling check for the same commit has failed).

Both run on the self-hosted runner, where a wrong answer is expensive either way: scoping too
narrowly lets a break through unreviewed, and a watchdog that misreads a status kills a healthy
40-minute run. So the rules they exist for are pinned here: every doubt runs the full suite, and
only a positive reading of a failed sibling stops a job.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".forgejo" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


scope = _load("pr_scope")
watch = _load("sibling_watch")


# --- pr_scope --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/agent_sessions/main.py",
        "tests/test_api.py",
        "pyproject.toml",
        "uv.lock",
        "install.sh",
        "uninstall.sh",
        "deploy/agent-sessions.service",
        "scripts/check-public-snapshot",
        # A PR must not be able to scope away its own checks by editing CI.
        ".forgejo/workflows/pr-validate.yml",
        ".forgejo/scripts/pr_scope.py",
    ],
)
def test_backend_packaging_and_ci_paths_need_the_full_suite(path):
    assert scope.needs_full(["web/src/App.tsx", path])
    mode, tests, _ = scope.decide(["web/src/App.tsx", path], Path("unused"))
    assert (mode, tests) == ("full", [])


@pytest.mark.parametrize(
    "path", ["web/src/App.tsx", "docs/site/guide/projects.md", "README.md", "landing/index.html"]
)
def test_web_docs_and_landing_do_not(path):
    assert not scope.needs_full([path])


def test_every_doubt_is_the_full_suite():
    # Unreadable list, an empty list: never "nothing to run".
    assert scope.decide(None, Path("unused"))[0] == "full"
    assert scope.decide([], Path("unused"))[0] == "full"


def _tests(tmp_path: Path, files: dict[str, str]) -> Path:
    d = tmp_path / "tests"
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


def test_a_web_only_diff_runs_the_tests_that_read_web_files(tmp_path):
    d = _tests(
        tmp_path,
        {
            "test_fixture_pin.py": 'WEB = ROOT / "web" / "src"',
            "test_slash_form.py": "open('web/src/sw.ts')",
            "test_by_basename.py": "check('roster.fixture.json')",
            "test_backend_only.py": "def test_x(): assert 1",
            # The appmode suite has its own workflow; never collected by pr-validate.
            "appmode/test_appmode_web.py": '"web"',
        },
    )
    mode, tests, _ = scope.decide(["web/src/test/roster.fixture.json"], d)
    assert mode == "subset"
    assert tests == [
        "tests/test_by_basename.py",
        "tests/test_fixture_pin.py",
        "tests/test_slash_form.py",
    ]


def test_a_docs_diff_picks_the_doc_pinning_tests_and_nothing_else(tmp_path):
    d = _tests(
        tmp_path,
        {
            "test_engine_docs.py": 'p = tmp / "docs/site/guide/engines.md"',
            "test_other.py": '"web"',
        },
    )
    assert scope.decide(["docs/site/guide/engines.md"], d) == (
        "subset",
        ["tests/test_engine_docs.py"],
        "1 non-backend file(s); 1 test file(s) mention them",
    )


def test_a_diff_no_test_mentions_runs_no_pytest(tmp_path):
    d = _tests(tmp_path, {"test_x.py": "def test_x(): pass"})
    assert scope.decide(["landing/privacy.html"], d)[0] == "none"


def test_short_basenames_are_not_used_as_tokens():
    # "a.md" would match half the suite; top-level dir tokens still apply.
    assert scope.tokens_for("docs/a.md") == {'"docs"', "'docs'", "docs/"}
    assert scope.tokens_for("README.md") == {"README.md"}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_file_list_is_paged_and_renames_count_both_paths(monkeypatch):
    pages = {
        1: [{"filename": f"web/f{i}.ts"} for i in range(scope.PAGE)],
        2: [{"filename": "src/new.py", "previous_filename": "src/old.py"}],
    }
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        page = int(req.full_url.rsplit("page=", 1)[1])
        return _Resp(json.dumps(pages.get(page, [])).encode())

    monkeypatch.setattr(scope.urllib.request, "urlopen", fake_urlopen)
    files = scope.changed_files("https://x/api", "7", "tok")
    assert len(files) == scope.PAGE + 2
    assert files[-2:] == ["src/new.py", "src/old.py"]
    # The token travels in a header, never in the URL.
    assert all(
        "tok" not in r.full_url and r.get_header("Authorization") == "token tok" for r in seen
    )


def test_main_fails_closed_when_the_api_is_down(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(scope.urllib.request, "urlopen", boom)
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("API", "https://x/api")
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("TOKEN", "tok")
    assert scope.main() == 0
    written = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert written["mode"] == "full"
    assert written["tests"] == ""


def test_the_real_tree_scopes_a_web_only_pr_to_its_pinning_tests():
    # Against THIS repo: a web file must pull in the tests that pin web files, and the scope
    # must stay a small fraction of the suite (the point of #1244).
    tests_dir = Path(__file__).resolve().parent
    mode, picked, _ = scope.decide(["web/src/test/roster.fixture.json"], tests_dir)
    assert mode == "subset"
    assert "tests/test_web_roster_fixture.py" in picked
    total = len([p for p in tests_dir.rglob("test_*.py") if "appmode" not in p.parts])
    assert len(picked) < total / 3


# --- sibling_watch ---------------------------------------------------------------------------

SELF = "web-ci / e2e (1) (pull_request)"


def _st(ctx, state, desc=""):
    return {"context": ctx, "status": state, "description": desc}


def test_a_failed_sibling_is_named():
    statuses = [_st(SELF, "pending"), _st("pr-validate / python (pull_request)", "failure")]
    assert watch.failed_sibling(statuses, SELF) == "pr-validate / python (pull_request)"
    assert watch.failed_sibling([_st("x", "error")], SELF) == "x"


@pytest.mark.parametrize(
    "statuses",
    [
        [_st(SELF, "failure")],  # our own context
        [_st("web-ci / e2e (2) (pull_request)", "failure", "Has been cancelled")],  # superseded run
        [_st("pr-visual / capture (workflow_dispatch)", "failure")],  # advisory
        [_st("a", "pending"), _st("b", "success")],
        [],
    ],
)
def test_what_is_not_a_sibling_failure(statuses):
    assert watch.failed_sibling(statuses, SELF) is None


def _fake_reader(monkeypatch, sequence):
    """Each status read pops the next item; an Exception item is raised."""
    items = list(sequence)

    def read(api, sha, token):
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(watch, "read_statuses", read)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        # ESRCH: the process was reaped between opening its stat file and reading it.
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_a_sibling_failure_stops_the_whole_process_group(monkeypatch, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    # A child that starts a grandchild and waits: pytest -n 8 and Playwright both look like this.
    child = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(60)"
        ),
    ]
    _fake_reader(monkeypatch, [[], [_st("pr-validate / python (pull_request)", "failure")]])
    t0 = time.monotonic()
    rc = watch.run(child, SELF, "api", "sha", "tok", interval=0.2)
    assert rc == watch.ABORTED
    assert time.monotonic() - t0 < 20
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the grandchild survived — the group was not stopped"


def test_a_clean_run_returns_the_commands_own_status(monkeypatch):
    _fake_reader(monkeypatch, [[_st("x", "pending")]])
    assert watch.run([sys.executable, "-c", "raise SystemExit(7)"], SELF, "a", "s", "t", 0.05) == 7
    assert watch.run([sys.executable, "-c", "pass"], SELF, "a", "s", "t", 0.05) == 0


def test_a_failed_status_read_never_stops_the_job(monkeypatch):
    _fake_reader(monkeypatch, [OSError("503")])
    rc = watch.run(
        [sys.executable, "-c", "import time; time.sleep(0.6)"], SELF, "a", "s", "t", 0.05
    )
    assert rc == 0


def test_without_credentials_the_command_runs_unwatched(monkeypatch):
    for k in ("API", "HEAD_SHA", "TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert watch.main(["--self", SELF, "--", sys.executable, "-c", "raise SystemExit(5)"]) == 5


def test_bad_usage_is_refused():
    assert watch.main(["--", "true"]) == 2
    assert watch.main(["--self", SELF, "true"]) == 2


def test_the_script_runs_as_a_command(tmp_path):
    # The workflow invokes it as `python3 .../sibling_watch.py`; exercise that path end to end.
    env = {k: v for k, v in os.environ.items() if k not in ("API", "HEAD_SHA", "TOKEN")}
    r = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "sibling_watch.py"),
            "--self",
            SELF,
            "--",
            sys.executable,
            "-c",
            "print('ran')",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert r.returncode == 0
    assert "ran" in r.stdout


# --- hardening from review 5433: output injection, paging, log injection ---------------------


def _runner_parse(text: str) -> dict[str, str]:
    """What act_runner does with $GITHUB_OUTPUT: line by line, `k=v`, last value wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_a_newline_in_a_changed_filename_cannot_scope_away_the_suite(monkeypatch, tmp_path):
    # The exploit from review 5433: a backend file whose NAME carries "\nmode=none\ntests=".
    evil = "src/x\nmode=none\ntests="

    def fake_urlopen(req, timeout):
        page = int(req.full_url.rsplit("page=", 1)[1])
        return _Resp(json.dumps([{"filename": evil}] if page == 1 else []).encode())

    monkeypatch.setattr(scope.urllib.request, "urlopen", fake_urlopen)
    out = tmp_path / "gh_output"
    for k, v in {
        "GITHUB_OUTPUT": str(out),
        "API": "https://x/api",
        "PR_NUMBER": "7",
        "TOKEN": "t",
    }.items():
        monkeypatch.setenv(k, v)
    assert scope.main() == 0
    text = out.read_text()
    assert len(text.splitlines()) == 3, text  # exactly mode / tests / reason — nothing injected
    parsed = _runner_parse(text)
    assert parsed["mode"] == "full"
    assert parsed["tests"] == ""


def test_any_control_character_in_a_path_fails_closed():
    for bad in ("web/a\nb.ts", "docs/a\rb.md", "web/a\x00b", "web/\x1bx"):
        mode, tests, reason = scope.decide([bad], Path("unused"))
        assert (mode, tests) == ("full", []), bad
        assert "control characters" in reason


def test_output_values_are_one_control_free_line():
    assert scope.output_value("a\nmode=none\r\ttests=x") == "a mode=none tests=x"
    assert "\n" not in scope.output_value("x\n" * 5)


def test_only_plain_test_paths_can_be_selected(tmp_path):
    d = _tests(tmp_path, {"test_ok.py": '"web"', "test_has space.py": '"web"'})
    assert scope.select_tests(["web/src/a.tsx"], d) == ["tests/test_ok.py"]


def test_paging_does_not_trust_a_short_page(monkeypatch):
    # An instance capping pages at 10 (below PAGE): the list must still be read to the end.
    pages = {
        1: [{"filename": f"web/a{i}.ts"} for i in range(10)],
        2: [{"filename": f"web/b{i}.ts"} for i in range(10)],
        3: [{"filename": "src/late.py"}],
    }

    def fake_urlopen(req, timeout):
        page = int(req.full_url.rsplit("page=", 1)[1])
        return _Resp(json.dumps(pages.get(page, [])).encode())

    monkeypatch.setattr(scope.urllib.request, "urlopen", fake_urlopen)
    files = scope.changed_files("https://x/api", "7", "tok")
    assert len(files) == 21 and files[-1] == "src/late.py"
    assert scope.decide(files, Path("unused"))[0] == "full"


def test_a_crafted_check_name_cannot_inject_a_runner_command(monkeypatch, capfd):
    _fake_reader(monkeypatch, [[_st("ci / x\n::add-mask::secret (pull_request)", "failure")]])
    rc = watch.run([sys.executable, "-c", "import time; time.sleep(30)"], SELF, "a", "s", "t", 0.05)
    assert rc == watch.ABORTED
    lines = capfd.readouterr().out.splitlines()
    assert not any(line.startswith("::add-mask::") for line in lines)
    assert any("aborted: sibling" in line for line in lines)
