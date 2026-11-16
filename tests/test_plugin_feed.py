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
from agent_sessions.plugins import feed, freshness, manifest, runner, storage

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
        # #1730: a cut may be approved for up to 90 days (a long one needs a freshness statement
        # to be used); beyond that it is malformed.
        ("expires_at", NOW + freshness.MAX_CONTENT_AGE),
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


def test_atomic_state_replacement_keeps_an_already_open_snapshot_readable(signer, monkeypatch):
    with storage.locked("fixture.json") as path:
        storage.write(path, {"revision": "before"})
    original = storage.provenance.open_verified

    def replace_after_open(raw, **kwargs):
        fd, st = original(raw, **kwargs)
        if str(raw) == str(path):
            with storage.locked("fixture.json") as locked:
                storage.write(locked, {"revision": "after"})
            assert os.fstat(fd).st_nlink == 0
        return fd, st

    monkeypatch.setattr(storage.provenance, "open_verified", replace_after_open)
    assert storage.read(path) == {"revision": "before"}
    monkeypatch.setattr(storage.provenance, "open_verified", original)
    assert storage.read(path) == {"revision": "after"}
    os.link(path, path.with_name("alias.json"))
    with pytest.raises(storage.StateError):
        storage.read(path)


def test_state_limit_measures_the_formatted_bytes_before_replacement(signer, monkeypatch):
    doc = {"rows": [{"label": "sample"}] * 20}
    with storage.locked("fixture.json") as path:
        storage.write(path, {"previous": True})
        previous = path.read_bytes()
        monkeypatch.setattr(storage, "MAX_STATE_BYTES", len(json.dumps(doc).encode()))
        with pytest.raises(storage.StateError, match="too large"):
            storage.write(path, doc)
    assert path.read_bytes() == previous
    assert storage.read(path) == {"previous": True}


@pytest.mark.parametrize("override", [False, True])
def test_state_at_the_exact_serialized_limit_reads_back(signer, monkeypatch, override):
    doc = {"z": ["\u00e9", "\n"], "a": {"nested": True}}
    encoded = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
    monkeypatch.setattr(storage, "MAX_STATE_BYTES", len(encoded) - int(override))
    options = {"max_bytes": len(encoded)} if override else {}
    with storage.locked("fixture.json") as path:
        storage.write(path, doc, **options)
        with pytest.raises(storage.StateError):
            storage.write(path, doc, max_bytes=len(encoded) - 1)
    assert path.read_bytes() == encoded
    assert storage.read(path, **options) == doc
    with pytest.raises(storage.StateError):
        storage.read(path, max_bytes=len(encoded) - 1)


# ---- the agents-only catalog key (#2082) ---------------------------------------------------------

#: The reviewed catalog keys, exactly as listed in the shipped `catalog-signers`. EMPTY until the
#: operator's approval is recorded on #2082 and the key exists (battlelab-cloud#98): adding the key
#: is one deliberate line here, beside the one line in the signers file.
CATALOG_KEYS: list[str] = []
SHIPPED_CATALOG_SIGNERS = feed.CATALOG_SIGNERS


def _entries(path):
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _keypair(tmp_path, name):
    key = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    return key


def _sign_with(key, data, namespace=feed.NAMESPACE):
    return subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def catalog(signer, tmp_path, monkeypatch):
    """A catalog key listed, as shipped entries must be, in a scratch `catalog-signers`."""
    key = _keypair(tmp_path, "catalog")
    allowed = tmp_path / "catalog-signers"
    allowed.write_text(
        f'{feed.CATALOG_PRINCIPAL} namespaces="{feed.NAMESPACE}" '
        + key.with_suffix(".pub").read_text()
    )
    monkeypatch.setattr(feed, "CATALOG_SIGNERS", allowed)

    def sign(data, namespace=feed.NAMESPACE):
        return _sign_with(key, data, namespace)

    sign.key = key
    return sign


def _state():
    """Every trust record's bytes (lock files carry none)."""
    root = storage.root()
    if not root.exists():
        return {}
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.suffix != ".lock"}


@pytest.mark.deploy_shape
def test_shipped_catalog_signers_lists_exactly_the_reviewed_keys():
    assert SHIPPED_CATALOG_SIGNERS.is_file()
    entries = _entries(SHIPPED_CATALOG_SIGNERS)
    assert entries == CATALOG_KEYS
    for line in entries:  # agents cuts only, even where the file is misused
        assert line.startswith(f'{feed.CATALOG_PRINCIPAL} namespaces="{feed.NAMESPACE}" ')
    assert feed.CATALOG_PRINCIPAL == "catalog@battlelab"


def test_a_catalog_signed_cut_is_accepted_and_reads_back_after_a_restart(catalog):
    data = feed.canonical(document())
    assert feed.accept(data, catalog(data), now=NOW).sequence == 1
    assert feed.current(now=NOW).digest == hashlib.sha256(data).hexdigest()
    # A legacy cache (no floor file yet) backfills its floor from catalog-signed evidence too.
    (storage.root() / "feed-floor.json").unlink()
    assert feed.historical().sequence == 1
    assert storage.read(storage.root() / "feed-floor.json")["sequence"] == 1


@pytest.mark.parametrize("client", ["shipped", "absent"])
def test_an_older_client_refuses_a_catalog_cut_and_keeps_every_record(
    catalog, signer, monkeypatch, tmp_path, client
):
    old = feed.canonical(document(1))
    feed.accept(old, signer(old), now=NOW)
    before = _state()
    path = SHIPPED_CATALOG_SIGNERS if client == "shipped" else tmp_path / "absent"
    monkeypatch.setattr(feed, "CATALOG_SIGNERS", path)
    calls = []
    original = runner.run
    monkeypatch.setattr(runner, "run", lambda *a, **kw: calls.append(a) or original(*a, **kw))
    new = feed.canonical(document(2))
    with pytest.raises(feed.FeedError, match="not from a trusted plugin-feed signer"):
        feed.accept(new, catalog(new), now=NOW)
    assert len(calls) == 1  # an empty catalog-signers spawns no second check
    assert _state() == before
    assert feed.current(now=NOW).digest == hashlib.sha256(old).hexdigest()


def test_both_signers_share_one_sequence_floor(catalog, signer):
    two = feed.canonical(document(2))
    feed.accept(two, signer(two), now=NOW)
    three = feed.canonical(document(3))
    feed.accept(three, catalog(three), now=NOW)
    assert feed.current(now=NOW).digest == hashlib.sha256(three).hexdigest()
    other_three = feed.canonical({**document(3), "expires_at": NOW + 1000})
    for data, sign in [
        (two, catalog),
        (two, signer),
        (feed.canonical(document(1)), catalog),
        (other_three, catalog),
        (other_three, signer),
    ]:
        before = _state()
        with pytest.raises(feed.FeedError, match="backwards|different"):
            feed.accept(data, sign(data), now=NOW)
        assert _state() == before
    feed.accept(three, catalog(three), now=NOW)  # the identical cut again is idempotent
    # Recovery is by sequence: a higher release-signed cut supersedes the catalog key's.
    four = feed.canonical(document(4))
    assert feed.accept(four, signer(four), now=NOW).sequence == 4


@pytest.mark.parametrize("misplacement", ["release-principal", "release-file", "namespace"])
def test_the_catalog_key_verifies_only_under_its_own_principal_file_and_namespace(
    catalog, monkeypatch, tmp_path, misplacement
):
    data = feed.canonical(document())
    namespace = feed.NAMESPACE
    pub = catalog.key.with_suffix(".pub").read_text()
    if misplacement == "release-principal":
        feed.CATALOG_SIGNERS.write_text(f"{feed.PRINCIPAL} {pub}")
    elif misplacement == "release-file":
        with feed.SIGNERS.open("a") as f:
            f.write(f"{feed.CATALOG_PRINCIPAL} {pub}")
        monkeypatch.setattr(feed, "CATALOG_SIGNERS", tmp_path / "absent")
    else:
        namespace = "battlelab-gallery"
    with pytest.raises(feed.FeedError, match="signature"):
        feed.accept(data, catalog(data, namespace), now=NOW)
    assert not (storage.root() / "feed.json").exists()


def test_the_catalog_key_signs_no_gallery_index_and_no_freshness_statement(
    catalog, signer, monkeypatch, tmp_path
):
    from agent_sessions.gallery import contract, trust

    # An unrestricted line: refusal must come from which file each verifier reads, not namespaces.
    feed.CATALOG_SIGNERS.write_text(
        f"{feed.CATALOG_PRINCIPAL} " + catalog.key.with_suffix(".pub").read_text()
    )
    fresh_key = _keypair(tmp_path, "freshness")
    allowed = tmp_path / "freshness-signers"
    allowed.write_text(freshness.PRINCIPAL + " " + fresh_key.with_suffix(".pub").read_text())
    monkeypatch.setattr(freshness, "SIGNERS", allowed)
    data = b'{"any":"bytes"}'
    trust.verify(data, signer(data, contract.NAMESPACE))  # controls: each real signer passes
    freshness.verify(data, _sign_with(fresh_key, data, freshness.NAMESPACE))
    with pytest.raises(contract.GalleryError, match="trusted signer"):
        trust.verify(data, catalog(data, contract.NAMESPACE))
    with pytest.raises(freshness.FreshnessError, match="trusted freshness signer"):
        freshness.verify(data, catalog(data, freshness.NAMESPACE))


def test_verify_names_the_signer_that_vouched(catalog, signer):
    data = feed.canonical(document())
    assert feed.verify(data, signer(data)) == feed.PRINCIPAL
    assert feed.verify(data, catalog(data)) == feed.CATALOG_PRINCIPAL


@pytest.mark.parametrize("floor", [None, 7])
def test_a_catalog_cut_advances_at_most_catalog_max_step(catalog, signer, floor):
    """A stolen catalog key can't sign a sequence that recovery by a higher release-signed cut
    could never exceed (#2082): the step is bounded; release cuts are not."""
    if floor is not None:
        start = feed.canonical(document(floor))
        feed.accept(start, signer(start), now=NOW)
    base = floor or 0
    before = _state()
    too_far = feed.canonical(document(base + feed.CATALOG_MAX_STEP + 1))
    with pytest.raises(feed.FeedError, match="advances too far"):
        feed.accept(too_far, catalog(too_far), now=NOW)
    assert _state() == before
    edge = feed.canonical(document(base + feed.CATALOG_MAX_STEP))
    assert feed.accept(edge, catalog(edge), now=NOW).sequence == base + feed.CATALOG_MAX_STEP
    # The admitted catalog cut re-reads from storage without re-applying the bound.
    (storage.root() / "feed-floor.json").unlink()
    assert feed.historical().digest == hashlib.sha256(edge).hexdigest()
    assert feed.current(now=NOW).digest == hashlib.sha256(edge).hexdigest()
    # Recovery: a release-signed cut far above the floor stays admissible.
    far = feed.canonical(document(10**15))
    assert feed.accept(far, signer(far), now=NOW).sequence == 10**15


def test_the_catalog_step_is_measured_from_a_floor_ahead_of_its_record(catalog, signer):
    """An interrupted advancement leaves the floor ahead of the record: the bound counts from
    the higher of the two, the effective floor."""
    one = feed.canonical(document(1))
    feed.accept(one, signer(one), now=NOW)
    ahead = feed.canonical(document(500))
    with storage.locked("feed.json") as path:
        storage.write(path.with_name("feed-floor.json"), feed._mark(feed.parse(ahead, now=NOW)))
    edge = feed.canonical(document(500 + feed.CATALOG_MAX_STEP))
    assert feed.accept(edge, catalog(edge), now=NOW).sequence == 500 + feed.CATALOG_MAX_STEP
