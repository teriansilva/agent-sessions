"""Material replacement uses the editor's real lease, displacement and crash recovery."""

import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sessions import fileedit
from agent_sessions.files import FsError
from test_file_edit import root as _edit_root

root = _edit_root


def sha(value):
    return hashlib.sha256(value).hexdigest()


def replace(path, content, before, admit=lambda path: None):
    return fileedit.save_bytes(str(path), content, sha(before), root=str(path.parent), admit=admit)


@pytest.mark.parametrize(
    "before,after",
    [(b"\x00\xffold", b"\xfe\x00new"), (b"\xef\xbb\xbfhello\r\n", b"plain\n"), (b"a\r\nb\n", b"")],
)
def test_materials_keep_exact_bytes_without_text_normalization(root, before, after):
    path = root / "asset.bin"
    path.write_bytes(before)
    path.chmod(0o640)
    result = replace(path, after, before)
    assert path.read_bytes() == after
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.stat().st_nlink == 1
    assert Path(result["retained"]["path"]).read_bytes() == before
    assert result["version"] == sha(after)


def test_public_editor_still_refuses_binary_payloads_and_binary_targets(root):
    path = root / "asset.bin"
    path.write_bytes(b"\xffold")
    with pytest.raises(FsError, match="content must be text"):
        fileedit.save(str(path), b"\xffnew", sha(b"\xffold"))
    with pytest.raises(fileedit.SaveRefused, match="UTF-8"):
        fileedit.save(str(path), "new", sha(b"\xffold"))
    assert path.read_bytes() == b"\xffold"


def test_live_admission_failure_after_install_restores_the_original(root):
    path = root / "asset.bin"
    path.write_bytes(b"\xffold")
    installed = False

    def hook(step):
        nonlocal installed
        installed |= step == "install"

    def admit(_):
        if installed:
            raise FsError("deployment admission revoked", status=409)

    fileedit._HOOK = hook
    try:
        with pytest.raises(FsError, match="admission revoked"):
            replace(path, b"\xfenew", b"\xffold", admit)
    finally:
        fileedit._HOOK = None
    assert path.read_bytes() == b"\xffold"


CHILD = r"""
import os, signal, sys
from agent_sessions import fileedit
assert fileedit.install_lease_signal_handler()
fileedit.LEASE_BUDGET_S = 120.0
fileedit.STORE_LOCK_WAIT_S = 120.0
path, step, before = sys.argv[1:]
def hook(current):
    if current == step:
        os.kill(os.getpid(), signal.SIGKILL)
fileedit._HOOK = hook
fileedit.save_bytes(path, b'\xfenew', before, root=os.path.dirname(path), admit=lambda _: None)
"""


@pytest.mark.parametrize("step", ["intent", "stage", "displace", "check", "install", "finish"])
def test_a_real_process_death_recovers_binary_bytes_without_assuming_consent(root, step):
    path = root / "asset.bin"
    path.write_bytes(b"\xffold")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_SESSIONS_")}
    env["AGENT_SESSIONS_FS_ROOT"] = str(root)
    env["AGENT_SESSIONS_EDIT_RECOVERY"] = fileedit.recovery_dir()
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(path), step, sha(b"\xffold")],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    fileedit.startup_resolve()
    assert path.read_bytes() == (b"\xfenew" if step == "finish" else b"\xffold")
    if step not in {"intent", "stage"}:
        assert any(
            p.is_file() and p.read_bytes() == b"\xffold"
            for p in Path(fileedit.recovery_dir()).glob("*/*")
        )


@pytest.mark.parametrize("admit,scope", [(None, "/tmp"), (lambda _: None, None)])
def test_material_replacement_requires_a_live_guard_and_explicit_root(root, admit, scope):
    path = root / "asset.bin"
    path.write_bytes(b"old")
    with pytest.raises(FsError, match="root and live admission"):
        fileedit.save_bytes(str(path), b"new", sha(b"old"), root=scope, admit=admit)
    assert path.read_bytes() == b"old"
