"""Self-update (#65 Phase 5): version check + no-input apply."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    # apply() now resolves the target tag to verify it against the release manifest
    # (#612), so stub the remote lookup — these tests are about the spawn, not the network.
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": "a" * 40})
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
    # apply() now resolves the target tag to verify it against the release manifest
    # (#612), so stub the remote lookup — these tests are about the spawn, not the network.
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": "a" * 40})
    captured = {}
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: captured.update(kw) or SimpleNamespace()
    )
    assert update.apply() is True
    # The stale value is gone — that is the original guarantee and it still holds.
    assert captured["env"]["AGENT_SESSIONS_REF"] != "old-pinned-ref"
    # What replaces it is the tag this process just resolved AND verified. Asserting only
    # the absence of the stale ref would now pass against an installer left free to resolve
    # the tag a second time on its own, which is the hole #820's review found: the check and
    # the build have to name the same object.
    assert captured["env"]["AGENT_SESSIONS_REF"] == "v9.9.9"
    assert captured["env"]["AGENT_SESSIONS_EXPECT_COMMIT"] == "a" * 40
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
    # apply() now resolves the target tag to verify it against the release manifest
    # (#612), so stub the remote lookup — these tests are about the spawn, not the network.
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": "a" * 40})
    spawns: list[object] = []
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: spawns.append(argv) or SimpleNamespace()
    )
    assert update.apply_manual() == "started"
    assert update.apply_manual() == "busy"
    assert len(spawns) == 1


# ---- release-tag verification against the committed manifest (#612 Phase 1) -----

_REPO = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO / "scripts" / "release-manifest.json"


#: The remote every fake-manifest test speaks about, so the key and the lookup agree.
_TEST_REMOTE = "https://example.invalid/org/repo.git"


def _fake_manifest(monkeypatch, tmp_path, releases: dict, *, remote: str = _TEST_REMOTE):
    """Write a schema-2 manifest whose entries are keyed by ``remote``.

    Keying is not decoration: a manifest generated against one remote and checked against
    another reports every legitimate release as moved, which the updater turns into a refusal.
    Tests therefore have to name the remote they mean, the same way production does.
    """
    key = update.remote_key(remote)
    p = tmp_path / "release-manifest.json"
    p.write_text(json.dumps({"version": 2, "releases": {t: {key: e} for t, e in releases.items()}}))
    monkeypatch.setattr(update, "manifest_path", lambda: p)


def _fake_ls_remote(monkeypatch, stdout: str, returncode: int = 0):
    monkeypatch.setattr(update.shutil, "which", lambda _n: "/usr/bin/git")
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout),
    )


def test_verify_refuses_a_tag_that_moved(monkeypatch, tmp_path):
    """A released tag re-pointed at a different commit is refused.

    This is the whole attack the manifest exists to catch: anyone who can write to the forge
    can move `v0.12.0` to new code, and every install tracking `stable` would take it with no
    diff, no review, and no version change.
    """
    _fake_manifest(monkeypatch, tmp_path, {"v0.12.0": {"object": "aaa", "commit": "bbb"}})
    _fake_ls_remote(monkeypatch, "eee\trefs/tags/v0.12.0\nfff\trefs/tags/v0.12.0^{}\n")
    ok, reason = update.verify_release_tag("v0.12.0", _TEST_REMOTE)
    assert ok is False
    assert "has moved" in reason and "aaa" in reason and "eee" in reason


def test_verify_accepts_a_tag_that_still_matches(monkeypatch, tmp_path):
    _fake_manifest(monkeypatch, tmp_path, {"v0.12.0": {"object": "aaa", "commit": "bbb"}})
    _fake_ls_remote(monkeypatch, "aaa\trefs/tags/v0.12.0\nbbb\trefs/tags/v0.12.0^{}\n")
    ok, reason = update.verify_release_tag("v0.12.0", _TEST_REMOTE)
    assert ok is True
    assert "verified" in reason


def test_verify_allows_a_tag_newer_than_the_manifest(monkeypatch, tmp_path):
    """An unknown tag passes — and this is the design, not a hole.

    The manifest ships inside the repo, so a running build's copy can never contain an entry
    for a release cut afterwards. Failing closed here would not be strict: it would mean no
    install ever auto-updates again, because every genuine update is by construction a tag
    this build has not heard of. What is bought is retroactive-mutation detection; what is
    not bought is vouching for a brand-new tag, which needs signatures.
    """
    _fake_manifest(monkeypatch, tmp_path, {"v0.12.0": {"object": "aaa", "commit": "bbb"}})
    _fake_ls_remote(monkeypatch, "zzz\trefs/tags/v0.13.0\n")
    ok, reason = update.verify_release_tag("v0.13.0", _TEST_REMOTE)
    assert ok is True
    assert "not in manifest" in reason


def test_verify_degrades_open_without_a_manifest_or_a_reachable_remote(monkeypatch, tmp_path):
    """Absence of evidence is not evidence of tampering.

    A source checkout, a release predating the manifest, or an unreachable remote must not
    strand an install — none of them is an attack signal, and refusing on them would break
    legitimate updates while blocking nothing.
    """
    monkeypatch.setattr(update, "manifest_path", lambda: None)
    assert update.verify_release_tag("v0.12.0", _TEST_REMOTE)[0] is True

    _fake_manifest(monkeypatch, tmp_path, {"v0.12.0": {"object": "aaa", "commit": "bbb"}})
    _fake_ls_remote(monkeypatch, "", returncode=1)
    ok, reason = update.verify_release_tag("v0.12.0", _TEST_REMOTE)
    assert ok is True and "could not be resolved" in reason


def test_apply_refuses_to_spawn_the_installer_for_a_moved_tag(monkeypatch, tmp_path):
    """The gate is on `apply()`, and it must stop the spawn — not merely report.

    Verification has to happen BEFORE the installer starts: afterwards this process has no
    say at all, because the installer resolves the ref itself and is about to restart us.
    """
    inst = tmp_path / "install.sh"
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update, "installer_path", lambda: inst)
    monkeypatch.setattr(update, "_channel", lambda: "stable")
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    # apply() now performs the tag's single remote lookup itself and hands the result to
    # verification, so the network has to be stubbed here rather than short-circuited by
    # stubbing verify_release_tag alone.
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": "0" * 40})
    monkeypatch.setattr(update, "verify_release_tag", lambda *a, **k: (False, "v9.9.9 has moved"))

    spawned = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert update.apply() is False
    assert spawned == [], "the installer was spawned despite a failed verification"
    # A refusal must be visible: `apply()` returning False otherwise reads exactly like
    # "no installer here", and a silently-not-updating install is what an attacker wants.
    assert "has moved" in str(update.check().get("blocked", ""))


def test_apply_does_not_gate_the_main_channel(monkeypatch, tmp_path):
    """`main` tracks a branch by design — there is no tag to verify, so nothing is gated."""
    inst = tmp_path / "install.sh"
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setattr(update, "installer_path", lambda: inst)
    monkeypatch.setattr(update, "_channel", lambda: "main")
    monkeypatch.setattr(
        update, "verify_release_tag", lambda *a, **k: (False, "should not be called")
    )
    spawned = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert update.apply() is True
    assert len(spawned) == 1
    update._SPAWNED_AT = None  # module global — don't leak the cooldown into other tests


def test_committed_manifest_is_keyed_by_remote_and_covers_the_default_one():
    """The shipped manifest speaks about the remote installs actually update from.

    This is the regression for the outage found in review: the manifest used to be generated
    from the local forge checkout while `_DEFAULT_REPO` is the public mirror, and those are
    **different objects** — the mirror is a snapshot publish, so its `v0.19.2` is a lightweight
    tag at an entirely different commit. Comparing one remote's record with the other's tag
    made the updater refuse the legitimate current release, i.e. a fleet-wide auto-update
    outage. Asserting the key here is what stops a future regeneration from silently
    reintroducing it.
    """
    doc = json.loads(_MANIFEST.read_text())
    assert doc["version"] == 2, "schema 1 is flat and cannot express per-remote identity"
    releases = doc["releases"]
    assert len(releases) > 10, "the manifest looks truncated"

    default_key = update.remote_key(update._DEFAULT_REPO)
    covered = [t for t, per_remote in releases.items() if default_key in per_remote]
    assert covered, (
        f"no entry is keyed for the DEFAULT remote {default_key!r} — the shipped manifest "
        "would verify nothing on an unconfigured install"
    )
    for tag, per_remote in releases.items():
        for key, entry in per_remote.items():
            assert key == key.lower() and "://" not in key, f"{tag}: unnormalised key {key!r}"
            assert set(entry) == {"object", "commit"}, f"{tag}/{key}: unexpected fields"
            for field, sha in entry.items():
                assert len(sha) == 40 and all(
                    c in "0123456789abcdef" for c in sha
                ), f"{tag}/{key}/{field} is not a full sha: {sha!r}"


def test_a_manifest_for_another_remote_never_reports_a_release_as_moved():
    """The outage, reduced to one assertion.

    A record for remote A must not be compared against remote B's tag — the honest answer is
    "not verified", never "moved". `verify_release_tag` returning False here is what took the
    fleet's auto-updates down, so this asserts the *reason*, not just the verdict.
    """
    doc = json.loads(_MANIFEST.read_text())
    tag = next(iter(doc["releases"]))
    ok, reason = update.verify_release_tag(tag, "https://elsewhere.invalid/someone/agent-sessions")
    assert ok is True
    assert "no trust record for remote" in reason


# ---- the verified tag and the built commit must be one decision (#612, review) ----


def _apply_env(monkeypatch, tmp_path):
    """A minimal on-disk install so apply() gets past installer_path()."""
    inst = tmp_path / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    monkeypatch.setattr(update, "_SPAWNED_AT", None)
    return inst


def _apply_harness(monkeypatch, tmp_path, *, latest, pin, manifest=None):
    """Run apply() with the network stubbed out and return the child's env.

    Everything stubbed here exists independently of this change (`latest_ref`,
    `verify_release_tag`, `load_manifest`, `remote_tag_shas`), so these tests run against the
    pre-fix code too and fail on its *behaviour* — not on a new symbol being absent, which
    would prove only that the symbol is new.
    """
    inst = tmp_path / "current" / "src" / "install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENT_SESSIONS_HOME", str(tmp_path))
    monkeypatch.setattr(update, "_SPAWNED_AT", None)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: latest)
    monkeypatch.setattr(update, "verify_release_tag", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(update, "load_manifest", lambda: manifest or {})
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {"commit": pin} if pin else {})
    captured = {}
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: captured.update(kw) or SimpleNamespace()
    )
    assert update.apply() is True
    return captured["env"]


def test_apply_hands_the_installer_the_exact_commit_it_verified(monkeypatch, tmp_path):
    """The installer must not be left to resolve the tag a second time.

    `apply()` verifies a tag, then spawns an installer that used to run its own `ls-remote`
    and take the highest tag again. Everything verified then described a lookup the build
    never used: a tag moved in between, or a higher tag published in between, was built
    unverified. Passing both the name and the immutable commit makes the two one decision.
    """
    env = _apply_harness(monkeypatch, tmp_path, latest="v9.9.9", pin="b" * 40)
    assert env["AGENT_SESSIONS_REF"] == "v9.9.9"
    assert env["AGENT_SESSIONS_EXPECT_COMMIT"] == "b" * 40


def test_apply_pins_the_manifest_commit_rather_than_the_remotes_answer(monkeypatch, tmp_path):
    """The pin comes from the reviewed record when there is one, not from the remote.

    The remote is the thing an attacker would have rewritten; the manifest is the reviewed
    statement of what that release was. They can only disagree in a case `verify_release_tag`
    has already refused, so this never arbitrates a live contradiction — it just must not be
    the weaker of the two by construction.
    """
    env = _apply_harness(
        monkeypatch,
        tmp_path,
        latest="v9.9.9",
        pin="e" * 40,
        manifest={"v9.9.9": {"object": "c" * 40, "commit": "d" * 40}},
    )
    assert env["AGENT_SESSIONS_EXPECT_COMMIT"] == "d" * 40


def test_apply_pins_the_remote_commit_for_a_tag_the_manifest_never_saw(monkeypatch, tmp_path):
    """A brand-new release has no manifest entry — it still gets bound to what we resolved.

    This is the bootstrap case the module docstring calls out: a running build's manifest can
    never contain a release cut after it. The remote's answer is not a *reviewed* value, but
    pinning it still means the installer builds the object this process saw rather than
    whatever the name points at moments later.
    """
    env = _apply_harness(monkeypatch, tmp_path, latest="v9.9.9", pin="f" * 40, manifest={})
    assert env["AGENT_SESSIONS_EXPECT_COMMIT"] == "f" * 40


def test_apply_refuses_to_spawn_without_a_commit_to_check_against(monkeypatch, tmp_path):
    """No pin, no spawn. The earlier version of this test asserted the opposite — and was wrong.

    It read the empty lookup as "absence of evidence" and degraded open, by analogy with the
    missing-manifest case. The analogy does not hold: a missing manifest entry is structural
    and unavoidable for every new release, while an empty *remote* lookup is transient and
    reachable by anyone who can write tags — break the ref, let the update spawn unpinned,
    then repoint it before the clone. `install.sh` skips the comparison entirely when
    AGENT_SESSIONS_EXPECT_COMMIT is empty, so that spawn is an unverified build on demand.
    Refusing costs a postponed update; the next cycle retries. Caught in review on this PR.
    """
    _apply_env(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "verify_release_tag", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(update, "load_manifest", lambda: {})
    monkeypatch.setattr(update, "remote_tag_shas", lambda _t, _u: {})  # lookup comes back empty
    spawns: list[object] = []
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: spawns.append(argv) or SimpleNamespace()
    )
    assert update.apply() is False
    assert spawns == [], "an installer was spawned with nothing to verify the clone against"
    assert "could not be resolved to a commit" in str(update.check().get("blocked", ""))


def test_apply_refuses_to_spawn_when_no_tag_resolves(monkeypatch, tmp_path):
    """The other no-pin door: an unresolvable tag must not reach Popen either.

    `verify_release_tag("")` treats the empty tag as unknown and passes it, so without an
    explicit guard a failed selection sailed straight through to an installer that would then
    resolve the tag itself — entirely unverified.
    """
    # The install exists; what is missing is the tag.
    _apply_env(monkeypatch, tmp_path)
    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: None)
    spawns: list[object] = []
    monkeypatch.setattr(
        update.subprocess, "Popen", lambda argv, **kw: spawns.append(argv) or SimpleNamespace()
    )
    assert update.apply() is False
    assert spawns == []


def test_the_remote_is_looked_up_once_per_update_decision(monkeypatch, tmp_path):
    """Verification and the pin must describe the same observation, not two of them.

    A forward guard, stated honestly: the pre-fix code also made exactly one lookup, because
    its two paths were mutually exclusive (a manifest hit consulted the remote and then used
    the manifest's commit; a miss skipped the remote in verification and consulted it for the
    pin). Nothing was broken here. What changed is that the single lookup is now explicit and
    shared rather than an accident of which branch ran, and this pins it that way — resolving
    a mutable name twice is precisely how a verified answer and a built answer come apart, so
    it should not be re-introducible by an edit that looks locally harmless.
    """
    _apply_env(monkeypatch, tmp_path)
    calls: list[str] = []

    def counting(tag, _url):
        calls.append(tag)
        return {"object": "b" * 40, "commit": "b" * 40}

    monkeypatch.setattr(update, "latest_ref", lambda _c, _u: "v9.9.9")
    monkeypatch.setattr(update, "load_manifest", lambda: {})
    monkeypatch.setattr(update, "remote_tag_shas", counting)
    monkeypatch.setattr(update.subprocess, "Popen", lambda argv, **kw: SimpleNamespace())
    assert update.apply() is True
    assert calls == ["v9.9.9"], f"the remote was consulted {len(calls)} times: {calls}"
