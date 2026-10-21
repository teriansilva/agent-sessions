"""Hostile downloads and archives never turn into active plugin files (#1259)."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import tarfile
import time

import httpx
import pytest

from agent_sessions.plugins import artifacts, feed

URL = "https://github.com/example/fixture/releases/download/v1/fixture.tar.gz"


def archive(members, *, fmt=tarfile.USTAR_FORMAT):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=fmt) as tar:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o6755
            info.type = kind
            info.linkname = "../../outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            info.size = len(content) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(content) if info.size else None)
    return gzip.compress(raw.getvalue())


def record(data, url=URL):
    return feed.Artifact(url, hashlib.sha256(data).hexdigest(), ".")


def test_real_archive_is_inert_and_drops_privileged_metadata(tmp_path):
    data = archive([("bin/fixture", b"#!/bin/true\n", tarfile.REGTYPE)])
    destination = tmp_path / "stage"
    artifacts.extract(data, record(data), "tarball", destination)
    assert (destination / "bin/fixture").read_bytes() == b"#!/bin/true\n"
    assert (destination / "bin/fixture").stat().st_mode & 0o7777 == 0o700


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("dir/../../escape", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("device", tarfile.CHRTYPE),
        ("pipe", tarfile.FIFOTYPE),
        ("sparse", tarfile.GNUTYPE_SPARSE),
        ("back\\slash", tarfile.REGTYPE),
    ],
)
def test_archive_escape_and_special_members_are_refused(tmp_path, name, kind):
    data = archive([(name, b"payload", kind)])
    with pytest.raises(ValueError):
        artifacts.extract(data, record(data), "tarball", tmp_path / "stage")
    assert not (tmp_path / "escape").exists()


def test_digest_is_checked_before_any_extraction(tmp_path):
    data = archive([("fixture", b"hello", tarfile.REGTYPE)])
    rec = feed.Artifact(URL, "0" * 64, ".")
    with pytest.raises(artifacts.ArtifactError, match="digest"):
        artifacts.extract(data, rec, "tarball", tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_duplicate_files_and_existing_destinations_are_never_overwritten(tmp_path):
    data = archive(
        [("fixture", b"first", tarfile.REGTYPE), ("fixture", b"second", tarfile.REGTYPE)]
    )
    stage = tmp_path / "stage"
    with pytest.raises(artifacts.ArtifactError, match="duplicate"):
        artifacts.extract(data, record(data), "tarball", stage)
    assert (stage / "fixture").read_bytes() == b"first"
    other = archive([("fixture", b"overwrite", tarfile.REGTYPE)])
    with pytest.raises(FileExistsError):
        artifacts.extract(other, record(other), "tarball", stage)
    assert (stage / "fixture").read_bytes() == b"first"


def test_expansion_and_member_limits_precede_file_writes(tmp_path, monkeypatch):
    data = archive([("fixture", b"x" * 4096, tarfile.REGTYPE)])
    monkeypatch.setattr(artifacts, "MAX_EXPANDED", 1024)
    with pytest.raises(artifacts.ArtifactError, match="expanded"):
        artifacts.extract(data, record(data), "tarball", tmp_path / "stage")
    assert not (tmp_path / "stage").exists()
    monkeypatch.setattr(artifacts, "MAX_EXPANDED", 20000)
    monkeypatch.setattr(artifacts, "MAX_FILE", 1024)
    with pytest.raises(artifacts.ArtifactError, match="member"):
        artifacts.extract(data, record(data), "tarball", tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_pax_paths_are_checked_and_size_overrides_are_refused(tmp_path):
    for fields in [{"path": "../escape"}, {"size": "999999999"}, {"GNU.sparse.map": "0,99"}]:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo("fixture")
            info.pax_headers = fields
            info.size = 5
            tar.addfile(info, io.BytesIO(b"hello"))
        data = gzip.compress(stream.getvalue())
        with pytest.raises(ValueError):
            artifacts.extract(data, record(data), "tarball", tmp_path / "stage")
    assert not (tmp_path / "escape").exists()


def test_npm_wrapper_is_stripped_without_accepting_another_root(tmp_path):
    data = archive([("package/bin/fixture", b"hello", tarfile.REGTYPE)])
    artifacts.extract(data, record(data), "npm-prefix", tmp_path / "stage")
    assert (tmp_path / "stage/bin/fixture").read_bytes() == b"hello"
    outside = archive([("other/file", b"hello", tarfile.REGTYPE)])
    with pytest.raises(artifacts.ArtifactError, match="outside"):
        artifacts.extract(outside, record(outside), "npm-prefix", tmp_path / "other")


def test_redirect_authority_is_checked_before_next_request(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.test/payload"})

    monkeypatch.setattr(artifacts, "_TRANSPORT", httpx.MockTransport(handler))
    with pytest.raises(feed.FeedError, match="authority"):
        artifacts.fetch(record(b"test"), "tarball")
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


def test_allowed_release_redirect_download_and_digest(monkeypatch):
    payload = b"artifact bytes"
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://release-assets.githubusercontent.com/asset?signature=x"
                },
            )
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    monkeypatch.setattr(artifacts, "_TRANSPORT", httpx.MockTransport(handler))
    assert artifacts.fetch(record(payload), "tarball") == payload
    assert len(requests) == 2
    with pytest.raises(artifacts.ArtifactError, match="digest"):
        artifacts.fetch(record(b"wrong"), "tarball")


def test_download_limit(monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_DOWNLOAD", 3)
    monkeypatch.setattr(
        artifacts,
        "_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"four"))),
    )
    with pytest.raises(artifacts.ArtifactError, match="size"):
        artifacts.fetch(record(b"four"), "tarball")


@pytest.mark.parametrize("phase", ["headers", "redirects", "empty-body", "body"])
def test_total_download_deadline_cancels_every_network_phase(monkeypatch, phase):
    closed = []
    calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if phase == "body":
                yield b"x"
            await asyncio.sleep(10)
            if False:
                yield b""

        async def aclose(self):
            closed.append(True)

    async def handler(request):
        calls.append(request)
        if phase == "headers":
            await asyncio.sleep(10)
        if phase == "redirects":
            await asyncio.sleep(0.02)
            return httpx.Response(302, headers={"location": URL})
        return httpx.Response(200, stream=Stream())

    monkeypatch.setattr(artifacts, "DOWNLOAD_SECONDS", 0.05)
    monkeypatch.setattr(artifacts, "_TRANSPORT", httpx.MockTransport(handler))
    start = time.monotonic()
    with pytest.raises(artifacts.ArtifactError, match="time limit"):
        artifacts.fetch(record(b""), "tarball")
    assert time.monotonic() - start < 2
    assert calls
    if phase in ("empty-body", "body"):
        assert closed == [True]
    if phase == "redirects":
        assert 1 < len(calls) < 5
