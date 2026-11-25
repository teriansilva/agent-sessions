"""`recheck.py` + the `recheck_report` pytest plugin (#2072).

A few failed tests run again alone before a check goes red. The rules, judged from structured
reports and never from exit codes alone:

- only ordinary test failures are rechecked: never a crashed xdist worker, a timed-out test (#2178),
  a collection or internal error, a run-level Playwright error or a dead Playwright worker, and
  never more than five;
- the recheck passes only if every one of those tests RAN and PASSED under the same identity: a
  test that skips, is missing or fails again keeps the suite's status;
- each recovered test is named in a one-line warning.

The real-pytest tests below run the plugin in a subprocess, with xdist, as CI does.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".forgejo" / "scripts"
PLUGINS = SCRIPTS.parent / "pytest_plugins"
_spec = importlib.util.spec_from_file_location("recheck", SCRIPTS / "recheck.py")
recheck = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(recheck)


def _pytest_report(results=None, **extra):
    base = {
        "results": results or {},
        "crashed": [],
        "timed_out": [],
        "collect_errors": 0,
        "internal_error": False,
    }
    return {**base, **extra}


# --- the decision -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rc", "report", "go"),
    [
        (0, None, False),  # passed
        (1, _pytest_report({"t::a": "failed", "t::b": "passed"}), True),
        (2, _pytest_report({"t::a": "failed"}), False),  # interrupted
        (1, None, False),  # no report written
        (1, _pytest_report({"t::a": "failed"}, crashed=["t::a"]), False),
        (1, _pytest_report({"t::a": "failed"}, timed_out=["t::a"]), False),
        (1, _pytest_report({"t::a": "failed"}, collect_errors=1), False),
        (1, _pytest_report({"t::a": "failed"}, internal_error=True), False),
        (1, _pytest_report({"t::a": "passed"}), False),  # red with nothing failed
        (1, _pytest_report({f"t::{i}": "failed" for i in range(5)}), True),
        (1, _pytest_report({f"t::{i}": "failed" for i in range(6)}), False),
    ],
)
def test_what_pytest_rechecks(rc, report, go):
    assert recheck.decide("pytest", rc, recheck.pytest_verdict(report))[0] is go


def _pw(*tests, errors=None):
    """A Playwright JSON report: tests are (project, title, status[, error message])."""
    specs = []
    for t in tests:
        result = {"status": "failed" if t[2] == "unexpected" else "passed"}
        if len(t) > 3:
            result["error"] = {"message": t[3]}
        specs.append(
            {
                "title": t[1],
                "file": "a.spec.ts",
                "line": 3,
                "tests": [{"projectName": t[0], "status": t[2], "results": [result]}],
            }
        )
    return {
        "suites": [{"title": "a.spec.ts", "specs": specs, "suites": []}],
        "errors": errors or [],
    }


def test_what_playwright_rechecks():
    report = _pw(("desktop", "opens", "unexpected"), ("mobile", "opens", "expected"))
    v = recheck.playwright_verdict(report)
    assert v.failed == ["[desktop] › a.spec.ts:3 › opens"]  # the project is part of the identity
    assert recheck.decide("playwright", 1, v)[0]
    assert recheck.playwright_verdict(None).refuse
    assert recheck.playwright_verdict(_pw(errors=[{"message": "webServer timed out"}])).refuse
    died = _pw(("mobile", "opens", "unexpected", "Error: Worker process exited unexpectedly"))
    assert "worker process died" in recheck.playwright_verdict(died).refuse


def test_a_retry_counts_only_tests_that_ran_and_passed():
    ids = ["t::a", "t::b", "t::c"]
    retry = _pytest_report({"t::a": "passed", "t::b": "skipped"})  # t::c did not run
    assert recheck.pytest_not_passed(retry, ids) == ["t::b", "t::c"]
    assert recheck.pytest_not_passed(None, ids) == ids
    crashed = _pytest_report({"t::a": "passed"}, crashed=["x"])
    assert recheck.pytest_not_passed(crashed, ["t::a"]) == ["t::a"]
    first = ["[desktop] › a.spec.ts:3 › opens", "[mobile] › a.spec.ts:3 › opens"]
    retry_pw = _pw(("desktop", "opens", "expected"), ("mobile", "opens", "skipped"))
    assert recheck.playwright_not_passed(retry_pw, first) == ["[mobile] › a.spec.ts:3 › opens"]
    assert recheck.playwright_not_passed(None, first) == first


def test_a_test_title_cannot_inject_a_workflow_command():
    cleaned = recheck._clean("x\n::error::boom\r%0A::error::two")
    assert "\n" not in cleaned and "\r" not in cleaned
    assert "%0A" not in cleaned  # the runner would decode an escaped newline
    assert recheck._clean("tests/a.py::t[x]") == "tests/a.py::t[x]"


def test_bad_usage_is_refused():
    assert recheck.main([]) == 2
    assert recheck.main(["pytest", "--rc", "1", "--", "x"]) == 2  # no --report
    assert recheck.main(["jest", "--rc", "1", "--report", "r", "--", "x"]) == 2
    assert recheck.main(["pytest", "--rc", "x", "--report", "r", "--", "x"]) == 2


# --- the plugin and the whole loop, in real pytest ----------------------------------------------


def _pytest_argv(tmp_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(tmp_path),
    ]


def _env() -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(PLUGINS)}
    env.pop("PYTEST_ADDOPTS", None)
    return env


def _first_run(tmp_path: Path, body: str, workers: str = "2") -> tuple[int, Path]:
    (tmp_path / "test_probe.py").write_text(body)
    report = tmp_path / "first.json"
    argv = [
        *_pytest_argv(tmp_path),
        "-p",
        "recheck_report",
        f"--recheck-report={report}",
        "-n",
        workers,
        "test_probe.py",
    ]
    rc = subprocess.run(argv, cwd=tmp_path, env=_env(), capture_output=True, timeout=120).returncode
    return rc, report


def _loop(tmp_path: Path, body: str) -> tuple[int, str]:
    rc, report = _first_run(tmp_path, body)
    argv = [
        sys.executable,
        str(SCRIPTS / "recheck.py"),
        "pytest",
        "--rc",
        str(rc),
        "--report",
        str(report),
        "--",
        *_pytest_argv(tmp_path),
        "-n",
        "0",
    ]
    out = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    return out.returncode, out.stdout


FLAKE = """
import os
def test_flaky():
    if not os.path.exists("marker"):
        open("marker", "w").write("1")
        assert False, "load flake"
def test_ok():
    pass
"""

SKIPS_ON_RETRY = """
import os, pytest
def test_skips_on_retry():
    if os.path.exists("marker"):
        pytest.skip("skipped on the retry")
    open("marker", "w").write("1")
    assert False
"""

WORKER_CRASH = """
import os
def test_dies():
    os._exit(17)
"""


def test_a_real_flake_turns_green_and_is_named(tmp_path):
    rc, out = _loop(tmp_path, FLAKE)
    assert rc == 0, out
    assert "::warning title=Passed only on a serial recheck::test_probe.py::test_flaky" in out


def test_a_test_that_skips_on_the_retry_stays_red(tmp_path):
    rc, out = _loop(tmp_path, SKIPS_ON_RETRY)
    assert rc == 1, out
    assert "did not pass alone" in out and "::warning" not in out


def test_a_crashed_xdist_worker_is_never_rechecked(tmp_path):
    rc, report = _first_run(tmp_path, WORKER_CRASH)
    assert rc == 1  # xdist exits 1 for a dead worker, like an ordinary failure
    assert json.loads(report.read_text())["crashed"] == ["test_probe.py::test_dies"]
    rc, out = _loop(tmp_path, WORKER_CRASH)
    assert rc == 1 and "worker crashed" in out and "rechecking" not in out, out


def test_a_collection_error_is_never_rechecked(tmp_path):
    rc, out = _loop(tmp_path, "import no_such_module_xyz\ndef test_x(): pass\n")
    assert rc != 0 and "rechecking" not in out, out


TIMEOUT_IN_CALL = """
import time
import pytest

@pytest.mark.timeout(2)
def test_hangs():
    time.sleep(30)
"""

TIMEOUT_IN_TEARDOWN = """
import time
import pytest

@pytest.fixture
def slow_teardown():
    yield
    time.sleep(30)

@pytest.mark.timeout(2)
def test_passes_then_hangs_in_teardown(slow_teardown):
    pass
"""


def _assert_a_timeout_stays_red(tmp_path, body: str, nodeid: str, workers: str = "2"):
    """The hung test fails by timeout, is named in the report, and is never re-run (#2178)."""
    rc, report = _first_run(tmp_path, body, workers)
    assert rc == 1  # a timeout is a test failure, never a clean exit
    data = json.loads(report.read_text())
    assert data["timed_out"] == [nodeid]
    assert data["results"][nodeid] == "failed"
    rc, out = _loop(tmp_path, body)
    assert rc == 1, out
    assert "timed out" in out and "rechecking" not in out, out


@pytest.mark.parametrize("workers", ["2", "0"])  # xdist, and the serial recheck shape
def test_a_test_that_times_out_in_its_call_is_never_rechecked(tmp_path, workers):
    _assert_a_timeout_stays_red(tmp_path, TIMEOUT_IN_CALL, "test_probe.py::test_hangs", workers)


def test_a_test_that_times_out_in_teardown_is_never_rechecked(tmp_path):
    _assert_a_timeout_stays_red(
        tmp_path, TIMEOUT_IN_TEARDOWN, "test_probe.py::test_passes_then_hangs_in_teardown"
    )


# A call timeout fires first; the fixture finalizer then blocks. pytest-timeout's one-shot
# signal has no timer left, so without the independent hard stop this run never ends (#2178 P1).
BLOCKED_CLEANUP = """
import os
import time
import pytest

@pytest.fixture
def slow_teardown():
    yield
    open(os.environ["CLEANUP_MARK"], "w").write("1")
    time.sleep(30)

@pytest.mark.timeout(1)
def test_call_times_out_then_cleanup_hangs(slow_teardown):
    time.sleep(30)
"""

BLOCKED_NODEID = "test_probe.py::test_call_times_out_then_cleanup_hangs"


def _blocked_cleanup_run(tmp_path: Path, workers: str):
    """The probe under a tiny hard margin, with an external deadline as the regression's proof."""
    (tmp_path / "test_probe.py").write_text(BLOCKED_CLEANUP)
    report = tmp_path / "first.json"
    argv = [
        *_pytest_argv(tmp_path),
        "-p",
        "recheck_report",
        "-o",
        "timeout_hard_margin=2",
        f"--recheck-report={report}",
        "-n",
        workers,
        "test_probe.py",
    ]
    env = {**_env(), "CLEANUP_MARK": str(tmp_path / "cleanup_entered")}
    try:
        proc = subprocess.run(
            argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        pytest.fail("a blocked cleanup held the run past the hard backstop")
    assert (tmp_path / "cleanup_entered").is_file(), "the fixture finalizer was never reached"
    return proc, report


def _recheck(tmp_path: Path, rc: int, report: Path) -> tuple[int, str]:
    argv = [
        sys.executable,
        str(SCRIPTS / "recheck.py"),
        "pytest",
        "--rc",
        str(rc),
        "--report",
        str(report),
        "--",
        *_pytest_argv(tmp_path),
        "-n",
        "0",
    ]
    out = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    return out.returncode, out.stdout


def test_a_blocked_cleanup_after_a_timeout_is_hard_stopped_serially(tmp_path):
    proc, report = _blocked_cleanup_run(tmp_path, "0")
    assert proc.returncode != 0
    assert not report.exists(), "a hard-killed serial process must not write a passing report"
    rc, out = _recheck(tmp_path, proc.returncode, report)
    assert rc != 0 and "no pytest report was written" in out and "rechecking" not in out, out


def test_a_blocked_cleanup_after_a_timeout_is_hard_stopped_under_xdist(tmp_path):
    proc, report = _blocked_cleanup_run(tmp_path, "2")
    assert proc.returncode != 0
    data = json.loads(report.read_text())
    assert data["crashed"] == [BLOCKED_NODEID]  # the hard stop killed the worker
    assert data["timed_out"] == [BLOCKED_NODEID]  # the soft timeout fired first
    rc, out = _recheck(tmp_path, proc.returncode, report)
    assert rc != 0 and "worker crashed" in out and "rechecking" not in out, out
