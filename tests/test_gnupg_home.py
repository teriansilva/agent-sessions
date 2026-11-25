"""#2045: a GnuPG home a test uses never leaves a daemon behind.

`gpg --quick-gen-key` autostarts `gpg-agent --homedir <home> --daemon`. Before `gnupg_home`, the
PGP tests never stopped it; on the CI runners the agents outlived their runs and held the pytest
basetemp open. These pin that the fixture's teardown stops what the home started, through
`gpgconf` on that home only.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import gnupg_daemons, stop_gnupg, stopped_gnupg

needs_gnupg = pytest.mark.skipif(
    not (shutil.which("gpg") and shutil.which("gpgconf") and shutil.which("gpg-connect-agent")),
    reason="GnuPG required",
)


def _start_agent(home) -> list[int]:
    subprocess.run(
        ["gpg-connect-agent", "--homedir", str(home), "/bye"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return gnupg_daemons(home)


@needs_gnupg
def test_stop_gnupg_ends_the_agent_its_home_started(tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    try:
        assert _start_agent(home), "no agent started: the rest would prove nothing"
        bystander = _start_agent(other)
        assert bystander
        assert stop_gnupg(home) == []
        assert gnupg_daemons(home) == []
        # Only that home's agent: another home's one (on the runner: another job's) still runs.
        assert gnupg_daemons(other) == bystander
    finally:
        stop_gnupg(home)
        stop_gnupg(other)


@pytest.fixture
def homes_checked_after_teardown():
    """Requested BEFORE `gnupg_home`, so its teardown runs after `gnupg_home`'s. Were the order
    ever different, the check would run with the agent still up and fail, never pass falsely."""
    homes: list = []
    yield homes
    survivors = {str(h): gnupg_daemons(h) for h in homes}
    for h in homes:
        stop_gnupg(h)  # never leave one behind, even when this fails
    assert not any(survivors.values()), f"outlived gnupg_home's teardown: {survivors}"


@needs_gnupg
def test_the_fixture_stops_the_agent_a_key_generation_starts(
    homes_checked_after_teardown, gnupg_home
):
    homes_checked_after_teardown.append(gnupg_home)
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            "pgp@example.invalid",
            "ed25519",
            "sign",
            "never",
        ],
        env={"PATH": os.environ["PATH"], "GNUPGHOME": str(gnupg_home), "LC_ALL": "C"},
        check=True,
        capture_output=True,
        timeout=60,
    )
    assert gnupg_daemons(gnupg_home), "the key generation started no agent: nothing to stop"


@needs_gnupg
def test_a_test_that_raises_still_stops_its_agent(tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    started: list[int] = []
    try:
        with pytest.raises(RuntimeError, match="the test failed"), stopped_gnupg(home):
            started = _start_agent(home)
            assert started
            raise RuntimeError("the test failed")
        assert gnupg_daemons(home) == []
    finally:
        stop_gnupg(home)
