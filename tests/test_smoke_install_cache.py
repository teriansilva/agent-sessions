"""scripts/smoke-install's download cache (#1102), exercised network-free.

The `curl` wrapper is extracted from the script verbatim and run against a fake transport, so what
is tested is the shipped text. The property that matters: the cache can shorten a run, but it can
never serve bytes other than what the requested url served — including a DIFFERENT artifact whose
digest is also one of install.sh's pins (Hermes, review 5068).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/1/cpython.tar.gz"
NODE_URL = "https://nodejs.org/dist/v1/node.tar.gz"


def _shim_text() -> str:
    text = (ROOT / "scripts" / "smoke-install").read_text()
    m = re.search(r"read -r -d '' CURL_SHIM <<'SHIM' \|\| true\n(.*?)\nSHIM\n", text, re.S)
    assert m, "CURL_SHIM heredoc not found in scripts/smoke-install"
    return m.group(1) + "\n"


@pytest.fixture
def world(tmp_path: Path):
    python_bytes, node_bytes = b"python-artifact\n", b"node-artifact\n"
    served = tmp_path / "served"
    served.mkdir()
    (served / "python").write_bytes(python_bytes)
    (served / "node").write_bytes(node_bytes)
    pins = tmp_path / "install.sh"
    pins.write_text(
        "".join(f"want_sha={hashlib.sha256(b).hexdigest()}\n" for b in (python_bytes, node_bytes))
    )
    calls = tmp_path / "calls.log"
    fake = tmp_path / "real-curl"
    # The fake transport records every call and "downloads" by url, like the real one would.
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {calls}\n'
        'out=""; url=""; prev=""\n'
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] && out="$a"; case "$a" in https://*) url="$a";; esac; prev="$a"\n'
        "done\n"
        f'case "$url" in *python*) src={served}/python ;; *node*) src={served}/node ;;\n'
        "  *) exit 22 ;;\nesac\n"
        'cp "$src" "$out"\n'
    )
    fake.chmod(0o755)
    shim = tmp_path / "curl"
    shim.write_text(_shim_text())
    shim.chmod(0o755)
    env = dict(
        os.environ,
        SMOKE_REAL_CURL=str(fake),
        SMOKE_CACHE_ROOT=str(tmp_path / "cache"),
        SMOKE_PIN_FILE=str(pins),
    )

    def fetch(url: str) -> bytes:
        out = tmp_path / "out.tgz"
        out.unlink(missing_ok=True)
        subprocess.run([str(shim), "-fsSL", url, "-o", str(out)], env=env, check=True)
        return out.read_bytes()

    def transport_calls() -> int:
        return len(calls.read_text().splitlines()) if calls.exists() else 0

    def entry(url: str) -> Path:
        return tmp_path / "cache" / "downloads" / hashlib.sha256(url.encode()).hexdigest()

    return dict(
        fetch=fetch, calls=transport_calls, entry=entry, python=python_bytes, node=node_bytes
    )


def test_a_miss_stores_and_a_hit_skips_the_transport(world):
    assert world["fetch"](PY_URL) == world["python"]
    assert world["calls"]() == 1
    assert world["fetch"](PY_URL) == world["python"]
    assert world["calls"]() == 1, "a warm hit still went to the network"


def test_a_different_PINNED_artifact_under_the_key_is_evicted_not_served(world):
    """Review 5068: a digest pinned SOMEWHERE in install.sh is not the digest this url serves."""
    world["fetch"](PY_URL)
    e = world["entry"](PY_URL)
    e.write_bytes(world["node"])  # the other valid pinned artifact, under the python url's key
    before = world["calls"]()
    assert world["fetch"](PY_URL) == world["python"]
    assert world["calls"]() == before + 1, "the substituted entry was served instead of refetched"
    assert e.read_bytes() == world["python"], "the cache was not repaired to the url's bytes"
    assert world["fetch"](PY_URL) == world["python"]
    assert world["calls"]() == before + 1, "the repaired entry is not served warm"


def test_a_non_pinned_entry_is_evicted_and_refetched(world):
    world["fetch"](NODE_URL)
    world["entry"](NODE_URL).write_bytes(b"garbage")
    assert world["fetch"](NODE_URL) == world["node"]
    assert world["entry"](NODE_URL).read_bytes() == world["node"]


def test_an_entry_without_its_url_binding_is_never_served(world):
    world["fetch"](PY_URL)
    e = world["entry"](PY_URL)
    e.with_name(e.name + ".sha").unlink()
    before = world["calls"]()
    world["fetch"](PY_URL)
    assert world["calls"]() == before + 1


def test_other_urls_pass_straight_through_and_are_never_cached(world, tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        world["fetch"]("https://example.com/other.tgz")  # fake transport exits 22 for it
    assert not (tmp_path / "cache" / "downloads").exists()


def test_run_sh_evicts_and_retries_cold_on_a_checksum_mismatch():
    """The backstop for anything the shim cannot see: install.sh's per-asset refusal."""
    text = (ROOT / "scripts" / "smoke-install").read_text()
    retry = text[text.index("if ! sh /srepo/install.sh") :]
    retry = retry[: retry.index("P=\\$AGENT_SESSIONS_HOME")]
    assert 'grep -q "checksum mismatch"' in retry
    assert retry.index("rm -rf /smoke-cache/downloads") < retry.rindex("sh /srepo/install.sh")
    assert "exit 1" in retry, "a non-checksum install failure must still fail the smoke"
