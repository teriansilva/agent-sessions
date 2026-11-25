"""The suite-wide per-test timeout (#2178).

A hung test used to hold a pr-validate worker for hours: the PR's check stayed `pending` until
someone pushed a new commit. The guard is one default in `pyproject.toml`, so it is pinned here —
a future edit that removes it, switches to the teardown-skipping `thread` method, or moves the
faulthandler dump past the hard stop would silently reopen the hole.

180 s is pytest-timeout's SOFT limit; the independent hard stop lives in the CI plugin
(`.forgejo/pytest_plugins/recheck_report.py`, 60 s later, so a blocked cleanup is bounded too).
The soft default is sized from measured runs, with wide headroom: the slowest legitimate tests
outside `tests/test_installer.py` are ~19 s locally (`tests/test_handoff.py`), and the
native-runtime / session-latency files peak at ~8 s; CI's `-n 8` inflates those, so 180 s is >9x
the measured worst case. The full install.sh end-to-end tests carry their own explicit
`@pytest.mark.timeout(...)` because they are legitimately multi-minute.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ini() -> dict:
    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]


def test_one_signal_timeout_below_the_faulthandler_dump():
    ini = _ini()
    # `signal` raises inside the test, so setup/call/teardown still run. `thread` hard-exits
    # pytest and skips fixture teardown, which no default may do.
    assert ini["timeout_method"] == "signal"
    assert ini["timeout_func_only"] is False  # setup + call + teardown, not only the call
    assert ini["timeout"] == 180
    assert ini["faulthandler_timeout"] < ini["timeout"]
