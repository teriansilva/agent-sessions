"""The Hermes notify script's contract (#735).

Driven as a SUBPROCESS with stubbed binaries on PATH, because the properties worth pinning are
about how the real script behaves — not about logic in isolation. A test of the branching would
have passed against every broken version of this file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".forgejo/scripts/trigger-hermes-review.sh"
BODY = '{"sender":{"login":"x"},"pull_request":{"user":{"login":"y"}}}'


def _env(fake_bin: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_SECRET": extra.pop("secret", "s"),
        "HERMES_URL": "https://example.invalid/hook",
        "EVENT_TYPE": "pull_request",
        "DELIVERY_ID": "test-delivery",
        **extra,
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["bash", str(SCRIPT)],
        input=BODY,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_webhook_secret_never_appears_in_argv(tmp_path):
    """A self-hosted runner is shared and /proc/<pid>/cmdline is world-readable, so a secret
    passed as `openssl -hmac "$SECRET"` can be lifted with `ps` and used to forge review
    webhooks. The body may go on stdin; it was only ever the KEY that leaked.

    Asserted by making every child process record its own command line.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    seen = tmp_path / "argv.log"
    for tool in ("openssl", "curl", "python3"):
        t = fake_bin / tool
        t.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {seen}\nexec /usr/bin/{tool} "$@"\n')
        t.chmod(0o755)

    secret = "SUPERSECRETVALUE"  # noqa: S105 — test fixture
    _run(_env(fake_bin, secret=secret))

    recorded = seen.read_text() if seen.exists() else ""
    assert recorded, "no child command lines were captured — the probe did not run"
    assert secret not in recorded, (
        "the webhook secret appeared in a child process's argv, where any local user can read "
        "it from /proc/<pid>/cmdline and forge review webhooks"
    )


def test_the_signature_is_wire_identical_to_the_openssl_form(tmp_path):
    """Moving the HMAC off openssl must not change a single byte of the signature, or Hermes'
    verifier rejects every webhook and reviews stop entirely."""
    import hashlib
    import hmac

    secret = "s3cr3t"  # noqa: S105 — test fixture
    expected = hmac.new(secret.encode(), BODY.encode(), hashlib.sha256).hexdigest()
    openssl = subprocess.run(  # noqa: S603
        ["openssl", "dgst", "-sha256", "-hmac", secret],
        input=BODY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[-1]
    assert openssl == expected, "python hmac and openssl disagree — the wire format would change"


@pytest.mark.parametrize("rc", [6, 7, 28])
def test_any_curl_failure_fails_the_job(tmp_path, rc):
    """Strict by design. Three attempts to tolerate a timeout were all fail-open (see the
    script's comment), and a review gate that reports success with no review queued is worse
    than one that goes red. Durable acceptance is the receiver's property; the sender cannot
    establish it, so it does not pretend to.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(f"#!/bin/sh\nexit {rc}\n")
    curl.chmod(0o755)

    r = _run(_env(fake_bin))
    assert r.returncode != 0, f"curl exit {rc} was tolerated — the gate can go green unreviewed"


def test_a_successful_post_succeeds(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o755)

    r = _run(_env(fake_bin))
    assert r.returncode == 0, r.stderr
    assert "notified" in r.stdout + r.stderr
