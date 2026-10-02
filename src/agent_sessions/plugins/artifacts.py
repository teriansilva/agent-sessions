"""Digest-before-extract downloads and bounded, inert archive extraction (#1259).

No installer or lifecycle script runs. URLs come from a validated recipe; every redirect is
checked before the next request. Extraction drops ownership/mode metadata and admits only plain
directories/files, under an operator-owned staging directory with no pre-existing destinations.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import os
import tarfile
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import budget, feed, storage

MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILE = 320 * 1024 * 1024  # pinned native Codex is 287,086,056 bytes (#1266)
MAX_MEMBERS = 50000
MAX_METADATA = 16384
DOWNLOAD_SECONDS = 600  # shares the unchanged total install deadline; no extra budget
# Test seam only; no route, manifest or environment variable supplies a transport.
_TRANSPORT = None


class ArtifactError(ValueError):
    pass


def fetch(artifact: feed.Artifact, kind: str) -> bytes:
    # Installer jobs run outside the ASGI event loop. The async transport gives one cancellable
    # wall-clock deadline across DNS/connect, drip-fed headers, all redirects and body reads.
    return asyncio.run(_fetch(artifact, kind))


async def _fetch(artifact: feed.Artifact, kind: str) -> bytes:
    url = feed.artifact_url(artifact.url, kind)
    seconds = budget.remaining(DOWNLOAD_SECONDS)
    deadline = time.monotonic() + seconds
    try:
        async with (
            asyncio.timeout(seconds),
            httpx.AsyncClient(
                follow_redirects=False, timeout=10, trust_env=False, transport=_TRANSPORT
            ) as client,
        ):
            for hop in range(5):
                if time.monotonic() >= deadline:
                    raise TimeoutError
                async with client.stream(
                    "GET", url, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location or hop == 4:
                            raise ArtifactError("artifact redirect limit exceeded")
                        url = feed.artifact_url(urljoin(url, location), kind, redirect=True)
                        continue
                    if response.status_code != 200:
                        raise ArtifactError(
                            f"artifact download returned HTTP {response.status_code}"
                        )
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ArtifactError("artifact HTTP content encoding is not supported")
                    data = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data) + len(chunk) > MAX_DOWNLOAD:
                            raise ArtifactError("artifact download exceeded its size limit")
                        data.extend(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    if hashlib.sha256(data).hexdigest() != artifact.sha256:
                        raise ArtifactError("artifact digest does not match the reviewed recipe")
                    return bytes(data)
    except (TimeoutError, httpx.TimeoutException):
        raise ArtifactError("artifact download exceeded its time limit") from None
    raise ArtifactError("artifact redirect limit exceeded")


def _pax(data: bytes) -> None:
    # tarfile reads extended headers before returning a member. Preflight their size and fields
    # so a PAX size override/sparse map cannot evade the ordinary member limits or allocate GBs.
    while data:
        try:
            number, _ = data.split(b" ", 1)
            length = int(number)
            if length <= len(number) + 3 or length > len(data):
                raise ValueError
            record, data = data[:length], data[length:]
            if not record.endswith(b"\n"):
                raise ValueError
            key = record[len(number) + 1 :].split(b"=", 1)[0]
            if key not in (
                b"path",
                b"mtime",
                b"atime",
                b"ctime",
                b"uid",
                b"gid",
                b"uname",
                b"gname",
            ):
                raise ValueError
        except (ValueError, IndexError):
            raise ArtifactError("unsupported archive extended metadata") from None


def _preflight(data: bytes) -> None:
    offset, members = 0, 0
    while offset + 512 <= len(data):
        budget.check()
        header = data[offset : offset + 512]
        if header == bytes(512):
            if any(data[offset:]):
                raise ArtifactError("data follows the archive terminator")
            return
        members += 1
        try:
            size = int(header[124:136].strip(b"\x00 ") or b"0", 8)
            checksum = int(header[148:156].strip(b"\x00 "), 8)
        except ValueError:
            raise ArtifactError("unsupported archive numeric header") from None
        if checksum != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            raise ArtifactError("archive header checksum mismatch")
        kind = header[156:157]
        if kind not in (b"0", b"\x00", b"5", b"x", b"L"):
            raise ArtifactError("archive links, special files and sparse entries are refused")
        if size < 0 or size > MAX_FILE or members > MAX_MEMBERS:
            raise ArtifactError("archive exceeded its member limits")
        end = offset + 512 + ((size + 511) // 512) * 512
        if end > len(data):
            raise ArtifactError("archive is truncated")
        if kind in (b"x", b"L"):
            if size > MAX_METADATA:
                raise ArtifactError("archive metadata is too large")
            if kind == b"x":
                _pax(data[offset + 512 : offset + 512 + size])
        if kind == b"5" and size:
            raise ArtifactError("archive directory has a payload")
        offset = end
    if offset != len(data):
        raise ArtifactError("archive is truncated")


def extract(
    data: bytes,
    artifact: feed.Artifact,
    kind: str,
    destination: Path,
    *,
    max_expanded: int = MAX_EXPANDED,
) -> int:
    # Recheck at the boundary even when a caller passed bytes from a cache or a test fixture.
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ArtifactError("artifact digest does not match the reviewed recipe")
    if max_expanded <= 0:
        raise ArtifactError("archive exceeded its expanded size limit")
    limit = min(MAX_EXPANDED, max_expanded)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            chunks, length = [], 0
            while length <= limit:
                budget.check()
                chunk = compressed.read(min(65536, limit + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            raw = b"".join(chunks)
            budget.check()
        if len(raw) > limit:
            raise ArtifactError("archive exceeded its expanded size limit")
        _preflight(raw)
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except (OSError, EOFError, tarfile.TarError):
        raise ArtifactError("artifact is not a supported gzip tar archive") from None
    seen = set()
    with archive, storage.directory(destination):
        for member in archive:
            budget.check()
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            name = name.rstrip("/")
            if not name and member.isdir():
                continue
            if kind == "npm-prefix":
                if name == "package" and member.isdir():
                    continue
                if not name.startswith("package/"):
                    raise ArtifactError("npm archive contains files outside its package")
                name = name[len("package/") :]
            relative = feed.relative(name)
            if relative in seen:
                raise ArtifactError("duplicate archive destination")
            seen.add(relative)
            path = destination / relative
            if member.isdir():
                with storage.directory(path):
                    pass
                continue
            if not member.isreg() or member.size > MAX_FILE:
                raise ArtifactError("only bounded regular archive files are admitted")
            with storage.directory(path.parent) as parent:
                fd = os.open(
                    path.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o700 if member.mode & 0o111 else 0o600,
                    dir_fd=parent,
                )
                try:
                    source = archive.extractfile(member)
                    if source is None:
                        raise ArtifactError("archive file has no payload")
                    with source, os.fdopen(fd, "wb", closefd=False) as target:
                        copied = 0
                        while chunk := source.read(65536):
                            budget.check()
                            copied += len(chunk)
                            if copied > member.size:
                                raise ArtifactError("archive file exceeds its declared size")
                            target.write(chunk)
                        if copied != member.size:
                            raise ArtifactError("archive file is truncated")
                        target.flush()
                        os.fsync(fd)
                    os.fsync(parent)
                finally:
                    os.close(fd)
    budget.check()
    return len(raw)
