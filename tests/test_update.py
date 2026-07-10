"""Self-update (#65 Phase 5): version check + no-input apply."""

from __future__ import annotations

from types import SimpleNamespace

from agent_sessions import update


def test_latest_ref_stable_picks_highest_tag(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda _n: "/usr/bin/git")
    out = SimpleNamespace(
        returncode=0,
        stdout="s1\trefs/tags/v0.1.0\ns2\trefs/tags/v0.10.0\ns3\trefs/tags/v0.2.0\n",
    )
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: out)
    assert update.latest_ref("stable", "url") == "v0.10.0"  # semver, not lexical


def test_latest_ref_main_returns_short_sha(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda _n: "/usr/bin/git")
    out = SimpleNamespace(returncode=0, stdout="abcdef1234567\trefs/heads/main\n")
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: out)
    assert update.latest_ref("main", "url") == "abcdef1"


def test_latest_ref_no_git_is_none(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda _n: None)
    assert update.latest_ref("stable", "url") is None


def test_check_available_and_up_to_date(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v0.2.0")
    monkeypatch.delenv("AGENT_SESSIONS_CHANNEL", raising=False)
    # Hermetic channel read (#538): _channel() consults the env file first — point it at
    # an absent tmp file so a file left behind by another test can't leak in.
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setattr(update, "get_version", lambda: "0.1.0")
    assert update.check()["update_available"] is True
    monkeypatch.setattr(update, "get_version", lambda: "0.2.0")
    assert update.check()["update_available"] is False  # tag == running version


# ---- #583: main-channel check compares SHA↔SHA, never SHA↔version-string ---------------


def test_running_sha_parses_setuptools_scm_and_dev_placeholder():
    assert update._running_sha("0.9.1.dev3+g64eefb3") == "64eefb3"
    assert update._running_sha("0.0.0+ab12cd3") == "ab12cd3"
    assert update._running_sha("0.9.1.dev3+g64eefb3.dirty") == "64eefb3"  # suffix dropped
    assert update._running_sha("0.9.0") is None  # clean release → no SHA to compare
    assert update._running_sha("0.9.0+glocal") is None  # non-hex local segment


def _main_channel(monkeypatch, tmp_path):
    """Point the channel read at ``main`` hermetically (env var + absent env file)."""
    monkeypatch.setenv("AGENT_SESSIONS_CHANNEL", "main")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))


def test_check_main_tagged_head_is_current_not_a_reinstall_loop(monkeypatch, tmp_path):
    # The #583 repro: main HEAD sits on a release tag → setuptools_scm reports a clean
    # "0.9.0" with no SHA. The remote main HEAD is a short SHA. The old code did
    # ("64eefb3" not in "0.9.0") → True → update_available forever → reinstall loop.
    _main_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "64eefb3")
    monkeypatch.setattr(update, "get_version", lambda: "0.9.0")
    assert update.check()["update_available"] is False


def test_check_main_dev_build_behind_head_is_available(monkeypatch, tmp_path):
    _main_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "abcdef1")  # remote moved
    monkeypatch.setattr(update, "get_version", lambda: "0.9.1.dev3+g64eefb3")
    assert update.check()["update_available"] is True


def test_check_main_dev_build_at_head_is_current(monkeypatch, tmp_path):
    _main_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "64eefb3")
    monkeypatch.setattr(update, "get_version", lambda: "0.9.1.dev3+g64eefb3")
    assert update.check()["update_available"] is False


def test_check_main_head_at_head_tolerates_short_sha_lengths(monkeypatch, tmp_path):
    # latest_ref truncates to 7 chars; the embedded SHA may be longer — prefix-compare.
    _main_channel(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "64eefb3")
    monkeypatch.setattr(update, "get_version", lambda: "0.9.1.dev3+g64eefb3a9")
    assert update.check()["update_available"] is False


def test_apply_returns_false_without_an_installer(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))  # no current/src/install.sh
    assert update.apply() is False


def test_apply_spawns_installer_detached_no_user_input(monkeypatch, tmp_path):
    inst = tmp_path / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return SimpleNamespace()

    monkeypatch.setattr(update.subprocess, "Popen", fake_popen)
    assert update.apply() is True
    assert captured["argv"][0].endswith("sh") and captured["argv"][1].endswith("install.sh")
    assert len(captured["argv"]) == 2  # no user-supplied ref/command
    assert captured["kw"]["start_new_session"] is True  # survives the service restart


def test_apply_never_inherits_a_pinned_ref(monkeypatch, tmp_path):
    # A stale AGENT_SESSIONS_REF in the service env must NOT pin the self-update; it
    # always moves to the channel's latest.
    inst = tmp_path / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_SESSIONS_REF", "old-pinned-ref")
    captured = {}
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: captured.update(kw) or SimpleNamespace()
    )
    assert update.apply() is True
    assert "AGENT_SESSIONS_REF" not in captured["env"]
    assert captured["env"]["AGENT_SESSIONS_CHANNEL"]  # channel drives the update


def test_autoupdate_applies_only_when_available(monkeypatch):
    # A real apply() in an earlier test stamps the spawn cooldown (#538) — clear it, this
    # test is about the availability decision, not the cooldown.
    monkeypatch.setattr(update, "_SPAWNED_AT", None)
    monkeypatch.setattr(update, "check", lambda: {"update_available": False})
    assert update.autoupdate() == "up-to-date"

    monkeypatch.setattr(update, "check", lambda: {"update_available": True})
    monkeypatch.setattr(update, "apply", lambda: True)
    assert update.autoupdate() == "applied"
    monkeypatch.setattr(update, "apply", lambda: False)
    assert update.autoupdate() == "unavailable"


def test_cli_autoupdate(monkeypatch, capsys):
    from agent_sessions import cli

    monkeypatch.setattr(update, "autoupdate", lambda: "up-to-date")
    assert cli.main(["autoupdate"]) == 0
    assert capsys.readouterr().out.strip() == "up-to-date"


def test_repo_url_defaults_public_and_honors_override(monkeypatch):
    # Public mirror (#322): the shipped default points at the PUBLIC GitHub repo so a public
    # self-hoster's updater resolves there. Internal deploys override via AGENT_SESSIONS_REPO
    # (the Forgejo URL) and must keep working — the env override wins.
    monkeypatch.delenv("AGENT_SESSIONS_REPO", raising=False)
    assert update._repo_url() == "https://github.com/teriansilva/agent-sessions.git"
    # the public default is a github.com URL (no internal host)
    assert update._DEFAULT_REPO.startswith("https://github.com/")

    # An override (internal deploys point at a private mirror) must win — using an example host
    # here so this very test stays clean of internal references.
    override = "https://git.example.com/org/agent-sessions.git"
    monkeypatch.setenv("AGENT_SESSIONS_REPO", override)
    assert update._repo_url() == override


# ---- #538: persisted settings (env-file-first live read) ------------------------------


def test_settings_env_file_first_live_read(monkeypatch, tmp_path):
    # The running service's os.environ snapshot predates a UI toggle — the env file must
    # win so Settings changes apply live, with process env only as fallback.
    envf = tmp_path / "env"
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.setenv("AGENT_SESSIONS_CHANNEL", "main")
    monkeypatch.setenv("AGENT_SESSIONS_AUTOUPDATE", "1")
    assert update._channel() == "main"  # no file yet → process env fallback
    assert update.auto_update_enabled() is True
    envf.write_text("AGENT_SESSIONS_CHANNEL=stable\nAGENT_SESSIONS_AUTOUPDATE=0\n")
    assert update._channel() == "stable"  # the file wins over stale process env
    assert update.auto_update_enabled() is False


def test_channel_rejects_unknown_values(monkeypatch, tmp_path):
    envf = tmp_path / "env"
    envf.write_text("AGENT_SESSIONS_CHANNEL=beta\n")
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    monkeypatch.delenv("AGENT_SESSIONS_CHANNEL", raising=False)
    assert update._channel() == "stable"  # unknown persisted value → safe default


def test_set_settings_roundtrip_and_preserves_other_lines(monkeypatch, tmp_path):
    envf = tmp_path / "sub" / "env"  # parent dir created on demand (dev checkout)
    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(envf))
    envf.parent.mkdir(parents=True)
    envf.write_text("AGENT_SESSIONS_SECRET_KEY=abc\n")
    out = update.set_settings(auto_update=True, channel="main")
    assert out == {"auto_update": True, "channel": "main"}
    text = envf.read_text()
    assert "AGENT_SESSIONS_SECRET_KEY=abc" in text  # untouched lines preserved
    assert "AGENT_SESSIONS_AUTOUPDATE=1" in text
    assert "AGENT_SESSIONS_CHANNEL=main" in text
    # live read sees it immediately
    assert update.settings() == {"auto_update": True, "channel": "main"}
    # partial update: only the given key changes
    assert update.set_settings(auto_update=False)["channel"] == "main"


def test_set_settings_rejects_unknown_channel(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setenv("AGENT_SESSIONS_ENV_FILE", str(tmp_path / "env"))
    with pytest.raises(ValueError):
        update.set_settings(channel="beta")


# ---- #538: single-flight + recent-runtime status ---------------------------------------


def test_autoupdate_and_manual_apply_are_single_flight(monkeypatch):
    # While one check/apply holds the lock, both entrypoints report busy and never reach
    # the network (check would raise).
    monkeypatch.setattr(
        update, "check", lambda: (_ for _ in ()).throw(AssertionError("network hit"))
    )
    assert update._RUN_LOCK.acquire(blocking=False)
    try:
        assert update.autoupdate() == "busy"
        assert update.apply_manual() == "busy"
    finally:
        update._RUN_LOCK.release()


def test_autoupdate_skips_within_spawn_cooldown(monkeypatch):
    # An installer spawned moments ago is about to restart the service — don't stack a
    # second one from the scheduled path.
    monkeypatch.setattr(update, "_SPAWNED_AT", update.time.monotonic())
    monkeypatch.setattr(
        update, "check", lambda: (_ for _ in ()).throw(AssertionError("network hit"))
    )
    assert update.autoupdate() == "busy"


def test_record_and_last_auto(monkeypatch):
    monkeypatch.setattr(update, "_LAST_AUTO", None)
    assert update.last_auto() is None
    update.record_auto("up-to-date")
    la = update.last_auto()
    assert la is not None and la["result"] == "up-to-date"
    assert isinstance(la["ts"], float)


def test_apply_manual_cooldown_prevents_double_spawn(monkeypatch, tmp_path):
    # Hermes #539: apply() returns right after the detached spawn, so without the cooldown
    # a double-click / retried POST would launch a second installer while the first is
    # still building. The second call must report busy and spawn nothing.
    inst = tmp_path / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    monkeypatch.setattr(update, "_SPAWNED_AT", None)
    spawns: list[object] = []
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: spawns.append(argv) or SimpleNamespace()
    )
    assert update.apply_manual() == "started"
    assert update.apply_manual() == "busy"
    assert len(spawns) == 1
