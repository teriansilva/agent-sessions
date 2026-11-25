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


@pytest.fixture(autouse=True)
def _not_in_a_ci_event(monkeypatch):
    """The suite itself runs in CI, where the runner sets GITHUB_EVENT_NAME (`push` on main)."""
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)


# --- pr_scope --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "src/agent_sessions/__init__.py",
        "tests/conftest.py",
        "tests/fixtures/sample.json",
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


def test_a_killed_watcher_takes_the_whole_command_group_with_it(tmp_path):
    """A cancelled run SIGKILLs the step's process group, so the watcher dies before any handler
    of its own can run. The command leads a group of its own and used to live on under init: an
    e2e shard still waiting for a slot then took one for a run nobody was waiting on."""
    pids = tmp_path / "pids"
    child = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);"
        "import os;"
        f"open({str(pids)!r},'w').write(f'{{os.getppid()}} {{os.getpid()}} {{p.pid}}');"
        "time.sleep(120)"
    )
    # Credentials set, so it really watches; the address refuses at once and a failed read keeps
    # the command running, which is the state a waiting shard is in when its run is cancelled.
    env = {**os.environ, "API": "https://127.0.0.1:9", "HEAD_SHA": "sha", "TOKEN": "tok"}
    env["SIBLING_WATCH_INTERVAL"] = "600"
    watcher = subprocess.Popen(
        [str(sys.executable), str(SCRIPTS / "sibling_watch.py"), "--self", SELF, "--"]
        + [sys.executable, "-c", child],
        env=env,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    started: list[int] = []
    try:
        deadline = time.monotonic() + 20
        while not (pids.exists() and pids.read_text().count(" ") == 2):
            assert time.monotonic() < deadline, "the command never started"
            time.sleep(0.05)
        started = [int(p) for p in pids.read_text().split()]
        watcher.kill()  # what the runner's cancel does: no handler runs
        watcher.wait(timeout=10)
        deadline = time.monotonic() + 30
        while any(_alive(p) for p in started) and time.monotonic() < deadline:
            time.sleep(0.05)
        # The guard (the command's parent), the command and its grandchild: all of them.
        assert not any(_alive(p) for p in started), "the command outlived its killed watcher"
    finally:
        for p in started:  # only processes this test started
            if p > 1 and _alive(p):
                os.kill(p, 9)
        if watcher.poll() is None:
            watcher.kill()


def test_a_cancel_during_a_status_read_still_stops_the_group(monkeypatch, tmp_path):
    """The runner's SIGTERM can land while the status request is in flight. The broad catch of a
    failed read must not swallow it as one (review 6288)."""
    import signal

    pidfile = tmp_path / "grandchild.pid"
    child = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
            f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(60)"
        ),
    ]

    def read(api, sha, token):
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)  # the cancel, mid-request
        time.sleep(1)  # the handler runs here, inside the read
        return []

    monkeypatch.setattr(watch, "read_statuses", read)
    rc = watch.run(child, SELF, "api", "sha", "tok", interval=0.2)
    assert rc == 143, "the cancel was taken for a failed read and the command kept running"
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the grandchild survived the cancel"


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
        if req.full_url.endswith("/labels"):
            return _Resp(b"[]")
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
    # Exactly mode / tests / backend / reason — nothing injected.
    assert len(text.splitlines()) == 4, text
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
    # An unsafe name never reaches the word-split argv — and is never silently dropped either:
    # a selection that skips a selected test is not a selection (#2072, Hermes 8260).
    d = _tests(tmp_path, {"test_ok.py": '"web"', "test_has space.py": '"web"'})
    assert scope.decide(["web/src/a.tsx"], d)[:2] == ("full", [])
    d2 = _tests(tmp_path / "b", {"test_ok.py": '"web"'})
    assert scope.decide(["web/src/a.tsx"], d2)[1] == ["tests/test_ok.py"]


def test_paging_does_not_trust_a_short_page(monkeypatch):
    # An instance capping pages at 10 (below PAGE): the list must still be read to the end.
    pages = {
        1: [{"filename": f"web/a{i}.ts"} for i in range(10)],
        2: [{"filename": f"web/b{i}.ts"} for i in range(10)],
        3: [{"filename": "pyproject.toml"}],  # still a full-suite path (#2072 narrowed src/)
    }

    def fake_urlopen(req, timeout):
        page = int(req.full_url.rsplit("page=", 1)[1])
        return _Resp(json.dumps(pages.get(page, [])).encode())

    monkeypatch.setattr(scope.urllib.request, "urlopen", fake_urlopen)
    files = scope.changed_files("https://x/api", "7", "tok")
    assert len(files) == 21 and files[-1] == "pyproject.toml"
    assert scope.decide(files, Path("unused"))[0] == "full"


def test_a_crafted_check_name_cannot_inject_a_runner_command(monkeypatch, capfd):
    _fake_reader(monkeypatch, [[_st("ci / x\n::add-mask::secret (pull_request)", "failure")]])
    rc = watch.run([sys.executable, "-c", "import time; time.sleep(30)"], SELF, "a", "s", "t", 0.05)
    assert rc == watch.ABORTED
    lines = capfd.readouterr().out.splitlines()
    assert not any(line.startswith("::add-mask::") for line in lines)
    assert any("aborted: sibling" in line for line in lines)


# --- #2072: backend scoping by the import graph ----------------------------------------------


def _repo(tmp_path: Path) -> Path:
    """A package with a chain, a lazy import, a route module and main; tests of each kind."""
    files = {
        "src/agent_sessions/__init__.py": "",
        "src/agent_sessions/store.py": "X = 1\n",
        "src/agent_sessions/service.py": "from agent_sessions import store\n",
        "src/agent_sessions/api.py": "from . import service\n",
        "src/agent_sessions/late.py": "def f():\n    from agent_sessions import api\n",
        "src/agent_sessions/later.py": "def g():\n    from agent_sessions import late\n",
        "src/agent_sessions/routes/__init__.py": "",
        "src/agent_sessions/routes/widgets.py": "from agent_sessions import store\n",
        "src/agent_sessions/main.py": (
            "from agent_sessions.routes import widgets\nfrom . import api\n"
        ),
        "src/agent_sessions/templates/page.html": "<p>",
        "src/agent_sessions/static/unnamed.css": "p{}",
        "tests/test_store.py": "from agent_sessions import store\n",
        "tests/test_service.py": "from agent_sessions.service import store\n",
        "tests/test_api.py": "import agent_sessions.api\n",
        "tests/test_late.py": "from agent_sessions import late\n",
        "tests/test_later.py": "from agent_sessions import later\n",
        "tests/test_app.py": "from agent_sessions.main import create_app\n",
        "tests/test_widgets_http.py": "from agent_sessions.main import x\nget('/widgets')\n",
        "tests/test_tree.py": 'SRC = ROOT / "src"\nfor p in SRC.rglob("*.py"): pass\n',
        "tests/test_page.py": "render('page.html')\n",
        "tests/test_unrelated.py": "def test_x(): pass\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path / "tests"


def test_a_backend_module_selects_its_import_reach_and_the_whole_tree_tests(tmp_path):
    tests = _repo(tmp_path)
    mode, picked, _ = scope.decide(["src/agent_sessions/store.py"], tests)
    assert mode == "subset"
    # store ← service ← api (module level, to a fixed point), ← late (one lazy hop) — not later
    # (a second lazy hop), not main's importers. Plus routes/widgets' test (module level) and the
    # whole-tree test.
    assert picked == [
        "tests/test_api.py",
        "tests/test_late.py",
        "tests/test_service.py",
        "tests/test_store.py",
        "tests/test_tree.py",
    ]


def test_a_route_module_reaches_the_http_tests_naming_it(tmp_path):
    tests = _repo(tmp_path)
    picked = scope.decide(["src/agent_sessions/routes/widgets.py"], tests)[1]
    assert "tests/test_widgets_http.py" in picked  # imports only main, names the route
    assert "tests/test_app.py" not in picked
    assert "tests/test_unrelated.py" not in picked


def test_main_selects_every_test_importing_it(tmp_path):
    tests = _repo(tmp_path)
    picked = scope.decide(["src/agent_sessions/main.py"], tests)[1]
    assert {"tests/test_app.py", "tests/test_widgets_http.py", "tests/test_tree.py"} <= set(picked)
    assert "tests/test_store.py" not in picked


def test_backend_data_files_and_changed_tests(tmp_path):
    tests = _repo(tmp_path)
    assert scope.decide(["src/agent_sessions/templates/page.html"], tests)[1] == [
        "tests/test_page.py",
        "tests/test_tree.py",
    ]
    # A data file no test names: everything rather than nothing.
    assert scope.decide(["src/agent_sessions/static/unnamed.css"], tests)[0] == "full"
    # A changed test file runs itself (and is not a backend change).
    assert scope.decide(["tests/test_unrelated.py"], tests)[1] == ["tests/test_unrelated.py"]


def test_the_backend_full_triggers(tmp_path):
    tests = _repo(tmp_path)
    many = [f"src/agent_sessions/m{i}.py" for i in range(26)]
    assert scope.decide(many, tests)[0] == "full"
    assert scope.decide(["src/agent_sessions/store.py"], tests, ["ci:full"])[0] == "full"
    assert scope.decide(["tests/conftest.py"], tests)[0] == "full"
    # The appmode suite is pr-validate-neutral.
    assert scope.decide(["tests/appmode/seed.py"], tests)[0] == "none"


def test_the_output_says_whether_a_backend_file_changed(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    scope.main(["--files", "docs/site/guide/projects.md"])
    assert "backend=0\n" in out.read_text()
    out.unlink()
    scope.main(["--files", "src/agent_sessions/prefs.py"])
    text = out.read_text()
    assert "backend=1\n" in text and "mode=subset\n" in text


def test_the_real_tree_narrows_a_route_change():
    repo = Path(__file__).resolve().parents[1]
    mode, picked, reason = scope.decide(["src/agent_sessions/routes/artifacts.py"], repo / "tests")
    assert mode == "subset", reason
    assert "tests/test_artifacts.py" in picked and "tests/test_hq_coverage.py" in picked
    total = len(list((repo / "tests").rglob("test_*.py")))
    assert len(picked) < total * 0.6, (len(picked), total)


def test_a_push_to_main_is_the_full_backstop(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert scope.main([]) == 0
    text = out.read_text()
    assert "mode=full\n" in text and "backend=1\n" in text


def test_a_test_name_that_cannot_be_passed_safely_runs_everything(tmp_path):
    tests = _repo(tmp_path)
    (tests / "test_store extra.py").write_text("from agent_sessions import store\n")
    mode, picked, reason = scope.decide(["src/agent_sessions/store.py"], tests)
    assert (mode, picked) == ("full", []), reason
