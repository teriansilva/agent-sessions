"""The e2e preview server dies with its Playwright, and Chromium keeps shared memory in RAM.

Playwright runs its webServer in a process group of its own, so a shard stopped by
sibling-watch (#1244) or a runner cancel — which signal the step's group, or SIGKILL Playwright
before its teardown runs — used to leave `vite preview` reparented to init, squatting its port
for hours (15 such orphans were reaped from the runner on 2026-10-07). In CI the webServer now
execs vite under `setpriv --pdeathsig KILL`, so the kernel kills it when Playwright exits.

Playwright also launches Chromium with `--disable-dev-shm-usage` by default, which moves its
shared memory into the temp dir — on the CI host that was the runner's disk, the measured CI
bottleneck. Every `launchOptions` in the suite opts out of it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
CONFIG = (WEB / "playwright.config.ts").read_text()


def _ci_preview_command() -> str:
    m = re.search(r"const PREVIEW_CMD = process\.env\.CI\s*\?\s*`([^`]+)`", CONFIG)
    assert m, "playwright.config.ts no longer defines the CI preview command"
    return m.group(1)


def test_the_ci_preview_server_is_bound_to_its_playwright_parent() -> None:
    cmd = _ci_preview_command()
    assert cmd.startswith("exec setpriv --pdeathsig KILL -- "), cmd
    # No npm / extra sh between setpriv and vite: pdeathsig binds only the exec'd process, and an
    # intermediate wrapper would be the one that dies while vite lives on.
    assert "npm " not in cmd and "vite.js preview" in cmd
    assert "command: PREVIEW_CMD" in CONFIG, "webServer no longer uses the bound command"


@pytest.mark.skipif(not Path("/usr/bin/setpriv").exists(), reason="setpriv is Linux/util-linux")
def test_pdeathsig_kills_the_server_when_its_parent_is_sigkilled() -> None:
    """The mechanism itself, for real: SIGKILL the parent, and the bound child must die too."""
    parent = subprocess.Popen(
        [
            "python3",
            "-c",
            "import subprocess,sys,time;"
            "c=subprocess.Popen(['sh','-c','exec setpriv --pdeathsig KILL -- sleep 300']);"
            "print(c.pid,flush=True);time.sleep(300)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    child = int(parent.stdout.readline())
    try:
        # Kill only once the child has exec'd into the server (pdeathsig is set by setpriv just
        # before that exec) — the same order Playwright guarantees by waiting for the server's URL.
        for _ in range(100):
            if Path(f"/proc/{child}/cmdline").read_bytes().startswith(b"sleep\0"):
                break
            time.sleep(0.05)
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        for _ in range(50):
            if not Path(f"/proc/{child}").exists():
                break
            time.sleep(0.1)
        assert not Path(f"/proc/{child}").exists(), "the bound child outlived its parent"
    finally:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_every_launch_options_keeps_chromium_shared_memory_out_of_the_temp_dir() -> None:
    sources = {"playwright.config.ts": CONFIG} | {
        str(p.relative_to(WEB)): p.read_text() for p in sorted((WEB / "e2e").glob("*.ts"))
    }
    assert "launchOptions: CHROMIUM_LAUNCH" in CONFIG
    assert 'ignoreDefaultArgs: ["--disable-dev-shm-usage"]' in CONFIG
    overriding = [name for name, text in sources.items() if "launchOptions: {" in text]
    assert overriding, "expected the homefree specs' own launchOptions"
    for name in overriding:
        assert (
            '"--disable-dev-shm-usage"' in sources[name]
        ), f"{name} replaces launchOptions without repeating ignoreDefaultArgs"
