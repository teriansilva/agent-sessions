"""Real signature checks, feed rollback/freeze resistance and closed recipes (#1259)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

import test_plugins
from agent_sessions.plugins import feed, manifest, runner, storage

NOW = 1_790_000_000


def entry():
    doc = test_plugins.doc()
    doc["identity"]["id"] = "fixture"
    doc["binary"].update(name="fixture", aliases=[])
    doc["install"] = {
        "kind": "npm-prefix",
        "authority": "registry.npmjs.org",
        "package": "fixture",
        "version": "1.0.0",
        "digest": "sha256:" + "a" * 64,
        "entrypoint": "node_modules/fixture/bin/fixture",
    }
    return {
        "manifest": doc,
        "recipe": {
            "artifacts": [
                {
                    "url": "https://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz",
                    "sha256": "a" * 64,
                    "destination": "node_modules/fixture",
                }
            ]
        },
    }


def document(sequence=1):
    return {
        "contract": 1,
        "sequence": sequence,
        "issued_at": NOW - 60,
        "expires_at": NOW + 86400,
        "plugins": [entry()],
    }


@pytest.fixture
def signer(tmp_path, monkeypatch):
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    allowed = tmp_path / "allowed"
    allowed.write_text(feed.PRINCIPAL + " " + key.with_suffix(".pub").read_text())
    monkeypatch.setattr(feed, "SIGNERS", allowed)
    monkeypatch.setenv("AGENT_SESSIONS_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_SESSIONS_PLUGINS_DIR", str(tmp_path / "plugins"))

    def sign(data, namespace=feed.NAMESPACE):
        return subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
            input=data,
            capture_output=True,
            check=True,
        ).stdout

    return sign


@pytest.mark.deploy_shape
def test_packaged_trust_root_exists_and_matches_reviewed_release_root():
    assert feed.SIGNERS.is_file()
    source = Path(__file__).parents[1] / "scripts" / "release-signers"
    assert feed.SIGNERS.read_bytes() == source.read_bytes()


def test_real_signature_acceptance_and_restart_read(signer):
    data = feed.canonical(document())
    accepted = feed.accept(data, signer(data), now=NOW)
    assert accepted.sequence == 1
    assert accepted.entries[0].manifest.id == "fixture"
    assert feed.current(now=NOW).digest == hashlib.sha256(data).hexdigest()
    feed.accept(data, signer(data), now=NOW)  # same bytes/sequence is idempotent
    with pytest.raises(feed.FeedError, match="expired"):
        feed.current(now=NOW + 86400)


@pytest.mark.parametrize("change", ["payload", "namespace", "principal", "key"])
def test_signature_rejects_every_wrong_binding(signer, monkeypatch, tmp_path, change):
    data = feed.canonical(document())
    signature = signer(data, "git" if change == "namespace" else feed.NAMESPACE)
    if change == "payload":
        data = feed.canonical(document(2))
    elif change == "principal":
        monkeypatch.setattr(feed, "PRINCIPAL", "someone-else")
    elif change == "key":
        other = tmp_path / "other"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True
        )
        feed.SIGNERS.write_text(feed.PRINCIPAL + " " + other.with_suffix(".pub").read_text())
    with pytest.raises(feed.FeedError, match="signature"):
        feed.accept(data, signature, now=NOW)
    assert not (storage.root() / "feed.json").exists()


def test_high_water_mark_rejects_rollback_and_same_sequence_equivocation(signer):
    data = feed.canonical(document(3))
    feed.accept(data, signer(data), now=NOW)
    for doc in [document(2), {**document(3), "expires_at": NOW + 1000}]:
        bad = feed.canonical(doc)
        with pytest.raises(feed.FeedError, match="backwards|different"):
            feed.accept(bad, signer(bad), now=NOW)
        assert feed.current(now=NOW).digest == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("issued_at", NOW + 301),
        ("expires_at", NOW),
        ("expires_at", NOW + feed.MAX_AGE),
        ("contract", True),
        ("sequence", 0),
        ("sequence", 1.0),
        ("extra", "ignored?"),
    ],
)
def test_bad_feed_contracts_fail_closed(field, value):
    doc = document()
    doc[field] = value
    with pytest.raises(ValueError):
        feed.parse(feed.canonical(doc), now=NOW)


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b'{ "a":1}', b'{"a":NaN}', b"{}\n", b"\xff"])
def test_noncanonical_or_ambiguous_json_is_refused(data):
    with pytest.raises(feed.FeedError):
        feed.decode(data)


def test_surrogate_label_is_refused_before_it_can_reach_an_api_response():
    doc = document()
    doc["plugins"][0]["manifest"]["identity"]["label"] = "bad\ud800"
    with pytest.raises(feed.FeedError):
        feed.parse(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(), now=NOW)


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz",
        "https://registry.npmjs.org.evil.test/fixture/-/fixture-1.0.0.tgz",
        "https://evil@registry.npmjs.org/fixture/-/fixture-1.0.0.tgz",
        "https://registry.npmjs.org:444/fixture/-/fixture-1.0.0.tgz",
        "https://registry.npmjs.org/fixture/-/fixture-1.0.0.tgz?other=1",
        "https://registry.npmjs.org/fixture/-/%2e%2e/file.tgz",
        "https://registry.npmjs.org/fixture/-/fixture-2.0.0.tgz",
    ],
)
def test_artifact_authority_and_primary_coordinates_are_bound(url):
    doc = entry()
    doc["recipe"]["artifacts"][0]["url"] = url
    with pytest.raises(feed.FeedError):
        feed.entry(doc, signed=False)


def test_incomplete_or_conflicting_recipe_is_refused():
    for change in ["digest", "duplicate", "destination", "unknown"]:
        doc = entry()
        artifact = doc["recipe"]["artifacts"][0]
        if change == "digest":
            artifact["sha256"] = "b" * 64
        elif change == "duplicate":
            doc["recipe"]["artifacts"].append(copy.deepcopy(artifact))
        elif change == "destination":
            artifact["destination"] = "node_modules/../escape"
        else:
            artifact["command"] = "run-me"
        with pytest.raises(feed.FeedError):
            feed.entry(doc, signed=False)


def test_local_identity_cannot_claim_in_tree_or_route_names():
    for identity in ["defaults", "gemini"]:
        doc = entry()
        doc["manifest"]["identity"]["id"] = identity
        with pytest.raises(ValueError):
            feed.entry(doc, signed=False)
    doc = test_plugins.doc()
    doc["identity"]["id"] = "defaults"
    with pytest.raises(manifest.ManifestError, match="reserved"):
        manifest.parse(doc)


def test_signed_runtime_manifest_does_not_gain_shell_exemption():
    doc = entry()
    doc["manifest"]["binary"].update(name="bash", aliases=["bash", "fixture"])
    with pytest.raises(Exception, match="shell"):
        feed.entry(doc, signed=True)


def test_private_state_refuses_symlink_and_foreign_writable_directory(signer, tmp_path):
    data = feed.canonical(document())
    signature = signer(data)
    real = tmp_path / "elsewhere"
    real.mkdir()
    storage.root().symlink_to(real)
    with pytest.raises((OSError, ValueError)):
        feed.accept(data, signature, now=NOW)
    assert list(real.iterdir()) == []


def test_malformed_high_water_record_is_preserved(signer):
    data = feed.canonical(document())
    with storage.locked("feed.json") as path:
        storage.write(path, {"sequence": "broken"})
    before = path.read_bytes()
    with pytest.raises(feed.FeedError, match="malformed"):
        feed.accept(data, signer(data), now=NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "argv",
    [
        [sys.executable, "-c", "import os,time; os.fork() or os.setsid(); time.sleep(60)"],
        ["/usr/bin/ssh-keygen", "-Y", "verify", "-f", "/tmp/key", "-O", "arbitrary"],
        ["/usr/bin/ssh-keygen", "-t", "ed25519"],
    ],
)
def test_runner_refuses_other_programs_and_ssh_modes_before_spawn(monkeypatch, argv):
    def never(*a, **kw):
        pytest.fail("an unsupported executable or argv reached Popen")

    monkeypatch.setattr(runner.subprocess, "Popen", never)
    with pytest.raises(runner.StepError, match="fixed system SSH"):
        runner.run(argv)


def test_runner_bounds_real_ssh_output_time_and_moves_large_input(signer, tmp_path):
    data = b"x" * (1024 * 1024)
    sig = tmp_path / "signature"
    sig.write_bytes(signer(data))
    argv = [
        "/usr/bin/ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(feed.SIGNERS),
        "-I",
        feed.PRINCIPAL,
        "-n",
        feed.NAMESPACE,
        "-s",
        str(sig),
    ]
    result = runner.run(argv, data=data)
    assert result.code == 0 and b"Good" in result.output
    with pytest.raises(runner.StepError, match="output"):
        runner.run(argv, data=data, max_output=4)
    start = time.monotonic()
    with pytest.raises(runner.StepError, match="time"):
        runner.run(argv, data=data, timeout=0.000001)
    assert time.monotonic() - start < 3


@pytest.mark.parametrize("field", ["digest", "sequence", "data", "signature", "extra"])
def test_new_signed_feed_cannot_overwrite_corrupt_saved_evidence(signer, field):
    data = feed.canonical(document())
    feed.accept(data, signer(data), now=NOW)
    with storage.locked("feed.json") as path:
        old = storage.read(path)
        if field == "digest":
            old[field] = "0" * 64
        elif field == "sequence":
            old[field] = 0
        elif field == "data":
            old[field] = feed.canonical(document(2)).decode("ascii")
        else:
            old[field] = "broken"
        storage.write(path, old)
    before = path.read_bytes()
    fresh = feed.canonical(document(3))
    with pytest.raises(feed.FeedError):
        feed.accept(fresh, signer(fresh), now=NOW)
    assert path.read_bytes() == before


def test_expired_but_intact_saved_feed_can_advance(signer):
    old = feed.canonical(document())
    feed.accept(old, signer(old), now=NOW)
    later = NOW + 86401
    fresh = feed.canonical({**document(2), "issued_at": later, "expires_at": later + 1000})
    assert feed.accept(fresh, signer(fresh), now=later).sequence == 2
    assert feed.current(now=later).digest == hashlib.sha256(fresh).hexdigest()


def test_feed_builder_produces_exact_bytes_and_refuses_to_replace_a_cut(tmp_path):
    directory = tmp_path / "entries"
    directory.mkdir()
    (directory / "fixture.json").write_text(json.dumps(entry()))
    output = tmp_path / "cut"
    script = Path(__file__).parents[1] / "scripts/build-plugin-feed"
    argv = [
        sys.executable,
        str(script),
        str(directory),
        str(output),
        "--sequence",
        "1",
        "--issued-at",
        str(NOW - 60),
        "--valid-for",
        "86460",
    ]
    subprocess.run(argv, check=True, capture_output=True)
    assert (output / "plugin-feed.json").read_bytes() == feed.canonical(document())
    again = subprocess.run(argv, capture_output=True)
    assert again.returncode != 0
    assert (output / "plugin-feed.json").read_bytes() == feed.canonical(document())


@pytest.fixture
def builder():
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts/build-plugin-feed"))


@pytest.mark.parametrize("collision", ["signature", "feed", "empty", "file", "symlink"])
def test_feed_cut_collision_preserves_every_existing_byte(builder, tmp_path, collision):
    output = tmp_path / "cut"
    if collision == "file":
        output.write_bytes(b"existing")
    elif collision == "symlink":
        output.symlink_to(tmp_path / "absent")
    else:
        output.mkdir()
        if collision != "empty":
            name = "plugin-feed.json.sig" if collision == "signature" else "plugin-feed.json"
            (output / name).write_bytes(b"existing")
    before = [(p.name, p.read_bytes()) for p in output.iterdir()] if output.is_dir() else []
    with pytest.raises(FileExistsError):
        builder["publish"](output, b"new feed", b"new signature")
    if output.is_dir():
        assert [(p.name, p.read_bytes()) for p in output.iterdir()] == before
    elif collision == "file":
        assert output.read_bytes() == b"existing"
    else:
        assert output.is_symlink()
    assert not list(tmp_path.glob(".plugin-feed-stage-*"))


@pytest.mark.parametrize("when", ["before", "after"])
def test_abrupt_publication_interruption_exposes_no_partial_pair(builder, signer, tmp_path, when):
    data = feed.canonical(document())
    signature = signer(data)
    output = tmp_path / "cut"
    publish = builder["publish"]
    original = publish.__globals__["_publish_directory"]
    pid = os.fork()
    if pid == 0:

        def interrupted(staged, destination):
            if when == "after":
                original(staged, destination)
            os._exit(73)

        publish.__globals__["_publish_directory"] = interrupted
        publish(output, data, signature)
        os._exit(74)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    if when == "before":
        assert not output.exists()
        # The abandoned hidden stage does not block an ordinary rerun.
        publish(output, data, signature)
    assert set(p.name for p in output.iterdir()) == {"plugin-feed.json", "plugin-feed.json.sig"}
    feed.verify(
        (output / "plugin-feed.json").read_bytes(), (output / "plugin-feed.json.sig").read_bytes()
    )


def test_raced_empty_destination_is_not_replaced(builder, tmp_path, monkeypatch):
    publish = builder["publish"]
    original = publish.__globals__["_publish_directory"]
    output = tmp_path / "cut"

    def raced(staged, destination):
        destination.mkdir()
        original(staged, destination)

    monkeypatch.setitem(publish.__globals__, "_publish_directory", raced)
    with pytest.raises(FileExistsError):
        publish(output, b"feed", b"signature")
    assert list(output.iterdir()) == []
    assert not list(tmp_path.glob(".plugin-feed-stage-*"))


def test_failed_signature_write_cleans_only_its_unpublished_stage(builder, tmp_path, monkeypatch):
    original = Path.open

    def fail(path, *args, **kwargs):
        if path.name == "plugin-feed.json.sig":
            raise OSError("injected disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    output = tmp_path / "cut"
    with pytest.raises(OSError, match="disk failure"):
        builder["publish"](output, b"feed", b"signature")
    assert not output.exists()
    assert not list(tmp_path.glob(".plugin-feed-stage-*"))
