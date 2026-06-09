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


def test_check_available_and_up_to_date(monkeypatch):
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v0.2.0")
    monkeypatch.delenv("AGENT_SESSIONS_CHANNEL", raising=False)
    monkeypatch.setattr(update, "get_version", lambda: "0.1.0")
    assert update.check()["update_available"] is True
    monkeypatch.setattr(update, "get_version", lambda: "0.2.0")
    assert update.check()["update_available"] is False  # tag == running version


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
