"""scripts/smoke-install keeps the throwaway install off the runner's disk.

The installer smoke clones, installs the vendored toolchains, runs `npm ci` and a Vite build, all
under /home/tester inside a `--rm` container. On the CI host that container's overlay layer sits
on a shared HDD mirror that is the measured CI bottleneck, so /home is a tmpfs. It must stay
`exec` — the vendored python and node are executed from there, and Docker's tmpfs default is
`noexec`, which would fail the smoke the moment the installer runs its own toolchain.
"""

from __future__ import annotations

import re
from pathlib import Path

SMOKE = Path(__file__).resolve().parent.parent / "scripts" / "smoke-install"


def _docker_run_line() -> str:
    lines = [ln for ln in SMOKE.read_text().splitlines() if ln.startswith("docker run ")]
    assert len(lines) == 1, f"expected exactly one top-level docker run, found {len(lines)}"
    return lines[0]


def test_home_is_a_tmpfs_in_the_smoke_container():
    m = re.search(r"--tmpfs /home:(\S+)", _docker_run_line())
    assert m, "the smoke container no longer mounts /home as tmpfs"
    opts = m.group(1).split(",")
    assert "exec" in opts, "tmpfs /home must be exec — the vendored toolchains run from it"
    assert "noexec" not in opts


def test_the_tmpfs_is_bounded():
    m = re.search(r"--tmpfs /home:(\S+)", _docker_run_line())
    assert m and any(
        o.startswith("size=") for o in m.group(1).split(",")
    ), "an unbounded tmpfs can take the runner host's RAM with it"
