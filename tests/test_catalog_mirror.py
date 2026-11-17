"""The catalog mirror's GitHub leg (#2082 P3): the decision order, case by case.

Every case starts from a fake world: a fake github.com (the `releases/latest` redirect and the
release assets, which redirect to the asset host like the real one) and a fake relay (the agents
cut and the agents freshness statement), all behind `feed_client._TRANSPORT`, plus a fake upload
that writes into the fake GitHub. Cuts and statements are signed with throwaway keys pinned over
the installed signer files, and the mirror floor lives in a temporary state directory. Nothing
touches the network, a real key or the runner's real floor.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml

import test_feed_freshness as tff
import test_plugin_feed as tpf
from agent_sessions.plugins import feed, feed_client, freshness, freshness_client

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".forgejo" / "scripts" / "catalog_mirror.py"
WF = ROOT / ".forgejo" / "workflows" / "agent-catalog-mirror.yml"

spec = importlib.util.spec_from_file_location("catalog_mirror", SCRIPT)
cm = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = cm  # dataclasses resolve their module by name
spec.loader.exec_module(cm)

keys = tff.keys
signer = tpf.signer
catalog = tpf.catalog

TAG = "v0.30.0"
NEXT = "v0.30.1"
GH = f"https://github.com/{feed_client.REPOSITORY}/releases"
ASSETS = "https://release-assets.githubusercontent.com/github-production-release-asset"
_DOWNLOAD = re.compile(re.escape(GH) + r"/download/([^/]+)/([^/]+)")
_ASSET = re.compile(re.escape(ASSETS) + r"/([^/]+)/([^/]+)")


def _second_release_key(keys):
    """Another key trusted under the release principal: ed25519 signatures are deterministic,
    so "the same cut under another signature" needs a second signer."""
    key = tff._key(keys.tmp, "second-release")
    with feed.SIGNERS.open("a") as signers:
        signers.write(feed.PRINCIPAL + " " + key.with_suffix(".pub").read_text())
    return lambda data: tff._sign(key, data, feed.NAMESPACE)


def _cut(sequence, sign):
    data = feed.canonical(tpf.document(sequence))
    return data, sign(data)


class World:
    """The fake github.com + relay. `latest` may be a list: one answer per `releases/latest`."""

    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CATALOG_MIRROR_STATE_DIR", str(tmp_path / "mirror-state"))
        self.latest = [TAG]
        self.assets: dict[tuple[str, str], bytes] = {}
        self.relay: dict[str, bytes] = {}
        self.status: dict[str, int] = {}
        self.urls: list[str] = []
        self.uploads: list[str] = []
        self.latest_reads = 0
        monkeypatch.setattr(feed_client, "_TRANSPORT", httpx.MockTransport(self.respond))
        monkeypatch.setattr(feed.time, "time", lambda: tpf.NOW)

    def respond(self, request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        url = str(request.url)
        self.urls.append(url)
        if url in self.status:
            return httpx.Response(self.status[url])
        if url == feed_client.LATEST:
            tag = self.latest[min(self.latest_reads, len(self.latest) - 1)]
            self.latest_reads += 1
            return httpx.Response(302, headers={"location": f"{GH}/tag/{tag}"})
        if match := _DOWNLOAD.fullmatch(url):
            if match.groups() not in self.assets:
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": f"{ASSETS}/{'/'.join(match.groups())}"})
        if match := _ASSET.fullmatch(url):
            return httpx.Response(200, content=self.assets[match.groups()])
        if url in self.relay:
            return httpx.Response(200, content=self.relay[url])
        return httpx.Response(404)

    # -- relay ------------------------------------------------------------------------------------

    def publish(self, data, signature, *, pointer=None):
        snapshot = feed.parse(data)
        selected = pointer or {"sequence": snapshot.sequence, "digest": snapshot.digest}
        self.relay[cm.RELAY + "/current.json"] = feed.canonical(selected)
        base = f"{cm.RELAY}/releases/{snapshot.sequence}-{snapshot.digest}/plugin-feed.json"
        self.relay[base] = data
        self.relay[base + ".sig"] = signature
        return cm.Cut(data, signature, snapshot.sequence, snapshot.digest)

    def statement(self, keys, content_data, *, content_sequence, sequence=1, signature=None):
        doc = json.loads(content_data)
        data = tff.stmt(content_data, doc, sequence=sequence, content_sequence=content_sequence)
        digest = hashlib.sha256(data).hexdigest()
        self.relay[cm.FRESHNESS + "/current.json"] = feed.canonical(
            {"sequence": sequence, "digest": digest}
        )
        url = f"{cm.FRESHNESS}/releases/{sequence}-{digest}/statement.json"
        self.relay[url] = data
        self.relay[url + ".sig"] = keys.statement(data) if signature is None else signature

    # -- GitHub -----------------------------------------------------------------------------------

    def github(self, data=None, signature=None, tag=TAG):
        if data is not None:
            self.assets[(tag, "plugin-feed.json")] = data
        if signature is not None:
            self.assets[(tag, "plugin-feed.json.sig")] = signature

    def pair(self, tag=TAG):
        return (
            self.assets.get((tag, "plugin-feed.json")),
            self.assets.get((tag, "plugin-feed.json.sig")),
        )

    def upload(self, tag, cut):
        self.uploads.append(tag)
        self.github(cut.data, cut.signature, tag=tag)

    # -- the mirror floor -------------------------------------------------------------------------

    def set_floor(self, sequence, digest=None):
        path = cm.floor_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(sequence, cm.Cut):
            sequence, digest = sequence.sequence, sequence.digest
        path.write_text(json.dumps({"sequence": sequence, "digest": digest or "f" * 64}))

    def floor(self):
        return cm.read_floor()


def _run(world, *, upload=None, sleep=None):
    pauses = []

    async def no_sleep(seconds):
        pauses.append(seconds)

    code = cm.main([], upload=upload or world.upload, sleep=sleep or no_sleep)
    return code, pauses


@pytest.fixture
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path)


# ---- 3.2: absent, incomplete or not verifying ---------------------------------------------------


def test_an_absent_pair_at_r_equal_h_is_repaired_and_read_back(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.statement(keys, data, content_sequence=5)
    world.set_floor(4)  # the previous cut this job mirrored; a new app release has no pair yet
    assert _run(world)[0] == cm.OK
    assert world.uploads == [TAG] and world.pair() == (data, sig)
    assert world.floor() == {"sequence": 5, "digest": feed.parse(data).digest}
    assert "uploaded and read back the relay's cut 5" in capsys.readouterr().out


def test_no_freshness_statement_published_lets_the_floor_decide(keys, world):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.set_floor(cut)
    assert _run(world)[0] == cm.OK
    assert world.pair() == (data, sig)


@pytest.mark.parametrize("torn", ["sig-missing", "sig-mismatched", "json-truncated", "json-lower"])
def test_a_torn_pair_at_r_equal_h_is_repaired(keys, world, torn):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.statement(keys, data, content_sequence=5)
    world.set_floor(cut)  # this job's own interrupted upload of R wrote the floor first
    lower, lower_sig = _cut(4, keys.content)
    if torn == "sig-missing":
        world.github(data)  # an interrupted upload: the JSON landed, the signature didn't
    elif torn == "sig-mismatched":
        world.github(data, lower_sig)
    elif torn == "json-truncated":
        world.github(data[: len(data) // 2], sig)
    else:
        world.github(lower, sig)
    assert _run(world)[0] == cm.OK
    assert world.uploads == [TAG] and world.pair() == (data, sig)


@pytest.mark.parametrize("signature", ["missing", "mismatched"])
def test_a_torn_pair_left_by_a_higher_cut_while_the_relay_offers_a_lower_one_is_red(
    keys, world, capsys, signature
):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.statement(keys, data, content_sequence=5)
    world.set_floor(cut)  # even a floor that agrees with the relay can't outvote the torn bytes
    higher, _ = _cut(6, keys.content)
    world.github(higher, None if signature == "missing" else sig)
    assert _run(world)[0] == cm.RED
    assert world.uploads == [] and world.pair()[0] == higher
    assert "names sequence 6, above the relay's 5" in capsys.readouterr().out


def test_r_below_the_freshness_statement_with_a_torn_pair_is_red(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.set_floor(cut)
    # The freshness record names 6: the publication store was restored behind it.
    world.statement(keys, feed.canonical(tpf.document(6)), content_sequence=6)
    world.github(data)
    assert _run(world)[0] == cm.RED
    assert world.uploads == []
    assert "offers sequence 5 but once published 6" in capsys.readouterr().out


def test_r_below_the_freshness_statement_with_an_absent_pair_is_red(keys, world):
    data, sig = _cut(5, keys.content)
    world.set_floor(world.publish(data, sig))
    world.statement(keys, feed.canonical(tpf.document(7)), content_sequence=7)
    assert _run(world)[0] == cm.RED and world.uploads == []


@pytest.mark.parametrize("damage", ["bad-signature", "generation-missing"])
def test_an_unverifiable_freshness_statement_adds_nothing_and_the_floor_decides(
    keys, world, capsys, damage
):
    data, sig = _cut(5, keys.content)
    world.set_floor(world.publish(data, sig))
    bad = sig if damage == "bad-signature" else None  # the content key never signs statements
    world.statement(keys, data, content_sequence=5, signature=bad)
    if damage == "generation-missing":
        for url in [u for u in world.relay if u.endswith("statement.json.sig")]:
            del world.relay[url]
    assert _run(world)[0] == cm.OK and world.pair() == (data, sig)
    assert "freshness evidence unavailable, not counted" in capsys.readouterr().out


def test_a_freshness_statement_naming_another_cut_at_r_is_red(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    other = feed.canonical({**tpf.document(5), "issued_at": tpf.NOW - 120})
    world.statement(keys, other, content_sequence=5)
    world.github(*_cut(4, keys.content))
    assert _run(world)[0] == cm.RED and world.uploads == []


# ---- the mirror floor (Hermes on PR #2093) ------------------------------------------------------


class Crash(Exception):
    """The runner dies: nothing in the script catches this."""


@pytest.mark.parametrize("left", ["absent", "truncated"])
def test_a_higher_cut_torn_before_its_freshness_statement_then_a_relay_rollback_is_red(
    keys, world, capsys, left
):
    """Hermes' probe: the relay publishes 6, the upload of 6 tears, and the relay is restored to
    5 while its freshness statement (renewed only after mirror verification) still names 5. The
    freshness evidence says r == H; only the floor knows 6 was on its way to GitHub."""
    five, five_sig = _cut(5, keys.content)
    world.publish(five, five_sig)
    world.statement(keys, five, content_sequence=5)
    world.github(five, five_sig)
    assert _run(world)[0] == cm.OK and world.floor()["sequence"] == 5  # bootstrap no-op

    six, six_sig = _cut(6, keys.content)
    world.publish(six, six_sig)  # the statement still names 5: no mirror verification yet

    def tearing(tag, cut):
        world.uploads.append(tag)
        world.assets.clear()  # --clobber deleted the old assets first
        if left == "truncated":
            world.github(cut.data[: len(cut.data) // 2], tag=tag)
        raise cm.Red("gh release upload failed (exit 1)")

    assert _run(world, upload=tearing)[0] == cm.RED
    assert world.floor()["sequence"] == 6
    torn = world.pair()

    world.publish(five, five_sig)  # the relay is restored behind 6
    capsys.readouterr()
    assert _run(world)[0] == cm.RED
    assert world.pair() == torn and world.uploads == [TAG]  # nothing lowered, nothing uploaded
    assert world.floor()["sequence"] == 6
    assert "already started uploading 6; the relay went backwards" in capsys.readouterr().out


@pytest.mark.parametrize("github", ["verified-5", "absent"])
def test_the_floor_is_written_before_the_upload_and_a_crash_then_a_rollback_is_red(
    keys, world, github
):
    five, five_sig = _cut(5, keys.content)
    five_cut = world.publish(five, five_sig)
    world.set_floor(five_cut)
    if github == "verified-5":
        world.github(five, five_sig)
    six, six_sig = _cut(6, keys.content)
    six_cut = world.publish(six, six_sig)
    seen = []

    def crashing(tag, cut):
        seen.append(world.floor())  # the floor is already on disk when gh would start
        raise Crash

    with pytest.raises(Crash):
        _run(world, upload=crashing)
    assert seen == [{"sequence": 6, "digest": six_cut.digest}]

    world.publish(five, five_sig)  # rollback
    before = world.pair()
    assert _run(world)[0] == cm.RED
    assert world.pair() == before and world.floor()["sequence"] == 6


def test_no_floor_and_an_absent_pair_is_red_with_the_bootstrap_message(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.statement(keys, data, content_sequence=5)
    assert _run(world)[0] == cm.RED
    assert world.uploads == [] and world.pair() == (None, None) and world.floor() is None
    assert "bootstrap: no mirror floor on this runner" in capsys.readouterr().out


def test_no_floor_and_a_torn_pair_is_red(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(data)
    assert _run(world)[0] == cm.RED and world.uploads == [] and world.floor() is None


def test_a_verified_no_op_bootstraps_the_floor_and_a_later_tear_is_then_repaired(keys, world):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.github(data, sig)
    assert _run(world)[0] == cm.OK and world.uploads == []
    assert world.floor() == {"sequence": 5, "digest": cut.digest}
    world.assets.clear()
    world.github(data)  # a later torn pair at the same cut
    assert _run(world)[0] == cm.OK and world.pair() == (data, sig)


def test_a_verified_upload_raises_the_floor(keys, world):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.set_floor(3)
    world.github(*_cut(4, keys.content))
    assert _run(world)[0] == cm.OK
    assert world.floor() == {"sequence": 5, "digest": cut.digest}


def test_the_floor_is_never_lowered(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.set_floor(6, "a" * 64)
    with pytest.raises(cm.Red, match="refusing to lower the mirror floor from 6 to 5"):
        cm.write_floor(cut)
    with pytest.raises(cm.Red, match="another cut at sequence 6"):
        cm.write_floor(cm.Cut(b"", b"", 6, "b" * 64))
    lower = _cut(4, keys.content)
    world.github(*lower)  # a verified lower pair would normally be raised to R...
    assert _run(world)[0] == cm.RED  # ...but the relay is behind the floor
    assert world.pair() == lower and world.floor() == {"sequence": 6, "digest": "a" * 64}


def test_a_floor_naming_another_cut_at_r_is_red(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.set_floor(5, "a" * 64)
    world.github(data, sig)  # even an otherwise identical no-op
    assert _run(world)[0] == cm.RED and world.floor()["digest"] == "a" * 64


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"\xff",
        b"[]",
        b'{"sequence": 5}',
        b'{"sequence": "5", "digest": "' + b"a" * 64 + b'"}',
        b'{"sequence": true, "digest": "' + b"a" * 64 + b'"}',
        b'{"sequence": 0, "digest": "' + b"a" * 64 + b'"}',
        b'{"sequence": 5, "digest": "nothex"}',
        b'{"sequence": 5, "digest": "' + b"a" * 64 + b'", "extra": 1}',
        "directory",
    ],
)
def test_a_corrupt_floor_is_red_and_never_reset(keys, world, capsys, content):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(data, sig)
    path = cm.floor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if content == "directory":
        path.mkdir()
    else:
        path.write_bytes(content)
    assert _run(world)[0] == cm.RED and world.uploads == []
    assert "mirror floor" in capsys.readouterr().out
    if content != "directory":
        assert path.read_bytes() == content


def test_the_floor_write_is_atomic_and_private(keys, world):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    cm.write_floor(cut)
    path = cm.floor_path()
    assert json.loads(path.read_text()) == {"sequence": 5, "digest": cut.digest}
    assert sorted(p.name for p in path.parent.iterdir()) == ["floor.json"]  # no temp left over
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_the_floor_defaults_to_the_xdg_state_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("CATALOG_MIRROR_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert cm.floor_path() == tmp_path / "battlelab-catalog-mirror" / "floor.json"


# ---- 3.1: present and verifies ------------------------------------------------------------------


def test_a_lower_mirror_is_raised(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(*_cut(4, keys.content))
    assert _run(world)[0] == cm.OK
    assert world.uploads == [TAG] and world.pair() == (data, sig)


def test_a_lower_expired_mirror_is_raised_without_retained_evidence(keys, world):
    """3.1 needs no H: a verified lower pair is never a repair, even if expired."""
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.statement(keys, data, content_sequence=5, signature=b"broken")
    old = feed.canonical(
        {**tpf.document(4), "issued_at": tpf.NOW - 9 * 86400, "expires_at": tpf.NOW - 86400}
    )
    world.github(old, keys.content(old))
    assert _run(world)[0] == cm.OK and world.pair() == (data, sig)


def test_an_equal_identical_mirror_is_a_no_op(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(data, sig)
    assert _run(world)[0] == cm.OK
    assert world.uploads == [] and world.latest_reads == 1
    assert world.floor() == {"sequence": 5, "digest": feed.parse(data).digest}
    assert "already serves the relay's cut 5" in capsys.readouterr().out


def test_an_equal_sequence_with_different_bytes_is_red_equivocation(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    other = feed.canonical({**tpf.document(5), "issued_at": tpf.NOW - 120})
    world.github(other, keys.content(other))
    assert _run(world)[0] == cm.RED
    assert world.uploads == [] and world.pair()[0] == other
    assert "equivocation" in capsys.readouterr().out


def test_the_same_cut_under_another_signature_is_aligned_with_the_relays(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    other_sig = _second_release_key(keys)(data)
    assert other_sig != sig
    world.github(data, other_sig)
    assert _run(world)[0] == cm.OK and world.pair() == (data, sig)


def test_a_higher_mirror_is_red_and_never_lowered(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    higher = _cut(6, keys.content)
    world.github(*higher)
    assert _run(world)[0] == cm.RED
    assert world.uploads == [] and world.pair() == higher
    assert "holds sequence 6, above the relay's 5" in capsys.readouterr().out


def test_a_catalog_signed_relay_cut_is_mirrored(signer, catalog, world):
    data, sig = _cut(5, catalog)
    world.publish(data, sig)
    world.github(*_cut(4, signer))
    assert _run(world)[0] == cm.OK and world.pair() == (data, sig)


# ---- step 1: the pinned tag ---------------------------------------------------------------------


def test_the_latest_release_moving_mid_run_aborts_without_uploading(keys, world, capsys):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.set_floor(4)
    world.latest = [TAG, NEXT]
    assert _run(world)[0] == cm.MOVED == 0
    assert world.uploads == [] and world.pair() == (None, None)
    assert world.floor()["sequence"] == 4  # the floor is written only right before an upload
    assert f"moved from {TAG} to {NEXT} before the upload" in capsys.readouterr().out


def test_the_pair_is_read_from_the_pinned_tag(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(data, sig)
    world.github(*_cut(9, keys.content), tag=NEXT)  # never consulted: it isn't the pinned tag
    assert _run(world)[0] == cm.OK and world.uploads == []
    assert not any(f"/{NEXT}/" in url for url in world.urls)


def test_latest_moving_after_the_upload_is_reported_not_red(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.set_floor(4)
    world.latest = [TAG, TAG, NEXT]
    assert _run(world)[0] == cm.MOVED
    assert world.uploads == [TAG]


# ---- step 5: readback through the client's path -------------------------------------------------


@pytest.mark.parametrize("wrong", ["nothing", "other-signature", "other-cut"])
def test_a_readback_mismatch_is_red_after_bounded_retries(keys, world, capsys, wrong):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(*_cut(4, keys.content))
    second = _second_release_key(keys)

    def broken(tag, cut):
        world.uploads.append(tag)
        if wrong == "other-signature":
            world.github(cut.data, second(cut.data), tag=tag)
        elif wrong == "other-cut":
            world.github(*_cut(3, keys.content), tag=tag)

    code, pauses = _run(world, upload=broken)
    assert code == cm.RED
    assert pauses == [cm.READBACK_PAUSE] * (cm.READBACK_ATTEMPTS - 1)
    assert "readback" in capsys.readouterr().out


def test_a_readback_that_settles_on_a_retry_passes(keys, world):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(*_cut(4, keys.content))

    async def settle(seconds):
        world.github(data, sig)

    code, _ = _run(world, upload=lambda tag, cut: world.uploads.append(tag), sleep=settle)
    assert code == cm.OK


def test_readback_resolves_exactly_as_the_client_does(keys, world, monkeypatch):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.set_floor(4)
    calls = []
    real = feed_client.fetch_pair

    async def spy(client):
        calls.append(client)
        return await real(client)

    monkeypatch.setattr(feed_client, "fetch_pair", spy)
    seen = len(world.urls)
    original = world.upload

    def upload(tag, cut):
        nonlocal seen
        original(tag, cut)
        seen = len(world.urls)

    assert _run(world, upload=upload)[0] == cm.OK
    assert len(calls) == 1
    base = feed_client.feed_url(TAG)
    assert world.urls[seen:] == [
        feed_client.LATEST,
        base,
        f"{ASSETS}/{TAG}/plugin-feed.json",
        base + ".sig",
        f"{ASSETS}/{TAG}/plugin-feed.json.sig",
    ]


def test_the_client_refresh_goes_through_the_same_fetch_pair(keys, world, monkeypatch):
    data, sig = _cut(5, keys.content)
    world.github(data, sig)
    calls = []
    real = feed_client.fetch_pair

    async def spy(client):
        calls.append(client)
        return await real(client)

    monkeypatch.setattr(feed_client, "fetch_pair", spy)
    assert asyncio.run(feed_client.refresh()).sequence == 5
    assert len(calls) == 1


# ---- reads that fail decide nothing -------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 403])
def test_a_github_outage_on_an_asset_is_not_absence(keys, world, status):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.status[feed_client.feed_url(TAG) + ".sig"] = status
    assert _run(world)[0] == cm.UNAVAILABLE and world.uploads == []


def test_an_unreachable_relay_decides_nothing(keys, world):
    world.github(*_cut(4, keys.content))
    assert _run(world)[0] == cm.UNAVAILABLE and world.uploads == []


def test_a_relay_cut_that_does_not_verify_is_red(keys, world, capsys):
    data, _ = _cut(5, keys.content)
    world.publish(data, keys.content(data, namespace="other"))
    assert _run(world)[0] == cm.RED and world.uploads == []
    assert "does not verify" in capsys.readouterr().out


def test_a_relay_cut_that_does_not_match_its_pointer_is_red(keys, world):
    data, sig = _cut(5, keys.content)
    cut = world.publish(data, sig)
    world.relay[cm.RELAY + "/current.json"] = feed.canonical({"sequence": 5, "digest": "0" * 64})
    world.relay[f"{cm.RELAY}/releases/5-{'0' * 64}/plugin-feed.json"] = cut.data
    world.relay[f"{cm.RELAY}/releases/5-{'0' * 64}/plugin-feed.json.sig"] = cut.signature
    assert _run(world)[0] == cm.RED and world.uploads == []


def test_a_failed_gh_upload_is_red(keys, world, monkeypatch):
    data, sig = _cut(5, keys.content)
    world.publish(data, sig)
    world.github(*_cut(4, keys.content))

    class Done:
        returncode = 1

    monkeypatch.setattr(cm, "subprocess", types.SimpleNamespace(run=lambda *a, **kw: Done()))
    assert _run(world, upload=cm.gh_upload)[0] == cm.RED


# ---- the upload itself --------------------------------------------------------------------------


def test_gh_upload_is_one_argv_call_with_both_assets_and_no_injected_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "forgejo-injected")
    monkeypatch.setenv("GH_TOKEN", "other")
    seen = {}

    class Done:
        returncode = 0

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        seen["files"] = {Path(p).name: Path(p).read_bytes() for p in argv[4:6]}
        return Done()

    monkeypatch.setattr(cm, "subprocess", types.SimpleNamespace(run=run))
    cm.gh_upload(TAG, cm.Cut(b"{}", b"sig", 5, "0" * 64))
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:4] == ["/usr/bin/gh", "release", "upload", TAG]
    assert argv[6:] == ["--clobber", "--repo", feed_client.REPOSITORY]
    assert seen["files"] == {"plugin-feed.json": b"{}", "plugin-feed.json.sig": b"sig"}
    assert "shell" not in seen["kwargs"]
    assert "GITHUB_TOKEN" not in seen["kwargs"]["env"]
    assert "GH_TOKEN" not in seen["kwargs"]["env"]


def test_the_script_is_shell_free():
    source = SCRIPT.read_text()
    assert not re.search(r"shell=True|bash -c|os\.system\(|os\.popen\(", source)


def test_the_relay_origin_is_the_pinned_publisher_origin():
    origin = freshness_client.BASE.removesuffix("/catalogs/freshness/v1")
    assert origin == "https://relay.battlelab.superstatus.io"
    assert cm.RELAY == origin + "/catalogs/agents/v1"
    assert cm.FRESHNESS == freshness_client.BASE + "/agents"
    assert "agents" in freshness.FEEDS


# ---- the workflow -------------------------------------------------------------------------------


def _workflow():
    return yaml.safe_load(WF.read_text())


def test_the_workflow_is_hourly_and_dispatchable_in_its_own_group():
    wf = _workflow()
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert set(on) == {"schedule", "workflow_dispatch"}
    (cron,) = [s["cron"] for s in on["schedule"]]
    minute, hour, *rest = cron.split()
    assert minute.isdigit() and hour == "*" and rest == ["*", "*", "*"]
    assert wf["concurrency"] == {"group": "agent-catalog-mirror", "cancel-in-progress": False}
    assert wf["permissions"] == {"contents": "read"}


def test_the_workflow_runs_on_the_gh_login_host_with_no_secret():
    text = WF.read_text()
    assert "secrets." not in text
    (job,) = _workflow()["jobs"].values()
    assert job["runs-on"] == ["self-hosted", "host"]
    publish = yaml.safe_load((ROOT / ".forgejo/workflows/publish-public.yml").read_text())
    assert job["runs-on"] == publish["jobs"]["publish"]["runs-on"]
    (step,) = [s for s in job["steps"] if "catalog_mirror.py" in str(s.get("run", ""))]
    assert "unset GITHUB_TOKEN" in step["run"]
    assert step["if"] == "github.event_name == 'schedule' || github.ref == 'refs/heads/main'"


def test_no_other_workflow_uploads_the_catalog_pair():
    """The group serializes mirror publication only if this is the one automated writer."""
    for path in (ROOT / ".forgejo" / "workflows").glob("*.yml"):
        if path != WF:
            assert "plugin-feed.json" not in path.read_text(), path.name


def test_the_workflow_installs_only_the_mirror_group_lock_pinned():
    """The hourly job installs the `catalog-mirror` group alone (httpx, pinned like the app) from
    the lock, hash-verified: the full `uv sync` fetched the app's heavy dependencies every hour
    and hit the job deadline before the script ran (#2082 P3)."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    wf = (root / ".forgejo/workflows/agent-catalog-mirror.yml").read_text()
    assert "uv sync --locked --only-group catalog-mirror --no-install-project" in wf
    assert "uv run --no-sync python .forgejo/scripts/catalog_mirror.py" in wf
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["dependency-groups"]["catalog-mirror"] == ["httpx==0.28.1"]
    assert "httpx==0.28.1" in project["project"]["dependencies"]
